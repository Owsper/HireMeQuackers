"""Industry-agnostic discovery: search requests, query planning, filtering
and matching for non-IT professions, end to end with an offline source."""
from __future__ import annotations

import pytest

from config.search_config import EmploymentType, SearchConfig
from quackers import cli
from quackers.commands_extra import parse_search_request
from services.query_planner import plan_queries
from sources.base import JobSource, RawListing, SourceResult

IT_WORDS = ("software", "developer", "engineer", "programm", "python")

# (title, company, location, description) — one offline "job board".
LISTINGS = [
    ("Registered Nurse (m/w/d)", "Charité", "Berlin, Germany",
     "Patient care on a surgical ward. Nursing degree and German (B2) required."),
    ("Pflegefachkraft (m/w/d)", "Vivantes", "Berlin",
     "Wir suchen eine Pflegefachkraft für die Intensivstation. Patient care, wound care."),
    ("Senior Software Engineer", "Tech GmbH", "Berlin", "Python and Kubernetes."),
    ("Lagerhelfer (m/w/d)", "Logi AG", "Hamburg, Germany", "Picking, packing, forklift."),
    ("Warehouse Associate", "Ship Co", "Munich, Germany", "Picking and packing orders."),
    ("Staff Accountant", "Ledger Ltd", "London, UK", "Bookkeeping, reconciliation, IFRS."),
    ("Marketing Assistant", "Brand Berlin", "Berlin", "Social media, content creation."),
    ("Junior Graphic Designer", "Studio Paris", "Paris, France", "Illustrator, typography."),
    ("Marketing Intern", "Brand Berlin", "Berlin", "Social media internship."),
    ("Graduate Trainee Logistics", "Logi AG", "Berlin", "Graduate programme, supply chain."),
    ("Head of Finance", "Ledger Ltd", "Berlin", "Lead the finance team."),
    ("Sales Associate", "Shop KG", "Berlin", "Customer service, cash handling."),
]


class OfflineBoard(JobSource):
    name = "offline_board"
    slug = "offline_board"

    def scan(self, cfg):
        items = [RawListing(source=self.name, title=t, company=c, location=loc,
                            description_text=d, original_url=f"https://jobs.example/{i}")
                 for i, (t, c, loc, d) in enumerate(LISTINGS)]
        return SourceResult(success=True, listings=items, listings_found=len(items),
                            parsed=len(items))


@pytest.fixture
def offline(tmp_path, monkeypatch):
    from database.connection import init_db
    from sources import registry

    url = f"sqlite:///{(tmp_path / 't.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    init_db(url)
    registry.ensure_loaded()
    monkeypatch.setitem(registry.REGISTRY, OfflineBoard.name, OfflineBoard)
    return monkeypatch


def _search(mp, request: str):
    """Run `quackers search <request>` offline; return (matched titles, cfg)."""
    from services.discovery import run_discovery

    captured = {}

    def fake_run_scan(cfg, *, evaluate):
        cfg.enabled_sources = [OfflineBoard.name]
        cfg.enable_detail_extraction = False
        captured["cfg"] = cfg
        summary = run_discovery(cfg, evaluate_with_ai=False)
        captured["matched"] = sorted(j.title for j in summary.matched_jobs)
        return {"status": "done"}

    mp.setattr(cli, "_run_scan", fake_run_scan)
    mp.setattr(cli, "_print_scan_result", lambda r: None)
    args = cli.build_parser().parse_args(["search", *request.split(), "--no-ai"])
    args.func(args)
    return captured["matched"], captured["cfg"]


@pytest.mark.parametrize("request_text,expected", [
    ("Nurse in Berlin", ["Pflegefachkraft (m/w/d)", "Registered Nurse (m/w/d)"]),
    ("Warehouse Worker in Germany", ["Lagerhelfer (m/w/d)", "Warehouse Associate"]),
    ("Accountant in London", ["Staff Accountant"]),
    ("Marketing Assistant in Berlin", ["Marketing Assistant"]),
    ("Junior Graphic Designer in Paris", ["Junior Graphic Designer"]),
])
def test_search_finds_the_profession_and_nothing_else(offline, request_text, expected):
    matched, cfg = _search(offline, request_text)
    assert matched == expected
    # a plain profession search is not restricted to internships
    assert set(cfg.employment_types) == set(EmploymentType)


def test_any_entry_level_jobs_spans_industries_without_senior_roles(offline):
    matched, cfg = _search(offline, "Any entry-level jobs in Berlin")
    assert cfg.primary_title == "" and cfg.include_keywords == []
    for title in ("Marketing Intern", "Graduate Trainee Logistics", "Marketing Assistant",
                  "Sales Associate", "Registered Nurse (m/w/d)"):
        assert title in matched
    assert "Senior Software Engineer" not in matched and "Head of Finance" not in matched


def test_search_ignores_saved_it_keywords(offline):
    cfg = cli.load_config()
    cfg.include_keywords, cfg.primary_title = ["Python Developer"], "Python Developer"
    cli._save_config(cfg)
    matched, used = _search(offline, "Nurse in Berlin")
    assert used.include_keywords == ["Nurse"] and used.primary_title == "Nurse"
    assert "Registered Nurse (m/w/d)" in matched


def test_parse_search_request_shapes():
    assert parse_search_request("Warehouse Worker in Germany")["locations"] == ["Germany"]
    r = parse_search_request("Any entry-level jobs in Berlin")
    assert r["title"] == "" and "graduate" in r["employment_types"]
    r = parse_search_request("Marketing internship in Berlin or Munich")
    assert r["locations"] == ["Berlin", "Munich"] and r["employment_types"] == ["internship"]
    assert parse_search_request("remote Customer Service Agent")["remote"] is True


@pytest.mark.parametrize("title,locations", [
    ("Nurse", ["Berlin"]), ("Warehouse Worker", ["Germany"]), ("Accountant", ["London"]),
    ("Marketing Assistant", ["Berlin"]), ("Junior Graphic Designer", ["Paris"]),
    ("Electrician", ["Vienna"]),
])
def test_planner_has_no_it_leak_for_non_it_titles(title, locations):
    cfg = SearchConfig(primary_title=title, include_keywords=[title], locations=locations,
                       employment_types=list(EmploymentType))
    plan = plan_queries(cfg, max_queries=200)
    qs = [s.query_string.lower() for s in plan]
    assert qs and all(not any(w in q for w in IT_WORDS) for q in qs), qs
    for q, s in zip(qs, plan):
        if s.meta.get("lang") == "en" and s.category in ("jobboard", "ats", "role"):
            assert title.lower() in q, q


def test_planner_level_only_and_localized_non_it_titles():
    plan = plan_queries(SearchConfig(locations=["Berlin"],
                                     employment_types=[EmploymentType.GRADUATE,
                                                       EmploymentType.INTERNSHIP]),
                        max_queries=200)
    qs = {s.query_string for s in plan}
    assert "graduate jobs" in qs and "internship" in qs and "Berufseinsteiger" in qs
    assert not any("software" in q.lower() for q in qs)
    de = plan_queries(SearchConfig(primary_title="Nurse", locations=["Berlin"],
                                   employment_types=list(EmploymentType)), max_queries=200)
    assert any(s.query_string == "Pflegefachkraft Berlin" for s in de)
    fr = plan_queries(SearchConfig(primary_title="Accountant", locations=["Paris"],
                                   employment_types=list(EmploymentType)), max_queries=200)
    assert any(s.query_string == "comptable" for s in fr)


def test_lang_command_drives_localized_titles(offline):
    def run(argv):
        a = cli.build_parser().parse_args(argv)
        a.func(a)

    run(["lang", "es", "it"])
    cfg = cli.load_config()
    assert cfg.search_languages == ["es", "it"]
    cfg.primary_title, cfg.locations = "Warehouse Worker", ["London"]
    cfg.employment_types = list(EmploymentType)  # any level: plain local titles
    qs = {s.query_string for s in plan_queries(cfg, max_queries=200)}
    assert "mozo de almacén" in qs and "magazziniere" in qs
    with pytest.raises(cli.CliError, match="Unknown language"):
        run(["lang", "xx"])
    run(["lang", "auto"])
    assert cli.load_config().search_languages == []


def test_skill_tagging_and_matching_are_industry_neutral():
    import json

    from database.models import CandidateProfile, Job
    from services.ai_client import deterministic_score
    from services.skill_lexicon import extract_skills
    from sources.greenhouse import GreenhouseSource

    assert {"patient care", "wound care"} <= set(extract_skills("Patient care and wound care."))
    assert {"forklift", "picking"} <= set(GreenhouseSource._extract_skills("forklift, picking"))

    nurse = CandidateProfile(
        name="Ada", skills_json=json.dumps(["Patient care", "Wound care"]),
        experience_json=json.dumps([{"title": "Krankenpfleger, Klinikum Nord"}]),
        languages_json="[]")

    def job(title, desc):
        return Job(title=title, company="X", original_url="u", source="t",
                   content_fingerprint=title, description=desc, employment_type="other")

    ward = deterministic_score(nurse, job("Registered Nurse", "Patient care and wound care."))
    books = deterministic_score(nurse, job("Staff Accountant", "Bookkeeping and IFRS."))
    assert ward["total_score"] > books["total_score"]
    assert ward["experience_fit"] == "great" and books["experience_fit"] == "good"


def test_excluded_titles_whole_word_on_title_only():
    from database.models import Job
    from services.filters import apply_filters

    cfg = SearchConfig(employment_types=list(EmploymentType), excluded_titles=["senior", "lead"])
    jobs = [Job(title=t, company="X", original_url="u", source="t", content_fingerprint=t,
                employment_type="other", description="You will lead projects.")
            for t in ("Senior Nurse", "Nurse", "Leadership Trainee")]
    included, _ = apply_filters(jobs, cfg)
    assert [j.title for j in included] == ["Nurse", "Leadership Trainee"]

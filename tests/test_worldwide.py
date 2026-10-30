"""Worldwide discovery: any country / city, no Germany/Berlin defaults,
location-aware languages, regional boards, coverage notes, end to end."""
from __future__ import annotations

import pytest

from config.search_config import EmploymentType, SearchConfig
from quackers import cli
from quackers.commands_extra import coverage_notes, parse_search_request
from services import geo
from services.multilingual import search_languages
from services.query_planner import plan_queries
from sources.base import JobSource, RawListing, SourceResult

GERMAN_WORDS = ("praktikum", "praktikant", "werkstudent", "berufseinsteiger", "wir suchen",
                "berlin", "germany", "deutschland")

LISTINGS = [
    ("Registered Nurse", "Toronto General", "Toronto, ON"),
    ("Warehouse Worker", "Gulf Logistics", "Dubai, United Arab Emirates"),
    ("Mechanical Engineer", "Nippon Machines", "Tokyo, Japan"),
    ("Marketing Manager", "Big Apple Brands", "New York, NY"),
    ("Graduate Analyst", "Down Under Bank", "Sydney NSW"),
    ("Accountant", "Ledger Ltd", "London, United Kingdom"),
    ("Python Developer", "Spree Tech", "Berlin, Germany"),
    ("Customer Support Agent", "Anywhere Inc", "Remote - Worldwide"),
]


class WorldBoard(JobSource):
    name = "world_board"
    slug = "world_board"

    def scan(self, cfg):
        items = [RawListing(source=self.name, title=t, company=c, location=loc,
                            workplace_arrangement="remote" if "Remote" in loc else "onsite",
                            description_text=f"{t} at {c}.", original_url=f"https://w.example/{i}")
                 for i, (t, c, loc) in enumerate(LISTINGS)]
        return SourceResult(success=True, listings=items, listings_found=len(items))


@pytest.fixture
def world(tmp_path, monkeypatch):
    from database.connection import init_db
    from sources import registry

    url = f"sqlite:///{(tmp_path / 't.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    init_db(url)
    registry.ensure_loaded()
    monkeypatch.setitem(registry.REGISTRY, WorldBoard.name, WorldBoard)
    return monkeypatch


def _search(mp, request):
    from services.discovery import run_discovery

    seen = {}

    def fake_run_scan(cfg, *, evaluate):
        cfg.enabled_sources = [WorldBoard.name]
        cfg.enable_detail_extraction = False
        seen["cfg"] = cfg
        seen["matched"] = sorted(j.title for j in run_discovery(cfg, evaluate_with_ai=False).matched_jobs)
        return {"status": "done"}

    mp.setattr(cli, "_run_scan", fake_run_scan)
    mp.setattr(cli, "_print_scan_result", lambda r: None)
    args = cli.build_parser().parse_args(["search", *request.split(), "--no-ai"])
    args.func(args)
    return seen["matched"], seen["cfg"]


@pytest.mark.parametrize("request_text,expected", [
    ("Nurse in Toronto", ["Registered Nurse"]),
    ("Warehouse Worker in Dubai", ["Warehouse Worker"]),
    ("Mechanical Engineer in Tokyo", ["Mechanical Engineer"]),
    ("Marketing Manager in New York", ["Marketing Manager"]),
    ("Entry-level jobs in Australia", ["Graduate Analyst"]),
    ("Accountant in London", ["Accountant"]),
    ("Python Developer in Berlin", ["Python Developer"]),
])
def test_search_in_any_country(world, request_text, expected):
    matched, _ = _search(world, request_text)
    assert matched == expected


def test_jobs_in_any_country_and_remote_worldwide(world):
    cfg = cli.load_config()
    cfg.locations = ["Berlin"]  # a saved preference must not leak into "any country"
    cli._save_config(cfg)
    matched, used = _search(world, "Jobs in any country")
    assert used.locations == [] and len(matched) == len(LISTINGS)
    matched, used = _search(world, "Remote jobs worldwide")
    assert used.locations == [] and used.worldwide_remote
    assert matched == ["Customer Support Agent"]


def test_parse_requests_from_the_spec():
    cases = {
        "Python Developer in Berlin": ("Python Developer", ["Berlin"]),
        "Accountant in London": ("Accountant", ["London"]),
        "Marketing Manager in New York": ("Marketing Manager", ["New York"]),
        "Nurse in Toronto": ("Nurse", ["Toronto"]),
        "Warehouse Worker in Dubai": ("Warehouse Worker", ["Dubai"]),
        "Mechanical Engineer in Tokyo": ("Mechanical Engineer", ["Tokyo"]),
        "Entry-level jobs in Australia": ("", ["Australia"]),
    }
    for text, (title, locs) in cases.items():
        r = parse_search_request(text)
        assert (r["title"], r["locations"]) == (title, locs), text
    r = parse_search_request("Remote jobs worldwide")
    assert r["remote"] and r["anywhere"] and r["locations"] == []
    r = parse_search_request("Jobs in any country")
    assert r["anywhere"] and r["locations"] == [] and not r["remote"]


@pytest.mark.parametrize("title,place", [
    ("Nurse", "Toronto"), ("Warehouse Worker", "Dubai"), ("Mechanical Engineer", "Tokyo"),
    ("Marketing Manager", "New York"), ("Accountant", "Sydney"), ("Teacher", "São Paulo"),
])
def test_planner_uses_the_requested_place_and_no_german_defaults(title, place):
    cfg = SearchConfig(primary_title=title, include_keywords=[title], locations=[place],
                       employment_types=list(EmploymentType))
    qs = [s.query_string.lower() for s in plan_queries(cfg, max_queries=200)]
    assert any(place.lower() in q for q in qs)
    leaked = [q for q in qs if any(w in q for w in GERMAN_WORDS)]
    assert not leaked, leaked


def test_location_languages_and_lang_preference_win():
    assert search_languages(SearchConfig(locations=["Tokyo"])) == ["en", "ja"]
    assert search_languages(SearchConfig(locations=["Dubai"])) == ["en", "ar"]
    assert search_languages(SearchConfig(locations=["Paris"])) == ["en", "fr"]
    assert search_languages(SearchConfig(locations=["Berlin"])) == ["en", "de"]
    # /lang de chosen: Tokyo does not force Japanese
    assert search_languages(SearchConfig(locations=["Tokyo"], search_languages=["de"])) == ["en", "de"]
    tokyo = plan_queries(SearchConfig(primary_title="Mechanical Engineer", locations=["Tokyo"],
                                      employment_types=list(EmploymentType)), max_queries=200)
    assert any(s.query_string == "機械エンジニア Tokyo" for s in tokyo)
    dubai = plan_queries(SearchConfig(primary_title="Warehouse Worker", locations=["Dubai"],
                                      employment_types=list(EmploymentType)), max_queries=200)
    assert any(s.query_string == "عامل مستودع" for s in dubai)


def test_regional_boards_follow_the_country():
    from services.platforms import boards_for_scan

    def keys(locs):
        return [b.key for b in boards_for_scan(locs)]

    tokyo, ny, berlin, anywhere = keys(["Tokyo"]), keys(["New York"]), keys(["Berlin"]), keys([])
    assert not {"stepstone", "jobware", "ziprecruiter"} & set(tokyo)
    assert ny[0] == "ziprecruiter" and "stepstone" not in ny
    assert berlin[0] in ("stepstone", "jobware") and "ziprecruiter" not in berlin
    assert {"stepstone", "ziprecruiter", "linkedin"} <= set(anywhere)


def test_geo_resolution_and_location_filter_any_country():
    from database.models import Job
    from services.filters import apply_filters

    assert geo.resolve("Sydney NSW") == ("sydney", "australia")
    assert geo.resolve("Toronto, ON") == ("toronto", "canada")
    assert geo.same_place("Dubai, UAE", "United Arab Emirates")
    assert not geo.same_place("London, ON, Canada", "London")

    def job(loc):
        return Job(title="Nurse", company="X", location=loc, original_url="u", source="t",
                   content_fingerprint=loc, employment_type="other")

    cfg = SearchConfig(locations=["Australia"], employment_types=list(EmploymentType))
    included, _ = apply_filters([job("Sydney NSW"), job("Melbourne VIC"), job("Berlin")], cfg)
    assert [j.location for j in included] == ["Sydney NSW", "Melbourne VIC"]
    cfg = SearchConfig(locations=["Canada"], employment_types=list(EmploymentType))
    assert [j.location for j in apply_filters([job("Toronto, ON"), job("Tokyo")], cfg)[0]] == ["Toronto, ON"]


def test_intent_recognises_countries_worldwide():
    from config.search_intent import build_intent

    intent = build_intent(SearchConfig(locations=["Australia", "Japan", "Toronto", "UAE"]))
    assert intent.countries == ["Australia", "Japan", "UAE"] and intent.cities == ["Toronto"]


def test_coverage_notes_are_honest():
    notes = coverage_notes(SearchConfig(locations=["Tokyo"]))
    assert any("Arbeitnow" in n and "Japan" in n for n in notes)
    assert any("EURES" in n for n in notes)
    assert coverage_notes(SearchConfig(locations=["Berlin"])) == []
    assert coverage_notes(SearchConfig(locations=[])) == []

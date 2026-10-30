"""Multilingual search: languages, localized queries, detection, extraction,
title matching, language gate / fit and the job-language migration."""
from __future__ import annotations

from config.search_config import SearchConfig
from config.search_intent import build_intent
from services import multilingual as ml
from services.query_planner import plan_queries
from sources.base import RawListing

DE_DESC = ("Wir suchen eine engagierte Werkstudentin für unser Team in Berlin, die mit uns "
           "die Zukunft der Mobilität gestaltet. Du bringst sehr gute Deutsch- und "
           "Englischkenntnisse mit und hast Erfahrung mit Python.")
EN_DESC = ("We are looking for a motivated intern to join our data team in Berlin. You will "
           "build dashboards with us and you are fluent in English. German is a plus.")


def test_search_languages_from_locations_and_config():
    assert ml.search_languages(SearchConfig(locations=["Berlin"])) == ["en", "de"]
    assert ml.search_languages(SearchConfig(locations=["Zurich, Switzerland"])) == ["en", "de", "fr"]
    cfg = SearchConfig(locations=["Berlin"], search_languages=["fr", "xx"], include_english=False)
    assert ml.search_languages(cfg) == ["de", "fr"]
    assert ml.search_languages(SearchConfig(locations=["Berlin"], auto_detect_local_language=False)) == ["en"]
    # "Bern" must not fire inside "Bernard"
    assert ml.languages_for_locations(["Bernard Street, London"]) == []


def test_canonicalize_and_localize_titles():
    assert ml.canonicalize_title("Werkstudent Softwareentwicklung (m/w/d)") == \
        "working student software development"
    assert ml.canonicalize_title("Stagiaire développeur web H/F") == "intern web developer"
    assert ml.canonicalize_title("Software Engineer Intern") == "software engineer intern"
    assert ml.localized_titles("Software Developer Intern", "de")[0] == "Praktikum Softwareentwickler"
    assert ml.localized_titles("Data Analyst Intern", "fr")[0] == "stage analyste de données"
    # unknown role and no level word -> no guessed translation
    assert ml.localized_titles("Product Manager", "de") == []
    assert ml.localized_titles("Software Developer Intern", "en") == []


def test_planner_emits_localized_queries_and_respects_include_english():
    plan = plan_queries(SearchConfig(locations=["Paris"], primary_title="Data Analyst Intern"),
                        max_queries=200)
    fr = [s.query_string for s in plan if s.meta.get("lang") == "fr"]
    assert "stage analyste de données" in fr and "stage analyste de données Paris" in fr
    assert any(s.meta.get("lang") == "en" for s in plan)

    only_local = plan_queries(SearchConfig(locations=["Paris"], include_english=False,
                                           primary_title="Data Analyst Intern"), max_queries=200)
    for s in only_local:
        assert s.meta.get("lang") != "en" or s.category in {"ats", "jobboard", "company"}
    assert any(s.meta.get("lang") == "fr" for s in only_local)

    # explicit search language without a matching location
    es = plan_queries(SearchConfig(search_languages=["es"], primary_title="Data Analyst Intern"),
                      max_queries=200)
    assert any(s.query_string == "prácticas analista de datos" for s in es)


def test_detect_language_is_conservative():
    assert ml.detect_language(DE_DESC) == "de"
    assert ml.detect_language(EN_DESC) == "en"
    assert ml.detect_language("Python SQL Docker Kubernetes AWS GCP Terraform React Go Rust") is None
    assert ml.detect_language("Hallo Welt") is None


def test_extract_required_languages_needs_requirement_context():
    assert ml.extract_required_languages(DE_DESC) == ["de", "en"]
    assert ml.extract_required_languages("Fluent English (C1) required.") == ["en"]
    assert ml.extract_required_languages("Maîtrise de l'anglais exigée") == ["en"]
    assert ml.extract_required_languages("Deutschkenntnisse von Vorteil") == ["de"]
    # merely mentioning a language/country is not a requirement
    assert ml.extract_required_languages("We are a German company building German software.") == []
    assert ml.extract_required_languages("") == []


def test_profile_language_codes_and_fit():
    assert ml.profile_language_codes(["German (C1)", "English - native", {"name": "Русский"}]) == \
        ["de", "en", "ru"]
    assert ml.profile_language_codes(["IT skills"]) == []
    assert ml.language_fit([], ["en"]) == ("great", [])
    assert ml.language_fit(["de", "fr"], ["de", "en"]) == ("poor", ["fr"])
    assert ml.language_fit(["de"], []) == ("unknown", ["de"])


def test_local_title_matches_english_intent_but_keeps_seniority_rules():
    from services.title_relevance import _norm_title, classify_title

    intent = build_intent(SearchConfig(primary_title="Software Developer Intern"))
    assert classify_title("Praktikum Softwareentwickler (m/w/d)", intent).passes_hard
    assert classify_title("Stagiaire développeur logiciel H/F", intent).passes_hard
    assert not classify_title("Senior Softwareentwickler", intent).passes_hard
    assert not classify_title("Marketing Praktikum", intent).passes_hard
    # Unicode letters are kept (used to become "m nchen")
    assert _norm_title("Entwickler München") == "entwickler münchen"


def test_raw_to_job_fills_languages_and_respects_source_values():
    from services.normalization import raw_to_job

    norm = raw_to_job(RawListing(source="t", title="Werkstudent Python (m/w/d)", company="A",
                                 description_text=DE_DESC))
    assert norm["description_language"] == "de" and norm["required_languages"] == ["de", "en"]
    given = raw_to_job(RawListing(source="t", title="X", company="A", description_text=DE_DESC,
                                  description_language="de", required_languages=["FR"]))
    assert given["required_languages"] == ["fr"]
    empty = raw_to_job(RawListing(source="t", title="Intern", company="A"))
    assert empty["description_language"] is None and empty["required_languages"] is None


def _job(**kw):
    from database.models import Job

    base = dict(title="Data Intern", company="A", original_url="u", source="t",
                content_fingerprint="f", employment_type="internship")
    base.update(kw)
    return Job(**base)


def test_description_language_gate_passes_unknown():
    from services.filters import apply_filters

    cfg = SearchConfig(description_languages=["en"])
    de, en, unknown = (_job(description_language="de"), _job(description_language="en"),
                       _job(description_language=None))
    included, excluded = apply_filters([de, en, unknown], cfg)
    assert included == [en, unknown]
    assert "written in 'de'" in excluded[0][1]


def test_deterministic_language_fit_uses_codes():
    import json

    from database.models import CandidateProfile
    from services.gemini_client import deterministic_score

    prof = CandidateProfile(name="Ada", languages_json=json.dumps([{"name": "English (C1)"}]),
                            skills_json="[]")
    needs_de = _job(required_languages_json=json.dumps(["de"]), description="x")
    needs_en = _job(required_languages_json=json.dumps(["en"]), description="x")
    assert deterministic_score(prof, needs_de)["language_fit"] == "poor"
    assert deterministic_score(prof, needs_en)["language_fit"] == "great"
    no_langs = CandidateProfile(name="Ada", languages_json="[]", skills_json="[]")
    assert deterministic_score(no_langs, needs_de)["language_fit"] == "good"


def test_job_language_columns_persist_and_migrate(tmp_path):
    import sqlite3

    from sqlalchemy import create_engine, inspect
    from sqlalchemy.orm import Session

    from database import migrations, repository
    from database.connection import init_db

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("CREATE TABLE jobs (id INTEGER PRIMARY KEY, title TEXT);"
                      "INSERT INTO jobs VALUES (1, 'kept');")
    con.commit()
    con.close()
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    migrations.upgrade(engine)
    migrations.upgrade(engine)
    cols = {c["name"] for c in inspect(engine).get_columns("jobs")}
    assert {"description_language", "required_languages_json"} <= cols
    with engine.connect() as c:
        assert c.exec_driver_sql("SELECT title FROM jobs").scalar() == "kept"

    url = f"sqlite:///{(tmp_path / 'new.db').as_posix()}"
    init_db(url)
    with Session(create_engine(url)) as s:
        job, _ = repository.get_or_create_job_by_fingerprint(
            s, "fp1", title="T", company="C", original_url="u", source="t",
            description_language="de", required_languages=["en", "de"])
        assert job.description_language == "de" and job.required_languages == ["de", "en"]
        unknown, _ = repository.get_or_create_job_by_fingerprint(
            s, "fp2", title="T2", company="C", original_url="u2", source="t",
            required_languages=None)
        assert unknown.required_languages_json is None

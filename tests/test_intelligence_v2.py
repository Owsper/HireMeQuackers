"""V2 reliability contracts. Synthetic CVs/pages, isolated databases, zero paid calls."""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

import httpx
import pytest

from config.settings import AppConfig
from database.models import CandidateProfile, Job
from services import llm
from services.ai_diagnostics import SAMPLE_JD
from services.job_intelligence import calculate_score, map_evidence, extract_requirements


def candidate(**kwargs):
    data = dict(id=1, profile_version=1, name="Ada Example", skills_json='["SQL", "Python"]',
                experience_json='[{"title":"Developer", "employer":"Real Company", "dates":"2020-2024", "summary":"Built Python services"}]',
                education_json='[{"degree":"Bachelor of Computer Science"}]', languages_json='[{"name":"English", "proficiency":"C1"}]', other_json="{}")
    data.update(kwargs)
    return CandidateProfile(**data)


def vacancy(**kwargs):
    data = dict(id=1, title="Python Developer", company="Example", description=SAMPLE_JD,
                required_skills_json='["Python", "SQL", "Rust"]', original_url="https://example.test/job/1", source="test", content_fingerprint="test")
    data.update(kwargs)
    return Job(**data)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    from database.connection import init_db
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'jobs.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    init_db()
    return tmp_path


class SemanticAI:
    available, label, provider, model, reason = True, "mock:model", "mock", "model", None
    last_diagnostic = {"success": True, "retry_count": 0}

    def complete(self, system, prompt, json_mode=False):
        request = json.loads(prompt)
        if "candidate_evidence" in request:
            return json.dumps({"mappings": [{"requirement_id": r["id"], "status": "UNKNOWN", "evidence_index": None} for r in request["requirements"]]})
        if "skills" in request:
            return json.dumps({"skill_order": list(reversed(request["skills"]))})
        return json.dumps({"requirements": [{"text": "Python", "source_quote": "Python is required.", "category": "required_skills", "kind": "skill"}]})


def test_unknown_is_not_missing_and_score_has_evidence():
    report = calculate_score(candidate(), vacancy(), AppConfig().matching_weights)
    assert report["total_score"] is not None
    rust = next(r for r in report["requirement_matrix"] if r["text"] == "Rust")
    assert rust["status"] == "UNKNOWN" and not rust["evidence"]
    assert "Rust" not in report["missing_requirements"]
    assert report["scoring_status"] == "PROVISIONAL"
    assert report["score_range"][0] <= report["score_range"][1]
    assert all(r["evidence"] for r in report["requirement_matrix"] if r["status"] == "MATCHED")


def test_explicit_user_denial_is_missing():
    c = candidate(other_json='{"unavailable_skills":["Rust"]}')
    row = map_evidence([{"id": 0, "text": "Rust", "category": "required_skills", "source_quote": "Rust"}], c)[0]
    assert row["status"] == "MISSING"


@pytest.mark.parametrize("description,method,status", [(None, None, "UNAVAILABLE"), ("Python Developer", None, None), (SAMPLE_JD, "search_snippet", "PARTIAL")])
def test_insufficient_description_never_gets_a_score(description, method, status):
    r = calculate_score(candidate(), vacancy(description=description, extraction_method=method, extraction_status=status), AppConfig().matching_weights)
    assert r["total_score"] is None and r["scoring_status"] == "INSUFFICIENT_INFORMATION"


def test_exact_weighted_calculation_and_unknown_exclusion(monkeypatch):
    import services.job_intelligence as ji
    rows = [{"id": i, "text": str(i), "category": category, "source_quote": str(i), "status": status, "evidence": []}
            for i, (category, status) in enumerate([("required_skills", "MATCHED"), ("required_skills", "MISSING"), ("experience", "PARTIALLY_MATCHED"), ("languages", "UNKNOWN")])]
    monkeypatch.setattr(ji, "extract_requirements", lambda *a, **kw: rows)
    monkeypatch.setattr(ji, "map_evidence", lambda *a, **kw: rows)
    result = ji.calculate_score(candidate(), vacancy(), AppConfig().matching_weights)
    assert result["total_score"] == 50
    assert result["category_scores"]["languages"]["score"] is None
    assert not result["category_scores"]["location"]["applicable"]


def test_ai_requirement_quotes_and_mapping_schema_are_enforced():
    requirements = extract_requirements(vacancy(), SemanticAI())
    assert requirements
    bad = NS(complete=lambda *a, **kw: '{"requirements":[{"text":"PhD", "category":"qualifications", "source_quote":"PhD required"}]}')
    with pytest.raises(llm.LLMError, match="ungrounded"):
        extract_requirements(vacancy(), bad)
    bad = NS(complete=lambda *a, **kw: '{"mappings":[{"requirement_id":0,"status":"MISSING","evidence_index":0}]}')
    with pytest.raises(llm.LLMError, match="omission"):
        map_evidence([{"id": 0, "text": "Rust", "category": "required_skills", "source_quote": "Rust"}], candidate(), bad)


def test_language_levels_are_compared_and_short_skill_boundaries():
    reqs = [{"id": 0, "text": "English B2 required", "category": "languages", "source_quote": "English B2"},
            {"id": 1, "text": "C", "category": "required_skills", "source_quote": "C"}]
    rows = map_evidence(reqs, candidate())
    assert rows[0]["status"] == "MATCHED"
    assert rows[1]["status"] == "UNKNOWN"


def test_ai_mapping_cannot_promote_unproved_numeric_requirement():
    ai = NS(complete=lambda *a, **kw: '{"mappings":[{"requirement_id":0,"status":"MATCHED","evidence_index":3}]}')
    reqs = [{"id": 0, "text": "10 years Python experience", "category": "experience", "source_quote": "10 years Python experience"}]
    row = map_evidence(reqs, candidate(), ai)[0]
    assert row["status"] != "MATCHED"


def test_provenance_excludes_inference():
    from services.candidate_intelligence import provenance, evidence
    fields = {"skills": ["Python", "Rust"]}
    ledger = provenance(fields, raw_text="Skills: Python", source="original.pdf")
    assert [x["status"] for x in ledger["claims"]] == ["VERIFIED", "INFERRED"]
    c = candidate(skills_json='["Python", "Rust"]', other_json=json.dumps({"provenance": ledger}))
    assert not any(e["quote"] == "Rust" for e in evidence(c))


def test_score_cache_key_covers_full_content_candidate_and_weights(monkeypatch):
    from services.ai_client import AIClient
    c, j = candidate(), vacancy()
    client = AIClient()
    base = client._cache_key(c, j)
    c.summary = "Changed summary"
    assert client._cache_key(c, j) != base
    c.summary = None
    j.description += "x" * 2000
    mid = client._cache_key(c, j)
    j.description += "Changed after 1500 characters"
    assert client._cache_key(c, j) != mid
    monkeypatch.setenv("MATCHING_WEIGHTS", json.dumps({**AppConfig().matching_weights, "experience": 50}))
    assert client._cache_key(c, j) != mid


def test_failed_ai_results_never_cached_and_recovery_is_allowed(monkeypatch):
    from services import ai_client
    store = {}
    monkeypatch.setattr(ai_client, "cache_get", store.get)
    monkeypatch.setattr(ai_client, "cache_set", store.__setitem__)
    monkeypatch.setattr(ai_client, "record_usage", lambda **kw: None)
    client = ai_client.AIClient()
    client._ai = SemanticAI()
    original = client._ai.complete
    client._ai.complete = Mock(side_effect=llm.LLMError("invalid response"))
    for _ in range(3):
        assert client.score_job_fit(candidate(), vacancy())["ai_model"] == "deterministic-fallback"
    assert not store and client.is_configured
    client._ai.complete = original
    assert client.score_job_fit(candidate(), vacancy())["ai_model"] == "mock:model"
    assert len(store) == 1
    assert client.score_job_fit(candidate(), vacancy())["cache_hit"]


@pytest.mark.parametrize("status,expected_attempts", [(401, 1), (403, 1), (400, 1), (429, 3), (503, 3)])
def test_provider_failures_are_classified_retried_and_redacted(status, expected_attempts, monkeypatch, caplog):
    monkeypatch.setattr(llm, "sdk_installed", lambda sdk: True)
    delays = []
    monkeypatch.setattr(llm.time, "sleep", delays.append)
    ai = llm.LLM(AppConfig(ai_provider="openai", openai_api_key="PRIVATE_KEY", ai_backoff_seconds=1))
    class ProviderError(Exception):
        status_code = status
    call = Mock(side_effect=ProviderError("PRIVATE_KEY CV_PERSONAL_TEXT"))
    ai._client = NS(responses=NS(create=call))
    with pytest.raises(llm.LLMError):
        ai.complete("PRIVATE_KEY", "CV_PERSONAL_TEXT")
    assert call.call_count == expected_attempts
    assert delays == ([1, 2] if expected_attempts == 3 else [])
    assert ai.last_diagnostic["retry_count"] == expected_attempts - 1
    assert "PRIVATE_KEY" not in caplog.text and "CV_PERSONAL_TEXT" not in caplog.text


def test_timeout_retry_recovery_and_malformed_json(monkeypatch):
    monkeypatch.setattr(llm, "sdk_installed", lambda sdk: True)
    monkeypatch.setattr(llm.time, "sleep", lambda n: None)
    ai = llm.LLM(AppConfig(ai_provider="openai", openai_api_key="dummy"))
    call = Mock(side_effect=[TimeoutError(), NS(output_text='{"ok":true}')])
    ai._client = NS(responses=NS(create=call))
    assert ai.complete("JSON", "JSON", True) == '{"ok":true}'
    assert ai.last_diagnostic["retry_count"] == 1
    ai._client = NS(responses=NS(create=Mock(return_value=NS(output_text="not JSON"))))
    with pytest.raises(llm.LLMError, match="malformed"):
        ai.complete("JSON", "JSON", True)


@pytest.mark.parametrize("provider", list(llm.PROVIDERS))
def test_each_provider_uses_selected_model_and_sdk_contract(provider, monkeypatch):
    monkeypatch.setattr(llm, "sdk_installed", lambda sdk: True)
    p = llm.PROVIDERS[provider]
    kwargs = {p.key: "test-key"} if p.key else {}
    ai = llm.LLM(AppConfig(ai_provider=provider, ai_model="chosen-model", **kwargs))
    if p.sdk == "gemini":
        call = Mock(return_value=NS(text='{"ok":true}'))
        ai._client = NS(models=NS(generate_content=call))
    elif p.sdk == "anthropic":
        call = Mock(return_value=NS(stop_reason="end_turn", content=[NS(type="text", text='{"ok":true}')]))
        ai._client = NS(messages=NS(create=call))
    elif ai.responses_api:
        call = Mock(return_value=NS(output_text='{"ok":true}'))
        ai._client = NS(responses=NS(create=call))
    else:
        call = Mock(return_value=NS(choices=[NS(message=NS(content='{"ok":true}'))]))
        ai._client = NS(chat=NS(completions=NS(create=call)))
    assert json.loads(ai.complete("Return JSON", "JSON", True))["ok"]
    assert call.call_args.kwargs["model"] == "chosen-model"


def test_jsonld_graph_and_html_beats_metadata():
    from services.detail_extractor import extract_from_html
    graph = {"@graph": [{"@type": "Organization"}, {"@type": ["JobPosting"], "title": "Engineer", "description": "Short."}]}
    html = f'<script type="application/ld+json">{json.dumps(graph)}</script><meta property="og:description" content="snippet"><article><nav>Cookie junk</nav>{SAMPLE_JD}</article>'
    result = extract_from_html(html, "https://example.test/1")
    assert result.extraction_status == "COMPLETE" and result.extraction_method == "html"
    assert "Cookie junk" not in result.description_text and "SQL" in result.description_text
    assert result.structured_requirements


def test_embedded_json_and_incomplete_metadata():
    from services.detail_extractor import extract_from_html
    html = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps({"props": {"job": {"title": "Engineer", "jobDescription": SAMPLE_JD}}}) + '</script>'
    assert extract_from_html(html, "https://example.test/1").extraction_method == "embedded_json"
    result = extract_from_html('<meta property="og:description" content="A short search summary">', "https://example.test/1")
    assert result.extraction_status == "PARTIAL" and result.extraction_confidence <= .35


@pytest.mark.parametrize("status", [401, 403, 429, 404, 500])
def test_failed_fetch_status_never_complete(status, monkeypatch):
    from services import detail_extractor as de
    monkeypatch.setattr("services.robots.is_allowed_by_robots", lambda *a, **kw: True)
    monkeypatch.setattr(de, "safe_get", lambda *a, **kw: NS(status_code=status, content=b"", headers={}))
    result = de.fetch_and_extract("https://example.test/job")
    assert result.extraction_status in {"UNAVAILABLE", "NEEDS_MANUAL_REVIEW"} and result.extraction_errors


def test_robots_blocks_fetch_and_is_evaluated_per_path(monkeypatch):
    from services import robots, detail_extractor
    robots.clear_cache()
    monkeypatch.setattr(robots, "safe_get", lambda *a, **kw: NS(status_code=200, content=b"User-agent: *\nDisallow: /private\n"))
    assert not robots.is_allowed_by_robots("https://example.test/private/job")
    assert robots.is_allowed_by_robots("https://example.test/public/job")
    fetch = Mock()
    monkeypatch.setattr(detail_extractor, "safe_get", fetch)
    result = detail_extractor.fetch_and_extract("https://example.test/private/job")
    assert result.extraction_status == "NEEDS_MANUAL_REVIEW" and not fetch.called
    robots.clear_cache()


@pytest.mark.parametrize("provider,url,payload", [
    ("greenhouse", "https://boards.greenhouse.io/acme/jobs/123", {"title": "Engineer", "content": SAMPLE_JD}),
    ("lever", "https://jobs.lever.co/acme/123", {"text": "Engineer", "descriptionPlain": SAMPLE_JD}),
    ("ashby", "https://jobs.ashbyhq.com/acme/123", {"jobs": [{"id": "123", "title": "Engineer", "descriptionPlain": SAMPLE_JD}]}),
])
def test_official_ats_detail_is_used(provider, url, payload, monkeypatch):
    from services import ats_detail
    monkeypatch.setattr(ats_detail, "is_allowed_by_robots", lambda *a, **kw: True)
    monkeypatch.setattr(ats_detail, "safe_get", lambda *a, **kw: NS(status_code=200, content=json.dumps(payload).encode()))
    detail = ats_detail.fetch_ats_detail(url)
    assert detail.extraction_status == "COMPLETE" and detail.extraction_method == "ats_" + provider


def test_pdf_docx_json_parsing_preserves_originals(tmp_path):
    from documents.exporter import export_cv_text
    from services.cv_parser import parse_cv
    text = "Ada Example\nada@example.test\n\n## Skills\nPython, SQL\n\n## Work Experience\nDeveloper at Real Company 2020-2024\nBuilt Python services and maintained SQL reports with the engineering team.\n\n## Education\nBachelor of Computer Science\n"
    for ext in (".pdf", ".docx"):
        path = tmp_path / ("cv" + ext)
        export_cv_text(text, path)
        original = path.read_bytes()
        parsed = parse_cv(path)
        assert "Python" in parsed["raw_text"] and parsed["fields"]["name"] == "Ada Example"
        assert path.read_bytes() == original
    path = tmp_path / "cv.json"
    path.write_text(json.dumps({"name": "Ada Example", "skills": ["Python"]}))
    assert parse_cv(path)["fields"]["skills"] == ["Python"]


def test_tailoring_reorders_only_existing_facts_and_rejects_invention():
    from services.tailored_cv import build_tailored_cv, validate_draft
    c, j = candidate(), vacancy()
    draft = build_tailored_cv(c, j, SemanticAI())
    assert draft["validation_status"] == "PASSED" and draft["keywords_added"] == []
    assert "Real Company" in draft["tailored_cv"] and "2020-2024" in draft["tailored_cv"]
    assert "Rust" not in draft["tailored_cv"]
    draft["tailored_cv"] += "- Certified Rust expert\n"
    with pytest.raises(ValueError, match="factual"):
        validate_draft(draft, c, j)


def test_tailoring_rejects_ai_skill_fabrication():
    from services.tailored_cv import build_tailored_cv
    ai = NS(complete=lambda *a, **kw: '{"skill_order":["Python", "SQL", "Rust"]}')
    draft = build_tailored_cv(candidate(), vacancy(), ai)
    assert draft["fallback_reason"] and "Rust" not in draft["tailored_cv"]


def test_cli_end_to_end_import_discover_extract_match_review_export_track(isolated, monkeypatch):
    from quackers import cli, store
    from services.discovery import run_discovery
    from sources.base import RawListing, SourceResult
    from sources.search_engine import ScanSummary
    from services.detail_extractor import extract_from_html
    from database.models import GeneratedDocument, JobMatch, Application
    def run(argv):
        args = cli.build_parser().parse_args(argv)
        args.func(args)
    source = isolated / "original.json"
    source.write_text(json.dumps({"name": "Ada Example", "skills": ["SQL", "Python"], "experience": [{"title": "Developer", "employer": "Real Company", "dates": "2020-2024"}]}))
    original = source.read_bytes()
    run(["cv", str(source), "--yes"])
    result = SourceResult(success=True, listings=[RawListing(source="test", title="Python Developer", company="Example", description_text="snippet", original_url="https://example.test/job/1")])
    # Match the existing source engine's result contract.
    summary = ScanSummary()
    summary.per_source = {"test": result}
    summary.all_listings = result.listings
    summary.total_discovered = 1
    summary.status = "done"
    contexts = []
    def discover(*args, **kwargs):
        contexts.append(kwargs.get("profile"))
        return summary
    monkeypatch.setattr("sources.search_engine.SearchEngine.run_all", discover)
    monkeypatch.setattr("services.enrichment_service.fetch_and_extract", lambda *a, **kw: extract_from_html("<article>" + SAMPLE_JD + "</article>", "https://example.test/job/1"))
    from config.search_config import SearchConfig
    cfg = SearchConfig(employment_types=[], enable_detail_extraction=True, enrichment_delay_seconds=0)
    scan = run_discovery(cfg)
    assert contexts and contexts[0].skills == ["SQL", "Python"]
    assert scan.new_jobs == 1 and scan.enrichment_succeeded == 1
    with store.with_session() as s:
        job = s.query(Job).one()
        jid = job.id
        assert job.extraction_status == "COMPLETE" and s.query(JobMatch).count() == 1
    run(["match", str(jid)])
    run(["job", str(jid)])
    run(["docs", "cv", str(jid), "--tailored"])
    with store.with_session() as s:
        did = s.query(GeneratedDocument).filter_by(doc_type="tailored_cv").one().id
    with pytest.raises(cli.CliError, match="approve"):
        run(["docs", "export", str(did), str(isolated / "out.docx")])
    run(["docs", "show", str(did)])
    run(["docs", "approve", str(did)])
    for ext in (".docx", ".pdf", ".txt"):
        dest = isolated / ("out" + ext)
        run(["docs", "export", str(did), str(dest)])
        assert dest.stat().st_size > 0
    with pytest.raises(cli.CliError, match="original CV"):
        run(["docs", "export", str(did), str(source), "--yes"])
    with store.with_session() as s:
        document = s.get(GeneratedDocument, did)
        draft_path = Path(document.file_path)
        draft = json.loads(draft_path.read_text(encoding="utf-8"))
        draft["notes"] += " altered after approval"
        draft_path.write_text(json.dumps(draft), encoding="utf-8")
    with pytest.raises(cli.CliError, match="changed since approval"):
        run(["docs", "export", str(did), str(isolated / "tampered.txt")])
    run(["app", "set", str(jid), "SHORTLISTED"])
    with store.with_session() as s:
        assert s.query(Application).one().status == "SHORTLISTED"
        assert json.loads(s.query(CandidateProfile).one().experience_json)[0]["employer"] == "Real Company"
    assert source.read_bytes() == original


def test_additive_migration_backs_up_and_preserves_legacy_history(tmp_path):
    import sqlite3
    from sqlalchemy import create_engine, inspect
    from database.connection import init_db
    path = tmp_path / "legacy.db"
    engine = init_db(f"sqlite:///{path}")
    from sqlalchemy.orm import Session
    from database.models import Application
    with Session(engine) as s:
        s.add(CandidateProfile(id=1, name="Original", profile_version=1, active=True))
        s.add(vacancy())
        s.commit()
        s.add(Application(job_id=1, status="APPLIED"))
        s.commit()
    # Real legacy columns and records, including dependent application/document history.
    with engine.begin() as con:
        con.exec_driver_sql("CREATE TABLE job_matches AS SELECT id,job_id,match_score,score_json,matching_evidence_json,missing_requirements_json,ai_model,evaluated_at,profile_version,scoring_config_hash FROM candidate_job_matches")
        con.exec_driver_sql("INSERT INTO job_matches (id,job_id,match_score,profile_version) VALUES (9,1,75,1)")
        con.exec_driver_sql("ALTER TABLE jobs DROP COLUMN raw_description")
    engine.dispose()
    engine = init_db(f"sqlite:///{path}")
    init_db(f"sqlite:///{path}")
    backups = list(tmp_path.glob("legacy.db.pre-v2-*.bak"))
    assert backups
    with sqlite3.connect(backups[0]) as con:
        assert con.execute("SELECT name FROM candidate_profiles").fetchone() == ("Original",)
    with engine.connect() as con:
        assert con.exec_driver_sql("SELECT status FROM applications").scalar() == "APPLIED"
        assert con.exec_driver_sql("SELECT match_score FROM job_matches WHERE id=9").scalar() == 75
        assert con.exec_driver_sql("SELECT COUNT(*) FROM candidate_job_matches WHERE id=9").scalar() == 1
    assert "raw_description" in {c["name"] for c in inspect(engine).get_columns("jobs")}


def test_candidate_match_rows_do_not_collide(isolated):
    from quackers import store
    from database import repository as db
    with store.with_session() as s:
        a = db.upsert_candidate_profile(s, name="A")
        b = db.upsert_candidate_profile(s, name="B")
        j = vacancy()
        s.add(j)
        s.commit()
        for c, score in ((a, 90), (b, 25)):
            db.create_match(s, job_id=j.id, profile_version=1, candidate_profile_id=c.id, match_score=score)
        assert db.get_match_for_job(s, j.id, 1, a.id).match_score == 90
        assert db.get_match_for_job(s, j.id, 1, b.id).match_score == 25


def test_scalar_profile_corrections_version_the_profile_and_source(isolated):
    from services import candidate_profile as cp
    profile = cp.upsert_from_extraction({"name": "Ada", "summary": "Original", "skills": ["Python"]})
    changed = cp.upsert_profile(profile.id, summary="Corrected")
    assert changed.profile_version == 2
    ledger = json.loads(changed.other_json)["provenance"]
    assert any(c["value"] == "Corrected" and c["status"] == "USER_CONFIRMED" for c in ledger["claims"])


def test_enrichment_upgrades_snippet_and_caches_success_and_failures(isolated, monkeypatch):
    from quackers import store
    from services.enrichment_service import enrich_missing_jobs
    from services.detail_extractor import extract_from_html, ExtractedDetail
    from config.search_config import SearchConfig
    cfg = SearchConfig(enrichment_delay_seconds=0)
    with store.with_session() as s:
        j = vacancy(description="Tiny snippet", extraction_status="PARTIAL", extraction_method="search_snippet")
        s.add(j)
        s.commit()
        fetch = Mock(return_value=extract_from_html("<article>" + SAMPLE_JD + "</article>", j.original_url))
        monkeypatch.setattr("services.enrichment_service.fetch_and_extract", fetch)
        assert enrich_missing_jobs(s, cfg, job_ids=[j.id]).succeeded == 1
        assert j.description == SAMPLE_JD.strip() and j.extraction_status == "COMPLETE"
        assert enrich_missing_jobs(s, cfg, job_ids=[j.id]).attempted == 0
        j.extracted_at = None
        fetch.return_value = ExtractedDetail(extraction_errors=["network_error"])
        assert enrich_missing_jobs(s, cfg, job_ids=[j.id]).failed == 1
        assert j.description == SAMPLE_JD.strip()
        assert enrich_missing_jobs(s, cfg, job_ids=[j.id]).attempted == 0


def test_duplicate_tracking_urls_share_identity_and_distinct_ids_survive(isolated):
    from services.normalization import canonical_url
    from services.discovery import _find_same_vacancy
    from quackers import store
    assert canonical_url("https://EXAMPLE.test/job?id=1&utm_source=x#fragment") == canonical_url("https://example.test/job?id=1")
    assert canonical_url("https://example.test/job?id=2") != canonical_url("https://example.test/job?id=1")
    with store.with_session() as s:
        j = vacancy(external_job_id="1", location="Berlin")
        s.add(j)
        s.commit()
        assert _find_same_vacancy(s, {"title": j.title, "company": j.company, "location": "Berlin", "source": "test", "external_job_id": "2", "original_url": "https://example.test/job/2"}) is None


def test_robots_longest_allow_and_outage_are_respected(monkeypatch):
    from services import robots
    assert robots._path_allowed("User-agent: *\nDisallow: /jobs/\nAllow: /jobs/public/", "/jobs/public/1")
    assert not robots._path_allowed("User-agent: *\nDisallow: /jobs/\nAllow: /jobs/public/", "/jobs/private/1")
    robots.clear_cache()
    monkeypatch.setattr(robots, "safe_get", Mock(side_effect=TimeoutError()))
    assert not robots.is_allowed_by_robots("https://example.test/job")
    robots.clear_cache()


def test_safe_fetch_follows_only_validated_redirects_and_bounds_content(monkeypatch):
    from services.safe_requests import safe_get
    monkeypatch.setattr("services.safe_requests._resolve_fqdn", lambda host: ["8.8.8.8"])
    visited = []
    def handler(request):
        visited.append(str(request.url))
        return httpx.Response(302, headers={"location": "/final"}) if request.url.path == "/start" else httpx.Response(200, content=b"x" * 100)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = safe_get("https://example.test/start", client=client, max_bytes=10)
        assert result.content == b"x" * 10 and "truncated" in result.error
        assert len(visited) == 2
        blocked = safe_get("https://example.test/start", client=client, redirect_policy=lambda url: False)
        assert blocked.status_code == 403
    with pytest.raises(ValueError):
        safe_get("http://127.0.0.1/private")


def test_capability_probes_are_independent_and_never_count_fallback():
    from services.ai_diagnostics import probe
    for capability in ("extraction", "matching", "tailoring"):
        assert probe(capability, SemanticAI())["status"] == "PASSED"
    bad = SemanticAI()
    bad.complete = lambda *a, **kw: '{"skill_order":["invented"]}'
    assert probe("tailoring", bad)["status"] == "FAILED"


def test_tailoring_keeps_employers_attached_to_their_roles():
    from services.tailored_cv import build_tailored_cv, validate_draft, render_cv
    c, j = candidate(), vacancy()
    draft = build_tailored_cv(c, j)
    draft["tailored_data"]["experience"][0]["employer"] = "Invented Employer"
    draft["tailored_cv"] = render_cv(draft["tailored_data"])
    with pytest.raises(ValueError, match="history|factual"):
        validate_draft(draft, c, j)


def test_cannot_tailor_without_a_description():
    from services.tailored_cv import build_tailored_cv
    with pytest.raises(ValueError, match="description"):
        build_tailored_cv(candidate(), vacancy(description=None))


def test_unrelated_profile_edit_does_not_confirm_inferred_skills(isolated):
    from services import candidate_profile as cp
    from services.candidate_intelligence import provenance
    from services.tailored_cv import build_tailored_cv
    ledger = provenance({"skills": ["Python", "Rust"]}, raw_text="Python", source="original.pdf")
    p = cp.upsert_from_extraction({"name": "Ada", "skills": ["Python", "Rust"], "other": {"provenance": ledger}})
    p = cp.upsert_profile(p.id, summary="A corrected summary")
    claims = json.loads(p.other_json)["provenance"]["claims"]
    assert next(c for c in claims if c["value"] == "Rust")["status"] == "INFERRED"
    with pytest.raises(ValueError, match="inferred"):
        build_tailored_cv(p, vacancy())


def test_distinct_ats_requisitions_survive_scan_deduplication():
    from sources.search_engine import ScanSummary, _add_listing
    from sources.base import RawListing
    summary, keys = ScanSummary(), {}
    for identifier in ("1", "2"):
        _add_listing(summary, keys, RawListing(source="ATS", title="Engineer", company="Example", location="Berlin", external_job_id=identifier, original_url=f"https://example.test/jobs/{identifier}"))
    assert len(summary.all_listings) == 2


def test_truncated_description_stays_partial_through_storage_and_matching(isolated):
    from services.detail_extractor import extract_from_html, MAX_DESCRIPTION_CHARS
    from database import repository as db
    from quackers import store
    detail = extract_from_html("<article>" + SAMPLE_JD + " " + "Detailed responsibility. " * MAX_DESCRIPTION_CHARS + "</article>", "https://example.test/1")
    assert detail.extraction_status == "PARTIAL" and "description_truncated" in detail.extraction_errors
    with store.with_session() as session:
        job = vacancy(description=None)
        session.add(job)
        session.commit()
        db.update_job_from_enrichment(session, job.id, detail)
        assert job.extraction_status == "PARTIAL"
        assert calculate_score(candidate(), job, AppConfig().matching_weights)["scoring_status"] != "SCORED"


def test_unknown_requirements_are_not_stored_or_displayed_as_missing(isolated, capsys):
    from services.matching import evaluate_jobs
    from quackers import store, cli
    from database.models import JobMatch
    with store.with_session() as session:
        c = candidate(active=True)
        j = vacancy()
        session.add_all([c, j])
        session.commit()
        report = evaluate_jobs([j], c)[0][1]
        row = session.query(JobMatch).one()
        assert "Rust" in report["unknown_requirements"]
        assert json.loads(row.missing_requirements_json) == []
    cli._print_match_report(report)
    output = capsys.readouterr().out
    assert "UNKNOWN [required_skills] Rust" in output
    assert "MISSING [required_skills] Rust" not in output
    assert "Candidate evidence" in output and "Evidence coverage" in output

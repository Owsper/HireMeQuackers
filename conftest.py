"""Pytest configuration; isolates DB to a temp file + mock external I/O."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TEST_DB_PATH = ROOT / "test_data" / "test.db"


# ---- Fixtures ----


@pytest.fixture(scope="session", autouse=True)
def _prepare_test_db(tmp_path_factory):
    """Create the test DB directory and a fresh DB file."""
    TEST_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if TEST_DB_PATH.exists():
        try:
            TEST_DB_PATH.unlink()
        except Exception:
            pass
    yield
    # leave file for inspection


@pytest.fixture(autouse=True)
def _no_real_ai(monkeypatch):
    """Tests never reach a real AI provider, whatever keys this machine has."""
    from config import settings

    monkeypatch.setattr(settings, "_ENV_LOADED", True)
    monkeypatch.setenv("ENABLE_PLAYWRIGHT", "false")
    for name in ("AI_PROVIDER", "AI_MODEL", "GEMINI_API_KEY", "GEMINI_MODEL", "OPENAI_API_KEY", "OPENAI_BASE_URL",
                 "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "GROQ_API_KEY", "OLLAMA_BASE_URL"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="session")
def sql_engine(test_db_cleanup):
    """Provide one engine for the whole session bound to the test DB."""
    from database.connection import get_engine, init_db

    url = f"sqlite:///{TEST_DB_PATH}"
    engine = get_engine(url)
    # Ensure schema is created before any test touches the DB
    init_db(url)
    return engine


@pytest.fixture(scope="session")
def test_db_cleanup():
    """No-op cleanup marker; ordering + safety."""
    if TEST_DB_PATH.exists():
        try:
            TEST_DB_PATH.unlink()
        except Exception:
            pass
    yield


@pytest.fixture(scope="session")
def session_factory(sql_engine):
    """Yield an SQLAlchemy session factory; commit/rollback on demand."""
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=sql_engine, expire_on_commit=True)
    yield factory


@pytest.fixture(scope="function")
def db_session(session_factory):
    """Provide a per-test session; rolled back at end."""
    session = session_factory()
    yield session
    try:
        session.rollback()
    finally:
        session.close()


@pytest.fixture
def app_config(monkeypatch):
    """Provide an AppConfig bound to the temp test DB."""
    from config.settings import AppConfig

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{TEST_DB_PATH}")
    env = AppConfig(
        database_url=f"sqlite:///{TEST_DB_PATH}",
        data_dir=ROOT / "test_data",
    )
    return env


@pytest.fixture
def search_config_factory():
    def _mk(**overrides: Any) -> Any:
        from config.search_config import SearchConfig

        defaults: dict[str, Any] = dict(
            employment_types=["internship"],
            include_keywords=["python", "data"],
            locations=["Berlin", "Germany"],
            include_remote=True,
            include_hybrid=True,
            max_results=25,
            min_match_score=0.0,
        )
        defaults.update(overrides)
        return SearchConfig(**defaults)

    return _mk


@pytest.fixture
def sample_job_factory():
    def _mk(**overrides: Any) -> dict[str, Any]:
        defaults: dict[str, Any] = dict(
            title="Python Data Intern",
            company="Acme Corp",
            location="Berlin",
            country="Germany",
            employment_type="internship",
            workplace_arrangement="hybrid",
            description="Python | data | ML role about...",
            required_skills_json='["python", "data"]',
            preferred_skills_json='["ml"]',
            original_url="https://acme.example/careers/di-1",
            application_url="https://acme.example/apply/di-1",
            source="acme",
        )
        defaults.update(overrides)
        return defaults

    return _mk


@pytest.fixture
def sample_profile(db_session):
    """Insert a tightly-controlled test profile."""
    from database.models import CandidateProfile
    from database import repository as db

    profile = db.upsert_candidate_profile(
        db_session,
        name="Alex Smith",
        email="alex.smith@example.com",
        phone="000-000-0000",
        skills=["Python", "SQL", "Pandas"],
        education=[{"school": "TU Berlin", "major": "Data Science", "degree": "BSc"}],
        experience=[
            {
                "company": "Acme Corp",
                "title": "Lab Assistant",
                "dates": "Summer 2024",
                "bullets": ["Pandas notebook project"],
            }
        ],
    )
    return profile


@pytest.fixture
def sample_score():
    def _mk(total: int = 85, **overrides: Any) -> dict[str, Any]:
        defaults: dict[str, Any] = dict(
            total_score=total,
            must_have_skills_matched=["Python"],
            must_have_skills_missing=[],
            preferred_skills_matched=["SQL"],
            experience_fit="good",
            education_fit="good",
            language_fit="great",
            location_fit="great",
            evidence=["Python data pipeline for 10k rows"],
            missing_recommendation=["Add project detail for specific requirements"],
            confidence="high",
        )
        defaults.update(overrides)
        return defaults

    return _mk


@pytest.fixture
def mocked_gemini():
    """Patch AIClient.score_job_fit to deterministic output."""
    from database.models import Job, CandidateProfile

    def _mk_score(self: Any, profile: Any, job: Any, **kwargs: Any) -> Any:
        return {
            "total_score": 75,
            "must_have_skills_matched": ["Python"],
            "must_have_skills_missing": [],
            "preferred_skills_matched": ["Pandas"],
            "experience_fit": "good",
            "education_fit": "good",
            "language_fit": "great",
            "location_fit": "great",
            "evidence": ["Profile shows Python experience"],
            "missing_recommendation": ["Show a data project"],
            "confidence": "high",
        }

    import services.ai_client as gc

    caller = mock.patch.object(
        gc.AIClient, "score_job_fit", _mk_score
    )
    caller.start()
    try:
        yield gc
    finally:
        caller.stop()


@pytest.fixture
def sample_greenhouse_payload():
    return {
        "jobs": [
            {
                "id": "101",
                "title": "Python Intern",
                "location": "Berlin",
                "updated_at": "2026-08-01T00:00:00Z",
                "content": "<p>Looking for a Python intern.</p>",
            },
            {
                "id": "102",
                "title": "Software Engineering Intern",
                "location": "Remote - UK",
                "updated_at": "2026-08-02T00:00:00Z",
                "content": "<p>Full-stack internship.</p>",
            },
            {
                "id": "103",
                "title": "Part-time Support Engineer",
                "location": "London",
                "updated_at": "2026-08-02T00:00:00Z",
                "content": "<p>Part-time internship-like role.</p>",
            },
        ]
    }


@pytest.fixture
def sample_rss_feed():
    return """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Samples</title>
    <link>https://acme.example/careers</link>
    <description>Sample postings</description>
    <item>
      <title>Python Analyst Intern</title>
      <link>https://acme.example/careers/an-2</link>
      <guid>https://acme.example/careers/an-2</guid>
      <pubDate>Tue, 05 Aug 2026 12:00:00 +0000</pubDate>
      <description>Analyst intern</description>
    </item>
    <item>
      <title>Data Intern</title>
      <link>https://acme.example/careers/d-3</link>
      <guid>https://acme.example/careers/d-3</guid>
      <description>Data-focused intern</description>
    </item>
  </channel>
</rss>"""


@pytest.fixture
def safe_httpx_mock_transport(sample_greenhouse_payload, sample_rss_feed):
    """httpx.MockTransport that fakes Greenhouse JSON + an RSS feed."""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "greenhouse.io" in url:
            return httpx.Response(200, json=sample_greenhouse_payload)
        if ".rss" in url or "feed" in url:
            return httpx.Response(
                200, text=sample_rss_feed,
                headers={"content-type": "application/rss+xml"},
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.fixture
def test_output_dir(tmp_path):
    out = tmp_path / "output"
    out.mkdir(parents=True, exist_ok=True)
    return out


@pytest.fixture
def test_data_dir(tmp_path):
    d = tmp_path / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d

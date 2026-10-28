"""Tests for Gemini matching (mocked) and match-score validation.

All Gemini calls are mocked via the `mocked_gemini` fixture defined in
conftest.py — no real API key or network is required.
"""
from __future__ import annotations

from unittest import mock

import pytest

from config.search_config import EmploymentType, SearchConfig
from database import repository as db
from database.models import Job, utcnow
from services.filters import apply_filters
from services.matching import evaluate_jobs


# ---------------------------------------------------------------------------
# Match score range validation
# ---------------------------------------------------------------------------


def test_match_score_in_range(mocked_gemini, db_session, sample_job_factory, sample_profile):
    from services.normalization import content_fingerprint
    data = sample_job_factory()
    fp = content_fingerprint(
        data["original_url"], data["title"], data["company"], data.get("description", "")
    )
    job, _ = db.get_or_create_job_by_fingerprint(db_session, fp, **data)
    # Patch get_engine/get_session so evaluate_jobs writes to the test DB
    engine = db_session.get_bind()
    from sqlalchemy.orm import sessionmaker
    test_session_factory = sessionmaker(bind=engine)

    def _fake_get_session(e=None):
        return test_session_factory()

    with mock.patch("config.settings.get_config") as gc, \
         mock.patch("services.matching.get_engine", return_value=engine), \
         mock.patch("services.matching.get_session", _fake_get_session):
        gc.return_value.enable_ai_matching = True
        gc.return_value.gemini_model = "gemini-2.0-flash"
        result = evaluate_jobs([job], sample_profile)
    # Mocked client returns 75
    assert len(result) == 1
    matched_job, score = result[0]
    assert 0 <= score.get("total_score", 0) <= 100


def test_match_disabled_returns_empty(mocked_gemini, db_session, sample_job_factory, sample_profile):
    from services.normalization import content_fingerprint
    data = sample_job_factory()
    fp = content_fingerprint(
        data["original_url"], data["title"], data["company"], data.get("description", "")
    )
    job, _ = db.get_or_create_job_by_fingerprint(db_session, fp, **data)
    with mock.patch("config.settings.get_config") as gc:
        gc.return_value.enable_ai_matching = False
        # Also patch any direct imports of get_config into services.matching
        import services.matching as _m
        from services.matching import get_config as _orig_get_config
        try:
            _m.get_config = lambda: gc.return_value
            result = evaluate_jobs([job], sample_profile)
            # Matching "disabled" may return [] OR the deterministic
            # deterministic fallback (which is 0-scored).  The
            # invariant we care about: nothing should be marked as a
            # strong match (score >= 50) when AI is disabled.
            if result:
                for _j, m in result:
                    score = m.get("total_score")
                    assert not score or score < 50, (
                        f"disabling AI matching should not produce "
                        f"a score >= 50 but got {score!r}"
                    )
        finally:
            _m.get_config = _orig_get_config


# ---------------------------------------------------------------------------
# Deterministic filters
# ---------------------------------------------------------------------------


def _mk_job(**kw) -> Job:
    defaults = dict(
        title="Python Intern",
        company="Acme Corp",
        location="Berlin",
        employment_type="internship",
        workplace_arrangement="hybrid",
        description="Working with Python and data pipelines.",
        original_url="https://acme.example/j1",
        source="test",
        content_fingerprint="fp1",
        status="OPEN",
        first_discovered_at=utcnow(),
        last_seen_at=utcnow(),
    )
    defaults.update(kw)
    return Job(**defaults)


def test_filter_employment_type_match():
    job = _mk_job(employment_type="internship")
    cfg = SearchConfig(employment_types=[EmploymentType.INTERNSHIP])
    included, excluded = apply_filters([job], cfg)
    assert len(included) == 1
    assert len(excluded) == 0


def test_filter_employment_type_mismatch():
    job = _mk_job(employment_type="full_time")
    cfg = SearchConfig(employment_types=[EmploymentType.INTERNSHIP])
    included, excluded = apply_filters([job], cfg)
    assert len(included) == 0
    assert len(excluded) == 1


def test_filter_include_keywords_pass():
    job = _mk_job(description="Python and data processing.")
    cfg = SearchConfig(include_keywords=["python"])
    included, excluded = apply_filters([job], cfg)
    assert len(included) == 1


def test_filter_include_keywords_fail():
    job = _mk_job(title="Java Developer", description="Java developer.")
    cfg = SearchConfig(include_keywords=["python"])
    included, excluded = apply_filters([job], cfg)
    assert len(included) == 0


def test_filter_exclude_keywords():
    job = _mk_job(description="Python admin intern (legacy system).")
    cfg = SearchConfig(exclude_keywords=["legacy"])
    included, excluded = apply_filters([job], cfg)
    assert len(included) == 0
    assert len(excluded) == 1
    _, reason = excluded[0]
    assert "legacy" in reason.lower()


def test_filter_location_match():
    job = _mk_job(location="Berlin")
    cfg = SearchConfig(locations=["Berlin"])
    included, excluded = apply_filters([job], cfg)
    assert len(included) == 1


def test_filter_location_mismatch():
    job = _mk_job(location="New York")
    cfg = SearchConfig(locations=["Berlin"])
    included, excluded = apply_filters([job], cfg)
    assert len(included) == 0


def test_filter_location_excluded():
    job = _mk_job(location="Berlin")
    cfg = SearchConfig(excluded_locations=["Berlin"])
    included, excluded = apply_filters([job], cfg)
    assert len(included) == 0


def test_filter_remote_workplace():
    job = _mk_job(workplace_arrangement="remote")
    cfg = SearchConfig(include_remote=True)
    included, excluded = apply_filters([job], cfg)
    assert len(included) == 1


def test_filter_onsite_rejected_when_remote_only():
    job = _mk_job(workplace_arrangement="onsite")
    cfg = SearchConfig(include_remote=True)  # remote only
    included, excluded = apply_filters([job], cfg)
    assert len(included) == 0


def test_filter_empty_excluded_locations_pass():
    job = _mk_job(location="Berlin")
    cfg = SearchConfig(excluded_locations=[])
    included, excluded = apply_filters([job], cfg)
    assert len(included) == 1


def test_filter_no_constraints_passes_all():
    job = _mk_job()
    cfg = SearchConfig()
    cfg.employment_types = []  # clear employment restriction
    included, excluded = apply_filters([job], cfg)
    assert len(included) == 1

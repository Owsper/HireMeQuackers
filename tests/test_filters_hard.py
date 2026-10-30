"""Hard-constraint filter tests: AI scores MUST NOT override deterministic
title/location hard gates.

These tests use a mocked AIClient that returns a very high AI score
(total_score=100) for a job in the WRONG location, and assert that the
job is still excluded by the filter layer.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from config.search_config import EmploymentType, SearchConfig
from config.search_intent import build_intent
from database import repository as db
from database.models import Job
from services.filters import apply_filters


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def base_config() -> SearchConfig:
    return SearchConfig(
        primary_title="Python Developer",
        locations=["Berlin"],
        include_remote=False,
        include_hybrid=True,
        employment_types=[EmploymentType.INTERNSHIP, EmploymentType.FULL_TIME],
    )


def _make_job(db_session, **kwargs: Any) -> Job:
    defaults = dict(
        title="Python Developer Intern",
        company="Acme Corp",
        location="Berlin, Germany",
        country="Germany",
        employment_type="internship",
        workplace_arrangement="hybrid",
        description="Python developer role. You will write Python code.",
        required_skills=["python"],
        preferred_skills=[],
        original_url="https://example.com/jobs/acme-pydev",
        application_url="https://example.com/apply/acme-pydev",
        source="acme",
        content_fingerprint="fp-acme-pydev",
    )
    defaults.update(kwargs)
    return db.create_job(db_session, **defaults)


# ---------------------------------------------------------------------------
# Title hard gate
# ---------------------------------------------------------------------------

class TestTitleHardGate:
    def test_exact_title_passes(self, db_session, base_config):
        job = _make_job(db_session, title="Python Developer Intern",
                        location="Berlin, Germany")
        intent = build_intent(base_config)
        matched, excluded = apply_filters([job], base_config, intent=intent)
        assert len(matched) == 1
        assert job in matched

    def test_wildly_different_title_excluded(self, db_session, base_config):
        job = _make_job(db_session, title="Marketing Manager",
                        location="Berlin, Germany",
                        description="We need a great marketing person.")
        intent = build_intent(base_config)
        matched, excluded = apply_filters([job], base_config, intent=intent)
        assert len(matched) == 0
        assert len(excluded) == 1
        reason = excluded[0][1]
        assert "title" in reason.lower()

    def test_frontend_vs_backend_excluded(self, db_session):
        cfg = SearchConfig(
            primary_title="Backend Developer",
            employment_types=[EmploymentType.INTERNSHIP, EmploymentType.FULL_TIME],
        )
        job = _make_job(db_session, title="Frontend Developer",
                        location="Berlin, Germany")
        intent = build_intent(cfg)
        matched, excluded = apply_filters([job], cfg, intent=intent)
        assert len(matched) == 0


# ---------------------------------------------------------------------------
# Location hard gate
# ---------------------------------------------------------------------------

class TestLocationHardGate:
    def test_berlin_job_passes_for_berlin_intent(self, db_session, base_config):
        job = _make_job(db_session, title="Python Developer",
                        location="Berlin, Germany")
        intent = build_intent(base_config)
        matched, _ = apply_filters([job], base_config, intent=intent)
        assert len(matched) == 1

    def test_london_job_excluded_for_berlin_intent(self, db_session, base_config):
        job = _make_job(db_session, title="Python Developer",
                        location="London, UK")
        intent = build_intent(base_config)
        matched, excluded = apply_filters([job], base_config, intent=intent)
        assert len(matched) == 0
        assert len(excluded) == 1

    def test_remote_germany_not_berlin(self, db_session, base_config):
        """'Remote - Germany' must not pass a 'Berlin' city constraint."""
        job = _make_job(db_session, title="Python Developer",
                        location="Remote - Germany",
                        workplace_arrangement="remote")
        cfg = SearchConfig(
            primary_title="Python Developer",
            locations=["Berlin"],
            include_remote=True,
            employment_types=[EmploymentType.INTERNSHIP, EmploymentType.FULL_TIME],
        )
        intent = build_intent(cfg)
        matched, excluded = apply_filters([job], cfg, intent=intent)
        assert len(matched) == 0


# ---------------------------------------------------------------------------
# AI score CANNOT override hard filter
# ---------------------------------------------------------------------------

class TestAIOverrideBlocked:
    def test_high_ai_score_does_not_override_location_mismatch(self,
                                                                db_session,
                                                                base_config,
                                                                monkeypatch):
        """A job in New York with a 100 AI score is still excluded
        when the user asked for Berlin."""
        import services.ai_client as gc

        fake_result = {
            "total_score": 100,
            "must_have_skills_matched": ["Python"],
            "must_have_skills_missing": [],
            "preferred_skills_matched": [],
            "experience_fit": "great",
            "education_fit": "great",
            "language_fit": "great",
            "location_fit": "great",   # AI claims location fits (wrongly)
            "evidence": ["claim: perfect fit"],
            "missing_recommendation": [],
            "confidence": "high",
            "title_relevance": "exact",
            "location_compatibility": "match",  # AI also wrong here
            "search_relevance_score": 100,
            "ai_model": "mocked-gemini",
        }

        def _fake_score(self, profile, job, **kw):
            return fake_result

        monkeypatch.setattr(gc.AIClient, "score_job_fit", _fake_score)

        job = _make_job(db_session, title="Python Developer",
                        location="New York, US")
        intent = build_intent(base_config)
        matched, excluded = apply_filters([job], base_config, intent=intent)

        # The location hard gate in filters.py runs BEFORE any AI call.
        # The job must be excluded regardless of the AI score.
        assert len(matched) == 0, (
            "Job in New York should be excluded for Berlin intent; "
            "AI score must not override the deterministic location filter"
        )
        assert len(excluded) == 1

    def test_high_ai_score_does_not_override_title_mismatch(self,
                                                             db_session,
                                                             base_config,
                                                             monkeypatch):
        import services.ai_client as gc

        fake_result = {
            "total_score": 100,
            "must_have_skills_matched": ["Python"],
            "must_have_skills_missing": [],
            "preferred_skills_matched": [],
            "experience_fit": "great",
            "education_fit": "great",
            "language_fit": "great",
            "location_fit": "great",
            "evidence": ["perfect"],
            "missing_recommendation": [],
            "confidence": "high",
            "title_relevance": "exact",
            "location_compatibility": "match",
            "search_relevance_score": 100,
            "ai_model": "mocked-gemini",
        }

        def _fake_score(self, profile, job, **kw):
            return fake_result

        monkeypatch.setattr(gc.AIClient, "score_job_fit", _fake_score)

        job = _make_job(db_session, title="Marketing Manager",
                        location="Berlin, Germany")
        intent = build_intent(base_config)
        matched, excluded = apply_filters([job], base_config, intent=intent)
        assert len(matched) == 0


# ---------------------------------------------------------------------------
# related-roles opt-in
# ---------------------------------------------------------------------------

class TestRelatedRoles:
    def test_related_excluded_by_default(self, db_session):
        """Frontend Developer vs Frontend Engineer: same role family,
        but NOT a synonym — should be 'related', excluded by default."""
        cfg = SearchConfig(
            primary_title="Frontend Engineer",
            locations=["Berlin"],
            include_related_roles=False,
            employment_types=[EmploymentType.INTERNSHIP, EmploymentType.FULL_TIME],
        )
        job = _make_job(db_session, title="Frontend Developer",
                        location="Berlin, Germany")
        intent = build_intent(cfg, include_related_titles=False)
        matched, _ = apply_filters([job], cfg, intent=intent)
        assert len(matched) == 0

    def test_related_included_when_opted_in(self, db_session):
        cfg = SearchConfig(
            primary_title="Frontend Engineer",
            locations=["Berlin"],
            include_related_roles=True,
            employment_types=[EmploymentType.INTERNSHIP, EmploymentType.FULL_TIME],
        )
        job = _make_job(db_session, title="Frontend Developer",
                        location="Berlin, Germany")
        intent = build_intent(cfg, include_related_titles=True)
        matched, _ = apply_filters([job], cfg, intent=intent)
        assert len(matched) == 1


# ---------------------------------------------------------------------------
# Legacy path: intent=None keeps old behavior
# ---------------------------------------------------------------------------

class TestLegacyNoIntent:
    def test_legacy_blob_keyword_match_still_works(self, db_session):
        cfg = SearchConfig(
            include_keywords=["Python"],
            locations=["Berlin"],
            employment_types=[EmploymentType.INTERNSHIP, EmploymentType.FULL_TIME],
        )
        job = _make_job(db_session, title="Data Analyst Intern",
                        location="Berlin, Germany",
                        description="Works with Python for data analysis.")
        matched, excluded = apply_filters([job], cfg, intent=None)
        # Legacy: keyword "python" is in description/blob -> passes
        assert len(matched) == 1

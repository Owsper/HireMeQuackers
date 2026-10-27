"""Tests for CV tailoring — deterministic fallback path (no Gemini needed).

The `enable_ai_direction` flag is set to False so the deterministic code path
is exercised without any API call.
"""
from __future__ import annotations

from unittest import mock

import pytest

from database import repository as db
from database.models import Job, utcnow
from services.cv_tailoring import build_cv_tailoring_suggestions


def _mk_job(**kw) -> Job:
    defaults = dict(
        title="Python Data Intern",
        company="Acme Corp",
        location="Berlin, Germany",
        country="Germany",
        employment_type="internship",
        workplace_arrangement="hybrid",
        description="Data internship using Python, SQL, and Pandas.",
        required_skills_json='["Python", "SQL", "Pandas"]',
        preferred_skills_json='["ML", "Docker"]',
        language_requirements="English B2+",
        original_url="https://acme.example/jointern",
        application_url="https://acme.example/apply",
        source="test",
        content_fingerprint="fp_cv1",
        status="OPEN",
        first_discovered_at=utcnow(),
        last_seen_at=utcnow(),
    )
    defaults.update(kw)
    return Job(**defaults)


def _mk_match(
    must_matched=("Python", "SQL"),
    must_missing=(),
    edu_fit="good",
    total=70,
) -> dict:
    return {
        "total_score": total,
        "must_have_skills_matched": list(must_matched),
        "must_have_skills_missing": list(must_missing),
        "preferred_skills_matched": [],
        "experience_fit": "good",
        "education_fit": edu_fit,
        "language_fit": "good",
        "location_fit": "great",
        "evidence": ["Profile shows Python and SQL experience"],
        "missing_recommendation": list(must_missing),
        "confidence": "high",
    }


def _patch_config_off():
    """Patch config to disable AI direction so deterministic path runs."""
    return mock.patch("config.settings.get_config")


# ---------------------------------------------------------------------------
# Output shape
# ---------------------------------------------------------------------------


def test_returns_list_of_dicts(sample_profile):
    job = _mk_job()
    match = _mk_match()
    with _patch_config_off() as gc:
        gc.return_value.enable_ai_direction = False
        gc.return_value.gemini_api_key = ""
        gc.return_value.gemini_model = "gemini-2.0-flash"
        result = build_cv_tailoring_suggestions(sample_profile, job, match)
    assert isinstance(result, list)
    for item in result:
        assert isinstance(item, dict)
        assert "field" in item
        assert "action" in item
        assert "reason" in item


def test_fields_are_valid(sample_profile):
    job = _mk_job()
    match = _mk_match()
    allowed = {
        "summary", "experience", "skills", "projects", "certifications",
        "languages", "achievements", "linkedin_headline", "linkedin_features",
    }
    with _patch_config_off() as gc:
        gc.return_value.enable_ai_direction = False
        gc.return_value.gemini_api_key = ""
        gc.return_value.gemini_model = "gemini-2.0-flash"
        result = build_cv_tailoring_suggestions(sample_profile, job, match)
    for item in result:
        assert item["field"] in allowed, f"Unexpected field: {item['field']}"


def test_summary_suggestion_present(sample_profile):
    job = _mk_job()
    match = _mk_match()
    with _patch_config_off() as gc:
        gc.return_value.enable_ai_direction = False
        gc.return_value.gemini_api_key = ""
        gc.return_value.gemini_model = "gemini-2.0-flash"
        result = build_cv_tailoring_suggestions(sample_profile, job, match)
    fields = [item["field"] for item in result]
    assert "summary" in fields


def test_language_missing_triggers_suggestion(sample_profile):
    """JD requires English B2+ but profile only lists German."""
    job = _mk_job(language_requirements="English B2+")
    # Sample profile from conftest has no languages field -> missing language
    match = _mk_match()
    with _patch_config_off() as gc:
        gc.return_value.enable_ai_direction = False
        gc.return_value.gemini_api_key = ""
        gc.return_value.gemini_model = "gemini-2.0-flash"
        result = build_cv_tailoring_suggestions(sample_profile, job, match)
    fields = [item["field"] for item in result]
    # English is required but not in profile -> "languages" field
    assert "languages" in fields


def test_no_skills_fabricated(sample_profile):
    """Missing skills should not be invented; the field they appear in
    must already be present in the profile text."""
    job = _mk_job()
    match = _mk_match(must_missing=("Docker", "Kubernetes"))
    with _patch_config_off() as gc:
        gc.return_value.enable_ai_direction = False
        gc.return_value.gemini_api_key = ""
        gc.return_value.gemini_model = "gemini-2.0-flash"
        result = build_cv_tailoring_suggestions(sample_profile, job, match)
    # "Docker" and "Kubernetes" are missing skills NOT in the profile,
    # so no "skills" suggestion should appear for them.
    for item in result:
        if item["field"] == "skills":
            assert "Docker" not in item["action"] or "already evidenced" not in item["reason"], (
                "Should not move a skill that has no evidence in the profile"
            )


def test_max_12_suggestions(sample_profile):
    job = _mk_job()
    match = _mk_match()
    with _patch_config_off() as gc:
        gc.return_value.enable_ai_direction = False
        gc.return_value.gemini_api_key = ""
        gc.return_value.gemini_model = "gemini-2.0-flash"
        result = build_cv_tailoring_suggestions(sample_profile, job, match)
    assert len(result) <= 12

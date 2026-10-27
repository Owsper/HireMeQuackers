"""Tests for deduplication: content fingerprint, URL canonicalization,
primary_hard_match, and secondary_similarity.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from database.models import Job, utcnow
from services.deduplication import (
    is_expiring,
    is_stale,
    primary_hard_match,
    secondary_similarity,
)
from services.normalization import canonical_url, content_fingerprint


# ---------------------------------------------------------------------------
# canonical_url
# ---------------------------------------------------------------------------


def test_canonical_url_lowercases_scheme_and_host():
    assert canonical_url("HTTPS://Example.COM/Path") == "https://example.com/Path"


def test_canonical_url_strips_fragment():
    assert canonical_url("https://example.com/page#section") == "https://example.com/page"


def test_canonical_url_keeps_query():
    assert canonical_url("https://example.com/page?a=1&b=2") == "https://example.com/page?a=1&b=2"


def test_canonical_url_empty():
    assert canonical_url("") == ""
    assert canonical_url(None) == ""


def test_canonical_url_strips_trailing_slash():
    assert canonical_url("https://example.com/") == "https://example.com"


# ---------------------------------------------------------------------------
# content_fingerprint
# ---------------------------------------------------------------------------


def test_fingerprint_is_deterministic():
    a = content_fingerprint("https://a.com/j", "Python Intern", "Acme", "desc")
    b = content_fingerprint("https://a.com/j", "Python Intern", "Acme", "desc")
    assert a == b
    assert len(a) == 64


def test_fingerprint_differs_for_different_source_tag():
    # Different URL -> different fingerprint
    a = content_fingerprint("https://a.com/1", "Intern", "Co", "Desc")
    b = content_fingerprint("https://b.com/2", "Intern", "Co", "Desc")
    assert a != b


def test_fingerprint_case_insensitive_for_title():
    a = content_fingerprint("https://a.com/1", "Python Intern", "Acme", "desc")
    b = content_fingerprint("https://a.com/1", "python intern", "acme", "DESC")
    assert a == b


# ---------------------------------------------------------------------------
# primary_hard_match
# ---------------------------------------------------------------------------


def _mk_job(**kw) -> Job:
    defaults = dict(
        title="Python Intern",
        company="Acme Corp",
        location="Berlin",
        employment_type="internship",
        workplace_arrangement="hybrid",
        description="desc",
        original_url="https://acme.example/careers/di-1",
        source="test",
        content_fingerprint="fp123",
        status="OPEN",
        first_discovered_at=utcnow(),
        last_seen_at=utcnow(),
    )
    defaults.update(kw)
    return Job(**defaults)


def test_primary_hard_match_same_fp():
    job = _mk_job(content_fingerprint="fp123")
    candidate = {"content_fingerprint": "fp123"}
    assert primary_hard_match(job, candidate) is True


def test_primary_hard_match_different_fp():
    job = _mk_job(content_fingerprint="fp123")
    candidate = {"content_fingerprint": "fp999"}
    assert primary_hard_match(job, candidate) is False


def test_primary_hard_match_no_fp():
    job = _mk_job(content_fingerprint="")
    candidate = {"content_fingerprint": "fp999"}
    assert primary_hard_match(job, candidate) is False


# ---------------------------------------------------------------------------
# secondary_similarity
# ---------------------------------------------------------------------------


def test_secondary_same_company_and_title():
    existing = _mk_job(title="Python Data Intern", company="Acme Corp")
    existing.id = 99999  # set a non-None id so the skip check doesn't trigger
    candidate = {
        "title": "Python Data Intern",
        "company": "Acme Corp",
        "workplace_arrangement": "hybrid",
        "original_url": "https://acme.example/careers/di-1",
    }
    result = secondary_similarity(candidate, [existing])
    assert result is not None
    matched_job, score = result
    assert matched_job.title == "Python Data Intern"
    assert score >= 0.75


def test_secondary_differs_other_company():
    existing = _mk_job(title="Python Data Intern", company="Acme Corp")
    candidate = {
        "title": "Python Data Intern",
        "company": "Totally Different Inc",
        "workplace_arrangement": "hybrid",
        "original_url": "https://other.example/careers/di-1",
    }
    result = secondary_similarity(candidate, [existing])
    # Different companies with different company tokens -> no strong match
    assert result is None


def test_secondary_empty_job_list():
    candidate = {"title": "Python", "company": "Acme", "original_url": "https://a.com"}
    result = secondary_similarity(candidate, [])
    assert result is None


# ---------------------------------------------------------------------------
# stale / expiring
# ---------------------------------------------------------------------------


def test_is_stale_false_when_recent():
    job = _mk_job(first_discovered_at=utcnow() - timedelta(days=10), status="OPEN")
    assert is_stale(job) is False


def test_is_stale_true_when_old():
    job = _mk_job(first_discovered_at=utcnow() - timedelta(days=100), status="OPEN")
    assert is_stale(job) is True


def test_is_stale_false_when_closed():
    job = _mk_job(first_discovered_at=utcnow() - timedelta(days=200), status="CLOSED")
    assert is_stale(job) is False


def test_is_expiring_within_7_days():
    job = _mk_job(closing_at=utcnow() + timedelta(days=3), status="OPEN")
    assert is_expiring(job, ahead=7) is True


def test_is_expiring_not_when_far():
    job = _mk_job(closing_at=utcnow() + timedelta(days=30), status="OPEN")
    assert is_expiring(job, ahead=7) is False


def test_is_expiring_none_closing():
    job = _mk_job(closing_at=None, status="OPEN")
    assert is_expiring(job, ahead=7) is False

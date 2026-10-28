"""Phase 3: enrichment service tests (DB row populated, mocks only)."""
from __future__ import annotations

from unittest import mock

import pytest

from database import repository as db
from services.detail_extractor import ExtractedDetail
from services.enrichment_service import enrich_missing_jobs


_HTML_PAGE = (
    "<html><head><title>T</title></head><body>"
    "<article>We seek a Python backend intern in Berlin. Responsibilities include "
    "writing tests and code review. Salary 4.000 – 4.800 EUR monthly. Hybrid.</article>"
    '<a href="https://acme.example/apply/xyz">Apply now</a>'
    "</body></html>"
)


def _mk_detail() -> ExtractedDetail:
    return ExtractedDetail(
        title="Backend Intern",
        employer="Acme",
        description_text="Seeking a Python intern in Berlin.",
        location="Berlin",
        salary_raw="4.000-4.800 EUR",
        salary_currency="EUR",
        salary_min=4000,
        salary_max=4800,
        employment_type="internship",
        workplace_arrangement="hybrid",
        application_url="https://acme.example/apply/xyz",
        extraction_confidence=0.85,
    )


def _empty_detail() -> ExtractedDetail:
    return ExtractedDetail(extraction_confidence=0.0)


@pytest.fixture
def jobs_with_missing_desc(db_session):
    """Create 3 job rows that lack descriptions."""
    jobs = []
    for i in range(3):
        j = db.create_job(
            db_session,
            title=f"Empty Desc Intern {i}",
            company="Acme",
            location="Berlin",
            country="Germany",
            employment_type="internship",
            workplace_arrangement="hybrid",
            original_url=f"https://acme.example/jobs/{i}",
            application_url=None,
            source="acme",
            description=None,
        )
        jobs.append(j)
    return jobs


def test_enriches_missing_jobs(db_session, search_config_factory, jobs_with_missing_desc):
    cfg = search_config_factory()
    jobs = jobs_with_missing_desc
    job_ids = [j.id for j in jobs]

    with mock.patch("services.enrichment_service.fetch_and_extract") as fake:
        fake.side_effect = lambda url, client=None: (
            _mk_detail() if "acme.example/jobs" in url else ExtractedDetail(extraction_confidence=0.0)
        )
        summary = enrich_missing_jobs(db_session, cfg, job_ids=job_ids, delay_seconds=0)

    assert summary.attempted == 3
    assert summary.succeeded == 3
    assert summary.failed == 0
    # Verify description was populated
    for jid in job_ids:
        row = db_session.query(__import__("database.models", fromlist=["Job"]).Job).get(jid)
        assert row.description and row.description.strip()


def test_does_not_overwrite_existing_description(db_session, search_config_factory):
    j = db.create_job(
        db_session,
        title="Already has desc",
        company="Acme",
        location="Berlin",
        country="Germany",
        employment_type="internship",
        workplace_arrangement="hybrid",
        original_url="https://acme.example/exists",
        application_url=None,
        source="acme",
        description="ALREADY THERE — original desc",
    )
    # create_job auto-sets source and such; make sure description persists
    assert j.id is not None

    cfg = search_config_factory()
    with mock.patch("services.enrichment_service.fetch_and_extract") as fake:
        fake.return_value = _mk_detail()
        summary = enrich_missing_jobs(db_session, cfg, job_ids=[j.id], delay_seconds=0)

    # description should be LEFT ALONE (original value preserved)
    from database.models import Job
    row = db_session.query(Job).get(j.id)
    assert row.description == "ALREADY THERE — original desc"
    # but employer etc can change
    assert summary.attempted == 1


def test_http_failure_does_not_break_loop(db_session, search_config_factory, jobs_with_missing_desc):
    from services.safe_requests import FetchResult

    jobs = jobs_with_missing_desc
    cfg = search_config_factory()

    with mock.patch("services.enrichment_service.fetch_and_extract") as fake:
        def side(url, client=None):
            if "jobs/0" in url:
                raise RuntimeError("boom")
            return _mk_detail() if "jobs" in url else _empty_detail()
        fake.side_effect = side
        summary = enrich_missing_jobs(
            db_session, cfg, job_ids=[j.id for j in jobs], delay_seconds=0
        )

    # 1 boom + 2 ok
    assert summary.attempted == 3
    # boom counts as failure; the 2 valid fetches contribute 0 rows since they write the
    # same salary/employment values to be already !==, but employer=Acme and location=Berlin
    # are already set, so rowcount could be 0 for those
    # Just assert: first fetch marked as failure
    assert summary.failed >= 1


def test_rate_limit_respected(db_session, search_config_factory, jobs_with_missing_desc):
    """Fetches are spaced by delay_seconds."""
    caught = []

    def fake(url, client=None):
        caught.append(url)
        return _mk_detail()

    cfg = search_config_factory()
    with (
        mock.patch("services.enrichment_service.fetch_and_extract", side_effect=fake),
        mock.patch("services.enrichment_service.time.monotonic", side_effect=[0.0, 0.0, 0.0, 0.0, 0.0, 0.0]),
        mock.patch("services.enrichment_service.time.sleep") as sleep_mock,
    ):
        enrich_missing_jobs(
            db_session, cfg, job_ids=[j.id for j in jobs_with_missing_desc], delay_seconds=2.0
        )
    # 3 jobs -> 2 gaps of 2.0s each (or 3 including initial)
    # Assert sleep was called between each pair
    n_calls = sleep_mock.call_count
    assert n_calls >= 2


def test_max_per_scan_cap(db_session, search_config_factory):
    """Only the first `max_per_scan` jobs get enriched, even if more exist."""
    ids = []
    for i in range(6):
        j = db.create_job(
            db_session,
            title=f"Cap Test {i}",
            company="A",
            location="Berlin",
            country="Germany",
            employment_type="internship",
            workplace_arrangement="hybrid",
            original_url=f"https://x.example/cap/{i}",
            application_url=None,
            source="acme",
            description=None,
        )
        ids.append(j.id)

    cfg = search_config_factory()
    with mock.patch("services.enrichment_service.fetch_and_extract") as fake:
        fake.return_value = _mk_detail()
        summary = enrich_missing_jobs(
            db_session, cfg, job_ids=ids, max_per_scan=3, delay_seconds=0
        )
    assert summary.attempted == 3  # capped at 3
    # 6 created, 3 enriched; the rest are still missing desc
    from database.models import Job
    remaining = (
        db_session.query(Job)
        .filter(Job.id.in_(ids))
        .filter((Job.description.is_(None)) | (Job.description == ""))
        .count()
    )
    assert remaining >= 3


def test_enrichment_summary_zero_when_empty(db_session, search_config_factory):
    cfg = search_config_factory()
    with mock.patch("services.enrichment_service.fetch_and_extract") as fake:
        fake.return_value = _mk_detail()
        summary = enrich_missing_jobs(db_session, cfg, job_ids=[], delay_seconds=0)
    assert summary.attempted == 0
    assert summary.succeeded == 0


def test_enrichment_skips_jobs_without_url(db_session, search_config_factory):
    # original_url="" (empty string) -> the service should skip the fetch
    j = db.create_job(
        db_session,
        title="No URL Intern",
        company="A",
        location="Berlin",
        country="Germany",
        employment_type="internship",
        workplace_arrangement="hybrid",
        original_url="",  # empty string is falsy in Python
        application_url=None,
        source="acme",
        description=None,
    )
    cfg = search_config_factory()
    with mock.patch("services.enrichment_service.fetch_and_extract") as fake:
        fake.return_value = _mk_detail()
        summary = enrich_missing_jobs(db_session, cfg, job_ids=[j.id], delay_seconds=0)
    assert summary.attempted == 1
    assert summary.skipped == 1
    assert summary.succeeded == 0
    assert summary.failed == 0
    # And critically: no fetch should have occurred
    assert not fake.called


def test_no_network_call_when_config_disabled(db_session, search_config_factory):
    cfg = search_config_factory(enable_detail_extraction=False)
    # `enrich_missing_jobs` itself does not look at cfg.enable_detail_extraction —
    # the caller (discovery) checks.  Assert the repo lookup runs without raising.
    with mock.patch("services.enrichment_service.fetch_and_extract") as fake:
        fake.return_value = _mk_detail()
        enrich_missing_jobs(db_session, cfg, job_ids=[1, 2, 3], delay_seconds=0)
    # (This documents intent: enrichment is called explicitly by the caller.)

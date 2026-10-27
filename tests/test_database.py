"""Tests for SQLite initialization and basic CRUD operations."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from config.search_config import EmploymentType, SearchConfig
from database import repository as db
from database.models import (
    Application,
    CandidateProfile,
    GeneratedDocument,
    Job,
    JobMatch,
    ScanRecord,
    SourceHealthRecord,
)
from services.normalization import content_fingerprint


# ---------------------------------------------------------------------------
# Schema initialization
# ---------------------------------------------------------------------------


def test_db_table_exists(db_session):
    """All main tables are present after init_db."""
    from database.models import Base
    from sqlalchemy import inspect

    engine = db_session.get_bind()
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    expected = {
        "jobs",
        "candidate_profiles",
        "job_matches",
        "scan_records",
        "source_health",
        "applications",
        "generated_documents",
        "settings_rows",
        "search_profiles",
        "ai_usage",
        "schema_version",
    }
    missing = expected - tables
    assert not missing, f"Missing tables: {missing}"


# ---------------------------------------------------------------------------
# Job CRUD
# ---------------------------------------------------------------------------


def _fp_for(data: dict) -> str:
    return content_fingerprint(
        data.get("original_url", ""),
        data.get("title", ""),
        data.get("company", ""),
        data.get("description", ""),
    )


def test_job_create_and_get(db_session, sample_job_factory):
    from services.normalization import content_fingerprint
    data = sample_job_factory()
    fp = content_fingerprint(
        data["original_url"], data["title"], data["company"], data["description"]
    )
    job, created = db.get_or_create_job_by_fingerprint(db_session, fp, **data)
    assert created is True
    assert job.id is not None
    fetched = db.get_job(db_session, job.id)
    assert fetched is not None
    assert fetched.title == "Python Data Intern"
    assert fetched.company == "Acme Corp"


def test_job_dedup_hard_match(db_session, sample_job_factory):
    from services.normalization import content_fingerprint
    data = sample_job_factory(
        original_url="https://acme.example/careers/dedup-test",
        title="Dedup Test Intern",
    )
    fp = content_fingerprint(
        data["original_url"], data["title"], data["company"], data["description"]
    )
    job1, created1 = db.get_or_create_job_by_fingerprint(db_session, fp, **data)
    job2, created2 = db.get_or_create_job_by_fingerprint(db_session, fp, **data)
    assert created1 is True
    assert created2 is False
    assert job2.id == job1.id


def test_job_fields_updated_on_second_scan(db_session, sample_job_factory):
    from services.normalization import content_fingerprint
    data = sample_job_factory(description="First description")
    fp = content_fingerprint(
        data["original_url"], data["title"], data["company"], "First description"
    )
    job1, _ = db.get_or_create_job_by_fingerprint(db_session, fp, **data)
    data2 = dict(data, description="Updated description")
    job2, created = db.get_or_create_job_by_fingerprint(db_session, fp, **data2)
    assert created is False
    db.update_job_fields(db_session, job2, description="Updated description")
    assert job2.description == "Updated description"


def test_list_jobs(db_session, sample_job_factory):
    from services.normalization import content_fingerprint
    for i in range(5):
        d = sample_job_factory(title=f"Job {i}", original_url=f"https://acme.example/j{i}")
        fp = content_fingerprint(d["original_url"], d["title"], d["company"], d.get("description", ""))
        db.get_or_create_job_by_fingerprint(db_session, fp, **d)
    # Count only the 5 jobs we just inserted (titles "Job 0".."Job 4")
    from database.models import Job
    db_session.expire_all()
    jobs = (
        db_session.query(Job)
        .filter(Job.title.in_([f"Job {i}" for i in range(5)]))
        .all()
    )
    assert len(jobs) == 5


# ---------------------------------------------------------------------------
# Candidate profile
# ---------------------------------------------------------------------------


def test_profile_create(db_session, sample_profile):
    assert sample_profile is not None
    assert sample_profile.name == "Alex Smith"
    assert sample_profile.profile_version == 1
    assert sample_profile.skills == ["Python", "SQL", "Pandas"]


def test_profile_get_active(db_session, sample_profile):
    active = db.get_active_candidate_profile(db_session)
    assert active is None  # sample_profile.active defaults to False


def _make_job(db_session, **overrides):
    data = dict(
        title="Python Data Intern",
        company="Acme Corp",
        location="Berlin",
        country="Germany",
        employment_type="internship",
        workplace_arrangement="hybrid",
        description="Python | data | ML role about...",
        required_skills_json=None,
        preferred_skills_json=None,
        original_url="https://acme.example/careers/di-1",
        application_url="https://acme.example/apply/di-1",
        source="acme",
        **overrides,
    )
    from services.normalization import content_fingerprint
    fp = content_fingerprint(
        data["original_url"], data["title"], data["company"], data.get("description", "")
    )
    job, _ = db.get_or_create_job_by_fingerprint(db_session, fp, **data)
    return job


def test_match_create_and_get(db_session, sample_profile):
    job = _make_job(db_session)
    match = db.create_match(
        db_session,
        job_id=job.id,
        profile_version=1,
        match_score=82.0,
        score={"total_score": 82},
        evidence=["Python experience"],
        missing=["Django"],
        ai_model="gemini-2.0-flash",
    )
    assert match.id is not None
    fetched = db.get_match_for_job(db_session, job.id, profile_version=1)
    assert fetched is not None
    assert fetched.match_score == 82.0


def test_match_upsert(db_session):
    job = _make_job(db_session)
    db.create_match(db_session, job_id=job.id, profile_version=1, match_score=50.0)
    updated = db.create_match(db_session, job_id=job.id, profile_version=1, match_score=90.0)
    assert updated.match_score == 90.0
    all_matches = (
        db_session.query(JobMatch)
        .filter_by(job_id=job.id, profile_version=1)
        .all()
    )
    assert len(all_matches) == 1


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


def test_application_create(db_session):
    job = _make_job(db_session)
    app = db.create_application(db_session, job_id=job.id)
    assert app is not None
    assert app.status == "DISCOVERED"
    assert app.job_id == job.id


def test_application_unique(db_session):
    job = _make_job(db_session)
    app1 = db.create_application(db_session, job_id=job.id)
    app2 = db.create_application(db_session, job_id=job.id)
    assert app1.id == app2.id


def test_application_status_transitions(db_session):
    job = _make_job(db_session)
    app = db.create_application(db_session, job_id=job.id)
    db.transition_application_status(db_session, app, "SHORTLISTED")
    assert app.status == "SHORTLISTED"
    db.transition_application_status(db_session, app, "APPLIED")
    assert app.status == "APPLIED"


# ---------------------------------------------------------------------------
# Scan record
# ---------------------------------------------------------------------------


def test_scan_record_create(db_session):
    rec = db.create_scan_record(db_session)
    assert rec.id is not None
    assert rec.status == "running"
    assert rec.trigger == "user"
    db.finish_scan(
        db_session,
        rec,
        status="done",
        total_discovered=10,
        new_jobs=5,
        duplicates=3,
    )
    assert rec.status == "done"
    assert rec.total_discovered == 10
    assert rec.new_jobs == 5


def test_source_health_record(db_session):
    rec = db.record_source_health(
        db_session, source_name="TestSource", source_url="https://example.com"
    )
    assert rec.source_name == "TestSource"
    assert rec.is_enabled is True


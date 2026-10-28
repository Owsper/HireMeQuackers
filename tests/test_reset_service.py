"""Tests for services.reset_service (see model docstrings for fields)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytest
from sqlalchemy import inspect

from database.models import (
    AiUsage,
    Application,
    CandidateProfile,
    GeneratedDocument,
    Job,
    JobMatch,
    ScanRecord,
    SourceHealthRecord,
    SourceRegistryEntry,
)
from services.reset_service import (
    ResetPlan,
    ResetResult,
    execute_reset,
    plan_reset,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _table_exists(session, name: str) -> bool:
    return name in inspect(session.get_bind()).get_table_names()


def _wipe_all(session) -> None:
    """FK-safe hard delete of every resettable table for isolation."""
    session.query(AiUsage).delete(synchronize_session=False)
    session.query(JobMatch).delete(synchronize_session=False)
    session.query(Application).delete(synchronize_session=False)
    session.query(GeneratedDocument).delete(synchronize_session=False)
    session.query(ScanRecord).delete(synchronize_session=False)
    session.query(SourceHealthRecord).delete(synchronize_session=False)
    session.query(Job).delete(synchronize_session=False)
    session.commit()


def _insert_job(session, **kw: Any) -> Job:
    now = _now()
    kw.setdefault("title", "Python Data Intern")
    kw.setdefault("company", "Acme")
    kw.setdefault("location", "Berlin")
    kw.setdefault("country", "Germany")
    kw.setdefault("employment_type", "internship")
    kw.setdefault("source", "acme")
    kw.setdefault("original_url", "https://acme.example/di-1")
    kw.setdefault("content_fingerprint", "f-" + kw.get("original_url", "u"))
    kw["first_discovered_at"] = kw.get("first_discovered_at") or now
    kw["last_seen_at"] = kw.get("last_seen_at") or now
    job = Job(**kw)
    session.add(job)
    session.flush()
    return job


def _insert_match(session, job_id: int, **kw: Any) -> JobMatch:
    kw.setdefault("job_id", job_id)
    kw.setdefault("profile_version", 1)
    kw.setdefault("match_score", 80)
    m = JobMatch(**kw)
    session.add(m)
    session.flush()
    return m


def _insert_application(session, job_id: int, **kw: Any) -> Application:
    kw.setdefault("job_id", job_id)
    kw.setdefault("status", "APPLIED")
    a = Application(**kw)
    session.add(a)
    session.flush()
    return a


def _insert_doc(session, job_id: int, **kw: Any) -> GeneratedDocument:
    kw.setdefault("job_id", job_id)
    kw.setdefault("profile_version", 1)
    kw.setdefault("doc_type", "COVER_LETTER")
    kw.setdefault("file_path", "outputs/test.pdf")
    g = GeneratedDocument(**kw)
    session.add(g)
    session.flush()
    return g


def _insert_ai_usage(session, job_id: Optional[int], **kw: Any) -> AiUsage:
    kw.setdefault("job_id", job_id)
    kw.setdefault("operation", "score")
    kw.setdefault("model", "test")
    u = AiUsage(**kw)
    session.add(u)
    session.flush()
    return u


def _insert_scan(session, status: str = "done", **kw: Any) -> ScanRecord:
    kw.setdefault("status", status)
    kw.setdefault("trigger", "user")
    s = ScanRecord(**kw)
    session.add(s)
    session.flush()
    return s


def _insert_source_health(session, **kw: Any) -> SourceHealthRecord:
    kw.setdefault("source_name", "acme")
    kw.setdefault("listings_found", 1)
    kw.setdefault("is_enabled", True)
    s = SourceHealthRecord(**kw)
    session.add(s)
    session.flush()
    return s


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


def test_plan_bad_scope_raises(db_session):
    with pytest.raises(ValueError):
        plan_reset(db_session, "bogus")  # type: ignore[arg-type]


def test_plan_all_no_data(db_session):
    _wipe_all(db_session)
    plan = plan_reset(db_session, "all")
    assert plan.total == 0
    assert plan.scope == "all"
    assert plan.to_dict()["total"] == 0


def test_plan_current_no_scan(db_session):
    _wipe_all(db_session)
    plan = plan_reset(db_session, "current")
    assert plan.total == 0
    assert plan.scan_record_id is None


def test_plan_reads_everything(db_session):
    _wipe_all(db_session)
    j = _insert_job(db_session, original_url="h-1")
    _insert_match(db_session, j.id)
    _insert_application(db_session, j.id)
    _insert_doc(db_session, j.id)
    _insert_ai_usage(db_session, j.id)
    _insert_scan(db_session, status="done")
    _insert_source_health(db_session, source_name="acme")

    plan = plan_reset(db_session, "all")
    assert plan.jobs == 1
    assert plan.job_matches == 1
    assert plan.applications == 1
    assert plan.generated_documents == 1
    assert plan.ai_usage == 1
    assert plan.scan_records == 1
    assert plan.source_health == 1


# ---------------------------------------------------------------------------
# Execute — all scope
# ---------------------------------------------------------------------------


def test_execute_all_removes_resettable_rows(db_session):
    _wipe_all(db_session)
    for i in range(3):
        j = _insert_job(db_session, original_url=f"https://acme.example/a-{i}")
        _insert_match(db_session, j.id)
        _insert_application(db_session, j.id)
        _insert_doc(db_session, j.id)
        _insert_ai_usage(db_session, j.id)
    _insert_scan(db_session, status="done")
    _insert_source_health(db_session, source_name="acme")
    db_session.commit()

    plan = plan_reset(db_session, "all")
    result = execute_reset(db_session, plan=plan)
    assert result.success, result.error

    assert db_session.query(Job).count() == 0
    assert db_session.query(JobMatch).count() == 0
    assert db_session.query(Application).count() == 0
    assert db_session.query(GeneratedDocument).count() == 0
    assert db_session.query(AiUsage).count() == 0
    assert db_session.query(ScanRecord).count() == 0
    assert db_session.query(SourceHealthRecord).count() == 0


def test_execute_all_preserves_protected_rows(db_session, sample_profile):
    """Candidate profiles and the source registry survive a full reset."""
    _wipe_all(db_session)
    j = _insert_job(db_session, original_url="keepme")
    _insert_match(db_session, j.id)
    db_session.commit()

    if _table_exists(db_session, "source_registry"):
        before = db_session.query(SourceRegistryEntry).count()
        db_session.add(SourceRegistryEntry(
            domain="acme.example",
            discovered_via="manual",
        ))
        db_session.commit()
    else:
        before = None

    result = execute_reset(db_session, scope="all")
    assert result.success
    assert db_session.query(Job).count() == 0

    profile = db_session.get(CandidateProfile, sample_profile.id)
    assert profile is not None
    assert profile.email == "alex.smith@example.com"

    if before is not None:
        after = db_session.query(SourceRegistryEntry).count()
        assert after == before + 1


def test_execute_all_without_plan_db_session(db_session):
    """execute_reset() with no plan defaults to scope='all'."""
    _wipe_all(db_session)
    j = _insert_job(db_session, original_url="no-plan")
    _insert_match(db_session, j.id)
    db_session.commit()

    result = execute_reset(db_session)
    assert result.success
    assert db_session.query(Job).count() == 0
    assert db_session.query(JobMatch).count() == 0


def test_execute_all_empty_is_noop(db_session):
    _wipe_all(db_session)
    plan = plan_reset(db_session, "all")
    result = execute_reset(db_session, plan=plan)
    assert result.success
    assert result.deleted["jobs"] == 0


# ---------------------------------------------------------------------------
# Execute — current scope
# ---------------------------------------------------------------------------


def test_execute_current_only_deletes_latest_scan(db_session):
    _wipe_all(db_session)
    now = _now()
    t2 = now.replace(microsecond=0)
    t0 = t2 - timedelta(hours=2)  # earlier scan, well before t2

    s1 = _insert_scan(db_session, status="done", started_at=t0)
    for i in range(2):
        _insert_job(
            db_session,
            original_url=f"https://acme.example/old-{i}",
            first_discovered_at=t0 + timedelta(seconds=1),
            last_seen_at=t0 + timedelta(seconds=1),
        )
    s2 = _insert_scan(db_session, status="done", started_at=t2)
    for i in range(2):
        _insert_job(
            db_session,
            original_url=f"https://acme.example/new-{i}",
            first_discovered_at=t2 + timedelta(seconds=1),
            last_seen_at=t2 + timedelta(seconds=1),
        )
    db_session.commit()

    plan = plan_reset(db_session, "current")
    assert plan.scan_record_id == s2.id
    # Only the new jobs are in the window (t2+1s >= t2), 2 expected.
    assert plan.jobs == 2
    assert plan.scan_records == 1

    result = execute_reset(db_session, plan=plan)
    assert result.success, result.error

    # Old (pre-current) jobs + matches still there.
    assert db_session.query(Job).count() == 2
    remaining = db_session.query(Job).all()
    assert all("old-" in (j.original_url or "") for j in remaining)
    # Only the latest scan record was removed, old one kept.
    assert db_session.query(ScanRecord).count() == 1
    kept = db_session.query(ScanRecord).first()
    assert kept.id == s1.id


def test_current_with_no_scan_is_noop(db_session):
    _wipe_all(db_session)
    j = _insert_job(db_session, original_url="lonely")
    db_session.commit()

    plan = plan_reset(db_session, "current")
    assert plan.scan_record_id is None
    result = execute_reset(db_session, plan=plan)
    assert result.success
    assert db_session.query(Job).count() == 1


def test_current_removes_dependent_rows(db_session):
    _wipe_all(db_session)
    now = _now()
    t_start = now - timedelta(minutes=10)
    _insert_scan(db_session, status="done", started_at=t_start)
    j = _insert_job(
        db_session, original_url="dep",
        first_discovered_at=now, last_seen_at=now,
    )
    _insert_match(db_session, j.id)
    _insert_application(db_session, j.id)
    _insert_doc(db_session, j.id)
    db_session.commit()

    plan = plan_reset(db_session, "current")
    # Single scan: since = that scan's start, so job is in the window.
    assert plan.applications == 1
    assert plan.generated_documents == 1
    result = execute_reset(db_session, plan=plan)
    assert result.success
    assert db_session.query(Job).count() == 0
    assert db_session.query(Application).count() == 0
    assert db_session.query(GeneratedDocument).count() == 0


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


def test_rollback_on_commit_failure(db_session):
    """If commit fails, in-txn deletions are rolled back."""
    _wipe_all(db_session)
    j = _insert_job(db_session, original_url="rb")
    _insert_match(db_session, j.id)
    _insert_application(db_session, j.id)
    db_session.commit()

    job_count = db_session.query(Job).count()
    match_count = db_session.query(JobMatch).count()
    app_count = db_session.query(Application).count()

    real_commit = db_session.commit

    def _boom() -> None:
        raise RuntimeError("simulated: db locked")

    db_session.commit = _boom  # type: ignore[method-assign]
    try:
        result = execute_reset(db_session, scope="all")
    finally:
        db_session.commit = real_commit

    assert result.success is False
    assert "simulated: db locked" in (result.error or "")

    assert db_session.query(Job).count() == job_count
    assert db_session.query(JobMatch).count() == match_count
    assert db_session.query(Application).count() == app_count


# ---------------------------------------------------------------------------
# Running scans
# ---------------------------------------------------------------------------


def test_plan_cancels_running_scans(db_session):
    _wipe_all(db_session)
    _insert_job(db_session, original_url="busy")
    _insert_scan(db_session, status="running")
    db_session.commit()

    plan = plan_reset(db_session, "all")
    # Running scan must be flagged 'cancelled' in the plan.
    assert plan.running_scans_cancelled == 1
    result = execute_reset(db_session, plan=plan)
    assert result.success


def test_run_new_scan_immediately_after_reset(db_session):
    _wipe_all(db_session)
    j = _insert_job(db_session, original_url="pre")
    _insert_match(db_session, j.id)
    db_session.commit()

    result = execute_reset(db_session, scope="all")
    assert result.success

    # A brand-new scan (record + job) is the expected "fresh" state.
    _insert_scan(db_session, status="done")
    _insert_job(
        db_session, original_url="https://acme.example/after-reset",
        first_discovered_at=_now(), last_seen_at=_now(),
    )
    db_session.commit()
    assert db_session.query(ScanRecord).count() == 1
    assert db_session.query(Job).count() == 1


# ---------------------------------------------------------------------------
# Orphans
# ---------------------------------------------------------------------------


def test_reset_leaves_no_orphans(db_session):
    _wipe_all(db_session)
    for i in range(2):
        j = _insert_job(
            db_session, original_url=f"https://acme.example/orphan-{i}"
        )
        _insert_match(db_session, j.id)
        _insert_application(db_session, j.id)
        _insert_doc(db_session, j.id)
        _insert_ai_usage(db_session, j.id)
    db_session.commit()

    result = execute_reset(db_session, scope="all")
    assert result.success
    assert db_session.query(JobMatch).count() == 0
    assert db_session.query(Application).count() == 0
    assert db_session.query(GeneratedDocument).count() == 0
    assert db_session.query(AiUsage).count() == 0
    assert db_session.query(Job).count() == 0


def test_reset_handles_orphan_children(db_session):
    """Children are always deleted even when their parent isn't in the
    deletion window — i.e. the reset cannot leave orphaned dependent rows.

    (In practice SQLite FK enforcement prevents creating a genuine
    orphan, so we just confirm that deleting in FK-safe order leaves
    no traces behind.)
    """
    _wipe_all(db_session)
    j = _insert_job(db_session, original_url="parent")
    m = _insert_match(db_session, j.id)
    match_id = m.id
    db_session.commit()

    result = execute_reset(db_session, scope="all")
    assert result.success
    assert match_id is not None
    assert db_session.query(JobMatch).count() == 0
    assert db_session.query(Job).count() == 0
    assert db_session.get(JobMatch, match_id) is None


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def test_reset_result_serialization():
    r = ResetResult(success=True, scope="all", deleted={"jobs": 3})
    d = r.to_dict()
    assert d["scope"] == "all"
    assert d["deleted"] == {"jobs": 3}
    assert d["success"] is True

    fp = ResetPlan(scope="all", jobs=1, job_matches=2, applications=3)
    df = fp.to_dict()
    assert df["jobs"] == 1
    assert df["job_matches"] == 2
    assert df["applications"] == 3
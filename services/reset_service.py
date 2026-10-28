"""Safe, one-click database + search reset.

This module exposes exactly two public functions:

``plan_reset(session, scope)``  - inspect the database and return a
    ``ResetPlan`` with the number of rows that would be removed for each
    protected/resettable table, without deleting anything.

``execute_reset(session, plan, scope)`` - delete the rows listed by the
    plan in strict FK-safe order inside a single transaction.  On any
    error the whole transaction rolls back and no data is lost.

Scopes
------
``"current"``
    Delete jobs (and dependent rows) belonging to the latest scan only:
    ``jobs.first_discovered_at >= <latest scan>.started_at``.  The
    scan record itself is removed as well.  Jobs discovered by earlier
    scans are untouched.

``"all"``
    Delete every resettable row (jobs, matches, applications,
    generated documents, scan records, source health, AI usage).
    Candidate profile, search settings, search profiles, source
    registry and schema version are always preserved.

Protected rows (NEVER deleted by a reset)
-----------------------------------------
- ``candidate_profiles``
- ``settings_rows``
- ``search_profiles``
- ``source_registry``
- ``schema_version``

If a reset is started while any scan record has ``status='running'``,
all running scans are first marked ``cancelled`` so a background thread
cannot commit new rows mid-reset.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Optional, Tuple

from sqlalchemy.orm import Session

from database.models import (
    AiUsage,
    Application,
    GeneratedDocument,
    Job,
    JobMatch,
    ScanRecord,
    SourceHealthRecord,
)
from services.logger import get_logger

log = get_logger("jobhunter.reset")

ResetScope = Literal["current", "all"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ResetPlan:
    """Rows that ``execute_reset`` will delete, tabulated by table."""

    scope: ResetScope
    scan_record_id: Optional[int] = None
    scan_started_at: Optional[datetime] = None
    jobs: int = 0
    job_matches: int = 0
    applications: int = 0
    generated_documents: int = 0
    scan_records: int = 0
    source_health: int = 0
    ai_usage: int = 0
    # Scans that were running and got cancelled first.
    running_scans_cancelled: int = 0

    @property
    def total(self) -> int:
        return (
            self.jobs
            + self.job_matches
            + self.applications
            + self.generated_documents
            + self.scan_records
            + self.source_health
            + self.ai_usage
        )

    def to_dict(self) -> dict:
        return {
            "scope": self.scope,
            "scan_record_id": self.scan_record_id,
            "jobs": self.jobs,
            "job_matches": self.job_matches,
            "applications": self.applications,
            "generated_documents": self.generated_documents,
            "scan_records": self.scan_records,
            "source_health": self.source_health,
            "ai_usage": self.ai_usage,
            "running_scans_cancelled": self.running_scans_cancelled,
            "total": self.total,
        }


@dataclass
class ResetResult:
    success: bool
    scope: ResetScope
    deleted: dict
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "scope": self.scope,
            "deleted": self.deleted,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


def _latest_scan(session: Session) -> Optional[ScanRecord]:
    return (
        session.query(ScanRecord)
        .order_by(ScanRecord.started_at.desc(), ScanRecord.id.desc())
        .first()
    )


def plan_reset(session: Session, scope: ResetScope) -> ResetPlan:
    """Inspect the DB and return a DeletePlan without modifying anything."""
    if scope not in ("current", "all"):
        raise ValueError(f"unknown reset scope: {scope!r}")

    plan = ResetPlan(scope=scope)

    if scope == "all":
        plan.scan_records = session.query(ScanRecord).count()
        plan.jobs = session.query(Job).count()
        plan.job_matches = session.query(JobMatch).count()
        plan.applications = session.query(Application).count()
        plan.generated_documents = session.query(GeneratedDocument).count()
        plan.source_health = session.query(SourceHealthRecord).count()
        plan.ai_usage = session.query(AiUsage).count()
        plan.running_scans_cancelled = (
            session.query(ScanRecord)
            .filter(ScanRecord.status == "running")
            .count()
        )
        return plan

    # scope == "current": delete the LATEST scan and everything that
    # scan produced.  A job belongs to the latest scan when its
    # first_discovered_at >= latest.started_at.  This matches the UI
    # copy: "Clear current search results (latest scan only)" — the
    # user's previous search and the scan record that goes with it are
    # NOT touched.
    scans = (
        session.query(ScanRecord)
        .order_by(ScanRecord.started_at.desc(), ScanRecord.id.desc())
        .all()
    )
    if not scans:
        return plan  # nothing to do

    latest = scans[0]
    plan.scan_record_id = latest.id
    plan.scan_started_at = latest.started_at
    since: Optional[datetime] = latest.started_at

    plan.jobs = session.query(Job).filter(
        Job.first_discovered_at >= since
    ).count()
    if plan.jobs == 0:
        return plan

    job_ids = [
        j_id for (j_id,) in session.query(Job.id).filter(
            Job.first_discovered_at >= since
        ).all()
    ]
    if job_ids:
        plan.job_matches = (
            session.query(JobMatch).filter(JobMatch.job_id.in_(job_ids)).count()
        )
        plan.applications = (
            session.query(Application).filter(Application.job_id.in_(job_ids)).count()
        )
        plan.generated_documents = (
            session.query(GeneratedDocument)
            .filter(GeneratedDocument.job_id.in_(job_ids))
            .count()
        )
        plan.ai_usage = session.query(AiUsage).filter(
            (AiUsage.job_id.in_(job_ids))
            | ((AiUsage.ts >= since) & (AiUsage.job_id.is_(None)))
        ).count()
    # Source health is per-source, not per-scan; only cleared in "all".
    plan.scan_records = 1  # the most-recent scan record
    return plan


# ---------------------------------------------------------------------------
# Execute
# ---------------------------------------------------------------------------


def execute_reset(session: Session, plan: Optional[ResetPlan] = None,
                  scope: Optional[ResetScope] = None) -> ResetResult:
    """Delete rows in FK-safe order; single-transaction; rollback on error.

    If ``plan`` is None, a fresh plan is built by re-invoking ``plan_reset``.
    If both ``plan`` and ``scope`` are None, ``scope`` defaults to "all".
    """
    if plan is None:
        if scope is None:
            scope = "all"
        plan = plan_reset(session, scope)
    elif scope is None:
        scope = plan.scope

    deleted: dict = {
        "jobs": 0,
        "job_matches": 0,
        "applications": 0,
        "generated_documents": 0,
        "scan_records": 0,
        "source_health": 0,
        "ai_usage": 0,
    }

    try:
        # Cancel any scans still marked "running" so a concurrent scan
        # cannot INSERT rows into a table we're about to delete.  Done
        # at the *execute* step (not plan) so plan stays side-effect
        # free — the UI can preview safely.
        _cancel_running_scans(session, plan)

        if scope == "all":
            _delete_all(session, deleted)
        else:
            _delete_current(session, plan, deleted)

        session.commit()
        log.info("reset(%s) committed: %s", scope, deleted)
        return ResetResult(success=True, scope=scope, deleted=deleted)
    except Exception as exc:  # noqa: BLE001 — commit fails, we must roll back
        session.rollback()
        log.exception("reset(%s) failed, rolled back: %s", scope, exc)
        return ResetResult(
            success=False, scope=scope, deleted={"rolled_back": True},
            error=f"{type(exc).__name__}: {exc}",
        )


def _cancel_running_scans(session: Session, plan: ResetPlan) -> None:
    """Flush ``status='running'`` scans to ``cancelled`` within this txn."""
    running = (
        session.query(ScanRecord).filter(ScanRecord.status == "running").all()
    )
    for scan in running:
        scan.status = "cancelled"
        if scan.completed_at is None:
            scan.completed_at = _utcnow()
    plan.running_scans_cancelled += len(running)
    if running:
        log.info("cancelled %d running scan(s) before reset", len(running))


def _delete_all(session: Session, deleted: dict) -> None:
    """FK-safe order: children first, then parents."""
    # 1. Dependent rows (FK -> jobs, plus orphan AI usage)
    deleted["ai_usage"] = session.query(AiUsage).delete(synchronize_session=False)
    deleted["job_matches"] = (
        session.query(JobMatch).delete(synchronize_session=False)
    )
    deleted["applications"] = (
        session.query(Application).delete(synchronize_session=False)
    )
    deleted["generated_documents"] = (
        session.query(GeneratedDocument).delete(synchronize_session=False)
    )
    # 2. Search state (no FK to jobs)
    deleted["scan_records"] = session.query(ScanRecord).delete(synchronize_session=False)
    deleted["source_health"] = (
        session.query(SourceHealthRecord).delete(synchronize_session=False)
    )
    # 3. Parents
    deleted["jobs"] = session.query(Job).delete(synchronize_session=False)


def _delete_current(session: Session, plan: ResetPlan, deleted: dict) -> None:
    if plan.scan_record_id is None or plan.scan_started_at is None:
        return
    since = plan.scan_started_at
    job_q = session.query(Job).filter(Job.first_discovered_at >= since)
    job_ids = [jid for (jid,) in job_q.with_entities(Job.id).all()]
    if job_ids:
        deleted["ai_usage"] = session.query(AiUsage).filter(
            (AiUsage.job_id.in_(job_ids))
            | ((AiUsage.ts >= since) & (AiUsage.job_id.is_(None)))
        ).delete(synchronize_session=False)
        deleted["job_matches"] = session.query(JobMatch).filter(
            JobMatch.job_id.in_(job_ids)
        ).delete(synchronize_session=False)
        deleted["applications"] = session.query(Application).filter(
            Application.job_id.in_(job_ids)
        ).delete(synchronize_session=False)
        deleted["generated_documents"] = session.query(GeneratedDocument).filter(
            GeneratedDocument.job_id.in_(job_ids)
        ).delete(synchronize_session=False)
        deleted["jobs"] = job_q.delete(synchronize_session=False)
    # Remove the latest scan record (and not earlier ones).
    deleted["scan_records"] = (
        session.query(ScanRecord)
        .filter(ScanRecord.id == plan.scan_record_id)
        .delete(synchronize_session=False)
    )

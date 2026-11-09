"""Shared DB access + serialisation helpers for the `quackers` CLI.

- Engine creation lives in `database.connection` (cached per DATABASE_URL).
- `with_session()` is a context manager that commits on success, rolls back
  on exception and always closes.
- The remaining helpers turn ORM rows into plain dicts so command handlers
  never touch detached ORM instances.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from database import repository as db  # noqa: F401 (re-export for callers)
from database.connection import get_engine, get_session, init_db


# ---------------------------------------------------------------------------
# Engine / session
# ---------------------------------------------------------------------------


def ensure_db() -> None:
    """Idempotently create the schema. Called once at app startup."""
    init_db()


@contextmanager
def with_session() -> Iterator:
    """Yield a Session; commit on success, rollback on error, always close.

    Usage:
        with with_session() as s:
            job = db.get_job(s, 42)
            ...  (commits on normal exit)
    """
    engine = get_engine()
    session = get_session(engine)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Date / time helpers
# ---------------------------------------------------------------------------


def dt_local(dt: Optional[datetime]) -> str:
    """Format a datetime as 'YYYY-MM-DD HH:MM' or '' if missing."""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M")


def dt_short(dt: Optional[datetime]) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%d")


def within_days(dt: Optional[datetime], days: int) -> bool:
    """True if dt is within the last `days` days (also accepts future)."""
    if dt is None:
        return False
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now - dt) <= timedelta(days=days)


# ---------------------------------------------------------------------------
# Job serialisation
# ---------------------------------------------------------------------------


def job_to_dict(job) -> Dict[str, Any]:
    """Stable serialisable form of a Job (plain dict, safe outside a session)."""
    return {
        "id": job.id,
        "title": job.title,
        "company": job.company,
        "location": job.location or "",
        "country": job.country or "",
        "employment_type": job.employment_type,
        "workplace_arrangement": job.workplace_arrangement,
        "published_at": dt_local(job.published_at),
        "source": job.source,
        "salary": job.salary or "",
        "original_url": job.original_url,
        "application_url": job.application_url or "",
        "last_seen": dt_short(job.last_seen_at),
        "first_discovered": dt_short(job.first_discovered_at),
        "status": job.status,
        "fingerprint": job.content_fingerprint,
        "extraction_status": job.extraction_status,
        "extraction_method": job.extraction_method,
        "description_completeness": job.description_completeness,
    }


def load_active_profile() -> Optional[Any]:
    """Return the currently active CandidateProfile ORM instance, or None.

    Used by commands that need the active profile data without opening a DB
    session themselves (e.g. `quackers docs cover-letter`).
    """
    with with_session() as session:
        return db.get_active_candidate_profile(session)


def list_documents_for_job(job_id: int) -> List[Dict[str, Any]]:
    """Return every GeneratedDocument for a specific job as plain dicts.

    Caller expectations (quackers.cli job / docs commands):
        d["doc_type"]       - str (e.g. "cv_suggestions", "cover_letter")
        d["created_at"]     - str  (formatted YYYY-MM-DD HH:MM)
        d["version"]        - int  (from GeneratedDocument.profile_version)
        d["approved"]       - bool
        d["file_path"]      - str
    """
    with with_session() as session:
        docs = db.get_doc_list(session, job_id=job_id)
        return [
            {
                "id": d.id,
                "doc_type": d.doc_type,
                "created_at": dt_local(d.created_at),
                "version": d.profile_version,
                "approved": bool(d.approved),
                "file_path": d.file_path,
            }
            for d in docs
        ]


def get_output_dir() -> Path:
    """Return (and create if missing) the directory for generated documents.

    Falls back to PROJECT_ROOT/data/outputs which is consistent with the
    PROJECT_ROOT-based data directories used everywhere else.
    """
    from config.settings import get_config

    cfg = get_config()
    out = cfg.data_dir / "outputs"
    out.mkdir(parents=True, exist_ok=True)
    return out


def match_to_row(job, match, profile_version: Optional[int]) -> Dict[str, Any]:
    """Merge a Job + optional JobMatch into a single row dict."""
    base = job_to_dict(job)
    base["match_score"] = match.match_score if match else None
    base["match_profile_version"] = match.profile_version if match else None
    base["match_ai_model"] = match.ai_model if match else None
    base["match_evaluated_at"] = dt_short(match.evaluated_at) if match else ""
    base["profile_version"] = profile_version
    return base


def list_jobs_with_match(
    min_score: float = 0.0,
    status: Optional[str] = None,
    source: Optional[str] = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """List jobs (newest first) merged with their latest match row."""
    from database.models import Job

    with with_session() as session:
        q = session.query(Job)
        if status:
            q = q.filter(Job.status == status)
        if source:
            q = q.filter(Job.source == source)
        jobs = q.order_by(Job.first_discovered_at.desc()).limit(limit).all()
        active_profile = db.get_active_candidate_profile(session)
        rows = []
        for j in jobs:
            m = (
                db.get_match_for_job(
                    session, j.id, profile_version=active_profile.profile_version, candidate_profile_id=active_profile.id
                )
                if active_profile
                else db.get_match_for_job(session, j.id)
            )
            row = match_to_row(
                j, m, active_profile.profile_version if active_profile else None
            )
            score = row.get("match_score")
            if min_score > 0 and (score is None or score < min_score):
                continue
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Application-state helpers
# ---------------------------------------------------------------------------

APP_STATUSES = [
    "DISCOVERED",
    "SHORTLISTED",
    "PREPARED",
    "APPROVED",
    "APPLIED",
    "INTERVIEW",
    "REJECTED",
    "WITHDRAWN",
]

STATUS_LABEL = {
    "DISCOVERED": "Discovered",
    "SHORTLISTED": "Shortlisted",
    "PREPARED": "Docs Prepared",
    "APPROVED": "Awaiting Approval",
    "APPLIED": "Applied",
    "INTERVIEW": "Interview",
    "REJECTED": "Rejected",
    "WITHDRAWN": "Withdrawn",
}


def application_row(app, job) -> Dict[str, Any]:
    base = job_to_dict(job) if job else {}
    base.update(
        {
            "app_id": app.id,
            "app_status": app.status,
            "status_label": STATUS_LABEL.get(app.status, app.status),
            "shortlisted_at": dt_short(app.shortlisted_at),
            "docs_prepared_at": dt_short(app.docs_prepared_at),
            "approved_at": dt_short(app.approved_at),
            "applied_at": dt_short(app.applied_at),
            "follow_up_at": dt_short(app.follow_up_at),
            "interview_status": app.interview_status or "",
        }
    )
    return base


def list_applications_with_jobs() -> List[Dict[str, Any]]:
    """All applications joined with their job (for `quackers app list`)."""
    from database.models import Application, Job

    with with_session() as session:
        apps = session.query(Application).order_by(Application.id.desc()).all()
        jobs = {
            j.id: j for j in session.query(Job).all()
        }
        return [application_row(a, jobs.get(a.job_id)) for a in apps]


def upcoming_followups(window_days: int = 14) -> List[Dict[str, Any]]:
    """Applications with a follow-up within window_days, oldest first."""
    from database.models import Application, Job, utcnow
    from datetime import timedelta

    with with_session() as session:
        now = utcnow()
        cutoff = now + timedelta(days=window_days)
        apps = (
            session.query(Application)
            .filter(Application.follow_up_at.isnot(None))
            .filter(Application.follow_up_at <= cutoff)
            .filter(Application.follow_up_at >= now - timedelta(hours=1))
            .order_by(Application.follow_up_at.asc())
            .all()
        )
        jobs = {j.id: j for j in session.query(Job).all()}
        return [application_row(a, jobs.get(a.job_id)) for a in apps]


# ---------------------------------------------------------------------------
# Search profiles
# ---------------------------------------------------------------------------


def list_search_profile_names() -> List[str]:
    from database.models import SearchProfile

    with with_session() as session:
        rows = (
            session.query(SearchProfile).order_by(SearchProfile.name.asc()).all()
        )
        return [r.name for r in rows]


def load_search_profile_dict(name: str) -> Optional[Dict[str, Any]]:
    """Load a named SearchProfile and parse its JSON into a dict, or None."""
    import json

    with with_session() as session:
        prof = db.load_search_profile(session, name)
        if prof is None or not prof.config_json:
            return None
        try:
            return json.loads(prof.config_json)
        except json.JSONDecodeError:
            return None


def save_search_profile_dict(name: str, payload: Dict[str, Any]) -> None:
    """Persist a dict as a named SearchProfile (SELECT + upsert)."""
    import json

    json_safe = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    with with_session() as session:
        db.save_search_profile(session, name, json_safe)


# ---------------------------------------------------------------------------
# Source names
# ---------------------------------------------------------------------------


def list_registered_sources() -> List[str]:
    """Return the list of registered source names (by source class name)."""
    from sources.registry import list_sources

    return sorted(s.name for s in list_sources())


# ---------------------------------------------------------------------------
# Scan history
# ---------------------------------------------------------------------------


def list_scans(session, limit: int = 50):
    """Return ScanRecord rows newest first."""
    from database.models import ScanRecord

    return (
        session.query(ScanRecord).order_by(ScanRecord.started_at.desc()).limit(limit).all()
    )


def _json_safe(default, raw: Optional[str]):
    """Parse a JSON text column; return `default` if empty or invalid."""
    if not raw:
        return default
    try:
        import json

        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return default
    return default if parsed is None else parsed


def latest_scan_dict(limit: int = 1) -> Optional[Dict[str, Any]]:
    """Convenience serialised summary of the latest `limit` scan(s) (1 default)."""
    with with_session() as session:
        scans = list_scans(session, limit=limit)
        out = []
        for s in scans:
            dur_s = 0.0
            if s.started_at and s.completed_at:
                dur_s = (s.completed_at - s.started_at).total_seconds()
            out.append(
                {
                    "id": s.id,
                    "started_at": dt_local(s.started_at),
                    "completed_at": dt_short(s.completed_at) if s.completed_at else "",
                    "status": s.status,
                    "trigger": s.trigger,
                    "total": s.total_discovered,
                    "new": s.new_jobs,
                    "dups": s.duplicates,
                    "dur_s": dur_s,
                    # search_config is stored as a JSON text column.
                    "search_config": _json_safe(dict, s.search_config_json),
                }
            )
        return out[0] if out and limit == 1 else out

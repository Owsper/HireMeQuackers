"""Repository layer (CRUD + queries) over the models."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from .models import (
    AiUsage,
    Application,
    CandidateProfile,
    GeneratedDocument,
    Job,
    JobMatch,
    ScanRecord,
    SearchProfile,
    SettingsRow,
    SourceHealthRecord,
    utcnow,
)


# ---- Jobs ----


def create_job(
    session: Session,
    *,
    title: str,
    company: str,
    location: Optional[str],
    country: Optional[str],
    employment_type: str,
    workplace_arrangement: str,
    original_url: str,
    application_url: Optional[str],
    source: str,
    description: Optional[str] = None,
    required_skills: Optional[List[str]] = None,
    preferred_skills: Optional[List[str]] = None,
    experience_requirements: Optional[str] = None,
    education_requirements: Optional[str] = None,
    language_requirements: Optional[str] = None,
    salary: Optional[str] = None,
    industry: Optional[str] = None,
    published_at: Optional[datetime] = None,
    closing_at: Optional[datetime] = None,
    content_fingerprint: Optional[str] = None,
) -> Job:
    job = Job(
        title=title,
        company=company,
        location=location,
        country=country,
        employment_type=employment_type,
        workplace_arrangement=workplace_arrangement,
        original_url=original_url,
        application_url=application_url,
        source=source,
        description=description,
        required_skills_json=_j(required_skills),
        preferred_skills_json=_j(preferred_skills),
        experience_requirements=experience_requirements,
        education_requirements=education_requirements,
        language_requirements=language_requirements,
        salary=salary,
        industry=industry,
        published_at=published_at,
        first_discovered_at=utcnow(),
        last_seen_at=utcnow(),
        closing_at=closing_at,
        status="OPEN",
        content_fingerprint=content_fingerprint or "",
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def get_or_create_job_by_fingerprint(
    session: Session, fingerprint: str, **fields: Any
) -> Tuple[Job, bool]:
    """Return (job, created). Refreshes last_seen_at when found."""
    existing = session.query(Job).filter_by(content_fingerprint=fingerprint).first()
    if existing is not None:
        existing.last_seen_at = utcnow()
        session.commit()
        session.refresh(existing)
        return existing, False
    # Normalize list fields into JSON columns
    normalized: Dict[str, Any] = {}
    for k, v in fields.items():
        if k in ("required_skills", "preferred_skills") and isinstance(v, (list, set)):
            normalized[k + "_json"] = _j(list(v))
        else:
            normalized[k] = v
    normalized.setdefault("content_fingerprint", fingerprint)
    job = Job(**normalized)
    session.add(job)
    session.commit()
    session.refresh(job)
    return job, True


def find_duplicate(session: Session, job: Job) -> Optional[Job]:
    """Find an existing row with same fingerprint OR very-close company+title."""
    query = session.query(Job).filter(Job.content_fingerprint == job.content_fingerprint)
    row = query.first()
    if row is not None and row.id != job.id:
        return row
    # Search by company + title
    same = (
        session.query(Job)
        .filter(Job.company == job.company)
        .filter(Job.title == job.title)
        .filter(Job.id != job.id)
        .first()
    )
    return same


def update_job_fields(session: Session, job: Job, **fields: Any) -> Job:
    if "required_skills" in fields and isinstance(fields["required_skills"], list):
        fields = dict(fields, required_skills_json=_j(fields["required_skills"]))
        fields.pop("required_skills", None)
    if "preferred_skills" in fields and isinstance(fields["preferred_skills"], list):
        fields = dict(fields, preferred_skills_json=_j(fields["preferred_skills"]))
        fields.pop("preferred_skills", None)
    for k, v in fields.items():
        if hasattr(job, k):
            setattr(job, k, v)
    job.last_seen_at = utcnow()
    session.commit()
    session.refresh(job)
    return job


def get_jobs_by_scan(session: Session, scan_id: int) -> List[Job]:
    # Simple heuristic: jobs first_discovered >= scan.started_at
    scan = session.query(ScanRecord).filter_by(id=scan_id).first()
    if scan is None:
        return []
    limit = scan.started_at
    return session.query(Job).filter(Job.first_discovered_at >= limit).all()


def list_jobs(session: Session, limit: int = 200) -> List[Job]:
    return session.query(Job).order_by(Job.first_discovered_at.desc()).limit(limit).all()


def update_job_from_enrichment(
    session: Session, job_id: int, detail: Any
) -> int:
    """Fill in currently-empty Job fields from an ExtractedDetail.

    Never overwrites data that already exists. Returns number of rows updated.
    """
    job = session.query(Job).filter_by(id=job_id).first()
    if job is None:
        return 0
    changes = False

    def _fill(attr: str, value: Any) -> None:
        nonlocal changes
        if value is None or value == "":
            return
        current = getattr(job, attr, None)
        if current is None or current == "":
            setattr(job, attr, value)
            changes = True

    _fill("description", detail.description_text)
    _fill("company", detail.employer)
    _fill("location", detail.location)
    _fill("salary", detail.salary_raw)
    _fill("employment_type", detail.employment_type)
    _fill("workplace_arrangement", detail.workplace_arrangement)
    _fill("application_url", detail.application_url)
    _fill("education_requirements", detail.education_requirements)
    if detail.json_ld_present or detail.extraction_confidence > 0:
        if job.extraction_confidence is None or job.extraction_confidence < detail.extraction_confidence:
            job.extraction_confidence = float(detail.extraction_confidence)
            changes = True
    if detail.posted_date:
        if job.published_at is None:
            from datetime import datetime
            try:
                job.published_at = datetime.strptime(str(detail.posted_date)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                changes = True
            except ValueError:
                pass
    if detail.closing_date:
        if job.closing_at is None:
            from datetime import datetime
            try:
                job.closing_at = datetime.strptime(str(detail.closing_date)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                changes = True
            except ValueError:
                pass
    if not changes:
        return 0
    job.last_verified_at = utcnow()
    session.commit()
    return 1


def list_jobs_needing_enrichment(session: Session, limit: int = 25) -> List[Job]:
    """Jobs with no description and an original_url to fetch."""
    return (
        session.query(Job)
        .filter((Job.description.is_(None)) | (Job.description == ""))
        .filter(Job.original_url.isnot(None))
        .filter(Job.original_url != "")
        .limit(limit)
        .all()
    )


# ---- Candidate profile ----


def upsert_candidate_profile(
    session: Session,
    *,
    name: str,
    email: Optional[str] = None,
    phone: Optional[str] = None,
    candidate_email: Optional[str] = None,
    linkedin: Optional[str] = None,
    website: Optional[str] = None,
    summary: Optional[str] = None,
    skills: Optional[List[str]] = None,
    education: Optional[List[Dict[str, Any]]] = None,
    experience: Optional[List[Dict[str, Any]]] = None,
    languages: Optional[List[Dict[str, Any]]] = None,
    certifications: Optional[List[str]] = None,
    achievements: Optional[List[str]] = None,
    projects: Optional[List[Dict[str, Any]]] = None,
    cv_path: Optional[str] = None,
    active: bool = False,
    other: Optional[Dict[str, Any]] = None,
) -> CandidateProfile:
    """Create a brand-new profile (the master CV is profile_id=1 conventionally).

    This always creates a new profile row. Callers should load the existing
    one and call `update_profile` for in-place edits.
    """
    profile = CandidateProfile(
        name=name,
        email=email,
        phone=phone,
        candidate_email=candidate_email,
        linkedin=linkedin,
        website=website,
        summary=summary,
        skills_json=_j(skills),
        education_json=_j(education),
        experience_json=_j(experience),
        languages_json=_j(languages),
        certifications_json=_j(certifications),
        achievements_json=_j(achievements),
        projects_json=_j(projects),
        other_json=_j(other),
        profile_version=1,
        cv_path=cv_path,
        active=active,
        updated_at=utcnow(),
    )
    session.add(profile)
    session.commit()
    session.refresh(profile)
    return profile


def get_candidate_profile(session: Session, profile_id: int) -> Optional[CandidateProfile]:
    return session.query(CandidateProfile).filter_by(id=profile_id).first()


def get_active_candidate_profile(session: Session) -> Optional[CandidateProfile]:
    return (
        session.query(CandidateProfile)
        .filter_by(active=True)
        .order_by(CandidateProfile.profile_version.desc())
        .first()
    )


def increment_profile_version(profile: CandidateProfile) -> CandidateProfile:
    profile.profile_version += 1
    profile.updated_at = utcnow()
    return profile


# ---- JobMatch ----


def create_match(
    session: Session,
    *,
    job_id: int,
    profile_version: int,
    match_score: float,
    score: Optional[Dict[str, Any]] = None,
    evidence: Optional[List[str]] = None,
    missing: Optional[List[str]] = None,
    ai_model: Optional[str] = None,
    scoring_config_hash: Optional[str] = None,
) -> JobMatch:
    # Upsert: replace existing for (job_id, profile_version)
    existing = (
        session.query(JobMatch)
        .filter_by(job_id=job_id, profile_version=profile_version)
        .first()
    )
    if existing is not None:
        existing.match_score = match_score
        existing.score_json = _j(score)
        existing.matching_evidence_json = _j(evidence)
        existing.missing_requirements_json = _j(missing)
        existing.ai_model = ai_model
        existing.evaluated_at = utcnow()
        existing.scoring_config_hash = scoring_config_hash
        session.commit()
        session.refresh(existing)
        return existing

    record = JobMatch(
        job_id=job_id,
        profile_version=profile_version,
        match_score=match_score,
        score_json=_j(score),
        matching_evidence_json=_j(evidence),
        missing_requirements_json=_j(missing),
        ai_model=ai_model,
        evaluated_at=utcnow(),
        scoring_config_hash=scoring_config_hash,
    )
    session.add(record)
    session.commit()
    session.refresh(record)
    return record


def get_match_for_job(
    session: Session, job_id: int, profile_version: Optional[int] = None
) -> Optional[JobMatch]:
    if profile_version is not None:
        return (
            session.query(JobMatch)
            .filter_by(job_id=job_id, profile_version=profile_version)
            .first()
        )
    return (
        session.query(JobMatch)
        .filter_by(job_id=job_id)
        .order_by(JobMatch.evaluated_at.desc())
        .first()
    )


# ---- Scans ----


def create_scan_record(
    session: Session,
    *,
    search_config: Optional[Dict[str, Any]] = None,
    trigger: str = "user",
) -> ScanRecord:
    scan = ScanRecord(
        started_at=utcnow(),
        total_discovered=0,
        new_jobs=0,
        duplicates=0,
        failures_json=None,
        status="running",
        search_config_json=_j(search_config),
        trigger=trigger,
    )
    session.add(scan)
    session.commit()
    session.refresh(scan)
    return scan


def finish_scan(
    session: Session,
    scan: ScanRecord,
    *,
    status: str = "done",
    total_discovered: int = 0,
    new_jobs: int = 0,
    duplicates: int = 0,
    failures: Optional[List[Dict[str, Any]]] = None,
) -> ScanRecord:
    scan.status = status
    scan.completed_at = utcnow()
    scan.total_discovered = total_discovered
    scan.new_jobs = new_jobs
    scan.duplicates = duplicates
    scan.failures_json = _j(failures or [])
    session.commit()
    session.refresh(scan)
    return scan


def list_scans(session: Session, limit: int = 50) -> List[ScanRecord]:
    return (
        session.query(ScanRecord)
        .order_by(ScanRecord.started_at.desc())
        .limit(limit)
        .all()
    )


# ---- Source health ----


def record_source_health(
    session: Session,
    *,
    source_name: str,
    source_url: Optional[str] = None,
    listings_found: int = 0,
    error_message: Optional[str] = None,
    is_enabled: bool = True,
) -> SourceHealthRecord:
    existing = (
        session.query(SourceHealthRecord).filter_by(source_name=source_name).first()
    )
    if existing is None:
        existing = SourceHealthRecord(
            source_name=source_name,
            source_url=source_url,
            listings_found=0,
            error_message=None,
            is_enabled=is_enabled,
            last_attempt_at=utcnow(),
        )
        session.add(existing)
    else:
        existing.last_attempt_at = utcnow()
        existing.is_enabled = is_enabled
    existing.listings_found = listings_found
    existing.error_message = error_message
    if error_message is None:
        existing.last_success_at = utcnow()
    session.commit()
    session.refresh(existing)
    return existing


def get_source_health(
    session: Session, source_name: str
) -> Optional[SourceHealthRecord]:
    return session.query(SourceHealthRecord).filter_by(source_name=source_name).first()


def set_source_enabled(
    session: Session, source_name: str, enabled: bool
) -> Optional[SourceHealthRecord]:
    row = get_source_health(session, source_name)
    if row is None:
        return None
    row.is_enabled = enabled
    session.commit()
    session.refresh(row)
    return row


# ---- Applications ----


def create_application(session: Session, *, job_id: int) -> Application:
    existing = (
        session.query(Application).filter_by(job_id=job_id).first()
    )
    if existing is not None:
        return existing
    app = Application(job_id=job_id, status="DISCOVERED", interview_status="Not Started")
    session.add(app)
    session.commit()
    session.refresh(app)
    return app


def get_application_state(session: Session, job_id: int) -> Optional[Application]:
    return session.query(Application).filter_by(job_id=job_id).first()


def mark_shortlisted(session: Session, app: Application) -> Application:
    if app.shortlisted_at is None:
        app.shortlisted_at = utcnow()
    app.status = "SHORTLISTED"
    app.interview_status = app.interview_status or "Not Started"
    session.commit()
    session.refresh(app)
    return app


def mark_docs_prepared(session: Session, app: Application) -> Application:
    if app.docs_prepared_at is None:
        app.docs_prepared_at = utcnow()
    if app.status in ("DISCOVERED", "SHORTLISTED"):
        app.status = "PREPARED"
    session.commit()
    session.refresh(app)
    return app


def mark_approved(session: Session, app: Application) -> Application:
    app.approved_at = utcnow()
    if app.status in ("DISCOVERED", "SHORTLISTED", "PREPARED"):
        app.status = "APPROVED"
    session.commit()
    session.refresh(app)
    return app


def is_already_applied(app: Optional[Application]) -> bool:
    return app is not None and app.status in {
        "APPLIED",
        "INTERVIEW",
        "REJECTED",
        "WITHDRAWN",
    }


def mark_applied(
    session: Session, app: Application, *, via: str = "manual"
) -> Application:
    if is_already_applied(app):
        raise DuplicateApplicationError(app.job_id)
    if app.applied_at is None:
        app.applied_at = utcnow()
    app.status = "APPLIED"
    app.notes = (app.notes or "") + f"\napplied_via={via}"
    session.commit()
    session.refresh(app)
    return app


def transition_application_status(
    session: Session, app: Application, new_status: str
) -> Application:
    app.status = new_status
    if new_status == "INTERVIEW" and app.status != "INTERVIEW":
        app.interview_status = "Scheduled"
    session.commit()
    session.refresh(app)
    return app


def add_note(session: Session, app: Application, note: str) -> Application:
    app.notes = ((app.notes or "") + "\n" + note).strip()
    session.commit()
    session.refresh(app)
    return app


def set_follow_up(
    session: Session, app: Application, when: Optional[datetime]
) -> Application:
    app.follow_up_at = when
    session.commit()
    session.refresh(app)
    return app


def set_interview_status(
    session: Session, app: Application, status: str
) -> Application:
    app.interview_status = status
    session.commit()
    session.refresh(app)
    return app


class DuplicateApplicationError(RuntimeError):
    def __init__(self, job_id: int):
        super().__init__(f"Duplicate application for job_id={job_id}")
        self.job_id = job_id


# ---- Generated documents ----


def create_generated_document(
    session: Session,
    *,
    job_id: int,
    doc_type: str,
    file_path: str,
    profile_version: int = 1,
    notes: Optional[str] = None,
) -> GeneratedDocument:
    doc = GeneratedDocument(
        job_id=job_id,
        doc_type=doc_type,
        file_path=file_path,
        profile_version=profile_version,
        approved=False,
        notes=notes,
    )
    session.add(doc)
    session.commit()
    session.refresh(doc)
    return doc


def get_doc_list(session: Session, job_id: Optional[int] = None) -> List[GeneratedDocument]:
    query = session.query(GeneratedDocument)
    if job_id is not None:
        query = query.filter_by(job_id=job_id)
    return query.order_by(GeneratedDocument.created_at.desc()).all()


def mark_doc_approved(
    session: Session, doc: GeneratedDocument, approved: bool = True
) -> GeneratedDocument:
    doc.approved = approved
    session.commit()
    session.refresh(doc)
    return doc


# ---- Settings rows ----


def save_settings(
    session: Session, key: str, value: Any
) -> SettingsRow:
    existing = session.query(SettingsRow).filter_by(key=key).first()
    if existing is None:
        existing = SettingsRow(key=key, value=_j(value) if not isinstance(value, str) else value)
        session.add(existing)
    else:
        existing.value = _j(value) if not isinstance(value, str) else value
    session.commit()
    session.refresh(existing)
    return existing


def load_settings(session: Session, key: str) -> Optional[Any]:
    row = session.query(SettingsRow).filter_by(key=key).first()
    if row is None:
        return None
    try:
        return json.loads(row.value or "")
    except (TypeError, json.JSONDecodeError):
        return row.value


# ---- Search profiles ----


def save_search_profile(session: Session, name: str, config_json: str) -> SearchProfile:
    existing = session.query(SearchProfile).filter_by(name=name).first()
    if existing is None:
        existing = SearchProfile(name=name, config_json=config_json, created_at=utcnow())
        session.add(existing)
    else:
        existing.config_json = config_json
    session.commit()
    session.refresh(existing)
    return existing


def load_search_profile(session: Session, name: str) -> Optional[SearchProfile]:
    return session.query(SearchProfile).filter_by(name=name).first()


def list_search_profiles(session: Session) -> List[SearchProfile]:
    return session.query(SearchProfile).order_by(SearchProfile.name.asc()).all()


# ---- AI usage ----


def record_ai_usage(
    session: Session,
    *,
    job_id: Optional[int] = None,
    operation: str = "score",
    model: Optional[str] = None,
    estimate_tokens: Optional[int] = None,
    cache_hit: bool = False,
    note: Optional[str] = None,
) -> AiUsage:
    row = AiUsage(
        job_id=job_id,
        operation=operation,
        model=model,
        estimate_tokens=estimate_tokens,
        cache_hit=cache_hit,
        note=note,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def get_ai_usage(
    session: Session, limit: int = 100
) -> List[AiUsage]:
    return (
        session.query(AiUsage)
        .order_by(AiUsage.ts.desc())
        .limit(limit)
        .all()
    )


# ---- Upcoming follow-ups ----


def upcoming_followups(
    session: Session, window_days: int = 14
) -> List[Tuple[Application, Job]]:
    now = utcnow()
    cutoff = now + timedelta(days=window_days)
    rows = (
        session.query(Application)
        .join(Job, Job.id == Application.job_id)
        .filter(Application.follow_up_at.isnot(None))
        .filter(Application.follow_up_at <= cutoff)
        .filter(Application.follow_up_at >= now - timedelta(hours=1))
        .all()
    )
    return [(app, get_job(session, app.job_id)) for app in rows]


def get_job(session: Session, job_id: int) -> Optional[Job]:
    return session.query(Job).filter_by(id=job_id).first()


def ensure_migrations(engine) -> None:
    """Apply pending DB migrations. Idempotent."""
    from database.migrations import upgrade
    upgrade(engine)


# ---- helpers ----


def _j(value: Any) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, sort_keys=True, separators=(",", ":"))

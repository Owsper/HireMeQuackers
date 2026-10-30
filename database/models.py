"""SQLAlchemy 2.x models (declarative). UTF-8, JSON columns via Text."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """Naive UTC timestamp (datetime.utcnow is deprecated in 3.12)."""
    return datetime.now(timezone.utc)


def _to_json(value: Any) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _from_json(raw: Optional[str]) -> Any:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None


class Base(DeclarativeBase):
    """Declarative base for all models."""


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(300), index=True)
    company: Mapped[str] = mapped_column(String(300), index=True)
    location: Mapped[Optional[str]] = mapped_column(String(300), index=True)
    country: Mapped[Optional[str]] = mapped_column(String(120))
    employment_type: Mapped[str] = mapped_column(String(60), index=True, default="other")
    workplace_arrangement: Mapped[str] = mapped_column(
        String(40), index=True, default="unknown"
    )
    description: Mapped[Optional[str]] = mapped_column(Text)
    required_skills_json: Mapped[Optional[str]] = mapped_column(Text)
    preferred_skills_json: Mapped[Optional[str]] = mapped_column(Text)
    experience_requirements: Mapped[Optional[str]] = mapped_column(Text)
    education_requirements: Mapped[Optional[str]] = mapped_column(Text)
    language_requirements: Mapped[Optional[str]] = mapped_column(Text)
    salary: Mapped[Optional[str]] = mapped_column(Text)
    industry: Mapped[Optional[str]] = mapped_column(String(120))
    original_url: Mapped[str] = mapped_column(Text)
    application_url: Mapped[Optional[str]] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(60), index=True)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    first_discovered_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    closing_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(40), default="OPEN", index=True)
    content_fingerprint: Mapped[str] = mapped_column(String(80), index=True)

    # --- Max-coverage discovery engine fields (Phase A2) ---
    external_job_id: Mapped[Optional[str]] = mapped_column(String(120), nullable=True, index=True)
    source_type: Mapped[Optional[str]] = mapped_column(String(40), nullable=True, default=None)
    verification_status: Mapped[str] = mapped_column(String(30), nullable=False, default="unverified")
    extraction_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    last_verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    application_method: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    scam_risk_flags: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # --- P2 search-intent validation columns ---
    title_relevance: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    location_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    match_explanation: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # --- multilingual: language the posting is written in + languages the
    # applicant must speak (ISO 639-1; NULL = unknown, never guessed) ---
    description_language: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    required_languages_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    @property
    def required_languages(self) -> List[str]:
        return _from_json(self.required_languages_json) or []

    @property
    def required_skills(self) -> List[str]:
        return _from_json(self.required_skills_json) or []

    @property
    def preferred_skills(self) -> List[str]:
        return _from_json(self.preferred_skills_json) or []


class CandidateProfile(Base):
    __tablename__ = "candidate_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[Optional[str]] = mapped_column(String(200))
    phone: Mapped[Optional[str]] = mapped_column(String(60))
    candidate_email: Mapped[Optional[str]] = mapped_column(String(200))
    linkedin: Mapped[Optional[str]] = mapped_column(String(300))
    website: Mapped[Optional[str]] = mapped_column(String(300))
    summary: Mapped[Optional[str]] = mapped_column(Text)
    skills_json: Mapped[Optional[str]] = mapped_column(Text)
    education_json: Mapped[Optional[str]] = mapped_column(Text)
    experience_json: Mapped[Optional[str]] = mapped_column(Text)
    languages_json: Mapped[Optional[str]] = mapped_column(Text)
    certifications_json: Mapped[Optional[str]] = mapped_column(Text)
    achievements_json: Mapped[Optional[str]] = mapped_column(Text)
    projects_json: Mapped[Optional[str]] = mapped_column(Text)
    preferred_employment_types_json: Mapped[Optional[str]] = mapped_column(Text)
    preferred_locations_json: Mapped[Optional[str]] = mapped_column(Text)
    other_json: Mapped[Optional[str]] = mapped_column(Text)
    profile_version: Mapped[int] = mapped_column(Integer, default=1)
    cv_path: Mapped[Optional[str]] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    @property
    def skills(self) -> List[str]:
        return _from_json(self.skills_json) or []

    @property
    def education(self) -> List[Dict[str, Any]]:
        return _from_json(self.education_json) or []

    @property
    def experience(self) -> List[Dict[str, Any]]:
        return _from_json(self.experience_json) or []

    @property
    def certifications(self) -> List[str]:
        return _from_json(self.certifications_json) or []

    @property
    def languages(self) -> List[Dict[str, Any]]:
        return _from_json(self.languages_json) or []

    @property
    def achievements(self) -> List[str]:
        return _from_json(self.achievements_json) or []

    @property
    def projects(self) -> List[Dict[str, Any]]:
        return _from_json(self.projects_json) or []


class JobMatch(Base):
    __tablename__ = "job_matches"
    __table_args__ = (
        UniqueConstraint("job_id", "profile_version", name="uq_match_job_profile"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    match_score: Mapped[float] = mapped_column(Float, default=0.0)
    score_json: Mapped[Optional[str]] = mapped_column(Text)
    matching_evidence_json: Mapped[Optional[str]] = mapped_column(Text)
    missing_requirements_json: Mapped[Optional[str]] = mapped_column(Text)
    ai_model: Mapped[Optional[str]] = mapped_column(String(120))
    evaluated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    profile_version: Mapped[int] = mapped_column(Integer, default=1)
    scoring_config_hash: Mapped[Optional[str]] = mapped_column(String(80))


class ScanRecord(Base):
    __tablename__ = "scan_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    total_discovered: Mapped[int] = mapped_column(Integer, default=0)
    new_jobs: Mapped[int] = mapped_column(Integer, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, default=0)
    failures_json: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(40), default="running", index=True)
    search_config_json: Mapped[Optional[str]] = mapped_column(Text)
    trigger: Mapped[str] = mapped_column(String(40), default="user")
    # {platform: {found, unique, cross_source_duplicates}} for this scan
    platform_stats_json: Mapped[Optional[str]] = mapped_column(Text)


class SourceHealthRecord(Base):
    __tablename__ = "source_health"
    __table_args__ = (UniqueConstraint("source_name", name="uq_source_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_name: Mapped[str] = mapped_column(String(80))
    source_url: Mapped[Optional[str]] = mapped_column(Text)
    last_success_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    last_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    listings_found: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Application(Base):
    __tablename__ = "applications"
    __table_args__ = (UniqueConstraint("job_id", name="uq_application_job"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(40), default="DISCOVERED", index=True)
    shortlisted_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    docs_prepared_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    applied_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    follow_up_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    interview_status: Mapped[str] = mapped_column(
        String(40), default="Not Started"
    )


class GeneratedDocument(Base):
    __tablename__ = "generated_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    doc_type: Mapped[str] = mapped_column(String(40))  # CV | COVER_LETTER
    file_path: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    profile_version: Mapped[int] = mapped_column(Integer, default=1)
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[Optional[str]] = mapped_column(Text)


class SettingsRow(Base):
    __tablename__ = "settings_rows"
    __table_args__ = (UniqueConstraint("key", name="uq_settings_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(120))
    value: Mapped[Optional[str]] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class SearchProfile(Base):
    __tablename__ = "search_profiles"
    __table_args__ = (UniqueConstraint("name", name="uq_search_profile_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120))
    config_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AiUsage(Base):
    __tablename__ = "ai_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    job_id: Mapped[Optional[int]] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    operation: Mapped[str] = mapped_column(String(60), default="score")
    model: Mapped[Optional[str]] = mapped_column(String(120))
    estimate_tokens: Mapped[Optional[int]] = mapped_column(Integer)
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[Optional[str]] = mapped_column(Text)


class SchemaVersion(Base):
    __tablename__ = "schema_version"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version: Mapped[str] = mapped_column(String(40))
    applied_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ScanSourceHealth(Base):
    __tablename__ = "scan_source_health"
    __table_args__ = (UniqueConstraint("source_name", name="uq_scan_source_health_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_name: Mapped[str] = mapped_column(String(60), nullable=False)
    last_scanned_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    listings_found: Mapped[int] = mapped_column(Integer, default=0)
    listings_stale: Mapped[int] = mapped_column(Integer, default=0)
    is_healthy: Mapped[bool] = mapped_column(Boolean, default=True)


class SourceRegistryEntry(Base):
    __tablename__ = "source_registry"
    __table_args__ = (UniqueConstraint("domain", name="uq_source_registry_domain"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    domain: Mapped[str] = mapped_column(String(200), nullable=False)
    ats_type: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    board_slug: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    discovered_via: Mapped[str] = mapped_column(String(40), default="manual")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


# Indexes for performance
Index("ix_job_status_first_discovered", Job.status, Job.first_discovered_at)
Index("ix_application_status", Application.job_id, Application.status)
Index("ix_generated_doc_job_type", GeneratedDocument.job_id, GeneratedDocument.doc_type)

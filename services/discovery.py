"""Orchestrates the full scan: discover -> normalize -> dedupe -> store."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable, List, Optional

from config.search_config import SearchConfig
from database import repository as db
from database.connection import get_engine, get_session
from database.models import CandidateProfile, Job, utcnow
from config.search_intent import build_intent
from services.deduplication import primary_hard_match, secondary_similarity
from services.filters import apply_filters
from services.logger import get_logger
from services.normalization import (
    canonical_url,
    content_fingerprint,
    raw_to_job,
)
from sources.base import RawListing, SourceResult
from sources.search_engine import ScanSummary, SearchEngine
from sources.registry import list_sources, enabled_sources

log = get_logger("jobhunter.discovery")


@dataclass
class DiscoverySummary:
    scan_record_id: Optional[int] = None
    total_discovered: int = 0
    new_jobs: int = 0
    duplicates: int = 0
    failures: List[dict] = field(default_factory=list)
    per_source: dict = field(default_factory=dict)
    per_platform: dict = field(default_factory=dict)
    cross_source_duplicates: int = 0
    ats_boards_discovered: int = 0
    matched_jobs: List[Job] = field(default_factory=list)
    excluded: List[dict] = field(default_factory=list)
    status: str = "error"
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    duration_seconds: float = 0.0
    enrichment_attempted: int = 0
    enrichment_succeeded: int = 0
    enrichment_failed: int = 0
    enrichment_skipped: int = 0
    enrichment_avg_confidence: float = 0.0

    def to_dict(self) -> dict:
        return {
            "scan_record_id": self.scan_record_id,
            "status": self.status,
            "total_discovered": self.total_discovered,
            "new_jobs": self.new_jobs,
            "duplicates": self.duplicates,
            "failures": self.failures,
            "matched_jobs": [json_safe(_job_meta(j)) for j in self.matched_jobs],
            "excluded": self.excluded[:25],
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_seconds": self.duration_seconds,
            "enrichment": {
                "attempted": self.enrichment_attempted,
                "succeeded": self.enrichment_succeeded,
                "failed": self.enrichment_failed,
                "skipped": self.enrichment_skipped,
                "avg_confidence": self.enrichment_avg_confidence,
            },
            "per_source": self.per_source,
            "per_platform": self.per_platform,
            "cross_source_duplicates": self.cross_source_duplicates,
            "ats_boards_discovered": self.ats_boards_discovered,
        }


def _job_meta(j: Job) -> dict:
    return {
        "id": j.id,
        "title": j.title,
        "company": j.company,
        "location": j.location,
        "employment_type": j.employment_type,
        "workplace_arrangement": j.workplace_arrangement,
        "source": j.source,
        "original_url": j.original_url,
        "application_url": j.application_url,
    }


def json_safe(v):
    return str(v)


def run_discovery(
    cfg: SearchConfig,
    *,
    on_progress: Optional[Callable[[int, str], None]] = None,
    evaluate_with_ai: bool = True,
) -> DiscoverySummary:
    from datetime import datetime
    from config.settings import get_config

    env = get_config()
    summary = DiscoverySummary()
    session = get_session(get_engine())
    scan = db.create_scan_record(session, search_config=cfg.normalized_dict(), trigger="user")
    summary.scan_record_id = scan.id
    summary.started_at = utcnow()

    # Step 1: Scan sources in parallel
    engine = SearchEngine()
    per_source_enabled = list_enabled(cfg)

    def _progress(count: int, state: str) -> None:
        if on_progress:
            on_progress(count, state)

    candidate = db.get_active_candidate_profile(session)
    scan_summary = engine.run_all(cfg, on_progress=_progress, profile=candidate) if candidate is not None else engine.run_all(cfg, on_progress=_progress)
    for name, result in scan_summary.per_source.items():
        db.record_source_health(
            session=session,
            source_name=name,
            source_url=(result.listings[0].original_url if result.listings else None),
            listings_found=result.listings_found,
            error_message=result.error_message,
            is_enabled=True,
        )
    # Step 1b: remember ATS boards seen in results so later scans query
    # them directly through the ATS adapters (non-fatal).
    try:
        from services.ats_detection import detect_ats_from_url, register_matches

        matches = [m for m in (detect_ats_from_url(l.original_url or "")
                               for l in scan_summary.all_listings) if m]
        summary.ats_boards_discovered = register_matches(matches, via="scan")
    except Exception:
        log.exception("ATS board registration failed (non-fatal)")

    # Step 2: Normalize + dedupe + create/update Job rows
    seen_urls = set()
    new_jobs = 0
    duplicates = 0
    eligible: List[Job] = []

    for raw in scan_summary.all_listings:
        norm = raw_to_job(raw)
        url_canon = canonical_url(raw.original_url)
        fp = content_fingerprint(
            url_canon,
            norm.get("title", ""),
            norm.get("company", ""),
            norm.get("description", "") or "",
        )
        norm["content_fingerprint"] = fp
        # Same vacancy already stored from another source/URL?
        same = _find_same_vacancy(session, norm)
        if same is not None:
            existing_row, created = same, False
        else:
            existing_row, created = db.get_or_create_job_by_fingerprint(session, fp, **norm)
        if created:
            new_jobs += 1
        else:
            # Update last-seen without erasing application history
            existing_row.last_seen_at = utcnow()
            existing_row.status = "OPEN"
            duplicates += 1
            session.commit()
        if existing_row.id not in {j.id for j in eligible}:
            eligible.append(existing_row)

    summary.total_discovered = len(eligible)
    summary.new_jobs = new_jobs
    summary.duplicates = duplicates

    # Enrich before filtering so snippets cannot exclude a genuinely relevant vacancy.
    if getattr(cfg, "enable_detail_extraction", True):
        from services.enrichment_service import enrich_missing_jobs
        try:
            esum = enrich_missing_jobs(session, cfg, job_ids=[j.id for j in eligible])
            summary.enrichment_attempted = esum.attempted
            summary.enrichment_succeeded = esum.succeeded
            summary.enrichment_failed = esum.failed
            summary.enrichment_skipped = esum.skipped
            summary.enrichment_avg_confidence = esum.avg_confidence
        except Exception as exc:
            session.rollback()
            log.warning("Enrichment step failed: %s", type(exc).__name__)

    # Step 3: deterministic filters, with a structured search intent so
    # the title / location hard constraints run *before* the loose
    # keyword pass.
    intent = build_intent(
        cfg,
        include_related_titles=bool(cfg.include_related_roles),
    )
    matched, excluded = apply_filters(eligible, cfg, intent=intent)
    summary.matched_jobs = matched
    summary.excluded = [
        {"job_id": j.id, "title": j.title, "company": j.company, "reason": r}
        for (j, r) in excluded
    ]

    # Step 4: (optional) AI evaluation
    profile = db.get_active_candidate_profile(session)
    if profile is not None:
        from services.matching import evaluate_jobs
        evaluate_jobs(matched, profile, on_progress=_progress, intent=intent, cfg=cfg, use_ai=evaluate_with_ai)

    # Step 5: finish scan record
    summary.status = "done" if scan_summary.status in ("done", "partial") else "error"
    if scan_summary.failures:
        summary.status = "partial" if scan_summary.total_discovered else "error"
    summary.failures = list(scan_summary.failures)
    summary.per_source = {
        n: r.details() for n, r in scan_summary.per_source.items()
    }
    summary.per_platform = dict(scan_summary.per_platform)
    summary.cross_source_duplicates = scan_summary.cross_source_duplicates
    summary.completed_at = utcnow()
    summary.duration_seconds = (
        (summary.completed_at - summary.started_at).total_seconds()
        if summary.started_at and summary.completed_at
        else 0.0
    )

    # Finish scan + record
    db.finish_scan(
        session=session,
        scan=scan,
        status="partial" if summary.status == "partial" else summary.status,
        total_discovered=summary.total_discovered,
        new_jobs=summary.new_jobs,
        duplicates=summary.duplicates,
        failures=summary.failures,
        platform_stats=summary.per_platform,
    )
    session.commit()
    session.close()
    return summary


def _find_same_vacancy(session, norm: dict) -> Optional[Job]:
    """A stored job with the same title + company (+ compatible city)."""
    from sqlalchemy import func
    from services.platforms import same_city, vacancy_key
    url = canonical_url(norm.get("original_url"))
    external_id = norm.get("external_job_id")
    if external_id:
        exact = session.query(Job).filter_by(external_job_id=external_id, source=norm.get("source")).first()
        if exact is not None:
            return exact
    for row in session.query(Job).filter(Job.original_url.like(url.split("?")[0] + "%")).limit(200):
        if url and canonical_url(row.original_url) == url:
            return row

    key = vacancy_key(norm.get("title", ""), norm.get("company", ""))
    if not key:
        return None
    company = (norm.get("company") or "").strip().lower()
    for row in session.query(Job).filter(func.lower(Job.company) == company).limit(200):
        if external_id and row.external_job_id and row.source == norm.get("source") and row.external_job_id != external_id:
            continue
        if (vacancy_key(row.title or "", row.company or "") == key
                and same_city(row.location, norm.get("location"))):
            return row
    return None


def list_enabled(cfg: SearchConfig) -> List[str]:
    if cfg.enabled_sources:
        return list(cfg.enabled_sources)
    return [s.name for s in list_sources()]

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
from services.deduplication import primary_hard_match, secondary_similarity
from services.filters import apply_filters
from services.gemini_client import GeminiClient
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
    matched_jobs: List[Job] = field(default_factory=list)
    excluded: List[dict] = field(default_factory=list)
    status: str = "error"
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    duration_seconds: float = 0.0

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
    evaluate_with_gemini: bool = True,
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

    scan_summary = engine.run_all(cfg, on_progress=_progress)
    for name, result in scan_summary.per_source.items():
        db.record_source_health(
            session=session,
            source_name=name,
            source_url=(result.listings[0].original_url if result.listings else None),
            listings_found=result.listings_found,
            error_message=result.error_message,
            is_enabled=True,
        )
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
        # Look up by fingerprint
        existing_row, created = db.get_or_create_job_by_fingerprint(session, fp, **norm)
        if created:
            new_jobs += 1
        else:
            # Update last-seen without erasing application history
            existing_row.last_seen_at = utcnow()
            existing_row.status = "OPEN"
            duplicates += 1
            session.commit()
        eligible.append(existing_row)

    summary.total_discovered = len(eligible)
    summary.new_jobs = new_jobs
    summary.duplicates = duplicates

    # Step 3: deterministic filters
    matched, excluded = apply_filters(eligible, cfg)
    summary.matched_jobs = matched
    summary.excluded = [
        {"job_id": j.id, "title": j.title, "company": j.company, "reason": r}
        for (j, r) in excluded
    ]

    # Step 4: (optional) Gemini evaluation
    if evaluate_with_gemini:
        profile = db.get_active_candidate_profile(session)
        if profile is not None:
            # Refresh jobs (session.commit lazy)
            from services.matching import evaluate_jobs

            evaluate_jobs(matched, profile, on_progress=_progress)

    # Step 5: finish scan record
    summary.status = "done" if scan_summary.status in ("done", "partial") else "error"
    if scan_summary.failures:
        summary.status = "partial" if scan_summary.total_discovered else "error"
    summary.failures = list(scan_summary.failures)
    summary.per_source = {
        n: r.details() for n, r in scan_summary.per_source.items()
    }
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
    )
    session.commit()
    session.close()
    return summary


def list_enabled(cfg: SearchConfig) -> List[str]:
    if cfg.enabled_sources:
        return list(cfg.enabled_sources)
    return [s.name for s in list_sources()]

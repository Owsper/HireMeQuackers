"""Post-discovery detail-page enrichment.

Fills in missing Job fields (description, salary, location, application
URL, ...) by fetching each missing job's original_url and running
:func:`services.detail_extractor.fetch_and_extract`.

Design:
- NOT a discovery JobSource — it operates on already-stored rows.
- Rate-limited between fetches (per-domain not required; global min delay).
- Never raises: a single failed fetch is logged + skipped.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import List, Optional

from config.search_config import SearchConfig
from database import repository as db
from services.detail_extractor import ExtractedDetail, fetch_and_extract
from config.settings import get_config
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass
class EnrichmentSummary:
    attempted: int = 0
    succeeded: int = 0
    skipped: int = 0
    failed: int = 0
    avg_confidence: float = 0.0
    confidence_sum: float = field(default=0.0, repr=False)

    def records(self) -> List[ExtractedDetail]:
        return []


def enrich_missing_jobs(
    session,
    cfg: SearchConfig,
    *,
    job_ids: Optional[List[int]] = None,
    max_per_scan: Optional[int] = None,
    delay_seconds: Optional[float] = None,
    client=None,
) -> EnrichmentSummary:
    """Fetch detail pages and store the best non-empty extracted fields.

    ``job_ids`` (when given) narrows the target set; otherwise all jobs
    with a missing description are considered.
    """
    s = EnrichmentSummary()
    cap = max_per_scan if max_per_scan is not None else getattr(cfg, "enrichment_max_per_scan", 25)
    delay = delay_seconds if delay_seconds is not None else getattr(cfg, "enrichment_delay_seconds", 1.5)

    if job_ids is not None:
        from database.models import Job
        if not job_ids:
            jobs = []
        else:
            jobs = session.query(Job).filter(Job.id.in_(job_ids)).all()
    else:
        jobs = db.list_jobs_needing_enrichment(session, limit=cap)
    # Apply TTL before the cap so recently failed jobs do not starve later rows.
    jobs = [j for j in jobs if extraction_due(j)][:cap]

    last_fetch = 0.0
    for job in jobs:
        s.attempted += 1
        url = job.original_url
        if not url:
            s.skipped += 1
            continue
        # Rate limit
        wait = delay - (time.monotonic() - last_fetch)
        if wait > 0:
            time.sleep(wait)
        try:
            detail = fetch_and_extract(url, client=client)
        except Exception as exc:
            logger.warning("Enrichment extraction failed for job id=%s type=%s", job.id, type(exc).__name__)
            detail = ExtractedDetail(extraction_errors=[f"extraction_failed:{type(exc).__name__}"])
        last_fetch = time.monotonic()
        try:
            updated = db.update_job_from_enrichment(session, job.id, detail)
        except Exception as exc:
            session.rollback()
            logger.warning("Enrichment storage failed for job id=%s type=%s", job.id, type(exc).__name__)
            s.failed += 1
            continue
        if detail.extraction_confidence <= 0.0 or not detail.description_text:
            s.failed += 1
            continue
        if updated:
            s.succeeded += 1
            s.confidence_sum += float(detail.extraction_confidence) or 0.0
        else:
            s.skipped += 1
    if s.succeeded:
        s.avg_confidence = s.confidence_sum / s.succeeded
    return s


def extraction_due(job, *, now=None) -> bool:
    cfg = get_config()
    timestamp = getattr(job, "extracted_at", None)
    if timestamp is None:
        return True
    timestamp = timestamp.replace(tzinfo=timezone.utc) if timestamp.tzinfo is None else timestamp
    age = ((now or datetime.now(timezone.utc)) - timestamp).total_seconds() / 3600
    has_errors = getattr(job, "extraction_errors_json", None) not in (None, "", "[]")
    return age >= (cfg.extraction_refresh_hours if getattr(job, "extraction_status", None) == "COMPLETE" and not has_errors else cfg.extraction_retry_hours)

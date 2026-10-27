"""Deduplication + similarity heuristic for stored jobs.

Primary key: `content_fingerprint`. Secondary: company + title Jaccard across
title tokens, with location/employment/arrangement straddling.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

from database.models import Job, utcnow
from services.normalization import canonical_url, content_fingerprint


_TOKEN_RE = re.compile(r"[^a-z0-9]+", re.I)


def _tokens(text: str) -> set:
    out = set()
    for tok in _TOKEN_RE.split((text or "").lower()):
        tok = tok.strip()
        if tok and len(tok) >= 2:
            out.add(tok)
    return out


def primary_hard_match(job: Job, candidate: Dict) -> bool:
    """Same content_fingerprint -> definitely the same listing."""
    return bool(
        job.content_fingerprint
        and candidate.get("content_fingerprint")
        and job.content_fingerprint == candidate["content_fingerprint"]
    )


def secondary_similarity(candidate: Dict, jobs: Iterable[Job]) -> Optional[Tuple[Job, float]]:
    """Return (job, score) for the best secondary match.

    Scoring:
      score = 0.55 * company_token_jaccard
            + 0.30 * title_token_jaccard
            + 0.15 * workplace_text_jaccard
    ---
    We do NOT merge across different (title + company) pairs unless the
    overlap is at least 80% (Jaccard >= 0.80) OR same canonical URL with
    minor title variation.
    """
    best: Optional[Tuple[Job, float]] = None
    cand_comp_tokens = _tokens(candidate.get("company", ""))
    cand_title_tokens = _tokens(candidate.get("title", ""))
    cand_arr = (candidate.get("workplace_arrangement") or "").lower().strip()
    cand_url = canonical_url(candidate.get("original_url"))
    for job in jobs:
        if job.id == candidate.get("_job_id"):
            continue
        job_comp_tokens = _tokens(job.company or "")
        job_title_tokens = _tokens(job.title or "")
        job_arr = (job.workplace_arrangement or "").lower().strip()
        job_url = canonical_url(job.original_url)
        comp_j = _jaccard(cand_comp_tokens, job_comp_tokens)
        title_j = _jaccard(cand_title_tokens, job_title_tokens)
        arr_j = 1.0 if (cand_arr and job_arr == cand_arr) else 0.5 if (not cand_arr and not job_arr) else 0.2
        score = 0.55 * comp_j + 0.30 * title_j + 0.15 * arr_j
        # Don't merge same company+title across different URLs unless a strong URL overlap
        same_url = bool(cand_url and job_url and cand_url == job_url)
        # Block if both look different enough
        if score < 0.75 and not same_url:
            continue
        if cand_comp_tokens and job_comp_tokens and comp_j < 0.80 and not same_url:
            continue
        if best is None or score > best[1]:
            best = (job, score)
    return best


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 1.0
    return len(a & b) / len(union)


def is_stale(job: Job, *, now: Optional[datetime] = None) -> bool:
    """Consider stale when `first_discovered_at` is older than 90 days and
    `status == 'OPEN'`. (No explicit 'closing_at' info we can trust.)"""
    now = now or utcnow()
    if job.status != "OPEN":
        return False
    discovered = job.first_discovered_at
    if not discovered:
        return False
    return (now - discovered).days > 90


def is_expiring(job: Job, *, ahead: int = 7, now: Optional[datetime] = None) -> bool:
    now = now or utcnow()
    if job.closing_at is None:
        return False
    delta = (job.closing_at - now).days
    return 0 <= delta <= ahead


def is_unreachable(job: Job) -> bool:
    """Mark unreachable when last_seen is longer than 30 days ago."""
    if not job.last_seen_at:
        return False
    return (utcnow() - job.last_seen_at).days > 30

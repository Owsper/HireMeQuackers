"""Deterministic filters over normalized jobs.

Unknown fields are treated as `does not pass` so we never make up data.
"""
from __future__ import annotations

import datetime as _dt
import re
from datetime import datetime, timedelta
from typing import Iterable, List, Optional, Tuple

from config.search_config import SearchConfig
from database.models import Job


def _parse_year(text: Optional[str]) -> Optional[int]:
    if not text:
        return None

    m = re.search(r"(\d{4})", str(text))
    if not m:
        return None
    y = int(m.group(1))
    if 1900 <= y <= 2100:
        return y
    return None


def apply_filters(
    jobs: Iterable[Job], cfg: SearchConfig, *, now: Optional[datetime] = None
) -> Tuple[List[Job], List[Tuple[Job, str]]]:
    """Return (matched_jobs, excluded_jobs_with_reason)."""
    now = now or _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    now_tz = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)

    included = []
    excluded: List[Tuple[Job, str]] = []

    for job in jobs:
        item, reason = _check_one(job, cfg, now_tz)
        if item is not None:
            included.append(item)
        else:
            excluded.append((job, reason))
    return included, excluded


def _check_one(
    job: Job, cfg: SearchConfig, now_tz
) -> Tuple[Optional[Job], str]:
    # employment types
    if cfg.employment_types:
        et = (job.employment_type or "").lower()
        allowed = {e.value for e in cfg.employment_types}
        if et and et not in allowed:
            return None, f"employment_type mismatch: {et} not in {sorted(allowed)}"
        # unknown is allowed when user did not restrict
    # exclude / include keywords
    descriptor = (
        (job.title or "") + " " + (job.company or "") + " " + (job.location or "") + " " + (job.description or "")
    ).lower()
    for k in [k.lower().strip() for k in (cfg.exclude_keywords or []) if k]:
        if k and k in descriptor:
            return None, f"excluded keyword: {k!r}"
    if cfg.include_keywords:
        if not any(k.lower().strip() in descriptor for k in cfg.include_keywords if k and k.strip()):
            return None, "missing required keyword(s)"
    # phrases
    if cfg.phrases:
        if not any(p.lower().strip() in descriptor for p in cfg.phrases if p and p.strip()):
            return None, "missing required phrase(s)"
    # locations
    loc = (job.location or "").lower()
    title_loc = (job.title or "").lower()
    desc = (job.description or "").lower()
    if cfg.locations:
        if not any(
            (l.lower().strip() in loc) or (l.lower().strip() in title_loc) or (l.lower().strip() in desc)
            for l in cfg.locations if l and l.strip()
        ):
            return None, "location not in requested places"
    if cfg.excluded_locations:
        if any(
            l.lower().strip() in loc
            for l in cfg.excluded_locations if l and l.strip()
        ):
            return None, "location excluded"
    # workplace arrangement
    if any([cfg.include_remote, cfg.include_hybrid, cfg.include_onsite, cfg.worldwide_remote]):
        arr = (job.workplace_arrangement or "").lower().strip()
        allowed_ss = set()
        if cfg.include_remote:
            allowed_ss.add("remote")
        if cfg.worldwide_remote:
            allowed_ss.add("worldwide")
        if cfg.include_hybrid:
            allowed_ss.add("hybrid")
        if cfg.include_onsite:
            allowed_ss.add("onsite")
        if arr:
            if not any(s in arr for s in allowed_ss):
                return None, f"workplace '{arr}' not in requested arrangements"
        # else unknown -> pass
    # posted within days
    if cfg.posted_within_days and job.published_at:
        if now_tz - job.published_at > timedelta(days=cfg.posted_within_days):
            return None, "outside posted-within window"
    # salary thresholds (only when both parseable numeric)
    if cfg.salary_min is not None and job.salary:
        v = _salary_value(job.salary)
        if v is not None and v < cfg.salary_min:
            return None, f"salary {v} below minimum"
    if cfg.salary_max is not None and job.salary:
        v = _salary_value(job.salary)
        if v is not None and v > cfg.salary_max:
            return None, f"salary {v} above maximum"
    return job, ""


def _salary_value(text: Optional[str]) -> Optional[float]:
    """Extract first 1-7 digit number that looks like a salary."""
    import re

    if not text:
        return None
    m = re.search(r"(\d[\d,\.]{2,7})", text)
    if not m:
        return None
    s = m.group(1)
    s = s.replace(",", "").replace(".", "").strip()
    try:
        return float(s)
    except ValueError:
        return None

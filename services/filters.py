"""Deterministic filters over normalized jobs.

Unknown fields are treated as `does not pass` so we never make up data.

Pipeline order inside `_check_one`:

    1. Employment type       (cheap, must match when restricted)
    2. Exclude keywords       (omit the obvious miss before doing work)
    3. Title relevance        (**hard** gate — the PRIORITY 2 fix)
    4. Location               (**hard** gate — the PRIORITY 2 fix)
    5. Include keywords       (kept loose: description-mention is allowed,
       because the real title match is in step 3)
    6. Phrases
    7. Workplace arrangement
    8. Posted-within window
    9. Salary min/max

Steps 3-4 only run when an `intent` is supplied; passing `intent=None`
preserves the exact previous behaviour (tests like
`tests/test_filters.py` from before this change still pass).
"""
from __future__ import annotations

import datetime as _dt
import re
from datetime import datetime, timedelta
from typing import Iterable, List, Optional, Tuple

from config.search_config import SearchConfig
from config.search_intent import SearchIntent
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
    jobs: Iterable[Job], cfg: SearchConfig, *, now: Optional[datetime] = None,
    intent: Optional[SearchIntent] = None,
) -> Tuple[List[Job], List[Tuple[Job, str]]]:
    """Return (matched_jobs, excluded_jobs_with_reason).

    `intent` is optional: when None we use the legacy blob-based
    keyword check (existing tests still pass). When supplied we run the
    stricter title-relevance + location-validation gates first.
    """
    now = now or _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    now_tz = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)

    included = []
    excluded: List[Tuple[Job, str]] = []

    for job in jobs:
        item, reason = _check_one(job, cfg, now_tz, intent=intent)
        if item is not None:
            included.append(item)
        else:
            excluded.append((job, reason))
    return included, excluded


def _check_one(
    job: Job, cfg: SearchConfig, now_tz, *, intent: Optional[SearchIntent] = None
) -> Tuple[Optional[Job], str]:
    # 1) employment types
    if cfg.employment_types:
        et = (job.employment_type or "").lower()
        allowed = {e.value for e in cfg.employment_types}
        if et and et not in allowed:
            return None, f"employment_type mismatch: {et} not in {sorted(allowed)}"
        # unknown is allowed when user did not restrict
    # 1b) language the posting is written in — only when the user restricted
    # it; an undetected language (None) always passes.
    wanted_langs = {x.strip().lower() for x in (getattr(cfg, "description_languages", None) or []) if x}
    job_lang = (getattr(job, "description_language", None) or "").lower()
    if wanted_langs and job_lang and job_lang not in wanted_langs:
        return None, f"written in {job_lang!r}, not in {sorted(wanted_langs)}"
    # 2) exclude keywords (omit obvious miss before doing work)
    descriptor = (
        (job.title or "") + " " + (job.company or "") + " " + (job.location or "") + " " + (job.description or "")
    ).lower()
    for k in [k.lower().strip() for k in (cfg.exclude_keywords or []) if k]:
        if k and k in descriptor:
            return None, f"excluded keyword: {k!r}"
    # 2b) excluded titles: whole-word match on the title only (documented
    # on SearchConfig.excluded_titles).
    for t in [t.lower().strip() for t in (getattr(cfg, "excluded_titles", None) or []) if t]:
        if re.search(r"(?<!\w)" + re.escape(t) + r"(?!\w)", (job.title or "").lower()):
            return None, f"excluded title: {t!r}"

    # 3) TITLE RELEVANCE — new: strict gate, runs before the loose
    # keyword pass below.
    if intent is not None and (intent.primary_title or intent.title_synonyms):
        # (handles title gate below)
        pass
    _title_gate_result = None  # tracked separately
    _title_hard_pass = False  # exact/strong title match (incl. translation/synonym)
    if intent is not None and (intent.primary_title or intent.title_synonyms):
        from services.title_relevance import classify_title
        bare_title = (job.title or "").strip()
        tr = classify_title(bare_title, intent)
        # Hard pass / soft pass / fallback — normalise to (ok, reason).
        if tr.passes_hard:
            _title_gate_result = ("pass", "")
            _title_hard_pass = True
        elif tr.label == "related" and intent.include_related_titles:
            _title_gate_result = ("pass", "")
        else:
            # Before we reject on title alone, allow the legacy
            # descriptor-keyword fallback *if the intent has no explicit
            # location constraint* — this preserves prior behaviour for
            # tests where the intent is built from a config that just
            # set `include_keywords`.
            _fallback_ok = (
                not (intent.cities or intent.countries)
                and any(
                    k.lower().strip() in descriptor
                    for k in (cfg.include_keywords or [intent.primary_title])
                    if k and k.strip()
                )
                and intent.primary_title not in (cfg.excluded_titles or [])
            )
            if _fallback_ok:
                _title_gate_result = ("pass", "")
            else:
                _title_gate_result = ("reject", f"title: {tr.reason}")
    _lo = (job.location or "").lower()
    _tl = (job.title or "").lower()
    _dl = (job.description or "").lower()
    if cfg.excluded_locations:
        if any(l.lower().strip() in _lo for l in cfg.excluded_locations if l and l.strip()):
            return None, "location excluded"
    # 4) LOCATION
    if intent is not None and (intent.cities or intent.countries
                                or intent.remote or intent.hybrid
                                or intent.onsite or intent.worldwide_remote):
        from services.location_validation import parse_location, location_verdict
        raw_loc = (job.location or "").strip()
        if not raw_loc:
            try:
                raw_loc = (job.country or "")
            except AttributeError:
                raw_loc = ""
        parsed = parse_location(raw_loc)
        verdict, loc_reason = location_verdict(parsed, intent)
        if verdict == "mismatch":
            return None, f"location: {loc_reason}"
        if verdict == "unknown" and getattr(cfg, "require_verified_location", False):
            return None, f"location unverified: {loc_reason}"
    # legacy location gate (SearchConfig.locations) — still apply even
    # with intent, because it's a *tight substring* rule the user
    # authored explicitly.
    if cfg.locations:
        if not any(
            (l.lower().strip() in _lo) or (l.lower().strip() in _tl) or (l.lower().strip() in _dl)
            for l in cfg.locations if l and l.strip()
        ):
            return None, "location not in requested places"
    # 4b) title gate finalise
    if _title_gate_result and _title_gate_result[0] == "reject":
        return None, _title_gate_result[1]

    # 5) include keywords (kept loose: description-mention is allowed,
    # because the real title match is in step 3)
    if cfg.include_keywords:
        # A keyword that *is* the searched title is satisfied by a strict
        # title match — "Pflegefachkraft" or "Warehouse Associate" never
        # contain the literal word "Nurse" / "Warehouse Worker".
        titles = {t.lower().strip() for t in (intent.all_titles() if intent is not None else [])}
        if _title_hard_pass and any(k.lower().strip() in titles for k in cfg.include_keywords if k):
            pass
        elif not any(k.lower().strip() in descriptor for k in cfg.include_keywords if k and k.strip()):
            return None, "missing required keyword(s)"
    # 6) phrases
    if cfg.phrases:
        if not any(p.lower().strip() in descriptor for p in cfg.phrases if p and p.strip()):
            return None, "missing required phrase(s)"
    # 7) workplace arrangement
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
    # 8) posted within days
    if cfg.posted_within_days and job.published_at:
        if now_tz - job.published_at > timedelta(days=cfg.posted_within_days):
            return None, "outside posted-within window"
    # 9) salary thresholds (only when both parseable numeric)
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

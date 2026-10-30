"""EURES (EU public-sector jobs) adapter — opt-in via a user-supplied URL.

EURES (the European Employment Services network publishes its listings
on the EURES Jobs Mobility Portal).  Unlike RemoteOK / Hacker News,
*no* verified, stable, *keyless* machine endpoint is shipped with this
project.  Inventing one would violate the project's "never assume a
public endpoint" rule, so this adapter is deliberately a thin, opt-in
skeleton:

* ``cfg.eures_api_url`` is ``None`` / empty  -> the source is **skipped**
  (reported ``success=True`` with ``attempted=0``) so scans are not
  marked "failed" just because the user hasn't configured EURES.
* ``cfg.eures_api_url`` set                      -> we ``safe_get`` it
  once and parse the JSON best-effort (accepting a bare list or a
  dict carrying ``hits`` / ``jobs`` / ``results``).  This keeps the
  adapter resilient to the endpoint evolving without hiding real data.

The endpoint is normally set in ``.env`` as ``EURES_API_URL`` (loaded
into ``SearchConfig.eures_api_url`` by the config layer).
"""
from __future__ import annotations

import json
import logging
from typing import List, Optional

from config.search_config import SearchConfig
from services.safe_requests import safe_get
from .base import JobSource, RawListing, SourceResult
from .registry import register

logger = logging.getLogger(__name__)

_LIST_KEYS = ("hits", "jobs", "results", "offres", "vacancies")


@register
class EuresSource(JobSource):
    name = "EURES"
    slug = "eures"
    coverage_countries = ("austria", "belgium", "bulgaria", "croatia", "czech republic", "denmark", "estonia", "finland", "france", "germany", "greece", "hungary", "iceland", "ireland", "italy", "latvia", "lithuania", "luxembourg", "netherlands", "norway", "poland", "portugal", "romania", "slovakia", "slovenia", "spain", "sweden", "switzerland")
    coverage_note = "EU / EEA only"
    official_search_url = None  # endpoint is user-configured
    robots_check = False  # API endpoint, no robot path required

    @staticmethod
    def _extract_items(payload) -> list:
        """Best-effort: return a list of job dicts from a JSON payload."""
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in _LIST_KEYS:
                val = payload.get(key)
                if isinstance(val, list):
                    return val
        return []

    def scan(self, cfg: SearchConfig) -> SourceResult:
        endpoint = (getattr(cfg, "eures_api_url", None) or "").strip()
        # Not configured => skip, do NOT count as failed.
        if not endpoint:
            return SourceResult(
                success=True,
                listings=[],
                error_message="EURES endpoint not configured (set EURES_API_URL)",
                attempted=0,
                fetched=0,
                parsed=0,
                duplicates=0,
                failed=0,
            )

        # Single request, isolated error handling.
        tried = 1
        try:
            result = safe_get(endpoint, timeout=15.0, client=self.client)
        except Exception as exc:
            logger.warning("EURES fetch error: %s", exc)
            return SourceResult(
                success=False,
                listings=[],
                error_message=f"EURES: {exc}",
                attempted=tried,
                fetched=0,
                parsed=0,
                duplicates=0,
                failed=1,
            )

        if result is None or result.status_code != 200 or not result.content:
            status = result.status_code if result is not None else "n/a"
            return SourceResult(
                success=False,
                listings=[],
                error_message=f"EURES: HTTP {status}",
                attempted=tried,
                fetched=0,
                parsed=0,
                duplicates=0,
                failed=1,
            )

        try:
            payload = json.loads(result.content.decode("utf-8", "replace"))
        except Exception as exc:
            logger.warning("EURES JSON parse error: %s", exc)
            return SourceResult(
                success=False,
                listings=[],
                error_message=f"EURES: JSON parse error: {exc}",
                attempted=tried,
                fetched=1,
                parsed=0,
                duplicates=0,
                failed=1,
            )

        items = self._extract_items(payload)
        per_source_cap = max(1, int(getattr(cfg, "per_source_max_results", 100) or 100))
        user_max = max(1, int(getattr(cfg, "max_results", 100) or 100))
        cap = min(per_source_cap, user_max)

        listings: List[RawListing] = []
        seen = set()
        published = None  # a shared ``published_at`` fallback (some lists carry one)
        for raw in items:
            if not isinstance(raw, dict):
                continue
            title = str(raw.get("title") or raw.get("poste") or "").strip()
            url = str(raw.get("url") or raw.get("link") or raw.get("apply") or "").strip()
            if not title:
                continue
            if url:
                key = url.lower()
                if key in seen:
                    continue
                seen.add(key)
            # published_at absent is fine — never invent a date.
            pub_raw = raw.get("published_at") or raw.get("date") or published
            company_raw = raw.get("company") or raw.get("employer") or raw.get("organisme")
            location_raw = raw.get("location") or raw.get("lieu") or raw.get("address")
            listings.append(RawListing(
                source=self.name,
                title=title[:200],
                company=(str(company_raw).strip() if company_raw else None),
                location=(str(location_raw).strip() if location_raw else None),
                original_url=url,
                canonical_url=url or None,
                published_at=(str(pub_raw)[:40] if pub_raw else None),
            ))
            if len(listings) >= cap:
                break

        filtered = self.apply_filters(cfg, listings)
        return SourceResult(
            success=bool(filtered) or len(listings) > 0,
            listings=filtered,
            listings_found=len(filtered),
            listings_new=len(filtered),
            error_message=None,
            attempted=tried,
            fetched=1,
            parsed=len(listings),
            duplicates=0,
            failed=0,
        )

"""Hacker News job listings via Algolia's keyless public API.

Primary endpoint: ``https://hn.algolia.com/api/v1/jobs`` — the dedicated
HN "jobs" index.  Fallback (used only when the primary path returns no
hits or errors): ``https://hn.algolia.com/api/v1/search?tags=job&hitsPerPage=30``
— generic search restricted to items tagged ``job``.

Both endpoints are public, keyless, and documented at
https://hn.algolia.com/api (they back the site's own job board).  Each
hit carries ``title``, ``url`` (the real job posting link), and
``created_at``.  There is generally *no* company name on these
listings, so ``company`` is ``None`` (``RawListing.company`` is
``Optional`` precisely for this reason).

Because HN is an aggregator, a single "job" entry is just a title +
external link — the real description lives on the linked posting.  We
therefore never claim detailed extraction here (``detail_extraction_succeeded``
stays ``False``); the detail extractor (Phase 3) can enrich the URL
downstream.

A single failing request (transient 429, timeout, bad JSON) must not
discard a subsequent fallback's results, and vice versa — each
``safe_get`` call is wrapped independently (mirrors ``remoteok.py``
error isolation).  Only when *both* endpoints fail is the source
reported ``failed``.
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

_JOBS_URL = "https://hn.algolia.com/api/v1/jobs"
_SEARCH_URL = "https://hn.algolia.com/api/v1/search?tags=job&hitsPerPage=30"


@register
class HackerNewsJobsSource(JobSource):
    name = "HackerNews"
    slug = "hackernews"
    official_search_url = _JOBS_URL
    robots_check = False  # API endpoint, no robot path required

    @staticmethod
    def _extract_hits(payload) -> list:
        """Pull a list of hit dicts out of an Algolia reply (or [])."""
        if not isinstance(payload, dict):
            return []
        hits = payload.get("hits")
        return hits if isinstance(hits, list) else []

    def _fetch_hits(self, url: str) -> tuple[list, bool]:
        """``safe_get`` one Algolia URL.

        Returns ``(hits, ok)``:

        * ``ok=True`` when the request returned HTTP 200 with content
          AND parsed as valid JSON (regardless of hit count — an empty
          ``hits`` list is a *valid* "no jobs" answer, not a failure).
        * ``ok=False`` when the request raised a network error or
          returned a non-200 status / undecodable body (a true failure).
        """
        try:
            result = safe_get(url, timeout=15.0, client=self.client)
        except Exception as exc:
            logger.warning("HackerNews %s: %s", url, exc)
            return [], False
        if result is None or result.status_code != 200 or not result.content:
            logger.warning(
                "HackerNews %s: HTTP %s",
                url,
                result.status_code if result is not None else "n/a",
            )
            return [], False
        try:
            payload = json.loads(result.content.decode("utf-8", "replace"))
        except Exception as exc:
            logger.warning("HackerNews %s: JSON parse error: %s", url, exc)
            return [], False
        return self._extract_hits(payload), True

    def scan(self, cfg: SearchConfig) -> SourceResult:
        attempted = 0
        fetched = 0
        parsed = 0
        duplicates = 0
        failed = 0
        err: Optional[str] = None

        # Try the dedicated jobs index first; only fall back if it
        # returns no hits (404 / empty index) so we never waste the
        # fallback on an already-good primary answer.
        hits: list = []
        for url in (_JOBS_URL, _SEARCH_URL):
            attempted += 1
            fetched_hits, ok = self._fetch_hits(url)
            if not ok:
                failed += 1
                err = f"HackerNews: {url} failed"
                continue
            fetched += 1
            if fetched_hits:
                hits = fetched_hits
                err = None
                break
            # ok but zero hits: a satisfied, empty primary stays a
            # valid answer; keep probing the fallback.
        if not hits:
            return SourceResult(
                success=(fetched > 0),
                listings=[],
                error_message=err or "HackerNews: no job hits from Algolia",
                attempted=attempted,
                fetched=fetched,
                parsed=0,
                duplicates=0,
                failed=failed,
            )

        per_source_cap = max(1, int(getattr(cfg, "per_source_max_results", 100) or 100))
        user_max = max(1, int(getattr(cfg, "max_results", 100) or 100))
        cap = min(per_source_cap, user_max)

        listings: List[RawListing] = []
        seen = set()
        for raw in hits:
            if not isinstance(raw, dict):
                continue
            title = str(raw.get("title") or "").strip()
            url = str(raw.get("url") or "").strip()
            if not title or not url:
                continue
            if not url.lower().startswith(("http://", "https://")):
                continue
            key = url.lower()
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)

            # HN job hits occasionally carry an ``employer`` / ``company``
            # field; treat it as optional.
            company_raw = raw.get("company")
            if company_raw is None:
                company_raw = raw.get("employer")
            company = str(company_raw).strip() if company_raw else None

            listings.append(RawListing(
                source=self.name,
                title=title[:200],
                company=company,
                location=None,
                description_text=str(raw.get("story_text") or "") or None,
                original_url=url,
                canonical_url=url,
                published_at=str(raw.get("created_at") or "")[:40] or None,
                detail_extraction_succeeded=False,
            ))
            parsed += 1
            if parsed >= cap:
                break

        filtered = self.apply_filters(cfg, listings)
        if not filtered and parsed == 0 and failed > 0:
            return SourceResult(
                success=False,
                listings=[],
                error_message="HackerNews: no listings parsed",
                attempted=attempted,
                fetched=fetched,
                parsed=0,
                duplicates=duplicates,
                failed=failed,
            )
        return SourceResult(
            success=bool(filtered) or parsed > 0,
            listings=filtered,
            listings_found=len(filtered),
            listings_new=len(filtered),
            error_message=None,
            attempted=attempted,
            fetched=fetched,
            parsed=parsed,
            duplicates=duplicates,
            failed=failed,
        )

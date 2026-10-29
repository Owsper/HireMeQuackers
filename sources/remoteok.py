"""RemoteOK JSON API adapter (no key required).

RemoteOK publishes its live listings at ``https://remoteok.com/api`` as
JSON.  The response is a list where each item carries:

    id, title, company, location, tags (list of {"name": ...}),
    description (HTML text), company_logo, salary, created_at, url

This is a well-known, stable, keyless endpoint (it has dated back to the
site's early days).  The feed includes full listings, so the returned
:class:`RawListing` objects carry a ``description_text`` and downstream
matching can operate on real content instead of just the title.

A single HTTP 404 (site moving to a different CDN, temporary
rate-limit, etc) is treated as a *failed sub-request* — the source is
still considered successful so long as at least one listing parsed.
Per-spec, a failed source must not discard the results of the others.
"""
from __future__ import annotations

import json
import logging
import re
from typing import List, Optional

from config.search_config import SearchConfig
from services.safe_requests import safe_get
from services.sanitization import strip_html
from .base import JobSource, RawListing, SourceResult
from .registry import register

logger = logging.getLogger(__name__)

_API_URL = "https://remoteok.com/api"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# RemoteOK ``^url`` values are already clean absolute URLs
# (e.g. ``https://remoteok.com/l/1234``) — verify we don't leak
# relative fragments.
_REMOTE_OK_PAGE = re.compile(r"^https?://", re.I)


def _maybe_salary(value) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


@register
class RemoteOkSource(JobSource):
    name = "RemoteOK"
    slug = "remoteok"
    official_search_url = _API_URL
    robots_check = False  # API endpoint, no robot path required

    def scan(self, cfg: SearchConfig) -> SourceResult:
        attempted = 1
        fetched = 0
        parsed = 0
        duplicates = 0
        failed = 0

        # try up to 2 times (1 retry) per the spec
        result = None
        err: Optional[str] = None
        for attempt in (1, 2):
            try:
                result = safe_get(_API_URL, timeout=15.0, client=self.client)
                err = None
                if (result and result.status_code == 200 and result.content):
                    break
                err = f"HTTP {result.status_code if result else 'n/a'}"
            except Exception as exc:
                err = str(exc)
            logger.warning("RemoteOK attempt %d: %s", attempt, err)

        if result is None:
            failed = 1
            return SourceResult(
                success=False,
                listings=[],
                error_message=f"RemoteOK: {err or 'no result'}",
                attempted=attempted,
                fetched=0,
                parsed=0,
                duplicates=0,
                failed=failed,
            )

        if result.status_code != 200 or not result.content:
            failed = 1
            return SourceResult(
                success=False,
                listings=[],
                error_message=f"RemoteOK: HTTP {result.status_code}",
                attempted=attempted,
                fetched=0,
                parsed=0,
                duplicates=0,
                failed=failed,
            )

        fetched = 1
        try:
            payload = json.loads(result.content.decode("utf-8", "replace"))
        except Exception as exc:
            failed += 1
            return SourceResult(
                success=False,
                listings=[],
                error_message=f"RemoteOK: JSON parse error: {exc}",
                attempted=attempted,
                fetched=fetched,
                parsed=0,
                duplicates=0,
                failed=failed,
            )

        # RemoteOK returns a JSON list (not {jobs: [...]})
        items = payload if isinstance(payload, list) else (payload.get("jobs") or [])
        if not isinstance(items, list):
            items = []

        per_source_cap = max(1, int(getattr(cfg, "per_source_max_results", 100) or 100))
        user_max = max(1, int(getattr(cfg, "max_results", 100) or 100))
        cap = min(per_source_cap, user_max)

        listings: List[RawListing] = []
        seen = set()
        for raw in items:
            if not isinstance(raw, dict):
                continue
            # The live API names the job title "position"; "title" is kept
            # as a fallback for older payloads / test fixtures.
            title = str(raw.get("position") or raw.get("title") or "").strip()
            url = str(raw.get("url") or "").strip()
            if not title or not url:
                continue
            if not _REMOTE_OK_PAGE.match(url):
                continue
            key = url.lower()
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)

            desc_html = str(raw.get("description") or "")
            emp_type = None
            if raw.get("tags"):
                tags = raw["tags"]
                if isinstance(tags, list) and tags:
                    first = tags[0].get("name") if isinstance(tags[0], dict) else None
                    if first:
                        from config.search_config import classify_employment_type
                        emp_type = classify_employment_type(str(first))

            listings.append(RawListing(
                source=self.name,
                title=title[:200],
                company=str(raw.get("company") or "Unknown").strip() or "Unknown",
                location=str(raw.get("location") or "").strip() or None,
                description_text=strip_html(desc_html) if desc_html else None,
                employment_type=emp_type,
                workplace_arrangement="remote",
                salary=_maybe_salary(raw.get("salary"))
                or _maybe_salary(raw.get("ranges")),
                original_url=url,
                canonical_url=url,
                published_at=str(raw.get("created_at") or "")[:30] or None,
                detail_extraction_succeeded=bool(desc_html),
            ))
            parsed += 1
            if len(listings) >= cap:
                break

        filtered = self.apply_filters(cfg, listings)
        if not filtered and parsed == 0 and failed > 0:
            return SourceResult(
                success=False,
                listings=[],
                error_message="RemoteOK: no listings parsed",
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

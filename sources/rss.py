"""RSS / Atom feed adapter (feedparser).

This source will scan:

    (user's own ``cfg.rss_urls``)
  UNION
    (feeds from :data:`services.feed_registry.FEED_REGISTRY`
             subset of those whose category appears in
             ``cfg.feed_registry_categories`` — empty/None = all).

So if the user has nothing configured, the *default* set of remote job
feeds (WWR + RemoteOK) is used automatically.  If the user supplies a
partial category list (e.g. only ``["european"]``), only feeds tagged
with that category ship in.

Per-feed counts (attempted / fetched) are tracked and surfaced on
:class:`SourceResult`; one failing feed no longer masks the others.
"""
from __future__ import annotations

import logging
import time
from typing import List, Optional
from urllib.parse import quote

from config.search_config import EmploymentType, SearchConfig, classify_employment_type
from services.safe_requests import safe_get
from services.sanitization import strip_html
from services.feed_registry import get_feed_urls
from .base import JobSource, RawListing, SourceResult
from .registry import register

try:
    from feedparser import parse as _feedparser_parse
except Exception:  # optional; tests can mock
    _feedparser_parse = None  # type: ignore

logger = logging.getLogger(__name__)


def _resolve_feed_list(
    cfg: SearchConfig,
    explicit: Optional[List[dict]] = None,
) -> List[dict]:
    """Union of the user's feeds + the registry, deduped by URL.

    * ``explicit`` (constructor feeds) take precedence if given
      (back-compat with the legacy signature + tests that pass feeds).
    * If ``explicit`` is ``None`` and ``cfg.rss_urls`` is present,
      those take the *user* slot, the registry fills the rest.
    * Otherwise, the registry fills *the whole* slot.
    """
    user_urls = {
        (u or "").strip() for u in (getattr(cfg, "rss_urls", None) or []) if (u or "").strip()
    }
    seen = set()
    out: list[dict] = []

    # 1. Explicit constructor feeds (legacy override)
    if explicit:
        for entry in explicit:
            url = (entry.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            out.append({"name": entry.get("name") or url, "url": url})

    # 2. User's own cfg.rss_urls
    for url in user_urls:
        if url in seen:
            continue
        seen.add(url)
        out.append({"name": "User Feed", "url": url})

    # 3. Registry (filtered by cfg.feed_registry_categories)
    for entry in get_feed_urls(cfg.feed_registry_categories or None):
        url = entry.url
        if url in seen:
            continue
        seen.add(url)
        out.append({"name": entry.name, "url": url})

    return out


@register
class RSSFeedSource(JobSource):
    name = "RSS Feeds"
    slug = "rss"
    coverage_note = "depends on the feeds (defaults: remote + European feeds)"
    robots_check = True
    official_search_url: Optional[str] = None

    def __init__(self, feeds: List[dict] | None = None, **kwargs):
        super().__init__(**kwargs)
        # ``feeds`` is a legacy constructor override (tests + explicit
        # caller).  When None, :func:`_resolve_feed_list` will union the
        # per-cfg user URLs + the registry at scan time.
        self.feeds_override = feeds

    def scan(self, cfg: SearchConfig) -> SourceResult:
        from services.robots import is_allowed_by_robots

        if _feedparser_parse is None:
            return SourceResult(
                success=False,
                error_message="feedparser is not installed",
            )

        # Resolve the effective feed list for THIS cfg.  Honour any
        # explicit feeds list the constructor was given (back-compat),
        # otherwise the user's cfg.rss_urls + registry.
        if self.feeds_override is not None:
            feed_list: list[dict] = list(self.feeds_override)
        else:
            feed_list = _resolve_feed_list(cfg, explicit=None)

        attempted = 0
        fetched = 0
        parsed = 0
        duplicates = 0
        failed = 0

        listings: List[RawListing] = []
        errors: List[str] = []
        seen = set()
        for entry in feed_list:
            name = entry.get("name") or "Feed"
            url = entry.get("url")
            if not url:
                continue
            attempted += 1
            if self.robots_check and not is_allowed_by_robots(url, timeout=5.0):
                errors.append(f"{name}: robots disallowed")
                failed += 1
                continue
            try:
                result = safe_get(url, timeout=10.0, client=self.client)
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                failed += 1
                continue
            if result.status_code != 200 or not result.content:
                errors.append(f"{name}: HTTP {result.status_code or 'n/a'}")
                failed += 1
                continue
            fetched += 1
            try:
                parsed_feed = _feedparser_parse(result.content)
            except Exception as exc:
                errors.append(f"{name}: parse error: {exc}")
                failed += 1
                continue
            for e in parsed_feed.get("entries") or []:
                title = (e.get("title") or "").strip()
                link = (e.get("link") or "").strip()
                guid = e.get("id") or link
                if not title or not link:
                    continue
                digest = (link or "").lower() + "|" + (guid or "").lower()
                if digest in seen:
                    duplicates += 1
                    continue
                seen.add(digest)
                parsed += 1
                # parse updated / pubDate (best-effort)
                ts_raw = e.get("published") or e.get("updated")
                published_at = None
                if ts_raw:
                    try:
                        ts = time.mktime(e.get("published_parsed") or e.get("updated_parsed") or ())
                        published_at = str(int(ts))
                    except Exception:
                        published_at = str(ts_raw)[:25]
                desc = e.get("description") or e.get("summary")
                workplace = self._detect_workplace(title, link, desc or "")
                item = RawListing(
                    source=name,
                    title=title,
                    company="Unknown",
                    location=None,
                    description_text=strip_html(desc) if desc else None,
                    employment_type=classify_employment_type(title),
                    workplace_arrangement=workplace,
                    original_url=link,
                    application_url=link,
                    published_at=published_at,
                )
                listings.append(item)

        filtered = self.apply_filters(cfg, listings)
        if errors and not filtered:
            return SourceResult(
                success=False,
                listings=[],
                error_message=" | ".join(errors),
                attempted=attempted,
                fetched=fetched,
                parsed=parsed,
                duplicates=duplicates,
                failed=failed,
            )
        return SourceResult(
            success=bool(filtered),
            listings=filtered,
            listings_found=len(filtered),
            listings_new=len(filtered),
            error_message=" | ".join(errors) if errors else None,
            attempted=attempted,
            fetched=fetched,
            parsed=parsed,
            duplicates=duplicates,
            failed=failed,
        )

    @staticmethod
    def _detect_workplace(title: str, link: str, content: str) -> str:
        low = (title + " " + link + " " + content).lower()
        if "remote" in low or "worldwide" in low:
            return "remote"
        if "hybrid" in low:
            return "hybrid"
        if "onsite" in low or "in office" in low:
            return "onsite"
        return "unknown"

"""RSS / Atom feed adapter (feedparser)."""
from __future__ import annotations

import re
import time
from typing import List, Optional
from urllib.parse import quote

from config.search_config import EmploymentType, SearchConfig, classify_employment_type
from services.safe_requests import safe_get
from services.sanitization import strip_html
from .base import JobSource, RawListing, SourceResult
from .registry import register
try:
    from feedparser import parse as _feedparser_parse
except Exception:  # optional; tests can mock
    _feedparser_parse = None  # type: ignore

# A small starting feed list — replace / extend with your own feeds.
# Format: {"name": str, "url": str}
FEEDS = [
    {"name": "ACME Jobs", "url": "https://acme.example/careers/rss"},
    {"name": "Sample Job Exchange", "url": "https://jobs.example.com/feed.xml?sector=tech"},
]


@register
class RSSFeedSource(JobSource):
    name = "RSS Feeds"
    slug = "rss"
    robots_check = True
    official_search_url: Optional[str] = None

    def __init__(self, feeds: List[dict] | None = None, **kwargs):
        super().__init__(**kwargs)
        self.feeds = feeds if feeds is not None else FEEDS

    def scan(self, cfg: SearchConfig) -> SourceResult:
        from services.robots import is_allowed_by_robots

        if _feedparser_parse is None:
            return SourceResult(
                success=False,
                error_message="feedparser is not installed",
            )

        listings: List[RawListing] = []
        errors: List[str] = []
        seen = set()
        for entry in self.feeds:
            name = entry.get("name") or "Feed"
            url = entry.get("url")
            if not url:
                continue
            if self.robots_check and not is_allowed_by_robots(url, timeout=5.0):
                errors.append(f"{name}: robots disallowed")
                continue
            try:
                result = safe_get(url, timeout=10.0, client=self.client)
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                continue
            if result.status_code != 200 or not result.content:
                errors.append(f"{name}: HTTP {result.status_code or 'n/a'}")
                continue
            try:
                parsed = _feedparser_parse(result.content)
            except Exception as exc:
                errors.append(f"{name}: parse error: {exc}")
                continue
            # feedparser `parsed.entries` is a list of feed entries
            if parsed.get("status") not in (200, None) and parsed.get("status", 200) != 200:
                errors.append(f"{name}: feed status {parsed.get('status')}")
                continue
            for e in parsed.get("entries") or []:
                title = (e.get("title") or "").strip()
                link = (e.get("link") or "").strip()
                guid = e.get("id") or link
                if not title or not link:
                    continue
                digest = (link or "").lower() + "|" + (guid or "").lower()
                if digest in seen:
                    continue
                seen.add(digest)
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
                success=False, listings=[], error_message=" | ".join(errors)
            )
        return SourceResult(
            success=bool(filtered),
            listings=filtered,
            listings_found=len(filtered),
            listings_new=len(filtered),
            error_message=" | ".join(errors) if errors else None,
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

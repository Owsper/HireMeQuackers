"""Generic career-page HTML scanner (JS-rendered pages use Playwright ONLY when
the user has enabled it in settings + only for sites where it is permitted).
"""
from __future__ import annotations

import re
from typing import List, Optional
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup

from config.search_config import EmploymentType, SearchConfig, classify_employment_type
from services.safe_requests import safe_get
from services.sanitization import strip_html
from services.robots import is_allowed_by_robots
from .base import JobSource, RawListing, SourceResult
from .registry import register

# Known site patterns: `key` is a stable identity used in the registry.
# `url` is the URL to crawl; `keywords` are the anchor text patterns we look for.
# Only a few are included by default; user can add more to CAREERS_PAGES.
CAREERS_PAGES: List[dict] = [
    {"key": "google", "url": "https://careers.google.com", "company": "Google"},
    {"key": "microsoft", "url": "https://careers.microsoft.com", "company": "Microsoft"},
    {"key": "intel", "url": "https://jobs.intel.com", "company": "Intel"},
]

_ANCHOR_PATTERNS = (
    # simple "Jobs / Careers / Locations" search paths
    re.compile(r"/(jobs|careers|search/jobs|search-positions|positions)(\?.*)?/?\s*$", re.I),
    # Individual job detail pages
    re.compile(r"/(posting|job|opening|career)[-_]?/?\d+", re.I),
)

ANCHOR_TEXT_KEYWORDS = (
    "apply", "open role", "open position", "view job", "job posting",
    "this position", "open roles", "search for jobs", "job board",
)


@register
class CareerPageSource(JobSource):
    name = "Career Pages"
    slug = "career_pages"
    robots_check = True
    official_search_url = None

    def __init__(self, pages: List[dict] | None = None,
                 allow_browser: bool = False, **kwargs):
        super().__init__(**kwargs)
        self.pages = pages if pages is not None else CAREERS_PAGES
        self.allow_browser = allow_browser

    def scan(self, cfg: SearchConfig) -> SourceResult:
        listings: List[RawListing] = []
        errors: List[str] = []
        for page in self.pages:
            key = page["key"]
            url = page["url"]
            company = page.get("company") or key
            if self.robots_check and not is_allowed_by_robots(url, timeout=5.0):
                errors.append(f"{key}: robots disallowed")
                continue
            try:
                result = safe_get(url, timeout=15.0, client=self.client)
            except Exception as exc:
                errors.append(f"{key}: {exc}")
                continue
            if result.status_code != 200 or not result.content:
                errors.append(f"{key}: HTTP {result.status_code or 'n/a'}")
                continue
            try:
                html = result.content.decode("utf-8", errors="ignore")
            except Exception:
                html = ""
            if not html:
                continue
            soup = BeautifulSoup(html, "html.parser" if self._parser() == "html.parser" else self._parser())
            # Try a few heuristics:
            # 1) Find `a[href]` that matches known job paths
            # 2) Match anchor text that signals "this is a job posting"
            seen = set()
            for a in soup.find_all("a", href=True):
                href = a.get("href", "").strip()
                text = (a.get_text() or "").strip().lower()
                url2 = urljoin(url, href)
                if not url2.startswith("http"):
                    continue
                if url2 in seen:
                    continue
                if not re.search(r"\S", text):
                    continue
                if not (re.search(r"/jobs|/careers|/position|/embedding|/jobs/|/openings", href, re.I)
                        or any(k in text for k in ANCHOR_TEXT_KEYWORDS)):
                    continue
                seen.add(url2)
                title = text[:200] or "Job posting"
                item = RawListing(
                    source=f"{self.name} ({company})",
                    title=title,
                    company=company,
                    location=None,
                    description_text=None,
                    employment_type=classify_employment_type(title),
                    workplace_arrangement=self._detect_workplace(title, url2, ""),
                    original_url=url2,
                    application_url=url2,
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
        return "unknown"

    @staticmethod
    def _parser() -> str:
        try:
            import lxml  # noqa: F401
            return "lxml"
        except Exception:
            return "html.parser"

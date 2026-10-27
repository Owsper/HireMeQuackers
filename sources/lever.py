"""Lever public postings adapter.

Lever's public endpoint:
    https://api.lever.co/v0/postings/{company}

Returns a list of posts. `department`, `categories`, `location` etc. are
best-effort.
"""
from __future__ import annotations

import re
from typing import List, Optional
from urllib.parse import quote

from config.search_config import (
    EmploymentType,
    SearchConfig,
    classify_employment_type,
)
from services.safe_requests import safe_get
from services.sanitization import strip_html
from .base import JobSource, RawListing, SourceResult
from .registry import register

LEVER_SLUGS = [
    {"company": "tinder", "label": "Tinder"},
    {"company": "robinhood", "label": "Robinhood"},
    {"company": "seamless", "label": "Seamless"},
    {"company": "hashicorp", "label": "HashiCorp"},
    {"company": "datadog", "label": "Datadog"},
]


@register
class LeverSource(JobSource):
    name = "Lever"
    slug = "lever"
    robots_check = False
    official_search_url = "https://jobs.lever.co/"

    def __init__(self, slugs: List[dict] | None = None, **kwargs):
        super().__init__(**kwargs)
        self.slugs = slugs if slugs is not None else LEVER_SLUGS

    def scan(self, cfg: SearchConfig) -> SourceResult:
        import json as _json

        listings: List[RawListing] = []
        errors: List[str] = []
        for entry in self.slugs:
            company = entry.get("company")
            label = entry.get("label") or company
            if not company:
                continue
            api_url = f"https://api.lever.co/v0/postings/{quote(str(company), safe='')}"
            try:
                result = safe_get(api_url, timeout=10.0, client=self.client)
            except Exception as exc:
                errors.append(f"{company}: {exc}")
                continue
            if result.status_code != 200 or not result.content:
                if result.status_code:
                    errors.append(f"{company}: HTTP {result.status_code}")
                continue
            try:
                data = result.content.decode("utf-8", errors="ignore")
                parsed = _json.loads(data)
            except Exception:
                errors.append(f"{company}: bad JSON")
                continue
            for post in parsed or []:
                title = (post.get("text") or post.get("id") or "").strip()
                if not title:
                    continue
                loc = post.get("categories") or {}
                location = loc.get("committed") or loc.get("transferred")
                base_url = (
                    "https://jobs.lever.co/" + quote(str(company), safe="")
                    + "/" + quote(str(post.get("id") or ""), safe="")
                )
                item = RawListing(
                    source=self.name,
                    title=title,
                    company=label,
                    location=location,
                    description_text=post.get("allContent") and strip_html(post["allContent"]),
                    employment_type=classify_employment_type(title),
                    workplace_arrangement=self._detect_workplace(title, location or "", post.get("allContent") or ""),
                    original_url=base_url,
                    application_url=base_url,
                    required_skills=[],
                    preferred_skills=[],
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
    def _detect_workplace(title: str, location: str, content: str) -> str:
        low = (title + " " + location + " " + content).lower()
        if "remote" in low or "worldwide" in low:
            return "remote"
        if "hybrid" in low:
            return "hybrid"
        if "onsite" in low or "in office" in low:
            return "onsite"
        return "unknown"

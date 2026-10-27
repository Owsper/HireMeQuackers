"""Greenhouse public job-board adapter (real, verified endpoint).

Greenhouse exposes a public JSON endpoint:
    https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true

This adapter scans a fixed set of board tokens. The list below is a starting
point — replace / extend it by constructing `GREENHOUSE_TOKENS` with your own
public board tokens (each public board token is just the company slug the
employer used, e.g. `stripe`, `mindmaze`, ...).

Visibility / content may change without notice; treat as best-effort.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import List, Optional
from urllib.parse import quote

from config.search_config import (
    EmploymentType,
    SearchConfig,
    classify_employment_type,
)
from services.safe_requests import safe_get
from services.sanitization import strip_html
from services.robots import is_allowed_by_robots
from .base import JobSource, RawListing, SourceResult
from .registry import register


# Known public Greenhouse board tokens (verify before relying).
GREENHOUSE_TOKENS = [
    {"token": "stripe", "company": "Stripe"},
    {"token": "coinbase", "company": "Coinbase"},
    {"token": "shopify", "company": "Shopify"},
    {"token": "mindmaze", "company": "MindMaze"},
    {"token": "pinterest", "company": "Pinterest"},
    {"token": "launchdarkly", "company": "LaunchDarkly"},
    {"token": "workday", "company": "Workday"},
    {"token": "stanv", "company": "StanV Nicholas"},
    {"token": "nickel", "company": "Nickel"},
    {"token": "wework", "company": "WeWork"},
]


@register
class GreenhouseSource(JobSource):
    name = "Greenhouse"
    slug = "greenhouse"
    robots_check = False
    official_search_url = "https://boards.greenhouse.io/"

    def __init__(self, board_tokens: List[dict] | None = None, **kwargs):
        super().__init__(**kwargs)
        self.board_tokens = board_tokens if board_tokens is not None else GREENHOUSE_TOKENS

    def scan(self, cfg: SearchConfig) -> SourceResult:
        from services.safe_requests import safe_get
        from services.robots import is_allowed_by_robots

        listings: List[RawListing] = []
        errors: List[str] = []
        for entry in self.board_tokens:
            token = entry.get("token")
            company = entry.get("company") or token
            if not token:
                continue
            api_url = (
                "https://boards-api.greenhouse.io/v1/boards/"
                + quote(str(token), safe="")
                + "/jobs?content=true"
            )
            if self.robots_check and not is_allowed_by_robots(api_url, timeout=5.0):
                errors.append(f"robots: {token}")
                continue
            try:
                result = safe_get(api_url, timeout=10.0, client=self.client)
            except Exception as exc:
                errors.append(f"{token}: {exc}")
                continue
            if result.status_code != 200 or not result.content:
                if result.status_code:
                    errors.append(f"{token}: HTTP {result.status_code}")
                continue
            try:
                data = result.content.decode("utf-8", errors="ignore")
                import json as _json
                parsed = _json.loads(data)
            except Exception:
                errors.append(f"{token}: bad JSON")
                continue

            jobs = parsed.get("jobs") or []
            def _as_str(v) -> str:
                if v is None:
                    return ""
                if isinstance(v, str):
                    return v
                if isinstance(v, dict):
                    # Greenhouse sometimes nests values (e.g. location={"name": "..."});
                    # prefer a "name"/"display" key, else fall back to the first value.
                    for k in ("name", "display", "label", "title"):
                        if k in v and isinstance(v[k], str):
                            return v[k]
                    for val in v.values():
                        if isinstance(val, str):
                            return val
                    return ""
                return str(v)

            for j in jobs:
                title = _as_str(j.get("title")).strip()
                location = _as_str(j.get("location")).strip()
                content_raw = _as_str(j.get("content"))
                url = (
                    "https://boards.greenhouse.io/"
                    + quote(str(token), safe="")
                    + f"/jobs/{j.get('id')}"
                )
                if not title:
                    continue
                workplace = self._detect_workplace(title, location, content_raw or "")
                item = RawListing(
                    source=self.name,
                    title=title,
                    company=company,
                    location=location or None,
                    description_html=content_raw,
                    description_text=strip_html(content_raw) if content_raw else None,
                    employment_type=classify_employment_type(title),
                    workplace_arrangement=workplace,
                    original_url=url,
                    application_url=url,
                    published_at=j.get("updated_at"),
                    required_skills=self._extract_skills(
                        (j.get("content") or "").lower()
                    ),
                    preferred_skills=self._extract_skills(
                        (j.get("content") or "").lower()
                    ),
                )
                listings.append(item)

        # Employer-name + filter
        filtered = self.apply_filters(cfg, listings)
        success = bool(len(filtered) > 0) and (not errors or len(filtered) > 0)
        if not errors and not filtered:
            # source scanned OK but found nothing relevant
            success = False
        elif errors and not filtered:
            success = False
        return SourceResult(
            success=bool(filtered),
            listings=filtered,
            listings_found=len(filtered),
            listings_new=len(filtered),
            error_message=(" | ".join(errors)) if errors else None,
        )

    @property
    def description(self) -> str:
        return self.name

    @staticmethod
    def _detect_workplace(title: str, location: str, content: str) -> str:
        low = (title + " " + location + " " + content).lower()
        if "remote" in low or "fully remote" in low or "worldwide" in low:
            return "remote"
        if "hybrid" in low:
            return "hybrid"
        if "onsite" in low or "on-site" in low or "in office" in low:
            return "onsite"
        return "unknown"

    @staticmethod
    def _extract_skills(text_low: str) -> List[str]:
        lex = (
            "python,sql,pandas,numpy,javascript,typescript,react,aws,azure,"
            "docker,kubernetes,git,django,fastapi,flask,pytorch,tensorflow,"
            "spark,hadoop,salesforce,sap,tableau,excel,machine learning,"
            "deep learning,classification,nlp,data analysis,data engineering,"
            "data science,ETL,SQLite,MySQL,PostgreSQL,Cloud,API,REST,"
            "GraphQL,Metabase,Airflow"
        ).split(",")
        found = []
        for skill in lex:
            skill = skill.strip().lower()
            if skill and re.search(r"\b" + re.escape(skill) + r"\b", text_low):
                found.append(skill)
        return found

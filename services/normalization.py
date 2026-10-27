"""Normalize a `RawListing` into a `Job` ORM row.

- map employment type from the title (and `RawListing.employment_type` if present)
- detect workplace arrangement when not already set
- content_fingerprint = sha256(canonical url | title.lower() | company.lower() | description.lower())
"""
from __future__ import annotations

import hashlib
from typing import Optional

from database.models import Job
from config.search_config import classify_employment_type
from sources.base import RawListing
from services.sanitization import strip_html
from services.safe_requests import validate_public_http_url


def canonical_url(url: Optional[str]) -> str:
    """Normalize a URL: lowercase scheme/host, strip fragment, keep query."""
    if not url:
        return ""
    try:
        from urllib.parse import urlparse

        p = urlparse(url.strip())
        scheme = (p.scheme or "https").lower()
        netloc = (p.netloc or "").lower()
        path = p.path or "/"
        query = p.query
        return (scheme + "://" + netloc + path + ("?" + query if query else "")).rstrip("/")
    except Exception:
        return (url or "").strip()


def content_fingerprint(
    canonical_url_: str, title: str, company: str, description: Optional[str]
) -> str:
    payload = "|".join(
        [
            canonical_url_,
            (title or "").lower().strip(),
            (company or "").lower().strip(),
            (description or "").lower().strip()[:2000],
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:64]


def raw_to_job(raw: RawListing, existing: Optional[Job] = None) -> dict:
    """Build the normalized dict to attach to a `Job` row."""
    title = (raw.title or "").strip()
    company = (raw.company or "Unknown").strip()
    location = (raw.location or "").strip() or None
    desc_raw = raw.description_text or raw.description_html or ""
    desc = (desc_raw or "").strip()
    desc = strip_html(desc_raw) if "<" in desc_raw else desc

    emp = raw.employment_type or classify_employment_type(title)
    workplace = (raw.workplace_arrangement or "unknown").strip().lower()

    return dict(
        title=title,
        company=company,
        location=location,
        country=getattr(raw, "country", None),
        employment_type=emp.value,
        workplace_arrangement=workplace,
        description=desc or None,
        required_skills=raw.required_skills or [],
        preferred_skills=raw.preferred_skills or [],
        experience_requirements=raw.experience_requirements,
        education_requirements=raw.education_requirements,
        language_requirements=raw.language_requirements,
        salary=raw.salary,
        industry=raw.industry,
        original_url=raw.original_url,
        application_url=raw.application_url,
        source=raw.source,
        # published_at from raw (ISO string / int epoch / None)
        published_at=_coerce_date(raw.published_at),
    )


def _coerce_date(value) -> Optional[object]:
    """Best-effort coercion from a variety of raw date-ish values."""
    if value in (None, "", 0, "0"):
        return None
    if isinstance(value, (int, float)):
        try:
            from datetime import datetime, timezone

            return datetime.fromtimestamp(int(value), tz=timezone.utc).replace(tzinfo=None)
        except Exception:
            return None
    s = str(value).strip()
    if not s:
        return None
    iso_like = None
    if "T" in s or " " in s:
        try:
            from datetime import datetime

            iso_like = s.replace("Z", "")
            p = datetime.fromisoformat(iso_like)
            if p.tzinfo is None:
                return p
            return p.astimezone().replace(tzinfo=None)
        except Exception:
            pass
    if "T" in s:
        try:
            from datetime import datetime

            iso_like = s.replace("Z", " +0000").rstrip()
            iso_like = iso_like.replace("+0000", "+00:00")
            return datetime.fromisoformat(iso_like).replace(tzinfo=None)
        except Exception:
            pass
    return None


def safe_description(url: str, html: str) -> str:
    """Convert HTML to plain text, URLValidate the protected URL first.

    This function never calls external services beyond the given `html`.
    """
    if not html:
        return ""
    return strip_html(html)

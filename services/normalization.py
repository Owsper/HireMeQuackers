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
        from urllib.parse import urlparse, parse_qsl, urlencode

        p = urlparse(url.strip())
        scheme = (p.scheme or "https").lower()
        netloc = (p.netloc or "").lower()
        path = p.path or "/"
        query = urlencode(sorted((k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                                 if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid", "ref", "source"}))
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
    lang, required_langs = job_languages(raw, title, desc)
    workplace = (raw.workplace_arrangement or "unknown").strip().lower()
    from services.job_intelligence import description_quality, extract_requirements
    from database.models import utcnow
    import json
    method = "search_snippet" if (raw.search_provider or raw.source.lower() in {"websearch", "web search", "brave", "exa", "duckduckgo"}) and not raw.detail_extraction_succeeded else "source_feed"
    extraction_status, completeness = description_quality(desc, method)
    requirements = extract_requirements(Job(description=desc, location=location,
        required_skills_json=json.dumps(raw.required_skills or []), preferred_skills_json=json.dumps(raw.preferred_skills or []),
        education_requirements=raw.education_requirements, experience_requirements=raw.experience_requirements,
        language_requirements=raw.language_requirements)) if method != "search_snippet" and desc else []

    return dict(
        title=title,
        company=company,
        location=location,
        country=getattr(raw, "country", None),
        employment_type=emp.value,
        workplace_arrangement=workplace,
        description=desc or None,
        extraction_status=extraction_status,
        extraction_method=method,
        description_completeness=completeness,
        structured_requirements_json=json.dumps(requirements),
        extracted_at=utcnow() if extraction_status == "COMPLETE" else None,
        raw_description=desc_raw or None,
        external_job_id=getattr(raw, "external_job_id", None),
        source_type=getattr(raw, "source_type", None),
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
        description_language=lang,
        required_languages=required_langs,
    )


def job_languages(raw: RawListing, title: str, desc: str) -> tuple:
    """(description_language, required_languages) — values supplied by the
    source win; otherwise detected/extracted from the text, ``None`` / ``[]``
    when the evidence is too weak."""
    from services.multilingual import detect_language, extract_required_languages

    lang = raw.description_language or detect_language(f"{title}\n{desc}")
    text = " ".join(x for x in (desc, raw.language_requirements or "") if x)
    if raw.required_languages is not None:
        required = sorted({x.strip().lower() for x in raw.required_languages if x and x.strip()})
    elif text.strip():
        required = extract_required_languages(text)
    else:
        required = None  # nothing to read yet; enrichment may fill it later
    return lang, required


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
    if "T" in s or " " in s or len(s) == 10:
        try:
            from datetime import datetime, timezone

            iso_like = s.replace("Z", "+00:00")
            p = datetime.fromisoformat(iso_like)
            if p.tzinfo is None:
                return p
            return p.astimezone(timezone.utc).replace(tzinfo=None)
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

"""Source base types + abstract source interface."""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import urlparse

import httpx

from config.search_config import EmploymentType, SearchConfig
from services.safe_requests import safe_get
from services.sanitization import strip_html


@dataclass
class RawListing:
    """Normalized raw job listed. Pre-filtering + pre-HTML-strip."""

    source: str
    title: str
    # Required for the abstract RawListing signature (company may be
    # genuinely unknown — e.g. Hacker News job listings).
    company: Optional[str]
    location: Optional[str] = None
    country: Optional[str] = None
    description_html: Optional[str] = None
    description_text: Optional[str] = None
    employment_type: Optional[EmploymentType] = None
    workplace_arrangement: Optional[str] = None
    required_skills: List[str] = field(default_factory=list)
    preferred_skills: List[str] = field(default_factory=list)
    education_requirements: Optional[str] = None
    experience_requirements: Optional[str] = None
    language_requirements: Optional[str] = None
    salary: Optional[str] = None
    industry: Optional[str] = None
    original_url: str = ""
    # Canonical / dedup URL: the URL after any search-engine or ATS
    # redirect (e.g. DDG's ``/l/?uddg=...``).  Used as the
    # de-duplication key; ``original_url`` is kept as-is for provenance.
    canonical_url: Optional[str] = None
    application_url: Optional[str] = None
    published_at: Optional[str] = None  # ISO 8601 or raw
    # --- max-coverage discovery engine: provenance + quality signals ---
    # The search provider that produced this result (e.g. "brave",
    # "duckduckgo"), plus the query string that matched.
    search_provider: Optional[str] = None
    search_query: Optional[str] = None
    # Detail-page extraction succeeded to some level?  ``True`` when
    # the description was extracted from the listing page (vs. only
    # the search snippet), ``False`` when only the snippet is available.
    detail_extraction_succeeded: bool = False
    # ATS-native job id (Ashby id, SmartRecruiters posting id, ...).
    external_job_id: Optional[str] = None
    # ISO 639-1 language the vacancy is *written* in (None = detect later).
    description_language: Optional[str] = None
    # Languages the *applicant* must speak, ISO codes (None = extract later).
    required_languages: Optional[List[str]] = None

    def dedup_key(self) -> str:
        """Best available de-duplication key.

        Order: ``canonical_url`` → ``application_url`` →
        ``original_url`` → empty string.  Callers can
        ``if not job.dedup_key(): continue``.
        """
        for key in (self.canonical_url, self.application_url, self.original_url):
            if key and key.strip():
                return key.strip()
        return ""

    def text_description(self) -> Optional[str]:
        """Textifcatif the description (stored as text field)."""
        if self.description_text:
            return self.description_text
        if self.description_html:
            return strip_html(self.description_html)
        return None


@dataclass
class SourceResult:
    """Outcome of a single source scan."""

    success: bool
    listings: List[RawListing] = field(default_factory=list)
    error_message: Optional[str] = None
    listings_found: int = 0
    listings_new: int = 0
    cache_ttl: int = 0
    # --- per-source fidelity counters (spec §10 scan diagnostics) ---
    # ``attempted``  - number of sub-requests the source made
    #                (e.g. one *per query* for web_search, one *per board*
    #                for greenhouse).
    # ``fetched``    - of the attempted ones, how many returned HTTP 200
    #                (for web_search: queries that *found* at least one
    #                listing; for greenhouse/lever: boards with 200+JSON).
    # ``parsed``     - number of :class:`RawListing` instances extracted.
    # ``duplicates`` - listings discarded because ``dedup_key`` was already
    #                seen in this source's run.
    # ``failed``     - attempted requests that errored out / returned
    #                non-200.
    attempted: int = 0
    fetched: int = 0
    parsed: int = 0
    duplicates: int = 0
    failed: int = 0

    def details(self) -> dict:
        return {
            "success": self.success,
            "listings_found": self.listings_found,
            "listings_new": self.listings_new,
            "error": self.error_message,
            "attempted": self.attempted,
            "fetched": self.fetched,
            "parsed": self.parsed,
            "duplicates": self.duplicates,
            "failed": self.failed,
        }


class JobSource(abc.ABC):
    """Abstract base for all source adapters."""

    # Override in subclasses:
    # name: str = "MySource"
    # official_search_url: str = "https://example.com"
    # slug: str = "mysource"

    name: str = "Generic"
    slug: str = "generic"
    official_search_url: Optional[str] = None
    robots_check: bool = False
    #: Where this source actually has vacancies.  Empty = worldwide.  Used
    #: only to *tell the user* about limits; sources are never skipped.
    coverage_countries: tuple = ()
    #: Short human description of the coverage (shown by `search`).
    coverage_note: str = "worldwide"

    def __init__(self, *, client: Optional[httpx.Client] = None, allow_robots: bool = True):
        self.client = client
        self.allow_robots = allow_robots

    @abc.abstractmethod
    def scan(self, cfg: SearchConfig) -> SourceResult:
        """Fetch latest listings for the search config.

        Return at most the jobs in the search, post-filtering by
        employment type + locations, etc.
        BEFORE HTTP — subclasses can pre-filter with a `listings` [] to skip
        fetching when no relevant title matches are present (e.g. guarded
        RSS titles).
        """
        raise NotImplementedError

    # ---- helpers ----
    def apply_filters(self, cfg: SearchConfig, listings: List[RawListing]) -> List[RawListing]:
        lower_inc = [k.lower() for k in cfg.include_keywords]
        lower_excl = [k.lower() for k in cfg.exclude_keywords]
        lower_locs: list = []
        lower_exl_locs: list = []
        # NOTE: cfg.locations / cfg.excluded_locations may not exist in
        # SearchConfig — access defensively via getattr.
        _cfg_locs = getattr(cfg, "locations", None) or []
        _cfg_exl_locs = getattr(cfg, "excluded_locations", None) or []
        lower_locs = [loc.lower().strip() for loc in _cfg_locs if loc and loc.strip()]
        lower_exl_locs = [loc.lower().strip() for loc in _cfg_exl_locs if loc and loc.strip()]
        require_workplace = any(
            (cfg.include_remote, cfg.include_hybrid, cfg.include_onsite, cfg.worldwide_remote)
        )

        out: List[RawListing] = []
        for item in listings:
            text = " | ".join(
                [item.title or "", item.company or "", item.location or "",
                 item.description_text or ""]
            ).lower()
            # exclude keywords
            if any(k in text for k in lower_excl):
                continue
            # include keywords: if any specified, skip when no match
            if lower_inc and not any(k in text or k in item.title.lower() for k in lower_inc):
                continue
            # employment types: if provided, skip when mismatch
            if cfg.employment_types:
                item_et = item.employment_type
                if item_et is not None and item_et not in cfg.employment_types:
                    continue
            # locations: any of the user-provided substrings in the listing
            if lower_locs:
                loc_text = (item.location or "").lower()
                title_loc = (item.title or "").lower()
                desc = (item.description_text or "").lower()
                if not any(
                    (loc in loc_text) or (loc in title_loc) or (loc in desc)
                    for loc in lower_locs
                ):
                    continue
            if lower_exl_locs:
                loc_text = (item.location or "").lower()
                if any(loc in loc_text for loc in lower_exl_locs):
                    continue
            # workplace arrangement
            if require_workplace:
                arr = (item.workplace_arrangement or "").lower()
                allowed_ss = []
                if cfg.include_remote:
                    allowed_ss.append("remote")
                if cfg.worldwide_remote:
                    allowed_ss.append("worldwide")
                if cfg.include_hybrid:
                    allowed_ss.append("hybrid")
                if cfg.include_onsite:
                    allowed_ss.append("onsite")
                if arr:
                    if not any(s in arr for s in allowed_ss):
                        continue
                # if arr is empty/unknown, pass through (unknown is allowed)
            out.append(item)
            if len(out) >= cfg.max_results:
                break
        return out

    # ---- HTTP helpers ----
    def get_response(self, url: str, *, timeout: float = 15.0, max_bytes: int = 3 * 1024 * 1024):
        """Return (httpx.Response | None, error_message | None).

        `client=None` in safe_get creates a fresh client per call.
        An httpx.Client supplied to the source is forwarded instead.
        """
        from services.safe_requests import safe_get

        try:
            result = safe_get(
                url,
                timeout=timeout,
                max_bytes=max_bytes,
                client=self.client,
            )
        except (ValueError, Exception) as exc:
            return None, str(exc)
        if result.error is not None and result.content == b"":
            return None, result.error
        return result, None


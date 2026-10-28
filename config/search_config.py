"""Search preferences (pydantic) + employment-type classification."""
from __future__ import annotations

import hashlib
import json
import re
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EmploymentType(str, Enum):
    """Supported employment categories.

    Defaults to INTERNSHIP per spec.
    """

    INTERNSHIP = "internship"
    WORKING_STUDENT = "working_student"
    PART_TIME = "part_time"
    FULL_TIME = "full_time"
    GRADUATE = "graduate"
    TEMPORARY = "temporary"
    CONTRACT = "contract"
    OTHER = "other"

    @property
    def label(self) -> str:
        return self.value.replace("_", " ").title()


# ---- Employment-type keyword rules (ordered; first match wins) ----
_ETS_RULES: list[tuple[EmploymentType, tuple[str, ...]]] = [
    (EmploymentType.INTERNSHIP, ("intern", "internship", "summer student", "payed intern", "unpaid intern")),
    (EmploymentType.WORKING_STUDENT, ("working student", "werkstudent", "working-student", "student position")),
    (EmploymentType.GRADUATE, ("graduate", "new grad", "new grad", "early career", "entry level", "entry-level", "new grad")),
    (EmploymentType.PART_TIME, ("part-time", "part time", "pt", "evening shift")),
    (EmploymentType.TEMPORARY, ("temporary", "temp", "seasone", "seasonal")),
    (EmploymentType.CONTRACT, ("contract", "freelance", "consulting", "gig")),
    (EmploymentType.FULL_TIME, ("full-time", "full time", "ft", "permanent", "indefinite")),
]


def classify_employment_type(text: str) -> EmploymentType:
    """Map a free-form title / posting string to an EmploymentType.

    Heuristic: lower-case the text and attempt a substring match against
    layered rules; fallback to OTHER.
    """
    if not text:
        return EmploymentType.OTHER
    t = text.lower()
    for et, needles in _ETS_RULES:
        for n in needles:
            if n in t:
                return et
    return EmploymentType.OTHER


def normalize_employment_type(text: str) -> EmploymentType:
    """Alias used across the codebase; same as classify_employment_type."""
    return classify_employment_type(text)


class BooleanGroups(BaseModel):
    """Optional grouped AND / OR / NOT keyword patterns."""

    model_config = ConfigDict(populate_by_name=True)

    and_: List[str] = Field(default_factory=list, alias="AND")
    or_: List[str] = Field(default_factory=list, alias="OR")
    not_: List[str] = Field(default_factory=list, alias="NOT")


class SearchConfig(BaseModel):
    """Runtime-searchable user-authored preferences."""

    employment_types: List[EmploymentType] = Field(
        default_factory=lambda: [EmploymentType.INTERNSHIP]
    )
    include_keywords: List[str] = Field(default_factory=list)
    exclude_keywords: List[str] = Field(default_factory=list)
    required_skills: List[str] = Field(default_factory=list)
    preferred_skills: List[str] = Field(default_factory=list)
    company_names: List[str] = Field(default_factory=list)
    industries: List[str] = Field(default_factory=list)
    phrases: List[str] = Field(default_factory=list)
    boolean_groups: Optional[BooleanGroups] = None

    locations: List[str] = Field(default_factory=list)
    excluded_locations: List[str] = Field(default_factory=list)
    include_remote: bool = False
    include_hybrid: bool = False
    include_onsite: bool = False
    worldwide_remote: bool = False
    language_requirements: List[str] = Field(default_factory=list)

    # -- P2: Search intent (primary title + synonym overrides) --
    # User-specified primary job title. When empty, the pipeline falls back
    # to keyword-based title relevance only (no hard title gate).
    primary_title: str = ""
    # User-specified synonyms treated as equivalent to primary_title.
    # Existing exact/near synonyms are covered by title_matching.SYNONYM_MAP.
    title_synonyms: List[str] = Field(default_factory=list)
    # Roles to exclude (case-insensitive substring match on job title).
    excluded_titles: List[str] = Field(default_factory=list)
    # When True, "related" tier title matches are shown alongside
    # "strong"/"exact" matches.  Default off — only strongly relevant.
    include_related_roles: bool = False
    # When True, only jobs whose location_status is "match" are shown.
    # Jobs with location_status "unknown" are rejected when this is True,
    # and accepted (tagged unverified) when False.
    require_verified_location: bool = False
    # Minimum token-Jaccard overlap for a "strong" title tier.
    min_title_jaccard: float = 0.60
    # Minimum structured-location confidence for a "match" status.
    min_location_confidence: float = 0.70

    posted_within_days: Optional[int] = None
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    education_level: Optional[str] = None
    company_size_min: Optional[int] = None
    company_size_max: Optional[int] = None
    max_results: int = 100
    min_match_score: float = 0.0
    enabled_sources: List[str] = Field(default_factory=list)

    # -- max-coverage discovery engine (Phase A) --
    search_provider: str = "brave"
    max_queries_per_scan: int = 30
    per_source_max_results: int = 100
    enable_detail_extraction: bool = True
    enable_social_discovery: bool = True
    per_domain_min_seconds: float = 1.5

    # -- detail-page enrichment (Phase 3) --
    enrichment_max_per_scan: int = 25
    enrichment_delay_seconds: float = 1.5

    # -- feed registry + optional extra sources (Phase 5) --
    # User's own feed URLs (unioned with the registry at scan time).
    # This is a *new* field, so configs loaded from older JSON stay valid.
    rss_urls: List[str] = Field(default_factory=list)
    # Categories from services.feed_registry that ship in with this scan.
    # Empty list = use all enabled_default feeds.  Values: "remote", "european",
    # "student", "industry", "general".
    feed_registry_categories: List[str] = Field(
        default_factory=lambda: ["remote", "european", "student"]
    )
    # Optional: EURES public API endpoint.  None => the EURES source is
    # *skipped* (not counted as failed) because no verified keyless endpoint
    # is bundled (EURES's machine-readable data lives behind the EURES JPY portal).
    eures_api_url: Optional[str] = None

    @field_validator("employment_types", mode="before")
    @classmethod
    def _coerce_types(cls, v: Any) -> List[EmploymentType]:
        if v is None:
            return [EmploymentType.INTERNSHIP]
        if isinstance(v, list):
            out: List[EmploymentType] = []
            for item in v:
                if isinstance(item, EmploymentType):
                    out.append(item)
                elif isinstance(item, str):
                    try:
                        out.append(EmploymentType(item.lower()))
                    except ValueError:
                        out.append(classify_employment_type(item))
            return out or [EmploymentType.INTERNSHIP]
        # not a list - coerce scalar
        return [EmploymentType(str(v))]

    def normalized_dict(self) -> Dict[str, Any]:
        """Deterministic serializable form for hashing + persistence."""
        return {
            "employment_types": [e.value for e in self.employment_types],
            "include_keywords": sorted(set(self.include_keywords)),
            "exclude_keywords": sorted(set(self.exclude_keywords)),
            "required_skills": sorted(set(self.required_skills)),
            "preferred_skills": sorted(set(self.preferred_skills)),
            "company_names": sorted(set(self.company_names)),
            "industries": sorted(set(self.industries)),
            "phrases": sorted(set(self.phrases)),
            "boolean_groups": (
                self.boolean_groups.model_dump(by_alias=True)
                if self.boolean_groups
                else None
            ),
            "locations": sorted(set(self.locations)),
            "excluded_locations": sorted(set(self.excluded_locations)),
            "include_remote": self.include_remote,
            "include_hybrid": self.include_hybrid,
            "include_onsite": self.include_onsite,
            "worldwide_remote": self.worldwide_remote,
            "language_requirements": sorted(set(self.language_requirements)),
            "posted_within_days": self.posted_within_days,
            "salary_min": self.salary_min,
            "salary_max": self.salary_max,
            "education_level": self.education_level,
            "company_size_min": self.company_size_min,
            "company_size_max": self.company_size_max,
            "max_results": self.max_results,
            "min_match_score": self.min_match_score,
            "enabled_sources": sorted(set(self.enabled_sources)),
            "rss_urls": sorted(set(self.rss_urls)),
            "feed_registry_categories": sorted(set(self.feed_registry_categories)),
            "eures_api_url": self.eures_api_url,
            "search_provider": self.search_provider,
            "max_queries_per_scan": self.max_queries_per_scan,
            "per_source_max_results": self.per_source_max_results,
            "enable_detail_extraction": self.enable_detail_extraction,
            "enable_social_discovery": self.enable_social_discovery,
            "per_domain_min_seconds": self.per_domain_min_seconds,
        }

    def hash_config(self) -> str:
        """Deterministic SHA256 hex of the normalized search config."""
        payload = json.dumps(
            self.normalized_dict(), sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def default_search_config() -> SearchConfig:
    """Factory for a brand-new default search config (internship focus)."""
    return SearchConfig(employment_types=[EmploymentType.INTERNSHIP])

"""Query planner for the max-coverage discovery engine.

Takes a :class:`~config.search_config.SearchConfig` and (optionally) a
:class:`~database.models.CandidateProfile` and produces a prioritized list
of :class:`QuerySpec` objects for the search-provider pass.

Query families (spec 5): role, skill, seniority, internship (incl. German),
location (remote/hybrid/city), company (career-page/ATS), informal (social),
recent. Total count caps at ``cfg.scan_max_total_queries`` (default 40) and
each spec carries ``max_pages`` for per-query pagination.

This module is provider-agnostic and pure: no network, no DB writes. The
source adapter consumes the list and calls provider.search per spec.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence

from config.search_config import SearchConfig


@dataclass
class QuerySpec:
    """One planned search query.

    Attributes:
        query_string: the literal query to send to the provider.
        category: one of role, skill, seniority, internship, location,
            company, informal, recent.
        priority: 1 (highest) to 10 (lowest). Sorted descending, stable.
        max_pages: provider-side pagination depth for this query (soft).
    """

    query_string: str
    category: str = "role"
    priority: int = 5
    max_pages: int = 2

    def __str__(self) -> str:  # pragma: no cover - debugging
        return (
            f"QuerySpec({self.category!r}, p{self.priority}, "
            f"pages<={self.max_pages}): {self.query_string!r}"
        )


# German-language internship / student-job variants (spec 5 + EU coverage).
_GERMAN_INTERNSHIP_TERMS = (
    "Praktikum",
    "Werkstudent",
    "studentische Mitarbeiterin",
    "studentischer Mitarbeiter",
    "Ausbildung",
    "Trainee",
)

# English internship / graduate variants.
_ENG_INTERNSHIP_TERMS = (
    "intern",
    "internship",
    "graduate",
    "trainee",
    "founding engineer",
    "research assistant",
)

_SENIORITY_TERMS = (
    "junior",
    "entry level",
    "entry-level",
    "early career",
    "mid-level",
)

_INFORMAL_PATTERNS = (
    'we\'re hiring',
    "looking for",
    "hiring",
    "join our team",
    "remote freelance",
    "paid project",
    "part-time",
)

_RECENT_MARKERS = (
    "last week",
    "recently posted",
)


def _dedup_lower(items: Sequence[str]) -> List[str]:
    """Trim, drop empties, case-insensitive dedup preserving order."""
    seen = set()
    out: List[str] = []
    for raw in items:
        s = (raw or "").strip()
        if not s:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def _profile_skills(profile: Optional[object], n: int = 6) -> List[str]:
    """Extract up to ``n`` skill strings from a CandidateProfile-like object."""
    if profile is None:
        return []
    skills = getattr(profile, "skills", None)
    if isinstance(skills, Sequence):
        cleaned = _dedup_lower(list(skills))
    else:
        cleaned = []
    return cleaned[:n]


def _keywords(cfg: SearchConfig) -> List[str]:
    kws = list(cfg.include_keywords) + list(cfg.company_names)
    if not kws:
        kws = list(cfg.phrases)
    if not kws:
        kws = ["Software Intern"]
    return _dedup_lower(kws)


def _locations(cfg: SearchConfig) -> List[str]:
    locs = [loc for loc in cfg.locations]
    return _dedup_lower(locs)


def _german_targeted(cfg: SearchConfig, profile: Optional[object]) -> bool:
    """Heuristic: user is job-hunting in the German-speaking region."""
    text = " ".join(
        [l.lower() for l in _locations(cfg)]
        + [s.lower() for s in (_profile_skills(profile) and _profile_skills(profile) or [])]
        + [" ".join(cfg.include_keywords).lower(),
           " ".join(cfg.phrases).lower()]
    )
    markers = ("germany", "deutschland", "potsdam", "berlin", "munchen",
               "munich", "hamburg", "frankfurt", "stuttgart", "bremen",
               "leipzig", "koln", "cologne", "deutsch", "deutschland",
               "germany")
    return any(m in text for m in markers)


def plan_queries(
    cfg: SearchConfig,
    profile: Optional[object] = None,
    *,
    max_queries: Optional[int] = None,
) -> List[QuerySpec]:
    """Build the prioritized query list for a scan.

    Args:
        cfg: active search configuration.
        profile: optional CandidateProfile (duck-typed; only ``.skills`` used).
        max_queries: explicit cap; defaults to ``cfg.scan_max_total_queries``.

    Returns:
        List of QuerySpec sorted by priority (desc), insertion order (stable).
    """
    if max_queries is None:
        max_queries = getattr(cfg, "scan_max_total_queries", 40) or 40

    kws = _keywords(cfg)
    locs = _locations(cfg)
    german = _german_targeted(cfg, profile)
    skills = _profile_skills(profile)

    specs: List[QuerySpec] = []
    seen = set()

    def add(qs: str, category: str, priority: int, max_pages: int = 2) -> None:
        s = (qs or "").strip()
        if not s:
            return
        key = s.lower()
        if key in seen:
            return
        seen.add(key)
        specs.append(QuerySpec(s, category, priority, max_pages))

    # --- role: each keyword x each location (and a bare keyword) ---------- #
    for kw in kws:
        add(f"{kw} jobs", "role", 1)
        for loc in locs:
            add(f"{kw} {loc} jobs", "role", 2)

    # --- skill: keyword + top skills -------------------------------------- #
    for kw in kws:
        for sk in skills[:3]:
            add(f"{kw} {sk}", "skill", 4)

    # --- seniority: pair a keyword with junior/entry ---------------------- #
    for kw in kws[:3]:
        for sen in _SENIORITY_TERMS[:2]:
            add(f"{sen} {kw}", "seniority", 5)

    # --- location: remote variant for top keywords (coverage expansion) ---- #
    for kw in kws[:4]:
        add(f"remote {kw}", "location", 4)

    # --- internship (english + german variants) --------------------------- #
    for kw in kws[:4]:
        for term in _ENG_INTERNSHIP_TERMS[:3]:
            add(f"{term} {kw}", "internship", 2)
        if german:
            for term in _GERMAN_INTERNSHIP_TERMS[:3]:
                add(f"{term} {kw}", "internship", 2)

    # bare internship terms when location present (e.g. "Werkstudent Berlin")
    if german and locs:
        for term in _GERMAN_INTERNSHIP_TERMS[:2]:
            for loc in locs[:2]:
                add(f"{term} {loc}", "internship", 3)

    # --- company / ATS / career-page -------------------------------------- #
    for comp in cfg.company_names[:6]:
        add(f"careers {comp}", "company", 3)
        add(f"jobs {comp}", "company", 3)

    # --- informal / social (spec 13) -------------------------------------- #
    for term in _INFORMAL_PATTERNS[:3]:
        for kw in kws[:2]:
            add(f"\"{term}\" {kw}", "informal", 6)

    # --- recent ----------------------------------------------------------- #
    for kw in kws[:3]:
        add(f"{kw} recently posted", "recent", 6)

    # --- cap + sort (stable by priority, lowest number = highest) --------- #
    specs.sort(key=lambda s: s.priority)
    return specs[:max(1, int(max_queries))]


def describe(specs: Sequence[QuerySpec]) -> str:
    """One-line human summary of a plan (for logs / UI diagnostics)."""
    by_cat: dict = {}
    for s in specs:
        by_cat.setdefault(s.category, 0)
        by_cat[s.category] += 1
    parts = [f"{k}={v}" for k, v in by_cat.items()]
    return f"total={len(specs)} " + " ".join(parts)


__all__ = ["QuerySpec", "plan_queries", "describe"]

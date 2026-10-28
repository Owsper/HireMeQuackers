"""Query planner — generates a prioritised family of search queries
for the max-coverage discovery engine.

Given a ``SearchConfig`` (and optionally a candidate profile) the
planner returns a sorted :class:`QuerySpec` list.  Queries are
grouped into *families*:

* ``role``        — exact role titles + synonyms (e.g. "software intern",
                  "Praktikum Software" when German locations present)
* ``skill``       — skill × role / skill × position ("intern python")
* ``location``    — location + remote / hybrid variants
* ``ats``         — ATS/career-page search ("site:jobs.ashbyhq.com intern")
* ``informal``    — community / forum wording ("we're hiring intern")
* ``company``     — named-company career pages ("jobs at acme")
* ``seniority``   — graduate / entry-level / working student variants
* ``recent``      — "posted last week" family, for recency filters

Each ``QuerySpec`` carries:

* ``query_string``  — the literal phrase to pass to the provider
* ``category``      — the family name above (``str``)
* ``priority``      — 1-100;  higher = more likely to be relevant
* ``max_pages``     — number of provider pages to follow for this query
                    (default 2; can be stepped up for top-priority
                    families when the user has set a generous budget)

``plan_queries`` enforces ``max_queries`` and reports whether the
plan was *truncated* (the caller can surface "hit the query cap" in
the scan diagnostics UI).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Recognised German locations (case-insensitive substring match).
#: Only these fire the German language variants — so we don't emit
#: "Praktikum" if the user is searching for "London".
_GERMAN_LOCATIONS: tuple[str, ...] = (
    "germany", "deutschland",
    "berlin", "potsdam", "münchen", "munich",
    "hamburg", "cologne", "cöln", "frankfurt",
    "üsseldorf", "düsseldorf", "stuttgart", "dresden",
    "leipzig", "hannover", "nürnberg", "nuremberg",
    "aachen", "bochum", "bremen", "bonn",
)

# German language variants for common job-hunt keywords.
#: We emit one query per (base, german) pair, *only* when at least
#: one location looks German.  Kept small on purpose — the planner
#: is not a translation service.
_GERMAN_VARIANTS: list[tuple[str, str]] = [
    ("software intern", "Praktikum Software"),
    ("intern software", "Werkstudent Software"),
    ("junior software engineer", "Ausbildung Software"),
]


@dataclass
class QuerySpec:
    """One concrete query to run through a search provider."""

    query_string: str
    category: str                      # role | skill | location | ats | informal | company | seniority | recent
    priority: int = 50                 # 1-100
    max_pages: int = 2
    #: Human-readable reason this query is in the plan (shown in UI).
    reason: str = ""
    #: Related lookup fields (employer, skill, location) — used by
    #: the search engine to attribute results to a family later.
    meta: Dict[str, Any] = field(default_factory=dict)


def _lower(s: str | None) -> str:
    return (s or "").strip().lower()


def _dedupe(specs: List[QuerySpec]) -> List[QuerySpec]:
    seen: set[str] = set()
    out: List[QuerySpec] = []
    for spec in specs:
        key = spec.query_string.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(spec)
    return out


def _has_german_location(locations: List[str]) -> bool:
    lowered = [_lower(x) for x in locations if _lower(x)]
    for loc in lowered:
        for needle in _GERMAN_LOCATIONS:
            if needle in loc:
                return True
    return False


def plan_queries(
    cfg: "SearchConfig",
    *,
    profile: Any | None = None,
    max_queries: Optional[int] = None,
    max_pages_per_query: int = 2,
) -> list[QuerySpec]:
    """Build the query family for a scan.

    Parameters
    ----------
    cfg
        User search config (from ``ui`` or API).
    profile
        Optional :class:`services.candidate_profile.CandidateProfile`.
        When present the planner tries to pull in the user's
        skills/industries if they aren't already in ``cfg``.
    max_queries
        Total queries to return (sorted by priority desc).  When
        ``None``, falls back to ``cfg.max_queries_per_scan``.
    max_pages_per_query
        Number of provider pages to follow per query.  Ignored if
        a caller passes ``QuerySpec.max_pages`` explicitly.
    """
    specs: List[QuerySpec] = []
    is_german = _has_german_location(cfg.locations)

    # Collect skill/keyword candidates from cfg + profile.
    skills: list[str] = list(cfg.required_skills or [])
    for cs in (getattr(profile, "skills", None) or []):
        if _lower(cs) not in { _lower(s) for s in skills }:
            skills.append(cs)
    kws: list[str] = []
    for k in list(cfg.include_keywords or []):
        if _lower(k) not in { _lower(x) for x in kws }:
            kws.append(k)

    def add(
        query: str,
        category: str,
        priority: int,
        reason: str = "",
        max_pages: int = max_pages_per_query,
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        q = (query or "").strip()
        if not q:
            return
        specs.append(QuerySpec(
            query_string=q,
            category=category,
            priority=priority,
            max_pages=max_pages,
            reason=reason,
            meta=meta or {},
        ))

    # ---------------------------------------------------------------- #
    # Family 1: role + synonyms (priority 90)
    # ---------------------------------------------------------------- #
    # The "role" family is the single most-relevant family — the
    # user specifically typed, or we infer from the profile, a set of
    # job roles.  We always emit at least the bare 3-tuple.
    base_role = "software intern"
    for k in kws[:10]:
        add(f"{k} intern", "role", 90, reason=f"keyword {k}")
        if is_german:
            add(f"Praktikum {k}", "role", 90, reason=f"German keyword {k}")
            add(f"Werkstudent {k}", "role", 88, reason=f"German working-student {k}")
    # Bare role variants.
    add("software intern", "role", 95, reason="base role")
    add("software engineering internship", "role", 90, reason="synonym")
    add("summer intern software", "role", 85, reason="synonym")
    add("junior software developer", "role", 85, reason="seniority synonym")
    if is_german:
        add("Praktikum Software", "role", 95, reason="German base role")
        add("Werkstudent Software", "role", 90, reason="German working student")
        add("Ausbildung Software", "role", 85, reason="German apprenticeship")

    # ---------------------------------------------------------------- #
    # Family 2: skill × role (priority 70)
    # ---------------------------------------------------------------- #
    for s in skills[:15]:
        add(f"intern {s}", "skill", 75, reason=f"skill {s}")
        add(f"junior developer {s}", "skill", 70, reason=f"skill {s}")
        if is_german:
            add(f"Werkstudent {s}", "skill", 72, reason=f"German skill {s}")

    # ---------------------------------------------------------------- #
    # Family 3: location + remote (priority 60)
    # ---------------------------------------------------------------- #
    for loc in cfg.locations[:6]:
        add(f"remote software intern {loc}", "location", 65)
        add(f"intern {loc}", "location", 60)
        if is_german:
            add(f"Werkstudent {loc}", "location", 62)
    if cfg.worldwide_remote or cfg.include_remote:
        add("remote software intern", "location", 68, reason="worldwide remote")
        add("remote internship unlimited locations", "location", 60)

    # ---------------------------------------------------------------- #
    # Family 4: ATS / career page (priority 55-50)
    # ---------------------------------------------------------------- #
    # `site:` operator is *one signal* — we don't rely on it
    # exclusively.  For each recognised ATS, we emit a query.
    ats_roots: List[tuple[str, str, str]] = [
        # (site_hint, display_name, job_path)
        ("jobs.ashbyhq.com", "Ashby", "jobs"),
        ("jobs.lever.co", "Lever", None),
        ("boards.greenhouse.io", "Greenhouse", "jobs"),
        ("apply.workable.com", "Workable", "positions"),
        ("bamboohr.com", "BambooHR", "careers.json"),
    ]
    for site, label, _ in ats_roots:
        add(f"site:{site} intern", "ats", 55, reason=f"{label} ATS search")
        if is_german:
            add(f"site:{site} Praktikum", "ats", 55, reason=f"{label} ATS search (DE)")

    # ---------------------------------------------------------------- #
    # Family 5: informal / community wording (priority 45-40)
    # ---------------------------------------------------------------- #
    informal_phrases = [
        "hiring software intern",
        "looking for a developer intern",
        "we are hiring intern",
        "looking for a junior developer",
        "founding engineer",
        "first engineer",
    ]
    if is_german:
        informal_phrases.extend([
            "wir suchen praktikant",
            "erkundig dich Praktikant",
            "wir sind auf der suche nach",
        ])
    for p in informal_phrases:
        add(p, "informal", 45)

    # ---------------------------------------------------------------- #
    # Family 6: company career pages (priority 55)
    # ---------------------------------------------------------------- #
    for company in (cfg.company_names or [])[:10]:
        add(f"jobs at {company}", "company", 55, reason=company)
        add(f"{company} intern", "company", 55, reason=f"{company} job")

    # ---------------------------------------------------------------- #
    # Family 7: seniority / graduate (priority 50)
    # ---------------------------------------------------------------- #
    add("graduate software internship", "seniority", 50)
    add("entry level software engineer", "seniority", 48)
    add("working student software", "seniority", 50)
    if is_german:
        add("Studentische Mitarbeitende", "seniority", 50)

    # ---------------------------------------------------------------- #
    # Family 8: recent (priority 30) — provider-dependent; many
    # providers have `freshness` or similar.  We *always* emit this
    # family so that a provider with recency support can use it.
    # ---------------------------------------------------------------- #
    add("software intern posted last week", "recent", 30)
    if is_german:
        add("Praktikum Software neu", "recent", 30)

    # ---------------------------------------------------------------- #
    # Sort by priority desc; truncate to max_queries.
    # ---------------------------------------------------------------- #
    specs = _dedupe(specs)
    specs.sort(key=lambda s: (-s.priority, s.query_string.lower()))

    if max_queries is None:
        # Prefer the user's per-scan cap; fall back to a sensible default.
        max_queries = getattr(cfg, "max_queries_per_scan", None) or 24
    max_queries = max(1, int(max_queries))
    if len(specs) > max_queries:
        specs = specs[:max_queries]
    return specs


__all__ = ["QuerySpec", "plan_queries"]
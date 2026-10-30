"""Query planner — generates a prioritised family of search queries
for the max-coverage discovery engine.

Given a ``SearchConfig`` (and optionally a candidate profile) the
planner returns a sorted :class:`QuerySpec` list.  Queries are
grouped into *families*:

* ``role``        — the user's own titles / keywords / CV target roles in any
                  industry ("Nurse", "Marketing Assistant intern",
                  "Praktikum Marketing" when German locations present)
* ``skill``       — skill × role / skill × position ("intern python")
* ``location``    — location + remote / hybrid variants
* ``ats``         — ATS/career-page search ("site:jobs.ashbyhq.com intern")
* ``jobboard``    — site-specific major job-board search
                  ("site:stepstone.de Werkstudent Python Berlin"); these
                  get reserved slots so the query cap cannot crowd them out
* (localized)    — role titles translated into every search language
                  (``services.multilingual``); tagged ``meta["lang"]``
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

import json
import re
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


#: EmploymentType value -> level word appended to role queries.
_LEVEL_WORDS: Dict[str, str] = {
    "internship": "intern",
    "working_student": "working student",
    "graduate": "graduate",
    "part_time": "part time",
}
_LEVEL_RE = re.compile(
    r"(?<!\w)(?:intern(?:ship)?|trainee|apprentice\w*|working student|werkstudent\w*|"
    r"praktik\w*|graduate|entry[- ]level|junior|azubi|ausbildung|stage|stagiaire|"
    r"part[- ]time)(?!\w)", re.I)


def _has_level_word(title: str) -> bool:
    """True when the title already names a level ("Marketing Intern",
    "Werkstudent Vertrieb", "Graduate Nurse", "Entry-level ...")."""
    return bool(_LEVEL_RE.search(title or ""))


def _level_words(cfg) -> List[str]:
    """Level words for the configured employment types, in config order.
    Empty when the user searches regular (full-time / any) jobs."""
    types = [getattr(e, "value", e) for e in (getattr(cfg, "employment_types", None) or [])]
    if len(types) >= 6:  # "all types" — the user did not restrict the level
        return []
    out: List[str] = []
    for t in types:
        word = _LEVEL_WORDS.get(t)
        if word and word not in out:
            out.append(word)
    return out


def _roles(cfg, profile: Any | None) -> List[str]:
    """Job titles to search for: the user's titles and keywords first,
    then target roles stored with the CV.  Never a built-in default."""
    raw: List[str] = [getattr(cfg, "primary_title", "") or ""]
    raw += list(getattr(cfg, "title_synonyms", None) or [])
    raw += list(getattr(cfg, "include_keywords", None) or [])
    other = getattr(profile, "other_json", None) if profile is not None else None
    if isinstance(other, str):
        try:
            other = json.loads(other)
        except ValueError:
            other = None
    if isinstance(other, dict):
        for key in ("target_roles", "job_titles"):
            val = other.get(key)
            raw += val if isinstance(val, list) else ([val] if isinstance(val, str) else [])
    out: List[str] = []
    for r in raw:
        r = " ".join(str(r or "").split())
        if r and _lower(r) not in {_lower(x) for x in out}:
            out.append(r)
    return out


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
    from services.multilingual import localized_titles, query_language, search_languages

    specs: List[QuerySpec] = []
    is_german = _has_german_location(cfg.locations)
    languages = search_languages(cfg)

    # Industry-agnostic: every query is built from what the user asked for
    # (titles / keywords / skills / companies) or from the CV — never from
    # a built-in profession list.
    roles = _roles(cfg, profile)
    levels = _level_words(cfg)
    level = levels[0] if levels else ""
    entry_level = bool(levels)
    skills: list[str] = list(cfg.required_skills or [])
    for cs in (getattr(profile, "skills", None) or []):
        if _lower(cs) not in {_lower(s) for s in skills}:
            skills.append(cs)
    # What to put in a query when the user gave no role at all
    # ("any entry-level jobs in Berlin").
    generic = [{"intern": "internship"}.get(lv, f"{lv} jobs") for lv in levels] or ["jobs"]
    primary = roles[0] if roles else generic[0]

    def add(
        query: str,
        category: str,
        priority: int,
        reason: str = "",
        max_pages: int = max_pages_per_query,
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        q = " ".join((query or "").split())
        if not q:
            return
        meta = dict(meta or {})
        meta.setdefault("lang", query_language(q))
        specs.append(QuerySpec(
            query_string=q,
            category=category,
            priority=priority,
            max_pages=max_pages,
            reason=reason,
            meta=meta,
        ))

    def with_level(role: str, lv: str) -> str:
        """'Nurse' + 'intern' -> 'Nurse intern'; no duplicate level word."""
        if not lv or _has_level_word(role):
            return role
        return f"{role} {lv}"

    first_role = roles[0] if roles else ""

    # ---------------------------------------------------------------- #
    # Family 1: role titles (priority 95-85) — the user's own titles,
    # with the requested level word ("intern", "working student", ...).
    # ---------------------------------------------------------------- #
    for i, role in enumerate(roles[:10]):
        top = 95 if i == 0 else 90
        add(role, "role", top, reason="requested title")
        for j, lv in enumerate(levels[:2]):
            add(with_level(role, lv), "role", top - 1 - j, reason=f"{role} ({lv})")
        if is_german and entry_level and not _has_level_word(role):
            add(f"Praktikum {role}", "role", 89, reason=f"German internship {role}")
            add(f"Werkstudent {role}", "role", 88, reason=f"German working student {role}")
    if not roles:
        for i, g in enumerate(generic[:3]):
            add(g, "role", 90 - i, reason="no title given — level only")
        if is_german and entry_level:
            add("Praktikum", "role", 88, reason="German internship (any field)")
            add("Werkstudent", "role", 87, reason="German working student (any field)")
            add("Berufseinsteiger", "role", 86, reason="German entry level (any field)")

    # ---------------------------------------------------------------- #
    # Family 2: skill / qualification x level (priority 75-70)
    # ---------------------------------------------------------------- #
    for s in skills[:15]:
        add(f"{s} {level}" if level else f"{s} jobs", "skill", 75, reason=f"skill {s}")
        if first_role:
            add(f"{first_role} {s}", "skill", 70, reason=f"{first_role} + skill {s}")
        if is_german and entry_level:
            add(f"Werkstudent {s}", "skill", 72, reason=f"German skill {s}")

    # ---------------------------------------------------------------- #
    # Family 3: location + remote (priority 68-60)
    # ---------------------------------------------------------------- #
    for loc in cfg.locations[:6]:
        add(f"{with_level(primary, level)} {loc}", "location", 65)
        for role in roles[1:3]:
            add(f"{role} {loc}", "location", 61)
        if is_german and entry_level:
            add(f"Werkstudent {first_role} {loc}", "location", 62)
    if cfg.worldwide_remote or cfg.include_remote:
        add(f"remote {with_level(primary, level)}", "location", 68, reason="remote")
        add(f"remote {primary} worldwide", "location", 60)

    # ---------------------------------------------------------------- #
    # Family 4: ATS / career page (priority 55) — the same role on every
    # ATS, so companies of every industry using these systems are found.
    # ---------------------------------------------------------------- #
    ats_roots: List[tuple[str, str]] = [
        ("jobs.ashbyhq.com", "Ashby"),
        ("jobs.lever.co", "Lever"),
        ("boards.greenhouse.io", "Greenhouse"),
        ("apply.workable.com", "Workable"),
        ("bamboohr.com", "BambooHR"),
    ]
    for site, label in ats_roots:
        add(f"site:{site} {with_level(primary, level)}", "ats", 55, reason=f"{label} ATS search")
        if is_german and entry_level:
            add(f"site:{site} Praktikum {first_role}", "ats", 55, reason=f"{label} ATS search (DE)")

    # ---------------------------------------------------------------- #
    # Family 4b: major job boards (priority 60-58) — publicly indexed
    # listings only; boards with an official API (Arbeitnow) are
    # covered by their adapter and skipped here.
    # ---------------------------------------------------------------- #
    from services.platforms import boards_for_scan

    board_role = with_level(primary, level) if roles else primary
    board_loc = cfg.locations[0] if cfg.locations else ""
    for i, board in enumerate(boards_for_scan(cfg.locations)):
        add(
            f"site:{board.site} {board_role} {board_loc}",
            "jobboard", 60 - min(i, 2),
            reason=f"{board.label} indexed listings",
            meta={"platform": board.label},
        )

    # ---------------------------------------------------------------- #
    # Family 4c: localized role titles (priority 88-62) for every search
    # language other than English — dictionary translations only, so an
    # unknown title produces no query instead of a bad guess.
    # ---------------------------------------------------------------- #
    base_titles = [with_level(r, level) for r in roles[:3]] or generic[:1]
    for lang in languages:
        if lang == "en":
            continue
        for title in base_titles:
            for i, local in enumerate(localized_titles(title, lang)):
                add(local, "role", 88 - i, reason=f"{lang} title for {title!r}",
                    meta={"lang": lang})
                for loc in cfg.locations[:2]:
                    add(f"{local} {loc}", "location", 63 - i,
                        reason=f"{lang} title + location", meta={"lang": lang})

    # ---------------------------------------------------------------- #
    # Family 5: informal / community wording (priority 45)
    # ---------------------------------------------------------------- #
    for p in (f"hiring {primary}", f"we are hiring {primary}", f"looking for {primary}"):
        add(p, "informal", 45)
    if is_german:
        add(f"wir suchen {primary}", "informal", 45)

    # ---------------------------------------------------------------- #
    # Family 6: company career pages (priority 55)
    # ---------------------------------------------------------------- #
    for company in (cfg.company_names or [])[:10]:
        add(f"jobs at {company}", "company", 55, reason=company)
        add(f"{company} {with_level(primary, level)}", "company", 55, reason=f"{company} job")

    # ---------------------------------------------------------------- #
    # Family 7: entry-level / graduate variants (priority 50) — only when
    # the user searches at an entry level.
    # ---------------------------------------------------------------- #
    if entry_level:
        add(f"graduate {first_role or 'jobs'}", "seniority", 50)
        add(f"entry level {first_role or 'jobs'}", "seniority", 48)
        if is_german:
            add(f"Berufseinsteiger {first_role}", "seniority", 50)

    # ---------------------------------------------------------------- #
    # Family 8: recent (priority 30) — provider-dependent recency.
    # ---------------------------------------------------------------- #
    add(f"{with_level(primary, level)} posted last week", "recent", 30)
    if is_german and entry_level:
        add(f"Praktikum {first_role} neu", "recent", 30)

    # ---------------------------------------------------------------- #
    # Sort by priority desc; truncate to max_queries.
    # ---------------------------------------------------------------- #
    specs = _dedupe(specs)
    # include_english=False: drop English-worded role/keyword queries once
    # at least one other language is searched (site:/company queries stay).
    if not getattr(cfg, "include_english", True) and any(x != "en" for x in languages):
        neutral = {"ats", "jobboard", "company"}
        specs = [s for s in specs
                 if s.category in neutral or s.meta.get("lang") != "en"]
    specs.sort(key=lambda s: (-s.priority, s.query_string.lower()))

    if max_queries is None:
        # Prefer the user's per-scan cap; fall back to a sensible default.
        max_queries = getattr(cfg, "max_queries_per_scan", None) or 24
    max_queries = max(1, int(max_queries))
    if len(specs) > max_queries:
        board_specs = [s for s in specs if s.category == "jobboard"]
        quota = min(len(board_specs), max(1, max_queries // 3))
        reserved = board_specs[:quota]
        rest = [s for s in specs if s not in reserved][: max_queries - len(reserved)]
        specs = sorted(rest + reserved, key=lambda s: (-s.priority, s.query_string.lower()))
    return specs


__all__ = ["QuerySpec", "plan_queries"]
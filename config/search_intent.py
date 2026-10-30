"""Structured search intent: a small, explicit view of *what* a search is for.

The user-facing `SearchConfig` (see `config/search_config.py`) is the
serializable, persisted, "search parameters" object. The `SearchIntent`
defined here is a *derived*, in-memory interpretation that the
deterministic filter / title-relevance / location-validation layers
consume.

Why split the two?

- `SearchConfig` is user-visible (settings page) and must stay
  backwards-compatible with rows in `search_profiles.config_json`.
- `SearchIntent` is a *view* that makes a distinction `SearchConfig`
  cannot: "the user asked for *Python Developer* in *Berlin*" — the
  role is a named intent, not a keyword that happens to be in the
  description. This distinction is what lets us reject a "Python
  smoker" listing even when the description mentions Python.

The intent is *never persisted on its own*: it is derived from a
`SearchConfig` (and optionally a free-text query the user typed in)
each time a scan runs, and the derivation is deterministic so a given
config always produces the same intent.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from config.search_config import EmploymentType, SearchConfig


# ---------------------------------------------------------------------------
# Seniority / opportunity-type tokens
# ---------------------------------------------------------------------------

# Ordered by precedence: longer / more-specific tokens first so that
# "junior data engineer" != "senior data engineer" but = "data engineer".
_SENIORITY_TOKENS: Tuple[Tuple[str, ...], str] = (
    (("senior", "sr.", "sr ", "sr)", "staff", "lead", "principal",
      "head of", "manager", "director", "architect", "vp", "vice president"),
     "senior"),
    (("junior", "jr.", "jr ", "jr)", "associate", "entry", "entry-level",
      "founder", "apprentice"), "junior"),
    (("intern", "internship", "co-op", "coop", "summer student",
      "working student", "werkstudent"), "intern"),
    (("graduate", "new grad", "early career", "entry level",
      "entry-level"), "graduate"),
)


def _detect_seniority(text: str) -> Optional[str]:
    t = text.lower()
    for tokens, label in _SENIORITY_TOKENS:
        for tok in tokens:
            if tok in t:
                return label
    return None


# ---------------------------------------------------------------------------
# Search intent
# ---------------------------------------------------------------------------


@dataclass
class SearchIntent:
    """An explicit, in-memory description of *what the user is looking for*.

    All fields default to "unspecified" (empty list / None / False) so an
    intent can always be built from a partial config. An unspecified
    field means "no constraint" — never "definitely anything" — so the
    filter layer treats it as a *soft* criterion, not a *hard* one.
    """

    raw_query: str = ""
    primary_title: str = ""
    title_synonyms: List[str] = field(default_factory=list)
    excluded_titles: List[str] = field(default_factory=list)
    required_skills: List[str] = field(default_factory=list)
    preferred_skills: List[str] = field(default_factory=list)

    cities: List[str] = field(default_factory=list)
    countries: List[str] = field(default_factory=list)
    remote: bool = False
    hybrid: bool = False
    onsite: bool = False
    worldwide_remote: bool = False

    include_related_titles: bool = False
    include_unverified_location: bool = False
    commute_radius_km: Optional[float] = None

    posted_within_days: Optional[int] = None
    employment_types: List[EmploymentType] = field(default_factory=list)
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    seniority: Optional[str] = None  # "senior" | "junior" | "intern" | "graduate" | None

    # Provenance
    source: str = "config"  # "config" | "free_text" | "config+free_text"
    original_search_config_kind: str = ""  # "SearchConfig" for the type

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def all_titles(self) -> List[str]:
        """Full list of titles the search is interested in."""
        out: List[str] = []
        if self.primary_title:
            out.append(self.primary_title)
        for s in self.title_synonyms:
            if s and s not in out:
                out.append(s)
        return out

    def all_locations(self) -> List[str]:
        out: List[str] = []
        for c in self.cities:
            if c and c not in out:
                out.append(c)
        for cc in self.countries:
            if cc and cc not in out:
                out.append(cc)
        return out

    def is_empty(self) -> bool:
        return not any(
            [
                self.raw_query,
                self.primary_title,
                self.title_synonyms,
                self.required_skills,
                self.preferred_skills,
                self.cities,
                self.countries,
                self.remote,
                self.hybrid,
                self.onsite,
                self.worldwide_remote,
            ]
        )

    def needs_intent_validation(self) -> bool:
        """True when an intent has both a title AND an explicit location
        (city or country) — those are the high-cost / high-precision
        cases the AI stage should be most helpful for (and that the UI
        should summarise back to the user)."""
        return bool(self.primary_title) and (
            bool(self.cities) or bool(self.countries)
        )

    def to_summary(self) -> str:
        """A short human-readable label used by the dashboard empty-state
        and the jobs-page 'Why included' tooltip. Runs deterministic; safe
        to call from a render pass."""
        parts: List[str] = []
        t = self.primary_title or "/".join(self.title_synonyms[:2]) or "?"
        parts.append(t)
        locs = ", ".join(self.all_locations()[:3])
        if locs:
            parts.append(locs)
        elif self.worldwide_remote:
            parts.append("worldwide remote")
        elif self.remote:
            parts.append("remote")
        elif self.hybrid:
            parts.append("hybrid")
        elif self.onsite:
            parts.append("onsite")
        if self.seniority:
            parts.append(self.seniority)
        return " · ".join(parts)


# ---------------------------------------------------------------------------
# Derivation: SearchConfig + optional free text -> SearchIntent
# ---------------------------------------------------------------------------


# Deliberately **small, curated** map. The rule:
#   - Both sides must be stable, well-known job titles.
#   - We deliberately do NOT treat "analyst" == "engineer" or
#     "frontend" == "backend" — those are *different* roles.
#   - We don't list every possible spelling; `title_relevance.py` has a
#     token-based fallback that handles the long tail without claiming
#     legality for every synonym.
_TITLE_SYNONYM_MAP: List[List[str]] = [
    ["machine learning engineer", "ml engineer", "ml eng",
     "machine learning", "ml", "ai engineer", "artificial intelligence engineer"],
    ["data analyst", "business analyst", "data analyst intern"],
    ["data engineer", "data engineering", "etl engineer"],
    ["software engineer", "swe", "software developer", "software dev",
     "software development engineer", "sw engineer", "programmer",
     "software engineering intern", "sde"],
    ["frontend developer", "front-end developer", "front end developer",
     "fe developer", "fe", "ui developer", "ui engineer", "web developer"],
    ["backend developer", "back-end developer", "back end developer",
     "be developer", "be", "server-side developer"],
    ["full-stack developer", "full stack developer", "fullstack developer",
     "full stack", "full-stack"],
    ["python developer", "python engineer", "python engineer intern",
     "python intern", "python software developer", "python scientist"],
    ["java developer", "java engineer", "java developer intern"],
    ["devops engineer", "devops", "sre", "site reliability engineer"],
    ["qa engineer", "qa", "quality assurance engineer", "test engineer",
     "software test engineer", "manual qa"],
    ["data scientist", "data science", "statistician"],
    ["product manager", "pm", "product"],
    ["technical writer", "technical writing", "docs writer"],
    ["research engineer", "research scientist", "research engineer intern"],
    # ---- other industries (same discipline: only true equivalents) ----
    ["nurse", "registered nurse", "staff nurse", "pflegefachkraft", "krankenpfleger",
     "gesundheits- und krankenpfleger"],
    ["warehouse worker", "warehouse associate", "warehouse operative", "picker packer",
     "order picker", "lagerhelfer", "lagermitarbeiter", "lagerist"],
    ["accountant", "staff accountant", "bookkeeper", "buchhalter", "finanzbuchhalter"],
    ["marketing assistant", "marketing coordinator",
     "marketingassistent"],
    ["graphic designer", "visual designer", "grafikdesigner", "mediengestalter"],
    ["sales assistant", "retail assistant", "shop assistant",
     "verkäufer", "verkaufsberater"],
    ["customer service representative", "customer service agent", "customer support agent",
     "customer advisor", "kundenberater"],
    ["administrative assistant", "office assistant", "office administrator",
     "bürokaufmann", "bürokauffrau", "bürokraft"],
    ["receptionist", "front desk agent", "front office agent", "rezeptionist"],
    ["teacher", "lehrer", "lehrkraft", "educator"],
    ["electrician", "elektriker", "elektroniker"],
    ["delivery driver", "driver", "courier", "fahrer", "berufskraftfahrer"],
    ["cook", "line cook", "koch"],
    ["waiter", "waitress", "server", "kellner", "servicekraft"],
]


def _lookup_synonyms(title: str) -> List[str]:
    key = _norm_title(title)
    for group in _TITLE_SYNONYM_MAP:
        if any(_norm_title(g) == key for g in group):
            return [g for g in group if g.lower() != title.lower()]
    return []


def _norm_title(text: str) -> str:
    """Lowercase, de-hyphenate, de-possessiveise, strip punctuation.

    Keeps *words* intact so "jr ." and "jr" are both normalised; keeps
    *word order* so "engineer data" -> "engineer data" (not sorted)."""
    if not text:
        return ""
    t = text.lower().strip()
    t = t.replace("-", " ").replace("_", " ")
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _extract_city_country(text: str) -> Tuple[List[str], List[str]]:
    """Cheap heuristic: look for comma / " in " / " at " separators in a
    location string, and split into (cities, countries). This is a
    *compressed* form of `services/location_validation.parse_location`
    that returns the bare-name lists the intent needs.

    We intentionally do NOT guess country from city here (that's the
    location-validation layer's job, with a confidence score).
    """
    cities: List[str] = []
    countries: List[str] = []
    for part in re.split(r"[,;/|\n]", text or ""):
        p = part.strip()
        if not p:
            continue
        # "Berlin, Germany" - style already split above; this catches:
        # "Berlin (Germany)"; "Berlin – Germany"; "Berlin/Germany"
        m = re.match(r"^(.*?)\s*[\-–—]\s*(.+)$", p)
        if m:
            cities.append(m.group(1).strip())
            countries.append(m.group(2).strip())
            continue
        if re.search(r"\b(in|at|around|near)\b", p, re.I):
            m2 = re.match(r"^\w+\b\s+(in|at|around|near)\b\s+(.+)$", p, re.I)
            if m2:
                cities.append(m2.group(1).strip())
                countries.append(m2.group(2).strip())
                continue
        # bare: a country anywhere in the world (services.geo) or "remote"
        # is a country-level request; anything else is taken as a city.
        from services.geo import country as _geo_country

        if re.search(r"\bremote\b", p, re.I) or _geo_country(p):
            countries.append(p)
        else:
            cities.append(p)
    return cities, countries


def build_intent(
    cfg: SearchConfig,
    free_text: Optional[str] = None,
    *,
    include_related_titles: bool = False,
    include_unverified_location: bool = False,
    commute_radius_km: Optional[float] = None,
) -> SearchIntent:
    """Derive a `SearchIntent` from a `SearchConfig` + optional free text.

    Deterministic: same inputs -> same intent (no randomness, no AI).
    The intent is *never* more specific than the config: we never add
    cities the config didn't mention; we only *split* the config's
    `locations` into (cities, countries) when it looks like a
    "City, Country" pair.
    """
    intent = SearchIntent(
        required_skills=list(cfg.required_skills or []),
        preferred_skills=list(cfg.preferred_skills or []),
        remote=bool(cfg.include_remote),
        hybrid=bool(cfg.include_hybrid),
        onsite=bool(cfg.include_onsite),
        worldwide_remote=bool(cfg.worldwide_remote),
        posted_within_days=cfg.posted_within_days,
        employment_types=list(cfg.employment_types),
        salary_min=cfg.salary_min,
        salary_max=cfg.salary_max,
        include_related_titles=bool(include_related_titles),
        include_unverified_location=bool(include_unverified_location),
        commute_radius_km=commute_radius_km,
        source="config",
    )

    # Locations -> cities + countries
    cities: List[str] = []
    countries: List[str] = []
    for loc in (cfg.locations or []):
        c, co = _extract_city_country(loc)
        for x in c:
            if x and x not in cities:
                cities.append(x)
        for x in co:
            if x and x not in countries:
                countries.append(x)
    intent.cities = cities
    intent.countries = countries

    # Free-text query: "Python Developer in Berlin" -> primary_title
    # "Python Developer", city "Berlin".
    if free_text and free_text.strip():
        intent.source = "config+free_text" if any(
            [cities, countries, intent.required_skills, intent.preferred_skills]
        ) else "free_text"
        intent.raw_query = free_text.strip()
        title, extra_city, extra_country = _parse_free_text_query(free_text)
        if title:
            intent.primary_title = title
            intent.title_synonyms = _lookup_synonyms(title)
            intent.seniority = _detect_seniority(title)
            if extra_city and extra_city not in intent.cities:
                intent.cities.append(extra_city)
            if extra_country and extra_country not in intent.countries:
                intent.countries.append(extra_country)

    if intent.source == "config" and not intent.primary_title:
        # Priority 1: use cfg.primary_title directly (explicit user intent)
        if getattr(cfg, "primary_title", None):
            intent.primary_title = cfg.primary_title.strip()
            intent.title_synonyms = list(
                getattr(cfg, "title_synonyms", None) or []
            ) or _lookup_synonyms(intent.primary_title)
            intent.seniority = _detect_seniority(intent.primary_title)
        # Priority 2 (fallback): if there's exactly one keyword, take it
        # as the primary title so title-relevance has a nominal target.
        elif len(cfg.include_keywords or []) == 1:
            intent.primary_title = cfg.include_keywords[0].strip()
            intent.title_synonyms = _lookup_synonyms(intent.primary_title)
            intent.seniority = _detect_seniority(intent.primary_title)
    return intent


def _parse_free_text_query(text: str) -> Tuple[str, Optional[str], Optional[str]]:
    """Split "Python Developer in Berlin" or "ML Engineer, Germany" into
    (title, city, country). Deliberately simple + conservative; complex
    queries just fall back to "everything is title"."""
    t = (text or "").strip()
    if not t:
        return "", None, None

    # "X in Y"
    m = re.match(r"^(.+?)\s+(?:in|ats|bei|at)\s+([A-Z][a-zA-Z\-]+(?:\s+[A-Z][a-zA-Z\-]+)?)\.?$", t)
    if m:
        return m.group(1).strip(), m.group(2).strip(), None

    # "X, Y"
    m = re.match(r"^(.+?),\s*([A-Z][a-zA-Z\-]+(?:\s+[A-Z][a-zA-Z\-]+)?)\.?$", t)
    if m:
        return m.group(1).strip(), m.group(2).strip(), None

    # "X - Y"
    m = re.match(r"^(.+?)\s+-\s+([A-Z][a-zA-Z\-]+(?:\s+[A-Z][a-zA-Z\-]+)?)\.?$", t)
    if m:
        return m.group(1).strip(), m.group(2).strip(), None

    return t, None, None


def intent_search_config_params(
    intent: SearchIntent, base: Optional[SearchConfig] = None
) -> List[Tuple[str, object]]:
    """Diff-ready list of (key, value) pairs the scan *should* honour.

    Used by the discovery layer to log what about the intent changed
    since the last scan (cache invalidation) and by tests to assert
    the config still matches the user's stated requirements.
    """
    base = base or SearchConfig()
    return [
        ("employment_types", [e.value for e in (intent.employment_types or [])]),
        ("include_keywords", sorted(set(
            (base.include_keywords or []) + (intent.all_titles() if intent.include_related_titles else [intent.primary_title])
        ))),
        ("required_skills", sorted(set(intent.required_skills or []))),
        ("preferred_skills", sorted(set(intent.preferred_skills or []))),
        ("locations", sorted(set((base.locations or []) + intent.all_locations()))),
        ("include_remote", intent.remote),
        ("include_hybrid", intent.hybrid),
        ("include_onsite", intent.onsite),
        ("worldwide_remote", intent.worldwide_remote),
        ("posted_within_days", intent.posted_within_days),
        ("salary_min", intent.salary_min),
        ("salary_max", intent.salary_max),
        # These are intent-only flags — they have no SearchConfig
        # column by design (they drive the filter, not the query):
        ("_intent_include_related_titles", intent.include_related_titles),
        ("_intent_include_unverified_location", intent.include_unverified_location),
        ("_intent_commute_radius_km", intent.commute_radius_km),
        ("_intent_seniority", intent.seniority),
    ]

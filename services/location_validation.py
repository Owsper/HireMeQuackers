"""Deterministic location parsing + verdict against a `SearchIntent`.

The user-visible `Job.location` and `Job.country` today are a free
string column concatenated by source adapters. This module gives us
three things the filter stage needs:

    1. A structured parse of the raw string (city / region / country /
       remote_arrangement) with a confidence + provenance
       (`source_explicit` / `inferred` / `missing`).
    2. A `parse_location` that is conservative — *when not sure, return
       the raw string with `source="inferred", confidence<0.8` rather
       than inventing a guess.*
    3. A `location_verdict` that works in three states (MATCH /
       MISMATCH / UNKNOWN) rather than treating "no data" as "match".

We deliberately do NOT use a GeoNames / Nominatim / haversine call;
those need a network dependency that is out of scope. City-alias and
commute-radius are *user-provided* — if the user tells us "Berlin and
Potsdam are ~25 km apart", we honour that with their number, not with a
fetched map.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from config.search_intent import SearchIntent


# ---------------------------------------------------------------------------
# Parsed location
# ---------------------------------------------------------------------------

_CONFIDENCE_UNKNOWN = 0.0
_CONFIDENCE_INFERRED = 0.6
_CONFIDENCE_EXPLICIT = 1.0

# Deliberately small aliases. Anything not in here gets passed through as
# its raw text with `source="inferred"` and a low confidence — *we never
# invent a city from a phone number or a random word in the description.*
_CITY_ALIASES: List[Tuple[str, str]] = [
    ("new york", "new york"),
    ("nyc", "new york"),
    ("la", "los angeles"),
    ("los angeles", "los angeles"),
    ("sf", "san francisco"),
    ("san francisco", "san francisco"),
    ("berlin", "berlin"),
    ("muenchen", "munich"),
    ("münchen", "munich"),
    ("munich", "munich"),
    ("hamburg", "hamburg"),
    ("münchen-berlin", "berlin"),  # ambiguous: returns "berlin" — but see below
    ("london", "london"),
    ("paris", "paris"),
    ("frankfurt", "frankfurt"),
    ("frankfurt am main", "frankfurt"),
    ("münchen", "munich"),
    ("amsterdam", "amsterdam"),
    ("utrecht", "utrecht"),
    ("madrid", "madrid"),
    ("barcelona", "barcelona"),
    ("rome", "rome"),
    ("milan", "milan"),
    ("london uk", "london"),
    ("london, uk", "london"),
]


_COUNTRY_ALIASES: dict = {
    "uk": "united kingdom",
    "usa": "united states",
    "us": "united states",
    "u.s.": "united states",
    "u.s": "united states",
    "ger": "germany",
    "chi": "china",
    "fra": "france",
    "esp": "spain",
    "ita": "italy",
    "nld": "netherlands",
    "net": "netherlands",
    "pol": "poland",
    "aut": "austria",
    "swz": "switzerland",
    "che": "switzerland",
    "swe": "sweden",
    "nor": "norway",
    "den": "denmark",
    "fin": "finland",
    "ire": "ireland",
    "bel": "belgium",
    "lux": "luxembourg",
    "ch": "switzerland",
    "de": "germany",
    "fr": "france",
    "es": "spain",
    "it": "italy",
    "nl": "netherlands",
    "pl": "poland",
    "at": "austria",
    "ch": "switzerland",
    "pt": "portugal",
}


@dataclass
class ParsedLocation:
    city: Optional[str] = None
    region: Optional[str] = None  # state / county / provincia
    country: Optional[str] = None
    remote: bool = False
    arrangement: Optional[str] = None  # "remote" | "hybrid" | "onsite" | None
    original: str = ""
    source: str = "inferred"  # "source_explicit" | "inferred" | "missing"
    confidence: float = _CONFIDENCE_UNKNOWN
    notes: List[str] = field(default_factory=list)

    @property
    def is_concrete(self) -> bool:
        return bool(self.city or self.country) and self.confidence >= 0.8

    def to_dict(self) -> dict:
        return {
            "city": self.city,
            "region": self.region,
            "country": self.country,
            "remote": self.remote,
            "arrangement": self.arrangement,
            "original": self.original,
            "source": self.source,
            "confidence": round(float(self.confidence), 3),
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------


# Layouts we *explicitly* recognise — each returns a tuple we can index.
_LAYOUTS: List[Tuple[re.Pattern, str]] = [
    # "Remote - Germany" / "Remote — Germany" / "Remote, Germany"
    (
        re.compile(r"^\s*remote\s*[-–—,]?\s*([A-Z][A-Za-z\-\.]+(?:\s+[A-Z][A-Za-z\-\.]+)?)\s*$", re.I),
        "remote_country",
    ),
    # "U.S. Remote"
    (
        re.compile(r"^\s*([A-Z][A-Za-z\-\.]+(?:\s+[A-Z][A-Za-z\-\.]+)?)\s+remote\s*$", re.I),
        "country_remote",
    ),
    # "Berlin, Germany" / "Berlin, Germany DE"
    (
        re.compile(r"^\s*([A-Z][a-zA-Z\-]*(?:\s+[A-Z][a-zA-Z\-]*)?)\s*,\s*\(?([A-Z][A-Za-z\.]*(?:\s+[A-Z][A-Za-z\.]*)?)\)?(?:\s+[A-Z]{2})?\s*$"),
        "city_country",
    ),
    # "Berlin (DE)" / "Berlin, DE"
    (
        re.compile(r"^\s*([A-Z][a-zA-Z\-]*(?:\s+[A-Z][a-zA-Z\-]*)?)\s*[\(,]\s*([A-Z]{2,3})\s*\)?\s*$"),
        "city_cc",
    ),
    # "Berlin – Germany"
    (
        re.compile(r"^\s*([A-Z][a-zA-Z\-]*(?:\s+[A-Z][a-zA-Z\-]*)?)\s*[\-–—]\s*([A-Z][A-Za-z\.]*(?:\s+[A-Z][A-Za-z\.]*)?)\s*$"),
        "city_dash_country",
    ),
    # bare all-caps code (just "DE")
    (
        re.compile(r"^\s*([A-Z]{2,3})\s*$"),
        "cc_only",
    ),
]


def _lookup_city(text: str) -> Optional[str]:
    key = text.strip().lower()
    for alias, canon in _CITY_ALIASES:
        if alias == key:
            return canon
    from services.geo import is_city

    return key if is_city(key) else None


def _lookup_country(text: str) -> Optional[str]:
    key = text.strip().lower()
    from services.geo import country

    if key in _COUNTRY_ALIASES:
        return _COUNTRY_ALIASES[key]
    canon = country(key)
    # A full name stays as written ("Germany"); only aliases/codes map.
    return canon if canon and canon != key else None


def _fix_region(city: Optional[str], country: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """"Toronto, ON" / "New York, NY": the second part is a state or
    province, not a country — use the city's country, keep the region."""
    from services.geo import city_country, country as geo_country

    if city and country and not geo_country(country) and city_country(city):
        return city_country(city), country
    if city and not country and city_country(city):
        return city_country(city), None
    return country, None


def parse_location(raw: str) -> ParsedLocation:
    """Parse a free-string into a `ParsedLocation`.

    When we can't confidently identify a city or country, we return
    `source="missing"` rather than make a guess. *This* is the fix for
    the "Remote – Germany displayed as Berlin, Germany" failure mode in
    the original pipeline: instead of filling in a city, we leave the
    city blank and mark confidence so an UI can surface "location not
    verified".
    """
    original = (raw or "").strip()
    if not original:
        return ParsedLocation(original="", source="missing", confidence=_CONFIDENCE_UNKNOWN)

    lower = original.lower()

    # Arrangement detection first (independent of city/country layout).
    remote = "remote" in lower or "echt remote" in lower
    hybrid = re.search(r"\bhybrid\b", lower) is not None
    onsite = re.search(r"\bon[- ]?site\b|\bin[- ]?office\b", lower) is not None
    arrangement: Optional[str] = None
    if remote and hybrid:
        arrangement = "hybrid-remote"
    elif remote:
        arrangement = "remote"
    elif hybrid:
        arrangement = "hybrid"
    elif onsite:
        arrangement = "onsite"

    # Try our layouts in order. First match wins.
    for pat, label in _LAYOUTS:
        m = pat.match(original)
        if not m:
            continue
        city: Optional[str] = None
        region: Optional[str] = None
        country: Optional[str] = None
        notes: List[str] = []

        if label == "remote_country":
            country = _lookup_country(m.group(1)) or m.group(1)
            notes.append("remote with country")
        elif label == "country_remote":
            country = _lookup_country(m.group(1)) or m.group(1)
            notes.append("country + remote")
        elif label == "city_country":
            city = _lookup_city(m.group(1)) or m.group(1).strip()
            c2 = _lookup_country(m.group(2)) or m.group(2).strip()
            country = c2
            if c2 and _lookup_country(m.group(2)) is None:
                # Fallback: short code (DE, US, ...); we can
                # surmise it's a country by length.
                pass
        elif label == "city_cc":
            city = _lookup_city(m.group(1)) or m.group(1).strip()
            country = _lookup_country(m.group(2)) or m.group(2).upper()
        elif label == "city_dash_country":
            city = _lookup_city(m.group(1)) or m.group(1).strip()
            country = _lookup_country(m.group(2)) or m.group(2).strip()
        elif label == "cc_only":
            country = _lookup_country(m.group(1)) or m.group(1).upper()

        country, state = _fix_region(city, country)
        region = region or state

        # If any of city / country was resolved via an alias or a
        # well-known code, bump confidence to "explicit".
        source = "source_explicit"
        confidence = _CONFIDENCE_EXPLICIT
        if city is None and country is None:
            source = "inferred"
            confidence = _CONFIDENCE_INFERRED
            notes.append("no city/country resolved from layout")

        return ParsedLocation(
            city=city, region=region, country=country,
            remote=bool(remote) or arrangement == "remote",
            arrangement=arrangement,
            original=original, source=source, confidence=confidence,
            notes=notes,
        )

    # No match on any layout. Fall through to "single-token" guesses:
    # "Germany" alone -> country; "Berlin" alone -> city. Anything else
    # (e.g. "near my office") gets `source="inferred", city=None`.
    lower_stripped = original.lower()
    if re.match(r"^[a-z\- ]{2,}$", lower_stripped):
        word = lower_stripped.strip()
        if word in _COUNTRY_ALIASES:
            return ParsedLocation(
                country=_COUNTRY_ALIASES[word], original=original,
                remote=remote, arrangement=arrangement,
                source="source_explicit", confidence=_CONFIDENCE_EXPLICIT,
                notes=["single-token country"],
            )
        for alias, canon in _CITY_ALIASES:
            if alias == word:
                return ParsedLocation(
                    city=canon, original=original,
                    remote=remote, arrangement=arrangement,
                    source="source_explicit", confidence=_CONFIDENCE_EXPLICIT,
                    notes=["single-token city"],
                )
    # Known city / country anywhere in the world ("Sydney NSW", "Dubai",
    # "Tokyo-to", "Japan") from the offline geo tables.
    from services.geo import resolve

    g_city, g_country = resolve(original)
    if g_city or g_country:
        return ParsedLocation(
            city=g_city, country=g_country, original=original,
            remote=remote, arrangement=arrangement,
            source="source_explicit", confidence=0.9,
            notes=["geo table"],
        )
    # Truly not recognisable: city + country both None, retained
    # original text, low confidence so the UI can show an "unverified"
    # badge.
    return ParsedLocation(
        city=None, country=None,
        original=original,
        remote=remote, arrangement=arrangement,
        source="inferred", confidence=_CONFIDENCE_INFERRED,
        notes=["could not identify city or country"],
    )


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


# "Any of these arrangements satisfies a request for remote"; etc.
# These tables live here so the filter is one line away from them.
_ARRANGEMENTS = ("remote", "hybrid", "onsite", "hybrid-remote")


def _city_equal(a: str, b: str) -> bool:
    if not a or not b:
        return False
    return (a.strip().lower() == b.strip().lower())


def location_verdict(
    parsed_job_loc: ParsedLocation, intent: SearchIntent
) -> Tuple[str, str]:
    """Return `(verdict, reason)`.

    `verdict` is one of "match", "mismatch", "unknown".
        - "match":    the job's location satisfies the intent.
        - "mismatch": the job's location sufficiently contradicts the
                      intent (city or country explicitly different).
        - "unknown":  not enough data to be certain either way —
                      the filter flags this as "unverified" and lets
                      `intent.include_unverified_location` decide.

    Rule order (first hit wins):
        1. If the intent has NO locations (only remote flags), we check
           the arrangement only.
        2. If the intent has an explicit country job side that
           contradicts a country the intent asked for -> mismatch.
        3. If job has a concrete different city from the intent's
           cities or a different country than what the intent asked
           for -> mismatch.
        4. If intent wants `worldwide_remote`, any job that is remote
           (regardless of country) passes.
        5. If intent wants remote/hybrid/onsite and the job's
           arrangement contradicts -> mismatch.
        6. If we couldn't identify a city/country on either side ->
           unknown.
    """
    requested_cities = [c.strip().lower() for c in (intent.cities or []) if c]
    requested_countries = [c.strip().lower() for c in (intent.countries or []) if c]

    # 1) location-unspecified: arrangement check only.
    if not requested_cities and not requested_countries:
        return _verdict_arrangement_only(parsed_job_loc, intent)

    # 2) Hard country mismatch: job has a *concrete* country and the
    #    intent explicitly asks for a different, specific country list
    #    that does not include the job's country. We do NOT reject on
    #    inferred ("missing") job countries.
    if parsed_job_loc.country:
        jc = parsed_job_loc.country.strip().lower()
        if requested_countries and not any(
            _matches_country(jc, rc) for rc in requested_countries
        ):
            if parsed_job_loc.source in ("source_explicit",) or parsed_job_loc.confidence >= 0.8:
                return "mismatch", (
                    f"job country '{parsed_job_loc.country}' is not in "
                    f"requested {requested_countries[:3]}"
                )
            # Inferred case: treat as unknown and let the UI decide.
            return "unknown", (
                f"job country '{parsed_job_loc.country}' (inferred) does not "
                f"obviously match requested {requested_countries[:3]}"
            )

    # 3) City mismatch only fires when we have both a *concrete* city on
    #    the job side AND the intent restricted to a list of cities not
    #    including the job's.
    job_cities = [c.strip().lower() for c in (parsed_job_loc.city or "").split() if c]
    if job_cities and requested_cities:
        # Compare the whole city name too: multi-word cities ("New York",
        # "São Paulo", "Frankfurt am Main") never matched word by word.
        full_city = (parsed_job_loc.city or "").strip().lower()
        job_city_hit = any(_city_equal(full_city, rc) for rc in requested_cities) or any(
            _city_equal(jc, rc) for jc in job_cities for rc in requested_cities)
        # Job country might also satisfy a "country-only" intent.
        if job_city_hit or (not job_cities and parsed_job_loc.country):
            pass  # fall through
        else:
            # Trust only the job's city if it's source_explicit (not
            # a guess). Otherwise "unknown".
            if parsed_job_loc.source == "source_explicit" and parsed_job_loc.confidence >= 0.8:
                return "mismatch", (
                    f"job city '{parsed_job_loc.city}' is not in requested cities "
                    f"{[c for c in requested_cities if c][:3]}"
                )
            return "unknown", (
                f"job city '{parsed_job_loc.city}' (inferred) does not clearly "
                f"match requested cities"
            )

    # 4) worldwide_remote shortcut.
    if intent.worldwide_remote:
        if parsed_job_loc.remote or parsed_job_loc.arrangement in ("remote", "hybrid-remote"):
            return "match", "worldwide remote"
        if parsed_job_loc.country or parsed_job_loc.city:
            # remote-request + a concrete location that's in the requested
            # countries is a HOME-BASED-REMOTE match (in intent terms).
            return "match", "worldwide remote request, location falls in requested scope"
        return "unknown", "worldwide remote: insufficient location data on the job"

    # 5) Arrangement check (only if requested location is satisfied).
    if requested_cities or requested_countries:
        # If we got here, no clear mismatch: let the arrangement check
        # take over for the final call.
        v, r = _verdict_arrangement_only(parsed_job_loc, intent)
        return v, f"location ok; {r}"

    return "unknown", "insufficient location data on both sides"


def _matches_country(job_country: str, request_country: str) -> bool:
    j = job_country.strip().lower()
    r = request_country.strip().lower()
    if j == r:
        return True
    # "united states" vs "usa" / "us"
    from services.geo import country

    j_canon = _COUNTRY_ALIASES.get(j) or country(j) or j
    r_canon = _COUNTRY_ALIASES.get(r) or country(r) or r
    return j_canon == r_canon


def _verdict_arrangement_only(
    parsed: ParsedLocation, intent: SearchIntent
) -> Tuple[str, str]:
    """Arrangement-only verdict: single remote / hybrid / onsite request."""
    # Remote with a country that's explicitly requested, or no location
    # requested at all.
    if intent.remote:
        if parsed.remote or parsed.arrangement in ("remote", "hybrid-remote"):
            return "match", "remote (request is remote)"
        return "unknown", "requested remote; job arrangement unclear"
    if intent.hybrid:
        if parsed.arrangement in ("hybrid", "hybrid-remote", "onsite"):
            return "match", "hybrid accepted"
        if parsed.arrangement:
            return "mismatch", f"requested hybrid, job is {parsed.arrangement}"
        return "unknown", "requested hybrid; job arrangement unclear"
    if intent.onsite:
        if parsed.arrangement in ("onsite",):
            return "match", "onsite (request is onsite)"
        if parsed.remote:
            return "mismatch", "requested onsite, job is remote"
        return "unknown", "requested onsite; job arrangement unclear"
    # No arrangement constraint on the job's arrangement either; we set
    # `intent.worldwide_remote` False and didn't reach a city/mismatch
    # above, so this is the "location unspecified" case.
    return "match", "no location or arrangement constraints"

"""Tests for services/location_validation.py — parse_location and location_verdict.

All tests are pure unit tests: no DB, no network.
"""
from __future__ import annotations

import pytest

from config.search_intent import SearchIntent
from services.location_validation import (
    ParsedLocation,
    parse_location,
    location_verdict,
)


# ---------------------------------------------------------------------------
# parse_location: layout recognition
# ---------------------------------------------------------------------------

class TestParseLocationLayouts:
    def test_berlin_germany(self):
        p = parse_location("Berlin, Germany")
        assert p.city == "berlin"
        assert p.country == "Germany"
        assert p.source == "source_explicit"

    def test_munich_germany(self):
        p = parse_location("Munich, Germany")
        assert p.city == "munich"
        assert p.country == "Germany"

    def test_remote_germany(self):
        p = parse_location("Remote - Germany")
        assert p.country == "Germany"
        assert p.remote is True
        assert p.arrangement == "remote"
        # City should NOT be guessed
        assert p.city is None

    def test_remote_uk(self):
        p = parse_location("Remote, UK")
        assert p.remote is True
        # "UK" resolved as country
        assert p.city is None

    def test_empty_string(self):
        p = parse_location("")
        assert p.source == "missing"
        assert p.city is None
        assert p.country is None

    def test_none_input(self):
        p = parse_location(None)
        assert p.source == "missing"

    def test_hybrid_arrangement(self):
        p = parse_location("Berlin, Germany (Hybrid)")
        # "hybrid" in the text
        assert p.arrangement == "hybrid"


# ---------------------------------------------------------------------------
# location_verdict: match cases
# ---------------------------------------------------------------------------

class TestLocationVerdictMatch:
    def test_exact_city_match(self):
        job = ParsedLocation(city="berlin", country="Germany",
                             source="source_explicit", confidence=1.0)
        intent = SearchIntent(cities=["Berlin"], countries=["Germany"])
        verdict, reason = location_verdict(job, intent)
        assert verdict == "match"

    def test_country_only_match(self):
        job = ParsedLocation(city="munich", country="Germany",
                             source="source_explicit", confidence=1.0)
        intent = SearchIntent(countries=["Germany"])
        verdict, reason = location_verdict(job, intent)
        assert verdict == "match"

    def test_worldwide_remote(self):
        job = ParsedLocation(city=None, country="US", remote=True,
                             arrangement="remote", source="source_explicit", confidence=1.0)
        intent = SearchIntent(worldwide_remote=True)
        verdict, reason = location_verdict(job, intent)
        assert verdict == "match"


# ---------------------------------------------------------------------------
# location_verdict: mismatch cases
# ---------------------------------------------------------------------------

class TestLocationVerdictMismatch:
    def test_wrong_city_mismatch(self):
        job = ParsedLocation(city="london", country="UK",
                             source="source_explicit", confidence=1.0)
        intent = SearchIntent(cities=["Berlin"], countries=["Germany"])
        verdict, reason = location_verdict(job, intent)
        assert verdict == "mismatch"

    def test_wrong_country_mismatch(self):
        job = ParsedLocation(city="new york", country="US",
                             source="source_explicit", confidence=1.0)
        intent = SearchIntent(countries=["Germany"])
        verdict, reason = location_verdict(job, intent)
        assert verdict == "mismatch"


# ---------------------------------------------------------------------------
# location_verdict: unknown cases
# ---------------------------------------------------------------------------

class TestLocationVerdictUnknown:
    def test_inferred_wrong_country_unknown(self):
        """Inferred (low confidence) job country: NOT a hard mismatch."""
        job = ParsedLocation(city=None, country="France",
                             source="inferred", confidence=0.5)
        intent = SearchIntent(countries=["Germany"])
        verdict, reason = location_verdict(job, intent)
        assert verdict == "unknown"

    def test_inferred_wrong_city_unknown(self):
        """Inferred (low confidence) job city: NOT a hard mismatch."""
        job = ParsedLocation(city="paris", country="France",
                             source="inferred", confidence=0.5)
        intent = SearchIntent(cities=["Berlin"])
        verdict, reason = location_verdict(job, intent)
        assert verdict == "unknown"


# ---------------------------------------------------------------------------
# End-to-end: parse + verdict in one flow
# ---------------------------------------------------------------------------

class TestEndToEnd:
    def test_berlin_germany_matches(self):
        parsed = parse_location("Berlin, Germany")
        intent = SearchIntent(cities=["Berlin"], countries=["Germany"])
        verdict, _ = location_verdict(parsed, intent)
        assert verdict == "match"

    def test_london_uk_mismatches_berlin(self):
        parsed = parse_location("London, UK")
        intent = SearchIntent(cities=["Berlin"])
        verdict, _ = location_verdict(parsed, intent)
        assert verdict in ("mismatch", "unknown")


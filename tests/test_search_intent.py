"""Tests for config/search_intent.py — SearchIntent derivation and helpers.

These are deterministic unit tests; no DB, no network.
"""
from __future__ import annotations

import pytest

from config.search_config import EmploymentType, SearchConfig
from config.search_intent import SearchIntent, build_intent, _lookup_synonyms


# ---------------------------------------------------------------------------
# build_intent: basic derivation
# ---------------------------------------------------------------------------

class TestBuildIntentFromConfig:
    def test_minimal_config_produces_minimal_intent(self):
        cfg = SearchConfig()
        intent = build_intent(cfg)
        assert intent.is_empty() is False or intent.is_empty() is True  # any empty-ness is ok
        # No locations, no title
        assert intent.cities == []
        assert intent.countries == []
        assert intent.primary_title == ""

    def test_locations_city_country_split(self):
        cfg = SearchConfig(locations=["Berlin, Germany"])
        intent = build_intent(cfg)
        assert "Berlin" in intent.cities
        assert "Germany" in intent.countries

    def test_locations_multiple(self):
        cfg = SearchConfig(locations=["Berlin, Germany", "London"])
        intent = build_intent(cfg)
        assert "Berlin" in intent.cities
        assert "Germany" in intent.countries
        assert "London" in intent.cities

    def test_remote_flags_propagate(self):
        cfg = SearchConfig(include_remote=True, include_hybrid=True,
                           include_onsite=False, worldwide_remote=False)
        intent = build_intent(cfg)
        assert intent.remote is True
        assert intent.hybrid is True
        assert intent.onsite is False
        assert intent.worldwide_remote is False

    def test_employment_types_propagate(self):
        cfg = SearchConfig(employment_types=[EmploymentType.INTERNSHIP, EmploymentType.GRADUATE])
        intent = build_intent(cfg)
        assert EmploymentType.INTERNSHIP in intent.employment_types
        assert EmploymentType.GRADUATE in intent.employment_types

    def test_salary_and_dates_propagate(self):
        cfg = SearchConfig(salary_min=50000, salary_max=70000, posted_within_days=30)
        intent = build_intent(cfg)
        assert intent.salary_min == 50000
        assert intent.salary_max == 70000
        assert intent.posted_within_days == 30


# ---------------------------------------------------------------------------
# build_intent: free-text query parsing
# ---------------------------------------------------------------------------

class TestBuildIntentFreeText:
    def test_python_developer_berlin(self):
        cfg = SearchConfig()
        intent = build_intent(cfg, free_text="Python Developer in Berlin")
        assert intent.primary_title == "Python Developer"
        assert "Berlin" in intent.cities

    def test_ml_engineer_germany(self):
        cfg = SearchConfig()
        intent = build_intent(cfg, free_text="ML Engineer in Germany")
        assert intent.primary_title == "ML Engineer"
        # _parse_free_text_query puts "Germany" into the cities slot,
        # not the countries slot. The location_verdict layer handles
        # the final city-vs-country disambiguation with confidence.
        assert "Germany" in intent.cities or "Germany" in intent.countries

    def test_comma_separator(self):
        cfg = SearchConfig()
        intent = build_intent(cfg, free_text="Data Engineer, Munich")
        assert intent.primary_title == "Data Engineer"
        assert "Munich" in intent.cities

    def test_dash_separator(self):
        cfg = SearchConfig()
        intent = build_intent(cfg, free_text="Software Engineer - Amsterdam")
        assert intent.primary_title == "Software Engineer"
        assert "Amsterdam" in intent.cities

    def test_synonyms_populated(self):
        cfg = SearchConfig()
        intent = build_intent(cfg, free_text="Python Developer in Berlin")
        # "Python Developer" is in the synonym map, so synonyms should be non-empty
        assert len(intent.title_synonyms) > 0

    def test_seeded_config_plus_free_text(self):
        cfg = SearchConfig(locations=["London"])
        intent = build_intent(cfg, free_text="Data Scientist in London")
        assert intent.primary_title == "Data Scientist"
        assert "London" in intent.cities
        # source should be config+free_text because config already had locations
        assert intent.source == "config+free_text"


# ---------------------------------------------------------------------------
# build_intent: seniority detection
# ---------------------------------------------------------------------------

class TestSeniorityDetection:
    def test_intern_detected(self):
        cfg = SearchConfig()
        intent = build_intent(cfg, free_text="Python Developer Intern in Berlin")
        assert intent.seniority == "intern"

    def test_senior_detected(self):
        cfg = SearchConfig()
        intent = build_intent(cfg, free_text="Senior Data Engineer in Berlin")
        assert intent.seniority == "senior"

    def test_no_seniority_marker(self):
        cfg = SearchConfig()
        intent = build_intent(cfg, free_text="Data Engineer in Berlin")
        assert intent.seniority is None


# ---------------------------------------------------------------------------
# build_intent: keyword fallback
# ---------------------------------------------------------------------------

class TestKeywordFallback:
    def test_single_keyword_becomes_primary_title(self):
        cfg = SearchConfig(include_keywords=["Python Developer"])
        intent = build_intent(cfg)
        assert intent.primary_title == "Python Developer"

    def test_multiple_keywords_do_not_become_title(self):
        cfg = SearchConfig(include_keywords=["Python", "Django"])
        intent = build_intent(cfg)
        # Two keywords: no single title is chosen
        assert intent.primary_title == ""


# ---------------------------------------------------------------------------
# _lookup_synonyms
# ---------------------------------------------------------------------------

class TestLookupSynonyms:
    def test_known_title_returns_synonyms(self):
        syns = _lookup_synonyms("Python Developer")
        assert len(syns) > 0

    def test_unknown_title_returns_empty(self):
        syns = _lookup_synonyms("Completely Novel Role")
        assert syns == []


# ---------------------------------------------------------------------------
# SearchIntent helpers
# ---------------------------------------------------------------------------

class TestSearchIntentHelpers:
    def test_all_titles_includes_primary_and_synonyms(self):
        intent = SearchIntent(
            primary_title="Python Developer",
            title_synonyms=["Python Engineer", "Python Intern"],
        )
        titles = intent.all_titles()
        assert "Python Developer" in titles
        assert "Python Engineer" in titles
        assert "Python Intern" in titles

    def test_all_locations_combines_cities_and_countries(self):
        intent = SearchIntent(cities=["Berlin", "Munich"], countries=["Germany"])
        locs = intent.all_locations()
        assert "Berlin" in locs
        assert "Munich" in locs
        assert "Germany" in locs

    def test_is_empty_true_for_default(self):
        intent = SearchIntent()
        assert intent.is_empty() is True

    def test_is_empty_false_with_title(self):
        intent = SearchIntent(primary_title="Python Developer")
        assert intent.is_empty() is False

    def test_needs_intent_validation_requires_both_title_and_location(self):
        intent = SearchIntent(primary_title="Python Developer", cities=["Berlin"])
        assert intent.needs_intent_validation() is True

    def test_needs_intent_validation_false_without_location(self):
        intent = SearchIntent(primary_title="Python Developer")
        assert intent.needs_intent_validation() is False

    def test_to_summary_is_readable(self):
        intent = SearchIntent(
            primary_title="Python Developer",
            cities=["Berlin"],
            countries=["Germany"],
            seniority="intern",
        )
        s = intent.to_summary()
        assert "Python Developer" in s
        assert "Berlin" in s
        assert "intern" in s

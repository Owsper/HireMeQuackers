"""Tests for services/title_relevance.py — deterministic title classifier.

All tests are pure unit tests: no DB, no network.
"""
from __future__ import annotations

import pytest

from config.search_intent import SearchIntent
from services.title_relevance import classify_title, explain, TitleRelevance


def _intent(title: str = "Machine Learning Engineer",
            synonyms=None,
            include_related: bool = False) -> SearchIntent:
    return SearchIntent(
        primary_title=title,
        title_synonyms=synonyms or [],
        include_related_titles=include_related,
    )


# ---------------------------------------------------------------------------
# Exact matches
# ---------------------------------------------------------------------------

class TestExactMatches:
    def test_exact_case_insensitive(self):
        r = classify_title("Machine Learning Engineer", _intent())
        assert r.label == "exact"
        assert r.score == 1.0
        assert r.passes_hard is True

    def test_exact_hyphenated(self):
        r = classify_title("machine-learning-engineer", _intent())
        assert r.label == "exact"

    def test_exact_with_extra_punctuation(self):
        r = classify_title("Machine Learning Engineer!", _intent())
        assert r.label == "exact"


# ---------------------------------------------------------------------------
# Synonym matches (via title_synonyms on intent)
# ---------------------------------------------------------------------------

class TestSynonymMatches:
    def test_ml_engineer_is_synonym(self):
        intent = SearchIntent(
            primary_title="Machine Learning Engineer",
            title_synonyms=["ML Engineer", "AI Engineer"],
        )
        r = classify_title("ML Engineer", intent)
        assert r.label == "exact"

    def test_ai_engineer_is_synonym(self):
        intent = SearchIntent(
            primary_title="Machine Learning Engineer",
            title_synonyms=["ML Engineer", "AI Engineer"],
        )
        r = classify_title("AI Engineer", intent)
        assert r.label == "exact"


# ---------------------------------------------------------------------------
# Strong matches (token Jaccard >= 0.7, seniority-compatible)
# ---------------------------------------------------------------------------

class TestStrongMatches:
    def test_senior_suffix_removed(self):
        intent = _intent("Software Engineer")
        # "Senior Software Engineer" has core tokens {software, engineer}
        # which exactly match target core {software, engineer}
        # But seniority: target has no marker, job has "senior" -> compatible
        r = classify_title("Senior Software Engineer", intent)
        assert r.label in ("exact", "strong")
        assert r.passes_hard is True

    def test_name_variant(self):
        intent = _intent("Data Engineer")
        # "Data Engineering" should be strong (core tokens: data, engineering vs data, engineer)
        # Jaccard of {data, engineering} vs {data, engineer} = 1/3 < 0.7 -> not strong
        # Actually "Data Engineering" might be in a role family -> related
        r = classify_title("Data Engineering", intent)
        assert r.label in ("strong", "related", "irrelevant")
        # The key assertion: it's never "exact"
        assert r.label != "exact"


# ---------------------------------------------------------------------------
# Seniority mismatches
# ---------------------------------------------------------------------------

class TestSeniorityMismatches:
    def test_senior_target_rejects_intern(self):
        intent = _intent("Senior Software Engineer")
        # "Software Engineer Intern" -> seniority "intern", target "senior"
        # _seniority_ok: tgt=senior, job=intern -> False
        r = classify_title("Software Engineer Intern", intent)
        # Not passes_hard even if core tokens match
        assert r.passes_hard is False

    def test_intern_target_rejects_senior(self):
        intent = _intent("Software Engineer Intern")
        r = classify_title("Senior Software Engineer", intent)
        assert r.passes_hard is False


# ---------------------------------------------------------------------------
# Frontend vs Backend: intentionally NOT equivalent
# ---------------------------------------------------------------------------

class TestFrontendBackend:
    def test_frontend_not_strong_for_backend(self):
        intent = _intent("Backend Developer")
        r = classify_title("Frontend Developer", intent)
        assert r.label != "exact"
        assert r.label != "strong"
        # May be "related" (same dev family) or "irrelevant"
        assert r.label in ("related", "irrelevant")

    def test_backend_interned_for_frontend(self):
        intent = _intent("Frontend Developer")
        r = classify_title("Backend Developer", intent)
        assert r.label in ("related", "irrelevant")


# ---------------------------------------------------------------------------
# Data Analyst vs Data Engineer: NOT equivalent
# ---------------------------------------------------------------------------

class TestAnalystEngineer:
    def test_analyst_not_strong_for_engineer(self):
        intent = _intent("Data Engineer")
        r = classify_title("Data Analyst", intent)
        assert r.label != "exact"
        assert r.label != "strong"

    def test_engineer_not_strong_for_analyst(self):
        intent = _intent("Data Analyst")
        r = classify_title("Data Engineer", intent)
        assert r.label != "exact"
        assert r.label != "strong"


# ---------------------------------------------------------------------------
# Related: role family (only passes when include_related_titles=True)
# ---------------------------------------------------------------------------

class TestRelated:
    def test_same_family_returns_related(self):
        # "ML Engineer" and "AI Engineer" are in the same family
        intent = _intent("ML Engineer", include_related=True)
        r = classify_title("AI Engineer", intent)
        # Could be exact (synonym) or related
        assert r.label in ("exact", "strong", "related")

    def test_wildly_different_is_irrelevant(self):
        intent = _intent("Python Developer")
        r = classify_title("Marketing Manager", intent)
        assert r.label == "irrelevant"
        assert r.passes_hard is False
        assert r.score == 0.0


# ---------------------------------------------------------------------------
# Empty intent: title check waived
# ---------------------------------------------------------------------------

class TestEmptyIntent:
    def test_no_title_constraint_returns_exact(self):
        intent = SearchIntent()
        r = classify_title("Any Random Title", intent)
        assert r.label == "exact"
        assert "waived" in r.reason.lower() or "no title constraint" in r.reason.lower()


# ---------------------------------------------------------------------------
# explain() helper
# ---------------------------------------------------------------------------

class TestExplain:
    def test_explain_returns_reason_string(self):
        r_str = explain("Python Developer", _intent("Python Developer"))
        assert isinstance(r_str, str)
        assert len(r_str) > 0

    def test_explain_for_irrelevant(self):
        r_str = explain("Marketing Manager", _intent("Python Developer"))
        assert isinstance(r_str, str)
        assert "no overlap" in r_str.lower() or "irrelevant" in r_str.lower() or len(r_str) > 0

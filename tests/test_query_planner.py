"""Unit tests for the query planner (services/query_planner.py)."""
from __future__ import annotations

import pytest

from config.search_config import SearchConfig
from services.query_planner import QuerySpec, plan_queries


# ------------------------------------------------------------------ #
# generic behavior
# ------------------------------------------------------------------ #

def test_plan_queries_returns_list_of_queryspec():
    """The planner always returns a non-empty list of QuerySpec for a
    reasonable config."""
    cfg = SearchConfig(
        include_keywords=["Backend"],
        locations=["Berlin"],
        employment_types=["internship"],
    )
    plan = plan_queries(cfg)
    assert len(plan) > 0
    for spec in plan:
        assert isinstance(spec, QuerySpec)
        assert spec.query_string
        assert spec.category
        assert 1 <= spec.priority <= 100


def test_plan_queries_cap_enforced():
    """max_queries=3 → at most 3 specs are returned, priority-sorted."""
    cfg = SearchConfig(
        include_keywords=["Backend", "Frontend", "ML", "DevOps", "QA"],
        locations=["Berlin", "London", "Zurich", "Vienna", "Prague", "Oslo"],
    )
    plan = plan_queries(cfg, max_queries=3)
    assert len(plan) <= 3
    priorities = [p.priority for p in plan]
    assert priorities == sorted(priorities, reverse=True)


def test_plan_queries_default_cap_is_30():
    """Without explicit max_queries, the plan uses cfg.max_queries_per_scan
    (default 30) as the cap."""
    cfg = SearchConfig(
        include_keywords=[f"kw{i}" for i in range(50)],
        locations=[f"loc{i}" for i in range(10)],
    )
    plan = plan_queries(cfg)  # max_queries=None → cfg.cap = 30 default
    assert len(plan) <= 30


def test_plan_queries_respects_cfg_cap():
    """cfg.max_queries_per_scan is used when explicit max_queries is None."""
    cfg = SearchConfig(max_queries_per_scan=5)
    cfg.include_keywords = ["Backend", "Frontend", "ML"]
    cfg.locations = ["Berlin", "London"]
    plan = plan_queries(cfg)  # no max_queries arg
    assert len(plan) <= 5


# ------------------------------------------------------------------ #
# German location variants
# ------------------------------------------------------------------ #

def test_german_variants_present_when_german_locations():
    """Berlin → German variants (from the generic localization layer, not a
    hard-coded German list): Praktikum for internships, Werkstudent only
    when working-student positions are requested."""
    from config.search_config import EmploymentType

    cfg = SearchConfig(
        include_keywords=["Backend"],
        locations=["Berlin"],
    )
    plan = plan_queries(cfg, max_queries=100)
    texts = [p.query_string.lower() for p in plan]
    assert any("praktikum" in t for t in texts), (
        "expected a 'Praktikum' variant in the plan"
    )
    assert not any("werkstudent" in t for t in texts)
    cfg.employment_types = [EmploymentType.INTERNSHIP, EmploymentType.WORKING_STUDENT]
    texts = [p.query_string.lower() for p in plan_queries(cfg, max_queries=100)]
    assert any("werkstudent" in t for t in texts), (
        "expected a 'Werkstudent' variant in the plan"
    )


def test_no_german_for_non_german_locations():
    """London / Zurich → German variants should NOT appear."""
    cfg = SearchConfig(
        include_keywords=["Backend"],
        locations=["London"],
    )
    plan = plan_queries(cfg, max_queries=100)
    texts = [p.query_string.lower() for p in plan]
    bad = [t for t in texts if "praktikum" in t or "werkstudent" in t]
    assert not bad, (
        f"German variants leaked into a non-German plan: {bad}"
    )


def test_germany_country_string_triggers_variants():
    """The country string 'Germany' also triggers German variants."""
    cfg = SearchConfig(
        include_keywords=["Backend"],
        locations=["Germany"],
    )
    plan = plan_queries(cfg, max_queries=100)
    texts = [p.query_string.lower() for p in plan]
    assert any("praktikum" in t for t in texts)


# ------------------------------------------------------------------ #
# specific families
# ------------------------------------------------------------------ #

def test_role_family_present():
    """The role family (priority >= 90) must always be present."""
    cfg = SearchConfig(include_keywords=["Backend"], locations=["Berlin"])
    plan = plan_queries(cfg, max_queries=100)
    role_specs = [p for p in plan if p.category == "role" and p.priority >= 90]
    assert role_specs, "no high-priority role queries found"


def test_skill_queries_present():
    """Skills from the config map to 'intern {skill}' queries."""
    cfg = SearchConfig(
        include_keywords=["Developer"],
        locations=["Berlin"],
        required_skills=["Python"],
    )
    plan = plan_queries(cfg, max_queries=100)
    assert any("python" in p.query_string.lower() for p in plan)


def test_ats_queries_present():
    """The ATS family should be present — site: queries for Greenhouse,
    Lever, Ashby, Workable, BambooHR."""
    cfg = SearchConfig(
        include_keywords=["Backend"],
        locations=["Berlin"],
    )
    plan = plan_queries(cfg, max_queries=100)
    ats = [p for p in plan if p.category == "ats"]
    assert ats, "no ATS queries found in the plan"
    texts = [p.query_string.lower() for p in ats]
    assert any("greenhouse" in t or "lever" in t or "ashby" in t or
               "workable" in t or "bamboohr" in t for t in texts)


def test_company_queries_present_when_companies():
    """cfg.company_names → 'jobs at {company}' + '{company} intern'."""
    cfg = SearchConfig(
        include_keywords=["Backend"],
        locations=["Berlin"],
        company_names=["Acme"],
    )
    plan = plan_queries(cfg, max_queries=100)
    company = [p for p in plan if p.category == "company"]
    assert any("acme" in p.query_string.lower() for p in company)


def test_informal_phrases_present():
    """The informal family is always emitted."""
    cfg = SearchConfig(include_keywords=["Backend"], locations=["Berlin"])
    plan = plan_queries(cfg, max_queries=100)
    informal = [p for p in plan if p.category == "informal"]
    assert informal


def test_recency_queries_present():
    """The 'posted last week' family is always emitted."""
    cfg = SearchConfig(include_keywords=["Backend"], locations=["Berlin"])
    plan = plan_queries(cfg, max_queries=100)
    recent = [p for p in plan if p.category == "recent"]
    assert recent


def test_dedupe_lowercases_query():
    """If the same query string is emitted twice (case-insensitive),
    only one appears in the plan."""
    cfg = SearchConfig(
        include_keywords=["Backend", "backend"],  # duplicate case
        locations=["Berlin"],
    )
    plan = plan_queries(cfg, max_queries=100)
    lowered = [p.query_string.lower() for p in plan]
    assert len(lowered) == len(set(lowered)), "duplicate query in plan"


def test_priority_sorted_desc():
    """The returned plan is priority-sorted (desc)."""
    cfg = SearchConfig(
        include_keywords=["Backend"],
        locations=["Berlin"],
    )
    plan = plan_queries(cfg, max_queries=100)
    priorities = [p.priority for p in plan]
    assert priorities == sorted(priorities, reverse=True)

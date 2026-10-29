"""Tests for SearchConfig, EmploymentType, and employment-type classification."""
from __future__ import annotations

import pytest

from config.search_config import (
    BooleanGroups,
    EmploymentType,
    SearchConfig,
    classify_employment_type,
    default_search_config,
    normalize_employment_type,
)


# ---------------------------------------------------------------------------
# EmploymentType enum
# ---------------------------------------------------------------------------


def test_employment_type_values():
    assert EmploymentType.INTERNSHIP.value == "internship"
    assert EmploymentType.WORKING_STUDENT.value == "working_student"
    assert EmploymentType.PART_TIME.value == "part_time"
    assert EmploymentType.FULL_TIME.value == "full_time"
    assert EmploymentType.GRADUATE.value == "graduate"
    assert EmploymentType.TEMPORARY.value == "temporary"
    assert EmploymentType.CONTRACT.value == "contract"
    assert EmploymentType.OTHER.value == "other"


def test_employment_type_label():
    assert EmploymentType.WORKING_STUDENT.label == "Working Student"
    assert EmploymentType.INTERNSHIP.label == "Internship"


# ---------------------------------------------------------------------------
# classify_employment_type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Python Intern", EmploymentType.INTERNSHIP),
        ("Summer Internship", EmploymentType.INTERNSHIP),
        ("unpaid intern", EmploymentType.INTERNSHIP),
        ("Working Student - Data", EmploymentType.WORKING_STUDENT),
        ("Werkstudent ML", EmploymentType.WORKING_STUDENT),
        ("Part-time Analyst", EmploymentType.PART_TIME),
        ("part time support", EmploymentType.PART_TIME),
        ("Full-time Engineer", EmploymentType.FULL_TIME),
        ("full time role", EmploymentType.FULL_TIME),
        ("Graduate Software Engineer", EmploymentType.GRADUATE),
        ("New Grad Data Scientist", EmploymentType.GRADUATE),
        ("entry-level developer", EmploymentType.GRADUATE),
        ("Temporary Event Staff", EmploymentType.TEMPORARY),
        ("Seasonal Retail", EmploymentType.TEMPORARY),
        ("Contract Developer", EmploymentType.CONTRACT),
        ("Freelance Designer", EmploymentType.CONTRACT),
        ("Junior Developer", EmploymentType.OTHER),
        ("Staff Engineer", EmploymentType.OTHER),
        ("", EmploymentType.OTHER),
    ],
)
def test_classify_employment_type(text, expected):
    assert classify_employment_type(text) == expected


def test_normalize_employment_type_is_alias():
    assert normalize_employment_type("Intern") == classify_employment_type("Intern")


def test_classify_empty_string():
    assert classify_employment_type("") == EmploymentType.OTHER
    assert normalize_employment_type("") == EmploymentType.OTHER


# ---------------------------------------------------------------------------
# SearchConfig
# ---------------------------------------------------------------------------


def test_default_config_is_internship():
    cfg = default_search_config()
    assert cfg.employment_types == [EmploymentType.INTERNSHIP]
    assert cfg.max_results == 100
    assert cfg.include_keywords == []
    assert cfg.locations == []


def test_search_config_accepts_string_employment_types():
    cfg = SearchConfig(employment_types=["internship", "full_time"])
    assert EmploymentType.INTERNSHIP in cfg.employment_types
    assert EmploymentType.FULL_TIME in cfg.employment_types


def test_search_config_none_employment_defaults_to_internship():
    cfg = SearchConfig(employment_types=None)
    assert cfg.employment_types == [EmploymentType.INTERNSHIP]


def test_normalized_dict_is_deterministic():
    cfg = SearchConfig(
        include_keywords=["python", "data"],
        locations=["Berlin", "Munich"],
    )
    a = cfg.normalized_dict()
    b = cfg.normalized_dict()
    assert a == b


def test_hash_config_stable():
    a = SearchConfig(employment_types=["internship"]).hash_config()
    b = SearchConfig(employment_types=["internship"]).hash_config()
    assert a == b
    assert len(a) == 64


def test_boolean_groups_alias():
    bg = BooleanGroups(**{"AND": ["python"], "OR": ["sql"], "NOT": ["java"]})
    assert bg.and_ == ["python"]
    assert bg.or_ == ["sql"]
    assert bg.not_ == ["java"]


def test_config_reads_back_roundtrip():
    cfg = SearchConfig(
        include_keywords=["AI"],
        exclude_keywords=["legacy"],
        include_remote=True,
        posted_within_days=7,
        salary_min=30000,
        max_results=50,
    )
    d = cfg.normalized_dict()
    assert d["include_keywords"] == ["AI"]
    assert d["exclude_keywords"] == ["legacy"]
    assert d["include_remote"] is True
    assert d["posted_within_days"] == 7
    assert d["salary_min"] == 30000
    assert d["max_results"] == 50


# ---------------------------------------------------------------------------
# CLI config validation path (`quackers config set / edit / load`)
# ---------------------------------------------------------------------------


def test_validate_config_accepts_valid_dict():
    from quackers.cli import validate_config

    cfg = validate_config({"employment_types": ["internship"], "max_results": 100})
    assert cfg.max_results == 100


def test_validate_config_rejects_bad_value():
    from pydantic import ValidationError
    from quackers.cli import validate_config

    with pytest.raises(ValidationError):
        validate_config({"max_results": "not-a-number"})


def test_persisted_config_keeps_title_gate_fields():
    """Saving must not drop fields that normalized_dict() omits."""
    import json
    from config.runtime_settings import _payload

    cfg = SearchConfig(primary_title="Data Analyst", require_verified_location=True)
    data = json.loads(_payload(cfg))
    assert data["primary_title"] == "Data Analyst"
    assert data["require_verified_location"] is True
    assert SearchConfig(**data).primary_title == "Data Analyst"

"""Configuration package."""
from __future__ import annotations

from .settings import AppConfig, PROJECT_ROOT, get_config
from .search_config import (
    BooleanGroups,
    EmploymentType,
    SearchConfig,
    classify_employment_type,
    default_search_config,
    normalize_employment_type,
)

__all__ = [
    "AppConfig",
    "BooleanGroups",
    "EmploymentType",
    "PROJECT_ROOT",
    "SearchConfig",
    "classify_employment_type",
    "default_search_config",
    "get_config",
    "normalize_employment_type",
]

"""Sources package: job-source adapters + registries."""
from __future__ import annotations

from .base import JobSource, RawListing, SourceResult
from .registry import REGISTRY, enabled_sources, get, list_sources, register
from .search_engine import ScanSummary, SearchEngine

__all__ = [
    "JobSource",
    "REGISTRY",
    "RawListing",
    "ScanSummary",
    "SearchEngine",
    "SourceResult",
    "enabled_sources",
    "get",
    "list_sources",
    "register",
]


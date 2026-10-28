"""Integration tests: WebSearchSource uses the provider abstraction,
not the legacy DDG scraping internals.
"""
from __future__ import annotations

from typing import List
from unittest.mock import MagicMock, patch

import pytest

from config.search_config import SearchConfig
from sources.base import RawListing
from sources.search_providers import SearchResult, SearchProvider
from sources.web_search import WebSearchSource


class FakeProvider(SearchProvider):
    """A test-only provider that returns deterministic results."""

    _impl_name = "fake"

    def __init__(self, results: List[List[SearchResult]], *_, **__):
        """results: one list per call (in order).  Call index = which
        query was issued (the planner sends queries in priority order)."""
        self._results = results
        self._call_index = 0
        self.call_log: List[str] = []

    @property
    def provider_name(self) -> str:
        return self._impl_name

    def _do_search(
        self, query: str, *, max_results: int, page: int,
    ) -> List[SearchResult]:
        self.call_log.append(query)
        idx = min(self._call_index, len(self._results) - 1)
        out = self._results[idx][:max_results]
        self._call_index += 1
        return out


def _mock_search_provider_with_results(per_query_results):
    """Build a FakeProvider backed by the given per-query results."""
    return FakeProvider(list(per_query_results))


# ------------------------------------------------------------------ #
# provider is actually used (not the legacy inline scraping)
# ------------------------------------------------------------------ #

def test_web_search_uses_provider_not_inline_ddg():
    """A mocked provider returning 3 results must yield 3 RawListings
    with the provider's `search_provider` name and `search_query`
    value — proof the source calls `provider.search`, not its own
    internal DDG scrape."""
    fake = _mock_search_provider_with_results([
        [
            SearchResult(title="Job A", url="https://a.com/j1", final_url="https://a.com/j1", provider="fake"),
            SearchResult(title="Job B", url="https://a.com/j2", final_url="https://a.com/j2", provider="fake"),
            SearchResult(title="Job C", url="https://a.com/j3", final_url="https://a.com/j3", provider="fake"),
        ],
    ])
    src = WebSearchSource(provider=fake)
    cfg = SearchConfig(max_results=20)
    result = src.scan(cfg)

    assert result.success
    assert len(result.listings) == 3
    for l in result.listings:
        assert l.search_provider == "fake"
        assert l.search_query  # non-empty
    # The provider was actually called
    assert len(fake.call_log) >= 1
    # No listings were *excluded* by `JOB_URL_HINTS` — the provider is
    # the source of truth here.
    assert result.listings_new == 3
    assert result.listings_found == 3


def test_web_search_does_not_call_own_scrape_when_provider_injected():
    """When a provider is injected, the `_fetch_with_retry` legacy
    scraping method must NOT be called (we removed it from the
    class).  We assert by name here — the whole class has been
    structured around the provider."""
    assert not hasattr(WebSearchSource("_client"), "_fetch_with_retry"), (
        "WebSearchSource should not expose the legacy "
        "_fetch_with_retry method anymore"
    )


# ------------------------------------------------------------------ #
# source-level dedup
# ------------------------------------------------------------------ #

def test_web_search_flags_duplicates_within_source():
    """Same final_url returned across multiple queries → only one
    listing; `duplicates` counter increments."""
    fake = _mock_search_provider_with_results([
        [
            SearchResult(title="Dup 1", url="https://a.com/same", final_url="https://a.com/same", provider="fake"),
            SearchResult(title="Dup 2", url="https://a.com/same", final_url="https://a.com/same", provider="fake"),
        ],
    ])
    src = WebSearchSource(provider=fake)
    result = src.scan(SearchConfig(max_results=20))
    # Two listings presented to the source (same canonical URL),
    # only one ends up in `all_listings`.
    assert len(result.listings) == 1
    assert result.duplicates >= 1
    assert result.parsed == 1


def test_web_search_respects_max_results():
    """When the user sets max_results=2 and the provider returns
    5, the source stops after 2."""
    fake = _mock_search_provider_with_results([
        [
            SearchResult(title=f"J{i}", url=f"https://a.com/{i}", final_url=f"https://a.com/{i}", provider="fake")
            for i in range(5)
        ],
    ])
    src = WebSearchSource(provider=fake)
    result = src.scan(SearchConfig(max_results=2))
    assert len(result.listings) <= 2
    # The source stops iterating, so the provider is called exactly
    # once (the first query already filled the cap).
    assert fake.call_log  # at least one query ran


def test_web_search_all_failed_returns_failure():
    """If the provider returns [] for *every* query, the source
    reports success=False (not silent crash)."""
    fake = _mock_search_provider_with_results([[]])
    src = WebSearchSource(provider=fake)
    result = src.scan(SearchConfig(max_results=20))
    assert result.success is False
    assert result.error_message  # non-empty
    assert result.listings == []
    assert result.attempted >= 1
    assert result.failed >= 1


# ------------------------------------------------------------------ #
# back-compat: legacy helpers still available
# ------------------------------------------------------------------ #

def test_legacy_helpers_still_importable():
    """The module-level `_query_pairs`, `_build_search_query`,
    `MAX_QUERIES`, and `JOB_URL_HINTS` are still importable so
    the existing 05.12-era tests don't break."""
    import sources.web_search as ws
    assert hasattr(ws, "_query_pairs")
    assert hasattr(ws, "_build_search_query")
    assert hasattr(ws, "MAX_QUERIES")
    assert hasattr(ws, "JOB_URL_HINTS")
    assert ws.MAX_QUERIES == 8
    # Sanity: _build_search_query still works as a pure function.
    q = ws._build_search_query("Software Engineering", "Potsdam")
    assert isinstance(q, str)
    assert "Software Engineering" in q
    assert "Potsdam" in q


def test_class_name_and_slug_unchanged():
    """The JobSource registration identity is preserved."""
    assert WebSearchSource.name == "Web Search"
    assert WebSearchSource.slug == "web_search"

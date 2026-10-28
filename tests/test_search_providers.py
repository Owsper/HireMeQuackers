"""Unit tests for the search-provider abstraction.

All external HTTP is mocked — no real network is required and no
live API keys are needed.
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from sources.search_providers import (
    BraveSearchProvider,
    DuckDuckGoSearchProvider,
    ExaSearchProvider,
    SearchResult,
    SearchProvider,
    get_search_provider,
)

# ------------------------------------------------------------------ #
# fixtures
# ------------------------------------------------------------------ #

def _fake_brave_json(results: list[dict]) -> str:
    return json.dumps({"web": {"results": results}})


def _fake_response_200(body_text: str) -> MagicMock:
    m = MagicMock(spec=httpx.Response)
    m.status_code = 200
    m.text = body_text
    def _json():
        return json.loads(body_text)
    m.json = _json
    return m


def _fake_response_error(status: int, body: str = "") -> MagicMock:
    m = MagicMock(spec=httpx.Response)
    m.status_code = status
    m.text = body
    def _json():
        return {}
    m.json = _json
    return m


# ------------------------------------------------------------------ #
# factory tests
# ------------------------------------------------------------------ #

def test_get_search_provider_falls_back_when_no_brave_key():
    """No BRAVE_SEARCH_API_KEY → DuckDuckGoSearchProvider (never crash)."""
    fake_app = MagicMock(brave_search_api_key="", exa_api_key="")
    with patch("sources.search_providers._resolve_app_config", return_value=fake_app):
        from config.search_config import SearchConfig
        cfg = SearchConfig(search_provider="brave")
        p = get_search_provider(cfg, app_config=fake_app)
        assert isinstance(p, DuckDuckGoSearchProvider)
        assert p.provider_name == "duckduckgo"


def test_get_search_provider_prefers_brave_when_key_set():
    """BRAVE_SEARCH_API_KEY set → BraveSearchProvider is returned."""
    fake_app = MagicMock(brave_search_api_key="XBraveK", exa_api_key="")
    from config.search_config import SearchConfig
    cfg = SearchConfig(search_provider="brave")
    p = get_search_provider(cfg, app_config=fake_app)
    assert isinstance(p, BraveSearchProvider)
    assert p.provider_name == "brave"
    p.close()


def test_get_search_provider_falls_back_on_exa():
    """Exa without key → DuckDuckGoSearchProvider (warn, do not crash)."""
    fake_app = MagicMock(brave_search_api_key="", exa_api_key="")
    from config.search_config import SearchConfig
    cfg = SearchConfig(search_provider="exa")
    p = get_search_provider(cfg, app_config=fake_app)
    assert isinstance(p, DuckDuckGoSearchProvider)
    assert p.provider_name == "duckduckgo"


def test_get_search_provider_duckduckgo_when_explicit():
    """search_provider='duckduckgo' → DDG even if brave key present."""
    fake_app = MagicMock(brave_search_api_key="KEY", exa_api_key="")
    from config.search_config import SearchConfig
    cfg = SearchConfig(search_provider="duckduckgo")
    p = get_search_provider(cfg, app_config=fake_app)
    assert isinstance(p, DuckDuckGoSearchProvider)


# ------------------------------------------------------------------ #
# Brave provider tests
# ------------------------------------------------------------------ #

def test_brave_search_parses_200():
    """A 200 JSON body with N results returns N SearchResults."""
    body = _fake_brave_json([
        {"title": "Job 1", "url": "https://a.com/job1", "description": "text1", "page_age": "2025-09-01 00:00"},
        {"title": "Job 2", "url": "https://a.com/job2", "description": "text2", "page_age": "2025-09-02 00:00"},
        {"title": "Job 3", "url": "https://a.com/job3", "description": "text3", "page_age": "2025-09-03 00:00"},
    ])
    fake_resp = _fake_response_200(body)
    fake_client = MagicMock(spec=httpx.Client)
    fake_client.get.return_value = fake_resp

    provider = BraveSearchProvider(api_key="XBraveK", min_delay_s=0.0)
    provider._client = fake_client
    provider._min_delay_s = 0.0

    results = provider.search("software intern berlin", max_results=10)
    assert len(results) == 3
    assert results[0].title == "Job 1"
    assert results[0].url == "https://a.com/job1"
    assert results[0].provider == "brave"
    assert results[0].page_age == "2025-09-01 00:00"
    assert fake_client.get.call_count == 1


def test_brave_search_handles_429():
    """A 429 (then a 500 retry) returns [] rather than raising."""
    fake_client = MagicMock(spec=httpx.Client)
    fake_client.get.side_effect = [
        _fake_response_error(429, '{"detail":"too many requests"}'),
        _fake_response_error(500, '{"detail":"server error"}'),
    ]
    provider = BraveSearchProvider(api_key="XBraveK", min_delay_s=0.0)
    provider._client = fake_client
    provider._min_delay_s = 0.0

    results = provider.search("intern", max_results=5)
    assert results == []
    # Two HTTP calls made (original + one retry)
    assert fake_client.get.call_count == 2


def test_brave_search_handles_500():
    """A 500 (then a 500 retry) returns [] without raising."""
    fake_client = MagicMock(spec=httpx.Client)
    fake_client.get.side_effect = [
        _fake_response_error(500),
        _fake_response_error(502),
    ]
    provider = BraveSearchProvider(api_key="XBraveK", min_delay_s=0.0)
    provider._client = fake_client
    provider._min_delay_s = 0.0

    results = provider.search("intern", max_results=5)
    assert results == []


def test_brave_search_returns_empty_on_exception():
    """A transport exception (network down) returns [] (not crash)."""
    fake_client = MagicMock(spec=httpx.Client)
    fake_client.get.side_effect = httpx.ConnectError("boom")
    provider = BraveSearchProvider(api_key="XBraveK", min_delay_s=0.0)
    provider._client = fake_client
    provider._min_delay_s = 0.0

    results = provider.search("intern", max_results=5)
    assert results == []


def test_brave_search_pagination():
    """page=2 sends offset = count.  Two pages produce disjoint results."""
    body1 = _fake_brave_json([
        {"title": "A", "url": "https://a.com/1", "description": "", "page_age": None},
    ])
    body2 = _fake_brave_json([
        {"title": "B", "url": "https://a.com/2", "description": "", "page_age": None},
    ])
    fake_client = MagicMock(spec=httpx.Client)
    fake_client.get.side_effect = [
        _fake_response_200(body1),
        _fake_response_200(body2),
    ]
    provider = BraveSearchProvider(api_key="XBraveK", min_delay_s=0.0)
    provider._client = fake_client
    provider._min_delay_s = 0.0

    results1 = provider.search("intern", max_results=5, page=1)
    results2 = provider.search("intern", max_results=5, page=2)
    assert len(results1) == 1
    assert len(results2) == 1
    # Inspect the `offset` parameter passed to the API.
    call1_kwargs = fake_client.get.call_args_list[0].kwargs
    call2_kwargs = fake_client.get.call_args_list[1].kwargs
    assert call1_kwargs["params"]["offset"] == 0
    assert call2_kwargs["params"]["offset"] == 5


def test_brave_search_rate_limit_between_calls(monkeypatch):
    """Consecutive calls sleep at least `min_delay_s` between them."""
    body = _fake_brave_json([
        {"title": "X", "url": "https://x.com/1", "description": "", "page_age": None},
    ])
    fake_client = MagicMock(spec=httpx.Client)
    fake_client.get.return_value = _fake_response_200(body)

    real_sleep = __import__("time").sleep
    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s) or None)

    provider = BraveSearchProvider(api_key="XBraveK", min_delay_s=5.0)
    provider._client = fake_client
    provider._last_request_at = 1000.0  # set to "5s ago" effectively

    # 1st call: sleep ≈ 0 (we set _last_request_at to 1000 => wait ~0)
    provider.search("q1")
    # 2nd call: should sleep ~5s
    provider.search("q2")

    assert sleeps
    # The last sleep should be roughly 5.0 (timeout 12 is irrelevant
    # for this mock).
    assert sleeps[-1] >= 4.9, f"rate-limit not applied: sleeps={sleeps}"


# ------------------------------------------------------------------ #
# DuckDuckGo provider tests
# ------------------------------------------------------------------ #

def _fake_fetch_body() -> bytes:
    """Minimal DDG HTML with 2 result__a anchors + a redirect."""
    return (
        b'<a class="result__a" href="/l/?uddg=https%3A%2F%2Fa.com%2Fjob1">Job A</a>'
        b'<a class="result__a" href="https://b.com/jobs/job2">Job B</a>'
    )


def test_ddg_provider_parses_anchors(monkeypatch):
    """The anchor regex + redirect unwrap works end-to-end."""
    fake_get = MagicMock()
    fake_get.return_value = MagicMock(
        status_code=200,
        content=_fake_fetch_body(),
        headers={}),
    with patch("sources.search_providers.safe_get", side_effect=lambda url, **kw: MagicMock(
        status_code=200, content=_fake_fetch_body(), error=None
    )):
        provider = DuckDuckGoSearchProvider(min_delay_s=0.0)
        provider._min_delay_s = 0.0
        results = provider.search("intern", max_results=10)
    assert len(results) >= 1
    assert results[0].provider == "duckduckgo"
    # The first result should have the redirect unwrapped
    assert any(r.final_url and r.final_url.startswith("https://") for r in results)


def test_ddg_provider_returns_empty_on_fetch_failure(monkeypatch):
    """On network failure (safe_get raises) → []."""
    with patch("sources.search_providers.safe_get", side_effect=Exception("boom")):
        provider = DuckDuckGoSearchProvider(min_delay_s=0.0)
        provider._min_delay_s = 0.0
        results = provider.search("intern", max_results=10)
    assert results == []


def test_ddg_provider_dedupes_by_final_url():
    """If two anchors have the same `uddg=` target, only one is returned."""
    body = (
        b'<a class="result__a" href="/l/?uddg=https%3A%2F%2Fa.com%2Fsame">First</a>'
        b'<a class="result__a" href="/l/?uddg=https%3A%2F%2Fa.com%2Fsame">Second</a>'
    )
    with patch("sources.search_providers.safe_get", return_value=MagicMock(
        status_code=200, content=body, error=None
    )):
        provider = DuckDuckGoSearchProvider(min_delay_s=0.0)
        provider._min_delay_s = 0.0
        results = provider.search("intern", max_results=10)
    assert len(results) == 1


# ------------------------------------------------------------------ #
# Exa stub
# ------------------------------------------------------------------ #

def test_exa_provider_falls_back_to_ddg():
    """Exa without key → delegates to DuckDuckGo."""
    with patch("sources.search_providers.safe_get", side_effect=Exception("no pets (mocked)")):
        provider = ExaSearchProvider(api_key="")
        results = provider.search("intern", max_results=5)
    assert results == []  # DDG fails; no exception
    assert provider._impl_name == "exa"

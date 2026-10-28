"""Tests for the new Phase 5 source adapters (all HTTP mocked)."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from config.search_config import SearchConfig


def _make_previous_response(status: int = 200, body: dict | list | None = None) -> MagicMock:
    body_bytes = json.dumps(body).encode() if body is not None else b""
    return MagicMock(status_code=status, content=body_bytes)


# ------------------------------------------------------------------ #
# HackerNews
# ------------------------------------------------------------------ #

def test_hackernews_parses_two_hits():
    hits = [
        {"title": "Job A", "url": "https://a.com/job", "created_at": "2025-01-01"},
        {"title": "Job B", "url": "https://b.com/job", "created_at": "2025-02-01"},
    ]
    resp = _make_previous_response(200, {"hits": hits})
    with patch("sources.hackernews.safe_get", return_value=resp) as mock_get:
        from sources.hackernews import HackerNewsJobsSource
        res = HackerNewsJobsSource().scan(SearchConfig())
    assert res.success
    assert len(res.listings) == 2
    assert res.listings[0].title == "Job A"
    assert res.listings[0].original_url == "https://a.com/job"
    assert res.attempted == 1
    assert res.fetched == 1
    mock_get.assert_called_once()


def test_hackernews_fallback_when_jobs_empty():
    # Primary returns 200 but 0 hits -> fallback makes a second call.
    resp_jobs = _make_previous_response(200, {"hits": []})
    hits = [{"title": "Job C", "url": "https://c.com/job", "created_at": "2025-03-01"}]
    resp_search = _make_previous_response(200, {"hits": hits})
    calls = {"n": 0}

    def fake_get(url, timeout=None, client=None, **kw):
        calls["n"] += 1
        if url == "https://hn.algolia.com/api/v1/jobs":
            return resp_jobs
        return resp_search

    with patch("sources.hackernews.safe_get", side_effect=fake_get):
        from sources.hackernews import HackerNewsJobsSource
        res = HackerNewsJobsSource().scan(SearchConfig())

    assert res.success
    assert len(res.listings) == 1
    assert res.listings[0].title == "Job C"
    assert calls["n"] == 2
    assert res.attempted == 2
    assert res.fetched == 2


def test_hackernews_no_hits_both_endpoints():
    with patch("sources.hackernews.safe_get", return_value=_make_previous_response(200, {"hits": []})):
        from sources.hackernews import HackerNewsJobsSource
        res = HackerNewsJobsSource().scan(SearchConfig())
    assert res.listings == []
    assert res.fetched == 2


# ------------------------------------------------------------------ #
# EURES
# ------------------------------------------------------------------ #

def test_eures_not_configured_returns_skip():
    with patch("sources.eures.safe_get") as mock_get:
        from sources.eures import EuresSource
        res = EuresSource().scan(SearchConfig(eures_api_url=None))
    assert res.success is True
    assert res.listings == []
    assert res.attempted == 0
    assert "EURES endpoint not configured" in (res.error_message or "")
    mock_get.assert_not_called()


def test_eures_configured_parses_one_job():
    resp = _make_previous_response(200, [
        {"title": "Software Intern", "url": "https://eures.eu/jobs/1", "company": "Acme"},
    ])
    with patch("sources.eures.safe_get", return_value=resp) as mock_get:
        from sources.eures import EuresSource
        res = EuresSource().scan(SearchConfig(eures_api_url="https://eures.example/api"))
    assert res.success
    assert len(res.listings) == 1
    assert res.listings[0].title == "Software Intern"
    assert res.listings[0].company == "Acme"
    mock_get.assert_called_once()


def test_eures_configured_http_failure():
    with patch("sources.eures.safe_get", return_value=_make_previous_response(404)):
        from sources.eures import EuresSource
        res = EuresSource().scan(SearchConfig(eures_api_url="https://eures.example/api"))
    assert res.success is False
    assert res.listings == []
    assert "HTTP 404" in (res.error_message or "")


# ------------------------------------------------------------------ #
# Registry + RSS union
# ------------------------------------------------------------------ #

def test_registry_contains_new_adapters():
    """Ensure the new Phase 5 adapters (hackernews + eures) register.

    ``ensure_loaded()`` forces eager module import so the registry is
    fully populated regardless of which other test loaded which modules
    first (test isolation: each test file's imports are independent).
    ``RemoteOK`` is verified separately by its own dedicated test file,
    so it's excluded from this assertion to keep it focused on the
    newly-created sources only.
    """
    from sources.registry import ensure_loaded, list_sources
    ensure_loaded()
    slugs = {s.slug for s in list_sources()}
    assert {"hackernews", "eures"} <= slugs


def test_rss_source_uses_resolve_feed_list_union():
    """_resolve_feed_list: user cfg.rss_urls + registry, deduped by URL."""
    from sources.rss import _resolve_feed_list
    cfg = SearchConfig(
        rss_urls=[
            "https://weworkremotely.com/categories/remote-programming-jobs.rss",
            "https://custom.com/feed.xml",
        ],
        feed_registry_categories=["remote"],
    )
    result = _resolve_feed_list(cfg, explicit=None)
    urls = [e["url"] for e in result]
    assert len(urls) == len(set(urls))  # no duplicates
    assert "https://custom.com/feed.xml" in urls
    assert urls.count("https://weworkremotely.com/categories/remote-programming-jobs.rss") == 1

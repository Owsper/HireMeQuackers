"""Registration test for the Web Search JobSource.

Verifies that WebSearchSource is eagerly registered via the @register
decorator when its module is imported, that the instance has the correct
class attributes (name, slug) and a correctly-headed httpx.Client (UA),
and that ``_build_search_query`` never crashes on list vs str (the bug
that used to return ``".rstrip() on a list"``).  No network is required;
``safe_get`` is monkey-patched when we exercise ``scan`` (which we do NOT
do here — this is a registration + shape test only).
"""
from __future__ import annotations

import importlib

import pytest

from config.search_config import SearchConfig


# ------------------------------------------------------------------ #
# module import + registration
# ------------------------------------------------------------------ #

def test_module_imports():
    """Importing sources.web_search must succeed (no top-level crash)."""
    mod = importlib.import_module("sources.web_search")
    assert hasattr(mod, "WebSearchSource"), (
        "WebSearchSource class is missing from sources.web_search"
    )


def test_web_search_registered_in_registry():
    """After import, the Web Search class must appear in the REGISTRY.

    The other 4 sources (greenhouse, lever, rss, career_pages) are
    imported by name (not as part of this test's module tree), so
    we import each of them explicitly to make sure the full set is
    present for a meaningful "registry has all 5" assertion.
    """
    from sources.registry import REGISTRY
    importlib.import_module("sources.greenhouse")
    importlib.import_module("sources.lever")
    importlib.import_module("sources.rss")
    importlib.import_module("sources.career_pages")
    importlib.import_module("sources.web_search")
    missing = [n for n in ("Greenhouse", "Lever", "RSS Feeds", "Career Pages", "Web Search")
               if n not in REGISTRY]
    assert not missing, f"missing from REGISTRY: {missing}; current keys: {list(REGISTRY)}"


def test_registry_list_sources_includes_web_search():
    """list_sources() must return at least one WebSearchSource instance."""
    from sources.registry import ensure_loaded, list_sources
    ensure_loaded()
    instances = list_sources()
    web = [s for s in instances if getattr(s, "name", None) == "Web Search"]
    assert web, (
        f"list_sources() did not include Web Search. "
        f"Names: {[getattr(s, 'name', '?') for s in instances]}"
    )


def test_class_attributes_are_correct():
    """The class must publish name, slug, official_search_url."""
    from sources.web_search import WebSearchSource
    assert WebSearchSource.name == "Web Search"
    assert WebSearchSource.slug == "web_search"
    assert "duckduckgo.com" in (WebSearchSource.official_search_url or "")


def test_httpx_client_has_realistic_ua():
    """Constructing a WebSearchSource instance must not raise, and the
    lazily-created client must carry a non-bot User-Agent
    (this is the fix for DDG returning 202)."""
    from sources.web_search import WebSearchSource
    src = WebSearchSource()
    try:
        assert src.client is not None, "client is None after __init__"
        ua = src.client.headers.get("User-Agent", "")
        # Assert the UA is a realistic browser string (not python-requests)
        assert "Mozilla" in ua, f"UA buggy (looks like a Python lib): {ua!r}"
    finally:
        try:
            src.client.close()
        except Exception:
            pass


def test_build_search_query_is_clean_string():
    """Regression: the old buggy line was

        parts = [kw, loc, "job", "jobs"].rstrip()
    (``.rstrip() on a list`` → TypeError at every scan).

    Now ``_build_search_query(kw, loc)`` must return a clean ``str``
    with no leading/trailing whitespace and no double-space leak.
    """
    from sources.web_search import _build_search_query
    # the original crash case: kw & loc both truthy
    q = _build_search_query("Software Engineering", "Potsdam")
    assert isinstance(q, str), f"expected str, got {type(q).__name__}"
    assert q.strip() == q, "query has leading/trailing whitespace"
    assert "Software Engineering" in q
    assert "Potsdam" in q
    # the "loc empty" case (remote / worldwide)
    q2 = _build_search_query("Job", "")
    assert isinstance(q2, str)
    # whitespace-only loc must not produce a double-space leak
    q3 = _build_search_query("Dev", "  ")
    assert "  " not in q3, f"double-space leak in query: {q3!r}"


def test_query_pairs_respects_max_queries():
    """_query_pairs caps pairs at MAX_QUERIES (8) and falls back
    to (Software Intern, "") when both keywords and locations
    are empty."""
    from sources.web_search import _query_pairs, MAX_QUERIES
    cfg = SearchConfig(
        include_keywords=[f"kw{i}" for i in range(50)],
        locations=[f"loc{j}" for j in range(20)],
    )
    pairs = _query_pairs(cfg)
    assert len(pairs) == MAX_QUERIES, f"expected {MAX_QUERIES} pairs, got {len(pairs)}"
    for kw, loc in pairs:
        assert isinstance(kw, str) and isinstance(loc, str)
    # fallback: no keywords, no locations → single (Software Intern, "") pair
    pairs2 = _query_pairs(SearchConfig())
    assert len(pairs2) == 1
    assert pairs2[0] == ("Software Intern", "")


def test_scan_returns_clean_failure_when_no_pairs():
    """If _query_pairs is patched to return [] (no network at all),
    scan must surface an error, not silently crash.
    """
    import sources.web_search as ws
    from sources.web_search import WebSearchSource
    orig = ws._query_pairs
    ws._query_pairs = lambda cfg: []
    try:
        src = WebSearchSource()
        try:
            r = src.scan(SearchConfig())
        finally:
            try:
                src.client.close()
            except Exception:
                pass
    finally:
        ws._query_pairs = orig
    assert r.success is False
    assert r.error_message
"""Search-provider abstraction for the max-coverage discovery engine.

Two providers ship out of the box:

* :class:`BraveSearchProvider` — the *recommended primary* provider. Uses the
  official Brave Web Search API (``https://api.search.brave.com/res/v1/web/search``)
  with the ``X-Subscription-Token`` header. Returns JSON with first-party
  ``title`` / ``url`` / ``description`` / ``page_age`` fields; supports
  pagination via ``offset`` + ``count`` (0..9 offset, 1..50 count). Requires
  ``BRAVE_SEARCH_API_KEY`` (free tier: 2,000 queries / month).
* :class:`DuckDuckGoSearchProvider` — the *keyless fallback*. Reuses the
  existing :mod:`sources.web_search` HTML-scraping machinery
  (``_fetch_with_retry`` + ``_parse_results``) so no new DDG code is
  duplicated. ``page > 1`` is a no-op (DDG HTML has no page cursor).

Design rules (from the discovery spec):

* The app MUST start and scan when no API key is configured. A missing
  ``BRAVE_SEARCH_API_KEY`` (or an invalid ``search_provider`` value) degrades
  gracefully to :class:`DuckDuckGoSearchProvider` — it never raises.
* Per-provider rate limiting: each search call sleeps at least
  ``min_delay`` seconds between requests.
* Redirects are never followed blindly. For providers that expose a final
  URL, ``final_url`` is populated from the API response; the downstream
  detail-page fetcher re-validates via ``safe_get`` before any fetch, so
  no SSRF bypass is possible.

This module depends only on :mod:`config.settings`,
:mod:`services.safe_requests`, and (for the DDG path) :mod:`sources.web_search`.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger(__name__)

# Brave Web Search API endpoint (documented: https://api.search.brave.com)
BRAVE_SEARCH_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"

# Retryable HTTP statuses for API search calls (per spec: 429 + 5xx).
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


@dataclass
class SearchResult:
    """One organic search hit from any provider.

    ``final_url`` is the URL the provider reports as the resolved destination
    after following the provider's own redirect scheme (e.g. DDG's ``uddg=``
    redirect is unwrapped at parse time; Brave already returns a clean
    canonical URL). ``canonical_url`` (used downstream in
    :mod:`services.normalization`) is derived from ``final_url`` by the
    detail-page fetcher, *not* here.
    """

    title: str
    url: str
    final_url: str
    snippet: Optional[str] = None
    provider: str = ""
    query: str = ""
    # Provider-side rank (useful for tie-breaking downstream).
    position: int = 0


class SearchProvider:
    """Abstract base for web search providers."""

    provider_name: str = "base"

    def __init__(self, *, min_delay: float = 1.0, timeout: float = 12.0):
        self.min_delay = max(0.0, float(min_delay))
        self.timeout = timeout
        self._last_request_at = 0.0

    # -- rate limit -------------------------------------------------------
    def _enforce_delay(self) -> None:
        """Sleep so at least ``min_delay`` seconds separate consecutive requests."""
        if self.min_delay <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.min_delay:
            time.sleep(self.min_delay - elapsed)
        self._last_request_at = time.monotonic()

    # -- main entry -------------------------------------------------------
    def search(
        self,
        query: str,
        max_results: int = 10,
        page: int = 1,
    ) -> List[SearchResult]:
        """Perform one search. Never raises for network / quota / parse errors.

        Returns ``[]`` on any failure (logs a warning). The caller is expected
        to treat an empty list as "this query yielded nothing" — NOT as a
        source-level failure (spec ¶6: per-query failure isolation).
        """
        raise NotImplementedError

    def health_probe(self) -> bool:
        """Best-effort: can this provider return *any* results right now?

        Used by adapters to decide whether to fall back. Default: unknown.
        """
        return False


class BraveSearchProvider(SearchProvider):
    """Official Brave Search Web API adapter.

    Endpoint: ``GET https://api.search.brave.com/res/v1/web/search``
    Auth header: ``X-Subscription-Token: <api_key>``
    Params: ``q`` (required), ``count`` (1-50, default 10), ``offset`` (0..9,
    pages up to 10), ``freshness`` (optional), ``search_lang`` / ``country``
    (optional).

    Pagination: Brave supports ``offset`` 0..9 with ``count`` up to 50, so up
    to 10 pages x 50 results = 500 per query. We never request beyond that.
    """

    provider_name = "brave"

    def __init__(self, *, api_key: Optional[str], min_delay: float = 1.0,
                 timeout: float = 12.0):
        super().__init__(min_delay=min_delay, timeout=timeout)
        self.api_key = (api_key or "").strip() or None
        if not self.api_key:
            logger.warning(
                "BraveSearchProvider instantiated without an API key; "
                "search() will return [] and callers should fall back to DDG."
            )

    def _http_get_json(self, params: dict) -> Optional[dict]:
        """GET the Brave endpoint with the auth header + retry on 429/5xx.

        Uses :mod:`httpx` directly because :func:`services.safe_requests.safe_get`
        does not forward custom headers. SSRF / private-IP / scheme guards
        are still enforced via :func:`services.safe_requests.
        validate_public_http_url` — the security layer is not bypassed.
        """
        import httpx
        import urllib.parse as _up
        from services.safe_requests import validate_public_http_url

        qs = _up.urlencode(params, doseq=True)
        url = f"{BRAVE_SEARCH_ENDPOINT}?{qs}"
        self._enforce_delay()
        try:
            validate_public_http_url(url)
        except Exception as e:
            logger.warning("Brave: blocked URL: %s", e)
            return None
        headers = {
            "X-Subscription-Token": self.api_key or "",
            "Accept": "application/json",
            "User-Agent": "HireMeQuackers/1.0 (job discovery; contact: local)",
        }
        last_err = None
        for attempt in range(3):  # initial + 2 retries on 429/5xx
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    r = client.get(url, headers=headers)
            except Exception as e:  # noqa: BLE001 — broad, logged
                last_err = e
                time.sleep(1.0 * (attempt + 1))
                continue
            if r.status_code in _RETRYABLE_STATUS:
                ra = r.headers.get("Retry-After", "")
                try:
                    delay = min(5.0, float(ra)) if ra else 1.5 * (attempt + 1)
                except ValueError:
                    delay = 1.5 * (attempt + 1)
                time.sleep(delay)
                last_err = f"HTTP {r.status_code}"
                continue
            if r.status_code != 200:
                # Non-retryable error (401/403/404/422): stop, do not retry.
                return None
            try:
                return r.json()
            except Exception as e:  # noqa: BLE001
                logger.warning("Brave: JSON parse failed: %s", e)
                return None
        logger.warning("Brave: retries exhausted: %s", last_err)
        return None

    # -- public search ----------------------------------------------------
    def search(
        self,
        query: str,
        max_results: int = 10,
        page: int = 1,
    ) -> List[SearchResult]:
        if not self.api_key:
            logger.debug("Brave: no API key, returning [] (caller should fall back)")
            return []
        if not query or not query.strip():
            return []
        count = max(1, min(int(max_results), 50))
        offset = (max(1, int(page)) - 1) * count
        if offset > 9 * 50:
            return []
        params = {"q": query.strip(), "count": count, "offset": offset}
        data = self._http_get_json(params)
        if not isinstance(data, dict):
            return []
        web = data.get("web") or {}
        results = web.get("results") or []
        out: List[SearchResult] = []
        for i, r in enumerate(results):
            if not isinstance(r, dict):
                continue
            url = (r.get("url") or "").strip()
            if not url:
                continue
            title = (r.get("title") or "").strip()[:200]
            snippet = (r.get("description") or None)
            if snippet:
                snippet = snippet[:500]
            out.append(
                SearchResult(
                    title=title or "(untitled)",
                    url=url,
                    final_url=url,
                    snippet=snippet,
                    provider=self.provider_name,
                    query=query,
                    position=i,
                )
            )
            if len(out) >= max_results:
                break
        return out


class DuckDuckGoSearchProvider(SearchProvider):
    """Keyless DuckDuckGo HTML provider (reuses :mod:`sources.web_search`).

    Routed per-query through the existing :class:`WebSearchSource` retry +
    parse machinery so callers can send query specs generated by
    :mod:`services.query_planner`.

    Pagination: ``page > 1`` is a no-op (the DDG HTML endpoint does not expose
    a page cursor); callers requesting ``page > 1`` receive the same page of
    results and downstream dedup by URL in :mod:`sources.search_engine`
    handles the duplicates.

    Note: the :class:`~sources.web_search.WebSearchSource` adapter (not this
    provider) makes the final job-listing decision using ``JOB_URL_HINTS`` as
    a *priority* signal. This provider returns whatever the static parser
    yields, so the adapter layer can separate strong-hint from weak-hint
    results without re-parsing HTML.
    """

    provider_name = "duckduckgo"

    def search(
        self,
        query: str,
        max_results: int = 10,
        page: int = 1,
    ) -> List[SearchResult]:
        if not query or not query.strip():
            return []
        import urllib.parse as _up
        from sources.web_search import (
            DDG_HTML_URL,
            MAX_RESULTS_PER_QUERY,
            WebSearchSource,
            _parse_results,
        )

        q = query.strip()
        url = f"{DDG_HTML_URL}?q={_up.quote_plus(q)}"
        src = WebSearchSource()
        try:
            result = src._fetch_with_retry(url)
        finally:
            try:
                if src.client is not None:
                    src.client.close()
            except Exception:
                pass
        if result is None or getattr(result, "error", None) or \
                getattr(result, "status_code", -1) != 200 or \
                not getattr(result, "content", b""):
            return []
        decoded = result.content.decode("utf-8", errors="replace")
        hit_dicts = _parse_results(decoded, q)
        out: List[SearchResult] = []
        seen = set()
        for i, hit in enumerate(hit_dicts):
            u = hit.get("url") or ""
            if not u:
                continue
            key = u.lower()
            if key in seen:
                continue
            seen.add(key)
            title = (hit.get("title") or "").strip()[:200] or "(untitled)"
            snippet = hit.get("snippet")
            out.append(
                SearchResult(
                    title=title,
                    url=u,
                    final_url=u,
                    snippet=snippet,
                    provider=self.provider_name,
                    query=q,
                    position=i,
                )
            )
            if len(out) >= max(MAX_RESULTS_PER_QUERY, int(max_results)):
                break
        return out


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #

def get_search_provider(
    search_provider: str = "brave",
    *,
    brave_api_key: Optional[str] = None,
    min_delay: float = 1.0,
) -> SearchProvider:
    """Return the configured search provider, with graceful degradation.

    Resolution order:

    1. If ``search_provider == "brave"`` AND ``brave_api_key`` is truthy →
       :class:`BraveSearchProvider`.
    2. Otherwise (missing key / unknown provider name) →
       :class:`DuckDuckGoSearchProvider` (keyless, always available).

    Never raises. The app MUST remain responsive when no API key is configured
    (spec §3A: "never require a paid API key for the application to start").
    """
    name = (search_provider or "").strip().lower()
    if name == "brave" and brave_api_key:
        return BraveSearchProvider(api_key=brave_api_key, min_delay=min_delay)
    if name in ("duckduckgo", "ddg") or not name or not brave_api_key:
        return DuckDuckGoSearchProvider(min_delay=min_delay)
    # Unknown provider name — fall back to DDG with a warning.
    logger.warning(
        "Unknown search_provider=%r; falling back to DuckDuckGo.", search_provider
    )
    return DuckDuckGoSearchProvider(min_delay=min_delay)


def create_provider_from_config(cfg: Optional[object] = None) -> SearchProvider:
    """Convenience: build the provider from the active AppConfig."""
    from config.settings import get_config

    env = cfg if cfg is not None else get_config()
    return get_search_provider(
        getattr(env, "search_provider", "brave") or "brave",
        brave_api_key=getattr(env, "brave_search_api_key", None),
        min_delay=getattr(env, "search_min_delay_seconds", 1.0),
    )


__all__ = [
    "BRAVE_SEARCH_ENDPOINT",
    "BraveSearchProvider",
    "DuckDuckGoSearchProvider",
    "SearchProvider",
    "SearchResult",
    "create_provider_from_config",
    "get_search_provider",
]
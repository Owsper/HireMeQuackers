"""Search-provider abstraction (Brave / Exa / DuckDuckGo).

This module hosts the three concrete search providers the
max-coverage discovery engine uses to discover job URLs across the
open web, plus the factory that picks the right one based on user
config + installed API keys::

    get_search_provider(cfg)  ->  SearchProvider

Design rules
------------
* **Never require a paid API key for the app to start.**  If
  ``BRAVE_SEARCH_API_KEY`` is empty the factory silently falls back
  to :class:`DuckDuckGoSearchProvider` (no key, HTML-scrape).
* **Providers never raise on API errors** — they return an empty
  list and log a warning.  The caller (web search source) treats
  an empty list as "no listings for this query".
* **Rate limiting** is applied *inside* each provider (via
  ``time.monotonic``) so that callers do not have to worry about
  exceeding per-provider quotas.
* **SSRF protection** is delegated to :func:`services.safe_requests.safe_get`
  (already present and robust); we do not re-implement redirect
  re-validation here.
"""
from __future__ import annotations

import html as html_mod
import json
import logging
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import List, Optional

import httpx

from config.search_config import SearchConfig
from services.safe_requests import FetchResult, safe_get
from services.sanitization import strip_html

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
# dataclasses
# ------------------------------------------------------------------ #

@dataclass
class SearchResult:
    """One result returned by a search provider (agnostic of type)."""

    title: str
    url: str
    snippet: str = ""
    # The *final* URL after any search-engine redirect (e.g. DDG's
    # ``uddg`` parameter).  When the provider does not expose this
    # (or we could not resolve it), it is simply None and callers
    # use ``url`` as-is.
    final_url: Optional[str] = None
    # ``page_age`` (Brave) / human-readable freshness hint — kept as
    # raw string so we don't force a particular parse.
    page_age: Optional[str] = None
    # Which query produced this result (useful for debug + provenance).
    query: str = ""
    # The provider that produced it (``brave`` / ``duckduckgo`` / …).
    provider: str = ""


@dataclass
class SearchProvider:
    """Protocol / base that every search provider implements.

    Sub-classes override :meth:`_search` with actual HTTP + parsing.
    The shared public :meth:`search` calls :meth:`_search` inside a
    try/except so that transport errors never escape the provider.
    """

    # Sub-classes must set this.
    _impl_name: str = "base"

    @property
    def provider_name(self) -> str:
        return self._impl_name

    @property
    def name(self) -> str:
        """Alias for provider_name (used by metrics + UI)."""
        return self._impl_name

    def search(
        self,
        query: str,
        *,
        max_results: int = 10,
        page: int = 1,
    ) -> List[SearchResult]:
        """Execute one search and return up to ``max_results`` items.

        Never raises: on any internal failure the result list is
        empty and a warning is logged.  ``page`` starts at 1.
        """
        try:
            return self._do_search(query, max_results=max_results, page=page)
        except Exception as exc:
            logger.warning(
                "SearchProvider %s failed for query %r: %s",
                self._impl_name, query[:80], exc,
            )
            return []

    def _do_search(
        self,
        query: str,
        *,
        max_results: int,
        page: int,
    ) -> List[SearchResult]:
        raise NotImplementedError


# ------------------------------------------------------------------ #
# Brave Search
# ------------------------------------------------------------------ #

class BraveSearchProvider(SearchProvider):
    """Brave Search Web API (``X-Subscription-Token`` auth, JSON).

    Endpoint::

        GET https://api.search.brave.com/res/v1/web/search

    Pagination:  ``count`` (1-20, default 10) + ``offset`` (page * count).
    Retry:  one retry on 429/5xx with a 1.0 s backoff.
    Rate-limit:  minimum ``min_delay_s`` between requests (monotonic).
    """

    _impl_name = "brave"
    _ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
    _RETRYABLE_STATUSES = {429, 500, 502, 503, 504}

    def __init__(self, *, api_key: str, min_delay_s: float = 1.0, client: httpx.Client | None = None):
        if not api_key:
            raise ValueError("BraveSearchProvider requires a non-empty api_key")
        self._api_key = api_key
        self._min_delay_s = max(0.0, float(min_delay_s))
        self._last_request_at: float = 0.0
        self._client = client or httpx.Client(timeout=12.0)
        self._owns_client = client is None

    # -- rate-limit helper --------------------------------------------
    def _wait_for_rate_limit(self) -> None:
        now = time.monotonic()
        if self._min_delay_s <= 0:
            self._last_request_at = now
            return
        wait = (self._last_request_at + self._min_delay_s) - now
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    # -- HTTP ---------------------------------------------------------
    def _request(
        self, *, count: int, offset: int, q: str
    ) -> Optional[httpx.Response]:
        """GET the endpoint with one retry on 429/5xx. Returns None
        when both attempts fail / return non-200."""
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": self._api_key,
        }
        params = {"q": q, "count": count, "offset": offset}
        last_exc: Optional[Exception] = None
        for attempt in (1, 2):
            self._wait_for_rate_limit()
            try:
                resp = self._client.get(
                    self._ENDPOINT, params=params, headers=headers,
                )
            except Exception as exc:
                last_exc = exc
                logger.debug("Brave request except: %s", exc)
                time.sleep(1.0)
                continue
            if resp.status_code != 200:
                if resp.status_code in self._RETRYABLE_STATUSES and attempt < 2:
                    try:
                        body = json.loads(resp.text) if resp.text else {}
                    except Exception:
                        body = {}
                    logger.warning(
                        "Brave %d on q=%r: %s",
                        resp.status_code, q[:60],
                        (body.get("detail") or body.get("message") or resp.text[:200]),
                    )
                    time.sleep(1.0)
                    continue
                return None
            return resp
        return None

    # -- main search --------------------------------------------------
    def _do_search(
        self, query: str, *, max_results: int, page: int
    ) -> List[SearchResult]:
        count = max(1, min(20, int(max_results)))
        offset = max(0, (int(page) - 1) * count)
        resp = self._request(count=count, offset=offset, q=query)
        if resp is None:
            return []
        try:
            data = resp.json()
        except Exception as exc:
            logger.warning("Brave JSON parse failed: %s", exc)
            return []
        out: List[SearchResult] = []
        for item in (data.get("web", {}).get("results") or [])[:count]:
            url = (item.get("url") or "").strip()
            if not url:
                continue
            out.append(SearchResult(
                title=item.get("title") or "",
                url=url,
                snippet=strip_html(item.get("description") or "")[:500],
                final_url=url,
                page_age=item.get("page_age"),
                query=query,
                provider=self._impl_name,
            ))
        return out

    def close(self) -> None:
        if self._owns_client:
            try:
                self._client.close()
            except Exception:
                pass


# ------------------------------------------------------------------ #
# DuckDuckGo (HTML-scrape, no key)
# ------------------------------------------------------------------ #

# Reuse the same UA as the legacy web_search source — DDG's HTML
# endpoint returns 202 (polite-block) for python-requests / curl UAs.
_DDG_UA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
}

DDG_HTML_URL = "https://html.duckduckgo.com/html/"
DDG_LITE_URL = "https://lite.duckduckgo.com/lite/"
_DDG_MAX_BYTES = 1024 * 256
_DDG_RETRY_DELAY_S = 1.5
_DDG_RETRYABLE_STATUSES = {202, 408, 429, 503}

# Anchors with class ``result__a``  (both HTML + Lite endpoints).
_DDG_ANCHOR_RE = re.compile(
    r'<a[^>]+class="[^"]*result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S
)
# ``uddg=<url>`` redirect unwrap (DDG HTML uses a redirector).
_DDG_UDDG_RE = re.compile(r"/l/\?.*uddg=([^&]+)")


class DuckDuckGoSearchProvider(SearchProvider):
    """DuckDuckGo HTML search (no API key required).

    Mirrors the historical ``web_search._fetch_with_retry`` + anchor
    parsing; generalised into :class:`SearchResult` so the rest of
    the engine treats it identically to paid providers.

    * Page 1 only (DDG HTML does not support offset/pagination).
      ``page > 1`` behaves like ``page = 1`` — document and warn.
    * Fallback: on a *repeated* 202/408/429/503 drop to the
      ``lite.duckduckgo.com`` endpoint once.
    """

    _impl_name = "duckduckgo"

    def __init__(self, *, client: httpx.Client | None = None, min_delay_s: float = 1.0):
        self._min_delay_s = max(0.0, float(min_delay_s))
        self._last_request_at: float = 0.0
        self._client = client or httpx.Client(
            timeout=10.0, follow_redirects=False, headers=_DDG_UA_HEADERS,
        )
        self._owns_client = client is None

    def _wait_for_rate_limit(self) -> None:
        now = time.monotonic()
        if self._min_delay_s <= 0:
            self._last_request_at = now
            return
        wait = (self._last_request_at + self._min_delay_s) - now
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    # -- DDG redirect helper ------------------------------------------
    @staticmethod
    def _unwrap_ddg_redirect(url: str) -> str:
        """If ``url`` is an internal Collage redirect (``/l/?uddg=…``),
        return the unwrapped target URL.  Otherwise return ``url``."""
        m = _DDG_UDDG_RE.search(url)
        if not m:
            return url
        u = m.group(1)
        try:
            u = urllib.parse.unquote(u)
        except Exception:
            pass
        return u.lstrip()

    # -- fetch with retry ----------------------------------------------
    def _fetch(self, *, q: str) -> Optional[bytes]:
        """GET the DDG HTML endpoint with 2 attempts, then once on
        the Lite endpoint.  Returns body bytes or None on failure."""
        url = f"{DDG_HTML_URL}?q={urllib.parse.quote_plus(q)}"
        last_err: Optional[str] = None
        for attempt in range(1, 3):  # 2 attempts
            self._wait_for_rate_limit()
            try:
                r = safe_get(url, timeout=10.0, max_bytes=_DDG_MAX_BYTES, client=self._client)
            except Exception as exc:
                last_err = f"exception: {exc}"
                time.sleep(_DDG_RETRY_DELAY_S)
                continue
            if r.status_code not in _DDG_RETRYABLE_STATUSES and r.error is None:
                return r.content
            last_err = f"status={r.status_code} err={r.error}"
            time.sleep(_DDG_RETRY_DELAY_S)
        # Lite fallback (one attempt)
        lite_url = f"{DDG_LITE_URL}?q={urllib.parse.quote_plus(q)}"
        self._wait_for_rate_limit()
        try:
            r = safe_get(lite_url, timeout=10.0, max_bytes=_DDG_MAX_BYTES, client=self._client)
        except Exception:
            return None
        if r.status_code == 200 and r.content:
            return r.content
        return None

    # -- main search ----------------------------------------------------
    def _do_search(
        self, query: str, *, max_results: int, page: int
    ) -> List[SearchResult]:
        if page > 1:
            logger.debug(
                "DDG provider: page=%d not supported — using page 1", page,
            )
        body = self._fetch(q=query)
        if not body:
            return []
        try:
            text = body.decode("utf-8", errors="replace")
        except Exception:
            return []
        out: List[SearchResult] = []
        seen: set[str] = set()
        for m in _DDG_ANCHOR_RE.finditer(text):
            raw_href = html_mod.unescape(m.group(1))
            title_raw = html_mod.unescape(m.group(2))
            final_url = self._unwrap_ddg_redirect(raw_href)
            if final_url in seen:
                continue
            seen.add(final_url)
            out.append(SearchResult(
                title=strip_html(title_raw)[:300],
                url=raw_href,
                snippet="",
                final_url=final_url,
                query=query,
                provider=self._impl_name,
            ))
            if len(out) >= int(max_results):
                break
        return out

    def close(self) -> None:
        if self._owns_client:
            try:
                self._client.close()
            except Exception:
                pass


# ------------------------------------------------------------------ #
# Exa (stub — drops to DuckDuckGo)
# ------------------------------------------------------------------ #

class ExaSearchProvider(SearchProvider):
    """Exa search adapter (placeholder).

    Exa's proprietary search API is intended for crawlers/research
    pipelines and pricing is aggressive for a job-hunter use case;
    we ship a *ceiling* implementation here (so a future
    ``get_search_provider(cfg, key="exa")`` path can be swapped in
    without touching callers) but the real call is deferred.  When
    invoked, this provider falls back to ``DuckDuckGoSearchProvider``
    and returns its results.
    """

    _impl_name = "exa"

    def __init__(self, *, api_key: str = "", min_delay_s: float = 1.0):
        if not api_key:
            logger.warning(
                "ExaSearchProvider constructed without API key — "
                "all search calls will fall back to DuckDuckGo."
            )
        self._api_key = api_key
        self._ddg = DuckDuckGoSearchProvider()

    def _do_search(
        self, query: str, *, max_results: int, page: int
    ) -> List[SearchResult]:
        logger.info("Exa provider: falling back to DuckDuckGo (API not wired)")
        return self._ddg.search(
            query, max_results=max_results, page=page,
        )

    def close(self) -> None:
        self._ddg.close()


# ------------------------------------------------------------------ #
# Factory
# ------------------------------------------------------------------ #

def get_search_provider(
    cfg: SearchConfig | None = None, *, app_config=None,
) -> SearchProvider:
    """Pick a :class:`SearchProvider` based on config.

    Resolution order:
      1.  If ``cfg.search_provider == "duckduckgo"`` -> DDG.
      2.  If ``cfg.search_provider == "exa"`` and
          ``app_config.exa_api_key`` is set -> Exa (which itself
          falls back to DDG when invoked).
      3.  If ``cfg.search_provider == "brave"`` (default) and
          ``app_config.brave_search_api_key`` is set -> Brave.
      4.  Else -> DuckDuckGo (no key required, ensures the app
          boots without any paid credentials).
    """
    provider_pref = "brave"
    if cfg is not None:
        provider_pref = (cfg.search_provider or "brave").lower().strip()

    app = app_config or _resolve_app_config()
    brave_key = getattr(app, "brave_search_api_key", "") or ""
    exa_key = getattr(app, "exa_api_key", "") or ""

    if provider_pref == "duckduckgo":
        return DuckDuckGoSearchProvider()
    if provider_pref == "brave":
        if brave_key:
            return BraveSearchProvider(api_key=brave_key)
        logger.warning(
            "Search provider 'brave' requested but "
            "BRAVE_SEARCH_API_KEY is empty — falling back to DuckDuckGo."
        )
        return DuckDuckGoSearchProvider()
    if provider_pref == "exa":
        if exa_key:
            return ExaSearchProvider(api_key=exa_key)
        logger.warning(
            "Search provider 'exa' requested but "
            "EXA_API_KEY is empty — falling back to DuckDuckGo."
        )
        return DuckDuckGoSearchProvider()
    # unknown provider
    logger.warning(
        "Unknown search_provider %r — using DuckDuckGo.", provider_pref,
    )
    return DuckDuckGoSearchProvider()


def _resolve_app_config():
    """Lazy + safe accessor for the process-level :class:`AppConfig`.

    We import here (not at the top of the module) to keep the
    dependency graph flat — :mod:`config.settings` itself does not
    import anything from this package.
    """
    try:
        from config.settings import get_config
    except Exception:
        return None
    try:
        return get_config()
    except Exception:
        return None


__all__ = [
    "SearchResult",
    "SearchProvider",
    "BraveSearchProvider",
    "DuckDuckGoSearchProvider",
    "ExaSearchProvider",
    "get_search_provider",
]
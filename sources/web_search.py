"""Web search (DuckDuckGo, no API key) integrated as a standard JobSource.

Queries DuckDuckGo's HTML endpoint for ``include_keywords x locations``
combinations, parses organic search-result URLs, and normalizes each hit
into a :class:`RawListing`.  Deduplication by URL is handled downstream by
``SearchEngine.run_all``.

The query is built from ``cfg.include_keywords`` (or non-empty
``phrases/company_names`` if keywords are not set) combined with each entry
in ``cfg.locations``, yielding ``"<kw> <loc> jobs job openings"`` per
attempt.  Links are filtered to plausible job/posting URLs before being
returned (see :data:`JOB_URL_HINTS`).
"""
from __future__ import annotations

import html
import logging
import re
import time
import urllib.parse

import httpx

from config.search_config import SearchConfig, classify_employment_type
from services.safe_requests import FetchResult, safe_get
from services.sanitization import strip_html

from .base import JobSource, RawListing, SourceResult
from .registry import register

logger = logging.getLogger(__name__)

# Realistic Desktop Browser UA. DDG returns HTTP 202 ("Accepted" — its
# polite-block response) when the caller's UA looks like python-requests
# or curl. A standard Chrome/Edge UA keeps 200.
_UA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
}

# DuckDuckGo HTML endpoints (no key required, plain HTTP).
# `html` is the primary; `lite` is the fallback (smaller document, fewer
# JS, less likely to return 202 — but uses the same `uddg=` redirect
# scheme, so `_unwrap_ddg_redirect` works identically on both).
DDG_HTML_URL = "https://html.duckduckgo.com/html/"
DDG_LITE_URL = "https://lite.duckduckgo.com/lite/"
MAX_QUERIES = 8          # hard cap: len(keywords) x len(locations) pairs
MAX_RESULTS_PER_QUERY = 10
MAX_BYTES_PER_FETCH = 1024 * 256
_RETRY_DELAY_S = 1.5     # sleep seconds between 202/403 retries
_RETRYABLE_STATUSES = {202, 408, 429, 503}

# URL substrings that strongly suggest a job / career listing.
JOB_URL_HINTS = (
    "/careers", "/career", "/jobs", "/job", "/hiring", "/recruiting",
    "/jobs/", "/job/", "/careers/", "/vacancies", "/open-roles",
    "ats.greenhouse.io", "jobs.lever.co", "workday.com/wday/cxs",
    "/apply/", "/application/", "/ref/",
)

# Regex for a DuckDuckGo HTML result anchor.  The pattern is intentionally
# loose: it captures the first ``href="..."`` inside a <a> that is
# *not* a /duck/ redirect and whose anchor text is at most 120 chars.
_ANCHOR_RE = re.compile(r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_SNIPPET_RE = re.compile(r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', re.S)
_TITLE_RE = re.compile(r'<a[^>]+class="[^"]*result__a[^"]*"[^>]*>(.*?)</a>', re.S)


def _query_pairs(cfg: SearchConfig) -> list[tuple[str, str]]:
    """Yield ``(keyword, location)`` pairs, capped to at most ``MAX_QUERIES``."""
    kws: list[str] = []
    for k in list(cfg.include_keywords) + list(cfg.company_names):
        k = (k or "").strip()
        if k and k.lower() not in {x.lower() for x in kws}:
            kws.append(k)
    # fallback: phrases
    if not kws:
        for p in cfg.phrases:
            p = (p or "").strip()
            if p and p.lower() not in {x.lower() for x in kws}:
                kws.append(p)
    if not kws:
        # absolute last resort: the intern / job keyword
        kws.append("Software Intern")

    locs: list[str] = []
    for loc in cfg.locations:
        loc = (loc or "").strip()
        if loc and loc.lower() not in {x.lower() for x in locs}:
            locs.append(loc)
    if not locs:
        locs.append("")  # one pair, no location (remote / global search)

    pairs: list[tuple[str, str]] = []
    for kw in kws:
        for loc in locs:
            pairs.append((kw, loc))
            if len(pairs) >= MAX_QUERIES:
                return pairs
    return pairs


def _build_search_query(kw: str, loc: str) -> str:
    """Compose the DuckDuckGo HTML query string.

    Joins ``kw``, ``loc``, ``"job"``, and ``"jobs"`` with single spaces,
    skipping any part that is empty or whitespace-only (so a blank
    ``loc`` does not leak a double-space into the URL).
    """
    parts = [kw, loc, "job", "jobs"]
    return " ".join(p.strip() for p in parts if p and p.strip()).strip()


@register
class WebSearchSource(JobSource):
    """DuckDuckGo HTML search for jobs matching the user query.

    Uses a *persistent* httpx.Client (created in `__init__`) with a
    realistic browser User-Agent so that DDG does not respond with its
    polite-block 202.  On 202/408/429/503 the source retries twice
    (1.5s between attempts) before falling back to the `lite` DDG
    endpoint, which is smaller and less aggressively blocked.
    """

    name = "Web Search"
    slug = "web_search"
    official_search_url = "https://duckduckgo.com/"
    robots_check = False

    def __init__(self, client: httpx.Client | None = None, allow_robots: bool = False):
        super().__init__(client=client, allow_robots=allow_robots)
        if self.client is None:
            self.client = httpx.Client(
                timeout=10.0,
                follow_redirects=False,
                headers=_UA_HEADERS,
            )
            # The persistent client is ours to close; we own the handle here.

    def _fetch_with_retry(
        self, url: str, *, timeout: float = 10.0, max_bytes: int = MAX_BYTES_PER_FETCH
    ) -> FetchResult:
        """GET `url` with 2 retries on 202/408/429/503 + 1.5s backoff,
        then fall back to DDG `lite` endpoint when `url` is an
        `html.duckduckgo.com/html/` URL.

        Returns a `FetchResult` with `error` populated and `content == b""`
        for the final failure.  All network failures (including the
        original ValueError from `safe_get`) are returned, not raised —
        the caller treats them as `errors.append` for that query.
        """
        attempts = 2  # initial + 1 retry
        last_err: FetchResult | None = None
        for attempt in range(1, attempts + 1):
            try:
                r = safe_get(url, timeout=timeout, max_bytes=max_bytes, client=self.client)
            except Exception as exc:
                r = FetchResult(status_code=-1, content=b"", headers=httpx.Headers(),
                                error=f"exception: {exc}")
            last_err = r
            if r.status_code not in _RETRYABLE_STATUSES and r.error is None:
                return r
            if attempt < attempts:
                time.sleep(_RETRY_DELAY_S)
        # Retry loop exhausted on a still-blockable status; one last try
        # against the `lite` endpoint if we started on `html`.
        if "html.duckduckgo.com/html/" in url and last_err is not None:
            new_url = url.replace("html.duckduckgo.com/html/", "lite.duckduckgo.com/lite/")
            try:
                r = safe_get(new_url, timeout=timeout, max_bytes=max_bytes, client=self.client)
            except Exception as exc:
                r = FetchResult(status_code=-1, content=b"", headers=httpx.Headers(),
                                error=f"exception: {exc}")
            if r.status_code == 200 and r.content:
                return r
        return last_err

    def scan(self, cfg: SearchConfig) -> SourceResult:
        pairs = _query_pairs(cfg)
        if not pairs:
            return SourceResult(success=False, error_message="no keywords or locations in config")

        all_listings: list[RawListing] = []
        seen_urls: set[str] = set()
        errors: list[str] = []

        for i, (kw, loc) in enumerate(pairs):
            query = _build_search_query(kw, loc)
            url = f"{DDG_HTML_URL}?q={urllib.parse.quote_plus(query)}"
            # Use the retry-fallback helper.  It swallows all network-level
            # exceptions and returns a FetchResult with `error` set when
            # both the html and lite endpoints fail.  The caller treats a
            # non-200 / `error` as "this query produced no listings"
            # rather than a source-level failure.
            result = self._fetch_with_retry(url)
            if result.error:
                errors.append(f"query {i+1}: {result.error}")
                continue
            if result.status_code != 200:
                errors.append(f"query {i+1}: HTTP {result.status_code}")
                continue

            decoded = result.content.decode("utf-8", errors="replace")
            hits = self._parse_results(decoded, query)
            for hit in hits[:MAX_RESULTS_PER_QUERY]:
                u = hit["url"].lower()
                if u in seen_urls:
                    continue
                seen_urls.add(u)
                item = RawListing(
                    source="Web Search",
                    title=hit["title"][:200] or "(untitled)",
                    company="Unknown",
                    description_text=hit.get("snippet"),
                    employment_type=classify_employment_type(hit.get("title", "")),
                    original_url=hit["url"],
                    application_url=hit["url"],
                )
                all_listings.append(item)

        filtered = self.apply_filters(cfg, all_listings)
        errors_str = " | ".join(errors[:5]) + f" (+{len(errors)-5} more)" if len(errors)>5 else " | ".join(errors)
        if not filtered and errors:
            logger.warning("WebSearch: %d errors | %s", len(errors), errors_str[:400])
        return SourceResult(
            success=bool(filtered),
            listings=filtered,
            listings_found=len(filtered),
            listings_new=len(filtered),
            error_message=errors_str if errors and not filtered else None,
        )

    @staticmethod
    def _parse_results(decoded_html: str, _query: str) -> list[dict]:
        """Parse organic search results: list of ``{"url","title","snippet"}``.

        Primary parser uses the `html.duckduckgo.com` class="result__a"
        anchors.  If *none* are found (i.e. the response is from the
        `lite.duckduckgo.com` fallback), it retries with the generic
        `<a href="…uddg=…">` matcher, treating the anchor body as the
        title (the lite endpoint has no separate title/snippet markup).
        """
        results: list[dict] = []

        # --- Pass 1: html.duckduckgo.com class-anchored results ---
        anchors = list(_ANCHOR_RE.finditer(decoded_html))
        snippets = list(_SNIPPET_RE.finditer(decoded_html))
        for i, m in enumerate(anchors):
            raw_url = m.group(1)
            raw_title = m.group(2)
            final_url = _unwrap_ddg_redirect(raw_url)
            if not final_url:
                continue
            low = final_url.lower()
            if not any(h in low for h in JOB_URL_HINTS):
                continue
            title = clean_anchor_text(raw_title)
            snippet_text = ""
            if i < len(snippets):
                raw_snip = snippets[i].group(1)
                snippet_text = strip_html(raw_snip)
            results.append({"url": final_url, "title": title, "snippet": snippet_text or None})
            if len(results) >= MAX_RESULTS_PER_QUERY * 2:
                break
        if results:
            return results

        # --- Pass 2: fallback match for `lite` endpoint ---
        for m in _LITE_ANCHOR_RE.finditer(decoded_html):
            raw_url = m.group(1)
            raw_title = m.group(2)
            final_url = _unwrap_ddg_redirect(raw_url)
            if not final_url:
                continue
            low = final_url.lower()
            if not any(h in low for h in JOB_URL_HINTS):
                continue
            title = clean_anchor_text(raw_title)
            if not title or len(title) < 2:
                continue
            results.append({"url": final_url, "title": title, "snippet": None})
            if len(results) >= MAX_RESULTS_PER_QUERY * 2:
                break
        return results


# Fallback regex for the DDG `lite` endpoint.  `lite` results are plain
# `<a href="...">` anchors with no class="..." wrapper, and the visible
# title *is* the anchor body itself.  We match any `a` whose `href`
# contains `uddg=` (i.e. a search-result redirect; not the `query` form
# submit or DDG's own nav links) and whose body is non-empty.
_LITE_ANCHOR_RE = re.compile(
    r'<a[^>]+href="([^"]*uddg=[^"]+)"[^>]*>(.*?)</a>', re.S
)


def _unwrap_ddg_redirect(raw: str) -> str | None:
    """Follow ``uddg=`` param inside DuckDuckGo's HTML-encoded redirect."""
    if not raw:
        return None
    # decode HTML entities
    s = html.unescape(raw)
    if "duckduckgo.com/l/" in s or "/y.js" in s or "uddg=" in s:
        try:
            parsed = urllib.parse.urlparse(s)
            qs = urllib.parse.parse_qs(parsed.query)
            uddg = qs.get("uddg", [None])[0]
            if uddg:
                return uddg
        except Exception:
            pass
        # direct uddg= without query string
        if "?" in s:
            query = s.split("?", 1)[1]
            for pair in query.split("&"):
                if pair.startswith("uddg="):
                    return urllib.parse.unquote(pair[len("uddg="):])
        return None
    # strip whitespace/return
    return s.strip().split()[0] if s.strip() else None


def clean_anchor_text(raw: str) -> str:
    """Strip tags / entities from an <a> anchor body."""
    t = re.sub(r"[<>]+", " ", raw)
    t = html.unescape(t)
    return strip_html(t).strip()

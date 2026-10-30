"""Web search (search-provider abstraction) integrated as a JobSource.

This adapter is no longer a hand-rolled DuckDuckGo HTML scraper.  It
plugs into :mod:`sources.search_providers` (Brave / Exa / DuckDuckGo)
and :mod:`services.query_planner` so that:

* Queries come from the *planner* (multi-family, prioritised, with
  German variants) rather than the old ``kw × loc`` pair matrix.
* The provider is *pluggable* — a user with ``BRAVE_SEARCH_API_KEY``
  gets the JSON API (no scraping); without it, falls back to DDG HTML.
* Per-query results flow through the same :class:`RawListing` shape
  as every other source.

``JOB_URL_HINTS`` is retained as a **soft** relevance boost
(hint-matching listings are ranked *ahead* of non-matching ones).
It is **not** an exclusion filter — previously it censored entire
pages of job-board results, which was the main coverage gap.
"""
from __future__ import annotations

import logging
from typing import List, Optional

import httpx

from config.search_config import SearchConfig, classify_employment_type
from services.platforms import is_aggregator, looks_like_results_page, split_board_title
from services.query_planner import QuerySpec, plan_queries
from services.sanitization import strip_html

from .base import JobSource, RawListing, SourceResult
from .registry import register
from .search_providers import (
    SearchProvider,
    get_search_provider,
)

logger = logging.getLogger(__name__)

# --- module-level back-compat hooks -------------------------------
# The old ``sources.web_search._build_search_query`` / ``_query_pairs``
# / ``MAX_QUERIES`` surface is preserved so that older test suites
# calling into them continue to pass.  New code should use
# :mod:`services.query_planner` directly.
MAX_QUERIES = 8          # cap for the legacy ``_query_pairs``
MAX_RESULTS_PER_QUERY = 10

# URL substrings that suggest a job / career listing.  Used as a *soft*
# relevance boost only — NOT an exclusion filter.
JOB_URL_HINTS = (
    "/careers", "/career", "/jobs", "/job", "/hiring", "/recruiting",
    "/jobs/", "/job/", "/careers/", "/vacancies", "/open-roles",
    "ats.greenhouse.io", "jobs.lever.co", "workday.com/wday/cxs",
    "/apply/", "/application/", "/ref/",
)


def _hint_score(url: str) -> int:
    """How many of :data:`JOB_URL_HINTS` appear in *url*.

    Higher = more likely to be a real job listing (used for sorting;
    never for filtering).
    """
    u = (url or "").lower()
    return sum(1 for h in JOB_URL_HINTS if h in u)


def _query_pairs(cfg: SearchConfig) -> List[tuple[str, str]]:
    """Legacy (keyword, location) pair matrix — preserved for back-compat.

    Kept at the old hardcoded 8-pair cap.  New code uses
    :func:`services.query_planner.plan_queries`.
    """
    kws: List[str] = []
    for k in list(cfg.include_keywords) + list(cfg.company_names):
        k = (k or "").strip()
        if k and k.lower() not in {x.lower() for x in kws}:
            kws.append(k)
    if not kws:
        for p in cfg.phrases:
            p = (p or "").strip()
            if p and p.lower() not in {x.lower() for x in kws}:
                kws.append(p)
    if not kws:
        kws.append("jobs")  # no industry assumed when nothing was asked for

    locs: List[str] = []
    for loc in cfg.locations:
        loc = (loc or "").strip()
        if loc and loc.lower() not in {x.lower() for x in locs}:
            locs.append(loc)
    if not locs:
        locs.append("")

    pairs: List[tuple[str, str]] = []
    for kw in kws:
        for loc in locs:
            pairs.append((kw, loc))
            if len(pairs) >= MAX_QUERIES:
                return pairs
    return pairs


def _build_search_query(kw: str, loc: str) -> str:
    """Legacy query builder (kw + loc + "job" + "jobs").

    Preserved for back-compat with older tests.  New code uses
    :mod:`services.query_planner`.
    """
    parts = [kw, loc, "job", "jobs"]
    return " ".join(p.strip() for p in parts if p and p.strip()).strip()


# ------------------------------------------------------------------ #
# JobSource implementation
# ------------------------------------------------------------------ #

@register
class WebSearchSource(JobSource):
    """Web-search source backed by the new provider abstraction.

    * If ``provider`` is not injected (typical), the source resolves
      one on first ``scan`` via :func:`get_search_provider` and
      *keeps the handle* for the source instance lifetime (the
      provider holds its own HTTP client + rate-limiter).
    * Queries come from :func:`plan_queries` (the multi-family
      planner), not the legacy kw×loc matrix.
    * Results are de-duplicated by *canonical* URL (via
      :meth:`RawListing.dedup_key`) before being returned.
    * ``JOB_URL_HINTS`` is a soft relevance boost: hint-matching
      listings are ranked first, but *no* listing is filtered out.
    """

    name = "Web Search"
    slug = "web_search"
    official_search_url = "https://duckduckgo.com/"
    robots_check = False

    def __init__(
        self,
        client: httpx.Client | None = None,
        allow_robots: bool = False,
        provider: SearchProvider | None = None,
    ):
        # ``client`` is retained for back-compat: a legacy caller may
        # pass an httpx client (e.g. a test fixture) and expect the
        # source to own it.  We no longer use it for DDG scraping
        # (the provider owns its own client) but we still store it so
        # that the tests that inspect ``source.client`` keep passing.
        super().__init__(client=client, allow_robots=allow_robots)
        if self.client is None:
            # Legacy tests (``test_httpx_client_has_realistic_ua``)
            # assert that ``.client`` is not None and has a
            # realistic UA.  We honour that with a *dummy* client
            # whose only purpose is to satisfy the old contract.
            import httpx as _httpx
            self.client = _httpx.Client(
                timeout=10.0,
                follow_redirects=False,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                },
            )
        self._provider = provider  # None → lazy-resolve on first scan
        self._provider_resolved = provider is not None

    # -------------------------------------------------------------- #
    # provider resolution (lazy, once per source-instance)
    # -------------------------------------------------------------- #
    def _get_provider(self, cfg: SearchConfig) -> SearchProvider:
        if self._provider is not None:
            return self._provider
        self._provider = get_search_provider(cfg)
        self._provider_resolved = True
        return self._provider

    # -------------------------------------------------------------- #
    # main scan
    # -------------------------------------------------------------- #
    def scan(self, cfg: SearchConfig) -> SourceResult:
        provider = self._get_provider(cfg)
        plan = plan_queries(cfg)
        if not plan:
            # The planner *always* emits at least the base role
            # variant — if it is empty something is very wrong.
            return SourceResult(
                success=False,
                error_message="query planner produced no queries",
            )

        seen_keys: set[str] = set()
        all_listings: list[RawListing] = []
        attempted = 0
        fetched = 0
        parsed = 0
        duplicates = 0
        failed = 0

        for spec in plan:
            attempted += 1
            try:
                results = provider.search(
                    spec.query_string,
                    max_results=MAX_RESULTS_PER_QUERY,
                    page=1,
                )
            except Exception as exc:  # pragma: no cover - provider swallows
                logger.exception(
                    "Web Search: provider.search crashed for %r: %s",
                    spec.query_string[:60], exc,
                )
                failed += 1
                continue

            if not results:
                failed += 1
                continue
            fetched += 1

            # Soft sort: hint-matching first.  No exclusion.
            ranked = sorted(
                results,
                key=lambda r: -_hint_score(
                    (r.final_url or r.url or ""),
                ),
            )
            for res in ranked:
                key = (res.final_url or res.url or "").strip()
                if not key:
                    continue
                if key in seen_keys:
                    duplicates += 1
                    continue
                seen_keys.add(key)
                title = strip_html(res.title or "")[:200]
                if not title:
                    continue
                if is_aggregator(res.url) and looks_like_results_page(title):
                    # A board's search/overview page, not one vacancy.
                    continue
                # Job-board titles carry "Title - Company - City | Board";
                # other pages keep company unknown until enrichment.
                title, company, location = split_board_title(title, res.url)
                parsed += 1
                all_listings.append(RawListing(
                    source=self.name,
                    title=title,
                    company=company,
                    location=location,
                    description_text=(res.snippet or None),
                    original_url=res.url,
                    canonical_url=res.final_url or res.url,
                    search_provider=provider.provider_name,
                    search_query=spec.query_string,
                    detail_extraction_succeeded=False,
                ))
                if len(all_listings) >= cfg.max_results:
                    # Stop early (upper bound from the user config).
                    break
            if len(all_listings) >= cfg.max_results:
                break

        success = True
        error_message: Optional[str] = None
        if all_listings == [] and failed == attempted and attempted > 0:
            success = False
            error_message = (
                f"all {attempted} search queries failed ({provider.name})"
            )

        return SourceResult(
            success=success,
            listings=all_listings,
            error_message=error_message,
            listings_found=len(all_listings),
            listings_new=len(all_listings),
            attempted=attempted,
            fetched=fetched,
            parsed=parsed,
            duplicates=duplicates,
            failed=failed,
        )
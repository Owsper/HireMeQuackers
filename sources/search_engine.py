"""Orchestrates the multi-source scan: bounded parallel, per-source progress."""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from config.search_config import SearchConfig
from services.logger import get_logger
from services.query_planner import plan_queries
from sources.base import RawListing, SourceResult
from sources.registry import ensure_loaded, list_sources
from sources.search_providers import SearchProvider, get_search_provider

log = get_logger("jobhunter.sources")


@dataclass
class ScanSummary:
    total_discovered: int = 0
    new_jobs: int = 0
    duplicates: int = 0
    failures: List[Dict[str, Any]] = field(default_factory=list)
    per_source: Dict[str, SourceResult] = field(default_factory=dict)
    all_listings: List[RawListing] = field(default_factory=list)
    status: str = "running"  # done | error | partial
    # New: scan-level diagnostics surfaced in the scan-history UI.
    scan_started_at: float = 0.0
    scan_duration_seconds: float = 0.0
    queries_executed: int = 0
    # Per source {name: {attempted, fetched, parsed, duplicates, failed}}
    per_source_counts: Dict[str, Dict[str, int]] = field(default_factory=dict)
    # Per platform the listing lives on (Indeed, Ashby, Company site, ...):
    # {label: {found, unique, cross_source_duplicates}}
    per_platform: Dict[str, Dict[str, int]] = field(default_factory=dict)
    cross_source_duplicates: int = 0

    def detail(self) -> Dict[str, Any]:
        return {
            "total_discovered": self.total_discovered,
            "new_jobs": self.new_jobs,
            "duplicates": self.duplicates,
            "status": self.status,
            "failures": len(self.failures),
            "listings": len(self.all_listings),
            "scan_duration_seconds": round(self.scan_duration_seconds, 1),
            "queries_executed": self.queries_executed,
            "cross_source_duplicates": self.cross_source_duplicates,
            "per_platform": self.per_platform,
            "per_source": {
                name: sr.details()
                for name, sr in self.per_source.items()
            },
        }


def _platform_row(summary: ScanSummary, label: str) -> Dict[str, int]:
    return summary.per_platform.setdefault(
        label, {"found": 0, "unique": 0, "cross_source_duplicates": 0})


def _add_listing(summary: ScanSummary, vacancies: Dict[str, List[int]], item: RawListing) -> None:
    """Append ``item`` unless the same vacancy (title + company + city)
    already came from another source.  A direct employer/ATS listing
    replaces a job-board copy, so users are linked to the original."""
    from services.platforms import is_aggregator, platform_of, same_city, vacancy_key

    label = platform_of(item.original_url)
    row = _platform_row(summary, label)
    row["found"] += 1
    vkey = vacancy_key(item.title, item.company)
    if vkey:
        for idx in vacancies.get(vkey, []):
            existing = summary.all_listings[idx]
            if (item.source == existing.source and item.external_job_id and existing.external_job_id
                    and item.external_job_id != existing.external_job_id):
                continue  # two distinct ATS requisitions can have the same title and location
            if not same_city(existing.location, item.location):
                continue
            summary.cross_source_duplicates += 1
            row["cross_source_duplicates"] += 1
            if is_aggregator(existing.original_url) and not is_aggregator(item.original_url):
                old = _platform_row(summary, platform_of(existing.original_url))
                old["unique"] -= 1
                summary.all_listings[idx] = item
                row["unique"] += 1
                row["cross_source_duplicates"] -= 1
                old["cross_source_duplicates"] += 1
            return
        vacancies.setdefault(vkey, []).append(len(summary.all_listings))
    summary.all_listings.append(item)
    row["unique"] += 1


class SearchEngine:
    """Run enabled sources in parallel; respect ``max_results`` per source.

    The engine accepts an *optional* pre-computed query plan and a
    *search provider*.  When neither is supplied it resolves both
    from the user's :class:`SearchConfig` + :class:`AppConfig`
    defaults (same behaviour as a bare scan).  This lets callers
    (e.g. the UI's "scan now" button) share one provider instance
    across the entire scan (brings the rate-limit benefit) instead
    of letting each source spin up its own.
    """

    def __init__(self, *, max_workers: int = 5, max_sources: int = 20):
        self.max_workers = max(1, int(max_workers))
        self.max_sources = max(1, int(max_sources))

    def run_all(
        self,
        cfg: SearchConfig,
        *,
        on_progress: Optional[Callable[[int, str], None]] = None,
        provider: Optional[SearchProvider] = None,
        profile: Any = None,
    ) -> ScanSummary:
        # Ensure all registered source adapters are loaded into REGISTRY
        # (fires the @register decorators on first scan from any caller).
        ensure_loaded()
        enabled = list_sources()
        # Filter by ``cfg.enabled_sources``
        if cfg.enabled_sources:
            enabled = [s for s in enabled if s.name in cfg.enabled_sources or s.slug in cfg.enabled_sources]
        # Cap number of sources
        enabled = enabled[: self.max_sources]
        if not enabled:
            log.warning("No sources enabled for scan.")
            return ScanSummary(status="error", failures=[
                {"source": "<none>", "error": "no enabled sources"}
            ])

        # -- lazy resolution of the search provider (shared) --------
        if provider is None:
            provider = get_search_provider(cfg)
        plan = plan_queries(cfg, profile=profile) if profile else plan_queries(cfg)

        summary = ScanSummary(
            total_discovered=0,
            status="running",
            scan_started_at=time.monotonic(),
            queries_executed=len(plan),
        )
        seen_urls: set[str] = set()
        vacancies: Dict[str, List[int]] = {}
        total = len(enabled)

        def _run_one(source):
            try:
                # Only the web-search source understands the query
                # plan and the provider.  For all other sources
                # (greenhouse, lever, rss, career_pages) we call
                # scan() the untouched way — the provider/plan
                # don't apply to them.
                if provider is not None and plan is not None:
                    if getattr(source, "slug", "") == "web_search":
                        # Build a source that knows about the provider
                        # + plan (WebSearchSource.__init__ accepts
                        # ``provider``; the plan is read from cfg's
                        # ``search_provider``).
                        from sources.web_search import WebSearchSource
                        if (isinstance(source, WebSearchSource)
                                and source._provider is None):
                            source._provider = provider
                            source._provider_resolved = True
                    elif getattr(source, "slug", "") not in (
                        "web_search",
                    ):
                        pass

                return source, source.scan(cfg)
            except Exception as exc:  # broadband
                log.warning("Source %s crashed: %s", source.name, exc)
                return source, SourceResult(success=False, error_message=str(exc))

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = [pool.submit(_run_one, s) for s in enabled]
            for i, fut in enumerate(as_completed(futures), start=1):
                try:
                    source, result = fut.result()
                except Exception as exc:
                    summary.failures.append({
                        "source": "unknown",
                        "error": str(exc),
                    })
                    log.exception("Uncaught source crash")
                    if on_progress:
                        on_progress(i, "error")
                    continue

                summary.per_source[source.name] = result
                summary.per_source_counts[source.name] = {
                    "attempted": result.attempted,
                    "fetched": result.fetched,
                    "parsed": result.parsed,
                    "duplicates": result.duplicates,
                    "failed": result.failed,
                }
                if result.success:
                    for item in result.listings:
                        key = item.dedup_key()
                        if key in seen_urls:
                            continue
                        seen_urls.add(key)
                        _add_listing(summary, vacancies, item)
                    summary.total_discovered += len(result.listings)
                else:
                    # Per-source failure must NOT discard the
                    # *other* sources' results.  Record it and
                    # continue.
                    summary.failures.append({
                        "source": source.name,
                        "error": result.error_message,
                    })
                if on_progress:
                    on_progress(i, "ok" if result.success else "partial")
                log.info(
                    "Source %s: found=%d success=%s (attempted=%d fetched=%d parsed=%d dup=%d failed=%d)",
                    source.name,
                    len(result.listings),
                    result.success,
                    result.attempted,
                    result.fetched,
                    result.parsed,
                    result.duplicates,
                    result.failed,
                )

        # Decide overall status
        if summary.per_source and all(r.success for r in summary.per_source.values()):
            summary.status = "done"
        elif summary.failures and not all(
            failure_msg for failure_msg in [f.get("error") for f in summary.failures]
        ):
            summary.status = "partial"
        else:
            summary.status = (
                "error" if not summary.total_discovered else "partial"
            )
        summary.scan_duration_seconds = time.monotonic() - summary.scan_started_at
        if on_progress:
            on_progress(total, "done")
        log.info("Scan finished: %s", summary.detail())
        return summary

"""Orchestrates the multi-source scan: bounded parallel, per-source progress."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from config.search_config import SearchConfig
from services.logger import get_logger
from sources.base import RawListing, SourceResult
from sources.registry import ensure_loaded, list_sources

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

    def detail(self) -> Dict[str, Any]:
        return {
            "total_discovered": self.total_discovered,
            "new_jobs": self.new_jobs,
            "duplicates": self.duplicates,
            "status": self.status,
            "failures": len(self.failures),
            "listings": len(self.all_listings),
        }


class SearchEngine:
    """Run enabled sources in parallel; respect `max_results` per source."""

    def __init__(self, *, max_workers: int = 4, max_sources: int = 8):
        self.max_workers = max(1, int(max_workers))
        self.max_sources = max(1, int(max_sources))

    def run_all(
        self,
        cfg: SearchConfig,
        *,
        on_progress: Optional[Callable[[int, str], None]] = None,
    ) -> ScanSummary:
        # Ensure all registered source adapters are loaded into REGISTRY
        # (fires the @register decorators on first scan from any caller).
        ensure_loaded()
        enabled = list_sources()
        # Filter by `cfg.enabled_sources`
        if cfg.enabled_sources:
            enabled = [s for s in enabled if s.name in cfg.enabled_sources or s.slug in cfg.enabled_sources]
        # Cap number of sources
        enabled = enabled[: self.max_sources]
        if not enabled:
            log.warning("No sources enabled for scan.")
            return ScanSummary(status="error", failures=[
                {"source": "<none>", "error": "no enabled sources"}
            ])

        summary = ScanSummary(
            total_discovered=0,
            status="running",
        )
        seen_urls = set()
        total = len(enabled)

        def _run_one(source):
            try:
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
                if result.success:
                    for item in result.listings:
                        if item.original_url in seen_urls:
                            continue
                        seen_urls.add(item.original_url)
                        summary.all_listings.append(item)
                    summary.total_discovered += len(result.listings)
                else:
                    summary.failures.append({
                        "source": source.name,
                        "error": result.error_message,
                    })
                if on_progress:
                    on_progress(i, "ok" if result.success else "partial")
                log.info(
                    "Source %s: found=%d success=%s",
                    source.name,
                    len(result.listings),
                    result.success,
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
        if on_progress:
            on_progress(total, "done")
        log.info("Scan finished: %s", summary.detail())
        return summary

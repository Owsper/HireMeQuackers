"""On-demand scheduler wrapper.

Default: does NOT auto-scan. Users MUST click [Scan for new jobs now]
which invokes `OnDemandScheduler.run_scan`.

An optional daily cron is stubbed out; it is disabled by default and not
wired into the CLI. For periodic scans, schedule `quackers scan` with cron
or Windows Task Scheduler instead.
"""
from __future__ import annotations

from typing import Callable, Optional

from config.search_config import SearchConfig
from services.discovery import DiscoverySummary, run_discovery


class OnDemandScheduler:
    """Single-run, human-triggered scan scheduler."""

    def __init__(self, scan_cap_sources: int = 8, cap_per_source_requests: int = 30):
        self.scan_cap_sources = max(1, int(scan_cap_sources))
        self.cap_per_source_requests = max(1, int(cap_per_source_requests))

    def run_scan(
        self,
        cfg: SearchConfig,
        *,
        on_progress: Optional[Callable[[int, str], None]] = None,
    ) -> DiscoverySummary:
        """Run the full scan; return a high-level summary.

        `evaluate_with_gemini=True` is the default; a missing API key or
        the deterministic fallback in `gemini_client` keeps the app
        runnable offline.
        """
        return run_discovery(
            cfg,
            on_progress=on_progress,
            evaluate_with_gemini=True,
        )

    def retry_failed(self, source_name: str) -> DiscoverySummary:
        """Run only the failed source; intended for [Retry failed source]."""
        from config.search_config import default_search_config

        # Should load the saved config then restrict to the failed source:
        # load persistent-search-config and only allow the requested source.
        try:
            from database.connection import get_engine, get_session
            from config.runtime_settings import load_search_config

            session = get_session(get_engine())
            data = load_search_config(session)
            cfg = SearchConfig(**data) if data else default_search_config()
            session.close()
        except Exception:
            cfg = default_search_config()
        cfg.enabled_sources = [source_name]
        return self.run_scan(cfg)

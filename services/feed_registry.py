"""Configuration registry for external RSS/Atom feeds.

Exposes a small, curated list of known, stable, public feed URLs
that are *outside* the user's own ``cfg.rss_urls``.  A category-based
filter in :func:`get_feed_urls` lets the ``RSSFeedSource`` adapter
decide which feed cluster to look at (remote, european, student,
industry, general) without hard-coding the URL list inside any adapter.

Only *high-confidence* URLs are included.  Do not invent URLs here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class FeedEntry:
    name: str
    url: str
    language: str = "en"
    region: str = "worldwide"
    category: str = "general"
    description: str = ""
    enabled_default: bool = True


FEED_REGISTRY: List[FeedEntry] = [
    FeedEntry(
        name="We Work Remotely (Remote Programming)",
        url="https://weworkremotely.com/categories/remote-programming-jobs.rss",
        language="en",
        region="worldwide",
        category="remote",
        description="WWR remote programming jobs feed",
        enabled_default=True,
    ),
    FeedEntry(
        name="RemoteOK (Remote Jobs)",
        url="https://remoteok.com/remote-jobs.rss",
        language="en",
        region="worldwide",
        category="remote",
        description="RemoteOK community remote jobs feed",
        enabled_default=True,
    ),
]


def get_default_feeds(*, only_enabled: bool = True) -> List[FeedEntry]:
    """All feeds that are enabled_default (or all if only_enabled is False)."""
    if only_enabled:
        return [f for f in FEED_REGISTRY if f.enabled_default]
    return list(FEED_REGISTRY)


def list_feeds() -> List[FeedEntry]:
    """All feeds (enabled or not) as FeedEntry dataclasses."""
    return list(FEED_REGISTRY)


def get_feed_urls(
    selected_categories: Optional[List[str]] = None,
) -> List[FeedEntry]:
    """FeedEntry list whose category appears in *selected_categories*.

    * ``selected_categories=None or []`` -> all enabled_default feeds.
    * Unknown category names are silently ignored.
    * Returns full FeedEntry objects (not just URL strings) so callers
      can also use ``.name`` in ``RawListing.source``.
    """
    if not selected_categories:
        return get_default_feeds()
    wanted = {
        c.strip().lower() for c in selected_categories if isinstance(c, str) and c.strip()
    }
    out: list[FeedEntry] = []
    for f in FEED_REGISTRY:
        if not f.enabled_default:
            continue
        if f.category.lower() not in wanted:
            continue
        out.append(f)
    return out


def all_category_values() -> List[str]:
    """Sorted, unique category values present (for the settings UI)."""
    return sorted({f.category for f in FEED_REGISTRY if f.enabled_default})


__all__ = [
    "FeedEntry",
    "FEED_REGISTRY",
    "get_default_feeds",
    "list_feeds",
    "get_feed_urls",
    "all_category_values",
]

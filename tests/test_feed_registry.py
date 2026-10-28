"""Tests for services.feed_registry — no I/O, pure data checks."""
from __future__ import annotations

from services.feed_registry import (
    all_category_values,
    get_default_feeds,
    get_feed_urls,
    list_feeds,
)


def test_default_feeds_returns_2():
    feeds = get_default_feeds()
    assert len(feeds) == 2
    urls = {f.url for f in feeds}
    assert "https://weworkremotely.com/categories/remote-programming-jobs.rss" in urls
    assert "https://remoteok.com/remote-jobs.rss" in urls


def test_get_feed_urls_remote_category():
    feeds = get_feed_urls(["remote"])
    assert len(feeds) == 2
    for f in feeds:
        assert f.category == "remote"


def test_get_feed_urls_bogus_category():
    feeds = get_feed_urls(["nonexistent"])
    assert feeds == []


def test_get_feed_urls_none_returns_all_enabled():
    feeds = get_feed_urls(None)
    assert len(feeds) == 2


def test_all_category_values():
    # Both bundled feeds are category="remote", so only "remote" appears.
    assert all_category_values() == ["remote"]


def test_list_feeds_returns_all():
    feeds = list_feeds()
    assert len(feeds) == 2
    assert all(f.name for f in feeds)

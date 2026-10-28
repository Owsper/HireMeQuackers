"""Source registry: central place to discover and instantiate adapters."""
from __future__ import annotations

import importlib
from typing import Dict, List

from config.search_config import SearchConfig
from .base import JobSource

REGISTRY: Dict[str, type[JobSource]] = {}


def register(source_cls: type[JobSource]) -> type[JobSource]:
    """Decorator: register a JobSource class.
    Reads `cls.name` as the key; subclass authors set a `name` class attribute.
    """
    if not hasattr(source_cls, "name"):
        raise TypeError(f"{source_cls.__name__} must define a `name` class attribute")
    name = source_cls.name
    if name in REGISTRY:
        raise ValueError(f"Duplicate source name: {name}")
    REGISTRY[name] = source_cls
    return source_cls


def list_sources() -> List[JobSource]:
    """Return instances of every registered source (not to mutate them)."""
    out: List[JobSource] = []
    for cls in REGISTRY.values():
        try:
            out.append(cls())
        except Exception:
            continue
    return out


def get(name: str) -> JobSource | None:
    cls = REGISTRY.get(name)
    if cls is None:
        return None
    return cls()


def enabled_sources(cfg: SearchConfig) -> List[JobSource]:
    """Filter `list_sources()` to those explicitly enabled in `cfg`.

    When `cfg.enabled_sources` is empty, return all. `SourceHealthRecord.is_enabled`
    is checked by callers (scheduler) to honor per-row toggles. This helper
    honors the `cfg.enabled_sources` filter only.
    """
    all_sources = list_sources()
    if not cfg.enabled_sources:
        return all_sources
    enabled_names = set(cfg.enabled_sources)
    return [s for s in all_sources if s.name in enabled_names or s.slug in enabled_names]


def _eager_load() -> None:
    # Import the concrete modules so their `register` decorators fire.
    # Sub-modules do not auto-import if they are not in REGISTRY yet.
    _imports = (
        "sources.greenhouse",
        "sources.lever",
        "sources.rss",
        "sources.remoteok",
        "sources.hackernews",
        "sources.eures",
        "sources.career_pages",
        "sources.web_search",
    )
    import sys

    for mod in _imports:
        if mod not in sys.modules:
            try:
                importlib.import_module(mod)
            except Exception:
                pass


def ensure_loaded() -> None:
    if not REGISTRY:
        _eager_load()


__all__ = [
    "REGISTRY",
    "enabled_sources",
    "ensure_loaded",
    "get",
    "list_sources",
    "register",
]

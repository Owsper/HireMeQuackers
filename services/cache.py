"""In-process LRU + on-disk cache for AI scores and document outputs."""
from __future__ import annotations

import json
import os
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Optional

from config.settings import PROJECT_ROOT

_CACHE_DIR = PROJECT_ROOT / "data" / "cache"
_LRU_MAX = 500
_LOCK = threading.RLock()
_MEM: "OrderedDict[str, str]" = OrderedDict()


def _get_cache_dir() -> Path:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        return _CACHE_DIR
    except Exception:
        return Path(getenv_temp())


def getenv_temp() -> str:
    import tempfile

    return tempfile.gettempdir() / "jobhunter-cache"


def _key_to_path(key: str) -> Optional[Path]:
    # map hex digest to a safe path (cache_keys are already short 64-char hex)
    if not key or not isinstance(key, str):
        return None
    safe = key.replace(":_", "_").replace("//", "/")
    sanitized = "".join(c for c in safe if c.isalnum() or c in "-_/.")
    if not sanitized:
        return None
    d = _get_cache_dir()
    p = d / (sanitized[:24] + ".json")
    return p


def cache_get(key: str) -> Optional[str]:
    if not key:
        return None
    with _LOCK:
        if key in _MEM:
            _MEM.move_to_end(key)
            return _MEM[key]
    p = _key_to_path(key)
    if p is None or not p.exists():
        return None
    try:
        text = p.read_text(encoding="utf-8")
    except Exception:
        return None
    with _LOCK:
        if len(_MEM) >= _LRU_MAX:
            _MEM.popitem(last=False)
        _MEM[key] = text
    return text


def cache_set(key: str, value: str) -> None:
    if not key or not value:
        return
    with _LOCK:
        if len(_MEM) >= _LRU_MAX:
            _MEM.popitem(last=False)
        _MEM[key] = value
    p = _key_to_path(key)
    if p is None:
        return
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(value, encoding="utf-8")
    except Exception:
        pass


def invalidate_profile_prefix(pref: str) -> None:
    """Remove all cache entries whose key starts with `pref` (best-effort)."""
    with _LOCK:
        for k in list(_MEM.keys()):
            if k.startswith(pref):
                _MEM.pop(k, None)
    d = _get_cache_dir()
    exists = d.exists() if hasattr(d, "exists") else False
    if exists:
        for f in d.rglob("*.json"):
            name = f.name
            if any(part in name for part in pref.replace("profile:", "").split(":")[:2]):
                try:
                    f.unlink()
                except Exception:
                    pass


def clear_all() -> None:
    with _LOCK:
        _MEM.clear()
    d = _get_cache_dir()
    if d.exists():
        for f in d.rglob("*.json"):
            try:
                f.unlink()
            except Exception:
                pass

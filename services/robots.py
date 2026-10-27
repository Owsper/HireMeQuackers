"""Best-effort robots.txt cache + check (default allow, best-effort fetch)."""
from __future__ import annotations

import re
import threading
import time
from typing import Dict, Tuple
from urllib.parse import urlparse

from services.safe_requests import safe_get

_ROBOTS_CACHE: Dict[str, Tuple[float, bool]] = {}
_LOCK = threading.Lock()
_TTL_SECONDS = 600


def is_allowed_by_robots(url: str, *, timeout: float = 5.0) -> bool:
    """Check `<scheme>://<host>/robots.txt` (best-effort). Allow if:

    - fetch fails (assume allowed; a DMCA on robots is not the point)
    - robots.txt missing (200 or not) without a `Disallow: /` rule
    - my path is allowed by a User-agent-specific `Allow:` rule
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return True
    host = (parsed.hostname or "").lower()
    if not host:
        return True

    now = time.monotonic()
    with _LOCK:
        entry = _ROBOTS_CACHE.get(host)
        if entry and entry[0] + _TTL_SECONDS > now:
            return entry[1]

    robots_url = f"{parsed.scheme}://{host}/robots.txt"
    try:
        result = safe_get(
            robots_url, timeout=min(timeout, 5.0), max_bytes=64 * 1024
        )
    except Exception:
        with _LOCK:
            _ROBOTS_CACHE[host] = (now, True)
        return True

    if result.status_code != 200:
        with _LOCK:
            _ROBOTS_CACHE[host] = (now, True)
        return True

    text = result.content.decode("utf-8", errors="ignore")
    allowed = _path_allowed(text, path=parsed.path or "/")
    with _LOCK:
        _ROBOTS_CACHE[host] = (now, allowed)
    return allowed


def _path_allowed(robots_text: str, path: str) -> bool:
    """Simple robots.txt walk: pick Universe / * block and longest-match rule.

    Returns False when a `Disallow` rule with specificity >= any `Allow`
    matches the target path.
    """
    if not robots_text.strip():
        return True

    parsed_clean = [ln for ln in robots_text.splitlines()]

    # Split blocks: User-agent line begins a block; Allow/Disallow belong to it.
    # We use the first `*` block (or the first block if no wildcard).
    cur_agent: str | None = None
    cur_rules: list[tuple[str, str]] = []
    items: list[tuple[str, list[tuple[str, str]]]] = []

    for ln in parsed_clean:
        raw = ln.split("#", 1)[0].strip()
        if not raw or ":" not in raw:
            continue
        field, value = raw.split(":", 1)
        field = field.strip().lower()
        value = value.strip()
        if field == "user-agent":
            if cur_agent is not None and cur_rules:
                items.append((cur_agent, cur_rules))
            cur_agent = value
            cur_rules = []
        elif field in ("allow", "disallow"):
            if cur_agent is None:
                cur_agent = "*"
            cur_rules.append((field, value))
    if cur_agent is not None and cur_rules:
        items.append((cur_agent, cur_rules))

    # Pick preferred block
    star_block = None
    for agent, rules in items:
        if agent == "*":
            star_block = rules
            break
    chosen = star_block
    if chosen is None and items:
        chosen = items[0][1]

    if not chosen:
        return True

    # Track longest Allow / Disallow that matches our path
    best_allow: int | None = None
    best_disallow: int | None = None
    for directive, pattern in chosen:
        if not pattern or not _pattern_matches(pattern, path):
            continue
        specificity = len(pattern.rstrip("*"))
        if directive == "allow" and (best_allow is None or specificity > best_allow):
            best_allow = specificity
        elif directive == "disallow" and (
            best_disallow is None or specificity > best_disallow
        ):
            best_disallow = specificity

    if best_allow is None and best_disallow is None:
        return True
    if best_allow is not None and best_disallow is not None:
        return best_allow >= best_disallow
    if best_allow is not None:
        return True
    if best_disallow is not None:
        return best_disallow == 0
    return True


def _pattern_matches(pattern: str, path: str) -> bool:
    if not pattern:
        return True
    if pattern == "/":
        return True
    p = path.split("?", 1)[0]
    pat = re.escape(pattern)
    pat = re.sub(r"\*", ".*", pat)
    if not pat.endswith("$"):
        pat = "(" + pat + "(.*)?)"
    try:
        return re.match(pat, p) is not None
    except Exception:
        return False


def clear_cache() -> None:
    with _LOCK:
        _ROBOTS_CACHE.clear()


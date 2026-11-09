"""Cached robots rules evaluated per URL, conservatively handling outages."""
from __future__ import annotations
import threading
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser
from services.safe_requests import safe_get

_ROBOTS_CACHE: dict = {}
_LOCK = threading.Lock()
_TTL_SECONDS = 600
_LAST_FETCH: dict[str, float] = {}
USER_AGENT = "HireMeQuackers"


def is_allowed_by_robots(url: str, *, timeout: float = 5.0, client=None) -> bool:
    p = urlparse(url)
    if p.scheme not in {"http", "https"} or not p.hostname:
        return False
    origin, now = f"{p.scheme}://{p.netloc}", time.monotonic()
    with _LOCK:
        entry = _ROBOTS_CACHE.get(origin)
    if entry is None or entry[0] + _TTL_SECONDS <= now:
        try:
            result = safe_get(origin + "/robots.txt", timeout=min(timeout, 5), max_bytes=65536, client=client)
            rules = result.content.decode("utf-8", errors="replace") if result.status_code == 200 else result.status_code in {404, 410}
        except Exception:
            rules = False
        entry = (now, rules)
        with _LOCK:
            _ROBOTS_CACHE[origin] = entry
    return entry[1] if isinstance(entry[1], bool) else _path_allowed(entry[1], url)


def _path_allowed(robots_text: str, path: str) -> bool:
    import re
    groups, agents, rules = [], [], []
    for line in robots_text.splitlines():
        line = line.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = [s.strip() for s in line.split(":", 1)]
        key = key.lower()
        if key == "user-agent":
            if rules:
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(value.lower())
        elif key in {"allow", "disallow"} and agents:
            rules.append((key, value))
    groups.append((agents, rules))
    specific = [(a, r) for a, r in groups if any(x != "*" and x in USER_AGENT.lower() for x in a)]
    selected = specific or [(a, r) for a, r in groups if "*" in a]
    p = urlparse(path)
    target = p.path + ("?" + p.query if p.query else "") if p.scheme else path
    matches = []
    for _, group_rules in selected:
        for directive, pattern in group_rules:
            if not pattern:
                continue
            regex = re.escape(pattern).replace(r"\*", ".*")
            regex = regex[:-2] + "$" if regex.endswith(r"\$") else regex
            if re.match(regex, target):
                matches.append((len(pattern.replace("*", "").rstrip("$")), directive == "allow"))
    return max(matches)[1] if matches else True


def wait_for_crawl_slot(url: str) -> None:
    """Honor published crawl delay/request rate as well as the app's domain minimum."""
    from config.settings import get_config
    p = urlparse(url)
    origin = f"{p.scheme}://{p.netloc}"
    interval = get_config().rate_limit_min_seconds
    entry = _ROBOTS_CACHE.get(origin)
    if entry and isinstance(entry[1], str):
        parser = RobotFileParser()
        parser.parse(entry[1].splitlines())
        interval = max(interval, parser.crawl_delay(USER_AGENT) or parser.crawl_delay("*") or 0)
        rate = parser.request_rate(USER_AGENT) or parser.request_rate("*")
        if rate and rate.requests:
            interval = max(interval, rate.seconds / rate.requests)
    with _LOCK:
        now = time.monotonic()
        slot = max(now, _LAST_FETCH.get(origin, 0) + interval)
        _LAST_FETCH[origin] = slot
    if slot > now:
        time.sleep(slot - now)


def _pattern_matches(pattern: str, path: str) -> bool:
    import fnmatch
    return fnmatch.fnmatch(path, pattern + "*")


def clear_cache() -> None:
    with _LOCK:
        _ROBOTS_CACHE.clear()
        _LAST_FETCH.clear()

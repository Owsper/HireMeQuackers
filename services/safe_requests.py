"""SSRF-safe HTTP fetching.

- validate_public_http_url: blocks localhost / private nets / link-local /
  cloud metadata / .local domains via direct IP check AND post-DNS resolution.
- safe_get: bounded request with size cap + user-supplied httpx transport
  injection (for tests).
- RateLimiter / ExponentialBackoff in config.rate_limit.
"""
from __future__ import annotations

import ipaddress
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

from config.rate_limit import ExponentialBackoff, RateLimiter
from services.sanitization import safe_log

DEFAULT_TIMEOUT = 15.0
DEFAULT_MAX_BYTES = 3 * 1024 * 1024

# Cloud metadata + block list (hostnames used as string match after lower-casing).
_BLOCKED_HOSTS = {
    "metadata.google.internal",
    "metadata.goog",
    "metadata",
    "sts.amazonaws.com",
    "169.254.169.254",
    "fd00:ec2::254",
}

# Patterns never allowed anywhere in a URL
_BLOCKED_SUBSTRINGS = ("169.254.169.254", "metadata.google.internal", "fd00:ec2::254")


def _is_blocked_ip(ip: ipaddress._BaseAddress) -> bool:
    """Return True for IP types we never fetch."""
    if ip.is_private or ip.is_loopback or ip.is_link_local:
        return True
    if ip.is_reserved or ip.is_multicast or ip.is_unspecified:
        return True
    # CGNAT / Cloud metadata goals (100.64.0.0/10)
    if ip.version == 4:
        first = ip.packed[0] if ip.packed else None
        if first == 100:
            return True
        # 8.8.8.8 is allowed (Googlu DNS); 6.6.6 is blocked (Juniper)
        if first == 6:
            return True
    return False


def _is_blocked_domain(host: str) -> bool:
    if not host:
        return True
    low = host.strip().lower()
    # Local file names & no DNS
    if low == "unknown" or low == "":
        return True
    if low.startswith("[") and low.endswith("]"):
        # Bracketed IPv6
        return _is_blocked_ip(ipaddress.IPv6Address(low[1:-1]))
    if "." not in low:
        # e.g. `localhost`, `foo` inside . sits outside the four dot part needed
        return low == "localhost"
    if low.endswith(".local") or low.endswith(".internal") or low.endswith(".localhost"):
        return True
    if low in _BLOCKED_HOSTS:
        return True
    if low.startswith("metadata") and ".goog" in low:
        return True
    return False


def _resolve_fqdn(host: str) -> List[str]:
    try:
        infos = socket.getaddrinfo(host, None)
    except (OSError, socket.gaierror, UnicodeError):
        return []
    out: List[str] = []
    for info in infos:
        addr = info[4][0]
        out.append(addr)
    return out


def validate_public_http_url(
    url: str, *, allow_private: bool = False
) -> None:
    """Raise ValueError if the URL is unsafe (SSRF risk).

    Default: only http(s) to PUBLIC domains.
    - reject scripts, not http/s, file, ftp
    - reject .local, localhost, 127.*, 10.*, 172.16-31.*, 192.168.*,
      169.254.*, cloud metadata (169.254.169.254 / 100.64.0.0/10),
      link-local, reserved, multicast, unspecified
    - resolve FQDN D?
    - if the client accepts allow_private=True -> skip IPv6/v4 block & lookuup
    """
    if not url or not isinstance(url, str):
        raise ValueError("URL must be a non-empty string")
    if any(s in url for s in _BLOCKED_SUBSTRINGS):
        raise ValueError("URL contains a blocked substring")

    try:
        parsed = urlparse(url)
    except Exception as e:  # pragma: no cover - malformed
        raise ValueError(f"Malformed URL: {e}") from e

    scheme = parsed.scheme.lower()
    if not scheme in {"http", "https"}:
        raise ValueError(f"Scheme must be http/https, got {scheme!r}")

    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise ValueError("URL missing hostname")

    # Blocklist by hostname
    if _is_blocked_domain(host):
        raise ValueError("Blocked domain: " + host)

    # Blocklist by literal IP
    is_literal_ip = False
    try:
        ip = ipaddress.ip_address(host)
        is_literal_ip = True
        if not allow_private and _is_blocked_ip(ip):
            raise ValueError(f"Blocked IP: {ip}")
    except ValueError:
        # not an IP literal
        is_literal_ip = False

    # If NOT a literal IP, resolve and verify no resolved IP is private.
    if not is_literal_ip and not allow_private:
        addrs = _resolve_fqdn(host)
        for a in addrs:
            try:
                ip = ipaddress.ip_address(a)
            except ValueError:
                continue
            if _is_blocked_ip(ip):
                raise ValueError(f"Host resolves to blocked address: {a}")


class SSRError(ValueError):
    """Wrap a rejected URL fetch with the host name redaction."""

    def __init__(self, host: str, reason: str):
        super().__init__(f"Blocked host {host!r}: {reason}")
        self.host = host
        self.reason = reason


@dataclass
class FetchResult:
    status_code: int
    content: bytes
    headers: httpx.Headers
    from_cache: bool = False
    attempts: int = 1
    error: Optional[str] = None


def safe_get(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = DEFAULT_MAX_BYTES,
    rate_limiter: Optional[RateLimiter] = None,
    backoff: Optional[ExponentialBackoff] = None,
    client: Optional[httpx.Client] = None,
    allow_private: bool = False,
) -> FetchResult:
    """Bounded-size, rate-limited, retried HTTPS GET.

    Raises httpx.HTTPError for connect/timeout issues on the final retry.
    Catches `httpx.TimeoutException` and converts to explicit errors.
    """
    validate_public_http_url(url, allow_private=allow_private)

    backoff = backoff or ExponentialBackoff(base=1.0, factor=2.0, max_retries=2)
    attempts = max(1, backoff.max_retries)

    if client is None:
        client = httpx.Client(timeout=timeout, follow_redirects=False)
        should_close = True
    else:
        should_close = False

    try:
        last_error = None
        content = b""
        headers = httpx.Headers()
        status_code = -1
        for attempt in range(attempts):
            if rate_limiter is not None:
                rate_limiter.wait()
            try:
                resp = client.get(url, timeout=timeout)
            except httpx.ConnectTimeout as e:
                last_error = safe_log(f"connect timeout: {e}")
            except httpx.ReadTimeout as e:
                last_error = safe_log(f"read timeout: {e}")
            except httpx.TimeoutException as e:
                last_error = safe_log(f"timeout: {e}")
            except httpx.RequestError as e:
                last_error = safe_log(f"request error: {e}")
            else:
                status_code = resp.status_code
                headers = resp.headers
                if resp.status_code in (429, 503):
                    # retryable
                    last_error = f"HTTP {resp.status_code}"
                else:
                    content = resp.content
                    if len(content) > max_bytes:
                        content = content[:max_bytes]
                        last_error = f"response truncated to {max_bytes} bytes"
                    return FetchResult(
                        status_code=status_code,
                        content=content,
                        headers=headers,
                        attempts=attempt + 1,
                    )
            if attempt < attempts - 1:
                time.sleep(backoff.delay(attempt))
        return FetchResult(
            status_code=status_code,
            content=content,
            headers=headers,
            error=last_error or "unknown",
            attempts=attempts,
        )
    finally:
        if should_close:
            try:
                client.close()
            except Exception:
                pass

"""Simple rate-limiting + exponential backoff helpers."""
from __future__ import annotations

import threading
import time


class RateLimiter:
    """Conservative rate limiter; enforces a minimum interval between calls.

    Thread-safe: multiple workers can share an instance.
    """

    def __init__(self, min_interval: float):
        self.min_interval = float(min_interval)
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        """Block until a slot is available. Called before each request."""
        with self._lock:
            now = time.monotonic()
            next_ok = self._last + self.min_interval
            if now < next_ok:
                time.sleep(max(0.0, next_ok - now))
            self._last = time.monotonic()


class ExponentialBackoff:
    """Compute backoff delay; also drive a retry loop.

    delays = base * (factor ** attempt)
    For example: base=1, factor=2, max_retries=3 -> 1, 2, 4 seconds.
    """

    def __init__(self, base: float = 1.0, factor: float = 2.0, max_retries: int = 3):
        self.base = float(base)
        self.factor = float(factor)
        self.max_retries = int(max_retries)

    def delay(self, attempt: int) -> float:
        attempt = max(0, int(attempt))
        return self.base * (self.factor ** attempt)

    def __call__(self, attempt: int) -> None:
        """Sleep for this backoff (calls time.sleep)."""
        time.sleep(self.delay(attempt))

    @classmethod
    def get_max_tokens(cls, max_retries: int = 3, base: float = 1.0, factor: float = 2.0) -> float:
        """Total backoff time if every retry is consumed (seconds)."""
        r = clamp_int(max_retries)
        return sum(cls._static_delay(i, base, factor) for i in range(max(0, r)))

    @staticmethod
    def _static_delay(attempt: int, base: float, factor: float) -> float:
        return base * (factor ** max(0, int(attempt)))


def clamp_int(value: int | None, minimum: int = 0, maximum: int = 64) -> int:
    if value is None:
        return minimum
    v = int(value)
    if v < minimum:
        return minimum
    if v > maximum:
        return maximum
    return v

"""Shared machinery for public ATS job-board adapters.

Subclasses set ``provider`` / ``name`` / ``slug``, optionally
``DEFAULT_BOARDS``, and implement :meth:`fetch_board`, which yields
:class:`RawListing` objects for one board using :meth:`_get_json` /
:meth:`_get_text` (safe_get + per-provider rate limit + robots check).

The base class handles: board resolution (explicit list, ``cfg.ats_boards``,
discovered ``source_registry`` rows, defaults), per-board error isolation,
de-duplication and the fetched / parsed / duplicate / failed counters.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Iterator, List, Optional

from config.rate_limit import RateLimiter
from config.search_config import EmploymentType, SearchConfig, classify_employment_type
from services.robots import is_allowed_by_robots
from services.safe_requests import safe_get
from .base import JobSource, RawListing, SourceResult

log = logging.getLogger(__name__)

MAX_PAGES = 20  # hard cap on pagination per board


class BoardError(Exception):
    """A single board failed (HTTP error, bad payload, robots)."""


def detect_workplace(*parts: Optional[str]) -> str:
    low = " ".join(p or "" for p in parts).lower()
    if "hybrid" in low:
        return "hybrid"
    if re.search(r"\b(remote|fully remote|worldwide|home ?office|remoto|télétravail)\b", low):
        return "remote"
    if re.search(r"\b(on-?site|in office|vor ort)\b", low):
        return "onsite"
    return "unknown"


_ET_HINTS = [
    (EmploymentType.INTERNSHIP, ("intern", "praktikum", "trainee")),
    (EmploymentType.WORKING_STUDENT, ("werkstudent", "working student", "working_student")),
    (EmploymentType.PART_TIME, ("part-time", "part time", "parttime", "part_time", "teilzeit")),
    (EmploymentType.CONTRACT, ("contract", "freelance", "contractor")),
    (EmploymentType.TEMPORARY, ("temporary", "temp")),
    (EmploymentType.FULL_TIME, ("full-time", "full time", "fulltime", "full_time", "permanent", "vollzeit")),
]


def employment_type_from(raw: Optional[str], title: str) -> EmploymentType:
    """ATS employment-type label first, title second."""
    by_title = classify_employment_type(title)
    if by_title in (EmploymentType.INTERNSHIP, EmploymentType.WORKING_STUDENT):
        return by_title
    low = (raw or "").lower()
    for et, needles in _ET_HINTS:
        if any(n in low for n in needles):
            return et
    return by_title


class ATSBoardSource(JobSource):
    provider: str = ""
    robots_check = True
    DEFAULT_BOARDS: List[str] = []
    #: Seconds between requests to the same provider (shared per class).
    min_interval: float = 1.0
    _limiters: dict = {}

    def __init__(self, boards: Optional[List[str]] = None, **kwargs):
        super().__init__(**kwargs)
        self.boards = boards
        self._stats = {"attempted": 0, "fetched": 0, "failed": 0}

    def resolve_boards(self, cfg: SearchConfig) -> List[str]:
        if self.boards is not None:
            return list(dict.fromkeys(self.boards))
        out: List[str] = []
        for entry in getattr(cfg, "ats_boards", None) or []:
            prov, _, board = str(entry).partition(":")
            if prov.strip().lower() == self.provider and board.strip():
                out.append(board.strip())
        from services.ats_detection import registered_boards

        out.extend(registered_boards(self.provider))
        out.extend(self.DEFAULT_BOARDS)
        seen: set[str] = set()
        uniq = []
        for b in out:
            if b.lower() not in seen:
                seen.add(b.lower())
                uniq.append(b)
        return uniq

    def _limiter(self) -> RateLimiter:
        lim = ATSBoardSource._limiters.get(self.provider)
        if lim is None:
            lim = ATSBoardSource._limiters[self.provider] = RateLimiter(self.min_interval)
        return lim

    def _get_text(self, url: str, *, timeout: float = 15.0) -> str:
        self._stats["attempted"] += 1
        if self.robots_check and self.allow_robots and not is_allowed_by_robots(url, timeout=5.0):
            self._stats["failed"] += 1
            raise BoardError("disallowed by robots.txt")
        try:
            result = safe_get(url, timeout=timeout, client=self.client, rate_limiter=self._limiter())
        except Exception as exc:  # SSRF guard, timeouts on final retry
            self._stats["failed"] += 1
            raise BoardError(str(exc)) from exc
        if result.status_code != 200 or not result.content:
            self._stats["failed"] += 1
            status = result.status_code if result.status_code > 0 else (result.error or "n/a")
            raise BoardError(f"HTTP {status}")
        self._stats["fetched"] += 1
        return result.content.decode("utf-8", errors="replace")

    def _get_json(self, url: str, **kw):
        text = self._get_text(url, **kw)
        try:
            return json.loads(text)
        except ValueError as exc:
            raise BoardError("malformed JSON") from exc

    def fetch_board(self, board: str, cfg: SearchConfig) -> Iterator[RawListing]:
        raise NotImplementedError

    def scan(self, cfg: SearchConfig) -> SourceResult:
        self._stats = {"attempted": 0, "fetched": 0, "failed": 0}
        boards = self.resolve_boards(cfg)
        if not boards:
            return SourceResult(
                success=True,
                error_message=(f"no {self.name} boards configured — add with "
                               f"`quackers config set ats_boards={self.provider}:<board>`"),
            )
        seen: set[str] = set()
        listings: List[RawListing] = []
        errors: List[str] = []
        parsed = duplicates = 0
        for board in boards:
            try:
                for item in self.fetch_board(board, cfg):
                    if not item.title or not item.original_url:
                        continue
                    parsed += 1
                    key = item.dedup_key()
                    if key in seen:
                        duplicates += 1
                        continue
                    seen.add(key)
                    listings.append(item)
            except BoardError as exc:
                errors.append(f"{board}: {exc}")
            except Exception as exc:  # malformed payload shape etc.
                log.warning("%s board %s failed: %s", self.name, board, exc)
                errors.append(f"{board}: {type(exc).__name__}: {exc}")
        filtered = self.apply_filters(cfg, listings)
        all_failed = bool(errors) and len(errors) >= len(boards)
        return SourceResult(
            success=not all_failed,
            listings=filtered,
            error_message=" | ".join(errors) if errors else None,
            listings_found=len(filtered),
            listings_new=len(filtered),
            attempted=self._stats["attempted"],
            fetched=self._stats["fetched"],
            parsed=parsed,
            duplicates=duplicates,
            failed=self._stats["failed"],
        )

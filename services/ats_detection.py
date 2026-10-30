"""Detect the recruitment platform (ATS) behind a careers URL / page.

* :func:`detect_ats_from_url` — pure URL pattern matching, no network.
  Cheap enough to run on every search result of every scan.
* :func:`detect_ats_from_html` — looks inside a fetched careers page for
  embedded ATS widgets, links and JSON-LD URLs pointing at a known ATS.

Provider keys match the ``ats_type`` stored in ``source_registry`` and the
``ats_boards`` config entries ("ashby:linear", "workable:acme", ...).

Providers with a public, documented job-board API have an adapter in
``sources/``.  The rest (workday, icims, successfactors, jobvite, oracle)
are detected and registered only; career-page HTML scanning covers them.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Iterable, List, Optional
from urllib.parse import urljoin, urlparse

API_PROVIDERS = frozenset({
    "greenhouse", "lever", "ashby", "smartrecruiters", "recruitee",
    "personio", "workable",
})
HTML_ONLY_PROVIDERS = frozenset({
    "workday", "icims", "successfactors", "jobvite", "oracle",
})

# Path segments that are never a company slug.
_RESERVED_SLUGS = frozenset({
    "", "api", "embed", "jobs", "job", "careers", "v1", "v0", "widget",
    "accounts", "posting-api", "static", "assets", "search", "en", "de",
    "j", "o", "apply", "boards", "board", "www", "login", "privacy",
})


@dataclass(frozen=True)
class ATSMatch:
    provider: str
    board: str
    url: str

    @property
    def registry_key(self) -> str:
        """Unique per (provider, board); stored in ``source_registry.domain``."""
        return f"{self.provider}:{self.board.lower()}"

    @property
    def has_api(self) -> bool:
        return self.provider in API_PROVIDERS


def _first_segment(path: str) -> str:
    for seg in (path or "").split("/"):
        if seg.strip():
            return seg.strip()
    return ""


def _valid_slug(slug: str) -> bool:
    return bool(slug) and slug.lower() not in _RESERVED_SLUGS and bool(
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,99}", slug)
    )


def detect_ats_from_url(url: str) -> Optional[ATSMatch]:
    """Identify an ATS + board from a URL alone (no network)."""
    if not url:
        return None
    try:
        p = urlparse(url.strip())
    except ValueError:
        return None
    host = (p.hostname or "").lower()
    path = p.path or ""
    if not host:
        return None

    def match(provider: str, board: str) -> Optional[ATSMatch]:
        return ATSMatch(provider, board, url) if _valid_slug(board) else None

    if host == "jobs.ashbyhq.com":
        return match("ashby", _first_segment(path))
    if host == "api.ashbyhq.com":
        m = re.search(r"/job-board/([^/?#]+)", path)
        return match("ashby", m.group(1)) if m else None
    if host in {"boards.greenhouse.io", "job-boards.greenhouse.io", "job-boards.eu.greenhouse.io"}:
        return match("greenhouse", _first_segment(path))
    if host == "boards-api.greenhouse.io":
        m = re.search(r"/boards/([^/?#]+)", path)
        return match("greenhouse", m.group(1)) if m else None
    if host in {"jobs.lever.co", "jobs.eu.lever.co"}:
        return match("lever", _first_segment(path))
    if host in {"jobs.smartrecruiters.com", "careers.smartrecruiters.com"}:
        return match("smartrecruiters", _first_segment(path))
    if host == "api.smartrecruiters.com":
        m = re.search(r"/companies/([^/?#]+)", path)
        return match("smartrecruiters", m.group(1)) if m else None
    if host.endswith(".recruitee.com"):
        return match("recruitee", host[: -len(".recruitee.com")])
    m = re.fullmatch(r"([a-z0-9\-]+)\.jobs\.personio\.(?:de|com)", host)
    if m:
        return match("personio", m.group(1))
    if host == "apply.workable.com":
        return match("workable", _first_segment(path))
    if host == "www.workable.com":
        m = re.search(r"/api/accounts/([^/?#]+)", path)
        return match("workable", m.group(1)) if m else None
    if host.endswith(".workable.com"):
        return match("workable", host[: -len(".workable.com")])

    # --- detection-only providers ---
    m = re.fullmatch(r"([a-z0-9\-]+)\.wd\d+\.myworkdayjobs\.com", host)
    if m:
        site = _first_segment(re.sub(r"^/[a-z]{2}-[A-Z]{2}(?=/|$)", "", path))
        return ATSMatch("workday", f"{m.group(1)}/{site}" if site else m.group(1), url)
    m = re.fullmatch(r"(?:careers-)?([a-z0-9\-]+)\.icims\.com", host)
    if m and m.group(1) not in {"www", "api"}:
        return match("icims", m.group(1))
    if "successfactors" in host or host.endswith(".jobs2web.com"):
        q = re.search(r"company=([A-Za-z0-9_\-]+)", p.query or "")
        return match("successfactors", q.group(1) if q else host.split(".")[0])
    if host in {"jobs.jobvite.com", "careers.jobvite.com"}:
        return match("jobvite", _first_segment(path))
    m = re.fullmatch(r"([a-z0-9\-]+)\.fa\.[a-z0-9\-]+\.oraclecloud\.com", host)
    if m and ("candidateexperience" in path.lower() or "/hcmui/" in path.lower()):
        return match("oracle", m.group(1))
    return None


_EMBED_PATTERNS: List[tuple[str, re.Pattern]] = [
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.\-]+)")),
    ("ashby", re.compile(r"api\.ashbyhq\.com/posting-api/job-board/([A-Za-z0-9_.\-]+)")),
    ("greenhouse", re.compile(r"boards\.greenhouse\.io/embed/job_board(?:/js)?\?for=([A-Za-z0-9_\-]+)")),
    ("smartrecruiters", re.compile(r"api\.smartrecruiters\.com/v1/companies/([A-Za-z0-9_\-]+)")),
    ("recruitee", re.compile(r"\b([a-z0-9\-]+)\.recruitee\.com")),
    ("personio", re.compile(r"\b([a-z0-9\-]+)\.jobs\.personio\.(?:de|com)")),
    ("workable", re.compile(r"apply\.workable\.com/([A-Za-z0-9_\-]+)")),
]


def detect_ats_from_html(html: str, base_url: str = "") -> List[ATSMatch]:
    """Every distinct ATS board referenced from a careers page."""
    if not html:
        return []
    found: dict[str, ATSMatch] = {}

    def add(m: Optional[ATSMatch]) -> None:
        if m is not None and m.registry_key not in found:
            found[m.registry_key] = m

    for provider, pat in _EMBED_PATTERNS:
        for mm in pat.finditer(html):
            if _valid_slug(mm.group(1)):
                add(ATSMatch(provider, mm.group(1), base_url or mm.group(0)))
    for mm in re.finditer(r"""(?:href|src|action)\s*=\s*["']([^"']+)["']""", html, re.I):
        add(detect_ats_from_url(urljoin(base_url, mm.group(1)) if base_url else mm.group(1)))
    for block in re.findall(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", html, re.I | re.S):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        for u in _iter_urls(data):
            add(detect_ats_from_url(u))
    return list(found.values())


def _iter_urls(data) -> Iterable[str]:
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, str) and k.lower() in {"url", "sameas", "applyurl", "@id"}:
                yield v
            else:
                yield from _iter_urls(v)
    elif isinstance(data, list):
        for item in data:
            yield from _iter_urls(item)


def find_careers_link(html: str, base_url: str) -> Optional[str]:
    """Most likely careers-page URL linked from a company homepage."""
    best: Optional[tuple[int, str]] = None
    for mm in re.finditer(r"""<a[^>]+href\s*=\s*["']([^"'#]+)["'][^>]*>(.*?)</a>""", html or "", re.I | re.S):
        href = mm.group(1)
        text = re.sub(r"<[^>]+>", " ", mm.group(2)).strip().lower()
        score = 0
        if re.search(r"/(careers?|jobs|karriere|stellen(angebote)?|emplois?|empleo|vacanc(y|ies)|join-us)\b", href.lower()):
            score += 2
        if re.search(r"\b(careers?|jobs|karriere|stellenangebote|join us|work with us|carrières|empleo)\b|вакансии", text):
            score += 2
        if detect_ats_from_url(href):
            score += 3
        if score and (best is None or score > best[0]):
            best = (score, urljoin(base_url, href))
    return best[1] if best else None


def register_matches(matches: Iterable[ATSMatch], *, via: str = "web_search") -> int:
    """Persist new boards into ``source_registry``; skip known ones. Returns #new."""
    from database.connection import get_engine, get_session
    from database.models import SourceRegistryEntry

    matches = list(matches)
    if not matches:
        return 0
    session = get_session(get_engine())
    try:
        known = {r[0] for r in session.query(SourceRegistryEntry.domain).all()}
        new = 0
        for m in matches:
            if m.registry_key in known:
                continue
            known.add(m.registry_key)
            session.add(SourceRegistryEntry(
                domain=m.registry_key, ats_type=m.provider, board_slug=m.board,
                discovered_via=via, is_active=True,
            ))
            new += 1
        session.commit()
        return new
    finally:
        session.close()


def registered_boards(provider: str) -> List[str]:
    """Active board slugs for ``provider`` from ``source_registry`` (best-effort)."""
    try:
        from database.connection import get_engine, get_session
        from database.models import SourceRegistryEntry

        session = get_session(get_engine())
        try:
            rows = session.query(SourceRegistryEntry).filter_by(ats_type=provider, is_active=True).all()
            return [r.board_slug for r in rows if r.board_slug]
        finally:
            session.close()
    except Exception:
        return []

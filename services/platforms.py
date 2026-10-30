"""Major job boards: site-specific discovery, attribution and cross-source keys.

Coverage of the big boards (Indeed, LinkedIn, StepStone, ...) comes from
*publicly indexed* listing pages found through the configured web-search
provider (``site:`` queries) — HireMeQuackers never logs in to, scrapes
behind, or works around the access controls of these sites.  Search-engine
indexing is partial, so this is best-effort coverage, not a full mirror.
Boards with an official public feed get a real adapter instead
(Arbeitnow -> :class:`sources.ats_adapters.ArbeitnowSource`).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Optional, Tuple
from urllib.parse import urlparse


@dataclass(frozen=True)
class JobBoard:
    key: str
    label: str
    #: Value for the ``site:`` operator.
    site: str
    #: Host suffixes that identify the board.
    domains: Tuple[str, ...]
    #: Region focus: "de", "us" or "global" — used only for ordering.
    region: str = "global"
    #: Name of a registered adapter using an official API/feed, if any.
    api_source: Optional[str] = None


MAJOR_JOB_BOARDS: Tuple[JobBoard, ...] = (
    JobBoard("linkedin", "LinkedIn Jobs", "linkedin.com/jobs/view", ("linkedin.com",)),
    JobBoard("indeed", "Indeed", "indeed.com", ("indeed.com", "indeed.de", "indeed.co.uk")),
    JobBoard("stepstone", "StepStone", "stepstone.de", ("stepstone.de", "stepstone.at", "stepstone.com"), "de"),
    JobBoard("glassdoor", "Glassdoor", "glassdoor.com", ("glassdoor.com", "glassdoor.de", "glassdoor.co.uk")),
    JobBoard("wellfound", "Wellfound", "wellfound.com/jobs", ("wellfound.com", "angel.co")),
    JobBoard("jobware", "Jobware", "jobware.de", ("jobware.de",), "de"),
    JobBoard("monster", "Monster", "monster.com", ("monster.com", "monster.de")),
    JobBoard("ziprecruiter", "ZipRecruiter", "ziprecruiter.com", ("ziprecruiter.com",), "us"),
    JobBoard("arbeitnow", "Arbeitnow", "arbeitnow.com", ("arbeitnow.com",), "de", api_source="Arbeitnow"),
)

_ATS_LABELS = {
    "ashby": "Ashby", "greenhouse": "Greenhouse", "lever": "Lever",
    "smartrecruiters": "SmartRecruiters", "recruitee": "Recruitee",
    "personio": "Personio", "workable": "Workable", "workday": "Workday",
    "icims": "iCIMS", "successfactors": "SuccessFactors", "jobvite": "Jobvite",
    "oracle": "Oracle Cloud",
}


def _host(url: Optional[str]) -> str:
    try:
        host = (urlparse((url or "").strip()).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def board_for_url(url: Optional[str]) -> Optional[JobBoard]:
    host = _host(url)
    if not host:
        return None
    for board in MAJOR_JOB_BOARDS:
        for d in board.domains:
            if host == d or host.endswith("." + d):
                return board
    return None


def platform_of(url: Optional[str]) -> str:
    """Human label of the platform a listing URL lives on."""
    board = board_for_url(url)
    if board:
        return board.label
    from services.ats_detection import detect_ats_from_url

    match = detect_ats_from_url(url or "")
    if match:
        return _ATS_LABELS.get(match.provider, match.provider)
    return "Company site" if _host(url) else "Unknown"


def boards_for_scan(locations, *, include_api_boards: bool = False) -> list[JobBoard]:
    """Boards to query, local-region boards first; API-backed boards skipped."""
    from services.query_planner import _has_german_location

    german = _has_german_location(list(locations or []))
    boards = [b for b in MAJOR_JOB_BOARDS if include_api_boards or not b.api_source]
    rank = {"de": 0 if german else 2, "global": 1, "us": 3}
    return sorted(boards, key=lambda b: rank.get(b.region, 1))


# --------------------------------------------------------------------------- #
# Search-result titles from job boards ("Title - Company - City | Indeed.com")
# --------------------------------------------------------------------------- #

_BOARD_SUFFIX = re.compile(
    r"\s*[|\-–—]\s*(indeed(\.com|\.de)?|linkedin|stepstone(\.de)?|glassdoor|monster(\.com|\.de)?|"
    r"ziprecruiter|wellfound|arbeitnow|jobware(\.de)?)\s*$",
    re.I,
)
_LINKEDIN = re.compile(r"^(?P<company>.+?) hiring (?P<title>.+?)(?: in (?P<loc>.+))?$", re.I)
_BEI = re.compile(r"^(?P<title>.+?) bei (?P<company>.+?)(?: in (?P<loc>.+))?$", re.I)
_RESULTS_PAGE = re.compile(
    r"(^\d[\d.,]*\+?\s)|(\b\d[\d.,]*\+?\s+(jobs|stellen|stellenangebote|vacancies)\b)|"
    r"^(jobs|stellenangebote|stellen) (in|für|for)\b",
    re.I,
)


def looks_like_results_page(title: str) -> bool:
    """True for board search/overview pages ("1.234 Python Jobs in Berlin")."""
    return bool(_RESULTS_PAGE.search(title or ""))


def split_board_title(title: str, url: Optional[str]) -> Tuple[str, str, Optional[str]]:
    """Split a job-board search-result title into (title, company, location).

    Only applied to known boards and only when the title has the board's
    usual shape; otherwise the cleaned title is returned with an empty
    company — never a guessed one.
    """
    raw = (title or "").strip()
    if board_for_url(url) is None:
        return raw, "", None
    t = _BOARD_SUFFIX.sub("", raw).strip()
    m = _LINKEDIN.match(t) or _BEI.match(t)
    if m:
        return m.group("title").strip(), m.group("company").strip(), (m.group("loc") or "").strip() or None
    parts = [p.strip() for p in re.split(r"\s+[-–—]\s+", t) if p.strip()]
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return parts[0], parts[1], None
    return t, "", None


# --------------------------------------------------------------------------- #
# Cross-source vacancy key
# --------------------------------------------------------------------------- #

_GENDER_TAG = re.compile(r"\((?:m|w|f|d|x|h|div)(?:\s*/\s*(?:m|w|f|d|x|h|div))+\)|\b[mwfd]/[mwfd](?:/[mwfdx])?\b", re.I)
_LEGAL_FORM = re.compile(r"\b(gmbh|ag|se|kg|ug|inc|llc|ltd|limited|corp|co|plc|bv|sa|sas)\b\.?", re.I)


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def vacancy_key(title: str, company: str) -> str:
    """Source-independent identity of a vacancy; "" when company is unknown.

    Title gender tags ("(m/w/d)") and company legal forms ("GmbH") are
    ignored so the same vacancy from an ATS feed and a job board collapses
    to one key.  Combine with :func:`same_city` to keep same-titled
    openings in different cities apart.
    """
    company_n = _norm(_LEGAL_FORM.sub(" ", company or ""))
    title_n = _norm(_GENDER_TAG.sub(" ", title or ""))
    if not company_n or not title_n:
        return ""
    return f"{title_n}|{company_n}"


def city_of(location: Optional[str]) -> str:
    return _norm((location or "").split(",")[0])


def same_city(a: Optional[str], b: Optional[str]) -> bool:
    """Cities agree, or at least one side is unknown."""
    ca, cb = city_of(a), city_of(b)
    return not ca or not cb or ca == cb


def is_aggregator(url: Optional[str]) -> bool:
    return board_for_url(url) is not None

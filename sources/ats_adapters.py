"""Public ATS / job-board API adapters.

Every endpoint used here is a documented, unauthenticated public feed:

* Ashby          https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true
* SmartRecruiters https://api.smartrecruiters.com/v1/companies/{id}/postings (Posting API, paginated)
* Recruitee      https://{company}.recruitee.com/api/offers/
* Personio       https://{company}.jobs.personio.de/xml (XML job feed)
* Workable       https://www.workable.com/api/accounts/{subdomain}?details=true (job widget API)
* Arbeitnow      https://www.arbeitnow.com/api/job-board-api (public job board API, paginated)

Boards come from ``cfg.ats_boards`` ("ashby:linear"), the
``source_registry`` table (auto-discovered) and ``DEFAULT_BOARDS``.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Iterator, List, Optional
from urllib.parse import quote, urlencode

from config.search_config import SearchConfig
from services.sanitization import strip_html
from .ats_base import MAX_PAGES, ATSBoardSource, BoardError, detect_workplace, employment_type_from
from .base import RawListing
from .registry import register


def _s(v) -> str:
    return v.strip() if isinstance(v, str) else ""


def _salary(lo, hi, currency, period=None) -> Optional[str]:
    if lo in (None, "", 0) and hi in (None, "", 0):
        return None
    rng = f"{lo}–{hi}" if lo and hi and lo != hi else str(lo or hi)
    return " ".join(x for x in (rng, currency or "", f"per {period}" if period else "") if x)


@register
class AshbySource(ATSBoardSource):
    name = "Ashby"
    slug = "ashby"
    provider = "ashby"
    official_search_url = "https://jobs.ashbyhq.com/"
    DEFAULT_BOARDS = ["ashby", "linear"]

    def fetch_board(self, board: str, cfg: SearchConfig) -> Iterator[RawListing]:
        data = self._get_json(
            f"https://api.ashbyhq.com/posting-api/job-board/{quote(board, safe='')}"
            "?includeCompensation=true")
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise BoardError("unexpected payload (no 'jobs' list)")
        for j in data["jobs"]:
            if not isinstance(j, dict) or j.get("isListed") is False:
                continue
            title = _s(j.get("title"))
            url = _s(j.get("jobUrl")) or f"https://jobs.ashbyhq.com/{board}/{j.get('id')}"
            html = j.get("descriptionHtml") or ""
            comp = j.get("compensation") or {}
            extra = [_s(x.get("location")) for x in (j.get("secondaryLocations") or []) if isinstance(x, dict)]
            location = ", ".join(x for x in [_s(j.get("location")), *extra] if x) or None
            wt = {"Hybrid": "hybrid", "OnSite": "onsite", "Remote": "remote"}.get(j.get("workplaceType") or "")
            yield RawListing(
                source=self.name,
                title=title,
                company=_s(data.get("organizationName")) or board,
                location=location,
                description_html=html or None,
                description_text=_s(j.get("descriptionPlain")) or (strip_html(html) if html else None),
                employment_type=employment_type_from(j.get("employmentType"), title),
                workplace_arrangement="remote" if j.get("isRemote") else (wt or detect_workplace(title, location)),
                salary=_s(comp.get("compensationTierSummary")) or None,
                original_url=url,
                application_url=_s(j.get("applyUrl")) or url,
                published_at=j.get("publishedAt"),
                external_job_id=_s(j.get("id")) or None,
            )


@register
class SmartRecruitersSource(ATSBoardSource):
    name = "SmartRecruiters"
    slug = "smartrecruiters"
    provider = "smartrecruiters"
    official_search_url = "https://jobs.smartrecruiters.com/"
    DEFAULT_BOARDS = ["Visa", "BoschGroup"]
    PAGE_SIZE = 100
    #: Detail requests (full description) per board; list data is kept otherwise.
    MAX_DETAILS = 25

    def fetch_board(self, board: str, cfg: SearchConfig) -> Iterator[RawListing]:
        base = f"https://api.smartrecruiters.com/v1/companies/{quote(board, safe='')}/postings"
        offset = details = 0
        for _ in range(MAX_PAGES):
            data = self._get_json(f"{base}?{urlencode({'limit': self.PAGE_SIZE, 'offset': offset})}")
            if not isinstance(data, dict) or not isinstance(data.get("content"), list):
                raise BoardError("unexpected payload (no 'content' list)")
            page = data["content"]
            for p in page:
                if not isinstance(p, dict):
                    continue
                pid = _s(p.get("id"))
                title = _s(p.get("name"))
                loc = p.get("location") or {}
                location = _s(loc.get("fullLocation")) or ", ".join(
                    x for x in (_s(loc.get("city")), _s(loc.get("country")).upper()) if x) or None
                url = apply_url = f"https://jobs.smartrecruiters.com/{board}/{pid}"
                desc = None
                if details < self.MAX_DETAILS and pid:
                    details += 1
                    try:
                        d = self._get_json(f"{base}/{quote(pid, safe='')}")
                        sections = ((d.get("jobAd") or {}).get("sections") or {}) if isinstance(d, dict) else {}
                        desc = "\n".join(
                            f"<h3>{_s(s.get('title'))}</h3>{s.get('text') or ''}"
                            for s in sections.values() if isinstance(s, dict) and s.get("text")) or None
                        url = _s(d.get("postingUrl")) or url
                        apply_url = _s(d.get("applyUrl")) or url
                    except BoardError:
                        pass  # keep the list-level record
                yield RawListing(
                    source=self.name,
                    title=title,
                    company=_s((p.get("company") or {}).get("name")) or board,
                    location=location,
                    country=_s(loc.get("country")).upper() or None,
                    description_html=desc,
                    description_text=strip_html(desc) if desc else None,
                    employment_type=employment_type_from(_s((p.get("typeOfEmployment") or {}).get("label")), title),
                    workplace_arrangement="remote" if loc.get("remote") else detect_workplace(title, location),
                    experience_requirements=_s((p.get("experienceLevel") or {}).get("label")) or None,
                    original_url=url,
                    application_url=apply_url,
                    published_at=p.get("releasedDate"),
                    external_job_id=pid or None,
                )
            offset += len(page)
            total = data.get("totalFound")
            if not page or (isinstance(total, int) and offset >= total):
                break


@register
class RecruiteeSource(ATSBoardSource):
    name = "Recruitee"
    slug = "recruitee"
    provider = "recruitee"
    official_search_url = "https://recruitee.com/"

    def fetch_board(self, board: str, cfg: SearchConfig) -> Iterator[RawListing]:
        data = self._get_json(f"https://{quote(board, safe='')}.recruitee.com/api/offers/")
        if not isinstance(data, dict) or not isinstance(data.get("offers"), list):
            raise BoardError("unexpected payload (no 'offers' list)")
        for o in data["offers"]:
            if not isinstance(o, dict):
                continue
            title = _s(o.get("title"))
            html = (o.get("description") or "") + (o.get("requirements") or "")
            url = _s(o.get("careers_url")) or f"https://{board}.recruitee.com/o/{o.get('slug')}"
            sal = o.get("salary")
            location = _s(o.get("location")) or ", ".join(
                x for x in (_s(o.get("city")), _s(o.get("country"))) if x) or None
            workplace = ("remote" if o.get("remote") else "hybrid" if o.get("hybrid")
                         else "onsite" if o.get("on_site") else detect_workplace(title, location))
            yield RawListing(
                source=self.name,
                title=title,
                company=_s(o.get("company_name")) or board,
                location=location,
                country=_s(o.get("country_code")).upper() or None,
                description_html=html or None,
                description_text=strip_html(html) if html else None,
                employment_type=employment_type_from(o.get("employment_type_code"), title),
                workplace_arrangement=workplace,
                salary=(_salary(sal.get("min"), sal.get("max"), sal.get("currency"), sal.get("period"))
                        if isinstance(sal, dict) else None),
                education_requirements=_s(o.get("education_code")) or None,
                experience_requirements=_s(o.get("experience_code")) or None,
                original_url=url,
                application_url=_s(o.get("careers_apply_url")) or url,
                published_at=o.get("published_at") or o.get("created_at"),
                external_job_id=str(o["id"]) if o.get("id") is not None else None,
            )


@register
class PersonioSource(ATSBoardSource):
    name = "Personio"
    slug = "personio"
    provider = "personio"
    official_search_url = "https://www.personio.com/"

    def fetch_board(self, board: str, cfg: SearchConfig) -> Iterator[RawListing]:
        host = f"https://{quote(board, safe='')}.jobs.personio.de"
        text = self._get_text(f"{host}/xml")
        try:
            root = ET.fromstring(text.encode("utf-8"))
        except ET.ParseError as exc:
            raise BoardError(f"malformed XML ({exc})") from exc
        if root.tag != "workzag-jobs":
            raise BoardError(f"unexpected XML root <{root.tag}>")

        def t(el, tag: str) -> str:
            node = el.find(tag)
            return (node.text or "").strip() if node is not None and node.text else ""

        for pos in root.findall("position"):
            title, pid = t(pos, "name"), t(pos, "id")
            html = "\n".join(f"<h3>{t(jd, 'name')}</h3>{t(jd, 'value')}"
                             for jd in pos.findall("jobDescriptions/jobDescription"))
            offices = [(o.text or "").strip() for o in pos.findall("additionalOffices/office")]
            location = ", ".join(x for x in [t(pos, "office"), *offices] if x) or None
            url = f"{host}/job/{pid}" if pid else host
            yield RawListing(
                source=self.name,
                title=title,
                company=t(pos, "subcompany") or board,
                location=location,
                description_html=html or None,
                description_text=strip_html(html) if html else None,
                employment_type=employment_type_from(f"{t(pos, 'employmentType')} {t(pos, 'schedule')}", title),
                workplace_arrangement=detect_workplace(title, location),
                experience_requirements=t(pos, "yearsOfExperience") or t(pos, "seniority") or None,
                industry=t(pos, "occupationCategory") or None,
                original_url=url,
                application_url=url,
                published_at=t(pos, "createdAt") or None,
                external_job_id=pid or None,
            )


@register
class WorkableSource(ATSBoardSource):
    name = "Workable"
    slug = "workable"
    provider = "workable"
    official_search_url = "https://apply.workable.com/"

    def fetch_board(self, board: str, cfg: SearchConfig) -> Iterator[RawListing]:
        data = self._get_json(f"https://www.workable.com/api/accounts/{quote(board, safe='')}?details=true")
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise BoardError("unexpected payload (no 'jobs' list)")
        for j in data["jobs"]:
            if not isinstance(j, dict):
                continue
            title = _s(j.get("title"))
            locs = [", ".join(x for x in (_s(l.get("city")), _s(l.get("country"))) if x)
                    for l in (j.get("locations") or []) if isinstance(l, dict)]
            location = "; ".join(x for x in locs if x) or ", ".join(
                x for x in (_s(j.get("city")), _s(j.get("country"))) if x) or None
            url = _s(j.get("url")) or _s(j.get("shortlink")) or (
                f"https://apply.workable.com/{board}/j/{j.get('shortcode')}")
            html = j.get("description") or ""
            yield RawListing(
                source=self.name,
                title=title,
                company=_s(data.get("name")) or board,
                location=location,
                description_html=html or None,
                description_text=strip_html(html) if html else None,
                employment_type=employment_type_from(j.get("employment_type"), title),
                workplace_arrangement="remote" if j.get("telecommuting") else detect_workplace(title, location),
                education_requirements=_s(j.get("education")) or None,
                experience_requirements=_s(j.get("experience")) or None,
                industry=_s(j.get("industry")) or None,
                original_url=url,
                application_url=_s(j.get("application_url")) or url,
                published_at=j.get("published_on") or j.get("created_at"),
                external_job_id=_s(j.get("shortcode")) or None,
            )


@register
class ArbeitnowSource(ATSBoardSource):
    """Arbeitnow's public job-board API (Europe/Germany focused, no key)."""

    name = "Arbeitnow"
    slug = "arbeitnow"
    provider = "arbeitnow"
    official_search_url = "https://www.arbeitnow.com/"
    DEFAULT_BOARDS = ["all"]
    MAX_PAGES = 5

    def fetch_board(self, board: str, cfg: SearchConfig) -> Iterator[RawListing]:
        for page in range(1, self.MAX_PAGES + 1):
            data = self._get_json(f"https://www.arbeitnow.com/api/job-board-api?page={page}")
            if not isinstance(data, dict) or not isinstance(data.get("data"), list):
                raise BoardError("unexpected payload (no 'data' list)")
            for j in data["data"]:
                if not isinstance(j, dict):
                    continue
                title = _s(j.get("title"))
                html = j.get("description") or ""
                location = _s(j.get("location")) or None
                yield RawListing(
                    source=self.name,
                    title=title,
                    company=_s(j.get("company_name")) or None,
                    location=location,
                    description_html=html or None,
                    description_text=strip_html(html) if html else None,
                    employment_type=employment_type_from(" ".join(map(str, j.get("job_types") or [])), title),
                    workplace_arrangement="remote" if j.get("remote") else detect_workplace(title, location),
                    original_url=_s(j.get("url")),
                    application_url=_s(j.get("url")),
                    published_at=j.get("created_at"),
                    external_job_id=_s(j.get("slug")) or None,
                )
            if not data["data"] or not (data.get("links") or {}).get("next"):
                break

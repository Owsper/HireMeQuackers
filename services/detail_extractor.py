"""Detail-page extraction: fetch a job posting URL and extract structured fields.

Strategy (priority order):
  1. JSON-LD JobPosting
  2. Open Graph meta tags
  3. HTML heuristics (article, classes, meta, regexes)

All values are best-effort. Missing values are None — never invented.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from html import unescape
from typing import List, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .sanitization import strip_html, truncate
from .safe_requests import safe_get

logger = logging.getLogger(__name__)

MAX_DESCRIPTION_CHARS = 4000
MAX_RESPONSE_BYTES = 524_288  # 512 KB

_APPLY_TEXTS = {"apply", "apply now", "apply here", "submit application", "apply for this role", "werde teammitglied"}


@dataclass
class ExtractedDetail:
    title: Optional[str] = None
    employer: Optional[str] = None
    description_text: Optional[str] = None
    location: Optional[str] = None
    salary_raw: Optional[str] = None
    salary_currency: Optional[str] = None
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    employment_type: Optional[str] = None
    workplace_arrangement: Optional[str] = None
    posted_date: Optional[str] = None
    closing_date: Optional[str] = None
    required_skills: List[str] = field(default_factory=list)
    experience_years: Optional[str] = None
    education_requirements: Optional[str] = None
    application_url: Optional[str] = None
    json_ld_present: bool = False
    extraction_confidence: float = 0.0


def _parse_salary_range(salary_raw: str) -> tuple[Optional[int], Optional[int], Optional[str]]:
    """Extract (min, max, currency) from a freeform salary string."""
    if not salary_raw:
        return None, None, None
    y = salary_raw.lower()
    if "eur" in y or "€" in salary_raw or "euro" in y:
        cur = "EUR"
    elif "usd" in y or "$" in salary_raw:
        cur = "USD"
    elif "gbp" in y or "£" in salary_raw:
        cur = "GBP"
    else:
        cur = None
    nums = re.findall(r"\d{1,3}(?:[.,]\d{3})+|\d+", salary_raw)
    def _num(s: str) -> Optional[int]:
        return int(re.sub(r"[^0-9]", "", s)) if re.sub(r"[^0-9]", "", s) else None
    if nums:
        vals = [_num(p) for p in nums]
        vals = [v for v in vals if v and v > 1000]
        if len(vals) >= 2:
            vals.sort()
            return vals[0], vals[-1], cur
        if len(vals) == 1:
            return vals[0], vals[0], cur
    return None, None, cur


_EMPLOYMENT_KEYWORDS = [
    ("working_student", ("working student", "werkstudent")),
    ("internship", ("intern", "praktikum", "duales studium")),
    ("part_time", ("part-time", "part time", "0,5", "50%")),
    ("full_time", ("full-time", "full time", "100%")),
    ("freelance", ("freelance", "contractor", "freiberuflich")),
    ("apprenticeship", ("apprentice", "ausbildung")),
]


def _classify_employment(text: str) -> Optional[str]:
    y = (text or "").lower()
    for etype, kws in _EMPLOYMENT_KEYWORDS:
        if any(kw in y for kw in kws):
            return etype
    return None


_WORKPLACE_KEYWORDS = [
    ("remote", ("remote", "fully remote", "von zu hause")),
    ("on_site", ("on-site", "on site", "onsite", "vor ort", "im büro")),
    ("hybrid", ("hybrid",)),
]


def _classify_workplace(text: str) -> Optional[str]:
    y = (text or "").lower()
    for wtype, kws in _WORKPLACE_KEYWORDS:
        if any(kw in y for kw in kws):
            return wtype
    return None


def _extract_json_ld(html: str) -> dict:
    """Find the first @type JobPosting JSON-LD block; return parsed dict."""
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text()
        if not raw:
            continue
        raw = raw.strip()
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            try:
                data = json.loads(unescape(raw))
            except (json.JSONDecodeError, ValueError):
                continue
        nodes = data if isinstance(data, list) else [data]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            t = str(node.get("@type", "")).lower()
            if "jobposting" in t:
                if isinstance(node.get("description"), dict):
                    node["description"] = node["description"].get("value", "")
                return node
    return {}


def _json_ld_to_detail(ld: dict, base_url: str) -> ExtractedDetail:
    d = ExtractedDetail(json_ld_present=True, extraction_confidence=0.9)
    # JobPosting schema: "title" is the canonical field; "name" is a legacy alias.
    d.title = ld.get("title") or ld.get("name")
    d.description_text = _clean_description(ld.get("description"))
    ho = ld.get("hiringOrganization") or {}
    if isinstance(ho, dict):
        d.employer = ho.get("name")
    jl = ld.get("jobLocation")
    if isinstance(jl, dict):
        addr = jl.get("address") or {}
        if isinstance(addr, dict):
            parts = [addr.get(k) for k in ("streetAddress", "addressLocality", "addressRegion") if addr.get(k)]
            if parts:
                d.location = ", ".join(parts)
        elif isinstance(jl.get("name"), str):
            d.location = jl.get("name")
    bs = ld.get("baseSalary")
    if isinstance(bs, dict):
        val = bs.get("value")
        if isinstance(val, dict):
            d.salary_min = val.get("minValue")
            d.salary_max = val.get("maxValue")
            d.salary_currency = val.get("currency", bs.get("currency"))
            d.salary_raw = f"{d.salary_min}-{d.salary_max} {d.salary_currency or ''}".strip()
        elif isinstance(val, (int, float)):
            d.salary_min = d.salary_max = int(val)
            d.salary_currency = bs.get("currency")
            d.salary_raw = str(val)
    if "employmentType" in ld:
        d.employment_type = _classify_employment(str(ld.get("employmentType")) or "")
    if d.employment_type is None:
        d.employment_type = _classify_employment(f"{d.title or ''} {d.description_text or ''}")
    vp = ld.get("virtualLocation")
    if isinstance(vp, str) and vp.lower() == "remote":
        d.workplace_arrangement = "remote"
    elif d.workplace_arrangement is None:
        combined = f"{d.title or ''} {d.description_text or ''}"
        d.workplace_arrangement = _classify_workplace(combined)
    d.posted_date = ld.get("datePosted")
    d.closing_date = ld.get("validThrough")
    app = ld.get("directApply")
    if isinstance(app, str):
        d.application_url = urljoin(base_url, app) if app else None
    pts = ld.get("percentOfWeeklyHours")
    if pts is not None and d.employment_type is None:
        d.employment_type = "part_time" if int(pts) < 80 else "full_time"
    d.extraction_confidence = 0.9
    return d


def _clean_description(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    text = strip_html(text) if "<" in text else text
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return None
    return truncate(text, MAX_DESCRIPTION_CHARS)


def _html_to_detail(html: str, base_url: str) -> ExtractedDetail:
    soup = BeautifulSoup(html, "lxml")
    d = ExtractedDetail(extraction_confidence=0.0)

    # Title: og:title then <title>
    og_title = soup.find("meta", attrs={"property": "og:title"})
    if og_title and og_title.get("content"):
        d.title = og_title["content"].strip()
    if not d.title and soup.title and soup.title.string:
        d.title = soup.title.string.strip()[:200]

    # Description: og:description, then article / class heuristics
    og_desc = soup.find("meta", attrs={"property": "og:description"})
    cand: Optional[str] = None
    if og_desc and og_desc.get("content"):
        cand = og_desc["content"]
    if not cand:
        art = soup.find("article")
        if art and len(art.get_text(" ", strip=True)) > 80:
            cand = art.get_text("\n", strip=True)
    if not cand:
        for cls in ("job-description", "job_description", "description", "jobdetails", "edit-block-body"):
            div = soup.find(attrs={"class": re.compile(cls, re.I)})
            if div and len(div.get_text(" ", strip=True)) > 80:
                cand = div.get_text("\n", strip=True)
                break
    if not cand:
        for sec in soup.find_all(["section", "div"]):
            txt = sec.get_text(" ", strip=True)
            if ("responsibilit" in txt.lower() or "about the role" in txt.lower()) and len(txt) > 150:
                cand = sec.get_text("\n", strip=True)
                break
    d.description_text = _clean_description(cand)

    # Location
    loc = None
    span = soup.find(attrs={"class": re.compile(r"^location|job-location|jobLocation", re.I)})
    if span:
        loc = span.get_text(" ", strip=True)
    if not loc:
        meta_addr = soup.find("meta", attrs={"itemprop": "address"})
        if meta_addr and meta_addr.get("content"):
            loc = meta_addr["content"]
    if not loc:
        addr = soup.find(attrs={"typeof": re.compile(r"PostalAddress", re.I)})
        if addr:
            loc = addr.get_text(", ", strip=True)
    if loc:
        d.location = truncate(loc, 200)

    # Salary
    text_for_salary = d.description_text or ""
    dm = re.search(
        r"(\d[\d.,]*)\s*(?:-|–|to|bis)\s*(\d[\d.,]*)\s*(EUR|USD|GBP|\$|€|£|EUR/yr|per year|/ year|Jahres(?:gesamt)?\s*(?:gehalt)?)",
        text_for_salary, re.I,
    )
    if dm:
        d.salary_raw = f"{dm.group(1)}-{dm.group(2)} {dm.group(3)}".strip()
        d.salary_min, d.salary_max, d.salary_currency = _parse_salary_range(d.salary_raw)
    else:
        dm2 = re.search(r"(?:gehalt|salary|pay)\D{0,5}(\d[\d.,]*)", text_for_salary, re.I)
        if dm2:
            d.salary_raw = dm2.group(1)
            d.salary_min, d.salary_max, d.salary_currency = _parse_salary_range(d.salary_raw)

    # Employment + workplace
    combined = f"{d.title or ''} {text_for_salary[:1000]}"
    d.employment_type = _classify_employment(combined)
    d.workplace_arrangement = _classify_workplace(combined)

    # Dates
    tm = soup.find("time", attrs={"datetime": True})
    if tm and tm.get("datetime"):
        d.posted_date = tm["datetime"][:10]
    if not d.posted_date:
        pm = re.search(r"posted\s+(\d{4}-\d{2}-\d{2})", text_for_salary, re.I)
        if pm:
            d.posted_date = pm.group(1)
    cm = re.search(r"(?:closing|deadline|ends?)\s*(?:date)?\D{0,3}(\d{4}-\d{2}-\d{2})", text_for_salary, re.I)
    if cm:
        d.closing_date = cm.group(1)

    # Application URL: <a> with apply-like text, or <form action>
    target_host = urlparse(base_url).netloc.lower()
    for a in soup.find_all("a", href=True):
        txt = a.get_text(" ", strip=True).lower()
        if txt in _APPLY_TEXTS or txt.startswith("apply"):
            href = a["href"]
            url = urljoin(base_url, href)
            if urlparse(url).netloc.lower() == target_host:
                d.application_url = url
                break
    if not d.application_url:
        form = soup.find("form", attrs={"action": re.compile(r"apply|submission", re.I)})
        if form and form.get("action"):
            action = form["action"]
            if action.startswith("/"):
                d.application_url = urljoin(base_url, action)

    # Confidence: measure what we actually found
    non_null = sum(
        1 for v in (
            d.title, d.description_text, d.location, d.salary_raw,
            d.employment_type, d.workplace_arrangement, d.application_url,
        ) if v
    )
    d.extraction_confidence = (0.2 + 0.12 * non_null) if non_null else 0.0
    d.extraction_confidence = min(1.0, d.extraction_confidence)

    # Experience heuristic
    em = re.search(r"(\d+)\+?\s*(?:years|jahre)\s*(?:of|of experience|berufserfahrung)", text_for_salary, re.I)
    if em:
        d.experience_years = em.group(1)

    return d


def extract_from_html(html: str, base_url: str) -> ExtractedDetail:
    """Extract structured fields from already-fetched HTML (no network)."""
    if not html:
        return ExtractedDetail(extraction_confidence=0.0)
    try:
        ld = _extract_json_ld(html)
        if ld:
            return _json_ld_to_detail(ld, base_url)
    except Exception:
        logger.debug("JSON-LD extraction failed", exc_info=True)
    try:
        return _html_to_detail(html, base_url)
    except Exception:
        logger.debug("HTML extraction failed", exc_info=True)
        return ExtractedDetail(extraction_confidence=0.0)


def fetch_and_extract(url: str, client=None) -> ExtractedDetail:
    """Fetch the detail page safely and extract. Returns empty result on failure."""
    if not url:
        return ExtractedDetail(extraction_confidence=0.0)
    try:
        result = safe_get(
            url,
            timeout=15.0,
            max_bytes=MAX_RESPONSE_BYTES,
            client=client,
        )
    except Exception:
        logger.warning("detail fetch failed (network/validation) %s", url)
        return ExtractedDetail(extraction_confidence=0.0)
    if result.status_code != 200 or not result.content:
        logger.warning("detail fetch %s -> HTTP %s", url, result.status_code)
        return ExtractedDetail(extraction_confidence=0.0)
    ctype = result.headers.get("content-type", "") if result.headers else ""
    content = result.content.decode("utf-8", errors="replace")
    if "json" in ctype:
        try:
            data = json.loads(content)
            data["@type"] = data.get("@type", "JobPosting")
            return _json_ld_to_detail(data, url)
        except (json.JSONDecodeError, ValueError):
            pass
    return extract_from_html(content, url)
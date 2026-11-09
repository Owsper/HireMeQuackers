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
from datetime import datetime, timezone
from dataclasses import dataclass, field
from html import unescape
from typing import List, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .sanitization import strip_html, truncate
from .safe_requests import safe_get

logger = logging.getLogger("jobhunter.extraction")

MAX_DESCRIPTION_CHARS = 60000
MAX_RESPONSE_BYTES = 3 * 1024 * 1024

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
    preferred_skills: List[str] = field(default_factory=list)
    structured_requirements: List[dict] = field(default_factory=list)
    raw_content: Optional[str] = None
    extraction_method: str = "unavailable"
    extraction_status: str = "UNAVAILABLE"
    extracted_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    description_completeness: float = 0.0
    extraction_errors: List[str] = field(default_factory=list)


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
    y = (text or "").lower().replace("_", " ")
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
        nodes = _nodes(data)
        for node in nodes:
            if not isinstance(node, dict):
                continue
            t = str(node.get("@type", "")).lower()
            if "jobposting" in t:
                if isinstance(node.get("description"), dict):
                    node["description"] = node["description"].get("value", "")
                return node
    return {}


def _nodes(data):
    if isinstance(data, dict):
        yield data
        for value in data.values():
            if isinstance(value, (dict, list)):
                yield from _nodes(value)
    elif isinstance(data, list):
        for item in data:
            yield from _nodes(item)


def _json_ld_to_detail(ld: dict, base_url: str) -> ExtractedDetail:
    d = ExtractedDetail(json_ld_present=True, extraction_confidence=0.9, extraction_method="json_ld")
    # JobPosting schema: "title" is the canonical field; "name" is a legacy alias.
    d.title = ld.get("title") or ld.get("name")
    d.description_text = _clean_description(ld.get("description"))
    ho = ld.get("hiringOrganization") or {}
    if isinstance(ho, dict):
        d.employer = ho.get("name")
    jl = ld.get("jobLocation")
    if isinstance(jl, list):
        jl = jl[0] if jl else None
    if isinstance(jl, dict):
        addr = jl.get("address") or {}
        if isinstance(addr, dict):
            parts = [str(addr.get(k)) for k in ("streetAddress", "addressLocality", "addressRegion", "addressCountry") if addr.get(k)]
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
    if ld.get("jobLocationType") == "TELECOMMUTE" or isinstance(vp, str) and vp.lower() == "remote":
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
        try:
            d.employment_type = "part_time" if int(pts) < 80 else "full_time"
        except (ValueError, TypeError):
            d.extraction_errors.append("invalid_percent_of_weekly_hours")
    for key, attr in (("skills", "required_skills"), ("qualifications", "education_requirements"), ("experienceRequirements", "experience_years")):
        value = ld.get(key)
        if isinstance(value, str):
            setattr(d, attr, [value] if attr == "required_skills" else value)
    d.extraction_confidence = 0.9
    return d


def _clean_description(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    if not isinstance(text, str):
        return None
    text = unescape(text)
    if "<" in text:
        soup = BeautifulSoup(unescape(text), "html.parser")
        for elem in soup.select("script,style,nav,header,footer,aside,form,[role=banner],[role=navigation],.cookie-banner,.advertisement"):
            elem.decompose()
        for li in soup.find_all("li"):
            li.insert_before("\n- ")
        text = soup.get_text("\n", strip=True)
    text = unescape(text)
    text = "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines())
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
    for elem in soup.select("script,style,nav,header,footer,aside,form,[role=banner],[role=navigation],.cookie-banner,.advertisement"):
        elem.decompose()

    # Description: og:description, then article / class heuristics
    og_desc = soup.find("meta", attrs={"property": "og:description"})
    cand: Optional[str] = None
    # Prefer actual job content over the site's search/social summary.
    if not cand:
        art = soup.find("article")
        if art and len(art.get_text(" ", strip=True)) > 80:
            cand = str(art)
    if not cand:
        for cls in ("job-description", "job_description", "description", "jobdetails", "edit-block-body"):
            div = soup.find(attrs={"class": re.compile(cls, re.I)})
            if div and len(div.get_text(" ", strip=True)) > 80:
                cand = str(div)
                break
    if not cand:
        main = soup.find("main")
        if main and len(main.get_text(" ", strip=True)) > 150:
            cand = str(main)
    if not cand and og_desc and og_desc.get("content"):
        cand = og_desc["content"]
        d.extraction_method = "metadata"
    if not cand:
        meta = soup.find("meta", attrs={"name": "description"})
        if meta and meta.get("content"):
            cand = meta["content"]
            d.extraction_method = "metadata"
    if d.extraction_method != "metadata":
        d.extraction_method = "html"
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
    candidates = []
    errors = []
    try:
        ld = _extract_json_ld(html)
        if ld:
            candidates.append(_json_ld_to_detail(ld, base_url))
    except (ValueError, TypeError, AttributeError):
        errors.append("json_ld_invalid")
        logger.warning("JSON-LD extraction failed")
    # Common embedded application data (Next.js and JSON script payloads).
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script"):
        if script.get("type") == "application/ld+json":
            continue
        if script.get("id") == "__NEXT_DATA__" or script.get("type") == "application/json":
            try:
                data = json.loads(script.string or script.get_text())
                for node in _nodes(data):
                    desc = node.get("descriptionHtml") or node.get("descriptionPlain") or node.get("jobDescription") or node.get("description")
                    title = node.get("title") or node.get("jobTitle")
                    if isinstance(desc, str) and isinstance(title, str) and len(desc) > 80:
                        d = _json_ld_to_detail({"title": title, "description": desc}, base_url)
                        d.json_ld_present = False
                        d.extraction_method = "embedded_json"
                        candidates.append(d)
            except (ValueError, TypeError):
                errors.append("embedded_json_invalid")
    try:
        candidates.append(_html_to_detail(html, base_url))
    except (ValueError, TypeError, AttributeError):
        errors.append("html_invalid")
    if not candidates:
        return ExtractedDetail(extraction_errors=errors)
    # Incomplete structured data must not hide a substantive body description.
    d = max(candidates, key=lambda x: (len(x.description_text or "") > 300, x.extraction_method != "metadata", len(x.description_text or "")))
    for other in candidates:
        for attr in ("title", "employer", "location", "salary_raw", "salary_min", "salary_max", "salary_currency", "employment_type", "workplace_arrangement", "posted_date", "closing_date", "application_url"):
            if not getattr(d, attr) and getattr(other, attr):
                setattr(d, attr, getattr(other, attr))
    d.raw_content = html[:MAX_RESPONSE_BYTES]
    d.extraction_errors.extend(errors)
    return finalize_detail(d)


def finalize_detail(d: ExtractedDetail) -> ExtractedDetail:
    from services.job_intelligence import description_quality, extract_requirements
    from database.models import Job
    for attr in ("title", "employer", "description_text", "location", "salary_raw", "salary_currency", "employment_type", "workplace_arrangement", "posted_date", "closing_date", "experience_years", "education_requirements", "application_url"):
        value = getattr(d, attr)
        if value is not None and not isinstance(value, str):
            setattr(d, attr, None)
            d.extraction_errors.append(f"invalid_{attr}")
    for attr in ("salary_min", "salary_max"):
        value = getattr(d, attr)
        if value is not None and type(value) not in (int, float):
            setattr(d, attr, None)
            d.extraction_errors.append(f"invalid_{attr}")
    d.extraction_status, d.description_completeness = description_quality(d.description_text, d.extraction_method)
    if len(d.description_text or "") >= MAX_DESCRIPTION_CHARS:
        d.extraction_status = "PARTIAL"
        d.description_completeness = min(.65, d.description_completeness)
        d.extraction_errors.append("description_truncated")
    # Preserve low-level confidence for compatibility, but never equate metadata with completeness.
    if not d.description_text:
        d.extraction_confidence = min(d.extraction_confidence, .25)
    if d.extraction_method == "metadata":
        d.extraction_confidence = min(d.extraction_confidence, .35)
    if d.description_text:
        job = Job(description=d.description_text, required_skills_json=json.dumps(d.required_skills), preferred_skills_json=json.dumps(d.preferred_skills),
                  experience_requirements=d.experience_years, education_requirements=d.education_requirements, location=d.location)
        d.structured_requirements = extract_requirements(job)
    return d


def fetch_and_extract(url: str, client=None) -> ExtractedDetail:
    """Fetch the detail page safely and extract. Returns empty result on failure."""
    if not url:
        return ExtractedDetail(extraction_errors=["missing_url"])
    from services.robots import is_allowed_by_robots
    from services.ats_detail import fetch_ats_detail
    from config.settings import get_config
    if not is_allowed_by_robots(url, client=client):
        return ExtractedDetail(extraction_errors=["robots_disallowed_or_unavailable"], extraction_status="NEEDS_MANUAL_REVIEW")
    from services.robots import wait_for_crawl_slot
    wait_for_crawl_slot(url)
    errors = []
    try:
        ats = fetch_ats_detail(url, client=client)
        if ats and ats.description_text:
            return ats
        if ats:
            errors.extend(ats.extraction_errors)
    except Exception as exc:
        errors.append(f"ats_fetch_failed:{type(exc).__name__}")
    try:
        result = safe_get(
            url,
            timeout=15.0,
            max_bytes=MAX_RESPONSE_BYTES,
            client=client,
            redirect_policy=lambda target: is_allowed_by_robots(target, client=client),
        )
    except Exception as exc:
        logger.warning("Detail fetch failed: %s", type(exc).__name__)
        return ExtractedDetail(extraction_errors=errors + [f"fetch_failed:{type(exc).__name__}"])
    if result.status_code != 200 or not result.content:
        logger.warning("Detail fetch HTTP %s", result.status_code)
        return ExtractedDetail(extraction_errors=errors + [f"http_{result.status_code}"], extraction_status="NEEDS_MANUAL_REVIEW" if result.status_code in {401, 403, 429} else "UNAVAILABLE")
    ctype = result.headers.get("content-type", "") if result.headers else ""
    content = result.content.decode("utf-8", errors="replace")
    if "json" in ctype:
        try:
            data = json.loads(content)
            if isinstance(data, dict) and "jobposting" in str(data.get("@type", "")).lower():
                detail = _json_ld_to_detail(data, url)
                detail.raw_content = content
                return finalize_detail(detail)
        except (json.JSONDecodeError, ValueError, TypeError):
            errors.append("json_endpoint_invalid")
    soup = BeautifulSoup(content, "html.parser")
    title = soup.title.get_text().lower() if soup.title else ""
    if any(x in title for x in ("captcha", "access denied", "sign in", "just a moment")):
        return ExtractedDetail(extraction_status="NEEDS_MANUAL_REVIEW", extraction_errors=errors + ["access_restricted"])
    detail = extract_from_html(content, url)
    if getattr(result, "error", None):
        detail.extraction_errors.append("retrieval_incomplete")
        detail.extraction_status = "PARTIAL" if detail.description_text else "UNAVAILABLE"
        detail.description_completeness = min(.65, detail.description_completeness)
    detail.extraction_errors.extend(errors)
    if get_config().enable_playwright and detail.extraction_status != "COMPLETE":
        try:
            from services.browser_extraction import render_public_page
            rendered = extract_from_html(render_public_page(url), url)
            if len(rendered.description_text or "") > len(detail.description_text or ""):
                rendered.extraction_method = "browser"
                rendered.extraction_errors.extend(detail.extraction_errors)
                detail = finalize_detail(rendered)
        except Exception as exc:
            detail.extraction_errors.append(f"browser_unavailable:{type(exc).__name__}")
            logger.warning("Browser extraction unavailable: %s", type(exc).__name__)
    return detail

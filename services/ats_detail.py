"""Official public detail endpoints for existing ATS integrations."""
from __future__ import annotations
import json
from urllib.parse import parse_qs, quote, urlparse
from services.safe_requests import safe_get
from services.robots import is_allowed_by_robots


def fetch_ats_detail(url: str, client=None):
    from services.detail_extractor import ExtractedDetail, _json_ld_to_detail, finalize_detail
    p = urlparse(url)
    host, parts = (p.hostname or "").lower(), [x for x in p.path.split("/") if x]
    endpoint = provider = identifier = ""
    if host in {"boards.greenhouse.io", "job-boards.greenhouse.io", "job-boards.eu.greenhouse.io"} and parts:
        identifier = parts[-1] if "jobs" in parts else parse_qs(p.query).get("gh_jid", [""])[0]
        if identifier.isdigit():
            provider = "greenhouse"
            endpoint = f"https://boards-api.greenhouse.io/v1/boards/{quote(parts[0], safe='')}/jobs/{identifier}"
    elif host in {"jobs.lever.co", "jobs.eu.lever.co"} and len(parts) >= 2:
        provider, identifier = "lever", parts[1]
        api_host = "api.eu.lever.co" if host == "jobs.eu.lever.co" else "api.lever.co"
        endpoint = f"https://{api_host}/v0/postings/{quote(parts[0], safe='')}/{quote(identifier, safe='')}"
    elif host == "jobs.ashbyhq.com" and len(parts) >= 2:
        provider, identifier = "ashby", parts[1]
        endpoint = f"https://api.ashbyhq.com/posting-api/job-board/{quote(parts[0], safe='')}?includeCompensation=true"
    if not endpoint:
        return None
    if not is_allowed_by_robots(endpoint, client=client):
        return ExtractedDetail(extraction_errors=["ats_robots_disallowed"], extraction_status="NEEDS_MANUAL_REVIEW")
    response = safe_get(endpoint, client=client, timeout=15, max_bytes=3 * 1024 * 1024)
    if response.status_code != 200:
        return ExtractedDetail(extraction_errors=[f"ats_http_{response.status_code}"])
    try:
        data = json.loads(response.content)
        if not isinstance(data, dict):
            raise ValueError("not an object")
        if provider == "ashby":
            data = next((j for j in data.get("jobs", []) if isinstance(j, dict) and str(j.get("id")) == identifier), {})
            desc, title = data.get("descriptionHtml") or data.get("descriptionPlain"), data.get("title")
        elif provider == "lever":
            title = data.get("text")
            desc = (data.get("description") or data.get("descriptionPlain") or "") + "\n" + "\n".join(
                f"<h2>{x.get('text', '')}</h2>{x.get('content', '')}" for x in data.get("lists", []) if isinstance(x, dict))
        else:
            title, desc = data.get("title"), data.get("content")
        if not isinstance(title, str) or not isinstance(desc, str) or not desc.strip():
            return ExtractedDetail(extraction_errors=["ats_incomplete_response"])
        detail = _json_ld_to_detail({"title": title, "description": desc}, url)
        detail.json_ld_present, detail.extraction_method = False, f"ats_{provider}"
        detail.raw_content = response.content.decode("utf-8", errors="replace")
        loc = data.get("location") or (data.get("categories") or {}).get("location")
        detail.location = loc.get("name") if isinstance(loc, dict) else loc if isinstance(loc, str) else None
        detail.application_url = data.get("applyUrl") or data.get("absolute_url") or data.get("jobUrl")
        return finalize_detail(detail)
    except (ValueError, TypeError, AttributeError):
        return ExtractedDetail(extraction_errors=["ats_malformed_response"])

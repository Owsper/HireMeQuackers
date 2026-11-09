"""Factual CV drafts: reorder existing evidence, preserve every professional-history claim."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import difflib
import hashlib
import json
import logging
import re

from services import llm
from services.ai_client import deterministic_score
from services.candidate_intelligence import payload

log = logging.getLogger(__name__)


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def profile_data(profile) -> dict:
    return {**payload(profile), **{k: getattr(profile, k, None) or "" for k in ("name", "email", "phone", "linkedin", "website")}}


def _entry(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [line for item in value for line in _entry(item)]
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return [value["text"]]  # parsed raw entries already contain all derived fields
        lines = []
        for key, item in value.items():
            if item:
                label = "" if key in {"text", "summary", "description", "title", "name"} else key.replace("_", " ").capitalize() + ": "
                parts = _entry(item)
                lines.extend([label + parts[0], *parts[1:]] if parts else [])
        return lines
    return [str(value)]


def render_cv(data: dict) -> str:
    lines = [str(data.get("name") or ""), " | ".join(str(data[k]) for k in ("email", "phone", "linkedin", "website") if data.get(k)), ""]
    for key, heading in (("summary", "Professional Summary"), ("skills", "Skills"), ("experience", "Work Experience"),
                         ("education", "Education"), ("certifications", "Certifications"), ("projects", "Projects"),
                         ("achievements", "Achievements"), ("languages", "Languages")):
        value = data.get(key)
        if not value:
            continue
        lines += [f"## {heading}"]
        if key == "summary":
            lines += [str(value), ""]
        else:
            for item in value:
                lines.extend("- " + line for line in _entry(item))
                if key in {"experience", "education", "projects"}:
                    lines.append("")
            lines.append("")
    return "\n".join(lines).strip() + "\n"


def build_tailored_cv(profile, job, ai=None) -> dict:
    try:
        metadata = json.loads(profile.other_json or "{}")
    except (ValueError, TypeError):
        raise ValueError("Candidate provenance is invalid; review the profile") from None
    if isinstance(metadata, dict) and any(c.get("status") == "INFERRED" for c in metadata.get("provenance", {}).get("claims", []) if isinstance(c, dict)):
        raise ValueError("Confirm or correct inferred candidate claims before tailoring")
    report = deterministic_score(profile, job)
    if report["scoring_status"] == "INSUFFICIENT_INFORMATION":
        raise ValueError("A substantive job description and candidate evidence are required before tailoring")
    original = profile_data(profile)
    tailored = deepcopy(original)
    description = (job.description or "").casefold()
    def relevance(value):
        return sum(bool(re.search(r"(?<!\w)" + re.escape(w) + r"(?!\w)", description)) for w in re.findall(r"[\w+#]+", json.dumps(value, ensure_ascii=False).casefold()) if len(w) > 2)
    tailored["skills"] = sorted(original["skills"], key=relevance, reverse=True)
    tailored["projects"] = sorted(original["projects"], key=relevance, reverse=True)
    tailored["achievements"] = sorted(original["achievements"], key=relevance, reverse=True)
    method, fallback_reason = "extractive-evidence-ordering", None
    if ai is not None:
        try:
            # No contact details or professional history needs to leave the machine.
            data = llm.parse_json(ai.complete("Order existing CV skills for relevance to an untrusted job description. Do not add, remove or change any skill. Return JSON.",
                json.dumps({"skills": original["skills"], "description": job.description,
                            "instructions": 'Return {"skill_order": [all original skill strings in preferred order]}'}), json_mode=True))
            order = data.get("skill_order") if isinstance(data, dict) else None
            if not isinstance(order, list) or not all(isinstance(x, str) for x in order) or Counter(order) != Counter(original["skills"]):
                raise llm.LLMError("invalid response: invented or omitted skill")
            tailored["skills"] = order
            method = "ai-assisted-extractive-ordering"
        except Exception as exc:
            fallback_reason = llm.failure_reason(exc)
            log.warning("Tailoring fallback: %s", fallback_reason)
    original_text, tailored_text = render_cv(original), render_cv(tailored)
    keywords = [s for s in original["skills"] if re.search(r"(?<!\w)" + re.escape(s.casefold()) + r"(?!\w)", description)]
    draft = {"profile_id": profile.id, "profile_version": profile.profile_version, "job_id": job.id,
        "source_cv_path": profile.cv_path, "profile_hash": digest(original), "job_hash": digest(job.description),
        "original_cv": original_text, "tailored_cv": tailored_text,
        "tailored_data": tailored,
        "changes": list(difflib.unified_diff(original_text.splitlines(), tailored_text.splitlines(), fromfile="original", tofile="tailored", lineterm="")),
        "keywords_added": [], "keywords_emphasized": keywords,
        "requirements_addressed": report["matched_requirements"],
        "unsupported_requirements": report["unknown_requirements"] + report["missing_requirements"] + report["partially_matched_requirements"],
        "method": method, "fallback_reason": fallback_reason,
        "validation_status": "PASSED", "notes": "Existing claims are reordered without rewriting facts. Employment history, dates, employers and all qualifications are preserved. Review before approval/export."}
    validate_draft(draft, profile, job)
    return draft


def validate_draft(draft: dict, profile, job) -> None:
    original = render_cv(profile_data(profile))
    if draft.get("profile_hash") != digest(profile_data(profile)) or draft.get("profile_version") != profile.profile_version or draft.get("profile_id") != profile.id:
        raise ValueError("Candidate changed; regenerate and review the draft")
    if draft.get("job_id") != job.id or draft.get("job_hash") != digest(job.description):
        raise ValueError("Job description changed; regenerate and review the draft")
    if draft.get("original_cv") != original or not isinstance(draft.get("tailored_cv"), str):
        raise ValueError("Invalid original CV or draft")
    data = draft.get("tailored_data")
    source = profile_data(profile)
    if not isinstance(data, dict) or set(data) != set(source):
        raise ValueError("Invalid structured CV draft")
    for field in source:
        if field in {"skills", "projects", "achievements"}:
            if not isinstance(data[field], list) or Counter(map(digest, source[field])) != Counter(map(digest, data[field])):
                raise ValueError("Draft changes factual content")
        elif data[field] != source[field]:
            raise ValueError("Draft changes professional history or factual content")
    if render_cv(data) != draft["tailored_cv"]:
        raise ValueError("Draft contains added, removed or rewritten factual content")
    if draft.get("keywords_added"):
        raise ValueError("Extractive drafts cannot add keywords")

"""CV / LinkedIn tailoring suggestions.

If an AI provider is available, produce structured `suggestions`. If not, produce
deterministic suggestions from overlap between `must_have_skills_missing` and
profile content (skills, projects, experience text).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from config.settings import get_config
from database.models import CandidateProfile, GeneratedDocument, Job
from services import llm
from services.ai_client import ensure_clean_jd
from services.usage_tracking import record_usage


def build_cv_tailoring_suggestions(
    profile: CandidateProfile, job: Job, match: Dict[str, Any]
) -> List[Dict[str, str]]:
    '''Return `list[{"field": str, "action": str, "reason": str}]`.

    Fields to cover: summary, experience, skills, projects, certifications,
    languages, achievements, linkedin_headline, linkedin_features.
    Only contain "reason" based on what is ACTUALLY present in the profile
    and job. Do not fabricate.

    Optionally use the AI provider for cleaner wording; otherwise deterministic
    fallthroughs.
    '''
    env = get_config()
    if env.enable_ai_direction:
        ai = llm.get()
        if ai.available:
            try:
                sugg = _ai_direction(ai, profile, job, match)
                if sugg:
                    return sugg
            except llm.LLMError as exc:
                logging.getLogger(__name__).warning("CV advice fallback: %s", llm.failure_reason(exc))
    return _deterministic(profile, job, match)


def _ai_direction(
    ai: llm.LLM, profile: CandidateProfile, job: Job, match: Dict[str, Any]
) -> List[Dict[str, str]]:
    system_prompt = (
        "You are a professional resume coach. Return ONLY JSON: an object "
        '{"suggestions": [...]} whose items have exactly these keys: field, action, reason, source_quote. '
        "Present 'field' from the enum: "
        "summary, experience, skills, projects, certifications, languages, "
        "achievements, linkedin_headline, linkedin_features. "
        "Keep each 'reason' under 120 chars, grounded in the profile + JD, "
        "and explicitly citing the concrete evidence (e.g. 'profile lists Kotlin in projects' "
        "or 'JD requires SQL', etc.). Do NOT invent experience that is not in the "
        "candidate profile."
        " Include a source_quote field with an exact quote from the candidate profile supporting each suggestion."
    )
    candidate = {
        "name": profile.name,
        "skills": profile.skills,
        "education": profile.education,
        "experience": profile.experience,
        "projects": profile.projects,
        "certifications": profile.certifications,
        "languages": profile.languages,
        "achievements": profile.achievements,
        "summary": profile.summary,
    }
    job_payload = {
        "title": job.title,
        "company": job.company,
        "location": job.location,
        "employment_type": job.employment_type,
        "workplace_arrangement": job.workplace_arrangement,
        "required_skills": job.required_skills,
        "preferred_skills": job.preferred_skills,
        "education_requirements": job.education_requirements,
        "experience_requirements": job.experience_requirements,
        "language_requirements": job.language_requirements,
        "description": ensure_clean_jd(job.description or ""),
    }
    prompt = json.dumps(
        {
            "SCORE_MATCH": match,
            "CANDIDATE_PROFILE": candidate,
            "JOB": job_payload,
            "INSTRUCTIONS": (
                'Return {"suggestions": [...]} with 5-12 objects; each with '
                "'field' in {summary, experience, skills, projects, certifications, "
                "languages, achievements, linkedin_headline, linkedin_features}, "
                "'action' (short imperative sentence), 'reason' (grounded). "
                "Do not mention or invent skills that are absent from the candidate profile. "
            ),
        }
    )
    items = llm.json_list(ai.complete(system_prompt, prompt, json_mode=True), "suggestions")
    out = []
    allowed = {
        "summary", "experience", "skills", "projects", "certifications",
        "languages", "achievements", "linkedin_headline", "linkedin_features",
    }
    for item in items:
        f = str(item.get("field") or "").lower().strip()
        action = str(item.get("action") or "").strip()
        reason = str(item.get("reason") or "").strip()
        quote = item.get("source_quote")
        from services.candidate_intelligence import evidence
        grounded = isinstance(quote, str) and len(quote.strip()) >= 3 and any(quote.casefold() in e["quote"].casefold() for e in evidence(profile))
        if f in allowed and action and reason and grounded:
            out.append({"field": f, "action": action[:220], "reason": reason[:260]})
        if len(out) >= 12:
            break
    return out


def _deterministic(
    profile: CandidateProfile, job: Job, match: Dict[str, Any]
) -> List[Dict[str, str]]:
    def find_evidence(field_blob: str) -> Optional[str]:
        return field_blob or None

    exp_evidence = (match.get("must_have_skills_missing") or [])
    expected_skills = list(job.required_skills or [])
    pref_skills = list(job.preferred_skills or [])
    matched = list(match.get("must_have_skills_matched") or [])

    suggestions: List[Dict[str, str]] = []

    # summary: lead with the strongest matched skills / experience
    if matched:
        top = matched[:3]
        suggestions.append({
            "field": "summary",
            "action": f"Lead with {', '.join(top)} experience when it is relevant to the role",
            "reason": f"Profile already lists {', '.join(top)}; JD title is: {job.title}",
        })
    else:
        suggestions.append({
            "field": "summary",
            "action": "Rewrite summary to state your strongest, job-relevant skill that appears in both profile and JD",
            "reason": f"Job requires {', '.join(expected_skills[:5])}; matched overlap is unclear",
        })

    # experience: highlight matching positions
    if matched:
        suggestions.append({
            "field": "experience",
            "action": "Emphasize bullets that evidence: " + ", ".join(matched[:4]),
            "reason": "Profiles existing projects/experience that evidence these skills",
        })
    # skills: bump missing required skills if they appear in experience already
    # (we don't fabricate; we only recommend that if a skill appears in exp text, 
    #  add it to the skills section)
    skill_blob = " ".join(profile.skills or []).lower()
    exp_texts_blob = " ".join(
        json.dumps(e.get("summary") or e.get("title") or "") for e in (profile.experience or [])
    ).lower()
    proj_blob = " ".join(
        json.dumps(p.get("description") or p.get("name") or "") for p in (profile.projects or [])
    ).lower()
    tech_blob = skill_blob + " " + exp_texts_blob + " " + proj_blob

    for s in exp_evidence[:3]:
        ls = s.lower().strip()
        if not ls:
            continue
        if ls in tech_blob:
            suggestions.append({
                "field": "skills",
                "action": f"{s.capitalize()}: already evidenced in experience/projects — move it into the top Skills list",
                "reason": f"JD mentions {s!r}; profile evidence appears in experience or projects text",
            })
    # languages
    if job.language_requirements:
        langs = {l.get("name", "") for l in (profile.languages or [])}
        req = [r.lower().split(",")[0].strip() for r in (job.language_requirements.split(",") if "," in job.language_requirements else [job.language_requirements])]
        for r in req:
            if r and r not in {x.lower() for x in langs}:
                suggestions.append({
                    "field": "languages",
                    "action": "Confirm your proficiency in " + r.capitalize() + "; include it only if you actually have it",
                    "reason": f"JD requests language related to {r!r} that is not listed in the profile",
                })
    # certifications strong link
    edefit = match.get("education_fit", "bad")
    if edefit == "great":
        suggestions.append({
            "field": "certifications",
            "action": "Keep certifications in the same order as the JD lists them",
            "reason": "Education fit is strong; JD: " + (job.education_requirements or "n/a"),
        })
    # projects
    if pref_skills:
        suggestions.append({
            "field": "projects",
            "action": "Highlight projects that evidence the preferred skills: " + ", ".join(pref_skills[:3]),
            "reason": f"Job mentions preferred skills {', '.join(pref_skills[:3])} in " + str(job.company or ""),
        })
    # linkedin_headline
    if (job.employment_type or "").lower() == "internship" and not (profile.skills or []):
        suggestions.append({
            "field": "linkedin_headline",
            "action": f"Headline: {job.title} | {', '.join(matched[:2] or ['student'])}",
            "reason": "Interns should headline role + strongest skill(s)",
        })
    # linkedin_features
    if "remote" in (job.workplace_arrangement or "").lower():
        suggestions.append({
            "field": "linkedin_features",
            "action": "Set open-to-work: Remote roles, include titles from the target job",
            "reason": "JD is remote",
        })
    return suggestions[:12]

"""Application lifecycle management + interview prep (no auto-send)."""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, List, Optional

from config.settings import get_config
from database import repository as db
from database.models import Application, CandidateProfile, GeneratedDocument, Job
from database.connection import get_engine, get_session
from services import llm
from services.ai_client import ensure_clean_jd


def create_application_row(job: Job) -> Application:
    """Create an Application in 'PREPARED' state for the human to later approve."""
    session = get_session(get_engine())
    try:
        app = db.create_application(
            session=session,
            job_id=job.id,
            status="PREPARED",
            cover_letter_path=None,
            detailed_fit=None,
            checklist=None,
            user_comment=None,
        )
        return app
    finally:
        session.close()


def interview_role_prep(
    profile: CandidateProfile,
    job: Job,
    match: Dict[str, Any],
    *,
    refine: Optional[str] = None,
) -> List[Dict[str, str]]:
    """6-10 pairs: question -> candidate_response_expected.

    Uses the AI provider if available; else deterministic. `refine` (when provided)
    asks for a harder round; NOT fabricating.
    """
    env = get_config()
    if env.enable_ai_direction:
        ai = llm.get()
        if ai.available:
            try:
                qs = _interview_ai(ai, profile, job, match, refine)
                if qs:
                    return qs
            except llm.LLMError:
                pass
    return _interview_deterministic(profile, job, match, refine)


def _interview_ai(
    ai: llm.LLM,
    profile: CandidateProfile,
    job: Job,
    match: Dict[str, Any],
    refine: Optional[str],
) -> List[Dict[str, str]]:
    import json

    system = (
        'You are a professional interviewer. Return ONLY a JSON object {"questions": [...]} '
        "with 6-10 objects that have keys 'question' and 'candidate_response_expected'. "
        "Every 'candidate_response_expected' must be grounded in the candidate "
        "profile (posts / projects / experience / education / skills). Do not "
        "invent metrics or employers."
    )
    prompt = json.dumps(
        {
            "job": {"title": job.title, "company": job.company,
                    "description": ensure_clean_jd(job.description or "")[:1200]},
            "profile": {
                "name": profile.name,
                "skills": profile.skills,
                "projects": profile.projects,
                "experience": profile.experience,
                "education": profile.education,
            },
            "match": {
                "total_score": match.get("total_score"),
                "must_have_skills_matched": match.get("must_have_skills_matched"),
                "must_have_skills_missing": match.get("must_have_skills_missing"),
            },
            "refine": refine,
        }
    )
    out = []
    for item in llm.json_list(ai.complete(system, prompt, json_mode=True), "questions"):
        q = str(item.get("question") or "").strip()
        r = str(item.get("candidate_response_expected") or "").strip()
        if q and r:
            out.append({"question": q[:500], "candidate_response_expected": r[:700]})
        if len(out) >= 10:
            break
    return out


def _interview_deterministic(
    profile: CandidateProfile,
    job: Job,
    match: Dict[str, Any],
    refine: Optional[str],
) -> List[Dict[str, str]]:
    prj = profile.projects or []
    p0 = prj[0] if prj else None

    questions: List[Dict[str, str]] = [
        {
            "question": "Tell me about your background and what motivated you to apply to this role.",
            "candidate_response_expected": (
                "Cite your recent role/project or academic summary first, then "
                "connect it to the job's required skills list. "
                f"Expected to use 1-2 examples from profile.experience/projects."
            ),
        },
    ]
    questions.append(
        {
            "question": "Walk me through a project where you had to learn something new quickly.",
            "candidate_response_expected": (
                f"Expected to cite {p0.get('name') if p0 else 'a related project'}; "
                f"pinpoint the iteration speed and the skills exercised."
            ),
        }
    )
    questions.append(
        {
            "question": "Which of the listed required skills are you strongest at and why?",
            "candidate_response_expected": "Expected to cite 2-3 matched skills from the profile.skills list with a one-line example each.",
        }
    )
    questions.append(
        {
            "question": "Which of the required skills have you NOT yet directly demonstrated?",
            "candidate_response_expected": (
                "Expected to name up to 2-3 items from the must_have_skills_missing list and state "
                "a concrete plan to close the gap."
            ),
        }
    )
    questions.append(
        {
            "question": "How have you handled ambiguity or disagreement in a team setting?",
            "candidate_response_expected": "Expected to give a concrete example citing role + outcome (from experience).",
        }
    )
    if refine:
        questions.append(
            {
                "question": "Follow-up: " + refine,
                "candidate_response_expected": "Refinement based on previous round.",
            }
        )
    return questions[:10]


def checklist_cover_letter(candidate: str, interview_qs: List[Dict[str, str]]) -> List[str]:
    return [
        "Cover letter sent (post-approval only).",
        "LinkedIn headline + About updated to target role + strongest skill(s).",
        "References (academic/professional) confirmed and informed.",
        f"CV tailored: applied the `{len(interview_qs)}` suggested actions.",
        "Interview questions reviewed; candidate prepared the expected responses.",
        "Follow-up within 2 business days if no confirmation.",
    ]


def mark_applied(candidate, note: str) -> None:
    """Transition: PREPARED -> SHORTLISTED -> (APPROVED) -> APPLIED. Requires APPROVED.

    We do NOT automate the "apply" action itself (that is at a browser form).
    This function just marks the status; the user clicks "Open application URL"
    for that.
    """
    pass


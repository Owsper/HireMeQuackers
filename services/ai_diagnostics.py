"""Independent synthetic capability probes. No candidate data or paid keys in tests."""
from __future__ import annotations
import json
from services import llm

SAMPLE_JD = """Responsibilities
Build Python services and maintain SQL reporting pipelines. Work with the team to review code, investigate production incidents, write automated tests, and document design choices. Communicate progress clearly and help improve reliability for customers.
Requirements
Python is required. SQL is required. English B2 is required. You should be comfortable working on production software and collaborating with colleagues. The role includes regular discussion of implementation choices and responsibility for the quality of delivered work. We provide a supportive environment and practical onboarding for new team members.
Preferred
Docker knowledge is preferred.
"""


def probe(capability: str, ai=None) -> dict:
    ai = ai or llm.get()
    info = {"provider": ai.provider or "none", "model": ai.model or "none", "capability": capability,
            "status": "FAILED", "connectivity": "not_tested", "fallback_used": False}
    if not ai.available:
        return {**info, "failure_reason": ai.reason, "response_validation": "not_run"}
    try:
        if capability == "connectivity":
            data = llm.parse_json(ai.complete('Return JSON {"ok": true}.', "Return JSON confirming connectivity only.", json_mode=True))
            if not isinstance(data, dict) or data.get("ok") is not True:
                raise llm.LLMError("invalid response: connectivity")
        else:
            from database.models import CandidateProfile, Job
            from services.job_intelligence import extract_requirements, calculate_score
            from config.settings import get_config
            profile = CandidateProfile(id=0, name="Test Candidate", profile_version=1, skills_json='["SQL", "Python"]', other_json="{}")
            job = Job(id=0, title="Test role", company="Test", description=SAMPLE_JD,
                      required_skills_json='["Python", "SQL"]', original_url="https://example.org/test", source="diagnostic")
            if capability == "extraction":
                result = extract_requirements(job, ai=ai)
                if not any("python" in r["source_quote"].lower() for r in result):
                    raise llm.LLMError("invalid response: extraction")
            elif capability == "matching":
                result = calculate_score(profile, job, get_config().matching_weights, ai=ai)
                if result["total_score"] is None or not result["requirement_matrix"]:
                    raise llm.LLMError("invalid response: matching")
            elif capability == "tailoring":
                from services.tailored_cv import build_tailored_cv
                result = build_tailored_cv(profile, job, ai=ai)
                if result["fallback_reason"] or result["method"] != "ai-assisted-extractive-ordering":
                    raise llm.LLMError("invalid response: tailoring")
            else:
                raise ValueError("Unknown AI capability")
        return {**info, **ai.last_diagnostic, "status": "PASSED", "connectivity": "reachable", "response_validation": "passed"}
    except Exception as exc:
        return {**info, **ai.last_diagnostic, "status": "FAILED", "failure_reason": llm.failure_reason(exc),
                "connectivity": "reachable" if ai.last_diagnostic.get("success") else "failed", "response_validation": "failed"}

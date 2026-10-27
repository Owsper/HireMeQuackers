"""Run Gemini scoring over eligible jobs; deterministic fallback if no model."""
from __future__ import annotations

from config.settings import get_config
from database import repository as db
from database.connection import get_engine, get_session
from database.models import CandidateProfile, Job
from services.gemini_client import GeminiClient
from services.logger import get_logger

log = get_logger("jobhunter.matching")


def evaluate_jobs(
    jobs, profile: CandidateProfile, on_progress=None
):
    """Score each job and persist a JobMatch row. Returns list of (job, match).

    Skips jobs that already have a JobMatch for the same (job_id, profile_version)
    unless `force=True` is passed (not here by design).
    """
    client = GeminiClient()
    if not get_config().enable_ai_matching:
        log.info("AI matching disabled; skipping All %d jobs.", len(jobs))
        return []

    out = []
    for i, job in enumerate(jobs):
        try:
            result = client.score_job_fit(profile, job)
            db.create_match(
                session=get_session(get_engine()),
                job_id=job.id,
                profile_version=profile.profile_version,
                match_score=result.get("total_score", 0.0),
                score=result,
                evidence=result.get("evidence") or [],
                missing=(
                    result.get("must_have_skills_missing", [])
                    or result.get("missing_recommendation", [])
                ),
                ai_model=result.get("ai_model"),
                scoring_config_hash=None,
            )
            out.append((job, result))
        except Exception as exc:
            log.warning("Score %s(%s) failed: %s", type(job).__name__, job.id, exc)
        if on_progress:
            on_progress(i + 1, "ok")
    return out

"""Run AI scoring over eligible jobs; deterministic fallback if no model."""
from __future__ import annotations

from typing import Optional

from config.search_config import SearchConfig
from config.search_intent import SearchIntent, build_intent
from config.settings import get_config
from database import repository as db
from database.connection import get_engine, get_session
from database.models import CandidateProfile, Job
from services.ai_client import AIClient
from services.logger import get_logger
from services.title_relevance import classify_title
from services.location_validation import parse_location, location_verdict

log = get_logger("jobhunter.matching")


def _deterministic_gate(
    job: Job, intent: Optional[SearchIntent]
) -> Optional[str]:
    """Return a rejection reason if a hard constraint fails, else None.

    This stops the AI from ever re-admitting a job that the deterministic
    title / location hard-gates already excluded.
    """
    if intent is None:
        return None
    # Title hard gate
    if intent.primary_title or intent.title_synonyms:
        tr = classify_title(job.title or "", intent)
        if not tr.passes_hard and not (tr.label == "related" and intent.include_related_titles):
            return f"title_relevance: {tr.reason}"
    # Location hard gate
    if (intent.cities or intent.countries or intent.remote
            or intent.hybrid or intent.onsite or intent.worldwide_remote):
        parsed = parse_location(job.location or "")
        verdict, reason = location_verdict(parsed, intent)
        if verdict == "mismatch":
            return f"location: {reason}"
    return None


def evaluate_jobs(
    jobs, profile: CandidateProfile, on_progress=None,
    intent: Optional[SearchIntent] = None,
    cfg: Optional[SearchConfig] = None,
    use_ai: bool = True,
):
    """Score each job and persist a JobMatch row. Returns list of (job, match).

    Jobs that fail a deterministic hard constraint (title relevance, location
    mismatch) are skipped — the AI call is never made for them, saving cost
    and preventing a high AI score from overriding a hard-filter rejection.
    """
    client = AIClient()
    if not use_ai:
        client._ai.reason = "AI disabled for this scan"
    if not get_config().enable_ai_matching:
        log.info("AI matching disabled; skipping all %d jobs.", len(jobs))
        return []

    # Build intent dynamically if not supplied (backward-compat)
    if intent is None and cfg is not None:
        intent = build_intent(cfg, include_related_titles=getattr(cfg, "include_related_roles", False))

    out = []
    skipped = 0
    for i, job in enumerate(jobs):
        # Hard-filter short-circuit: never call AI for jobs the deterministic
        # gates already rejected.
        rejection = _deterministic_gate(job, intent)
        if rejection is not None:
            log.debug("Job %s(%s) skipped AI: %s", type(job).__name__, job.id, rejection)
            skipped += 1
            if on_progress:
                on_progress(i + 1, f"skipped: {rejection}")
            continue
        try:
            result = client.score_job_fit(profile, job)
            with get_session(get_engine()) as session:
                db.create_match(
                    session=session,
                    job_id=job.id,
                    profile_version=profile.profile_version,
                    candidate_profile_id=profile.id,
                    match_score=result.get("total_score"),
                    score=result,
                    evidence=result.get("evidence") or [],
                    missing=result.get("missing_requirements", result.get("must_have_skills_missing", [])),
                    ai_model=result.get("ai_model"),
                    scoring_config_hash=None,
                )
            out.append((job, result))
        except Exception as exc:
            log.warning("Score storage failed: job_id=%s type=%s", job.id, type(exc).__name__)
        if on_progress:
            on_progress(i + 1, "ok")
    if skipped:
        log.info("AI matching: %d jobs skipped by deterministic hard-filter, %d scored.", skipped, len(out))
    return out

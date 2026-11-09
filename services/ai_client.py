"""Job-fit scoring through the configured AI provider (see services.llm).

Without a provider, or when a model call fails, a deterministic heuristic
scores the job instead.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Dict, List, Literal, Optional

from database.models import CandidateProfile, Job
from services import llm
from services.cache import cache_get, cache_set, invalidate_profile_prefix
from services.usage_tracking import record_usage


SCORE_RESULT_FIELDS = (
    "total_score",
    "must_have_skills_matched",
    "must_have_skills_missing",
    "preferred_skills_matched",
    "experience_fit",
    "education_fit",
    "language_fit",
    "location_fit",
    "evidence",
    "missing_recommendation",
    "confidence",
)

_SCORE_PROTECTION_VALUES = {"great", "good", "poor", "bad"}

_TITLE_RELEVANCE_VALUES = {"exact", "related", "weak", "none"}
_LOCATION_COMPATIBILITY_VALUES = {"match", "mismatch", "unverified"}
_OPEN_ENDED_VALUES = {"match", "mismatch", "unspecified"}


def _opt_str(v: Any, allowed: set) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip().lower()
    if not s:
        return None
    return s if s in allowed else None


def _opt_int(v: Any, lo: int, hi: int) -> Optional[int]:
    if v is None:
        return None
    try:
        x = int(v)
    except (TypeError, ValueError):
        try:
            x = int(float(v))
        except (TypeError, ValueError):
            return None
    return max(lo, min(hi, x))

_CONFIDENCE = {"high", "medium", "low"}

_PATTERNS_TO_CLEAN = [
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions", re.I),
    re.compile(r"^(system|assistant|user)\s*:\s*", re.I),
    re.compile(r"do\s+(not\s+)?(reveal|expose|print|output)\s+your\s+system\s+prompt", re.I),
    re.compile(r"^you\s+are\s+no\s+longer.*$", re.I | re.S),
]


def ensure_clean_jd(jd: str) -> str:
    """Remove the common prompt-injection guard-rail phrases from untrusted JDs."""
    if not jd:
        return ""
    out = jd
    for p in _PATTERNS_TO_CLEAN:
        out = p.sub("", out)
    # Trim newlines big difference
    return out.strip()


class AIClient:
    def __init__(self):
        self._ai = llm.get()

    @property
    def is_configured(self) -> bool:
        return self._ai.available

    def score_job_fit(self, profile: CandidateProfile, job: Job) -> Dict[str, Any]:
        import logging
        log = logging.getLogger("jobhunter.matching")
        cache_key = self._cache_key(profile, job)
        cached = cache_get(cache_key)
        if cached is not None:
            try:
                result = json.loads(cached)
                if not isinstance(result, dict) or result.get("scoring_method") != "weighted-evidence-v2":
                    raise ValueError("obsolete cached score")
                value = result.get("total_score")
                if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 100 or not isinstance(result.get("requirement_matrix"), list):
                    raise ValueError("invalid cached score")
                result["cache_hit"] = True
                record_usage(job_id=job.id, operation="score", model=result.get("ai_model"), cache_hit=True, note="validated cache hit")
                return result
            except (ValueError, TypeError):
                log.warning("Ignoring invalid scoring cache entry")
        fallback_reason = None
        # Missing descriptions are never sent to AI or assigned invented percentages.
        if not self.is_configured or len((job.description or "").split()) < 30:
            fallback_reason = self._ai.reason or "insufficient_description"
            result = deterministic_score(profile, job)
        else:
            try:
                result = self._call_model(profile, job)
            except Exception as exc:
                fallback_reason = getattr(self._ai, "last_diagnostic", {}).get("failure_reason") or llm.failure_reason(exc)
                log.warning("Matching fell back: provider=%s model=%s reason=%s", getattr(self._ai, "provider", "unknown"), getattr(self._ai, "model", "unknown"), fallback_reason)
                result = deterministic_score(profile, job)
        result["cache_hit"] = False
        result["fallback_reason"] = fallback_reason
        result["diagnostics"] = dict(getattr(self._ai, "last_diagnostic", {}))
        if fallback_reason:
            result["diagnostics"]["fallback_reason"] = fallback_reason
            if fallback_reason in {"invalid response", "response_validation_failed"}:
                result["diagnostics"]["response_validation"] = "failed"
        # A failed AI request is never cached, including its fallback.
        if fallback_reason is None and result.get("total_score") is not None:
            cache_set(cache_key, json.dumps(result, sort_keys=True))
        record_usage(job_id=job.id, operation="score", model=result.get("ai_model"),
                     estimate_tokens=_estimate_tokens(profile, job), cache_hit=False,
                     note=f"status={result['scoring_status']}; fallback={fallback_reason or 'none'}")
        return result

    def health_check(self) -> Dict[str, str]:
        ai = self._ai
        return {
            "provider": ai.provider or "none",
            "model": ai.model or "-",
            "status": "AI_HEALTH_OK" if ai.available else "AI_HEALTH_OFF",
            "detail": ai.reason or "configured (not contacted)",
            "connectivity": "not_tested",
            "capabilities": "not_tested",
        }

    # ---- private ----
    def _cache_key(self, profile: CandidateProfile, job: Job) -> str:
        from config.settings import get_config
        payload = {"profile_id": profile.id, "profile_version": profile.profile_version,
                   "candidate": profile_payload(profile), "other": profile.other_json,
                   "job_id": job.id, "description": job.description,
                   "requirements": [job.required_skills, job.preferred_skills, job.experience_requirements,
                                    job.education_requirements, job.language_requirements, job.location],
                   "extraction": [getattr(job, "extraction_status", None), getattr(job, "extraction_method", None)],
                   "weights": get_config().matching_weights, "version": _scoring_hash(), "provider": self._ai.label}
        return "score:" + hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()

    def _call_model(self, profile: CandidateProfile, job: Job) -> Dict[str, Any]:
        from config.settings import get_config
        from services.job_intelligence import calculate_score
        return calculate_score(profile, job, get_config().matching_weights, ai=self._ai)


def profile_payload(profile: CandidateProfile) -> Dict[str, Any]:
    # name / email / phone are not needed to judge fit, so they never leave the machine here
    return {
        "summary": profile.summary or "",
        "skills": profile.skills or [],
        "education": profile.education or [],
        "experience": profile.experience or [],
        "certifications": profile.certifications or [],
        "languages": profile.languages or [],
        "achievements": profile.achievements or [],
        "projects": profile.projects or [],
    }


def deterministic_score(profile: CandidateProfile, job: Job) -> Dict[str, Any]:
    from config.settings import get_config
    from services.job_intelligence import calculate_score
    return calculate_score(profile, job, get_config().matching_weights)


_ROLE_STOP = frozenset("""intern internship junior senior lead head trainee working student
werkstudent praktikum graduate assistant associate the and for mit und für full time part""".split())


def _term_in(term: str, text: str) -> bool:
    return bool(term) and re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text) is not None


def _role_overlap(job_title: str, past_titles: str) -> bool:
    """True when a meaningful word of the vacancy title (in its English
    form, so "Pflegefachkraft" counts as "nurse") appears in past roles."""
    from services.multilingual import canonicalize_title

    words = {w for w in re.findall(r"[^\W\d_]{3,}", canonicalize_title(job_title))
             if w not in _ROLE_STOP}
    past = canonicalize_title(past_titles)
    return any(_term_in(w, past) for w in words)


def _contains(needle, profile_skills, profile_text) -> bool:
    n = (needle or "").lower().strip()
    if not n:
        return False
    if n in profile_skills:
        return True
    return n in (profile_text or "")


def match_evidence_or_none(profile: CandidateProfile, job: Job) -> List[str]:
    out = []
    for skill in job.required_skills or []:
        s = (skill or "").lower().strip()
        if not s:
            continue
        profile_skills = [x.lower() for x in profile.skills]
        if s in profile_skills:
            out.append(f"Skill match: {skill!r} listed in candidate profile")
        else:
            # look for the match in experience/projects
            blob = json.dumps(
                {
                    "experience": profile.experience or [],
                    "projects": profile.projects or [],
                    "achievements": profile.achievements or [],
                },
                indent=2,
            ).lower()
            if s in blob:
                out.append(f"Evidence mentions {skill!r} in project/experience text")
    return out[:6]


def missing_recommendation(profile: CandidateProfile, job: Job) -> List[str]:
    out = []
    for skill in (job.required_skills or []) + (job.preferred_skills or []):
        s = (skill or "").lower().strip()
        if not s:
            continue
        profile_blob = json.dumps(
            {
                "skills": profile.skills,
                "experience": profile.experience or [],
                "projects": profile.projects or [],
                "achievements": profile.achievements or [],
            },
            indent=2,
        ).lower()
        if s and s not in profile_blob and s not in {x.lower() for x in profile.skills}:
            out.append(f"Add or emphasize evidence for {skill!r}")
    return out[:6]


# ---- helpers ----
def _scoring_hash() -> str:
    """Hash of the 'scoring configuration version' (for cache key)."""
    return "weighted-evidence-v2"


def _safe(exc: Exception) -> str:
    return str(exc)[:200].replace("\n", " ")


def _estimate_tokens(profile: CandidateProfile, job: Job) -> int:
    try:
        blob = profile_payload(profile).__str__() + (job.description or "")
        return max(256, len(blob) // 4)
    except Exception:
        return 0


def normalize_score(data: Dict[str, Any], model: str = "") -> Dict[str, Any]:
    """Clamp + coerce fields and make sure `evidence` is a list[str]."""
    out = {
        "total_score": _score100(data.get("total_score", 50)),
        "must_have_skills_matched": [s.strip() for s in _strs(data.get("must_have_skills_matched")) if s.strip()],
        "must_have_skills_missing": [s.strip() for s in _strs(data.get("must_have_skills_missing")) if s.strip()],
        "preferred_skills_matched": [s.strip() for s in _strs(data.get("preferred_skills_matched")) if s.strip()],
        "experience_fit": str(data.get("experience_fit", "bad")).lower(),
        "education_fit": str(data.get("education_fit", "bad")).lower(),
        "language_fit": str(data.get("language_fit", "bad")).lower(),
        "location_fit": str(data.get("location_fit", "bad")).lower(),
        "evidence": [e[:300] for e in _strs(data.get("evidence"))][:10],
        "missing_recommendation": [m[:300] for m in _strs(data.get("missing_recommendation"))][:10],
        "confidence": str(data.get("confidence", "low")).lower(),
        "ai_model": str(model or data.get("ai_model") or "unknown").lower(),
        "notes": _strs(data.get("notes")),
        # ---- structured search-relevance view (independent of total_score) ----
        "title_relevance": _opt_str(data.get("title_relevance"), _TITLE_RELEVANCE_VALUES),
        "location_compatibility": _opt_str(data.get("location_compatibility"), _LOCATION_COMPATIBILITY_VALUES),
        "skills_match": _opt_int(data.get("skills_match"), 0, 100),
        "seniority_compatibility": _opt_str(data.get("seniority_compatibility"), _OPEN_ENDED_VALUES),
        "employment_type_compatible": _opt_str(data.get("employment_type_compatible"), _OPEN_ENDED_VALUES),
        "verification_confidence": _opt_str(data.get("verification_confidence"), _CONFIDENCE),
        "search_relevance_score": _opt_int(data.get("search_relevance_score"), 0, 100),
    }
    # enforce enum constants
    for k in ("experience_fit", "education_fit", "language_fit", "location_fit"):
        if out[k] not in _SCORE_PROTECTION_VALUES:
            out[k] = "bad"
    if out["confidence"] not in _CONFIDENCE:
        out["confidence"] = "low"
    if out["confidence"] == "low" and not out["evidence"]:
        # Ensure honesty downgraded to low when no evidence
        out["total_score"] = max(0, out["total_score"] - 10)
    return out


def _score100(v: Any) -> int:
    """0..100 (the schema's only scale); junk -> 50."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return 50
    if math.isnan(x):
        return 50
    return int(max(0.0, min(100.0, x)))


def _strs(v: Any) -> List[str]:
    # a lone string must not turn into a list of characters
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v if x is not None]
    return [str(v)]


def invalidate_on_profile_change(profile_version: int, profile_id: int) -> None:
    invalidate_profile_prefix(f"profile:{profile_id}:v{profile_version}")

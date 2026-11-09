"""Helpers to load, upsert, and version candidate profiles."""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from database import repository as db
from database.models import CandidateProfile
from services.ai_client import invalidate_on_profile_change


def load_profile(profile_id: int) -> Optional[CandidateProfile]:
    from database.connection import get_engine, get_session

    session = get_session(get_engine())
    try:
        return db.get_candidate_profile(session, profile_id)
    finally:
        session.close()


def upsert_profile(
    profile_id: int,
    *,
    name: Optional[str] = None,
    email: Optional[str] = None,
    phone: Optional[str] = None,
    summary: Optional[str] = None,
    linkedin: Optional[str] = None,
    website: Optional[str] = None,
    skills: Optional[List[str]] = None,
    education: Optional[List[Dict[str, Any]]] = None,
    experience: Optional[List[Dict[str, Any]]] = None,
    languages: Optional[List[Dict[str, Any]]] = None,
    certifications: Optional[List[str]] = None,
    achievements: Optional[List[str]] = None,
    projects: Optional[List[Dict[str, Any]]] = None,
    cv_path: Optional[str] = None,
    active: Optional[bool] = None,
    other: Optional[Dict[str, Any]] = None,
) -> CandidateProfile:
    """Edit an existing profile. Bumps profile_version on any substantive change."""
    from database.connection import get_engine, get_session

    session = get_session(get_engine())
    try:
        profile = db.get_candidate_profile(session, profile_id)
        if profile is None:
            raise ValueError(f"Profile {profile_id} does not exist")
        from services.candidate_intelligence import payload, provenance
        import json
        before = payload(profile)
        try:
            previous_metadata = json.loads(profile.other_json or "{}")
        except (ValueError, TypeError):
            previous_metadata = {}
        previous_metadata = previous_metadata if isinstance(previous_metadata, dict) else {}
        changed = False
        for k, v in (
            ("name", name),
            ("email", email),
            ("phone", phone),
            ("linkedin", linkedin),
            ("website", website),
            ("summary", summary),
            ("cv_path", cv_path),
        ):
            if v is not None:
                changed = changed or getattr(profile, k) != v
                setattr(profile, k, v)
        if skills is not None:
            if skills != profile.skills:
                profile.skills_json = _j(skills)
                changed = True
        if education is not None and education != profile.education:
            profile.education_json = _j(education)
            changed = True
        if experience is not None and experience != profile.experience:
            profile.experience_json = _j(experience)
            changed = True
        if languages is not None and languages != profile.languages:
            profile.languages_json = _j(languages)
            changed = True
        if certifications is not None and certifications != profile.certifications:
            profile.certifications_json = _j(certifications)
            changed = True
        if achievements is not None and achievements != profile.achievements:
            profile.achievements_json = _j(achievements)
            changed = True
        if projects is not None and projects != profile.projects:
            profile.projects_json = _j(projects)
            changed = True
        if other is not None:
            profile.other_json = _j(other)
            changed = True
        if active is not None:
            if active:
                session.query(CandidateProfile).filter(
                    CandidateProfile.id != profile.id, CandidateProfile.active.is_(True)
                ).update({"active": False}, synchronize_session=False)
            profile.active = active
        # Only bump version on real content change
        if changed:
            metadata = json.loads(profile.other_json or "{}")
            current = payload(profile)
            ledger = provenance(current, source=profile.cv_path or f"profile:{profile.id}")
            previous = {c["field"]: c for c in previous_metadata.get("provenance", {}).get("claims", []) if isinstance(c, dict) and "field" in c}
            for claim in ledger["claims"]:
                root = claim["field"].split("[")[0].split(".")[0]
                if before.get(root) != current.get(root):
                    claim.update(status="USER_CONFIRMED", source="user correction")
                elif claim["field"] in previous and previous[claim["field"]].get("value") == claim["value"]:
                    claim.update(previous[claim["field"]])
                else:
                    claim["status"] = "LEGACY_UNVERIFIED"
            metadata["provenance"] = ledger
            profile.other_json = _j(metadata)
            profile.profile_version += 1
            profile.updated_at = _utcnow()
            # Cache keys include the entire candidate payload and version.
        session.commit()
        session.refresh(profile)
        return profile
    finally:
        session.close()


def upsert_from_extraction(
    extraction: Dict[str, Any], *, active: bool = True
) -> CandidateProfile:
    """Create a new profile from a CV extraction dict.

    extraction keys: name, email, phone, linkedin, website, summary,
    skills (list[str]), education (list[dict]), experience (list[dict]),
    languages (list[dict]), certifications (list[str]), achievements (list[str]),
    projects (list[dict]), other (dict)
    """
    from database.connection import get_engine, get_session

    session = get_session(get_engine())
    try:
        from services.candidate_intelligence import provenance
        other = dict(extraction.get("other") or {})
        if "provenance" not in other:
            other["provenance"] = provenance(extraction, source=extraction.get("cv_path") or "user input")
        profile = db.upsert_candidate_profile(
            session=session,
            name=extraction.get("name") or "Unknown",
            email=extraction.get("email"),
            phone=extraction.get("phone"),
            linkedin=extraction.get("linkedin"),
            website=extraction.get("website"),
            summary=extraction.get("summary"),
            skills=extraction.get("skills") or [],
            education=extraction.get("education") or [],
            experience=extraction.get("experience") or [],
            languages=extraction.get("languages") or [],
            certifications=extraction.get("certifications") or [],
            achievements=extraction.get("achievements") or [],
            projects=extraction.get("projects") or [],
            cv_path=extraction.get("cv_path"),
            active=active,
            other=other,
        )
        return profile
    finally:
        session.close()


def get_active_profile() -> Optional[CandidateProfile]:
    from database.connection import get_engine, get_session

    session = get_session(get_engine())
    try:
        return db.get_active_candidate_profile(session)
    finally:
        session.close()


def _j(value) -> Optional[str]:
    import json

    if value is None:
        return None
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _utcnow():
    from database.models import utcnow

    return utcnow()

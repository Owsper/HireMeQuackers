"""Candidate-profile / CV page.

Layout:
- Top: upload a master CV (PDF or DOCX) and trigger parsing.
- Middle: editable form bound to the *active* CandidateProfile row.
- Bottom: [Save profile] (bumps profile_version on content change) and
  [Delete profile] (soft removal — keeps extracted data, deactivates it).

After parsing, the user is expected to review the extracted fields,
fix any errors, and then click Save. Any content change bumps
CandidateProfile.profile_version, which invalidates cached Gemini
evaluations for that profile version (see services/gemini_client.py).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import streamlit as st

from config.settings import get_config
from database import repository as db
from services import candidate_profile as cp
from services import cv_parser
from services.sanitization import safe_join, sanitize_filename
from ui import util


MAX_UPLOAD_BYTES = 15 * 1024 * 1024  # 15 MB
ALLOWED_SUFFIXES = {".pdf", ".docx"}


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def _active_profile():
    """Return the active CandidateProfile ORM object (or None)."""
    with util.with_session() as session:
        return db.get_active_candidate_profile(session)


def _all_profiles():
    """Return all CandidateProfile rows (for the selector)."""
    from database.models import CandidateProfile

    with util.with_session() as session:
        return session.query(CandidateProfile).order_by(
            CandidateProfile.id.desc()
        ).all()


def _save_upload(uploaded_file) -> Path:
    """Persist the upload under data/cv/ with a sanitized filename."""
    settings = get_config()
    base = settings.data_dir / "cv"
    base.mkdir(parents=True, exist_ok=True)
    safe_name = sanitize_filename(uploaded_file.name, default="cv.docx")
    target = safe_join(base, safe_name)
    # If a file with the same safe-name exists, add a numeric suffix.
    if target.exists() and target != Path(uploaded_file.name):
        stem, _, ext = safe_name.rpartition(".")
        n = 1
        while target.exists():
            target = base / f"{stem}_{n}.{ext}" if ext else base / f"{stem}_{n}"
            n += 1
    with open(target, "wb") as out:
        out.write(uploaded_file.read())
    return target


def _load_profile_fields(profile) -> Dict[str, Any]:
    """Mirror the profile's stored fields into a flat dict for the form."""

    def _bullets(items) -> str:
        """Render list-of-dicts (with 'text') or list-of-strs as newline text."""
        if not items:
            return ""
        lines = []
        for item in items:
            if isinstance(item, dict):
                lines.append(str(item.get("text", "")))
            else:
                lines.append(str(item))
        return "\n".join(x for x in lines if x)

    return {
        "id": profile.id,
        "name": profile.name or "",
        "email": profile.email or "",
        "phone": profile.phone or "",
        "linkedin": profile.linkedin or "",
        "website": profile.website or "",
        "summary": profile.summary or "",
        "skills": ", ".join(profile.skills),
        "certifications": ", ".join(profile.certifications),
        "achievements": ", ".join(profile.achievements),
        "experience_json": _bullets(profile.experience),
        "education_json": _bullets(profile.education),
        "projects_json": _bullets(profile.projects),
        "languages_json": _bullets(profile.languages),
        "profile_version": profile.profile_version,
        "cv_path": profile.cv_path or "",
        "active": bool(profile.active),
    }


def _split_list(text: str) -> List[str]:
    if not text:
        return []
    out = []
    for line in text.replace("\r", "\n").splitlines():
        for tok in line.split(","):
            t = tok.strip()
            if t:
                out.append(t)
    return out


# ---------------------------------------------------------------------------
# Upload + parse flow
# ---------------------------------------------------------------------------


def _handle_upload() -> None:
    st.subheader("Upload master CV")
    st.caption(
        "PDF or DOCX, up to 15 MB. The original file is never modified and is "
        "stored under `data/cv/`. Extraction is best-effort — please review "
        "the fields below before saving."
    )
    up = st.file_uploader(
        "Choose file",
        type=["pdf", "docx"],
        key="cv_upload",
    )
    if up is None:
        return
    try:
        size = len(up.getvalue())
    except Exception:
        size = 0
    if size > MAX_UPLOAD_BYTES:
        st.error("File too large (max 15 MB).")
        return
    # NOTE: Intentionally no page-level "Only .pdf/.docx" gate here.
    # The *parser* (cv_parser._suffix_is_supported) sniffs the actual
    # file contents (magic bytes) and will reject anything that isn't
    # really PDF/DOCX itself, with a clear error.  A page-level
    # extension check would wrongly reject files whose *names* happen
    # to end in e.g. `.extended_pdf` (very common from FlowCV export)
    # even when the file is a valid PDF.  Let the parser judge.

    saved = _save_upload(up)
    try:
        extraction = cv_parser.parse_cv(saved)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not parse file: {exc}")
        return

    conf = extraction.get("confidence", {}) or {}
    fields = extraction.get("fields", {}) or {}
    # Merge raw section text into well-known form fields.
    merged: Dict[str, Any] = {
        "name": fields.get("name") or "Unknown",
        "email": fields.get("email") or "",
        "phone": fields.get("phone") or "",
        "linkedin": fields.get("linkedin") or "",
        "website": fields.get("website") or "",
        "summary": fields.get("summary") or "",
        "skills": fields.get("_skills_raw") or "",
        "certifications": fields.get("_certifications_raw") or "",
        "achievements": fields.get("_achievements_raw") or "",
        "experience_json": fields.get("_experience_raw") or "",
        "education_json": fields.get("_education_raw") or "",
        "projects_json": fields.get("_projects_raw") or "",
        "languages_json": fields.get("_languages_raw") or "",
        "cv_path": str(saved),
        "profile_version": 1,
        "active": True,
    }

    if st.button("Import into profile form", type="primary", use_container_width=True):
        # Refresh flow: set cv_form AND invalidate every form widget key so
        # the auto-rerun rebuilt from the merged dict renders fresh values.
        st.session_state["cv_form"] = merged
        st.session_state["cv_imported_path"] = str(saved)
        st.session_state["cv_replacing_profile_id"] = None
        # Drop any retained widget values so the new `value=` shown is used.
        for k in (
            "f_name", "f_email", "f_phone", "f_site", "f_link", "f_cv",
            "f_summary", "f_skills", "f_cert", "f_ach", "f_langs",
            "f_exp", "f_edu", "f_proj",
        ):
            st.session_state.pop(k, None)
        st.toast("CV parsed — please review the form below before saving.")
        st.rerun()

    with st.expander("Extraction confidence", expanded=False):
        for k, v in conf.items():
            st.markdown(f"- **{k}**: {v}")


# ---------------------------------------------------------------------------
# Create / import flow
# ---------------------------------------------------------------------------


def _import_into_form(profile_id: Optional[int], payload: Dict[str, Any]) -> None:
    if profile_id:
        with util.with_session() as session:
            p = db.get_candidate_profile(session, profile_id)
        if p is None:
            st.error(f"Profile {profile_id} not found.")
            return
        st.session_state["cv_form"] = _load_profile_fields(p)
        st.session_state["cv_replacing_profile_id"] = p.id
        st.session_state["cv_imported_path"] = p.cv_path or ""
    else:
        st.session_state["cv_form"] = dict(payload)
        st.session_state["cv_replacing_profile_id"] = None
        st.session_state["cv_imported_path"] = payload.get("cv_path", "")


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def render() -> None:
    st.title("Candidate profile & CV")
    st.caption(
        "Manage the master CV and the editable candidate profile used by the "
        "AI matching engine and CV tailoring. Content changes bump "
        "`profile_version`, which invalidates cached match evaluations."
    )

    # ---- Upload section (always visible) ----
    _handle_upload()

    # ---- Existing profiles selector ----
    st.divider()
    profiles = _all_profiles()
    options = ["(new profile)"] + [f"Profile #{p.id} — {p.name or 'unnamed'}" for p in profiles]
    choice = st.selectbox("Load an existing profile into the form below", options, key="prof_pick")
    if choice != "(new profile)":
        # Parse 'Profile #3 — John'
        try:
            pid = int(choice.split("#")[1].split("—")[0].strip())
        except Exception:
            pid = None
        if pid and "cv_form" not in st.session_state:
            _import_into_form(pid, {})

    form: Dict[str, Any] = st.session_state.get("cv_form") or {
        "id": None,
        "name": "",
        "email": "",
        "phone": "",
        "linkedin": "",
        "website": "",
        "summary": "",
        "skills": "",
        "certifications": "",
        "achievements": "",
        "experience_json": "",
        "education_json": "",
        "projects_json": "",
        "languages_json": "",
        "profile_version": 1,
        "cv_path": st.session_state.get("cv_imported_path", ""),
        "active": True,
    }

    # ---- Identifying info ----
    st.subheader("Contact")
    row = st.columns(4)
    form["name"] = row[0].text_input("Full name", value=form.get("name", ""), key="f_name")
    form["email"] = row[1].text_input("Email", value=form.get("email", ""), key="f_email")
    form["phone"] = row[2].text_input("Phone", value=form.get("phone", ""), key="f_phone")
    form["website"] = row[3].text_input("Website", value=form.get("website", ""), key="f_site")
    row2 = st.columns(2)
    form["linkedin"] = row2[0].text_input("LinkedIn", value=form.get("linkedin", ""), key="f_link")
    form["cv_path"] = row2[1].text_input("CV file (local)", value=form.get("cv_path", ""), key="f_cv")

    # ---- Summary ----
    st.subheader("Summary")
    form["summary"] = st.text_area(
        "Short professional summary",
        value=form.get("summary", ""),
        height=120,
        key="f_summary",
    )

    # ---- Skills / certifications / achievements ----
    st.subheader("Skills, certifications, achievements")
    col_a, col_b = st.columns(2)
    with col_a:
        form["skills"] = st.text_area(
            "Skills (comma separated)", value=form.get("skills", ""), height=100, key="f_skills"
        )
        form["certifications"] = st.text_area(
            "Certifications (comma separated)",
            value=form.get("certifications", ""),
            height=100,
            key="f_cert",
        )
    with col_b:
        form["achievements"] = st.text_area(
            "Key achievements / highlights (comma separated)",
            value=form.get("achievements", ""),
            height=200,
            key="f_ach",
        )
        form["languages_json"] = st.text_area(
            "Languages — one per line, 'Language — level' format",
            value=form.get("languages_json", ""),
            height=100,
            key="f_langs",
        )

    # ---- Structured sections (raw text, parsed on save into lists) ----
    st.subheader("Experience")
    form["experience_json"] = st.text_area(
        "Experience (one bullet per line)",
        value=form.get("experience_json", ""),
        height=200,
        key="f_exp",
        help="Stored as a list of dicts on save: each line becomes an entry.",
    )

    st.subheader("Education")
    form["education_json"] = st.text_area(
        "Education (one entry per line)",
        value=form.get("education_json", ""),
        height=120,
        key="f_edu",
    )

    st.subheader("Projects")
    form["projects_json"] = st.text_area(
        "Projects (one entry per line)",
        value=form.get("projects_json", ""),
        height=120,
        key="f_proj",
    )

    # ---- Footer actions ----
    st.divider()
    k1, k2, k3, k4 = st.columns(4)
    active_choice = k1.selectbox(
        "Active profile?",
        options=[True, False],
        index=0 if form.get("active", True) else 1,
        format_func=lambda v: "Yes (active)" if v else "No (inactive)",
        key="f_active",
    )
    if k2.button("Reset form", use_container_width=True):
        st.session_state["cv_form"] = {
            "id": None,
            "name": "",
            "email": "",
            "phone": "",
            "linkedin": "",
            "website": "",
            "summary": "",
            "skills": "",
            "certifications": "",
            "achievements": "",
            "experience_json": "",
            "education_json": "",
            "projects_json": "",
            "languages_json": "",
            "profile_version": 1,
            "cv_path": "",
            "active": True,
        }
        st.rerun()
    if k3.button("Save profile", type="primary", use_container_width=True):
        _do_save(form, active=active_choice)
    if k4.button("Delete profile", use_container_width=True,
                 disabled=form.get("id") is None):
        _do_deactivate(form)

    if "cv_save_flash" in st.session_state:
        st.success(st.session_state.pop("cv_save_flash"))
    if "cv_err" in st.session_state:
        st.error(st.session_state.pop("cv_err"))

    st.caption(
        f"Current profile version: **{form.get('profile_version', 1)}** — "
        "saving with content changes bumps this by 1 and invalidates cached "
        "Gemini match evaluations for the old version."
    )


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def _lines_to_bullets(text: str) -> List[Dict[str, str]]:
    """Convert a multi-line textarea into a list of {text: ...} dicts."""
    out: List[Dict[str, str]] = []
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if ln:
            out.append({"text": ln})
    return out


def _do_save(form: Dict[str, Any], *, active: bool) -> None:
    name = (form.get("name") or "").strip()
    if not name:
        st.session_state["cv_err"] = "Name is required."
        st.rerun()
    skills = _split_list(form.get("skills") or "")
    certs = _split_list(form.get("certifications") or "")
    ach = _split_list(form.get("achievements") or "")
    edus = _lines_to_bullets(form.get("education_json") or "")
    exps = _lines_to_bullets(form.get("experience_json") or "")
    projs = _lines_to_bullets(form.get("projects_json") or "")

    existing_id = form.get("id")
    try:
        if existing_id is None:
            p = cp.upsert_from_extraction(
                {
                    "name": name,
                    "email": form.get("email") or None,
                    "phone": form.get("phone") or None,
                    "linked_in": None,
                    "linkedin": form.get("linkedin") or None,
                    "website": form.get("website") or None,
                    "summary": form.get("summary") or None,
                    "skills": skills,
                    "education": edus,
                    "experience": exps,
                    "languages": [],
                    "certifications": certs,
                    "achievements": ach,
                    "projects": projs,
                    "cv_path": form.get("cv_path") or None,
                    "active": active,
                }
            )
            st.session_state["cv_form"] = _load_profile_fields(p)
            st.session_state["cv_form"]["active"] = active
            st.session_state["cv_save_flash"] = (
                f"Created profile #{p.id} (v{p.profile_version})."
            )
        else:
            p = cp.upsert_profile(
                existing_id,
                name=name,
                email=form.get("email") or None,
                phone=form.get("phone") or None,
                linkedin=form.get("linkedin") or None,
                website=form.get("website") or None,
                summary=form.get("summary") or None,
                skills=skills,
                education=edus,
                experience=exps,
                languages=[],
                certifications=certs,
                achievements=ach,
                projects=projs,
                cv_path=form.get("cv_path") or None,
                active=active,
            )
            st.session_state["cv_form"] = _load_profile_fields(p)
            st.session_state["cv_form"]["active"] = active
            st.session_state["cv_save_flash"] = (
                f"Saved profile #{p.id} (v{p.profile_version})."
            )
    except Exception as exc:  # noqa: BLE001
        st.session_state["cv_err"] = f"Could not save profile: {exc}"
    st.rerun()


def _do_deactivate(form: Dict[str, Any]) -> None:
    pid = form.get("id")
    if pid is None:
        return
    try:
        p = cp.upsert_profile(pid, active=False)
        st.session_state["cv_form"] = _load_profile_fields(p)
        st.session_state["cv_save_flash"] = (
            f"Profile #{p.id} marked inactive (kept in DB)."
        )
    except Exception as exc:  # noqa: BLE001
        st.session_state["cv_err"] = f"Could not deactivate profile: {exc}"
    st.rerun()

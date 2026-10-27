"""Job detail page.

Renders a single job by id (from `st.session_state["selected_job_id"]`)
with full description, match breakdown, application links, and document
actions (CV suggestions + cover letter).
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import streamlit as st

from database import repository as db
from ui import util


# ---------------------------------------------------------------------------
# Top-level render
# ---------------------------------------------------------------------------


def render() -> None:
    st.title("Job")

    # ---- Job selector dropdown ----------------------------------------
    all_jobs = util.list_jobs_with_match(limit=200)
    if not all_jobs:
        st.info("No jobs found. Run a scan from the Overview page first.")
        return

    job_labels = [
        f"{r['id']}. {r['title']} — {r['company']} ({r['location'] or 'n/a'})"
        for r in all_jobs
    ]
    job_ids = [r["id"] for r in all_jobs]

    # Determine the index of the currently selected job (default: 0)
    current_job_id = st.session_state.get("selected_job_id")
    if current_job_id in job_ids:
        select_index = job_ids.index(current_job_id)
    else:
        select_index = 0

    # Use an integer index directly so duplicate labels can't cause ambiguity.
    sel_idx = st.selectbox(
        "Select a job",
        options=list(range(len(job_labels))),
        format_func=lambda i: job_labels[i],
        index=select_index,
        key="detail_job_select",
    )
    # Map the selected index straight to a job_id; if it differs, update and rerun
    new_job_id = job_ids[sel_idx]
    if st.session_state.get("selected_job_id") != new_job_id:
        st.session_state["selected_job_id"] = new_job_id
        st.rerun()

    # ---- Resolve the selected job id ---------------------------------
    job_id = new_job_id

    with util.with_session() as session:
        job = db.get_job(session, job_id)
        if job is None:
            st.error(f"Job {job_id} not found.")
            return

        active_profile = db.get_active_candidate_profile(session)
        if active_profile:
            match = db.get_match_for_job(
                session, job.id, profile_version=active_profile.profile_version
            )
        else:
            match = db.get_match_for_job(session, job.id)
        app_row = db.get_application_state(session, job.id)

        # Eagerly load every attribute before leaving the session scope
        job_data = util.job_to_dict(job)
        match_data = _serialize_match(match)
        job_description = job.description or ""
        required_skills = list(job.required_skills or [])
        preferred_skills = list(job.preferred_skills or [])
        education_req = job.education_requirements or ""
        experience_req = job.experience_requirements or ""
        language_req = job.language_requirements or ""
        application_row = util.application_row(app_row, job) if app_row else None

    if not job_data:
        st.error("No data.")
        return

    # ---- Header ----------------------------------------------------------
    h_l, h_r = st.columns([3, 1])
    with h_l:
        st.markdown(f"## {job_data['title']}")
        st.caption(
            f"**{job_data['company']}** · "
            f"{job_data['location'] or 'n/a'} ({job_data['country'] or 'n/a'})"
        )
    with h_r:
        score = match_data.get("match_score") if match_data else None
        if score is not None:
            _score_header(score)
        else:
            st.info("No match yet")

    # ---- Compact chips ----------------------------------------------------
    with st.container(border=True):
        tags = (
            [
                job_data.get("employment_type", ""),
                job_data.get("workplace_arrangement", ""),
                job_data.get("status", ""),
            ]
            + (required_skills or [])[:5]
        )
        tags = [t for t in tags if t]
        if tags:
            st.markdown(" ".join(f"`{t}`" for t in tags))

    # ---- Tabs ------------------------------------------------------------
    tab_desc, tab_score, tab_apply, tab_files = st.tabs(
        ["Description", "Match", "Application", "Documents"]
    )
    with tab_desc:
        _render_description(
            job_data,
            job_description,
            required_skills,
            preferred_skills,
            education_req,
            experience_req,
            language_req,
        )
    with tab_score:
        _render_match(match_data)
    with tab_apply:
        _render_apply(job_data, application_row, job_id)
    with tab_files:
        _render_documents(job_id)

    # ---- Navigation -------------------------------------------------------
    st.divider()
    col_a, col_b = st.columns(2)
    with col_a:
        if st.button("Back to Jobs", use_container_width=True):
            st.session_state.pop("selected_job_id", None)
            st.rerun()
    with col_b:
        if st.button("Back to Overview", use_container_width=True):
            st.session_state.pop("selected_job_id", None)
            st.rerun()


# ---------------------------------------------------------------------------
# Sub-renderers
# ---------------------------------------------------------------------------


def _score_header(score: float) -> None:
    pct = max(0.0, min(100.0, float(score)))
    if pct >= 80:
        label = "Strong match"
    elif pct >= 60:
        label = "Decent match"
    else:
        label = "Weak match"
    st.metric(label=f"Match — {label}", value=f"{pct:.0f}/100")


def _render_description(
    job_data: Dict[str, Any],
    job_description: str,
    required: List[str],
    preferred: List[str],
    education_req: str,
    experience_req: str,
    language_req: str,
) -> None:
    # ---- Key facts panel -------------------------------------------------
    facts = [
        {"Field": "Published", "Value": job_data.get("published_at") or "n/a"},
        {"Field": "Source", "Value": job_data.get("source") or "n/a"},
        {"Field": "Discovered", "Value": job_data.get("first_discovered") or "n/a"},
        {"Field": "Last seen", "Value": job_data.get("last_seen") or "n/a"},
    ]
    if job_data.get("salary"):
        facts.append({"Field": "Salary", "Value": job_data["salary"]})
    with st.container(border=True):
        st.dataframe(facts, hide_index=True, use_container_width=True)

    # ---- Job description -------------------------------------------------
    st.subheader("Job Description")
    desc = (job_description or "").strip()
    if not desc:
        st.info("No description captured for this job.")
    else:
        st.markdown(desc)

    # ---- Skills -----------------------------------------------------------
    st.subheader("Skills")
    col_a, col_b = st.columns(2)
    with col_a:
        st.markdown("**Required**")
        if required:
            for s in required:
                st.markdown(f"- {s}")
        else:
            st.caption("No required skills captured.")
    with col_b:
        st.markdown("**Preferred**")
        if preferred:
            for s in preferred:
                st.markdown(f"- {s}")
        else:
            st.caption("No preferred skills captured.")

    # ---- Additional requirements -----------------------------------------
    has_reqs = any([education_req, experience_req, language_req])
    if has_reqs:
        st.subheader("Requirements")
        if education_req:
            st.markdown(f"**Education:** {education_req}")
        if experience_req:
            st.markdown(f"**Experience:** {experience_req}")
        if language_req:
            st.markdown(f"**Languages:** {language_req}")

    # ---- Original posting ---------------------------------------------
    orig = job_data.get("original_url") or ""
    app_url = job_data.get("application_url") or ""
    links = []
    if orig:
        links.append(("View original posting", orig))
    if app_url and app_url != orig:
        links.append(("Apply directly", app_url))
    for label, url in links:
        st.markdown(f"[{label}]({url})")
    if not links:
        st.caption("No direct link captured for this job.")


def _render_match(match_data: Optional[Dict[str, Any]]) -> None:
    if not match_data:
        st.info("No match computed yet — run the scan first.")
        return

    pct = max(0.0, min(100.0, float(match_data["match_score"])))
    if pct >= 80:
        label = "Strong match"
    elif pct >= 60:
        label = "Moderate match"
    else:
        label = "Weak match"

    with st.container(border=True):
        st.metric(
            label="Match Score",
            value=f"{pct:.0f}/100",
            help=(
                f"Evaluated at {match_data.get('match_evaluated_at') or 'unknown'} · "
                f"model: {match_data.get('match_ai_model') or '—'} · "
                f"profile version: {match_data.get('match_profile_version') or '—'}"
            ),
        )
        st.caption(f"Assessment: {label}")

    breakdown = match_data.get("breakdown") or {}
    if breakdown:
        st.subheader("Score Breakdown")
        for key, value in breakdown.items():
            st.markdown(f"- **{key}**: {value}")

    evidence = match_data.get("evidence") or []
    if evidence:
        st.subheader("Why?")
        for item in evidence:
            st.markdown(f"- {item}")
    else:
        st.caption("No evidence captured.")

    missing = match_data.get("missing") or []
    if missing:
        st.subheader("What's missing?")
        for item in missing:
            st.markdown(f"- {item}")


def _render_apply(
    job_data: Dict[str, Any],
    application_row: Optional[Dict[str, Any]],
    job_id: int,
) -> None:
    # Ensure an application row exists
    with util.with_session() as session:
        app = db.get_application_state(session, job_id)
        if app is None:
            app = db.create_application(session, job_id=job_id)

    st.subheader("Status")
    if application_row:
        st.markdown(
            f"**Current:** {application_row.get('status_label')}  "
            f"(shortlisted {application_row.get('shortlisted_at') or '—'} · "
            f"applied {application_row.get('applied_at') or '—'} · "
            f"follow-up {application_row.get('follow_up_at') or '—'})"
        )
    else:
        st.caption("No application row yet for this job.")

    st.divider()
    lcol, mcol, rcol = st.columns(3)
    with lcol:
        if st.button("Mark as Shortlisted", use_container_width=True):
            with util.with_session() as session:
                a = db.get_application_state(session, job_id) or db.create_application(
                    session, job_id=job_id
                )
                db.mark_shortlisted(session, a)
            st.rerun()
    with mcol:
        if st.button("Mark as Applied", use_container_width=True):
            with util.with_session() as session:
                a = db.get_application_state(session, job_id) or db.create_application(
                    session, job_id=job_id
                )
                try:
                    db.mark_applied(session, a, via="manual")
                    st.toast("Marked as Applied")
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Could not mark applied: {exc}")
            st.rerun()
    with rcol:
        if application_row:
            current = application_row.get("app_status", "")
            if current in {"DISCOVERED", "SHORTLISTED", "PREPARED", "APPROVED"}:
                if st.button("Mark as Rejected", use_container_width=True):
                    with util.with_session() as session:
                        a = db.get_application_state(session, job_id)
                        if a is not None:
                            db.transition_application_status(session, a, "REJECTED")
                    st.rerun()

    if application_row and application_row.get("app_status") in {"APPLIED", "INTERVIEW"}:
        with st.container(border=True):
            current = application_row["app_status"]
            options = ["INTERVIEW", "APPLIED", "REJECTED", "WITHDRAWN"]
            idx = options.index(current) if current in options else 0
            new_status = st.selectbox(
                "Advanced status", options, index=idx, key=f"adv_status_{job_id}"
            )
            if st.button("Update Status", use_container_width=True):
                with util.with_session() as session:
                    a = db.get_application_state(session, job_id)
                    if a is not None and a.status != new_status:
                        if new_status == "APPLIED":
                            try:
                                db.mark_applied(session, a, via="manual")
                            except Exception:
                                st.toast("Already applied; status unchanged.")
                        else:
                            db.transition_application_status(session, a, new_status)
                st.rerun()

    st.subheader("Notes & follow-ups")
    note = ""
    with util.with_session() as session:
        a_row = db.get_application_state(session, job_id)
        if a_row is not None:
            note = a_row.notes or ""

    new_note = st.text_area("Notes", value=note, height=80, key=f"note_{job_id}")
    fu = st.date_input("Follow-up date", value=None, key=f"fu_{job_id}")
    if st.button("Save notes & follow-up", use_container_width=True):
        with util.with_session() as session:
            a = db.get_application_state(session, job_id)
            if a is not None:
                if (a.notes or "") != new_note:
                    a.notes = new_note.strip()
                if fu is not None:
                    from datetime import datetime, timezone

                    fu_dt = datetime(fu.year, fu.month, fu.day, tzinfo=timezone.utc)
                    a.follow_up_at = fu_dt
                else:
                    a.follow_up_at = None
                session.commit()
            st.toast("Saved.")
        st.rerun()

    st.divider()
    st.subheader("Apply")
    if job_data.get("application_url"):
        st.markdown(f"[Open application URL]({job_data['application_url']})")
    elif job_data.get("original_url"):
        st.markdown(f"[Open original URL]({job_data['original_url']})")
    else:
        st.caption("No application link captured. Open the original listing manually.")


def _render_documents(job_id: int) -> None:
    active_profile = util.load_active_profile()
    if active_profile is None:
        st.warning(
            "No active candidate profile yet. Add one on the Candidate Profile page."
        )
        return

    cv_col, cl_col = st.columns(2)
    with cv_col:
        if st.button("Generate CV Suggestions", type="primary"):
            _generate_cv_suggestions(job_id, active_profile)
    with cl_col:
        if st.button("Generate Cover Letter", type="secondary"):
            _generate_cover_letter(job_id, active_profile)

    docs = util.list_documents_for_job(job_id)
    if docs:
        st.subheader("Generated documents")
        for d in docs:
            label = (
                f"{d['doc_type']} — {d['created_at']} · "
                f"v{d['version']} · {'✓ approved' if d['approved'] else 'draft'}"
            )
            with st.expander(label):
                content = _read_local(d["file_path"])
                if content:
                    st.code(content, language="markdown")
                else:
                    st.caption("(empty or unreadable file)")
                from pathlib import Path

                raw = Path(d["file_path"])
                if raw.exists():
                    st.download_button(
                        "Download",
                        data=raw.read_bytes(),
                        file_name=raw.name,
                        mime=_mime_for(raw.suffix),
                    )


# ---------------------------------------------------------------------------
# Document generation
# ---------------------------------------------------------------------------


def _generate_cv_suggestions(job_id: int, profile) -> None:
    """Use `services.cv_tailoring.build_cv_tailoring_suggestions` (deterministic
    or Gemini-assisted) to produce an actionable list of CV edits and persist
    it as a `.txt` document."""
    from services.cv_tailoring import build_cv_tailoring_suggestions
    from services.gemini_client import deterministic_score

    with util.with_session() as session:
        job = db.get_job(session, job_id)
        if job is None:
            st.error("Job vanished during generation.")
            return
        # Build a lightweight match dict for the tailoring suggestion logic
        match_data = deterministic_score(profile, job)

    suggestions = build_cv_tailoring_suggestions(profile, _fresh_session_job(job_id), match_data)

    out_dir = util.get_output_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"cv_suggestions_{job_id}.txt"

    lines = [
        f"# CV Tailoring Suggestions — Job {job_id}",
        "",
        f"Generated from profile v{profile.profile_version}",
        "",
    ]
    for idx, s in enumerate(suggestions, start=1):
        lines.append(f"{idx}. **{s.get('field', 'general')}**")
        lines.append(f"   - {s.get('action', '')}")
        lines.append(f"   - Why: {s.get('reason', '')}")
        lines.append("")
    out_path.write_text("\n".join(lines), encoding="utf-8")

    with util.with_session() as session:
        db.create_generated_document(
            session,
            job_id=job_id,
            doc_type="cv_suggestions",
            file_path=str(out_path),
            profile_version=profile.profile_version,
            notes="auto-generated by cv_tailoring",
        )
    st.toast("CV suggestions written.")
    st.rerun()


def _fresh_session_job(job_id: int):
    """Return a detached Job for out-of-session use (for doc generation)."""
    with util.with_session() as session:
        return db.get_job(session, job_id)


def _generate_cover_letter(job_id: int, profile) -> None:
    from services.cover_letter import generate_cover_letter
    from services.gemini_client import deterministic_score

    job = _fresh_session_job(job_id)
    match_data = deterministic_score(profile, job)

    tone = st.selectbox("Tone", ["professional", "casual", "confident"], index=0, key=f"tone_{job_id}")
    content = generate_cover_letter(profile, job, match_data, tone=tone)

    out_dir = util.get_output_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"cover_letter_{job_id}.md"
    out_path.write_text(content, encoding="utf-8")

    with util.with_session() as session:
        db.create_generated_document(
            session,
            job_id=job_id,
            doc_type="cover_letter",
            file_path=str(out_path),
            profile_version=profile.profile_version,
            notes=f"generated with tone={tone}",
        )
    st.toast("Cover letter written.")
    st.rerun()


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _serialize_match(match) -> Optional[Dict[str, Any]]:
    if match is None:
        return None
    out: Dict[str, Any] = {
        "match_score": getattr(match, "match_score", None),
        "match_ai_model": getattr(match, "ai_model", None) or "",
        "match_evaluated_at": util.dt_short(match.evaluated_at)
        if match.evaluated_at
        else "",
        "match_profile_version": getattr(match, "profile_version", None),
        "breakdown": _json_get(match.score_json),
        "evidence": _json_get(match.matching_evidence_json) or [],
        "missing": _json_get(match.missing_requirements_json) or [],
    }
    return out


def _json_get(raw: Optional[str]) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return None


def _read_local(path: str) -> str:
    from pathlib import Path

    p = Path(path)
    try:
        return p.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return ""


def _mime_for(suffix: str) -> str:
    m = {
        ".md": "text/markdown",
        ".txt": "text/plain",
        ".docx": (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        ".pdf": "application/pdf",
    }
    return m.get(suffix, "application/octet-stream")

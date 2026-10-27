"""Application Tracker page.

- Table of all applications joined with their job.
- Per-row actions: shortlist, mark prepared, approve, mark applied,
  transition to interview / rejected / withdrawn, set follow-up, notes.

All destructive or consequential transitions are explicit button clicks.
Marking "Applied" is an explicit human confirmation (per spec section 14).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import streamlit as st

from database import repository as db
from ui import util

# Status -> next forward transitions allowed (manual "role" / workflow)
NEXT_STATUSES: Dict[str, List[str]] = {
    "DISCOVERED": ["SHORTLISTED"],
    "SHORTLISTED": ["PREPARED", "APPROVED"],
    "PREPARED": ["APPROVED"],
    "APPROVED": ["APPLIED"],
    "APPLIED": ["INTERVIEW"],
}

# Terminal / optional transitions
TERMINAL = ["REJECTED", "WITHDRAWN"]
INTRO_TEXT = "Track every job through its application lifecycle. " \
             "Each transition is an explicit action — nothing is inferred."


def render() -> None:
    st.title("Application Tracker")
    st.caption(INTRO_TEXT)

    rows = util.list_applications_with_jobs()
    if not rows:
        st.info("No applications yet. Shortlist a job from the Job Discovery page to begin.")
        _render_followups()
        return

    # --- Filters ---
    f1, f2, f3 = st.columns(3)
    with f1:
        chosen_status = f1.selectbox(
            "Filter by status",
            options=["(all)"] + [s for s in util.APP_STATUSES],
            key="app_filter_status",
        )
    with f2:
        q = f2.text_input("Search (title / company)", key="app_search")
    with f3:
        only_followups = f3.checkbox("Only with follow-up set", key="only_followups")

    filtered: List[Dict[str, Any]] = []
    for r in rows:
        if chosen_status != "(all)" and r.get("app_status") != chosen_status:
            continue
        if q:
            blob = f"{r.get('title', '')} {r.get('company', '')}".lower()
            if q.lower() not in blob:
                continue
        if only_followups and not r.get("follow_up_at"):
            continue
        filtered.append(r)

    if not filtered:
        st.caption("No applications match the filters.")
        _render_followups()
        return

    # --- Summary metrics ---
    m1, m2, m3, m4, m5, m6 = st.columns(6)
    count_of = lambda s: sum(1 for r in filtered if r.get("app_status") == s)  # noqa: E731
    m1.metric("Total", len(filtered))
    m2.metric("Shortlisted", count_of("SHORTLISTED"))
    m3.metric("Prepared", count_of("PREPARED") + count_of("APPROVED"))
    m4.metric("Applied", count_of("APPLIED"))
    m5.metric("Interview", count_of("INTERVIEW"))
    m6.metric("Closed", count_of("REJECTED") + count_of("WITHDRAWN"))

    # --- Table (compact) ---
    display = [
        {
            "Id": r.get("app_id"),
            "Job": r.get("title", "")[:36],
            "Company": r.get("company", "")[:24],
            "Status": r.get("status_label", ""),
            "Shortlisted": r.get("shortlisted_at", ""),
            "Applied": r.get("applied_at", ""),
            "Follow-up": r.get("follow_up_at", ""),
            "Interview": r.get("interview_status", "")[:18],
        }
        for r in filtered
    ]
    st.dataframe(display, use_container_width=True, hide_index=True)

    st.markdown("### Row actions")
    _render_rows_actions(filtered)
    _render_followups()


# ---------------------------------------------------------------------------
# Per-row action editor
# ---------------------------------------------------------------------------

def _render_rows_actions(rows: List[Dict[str, Any]]) -> None:
    # One expander per row keeps the page manageable.
    for r in rows:
        app_id = r.get("app_id")
        if app_id is None:
            continue
        job_id = r.get("id")
        label = f"#{app_id} — {r.get('company', '?')} · {r.get('title', '?')}"
        with st.expander(f"{label}   (status: {r.get('status_label', r.get('app_status'))})"):
            _render_row_actions(app_id=app_id, job_id=job_id, row=r)


def _render_row_actions(app_id: int, job_id: int, row: Dict[str, Any]) -> None:
    with util.with_session() as session:
        app = (
            session.query(__import__("database.models", fromlist=["Application"]).Application)
            .filter_by(id=app_id).first()
        )
        if app is None:
            st.error("Application row missing from DB.")
            return
        current = app.status

        # ---- Status-transition buttons ---------------------------------
        c1, c2, c3, c4, c5, c6 = st.columns(6)
        transitions: Dict[str, str] = {}
        for opt in NEXT_STATUSES.get(current, []):
            transitions["Move to " + util.STATUS_LABEL.get(opt, opt)] = opt
        for opt in TERMINAL:
            if opt != current:
                transitions["Mark " + util.STATUS_LABEL.get(opt, opt)] = opt

        idx = 0
        for label, target in list(transitions.items())[:5]:  # cap buttons
            col = c1 if idx == 0 else (c2 if idx == 1 else (c3 if idx == 2 else (c4 if idx == 3 else c5)))
            if idx < 5 and col.button(label, key=f"app_a_{app_id}_{target}", use_container_width=True):
                _perform(app_id=app_id, job_id=job_id, transition=target)
                return
            idx += 1

        # Rejected / withdrawn if they did not fit above
        for label, target in list(transitions.items())[5:]:
            if st.button(label, key=f"app_a_{app_id}_{target}", use_container_width=True):
                _perform(app_id=app_id, job_id=job_id, transition=target)
                return

        # ---- Enabled at any stage --------------------------------------
        st.divider()
        l_so, l_no, l_fu = st.columns(3)
        with l_so:
            if st.button("Shortlist", key=f"ap_sh_{app_id}", use_container_width=True):
                _perform(app_id=app_id, job_id=job_id, transition="SHORTLISTED", force=True)
        with l_no:
            if st.button("Mark Docs Prepared", key=f"ap_dp_{app_id}", use_container_width=True):
                _perform(app_id=app_id, job_id=job_id, transition="PREPARED")
        with l_fu:
            if st.button("Approve", key=f"ap_ap_{app_id}", use_container_width=True):
                _perform(app_id=app_id, job_id=job_id, transition="APPROVED")

        # Mark as applied: explicit confirmation, per spec 14
        st.divider()
        if current in {"PREPARED", "APPROVED"}:
            ok = st.checkbox(
                "I have personally confirmed the application was submitted",
                key=f"ap_confirm_{app_id}",
            )
            if st.button("CONFIRM Mark as Applied", key=f"ap_confirmbtn_{app_id}",
                         type="primary", use_container_width=True, disabled=not ok):
                _perform(app_id=app_id, job_id=job_id, transition="APPLIED")

        # ---- Follow-up date + interview status --------------------------
        st.divider()
        f1, f2 = st.columns(2)
        with f1:
            fu = st.date_input(
                "Follow-up date",
                value=datetime.now() + timedelta(days=14),
                key=f"fu_date_{app_id}",
            )
            if st.button("Set Follow-up", key=f"fu_set_{app_id}", use_container_width=True):
                _perform(app_id=app_id, job_id=job_id, follow_up=fu)
        with f2:
            ist = st.text_input(
                "Interview status",
                value=row.get("interview_status", "") or "Not Started",
                key=f"ist_{app_id}",
            )
            if st.button("Update Interview Status", key=f"ist_set_{app_id}", use_container_width=True):
                _perform(app_id=app_id, job_id=job_id, interview=ist)

        # ---- Notes ------------------------------------------------------
        st.divider()
        note = st.text_area("Add a note (prepended)", key=f"nta_{app_id}", height=90)
        if st.button("Save Note", key=f"nt_save_{app_id}", use_container_width=True):
            if note.strip():
                _perform(app_id=app_id, job_id=job_id, note=note.strip())


def _perform(
    app_id: int,
    job_id: Optional[int] = None,
    transition: Optional[str] = None,
    follow_up: Optional[datetime] = None,
    interview: Optional[str] = None,
    note: Optional[str] = None,
    force: bool = False,
) -> None:
    """Apply one or more changes to the application row in a single session."""
    from database.models import Application  # local import avoids module coupling

    with util.with_session() as session:
        app = session.query(Application).filter_by(id=app_id).first()
        if app is None:
            st.error("Application not found.")
            return
        if transition:
            if force and transition == "SHORTLISTED":
                db.mark_shortlisted(session, app)
            elif transition == "PREPARED":
                db.mark_docs_prepared(session, app)
            elif transition == "APPROVED":
                db.mark_approved(session, app)
            elif transition == "APPLIED":
                try:
                    db.mark_applied(session, app, via="manual")
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Could not mark applied: {exc}")
                    return
            else:
                try:
                    db.transition_application_status(session, app, transition)
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Could not transition to {transition}: {exc}")
                    return
        if follow_up is not None:
            try:
                db.set_follow_up(session, app, follow_up)
            except Exception as exc:  # noqa: BLE001
                st.error(f"Could not set follow-up: {exc}")
        if interview is not None:
            try:
                db.set_interview_status(session, app, interview)
            except Exception as exc:  # noqa: BLE001
                st.error(f"Could not set interview status: {exc}")
        if note:
            try:
                db.add_note(session, app, note)
            except Exception as exc:  # noqa: BLE001
                st.error(f"Could not save note: {exc}")
    st.rerun()


# ---------------------------------------------------------------------------
# Follow-ups panel (also shown on empty state)
# ---------------------------------------------------------------------------

def _render_followups() -> None:
    st.markdown("### Upcoming follow-ups (next 14 days)")
    ups = util.upcoming_followups(window_days=14)
    if not ups:
        st.caption("Nothing scheduled.")
        return
    st.dataframe(
        [
            {
                "Job": f"{x.get('company')} — {x.get('title')}"[:60],
                "Follow-up": x.get("follow_up_at", ""),
                "Status": x.get("status_label", ""),
                "Interview": x.get("interview_status", ""),
            }
            for x in ups
        ],
        use_container_width=True,
        hide_index=True,
    )

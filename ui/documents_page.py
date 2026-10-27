"""Generated Documents page.

Lists every CV and cover letter generated per job. Downloads via
`st.download_button` using the file path stored on disk.
Each document is versioned against the profile_version at creation time.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List

import streamlit as st

from database import repository as db
from ui import util


def render() -> None:
    st.title("Generated Documents")
    st.caption("All CVs and cover letters produced by the pipeline, per job.")

    with util.with_session() as session:
        docs = db.get_doc_list(session)
        jobs = {j.id: j for j in session.query(__import__("database.models", fromlist=["Job"]).Job).all()}

    if not docs:
        st.info(
            "Nothing generated yet. Open a job from the Job detail page and "
            "use the Document Actions to produce a CV / cover letter."
        )
        return

    # Group by job -> per-job section
    by_job: Dict[int, List] = {}
    for d in docs:
        by_job.setdefault(d.job_id, []).append(d)

    # Optional: filter by doc_type
    f1, f2 = st.columns(2)
    doc_filter = f1.selectbox(
        "Document type", options=["(all)", "CV", "COVER_LETTER"], key="doc_filter"
    )
    _ = f2

    for job_id, group in sorted(by_job.items(), key=lambda kv: kv[0], reverse=True):
        job = jobs.get(job_id)
        job_label = (
            f"{job.company} — {job.title}" if job is not None else f"Job #{job_id} (not in DB)"
        )
        with st.expander(f"📎 {job_label[:80]}  ({len(group)} doc(s))"):
            if job is not None:
                st.caption(
                    f"Location: {job.location or '—'}   "
                    f"Type: {job.employment_type}   "
                    f"Application: {job.application_url or '—'}"
                )
            for d in sorted(group, key=lambda x: x.id, reverse=True):
                if doc_filter != "(all)" and d.doc_type != doc_filter:
                    continue
                _render_doc_row(d, job)


def _render_doc_row(d, job) -> None:
    cols = st.columns([2, 1, 1, 3, 1.5])
    with cols[0]:
        st.markdown(f"**{d.doc_type}** v{d.profile_version}")
        st.caption(util.dt_local(d.created_at))
    with cols[1]:
        st.caption(f"Approved: {'✅' if d.approved else '—'}")
    with cols[2]:
        if st.button("Approve", key=f"doc_ap_{d.id}", disabled=d.approved):
            with util.with_session() as session:
                db.mark_doc_approved(session, d.id)
            st.rerun()
    with cols[3]:
        _download_button(d)
    with cols[4]:
        if d.notes:
            st.caption(d.notes[:60])


def _download_button(d) -> None:
    """Build the st.download_button payload. Missing file -> friendly error."""
    fp = Path(d.file_path) if d.file_path else None
    if fp is None or not fp.exists():
        st.caption("⚠ File not found on disk. Regenerate from the Job page.")
        return
    if fp.suffix.lower() in {".pdf", ".png", ".jpg", ".jpeg"}:
        # Streamlit can only download binary from memory; show inline preview
        st.download_button(
            label=f"Download {fp.name}",
            data=fp.read_bytes(),
            file_name=fp.name,
            mime=_mime(fp),
            key=f"dl_{d.id}",
        )
    else:
        # DOCX or other text-formatted files: serve the bytes raw
        st.download_button(
            label=f"Download {fp.name}",
            data=fp.read_bytes(),
            file_name=fp.name,
            mime=_mime(fp),
            key=f"dl_{d.id}",
        )


def _mime(fp: Path) -> str:
    suffix = fp.suffix.lower()
    return {
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".pdf": "application/pdf",
        ".txt": "text/plain",
        ".md": "text/markdown",
    }.get(suffix, "application/octet-stream")

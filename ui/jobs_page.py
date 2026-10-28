"""Job discovery / job table page.

Layout:
- Filters: source, employment type, workplace, min match score, status,
  'new since last scan' toggle, text search box.
- Sorted table (id, title, company, location, workplace, type, score, published, source).
- Click a row -> jump to the detail page (via st.session_state.job_id).
"""
from __future__ import annotations

from typing import Dict, List, Optional

import streamlit as st

from config.search_config import EmploymentType
from config.runtime_settings import load_search_config
from config.search_intent import build_intent
from services.title_relevance import classify_title
from services.location_validation import parse_location, location_verdict
from ui import util

STATUS_OPTIONS = [
    "OPEN",
    "CLOSED",
    "EXPIRED",
    "PENDING_REVIEW",
    "FLAGGED",
]

EMPLOYMENT_OPTIONS = [et.value for et in EmploymentType]


def _load_intent():
    """Load the live-search intent for explanation purposes."""
    try:
        with util.with_session() as s:
            data = load_search_config(s)
        if not data:
            return None
        from config.search_config import SearchConfig
        cfg = SearchConfig(**data)
        return build_intent(
            cfg,
            include_related_titles=getattr(cfg, "include_related_roles", False),
        )
    except Exception:
        return None


def _explain_job(row: Dict, intent) -> str:
    """Build a short evidence-based 'Why included' string for a job row."""
    parts = []
    if intent is not None:
        # Title relevance
        if intent.primary_title:
            tr = classify_title(row.get("title", "") or "", intent)
            if tr.label == "exact":
                parts.append("Exact title match")
            elif tr.label == "strong":
                parts.append("Strong title match")
            elif tr.label == "related":
                parts.append("Related title")
            else:
                parts.append("Title: not constrained")
        # Location
        loc = row.get("location", "") or ""
        parsed = parse_location(loc)
        if not parsed.is_concrete and loc:
            parts.append("Location: unverified")
        elif parsed.is_concrete and (intent.cities or intent.countries):
            verdict, reason = location_verdict(parsed, intent)
            if verdict == "match":
                parts.append(f"Location confirmed ({parsed.city or parsed.country})")
            elif verdict == "unknown":
                parts.append("Location: unverified")
            else:
                parts.append("Location: mismatch")
        elif parsed.remote:
            parts.append("Remote role")
    # Source tag
    src = (row.get("source") or "").lower()
    if "jobboard" in src or "greenhouse" in src or "lever" in src:
        parts.append("Official job board")
    elif "hackernews" in src or "discord" in src:
        parts.append("Community post")
    elif "rss" in src or "feed" in src:
        parts.append("Aggregator feed")
    # Published date
    pub = row.get("published_at")
    if pub:
        parts.append(f"Posted {str(pub)[:10]}")
    else:
        parts.append("Date unverified")
    return " · ".join(parts) if parts else "—"



def render() -> None:
    st.title("Job discovery")
    st.caption("Browse, filter, and shortlist discovered jobs.")

    # ---------------- Filters ----------------
    f1, f2, f3, f4, f5, f6 = st.columns(6)
    with f1:
        source = st.selectbox(
            "Source",
            options=["(all)"] + util.list_registered_sources(),
            key="filter_source",
        )
    with f2:
        emp = st.selectbox(
            "Employment type",
            options=["(all)"] + EMPLOYMENT_OPTIONS,
            key="filter_emp",
        )
    with f3:
        workplace = st.selectbox(
            "Workplace",
            options=["(all)", "remote", "hybrid", "onsite", "unknown"],
            key="filter_workplace",
        )
    with f4:
        min_score = st.number_input(
            "Min match score", min_value=0.0, max_value=100.0, value=0.0, step=1.0,
            key="filter_min_score",
        )
    with f5:
        status = st.selectbox(
            "Status", options=["(all)"] + STATUS_OPTIONS, key="filter_status"
        )
    with f6:
        show_new = st.toggle(
            "New since last scan", value=False, key="filter_new_only",
            help="Only show jobs first seen in the most recent scan.",
        )

    query_text = st.text_input(
        "Search title / company / keywords",
        value=st.session_state.get("job_search_text", ""),
        key="job_search_text",
    ).strip()

    rows = util.list_jobs_with_match(
        min_score=float(min_score),
        limit=500,
    )

    # Apply in-event filters that aren't SQL filters in util.list_jobs_with_match
    filtered: List[Dict] = []
    last_scan = util.latest_scan_dict(limit=1)
    last_scan_id = last_scan.get("id") if last_scan else None
    for r in rows:
        if source != "(all)" and r.get("source") != source:
            continue
        if emp != "(all)" and r.get("employment_type") != emp:
            continue
        if workplace != "(all)" and r.get("workplace_arrangement") != workplace:
            continue
        if status != "(all)" and r.get("status") != status:
            continue
        if query_text:
            q = query_text.lower()
            hay = " ".join(
                [str(r.get(k, "")) for k in ("title", "company", "location", "employment_type")]
            ).lower()
            if q not in hay:
                continue
        if show_new and not _is_new(r, last_scan):
            continue
        filtered.append(r)

    # ---------------- Empty state ----------------
    if not filtered:
        _render_empty_state()
        return

    # Load intent once for the whole table
    intent = _load_intent()

    st.write(f"**{len(filtered)}** jobs (showing first 200)")
    display = [
        {
            "id": r["id"],
            "title": r["title"][:56],
            "company": r["company"][:32],
            "location": (r["location"] or "")[:28],
            "workplace": (r["workplace_arrangement"] or "")[:10],
            "type": (r["employment_type"] or "")[:12],
            "score": r.get("match_score"),
            "published": r.get("published_at") or "",
            "first seen": r.get("first_discovered") or "",
            "source": (r["source"] or "")[:18],
            "why included": _explain_job(r, intent),
        }
        for r in filtered[:200]
    ]
    st.dataframe(
        display,
        use_container_width=True,
        hide_index=True,
        column_config={
            "score": st.column_config.ProgressColumn(
                "Score", min_value=0, max_value=100, format="%.0f"
            ),
        },
    )

    # ---------------- Row selection -> detail ----------------
    pick = st.selectbox(
        "Open a job (by id)",
        options=[r["id"] for r in filtered[:200]],
        format_func=lambda i: f"{i}  —  {next((r['title'] for r in filtered if r['id'] == i), '')[:40]}",
        key="open_job_pick",
    )
    if st.button("Open job detail", type="primary"):
        if pick is not None:
            st.session_state.job_id = int(pick)
            st.session_state.page = "Job detail"
            st.rerun()


def _is_new(row: Dict, last_scan: Optional[Dict]) -> bool:
    """Approximate 'new since last scan': first inspected within last 1 day."""
    # We don't store the first-inspected timestamp per (job, scan); the closest
    # practical signal is first_discovered == last scan's started_at date.
    if last_scan is None or not last_scan.get("started_at"):
        return False
    start_day = str(last_scan["started_at"])[:10]
    first_day = row.get("first_discovered", "")[:10]
    return first_day and first_day >= start_day


def _render_empty_state() -> None:
    """Explain why no jobs are showing and offer concrete next steps."""
    st.subheader("No jobs found yet")
    reasons = [
        ("No sources returned results for the current filters.",
         "Check that at least one source is enabled on the **Sources** page."),
        ("The requested title may need additional synonyms.",
         "Add known synonyms in **Settings → Search intent → Title synonyms** "
         "(e.g. 'Python Developer' + 'Python Engineer')."),
        ("The requested location may be too restrictive.",
         "Try broadening to a country (e.g. 'Germany' instead of 'Berlin'), "
         "or enable **Worldwide remote** for remote roles."),
        ("Some sources may have failed during the last scan.",
         "Check **Scan history** for per-source failures and re-run the scan."),
        ("Jobs with unverifiable locations are hidden by default.",
         "In **Settings → Search intent**, untoggle *Require verified location* "
         "to include jobs tagged 'unverified'."),
        ("The posting-date window may be too narrow.",
         "Increase *Posted within days* on the Settings page (e.g. 30 instead of 7)."),
    ]
    col1, col2 = st.columns(2)
    for i, (reason, action) in enumerate(reasons):
        with (col1 if i % 2 == 0 else col2):
            with st.expander(f"Possible cause: {reason.split('.')[0]}"):
                st.write(f"**{reason}**")
                st.caption(f"→ {action}")
    st.divider()
    st.write("**Quick actions:**")
    b1, b2, b3 = st.columns(3)
    if b1.button("Go to Settings", use_container_width=True):
        st.session_state.page = "Settings"
        st.rerun()
    if b2.button("Go to Sources", use_container_width=True):
        st.session_state.page = "Sources"
        st.rerun()
    if b3.button("Run a new scan", use_container_width=True, type="primary"):
        st.session_state.page = "Overview"
        st.rerun()
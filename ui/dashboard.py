"""Overview / dashboard page (Overview in the spec).

Shows top-level stats + last scan + recent jobs + recent applications.
The "Scan for new jobs now" button is also here (top fault tolerance),
plus a more-detailed scan runner that backgrounds the call in a
thread and stores the result on `st.session_state.scan_result`.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import streamlit as st

from config.runtime_settings import load_search_config
from config.search_config import SearchConfig, default_search_config
from services.gemini_client import GeminiClient
from services.reset_service import execute_reset, plan_reset, ResetPlan
from services.scheduler import OnDemandScheduler
from ui import util

st.set_page_config(
    page_title="Job Hunt Agent — Overview",
    page_icon="\U0001F50D",  # 🔍
    layout="wide",
    initial_sidebar_state="expanded",
)


# ---------------------------------------------------------------------------
# Session-state helpers (live mode: one scan at a time)
# ---------------------------------------------------------------------------


def _progress_target() -> Dict[str, Any]:
    """Lazily create the progress record in session state."""
    if "scan_progress" not in st.session_state:
        st.session_state.scan_progress = {
            "total": 0,
            "done": 0,
            "message": "not started",
            "active": False,
        }
    return st.session_state.scan_progress


def _on_progress(done: int, message: str) -> None:
    p = _progress_target()
    p["done"] = done
    p["message"] = message
    p["active"] = True


def _set_total(total: int) -> None:
    p = _progress_target()
    p["total"] = total


def _load_current_search_config() -> SearchConfig:
    """Return the active SearchConfig (saved or default)."""
    with util.with_session() as session:
        data = load_search_config(session)
    if data:
        try:
            return SearchConfig(**data)
        except Exception:
            pass
    return default_search_config()


def _start_scan(req: Dict[str, Any]) -> None:
    """Kick off a scan in a background thread (one at a time)."""
    if st.session_state.scan_progress.get("active"):
        st.info("A scan is already in progress — wait for it to finish, or stop it.")
        return

    cfg = _load_current_search_config()
    evaluate = bool(req.get("evaluate_with_gemini", True))
    # Count sources so the progress bar has a denominator.
    from sources.registry import enabled_sources

    try:
        sources = enabled_sources(cfg)
        st.session_state.scan_progress["total"] = max(1, len(sources))
    except Exception:
        st.session_state.scan_progress["total"] = 1

    def _run() -> None:
        # Bind a fresh session scope for each *outer* commit. `run_discovery`
        # manages its own sessions internally, so we only need the result.
        try:
            sched = OnDemandScheduler()
            summary = sched.run_scan(cfg, on_progress=_on_progress)
            st.session_state.scan_result = summary.to_dict()
            st.session_state.scan_progress["active"] = False
            st.session_state.scan_progress["message"] = "done"
        except Exception as exc:  # noqa: BLE001 - catch-all for background thread
            st.session_state.scan_result = {
                "status": "error",
                "failures": [{"error": f"{type(exc).__name__}: {exc}"}],
                "total_discovered": 0,
                "new_jobs": 0,
                "duplicates": 0,
            }
            st.session_state.scan_progress["active"] = False
            st.session_state.scan_progress["message"] = f"failed: {exc}"

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    st.session_state.scan_progress["active"] = True
    st.session_state.scan_progress["started_at"] = datetime.now(timezone.utc)


def _stop_scan() -> None:
    st.session_state.scan_progress["active"] = False
    st.session_state.scan_progress["message"] = "cancelled by user"


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def _source_diagnostic_rows(per_source: dict) -> List[Dict[str, Any]]:
    """Convert a per_source dict (from DiscoverySummary.to_dict) into
    flat, category-tagged rows for the diagnostics table."""
    rows: List[Dict[str, Any]] = []
    for name, data in per_source.items():
        if not name:
            continue
        if not isinstance(data, dict):
            continue
        success = bool(data.get("success"))
        failed = int(data.get("failed", 0) or 0)
        attempted = int(data.get("attempted", 0) or 0)
        if not success and attempted == 0:
            status = "Skipped"
        elif success and failed == 0:
            status = "Success"
        elif success and failed > 0:
            status = "Partial"
        else:
            status = "Failed"
        err = (data.get("error") or data.get("error_message") or "") or ""
        if len(err) > 100:
            err = err[:100] + "..."
        rows.append({
            "Source": name,
            "Status": status,
            "Attempted": attempted,
            "Fetched": int(data.get("fetched", 0) or 0),
            "Parsed": int(data.get("parsed", 0) or 0),
            "Duplicates": int(data.get("duplicates", 0) or 0),
            "Listings": int(data.get("listings_found", 0) or 0),
            "Failed": failed,
            "Error": err,
        })
    return rows


def _render_source_diagnostics(result: dict) -> None:
    """Render per-source diagnostics from a scan result dict.

    Reads ``result["per_source"]`` (a dict of {source_name: SourceResult.details()}).
    Renders a dataframe inside a st.expander, with error text shown truncated
    (full text remains in the ``Error`` column, which streamlit can scroll
    horizontally to read).
    """
    per_source = result.get("per_source") or {}
    if not per_source:
        return
    try:
        import pandas as pd
    except Exception:
        # pandas is a streamlit local requirement, so this branch is rare.
        st.dataframe(list(per_source.items()))
        return
    rows = _source_diagnostic_rows(per_source)
    if not rows:
        return
    with st.expander("Source diagnostics"):
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def render() -> None:
    st.title("Overview")
    st.caption("Job Hunt Agent — local-first, AI-assisted job discovery.")

    # ---- Top row: stat counters using the latest data ----------------
    job_rows = util.list_jobs_with_match(limit=500)
    all_apps = util.list_applications_with_jobs()
    up_followups = util.upcoming_followups(window_days=14)
    last_scan = _last_scan_header(stats_all_apps=all_apps)

    c1, c2, c3, c4, c5, c6, c7 = st.columns(7)
    c1.metric("Total jobs", len(job_rows))
    c2.metric("New (last 7d)", _count_new(job_rows, days=7))
    c3.metric("Shortlisted", len([a for a in all_apps if a["app_status"] == "SHORTLISTED"]))
    c4.metric(
        "Prepared", len([a for a in all_apps if a["app_status"] in {"PREPARED", "APPROVED"}])
    )
    c5.metric("Applied", len([a for a in all_apps if a["app_status"] == "APPLIED"]))
    c6.metric("Interviews", len([a for a in all_apps if a["app_status"] == "INTERVIEW"]))
    c7.metric("Follow-ups", len(up_followups))

    # ---- Gemini health badge (non-invasive: SDK + key check) ----
    if st.button("AI health check"):
        try:
            info = GeminiClient().health_check()
            ok = info.get("status") == "AI_HEALTH_OK"
            status_color = "green" if ok else "orange"
            with st.container(border=True):
                st.markdown(f"**AI health** ({status_color}): {info.get('status')}")
                for k, v in info.items():
                    st.write(f"- **{k}**: {v}")
        except Exception as exc:  # noqa: BLE001
            st.error(f"AI health check failed: {exc}")

    # ---- Scan section ---------------------------------------------
    st.subheader("Scan for new jobs now")
    eval_with_gemini = st.toggle(
        "Evaluate with Gemini",
        value=True,
        help="If disabled, scans will not call the AI (deterministic filters only).",
    )
    cols = st.columns(3)
    scan = cols[0].button("Scan for new jobs now", type="primary", use_container_width=True)
    stop_btn = cols[1].button("Stop scan", use_container_width=True,
                              disabled=not _progress_target().get("active"))
    if scan:
        _start_scan({"evaluate_with_gemini": eval_with_gemini})
    if stop_btn:
        _stop_scan()

    p = _progress_target()
    if p.get("active") or p.get("message") not in ("not started", "done") or p.get(
        "message") == "done":
        pct = 0.0
        if p.get("total") and p.get("done") is not None:
            pct = min(1.0, p["done"] / max(1, p["total"]))
        st.progress(pct, text=f"Scan {p.get('message', '')} ({p.get('done', 0)}/{p.get('total', 0)})")

    if "scan_result" in st.session_state:
        result = st.session_state.scan_result
        status = result.get("status", "unknown")
        color = {"done": "green", "partial": "orange", "error": "red"}.get(status, "gray")
        with st.container(border=True):
            st.markdown(f"Last scan: **{status}** — {color}")
            # Stage counters — only show values the scan actually produced
            total_disc  = result.get("total_discovered", 0)
            new_jobs    = result.get("new_jobs", 0)
            dups        = result.get("duplicates", 0)
            excluded    = len(result.get("excluded") or [])
            failed_src  = len(result.get("failures") or [])
            scanned_src = len(result.get("per_source") or {})
            s1, s2, s3, s4, s5, s6 = st.columns(6)
            s1.metric("Discovered", total_disc, delta=f"-{dups} dups" if dups else None)
            s2.metric("New jobs", new_jobs)
            s3.metric("Filtered out", excluded if excluded else "—",
                      help="Jobs removed by hard filters (title, location, employment type).")
            s4.metric("AI skipped", "—",
                      help="Jobs removed by the deterministic hard-filter before AI scoring (logged in run log).")
            s5.metric("Sources OK", scanned_src - failed_src if failed_src else scanned_src)
            s6.metric("Sources failed", failed_src,
                      help="Sources that errored; scan continued with remaining sources.")
            fails = result.get("failures") or []
            if fails:
                st.markdown("**Failures:**")
                st.dataframe(fails, use_container_width=True)
            _render_source_diagnostics(result)
            if st.button("Dismiss scan result"):
                st.session_state.pop("scan_result", None)

    # ---- Data reset (one-click) ------------------------------------
    _render_data_management(job_rows)

    # ---- Last scan header / stats board ----------------------------
    if last_scan is None:
        st.info("No scans have been run yet — click [Scan for new jobs now] to start.")
    else:
        st.write(
            f"Last scan on **{last_scan['started_at']}** — "
            f"status: {last_scan['status']} — "
            f"discovered {last_scan['total']}, new {last_scan['new']} ({last_scan['dur_s']:.1f}s)"
        )

    # ---- Quick references ------------------------------------------
    col_a, col_b = st.columns(2)
    with col_a:
        _render_recent_jobs(job_rows)
    with col_b:
        _render_recent_applications(all_apps)

    # ---- Upcoming follow-ups ---------------------------------------
    if up_followups:
        st.subheader("Upcoming follow-ups (next 14 days)")
        st.dataframe(
            [
                {
                    "job": f"{x.get('company')} — {x.get('title')}",
                    "follow_up": x.get("follow_up_at", ""),
                    "status": x.get("status_label", ""),
                }
                for x in up_followups
            ],
            use_container_width=True,
        )
    else:
        st.caption("No upcoming follow-ups.")


# ---------------------------------------------------------------------------
# Sub-renderers
# ---------------------------------------------------------------------------


def _last_scan_header(stats_all_apps: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Return a summary dict of the most recent ScanRecord, or None."""
    return util.latest_scan_dict(limit=1)


def _count_new(rows: List[Dict[str, Any]], days: int = 7) -> int:
    count = 0
    for r in rows:
        if r.get("first_discovered") and util.within_days(
            datetime.strptime(r["first_discovered"], "%Y-%m-%d").replace(tzinfo=timezone.utc), days
        ):
            count += 1
    return count


def _render_recent_jobs(rows: List[Dict[str, Any]]) -> None:
    st.subheader("Recent jobs")
    if not rows:
        st.caption("No jobs yet.")
        return
    display = [
        {
            "id": r["id"],
            "title": r["title"][:42],
            "company": r["company"][:24],
            "location": (r["location"] or "")[:20],
            "type": r["employment_type"][:12],
            "score": r.get("match_score"),
            "source": r["source"][:16],
        }
        for r in rows[:12]
    ]
    st.dataframe(display, use_container_width=True, hide_index=True)


def _render_recent_applications(apps: List[Dict[str, Any]]) -> None:
    st.subheader("Recent applications")
    if not apps:
        st.caption("No applications yet — shortlist a job to begin.")
        return
    display = [
        {
            "company": r.get("company", "")[:24],
            "title": r.get("title", "")[:28],
            "status": r.get("status_label", ""),
            "follow_up": r.get("follow_up_at", ""),
        }
        for r in apps[:12]
    ]
    st.dataframe(display, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Data management (one-click reset)
# ---------------------------------------------------------------------------


def _render_data_management(job_rows: List[Dict[str, Any]]) -> None:
    """Render the reset card with two-level confirmation."""
    st.subheader("Data management")

    # Guard: a running scan means the reset would race with a writer.
    if _progress_target().get("active"):
        st.warning(
            "A scan is in progress. Stop the scan first to reset the database."
        )
        return

    scope = st.radio(
        "Reset scope",
        options=["current", "all"],
        format_func=lambda v: {
            "current": "Clear current search results (latest scan only)",
            "all": "Clear all job-search data (jobs, scans, source health, AI usage)",
        }[v],
        horizontal=True,
        key="reset_scope",
    )

    # Plan: how many rows would be affected?
    plan: Optional["ResetPlan"] = None
    try:
        with util.with_session() as s:
            plan = plan_reset(s, scope)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not compute reset plan: {exc}")
        return

    d = plan.to_dict()
    st.caption(
        f"Would remove: **{d['jobs']}** jobs, **{d['job_matches']}** job matches, "
        f"**{d['applications']}** applications, **{d['generated_documents']}** "
        f"generated documents, **{d['scan_records']}** scan records, "
        f"**{d['source_health']}** source health rows, **{d['ai_usage']}** AI usage "
        f"rows. **Total: {d['total']}** rows."
        + (
            f" ({d['running_scans_cancelled']} running scan cancelled before plan)"
            if d.get("running_scans_cancelled")
            else ""
        )
    )

    col_a, col_b = st.columns(2)
    with col_a:
        st.markdown(
            "**Preserved (never deleted):**"
        )
        st.caption(
            "Candidate profiles, saved CVs, cover letters, application "
            "tracking notes, search settings and named profiles, and the "
            "registry of discovered job sources."
        )
    with col_b:
        st.markdown(
            "**Removed:** "
            + ("the most recent scan and jobs it produced."
               if scope == "current" else
               "all job-search rows (jobs, matches, applications, "
                  "generated documents, scans, source health, AI usage).")
        )

    checkbox_text = (
        "I understand the above data will be removed"
        if scope == "all" else
        "I understand only the latest scan and its jobs will be removed"
    )
    confirmed = st.checkbox(checkbox_text, key="reset_confirm")
    if confirmed and st.button(
        "Reset job-search data", type="primary", use_container_width=True,
        key="reset_confirm_button",
    ):
        try:
            with util.with_session() as s:
                # Re-plan so we reflect the live DB state immediately
                # before executing; reuse the same session for atomicity.
                plan2 = plan_reset(s, scope)
                result = execute_reset(s, plan=plan2)
            if result.success:
                _post_reset_cleanup()
                st.success(
                    "Reset complete. Removed "
                    f"{_total_removed(result)} rows. "
                    "Start a new scan to repopulate."
                )
            else:
                st.error(
                    "Reset failed and was rolled back. "
                    f"Error: {result.error}"
                )
        except Exception as exc:  # noqa: BLE001
            st.error(f"Reset failed: {type(exc).__name__}: {exc}")


def _total_removed(result) -> int:
    deleted = result.deleted or {}
    return sum(
        v for v in deleted.values() if isinstance(v, int)
    )


def _post_reset_cleanup() -> None:
    """Clear all Streamlit-side caches that could resurrect deleted jobs."""
    st.session_state.pop("scan_result", None)
    st.session_state.pop("scan_progress", None)
    st.session_state.pop("job_id", None)
    # The activity log entry can stay — it records what the user did.




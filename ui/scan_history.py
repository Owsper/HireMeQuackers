"""Scan History page.

Lists every past scan with its configuration, timing, and outcomes.
Encourages one-click "run again" for convenience.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

import streamlit as st

from database import repository as db
from ui import util


def render() -> None:
    st.title("Scan history")
    st.caption("Every scan run, newest first.")

    # Keep a single session open for the whole render: the rows below are
    # ORM objects whose attributes are read **after** the fetch, and with
    # `expire_on_commit=True` a closed/committed session would raise
    # `DetachedInstanceError` on those reads. Drawing inside the session
    # keeps the objects attached so lazy attribute loads succeed.
    with util.with_session() as session:
        rows = util.list_scans(session, limit=100)
        if not rows:
            st.info("No scans yet. Trigger one from the Overview page.")
            return

        _render_scans(rows)


def _render_scans(rows) -> None:
    # Metrics
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Scans (shown)", len(rows))
    c2.metric("Total discovered", sum(r.total_discovered or 0 for r in rows))
    c3.metric("New jobs", sum(r.new_jobs or 0 for r in rows))
    c4.metric("Duplicates merged", sum(r.duplicates or 0 for r in rows))

    for r in rows:
        label = (
            f"#{r.id} — {util.dt_local(r.started_at)}"
            f"  ({_final_status(r)})"
            f"  · discovered {r.total_discovered or 0}  "
            f"  · new {r.new_jobs or 0}  "
            f"  · duplicates {r.duplicates or 0}"
        )
        with st.expander(label):
            # Timing
            b1, b2, b3 = st.columns(3)
            b1.markdown(f"**Started:** {util.dt_local(r.started_at)}")
            b2.markdown(f"**Completed:** {util.dt_local(r.completed_at) or '—'}")
            b3.markdown(
                f"**Duration:** {_duration(r.started_at, r.completed_at)}"
            )
            b4, b5, b6 = st.columns(3)
            b4.markdown(f"**Status:** {r.status}")
            b5.markdown(f"**Trigger:** {r.trigger}")
            b6.caption("—")

            # Config
            st.markdown("**Search config**")
            cfg = _parse_json(r.search_config_json)
            if cfg:
                # Compact display
                et = cfg.get("employmentTypes") or cfg.get("employment_types") or []
                kw = cfg.get("keywords") or cfg.get("include_keywords") or []
                locs = cfg.get("locations") or []
                extra = {
                    "keywords": kw,
                    "locations": locs,
                    "employment_types": et,
                }
                for k, v in extra.items():
                    if v:
                        st.write(f"- **{k}:** {', '.join(str(x) for x in v[:12])}")
                with st.expander("Full config JSON"):
                    st.json(cfg)
            else:
                st.caption("No config recorded.")

            # Failures
            fails = _parse_json(r.failures_json)
            if fails:
                st.markdown("**Failures**")
                if isinstance(fails, list):
                    for f in fails:
                        if isinstance(f, dict):
                            st.markdown(
                                f"- {f.get('source', f.get('name', '?'))}: "
                                f"{(f.get('error') or f.get('message') or '')[:200]}"
                            )
                        else:
                            st.markdown(f"- {f}")
                else:
                    st.json(fails)

            # Retry
            if r.status in ("error", "partial", "running", "timeout"):
                if st.button("Re-run this search config", key=f"retry_{r.id}"):
                    # Persist the old config then trigger a fresh scan.
                    st.session_state.scan_retry_config = r.search_config_json
                    st.rerun()


def _final_status(r) -> str:
    return r.status or "?"


def _duration(start: Optional[datetime], end: Optional[datetime]) -> str:
    if not start or not end:
        return "—"
    d = end - start
    return f"{d.total_seconds():.1f}s"


def _parse_json(raw: Optional[str]) -> Optional[Any]:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None

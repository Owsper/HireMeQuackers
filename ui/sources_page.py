"""Source Health page.

Shows every registered job source with its last scan status, error,
and an Enable / Disable control. This is the single source of truth for
per-source visibility of the scan pipeline.
"""
from __future__ import annotations

from datetime import datetime
from typing import List

import streamlit as st

from database import repository as db
from ui import util


def render() -> None:
    st.title("Source Health")
    st.caption("Per-source scan status and enable / disable controls.")

    registered = util.list_registered_sources()
    if not registered:
        st.error("No sources are registered. Check the `sources/` package.")
        return

    # `_render_table` opens its own session via `with_session()`.
    _render_table(registered)


def _render_table(registered: List[str]) -> None:
    with util.with_session() as session:
        for source in registered:
            rec = db.get_source_health(session, source)
            c1, c2, c3, c4, c5 = st.columns([3, 2, 2, 2, 2])
            name_display = source

            with c1:
                st.markdown(f"**{name_display}**")
                if rec and rec.source_url:
                    st.caption(rec.source_url)

            with c2:
                if rec and rec.last_success_at:
                    st.markdown(f"Last OK: {_dt(rec.last_success_at)}")
                    st.caption(f"Last try: {_dt(rec.last_attempt_at)}" if rec.last_attempt_at else "—")
                elif rec and rec.last_attempt_at:
                    st.markdown(f"Last try: {_dt(rec.last_attempt_at)}")
                    st.caption("No successful scan yet")
                else:
                    st.caption("Never scanned")

            with c3:
                listings = rec.listings_found if rec else 0
                st.metric("Found", listings)

            with c4:
                if rec and rec.error_message:
                    st.caption(f"⚠ {rec.error_message[:80]}")
                    if len(rec.error_message) > 80:
                        with st.expander("Full error"):
                            st.code(rec.error_message)
                else:
                    st.caption("—")

            with c5:
                enabled = rec.is_enabled if rec else True
                st.toggle("Enabled", value=enabled, key=f"src_en_{source}")
                if st.button(
                    "Save",
                    key=f"src_save_{source}",
                    use_container_width=True,
                ):
                    new_state = bool(st.session_state.get(f"src_en_{source}", enabled))
                    try:
                        if not rec:
                            # No health row yet -> create one (enabled reflects the toggle)
                            db.record_source_health(
                                session,
                                source_name=source,
                                source_url=None,
                                is_enabled=new_state,
                            )
                        db.set_source_enabled(session, source, new_state)
                        st.toast(f"{source}: enabled={new_state}")
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"Could not update {source}: {exc}")
                    st.rerun()


def _dt(dt: datetime) -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        return dt.strftime("%Y-%m-%d %H:%M")
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")

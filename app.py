"""HireMeQuackers - AI Job-Hunting Agent.

Entry point. Run with: streamlit run app.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure project root is on sys.path so relative imports work
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

from services.logger import setup_logging  # noqa: E402
from ui.util import ensure_db  # noqa: E402

# Map each sidebar label to the module that provides render().
_PAGE_MODULES = {
    "Overview": "ui.dashboard",
    "Search Settings": "ui.settings_page",
    "Job Discovery": "ui.jobs_page",
    "Job Detail": "ui.job_detail_page",
    "Candidate Profile": "ui.cv_page",
    "Documents": "ui.documents_page",
    "Application Tracker": "ui.applications_page",
    "Sources": "ui.sources_page",
    "Scan History": "ui.scan_history",
}


def main() -> None:
    # Step 0: Initialize logging *before* anything else so every
    # subsequent service (cv_parser, gemini_client, sources, etc.)
    # can log to `data/logs/app.log` (previously orphaned — the root
    # logger had no file handler, so log lines went silent).
    setup_logging()

    # Step 1: Ensure the SQLite schema exists before any page issues
    # a query. Idempotent: create_all() is a no-op when tables exist.
    ensure_db()

    st.set_page_config(page_title="HireMeQuackers", page_icon="\U0001F4BC", layout="wide")

    # Sidebar navigation: individual buttons with text labels
    st.sidebar.title("Navigation")
    if "nav_choice" not in st.session_state:
        st.session_state.nav_choice = "Overview"

    for key in _PAGE_MODULES.keys():
        pressed = st.sidebar.button(key, use_container_width=True)
        if pressed:
            st.session_state.nav_choice = key
        # Active indicator (●) — can be a CSS trick, but let's keep it simple
        # by relying on the button's visual state. Streamlit buttons don't
        # have a persistent "active" state, so we render a muted caption
        # next to the currently selected one instead.

    choice_key = st.session_state.nav_choice
    module_path = _PAGE_MODULES.get(choice_key, "ui.dashboard")
    module = __import__(module_path, fromlist=["render"])
    module.render()


if __name__ == "__main__":
    main()

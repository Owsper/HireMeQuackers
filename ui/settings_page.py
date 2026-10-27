"""Search settings UI — edit SearchConfig and save as a named profile.

Layout:
- Top: load existing settings / choose a saved profile.
- Middle: forms for each SearchConfig section (employment, keywords,
  locations, keywords, filters, sources).
- Bottom: [Save as NEW] (profiles dropdown + new-name field) and
  [Apply + persist to live settings] + [Reset to defaults].
- Side: Gemini/API health card.

All numeric fields are parsed with try/except and re-raised as a
user-facing st.error message — the house style for this codebase
(mirror of Vera's settings_db.py input-validation pattern).
"""
from __future__ import annotations

from typing import Any, Dict, List

import streamlit as st

from config.runtime_settings import load_search_config, save_search_config
from config.search_config import (
    BooleanGroups,
    EmploymentType,
    SearchConfig,
    default_search_config,
)

from ui import util

EMPLOYMENT_LABELS = {et.value: et.label for et in EmploymentType}
EMPLOYMENT_CHOICES = list(EMPLOYMENT_LABELS.keys())


# ---------------------------------------------------------------------------
# Conversion helpers (front-edge guards)
# ---------------------------------------------------------------------------


def _parse_int_list(text: str, key_desc: str) -> List[str]:
    """Parse a comma/newline separated list of non-blank tokens."""
    if not text:
        return []
    out = []
    for line in text.replace("\r", "\n").splitlines():
        for tok in line.split(","):
            t = tok.strip()
            if t:
                out.append(t)
    return out


def _to_bool(t: Any) -> bool:
    if isinstance(t, bool):
        return t
    if isinstance(t, str):
        return t.lower() in {"1", "true", "t", "yes", "y", "on"}
    return bool(t)


def _to_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip() == "":
        return None
    try:
        return int(value)
    except (ValueError, TypeError) as exc:
        raise ValueError("Must be an integer.") from exc


def _to_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip() == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError) as exc:
        raise ValueError("Must be a number.") from exc


# ---------------------------------------------------------------------------
# Persistence glue
# ---------------------------------------------------------------------------


def _load_live() -> SearchConfig:
    with util.with_session() as s:
        data = load_search_config(s)
    if data:
        try:
            return SearchConfig(**data)
        except Exception:
            pass
    return default_search_config()


def _load_named(name: str) -> SearchConfig | None:
    d = util.load_search_profile_dict(name)
    if not d:
        return None
    try:
        return SearchConfig(**d)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def render() -> None:
    st.title("Search settings")
    st.caption("Edit the global search preferences, or load / save a named profile.")

    # ---- Profile loader ----
    saved = util.list_search_profile_names()
    col_a, col_b = st.columns(2)
    with col_a:
        chosen = col_a_select(saved)
        if chosen and st.button("Load profile", use_container_width=True):
            cfg_named = _load_named(chosen)
            if cfg_named is not None:
                st.session_state.search_config = cfg_named
                st.session_state.flash = f"Loaded profile: {chosen}"
            else:
                st.error(f"Could not load profile '{chosen}'.")
    with col_b:
        fresh_btn = col_b.button("New default", use_container_width=True)
        if fresh_btn:
            st.session_state.search_config = default_search_config()
            st.session_state.flash = "Reset to defaults (not persisted yet)."

    cfg: SearchConfig = st.session_state.get("search_config") or _load_live()
    st.session_state.setdefault("flash", "")

    if st.session_state.flash:
        st.info(st.session_state.flash)
        st.session_state.flash = ""

    _render_forms(cfg)

    # ---- Footer actions ----
    st.divider()
    left, mid, right = st.columns([1, 1, 1])
    _render_footer(cfg)


def _render_forms(cfg: SearchConfig) -> None:
    # --- Employment types ---
    st.subheader("Employment types")
    selected_types = [
        et.value if isinstance(et, EmploymentType) else et for et in cfg.employment_types
    ]
    # Build a label map (value -> human)
    all_labels = {et.value: et.label for et in EmploymentType}
    # On Streamlit 1.43+ `st.multiselect` returns the placeholder, so use the
    # base list (value) and look up labels for the caption.
    selected = st.multiselect(
        "Select one or more",
        options=list(all_labels.keys()),
        default=selected_types,
        format_func=lambda v: all_labels.get(v, v),
        help="Employment types that a discovered job must match to be considered.",
    )
    if len(selected) == 0:
        st.warning("At least one employment type is required.")
    cfg.employment_types = [EmploymentType(v) for v in selected]

    # --- Keywords / skills ---
    col1, col2 = st.columns(2)
    with col1:
        st.markdown("**Include keywords** (comma or newline separated)")
        inc = st.text_area(
            "include_keywords",
            value=", ".join(cfg.include_keywords),
            key="inc_kw",
            height=100,
        )
        st.markdown("**Exclude keywords**")
        exc = st.text_area(
            "exclude_keywords",
            value=", ".join(cfg.exclude_keywords),
            key="exc_kw",
            height=100,
        )
    with col2:
        st.markdown("**Required skills**")
        req = st.text_area(
            "required_skills",
            value=", ".join(cfg.required_skills),
            key="req_sk",
            height=100,
        )
        st.markdown("**Preferred skills**")
        pref = st.text_area(
            "preferred_skills",
            value=", ".join(cfg.preferred_skills),
            key="pref_sk",
            height=100,
        )
    cfg.include_keywords = _parse_int_list(inc, "include keywords")
    cfg.exclude_keywords = _parse_int_list(exc, "exclude keywords")
    cfg.required_skills = _parse_int_list(req, "required skills")
    cfg.preferred_skills = _parse_int_list(pref, "preferred skills")

    # --- Companies / industries / phrases ---
    row = st.columns(3)
    with row[0]:
        companies = st.text_area(
            "Company names", value=", ".join(cfg.company_names), key="companies", height=80
        )
    with row[1]:
        industries = st.text_area(
            "Industries", value=", ".join(cfg.industries), key="industries", height=80
        )
    with row[2]:
        phrases = st.text_area(
            "Phrases",
            value=", ".join(cfg.phrases),
            key="phrases",
            height=80,
        )
    cfg.company_names = _parse_int_list(companies, "companies")
    cfg.industries = _parse_int_list(industries, "industries")
    cfg.phrases = _parse_int_list(phrases, "phrases")

    # --- Locations ---
    st.subheader("Locations")
    lrow = st.columns(3)
    with lrow[0]:
        locations = st.text_area(
            "Included locations",
            value=", ".join(cfg.locations),
            key="locations",
            height=80,
        )
    with lrow[1]:
        excluded_locations = st.text_area(
            "Excluded locations",
            value=", ".join(cfg.excluded_locations),
            key="excl_locations",
            height=80,
        )
    with lrow[2]:
        languages = st.text_area(
            "Language requirements",
            value=", ".join(cfg.language_requirements),
            key="langs",
            height=80,
        )
    cfg.locations = _parse_int_list(locations, "locations")
    cfg.excluded_locations = _parse_int_list(excluded_locations, "excluded locations")
    cfg.language_requirements = _parse_int_list(languages, "languages")

    # --- Workplace arrangement ---
    st.subheader("Workplace arrangement")
    wrap = st.columns(4)
    cfg.include_remote = wrap[0].toggle("Remote", value=cfg.include_remote, key="inc_remote")
    cfg.include_hybrid = wrap[1].toggle("Hybrid", value=cfg.include_hybrid, key="inc_hybrid")
    cfg.include_onsite = wrap[2].toggle("Onsite", value=cfg.include_onsite, key="inc_onsite")
    cfg.worldwide_remote = wrap[3].toggle(
        "Worldwide remote", value=cfg.worldwide_remote, key="ww_remote"
    )

    # --- Filters ---
    st.subheader("Filters")
    frow = st.columns(4)
    with frow[0]:
        posted_within_days = _number_input(
            "Posted within days (1/3/7/14/30 or blank)",
            value=cfg.posted_within_days,
            min_value=None,
            key="posted_within_days",
        )
    with frow[1]:
        salary_min = _number_input(
            "Salary min", value=cfg.salary_min, min_value=None, key="salary_min"
        )
    with frow[2]:
        salary_max = _number_input(
            "Salary max", value=cfg.salary_max, min_value=None, key="salary_max"
        )
    with frow[3]:
        max_results = _number_input(
            "Max results (1-5000)",
            value=cfg.max_results,
            min_value=1,
            max_value=5000,
            key="max_results",
        )
    if posted_within_days not in (None, 1, 3, 7, 14, 30):
        st.warning("Posted-within-days should be one of: 1, 3, 7, 14, 30 (or blank).")
    if salary_min is not None and salary_max is not None and salary_max < salary_min:
        st.error("Salary max must be >= Salary min.")
    cfg.posted_within_days = posted_within_days
    cfg.salary_min = salary_min
    cfg.salary_max = salary_max
    cfg.max_results = max(1, min(5000, int(max_results or 100)))

    # --- Boolean groups (optional) ---
    with st.expander("Boolean groups (AND / OR / NOT)"):
        b_row = st.columns(3)
        with b_row[0]:
            b_and = st.text_area(
                "AND",
                value=cfg.boolean_groups.and_ if cfg.boolean_groups else ", ".join([]),
                key="b_and",
                height=100,
                disabled=False,
            )
        with b_row[1]:
            b_or = st.text_area(
                "OR",
                value=cfg.boolean_groups.or_ if cfg.boolean_groups else ", ".join([]),
                key="b_or",
                height=100,
            )
        with b_row[2]:
            b_not = st.text_area(
                "NOT",
                value=cfg.boolean_groups.not_ if cfg.boolean_groups else ", ".join([]),
                key="b_not",
                height=100,
            )
        a_list = _parse_int_list(b_and, "and")
        o_list = _parse_int_list(b_or, "or")
        n_list = _parse_int_list(b_not, "not")
        if a_list or o_list or n_list:
            cfg.boolean_groups = BooleanGroups(**{"AND": a_list, "OR": o_list, "NOT": n_list})
        else:
            cfg.boolean_groups = None

    # --- Education / company size (rarely filled; shown but optional) ---
    with st.expander("Advanced (education / company size)"):
        arow = st.columns(3)
        with arow[0]:
            cfg.education_level = arow[0].text_input(
                "Education level (e.g. BSc, MSc)",
                value=cfg.education_level or "",
                key="edu_level",
            )
            if cfg.education_level == "":
                cfg.education_level = None
        with arow[1]:
            size_min = _number_input(
                "Company size min", value=cfg.company_size_min, min_value=None, key="csize_min"
            )
            cfg.company_size_min = size_min
        with arow[2]:
            size_max = _number_input(
                "Company size max", value=cfg.company_size_max, min_value=None, key="csize_max"
            )
            cfg.company_size_max = size_max

    # --- Sources selection ---
    st.subheader("Sources")
    all_sources = util.list_registered_sources()
    if not all_sources:
        st.info("No sources registered yet — the registry will populate as adapters are loaded.")
        return
    # Multiselect values (string slugs).
    selected = st.multiselect(
        "Enabled sources (empty = all)",
        options=all_sources,
        default=cfg.enabled_sources,
        key="enabled_sources",
    )
    cfg.enabled_sources = sorted(set(selected))


def _render_footer(cfg: SearchConfig) -> None:
    min_score = _number_input(
        "Min match score (0-100, 0 = show all)", value=cfg.min_match_score, min_value=0.0,
        max_value=100.0, key="min_match_score",
    )
    cfg.min_match_score = float(min_score) if min_score is not None else 0.0

    left, mid, right = st.columns([1, 1, 2])
    apply_share_ok = False
    if left.button("Apply + persist to live settings", type="primary", use_container_width=True):
        try:
            cfg.validate()
        except Exception as exc:
            st.error(f"Validation failed: {exc}")
            return
        with util.with_session() as s:
            save_search_config(s, cfg)
        st.success("Search config saved as the live (default) local setting.")
        apply_share_ok = True

    profiles = util.list_search_profile_names()
    profile_name = right.text_input(
        "Save current as profile (name)...", value=st.session_state.get("profile_name", ""),
        key="profile_name", placeholder="e.g., 'Berlin internship'",
    )
    save_as_btn = right.button("Save as profile", use_container_width=True)
    if save_as_btn and profile_name:
        try:
            cfg.validate()
        except Exception as exc:
            st.error(f"Validation failed: {exc}")
            return
        util.save_search_profile_dict(profile_name, cfg.normalized_dict())
        st.success(f"Saved as profile: {profile_name}")
        st.session_state.profile_name = ""

    # ---- Gemini health side card ----
    st.subheader("Gemini / AI health")
    try:
        from services.gemini_client import GeminiClient

        info = GeminiClient().health_check()
        ok = info.get("status") == "AI_HEALTH_OK"
        with st.container(border=True):
            status_color = "green" if ok else "red"
            st.markdown(f"**AI health** ({status_color}): {info.get('status')}")
            st.write(f"- `gemini_model`: {info.get('model')}")
            st.write(f"- SDK available: {info.get('sdk_available')}")
            st.write(f"- API key present: {info.get('key_present')}")
    except Exception as exc:  # noqa: BLE001
        st.error(f"AI health check failed: {exc}")


def col_a_select(saved: List[str]):
    """Small multiselect wrapper for loading a saved profile (returns a string)."""
    if not saved:
        st.caption("No saved profiles yet.")
        return ""
    sel = st.selectbox("Saved profiles", options=saved, key="saved_profile_pick")
    return sel


def col_b(x):  # no-op (kept for clarity)
    return None


def _number_input(label, value, min_value=None, max_value=None, key=""):
    """Build st.number_input safely. Returns the new value."""
    num = st.number_input(
        label,
        value=value if value is not None else 0,
        min_value=min_value,
        max_value=max_value,
        key=key or "_num",
    )
    # Treat 0 as None for the optional numeric filters (salary_min/max, min_match, etc.)
    # where None is a meaningful sentinel.
    if min_value is None or (min_value == 0 and num == 0):
        return None if num == 0 and (label.startswith("Posted") or "Salary" in label or label.startswith("Min match")) else num
    return num
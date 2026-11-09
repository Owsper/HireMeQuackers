"""`quackers` — command-line interface for HireMeQuackers.

Replaces the former Streamlit dashboard. Every command is a thin layer over
the existing `services/`, `database/` and `config/` modules; no business
logic lives here.

Run `quackers` alone for the interactive session, or `quackers --help` for the
command list.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from quackers import store
from quackers import term

db = store.db

MAX_CV_BYTES = 15 * 1024 * 1024  # 15 MB, same limit the dashboard enforced

# Allowed "next step" per status (REJECTED / WITHDRAWN are always allowed).
NEXT_STATUSES: Dict[str, List[str]] = {
    "DISCOVERED": ["SHORTLISTED"],
    "SHORTLISTED": ["PREPARED", "APPROVED"],
    "PREPARED": ["APPROVED"],
    "APPROVED": ["APPLIED"],
    "APPLIED": ["INTERVIEW"],
}
TERMINAL = ["REJECTED", "WITHDRAWN"]


class CliError(Exception):
    """A user-facing error: printed without a traceback, exit code 1."""


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _json_get(raw: Optional[str]) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None


def _split_list(text: str) -> List[str]:
    out: List[str] = []
    for line in (text or "").replace("\r", "\n").splitlines():
        out.extend(tok.strip() for tok in line.split(",") if tok.strip())
    return out


def _lines(items: Any) -> List[str]:
    """List of {text: ...} dicts or strings -> list of non-empty strings."""
    out: List[str] = []
    for item in items or []:
        text = item.get("text", "") if isinstance(item, dict) else item
        text = str(text).strip()
        if text:
            out.append(text)
    return out


def _bullets(lines: Sequence[str]) -> List[Dict[str, str]]:
    return [{"text": str(ln).strip()} for ln in lines if str(ln).strip()]


def _require_job(session, job_id: int):
    job = db.get_job(session, job_id)
    if job is None:
        raise CliError(f"Job {job_id} not found. List jobs with `quackers jobs`.")
    return job


def _require_active_profile():
    profile = store.load_active_profile()
    if profile is None:
        raise CliError(
            "No active candidate profile. Create one with `quackers profile import <cv>`."
        )
    return profile


# ---------------------------------------------------------------------------
# Search config
# ---------------------------------------------------------------------------


def load_config():
    """Return the live SearchConfig (saved, or the default)."""
    from config.runtime_settings import load_search_config
    from config.search_config import SearchConfig, default_search_config

    with store.with_session() as session:
        data = load_search_config(session)
    if data:
        try:
            return SearchConfig(**data)
        except Exception as exc:  # noqa: BLE001
            term.warn(f"Saved search config is invalid ({exc}); using defaults.")
    return default_search_config()


def validate_config(data: Dict[str, Any]):
    """Build a fully validated SearchConfig from a plain dict.

    Raises pydantic.ValidationError on bad values.
    """
    from config.search_config import SearchConfig

    return SearchConfig.model_validate(data)


def _config_dict(cfg) -> Dict[str, Any]:
    return cfg.model_dump(mode="json", by_alias=True)


def _coerce_config_value(cfg, key: str, raw: str) -> Any:
    """Turn a `config set KEY=VALUE` string into a value pydantic accepts."""
    current = _config_dict(cfg)
    if key not in current:
        known = ", ".join(sorted(current))
        raise CliError(f"Unknown setting {key!r}. Known settings: {known}")
    if raw.strip().lower() in {"none", "null", ""}:
        if isinstance(current[key], list):
            return []
        return "" if isinstance(current[key], str) else None
    if isinstance(current[key], list):
        return _split_list(raw)
    return raw  # pydantic coerces "true", "42", "0.5", enum values, ...


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def cmd_status(args) -> None:
    jobs = store.list_jobs_with_match(limit=500)
    apps = store.list_applications_with_jobs()
    followups = store.upcoming_followups(window_days=14)
    last = store.latest_scan_dict(limit=1)

    def count(*statuses: str) -> int:
        return len([a for a in apps if a["app_status"] in statuses])

    new_7d = 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    for r in jobs:
        try:
            first = datetime.strptime(r.get("first_discovered") or "", "%Y-%m-%d")
        except ValueError:
            continue
        if first.replace(tzinfo=timezone.utc) >= cutoff:
            new_7d += 1

    term.heading("HireMeQuackers")
    term.kv([
        ("Total jobs", len(jobs)),
        ("New (last 7 days)", new_7d),
        ("Shortlisted", count("SHORTLISTED")),
        ("Prepared", count("PREPARED", "APPROVED")),
        ("Applied", count("APPLIED")),
        ("Interviews", count("INTERVIEW")),
        ("Follow-ups (14 days)", len(followups)),
    ])

    term.heading("Last scan")
    if not last:
        term.info("  No scans yet. Run `quackers scan`.")
    else:
        term.kv([
            ("Started", last["started_at"]),
            ("Status", last["status"]),
            ("Discovered", last["total"]),
            ("New", last["new"]),
            ("Duplicates", last["dups"]),
            ("Duration", f"{last['dur_s']:.1f}s"),
        ])

    if jobs:
        term.heading("Recent jobs")
        term.table(
            jobs[:10],
            ["id", "match_score", "title", "company", "location"],
            headers={"match_score": "score"},
            max_widths={"title": 45, "company": 25, "location": 25},
        )

    if followups:
        term.heading("Upcoming follow-ups")
        term.table(
            followups,
            ["id", "follow_up_at", "status_label", "company", "title"],
            headers={"id": "job", "follow_up_at": "follow-up", "status_label": "status"},
            max_widths={"title": 45, "company": 25},
        )

    if args.ai_check:
        print()
        cmd_ai_check(args)


def cmd_ai_check(args) -> None:
    from services import llm
    from services.ai_client import AIClient

    info = AIClient().health_check()
    if getattr(args, "verbose", False):
        from services.ai_diagnostics import probe
        info = probe("connectivity")
    term.heading("AI health")
    term.kv([(k, v) for k, v in info.items()])
    term.heading("Providers")
    term.table(llm.overview(), ["provider", "active", "key", "sdk", "default_model"],
               headers={"default_model": "default model"})
    term.info("Pick one with AI_PROVIDER and a model with AI_MODEL (`quackers env`).")
    if info.get("status") != "AI_HEALTH_OK":
        if info.get("status") != "PASSED":
            term.warn("AI unavailable or untested; see diagnostics above.")
    term.info("Connectivity does not verify extraction, matching or tailoring. Use `quackers ai-test <capability>`.")
    if getattr(args, "verbose", False) and info.get("status") == "FAILED":
        raise CliError("AI connectivity check failed")


def cmd_ai_test(args) -> None:
    from services.ai_diagnostics import probe
    result = probe(args.capability)
    term.kv(result.items())
    if result["status"] != "PASSED":
        raise CliError(f"AI {args.capability} capability failed; no fallback counted as success")


def cmd_job_extract(args) -> None:
    from services.enrichment_service import enrich_missing_jobs
    with store.with_session() as session:
        job = _require_job(session, args.job_id)
        if args.force:
            job.extracted_at = None
        summary = enrich_missing_jobs(session, load_config(), job_ids=[job.id])
        term.kv([("attempted", summary.attempted), ("status", job.extraction_status),
                 ("method", job.extraction_method), ("completeness", job.description_completeness),
                 ("errors", job.extraction_errors_json or "[]")])


def cmd_match(args) -> None:
    from services.matching import evaluate_jobs
    profile = _require_active_profile()
    with store.with_session() as session:
        jobs = [_require_job(session, args.job_id)] if args.job_id else db.list_jobs(session)
    results = evaluate_jobs(jobs, profile)
    for job, result in results:
        term.kv([("job", job.id), ("status", result["scoring_status"]), ("score", result["total_score"]),
                 ("confidence", result["confidence"]), ("evidence coverage", result["evidence_coverage"])])


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------


def source_diagnostic_rows(per_source: dict) -> List[Dict[str, Any]]:
    """Flatten DiscoverySummary.per_source into status-tagged table rows."""
    rows: List[Dict[str, Any]] = []
    for name, data in per_source.items():
        if not name or not isinstance(data, dict):
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


def _run_scan(cfg, *, evaluate: bool) -> Dict[str, Any]:
    from services.discovery import run_discovery
    from sources.registry import enabled_sources

    try:
        total = max(1, len(enabled_sources(cfg)))
    except Exception:  # noqa: BLE001
        total = 1

    def on_progress(done: int, message: str) -> None:
        term.info(f"  [{done}/{total}] {message}")

    from services import llm

    ai = llm.get()
    note = f" (AI scoring : {ai.label})" if evaluate and ai.available else ("" if evaluate else " (AI scoring off)")
    term.info(f"Scanning {total} source(s){note}…")
    try:
        summary = run_discovery(cfg, on_progress=on_progress, evaluate_with_ai=evaluate)
    except KeyboardInterrupt:
        raise CliError("Scan interrupted. Partial results may already be saved.")
    return summary.to_dict()


def _print_scan_result(result: Dict[str, Any]) -> None:
    status = str(result.get("status", "unknown"))
    colour = {"done": "green", "partial": "yellow", "error": "red"}.get(status)
    per_source = result.get("per_source") or {}
    failures = result.get("failures") or []
    enrich = result.get("enrichment") or {}

    print()
    print(term.style("Scan result: ", "bold", "cyan") + term.style(status, "bold", colour or "cyan"))
    term.kv([
        ("Discovered", result.get("total_discovered", 0)),
        ("New jobs", result.get("new_jobs", 0)),
        ("Duplicates", result.get("duplicates", 0)),
        ("Filtered out", len(result.get("excluded") or [])),
        ("Sources OK", max(0, len(per_source) - len(failures))),
        ("Sources failed", len(failures)),
        ("Enriched", f"{enrich.get('succeeded', 0)}/{enrich.get('attempted', 0)}"),
        ("Duration", f"{float(result.get('duration_seconds') or 0):.1f}s"),
    ])
    rows = source_diagnostic_rows(per_source)
    if rows:
        term.heading("Sources")
        term.table(
            rows,
            ["Source", "Status", "Attempted", "Fetched", "Parsed", "Duplicates", "Failed", "Error"],
            max_widths={"Error": 60},
        )
    platforms = result.get("per_platform") or {}
    if platforms:
        term.heading("Platforms")
        term.table(
            [{"Platform": name, "Found": d.get("found", 0), "Unique": d.get("unique", 0),
              "Cross-source dupes": d.get("cross_source_duplicates", 0)}
             for name, d in sorted(platforms.items(), key=lambda kv: -kv[1].get("unique", 0))],
            ["Platform", "Found", "Unique", "Cross-source dupes"],
        )
    if result.get("ats_boards_discovered"):
        term.info(f"Discovered {result['ats_boards_discovered']} new ATS board(s); "
                  "they will be queried directly next scan.")
    if result.get("new_jobs"):
        term.info("\nSee the new jobs with `quackers jobs --new`.")


NO_CV_MESSAGE = (
    "No CV. Add one with `quackers cv <file.pdf|.docx|.json>`, or scan without a CV:\n"
    '  quackers scan --titles "Data Analyst, BI Intern" --type "remote, hybrid" '
    '--location "Berlin, Munich"\n'
    "  (--location may be left out when --type is remote only)"
)

# --type value -> SearchConfig flag
WORKPLACE_TYPES = {
    "remote": "include_remote",
    "hybrid": "include_hybrid",
    "onsite": "include_onsite",
    "on-site": "include_onsite",
    "office": "include_onsite",
}


def parse_workplace_types(text: str) -> List[str]:
    """'Remote, hybrid' -> ['remote', 'hybrid']. Raises CliError on unknown values."""
    types = [t.lower() for t in _split_list(text)]
    unknown = [t for t in types if t not in WORKPLACE_TYPES]
    if unknown:
        raise CliError(f"Unknown --type {', '.join(unknown)}: use remote, hybrid and/or onsite.")
    return list(dict.fromkeys(WORKPLACE_TYPES[t] for t in types))


def apply_scan_overrides(cfg, titles: List[str], locations: List[str],
                         workplace: Optional[List[str]] = None):
    """Apply one-off --titles / --type / --location to a config (not persisted)."""
    if titles:
        cfg.primary_title = titles[0]
        cfg.title_synonyms = titles[1:]
        # The query planner builds role queries from include_keywords.
        cfg.include_keywords = list(dict.fromkeys([*titles, *cfg.include_keywords]))
    if locations:
        cfg.locations = locations
    if workplace:
        for flag in set(WORKPLACE_TYPES.values()):
            setattr(cfg, flag, flag in workplace)
        if workplace == ["include_remote"] and not locations:
            cfg.worldwide_remote = True
    return cfg


def cmd_scan(args) -> None:
    titles = _split_list(args.titles or "")
    locations = _split_list(args.location or "")
    workplace = parse_workplace_types(args.type or "")
    remote_only = workplace == ["include_remote"]
    if store.load_active_profile() is None and not (titles and (locations or remote_only)):
        raise CliError(NO_CV_MESSAGE)
    cfg = apply_scan_overrides(load_config(), titles, locations, workplace)
    if args.source:
        cfg.enabled_sources = list(args.source)
    result = _run_scan(cfg, evaluate=not args.no_ai)
    _print_scan_result(result)
    if result.get("status") == "error":
        raise CliError("Scan finished with errors (see above).")


# ---------------------------------------------------------------------------
# jobs / job
# ---------------------------------------------------------------------------


def _load_intent(cfg):
    from config.search_intent import build_intent

    try:
        return build_intent(cfg, include_related_titles=bool(cfg.include_related_roles))
    except Exception:  # noqa: BLE001
        return None


def explain_job(row: Dict[str, Any], intent) -> str:
    """Short evidence-based 'why included' string for a job row."""
    from services.location_validation import location_verdict, parse_location
    from services.title_relevance import classify_title

    parts: List[str] = []
    if intent is not None:
        if intent.primary_title:
            label = classify_title(row.get("title", "") or "", intent).label
            parts.append({
                "exact": "exact title", "strong": "strong title", "related": "related title",
            }.get(label, "title unconstrained"))
        loc = row.get("location", "") or ""
        parsed = parse_location(loc)
        if not parsed.is_concrete and loc:
            parts.append("location unverified")
        elif parsed.is_concrete and (intent.cities or intent.countries):
            verdict, _ = location_verdict(parsed, intent)
            parts.append({
                "match": f"location ok ({parsed.city or parsed.country})",
                "unknown": "location unverified",
            }.get(verdict, "location mismatch"))
        elif parsed.remote:
            parts.append("remote")
    src = (row.get("source") or "").lower()
    if "jobboard" in src or "greenhouse" in src or "lever" in src:
        parts.append("official board")
    elif "hackernews" in src or "discord" in src:
        parts.append("community post")
    elif "rss" in src or "feed" in src:
        parts.append("feed")
    return ", ".join(parts) if parts else "—"


def cmd_jobs(args) -> None:
    filtering = args.new or args.search
    rows = store.list_jobs_with_match(
        min_score=args.min_score or 0.0,
        status=args.status,
        source=args.source,
        limit=500 if filtering else max(args.limit, 1),
    )
    if args.new:
        last = store.latest_scan_dict(limit=1)
        if last and last.get("started_at"):
            since = last["started_at"][:10]
            rows = [r for r in rows if (r.get("first_discovered") or "") >= since]
    if args.search:
        needle = args.search.lower()
        rows = [
            r for r in rows
            if needle in f"{r.get('title')} {r.get('company')} {r.get('location')}".lower()
        ]
    if args.sort == "score":
        rows.sort(key=lambda r: (r.get("match_score") is None, -(r.get("match_score") or 0)))
    rows = rows[: max(args.limit, 1)]

    if not rows:
        term.info("No jobs match. Run `quackers scan` or relax the filters.")
        return

    if args.why:
        intent = _load_intent(load_config())
        for r in rows:
            r["why"] = explain_job(r, intent)

    columns = ["id", "match_score", "title", "company", "location", "employment_type", "source"]
    if args.why:
        columns.append("why")
    term.table(
        rows,
        columns,
        headers={"match_score": "score", "employment_type": "type"},
        max_widths={"title": 45, "company": 24, "location": 22, "source": 14, "why": 50},
    )
    term.info(f"\n{len(rows)} job(s). Details: `quackers job <id>`.")


def _print_match_report(report: Dict[str, Any]) -> None:
    """Readable category scores and full source evidence, with unknowns kept distinct."""
    if report.get("scoring_method") != "weighted-evidence-v2":
        term.kv(report.items(), indent=4)
        return
    term.kv([
        ("Scoring status", report.get("scoring_status")),
        ("Method", report.get("scoring_method")),
        ("Confidence", report.get("confidence")),
        ("Evidence coverage", f"{100 * report.get('evidence_coverage', 0):.0f}%"),
        ("Possible score range", report.get("score_range")),
        ("Fallback reason", report.get("fallback_reason") or "none"),
    ])
    categories = []
    for name, category in report.get("category_scores", {}).items():
        categories.append({"category": name.replace("_", " "),
            "score": category["score"] if category.get("score") is not None else "unknown" if category.get("applicable") else "n/a",
            "weight": category.get("weight"), "known": category.get("known"), "unknown": category.get("unknown")})
    term.table(categories, ["category", "score", "weight", "known", "unknown"])
    term.paragraph(report.get("explanation") or "")
    for row in report.get("requirement_matrix", []):
        term.paragraph(f"{row['status']} [{row['category']}] {row['text']}", indent=4)
        term.paragraph(f"Job evidence: {row.get('source_quote') or row['text']}", indent=6)
        for fact in row.get("evidence", []):
            term.paragraph(f"Candidate evidence ({fact['field']}): {fact['quote']}", indent=6)
            term.paragraph(f"Source: {fact.get('source', 'profile')}; provenance: {fact.get('status', 'unknown')}", indent=6)
    for key, label in (("transferable_skills", "Transferable evidence"), ("potential_weaknesses", "Potential weaknesses")):
        if report.get(key):
            term.info(label + ":")
            for item in report[key]:
                term.paragraph(item, indent=4)


def cmd_job(args) -> None:
    with store.with_session() as session:
        job = _require_job(session, args.job_id)
        profile = db.get_active_candidate_profile(session)
        match = db.get_match_for_job(
            session, job.id, profile_version=profile.profile_version if profile else None,
            candidate_profile_id=profile.id if profile else None,
        )
        app = db.get_application_state(session, job.id)
        data = store.job_to_dict(job)
        description = job.description or ""
        required = list(job.required_skills or [])
        preferred = list(job.preferred_skills or [])
        reqs = [
            ("Education", job.education_requirements),
            ("Experience", job.experience_requirements),
            ("Languages", job.language_requirements),
            ("Must speak", ", ".join(job.required_languages) if job.required_languages_json else None),
            ("Written in", job.description_language),
        ]
        app_row = store.application_row(app, job) if app else None
        notes = app.notes if app else ""

    term.heading(f"#{data['id']}  {data['title']}")
    term.kv([
        ("Company", data["company"]),
        ("Location", f"{data['location'] or 'n/a'} ({data['country'] or 'n/a'})"),
        ("Type", f"{data['employment_type']} · {data['workplace_arrangement']}"),
        ("Salary", data["salary"] or "—"),
        ("Published", data["published_at"] or "—"),
        ("Source", data["source"]),
        ("First seen", data["first_discovered"]),
        ("Last seen", data["last_seen"]),
        ("Original URL", data["original_url"]),
        ("Apply URL", data["application_url"] or "—"),
        ("Extraction status", data.get("extraction_status") or "not assessed"),
        ("Extraction method", data.get("extraction_method") or "unknown"),
    ])

    term.heading("Match")
    if match is None:
        term.info("  Not scored yet (run a scan with an active profile).")
    else:
        score = max(0.0, min(100.0, float(match.match_score))) if match.match_score is not None else None
        verdict = "insufficient information" if score is None else "strong" if score >= 80 else "moderate" if score >= 60 else "weak"
        term.kv([
            ("Score", f"{score:.0f}/100 ({verdict})" if score is not None else "Unavailable: insufficient information"),
            ("Model", match.ai_model or "—"),
            ("Profile version", match.profile_version),
            ("Evaluated", store.dt_short(match.evaluated_at)),
        ])
        breakdown = _json_get(match.score_json) or {}
        if isinstance(breakdown, dict) and breakdown:
            term.info("  Breakdown:")
            _print_match_report(breakdown)
        evidence = _json_get(match.matching_evidence_json) or []
        if evidence:
            term.info("  Why:")
            term.bullets(evidence, indent=4)
        missing = _json_get(match.missing_requirements_json) or []
        if missing:
            term.info("  Missing:")
            term.bullets(missing, indent=4)

    if required or preferred or any(v for _, v in reqs):
        term.heading("Requirements")
        if required:
            term.kv([("Required skills", ", ".join(required))])
        if preferred:
            term.kv([("Preferred skills", ", ".join(preferred))])
        term.kv([(k, v) for k, v in reqs if v])

    term.heading("Application")
    if app_row is None:
        term.info(f"  Not tracked yet. Start with `quackers app set {data['id']} SHORTLISTED`.")
    else:
        term.kv([
            ("Status", app_row["status_label"]),
            ("Follow-up", app_row["follow_up_at"] or "—"),
            ("Interview", app_row["interview_status"] or "—"),
        ])
        if notes:
            term.info("  Notes:")
            term.paragraph(notes, indent=4)

    docs = store.list_documents_for_job(data["id"])
    if docs:
        term.heading("Documents")
        term.table(docs, ["id", "doc_type", "created_at", "version", "approved", "file_path"],
                   max_widths={"file_path": 60})

    if not args.no_description:
        term.heading("Description")
        if description.strip():
            term.paragraph(description)
        else:
            term.info("  No description captured.")


def cmd_open(args) -> None:
    import webbrowser
    from urllib.parse import urlparse

    with store.with_session() as session:
        job = _require_job(session, args.job_id)
        url = (job.application_url if args.apply else None) or job.original_url
    if urlparse(url or "").scheme not in {"http", "https"}:
        raise CliError(f"Refusing to open a non-http(s) URL: {term.clean(url)!r}")
    term.info(f"Opening {url}")
    webbrowser.open(url)


# ---------------------------------------------------------------------------
# app (application tracker)
# ---------------------------------------------------------------------------


def cmd_app_list(args) -> None:
    rows = store.list_applications_with_jobs()
    if args.status:
        rows = [r for r in rows if r["app_status"] == args.status]
    if not rows:
        term.info("No tracked applications. Start with `quackers app set <job-id> SHORTLISTED`.")
        return
    term.table(
        rows,
        ["id", "status_label", "company", "title", "applied_at", "follow_up_at", "interview_status"],
        headers={"id": "job", "status_label": "status", "applied_at": "applied",
                 "follow_up_at": "follow-up", "interview_status": "interview"},
        max_widths={"title": 40, "company": 24},
    )


def cmd_app_followups(args) -> None:
    rows = store.upcoming_followups(window_days=args.days)
    if not rows:
        term.info(f"No follow-ups in the next {args.days} days.")
        return
    term.table(rows, ["id", "follow_up_at", "status_label", "company", "title"],
               headers={"id": "job", "follow_up_at": "follow-up", "status_label": "status"},
               max_widths={"title": 45, "company": 24})


def _with_application(job_id: int, action) -> None:
    """Run `action(session, app)` on the job's application, creating it if needed."""
    with store.with_session() as session:
        _require_job(session, job_id)
        app = db.get_application_state(session, job_id) or db.create_application(
            session, job_id=job_id
        )
        action(session, app)


def cmd_app_set(args) -> None:
    target = args.status
    if target == "APPLIED" and not args.confirm:
        term.info("Only mark a job as applied after you have submitted the application yourself.")
        if not term.confirm("Did you personally submit this application?"):
            raise CliError("Not marked as applied (pass --confirm to skip this question).")

    def action(session, app) -> None:
        current = app.status
        allowed = NEXT_STATUSES.get(current, []) + TERMINAL
        if target not in allowed and target != current and not args.force:
            term.warn(
                f"{current} → {target} skips the usual order "
                f"(next: {', '.join(allowed)}). Applying anyway."
            )
        if target == "SHORTLISTED":
            db.mark_shortlisted(session, app)
        elif target == "PREPARED":
            db.mark_docs_prepared(session, app)
        elif target == "APPROVED":
            db.mark_approved(session, app)
        elif target == "APPLIED":
            try:
                db.mark_applied(session, app, via="manual")
            except db.DuplicateApplicationError as exc:
                raise CliError(str(exc)) from exc
        else:
            db.transition_application_status(session, app, target)

    _with_application(args.job_id, action)
    term.ok(f"Job {args.job_id} → {store.STATUS_LABEL.get(target, target)}")


def cmd_app_note(args) -> None:
    text = " ".join(args.text).strip()
    if not text:
        raise CliError("Note text is empty.")
    _with_application(args.job_id, lambda s, a: db.add_note(s, a, text))
    term.ok(f"Note added to job {args.job_id}.")


def cmd_app_followup(args) -> None:
    if args.date.lower() in {"none", "clear"}:
        when = None
    else:
        try:
            when = datetime.strptime(args.date, "%Y-%m-%d")
        except ValueError as exc:
            raise CliError("Date must be YYYY-MM-DD (or 'clear').") from exc
    _with_application(args.job_id, lambda s, a: db.set_follow_up(s, a, when))
    term.ok(f"Follow-up for job {args.job_id}: {args.date}")


def cmd_app_interview(args) -> None:
    status = " ".join(args.text).strip()
    if not status:
        raise CliError("Interview status is empty.")
    _with_application(args.job_id, lambda s, a: db.set_interview_status(s, a, status))
    term.ok(f"Interview status for job {args.job_id}: {status}")


# ---------------------------------------------------------------------------
# docs
# ---------------------------------------------------------------------------


def _record_doc(job_id: int, doc_type: str, path: Path, profile, notes: str) -> int:
    with store.with_session() as session:
        doc = db.create_generated_document(
            session,
            job_id=job_id,
            doc_type=doc_type,
            file_path=str(path),
            profile_version=profile.profile_version,
            notes=notes,
        )
        return doc.id


def cmd_docs_cv(args) -> None:
    from services.cv_tailoring import build_cv_tailoring_suggestions
    from services.ai_client import deterministic_score

    profile = _require_active_profile()
    if getattr(args, "tailored", False):
        from services.tailored_cv import build_tailored_cv, digest
        from services import llm
        from config.settings import get_config
        with store.with_session() as session:
            job = _require_job(session, args.job_id)
            ai = llm.get() if get_config().enable_ai_direction else None
            try:
                draft = build_tailored_cv(profile, job, ai=ai)
            except ValueError as exc:
                raise CliError(str(exc)) from exc
        import uuid
        path = store.get_output_dir() / f"tailored_cv_{args.job_id}_{uuid.uuid4().hex[:8]}.json"
        path.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
        doc_id = _record_doc(args.job_id, "tailored_cv", path, profile, json.dumps({"draft_hash": digest(draft)}))
        term.ok(f"CV draft saved as document #{doc_id}: {path}")
        term.info(f"Review: quackers docs show {doc_id}; approve: quackers docs approve {doc_id}; export: quackers docs export {doc_id} cv.docx")
        if args.show:
            term.paragraph(draft["tailored_cv"] + "\nChanges:\n" + "\n".join(draft["changes"]) + "\nUnsupported:\n" + "\n".join(draft["unsupported_requirements"]))
        return
    with store.with_session() as session:
        job = _require_job(session, args.job_id)
        match_data = deterministic_score(profile, job)
        suggestions = build_cv_tailoring_suggestions(profile, job, match_data)

    lines = [f"# CV Tailoring Suggestions — Job {args.job_id}", "",
             f"Generated from profile v{profile.profile_version}", ""]
    for idx, s in enumerate(suggestions, start=1):
        lines += [f"{idx}. **{s.get('field', 'general')}**",
                  f"   - {s.get('action', '')}",
                  f"   - Why: {s.get('reason', '')}", ""]
    path = store.get_output_dir() / f"cv_suggestions_{args.job_id}.txt"
    path.write_text("\n".join(lines), encoding="utf-8")
    doc_id = _record_doc(args.job_id, "cv_suggestions", path, profile,
                         "auto-generated by cv_tailoring")
    term.ok(f"CV suggestions saved (doc #{doc_id}): {path}")
    if args.show:
        term.paragraph("\n".join(lines))


def cmd_docs_letter(args) -> None:
    from services.cover_letter import generate_cover_letter
    from services.ai_client import deterministic_score

    profile = _require_active_profile()
    with store.with_session() as session:
        job = _require_job(session, args.job_id)
        match_data = deterministic_score(profile, job)
        content = generate_cover_letter(profile, job, match_data, tone=args.tone)

    path = store.get_output_dir() / f"cover_letter_{args.job_id}.md"
    path.write_text(content, encoding="utf-8")
    doc_id = _record_doc(args.job_id, "cover_letter", path, profile, f"tone={args.tone}")
    term.ok(f"Cover letter saved (doc #{doc_id}): {path}")
    if args.show:
        term.paragraph(content)


def cmd_docs_list(args) -> None:
    with store.with_session() as session:
        docs = db.get_doc_list(session, job_id=args.job_id)
        rows = []
        for d in docs:
            job = db.get_job(session, d.job_id)
            rows.append({
                "id": d.id,
                "job": d.job_id,
                "company": job.company if job else "(deleted)",
                "doc_type": d.doc_type,
                "created": store.dt_local(d.created_at),
                "version": d.profile_version,
                "approved": bool(d.approved),
                "exists": Path(d.file_path).exists() if d.file_path else False,
            })
    if args.type:
        rows = [r for r in rows if r["doc_type"].lower() == args.type.lower()]
    if not rows:
        term.info("No documents yet. Generate one with `quackers docs cover-letter <job-id>`.")
        return
    term.table(rows, ["id", "job", "company", "doc_type", "created", "version", "approved", "exists"],
               max_widths={"company": 24})


def _get_doc(session, doc_id: int):
    from database.models import GeneratedDocument

    doc = session.query(GeneratedDocument).filter_by(id=doc_id).first()
    if doc is None:
        raise CliError(f"Document {doc_id} not found. List them with `quackers docs list`.")
    return doc


def cmd_docs_show(args) -> None:
    with store.with_session() as session:
        doc = _get_doc(session, args.doc_id)
        path = Path(doc.file_path) if doc.file_path else None
    if path is None or not path.exists():
        raise CliError("File is missing on disk. Regenerate it from the job.")
    term.info(f"{path}\n")
    term.paragraph(path.read_text(encoding="utf-8", errors="replace"), indent=0)


def cmd_docs_export(args) -> None:
    with store.with_session() as session:
        doc = _get_doc(session, args.doc_id)
        src = Path(doc.file_path) if doc.file_path else None
    if src is None or not src.exists():
        raise CliError("File is missing on disk. Regenerate it from the job.")
    dest = Path(args.dest).expanduser()
    if dest.is_dir():
        dest = dest / src.name
    if dest.exists() and not term.confirm(f"{dest} exists. Overwrite?", assume_yes=args.yes):
        raise CliError("Not exported.")
    with store.with_session() as session:
        from database.models import CandidateProfile
        protected = []
        for p in session.query(CandidateProfile):
            other = _json_get(p.other_json) or {}
            protected.extend(x for x in (p.cv_path, other.get("original_cv_path") if isinstance(other, dict) else None) if isinstance(x, str) and x)
        if any(Path(p).resolve() == dest.resolve() for p in protected):
            raise CliError("Cannot overwrite an original CV")
    if doc.doc_type == "tailored_cv":
        if not doc.approved:
            raise CliError("Review and approve the CV draft before exporting")
        from services.tailored_cv import digest
        from documents.exporter import export_cv_text
        draft = _validated_draft(doc)
        meta = _json_get(doc.notes) or {}
        if meta.get("approved_hash") != digest(draft):
            raise CliError("Draft changed since approval; review and approve it again")
        try:
            export_cv_text(draft["tailored_cv"], dest)
        except (ValueError, OSError) as exc:
            raise CliError(str(exc)) from exc
    else:
        shutil.copyfile(src, dest)
    term.ok(f"Exported to {dest}")


def cmd_docs_approve(args) -> None:
    with store.with_session() as session:
        doc = _get_doc(session, args.doc_id)
        if doc.doc_type == "tailored_cv":
            from services.tailored_cv import digest
            meta = _json_get(doc.notes) or {}
            meta["approved_hash"] = None if args.undo else digest(_validated_draft(doc))
            doc.notes = json.dumps(meta)
        db.mark_doc_approved(session, doc, approved=not args.undo)
    term.ok(f"Document {args.doc_id} {'unapproved' if args.undo else 'approved'}.")


def _validated_draft(doc):
    from services.tailored_cv import validate_draft
    try:
        draft = json.loads(Path(doc.file_path).read_text(encoding="utf-8"))
        with store.with_session() as session:
            profile = db.get_candidate_profile(session, draft["profile_id"])
            job = _require_job(session, doc.job_id)
            if profile is None or profile.profile_version != doc.profile_version:
                raise ValueError("Source profile is missing or changed")
            validate_draft(draft, profile, job)
        return draft
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise CliError(f"CV draft validation failed: {exc}") from exc


# ---------------------------------------------------------------------------
# profile
# ---------------------------------------------------------------------------


def _profile_form(p) -> Dict[str, Any]:
    """Editable JSON view of a CandidateProfile."""
    return {
        "name": p.name or "",
        "email": p.email or "",
        "phone": p.phone or "",
        "linkedin": p.linkedin or "",
        "website": p.website or "",
        "summary": p.summary or "",
        "skills": list(p.skills),
        "certifications": list(p.certifications),
        "achievements": list(p.achievements),
        "experience": p.experience,
        "education": p.education,
        "projects": p.projects,
        "languages": p.languages,
        "cv_path": p.cv_path or "",
        "other": _json_get(p.other_json) or {},
    }


def _form_to_fields(form: Dict[str, Any]) -> Dict[str, Any]:
    def entries(key: str) -> List[Dict[str, Any]]:
        value = form.get(key) or []
        if isinstance(value, str):
            value = value.splitlines()
        return [dict(v) if isinstance(v, dict) else {"text": str(v).strip()} for v in value if v]
    def as_list(key: str) -> List[str]:
        value = form.get(key) or []
        if isinstance(value, str):
            return _split_list(value)
        return [str(v).strip() for v in value if str(v).strip()]

    def as_lines(key: str) -> List[str]:
        value = form.get(key) or []
        if isinstance(value, str):
            return [ln.strip() for ln in value.splitlines() if ln.strip()]
        return [str(v).strip() for v in value if str(v).strip()]

    name = str(form.get("name") or "").strip()
    if not name:
        raise CliError("Profile name is required.")
    return {
        "name": name,
        "email": form.get("email") or None,
        "phone": form.get("phone") or None,
        "linkedin": form.get("linkedin") or None,
        "website": form.get("website") or None,
        "summary": form.get("summary") or None,
        "skills": as_list("skills"),
        "certifications": as_list("certifications"),
        "achievements": as_list("achievements"),
        "experience": entries("experience"),
        "education": entries("education"),
        "projects": entries("projects"),
        "languages": entries("languages"),
        "cv_path": form.get("cv_path") or None,
        "other": form.get("other") if isinstance(form.get("other"), dict) and form.get("other") else None,
    }


def _get_profile(profile_id: Optional[int]):
    if profile_id is None:
        return _require_active_profile()
    with store.with_session() as session:
        p = db.get_candidate_profile(session, profile_id)
    if p is None:
        raise CliError(f"Profile {profile_id} not found. See `quackers profile list`.")
    return p


def cmd_profile_list(args) -> None:
    from database.models import CandidateProfile

    with store.with_session() as session:
        rows = [
            {"id": p.id, "name": p.name, "email": p.email, "version": p.profile_version,
             "active": bool(p.active), "skills": len(p.skills),
             "updated": store.dt_local(p.updated_at)}
            for p in session.query(CandidateProfile).order_by(CandidateProfile.id.desc()).all()
        ]
    if not rows:
        term.info("No profiles. Create one with `quackers profile import <cv.pdf|cv.docx>`.")
        return
    term.table(rows, ["id", "name", "email", "version", "active", "skills", "updated"])


def cmd_profile_show(args) -> None:
    p = _get_profile(args.profile_id)
    form = _profile_form(p)
    term.heading(f"Profile #{p.id} — {p.name} (v{p.profile_version}{', active' if p.active else ''})")
    term.kv([(k, form[k]) for k in ("email", "phone", "linkedin", "website", "cv_path") if form[k]])
    if form["summary"]:
        term.info("  Summary:")
        term.paragraph(form["summary"], indent=4)
    for key in ("skills", "certifications", "achievements"):
        if form[key]:
            term.kv([(key.capitalize(), ", ".join(form[key]))])
    for key in ("experience", "education", "projects", "languages"):
        if form[key]:
            term.info(f"  {key.capitalize()}:")
            term.bullets(form[key], indent=4)
    for key, val in (form.get("other") or {}).items():
        if key.startswith("_") or key == "provenance":
            continue
        if val:
            term.kv([(key.replace("_", " ").capitalize(),
                      ", ".join(map(str, val)) if isinstance(val, list) else val)])


def _store_cv_copy(src: Path) -> Path:
    from config.settings import get_config
    from services.sanitization import safe_join, sanitize_filename

    base = get_config().data_dir / "cv"
    base.mkdir(parents=True, exist_ok=True)
    name = sanitize_filename(src.name, default="cv.pdf")
    target = safe_join(base, name)
    stem, dot, ext = name.rpartition(".")
    n = 1
    while target.exists():
        target = safe_join(base, f"{stem}_{n}.{ext}" if dot else f"{name}_{n}")
        n += 1
    shutil.copyfile(src, target)
    return target


def cmd_profile_import(args) -> None:
    from services import candidate_profile as cp
    from services import cv_parser

    src = Path(args.cv_file).expanduser()
    if not src.is_file():
        raise CliError(f"File not found: {src}")
    if src.stat().st_size > MAX_CV_BYTES:
        raise CliError("File too large (max 15 MB).")

    saved = _store_cv_copy(src)
    try:
        extraction = cv_parser.parse_cv(saved)
    except Exception as exc:  # noqa: BLE001
        raise CliError(f"Could not parse CV: {exc}") from exc

    if extraction.get("method") == "json":
        form = dict(extraction["fields"], cv_path=str(saved))
        form.setdefault("other", {}).update(_source_text=extraction["raw_text"], _source_path=str(saved))
    else:
        form = _extraction_form(extraction, saved, src.name)
    form.setdefault("other", {})["original_cv_path"] = str(src.resolve())
    term.ok(f"Parsed {src.name} (copy stored at {saved}).")
    _review_and_save(form, edit=args.edit, yes=args.yes, inactive=args.inactive)


def _extraction_form(extraction: Dict[str, Any], saved: Path, label: str) -> Dict[str, Any]:
    """Form from a parse_cv() result; refuses unreadable PDFs (never an empty profile)."""
    from services.cv_validation import readable_text_problem

    problem = readable_text_problem(extraction.get("raw_text") or "")
    notes = list(extraction.get("notes") or [])
    if problem:
        extra = "".join(f"\n  ({n})" for n in notes)
        raise CliError(
            f"Could not read {label}: {problem}{extra}\n"
            "Nothing was saved. Use the JSON template instead:\n"
            "  quackers cv-template          (creates it and opens your editor)\n"
            "  quackers cv <filled.json>     (validates, previews, asks before saving)")
    fields = extraction.get("fields", {}) or {}

    def raw_lines(key: str) -> List[str]:
        return [ln.strip() for ln in (fields.get(key) or "").splitlines() if ln.strip()]

    form = {
        "name": fields.get("name") or "Unknown",
        "email": fields.get("email") or "",
        "phone": fields.get("phone") or "",
        "linkedin": fields.get("linkedin") or "",
        "website": fields.get("website") or "",
        "summary": fields.get("summary") or "",
        "skills": _split_list(fields.get("_skills_raw") or ""),
        "certifications": _split_list(fields.get("_certifications_raw") or ""),
        "achievements": _split_list(fields.get("_achievements_raw") or ""),
        # Entries group a job's title line with its bullet details.
        "experience": fields.get("structured_experience") or fields.get("experience_entries") or raw_lines("_experience_raw"),
        "education": fields.get("structured_education") or fields.get("education_entries") or raw_lines("_education_raw"),
        "projects": raw_lines("_projects_raw"),
        "languages": raw_lines("_languages_raw"),
        "cv_path": str(saved),
        "other": {"_source_text": extraction.get("raw_text") or "", "_source_path": str(saved)},
    }
    if form["name"] == "Unknown":
        form["name"] = ""  # never save a made-up name
    for note in notes:
        term.warn(note)
    # Shown in the preview only; removed before editing/saving.
    form["_confidence"] = dict(extraction.get("confidence") or {})
    return form


STATUS_STYLE = {"ok": "green", "missing": "dim", "suspect": "yellow"}


def _print_cv_preview(form: Dict[str, Any], validation,
                      confidence: Optional[Dict[str, str]] = None) -> None:
    rows = []
    confidence = confidence or {}
    for key, status in validation.status.items():
        val = form.get(key)
        if isinstance(val, list):
            shown = (f"{len(val)} item(s): " + "; ".join(map(str, val[:3]))) if val else ""
        else:
            shown = str(val or "")
        row = {"field": key, "status": term.style(status, STATUS_STYLE.get(status, "")),
               "value": shown}
        if confidence:
            row["confidence"] = confidence.get(key, "")
        rows.append(row)
    for key, val in (form.get("other") or {}).items():
        rows.append({"field": key, "status": "extra",
                     "value": ", ".join(map(str, val)) if isinstance(val, list) else str(val)})
    cols = ["field", "status"] + (["confidence"] if confidence else []) + ["value"]
    term.table(rows, cols, max_widths={"value": 70})
    for e in validation.errors:
        term.error(e)
    for w in validation.warnings:
        term.warn(w)


def _review_and_save(form: Dict[str, Any], *, edit: bool = False, yes: bool = False,
                     inactive: bool = False, save: bool = True) -> Optional[Any]:
    """Validate -> preview -> (fix in editor) -> confirm -> save as a NEW profile.

    Validation errors always block saving, even with --yes.  The previous
    profile row is never modified; it is only deactivated when the new one
    becomes active.
    """
    from services import candidate_profile as cp
    from services.cv_validation import validate_form

    confidence = form.pop("_confidence", None)
    if edit:
        try:
            form = term.edit_json(form, label="profile") or form
        except ValueError as exc:
            raise CliError(str(exc)) from exc
    while True:
        validation = validate_form(form)
        term.heading("CV preview")
        _print_cv_preview(form, validation, confidence)
        if not save:
            if validation.ok:
                term.ok("Looks good — nothing saved (check only).")
            return None
        if validation.ok:
            break
        if not yes and sys.stdin.isatty() and term.confirm("\nFix these in your editor now?"):
            try:
                form = term.edit_json(form, label="profile") or form
            except ValueError as exc:
                raise CliError(str(exc)) from exc
            continue
        raise CliError("Not saved: fix the errors above (re-run with --edit).")

    active = store.load_active_profile()
    if active is not None and not inactive:
        question = (f"\nSave as a new profile and make it active? "
                    f"(current #{active.id} {active.name} is kept, just deactivated)")
    else:
        question = "\nSave as a new profile?"
    if not term.confirm(question, assume_yes=yes):
        raise CliError("Not saved.")
    from services.candidate_intelligence import provenance
    fields = _form_to_fields(form)
    other = dict(fields.get("other") or {})
    source_text = other.pop("_source_text", "")
    source_path = other.pop("_source_path", fields.get("cv_path") or "user input")
    other["provenance"] = provenance(fields, raw_text=source_text, source=source_path, confirmed=True)
    fields["other"] = other
    p = cp.upsert_from_extraction(fields, active=not inactive)
    term.ok(f"Created profile #{p.id} (v{p.profile_version}){'' if inactive else ', active'}.")
    return p


def cmd_profile_edit(args) -> None:
    from services import candidate_profile as cp

    p = _get_profile(args.profile_id)
    try:
        edited = term.edit_json(_profile_form(p), label="profile")
    except ValueError as exc:
        raise CliError(str(exc)) from exc
    if edited is None:
        term.info("No changes.")
        return
    updated = cp.upsert_profile(p.id, **_form_to_fields(edited))
    term.ok(f"Saved profile #{updated.id} (v{updated.profile_version}).")


CV_JSON_KEYS = ("name", "email", "phone", "linkedin", "website", "summary", "skills",
                "certifications", "achievements", "experience", "education", "projects",
                "languages")


def _load_cv_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CliError(f"Could not read JSON CV: {exc}") from exc
    if not isinstance(data, dict):
        raise CliError("JSON CV must be an object, e.g. "
                       '{"name": "Ada", "skills": ["Python", "SQL"], "experience": ["..."]}')
    from services.cv_validation import normalize_json_cv

    form, unknown = normalize_json_cv(data)
    if unknown:
        term.warn(f"Ignoring unknown CV fields: {', '.join(unknown)}")
    form["cv_path"] = str(path)
    return form


def cmd_cv(args) -> None:
    """Show the active CV, or load a new one from PDF / DOCX / JSON."""
    if not args.path:
        if store.load_active_profile() is None:
            raise CliError(NO_CV_MESSAGE)
        cmd_profile_show(argparse.Namespace(profile_id=None))
        return

    path = Path(args.path).expanduser().resolve()
    if not path.is_file():
        raise CliError(f"File not found: {path}")
    suffix = path.suffix.lower()
    yes = bool(getattr(args, "yes", False))
    edit = bool(getattr(args, "edit", False))
    if suffix == ".json":
        if path.stat().st_size > MAX_CV_BYTES:
            raise CliError("File too large (max 15 MB).")
        saved = _store_cv_copy(path)
        form = _load_cv_json(saved)
        form.setdefault("other", {}).update(_source_text=saved.read_text(encoding="utf-8-sig"), _source_path=str(saved), original_cv_path=str(path))
        _review_and_save(form, edit=edit, yes=yes)
    elif suffix in {".pdf", ".docx"}:
        cmd_profile_import(argparse.Namespace(
            cv_file=str(path), edit=edit, inactive=False, yes=yes))
    else:
        raise CliError(f"Unsupported CV type {suffix or '(none)'}: use .pdf, .docx or .json.")
    term.info("Next: /scan")


def cmd_profile_activate(args) -> None:
    from services import candidate_profile as cp

    _get_profile(args.profile_id)
    p = cp.upsert_profile(args.profile_id, active=not args.off)
    term.ok(f"Profile #{p.id} {'deactivated (kept in DB)' if args.off else 'activated'}.")


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def _save_config(cfg) -> None:
    from config.runtime_settings import save_search_config

    with store.with_session() as session:
        save_search_config(session, cfg)


def cmd_config_show(args) -> None:
    data = _config_dict(load_config())
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return
    term.kv([(k, ", ".join(map(str, v)) if isinstance(v, list) else term.fmt(v))
             for k, v in sorted(data.items())])


def cmd_config_set(args) -> None:
    from pydantic import ValidationError

    cfg = load_config()
    data = _config_dict(cfg)
    for pair in args.pairs:
        if "=" not in pair:
            raise CliError(f"Expected KEY=VALUE, got {pair!r}.")
        key, raw = pair.split("=", 1)
        data[key.strip()] = _coerce_config_value(cfg, key.strip(), raw)
    try:
        new_cfg = validate_config(data)
    except ValidationError as exc:
        raise CliError(f"Invalid setting: {exc}") from exc
    _save_config(new_cfg)
    term.ok("Search config saved.")


def cmd_config_edit(args) -> None:
    from pydantic import ValidationError

    try:
        edited = term.edit_json(_config_dict(load_config()), label="config")
    except ValueError as exc:
        raise CliError(str(exc)) from exc
    if edited is None:
        term.info("No changes.")
        return
    try:
        cfg = validate_config(edited)
    except ValidationError as exc:
        raise CliError(f"Invalid config, nothing saved: {exc}") from exc
    _save_config(cfg)
    term.ok("Search config saved.")


def cmd_config_profiles(args) -> None:
    names = store.list_search_profile_names()
    if not names:
        term.info("No saved search profiles. Save one with `quackers config save-as <name>`.")
        return
    term.bullets(names, indent=0)


def cmd_config_save_as(args) -> None:
    from config.runtime_settings import save_search_profile

    with store.with_session() as session:
        save_search_profile(session, args.name, load_config())
    term.ok(f"Saved current config as profile {args.name!r}.")


def cmd_config_load(args) -> None:
    from pydantic import ValidationError

    data = store.load_search_profile_dict(args.name)
    if data is None:
        raise CliError(f"No saved profile named {args.name!r}.")
    try:
        cfg = validate_config(data)
    except ValidationError as exc:
        raise CliError(f"Saved profile is invalid: {exc}") from exc
    _save_config(cfg)
    term.ok(f"Profile {args.name!r} is now the live search config.")


def cmd_config_reset(args) -> None:
    from config.search_config import default_search_config

    if not term.confirm("Replace the live search config with the defaults?", assume_yes=args.yes):
        raise CliError("Cancelled.")
    _save_config(default_search_config())
    term.ok("Search config reset to defaults.")


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------


def cmd_sources_list(args) -> None:
    rows = []
    with store.with_session() as session:
        for name in store.list_registered_sources():
            rec = db.get_source_health(session, name)
            rows.append({
                "source": name,
                "enabled": rec.is_enabled if rec else True,
                "found": rec.listings_found if rec else 0,
                "last_ok": store.dt_local(rec.last_success_at) if rec else "",
                "last_try": store.dt_local(rec.last_attempt_at) if rec else "",
                "error": (rec.error_message or "") if rec else "",
            })
    if not rows:
        raise CliError("No sources are registered. Check the `sources/` package.")
    term.table(rows, ["source", "enabled", "found", "last_ok", "last_try", "error"],
               headers={"last_ok": "last ok", "last_try": "last try"},
               max_widths={"error": 60})


def cmd_sources_toggle(args) -> None:
    enabled = args.action == "enable"
    known = store.list_registered_sources()
    if args.name not in known:
        raise CliError(f"Unknown source {args.name!r}. Known: {', '.join(known)}")
    with store.with_session() as session:
        if db.get_source_health(session, args.name) is None:
            db.record_source_health(session, source_name=args.name, is_enabled=enabled)
        db.set_source_enabled(session, args.name, enabled)
    term.ok(f"{args.name}: {'enabled' if enabled else 'disabled'}")


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------


def _duration(start, end) -> str:
    if not start or not end:
        return "—"
    return f"{(end - start).total_seconds():.1f}s"


def cmd_history_list(args) -> None:
    with store.with_session() as session:
        rows = [
            {"id": r.id, "started": store.dt_local(r.started_at), "status": r.status,
             "trigger": r.trigger, "found": r.total_discovered or 0, "new": r.new_jobs or 0,
             "dups": r.duplicates or 0, "duration": _duration(r.started_at, r.completed_at)}
            for r in store.list_scans(session, limit=args.limit)
        ]
    if not rows:
        term.info("No scans yet. Run `quackers scan`.")
        return
    term.table(rows, ["id", "started", "status", "trigger", "found", "new", "dups", "duration"])


def _get_scan(session, scan_id: int):
    from database.models import ScanRecord

    rec = session.query(ScanRecord).filter_by(id=scan_id).first()
    if rec is None:
        raise CliError(f"Scan {scan_id} not found. See `quackers history list`.")
    return rec


def cmd_history_show(args) -> None:
    with store.with_session() as session:
        r = _get_scan(session, args.scan_id)
        cfg = _json_get(r.search_config_json)
        fails = _json_get(r.failures_json)
        term.heading(f"Scan #{r.id}")
        term.kv([
            ("Started", store.dt_local(r.started_at)),
            ("Completed", store.dt_local(r.completed_at) or "—"),
            ("Duration", _duration(r.started_at, r.completed_at)),
            ("Status", r.status),
            ("Trigger", r.trigger),
            ("Discovered", r.total_discovered or 0),
            ("New", r.new_jobs or 0),
            ("Duplicates", r.duplicates or 0),
        ])
    if isinstance(cfg, dict):
        term.heading("Search config")
        term.kv([(k, ", ".join(map(str, v)) if isinstance(v, list) else term.fmt(v))
                 for k, v in sorted(cfg.items()) if v not in (None, [], "")])
    if fails:
        term.heading("Failures")
        for f in fails if isinstance(fails, list) else [fails]:
            if isinstance(f, dict):
                term.bullets([f"{f.get('source', f.get('name', '?'))}: "
                              f"{(f.get('error') or f.get('message') or '')[:200]}"])
            else:
                term.bullets([f])


def cmd_history_rerun(args) -> None:
    from pydantic import ValidationError

    with store.with_session() as session:
        raw = _get_scan(session, args.scan_id).search_config_json
    data = _json_get(raw)
    if not isinstance(data, dict):
        raise CliError("That scan has no stored search config.")
    try:
        cfg = validate_config(data)
    except ValidationError as exc:
        raise CliError(f"Stored config is no longer valid: {exc}") from exc
    _print_scan_result(_run_scan(cfg, evaluate=not args.no_ai))


# ---------------------------------------------------------------------------
# reset
# ---------------------------------------------------------------------------


def cmd_reset(args) -> None:
    from services.reset_service import execute_reset, plan_reset

    with store.with_session() as session:
        plan = plan_reset(session, args.scope).to_dict()

    term.heading("Reset plan: " + ("latest scan only" if args.scope == "current"
                                   else "ALL job-search data"))
    term.kv([
        ("Jobs", plan["jobs"]),
        ("Job matches", plan["job_matches"]),
        ("Applications", plan["applications"]),
        ("Generated documents", plan["generated_documents"]),
        ("Scan records", plan["scan_records"]),
        ("Source health rows", plan["source_health"]),
        ("AI usage rows", plan["ai_usage"]),
        ("Total rows", plan["total"]),
    ])
    term.info("\nKept: candidate profiles, CV files, document files on disk, "
              "search settings and saved search profiles.")
    if plan["total"] == 0:
        term.info("Nothing to remove.")
        return
    if not term.confirm("Delete these rows? This cannot be undone.", assume_yes=args.yes):
        raise CliError("Cancelled. Nothing was deleted.")

    with store.with_session() as session:
        result = execute_reset(session, plan=plan_reset(session, args.scope))
    if not result.success:
        raise CliError(f"Reset failed and was rolled back: {result.error}")
    removed = sum(v for v in (result.deleted or {}).values() if isinstance(v, int))
    term.ok(f"Reset complete. Removed {removed} rows. Run `quackers scan` to repopulate.")


# ---------------------------------------------------------------------------
# env (.env editor)
# ---------------------------------------------------------------------------


def _ask(prompt: str, choices: str) -> str:
    """Single-letter menu choice; EOF / Ctrl+C count as cancel ('c')."""
    try:
        answer = input(prompt).strip().lower()[:1]
    except (EOFError, KeyboardInterrupt):
        print()
        return "c"
    return answer if answer in choices else ""


def _show_env_settings(values: Dict[str, str]) -> None:
    import os

    from quackers import envfile

    rows = []
    for var in envfile.ENV_VARS:
        if var.name in values:
            source, value = ".env", values[var.name]
        elif var.name in os.environ:
            source, value = "OS environment", os.environ[var.name]
        else:
            source, value = "default", None
        shown = envfile.describe_value(var.name, value)
        if value is None and var.default:
            shown = f"default ({var.default})"
        rows.append({"setting": var.name, "value": shown, "from": source,
                     "what": var.description})
    term.table(rows, ["setting", "value", "from", "what"],
               max_widths={"value": 26, "what": 60})
    extra = sorted(set(values) - set(envfile.KNOWN))
    if extra:
        term.info(f"\nOther variables in .env: {', '.join(extra)}")
    term.info("\nEvery setting is optional. OS environment variables override .env.")


def _is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def cmd_env(args) -> None:
    from quackers import envfile

    if not _is_interactive():
        raise CliError(
            "`env` is interactive and needs a terminal. "
            f"To configure non-interactively, edit {envfile.env_path()} "
            "(the README lists every setting).")

    path = envfile.env_path()
    term.heading("Environment configuration")
    term.info("  .env holds API keys and settings (AI providers, web search, storage).")
    term.info("  It is read once at startup; your OS environment variables take precedence.")
    term.kv([("File", str(path))])

    root = path.parent
    if envfile.ensure_gitignored(root):
        term.ok("Added .env to .gitignore so it is never committed.")
    if envfile.git_tracks(path):
        term.warn(
            ".env is TRACKED by git, so its secrets may already be in the repository history. "
            "Run `git rm --cached .env` and commit, then rotate any keys it contained "
            "(.gitignore alone does not untrack a file). Nothing was changed automatically.")

    if path.exists():
        try:
            original = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise CliError(f"Cannot read {path}: {exc}") from exc
        while True:
            choice = _ask("\n  [e]dit  [s]how settings  [c]ancel > ", "esc")
            if choice == "s":
                _show_env_settings(envfile.parse(original).values)
            elif choice == "e":
                break
            elif choice == "c":
                term.info("Cancelled. Nothing was changed.")
                return
        text = original
    else:
        term.info("\n  No .env file yet.")
        if not term.confirm("  Create it from the template (all settings commented out)?"):
            term.info("Cancelled. Nothing was created.")
            return
        original = None
        text = envfile.template()

    old_values = envfile.parse(original).values if original is not None else {}
    while True:
        term.info("  Opening your editor… save and close it to continue.")
        try:
            edited = term.edit_text(text, label="env", suffix=".env")
        except ValueError as exc:
            raise CliError(f"{exc}\nNothing was saved.") from exc
        if edited is None:
            if original is not None:
                term.info("No changes. Nothing was saved.")
                return
            if not term.confirm("  Template unchanged. Save it as-is?"):
                term.info("Cancelled. Nothing was created.")
                return
            edited = text
        result = envfile.parse(edited)
        for warning in result.warnings:
            term.warn(warning)
        if not result.errors:
            break
        for err in result.errors:
            term.error(err)
        if not term.confirm("  Fix these in the editor?"):
            term.info("Cancelled. The existing file was not changed.")
            return
        text = edited

    try:
        created = envfile.write_atomic(path, edited)
    except OSError as exc:
        raise CliError(f"Could not write {path}: {exc}. Nothing was changed.") from exc

    changes = envfile.summarize_changes(old_values, result.values)
    term.ok(f"{'Created' if created else 'Saved'} {path}")
    labels = {"added": "Added", "changed": "Changed", "removed": "Removed"}
    summary = [(labels[k], ", ".join(v)) for k, v in changes.items() if v]
    if summary:
        term.kv(summary)
    else:
        term.info("  No variables changed (comments / formatting only).")
    secrets = [n for n in result.values if envfile.is_secret(n) and result.values[n]]
    if secrets:
        term.info(f"  Secrets set (values hidden): {', '.join(sorted(secrets))}")
    term.info("  Restart quackers for the changes to take effect.")


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    statuses = list(store.APP_STATUSES)
    # "did you mean …?" for typos exists only on Python 3.14+; the project
    # supports 3.10+, so pass it only where argparse knows it.
    extra = {"suggest_on_error": True} if sys.version_info >= (3, 14) else {}
    p = argparse.ArgumentParser(
        prog="quackers",
        description="HireMeQuackers — local-first AI job-hunting agent.",
        epilog="New here? Run `quackers guide`.",
        **extra,
    )
    sub = p.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    s = sub.add_parser("status", help="overview: counts, last scan, recent jobs, follow-ups")
    s.add_argument("--ai-check", action="store_true", help="also check the AI provider configuration")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("ai-check", help="show the active AI provider/model and every supported provider")
    s.add_argument("--verbose", action="store_true", help="contact the provider using synthetic data")
    s.set_defaults(func=cmd_ai_check)
    s = sub.add_parser("ai-test", help="test an AI capability independently with synthetic data")
    s.add_argument("capability", choices=["extraction", "matching", "tailoring"])
    s.set_defaults(func=cmd_ai_test)
    s = sub.add_parser("job-extract", help="retrieve a stored job's description and show extraction diagnostics")
    s.add_argument("job_id", type=int)
    s.add_argument("--force", action="store_true", help="refresh before the normal cache TTL")
    s.set_defaults(func=cmd_job_extract)
    s = sub.add_parser("match", help="score stored jobs using the active candidate profile")
    s.add_argument("job_id", type=int, nargs="?")
    s.set_defaults(func=cmd_match)

    s = sub.add_parser("scan", help="discover new jobs with the live search config")
    s.add_argument("--no-ai", action="store_true", help="skip AI scoring (deterministic only)")
    s.add_argument("--source", action="append", metavar="NAME",
                   help="only scan this source (repeatable)")
    s.add_argument("--titles", metavar='"A, B"',
                   help="job titles to search for, comma-separated (required without a CV)")
    s.add_argument("--type", metavar='"remote, hybrid"',
                   help="workplace type(s): remote, hybrid, onsite (comma-separated)")
    s.add_argument("--location", metavar='"X, Y"',
                   help="locations, comma-separated (required without a CV unless --type remote)")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("cv", help="load your CV (PDF / DOCX / JSON), or show the current one")
    s.add_argument("path", nargs="?", help="CV file; omit to show the active CV")
    s.add_argument("--edit", action="store_true", help="fix the extracted fields in $EDITOR first")
    s.add_argument("-y", "--yes", action="store_true",
                   help="don't ask before saving (validation errors still block)")
    s.set_defaults(func=cmd_cv)

    s = sub.add_parser("jobs", help="list discovered jobs")
    s.add_argument("--min-score", type=float, help="hide jobs scored below this (0-100)")
    s.add_argument("--status", help="job status filter (e.g. OPEN)")
    s.add_argument("--source", help="only jobs from this source")
    s.add_argument("--search", help="text filter on title / company / location")
    s.add_argument("--new", action="store_true", help="only jobs found since the latest scan")
    s.add_argument("--sort", choices=["date", "score"], default="date")
    s.add_argument("--why", action="store_true", help="explain why each job was included")
    s.add_argument("--limit", type=int, default=50)
    s.set_defaults(func=cmd_jobs)

    s = sub.add_parser("job", help="show one job: details, match, application, description")
    s.add_argument("job_id", type=int)
    s.add_argument("--no-description", action="store_true")
    s.set_defaults(func=cmd_job)

    s = sub.add_parser("open", help="open a job posting in the browser")
    s.add_argument("job_id", type=int)
    s.add_argument("--apply", action="store_true", help="open the application URL instead")
    s.set_defaults(func=cmd_open)

    # app
    app = sub.add_parser("app", help="application tracker").add_subparsers(
        dest="app_command", metavar="<action>")
    app.required = True
    s = app.add_parser("list", help="all tracked applications")
    s.add_argument("--status", type=str.upper, choices=statuses)
    s.set_defaults(func=cmd_app_list)
    s = app.add_parser("set", help="change a job's application status")
    s.add_argument("job_id", type=int)
    s.add_argument("status", type=str.upper, choices=statuses)
    s.add_argument("--confirm", action="store_true",
                   help="confirm you submitted the application yourself (for APPLIED)")
    s.add_argument("--force", action="store_true", help="don't warn about out-of-order moves")
    s.set_defaults(func=cmd_app_set)
    s = app.add_parser("note", help="append a note")
    s.add_argument("job_id", type=int)
    s.add_argument("text", nargs="+")
    s.set_defaults(func=cmd_app_note)
    s = app.add_parser("follow-up", help="set a follow-up date (YYYY-MM-DD or 'clear')")
    s.add_argument("job_id", type=int)
    s.add_argument("date")
    s.set_defaults(func=cmd_app_followup)
    s = app.add_parser("interview", help="set the interview status text")
    s.add_argument("job_id", type=int)
    s.add_argument("text", nargs="+")
    s.set_defaults(func=cmd_app_interview)
    s = app.add_parser("followups", help="upcoming follow-ups")
    s.add_argument("--days", type=int, default=14)
    s.set_defaults(func=cmd_app_followups)

    # docs
    docs = sub.add_parser("docs", help="generated CV suggestions and cover letters").add_subparsers(
        dest="docs_command", metavar="<action>")
    docs.required = True
    s = docs.add_parser("list", help="list generated documents")
    s.add_argument("--job", dest="job_id", type=int)
    s.add_argument("--type", help="cv_suggestions or cover_letter")
    s.set_defaults(func=cmd_docs_list)
    s = docs.add_parser("cv", help="generate CV tailoring suggestions for a job")
    s.add_argument("job_id", type=int)
    s.add_argument("--show", action="store_true", help="print the result")
    s.add_argument("--tailored", action="store_true", help="create a factual CV draft with comparison and approval")
    s.set_defaults(func=cmd_docs_cv)
    s = docs.add_parser("cover-letter", help="generate a cover letter for a job")
    s.add_argument("job_id", type=int)
    s.add_argument("--tone", choices=["professional", "casual", "confident"], default="professional")
    s.add_argument("--show", action="store_true", help="print the result")
    s.set_defaults(func=cmd_docs_letter)
    s = docs.add_parser("show", help="print a document")
    s.add_argument("doc_id", type=int)
    s.set_defaults(func=cmd_docs_show)
    s = docs.add_parser("export", help="copy a document to a file or folder")
    s.add_argument("doc_id", type=int)
    s.add_argument("dest")
    s.add_argument("-y", "--yes", action="store_true", help="overwrite without asking")
    s.set_defaults(func=cmd_docs_export)
    s = docs.add_parser("approve", help="mark a document approved")
    s.add_argument("doc_id", type=int)
    s.add_argument("--undo", action="store_true")
    s.set_defaults(func=cmd_docs_approve)

    # profile
    prof = sub.add_parser("profile", help="candidate profile / CV").add_subparsers(
        dest="profile_command", metavar="<action>")
    prof.required = True
    s = prof.add_parser("list", help="all profiles")
    s.set_defaults(func=cmd_profile_list)
    s = prof.add_parser("show", help="show a profile (default: active)")
    s.add_argument("profile_id", type=int, nargs="?")
    s.set_defaults(func=cmd_profile_show)
    s = prof.add_parser("import", help="parse a PDF/DOCX CV into a new profile")
    s.add_argument("cv_file")
    s.add_argument("--edit", action="store_true", help="review the fields in your editor first")
    s.add_argument("--inactive", action="store_true", help="don't make it the active profile")
    s.add_argument("-y", "--yes", action="store_true", help="save without asking")
    s.set_defaults(func=cmd_profile_import)
    s = prof.add_parser("edit", help="edit a profile in $EDITOR (default: active)")
    s.add_argument("profile_id", type=int, nargs="?")
    s.set_defaults(func=cmd_profile_edit)
    s = prof.add_parser("activate", help="make a profile active")
    s.add_argument("profile_id", type=int)
    s.set_defaults(func=cmd_profile_activate, off=False)
    s = prof.add_parser("deactivate", help="deactivate a profile (keeps it)")
    s.add_argument("profile_id", type=int)
    s.set_defaults(func=cmd_profile_activate, off=True)

    # config
    cfg = sub.add_parser("config", help="search settings").add_subparsers(
        dest="config_command", metavar="<action>")
    cfg.required = True
    s = cfg.add_parser("show", help="print the live search config")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_config_show)
    s = cfg.add_parser("set", help="set values: KEY=VALUE ... (lists are comma-separated)")
    s.add_argument("pairs", nargs="+", metavar="KEY=VALUE")
    s.set_defaults(func=cmd_config_set)
    s = cfg.add_parser("edit", help="edit the whole config as JSON in $EDITOR")
    s.set_defaults(func=cmd_config_edit)
    s = cfg.add_parser("profiles", help="list saved search profiles")
    s.set_defaults(func=cmd_config_profiles)
    s = cfg.add_parser("save-as", help="save the live config under a name")
    s.add_argument("name")
    s.set_defaults(func=cmd_config_save_as)
    s = cfg.add_parser("load", help="make a saved profile the live config")
    s.add_argument("name")
    s.set_defaults(func=cmd_config_load)
    s = cfg.add_parser("reset", help="restore the default config")
    s.add_argument("-y", "--yes", action="store_true")
    s.set_defaults(func=cmd_config_reset)

    # sources
    src = sub.add_parser("sources", help="job sources and their health").add_subparsers(
        dest="sources_command", metavar="<action>")
    src.required = True
    s = src.add_parser("list", help="health of every registered source")
    s.set_defaults(func=cmd_sources_list)
    for action in ("enable", "disable"):
        s = src.add_parser(action, help=f"{action} a source")
        s.add_argument("name")
        s.set_defaults(func=cmd_sources_toggle, action=action)

    # history
    hist = sub.add_parser("history", help="past scans").add_subparsers(
        dest="history_command", metavar="<action>")
    hist.required = True
    s = hist.add_parser("list", help="recent scans")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_history_list)
    s = hist.add_parser("show", help="one scan with its config and failures")
    s.add_argument("scan_id", type=int)
    s.set_defaults(func=cmd_history_show)
    s = hist.add_parser("rerun", help="run a new scan with an old scan's config")
    s.add_argument("scan_id", type=int)
    s.add_argument("--no-ai", action="store_true")
    s.set_defaults(func=cmd_history_rerun)

    s = sub.add_parser("env", help="create or edit the .env file (API keys, models, providers)")
    s.set_defaults(func=cmd_env)

    s = sub.add_parser("reset", help="delete job-search data (asks first)")
    s.add_argument("--scope", choices=["current", "all"], default="current",
                   help="current = latest scan only (default); all = every job-search row")
    s.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    s.set_defaults(func=cmd_reset)

    from quackers import commands_extra

    commands_extra.register(sub)
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    term.setup_stdout()
    argv = list(sys.argv[1:] if argv is None else argv)

    from services.logger import setup_logging

    if not argv:  # bare `quackers` opens the interactive session
        setup_logging()
        store.ensure_db()
        from quackers import launcher, shell

        if launcher.should_launch_new_terminal():
            launcher.launch()
            return 0
        return shell.run()

    args = build_parser().parse_args(argv)
    setup_logging()
    store.ensure_db()
    try:
        args.func(args)
    except CliError as exc:
        term.error(str(exc))
        return 1
    except KeyboardInterrupt:
        term.error("Interrupted.")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())

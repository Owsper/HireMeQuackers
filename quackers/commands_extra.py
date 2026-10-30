"""Convenience commands: guide, show, cv-scan / cv-check / cv-template,
boards, platforms and export.

They reuse the handlers in :mod:`quackers.cli`; ``register(sub)`` is
called from ``cli.build_parser`` so the interactive shell's /help and
Tab completion pick them up automatically.
"""
from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path
from typing import Any, Dict, List

from quackers import store, term

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_JSON = PROJECT_ROOT / "config" / "cv_template.json"
TEMPLATE_HELP = PROJECT_ROOT / "config" / "cv_template.md"


def _cli():
    from quackers import cli

    return cli


def _run(argv: List[str]) -> None:
    """Run another quackers command in-process (same handlers, same errors)."""
    args = _cli().build_parser().parse_args(argv)
    args.func(args)


# ---------------------------------------------------------------------------
# guide
# ---------------------------------------------------------------------------

GUIDE = [
    ("1. Set up (once)", [
        ("quackers env", "API keys / search provider (all optional; Gemini too)"),
        ("quackers cv my_cv.pdf", "load your CV — preview + confirmation before saving"),
        ("quackers cv-template", "PDF unreadable? fill a JSON CV in your editor instead"),
        ('quackers config set locations="Berlin, Munich" include_remote=true', "where to search"),
        ("quackers config set search_languages=de,en", "extra search languages"),
        ("quackers config set description_languages=en", "only jobs written in these languages"),
    ]),
    ("2. Find jobs", [
        ("quackers scan", "search all sources (ATS boards, job boards, feeds, web search)"),
        ("quackers jobs --new --sort score", "what's new, best matches first"),
        ("quackers job 12", "one job: details, match, description"),
        ("quackers open 12", "open the original posting"),
        ("quackers platforms", "which platforms the last scan found jobs on"),
    ]),
    ("3. Apply & track", [
        ("quackers docs cover-letter 12", "draft a cover letter"),
        ("quackers app set 12 applied", "track an application"),
        ("quackers app followups", "what needs a follow-up"),
    ]),
    ("4. More sources", [
        ("quackers boards discover https://acme.com/careers", "find a company's ATS board"),
        ("quackers boards add ashby:linear", "add an ATS board by hand"),
        ("quackers sources list", "health of every source"),
    ]),
    ("Look things up", [
        ("quackers show cv | config | scan | boards | jobs", "quick views"),
        ("quackers export jobs.csv --min-score 60", "save jobs to CSV / JSON"),
        ("quackers <command> -h", "all options of a command"),
    ]),
]


def cmd_guide(args) -> None:
    for title, rows in GUIDE:
        term.heading(title)
        term.table([{"command": c, "what": w} for c, w in rows], ["command", "what"],
                   max_widths={"command": 62, "what": 60})
    term.info("\nTip: run `quackers` with no arguments for the interactive shell "
              "(Tab completes commands).")


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------

SHOW_TARGETS = {
    "cv": (["cv"], "the active CV / profile"),
    "profiles": (["profile", "list"], "all saved CV profiles"),
    "config": (["config", "show"], "the live search config"),
    "jobs": (["jobs"], "discovered jobs"),
    "new": (["jobs", "--new"], "jobs from the latest scan"),
    "job": (["job"], "one job (needs an id)"),
    "scan": (["history", "show"], "a scan (default: latest)"),
    "scans": (["history", "list"], "recent scans"),
    "sources": (["sources", "list"], "source health"),
    "boards": (["boards", "list"], "ATS boards that get scanned"),
    "platforms": (["platforms"], "per-platform stats of a scan"),
    "apps": (["app", "list"], "tracked applications"),
    "followups": (["app", "followups"], "upcoming follow-ups"),
    "docs": (["docs", "list"], "generated documents"),
}


def cmd_show(args) -> None:
    argv = list(SHOW_TARGETS[args.what][0])
    if args.what == "job":
        if args.id is None:
            raise _cli().CliError("Which job? e.g. `quackers show job 12`.")
        argv.append(str(args.id))
    elif args.what == "scan":
        argv.append(str(args.id if args.id is not None else _latest_scan_id()))
    elif args.what == "platforms" and args.id is not None:
        argv.append(str(args.id))
    _run(argv)


def _latest_scan_id() -> int:
    last = store.latest_scan_dict(limit=1)
    if not last or not last.get("id"):
        raise _cli().CliError("No scans yet. Run `quackers scan`.")
    return int(last["id"])


# ---------------------------------------------------------------------------
# CV: scan (preview) / check / template
# ---------------------------------------------------------------------------


def _parse_file(path: Path) -> Dict[str, Any]:
    from services import cv_parser

    cli = _cli()
    try:
        extraction = cv_parser.parse_cv(path)
    except Exception as exc:  # noqa: BLE001
        raise cli.CliError(f"Could not parse CV: {exc}") from exc
    return cli._extraction_form(extraction, path, path.name)


def _check_path(raw: str) -> Path:
    cli = _cli()
    path = Path(raw).expanduser().resolve()
    if not path.is_file():
        raise cli.CliError(f"File not found: {path}")
    if path.stat().st_size > cli.MAX_CV_BYTES:
        raise cli.CliError("File too large (max 15 MB).")
    return path


def cmd_cv_scan(args) -> None:
    """Parse a CV and show what was found; save only with --save + confirmation."""
    cli = _cli()
    path = _check_path(args.path)
    if path.suffix.lower() == ".json":
        form = cli._load_cv_json(path)
    else:
        form = _parse_file(path)
        if args.save:
            form["cv_path"] = str(cli._store_cv_copy(path))
    cli._review_and_save(form, edit=args.edit, yes=args.yes, save=args.save)
    from services.cv_validation import validate_form

    if not args.save and validate_form(form).ok:
        term.info(f'\nTo save it: quackers cv-scan "{args.path}" --save   (add --edit to fix fields first)')


def cmd_cv_check(args) -> None:
    args.save, args.edit, args.yes = False, False, False
    cmd_cv_scan(args)


def cmd_cv_template(args) -> None:
    """Create a JSON CV from the template, open it, then validate/preview/confirm/save."""
    from config.settings import get_config

    cli = _cli()
    target = Path(args.path).expanduser() if args.path else get_config().data_dir / "cv" / "cv.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not args.path:
        term.info(f"Using your existing {target} (delete it to start from a blank template).")
    elif target.exists():
        raise cli.CliError(f"{target} already exists — load it with `quackers cv {target}`.")
    else:
        shutil.copyfile(TEMPLATE_JSON, target)
        term.ok(f"Created {target}")
    help_path = target.with_name("cv_template_help.md")
    if not help_path.exists():
        shutil.copyfile(TEMPLATE_HELP, help_path)
    term.info(f"Field guide: {help_path}")

    if args.no_edit:
        term.info(f'\nFill it in, then run: quackers cv "{target}"')
        return
    data = json.loads(target.read_text(encoding="utf-8-sig"))
    try:
        edited = term.edit_json(data, label="cv")
    except ValueError as exc:  # no editor / invalid JSON after editing
        term.warn(str(exc))
        term.info(f'\nEdit {target} by hand, then run: quackers cv "{target}"')
        return
    if edited is None:
        term.info(f'No changes. Fill it in later, then run: quackers cv "{target}"')
        return
    target.write_text(json.dumps(edited, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    term.ok(f"Saved {target}")
    cli._review_and_save(cli._load_cv_json(target), yes=False)


# ---------------------------------------------------------------------------
# boards (ATS career portals)
# ---------------------------------------------------------------------------


def _registry_rows():
    from database.models import SourceRegistryEntry

    with store.with_session() as session:
        return [
            {"board": r.domain, "provider": r.ats_type or "", "via": r.discovered_via,
             "active": bool(r.is_active), "added": store.dt_local(r.discovered_at)}
            for r in session.query(SourceRegistryEntry).order_by(SourceRegistryEntry.id.desc()).all()
        ]


def cmd_boards_list(args) -> None:
    from services.ats_detection import API_PROVIDERS
    from sources.registry import ensure_loaded, list_sources

    cli = _cli()
    rows = [{"board": b, "provider": b.partition(":")[0], "via": "config", "active": True, "added": ""}
            for b in cli.load_config().ats_boards]
    rows += _registry_rows()
    ensure_loaded()
    listed = {r["board"].lower() for r in rows}
    for src in list_sources():
        for b in getattr(src, "DEFAULT_BOARDS", None) or []:
            if getattr(src, "provider", "") and b != "all" and f"{src.provider}:{b}".lower() not in listed:
                rows.append({"board": f"{src.provider}:{b}", "provider": src.provider,
                             "via": "built-in", "active": True, "added": ""})
    for r in rows:
        r["scanned"] = "yes" if r["active"] and r["provider"] in API_PROVIDERS else "no"
    term.table(rows, ["board", "provider", "via", "active", "scanned", "added"])
    term.info("\n'scanned: no' = detected only (Workday / iCIMS / SuccessFactors / Jobvite / "
              "Oracle have no documented public API); their pages are still found via web search.")


def _parse_board(entry: str):
    from services.ats_detection import API_PROVIDERS, HTML_ONLY_PROVIDERS, ATSMatch, detect_ats_from_url

    cli = _cli()
    if entry.startswith(("http://", "https://")):
        m = detect_ats_from_url(entry)
        if m is None:
            raise cli.CliError(f"No known ATS in {entry}. Try `quackers boards discover {entry}`.")
        return m
    prov, _, board = entry.partition(":")
    prov = prov.strip().lower()
    if prov not in API_PROVIDERS | HTML_ONLY_PROVIDERS or not board.strip():
        raise cli.CliError("Expected provider:board, e.g. ashby:linear. Providers: "
                           + ", ".join(sorted(API_PROVIDERS)))
    return ATSMatch(prov, board.strip(), "")


def cmd_boards_add(args) -> None:
    from database.models import SourceRegistryEntry
    from services.ats_detection import register_matches

    matches = [_parse_board(e) for e in args.boards]
    new = register_matches(matches, via="manual")
    with store.with_session() as session:  # re-enable boards removed earlier
        for m in matches:
            row = session.query(SourceRegistryEntry).filter_by(domain=m.registry_key).first()
            if row is not None and not row.is_active:
                row.is_active = True
        session.commit()
    term.ok(f"{len(matches)} board(s) active ({new} new): "
            + ", ".join(m.registry_key for m in matches))


def cmd_boards_remove(args) -> None:
    from database.models import SourceRegistryEntry

    cli = _cli()
    keys = {_parse_board(e).registry_key for e in args.boards}
    cfg = cli.load_config()
    kept = [b for b in cfg.ats_boards if b.lower() not in keys]
    if kept != cfg.ats_boards:
        data = cli._config_dict(cfg)
        data["ats_boards"] = kept
        cli._save_config(cli.validate_config(data))
    hit = 0
    with store.with_session() as session:
        for row in session.query(SourceRegistryEntry).filter(SourceRegistryEntry.domain.in_(keys)):
            row.is_active = False  # kept so it is never re-discovered
            hit += 1
        session.commit()
    term.ok(f"Removed {', '.join(sorted(keys))} ({hit} registry row(s) deactivated).")


def _fetch(url: str) -> str:
    from services.robots import is_allowed_by_robots
    from services.safe_requests import safe_get

    if not is_allowed_by_robots(url):
        raise _cli().CliError(f"robots.txt disallows fetching {url}.")
    res = safe_get(url, timeout=15.0)
    if res.status_code != 200 or not res.content:
        raise _cli().CliError(f"Could not fetch {url} (HTTP {res.status_code or res.error}).")
    return res.content.decode("utf-8", errors="replace")


def cmd_boards_discover(args) -> None:
    """Find the ATS behind a company careers page and register it."""
    from services.ats_detection import (
        detect_ats_from_html, detect_ats_from_url, find_careers_link, register_matches,
    )

    url = args.url if args.url.startswith(("http://", "https://")) else "https://" + args.url
    matches = [m for m in [detect_ats_from_url(url)] if m]
    if not matches:
        html = _fetch(url)
        matches = detect_ats_from_html(html, url)
        careers = None if matches else find_careers_link(html, url)
        if careers and careers != url:
            term.info(f"Following careers link: {careers}")
            direct = detect_ats_from_url(careers)
            matches = [direct] if direct else detect_ats_from_html(_fetch(careers), careers)
    if not matches:
        term.warn("No known ATS found. The page may be self-hosted — it is still covered "
                  "by web search and the career-page scanner.")
        return
    new = register_matches(matches, via="manual")
    term.table([{"board": m.registry_key, "provider": m.provider,
                 "scanned directly": "yes" if m.has_api else "no (detected only)"} for m in matches],
               ["board", "provider", "scanned directly"])
    term.ok(f"{new} new board(s) registered; they are included in the next `quackers scan`.")


# ---------------------------------------------------------------------------
# platforms / export
# ---------------------------------------------------------------------------


def cmd_platforms(args) -> None:
    from database.models import ScanRecord

    cli = _cli()
    with store.with_session() as session:
        q = session.query(ScanRecord)
        rec = (q.filter_by(id=args.scan_id).first() if args.scan_id
               else q.filter(ScanRecord.platform_stats_json.isnot(None))
                     .order_by(ScanRecord.id.desc()).first())
        if rec is None:
            raise cli.CliError("No scan with platform stats yet. Run `quackers scan`.")
        stats = cli._json_get(rec.platform_stats_json) or {}
        scan_id = rec.id
    if not stats:
        term.info(f"Scan #{scan_id} has no platform stats (older scan, or no results).")
        return
    term.heading(f"Platforms — scan #{scan_id}")
    term.table(
        [{"platform": k, "found": v.get("found", 0), "unique": v.get("unique", 0),
          "dupes": v.get("cross_source_duplicates", 0)}
         for k, v in sorted(stats.items(), key=lambda kv: -kv[1].get("unique", 0))],
        ["platform", "found", "unique", "dupes"],
        headers={"dupes": "cross-source dupes"},
    )


EXPORT_COLUMNS = ["id", "match_score", "title", "company", "location", "employment_type",
                  "workplace_arrangement", "source", "original_url", "application_url",
                  "first_discovered"]


def cmd_export(args) -> None:
    cli = _cli()
    rows = store.list_jobs_with_match(min_score=args.min_score or 0.0, status=args.status,
                                      source=None, limit=args.limit)
    if args.new:
        last = store.latest_scan_dict(limit=1)
        since = ((last or {}).get("started_at") or "")[:10]
        rows = [r for r in rows if (r.get("first_discovered") or "") >= since]
    out = Path(args.path).expanduser()
    if out.exists() and not term.confirm(f"{out} exists. Overwrite?", assume_yes=args.yes):
        raise cli.CliError("Not exported.")
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.suffix.lower() == ".json":
        out.write_text(json.dumps([{c: r.get(c) for c in EXPORT_COLUMNS} for r in rows],
                                  indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    else:
        with out.open("w", newline="", encoding="utf-8-sig") as fh:  # BOM: opens cleanly in Excel
            w = csv.DictWriter(fh, fieldnames=EXPORT_COLUMNS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
    term.ok(f"Exported {len(rows)} job(s) to {out}")


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


def register(sub) -> None:
    s = sub.add_parser("guide", help="step-by-step cheat sheet of the most useful commands")
    s.set_defaults(func=cmd_guide)

    s = sub.add_parser("show", help="quick views: cv, config, jobs, job ID, scan [ID], boards, ...")
    s.add_argument("what", choices=sorted(SHOW_TARGETS),
                   help="; ".join(f"{k}: {v[1]}" for k, v in sorted(SHOW_TARGETS.items())))
    s.add_argument("id", nargs="?", type=int, help="job / scan id where it applies")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("cv-scan", help="parse a CV (PDF/DOCX/JSON) and preview it; saves only with --save")
    s.add_argument("path")
    s.add_argument("--save", action="store_true", help="save after the preview (asks first)")
    s.add_argument("--edit", action="store_true", help="fix fields in $EDITOR before saving")
    s.add_argument("-y", "--yes", action="store_true", help="with --save: don't ask")
    s.set_defaults(func=cmd_cv_scan)

    s = sub.add_parser("cv-check", help="validate a CV file and preview it (never saves)")
    s.add_argument("path")
    s.set_defaults(func=cmd_cv_check)

    s = sub.add_parser("cv-template", help="fill a JSON CV in your editor (for unreadable PDFs)")
    s.add_argument("path", nargs="?", help="where to create it (default data/cv/cv.json)")
    s.add_argument("--no-edit", action="store_true", help="only create the file")
    s.set_defaults(func=cmd_cv_template)

    boards = sub.add_parser("boards", help="company ATS career boards (Ashby, Workable, ...)").add_subparsers(
        dest="boards_cmd", metavar="<action>")
    boards.required = True
    s = boards.add_parser("list", help="every board that is (or could be) scanned")
    s.set_defaults(func=cmd_boards_list)
    s = boards.add_parser("add", help="add boards: provider:board or a board URL")
    s.add_argument("boards", nargs="+")
    s.set_defaults(func=cmd_boards_add)
    s = boards.add_parser("remove", help="stop scanning boards (and never re-discover them)")
    s.add_argument("boards", nargs="+")
    s.set_defaults(func=cmd_boards_remove)
    s = boards.add_parser("discover", help="detect the ATS behind a company careers page")
    s.add_argument("url")
    s.set_defaults(func=cmd_boards_discover)

    s = sub.add_parser("platforms", help="which platforms a scan found jobs on (default: latest)")
    s.add_argument("scan_id", nargs="?", type=int)
    s.set_defaults(func=cmd_platforms)

    s = sub.add_parser("export", help="save jobs to .csv (Excel) or .json")
    s.add_argument("path", help="e.g. jobs.csv or jobs.json")
    s.add_argument("--min-score", type=float)
    s.add_argument("--status")
    s.add_argument("--new", action="store_true", help="only jobs from the latest scan")
    s.add_argument("--limit", type=int, default=1000)
    s.add_argument("-y", "--yes", action="store_true", help="overwrite without asking")
    s.set_defaults(func=cmd_export)

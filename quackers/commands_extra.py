"""Convenience commands: guide, show, cv-scan / cv-check / cv-template,
boards, platforms and export.

They reuse the handlers in :mod:`quackers.cli`; ``register(sub)`` is
called from ``cli.build_parser`` so the interactive shell's /help and
Tab completion pick them up automatically.
"""
from __future__ import annotations

import csv
import json
import re
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
        ("quackers lang de fr", "also search job titles in German + French (/lang)"),
        ("quackers config set description_languages=en", "only jobs written in these languages"),
    ]),
    ("2. Find jobs", [
        ('quackers search Nurse in Toronto', "any profession, any country (also: Remote jobs worldwide)"),
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


# ---------------------------------------------------------------------------
# search "<title> in <place>" (any industry) / lang
# ---------------------------------------------------------------------------

#: Words that only say "any job" — removed when deciding if a title was given.
_ANY_WORDS = re.compile(r"(?<!\w)(?:any|all|some|jobs?|positions?|roles?|vacanc(?:y|ies)|"
                        r"openings?|stellen|stellenangebote)(?!\w)", re.I)
#: Level words in a request -> employment types to keep.
_LEVEL_TYPES = (
    (re.compile(r"(?<!\w)(?:working students?|werkstudent\w*)(?!\w)", re.I), ["working_student"]),
    (re.compile(r"(?<!\w)(?:intern(?:ship)?s?|praktik\w*|trainee|stage|stagiaire)(?!\w)", re.I),
     ["internship"]),
    (re.compile(r"(?<!\w)(?:entry[- ]level|graduates?|berufseinsteiger\w*|absolvent\w*|"
                r"new grad)(?!\w)", re.I),
     ["graduate", "internship", "working_student", "other"]),
    (re.compile(r"(?<!\w)(?:part[- ]time|teilzeit|minijob)(?!\w)", re.I), ["part_time"]),
)


#: Titles never shown for an "any entry-level jobs" search.
ENTRY_LEVEL_EXCLUDED_TITLES = ("senior", "sr", "lead", "head of", "principal", "director",
                               "chief", "vp", "leiter", "leiterin", "teamleiter")


def parse_search_request(text: str) -> Dict[str, Any]:
    """"Warehouse Worker in Germany" -> title / locations / employment types.

    Works for any profession; nothing is assumed about the industry.  A
    request without a real title ("Any entry-level jobs in Berlin") gives
    an empty title, so no title filter is applied.
    """
    t = " ".join((text or "").split()).strip(" .")
    remote = bool(re.search(r"(?<!\w)remote(?!\w)", t, re.I))
    t = re.sub(r"(?<!\w)(?:fully )?remote(?!\w)", " ", t, flags=re.I)
    from services.geo import ANYWHERE_RE

    anywhere = False
    locations: List[str] = []
    m = re.match(r"^(.*?)\s+(?:in|near|bei|at)\s+(.+)$", t, re.I)
    if m and m.group(2).strip():
        t = m.group(1)
        locations = [x.strip() for x in re.split(r"\s*(?:,|/|\bor\b|\band\b)\s*", m.group(2))
                     if x.strip()]
    # "worldwide", "anywhere", "in any country": no geographic restriction
    if any(ANYWHERE_RE.search(x) for x in locations) or ANYWHERE_RE.search(t):
        anywhere = True
        locations = [x for x in locations if not ANYWHERE_RE.search(x)]
        t = ANYWHERE_RE.sub(" ", t)
    types: List[str] = []
    for rx, kinds in _LEVEL_TYPES:
        if rx.search(t):
            types += [k for k in kinds if k not in types]
    title = " ".join(t.split()).strip(" ,-")
    bare = " ".join(_ANY_WORDS.sub(" ", title).split())
    level_only = not bare or bool(re.fullmatch(
        r"(?:entry[- ]level|graduates?|intern(?:ship)?s?|working students?|part[- ]time|"
        r"praktik\w*|werkstudent\w*|berufseinsteiger\w*|trainee)", bare, re.I))
    return {
        "title": "" if level_only else title,
        "level_only_query": bare if level_only else "",
        "locations": locations,
        "employment_types": types,  # [] = any level
        "remote": remote,
        "anywhere": anywhere,  # True = no location restriction at all
    }


def cmd_search(args) -> None:
    """One-off search for any profession: `quackers search Nurse in Berlin`."""
    from config.search_config import EmploymentType

    cli = _cli()
    raw = " ".join(args.request)
    req = parse_search_request(raw)
    if not (req["title"] or req["employment_types"] or req["remote"] or req["anywhere"]
            or req["locations"] or re.search(r"(?<!\w)(?:any|all|jobs?)(?!\w)", raw, re.I)):
        raise cli.CliError('Say what to look for, e.g. quackers search "Accountant in London"')
    cfg = cli.load_config()
    # No place given -> the saved location preferences (if any) apply;
    # none saved -> no location restriction.  Any country works.
    titles = [req["title"]] if req["title"] else []
    workplace = ["include_remote"] if req["remote"] else []
    if not titles:
        # No title: no title gate and no keyword left over from the saved config.
        cfg.primary_title, cfg.title_synonyms, cfg.include_keywords = "", [], []
    cfg = cli.apply_scan_overrides(cfg, titles, req["locations"], workplace)
    if titles:
        cfg.include_keywords = titles  # not the saved config's (maybe IT) keywords
    if req["anywhere"]:
        cfg.locations, cfg.excluded_locations = [], []
        cfg.worldwide_remote = bool(req["remote"])
    # The level the user typed decides the employment types; no level means
    # every type (a "Nurse" search must not be limited to internships).
    cfg.employment_types = ([EmploymentType(t) for t in req["employment_types"]]
                            or list(EmploymentType))
    if req["level_only_query"] and "graduate" in req["employment_types"]:
        # "Any entry-level jobs": every field, but not senior roles.
        cfg.excluded_titles = list(dict.fromkeys(
            [*cfg.excluded_titles, *ENTRY_LEVEL_EXCLUDED_TITLES]))
    if args.source:
        cfg.enabled_sources = list(args.source)
    what = req["title"] or " ".join(f"any {req['level_only_query']} job".split())
    term.info(f"Searching: {what}"
              f"{' in ' + ', '.join(cfg.locations) if cfg.locations else ''}"
              f"{' (remote)' if req['remote'] else ''} · languages: "
              f"{', '.join(_search_langs(cfg))}")
    for note in coverage_notes(cfg):
        term.info(note)
    result = cli._run_scan(cfg, evaluate=not args.no_ai)
    cli._print_scan_result(result)
    if result.get("status") == "error":
        raise cli.CliError("Search finished with errors (see above).")


def coverage_notes(cfg) -> List[str]:
    """Honest notes about sources that do not cover the requested places.
    Sources are still queried; this only sets expectations."""
    from services.geo import countries_of
    from sources.registry import enabled_sources

    wanted = countries_of(cfg.locations or [])
    if not wanted:
        return []
    notes = []
    for src in enabled_sources(cfg):
        cover = set(getattr(src, "coverage_countries", ()) or ())
        if cover and not cover & set(wanted):
            notes.append(f"  note: {src.name} covers {src.coverage_note} — few or no results "
                         f"expected for {', '.join(w.title() for w in wanted)}.")
    return notes


def _search_langs(cfg) -> List[str]:
    from services.multilingual import search_languages

    return search_languages(cfg)


def cmd_lang(args) -> None:
    """Show or set the languages job titles are searched in (`/lang de fr`)."""
    from services.multilingual import SUPPORTED

    cli = _cli()
    cfg = cli.load_config()
    changed = False
    if args.languages:
        codes = [c.strip().lower() for c in re.split(r"[,\s]+", " ".join(args.languages)) if c.strip()]
        if codes == ["auto"]:
            codes = []
        bad = [c for c in codes if c not in SUPPORTED]
        if bad:
            raise cli.CliError(f"Unknown language(s) {', '.join(bad)}. Supported: {', '.join(SUPPORTED)}")
        cfg.search_languages = codes
        changed = True
    if args.no_english is not None:
        cfg.include_english = not args.no_english
        changed = True
    if changed:
        cli._save_config(cfg)
        term.ok("Search languages saved.")
    term.kv([
        ("Searching in", ", ".join(_search_langs(cfg))),
        ("Extra languages", ", ".join(cfg.search_languages) or "none (English + local only)"),
        ("Local language of locations", "yes" if cfg.auto_detect_local_language else "no"),
        ("English too", "yes" if cfg.include_english else "no"),
    ])
    term.info("Supported: " + ", ".join(SUPPORTED) + "   (`lang auto` = English + local only)")


def register(sub) -> None:
    s = sub.add_parser("guide", help="step-by-step cheat sheet of the most useful commands")
    s.set_defaults(func=cmd_guide)

    s = sub.add_parser("search", help="search any profession: search Nurse in Berlin")
    s.add_argument("request", nargs="+",
                   help="e.g. Accountant in London / Mechanical Engineer in Tokyo / Jobs in any country")
    s.add_argument("--no-ai", action="store_true", help="skip AI scoring (deterministic only)")
    s.add_argument("--source", action="append", metavar="NAME", help="only this source (repeatable)")
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("lang", help="show / set the languages job titles are searched in")
    s.add_argument("languages", nargs="*", help="ISO codes, e.g. de fr  (or: auto)")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--no-english", dest="no_english", action="store_true", default=None,
                   help="don't search English titles")
    g.add_argument("--english", dest="no_english", action="store_false",
                   help="search English titles too (default)")
    s.set_defaults(func=cmd_lang)

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

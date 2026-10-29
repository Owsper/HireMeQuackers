"""Tests for the `quackers` CLI layer (parsing, coercion, terminal safety)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from config.search_config import SearchConfig
from quackers import cli, term


def test_clean_strips_ansi_and_control_characters():
    hostile = "Intern\x1b[2J\x1b]0;pwned\x07 role\r\nline2\x00\x9b"
    assert term.clean(hostile) == "Intern role\nline2"


def test_clean_keeps_unicode_and_tabs():
    assert term.clean("Praktikum München\tBerlin") == "Praktikum München\tBerlin"


@pytest.mark.parametrize(
    "argv, func",
    [
        (["status"], cli.cmd_status),
        (["scan", "--no-ai", "--source", "lever"], cli.cmd_scan),
        (["jobs", "--min-score", "60", "--sort", "score"], cli.cmd_jobs),
        (["job", "7"], cli.cmd_job),
        (["app", "set", "7", "shortlisted"], cli.cmd_app_set),
        (["docs", "cover-letter", "7", "--tone", "casual"], cli.cmd_docs_letter),
        (["profile", "import", "cv.pdf", "--yes"], cli.cmd_profile_import),
        (["config", "set", "locations=Berlin, Munich"], cli.cmd_config_set),
        (["sources", "disable", "lever"], cli.cmd_sources_toggle),
        (["history", "rerun", "3"], cli.cmd_history_rerun),
        (["reset", "--scope", "all"], cli.cmd_reset),
    ],
)
def test_parser_routes_commands(argv, func):
    args = cli.build_parser().parse_args(argv)
    assert args.func is func


def test_app_status_is_case_insensitive():
    args = cli.build_parser().parse_args(["app", "set", "3", "applied", "--confirm"])
    assert args.status == "APPLIED" and args.confirm


def test_coerce_list_and_none_values():
    cfg = SearchConfig()
    assert cli._coerce_config_value(cfg, "locations", "Berlin, Munich") == ["Berlin", "Munich"]
    assert cli._coerce_config_value(cfg, "salary_min", "none") is None
    assert cli._coerce_config_value(cfg, "locations", "") == []


def test_coerce_unknown_key_is_a_cli_error():
    with pytest.raises(cli.CliError):
        cli._coerce_config_value(SearchConfig(), "no_such_field", "1")


def test_form_to_fields_accepts_strings_and_lists():
    fields = cli._form_to_fields({
        "name": " Ada ",
        "skills": "Python, SQL",
        "experience": ["Intern at X", ""],
        "education": "BSc CS\nMSc AI",
    })
    assert fields["name"] == "Ada"
    assert fields["skills"] == ["Python", "SQL"]
    assert fields["experience"] == [{"text": "Intern at X"}]
    assert fields["education"] == [{"text": "BSc CS"}, {"text": "MSc AI"}]


def test_form_to_fields_requires_name():
    with pytest.raises(cli.CliError):
        cli._form_to_fields({"name": "  "})


@pytest.mark.parametrize(
    "name, expected",
    [
        ("my cv.pdf", "my_cv.pdf"),
        ("CV.final.v2.docx", "CV_final_v2.docx"),
        ("../../etc/passwd", "passwd"),
        ("report", "report"),
        (".pdf", "pdf"),
    ],
)
def test_sanitize_filename_keeps_extension(name, expected):
    from services.sanitization import sanitize_filename

    assert sanitize_filename(name) == expected


def test_registered_sources_load_without_a_scan():
    from quackers import store

    assert len(store.list_registered_sources()) > 0


# ---- `quackers cv` and CV-aware `quackers scan` -----------------------------


def _scan_args(argv):
    return cli.build_parser().parse_args(["scan", *argv])


def test_scan_without_cv_and_without_titles_says_no_cv(monkeypatch):
    monkeypatch.setattr(cli.store, "load_active_profile", lambda: None)
    monkeypatch.setattr(cli, "_run_scan", lambda *a, **k: pytest.fail("must not scan"))
    with pytest.raises(cli.CliError, match="No CV"):
        cli.cmd_scan(_scan_args([]))
    with pytest.raises(cli.CliError, match="No CV"):
        cli.cmd_scan(_scan_args(["--titles", "Data Analyst"]))  # location missing too


def test_scan_without_cv_uses_titles_and_location(monkeypatch):
    seen = {}

    def fake_run(cfg, *, evaluate):
        seen["cfg"] = cfg
        return {"status": "done", "per_source": {}}

    monkeypatch.setattr(cli.store, "load_active_profile", lambda: None)
    monkeypatch.setattr(cli, "load_config", lambda: SearchConfig())
    monkeypatch.setattr(cli, "_run_scan", fake_run)
    cli.cmd_scan(_scan_args(["--titles", "Data Analyst, BI Intern", "--location", "Berlin, Munich"]))
    cfg = seen["cfg"]
    assert cfg.primary_title == "Data Analyst"
    assert cfg.title_synonyms == ["BI Intern"]
    assert cfg.include_keywords[:2] == ["Data Analyst", "BI Intern"]
    assert cfg.locations == ["Berlin", "Munich"]


def test_scan_with_cv_needs_no_flags(monkeypatch):
    ran = []
    monkeypatch.setattr(cli.store, "load_active_profile", lambda: object())
    monkeypatch.setattr(cli, "load_config", lambda: SearchConfig())
    monkeypatch.setattr(cli, "_run_scan",
                        lambda cfg, *, evaluate: ran.append(cfg) or {"status": "done", "per_source": {}})
    cli.cmd_scan(_scan_args(["--no-ai"]))
    assert len(ran) == 1


def test_cv_json_creates_active_profile(monkeypatch, tmp_path):
    import json
    from services import candidate_profile as cp

    saved = {}

    def fake_upsert(fields, *, active):
        saved.update(fields=fields, active=active)
        return SimpleNamespace(id=7, name=fields["name"], profile_version=1)

    monkeypatch.setattr(cp, "upsert_from_extraction", fake_upsert)
    cv = tmp_path / "cv.json"
    cv.write_text(json.dumps({"name": "Ada", "skills": ["Python", "SQL"],
                              "experience": ["Intern at X"], "hobby": "chess"}), encoding="utf-8")
    cli.cmd_cv(cli.build_parser().parse_args(["cv", str(cv)]))
    assert saved["active"] is True
    assert saved["fields"]["skills"] == ["Python", "SQL"]
    assert saved["fields"]["experience"] == [{"text": "Intern at X"}]


@pytest.mark.parametrize("content, ext", [("[1, 2]", ".json"), ("{not json", ".json"), ("x", ".txt")])
def test_cv_rejects_bad_files(tmp_path, content, ext):
    f = tmp_path / f"cv{ext}"
    f.write_text(content, encoding="utf-8")
    with pytest.raises(cli.CliError):
        cli.cmd_cv(cli.build_parser().parse_args(["cv", str(f)]))


def test_cv_without_path_and_no_profile_says_no_cv(monkeypatch):
    monkeypatch.setattr(cli.store, "load_active_profile", lambda: None)
    with pytest.raises(cli.CliError, match="No CV"):
        cli.cmd_cv(cli.build_parser().parse_args(["cv"]))

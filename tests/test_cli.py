"""Tests for the `quackers` CLI layer (parsing, coercion, terminal safety)."""
from __future__ import annotations

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

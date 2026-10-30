"""`/lang`: show / add / clear / help, validation, persistence, planner use,
and the applicant's CV languages staying untouched."""
from __future__ import annotations

import json

import pytest

from config.search_config import EmploymentType
from quackers import cli, shell
from services.query_planner import plan_queries


@pytest.fixture
def db(tmp_path, monkeypatch):
    from database.connection import init_db

    url = f"sqlite:///{(tmp_path / 't.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    init_db(url)
    return tmp_path


def lang(*codes):
    args = cli.build_parser().parse_args(["lang", *codes])
    args.func(args)


def test_lang_shows_selected_and_available(db, capsys):
    lang()
    out = capsys.readouterr().out
    assert "Selected" in out and "DE German" in out and "JA Japanese" in out and "ZU Zulu" in out


def test_lang_adds_multiple_any_case_without_duplicates(db, capsys):
    lang("DE", "ru")
    lang("de", "EN")  # DE again: not duplicated, EN added, RU kept
    out = capsys.readouterr().out
    assert cli.load_config().search_languages == ["de", "ru", "en"]
    assert "Already selected: DE German" in out and "Added: EN English" in out


def test_lang_rejects_unknown_codes_and_changes_nothing(db):
    lang("DE")
    with pytest.raises(cli.CliError, match="Unknown language code.*XX.*ISO 639-1"):
        lang("FR", "XX")
    assert cli.load_config().search_languages == ["de"]


def test_lang_persists_and_clear_restores_defaults_only(db):
    cfg = cli.load_config()
    cfg.locations, cfg.include_english = ["Tokyo"], False
    cli._save_config(cfg)
    lang("DE", "RU")
    saved = cli.load_config()  # fresh load = what the next session sees
    assert saved.search_languages == ["de", "ru"] and saved.locations == ["Tokyo"]
    lang("clear")
    saved = cli.load_config()
    assert saved.search_languages == [] and saved.include_english is True
    assert saved.locations == ["Tokyo"]  # unrelated settings untouched


def test_lang_help(db, capsys):
    lang("help")
    assert "/lang clear" in capsys.readouterr().out
    assert cli.load_config().search_languages == []


def test_selected_languages_reach_the_planner(db):
    lang("DE", "RU", "EN")
    cfg = cli.load_config()
    cfg.primary_title, cfg.locations = "Python Developer", ["Berlin"]
    cfg.employment_types = list(EmploymentType)
    qs = {s.query_string for s in plan_queries(cfg, max_queries=200)}
    # technical term kept, role translated
    assert {"Python Developer", "Python Entwickler", "Python разработчик"} <= qs


def test_lang_never_touches_applicant_languages(db):
    from database.models import CandidateProfile
    from quackers import store
    from services import candidate_profile as cp

    p = cp.upsert_from_extraction({"name": "Ada Lovelace", "languages": [{"name": "English (C1)"}]},
                                  active=True)
    lang("DE", "RU")
    with store.with_session() as s:
        row = s.get(CandidateProfile, p.id)
        assert json.loads(row.languages_json) == [{"name": "English (C1)"}]


def test_slash_lang_in_interactive_shell(db, capsys):
    tree = shell.command_tree(cli.build_parser())
    assert shell.handle_line("/lang DE RU EN", tree, None) is True
    assert cli.load_config().search_languages == ["de", "ru", "en"]
    shell.handle_line("/lang QQ", tree, None)
    captured = capsys.readouterr()
    assert "Unknown language code" in captured.out + captured.err

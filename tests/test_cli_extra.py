"""New convenience commands, CV validation/fallback flow, single active profile, migration."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from quackers import cli


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Fresh isolated SQLite DB (+ data dir) for one test."""
    from database.connection import init_db

    url = f"sqlite:///{(tmp_path / 't.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    init_db(url)
    return tmp_path


def run(argv):
    args = cli.build_parser().parse_args(argv)
    args.func(args)


def _pdf(path, text):
    import pymupdf

    doc = pymupdf.open()
    for chunk in (text if isinstance(text, list) else [text]):
        page = doc.new_page()
        if chunk:
            page.insert_text((50, 60), chunk)
    doc.save(str(path))
    return path


GOOD_CV = ("Ada Lovelace\nada@example.com\n+49 151 2345678\n\nSKILLS\nPython, SQL\n\n"
           "EXPERIENCE\nData Intern - Acme GmbH, Berlin (06/2025-09/2025)\n\n"
           "EDUCATION\nB.Sc. Computer Science - TU Berlin")


# ---------------------------------------------------------------- validation


def test_validate_form_statuses_and_errors():
    from services.cv_validation import validate_form

    v = validate_form({"name": "Ada", "email": "not-an-email", "skills": ["Python"],
                       "experience": "Intern at X", "education": 5})
    assert v.status["name"] == "ok" and v.status["email"] == "suspect"
    assert v.status["experience"] == "ok" and v.status["projects"] == "missing"
    assert any("email" in e for e in v.errors) and any("education" in e for e in v.errors)
    assert not validate_form({"name": ""}).ok
    assert not validate_form({"name": "Unknown"}).ok


def test_garbled_and_unreadable_text_detection():
    from services.cv_validation import looks_garbled, readable_text_problem

    assert looks_garbled("(cid:12)(cid:44)(cid:71) (cid:3) foo bar baz qux")
    assert looks_garbled("S o f t w a r e E n g i n e e r i n g")
    assert not looks_garbled("Built KPI dashboards for the sales team in Python.")
    assert "No text" in readable_text_problem("   ")
    assert "characters" in readable_text_problem("Ada Lovelace")
    assert readable_text_problem(GOOD_CV * 2) == ""


def test_json_aliases_and_unknown_fields():
    from services.cv_validation import normalize_json_cv

    form, unknown = normalize_json_cv({"full_name": "Ada", "professional_summary": "Hi",
                                       "location": "Berlin", "job_titles": ["Data Intern"],
                                       "hobby": "chess"})
    assert form["name"] == "Ada" and form["summary"] == "Hi"
    assert form["other"] == {"location": "Berlin", "job_titles": ["Data Intern"]}
    assert unknown == ["hobby"]


def test_template_json_is_valid_and_comment_free():
    root = Path(__file__).resolve().parent.parent
    data = json.loads((root / "config" / "cv_template.json").read_text(encoding="utf-8"))
    assert data["name"] == "" and isinstance(data["skills"], list)


# ---------------------------------------------------------------- cv commands


def test_cv_scan_previews_without_saving(db, capsys):
    from database.models import CandidateProfile
    from quackers import store

    run(["cv-scan", str(_pdf(db / "cv.pdf", GOOD_CV))])
    out = capsys.readouterr().out
    assert "Ada Lovelace" in out and "nothing saved" in out
    with store.with_session() as s:
        assert s.query(CandidateProfile).count() == 0


def test_unreadable_pdf_never_creates_profile(db):
    from database.models import CandidateProfile
    from quackers import store

    blank = _pdf(db / "scan.pdf", "")
    for argv in (["cv", str(blank), "-y"], ["cv-scan", str(blank), "--save", "-y"]):
        with pytest.raises(cli.CliError, match="cv-template"):
            run(argv)
    with store.with_session() as s:
        assert s.query(CandidateProfile).count() == 0


def test_multipage_pdf_is_read(db, capsys):
    run(["cv-scan", str(_pdf(db / "two.pdf", [GOOD_CV, "PROJECTS\nJob scraper CLI in Python"]))])
    assert "Ada Lovelace" in capsys.readouterr().out


def test_new_cv_keeps_old_profile_and_single_active(db):
    from database.models import CandidateProfile
    from quackers import store

    pdf = _pdf(db / "cv.pdf", GOOD_CV)
    run(["cv", str(pdf), "-y"])
    run(["cv", str(pdf), "-y"])
    with store.with_session() as s:
        rows = s.query(CandidateProfile).order_by(CandidateProfile.id).all()
        assert len(rows) == 2
        assert [r.active for r in rows] == [False, True]
        newest = rows[1].id
    assert store.load_active_profile().id == newest


def test_invalid_json_cv_blocked_even_with_yes(db):
    from database.models import CandidateProfile
    from quackers import store

    f = db / "cv.json"
    f.write_text(json.dumps({"name": "", "email": "nope"}), encoding="utf-8")
    with pytest.raises(cli.CliError, match="Not saved"):
        run(["cv", str(f), "-y"])
    run(["cv-check", str(f)])  # check never raises / saves
    with store.with_session() as s:
        assert s.query(CandidateProfile).count() == 0


def test_cv_template_creates_file_and_help(db):
    target = db / "mine.json"
    run(["cv-template", str(target), "--no-edit"])
    assert json.loads(target.read_text(encoding="utf-8"))["name"] == ""
    assert (db / "cv_template_help.md").exists()
    with pytest.raises(cli.CliError, match="already exists"):
        run(["cv-template", str(target), "--no-edit"])


def test_json_cv_other_fields_roundtrip(db, capsys):
    f = db / "cv.json"
    f.write_text(json.dumps({"full_name": "Ada", "email": "ada@example.com", "skills": ["SQL"],
                             "location": "Berlin", "job_titles": ["Data Intern"]}), encoding="utf-8")
    run(["cv", str(f), "-y"])
    capsys.readouterr()
    run(["show", "cv"])
    out = capsys.readouterr().out
    assert "Ada" in out and "Berlin" in out and "Data Intern" in out


# ---------------------------------------------------------------- boards / show / platforms / export


def test_boards_add_list_remove_never_rediscovered(db, capsys):
    from services.ats_detection import ATSMatch, register_matches, registered_boards

    run(["boards", "add", "ashby:acme", "https://jobs.lever.co/datadog/abc"])
    assert registered_boards("lever") == ["datadog"]
    run(["boards", "list"])
    assert "ashby:acme" in capsys.readouterr().out
    run(["boards", "remove", "lever:datadog"])
    assert registered_boards("lever") == []
    assert register_matches([ATSMatch("lever", "datadog", "")]) == 0  # stays removed
    run(["boards", "add", "lever:datadog"])  # explicit add re-enables
    assert registered_boards("lever") == ["datadog"]
    with pytest.raises(cli.CliError):
        run(["boards", "add", "nonsense"])


def test_boards_discover_from_careers_page(db, monkeypatch):
    from quackers import commands_extra
    from services.ats_detection import registered_boards

    pages = {"https://acme.com": '<a href="/karriere">Karriere</a>',
             "https://acme.com/karriere": '<iframe src="https://acme.jobs.personio.de/"></iframe>'}
    monkeypatch.setattr(commands_extra, "_fetch", lambda url: pages[url])
    run(["boards", "discover", "acme.com"])
    assert registered_boards("personio") == ["acme"]


def test_platform_stats_persisted_and_shown(db, capsys):
    from database import repository
    from quackers import store

    with store.with_session() as s:
        scan = repository.create_scan_record(s, search_config={}, trigger="user")
        repository.finish_scan(s, scan, platform_stats={"Indeed": {"found": 3, "unique": 2,
                                                                     "cross_source_duplicates": 1}})
    run(["platforms"])
    out = capsys.readouterr().out
    assert "Indeed" in out and "3" in out
    run(["show", "scan"])  # latest scan resolves without an id
    assert "Scan #" in capsys.readouterr().out


def test_show_requires_job_id_and_export_csv(db):
    with pytest.raises(cli.CliError, match="Which job"):
        run(["show", "job"])
    out = db / "jobs.csv"
    run(["export", str(out)])
    assert out.read_text(encoding="utf-8-sig").startswith("id,match_score,title")


def test_guide_and_typo_suggestion(capsys):
    run(["guide"])
    # /guide shows interactive slash syntax only — no standalone `quackers` prefix.
    out = capsys.readouterr().out
    assert "/scan" in out and "quackers " not in out and "`quackers" not in out
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["scna"])
    assert "scan" in capsys.readouterr().err


# ---------------------------------------------------------------- migration


def test_migration_on_old_db_keeps_profiles(tmp_path):
    import sqlite3

    from sqlalchemy import create_engine, inspect

    from database import migrations

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE scan_records (id INTEGER PRIMARY KEY, status VARCHAR(40));
        CREATE TABLE jobs (id INTEGER PRIMARY KEY, title TEXT);
        CREATE TABLE candidate_profiles (id INTEGER PRIMARY KEY, name TEXT, active BOOLEAN);
        INSERT INTO candidate_profiles VALUES (1, 'Old Ada', 1);
    """)
    con.commit()
    con.close()
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    migrations.upgrade(engine)
    migrations.upgrade(engine)  # idempotent
    cols = [c["name"] for c in inspect(engine).get_columns("scan_records")]
    assert "platform_stats_json" in cols
    with engine.connect() as c:
        assert c.exec_driver_sql("SELECT name, active FROM candidate_profiles").all() == [("Old Ada", 1)]

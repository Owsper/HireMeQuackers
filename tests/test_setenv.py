"""Tests for the ASCII banner and `quackers env` (.env editing).

Everything runs against temporary directories with a mocked editor and
fake credentials; the real project `.env` is never touched.
"""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from quackers import banner, cli, envfile, shell, term

FAKE_SECRET = "fake-secret-value-123"


# ---- banner ------------------------------------------------------------------


def test_banner_fits_80_columns_and_falls_back_when_narrow():
    assert banner.ART_WIDTH == max(len(line.rstrip()) for line in banner.ART)
    assert banner.ART_WIDTH > 40
    assert banner.render(1000) and len(banner.render(1000)) == len(banner.ART)
    assert banner.render(40) == [term.style("HireMeQuackers", "bold", "bright_cyan")]


def test_banner_has_no_ansi_when_colour_is_off(monkeypatch):
    monkeypatch.setattr(term, "_COLOR", False)
    assert all("\x1b" not in line for line in banner.render(120))


class _OffAgent:
    available = False
    unavailable_reason = "no key"
    model = "x"


def test_session_header_uses_banner_not_welcome_text(capsys, monkeypatch):
    monkeypatch.setattr(shell, "_width", lambda: 100)
    monkeypatch.setattr(shell.store, "load_active_profile", lambda: None)
    monkeypatch.setattr(shell.store, "list_jobs_with_match", lambda limit: [])
    monkeypatch.setattr(shell.store, "latest_scan_dict", lambda limit: None)
    shell.banner(_OffAgent())
    out = capsys.readouterr().out
    assert "Welcome to HireMeQuackers" not in out
    assert banner.ART[1].rstrip() in out


# ---- env path / template -----------------------------------------------------


def test_env_path_is_project_root_regardless_of_cwd(monkeypatch, tmp_path):
    from config.settings import PROJECT_ROOT

    monkeypatch.chdir(tmp_path)
    assert envfile.env_path() == PROJECT_ROOT / ".env"


def test_template_parses_and_contains_no_values():
    result = envfile.parse(envfile.template())
    assert result.errors == [] and result.warnings == []
    assert all(v == "" for v in result.values.values())
    assert set(result.values) == {v.name for v in envfile.ENV_VARS if v.secret}


def test_every_template_var_is_read_by_settings():
    from config.settings import PROJECT_ROOT

    source = (PROJECT_ROOT / "config" / "settings.py").read_text(encoding="utf-8")
    for var in envfile.ENV_VARS:
        assert f'"{var.name}"' in source, var.name


# ---- parsing -------------------------------------------------------------------


def test_parse_handles_comments_quotes_export_and_duplicates():
    text = (
        "# comment\n\nexport GEMINI_API_KEY='abc'\nGEMINI_MODEL=\"m-1\"  \n"
        "SEARCH_PROVIDER=brave # inline\nGEMINI_MODEL=m-2\nCUSTOM_THING=1\n"
    )
    r = envfile.parse(text)
    assert r.errors == []
    assert r.values == {"GEMINI_API_KEY": "abc", "GEMINI_MODEL": "m-1",
                        "SEARCH_PROVIDER": "brave", "CUSTOM_THING": "1"}
    assert any("defined twice" in w for w in r.warnings)
    assert any("CUSTOM_THING" in w for w in r.warnings)


def test_parse_errors_never_include_values():
    text = f"GEMINI_API_KEY='{FAKE_SECRET}\nnot a line {FAKE_SECRET}\n1BAD={FAKE_SECRET}\n"
    r = envfile.parse(text)
    assert len(r.errors) == 3
    assert all(FAKE_SECRET not in e for e in r.errors + r.warnings)


def test_parse_warns_when_empty_value_overrides_default():
    r = envfile.parse("DATABASE_URL=\n")
    assert any("DATABASE_URL is empty" in w for w in r.warnings)


def test_summarize_changes_returns_names_only():
    changes = envfile.summarize_changes({"A": "1", "B": FAKE_SECRET}, {"A": "2", "C": FAKE_SECRET})
    assert changes == {"added": ["C"], "changed": ["A"], "removed": ["B"]}


def test_describe_value_hides_secrets():
    assert envfile.describe_value("GEMINI_API_KEY", FAKE_SECRET) == "set (hidden)"
    assert envfile.describe_value("MY_TOKEN", FAKE_SECRET) == "set (hidden)"
    assert envfile.describe_value("GEMINI_MODEL", "m") == "m"
    assert envfile.describe_value("GEMINI_MODEL", None) == "not set"


# ---- files / git -----------------------------------------------------------------


def test_write_atomic_creates_and_preserves(tmp_path):
    path = tmp_path / ".env"
    assert envfile.write_atomic(path, "A=1\n") is True
    if os.name != "nt":
        assert oct(path.stat().st_mode & 0o777) == oct(0o600)
        path.chmod(0o640)
    assert envfile.write_atomic(path, "A=2\n") is False
    assert path.read_text(encoding="utf-8") == "A=2\n"
    if os.name != "nt":
        assert oct(path.stat().st_mode & 0o777) == oct(0o640)
    assert [p.name for p in tmp_path.iterdir()] == [".env"]  # no temp files left


def test_ensure_gitignored_adds_rule_once(tmp_path):
    (tmp_path / ".gitignore").write_text("data/", encoding="utf-8")
    assert envfile.ensure_gitignored(tmp_path) is True
    assert envfile.ensure_gitignored(tmp_path) is False
    text = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert text.count("\n.env\n") == 1 and text.startswith("data/\n")


def test_project_gitignore_excludes_env():
    from config.settings import PROJECT_ROOT

    assert envfile.gitignore_covers_env(PROJECT_ROOT)


@pytest.mark.skipif(not shutil.which("git"), reason="git not installed")
def test_git_tracks_detects_tracked_env(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    env = tmp_path / ".env"
    env.write_text("A=1\n", encoding="utf-8")
    assert envfile.git_tracks(env) is False
    subprocess.run(["git", "-C", str(tmp_path), "add", "-f", ".env"], check=True)
    assert envfile.git_tracks(env) is True


# ---- the interactive command ----------------------------------------------------


@pytest.fixture
def setenv(monkeypatch, tmp_path):
    """Run `env` against tmp_path/.env with scripted input and editor."""
    path = tmp_path / ".env"
    monkeypatch.setattr(envfile, "env_path", lambda: path)
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    monkeypatch.setattr(envfile, "git_tracks", lambda p: False)
    state = {"inputs": [], "confirms": [], "editor": None, "edits": 0}

    def fake_input(prompt=""):
        if not state["inputs"]:
            raise EOFError
        return state["inputs"].pop(0)

    def fake_confirm(prompt, assume_yes=False):
        return state["confirms"].pop(0) if state["confirms"] else False

    def fake_edit(text, *, label, suffix=".txt"):
        state["edits"] += 1
        return state["editor"](text)

    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr(term, "confirm", fake_confirm)
    monkeypatch.setattr(term, "edit_text", fake_edit)

    def run():
        cli.cmd_env(cli.build_parser().parse_args(["env"]))

    state["run"], state["path"] = run, path
    return state


def test_creates_missing_env_without_printing_secret(setenv, capsys):
    setenv["confirms"] = [True]
    setenv["editor"] = lambda t: t.replace("GEMINI_API_KEY=", f"GEMINI_API_KEY={FAKE_SECRET}")
    setenv["run"]()
    out = capsys.readouterr()
    text = setenv["path"].read_text(encoding="utf-8")
    assert f"GEMINI_API_KEY={FAKE_SECRET}" in text and "# --- AI ---" in text
    assert FAKE_SECRET not in out.out + out.err
    assert "GEMINI_API_KEY" in out.out and "Restart quackers" in out.out
    assert (setenv["path"].parent / ".gitignore").exists()


def test_declining_creation_creates_nothing(setenv):
    setenv["confirms"] = [False]
    setenv["run"]()
    assert not setenv["path"].exists() and setenv["edits"] == 0


def test_edit_preserves_unrelated_lines(setenv, capsys):
    setenv["path"].write_text("# my notes\nOTHER_TOOL_TOKEN=keepme\nGEMINI_MODEL=a\n", encoding="utf-8")
    setenv["inputs"] = ["e"]
    setenv["editor"] = lambda t: t.replace("GEMINI_MODEL=a", "GEMINI_MODEL=b") + "BRAVE_SEARCH_API_KEY=x\n"
    setenv["run"]()
    text = setenv["path"].read_text(encoding="utf-8")
    assert "# my notes" in text and "OTHER_TOOL_TOKEN=keepme" in text and "GEMINI_MODEL=b" in text
    out = capsys.readouterr().out
    assert "keepme" not in out and "Added" in out and "Changed" in out


@pytest.mark.parametrize("inputs", [["c"], []])  # explicit cancel, or Ctrl+D / EOF
def test_cancel_leaves_file_untouched(setenv, inputs):
    original = f"GEMINI_API_KEY={FAKE_SECRET}\n"
    setenv["path"].write_text(original, encoding="utf-8")
    setenv["inputs"] = inputs
    setenv["run"]()
    assert setenv["path"].read_text(encoding="utf-8") == original and setenv["edits"] == 0


def test_show_settings_hides_secret_values(setenv, capsys):
    setenv["path"].write_text(f"GEMINI_API_KEY={FAKE_SECRET}\nGEMINI_MODEL=m-x\n", encoding="utf-8")
    setenv["inputs"] = ["s", "c"]
    setenv["run"]()
    out = capsys.readouterr().out
    assert "set (hidden)" in out and "m-x" in out and FAKE_SECRET not in out


def test_editor_failure_keeps_file(setenv):
    original = "GEMINI_MODEL=a\n"
    setenv["path"].write_text(original, encoding="utf-8")
    setenv["inputs"] = ["e"]

    def broken(text):
        raise ValueError("Editor 'nano' exited with code 1; nothing was saved.")

    setenv["editor"] = broken
    with pytest.raises(cli.CliError, match="exited with code 1"):
        setenv["run"]()
    assert setenv["path"].read_text(encoding="utf-8") == original


def test_invalid_syntax_declined_fix_keeps_file(setenv, capsys):
    original = "GEMINI_MODEL=a\n"
    setenv["path"].write_text(original, encoding="utf-8")
    setenv["inputs"] = ["e"]
    setenv["confirms"] = [False]
    setenv["editor"] = lambda t: t + f"this is not valid {FAKE_SECRET}\n"
    setenv["run"]()
    out = capsys.readouterr()
    assert setenv["path"].read_text(encoding="utf-8") == original
    assert "line 2" in out.err and FAKE_SECRET not in out.out + out.err


def test_invalid_syntax_can_be_fixed_in_editor(setenv):
    setenv["path"].write_text("GEMINI_MODEL=a\n", encoding="utf-8")
    setenv["inputs"] = ["e"]
    setenv["confirms"] = [True]
    attempts = iter([lambda t: t + "broken line\n", lambda t: t.replace("broken line\n", "")])
    setenv["editor"] = lambda t: next(attempts)(t)
    setenv["run"]()
    assert setenv["edits"] == 2


def test_write_failure_is_reported_without_secret(setenv, monkeypatch):
    setenv["confirms"] = [True]
    setenv["editor"] = lambda t: t + f"EXA_API_KEY={FAKE_SECRET}\n"

    def denied(path, text):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(envfile, "write_atomic", denied)
    with pytest.raises(cli.CliError) as exc:
        setenv["run"]()
    assert "Permission denied" in str(exc.value) and FAKE_SECRET not in str(exc.value)
    assert not setenv["path"].exists()


def test_tracked_env_triggers_warning(setenv, monkeypatch, capsys):
    monkeypatch.setattr(envfile, "git_tracks", lambda p: True)
    setenv["path"].write_text("GEMINI_MODEL=a\n", encoding="utf-8")
    setenv["inputs"] = ["c"]
    setenv["run"]()
    assert "TRACKED by git" in capsys.readouterr().err


def test_non_interactive_refuses_with_path(monkeypatch):
    monkeypatch.setattr(cli, "_is_interactive", lambda: False)
    with pytest.raises(cli.CliError, match=r"needs a terminal(.|\n)*\.env"):
        cli.cmd_env(None)


def test_command_is_registered_for_slash_completion():
    tree = shell.command_tree(cli.build_parser())
    assert "env" in tree and "quacker" not in tree
    assert "env" in [c[0] for c in shell.slash_completions("/en", tree)]


def test_ai_agent_cannot_edit_env():
    from quackers import agent

    for name, (_, schema, _) in agent.TOOLS.items():
        assert "env" not in name.split("_")
        args = {k: 1 if k == "job_id" else ["A=1"] if k == "settings" else "x"
                for k in schema.get("required", [])}
        if name == "set_application_status":
            args["status"] = "SHORTLISTED"
        assert agent.build_argv(name, args)[0] != "env"


# ---- real editor plumbing (term.edit_text) -------------------------------------


def test_edit_text_uses_editor_and_cleans_up(monkeypatch, tmp_path):
    seen = {}

    def fake_call(cmd):
        seen["cmd"] = cmd
        with open(cmd[-1], "a", encoding="utf-8") as fh:
            fh.write("B=2\n")
        return 0

    monkeypatch.setenv("EDITOR", "myeditor --wait")
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setattr(term.subprocess, "call", fake_call)
    assert term.edit_text("A=1\n", label="env", suffix=".env") == "A=1\nB=2\n"
    assert seen["cmd"][:2] == ["myeditor", "--wait"]
    assert not os.path.exists(seen["cmd"][-1])  # temp file with secrets removed


def test_edit_text_reports_missing_and_failing_editor(monkeypatch):
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.setattr(term.shutil, "which", lambda name: None)
    with pytest.raises(ValueError, match="No text editor found"):
        term.edit_text("x", label="env")
    monkeypatch.setenv("EDITOR", "myeditor")
    monkeypatch.setattr(term.subprocess, "call", lambda cmd: 2)
    with pytest.raises(ValueError, match="exited with code 2"):
        term.edit_text("x", label="env")


# ---- environment loading precedence ----------------------------------------------


def test_env_file_loads_once_and_os_env_wins(monkeypatch, tmp_path):
    from config import settings

    env = tmp_path / ".env"
    env.write_text("QUACKERS_T_FILE_ONLY=file\nQUACKERS_T_BOTH=file\n", encoding="utf-8")
    monkeypatch.setattr(settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(settings, "_ENV_LOADED", False)
    monkeypatch.setenv("QUACKERS_T_BOTH", "os")
    try:
        settings._load_env_file()
        assert os.environ["QUACKERS_T_FILE_ONLY"] == "file"
        assert os.environ["QUACKERS_T_BOTH"] == "os"
        env.write_text("QUACKERS_T_LATER=x\n", encoding="utf-8")
        settings._load_env_file()
        assert "QUACKERS_T_LATER" not in os.environ
    finally:
        for key in ("QUACKERS_T_FILE_ONLY", "QUACKERS_T_LATER"):
            os.environ.pop(key, None)

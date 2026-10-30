"""Tests for the new-terminal launcher (``quackers.launcher``).

All external side effects (subprocess.Popen, shutil.which, TTY) are mocked.
No real terminal window is opened. Windows branches are tested directly
because CI runs on Windows (``os.name == "nt"`` naturally).
"""
from __future__ import annotations

import os
import sys
import shlex as shlex_mod
from pathlib import Path

import pytest

from config.settings import PROJECT_ROOT
from quackers import launcher


# ---------------------------------------------------------------------------
# Recursion guard
# ---------------------------------------------------------------------------


def test_is_internal_launch_true(monkeypatch):
    monkeypatch.setenv(launcher.LAUNCH_ENV, "1")
    assert launcher.is_internal_launch() is True


def test_is_internal_launch_false(monkeypatch):
    monkeypatch.delenv(launcher.LAUNCH_ENV, raising=False)
    assert launcher.is_internal_launch() is False


# ---------------------------------------------------------------------------
# should_launch_new_terminal
# ---------------------------------------------------------------------------


def test_should_launch_false_on_internal(monkeypatch):
    monkeypatch.setenv(launcher.LAUNCH_ENV, "1")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    assert launcher.should_launch_new_terminal() is False


def test_should_launch_false_when_not_tty(monkeypatch):
    monkeypatch.delenv(launcher.LAUNCH_ENV, raising=False)
    monkeypatch.setenv(launcher.ENABLED_ENV, "true")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    assert launcher.should_launch_new_terminal() is False


def test_should_launch_true_when_tty_and_enabled(monkeypatch):
    monkeypatch.delenv(launcher.LAUNCH_ENV, raising=False)
    monkeypatch.setenv(launcher.ENABLED_ENV, "true")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    assert launcher.should_launch_new_terminal() is True


def test_should_launch_false_when_disabled(monkeypatch):
    monkeypatch.delenv(launcher.LAUNCH_ENV, raising=False)
    monkeypatch.setenv(launcher.ENABLED_ENV, "false")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    assert launcher.should_launch_new_terminal() is False


# ---------------------------------------------------------------------------
# build_spawn_command / _safe_command
# ---------------------------------------------------------------------------


def test_build_spawn_command():
    assert launcher.build_spawn_command() == [sys.executable, "-m", "quackers.internal"]


def test_safe_command_quotes_spaces():
    argv = ["/path/to/my venv/python.exe", "-m", "quackers.internal"]
    cmd = launcher._safe_command(argv)
    if os.name == "nt":
        # Windows branch: double quotes; tokens appear verbatim in the command.
        assert os.name == "nt"
        assert '"' + argv[0] + '"' in cmd
        assert "quackers.internal" in cmd and "-m" in cmd
    else:
        assert shlex_mod.split(cmd) == argv


def test_safe_command_windows_double_quotes_real_venv_path():
    token = r"C:\Users\D\desktop\hiremequackers\.venv\Scripts\python.exe"
    cmd = launcher._safe_command([token, "-m", "quackers.internal"])
    assert os.name == "nt"
    assert '"' + token + '"' in cmd
    # Must NOT be single-quoted (the original PowerShell bug).
    assert "'" + token + "'" not in cmd


# ---------------------------------------------------------------------------
# _terminal_launch — Windows branches only (CI runs on Windows; skip darwin/linux)
# ---------------------------------------------------------------------------


def test_terminal_launch_windows_wt(monkeypatch):
    monkeypatch.setattr(
        launcher.shutil, "which", lambda n: "/mock/wt" if n == "wt" else None
    )
    argv = launcher._terminal_launch("cmd-here")
    assert argv is not None
    assert argv[:2] == ["wt", "new-tab"]
    assert argv[-1] == "cmd-here"


def test_terminal_launch_windows_powershell(monkeypatch):
    monkeypatch.setattr(
        launcher.shutil, "which", lambda n: "/mock/ps" if n == "powershell" else None
    )
    argv = launcher._terminal_launch("x")
    assert argv is not None and argv[0] == "powershell"


def test_terminal_launch_windows_cmd(monkeypatch):
    monkeypatch.setattr(
        launcher.shutil, "which", lambda n: "/mock/cmd" if n == "cmd" else None
    )
    argv = launcher._terminal_launch("x")
    assert argv is not None and argv[0] == "cmd" and argv[1] == "/k"


def test_terminal_launch_windows_none(monkeypatch):
    monkeypatch.setattr(launcher.shutil, "which", lambda n: None)
    assert launcher._terminal_launch("x") is None


# ---------------------------------------------------------------------------
# launch()
# ---------------------------------------------------------------------------


def test_launch_success(monkeypatch):
    monkeypatch.setattr(
        launcher.shutil, "which", lambda n: "/mock/ps" if n == "powershell" else None
    )
    capture = {}

    def fake_popen(argv, **kwargs):
        capture["argv"] = argv
        capture["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)
    monkeypatch.delenv(launcher.LAUNCH_ENV, raising=False)
    monkeypatch.setenv(launcher.ENABLED_ENV, "true")

    result = launcher.launch()

    assert result is True
    assert capture["kwargs"]["env"][launcher.LAUNCH_ENV] == "1"
    assert Path(capture["kwargs"]["cwd"]) == PROJECT_ROOT
    combined = " ".join(capture["argv"])
    assert "-m" in combined and "quackers.internal" in combined


def test_launch_false_no_terminal(monkeypatch):
    monkeypatch.setattr(launcher.shutil, "which", lambda n: None)
    called = {}

    def _no(*a, **k):
        called["hit"] = True

    monkeypatch.setattr(launcher.subprocess, "Popen", _no)
    # Silence the "no suitable terminal" info message.
    monkeypatch.setattr(launcher.term, "info", lambda msg: None)
    assert launcher.launch() is False
    assert "hit" not in called


def test_launch_false_popen_raises(monkeypatch):
    monkeypatch.setattr(
        launcher.shutil, "which", lambda n: "/mock/ps" if n == "powershell" else None
    )

    def boom(*a, **k):
        raise OSError("no terminal")

    monkeypatch.setattr(launcher.subprocess, "Popen", boom)
    monkeypatch.setattr(launcher.term, "error", lambda msg: None)
    assert launcher.launch() is False  # must not raise


# ---------------------------------------------------------------------------
# Regression: cli.main() wiring
# ---------------------------------------------------------------------------


def test_main_with_subcommand_does_not_call_launcher(monkeypatch):
    called = {}

    def _no():
        called["hit"] = True

    monkeypatch.setattr(launcher, "launch", _no)
    monkeypatch.setattr(launcher, "should_launch_new_terminal", lambda: True)

    from quackers import cli

    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert "hit" not in called


def test_main_empty_argv_not_tty_runs_shell(monkeypatch):
    from quackers import cli, shell

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    monkeypatch.setenv(launcher.LAUNCH_ENV, "1")  # also disables recursion
    monkeypatch.setattr(shell, "run", lambda: 42)
    monkeypatch.setattr(cli.store, "ensure_db", lambda: None)
    monkeypatch.setattr("services.logger.setup_logging", lambda: None)

    assert cli.main([]) == 42

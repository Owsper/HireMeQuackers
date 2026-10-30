"""Launcher for opening the Quackers interactive shell in a *new* terminal window.

Design goals (see the task the user approved):

- When the user types ``quackers`` (no args) in PowerShell/CMD, open a
  dedicated terminal window that hosts the interactive shell, while the
  original window keeps working.
- Never recurse: the child process is marked via an internal env var so it
  runs the shell directly instead of spawning yet another window.
- Preserve the active virtualenv (same Python, same project root, same env),
  so the child reuses the same ``data/jobs.db``.
- Fail soft: if we cannot open a new terminal for any reason, fall back to
  running the shell in the current terminal, recording a clear message.

This module uses only the standard library (``os``, ``sys``, ``shutil``,
``subprocess``, ``shlex``) — no new dependencies.
"""
from __future__ import annotations

import os
import shutil
import shlex
import subprocess
import sys
from typing import List, Optional

from config.settings import PROJECT_ROOT
from quackers import term

LAUNCH_ENV = "QUACKERS_INTERNAL_LAUNCH"
ENABLED_ENV = "QUACKERS_LAUNCH_IN_NEW_TERMINAL"

_TRUE = {"1", "true", "t", "yes", "y", "on"}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, str(default))
    return raw.strip().lower() in _TRUE


def is_internal_launch() -> bool:
    """True when the current process was started by :func:`launch`."""
    return os.environ.get(LAUNCH_ENV, "").strip().lower() in _TRUE


def _launch_enabled() -> bool:
    """User setting ``launch_in_new_terminal`` (defaults to ``true``)."""
    return _env_bool(ENABLED_ENV, True)


def should_launch_new_terminal() -> bool:
    """Decide whether a bare ``quackers`` invocation should open a new window.

    Order matters:

    1. Recursion guard — an internal launch must never re-launch.
    2. If stdin/stdout are not a TTY we have nothing to attach to.
    3. The user setting.
    """
    if is_internal_launch():
        return False
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    return _launch_enabled()


def _terminal_launch(command: str) -> Optional[List[str]]:
    """Build the argv that opens a *new* terminal window running ``command``.

    Returns ``None`` when no suitable terminal application can be found.
    The command string is treated as an opaque shell command; the caller is
    responsible for quoting it (see :func:`_safe_command`).
    """
    if os.name == "nt":
        if shutil.which("wt"):
            return ["wt", "new-tab", "powershell", "-NoExit", "-Command", command]
        if shutil.which("powershell"):
            return ["powershell", "-NoExit", "-Command", command]
        if shutil.which("cmd"):
            return ["cmd", "/k", command]
        return None

    if sys.platform == "darwin":
        # osascript can drive Terminal.app without needing ``tell`` quoting tricks.
        return [
            "osascript",
            "-e",
            'tell application "Terminal" to do script '
            + '"' + command.replace("\\", "\\\\").replace('"', '\\"') + '"',
        ]

    # Linux — try the common emulators in a sensible order.
    for exe, *flags in (
        ("gnome-terminal", ["--"]),
        ("konsole", ["-e"]),
        ("xfce4-terminal", ["-e"]),
        ("xterm", ["-e"]),
    ):
        if shutil.which(exe):
            return [exe, *flags, command]
    return None


def _quote_win(token: str) -> str:
    """Quote one argv token for a Windows command line (PowerShell/cmd/WT).

    Always double-quote, and escape any embedded backslash-double-quote by
    doubling the backslash (the Windows command-line rule) so a trailing
    ``\"`` cannot inject an extra argument.
    """
    if '"' in token:
        return '"' + token.replace(r'\\"', r'\\\\\\"') + '"'
    return '"' + token + '"'


def _safe_command(argv: List[str]) -> str:
    """Return an opaque, safe shell command string for ``argv``.

    Windows shells (PowerShell / cmd / Windows Terminal) reject the single
    quotes that ``shlex.quote`` emits (e.g. ``'python.exe' -m ...`` fails),
    so on Windows each token is double-quoted instead.  POSIX shells keep
    ``shlex.quote`` so paths with spaces and metacharacters survive there.
    """
    if os.name == "nt":
        return " ".join(_quote_win(a) for a in argv)
    return " ".join(shlex.quote(a) for a in argv)


def build_spawn_command() -> List[str]:
    """argv that runs the interactive shell in the *same* Python install."""
    # Use ``-m quackers.internal`` so the child goes through the internal
    # entry point (no recursion), and inherits the active venv unchanged.
    return [sys.executable, "-m", "quackers.internal"]


def launch() -> bool:
    """Open the interactive shell in a new terminal window.

    Returns ``True`` when a new window was started, ``False`` when the
    caller should run the shell in the current terminal instead. Never
    raises: terminal-launch problems are reported and swallowed.
    """
    command = _safe_command(build_spawn_command())
    argv = _terminal_launch(command)
    if argv is None:
        # term.info outside a real TTY is harmless (it just prints).
        try:
            term.info("No suitable terminal app found; continuing in this terminal.")
        except Exception:  # noqa: BLE001 — never fatal
            print("[quackers] No suitable terminal app found; continuing here.")
        return False

    env = dict(os.environ)
    env[LAUNCH_ENV] = "1"

    try:
        subprocess.Popen(argv, cwd=str(PROJECT_ROOT), env=env)
        return True
    except Exception as exc:  # noqa: BLE001 — user-facing fallback, not fatal
        try:
            term.error(f"Could not open a new terminal: {exc}. Continuing here.")
        except Exception:  # noqa: BLE001
            print(f"[quackers] Could not open a new terminal: {exc}", file=sys.stderr)
        return False

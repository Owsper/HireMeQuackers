"""Terminal output helpers for the `quackers` CLI.

Everything printed here may contain text scraped from third-party job pages,
so `clean()` strips control characters (ANSI escape sequences, carriage
returns, BEL, ...) before anything reaches the terminal. Untrusted text must
never be able to move the cursor, rewrite earlier lines or change the
terminal title.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

# C0 + DEL + C1 controls, except tab and newline.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?")


def _enable_windows_ansi() -> bool:
    """Turn on VT processing for legacy Windows consoles. True on success."""
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:  # noqa: BLE001 - best effort only
        return False


def _color_enabled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stdout.isatty():
        return False
    return _enable_windows_ansi()


_COLOR = _color_enabled()
_CODES = {
    "bold": "1", "dim": "2", "red": "31", "green": "32",
    "yellow": "33", "blue": "34", "cyan": "36",
    "bright_cyan": "96",
}


def setup_stdout() -> None:
    """Never crash on characters the console code page cannot encode.

    Redirected output (files, pipes) is written as UTF-8 instead of the
    legacy Windows code page, which cannot represent "—", "✓" or umlauts.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.isatty():
                stream.reconfigure(errors="replace")
            else:
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def clean(value: Any) -> str:
    """Render any value as terminal-safe text (no control chars / escapes)."""
    if value is None:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = _ANSI_RE.sub("", text)
    return _CONTROL_RE.sub("", text)


def style(text: str, *names: str) -> str:
    if not _COLOR or not names:
        return text
    codes = ";".join(_CODES[n] for n in names if n in _CODES)
    return f"\x1b[{codes}m{text}\x1b[0m"


def heading(text: str) -> None:
    print()
    print(style(clean(text), "bold", "cyan"))


def info(text: str) -> None:
    print(clean(text))


def ok(text: str) -> None:
    print(style("✓ ", "green") + clean(text))


def warn(text: str) -> None:
    print(style("! ", "yellow") + clean(text), file=sys.stderr)


def error(text: str) -> None:
    print(style("✗ ", "red") + clean(text), file=sys.stderr)


def kv(pairs: Iterable[tuple], indent: int = 2) -> None:
    """Print aligned `key: value` lines."""
    rows = [(clean(k), clean(v)) for k, v in pairs]
    if not rows:
        return
    width = max(len(k) for k, _ in rows)
    pad = " " * indent
    for k, v in rows:
        print(f"{pad}{style(k.ljust(width), 'dim')}  {v}")


def bullets(items: Iterable[Any], indent: int = 2) -> None:
    pad = " " * indent
    for item in items:
        print(f"{pad}- {clean(item)}")


def paragraph(text: str, width: int = 100, indent: int = 2) -> None:
    """Print untrusted multi-line text, wrapped, without interpreting markup."""
    pad = " " * indent
    for line in clean(text).split("\n"):
        line = line.strip()
        if not line:
            print()
            continue
        for wrapped in textwrap.wrap(line, width=width) or [""]:
            print(pad + wrapped)


def _cell_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _fit(text: str, width: int) -> str:
    text = clean(text).replace("\n", " ")
    if _cell_width(text) <= width:
        return text + " " * (width - _cell_width(text))
    out, used = "", 0
    for ch in text:
        w = _cell_width(ch)
        if used + w > width - 1:
            break
        out += ch
        used += w
    return out + "…" + " " * (width - used - 1)


def table(
    rows: Sequence[Dict[str, Any]],
    columns: Sequence[str],
    *,
    headers: Optional[Dict[str, str]] = None,
    max_widths: Optional[Dict[str, int]] = None,
) -> None:
    """Print a plain-text table. Long cells are truncated with an ellipsis."""
    if not rows:
        return
    headers = headers or {}
    max_widths = max_widths or {}
    widths: Dict[str, int] = {}
    for col in columns:
        head = headers.get(col, col)
        longest = max(
            [_cell_width(head)]
            + [_cell_width(clean(fmt(r.get(col))).replace("\n", " ")) for r in rows]
        )
        widths[col] = min(longest, max_widths.get(col, 40))
    line = "  ".join(_fit(headers.get(c, c), widths[c]) for c in columns)
    print(style(line.rstrip(), "bold"))
    for r in rows:
        print("  ".join(_fit(fmt(r.get(c)), widths[c]) for c in columns).rstrip())


def fmt(value: Any) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.0f}" if value.is_integer() else f"{value:.1f}"
    return str(value)


def confirm(prompt: str, *, assume_yes: bool = False) -> bool:
    """Ask a y/N question. Non-interactive sessions answer "no"."""
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        return False
    try:
        answer = input(f"{prompt} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


NO_EDITOR_HELP = (
    "No text editor found. Set one, e.g.\n"
    '  PowerShell:  $env:EDITOR = "notepad"      (or "code --wait")\n'
    "  bash/zsh:    export EDITOR=nano          (or vim, or 'code --wait')"
)


def _editor_command() -> List[str]:
    """$VISUAL, then $EDITOR, then the first installed fallback editor."""
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if editor:
        return shlex.split(editor, posix=os.name != "nt")
    fallbacks = ["notepad"] if os.name == "nt" else ["nano", "vim", "vi"]
    for name in fallbacks:
        if shutil.which(name):
            return [name]
    raise ValueError(NO_EDITOR_HELP)


def edit_text(text: str, *, label: str, suffix: str = ".txt") -> Optional[str]:
    """Open `text` in the user's editor; return the edited text.

    Returns None when the file comes back unchanged. The temp file is created
    with mkstemp (owner-only permissions on POSIX) and always deleted, because
    callers may pass secrets. Raises ValueError if no editor can be started or
    the editor reports a failure.
    """
    fd, name = tempfile.mkstemp(prefix=f"quackers-{label}-", suffix=suffix)
    path = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        command = _editor_command()
        try:
            code = subprocess.call(command + [str(path)])
        except OSError as exc:
            raise ValueError(f"Could not start editor {command[0]!r} ({exc}).\n{NO_EDITOR_HELP}") from exc
        if code != 0:
            raise ValueError(f"Editor {command[0]!r} exited with code {code}; nothing was saved.")
        edited = path.read_text(encoding="utf-8")
    finally:
        path.unlink(missing_ok=True)
    return None if edited == text else edited


def edit_json(data: Dict[str, Any], *, label: str) -> Optional[Dict[str, Any]]:
    """Open `data` as JSON in the user's editor and return the edited dict.

    Returns None when the user leaves the file unchanged. Raises ValueError
    on invalid JSON or editor problems.
    """
    original = json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n"
    edited = edit_text(original, label=label, suffix=".json")
    if edited is None:
        return None
    try:
        parsed = json.loads(edited)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON ({exc}); nothing was saved.") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Expected a JSON object; nothing was saved.")
    return parsed

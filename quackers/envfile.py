"""`.env` handling for `quackers quacker setenv`.

The app loads `<project root>/.env` through python-dotenv in
`config.settings._load_env_file` (override=False, so variables already set
in the OS environment win). This module edits that same file: it never
creates a `.env` anywhere else, never prints secret values, and writes
atomically so an interrupted save cannot leave a half-written file.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple


class EnvVar(NamedTuple):
    name: str
    group: str
    description: str
    default: str = ""
    secret: bool = False
    url: str = ""


# Only variables that the code actually reads AND uses (config/settings.py,
# database/connection.py). Every one of them is optional.
ENV_VARS: Tuple[EnvVar, ...] = (
    EnvVar("GEMINI_API_KEY", "AI (Gemini)",
           "Enables AI match scoring, cover letters and plain-text questions in `quackers`.",
           secret=True, url="https://aistudio.google.com/apikey"),
    EnvVar("GEMINI_MODEL", "AI (Gemini)", "Gemini model id.", "gemini-2.0-flash"),
    EnvVar("ENABLE_AI_MATCHING", "AI (Gemini)", "Score jobs with Gemini during scans (true/false).", "true"),
    EnvVar("ENABLE_AI_DIRECTION", "AI (Gemini)", "Let Gemini word CV-tailoring suggestions (true/false).", "false"),
    EnvVar("SEARCH_PROVIDER", "Web search", "Web-search backend: brave, exa or duckduckgo.", "brave"),
    EnvVar("BRAVE_SEARCH_API_KEY", "Web search",
           "Brave Search API key. Without it web search falls back to DuckDuckGo.",
           secret=True, url="https://brave.com/search/api/"),
    EnvVar("EXA_API_KEY", "Web search", "Exa API key (used when SEARCH_PROVIDER=exa).",
           secret=True, url="https://exa.ai/"),
    EnvVar("EURES_API_URL", "Job sources",
           "EURES API endpoint. The EURES source is skipped when unset."),
    EnvVar("ENRICHMENT_MAX_PER_SCAN", "Scanning", "New jobs re-fetched for full details per scan.", "25"),
    EnvVar("ENRICHMENT_DELAY_SECONDS", "Scanning", "Pause between those detail fetches (seconds).", "1.5"),
    EnvVar("DATABASE_URL", "Storage", "SQLAlchemy database URL.", "sqlite:///./data/jobs.db"),
    EnvVar("DATA_DIR", "Storage", "Folder for CV copies, generated documents and logs.", "<project>/data"),
)
KNOWN = {v.name: v for v in ENV_VARS}
SECRET_HINT = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASS|AUTH)", re.I)

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def env_path() -> Path:
    """The `.env` the app loads: always the project root, never the cwd."""
    from config.settings import PROJECT_ROOT

    return PROJECT_ROOT / ".env"


def is_secret(name: str) -> bool:
    var = KNOWN.get(name)
    return var.secret if var else bool(SECRET_HINT.search(name))


def template() -> str:
    """Commented starter `.env` (also published as `.env.example`)."""
    lines = [
        "# HireMeQuackers configuration",
        "# Edit with `quackers quacker setenv`. This file holds secrets: never commit it.",
        "# Every setting is optional. Variables already set in your OS environment win.",
        "# Lines starting with # are ignored; remove the # to activate a setting.",
    ]
    group = None
    for var in ENV_VARS:
        if var.group != group:
            group = var.group
            lines += ["", f"# --- {group} ---"]
        lines.append(f"# {var.description}")
        if var.url:
            lines.append(f"# Get one at {var.url}")
        if var.secret:
            lines.append(f"{var.name}=")
        else:
            # Commented out: an empty value would override the built-in default.
            lines.append(f"# {var.name}={var.default}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Parsing / validation
# ---------------------------------------------------------------------------


@dataclass
class ParseResult:
    values: Dict[str, str] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)      # never contain values
    warnings: List[str] = field(default_factory=list)    # never contain values


def _unquote(raw: str) -> Tuple[str, bool]:
    """Return (value, ok). Strips matching quotes and trailing ` # comments`."""
    raw = raw.strip()
    if raw[:1] in {'"', "'"}:
        quote = raw[0]
        end = raw.find(quote, 1)
        if end == -1:
            return "", False
        return raw[1:end], True
    hash_at = raw.find(" #")
    return (raw[:hash_at] if hash_at != -1 else raw).strip(), True


def parse(text: str) -> ParseResult:
    result = ParseResult()
    seen: Dict[str, int] = {}
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export "):].lstrip()
        if "=" not in stripped:
            result.errors.append(f"line {lineno}: expected KEY=VALUE")
            continue
        key, raw = stripped.split("=", 1)
        key = key.strip()
        if not _KEY_RE.match(key):
            result.errors.append(f"line {lineno}: invalid variable name")
            continue
        value, ok = _unquote(raw)
        if not ok:
            result.errors.append(f"line {lineno}: {key} has an unclosed quote")
            continue
        if key in seen:
            result.warnings.append(
                f"{key} is defined twice (lines {seen[key]} and {lineno}); the first one is used")
            continue
        seen[key] = lineno
        result.values[key] = value
        var = KNOWN.get(key)
        if var and not var.secret and value == "":
            result.warnings.append(
                f"{key} is empty, which overrides its default ({var.default}); comment it out instead")
        if var is None:
            result.warnings.append(f"{key} is not a HireMeQuackers setting (kept as-is)")
    provider = result.values.get("SEARCH_PROVIDER")
    if provider and provider.lower() not in {"brave", "exa", "duckduckgo"}:
        result.warnings.append("SEARCH_PROVIDER should be brave, exa or duckduckgo")
    return result


def summarize_changes(old: Dict[str, str], new: Dict[str, str]) -> Dict[str, List[str]]:
    """Variable NAMES added / changed / removed (values are never returned)."""
    return {
        "added": sorted(set(new) - set(old)),
        "changed": sorted(k for k in set(new) & set(old) if new[k] != old[k]),
        "removed": sorted(set(old) - set(new)),
    }


def describe_value(name: str, value: Optional[str]) -> str:
    """Safe one-word description of a value for display."""
    if value is None:
        return "not set"
    if value == "":
        return "empty"
    return "set (hidden)" if is_secret(name) else value


# ---------------------------------------------------------------------------
# Files / git
# ---------------------------------------------------------------------------


def write_atomic(path: Path, text: str) -> bool:
    """Atomically write `text` to `path`. Returns True if the file was created.

    New files get mode 0600 on POSIX; existing files keep their permissions.
    """
    created = not path.exists()
    mode = None if created else stat.S_IMODE(path.stat().st_mode)
    fd, tmp_name = tempfile.mkstemp(prefix=".env.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if os.name != "nt":
            os.chmod(tmp, 0o600 if created else mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return created


def gitignore_covers_env(root: Path) -> bool:
    gi = root / ".gitignore"
    if not gi.exists():
        return False
    for line in gi.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip() in {".env", "/.env", ".env*", "*.env"}:
            return True
    return False


def ensure_gitignored(root: Path) -> bool:
    """Add `.env` to .gitignore if missing. Returns True when a rule was added."""
    if gitignore_covers_env(root):
        return False
    gi = root / ".gitignore"
    existing = gi.read_text(encoding="utf-8") if gi.exists() else ""
    sep = "" if not existing or existing.endswith("\n") else "\n"
    with gi.open("a", encoding="utf-8") as fh:
        fh.write(f"{sep}\n# Local secrets (quackers quacker setenv)\n.env\n")
    return True


def git_tracks(path: Path) -> Optional[bool]:
    """True/False if git tracks `path`; None when git or the repo is unavailable."""
    if not shutil.which("git"):
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(path.parent), "ls-files", "--error-unmatch", path.name],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if "not a git repository" in (proc.stderr or "").lower():
        return None
    return proc.returncode == 0

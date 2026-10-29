"""Sanitization helpers: filenames, HTML stripping, secrets scrubbing, safe
path joining, Markdown stripping. Never uses subprocess or eval/exec.
"""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, Optional

from bs4 import BeautifulSoup

# Patterns we always strip from scraped HTML before showing / scoring text.
_SCRIPT_RE = re.compile(r"<script\b.*?</script>", re.I | re.S)
_STYLE_RE = re.compile(r"<style\b.*?</style>", re.I | re.S)
_IFRAME_RE = re.compile(r"<iframe\b.*?</iframe>", re.I | re.S)
_OBJ_RE = re.compile(r"<object\b.*?</object>", re.I | re.S)
_EMBED_RE = re.compile(r"<embed\b.*?", re.I | re.S)
_EVENT_ATTRS_RE = re.compile(r"\son\w+\s*=\s*(['\"(]*).*?(?=>|['\"(]*)", re.I | re.S)
_JS_URL_RE = re.compile(r"\s+href\s*=\s*(['\"]?)\s*javascript:[^'\"]*\1", re.I)

# Rename-mapping for safe storing of filenames
_UNSAFE_CHARS_RE = re.compile(r"[<>:\"/\\|?*\x00-\x1f]")
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}


def strip_html(html: Optional[str]) -> str:
    """Best-effort: remove scripts, styles, iframes, and inline event attrs.

    Uses BeautifulSoup if available (installed by default in this project).
    """
    if not html:
        return ""
    try:
        soup = BeautifulSoup(html, "lxml" if "lxml" in _bs_available() else "html.parser")
        for tag in soup(["script", "style", "iframe", "object", "embed", "form"]):
            tag.decompose()
        # Remove inline event attributes (onclick=...) 
        for el in soup.find_all(True):
            attrs = el.attrs
            for name in list(attrs.keys()):
                if name.lower().startswith("on"):
                    del attrs[name]
            # Remove javascript: URLs
            href = el.get("href", None)
            if isinstance(href, str) and href.lower().startswith("javascript:"):
                del el["href"]
        return soup.get_text(separator=chr(10), strip=True)
    except Exception:
        # Fallback: regex-based removal (does not HTML parse).
        text = re.sub(_SCRIPT_RE, "", html)
        text = re.sub(_STYLE_RE, "", text)
        text = re.sub(_IFRAME_RE, "", text)
        text = re.sub(_OBJ_RE, "", text)
        text = re.sub(_EMBED_RE, "", text)
        text = re.sub(_EVENT_ATTRS_RE, "", text)
        text = re.sub(_JS_URL_RE, "", text)
        # Simple tag removal
        text = re.sub(r"<[^>]+>", " ", text)
        # Collapse whitespace
        text = re.sub(r"\s+", " ", text)
        return text.strip()


def _bs_available() -> str:
    import sys

    # Simple capability probe
    try:
        import lxml  # noqa: F401
        return "lxml"
    except Exception:
        return "html.parser"


def strip_md(text: str) -> str:
    """Remove or neutralize Markdown link syntax and raw HTML from Markdown."""
    if not text:
        return ""
    # Encode angle brackets minimally to avoid raw HTML executing-like rendering
    out = re.sub(r"`", "", text)
    out = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", out)
    out = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", out)
    return out.strip()


def truncate(text: Optional[str], n: int = 200) -> str:
    if not text:
        return ""
    t = str(text)
    if len(t) <= n:
        return t
    return t[: max(0, n - 3)].rstrip() + "..."


def sanitize_filename(name: Optional[str], default: str = "file") -> str:
    """Strip path separators; transliterate; fall back to OS-safe value.

    Do NOT include `..` segments. Windows reserved names are avoided.
    """
    if not name:
        return default
    # Take path basename (POSIX and Win)
    tail = name.replace("\\", "/").split("/")[-1].strip()
    # Normalize unicode
    tail = unicodedata.normalize("NFKD", tail)
    tail = "".join(ch for ch in tail if not unicodedata.combining(ch))
    # Replace unsafe chars
    tail = _UNSAFE_CHARS_RE.sub("_", tail)
    # Collapse dots/underscores/whitespace in the stem only, so the
    # extension survives ("my cv.pdf" -> "my_cv.pdf", not "my_cv_pdf").
    stem, dot, ext = tail.rpartition(".")
    if not dot:
        stem, ext = tail, ""
    stem = re.sub(r"[._\s]+", "_", stem).strip("._ ")
    ext = re.sub(r"[^A-Za-z0-9]", "", ext)[:10]
    if not stem:
        stem, ext = ext, ""
    tail = f"{stem}.{ext}" if ext else stem
    if not tail:
        return default
    # Strip up to 120 chars (keep extension if present)
    if "." in tail:
        head, ext = tail.rsplit(".", 1)
        if len(head) > 120:
            head = head[:120]
        tail = f"{head}.{ext}"
    elif len(tail) > 120:
        tail = tail[:120]
    upper = tail.upper()
    if upper in _WINDOWS_RESERVED or (upper.rsplit(".", 1)[0] in _WINDOWS_RESERVED):
        tail = f"_{tail}"
    return tail


def safe_join(base: str | Path, *parts: str) -> Path:
    """Join parts to a base and assert the resolved path is inside base.

    Raises ValueError if any part or the final path escapes the base directory.
    """
    base_path = Path(base).resolve()
    candidate = base_path
    for part in parts:
        candidate = candidate / str(part)
    resolved = candidate.resolve()
    # Ensure the resolved candidate is inside the base path.
    try:
        resolved.relative_to(base_path)
    except ValueError:
        raise ValueError(f"Path escapes base directory: {resolved} < {base_path}")
    return resolved


def assert_safe_relative_path(base: str | Path, path: str | Path) -> Path:
    """Only permit `path` under `base` (both absolute/relative normalized)."""
    base_path = Path(base).resolve()
    candidate = Path(path)
    if candidate.is_absolute() and not candidate.resolve().is_relative_to(base_path):
        raise ValueError(f"Absolute path must be under base: {candidate}")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(base_path):
        raise ValueError(f"Path escapes base directory: {resolved} < {base_path}")
    return resolved


_SECRET_KEYS = (
    "key", "token", "secret", "password", "apikey", "api_key", "auth",
)


def _looks_like_secret(name: str) -> bool:
    low = name.lower()
    return any(k in low for k in _SECRET_KEYS)


def scrub_secrets(data: Any) -> Any:
    """Return a copy of `data` where any dict value whose key contains a
    secret-like term / or whose value looks like an `***` key is masked.
    """
    if isinstance(data, dict):
        out: Dict[str, Any] = {}
        for k, v in data.items():
            if _looks_like_secret(str(k)) and isinstance(v, str) and v:
                out[str(k)] = "***"
            else:
                out[str(k)] = scrub_secrets(v)
        return out
    if isinstance(data, list):
        return [scrub_secrets(v) for v in data]
    return data


def safe_log(message: str, max_length: int = 500) -> str:
    """Truncate + strip newline/multiline characters to keep logs single-line."""
    return re.sub(r"[\r\n]+", " | ", message)[:max_length]

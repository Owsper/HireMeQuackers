"""Validation of a candidate-profile form before it is saved.

A *form* is the flat dict the CLI shows and edits (name, email, ...,
skills: [str], experience: [str], other: {...}).  Validation never fills
or guesses values; it only reports per-field status:

* ``ok``       — present and plausible
* ``missing``  — empty
* ``suspect``  — present but looks wrong (bad email, garbled PDF text, ...)

``errors`` block saving; ``warnings`` are shown but do not.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
PHONE_RE = re.compile(r"^\+?[\d\s()./-]{6,25}$")
URL_RE = re.compile(r"^(https?://)?[\w.-]+\.[a-z]{2,}(/\S*)?$", re.I)

TEXT_FIELDS = ("name", "email", "phone", "linkedin", "website", "summary")
LIST_FIELDS = ("skills", "certifications", "achievements", "experience",
               "education", "projects", "languages")
KNOWN_FIELDS = set(TEXT_FIELDS) | set(LIST_FIELDS) | {"cv_path", "other"}

#: JSON-template aliases -> canonical form keys.
ALIASES = {
    "full_name": "name",
    "professional_summary": "summary",
    "profile": "summary",
    "linkedin_url": "linkedin",
    "portfolio": "website",
    "github": "website",
}
#: Accepted template keys stored under ``other`` (no dedicated DB column).
OTHER_FIELDS = ("location", "job_titles", "target_roles", "availability", "work_authorization")

#: Minimum non-space characters for a PDF to count as readable.
MIN_READABLE_CHARS = 80


@dataclass
class CVValidation:
    status: Dict[str, str] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def looks_garbled(text: str) -> bool:
    """Heuristic for broken PDF text layers (CID glyphs, letter soup)."""
    t = (text or "").strip()
    if len(t) < 20:
        return False
    if "(cid:" in t or t.count("�") >= 3:
        return True
    letters = sum(c.isalpha() for c in t)
    if letters / max(len(t.replace(" ", "")), 1) < 0.45:
        return True
    words = re.findall(r"\w+", t)
    if words and sum(len(w) > 25 for w in words) / len(words) > 0.15:
        return True
    # single letters separated by spaces: "S o f t w a r e"
    return bool(re.search(r"(?:\b\w\b\s){8,}", t))


def readable_text_problem(raw_text: str) -> str:
    """Reason the extracted CV text is unusable, or "" when it is fine."""
    chars = len(re.sub(r"\s", "", raw_text or ""))
    if chars == 0:
        return ("No text could be extracted. The PDF is probably a scanned image "
                "or exported as pictures (no text layer).")
    if chars < MIN_READABLE_CHARS:
        return f"Only {chars} characters of text were found — the PDF is mostly images."
    if looks_garbled(raw_text):
        return ("The PDF's text layer is garbled (broken font encoding), so the "
                "extracted text is unreliable.")
    return ""


def normalize_json_cv(data: Dict[str, Any]) -> tuple[Dict[str, Any], List[str]]:
    """Map template aliases onto form keys; return (form, unknown_keys)."""
    form: Dict[str, Any] = {}
    other: Dict[str, Any] = dict(data.get("other") or {}) if isinstance(data.get("other"), dict) else {}
    unknown: List[str] = []
    for key, value in data.items():
        if key == "other":
            continue
        canon = ALIASES.get(key, key)
        if canon == "skills" and "skills" in form:
            values = value if isinstance(value, list) else [value] if isinstance(value, str) else []
            existing = form["skills"] if isinstance(form["skills"], list) else [form["skills"]]
            form["skills"] = list(dict.fromkeys(str(x) for x in existing + values if x))
            continue
        if key in {"technical_skills", "soft_skills", "tools", "technologies"}:
            values = value if isinstance(value, list) else [value] if isinstance(value, str) else []
            existing = form.get("skills", [])
            existing = existing if isinstance(existing, list) else [existing]
            form["skills"] = list(dict.fromkeys(str(x) for x in existing + values if x))
            other.setdefault("skill_groups", {})[key] = values
            continue
        if canon in KNOWN_FIELDS:
            if canon not in form or not form[canon]:
                form[canon] = value
        elif key in OTHER_FIELDS:
            other[key] = value
        else:
            unknown.append(key)
    if other:
        form["other"] = other
    return form, sorted(unknown)


def validate_form(form: Dict[str, Any]) -> CVValidation:
    v = CVValidation()

    def text(key: str) -> str:
        val = form.get(key)
        if val is None:
            return ""
        if not isinstance(val, str):
            v.errors.append(f"{key}: must be text, got {type(val).__name__}")
            v.status[key] = "suspect"
            return ""
        return val.strip()

    name = text("name")
    if "name" in v.status:
        pass
    elif not name or name.lower() == "unknown":
        v.status["name"] = "missing"
        v.errors.append("name: required (could not be found — fill it in)")
    elif len(name) > 80 or re.search(r"\d|@|https?://", name):
        v.status["name"] = "suspect"
        v.warnings.append(f"name: {name[:40]!r} does not look like a person's name")
    else:
        v.status["name"] = "ok"

    checks = {"email": EMAIL_RE, "phone": PHONE_RE, "linkedin": URL_RE, "website": URL_RE}
    for key, rx in checks.items():
        val = text(key)
        if key in v.status:
            continue
        if not val:
            v.status[key] = "missing"
        elif not rx.match(val):
            v.status[key] = "suspect"
            target = v.errors if key == "email" else v.warnings
            target.append(f"{key}: {val[:60]!r} is not a valid {key}")
        else:
            v.status[key] = "ok"

    summary = text("summary")
    if "summary" not in v.status:
        if not summary:
            v.status["summary"] = "missing"
        elif looks_garbled(summary):
            v.status["summary"] = "suspect"
            v.warnings.append("summary: text looks garbled")
        else:
            v.status["summary"] = "ok"

    for key in LIST_FIELDS:
        val = form.get(key)
        if val in (None, "", []):
            v.status[key] = "missing"
            continue
        if isinstance(val, str):
            val = [val]
        if not isinstance(val, list):
            v.status[key] = "suspect"
            v.errors.append(f"{key}: must be a list, got {type(val).__name__}")
            continue
        if any(not isinstance(x, (str, dict)) for x in val):
            v.status[key] = "suspect"
            v.errors.append(f"{key}: items must be text or objects")
            continue
        joined = " ".join(x if isinstance(x, str) else " ".join(map(str, x.values())) for x in val)
        if looks_garbled(joined):
            v.status[key] = "suspect"
            v.warnings.append(f"{key}: text looks garbled")
        else:
            v.status[key] = "ok"

    other = form.get("other")
    if other is not None and not isinstance(other, dict):
        v.errors.append("other: must be an object")

    for key in ("skills", "experience", "education"):
        if v.status.get(key) == "missing":
            v.warnings.append(f"{key}: empty — matching works better with it filled in")
    return v

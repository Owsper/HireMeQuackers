"""Local claim provenance. 'VERIFIED' means present in source, not independently certified."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterator

FIELDS = ("summary", "skills", "experience", "education", "certifications", "languages", "projects", "achievements")


def leaves(value: Any, path: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield from leaves(item, f"{path}.{key}" if path else key)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from leaves(item, f"{path}[{index}]")
    elif value is not None and str(value).strip():
        yield path, str(value).strip()


def payload(profile) -> dict:
    return {field: getattr(profile, field, None) or ([] if field != "summary" else "") for field in FIELDS}


def provenance(fields: dict, *, raw_text: str = "", source: str = "user", confirmed: bool = False) -> dict:
    """A ledger for every nonempty leaf, including the exact field/source quote."""
    norm = " ".join(raw_text.casefold().split())
    claims = []
    for field, value in leaves({k: fields.get(k) for k in FIELDS}):
        present = " ".join(value.casefold().split()) in norm if norm else False
        claims.append({"field": field, "value": value, "source": source,
            "source_quote": value if present else None,
            "status": "USER_CONFIRMED" if confirmed else "VERIFIED" if present else "INFERRED" if raw_text else "USER_PROVIDED"})
    return {"source": source, "source_sha256": hashlib.sha256(raw_text.encode()).hexdigest() if raw_text else None,
            "claims": claims, "missing_fields": [k for k in FIELDS if not fields.get(k)]}


def evidence(profile) -> list[dict]:
    try:
        other = json.loads(profile.other_json or "{}")
    except (ValueError, TypeError):
        other = {}
    if not isinstance(other, dict):
        other = {}
    ledger = other.get("provenance", {})
    ledger = ledger if isinstance(ledger, dict) else {}
    statuses = {c["field"]: c for c in ledger.get("claims", []) if isinstance(c, dict) and isinstance(c.get("field"), str)}
    result = []
    for path, text in leaves(payload(profile)):
        claim = statuses.get(path, {})
        # An inference is never evidence of a qualification.
        if claim.get("status") == "INFERRED":
            continue
        result.append({"field": path, "quote": text, "source": claim.get("source") or profile.cv_path or f"profile:{profile.id}",
                       "status": claim.get("status", "LEGACY_UNVERIFIED")})
    for field in ("languages", "education", "experience", "projects"):
        for index, item in enumerate(getattr(profile, field, []) or []):
            if isinstance(item, dict):
                prefix = f"{field}[{index}]"
                parts = [f["quote"] for f in result if f["field"].startswith(prefix + ".")]
                if parts:
                    result.append({"field": prefix, "quote": " | ".join(parts), "source": profile.cv_path or f"profile:{profile.id}", "status": "PROFILE_EVIDENCE"})
    for key in ("location", "work_authorization", "unavailable_skills"):
        if other.get(key):
            result.extend({"field": f"other.{key}", "quote": text, "source": f"profile:{profile.id}", "status": "USER_PROVIDED"}
                          for _, text in leaves(other[key]))
    return result

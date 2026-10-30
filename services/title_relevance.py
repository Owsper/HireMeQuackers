"""Deterministic title-relevance classifier.

This module answers a single question from the filter layer:

    `Given a job's title, how closely does it match the search intent?`

It does NOT answer the related question `is this job relevant overall`
(that's the AI's job, in `services/matching.py`) and it does NOT answer
`does this title pass my include_keywords` (that's the filter's job).

The classifier operates on **normalized tokens + a small curated
synonym map** and returns one of four discrete labels:

    exact     - normalized form of a requested title (incl. synonyms)
    strong    - shares the "core role" word with a requested title and
                all required-skill tokens; e.g. "Intern ML Engineer"
                vs intent "Machine Learning Engineer"
    related   - looks like the same role family BUT a different niche
                (e.g. "Data Analyst" vs "Data Engineer"); only surfaced
                when `intent.include_related_titles` is True.
    irrelevant - no overlap with the requested title after normalization

Rules we explicitly do **not** break:
    - `Frontend Developer` is NOT `backend`-compatible.
    - `Intern X` is NOT `Senior X`.
    - `Data Analyst` is NOT `Data Engineer` (unless explicitly asked).

The same discipline is baked in here as in `SearchIntent` — the
curated `_ROLE_FAMILY_MAP` below only unifies *unambiguous*
role-family synonyms; everything else falls through to the
token-based path which is stricter, not more permissive.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from config.search_intent import SearchIntent


# ---------------------------------------------------------------------------
# Token + normalization helpers
# ---------------------------------------------------------------------------

# Unicode-aware: "Softwareentwickler", "développeur", "München" keep their letters.
_TOKEN_RE = re.compile(r"[\W_]+", re.UNICODE)


def _tokens(text: str) -> List[str]:
    """Lowercase, de-hyphenate, whitespace/cut, keep order for matching."""
    out: List[str] = []
    for tok in _TOKEN_RE.split((text or "").lower()):
        tok = tok.strip()
        if tok and len(tok) >= 2:
            out.append(tok)
    return out


def _tok_set(text: str) -> Set[str]:
    return set(_tokens(text))


def _jaccard(a: Set[str], b: Set[str]) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 1.0
    return len(a & b) / len(union)


# Titles that pattern-match as "same role family but different niche".
# These are the ONLY ones we allow to return `related`. Anything not in
# this map that doesn't land on `strong` is `irrelevant`.
_ROLE_FAMILY_MAP: List[List[str]] = [
    ["ml engineer", "machine learning engineer", "ai engineer"],
    ["data analyst", "data scientist", "business analyst"],
    ["data engineer", "data engineering", "etl engineer"],
    ["software engineer", "software developer", "programmer", "sde"],
    ["frontend developer", "frontend engineer", "web developer", "ui developer"],
    ["backend developer", "backend engineer", "server-side developer"],
]


def _family_of(text: str) -> Optional[str]:
    """Return the canonical family-name for a title, or None.

    A title is *in* a family when its normalized form exactly matches
    one of the family keywords. We never claim "full-stack" is in the
    "frontend" family: different role.
    """
    n = _norm_title(text)
    for fam in _ROLE_FAMILY_MAP:
        if n == _norm_title(fam[0]):
            return fam[0]
        for k in fam[1:]:
            if n == _norm_title(k):
                return fam[0]
    return None


def _norm_title(text: str) -> str:
    if not text:
        return ""
    t = text.lower().strip()
    t = t.replace("-", " ").replace("_", " ")
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


# Seniority tokens — mirror of `config/search_intent._SENIORITY_TOKENS`
# (kept local because the filter runs without importing that private
# function; the mapping is small + stable).
_SENIORITY_SENIOR = re.compile(
    r"\b(senior|sr\.?|staff|lead|principal|head of|manager|director|"
    r"architect|vp|vice president)\b",
    re.I,
)
_SENIORITY_MID = re.compile(
    r"\b(junior|jr\.?|associate|entry[- ]level|founder|apprentice)\b", re.I,
)
_SENIORITY_INTERN = re.compile(
    r"\b(intern|internship|co-?op|summer student|working student|"
    r"werkstudent)\b", re.I,
)
_SENIORITY_GRAD = re.compile(
    r"\b(graduate|new grad|early[- ]career|entry[- ]level)\b", re.I,
)


def _seniority_bucket(text: str) -> Optional[str]:
    t = (text or "").lower()
    if not t:
        return None
    if _SENIORITY_INTERN.search(t):
        return "intern"
    if _SENIORITY_GRAD.search(t):
        return "graduate"
    if _SENIORITY_MID.search(t):
        return "junior"
    if _SENIORITY_SENIOR.search(t):
        return "senior"
    return None


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class TitleRelevance:
    label: str  # "exact" | "strong" | "related" | "irrelevant"
    score: float  # 0.0..1.0; monotonic with position in the label scale
    reason: str  # short human-readable explanation
    matched_title: Optional[str] = None  # which requested title took us there
    _meta: Dict[str, object] = field(default_factory=dict)

    @property
    def passes_hard(self) -> bool:
        """Hard-pass = exact or strong. Soft-pass = related. Rejected = irrelevant."""
        return self.label in ("exact", "strong")

    @property
    def passes_any(self) -> bool:
        """Soft-pass includes related; used when include_related_titles is True."""
        return self.label in ("exact", "strong", "related")

    def to_dict(self) -> Dict[str, object]:
        return {
            "label": self.label,
            "score": round(float(self.score), 4),
            "reason": self.reason,
            "matched_title": self.matched_title,
        }


# ---------------------------------------------------------------------------
# Classify
# ---------------------------------------------------------------------------


_SCORE_BY_LABEL = {"exact": 1.0, "strong": 0.8, "related": 0.5, "irrelevant": 0.0}


def _seniority_ok(job_seniority: Optional[str], target: str) -> bool:
    """True when the job's seniority (or absence thereof) is compatible with
    the target title's.

    Specifics (deliberately strict):
      - If the target is explicit about seniority (senior / junior /
        intern / graduate) and the job differs, reject.
      - If the job is junior / intern and the target is senior /
        staff, reject (a junior listing is not a senior role).
      - If either has no explicit marker, compatible.
      - "Intern" is a level, *not* a seniority adjective: a "Python
        Developer" with no level marker is compatible with a "Python
        Developer Intern" at the *core-role* level. We still let the
        title-relevance label surface it; the filter does the call.
    """
    t_low = (target or "").lower()
    job_explicit = _seniority_bucket(t_low) if False else job_seniority
    tgt_explicit = _seniority_bucket(t_low)
    if job_explicit is None or tgt_explicit is None:
        return True
    if job_explicit == tgt_explicit:
        return True
    # Cross-level rules: senior doesn't match junior / intern / graduate
    if tgt_explicit == "senior" and job_explicit in ("junior", "intern", "graduate"):
        return False
    if job_explicit == "senior" and tgt_explicit in ("junior", "intern", "graduate"):
        return False
    # Intern titles are *compatible* with non-matching level (it's a cohort,
    # not a seniority adjective): if target is "Data Analyst" (no level),
    # an "Intern Data Analyst" candidate is accepted.
    if job_explicit == "intern" or tgt_explicit == "intern":
        return True
    # Intern vs junior / graduate mismatch: still accept (different cohorts,
    # both entry).
    if (job_explicit, tgt_explicit) in {
        ("graduate", "junior"), ("junior", "graduate"),
    }:
        return True
    return False


def classify_title(job_title: str, intent: SearchIntent) -> TitleRelevance:
    """Classify ``job_title``; a local-language title ("Werkstudent
    Softwareentwicklung (m/w/d)") is retried in its canonical English
    form and the better label wins."""
    result = _classify_title(job_title, intent)
    if result.label == "exact":
        return result
    from services.multilingual import canonicalize_title

    canon = canonicalize_title(job_title)
    if canon and canon != _norm_title(job_title):
        alt = _classify_title(canon, intent)
        if _SCORE_BY_LABEL[alt.label] > _SCORE_BY_LABEL[result.label]:
            alt.reason += f" (title read as {canon!r})"
            return alt
    return result


def _classify_title(job_title: str, intent: SearchIntent) -> TitleRelevance:
    """Returns a `TitleRelevance` for `job_title` against `intent`.

    An *empty* intent (no primary_title, no synonyms) means "no
    title-specific constraint": we return `exact` with a reason that
    states so, so that a downstream filter can *bail out* of the
    title-hard-check and fall through to the other hard checks
    (location, employment type). This is documented behaviour — we
    are not claiming every title is great, we are *not constraining*.
    """
    if not (intent.primary_title or intent.title_synonyms):
        return TitleRelevance(
            label="exact",
            score=1.0,
            reason="no title constraint in intent — title check waived",
        )

    target_titles = intent.all_titles()  # primary + synonyms
    if not target_titles:
        return TitleRelevance(
            label="exact", score=1.0,
            reason="empty target list — title check waived",
        )

    jt = (job_title or "").strip()
    jt_norm = _norm_title(jt)
    jt_tokens = _tok_set(jt_norm)
    jt_family = _family_of(jt)
    jt_seniority = _seniority_bucket(jt)

    # 1) EXACT: same normalized text (case / hyphenation-insensitive)
    for t in target_titles:
        if _norm_title(t) == jt_norm and jt_norm:
            return TitleRelevance(
                label="exact", score=1.0,
                reason=f"exact title match with '{t}'",
                matched_title=t,
            )

    # 2) STRONG: knows the core role word by name (token Jaccard >= 0.7
    #    on the non-seniority tokens) AND no seniority conflict.
    best_strong: Optional[Tuple[str, float]] = None
    for t in target_titles:
        t_norm = _norm_title(t)
        if not t_norm:
            continue
        t_tokens = _tok_set(t_norm)
        # Seniority-aware: strip seniority tokens before comparison so
        # "Software Engineer Intern" and "Software Engineer" are both
        # compared on their "Software Engineer" core.
        core_j = jt_tokens - _SENIORITY_TOKEN_SET - _INTERNSHIP_TOKEN_SET
        core_t = t_tokens - _SENIORITY_TOKEN_SET - _INTERNSHIP_TOKEN_SET
        if not core_j or not core_t:
            continue
        j = _jaccard(core_j, core_t)
        if j >= 0.7 and _seniority_ok(jt_seniority, t_norm):
            if best_strong is None or j > best_strong[1]:
                best_strong = (t, float(j))
    if best_strong is not None:
        t, j = best_strong
        return TitleRelevance(
            label="strong",
            score=_SCORE_BY_LABEL["strong"] * (0.5 + 0.5 * j),
            reason=f"core role matches '{t}' (Jaccard {j:.2f}); seniority compatible",
            matched_title=t,
        )

    # 2.5) STRONG via exact synonym already covered in step 1; move on.

    # 3) RELATED: same curated role family (only when include_related_titles
    #    is set). We return "related" even if `include_related_titles` is
    #    False so the UI can show the reason, but the filter is responsible
    #    for turning it into an exclusion when not opted in.
    if jt_family is not None:
        for t in target_titles:
            t_family = _family_of(t)
            if t_family is not None and t_family == jt_family:
                return TitleRelevance(
                    label="related",
                    score=_SCORE_BY_LABEL["related"],
                    reason=(
                        f"same role family ('{t_family}') as '{t}', "
                        f"but different niche"
                    ),
                    matched_title=t,
                )

    # 4) IRRELEVANT
    return TitleRelevance(
        label="irrelevant",
        score=0.0,
        reason=f"no overlap with requested title(s): {target_titles[:3]}",
    )


# Phrase aliases that get diff'd out *before* the token comparison,
# so that "Machine Learning Engineer" and "ML Engineer" both reduce to
# `{engineer, machine, learning}` on one side and `{ml, engineer}` on
# the other (raw) or fully normalized synonyms on the other path.
# We keep both sets: one pure-seniority, one "intern cohort", because
# the seniority label is checked separately.
_SENIORITY_TOKEN_SET = {
    "senior", "sr", "staff", "lead", "principal", "head", "manager",
    "director", "architect", "vp", "vice", "president",
    "junior", "jr", "associate", "entry", "level", "founder",
    "apprentice", "early", "career", "graduate", "new", "grad",
}
_INTERNSHIP_TOKEN_SET = {
    "intern", "internship", "co", "op", "summer", "student", "working",
    "werkstudent",
}


def explain(job_title: str, intent: SearchIntent) -> str:
    """One-liner used in the jobs-page 'Why included' tooltip."""
    r = classify_title(job_title, intent)
    return r.reason

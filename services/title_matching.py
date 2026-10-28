"""Title relevance matching for job-search accuracy.

Provides a 4-tier ranking (`exact`, `strong`, `related`, `irrelevant`) that
classifies how closely a job title matches the user's requested role.

Design rules
------------
- A seniority-level mismatch degrades the tier:
  "Software Engineering Intern" vs "Senior Software Engineer" -> related (never exact/strong)
- Cross-role confusion is avoided:
  "Data Analyst" vs "Data Engineer" -> related (never strong)
  "Frontend Developer" vs "Backend Developer" -> related (never strong)
- Synonyms and abbreviations are folded before comparing:
  "ML Engineer" == "Machine Learning Engineer" -> exact
  "Dev" == "Developer" == "Engineer" (in title context) -> strong

All matching is deterministic and case-insensitive.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

_ABBREVIATIONS: dict[str, str] = {
    # Acronyms / short forms -> canonical token
    "ml": "machine learning",
    "dl": "deep learning",
    "nlp": "natural language processing",
    "ai": "artificial intelligence",
    "dev": "developer",
    "sr": "senior",
    "jr": "junior",
    "sw": "software",
    "fe": "frontend",
    "be": "backend",
    "fl": "full",
    "ft": "full",
    "pt": "part",
    "se": "software engineer",
    "swe": "software engineer",
    "ds": "data science",
    "qa": "quality assurance",
    "vp": "vice president",
    "cto": "chief technology officer",
    "cfo": "chief financial officer",
    "ceo": "chief executive officer",
    "coo": "chief operating officer",
    "cto": "chief technology officer",
    "csmo": "chief supplies and marketing officer",
    "pm": "product manager",
    "po": "product owner",
    "kp": "key person",
    "ca": "certified association",
    "us": "united states",
    "uk": "united kingdom",
    "au": "australia",
    "de": "germany",
    "fr": "france",
    "es": "spain",
    "it": "italy",
    "nl": "netherlands",
    "be": "belgium",
    "at": "austria",
    "ch": "switzerland",
    "fi": "finland",
    "pl": "poland",
    "pt": "portugal",
    "ie": "ireland",
    "il": "israel",
    "sg": "singapore",
    "my": "malaysia",
    "in": "india",
    "cn": "china",
    "jp": "japan",
    "kr": "south korea",
    "br": "brazil",
    "mx": "mexico",
    "ar": "argentina",
}

# Job titles are case-insensitive and punctuation-sensitive in meaning.
_PUNCT_RE = re.compile(r"[^a-z0-9\s]")
_MULTI_SPACE_RE = re.compile(r"\s+")

# Seniority tokens (check in order; longer/more specific first)
_SENIORITY_TOKENS: list[tuple[str, str]] = [
    ("working student", "working_student"),
    ("new grad", "graduate"),
    ("entry-level", "entry"),
    ("senior", "senior"),
    ("junior", "junior"),
    ("staff", "staff"),
    ("principal", "principal"),
    ("lead", "lead"),
    ("head", "head"),
    ("manager", "manager"),
    ("director", "director"),
    ("intern", "intern"),
    ("entry", "entry"),
    ("graduate", "graduate"),
    ("apprentice", "apprentice"),
    ("werkstudent", "working_student"),
]


def _normalize_title(raw: str) -> str:
    """Lowercase, strip punctuation, expand abbreviations, collapse spaces."""
    if not raw:
        return ""
    t = raw.lower()
    t = _PUNCT_RE.sub(" ", t)
    # Expand known abbreviations (token-by-token)
    tokens = t.split()
    expanded = []
    for tok in tokens:
        expanded.append(_ABBREVIATIONS.get(tok, tok))
    t = " ".join(expanded)
    t = _MULTI_SPACE_RE.sub(" ", t).strip()
    return t


def _extract_seniority(title: str) -> Optional[str]:
    """Return the detected seniority level or None."""
    t = f" {title.lower()} "
    for token, level in _SENIORITY_TOKENS:
        date_str = token if isinstance(token, str) else token
        # Handle non-string keys (defensive)
        if isinstance(token, tuple):
            continue
        if date_str in t:
            return level
    return None


# ---------------------------------------------------------------------------
# Core result type
# ---------------------------------------------------------------------------

@dataclass
class TitleMatch:
    """Result of a single title-relevance comparison."""

    tier: str  # 'exact' | 'strong' | 'related' | 'irrelevant'
    rationale: str = ""
    seniority_mismatch: bool = False
    tokens_matched: int = 0
    total_tokens: int = 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def title_relevance(job_title: str, config) -> TitleMatch:
    """Classify how closely ``job_title`` matches the SearchConfig's intent.

    ``config`` may be a SearchConfig or any object with these attributes:
        primary_title (str, optional)
        title_synonyms (list[str])
        include_keywords (list[str])
        required_skills (list[str])

    Returns a TitleMatch.  Tier ordering:
        exact   — same canonical title (after synonym expansion)
        strong  — shares >= 60% of meaningful tokens with the primary title
        related — shares >= 30% or matches a known related-role pattern
        irrelevant — no meaningful overlap
    """
    if not job_title:
        return TitleMatch(tier="irrelevant", rationale="empty job title")

    primary = _get_primary_title(config)
    synonyms = _get_synonyms(config)
    keywords: List[str] = list(getattr(config, "include_keywords", None) or [])

    # 1. Exact match: normalized primary or any synonym
    norm_job = _normalize_title(job_title)
    candidates = []
    if primary:
        candidates.append(_normalize_title(primary))
    for syn in synonyms:
        candidates.append(_normalize_title(syn))

    for cand in candidates:
        if cand and norm_job == cand:
            return TitleMatch(tier="exact", rationale=f"exact title match")

    if not primary and not keywords:
        # No specific title configured — fall back to keyword-only relevance
        return _keyword_relevance(norm_job, keywords)

    # 2. Strong: high token overlap with primary or a synonym
    best_overlap = 0.0
    best_candidate = ""
    for cand in candidates:
        if not cand:
            continue
        overlap = _token_overlap(norm_job, cand)
        if overlap > best_overlap:
            best_overlap = overlap
            best_candidate = cand

    seniority_job = _extract_seniority(job_title)
    seniority_primary = None
    if primary:
        seniority_primary = _extract_seniority(primary)

    seniority_mismatch = (
        seniority_job is not None
        and seniority_primary is not None
        and seniority_job != seniority_primary
    )

    if best_overlap >= 0.8:
        if seniority_mismatch:
            return TitleMatch(
                tier="related",
                rationale=f"seniority mismatch: job '{seniority_job}' vs search '{seniority_primary}'",
                seniority_mismatch=True,
            )
        return TitleMatch(tier="exact", rationale="high token overlap with primary title")

    if best_overlap >= 0.5:
        if seniority_mismatch:
            return TitleMatch(
                tier="related",
                rationale=f"seniority mismatch: job '{seniority_job}' vs search '{seniority_primary}'",
                seniority_mismatch=True,
            )
        return TitleMatch(
            tier="strong",
            rationale="strong token overlap with primary/synonym",
            tokens_matched=int(best_overlap * _count_tokens(best_candidate)),
            total_tokens=_count_tokens(best_candidate),
        )

    if best_overlap >= 0.25:
        return TitleMatch(
            tier="related",
            rationale=f"partial token overlap ({best_overlap:.0%}) with primary/synonym",
        )

    # 3. Keyword fallback when title isn't close but config has keywords
    kw_match = _keyword_relevance(norm_job, keywords)
    if kw_match.tier != "irrelevant":
        return kw_match

    return TitleMatch(tier="irrelevant", rationale="no meaningful overlap")


def _token_overlap(a: str, b: str) -> float:
    """Jaccard-like overlap ratio between two normalized titles."""
    words_a = set(w for w in a.split() if len(w) > 1)
    words_b = set(w for w in b.split() if len(w) > 1)
    if not words_a or not words_b:
        return 0.0
    inter = words_a & words_b
    union = words_a | words_b
    return len(inter) / len(union) if union else 0.0


def _count_tokens(title: str) -> int:
    return len([w for w in title.split() if len(w) > 1])


def _keyword_relevance(norm_job: str, keywords: List[str]) -> TitleMatch:
    """Fallback relevance based on config include_keywords."""
    if not keywords:
        return TitleMatch(tier="irrelevant", rationale="no keywords configured")
    hits = sum(1 for kw in keywords if kw.lower() in norm_job)
    if hits >= 2:
        return TitleMatch(tier="related", rationale=f"matches {hits} keywords")
    if hits == 1:
        return TitleMatch(tier="irrelevant", rationale="1 keyword only — not enough")
    return TitleMatch(tier="irrelevant", rationale="no keyword match")


def _get_primary_title(config) -> str:
    primary = getattr(config, "primary_title", None)
    if primary and isinstance(primary, str) and primary.strip():
        return primary.strip()
    return ""


def _get_synonyms(config) -> List[str]:
    syns = getattr(config, "title_synonyms", None)
    if isinstance(syns, (list, tuple)):
        return [s.strip() for s in syns if isinstance(s, str) and s.strip()]
    return []
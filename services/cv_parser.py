"""CV extraction: PDF via PyMuPDF (optional), DOCX via python-docx.

Returns plain structured dict for later upsert into `CandidateProfile`.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _suffix_is_supported(p: Path) -> str:
    """Return '.pdf'/'.docx' if the file should be treated as that
    format, else ''.

    Uses **content sniffing first** (magic bytes) rather than the
    extension, because tools like FlowCV save PDFs with arbitrary or
    extra trailing extensions (e.g. ``name.extended_pdf`` or even a
    hidden file name).  If magic bytes are inconclusive, fall back to
    extension checks (any dot-segment or stem suffix).
    The actual read still happens via the library by path, so this is
    purely a format gate.
    """
    # 1) Content sniff: PDF magic / DOCX (zip) first-entry check.
    try:
        with open(p, "rb") as f:
            head = f.read(8)
        if head.startswith(b"%PDF-"):
            return ".pdf"
        # DOCX is a ZIP archive; magic bytes are PK\x03\x04.
        # A non-docx zip would still fail later in python-docx
        # (raises a handled RuntimeError), so this is safe.
        if head.startswith(b"PK\x03\x04"):
            return ".docx"
    except Exception:
        pass

    # 2) Extension checks (only reached when content sniff was
    #    inconclusive, e.g. an encrypted PDF lacking the standard
    #    header, or a sparse/temp file not yet flushed).
    s = p.suffix.lower()
    if s in {".pdf", ".docx"}:
        return s
    for seg in p.suffixes:
        seg = seg.lower()
        if seg in {".pdf", ".docx"}:
            return seg
    stem = p.stem.lower()
    if stem.endswith("pdf"):
        return ".pdf"
    if stem.endswith("docx"):
        return ".docx"
    return ""


def parse_cv(path: str | Path, *, extract_sections: bool = True) -> Dict[str, Any]:
    p = Path(path)
    text = _read_raw(p)
    if extract_sections:
        fields = _split_sections(text)
    else:
        fields = {}
    return {
        "cv_path": str(p),
        "raw_text": text,
        "fields": fields,
        "confidence": fields.get("_confidence", {}),
    }


def _read_raw(p: Path) -> str:
    if not p.exists():
        raise FileNotFoundError(f"CV file not found: {p}")
    fmt = _suffix_is_supported(p)
    if fmt == ".pdf":
        # Prefer the modern `pymupdf` module (PyMuPDF >= 1.24). Fall back
        # to the legacy `fitz` name for very old installs that only expose
        # that name.  Distinguish ImportError (real missing library) from
        # other parse errors (encrypted / corrupt PDF) so we never silently
        # produce garbage from the crude regex fallback.
        import importlib

        pymupdf = None
        try:
            pymupdf = importlib.import_module("pymupdf")
        except ImportError:
            try:
                pymupdf = importlib.import_module("fitz")
            except ImportError as exc:
                raise RuntimeError(
                    "PyMuPDF is not importable. Install it with "
                    f"`pip install pymupdf`. Original error: {exc}"
                ) from exc
        try:
            doc = pymupdf.open(str(p))
            try:
                text = _pdf_blocks_text(doc)
            finally:
                doc.close()
            logger.info("PDF parsed via PyMuPDF: %d chars (%s)", len(text), p.name)
            return text
        except Exception:
            # Parse-time failure (not a missing library): encrypted or
            # corrupt PDF. Fall back to the crude regex extractor with a
            # loud warning so the user knows it's low-quality.
            logger.warning(
                "PyMuPDF open/read failed for %s; using low-quality fallback",
                p.name,
            )
            data = p.read_bytes()
            return _pdf_fallback_extract(data)
    if fmt == ".docx":
        try:
            from docx import Document

            doc = Document(str(p))
            parts = []
            for para in doc.paragraphs:
                if para.text is None:
                    continue
                text = para.text
                style = para.style.name.lower() if para.style else ""
                prefix = ""
                if "heading1" in style or "title" in style:
                    prefix = "\n# "
                elif "heading2" in style:
                    prefix = "\n## "
                elif "heading3" in style:
                    prefix = "\n### "
                parts.append(prefix + text)
            # Include tables (e.g., one for education)
            for table in doc.tables:
                for row in table.rows:
                    cells = [
                        (cell.text or "").strip().replace("\n", " | ")
                        for cell in row.cells
                    ]
                    parts.append(" | ".join(c for c in cells if c))
            return "\n".join(parts)
        except Exception as exc:
            raise RuntimeError(f"Failed to parse DOCX: {exc}") from exc
    raise ValueError(f"Unsupported CV format: {p.name}")


def _pdf_blocks_text(doc) -> str:
    """Extract text from a PyMuPDF document using 2-D ``blocks`` +
    coordinate sort, which works far better for two-column CV layouts
    than the flat 1-D string from ``get_page_text``.

    PyMuPDF's ``page.get_text("blocks")`` returns a list of tuples
    ``x0, y0, x1, y1, block_text, block_no, block_type``. When you call
    ``get_page_text`` on a two-column document (the common FlowCV /
    resume-builder layout), the two columns are **interleaved** in the
    output — left-column line 1, then right-column line 1, then
    left-column line 2 — which makes downstream section splitting
    (searching for "Experience" as a standalone line) unreliable. Using
    blocks + **(y, x) sort** reconstructs the visual reading order
    (top-to-bottom, and within the same vertical *line* band,
    left-to-right), so the result reads like *"here's a human eyeballing
    the page"* rather than *"here's the page's raw text stream".

    Accept a doc so it can be used inside the existing ``try/finally``
    that owns the file handle. Returns a single text block per page
    (pages joined by \\n, like before, to preserve downstream
    behaviour that splits on blank lines / newlines).
    """
    try:
        page_count = doc.page_count
    except Exception:
        return ""

    per_page: List[str] = []
    for i in range(page_count):
        page = doc[i]
        try:
            blocks = page.get_text("blocks", sort=True)
        except Exception:
            # Some PDFs with unusual content streams can raise here;
            # fall back to the flat text so we at least return *something*.
            per_page.append(page.get_text())
            continue
        if not blocks:
            # Degenerate: no blocks at all -> the flat path is enough.
            per_page.append(page.get_text())
            continue

        # `sort=True` already does a (y0, x0) primary sort, but we want a
        # *visual-line* sort: blocks that share a vertical band (within
        # ~6 points of each other's y0) belong to the same visual line
        # and should be ordered left-to-right **within that band**. We
        # sort by (y0, x0) directly since PyMuPDF has already given us
        # text blocks (not individual words); one block per text row
        # and the (y0, x0) tiebreak handles side-by-side blocks.
        #
        # In PyMuPDF 2.0 the block_type indices can vary; accept any
        # block that has non-empty text at index 4, and prefer to keep
        # blocks whose type is 0 (text) or 1 (image-adjacent text).
        # The len(b) >= 5 guard is a safety net for short tuples.
        candidates = [
            b for b in blocks
            if b and len(b) >= 5
        ]
        # Prefer type 0 (text) first; if PyMuPDF 2.0 returns only other
        # types, accept any block with a non-empty string at index 4.
        typed = [b for b in candidates if b[6] in (0, 1)]
        if not typed:
            # PyMuPDF 2.0 may use different type numbers; accept any
            # block whose text field (index 4) is a non-empty str.
            typed = [b for b in candidates if isinstance(b[4], str) and b[4].strip()]
        ordered = sorted(
            typed,
            key=lambda b: (round(b[1]), b[0]),
        )
        # "Don't return empty" guard: if every block on this page was
        # filtered out (all types unknown, or all empty), fall back to
        # the flat get_text() so the caller always gets *something*.
        if not ordered or not any((b[4] or "").strip() for b in ordered):
            per_page.append(page.get_text())
            continue
        parts = [
            (b[4] or "").strip() for b in ordered
        ]
        per_page.append("\n".join(t for t in parts if t))

    return "\n".join(per_page)


def _pdf_fallback_extract(data: bytes) -> str:
    """Very crude text extraction fallback without PyMuPDF.

    Most modern PDF writers (FlowCV, Word->PDF, LaTeX) use FlateDecode
    streams, so we inflate every `stream ... endstream` block with zlib
    before scanning for `(...)Tj` text-show commands.  Still low-quality
    (no layout/columns), but vastly better than raw-read for compressed
    PDFs.
    """
    import zlib

    # Inflate every FlateDecode stream so Tj commands become visible.
    inflated_chunks: list[bytes] = []
    for block in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", data, re.S):
        raw = block.group(1)
        try:
            inflated_chunks.append(zlib.decompress(raw))
        except Exception:
            # Not Flate (could be ASCII85 / LZW / uncompressed) — keep raw.
            inflated_chunks.append(raw)

    haystack = b"\n".join(inflated_chunks) or data

    out: list[str] = []
    # Find `(...)Tj` strings (PDF text-show commands).
    for m in re.findall(rb"\((?:[^()\\]|\\.)*\)\s*Tj", haystack):
        body = m[: m.rfind(b")")].lstrip(b"(")
        # De-escape common PDF escapes: \( \) \\ \\n \\r \\t and octal.
        body = re.sub(rb"\\([()\\])", rb"\1", body)
        body = body.replace(b"\\n", b" ").replace(b"\\r", b" ")
        body = body.replace(b"\\t", b" ")
        body = re.sub(rb"\\(\d{1,3})", lambda g: bytes([int(g.group(1), 8) & 0xFF]), body)
        try:
            out.append(body.decode("latin-1", errors="replace"))
        except Exception:
            pass

    # Second pass: strings stored with TJ (arrays) — content streams emit
    # `[(he) -12 (llo)] TJ`.  Pulling each parenthesised atom is enough for
    # a usable rough transcript.
    if not out:
        atoms = re.findall(rb"\((?:[^()\\]|\\.)*\)", haystack)
        for a in atoms:
            a = a[1:-1]
            a = re.sub(rb"\\([()\\])", rb"\1", a)
            try:
                t = a.decode("latin-1", errors="replace")
            except Exception:
                continue
            if len(t) >= 2:
                out.append(t)

    return " ".join(out).strip()


# Canonical section keys returned by `_normalize_heading` (in priority
# order). Longer / more specific phrases first so they win the exact match.
# Every phrase accepted by `_HEADING_PHRASES` must resolve here.
_HEADINGS = [
    "professional summary", "personal statement", "profile", "summary",
    "about me", "about", "career objective", "objective",
    "work experience", "professional experience", "employment history",
    "work history", "work", "experience", "employment",
    "technical skills", "core skills", "skills", "technical",
    "personal projects", "projects",
    "certifications", "certificates", "certification",
    "education history", "education", "academics", "academic",
    "achievements", "achievement", "awards", "honors", "honours",
    "languages",
    "interests", "hobbies",
]

# Exact short-phrase set (lowercased) used by `_is_heading_line` to decide
# whether an isolated line is a heading. Includes common aliases and the
# canonical keys above so Title/UPPERcase headings are all recognised.
_HEADING_PHRASES = frozenset(
    {
        "summary", "professional summary", "personal statement", "profile",
        "about me", "about", "career objective", "objective",
        "experience", "work experience", "professional experience",
        "work history", "employment", "employment history", "work",
        "skills", "technical skills", "core skills", "technical",
        "skills & tools", "tools & skills",
        "projects", "personal projects",
        "certifications", "certificates", "certification",
        "education", "education history", "academics", "academic",
        "achievements", "achievement", "awards", "honors", "honours",
        "languages", "interests", "hobbies",
    }
)

def _normalize_heading(h: str) -> Optional[str]:
    hh = (h or "").strip().lower()
    hh = re.sub(r":$", "", hh).strip()
    if not hh:
        return None
    for canonical in _HEADINGS:
        if canonical == hh:
            return canonical
    # fuzzy
    for canonical in _HEADINGS:
        if canonical in hh:
            return canonical
    return None


def _is_heading_line(stripped: str) -> bool:
    """True if `stripped` looks like a standalone section heading.

    CVs produced by FlowCV / PDF text dumps / DOCX render headings as a
    short, isolated line in ANY case ("Skills", "EXPERIENCE", "Education:").
    We accept it when:
      - it is short (a few words / chars) so body text is never mistaken
        for a heading,
      - it is not list/bullet content (no leading "-", "*", "•", "."),
      - it does not end like a sentence (no trailing period),
      - and it **exactly** matches a known heading (case-insensitive,
        allowing a trailing colon).
    The "exact match" requirement keeps sentences like
    "I have experience with Python" out — those are not headings.
    """
    if not stripped:
        return False
    # Not a heading if it's a bullet or a long line.
    if stripped[0] in {"-", "*", "•", "·", "•", "·", "#", ">", "}", "】"}:
        return False
    if len(stripped) > 30:
        return False
    if stripped.endswith((".", "!", "?")):
        return False
    # Normalise the candidate to a colon-free lowercase phrase.
    cand = stripped.rstrip(":").strip().lower()
    if not cand:
        return False
    # Must be 1-4 words (guards out sentences that happen to be short).
    words = cand.split()
    if not (1 <= len(words) <= 4):
        return False
    # Build a set of all known heading phrases (deduped, lowercased) plus
    # a few common variants, and require an exact phrase match.
    if cand in _HEADING_PHRASES:
        return True
    return False


def _split_sections(text: str) -> Dict[str, Any]:
    """Best-effort split using Markdown headings + a few keyword heuristics."""
    sections: Dict[str, List[str]] = {}
    current = None
    lines = text.splitlines()
    for raw in lines:
        line = raw.rstrip()
        if not line:
            continue
        stripped = line.strip()
        # Detect a heading line: Markdown (#...), ALL-CAPS, or a short
        # standalone Title-case line (e.g. "Experience", "Skills") that
        # exactly matches a known heading. This is what most real CVs
        # (PDF text dumps, DOCX, FlowCV outputs) actually produce.
        m: Optional[str] = None
        if stripped.startswith("#"):
            # Markdown heading
            m = _normalize_heading(stripped.lstrip("#").strip())
        elif _is_heading_line(stripped):
            m = _normalize_heading(stripped)
        if m:
            current = m
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(raw.rstrip())
    # Personal fields: pass only the **header** block (everything before the
    # first recognised heading), not the entire flattened CV.  Contact
    # details (name / tagline / email / phone / location / site) always live
    # above the first section heading in real-world CVs; passing the whole
    # document is what caused `name` to absorb the entire body on FlowCV
    # resumes (the 2880-char "name" bug).
    header_block: List[str] = []
    for line in text.splitlines():
        s = line.strip()
        if _is_heading_line(s) or s.startswith("#"):
            break  # reached the first section heading — everything above was header
        header_block.append(line)
    header_text = "\n".join(header_block)
    name, email, phone, linkedin, website = _extract_personal(header_text)
    return {
        "name": name,
        "email": email,
        "phone": phone,
        "linkedin": linkedin,
        "website": website,
        "summary": "\n".join(sections.get("summary", [])).strip(),
        "_experience_raw": "\n".join(sections.get("experience", [])).strip(),
        "_skills_raw": "\n".join(sections.get("skills", [])).strip(),
        "_education_raw": "\n".join(sections.get("education", [])).strip(),
        "_projects_raw": "\n".join(sections.get("projects", [])).strip(),
        "_certifications_raw": "\n".join(sections.get("certifications", [])).strip(),
        "_languages_raw": "\n".join(sections.get("languages", [])).strip(),
        "_achievements_raw": "\n".join(sections.get("achievement", [])).strip(),
    }


def _extract_personal(text: str) -> Tuple[str, Optional[str], Optional[str], Optional[str], Optional[str]]:
    name = None
    email = None
    phone = None
    linkedin = None
    website = None

    email_m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
    if email_m:
        email = email_m.group(0)

    # Phone: (000) 000-000 000 / 000-000-0000 / +00 000 000 000
    phone_m = re.search(
        r"(\+?\d[\d\s\-]{7,15}\d)",
        text,
    )
    if phone_m:
        phone = phone_m.group(0)

    linkedin_m = re.search(
        r"(?:linkedin\.com(?:/in|pub)/[\w\-]+)|(?:hnhdn\.in|linkedin\.com/in)/[\w\-]+",
        text,
    )
    if linkedin_m:
        linkedin = linkedin_m.group(0)

    website_m = re.search(
        r"https?://(?!www\.linkedin)[a-z0-9\-]+\.[a-z]{2,}(/\S*)?",
        text,
        re.I,
    )
    if website_m:
        website = website_m.group(0)

    # Name: *first clean line* in the header block.  A clean name line is
    #  - 2-5 words,
    #  - letters + hyphens + apostrophes only (no `|`, `&`, `@`, `+`, digits,
    #    or slaszes),
    #  - not an ALL-CAPS heading (guarded by the caller passing only the
    #    header, but the char-class filter already rejects "SUMMARY").
    # The "Product-Minded Software Engineering Student | Python, SQL & Data
    # Products" *tagline* is **not** a name — it contains `|` and `&`, so the
    # char-class excludes it.  This is what was causing `name` to grab the
    # tagline line in FlowCV exports.
    name = ""
    for ln in (text.splitlines() or [""]):
        s = ln.strip()
        if not s:
            continue
        if any(ch in s for ch in "@+/*|&"):   # email/phone/url/tagline/separators
            continue
        if any(ch in s for ch in "0123456789"):  # dates, age, year
            continue
        # Only letters, spaces, hyphens, apostrophes, middot allowed.
        if not re.fullmatch(r"[A-Za-z\-\'\s·.]+", s):
            continue
        words = [w for w in s.split() if w]
        if not (2 <= len(words) <= 5):   # 1-word = too generic; 6+ = probably a sentence
            continue
        name = s
        break

    if not name:
        # Last resort: literal "Name: ..." line if present
        name_line = re.search(r"^\s*Name\s*:\s*([^\n]+)", text, re.I | re.M)
        name = name_line.group(1).strip() if name_line else ""

    return name, email, phone, linkedin, website


def _confidence_for(fields: Dict[str, Any]) -> Dict[str, str]:
    out = {}
    items = (
        ("name", fields.get("name")),
        ("email", fields.get("email")),
        ("phone", fields.get("phone")),
    )
    for name, val in items:
        out[name] = "high" if val else "missing"
    return out

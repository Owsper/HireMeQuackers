"""CV extraction: PDF via PyMuPDF (optional), DOCX via python-docx.

Returns plain structured dict for later upsert into `CandidateProfile`.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
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


#: Below this many non-space characters a PDF counts as "no text layer"
#: and local OCR is attempted (when available).
OCR_MIN_CHARS = 80


def parse_cv(path: str | Path, *, extract_sections: bool = True) -> Dict[str, Any]:
    """Extract a CV locally.  Nothing is sent to any external service.

    Result keys: ``cv_path``, ``raw_text``, ``fields``, ``confidence``
    (per field: high / medium / low / missing), ``method`` ("text",
    "docx", "ocr" or "fallback") and ``notes`` (human-readable caveats).
    """
    p = Path(path)
    text, method, notes = _read_raw_ex(p)
    fields = _split_sections(text) if extract_sections else {}
    confidence = dict(fields.get("_confidence", {}))
    if method in ("ocr", "fallback"):
        # OCR / crude-fallback text is never "high" confidence.
        confidence = {k: ("medium" if v == "high" else v) for k, v in confidence.items()}
    return {
        "cv_path": str(p),
        "raw_text": text,
        "fields": fields,
        "confidence": confidence,
        "method": method,
        "notes": notes,
    }


def _read_raw(p: Path) -> str:
    return _read_raw_ex(p)[0]


def _read_raw_ex(p: Path) -> Tuple[str, str, List[str]]:
    """(text, method, notes)."""
    notes: List[str] = []
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
                method = "text"
                if len(re.sub(r"\s", "", text)) < OCR_MIN_CHARS:
                    ocr_text, note = _ocr_pdf(doc)
                    if note:
                        notes.append(note)
                    if len(re.sub(r"\s", "", ocr_text)) > len(re.sub(r"\s", "", text)):
                        text, method = ocr_text, "ocr"
                        notes.append("Text was read with OCR from page images; "
                                     "check every field before saving.")
            finally:
                doc.close()
            logger.info("PDF parsed via PyMuPDF (%s): %d chars (%s)", method, len(text), p.name)
            return text, method, notes
        except Exception:
            # Parse-time failure (not a missing library): encrypted or
            # corrupt PDF. Fall back to the crude regex extractor with a
            # loud warning so the user knows it's low-quality.
            logger.warning(
                "PyMuPDF open/read failed for %s; using low-quality fallback",
                p.name,
            )
            data = p.read_bytes()
            notes.append("PDF could not be opened normally; low-quality fallback used.")
            return _pdf_fallback_extract(data), "fallback", notes
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
            return "\n".join(parts), "docx", notes
        except Exception as exc:
            raise RuntimeError(f"Failed to parse DOCX: {exc}") from exc
    raise ValueError(f"Unsupported CV format: {p.name}")


def _ocr_pdf(doc) -> Tuple[str, str]:
    """OCR every page locally with Tesseract (optional).

    Returns (text, note).  Needs the ``pytesseract`` package and the
    Tesseract program; when either is missing the note says so and the
    text is empty.  Page images go to a private temp dir that is deleted
    afterwards; nothing leaves the machine.
    """
    try:
        import pytesseract  # type: ignore
    except ImportError:
        return "", ("No text layer found and OCR is not installed "
                    "(optional: `pip install pytesseract` + the Tesseract program).")
    lang = os.environ.get("OCR_LANG", "eng+deu")
    parts: List[str] = []
    try:
        with tempfile.TemporaryDirectory(prefix="quackers-ocr-") as tmp:
            for i in range(doc.page_count):
                img = os.path.join(tmp, f"page{i}.png")
                doc[i].get_pixmap(dpi=300).save(img)
                parts.append(pytesseract.image_to_string(img, lang=lang) or "")
    except Exception as exc:  # TesseractNotFoundError, missing language pack, ...
        return "", f"OCR failed: {type(exc).__name__}: {str(exc)[:120]}"
    return "\n".join(parts).strip(), ""


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


# Section headings -> canonical section key.  Multilingual (EN / DE / FR /
# ES / NL / IT / RU) because CVs for European jobs are often not English.
# Matching is exact on the whole (lower-cased, colon-free) heading line, so
# sentences such as "I have experience with Python" are never headings.
SECTION_ALIASES: Dict[str, str] = {}
_ALIAS_SOURCE: Dict[str, Tuple[str, ...]] = {
    "summary": (
        "summary", "professional summary", "personal statement", "profile",
        "professional profile", "about me", "about", "career objective", "objective",
        "profil", "kurzprofil", "über mich", "zusammenfassung", "persönliches profil",
        "berufsprofil", "à propos", "profil professionnel", "perfil",
        "sobre mí", "resumen", "perfil profesional", "profiel", "over mij", "profilo",
        "о себе", "профиль", "цель",
    ),
    "experience": (
        "experience", "work experience", "professional experience", "employment history",
        "work history", "employment", "work", "internships", "relevant experience",
        "berufserfahrung", "erfahrung", "praktische erfahrung", "arbeitserfahrung",
        "beruflicher werdegang", "werdegang", "praktika", "berufliche erfahrung",
        "expérience", "expérience professionnelle", "expériences professionnelles",
        "experiencia", "experiencia laboral", "experiencia profesional", "werkervaring",
        "ervaring", "esperienza", "esperienze lavorative", "esperienza professionale",
        "опыт работы", "опыт", "профессиональный опыт", "стажировки",
    ),
    "skills": (
        "skills", "technical skills", "core skills", "technical", "skills & tools",
        "tools & skills", "key skills", "competencies", "tech stack", "it skills",
        "kenntnisse", "fähigkeiten", "fachkenntnisse", "it-kenntnisse", "kompetenzen",
        "fähigkeiten & kenntnisse", "kenntnisse und fähigkeiten", "edv-kenntnisse",
        "compétences", "compétences techniques", "habilidades", "competencias",
        "conocimientos", "vaardigheden", "competenze", "навыки", "ключевые навыки",
        "технические навыки", "компетенции",
    ),
    "projects": (
        "projects", "personal projects", "selected projects", "side projects",
        "projekte", "projekterfahrung", "projets", "proyectos", "projecten",
        "progetti", "проекты",
    ),
    "certifications": (
        "certifications", "certificates", "certification", "licenses & certifications",
        "zertifikate", "zertifizierungen", "weiterbildung", "weiterbildungen",
        "certificats", "certificaciones", "certificados", "certificaten",
        "certificazioni", "сертификаты", "курсы",
    ),
    "education": (
        "education", "education history", "academics", "academic", "academic background",
        "ausbildung", "bildung", "bildungsweg", "studium", "schulbildung",
        "akademischer werdegang", "formation", "formations", "éducation", "educación",
        "formación", "formación académica", "opleiding", "opleidingen", "istruzione",
        "formazione", "образование",
    ),
    "achievements": (
        "achievements", "achievement", "awards", "honors", "honours", "awards & honors",
        "auszeichnungen", "erfolge", "distinctions", "prix", "logros", "premios",
        "prestaties", "riconoscimenti", "достижения", "награды",
    ),
    "languages": (
        "languages", "language skills", "sprachen", "sprachkenntnisse", "langues",
        "idiomas", "talen", "lingue", "языки", "знание языков",
    ),
    "interests": (
        "interests", "hobbies", "interessen", "hobbys", "freizeit", "centres d'intérêt",
        "loisirs", "intereses", "aficiones", "hobby's", "interessi", "интересы", "хобби",
    ),
    "personal": (
        "personal details", "personal information", "personal data", "contact",
        "contact details", "persönliche daten", "persönliche angaben", "kontakt",
        "kontaktdaten", "informations personnelles", "coordonnées", "datos personales",
        "contacto", "persoonlijke gegevens", "dati personali", "контакты",
        "личные данные", "контактная информация",
    ),
}
for _key, _phrases in _ALIAS_SOURCE.items():
    for _ph in _phrases:
        SECTION_ALIASES.setdefault(_ph, _key)

#: Lines that title the whole document — never a person's name.
_DOC_TITLES = frozenset({
    "curriculum vitae", "lebenslauf", "resume", "résumé", "cv", "tabellarischer lebenslauf",
    "резюме", "currículum", "curriculum",
})

_BULLET_CHARS = "-*•·▪◦‣–—>"
_DATE_RE = re.compile(
    r"(?:(?:0?[1-9]|1[0-2])[./](?:19|20)\d{2}|(?:19|20)\d{2}"
    r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|mär|mai|okt|dez|janv|févr|"
    r"avr|juil|déc|ene|abr|ago|dic)\w*\.?\s+(?:19|20)\d{2})",
    re.I,
)
_DATE_RANGE_RE = re.compile(
    _DATE_RE.pattern + r"\s*(?:-|–|—|to|bis|à|до|until)\s*(?:" + _DATE_RE.pattern
    + r"|present|current|now|today|heute|aktuell|jetzt|bis heute|aujourd'hui|actualidad|"
    r"presente|heden|oggi|настоящее время|н\.\s*в\.)",
    re.I,
)


def _normalize_heading(h: str) -> Optional[str]:
    """Section key for a heading text, or None."""
    hh = re.sub(r"[:：]\s*$", "", (h or "").strip().lower()).strip()
    hh = re.sub(r"\s+", " ", hh)
    return SECTION_ALIASES.get(hh) if hh else None


def _is_heading_line(stripped: str) -> bool:
    """True if ``stripped`` is a standalone section heading (any case,
    optional trailing colon, at most 5 words, exact alias match)."""
    if not stripped or stripped[0] in _BULLET_CHARS + "#}】":
        return False
    if len(stripped) > 40 or stripped.endswith((".", "!", "?")):
        return False
    cand = stripped.rstrip(":：").strip()
    if not (1 <= len(cand.split()) <= 5):
        return False
    return _normalize_heading(cand) is not None


def _inline_heading(stripped: str) -> Optional[Tuple[str, str]]:
    """"Skills: Python, SQL" -> ("skills", "Python, SQL")."""
    m = re.match(r"^([^:：|]{2,40})\s*[:：|]\s*(.+)$", stripped)
    if not m:
        return None
    key = _normalize_heading(m.group(1))
    if key is None or key == "personal":
        return None
    return key, m.group(2).strip()


def _split_sections(text: str) -> Dict[str, Any]:
    """Split CV text into sections by (multilingual) headings and extract
    contact details.  Nothing is guessed: unknown content stays empty."""
    sections: Dict[str, List[str]] = {}
    headings_found: List[str] = []
    current: Optional[str] = None
    header_block: List[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        key: Optional[str] = None
        rest = ""
        if stripped.startswith("#"):
            key = _normalize_heading(stripped.lstrip("#").strip())
        elif _is_heading_line(stripped):
            key = _normalize_heading(stripped)
        else:
            inline = _inline_heading(stripped)
            if inline:
                key, rest = inline
        if key:
            current = key
            if key not in headings_found:
                headings_found.append(key)
            sections.setdefault(current, [])
            if rest:
                sections[current].append(rest)
            continue
        if current is None:
            header_block.append(stripped)
        else:
            sections[current].append(stripped)

    # Contact details live above the first heading (or in a "Personal
    # details" section).  Email / phone / URLs are unambiguous patterns, so
    # they may also come from elsewhere; the *name* only from the header.
    header_text = "\n".join(header_block + sections.get("personal", []))
    name, email, phone, linkedin, website, name_source = _extract_personal(header_text)
    if not (email and phone and linkedin and website):
        _, e2, p2, l2, w2, _ = _extract_personal(text)
        email, phone, linkedin, website = email or e2, phone or p2, linkedin or l2, website or w2

    def joined(key: str) -> str:
        return "\n".join(sections.get(key, [])).strip()

    fields: Dict[str, Any] = {
        "name": name,
        "email": email,
        "phone": phone,
        "linkedin": linkedin,
        "website": website,
        "summary": joined("summary"),
        "_experience_raw": joined("experience"),
        "_skills_raw": joined("skills"),
        "_education_raw": joined("education"),
        "_projects_raw": joined("projects"),
        "_certifications_raw": joined("certifications"),
        "_languages_raw": joined("languages"),
        "_achievements_raw": joined("achievements"),
        "experience_entries": _group_entries(sections.get("experience", [])),
        "education_entries": _group_entries(sections.get("education", [])),
        "sections_found": headings_found,
    }
    fields["_confidence"] = _confidence_for(fields, name_source=name_source)
    return fields


def _is_bullet(line: str) -> bool:
    return bool(line) and line[0] in _BULLET_CHARS


def _group_entries(lines: List[str]) -> List[str]:
    """Group a section's lines into entries ("Title - Company (dates) |
    detail | detail").  A new entry starts at a non-bullet line once the
    current entry already has a date range plus details, or bullet
    details.  Text is only re-joined, never rewritten."""
    entries: List[List[str]] = []
    for line in lines:
        if not entries:
            entries.append([line])
            continue
        cur = entries[-1]
        has_dates = any(_DATE_RANGE_RE.search(x) for x in cur)
        has_bullets = any(_is_bullet(x) for x in cur[1:])
        dated = _DATE_RANGE_RE.search(line) is not None
        starts_new = not _is_bullet(line) and (
            has_bullets
            or (has_dates and dated)
            or (has_dates and len(cur) >= 2 and len(line) < 80
                and not line.endswith((".", ",")))
        )
        if starts_new:
            entries.append([line])
        else:
            cur.append(line)
    out = []
    for e in entries:
        parts = [e[0]] + [x.lstrip(_BULLET_CHARS + " ").strip() for x in e[1:]]
        out.append(" | ".join(p for p in parts if p))
    return out


_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}", re.I)
_PHONE_RE = re.compile(r"(?<![\w/])(\+?\(?\d[\d\s()./-]{6,18}\d)(?![\w/])")
_LINKEDIN_RE = re.compile(r"(?:https?://)?(?:[a-z]{2,3}\.)?linkedin\.com/(?:in|pub)/[\w%\-]+/?", re.I)
_WEBSITE_RE = re.compile(
    r"(?:https?://|www\.)[^\s|,;()<>]+|(?<![\w@.])(?:github\.com|gitlab\.com)/[\w\-]+", re.I
)
_YEAR_SPAN_RE = re.compile(r"(?:19|20)\d{2}\s*[-–/]\s*(?:(?:0?[1-9]|1[0-2])[./])?(?:19|20)\d{2}")


def _extract_personal(text: str) -> Tuple[str, Optional[str], Optional[str], Optional[str], Optional[str], str]:
    """(name, email, phone, linkedin, website, name_source).

    ``name_source`` is "label" for an explicit "Name: ..." line, "header"
    for the first clean line of the header, "" when not found.
    """
    email = phone = linkedin = website = None

    m = _EMAIL_RE.search(text)
    if m:
        email = m.group(0)
    for m in _PHONE_RE.finditer(text):
        cand = m.group(1).strip()
        digits = re.sub(r"\D", "", cand)
        if not 7 <= len(digits) <= 15:
            continue
        if not cand.startswith("+") and (_DATE_RANGE_RE.search(cand) or _YEAR_SPAN_RE.search(cand)):
            continue  # "2019 - 2023", "06/2025-09/2025" are dates, not phones
        phone = cand
        break
    m = _LINKEDIN_RE.search(text)
    if m:
        linkedin = m.group(0).rstrip("/")
    for m in _WEBSITE_RE.finditer(text):
        url = m.group(0).rstrip(".")
        if "linkedin.com" not in url.lower() and "@" not in url:
            website = url
            break

    labelled = re.search(
        r"^\s*(?:name|vor-?\s*und\s*nachname|nom|nombre|naam|nome|имя|фио)\s*[:：]\s*([^\n]+)$",
        text, re.I | re.M)
    if labelled and _looks_like_name(labelled.group(1).strip()):
        return labelled.group(1).strip(), email, phone, linkedin, website, "label"

    for ln in text.splitlines():
        s = ln.strip()
        if _looks_like_name(s):
            return s, email, phone, linkedin, website, "header"
    return "", email, phone, linkedin, website, ""


_NAME_PARTICLES = frozenset({"van", "von", "de", "der", "den", "da", "di", "del", "la", "le",
                             "zu", "bin", "al", "dos", "du"})


def _looks_like_name(s: str) -> bool:
    """2-5 capitalised words of letters (any script), hyphens, apostrophes
    or dots; not a heading, document title or tagline."""
    if not s or s.lower() in _DOC_TITLES or _normalize_heading(s):
        return False
    if any(ch in s for ch in "@+/*|&,:") or any(ch.isdigit() for ch in s):
        return False
    if not all(ch.isalpha() or ch in " -'’·." for ch in s):
        return False
    words = s.split()
    if not (2 <= len(words) <= 5):
        return False
    return all(w[0].isupper() or w.lower() in _NAME_PARTICLES for w in words)


def _confidence_for(fields: Dict[str, Any], *, name_source: str = "") -> Dict[str, str]:
    """Per-field extraction confidence: high / medium / low / missing.

    * pattern-matched contact fields (email, phone, URLs): high
    * name: high from a "Name:" label, medium from the first header line
    * sections: high under a recognised heading, low when the text looks
      garbled
    """
    from services.cv_validation import looks_garbled

    out: Dict[str, str] = {}
    if not fields.get("name"):
        out["name"] = "missing"
    else:
        out["name"] = "high" if name_source == "label" else "medium"
    for key in ("email", "phone", "linkedin", "website"):
        out[key] = "high" if fields.get(key) else "missing"
    for key, raw_key in (("summary", "summary"), ("experience", "_experience_raw"),
                         ("skills", "_skills_raw"), ("education", "_education_raw"),
                         ("projects", "_projects_raw"), ("certifications", "_certifications_raw"),
                         ("languages", "_languages_raw"), ("achievements", "_achievements_raw")):
        val = fields.get(raw_key) or ""
        if not val.strip():
            out[key] = "missing"
        elif looks_garbled(val):
            out[key] = "low"
        else:
            out[key] = "high"
    return out

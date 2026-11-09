"""PDF/DOCX exporters for tailoring suggestions + cover letter + job details."""
from __future__ import annotations

import hashlib
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.settings import PROJECT_ROOT
from database.models import GeneratedDocument
from database import repository as db
from services.sanitization import sanitize_filename
from services.usage_tracking import record_usage


def _content_hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:64]


TEMPLATE_DIR = PROJECT_ROOT / "documents" / "templates"


def _new_doc_record(
    *,
    doc_kind: str,
    doc_hash: str,
    source_profile_version: int,
    job_id: int,
    path: Path,
    meta: Optional[Dict[str, Any]] = None,
    session=None,
) -> GeneratedDocument:
    """Insert a `GeneratedDocument` row, then commit if we opened the session."""
    opened = False
    if session is None:
        from database.connection import get_engine, get_session

        session = get_session(get_engine())
        opened = True
    try:
        doc = db.create_generated_document(
            session=session,
            doc_type=doc_kind,
            profile_version=source_profile_version,
            job_id=job_id,
            file_path=str(path),
            notes=__import__("json").dumps({"content_hash": doc_hash, **(meta or {})}),
        )
        if opened:
            session.commit()
        return doc
    finally:
        if opened:
            session.close()


def export_cv_suggestions(
    profile_id: int,
    job_id: int,
    suggestions: List[Dict[str, str]],
    *,
    source_profile_version: int,
    path_override: Optional[str] = None,
    session=None,
) -> GeneratedDocument:
    if not path_override:
        rel = f"cv_suggestions_{job_id}.txt"
    else:
        rel = sanitize_filename(path_override, f"cv_suggestions_{job_id}.txt") or f"cv_suggestions_{job_id}.txt"
    output_dir = PROJECT_ROOT / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / rel
    content_chars = _suggestions_to_text(profile_id, job_id, suggestions)
    path.write_text(content_chars, encoding="utf-8")
    h = _content_hash(content_chars)
    return _new_doc_record(
        doc_kind="cv_suggestions",
        doc_hash=h,
        source_profile_version=source_profile_version,
        job_id=job_id,
        path=path,
        meta={"profile_id": profile_id, "suggestion_count": len(suggestions)},
        session=session,
    )


def _suggestions_to_text(profile_id, job_id, suggestions) -> str:
    lines = [
        f"CV / LinkedIn Tailoring — Candidate ID {profile_id} × Job ID {job_id}",
        "=" * 60,
        "",
    ]
    for s in suggestions:
        lines.append(f"## {s.get('field', 'general')}")
        lines.append(f"- Action: {s.get('action', '')}")
        lines.append(f"- Reason: {s.get('reason', '')}")
        lines.append("")
    return "\n".join(lines)


def export_cover_letter(
    profile_id: int,
    job_id: int,
    cover_letter: str,
    *,
    source_profile_version: int,
    path_override: Optional[str] = None,
    session=None,
) -> GeneratedDocument:
    if not path_override:
        rel = f"cover_letter_{job_id}.md"
    else:
        rel = sanitize_filename(path_override, "cover_letter.md") or f"cover_letter_{job_id}.md"
    output_dir = PROJECT_ROOT / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / rel
    path.write_text(cover_letter, encoding="utf-8")
    h = _content_hash(cover_letter)
    return _new_doc_record(
        doc_kind="cover_letter",
        doc_hash=h,
        source_profile_version=source_profile_version,
        job_id=job_id,
        path=path,
        meta={"profile_id": profile_id},
        session=session,
    )


def export_job_details(
    job_id: int,
    job,
    match: Optional[Dict[str, Any]] = None,
    *,
    path_override: Optional[str] = None,
    session=None,
) -> GeneratedDocument:
    """Markdown-variant for quick copy; since JobMatch.csv export is also supported,
    we produce a .md detail report."""
    rel = (
        sanitize_filename(path_override, f"job_{job_id}.md")
        if path_override
        else f"job_details_{job_id}.md"
    )
    output_dir = PROJECT_ROOT / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / rel
    lines = [
        f"# {job.title}",
        "",
        f"**Company:** {job.company}",
        f"**Location:** {job.location or 'n/a'} ({job.country or 'n/a'})",
        f"**Employment Type:** {job.employment_type}",
        f"**Workplace:** {job.workplace_arrangement}",
        f"**Published:** {job.published_at}",
        f"**Source:** {job.source}",
        f"**Original URL:** {job.original_url}",
        f"**Application URL:** {job.application_url}",
        "",
        "## Description",
        (job.description or "not available"),
        "",
        "## Required Skills",
        ", ".join(job.required_skills or []) or "n/a",
        "",
        "## Preferred Skills",
        ", ".join(job.preferred_skills or []) or "n/a",
        "",
        "## Language Requirements",
        job.language_requirements or "n/a",
        "",
        "## Education Requirements",
        job.education_requirements or "n/a",
        "",
        "## Experience Requirements",
        job.experience_requirements or "n/a",
    ]
    if match:
        lines += [
            "",
            f"## Fit (score {match.get('total_score', 'n/a')}) — confidence: {match.get('confidence', 'n/a')}",
            f"- Matched skills: {', '.join(match.get('must_have_skills_matched', []) or [])}",
            f"- Missing skills: {', '.join(match.get('must_have_skills_missing', []) or [])}",
            f"- Experience fit: {match.get('experience_fit', 'n/a')}",
            f"- Education fit: {match.get('education_fit', 'n/a')}",
            f"- Language fit: {match.get('language_fit', 'n/a')}",
            f"- Location fit: {match.get('location_fit', 'n/a')}",
            "### Evidence",
        ]
        for e in match.get("evidence", []) or []:
            lines.append(f"- {e}")
    content = "\n".join(lines)
    path.write_text(content, encoding="utf-8")
    h = _content_hash(content)
    return _new_doc_record(
        doc_kind="job_details",
        doc_hash=h,
        source_profile_version=0,
        job_id=job.id,
        path=path,
        meta={"profile_id": None},
        session=session,
    )


def _content_hash(s: str) -> str:
    import hashlib

    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:64]


def export_cv_text(text: str, dest: Path) -> None:
    """Single-column ATS output with conventional headings and Unicode support."""
    from xml.sax.saxutils import escape
    suffix = dest.suffix.lower()
    if suffix in {".txt", ".md"}:
        dest.write_text(text, encoding="utf-8")
    elif suffix == ".docx":
        from docx import Document
        from docx.shared import Inches, Pt, RGBColor
        document = Document()
        for section in document.sections:
            section.top_margin = section.bottom_margin = Inches(.65)
            section.page_width, section.page_height = Inches(8.5), Inches(11)
        document.styles["Normal"].font.name = "Arial"
        document.styles["Normal"].font.size = Pt(11)
        for name in ("Title", "Heading 1", "Normal"):
            document.styles[name].font.color.rgb = RGBColor(0, 0, 0)
        for index, line in enumerate(text.splitlines()):
            if index == 0:
                document.add_paragraph(line, style="Title")
            elif line.startswith("## "):
                document.add_heading(line[3:], 1)
            elif line.startswith("- "):
                document.add_paragraph(line[2:], style="List Bullet")
            elif line:
                document.add_paragraph(line)
        document.save(str(dest))
    elif suffix == ".pdf":
        import os
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        font_path = next((p for p in (os.getenv("QUACKERS_PDF_FONT", ""), "C:/Windows/Fonts/arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf") if p and Path(p).is_file()), None)
        if font_path:
            pdfmetrics.registerFont(TTFont("QuackersUnicode", font_path))
        elif any(ord(ch) > 255 for ch in text):
            raise ValueError("Set QUACKERS_PDF_FONT to a Unicode TTF font for this CV")
        styles = getSampleStyleSheet()
        for style in styles.byName.values():
            style.fontName = "QuackersUnicode" if font_path else "Helvetica"
        story = []
        for index, line in enumerate(text.splitlines()):
            if not line:
                story.append(Spacer(1, 5))
            else:
                style = styles["Title"] if index == 0 else styles["Heading2"] if line.startswith("## ") else styles["BodyText"]
                content = line[3:] if line.startswith("## ") else line
                story.append(Paragraph(escape(content), style))
        SimpleDocTemplate(str(dest), leftMargin=45, rightMargin=45, topMargin=40, bottomMargin=40).build(story)
    else:
        raise ValueError("CV export requires .pdf, .docx, .txt or .md")

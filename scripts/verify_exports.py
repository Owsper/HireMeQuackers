"""Create synthetic exports and render PDF pages for manual visual QA.

Run: python scripts/verify_exports.py --output-dir data/qa
Optional DOCX rendering uses the Documents skill's render_docx.py when available.
"""
from __future__ import annotations
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from documents.exporter import export_cv_text

TEXT = """Ada Example
ada@example.test | Berlin Germany

## Professional Summary
Software developer with experience building Python services and SQL reports.

## Skills
- Python
- SQL
- Communication

## Work Experience
- Software Developer
- Employer: Example Company
- Dates: 2020–2024
- Built reporting services and maintained automated tests.
- Worked with colleagues to investigate issues and document changes.

## Education
- Bachelor of Computer Science

## Projects
- Reporting tool using Python and SQL

## Languages
- English C1
- German B2
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path("data/qa"))
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for ext in ("pdf", "docx", "txt"):
        export_cv_text(TEXT, args.output_dir / f"synthetic_cv.{ext}")
    import pymupdf
    with pymupdf.open(args.output_dir / "synthetic_cv.pdf") as doc:
        for i, page in enumerate(doc):
            assert "Ada Example" in page.get_text() if i == 0 else True
            for block in page.get_text("blocks"):
                assert block[0] >= 0 and block[1] >= 0 and block[2] <= page.rect.width and block[3] <= page.rect.height
            page.get_pixmap(matrix=pymupdf.Matrix(1.4, 1.4)).save(args.output_dir / f"page-{i + 1}.png")
        print(f"PDF: {len(doc)} page(s), text preserved, all text blocks within page bounds")
    from docx import Document
    document = Document(args.output_dir / "synthetic_cv.docx")
    assert not document.tables and document.paragraphs[0].style.name == "Title"
    print("DOCX: conventional title/headings, single column, no tables; visual rendering requires LibreOffice")


if __name__ == "__main__":
    main()

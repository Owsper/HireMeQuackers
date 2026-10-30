"""CV parser: multilingual headings, entries, contact fields, confidence, OCR."""
from __future__ import annotations

import sys
import types

import pytest

from services import cv_parser
from services.cv_parser import _split_sections

DE_CV = """Lebenslauf
Jürgen Müller-Lüdenscheidt
juergen@example.de | +49 151 2345678 | linkedin.com/in/jmueller | github.com/jmueller
Berufserfahrung
Werkstudent Data Engineering - Acme GmbH, Berlin
10/2024 - heute
- ETL-Pipelines in Python gebaut
- Dashboards mit SQL
Praktikum Softwareentwicklung - Foo AG
03/2024 - 09/2024
- REST-API in FastAPI
Ausbildung
B.Sc. Informatik - TU Berlin
2022 - 2025
Kenntnisse: Python, SQL, Docker
Sprachen
Deutsch (Muttersprache), Englisch (C1)
"""


def test_german_cv_sections_entries_and_contact():
    f = _split_sections(DE_CV)
    assert f["name"] == "Jürgen Müller-Lüdenscheidt"  # not "Lebenslauf", umlauts kept
    assert f["email"] == "juergen@example.de" and f["phone"] == "+49 151 2345678"
    assert f["linkedin"] == "linkedin.com/in/jmueller" and f["website"] == "github.com/jmueller"
    assert f["_skills_raw"] == "Python, SQL, Docker"  # inline "Kenntnisse: ..." heading
    assert f["experience_entries"] == [
        "Werkstudent Data Engineering - Acme GmbH, Berlin | 10/2024 - heute | "
        "ETL-Pipelines in Python gebaut | Dashboards mit SQL",
        "Praktikum Softwareentwicklung - Foo AG | 03/2024 - 09/2024 | REST-API in FastAPI",
    ]
    assert f["education_entries"] == ["B.Sc. Informatik - TU Berlin | 2022 - 2025"]
    assert f["sections_found"] == ["experience", "education", "skills", "languages"]
    assert f["_confidence"]["name"] == "medium" and f["_confidence"]["email"] == "high"
    assert f["_confidence"]["projects"] == "missing"


@pytest.mark.parametrize("heading,key", [
    ("WORK EXPERIENCE", "_experience_raw"),        # used to land in an unread key
    ("Technical Skills:", "_skills_raw"),
    ("Achievements", "_achievements_raw"),          # used to be dropped ("achievement")
    ("Expérience professionnelle", "_experience_raw"),
    ("Опыт работы", "_experience_raw"),
    ("Formación", "_education_raw"),
])
def test_heading_aliases_reach_their_section(heading, key):
    f = _split_sections(f"Ada Lovelace\n{heading}\nSomething real here")
    assert f[key] == "Something real here"


def test_sentences_are_not_headings_and_nothing_is_invented():
    f = _split_sections("Ada Lovelace\nI have experience with Python and SQL.\n")
    assert f["_experience_raw"] == "" and f["experience_entries"] == []
    assert f["summary"] == "" and f["email"] is None and f["phone"] is None


def test_name_label_and_rejections():
    f = _split_sections("Curriculum Vitae\nPersönliche Daten\nName: Max Mustermann\n"
                        "E-Mail: max@example.de\nBerufserfahrung\nIntern - X")
    assert f["name"] == "Max Mustermann" and f["_confidence"]["name"] == "high"
    assert _split_sections("SOFTWARE ENGINEER | PYTHON & SQL\nSkills\nPython")["name"] == ""
    ru = _split_sections("Иван Петров\nivan@example.ru\n+7 912 345-67-89\nНавыки\nPython")
    assert ru["name"] == "Иван Петров" and ru["phone"] == "+7 912 345-67-89"


def test_date_ranges_are_not_phone_numbers():
    f = _split_sections("Ada Lovelace\nExperience\nIntern - Acme (06/2025 - 09/2025)\n2019 - 2023")
    assert f["phone"] is None


def _blank_pdf(path):
    import pymupdf

    doc = pymupdf.open()
    doc.new_page()
    doc.save(str(path))
    return path


def test_scanned_pdf_without_ocr_says_so(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "pytesseract", None)  # import -> ImportError
    res = cv_parser.parse_cv(_blank_pdf(tmp_path / "scan.pdf"))
    assert res["method"] == "text" and res["raw_text"].strip() == ""
    assert any("OCR is not installed" in n for n in res["notes"])


def test_scanned_pdf_uses_local_ocr_when_available(tmp_path, monkeypatch):
    seen = {}

    def image_to_string(img_path, lang):
        seen["lang"], seen["png"] = lang, open(img_path, "rb").read(4)
        return ("Ada Lovelace\nada@example.com\nSkills\nPython, SQL\n"
                "Experience\nData Intern - Acme GmbH (06/2025 - 09/2025)\n")

    monkeypatch.setitem(sys.modules, "pytesseract",
                        types.SimpleNamespace(image_to_string=image_to_string))
    monkeypatch.setenv("OCR_LANG", "deu")
    res = cv_parser.parse_cv(_blank_pdf(tmp_path / "scan.pdf"))
    assert res["method"] == "ocr" and seen == {"lang": "deu", "png": b"\x89PNG"}
    assert res["fields"]["name"] == "Ada Lovelace"
    assert res["confidence"]["email"] == "medium"  # OCR is never "high"
    assert any("OCR" in n for n in res["notes"])


def test_ocr_failure_is_reported_not_raised(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("tesseract is not installed")

    monkeypatch.setitem(sys.modules, "pytesseract", types.SimpleNamespace(image_to_string=boom))
    res = cv_parser.parse_cv(_blank_pdf(tmp_path / "scan.pdf"))
    assert res["method"] == "text" and any("OCR failed" in n for n in res["notes"])


def test_cli_scan_shows_confidence_and_ocr_hint(tmp_path, monkeypatch, capsys):
    from quackers import cli

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 't.db').as_posix()}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setitem(sys.modules, "pytesseract", None)
    with pytest.raises(cli.CliError, match="OCR is not installed"):
        args = cli.build_parser().parse_args(["cv-scan", str(_blank_pdf(tmp_path / "s.pdf"))])
        args.func(args)

    import pymupdf

    doc = pymupdf.open()
    doc.new_page().insert_text((50, 60), DE_CV.replace("Jürgen Müller-Lüdenscheidt", "Ada Lovelace"))
    doc.save(str(tmp_path / "de.pdf"))
    args = cli.build_parser().parse_args(["cv-scan", str(tmp_path / "de.pdf")])
    args.func(args)
    out = capsys.readouterr().out
    assert "confidence" in out and "Ada Lovelace" in out and "Acme GmbH" in out

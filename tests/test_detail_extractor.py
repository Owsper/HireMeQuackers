"""Phase 3: JSON-LD + HTML detail-page extraction tests (no network)."""
from __future__ import annotations

import json

from services.detail_extractor import (
    MAX_DESCRIPTION_CHARS,
    ExtractedDetail,
    _classify_employment,
    _classify_workplace,
    _parse_salary_range,
    extract_from_html,
)


_JOBPOST_LD = {
    "@context": "https://schema.org",
    "@type": "JobPosting",
    "title": "Software Engineering Intern - Summer 2028",
    "description": "Build features in Python. <b>Bonus</b> if you know Docker.",
    "datePosted": "2026-06-01",
    "validThrough": "2026-08-31",
    "employmentType": "INTERN",
    "hiringOrganization": {"@type": "Organization", "name": "Acme Corp"},
    "jobLocation": {
        "@type": "Place",
        "address": {
            "@type": "PostalAddress",
            "addressLocality": "Berlin",
            "addressRegion": "BE",
            "streetAddress": "Alexanderplatz 1",
        },
    },
    "baseSalary": {
        "@type": "MonetaryAmount",
        "currency": "EUR",
        "value": {"@type": "QuantitativeValue", "minValue": 1600, "maxValue": 2000},
    },
    "directApply": "https://acme.test/careers/intern/apply",
}


def _page_with_ld() -> str:
    return (
        '<html><head>'
        f'<script type="application/ld+json">{json.dumps(_JOBPOST_LD)}</script>'
        "<title>t</title></head><body></body></html>"
    )


class TestJsonLd:
    def test_json_ld_jobposting_extraction(self):
        d = extract_from_html(_page_with_ld(), "https://acme.test/careers/intern")
        assert d.json_ld_present
        assert d.title == _JOBPOST_LD["title"]
        assert d.employer == "Acme Corp"
        assert d.location is not None and "Berlin" in d.location
        assert d.salary_min == 1600 and d.salary_max == 2000
        assert d.salary_currency == "EUR"
        assert d.employment_type == "internship"
        assert d.posted_date == "2026-06-01"
        assert d.closing_date == "2026-08-31"
        assert d.description_text and "<" not in d.description_text
        assert d.extraction_confidence >= 0.9

    def test_json_ld_missing_falls_back_to_html(self):
        html = (
            '<html><head><title>Junior Dev</title>'
            '<meta property="og:description" content="Very long description that goes on and on about the role for a junior developer.">'
            "</head><body><article>We are looking for a junior developer. Responsibilities include backend work.</article></body></html>"
        )
        d = extract_from_html(html, "https://acme.test/jr")
        assert d.json_ld_present is False
        assert d.title == "Junior Dev"
        assert d.description_text is not None
        assert d.extraction_confidence > 0


def test_html_description_article_fallback():
    html = (
        '<html><body><div class="job-descriptionContainer">'
        "Frontend engineer wanted. Knowledge of React required. "
        "You will work in an agile team shipping customer features."
        "</div></body></html>"
    )
    d = extract_from_html(html, "https://x.test/jobs/1")
    assert d.description_text and "React" in d.description_text


def test_html_h1_no_description_returns_zero_confidence():
    d = extract_from_html("<html><body><h1>Hi</h1></body></html>", "https://x.test/1")
    assert d.extraction_confidence == 0.0
    assert d.description_text is None


def test_salary_regex_european_range():
    html = (
        "<html><body><article>Yearly salary: 4.500 – 5.200 EUR gross. "
        "Please apply as a full-time intern in Berlin.</article></body></html>"
    )
    d = extract_from_html(html, "https://x.test/s")
    assert d.salary_min == 4500 and d.salary_max == 5200
    assert d.salary_currency == "EUR"


def test_employment_type_classification():
    assert _classify_employment("Software Engineering Internship Berlin") == "internship"
    assert _classify_employment("Werkstudent Backend") == "working_student" or _classify_employment(
        "Werkstudent Backend"
    ) == "working_student"
    assert _classify_employment("Freelance Consultant") == "freelance"
    assert _classify_employment("Ausbildung Informatik") == "apprenticeship"
    assert _classify_employment("nothing matching here at all") is None


def test_workplace_classification():
    assert _classify_workplace("Fully remote engineer") == "remote"
    assert _classify_workplace("Hybrid role in Munich") == "hybrid"
    assert _classify_workplace("On-site position") == "on_site"
    assert _classify_workplace("no arrangement described") is None


def test_application_url_same_domain_captured():
    html = (
        "<html><body><article>We need an engineer.</article>"
        '<a href="https://acme.test/apply/123">Apply Now</a>'
        "</body></html>"
    )
    d = extract_from_html(html, "https://acme.test/jobs/123")
    assert d.application_url == "https://acme.test/apply/123"


def test_application_url_cross_domain_ignored():
    html = (
        "<html><body><article>Some description text here that is long enough.</article>"
        '<a href="https://evil.test/phish">Apply here</a>'
        "</body></html>"
    )
    d = extract_from_html(html, "https://acme.test/jobs/1")
    assert d.application_url is None


def test_html_stripping_removes_tags():
    html = (
        "<html><body><article><p>Line one.</p><p>Line two.</p>"
        "<script>bad()</script></article></body></html>"
    )
    d = extract_from_html(html, "https://x.test/1")
    assert "<" not in (d.description_text or "")
    assert "bad()" not in (d.description_text or "")


def test_long_description_truncated():
    long_text = "Responsibility: " + "word " * 2000
    html = f"<html><body><article>{long_text}</article></body></html>"
    d = extract_from_html(html, "https://x.test/1")
    if d.description_text:
        assert len(d.description_text) <= MAX_DESCRIPTION_CHARS + 10


def test_og_title_used_when_no_ld():
    html = (
        '<html><head><meta property="og:title" content="Data Intern Berlin">'
        "</head><body><article>Job description here about the data internship of the team.</article></body></html>"
    )
    d = extract_from_html(html, "https://x.test/1")
    assert d.title == "Data Intern Berlin"


def test_date_time_tag_parsed():
    html = (
        '<html><body><time datetime="2026-05-10T08:00:00Z">Posted</time>'
        "<article>Closing date 2026-07-01. Full description for the opening.</article></body></html>"
    )
    d = extract_from_html(html, "https://x.test/1")
    assert d.posted_date == "2026-05-10"


def test_extracted_detail_defaults_empty():
    d = ExtractedDetail()
    assert d.extraction_confidence == 0.0
    assert d.title is None
    assert d.required_skills == []

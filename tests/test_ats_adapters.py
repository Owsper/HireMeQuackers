"""ATS adapters + ATS detection (all HTTP mocked)."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from config.search_config import EmploymentType, SearchConfig


def _resp(status=200, body=None, raw=None):
    content = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
    return MagicMock(status_code=status, content=content, error=None)


def _cfg(**kw):
    kw.setdefault("employment_types", list(EmploymentType))
    return SearchConfig(**kw)


def _run(source, routes):
    """routes: list of (url-substring, response) checked in order."""
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        for needle, resp in routes:
            if needle in url:
                return resp
        return _resp(404)

    with patch("sources.ats_base.safe_get", side_effect=fake_get), \
         patch("sources.ats_base.is_allowed_by_robots", return_value=True):
        return source.scan(_cfg()), calls


def test_ashby_parses_jobs_and_salary():
    from sources.ats_adapters import AshbySource
    body = {"organizationName": "Acme", "jobs": [
        {"id": "a1", "title": "Junior Python Developer", "location": "Berlin",
         "employmentType": "FullTime", "workplaceType": "Hybrid",
         "descriptionHtml": "<p>Python and SQL</p>", "jobUrl": "https://jobs.ashbyhq.com/acme/a1",
         "applyUrl": "https://jobs.ashbyhq.com/acme/a1/application",
         "compensation": {"compensationTierSummary": "€50K – €60K"}},
        {"id": "a2", "title": "Hidden", "isListed": False, "jobUrl": "https://x/a2"},
    ]}
    res, _ = _run(AshbySource(boards=["acme"]), [("job-board/acme", _resp(200, body))])
    assert res.success and len(res.listings) == 1
    j = res.listings[0]
    assert (j.company, j.location, j.workplace_arrangement) == ("Acme", "Berlin", "hybrid")
    assert j.salary == "€50K – €60K" and j.employment_type == EmploymentType.FULL_TIME
    assert j.application_url.endswith("/application")
    assert (res.attempted, res.fetched, res.parsed, res.failed) == (1, 1, 1, 0)


def test_smartrecruiters_paginates_and_fetches_detail():
    from sources.ats_adapters import SmartRecruitersSource
    page1 = {"totalFound": 2, "content": [
        {"id": "p1", "name": "Data Intern", "company": {"name": "Bosch"},
         "location": {"city": "Berlin", "country": "de"}, "typeOfEmployment": {"label": "Intern"}}]}
    page2 = {"totalFound": 2, "content": [
        {"id": "p2", "name": "Python Engineer", "company": {"name": "Bosch"},
         "location": {"city": "Munich", "country": "de", "remote": True}}]}
    detail = {"postingUrl": "https://jobs.smartrecruiters.com/Bosch/p1-data-intern",
              "applyUrl": "https://jobs.smartrecruiters.com/Bosch/p1/apply",
              "jobAd": {"sections": {"jobDescription": {"title": "Job", "text": "<p>Build things</p>"}}}}
    routes = [("offset=0", _resp(200, page1)), ("offset=1", _resp(200, page2)),
              ("postings/p1", _resp(200, detail)), ("postings/p2", _resp(500))]
    res, _ = _run(SmartRecruitersSource(boards=["Bosch"]), routes)
    assert [l.title for l in res.listings] == ["Data Intern", "Python Engineer"]
    assert res.listings[0].original_url.endswith("p1-data-intern")
    assert "Build things" in res.listings[0].description_text
    assert res.listings[1].workplace_arrangement == "remote"
    assert res.listings[1].description_text is None  # detail failed, list data kept
    assert res.failed == 1 and res.fetched == 3


def test_recruitee_dedupes_and_parses_salary():
    from sources.ats_adapters import RecruiteeSource
    offer = {"id": 7, "title": "Werkstudent Python", "description": "<p>Hallo</p>",
             "careers_url": "https://acme.recruitee.com/o/werk", "city": "Berlin", "country": "Germany",
             "hybrid": True, "salary": {"min": 15, "max": 18, "currency": "EUR", "period": "hour"}}
    res, _ = _run(RecruiteeSource(boards=["acme"]),
                  [("acme.recruitee.com", _resp(200, {"offers": [offer, offer]}))])
    assert len(res.listings) == 1 and res.duplicates == 1 and res.parsed == 2
    j = res.listings[0]
    assert j.employment_type == EmploymentType.WORKING_STUDENT
    assert j.salary == "15–18 EUR per hour" and j.workplace_arrangement == "hybrid"


PERSONIO_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<workzag-jobs><position><id>42</id><subcompany>Acme GmbH</subcompany><office>Berlin</office>
<name>Junior Softwareentwickler (m/w/d)</name><employmentType>permanent</employmentType>
<schedule>full-time</schedule><jobDescriptions><jobDescription><name>Aufgaben</name>
<value><![CDATA[<p>Python entwickeln</p>]]></value></jobDescription></jobDescriptions>
<createdAt>2026-10-01T10:00:00+00:00</createdAt></position></workzag-jobs>"""


def test_personio_xml():
    from sources.ats_adapters import PersonioSource
    res, _ = _run(PersonioSource(boards=["acme"]),
                  [("acme.jobs.personio.de/xml", _resp(200, raw=PERSONIO_XML))])
    j = res.listings[0]
    assert j.company == "Acme GmbH" and j.original_url == "https://acme.jobs.personio.de/job/42"
    assert "Python entwickeln" in j.description_text
    assert j.employment_type == EmploymentType.FULL_TIME


def test_personio_malformed_xml_fails_cleanly():
    from sources.ats_adapters import PersonioSource
    res, _ = _run(PersonioSource(boards=["acme"]), [("personio", _resp(200, raw=b"<oops"))])
    assert res.success is False and "malformed XML" in res.error_message


def test_workable_parses_locations():
    from sources.ats_adapters import WorkableSource
    body = {"name": "Acme", "jobs": [{"title": "Backend Engineer", "shortcode": "AB1", "telecommuting": True,
            "url": "https://apply.workable.com/j/AB1", "employment_type": "Full-time",
            "locations": [{"city": "Paris", "country": "France"}], "description": "<p>Go</p>"}]}
    res, _ = _run(WorkableSource(boards=["acme"]), [("accounts/acme", _resp(200, body))])
    j = res.listings[0]
    assert j.location == "Paris, France" and j.workplace_arrangement == "remote"


def test_arbeitnow_follows_next_link():
    from sources.ats_adapters import ArbeitnowSource
    p1 = {"data": [{"slug": "s1", "title": "Python Dev", "company_name": "X",
                    "url": "https://www.arbeitnow.com/jobs/s1", "location": "Berlin"}],
          "links": {"next": "page=2"}}
    p2 = {"data": [{"slug": "s2", "title": "SQL Dev", "company_name": "Y",
                    "url": "https://www.arbeitnow.com/jobs/s2", "location": "Hamburg", "remote": True}],
          "links": {"next": None}}
    res, calls = _run(ArbeitnowSource(), [("page=1", _resp(200, p1)), ("page=2", _resp(200, p2))])
    assert len(res.listings) == 2 and len(calls) == 2


@pytest.mark.parametrize("payload", [b"not json", b'{"jobs": "nope"}', b"[]"])
def test_malformed_payload_is_isolated_per_board(payload):
    from sources.ats_adapters import AshbySource
    good = {"jobs": [{"id": "1", "title": "Dev", "jobUrl": "https://jobs.ashbyhq.com/good/1"}]}
    res, _ = _run(AshbySource(boards=["bad", "good"]),
                  [("job-board/bad", _resp(200, raw=payload)), ("job-board/good", _resp(200, good))])
    assert res.success and len(res.listings) == 1
    assert "bad:" in res.error_message


def test_all_boards_failing_reports_failure_not_crash():
    from sources.ats_adapters import WorkableSource
    res, _ = _run(WorkableSource(boards=["x"]), [])
    assert res.success is False and "HTTP 404" in res.error_message and res.failed == 1


def test_no_boards_configured_is_a_skip(monkeypatch):
    from sources.ats_adapters import RecruiteeSource
    monkeypatch.setattr("services.ats_detection.registered_boards", lambda p: [])
    res = RecruiteeSource().scan(_cfg())
    assert res.success and res.listings == [] and "ats_boards" in res.error_message


def test_boards_from_config_and_registry(monkeypatch):
    from sources.ats_adapters import AshbySource
    monkeypatch.setattr("services.ats_detection.registered_boards",
                        lambda p: ["found"] if p == "ashby" else [])
    boards = AshbySource().resolve_boards(_cfg(ats_boards=["ashby:mine", "workable:other", "ashby:MINE"]))
    assert boards[:2] == ["mine", "found"] and "other" not in boards
    assert len(boards) == len({b.lower() for b in boards})


def test_engine_isolates_crashing_adapter(monkeypatch):
    from sources import search_engine
    from sources.base import JobSource, RawListing, SourceResult

    class Boom(JobSource):
        name, slug = "Boom", "boom"

        def scan(self, cfg):
            raise RuntimeError("kaput")

    class Ok(JobSource):
        name, slug = "Ok", "ok"

        def scan(self, cfg):
            return SourceResult(success=True, listings=[
                RawListing(source="Ok", title="T", company="C", original_url="https://e.com/1")])

    monkeypatch.setattr(search_engine, "list_sources", lambda: [Boom(), Ok()])
    monkeypatch.setattr(search_engine, "get_search_provider", lambda cfg: None)
    summary = search_engine.SearchEngine().run_all(_cfg())
    assert len(summary.all_listings) == 1
    assert any(f["source"] == "Boom" for f in summary.failures)


@pytest.mark.parametrize("url,provider,board", [
    ("https://jobs.ashbyhq.com/linear/123", "ashby", "linear"),
    ("https://boards.greenhouse.io/stripe/jobs/1", "greenhouse", "stripe"),
    ("https://jobs.lever.co/datadog/abc", "lever", "datadog"),
    ("https://jobs.smartrecruiters.com/Visa/7444", "smartrecruiters", "Visa"),
    ("https://acme.recruitee.com/o/dev", "recruitee", "acme"),
    ("https://acme.jobs.personio.de/job/42", "personio", "acme"),
    ("https://apply.workable.com/acme/j/AB1/", "workable", "acme"),
    ("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/x", "workday",
     "nvidia/NVIDIAExternalCareerSite"),
    ("https://careers-acme.icims.com/jobs/1/job", "icims", "acme"),
    ("https://jobs.jobvite.com/acme/job/o1", "jobvite", "acme"),
])
def test_detect_ats_from_url(url, provider, board):
    from services.ats_detection import detect_ats_from_url
    m = detect_ats_from_url(url)
    assert (m.provider, m.board) == (provider, board)


def test_detect_ignores_plain_sites():
    from services.ats_detection import detect_ats_from_url
    assert detect_ats_from_url("https://example.com/careers") is None
    assert detect_ats_from_url("https://jobs.ashbyhq.com/") is None


def test_detect_ats_from_html_embeds_and_jsonld():
    from services.ats_detection import detect_ats_from_html, find_careers_link
    html = """<a href="/karriere">Karriere</a>
      <script src="https://boards.greenhouse.io/embed/job_board/js?for=acme"></script>
      <iframe src="https://acme.jobs.personio.de/"></iframe>
      <script type="application/ld+json">{"@type":"JobPosting","url":"https://jobs.lever.co/acme/1"}</script>"""
    keys = {m.registry_key for m in detect_ats_from_html(html, "https://acme.com")}
    assert {"greenhouse:acme", "personio:acme", "lever:acme"} <= keys
    assert find_careers_link(html, "https://acme.com") == "https://acme.com/karriere"

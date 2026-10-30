"""Major job-board coverage: site queries, title parsing, cross-source dedup, platform stats."""
from __future__ import annotations

from config.search_config import EmploymentType, SearchConfig
from services.platforms import (
    MAJOR_JOB_BOARDS, platform_of, same_city, split_board_title, vacancy_key,
    looks_like_results_page,
)
from services.query_planner import plan_queries
from sources.base import RawListing


def _cfg(**kw):
    kw.setdefault("employment_types", list(EmploymentType))
    return SearchConfig(**kw)


def test_planner_emits_site_query_per_board_except_api_ones():
    plan = plan_queries(_cfg(locations=["Berlin"], primary_title="Werkstudent Python"), max_queries=200)
    board_qs = [s.query_string for s in plan if s.category == "jobboard"]
    for b in MAJOR_JOB_BOARDS:
        hits = [q for q in board_qs if q.startswith(f"site:{b.site} ")]
        # API boards use their adapter; US-only ZipRecruiter is not queried for Berlin
        assert bool(hits) == (b.api_source is None and b.region in ("global", "de")), b.key
    assert "site:stepstone.de Werkstudent Python Berlin" in board_qs
    # German location -> German boards ranked first
    ordered = [s for s in plan if s.category == "jobboard"]
    assert ordered[0].meta["platform"] in ("StepStone", "Jobware")


def test_board_queries_survive_tight_query_cap():
    cfg = _cfg(required_skills=[f"skill{i}" for i in range(15)], include_keywords=["data"])
    plan = plan_queries(cfg, max_queries=12)
    assert len(plan) == 12
    assert sum(s.category == "jobboard" for s in plan) == 4
    assert len({s.query_string.lower() for s in plan}) == 12


def test_split_board_title_shapes_and_no_guessing():
    assert split_board_title("Junior Data Analyst - Acme GmbH - Berlin | Indeed.com",
                             "https://de.indeed.com/viewjob?jk=1") == ("Junior Data Analyst", "Acme GmbH", "Berlin")
    assert split_board_title("Acme hiring Python Intern in Munich, Bavaria | LinkedIn",
                             "https://www.linkedin.com/jobs/view/123") == ("Python Intern", "Acme", "Munich, Bavaria")
    assert split_board_title("Werkstudent IT (m/w/d) bei Foo AG in Hamburg",
                             "https://www.stepstone.de/stellenangebote--x") == ("Werkstudent IT (m/w/d)", "Foo AG", "Hamburg")
    # unknown shape on a board: no invented company
    assert split_board_title("Python Intern | Glassdoor", "https://www.glassdoor.com/job-listing/x")[1] == ""
    # non-board URL: untouched
    assert split_board_title("A - B - C", "https://acme.com/jobs/1") == ("A - B - C", "", None)


def test_results_pages_detected():
    assert looks_like_results_page("1.234 Python Jobs in Berlin")
    assert looks_like_results_page("Jobs in Berlin, Stellenangebote")
    assert not looks_like_results_page("Junior Python Developer (m/w/d)")


def test_platform_attribution():
    assert platform_of("https://de.indeed.com/viewjob?jk=1") == "Indeed"
    assert platform_of("https://www.linkedin.com/jobs/view/1") == "LinkedIn Jobs"
    assert platform_of("https://jobs.ashbyhq.com/acme/1") == "Ashby"
    assert platform_of("https://acme.com/careers/1") == "Company site"


def test_vacancy_key_normalises_tags_and_legal_forms():
    assert vacancy_key("Werkstudent Python (m/w/d)", "Acme GmbH") == vacancy_key("Werkstudent Python", "ACME")
    assert vacancy_key("Dev", "") == ""
    assert same_city("Berlin, Germany", "berlin") and same_city(None, "Berlin")
    assert not same_city("Berlin", "Munich")


def test_engine_dedupes_across_sources_and_prefers_direct_listing(monkeypatch):
    from sources import search_engine
    from sources.base import JobSource, SourceResult

    board = RawListing(source="Web Search", title="Python Intern", company="Acme",
                       location=None, original_url="https://de.indeed.com/viewjob?jk=9")
    direct = RawListing(source="Ashby", title="Python Intern (m/w/d)", company="Acme GmbH",
                        location="Berlin", original_url="https://jobs.ashbyhq.com/acme/1")
    other_city = RawListing(source="Ashby", title="Python Intern", company="Acme",
                            location="Munich", original_url="https://jobs.ashbyhq.com/acme/2")

    def src(name, items):
        class S(JobSource):
            def scan(self, cfg):
                return SourceResult(success=True, listings=list(items))
        S.name, S.slug = name, name.lower()
        return S()

    sources = [src("Web", [board]), src("Ash", [direct, other_city])]
    monkeypatch.setattr(search_engine, "list_sources", lambda: sources)
    monkeypatch.setattr(search_engine, "get_search_provider", lambda cfg: None)
    summary = search_engine.SearchEngine(max_workers=1).run_all(_cfg())

    urls = sorted(l.original_url for l in summary.all_listings)
    assert urls == ["https://jobs.ashbyhq.com/acme/1", "https://jobs.ashbyhq.com/acme/2"]
    assert summary.cross_source_duplicates == 1
    assert summary.per_platform["Indeed"] == {"found": 1, "unique": 0, "cross_source_duplicates": 1}
    assert summary.per_platform["Ashby"]["unique"] == 2
    assert summary.detail()["per_platform"]


def test_web_search_parses_board_results_and_skips_overview_pages():
    from sources.search_providers import SearchResult
    from sources.web_search import WebSearchSource

    class FakeProvider:
        name = provider_name = "fake"

        def search(self, query, max_results=10, page=1):
            return [
                SearchResult(title="Data Intern - Foo GmbH - Hamburg | Indeed.com",
                             url="https://de.indeed.com/viewjob?jk=1", snippet="s"),
                SearchResult(title="2.345 Python Jobs in Berlin | Indeed",
                             url="https://de.indeed.com/q-python-jobs.html", snippet="s"),
            ]

    res = WebSearchSource(provider=FakeProvider()).scan(_cfg(max_queries_per_scan=1))
    assert [(l.title, l.company, l.location) for l in res.listings] == [("Data Intern", "Foo GmbH", "Hamburg")]

"""Optional real Chromium tests against a local synthetic fixture, never a job site."""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlparse

import pytest

from services.ai_diagnostics import SAMPLE_JD


@pytest.fixture
def browser_fixture(monkeypatch):
    api = pytest.importorskip("playwright.sync_api")
    with api.sync_playwright() as pw:
        if not Path(pw.chromium.executable_path).exists():
            pytest.skip("Install optional Chromium using python -m playwright install chromium")
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            restricted = self.path == "/restricted"
            self.send_response(403 if restricted else 200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if restricted:
                page = "<title>Access denied</title>"
            else:
                page = '<title>Job fixture</title><article id="jd"></article><script>setTimeout(() => {document.getElementById("jd").textContent=' + json.dumps(SAMPLE_JD) + ';}, 25);</script>'
            self.wfile.write(page.encode())
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Only this local fixture gets a test-only network-policy substitution.
    def allow_fixture(url):
        assert urlparse(url).hostname == "127.0.0.1"
    monkeypatch.setattr("services.browser_extraction.validate_public_http_url", allow_fixture)
    monkeypatch.setattr("services.browser_extraction.is_allowed_by_robots", lambda url: True)
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_javascript_rendering_extracts_full_fixture_description(browser_fixture):
    from services.browser_extraction import render_public_page
    from services.detail_extractor import extract_from_html
    origin, requests = browser_fixture
    detail = extract_from_html(render_public_page(origin + "/job"), origin + "/job")
    assert detail.extraction_status == "COMPLETE"
    assert "Python is required" in detail.description_text
    assert requests == ["/job"]


def test_browser_respects_access_restriction_without_retry(browser_fixture):
    from services.browser_extraction import render_public_page
    origin, requests = browser_fixture
    with pytest.raises(ValueError, match="access_restricted"):
        render_public_page(origin + "/restricted")
    assert requests == ["/restricted"]


def test_extraction_pipeline_uses_browser_for_empty_javascript_shell(browser_fixture, monkeypatch):
    from types import SimpleNamespace
    from services import detail_extractor
    origin, _ = browser_fixture
    monkeypatch.setenv("ENABLE_PLAYWRIGHT", "true")
    monkeypatch.setattr("services.robots.is_allowed_by_robots", lambda *a, **kw: True)
    monkeypatch.setattr(detail_extractor, "safe_get", lambda *a, **kw: SimpleNamespace(status_code=200, content=b'<title>Job fixture</title><article id="jd"></article>', headers={}))
    result = detail_extractor.fetch_and_extract(origin + "/job")
    assert result.extraction_method == "browser" and result.extraction_status == "COMPLETE"

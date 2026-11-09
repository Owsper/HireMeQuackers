"""Opt-in rendering of permitted public pages; no challenges or sign-in."""
from __future__ import annotations
from urllib.parse import urlparse
from services.safe_requests import validate_public_http_url
from services.robots import is_allowed_by_robots


def render_public_page(url: str) -> str:
    validate_public_http_url(url)
    if not is_allowed_by_robots(url):
        raise ValueError("robots_disallowed")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("browser_dependency_unavailable") from None
    host = urlparse(url).hostname
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            context = browser.new_context(user_agent="HireMeQuackers/2.0", service_workers="block")
            def route_request(route):
                request = route.request
                try:
                    validate_public_http_url(request.url)
                    if urlparse(request.url).hostname != host or request.method != "GET" or request.resource_type in {"image", "media", "font"} or not is_allowed_by_robots(request.url):
                        route.abort()
                        return
                except ValueError:
                    route.abort()
                    return
                route.continue_()
            context.route("**/*", route_request)
            page = context.new_page()
            response = page.goto(url, wait_until="networkidle", timeout=20000)
            if response is None or response.status in {401, 403, 429} or any(word in page.title().lower() for word in ("captcha", "access denied", "sign in", "just a moment")):
                raise ValueError("browser_access_restricted")
            return page.content()[:3 * 1024 * 1024]
        finally:
            browser.close()

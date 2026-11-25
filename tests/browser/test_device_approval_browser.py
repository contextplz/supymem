"""Run the gateway approval page with local credentials in an isolated browser."""

import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from tests.integration.test_gateway_b1_security import _close_gateway, _gateway


@pytest.mark.parametrize("mode", ["manual", "auto", "local", "retry"])
def test_approval_retries_opening_without_issuing_another_code(tmp_path, monkeypatch, mode):
    playwright = pytest.importorskip("playwright.sync_api")
    main, client, saved = _gateway(tmp_path, monkeypatch)
    base = "http://gateway.test"
    monkeypatch.setattr(main.settings, "gateway_origin", base)
    monkeypatch.setattr(main.settings, "console_origin", "http://console.test")
    url = f"{base}/auth/device/approve?code_challenge={'c' * 43}"
    posts = []
    expected_posts = 2 if mode == "retry" else 1
    screenshot_dir = os.getenv("SUPYMEM_APPROVAL_SCREENSHOT_DIR")
    try:
        if mode == "auto":
            client.get(urlsplit(url).path + "?" + urlsplit(url).query, follow_redirects=False)
        client.post("/auth/login", json={"passphrase": "changeme"})
        # test permits the real hosted-page branch with the isolated local subject.
        monkeypatch.setattr(main.settings, "app_env", "local" if mode == "local" else "test")
        logo = Path(__file__).resolve().parents[2] / "apps/console/public/supymem-logo.svg"
        with playwright.sync_playwright() as runner:
            browser = runner.firefox.launch(headless=True, firefox_user_prefs={
                "network.protocol-handler.external.supymem": False,
                "network.protocol-handler.external.supymem-staging": False,
            })
            context = browser.new_context(viewport={"width": 390, "height": 844})
            context.add_cookies([{"name": "cos_csrf", "value": client.cookies.get("cos_csrf"), "url": base}])
            context.route("http://console.test/supymem-logo.svg", lambda route: route.fulfill(
                content_type="image/svg+xml", body=logo.read_bytes()))

            def gateway_route(route):
                request = route.request
                if request.method == "POST":
                    posts.append(request.url)
                    if mode == "retry" and len(posts) == 1:
                        route.fulfill(status=503, json={"error": {"code": "upstream_unavailable"}})
                        return
                parsed = urlsplit(request.url)
                response = client.request(request.method, parsed.path + "?" + parsed.query,
                    content=request.post_data,
                    headers={key: value for key, value in request.headers.items() if key != "cookie"},
                    follow_redirects=False)
                route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.content)

            context.route(f"{base}/**", gateway_route)
            page = context.new_page()
            page.goto(url)
            if mode != "auto":
                playwright.expect(page.get_by_role("button", name="Approve this Mac")).to_be_visible()
                playwright.expect(page.locator("#fallback")).to_be_hidden()
                if screenshot_dir and mode == "manual":
                    Path(screenshot_dir).mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(Path(screenshot_dir) / "a3-before-approval.png"))
                page.get_by_role("button", name="Approve this Mac").click()
                if mode == "retry":
                    playwright.expect(page.get_by_text("The connection could not be completed. Please try again.")).to_be_visible()
                    playwright.expect(page.locator("#approve")).to_be_enabled()
                    page.get_by_role("button", name="Approve this Mac").click()
            playwright.expect(page.locator("#approve")).to_be_hidden()
            playwright.expect(page.locator("#fallback")).to_be_visible()
            assert len(posts) == expected_posts
            assert page.locator(".mark").evaluate("img => img.complete && img.naturalWidth > 0")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            if mode == "local":
                playwright.expect(page.locator("#open")).to_be_hidden()
                playwright.expect(page.locator("#approval-code")).to_be_visible()
            else:
                retry = page.get_by_role("link", name="Open Supymem")
                playwright.expect(retry).to_be_visible()
                playwright.expect(page.locator("#status")).to_have_text("Done. Supymem for Mac is signed in.")
                playwright.expect(page.locator("#approval-code")).to_be_hidden()
                if screenshot_dir and mode == "manual":
                    page.screenshot(path=str(Path(screenshot_dir) / "a3-after-approval.png"))
                retry.click()
                assert len(posts) == expected_posts
                page.get_by_text("Having trouble?", exact=True).click()
                playwright.expect(page.locator("#approval-code")).to_be_visible()
                if screenshot_dir and mode == "manual":
                    page.screenshot(path=str(Path(screenshot_dir) / "a3-code-disclosure.png"), mask=[page.locator("#approval-code")])
            assert page.locator("#approval-code").input_value()
            browser.close()
    finally:
        _close_gateway(saved)

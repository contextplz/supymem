"""Rendered console auth/permission checks with synthetic native and gateway edges.

Needs a separately started isolated Next server (GTM_CONSOLE_URL). This proves
console behavior; real OAuth, macOS TCC and release acceptance remain separate.
"""
import json
import os
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import pytest

pytestmark = pytest.mark.skipif(not os.getenv("GTM_CONSOLE_URL"), reason="isolated Console URL not supplied")


def _context(runner, *, native=False, connected=False, runtime_config=True, founder_ui="home"):
    browser = runner.firefox.launch(headless=True)
    context = browser.new_context(viewport={"width": 1100, "height": 780})
    context.add_init_script("""
      sessionStorage.setItem('cos_gateway_url', 'https://gateway-fixture.invalid');
    """)
    if runtime_config:
        context.add_init_script("window.__SUPYMEM__ = {mode: 'hosted'};")
    if native:
        context.add_init_script("""
          window.fixtureCalls = [];
          window.fixturePermissions = {'screen-recording': 'not_determined', accessibility: 'granted'};
          window.fixtureResumed = 0;
          window.__TAURI__ = {core: {invoke: async (command, args = {}) => {
            window.fixtureCalls.push(command);
            if (command === 'gateway_url') return 'https://gateway-fixture.invalid';
            if (command === 'desktop_auth_status') return STATUS;
            if (command === 'desktop_auth_start') return {...STATUS, state: 'authorizing', login_url: 'https://identity-fixture.invalid/approve'};
            if (command === 'permissions_status' || command === 'permissions_request') return window.fixturePermissions;
            if (command === 'desktop_api_request') {
              const response = await fetch('https://gateway-fixture.invalid' + args.path,
                {method: args.method, ...(args.body ? {body: JSON.stringify(args.body)} : {})});
              return {status: response.status, data: await response.json()};
            }
            return null;
          }}, event: {listen: async () => () => {}}};
        """.replace("STATUS", json.dumps({"state": "connected" if connected else "signed_out",
            "subject": "usr_fixture" if connected else None, "persisted": True})))
    requests = []

    def gateway(route):
        path = urlsplit(route.request.url).path
        requests.append((route.request.method, route.request.url, route.request.post_data_json if route.request.post_data else None))
        if path == "/auth/session":
            route.fulfill(status=200 if connected else 401, json={"authenticated": connected,
                "subject": "usr_fixture" if connected else None, "csrf": "fixture-csrf"})
        elif path == "/auth/cognito/start":
            route.fulfill(json={"authorize_url": "https://identity-fixture.invalid/signed-in"})
        elif path == "/auth/login":
            route.fulfill(status=401, json={"authenticated": False})
        elif path == "/api/email/preferences":
            route.fulfill(json={"preferences": {"product_updates": True, "engagement_reminders": False, "digests": False, "suggestions": True}})
        elif path == "/api/ui-config":
            route.fulfill(json={"founder_ui": founder_ui}, headers={"Cache-Control": "no-store"})
        else:
            route.fulfill(json={"items": [], "codes": []})

    context.route("https://gateway-fixture.invalid/**", gateway)
    context.route("https://identity-fixture.invalid/**", lambda route: route.fulfill(body="Synthetic secure sign-in opened"))
    return browser, context, requests


def _shot(page, name):
    destination = os.getenv("SUPYMEM_SIGNIN_SCREENSHOT_DIR")
    if destination:
        Path(destination).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(destination) / f"{name}.png"))


@pytest.mark.parametrize("provider", ["google", "email"])
def test_hosted_auth_only_offers_google_and_email(provider):
    from playwright.sync_api import sync_playwright, expect
    with sync_playwright() as runner:
        browser, context, requests = _context(runner)
        page = context.new_page()
        page.goto(os.environ["GTM_CONSOLE_URL"] + "/auth")
        expect(page.get_by_role("heading", name="Sign in to Supymem")).to_be_visible(timeout=30000)
        expect(page).to_have_url(os.environ["GTM_CONSOLE_URL"].rstrip("/") + "/auth")
        expect(page.get_by_role("tab")).to_have_count(0)
        expect(page.locator(".login-card button")).to_have_count(2)
        expect(page.get_by_text("Operator access", exact=True)).to_have_count(0)
        assert page.locator(".google-sign-in svg path").count() == 4
        assert "supymem-logo.svg" in page.locator(".login-kicker").evaluate("el => getComputedStyle(el, '::before').backgroundImage")
        _shot(page, "a2-hosted-sign-in")
        page.get_by_role("button", name="Sign in with Google" if provider == "google" else "Sign in with email", exact=True).click()
        expect(page).to_have_url("https://identity-fixture.invalid/signed-in")
        calls = [url for method, url, _ in requests if method == "POST" and "/auth/cognito/start" in url]
        assert len(calls) == 1
        assert ("?provider=google" in calls[0]) == (provider == "google")
        browser.close()


def test_browser_product_routes_never_mount_the_product_or_fetch_gateway_data():
    from playwright.sync_api import sync_playwright, expect
    with sync_playwright() as runner:
        browser, context, requests = _context(runner)
        page = context.new_page()
        for path in ["/", "/v/x", "/v/x/research", "/operator", "/new"]:
            page.goto(os.environ["GTM_CONSOLE_URL"] + path)
            expect(page.get_by_role("heading", name="Supymem runs on your Mac", exact=True)).to_be_visible(timeout=30000)
            expect(page.locator(".workspace-body, .sidebar, aside[aria-label='Sidebar']")).to_have_count(0)
            expect(page.get_by_role("button", name="Sign in with Google", exact=True)).to_have_count(0)
        assert requests == []
        browser.close()


def test_native_signed_out_has_one_sign_in_and_no_permission_requests():
    from playwright.sync_api import sync_playwright, expect
    with sync_playwright() as runner:
        browser, context, _ = _context(runner, native=True)
        page = context.new_page()
        page.goto(os.environ["GTM_CONSOLE_URL"] + "/account")
        expect(page.get_by_role("heading", name="Sign in to Supymem")).to_be_visible(timeout=30000)
        expect(page.locator(".login-card .primary")).to_have_count(1)
        expect(page.get_by_role("button", name="Sign in", exact=True)).to_be_visible()
        expect(page.get_by_text("Mac account connection", exact=True)).to_have_count(0)
        expect(page.get_by_role("button", name="Copy Supymem sign-in link", exact=True)).to_be_visible()
        _shot(page, "a1-native-startup")
        assert not any(command in {"desktop_permission_status", "desktop_permission_request", "permissions_request"} for command in page.evaluate("fixtureCalls"))
        page.get_by_role("button", name="Sign in", exact=True).click()
        expect(page.get_by_role("button", name="Start sign-in again", exact=True)).to_be_visible()
        assert page.evaluate("fixtureCalls.filter(c => c === 'desktop_auth_start').length") == 1
        browser.close()


def test_native_permission_denial_persists_and_retry_only_preflights():
    from playwright.sync_api import sync_playwright, expect
    with sync_playwright() as runner:
        browser, context, _ = _context(runner, native=True, connected=True)
        page = context.new_page()
        page.goto(os.environ["GTM_CONSOLE_URL"] + "/account")
        expect(page.locator(".workspace-body")).to_be_visible(timeout=30000)
        assert not any(command in {"desktop_permission_status", "desktop_permission_request", "permissions_request"} for command in page.evaluate("fixtureCalls"))
        demand = "window.dispatchEvent(new CustomEvent('supymem-mac-permissions-needed', {detail: {resume: () => fixtureResumed++}}))"
        page.evaluate(demand)
        expect(page.get_by_role("button", name="Grant permission", exact=True)).to_be_visible()
        expect(page.get_by_text("Supymem needs Screen Recording to see the app it's helping you with.", exact=True)).to_be_visible()
        _shot(page, "a5-first-mac-task")
        page.get_by_role("button", name="Grant permission", exact=True).click()
        expect(page.get_by_role("button", name="Open System Settings", exact=True)).to_be_visible()
        assert page.evaluate("localStorage.getItem('supymem-screen-recording-requested')") == "yes"
        assert page.evaluate("fixtureCalls.filter(c => c === 'permissions_request').length") == 1
        page.reload()
        expect(page.locator(".workspace-body")).to_be_visible(timeout=30000)
        assert "permissions_request" not in page.evaluate("fixtureCalls")
        page.evaluate(demand)
        expect(page.get_by_role("button", name="Open System Settings", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="Grant permission", exact=True)).to_have_count(0)
        _shot(page, "a5-denied-after-relaunch")
        page.get_by_role("button", name="Open System Settings", exact=True).click()
        page.evaluate("fixturePermissions['screen-recording'] = 'granted'")
        page.get_by_role("button", name="Retry", exact=True).click()
        expect(page.get_by_role("heading", name="Allow this Mac task", exact=True)).to_have_count(0)
        assert page.evaluate("fixtureResumed") == 1
        assert "permissions_request" not in page.evaluate("fixtureCalls")
        assert "desktop_permission_status" not in page.evaluate("fixtureCalls")
        browser.close()


def test_reading_toggle_and_route_scroll_reset_in_persistent_shell():
    from playwright.sync_api import sync_playwright, expect
    with sync_playwright() as runner:
        browser, context, _ = _context(runner, native=True, connected=True, founder_ui="classic")
        page = context.new_page()
        page.goto(os.environ["GTM_CONSOLE_URL"] + "/v/fixture/research")
        expect(page.get_by_role("group", name="Detail level", exact=True)).to_be_visible(timeout=30000)
        expect(page.locator(".sidebar")).to_be_visible(timeout=30000)
        # Nest synthetic height in the departing page so React removes it on navigation.
        # An unmanaged sibling survives replacement and makes Next focus the new page below it.
        body = page.locator(".workspace-body")
        page_content = page.get_by_role("heading", name="Decide what is worth building", exact=True).locator("..")
        expect(page_content).to_be_visible(timeout=30000)
        page_content.evaluate("el => { const content = document.createElement('div'); content.dataset.scrollFixture = 'route-reset'; content.style.height = '3000px'; el.appendChild(content); el.closest('.workspace-body').scrollTop = 250; }")
        assert body.evaluate("el => el.scrollTop") > 0
        page.locator(".sidebar").hover()
        # Sidebar sections start expanded; clicking Manage would collapse it.
        page.get_by_role("link", name="Settings", exact=True).click()
        expect(page).to_have_url(os.environ["GTM_CONSOLE_URL"] + "/v/fixture/settings", timeout=30000)
        expect(page.get_by_role("group", name="Detail level", exact=True)).to_have_count(0)
        expect(page.locator("[data-scroll-fixture]")).to_have_count(0)
        expect(page.locator(".workspace-body")).to_be_visible()
        page.wait_for_function("document.querySelector('.workspace-body')?.scrollTop === 0")
        page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
        assert page.locator(".workspace-body").evaluate("el => el.scrollTop") == 0
        _shot(page, "a7-classic-settings-no-toggle")
        browser.close()


def test_chat_first_sidebar_replaces_hidden_reading_pages():
    from playwright.sync_api import sync_playwright, expect
    base = os.environ["GTM_CONSOLE_URL"]
    with sync_playwright() as runner:
        browser, context, _ = _context(runner, native=True, connected=True, founder_ui="home")
        page = context.new_page()
        page.goto(base + "/v/fixture/research")
        # Chat-first hides /v/<id>/research and sends the founder to the company chat.
        expect(page).to_have_url(base + "/v/fixture", timeout=30000)
        rail = page.get_by_role("complementary", name="Sidebar", exact=True)
        expect(rail).to_be_visible(timeout=30000)
        nav = rail.get_by_role("navigation", name="Main", exact=True)
        for label in ["New chat", "Tuesday Archives", "Idea", "LinkedIn"]:
            expect(nav.get_by_role("button", name=label, exact=True)).to_be_visible()
        expect(nav.get_by_role("button", name="Company", exact=True)).to_have_count(0)
        expect(page.locator(".sidebar")).to_have_count(0)
        expect(page.get_by_role("group", name="Detail level", exact=True)).to_have_count(0)
        rail.get_by_role("button", name="Settings", exact=True).click()
        settings = page.get_by_role("dialog", name="Settings", exact=True)
        expect(settings).to_be_visible()
        expect(settings.get_by_role("heading", name="Venture", exact=True)).to_be_visible()
        settings.get_by_role("button", name="Close settings", exact=True).click()
        expect(settings).to_have_count(0)
        expect(page).to_have_url(base + "/v/fixture")
        _shot(page, "a7-home-settings-dialog")
        browser.close()


_DEVICE_KEY = "supymem.device.connect.intent:v1"
_DEVICE_NOW = 1_800_000_000_000
_DEVICE_INTENT = {"version": 1, "code_challenge": "q" * 43, "state": "s" * 43,
                  "device_name": "Synthetic custody test Mac", "platform": "macos"}


def _seed_browser_device_intent(context, issued_at):
    """Synthetic tab storage/clock only; the real React components still run."""
    origin = urlsplit(os.environ["GTM_CONSOLE_URL"])
    context.add_init_script("""(() => {
      if (location.origin !== ORIGIN) return;
      const realNow = Date.now.bind(Date), startedAt = realNow();
      Date.now = () => NOW + realNow() - startedAt;
      sessionStorage.setItem(KEY, JSON.stringify(INTENT));
      window.syntheticDeviceIntentWrites = [];
      const setItem = Storage.prototype.setItem;
      Storage.prototype.setItem = function(key, value) {
        if (this === window.sessionStorage && key === KEY) {
          window.syntheticDeviceIntentWrites.push(JSON.parse(value).issued_at);
        }
        return setItem.call(this, key, value);
      };
    })();""".replace("ORIGIN", json.dumps(f"{origin.scheme}://{origin.netloc}"))
        .replace("NOW", str(_DEVICE_NOW)).replace("KEY", json.dumps(_DEVICE_KEY))
        .replace("INTENT", json.dumps({**_DEVICE_INTENT, "issued_at": issued_at})))


def _device_approvals(requests):
    return [url for method, url, _ in requests
            if method == "GET" and urlsplit(url).path == "/auth/device/approve"]


def test_browser_expired_matching_mac_request_cannot_remint_custody():
    """Real AppShell/DevicePage lifecycle; Gateway account responses are synthetic."""
    from playwright.sync_api import sync_playwright, expect
    with sync_playwright() as runner:
        browser, context, requests = _context(runner, native=False, connected=True, runtime_config=False)
        try:
            issued_at = _DEVICE_NOW - 5 * 60 * 1000 - 1
            _seed_browser_device_intent(context, issued_at)
            page = context.new_page()
            query = urlencode({key: value for key, value in _DEVICE_INTENT.items() if key != "version"})
            page.goto(os.environ["GTM_CONSOLE_URL"].rstrip("/") + "/auth/device?" + query)
            expect(page.get_by_role("heading", name="Start again from your Mac", exact=True)).to_be_visible(timeout=30000)
            expect(page.locator(".workspace-body, .sidebar, aside[aria-label='Sidebar']")).to_have_count(0)
            page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
            expect(page.get_by_role("heading", name="Start again from your Mac", exact=True)).to_be_visible()
            expect(page.get_by_role("button", name="Review Mac connection", exact=True)).to_have_count(0)
            assert _device_approvals(requests) == []
            assert page.evaluate("key => sessionStorage.getItem(key)", _DEVICE_KEY) is None
            assert all(value <= issued_at for value in page.evaluate("syntheticDeviceIntentWrites")), "Expired timestamp was reminted"
            assert not page.evaluate("Boolean(window.__TAURI__ || window.__TAURI_INTERNALS__ || window.__SUPYMEM__)")
            _shot(page, "synthetic-expired-device-custody")
        finally:
            browser.close()


@pytest.mark.parametrize("parameter", ["state", "device_name", "platform"])
def test_browser_partial_mac_request_refuses_older_stored_intent(parameter):
    """Partial current URL must not restore another synthetic tab-local request."""
    from playwright.sync_api import sync_playwright, expect
    with sync_playwright() as runner:
        browser, context, requests = _context(runner, native=False, connected=True, runtime_config=False)
        try:
            _seed_browser_device_intent(context, _DEVICE_NOW - 10_000)
            page = context.new_page()
            page.goto(os.environ["GTM_CONSOLE_URL"].rstrip("/") + "/auth/device?"
                      + urlencode({parameter: _DEVICE_INTENT[parameter]}))
            expect(page.get_by_role("heading", name="Start again from your Mac", exact=True)).to_be_visible(timeout=30000)
            expect(page.locator(".workspace-body, .sidebar, aside[aria-label='Sidebar']")).to_have_count(0)
            page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
            expect(page.get_by_role("heading", name="Start again from your Mac", exact=True)).to_be_visible()
            expect(page.get_by_role("button", name="Review Mac connection", exact=True)).to_have_count(0)
            assert _device_approvals(requests) == []
            assert page.evaluate("key => sessionStorage.getItem(key)", _DEVICE_KEY) is None
            assert not page.evaluate("Boolean(window.__TAURI__ || window.__TAURI_INTERNALS__ || window.__SUPYMEM__)")
            _shot(page, "synthetic-partial-device-" + parameter)
        finally:
            browser.close()


def test_browser_fresh_bare_mac_request_continues_once():
    """Positive synthetic custody control through actual AppShell and DevicePage."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as runner:
        browser, context, requests = _context(runner, native=False, connected=True, runtime_config=False)
        try:
            _seed_browser_device_intent(context, _DEVICE_NOW - 10_000)
            page = context.new_page()
            page.goto(os.environ["GTM_CONSOLE_URL"].rstrip("/") + "/auth/device")
            page.wait_for_url(lambda url: urlsplit(url).path == "/auth/device/approve", timeout=30000)
            approvals = _device_approvals(requests)
            assert len(approvals) == 1
            from urllib.parse import parse_qs
            assert parse_qs(urlsplit(approvals[0]).query) == {
                key: [value] for key, value in _DEVICE_INTENT.items() if key != "version"
            }
            assert not page.evaluate("Boolean(window.__TAURI__ || window.__TAURI_INTERNALS__ || window.__SUPYMEM__)")
        finally:
            browser.close()

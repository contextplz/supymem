"""Real Console + assembled signed Core; native bridge/auth/provider edges are fixtures.

Run against a separately started isolated Console via GTM_CONSOLE_URL. Browser
requests are forwarded to Core's actual ASGI routes, not JSON journey mocks.
This proves rendered integration, not production Mac/gateway/OAuth/Gmail delivery.
All identities, source pages and delivery receipts below are synthetic fixtures.
"""
from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

import pytest
import httpx
from fastapi.testclient import TestClient

from tests.security.test_core_signed_boundary import SECRET, headers
from tests.unit.gtm.test_journey import journey_db as journey_db, _mail, _work


pytestmark = pytest.mark.skipif(not os.getenv("GTM_CONSOLE_URL"), reason="isolated Console URL not supplied")


@pytest.fixture()
def rendered_core(journey_db, monkeypatch):
    from app import main
    from app.gtm import journey
    from app.autopilot import outreach

    monkeypatch.setenv("COMPANY_OS_REQUIRE_SIGNED_CALLER", "1")
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", SECRET)
    monkeypatch.setenv("COMPANY_OS_VAULT_SECRET", "browser-fixture-vault-secret-at-least-32-bytes")
    monkeypatch.setenv("ADMIN_REQUIRE_TOTP", "1")
    monkeypatch.setenv("ADMIN_BOOTSTRAP_EMAILS", "ops@example.test")
    monkeypatch.setattr(main.bootstrap_mod, "_mark_owned_auth_from_env", lambda: None)
    from app.content import composio_social
    monkeypatch.setattr(composio_social, "connectable", lambda platform: False)
    def no_provider_http(*args, **kwargs):
        raise AssertionError("Unexpected real provider HTTP call in browser fixture")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_provider_http)
    from tests.gtm_fixtures import qualified_bakery, quality_model
    monkeypatch.setattr(journey, "_discovery_model", quality_model)
    async def discover(who, search_queries=None, **kwargs):
        # Explicitly synthetic: a source candidate without an email must stay
        # blocked. The manual fixture contact below exercises the eligible path.
        return [await qualified_bakery({"organization": "Source Only Fixture", "source_url": "https://source-only.example/contact",
                 "why": "Synthetic browser fixture: a public source with no contact address."}, who, **kwargs)]
    monkeypatch.setattr(journey, "_discover", discover)
    monkeypatch.setattr(outreach, "site_url", lambda vid: None)
    sends = []
    def send(box, to, subject, body, thread):
        sends.append({"to": to, "subject": subject, "body": body})
        return {"provider_message_id": "browser-fixture-receipt", "thread_id": "browser-fixture-thread"}
    monkeypatch.setattr(outreach, "_send_one", send)
    # The journey fixture initialized real SQLite tables from app metadata. No process-wide
    # scheduler lifespan is needed to exercise the assembled HTTP routes.
    with TestClient(main.create_app()) as client, ThreadPoolExecutor(max_workers=1) as workers:
        yield journey_db, client, workers, sends


def _install_forwarder(page, base, fixture, *, load_gate=None, expected_rejections=None):
    d, client, workers, sends = fixture
    root = "/fixture_gateway"
    # GTM is a Mac operations surface. Supply only the native transport edge;
    # every desktop_api_request still makes browser HTTP that this harness
    # forwards, with a fresh signed assertion, to the assembled Core service.
    script = """const fixtureGateway = GATEWAY;
      sessionStorage.setItem('cos_gateway_url', fixtureGateway);
      window.__TAURI__ = { core: { invoke: async (command, args = {}) => {
        if (command === 'gateway_url') return fixtureGateway;
        if (command === 'desktop_auth_status') return { state: 'connected', subject: ACTOR };
        if (command === 'desktop_permission_status') return { permissions: {
          screen_recording: true, accessibility: true, event_synthesizing: true } };
        if (command === 'desktop_api_request') {
          const response = await fetch(fixtureGateway + args.path, { method: args.method,
            headers: { ...args.headers, 'Content-Type': 'application/json', 'X-CSRF-Token': 'fixture-csrf' },
            ...(args.body === undefined ? {} : { body: JSON.stringify(args.body) }) });
          return { status: response.status, data: await response.json().catch(() => null) };
        }
        return null;
      } }, event: { listen: async () => () => {} } };
    """.replace("GATEWAY", json.dumps(base + root)).replace("ACTOR", json.dumps(d["actor"]))
    page.add_init_script(script)
    requests = []
    def forward(route):
        incoming = route.request
        url = urlsplit(incoming.url)
        assert incoming.url.startswith(base + root + "/"), incoming.url
        gateway_path = url.path[len(root):]
        if gateway_path == "/auth/session":
            route.fulfill(json={"authenticated": True, "subject": d["actor"], "csrf": "fixture-csrf"})
            return
        if gateway_path == "/auth/invite/status":
            # Gateway-only auth edge: this synthetic signed-in account has no
            # pending website invite. The journey remains assembled Core HTTP.
            assert incoming.method == "GET"
            route.fulfill(json={"pending": False}, headers={"Cache-Control": "no-store"})
            return
        if gateway_path == "/api/ui-config":
            # Chat-first ("home") redirects /gtm and /v/<id>/research to the chat;
            # these pages are what founder_ui=classic founders still get.
            route.fulfill(json={"founder_ui": "classic"}, headers={"Cache-Control": "no-store"})
            return
        assert gateway_path.startswith("/api/"), gateway_path
        core_path = gateway_path[4:]
        requests.append((incoming.method, core_path))
        if load_gate and load_gate[0] and core_path.endswith("/gtm/journey") and incoming.method == "GET":
            route.fulfill(status=503, json={"detail": "Fixture load interruption"})
            return
        signed = headers(core_path, incoming.method, d["actor"])
        for key in ("content-type", "accept", "x-workspace-id", "idempotency-key"):
            if key in incoming.headers:
                signed[key] = incoming.headers[key]
        if incoming.method not in ("GET", "HEAD"):
            assert incoming.headers.get("x-csrf-token") == "fixture-csrf"
        response = client.request(incoming.method, core_path + ("?" + url.query if url.query else ""),
                                  content=incoming.post_data_buffer, headers=signed)
        if "/gtm/journey" in core_path:
            if expected_rejections and incoming.method == "PATCH" and core_path == expected_rejections[0]["path"]:
                rejection = expected_rejections.pop(0)
                assert incoming.post_data_json == rejection["payload"]
                assert response.status_code == rejection["status"], (core_path, response.status_code, response.text)
                assert response.json() == {"detail": {"code": rejection["code"]}}
            else:
                expected = 202 if core_path.endswith("/prepare") else 200
                assert response.status_code == expected, (core_path, response.status_code, response.text)
            data = response.json()
            if data.get("state") == "queued" and core_path.endswith(("/prepare", "/icp")):
                workers.submit(_work, d, data).result(timeout=60)
        # Forward only HTTP representation headers; TestClient decompression
        # and ASGI's connection headers must not corrupt browser delivery.
        reply_headers = {k: v for k, v in response.headers.items()
                         if k.lower() in ("content-type", "cache-control", "x-request-id")}
        route.fulfill(status=response.status_code, headers=reply_headers, body=response.content)
    page.route(base + root + "/**", forward)
    return requests


def _screenshot(page, tmp_path, name):
    directory = Path(os.getenv("GTM_BROWSER_ARTIFACT_DIR", str(tmp_path)))
    directory.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(directory / name), full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Horizontal overflow"


def _reticle_send(page, tmp_path):
    """Declare the Send consequence before clicking, on the existing fixture session."""
    port = os.getenv("GTM_RETICLE_PORT")
    if not port:
        page.get_by_role("button", name="Send 1 reviewed email", exact=True).click()
        return
    assert port.isdigit(), "Reticle port must be loopback numeric"
    predicate = {"kind": "allOf", "predicates": [
        {"kind": "net", "method": "POST", "urlContains": "/gtm/journey/send", "status": 200, "count": 1},
        {"kind": "text", "contains": "What happened after outreach", "visible": True},
    ]}
    arguments = {"target": {"role": "button", "name": "Send 1 reviewed email"},
        "action": "click", "intent": "Send the reviewed synthetic batch once and display its saved results.",
        "until": predicate, "timeout_ms": 30_000}
    (tmp_path / "gtm-reticle-expectation.json").write_text(json.dumps(predicate))
    command = ["npx", "--offline", "@reticlehq/server", "mcp", "--port", port]
    with (tmp_path / "gtm-reticle-stderr.txt").open("wb") as stderr:
        process = subprocess.Popen(command, cwd=Path(__file__).resolve().parents[2] / "apps" / "console",
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr, start_new_session=True)
        pending, transcript = b"", []
        deadline = time.monotonic() + 45

        def write(message):
            process.stdin.write((json.dumps(message) + "\n").encode())
            process.stdin.flush()

        def reply(identity):
            nonlocal pending
            while time.monotonic() < deadline:
                # Reticle's page commands and fixture HTTP routes need the
                # Playwright dispatcher running throughout the MCP call.
                page.wait_for_timeout(50)
                if not select.select([process.stdout], [], [], 0)[0]:
                    continue
                chunk = os.read(process.stdout.fileno(), 65536)
                assert chunk, "Reticle proxy exited before its response"
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    if not line.strip():
                        continue
                    message = json.loads(line)
                    transcript.append(message)
                    if message.get("id") == identity:
                        assert "error" not in message, message
                        return message["result"]
            raise AssertionError("Reticle causal Send verdict timed out")

        try:
            write({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "supymem-gtm-browser-fixture", "version": "1"}}})
            reply(1)
            write({"jsonrpc": "2.0", "method": "notifications/initialized"})
            # Acquire the control connection first: the daemon may have gone
            # idle while the fixture prepared drafts. Its SDK reconnects while
            # we keep Playwright pumping, before any Send action is admitted.
            write({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "reticle_session", "arguments": {"action": "list"}}})
            reply(2)
            sessions = []
            while time.monotonic() < deadline:
                page.wait_for_timeout(100)
                with urlopen(f"http://127.0.0.1:{port}/status", timeout=3) as response:
                    status = json.load(response)
                sessions = [item for item in status["sessions"] if not item.get("stale")]
                if len(sessions) == 1 and sessions[0].get("url") == page.url:
                    break
            assert len(sessions) == 1 and sessions[0].get("url") == page.url, "Expected one live Reticle session on this exact fixture page"
            arguments["sessionId"] = sessions[0]["sessionId"]
            (tmp_path / "gtm-reticle-action.json").write_text(json.dumps(arguments))
            write({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "reticle_act_and_wait", "arguments": arguments}})
            result = reply(3)
            (tmp_path / "gtm-reticle-verdict.json").write_text(json.dumps(result, indent=2))
            assert not result.get("isError"), result
            verdict = result.get("structuredContent")
            if not isinstance(verdict, dict) or "verified" not in verdict:
                verdict = next((json.loads(part["text"]) for part in result.get("content", [])
                    if part.get("type") == "text" and part.get("text", "").lstrip().startswith("{")), {})
            assert verdict.get("verified") == "yes", verdict
        finally:
            (tmp_path / "gtm-reticle-mcp.json").write_text(json.dumps(transcript, indent=2))
            process.stdin.close()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=2)
            process.stdout.close()


def _check_email_preferences(page, base, d, client, tmp_path, width):
    page.goto(base + "/account", wait_until="domcontentloaded")
    preferences = page.get_by_role("region", name="Email preferences", exact=True)
    preferences.wait_for(timeout=30_000)
    from playwright.sync_api import expect
    expect(page.get_by_role("heading", name="Start your first venture in the Supymem Mac app", exact=True)).to_be_hidden()
    # The canonical Core portfolio is {items}; a parser mismatch previously
    # showed the empty-account message despite this venture's metered send.
    # Founder decision: no credit counts, only the way to buy more.
    expect(page.get_by_role("heading", name="Buy more", exact=True)).to_be_visible()
    expect(page.get_by_role("region", name="Account credits", exact=True)).to_be_hidden()
    assert preferences.get_by_role("checkbox").count() == 4
    digest = preferences.get_by_role("checkbox", name="Daily and weekly digests")
    assert not digest.is_checked(), "Synthetic new account digests must start opt-in"
    with page.expect_response(lambda response: response.url.endswith("/email/preferences") and response.request.method == "PUT") as update:
        digest.check()
    assert update.value.status == 200
    assert update.value.request.post_data_json == {"digests": True}
    stored = client.get("/email/preferences", headers=headers("/email/preferences", subject=d["actor"])).json()
    assert stored["preferences"]["digests"] is True
    _screenshot(page, tmp_path, f"email-preferences-{width}.png")
    page.reload(wait_until="domcontentloaded")
    page.get_by_role("region", name="Email preferences", exact=True).wait_for(timeout=30_000)
    assert page.get_by_role("checkbox", name="Daily and weekly digests").is_checked()


def _skip_intro(page):
    from playwright.sync_api import TimeoutError as BrowserTimeout
    # This synthetic account is new; complete the ordinary first-launch Skip
    # control rather than clicking through its modal backdrop.
    intro = page.get_by_role("dialog", name="Meet Charlie", exact=True)
    try:
        intro.wait_for(timeout=5000)
    except BrowserTimeout:
        pass
    if intro.is_visible():
        intro.get_by_role("button", name="Skip", exact=True).click()
        intro.wait_for(state="hidden")


def _start_discovery(page, fixture, audience, *, note=""):
    d, client, _workers, sends = fixture
    start = page.get_by_role("heading", name="Find customers for Order board", exact=True)
    start.wait_for(timeout=90_000)
    _skip_intro(page)
    page.get_by_label("Who should we target?", exact=True).fill(audience)
    if note:
        page.get_by_label("Anything we should know? (existing customers, who to target or avoid)", exact=True).fill(note)
    page.wait_for_timeout(6200)
    assert start.is_visible()
    path = f"/api/v1/ventures/{d['vid']}/gtm/journey"
    response = client.get(path, params={"workspace_id": d["wid"]}, headers=headers(path, subject=d["actor"]))
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["discovery_started"] is False and saved["job_id"] is None
    assert saved["prospects"] == [] and sends == []
    with page.expect_response(lambda response: response.url.split("?")[0].endswith(path + "/icp") and response.request.method == "PATCH") as started:
        page.get_by_role("button", name="Find customers", exact=True).click()
    assert started.value.status == 200
    assert started.value.request.post_data_json == {"who": audience, "note": note, "recruitment_opt_in": True}
    assert started.value.json()["discovery_started"] is True
    assert started.value.json()["discovery_note"] == note


def _reach_send(page, base, fixture, expected_rejections):
    from playwright.sync_api import expect
    d, client, _workers, _sends = fixture
    journey_path = f"/api/v1/ventures/{d['vid']}/gtm/journey"

    def saved():
        response = client.get(journey_path, params={"workspace_id": d["wid"]},
                              headers=headers(journey_path, subject=d["actor"]))
        assert response.status_code == 200, response.text
        return response.json()

    page.goto(f"{base}/gtm?workspace={d['wid']}&venture={d['vid']}", wait_until="domcontentloaded")
    note = "Target owner-operated bakeries; avoid chains."
    _start_discovery(page, fixture, "Independent bakery owners managing orders by phone", note=note)
    page.get_by_role("heading", name="1 prospect to review", exact=True).wait_for(timeout=30_000)
    source_only = page.get_by_role("listitem").filter(has=page.get_by_text("Source Only Fixture", exact=True))
    expect(source_only.get_by_text("No email address was found at the saved source. Use Add a contact with evidence only when you have an actual address and source.", exact=True)).to_be_visible()
    expect(source_only.get_by_text("Review permission to contact", exact=True)).to_be_hidden()
    expect(source_only.get_by_role("button", name="Add sending address", exact=True)).to_be_hidden()
    missing = saved()
    assert missing["discovery_note"] == note
    assert missing["prospects"][0]["email"] is None
    assert missing["prospects"][0]["blocked_reason"] == "published_email_missing"
    assert missing["drafts"] == []
    page.get_by_text("Add a contact with evidence", exact=True).click()
    for label, value in {
        "Name": "Ada Fixture", "Email": "ada@bakery.example", "Company": "Bakery Fixture",
        "Source URL": "https://bakery.example/contact", "Country code": "US",
        "Evidence of permission": "Synthetic fixture: Ada explicitly agreed to hear about the order board.",
    }.items():
        page.locator("form").get_by_label(label, exact=True).fill(value)
    page.get_by_role("button", name="Add contact and prepare draft").click()
    ada = page.get_by_role("listitem").filter(has=page.get_by_text("Ada Fixture", exact=True))
    expect(ada.get_by_text("US outreach needs your business mailing address in the email footer. Save your sending address to prepare eligible drafts.", exact=True)).to_be_visible()
    expect(ada.get_by_text("Review permission to contact", exact=True)).to_be_hidden()
    expect(page.get_by_role("button", name="Review 1 draft", exact=True)).to_be_hidden()
    blocked = saved()
    assert next(p for p in blocked["prospects"] if p["email"] == "ada@bakery.example")["blocked_reason"] == "us_postal_address_required"
    assert blocked["drafts"] == []
    ada.get_by_role("button", name="Add sending address", exact=True).click()
    address = page.get_by_label("Business mailing address", exact=True)
    expect(address).to_be_visible()
    assert page.get_by_text("Sending address for your footer", exact=True).evaluate("summary => summary.parentElement.open")
    postal = "123 Fixture Lane, Example City, CA 90000, US"
    address.fill(postal)
    with page.expect_response(lambda response: response.url.split("?")[0].endswith(journey_path + "/settings") and response.request.method == "PATCH") as addressed:
        page.get_by_role("button", name="Save sending address", exact=True).click()
    assert addressed.value.status == 200
    assert addressed.value.request.post_data_json == {"sender_postal_address": postal}
    eligible = addressed.value.json()
    assert next(p for p in eligible["prospects"] if p["email"] == "ada@bakery.example")["blocked_reason"] is None
    assert next(p for p in eligible["prospects"] if not p["email"])["blocked_reason"] == "published_email_missing"
    assert len(eligible["drafts"]) == 1 and postal in eligible["drafts"][0]["body"]
    page.get_by_role("button", name="Review 1 draft", exact=True).click()
    page.get_by_role("heading", name="Make the first message count").wait_for()
    page.get_by_role("button", name="Approve message", exact=True).click()
    page.get_by_role("button", name="Review sending · 1 approved", exact=True).wait_for()
    page.get_by_role("article").get_by_role("button", name="Edit", exact=True).click()
    subject = page.get_by_label("Subject", exact=True)
    subject.fill("A simpler bakery order board")
    opened = saved()["drafts"][0]
    draft_path = journey_path + "/drafts/" + opened["doc_id"]
    # A distinct signed HTTP client changes the real durable draft while the
    # browser editor remains open. No response JSON or component state is mocked.
    second_client = TestClient(client.app)
    try:
        concurrent = second_client.patch(draft_path, params={"workspace_id": d["wid"]},
            headers=headers(draft_path, "PATCH", d["actor"]), json={
                "expected_revision": opened["revision"], "decision": "draft",
                "subject": "A concurrent fixture revision", "body": opened["body"],
            })
    finally:
        second_client.close()
    assert concurrent.status_code == 200, concurrent.text
    changed = next(draft for draft in concurrent.json()["drafts"] if draft["doc_id"] == opened["doc_id"])
    assert changed["revision"] != opened["revision"]
    assert changed["subject"] == "A concurrent fixture revision" and changed["review_state"] == "draft"

    def current_poll(response):
        return (response.request.method == "GET" and response.url.split("?")[0].endswith(journey_path)
                and any(draft["doc_id"] == opened["doc_id"] and draft["revision"] == changed["revision"]
                        for draft in response.json().get("drafts", [])))

    # Returning focus must adopt the new server revision without idle polling or
    # replacing the text/revision captured when this editor opened.
    with page.expect_response(current_poll, timeout=20_000) as polled:
        page.evaluate("window.dispatchEvent(new Event('focus'))")
        expect(page.get_by_role("button", name="Review sending · 0 approved", exact=True)).to_be_visible(timeout=20_000)
    assert polled.value.status == 200
    expect(subject).to_have_value("A simpler bakery order board")
    expect(page.get_by_role("textbox", name="Message", exact=True)).to_have_value(opened["body"])
    old_save = {"expected_revision": opened["revision"], "decision": "draft",
                "subject": "A simpler bakery order board", "body": opened["body"]}
    # This one intentional rejection retains the strict 200/202 assertion for
    # every other journey request. A client incorrectly using the fresh revision
    # would receive 200 here and fail, rather than overwrite silently.
    expected_rejections.append({"path": draft_path, "payload": old_save,
                                "status": 422, "code": "gtm_draft_changed"})
    with page.expect_response(lambda response: response.url.split("?")[0].endswith(draft_path) and response.request.method == "PATCH") as stale:
        page.get_by_role("button", name="Save edit", exact=True).click()
    assert stale.value.status == 422 and stale.value.request.post_data_json == old_save
    assert not expected_rejections
    expect(page.get_by_text("This message changed. Refresh saved work and review the current message before deciding.", exact=True)).to_be_visible()
    untouched = next(draft for draft in saved()["drafts"] if draft["doc_id"] == opened["doc_id"])
    assert (untouched["subject"], untouched["body"], untouched["revision"]) == (changed["subject"], changed["body"], changed["revision"])
    # Recover through the displayed current draft, then edit and review again.
    page.get_by_role("button", name="Cancel edit", exact=True).click()
    expect(page.get_by_role("heading", name="A concurrent fixture revision", exact=True)).to_be_visible()
    page.get_by_role("article").get_by_role("button", name="Edit", exact=True).click()
    page.get_by_label("Subject", exact=True).fill("A simpler bakery order board")
    with page.expect_response(lambda response: response.url.split("?")[0].endswith(draft_path) and response.request.method == "PATCH") as fresh:
        page.get_by_role("button", name="Save edit", exact=True).click()
    assert fresh.value.status == 200
    assert fresh.value.request.post_data_json["expected_revision"] == changed["revision"]
    expect(page.get_by_role("heading", name="A simpler bakery order board", exact=True)).to_be_visible()
    # Actual service clears the previous approval after a content edit.
    page.get_by_role("button", name="Review sending · 0 approved", exact=True).wait_for()
    page.get_by_role("button", name="Approve message", exact=True).click()
    page.get_by_role("button", name="Review sending · 1 approved", exact=True).click()
    page.get_by_role("heading", name="Send the batch you reviewed").wait_for()
    expect(page.get_by_text("Up to 0 approved emails", exact=False)).to_be_hidden()
    expect(page.get_by_text("Sending this batch uses up to 0 credits.", exact=True)).to_be_hidden()
    expect(page.get_by_text("Approve at most 0 emails", exact=False)).to_be_hidden()


@pytest.mark.parametrize("viewport", [{"width": 1440, "height": 900}, {"width": 390, "height": 844}], ids=["desktop", "mobile"])
def test_actual_journey_review_meter_send_and_results(rendered_core, viewport, tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    base = os.environ["GTM_CONSOLE_URL"].rstrip("/")
    d, client, workers, sends = rendered_core
    _mail(d, sending_address=False)
    with playwright.sync_playwright() as runner:
        browser = runner.firefox.launch(headless=True)
        page = browser.new_page(viewport=viewport)
        page.set_default_navigation_timeout(90_000)
        expected_rejections = []
        requests = _install_forwarder(page, base, rendered_core, expected_rejections=expected_rejections)
        _reach_send(page, base, rendered_core, expected_rejections)
        assert not sends
        send_button = page.get_by_role("button", name="Send 1 reviewed email", exact=True)
        playwright.expect(send_button).to_be_enabled()  # founder decision: approving the batch is the permission
        _screenshot(page, tmp_path, f"gtm-{viewport['width']}-reviewed-send.png")
        _reticle_send(page, tmp_path)
        results_heading = page.get_by_role("heading", name="What happened after outreach")
        results_heading.wait_for(timeout=30_000)
        playwright.expect(results_heading).to_be_in_viewport(ratio=1)
        playwright.expect(results_heading).to_be_focused()
        playwright.expect(page.get_by_text("Possible interest", exact=True)).to_be_visible()
        _screenshot(page, tmp_path, f"gtm-{viewport['width']}-results.png")
        assert len(sends) == 1 and sends[0]["to"] == "ada@bakery.example"
        assert sends[0]["subject"] == "A simpler bakery order board"
        path = f"/api/v1/ventures/{d['vid']}/gtm/journey"
        saved = client.get(path, params={"workspace_id": d["wid"]}, headers=headers(path, subject=d["actor"])).json()
        assert saved["results"]["sent"] == 1 and saved["results"]["replied"] == 0
        assert saved["drafts"][0]["state"] == "sent"
        from app.db.models import CreditAccount
        with d["factory"]() as session:
            assert session.get(CreditAccount, d["account"]).spent_micro == 10_000
        assert ("PATCH", path + "/icp") in requests
        assert ("POST", path + "/send") in requests
        # Reload adopts saved Results instead of resubmitting the approved batch.
        page.reload(wait_until="domcontentloaded")
        page.get_by_role("heading", name="What happened after outreach").wait_for(timeout=30_000)
        assert len(sends) == 1
        _check_email_preferences(page, base, d, client, tmp_path, viewport["width"])
        browser.close()


def test_blocked_search_opens_contact_entry_without_retry_or_polling(rendered_core, monkeypatch, tmp_path):
    from app.gtm import journey
    playwright = pytest.importorskip("playwright.sync_api")
    expect = playwright.expect
    base = os.environ["GTM_CONSOLE_URL"].rstrip("/")
    d, client, workers, sends = rendered_core
    async def blocked(who, search_queries=None, **kwargs):
        raise journey.PublicSearchBlocked()
    monkeypatch.setattr(journey, "_discover", blocked)
    with playwright.sync_playwright() as runner:
        browser = runner.firefox.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        requests = _install_forwarder(page, base, rendered_core)
        page.goto(f"{base}/gtm?workspace={d['wid']}&venture={d['vid']}", wait_until="domcontentloaded")
        _start_discovery(page, rendered_core, "Independent bakery owners in Bengaluru")
        expect(page.get_by_role("button", name="Add a contact you know", exact=True)).to_be_visible(timeout=30_000)
        expect(page.get_by_role("button", name="Retry search", exact=True)).to_have_count(0)
        expect(page.locator('section[data-status="error"]')).to_have_count(0)
        reads = sum(method == "GET" and path.endswith("/gtm/journey") for method, path in requests)
        page.wait_for_timeout(6200)
        assert sum(method == "GET" and path.endswith("/gtm/journey") for method, path in requests) == reads
        page.get_by_role("button", name="Edit searches", exact=True).click()
        page.get_by_label("Search queries", exact=True).fill("bakeries Pune\nbakery phone orders")
        expect(page.get_by_role("button", name="Save searches and search", exact=True)).to_be_enabled()
        page.get_by_role("button", name="Add a contact you know", exact=True).click()
        expect(page.get_by_role("button", name="Add contact and prepare draft", exact=True)).to_be_visible()
        _screenshot(page, tmp_path, "gtm-blocked-contact-entry-1280.png")
        assert sends == []
        browser.close()


def test_missing_mailbox_and_failed_load_retry_are_actionable(rendered_core, tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    base = os.environ["GTM_CONSOLE_URL"].rstrip("/")
    d, client, workers, sends = rendered_core
    with playwright.sync_playwright() as runner:
        browser = runner.firefox.launch(headless=True)
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.set_default_navigation_timeout(90_000)
        # Hold the synthetic outage across ordinary automatic polling until
        # the retry control is actually reviewed and clicked.
        load_gate = [True]
        expected_rejections = []
        requests = _install_forwarder(page, base, rendered_core, load_gate=load_gate, expected_rejections=expected_rejections)
        page.goto(f"{base}/gtm?workspace={d['wid']}&venture={d['vid']}", wait_until="domcontentloaded")
        page.get_by_role("button", name="Retry loading", exact=True).wait_for(timeout=90_000)
        _skip_intro(page)
        _screenshot(page, tmp_path, "gtm-load-retry-mobile.png")
        load_gate[0] = False
        page.get_by_role("button", name="Retry loading", exact=True).click()
        page.get_by_role("heading", name="Find customers for Order board", exact=True).wait_for(timeout=30_000)
        _reach_send(page, base, rendered_core, expected_rejections)
        assert page.get_by_role("link", name="Connect Gmail", exact=True).is_visible()
        assert page.get_by_role("button", name="Send 1 reviewed email", exact=True).is_disabled()
        _screenshot(page, tmp_path, "gtm-missing-mailbox-mobile.png")
        assert sends == [] and not any(method == "POST" and path.endswith("/send") for method, path in requests)
        browser.close()


@pytest.mark.parametrize("code,message,action", [
    ("public_search_unavailable", "Public search is busy. Retry search in a moment.", "Retry search"),
    ("no_public_candidates", "No public company pages matched this audience. Refine the audience or add a contact you know.", "Search again"),
])
@pytest.mark.parametrize("viewport", [{"width": 1440, "height": 900}, {"width": 390, "height": 844}], ids=["desktop", "mobile"])
def test_failed_search_has_one_message_and_retries_the_same_audience(rendered_core, monkeypatch, tmp_path, code, message, action, viewport):
    from app.gtm import journey
    playwright = pytest.importorskip("playwright.sync_api")
    expect = playwright.expect
    base = os.environ["GTM_CONSOLE_URL"].rstrip("/")
    d, client, workers, sends = rendered_core
    searched = []
    from tests.gtm_fixtures import qualified_bakery
    async def discover(who, search_queries=None, **kwargs):
        searched.append(who)
        if len(searched) == 1:
            if code == "public_search_unavailable":
                raise RuntimeError("Synthetic provider failure, never show this text")
            return []
        return [await qualified_bakery({"organization": "Retry Source Fixture", "source_url": "https://retry-source.example/contact",
                 "why": "Synthetic source candidate after retry; permission remains unverified."}, who, **kwargs)]
    monkeypatch.setattr(journey, "_discover", discover)
    with playwright.sync_playwright() as runner:
        browser = runner.firefox.launch(headless=True)
        page = browser.new_page(viewport=viewport)
        page.set_default_navigation_timeout(90_000)
        requests = _install_forwarder(page, base, rendered_core)
        page.goto(f"{base}/gtm?workspace={d['wid']}&venture={d['vid']}", wait_until="domcontentloaded")
        audience = "Independent bakery owners managing orders by phone"
        _start_discovery(page, rendered_core, audience)
        expect(page.locator("#operations-workspace")).to_have_count(0)
        error_message = page.get_by_text(message, exact=True)
        expect(error_message).to_be_visible(timeout=30_000)
        expect(error_message).to_have_count(1)
        expect(page.get_by_role("button", name="Retry preparation", exact=True)).to_have_count(0)
        expect(page.get_by_text("Synthetic provider failure, never show this text", exact=True)).to_have_count(0)
        if code == "public_search_unavailable":
            expect(page.get_by_role("button", name="Edit audience and search", exact=True)).to_have_count(0)
        else:
            expect(page.get_by_role("button", name="Refine audience", exact=True)).to_be_visible()
        retry = page.get_by_role("button", name=action, exact=True)
        assert ("secondary" not in retry.get_attribute("class")) == (code == "public_search_unavailable")
        _screenshot(page, tmp_path, f"gtm-{code}-{viewport['width']}.png")
        with page.expect_response(lambda response: response.url.split("?")[0].endswith("/gtm/journey/prepare") and response.request.method == "POST") as retried:
            retry.click()
        assert retried.value.status == 202 and retried.value.request.post_data_json == {"retry": True}
        page.get_by_role("heading", name="1 prospect to review", exact=True).wait_for(timeout=30_000)
        expect(error_message).to_have_count(0)
        assert searched == [audience, audience]
        assert sends == [] and not any(method == "POST" and path.endswith("/send") for method, path in requests)
        browser.close()

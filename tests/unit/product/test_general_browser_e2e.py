"""Local e2e for general browser G1 against a fixture signup page."""
from __future__ import annotations

import json
import tempfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from coding_agents.owned_browser import OwnedBrowser, origin
from desktop_runtime.device_worker import DeviceWorker

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures"


class _Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(FIXTURE_DIR), **kwargs)

    def log_message(self, format, *args):  # noqa: A003
        return


@pytest.fixture()
def fixture_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host = f"http://127.0.0.1:{server.server_address[1]}"
    yield host
    server.shutdown()


def test_origin_rejects_loopback_by_default():
    with pytest.raises(PermissionError):
        origin("http://127.0.0.1:9/path")
    with pytest.raises(PermissionError):
        origin("http://localhost/path")
    with pytest.raises(PermissionError):
        origin("http://example.com")
    assert origin("https://resend.com/x") == "https://resend.com"
    assert origin("http://127.0.0.1:9/path", allow_loopback=True) == "http://127.0.0.1:9"


def test_marketing_send_button_does_not_hide_safe_signup_navigation(fixture_server):
    pytest.importorskip("playwright.sync_api")
    start = f"{fixture_server}/general_browser_marketing.html"
    browser = OwnedBrowser(
        order_id="dwo_g1_marketing", goal="Reach the signup form without submitting",
        origins=[origin(start, allow_loopback=True)],
        authorize=lambda *_: True, admit_effect=lambda *_: True,
        general_mode=True, allow_loopback_for_tests=True,
        profile_dir=tempfile.mkdtemp(prefix="supymem-g1-marketing-"),
    )
    try:
        browser.start(start)
        observation = browser.observe()
        assert observation.confirmation_pending is False
        assert browser.pause_kind is None
        assert any(control.label == "Create account" and control.operations == ["click"]
                   for control in observation.controls)
        assert all(control.label != "Sign up" for control in observation.controls)
        assert browser.last_control_summary["signup_links"] == 1
        assert browser.last_control_summary["click"] >= 1
        browser._page.get_by_role("link", name="Create account").click()
        observation = browser.observe()
        assert observation.confirmation_pending is True
        assert browser.pause_kind == "password"
    finally:
        browser.close()


def test_fill_reviewed_fields_refuses_when_admit_blocks(fixture_server):
    pytest.importorskip("playwright.sync_api")
    start = f"{fixture_server}/general_browser_signup.html"
    root = origin(start, allow_loopback=True)
    browser = OwnedBrowser(
        order_id="dwo_g1_refuse", goal="Fill nothing when refused",
        origins=[root], authorize=lambda *_: True, admit_effect=lambda *_: False,
        general_mode=True, allow_loopback_for_tests=True,
        fill_values={"name": "Ada Lovelace", "email": "ada@example.com"},
        profile_dir=tempfile.mkdtemp(prefix="supymem-g1-refuse-"),
    )
    try:
        browser.start(start)
        filled = browser.fill_reviewed_fields()
        assert filled == []
        assert browser._page.locator("#email").input_value() == ""
        assert browser._page.locator("#name").input_value() == ""
    except Exception as exc:
        if "chrome" in str(exc).lower() or "executable" in str(exc).lower():
            pytest.skip(f"owned chrome unavailable: {exc}")
        raise
    finally:
        browser.close()


def test_email_only_review_pauses_at_password_despite_unrelated_link(fixture_server):
    pytest.importorskip("playwright.sync_api")
    start = f"{fixture_server}/general_browser_signup.html"
    browser = OwnedBrowser(
        order_id="dwo_g1_email", goal="Use my work email, stop before submitting",
        origins=[origin(start, allow_loopback=True)],
        authorize=lambda *_: True, admit_effect=lambda *_: True,
        general_mode=True, allow_loopback_for_tests=True,
        fill_values={"email": "founder@example.com"},
        profile_dir=tempfile.mkdtemp(prefix="supymem-g1-email-"),
    )
    try:
        browser.start(start)
        filled = browser.fill_reviewed_fields()
        assert [item["field_kind"] for item in filled] == ["email"]
        observation = browser.observe()
        assert observation.confirmation_pending is True
        assert browser.pause_kind == "password"
        assert browser._page.locator("#email").input_value() == "founder@example.com"
        assert browser._page.locator("#name").input_value() == ""
        assert browser._page.locator("#status").inner_text() == "waiting"
    finally:
        browser.close()


def test_general_browser_full_pause_chain_without_submit(fixture_server):
    pytest.importorskip("playwright.sync_api")
    start = f"{fixture_server}/general_browser_signup.html"
    root = origin(start, allow_loopback=True)
    step_log = []

    def authorize(_order, _session):
        return True

    def admit(effect, _digest):
        assert effect.get("operation") == "type"
        assert effect["control"].get("typed") is True
        assert "value" not in effect["control"]
        assert "value" not in effect
        return True

    browser = OwnedBrowser(
        order_id="dwo_g1", goal="Fill signup without submitting",
        origins=[root], authorize=authorize, admit_effect=admit,
        general_mode=True, allow_loopback_for_tests=True,
        fill_values={"name": "Ada Lovelace", "email": "ada@example.com"},
        profile_dir=tempfile.mkdtemp(prefix="supymem-g1-e2e-"),
    )
    try:
        browser.start(start)
        filled = browser.fill_reviewed_fields()
        step_log.extend(filled)
        assert {item.get("field_kind") for item in filled} >= {"email", "name"}
        assert all(item.get("typed") is True for item in filled)
        dumped = json.dumps(filled)
        assert "Ada" not in dumped and "ada@" not in dumped

        observation = browser.observe()
        step_log.append({
            "kind": "observation", "pause_kind": browser.pause_kind,
            "founder_prompt": browser.founder_prompt,
        })
        assert observation.confirmation_pending is True
        assert browser.pause_kind == "password"
        # Founder types the password directly in the page (not via the agent).
        browser._page.fill("#password", "founder-only-secret")
        observation = browser.observe()
        step_log.append({"kind": "observation", "pause_kind": browser.pause_kind})
        assert browser.pause_kind == "terms"

        browser._page.check("#terms")
        observation = browser.observe()
        step_log.append({
            "kind": "observation", "pause_kind": browser.pause_kind,
            "founder_prompt": browser.founder_prompt,
        })
        assert browser.pause_kind == "send"
        assert observation.confirmation_pending is True
        assert browser._page.locator("#status").inner_text() == "waiting"

        with pytest.raises(Exception) as raised:
            browser._page.goto("https://example.com/", wait_until="domcontentloaded", timeout=5000)
        assert "ERR_BLOCKED_BY_CLIENT" in str(raised.value) or "blocked" in str(raised.value).lower()
        step_log.append({"kind": "origin_guard", "blocked": True})
    except Exception as exc:
        if "chrome" in str(exc).lower() or "executable" in str(exc).lower():
            pytest.skip(f"owned chrome unavailable: {exc}")
        raise
    finally:
        browser.close()

    print("G1_E2E_STEP_LOG", json.dumps(step_log))
    assert any(item.get("pause_kind") == "password" for item in step_log)
    assert any(item.get("pause_kind") == "terms" for item in step_log)
    assert any(item.get("pause_kind") == "send" for item in step_log)
    assert "founder-only-secret" not in json.dumps(step_log)
    assert "Ada" not in json.dumps(step_log)


def test_worker_records_redacted_type_steps_and_loop_limits():
    worker = DeviceWorker(type("G", (), {
        "order_id": "dwo", "venture_id": "v", "execution_id": None, "claim_attempted": False,
    })(), lambda receipt: None)
    worker._record_step(kind="effect", operation="type", label="Email", typed=True,
                        value="secret-should-not-appear")
    assert "secret-should-not-appear" not in str(worker.last["steps"])
    assert DeviceWorker.browser_start_url({
        "origins": ["https://httpbin.org"],
        "general_browser_verification": {"start_url": "https://httpbin.org/forms/post"},
    }) == "https://httpbin.org/forms/post?supymem_check=1"


def test_device_worker_constructs_owned_browser_without_loopback():
    captured = {}

    class FakeBrowser:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.session_id = "sess"
            self.pause_kind = None
            self.founder_prompt = None
            self.last_typed_label = None

        def start(self, _url):
            return None

        def fill_reviewed_fields(self):
            return []

        def observe(self):
            raise RuntimeError("stop_after_construct")

        def close(self):
            return None

    gateway = MagicMock()
    gateway.order_id = "dwo_x"
    gateway.venture_id = "ven_x"
    gateway.execution_id = "dex_x"
    gateway.claim_attempted = False
    gateway.read.return_value = {
        "state": "approved",
        "manifest": {
            "applications": ["com.google.Chrome"],
            "origins": ["https://httpbin.org"],
            "actions": ["browser.read", "browser.interact"],
            "instruction": "sign up using my work email",
            "duration_seconds": 30,
            "general_browser_verification": {
                "operation": "general_goal_v1",
                "start_url": "https://httpbin.org/forms/post",
                "allow_type": True,
                "max_steps": 2,
                "fill_values": {"email": "founder@example.com"},
            },
        },
    }
    gateway.claim.return_value = {"state": "claimed"}
    worker = DeviceWorker(gateway, lambda receipt: None)
    with patch("desktop_runtime.device_worker.OwnedBrowser", FakeBrowser), \
            patch("desktop_runtime.device_worker.OwnedProcesses") as processes:
        processes.return_value.capture.return_value = None
        processes.return_value.stop_all.return_value = None
        processes.return_value.stopped.return_value = True
        processes.return_value.receipt.return_value = {}
        worker.run_general_browser(gateway.read.return_value, gateway.read.return_value["manifest"])
    assert captured.get("allow_loopback_for_tests") in {None, False}
    assert captured.get("general_mode") is True


def test_server_cancel_stops_a_paused_owned_browser_without_ui_continue():
    from coding_agents.jev_decision import Observation

    class PausedBrowser:
        def __init__(self, **_kwargs):
            self.session_id = "sess"
            self.pause_kind = "send"
            self.founder_prompt = "Confirm and send yourself."

        def start(self, _url):
            return None

        def fill_reviewed_fields(self):
            return []

        def observe(self):
            return Observation(revision="one", application="com.google.Chrome",
                location="https://example.com/signup", goal="stop before sending", context="form",
                controls=[], permitted=True, confirmation_pending=True,
                prior_effect_uncertain=False)

        def close(self):
            return None

    gateway = MagicMock()
    gateway.order_id = "dwo_cancel"
    gateway.venture_id = "ven_cancel"
    gateway.execution_id = "dex_cancel"
    gateway.claim_attempted = True
    gateway.current.side_effect = [None, ValueError("cancelled")]
    gateway.stop_receipt.return_value = {"state": "stopped"}
    manifest = {
        "applications": ["com.google.Chrome"], "origins": ["https://example.com"],
        "actions": ["browser.read", "browser.interact"], "instruction": "stop before sending",
        "duration_seconds": 30,
        "general_browser_verification": {"operation": "general_goal_v1",
            "start_url": "https://example.com/signup", "max_steps": 2},
    }
    worker = DeviceWorker(gateway, lambda receipt: None)
    worker.continue_event.wait = lambda _timeout: False
    worker._notify_needs_you = lambda *_args: None
    worker._notify_terminal = lambda _outcome, **_kwargs: None
    with patch("desktop_runtime.device_worker.OwnedBrowser", PausedBrowser), \
            patch("desktop_runtime.device_worker.OwnedProcesses") as processes:
        processes.return_value.stopped.return_value = True
        processes.return_value.receipt.return_value = {}
        worker.run_general_browser({"state": "approved"}, manifest)
    assert worker.last["state"] == "stopped"
    assert worker.last["evidence"]["outcome"] == "cancelled_or_expired"
    assert worker.last["credits_reconciled"] is True

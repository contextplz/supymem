"""Production wiring of the persistent Supymem browser into the owned-browser worker path (headless Chrome only)."""
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import psutil
import pytest
from coding_agents.owned_browser import OwnedBrowser
from desktop_runtime.cu_ai_browser import ChromeFactory
from desktop_runtime.cu_browser.chrome import CHROME_BIN
from desktop_runtime.device_worker import DeviceWorker


def test_worker_builds_factory_with_account_id_only_when_signed_in(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPYMEM_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SUPYMEM_CU_BROWSER_MODE", raising=False)
    chrome = tmp_path / "chrome"
    chrome.write_text("")
    monkeypatch.setattr("desktop_runtime.cu_browser.chrome.CHROME_BIN", str(chrome))
    signed_in = DeviceWorker(SimpleNamespace(order_id="o", account_id="usr_abc"), lambda r: None)
    factory = signed_in.chrome_factory()
    assert isinstance(factory, ChromeFactory) and factory.account_id == "usr_abc" and factory.headless is False
    monkeypatch.setenv("SUPYMEM_CU_BROWSER_MODE", "headless")
    assert signed_in.chrome_factory().headless is True
    assert DeviceWorker(SimpleNamespace(order_id="o", account_id=None), lambda r: None).chrome_factory() is None
    monkeypatch.setattr("desktop_runtime.cu_browser.chrome.CHROME_BIN", str(tmp_path / "missing"))
    assert signed_in.chrome_factory() is None  # no Mac Chrome (e.g. Linux CI): legacy launch


def test_watch_browser_publishes_and_respects_pause():
    worker = DeviceWorker(SimpleNamespace(order_id="o"), lambda r: None)
    browser = SimpleNamespace(watch_frame=lambda: {"jpeg": b"j", "viewport": {"w": 1, "h": 2}})
    worker.watch_browser(browser, "observe")
    assert worker.watch.snapshot()["step"] == 1
    worker.watch.pause()
    worker.watch_browser(browser, "observe")
    worker.watch.resume()
    assert worker.watch.snapshot() is None  # pause discarded the frame; nothing captured while paused


def test_production_loop_reports_sign_in_and_pauses_watch_on_a_login_wall():
    from unittest.mock import MagicMock, patch
    from coding_agents.jev_decision import Observation

    class LoginWall:
        visible = True

        def __init__(self, **kwargs):
            self.session_id, self.pause_kind = "sess", "password"
            self.founder_prompt = "Type your password in the Supymem browser window, then press Continue."
            self.kwargs = kwargs

        def start(self, _url):
            return None

        def fill_reviewed_fields(self):
            return []

        def watch_frame(self):
            return {"jpeg": b"j", "viewport": None}

        def reveal(self):
            return True

        def observe(self):
            return Observation(revision="one", application="com.google.Chrome",
                location="https://example.com/login", goal="read", context="login", controls=[],
                permitted=True, confirmation_pending=True, prior_effect_uncertain=False)

        def close(self):
            return None

    gateway = MagicMock()
    gateway.order_id, gateway.venture_id, gateway.execution_id = "dwo_s", "ven_s", "dex_s"
    gateway.account_id, gateway.claim_attempted = "usr_a", True
    gateway.current.side_effect = [None, ValueError("cancelled")]
    gateway.stop_receipt.return_value = {"state": "stopped"}
    manifest = {"applications": ["com.google.Chrome"], "origins": ["https://example.com"],
                "actions": ["browser.read", "browser.interact"], "instruction": "read", "duration_seconds": 30,
                "general_browser_verification": {"operation": "general_goal_v1",
                                                 "start_url": "https://example.com/login", "max_steps": 2}}
    worker = DeviceWorker(gateway, lambda receipt: None)
    paused_during_wait = []
    worker.continue_event.wait = lambda _t: paused_during_wait.append(worker.watch.paused) or False
    worker._notify_terminal = lambda *_a, **_k: None
    with patch("desktop_runtime.device_worker.OwnedBrowser", LoginWall), \
            patch("desktop_runtime.device_worker.OwnedProcesses") as processes:
        processes.return_value.stopped.return_value = True
        processes.return_value.receipt.return_value = {}
        worker.run_general_browser({"state": "approved"}, manifest)
    assert gateway.report_needs_you.call_args.args[0] == "sign_in"
    assert paused_during_wait and all(paused_during_wait)  # no frames while the founder types
    assert worker.watch.snapshot() is None and not worker.watch.paused and worker._reveal is None


@pytest.fixture
def site():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            if "supymem" not in (self.headers.get("Cookie") or ""):
                self.send_header("Set-Cookie", "supymem=signed-in; Max-Age=3600; Path=/")
            self.end_headers()
            self.wfile.write(b"<html><body>hello</body></html>")

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def _browser(site, tmp_path, account):
    factory = ChromeFactory(tmp_path, account_id=account, profile_email="", headless=True)
    return factory, OwnedBrowser(order_id="o1", goal="read", origins=[site], authorize=lambda *_: True,
                                 admit_effect=lambda *_: True, allow_loopback_for_tests=True, chrome_factory=factory)


@pytest.mark.skipif(not os.path.exists(CHROME_BIN), reason="Google Chrome not installed")
def test_profile_reused_across_tasks_and_cleanup_kills_chrome_keeps_profile(site, tmp_path):
    pytest.importorskip("playwright.sync_api")
    factory, browser = _browser(site, tmp_path, "usr_a")
    browser.start(site + "/")
    pid = factory.session.proc.pid
    assert browser.watch_frame()["jpeg"][:2] == b"\xff\xd8"
    browser.observe()
    browser.close()
    profile = tmp_path / "profiles" / "usr_a"
    assert profile.is_dir() and not psutil.pid_exists(pid)  # Chrome killed, profile kept

    _, again = _browser(site, tmp_path, "usr_a")
    seen = []
    again._context = None
    again.start(site + "/")
    again._page.on("request", lambda r: seen.append(r.headers.get("cookie")))
    again._page.reload()
    assert "supymem=signed-in" in seen  # the task-1 cookie is sent: the profile persisted
    again.close()
    assert profile.is_dir()
    assert not (tmp_path / "profiles" / "usr_b").exists()  # accounts never share a profile

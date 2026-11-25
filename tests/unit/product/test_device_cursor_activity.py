"""Owned worker activity is real lifecycle feedback, never simulated input."""
import json
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

from coding_agents.jev_decision import Candidate, Observation
from desktop_runtime import agent_cursor, device_worker
from desktop_runtime.bridge import BridgeSession
from desktop_runtime.device_cursor_activity import DeviceCursorActivity
from desktop_runtime.device_worker import DeviceWorker


@pytest.fixture
def feed(monkeypatch):
    with tempfile.TemporaryDirectory(dir="/tmp") as directory:
        path = str(Path(directory) / "cursor.sock")
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
            server.bind(path)
            server.settimeout(0.03)
            monkeypatch.setenv(agent_cursor.SOCKET_ENV, path)
            for key in ("_sock", "_active_owner", "_latest_owner", "_published_owner", "_listener_identity"):
                monkeypatch.setattr(agent_cursor, key, None)

            def drain():
                events = []
                deadline = time.monotonic() + 0.05
                while time.monotonic() < deadline:
                    try:
                        events.append(json.loads(server.recv(2048)))
                    except TimeoutError:
                        return events
                return events

            yield drain
            if agent_cursor._sock is not None:
                agent_cursor._sock.close()


class Tracker:
    complete, observed = True, {}
    def capture(self): pass
    def stop_all(self): pass
    def stopped(self): return True
    def receipt(self): return {"processes": [], "complete": True}


def browser_order(general=False):
    manifest = {"applications": ["com.google.Chrome"], "origins": ["https://example.test"],
                "actions": ["browser.read", "browser.interact"], "duration_seconds": 60,
                "instruction": "Read private task text", "start_url": "https://example.test"}
    if general:
        manifest["general_browser_verification"] = {
            "operation": "general_goal_v1", "start_url": "https://example.test", "max_steps": 2}
    return {"state": "approved", "manifest": manifest}


class Gateway:
    order_id, venture_id, account_id = "dwo_cursor", "ven_cursor", "usr_cursor"
    execution_id, claim_attempted = None, False
    def __init__(self, saved, *, refuse=False):
        self.saved, self.refuse = saved, refuse
    def read(self): return self.saved
    def current(self): return self.saved
    def claim(self):
        if self.refuse:
            raise ValueError("claim_refused")
        self.claim_attempted, self.execution_id = True, "dex_cursor"
    def decide(self, _invocation, observation): return Candidate("blocked", None, observation.digest())
    def stop_receipt(self, *_args, **_kwargs): return {"state": "stopped"}


@pytest.mark.parametrize("general", [False, True])
@pytest.mark.parametrize("refuse", [False, True])
def test_real_browser_route_only_emits_after_claim_and_hides_on_cleanup(monkeypatch, feed, general, refuse):
    gateway = Gateway(browser_order(general), refuse=refuse)
    opened = []

    class Browser:
        session_id, pause_kind, last_control_summary = "browser-owned", None, {}
        def __init__(self, **_kwargs): pass
        def start(self, _url):
            assert gateway.claim_attempted
            opened.append(True)
        def fill_reviewed_fields(self): return []
        def observe(self):
            return Observation(revision="1", application="Chrome", location="https://example.test",
                goal="Private goal", context="Private page data", controls=[], permitted=True,
                confirmation_pending=False, prior_effect_uncertain=False)
        def verified_load(self, _observation):
            # Cursor lifecycle tests provide no current-document load proof.
            return False
        def close(self): pass

    monkeypatch.setattr(device_worker, "OwnedBrowser", Browser)
    monkeypatch.setattr(device_worker, "OwnedProcesses", Tracker)
    monkeypatch.setattr(device_worker.local_notices, "push_task_finished", lambda **_kwargs: None)
    worker = DeviceWorker(gateway, lambda _receipt: None, activity_current=lambda: True)
    worker.run()
    events = feed()
    if refuse:
        assert events == [] and opened == []
    else:
        assert opened == [True]
        assert [item["kind"] for item in events] == ["begin", "read", "read", "read", "end"]
        assert [item.get("label") for item in events if item["kind"] == "read"] == [
            "Opening page", "Reading page", "Waiting"]
        assert len({item["owner"] for item in events}) == 1
        assert all(set(item) <= {"v", "kind", "owner", "label"} for item in events)
        assert "Private" not in json.dumps(events)
        assert worker.last["task_verified"] is False


def coding_order():
    return {"state": "approved", "workspace_id": "wsp_cursor", "actor_id": "usr_cursor",
            "device_id": "dev_cursor", "task_snapshot": {"task_id": "tsk_cursor",
                "artifact_profile": "static_landing_v1", "landing_expectations": {"contract": {
                    "profile": "static_landing_v1", "product_name": "Page", "required_text": ["Page"],
                    "actions": []}}}, "manifest": {"applications": ["com.companyos.app"], "origins": [],
                "actions": ["editor.edit", "terminal.execute"], "duration_seconds": 60,
                "instruction": "Private code request", "coding_verification": {
                    "operation": "workspace_edit_v1", "provider": "codex", "workspace_slug": "private-slug",
                    "read_scope": "device_filesystem", "write_scope": "venture_workspace",
                    "command_network": "disabled", "offline_profile": "static_html_v1"}}}


@pytest.mark.parametrize("failure", [None, "preflight", "claim", "provider"])
def test_real_static_coding_route_truthful_label_and_exception_cleanup(monkeypatch, tmp_path, feed, failure):
    gateway = Gateway(coding_order(), refuse=failure == "claim")
    ran = []

    class Coding:
        provider = "codex"
        spawn_attempted = False
        def __init__(self, **_kwargs): pass
        def _workspace(self): return tmp_path
        def clear_temporary(self): pass
        def preflight(self):
            if failure == "preflight":
                raise ValueError("provider_unavailable")
        def run(self, _instruction, _deadline):
            assert gateway.claim_attempted
            ran.append(True)
            if failure == "provider":
                raise RuntimeError("private provider error")
            return {"artifact_verified": True, "task_acceptance_verified": False}

    monkeypatch.setattr(device_worker, "OwnedCoding", Coding)
    monkeypatch.setattr(device_worker, "OwnedProcesses", Tracker)
    monkeypatch.setattr(device_worker.local_notices, "push_task_finished", lambda **_kwargs: None)
    monkeypatch.setattr(device_worker.source_receipt, "capture", lambda **_kwargs: (_ for _ in ()).throw(
        ValueError("no_fixture_archive")))
    worker = DeviceWorker(gateway, lambda _receipt: None, activity_current=lambda: True)
    worker.run()
    events = feed()
    if failure in {"preflight", "claim"}:
        assert events == [] and ran == []
    else:
        assert ran == [True]
        assert [item["kind"] for item in events] == ["begin", "read", "end"]
        assert events[1]["label"] == "Working on code"
        assert all(set(item) <= {"v", "kind", "owner", "label"} for item in events)
        assert "Private" not in json.dumps(events) and "private" not in json.dumps(events)
        assert worker.last["task_verified"] is False


@pytest.mark.parametrize("state", ["attention_required", "closing", "stopped"])
def test_pause_and_terminal_retire_without_old_context_reacquisition(feed, state):
    activity = DeviceCursorActivity(lambda: True)
    activity.update("browser", "observing")
    activity.update("browser", state)
    activity.update("browser", "observing")
    activity.close()
    assert [item["kind"] for item in feed()] == ["begin", "read", "end"]


def test_founder_pause_has_no_heartbeat_and_resume_keeps_exact_visual_owner(feed):
    activity = DeviceCursorActivity(lambda: True, heartbeat_seconds=0.01)
    try:
        activity.update("browser", "observing")
        activity.update("browser", "needs_you")
        events = feed()
        assert [item.get("label") for item in events] == [None, "Reading page", "Waiting"]
        owner = events[0]["owner"]
        time.sleep(0.04)
        assert feed() == []
        activity.update("browser", "observing")
        events = feed()
        assert events and all(item["kind"] == "read" and item["owner"] == owner for item in events)
    finally:
        activity.close()
    assert [item["kind"] for item in feed()] == ["end"]


def test_paused_old_order_resume_cannot_reacquire_after_successor(feed):
    old = DeviceCursorActivity(lambda: True)
    new = DeviceCursorActivity(lambda: True)
    try:
        old.update("browser", "observing")
        old.update("browser", "needs_you")
        feed()
        new.update("coding", "running_coding")
        new_events = feed()
        old.update("browser", "observing")
        old.close()
        assert feed() == []
        assert new_events[1]["label"] == "Working on code"
    finally:
        old.close()
        new.close()


def test_cancel_from_other_thread_hides_and_cannot_rebegin(feed):
    worker = DeviceWorker(Gateway(browser_order()), lambda _receipt: None, activity_current=lambda: True)
    worker.deadline, worker._cursor_route = time.monotonic() + 60, "coding"
    worker.progress("running_coding", provider="private value")
    cancelled = threading.Thread(target=worker.cancel)
    cancelled.start()
    cancelled.join(1)
    assert not cancelled.is_alive()
    worker.progress("running_coding")
    assert [item["kind"] for item in feed()] == ["begin", "read", "end"]


def test_retirement_during_live_owner_check_cannot_start_a_delayed_preview(feed):
    entered, release = threading.Event(), threading.Event()
    def current():
        entered.set()
        release.wait(1)
        return True
    activity = DeviceCursorActivity(current)
    source = threading.Thread(target=lambda: activity.update("coding", "running_coding"))
    source.start()
    assert entered.wait(1)
    activity.close()
    release.set()
    source.join(1)
    assert not source.is_alive()
    assert feed() == []


def test_old_task_heartbeat_and_cleanup_cannot_repaint_or_hide_successor(feed):
    old = DeviceCursorActivity(lambda: True, heartbeat_seconds=0.01)
    new = DeviceCursorActivity(lambda: True)
    try:
        old.update("coding", "running_coding")
        previous = feed()[0]["owner"]
        new.update("browser", "observing")
        old.update("coding", "running_coding")
        old.close()
        time.sleep(0.03)
        events = feed()
        begin = next(index for index, item in enumerate(events) if item["kind"] == "begin")
        assert [item["kind"] for item in events[begin:]] == ["begin", "read"]
        assert all(item["owner"] != previous for item in events[begin:])
    finally:
        old.close()
        new.close()
    assert [item["kind"] for item in feed()] == ["end"]


def test_heartbeat_uses_only_live_worker_phase_and_ends_on_scope_retirement(feed):
    current = threading.Event()
    current.set()
    activity = DeviceCursorActivity(current.is_set, heartbeat_seconds=0.01)
    try:
        activity.update("coding", "running_coding")
        time.sleep(0.04)
        current.clear()
        time.sleep(0.04)
        events = feed()
        assert events[0]["kind"] == "begin" and events[-1]["kind"] == "end"
        reads = [item for item in events if item["kind"] == "read"]
        assert len(reads) >= 2
        assert all(item["label"] == "Working on code" for item in reads)
        assert all(set(item) <= {"v", "kind", "owner", "label"} for item in events)
        activity.update("coding", "running_coding")
        assert feed() == []
    finally:
        activity.close()


def test_no_live_owner_or_unknown_state_cannot_emit(feed):
    activity = DeviceCursorActivity(lambda: False)
    activity.update("coding", "running_coding")
    activity.close()
    unknown = DeviceCursorActivity(lambda: True)
    unknown.update("private task data", "reading private document")
    unknown.close()
    assert feed() == []


def test_default_worker_and_expired_owner_have_no_visual_authority(feed):
    worker = DeviceWorker(Gateway(browser_order()), lambda _receipt: None)
    worker.deadline, worker._cursor_route = time.monotonic() + 60, "coding"
    worker.progress("running_coding")
    worker._cursor_activity.close()
    expired = DeviceWorker(Gateway(browser_order()), lambda _receipt: None, activity_current=lambda: True)
    expired.deadline, expired._cursor_route = time.monotonic() - 1, "coding"
    expired.progress("running_coding")
    expired._cursor_activity.close()
    assert feed() == []


def test_worker_finally_hides_preview_after_uncaught_executor_exception(monkeypatch, feed):
    worker = DeviceWorker(Gateway(browser_order()), lambda _receipt: None, activity_current=lambda: True)
    def raises_after_start():
        worker.gateway.claim()
        worker.deadline, worker._cursor_route = time.monotonic() + 60, "browser"
        worker.progress("observing")
        raise RuntimeError("private executor error")
    monkeypatch.setattr(worker, "_run", raises_after_start)
    monkeypatch.setattr(device_worker, "report_error", lambda *_args, **_kwargs: None)
    with pytest.raises(RuntimeError, match="private executor error"):
        worker.run()
    assert [item["kind"] for item in feed()] == ["begin", "read", "end"]


def test_worker_ai_route_does_not_nest_or_compete_with_engine_visual_lease(monkeypatch, feed):
    saved = browser_order()
    saved["manifest"]["desktop_verification"] = {"operation": "desktop_goal_v1"}
    worker = DeviceWorker(Gateway(saved), lambda _receipt: None, activity_current=lambda: True)
    def engine(_saved, _manifest):
        assert worker._cursor_route is None
        with agent_cursor.execution():
            agent_cursor.emit("read", label="Reading screen")
            worker.progress("observing")
    monkeypatch.setattr(worker, "run_ai_loop", engine)
    monkeypatch.setenv("SUPYMEM_CU_ENGINE", "ai_loop")
    worker.run()
    events = feed()
    assert [item["kind"] for item in events] == ["begin", "read", "end"]
    assert len({item["owner"] for item in events}) == 1


def test_bridge_visual_predicate_is_local_nonblocking_and_retired_on_clear():
    session = BridgeSession("https://gateway.example", account_id="usr_a", device_id="dev_a", connected=True)
    scope = (session.gateway, "usr_a", "dev_a")
    assert session.activity_scope_current(scope)
    assert not session.activity_scope_current((session.gateway, "usr_b", "dev_a"))
    entered, release = threading.Event(), threading.Event()
    def busy():
        with session.lock:
            entered.set()
            release.wait(1)
    holder = threading.Thread(target=busy)
    holder.start()
    assert entered.wait(1)
    try:
        assert session.activity_scope_current(scope) is None
    finally:
        release.set()
        holder.join(1)
    session.clear()
    assert not session.activity_scope_current(scope)


def test_bridge_clear_retires_visual_immediately_and_same_account_rebind_cannot_revive(feed):
    session = BridgeSession("https://gateway.example", account_id="usr_a", device_id="dev_a", connected=True)
    scope = (session.gateway, "usr_a", "dev_a")
    activity = DeviceCursorActivity(lambda: session.activity_scope_current(scope))
    session.activity_retired = activity.close
    activity.update("coding", "running_coding")
    session.clear()
    session.account_id, session.device_id, session.connected = "usr_a", "dev_a", True
    activity.update("coding", "running_coding")
    assert [item["kind"] for item in feed()] == ["begin", "read", "end"]


def test_busy_bridge_lock_suppresses_only_current_tick_then_live_heartbeat_recovers(feed):
    session = BridgeSession("https://gateway.example", account_id="usr_a", device_id="dev_a", connected=True)
    scope = (session.gateway, "usr_a", "dev_a")
    activity = DeviceCursorActivity(lambda: session.activity_scope_current(scope), heartbeat_seconds=0.01)
    entered, release = threading.Event(), threading.Event()
    def busy():
        with session.lock:
            entered.set()
            release.wait(1)
    holder = threading.Thread(target=busy)
    try:
        activity.update("coding", "running_coding")
        feed()
        holder.start()
        assert entered.wait(1)
        activity.update("coding", "running_coding")
        time.sleep(0.03)
        assert feed() == []
        release.set()
        holder.join(1)
        time.sleep(0.03)
        recovered = feed()
        assert recovered and all(item["kind"] == "read" for item in recovered)
        assert all(item["label"] == "Working on code" for item in recovered)
    finally:
        release.set()
        holder.join(1)
        activity.close()


def test_initial_busy_owner_delays_preview_until_live_admission_without_another_progress(feed):
    current = {"value": None}
    activity = DeviceCursorActivity(lambda: current["value"], heartbeat_seconds=0.01)
    try:
        activity.update("coding", "running_coding")
        assert feed() == []
        current["value"] = True
        time.sleep(0.03)
        events = feed()
        assert events[0]["kind"] == "begin"
        assert all(item["kind"] == "read" and item["label"] == "Working on code" for item in events[1:])
    finally:
        activity.close()

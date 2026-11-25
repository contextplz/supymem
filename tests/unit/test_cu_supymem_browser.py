"""BF-2/3/4: persistent Supymem browser profile, sign-in takeover, watch feed. No Chrome, GUI or network."""
import os
import stat
import threading
from types import SimpleNamespace

import pytest
from coding_agents.cu_ai_loop import HUMAN_PROMPT, human_gate
from desktop_runtime import cu_browser
from desktop_runtime.cu_ai_browser import ChromeFactory
from desktop_runtime.cu_ai_engine import Observation, run_loop
from desktop_runtime.cu_browser import chrome
from desktop_runtime.cu_watch import MAX_FRAME_B64, WatchFeed


def _fake_chrome(monkeypatch, *, cookie="session=abc"):
    """Replace Chrome with a recorder that writes a cookie into the profile like a real run would."""
    runs, copies = [], []

    class Session:
        def __init__(self, profile, *, headless=True, **kw):
            self.profile, self.headless, self.revealed = profile, headless, 0
            runs.append(self)

        def start(self):
            (self.profile / "Cookies").write_text(cookie)
            return "http://127.0.0.1:9333"

        def stop(self):
            pass

        def reveal(self):
            self.revealed += 1
            return True

    monkeypatch.setattr(cu_browser, "ChromeSession", Session)
    monkeypatch.setattr(cu_browser, "BrowserBackend", lambda *a, **kw: SimpleNamespace(closed=False))
    monkeypatch.setattr(cu_browser, "prepare_profile_copy", lambda email, dest: copies.append((email, dest)))
    return runs, copies


def test_second_run_reuses_persistent_profile_and_cookies_survive(monkeypatch, tmp_path):
    runs, copies = _fake_chrome(monkeypatch)
    first = ChromeFactory(tmp_path, profile_email="a@example.test", account_id="usr_1")
    first()
    first.stop()
    profile = runs[0].profile
    assert profile == tmp_path / "profiles" / "usr_1" and (profile / "Cookies").read_text() == "session=abc"
    assert stat.S_IMODE(os.stat(profile).st_mode) == 0o700 and stat.S_IMODE(os.stat(profile.parent).st_mode) == 0o700
    (profile / "SingletonLock").symlink_to("host-999999999")  # crashed prior run: stale lock must not block reuse
    second = ChromeFactory(tmp_path, profile_email="a@example.test", account_id="usr_1")
    second()
    second.stop()
    assert runs[1].profile == profile and profile.exists()      # stop() never deletes the persistent profile
    assert (profile / "Cookies").read_text() == "session=abc"
    assert len(copies) == 1                                      # seeded once, never re-copied over later sign-ins
    assert not (profile / "SingletonLock").is_symlink()


def test_live_profile_owner_is_refused_and_bad_account_rejected(monkeypatch, tmp_path):
    _fake_chrome(monkeypatch)
    profile = chrome.persistent_profile_dir(tmp_path, "usr_1")
    (profile / "SingletonLock").symlink_to(f"host-{os.getpid()}")
    with pytest.raises(RuntimeError, match="browser_profile_in_use"):
        ChromeFactory(tmp_path, account_id="usr_1")()
    with pytest.raises(ValueError):
        chrome.persistent_profile_dir(tmp_path, "../escape")


def test_seed_copy_never_includes_saved_passwords():
    assert not any("Login Data" in item for item in chrome.COPY_ITEMS)


def test_mode_policy_default_headless_unchanged_and_visible_for_accounts(monkeypatch, tmp_path):
    runs, _ = _fake_chrome(monkeypatch)
    monkeypatch.delenv("SUPYMEM_CU_BROWSER_MODE", raising=False)
    monkeypatch.setenv("SUPYMEM_CU_CHROME_PROFILE_EMAIL", "")
    plain = ChromeFactory(tmp_path)
    plain()
    assert runs[0].headless is True and runs[0].profile.name.startswith("chrome_profile_")
    plain.stop()
    assert not runs[0].profile.exists()                          # suite path: fresh tmp profile, deleted
    visible = ChromeFactory(tmp_path, account_id="usr_1")
    visible()
    assert runs[1].headless is False
    visible.stop()
    monkeypatch.setenv("SUPYMEM_CU_BROWSER_MODE", "headless")
    forced = ChromeFactory(tmp_path, account_id="usr_1")
    forced()
    assert runs[2].headless is True and forced.reveal() is False  # headless has no window to raise
    forced.stop()


def test_launch_never_activates_or_raises_the_window(monkeypatch, tmp_path):
    spawned = []
    monkeypatch.setattr(chrome.subprocess, "Popen", lambda args, **kw: spawned.append(args) or SimpleNamespace(
        pid=4242, poll=lambda: None))
    monkeypatch.setattr(chrome.subprocess, "run", lambda *a, **k: pytest.fail("no osascript/open/activate"))
    session = chrome.ChromeSession(tmp_path / "p", headless=False, port=9333)
    monkeypatch.setattr(session, "_listener_alive", lambda: False)
    monkeypatch.setattr(session, "_read_version", lambda t: {
        "webSocketDebuggerUrl": "ws://127.0.0.1:9333/devtools/browser/x"})
    monkeypatch.setattr(session, "reveal", lambda: pytest.fail("the harness must never raise the window"))
    session.start()
    args = spawned[0]
    assert "--start-minimized" in args and not any(a.startswith("--headless") for a in args)
    assert args[0] == chrome.CHROME_BIN and "open" not in args[0].split("/")[-1:]


# --- BF-3: login wall -> needs_you(sign_in) ---------------------------------------------------------

class Mac:
    def __init__(self, screens):
        self.screens, self.observed, self.acted = screens, 0, []

    def observe(self):
        title, elements = self.screens[min(self.observed, len(self.screens) - 1)]
        self.observed += 1
        return Observation(b"", elements, "Chrome (browser)", title, "", False, None)

    def frontmost(self):
        return ("Chrome (browser)", 1)

    def focused_role(self):
        return "AXTextField"

    def activate(self, name):
        pytest.fail("the harness never activates")

    def act(self, action, obs):
        self.acted.append(action.op)
        return "ok"


def _act(op, **kw):
    return {"op": op, "element_id": "", "x": 0, "y": 0, "text": "", "chord": "", "direction": "",
            "amount": 0, "app": "", "ms": 0, "summary": "", **kw}


def _reply(*actions):
    return {"thought": "t", "memory": "", "subgoal_done": False, "actions": list(actions)}


def test_login_wall_detection_uses_password_field_or_sign_in_url_not_header_links():
    assert human_gate([{"type": "password", "label": "Password"}], "https://x.test/ | Home") == "sign_in"
    assert human_gate([], "https://accounts.google.com/v3/signin/identifier?x=1 | Sign in") == "sign_in"
    assert human_gate([], "https://x.test/login?next=/a | Log in") == "sign_in"
    assert human_gate([{"label": "Sign in"}], "https://x.test/ | Home") is None
    assert human_gate([], "https://x.test/blog/login-tips | Tips") is None


def test_login_wall_pauses_at_once_with_fixed_prompt_then_resumes_and_reobserves():
    wall = [{"id": "b1", "ax_role": "input", "type": "password", "label": "Password", "value": "hunter2-secret"}]
    mac = Mac([("https://x.test/login | Log in", wall), ("https://x.test/home | Home", [])])
    pauses, rows = [], []
    replies = iter([_reply(_act("done", summary="finished"))])

    def pause(kind, prompt, obs):
        pauses.append((kind, prompt, mac.observed))
        return "continue"
    import desktop_runtime.cu_ai_engine as eng
    eng.time.sleep = lambda s: None
    result = run_loop("read my inbox", lambda p: (next(replies), {}), mac, threading.Event(), pause=pause,
                      log=rows.append, verify=lambda *a: (True, ""))
    assert [p[0] for p in pauses] == ["sign_in"]                  # no "try alternatives first" detour
    assert pauses[0][1] == HUMAN_PROMPT["sign_in"] and "Supymem browser" in pauses[0][1]
    assert "hunter2" not in repr(rows) and "hunter2" not in repr(pauses)
    assert mac.observed >= 2 and result.outcome == "done"          # re-observed after Continue


def test_stop_during_a_step_ends_before_the_next_model_call():
    stop, mac, calls = threading.Event(), Mac([("https://x.test/ | Home", [])]), []
    mac.act = lambda action, obs: (stop.set(), "ok")[1]

    def decide(payload):
        calls.append(1)
        return _reply(_act("key", chord="tab")), {}
    import desktop_runtime.cu_ai_engine as eng
    eng.time.sleep = lambda s: None
    result = run_loop("read", decide, mac, stop, verify=lambda *a: (True, ""))
    assert result.outcome == "stopped" and len(calls) == 1


# --- BF-4: watch feed ----------------------------------------------------------------------------------

def test_watch_feed_publishes_latest_frame_and_is_blank_while_paused():
    feed = WatchFeed()
    assert feed.snapshot() is None
    assert feed.publish(b"\xff\xd8jpeg", step=1, label="Clicking", cursor={"x": 5, "y": 6}, viewport={"w": 10, "h": 10})
    first = feed.snapshot()
    assert first["step"] == 1 and first["cursor"] == {"x": 5, "y": 6} and first["jpeg_b64"]
    feed.pause()                                                   # Needs-you card open: nothing shown or captured
    assert feed.snapshot() is None and feed.publish(b"x", step=2) is False
    feed.resume()
    assert feed.snapshot() is None                                 # old frame was discarded, not replayed
    assert feed.publish(b"y", step=3) and feed.snapshot()["seq"] == 2


def test_watch_feed_drops_frames_too_big_for_the_tauri_status_reply():
    assert WatchFeed().publish(b"x" * MAX_FRAME_B64, step=1) is False


def test_status_route_merges_watch_and_cancel_clears_it():
    from desktop_runtime.device_worker import DeviceWorker
    worker = DeviceWorker(SimpleNamespace(order_id="o1"), lambda last: None)
    worker.watch.publish(b"jpeg", step=1)
    assert worker.watch.snapshot()["step"] == 1
    worker.cancel()
    assert worker.watch.snapshot() is None and worker.cancelled.is_set()

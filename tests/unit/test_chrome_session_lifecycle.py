"""Owned process lifecycle regressions; no Chrome, CDP or GUI is launched."""
import errno
import json
import signal
import itertools
import subprocess
from types import SimpleNamespace

import pytest
from desktop_runtime.cu_browser import chrome


class Child:
    pid = 123456
    def __init__(self):
        self.exited = False
        self.waits = []
    def poll(self):
        return 0 if self.exited else None
    def wait(self, timeout):
        self.waits.append(timeout)
        if not self.exited:
            raise subprocess.TimeoutExpired("owned", timeout)


def harness(monkeypatch, tmp_path, *, headless=True):
    child, signals, spawned = Child(), [], []
    session = chrome.ChromeSession(tmp_path / "private", headless=headless, port=9333)
    monkeypatch.setattr(chrome.subprocess, "Popen", lambda args, **kwargs: spawned.append((args, kwargs)) or child)
    monkeypatch.setattr(chrome.subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no global kill")))
    monkeypatch.setattr(session, "_listener_alive", lambda: False)
    def killpg(group, sig):
        assert group == child.pid
        if child.exited:
            raise ProcessLookupError
        signals.append(sig)
        if sig == signal.SIGTERM:
            child.exited = True
    monkeypatch.setattr(chrome.os, "killpg", killpg)
    monkeypatch.setattr(session, "_read_version", lambda timeout: {
        "webSocketDebuggerUrl": "ws://127.0.0.1:9333/devtools/browser/owned-instance"})
    monkeypatch.setattr(chrome.time, "sleep", lambda _: None)
    return session, child, signals, spawned


@pytest.mark.parametrize("headless", [False, True])
def test_both_modes_launch_direct_owned_new_session_and_stop_only_that_group(monkeypatch, tmp_path, headless):
    session, child, signals, spawned = harness(monkeypatch, tmp_path, headless=headless)
    assert session.start() == session.endpoint
    assert spawned[0][0][0] == chrome.CHROME_BIN
    assert spawned[0][1]["start_new_session"] is True
    assert spawned[0][1]["umask"] == 0o077
    session.stop()
    session.stop()
    assert child.exited and signal.SIGTERM in signals
    assert child.waits == []  # poll reaps an already exited direct child


def test_unstarted_or_borrowed_session_never_signals_process_or_listener(monkeypatch, tmp_path):
    session = chrome.ChromeSession(tmp_path, port=9333)
    session.proc = Child()  # Merely knowing a process/port is not ownership.
    monkeypatch.setattr(chrome.os, "killpg", lambda *a: (_ for _ in ()).throw(AssertionError("borrowed")))
    monkeypatch.setattr(chrome.subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("borrowed")))
    session.stop()
    assert not session.proc.exited


def test_existing_listener_prevents_launch_without_revoke(monkeypatch, tmp_path):
    session, _, signals, spawned = harness(monkeypatch, tmp_path)
    monkeypatch.setattr(session, "_listener_alive", lambda: True)
    with pytest.raises(RuntimeError, match="browser_endpoint_in_use"):
        session.start()
    assert not spawned and not signals


def test_unknown_endpoint_identity_is_not_startup_success(monkeypatch, tmp_path):
    session, child, _, _ = harness(monkeypatch, tmp_path)
    monkeypatch.setattr(session, "_read_version", lambda timeout: {"webSocketDebuggerUrl": "ws://elsewhere:9333/private"})
    ticks = itertools.chain([0, 0, 9, 10, 11], itertools.repeat(12))
    monkeypatch.setattr(chrome.time, "monotonic", lambda: next(ticks))
    with pytest.raises(RuntimeError, match="browser_start_timeout"):
        session.start(timeout=1)
    assert child.exited


def test_parent_exit_does_not_prove_owned_descendants_dead(monkeypatch, tmp_path):
    session, child, signals, _ = harness(monkeypatch, tmp_path)
    session.start()
    child.exited = True
    alive = [True]
    def killpg(group, sig):
        assert group == child.pid
        if not alive[0]:
            raise ProcessLookupError
        signals.append(sig)
        if sig == signal.SIGKILL:
            alive[0] = False
    monkeypatch.setattr(chrome.os, "killpg", killpg)
    ticks = iter(range(100))
    monkeypatch.setattr(chrome.time, "monotonic", lambda: next(ticks))
    session.stop()
    assert signal.SIGKILL in signals


def test_surviving_group_or_detached_listener_keeps_handles_and_refuses_success(monkeypatch, tmp_path):
    session, child, _, _ = harness(monkeypatch, tmp_path)
    session.start()
    child.exited = True
    monkeypatch.setattr(chrome.os, "killpg", lambda *a: None)
    ticks = iter(range(100))
    monkeypatch.setattr(chrome.time, "monotonic", lambda: next(ticks))
    with pytest.raises(RuntimeError, match="browser_close_unconfirmed"):
        session.stop()
    assert session.proc is child and not session.closed
    monkeypatch.setattr(chrome.os, "killpg", lambda *a: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(session, "_listener_alive", lambda: True)
    with pytest.raises(RuntimeError, match="browser_close_unconfirmed"):
        session.stop()
    assert session.proc is child and not session.closed


def test_listener_probe_unknown_is_not_absence(monkeypatch, tmp_path):
    session = chrome.ChromeSession(tmp_path, port=9333)
    monkeypatch.setattr(chrome.socket, "create_connection", lambda *a, **kw: (_ for _ in ()).throw(OSError(errno.ECONNREFUSED, "refused")))
    assert session._listener_alive() is False
    monkeypatch.setattr(chrome.socket, "create_connection", lambda *a, **kw: (_ for _ in ()).throw(TimeoutError("private")))
    with pytest.raises(RuntimeError, match="browser_listener_unknown") as error:
        session._listener_alive()
    assert "private" not in str(error.value)


def test_cdp_read_is_bounded_and_does_not_use_environment_proxy(monkeypatch, tmp_path):
    session = chrome.ChromeSession(tmp_path, port=9333)
    reads = []
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, size):
            reads.append(size)
            return json.dumps({"webSocketDebuggerUrl": "ws://127.0.0.1:9333/devtools/browser/owned"}).encode()
    monkeypatch.setattr(chrome.urllib.request, "build_opener", lambda proxy: SimpleNamespace(open=lambda url, timeout: Response()))
    assert session._read_version(.1)["webSocketDebuggerUrl"]
    assert reads == [65537]


@pytest.mark.parametrize("failure", ["profile", "backend"])
def test_factory_startup_failure_cleans_fresh_profile_and_owned_session(monkeypatch, tmp_path, failure):
    import desktop_runtime.cu_browser as exports
    from desktop_runtime.cu_ai_browser import ChromeFactory
    closed = []
    class Owned:
        def __init__(self, *args, **kw):
            pass
        def start(self):
            return "http://127.0.0.1:9333"
        def stop(self):
            closed.append(True)
    monkeypatch.setattr(exports, "ChromeSession", Owned)
    monkeypatch.setattr(exports, "BrowserBackend", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("backend")))
    monkeypatch.setenv("SUPYMEM_CU_CHROME_PROFILE_EMAIL", "synthetic@example.test")
    def copy(*a):
        if failure == "profile":
            raise OSError("copy")
    monkeypatch.setattr(exports, "prepare_profile_copy", copy)
    factory = ChromeFactory(tmp_path)
    with pytest.raises((OSError, RuntimeError), match="copy|backend"):
        factory()
    assert factory.session is None and factory._owned_profile is None
    assert not list(tmp_path.glob("chrome_profile_*"))
    assert closed == ([] if failure == "profile" else [True])


def test_factory_retains_profile_and_session_when_owned_stop_unconfirmed(monkeypatch, tmp_path):
    from desktop_runtime.cu_ai_browser import ChromeFactory
    factory = ChromeFactory(tmp_path)
    factory._owned_profile = tmp_path / "fresh-profile"
    factory._owned_profile.mkdir()
    (factory._owned_profile / "synthetic-cookie").write_text("preserve until death proved")
    factory.session = SimpleNamespace(stop=lambda: (_ for _ in ()).throw(RuntimeError("browser_close_unconfirmed")))
    with pytest.raises(RuntimeError, match="browser_close_unconfirmed"):
        factory.stop()
    assert factory.session is not None and factory._owned_profile.exists()
    with pytest.raises(RuntimeError, match="browser_factory_session_active"):
        factory()


def test_missing_group_at_signal_retires_identity_without_reprobe(monkeypatch, tmp_path):
    session, child, _, _ = harness(monkeypatch, tmp_path)
    session.start()
    child.exited = True
    calls = []
    def absent(group, sig):
        calls.append(sig)
        if len(calls) > 1:
            raise AssertionError("retired group may have been reused")
        raise ProcessLookupError
    monkeypatch.setattr(chrome.os, "killpg", absent)
    session.stop()
    assert session.closed and session._group_gone
    assert calls == [signal.SIGTERM]


@pytest.mark.parametrize("stage", ["signal", "wait", "listener"])
def test_cleanup_failure_keeps_content_free_exact_stage(monkeypatch, tmp_path, stage):
    from desktop_runtime.cu_cleanup import cleanup_trace
    session, child, _, _ = harness(monkeypatch, tmp_path)
    session.start()
    def failure(*args):
        raise PermissionError(errno.EPERM, "private-never-echo")
    expected = {"signal": "group_signal", "wait": "group_wait", "listener": "listener_probe"}[stage]
    if stage == "signal":
        monkeypatch.setattr(session, "_signal_group", failure)
    elif stage == "wait":
        monkeypatch.setattr(session, "_wait_group", failure)
    else:
        monkeypatch.setattr(session, "_listener_alive", failure)
    with pytest.raises(RuntimeError, match="browser_close_unconfirmed") as caught:
        session.stop()
    trace = cleanup_trace(caught.value)
    assert trace[-1]["stage"] == expected
    assert trace[-1]["error_type"] == "PermissionError" and trace[-1]["error_errno"] == "EPERM"
    assert "private-never-echo" not in json.dumps(trace)
    assert not session.closed


@pytest.mark.parametrize('state', ['empty', 'nonempty', 'unknown', 'live_parent', 'borrowed_parent', 'listener_present', 'listener_unknown'])
def test_eperm_retirement_requires_complete_owned_empty_group_and_listener_absence(monkeypatch, tmp_path, state):
    session, child, signals, _ = harness(monkeypatch, tmp_path)
    session.start()
    child.exited = state != 'live_parent'
    if state == 'borrowed_parent':
        replacement = Child()
        replacement.exited = True
        session.proc = replacement
    probes = []
    def denied(group, sig):
        probes.append((group, sig))
        if sig == 0:
            raise PermissionError(errno.EPERM, 'synthetic permission probe')
        raise AssertionError('no signal allowed during independent proof')
    monkeypatch.setattr(chrome.os, 'killpg', denied)
    def inventory(group):
        assert group == child.pid
        if state == 'unknown':
            raise RuntimeError('synthetic kernel unknown')
        return (321,) if state == 'nonempty' else ()
    monkeypatch.setattr(chrome, 'darwin_group_members', inventory)
    if state == 'listener_present':
        monkeypatch.setattr(session, '_listener_alive', lambda: True)
    elif state == 'listener_unknown':
        monkeypatch.setattr(session, '_listener_alive', lambda: (_ for _ in ()).throw(RuntimeError('synthetic listener unknown')))
    if state == 'empty':
        assert session._cleanup_stage('group_wait', session._wait_group, 2) is True
        assert session._group_gone is True
        before = list(probes)
        session.stop()
        session._signal_group(signal.SIGKILL)
        assert probes == before and session.closed is True
        assert session.cleanup_trace[0]['group_probe_reason'] == 'kernel_group_empty'
    else:
        with pytest.raises((PermissionError, RuntimeError)):
            session._cleanup_stage('group_wait', session._wait_group, 2)
        assert session._group_gone is False and not session.closed
        assert session.cleanup_trace[-1]['group_probe_reason'] != 'kernel_group_empty'
    assert all(sig == 0 for _, sig in probes)


@pytest.mark.parametrize('case', ['empty', 'member', 'errno', 'negative_length', 'full_buffer', 'misaligned', 'invalid_pid', 'unsupported_platform', 'invalid_group'])
def test_packaged_kernel_inventory_never_treats_unknown_or_partial_as_empty(monkeypatch, case):
    import ctypes
    import sys
    from desktop_runtime.cu_cleanup import darwin_group_members
    monkeypatch.setattr(sys, 'platform', 'linux' if case == 'unsupported_platform' else 'darwin')
    class Query:
        def __call__(self, mode, group, buffer, size):
            assert mode == 2 and group == 123
            if case == 'errno':
                ctypes.set_errno(errno.EPERM)
            if case in {'member', 'invalid_pid'}:
                buffer[0] = 321 if case == 'member' else -2
            return {'empty': 0, 'member': 4, 'errno': 0, 'negative_length': -1,
                    'full_buffer': size, 'misaligned': 3, 'invalid_pid': 4}.get(case, 0)
    monkeypatch.setattr(ctypes, 'CDLL', lambda *args, **kwargs: SimpleNamespace(proc_listpids=Query()))
    if case in {'empty', 'member'}:
        assert darwin_group_members(123) == (() if case == 'empty' else (321,))
    else:
        with pytest.raises(RuntimeError, match='group_inventory_unavailable'):
            darwin_group_members(1 if case == 'invalid_group' else 123)


def test_actual_packaged_darwin_inventory_tracks_only_owned_child_then_empty():
    import sys
    from desktop_runtime.cu_cleanup import darwin_group_members
    if sys.platform != 'darwin':
        pytest.skip('Darwin-specific kernel inventory contract')
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(2)'], start_new_session=True)
    try:
        assert child.pid in darwin_group_members(child.pid)
    finally:
        child.terminate()
        child.wait(timeout=5)
    assert darwin_group_members(child.pid) == ()


def test_eperm_transient_membership_wait_uses_original_budget_without_further_signals(monkeypatch, tmp_path):
    session, child, _, _ = harness(monkeypatch, tmp_path)
    session.start()
    child.exited = True
    probes = []
    def denied(group, sig):
        probes.append((group, sig))
        if sig != 0:
            raise AssertionError('EPERM group must never receive another signal')
        raise PermissionError(errno.EPERM, 'synthetic probe')
    monkeypatch.setattr(chrome.os, 'killpg', denied)
    observations = iter([(child.pid + 1,), ()])
    monkeypatch.setattr(chrome, 'darwin_group_members', lambda group: next(observations))
    assert session._wait_group(2) is True
    assert session._group_gone and session._group_permission_unconfirmed
    session._signal_group(signal.SIGKILL)
    session.stop()
    assert probes == [(child.pid, 0)] and session.closed


def test_signal_eperm_quarantines_retries_until_independent_owned_retirement(monkeypatch, tmp_path):
    session, child, _, _ = harness(monkeypatch, tmp_path)
    session.start()
    calls = []
    def denied(group, sig):
        calls.append((group, sig))
        raise PermissionError(errno.EPERM, 'synthetic denied TERM')
    monkeypatch.setattr(chrome.os, 'killpg', denied)
    monkeypatch.setattr(chrome, 'darwin_group_members', lambda group: ())
    with pytest.raises(RuntimeError, match='browser_close_unconfirmed'):
        session.stop()
    assert session._group_permission_unconfirmed and not session._group_gone
    assert session.cleanup_trace[-1]['stage'] == 'group_signal'
    assert session.cleanup_trace[-1]['error_errno'] == 'EPERM'
    with pytest.raises(RuntimeError, match='browser_close_unconfirmed'):
        session.stop()  # Live original child cannot be retired despite empty stub inventory.
    assert calls == [(child.pid, signal.SIGTERM)] and not session.closed
    child.exited = True
    monkeypatch.setattr(chrome, 'darwin_group_members', lambda group: (_ for _ in ()).throw(RuntimeError('unknown inventory')))
    with pytest.raises(RuntimeError, match='browser_close_unconfirmed'):
        session.stop()
    assert calls == [(child.pid, signal.SIGTERM)] and not session._group_gone
    monkeypatch.setattr(chrome, 'darwin_group_members', lambda group: ())
    session.stop()
    session._signal_group(signal.SIGKILL)
    assert calls == [(child.pid, signal.SIGTERM)]
    assert session.closed and session._group_gone and session._listener_alive() is False

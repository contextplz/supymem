"""No GUI or driver is launched: exercise direct-child ownership and bounded waits."""
import importlib.util
import subprocess
from pathlib import Path

import pytest


def module():
    path = Path(__file__).resolve().parents[2] / "scripts/desktop/cu_trial_daemon.py"
    spec = importlib.util.spec_from_file_location("trial_daemon_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Child:
    def __init__(self, *, exited=False, stubborn=False, fail=False):
        self.pid = 12345
        self.exited, self.stubborn, self.fail = exited, stubborn, fail
        self.calls = []

    def poll(self):
        return 0 if self.exited else None

    def terminate(self):
        self.calls.append("terminate")

    def kill(self):
        self.calls.append("kill")

    def wait(self, timeout):
        self.calls.append(("wait", timeout))
        if self.fail or (self.stubborn and "kill" not in self.calls):
            raise subprocess.TimeoutExpired("owned-child", timeout)
        self.exited = True


def setup_start(mod, monkeypatch, tmp_path, child):
    monkeypatch.setattr(mod.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(mod.os, "getsid", lambda pid: pid)
    calls = []
    counter = iter(range(10))
    def mkdir(**kwargs):
        assert kwargs == {"prefix": "supymem-cua-qa-", "dir": "/private/tmp"}
        path = tmp_path / str(next(counter))
        path.mkdir()
        return str(path)
    monkeypatch.setattr(mod.tempfile, "mkdtemp", mkdir)
    monkeypatch.setattr(mod.subprocess, "Popen", lambda args, **kwargs: calls.append((args, kwargs)) or child)
    monkeypatch.setattr(mod.time, "sleep", lambda _: None)
    def group_signal(group, sig):
        assert group == child.pid
        if child.exited:
            raise ProcessLookupError
        if sig:
            child.exited = True
    monkeypatch.setattr(mod.os, "killpg", group_signal)
    return calls


def test_cold_daemon_uses_unique_private_socket_no_overlay_and_retains_child(tmp_path, monkeypatch):
    mod, child = module(), Child()
    calls = setup_start(mod, monkeypatch, tmp_path, child)
    monkeypatch.setattr(mod.Path, "is_socket", lambda _: True)
    owner = mod.TrialDaemon()
    sock = owner.start("driver")
    assert sock.endswith("/driver.sock") and owner.process is child
    assert owner.directory.stat().st_mode & 0o777 == 0o700
    assert calls[0][0] == ["driver", "serve", "--socket", sock, "--no-overlay"]
    assert calls[0][1]["umask"] == 0o077
    assert calls[0][1]["start_new_session"] is True
    other = mod.TrialDaemon()
    assert other.start("driver") != sock
    other.close()
    owner.close()
    assert not Path(sock).parent.exists()


def test_stale_legacy_socket_is_not_reused_or_deleted(tmp_path, monkeypatch):
    mod, child = module(), Child()
    stale = tmp_path / "legacy-driver.sock"
    stale.write_text("unowned")
    setup_start(mod, monkeypatch, tmp_path, child)
    monkeypatch.setattr(mod.Path, "is_socket", lambda _: True)
    owner = mod.TrialDaemon()
    assert owner.start("driver") != str(stale)
    owner.close()
    assert stale.read_text() == "unowned"


@pytest.mark.parametrize("exit_early", [False, True])
def test_startup_timeout_or_child_exit_cleans_only_owned_child(tmp_path, monkeypatch, exit_early):
    mod, child = module(), Child(exited=exit_early)
    setup_start(mod, monkeypatch, tmp_path, child)
    monkeypatch.setattr(mod.Path, "is_socket", lambda _: False)
    import itertools
    ticks = itertools.chain([0, 0, 9], itertools.count(10))
    monkeypatch.setattr(mod.time, "monotonic", lambda: next(ticks))
    owner = mod.TrialDaemon()
    with pytest.raises(RuntimeError, match="qa_daemon_start_"):
        owner.start("driver", timeout=1)
    assert owner.closed and not owner.directory.exists()
    assert child.exited


def test_stubborn_child_is_killed_reaped_and_close_is_idempotent(tmp_path):
    mod = module()
    owner = mod.TrialDaemon()
    owner.directory, owner.process = tmp_path / "owned", Child(stubborn=True)
    owner.directory.mkdir()
    owner.close()
    owner.close()
    assert owner.process.calls == ["terminate", ("wait", 2), "kill", ("wait", 2)]


def test_unconfirmed_child_death_keeps_handle_and_directory(tmp_path):
    mod = module()
    owner = mod.TrialDaemon()
    owner.directory, owner.process = tmp_path / "owned", Child(fail=True)
    owner.directory.mkdir()
    with pytest.raises(RuntimeError, match="qa_daemon_close_unconfirmed"):
        owner.close()
    assert not owner.closed and owner.directory.exists() and owner.process is not None


def test_exited_parent_does_not_hide_surviving_owned_descendant(tmp_path, monkeypatch):
    mod = module()
    owner = mod.TrialDaemon()
    owner.directory, owner.process = tmp_path / "owned", Child(exited=True)
    owner.directory.mkdir()
    owner.process_group = owner.process.pid
    signals, alive = [], [True]
    def group_signal(group, sig):
        assert group == owner.process.pid
        if not alive[0]:
            raise ProcessLookupError
        signals.append(sig)
        if sig == mod.signal.SIGKILL:
            alive[0] = False
    monkeypatch.setattr(mod.os, "killpg", group_signal)
    ticks = iter(range(100))
    monkeypatch.setattr(mod.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(mod.time, "sleep", lambda _: None)
    owner.close()
    assert mod.signal.SIGTERM in signals and mod.signal.SIGKILL in signals
    assert owner.closed and not owner.directory.exists()


def test_group_survival_or_unknown_readback_prevents_cleanup_success(tmp_path, monkeypatch):
    mod = module()
    owner = mod.TrialDaemon()
    owner.directory, owner.process = tmp_path / "owned", Child(exited=True)
    owner.directory.mkdir()
    owner.process_group = owner.process.pid
    monkeypatch.setattr(mod.os, "killpg", lambda *a: None)
    ticks = iter(range(100))
    monkeypatch.setattr(mod.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(mod.time, "sleep", lambda _: None)
    with pytest.raises(RuntimeError, match="qa_daemon_close_unconfirmed"):
        owner.close()
    assert owner.directory.exists() and not owner.closed
    monkeypatch.setattr(mod.os, "killpg", lambda *a: (_ for _ in ()).throw(PermissionError()))
    with pytest.raises(RuntimeError, match="qa_daemon_close_unconfirmed"):
        owner.close()


def test_retry_never_signals_retired_group_after_listener_failure(tmp_path, monkeypatch):
    mod = module()
    owner = mod.TrialDaemon()
    owner.directory, owner.process = tmp_path / "owned", Child(exited=True)
    owner.directory.mkdir()
    owner.process_group = owner.process.pid
    monkeypatch.setattr(mod.os, "killpg", lambda *a: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(owner, "_listener_alive", lambda: True)
    with pytest.raises(RuntimeError, match="qa_daemon_close_unconfirmed"):
        owner.close()
    assert not owner.closed and owner.directory.exists()
    # An OS may reuse a now-retired group ID. No probe/signal is authorized.
    monkeypatch.setattr(mod.os, "killpg", lambda *a: (_ for _ in ()).throw(AssertionError("retired group")))
    monkeypatch.setattr(owner, "_listener_alive", lambda: False)
    owner.close()
    assert owner.closed and not owner.directory.exists()


@pytest.mark.parametrize("failure", ["group_wait", "parent_wait", "listener_probe", "dir_cleanup"])
def test_owned_close_failure_records_exact_stage_without_relaxing_gate(tmp_path, monkeypatch, failure):
    mod = module()
    owner = mod.TrialDaemon()
    owner.directory, owner.process = tmp_path / "owned", Child(exited=failure != "parent_wait", fail=failure == "parent_wait")
    owner.directory.mkdir()
    owner.process_group = owner.process.pid
    def killpg(*args):
        if failure == "group_wait":
            raise PermissionError("private metadata")
        raise ProcessLookupError
    monkeypatch.setattr(mod.os, "killpg", killpg)
    monkeypatch.setattr(owner, "_listener_alive", lambda: failure == "listener_probe")
    if failure == "dir_cleanup":
        monkeypatch.setattr(mod.shutil, "rmtree", lambda _: (_ for _ in ()).throw(OSError("private path")))
    with pytest.raises((RuntimeError, OSError)):
        owner.close()
    assert owner.closed is False and owner.directory.exists()
    assert owner.close_trace[-1]["stage"] == failure
    assert owner.close_trace[-1]["outcome"] in {"error", "unconfirmed"}
    assert all(row["duration_ms"] >= 0 and row["owned_group"] == owner.process.pid for row in owner.close_trace)
    assert "private" not in str(owner.close_trace)


def test_escaped_accepting_listener_prevents_success_after_group_death(tmp_path, monkeypatch):
    mod = module()
    owner = mod.TrialDaemon()
    owner.directory, owner.process = tmp_path / "owned", Child(exited=True)
    owner.directory.mkdir()
    (owner.directory / "driver.sock").touch()
    owner.process_group = owner.process.pid
    monkeypatch.setattr(mod.os, "killpg", lambda *a: (_ for _ in ()).throw(ProcessLookupError()))
    class Probe:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass
        def settimeout(self, timeout):
            assert timeout == 0.2
        def connect(self, path):
            assert path == str(owner.directory / "driver.sock")
    monkeypatch.setattr(mod.socket, "socket", lambda *a: Probe())
    with pytest.raises(RuntimeError, match="qa_daemon_close_unconfirmed"):
        owner.close()
    assert not owner.closed and owner.directory.exists()


def test_proxy_failure_still_closes_owned_child_and_borrowed_has_no_daemon_effect():
    mod = module()
    calls = []
    class Proxy:
        def close(self):
            calls.append("proxy")
            raise RuntimeError("private details")
    class Owner:
        def close(self):
            calls.append("owned")
    with pytest.raises(RuntimeError, match="^qa_backend_close_unconfirmed$"):
        mod.owned_caller_close(Proxy(), Owner())()
    assert calls == ["proxy", "owned"]
    calls.clear()
    with pytest.raises(RuntimeError, match="^qa_backend_close_unconfirmed$"):
        mod.owned_caller_close(Proxy())()
    assert calls == ["proxy"]


def test_make_mac_handshake_failure_tears_down_owned_daemon(monkeypatch):
    import importlib
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts/desktop"))
    trial = importlib.import_module("cu_ai_trial")
    daemon = importlib.import_module("cu_trial_daemon")
    import desktop_runtime.cua_session as sessions
    calls = []
    class Owner:
        def start(self, binary):
            calls.append("start")
            return "/private/tmp/supymem-cua-qa-owned/driver.sock"
        def close(self):
            calls.append("close")
    monkeypatch.setattr(daemon, "TrialDaemon", Owner)
    monkeypatch.setattr(sessions, "CuaSession", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("handshake")))
    with pytest.raises(RuntimeError, match="handshake"):
        trial.make_mac()
    assert calls == ["start", "close"]


@pytest.mark.parametrize("borrowed", [False, True])
def test_run_task_closes_only_backend_it_created_even_when_engine_raises(monkeypatch, borrowed):
    import importlib
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts/desktop"))
    trial = importlib.import_module("cu_ai_trial")
    calls = []
    class Mac:
        def close(self):
            calls.append("mac")
    class Client:
        def close(self):
            calls.append("client")
    monkeypatch.setenv("OPENAI_DEVICE_VISION_API_KEY", "offline-test")
    monkeypatch.setattr(trial.httpx, "Client", lambda **kwargs: Client())
    monkeypatch.setattr(trial, "make_mac", lambda _: Mac())
    monkeypatch.setattr(trial, "FileCheckpoint", lambda _: type("CK", (), {"clear": lambda self: None})())
    monkeypatch.setattr(trial, "run_loop", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("engine-primary")))
    with pytest.raises(RuntimeError, match="engine-primary"):
        trial.run_task("synthetic task", mac=Mac() if borrowed else None)
    assert calls == (["client"] if borrowed else ["client", "mac"])


def test_stop_outcome_survives_failed_owned_cleanup_and_is_not_acceptance(monkeypatch):
    import importlib
    from types import SimpleNamespace
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts/desktop"))
    trial = importlib.import_module("cu_ai_trial")
    class Mac:
        def close(self):
            raise RuntimeError("private cleanup text")
    monkeypatch.setenv("OPENAI_DEVICE_VISION_API_KEY", "offline-test")
    monkeypatch.setattr(trial.httpx, "Client", lambda **kwargs: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(trial, "make_mac", lambda _: Mac())
    monkeypatch.setattr(trial, "FileCheckpoint", lambda _: SimpleNamespace(clear=lambda: None))
    monkeypatch.setattr(trial, "run_loop", lambda *a, **kw: SimpleNamespace(
        outcome="stopped", reason="cancelled", summary="", steps=0, log=[]))
    out = trial.run_task("synthetic task")
    assert out["outcome"] == "stopped" and out["cleanup_verified"] is False
    assert "private cleanup" not in str(out)


def test_immediate_group_wait_error_keeps_authority_and_records_safe_type(tmp_path, monkeypatch):
    mod = module()
    owner = mod.TrialDaemon()
    owner.directory = tmp_path / 'owned'
    owner.directory.mkdir()
    owner.process = Child(exited=True)
    owner.process_group = owner.process.pid
    monkeypatch.setattr(owner, '_group_alive', lambda: False)
    monkeypatch.setattr(owner, '_wait_group', lambda _: (_ for _ in ()).throw(NameError('private symbol')))
    with pytest.raises(RuntimeError, match='qa_daemon_close_unconfirmed'):
        owner.close()
    assert not owner.closed and not owner._group_gone and owner.directory.exists()
    assert owner.close_trace[-1]['error_type'] == 'NameError'
    assert 'private' not in str(owner.close_trace)


def eperm_owner(mod, tmp_path, *, exited=True, identity=True):
    owner = mod.TrialDaemon()
    owner.directory = tmp_path / 'owned'
    owner.directory.mkdir()
    owner.process = Child(exited=exited)
    owner.process_group = owner.process.pid
    if identity:
        owner._owned_group_identity = (owner.process.pid, owner.process.pid, owner.process.pid)
    return owner


def test_eperm_only_retires_after_original_parent_exit_and_independent_empty_inventory(tmp_path, monkeypatch):
    import errno
    mod = module()
    owner = eperm_owner(mod, tmp_path)
    calls = []
    def probe(group, sig):
        calls.append((group, sig))
        assert sig == 0  # EPERM is not permission to send TERM/KILL
        raise PermissionError(errno.EPERM, 'private details')
    monkeypatch.setattr(mod.os, 'killpg', probe)
    monkeypatch.setattr(mod, 'darwin_group_members', lambda group: ())
    owner.close()
    assert owner.closed and owner._group_gone and not owner.directory.exists()
    assert calls == [(owner.process.pid, 0)]
    monkeypatch.setattr(mod.os, 'killpg', lambda *a: pytest.fail('retired/reused group must never be probed or signaled'))
    owner.close()
    assert owner._group_alive() is False


@pytest.mark.parametrize('case', ['alive_parent', 'identity_unknown', 'group_nonempty', 'inventory_unknown'])
def test_eperm_uncertainty_never_becomes_death_or_signal_authority(tmp_path, monkeypatch, case):
    import errno
    mod = module()
    owner = eperm_owner(mod, tmp_path, exited=case != 'alive_parent', identity=case != 'identity_unknown')
    calls = []
    def probe(group, sig):
        calls.append(sig)
        raise PermissionError(errno.EPERM, 'private details')
    monkeypatch.setattr(mod.os, 'killpg', probe)
    def inventory(group):
        if case == 'inventory_unknown':
            raise RuntimeError('qa_group_inventory_unavailable')
        return (999,) if case == 'group_nonempty' else ()
    monkeypatch.setattr(mod, 'darwin_group_members', inventory)
    with pytest.raises(RuntimeError, match='qa_daemon_close_unconfirmed'):
        owner.close()
    assert not owner.closed and not owner._group_gone and owner.directory.exists()
    assert calls == [0]


def test_empty_group_eperm_proof_still_cannot_hide_detached_listener(tmp_path, monkeypatch):
    import errno
    mod = module()
    owner = eperm_owner(mod, tmp_path)
    monkeypatch.setattr(mod.os, 'killpg', lambda *a: (_ for _ in ()).throw(PermissionError(errno.EPERM, 'private')))
    monkeypatch.setattr(mod, 'darwin_group_members', lambda group: ())
    monkeypatch.setattr(owner, '_listener_alive', lambda: True)
    with pytest.raises(RuntimeError, match='qa_daemon_close_unconfirmed'):
        owner.close()
    assert owner._group_gone and not owner.closed and owner.directory.exists()
    monkeypatch.setattr(mod.os, 'killpg', lambda *a: pytest.fail('retired group'))
    monkeypatch.setattr(owner, '_listener_alive', lambda: False)
    owner.close()
    assert owner.closed


@pytest.mark.parametrize('length,error', [(0, 1), (-1, 0), (16384, 0), (3, 0)])
def test_native_inventory_failure_or_truncation_is_unknown(monkeypatch, length, error):
    mod = module()
    monkeypatch.setattr(mod.sys, 'platform', 'darwin')
    class Query:
        def __call__(self, *args):
            mod.ctypes.set_errno(error)
            return length
    from types import SimpleNamespace
    monkeypatch.setattr(mod.ctypes, 'CDLL', lambda *a, **kw: SimpleNamespace(proc_listpids=Query()))
    with pytest.raises(RuntimeError, match='qa_group_inventory_unavailable'):
        mod.darwin_group_members(123)


def test_actual_darwin_inventory_observes_owned_child_then_empty_after_reap():
    import os
    import sys
    if sys.platform != 'darwin':
        pytest.skip('Darwin libproc platform contract')
    mod = module()
    # Harmless owned interpreter; no GUI/driver/socket/provider or broad signal.
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(0.25)'], start_new_session=True)
    try:
        assert os.getpgid(child.pid) == os.getsid(child.pid) == child.pid
        assert child.pid in mod.darwin_group_members(child.pid)
        child.wait(timeout=3)
        assert mod.darwin_group_members(child.pid) == ()
        # Exercise EPERM recovery using the real kernel membership result;
        # only the absent-group signal probe is substituted, never a kill.
        import errno
        from unittest.mock import patch
        owner = mod.TrialDaemon()
        owner.process, owner.process_group = child, child.pid
        owner._owned_group_identity = (child.pid, child.pid, child.pid)
        def denied(group, sig):
            assert group == child.pid and sig == 0
            raise PermissionError(errno.EPERM, "synthetic permission error")
        with patch.object(mod.os, "killpg", denied):
            assert owner._group_alive() is False and owner._group_gone
    finally:
        child.wait(timeout=3)


def test_startup_identity_mismatch_never_grants_group_signal_authority(tmp_path, monkeypatch):
    mod, child = module(), Child()
    setup_start(mod, monkeypatch, tmp_path, child)
    monkeypatch.setattr(mod.os, "getsid", lambda pid: pid + 1)
    monkeypatch.setattr(mod.os, "killpg", lambda *a: pytest.fail("unconfirmed group must not be signaled"))
    owner = mod.TrialDaemon()
    with pytest.raises(RuntimeError, match="qa_daemon_group_identity_unconfirmed"):
        owner.start("fake")
    assert owner.process_group is None and owner._owned_group_identity is None
    assert child.calls == ["terminate", ("wait", 2)]
    assert owner.closed


@pytest.mark.parametrize("case,reason", [("alive_parent", "parent_not_exited"),
    ("identity_unknown", "ownership_unconfirmed"), ("group_nonempty", "kernel_group_nonempty"),
    ("inventory_unknown", "kernel_inventory_unavailable")])
def test_eperm_probe_attribution_is_fixed_and_conservative(tmp_path, monkeypatch, case, reason):
    import errno
    mod = module()
    owner = eperm_owner(mod, tmp_path, exited=case != "alive_parent", identity=case != "identity_unknown")
    monkeypatch.setattr(mod.os, "killpg", lambda group, sig: (_ for _ in ()).throw(PermissionError(errno.EPERM, "private")))
    def inventory(group):
        if case == "inventory_unknown":
            raise RuntimeError("private inventory body")
        return (999,) if case == "group_nonempty" else ()
    monkeypatch.setattr(mod, "darwin_group_members", inventory)
    with pytest.raises(RuntimeError, match="qa_daemon_close_unconfirmed"):
        owner.close()
    from desktop_runtime.cu_cleanup import public_cleanup_trace
    trace = public_cleanup_trace(owner.close_trace)
    assert trace[-1]["group_probe_reason"] == reason
    if case == "group_nonempty":
        assert trace[-1]["group_member_count"] == 1
    assert "private" not in str(trace)
    assert not owner.closed and not owner._group_gone

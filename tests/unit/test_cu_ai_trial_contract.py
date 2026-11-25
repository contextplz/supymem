"""Evidence gates tested without launching apps, browser, models or GUI tools."""
import importlib.util
from pathlib import Path

import pytest


def suite():
    source = Path(__file__).resolve().parents[2] / "scripts" / "desktop" / "cu_ai_suite.py"
    spec = importlib.util.spec_from_file_location("cu_suite_contract", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_unknown_result_and_missing_decoy_cannot_pass():
    mod = suite()
    assert mod.trial_passed(True, "done", True, True)
    for verified in (False, None):
        assert not mod.trial_passed(verified, "done", True)
    assert not mod.trial_passed(True, "done", False)
    assert not mod.trial_passed(True, "failed", True)
    for display in ("", "10", "100", "900", "unknown"):
        assert not mod.decoy_unchanged(display)
    assert mod.decoy_unchanged("0")


def test_trial_never_kills_textedit_or_erases_saved_state(monkeypatch):
    mod = suite()
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not execute")))
    monkeypatch.setattr(mod.shutil, "rmtree", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not delete")))
    assert mod.kill_textedit() is None


def test_notes_readback_queries_only_named_trial_note(monkeypatch):
    mod = suite()
    scripts = []
    state = {"created": False, "edited": False}
    def read(script, **kwargs):
        scripts.append(script)
        if mod.EDIT_TITLE in script:
            return ("1\x1eowned-id\x1e" + mod.EDIT_TITLE + "\n" + mod.NOTE_BODY + "\n" + mod.EDIT_LINE) if state["edited"] else "0"
        return ("1\x1eowned-id\x1e" + mod.NOTE_TITLE + "\n" + mod.NOTE_BODY) if state["created"] and not state["edited"] else "0"
    monkeypatch.setattr(mod, "osa", read)
    mod.OWNED_NOTES.prepare_create()
    state["created"] = True
    assert mod.verify_notes_app()[0] is True
    mod.OWNED_NOTES.prepare_edit()
    state["edited"] = True
    assert mod.verify_notes_rename()[0] is True
    assert all("name of every note" not in script and "whose name is" in script for script in scripts)


def test_recipient_monitor_requires_fresh_same_instance_and_zero_input():
    suite()  # registers sibling script import path only; no GUI execution
    from cu_input_decoy import evidence
    before = {"instance_id": "run1", "pid": 123, "received_input_events": 0,
              "samples": 1, "updated_at": 100.0, "body_sha256": "a" * 64}
    after = {**before, "samples": 10, "updated_at": 101.0}
    assert evidence(before, after, "run1", 123, now=101.0) is True
    for change in ({"received_input_events": 1}, {"body_sha256": "b" * 64}):
        assert evidence(before, {**after, **change}, "run1", 123, now=101.0) is False
    for change in ({"instance_id": "other"}, {"samples": 1}, {"updated_at": 90.0}, {"pid": 999}):
        assert evidence(before, {**after, **change}, "run1", 123, now=101.0) is None
    assert evidence(before, None, "run1", 123, now=101.0) is None


def test_cleanup_refuses_existing_personal_or_outside_run_paths(tmp_path, monkeypatch):
    import pytest
    mod = suite()
    monkeypatch.setattr(mod, "DIR", tmp_path / "fresh-run")
    monkeypatch.setattr(mod, "_OWNED_RUN", True)
    other = tmp_path / "personal.txt"
    other.write_text("keep")
    with pytest.raises(ValueError, match="not_owned"):
        mod.rm(other)
    assert other.read_text() == "keep"


@pytest.mark.parametrize("stage,error", [("setup", ValueError), ("task", ValueError),
                                          ("constructor", OSError), ("start", OSError),
                                          ("start", ValueError), ("start", KeyboardInterrupt)])
def test_startup_errors_and_interrupts_cleanup_each_owned_component_once(tmp_path, monkeypatch, stage, error, capsys):
    from types import SimpleNamespace
    mod = suite()
    import cu_input_decoy as monitor
    attempts, written = [], []
    def fail():
        raise error("private startup text")
    def setup():
        attempts.append("setup")
        if stage == "setup":
            fail()
    def task():
        if stage == "task":
            fail()
        return "synthetic"
    def constructor(_):
        if stage == "constructor":
            fail()
        return SimpleNamespace(start=fail, close=lambda: attempts.append("decoy_close"))
    monkeypatch.setattr(mod, "DIR", tmp_path / "owned")
    monkeypatch.setattr(mod, "TRIALS", {"test": {"setup": setup, "task": task,
        "cleanup": lambda: (attempts.append("cleanup") or True)}})
    monkeypatch.setattr(mod, "active_fixture", lambda _: SimpleNamespace(close=lambda: attempts.append("fixture_close")))
    monkeypatch.setattr(monitor, "InputDecoy", constructor)
    monkeypatch.setattr(mod, "write_record", lambda row: (written.append(row) or row))
    if error is KeyboardInterrupt:
        with pytest.raises(KeyboardInterrupt):
            mod.run("test", "cua")
        assert written == []
    else:
        result = mod.run("test", "cua")
        assert len(written) == 1 and result["cleanup_verified"] is True
        assert result["outcome"] == "preflight_failed" and not result["passed"]
        assert "private" not in str(result)
    assert attempts.count("cleanup") == attempts.count("fixture_close") == 1
    assert attempts.count("decoy_close") == (stage == "start")
    assert capsys.readouterr().out == ""


def test_decoy_bundle_supplies_principal_class_to_appkit_without_launching(tmp_path, monkeypatch):
    import plistlib
    import time
    from types import SimpleNamespace
    suite()
    import cu_input_decoy as monitor
    decoy = monitor.InputDecoy(tmp_path, frontmost=lambda: ("owned", 123))
    calls = []
    monkeypatch.setattr(monitor.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    def no_gui_spawn(args, **kwargs):
        calls.append(args)
        bundle = Path(args[0]).parents[2]
        info = plistlib.loads((bundle / "Contents" / "Info.plist").read_bytes())
        assert info["NSPrincipalClass"] == "DecoyApplication"
        assert info["CFBundleExecutable"] == Path(args[0]).name
        assert kwargs["umask"] == 0o077
        return SimpleNamespace(pid=123, poll=lambda: None)
    monkeypatch.setattr(monitor.subprocess, "Popen", no_gui_spawn)
    count = iter(range(1, 10))
    monkeypatch.setattr(decoy, "read", lambda: {"instance_id": decoy.instance, "pid": 123,
                         "received_input_events": 0, "samples": next(count), "updated_at": time.time(),
                         "body_sha256": "a" * 64, "is_active": True, "active_app_pid": 123})
    assert decoy.start()["received_input_events"] == 0
    assert len(calls) == 1


def test_decoy_valid_snapshot_without_focus_never_passes_preflight(tmp_path, monkeypatch):
    import time
    import pytest
    from types import SimpleNamespace
    suite()
    from cu_input_decoy import InputDecoy
    decoy = InputDecoy(tmp_path, frontmost=lambda: ("other", 999))
    decoy.process = SimpleNamespace(pid=123, poll=lambda: None)
    from itertools import count as serial
    count = serial(1)
    monkeypatch.setattr(decoy, "read", lambda: {"instance_id": decoy.instance, "pid": 123,
        "received_input_events": 0, "samples": next(count), "updated_at": time.time(),
        "body_sha256": "a" * 64, "is_active": True, "active_app_pid": 123})
    with pytest.raises(RuntimeError, match="focus_unverified"):
        decoy.wait_for_focus(timeout=.15)


def test_focus_trace_requires_both_sources_fresh_complete_and_joined(monkeypatch):
    import time
    from types import SimpleNamespace
    suite()
    from cu_focus_trace import FocusTrace
    snapshot = {"instance_id": "run", "pid": 123, "received_input_events": 0,
        "samples": 3, "updated_at": time.time(), "body_sha256": "a" * 64,
        "is_active": True, "active_app_pid": 123}
    decoy = SimpleNamespace(instance="run", process=SimpleNamespace(pid=123), read=lambda: snapshot)
    trace = FocusTrace(decoy, lambda: ("owned", 123))
    trace.sample()
    trace.sample()
    assert trace.verified(True)
    assert not trace.verified(False)
    snapshot["active_app_pid"] = 999
    trace.sample()
    assert not trace.verified(True)
    trace.samples = trace.samples[:2]
    trace.samples[1]["at_monotonic"] += 3
    assert not trace.verified(True)
    trace.samples[1]["at_monotonic"] -= 3
    snapshot["updated_at"] = time.time() - 10
    trace.sample()
    assert not trace.verified(True)


def test_trace_records_only_metadata_with_unknown_code_fingerprint():
    from types import SimpleNamespace
    suite()
    from cu_focus_trace import FocusTrace, traced_call
    decoy = SimpleNamespace()
    trace = FocusTrace(decoy, lambda: ("", None))
    private = "private window body and input"
    call = traced_call(lambda *_: {"status": "refused", "code": "new_symbolic_code",
        "message": private, "elements": [{"value": private}], "screenshot_png_b64": private}, trace)
    call("hotkey", {"target": {"pid": 123, "window_id": 7}, "delivery_mode": "background", "text": private})
    assert len(trace.actions) == 2
    assert trace.actions[0]["action_index"] == trace.actions[1]["action_index"] == 1
    assert trace.actions[1]["driver_code"] == "other_code"
    assert len(trace.actions[1]["driver_code_id"]) == 12
    assert private not in str(trace.actions) and "new_symbolic_code" not in str(trace.actions)


def test_focus_sampler_is_joined_and_overflow_cannot_pass():
    import time
    from types import SimpleNamespace
    suite()
    from cu_focus_trace import FocusTrace
    snapshot = {"instance_id": "run", "pid": 123, "received_input_events": 0,
        "samples": 3, "updated_at": time.time(), "body_sha256": "a" * 64,
        "is_active": True, "active_app_pid": 123}
    decoy = SimpleNamespace(instance="run", process=SimpleNamespace(pid=123), read=lambda: snapshot)
    trace = FocusTrace(decoy, lambda: ("owned", 123), interval=.01)
    trace.start()
    time.sleep(.03)
    joined = trace.close()
    assert joined and not trace.thread.is_alive() and trace.verified(joined)
    trace.overflow = True
    assert not trace.verified(joined)


def test_decoy_repeated_snapshot_cannot_fake_consecutive_focus(tmp_path, monkeypatch):
    import time
    import pytest
    from types import SimpleNamespace
    suite()
    from cu_input_decoy import InputDecoy
    decoy = InputDecoy(tmp_path, frontmost=lambda: ("owned", 123))
    decoy.process = SimpleNamespace(pid=123, poll=lambda: None)
    monkeypatch.setattr(decoy, "read", lambda: {"instance_id": decoy.instance, "pid": 123,
        "received_input_events": 0, "samples": 1, "updated_at": time.time(),
        "body_sha256": "a" * 64, "is_active": True, "active_app_pid": 123})
    with pytest.raises(RuntimeError, match="focus_unverified"):
        decoy.wait_for_focus(timeout=.15)


def test_public_snapshot_discards_unknown_content_fields():
    import time
    suite()
    from cu_input_decoy import public_snapshot
    snapshot = {"instance_id": "run", "pid": 123, "received_input_events": 0,
        "samples": 3, "updated_at": time.time(), "body_sha256": "a" * 64,
        "is_active": True, "active_app_pid": 123, "private_text": "secret"}
    assert "secret" not in str(public_snapshot(snapshot, "run", 123))
    assert public_snapshot({**snapshot, "body_sha256": "private" * 9 + "!"}, "run", 123) is None


def test_backend_focus_loss_blocks_model_calls_and_records_preflight_failure(tmp_path, monkeypatch):
    import time
    from types import SimpleNamespace
    mod = suite()
    import cu_input_decoy as monitor
    snapshot = {"instance_id": "run", "pid": 123, "received_input_events": 0,
        "samples": 3, "updated_at": time.time(), "body_sha256": "a" * 64,
        "is_active": True, "active_app_pid": 123}
    def lost_focus(**_):
        raise RuntimeError("qa_decoy_focus_unverified")
    decoy = SimpleNamespace(instance="run", process=SimpleNamespace(pid=123),
        start=lambda: snapshot, read=lambda: snapshot, close=lambda: None, wait_for_focus=lost_focus)
    monkeypatch.setattr(monitor, "InputDecoy", lambda _: decoy)
    monkeypatch.setattr(mod, "DIR", tmp_path / "owned")
    monkeypatch.setattr(mod, "_OWNED_RUN", False)
    monkeypatch.setattr(mod, "TRIALS", {"test": {"setup": lambda: None, "task": "synthetic",
                                                "verify": lambda: (False, "not run")}})
    monkeypatch.setattr(mod, "frontmost_info", lambda: ("owned", 123))
    monkeypatch.setattr(mod.trial, "make_mac", lambda _: SimpleNamespace(fronted=0))
    monkeypatch.setattr(mod.trial, "run_task", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no model")))
    written = []
    class Sink:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def write(self, value):
            written.append(value)
    monkeypatch.setattr(mod, "open", lambda *a, **k: Sink(), raising=False)
    result = mod.run("test", "cua")
    assert result["outcome"] == "preflight_failed" and result["model_calls"] == 0
    assert not result["passed"] and result["sampler_joined"] and written


@pytest.mark.parametrize("engine_outcome,teardown_fails,verify_fails,spec_cleanup_fails", [
    ("stopped", False, False, False), ("stopped", True, False, False),
    ("done", True, False, False), ("done", False, True, False),
    ("done", False, False, True),
])
def test_verification_and_teardown_focus_are_included_without_rewriting_stop(tmp_path, monkeypatch, engine_outcome, teardown_fails, verify_fails, spec_cleanup_fails):
    import json
    import threading
    import time
    from types import SimpleNamespace
    mod = suite()
    import cu_input_decoy as monitor
    import cu_focus_trace as tracing
    active = [123]
    snapshot = {"instance_id": "run", "pid": 123, "received_input_events": 0,
        "samples": 3, "updated_at": time.time(), "body_sha256": "a" * 64,
        "is_active": True, "active_app_pid": 123}
    traces, phases = [], []
    class Captured(tracing.FocusTrace):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            traces.append(self)
    monkeypatch.setattr(tracing, "FocusTrace", Captured)
    def read():
        return {**snapshot, "samples": snapshot["samples"] + 10, "updated_at": time.time(),
                "active_app_pid": active[0], "is_active": active[0] == 123}
    decoy = SimpleNamespace(instance="run", process=SimpleNamespace(pid=123),
        start=lambda: snapshot, read=read, close=lambda: None,
        wait_for_focus=lambda **_: threading.Event().wait(.3))
    monkeypatch.setattr(monitor, "InputDecoy", lambda _: decoy)
    monkeypatch.setattr(mod, "DIR", tmp_path / "owned")
    def verify():
        phases.append(traces[0].phase)
        if verify_fails:
            raise RuntimeError("private verification error")
        return True, "private verification content must not be logged"
    def close():
        phases.append(traces[0].phase)
        assert traces[0].thread.is_alive()
        active[0] = 999  # actual teardown changes focus; strict gate must see it
        if teardown_fails:
            raise RuntimeError("private teardown error")
    def cleanup():
        if spec_cleanup_fails:
            raise RuntimeError("private cleanup error")
        return True
    monkeypatch.setattr(mod, "TRIALS", {"test": {"setup": lambda: None, "task": "synthetic", "verify": verify, "cleanup": cleanup}})
    monkeypatch.setattr(mod, "frontmost_info", lambda: ("owned", active[0]))
    monkeypatch.setattr(mod.trial, "make_mac", lambda _: SimpleNamespace(fronted=0, close=close, observe=lambda: None))
    monkeypatch.setattr(mod.trial, "run_task", lambda *a, **k: {"outcome": engine_outcome, "reason": "stopped_by_founder", "steps": 0, "cleanup_verified": True})
    written = []
    class Sink:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def write(self, value):
            written.append(value)
    monkeypatch.setattr(mod, "open", lambda *a, **k: Sink(), raising=False)
    result = mod.run("test", "cua")
    assert phases == ["verification", "teardown"]
    assert result["outcome"] == engine_outcome and not result["background_verified"] and not result["passed"]
    assert result["cleanup_verified"] is not (teardown_fails or spec_cleanup_fails) and result["sampler_joined"]
    assert len(written) == 1  # Failed teardown emits one failed trial, never a duplicate crash row.
    assert (result["verification_error"] is not None) == verify_fails
    assert "private verification" not in str(written)
    artifact = json.loads(Path(result["focus_trace_file"]).read_text())
    assert artifact["focus"][-1]["active_app_pid"] == 999
    assert {row["phase"] for row in artifact["phases"]} >= {"verification", "teardown", "complete"}


def test_verifier_artifact_is_exclusive_private_bounded_and_metadata_only(tmp_path, capsys):
    import json
    import stat
    suite()  # installs scripts/desktop import path without running apps
    from cu_verifier_artifact import PrivateVerifierArtifact
    first, second = PrivateVerifierArtifact(tmp_path), PrivateVerifierArtifact(tmp_path)
    for n in range(80):
        first.record({'step': n, 'passed': False, 'reason': 'PRIVATE rejection ' + 'x' * 500,
                      'code': 'independent_verifier_rejected', 'provider_body': 'DO NOT SERIALIZE'})
    second.record({'step': 1, 'passed': True, 'reason': 'PRIVATE second', 'code': None})
    first.close()
    second.close()
    assert first.path != second.path
    assert stat.S_IMODE(first.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(first.path.parent.stat().st_mode) == 0o700
    rows = [json.loads(line) for line in first.path.read_text().splitlines()]
    assert len(rows) == first.count == 64 and all(len(row['reason']) == 200 for row in rows)
    assert all(set(row) == {'step', 'passed', 'reason', 'code'} for row in rows)
    assert 'DO NOT SERIALIZE' not in first.path.read_text()
    assert set(first.metadata()) == {'verifier_diagnostic_file', 'verifier_diagnostic_count'}
    assert 'PRIVATE' not in json.dumps(first.metadata())
    assert capsys.readouterr().out == ''
    first.record({'step': 81, 'passed': False, 'reason': 'after close'})
    assert first.count == 64


def test_verifier_artifact_rejects_untyped_rows_and_does_not_create_empty_files(tmp_path):
    suite()
    from cu_verifier_artifact import PrivateVerifierArtifact
    artifact = PrivateVerifierArtifact(tmp_path)
    for value in (None, {'passed': 'true', 'step': 1, 'reason': 'private'},
                  {'passed': False, 'step': True, 'reason': 'private'},
                  {'passed': False, 'step': 1, 'reason': {'provider': 'private'}}):
        artifact.record(value)
    artifact.close()
    assert artifact.metadata() == {'verifier_diagnostic_file': None, 'verifier_diagnostic_count': 0}
    assert list(tmp_path.iterdir()) == []



def test_run_task_retains_verdict_only_in_private_artifact(tmp_path, monkeypatch, capsys):
    import json
    from types import SimpleNamespace
    suite()
    import cu_ai_trial as trial
    from cu_verifier_artifact import PrivateVerifierArtifact
    import cu_verifier_artifact
    monkeypatch.setenv("OPENAI_DEVICE_VISION_API_KEY", "synthetic-test-key")
    monkeypatch.setattr(trial.httpx, "Client", lambda **kwargs: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(cu_verifier_artifact, "PrivateVerifierArtifact", lambda: PrivateVerifierArtifact(tmp_path))
    def engine(*args, **kwargs):
        kwargs["verifier_diagnostic"]({"step": 1, "passed": False, "reason": "PRIVATE actual rejected verdict",
                                       "code": "independent_verifier_rejected"})
        return SimpleNamespace(outcome="failed", reason="completion_retry_exhausted", summary="", steps=1, log=[])
    monkeypatch.setattr(trial, "run_loop", engine)
    result = trial.run_task("synthetic private artifact test", mac=SimpleNamespace())
    assert result["verifier_diagnostic_count"] == 1
    private = Path(result["verifier_diagnostic_file"])
    assert json.loads(private.read_text())["reason"] == "PRIVATE actual rejected verdict"
    assert "PRIVATE" not in json.dumps(result) and "PRIVATE" not in capsys.readouterr().out


def test_private_artifact_close_failure_preserves_http_cleanup_and_holds_acceptance(monkeypatch):
    from types import SimpleNamespace
    suite()
    import cu_ai_trial as trial
    import cu_verifier_artifact
    closed = []
    monkeypatch.setenv('OPENAI_DEVICE_VISION_API_KEY', 'synthetic-test-key')
    monkeypatch.setattr(trial.httpx, 'Client', lambda **kwargs: SimpleNamespace(close=lambda: closed.append(True)))
    def broken_close():
        raise OSError('PRIVATE artifact error')
    monkeypatch.setattr(cu_verifier_artifact, 'PrivateVerifierArtifact', lambda: SimpleNamespace(
        record=lambda row: None, close=broken_close, metadata=lambda: {'verifier_diagnostic_file': None, 'verifier_diagnostic_count': 0}))
    monkeypatch.setattr(trial, 'run_loop', lambda *args, **kwargs: SimpleNamespace(
        outcome='done', reason='', summary='', steps=1, log=[]))
    result = trial.run_task('synthetic close failure', mac=SimpleNamespace())
    assert closed == [True] and result['cleanup_verified'] is False
    assert 'qa_trial_cleanup_unconfirmed' in result['cleanup_codes'] and 'PRIVATE' not in str(result)

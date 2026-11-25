"""Cleanup attribution is fixed metadata, including nested proxy/daemon failures."""
import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from desktop_runtime.cu_ai_browser import BrowserMac, RoutedMac
from desktop_runtime.cu_cleanup import CleanupError, cleanup_codes, public_cleanup_codes


def test_private_exception_text_and_untrusted_attributes_never_become_codes():
    error = RuntimeError("private account/email/body")
    error.cleanup_codes = ["private account/email/body", "qa_daemon_close_unconfirmed"]
    assert cleanup_codes(error, "native_backend_close_unconfirmed") == ["qa_daemon_close_unconfirmed"]
    error.cleanup_codes = "private body"
    assert cleanup_codes(error, "native_backend_close_unconfirmed") == ["native_backend_close_unconfirmed"]
    assert public_cleanup_codes({"private": "body"}) == []


def test_owned_form_cleanup_failure_remains_attributable_without_private_text():
    assert public_cleanup_codes([
        "qa_form_fixture_cleanup_unconfirmed", "private form payload",
    ]) == ["qa_form_fixture_cleanup_unconfirmed"]


def test_cleanup_stage_trace_accepts_only_bounded_metadata():
    from desktop_runtime.cu_cleanup import public_cleanup_trace
    safe = {"stage": "group_wait", "outcome": "unconfirmed", "at_monotonic": 100,
            "duration_ms": 2000, "owned_group": 123, "group_retired": False, "private_body": "secret"}
    trace = public_cleanup_trace([safe] * 40)
    assert len(trace) == 32 and "secret" not in str(trace)
    for changed in ({"stage": ["private"]}, {"stage": "private"}, {"duration_ms": float("nan")},
                    {"outcome": "private payload"}, {"at_monotonic": "private"}):
        assert public_cleanup_trace([{**safe, **changed}]) == []


def test_owned_daemon_stage_trace_survives_nested_cleanup_error(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts/desktop"))
    daemon = importlib.import_module("cu_trial_daemon")
    safe = {"stage": "listener_probe", "outcome": "unconfirmed", "at_monotonic": 100,
            "duration_ms": 200, "owned_group": 123, "group_retired": True}
    owner = SimpleNamespace(close_trace=[safe], close=lambda: (_ for _ in ()).throw(RuntimeError("private")))
    native = SimpleNamespace(close=daemon.owned_caller_close(SimpleNamespace(close=lambda: None), owner))
    with pytest.raises(CleanupError) as caught:
        RoutedMac(native, SimpleNamespace(close=lambda: None)).close()
    assert caught.value.cleanup_trace == [safe]
    assert "private" not in str(caught.value)


def test_successful_owned_close_exposes_same_public_stage_trace(monkeypatch):
    from desktop_runtime.cu_ai_cua import CuaMac
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts/desktop"))
    daemon = importlib.import_module("cu_trial_daemon")
    safe = {"stage": "dir_cleanup", "outcome": "completed", "at_monotonic": 100,
            "duration_ms": 2, "owned_group": 123, "group_retired": True}
    owner = SimpleNamespace(close_trace=[safe], close=lambda: None)
    def call(*args):
        pytest.fail("cleanup trace must not call driver tools")
    call.close = daemon.owned_caller_close(SimpleNamespace(close=lambda: None), owner)
    native = CuaMac(call)
    routed = RoutedMac(native, SimpleNamespace(close=lambda: None))
    routed.close()
    assert native.cleanup_trace == routed.cleanup_trace == [safe]


def test_nested_routing_preserves_components_and_attempts_all_cleanup(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts/desktop"))
    daemon = importlib.import_module("cu_trial_daemon")
    calls = []
    def failure(name):
        def fail():
            calls.append(name)
            raise RuntimeError("private detail")
        return fail
    native = SimpleNamespace(close=daemon.owned_caller_close(
        SimpleNamespace(close=failure("proxy")), SimpleNamespace(close=failure("daemon"))))
    browser = BrowserMac(SimpleNamespace(close=failure("cdp")),
                         factory=SimpleNamespace(stop=failure("browser_process")))
    with pytest.raises(CleanupError) as caught:
        RoutedMac(native, browser).close()
    assert calls == ["cdp", "browser_process", "proxy", "daemon"]
    assert set(caught.value.cleanup_codes) == {
        "browser_cdp_close_unconfirmed", "browser_process_close_unconfirmed",
        "qa_proxy_close_unconfirmed", "qa_daemon_close_unconfirmed"}
    assert str(caught.value) == "cu_backend_close_unconfirmed"


def test_run_task_http_client_close_failure_is_separate_from_engine_stop(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts/desktop"))
    trial = importlib.import_module("cu_ai_trial")
    monkeypatch.setenv("OPENAI_DEVICE_VISION_API_KEY", "offline-test")
    client = SimpleNamespace(close=lambda: (_ for _ in ()).throw(RuntimeError("private response")))
    monkeypatch.setattr(trial.httpx, "Client", lambda **kw: client)
    monkeypatch.setattr(trial, "FileCheckpoint", lambda _: SimpleNamespace(clear=lambda: None))
    monkeypatch.setattr(trial, "run_loop", lambda *a, **kw: SimpleNamespace(
        outcome="stopped", reason="cancelled", summary="", steps=0, log=[]))
    result = trial.run_task("synthetic", mac=SimpleNamespace())
    assert result["outcome"] == "stopped" and result["cleanup_verified"] is False
    assert result["cleanup_codes"] == ["qa_http_client_close_unconfirmed"]
    assert "private" not in str(result)

@pytest.mark.parametrize('error,kind,number', [
    (PermissionError(13, 'private path'), 'PermissionError', 'EACCES'),
    (ProcessLookupError(3, 'private payload'), 'ProcessLookupError', 'ESRCH'),
    (NameError('private identifier'), 'NameError', None),
    (OSError(99999, 'private path'), 'OSError', None),
])
def test_cleanup_error_metadata_is_fixed_and_content_free(error, kind, number):
    from desktop_runtime.cu_cleanup import public_cleanup_error, public_cleanup_trace
    row = {'stage': 'group_wait', 'outcome': 'error', 'at_monotonic': 1, 'duration_ms': 0,
           **public_cleanup_error(error)}
    actual = public_cleanup_trace([row])[0]
    assert actual['error_type'] == kind
    assert actual.get('error_errno') == number
    assert 'private' not in str(actual)
    assert 'error_type' not in public_cleanup_trace([{**row, 'error_type': ['private']}])[0]
    assert 'error_errno' not in public_cleanup_trace([{**row, 'error_errno': ['private']}])[0]


def test_group_probe_diagnostics_admit_fixed_metadata_only():
    from desktop_runtime.cu_cleanup import public_cleanup_trace
    row = {"stage": "group_wait", "outcome": "error", "at_monotonic": 1.0, "duration_ms": 0.4,
           "group_probe_reason": "kernel_group_nonempty", "group_member_count": 2, "private": "secret"}
    clean = public_cleanup_trace([row])[0]
    assert clean["group_probe_reason"] == "kernel_group_nonempty" and clean["group_member_count"] == 2
    assert "secret" not in str(clean)
    for bad in ["private_account_id", {"private": "secret"}, None]:
        row["group_probe_reason"] = bad
        assert "group_probe_reason" not in public_cleanup_trace([row])[0]
    row["group_probe_reason"] = "kernel_group_nonempty"
    row["group_member_count"] = 5000
    assert "group_member_count" not in public_cleanup_trace([row])[0]

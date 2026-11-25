"""Failure clocks follow the real worker reporting path without page/provider I/O."""
import json
from types import SimpleNamespace

import pytest

from desktop_runtime import crash_report, device_worker


def worker_fixture(monkeypatch):
    clock = [0.0]
    reports = []
    monkeypatch.setattr(crash_report, "_ensure_error_log", lambda: None)
    monkeypatch.setattr(device_worker, "report_error", lambda *args, **kwargs: reports.append((args, kwargs)))
    worker = device_worker.DeviceWorker(SimpleNamespace(order_id="dwo_diagnostic", claim_rejection=None), lambda _: None)
    worker._runtime_diagnostics = crash_report.RuntimeDiagnostics(clock=lambda: clock[0])
    return worker, clock, reports


@pytest.mark.parametrize("domain", ["warehouse handoffs", "family gardening"])
def test_landing_progress_keeps_remaining_time_and_real_state(monkeypatch, domain):
    worker, _, _ = worker_fixture(monkeypatch)
    worker.last.update(product_profile="static_landing_v1", title=domain)
    now = [100.0]
    monkeypatch.setattr(device_worker.time, "time", lambda: now[0])
    worker.progress("running_coding", build_deadline_at=1000)
    assert worker.last["build_remaining_seconds"] == 900
    now[0] += 510
    worker.progress("running_coding", events=[{"at": now[0], "text": "Your files are saved. Open Website to check them."}])
    assert worker.last["build_remaining_seconds"] == 390
    worker.progress("stopped", task_verified=False, outcome="timeout")
    assert worker.last["build_remaining_seconds"] == 0
    assert worker.last["state"] == "stopped" and len(worker.last["events"]) == 1


def test_failure_phase_is_frozen_before_cleanup_and_settlement(monkeypatch):
    worker, clock, reports = worker_fixture(monkeypatch)

    def run():
        worker.progress("deciding", message="PRIVATE_SCREEN_PROMPT")
        clock[0] = 0.125
        worker.last.update(failure_code="native_execution_failed", reason_code="cu_browser_decision_unavailable")
        worker.progress("closing", outcome="attention_required")
        clock[0] = 30
        worker.progress("reconciliation_required", credits_reconciled=False)

    monkeypatch.setattr(worker, "_run", run)
    worker.run()
    (args, metadata), = reports
    assert args == ("device_worker", "run_mac_task")
    assert metadata["code"] == "cu_browser_decision_unavailable"
    assert metadata["runtime_diagnostic"] == {"phase": "deciding", "reason": "cu_browser_decision_unavailable",
        "phase_ms": 125, "elapsed_ms": 125}
    assert "PRIVATE_SCREEN_PROMPT" not in json.dumps(reports)


@pytest.mark.parametrize("title", ["Warehouse procurement", "Weekend cooking"])
def test_coding_transport_failure_is_not_a_computer_use_effect(monkeypatch, title):
    worker, clock, reports = worker_fixture(monkeypatch)

    def run():
        worker.progress("claiming", tool="coding", message=title)
        clock[0] = 0.1
        worker.last["failure_code"] = "native_worker_request_unconfirmed"
        worker.progress("closing", outcome="execution_requires_reconciliation")

    monkeypatch.setattr(worker, "_run", run)
    worker.run()
    assert reports[0][1]["code"] == "native_worker_request_unconfirmed"
    assert reports[0][1]["runtime_diagnostic"]["reason"] == "runtime_failure"
    assert reports[0][1]["runtime_diagnostic"]["phase"] == "claiming"
    assert title not in json.dumps(reports)


def test_recoverable_pause_does_not_latch_a_failure_reason(monkeypatch):
    worker, clock, reports = worker_fixture(monkeypatch)

    def run():
        worker.progress("observing")
        clock[0] = 1
        worker.progress("needs_you", reason="PRIVATE_SIGN_IN", pause_kind="password")
        clock[0] = 20
        worker.progress("acting", resumed_after="needs_you")
        clock[0] = 20.5
        worker.last["failure_code"] = "cu_effect_unverified"
        worker.progress("closing", outcome="execution_requires_reconciliation")
        clock[0] = 50

    monkeypatch.setattr(worker, "_run", run)
    worker.run()
    diagnostic = reports[0][1]["runtime_diagnostic"]
    assert diagnostic == {"phase": "acting", "reason": "cu_effect_unverified", "phase_ms": 500, "elapsed_ms": 20500}
    assert "PRIVATE_SIGN_IN" not in json.dumps(reports)


def test_uncaught_failure_has_closed_metadata_and_preserves_exception(monkeypatch):
    worker, clock, reports = worker_fixture(monkeypatch)

    def run():
        worker.progress("observing")
        clock[0] = 0.25
        raise ValueError("PRIVATE_PROVIDER_RESPONSE")

    monkeypatch.setattr(worker, "_run", run)
    with pytest.raises(ValueError, match="PRIVATE_PROVIDER_RESPONSE"):
        worker.run()
    assert reports[0][1]["label"] == "Mac uncaught_exception"
    assert reports[0][1]["runtime_diagnostic"] == {"phase": "observing", "reason": "uncaught_exception",
        "phase_ms": 250, "elapsed_ms": 250}
    assert "PRIVATE_PROVIDER_RESPONSE" not in json.dumps(reports)


@pytest.mark.parametrize("domain", ["Supplier invoice review", "Family hiking planner"])
def test_landing_review_exception_reaches_order_result_and_crash_report(monkeypatch, tmp_path, domain):
    from tests.company_os_harness import load_company_os_app
    from tests.unit.product.test_product_build_runtime import Gateway, product_review
    from desktop_runtime import agent_status, coding_loop

    load_company_os_app()
    from app.pagebuilder import runtime
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_RUNTIME_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(runtime, "validate_inputs", lambda *_: None)
    monkeypatch.setattr("desktop_runtime.device_gateway.build_capability_ready", lambda: True)
    monkeypatch.setattr(agent_status, "_status", lambda _: {"installed": True, "available": True})
    monkeypatch.setattr(device_worker.OwnedCoding, "preflight", lambda *_: None)
    gateway = Gateway()
    activity = []
    gateway.coding_event = activity.append
    worker = device_worker.DeviceWorker(gateway, lambda _: None)
    monkeypatch.setattr(worker, "_notify_terminal", lambda *args, **kwargs: None)
    def fail(*_):
        worker.progress("running_coding", loop_phase="check")
        runtime.before_call(None, {"_runtime": {"calls_used": 2, "quote": {"call_budget": 2}}}, None)
    monkeypatch.setattr(coding_loop, "run_coding_loop", fail)
    monkeypatch.setattr(device_worker, "report_error", lambda *args, **kwargs: crash_report.report_error(*args, root=tmp_path, **kwargs))
    monkeypatch.setattr(crash_report, "_ensure_error_log", lambda: None)
    manifest = {**product_review(), "instruction": domain}
    manifest["coding_loop_verification"].update(profile="static_landing_v1", check_recipe="static_landing_checks_v1")
    saved = {"state": "approved", "task_snapshot": {"artifact_profile": "static_landing_v1"}}
    monkeypatch.setattr(worker, "_run", lambda: worker.run_coding_loop(saved, manifest))
    worker.run()
    result = worker.last["evidence"]["product_build"]
    assert result["exception_type"] == "FatalError"
    assert result["exception_location"].startswith("app.pagebuilder.runtime:")
    assert result["exception_location"].split(":")[1].isdigit()
    assert result["exception_code"] == result["failure_code"] == "pagebuilder_call_budget_exhausted"
    assert result["exception_frames"] == worker.last["exception_frames"]
    assert any(frame.startswith("app.pagebuilder.runtime:") for frame in result["exception_frames"])
    assert activity[-1]["exception_frames"] == result["exception_frames"]
    assert worker.last["events"][-1]["exception_frames"] == result["exception_frames"]
    crash, = crash_report.queue_dir(tmp_path).glob("crash-*.json")
    payload = json.loads(crash.read_text())
    assert payload["exception_details"] == {key: result[key] for key in ("exception_type", "exception_location", "exception_frames", "exception_code")}
    assert domain not in json.dumps(payload["exception_details"])


def test_success_and_user_cancel_add_no_error_report(monkeypatch):
    for cancelled in (False, True):
        worker, _, reports = worker_fixture(monkeypatch)

        def run():
            worker.progress("acting")
            if cancelled:
                worker.cancelled.set()
                worker.last["failure_code"] = "cu_stopped"
            worker.progress("closing", outcome="cancelled" if cancelled else "verified")

        monkeypatch.setattr(worker, "_run", run)
        worker.run()
        assert reports == []


@pytest.mark.parametrize("rejection, reason", [
    ("device_order_expired", "cu_claim_expired"),
    ("device_task_has_active_order", "cu_claim_busy"),
    ("device_order_review_changed", "cu_claim_changed"),
])
def test_definite_unstarted_claim_preserves_its_closed_reason(monkeypatch, rejection, reason):
    worker, clock, reports = worker_fixture(monkeypatch)
    worker.gateway.claim_rejection = rejection
    worker.gateway.claim_attempted = False

    def run():
        worker.progress("claiming")
        clock[0] = 0.1
        worker.last["failure_code"] = "native_execution_failed"
        worker.progress("closing", outcome="execution_requires_reconciliation")
        clock[0] = 15

    monkeypatch.setattr(worker, "_run", run)
    worker.run()
    assert reports[0][1]["runtime_diagnostic"] == {"phase": "claiming", "reason": reason,
        "phase_ms": 100, "elapsed_ms": 100}
    assert worker.last["outcome"] == "claim_rejected"
    assert worker.last["reason_code"] == reason

"""Runtime traces stay bounded and content-free through the real error pipeline."""
import json
import logging
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from core.runtime_diagnostics import closed_runtime_diagnostic
from tests.unit.test_error_reports import core as core, _rows  # noqa: F401

DIAGNOSTIC = {"phase": "deciding", "reason": "cu_browser_decision_unavailable", "phase_ms": 120, "elapsed_ms": 500}


def _summary(diagnostic=DIAGNOSTIC):
    return {"event": "ui.error", "event_id": str(uuid4()), "context_subject": "usr_runtime",
        "props": {"feature": "computer_use", "platform": "macos", "session_id": str(uuid4()),
            "error_code": "native_error", "error_context": "native", "error_text": "Mac native_failure",
            "error_source": "device_worker", "error_intent": "run_mac_task", "runtime_diagnostic": diagnostic}}


def test_first_failure_clock_survives_cleanup_without_recording_prose(tmp_path, monkeypatch):
    from desktop_runtime import crash_report
    monkeypatch.setenv("COMPANY_OS_RUNTIME_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(crash_report, "_ensure_error_log", lambda: None)
    clock = [10.0]
    trace = crash_report.RuntimeDiagnostics(clock=lambda: clock[0])
    trace.phase("deciding")
    clock[0] = 10.25
    trace.phase("closing", reason="cu_browser_decision_unavailable")
    original = trace.snapshot()
    clock[0] = 50.0
    trace.phase("reconciliation_required", reason="cu_reconcile_unavailable")
    assert trace.snapshot() == original == {**DIAGNOSTIC, "phase_ms": 250, "elapsed_ms": 250}
    # Arbitrary screen text/slugs never create a phase or reason.
    other = crash_report.RuntimeDiagnostics(clock=lambda: clock[0])
    other.phase("private_screen_text")
    assert other.snapshot("private_prompt") == {"phase": "starting", "reason": "runtime_failure", "phase_ms": 0, "elapsed_ms": 0}


@pytest.mark.parametrize("phase,reason", [
    ("sidecar_start", "sidecar_port_conflict"),
    ("sidecar_health", "sidecar_health_unavailable"),
    ("sidecar_start", "sidecar_binary_missing"),
    ("sidecar_start", "sidecar_permission_denied"),
    ("sidecar_start", "sidecar_startup_exited"),
    ("sidecar_health", "sidecar_protocol_mismatch"),
])
def test_sidecar_classification_requires_exact_source_and_intent(phase, reason):
    from core.runtime_diagnostics import compatible_runtime_diagnostic
    diagnostic = {"phase": phase, "reason": reason, "phase_ms": 0, "elapsed_ms": 46_000}
    assert compatible_runtime_diagnostic(diagnostic, "desktop_sidecar", "start_bridge", "Mac native_failure") == diagnostic
    for source, intent in [("desktop_sidecar", "run_bridge"), ("device_worker", "start_bridge")]:
        assert compatible_runtime_diagnostic(diagnostic, source, intent, "Mac native_failure") is None


def test_closed_shell_reason_survives_ingest_storage_and_error_log(core, caplog):
    from app.api.events_ingest import TelemetryBody
    from app.analytics.usage import capture
    from app.analytics.error_events import log_error_event
    from app.db.models import AnalyticsEvent
    from app.state import db
    summary = _summary()
    summary["props"].update(feature="shell", error_source="desktop_sidecar", error_intent="start_bridge",
        runtime_diagnostic={"phase": "sidecar_start", "reason": "sidecar_binary_missing", "phase_ms": 0, "elapsed_ms": 0},
        reason="sidecar_binary_missing")
    with caplog.at_level(logging.WARNING, logger="app.error_event"):
        result = capture(TelemetryBody.model_validate(summary).model_dump(mode="json"), "usr_runtime")
        log_error_event("client", feature="settings", code="network_error", error_status=0)
        log_error_event("client", feature="settings", code="request_error", error_status=403, reason="private_value")
    with db()() as session:
        assert json.loads(session.get(AnalyticsEvent, result["id"]).props_json)["reason"] == "sidecar_binary_missing"
    lines = [json.loads(record.getMessage().removeprefix("error_event ")) for record in caplog.records
             if record.getMessage().startswith("error_event ")]
    assert [line["reason"] for line in lines] == ["sidecar_binary_missing", "fetch_failed", "request_rejected"]
    summary["props"]["reason"] = "private_value"
    with pytest.raises(ValidationError):
        TelemetryBody.model_validate(summary)
    summary["props"].update(reason="sidecar_binary_missing", runtime_diagnostic=None, feature="settings")
    summary["event"] = "feature.used"
    with pytest.raises(ValidationError):
        TelemetryBody.model_validate(summary)


def test_rotation_is_private_and_keeps_only_one_backup(tmp_path, monkeypatch):
    from desktop_runtime import crash_report
    directory = tmp_path / "logs"
    monkeypatch.setenv("COMPANY_OS_RUNTIME_LOG_DIR", str(directory))
    monkeypatch.setattr(crash_report, "_log_ready", False)
    old_handlers = list(crash_report.log.handlers)
    crash_report._ensure_error_log()
    handler = next(h for h in crash_report.log.handlers if h not in old_handlers)
    handler.maxBytes = 400
    try:
        for _ in range(30):
            handler.emit(logging.LogRecord("runtime", logging.INFO, "", 0, "runtime_phase %s", (json.dumps(DIAGNOSTIC),), None))
        assert {p.name for p in directory.iterdir()} == {"runtime.log", "runtime.log.1"}
        assert all(p.stat().st_size < 600 and p.stat().st_mode & 0o777 == 0o600 for p in directory.iterdir())
        assert directory.stat().st_mode & 0o777 == 0o700
    finally:
        crash_report.log.removeHandler(handler)
        handler.close()


@pytest.mark.parametrize("bad", [
    {**DIAGNOSTIC, "reason": "private_screen_text"}, {**DIAGNOSTIC, "phase": "private_prompt"},
    {**DIAGNOSTIC, "phase_ms": True}, {**DIAGNOSTIC, "phase_ms": -1},
    {**DIAGNOSTIC, "elapsed_ms": 86_400_001}, {**DIAGNOSTIC, "phase_ms": 1.5},
    {**DIAGNOSTIC, "prompt": "private prompt"}, {**DIAGNOSTIC, "phase": ["deciding"]},
    {**DIAGNOSTIC, "phase_ms": 501},
])
def test_private_or_unbounded_metadata_is_rejected_at_ingest(core, bad):
    from app.api.events_ingest import TelemetryBody
    assert closed_runtime_diagnostic(bad) is None
    with pytest.raises(ValidationError):
        TelemetryBody.model_validate(_summary(bad))


@pytest.mark.parametrize("field,value", [("platform", "web"), ("error_source", "other"), ("error_intent", "other"), ("error_text", "Mac command_failed")])
def test_runtime_details_cannot_label_an_unrelated_error(core, field, value):
    from app.api.events_ingest import TelemetryBody
    summary = _summary()
    summary["props"][field] = value
    with pytest.raises(ValidationError):
        TelemetryBody.model_validate(summary)


@pytest.mark.parametrize("diagnostic", [
    {**DIAGNOSTIC, "phase": "sidecar_health"},
    {**DIAGNOSTIC, "reason": "sidecar_health_timeout"},
])
def test_worker_cannot_claim_sidecar_failure_metadata(core, diagnostic):
    from app.api.events_ingest import TelemetryBody
    with pytest.raises(ValidationError):
        TelemetryBody.model_validate(_summary(diagnostic))


@pytest.mark.parametrize("event", ["desktop.process_heartbeat", "ui.control_outcome"])
def test_other_events_cannot_carry_runtime_metadata(core, event):
    from app.api.events_ingest import TelemetryBody
    summary = _summary()
    summary["event"] = event
    summary["props"] = {"feature": "computer_use", "platform": "macos", "session_id": str(uuid4()),
        "runtime_diagnostic": DIAGNOSTIC}
    if event == "ui.control_outcome":
        summary["props"].update({"feature": "settings", "control_id": "settings_manager_rename", "outcome": "ok"})
    clean = {**summary, "props": {k: v for k, v in summary["props"].items() if k != "runtime_diagnostic"}}
    TelemetryBody.model_validate(clean)
    with pytest.raises(ValidationError):
        TelemetryBody.model_validate(summary)


def test_owner_matched_queue_revalidates_tampering_and_preserves_legacy(tmp_path, monkeypatch):
    from desktop_runtime import crash_report
    monkeypatch.setattr(crash_report, "_ensure_error_log", lambda: None)
    crash_report.save_crash_owner("usr_runtime", "dev_a", tmp_path)
    crash_report.report_error("device_worker", "run_mac_task", root=tmp_path, runtime_diagnostic=DIAGNOSTIC)
    [(path, summary)] = crash_report.pending_crash_summaries("usr_runtime", "dev_a", tmp_path)
    assert summary["props"]["runtime_diagnostic"] == DIAGNOSTIC
    assert crash_report.pending_crash_summaries("usr_other", "dev_a", tmp_path) == []
    assert crash_report.pending_crash_summaries("usr_runtime", "dev_other", tmp_path) == []
    saved = json.loads(path.read_text())
    saved.update({"runtime_diagnostic": {**DIAGNOSTIC, "screen": "private content"}, "stack": "private stack"})
    path.write_text(json.dumps(saved))
    [(same_path, legacy)] = crash_report.pending_crash_summaries("usr_runtime", "dev_a", tmp_path)
    assert same_path == path and "runtime_diagnostic" not in legacy["props"]
    assert "private" not in json.dumps(legacy)
    saved.pop("runtime_diagnostic")
    path.write_text(json.dumps(saved))
    assert crash_report.pending_crash_summaries("usr_runtime", "dev_a", tmp_path)[0][1] == legacy


def test_real_ingestion_is_idempotent_and_admin_retains_closed_failure_sample(core):
    from app.api import error_reports_api, events_ingest
    from app.db.models import Person
    from app.state import db
    from core.clock import utcnow
    from app.db.models import AnalyticsEvent
    with db()() as session:
        session.add(Person(id="usr_runtime", display_name="Owned fixture", email="fixture@example.test", created_at=utcnow()))
        session.commit()
    app = FastAPI()
    app.include_router(events_ingest.router)
    app.include_router(error_reports_api.router)
    admin_route = next(r for r in error_reports_api.router.routes if getattr(r, "path", None) == "/admin/errors")
    app.dependency_overrides[admin_route.dependant.dependencies[0].call] = lambda: object()
    summary = _summary()
    with TestClient(app) as client:
        first = client.post("/telemetry", json=summary, headers={"X-Subject": "usr_runtime"})
        replay = client.post("/telemetry", json=summary, headers={"X-Subject": "usr_runtime"})
        assert first.status_code == replay.status_code == 200 and first.json() == replay.json()
        items = client.get("/admin/errors").json()["items"]
    assert len(items) == 1 and items[0]["sample"]["runtime_diagnostic"] == DIAGNOSTIC
    assert items[0]["count"] == 1 and _rows()[0].severity == "error"
    with db()() as session:
        assert session.query(AnalyticsEvent).filter_by(id=first.json()["id"]).count() == 1
    # The optional field is removed when absent, preserving old replay serialization.
    legacy = _summary(None)
    del legacy["props"]["runtime_diagnostic"]
    from app.analytics.usage import capture
    result = capture(events_ingest.TelemetryBody.model_validate(legacy).model_dump(mode="json"), "usr_runtime")
    assert capture(events_ingest.TelemetryBody.model_validate(legacy).model_dump(mode="json"), "usr_runtime") == result
    with db()() as session:
        assert "runtime_diagnostic" not in json.loads(session.get(AnalyticsEvent, result["id"]).props_json)

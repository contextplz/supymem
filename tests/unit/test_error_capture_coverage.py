"""Coverage checklist: every component's error path yields the standard error event shape.

Components and where they are tested:
- desktop shell (Rust): src-tauri/src/error_capture.rs tests + apps/console/src/lib/nativeShellEvents.test.ts
- desktop runtime (this file), gateway / warden / any FastAPI service using core.error_event (this file)
- console server: apps/console/src/lib/errorEvent.test.ts
- core-api / worker: tests/unit/analytics/test_error_attribution.py
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.error_event import ErrorEventMiddleware

ROOT = Path(__file__).resolve().parents[2]
STANDARD = {"app_version", "code", "component", "intent", "source", "status"}


def _lines(caplog) -> list[dict]:
    return [json.loads(r.getMessage().removeprefix("error_event ")) for r in caplog.records
            if r.getMessage().startswith("error_event ")]


@pytest.mark.parametrize("component", ["gateway", "warden"])
def test_service_5xx_and_exceptions_log_standard_event_with_venture(component, caplog, monkeypatch) -> None:
    monkeypatch.setenv("APP_VERSION", "1.2.3")
    app = FastAPI()
    app.add_middleware(ErrorEventMiddleware, component=component)

    @app.post("/public/waitlists/{venture_id}")
    def submit(venture_id: str):
        raise RuntimeError("visitor@example.com leaked")

    @app.get("/down")
    def down():
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "x"}, status_code=502)

    client = TestClient(app, raise_server_exceptions=False)
    with caplog.at_level(logging.ERROR):
        assert client.post("/public/waitlists/ven_abc123", json={"email": "visitor@example.com"}).status_code == 500
        assert client.get("/down").status_code == 502
        assert client.get("/missing").status_code == 404
    waitlist, down_line = _lines(caplog)  # the 404 is not an error_event
    assert STANDARD <= set(waitlist) and waitlist["component"] == component
    assert waitlist["intent"] == "post_public_waitlists_venture_id" and waitlist["venture"] == "ven_abc123"
    assert waitlist["code"] == "RuntimeError" and waitlist["app_version"] == "1.2.3"
    assert down_line["code"] == "http_5xx" and down_line["status"] == 502 and "venture" not in down_line
    assert "visitor@example.com" not in caplog.text


def test_gateway_warden_and_console_install_the_standard_reporter() -> None:
    assert 'component="gateway"' in (ROOT / "services/gateway/app/main.py").read_text()
    assert 'component="warden"' in (ROOT / "services/warden/app/main.py").read_text()
    assert "onRequestError" in (ROOT / "apps/console/src/instrumentation.ts").read_text()


def test_runtime_error_is_logged_and_queued_as_closed_ui_error(tmp_path, monkeypatch, caplog) -> None:
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_APP_VERSION", "0.1.9")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    from desktop_runtime import crash_report

    crash_report.save_crash_owner("usr_a", "dev_a", tmp_path)
    caplog.set_level(logging.ERROR, logger="desktop_runtime.error_event")
    crash_report.report_error("device_worker", "run_mac_task", code="desktop_cua_window_target_not_found")
    crash_report.report_error("Bad Source/path", "run_mac_task", label="not a label")
    logged = [json.loads(r.getMessage().removeprefix("error_event ")) for r in caplog.records]
    assert logged[0]["component"] == "desktop_runtime" and logged[0]["source"] == "device_worker"
    assert logged[0]["intent"] == "run_mac_task" and logged[0]["app_version"] == "0.1.9"
    assert logged[1]["source"] == "other"
    assert (crash_report._error_log_directory() / "runtime.log").is_file()

    summaries = sorted((body for _, body in crash_report.pending_crash_summaries("usr_a", "dev_a", tmp_path)),
                       key=lambda body: body["props"]["error_source"])  # queue order is by timestamp+random id
    assert len(summaries) == 2
    props = summaries[0]["props"]  # "device_worker" sorts before "other"
    assert (props["error_source"], props["error_intent"], props["error_text"]) == ("device_worker", "run_mac_task", "Mac native_failure")
    assert props["error_context"] == "native" and props["app_version"] == "0.1.9"
    assert summaries[1]["props"]["error_source"] == "other" and summaries[1]["props"]["error_text"] == "Mac native_failure"
    # Same closed schema core enforces on /telemetry.
    from tests.company_os_harness import load_company_os_app

    load_company_os_app(reset=False)  # puts services/company_os first on sys.path
    from app.api.events_ingest import TelemetryBody

    for body in summaries:
        TelemetryBody.model_validate(body)


def test_uncaught_runtime_exception_carries_source_and_intent(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    from desktop_runtime import crash_report

    crash_report.save_crash_owner("usr_a", "dev_a", tmp_path)
    crash_report.enqueue_crash(stack="Traceback boom", root=tmp_path)
    (_, body), = crash_report.pending_crash_summaries("usr_a", "dev_a", tmp_path)
    assert body["props"]["error_text"] == "Mac uncaught_exception"
    assert (body["props"]["error_source"], body["props"]["error_intent"]) == ("desktop_runtime", "run_runtime")


@pytest.mark.skipif(not (ROOT / "scripts/infra/configure_error_alarm.py").is_file(),
                    reason="The public export excludes hosted alarm tooling.")
def test_error_alarm_covers_every_prod_service_log_group_and_uses_existing_topic() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("configure_error_alarm", ROOT / "scripts/infra/configure_error_alarm.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = module.plan(threshold=5, topic_arn="arn:aws:sns:us-east-1:1:t", email=None)
    filters = [kw for name, kw in calls if name == "put_metric_filter"]
    assert {f["logGroupName"] for f in filters} == {
        f"/ecs/selfhost-os-prod-{n}" for n in ("core-api", "worker-general", "warden", "console", "gateway")}
    assert all(f["filterPattern"] == '"error_event"' for f in filters)
    (alarm,) = [kw for name, kw in calls if name == "put_metric_alarm"]
    assert alarm["AlarmActions"] == ["arn:aws:sns:us-east-1:1:t"] and alarm["Threshold"] == 5.0

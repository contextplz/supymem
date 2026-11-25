"""The signed native summary retains closed timing metadata in migrated PostgreSQL."""
import json
from uuid import uuid4

from tests.integration.test_postgres_journey_jobs import journey_db as journey_db
from tests.integration.test_postgres_auth_security import pg_database as pg_database


def test_signed_runtime_summary_is_private_idempotent_and_visible_to_admin(journey_db, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from core.assertion import sign_assertion
    from desktop_runtime import crash_report
    from app.api import error_reports_api
    from app.db.models import AnalyticsEvent
    from app.db.supymem_models import ErrorReport
    from app.error_reports import service as reports

    actor = journey_db["actor"]
    secret = "runtime-diagnostic-fixture-secret-more-than-32-bytes"
    for key, value in {"COS_INTERNAL_ASSERTION_SECRET": secret, "ADMIN_REQUIRE_TOTP": "1",
                       "ADMIN_BOOTSTRAP_EMAILS": "ops@example.test",
                       "COMPANY_OS_VAULT_SECRET": "runtime-fixture-vault-secret-more-than-32-bytes",
                       "COMPANY_OS_APP_VERSION": "0.1.84"}.items():
        monkeypatch.setenv(key, value)
    from app.main import create_app
    monkeypatch.setattr(crash_report, "_ensure_error_log", lambda: None)
    monkeypatch.setattr(reports, "send_alert", lambda *_: None)
    diagnostic = {"phase": "deciding", "reason": "cu_browser_decision_unavailable", "phase_ms": 120, "elapsed_ms": 500}
    crash_report.save_crash_owner(actor, "dev_runtime_fixture", tmp_path)
    crash_report.report_error("device_worker", "run_mac_task", code="cu_browser_decision_unavailable",
                              runtime_diagnostic=diagnostic, root=tmp_path)
    (path, event), = crash_report.pending_crash_summaries(actor, "dev_runtime_fixture", tmp_path)
    raw = json.loads(path.read_text())
    path.write_text(json.dumps({**raw, "stack": "PRIVATE_RUNTIME_STACK", "logs": "PRIVATE_RUNTIME_SCREEN"}))
    (_, event), = crash_report.pending_crash_summaries(actor, "dev_runtime_fixture", tmp_path)
    assert "PRIVATE_RUNTIME" not in json.dumps(event)
    assert crash_report.pending_crash_summaries("usr_other", "dev_runtime_fixture", tmp_path) == []
    assert crash_report.pending_crash_summaries(actor, "dev_other", tmp_path) == []
    app = create_app()
    route = next(r for r in error_reports_api.router.routes if getattr(r, "path", None) == "/admin/errors")
    # Exercise the real admin response; admin session/TOTP authentication remains its existing separate gate.
    app.dependency_overrides[route.dependant.dependencies[0].call] = lambda: object()
    client = TestClient(app)

    def headers(path="/telemetry", method="POST", subject=actor):
        return {"X-COS-Assertion": sign_assertion(secret, sub=subject, email="", role="user",
            path=path, method=method, device_id="dev_runtime_fixture")}

    assert client.post("/telemetry", headers={"X-Subject": actor}, json=event).status_code == 401
    assert client.post("/telemetry", headers=headers(subject="usr_other"), json=event).status_code == 409
    first = client.post("/telemetry", headers=headers(), json=event)
    assert first.status_code == 200, first.text
    assert client.post("/telemetry", headers=headers(), json=event).json() == first.json()
    malformed = {**event, "event_id": str(uuid4()), "props": {**event["props"],
        "runtime_diagnostic": {**diagnostic, "screen": "PRIVATE_RUNTIME_SCREEN"}}}
    assert client.post("/telemetry", headers=headers(), json=malformed).status_code == 422
    conflict = {**event, "props": {**event["props"], "runtime_diagnostic": {**diagnostic, "phase_ms": 121}}}
    assert client.post("/telemetry", headers=headers(), json=conflict).status_code == 409
    response = client.get("/admin/errors", headers=headers("/admin/errors", "GET"))
    assert response.status_code == 200, response.text
    (item,) = [row for row in response.json()["items"] if row["code"] == "cu_browser_decision_unavailable"]
    assert item["count"] == 1 and item["sample"]["runtime_diagnostic"] == diagnostic
    with journey_db["factory"]() as session:
        (row,) = session.query(AnalyticsEvent).filter_by(event_name="ui.error", person_id=actor).all()
        assert json.loads(row.props_json)["runtime_diagnostic"] == diagnostic and "PRIVATE_RUNTIME" not in row.props_json
        (report,) = session.query(ErrorReport).filter_by(code="cu_browser_decision_unavailable").all()
        assert json.loads(report.sample_json)["runtime_diagnostic"] == diagnostic

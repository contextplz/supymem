"""Native process uptime cannot be forged by the hosted web client."""

from __future__ import annotations

from uuid import uuid4

from core.assertion import MIN_SECRET_BYTES, sign_assertion
from fastapi.testclient import TestClient

from tests.company_os_harness import load_company_os_app

import pytest

pytestmark = pytest.mark.hosted_only


SECRET = "r" * MIN_SECRET_BYTES
ACTOR = "usr_runtime_device"


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/runtime-telemetry.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", SECRET)
    monkeypatch.setenv("COMPANY_OS_REQUIRE_SIGNED_CALLER", "0")
    monkeypatch.setenv("ADMIN_REQUIRE_TOTP", "0")
    main = load_company_os_app()
    from app import state
    from app.authz.workspace import ensure_subject_person

    state.reset_db_factory()
    state.db()
    ensure_subject_person(ACTOR)
    from app.db.supymem_models import DesktopDevice
    with state.db()() as session:
        session.add(DesktopDevice(id="dev_runtime", person_id=ACTOR, name="Runtime test", platform="macos"))
        session.commit()
    return TestClient(main.create_app())


def _payload() -> dict:
    return {
        "event": "desktop.process_heartbeat",
        "event_id": str(uuid4()),
        "context_subject": ACTOR,
        "workspace_id": None,
        "venture_id": None,
        "props": {
            "feature": "settings",
            "platform": "macos",
            "app_version": "0.1.48",
            "session_id": str(uuid4()),
        },
    }


def _assertion(*, device_id: str | None) -> str:
    return sign_assertion(
        SECRET,
        sub=ACTOR,
        email="runtime@example.test",
        role="user",
        path="/telemetry",
        method="POST",
        device_id=device_id,
    )


def test_runtime_heartbeat_requires_device_bound_assertion(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        legacy = client.post("/telemetry", headers={"X-Subject": ACTOR}, json=_payload())
        assert legacy.status_code == 401

        browser = client.post(
            "/telemetry", headers={"X-COS-Assertion": _assertion(device_id=None)}, json=_payload()
        )
        assert browser.status_code == 403
        assert browser.json()["detail"]["error"]["code"] == "authenticated_device_required"

        accepted = client.post(
            "/telemetry", headers={"X-COS-Assertion": _assertion(device_id="dev_runtime")}, json=_payload()
        )
        assert accepted.status_code == 200, accepted.text

        from app.db.models import AnalyticsEvent
        from app.state import db

        with db()() as session:
            rows = session.query(AnalyticsEvent).filter_by(event_name="desktop.process_heartbeat").all()
            assert len(rows) == 1
            assert rows[0].person_id == ACTOR
            assert rows[0].workspace_id is None and rows[0].venture_id is None
            from app.db.supymem_models import DesktopDevice
            device = session.get(DesktopDevice, "dev_runtime")
            assert device.app_version == "0.1.48" and device.last_seen_at is not None


def test_runtime_version_cannot_update_unknown_foreign_or_revoked_device(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        from app.db.supymem_models import DesktopDevice
        from app.state import db
        from core.clock import utcnow
        with db()() as session:
            session.add(DesktopDevice(id="dev_foreign", person_id="other", name="Foreign", platform="macos"))
            session.add(DesktopDevice(id="dev_revoked", person_id=ACTOR, name="Revoked", platform="macos", revoked_at=utcnow()))
            session.commit()
        for device in ("dev_unknown", "dev_foreign", "dev_revoked"):
            result = client.post("/telemetry", headers={"X-COS-Assertion": _assertion(device_id=device)}, json=_payload())
            assert result.status_code == 403
        with db()() as session:
            assert session.get(DesktopDevice, "dev_foreign").app_version is None
            assert session.get(DesktopDevice, "dev_revoked").app_version is None


def test_closed_native_crash_receipt_is_accepted_without_raw_details(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        payload = _payload()
        payload["event"] = "ui.error"
        payload["props"].update({"error_code": "native_error", "error_context": "native",
            "error_text": "Mac panic_observed"})
        headers = {"X-COS-Assertion": _assertion(device_id="dev_runtime")}
        accepted = client.post("/telemetry", headers=headers, json=payload)
        assert accepted.status_code == 200, accepted.text
        retry = client.post("/telemetry", headers={"X-COS-Assertion": _assertion(device_id="dev_runtime")}, json=payload)
        assert retry.status_code == 200 and retry.json() == accepted.json()
        payload["event_id"] = str(uuid4())
        payload["props"]["error_text"] = "Mac uncaught_exception"
        python = client.post("/telemetry", headers={"X-COS-Assertion": _assertion(device_id="dev_runtime")}, json=payload)
        assert python.status_code == 200, python.text
        payload["event_id"] = str(uuid4())
        payload["props"]["error_text"] = "Mac panic_observed: secret stack"
        rejected = client.post("/telemetry", headers={"X-COS-Assertion": _assertion(device_id="dev_runtime")}, json=payload)
        assert rejected.status_code == 422


def test_shell_error_carries_source_and_intent(tmp_path, monkeypatch) -> None:
    """Standard shape for every native-shell error id the Console maps (see nativeShellEvents.ts)."""
    with _client(tmp_path, monkeypatch) as client:
        payload = _payload()
        payload["event"] = "ui.error"
        payload["props"].update({"feature": "shell", "error_code": "native_error", "error_context": "native",
            "error_text": "Mac process_crashed", "error_source": "desktop_shell", "error_intent": "previous_session"})
        assert client.post("/telemetry", headers={"X-COS-Assertion": _assertion(device_id="dev_runtime")}, json=payload).status_code == 200
        from app.db.models import AnalyticsEvent
        from app.state import db

        with db()() as session:
            row = session.query(AnalyticsEvent).filter_by(event_name="ui.error").one()
            assert '"error_source": "desktop_shell"' in row.props_json and '"error_intent": "previous_session"' in row.props_json
        payload["event_id"] = str(uuid4())
        payload["props"]["error_source"] = "Has Spaces/path"
        assert client.post("/telemetry", headers={"X-COS-Assertion": _assertion(device_id="dev_runtime")}, json=payload).status_code == 422

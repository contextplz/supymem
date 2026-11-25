import json
import threading
import time
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from desktop_runtime import device_journal
from desktop_runtime.bridge import create_bridge_app
from desktop_runtime.device_worker import DeviceWorker
from tests.unit.product.test_device_order_api import api, headers
from tests.unit.product.test_device_orders import orders


@pytest.fixture
def recovery(api, monkeypatch, tmp_path):
    server, (service, caller, manifest) = api
    secret = "owned-reconciliation-test-secret-over-32-characters"
    native_headers = {"x-native-token": secret}
    for key, value in {"COMPANY_OS_DATA_DIR": str(tmp_path), "BRIDGE_TOKEN": "bridge-test",
                       "BRIDGE_AUTH_OWNER": "native", "BRIDGE_NATIVE_TOKEN": secret,
                       "GATEWAY_URL": "https://gateway.example.test",
                       "SUPYMEM_NATIVE_BROWSER_ENABLED": "1", "DEVICE_BROWSER_EXECUTION_ENABLED": "1",
                       "JEV_DEVICE_DECISIONS_ENABLED": "1", "TYPESAFE_API_KEY": "offline-test"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("COMPANY_OS_PUBLIC_URL", raising=False)
    monkeypatch.setattr("desktop_runtime.device_resume.start_resume_monitor", lambda *_args: None)
    workers, threads = [], []
    released, finished = threading.Event(), threading.Event()
    failures = []

    def transport(request):
        if request.url.path == "/auth/session":
            return httpx.Response(200, json={"authenticated": True, "subject": "usr_one"})
        if request.url.path == "/api/telemetry":
            return httpx.Response(200, json={"id": "usage_test"})
        assert request.headers["authorization"] == "Bearer native-test"
        if request.url.path.endswith("/stop-receipt") and not failures:
            failures.append(json.loads(request.content))
            return httpx.Response(500, json={"error": "receipt_unavailable"})
        path = request.url.path.removeprefix("/api")
        response = server.request(request.method, path, headers=headers(path, request.method),
                                  json=json.loads(request.content) if request.content else None)
        return httpx.Response(response.status_code, content=response.content)

    client = TestClient(create_bridge_app(transport=httpx.MockTransport(transport)))
    assert client.post("/native/session", headers=native_headers, json={
        "account_id": "usr_one", "device_id": "dev_one", "access_token": "native-test",
        "expires_at": time.time() + 900}).status_code == 200

    def start(title, *, pause=False, uncertain_usage=False):
        saved = service.prepare("v_one", caller, manifest.model_copy(update={"title": title}))
        service.transition("v_one", caller, saved["id"], saved["review_digest"], "approve")

        def run(worker):
            workers.append(worker)
            threads.append(threading.current_thread())
            if worker.gateway.order_id != saved["id"]:
                worker.closed = True
                worker.progress("not_started", task_verified=False)
                return
            claim = worker.gateway.claim()
            if uncertain_usage:
                from app.build import device_invocations
                with service.db()() as session:
                    row, body, _account = device_invocations._locked(
                        session, "v_one", caller, saved["id"], receipt_only=True)
                    body["invocations"] = {"call_test": {"state": "uncertain", "quote_micro": 20,
                                                         "started_at": claim["claimed_at"]}}
                    row.provenance_json = json.dumps(body)
                    session.commit()
            worker.progress("reconciliation_required", tool="browser", task_verified=False,
                credits_reconciled=False, evidence_digest="a" * 64,
                evidence={"execution_id": claim["execution_id"], "processes_stopped": False})
            try:
                worker.gateway.stop_receipt("a" * 64, False)
            except (PermissionError, RuntimeError):
                pass
            finished.set()
            if pause:
                assert released.wait(120)

        monkeypatch.setattr(DeviceWorker, "run", run)
        base = f"/native/device-work-orders/v_one/{saved['id']}"
        response = client.post(base + "/start", headers=native_headers)
        assert response.status_code == 200, response.text
        assert finished.wait(3)
        if not pause:
            threads[0].join(3)
            assert not threads[0].is_alive()
        return saved, base, workers[0]

    next_saved = None
    def next_task():
        nonlocal next_saved
        if next_saved is None:
            next_saved = service.prepare("v_one", caller, manifest.model_copy(update={"task_id": "task_next"}))
            service.transition("v_one", caller, next_saved["id"], next_saved["review_digest"], "approve")
        return client.post(f"/native/device-work-orders/v_one/{next_saved['id']}/start", headers=native_headers)

    yield client, native_headers, service, caller, start, next_task, released, threads, failures
    released.set()
    for thread in threads:
        thread.join(3)
    client.close()


@pytest.mark.parametrize("title", ["Freight scheduling", "Meal planning"])
@pytest.mark.parametrize("uncertain_usage", [False, True])
@pytest.mark.parametrize("refresh", ["order_status", "bridge_status", "heartbeat", "new_start"])
def test_failed_stop_receipt_acceptance_clears_local_lock(recovery, title, uncertain_usage, refresh):
    client, native_headers, service, caller, start, next_task, _released, _threads, failures = recovery
    saved, base, worker = start(title, uncertain_usage=uncertain_usage)
    assert len(failures) == 1 and failures[0]["processes_stopped"] is False
    assert not worker.closed
    assert client.get(base + "/status", headers=native_headers).json()["state"] == "reconciliation_required"
    receipt = worker.gateway.stop_receipt("a" * 64, True)
    assert receipt["state"] == ("claimed" if uncertain_usage else "stopped")
    evidence = worker.last["evidence"]
    worker.last["evidence"] = {**evidence, "execution_id": "dex_other"}
    device_journal.save(("https://gateway.example.test", "usr_one", "dev_one"), "v_one", saved["id"], worker.last)
    assert client.get(base + "/status", headers=native_headers).json()["state"] == "reconciliation_required"
    assert not worker.closed
    worker.last["evidence"] = evidence
    device_journal.save(("https://gateway.example.test", "usr_one", "dev_one"), "v_one", saved["id"], worker.last)
    if not uncertain_usage:
        worker.last["task_verified"] = True
        device_journal.save(("https://gateway.example.test", "usr_one", "dev_one"), "v_one", saved["id"], worker.last)
    if refresh == "order_status":
        assert client.get(base + "/status", headers=native_headers).status_code == 200
    elif refresh == "bridge_status":
        assert client.get("/status", headers={"x-bridge-token": "bridge-test"}).status_code == 200
    elif refresh == "heartbeat":
        assert client.post("/native/telemetry/runtime", headers=native_headers, json={
            "event_id": str(uuid4()), "session_id": str(uuid4()), "app_version": "0.1.84"}).status_code == 200
    else:
        assert next_task().status_code == 200
    assert worker.closed and worker.last["state"] == "stopped"
    assert worker.last["credits_reconciled"] is not uncertain_usage
    local = device_journal.read(("https://gateway.example.test", "usr_one", "dev_one"), "v_one", saved["id"])
    assert local == worker.last and local["task_verified"] is False
    if uncertain_usage:
        server_order = service.retrieve("v_one", caller, saved["id"])
        assert server_order["invocations"]["call_test"]["state"] == "uncertain"
        assert server_order["stop_reconciliation_required"] is True
    if refresh != "new_start":
        assert next_task().status_code == 200


@pytest.mark.parametrize("title", ["Freight scheduling", "Meal planning"])
@pytest.mark.parametrize("missing_evidence", [False, True])
@pytest.mark.hosted_only
def test_founder_clear_unblocks_only_stopped_worker_and_preserves_uncertain_usage(recovery, title, missing_evidence):
    client, native_headers, service, caller, start, next_task, released, threads, _failures = recovery
    saved, base, worker = start(title, pause=True, uncertain_usage=True)
    assert worker.gateway.stop_receipt("a" * 64, False)["state"] == "claimed"
    assert client.get(base + "/status", headers=native_headers).json()["state"] == "reconciliation_required"
    blocked = next_task()
    assert blocked.status_code == 409 and "native_executor_busy_or_unreconciled" in blocked.text
    acknowledged = worker.gateway.request("POST", worker.gateway.path + "/acknowledge",
                                         {"review_digest": saved["review_digest"]})
    assert acknowledged["reviewed_at"] and acknowledged["reviewed_open_reconciliation"] is True
    assert client.get(base + "/status", headers=native_headers).json()["state"] == "reconciliation_required"
    assert not worker.closed
    released.set()
    threads[0].join(3)
    if missing_evidence:
        worker.last.pop("evidence")
        device_journal.save(("https://gateway.example.test", "usr_one", "dev_one"), "v_one", saved["id"], worker.last)
    status = client.get(base + "/status", headers=native_headers)
    assert status.status_code == 200 and status.json()["state"] == "stopped"
    assert worker.closed and worker.last["reconciliation_reviewed_at"] == acknowledged["reviewed_at"]
    assert worker.last["credits_reconciled"] is False and worker.last["task_verified"] is False
    assert worker.last["actual_usage_unconfirmed"] and worker.last["effect_outcome_unconfirmed"]
    server_order = service.retrieve("v_one", caller, saved["id"])
    assert server_order["state"] == "claimed" and server_order["stop_reconciliation_required"] is True
    assert server_order["invocations"]["call_test"]["state"] == "uncertain"
    from app.db.models import CreditReservation
    from sqlalchemy import select
    with service.db()() as session:
        hold = session.scalar(select(CreditReservation).where(CreditReservation.request_id == saved["id"]))
        assert hold.status == "reconciling" and hold.settled_micro is None
    assert next_task().status_code == 200

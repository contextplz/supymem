"""Settled previews reuse the installed native order transport without executing work."""
from copy import deepcopy
import hashlib
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from desktop_runtime import device_journal, device_worker, loop_checks, owned_deploy
from desktop_runtime.bridge import create_bridge_app
from desktop_runtime.device_gateway import WorkOrderGateway
from desktop_runtime.device_worker import DeviceWorker


DOMAINS = ["Warehouse dispatch", "Home fitness"]


def settled_order(title, provider):
    build = {"outcome": "pass", "provider": provider, "source_commit": "a" * 40,
        "file_manifest_sha256": "b" * 64, "artifact_verified": True, "task_verified": True}
    evidence = {"order_id": "order-one", "execution_id": "execution-one",
        "processes_stopped": True, "task_verified": True, "product_build": build}
    digest = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    receipt = {"order_id": "order-one", "state": "stopped", "task_verified": True,
        "credits_reconciled": True, "evidence": evidence, "evidence_digest": digest}
    order = {"id": "order-one", "venture_id": "venture-one", "device_id": "device-one",
        "actor_id": "usr_founder", "review_digest": "c" * 64, "state": "stopped",
        "claimed_at": "2026-10-27T00:00:00Z", "closed_at": "2026-10-27T00:01:00Z",
        "execution_id": "execution-one", "native_stop": {"execution_id": "execution-one",
            "evidence_digest": digest, "task_verified": True, "processes_stopped": True},
        "manifest": {"title": title, "coding_loop_verification": {"profile": "web_app_vercel_v1",
            "phase": "implement", "provider": provider, "workspace_slug": "reviewed-app"}}}
    return order, receipt


@pytest.fixture
def transport(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    owners, events, packets = [], [], []
    token = "native-preview-fixture-token-" * 2
    for key, value in {"COMPANY_OS_DATA_DIR": str(tmp_path), "BRIDGE_TOKEN": "local-fixture-secret",
            "BRIDGE_AUTH_OWNER": "native", "BRIDGE_NATIVE_TOKEN": token,
            "GATEWAY_URL": "https://gateway.invalid", "SUPYMEM_NATIVE_BROWSER_ENABLED": "1"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("COMPANY_OS_PUBLIC_URL", raising=False)

    class Preview:
        def __init__(self, *, workspace, build, closed):
            assert closed["processes_stopped"] and closed["task_verified"]
            self.build, self.receipt = build, {"status": "unavailable", "ready": False}
            self.cleanup = True
            owners.append(self)

        def start(self):
            events.append("preview-start")
            self.receipt = {"status": "ready", "ready": True, "stop_available": True,
                "source_commit": self.build["source_commit"], "file_manifest_sha256": self.build["file_manifest_sha256"],
                "url": "http://127.0.0.1:43127/", "expires_at": time.time() + 600}
            return self.status()

        def status(self):
            return dict(self.receipt)

        def stop(self, *, status="stopped"):
            events.append("preview-stop")
            self.receipt.update(status=status if self.cleanup else "failed", ready=False,
                processes_stopped=self.cleanup, stop_available=not self.cleanup)
            self.receipt.pop("url", None)
            return self.status()

    monkeypatch.setattr(loop_checks, "OwnedProductPreview", Preview)
    monkeypatch.setattr(device_worker.OwnedCoding, "_workspace", lambda self: tmp_path)
    monkeypatch.setattr(device_worker.OwnedCoding, "run", lambda *args, **kwargs: pytest.fail("coding must not restart"))
    monkeypatch.setattr(owned_deploy.OwnedDeploy, "run", lambda *args: pytest.fail("deployment must not start"))
    monkeypatch.setattr(DeviceWorker, "run", lambda *args: pytest.fail("worker must not execute"))
    monkeypatch.setattr(DeviceWorker, "_notify_terminal", lambda *args, **kwargs: None)
    monkeypatch.setattr(DeviceWorker, "_runtime_cleanup", lambda *args: None)
    monkeypatch.setattr(device_journal, "create", lambda *args, **kwargs: pytest.fail("must retain settled journal"))

    def open_client(title, provider):
        order, receipt = settled_order(title, provider)
        local = {"receipt": receipt}
        response_status = {"value": 200}
        monkeypatch.setattr(device_journal, "read", lambda *args: deepcopy(local["receipt"]))
        monkeypatch.setattr(device_journal, "save", lambda *args: local.update(receipt=deepcopy(args[-1])))

        def request(message):
            packets.append((message.method, message.url.path))
            assert message.method == "GET", "preview never claims, cancels Core, or deploys"
            return httpx.Response(response_status["value"], json=order)

        app = create_bridge_app(transport=httpx.MockTransport(request))
        session = app.state.bridge_session
        session.account_id, session.device_id = "usr_founder", "device-one"
        session.access_token, session.connected, session.expires_at = "fixture", True, time.time() + 900
        client = TestClient(app)
        return SimpleNamespace(app=app, client=client, order=order, local=local, session=session,
            response_status=response_status,
            headers={"x-native-token": token}, path="/native/device-work-orders/venture-one/order-one")

    return SimpleNamespace(open=open_client, owners=owners, events=events, packets=packets)


@pytest.mark.parametrize("title", DOMAINS)
@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_existing_start_status_cancel_own_preview_and_expiry_only(transport, title, provider):
    fixture = transport.open(title, provider)
    with fixture.client as client:
        assert client.post(fixture.path + "/start").status_code == 401
        result = client.post(fixture.path + "/start", headers=fixture.headers)
        assert result.status_code == 200
        receipt = result.json()
        assert receipt["state"] == "stopped" and receipt["credits_reconciled"] is True
        assert receipt["product_preview"]["transport"] == "device_order_v1"
        assert receipt["product_preview"]["source_commit"] == receipt["evidence"]["product_build"]["source_commit"]
        assert client.post(fixture.path + "/start", headers=fixture.headers).status_code == 200
        assert len(transport.owners) == 1
        assert client.get(fixture.path + "/status", headers=fixture.headers).json()["product_preview"]["ready"]
        transport.owners[0].stop(status="expired")
        expired = client.get(fixture.path + "/status", headers=fixture.headers).json()["product_preview"]
        assert expired["status"] == "expired" and "url" not in expired
        assert client.post(fixture.path + "/start", headers=fixture.headers).json()["product_preview"]["ready"]
        stopped = client.post(fixture.path + "/cancel", headers=fixture.headers).json()
        assert stopped["state"] == "stopped" and stopped["task_verified"] is True
        assert stopped["product_preview"]["processes_stopped"] is True
        assert "url" not in stopped["product_preview"]
        assert client.post(fixture.path + "/start", headers=fixture.headers).json()["product_preview"]["ready"]
    assert transport.owners[-1].status()["processes_stopped"] is True


@pytest.mark.parametrize("title", DOMAINS)
def test_saved_browser_result_folder_requires_the_reviewed_venture_marker(tmp_path, monkeypatch, title):
    from tests.company_os_harness import load_company_os_app

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    load_company_os_app()
    credential = b"test-only-local-credential"
    monkeypatch.setattr("core.local_security._proxy_secret", lambda: credential)
    order, receipt = settled_order(title, "codex")
    monkeypatch.setattr(device_journal, "read", lambda *_: deepcopy(receipt))
    app = create_bridge_app(local_core="http://127.0.0.1:3300",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=order)))
    session = app.state.bridge_session
    session.account_id, session.device_id = order["actor_id"], order["device_id"]
    session.access_token, session.connected = credential.decode(), True
    status = next(route.endpoint for route in app.routes if route.name == "native_order_status")
    workspace = tmp_path / "workspaces" / order["manifest"]["coding_loop_verification"]["workspace_slug"]
    workspace.mkdir(parents=True)
    marker = workspace / ".supymem-venture"
    marker.write_text("other-venture")
    assert "result_folder" not in status(order["venture_id"], order["id"])
    marker.write_text(order["venture_id"])
    assert status(order["venture_id"], order["id"])["result_folder"] == str(workspace)
    order["stop_reconciliation_required"] = True
    assert "result_folder" not in status(order["venture_id"], order["id"])


@pytest.mark.parametrize("title", DOMAINS)
def test_finished_build_stays_closing_until_executor_can_start_preview(transport, title):
    fixture = transport.open(title, "codex")
    with fixture.client as client:
        assert client.post(fixture.path + "/start", headers=fixture.headers).status_code == 200
        active = fixture.app.state.local_active_worker
        active["worker"].stop_product_preview()
        alive = True
        active["thread"] = SimpleNamespace(is_alive=lambda: alive, join=lambda **options: None)
        receipt = client.get(fixture.path + "/status", headers=fixture.headers).json()
        assert receipt["state"] == "closing" and receipt["task_verified"] is True
        assert client.post(fixture.path + "/start", headers=fixture.headers).status_code == 409
        alive = False
        assert client.get(fixture.path + "/status", headers=fixture.headers).json()["state"] == "stopped"
        assert client.post(fixture.path + "/start", headers=fixture.headers).json()["product_preview"]["ready"]


@pytest.mark.parametrize("title", DOMAINS)
@pytest.mark.parametrize("failure", ["credits", "verification", "digest", "device", "execution",
    "closure", "phase", "provider", "usage", "journal", "lost-custody", "lost-failed", "lost-expired", "signed-out"])
def test_preview_transport_refuses_unsettled_or_unowned_source(transport, title, failure):
    fixture = transport.open(title, "codex")
    local, order = fixture.local["receipt"], fixture.order
    if failure == "credits": local["credits_reconciled"] = False
    if failure == "verification": local["task_verified"] = False
    if failure == "digest": local["evidence_digest"] = "d" * 64
    if failure == "device": order["device_id"] = "other-device"
    if failure == "execution": order["execution_id"] = "other-execution"
    if failure == "closure": order["closed_at"] = None
    if failure == "phase": order["manifest"]["coding_loop_verification"]["phase"] = "plan"
    if failure == "provider": order["manifest"]["coding_loop_verification"]["provider"] = "claude"
    if failure == "usage": local["actual_usage_unconfirmed"] = True
    if failure == "journal": local["journal_unconfirmed"] = True
    if failure.startswith("lost-"): local["product_preview"] = {"status": {
        "lost-custody": "ready", "lost-failed": "failed", "lost-expired": "expired"}[failure], "ready": True,
        "url": "http://127.0.0.1:43127/", "stop_available": True, "transport": "device_order_v1"}
    if failure == "signed-out": fixture.session.connected = False
    with fixture.client as client:
        result = client.post(fixture.path + "/start", headers=fixture.headers)
        assert result.status_code in {401, 409}
        if failure.startswith("lost-"):
            preview = client.get(fixture.path + "/status", headers=fixture.headers).json()["product_preview"]
            assert preview["status"] == "expired" and preview["ready"] is False and "url" not in preview
            assert preview["stop_available"] is False
    assert not transport.owners


@pytest.mark.parametrize("title", DOMAINS)
@pytest.mark.parametrize("failure", ["start-journal", "marker-journal", "status-journal"])
def test_preview_journal_failures_do_not_leave_an_unstoppable_ready_link(transport, monkeypatch, title, failure):
    fixture = transport.open(title, "codex")
    saves = 0

    def save(*args):
        nonlocal saves
        saves += 1
        if saves >= (2 if failure == "marker-journal" else 1):
            raise OSError("journal unavailable")
        fixture.local["receipt"] = deepcopy(args[-1])

    with fixture.client as client:
        if failure == "status-journal":
            assert client.post(fixture.path + "/start", headers=fixture.headers).status_code == 200
            transport.owners[0].stop(status="expired")
        monkeypatch.setattr(device_journal, "save", save)
        if failure != "status-journal":
            assert client.post(fixture.path + "/start", headers=fixture.headers).status_code == 409
        receipt = client.get(fixture.path + "/status", headers=fixture.headers).json()
        assert receipt["product_preview"]["ready"] is False and "url" not in receipt["product_preview"]
        assert receipt["journal_unconfirmed"] is True
        assert transport.owners[0].status()["processes_stopped"] is True


@pytest.mark.parametrize("title", DOMAINS)
@pytest.mark.parametrize("custody", ["owned", "unclean-stop", "lost-custody"])
def test_another_order_cannot_replace_preview_custody_before_clean_stop(transport, title, custody):
    fixture = transport.open(title, "claude")
    if custody == "lost-custody":
        fixture.local["receipt"]["product_preview"] = {"status": "ready", "ready": True, "stop_available": True,
            "url": "http://127.0.0.1:43127/"}
    with fixture.client as client:
        first = client.post(fixture.path + "/start", headers=fixture.headers)
        assert first.status_code == (409 if custody == "lost-custody" else 200)
        if custody == "unclean-stop": transport.owners[0].cleanup = False
        order, receipt = settled_order(title, "claude")
        order["id"], receipt["order_id"] = "order-two", "order-two"
        receipt["evidence"]["order_id"] = "order-two"
        digest = hashlib.sha256(json.dumps(receipt["evidence"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        receipt["evidence_digest"] = order["native_stop"]["evidence_digest"] = digest
        fixture.order.update(order)
        fixture.local["receipt"] = receipt
        second = client.post(fixture.path.replace("order-one", "order-two") + "/start", headers=fixture.headers)
        assert second.status_code == (200 if custody == "owned" else 409)
        if custody == "owned":
            assert transport.events == ["preview-start", "preview-stop", "preview-start"]
            assert transport.owners[0].status()["processes_stopped"] is True
        if custody == "unclean-stop": transport.owners[0].cleanup = True


@pytest.mark.parametrize("title", DOMAINS)
@pytest.mark.parametrize("retirement", ["sign-out", "cancelled", "digest", "usage", "emergency", "unclean-stop"])
def test_live_preview_retires_without_touching_core_execution(transport, title, retirement):
    fixture = transport.open(title, "claude")
    with fixture.client as client:
        assert client.post(fixture.path + "/start", headers=fixture.headers).status_code == 200
        owner = transport.owners[0]
        if retirement == "sign-out": fixture.session.clear()
        elif retirement in {"emergency", "unclean-stop"}:
            owner.cleanup = retirement != "unclean-stop"
            result = client.post("/native/emergency-stop", headers=fixture.headers, json={"source": "app"}).json()
            assert result["stopped"] is owner.cleanup
            if not owner.cleanup:
                assert result["product_preview"]["stop_available"] is True
                owner.cleanup = True
        else:
            if retirement == "cancelled": fixture.order["state"] = "cancelled"
            if retirement == "digest": fixture.order["native_stop"]["evidence_digest"] = "e" * 64
            if retirement == "usage": fixture.order["unknown_usage_reconciliation"] = {"actual_usage_unconfirmed": True}
            result = client.get(fixture.path + "/status", headers=fixture.headers).json()["product_preview"]
            assert result["ready"] is False and "url" not in result
        assert not owner.status()["ready"]
    assert owner.status()["processes_stopped"] is True


@pytest.mark.parametrize("title", DOMAINS)
@pytest.mark.parametrize("failure", ["expired-token", "core-auth"])
def test_authentication_loss_retires_the_local_preview(transport, title, failure):
    fixture = transport.open(title, "codex")
    with fixture.client as client:
        assert client.post(fixture.path + "/start", headers=fixture.headers).status_code == 200
        if failure == "expired-token":
            fixture.session.expires_at = time.time() - 1
            assert client.get(fixture.path + "/status", headers=fixture.headers).status_code == 401
        else:
            gateway = fixture.app.state.native_order_gateway("venture-one", "order-one")
            fixture.response_status["value"] = 401
            with pytest.raises(PermissionError): gateway.read()
        assert transport.owners[0].status()["processes_stopped"] is True


@pytest.mark.parametrize("title", DOMAINS)
@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("settled", [True, False])
def test_worker_opens_preview_only_after_cleanup_and_core_stop(transport, title, provider, settled):
    order, receipt = settled_order(title, provider)
    gateway = WorkOrderGateway(venture_id=order["venture_id"], order_id=order["id"], device_id=order["device_id"],
        request=lambda *args: pytest.fail("unexpected execution"))
    gateway.execution_id, gateway.claimed, gateway.claim_attempted = "execution-one", True, True

    def stop_receipt(*args, **kwargs):
        transport.events.append("core-stop")
        return {"state": "stopped" if settled else "claimed"}

    gateway.stop_receipt = stop_receipt
    worker = DeviceWorker(gateway, lambda value: None, activity_current=lambda: True)
    worker.last = receipt
    worker._product_manifest = order["manifest"]
    worker._product_coding = SimpleNamespace(_workspace=lambda: None)
    tracker = SimpleNamespace(capture=lambda: None, complete=True, observed=[1],
        stop_all=lambda: transport.events.append("coding-cleanup"), stopped=lambda: True, receipt=lambda: {})
    worker._finish_product_execution(order["manifest"], tracker, True, receipt["evidence"]["product_build"],
        "pass", SimpleNamespace(spawn_attempted=True))
    assert worker.closed and worker.last["state"] == "stopped"
    assert bool(transport.owners) is settled
    if settled:
        assert transport.events == ["coding-cleanup", "core-stop", "preview-start"]
        worker.stop_product_preview()


@pytest.mark.parametrize("title", DOMAINS)
def test_native_deploy_retains_functional_evidence_but_core_v1_packet_stays_compatible(transport, monkeypatch, title):
    order, _ = settled_order(title, "codex")
    sent = []
    gateway = WorkOrderGateway(venture_id=order["venture_id"], order_id=order["id"], device_id=order["device_id"],
        request=lambda method, path, body: sent.append(deepcopy(body)))
    gateway.execution_id, gateway.claimed, gateway.claim_attempted = "execution-one", True, True
    result = {"provider": "vercel", "target": "preview", "url": "https://review.vercel.app",
        "source_commit": "a" * 40, "file_manifest_sha256": "b" * 64, "http_status": 200,
        "health_status": 200, "project_link_sha256": None, "exit_code": 0, "availability_status": "available",
        "functional_status": "failed", "http_journey": [{"status": "failed", "expected_sha256": "c" * 64}]}
    monkeypatch.setattr(device_worker, "OwnedProcesses", lambda: object())
    monkeypatch.setattr(owned_deploy, "OwnedDeploy", lambda **kwargs: SimpleNamespace(preflight=lambda **kwargs: None,
        run=lambda deadline: deepcopy(result)))
    worker = DeviceWorker(gateway, lambda value: None)
    worker._claim = lambda *args: None
    worker._finish_product_execution = lambda *args: None
    manifest = {"title": title, "applications": ["com.companyos.app"], "origins": [], "actions": ["terminal.execute"],
        "credit_ceiling_micro": 0, "duration_seconds": 60, "deploy_verification": {"operation": "workspace_deploy_v1",
            "provider": "vercel", "target": "preview"}}
    worker.run_deploy({"state": "approved", "task_snapshot": {"artifact_profile": "web_app_vercel_v1"}}, manifest)
    receipt = worker.last["deploy_receipt"]
    assert receipt["status"] == "deployed" and receipt["functional_status"] == "failed"
    assert receipt["availability_status"] == "available" and receipt["http_journey"] == result["http_journey"]
    assert sent[0]["execution_id"] == "execution-one"
    assert not {"functional_status", "availability_status", "http_journey"}.intersection(sent[0])
    gateway.deploy_receipt(receipt)
    assert sent[1] == sent[0] and receipt["functional_status"] == "failed"

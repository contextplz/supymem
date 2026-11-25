"""Cross-module admission, immutable deployment and honest cleanup regressions."""
from pathlib import Path
from types import SimpleNamespace
import hashlib
import json

import pytest

from desktop_runtime import agent_status, loop_checks, owned_deploy
from desktop_runtime.device_worker import DeviceWorker
from desktop_runtime.owned_coding import _trusted_git
from desktop_runtime.owned_terminal import OwnedTerminal
from desktop_runtime import product_receipts, device_journal


@pytest.mark.parametrize("venture_id", ["factory_checklists", "family_walks"])
@pytest.mark.parametrize("local", [True, False])
@pytest.mark.parametrize("prefix", ["/api", "/api/v1"])
def test_bridge_api_url_keeps_the_core_versioned_routes(monkeypatch, venture_id, local, prefix):
    from desktop_runtime.bridge import _api_url

    monkeypatch.setenv("SUPYMEM_MODE", "local" if local else "hosted")
    path = f"{prefix}/ventures/{venture_id}"
    expected = path[4:] if local and prefix == "/api" else path
    assert _api_url("http://127.0.0.1:43123", path) == "http://127.0.0.1:43123" + expected
    assert _api_url("http://127.0.0.1:43123", "/api/v1?limit=1") == "http://127.0.0.1:43123/api/v1?limit=1"


@pytest.mark.parametrize("title", ["Freight invoice reconciliation", "Garden watering journal"])
def test_embedded_local_bridge_binds_identity_before_reading_its_reference(monkeypatch, tmp_path, title):
    import threading
    import time
    import httpx
    from fastapi.testclient import TestClient
    from desktop_runtime.bridge import create_bridge_app
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    from core import local_security

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(local_security, "_proxy_secret", lambda: b"a" * 64)

    def request(req):
        assert req.url.host == "127.0.0.1" and req.url.port == 43123
        assert req.url.path == "/ventures/ven_owned/device-work-orders/dwo_owned/pagebuilder-reference"
        assert req.headers["x-local-proxy-secret"] == "a" * 64
        assert "authorization" not in req.headers
        return httpx.Response(200, content=title.encode(), headers={"Content-Type": "application/zip"})

    app = create_bridge_app(local_core="http://127.0.0.1:43123", transport=httpx.MockTransport(request))
    with TestClient(app):
        session = app.state.bridge_session
        assert session.account_id is None and session.access_token is None and not session.connected
        session.account_id, session.device_id, session.access_token = "per_owner", "dev_local", "a" * 64
        session.connected = True
        gateway = app.state.native_order_gateway("ven_owned", "dwo_owned")
        assert gateway.download(gateway.path + "/pagebuilder-reference", stop=threading.Event(),
            deadline=time.monotonic() + 5, max_bytes=8192) == title.encode()


@pytest.mark.parametrize("domain", ["Supplier invoice review", "Family hiking planner"])
@pytest.mark.parametrize("provider", ["claude", "codex", "cursor", "opencode"])
def test_local_build_capability_accepts_each_consented_subscription_tool(monkeypatch, domain, provider):
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    from app import coding_agents_consent
    from app.core import local_capabilities
    from app.pagebuilder import tool as page_tool
    from app.workspaces import resolve
    import sys

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(resolve, "local_person", lambda: SimpleNamespace(id=domain))
    monkeypatch.setattr(coding_agents_consent, "consents", lambda actor: {provider: actor == domain})
    tool = {"name": provider, "installed": True, "signed_in": True,
            "available": True, "subscription_login": True}
    monkeypatch.setattr(agent_status, "statuses", lambda: [tool])
    assert local_capabilities.enabled("build_with_my_tool") is True
    assert local_capabilities.enabled("landing_pages") is True
    monkeypatch.setitem(sys.modules, "app.chat.product_build_request", None)
    assert page_tool.connected_provider(domain) == provider
    tool["subscription_login"] = False
    assert local_capabilities.enabled("build_with_my_tool") is False
    assert local_capabilities.enabled("landing_pages") is False
    tool["subscription_login"] = True
    monkeypatch.setattr(coding_agents_consent, "consents", lambda actor: {provider: False})
    assert local_capabilities.enabled("build_with_my_tool") is False


@pytest.mark.parametrize("title", ["Freight invoice reconciliation", "Garden watering journal"])
def test_local_bridge_boots_with_owner_and_authenticates_cancellable_calls(monkeypatch, tmp_path, title):
    import threading
    import time
    import httpx
    from fastapi.testclient import TestClient
    from fastapi import HTTPException
    from desktop_runtime.bridge import create_bridge_app
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    from app.core import local_owner
    from app.build import device_orders
    from core import local_security

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_URL", "http://127.0.0.1:43123")
    monkeypatch.setenv("SUPYMEM_DESKTOP_INSTANCE_ID", title)
    monkeypatch.setenv("BRIDGE_AUTH_OWNER", "native")
    monkeypatch.setenv("BRIDGE_NATIVE_TOKEN", "native-test-" * 4)
    monkeypatch.setattr(local_security, "_proxy_secret", lambda: b"a" * 64)
    monkeypatch.setattr(local_owner, "ensure_owner", lambda: SimpleNamespace(id="per_owner", email=""))
    owner_device = "local-" + hashlib.sha256(b"per_owner").hexdigest()[:24]
    def device_caller(caller):
        assert caller.sub == "per_owner" and caller.role == "user" and not caller.legacy
        return SimpleNamespace(device_id=owner_device)
    monkeypatch.setattr(device_orders, "device_caller", device_caller, raising=False)
    calls = []

    def request(req):
        calls.append(req)
        assert req.url.host == "127.0.0.1" and req.url.port == 43123
        assert req.url.path == "/ventures/ven_owned/device-work-orders/dwo_owned/authority"
        assert req.headers["x-local-proxy-secret"] == "a" * 64
        assert "authorization" not in req.headers
        return httpx.Response(200, json={"title": title})

    app = create_bridge_app(transport=httpx.MockTransport(request))
    with TestClient(app, base_url="http://127.0.0.1:3500") as client:
        health = client.get("/health").json()
        assert health["connected"] is True
        assert health["desktop_instance"] == title
        session = app.state.bridge_session
        assert session.account_id == "per_owner"
        assert session.device_id == owner_device
        session.expires_at = 0
        gateway = app.state.native_order_gateway("ven_owned", "dwo_owned")
        assert gateway.cancellable_request("GET", gateway.path + "/authority", None,
            stop=threading.Event(), deadline=time.monotonic() + 5) == {"title": title}
        assert len(calls) == 1
        assert client.get("/native/coding-agents").status_code == 401
        config = client.get("/config")
        assert config.status_code == 200 and "a" * 64 not in config.text
        monkeypatch.setattr(local_security, "_proxy_secret", lambda: b"b" * 64)
        with pytest.raises(HTTPException) as rejected:
            app.state.native_order_gateway("ven_owned", "dwo_owned")
        assert rejected.value.status_code == 401


@pytest.mark.parametrize("title", ["Freight invoice reconciliation", "Garden watering journal"])
def test_local_bridge_uses_owner_credential_and_local_api(monkeypatch, tmp_path, title):
    import time
    import httpx
    from fastapi.testclient import TestClient
    from desktop_runtime.bridge import create_bridge_app
    from tests.company_os_harness import load_company_os_app
    load_company_os_app()
    from app.core import local_owner
    from core import local_security

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_URL", "http://127.0.0.1:3300")
    monkeypatch.setenv("GATEWAY_URL", "https://hosted.invalid")
    monkeypatch.setenv("BRIDGE_AUTH_OWNER", "native")
    monkeypatch.setenv("BRIDGE_NATIVE_TOKEN", "native-test-" * 4)
    monkeypatch.setattr(local_security, "_proxy_secret", lambda: b"a" * 64)
    monkeypatch.setattr(local_owner, "ensure_owner", lambda: SimpleNamespace(id="per_owner", email=""))
    calls = []
    def request(req):
        calls.append(req)
        assert req.url.host == "127.0.0.1" and req.url.port == 3300
        assert req.url.path == "/ventures/ven_owned/device-work-orders/dwo_owned"
        assert req.headers["x-local-proxy-secret"] == "a" * 64
        assert "authorization" not in req.headers
        assert req.url.path != "/auth/session"
        return httpx.Response(200, json={"orders": [], "title": title})
    app = create_bridge_app(transport=httpx.MockTransport(request))
    with TestClient(app) as client:
        body = {"access_token": "a" * 64, "account_id": "per_owner", "device_id": "dev_local",
                "expires_at": time.time() + 300}
        headers = {"x-native-token": "native-test-" * 4}
        assert client.post("/native/session", json=body, headers=headers).status_code == 200
        gateway = app.state.native_order_gateway("ven_owned", "dwo_owned")
        assert gateway.account_id == "per_owner"
        assert gateway.request("GET", gateway.path, None)["title"] == title
        assert len(calls) == 1
        bad = {**body, "access_token": "b" * 64}
        assert client.post("/native/session", json=bad, headers=headers).status_code == 401


@pytest.mark.parametrize("title", ["Freight invoice reconciliation", "Garden watering journal"])
@pytest.mark.parametrize("state", ["ready", "off", "other_device", "not_connected"])
def test_local_static_coding_admission_uses_owner_settings(monkeypatch, title, state):
    from fastapi import HTTPException
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    from app.api import device_orders_api as api
    from app.core import local_capabilities

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.delenv("DEVICE_BROWSER_EXECUTION_ENABLED", raising=False)
    caller = SimpleNamespace(sub="per_owner", device_id=None)
    order = {"device_id": "local_owned", "actor_id": "per_owner", "manifest": {
        "title": title, "actions": ["editor.edit", "terminal.execute"],
        "coding_verification": {"provider": "codex"}}}
    monkeypatch.setattr(api, "verify_caller", lambda *_args, **_kwargs: caller)
    monkeypatch.setattr(api.device_orders, "retrieve", lambda *_args: order)
    monkeypatch.setattr(api.device_orders, "_scope", lambda *_args: (
        "workspace_owned", "per_owner", "local_other" if state == "other_device" else "local_owned"))
    monkeypatch.setattr(api.device_orders, "coding_loop_enabled", lambda actor: actor == "per_owner" and state != "off")
    monkeypatch.setattr(local_capabilities, "blocked_message", lambda _name:
        "Open Settings > Keys and connections > Coding tools and connect your tool, then retry.")

    def check_connection(manifest, actor):
        assert manifest == order["manifest"] and actor == "per_owner"
        if state == "not_connected":
            raise HTTPException(403, detail={"code": "coding_tool_not_connected", "message":
                "Open Settings > Keys and connections > Coding tools and connect your tool, then retry."})

    monkeypatch.setattr(api.device_orders, "assert_coding_loop_available", check_connection)
    result = api.execution_capabilities("ven_owned", "dwo_owned", None)
    assert result["available"] is (state == "ready")
    if state != "ready":
        assert "Settings > Keys and connections > Coding tools" in result["message"]


@pytest.mark.parametrize("title", ["Freight invoice reconciliation", "Garden watering journal"])
def test_local_authority_derives_owner_device_without_hosted_execution_flag(monkeypatch, title):
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    from app.api import device_orders_api as api

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.delenv("DEVICE_BROWSER_EXECUTION_ENABLED", raising=False)
    owner = "per_" + hashlib.sha256(title.encode()).hexdigest()[:16]
    caller = SimpleNamespace(sub=owner, device_id=None, role="user", legacy=False)
    resolved = SimpleNamespace(sub=owner, device_id="local-" + hashlib.sha256(owner.encode()).hexdigest()[:24])
    monkeypatch.setattr(api, "verify_caller", lambda *_args, **_kwargs: caller)

    def device_caller(context):
        assert context is caller and context.role == "user" and not context.legacy
        return resolved

    monkeypatch.setattr(api.device_orders, "device_caller", device_caller)
    assert api.execution_caller(None) is resolved


@pytest.mark.parametrize("title", ["Freight invoice reconciliation", "Garden watering journal"])
def test_local_product_claim_uses_one_capability_not_hosted_flags(monkeypatch, title):
    from desktop_runtime import device_gateway
    from tests.company_os_harness import load_company_os_app
    load_company_os_app()
    from app.core import local_capabilities

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    checked = []
    monkeypatch.setattr(local_capabilities, "enabled", lambda name: checked.append(name) or True)
    order = {"id": "dwo_owned", "venture_id": "ven_owned", "device_id": "dev_owned",
        "actor_id": "per_owner", "state": "approved", "review_digest": "a" * 64,
        "manifest": {**product_review(), "title": title}}
    calls = []
    def request(method, path, body):
        calls.append(path)
        assert not path.endswith("/capabilities")
        return {**order, "state": "claimed", "execution_id": "dex_owned"} if method == "POST" else order
    gateway = device_gateway.WorkOrderGateway(venture_id="ven_owned", order_id="dwo_owned",
        device_id="dev_owned", account_id="per_owner", request=request)
    assert gateway.claim()["state"] == "claimed"
    assert checked == ["build_with_my_tool"]
    with pytest.raises(PermissionError, match="already_attempted"):
        gateway.claim()
    order["actor_id"] = "per_other"
    with pytest.raises(PermissionError, match="identity_changed"):
        gateway.read()
    def unreadable_settings(name):
        raise KeyError("saved_settings")
    monkeypatch.setattr(local_capabilities, "enabled", unreadable_settings)
    with pytest.raises(KeyError, match="saved_settings"):
        device_gateway.build_capability_ready()


@pytest.mark.parametrize("title", ["Freight invoice reconciliation", "Garden watering journal"])
@pytest.mark.parametrize("authorized", [True, False])
def test_requested_plan_continues_once_and_not_after_stop(monkeypatch, title, authorized):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    gateway = Gateway()
    worker = DeviceWorker(gateway, lambda _: None)
    worker.closed = True
    worker.last.update(state="stopped", task_verified=True, plan_posted=True,
        plan={"plan_digest": "a" * 64}, iteration_receipts=[{"posted": True}],
        evidence={"outcome": "plan_ready", "processes_stopped": True})
    worker.loop_saved = {"manifest": {**product_review(), "title": title}}
    if authorized:
        worker.loop_saved["requested_build"] = {"request_sha256": "a" * 64, "caps": {"max_iterations": 6,
            "max_wall_seconds": 2700, "per_call_seconds": 720, "max_turns": None}}
    worker.loop_saved["manifest"]["coding_loop_verification"]["phase"] = "plan"
    calls = []
    gateway.prepare_product_run = lambda plan: calls.append(plan) or {"id": "dwo_implementation"}
    if not authorized:
        assert worker.next_requested_build() is None and not calls
        return
    assert worker.next_requested_build()["id"] == "dwo_implementation"
    assert len(calls) == 1
    assert worker.next_requested_build() is None
    worker.last.pop("next_order_id")
    worker.cancel()
    assert worker.next_requested_build() is None
    assert len(calls) == 1


@pytest.mark.parametrize("title", ["Freight invoice reconciliation", "Garden watering journal"])
@pytest.mark.parametrize("switch_tool", [False, True])
def test_local_bridge_runs_the_saved_plan_then_bound_implementation(monkeypatch, tmp_path, title, switch_tool):
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    import threading
    import time
    import httpx
    from fastapi.testclient import TestClient
    from desktop_runtime import bridge, device_worker, device_gateway
    from core import local_security
    from app.core import local_owner

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BRIDGE_AUTH_OWNER", "native")
    monkeypatch.setenv("BRIDGE_NATIVE_TOKEN", "native-test-" * 4)
    monkeypatch.delenv("SUPYMEM_NATIVE_BROWSER_ENABLED", raising=False)
    monkeypatch.setattr(local_security, "_proxy_secret", lambda: b"a" * 64)
    monkeypatch.setattr(local_owner, "ensure_owner", lambda: SimpleNamespace(id="per_owner", email=""))
    monkeypatch.setattr(device_gateway, "build_capability_ready", lambda: True)
    manifest = {**product_review(), "title": title}
    manifest["coding_loop_verification"] = {**manifest["coding_loop_verification"], "phase": "plan"}
    source = {"id": "dwo_plan", "venture_id": "ven_owned", "device_id": None, "actor_id": "per_owner",
        "state": "approved", "review_digest": "b" * 64, "manifest": manifest,
        "requested_build": {"request_sha256": "c" * 64, "caps": {"max_iterations": 6,
            "max_wall_seconds": 2700, "per_call_seconds": 720, "max_turns": None}}}
    implementation = {**source, "id": "dwo_implementation", "manifest": {**manifest,
        "coding_loop_verification": {**manifest["coding_loop_verification"], "phase": "implement",
            "plan_order_id": "dwo_plan", "plan_digest": "a" * 64, "provider": "claude" if switch_tool else "codex"}}}
    orders, requests, started, ran = {source["id"]: source, implementation["id"]: implementation}, [], [], []
    def request(req):
        requests.append(req.url.path)
        assert req.headers["x-local-proxy-secret"] == "a" * 64
        return httpx.Response(200, json=implementation if req.url.path.endswith("/prepare-run") else orders[req.url.path.split("/")[-1]])
    class Thread:
        def __init__(self, *, target, args, **kwargs):
            self.target, self.args = target, args
        def start(self):
            started.append(self)
        def is_alive(self):
            return False
    class Worker(DeviceWorker):
        def run(self):
            ran.append(self.gateway.order_id)
            self.closed = True
            self.loop_saved = orders[self.gateway.order_id]
            evidence = {"execution_id": "dex_plan", "outcome": "plan_ready", "processes_stopped": True}
            self.progress("stopped", task_verified=True, plan_posted=True,
                plan={"plan_digest": "a" * 64}, iteration_receipts=[{"posted": True}], evidence=evidence)
            if self.gateway.order_id == "dwo_plan":
                source.update(state="stopped", closed_at="2026-11-15T00:00:00Z", execution_id="dex_plan",
                    approved_plan={"plan_digest": "a" * 64}, native_stop={"task_verified": True, "processes_stopped": True})
    monkeypatch.setattr(bridge, "threading", SimpleNamespace(Thread=Thread, Lock=threading.Lock, RLock=threading.RLock))
    monkeypatch.setattr(device_worker, "DeviceWorker", Worker)
    app = bridge.create_bridge_app(transport=httpx.MockTransport(request))
    session = app.state.bridge_session
    headers = {"x-native-token": "native-test-" * 4}
    with TestClient(app) as client:
        device = session.device_id
        source["device_id"] = implementation["device_id"] = device
        status_path = "/native/device-work-orders/ven_owned/dwo_plan/status"
        assert client.get(status_path, headers={"x-local-proxy-secret": "a" * 64}).status_code == 401
        assert client.get(status_path, headers={**headers, "origin": "http://127.0.0.1:3100"}).status_code == 401
        status = client.get(status_path, headers=headers)
        assert status.status_code == 200 and status.json()["state"] == "not_started"
        assert client.post("/native/device-work-orders/ven_owned/dwo_plan/start", headers=headers).status_code == 200
        assert len(started) == 1
        started[0].target(*started[0].args)
        scope = (session.gateway, "per_owner", device)
        saved = device_journal.read(scope, "ven_owned", "dwo_plan")
        assert sum(path.endswith("/prepare-run") for path in requests) == 1
        assert ran == (["dwo_plan"] if switch_tool else ["dwo_plan", "dwo_implementation"])
        if switch_tool:
            assert "next_order_id" not in saved and saved["continuation_message"]
        else:
            assert saved["next_order_id"] == "dwo_implementation"
            assert device_journal.read(scope, "ven_owned", "dwo_implementation")["state"] == "stopped"
            assert client.post("/native/device-work-orders/ven_owned/dwo_plan/cancel", headers=headers).status_code == 200
        assert client.post("/native/device-work-orders/ven_owned/dwo_plan/start", headers=headers).status_code == 409
@pytest.mark.parametrize("title", ["Freight invoice reconciliation", "Garden watering journal"])
def test_local_verified_preview_does_not_need_wallet_settlement(monkeypatch, title):
    from desktop_runtime import coding_loop
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    gateway = Gateway()
    worker = DeviceWorker(gateway, lambda _: None, activity_current=lambda: True)
    worker.closed = True
    worker._product_manifest = {**product_review(), "title": title}
    build = {"outcome": "pass", "provider": "codex"}
    evidence = {"order_id": gateway.order_id, "execution_id": gateway.execution_id,
        "processes_stopped": True, "product_build": build}
    worker.last.update(state="stopped", task_verified=True, credits_reconciled=False, evidence=evidence,
        evidence_digest=hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
    worker._product_coding = SimpleNamespace()
    monkeypatch.setattr(coding_loop, "start_product_preview", lambda *args: {"status": "ready", "ready": True})
    assert worker.start_product_preview()["ready"] is True
    worker.cancel()
    with pytest.raises(ValueError, match="preview_unavailable"):
        worker.start_product_preview()


@pytest.mark.parametrize("url", ["http://app.vercel.app", "https://evil.example/",
    "https://app.vercel.app@evil.example", "https://app.vercel.app/path",
    "https://app.vercel.app?token=private", "https://app.vercel.app\nhttps://evil.example"])
def test_deploy_stdout_cannot_forward_to_another_host(url):
    with pytest.raises(ValueError):
        owned_deploy.deployment_url(url.encode())


def deploy_fixture(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "api").mkdir()
    (workspace / "api/health.js").write_text("export default (req,res)=>res.end('ok')")
    (workspace / "index.html").write_text("<title>Owned app</title>")
    (workspace / "package.json").write_text('{"type":"module"}')
    (workspace / "review").mkdir()
    (workspace / "review/spec.md").write_text("Private reviewed product /api/health")
    (workspace / "tests").mkdir()
    (workspace / "tests/app.test.js").write_text("// local-only check")
    _trusted_git(workspace, "init", "--quiet")
    _trusted_git(workspace, "add", "--all", "--force", "--", ".")
    _trusted_git(workspace, "commit", "--quiet", "-m", "reviewed source")
    verification = {"operation": "workspace_deploy_v1", "provider": "vercel", "target": "preview",
        "workspace_slug": "owned-app", "source_commit": _trusted_git(workspace, "rev-parse", "HEAD"),
        "file_manifest_sha256": loop_checks.source_manifest(workspace)["file_manifest_sha256"]}
    deploy = owned_deploy.OwnedDeploy(venture_id="ven_ours", verification=verification,
        tracker=None, authorize=lambda: True, cancelled=lambda: False)
    destination = tmp_path / "upload"
    destination.mkdir()
    return deploy, workspace, destination


def test_snapshot_uploads_only_exact_reviewed_commit_files(tmp_path):
    deploy, workspace, destination = deploy_fixture(tmp_path)
    deploy._snapshot(workspace, destination)
    names = sorted(path.relative_to(destination).as_posix() for path in destination.rglob("*") if path.is_file())
    assert names == [".vercelignore", "api/health.js", "index.html", "package.json"]
    assert (destination / "index.html").read_bytes() == (workspace / "index.html").read_bytes()
    assert "review/" in (destination / ".vercelignore").read_text()


def test_uncommitted_source_and_unreviewed_project_destination_refuse(tmp_path):
    deploy, workspace, destination = deploy_fixture(tmp_path)
    (workspace / "index.html").write_text("<title>Later unreviewed source</title>")
    with pytest.raises(ValueError, match="deploy_source_changed"):
        deploy._snapshot(workspace, destination)
    _trusted_git(workspace, "checkout", "--", "index.html")
    (workspace / ".vercel").mkdir()
    (workspace / ".vercel/project.json").write_text('{"projectId":"prj_test","orgId":"org_test"}')
    with pytest.raises(ValueError, match="deploy_project_link_changed"):
        deploy._snapshot(workspace, destination)
    _metadata, digest = owned_deploy._project_link(workspace / ".vercel/project.json")
    deploy.verification["project_link_sha256"] = digest
    deploy._snapshot(workspace, destination)
    assert owned_deploy._project_link(destination / ".vercel/project.json")[1] == digest


class Gateway:
    order_id, venture_id, execution_id = "dwo_ours", "ven_ours", "dex_ours"
    claim_attempted, claimed = False, False

    def claim(self):
        self.claim_attempted = self.claimed = True

    def stop_receipt(self, _digest, stopped, *, task_verified=False):
        self.receipt = {"processes_stopped": stopped, "task_verified": task_verified}
        return {"state": "stopped" if stopped else "reconciliation_required"}


def product_review():
    return {"applications": ["com.companyos.app"], "origins": [], "actions": ["editor.edit", "terminal.execute"],
        "credit_ceiling_micro": 0, "duration_seconds": 60,
        "coding_loop_verification": {"operation": "workspace_loop_v1", "profile": "web_app_vercel_v1",
            "check_recipe": "web_app_vercel_checks_v1", "provider": "codex", "workspace_slug": "owned-app",
            "phase": "implement", "network": "none", "read_scope": "venture_workspace", "write_scope": "venture_workspace"}}


@pytest.mark.parametrize("domain", ["Supplier invoice review", "Family hiking planner"])
def test_signed_out_and_missing_checks_never_claim_or_start_agent(monkeypatch, domain):
    monkeypatch.setattr("desktop_runtime.device_gateway.build_capability_ready", lambda: True)
    gateway = Gateway()
    worker = DeviceWorker(gateway, lambda _: None)
    monkeypatch.setattr(worker, "_notify_terminal", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(agent_status, "statuses", lambda: [{"name": "codex", "installed": True, "available": True}])
    monkeypatch.setattr(agent_status, "_status", lambda provider: {"name": provider, "installed": True, "available": False})
    saved = {"state": "approved", "task_snapshot": {"artifact_profile": "web_app_vercel_v1"}}
    manifest = product_review()
    manifest["instruction"] = domain
    worker.run_coding_loop(saved, manifest)
    assert not gateway.claim_attempted and worker.last["state"] == "not_started"
    assert worker.last["failure_code"] == "coding_sign_in_required"
    monkeypatch.setattr(agent_status, "_status", lambda provider: {"name": provider, "installed": True, "available": True})
    monkeypatch.setattr(loop_checks, "preflight", lambda: False)
    worker.run_coding_loop(saved, manifest)
    assert not gateway.claim_attempted and worker.last["failure_code"] == "coding_checks_unavailable"


def test_cleanup_failure_and_journal_failure_cannot_invent_verified_stop(monkeypatch):
    gateway = Gateway()
    gateway.claim_attempted = gateway.claimed = True
    worker = DeviceWorker(gateway, lambda _: (_ for _ in ()).throw(OSError("journal unavailable")))
    monkeypatch.setattr(worker, "_notify_terminal", lambda *_args, **_kwargs: None)
    worker.last["task_verified"] = True
    def refuse():
        raise OSError("process inventory unavailable")
    tracker = SimpleNamespace(capture=refuse, stop_all=refuse)
    worker._finish_product_execution(product_review(), tracker, True, {"task_verified": True}, "pass",
                                     SimpleNamespace(spawn_attempted=True))
    assert gateway.receipt == {"processes_stopped": False, "task_verified": False}
    assert worker.closed is False and worker.last["state"] == "reconciliation_required"
    assert worker.last["journal_unconfirmed"] is True


@pytest.mark.parametrize("domain", ["Supplier invoice review", "Family hiking planner"])
@pytest.mark.parametrize("error,code,exception_type", [
    (ValueError("coding_processes_not_stopped"), "coding_processes_not_stopped", "ValueError"),
    (ValueError("coding_git_scope_invalid"), "coding_git_scope_invalid", "ValueError"),
    (ValueError("coding_git_checkpoint_failed"), "coding_git_checkpoint_failed", "ValueError"),
    (FileNotFoundError("private workspace path"), "coding_workspace_file_missing", "FileNotFoundError"),
])
def test_build_exception_keeps_stage_type_and_safe_code(monkeypatch, domain, error, code, exception_type):
    from desktop_runtime import coding_loop, device_worker
    from core.runtime_diagnostics import closed_runtime_diagnostic

    monkeypatch.setattr(device_worker, "is_local", lambda: True)
    monkeypatch.setattr("desktop_runtime.device_gateway.build_capability_ready", lambda: True)
    monkeypatch.setattr(agent_status, "_status", lambda provider: {"installed": True, "available": True})
    monkeypatch.setattr(loop_checks, "preflight", lambda: True)
    monkeypatch.setattr(device_worker.OwnedCoding, "preflight", lambda *_: None)
    gateway = Gateway()
    worker = DeviceWorker(gateway, lambda _: None)
    monkeypatch.setattr(worker, "_notify_terminal", lambda *_args, **_kwargs: None)
    def fail(*args):
        worker.progress("running_coding", loop_phase="check")
        raise error
    monkeypatch.setattr(coding_loop, "run_coding_loop", fail)
    manifest = {**product_review(), "instruction": domain}
    saved = {"state": "approved", "task_snapshot": {"artifact_profile": "web_app_vercel_v1"}}
    worker.run_coding_loop(saved, manifest)
    assert worker.last["failure_stage"] == "check"
    assert worker.last["exception_type"] == exception_type
    assert worker.last["failure_code"] == code
    result = worker.last["evidence"]["product_build"]
    assert result["failure_stage"] == "check" and result["exception_type"] == exception_type
    assert result["failure_code"] == code
    assert "checking" in worker.last["message"] and "Build activity" in worker.last["message"]
    assert "private workspace path" not in json.dumps(worker.last)
    diagnostic = worker._runtime_diagnostics.snapshot()
    assert diagnostic["phase"] == "acting" and diagnostic["reason"] == "runtime_failure"
    assert closed_runtime_diagnostic(diagnostic) == diagnostic


def test_workspace_marker_is_refused_before_reading_a_symlink(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    workspace = tmp_path / "Documents/Supymem/ventures/owned-app"
    workspace.mkdir(parents=True)
    target = tmp_path / "outside-marker"
    target.write_text("ven_ours\n")
    (workspace / ".supymem-venture").symlink_to(target)
    original = Path.read_text
    def assert_not_read(path, *args, **kwargs):
        assert path.name != ".supymem-venture"
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", assert_not_read)
    terminal = OwnedTerminal(venture_id="ven_ours", workspace_slug="owned-app",
                             tracker=None, authorize=lambda: False)
    with pytest.raises(ValueError, match="owner_unconfirmed"):
        terminal.workspace()


@pytest.mark.parametrize("deploy_order", [False, True])
def test_saved_plan_iteration_deploy_and_stop_sync_without_execution(deploy_order):
    class ReceiptGateway(Gateway):
        device_id, review_digest = "dev_ours", "a" * 64
        def __init__(self): self.calls = []
        def iteration_receipt(self, row): self.calls.append(("iteration", row))
        def post_plan(self, row): self.calls.append(("plan", row))
        def deploy_receipt(self, row): self.calls.append(("deploy", row))
        def stop_receipt(self, digest, stopped, *, task_verified=False):
            self.calls.append(("stop", digest, stopped, task_verified))
            return {"state": "stopped"}
        def claim(self): raise AssertionError("receipt syncing cannot claim or run anything")
    gateway = ReceiptGateway()
    evidence = {"execution_id": "dex_ours", "processes_stopped": True, "task_verified": True}
    digest = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    local = {"order_id": "dwo_ours", "state": "stopped", "evidence": evidence, "evidence_digest": digest,
        "iteration_receipts": [{"index": 0, "posted": False}],
        "plan": {"spec": "reviewed", "tasks": [], "plan_digest": "b" * 64}, "plan_posted": False}
    order = {"id": "dwo_ours", "device_id": "dev_ours", "state": "claimed",
        "execution_id": "dex_ours", "review_digest": "a" * 64, "manifest": product_review()}
    if deploy_order:
        local.pop("plan")
        local["iteration_receipts"] = []
        local.update(deploy_receipt={"status": "deployed"}, deploy_receipt_posted=False)
        order["manifest"] = {"deploy_verification": {"operation": "workspace_deploy_v1"}}
    saved = []
    result = product_receipts.reconcile(gateway, order, local, lambda row: saved.append(dict(row)))
    assert [call[0] for call in gateway.calls] == (["deploy", "stop"] if deploy_order else ["iteration", "plan", "stop"])
    assert result["credits_reconciled"] and result["deploy_receipt_posted" if deploy_order else "plan_posted"]
    with pytest.raises(ValueError, match="unavailable"):
        product_receipts.reconcile(gateway, {**order, "device_id": "another"}, local, lambda _: None)


@pytest.mark.parametrize("title", ["Freight invoice reconciliation", "Garden watering journal"])
@pytest.mark.parametrize("action", ["status", "reconcile"])
def test_local_build_stop_recovers_only_after_recorded_process_exits(monkeypatch, tmp_path, title, action):
    import httpx
    import psutil
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from desktop_runtime import bridge
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    from app.core import local_owner
    from core import local_security

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(local_security, "_proxy_secret", lambda: b"a" * 64)
    monkeypatch.setattr(local_owner, "current_owner", lambda: SimpleNamespace(id="per_owner"))
    order = {"id": "dwo_owned", "venture_id": "ven_owned", "device_id": "dev_local", "actor_id": "per_owner",
        "state": "claimed", "claimed_at": "2026-11-15T00:00:00Z", "execution_id": "dex_owned", "review_digest": "b" * 64,
        "manifest": {**product_review(), "title": title}, "stop_reconciliation_required": True}
    evidence = {"order_id": order["id"], "execution_id": order["execution_id"], "processes_stopped": False,
        "inventory": {"inventory_complete": True, "processes": [{"pid": 98765, "created_at": 123.0}]},
        "outcome": "cli_error", "product_build": {"outcome": "cli_error"}, "task_verified": False}
    digest = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    order["native_stop"] = {"execution_id": order["execution_id"], "evidence_digest": digest, "processes_stopped": False}
    receipt = {"order_id": order["id"], "tool": "coding", "state": "reconciliation_required",
        "evidence": evidence, "evidence_digest": digest, "task_verified": False, "credits_reconciled": False}
    posts = []

    def request(req):
        if req.method == "POST":
            assert req.url.path.endswith("/stop-receipt") and order["native_stop"]["processes_stopped"] is False
            payload = json.loads(req.content)
            posts.append(payload)
            order.update(state="stopped", closed_at="2026-11-15T00:00:00Z", native_stop=payload)
            order.pop("stop_reconciliation_required")
        return httpx.Response(200, json=order)

    native = bridge.create_bridge_app(local_core="http://127.0.0.1:43123", transport=httpx.MockTransport(request))
    session = native.state.bridge_session
    session.account_id, session.device_id, session.access_token = order["actor_id"], order["device_id"], "a" * 64
    session.connected = True
    scope = session.gateway, session.account_id, session.device_id
    venture_id, order_id = order["venture_id"], order["id"]
    device_journal.create(scope, venture_id, order_id)
    device_journal.save(scope, venture_id, order_id, receipt)
    worker = DeviceWorker(native.state.native_order_gateway(venture_id, order_id), lambda _: None)
    worker.last = receipt
    native.state.local_active_worker.update(worker=worker, scope=scope)
    monkeypatch.setattr(bridge, "create_bridge_app", lambda **kwargs: native)
    app = FastAPI()
    app.include_router(bridge.local_build_router("http://127.0.0.1:43123"))
    client = TestClient(app)
    path = f"/native/device-work-orders/{venture_id}/{order_id}"
    check_stop = client.get if action == "status" else client.post
    process_live = True
    real_process = psutil.Process

    def process(pid):
        if pid != 98765:
            return real_process(pid)
        if process_live:
            return SimpleNamespace(create_time=lambda: 123.0)
        raise psutil.NoSuchProcess(pid)

    monkeypatch.setattr(psutil, "Process", process)
    unconfirmed = check_stop(f"{path}/{action}")
    if action == "status":
        assert unconfirmed.json()["state"] == "reconciliation_required"
    else:
        assert unconfirmed.status_code == 409
    assert posts == [] and not worker.closed

    process_live = False
    stopped = check_stop(f"{path}/{action}")
    assert stopped.status_code == 200
    result = stopped.json()
    assert result["state"] == "stopped" and result["credits_reconciled"] is True
    assert result["evidence"] == {**evidence, "processes_stopped": True}
    new_digest = hashlib.sha256(json.dumps(result["evidence"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert posts == [{"execution_id": order["execution_id"], "evidence_digest": new_digest, "processes_stopped": True}]
    assert device_journal.read(scope, venture_id, order_id)["evidence_digest"] == new_digest
    assert client.get(f"{path}/status").json()["state"] == "stopped" and worker.closed
    assert len(posts) == 1


def test_product_journal_keeps_bounded_multilingual_plan_and_queue(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    scope = ("https://owned.test", "usr_ours", "dev_ours")
    device_journal.create(scope, "ven_ours", "dwo_ours")
    receipt = {"order_id": "dwo_ours", "state": "stopped", "product_profile": "web_app_vercel_v1",
        "plan": {"spec": "字" * 5000, "tasks": ["字" * 5000]},
        "events": [{"text": "字" * 90, "at": index} for index in range(50)],
        "iteration_receipts": [{"index": index, "diagnosis": "x" * 2000} for index in range(10)]}
    device_journal.save(scope, "ven_ours", "dwo_ours", receipt)
    assert device_journal.read(scope, "ven_ours", "dwo_ours") == receipt
    with pytest.raises(ValueError, match="too_large"):
        device_journal.save(scope, "ven_ours", "dwo_ours", {**receipt, "oversized": "x" * 256001})


@pytest.mark.parametrize("venture_id", ["factory_checklists", "family_walks"])
@pytest.mark.parametrize("change", ["authorized", "unrequested", "mismatched"])
def test_gateway_continuation_keeps_the_same_requested_build(monkeypatch, venture_id, change):
    from desktop_runtime import device_gateway

    monkeypatch.setattr(device_gateway, "is_local", lambda: True)
    monkeypatch.setattr(device_gateway, "build_capability_ready", lambda: True)
    authorization = {"request_sha256": "a" * 64, "caps": {"max_iterations": 6,
        "max_wall_seconds": 2700, "per_call_seconds": 720, "max_turns": None}}
    plan = {"plan_digest": "b" * 64}
    descriptor = {"phase": "plan", "provider": "codex", "workspace_slug": "owned-project"}
    source = {"state": "stopped", "closed_at": "2026-11-15T00:00:00", "approved_plan": plan,
        "native_stop": {"processes_stopped": True, "task_verified": True},
        "manifest": {"coding_loop_verification": descriptor}, "requested_build": authorization}
    result = {"id": "dwo_run", "venture_id": venture_id, "actor_id": "owner", "device_id": "local_device",
        "state": "approved", "requested_build": authorization,
        "manifest": {"coding_loop_verification": {**descriptor, "phase": "implement",
            "plan_order_id": "dwo_plan", "plan_digest": plan["plan_digest"]}}}
    calls = []
    gateway = SimpleNamespace(read=lambda: source, request=lambda *args: calls.append(args) or result,
        path="/api/ventures/" + venture_id + "/device-work-orders/dwo_plan", venture_id=venture_id,
        order_id="dwo_plan", device_id="local_device", account_id="owner")
    if change == "unrequested":
        source.pop("requested_build")
    elif change == "mismatched":
        result["requested_build"] = {**authorization, "request_sha256": "c" * 64}
    if change == "authorized":
        assert device_gateway.WorkOrderGateway.prepare_product_run(gateway, plan) == result
    else:
        with pytest.raises(PermissionError, match="coding_plan_changed"):
            device_gateway.WorkOrderGateway.prepare_product_run(gateway, plan)
    assert len(calls) == (0 if change == "unrequested" else 1)


@pytest.mark.parametrize("title", ["Freight invoice reconciliation", "Garden watering journal"])
@pytest.mark.parametrize("local", [True, False])
def test_execution_authority_keeps_owner_auth_without_a_local_browser_flag(monkeypatch, title, local):
    from fastapi import HTTPException
    from app.api import device_orders_api

    monkeypatch.setenv("SUPYMEM_MODE", "local" if local else "hosted")
    monkeypatch.delenv("DEVICE_BROWSER_EXECUTION_ENABLED", raising=False)
    caller = SimpleNamespace(sub=title, device_id="local_device", role="user", legacy=False)
    verified = []
    def verify(request, *, required):
        verified.append(required)
        return caller
    monkeypatch.setattr(device_orders_api, "verify_caller", verify)
    if local:
        assert device_orders_api.execution_caller(object()) is caller
    else:
        with pytest.raises(HTTPException) as rejected:
            device_orders_api.execution_caller(object())
        assert rejected.value.status_code == 503
    assert verified == [True]

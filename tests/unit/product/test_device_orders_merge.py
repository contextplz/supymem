import hashlib
import json
from dataclasses import replace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from tests.unit.product.test_device_orders import orders as orders


@pytest.fixture(params=["Fleet maintenance", "Family meal planning"])
def local_orders(orders, monkeypatch, request):
    service, caller, _ = orders
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr("app.coding_agents_consent.consents", lambda *_args: {"codex": True})
    monkeypatch.setattr("desktop_runtime.agent_status.statuses", lambda: [{"name": "codex",
        "installed": True, "signed_in": True, "subscription_login": True, "available": True}])
    monkeypatch.setattr("app.core.local_capabilities.enabled", lambda *_args: True)
    return service, caller, request.param


@pytest.mark.parametrize("state", ["ready", "not_connected", "signed_out", "api_key", "unavailable", "off"])
def test_local_admission_requires_the_selected_subscription(local_orders, monkeypatch, state):
    service, caller, domain = local_orders
    monkeypatch.setattr(service, "coding_loop_enabled", lambda *_args: False)
    monkeypatch.setattr("app.coding_agents_consent.consents", lambda *_args: {"codex": state != "not_connected"})
    monkeypatch.setattr("app.core.local_capabilities.enabled", lambda *_args: state != "off")
    monkeypatch.setattr("desktop_runtime.agent_status.statuses", lambda: [{"name": "codex",
        "installed": True, "signed_in": state != "signed_out", "subscription_login": state != "api_key",
        "available": state != "unavailable"}])
    manifest = {"title": domain, "coding_loop_verification": {"provider": "codex"}}
    if state == "ready":
        service.assert_coding_loop_available(manifest, caller.sub)
    elif state == "not_connected":
        with pytest.raises(HTTPException) as refused:
            service.assert_coding_loop_available(manifest, caller.sub)
        assert "Settings > Keys and connections > Coding tools" in refused.value.detail["message"]
    else:
        code = "device_coding_loop_unavailable" if state == "off" else "device_coding_subscription_required"
        with pytest.raises(ValueError, match=code):
            service.assert_coding_loop_available(manifest, caller.sub)


def test_local_identity_binds_owner_and_installation(local_orders, monkeypatch):
    service, caller, domain = local_orders
    caller = replace(caller, sub=domain, device_id=None)
    monkeypatch.setattr("core.local_security._proxy_secret", lambda: b"a" * 64)
    first = service.device_caller(caller)
    assert service.device_caller(caller) == first
    assert service.device_caller(replace(caller, sub=domain + " other")).device_id != first.device_id
    monkeypatch.setattr("core.local_security._proxy_secret", lambda: b"b" * 64)
    assert service.device_caller(caller).device_id != first.device_id
    assert service.device_caller(first) == first


def test_provider_free_request_selects_your_connected_tool(local_orders, monkeypatch):
    service, _, domain = local_orders
    monkeypatch.setattr("app.coding_agents_consent.consents", lambda *_args: {"codex": False, "claude": True})
    monkeypatch.setattr("desktop_runtime.agent_status.statuses", lambda: [{"name": "claude",
        "installed": True, "signed_in": True, "subscription_login": True, "available": True}])
    assert service.coding_loop_request(f"Build the app for {domain}") == "claude"
    assert service.coding_loop_request(f"What could you build for {domain}?") is None


def test_requested_plan_keeps_authorization_without_confirmation(local_orders, monkeypatch):
    service, caller, domain = local_orders
    with service.db()() as session:
        session.add(service.Venture(id="v_one", workspace_id="w_one", name=domain, raw_idea=domain,
            stage="draft", created_at=service.utcnow(), updated_at=service.utcnow()))
        session.commit()
    monkeypatch.setattr(service, "_product_build_snapshot", lambda *_args: {"kind": "product_build",
        "artifact_profile": "web_app_vercel_v1", "product_scope": {"profile": "web_app_vercel_v1"}})
    instruction = f"Build the app for {domain} with Codex"
    order = service.prepare_product_build_from_chat("v_one", caller, instruction)
    assert order["state"] == "approved" and order["budget_source"] == "local"
    assert order["requested_build"]["request_sha256"] == hashlib.sha256(instruction.encode()).hexdigest()
    assert json.dumps(instruction) in order["manifest"]["instruction"]
    assert "approve this plan" not in order["manifest"]["instruction"]


def test_requested_implementation_is_bound_and_never_restarted(local_orders, monkeypatch):
    service, caller, domain = local_orders
    caps = service.CodingLoopCaps().model_dump()
    authorization = {"request_sha256": hashlib.sha256(domain.encode()).hexdigest(), "caps": caps}
    source = {"id": "dwo_plan", "requested_build": authorization}
    monkeypatch.setattr(service, "_saved_order", lambda *_args: (None, source))
    monkeypatch.setattr(service, "_review_task_snapshot", lambda *_args, **_kwargs: {"title": domain})
    manifest = service.Manifest(task_id="product_build_v_one", title=domain, instruction="Implement the saved plan",
        applications=["com.companyos.app"], actions=["editor.edit", "terminal.execute"],
        credit_ceiling_micro=0, duration_seconds=caps["max_wall_seconds"], coding_loop_verification={
            "operation": "workspace_loop_v1", "provider": "codex", "workspace_slug": "owned-workspace",
            "profile": "web_app_vercel_v1", "check_recipe": "web_app_vercel_checks_v1", "phase": "implement",
            "network": "none", "read_scope": "venture_workspace", "write_scope": "venture_workspace",
            "plan_order_id": source["id"], "plan_digest": "a" * 64, **caps})
    changed = {**authorization, "request_sha256": "b" * 64}
    with pytest.raises(ValueError, match="device_coding_loop_scope_invalid"):
        service.prepare("v_one", caller, manifest, requested_build=changed)
    order = service.prepare("v_one", caller, manifest, requested_build=authorization)
    with service.db()() as session:
        row = session.get(service.VentureDocument, order["id"])
        order.update(state="stopped", closed_at=service.utcnow().isoformat())
        row.provenance_json = json.dumps(order)
        session.commit()
    assert service.prepare("v_one", caller, manifest, requested_build=authorization)["id"] == order["id"]
    changed = manifest.model_copy(update={"instruction": "Different implementation"})
    with pytest.raises(ValueError, match="device_coding_loop_scope_invalid"):
        service.prepare("v_one", caller, changed, requested_build=authorization)


@pytest.mark.parametrize("profile", ["web_app_vercel_v1", "static_landing_v1"])
def test_failed_unstarted_build_is_closed_without_unlocking_live_work(local_orders, monkeypatch, profile):
    service, caller, domain = local_orders
    monkeypatch.setattr(service, "_review_task_snapshot", lambda *_args, **_kwargs: {"title": domain})
    caps = service.CodingLoopCaps(max_iterations=2).model_dump()
    manifest = service.Manifest(task_id="build_v_one", title=domain, instruction="Implement your approved brief",
        applications=["com.companyos.app"], actions=["editor.edit", "terminal.execute"],
        credit_ceiling_micro=0, duration_seconds=caps["max_wall_seconds"], coding_loop_verification={
            "operation": "workspace_loop_v1", "provider": "codex", "workspace_slug": "owned-workspace",
            "profile": profile, "check_recipe": "static_landing_checks_v1" if profile == "static_landing_v1" else "web_app_vercel_checks_v1",
            "phase": "implement", "network": "none", "read_scope": "venture_workspace", "write_scope": "venture_workspace",
            "plan_order_id": "dwo_plan" if profile == "web_app_vercel_v1" else None,
            "plan_digest": "a" * 64 if profile == "web_app_vercel_v1" else None, **caps})
    order = service.prepare("v_one", caller, manifest)
    order = service.transition("v_one", caller, order["id"], order["review_digest"], "approve")
    other_manifest = manifest.model_copy(update={"task_id": "build_v_other", "title": "Another requested build"})
    with pytest.raises(service.ActiveDeviceOrder) as busy:
        service.prepare("v_other", caller, other_manifest)
    assert busy.value.order["id"] == order["id"]
    for other_caller, digest in [(replace(caller, device_id="another_device"), order["review_digest"]),
                                (replace(caller, sub="another_owner"), order["review_digest"]),
                                (caller, "b" * 64)]:
        with pytest.raises((KeyError, ValueError)):
            service.fail_before_start("v_one", other_caller, order["id"], digest, "coding_checks_unavailable")
    failed = service.fail_before_start("v_one", caller, order["id"], order["review_digest"], "coding_checks_unavailable")
    assert failed["state"] == "failed" and failed["failure_code"] == "coding_checks_unavailable"
    assert failed["closed_at"] == failed["failed_at"] and not failed.get("claimed_at")
    assert service.fail_before_start("v_one", caller, order["id"], order["review_digest"], "coding_checks_unavailable") == failed
    assert service.retrieve("v_one", caller, order["id"])["state"] == "failed"
    assert service.retrieve("v_one", replace(caller, device_id="reconnected_device"), order["id"])["failure_code"] == "coding_checks_unavailable"
    stopped = service.emergency_stop("v_one", caller, order["id"], "chat")
    assert stopped["state"] == "failed" and stopped["failure_code"] == "coding_checks_unavailable"
    assert service.emergency_stop("v_one", caller, order["id"], "chat") == stopped
    next_order = service.prepare("v_other", caller, other_manifest)
    next_order = service.transition("v_other", caller, next_order["id"], next_order["review_digest"], "approve")
    claimed = service.transition("v_other", caller, next_order["id"], next_order["review_digest"], "claim")
    with pytest.raises(ValueError, match="device_order_transition_invalid"):
        service.fail_before_start("v_other", caller, claimed["id"], claimed["review_digest"], "coding_checks_unavailable")
    service.transition("v_other", caller, claimed["id"], claimed["review_digest"], "cancel")
    with pytest.raises(service.ActiveDeviceOrder):
        service.prepare("v_one", caller, manifest)


def test_cancelled_plan_cannot_authorize_continuation(local_orders):
    service, caller, domain = local_orders
    source = {"id": "dwo_plan", "workspace_id": "w_one", "venture_id": "v_one", "actor_id": caller.sub,
        "device_id": caller.device_id, "task_snapshot": {"title": domain}, "expires_at": service.utcnow().isoformat(),
        "state": "stopped", "closed_at": service.utcnow().isoformat(),
        "native_stop": {"processes_stopped": True, "task_verified": True, "verification_kind": "workspace_loop_v1",
            "coding_loop_outcome": "plan_ready"},
        "manifest": {"coding_loop_verification": {"profile": "web_app_vercel_v1", "phase": "plan"}}}
    source["review_digest"] = service._digest(source)
    assert service._closed_product_order(source, phase="plan")
    assert not service._closed_product_order({**source, "stopped_by_user": True}, phase="plan")
    assert not service._closed_product_order({**source, "cancelled_at": source["closed_at"]}, phase="plan")


def test_local_plan_requires_the_tool_to_write_its_package(local_orders):
    service, _, domain = local_orders
    packet = {"execution_id": "dex_plan", "spec": domain + " with /api/health", "commit_sha": "a" * 40,
        "file_manifest_sha256": "b" * 64, "tasks": [{"id": "task-1", "title": domain,
            "files": ["package.json", "index.html"], "acceptance": [{"test_file": "tests/app.test.js",
                "test_name": "renders", "behaviour": "Shows the requested interface"}]}]}
    assert service.ProductPlanReceipt.model_validate(packet).tasks[0].files[0] == "package.json"
    packet["tasks"][0]["files"].remove("package.json")
    with pytest.raises(ValidationError, match="device_coding_plan_invalid"):
        service.ProductPlanReceipt.model_validate(packet)


def test_local_scope_is_rechecked_before_execution(local_orders, monkeypatch):
    from app.build.product_scope import assess

    service, caller, domain = local_orders
    snapshot = {"kind": "product_build", "artifact_profile": "web_app_vercel_v1",
        "product_scope": assess({"request": "A shared database for " + domain}).model_dump()}
    monkeypatch.setattr(service, "_product_build_snapshot", lambda *_args: snapshot)
    manifest = {"task_id": "product_build_v_one", "coding_loop_verification": {
        "profile": "web_app_vercel_v1", "phase": "plan"}}
    with pytest.raises(ValueError, match="device_product_scope_unsupported"):
        service._review_task_snapshot(None, "w_one", "v_one", manifest, actor=caller.sub, device=caller.device_id)


def test_continuation_carries_the_original_request_limits(local_orders, monkeypatch):
    service, caller, domain = local_orders
    caps = service.CodingLoopCaps(max_iterations=3, max_wall_seconds=1800, per_call_seconds=600).model_dump()
    authorization = {"request_sha256": hashlib.sha256(domain.encode()).hexdigest(), "caps": caps}
    source = {"requested_build": authorization, "approved_plan": {"plan_digest": "a" * 64},
        "manifest": {"coding_loop_verification": {"provider": "codex", "workspace_slug": "owned-workspace"}}}
    monkeypatch.setattr(service, "retrieve", lambda *_args: source)
    monkeypatch.setattr(service, "_closed_product_order", lambda *_args, **_kwargs: True)
    prepared = []
    def prepare(_venture, _caller, manifest, **kwargs):
        prepared.append((manifest, kwargs))
        return {"id": "dwo_implementation"}
    monkeypatch.setattr(service, "prepare", prepare)
    service.prepare_product_run("v_one", caller, "dwo_plan")
    manifest, options = prepared[0]
    assert options["requested_build"] == authorization
    assert all(getattr(manifest.coding_loop_verification, field) == limit for field, limit in caps.items())

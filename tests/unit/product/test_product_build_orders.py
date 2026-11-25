"""Separate founder approvals bind one plan, checked source and each deploy."""
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.unit.product.test_device_orders import orders as orders  # noqa: F401


@pytest.mark.parametrize("case", ["stopped", "expired", "uncertain_stop", "changed_plan", "changed_limits"])
def test_local_product_retry_preserves_only_the_original_approval(local_product_orders, monkeypatch, case):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api import device_orders_api as api
    from app.db.models import VentureDocument

    svc, caller = local_product_orders
    monkeypatch.setattr(api, "_verify_caller", lambda *args, **kwargs: caller)
    app = FastAPI()
    app.include_router(api.router)
    with svc.db()() as session:
        idea = session.get(svc.Venture, "v_one").raw_idea
    domain = idea.lower().replace(" ", "-") + ".test"
    plan = svc.prepare_product_build_from_chat("v_one", caller, f"Build the product with my Codex for {domain}")
    planning = svc.transition("v_one", caller, plan["id"], plan["review_digest"], "claim")
    packet = plan_packet(planning)
    packet["spec"] = f"Build {idea} for {domain} with /api/health."
    packet["tasks"][0]["title"] = f"Build {idea}"
    svc.record_product_plan("v_one", caller, plan["id"], svc.ProductPlanReceipt.model_validate(packet))
    svc.record_coding_iteration("v_one", caller, plan["id"], iteration(svc, planning))
    close(svc, caller, planning, task_verified=True)
    order = svc.prepare_product_run("v_one", caller, plan["id"])
    assert order["state"] == "approved"
    if case == "expired":
        later = datetime.fromisoformat(order["expires_at"]) + timedelta(seconds=1)
        monkeypatch.setattr(svc, "utcnow", lambda: later)
        monkeypatch.setattr(api, "utcnow", lambda: later)
    else:
        running = svc.transition("v_one", caller, order["id"], order["review_digest"], "claim")
        if case == "uncertain_stop":
            svc.transition("v_one", caller, order["id"], order["review_digest"], "cancel")
        else:
            close(svc, caller, running)
    if case in {"changed_plan", "changed_limits"}:
        changed_id = plan["id"] if case == "changed_plan" else order["id"]
        with svc.db()() as session:
            row = session.get(VentureDocument, changed_id)
            body = json.loads(row.provenance_json)
            if case == "changed_plan":
                body["approved_plan"]["plan_digest"] = "f" * 64
            else:
                body["manifest"]["coding_loop_verification"]["max_iterations"] += 1
            row.provenance_json = json.dumps(body)
            session.commit()

    client = TestClient(app)
    path = f"/ventures/v_one/device-work-orders/{order['id']}/renew"
    response = client.post(path)
    blocked = {"uncertain_stop": "device_order_requires_reconciliation",
        "changed_plan": "device_coding_plan_unavailable", "changed_limits": "device_order_review_changed"}
    if case in blocked:
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == blocked[case]
        return
    assert response.status_code == 200, response.text
    retry = response.json()
    assert retry["id"] != order["id"] and retry["state"] == "approved"
    assert retry["manifest"] == order["manifest"] and retry["task_snapshot"] == order["task_snapshot"]
    assert retry["requested_build"] == order["requested_build"] and "execution_id" not in retry
    assert retry["review_path"].endswith(f"/device-work-orders/{retry['id']}")
    assert client.post(path).json()["id"] == retry["id"]
    assert datetime.fromisoformat(retry["expires_at"]) > svc.utcnow()
    assert svc.transition("v_one", caller, retry["id"], retry["review_digest"], "claim")["state"] == "claimed"


def manifest(svc, vid="v_one", **changes):
    descriptor = {"operation": "workspace_loop_v1", "provider": "claude", "workspace_slug": "tip-splitter",
        "profile": "web_app_vercel_v1", "check_recipe": "web_app_vercel_checks_v1", "phase": "plan",
        "network": "none", "read_scope": "venture_workspace", "write_scope": "venture_workspace",
        "max_iterations": 2, "max_wall_seconds": 1440, "per_call_seconds": 720, "max_turns": 20, **changes}
    return svc.Manifest(task_id=f"product_build_{vid}", title="Plan a tip splitter", instruction="Write the reviewed plan",
        applications=["com.companyos.app"], origins=[], actions=["editor.edit", "terminal.execute"],
        credit_ceiling_micro=0, duration_seconds=descriptor["max_wall_seconds"], coding_loop_verification=descriptor)


@pytest.fixture
def product_orders(orders, monkeypatch):
    svc, caller, _ = orders
    monkeypatch.setenv("SUPYMEM_CODING_LOOP_ENABLED", "1")
    monkeypatch.setenv("SUPYMEM_CODING_LOOP_ALLOWLIST", json.dumps([caller.sub]))
    def snapshot(session, wid, vid):
        return {"kind": "product_build", "task_id": f"product_build_{vid}", "workspace_id": wid,
            "venture_id": vid, "revision_digest": "b" * 64, "artifact_profile": "web_app_vercel_v1",
            "idea_summary": "A tip splitter", "product_scope": {"profile": "web_app_vercel_v1"}}
    monkeypatch.setattr(svc, "_product_build_snapshot", snapshot)
    return svc, caller


@pytest.fixture(params=["Parts inspection for factories", "Practice tracker for musicians"], ids=["b2b", "consumer"])
def local_product_orders(product_orders, monkeypatch, request):
    from desktop_runtime import agent_status
    from app.coding_agents_consent import CodingAgentConsent, ensure_schema
    from app.core.local_secrets import update_settings
    from app.workspaces.resolve import LOCAL_PERSON_ID
    from app.db.models import CreditAccount
    from app.db.supymem_models import DesktopDevice

    svc, caller = product_orders
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    from app.core.local_owner import _ensure_proxy_secret
    _ensure_proxy_secret()
    for name in ("SUPYMEM_CODING_LOOP_ENABLED", "SUPYMEM_CODING_LOOP_ALLOWLIST", "DEVICE_BROWSER_EXECUTION_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    update_settings("capabilities", {"background_work": False}, {})
    update_settings("spend", {"monthly_spend_cap": 0}, {})
    caller = replace(caller, sub=LOCAL_PERSON_ID, device_id=None)
    with svc.db()() as session:
        ensure_schema(session.get_bind())
        session.delete(session.get(CreditAccount, "cra_device_test"))
        session.delete(session.get(DesktopDevice, "dev_one"))
        for provider in ("claude", "codex", "cursor", "opencode"):
            session.add(CodingAgentConsent(id="consent_" + provider, founder_id=caller.sub,
                provider=provider, consented_at=svc.utcnow()))
        for venture_id in ("v_one", "v_two"):
            session.add(svc.Venture(id=venture_id, workspace_id="w_one", name=request.param,
                raw_idea=request.param, stage="draft", created_at=svc.utcnow(), updated_at=svc.utcnow()))
        session.commit()
    monkeypatch.setattr(agent_status, "statuses", lambda: [{"name": provider, "installed": True,
        "signed_in": True, "subscription_login": True, "available": True} for provider in ("claude", "codex", "cursor", "opencode")])
    original = svc._product_build_snapshot
    monkeypatch.setattr(svc, "_product_build_snapshot", lambda session, wid, vid: {
        **original(session, wid, vid), "idea_summary": request.param})
    return svc, caller


@pytest.mark.parametrize("provider", ["Codex", "Claude Code"])
def test_local_requested_build_authorizes_plan_and_bounded_run_once_without_wallet(local_product_orders, provider):
    from app.build import device_invocations
    from app.db.models import CreditAccount, CreditReservation
    from sqlalchemy import select

    svc, caller = local_product_orders
    instruction = "Build the product with my " + provider
    plan = svc.prepare_product_build_from_chat("v_one", caller, instruction)
    assert plan["state"] == "approved" and plan["budget_source"] == "local"
    assert plan["requested_build"]["request_sha256"] == hashlib.sha256(instruction.encode()).hexdigest()
    assert svc.prepare_product_build_from_chat("v_one", caller, instruction)["id"] == plan["id"]
    claimed = svc.transition("v_one", caller, plan["id"], plan["review_digest"], "claim")
    with pytest.raises(ValueError, match="device_order_transition_invalid"):
        svc.transition("v_one", caller, plan["id"], plan["review_digest"], "claim")
    packet = svc.ProductPlanReceipt.model_validate(plan_packet(claimed))
    svc.record_product_plan("v_one", caller, plan["id"], packet)
    svc.record_coding_iteration("v_one", caller, plan["id"], iteration(svc, claimed))
    close(svc, caller, claimed, task_verified=True)
    run = svc.prepare_product_run("v_one", caller, plan["id"])
    assert run["state"] == "approved" and run["requested_build"] == plan["requested_build"]
    assert svc.prepare_product_run("v_one", caller, plan["id"])["id"] == run["id"]
    built = svc.transition("v_one", caller, run["id"], run["review_digest"], "claim")
    device_invocations.check_current("v_one", svc.device_caller(caller), run["id"])
    svc.record_coding_iteration("v_one", caller, run["id"], iteration(svc, built))
    stopped = close(svc, caller, built, task_verified=True)
    assert stopped["state"] == "stopped" and stopped["settled_micro"] == 0
    assert svc.prepare_product_run("v_one", caller, plan["id"])["state"] == "stopped"
    with svc.db()() as session:
        assert session.scalar(select(CreditAccount.id)) is None
        assert session.scalar(select(CreditReservation.id)) is None


def test_local_full_activity_keeps_every_saved_step_without_expanding_the_status_receipt(local_product_orders):
    svc, caller = local_product_orders
    claimed = claim(svc, caller, svc.prepare("v_one", caller, manifest(svc)))
    for seq in range(1, 53):
        svc.record_coding_event("v_one", caller, claimed["id"], svc.CodingActivityEvent(
            execution_id=claimed["execution_id"], seq=seq, at=float(seq), text=f"Saved step {seq}"))
    assert len(svc.retrieve("v_one", caller, claimed["id"])["events"]) == 50
    steps = svc.coding_events("v_one", caller, claimed["id"])["events"]
    assert [row["text"] for row in steps] == [f"Saved step {seq}" for seq in range(1, 53)]
    with pytest.raises(KeyError):
        svc.coding_events("v_two", caller, claimed["id"])


def test_local_requested_landing_build_does_not_require_a_product_plan(local_product_orders, monkeypatch):
    svc, caller = local_product_orders
    with svc.db()() as session:
        instruction = session.get(svc.Venture, "v_one").name + " landing page"

    def snapshot(session, wid, vid, job_id, actor):
        assert (wid, vid, job_id, actor) == ("w_one", "v_one", "job_requested_page", caller.sub)
        return {"kind": "landing_page_build", "artifact_profile": "static_landing_v1",
            "workspace_id": wid, "venture_id": vid, "page_brief": instruction}

    monkeypatch.setattr(svc, "_landing_page_snapshot", snapshot)
    order = svc.prepare_landing_page_from_job("v_one", caller, "job_requested_page", instruction, provider="codex")
    assert order["state"] == "approved" and order["budget_source"] == "local"
    assert order["manifest"]["coding_loop_verification"]["profile"] == "static_landing_v1"
    assert "plan_order_id" not in order["manifest"]["coding_loop_verification"]
    assert svc.prepare_landing_page_from_job("v_one", caller, "job_requested_page", instruction, provider="codex")["id"] == order["id"]
    assert svc.transition("v_one", caller, order["id"], order["review_digest"], "claim")["state"] == "claimed"


@pytest.mark.parametrize("change", ["consent", "api_login", "capability"])
@pytest.mark.parametrize("stage", ["before_claim", "during_run"])
def test_local_tool_consent_login_and_owner_switch_recheck_before_claim_and_keep_stop(local_product_orders, monkeypatch, change, stage):
    from desktop_runtime import agent_status
    from app.coding_agents_consent import CodingAgentConsent
    from app.core.local_secrets import update_settings
    from fastapi import HTTPException

    svc, caller = local_product_orders
    plan = svc.prepare_product_build_from_chat("v_one", caller, "Build the product with my Codex")
    claimed = svc.transition("v_one", caller, plan["id"], plan["review_digest"], "claim") if stage == "during_run" else None
    if change == "consent":
        with svc.db()() as session:
            session.delete(session.get(CodingAgentConsent, "consent_codex"))
            session.commit()
    elif change == "api_login":
        monkeypatch.setattr(agent_status, "statuses", lambda: [{"name": "codex", "installed": True,
            "signed_in": True, "subscription_login": False, "available": False}])
    else:
        update_settings("capabilities", {"build_with_my_tool": False}, {})
    with pytest.raises((ValueError, HTTPException)):
        if claimed:
            from app.build import device_invocations
            device_invocations.check_current("v_one", svc.device_caller(caller), plan["id"])
        else:
            svc.transition("v_one", caller, plan["id"], plan["review_digest"], "claim")
    assert svc.transition("v_one", caller, plan["id"], plan["review_digest"], "cancel")["state"] == "cancelled"
    if claimed:
        assert close(svc, caller, claimed)["state"] == "stopped"


def test_local_utility_chat_saves_and_retries_the_exact_scoped_build(local_product_orders, monkeypatch):
    from app.api import device_orders_api
    from app.chat.execute import _start_tool_build
    from app.core.local_owner import ensure_owner

    svc, caller = local_product_orders
    owner = ensure_owner()
    monkeypatch.setattr("app.core.local_owner.current_owner", lambda: owner)
    monkeypatch.setattr(device_orders_api, "verify_caller", lambda *_args, **_kwargs: caller)
    with svc.db()() as session:
        venture = session.get(svc.Venture, "v_one")
        domain = venture.raw_idea
        session.add(svc.Workspace(id=venture.workspace_id, kind="venture", name=domain, entry_mode="idea",
            owner_id=caller.sub, created_at=svc.utcnow(), updated_at=svc.utcnow()))
        session.commit()
    request = f"Write a local script to summarize participation for {domain}. Do not run it on my data."
    reply = _start_tool_build("v_one", {"text": request}, caller.sub)
    assert reply["kind"] == "build_tool_started", reply["body_md"]
    watch = reply["actions"][0]
    order = svc.retrieve("v_one", caller, watch["payload"]["order_id"])
    assert watch["href"].endswith(order["id"])
    assert order["state"] == "approved"
    assert order["task_snapshot"]["artifact_profile"] == "workspace_utility_v1"
    assert order["manifest"]["instruction"].endswith(json.dumps(request))
    assert order["manifest"]["coding_loop_verification"]["network"] == "none"
    svc.transition("v_one", caller, order["id"], order["review_digest"], "cancel")
    retry = device_orders_api.renew("v_one", order["id"], None)
    assert retry["id"] != order["id"] and retry["state"] == "approved"
    assert retry["manifest"] == order["manifest"] and retry["requested_build"] == order["requested_build"]


def test_local_build_capability_is_ready_only_for_connected_subscription_tool(local_product_orders, monkeypatch):
    from desktop_runtime import agent_status
    from app.core.local_capabilities import capability
    from app.core.local_secrets import update_settings

    assert capability("build_with_my_tool")["state"] == "ready"
    monkeypatch.setattr(agent_status, "statuses", lambda: [{"name": "codex", "installed": True,
        "signed_in": True, "subscription_login": False, "available": False}])
    blocked = capability("build_with_my_tool")
    assert blocked["state"] == "needs_setup" and "Keys and connections" in blocked["next_step"]
    assert "subscription" in blocked["next_step"]
    update_settings("capabilities", {"build_with_my_tool": False}, {})
    assert capability("build_with_my_tool")["state"] == "off"


def test_local_unrequested_plan_never_inherits_build_authorization(local_product_orders):
    svc, caller = local_product_orders
    plan = completed_plan(svc, caller)
    run = svc.prepare_product_run("v_one", caller, plan["id"])
    assert run["state"] == "pending" and "requested_build" not in run


def test_local_plan_accepts_only_tool_authored_scaffolding_paths(local_product_orders):
    svc, caller = local_product_orders
    plan = svc.prepare_product_build_from_chat("v_one", caller, "Build the product with my Codex")
    claimed = svc.transition("v_one", caller, plan["id"], plan["review_digest"], "claim")
    packet = plan_packet(claimed)
    packet["spec"] = claimed["task_snapshot"]["idea_summary"] + " with /api/health and /api/split."
    packet["tasks"][0].update(title=packet["spec"], files=["index.html", "package.json", "Dockerfile"])
    assert svc.ProductPlanReceipt.model_validate(packet).tasks[0].files == packet["tasks"][0]["files"]
    for unsafe in ("tests/package.json", "Package.JSON", ".git/config", "tests/.npmrc"):
        packet["tasks"][0]["files"] = ["package.json", unsafe]
        with pytest.raises(ValidationError, match="device_coding_plan_invalid"):
            svc.ProductPlanReceipt.model_validate(packet)
    packet["tasks"][0]["files"] = ["index.html"]
    with pytest.raises(ValidationError, match="device_coding_plan_invalid"):
        svc.ProductPlanReceipt.model_validate(packet)


def test_local_provider_free_request_selects_a_verified_consented_tool(local_product_orders, monkeypatch):
    from app.chat import product_build_request

    svc, caller = local_product_orders
    monkeypatch.setattr(product_build_request, "is_product_build_request", lambda text: text == "Build this")
    plan = svc.prepare_product_build_from_chat("v_one", caller, "Build this")
    assert plan["state"] == "approved" and plan["manifest"]["coding_loop_verification"]["provider"] == "codex"


def test_local_ambiguous_tool_choice_never_silently_switches_tools(local_product_orders, monkeypatch):
    from app.chat import product_build_request
    from app.api.device_orders_api import call
    from fastapi import HTTPException
    from app.db.models import VentureDocument
    from sqlalchemy import select

    svc, caller = local_product_orders
    monkeypatch.setattr(product_build_request, "is_product_build_request", lambda text: True)
    with pytest.raises(HTTPException) as refused:
        call(svc.prepare_product_build_from_chat, "v_one", caller, "Build this with Claude Code and Codex")
    assert refused.value.status_code == 409 and refused.value.detail["code"] == "device_coding_provider_unsupported"
    assert "Keys and connections" in refused.value.detail["message"]
    with svc.db()() as session:
        assert session.scalar(select(VentureDocument.id).where(VentureDocument.kind == svc.KIND)) is None


@pytest.mark.parametrize("state", ["disconnected", "not_allowed"])
def test_local_unavailable_opencode_never_silently_switches_tools(local_product_orders, monkeypatch, state):
    from fastapi import HTTPException
    from sqlalchemy import select
    from app.api.device_orders_api import call
    from app.coding_agents_consent import CodingAgentConsent
    from app.db.models import VentureDocument
    from desktop_runtime import agent_status

    svc, caller = local_product_orders
    if state == "disconnected":
        statuses = [tool for tool in agent_status.statuses() if tool["name"] != "opencode"]
        monkeypatch.setattr(agent_status, "statuses", lambda: statuses)
    else:
        with svc.db()() as session:
            session.delete(session.get(CodingAgentConsent, "consent_opencode"))
            session.commit()
    with pytest.raises(HTTPException) as refused:
        call(svc.prepare_product_build_from_chat, "v_one", caller, "Build this with OpenCode")
    assert refused.value.status_code == 409 and refused.value.detail["code"] == "coding_tool_not_connected"
    assert "OpenCode" in refused.value.detail["message"]
    assert "Settings > Keys and connections > Coding tools" in refused.value.detail["message"]
    with svc.db()() as session:
        assert session.scalar(select(VentureDocument.id).where(VentureDocument.kind == svc.KIND)) is None


@pytest.mark.parametrize("tool,provider", [("Cursor", "cursor"), ("OpenCode", "opencode")])
def test_local_connected_tool_prepares_the_same_bounded_product_plan(local_product_orders, tool, provider):
    svc, caller = local_product_orders
    plan = svc.prepare_product_build_from_chat("v_one", caller, "Build this with " + tool)
    descriptor = plan["manifest"]["coding_loop_verification"]
    assert plan["state"] == "approved"
    assert descriptor["provider"] == provider
    assert descriptor["network"] == "none"
    assert descriptor["read_scope"] == descriptor["write_scope"] == "venture_workspace"
    assert (descriptor["max_iterations"], descriptor["max_wall_seconds"], descriptor["per_call_seconds"]) == (2, 1440, 720)
    assert descriptor.get("max_turns") is None


def test_local_stopped_requested_plan_never_authorizes_a_successor(local_product_orders):
    svc, caller = local_product_orders
    plan = svc.prepare_product_build_from_chat("v_one", caller, "Build the product with my Codex")
    claimed = svc.transition("v_one", caller, plan["id"], plan["review_digest"], "claim")
    svc.record_product_plan("v_one", caller, plan["id"], svc.ProductPlanReceipt.model_validate(plan_packet(claimed)))
    svc.record_coding_iteration("v_one", caller, plan["id"], iteration(svc, claimed))
    svc.transition("v_one", caller, plan["id"], plan["review_digest"], "cancel")
    close(svc, caller, claimed, task_verified=True)
    with pytest.raises(ValueError, match="device_coding_plan_unavailable"):
        svc.prepare_product_run("v_one", caller, plan["id"])


def test_local_requested_run_cannot_expand_authorized_caps_or_change_digest(local_product_orders):
    from app.db.models import VentureDocument

    svc, caller = local_product_orders
    plan = svc.prepare_product_build_from_chat("v_one", caller, "Build the product with my Codex")
    claimed = svc.transition("v_one", caller, plan["id"], plan["review_digest"], "claim")
    svc.record_product_plan("v_one", caller, plan["id"], svc.ProductPlanReceipt.model_validate(plan_packet(claimed)))
    svc.record_coding_iteration("v_one", caller, plan["id"], iteration(svc, claimed))
    close(svc, caller, claimed, task_verified=True)
    with pytest.raises(ValueError, match="device_coding_loop_scope_invalid"):
        svc.prepare_product_run("v_one", caller, plan["id"], svc.CodingLoopCaps(max_iterations=10))
    svc.prepare_product_run("v_one", caller, plan["id"])
    with pytest.raises(ValueError, match="device_coding_loop_scope_invalid"):
        svc.prepare_product_run("v_one", caller, plan["id"], svc.CodingLoopCaps(max_iterations=1))
    with svc.db()() as session:
        row = session.get(VentureDocument, plan["id"])
        body = json.loads(row.provenance_json)
        body["requested_build"]["caps"]["max_iterations"] = 10
        row.provenance_json = json.dumps(body)
        session.commit()
    with pytest.raises(ValueError, match="device_coding_plan_unavailable"):
        svc.prepare_product_run("v_one", caller, plan["id"])


@pytest.mark.parametrize("requirement", ["with secure login and accounts", "with a shared database", "with Stripe payments"])
def test_local_requested_deliverable_is_not_silently_reduced_to_a_demo(local_product_orders, requirement):
    from app.api.device_orders_api import call
    from app.db.models import VentureDocument
    from fastapi import HTTPException
    from sqlalchemy import select

    svc, caller = local_product_orders
    with pytest.raises(HTTPException) as refused:
        call(svc.prepare_product_build_from_chat, "v_one", caller, "Build the product with my Codex " + requirement)
    assert refused.value.status_code == 409
    assert refused.value.detail["code"] == "device_product_scope_unsupported"
    assert "venture chat" in refused.value.detail["message"]
    with svc.db()() as session:
        assert session.scalar(select(VentureDocument.id).where(VentureDocument.kind == svc.KIND)) is None


def test_local_api_lists_owned_builds_and_records_requested_work_without_confirmation(local_product_orders, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api import device_orders_api as api
    from app.chat import store
    from app.analytics import events

    svc, caller = local_product_orders
    monkeypatch.setattr(api, "_verify_caller", lambda *args, **kwargs: caller)
    messages = []
    monkeypatch.setattr(store, "append_message", lambda *args, **kwargs: messages.append(kwargs))
    monkeypatch.setattr(events, "emit_once", lambda *args, **kwargs: None)
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)
    response = client.post("/ventures/v_one/device-work-orders/from-chat", json={"instruction": "Build the product with my Codex"})
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["state"] == "approved"
    assert not any(message.get("kind") == "confirmation" for message in messages)
    assert any("Watch" in action["label"] for message in messages for action in message.get("actions", []))
    listed = client.get("/ventures/v_one/device-work-orders")
    assert listed.status_code == 200 and [order["id"] for order in listed.json()["orders"]] == [plan["id"]]
    status = client.get(f"/ventures/v_one/device-work-orders/{plan['id']}/capabilities").json()
    assert status["available"] is True and status["coding_loop_v1"] is True


def claim(svc, caller, order):
    approved = svc.transition("v_one", caller, order["id"], order["review_digest"], "approve")
    return svc.transition("v_one", caller, order["id"], approved["review_digest"], "claim")


def plan_packet(body, **changes):
    from app.core.local_mode import is_local

    files = ["index.html", "api/split.js"]
    if is_local():
        files.append("package.json")
    return {"execution_id": body["execution_id"], "spec": "Tip splitter with /api/split and /api/health.",
        "tasks": [{"id": "split", "title": "Split the bill", "files": files,
            "acceptance": [{"test_file": "tests/split.test.mjs", "test_name": "zero tip", "behaviour": "Keeps the total unchanged"}]}],
        "commit_sha": "a" * 40, "file_manifest_sha256": "c" * 64, **changes}


def iteration(svc, body, **changes):
    plan = body["manifest"]["coding_loop_verification"]["phase"] == "plan"
    return svc.CodingLoopIteration.model_validate({"execution_id": body["execution_id"], "index": 0,
        "phase": "plan" if plan else "code", "outcome": "plan_ready" if plan else "pass",
        "summary": "Your plan is ready" if plan else "Your checks passed", "diagnosis": {},
        "commit_sha": "a" * 40, "file_manifest_sha256": "c" * 64,
        "caps_used": {"iterations": 1, "wall_seconds": 10}, "evidence_sha256": "d" * 64, **changes})


def close(svc, caller, body, *, task_verified=None):
    from app.build import device_stop
    caller = replace(caller, device_id=body["device_id"])
    return device_stop.record("v_one", caller, body["id"], execution_id=body["execution_id"],
        evidence_digest="e" * 64, processes_stopped=True, task_verified=task_verified)


def completed_plan(svc, caller):
    body = claim(svc, caller, svc.prepare("v_one", caller, manifest(svc)))
    svc.record_product_plan("v_one", caller, body["id"], svc.ProductPlanReceipt.model_validate(plan_packet(body)))
    svc.record_coding_iteration("v_one", caller, body["id"], iteration(svc, body))
    close(svc, caller, body, task_verified=True)
    return svc.retrieve("v_one", caller, body["id"])


def _order_account_state(svc, order_id):
    from sqlalchemy import select
    from app.db.models import CreditAccount, CreditLedgerEntry, CreditReservation, VentureDocument
    with svc.db()() as session:
        tables = [CreditAccount.__table__, CreditReservation.__table__, CreditLedgerEntry.__table__]
        order = session.get(VentureDocument, order_id)
        return (order.provenance_json, order.updated_at, [
            [dict(row) for row in session.execute(select(table).order_by(*table.primary_key.columns)).mappings()]
            for table in tables])


def test_conditional_cancel_replaces_only_the_exact_pending_product_review(product_orders):
    svc, caller = product_orders
    order = svc.prepare("v_one", caller, manifest(svc))
    cancelled = svc.transition("v_one", caller, order["id"], order["review_digest"], "cancel", unstarted_only=True)
    assert cancelled["state"] == "cancelled" and not cancelled.get("claimed_at") and not cancelled.get("execution_id")
    replacement = svc.prepare("v_one", caller, manifest(svc, max_turns=30))
    assert replacement["state"] == "pending" and replacement["id"] != order["id"]
    assert not replacement.get("claimed_at") and not replacement.get("execution_id")


@pytest.mark.parametrize("change", ["approved", "claimed", "late_claim", "reconnected_device",
                                  "wrong_digest", "execution_id", "claimed_at", "non_cancel"])
def test_conditional_cancel_rejects_changed_custody_without_mutating_state(product_orders, change):
    from app.db.models import VentureDocument
    svc, caller = product_orders
    order = svc.prepare("v_one", caller, manifest(svc))
    original_digest = order["review_digest"]
    if change in {"approved", "claimed", "late_claim"}:
        order = svc.transition("v_one", caller, order["id"], original_digest, "approve")
        assert order["review_digest"] == original_digest  # Early approval need not change the review digest.
        if change != "approved":
            order = svc.transition("v_one", caller, order["id"], order["review_digest"], "claim")
    if change in {"execution_id", "claimed_at"}:
        with svc.db()() as session:
            row = session.get(VentureDocument, order["id"])
            body = json.loads(row.provenance_json)
            body[change] = "dex_existing" if change == "execution_id" else svc.utcnow().isoformat()
            row.provenance_json = json.dumps(body)
            session.commit()
    before = _order_account_state(svc, order["id"])
    actor = replace(caller, device_id="dev_reconnected") if change == "reconnected_device" else caller
    digest = "f" * 64 if change == "wrong_digest" else original_digest if change == "late_claim" else order["review_digest"]
    error = "device_order_review_changed" if change == "wrong_digest" else "device_order_transition_invalid"
    with pytest.raises(ValueError, match="^" + error + "$"):
        svc.transition("v_one", actor, order["id"], digest,
                       "approve" if change == "non_cancel" else "cancel", unstarted_only=True)
    assert _order_account_state(svc, order["id"]) == before
    if change in {"approved", "claimed", "reconnected_device"}:
        # The optional condition must not alter the founder's ordinary Stop/cancel path.
        assert svc.transition("v_one", actor, order["id"], order["review_digest"], "cancel")["state"] == "cancelled"


def test_scope_calls_out_demo_storage_and_unsupported_dependencies(product_orders):
    from app.build.product_scope import assess
    scope = assess({"request": "A tip splitter that saves recent splits between devices, with login and Stripe payments"})
    assert scope.profile == "web_app_vercel_v1"
    assert any("small API" in sentence for sentence in scope.in_scope)
    assert any("between devices" in sentence for sentence in scope.not_in_this_build)
    assert any("sign-in" in sentence for sentence in scope.not_in_this_build)
    assert any("Payments" in sentence for sentence in scope.not_in_this_build)


@pytest.mark.parametrize("name", ["product_build_plan_manifest.json", "product_build_run_manifest.json",
    "product_deploy_preview_manifest.json", "product_deploy_production_manifest.json"])
def test_native_wire_fixtures_roundtrip_the_same_core_contract(product_orders, name):
    svc, _ = product_orders
    packet = json.loads((Path(__file__).parents[2] / "fixtures" / name).read_text())
    assert svc.Manifest.model_validate(packet).model_dump(exclude_none=True) == packet


def test_api_parser_gate_and_bounded_receipts_do_not_echo_rejected_secrets(product_orders, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api import device_orders_api as api
    svc, caller = product_orders
    monkeypatch.setattr(api, "verify_caller", lambda *args, **kwargs: caller)
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)
    order = svc.prepare("v_one", caller, manifest(svc))
    base = f"/ventures/v_one/device-work-orders/{order['id']}"
    missing = client.post(base + "/approve", json={"review_digest": order["review_digest"]})
    assert missing.status_code == 409 and missing.json()["detail"]["code"] == "device_product_build_parser_unavailable"
    approved = client.post(base + "/approve", json={"review_digest": order["review_digest"], "product_build_parser_supported": True})
    assert approved.status_code == 200
    body = svc.transition("v_one", caller, order["id"], approved.json()["review_digest"], "claim")
    secret = "api_key=" + "s" * 40
    response = client.post(base + "/plan", json=plan_packet(body, spec=secret + " /api/health"))
    assert response.status_code == 422 and secret not in response.text
    response = client.post(base + "/plan", content=b"x" * 32769)
    assert response.status_code == 413
    assert client.post(base + "/plan", json=plan_packet(body)).status_code == 200
    assert client.get(base + "/plan").json()["plan"]["spec"] == plan_packet(body)["spec"]


@pytest.mark.parametrize("changes", [{"max_iterations": 3}, {"provider": "codex"}, {"network": "enabled"},
    {"max_turns": None}, {"per_call_seconds": 1441}, {"plan_digest": "a" * 64}, {"phase": "implement"}])
def test_loop_scope_cannot_expand_or_implement_without_approved_plan(product_orders, changes):
    svc, _ = product_orders
    with pytest.raises(ValidationError):
        manifest(svc, **changes)
    assert manifest(svc, provider="codex", max_turns=None).coding_loop_verification.max_turns is None


def test_default_off_exact_person_and_revocation_keep_cancellation(product_orders, monkeypatch):
    svc, caller = product_orders
    for people in ([], ["other"], ["*"], "founder", [caller.sub, 2]):
        monkeypatch.setenv("SUPYMEM_CODING_LOOP_ALLOWLIST", json.dumps(people))
        with pytest.raises(ValueError, match="device_coding_loop_unavailable"):
            svc.prepare("v_one", caller, manifest(svc))
    monkeypatch.setenv("SUPYMEM_CODING_LOOP_ALLOWLIST", json.dumps([caller.sub]))
    body = svc.prepare("v_one", caller, manifest(svc))
    monkeypatch.delenv("SUPYMEM_CODING_LOOP_ENABLED")
    with pytest.raises(ValueError, match="device_coding_loop_unavailable"):
        svc.transition("v_one", caller, body["id"], body["review_digest"], "approve")
    assert svc.transition("v_one", caller, body["id"], body["review_digest"], "cancel")["state"] == "cancelled"


def test_one_pending_product_order_per_device_even_across_ventures(product_orders):
    svc, caller = product_orders
    first = svc.prepare("v_one", caller, manifest(svc))
    assert svc.prepare("v_one", caller, manifest(svc))["id"] == first["id"]
    with pytest.raises(svc.ActiveDeviceOrder):
        svc.prepare("v_two", caller, manifest(svc, "v_two"))


@pytest.mark.parametrize("domain", ["Wholesale inventory checks", "Weekend walking journal"])
@pytest.mark.parametrize("local", [False, True])
@pytest.mark.parametrize("path", ["package.json", "src/package.json", "tests/package.json", ".env", "src/../package.json"])
def test_local_plan_accepts_root_package_metadata_without_expanding_paths(monkeypatch, domain, local, path):
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    from app.build import device_orders

    monkeypatch.setenv("SUPYMEM_MODE", "local" if local else "hosted")
    packet = plan_packet({"execution_id": "dex_one"}, spec="# " + domain + "\nServe /api/health.")
    packet["tasks"][0]["files"] = [path]
    if local and path == "package.json":
        receipt = device_orders.ProductPlanReceipt.model_validate(packet)
        assert receipt.tasks[0].files == ["package.json"]
        with pytest.raises(ValueError, match="device_coding_plan_invalid"):
            device_orders._plan_path(path, test=True)
    else:
        with pytest.raises(ValidationError):
            device_orders.ProductPlanReceipt.model_validate(packet)


@pytest.mark.parametrize("domain", ["b2b", "consumer"])
def test_landing_snapshot_keeps_the_plan_owned_by_your_tool(monkeypatch, domain):
    from types import SimpleNamespace
    from tests.company_os_harness import load_company_os_app
    from tests.unit.pagebuilder.test_pipeline import customer_draft

    load_company_os_app()
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    from app.build import device_orders
    from app.pagebuilder import api, models, runtime

    inputs = models.PageInput.model_validate(customer_draft(domain)[0])
    doc = {"_actor_subject": "owner", "_runtime": {"state": "running", "input": inputs.model_dump(mode="json")},
        "tool_build": {"plan": None, "brief": "Build only the confirmed venture: " + inputs.idea}}
    row = SimpleNamespace(kind=api.KIND, workspace_id="workspace", venture_id=inputs.venture_id,
        payload_json=json.dumps(doc), cancel_requested=False, status="leased")
    validations = []
    monkeypatch.setattr(runtime, "validate_inputs", lambda *args: validations.append(args))
    session = SimpleNamespace(get=lambda model, identifier: row if identifier == "page_job" else None)
    snapshot = device_orders._landing_page_snapshot(session, "workspace", inputs.venture_id, "page_job", "owner")
    assert snapshot["page_plan"] is None and snapshot["page_input"] == inputs.model_dump(mode="json")
    assert snapshot["page_brief"] == doc["tool_build"]["brief"] and len(validations) == 1


@pytest.mark.parametrize("change", [{"spec": "No health endpoint"}, {"spec": "ü" * 10000},
    {"plan_digest": "f" * 64}, {"spec": "api_key=" + "s" * 40}])
def test_plan_is_bounded_and_digest_cannot_be_supplied_arbitrarily(product_orders, change):
    svc, _ = product_orders
    with pytest.raises(ValidationError):
        svc.ProductPlanReceipt.model_validate(plan_packet({"execution_id": "dex_one"}, **change))


@pytest.mark.parametrize("path", ["../api/a.js", "/api/a.js", ".git/hooks/test", "api/.env", "review/spec.md", "package.json",
    "AGENTS.md", "Claude.local.md", "node_Modules/a.js", "api/a b.js", "a" * 181])
def test_plan_paths_are_inside_the_owned_product_policy(product_orders, path):
    svc, _ = product_orders
    packet = plan_packet({"execution_id": "dex_one"})
    packet["tasks"][0]["files"] = [path]
    with pytest.raises(ValidationError):
        svc.ProductPlanReceipt.model_validate(packet)


def test_three_separate_approvals_bind_plan_tested_source_and_deployment(product_orders):
    from app.db.models import CreditAccount, CreditReservation
    svc, caller = product_orders
    plan = completed_plan(svc, caller)
    digest = hashlib.sha256(json.dumps({"spec": plan["approved_plan"]["spec"], "tasks": plan["approved_plan"]["tasks"]},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    assert plan["approved_plan"]["plan_digest"] == digest
    run = svc.prepare_product_run("v_one", caller, plan["id"])
    assert run["state"] == "pending" and run["id"] != plan["id"]
    assert run["manifest"]["coding_loop_verification"]["plan_digest"] == digest
    assert run["task_snapshot"]["approved_plan"] == plan["approved_plan"]
    body = claim(svc, caller, run)
    with pytest.raises(ValueError, match="device_deploy_source_unavailable"):
        svc.prepare_product_deploy("v_one", caller, body["id"], "preview")
    svc.record_coding_iteration("v_one", caller, body["id"], iteration(svc, body))
    close(svc, caller, body, task_verified=True)
    preview = svc.prepare_product_deploy("v_one", caller, body["id"], "preview")
    assert preview["state"] == "pending" and preview["manifest"]["deploy_verification"]["source_commit"] == "a" * 40
    with pytest.raises(ValueError):
        svc.transition("v_one", caller, preview["id"], preview["review_digest"], "claim")
    deployed = claim(svc, caller, preview)
    receipt = svc.ProductDeployReceipt(execution_id=deployed["execution_id"], provider="vercel", target="preview",
        status="protected", url="https://tip-splitter.vercel.app", exit_status=0, http_status=401,
        source_commit="a" * 40, file_manifest_sha256="c" * 64, project_link_sha256="f" * 64)
    saved = svc.record_product_deploy("v_one", caller, deployed["id"], receipt)
    assert svc.record_product_deploy("v_one", caller, deployed["id"], receipt) == saved
    close(svc, caller, deployed, task_verified=True)
    production = svc.prepare_product_deploy("v_one", caller, body["id"], "production")
    assert production["state"] == "pending" and production["id"] != deployed["id"]
    assert production["manifest"]["deploy_verification"]["project_link_sha256"] == "f" * 64
    with svc.db()() as session:
        holds = session.query(CreditReservation).all()
        assert len(holds) == 3 and all(hold.amount_micro == 0 and hold.status == "settled" for hold in holds)
        account = session.get(CreditAccount, "cra_device_test")
        assert account.spent_micro == account.reserved_micro == 0


def test_plan_and_iteration_replays_are_identical_and_owner_bound(product_orders, monkeypatch):
    svc, caller = product_orders
    body = claim(svc, caller, svc.prepare("v_one", caller, manifest(svc)))
    args = ("v_one", caller, body["id"])
    packet = svc.ProductPlanReceipt.model_validate(plan_packet(body))
    first = svc.record_product_plan(*args, packet)
    assert svc.record_product_plan(*args, packet) == first
    with pytest.raises(ValueError, match="conflict"):
        svc.record_product_plan(*args, svc.ProductPlanReceipt.model_validate(plan_packet(body, spec=packet.spec + " altered")))
    with pytest.raises(KeyError):
        svc.record_product_plan("v_one", replace(caller, device_id="other"), body["id"], packet)
    close(svc, caller, body)
    monkeypatch.delenv("SUPYMEM_CODING_LOOP_ENABLED")
    first = svc.record_coding_iteration(*args, iteration(svc, body))
    assert svc.record_coding_iteration(*args, iteration(svc, body)) == first
    with pytest.raises(ValueError, match="conflict"):
        svc.record_coding_iteration(*args, iteration(svc, body, summary="A different outcome"))
    with pytest.raises(ValueError, match="order_invalid"):
        svc.record_coding_iteration(*args, iteration(svc, body, index=2, caps_used={"iterations": 3, "wall_seconds": 20}))


def test_current_idea_change_invalidates_reviewed_implementation(product_orders, monkeypatch):
    svc, caller = product_orders
    plan = completed_plan(svc, caller)
    run = svc.prepare_product_run("v_one", caller, plan["id"])
    original = svc._product_build_snapshot
    monkeypatch.setattr(svc, "_product_build_snapshot", lambda *args: {**original(*args), "revision_digest": "f" * 64})
    with pytest.raises(ValueError):
        svc.transition("v_one", caller, run["id"], run["review_digest"], "approve")


def test_partial_task_or_unverified_stop_never_authorizes_deploy(product_orders):
    svc, caller = product_orders
    plan = completed_plan(svc, caller)
    body = claim(svc, caller, svc.prepare_product_run("v_one", caller, plan["id"]))
    partial = iteration(svc, body, outcome="task_passed", file_manifest_sha256=None)
    svc.record_coding_iteration("v_one", caller, body["id"], partial)
    complete = iteration(svc, body, index=1, caps_used={"iterations": 2, "wall_seconds": 20})
    svc.record_coding_iteration("v_one", caller, body["id"], complete)
    close(svc, caller, body)
    with pytest.raises(ValueError, match="device_deploy_source_unavailable"):
        svc.prepare_product_deploy("v_one", caller, body["id"], "preview")


@pytest.mark.parametrize("lapse", ["expired", "cancelled"])
def test_product_renewal_preserves_review_and_never_replays_execution(product_orders, monkeypatch, lapse):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api import device_orders_api as api
    svc, caller = product_orders
    monkeypatch.setattr(api, "verify_caller", lambda *args, **kwargs: caller)
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)
    clock = [svc.utcnow()]
    monkeypatch.setattr(svc, "utcnow", lambda: clock[0])
    monkeypatch.setattr(api, "utcnow", lambda: clock[0])
    def renew(order):
        return client.post(f"/ventures/v_one/device-work-orders/{order['id']}/renew")
    def lapse_review(order):
        if lapse == "expired":
            clock[0] = max(clock[0], datetime.fromisoformat(order["expires_at"])) + timedelta(seconds=1)
        else:
            svc.transition("v_one", caller, order["id"], order["review_digest"], "cancel")
    plan = completed_plan(svc, caller)
    caps = svc.CodingLoopCaps(max_iterations=4, max_wall_seconds=1200, per_call_seconds=600, max_turns=15)
    run = svc.prepare_product_run("v_one", caller, plan["id"], caps)
    assert renew(run).status_code == 409  # Active, unexpired reviews do not renew.
    lapse_review(run)
    response = renew(run)
    assert response.status_code == 200, response.text
    fresh = response.json()
    assert fresh["id"] != run["id"] and fresh["state"] == "pending" and "execution_id" not in fresh
    assert fresh["manifest"] == run["manifest"]
    assert fresh["task_snapshot"] == run["task_snapshot"]
    running = claim(svc, caller, fresh)
    assert renew(running).status_code == 409  # A consumed grant is never replayed.
    svc.record_coding_iteration("v_one", caller, running["id"], iteration(svc, running))
    close(svc, caller, running, task_verified=True)
    deploy = svc.prepare_product_deploy("v_one", caller, running["id"], "preview")
    assert renew(deploy).status_code == 409
    lapse_review(deploy)
    response = renew(deploy)
    assert response.status_code == 200, response.text
    fresh_deploy = response.json()
    assert fresh_deploy["id"] != deploy["id"] and fresh_deploy["state"] == "pending" and "execution_id" not in fresh_deploy
    assert fresh_deploy["manifest"] == deploy["manifest"]
    assert fresh_deploy["task_snapshot"] == deploy["task_snapshot"]
    lapse_review(fresh_deploy)
    monkeypatch.setattr(api, "verify_caller", lambda *args, **kwargs: replace(caller, device_id="other_device"))
    # Expired reviews remain observable after reconnect; a never-claimed
    # cancellation is hidden. Neither identity can inherit the old approval.
    assert renew(fresh_deploy).status_code == (409 if lapse == "expired" else 404)


@pytest.mark.parametrize("status,http,health,target", [("deployed", 200, 500, "production"),
    ("deployed", None, None, "preview"), ("protected", 401, 401, "production"), ("protected", 200, 200, "preview")])
def test_deploy_receipt_does_not_claim_unobserved_success(product_orders, status, http, health, target):
    svc, _ = product_orders
    with pytest.raises(ValidationError):
        svc.ProductDeployReceipt(execution_id="dex_one", provider="vercel", target=target, status=status,
            url="https://tip-splitter.vercel.app", exit_status=0, http_status=http, health_status=health,
            source_commit="a" * 40, file_manifest_sha256="c" * 64)

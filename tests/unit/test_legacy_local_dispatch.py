from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from coding_agents.protocol import CodingResult


@pytest.mark.parametrize("audience", ["Warehouse managers", "Home gardeners"])
@pytest.mark.asyncio
async def test_local_dispatch_passes_the_exact_build_attempt_key(local_order, audience):
    from app.api import coding_api

    _, _, _, calls, _ = local_order
    request = Request({"type": "http", "method": "POST", "path": "/coding/dispatch", "headers": []})
    order_id = "dwo_" + uuid4().hex
    await coding_api.post_dispatch(coding_api.DispatchBody(venture_id="venture", prompt=f"Build my app for {audience}",
        kind="product", order_id=order_id), request)
    assert calls[-1][3]["order_id"] == order_id


@pytest.fixture(params=[
    ("Inventory reconciliation for wholesalers", "warehouse/reconcile.js"),
    ("Personal trail journals", "journal/entries.js"),
])
def local_order(request, monkeypatch):
    from app.admin.authn import CallerContext
    from app.build.adr import clear_adrs, create_adr, lock_adr
    from app.build.workorder import clear_workorders
    from app.build import device_orders
    from app.builder import ctl

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    clear_adrs()
    clear_workorders()
    idea, artifact = request.param
    caller = CallerContext(sub="owner", email="", role="user", path="", method="POST", exp=0, nonce="")
    monkeypatch.setattr("app.core.local_owner.current_owner", lambda: SimpleNamespace(id=caller.sub, email=""))
    monkeypatch.setattr("app.admin.authn.verify_caller", lambda request: caller)
    monkeypatch.setattr(ctl, "local_tool", lambda *args, **kwargs: "codex")
    monkeypatch.setattr("app.ventures.lifecycle.is_paused", lambda *args: False)
    calls = []
    order = {"id": "dwo_test", "venture_id": "venture", "state": "pending", "review_digest": "digest",
        "approved_plan": {"tasks": [{"files": [artifact]}]},
        "review_path": "/v/venture/device-work-orders/dwo_test"}

    def prepare(venture_id, owner, instruction, **kwargs):
        calls.append((venture_id, owner, instruction, kwargs))
        return dict(order)

    monkeypatch.setattr(device_orders, "prepare_product_build_from_chat", prepare)
    monkeypatch.setattr(device_orders, "prepare_coding_task_from_chat", prepare)
    monkeypatch.setattr(device_orders, "transition", lambda *args: {**order, "state": "approved"})
    monkeypatch.setattr(device_orders, "retrieve", lambda *args: {**order, "state": "approved"})
    adr = create_adr("venture", title=idea, stack=["web"], protected_routes=["/api/private"])
    lock_adr(adr.id)
    yield idea, artifact, caller, calls, order
    clear_adrs()
    clear_workorders()


@pytest.mark.asyncio
async def test_local_work_order_dispatch_preserves_brief_and_observes_target(local_order, monkeypatch):
    from app.build import device_orders
    from app.build.dispatcher import dispatch
    from app.build.workorder import WorkOrder

    idea, artifact, caller, calls, order = local_order
    monkeypatch.setattr(device_orders, "prepare_product_build_from_chat", lambda *args, **kwargs: pytest.fail("A work order must not start an unrelated whole product build"))
    work = WorkOrder("wo_test", "venture", "Build " + idea, ["web"], [])
    result = await dispatch(work, caller=caller, fake_result=CodingResult(ok=True, changed_files=[artifact]))
    assert result.ok is False and result.device_work_order_id == order["id"]
    assert work.status != "done"
    assert idea in calls[0][2] and "/api/private" in calls[0][2]
    assert calls[0][1] is caller
    await dispatch(work, caller=caller)
    assert len(calls) == 1


def test_local_planner_uses_connected_plan_artifacts(local_order):
    from app.build.planner import plan_work_orders

    idea, artifact, caller, calls, order = local_order
    plan = order.pop("approved_plan")
    work = plan_work_orders("venture", caller=caller)[0]
    assert work.id == order["id"] and work.status == "approved"
    assert work.files_expected == []
    assert idea in calls[0][2] and "/api/private" in calls[0][2]
    order["approved_plan"] = plan
    assert plan_work_orders("venture", caller=caller)[0] is work
    assert work.files_expected == [artifact]
    assert len(calls) == 1


def test_local_build_plan_api_routes_authenticated_owner(local_order):
    from app.api.build import plan_build

    _, artifact, caller, calls, order = local_order
    request = Request({"type": "http", "method": "POST", "path": "/ventures/venture/build/plan", "headers": []})
    result = plan_build("venture", request)
    assert calls[0][1] is caller
    assert result["work_orders"][0]["id"] == order["id"]
    assert result["work_orders"][0]["files_expected"] == [artifact]
    assert result["work_orders"][0]["review_path"] == order["review_path"]


@pytest.mark.asyncio
async def test_local_direct_code_dispatch_uses_coding_task_not_product(local_order, monkeypatch):
    from app.api import coding_api
    from app.build import device_orders

    idea, _, caller, calls, order = local_order
    monkeypatch.setattr(device_orders, "prepare_product_build_from_chat", lambda *args, **kwargs: pytest.fail("A fix must not start a whole product build"))
    monkeypatch.setattr(coding_api, "dispatch", lambda **kwargs: pytest.fail("The legacy executor must not run"))
    request = Request({"type": "http", "method": "POST", "path": "/coding/dispatch", "headers": []})
    result = await coding_api.post_dispatch(coding_api.DispatchBody(venture_id="venture", prompt="Fix the export for " + idea), request)
    assert result["id"] == order["id"] and calls[0][1] is caller
    assert "Fix the export for " + idea in calls[0][2]
    assert "argv" not in result and "budget" not in result
    with pytest.raises(HTTPException) as blocked:
        await coding_api.post_dispatch(coding_api.DispatchBody(venture_id="venture", prompt=idea, branch="other"), request)
    assert blocked.value.status_code == 409 and len(calls) == 1


@pytest.mark.parametrize("entry", ["work_order", "planner", "plan_api", "coding_api"])
@pytest.mark.asyncio
async def test_local_missing_tool_names_exact_connection_step(local_order, monkeypatch, entry):
    from app.api import build, coding_api
    from app.build import dispatcher, planner
    from app.build.workorder import WorkOrder
    from app.builder import ctl

    idea, _, caller, calls, _ = local_order
    def unavailable(*args, **kwargs):
        raise HTTPException(409, detail={"code": "build_tool_needs_setup", "message": "Open Connections > Coding tools."})
    monkeypatch.setattr(ctl, "local_tool", unavailable)
    request = Request({"type": "http", "method": "POST", "path": "/coding/dispatch", "headers": []})
    with pytest.raises(HTTPException) as blocked:
        if entry == "work_order":
            await dispatcher.dispatch(WorkOrder("wo", "venture", idea, ["web"], []), caller=caller)
        elif entry == "planner":
            planner.plan_work_orders("venture", caller=caller)
        elif entry == "plan_api":
            build.plan_build("venture", request)
        else:
            await coding_api.post_dispatch(coding_api.DispatchBody(venture_id="venture", prompt=idea), request)
    message = blocked.value.detail["message"]
    assert blocked.value.status_code == 409
    assert "Settings > Keys and connections > Coding tools" in message
    assert message.count(".") == 1 and not calls


@pytest.mark.parametrize("entry", ["work_order", "planner"])
@pytest.mark.asyncio
async def test_local_locked_brief_required_before_any_tool_work(local_order, entry):
    from app.build import dispatcher, planner
    from app.build.adr import clear_adrs
    from app.build.workorder import WorkOrder

    idea, _, caller, calls, _ = local_order
    clear_adrs()
    if entry == "work_order":
        result = await dispatcher.dispatch(WorkOrder("wo", "venture", idea, ["web"], []), caller=caller)
        assert result.ok is False
        message = result.reason
    else:
        with pytest.raises(PermissionError) as blocked:
            planner.plan_work_orders("venture", caller=caller)
        message = str(blocked.value)
    assert "your venture chat" in message and not calls


def test_local_cached_legacy_plan_is_routed_without_losing_requested_scope(local_order):
    from app.build.planner import plan_work_orders
    from app.build.workorder import create_workorder

    idea, artifact, caller, calls, order = local_order
    work = create_workorder("venture", title="Build a private export for " + idea, components=["export"])
    assert plan_work_orders("venture", caller=caller)[0] is work
    assert work.id == order["id"] and work.files_expected == [artifact]
    assert work.title in calls[0][2] and "/api/private" in calls[0][2]


@pytest.mark.asyncio
async def test_local_console_product_dispatch_uses_product_not_current_task(local_order, monkeypatch):
    from app.api import coding_api
    from app.build import device_orders

    idea, _, caller, calls, order = local_order
    monkeypatch.setattr(device_orders, "prepare_coding_task_from_chat", lambda *args, **kwargs: pytest.fail("Your app build must not repair an unrelated current task"))
    monkeypatch.setattr(coding_api, "dispatch", lambda **kwargs: pytest.fail("The legacy executor must not run"))
    request = Request({"type": "http", "method": "POST", "path": "/coding/dispatch", "headers": []})
    prompt = "Build my app for " + idea
    result = await coding_api.post_dispatch(coding_api.DispatchBody(venture_id="venture", prompt=prompt, kind="product"), request)
    assert result["id"] == order["id"] and calls[0][1] is caller
    assert prompt in calls[0][2] and calls[0][3]["provider"] == "codex"

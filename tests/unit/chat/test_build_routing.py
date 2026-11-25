"""A build request opens the approved plan's Work item; it never mints a task."""

import re
import pytest

from tests.unit.chat.test_round3_matrix import venture as _venture


@pytest.fixture
def venture(tmp_path, monkeypatch, request):
    if request.node.name.startswith("test_local_"):
        monkeypatch.setenv("SUPYMEM_MODE", "local")
    from tests.company_os_harness import configure_local_gtm

    for data in _venture.__wrapped__(tmp_path, monkeypatch, request):
        from app.core.local_mode import is_local
        from app.core.local_secrets import update_settings

        monkeypatch.setenv("EMAIL_PROVIDER", "log")
        configure_local_gtm(monkeypatch)
        if is_local():
            update_settings("capabilities", {"background_work": True}, {})
        yield data

ASK = "Build the landing page from the approved plan and let me review it privately before anything is published."


@pytest.fixture(scope="module")
def tool_routing_modules():
    from tests.company_os_harness import load_company_os_app
    load_company_os_app()
    from app.chat import intent, llm_router, product_build_request
    return intent, llm_router, product_build_request


@pytest.mark.parametrize(("venture", "wording", "starts"), [
    ("warehouse", "Write a scraper for the supplier's public stock catalog with Codex", True),
    ("warehouse", "Use Claude Code to add tests for the pallet label parser", True),
    ("warehouse", "Fix the rounding bug in our freight cost calculator", True),
    ("warehouse", "Create a Python script to validate incoming barcode lists", True),
    ("warehouse", "Implement CSV export for the dispatch report using Cursor", True),
    ("warehouse", "Please start my product build.", True),
    ("warehouse", "Should I start my product build?", False),
    ("warehouse", "Should I use Codex to write a scraper for the supplier catalog?", False),
    ("warehouse", "Write a barcode validation script later for the inventory team", False),
    ("warehouse", "Can you explain how to fix the rounding bug in our freight calculator?", False),
    ("warehouse", "Use Cursor to plan a script for packing slip checks, without starting it", False),
    ("warehouse", "Fix the bug in our dispatch report, but don't start yet", False),
    ("gardening", "Build a small tool to track seed swaps with Claude Code", True),
    ("gardening", "Could you write a script that converts my planting calendar to CSV?", True),
    ("gardening", "Use OpenCode to refactor the frost date calculator", True),
    ("gardening", "Add a test for leap years in the watering planner", True),
    ("gardening", "Update calendar.py to accept dates without a leading zero", True),
    ("gardening", "Okay, kick off my app build.", True),
    ("gardening", 'My notes say "kick off my app build"', False),
    ("gardening", "How would I build a small tool to track seed swaps with Claude Code?", False),
    ("gardening", "Write a script to export my planting calendar tomorrow morning", False),
    ("gardening", 'My notes say "Use OpenCode to refactor the frost date calculator"', False),
    ("gardening", "For now, just show me how to add a test for the watering planner", False),
    ("gardening", "Update calendar.py once we've decided which dates to support", False),
])
def test_local_scoped_coding_corpus(tool_routing_modules, monkeypatch, venture, wording, starts):
    intent, llm_router, requests = tool_routing_modules
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    assert requests.is_product_build_request(wording) is starts, venture
    assert (intent.classify_intent(wording) == "build.tool.start") is starts
    proposed = "assistant" if starts else "build.tool.start"
    route = llm_router._validate({"acts": [{"intent": proposed, "text": wording}]}, wording)
    assert (route.acts == [("build.tool.start", wording)]) is starts


@pytest.mark.parametrize("name", ["StockLedger", "TrailNest"])
@pytest.mark.parametrize("wording", ["Build the first version of {name} with Codex",
    "Build the first version of my product with Claude Code", "Make a working prototype of {name} with Codex"])
def test_local_named_product_version_routes_to_tool(tool_routing_modules, monkeypatch, name, wording):
    intent, llm_router, requests = tool_routing_modules
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    text = wording.format(name=name)
    assert requests.local_build_intent(text) == "build.tool.start"
    assert requests.is_product_build_request(text)
    assert intent.classify_intent(text) == "build.tool.start"
    assert llm_router._validate({"acts": [{"intent": "assistant", "text": text}]}, text).acts == [("build.tool.start", text)]


@pytest.mark.parametrize(("venture", "name", "state"), [
    ("Inventory reconciliation for suppliers", "StockLedger", "approved"),
    ("Trail journals for families", "TrailNest", "claimed"),
], indirect=["venture"])
def test_local_build_request_watches_the_existing_order(venture, monkeypatch, name, state):
    import json
    from datetime import timedelta

    from core.clock import utcnow
    from app.admin.authn import CallerContext
    from app.build import device_orders
    from app.builder import ctl
    from app.core.local_owner import ensure_owner
    from app.db.models import VentureDocument
    from app.state import db
    from desktop_runtime import agent_status

    client, venture_id, workspace_id = venture
    owner = ensure_owner()
    caller = device_orders.device_caller(CallerContext(sub=owner.id, email="", role="user", path="", method="GET", exp=0, nonce=""))
    order_id = "dwo_existing"
    now = utcnow()
    order = {"id": order_id, "state": state, "venture_id": venture_id, "workspace_id": workspace_id,
             "actor_id": owner.id, "device_id": caller.device_id, "expires_at": (now + timedelta(hours=1)).isoformat(),
             "manifest": {"task_id": f"product_build_{venture_id}", "duration_seconds": 1440}}
    if state == "claimed":
        order["claimed_at"] = now.isoformat()
    with db()() as session:
        session.add(VentureDocument(id=order_id, venture_id=venture_id, kind=device_orders.KIND, title="Product build",
                                    provenance_json=json.dumps(order), created_at=now, updated_at=now))
        session.commit()
    monkeypatch.setattr(agent_status, "statuses", lambda: [])
    monkeypatch.setattr(ctl, "request_local_build", lambda *args, **kwargs: pytest.fail("The existing build must not be started again"))
    text = f"Build the first version of {name} with Codex"
    chat = f"/ventures/{venture_id}/chat"
    response = client.post(chat, json={"message": text, "idempotency_key": "watch-existing"})
    assert response.status_code == 200, response.text
    reply = response.json()
    assert reply["kind"] == "navigation", reply
    assert reply["actions"] == [{"label": "Watch your tool", "href": f"/v/{venture_id}/device-work-orders/{order_id}", "style": "primary"}]
    assert "recommend" not in reply["body_md"].lower()
    history = client.get(chat).json()["items"]
    assert [turn["body_md"] for turn in history if turn.get("client_request_id") == "watch-existing" and turn["role"] == "user"] == [text]
    assert any(turn["id"] == reply["id"] and turn["actions"] == reply["actions"] for turn in history)


@pytest.mark.parametrize("idea", ["Maintenance scheduling for factories", "Birdwatching journals for families"])
@pytest.mark.parametrize("tool", ["Claude Code", "Codex", "Cursor", "OpenCode", "my coding tool"])
@pytest.mark.parametrize("wording,expected", [
    ("Build my landing page with {tool}", "pagebuilder.build"),
    ("Rebuild my landing page with {tool}", "pagebuilder.build"),
    ("Make a new version of my page using {tool}", "pagebuilder.build"),
    ("Change the headline on my page with {tool}", "pagebuilder.build"),
    ("Change the headline using {tool} on my page", "pagebuilder.build"),
    ("Fix my landing page with {tool}", "pagebuilder.build"),
    ("Use {tool} to rebuild my website", "pagebuilder.build"),
    ("Build my product with {tool}", "build.tool.start"),
    ("Rebuild my app with {tool}", "build.tool.start"),
    ("Rebuild with {tool} my product", "build.tool.start"),
    ("Change the menu in my product with {tool}", "build.tool.start"),
    ("Fix my app with {tool}", "build.tool.start"),
])
def test_local_typed_tool_build_routing(tool_routing_modules, monkeypatch, idea, tool, wording, expected):
    import asyncio
    from types import SimpleNamespace
    from app.chat import router
    intent, llm_router, _ = tool_routing_modules
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    text = wording.format(tool=tool) + " for " + idea
    assert intent.classify_intent(text) == expected
    assert intent.split_speech_acts(text) == [(expected, text)]
    decision = llm_router._validate({"acts": [{"intent": "mac_prepare", "text": text}]}, text)
    assert decision.acts == [(expected, text)]
    calls = []
    async def execute(venture_id, action, **kwargs):
        calls.append((action, kwargs["payload"]))
        return {"action_started": True}
    monkeypatch.setattr(router, "execute_action", execute)
    monkeypatch.setattr(router, "local_capability_reply", lambda name: None)
    reply = asyncio.run(router._prepare_intent("venture", SimpleNamespace(venture={"name": idea}), "mac_prepare", text, None, "owner"))
    assert reply["action_started"] is True and calls[0][0] == expected
    assert calls[0][1] == ({"prepare": True, "query": text} if expected == "pagebuilder.build" else {"text": text})


@pytest.mark.parametrize("idea", ["Maintenance scheduling for factories", "Birdwatching journals for families"])
@pytest.mark.parametrize("constraint", ["Do not ", "Only explain how to ", "When I approve, "])
def test_local_tool_build_constraints_do_not_start(tool_routing_modules, monkeypatch, idea, constraint):
    local_build_intent = tool_routing_modules[2].local_build_intent
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    assert local_build_intent(constraint + "rebuild my landing page with Claude Code for " + idea) is None
    assert local_build_intent("Rebuild my app with Codex for " + idea + ". Answer only.") is None


@pytest.mark.parametrize("goal", ["factory maintenance records", "a family recipe collection"])
def test_utility_build_preserves_the_limit_on_running_it(tool_routing_modules, monkeypatch, goal):
    request = tool_routing_modules[2]
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    build = f"Build a Python CSV parser for {goal}"
    assert request.is_utility_build_request(build + ". Do not run it on my data.")
    assert request.is_utility_build_request(build + ", but do not run it on my data.")
    for limit in ("Do not build it.", "Do not start.", "Do not run Codex.", "Do not run it with Codex.", "Run it every week."):
        assert not request.is_utility_build_request(f"{build}. {limit}")


@pytest.mark.parametrize("idea", ["Maintenance scheduling for factories", "Birdwatching journals for families"])
def test_local_my_coding_tool_requires_choice_only_when_ambiguous(tool_routing_modules, monkeypatch, idea):
    local_product_provider = tool_routing_modules[2].local_product_provider
    from fastapi import HTTPException

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr("app.coding_agents_consent.consents", lambda actor: {"codex": True, "claude": True})
    monkeypatch.setattr("desktop_runtime.agent_status.statuses", lambda: [{"name": name, "installed": True,
        "signed_in": True, "subscription_login": True, "available": True} for name in ("codex", "claude")])
    with pytest.raises(HTTPException) as choice:
        local_product_provider("Build my landing page with my coding tool for " + idea, "owner")
    assert choice.value.detail["message"] == "Which coding tool should I use: Claude Code or Codex?"
    monkeypatch.setattr("app.coding_agents_consent.consents", lambda actor: {"claude": True})
    assert local_product_provider("Rebuild my app with my coding tool for " + idea, "owner") == "claude"


@pytest.mark.parametrize("idea", ["Maintenance scheduling for factories", "Birdwatching journals for families"])
@pytest.mark.parametrize("provider,label", [("claude", "Claude Code"), ("codex", "Codex"), ("cursor", "Cursor"), ("opencode", "OpenCode")])
@pytest.mark.parametrize("missing", ["consent", "login"])
def test_local_named_page_tool_requires_its_own_connection(tool_routing_modules, monkeypatch, idea, provider, label, missing):
    from app.chat import execute
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr("app.coding_agents_consent.consents", lambda actor: {provider: missing != "consent", "other": True})
    monkeypatch.setattr("desktop_runtime.agent_status.statuses", lambda: [{"name": provider, "installed": True,
        "signed_in": missing != "login", "subscription_login": True, "available": True}])
    reply = execute._build_landing_page("venture", {"query": "Rebuild my page with " + label + " for " + idea}, "owner")
    assert reply["action_started"] is False and label in reply["body_md"]
    assert "Settings > Keys and connections > Coding tools" in reply["body_md"]
    assert len(reply["body_md"].splitlines()) == 1
    assert reply["actions"][0]["href"] == "/settings/local#coding-tools"


@pytest.mark.parametrize("idea", ["Maintenance scheduling for factories", "Birdwatching journals for families"])
def test_local_shared_build_request_uses_owner_consent_and_one_capability(venture, monkeypatch, idea):
    from app.admin.authn import CallerContext
    from app.builder import ctl
    from app.build import device_orders
    from app.core import local_capabilities
    from app import coding_agents_consent

    _, venture_id, workspace_id = venture
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    caller = CallerContext(sub="owner", email="", role="user", path="", method="POST", exp=0, nonce="")
    monkeypatch.setattr("app.authz.workspace.assert_venture_access", lambda *args: ("owner", workspace_id))
    monkeypatch.setattr(coding_agents_consent, "consents", lambda actor: {"codex": True, "claude": False})
    monkeypatch.setattr("desktop_runtime.agent_status.statuses", lambda: [{"name": "codex", "installed": True,
        "signed_in": True, "subscription_login": True, "available": True}])
    capabilities = []
    monkeypatch.setattr(local_capabilities, "enabled", lambda name: capabilities.append(name) or True)
    monkeypatch.setattr("app.ventures.lifecycle.is_paused", lambda *args: False)
    calls = []
    def prepared(venture_id, owner, instruction, **kwargs):
        calls.append((venture_id, owner, instruction, kwargs))
        return {"id": "order", "venture_id": venture_id, "workspace_id": workspace_id,
                "state": "pending", "review_digest": "digest"}
    monkeypatch.setattr(device_orders, "prepare_product_build_from_chat", prepared)
    monkeypatch.setattr(device_orders, "prepare_coding_task_from_chat", prepared)
    approvals = []
    monkeypatch.setattr(device_orders, "transition", lambda venture_id, caller, order_id, digest, action:
        approvals.append((order_id, digest, action)) or {"id": order_id, "venture_id": venture_id, "state": "approved"})
    request = "Build " + idea
    order = ctl.request_local_build(venture_id, request, caller=caller)
    assert order["id"] == "order" and request in calls[0][2]
    assert calls[0][1] is caller and calls[0][3]["provider"] == "codex"
    assert capabilities == ["build_with_my_tool"]
    assert approvals == [("order", "digest", "approve")] and order["state"] == "approved"
    ctl.request_local_build(venture_id, "Fix the table for " + idea, caller=caller, repair=True, plan_task_id="exact-task")
    assert "Plan task: exact-task" in calls[1][2] and "Fix the table for " + idea in calls[1][2]
    monkeypatch.setattr(local_capabilities, "enabled", lambda name: False)
    monkeypatch.setattr(local_capabilities, "blocked_message", lambda name: "Open Settings > Your capabilities and switch on Build with my tool.")
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as blocked:
        ctl.request_local_build(venture_id, request, caller=caller)
    assert "Your capabilities" in blocked.value.detail["message"] and len(calls) == 2
    monkeypatch.setattr(local_capabilities, "enabled", lambda name: True)
    monkeypatch.setattr("desktop_runtime.agent_status.statuses", lambda: [{"name": "codex", "installed": True,
        "signed_in": True, "subscription_login": False, "available": True}])
    with pytest.raises(HTTPException):
        ctl.request_local_build(venture_id, request, caller=caller)
    assert len(calls) == 2
    monkeypatch.setattr(coding_agents_consent, "consents", lambda actor: {"codex": False})
    with pytest.raises(HTTPException):
        ctl.request_local_build(venture_id, request, caller=caller)
    assert len(calls) == 2


@pytest.mark.parametrize("idea", ["Maintenance scheduling for factories", "Birdwatching journals for families"])
def test_local_legacy_iteration_job_cannot_replay_an_accepted_request(venture, monkeypatch, idea):
    from app.builder import ctl
    from app.db.supymem_models import FounderRequest
    from app.state import db
    from core.clock import utcnow
    from fastapi import HTTPException

    _, venture_id, workspace_id = venture
    with db()() as session:
        session.add(FounderRequest(id="request", workspace_id=workspace_id, venture_id=venture_id, source="iteration",
            intent="iterate", text="Improve the list for " + idea, classification="in", outcome="pending", created_at=utcnow()))
        session.commit()
    calls = []
    monkeypatch.setattr(ctl, "request_local_build", lambda *args, **kwargs: calls.append(args) or {"id": "order"}, raising=False)
    payload = {"workspace_id": workspace_id, "venture_id": venture_id, "request_id": "request", "reason": "iteration"}
    assert ctl.run_build(payload)["id"] == "order"
    with pytest.raises(HTTPException):
        ctl.run_build(payload)
    assert len(calls) == 1


@pytest.mark.parametrize("idea", ["Maintenance scheduling for factories", "Birdwatching journals for families"])
def test_local_app_build_request_does_not_need_a_hosted_quote(venture, monkeypatch, idea):
    from app.builder import ctl
    from app.build import device_orders
    from app.state import db
    from tests.unit.pagebuilder.test_api import connect_local_tool

    client, venture_id, workspace_id = venture
    connect_local_tool(monkeypatch)
    with db()() as session:
        app_id = ctl._ensure_app(session, workspace_id, venture_id).id
        session.commit()
    calls = []
    monkeypatch.setattr(ctl, "request_local_build", lambda *args, **kwargs: calls.append((args, kwargs)) or
        {"id": "order", "state": "pending", "review_path": "/watch"}, raising=False)
    monkeypatch.setattr(device_orders, "retrieve", lambda *args:
        {"id": "order", "state": "pending", "review_path": "/watch"})
    response = client.post(f"/apps/{app_id}/builds", json={"instruction": "Build " + idea})
    assert response.status_code == 202, response.text
    assert response.json()["device_work_order_id"] == "order" and calls[0][0] == (venture_id, "Build " + idea)
    assert response.json()["build_run_id"] and "job_id" not in response.json()


@pytest.mark.parametrize("idea", ["Maintenance scheduling for factories", "Birdwatching journals for families"])
def test_local_legacy_dispatch_reaches_guarded_orders(venture, monkeypatch, idea):
    from app.api import coding_api
    from app.builder import ctl

    client, venture_id, _ = venture
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    calls = []
    def requested(venture_id, instruction, **kwargs):
        calls.append((venture_id, instruction, kwargs))
        return {"id": "order", "venture_id": venture_id, "state": "pending", "review_path": "/watch"}
    monkeypatch.setattr(ctl, "request_local_build", requested, raising=False)
    monkeypatch.setattr(coding_api, "dispatch", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("Unsafe legacy dispatcher")))
    response = client.post("/coding/dispatch", json={"venture_id": venture_id, "prompt": "Build " + idea})
    assert response.status_code == 200, response.text
    assert response.json()["id"] == "order"
    assert calls[0][:2] == (venture_id, "Build " + idea)
    assert calls[0][2]["caller"].role == "user"
    assert "argv" not in response.json()
    selected = client.post("/coding/dispatch", json={"venture_id": venture_id, "prompt": "Build " + idea, "prefs": ["claude"]})
    assert selected.status_code == 200 and calls[1][2]["provider"] == "claude"
    for options in ({"kind": "analysis"}, {"mode": "interactive"}):
        blocked = client.post("/coding/dispatch", json={"venture_id": venture_id, "prompt": idea, **options})
        assert blocked.status_code == 409 and "venture chat" in blocked.json()["detail"]["message"]
    assert len(calls) == 2


@pytest.mark.parametrize("idea", ["Maintenance scheduling for factories", "Birdwatching journals for families"])
def test_local_requested_work_handoff_never_marks_planned_build_done(venture, monkeypatch, idea):
    from app.builder import ctl
    from app.planning import execute, store

    _, venture_id, _ = venture
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    task = store.enqueue_task(venture_id, {"title": "Build " + idea, "detail": "Create the core workflow",
        "tags": ["capability:build.run", "plan_task:exact-task"], "runnable": False}, source="approved_plan:current")
    calls = []
    monkeypatch.setattr(ctl, "request_local_build", lambda *args, **kwargs: calls.append((args, kwargs)) or
        {"id": "order", "state": "pending", "review_path": "/watch"}, raising=False)
    result = execute.start_planned_task(venture_id, task["id"])
    assert result["device_work_order_id"] == "order" and result["status"] == "queued"
    assert calls[0][1]["plan_task_id"] == "exact-task"
    assert store.get_task(venture_id, task["id"])["status"] != "done"
    assert "job_id" not in result


@pytest.mark.parametrize("idea", ["Maintenance scheduling for factories", "Birdwatching journals for families"])
def test_local_iteration_and_requested_debug_share_guarded_executor(venture, monkeypatch, idea):
    from app.builder import ctl
    from app.debug import service as debug
    from app.iterate import service as iterate
    from app.state import db

    _, venture_id, workspace_id = venture
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    with db()() as session:
        app_id = ctl._ensure_app(session, workspace_id, venture_id).id
        session.commit()
    calls = []
    monkeypatch.setattr(ctl, "request_local_build", lambda *args, **kwargs: calls.append((args, kwargs)) or
        {"id": "order", "state": "pending", "review_path": "/watch"}, raising=False)
    change = "Improve the list layout for " + idea
    iteration = iterate.request_iteration(app_id, change)
    assert iteration["device_work_order_id"] == "order" and "job_id" not in iteration
    assert calls[0][0] == (venture_id, change) and calls[0][1]["repair"] is True
    incident = debug.open_incident(app_id, workspace_id, summary="The list crashes for " + idea)
    assert len(calls) == 1
    result = debug.start_debug(incident["id"])
    assert result["device_work_order_id"] == "order"
    assert calls[1][1]["repair"] is True and "crashes" in calls[1][0][1]


@pytest.mark.parametrize("idea", ["Maintenance scheduling for factories", "Birdwatching journals for families"])
def test_local_iteration_refuses_credentials_before_persisting_a_request(venture, monkeypatch, idea):
    from app.builder import ctl
    from app.db.supymem_models import FounderRequest
    from app.iterate import service
    from app.state import db
    from fastapi import HTTPException
    from sqlalchemy import func, select

    _, venture_id, workspace_id = venture
    with db()() as session:
        app_id = ctl._ensure_app(session, workspace_id, venture_id).id
        session.commit()
        before = session.scalar(select(func.count()).select_from(FounderRequest))
    with pytest.raises(HTTPException) as refused:
        service.request_iteration(app_id, "Improve " + idea + " using " + "sk-proj-" + "synthetic-credential")
    assert refused.value.status_code == 409 and "Keys and connections" in refused.value.detail["message"]
    with db()() as session:
        assert session.scalar(select(func.count()).select_from(FounderRequest)) == before


@pytest.mark.parametrize("idea", ["Maintenance scheduling for factories", "Birdwatching journals for families"])
def test_local_debug_credentials_are_refused_before_logs_or_incidents_are_saved(venture, monkeypatch, idea):
    from app.builder import ctl
    from app.config import data_root
    from app.db.supymem_models import AppIncident
    from app.debug import service
    from app.state import db
    from fastapi import HTTPException
    from sqlalchemy import func, select

    _, venture_id, workspace_id = venture
    with db()() as session:
        app_id = ctl._ensure_app(session, workspace_id, venture_id).id
        session.commit()
        before = session.scalar(select(func.count()).select_from(AppIncident))
    text = "Error in " + idea + " using " + "sk-proj-" + "synthetic-credential"
    with pytest.raises(HTTPException):
        service.append_log(app_id, text)
    with pytest.raises(HTTPException):
        service.open_incident(app_id, workspace_id, summary=text)
    assert not (data_root() / "apps" / app_id / "logs" / "app.log").exists()
    with db()() as session:
        assert session.scalar(select(func.count()).select_from(AppIncident)) == before


def test_managed_work_preserves_the_exact_plan_task(venture, monkeypatch):
    from app.chat import router
    from app.chat.execute import _managed_work_reply

    calls = []
    def product(venture_id, brief, actor, **kwargs):
        calls.append(kwargs)
        return {"kind": "confirmation", "body_md": "Review exact task", "actions": []}
    monkeypatch.setattr(router, "_product_build_reply", product)
    _managed_work_reply(venture[1], "Build", "ptk_exact", None)
    assert calls == [{"task_id": "ptk_exact"}]


def _count(venture_id):
    from app.db.models import PlannedTask
    from app.state import db

    with db()() as session:
        return session.query(PlannedTask).filter_by(venture_id=venture_id).count()


@pytest.mark.parametrize("task_title", ["Landing page for clinical lab schedulers", "Landing page for weekend hikers"])
def test_local_build_request_uses_page_tool_not_legacy_planned_item(venture, monkeypatch, task_title):
    client, vid, _ = venture
    from app.pagebuilder import api
    from app.planning.store import enqueue_task
    from tests.unit.pagebuilder.test_api import connect_local_tool

    connect_local_tool(monkeypatch)

    enqueue_task(vid, {"title": task_title, "runnable": True, "tags": ["phase"]}, source="approved_plan:pln_1")
    started = []
    monkeypatch.setattr("app.planning.execute.start_planned_task", lambda *_: pytest.fail("started the legacy landing task"))
    monkeypatch.setattr(api, "estimate", lambda *_: {"ready": True})
    monkeypatch.setattr(api, "profile", lambda *_: {"current": None})
    monkeypatch.setattr(api, "_start", lambda venture_id, actor, **options:
                        started.append((venture_id, options["instruction"])) or {"job_id": "page-job", "state": "queued"})
    monkeypatch.setattr("app.autopilot.receipts.queued_job", lambda _: None)
    before = _count(vid)
    reply = client.post(f"/ventures/{vid}/chat", json={"message": ASK}).json()
    assert reply["kind"] == "text" and not any(action.get("confirmation") for action in reply["actions"])
    assert started == [(vid, ASK)] and "connected coding tool" in reply["body_md"]
    assert "Nothing is published" in reply["body_md"]
    assert "credit" not in reply["body_md"].lower()
    assert _count(vid) == before


@pytest.mark.hosted_only
def test_managed_landing_build_never_becomes_a_second_work_card(venture, monkeypatch):
    """A managed build goes to the one build card path, not a work.start card that asks again."""
    client, vid, _ = venture
    from app.chat import router
    from app.planning.store import enqueue_task

    quoted_tasks = []
    original_product_build = router._product_build_reply

    def quote_product(*args, **kwargs):
        quoted_tasks.append(kwargs.get("task_id"))
        return original_product_build(*args, **kwargs)

    monkeypatch.setattr(router, "_product_build_reply", quote_product)
    enqueue_task(vid, {"title": "Landing page build and private review", "runnable": True,
                            "tags": ["capability:build.run", "plan_task:ptk_1"]}, source="approved_plan:pln_1")
    before = _count(vid)
    reply = client.post(f"/ventures/{vid}/chat", json={"message": ASK}).json()
    assert not any(a.get("confirmation", {}).get("action") == "work.start" for a in reply["actions"])
    from app.core.local_mode import is_local
    if is_local():
        assert not any(action.get("confirmation") for action in reply["actions"])
        assert "credit" not in reply["body_md"].lower()
    else:
        assert "approve its exact budget" in reply["body_md"]
    assert quoted_tasks == ["ptk_1"]
    assert _count(vid) == before


@pytest.mark.parametrize("venture", ["Inventory alerts for workshops", "Weekend camping for families"], indirect=True)
def test_landing_request_without_plan_builds_preview_without_generic_task(venture, monkeypatch):
    client, vid, _ = venture
    from app.chat import execute
    from tests.unit.pagebuilder.test_api import connect_local_tool

    connect_local_tool(monkeypatch)

    started = []
    def build(venture_id, payload, actor):
        started.append(venture_id)
        return {"kind": "site_preview", "body_md": "Your requested preview is saved privately.", "actions": [], "citations": []}
    monkeypatch.setattr(execute, "_build_landing_page", build)
    reply = client.post(f"/ventures/{vid}/chat", json={"message": ASK}).json()
    from app.core.local_mode import is_local
    if is_local():
        assert reply["kind"] == "site_preview" and "saved privately" in reply["body_md"]
        assert started == [vid]
        assert not any(action.get("confirmation") for action in reply["actions"])
    else:
        assert reply["kind"] == "navigation"
        assert started == []
        assert "Nothing was started" in reply["body_md"]
    assert _count(vid) == 0


@pytest.mark.parametrize("venture", ["Inventory alerts for workshops", "Weekend camping for families"], indirect=True)
@pytest.mark.parametrize("intent", ["start_product_build", "pagebuilder.build"])
def test_page_build_advice_offers_the_builder_without_starting_it(venture, monkeypatch, intent):
    import asyncio
    from app.chat import execute
    from app.chat.context import ChatContext
    from app.chat.router import _dispatch_intent

    _, venture_id, _ = venture
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    from tests.company_os_harness import configure_local_gtm

    configure_local_gtm(monkeypatch)
    monkeypatch.setattr(execute, "_build_landing_page", lambda *args: pytest.fail("Advice must not build a page"))
    reply = asyncio.run(_dispatch_intent(venture_id, ChatContext(venture={"id": venture_id}),
        intent, "Would it make sense to build a landing page before interviews?", None))
    assert not reply.get("action_started")
    assert any(action.get("action") == "pagebuilder.build" for action in reply["actions"])
    assert not any(action.get("confirmation") for action in reply["actions"])
    assert _count(venture_id) == 0


def test_chat_store_scrubs_loopback_urls(venture):
    from app.chat.store import append_message

    _, vid, _ = venture
    saved = append_message(vid, role="assistant", body_md="Live at `http://127.0.0.1:3300/w/wsp_x/site/` ok")
    assert "127.0.0.1" not in saved["body_md"]


@pytest.mark.hosted_only
def test_completion_message_has_no_ids_paths_or_localhost(venture, monkeypatch):
    import asyncio

    _, vid, _ = venture
    from app.chat.store import list_messages
    from app.planning.execute import execute_planned_task
    from app.planning.store import enqueue_task

    async def ship(*_a, **_k):
        return True, "\n\nServed first-look HTML is now version 1."

    monkeypatch.setattr("app.planning.execute._ship_first_look", ship)
    task = enqueue_task(vid, {"title": "Write the value proposition", "detail": "Put the offer on the served page.",
                              "tags": ["phase"], "runnable": True})
    asyncio.run(execute_planned_task(vid, task["id"]))
    done = [m for m in list_messages(vid) if "Finished" in m["body_md"]][-1]
    assert not re.search(r"tsk_|/repo/|\.md|127\.0\.0\.1|localhost", done["body_md"])
    assert [a.get("href") or a.get("action") for a in done["actions"]] == ["site.preview", "status"]  # chat-first: website and work are shown in chat


@pytest.mark.parametrize("venture", ["Equipment inspections for factories", "Bedtime stories for families"], indirect=True)
def test_local_page_work_hands_off_without_claiming_completion(venture, monkeypatch):
    import asyncio

    from app.builder import ctl
    from app.chat.store import list_messages
    from app.planning.execute import execute_planned_task
    from app.planning.store import enqueue_task, get_task

    _, venture_id, _ = venture
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    calls = []
    monkeypatch.setattr(ctl, "request_local_build", lambda *args, **kwargs: calls.append((args, kwargs)) or
        {"id": "order", "state": "approved", "review_path": "/watch"})
    monkeypatch.setattr("app.planning.execute._ship_first_look", lambda *args:
        pytest.fail("Your connected coding tool must build the page"))
    task = enqueue_task(venture_id, {"title": "Write the value proposition", "detail": "Put the offer on the page.",
        "tags": ["phase"], "runnable": True})
    result = asyncio.run(execute_planned_task(venture_id, task["id"]))
    assert result["status"] == "queued" and result["device_work_order_id"] == "order"
    assert len(calls) == 1 and task["id"] in calls[0][0][1]
    assert get_task(venture_id, task["id"])["status"] != "done"
    assert not any("Finished" in message["body_md"] for message in list_messages(venture_id))

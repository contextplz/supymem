import pytest
from tests.unit.gtm.test_journey import journey_db as journey_db

from tests.company_os_harness import fake_signed_in_codex, load_company_os_app


@pytest.fixture(scope="module")
def matcher():
    load_company_os_app()
    from app.chat.product_build_request import is_product_build_request
    return is_product_build_request


@pytest.mark.parametrize("audience", ["warehouse managers", "home cooks"])
def test_local_build_retry_uses_the_exact_persisted_order_after_it_closes(journey_db, monkeypatch, audience):
    from uuid import uuid4

    from app.admin.authn import CallerContext
    from app.build import device_orders

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(device_orders, "assert_coding_loop_available", lambda *args: None)
    owner = CallerContext(journey_db["actor"], "", "user", "", "POST", 0, "", device_id="dev_retry")
    venture = journey_db["vid"]
    order_id = "dwo_" + uuid4().hex
    prompt = f"Build a CSV utility for {audience} with Codex."
    prepare = device_orders.prepare_product_build_from_chat
    order = prepare(venture, owner, prompt, provider="codex", order_id=order_id)
    assert order["id"] == order_id and order["state"] == "approved"
    device_orders.transition(venture, owner, order_id, order["review_digest"], "cancel")
    assert prepare(venture, owner, prompt, provider="codex", order_id=order_id)["state"] == "cancelled"
    with pytest.raises(ValueError, match="device_coding_loop_scope_invalid"):
        prepare(venture, owner, prompt + " Add a chart.", provider="codex", order_id=order_id)
    assert [row["id"] for row in device_orders.list_orders(venture, owner)] == [order_id]


def test_local_chat_opens_the_build_whose_stop_is_unconfirmed(tmp_path, monkeypatch):
    import json
    from uuid import uuid4

    from tests.unit.product.test_computer_use_jobs import _client

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    client = _client(tmp_path, monkeypatch)
    from app.admin.authn import CallerContext
    from app.build import device_orders, device_stop
    from app.db.models import Job
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.pagebuilder import api as pagebuilder
    from app.state import db
    from app.workspaces.resolve import LOCAL_PERSON_ID
    from core.clock import utcnow
    from desktop_runtime.agent_status import _status
    fake_signed_in_codex(tmp_path, monkeypatch)
    monkeypatch.setattr(pagebuilder, "_candidate_current", lambda _saved: True)
    client.headers["X-Subject"] = LOCAL_PERSON_ID
    consent = client.post("/ui-config/coding-agents/consent", json={"provider": "codex", "allow": True,
        "candidate": _status("codex")["candidate"]})
    assert consent.status_code == 200, consent.text
    caller = CallerContext(LOCAL_PERSON_ID, "", "user", "", "POST", 0, "")
    ventures = []
    for name, idea in [("Stockroom", "Inventory checklist for warehouse operators"),
                       ("Trail Notes", "Hike journal for weekend walkers")]:
        created = client.post("/ventures", json={"raw_idea": idea, "name": name, "start": False})
        assert created.status_code == 201, created.text
        venture = created.json()
        venture_id, workspace_id = venture["id"], venture["workspace_id"]
        ventures.append((venture_id, name))
        now = utcnow()
        idea_id, revision_id = f"idea_{venture_id}", f"revision_{venture_id}"
        with db()() as session:
            session.add(IdeaSession(id=idea_id, workspace_id=workspace_id, venture_id=venture_id,
                case="know", state="confirmed", current_revision_id=revision_id, confirmed_revision_id=revision_id,
                created_at=now, updated_at=now))
            session.flush()
            session.add(IdeaRevision(id=revision_id, session_id=idea_id, workspace_id=workspace_id,
                venture_id=venture_id, rev=1, source="founder", summary=idea, structured=json.dumps({"idea": idea}),
                created_at=now))
            session.add(Job(id=f"page_{venture_id}", kind=pagebuilder.KIND, workspace_id=workspace_id,
                venture_id=venture_id, status="succeeded", created_at=now, payload_json=json.dumps({
                    "variant_count": 1, "result": {"bundles": [{"manifest": {}}]}, "_runtime": {"state": "ready",
                        "stage": "ready", "progress": 100, "operations": {}, "calls_used": 0,
                        "quote": {"ceiling_micro": 0, "payer": "local"}}})))
            session.commit()

    def chat(venture_id, message):
        response = client.post(f"/ventures/{venture_id}/chat", json={"message": message, "idempotency_key": uuid4().hex})
        assert response.status_code == 200, response.text
        return response.json()

    first_id, first_name = ventures[0]
    second_id, second_name = ventures[1]
    first_request = f"Build the first version of {first_name} with Codex"
    second_request = f"Build the first version of {second_name} with Codex"
    started = chat(first_id, first_request)
    assert started["kind"] == "build_tool_started", started
    order_id = started["actions"][0]["payload"]["order_id"]
    existing = device_orders.retrieve(first_id, caller, order_id)
    claimed = device_orders.transition(first_id, caller, order_id, existing["review_digest"], "claim")
    device_orders.transition(first_id, caller, order_id, claimed["review_digest"], "cancel")
    receipt_caller = device_orders.device_caller(caller)
    stop = {"execution_id": claimed["execution_id"], "evidence_digest": "e" * 64}
    assert device_stop.record(first_id, receipt_caller, order_id, processes_stopped=False, **stop)["stop_reconciliation_required"]

    for venture_id, message in [(first_id, first_request),
                                (first_id, f"Build a working prototype of {first_name} with Codex"),
                                (second_id, second_request)]:
        reply = chat(venture_id, message)
        assert reply["kind"] == "navigation", reply["body_md"]
        assert "Check Stop" in reply["body_md"]
        assert reply["actions"] == [{"label": "Open existing build",
            "href": f"/v/{first_id}/device-work-orders/{order_id}", "style": "primary"}]
        orders = client.get(f"/ventures/{venture_id}/device-work-orders").json()["orders"]
        assert [order["id"] for order in orders] == ([order_id] if venture_id == first_id else [])

    assert device_stop.record(first_id, receipt_caller, order_id, processes_stopped=True, **stop)["state"] == "stopped"
    started = chat(second_id, second_request)
    assert started["kind"] == "build_tool_started", started
    assert started["actions"][0]["payload"]["order_id"] != order_id


@pytest.mark.parametrize("text", ["Build my app with Codex", "Use Claude Code to implement our product",
    "Can you build the app with Codex?", "Please develop a full-stack app with Claude Code",
    "I want you to build my app with Codex", "I'd like to use Claude Code to build our app",
    "Help me build my app with Codex", "Let's build our product using Claude Code"])
def test_explicit_provider_product_request_is_recognized(matcher, text):
    assert matcher(text)


@pytest.mark.parametrize("audience", ["warehouse inspectors", "weekend cyclists"])
@pytest.mark.parametrize("request_text,requested", [("Build this for {}", True),
    ("Implement the plan for {}", True), ("What could you build for {}?", False),
    ('"Build this for {}"', False), ("Do not build this for {}", False),
    ("Build this for {} but do not start it", False),
    ("Build this for {} automatically every day", False)])
@pytest.mark.parametrize("local", [True, False])
def test_provider_free_requests_require_local_mode_and_explicit_consent(matcher, monkeypatch, audience, request_text, requested, local):
    monkeypatch.setenv("SUPYMEM_MODE", "local" if local else "hosted")
    assert matcher(request_text.format(audience)) is (local and requested)


@pytest.mark.parametrize("text", ["Don't build my app with Codex", "What can Codex build?",
    'Explain "build my app with Codex"', "Build my app with Codex but do not start it",
    "Build my app with Codex automatically every day", "Use Codex to inspect the product read-only",
    "Use my Mac to click Build in the Codex app", "Build a shortcut for the Codex app",
    "Use Codex to build the authenticated application. Repair its existing start script only; do not rerun the managed builder.",
    "Use Claude Code to build the app, but only fix its existing task",
    "Build my landing page with Codex"])
def test_questions_other_profiles_negations_and_autopilot_do_not_prepare(matcher, text):
    assert not matcher(text)


@pytest.mark.parametrize("audience", ["warehouse inspectors", "weekend cyclists"])
def test_repair_only_scope_does_not_start_a_fresh_local_build(monkeypatch, matcher, audience):
    from app.chat.product_build_request import local_build_intent

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    instruction = f"Use Codex to build the app for {audience}, but only repair the existing task."
    assert local_build_intent(instruction) is None
    assert not matcher(instruction)


def test_hosted_ai_capability_prompt_keeps_its_existing_gate(monkeypatch, matcher):
    from app.org.manager import capability_block
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    monkeypatch.delenv("SUPYMEM_CODING_LOOP_ENABLED", raising=False)
    text = capability_block()
    assert "build my app with Codex" in text and "pilot is currently disabled" in text
    assert "required permission and eligibility checks" in text and "no database" in text
    assert "Never ask you to reconfirm requested work" in text
    monkeypatch.setenv("SUPYMEM_CODING_LOOP_ENABLED", "1")
    assert "only allowlisted founders" in capability_block()


@pytest.mark.parametrize("audience", ["Dispatch managers", "Home cooks"])
@pytest.mark.parametrize("instruction", ["Build this", "Build this now", "Build my idea", "Build the app for {audience}", "Implement the plan",
    "Please build our product using Codex", "Can you build the app with Claude Code?",
    "Build an app for {audience} that shows alerts when supplies are late",
    "Build the app for {audience} with a landing page", "Create an app from the plan for {audience}",
    "Build the first working version for {audience} using my own coding tool on this laptop",
    "Build the app for {audience} with Cursor", "Use OpenCode to build the app for {audience}"])
def test_local_explicit_build_uses_deterministic_tool_route(monkeypatch, matcher, audience, instruction):
    from app.chat.intent import classify_intent, split_speech_acts

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    text = instruction.format(audience=audience)
    assert matcher(text)
    assert classify_intent(text) == "build.tool.start"
    assert split_speech_acts(text) == [("build.tool.start", text)]


@pytest.mark.parametrize("audience", ["Dispatch managers", "Home cooks"])
@pytest.mark.parametrize("instruction", ['"Build the app for {audience}"', 'Explain "build the app with Codex"',
    "What would it take to build the app for {audience}?", "Could Codex build the app for {audience}?",
    "Build this but don't execute it", "Build the app, but only explain how", "Build this when I approve",
    "Build the app for {audience} automatically", "Don't build this", "Build this later",
    "Build this, then start another build without asking", "Build this is what my customer said",
    "Build the app, but wait for my approval", "Build a landing page with Codex",
    "Build a shortcut for the Codex app", "Build an onboarding guide for the app for {audience}",
    "Create the app for {audience}, but do not create it", "Make a product for {audience} later",
    'Explain "write the code for the product for {audience}"',
    "Make a plan for {audience}", "Create a product plan for {audience}",
    "Develop a plan for {audience}", "Build a plan for {audience}",
    "Create this plan for {audience}", "Make it shorter for {audience}",
    "Build this onboarding guide for {audience}", "Develop this interview script for {audience}", "Create an idea for {audience}"])
def test_local_non_authorizing_build_never_routes_to_execution(monkeypatch, matcher, audience, instruction):
    from app.chat.intent import classify_intent, split_speech_acts

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    text = instruction.format(audience=audience)
    assert not matcher(text)
    assert classify_intent(text) != "build.tool.start"
    assert all(intent != "build.tool.start" for intent, _text in split_speech_acts(text))


@pytest.mark.parametrize("audience", ["Dispatch managers", "Home cooks"])
@pytest.mark.parametrize("ready", [True, False])
@pytest.mark.parametrize("instruction", ["Build the app for {audience}", "Can you build a website monitoring app for {audience}?"])
def test_local_build_dispatches_once_without_requested_work_card(journey_db, monkeypatch, audience, ready, instruction):
    import asyncio
    from types import SimpleNamespace
    from app.chat import execute as chat_execute, router
    from app.core import local_capabilities

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    next_step = "Open Settings > Keys and connections > Coding tools, connect Codex, then retry your build."
    checked = []
    def snapshot(name):
        checked.append(name)
        return {"state": "ready" if ready else "needs_setup", "next_step": next_step}

    monkeypatch.setattr(local_capabilities, "capability", snapshot)
    monkeypatch.setattr("app.chat.guard.guard_reply", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.chat.other_venture.other_venture_reply", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.build.device_orders.coding_loop_enabled", lambda *_args: pytest.fail("No hosted rollout"))
    calls = []
    def start(venture_id, payload, actor):
        calls.append((venture_id, payload, actor))
        return {"kind": "text", "body_md": "Your build is queued. Open Build activity to watch it.",
            "action_started": True, "actions": [], "citations": []}
    monkeypatch.setattr(chat_execute, "_active_product_build_reply", lambda *_args: None)
    monkeypatch.setattr(chat_execute, "_start_tool_build", start)
    text = instruction.format(audience=audience)
    context = SimpleNamespace(venture={"name": audience, "workspace_id": journey_db["wid"]})
    reply = asyncio.run(router._dispatch_intent(journey_db["vid"], context, "start_product_build",
        text, None, actor_subject=journey_db["actor"]))
    assert checked == ["build_with_my_tool"]
    assert len(calls) == int(ready)
    if ready:
        assert calls == [(journey_db["vid"], {"text": text}, journey_db["actor"])]
        assert reply["action_started"] is True
    else:
        assert reply["body_md"] == next_step and reply["action_started"] is False
    assert reply["kind"] != "confirmation" and "credit" not in str(reply).lower()


@pytest.mark.parametrize("audience", ["Dispatch managers", "Home cooks"])
@pytest.mark.asyncio
async def test_stalled_coding_probe_does_not_block_another_chat_request(journey_db, monkeypatch, audience):
    import asyncio
    from threading import Event
    from types import SimpleNamespace

    from app.chat import execute, router
    from desktop_runtime import agent_status

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(execute, "_active_product_build_reply", lambda *_: None)
    loop = asyncio.get_running_loop()
    probing = asyncio.Event()
    release = Event()
    attempts = []

    def stalled_probe():
        attempts.append(None)
        loop.call_soon_threadsafe(probing.set)
        assert release.wait(timeout=5), "The second request could not run while the coding tool was checked"
        return []

    monkeypatch.setattr(agent_status, "statuses", stalled_probe)
    context = SimpleNamespace(venture={"name": audience, "workspace_id": journey_db["wid"]})
    first = asyncio.create_task(router._dispatch_intent(journey_db["vid"], context, "build.tool.start",
        f"Build the app for {audience}", None, actor_subject=journey_db["actor"]))
    try:
        await asyncio.wait_for(probing.wait(), timeout=4)
        second = await asyncio.wait_for(router._dispatch_intent(journey_db["vid"], context, "venture.rename",
            "Rename this venture", None, actor_subject=journey_db["actor"]), timeout=4)
        assert "say" in second["body_md"] and not first.done()
    finally:
        release.set()
        reply = await first
    assert len(attempts) == 1
    assert reply["action_started"] is False and "/settings/local#coding-tools" == reply["actions"][0]["href"]


@pytest.mark.parametrize("audience", ["Dispatch managers", "Home cooks"])
def test_local_product_request_cannot_become_page_publication(journey_db, monkeypatch, audience):
    import asyncio
    from app.chat import router

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr("app.chat.idea_bridge.pending_question", lambda *_args, **_kwargs: None)
    intents = []
    async def dispatch(_venture_id, _context, intent, _text, _speaker, **_kwargs):
        intents.append(intent)
        return {"kind": "build_tool_started", "body_md": "Open Build activity to watch your local build. Nothing is published.",
            "actions": [], "citations": []}
    monkeypatch.setattr(router, "_dispatch_intent", dispatch)
    reply = asyncio.run(router.handle_message(journey_db["vid"],
        f"Build the app for {audience} with a landing page and publish it", actor_subject=journey_db["actor"]))
    assert intents == ["build.tool.start"]
    assert reply["kind"] == "build_tool_started"


@pytest.mark.parametrize("audience", ["Dispatch managers", "Home cooks"])
def test_local_tool_build_cannot_start_in_a_paused_venture(journey_db, monkeypatch, audience):
    import asyncio
    import sys
    from types import SimpleNamespace
    from app.chat import execute

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setitem(sys.modules, "app.core.local_capabilities", SimpleNamespace(
        enabled=lambda name: name == "build_with_my_tool", capability=lambda name: {"state": "ready"},
        blocked_message=lambda name: "Open Settings > Keys and connections > Coding tools."))
    monkeypatch.setattr("app.ventures.lifecycle.is_paused", lambda venture_id: True)
    monkeypatch.setattr(execute, "_start_tool_build", lambda *_args: pytest.fail("Paused venture cannot start work"))
    reply = asyncio.run(execute.execute_action(journey_db["vid"], "build.tool.start",
        payload={"text": f"Build the app for {audience}"}, actor_subject=journey_db["actor"]))
    assert "resume" in reply["body_md"].lower() and reply.get("action_started") is not True


@pytest.mark.parametrize("audience", ["Dispatch managers", "Home cooks"])
def test_local_tool_action_reuses_authoritative_order_and_never_restarts(journey_db, monkeypatch, audience):
    import asyncio
    import sys
    from types import SimpleNamespace
    from app.chat import execute, product_build_request
    from app.build import device_orders

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setitem(sys.modules, "app.core.local_capabilities", SimpleNamespace(
        enabled=lambda name: name == "build_with_my_tool", capability=lambda name: {"state": "ready"},
        blocked_message=lambda name: "Open Settings > Keys and connections > Coding tools."))
    monkeypatch.setattr("app.core.local_owner.current_owner", lambda: SimpleNamespace(id=journey_db["actor"]))
    monkeypatch.setattr(product_build_request, "local_product_provider", lambda *_args: "codex", raising=False)
    order = {"id": "dwo_requested", "venture_id": journey_db["vid"], "state": "pending", "review_digest": "saved"}
    prepared, approved = [], []
    def prepare(venture_id, instruction, *, caller, provider=None, plan_task_id=None, repair=False):
        prepared.append((venture_id, caller.sub, instruction, provider))
        assert plan_task_id is None and repair is False
        return dict(order)
    def approve(venture_id, caller, order_id, digest, action):
        approved.append((venture_id, caller.sub, order_id, digest, action))
        order["state"] = "approved"
        return dict(order)
    monkeypatch.setattr("app.builder.ctl.request_local_build", prepare)
    monkeypatch.setattr(device_orders, "transition", approve)
    text = f"Build the app for {audience}"
    for _attempt in range(2):
        reply = asyncio.run(execute.execute_action(journey_db["vid"], "build.tool.start",
            payload={"text": text}, actor_subject=journey_db["actor"]))
        assert reply["action_started"] is True and reply["actions"][0]["payload"]["order_id"] == order["id"]
        assert reply["body_md"].startswith("Your build uses your connected coding tool")
        assert reply["kind"] != "confirmation" and "credit" not in str(reply).lower()
        assert reply["actions"] == [{"label": "Watch your build", "href":
            f'/v/{journey_db["vid"]}/device-work-orders/{order["id"]}',
            "payload": {"order_id": order["id"]}, "style": "primary"}]
    assert prepared == [(journey_db["vid"], journey_db["actor"], text, "codex")] * 2
    assert approved == [(journey_db["vid"], journey_db["actor"], order["id"], "saved", "approve")]
    from app.chat.store import append_message
    saved = append_message(journey_db["vid"], role="assistant", body_md=reply["body_md"],
        kind=reply["kind"], actions=reply["actions"])
    assert saved["kind"] == "build_tool_started" and saved["actions"] == reply["actions"]


@pytest.mark.parametrize("audience", ["Dispatch managers", "Home cooks"])
def test_local_tool_choices_only_offer_ready_consented_tools(monkeypatch, matcher, audience):
    from fastapi import HTTPException

    from app.chat.product_build_request import local_product_provider

    allowed = {"codex": True, "claude": True, "cursor": False, "opencode": True}
    ready = {"codex", "cursor"}
    monkeypatch.setattr("app.coding_agents_consent.consents", lambda _: allowed)
    monkeypatch.setattr("desktop_runtime.agent_status.statuses", lambda: [{"name": name, "installed": True,
        "signed_in": name in ready, "available": name in ready, "subscription_login": name in ready}
        for name in allowed])
    request = f"Build the app for {audience} with my connected coding tool"
    assert local_product_provider(request, "owner") == "codex"
    ready.add("claude")
    with pytest.raises(HTTPException) as choice:
        local_product_provider(request, "owner")
    assert "Claude Code or Codex" in choice.value.detail["message"]
    ready.add("opencode")
    assert local_product_provider(f"Build the app for {audience} with OpenCode", "owner") == "opencode"


@pytest.mark.parametrize("audience", ["Dispatch managers", "Home cooks"])
def test_local_tool_selection_requires_consent_and_subscription(monkeypatch, matcher, audience):
    from fastapi import HTTPException
    from app.chat.product_build_request import local_product_provider
    from desktop_runtime import agent_status

    monkeypatch.setattr("app.coding_agents_consent.consents", lambda subject: {"codex": True, "claude": True})
    status = [{"name": "codex", "installed": True, "signed_in": True, "available": False, "subscription_login": False},
        {"name": "claude", "installed": True, "signed_in": True, "available": True, "subscription_login": True}]
    monkeypatch.setattr(agent_status, "statuses", lambda: status)
    assert local_product_provider(f"Build the app for {audience}", "owner") == "claude"
    with pytest.raises(HTTPException) as unavailable:
        local_product_provider(f"Build the app for {audience} with Codex", "owner")
    assert unavailable.value.status_code == 409
    assert unavailable.value.detail["code"] == "build_tool_needs_setup"
    assert "Settings > Keys and connections > Coding tools" in unavailable.value.detail["message"]
    assert local_product_provider(f"Build the app for {audience} with Codex and Claude Code", "owner") is None
    status[0].update(available=True, subscription_login=False)
    with pytest.raises(HTTPException, match="sign in with your subscription"):
        local_product_provider(f"Build the app for {audience} with Codex", "owner")
    status[0]["subscription_login"] = True
    assert local_product_provider(f"Build the app for {audience}", "owner") == "codex"
    monkeypatch.setattr("app.coding_agents_consent.consents", lambda subject: {"codex": False, "claude": False})
    assert local_product_provider(f"Build the app for {audience}", "owner") is None


@pytest.mark.parametrize("audience", ["Dispatch managers", "Home cooks"])
@pytest.mark.parametrize("provider", ["codex", "claude", "cursor"])
def test_local_tool_selection_honors_the_requested_connected_tool(monkeypatch, matcher, audience, provider):
    from fastapi import HTTPException
    from app.chat.product_build_request import local_product_provider

    providers = ("codex", "claude", "cursor", "opencode")
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    allowed = dict.fromkeys(providers, True)
    monkeypatch.setattr("app.coding_agents_consent.consents", lambda _subject: allowed)
    status = [{"name": name, "installed": True, "signed_in": True, "available": True,
               "subscription_login": True} for name in providers]
    monkeypatch.setattr("desktop_runtime.agent_status.statuses", lambda: status)
    text = f"Use {provider} to build the app for {audience}"
    assert matcher(text)
    assert local_product_provider(text, "owner") == provider
    assert local_product_provider(f"{text} with another tool, Codex and Claude Code", "owner") is None
    allowed[provider] = False
    with pytest.raises(HTTPException) as unauthorized:
        local_product_provider(text, "owner")
    assert unauthorized.value.status_code == 409
    assert unauthorized.value.detail["code"] == "build_tool_needs_setup"
    assert "Settings > Keys and connections > Coding tools" in unauthorized.value.detail["message"]
    allowed[provider] = True
    next(item for item in status if item["name"] == provider)["subscription_login"] = False
    with pytest.raises(HTTPException, match="sign in with your subscription"):
        local_product_provider(text, "owner")
    assert local_product_provider(text, None) is None


@pytest.mark.parametrize("audience", ["Dispatch managers", "Home cooks"])
@pytest.mark.parametrize("ready", [True, False])
def test_local_build_capability_prompt_uses_owner_readiness(monkeypatch, matcher, audience, ready):
    import sys
    from types import SimpleNamespace
    from app.org.manager import persona_instructions

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.delenv("SUPYMEM_CODING_LOOP_ENABLED", raising=False)
    next_step = "Open Settings > Keys and connections > Coding tools, connect your tool, then retry."
    monkeypatch.setitem(sys.modules, "app.core.local_capabilities", SimpleNamespace(
        enabled=lambda name: ready if name == "build_with_my_tool" else True,
        capability=lambda name: {"state": "ready" if ready else "needs_setup", "next_step": next_step},
        blocked_message=lambda name: next_step))
    text = persona_instructions("Your assistant", idea=audience)
    assert "build.tool.start" in text and "without a confirmation card" in text
    assert "recommendation or follow-up question never authorizes execution" in text
    assert "Research is optional context, never a prerequisite" in text
    assert "Claude Code, Codex, Cursor or OpenCode CLI" in text
    assert "pilot is currently disabled" not in text and "only allowlisted" not in text
    assert (next_step in text) is (not ready)


@pytest.mark.parametrize("audience", ["Dispatch managers", "Home cooks"])
def test_local_tool_build_is_not_diverted_to_written_content(monkeypatch, matcher, audience):
    from app.chat.idea_bridge import written_request

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    text = f"Create an app for {audience} that saves market test scripts"
    assert matcher(text)
    assert not written_request(text)


@pytest.mark.parametrize("audience", ["Dispatch managers", "Home cooks"])
@pytest.mark.parametrize("instruction", ['"Build the app for {audience}"', "Don't build the app for {audience}",
    "Could Codex build the app for {audience}?", "You could build an app for {audience}"])
def test_local_model_route_cannot_authorize_unrequested_build(journey_db, monkeypatch, audience, instruction):
    import asyncio
    from types import SimpleNamespace
    from app.chat import router, execute

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    prepared = []
    async def prepare(venture_id, context, intent, text, *args, **kwargs):
        prepared.append(intent)
        return {"kind": "navigation", "body_md": "Review your saved plan.", "actions": [
            {"action": "build.start", "style": "primary", "payload": {}}], "citations": []}
    monkeypatch.setattr(router, "_prepare_intent", prepare)
    monkeypatch.setattr(execute, "start_requested_work", lambda *_args, **_kwargs: pytest.fail("No requested build"))
    context = SimpleNamespace(venture={"name": audience})
    asyncio.run(router._dispatch_intent(journey_db["vid"], context, "start_product_build",
        instruction.format(audience=audience), None, actor_subject=journey_db["actor"]))
    assert prepared == ["assistant"]


@pytest.mark.parametrize("audience", ["Dispatch managers", "Home cooks"])
def test_build_recommendation_button_never_starts_without_owner_request(monkeypatch, matcher, audience):
    import asyncio
    from app.chat import confirmations, execute

    reply = {"kind": "navigation", "body_md": f"You can build your app for {audience} next.", "actions": [
        {"label": "Build it with your tool", "action": "build.tool.start", "style": "primary", "payload": {"text": "Build the app"}}]}
    monkeypatch.setattr(execute, "execute_action", lambda *_args, **_kwargs: pytest.fail("Recommendation is not authorization"))
    assert asyncio.run(confirmations.start_requested_work("venture", reply, "owner")) is reply


@pytest.mark.parametrize("audience", ["Dispatch managers", "Home cooks"])
@pytest.mark.parametrize("secret", [False, True])
def test_local_build_action_validates_request_before_tool_access(monkeypatch, matcher, audience, secret):
    from app.chat.execute import _start_tool_build

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr("app.core.local_owner.current_owner", lambda: pytest.fail("No tool access"))
    monkeypatch.setattr("app.secrets.detect.looks_like_secret", lambda text: secret)
    text = f"Build the app for {audience}" if secret else f'Explain "build the app for {audience}"'
    reply = _start_tool_build("venture", {"text": text}, "owner")
    assert reply["action_started"] is False and reply["kind"] != "confirmation"
    assert ("Settings > Keys and connections" if secret else "explicitly ask") in reply["body_md"]


@pytest.mark.parametrize("audience", ["Dispatch managers", "Home cooks"])
@pytest.mark.parametrize("code,next_step", [
    ("device_product_build_unavailable", "venture home"),
    ("device_coding_consent_required", "choose Allow Charlie"),
    ("device_coding_subscription_required", "your own subscription"),
    ("device_product_scope_unsupported", "keep your full requirements"),
    ("device_coding_request_contains_secret", "resend without secrets"),
    ("device_coding_request_too_long", "In your venture chat, shorten your request to the code changes you want made, then send it again."),
])
def test_local_build_admission_failures_give_exact_next_step(monkeypatch, matcher, audience, code, next_step):
    from types import SimpleNamespace
    from app.chat import execute, product_build_request
    from app.build import device_orders

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr("app.core.local_owner.current_owner", lambda: SimpleNamespace(id="owner"))
    monkeypatch.setattr(product_build_request, "local_product_provider", lambda *_args: "codex")
    def prepare(*args, **kwargs):
        error = ValueError(code)
        if code == "device_product_scope_unsupported":
            error.reasons = ["Shared account storage is outside this local build."]
        raise error
    monkeypatch.setattr("app.builder.ctl.request_local_build", prepare)
    monkeypatch.setattr(device_orders, "transition", lambda *_args: pytest.fail("Admission was refused"))
    reply = execute._start_tool_build("venture", {"text": f"Build the app for {audience}"}, "owner")
    assert reply["action_started"] is False and next_step in reply["body_md"]
    assert code not in str(reply) and "credit" not in str(reply).lower()


@pytest.mark.parametrize("audience", ["Dispatch managers", "Home cooks"])
@pytest.mark.parametrize("structured", [True, False])
def test_local_build_connection_failure_keeps_the_safe_next_step(monkeypatch, matcher, audience, structured):
    from types import SimpleNamespace
    from fastapi import HTTPException
    from app.chat import execute, product_build_request

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr("app.core.local_owner.current_owner", lambda: SimpleNamespace(id="owner"))
    monkeypatch.setattr(product_build_request, "local_product_provider", lambda *_args: "cursor")
    next_step = "Open Settings > Keys and connections > Coding tools, allow Charlie to use Cursor, then retry."
    def unavailable(*_args, **_kwargs):
        raise HTTPException(409, detail={"code": "build_tool_needs_setup", "message": next_step}
                            if structured else "internal_connection_detail")
    monkeypatch.setattr("app.builder.ctl.request_local_build", unavailable)
    reply = execute._start_tool_build("venture", {"text": f"Build the app for {audience} with Cursor"}, "owner")
    assert reply["action_started"] is False
    assert "Settings > Keys and connections > Coding tools" in reply["body_md"]
    assert (reply["body_md"] == next_step) is structured
    assert "internal_connection_detail" not in str(reply) and "build_tool_needs_setup" not in str(reply)


@pytest.mark.parametrize("audience,focus", [("Dispatch managers", "bulk inspection templates"),
    ("Home cooks", "weekly ingredient substitutions")])
@pytest.mark.parametrize("invalid", [None, "too_long", "contains_secret"])
def test_local_tool_plan_keeps_requested_focus_and_rejects_unsafe_input(journey_db, monkeypatch, audience, focus, invalid):
    import json
    from types import SimpleNamespace
    from app.build import device_orders

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(device_orders, "_scope", lambda *_args: (journey_db["wid"], journey_db["actor"], None))
    monkeypatch.setattr(device_orders, "coding_loop_enabled", lambda *_args: True)
    monkeypatch.setattr(device_orders, "_product_build_snapshot", lambda *_args: {"confirmed_idea": audience})
    monkeypatch.setattr("app.secrets.detect.looks_like_secret", lambda *_args: invalid == "contains_secret")
    prepared = []
    def prepare(_venture_id, _caller, manifest, **_kwargs):
        prepared.append(manifest)
        return {"manifest": manifest.model_dump()}
    monkeypatch.setattr(device_orders, "prepare", prepare)
    instruction = f"Build the app for {audience} with Codex, focus on {focus}"
    if invalid == "too_long":
        instruction += " " + "a" * 12000
    caller = SimpleNamespace(sub=journey_db["actor"])
    if invalid:
        with pytest.raises(ValueError, match="device_coding_request_" + invalid):
            device_orders.prepare_product_build_from_chat(journey_db["vid"], caller, instruction, provider="codex")
        assert not prepared
    else:
        order = device_orders.prepare_product_build_from_chat(journey_db["vid"], caller, instruction, provider="codex")
        assert json.dumps(instruction, ensure_ascii=False) in order["manifest"]["instruction"]
        assert "not additional permissions" in order["manifest"]["instruction"]

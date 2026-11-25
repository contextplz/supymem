"""An open-build chat command navigates to the saved same-venture run."""

import logging

import pytest

from tests.company_os_harness import load_company_os_app, local_chat as local_chat
from tests.unit.chat.test_round3_matrix import venture as venture


@pytest.fixture
def hosted_build(monkeypatch):
    """Managed build prerequisites remain hosted; local builds use your tool."""
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")


@pytest.fixture(params=["Warehouse maintenance scheduling", "Family hiking itineraries"])
def saved_build(hosted_build, venture, request):
    from app.db.models import Venture, Workspace
    from app.state import db
    from core.clock import utcnow

    with db()() as session:
        session.add(Venture(id="ven_ours", workspace_id=venture[2], name="Build fixture",
            raw_idea=request.param, stage="idea", created_at=utcnow(), updated_at=utcnow()))
        session.commit()
        return session.get(Workspace, venture[2]).owner_id


@pytest.mark.asyncio
async def test_open_build_uses_live_exact_run_and_never_foreign_route(monkeypatch):
    load_company_os_app()
    from app.chat.context import ChatContext
    from app.chat.router import _dispatch_intent
    from app.ventures import run_status

    ctx = ChatContext(venture={"id": "ven_ours", "workspace_id": "wsp_ours"})
    monkeypatch.setattr(run_status, "snapshot", lambda *_args: {
        "action": {"href": "/v/ven_ours/builds?app=app_one&run=bld_one"},
    })
    reply = await _dispatch_intent("ven_ours", ctx, "open_build", "Open the build", None)
    assert reply["kind"] == "navigation"
    assert reply["actions"][0]["href"] == "/v/ven_ours/builds?app=app_one&run=bld_one"

    monkeypatch.setattr(run_status, "snapshot", lambda *_args: {
        "action": {"href": "/v/ven_other/builds?app=app_secret"},
    })
    reply = await _dispatch_intent("ven_ours", ctx, "open_build", "Open the build", None)
    assert reply["actions"][0]["href"] == "/v/ven_ours/builds"


@pytest.mark.asyncio
@pytest.mark.parametrize(("command", "route"), [
    ("Open Settings", "settings"),
    ("Show me analytics", "analytics"),
    ("Go to the research page", "research"),
    ("Take me to the cockpit", ""),
    ("Open execution", "plan"),
    ("Open analytics in Supymem", "analytics"),
    ("Open LabHire Analytics in Supymem. Do not modify data.", "analytics"),
    ("Open LabHire Analytics in Supymem and tell me what the founder dashboard actually shows. "
     "Do not start a background task or claim those numbers are verified users.", "analytics"),
    ("Open analytics in Supymem and tell me what the founder dashboard actually shows. "
     "Answer only; do not start work.", "analytics"),
    ("Can you open Builds in Supymem?", "builds"),
])
async def test_explicit_chat_navigation_stays_on_current_venture(command, route):
    load_company_os_app()
    from app.chat.context import ChatContext
    from app.chat.intent import classify_intent
    from app.chat.router import _dispatch_intent

    assert classify_intent(command) == "open_screen"
    reply = await _dispatch_intent("ven_ours", ChatContext(venture={"id": "ven_ours", "name": "LabHire"}), "open_screen", command, None)
    assert reply["kind"] == "navigation"
    assert reply["actions"][0]["href"] == "/v/ven_ours" + (f"/{route}" if route else "")


@pytest.mark.asyncio
async def test_named_chat_navigation_refuses_a_different_venture():
    load_company_os_app()
    from app.chat.context import ChatContext
    from app.chat.intent import classify_intent
    from app.chat.router import _dispatch_intent

    command = "Open OtherCompany analytics in Supymem"
    assert classify_intent(command) == "open_screen"
    reply = await _dispatch_intent("ven_ours", ChatContext(venture={"id": "ven_ours", "name": "LabHire"}),
                                   "open_screen", command, None)
    assert reply["actions"] == []
    assert "Switch ventures" in reply["body_md"]


def test_chat_navigation_does_not_hijack_work_or_external_requests():
    load_company_os_app()
    from app.chat.intent import classify_intent

    assert classify_intent("Show me how to build a landing page") != "open_screen"
    assert classify_intent("Open https://example.com") != "open_screen"
    assert classify_intent("Change settings to autopilot") != "open_screen"
    assert classify_intent("Open analytics and delete the venture") != "open_screen"
    assert classify_intent("Open analytics in Supymem and click Delete") != "open_screen"


@pytest.mark.asyncio
async def test_build_my_startup_is_a_reviewed_product_path_not_a_generic_note(saved_build, monkeypatch):
    load_company_os_app()
    from app.chat.context import ChatContext
    from app.chat.intent import classify_intent
    from app.chat.router import _dispatch_intent
    from app.ventures import state_brief
    from app.planning import compiler
    from app.builder import execution

    monkeypatch.setattr(compiler, "get_plan", lambda _id: {"method": "metered_foundry_plan"})
    monkeypatch.setattr(execution, "start", lambda *_a, **_kw: pytest.fail("build started before confirmation"))

    for command in ("build me my startup", "Please start building my product", "create our app",
                    "Build my Instagram bot", "Can you build an Instagram bot with Supabase?",
                    "Help me build my startup", "I want to build my startup", "I need to build my startup",
                    "Let's start our company"):
        assert classify_intent(command) == "start_product_build"
    ctx = ChatContext(venture={"id": "ven_ours", "workspace_id": "wsp_ours"})
    monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda *_args, **_kwargs: {
        "idea": {"confirmed": True}, "research": {"present": True, "plan_ready": True},
        "plan": {"present": True, "id": "pln_one", "state": "draft"}})
    draft = await _dispatch_intent("ven_ours", ctx, "start_product_build", "I need to build my startup", None)
    assert draft["actions"][0]["action"] == "plan.approve" and not draft["actions"][0].get("href")
    assert "Review and approve the build plan" in draft["body_md"]
    assert "Nothing was started or published" in draft["body_md"]

    monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda *_args, **_kwargs: {
        "idea": {"confirmed": True, "stage": "planning"},
        "research": {"present": True, "plan_ready": True},
        "plan": {"present": True, "id": "pln_one", "state": "draft"},
        "builds": {"last": {"state": "failed", "closed": True}},
    })
    grounded = await _dispatch_intent("ven_ours", ctx, "start_product_build", "Help me build my startup", None)
    assert grounded["actions"][0]["action"] == "plan.approve"
    assert "Nothing was started or published" in grounded["body_md"]
    assert len(grounded["actions"]) == 1

    monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda *_args, **_kwargs: {
        "idea": {"confirmed": True}, "research": {"present": True, "plan_ready": True},
        "plan": {"present": True, "state": "approved"}})
    ready = await _dispatch_intent("ven_ours", ctx, "start_product_build", "build me my startup", None)
    assert ready["actions"][0]["action"] == "plan.generate" and not ready["actions"][0].get("href")
    assert ready["actions"][0]["payload"] == {"regenerate": True}
    assert "Update the build plan" in ready["body_md"] and "Nothing was started or published" in ready["body_md"]


@pytest.mark.asyncio
async def test_personal_build_request_hands_off_to_first_real_prerequisite(hosted_build, monkeypatch):
    load_company_os_app()
    from app.chat.context import ChatContext
    from app.chat.router import _dispatch_intent
    from app.ventures import state_brief

    ctx = ChatContext(venture={"id": "ven_ours", "workspace_id": "wsp_ours"})
    stages = [
        ({"idea": {"confirmed": False, "project_intent": "personal"}, "research": {"present": False}}, "idea"),
        ({"idea": {"confirmed": True, "project_intent": "personal"}, "research": {"present": False}}, "idea"),
        ({"idea": {"confirmed": True, "project_intent": "personal"}, "research": {"present": True, "plan_ready": False}}, "idea"),
        ({"idea": {"confirmed": True, "project_intent": "personal"}, "research": {"present": True}, "plan": {"state": "draft"}}, "idea"),
        ({"idea": {"confirmed": True, "project_intent": "personal"}, "research": {"present": True}, "plan": {"state": "approved"}}, "idea"),
    ]
    for brief, route in stages:
        monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda *_args, **_kwargs: brief)
        reply = await _dispatch_intent("ven_ours", ctx, "start_product_build", "Build my Instagram bot", None)
        assert reply["actions"][0]["href"] == f"/v/ven_ours/{route}"
        assert "No product build or publication was started" in reply["body_md"]
        assert "confirm" in reply["body_md"].lower()
        assert not any(action.get("confirmation") for action in reply["actions"])


@pytest.mark.asyncio
@pytest.mark.parametrize("audience", ["Warehouse maintenance teams", "Family hiking groups"])
@pytest.mark.parametrize("connected", [False, True])
async def test_local_build_request_uses_your_tool_or_its_connection_screen(venture, monkeypatch, audience, connected):
    from app.chat import execute as chat_execute, router
    from app.chat.context import ChatContext
    from app.core import local_capabilities
    from app.builder import execution
    from app.workspaces.resolve import LOCAL_PERSON_ID

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    next_step = "Open Settings > Keys and connections > Coding tools, connect your tool, then retry your build."
    monkeypatch.setattr(local_capabilities, "capability", lambda name: {"state": "ready" if connected else "needs_setup",
        "next_step": next_step})
    monkeypatch.setattr("app.chat.guard.guard_reply", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.chat.other_venture.other_venture_reply", lambda *_args, **_kwargs: None)

    def forbidden(*_args, **_kwargs):
        pytest.fail("Your local build reached the managed builder")

    monkeypatch.setattr(execution, "estimate", forbidden)
    monkeypatch.setattr(execution, "start", forbidden)
    calls = []

    def start(venture_id, payload, actor):
        calls.append((venture_id, payload, actor))
        return {"kind": "text", "body_md": "Open Build activity to watch your build.",
                "action_started": True, "actions": [], "citations": []}

    monkeypatch.setattr(chat_execute, "_active_product_build_reply", lambda *_: None)
    monkeypatch.setattr(chat_execute, "_start_tool_build", start)
    venture_id = venture[1]
    text = f"Build the app for {audience}"
    reply = await router._dispatch_intent(venture_id,
        ChatContext(venture={"id": venture_id, "name": audience, "workspace_id": venture[2]}),
        "start_product_build", text, None, actor_subject=LOCAL_PERSON_ID)
    if connected:
        assert calls == [(venture_id, {"text": text}, LOCAL_PERSON_ID)]
        assert reply["action_started"] is True
    else:
        assert calls == [] and reply["body_md"] == next_step
        assert reply["action_started"] is False
    assert reply["kind"] != "confirmation" and "credit" not in str(reply).lower()


@pytest.mark.asyncio
async def test_build_request_checks_actual_plan_and_run_gates(saved_build, monkeypatch):
    load_company_os_app()
    from app.chat.context import ChatContext
    from app.chat.router import _dispatch_intent
    from app.planning import compiler
    from app.ventures import state_brief
    from app.builder import execution

    monkeypatch.setattr(execution, "estimate", lambda *_a, **_kw: pytest.fail("quoted before prerequisite review"))
    monkeypatch.setattr(execution, "start", lambda *_a, **_kw: pytest.fail("build started before confirmation"))

    ctx = ChatContext(venture={"id": "ven_ours", "workspace_id": "wsp_ours"})
    base = {"idea": {"confirmed": True}, "research": {"plan_ready": True},
            "plan": {"present": True, "id": "pln_one", "state": "approved"}}
    monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda *_a, **_kw: base)
    monkeypatch.setattr(compiler, "get_plan", lambda _id: {
        "method": "metered_foundry_plan", "stale_dependencies": True, "newer_plan_id": None})
    stale = await _dispatch_intent("ven_ours", ctx, "start_product_build", "build me my startup", None)
    assert stale["actions"][0]["action"] == "plan.generate" and not stale["actions"][0].get("href")
    assert stale["actions"][0]["payload"] == {"regenerate": True}
    assert "Update the build plan" in stale["body_md"] and "Nothing was started or published" in stale["body_md"]

    monkeypatch.setattr(compiler, "get_plan", lambda _id: {
        "method": "metered_foundry_plan", "stale_dependencies": False, "newer_plan_id": None})
    base["builds"] = {"last": {"state": "succeeded", "review_status": "pending", "closed": True}}
    unreviewed = await _dispatch_intent("ven_ours", ctx, "start_product_build", "build me my startup", None)
    assert "founder acceptance is not saved" in unreviewed["body_md"]

    base["builds"] = {"last": {"state": "running", "closed": False}}
    active = await _dispatch_intent("ven_ours", ctx, "start_product_build", "build me my startup", None)
    assert active["actions"][0]["href"] == "/v/ven_ours/builds"
    assert "latest build is running" in active["body_md"]

    base["builds"] = {"last": {"state": "failed", "closed": False}}
    unsettled = await _dispatch_intent("ven_ours", ctx, "start_product_build", "build me my startup", None)
    assert "has not closed and settled" in unsettled["body_md"]

    base["builds"] = {"last": {"state": "failed", "closed": True}}
    failed = await _dispatch_intent("ven_ours", ctx, "start_product_build", "build me my startup", None)
    assert "will not retry automatically" in failed["body_md"]


@pytest.mark.asyncio
async def test_build_request_requires_plan_ready_research_not_only_a_saved_dossier(saved_build, monkeypatch):
    load_company_os_app()
    from app.chat.context import ChatContext
    from app.chat.router import _dispatch_intent
    from app.ventures import state_brief
    from app.builder import execution

    monkeypatch.setattr(execution, "estimate", lambda *_a, **_kw: pytest.fail("quoted before research was ready"))
    monkeypatch.setattr(execution, "start", lambda *_a, **_kw: pytest.fail("build started before confirmation"))

    monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda *_a, **_kw: {
        "idea": {"confirmed": True}, "research": {"present": True, "plan_ready": False},
        "plan": {"present": True, "state": "approved"}})
    reply = await _dispatch_intent("ven_ours", ChatContext(venture={"id": "ven_ours"}),
                                   "start_product_build", "build me my startup", None)
    assert reply["actions"][0]["action"] == "research.prepare" and not reply["actions"][0].get("href")
    assert 'Research competitors and demand' in reply['body_md'] and 'credit' not in reply['body_md'].lower()
    assert "Nothing was started or published" in reply["body_md"]


@pytest.mark.asyncio
async def test_approved_build_starts_directly_with_exact_internal_budget(saved_build, monkeypatch):
    load_company_os_app()
    from app.chat.context import ChatContext
    from app.chat.router import _dispatch_intent
    from app.planning import compiler
    from app.ventures import state_brief
    from app.infra import apps
    from app.builder import execution

    monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda *_a, **_kw: {
        "idea": {"confirmed": True}, "research": {"plan_ready": True},
        "plan": {"present": True, "id": "pln_one", "state": "approved"}})
    monkeypatch.setattr(compiler, "get_plan", lambda _id: {
        "method": "metered_foundry_plan", "stale_dependencies": False,
        "milestones": [{"title": "Core product", "included": True, "tasks": [{
            "id": "pt_one", "title": "Build first workflow", "state": "pending",
            "capability_id": "build.run", "implementation": "new_code"}]}]})
    monkeypatch.setattr(apps, "list_apps", lambda _id: [{
        "id": "app_one", "name": "SynthSwap", "build_mode": "supymem"}])
    calls = []
    monkeypatch.setattr(execution, "estimate", lambda app_id, **kwargs: (
        calls.append((app_id, kwargs)) or {"ready": True, "ceiling_micro": 1200000,
            "quote_id": "bq_one", "input_digest": "digest_one", "expires_at": "2026-09-29T12:00:00Z"}))
    starts = []
    monkeypatch.setattr(apps, "get_app", lambda _id: {"venture_id": "ven_ours"})
    monkeypatch.setattr(execution, "start", lambda app_id, **kwargs: starts.append((app_id, kwargs)) or {"id": "run_one"})
    reply = await _dispatch_intent("ven_ours", ChatContext(venture={"id": "ven_ours"}),
                                   "start_product_build", "build me my startup", None, saved_build)
    assert calls == [("app_one", {"actor": saved_build, "plan_id": "pln_one", "plan_task_id": "pt_one"})]
    assert reply["kind"] == "text" and not any(action.get("confirmation") for action in reply["actions"])
    assert 'credit' not in reply['body_md'].lower()
    assert starts == [("app_one", {"actor": saved_build, "quote_id": "bq_one", "input_digest": "digest_one",
                                  "approve_budget_micro": 1200000, "idempotency_key": "chat-build:bq_one"})]


@pytest.mark.hosted_only
@pytest.mark.parametrize("estimate", [{"ready": False, "ceiling_micro": 10}, {"ready": True, "ceiling_micro": "10"}])
def test_unready_build_estimate_keeps_validation_and_a_private_safe_retry(saved_build, monkeypatch, estimate):
    from app.builder import execution
    from app.chat import build_chain

    monkeypatch.setattr(execution, "estimate", lambda *args, **kwargs: {
        **estimate, "reasons": ["private founder idea sk-private-credential"]})
    monkeypatch.setattr(execution, "start", lambda *args, **kwargs: pytest.fail("An unready estimate cannot start a build"))
    plan = {"milestones": [{"included": True, "title": "Build", "tasks": [{"id": "pt_one", "title": "Page",
        "state": "ready", "capability_id": "build.run", "implementation": "new_code"}]}]}
    reply = build_chain.reply("ven_ours", "", "pln_one", plan, [{"id": "app_one"}], "Fixture", saved_build)
    assert "not ready yet" in reply["body_md"] and "No build or publication was started" in reply["body_md"]
    assert "private" not in reply["body_md"] and "credential" not in reply["body_md"]
    assert "credit" not in reply["body_md"].lower() and "model" not in reply["body_md"].lower()
    assert reply["actions"] == [{"label": "Retry", "action": "build.prepare", "payload": {"plan_task_id": "pt_one"}, "retry": True}]


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", ["BuildError", "KeyError", "PermissionError", "ValueError", "RuntimeError"])
async def test_blocked_build_quote_returns_truthful_logged_retry(saved_build, venture, monkeypatch, caplog, error_type):
    load_company_os_app()
    from app.chat.context import ChatContext
    from app.chat.router import _dispatch_intent
    from app.planning import compiler
    from app.ventures import state_brief
    from app.infra import apps
    from app.builder import execution

    monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda *_a, **_kw: {
        "idea": {"confirmed": True}, "research": {"plan_ready": True},
        "plan": {"present": True, "id": "pln_one", "state": "approved"}})
    monkeypatch.setattr(compiler, "get_plan", lambda _id: {
        "method": "metered_foundry_plan", "stale_dependencies": False,
        "milestones": [{"title": "Core", "included": True, "tasks": [{
            "id": "pt_one", "title": "Build", "state": "pending",
            "capability_id": "build.run", "implementation": "new_code"}]}]})
    monkeypatch.setattr(apps, "list_apps", lambda _id: [{
        "id": "app_one", "name": "SynthSwap", "build_mode": "supymem"}])
    private = "build_inputs_changed private founder idea sk-private-credential"
    errors = {"BuildError": execution.BuildError, "KeyError": KeyError,
              "PermissionError": PermissionError, "ValueError": ValueError, "RuntimeError": RuntimeError}

    def unavailable(*args, **kwargs):
        raise errors[error_type](private)

    monkeypatch.setattr(execution, "estimate", unavailable)
    monkeypatch.setattr(execution, "start", lambda *args, **kwargs: pytest.fail("Build started after a failed estimate"))
    command = "build me my startup"
    with caplog.at_level(logging.ERROR):
        if error_type == "RuntimeError":
            from app.api import chat_api

            async def through_router(venture_id, text, *, actor_subject=None, **kwargs):
                return await _dispatch_intent(venture_id, ChatContext(venture={"id": venture_id}),
                    "start_product_build", text, None, actor_subject)

            monkeypatch.setattr(chat_api, "handle_message", through_router)
            reply = await chat_api._generic_reply("ven_ours", chat_api.ChatBody(message=command),
                                                 command, saved_build, venture[2])
        else:
            reply = await _dispatch_intent("ven_ours", ChatContext(venture={"id": "ven_ours"}),
                                          "start_product_build", command, None, saved_build)
    assert "Choose Retry to check your saved work." in reply["body_md"]
    assert "build_inputs_changed" not in reply["body_md"]
    assert "private founder idea" not in reply["body_md"] and "sk-private-credential" not in reply["body_md"]
    assert "credit" not in reply["body_md"].lower() and "model" not in reply["body_md"].lower()
    assert "\u2014" not in reply["body_md"]
    retry = reply["actions"][0]
    assert retry["label"] == "Retry"
    if error_type == "RuntimeError":
        assert retry["action"] == "chat.reply" and retry["payload"]["text"] == command
    else:
        from app.chat.actions import get_action

        assert retry["action"] == get_action("build.prepare").name
        assert retry["payload"] == {"plan_task_id": "pt_one"}
        assert "No build or publication was started" in reply["body_md"]
    assert any(record.getMessage() == "founder_action_failed"
               and record.venture_id == "ven_ours" and record.error_type == error_type
               for record in caplog.records)
    assert private not in caplog.text
    assert "private founder idea" not in caplog.text and "sk-private-credential" not in caplog.text


@pytest.mark.asyncio
async def test_chat_build_confirmation_uses_authenticated_actor_and_exact_quote(venture, monkeypatch):
    _, vid, _ = venture
    from app.chat.execute import _execute_action
    from app.infra import apps
    from app.builder import execution
    from app.workspaces.resolve import LOCAL_PERSON_ID

    monkeypatch.setattr(apps, "get_app", lambda _id: {"venture_id": vid})
    calls = []
    monkeypatch.setattr(execution, "start", lambda app_id, **kwargs: (
        calls.append((app_id, kwargs)) or {"id": "bld_one"}))
    payload = {"app_id": "app_one", "quote_id": "bq_one", "input_digest": "digest_one",
               "ceiling_micro": 1200000, "actor": "forged_person"}
    with pytest.raises(PermissionError):
        await _execute_action(vid, "build.start", payload=payload)
    assert calls == []
    reply = await _execute_action(vid, "build.start", payload=payload,
                                  actor_subject=LOCAL_PERSON_ID)
    assert calls == [("app_one", {"actor": LOCAL_PERSON_ID, "quote_id": "bq_one",
        "input_digest": "digest_one", "approve_budget_micro": 1200000,
        "idempotency_key": "chat-build:bq_one"})]
    assert "queued" in reply["body_md"]


@pytest.mark.asyncio
async def test_approved_plan_without_app_creates_the_private_draft_directly(saved_build, monkeypatch):
    load_company_os_app()
    from app.chat.context import ChatContext
    from app.chat.router import _dispatch_intent
    from app.planning import compiler
    from app.ventures import state_brief
    from app.infra import apps

    monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda *_a, **_kw: {
        "idea": {"confirmed": True, "name": "Synth Swap"}, "research": {"plan_ready": True},
        "plan": {"present": True, "id": "pln_one", "state": "approved"}})
    monkeypatch.setattr(compiler, "get_plan", lambda _id: {
        "method": "metered_foundry_plan", "stale_dependencies": False, "milestones": []})
    monkeypatch.setattr(apps, "list_apps", lambda _id: [{"id": "app_saved", "build_mode": "supymem", "name": item["name"]} for item in created])
    monkeypatch.setattr(apps, "slug_available", lambda _slug: {"available": True})
    created = []
    monkeypatch.setattr(apps, "create_draft", lambda **kwargs: created.append(kwargs) or {"name": kwargs["name"]})
    reply = await _dispatch_intent("ven_ours", ChatContext(venture={"id": "ven_ours"}),
                                   "start_product_build", "build me my startup", None, saved_build)
    assert reply["kind"] == "text" and not any(action.get("confirmation") for action in reply["actions"])
    assert len(created) == 1 and (created[0]["name"], created[0]["slug"]) == ("Synth Swap", "synth-swap")
    assert created[0]["actor"] == saved_build and created[0]["venture_id"] == "ven_ours"
    assert "credit" not in reply["body_md"].lower() and "No build or publication was started" in reply["body_md"]
    assert "no executable" in reply["body_md"] and "Review the plan" in reply["body_md"]


@pytest.mark.asyncio
async def test_draft_app_confirmation_uses_saved_venture_scope(venture, monkeypatch):
    _, vid, wid = venture
    from app.chat.execute import _execute_action
    from app.db.models import Venture
    from app.infra import apps
    from app.workspaces.resolve import LOCAL_PERSON_ID

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, model, venture_id):
            assert model is Venture and venture_id == vid
            return type("SavedVenture", (), {"workspace_id": wid})()

    from app.chat import execute
    monkeypatch.setattr(execute, "db", lambda: Session)
    calls = []
    monkeypatch.setattr(apps, "create_draft", lambda **kwargs: (
        calls.append(kwargs) or {"name": "Synth Swap"}))
    payload = {"name": "Synth Swap", "slug": "synth-swap", "workspace_id": "wsp_foreign"}
    with pytest.raises(PermissionError):
        await _execute_action(vid, "build.app.create", payload=payload)
    assert calls == []
    reply = await _execute_action(vid, "build.app.create", payload=payload,
                                  actor_subject=LOCAL_PERSON_ID)
    assert calls == [{"workspace_id": wid, "venture_id": vid,
        "name": "Synth Swap", "slug": "synth-swap", "actor": LOCAL_PERSON_ID,
        "idempotency_key": "chat-build-app:synth-swap"}]
    assert "No build or publication started" in reply["body_md"]


@pytest.mark.asyncio
async def test_analytics_navigation_describes_saved_browser_events(monkeypatch):
    load_company_os_app()
    from app.chat.context import ChatContext
    from app.chat.router import _dispatch_intent
    from app.ventures import state_brief

    monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda *_args, **_kwargs: {
        "website": {"waitlist_count": 2},
        "analytics": {"state": "recorded", "counts": {"landing_view": 0,
            "cta_click": 0, "signup": 1, "first_action": 0, "payment": 0},
            "steps": ["landing_view", "cta_click", "signup", "first_action", "payment"]},
    })
    command = ("Open analytics in Supymem and tell me what the founder dashboard actually shows. "
               "Answer only; do not start work.")
    reply = await _dispatch_intent("ven_ours", ChatContext(venture={"id": "ven_ours", "name": "LabHire"}),
                                   "open_screen", command, None)
    assert reply["kind"] == "navigation"
    assert reply["actions"][0]["href"] == "/v/ven_ours/analytics"
    assert reply["_served_read"] == "open_screen.analytics_read"
    assert "managed waitlist ledger has 2 saved consented requests" in reply["body_md"]
    assert "1 reported signup event" in reply["body_md"]
    assert "not unique or verified users" in reply["body_md"]

    monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda *_args, **_kwargs: {
        "analytics": {"state": "unavailable"},
    })
    unavailable = await _dispatch_intent("ven_ours", ChatContext(venture={"id": "ven_ours", "name": "LabHire"}),
                                         "open_screen", command, None)
    assert unavailable["_served_read"] is None
    assert "waitlist count is unavailable" in unavailable["body_md"]
    assert "browser-event counts are unavailable" in unavailable["body_md"]


@pytest.mark.asyncio
async def test_analytics_navigation_reports_saved_waitlist_without_an_app(monkeypatch):
    load_company_os_app()
    from app.chat.context import ChatContext
    from app.chat.intent import classify_intent
    from app.chat.router import _dispatch_intent
    from app.ventures import state_brief

    monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda *_args, **_kwargs: {
        "website": {"waitlist_count": 0}, "analytics": {"state": "no_app"},
    })
    command = "Open analytics in Supymem and tell me what the founder dashboard actually shows. Answer only; do not start work."
    assert classify_intent(command) == "open_screen"
    reply = await _dispatch_intent("ven_ours", ChatContext(venture={"id": "ven_ours", "name": "My project"}),
                                   "open_screen", command, None)
    assert reply["actions"][0]["href"] == "/v/ven_ours/analytics"
    assert reply["_served_read"] == "open_screen.analytics_read"
    assert "0 saved consented requests" in reply["body_md"]
    assert "synthetic public-submission tests are excluded" in reply["body_md"]
    assert "No app yet. Approve a plan and build first" in reply["body_md"]
    assert "Browser event counts are unavailable" in reply["body_md"]


@pytest.mark.asyncio
async def test_analytics_navigation_distinguishes_empty_events_from_unknown_waitlist(monkeypatch):
    load_company_os_app()
    from app.chat.context import ChatContext
    from app.chat.router import _dispatch_intent
    from app.ventures import state_brief

    monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda *_args, **_kwargs: {
        "website": {"waitlist_count": None},
        "analytics": {"state": "empty", "counts": {"landing_view": 0}, "steps": ["landing_view"]},
    })
    reply = await _dispatch_intent("ven_other", ChatContext(venture={"id": "ven_other", "name": "Hobby"}),
                                   "open_screen", "Open analytics and tell me what the dashboard shows", None)
    assert reply["actions"][0]["href"] == "/v/ven_other/analytics"
    assert "waitlist count is unavailable" in reply["body_md"]
    assert "No recorded visitor events yet" in reply["body_md"]
    assert "0 saved consented requests" not in reply["body_md"]


@pytest.mark.asyncio
async def test_builds_navigation_reports_only_saved_same_venture_evidence(monkeypatch):
    load_company_os_app()
    from app.chat.context import ChatContext
    from app.chat.intent import classify_intent
    from app.chat.router import _dispatch_intent
    from app.ventures import state_brief

    monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda vid, **_kw: {
        "builds": {"last": {"state": "failed", "credits_consumed": 2.5,
                             "failure_reason": "runtime QA did not pass"}}} if vid == "ven_ours" else {})
    command = ("Open Builds in Supymem and report what build evidence is saved for LabHire. "
               "Do not start or modify any build.")
    assert classify_intent(command) == "open_screen"
    reply = await _dispatch_intent("ven_ours", ChatContext(venture={"id": "ven_ours", "name": "LabHire"}),
                                   "open_screen", command, None)
    assert reply["kind"] == "navigation"
    assert reply["actions"][0]["href"] == "/v/ven_ours/builds"
    assert reply["_served_read"] == "open_screen.builds_read"
    assert "latest saved build run is failed" in reply["body_md"]
    assert "runtime QA did not pass" in reply["body_md"]
    assert "No build was started or modified" in reply["body_md"]

    foreign = command.replace("LabHire", "OtherCompany")
    assert classify_intent(foreign) == "open_screen"
    refusal = await _dispatch_intent("ven_ours", ChatContext(venture={"id": "ven_ours", "name": "LabHire"}),
                                     "open_screen", foreign, None)
    assert refusal["actions"] == []
    assert "Switch ventures" in refusal["body_md"]


def test_builds_report_does_not_hijack_effectful_compound_command():
    load_company_os_app()
    from app.chat.intent import classify_intent

    assert classify_intent("Open Builds in Supymem and click Delete") != "open_screen"

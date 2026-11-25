"""Chat plan chain: intent -> card -> confirm -> the same service the Plan page calls."""

import pytest

from tests.company_os_harness import load_company_os_app
from tests.unit.chat.test_round3_matrix import venture  # noqa: F401

load_company_os_app()

QUOTE = {"provider": "openai", "model": "m1", "payer": "platform_credits", "ceiling_micro": 250_000,
         "ceiling_credits": 25, "available_micro": 9_000_000, "sufficient_now": True}


@pytest.mark.parametrize("venture", [
    "A maintenance scheduling tool for factory operators",
    "A neighborhood walking group app for adults",
], indirect=True)
def test_routine_screen_binds_an_owned_private_conversation(venture, monkeypatch):
    import json
    from app.chat import conversations
    from app.core.local_mode import is_local
    from app.db.models import Job, ChatConversation
    from app.db.task_models import Task
    from app.state import db
    from app.tasks import planner, routines
    from app.tasks.contracts import Contract

    client, venture_id, _ = venture
    monkeypatch.setattr(routines, "enabled", lambda: True)
    monkeypatch.setattr(routines, "capability_enabled", lambda _name: True)

    async def preview(goal, **_kwargs):
        assert conversations.current_conversation().visibility == "private"
        return Contract(title="Summarize saved signups", goal=goal,
            done_test={"type": "deterministic", "spec": {"predicate": "all_steps_verified"}},
            steps=[{"title": "Summarize saved signups", "skill": "tasks.waitlist_summary",
                "done_test": {"type": "deterministic", "spec": {"predicate": "workflow_receipt"}}}])

    monkeypatch.setattr(planner, "preview", preview)
    response = client.post(f"/ventures/{venture_id}/routines", json={
        "goal": "every Friday summarize my waitlist signups", "timezone": "UTC"})
    assert response.status_code == 200, response.text
    with db()() as session:
        task = session.get(Task, response.json()["id"])
        conversation = session.get(ChatConversation, json.loads(task.scope_json)["routine_conversation_id"])
        assert conversation.visibility == "private"
        assert conversation.creator_person_id == task.owner_person_id
        jobs = session.query(Job).filter_by(venture_id=venture_id, kind="routines.fire").all()
        assert len(jobs) == 1 and json.loads(jobs[0].payload_json)["_chat_conversation"]["conversation_id"] == conversation.id
    if is_local():
        monkeypatch.setattr(routines, "enabled", lambda: False)
        assert client.get(f"/ventures/{venture_id}/routines").status_code == 200
        assert client.post(f"/ventures/{venture_id}/routines", json={
            "goal": "every Friday summarize my waitlist signups"}).status_code == 403
    assert client.post(f"/ventures/{venture_id}/routines/{task.id}/pause").status_code == 200
    assert client.delete(f"/ventures/{venture_id}/routines/{task.id}").status_code == 200


def test_handoff_api_leaves_transactions_in_the_task_service():
    import ast
    import inspect
    from app.api import handoffs_api

    tree = ast.parse(inspect.getsource(handoffs_api))
    assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"commit", "flush", "rollback"} for node in ast.walk(tree))


def chat(client, vid, text):
    r = client.post(f"/ventures/{vid}/chat", json={"message": text})
    assert r.status_code == 200, r.text
    return r.json()


def confirm(client, vid, action, payload, **extra):
    return client.post(f"/ventures/{vid}/chat/confirm", json={"action": action, "payload": payload, **extra})


def card_of(reply):
    cards = [a["confirmation"] for a in reply["actions"] if a.get("confirmation")]
    return cards[0] if cards else None


@pytest.fixture()
def plans(monkeypatch):
    """Stub the metered compiler; record calls."""
    from app.planning import compiler
    from app.builder import budgets

    state = {"plans": [], "starts": 0, "quote": dict(QUOTE)}

    def quote(*_a, **_k):
        if isinstance(state["quote"], Exception):
            raise state["quote"]
        return state["quote"]

    def start(*_a, **_k):
        state["starts"] += 1
        state["plans"] = [{"id": "pln_1", "state": "founder_review", "version": 1}]
        return {"job_id": "job_1"}

    monkeypatch.setattr(compiler, "generation_quote", quote)
    monkeypatch.setattr(budgets, "runtime_quote", lambda *_args, **_kwargs: {"ready": False, "reasons": []})
    monkeypatch.setattr(compiler, "start", start)
    monkeypatch.setattr(compiler, "list_plans", lambda vid: state["plans"])
    return state


@pytest.mark.parametrize("text,intent", [
    ("Make the plan", "plan_generate"), ("please generate the plan now", "plan_generate"),
    ("plan it", "plan_generate"), ("Approve the plan", "plan_approve"),
    ("looks good, approve", "plan_approve"), ("I don't have any tools", "plan_resources_none"),
    ("start the next task", "work_start"), ("what's the next task?", "work_start"),
    ("how much will the plan cost?", "plan_quote"),
    ("continue to plan", "plan_generate"), ("Continue to planning", "plan_generate"), ("ok proceed to the plan", "plan_generate"),
    ("Retry plan", "plan_generate"), ("retry the plan", "plan_generate"),
    ("generate the plan again", "plan_generate"), ("please redo the plan", "plan_generate"),
    ("don't retry the plan", "assistant"),
    ("don't approve the plan yet", "assistant"), ("Don't make the plan", "assistant"),
    ("approve the plan but do not start anything", "assistant"), ("what is the plan?", "assistant"),
])
def test_intents(text, intent):
    from app.chat.intent import classify_intent

    assert classify_intent(text) == intent


def test_generate_starts_directly_and_repeat_is_idempotent(venture, plans, monkeypatch):
    client, vid, _ = venture
    from app.planning import compiler
    monkeypatch.setattr(compiler, "get_plan", lambda plan_id: {"reviewable": True})
    reply = chat(client, vid, "make the plan")
    assert reply["kind"] == "text" and card_of(reply) is None
    assert "credit" not in reply["body_md"].lower() and "m1" not in reply["body_md"]
    assert plans["starts"] == 1
    repeated = chat(client, vid, "make the plan")
    assert card_of(repeated) is None and plans["starts"] == 1


def test_generate_reprices_when_ceiling_changed(venture, plans):
    client, vid, _ = venture
    plans["quote"] = {**QUOTE, "ceiling_micro": 900_000}
    confirm(client, vid, "plan.generate", {"ceiling_micro": 250_000})
    assert plans["starts"] == 0


def test_changed_approved_plan_opens_a_fresh_quote_instead_of_an_approved_plan_loop(venture, plans, monkeypatch):
    from app.chat import build_path
    from app.planning import compiler

    client, vid, _ = venture
    plans["plans"] = [{"id": "pln_old", "state": "approved", "version": 1}]
    monkeypatch.setattr(compiler, "get_plan", lambda plan_id: {"stale_dependencies": True})
    action = build_path.reply(vid, [("plan.generate", "Update the build plan")])["actions"][0]
    reply = confirm(client, vid, action["action"], action["payload"])
    assert reply.status_code == 200, reply.text
    assert card_of(reply.json()) is None and plans["starts"] == 1
    assert "credit" not in reply.json()["body_md"].lower()


@pytest.mark.parametrize("venture", ["Inventory alerts for warehouse operators", "Walking groups for neighborhood families"], indirect=True)
def test_inventory_missing_asks_then_chains_to_generate_card(venture, plans, monkeypatch):
    client, vid, _ = venture
    from app.planning import resources

    plans["quote"] = ValueError("plan_resource_inventory_required")
    reply = chat(client, vid, "make the plan")
    assert card_of(reply) is None
    assert any(a.get("action") == "plan.resources_none" for a in reply["actions"])
    assert {"question": True} in reply["citations"] and "next_moves" not in reply
    assert "one tap" not in reply["body_md"].lower()
    assert "tell me you have no existing resources" in reply["body_md"]
    assert "?" in reply["body_md"]

    saved = []
    update = resources.update_inventory
    def record_inventory(w, v, assets, complete):
        saved.append((assets, complete))
        return update(w, v, assets, complete)
    monkeypatch.setattr(resources, "update_inventory", record_inventory)
    plans["quote"] = dict(QUOTE)
    reply = chat(client, vid, "I don't have any tools")
    assert saved == [([], True)] and card_of(reply) is None and plans["starts"] == 1


def test_button_click_resources_none_needs_no_card(venture, plans, monkeypatch):
    client, vid, _ = venture
    from app.planning import resources

    r = confirm(client, vid, "plan.resources_none", {})
    assert r.status_code == 200 and "Plan generation is running" in r.json()["body_md"]
    assert card_of(r.json()) is None and plans["starts"] == 1
    assert resources.snapshot(venture[2], vid)["inventory"]["complete"] is True


def test_quote_shows_ceiling_and_model_inline(venture, plans):
    client, vid, _ = venture
    reply = chat(client, vid, "how much will the plan cost?")
    assert 'credit' not in reply['body_md'].lower() and 'm1' not in reply['body_md'] and ('openai' not in reply['body_md'])
    assert card_of(reply) is None and plans["starts"] == 0


@pytest.mark.parametrize("venture", ["Inventory alerts for warehouse operators", "Walking groups for neighborhood families"], indirect=True)
@pytest.mark.parametrize("understood", [False, True])
def test_founder_without_research_gets_honest_next_step(venture, understood, request):
    from app.core.local_mode import is_local

    client, vid, _ = venture
    plans = request.getfixturevalue("plans") if is_local() else None
    if understood:
        from tests.unit.chat.test_assistant_routing import confirm_idea

        confirm_idea(vid)
    reply = chat(client, vid, "make the plan")
    assert card_of(reply) is None
    if is_local():
        assert reply["kind"] == "text"
        assert "Plan generation is running" in reply["body_md"]
        assert plans["starts"] == 1
        return
    assert "recommended, not mandatory" in reply["body_md"] and "Nothing was started" in reply["body_md"]
    if understood:
        assert reply["kind"] == "navigation" and "Confirm a jump" in reply["body_md"]
        assert reply["actions"][0]["action"] == "journey.jump_to"
        assert [action["action"] for action in reply["actions"] if action.get("style") == "primary"] == ["journey.jump_to"]
        assert any(a.get("action") in {"idea.review", "research"} for a in reply["actions"])
    else:
        assert not any(a.get("action") == "journey.jump_to" for a in reply["actions"])
        assert not any(a.get("action") in {"idea.review", "research"} for a in reply["actions"])
        assert not any(a.get("href") for a in reply["actions"])
        assert "Confirm a jump" not in reply["body_md"]
        assert "Explain in detail" in reply["body_md"]
        assert any(a.get("action") == "blocker.explain" for a in reply["actions"])


def test_insufficient_credits_offers_no_confirm(venture, plans):
    client, vid, _ = venture
    plans["quote"] = {**QUOTE, "sufficient_now": False}
    reply = chat(client, vid, "generate the plan")
    assert card_of(reply) is None and "usage limit" in reply["body_md"]
    assert "credit" not in reply["body_md"].lower()


def test_approve_card_summary_confirm_then_next_work_item(venture, plans, monkeypatch):
    client, vid, _ = venture
    from app.chat import proposals
    from app.planning import compiler
    from app.planning.store import enqueue_task

    plans["plans"] = [{"id": "pln_1", "state": "founder_review", "version": 1}]
    monkeypatch.setattr(proposals, "prepare_plan_approval", lambda v, a, p: {"id": "cpr_1"})
    monkeypatch.setattr(compiler, "get_plan", lambda pid: {
        "milestones": [{"title": t} for t in ["Landing", "Waitlist", "Outreach", "Pilot"]],
        "evidence_gaps": ["x"], "out_of_scope": [], "conditional": [], "estimate": {"total_upfront_credits": None}})
    confirmed = []
    monkeypatch.setattr(proposals, "confirm", lambda v, a, p: confirmed.append(p) or {"state": "succeeded"})

    task = enqueue_task(vid, {"title": "Write the value proposition", "runnable": True, "tags": ["plan_task:t1"]},
                        source="approved_plan:pln_1")
    done = chat(client, vid, "approve the plan")
    assert confirmed == ["cpr_1"]
    assert done["body_md"].split("-->\n", 1)[-1].startswith("Plan approved. Next: **Write the value proposition**")
    assert card_of(done)["action"] == "work.start" and card_of(done)["resulting_state"] == {"task_id": task["id"]}


def test_negated_approval_creates_no_card_and_no_proposal(venture, plans, monkeypatch):
    client, vid, _ = venture
    from app.chat import proposals

    plans["plans"] = [{"id": "pln_1", "state": "founder_review", "version": 1}]
    monkeypatch.setattr(proposals, "prepare_plan_approval", lambda *a: pytest.fail("prepared"))
    reply = chat(client, vid, "don't approve the plan yet")
    assert card_of(reply) is None


def test_approve_with_no_plan_is_honest(venture, plans):
    client, vid, _ = venture
    reply = chat(client, vid, "approve the plan")
    assert card_of(reply) is None and "No plan is saved yet" in reply["body_md"]


def test_next_and_named_task_cards_and_founder_only_item(venture):
    client, vid, _ = venture
    from app.planning.store import enqueue_task

    assert card_of(chat(client, vid, "start the next task")) is None
    second = enqueue_task(vid, {"title": "Draft the volunteer FAQ", "runnable": True, "tags": ["plan_task:t1"]},
                          source="approved_plan:pln_1")
    first = enqueue_task(vid, {"title": "Interview five volunteers", "runnable": False, "tags": ["plan_task:t0"]},
                         source="approved_plan:pln_1")  # newest enqueue ranks first
    reply = chat(client, vid, "what's the next task?")
    assert card_of(reply) is None and "yourself" in reply["body_md"] and first["id"]
    named = chat(client, vid, "start the volunteer FAQ")
    assert card_of(named)["resulting_state"] == {"task_id": second["id"]}


@pytest.mark.hosted
def test_managed_build_task_explains_review_step(venture):
    client, vid, _ = venture
    from app.planning.store import enqueue_task

    task = enqueue_task(vid, {"title": "Build the app", "runnable": False,
                              "tags": ["capability:build.run", "plan_task:ptk_9"]}, source="approved_plan:pln_1")
    done = confirm(client, vid, "work.start", {"task_id": task["id"]}).json()
    assert "approve its exact budget" in done["body_md"]
    assert done["actions"][0]["action"] == "status"  # chat-first: Builds is shown in chat


def test_voice_cannot_confirm_plan_generation(venture, plans):
    client, vid, _ = venture
    r = confirm(client, vid, "plan.generate", {"ceiling_micro": 250_000}, source="voice")
    assert r.status_code >= 400 and plans["starts"] == 0


def test_milestone_posts_offer_the_chat_actions(venture, monkeypatch):
    client, vid, _ = venture
    from app.chat import milestone_hooks, store
    from app.planning import compiler, execution

    monkeypatch.setattr(execution, "identifier", lambda j: "pln_1")
    monkeypatch.setattr(compiler, "get_plan", lambda pid: {"proposal": {"milestones": [{}]}, "estimate": {}})
    milestone_hooks.plan_done({"_job_id": "job_1", "venture_id": vid})
    post = [m for m in store.list_messages(vid) if m["kind"] == "plan_ready"][-1]
    assert post["actions"][0] == {"label": "Show status here", "action": "status", "style": "primary"}  # Execution page is hidden
    assert post["actions"][1]["action"] == "plan.approve"
    assert "Nothing has run" not in post["body_md"]
    assert "Background preparation" in post["body_md"]


@pytest.mark.parametrize("unsettled", [False, True])
def test_failed_plan_milestone_offers_recovery_not_approval(venture, monkeypatch, unsettled):
    _, vid, _ = venture
    from app.chat import milestone_hooks, store
    from app.core.local_mode import is_local
    from app.planning import compiler, execution
    recovery = {"message": "Use saved research while the charge is resolved." if unsettled else "Review a new estimate to retry.",
                "label": "Review saved research" if unsettled else "Retry plan",
                "href": f"/v/{vid}/research" if unsettled else f"/v/{vid}/plan#plan-create"}
    monkeypatch.setattr(execution, "identifier", lambda job: "pln_failed")
    monkeypatch.setattr(compiler, "get_plan", lambda pid: {"generation_state": "failed", "generation_recovery": recovery})
    milestone_hooks.plan_done({"_job_id": "job_failed", "venture_id": vid})
    post = [message for message in store.list_messages(vid) if message["kind"] == "needs_you"][-1]
    assert recovery["message"] in post["body_md"]
    if is_local() and unsettled:
        assert post["actions"][0]["href"] == recovery["href"]
    else:
        assert post["actions"][0]["action"] == ("research" if unsettled else "status")
    assert not any(action.get("action") == "plan.approve" for action in post["actions"])
    assert "ready" not in post["body_md"]


def test_general_reply_offers_only_the_next_step_button(monkeypatch):
    import asyncio
    from app.chat.handlers import general
    from app.ventures import next_step as ns

    def step(action, href="/v/v1/plan"):
        return lambda vid: {"cta_label": "Go", "chat_action": action, "href": href}

    monkeypatch.setattr(ns, "next_step", step("plan.generate"))
    assert general._next_step_actions("v1") == [{"label": "Go", "action": "plan.generate", "style": "primary"}]
    monkeypatch.setattr(ns, "next_step", step("work.start"))
    assert general._next_step_actions("v1") == [{"label": "Go", "href": "/v/v1/plan", "style": "primary"}]
    monkeypatch.setattr(ns, "next_step", lambda vid: 1 / 0)
    assert general._next_step_actions("v1") == []


NATIVE_REVISION = (
    "Revise the existing Kennel Scheduler landing page through a new native subscription coding plan. "
    "Use honest concept copy without claiming customers or a working product. "
    "Use the coding.subscription_static capability with installed Codex, no paid builder/model runtime. "
    "Prepare a reviewed plan before any execution or publication.")


def test_native_revision_with_constraints_is_one_read_only_intent():
    from app.chat.intent import classify_intent, split_speech_acts
    assert classify_intent(NATIVE_REVISION) == "plan_revision"
    assert split_speech_acts(NATIVE_REVISION) == [("plan_revision", NATIVE_REVISION)]
    for text in (
        "Do not revise the native subscription coding plan yet.",
        "Is the coding.subscription_static landing plan already revised?",
        "Revise the coding.subscription_static plan. Do not prepare a new plan.",
    ):
        assert classify_intent(text) == "assistant"


def test_native_plan_revision_starts_directly_in_chat(venture, plans):
    client, vid, _ = venture
    plans["plans"] = [{"id": "approved-old", "state": "approved", "version": 1}]
    seen = {}
    from app.planning import compiler
    quote = compiler.generation_quote
    compiler.generation_quote = lambda *a, **k: seen.update(k) or quote(*a, **k)
    try:
        reply = chat(client, vid, NATIVE_REVISION)
        assert reply["kind"] == "text" and card_of(reply) is None
        assert "Plan page" not in reply["body_md"] and "credit" not in reply["body_md"].lower()
        assert seen["base_plan_id"] == "approved-old"
        assert plans["starts"] == 1 and "revision is running" in reply["body_md"]
    finally:
        compiler.generation_quote = quote


@pytest.mark.parametrize("instruction", [
    "Update the plan: prioritize inventory checks for shipping teams.",
    "Revise my plan to prioritize seasonal menus for home cooks.",
])
def test_local_plan_revision_uses_planner_not_coding_tool(request, monkeypatch, instruction):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    client, vid, _ = request.getfixturevalue("venture")
    plans = request.getfixturevalue("plans")
    from app.planning import compiler

    monkeypatch.setattr("app.planning.execute.start_planned_task", lambda *_: pytest.fail("started coding work"))
    missing = chat(client, vid, instruction)
    assert "don't have a saved plan to update" in missing["body_md"] and plans["starts"] == 0
    plans["plans"] = [{"id": "approved-old", "state": "approved", "version": 1}]
    original = compiler.start
    started = []

    def start(*args, **options):
        started.append(options)
        return original(*args, **options)

    monkeypatch.setattr(compiler, "start", start)
    revised = chat(client, vid, instruction)
    assert started == [{"base_plan_id": "approved-old", "instruction": instruction}]
    assert "Plan revision is running" in revised["body_md"] and card_of(revised) is None

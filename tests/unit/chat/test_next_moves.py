import asyncio
import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from tests.company_os_harness import append_idea_revision, local_chat as local_chat
from tests.unit.chat.test_proactive import NOON, _messages, _user_said, env as env_fixture

@pytest.fixture
def env(env_fixture, monkeypatch):
    from app.core.local_mode import is_local
    from tests.idea_fixtures import install_provider

    if is_local():
        with monkeypatch.context() as provider_patch:
            install_provider(provider_patch)
    return env_fixture


DOMAINS = ["Inventory alerts for warehouse operators", "Walking groups for neighborhood families"]


def _finish_intake(venture_id):
    from app.core.local_mode import is_local
    from app.db.models import Venture
    from app.state import db
    from app.ventures.current_idea import current_revision, revision_fields
    from tests.unit.test_autonomous_experiments import confirm_saved_idea

    confirm_saved_idea(venture_id)
    if is_local():
        with db()() as session:
            revision = current_revision(session.get(Venture, venture_id), session)
            fields = {**revision_fields(revision), "_questions_skipped": True}
            append_idea_revision(session, revision.id, structured=json.dumps(fields))
            session.commit()


@pytest.mark.parametrize("idea", DOMAINS)
def test_first_landing_recommendation_starts_from_current_saved_idea(env, monkeypatch, idea):
    from fastapi import HTTPException
    from app.builder import ctl
    from app.chat import build_moves, execute, product_build_request, site_actions
    from app.pagebuilder import api

    _, venture_id, _ = env
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(ctl, "local_tool", lambda *_args, **_kwargs: "codex")
    monkeypatch.setattr(product_build_request, "local_product_provider", lambda *_: "codex")
    monkeypatch.setattr(site_actions, "_local_page_setup", lambda _: None)
    monkeypatch.setattr(api, "estimate", lambda *_: {"ready": True, "quote_fingerprint": "before-refresh"})
    monkeypatch.setattr(api, "profile", lambda *_: {"current": None})
    move = build_moves.recommendation(venture_id, {**_saved_state(idea), "idea": {"confirmed": True}, "page": False})
    assert move["title"] == "Build your landing page with Codex"

    def admit(venture, actor, *, expected_quote=None, provider, instruction, **_):
        assert venture == venture_id and provider == "codex" and instruction == move["payload"]["query"]
        if expected_quote is not None and expected_quote != "after-refresh":
            raise HTTPException(409, detail={"code": "pagebuilder_quote_changed"})
        return {"state": "queued", "job_id": "first_page"}

    monkeypatch.setattr(api, "_start", admit)
    monkeypatch.setattr("app.autopilot.receipts.queued_job", lambda _: None)
    result = execute._build_landing_page(venture_id, move["payload"], "owner")
    assert result["run_id"] == "first_page" and result["kind"] != "confirmation"
    with pytest.raises(HTTPException, match="pagebuilder_quote_changed"):
        execute._build_landing_page(venture_id, {**move["payload"], "quote_fingerprint": "stale", "ceiling_micro": 1}, "owner")


@pytest.mark.parametrize("idea", DOMAINS)
@pytest.mark.parametrize("published", [None, "https://saved.example"])
def test_ready_page_next_moves_keep_preview_and_export(env, monkeypatch, idea, published):
    from app.chat import actions, next_moves
    from app.db.models import Job, Venture
    from app.pagebuilder import api
    from app.state import db
    from core.clock import utcnow

    _, venture_id, _ = env
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    state = {**_saved_state(idea), "idea": {"confirmed": True}, "questions_pending": False,
             "page": True, "page_build": "ready", "published": published}
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: state)
    monkeypatch.setattr(actions, "disabled_reason", lambda _: None)
    monkeypatch.setattr(next_moves, "_fresh_recommendation", lambda _: None)
    monkeypatch.setattr(next_moves, "_customer_calls_offer", lambda *_: None)
    monkeypatch.setattr(next_moves, "experiments_autopilot_enabled", lambda: False)
    with db()() as session:
        workspace_id = session.get(Venture, venture_id).workspace_id
        session.add(Job(id="saved", kind=api.KIND, venture_id=venture_id, workspace_id=workspace_id,
                        status="succeeded", created_at=utcnow(), payload_json='{"_runtime":{"state":"ready"}}'))
        session.commit()
    monkeypatch.setattr(api, "_candidate_current", lambda _: True)
    card = next_moves.compose(venture_id, requested=True)
    prefix = f"/api/ventures/{venture_id}/pagebuilder/jobs/saved"
    assert [(action["label"], action["href"]) for action in card["actions"] if action.get("href", "").startswith(prefix)] == [
        ("Open preview", prefix + "/preview"), ("Export", prefix + "/source")]
    assert all(action.get("style") == "primary" for action in card["actions"] if action.get("href", "").startswith(prefix))
    assert not any(action.get("confirmation") for action in card["actions"])


@pytest.mark.parametrize("idea", DOMAINS)
def test_local_chat_reuses_only_the_latest_identical_recommendation(env, monkeypatch, idea):
    from fastapi import BackgroundTasks
    from app.api.chat_api import get_chat
    from app.chat import actions, next_moves, site_actions
    from app.chat.store import append_message, list_messages

    _, venture_id, clock = env
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    state = {**_saved_state(idea), "idea": {"confirmed": True}, "questions_pending": False,
             "page": True, "page_build": "ready"}
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: state)
    monkeypatch.setattr(actions, "disabled_reason", lambda _: None)
    monkeypatch.setattr(next_moves, "_fresh_recommendation", lambda _: None)
    monkeypatch.setattr(next_moves, "_customer_calls_offer", lambda *_: None)
    monkeypatch.setattr(next_moves, "experiments_autopilot_enabled", lambda: False)
    monkeypatch.setattr(site_actions, "ready_page_actions", lambda _: [])

    card = next_moves.compose(venture_id, requested=True)

    def save(key, moves, *, body=card["body_md"]):
        clock["now"] += timedelta(seconds=1)
        return append_message(venture_id, role="assistant", kind=card["kind"], body_md=body,
                              actions=moves, idempotency_key=key)

    _user_said(venture_id, NOON - timedelta(hours=1))
    first = save("initial-next-step", card["actions"])
    append_message(venture_id, role="assistant", body_md="Your work is saved.")
    repeat = save("next-step-after-action", [{**move, "state_hash": "new-hash"} for move in card["actions"]])
    assert repeat == first
    assert len(_messages(venture_id, next_moves.KIND)) == 1

    changed = [{**card["actions"][0], "href": "/settings/local#publishing-details"}, *card["actions"][1:]]
    assert save("updated-actions", changed)["id"] != first["id"]
    assert save("changed-back", card["actions"])["id"] != first["id"]
    save("updated-text", card["actions"], body="Your draft is also saved. " + card["body_md"])
    assert len(_messages(venture_id, next_moves.KIND)) == 4

    reason = card["body_md"].split("Why: ", 1)[1].split("\n", 1)[0]
    assert "sign up" in reason and "Settings" not in reason
    assert card["actions"][0]["href"] == "/settings/local#publishing-title"
    history = list_messages(venture_id)
    clock["now"] += timedelta(hours=2)
    background_tasks = BackgroundTasks()
    assert [message["id"] for message in get_chat(venture_id, background_tasks)["items"]] == [
        message["id"] for message in history]
    asyncio.run(background_tasks())
    assert list_messages(venture_id) == history


@pytest.mark.parametrize("idea", DOMAINS)
def test_failed_requested_page_keeps_retry_ahead_of_research(env, monkeypatch, idea):
    from app.builder import ctl
    from app.chat import actions, next_moves
    from tests.unit.test_autonomous_experiments import confirm_saved_idea

    _, venture_id, _ = env
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    confirm_saved_idea(venture_id)
    state = {**_saved_state(idea), "idea": {"confirmed": True, "idea_summary": idea},
             "idea_state": "confirmed", "page": False, "page_build": "failed",
             "priority_step": {"key": "research.start", "stage": "Your priority",
                               "title": "Research your market", "detail": "Review your audience.",
                               "cta_label": "Research", "href": f"/v/{venture_id}/research"}}
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: state)
    monkeypatch.setattr(ctl, "local_tool", lambda *_args, **_kwargs: "codex")
    monkeypatch.setattr(actions, "disabled_reason", lambda _: None)
    card = next_moves.compose(venture_id, requested=True)
    assert card["actions"][0]["action"] == "pagebuilder.build"
    assert card["actions"][0]["label"] == "Retry your landing page with Codex"
    assert card["actions"][0]["payload"]["prepare"] is True
    assert "confirmation" not in card["actions"][0]


@pytest.mark.parametrize("idea", DOMAINS)
@pytest.mark.parametrize("report_ready", [False, True])
def test_ordered_research_priority_precedes_unsolicited_build_advice(env, monkeypatch, idea, report_ready):
    from app.chat import build_moves, next_moves
    from app.ventures import next_step

    _, venture_id, _ = env
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    facts = {"idea_confirmed": True, "personal": False, "research_report_ready": report_ready,
             "dossier_state": "partial", "startup_review": {"complete": True, "question": None,
             "priorities": ["market_insights", "outreach_leads"]}}
    priority = next_step._founder_resolve(venture_id, f"/v/{venture_id}", facts)
    state = {**_saved_state(idea), "idea": {"confirmed": True, "idea_summary": idea},
             "idea_state": "confirmed", "priority_step": priority,
             "priorities": ["Research the market", "Design customer interviews"]}
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: state)
    monkeypatch.setattr(build_moves, "recommendation", lambda *_: {
        "id": "build:unrequested", "title": "Build your landing page with Codex",
        "why": "Your idea is confirmed.", "action": "pagebuilder.build", "payload": {}})
    card = next_moves.compose(venture_id, requested=True)
    assert card["body_md"].startswith("Your research is ready." if report_ready else "Research your market.")
    assert card["actions"][0]["href"] == f"/v/{venture_id}/research"
    assert "landing page" not in card["body_md"]
    assert not any(action.get("action") in {"pagebuilder.build", "build.tool.start"} for action in card["actions"])


@pytest.mark.parametrize("idea", DOMAINS)
@pytest.mark.parametrize("signal", ["confirmed", "research", "page", "demand", "demo", "bug"])
@pytest.mark.parametrize("connected", [True, False])
def test_local_build_timing_uses_saved_signals_and_allowed_tool(env, monkeypatch, idea, signal, connected):
    from fastapi import HTTPException
    from app.builder import ctl
    from app.chat import actions, customers, next_moves
    from app.db.models import ChatMessage, GtmContact, GtmTouch, Venture
    from app.db.supymem_models import Experiment
    from app.experiments import ledger
    from app.state import db
    from tests.unit.test_autonomous_experiments import confirm_saved_idea

    _, venture_id, _ = env
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    confirm_saved_idea(venture_id)
    state = {**_saved_state(idea), "idea": {"confirmed": True, "idea_summary": idea},
             "idea_state": "confirmed", "questions_pending": False,
             "research": {"present": signal == "research"}, "page": signal in {"demand", "demo", "bug"},
             "product": signal == "bug", "plan_state": "approved"}
    with db()() as session:
        workspace_id = session.get(Venture, venture_id).workspace_id
        direction = ledger.current_direction(session, venture_id)
        if signal in {"page", "demand"}:
            session.add(Experiment(id="build_signal", workspace_id=workspace_id, venture_id=venture_id,
                kind=ledger.KIND, hypothesis=f"People want {idea}", action="Count paid signups",
                state="proposed" if signal == "page" else "concluded",
                outcome=None if signal == "page" else "supported", ended_at=NOON if signal == "demand" else None,
                context=json.dumps({"direction": direction, "ran": signal == "demand",
                    "result_text": "Six people paid" if signal == "demand" else "",
                    "spec": {"scan": "landing" if signal == "page" else "demand"}})))
        if signal == "demo":
            session.add(GtmContact(id="build_contact", workspace_id=workspace_id,
                stage_changed_at=NOON, created_at=NOON))
            session.flush()
            session.add(GtmTouch(id="build_demo", workspace_id=workspace_id, venture_id=venture_id,
                contact_id="build_contact", direction="in", channel="email", simulated=0,
                classification="interested", body_text=f"Can you show me a demo of {idea}?", created_at=NOON))
        if signal == "bug":
            session.add(ChatMessage(id="build_bug", workspace_id=workspace_id, venture_id=venture_id,
                role="user", body_md=f"I found a bug in {idea}: saving changes crashes the app.", created_at=NOON))
        session.commit()
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: state)
    monkeypatch.setattr(next_moves, "_blocked_actions", lambda _: {"research.prepare"})
    monkeypatch.setattr(next_moves, "_fresh_recommendation", lambda _: None)
    monkeypatch.setattr(next_moves, "experiment_learning", lambda _: None)
    monkeypatch.setattr(next_moves, "_customer_calls_offer", lambda *args: None)
    monkeypatch.setattr(next_moves, "experiments_autopilot_enabled", lambda: False)
    monkeypatch.setattr(customers, "saved_search_request", lambda _: "")
    monkeypatch.setattr(actions, "disabled_reason", lambda _: None)
    started = []
    monkeypatch.setattr(ctl, "request_local_build", lambda *args, **kwargs: started.append((args, kwargs)))

    def tool(*args, **kwargs):
        if not connected:
            raise HTTPException(409, detail={"message": "Open Settings > Keys and connections > Coding tools, connect your tool and allow Charlie, then retry."})
        return "claude"

    monkeypatch.setattr(ctl, "local_tool", tool)
    card = next_moves.compose(venture_id, requested=True)
    move = card["actions"][0]
    assert not started
    if not connected:
        assert move["label"] == "Connect your coding tool"
        assert move["href"] == "/settings/local#coding-tools"
        assert "Keys and connections > Coding tools" in card["body_md"]
    else:
        label = ("Fix this" if signal == "bug" else "Build a first version" if signal in {"demand", "demo"}
                 else "Build your landing page")
        assert move["label"] == label + " with Claude Code"
        assert move["action"] == ("pagebuilder.build" if signal in {"confirmed", "research", "page"} else "build.tool.start")
        assert "claude" in json.dumps(move["payload"]).lower()
        assert move["payload"].get("repair", False) is (signal == "bug")
        if signal == "bug":
            from app.chat.build_moves import is_repair_request

            assert is_repair_request(move["payload"]["text"])
            assert not is_repair_request("Do not " + move["payload"]["text"])
            assert not is_repair_request(move["payload"]["text"] + "x" * 10000)
    assert "confirmation" not in move
    if signal == "bug":
        state.update(idea={"confirmed": False, "idea_summary": idea}, idea_state="clarifying",
                     reading_idea=True, review_ready=True, questions_pending=True)
        assert next_moves.compose(venture_id, requested=True)["actions"][0]["label"] == move["label"]


@pytest.mark.parametrize("idea", DOMAINS)
def test_local_build_timing_product_click_starts_the_connected_tool_without_confirmation(env, monkeypatch, idea):
    from app.builder import ctl
    from app.chat import build_moves, execute, product_build_request
    from app.core import local_owner
    from app.db.models import Venture
    from app.db.supymem_models import Experiment
    from app.experiments import ledger
    from app.state import db
    from app.workspaces.resolve import local_person
    from tests.unit.test_autonomous_experiments import confirm_saved_idea

    _, venture_id, _ = env
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    confirm_saved_idea(venture_id)
    with db()() as session:
        workspace_id = session.get(Venture, venture_id).workspace_id
        direction = ledger.current_direction(session, venture_id)
        session.add(Experiment(id="click_demand", workspace_id=workspace_id, venture_id=venture_id,
            kind=ledger.KIND, hypothesis=f"People pay for {idea}", state="concluded", outcome="supported",
            ended_at=NOON, context=json.dumps({"direction": direction, "ran": True,
                "result_text": "Six people paid", "spec": {"scan": "demand"}})))
        session.commit()
    monkeypatch.setattr(ctl, "local_tool", lambda *args, **kwargs: "claude")
    move = build_moves.recommendation(venture_id, {**_saved_state(idea), "idea": {"confirmed": True}, "page": True})
    monkeypatch.setattr(product_build_request, "local_product_provider", lambda *args: "claude")
    calls = []

    def start(venture, text, **kwargs):
        calls.append((venture, text, kwargs))
        return {"id": "requested_build", "state": "approved"}

    monkeypatch.setattr(ctl, "request_local_build", start)
    owner = local_person()
    token = local_owner._OWNER.set(owner)
    try:
        result = execute._start_tool_build(venture_id, move["payload"], owner.id)
    finally:
        local_owner._OWNER.reset(token)
    assert result["action_started"] is True and result["kind"] == "build_tool_started"
    assert len(calls) == 1 and calls[0][:2] == (venture_id, move["payload"]["text"])
    assert calls[0][2]["provider"] == "claude"
    assert not any(action.get("confirmation") for action in result["actions"])


@pytest.mark.parametrize("idea", DOMAINS)
def test_local_build_timing_ignores_unsaved_stale_or_sample_demand(env, monkeypatch, idea):
    from app.builder import ctl
    from app.chat import build_moves
    from app.db.models import Venture, VentureDocument
    from app.db.supymem_models import Experiment
    from app.experiments import ledger
    from app.state import db
    from tests.unit.test_autonomous_experiments import confirm_saved_idea

    _, venture_id, clock = env
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    confirm_saved_idea(venture_id)
    state = {**_saved_state(idea), "idea": {"confirmed": True}, "page": True}
    with db()() as session:
        workspace_id = session.get(Venture, venture_id).workspace_id
        direction = ledger.current_direction(session, venture_id)
        session.add(Experiment(id="demand_check", workspace_id=workspace_id, venture_id=venture_id,
                              kind=ledger.KIND, hypothesis=f"People will pay for {idea}"))
        session.add(Experiment(id="demand_recheck", workspace_id=workspace_id, venture_id=venture_id,
                              kind=ledger.KIND, hypothesis=f"People will pay for {idea}"))
        session.commit()
    row = {"id": "demand_check", "hypothesis": f"People will pay for {idea}",
           "direction": direction, "state": "concluded", "outcome": "supported",
           "ended_at": NOON, "ran": True, "result_text": "Six paid signups", "spec": {"scan": "demand"}}
    monkeypatch.setattr(ctl, "local_tool", lambda *args, **kwargs: "codex")
    for update in ({"sample": True}, {"direction": "previous_revision"}, {"outcome": "refuted"},
                   {"state": "running"}, {"ended_at": NOON + timedelta(days=1)},
                   {"ran": False}, {"result_text": ""}, {"id": "another_workspace"}):
        monkeypatch.setattr(ledger, "evidence_for_venture", lambda _, update=update: [{**row, **update}])
        assert build_moves.recommendation(venture_id, state) is None, update
    monkeypatch.setattr(ledger, "evidence_for_venture", lambda _: [row])
    assert build_moves.recommendation(venture_id, state)["title"] == "Build a first version with Codex"
    assert build_moves.recommendation(venture_id, {**state, "paused": True}) is None
    assert build_moves.recommendation(venture_id, {**state, "idea": {"confirmed": False}}) is None
    assert build_moves.recommendation(venture_id, {**state, "product": True}) is None
    clock["now"] += timedelta(hours=1)
    monkeypatch.setattr(ledger, "evidence_for_venture", lambda _: [row, {**row, "id": "demand_recheck",
        "outcome": "refuted", "ended_at": NOON + timedelta(seconds=1)}])
    assert build_moves.recommendation(venture_id, state) is None
    monkeypatch.setattr(ledger, "evidence_for_venture", lambda _: [row])
    with db()() as session:
        session.add(VentureDocument(id="active_coding", venture_id=venture_id, kind="device_work_order_v1",
            title="Requested build", created_at=NOON, updated_at=NOON, provenance_json=json.dumps({
                "state": "approved", "expires_at": (clock["now"] + timedelta(minutes=5)).isoformat(),
                "manifest": {"task_id": "requested_build"}})))
        session.commit()
    assert build_moves.recommendation(venture_id, state) is None
    clock["now"] += timedelta(minutes=6)
    assert build_moves.recommendation(venture_id, state)["title"] == "Build a first version with Codex"


@pytest.mark.parametrize("idea", DOMAINS)
def test_local_copy_recovery_links_survive_intake_without_allowing_unsafe_navigation(monkeypatch, idea):
    from types import SimpleNamespace
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    from app.chat import next_moves

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    card = {"intake_pending": True, "first_question_pending": True, "actions": [], "idea": idea}
    action = {"label": "Open Connections", "href": "/connections?venture=ven_fixture", "kind": "recovery_link"}
    bad = [{**action, "href": "https://outside.example/settings"}, {**action, "payload": {"run": True}}]
    assert next_moves.intake_actions(card, [action, *bad]) == [action]
    monkeypatch.setattr(next_moves, "compose", lambda *args, **kwargs: card)
    row = SimpleNamespace(kind="text", citations_json="[]", actions_json=json.dumps([action]))
    assert next_moves.attach("ven_fixture", row, request="Show saved content status") is False
    assert json.loads(row.actions_json) == [action]


@pytest.mark.parametrize("idea", DOMAINS)
def test_no_ready_local_moves_returns_the_exact_setup_step(monkeypatch, idea):
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    from app.chat import actions, customers, next_moves
    from app.core.local_capabilities import blocked_message
    from app.core.providers_local import clear_settings
    from app.ventures import stage

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    clear_settings()
    state = {**_saved_state(idea), "idea": {"confirmed": True}, "questions_pending": False}
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: state)
    monkeypatch.setattr(stage, "get_stage", lambda _: {"stage": "idea", "label": "Shaping your idea"})
    monkeypatch.setattr(customers, "saved_search_request", lambda *args: "")
    monkeypatch.setattr(next_moves, "_blocked_actions", lambda _: {"ceo_review", "research.prepare"})
    monkeypatch.setattr(next_moves, "_fresh_recommendation", lambda _: None)
    monkeypatch.setattr(next_moves, "_customer_calls_offer", lambda *args: None)
    monkeypatch.setattr(next_moves, "experiments_autopilot_enabled", lambda: False)
    reason = blocked_message("find_customers")
    monkeypatch.setattr(actions, "disabled_reason", lambda _: reason)

    reply = next_moves.compose("venture")
    assert reply["body_md"] == reason
    assert reply["actions"] == [{"label": "Keys and connections", "href": "/settings/local", "style": "primary"}]
    assert "Your AI provider" in reason


@pytest.mark.parametrize("mode", ["hosted", "local"])
@pytest.mark.parametrize("idea", DOMAINS)
def test_local_intake_keeps_saved_work_status_without_enabling_work(monkeypatch, idea, mode):
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    from app.chat.next_moves import intake_actions

    monkeypatch.setenv("SUPYMEM_MODE", mode)
    status = {"label": f"Review your saved work for {idea}", "action": "status", "style": "primary"}
    actions = [status, {"action": "outreach.draft"}, {"action": "gtm.send"}]
    card = {"intake_pending": True, "actions": []}
    assert intake_actions(card, actions) == [status]
    assert intake_actions({**card, "blocked_actions": ["status"]}, actions) == []


@pytest.mark.parametrize("mode", ["hosted", "local"])
@pytest.mark.parametrize("idea", DOMAINS)
def test_missing_audience_next_step_has_no_empty_alternatives_or_draft_dead_end(env, monkeypatch, idea, mode):
    from app.chat import actions, next_moves

    monkeypatch.setenv("SUPYMEM_MODE", mode)
    state = {**_saved_state(idea), "idea": {"confirmed": True}, "page": True,
             "icp": {"who": "", "needs_input": True}}
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: state)
    monkeypatch.setattr(next_moves, "_blocked_actions", lambda _: set())
    monkeypatch.setattr(next_moves, "_fresh_recommendation", lambda _: None)
    monkeypatch.setattr(next_moves, "experiment_learning", lambda _: None)
    monkeypatch.setattr(next_moves, "_customer_calls_offer", lambda *args: None)
    monkeypatch.setattr(next_moves, "experiments_autopilot_enabled", lambda: False)
    monkeypatch.setattr(actions, "disabled_reason", lambda action: None if action in {"gtm.find", "outreach.draft"} else "disabled")
    reply = next_moves.compose(env[1], requested=True)
    if mode == "local":
        assert reply["actions"][0]["action"] == "gtm.find"
        assert not any(action["action"] == "outreach.draft" for action in reply["actions"])
        assert next_moves._CAPABILITY_HEADING not in reply["body_md"]
        assert "audience" in reply["body_md"] and "Why:" in reply["body_md"]
    else:
        assert reply["actions"][0]["action"] == "outreach.draft"
        assert "Prepare messages you can review before anything is sent." in reply["body_md"]
        assert next_moves._CAPABILITY_HEADING in reply["body_md"]
    from app.clarify.dilemmas import ground_question

    audience = idea + " who already have a prescribed plan and must not be given professional advice or asked for private records"
    snapshot = {"answered_fields": {"customer": audience}, "_answered_fields": {"customer": audience}}
    question = ground_question({"field": "workflow", "text": "What task should your product help " + audience + " do first?"}, snapshot)
    if mode == "local":
        assert audience not in question["text"] and "your audience" in question["text"]
    else:
        assert question["text"] == "What task should your product help " + audience + " do first?"
    assert snapshot["answered_fields"]["customer"] == audience


@pytest.mark.parametrize("hypothesis,report,learning,sample_size", [
    ("Your quotation helper has no interviews with print shop estimators yet.",
     "I interviewed seven print shop estimators about preparing job quotes.",
     "You heard from seven estimators, but still need examples of quote revisions.", 7),
    ("Your birdwatching journal is only a concept with nobody interviewed yet.",
     "I spoke with five weekend birdwatchers about keeping their sighting notes.",
     "You spoke with five birdwatchers, but still need to observe how they record outings.", 5),
], ids=["b2b-printing", "consumer-birdwatching"])
def test_completed_interviews_next_step_uses_only_saved_venture_learning(env, monkeypatch, hypothesis, report, learning, sample_size):
    from app.chat import next_moves
    from app.db.models import Venture
    from app.db.supymem_models import Experiment
    from app.experiments import ledger
    from app.state import db

    client, venture_id, clock = env
    _finish_intake(venture_id)
    monkeypatch.setattr(next_moves, "experiments_autopilot_enabled", lambda: False)
    state = _saved_state(hypothesis, later=True)
    state["drafts"] = []
    state["idea"]["confirmed"] = True
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: state)
    experiment_id, = ledger.create_from_tests(venture_id, "chat", [{"hypothesis": hypothesis}])
    ledger.record_result(experiment_id, venture_id, True, report, "inconclusive", learning,
        sample_size=sample_size,
        provenance={"source": "Your notes", "method": "Interviews", "observed_at": clock["now"].isoformat()})
    foreign_learning = "You received paid orders for an unrelated furniture restoration service."
    with db()() as session:
        venture = session.get(Venture, venture_id)
        session.add(Venture(id="unrelated_venture", workspace_id=venture.workspace_id, name="Other idea",
            raw_idea="Furniture restoration", stage="idea", created_at=clock["now"], updated_at=clock["now"]))
        session.add(Experiment(id="unrelated_result", workspace_id=venture.workspace_id, venture_id="unrelated_venture",
            kind=ledger.KIND, state="concluded", hypothesis="Restoration orders", outcome="supported",
            learnings=foreign_learning, ended_at=clock["now"] + timedelta(seconds=1),
            context=json.dumps({"direction": ledger.current_direction(session, venture_id)})))
        session.commit()
    clock["now"] += timedelta(seconds=2)
    before = ledger.evidence_for_venture(venture_id)
    foreign_before = ledger.evidence_for_venture("unrelated_venture")

    response = client.post(f"/ventures/{venture_id}/chat", json={"message": "What next?"})

    assert response.status_code == 200, response.text
    reply = response.json()
    assert learning in reply["body_md"] and foreign_learning not in reply["body_md"]
    assert hypothesis not in reply["body_md"]
    assert "Your test is too early to judge" in reply["body_md"]
    assert "Choose Keep testing your direction to take your next step." in reply["body_md"]
    assert reply["actions"][0]["action"] == "experiment.list"
    assert not any(action.get("confirmation") for action in reply["actions"])
    assert "credit" not in reply["body_md"].lower() and "\u2014" not in reply["body_md"]
    assert ledger.evidence_for_venture(venture_id) == before
    assert ledger.evidence_for_venture("unrelated_venture") == foreign_before
    assert ledger.get(experiment_id, venture_id)["hypothesis"] == hypothesis


@pytest.mark.parametrize("audience", ["commercial greenhouse managers", "recreational canoeists"])
@pytest.mark.parametrize("autopilot", [False, True])
@pytest.mark.parametrize("previous_state", [None, "concluded", "running"])
def test_partial_interview_progress_is_your_next_step(env, monkeypatch, audience, autopilot, previous_state):
    from app.chat import next_moves
    from app.experiments import ledger

    client, venture_id, clock = env
    _finish_intake(venture_id)
    monkeypatch.setattr(next_moves, "experiments_autopilot_enabled", lambda: autopilot)
    older, running, proposed = ledger.create_from_tests(venture_id, "chat", [
        {"hypothesis": f"You can recruit {audience}"},
        {"hypothesis": f"You can solve a recurring problem for {audience}", "action": f"Interview eight {audience}"},
        {"hypothesis": f"You can reach {audience} through introductions"},
    ])
    if previous_state:
        previous = ledger.record_result(older, venture_id, True, f"I interviewed five {audience}.",
            "inconclusive", "You need more evidence.", complete=previous_state == "concluded", sample_size=5,
            provenance={"source": "Your notes", "method": "Interviews", "observed_at": clock["now"].isoformat()})
        assert previous["state"] == previous_state
        clock["now"] += timedelta(seconds=1)
    progress = "You reported results from 6 of 8. 2 remain."
    ledger.record_result(running, venture_id, True, f"I interviewed six {audience}. Two interviews remain.",
        "inconclusive", progress, complete=False)
    state = {**_saved_state(audience, later=True), "idea": {"confirmed": True},
        "questions_pending": False, "drafts": [], "experiment": ledger.get(running, venture_id)}
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: state)
    before = ledger.list_for_venture(venture_id)
    for drafts in ([], [("waiting_invitation", "draft")]):
        state["drafts"] = drafts
        for _ in range(2):
            reply = client.post(f"/ventures/{venture_id}/chat", json={"message": "What should I do next?"})
            assert reply.status_code == 200
            result = reply.json()
            assert result["actions"][0]["action"] == "experiment.list"
            assert progress in result["body_md"] and "finish" in result["body_md"].lower()
            assert "ending the test" in result["body_md"]
            assert not any(action.get("confirmation") for action in result["actions"])
    assert ledger.list_for_venture(venture_id) == before
    assert ledger.get(proposed, venture_id)["state"] == "proposed"


@pytest.mark.parametrize("audience,offer", [
    ("independent veterinary clinic owners", "a stock expiration tracker"),
    ("parents of beginner skaters", "a practice session calendar"),
])
@pytest.mark.parametrize("phase", ["clarifier", "gstack"])
def test_requested_review_shows_summary_and_preserves_open_question(env, audience, offer, phase):
    from app.chat.execute import _review_idea
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.idea.service import _next_turn_number
    from app.state import db
    from core.clock import utcnow

    from tests.unit.test_autonomous_experiments import confirm_saved_idea

    _, venture_id, _ = env
    confirm_saved_idea(venture_id)
    summary = f"You are building {offer} for {audience}."
    question = (f"What evidence do you have that your audience ({audience}) needs help with this?" if phase == "gstack"
                else f"How would you charge for helping {audience}?")
    with db()() as session:
        idea = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == venture_id))
        idea.state = "clarifying"
        answers = {"customer": audience, "offer": offer}
        revision = append_idea_revision(session, idea.current_revision_id, summary=summary,
            structured=json.dumps({**answers, "_answered_fields": answers}))
        session.add(ClarifierTurn(id="review_current_summary", session_id=idea.id, workspace_id=idea.workspace_id,
            venture_id=venture_id, revision_id=revision.id, turn=_next_turn_number(session, idea.id), role="founder",
            kind="answer", text=summary, options="{}", created_at=utcnow()))
        session.flush()
        session.add(ClarifierTurn(id="review_open_question", session_id=idea.id, workspace_id=idea.workspace_id,
            venture_id=venture_id, revision_id=revision.id, turn=_next_turn_number(session, idea.id), role="agent", kind="question",
            text=question, options=json.dumps({"field": "evidence" if phase == "gstack" else "pricing_hypothesis",
                "phase": phase, "gstack_id": "Q1", "gstack_name": "Demand Reality", "index": 1,
                "total": 2, "options": [], "queue": []}), created_at=utcnow()))
        session.commit()
    reply = _review_idea(venture_id)
    assert summary in reply["body_md"] and question in reply["body_md"]
    assert reply["kind"] == "idea_clarification_ready"
    assert any(action.get("action") == "chat.reply" and action.get("payload", {}).get("question_id") == "review_open_question"
               for action in reply["actions"])
    assert not any(action.get("confirmation") for action in reply["actions"])
    with db()() as session:
        assert session.scalar(select(IdeaSession).where(IdeaSession.venture_id == venture_id)).state == "clarifying"
        assert session.scalar(select(ClarifierTurn).where(ClarifierTurn.id == "review_open_question")).role == "agent"


@pytest.mark.parametrize("idea", DOMAINS)
def test_results_recommend_learning_not_old_invitations_when_nothing_waits(env, monkeypatch, idea):
    from app.chat import next_moves
    from app.db.models import Venture
    from app.db.supymem_models import Experiment
    from app.experiments import ledger
    from app.state import db

    from tests.unit.test_autonomous_experiments import confirm_saved_idea

    _, venture_id, clock = env
    confirm_saved_idea(venture_id)
    state = _saved_state(idea, later=True)
    state.update(drafts=[("old_invitation", "draft")], experiment={"id": "finished", "state": "concluded"})
    with db()() as session:
        venture = session.get(Venture, venture_id)
        session.add(Experiment(id="finished", workspace_id=venture.workspace_id, venture_id=venture_id,
            kind=ledger.KIND, state="concluded", hypothesis=idea, outcome="inconclusive", ended_at=clock["now"],
            context=json.dumps({"direction": ledger.current_direction(session, venture_id),
                "outreach_draft_ids": ["old_invitation"]})))
        session.commit()
    state["idea"]["confirmed"] = True
    recommendation = {"id": "persevere:finished:inconclusive", "title": "Keep testing your direction", "why": "You recorded an inconclusive result.",
        "action": "experiment.list", "payload": {}}
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: state)
    monkeypatch.setattr(next_moves, "_fresh_recommendation", lambda _: None)
    monkeypatch.setattr(next_moves, "experiment_learning", lambda _: {"recommendation": recommendation})
    reply = next_moves.compose(venture_id, requested=True)
    assert reply["actions"][0]["action"] == recommendation["action"]
    assert "inconclusive result" in reply["body_md"]
    assert not any(action["action"] in {"gtm.status", "gtm.automate", "outreach.draft"} for action in reply["actions"])
    assert state["drafts"] == [("old_invitation", "draft")]
    state["drafts"] = [("waiting", "draft")]
    assert next_moves.compose(venture_id, requested=True)["actions"][0]["action"] == "gtm.status"


@pytest.mark.parametrize("idea", DOMAINS)
def test_release_next_step_reads_learning_with_automation_disabled(env, monkeypatch, idea):
    from app.chat import next_moves
    from app.db.models import Venture
    from app.db.supymem_models import Experiment
    from app.experiments import ledger
    from app.state import db
    _, venture_id, clock = env
    _finish_intake(venture_id)
    monkeypatch.setattr(next_moves, "experiments_autopilot_enabled", lambda: False)
    state = {**_saved_state(idea), "page": True}
    state["idea"]["confirmed"] = True
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: state)
    learning = f"You still need four interviews before judging demand for {idea}."
    with db()() as session:
        venture = session.get(Venture, venture_id)
        session.add(Experiment(id="release_result", workspace_id=venture.workspace_id, venture_id=venture_id,
            kind=ledger.KIND, state="concluded", hypothesis=idea, outcome="inconclusive", learnings=learning,
            ended_at=clock["now"], context=json.dumps({"direction": ledger.current_direction(session, venture_id)})))
        session.commit()
    reply = next_moves.compose(venture_id)
    assert reply["recommendation"]["id"].endswith(":release_result:inconclusive")
    assert learning in reply["body_md"] and "Draft emails" not in reply["body_md"]
    assert "completed test" not in reply["body_md"]
    assert not any(action.get("confirmation") for action in reply["actions"])
    from app.chat.store import append_message
    append_message(venture_id, role="assistant", body_md="Your result is saved. Review your test.", kind="task_completed")
    client = env[0]
    for draft_count in (0, 2):
        state["drafts"] = [(f"draft_{index}", "draft") for index in range(draft_count)]
        for _ in range(2):
            response = client.post(f"/ventures/{venture_id}/chat", json={"message": "What should I do next?"})
            assert response.status_code == 200
            if draft_count:
                assert response.json()["actions"][0]["label"] == "Review your drafts"
                assert "Review your drafts" in response.json()["body_md"]
            else:
                assert learning in response.json()["body_md"]
            assert "Draft emails" not in response.json()["body_md"]


@pytest.mark.parametrize("idea", DOMAINS)
def test_next_steps_prioritize_current_plan_and_do_not_reuse_old_search(env, monkeypatch, idea):
    from app.chat import customers, next_moves, plan_chain
    from app.chat.store import append_message
    from app.db.models import ChatMessage
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.state import db
    from core.clock import utcnow

    client, venture_id, _ = env
    from app.db.models import Venture
    with db()() as session:
        workspace_id = session.get(Venture, venture_id).workspace_id
    now = utcnow()
    with db()() as session:
        for earlier in session.scalars(select(IdeaSession).where(IdeaSession.venture_id == venture_id)):
            earlier.state = "abandoned"
        session.add(ChatMessage(id="old_search", venture_id=venture_id, role="user",
            body_md="Find the previous unrelated audience", created_at=now))
        saved = IdeaSession(id="current_idea", venture_id=venture_id, workspace_id=workspace_id,
            case="know", state="confirmed", created_at=now, updated_at=now)
        session.add(saved)
        session.flush()
        revision = IdeaRevision(id="current_revision", session_id=saved.id, venture_id=venture_id,
            workspace_id=workspace_id, rev=1, source="founder", summary=idea,
            structured=json.dumps({"customer": idea}), created_at=now)
        session.add(revision)
        saved.current_revision_id = saved.confirmed_revision_id = revision.id
        session.commit()
    state = _saved_state(idea)
    state.update(idea_state="confirmed", questions_pending=False, plan_state="founder_review", page=True)
    state["idea"]["confirmed"] = True
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: state)
    monkeypatch.setattr(next_moves, "_fresh_recommendation", lambda _: None)
    monkeypatch.setattr(plan_chain, "_plans", lambda *args: [{"id": "current_plan", "state": "founder_review"}])
    monkeypatch.setattr(plan_chain, "next_task", lambda _: None)
    reply = next_moves.compose(venture_id)
    assert reply["actions"][0].get("href") == f"/v/{venture_id}/plan"
    assert not any(action.get("confirmation") for action in reply["actions"])
    state["plan_state"] = None
    monkeypatch.setattr(plan_chain, "_plans", lambda *args: [])
    reply = next_moves.compose(venture_id)
    search = next(action for action in reply["actions"] if action["action"] == "gtm.find")
    assert "previous unrelated" not in search.get("payload", {}).get("note", "")
    request = f"Find customers for {idea} in Canada"
    message = append_message(venture_id, role="user", body_md=request)
    assert message["citations"] == []
    assert customers.saved_search_request(venture_id) == request
    with db()() as session:
        session.get(IdeaSession, "current_idea").state = "reviewing"
        session.commit()
    corrected = idea + " in a new region"
    response = client.patch(f"/ventures/{venture_id}/idea/sessions/current_idea/revision",
        json={"patch": {"customer": corrected}})
    assert response.status_code == 200, response.text
    assert customers.saved_search_request(venture_id) == ""
    from app.chat import router
    refusal = {"body_md": "I cannot carry out that operation. Contact your qualified specialist before proceeding.", "actions": []}
    assert router._continue_clarifying(venture_id, refusal) == refusal
    row = ChatMessage(venture_id=venture_id, role="assistant", kind="status", body_md=refusal["body_md"],
        actions_json=json.dumps([{"label": "Retry", "action": "gtm.send", "retry": True}]))
    assert not next_moves.attach(venture_id, row)
    assert json.loads(row.actions_json) == []
    with db()() as session:
        receipt = session.scalar(select(ChatMessage).where(ChatMessage.venture_id == venture_id,
            ChatMessage.kind == "idea_correction_saved"))
        assert receipt is not None and corrected in receipt.body_md
        assert not any(action.get("confirmation") for action in json.loads(receipt.actions_json))


@pytest.mark.parametrize("idea,audience,consumer", [
    ("Dispatch handover tools", "shipping coordinators at freight firms in Canada", False),
    ("Weekend swimming groups", "adult swimming coaches in Ireland", True),
])
@pytest.mark.parametrize("typed", [False, True])
def test_customer_buttons_use_saved_people_target_without_changing_consumer_search(
        env, monkeypatch, idea, audience, consumer, typed):
    from app.chat import customers, next_moves
    from app.core.jobs import FatalError
    from app.core.local_mode import is_local
    from app.gtm import journey
    from types import SimpleNamespace

    _, venture_id, _ = env
    state = _saved_state(idea)
    state.update(questions_pending=False, page=True, research={"present": True}, library=[],
                 search_request=f"Find me 10 {audience} to talk to." if typed else "")
    state["idea"]["confirmed"] = True
    state["icp"] = {"who": audience, "mode": "consumer" if consumer else "company",
                    "customer_type": "B2C" if consumer else "B2B", "needs_input": False}
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: state)
    monkeypatch.setattr(next_moves, "_fresh_recommendation", lambda _: None)
    monkeypatch.setattr(customers, "saved_search_request", lambda _: state["search_request"])
    expected_note = state["search_request"] or ("" if is_local() or consumer else f"Find {audience} to talk to.")
    button = customers._search_card(state)
    suggested = next(move for move in next_moves.compose(venture_id)["actions"] if move["action"] == "gtm.find")
    assert button["payload"].get("note", "") == suggested["payload"].get("note", "") == expected_note
    if consumer:
        assert button["payload"]["recruitment_opt_in"] is True
    else:
        assert "recruitment_opt_in" not in button["payload"]
    document = {"icp": state["icp"], "_runtime": {"ceilings": {"signal_search": 8}}}
    contexts = []
    async def plan_model(payload, stage, context):
        contexts.append(context)
        return {"queries": [
            {"query": f"{audience} biographies", "intent": "person"},
            {"query": f"{audience} speakers", "intent": "person"}],
            "target_type": "person" if typed else "business" if not consumer else None,
            "audience": audience if typed else "", "requested_count": 10 if typed else None}
    def save_target(journey_id, payload, mutation):
        mutation(None, document, None)
        raise FatalError("gtm_test_target_saved")
    monkeypatch.setattr(journey, "_journal", lambda _: SimpleNamespace(
        document=lambda _: document, mutate=save_target))
    monkeypatch.setattr(journey, "_discovery_model", plan_model)
    monkeypatch.setattr(journey, "_progress", lambda *args: None)
    monkeypatch.setattr(journey, "_search_timing", lambda *args: None)
    with pytest.raises(FatalError, match="gtm_test_target_saved"):
        asyncio.run(journey._discover(audience, problem=idea, note=expected_note,
            payload={"venture_id": venture_id, "journey_id": "search-regression"}))
    target = document["search_target"]
    assert target.get("target_type") == ("person" if typed or not (is_local() or consumer)
                                         else None if consumer else "business")
    assert target.get("audience", "") == (audience if is_local() or typed or not consumer else "")
    assert target.get("requested_count") == (10 if typed else None)
    assert contexts[0]["audience"] == audience
    assert contexts[0]["mode"] == ("B2C" if consumer else "B2B")


@pytest.mark.parametrize("idea", [
    "Security incident evidence for enterprise response teams",
    "A wearable glucose monitor for people managing diabetes",
])
def test_next_moves_offer_work_without_claiming_it_started_or_requiring_approval(env, monkeypatch, idea):
    from app.chat import next_moves

    _, venture_id, _ = env
    state = _saved_state(idea, later=True)
    state["drafts"] = []
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: state)
    monkeypatch.setattr(next_moves, "_fresh_recommendation", lambda _: None)
    reply = next_moves.compose(venture_id)
    assert "Other steps you can ask me to take:" in reply["body_md"]
    assert "approve" not in reply["body_md"].lower()
    assert "what I'll do for you now" not in reply["body_md"]
    assert "Choose " in reply["body_md"]


def _saved_state(idea, *, later=False):
    return {"idea": {"idea_summary": idea, "confirmed": False},
            "icp": {"who": idea, "needs_input": False}, "research": {"present": later},
            "research_running": False, "search_state": "ready" if later else None, "search_job": None,
            "leads": ["contact_1"] if later else [], "reachable": [],
            "social_leads": ["contact_1"] if later else [], "gmail_connected": later,
            "drafts": [("draft_1", "draft")] if later else [], "sent": 0, "replied": 0,
            "page": later, "page_build": None, "published": None,
            "experiment": {"id": "experiment_1", "state": "proposed"} if later else None,
            "experiment_review": False, "library": ["entry_1"] if later else [],
            "skipped": [], "paused": False, "founder": None}


@pytest.mark.parametrize("idea", DOMAINS)
def test_local_chat_posts_only_a_changed_idle_recommendation(env, monkeypatch, idea):
    from app.chat import next_moves
    from app.chat.milestones import post_milestone
    from app.chat.store import append_message
    from app.ventures.stage import confirm_stage, get_stage
    from tests.company_os_harness import company_os_test_client

    client, _, clock = env
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    client = company_os_test_client(client.app)
    venture = client.post("/ventures", json={"raw_idea": idea, "start": False})
    assert venture.status_code == 201
    venture_id = venture.json()["id"]
    action, explanation = "research.prepare", "Check the demand."
    coaching = "Here's the one thing only you can do:"

    def card(*_args, **_kwargs):
        stage = get_stage(venture_id)["label"]
        label = {"research.prepare": "Research your audience", "outreach.draft": "Draft an invitation",
                 "gtm.find": "Find interviewees"}[action]
        return {"kind": "next_moves", "stage": stage, "state_hash": explanation, "citations": [],
                "body_md": f"You're at {stage}. I recommend: {label}. Why: {explanation}\n\n{coaching} Share what you learned.",
                "actions": [{"action": action, "label": label, "state_hash": explanation, "style": "primary"}]}

    def save(role, body, *, kind="text"):
        clock["now"] += timedelta(seconds=1)
        return append_message(venture_id, role=role, body_md=body, kind=kind)

    def load():
        clock["now"] += timedelta(seconds=1)
        response = client.get(f"/ventures/{venture_id}/chat")
        assert response.status_code == 200

    def recommendations():
        return [body for body, _ in _messages(venture_id, "next_moves")]

    monkeypatch.setattr(next_moves, "compose", card)
    save("assistant", "Your idea is saved.")
    load()
    load()
    assert len(recommendations()) == 1
    assert coaching in recommendations()[0]

    confirm_stage(venture_id, "outreach")
    action = "outreach.draft"
    load()
    assert len(recommendations()) == 1
    save("assistant", "Your plan is saved.")
    save("user", f"Design an experiment for {idea}.")
    load()
    assert len(recommendations()) == 1
    reply = save("assistant", "Your experiment is ready to review.", kind="task_completed")
    assert "next_moves" not in reply
    assert "I recommend:" not in reply["body_md"]
    clock["now"] += timedelta(seconds=1)
    assert post_milestone(venture_id, requested=True, key="customer-search-result", kind="gtm_search_results",
                          body_md="Your customer search results are saved.")
    assert "next_moves" not in client.get(f"/ventures/{venture_id}/chat").json()["items"][-1]
    load()
    assert len(recommendations()) == 2
    assert "Outreach" in recommendations()[-1]
    assert coaching in recommendations()[-1]

    explanation = "The saved evidence was updated."
    save("assistant", "Your feedback is saved.")
    load()
    assert len(recommendations()) == 2

    action = "gtm.find"
    load()
    assert len(recommendations()) == 3
    assert coaching not in recommendations()[-1]


@pytest.mark.parametrize("idea", DOMAINS)
def test_handler_admission_applies_to_recommendations_and_saved_reply_actions(env, monkeypatch, idea):
    from app.chat import build_path, experiments, next_moves
    from app.chat.store import append_message
    from app.core.local_mode import is_local

    _, venture_id, _ = env
    state = {**_saved_state(idea), "idea": {"idea_summary": idea, "confirmed": True},
             "page": True, "questions_pending": True, "experiment": {"id": "saved_test", "state": "proposed"}}
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: state)
    monkeypatch.setattr(next_moves, "experiments_autopilot_enabled", lambda: True)
    prerequisite = {"kind": "needs_you", "body_md": "Review your saved answers to continue.",
                    "actions": [{"label": "Review your answers", "action": "startup.review"}], "citations": []}
    monkeypatch.setattr(build_path, "research_prerequisites", lambda _: prerequisite)
    monkeypatch.setattr(experiments, "_idea_gate", lambda _: prerequisite)
    recommendation = {"id": "test:saved_test", "title": f"Test next: {idea}",
                      "why": "Use your saved test.", "action": "experiment.recommendation.prepare",
                      "payload": {"experiment_id": "saved_test"}}
    monkeypatch.setattr(next_moves, "_fresh_recommendation", lambda _: recommendation)
    blocked = {"research.prepare", *experiments.GATED_ACTIONS}

    assert build_path.research_card(venture_id) == prerequisite
    assert experiments.start_reply(venture_id, "") == prerequisite
    card = next_moves.compose(venture_id)
    assert "recommendation" not in card
    assert recommendation["title"] not in card["body_md"]
    assert card["actions"][0]["action"] == "startup.review"
    assert not blocked.intersection(move["action"] for move in card["actions"])
    assert "Choose " in card["body_md"] and "\u2014" not in card["body_md"]

    for action, label in (("research.prepare", "Start research"),
                          ("experiment.recommendation.prepare", "Run your saved test")):
        stale_actions = [{"label": label, "action": action, "payload": recommendation["payload"]}]
        assert not next_moves.align_reply(card, f"Would you like to {label}?", stale_actions)
        assert not stale_actions
        saved = append_message(venture_id, role="assistant", body_md="Your progress is saved.",
                               actions=[{"label": label, "action": action, "payload": recommendation["payload"]}])
        assert not blocked.intersection(move["action"] for move in saved["actions"])
        if is_local():
            assert "next_moves" not in saved
        else:
            assert saved["next_moves"]["actions"]

    monkeypatch.setattr(build_path, "research_prerequisites", lambda _: None)
    monkeypatch.setattr(experiments, "_idea_gate", lambda _: None)
    state["questions_pending"] = False
    admitted = next_moves.compose(venture_id)
    assert admitted["recommendation"] == recommendation
    assert admitted["actions"][0]["action"] == recommendation["action"]
    assert any(move["action"] == "research.prepare" for move in admitted["actions"])


@pytest.mark.parametrize("idea,requirement", [
    ("Inspection scheduling for fleet mechanics", "Review your fleet account challenge"),
    ("Shared meal plans for families", "Review your family calendar challenge"),
])
def test_pending_idea_keeps_the_founder_blocker_without_routine_actions(env, monkeypatch, idea, requirement):
    from app.chat import execute, next_moves

    _, venture_id, _ = env
    state = {**_saved_state(idea), "reading_idea": True, "first_question_pending": True,
             "founder": {"what_we_need": requirement}}
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: state)
    monkeypatch.setattr(execute, "_review_idea", lambda _: {"kind": "idea_clarification_pending",
        "body_md": "I'm reading your idea.", "actions": [{"action": "chat.reply", "payload": {"text": "Skip the questions"}}],
        "citations": []})
    reply = next_moves.compose(venture_id)
    assert reply["kind"] == "status" and requirement in reply["body_md"]
    assert reply["actions"] == [] and reply["state_hash"]


@pytest.mark.hosted
@pytest.mark.parametrize("idea", DOMAINS)
def test_learning_recommends_one_saved_next_move_without_repeating_or_starting_work(env, monkeypatch, idea):
    from tests.unit.test_autonomous_experiments import confirm_saved_idea
    from app.chat import next_moves
    from app.chat.actions import get_action
    from app.chat.store import append_message
    from app.credits import policy
    from app.db.models import Venture
    from app.db.supymem_models import Experiment
    from app.experiments import autonomous, beliefs, ledger
    from app.state import db

    _, vid, clock = env
    confirm_saved_idea(vid)
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: _saved_state(idea))
    original_policy = policy.get_policy
    flag = {"value": False}
    monkeypatch.setattr(policy, "get_policy", lambda key, default=None:
        {"experiments_autopilot_v1": flag["value"]} if key == "feature_flags" else original_policy(key, default))

    def forbidden(*args, **kwargs):
        pytest.fail("Reading next moves must not generate ideas or start an experiment")

    monkeypatch.setattr(autonomous, "available", forbidden)
    monkeypatch.setattr(autonomous, "run", forbidden)
    monkeypatch.setattr(beliefs, "generate", forbidden)
    with db()() as session:
        workspace_id = session.get(Venture, vid).workspace_id
        saved = beliefs.row(session, vid, create=True)
        direction = ledger.current_direction(session, vid)
        saved.context = json.dumps({"direction": direction, "beliefs": [{"id": "belief_next", "type": "pain",
            "status": "to_test", "statement": idea, "confidence": 0.8, "evidence": []}],
            "plan": {"id": "plan_next", "state": "draft", "tests": [{"experiment_id": "exp_next", "state": "queued"}]}})
        session.add_all([
            Experiment(id="exp_next", workspace_id=workspace_id, venture_id=vid, kind=ledger.KIND,
                state="proposed", hypothesis=idea, context=json.dumps({"runner": "system", "direction": direction,
                    "spec": {"id": "exp_next", "direction": direction}})),
            Experiment(id="exp_old", workspace_id=workspace_id, venture_id=vid, kind=ledger.KIND,
                state="concluded", hypothesis="An earlier direction", outcome="refuted", ended_at=clock["now"],
                context=json.dumps({"direction": "old_revision"})),
            Experiment(id="exp_future", workspace_id=workspace_id, venture_id=vid, kind=ledger.KIND,
                state="concluded", hypothesis="A future result", outcome="supported", ended_at=clock["now"] + timedelta(days=1),
                context=json.dumps({"direction": direction}))])
        session.commit()
    baseline = next_moves.compose(vid)
    assert "recommendation" not in baseline
    flag["value"] = "true"
    assert next_moves.compose(vid) == baseline
    flag["value"] = True
    idle = next_moves.compose(vid)
    assert idle["recommendation"]["id"] == "test:exp_next"
    assert idle["actions"][0]["action"] == "idea.review"
    assert idle["actions"][0]["style"] == "primary"
    assert idle["actions"][1]["action"] == "experiment.recommendation.prepare"
    assert idle["actions"][1]["label"] == "Test next"
    assert "Why:" in idle["body_md"] and idea in idle["body_md"]
    assert idle["state_hash"] != baseline["state_hash"]

    def save_and_check_once(body="Your saved work is ready."):
        clock["now"] += timedelta(seconds=1)
        saved = append_message(vid, role="assistant", kind="task_completed", body_md=body)
        recommendation = saved["next_moves"]["recommendation"]
        assert sum(action["label"] in {"Test next", "Plan your next test", "Consider a pivot", "Keep testing your direction"} for action in saved["next_moves"]["actions"]) == 1
        assert get_action(saved["next_moves"]["actions"][0]["action"])
        assert "spinoff" not in saved["next_moves"]["actions"][0]["action"]
        assert "\u2014" not in json.dumps(saved, ensure_ascii=False)
        assert "recommendation" not in next_moves.compose(vid)
        clock["now"] += timedelta(seconds=1)
        append_message(vid, role="assistant", kind="task_completed", body_md="Your preference is saved.")
        assert "recommendation" not in next_moves.compose(vid)
        return recommendation

    assert save_and_check_once("Shall we Continue clarifying?")["id"] == idle["recommendation"]["id"]
    with db()() as session:
        saved = beliefs.row(session, vid)
        context = json.loads(saved.context)
        context["plan"].update(state="active", tests=[])
        saved.context = json.dumps(context)
        session.commit()
    assert save_and_check_once()["id"] == "plan:belief_next"
    for outcome, decision in (("refuted", "pivot"), ("supported", "persevere"), ("inconclusive", "persevere")):
        with db()() as session:
            row = session.get(Experiment, "exp_next")
            row.state, row.outcome, row.ended_at = "concluded", outcome, clock["now"]
            session.commit()
        if outcome == "refuted":
            with db()() as session:
                waiting = Experiment(id="exp_waiting", workspace_id=workspace_id, venture_id=vid, kind=ledger.KIND,
                    state="proposed", hypothesis=idea + " follow-up", context=json.dumps({"direction": direction}))
                session.add(waiting)
                session.commit()
                continuing = next_moves.compose(vid)["recommendation"]
                assert continuing["id"].startswith("persevere:") and "other tests are still open" in continuing["why"]
                session.delete(waiting)
                session.commit()
        current = next_moves.compose(vid)
        assert current["recommendation"]["id"] == decision + ":exp_next:" + outcome
        assert save_and_check_once()["why"] == current["recommendation"]["why"]
    flag["value"] = False
    assert "recommendation" not in next_moves.compose(vid)
    with db()() as session:
        assert session.get(Experiment, "exp_next").outcome == "inconclusive"
        assert json.loads(beliefs.row(session, vid).context)["plan"]["state"] == "active"


@pytest.mark.hosted
@pytest.mark.parametrize("idea", DOMAINS)
def test_capability_copy_is_requested_or_stage_changed_without_consecutive_blocks(env, monkeypatch, idea):
    from app.chat import conversations, next_moves
    from app.chat.store import append_message, idempotent_message, list_messages
    from app.db.models import Venture, Workspace
    from app.state import db
    from app.tenancy import bind
    from app.ventures.stage import confirm_stage, get_stage

    _, vid, clock = env
    with db()() as session:
        session.get(Venture, vid).raw_idea = idea
        workspace_id = session.get(Venture, vid).workspace_id
        actor = session.get(Workspace, workspace_id).owner_id
        session.commit()
    state = _saved_state(idea)
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: state)
    heading = "Other steps you can ask me to take:"

    def reply(request=None, *, explicit=False, key=None):
        if request is not None:
            clock["now"] += timedelta(seconds=1)
            append_message(vid, role="user", body_md=request)
        clock["now"] += timedelta(seconds=1)
        card = next_moves.compose(vid)
        saved = append_message(vid, role="assistant", kind=card["kind"] if explicit else "task_completed",
                               body_md=card["body_md"] if explicit else "Your preference is saved.",
                               actions=card["actions"] if explicit else None, idempotency_key=key)
        requested = bool(request and next_moves._CAPABILITY_REQUEST.search(request))
        if requested:
            assert saved["actions"] == card["actions"][:1]
        else:
            assert (saved["actions"] if explicit else saved["next_moves"]["actions"]) == card["actions"]
        if explicit or requested:
            assert saved["next_moves"]["actions"] == []
        assert saved["next_moves"]["stage"] == card["stage"]
        assert has_status(saved) == has_block(saved)
        assert "\u2014" not in saved["body_md"]
        return saved

    def has_block(saved):
        return heading in saved["body_md"] or heading in saved.get("next_moves", {}).get("body_md", "")

    def has_status(saved):
        body = saved["body_md"] + saved.get("next_moves", {}).get("body_md", "")
        return any(copy in body for copy in ("You're at ", "Your work is underway.",
                   "Your work is waiting to start.", "You can keep going with the options below."))

    assert not has_block(reply("What's happening?", explicit=True))
    for request in ("Save that preference.", "Thanks, that answers my question."):
        saved = reply(request)
        assert saved["content"] == "Your preference is saved."
        assert saved["next_moves"]["body_md"] == ""
        assert not has_block(saved)
    assert not has_block(reply("What can you help me with?"))
    suppressed = reply("What should I do next?", explicit=True, key="capabilities-replay")
    assert not has_block(suppressed)
    card = next_moves.compose(vid)
    assert append_message(vid, role="assistant", kind=card["kind"], body_md=card["body_md"],
                          actions=card["actions"], idempotency_key="capabilities-replay") == suppressed
    assert idempotent_message(vid, "assistant", "capabilities-replay") == suppressed
    with pytest.raises(ValueError, match="chat_idempotency_conflict"):
        append_message(vid, role="assistant", kind=card["kind"], body_md="A different reply.",
                       idempotency_key="capabilities-replay")
    assert not has_block(reply("What's next?", explicit=True))
    state["research_running"] = True
    assert not has_block(reply("Save the revised preference."))
    progressed = reply()
    assert not has_block(progressed)
    assert progressed["next_moves"]["stage"] == get_stage(vid)["label"] == "Idea"
    assert progressed["next_moves"]["focus"] == "Researching your audience"
    confirm_stage(vid, "building")
    changed = reply()
    assert has_block(changed)
    assert changed["next_moves"]["stage"] == get_stage(vid)["label"] == "Building"
    assert changed["next_moves"]["focus"] == progressed["next_moves"]["focus"]
    assert not has_block(reply())
    state["jobs"] = [("job_1", "running")]
    assert not has_block(reply("Is my preference saved?"))
    assert not has_block(reply("What can you do for me?"))
    with bind(workspace_id=workspace_id, venture_id=vid, actor=actor):
        conversation = conversations.create(vid, idempotency_key="capabilities-private")
        with conversations.bind_conversation(vid, conversation["id"]):
            assert not has_block(reply("What could I do next?", explicit=True))
            assert not has_block(reply("Save my preference for this chat."))
    assert not has_block(reply())
    assert not has_block(reply())
    state["priorities"] = ["Review feedback from your first conversations."]
    for job_status in ("queued", "pending", "scheduled", "retry_wait", "leased", "running"):
        state["jobs"] = [("job_1", job_status)]
        status = reply("What's happening?", explicit=True)
        assert not has_block(status)
        assert state["priorities"][0] in status["body_md"]
        assert list_messages(vid)[-1] == status
    state["founder"] = {"id": "need_1", "reason_code": "captcha", "what_we_need": "Complete your account check."}
    blocked = reply("What's blocked?", explicit=True)
    assert not has_block(blocked)
    assert state["founder"]["what_we_need"] in blocked["body_md"]
    clock["now"] += timedelta(seconds=1)
    card = next_moves.compose(vid)
    legacy = append_message(vid, role="assistant", kind=card["kind"], body_md=card["body_md"],
                            actions=card["actions"], preserve_exact=True)
    assert has_block(legacy) and "next_moves" not in legacy
    state.update(_saved_state(idea, later=True))
    confirm_stage(vid, "outreach")
    assert not has_block(reply("Save my updated preference."))
    changed = reply()
    assert not has_block(changed)
    assert changed["next_moves"]["stage"] == get_stage(vid)["label"] == "Outreach"
    assert changed["next_moves"]["focus"] == "Outreach drafts ready"
    assert not has_block(reply())


@pytest.mark.parametrize("idea", DOMAINS)
def test_next_moves_use_founder_stage_without_turning_intent_into_completed_outreach(env, monkeypatch, idea):
    from app.chat import next_moves
    from app.db.models import Venture, Workspace
    from app.state import db
    from app.ventures.stage import confirm_stage, get_stage

    client, _, _ = env
    vid = client.post("/ventures", json={"raw_idea": idea, "start": False}).json()["id"]
    state = {**_saved_state(idea), "idea": {"confirmed": True}, "research_running": True}
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: state)
    with db()() as session:
        venture = session.get(Venture, vid)
        legacy_stage = venture.stage
        workspace_id = venture.workspace_id
    initial = next_moves.compose(vid)
    assert initial["stage"] == get_stage(vid)["label"] == "Idea"
    assert initial["focus"].lower() == "researching your audience"
    with db()() as session:
        workspace = session.get(Workspace, workspace_id)
        settings = json.loads(workspace.settings_json or "{}")
        workspace.settings_json = json.dumps({**settings, "entry_goal": "gtm", "intake_venture_id": vid})
        session.commit()
    requested = next_moves.compose(vid)
    assert requested["stage"] == get_stage(vid)["label"] == "Outreach"
    assert requested["focus"] == initial["focus"]
    assert requested["state_hash"] != initial["state_hash"]
    assert state["sent"] == state["replied"] == 0
    assert not state["leads"] and not state["drafts"]
    assert "learning from outreach" not in requested["body_md"].lower()
    for correction in ("building", "idea"):
        previous = next_moves.compose(vid)
        corrected = confirm_stage(vid, correction)
        current = next_moves.compose(vid)
        assert current["stage"] == corrected["label"]
        assert current["focus"] == initial["focus"]
        assert current["state_hash"] != previous["state_hash"]
        assert current == next_moves.compose(vid)
    with db()() as session:
        assert session.get(Venture, vid).stage == legacy_stage


@pytest.mark.hosted
@pytest.mark.parametrize("idea", DOMAINS)
def test_capabilities_only_repeat_when_requested_or_the_stage_changes(env, monkeypatch, idea):
    from app.chat import next_moves
    from app.chat.store import append_message, list_messages
    from app.ventures.stage import confirm_stage

    client, _, clock = env
    vid = client.post("/ventures", json={"raw_idea": idea, "start": False}).json()["id"]
    state = _saved_state(idea)
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: state)
    headings = ("Other steps you can ask me to take:", "Here's the one thing only you can do:",
                "Questions are optional")
    for request, expanded in [("Help me get started", False), ("Explain that choice", False),
                              ("What can you do for me?", True), ("Explain that choice", False),
                              ("How can you help me choose a price?", True), ("Explain that choice", False),
                              ("How can you help me?", True)]:
        clock["now"] += timedelta(seconds=1)
        append_message(vid, role="user", body_md=request)
        clock["now"] += timedelta(seconds=1)
        reply = append_message(vid, role="assistant", body_md="Your answer is saved.")
        assert reply["body_md"].endswith("Your answer is saved.")
        assert all(heading not in reply["next_moves"]["body_md"] for heading in headings)
        assert reply["next_moves"]["body_md"] == ""
        if expanded:
            assert reply["actions"] == next_moves.compose(vid)["actions"][:1]
            assert reply["next_moves"]["actions"] == []
        else:
            assert reply["next_moves"]["actions"] == next_moves.compose(vid)["actions"]
        if request == "What can you do for me?":
            clock["now"] += timedelta(seconds=1)
            followup = append_message(vid, role="assistant", kind="task_completed", body_md="Your work is saved.")
            assert followup["next_moves"]["body_md"] == ""
    state.update(_saved_state(idea, later=True))
    clock["now"] += timedelta(seconds=1)
    changed = append_message(vid, role="assistant", kind="task_completed", body_md="Your drafts are saved.")
    assert changed["next_moves"]["body_md"] == ""
    stage = confirm_stage(vid, "building")
    clock["now"] += timedelta(seconds=1)
    changed = append_message(vid, role="assistant", kind="task_completed", body_md="Your updated plan is saved.")
    assert "Review your draft" in changed["next_moves"]["body_md"]
    assert all(heading not in changed["next_moves"]["body_md"] for heading in headings)
    card = next_moves.compose(vid)
    for _ in range(2):
        clock["now"] += timedelta(seconds=1)
        reply = append_message(vid, role="assistant", kind=card["kind"], body_md=card["body_md"],
                               actions=card["actions"])
        assert all(heading not in reply["body_md"] for heading in headings)
        assert reply["next_moves"]["body_md"] == ""
        assert reply["next_moves"]["actions"] == []
        assert reply["actions"] == card["actions"]
        assert reply["next_moves"]["stage"] == stage["label"]
        assert reply["next_moves"]["focus"] == "Outreach drafts ready"
    assert list_messages(vid)[-1] == reply


@pytest.mark.parametrize("idea", DOMAINS)
def test_delayed_capabilities_stay_on_their_request_without_repeating_after_another_reply(env, monkeypatch, idea):
    from app.chat import next_moves
    from app.chat.store import CLIENT_REQUEST_ID, REPLY_REQUEST_ID, append_message, list_messages
    from app.core.local_mode import is_local

    client, _, clock = env
    vid = client.post("/ventures", json={"raw_idea": idea, "start": False}).json()["id"]
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: _saved_state(idea))
    heading = "Other steps you can ask me to take:"

    def save(role, body, nonce, *, preserve_exact=False):
        clock["now"] += timedelta(seconds=1)
        identity = CLIENT_REQUEST_ID if role == "user" else REPLY_REQUEST_ID
        token = identity.set(nonce)
        try:
            return append_message(vid, role=role, body_md=body, preserve_exact=preserve_exact)
        finally:
            identity.reset(token)

    original = save("user", "What can you do for me?", "request-original")
    newer = save("user", "Save my preference.", "request-newer")
    delayed = save("assistant", "Your options are ready.", "request-original", preserve_exact=True)
    latest = save("assistant", "Your preference is saved.", "request-newer")
    if is_local():
        assert not next_moves.post(vid, message_id=delayed["id"])
        rows = {row["id"]: row for row in list_messages(vid)}
        assert rows[delayed["id"]]["reply_to_message_id"] == original["id"]
        assert rows[latest["id"]]["reply_to_message_id"] == newer["id"]
        followup = save("assistant", "Your work is finished.", "request-original")
        assert followup["reply_to_message_id"] == original["id"]
        assert all("next_moves" not in row for row in (rows[delayed["id"]], rows[latest["id"]], followup))
        return
    assert next_moves.post(vid, message_id=delayed["id"])
    rows = {row["id"]: row for row in list_messages(vid)}
    assert rows[delayed["id"]]["reply_to_message_id"] == original["id"]
    assert rows[delayed["id"]]["next_moves"]["founder_message_id"] == original["id"]
    assert rows[delayed["id"]]["next_moves"]["body_md"] == ""
    assert rows[delayed["id"]]["actions"] == next_moves.compose(vid)["actions"][:1]
    assert rows[latest["id"]]["reply_to_message_id"] == newer["id"]
    assert rows[latest["id"]]["next_moves"]["body_md"] == ""
    followup = save("assistant", "Your work is finished.", "request-original")
    assert followup["reply_to_message_id"] == original["id"]
    assert followup["next_moves"]["founder_message_id"] == original["id"]
    assert followup["next_moves"]["body_md"] == ""


def test_next_moves_differ_by_saved_state_and_keep_actions_executable(env, monkeypatch):
    from app.core.local_mode import is_local
    from app.experiments import autonomous
    monkeypatch.setattr(autonomous, "run_blocked_reason", lambda *_: None)  # saved state fixture has no saved test row
    from tests.unit.test_autonomous_experiments import confirm_saved_idea

    from app.chat import next_moves
    from app.chat.actions import REGISTRY
    from app.chat.execute import _whats_next
    from app.analytics.feature_catalog import ACTION_SLUG
    from app.chat.assistant import SYSTEM_PROMPT
    assert "Clarifying questions are optional" in SYSTEM_PROMPT
    assert "asked as a question" not in SYSTEM_PROMPT

    from app.db.models import GtmContact, GtmTouch, Venture
    from app.db.supymem_models import IdeaSession
    from app.state import db
    client, _, _ = env
    ventures = [client.post("/ventures", json={"raw_idea": domain, "start": False}).json()["id"] for domain in DOMAINS]
    confirm_saved_idea(ventures[1])
    with db()() as session:
        workspace = session.get(Venture, ventures[1]).workspace_id
        session.add(GtmContact(id="test_contact", workspace_id=workspace, stage_changed_at=NOON, created_at=NOON))
        session.flush()
        session.add(GtmTouch(id="test_touch", workspace_id=workspace, venture_id=ventures[1], contact_id="test_contact",
                            direction="out", channel="email", simulated=0, sent_at=NOON, created_at=NOON))
        saved_idea = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == ventures[1]))
        saved_idea.state = "confirmed"
        saved_idea.confirmed_revision_id = saved_idea.current_revision_id
        session.commit()
    real_cards = [next_moves.compose(vid) for vid in ventures]
    assert real_cards[0]["stage"] == "Idea"
    assert real_cards[1]["stage"] == "Outreach"
    assert real_cards[1]["focus"].lower() == "learning from outreach"
    assert real_cards[0]["actions"] != real_cards[1]["actions"]

    states = {vid: _saved_state(domain, later=index == 1) for index, (vid, domain) in enumerate(zip(ventures, DOMAINS))}
    monkeypatch.setattr(next_moves, "_snapshot", states.__getitem__)
    cards = [_whats_next(vid) for vid in ventures]
    assert cards[0]["stage"] == "Idea"
    assert cards[1]["stage"] == "Outreach"
    assert cards[1]["focus"].lower() == "outreach drafts ready"
    assert cards[0]["actions"] != cards[1]["actions"]
    assert any(action["action"] == "gtm.library_claim" for action in cards[1]["actions"]) is (not is_local())
    assert any(action["action"] == "experiment.recommendation.prepare" for action in cards[1]["actions"])
    from app.chat.assistant import _fallback_answer
    preview = next_moves.short_reply(cards[0]["body_md"]) if is_local() else cards[0]["body_md"]
    assert _fallback_answer("What's going on?", {"venture_id": ventures[0]}) == preview
    for card in cards:
        for action in card["actions"]:
            if action["action"] in {"gtm.automate", "gtm.find"}:
                assert action["payload"].get("prepare") is True
                assert "confirmation" not in action
    from app.chat.execute import execute_action
    automation = asyncio.run(execute_action(env[1], "gtm.automate", payload={"prepare": True}))
    assert automation["kind"] == "text" and not any(action.get("confirmation") for action in automation["actions"])
    if is_local():
        from app.core.local_capabilities import blocked_message

        assert automation["body_md"] == blocked_message("send_email")
        assert automation["actions"] == []
    else:
        assert "Gmail is not connected" in automation["body_md"]
        assert automation["actions"][0]["action"] == "connections.connect"
    for card in cards:
        runnable = [action for action in card["actions"] if action["action"] != "idea.review"]
        assert len(runnable) >= 2
        assert card["body_md"].count("Here's the one thing only you can do:") == (0 if card["actions"][0]["action"] == "gtm.status" else 1)
        assert "I need you for:" not in card["body_md"] and "\u2014" not in card["body_md"]
        assert all(action["action"] in REGISTRY and "href" not in action for action in card["actions"])
        assert all(action["state_hash"] == card["state_hash"] for action in card["actions"])
    assert ACTION_SLUG["next_moves"] == "planning"
    for vid, domain in zip(ventures, DOMAINS):
        states[vid] = {**_saved_state(domain), "research_running": True, "search_state": "running", "page": True}
        running = next_moves.compose(vid)
        if is_local():
            assert [action["action"] for action in running["actions"] if action["action"] != "idea.review"] == ["ceo_review"]
        else:
            assert len([action for action in running["actions"] if action["action"] != "idea.review"]) >= 2
        states[vid]["idea"]["confirmed"] = True
        states[vid]["questions_pending"] = True
        pending = [action["action"] for action in next_moves.compose(vid)["actions"]]
        assert "startup.review" in pending and "idea.review" not in pending
        states[vid]["questions_pending"] = False
        assert not any(action["action"] == "idea.review" for action in next_moves.compose(vid)["actions"])


@pytest.mark.parametrize("idea", DOMAINS)
def test_next_moves_keep_two_alternatives_and_only_one_primary(monkeypatch, idea):
    from app.core.local_mode import is_local
    from app.experiments import autonomous
    monkeypatch.setattr(autonomous, "run_blocked_reason", lambda *_: None)  # saved state fixture has no saved test row
    from app.chat import next_moves
    from app.core.local_mode import is_local

    state = {**_saved_state(idea, later=True), "page": False, "drafts": []}
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: state)
    card = next_moves.compose("venture")
    actions = card["actions"]
    from app.chat.actions import _local_blocked_reason
    if is_local():
        expected = (["idea.review", "experiment.recommendation.prepare", "ceo_review"]
                    if _local_blocked_reason("outreach.draft") else
                    ["idea.review", "experiment.recommendation.prepare", "outreach.draft", "gtm.find"])
    else:
        expected = ["idea.review", "experiment.recommendation.prepare", "gtm.library_claim", "outreach.draft"]
    assert [action["action"] for action in actions] == expected
    assert card["body_md"].count("I recommend:") == 1
    assert card["body_md"].count("- I can ") == 2
    assert [action["style"] for action in actions] == ["primary"] + ["secondary"] * (len(actions) - 1)
    assert all(action["state_hash"] == card["state_hash"] for action in actions)
    assert actions[0]["payload"] == {} and actions[0]["label"] == "Continue clarifying"
    assert idea in state["icp"]["who"]


@pytest.mark.parametrize("idea", DOMAINS)
@pytest.mark.parametrize("draft_states", [["draft"] * 3, ["draft", "skipped", "sent"], ["skipped", "sent"]])
def test_ready_drafts_outrank_pending_clarification_without_reoffering_drafting(monkeypatch, idea, draft_states):
    from app.chat import next_moves
    from app.core.local_mode import is_local

    state = {**_saved_state(idea), "icp": {"who": idea, "needs_input": True},
             "drafts": [(f"draft_{index}", status) for index, status in enumerate(draft_states)]}
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: state)
    monkeypatch.setattr(next_moves, "_fresh_recommendation", lambda _: {"id": "saved-research",
        "title": "Inspect your saved research", "why": "Your evidence is saved.", "action": "research", "payload": {}})
    card = next_moves.compose("venture")
    count = draft_states.count("draft")
    if not count:
        if is_local():
            assert card["actions"][0]["action"] == "idea.review"
            assert not any(action["action"] == "outreach.draft" for action in card["actions"])
        else:
            assert any(action["action"] == "outreach.draft" for action in card["actions"])
        assert not any(action["action"] == "gtm.status" for action in card["actions"])
        return
    assert card["actions"][0]["action"] == "gtm.status"
    assert card["actions"][0]["style"] == "primary"
    assert card["actions"][0]["label"] == f"Review your draft{'s' if count != 1 else ''}"
    assert card["actions"][0]["payload"] == {"show_saved": True}
    assert card["actions"][1]["style"] == "secondary"
    assert not any(action["action"] == "outreach.draft" for action in card["actions"])
    assert not any(action["action"] == "idea.review" for action in card["actions"])
    assert card["actions"][0]["label"] in card["body_md"]
    assert "inspect your saved research" not in card["body_md"].lower()
    assert card["recommendation"]["id"] == "saved-research"
    assert len(card["body_md"].split()) <= 25


@pytest.mark.hosted
@pytest.mark.parametrize("idea", DOMAINS)
def test_next_moves_share_reply_and_replay_without_new_rows(env, monkeypatch, idea):
    from app.chat import next_moves, proactive
    from app.chat.store import append_message, idempotent_message, list_messages
    from app.db.models import ChatMessage, VentureDocument
    from app.state import db

    client, vid, clock = env
    state = _saved_state(idea)
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: state)
    monkeypatch.setattr(proactive, "_spawn", lambda function, *args, **kwargs: function(*args, **kwargs))
    _user_said(vid, NOON - timedelta(minutes=31))
    with db()() as session:
        documents = list(session.scalars(select(VentureDocument.id)))
    original = append_message(vid, role="assistant", kind="task_completed", body_md="Your work is saved.",
                              idempotency_key="finished-work")
    assert original["next_moves"]["stage"] == "Idea"
    assert original["next_moves"]["body_md"] == ""
    assert original["next_moves"]["actions"]
    assert original["citations"] == []
    assert len(list_messages(vid)) == 2
    assert client.get(f"/ventures/{vid}/chat").status_code == 200
    assert not _messages(vid, next_moves.KIND)
    state.update(_saved_state(idea, later=True))
    for _ in range(3):
        saved = append_message(vid, role="assistant", kind="task_completed", body_md="Your work is saved.",
                               idempotency_key="finished-work")
        assert saved == original == idempotent_message(vid, "assistant", "finished-work")
        assert not next_moves.post(vid)
    clock["now"] += timedelta(minutes=31)
    assert client.get(f"/ventures/{vid}/chat").status_code == 200
    assert list_messages(vid)[-1] == original
    assert len(list_messages(vid)) == 2
    with db()() as session:
        assert list(session.scalars(select(VentureDocument.id))) == documents
        row = session.get(ChatMessage, original["id"])
        assert next(item["next_moves"] for item in json.loads(row.citations_json) if "next_moves" in item) == original["next_moves"]
    updated = append_message(vid, role="assistant", kind="task_completed", body_md="Your drafts are saved.")
    assert updated["next_moves"]["stage"] == "Idea"
    assert updated["next_moves"]["focus"].lower() == "outreach drafts ready"
    assert not _messages(vid, next_moves.KIND)
    assert not next_moves.post(vid)
    from app.chat import milestones
    monkeypatch.setattr(milestones, "DAILY_MESSAGE_LIMIT", 0)
    other = client.post("/ventures", json={"raw_idea": idea, "start": False}).json()["id"]
    assert not next_moves.post(other)
    assert not next_moves.post(other, requested=True)
    card = next_moves.compose(other)
    explicit = append_message(other, role="assistant", kind=card["kind"], body_md=card["body_md"],
                              actions=card["actions"], idempotency_key="requested-next-moves")
    assert append_message(other, role="assistant", kind=card["kind"], body_md=card["body_md"],
                          actions=card["actions"], idempotency_key="requested-next-moves") == explicit
    assert len(_messages(other, next_moves.KIND)) == 1
    append_message(vid, role="user", body_md="Do not start tasks or change anything.")
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: pytest.fail("composed next moves for an answer-only request"))
    append_message(vid, role="assistant", kind="text", body_md="Your answer is here.")


@pytest.mark.parametrize("idea", DOMAINS)
def test_optional_questions_skip_through_existing_confirmation_and_keep_answers_routable(env, monkeypatch, idea):
    from app.chat import next_moves
    from app.chat.execute import execute_action
    from app.chat.router import _latest_clarification
    from app.chat.store import append_message
    from app.db.models import Venture, Workspace
    from app.state import db
    from app.ventures import journey_overrides
    from app.ventures import startup_review

    client, vid, _ = env
    with db()() as session:
        venture = session.get(Venture, vid)
        venture.raw_idea = idea
        actor = session.get(Workspace, venture.workspace_id).owner_id
        startup_review._document(session, vid, "internal_new_user_flow", {"enabled": True})
        session.commit()
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: {
        **_saved_state(idea), "skipped": sorted(journey_overrides.read(vid)["skipped"])})
    question = append_message(vid, role="assistant", kind="idea_clarification_ready", body_md="Optional audience detail?",
                              actions=[{"label": "An answer", "action": "chat.reply", "payload": {"question_id": "question_1", "text": "An answer"}}])
    assert _latest_clarification(vid)["id"] == question["id"]
    card = next_moves.compose(vid)
    assert sum(action["label"] == "Continue clarifying" for action in card["actions"]) == 1
    skip = {"label": "Skip questions and keep going", "action": "journey.skip_step",
            "payload": {"step": "clarification", "why": "Skip optional questions and use my saved idea as assumptions."}}
    assert journey_overrides.parse(skip["label"])[0] == "journey.skip_step"
    prepared = asyncio.run(execute_action(vid, skip["action"], payload=skip["payload"], actor_subject=actor))
    assert prepared["kind"] == "confirmation"
    assert "clarification" not in journey_overrides.read(vid)["skipped"]
    arguments = prepared["actions"][0]["confirmation"]["resulting_state"]
    result = asyncio.run(execute_action(vid, skip["action"], payload=arguments, actor_subject=actor))
    assert "clarification" in journey_overrides.read(vid)["skipped"]
    assert "Lower confidence" in result["body_md"]
    from app.chat.build_path import research_card
    from app.research.context import pending_steps
    assert not pending_steps(vid)
    assert research_card(vid)["kind"] == "confirmation"
    restored = journey_overrides.prepare(vid, "journey.restore_step", {"step": "clarification", "why": "Review my assumptions again."}, actor)
    proposal_id = restored["actions"][0]["confirmation"]["resulting_state"]["proposal_id"]
    journey_overrides.apply(vid, "journey.restore_step", proposal_id, actor)
    assert pending_steps(vid), "Restoring questions must restore their admission gate"
    prepared = journey_overrides.prepare(vid, "journey.skip_step", skip["payload"], actor)
    proposal_id = prepared["actions"][0]["confirmation"]["resulting_state"]["proposal_id"]
    journey_overrides.apply(vid, "journey.skip_step", proposal_id, actor)
    continued = next_moves.compose(vid)
    assert all(action["action"] != "journey.skip_step" for action in continued["actions"])
    assert any(action["action"] == "research.prepare" for action in continued["actions"])
    monkeypatch.setattr("app.chat.execute.research_needs_start", lambda _: pytest.fail("rendering next moves checked research prerequisites"))
    saved = append_message(vid, role="assistant", kind=continued["kind"], body_md=continued["body_md"], actions=continued["actions"])
    assert saved["kind"] == next_moves.KIND
    assert client.get(f"/ventures/{vid}/chat").status_code == 200


@pytest.mark.parametrize("idea", DOMAINS)
def test_next_moves_do_not_hide_pending_confirmations_or_audience_questions(env, monkeypatch, idea):
    from app.chat import customers, next_moves
    from app.chat.actions import get_action
    from app.chat.confirm import ConfirmationCard
    from app.chat.guard import _pending_card_label, guard_reply
    from app.chat.llm_router import _state_hint
    from app.chat.store import append_message
    from types import SimpleNamespace

    _, vid, clock = env
    state = _saved_state(idea)
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: state)
    card = ConfirmationCard.for_action(get_action("pause"), interpretation="Pause this venture?")
    append_message(vid, role="user", body_md=idea)
    append_message(vid, role="assistant", kind="confirmation", body_md=card.interpretation, actions=[card.as_action()])
    assert not next_moves.post(vid)
    assert _pending_card_label(vid) == "Do it"
    assert "which suggested action" in guard_reply(vid, "yes")["body_md"]
    assert _state_hint(vid, SimpleNamespace(venture={}))["last_assistant_offered_cards"] == ["pause"]
    clock["now"] += timedelta(seconds=1)
    append_message(vid, role="assistant", body_md="Which audience do you want to reach?")
    state["replied"] = 1
    assert not next_moves.post(vid)
    assert customers.last_reply_asked(vid, ("Which audience",))
    assert _pending_card_label(vid) is None
    clock["now"] += timedelta(seconds=1)
    append_message(vid, role="user", body_md=idea)
    assert not customers.last_reply_asked(vid, ("Which audience",))
    from app.chat.assistant import question_messages
    messages = asyncio.run(question_messages(vid, idea, brief={"_rendered": "Saved idea: " + idea}, workspace_id=None))
    assert "You're at" not in messages[1]["content"]
    assert "Which audience" in messages[1]["content"]




def test_connections_status_card_never_gets_next_moves_attached():
    from types import SimpleNamespace
    from app.chat import next_moves
    for venture in ("ven_dental_clinics", "ven_hiking_guides"):
        row = SimpleNamespace(kind="connections_status", citations_json="[]", actions_json="[]")
        assert next_moves.attach(venture, row, request="show my connections") is False
        assert row.citations_json == "[]"


@pytest.mark.parametrize("idea", DOMAINS)
def test_requested_next_step_has_one_short_saved_state_recommendation(env, monkeypatch, idea):
    from app.chat import next_moves
    from app.db.models import Job
    from app.state import db

    client, venture_id, _ = env
    state = _saved_state(idea, later=True)
    state["idea"]["confirmed"] = True
    state["experiment"] = None
    state["library"] = []
    state["social_leads"] = []
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: state)
    monkeypatch.setattr(next_moves, "_fresh_recommendation", lambda _: None)
    expected = next_moves.compose(venture_id)["actions"][0]
    with db()() as session:
        before = set(session.scalars(select(Job.id)))

    response = client.post(f"/ventures/{venture_id}/chat", json={"message": "What should I do next?"})

    assert response.status_code == 200
    reply = response.json()
    actions = [*reply["actions"], *reply.get("next_moves", {}).get("actions", [])]
    body = reply["body_md"] + reply.get("next_moves", {}).get("body_md", "")
    assert len(actions) == 1
    assert actions[0]["action"] == expected["action"]
    assert actions[0]["payload"] == expected["payload"]
    assert len(body.split()) <= 80
    assert "Other steps" not in body and "only you can do" not in body
    assert expected["label"] in body and "Choose " in body
    assert "\u2014" not in body and "credit" not in body.lower()
    with db()() as session:
        assert set(session.scalars(select(Job.id))) == before


@pytest.mark.parametrize("idea", DOMAINS)
def test_capability_reply_only_offers_its_selected_next_step(env, monkeypatch, idea):
    from app.chat import next_moves
    from app.chat.store import append_message
    from app.org.characters import parse_speaker

    _, venture_id, _ = env
    state = _saved_state(idea, later=True)
    state["idea"]["confirmed"] = True
    state["experiment"] = None
    state["library"] = []
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: state)
    monkeypatch.setattr(next_moves, "_fresh_recommendation", lambda _: None)
    expected = next_moves.compose(venture_id)["actions"][0]
    append_message(venture_id, role="user", body_md="What can you do?")
    body = "I can help research your audience and prepare your work. Choose " + expected["label"] + "."

    reply = append_message(venture_id, role="assistant", body_md=body,
                           actions=[{"label": "Review your build", "action": "build.prepare"}])

    actions = [*reply["actions"], *reply.get("next_moves", {}).get("actions", [])]
    assert len(actions) == 1
    assert actions[0]["action"] == expected["action"]
    assert actions[0]["payload"] == expected["payload"]
    assert parse_speaker(reply["body_md"])[1] == body
    assert reply.get("next_moves", {}).get("body_md", "") == ""
    retry = {"label": "Review saved status", "action": "status", "retry": True}
    append_message(venture_id, role="user", body_md="What can you do?")
    failed = append_message(venture_id, role="assistant", kind="error",
        body_md="Your reply is unconfirmed. Review saved status before trying again.", actions=[retry])
    assert failed["actions"] == [retry]

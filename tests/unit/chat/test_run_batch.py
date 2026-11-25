"""Founder-journey fixes: one build card, retry as a button, honest claims, real failure codes."""

from __future__ import annotations

import json

import pytest

from tests.company_os_harness import load_company_os_app, local_chat as local_chat
from tests.unit.chat.test_assistant_routing import confirm_idea
from tests.unit.chat.test_round3_matrix import venture as venture

load_company_os_app()


def chat(client, vid, text):
    r = client.post(f"/ventures/{vid}/chat", json={"message": text})
    assert r.status_code == 200, r.text
    return r.json()


def confirm(client, vid, action, payload):
    return client.post(f"/ventures/{vid}/chat/confirm", json={"action": action, "payload": payload})


def card_of(reply):
    return next((a["confirmation"] for a in reply["actions"] if a.get("confirmation")), None)


def add_job(vid, wid, kind, code, payload=None, status="failed_dead"):
    from app.db.models import Job
    from app.state import db
    from core.clock import utcnow
    from core.ids import new_id

    with db()() as session:
        job = Job(id=new_id("job"), kind=kind, venture_id=vid, workspace_id=wid,
                  payload_json=json.dumps(payload or {}), status=status, last_error=code, created_at=utcnow())
        session.add(job)
        session.commit()
        return job.id


# ------------------------------------------------------------------ intents

@pytest.mark.parametrize("text,intent", [
    ("build my landing page", "start_product_build"),
    ("build my startup", "start_product_build"),
    ("kick off the build", "start_product_build"),
    ("start the build", "start_product_build"),
    ("yes, start the build now", "start_product_build"),
    ("build my app", "start_product_build"),
    ("implement my plan", "start_product_build"),
    ("retry validation", "retry_job"), ("Retry research", "retry_job"), ("try again", "retry_job"),
    ("please retry", "retry_job"), ("run validation again", "retry_job"),
    ("retry the plan", "plan_generate"), ("regenerate the plan", "plan_generate"),
    ("make me a new plan", "plan_generate"),
    ("why did validation fail?", "latest_error"), ("why did the research fail", "latest_error"),
    ("what went wrong with the plan", "latest_error"),
    ("Let's run the test from the review: send a manual churn alert to one gym owner and see if they act",
     "experiment_from_review"),
    ("we should try the suggested test", "experiment_from_review"),
    ("how do I retry validation?", "assistant"),
])
def test_intents(text, intent):
    from app.chat.intent import classify_intent
    from app.core.local_mode import is_local

    if is_local():
        intent = {
            "build my landing page": "pagebuilder.build",
            "build my startup": "assistant",
            "kick off the build": "build.tool.start",
            "start the build": "build.tool.start",
            "yes, start the build now": "build.tool.start",
            "build my app": "build.tool.start",
            "implement my plan": "build.tool.start",
        }.get(text, intent)
    assert classify_intent(text) == intent


@pytest.mark.asyncio
@pytest.mark.parametrize("connected", [True, False])
@pytest.mark.parametrize("audience", ["Equipment inspections for factory supervisors", "Weekend pottery lessons for hobbyists"])
async def test_local_page_request_dispatches_once_with_its_private_constraints(monkeypatch, audience, connected):
    from app.chat import router
    from app.chat.context import ChatContext
    from app.chat import execute as chat_execute
    from app.chat.intent import classify_intent
    from app.core.local_mode import is_local
    from app import coding_agents_consent
    from app.pagebuilder import api
    from desktop_runtime import agent_status

    if not is_local():
        pytest.skip("local direct page request")
    monkeypatch.setattr(coding_agents_consent, "consents", lambda actor: {"codex": connected})
    monkeypatch.setattr(agent_status, "statuses", lambda: [{"name": "codex", "installed": True,
        "signed_in": True, "available": True, "subscription_login": True}] if connected else [])
    text = f"Create a website for {audience}. Leave it private, never send emails."
    calls = []
    starts = []
    async def execute(venture_id, action, *, payload, actor_subject):
        calls.append((venture_id, action, payload, actor_subject))
        return chat_execute._build_landing_page(venture_id, payload, actor_subject)
    def start(venture_id, actor, fingerprint, *, provider, instruction):
        starts.append((venture_id, actor, provider, instruction))
        return {"kind": "text", "body_md": "Your private preview is being prepared. Review it next.", "actions": []}
    monkeypatch.setattr(router, "execute_action", execute)
    monkeypatch.setattr(api, "estimate", lambda *_args: {"ready": True})
    monkeypatch.setattr(chat_execute, "_pagebuilder_start", start)
    reply = await router._dispatch_intent("ven_private", ChatContext(venture={"id": "ven_private", "name": audience}),
        classify_intent(text), text, None, actor_subject="owner")
    assert calls == [("ven_private", "pagebuilder.build", {"prepare": True, "query": text}, "owner")]
    if not connected:
        assert starts == [] and "Settings > Keys and connections > Coding tools" in reply["body_md"]
        assert not any(action.get("confirmation") for action in reply["actions"])
        return
    assert starts == [("ven_private", "owner", None, text)]
    assert reply["kind"] == "text" and not reply["actions"] and "private preview" in reply["body_md"]


# ------------------------------------------------------------------ 1a: one build card

def _plan(*tasks):
    return {"method": "metered_foundry_plan", "stale_dependencies": False, "milestones": [
        {"title": title, "kind": kind, "included": True, "tasks": [{
            "id": tid, "title": ttl, "state": "pending", "capability_id": "build.run",
            "implementation": "new_code"}]} for title, kind, tid, ttl in tasks]}


@pytest.fixture()
def approved(monkeypatch):
    from app.builder import budgets
    from app.chat import plan_chain
    from app.infra import apps
    from app.planning import compiler
    from app.ventures import state_brief

    state = {"apps": [], "plan": _plan(("Landing page", "landing", "pt_land", "Build landing page"))}
    monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda *_a, **_k: {
        "idea": {"confirmed": True, "name": "Churn Guard"}, "research": {"plan_ready": True},
        "plan": {"present": True, "id": "pln_one", "state": "approved"}})
    monkeypatch.setattr(compiler, "get_plan", lambda _id: state["plan"])
    monkeypatch.setattr(apps, "list_apps", lambda _id: state["apps"])
    monkeypatch.setattr(apps, "slug_available", lambda _slug: {"available": True})
    monkeypatch.setattr(budgets, "runtime_quote", lambda *_a, **_k: {"ready": True, "ceiling_micro": 1_500_000})
    monkeypatch.setattr(plan_chain, "_workspace", lambda _vid: "wsp_ours")
    monkeypatch.setenv("SUPYMEM_LANDING_LANE", "build")  # these tests cover the managed-build lane
    return state


@pytest.mark.asyncio
async def test_build_with_no_app_starts_directly_with_exact_internal_ceiling(venture, approved, monkeypatch):
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    from app.chat.context import ChatContext
    from app.chat.router import _dispatch_intent
    from app.builder import execution
    from app.infra import apps
    from app.workspaces.resolve import LOCAL_PERSON_ID

    _, vid, _ = venture
    calls = []
    monkeypatch.setattr(apps, "create_draft", lambda **kwargs: calls.append(("app", kwargs["slug"])) or {"id": "app_new", "name": kwargs["name"]})
    monkeypatch.setattr(execution, "estimate", lambda app_id, **kwargs: calls.append(("estimate", app_id, kwargs["plan_task_id"])) or {
        "ready": True, "ceiling_micro": 1_500_000, "quote_id": "bq_one", "input_digest": "digest_one"})
    monkeypatch.setattr(execution, "start", lambda app_id, **kwargs: calls.append(("start", app_id, kwargs["approve_budget_micro"])) or {"build_run_id": "bld_one"})
    reply = await _dispatch_intent(vid, ChatContext(venture={"id": vid}),
                                   "start_product_build", "build my landing page", None, LOCAL_PERSON_ID)
    assert reply["kind"] == "text" and not any(action.get("confirmation") for action in reply["actions"])
    assert calls == [("app", "churn-guard"), ("estimate", "app_new", "pt_land"), ("start", "app_new", 1_500_000)]
    assert 'credit' not in reply['body_md'].lower()
    assert "say" not in reply["body_md"].lower().split("build")[0]  # no "say 'build my startup' again"


@pytest.mark.asyncio
@pytest.mark.hosted_only
async def test_several_build_steps_become_option_buttons_never_a_question(venture, approved):
    _, vid, _ = venture
    from app.workspaces.resolve import LOCAL_PERSON_ID
    from app.chat.context import ChatContext
    from app.chat.router import _prepare_intent

    approved["plan"] = _plan(("Landing page", "landing", "pt_land", "Build landing page"),
                             ("Core product", "core", "pt_core", "Build first workflow"))
    ctx = ChatContext(venture={"id": "ven_ours"})
    reply = await _prepare_intent("ven_ours", ctx, "start_product_build", "Kick off the build", None, "person_one")
    assert [a["action"] for a in reply["actions"]] == ["build.prepare", "build.prepare"]
    assert [a["payload"]["plan_task_id"] for a in reply["actions"]] == ["pt_land", "pt_core"]
    assert "tell me" not in reply["body_md"].lower()
    # A landing-page request picks the landing step itself.
    landing = await _prepare_intent("ven_ours", ctx, "start_product_build",
                                     "Kick off the landing page build", None, "person_one")
    assert landing["kind"] == "confirmation"
    assert landing["actions"][0]["confirmation"]["resulting_state"]["plan_task_id"] == "pt_land"
    # An option button (build.prepare with a task id) gives that step's card.
    from app.chat.execute import _execute_action

    picked = await _execute_action(vid, "build.prepare", payload={"plan_task_id": "pt_core"},
                                   actor_subject=LOCAL_PERSON_ID)
    assert picked["actions"][0]["confirmation"]["resulting_state"]["plan_task_id"] == "pt_core"


@pytest.mark.asyncio
async def test_confirming_creates_the_app_quotes_and_starts_once(venture, approved, monkeypatch):
    _, vid, _ = venture
    from app.builder import execution
    from app.chat.execute import _execute_action
    from app.infra import apps
    from app.workspaces.resolve import LOCAL_PERSON_ID

    calls = []
    monkeypatch.setattr(apps, "create_draft", lambda **kw: calls.append(("app", kw["slug"])) or {"id": "app_new", "name": kw["name"]})
    monkeypatch.setattr(execution, "estimate", lambda app_id, **kw: calls.append(("estimate", app_id, kw["plan_task_id"])) or {
        "ready": True, "ceiling_micro": 1_500_000, "quote_id": "bq_one", "input_digest": "d1", "reasons": []})
    monkeypatch.setattr(execution, "start", lambda app_id, **kw: calls.append(("start", app_id, kw["approve_budget_micro"])) or {
        "build_run_id": "bld_one"})
    payload = {"create_app": {"name": "Churn Guard", "slug": "churn-guard"}, "plan_id": "pln_one",
               "plan_task_id": "pt_land", "ceiling_micro": 1_500_000}
    with pytest.raises(PermissionError):
        await _execute_action(vid, "build.start", payload=payload)
    assert calls == []
    from app.chat import execute
    monkeypatch.setattr(execute, "_workspace_id", lambda _v: "wsp_ours")
    reply = await _execute_action(vid, "build.start", payload=payload, actor_subject=LOCAL_PERSON_ID)
    assert calls == [("app", "churn-guard"), ("estimate", "app_new", "pt_land"), ("start", "app_new", 1_500_000)]
    assert "Build started" in reply["body_md"] and "run=bld_one" in reply["actions"][0]["href"]

    # A changed price never starts anything.
    calls.clear()
    monkeypatch.setattr(execution, "estimate", lambda app_id, **kw: {
        "ready": True, "ceiling_micro": 2_000_000, "quote_id": "bq_two", "input_digest": "d2", "reasons": []})
    monkeypatch.setattr(apps, "list_apps", lambda _id: [{"id": "app_new", "name": "Churn Guard", "build_mode": "supymem"}])
    changed = await _execute_action(vid, "build.start", payload=payload, actor_subject=LOCAL_PERSON_ID)
    assert not any(c[0] == "start" for c in calls) and "price changed" in changed["body_md"]


@pytest.mark.hosted
def test_planned_landing_card_keeps_managed_build_validation(monkeypatch):
    from app.chat.router import _planned_landing_card
    from app.planning import store

    monkeypatch.setattr(store, "list_current_tasks", lambda _v: [{
        "id": "tsk_1", "source": "approved_plan:pln_one", "title": "Build landing page",
        "tags": ["capability:build.run", "plan_task:pt_land"]}])
    reply = _planned_landing_card("ven_ours", "build my landing page")
    assert reply["kind"] == "navigation" and "confirm" in reply["body_md"].lower()
    assert any(action.get("action") == "idea.review" for action in reply["actions"])
    assert reply["actions"][0]["href"] == "/v/ven_ours/builds?task=pt_land"
    assert not any(action.get("action") == "work.start" or action.get("confirmation") for action in reply["actions"])


def test_yes_to_a_build_offer_routes_to_the_build_chain(monkeypatch):
    from app.chat import router, store

    offer = {"role": "assistant", "body_md": "Ready to kick off that landing page build?", "actions": []}
    monkeypatch.setattr(store, "list_messages", lambda *_a, **_k: [offer])
    for yes in ("yes", "Yes, kick it off", "yep go ahead", "sure!"):
        assert router._build_offer_affirmed("ven_ours", yes) == "Kick off the landing page build"
    assert router._build_offer_affirmed("ven_ours", "yes but what does it cost") is None
    monkeypatch.setattr(store, "list_messages", lambda *_a, **_k: [
        {"role": "assistant", "body_md": "Want a summary of the plan?", "actions": []}])
    assert router._build_offer_affirmed("ven_ours", "yes") is None


# ------------------------------------------------------------------ 1b: claim guard

def test_claims_of_unrun_actions_become_an_honest_offer():
    from app.chat import claims

    said = "Great, I'll kick it off now. Now starts the build of your landing page."
    assert claims.claims_action(said)
    assert claims.claims_action("I've published your landing page.")
    assert not claims.claims_action("Ready to kick off that landing page build? Tap the button.")
    assert not claims.claims_action("I can start it once you confirm. I did not start anything.")
    reply = claims.honest_reply("ven_x", said)
    assert reply["actions"][0]["action"] == "build.prepare"
    assert "not started" in reply["body_md"] or "have not started" in reply["body_md"]
    assert not claims.claims_action(reply["body_md"])


@pytest.mark.parametrize("body", [
    "I built your page.", "I've deployed your site.", "I published the launch plan.",
    "I built the page after you asked for advice.", "I deployed the page before checking it.",
    "I have built your page, if you want to inspect it.",
])
def test_completed_action_claims_are_still_blocked(body):
    from app.chat import claims
    assert claims.claims_action(body)


@pytest.mark.parametrize("body", [
    "Build a landing page, deploy it, then publish your launch post.",
    "I recommend building the landing page before launching.",
    "I'll start with the launch plan: build a waitlist, then interview users.",
    "If you approve, I will build the page.", "I'll deploy the page once you confirm.",
])
def test_build_advice_is_not_an_execution_claim(body):
    from app.chat import claims
    assert not claims.claims_action(body)


def test_detailed_launch_plan_survives_the_actual_chat_claim_guard(venture, monkeypatch):
    client, vid, _ = venture
    from app.chat.handlers import general
    from app.chat import router
    body = "I'll start with a detailed launch plan:\n" + "\n".join(
        f"{index}. Build a waitlist, interview users, then publish what you learned." for index in range(1, 11))
    async def advice(*args, **kwargs):
        return {"kind": "text", "body_md": body, "actions": [], "citations": []}
    monkeypatch.setattr(general, "handle", advice)
    monkeypatch.setattr(router, "split_speech_acts", lambda text: [("general", text)])
    monkeypatch.setattr("app.chat.idea_bridge.written_request", lambda _text: False)  # the claim guard, not the writing path
    reply = chat(client, vid, "Write me a detailed 10-point launch plan")
    assert "10. Build a waitlist" in reply["body_md"]
    assert "I have not started the build" not in reply["body_md"]


def test_chat_never_relays_a_model_claim_of_a_build(venture, monkeypatch):
    client, vid, _ = venture
    from app.chat.handlers import general

    async def liar(ctx, text, speaker=None, request_id=None):
        return {"kind": "text", "body_md": "Great! I'll kick it off now. Now starts the build.",
                "actions": [], "citations": []}

    monkeypatch.setattr(general, "handle", liar)
    reply = chat(client, vid, "sounds interesting, hmm")
    assert "kick it off now" not in reply["body_md"]
    assert reply["actions"] == []


@pytest.mark.parametrize("body", [
    "I prepared a Mac task for review. Open the review to continue.",
    "I've already prepared your read-only background browser task.",
    "I am preparing a saved work order.",
    "I prepared a build quote.",
])
def test_durable_prepared_claims_need_an_actual_review_receipt(body):
    from app.chat import claims
    assert claims.claims_action(body)
    reply = claims.honest_reply("ven_x", body)
    assert "This reply has not prepared a new saved task review" in reply["body_md"]
    assert "Existing tasks keep their saved state" in reply["body_md"]
    assert not claims.claims_action(reply["body_md"])
    assert reply["actions"] == []


@pytest.mark.parametrize("body", [
    "I prepared a draft below for you to edit.",
    "I prepared an outline of the research task.",
    "I prepared a task list below for your review.",
    "I prepared a task outline you can edit.",
    "I prepared your requested task description below.",
    "I prepared a build quote example below.",
    "I can prepare a Mac task once its supported scope is known.",
    "I did not prepare a task.",
    "Did you prepare a Mac task?",
])
def test_inline_writing_advice_and_negative_preparation_are_not_effect_claims(body):
    from app.chat import claims
    assert not claims.claims_action(body)


def test_actual_chat_replaces_model_only_prepared_claim_without_inventing_review(venture, monkeypatch):
    client, vid, _ = venture
    from app.chat.handlers import general
    from app.chat.store import list_messages

    async def liar(ctx, text, speaker=None, request_id=None):
        return {"kind": "text", "body_md": "I prepared a read-only browser task for review.",
                "actions": [], "citations": []}
    monkeypatch.setattr(general, "handle", liar)
    reply = chat(client, vid, "sounds interesting, hmm")
    assert "This reply has not prepared a new saved task review" in reply["body_md"]
    assert reply["kind"] == "text" and reply["actions"] == []
    assert not any(row["kind"] == "confirmation" for row in list_messages(vid))


# ------------------------------------------------------------------ 2 + 6 + 8: failed jobs

def test_retry_validation_starts_directly(venture, monkeypatch):
    client, vid, wid = venture
    add_job(vid, wid, "idea.validate", "validation_invalid_output",
            {"session_id": "ids_one", "revision_id": "rev_one"})
    from app.idea import validation

    started = []
    monkeypatch.setattr(validation, "start_validation", lambda sid, **kw: started.append((sid, kw["revision_id"])) or {})
    done = chat(client, vid, "retry validation")
    assert card_of(done) is None and started == [("ids_one", "rev_one")]
    assert "running again" in done["body_md"] and "credit" not in done["body_md"].lower()


def test_retry_research_starts_directly_and_try_again_picks_the_latest(venture, monkeypatch):
    client, vid, wid = venture
    add_job(vid, wid, "research.deep", "research_invalid_output")
    confirm_idea(vid)
    started = []
    monkeypatch.setattr("app.chat.build_path.research_prerequisites", lambda venture_id: None)
    monkeypatch.setattr("app.chat.execute._research_start", lambda venture_id, actor_subject=None, expected_revision_id=None: started.append(venture_id) or {
        "kind": "text", "body_md": "Your research is queued.", "actions": [], "action_started": True})
    for message in ("retry research", "try again"):
        reply = chat(client, vid, message)
        assert card_of(reply) is None and "credit" not in reply["body_md"].lower()
    assert started == [vid, vid]


def test_retry_with_nothing_failed_is_honest(venture):
    client, vid, _ = venture
    reply = chat(client, vid, "try again")
    assert "no failed" in reply["body_md"] and not card_of(reply)


def test_why_did_validation_fail_names_the_real_error_and_offers_retry(venture):
    client, vid, wid = venture
    add_job(vid, wid, "idea.validate", "validation_invalid_output", {"session_id": "ids_one", "revision_id": "rev_one"})
    add_job(vid, wid, "research.deep", "research_provider_rate_limited")
    reply = chat(client, vid, "why did validation fail?")
    assert "returned something I could not use" in reply["body_md"]
    assert "not recorded" not in reply["body_md"]
    labels = {a["label"]: a for a in reply["actions"]}
    assert labels["Retry validation"]["action"] == "job.retry"
    explain = labels["Explain in detail"]["payload"]
    assert explain["job_kind"] == "idea.validate" and explain["job_code"] == "validation_invalid_output"
    detail = confirm(client, vid, "blocker.explain", explain).json()
    assert "returned something I could not use" in detail["body_md"]
    assert any(a.get("action") == "job.retry" for a in detail["actions"])
    assert "validation_invalid_output" not in detail["body_md"]


def test_failure_posts_use_the_real_code_charlie_and_a_retry_button(venture):
    from app.core.local_mode import is_local
    from app.chat import milestone_hooks
    from app.state import db
    from app.chat.store import list_messages

    client, vid, _ = venture
    with db()() as session:
        milestone_hooks.failed(session, vid, "research.deep", "job_r1", "research_invalid_output")
        milestone_hooks.failed(session, vid, "plan.compile", "job_p1", "plan_inputs_changed")
        session.commit()
    msgs = [m for m in list_messages(vid) if m["kind"] in {"research_failed", "plan_failed"}]
    assert len(msgs) == 2
    for m in msgs:
        assert m["body_md"].startswith("<!--employee:") and ":Manager:" in m["body_md"]  # Charlie, not "Team"
        assert "research_invalid_output" not in m["body_md"] and "plan_inputs_changed" not in m["body_md"]
        assert any(action.get("action") == "job.retry" for action in m["actions"])
        explanation = next(a for a in m["actions"] if a.get("action") == "blocker.explain")
        assert explanation["payload"]["job_code"] in {"research_invalid_output", "plan_inputs_changed"}
    if is_local():
        assert msgs[0]["actions"][0]["href"] == f"/v/{vid}/research"
        assert "could not verify the review against your sources" in msgs[0]["body_md"]
    else:
        assert "returned a result I could not use" in msgs[0]["body_md"]
    assert "changed while the plan" in msgs[1]["body_md"]


def test_every_assistant_line_is_spoken_by_the_manager(venture):
    from app.chat.store import append_message

    _, vid, _ = venture
    saved = append_message(vid, role="assistant", body_md="Plain line with no speaker.")
    assert saved["body_md"].startswith("<!--employee:")


def test_raw_error_codes_are_humanized_in_any_chat_line(venture):
    from app.chat.blockers import humanize_codes
    from app.chat.store import append_message

    _, vid, _ = venture
    out = humanize_codes("The Mac task stopped.\ndesktop_app_launch_unconfirmed")
    assert "desktop_" not in out and "could not confirm" in out
    assert "desktop_" not in humanize_codes("desktop_some_new_thing failed")
    saved = append_message(vid, role="assistant", body_md="Stopped: desktop_app_launch_unconfirmed")
    assert "desktop_app_launch_unconfirmed" not in saved["body_md"]


@pytest.mark.parametrize("identifier", ["https://published.warehouse.example/sent", "https://sent.family-trails.example/published",
    "published@warehouse.example", "sent@family-trails.example"])
def test_publication_claim_guard_preserves_identifiers_without_confirming_unsaved_work(identifier):
    from types import SimpleNamespace
    from app.chat.router import scrub_hallucinations

    context = SimpleNamespace(governance={"autonomy": "L1"}, website={"published": False})
    body = scrub_hallucinations(f"I published your page at {identifier}. Nothing was sent.", context)
    assert body == f"I prepared your page at {identifier}. Nothing was sent."


def test_desktop_and_research_codes_have_plain_copy():
    from app.chat.blockers import humanize_error

    for code in ("research_invalid_output", "desktop_app_launch_unconfirmed", "desktop_cua_session_unavailable",
                 "plan_invalid_output", "validation_invalid_output", "desktop_cua_effect_uncertain_insert"):
        assert "_" not in humanize_error(code), code


# ------------------------------------------------------------------ 3: run the test from the review

@pytest.mark.parametrize("audience,signal", [("Gym owners", "manual churn alert"), ("Home cooks", "pantry reminder")])
def test_run_the_test_from_the_review_is_a_card_that_starts_the_experiment(venture, audience, signal):
    from app.core.local_mode import is_local
    from app.core.local_secrets import update_settings
    from app.experiments import ledger

    client, vid, _ = venture
    if is_local():
        update_settings("capabilities", {"run_for_me_experiments": True}, {})
    confirm_idea(vid)
    ledger.create_from_tests(vid, "validation", [{
        "hypothesis": f"{audience} will act on a {signal}",
        "action": f"Send {audience} a {signal}", "success_measure": "They act on the reminder"}])
    text = f"Let's run the test from the review: send a {signal} to {audience} and see if they act"
    reply = chat(client, vid, text)
    assert card_of(reply) is None and "experiment:" in reply["body_md"]
    assert f"{audience} will act on a {signal}" in reply["body_md"]
    assert "Nothing was sent, published or built." in reply["body_md"]
    assert "credit" not in reply["body_md"].lower()
    rows = ledger.list_for_venture(vid)
    assert len(rows) == 1, reply["body_md"]
    (row,) = rows
    assert row["state"] == "proposed"


@pytest.mark.parametrize("audience", ["Gym owners", "Home cooks"])
def test_run_the_test_creates_the_experiment_from_the_saved_validation_report(venture, monkeypatch, audience):
    from app.chat import experiments
    from app.core.local_mode import is_local
    from app.core.local_secrets import update_settings
    from app.experiments import ledger

    client, vid, _ = venture
    if is_local():
        update_settings("capabilities", {"run_for_me_experiments": True}, {})
    confirm_idea(vid)
    monkeypatch.setattr(experiments, "_saved_validation_tests", lambda _v: [{
        "hypothesis": f"Five {audience} will reply to a reminder", "action": f"Email five {audience}",
        "success_measure": "Two reply"}])
    assert ledger.list_for_venture(vid) == []
    reply = chat(client, vid, "Let's run the test from the review")
    assert card_of(reply) is None and "credit" not in reply["body_md"].lower()
    rows = ledger.list_for_venture(vid)
    assert len(rows) == 1, reply["body_md"]
    (row,) = rows
    from app.core.local_mode import is_test_mode
    assert row["state"] == "running"
    assert row["source"] == ("Sample" if is_test_mode() else "validation")
    assert row["hypothesis"] == f"Five {audience} will reply to a reminder"


# ------------------------------------------------------------------ 4: regenerate the plan

QUOTE = {"provider": "openai", "model": "m1", "payer": "platform_credits", "ceiling_micro": 250_000,
         "ceiling_credits": 25, "available_micro": 9_000_000, "sufficient_now": True}


@pytest.fixture()
def draft(monkeypatch):
    from app.planning import compiler

    state = {"findings": [{"code": "unconfirmed_price"}], "starts": 0}
    monkeypatch.setattr(compiler, "generation_quote", lambda *_a, **_k: QUOTE)
    monkeypatch.setattr(compiler, "list_plans", lambda vid: [{"id": "pln_1", "state": "founder_review", "version": 1}])
    monkeypatch.setattr(compiler, "get_plan", lambda pid: {"review_findings": state["findings"], "reviewable": True,
                                                            "stale_dependencies": False})
    monkeypatch.setattr(compiler, "start", lambda *_a, **_k: state.update(starts=state["starts"] + 1) or {"job_id": "j"})
    return state


def test_a_draft_with_blocking_findings_offers_a_regenerate_card(venture, draft):
    client, vid, _ = venture
    reply = chat(client, vid, "regenerate the plan")
    assert card_of(reply) is None and "credit" not in reply["body_md"].lower()
    assert "did not generate another" not in reply["body_md"]
    assert draft["starts"] == 1


def test_a_clean_waiting_draft_still_blocks_a_second_generation_unless_asked(venture, draft):
    client, vid, _ = venture
    draft["findings"] = []
    plain = chat(client, vid, "make the plan")
    assert "already waiting" in plain["body_md"] and not card_of(plain)
    assert not any(a.get("payload") == {"regenerate": True} for a in plain["actions"])
    asked = chat(client, vid, "regenerate the plan")  # an explicit ask always gets the card
    assert card_of(asked) is None and draft["starts"] == 1


# ------------------------------------------------------------------ 5: build before publish

@pytest.mark.hosted_only
def test_waitlist_next_step_is_build_when_nothing_is_built(monkeypatch):
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    from app.chat import site_actions
    from app.pagebuilder.api import enabled

    monkeypatch.setattr(site_actions, "publish_preflight", lambda _v: (None, None, [("x", {}, "no_hosted_app")]))
    text, actions = site_actions.after_waitlist("ven_x")
    assert "build your landing page" in text and "publish" not in text.replace("nothing to publish", "")
    assert actions[0]["action"] == ("pagebuilder.build" if enabled() else "build.prepare") and actions[0]["label"] == "Build landing page"
    monkeypatch.setattr(site_actions, "publish_preflight", lambda _v: ("app_1", "bld_1", []))
    text, actions = site_actions.after_waitlist("ven_x")
    assert text == "Next: publish." and actions[0]["confirmation"]["action"] == "site.publish"


@pytest.mark.hosted_only
def test_publish_without_a_build_offers_the_build_button(monkeypatch):
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    from app.chat import site_actions
    from app.core.local_mode import is_local

    monkeypatch.setattr(site_actions, "publish_preflight", lambda _v: (None, None, [
        ("There is no hosted app", site_actions.build_landing_button(), "no_hosted_app")]))
    reply = site_actions.route("ven_x", "site.publish", "publish my page")
    assert reply["actions"][0]["action"] == ("site.publish" if is_local() else "build.prepare")
    if is_local():
        assert reply["actions"] == [{**site_actions.publish_card("ven_x"), "style": "primary"}]
    else:
        assert not any(action.get("confirmation") for action in reply["actions"])


# ------------------------------------------------------------------ 7: next step

def test_next_step_says_validation_failed_and_offers_retry():
    from app.ventures import next_step

    facts = {"paused": False, "gtm_entry": False, "first_question_pending": False, "active_jobs": False,
             "personal": False, "idea_confirmed": False, "idea_state": "validating", "validation_done": False,
             "question_pending": False, "validation_failed": True}
    step = next_step._resolve("ven_x", "/v/ven_x", facts)
    assert step["key"] == "idea.validation_failed" and "did not finish" in step["title"]
    assert "being validated" not in step["title"] + step["detail"]
    assert (step["cta_label"], step["chat_action"], step["payload"]) == (
        "Retry validation", "job.retry", {"kind": "idea.validate"})
    assert next_step._resolve("ven_x", "/v/ven_x", {**facts, "validation_failed": False})["key"] == "idea.validating"


# ------------------------------------------------------------------ 9: publish sees the accepted build on any app

def test_chat_publish_finds_the_accepted_build_on_a_later_app(venture, monkeypatch):
    from app.chat import site_actions
    from app.conversion import waitlist
    from app.db.supymem_models import BuildRun, FounderApp
    from app.hosting import service
    from app.state import db
    from core.clock import utcnow

    _, vid, wid = venture
    with db()() as session:
        for app_id, slug in (("app_old", "old-draft"), ("app_live", "gyms-live")):
            session.add(FounderApp(id=app_id, workspace_id=wid, venture_id=vid, name=slug, slug=slug,
                                   kind="web", created_at=utcnow(), updated_at=utcnow()))
        session.flush()
        session.add(BuildRun(id="bld_ok", app_id="app_live", workspace_id=wid, venture_id=vid, trigger="test",
                             agent="managed", state="succeeded", spec="{}", runtime_version=1, created_at=utcnow()))
        session.commit()

    def accepted(session, app, build_id):
        if build_id != "bld_ok":
            raise service.HostingError("hosting_build_not_accepted")
        return (None,) * 5

    monkeypatch.setattr(service, "_accepted_source", accepted)
    monkeypatch.setattr(waitlist, "status", lambda *_a, **_k: {"enabled": True, "submission_verified": True})
    app_id, build_id, blockers = site_actions.publish_preflight(vid)
    assert (app_id, build_id, blockers) == ("app_live", "bld_ok", [])
    reply = site_actions.route(vid, "site.publish", "publish my landing page")
    assert reply["kind"] == "confirmation" and reply["actions"][0]["confirmation"]["action"] == "site.publish"


# ------------------------------------------------------------------ 10: drafting needs no posting connection

def test_drafting_outreach_for_review_needs_no_posting_connection(venture):
    import asyncio

    from app.chat.store import list_messages
    from app.planning.execute import _founder_gate, execute_planned_task
    from app.planning.store import enqueue_task

    _, vid, _ = venture
    title = "go - draft the outreach emails for me to review"
    assert _founder_gate(title, ["chat"], vid, title) is None
    task = enqueue_task(vid, {"title": title, "detail": title, "tags": ["chat"], "runnable": True}, source="chat")
    out = asyncio.run(execute_planned_task(vid, task["id"]))
    assert out["status"] == "done"
    text = "\n".join(m["body_md"] for m in list_messages(vid))
    assert "Connect posting" not in text and "Draft 1" in text and "Nothing was sent" in text
    assert _founder_gate("Post to LinkedIn about the launch", ["chat"], vid, "") is not None  # posting still needs it


@pytest.mark.parametrize("title,detail", [
    ("Build the free website; Stripe payments are out of scope.", ""),
    ("Build the website with Razorpay disabled for now.", ""),
    ("Create a pricing page for a paid offer; payment collection is disabled.", ""),
    ("Build the core product", "Razorpay payments remain disabled; do not implement checkout."),
    ("Build and publish the ordinary website", ""),
])
def test_optional_customer_payments_do_not_block_base_product_work(title, detail):
    from app.planning.execute import _founder_gate

    assert _founder_gate(title, [], detail=detail) is None


@pytest.mark.parametrize("title,provider", [
    ("Connect Stripe payment account; the website can work without payments.", "stripe"),
    ("Enable Razorpay payments for an approved paid offer", "razorpay"),
    ("Collect customer payments for an approved paid offer", "payments"),
])
def test_actual_payment_tasks_stay_held_for_separate_merchant_approval(title, provider):
    from app.planning.execute import _founder_gate

    gate = _founder_gate(title, [])
    assert gate["provider"] == provider and "stays disabled" in gate["why"]


# ------------------------------------------------------------------ 11: offered next actions carry buttons

@pytest.mark.parametrize("venture", ["Inventory alerts for warehouse operators", "Walking groups for neighborhood families"], indirect=True)
def test_a_reply_that_offers_next_actions_has_the_matching_buttons(venture, monkeypatch):
    from types import SimpleNamespace

    from app.chat import assistant
    from app.chat.intent import classify_intent
    from app.credits import metered

    client, vid, _ = venture
    ask = "The page is live. Help me get my first 10 gym owners onto the waitlist."
    assert classify_intent(ask) == "assistant"

    async def answer(_vid, _question, **_kw):
        return {"kind": "text", "citations": [], "actions": [],
                "body_md": "Your landing page is live and the waitlist is open. I can draft outreach emails for your review."}

    async def no_model_output(*_args, **kwargs):
        assert kwargs["venture_id"] == vid and kwargs["workspace_id"] == venture[2]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=""))])

    monkeypatch.setattr(assistant, "answer_question", answer)
    monkeypatch.setattr(metered, "llm", no_model_output)
    reply = chat(client, vid, ask)
    by_action = {action.get("action"): action for action in reply["actions"]}
    assert "outreach.draft" not in by_action
    assert "gtm.diagnosis" not in by_action
    confirm_idea(vid)
    understood = chat(client, vid, ask)
    by_action = {action.get("action"): action for action in understood["actions"]}
    assert by_action["outreach.draft"]["style"] == "primary"
    assert "gtm.diagnosis" in by_action
    assert [action.get("action") for action in understood["actions"] if action.get("style") == "primary"] == ["outreach.draft"]
    prepared = confirm(client, vid, "outreach.draft", {}).json()
    assert card_of(prepared) is None and "credit" not in prepared["body_md"].lower()
    from app.autopilot.outreach import drafts
    from app.db.models import GtmContact, GtmTouch
    from app.state import db
    from core.clock import utcnow
    from sqlalchemy import select

    # A personal batch needs an existing scoped recipient; it cannot invent one.
    with db()() as session:
        session.add(GtmContact(id="gtmc_known_review", workspace_id=venture[2],
            email="owner@example.test", email_confidence="risky",
            source=f"venture:{vid}:research", dossier_hook="Published business contact on example.test",
            enrichment_json=json.dumps({"business": "Known Gym"}),
            stage_changed_at=utcnow(), created_at=utcnow()))
        session.commit()
    done = confirm(client, vid, "outreach.draft", {}).json()

    assert "Drafted" in done["body_md"] and "Nothing is sent yet" in done["body_md"]
    saved = drafts(vid, "draft", "manual")
    assert saved and all(d["body"] and d["to"] == "owner@example.test" for d in saved)
    with db()() as session:
        assert not session.scalar(select(GtmTouch.id).where(GtmTouch.venture_id == vid))


def test_unrelated_replies_get_no_offer_buttons():
    from app.chat import offers

    assert offers.offer_buttons("what is my email address?", "It is on the Settings page.") == []
    assert offers.offer_buttons("thanks", "You're welcome.") == []
    assert offers.offer_buttons("what is my email address?", "I can draft outreach emails.") == []
    assert offers.offer_buttons("How should I find customers? Answer only.", "I can draft outreach emails.") == []

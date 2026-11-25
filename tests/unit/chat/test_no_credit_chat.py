import asyncio
from datetime import datetime, timedelta
import json
from types import SimpleNamespace

import pytest

from tests.company_os_harness import load_company_os_app, local_chat as local_chat
from tests.unit.chat.test_customers_in_chat import journey_db as journey_db, outreach_email_chat as outreach_email_chat


@pytest.mark.parametrize("audience", ["Factory purchasing teams", "Family garden clubs"])
@pytest.mark.parametrize("ready", [False, True])
def test_local_background_alias_uses_the_same_owner_switch(monkeypatch, audience, ready):
    load_company_os_app()
    from app.chat.actions import beta_blocked, disabled_reason
    from app.core import local_capabilities

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    next_step = "Open Settings > Features and turn on Background work."
    checked = []
    def enabled(name):
        checked.append(name)
        return ready
    monkeypatch.setattr(local_capabilities, "enabled", enabled)
    monkeypatch.setattr(local_capabilities, "blocked_message", lambda name: next_step)
    for action in ("autopilot.god_on", "god_mode.request"):
        reply = beta_blocked(action, audience)
        assert (reply is None) if ready else reply["body_md"] == next_step
        assert disabled_reason(action) == (None if ready else next_step)
    assert checked == ["background_work"] * 4


@pytest.mark.parametrize("audience", ["Factory purchasing teams", "Family garden clubs"])
def test_local_saved_memory_commands_are_not_dormant_gates(monkeypatch, audience):
    load_company_os_app()
    from app.chat.actions import disabled_reason, get_action

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    for action in ("memory.remember", "memory.correct", "memory.forget", "memory.list"):
        assert get_action(action).required_permission and disabled_reason(action) is None, audience


@pytest.mark.parametrize("audience", ["Factory purchasing teams", "Family garden clubs"])
def test_saved_experiment_limit_cards_hide_usage_and_keep_exact_server_inputs(audience):
    load_company_os_app()
    from app.chat.store import _PARTS, _ser

    state = {"experiment_id": "exp_saved", "cap_micro": 1_000_000}
    card = {"action": "experiment.spend_cap", "label": "Raise to $1", "confirmation": {
        "action": "experiment.spend_cap", "interpretation": f"Raise the cap for {audience} to $1.",
        "consequences": ["Your experiment can spend $1. Work already in progress may use money set aside for it."],
        "resulting_state": state}}
    body = f"Your experiment for {audience} has $0.05 left under its $0.30 cap. The next run needs up to $0.10."
    row = SimpleNamespace(id="chat_cap_saved", workspace_id="wsp_saved", conversation_id="conversation_saved", role="assistant", kind="confirmation",
                          body_md=body, actions_json=json.dumps([card]),
                          citations_json=json.dumps([{_PARTS: [{"type": "text", "text": body}]}]), created_at=datetime.now())
    reply = _ser(row)
    assert "$" not in json.dumps([reply["body_md"], reply["parts"], reply["actions"][0]["label"],
                                  reply["actions"][0]["confirmation"]["interpretation"],
                                  reply["actions"][0]["confirmation"]["consequences"]])
    assert "Change the limit or stop" in reply["body_md"]
    assert reply["actions"][0]["label"] == "Change experiment limit"
    assert reply["actions"][0]["confirmation"]["resulting_state"] == state
    row.kind = "text"
    row.body_md = "Your cap is now $1. $0.20 is spent so far. Run it again when you are ready."
    row.actions_json = json.dumps([{"action": "experiment.prepare", "payload": {"experiment_id": state["experiment_id"]}}])
    row.citations_json = json.dumps([{_PARTS: [{"type": "text", "text": row.body_md}]}])
    completed = _ser(row)
    assert completed["body_md"] == "Your experiment setting is saved. Run it again when you are ready."
    assert "$" not in json.dumps(completed["parts"])
    assert completed["actions"][0]["payload"] == {"experiment_id": state["experiment_id"]}
    row.body_md = f"Your experiment for {audience} has an updated limit of $1 and has used $0.20. Run it again when you are ready."
    row.citations_json = json.dumps([{_PARTS: [{"type": "text", "text": row.body_md}]},
                                    {"confirmed_action": {"action": "experiment.spend_cap", "state": "started"}}])
    assert _ser(row)["body_md"] == completed["body_md"]


@pytest.mark.parametrize("audience", ["Factory purchasing teams", "Family garden clubs"])
def test_saved_credit_blockers_give_one_buy_more_notice_without_starting_work(monkeypatch, audience):
    load_company_os_app()
    from app.chat import execute
    from app.chat.confirm import ConfirmationCard, visible_action
    from app.chat.confirmations import start_requested_work
    from app.chat.store import _ser
    from app.credits.errors import LIMIT_MESSAGE

    async def forbidden(*args, **kwargs):
        pytest.fail("Blocked work must not start or charge")

    monkeypatch.setattr(execute, "execute_action", forbidden)
    for reason in ("Your available credits do not cover this work.", "research_insufficient_credits"):
        card = visible_action(ConfirmationCard("research.start", "cap.write.research", audience).as_action() | {"disabled_reason": reason})
        reply = {"kind": "confirmation", "body_md": audience, "actions": [card]}
        blocked = asyncio.run(start_requested_work("venture", reply, "owner", actions={"research.start"}))
        assert blocked["body_md"] == LIMIT_MESSAGE and blocked["actions"][0]["label"] == "Buy more"
        row = SimpleNamespace(id="saved", workspace_id="wsp_saved", conversation_id=None, role="assistant", kind="confirmation", body_md=audience,
                              actions_json=json.dumps([card]), citations_json="[]", created_at=datetime.now())
        saved = _ser(row)
        assert saved["body_md"] == LIMIT_MESSAGE and saved["actions"][0]["href"] == "/account#account-buy"
    row.role, row.kind, row.actions_json = "assistant", "text", "[]"
    row.body_md = f"Not enough credits for {audience}."
    assert _ser(row)["body_md"] == LIMIT_MESSAGE
    assert _ser(row)["actions"][0]["label"] == "Buy more"
    row.role = "user"
    assert _ser(row)["body_md"] == row.body_md


@pytest.mark.parametrize("audience", ["Factory purchasing teams", "Family garden clubs"])
def test_customer_navigation_and_questions_do_not_start_suggested_searches(monkeypatch, audience):
    load_company_os_app()
    from app.chat import router, confirmations
    from app.chat.confirm import ConfirmationCard

    reply = {"actions": [ConfirmationCard("gtm.find", "cap.write.research", audience).as_action()]}
    calls = []

    async def prepare(*args, **kwargs):
        return reply

    async def start(*args, **kwargs):
        calls.append(kwargs["actions"])
        return {"action_started": True}

    monkeypatch.setattr(router, "_prepare_intent", prepare)
    monkeypatch.setattr(confirmations, "start_requested_work", start)
    for message in ("show my leads", "how many leads", "who should I contact", "go to market"):
        assert asyncio.run(router._dispatch_intent("venture", SimpleNamespace(), "gtm_review", message, None)) == reply
    assert calls == []
    for request in ("find customers", "please find customers", "I want you to find customers", "skip the plan and get customers"):
        assert asyncio.run(router._dispatch_intent("venture", SimpleNamespace(), "gtm_review", f"{request} for {audience}", None))["action_started"]
    assert calls == [{"gtm.find"}] * 4


@pytest.mark.parametrize("step", ["Review your invoice drafts", "Find people planning family walks"])
def test_saved_blocker_hides_internal_status_without_changing_founder_copy(step):
    load_company_os_app()
    from app.chat.store import _clean
    from app.needs_you.templates import blocker_message

    body = f"**Supymem needs you**\n\n{step}.\n\nSaved background status: This step stopped unexpectedly.\n"
    clean = _clean(body, assistant=True)
    assert step in clean and "Saved background status" not in clean and "stopped unexpectedly" not in clean
    assert "Saved background status" in _clean(body, assistant=False)
    blocker = blocker_message([{"title": step, "what_we_need": "Allow sending from your Gmail?", "how_to_resume": "Review drafts first.", "reason_code": "consent_send"}])
    assert "Gmail?" in blocker and "Gmail?." not in blocker


def test_chat_copy_hides_usage_quotes_without_changing_internal_state():
    load_company_os_app()
    from app.chat.confirm import ConfirmationCard, visible_action, without_usage_copy
    from app.chat.store import _PARTS, _clean, _ser
    from app.chat.blockers import explain

    for venture_id, audience in [("ven_labs", "Clinical lab schedulers"), ("ven_trails", "Weekend hikers")]:
        state = {"ceiling_micro": 2_000_000, "audience": audience, "credit_estimate_micro": 27_600}
        card = ConfirmationCard("research.start", "cap.write.research", f"Research {audience}.",
                                ["Uses up to 200 credits for public searches.", "Nothing is sent or published."], state)
        row = SimpleNamespace(id="chat_saved", workspace_id="wsp_saved", conversation_id="conversation_saved", role="assistant", kind="text",
                              body_md=f"Your research for {audience} is ready.\n\nResearch costs at most 200 credits; unused credits stay yours.",
                              actions_json=json.dumps([card.as_action()]),
                              citations_json=json.dumps([{_PARTS: [{"type": "text", "text": f"Your research for {audience} is ready. Research costs at most 200 credits."}]}]),
                              created_at=datetime.now())
        reply = _ser(row)
        assert "credit" not in reply["body_md"].lower()
        assert "credit" not in json.dumps(reply["parts"]).lower()
        assert audience in reply["body_md"]
        assert reply["actions"][0]["confirmation"]["resulting_state"] == state
        assert reply["actions"][0]["confirmation"]["consequences"] == ["Nothing is sent or published."]
        for malformed_parts in (None, "invalid", {}):
            row.citations_json = json.dumps([{_PARTS: malformed_parts}])
            assert "credit" not in json.dumps(_ser(row)["parts"]).lower()
        assert _clean("You serve credit unions.\n\nYour audience is saved.", assistant=True) == "You serve credit unions.\n\nYour audience is saved."
        assert _clean("I need 200 credits for a loyalty program.", assistant=False) == "I need 200 credits for a loyalty program."
        blocked = visible_action({"label": "Start", "disabled_reason": "Your available credits do not cover this work.", "payload": state})
        assert blocked["disabled_reason"] and "credit" not in blocked["disabled_reason"].lower()
        assert blocked["payload"] == state
        assert visible_action({"label": "Start", "disabled_reason": ""})["disabled_reason"] == ""
        assert visible_action({"label": "Top up credits"})["label"] == "Continue"
        exhausted = explain(venture_id, {"code": "insufficient_credits"})
        assert "credit" not in exhausted["body_md"].lower() and "\n" not in exhausted["body_md"]
        assert exhausted["actions"][0]["href"] == f"/v/{venture_id}/settings#settings-credits"
        for quote in ("I will quote credits before you confirm.", "Exact credits quoted before you confirm.",
                      "Reserves up to 2.76 credits; the actual charge settles after the run.",
                      "This uses no new credits.", "This uses 1 credit.", "Model usage: $0.04."):
            clean = without_usage_copy(f"Your work for {audience} is ready. {quote}")
            assert "credit" not in clean.lower() and "$0.04" not in clean
        assert without_usage_copy(f"Cost: $40 for {audience}") == f"Cost: $40 for {audience}"


@pytest.mark.parametrize("audience", ["Wholesale inventory planners", "Family camping activities"])
def test_requested_search_resume_preserves_caps_without_runtime_settings(monkeypatch, audience):
    load_company_os_app()
    from app.chat import customers
    from app.core.local_mode import is_local
    from app.gtm.journey import auto_search_settings

    if is_local():
        from app.core.local_secrets import update_settings

        update_settings("capabilities", {"background_work": True}, {})
    saved = auto_search_settings({}) | {"enabled": False, "target": 19}
    monkeypatch.setattr(customers, "_view", lambda _venture: {"auto_search": saved, "icp": {"who": audience}})
    calls = []
    async def execute(venture, action, payload, actor):
        calls.append((action, payload, actor))
        return {"action_started": True}
    monkeypatch.setattr(customers, "execute", execute)
    reply = asyncio.run(customers.run_command("ven_requested", "resume_search", None, "resume finding customers", "person_owner"))
    expected = {key: saved[key] for key in ("target", "interval_days", "ceiling_micro")} | {"enabled": True}
    assert reply["action_started"] and calls == [("gtm.auto_search", {"auto_search": expected}, "person_owner")]
    assert saved["enabled"] is False


@pytest.mark.parametrize("audience", ["Hospital procurement teams", "Family cycling holidays"])
def test_approving_a_batch_does_not_request_sending(monkeypatch, audience):
    load_company_os_app()
    from app.chat import customers, execute, router
    from app.chat.confirm import ConfirmationCard

    calls = []
    reply = {"body_md": "Your reviewed batch is approved. Ask to send it when you are ready.", "actions": []}

    async def prepare(*args, **kwargs):
        return {"actions": [ConfirmationCard("outreach.send", "cap.write.outreach", audience,
            resulting_state={"batch_id": audience}).as_action()]}

    async def run(venture, action, *args, **kwargs):
        calls.append(action)
        return reply

    monkeypatch.setattr(router, "_prepare_intent", prepare)
    monkeypatch.setattr(execute, "execute_action", run)
    monkeypatch.setattr(customers, "execute", run)
    assert asyncio.run(router._dispatch_intent("venture", SimpleNamespace(), "jarvis.batch_approve",
        "approve this batch", None, "owner")) == reply
    assert calls == ["gtm.approve"]


@pytest.mark.parametrize("audience", ["Hospital procurement teams", "Family cycling holidays"])
def test_requested_external_work_starts_without_starting_suggestions(monkeypatch, audience):
    load_company_os_app()
    from app.chat import execute
    from app.chat.confirm import ConfirmationCard
    from app.chat.confirmations import start_requested_work

    calls = []

    async def run(venture_id, action, **kwargs):
        calls.append((action, kwargs["payload"]))
        return {"kind": "text", "body_md": "Your request is complete. Ask for your next step.", "action_started": True}

    monkeypatch.setattr(execute, "execute_action", run)
    for action in ("gtm.send", "site.publish", "site.unpublish", "social.post", "autopilot.allow_send"):
        state = {"audience": audience, "approval_digest": "saved_exact_review"}
        reply = {"kind": "confirmation", "actions": [ConfirmationCard(action, "cap.write.social_post", audience,
                 resulting_state=state).as_action()]}
        assert asyncio.run(start_requested_work("ven_requested", reply, "person_owner", actions=set())) == reply
        assert asyncio.run(start_requested_work("ven_requested", reply, "person_owner", actions={action}))["action_started"]
        assert calls[-1] == (action, state)
    assert len(calls) == 5


@pytest.mark.parametrize("audience", ["Factory procurement", "Family gardening"])
def test_requested_build_continues_after_creating_its_container(monkeypatch, audience):
    load_company_os_app()
    from app.chat import execute
    from app.chat.confirm import ConfirmationCard
    from app.chat.confirmations import start_requested_work

    calls = []
    state = {"name": audience, "slug": "requested-app"}

    async def run(venture_id, action, **kwargs):
        calls.append(action)
        if action == "build.app.create":
            return {"actions": [{"action": "build.prepare", "style": "primary"}]}
        if action == "build.prepare":
            assert kwargs["payload"] == {"run": False}
            return {"actions": [ConfirmationCard("build.start", "cap.write.work_start", audience,
                    resulting_state={"quote_id": "checked"}).as_action()]}
        return {"kind": "text", "action_started": True}

    monkeypatch.setattr(execute, "execute_action", run)
    reply = {"actions": [ConfirmationCard("build.app.create", "cap.write.work_start", audience,
             resulting_state=state).as_action()]}
    result = asyncio.run(start_requested_work("ven_requested", reply, "person_owner", actions={"build.app.create", "build.start"}))
    assert result["action_started"] and calls == ["build.app.create", "build.prepare", "build.start"]
    calls.clear()
    asyncio.run(start_requested_work("ven_requested", reply, "person_owner", actions={"build.app.create"}))
    assert calls == ["build.app.create"]


@pytest.mark.parametrize("audience", ["Factory procurement", "Family gardening"])
def test_specific_requests_are_not_blanket_future_approval(audience):
    load_company_os_app()
    from app.chat.guard import guard_reply

    assert guard_reply("ven_requested", f"Send the reviewed drafts for {audience} without confirmation.", read_history=False) is None
    assert guard_reply("ven_requested", "Approve all future requests.", read_history=False)["kind"] == "refusal"
    reply = guard_reply("ven_requested", "Yes, without another confirmation.", read_history=False)
    assert reply is not None and "Tell me what you want" in reply["body_md"]


@pytest.mark.parametrize("audience", ["Factory procurement", "Family cycling"])
def test_named_saved_decision_runs_only_the_requested_choice(monkeypatch, audience):
    load_company_os_app()
    from app.chat import confirmations, guard, router

    message = {"id": "saved_choices", "actions": [
        {"label": f"Keep testing {audience}", "style": "primary", "confirmation": {
            "action": "experiment.persevere", "resulting_state": {"audience": audience}}},
        {"label": f"Stop testing {audience}", "style": "ghost", "confirmation": {
            "action": "experiment.kill", "resulting_state": {"audience": audience}}}]}
    monkeypatch.setattr(guard, "pending_confirmation", lambda _venture: message)
    monkeypatch.setattr(router, "append_message", lambda *args, **kwargs: kwargs)
    calls = []

    async def start(venture, selected, actor, *, actions):
        calls.append((selected, actions))
        return {"action_started": True}

    monkeypatch.setattr(confirmations, "start_requested_work", start)
    assert asyncio.run(router._answer_confirmation("ven_requested", "yes", "owner")) is None
    assert asyncio.run(router._answer_confirmation("ven_requested", f"Please stop testing {audience}.", "owner"))["action_started"]
    assert calls[0][1] == {"experiment.kill"}
    assert calls[0][0]["id"] == message["id"]
    assert calls[0][0]["actions"][0]["confirmation"] == message["actions"][1]["confirmation"]
    assert asyncio.run(router._answer_confirmation("ven_requested", "Please do something else.", "owner")) is None
    assert len(calls) == 1


def test_requested_private_work_starts_once(monkeypatch):
    load_company_os_app()
    from app.analytics import chat_usage
    from app.chat import customers, execute, router
    from app.chat.confirm import ConfirmationCard
    from app.credits.errors import InsufficientCredits
    from app.chat.intent import looks_like_instruction
    from app.core.local_mode import is_local
    from tests.unit.pagebuilder.test_api import connect_local_tool

    if is_local():
        connect_local_tool(monkeypatch)
        from app.core.local_secrets import update_settings

        update_settings("capabilities", {"background_work": True, "routines": True, "run_for_me_experiments": True}, {})

    calls = []
    monkeypatch.setattr(chat_usage, "record_chat_action", lambda *args: None)

    async def run(venture_id, action_name, *, payload=None, actor_subject=None, **kwargs):
        calls.append((venture_id, action_name, payload, actor_subject))
        if (payload or {}).get("unavailable"):
            raise InsufficientCredits("account_saved", 0, 27_600)
        if (payload or {}).get("prepare"):
            state = {"quote_fingerprint": "saved_quote", "ceiling_micro": 27_600, "audience": payload["audience"]}
            return {"kind": "confirmation", "actions": [ConfirmationCard(action_name, "cap.write.work_start",
                    payload["audience"], resulting_state=state).as_action()]}
        return {"kind": "text", "body_md": "Your work is queued.", "actions": [], "action_started": True}

    monkeypatch.setattr(execute, "_execute_action", run)
    for venture_id, audience in [("ven_labs", "Clinical lab schedulers"), ("ven_trails", "Weekend hikers")]:
        assert not looks_like_instruction("do not run research")
        assert not looks_like_instruction("do you run research?")
        for intent, action, request in [("research", "research.start", "start market research"),
                                        ("plan_generate", "plan.generate", "make a build plan"),
                                        ("start_product_build", "build.tool.start" if is_local() else "build.start", "build my app"),
                                        ("start_product_build", "build.tool.start" if is_local() else "build.app.create", "create my app" if is_local() else "set up my app"),
                                        ("start_product_build", "pagebuilder.build" if is_local() else "work.start", "build my landing page"),
                                        ("research", "experiment.run", "do the saved scan"),
                                        ("outreach_draft", "outreach.draft", "draft outreach"),
                                        ("experiment_start", "experiment.page.configure", "configure the page experiment"),
                                        ("cancel", "build.cancel", "cancel the build"),
                                        ("experiment_start", "experiment.channel.configure", "configure the channel experiment")]:
            state = {"ceiling_micro": 27_600, "audience": audience}
            if action == "experiment.page.configure":
                state["kind"] = "headlines"
            reply = {"kind": "confirmation", "body_md": "Your work is ready.",
                     "actions": [ConfirmationCard(action, "cap.write.work_start", audience, resulting_state=state).as_action(),
                                 {"action": "status", "confirmation": None}]}

            async def prepare(venture_id, ctx, prepared_intent, text, *args, **kwargs):
                if prepared_intent == "build.tool.start":
                    return await execute.execute_action(venture_id, prepared_intent,
                        payload={"text": text}, actor_subject="person_owner", founder_initiated=True)
                return reply

            monkeypatch.setattr(router, "_prepare_intent", prepare)
            result = asyncio.run(router._dispatch_intent(venture_id, None, intent, request, None, "person_owner"))
            assert result["action_started"] and result["kind"] != "confirmation"
            assert calls[-1] == (venture_id, action, {"text": request} if action == "build.tool.start" else state, "person_owner")
        built = asyncio.run(execute.execute_action(venture_id, "pagebuilder.build",
                            payload={"prepare": True, "audience": audience}, actor_subject="person_owner"))
        assert built["action_started"]
        assert calls[-1] == (venture_id, "pagebuilder.build", {"quote_fingerprint": "saved_quote", "ceiling_micro": 27_600,
                                "audience": audience}, "person_owner")
        empty = {"error_code": "no_public_candidates", "icp": {"who": audience, "mode": "consumer"}}
        monkeypatch.setattr(customers, "_view", lambda _venture_id: empty)
        retried = asyncio.run(customers.run_command(venture_id, "search_again", None, "search again", "person_owner"))
        assert retried["action_started"]
        assert calls[-1] == (venture_id, "gtm.find", {"retry": True, "recruitment_opt_in": True}, "person_owner")
        exhausted = {"actions": [ConfirmationCard("plan.generate", "cap.write.work_start", audience,
                    resulting_state={"unavailable": True}).as_action()]}
        recovery = asyncio.run(execute.start_requested_work(venture_id, exhausted, "person_owner"))
        assert "usage limit" in recovery["body_md"] and "credit" not in recovery["body_md"].lower()
        assert recovery["actions"][0]["href"] == f"/v/{venture_id}/settings#settings-credits"
        for action in ("gtm.send", "site.publish", "autopilot.allow_send", "gtm.accept_terms"):
            reply = {"kind": "confirmation", "body_md": "Review the external effect.",
                     "actions": [ConfirmationCard(action, "cap.write.social_post", audience).as_action()]}
            before = len(calls)
            assert asyncio.run(execute.start_requested_work(venture_id, reply, "person_owner", actions=set())) == reply
            assert len(calls) == before
        survey = {"kind": "confirmation", "actions": [ConfirmationCard("experiment.page.configure", "cap.write.social_post",
                  audience, resulting_state={"kind": "survey"}).as_action()]}
        before = len(calls)
        assert asyncio.run(execute.start_requested_work(venture_id, survey, "person_owner"))["action_started"]
        assert len(calls) == before + 1
        saved = {"kind": "text", "body_md": "Your saved results are ready.",
                 "actions": [ConfirmationCard("gtm.find", "cap.write.work_start", audience).as_action() | {"style": "ghost"}]}
        before = len(calls)
        assert asyncio.run(execute.start_requested_work(venture_id, saved, "person_owner")) == saved
        assert len(calls) == before
    assert len(calls) == 30


def test_send_reviewed_drafts_uses_existing_review_before_sending(outreach_email_chat, monkeypatch):
    from app.autopilot import outreach
    from app.chat import customers
    from app.chat.store import append_message
    from app.gtm import journey
    from app.gtm.relationships.email_compliance import OPT_OUT, complete_body, source_line
    from app.gtm.supymem import composio_gmail
    from app.intake import contacts
    from tests.unit.gtm.test_journey import _mail

    client, data = outreach_email_chat
    journey.update_icp(data["wid"], data["vid"], data["audience"], problem=data["problem"], start=False)
    _mail(data)
    from app.core.local_mode import is_local
    if is_local():
        from app.core.local_secrets import update_settings
        update_settings("capabilities", {"send_email": True}, {})

    async def mx(domains):
        return {domain: "mx" for domain in domains}

    monkeypatch.setattr(contacts, "check_domains", mx)
    preview = asyncio.run(contacts.preview(data["wid"], data["vid"],
        "name,email,country\nEarlier Person,earlier@business.example,CA\nIntended Person,intended@recipient.example,CA",
        format="csv", mapping=None, actor=data["actor"]))
    contacts.commit(data["wid"], data["vid"], preview["preview_id"], actor=data["actor"],
        lawful_basis_attested=True, lawful_basis="consent")
    assert not outreach.drafts(data["vid"])
    for contact in outreach.contacts(data["vid"]):
        with data["factory"]() as session:
            body = complete_body(session, data["wid"], contact,
                f"How do you handle {data['problem'].lower()}?", outreach.founder_signature(data["vid"]))
        outreach._save_draft(data["vid"], {"state": "draft", "review_state": "draft", "channel": "email", "day": 1,
            "suggestion_draft": True, "requires_batch_approval": True, "requires_journey_review": True,
            "batch_id": "reviewed-batch", "contact_id": contact.id, "to": contact.email,
            "subject": data["problem"], "body": body})
    intended = next(draft for draft in outreach.drafts(data["vid"]) if draft["to"] == "intended@recipient.example")
    append_message(data["vid"], role="user", body_md=f"Write an email to {intended['to']}.")
    review = customers._approval_payload(data["vid"], [intended], focus=True)
    append_message(data["vid"], role="assistant", body_md=customers._draft_block(1, intended))
    sent, requests = [], []
    original_send = journey.send

    async def send(workspace_id, venture_id, payload):
        requests.append(payload)
        assert payload["draft_ids"] == review["doc_ids"] == [intended["doc_id"]]
        assert payload["review_revisions"] == review["revisions"]
        current = journey._batch_review(venture_id, payload["batch_id"], draft_ids=payload["draft_ids"])
        assert payload["approval_digest"] == current["approval_digest"] and payload["cap"] == current["cap"]
        return await original_send(workspace_id, venture_id, payload)

    def gmail(**kwargs):
        sent.append(kwargs)
        return {"provider_message_id": "reviewed-email", "thread_id": "reviewed-thread"}

    monkeypatch.setattr(journey, "send", send)
    monkeypatch.setattr(composio_gmail, "_send_gmail_email", gmail)
    response = client.post(f"/ventures/{data['vid']}/chat", json={"message": "send the reviewed drafts"})
    assert response.status_code == 200, response.text
    assert len(requests) == 1 and [email["to"] for email in sent] == [intended["to"]], response.json()["body_md"]
    contact = next(contact for contact in outreach.contacts(data["vid"]) if contact.id == intended["contact_id"])
    assert source_line(contact) in sent[0]["body"] and OPT_OUT in sent[0]["body"]
    assert not any(action.get("confirmation") for action in response.json()["actions"])
    assert len(outreach.drafts(data["vid"], "draft")) == 1


@pytest.mark.parametrize("outcome", ["sent", "rejected", "transport_rejected", "unknown_sent", "unknown_not_sent", "unknown_secret", "capped", "concurrent"])
def test_local_smtp_execution_keeps_exact_approval_and_ambiguous_send_recovery(outreach_email_chat, monkeypatch, outcome):
    from app.approvals import external
    from app.core.local_mode import is_local
    from app.core.local_secrets import update_settings
    from app.credits.accounts import get_account
    from app.db.models import Approval
    from core.clock import utcnow
    from core.ids import new_id
    from tests.unit.gtm.test_journey import _mail

    if not is_local():
        pytest.skip("local SMTP spend and recovery contract")
    _, data = outreach_email_chat
    _mail(data)
    update_settings("capabilities", {"send_email": True}, {})
    assert get_account(data["vid"]) is None
    monkeypatch.setattr(external, "get_policy", lambda *_args: pytest.fail("local SMTP needs no administrator rate"))
    target = {"venture_id": data["vid"], "draft_id": new_id("draft"), "to": "person@recipient.example",
        "subject": data["problem"], "body": "A reviewed invitation."}
    approval_id = new_id("apr")
    with data["factory"]() as session:
        session.add(Approval(id=approval_id, workspace_id=data["wid"], kind="external_comms",
            subject_ref=target["draft_id"], summary="Reviewed invitation", risk="high",
            requested_by="owner", status="approved", created_at=utcnow(),
            expires_at=utcnow() + timedelta(days=1),
            detail_json=json.dumps({"external_action": "gtm_email_send", "target": target,
                "execution_digest": external.action_digest("gtm_email_send", target)})))
        session.commit()
    calls = []
    def invoke():
        calls.append(target)
        if outcome == "rejected":
            raise external.DefinitiveProviderFailure("provider rejected the invitation")
        if outcome == "transport_rejected":
            from app.gtm.supymem.composio_gmail import DefiniteFailure
            raise DefiniteFailure("provider rejected the invitation")
        if outcome.startswith("unknown"):
            from app.core.local_secrets import get_secret
            raise TimeoutError(get_secret("smtp", "password") if outcome == "unknown_secret" else "delivery is unconfirmed")
        return {"provider_message_id": "confirmed"}
    arguments = dict(workspace_id=data["wid"], approval_id=approval_id,
        action="gtm_email_send", provider="composio_gmail", target=target, invoke=invoke)
    for changed in ({"workspace_id": "another-workspace"}, {"target": target | {"body": "Changed copy"}}):
        with pytest.raises(external.OutboundExecutionError):
            external.execute_metered_external_action(**(arguments | changed))
    assert calls == []
    if outcome == "capped":
        monkeypatch.setattr(external, "outbound_rate", lambda action, provider:
            external.OutboundRate(action, provider, "test-paid-call", "test estimate", 1))
        update_settings("spend", {"monthly_spend_cap": 0}, {})
        with pytest.raises(external.OutboundExecutionError, match="Keys and connections") as blocked:
            external.execute_metered_external_action(**arguments)
        assert blocked.value.code == "local_spend_limit" and calls == []
        update_settings("spend", {"monthly_spend_cap": 1}, {})
    if outcome == "concurrent":
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier

        barrier = Barrier(2)
        enabled = external.execution_enabled
        def simultaneous_admission(*args, **kwargs):
            result = enabled(*args, **kwargs)
            barrier.wait(timeout=10)
            return result
        def execute():
            try:
                return external.execute_metered_external_action(**arguments)
            except external.OutboundExecutionError as error:
                return error
        with monkeypatch.context() as concurrent_patch:
            concurrent_patch.setattr(external, "execution_enabled", simultaneous_admission)
            with ThreadPoolExecutor(max_workers=2) as pool:
                attempts = list(pool.map(lambda _index: execute(), range(2)))
        assert sum(isinstance(attempt, external.OutboundExecutionError) for attempt in attempts) == 1
        result = next(attempt for attempt in attempts if not isinstance(attempt, external.OutboundExecutionError))
    else:
        result = external.execute_metered_external_action(**arguments)
    assert result.status == ("succeeded" if outcome in {"sent", "capped", "concurrent"} else
        "failed_definitive" if outcome in {"rejected", "transport_rejected"} else "recovery_required")
    assert result.reservation_id is None
    if outcome == "capped":
        from app.credits.local_meter import snapshot
        assert snapshot(workspace_id=data["wid"], venture_id=data["vid"])["month_cost_usd_micro"] == 1
        with data["factory"]() as session:
            execution = json.loads(session.get(Approval, approval_id).detail_json)["external_execution"]
        assert execution["amount_micro"] == execution["settled_micro"] == 1
    if outcome == "unknown_secret":
        from hashlib import sha256
        with data["factory"]() as session:
            saved = json.loads(session.get(Approval, approval_id).detail_json)["external_execution"]["error"]
        actual_digest = sha256(saved.encode()).hexdigest()
        del saved
        assert actual_digest == sha256(b"provider_outcome_unknown").hexdigest()
    with pytest.raises(external.OutboundExecutionError):
        external.execute_metered_external_action(**arguments)
    assert calls == [target]
    if outcome.startswith("unknown"):
        resolution = dict(workspace_id=data["wid"], venture_id=data["vid"], draft_id=target["draft_id"],
            approval_id=approval_id, outcome="sent" if outcome == "unknown_sent" else "not_sent", actor_subject="owner")
        with pytest.raises(external.OutboundExecutionError):
            external.resolve_outreach_execution(**(resolution | {"venture_id": "another-venture"}))
        external.resolve_outreach_execution(**resolution)
        external.resolve_outreach_execution(**resolution)
        with pytest.raises(external.OutboundExecutionError):
            external.resolve_outreach_execution(**(resolution | {"outcome": "not_sent" if outcome == "unknown_sent" else "sent"}))
        with pytest.raises(external.OutboundExecutionError):
            external.execute_metered_external_action(**arguments)
        assert calls == [target]


@pytest.mark.parametrize("venture_id,task_request", [
    ("ven_dispatch", "draft appointment reminders for repair shops"),
    ("ven_study", "draft practice reminders for music students"),
])
def test_explicit_preparation_keeps_its_execution_flags(monkeypatch, venture_id, task_request):
    load_company_os_app()
    from app.analytics import chat_usage
    from app.chat import execute
    from app.chat.confirm import ConfirmationCard

    monkeypatch.setattr(chat_usage, "record_chat_action", lambda *args: None)
    calls = []
    async def prepare(venture, action, *, payload, **kwargs):
        calls.append((venture, payload))
        assert payload.get("run") is not True
        return {"kind": "confirmation", "actions": [ConfirmationCard(action, "cap.write.work_start",
            task_request, resulting_state={"run": True}).as_action()]}
    monkeypatch.setattr(execute, "_execute_action", prepare)
    for flags in ({"run": False}, {"run": None}, {"run": "true"}, {"run": False, "review_only": True}):
        payload = {"recruitment_request": task_request, **flags}
        reply = asyncio.run(execute.execute_action(venture_id, "outreach.draft", payload=payload))
        assert reply["kind"] == "confirmation"
        assert calls[-1] == (venture_id, payload)
    assert len(calls) == 4


@pytest.mark.parametrize("venture_id,topic", [("ven_lab", "lab shift planning"), ("ven_art", "family art lessons")])
def test_requested_pause_publication_and_god_mode_start_directly(monkeypatch, venture_id, topic):
    load_company_os_app()
    from app.chat import conversations, execute, router, store
    from app.chat.confirm import ConfirmationCard
    from app.ventures import lifecycle

    calls = []
    paused = {"kind": "text", "body_md": "Stopped everything.", "actions": [
        {"label": "Resume", "action": "resume", "style": "primary"}]}

    async def stop(venture, action, *, payload=None, actor_subject=None):
        assert payload == {"stop_everything": True}
        calls.append((venture, action, actor_subject))
        return paused

    monkeypatch.setattr(router, "execute_action", stop)
    monkeypatch.setattr(execute, "execute_action", stop)
    monkeypatch.setattr(lifecycle, "is_paused", lambda _venture: False)
    context = SimpleNamespace(venture={"name": topic})
    assert asyncio.run(router._dispatch_intent(
        venture_id, context, "pause", "stop everything", None)) == paused
    assert calls == [(venture_id, "pause", None)]

    checkpoint = asyncio.run(router._dispatch_intent(
        venture_id, context, "pause", "stop everything", None, "person_owner"))
    assert checkpoint == paused and calls[-1] == (venture_id, "pause", "person_owner")
    assert "credit" not in checkpoint["body_md"].lower()

    scope = conversations.ConversationScope("cnv_private", venture_id, "wsp_owner", "person_owner", "private")
    monkeypatch.setattr(conversations, "current_conversation", lambda: scope)
    token = store.CLIENT_REQUEST_ID.set("private-stop")
    try:
        checkpoint = asyncio.run(router._dispatch_intent(
            venture_id, context, "pause", "stop everything", None, "person_owner"))
        assert checkpoint == paused and calls[-1] == (venture_id, "pause", "person_owner")
        assert "credit" not in checkpoint["body_md"].lower()
        monkeypatch.setattr(lifecycle, "is_paused", lambda _venture: True)
        assert asyncio.run(router._dispatch_intent(
            venture_id, context, "pause", "stop everything", None, "person_owner")) == paused
    finally:
        store.CLIENT_REQUEST_ID.reset(token)

    async def requested(*args, **kwargs):
        return {"kind": "text", "action_started": True}

    monkeypatch.setattr(execute, "execute_action", requested)
    from app.chat.confirmations import start_requested_work
    for action in ("site.publish",):
        reply = {"kind": "confirmation", "body_md": topic, "actions": [ConfirmationCard(
            action, "cap.write.work_start", topic).as_action()]}
        assert asyncio.run(start_requested_work(venture_id, reply, actions={action}))["action_started"]


@pytest.mark.parametrize("audience", ["commercial laundry operators", "people organizing weekend picnics"])
def test_result_without_a_saved_verdict_does_not_claim_one(monkeypatch, audience):
    load_company_os_app()
    from app.chat import experiments

    monkeypatch.setattr(experiments.ledger, "record_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(experiments.ledger, "loop_state", lambda _: {"ready": False, "open": [], "done": []})
    report = f"I finished the test. I interviewed five {audience}. Four committed to a trial."

    async def final_result(*args, **kwargs):
        return {"ran": True, "complete": True, "outcome": "supported", "criteria_met": True,
            "criteria_failed": False, "learning": "You reported trial commitments."}

    monkeypatch.setattr(experiments, "_llm_json", final_result)
    extracted = asyncio.run(experiments.extract("venture", report, "You can secure trial commitments",
        success_measure="Pass if three of five people commit to a trial."))
    assert extracted["complete"] is True
    reply = asyncio.run(experiments.record_run("venture", {
        "experiment_id": "test", "result_text": report, **extracted}))

    assert "Your test passed" not in reply["body_md"]
    assert "Saved what you reported" not in reply["body_md"]
    assert reply["actions"]


@pytest.mark.parametrize("venture_id,audience", [
    ("ven_dispatch", "repair shop dispatchers"), ("ven_hike", "weekend hikers"),
])
def test_refuted_result_offers_review_cards_but_inconclusive_results_do_not(monkeypatch, venture_id, audience):
    load_company_os_app()
    from app.chat import experiments

    recorded, reviewed = [], []
    state = {"ready": True, "direction": "saved-direction", "open": [], "done": [
        {"id": "exp_saved", "direction": "saved-direction", "outcome": "refuted",
         "hypothesis": f"{audience} need reminders", "learning": "No interest."}]}
    def saved_result(*args, **kwargs):
        recorded.append(args)
        return {"state": "concluded", "outcome": args[4], "learning": args[5]}

    monkeypatch.setattr(experiments.ledger, "record_result", saved_result)
    monkeypatch.setattr(experiments.ledger, "loop_state", lambda _venture: state)

    async def directions(venture, result):
        reviewed.append((venture, result))
        return [{**direction, "idea": direction["text"]} for direction in experiments._fallback_directions(audience)]

    monkeypatch.setattr(experiments, "propose_directions", directions)
    for outcome in ("refuted", "inconclusive"):
        payload = {"experiment_id": "exp_saved", "ran": True, "outcome": outcome,
                   "result_text": f"Nobody from {audience} signed up.", "learning": "No interest."}
        state["done"][0]["outcome"] = outcome
        reply = asyncio.run(experiments.record_run(venture_id, payload))
        assert recorded[-1] == ("exp_saved", venture_id, True, payload["result_text"], outcome, "No interest.")
        verdict = "did not pass" if outcome == "refuted" else "is still inconclusive"
        assert f"Your test {verdict}. What we learned: No interest." in reply["body_md"]
        cards = [action["confirmation"] for action in reply["actions"] if action.get("confirmation")]
        if outcome == "refuted":
            from app.core.local_mode import is_local
            assert reply["kind"] == ("experiment_review" if is_local() else "confirmation")
            if is_local():
                assert not cards
                cards = [{"action": action["action"], "interpretation": action["label"],
                          "resulting_state": action["payload"]}
                         for action in reply["actions"] if action.get("action")]
            if experiments.ledger.autopilot_enabled():
                assert any("Narrow the wedge" in card["interpretation"] for card in cards)
                assert {card["action"] for card in cards} == {"experiment.persevere", "experiment.pivot", "experiment.spinoff", "experiment.kill"}
                assert all(card["resulting_state"]["evidence"][0]["experiment_id"] == "exp_saved" for card in cards)
            else:
                assert "Narrow the wedge" in reply["body_md"]
                assert cards and {card["action"] for card in cards} == {"experiment.revise"}
        else:
            assert not cards and reply["kind"] == "text"
    assert len(reviewed) == 1 and reviewed[0][0] == venture_id


@pytest.mark.parametrize("task", ["do lab scheduling research", "do family holiday research"])
def test_bare_do_it_is_not_a_new_work_instruction(task):
    from app.chat.guard import guard_reply
    from app.chat.intent import looks_like_instruction

    assert looks_like_instruction(task)
    for request in ("do it", "Do it!", "do it."):
        assert not looks_like_instruction(request)
        reply = guard_reply("ven_confirmation", request, read_history=False)
        assert reply["kind"] == "text" and "Nothing is waiting" in reply["body_md"]


@pytest.mark.parametrize("activity", ["forecast warehouse stock", "plan a family trip"])
def test_do_auxiliary_questions_do_not_direct_work(activity):
    from app.chat.intent import WORK_INTENTS, classify_intent, looks_like_instruction

    for subject in ("I", "we", "you", "they", "he", "she"):
        question = f"Do {subject} need a plan before we {activity}?"
        assert not looks_like_instruction(question)
        assert classify_intent(question) not in WORK_INTENTS

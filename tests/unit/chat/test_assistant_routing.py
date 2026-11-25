"""Intent routing: questions → assistant; explicit research starts the requested work."""

from __future__ import annotations

import pytest
from tests.company_os_harness import company_os_test_client as TestClient

from tests.company_os_harness import load_company_os_app
from tests.company_os_harness import configure_local_test_model

DIRECT_MAC_REQUEST = (
    "Use my Mac to open https://example.com in your browser and report its page title. "
    "This is read-only: do not edit, submit forms, or send messages."
)
DIRECT_NATIVE_REQUEST = (
    "Use my Mac to open TextEdit and read the visible project notes. "
    "Read-only: do not change or save anything."
)


@pytest.mark.parametrize("prompt", [
    DIRECT_MAC_REQUEST,
    "Can you use my Mac to open Chrome?",
    "Can you please use my Mac to open Chrome?",
    "Could you please use my Mac to open Chrome?",
    "Can you please use my Mac to open https://example.com and report its title? Do not edit, submit forms, or send messages.",
])
def test_direct_mac_goal_and_all_constraints_are_one_review_intent(prompt):
    load_company_os_app()
    from app.chat.intent import classify_intent, split_speech_acts
    from app.build.device_orders import classify_device_chat_intent

    assert classify_intent(prompt) == "mac_prepare"
    assert split_speech_acts(prompt) == [("mac_prepare", prompt)]
    assert classify_device_chat_intent(prompt) == ("general_browser" if "https://" in prompt else "desktop")


@pytest.mark.parametrize("text", [
    "Can Supymem use my Mac to open a browser automatically?",
    "Can you use my Mac?",
    "Are you able to use my Mac?",
    "How do you use my Mac to open a browser?",
    "Should I use my Mac to open a browser?",
    "Do not use my Mac to open https://example.com in your browser.",
    "Please don't use my Mac to open https://example.com.",
    'The note says "Use my Mac to open https://example.com in your browser".',
    '"Use my Mac to open https://example.com in your browser"',
    "Do not use my Mac to open https://example.com in my browser.",
    'The note says "Use my Mac to open https://example.com in my browser".',
])
def test_incidental_negative_and_capability_mac_mentions_do_not_prepare(text):
    load_company_os_app()
    from app.chat.intent import classify_intent, split_speech_acts
    from app.build.device_orders import classify_device_chat_intent
    from app.chat.mac_offers import explicit_mac_control_request

    assert not explicit_mac_control_request(text)
    assert classify_intent(text) != "mac_prepare"
    assert all(intent != "mac_prepare" for intent, _ in split_speech_acts(text))
    assert classify_device_chat_intent(text) is None


@pytest.mark.parametrize("prompt", [
    DIRECT_NATIVE_REQUEST,
    "Can you use my Mac to open Chrome?",
    "Could you please use my Mac to inspect Photos and describe the holiday album? Do not edit or share anything.",
])
def test_direct_mac_api_offer_keeps_goal_has_no_orders_and_does_not_reuse_history(env, monkeypatch, prompt):
    from app.core.local_mode import is_local

    if is_local():
        from app.core.local_secrets import update_settings

        monkeypatch.delenv("DEVICE_DESKTOP_EXECUTION_ENABLED", raising=False)
        update_settings("capabilities", {"background_work": True}, {})
    else:
        monkeypatch.setenv("DEVICE_DESKTOP_EXECUTION_ENABLED", "1")
    from app.chat import llm_router
    from app.chat.store import append_message
    from app.build import device_orders
    from app.db.models import PlannedTask, VentureDocument
    from sqlalchemy import select

    client, vid = env
    historical_path = f"/v/{vid}/device-work-orders/dwo_previous"
    append_message(vid, role="assistant", kind="confirmation", body_md="An older Mac review.",
                   actions=[{"label": "Review Mac task", "href": historical_path}])
    def snapshot():
        with device_orders.db()() as session:
            return (list(session.scalars(select(PlannedTask.id))),
                    list(session.scalars(select(VentureDocument.id).where(VentureDocument.kind == device_orders.KIND))))
    before = snapshot()
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("direct Mac preparation must not depend on a model")
    monkeypatch.setattr(llm_router, "route", forbidden)
    monkeypatch.setattr(device_orders, "prepare_from_chat", lambda *_args, **_kwargs: pytest.fail("chat cannot prepare a device order"))
    response = client.post(f"/ventures/{vid}/chat", json={"message": prompt})
    assert response.status_code == 200, response.text
    reply = response.json()
    if is_local():
        from app.core.local_capabilities import blocked_message

        # 77be6a460 excludes computer control independently of background work.
        assert reply["kind"] == "text"
        assert blocked_message("computer_use") in reply["body_md"]
        assert not reply["actions"]
    else:
        assert reply["kind"] == "navigation"
        assert "No Mac task has been prepared or run yet" in reply["body_md"]
        assert reply["actions"] == [{"label": "Prepare Mac task for review", "action": "mac.task.prepare",
                                     "style": "primary", "text": prompt,
                                     "payload": {"text": prompt}}]
    assert historical_path not in str(reply)
    assert snapshot() == before


@pytest.mark.parametrize("screen,idea", [
    pytest.param("Builds", "Inventory alerts for warehouse teams", id="Builds"),
    pytest.param("Analytics", "Accessible trail guides for families", id="Analytics"),
    pytest.param("Settings", "Kitchen equipment scheduling for caterers", id="Settings"),
])
def test_use_my_mac_saved_supymem_screen_keeps_first_party_navigation(env, screen, idea):
    from app.chat.intent import classify_intent, split_speech_acts
    from app.build.device_orders import classify_device_chat_intent
    from app.core.local_mode import is_local

    client, _ = env
    created = client.post("/ventures", json={"raw_idea": idea, "start": False})
    assert created.status_code == 201, created.text
    vid = created.json()["id"]
    request = f"Use my Mac to open {screen} in Supymem. Do not modify data."
    assert classify_intent(request) == "open_screen"
    assert split_speech_acts(request) == [("open_screen", request)]
    assert classify_device_chat_intent(request) is None
    response = client.post(f"/ventures/{vid}/chat", json={"message": request})
    assert response.status_code == 200, response.text
    reply = response.json()
    assert reply["kind"] == "navigation"
    if screen == "Settings":
        assert reply["actions"][0]["kind"] == "recovery_link"
        assert reply["actions"][0]["href"] == f"/v/{vid}/settings"
    elif is_local() and screen == "Builds":
        assert reply["actions"][0]["href"] == f"/v/{vid}/builds"
    else:
        assert reply["actions"][0]["action"] == "status" and "href" not in reply["actions"][0]
    assert all(action.get("action") != "mac.task.prepare" for action in reply["actions"])


@pytest.mark.parametrize("prompt", [
    "Do not use my Mac to open https://example.com in my browser.",
    'The note says "Use my Mac to open https://example.com in my browser".',
])
def test_non_authorizing_mac_api_has_no_review_offer_or_queued_work(env, monkeypatch, prompt):
    from app.chat import assistant, llm_router
    from app.build import device_orders
    from app.db.models import PlannedTask, VentureDocument
    from sqlalchemy import select

    client, vid = env
    async def no_model(*_args, **_kwargs):
        return None
    async def answer(*_args, **_kwargs):
        return {"kind": "text", "body_md": "No Mac request has run.", "actions": [], "citations": []}
    monkeypatch.setattr(llm_router, "route", no_model)
    monkeypatch.setattr(assistant, "answer_question", answer)
    with device_orders.db()() as session:
        before = list(session.scalars(select(PlannedTask.id)))
    response = client.post(f"/ventures/{vid}/chat", json={"message": prompt})
    assert response.status_code == 200, response.text
    assert not response.json()["actions"]
    with device_orders.db()() as session:
        assert list(session.scalars(select(PlannedTask.id))) == before
        assert not session.scalar(select(VentureDocument.id).where(VentureDocument.kind == device_orders.KIND))


def test_assistant_intent_for_steering_questions():
    load_company_os_app()
    from app.chat.intent import classify_intent

    assert classify_intent("Hey, what's going on with LabHire right now? What do you need from me?") == "assistant"
    assert classify_intent("What did we decide about who LabHire is for?") == "assistant"
    assert classify_intent("Is my Gmail connected? What can you do with it?") == "assistant"
    assert classify_intent("Why did the last build fail and what should we do about it?") == "assistant"
    assert classify_intent("Show me how the waitlist is doing.") == "assistant"
    assert classify_intent("How many users do we have?") == "assistant"
    assert classify_intent("What should I do next, in order?") == "assistant"
    assert classify_intent(
        "What exactly blocks LabHire from having a working authenticated product at a public URL right now? "
        "Identify the saved source, QA and hosting state. Do not start a task or claim users."
    ) == "assistant"
    assert classify_intent("Research three competitors of LabHire.") == "research"
    assert classify_intent("Draft a short outreach email to a first prospect for LabHire. Do not send it.") == "outreach_draft"
    assert classify_intent("Update the plan: our first milestone is 10 waitlist signups this week.") == "plan_revision"
    assert classify_intent("Run independent QA on the exact stored coding source. Do not deploy.") == "source_qa"
    assert classify_intent("What happened in the latest Content agent cycle? Do not start another.") == "assistant"


@pytest.mark.parametrize("question, expected", [
    ("Recap", True),
    ("Please recap our progress.", True),
    ("Recap what we have done so far.", True),
    ("Catch me up.", True),
    ("Catch me up on our progress.", True),
    ("What did we do so far?", True),
    ("For this existing venture, recap the pilot’s target customer and two synthetic test scenarios. Do not start tasks or change anything.", False),
    ("Recap the target customer and pricing assumptions.", False),
    ("Please recap the two synthetic test scenarios.", False),
    ("Recap this chat's decisions and unanswered questions.", False),
    ("Catch me up on the pilot's customer and test scenarios.", False),
    ("Do not recap our progress; explain the customer instead.", False),
])
def test_progress_recap_shortcut_does_not_replace_specific_questions(question, expected):
    load_company_os_app()
    from app.chat.journey import asks_for_guidance

    assert asks_for_guidance(question) is expected


@pytest.mark.parametrize("question, expected", [
    ("Recap the target customer and pricing assumptions.", True),
    ("Please recap the two synthetic test scenarios.", True),
    ("For this existing venture, recap the pilot’s target customer and two synthetic test scenarios. Do not start tasks or change anything.", True),
    ("Catch me up on the pilot's customer and test scenarios.", True),
    ("Do not recap our progress; explain the customer instead.", False),
    ('The note says "recap the target customer".', False),
    ("Write a recap of the pilot.", False),
])
def test_specific_recap_is_a_read_request_not_an_incidental_mention(question, expected):
    load_company_os_app()
    from app.chat.intent import looks_like_question, classify_intent

    assert looks_like_question(question) is expected
    if expected:
        assert classify_intent(question) == "assistant"


def test_specific_pilot_recap_reaches_grounded_assistant_without_work(env, monkeypatch):
    from sqlalchemy import select
    from app.chat import assistant
    from app.db.models import Job, PlannedTask
    from app.state import db
    from app.ventures import run_status

    client, vid = env
    question = ("For this existing venture, recap the pilot’s target customer and two synthetic test scenarios. "
                "Do not start tasks or change anything.")
    saved = "Target customer: independent bakery owners. Synthetic scenarios: flour shortage and duplicate reorder."
    expected = "The pilot targets independent bakery owners; the synthetic scenarios cover a flour shortage and a duplicate reorder."
    monkeypatch.setattr(assistant, "get_venture_state_brief", lambda *_args, **_kwargs: {
        "venture_id": vid, "idea": {"name": "Trail Notebook"}, "_rendered": saved,
    })
    snapshots = []
    monkeypatch.setattr(run_status, "snapshot", lambda *_args, **_kwargs: snapshots.append(True) or {
        "state": "idle", "action": None,
    })
    calls = []
    async def complete(messages, **_kwargs):
        content = messages[1]["content"]
        assert question in content and saved in content
        calls.append(content)
        return expected
    monkeypatch.setattr(assistant, "_complete", complete)
    def work():
        with db()() as session:
            return (list(session.scalars(select(Job.id).where(Job.venture_id == vid))),
                    list(session.scalars(select(PlannedTask.id).where(PlannedTask.venture_id == vid))))
    before = work()
    reply = client.post(f"/ventures/{vid}/chat", json={"message": question})
    assert reply.status_code == 200, reply.text
    assert expected in reply.json()["body_md"]
    assert reply.json()["actions"] == []
    assert len(calls) == 1 and not snapshots
    assert work() == before


@pytest.mark.parametrize("audience", [
    "warehouse procurement teams",
    "families planning camping trips",
])
@pytest.mark.asyncio
async def test_recap_with_document_continuation_writes_requested_checklist(env, monkeypatch, audience):
    from app.chat import assistant
    from app.chat.store import append_message

    _, venture_id = env
    document = f"Interview document for {audience}: ask about your last difficult purchase."
    append_message(venture_id, role="user", body_md=f"Write an interview document for {audience}.")
    append_message(venture_id, role="assistant", body_md=document)
    question = ("What have you done so far? Continue the interview document by adding a three-item "
                "checklist for running the first interview. Do not contact anybody.")
    expected = (f"Your saved work: {document}\n\n"
                "1. Review your interview questions.\n2. Ask permission to take notes.\n"
                "3. Record answers without identifying details.\n\nYour next step: review your checklist.")
    calls = []

    async def complete(messages, **_kwargs):
        content = messages[1]["content"]
        assert question in content and document in content
        calls.append(content)
        return expected

    monkeypatch.setattr(assistant, "_complete", complete)
    reply = await assistant.answer_question(venture_id, question)
    assert len(calls) == 1
    assert document in reply["body_md"]
    for item in expected.splitlines()[2:5]:
        assert item in reply["body_md"]
    assert reply["actions"] == []
    assert "confirmation" not in reply


def test_single_outreach_email_keeps_request_and_no_send_constraint_on_its_review_card(env):
    from app.chat.intent import classify_intent, split_speech_acts
    client, vid = env
    request = "Draft a short outreach email to a first prospect for LabHire. Do not send it."
    assert classify_intent(request) == "outreach_draft"
    assert split_speech_acts(request) == [("outreach_draft", request)]
    reply = client.post(f"/ventures/{vid}/chat/confirm", json={"action": "outreach.draft",
        "payload": {"run": False, "recruitment_request": request}}).json()
    assert reply["kind"] == "confirmation" and "drafts" in reply["body_md"].lower()
    card = reply["actions"][0]["confirmation"]
    assert card["action"] == "outreach.draft"
    assert "one outreach email" in card["interpretation"]
    assert card["resulting_state"] == {"run": True, "recruitment_request": request}
    assert any("Nothing is sent" in text for text in card["consequences"])


@pytest.mark.parametrize("prompt", [
    "Draft a short outreach email. Then send it.",
    "Draft one outreach email and publish it.",
    "Draft one outreach email, then search for people.",
    "Don't draft an outreach email.",
])
def test_one_email_classifier_does_not_drop_extra_effects_or_a_prohibition(prompt):
    load_company_os_app()
    from app.chat.intent import single_outreach_email_request
    assert not single_outreach_email_request(prompt)


@pytest.mark.parametrize("audience,subject", [
    ("Freight dispatchers", "Route planning trial"), ("Garden hobbyists", "Seed planning trial"),
])
@pytest.mark.parametrize("draft_state", ["draft", "manual"])
def test_review_saved_outreach_drafts_button_is_grounded_and_does_not_send(env, audience, subject, draft_state):
    from app.autopilot import outreach
    from app.chat.intent import classify_intent
    from app.db.models import Venture
    from app.state import db
    client, vid = env
    with db()() as session:
        session.get(Venture, vid).raw_idea = subject + " for " + audience
        session.commit()
    outreach._save_draft(vid, {"to": "owner@example.com", "subject": subject, "body": "Try our pilot?", "state": draft_state})
    assert classify_intent("show my outreach drafts") == "outreach.drafts"
    reply = client.post(f"/ventures/{vid}/chat", json={"message": "show my outreach drafts"}).json()
    assert reply["kind"] == "text" and subject in reply["body_md"] and "Try our pilot?" in reply["body_md"]
    assert not any(action.get("action") in {"gtm.send", "outreach.send"} for action in reply["actions"])
    assert outreach.drafts(vid)[0]["state"] == draft_state


@pytest.mark.parametrize("audience,subject", [
    ("Regional procurement teams", "Supplier review notebook"),
    ("Families planning weekends", "Local activity planner"),
])
def test_saved_manual_email_lookup_keeps_pending_permission(env, audience, subject):
    import time

    from app.autopilot import outreach
    from app.chat import customers
    from app.db.models import Venture
    from app.state import MEM, db

    client, venture_id = env
    with db()() as session:
        session.get(Venture, venture_id).raw_idea = subject + " for " + audience
        session.commit()
    outreach._save_draft(venture_id, {"to": "recipient@example.com", "subject": subject,
        "body": "Would you share your experience?", "state": "manual"})
    key = customers._eligibility_key(venture_id)
    pending = {"at": time.time(), "contact_id": "pending-contact", "country": "CA"}
    MEM[key] = pending.copy()
    response = client.post(f"/ventures/{venture_id}/chat", json={"message": "List my drafts."})
    assert response.status_code == 200, response.text
    reply = response.json()
    assert subject in reply["body_md"] and "Would you share your experience?" in reply["body_md"]
    assert MEM[key] == pending
    assert not any(action.get("action") in {"gtm.send", "outreach.send"} for action in reply["actions"])
    assert outreach.drafts(venture_id)[0]["state"] == "manual"


@pytest.mark.parametrize("idea", ["Inventory alerts for warehouse teams", "Walking clubs for families"])
def test_discovery_progress_report_reaches_grounded_advice_without_search_or_status_shortcut(env, monkeypatch, idea):
    from app.chat import assistant
    from app.chat.intent import classify_intent, split_speech_acts
    client, vid = env
    question = (f"Discovery update for {idea}, founder-reported evidence: I followed your manual Google-to-Gmail guide. "
                "I verified https://example.com/ directly and sent one interview request from Gmail. "
                "The business lists a team, so solo ICP fit is unknown. Zero replies, interviews, signups or users. "
                "No private customer chats or screenshots. Keep this as outreach evidence, not demand validation. "
                "What exact next action should I take?")
    assert classify_intent(question) == "assistant"
    assert split_speech_acts(question) == [("assistant", question)]
    captured = []
    searches = []
    async def complete(messages, **_kwargs):
        captured.append(messages)
        return "Find one additional solo professional and verify their own published business contact before drafting an interview request."
    async def forbidden(*_args, **_kwargs):
        searches.append(True)
        raise AssertionError("a progress report does not authorize a web search")
    async def read_links(text, **kwargs):
        assert text == question
        return "Untrusted source: https://example.com/\nThe reported business lists a team."
    monkeypatch.setattr(assistant, "_complete", complete)
    monkeypatch.setattr("app.research.explore.explore_web", forbidden)
    monkeypatch.setattr("app.chat.link_context.linked_context_async", read_links)
    from app.chat import link_context

    monkeypatch.setattr(link_context.socket, "getaddrinfo", lambda *_args, **_kwargs: [
        (link_context.socket.AF_INET, link_context.socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
    monkeypatch.setattr(link_context, "_fetch", lambda url, timeout: "Public source context for " + url)
    reply = client.post(f"/ventures/{vid}/chat", json={"message": question})
    assert reply.status_code == 200, reply.text
    assert searches == []
    assert len(captured) == 1, reply.json()
    prompt = str(captured[0])
    assert question in prompt
    assert "founder-reported evidence" in prompt and "not independent verification" in prompt
    assert "one additional solo professional" in reply.json()["body_md"]
    assert reply.json()["actions"] == []


@pytest.mark.parametrize("question", [
    "Discovery update: I sent one email. Search the web for three more trainers.",
    "Discovery update: I sent one email. Search the web for three more trainers. What should I do next?",
    "Outreach results: One reply. Start getting users.",
    "Outreach results: One reply. Start getting users. What is next?",
    "Interview update: One reply. Draft the interview request. What should I do next?",
    "Discovery update: Search the web for three more trainers. What should I do next?",
    "Outreach update: One reply;send the interview request. What should I do next?",
])
def test_progress_prefix_does_not_swallow_explicit_new_search_or_execution(question):
    load_company_os_app()
    from app.chat.intent import discovery_progress_question
    assert not discovery_progress_question(question)


def test_reported_search_results_and_research_status_stay_progress_context():
    load_company_os_app()
    from app.chat.intent import discovery_progress_question
    assert discovery_progress_question("Discovery update: Search results were unrelated. Research is still pending. What should I do next?")
    assert discovery_progress_question("Outreach update: I sent one email. Do not send more. What should I do next?")


@pytest.mark.parametrize("audience", ["Invoice reconciliation for finance teams", "Indoor gardening for apartment renters"])
def test_local_copy_content_status_recovers_your_saved_venture_workspace(env, monkeypatch, audience):
    from app.chat import assistant
    from app.content.engine import record_event
    from app.db.models import Venture
    from app.state import db

    _, venture_id = env
    with db()() as session:
        venture = session.get(Venture, venture_id)
        venture.raw_idea = audience
        workspace_id = venture.workspace_id
        session.commit()
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    record_event(workspace_id=workspace_id, kind="agent.keepalive_ok", payload={})
    reply = assistant._content_activity_answer(venture_id, {"raw_idea": audience})
    assert "healthy" in reply["body_md"]
    assert "no connected workspace" not in reply["body_md"]
    assert "Connections" in reply["body_md"] and reply["actions"]
    missing = assistant._content_activity_answer("missing-venture", {"raw_idea": audience})
    assert "open your venture home" in missing["body_md"].lower()
    assert missing["actions"][0]["href"] == "/v/missing-venture"


@pytest.mark.parametrize("audience", ["Invoice reconciliation for finance teams", "Indoor gardening for apartment renters"])
@pytest.mark.parametrize("kind", ["agent.blocked", "agent.reconnect_needed", "agent.keepalive_ok", "agent.cycle_completed", "agent.unknown"])
def test_local_copy_content_events_use_plain_progress_and_connection_recovery(env, monkeypatch, audience, kind):
    from app.chat import assistant
    from app.content.engine import record_event
    from app.core import local_capabilities
    from app.db.models import Venture
    from app.state import db

    _, venture_id = env
    with db()() as session:
        venture = session.get(Venture, venture_id)
        venture.raw_idea = audience
        workspace_id = venture.workspace_id
        session.commit()
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    next_step = "Open Settings > Keys and connections and save your Composio API key."
    monkeypatch.setattr(local_capabilities, "enabled", lambda name: False)
    monkeypatch.setattr(local_capabilities, "blocked_message", lambda name: next_step)
    record_event(workspace_id=workspace_id, kind=kind,
                 payload={"reason": "no_healthy_channels", "channels": ["linkedin"], "error": "do-not-expose"})
    reply = assistant._content_activity_answer(venture_id, {"workspace_id": workspace_id, "raw_idea": audience})
    assert "agent." not in reply["body_md"] and "do-not-expose" not in str(reply)
    assert "Connections" in reply["body_md"] and reply["actions"]
    assert "proof" in reply["body_md"]
    if kind in {"agent.blocked", "agent.reconnect_needed"}:
        assert next_step in reply["body_md"]
        assert "LinkedIn" in reply["body_md"]
    else:
        assert "Reconnect" not in reply["body_md"]


@pytest.mark.parametrize("audience", ["Invoice reconciliation for finance teams", "Indoor gardening for apartment renters"])
def test_chat_reads_saved_content_agent_blocker_without_inventing_a_publish(env, monkeypatch, audience):
    from app.chat import assistant
    from app.chat.intent import split_speech_acts
    from app.content.engine import record_event
    from app.state import db
    from app.db.models import Venture

    client, vid = env
    with db()() as session:
        venture = session.get(Venture, vid)
        venture.raw_idea = audience
        workspace_id = venture.workspace_id
        session.commit()
    record_event(workspace_id=workspace_id, kind="agent.reconnect_needed",
                 payload={"channels": ["linkedin"], "secret": "must-not-appear"})
    record_event(workspace_id=workspace_id, kind="agent.blocked",
                 payload={"reason": "no_healthy_channels", "error": "must-not-appear"})
    question = ("What happened in your latest Content agent cycle and why did it stop? "
                "Use saved activity only. Do not start another cycle or contact anyone.")
    assert split_speech_acts(question) == [("assistant", question)]

    async def no_model(*_args, **_kwargs):
        raise AssertionError("Durable Content activity should not depend on model recall")

    monkeypatch.setattr(assistant, "_complete", no_model)
    reply = client.post(f"/ventures/{vid}/chat", json={"message": question})
    assert reply.status_code == 200, reply.text
    body = reply.json()["body_md"].lower()
    assert "no connected social channel is healthy" in body
    assert "reconnect linkedin" in body
    assert "across your ventures" in body
    assert "must-not-appear" not in body
    assert reply.json()["actions"] == [{"label": "Open Connections", "href": f"/connections?venture={vid}", "kind": "recovery_link"}]


def test_waitlist_metrics_question_does_not_target_latest_mac_task(env, monkeypatch):
    from app.chat import assistant
    from app.chat.intent import classify_intent, split_speech_acts

    client, vid = env
    question = (
        "For LabHire, how many verified waitlist signups and active product users do we have? "
        "Did the latest synthetic submission create a real lead? Do not start a task."
    )
    assert classify_intent(question) == "assistant"
    assert split_speech_acts(question) == [("assistant", question)]
    assert not assistant._asks_about_latest_mac_task(question)
    seen = []

    def brief(_vid, **kwargs):
        seen.append(kwargs)
        return {"idea": {"name": "LabHire"}, "website": {
            "waitlist_count": 2, "waitlist_submission_verified": True,
        }, "device_work": {"recent": [{
            "title": "Unrelated Safari Mac task", "status": "stopped",
            "review_href": f"/v/{vid}/device-work-orders/dwo_safari",
        }]}}

    monkeypatch.setattr(assistant, "get_venture_state_brief", brief)

    async def no_model(*_args, **_kwargs):
        raise AssertionError("Saved venture metrics should answer without model or task recall")

    monkeypatch.setattr(assistant, "_complete", no_model)
    reply = client.post(f"/ventures/{vid}/chat", json={"message": question})
    assert reply.status_code == 200, reply.text
    body = reply.json()["body_md"].lower()
    assert "2 consented early-access requests" in body
    assert "cannot verify a count of active product users" in body
    assert "did not create a real lead" in body
    assert "safari" not in body and "dwo_safari" not in body
    assert reply.json().get("actions") == []
    assert seen == [{"force": True, "mark_visit": True}]


def test_mixed_gtm_diagnosis_and_product_users_answers_both(env, monkeypatch):
    from app.chat import assistant
    from app.chat.intent import classify_intent, split_speech_acts

    client, vid = env
    question = (
        "What does the saved GTM diagnosis say is the biggest evidence gap, and has it acquired "
        "any verified active product users? Answer only from saved records. "
        "Do not start work or send outreach."
    )
    assert classify_intent(question) == "assistant"
    assert split_speech_acts(question) == [("assistant", question)]
    seen = []

    def brief(_vid, **kwargs):
        seen.append(kwargs)
        return {"gtm": {"present": True, "bottleneck": "The target cohort is unvalidated.",
                        "evidence_gaps": ["No customer interviews are recorded."]},
                "website": {"waitlist_count": 3}}

    monkeypatch.setattr(assistant, "get_venture_state_brief", brief)

    async def no_model(*_args, **_kwargs):
        raise AssertionError("Mixed saved facts must not depend on model recall")

    monkeypatch.setattr(assistant, "_complete", no_model)
    reply = client.post(f"/ventures/{vid}/chat", json={"message": question})
    assert reply.status_code == 200, reply.text
    body = reply.json()["body_md"].lower()
    assert "target cohort is unvalidated" in body
    assert "no customer interviews are recorded" in body
    assert "cannot verify a count of active product users" in body
    assert "3 consented" not in body  # The founder did not ask for the waitlist.
    assert reply.json()["actions"] == []
    assert seen == [{"force": True, "mark_visit": True}]

    alternate = client.post(f"/ventures/{vid}/chat", json={"message":
        "What is our bottleneck, and how many active product users do we have?"})
    assert alternate.status_code == 200, alternate.text
    assert "target cohort is unvalidated" in alternate.json()["body_md"].lower()
    assert "cannot verify a count of active product users" in alternate.json()["body_md"].lower()


def test_channel_readiness_and_outreach_answer_uses_scoped_saved_status(env, monkeypatch):
    from app.chat import assistant
    from app.chat.intent import classify_intent, split_speech_acts
    from app.connections import hub
    from app.gtm import journey

    client, vid = env
    question = (
        "Which GTM publication channels are connected and ready, "
        "and is outreach or active-user acquisition verified?"
    )
    assert classify_intent(question) == "assistant"
    assert split_speech_acts(question) == [("assistant", question)]
    monkeypatch.setattr(assistant, "get_venture_state_brief",
                        lambda _vid, **_kwargs: {
                            "workspace_id": "wsp_selected",
                            "website": {"live": True, "url": "https://example.test",
                                        "waitlist_count": 3},
                            "device_work": {"latest_coding_source": {
                                "coding_provider": "cursor", "status": "stopped",
                                "source_state": "stored",
                                "review_href": f"/v/{vid}/device-work-orders/dwo_old",
                            }},
                        })
    seen = []

    def status(workspace_id):
        seen.append(workspace_id)
        return {"connections": {
            "composio": {"ready": True},
            "gmail": {"connected": True, "ready": True, "account_email": "private@example.test"},
            "linkedin": {"connected": True, "ready": False, "handle": "private"},
            "reddit": {"connected": False, "ready": False},
            "x": {"connected": False, "ready": False},
            "discord": {"connected": False, "ready": False},
        }}

    monkeypatch.setattr(hub, "hub_status", status)
    monkeypatch.setattr(journey, "status", lambda *_args, **_kwargs: {
        "results": {"sent": 0, "replied": 0, "interested": 0, "negative": 0, "replies": []},
        "auto_search": {"contacted": 0},
    })

    async def no_model(*_args, **_kwargs):
        raise AssertionError("Saved channel and outreach status must not use model recall")

    monkeypatch.setattr(assistant, "_complete", no_model)
    reply = client.post(f"/ventures/{vid}/chat", json={"message": question})
    assert reply.status_code == 200, reply.text
    body = reply.json()["body_md"].lower()
    assert seen == ["wsp_selected"]
    assert "connected but not ready for publishing: linkedin" in body
    assert "no connected account is reported for reddit, x, discord" in body
    assert "linkedin" in body and "reddit" in body and "discord" in body
    assert "gmail is connected as a mailbox" in body
    assert "you have sent 0 outreach emails" in body
    assert "cannot verify acquired active product users" in body
    assert "private@example.test" not in body and "dwo_old" not in body
    assert "landing page is live" not in body and "source transfer" not in body
    from app.core.local_mode import is_local

    actions = [{"label": "Open leads and replies", "action": "gtm.status", "style": "primary"}]
    if is_local():
        # 2d2d6dabd keeps local Connections links navigable.
        actions.insert(0, {"label": "Open Connections", "href": f"/connections?venture={vid}", "style": "primary"})
    assert reply.json()["actions"] == actions


@pytest.mark.parametrize("idea", [
    "Inventory alerts for independent hardware retailers",
    "Trail planning for weekend hikers",
])
@pytest.mark.parametrize("scenario", ["sent", "empty", "active_users", "read", "unavailable", "missing_counts"])
def test_outreach_progress_uses_saved_scoped_records_and_preserves_proof_limits(env, monkeypatch, idea, scenario):
    import asyncio
    from copy import deepcopy

    from app.chat import assistant, customers
    from app.gtm import journey

    client, vid = env
    wid = "wsp_outreach_records"
    monkeypatch.setattr(assistant, "get_venture_state_brief", lambda *_args, **_kwargs: {
        "workspace_id": wid, "idea": {"raw_idea": idea},
    })
    saved = {
        "icp": {"who": idea},
        "results": {"sent": 3, "replied": 1, "interested": 1, "negative": 0, "replies": [
            {"contact_id": "responded", "from": "team@retailer.example", "body": "Please share availability for a conversation.",
             "classification": "human_positive", "received_at": "2026-11-09T12:00:00Z"},
            {"contact_id": "automated", "from": "robot@retailer.example", "body": "AUTOMATED_CONTENT_MUST_NOT_APPEAR",
             "classification": "automated", "received_at": "2026-11-09T13:00:00Z"},
        ]},
        "auto_search": {"contacted": 2, "enabled": True, "target": 50},
        "followup_days": [2, 4, 7],
        "prospects": [{"id": "pending", "email": "hello@outdoor.example"},
                      {"id": "responded", "email": None}, {"id": "suppressed", "email": None}],
        "drafts": [
            {"contact_id": "pending", "day": 1, "state": "sent", "sent_at": "2026-11-08T15:30:00+05:30", "body": "DRAFT_CONTENT_MUST_NOT_APPEAR"},
            {"contact_id": "pending", "day": 3, "state": "sent", "sent_at": "2026-11-10T10:00:00Z"},
            {"contact_id": "responded", "day": 1, "state": "sent", "sent_at": "2026-11-01T10:00:00Z"},
            {"contact_id": "suppressed", "day": 1, "state": "sent", "sent_at": "2026-11-01T10:00:00Z"},
        ],
    }
    if scenario == "empty":
        saved.update(results={"sent": 0, "replied": 0, "interested": 0, "negative": 0, "replies": []},
                     drafts=[{"day": 1, "state": "draft"}], auto_search={"contacted": 0})
    elif scenario == "missing_counts":
        saved["results"] = {}
    calls = []

    def status(workspace_id, venture_id, *, authorize=True):
        calls.append((workspace_id, venture_id, authorize))
        if scenario == "unavailable":
            raise ValueError("saved_records_unavailable")
        return deepcopy(saved)

    async def no_model(*_args, **_kwargs):
        pytest.fail("Outreach records and proof limits must not use model recall")

    monkeypatch.setattr(journey, "status", status)
    monkeypatch.setattr(assistant, "_complete", no_model)
    assert not assistant._asks_about_outreach_verification(f"Which {idea} leads have a public email address?")
    questions = ["Give me our saved outreach status, including responses.", "Have we sent any emails yet?",
                 "Who have we emailed so far?", "Are any responses recorded?", "When are the next follow-ups due?",
                 "Has anybody replied to us?"]
    if scenario == "active_users":
        questions = ["Have we acquired verified active product users?", "Are our active users verified?"]
    elif scenario == "read":
        questions = ["Have they read our message?", "Can you show proof of delivery?"]
    for question in questions:
        calls.clear()
        reply = asyncio.run(assistant.answer_question(vid, question, workspace_id=wid))
        body = reply["body_md"].lower()
        assert "\u2014" not in body and "\u2013" not in body
        assert "automated_content_must_not_appear" not in body and "draft_content_must_not_appear" not in body
        if scenario in {"active_users", "read"}:
            assert not calls and reply["actions"] == []
            assert ("cannot verify acquired active product users" if scenario == "active_users"
                    else "cannot verify whether your recipients received or read") in body
            continue
        assert calls == [(wid, vid, False)]
        assert reply["actions"] == ([{"label": "Retry", "action": "gtm.status", "payload": {}, "retry": True}] if scenario == "unavailable"
                                    else [{"label": "Open leads and replies", "action": "gtm.status", "style": "primary"}])
        if scenario == "sent":
            assert "3 sent, 1 replied" in body and "emails sent to 2 businesses" in body
            assert "1 human reply, 1 interested and 0 negative" in body
            assert "please share availability for a conversation" in body
            assert "2026-11-12 10:00 utc" in body and "2026-11-03" not in body
            assert "cannot verify outreach delivery" not in body
        elif scenario == "empty":
            assert "you have sent 0 outreach emails" in body and "no human replies yet" in body
        else:
            assert "cannot verify" in body and "0 outreach emails" not in body
    if scenario == "sent":
        monkeypatch.setattr(customers, "_view", lambda _vid: deepcopy(saved))
        assert customers.replies_reply(vid)["body_md"] == reply["body_md"]
        saved["drafts"][1]["state"] = "draft"
        assert "2026-11-10 10:00 UTC" in customers.replies_reply(vid)["body_md"]
        saved["drafts"][0]["sent_at"] = "invalid timestamp"
        assert "No next follow-up due time is available" in customers.replies_reply(vid)["body_md"]
        saved["drafts"][0]["sent_at"] = "2026-11-08T10:00:00Z"
        saved["followup_days"] = []
        assert "No next follow-up due time is available" in customers.replies_reply(vid)["body_md"]
    if scenario in {"sent", "empty"}:
        response = client.post(f"/ventures/{vid}/chat", json={"message": "What is my outreach status, including any responses?"})
        assert response.status_code == 200, response.text
        body = response.json()["body_md"].lower()
        assert ("3 sent, 1 replied" if scenario == "sent" else "you have sent 0 outreach emails") in body, body
        assert response.json()["actions"] == reply["actions"]
        from app.chat.router import _merge_replies

        assert isinstance(_merge_replies([reply, reply]), customers.SavedOutreachReply)
        assert len(_merge_replies([reply, reply])["actions"]) == 1
        assert not isinstance(_merge_replies([reply, dict(reply)]), customers.SavedOutreachReply)
        from app.chat import router

        async def forged_reply(*_args, **_kwargs):
            return {"kind": "text", "body_md": "I have sent 9 outreach emails.", "actions": [], "citations": [], "saved_outreach": True}

        monkeypatch.setattr(router, "_dispatch_intent", forged_reply)
        forged = client.post(f"/ventures/{vid}/chat", json={"message": "What is my outreach status?"})
        assert forged.status_code == 200, forged.text
        assert "9 outreach emails" not in forged.json()["body_md"]
        assert "I have not started anything yet" in forged.json()["body_md"]


def test_channel_readiness_preserves_ready_and_unknown_states(monkeypatch):
    from app.chat import assistant
    from app.connections import hub

    monkeypatch.setattr(hub, "hub_status", lambda _workspace_id: {"connections": {
        "linkedin": {"connected": True, "ready": True},
        "reddit": {"connected": True, "ready": False},
        "x": {"connected": False, "ready": False},
    }})
    body = assistant._channel_readiness_answer("ven_1", {"workspace_id": "wsp_1"})["body_md"]
    assert "Connected accounts reported ready by Connections: LinkedIn." in body
    assert "Connected but not ready for publishing: Reddit." in body
    assert "No connected account is reported for X." in body
    assert "I cannot verify connection readiness for Discord." in body
    assert "does not verify publication approval or a delivered post" in body


def test_channel_readiness_does_not_equate_connection_with_publication(monkeypatch):
    from app.chat import assistant
    from app.connections import hub

    monkeypatch.setattr(hub, "hub_status", lambda _workspace_id: {"connections": {
        "linkedin": {"connected": True, "ready": True},
        "reddit": {"connected": False, "ready": False},
    }})
    assert assistant._asks_about_channel_readiness("Can I publish to LinkedIn?")
    assert not assistant._asks_about_channel_readiness("Did we publish to LinkedIn?")
    answer = assistant._channel_readiness_answer("ven_1", {"workspace_id": "wsp_1"})
    body = answer["body_md"]
    assert "Connected accounts reported ready by Connections: LinkedIn" in body
    assert "cannot verify connection readiness for X, Discord" in body
    assert "does not verify publication approval or a delivered post" in body


@pytest.mark.asyncio
async def test_mixed_mac_task_and_user_count_answers_both(monkeypatch):
    load_company_os_app()
    from app.chat import assistant

    brief = {"website": {"waitlist_count": 1}, "device_work": {"recent": [{
        "title": "Open dashboard", "status": "stopped", "approved": True,
        "executed": True, "task_verified": True,
        "review_href": "/v/ven_test/device-work-orders/dwo_dashboard",
    }]}}
    monkeypatch.setattr(assistant, "get_venture_state_brief", lambda *_a, **_k: brief)
    answer = await assistant.answer_question(
        "ven_test", "What happened in the latest Mac task? How many active users do we have?"
    )
    assert "cannot verify a count of active product users" in answer["body_md"].lower()
    assert "open dashboard" in answer["body_md"].lower()
    assert answer["actions"][0]["href"].endswith("dwo_dashboard")


def test_saved_state_question_composes_live_site_metrics_and_product_blocker(env, monkeypatch):
    from app.chat import assistant
    from app.chat.intent import split_speech_acts

    question = (
        "For LabHire, answer from saved state only: what is live now, how many consented "
        "waitlist requests exist, how many verified active product users exist, and what "
        "blocks the authenticated application? Do not start a task or contact anyone."
    )
    assert split_speech_acts(question) == [("assistant", question)]
    client, vid = env
    brief = {
        "website": {"live": True, "url": "https://labhire.example/", "waitlist_enabled": True,
                    "waitlist_count": 3},
        "builds": {"last": {"state": "failed", "failure_reason": "old provider outage",
                            "retry_policy": "manual"}},
        "device_work": {"latest_coding_source": {
            "coding_provider": "codex", "status": "stopped", "source_state": "stored",
            "task_verified": False,
            "review_href": f"/v/{vid}/device-work-orders/dwo_source",
        }},
    }
    seen = []
    monkeypatch.setattr(assistant, "get_venture_state_brief",
                        lambda _vid, **kwargs: (seen.append(kwargs) or brief))

    async def no_model(*_args, **_kwargs):
        raise AssertionError("Composed saved facts should not depend on model recall")

    monkeypatch.setattr(assistant, "_complete", no_model)
    reply = client.post(f"/ventures/{vid}/chat", json={"message": question})
    assert reply.status_code == 200, reply.text
    answer = reply.json()
    body = answer["body_md"].lower()
    assert "landing page is live at https://labhire.example/" in body
    assert body.count("3 consented") == 1
    assert "cannot verify a count of active product users" in body
    assert "independent qa and hosting not attested for this source" in body
    assert "independent qa" in body and "hosting" in body
    assert "old provider outage" not in body
    assert answer["actions"] == []
    assert seen == [{"force": True, "mark_visit": True}]


@pytest.mark.asyncio
async def test_mixed_live_site_metrics_and_failed_build_without_coding_source(monkeypatch):
    load_company_os_app()
    from app.chat import assistant

    monkeypatch.setattr(assistant, "get_venture_state_brief", lambda *_args, **_kwargs: {
        "website": {"live": True, "url": "https://example.test/", "waitlist_count": 2},
        "builds": {"last": {"state": "failed", "failure_reason": "provider unavailable",
                            "retry_policy": "manual"}},
    })
    answer = await assistant.answer_question(
        "ven_test", "What is live now, how many waitlist requests exist, and what blocks the product application?"
    )
    body = answer["body_md"].lower()
    assert "landing page is live at https://example.test/" in body
    assert "2 consented early-access requests" in body
    assert "cannot verify a working authenticated product application" in body
    assert "provider unavailable" in body
    assert "no new build was started" in body
    assert answer["actions"] == []


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/intent_route.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_MEMORY_PATH", str(tmp_path / "mem.sqlite3"))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    main = load_company_os_app()
    configure_local_test_model()
    from app import state as state_mod

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    from tests.company_os_harness import company_os_test_client
    with company_os_test_client(main.create_app()) as client:
        created = client.post(
            "/ventures",
            json={"raw_idea": "LabHire for QA engineers"},
        )
        assert created.status_code == 201, created.text
        yield client, created.json()["id"]


def confirm_idea(vid):
    """A research quote needs a confirmed idea; a fresh fixture venture is still in website recon."""
    import json

    from app.core.local_mode import is_local
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.state import db
    from app.ventures.current_idea import revision_fields
    from tests.company_os_harness import append_idea_revision

    with db()() as session:
        idea = session.query(IdeaSession).filter_by(venture_id=vid).one()
        if is_local():
            revision = session.get(IdeaRevision, idea.current_revision_id)
            append_idea_revision(session, revision.id,
                structured=json.dumps({**revision_fields(revision), "_questions_skipped": True}))
        idea.state, idea.confirmed_revision_id = "confirmed", idea.current_revision_id
        session.commit()


def test_research_command_starts_requested_work_not_send_failure(env):
    from app.db.models import Job
    from app.state import db
    client, vid = env
    confirm_idea(vid)
    reply = client.post(
        f"/ventures/{vid}/chat",
        json={"message": "Research three competitors of LabHire."},
    )
    assert reply.status_code == 200, reply.text
    data = reply.json()
    assert data["kind"] == "text" and not any(action.get("confirmation") for action in data["actions"])
    assert "research" in data["body_md"].lower()
    assert data.get("actions")
    assert "Couldn't send that" not in data["body_md"]
    with db()() as session:
        assert session.query(Job).filter_by(venture_id=vid, kind="research.deep").one()


@pytest.mark.parametrize("idea", ["Inventory alerts for warehouse operators", "Walking groups for neighborhood families"])
def test_source_qa_request_reports_actual_capability_and_source_version(env, monkeypatch, idea):
    client, vid = env
    created = client.post("/ventures", json={"raw_idea": idea})
    assert created.status_code == 201, created.text
    vid = created.json()["id"]
    from app.ventures import state_brief

    source_sha = "a" * 64
    monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda *_args, **_kwargs: {
        "device_work": {"recent": [], "latest_coding_source":
                        {"coding_provider": "codex", "status": "stopped",
                         "source_state": "stored", "source_sha256": source_sha,
                         "review_href": f"/v/{vid}/device-work-orders/dwo_source"}},
    })
    reply = client.post(f"/ventures/{vid}/chat", json={"message":
                        "Run independent QA on the exact stored coding source. Do not deploy or send messages."})
    assert reply.status_code == 200, reply.text
    body = reply.json()["body_md"]
    assert source_sha in body
    assert "cannot run independent runtime QA" in body
    assert "I have not started QA" in body
    assert reply.json()["actions"] == [{"label": "Review stored source",
                                        "href": f"/v/{vid}/device-work-orders/dwo_source",
                                        "style": "primary"}]


@pytest.mark.hosted_only
def test_authenticated_chat_saves_closed_intent_analytics_without_message_text(env):
    client, vid = env
    reply = client.post(f"/ventures/{vid}/chat", json={"message": "What is LabHire doing now?"},
                        headers={"X-Subject": "founder"})
    assert reply.status_code == 200, reply.text
    import json

    from app.db.models import AnalyticsEvent
    from app.state import db
    from sqlalchemy import select

    with db()() as session:
        rows = session.scalars(select(AnalyticsEvent).where(
            AnalyticsEvent.venture_id == vid, AnalyticsEvent.event_name == "request.received")).all()
        outcomes = session.scalars(select(AnalyticsEvent).where(
            AnalyticsEvent.venture_id == vid, AnalyticsEvent.event_name == "request.outcome")).all()
    assert len(rows) == 1
    props = json.loads(rows[0].props_json)
    assert props == {"intent_categories": ["assistant"], "source": "chat",
                     "topics": ["status_blocker"], "work_requested": False}
    assert rows[0].person_id
    assert "LabHire" not in rows[0].props_json
    assert len(outcomes) == 1
    response = json.loads(outcomes[0].props_json)
    assert response == {"source": "chat", "request_event_id": rows[0].id,
                        "response_kind": "text", "offered_actions": 0}
    assert "LabHire" not in outcomes[0].props_json


@pytest.mark.hosted_only
def test_authenticated_gtm_guidance_records_feature_only_after_saved_reply(env):
    client, vid = env
    import json
    from sqlalchemy import select
    from app.db.models import AnalyticsEvent
    from app.state import db

    reply = client.post(f"/ventures/{vid}/chat",
                        json={"message": "Start getting users."},
                        headers={"X-Subject": "founder"})
    assert reply.status_code == 200, reply.text
    assert not any(a.get("href") for a in reply.json()["actions"])  # Find customers answers in chat
    with db()() as session:
        rows = session.scalars(select(AnalyticsEvent).where(
            AnalyticsEvent.venture_id == vid,
            AnalyticsEvent.event_name.in_(["chat.tool_invoked", "feature.used"]))).all()
    assert {(row.event_name, json.loads(row.props_json)["feature"]) for row in rows} == {
        ("chat.tool_invoked", "gtm"), ("feature.used", "gtm")}
    assert all("Start getting users" not in row.props_json for row in rows)


@pytest.mark.hosted_only
def test_chat_analytics_read_records_underlying_feature_only_when_metrics_served(env, monkeypatch):
    client, vid = env
    import json
    from sqlalchemy import select
    from app.db.models import AnalyticsEvent
    from app.state import db
    from app.ventures import state_brief

    monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda *_args, **_kwargs: {
        "analytics": {"state": "recorded", "counts": {"landing_view": 2},
                      "steps": ["landing_view"]},
    })
    headers = {"X-Subject": "founder"}
    navigation = client.post(f"/ventures/{vid}/chat", json={"message": "Open analytics in Supymem"}, headers=headers)
    assert navigation.status_code == 200, navigation.text
    read = client.post(f"/ventures/{vid}/chat", json={"message":
        "Open analytics in Supymem and tell me what the founder dashboard actually shows. "
        "Answer only; do not start work."}, headers=headers)
    assert read.status_code == 200, read.text
    assert "2 landing-views" in read.json()["body_md"]

    with db()() as session:
        rows = session.scalars(select(AnalyticsEvent).where(
            AnalyticsEvent.venture_id == vid,
            AnalyticsEvent.event_name.in_(["chat.tool_invoked", "feature.used"]))).all()
    assert [(row.event_name, json.loads(row.props_json)) for row in rows] == [
        ("chat.tool_invoked", {"source": "chat", "tool": "open_screen.analytics_read",
                               "feature": "analytics", "outcome": "returned"}),
        ("feature.used", {"source": "chat", "tool": "open_screen.analytics_read",
                          "feature": "analytics"}),
    ]
    assert all("LabHire" not in row.props_json for row in rows)


def test_saved_gtm_reply_survives_unavailable_usage_writer(env, monkeypatch):
    client, vid = env
    from app.analytics import chat_usage

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("usage writer unavailable")

    monkeypatch.setattr(chat_usage, "record_served_chat_read", unavailable)
    reply = client.post(f"/ventures/{vid}/chat", json={"message": "Start getting users."},
                        headers={"X-Subject": "founder"})
    assert reply.status_code == 200, reply.text
    history = client.get(f"/ventures/{vid}/chat")
    assert history.status_code == 200
    assert reply.json()["id"] in {item["id"] for item in history.json()["items"]}


@pytest.mark.hosted_only
@pytest.mark.parametrize("prompt", [
    "private founder prompt about shipment discrepancy alerts for freight coordinators",
    "private founder prompt about trail meetup planning for amateur hikers",
])
def test_saved_provider_error_reply_has_closed_operator_receipt(env, monkeypatch, prompt):
    client, vid = env
    import json

    from app.api import chat_api
    from app.db.models import AnalyticsEvent
    from app.state import db
    from llm_gateway.errors import ProviderCallError
    from sqlalchemy import select

    async def failed(_venture_id, _text, **_kwargs):
        raise ProviderCallError("provider_down", "private founder prompt and provider detail", retryable=True)

    monkeypatch.setattr(chat_api, "handle_message", failed)
    reply = client.post(f"/ventures/{vid}/chat", json={"message": prompt},
                        headers={"X-Subject": "founder"})
    assert reply.status_code == 200, reply.text
    assert reply.json()["kind"] == "error"
    history = client.get(f"/ventures/{vid}/chat", headers={"X-Subject": "founder"})
    assert history.status_code == 200
    saved = [item for item in history.json()["items"] if item.get("kind") == "error"]
    assert len(saved) == 1
    assert saved[0]["kind"] == "error"
    assert saved[0]["id"] == reply.json()["id"]
    with db()() as session:
        rows = session.scalars(select(AnalyticsEvent).where(
            AnalyticsEvent.venture_id == vid,
            AnalyticsEvent.event_name == "chat.reply_error")).all()
    assert len(rows) == 1
    assert rows[0].person_id
    assert json.loads(rows[0].props_json) == {"source": "chat", "failure_kind": "provider_call"}
    assert "private founder prompt" not in rows[0].props_json

    from app.analytics import chat_usage

    def broken_telemetry(*_args, **_kwargs):
        raise RuntimeError("telemetry storage unavailable")

    monkeypatch.setattr(chat_usage, "record_provider_error_reply", broken_telemetry)
    still_saved = client.post(f"/ventures/{vid}/chat", json={"message": "another private prompt"},
                              headers={"X-Subject": "founder"})
    assert still_saved.status_code == 200, still_saved.text
    assert still_saved.json()["kind"] == "error"
    history = client.get(f"/ventures/{vid}/chat")
    assert history.status_code == 200
    assert sum(item["kind"] == "error" for item in history.json()["items"]) == 2


def test_chat_analytics_topics_are_closed_and_do_not_copy_founder_entities():
    load_company_os_app()
    from app.chat.analytics_topics import classify_topics

    topics = classify_topics("Is the product ready? Please connect my Gmail API key to ExampleCo.")
    assert topics == ["integrations", "product_build"]
    assert classify_topics("an unusual private idea called Moonapple") == ["other"]


def test_build_failure_question_answers_from_brief(env, monkeypatch):
    client, vid = env
    import json

    from app.db.supymem_models import BuildRun, FounderApp
    from app.state import db
    from app.ventures.state_brief import invalidate_venture_state_brief
    from core.clock import utcnow
    from core.ids import new_id

    # Force fallback path (no live LLM).
    async def _no_llm(*_a, **_k):
        return ""

    monkeypatch.setattr("app.chat.assistant._complete", _no_llm)

    factory = db()
    with factory() as session:
        ven = None
        from app.db.models import Venture

        ven = session.get(Venture, vid)
        wid = ven.workspace_id
        app_id = new_id("app")
        session.add(
            FounderApp(
                id=app_id,
                workspace_id=wid,
                venture_id=vid,
                name="LabHire",
                slug=f"labhire-{app_id[-8:]}",
                kind="web",
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )
        session.flush()
        session.add(
            BuildRun(
                id=new_id("bld"),
                app_id=app_id,
                workspace_id=wid,
                venture_id=vid,
                trigger="test",
                agent="managed",
                state="failed",
                spec=json.dumps({"failure_reason": "provider down"}),
                credits_used=153_594,
                error_code="provider_down",
                exit_summary="provider down",
                runtime_version=1,
                created_at=utcnow(),
            )
        )
        session.commit()
    invalidate_venture_state_brief(vid)

    reply = client.post(
        f"/ventures/{vid}/chat",
        json={"message": "Why did the last build fail and what should we do about it?"},
    )
    assert reply.status_code == 200, reply.text
    body = reply.json()["body_md"]
    assert "don't have a record" not in body.lower()
    assert "provider" in body.lower() or "fail" in body.lower()
    assert "**Now**" not in body
    assert "**Needs you**" not in body


@pytest.mark.asyncio
async def test_product_readiness_prefers_new_coding_receipt_over_old_builder_failure(monkeypatch):
    load_company_os_app()
    from app.chat import assistant

    brief = {
        "workspace_id": "wsp_test", "idea": {"name": "Example"},
        "builds": {"last": {"state": "failed", "failure_reason": "provider down",
                            "retry_policy": "manual"}},
        "website": {"live": True, "url": "https://example.supymem.com/",
                    "waitlist_enabled": True, "waitlist_count": 3},
        "device_work": {"recent": [{"coding_provider": "codex", "status": "stopped",
                                    "task_verified": False,
                                    "review_href": "/v/ven_test/device-work-orders/dwo_test"}]},
    }
    seen = []
    monkeypatch.setattr(assistant, "get_venture_state_brief",
                        lambda _vid, **kwargs: (seen.append(kwargs) or brief))

    async def no_model(*_args, **_kwargs):
        raise AssertionError("stale model history must not replace the saved coding receipt")

    monkeypatch.setattr(assistant, "_complete", no_model)
    result = await assistant.answer_question(
        "ven_test",
        "What is blocked before Example can be called a live product, and what can Supymem do now without my approval?",
    )
    body = result["body_md"].lower()
    assert seen == [{"force": True, "mark_visit": True}]
    assert "landing page is live" in body
    assert "not verified product users" in body
    assert "immutable source" in body and "independently" in body
    assert "prior failed managed build" in body
    assert "retry" not in body
    assert result["actions"][0]["href"] == "/v/ven_test/device-work-orders/dwo_test"


def test_product_readiness_distinguishes_stored_source_from_qa_and_hosting():
    load_company_os_app()
    from app.chat.assistant import _coding_product_readiness_answer

    answer = _coding_product_readiness_answer({"device_work": {"recent": [{
        "coding_provider": "codex", "status": "stopped", "task_verified": False,
        "source_state": "stored", "review_href": "/v/ven_test/device-work-orders/dwo_test",
    }]}})
    assert answer is not None
    body = answer["body_md"].lower()
    assert "local source is stored in versioned custody" in body
    assert "independent qa" in body
    assert "hosted product application" in body
    assert "does not attest the local source" not in body
    assert "cannot start one yet for that product runtime" in body


@pytest.mark.asyncio
async def test_compound_product_blocker_question_uses_saved_source_receipt(monkeypatch):
    load_company_os_app()
    from app.chat import assistant
    from app.chat.intent import split_speech_acts

    question = (
        "What exactly blocks LabHire from having a working authenticated product at a public URL right now? "
        "Identify the saved source, QA and hosting state. Do not start a task or claim users."
    )
    assert split_speech_acts(question) == [("assistant", question)]
    monkeypatch.setattr(assistant, "get_venture_state_brief", lambda *_args, **_kwargs: {
        "website": {"live": True, "url": "https://labhire.supymem.com/",
                    "waitlist_enabled": True, "waitlist_count": 3},
        "device_work": {"recent": [{"coding_provider": "codex", "status": "stopped",
                                    "source_state": "stored", "source_sha256": "a" * 64,
                                    "task_verified": False,
                                    "review_href": "/v/ven_test/device-work-orders/dwo_test"}]},
    })

    async def no_model(*_args, **_kwargs):
        raise AssertionError("grounded readiness answer should not require the model")

    monkeypatch.setattr(assistant, "_complete", no_model)
    answer = await assistant.answer_question("ven_test", question)
    body = answer["body_md"].lower()
    assert "stored source sha-256" in body
    assert "independent qa" in body
    assert "hosted product application" in body
    assert "not verified product users" in body
    assert "several saved notes" not in body


@pytest.mark.parametrize('constraints', [
    'Do not search, build a product, publish, or send any messages.',
    "Don't search. Do not build a product or publish. Do not send any messages.",
    'Don’t search, publish or send any messages.',
])
def test_progress_with_trailing_constraints_precedes_manual_discovery(env, monkeypatch, constraints):
    from app.chat import assistant
    from app.chat.intent import classify_intent, split_speech_acts
    from app.db.models import Job
    from app.state import db
    from sqlalchemy import func, select
    client, vid = env
    question = ('Discovery update for LabHire: I sent three individual non-sales interview invitations '
                'to published trainer business contacts, following the manual Google-to-Gmail guide. '
                'All three are founder-reported sends; zero replies, interviews, signups or users. '
                'Solo-trainer fit remains unknown. What exact next action should I take now? ' + constraints)
    assert classify_intent(question) == 'assistant'
    assert split_speech_acts(question) == [('assistant', question)]
    captured = []
    async def complete(messages, **_kwargs):
        captured.append(messages)
        return 'Three reported invitations are pending; next check Gmail once for replies and record the actual outcome.'
    async def forbidden_search(*_args, **_kwargs):
        raise AssertionError('progress advice must not search')
    def forbidden_manual(*_args, **_kwargs):
        raise AssertionError('manual lookup must not replace acquisition progress advice')
    monkeypatch.setattr(assistant, '_complete', complete)
    monkeypatch.setattr('app.research.explore.explore_web', forbidden_search)
    monkeypatch.setattr('app.chat.search_recovery.manual_discovery_reply', forbidden_manual)
    with db()() as session:
        before = session.scalar(select(func.count(Job.id)).where(Job.venture_id == vid))
    reply = client.post(f'/ventures/{vid}/chat', json={'message': question})
    assert reply.status_code == 200, reply.text
    assert len(captured) == 1
    prompt = '\n'.join(message['content'] for message in captured[0])
    assert question in prompt
    assert 'not independent verification' in prompt
    assert 'Three reported invitations' in reply.json()['body_md'] and reply.json()['actions'] == []
    with db()() as session:
        assert session.scalar(select(func.count(Job.id)).where(Job.venture_id == vid)) == before


@pytest.mark.parametrize('text', [
    'Discovery update: I sent three invitations. What should I do next? Do not search. Build a product.',
    'Discovery update: I sent three invitations. What should I do next? Do not search, but draft an email.',
    'Discovery update: I sent three invitations. What should I do next? Then publish the site.',
])
def test_trailing_positive_operations_do_not_become_progress_advice(text):
    load_company_os_app()
    from app.chat.intent import discovery_progress_question
    assert not discovery_progress_question(text)


def test_manual_lookup_without_progress_report_still_uses_manual_route(env, monkeypatch):
    client, vid = env
    from app.chat import assistant
    async def forbidden(*_args, **_kwargs):
        raise AssertionError('explicit manual lookup should not invoke a model or live search')
    monkeypatch.setattr(assistant, '_complete', forbidden)
    monkeypatch.setattr('app.research.explore.explore_web', forbidden)
    question = ('Give me a manual Google-to-Gmail discovery guide to find three independent trainers in Bengaluru. '
                'Do not search or send any messages.')
    reply = client.post(f'/ventures/{vid}/chat', json={'message': question})
    assert reply.status_code == 200, reply.text
    assert 'Manual alternative' in reply.json()['body_md'] and 'Compose' in reply.json()['body_md']
    assert reply.json()['citations'] == []
    assert reply.json()['actions'] == []


@pytest.mark.parametrize("audience", ["Independent bicycle repair shops", "First-time cat owners"])
def test_manual_discovery_guide_takes_precedence_over_written_material(env, monkeypatch, audience):
    from app.chat import assistant

    client, vid = env

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("Manual discovery must not invoke a model or live search")

    monkeypatch.setattr(assistant, "_complete", forbidden)
    monkeypatch.setattr("app.research.explore.explore_web", forbidden)
    question = f"Give me a manual discovery guide to find {audience}. Do not search or send any messages."
    reply = client.post(f"/ventures/{vid}/chat", json={"message": question})
    assert reply.status_code == 200, reply.text
    assert "Manual alternative" in reply.json()["body_md"] and "Compose" in reply.json()["body_md"]
    assert reply.json()["citations"] == reply.json()["actions"] == []


def test_progress_report_with_positive_research_starts_requested_work(env, monkeypatch):
    from app.db.models import Job
    from app.state import db
    client, vid = env
    confirm_idea(vid)
    async def forbidden(*_args, **_kwargs):
        raise AssertionError('your requested venture research must use the durable research path')
    monkeypatch.setattr('app.research.explore.explore_web', forbidden)
    question = 'Discovery update: I sent three invitations. What should I do next? Research three competitors of LabHire.'
    reply = client.post(f'/ventures/{vid}/chat', json={'message': question})
    assert reply.status_code == 200, reply.text
    assert not any(action.get('confirmation') for action in reply.json()['actions']), reply.json()
    with db()() as session:
        assert session.query(Job).filter_by(venture_id=vid, kind="research.deep").one()


def test_direct_mac_request_is_coming_soon_when_native_gate_is_off(env, monkeypatch):
    monkeypatch.delenv("DEVICE_DESKTOP_EXECUTION_ENABLED", raising=False)
    from app.core.local_mode import is_local

    if is_local():
        from app.core.local_secrets import update_settings

        update_settings("capabilities", {"background_work": False}, {})
    client, vid = env
    reply = client.post(f"/ventures/{vid}/chat", json={"message": DIRECT_NATIVE_REQUEST}).json()
    from app.chat.recovery import button

    if is_local():
        from app.core.local_capabilities import blocked_message

        assert blocked_message("computer_use") in reply["body_md"]
        assert not reply["actions"]
    else:
        assert "coming soon in beta" in reply["body_md"] and reply["actions"] == [button("mac.task")]

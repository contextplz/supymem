"""Speech-act classifier: questions are not tasks."""

from __future__ import annotations

import pytest


@pytest.mark.parametrize("audience", ["Warehouse dispatch teams", "Weekend climbing groups"])
@pytest.mark.parametrize("mode", ["local", "hosted"])
def test_reviewed_test_keeps_its_full_request_instead_of_starting_a_customer_search(monkeypatch, audience, mode):
    load_company_os_app()
    from app.chat.intent import WORK_INTENTS, classify_intent, split_speech_acts

    monkeypatch.setenv("SUPYMEM_MODE", mode)
    request = f"Let's run the test from the review: send a reminder to {audience} and see if they act"
    assert classify_intent(request) == "experiment_from_review"
    assert split_speech_acts(request) == [("experiment_from_review", request)]
    for message in ("Do not run the test from the review for " + audience,
                    "Explain how to run the test from the review for " + audience, request + ". Answer only."):
        intent = classify_intent(message)
        assert intent in {"assistant", "general"} and intent not in WORK_INTENTS
        assert split_speech_acts(message) == [(intent, message)]

from tests.company_os_harness import load_company_os_app, local_chat as local_chat


@pytest.mark.parametrize("idea", ["freight dispatch software", "a home baking club"])
@pytest.mark.parametrize("phrase,intent", [
    ("Pressure-test my idea with market research: {idea}", "research.start"),
    ("Research the market for {idea}.", "research.start"),
    ("Pressure-test with research for {idea}.", "research.start"),
    ("Can you find competitors for {idea}?", "research.start"),
    ("Do market research for {idea}.", "research.start"),
    ("Start competitor research for {idea}.", "research.start"),
    ("Market research for {idea}", "research.start"),
    ("Competitors for {idea}?", "research.start"),
    ("Run office hours for {idea}.", "office_hours"),
    ("Ask me forcing questions about {idea}.", "office_hours"),
    ("Coach me on validating {idea}.", "office_hours"),
    ("Pressure-test my idea for {idea} by asking me questions.", "office_hours"),
    ("What is market research for {idea}?", "assistant"),
    ("Don't start market research for {idea}.", "assistant"),
    ("Give me advice on market research for {idea}.", "assistant"),
])
def test_local_explicit_research_and_office_hours(monkeypatch, idea, phrase, intent):
    load_company_os_app()
    from app.chat.intent import classify_intent, split_speech_acts

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    text = phrase.format(idea=idea)
    classified = classify_intent(text)
    assert classified == intent or intent == "assistant" and classified == "general"
    assert split_speech_acts(text) == [(classified, text)]


@pytest.mark.parametrize("idea", [
    "Equipment inspection reminders for factory supervisors",
    "Weekend pottery lessons for hobbyists",
])
@pytest.mark.parametrize("message,intent", [
    ("Build a private landing page for {idea}.", "pagebuilder.build"),
    ("Make me a page for {idea}.", "pagebuilder.build"),
    ("Create a website for {idea}.", "pagebuilder.build"),
    ("Landing page for {idea}", "pagebuilder.build"),
    ("One-pager site for {idea}", "pagebuilder.build"),
    ("Design our homepage for {idea}.", "pagebuilder.build"),
    ("Could you build a pricing page for {idea}?", "pagebuilder.build"),
    ("Generate a product landing page for {idea}.", "pagebuilder.build"),
    ("Build my website for {idea}. Keep it private; do not send emails.", "pagebuilder.build"),
    ("Kick off the landing page build.", "pagebuilder.build"),
    ("Can you edit the landing page for {idea}?", "pagebuilder.build"),
    ("Build the app for {idea}.", "build.tool.start"),
    ("Build my backend for {idea}.", "build.tool.start"),
    ("Write the code for the product for {idea}.", "build.tool.start"),
    ("Create an application for {idea}.", "build.tool.start"),
    ("Make a product for {idea}.", "build.tool.start"),
    ("Develop the app for {idea}.", "build.tool.start"),
    ("Implement the backend for {idea}.", "build.tool.start"),
    ("Build the app with a landing page for {idea}.", "build.tool.start"),
    ("Build the backend for my website for {idea}.", "build.tool.start"),
    ("Build a website monitoring app for {idea}.", "build.tool.start"),
])
def test_local_page_and_product_build_routing(monkeypatch, idea, message, intent):
    load_company_os_app()
    from app.chat.actions import beta_blocked, get_action
    from app.chat.intent import WORK_INTENTS, classify_intent, split_speech_acts
    from app.core import local_capabilities

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    checked = []
    monkeypatch.setattr(local_capabilities, "enabled", lambda name: checked.append(name) or True)
    text = message.format(idea=idea)
    assert classify_intent(text) == intent
    assert split_speech_acts(text) == [(intent, text)]
    assert intent in WORK_INTENTS
    if intent == "pagebuilder.build":
        assert get_action(intent).endpoint == "pagebuilder.api._start"
        assert beta_blocked(intent) is None
        assert checked == ["landing_pages"]
    else:
        assert beta_blocked(intent) is None
        assert get_action(intent).required_permission == "request_work"
        assert checked == ["build_with_my_tool"]


@pytest.mark.parametrize("message", [
    "How do I build a landing page?", "Should I create a website?",
    "Do not build the landing page.", "Show me my website.",
    "I built a landing page for my idea.", "Explain how to write the code for my app.",
])
def test_local_page_build_requires_a_request(monkeypatch, message):
    load_company_os_app()
    from app.chat.intent import classify_intent

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    assert classify_intent(message) not in {"pagebuilder.build", "start_product_build"}


@pytest.mark.parametrize("state", ["off", "needs_setup"])
def test_local_page_build_uses_owner_capability_next_step(monkeypatch, state):
    load_company_os_app()
    from app.chat.actions import beta_blocked, disabled_reason
    from app.core import local_capabilities

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    checked = []
    message = ("Open Settings > Features and turn on Landing pages." if state == "off" else
               "Open Settings > Keys and connections and save your model-provider key.")
    monkeypatch.setattr(local_capabilities, "enabled", lambda name: checked.append(name) or False)
    monkeypatch.setattr(local_capabilities, "blocked_message", lambda name: message)
    assert disabled_reason("pagebuilder.build") == message
    assert beta_blocked("pagebuilder.build")["body_md"] == message
    assert checked == ["landing_pages", "landing_pages"]


@pytest.mark.asyncio
@pytest.mark.parametrize("idea", ["Freight dispatch notes for depot managers", "Drawing challenges for weekend hobbyists"])
async def test_private_build_constraints_keep_one_click_dispatch(monkeypatch, idea):
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    load_company_os_app()
    from app.chat import execute, router
    from app.chat.context import ChatContext
    from app.chat.intent import _ONE_CLICK_PAGE_RE, classify_intent
    from app.core.local_mode import is_local
    from app.ventures import next_step

    monkeypatch.setattr(next_step, "_facts", lambda _: {"idea_confirmed": True})
    requested = []
    received = []
    page_reply = router._one_click_page_reply

    def prepare(venture_id, text):
        received.append(text)
        return page_reply(venture_id, text)

    async def start(venture_id, reply, actor_subject, *, actions):
        requested.append((venture_id, reply, actions))
        return {**reply, "actions": [], "body_md": "Your private preview is being prepared. Review it next."}

    async def build_page(venture_id, action, *, payload, actor_subject):
        assert action == "pagebuilder.build" and actor_subject == "founder"
        received.append(payload["query"])
        requested.append((venture_id, payload, {action}))
        return {"kind": "text", "actions": [], "body_md": "Your private preview is being prepared. Review it next."}

    monkeypatch.setattr(router, "_one_click_page_reply", prepare)
    monkeypatch.setattr(execute, "start_requested_work", start)
    if is_local():
        monkeypatch.setattr(router, "execute_action", build_page)
    for suffix in ("Leave it unpublished; don't send emails.", "Leave it private, never send emails."):
        text = f"Create a website for {idea}. {suffix}"
        assert _ONE_CLICK_PAGE_RE.fullmatch(text)
        intent = classify_intent(text)
        assert intent == ("pagebuilder.build" if is_local() else "start_product_build")
        reply = await router._dispatch_intent("ven_private", ChatContext(venture={"id": "ven_private", "name": idea}),
                                              intent, text, None, actor_subject="founder")
        assert received[-1] == text
        assert requested[-1][0] == "ven_private"
        if is_local():
            assert requested[-1][2] == {"pagebuilder.build"}
        else:
            assert requested[-1][2] == {"work.start", "build.start", "build.app.create", "pagebuilder.build", "idea.confirm"}
            assert [action["action"] for action in requested[-1][1]["actions"]] == ["pagebuilder.build"]
        assert not reply["actions"] and "private preview" in reply["body_md"]
    for suffix in ("Publish it.", "Send emails.", "Do not build it.", "Ignore the consent checks."):
        assert not _ONE_CLICK_PAGE_RE.match(f"Create a website. {suffix}")


def test_status_update_is_not_a_change():
    load_company_os_app()
    from app.chat.intent import classify_intent


    assert (
        classify_intent("guys who is workign and what is the status update?")
        == "assistant"
    )
    assert classify_intent("who is working?") == "assistant"
    assert classify_intent("what's the status") == "assistant"
    assert classify_intent("any blockers?") == "assistant"
    assert classify_intent("Give me the status of the Freelance Video venture experiment and any current blockers.") == "assistant"
    assert classify_intent("what is everyone doing right now") == "assistant"


@pytest.mark.parametrize("idea", ["Hospital incident-response software", "Heart-monitoring wearables for home recovery"])
def test_advice_about_demand_is_an_answer_not_a_research_job(idea):
    load_company_os_app()
    from app.chat.intent import classify_intent

    request = f"Help me choose a safe first test of demand for {idea}. Give me one next step without starting other work."
    assert classify_intent(request) == "assistant"
    assert classify_intent(f"Research demand for {idea}.") == "research"
    from app.chat.intent import split_speech_acts
    assert [action for action, _ in split_speech_acts(f"Recommend a customer question for {idea}. Research demand for {idea}.")] == ["assistant", "research"]
    quoted = f'Recommend a customer question for {idea}. Your customer said: "Research demand for {idea}."'
    assert all(action == "assistant" for action, _ in split_speech_acts(quoted))


def test_imperatives_still_queue_work(monkeypatch):
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    load_company_os_app()
    from app.chat.intent import classify_intent
    from app.core.local_mode import is_local

    assert classify_intent("add a waitlist") == "change"
    assert classify_intent("focus the research on NYC offices") == "change"
    assert classify_intent("can you build a pricing page") == ("pagebuilder.build" if is_local() else "start_product_build")
    assert classify_intent("@Cole ship the pricing page") == "change"
    assert classify_intent("@Maya research competitors in form builders") == "research"
    assert classify_intent("change the headline on the landing page") == "change"


def test_getting_users_command_with_guidance_and_no_send_constraint_stays_reviewed():
    load_company_os_app()
    from app.chat.intent import classify_intent, split_speech_acts

    request = (
        "Start getting users. First tell me the exact next step and what needs "
        "my approval. Do not send mail, publish, or contact anyone."
    )
    assert classify_intent(request) == "gtm_review"
    assert split_speech_acts(request) == [("gtm_review", request)]
    assert classify_intent("Don't start getting users yet") != "gtm_review"


def test_next_get_users_question_is_reviewed_without_catching_unrelated_next_steps():
    load_company_os_app()
    from app.chat.intent import classify_intent, split_speech_acts

    request = ("I already have a product. What should I do next to get users? "
               "Do not start paid work or send outreach.")
    assert classify_intent(request) == "gtm_review"
    assert split_speech_acts(request) == [("gtm_review", request)]
    assert classify_intent("What's next to get users?") == "gtm_review"
    assert classify_intent("What is the next step to find users?") == "gtm_review"
    assert classify_intent("What should I do next to fix the product?") != "gtm_review"


def test_declined_approval_never_offers_a_decision_card():
    load_company_os_app()
    from app.chat.intent import classify_intent, split_speech_acts

    question = (
        "Why can’t I proceed? Tell me the single next action. "
        "Do not approve this plan or start a build."
    )
    assert classify_intent(question) == "assistant"
    assert split_speech_acts(question) == [("assistant", question)]
    assert classify_intent("Do not approve this plan") == "assistant"
    assert classify_intent("Approve this plan") == "plan_approve"
    assert classify_intent("I approve this plan") == "approve"
    assert classify_intent("Reject the pending request") == "approve"
    personal_question = (
        "What should I do next for this personal project? "
        "Do not start a build or approve anything."
    )
    assert classify_intent(personal_question) == "assistant"
    assert split_speech_acts(personal_question) == [("assistant", personal_question)]
    assert classify_intent("Should I approve the latest request?") == "assistant"
    assert classify_intent("Tell me what needs approval; do not act") == "assistant"


def test_instructions_are_not_keyword_lookups():
    load_company_os_app()
    from app.chat.intent import classify_intent
    from app.core.local_mode import is_local

    assert (
        classify_intent("competitors are DoorDash, Toast, and Resy — not the NYTimes")
        == "change"
    )
    assert (
        classify_intent("draft an email to Priya about the waitlist, do not send")
        == "outreach_draft"
    )
    assert classify_intent("draft Priya note") == "change"
    assert classify_intent("waitlist only — rewrite the landing") == "change"
    assert (
        classify_intent(
            "rewrite the headline to Stop waiting on the lobby, competitors are "
            "DoorDash Toast and Resy, draft an email to Priya, do not send"
        )
        == "change"
    )
    assert classify_intent("stop that") == "cancel"
    assert classify_intent("cancel generic discovery") == "cancel"
    assert classify_intent("split the work") == "cancel"
    assert classify_intent("what's my email address") == "factual"
    assert classify_intent("who signed up for the waitlist") == "assistant"
    assert classify_intent("research the market") == ("research.start" if is_local() else "research")


def test_questions_are_lookups_not_work():
    load_company_os_app()
    from app.chat.intent import LOOKUP_INTENTS, classify_intent, split_speech_acts
    from app.core.local_mode import is_local

    assert classify_intent("How many waitlist signups do we have?") in LOOKUP_INTENTS
    assert classify_intent("who is working right now?") == "assistant"
    assert classify_intent("what's the status of LobbyLock?") == "assistant"
    assert classify_intent("is the landing page live?") in LOOKUP_INTENTS
    assert classify_intent("why is NYTimes listed as a competitor?") == "assistant"
    assert classify_intent("who are our competitors?") == "research.competitors"
    assert classify_intent("can you rewrite the headline?") == "change"
    assert classify_intent("Can you edit the landing page?") == ("pagebuilder.build" if is_local() else "change")
    assert classify_intent(
        "What is the current conversion path on FollowPilot's published landing page, "
        "and can this venture edit that page to link to its open managed waitlist? "
        "Answer only; do not create a Mac task."
    ) == "assistant"
    assert classify_intent(
        "For this venture, the page sends visitors to an unconfigured pilot page. "
        "Can you update the published page to link to the open waitlist? "
        "Tell me what review is required."
    ) == "assistant"
    assert classify_intent("competitors are Neighbor, FatLlama, and Home Depot") == "change"

    advisory = (
        "For this venture, give me one specific next GTM action using only the saved "
        "diagnosis and live waitlist. Explain what I must approve, and what you will "
        "do after approval. Do not send, publish, or prepare a new task yet."
    )
    assert classify_intent(advisory) == "assistant"
    assert split_speech_acts(advisory) == [("assistant", advisory)]
    assert classify_intent("Draft an email to Priya. Do not prepare a new task yet.") == "change"

    mixed = split_speech_acts(
        "How many waitlist signups do we have? Rewrite the headline to Stop waiting on the lobby"
    )
    assert len(mixed) == 2
    assert mixed[0][0] in LOOKUP_INTENTS
    assert mixed[1][0] == "change"
    compound = split_speech_acts(
        "rewrite the headline to Stop waiting on the lobby, competitors are "
        "DoorDash Toast and Resy, draft an email to Priya, do not send"
    )
    assert compound == [
        (
            "change",
            "rewrite the headline to Stop waiting on the lobby, competitors are "
            "DoorDash Toast and Resy, draft an email to Priya, do not send",
        )
    ]


def test_retarget_and_known_commands():
    load_company_os_app()
    from app.chat.intent import classify_intent
    from app.core.local_mode import is_local

    assert classify_intent("change the current task to rewrite the hero") == "retarget"
    assert classify_intent("do the first two only") == "slate_override"
    assert classify_intent("what's next") == "assistant"
    assert classify_intent("office hours — is this worth building?") == "office_hours"
    assert classify_intent("ceo review this plan") == "ceo_review"
    assert classify_intent("what do I know about Alice") == "brain_brief"
    assert classify_intent("brief me on Acme") == "brain_brief"
    assert classify_intent("post this to twitter: we launched") == "social_post"
    assert classify_intent("tweet this: shipping today") == "social_post"
    assert classify_intent("Explain how to connect X") == "social_guide"
    assert classify_intent("help me connect twitter") == ("connections.connect" if is_local() else "social_guide")
    assert classify_intent("I have a Zernio API key") == "social_guide"
    assert classify_intent("posting isn't working") == "social_status"
    assert classify_intent("is posting ready") == "social_status"
    assert classify_intent("Open the active LabHire build run. Do not start another.") == "open_build"
    assert classify_intent("Show me the current product build") == "open_build"
    assert classify_intent("build a new product") == ("build.tool.start" if is_local() else "start_product_build")
    fake = "sk_" + ("ab" * 32)
    assert classify_intent(f"here is my key {fake}") == "social_connect"


def test_strategy_questions_get_answers_and_direct_commands_find_people():
    load_company_os_app()
    from app.chat.intent import find_customers_request, strategy_question
    from app.chat.offers import offer_buttons

    ideas = "what are the different ways i could get my first 10 home bakers to try this"
    assert strategy_question(ideas) and not find_customers_request(ideas)
    assert strategy_question("how could I get my first users")
    assert offer_buttons(ideas, "Here are a few ways.")[0]["text"] == "find me people i should talk to"
    assert not strategy_question("how do i connect gmail")
    for command in ("find me people i should talk to", "skip these, just find me people i should talk to",
                    "who should I talk to", "find customers"):
        assert find_customers_request(command) and not strategy_question(command), command

"""Prompt contract for the venture chat assistant."""

from __future__ import annotations

import asyncio
import json
import re

import pytest

from tests.company_os_harness import load_company_os_app, local_chat as local_chat
from tests.unit.chat.test_round3_matrix import venture as venture


@pytest.fixture
def hosted_venture(monkeypatch, request):
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    return request.getfixturevalue("venture")


@pytest.mark.parametrize("domain", ["Warehouse maintenance", "Family hiking"])
def test_next_moves_preserves_navigation_and_chat_actions(monkeypatch, domain):
    load_company_os_app()
    from app.chat import assistant, next_moves

    actions = [{"label": "Keys and connections", "href": "/settings/local", "style": "primary"},
               {"label": "Show your saved status", "action": "status"}]
    card = {"actions": actions}
    monkeypatch.setattr(next_moves, "compose", lambda venture_id: card)
    messages = [{"role": "system", "content": "Guide you."}, {"role": "user", "content": domain}]
    assert asyncio.run(assistant.add_next_moves(messages, "venture")) == card
    assert '"href": "/settings/local"' in messages[1]["content"]
    assert '"action": "status"' in messages[1]["content"]
    assert messages[1]["content"].endswith(domain)


@pytest.mark.hosted_only
@pytest.mark.parametrize("idea, terms", [
    ("Inspection scheduling for food wholesalers", "Nine stockrooms, six inspectors, and a 45-minute review."),
    ("Used board game swaps for local hobbyists", "Seventeen players, three meetups, and a 20-minute review."),
])
def test_fresh_private_chat_recalls_and_continues_only_authorized_prior_work(hosted_venture, monkeypatch, idea, terms):
    from app.authz.workspace import ensure_subject_person
    from app.chat import assistant, conversations
    from app.chat.store import append_message, list_messages
    from app.db.models import WorkspaceMember
    from app.state import db
    from app.tenancy import bind
    from core.clock import utcnow
    from core.ids import new_id

    client, _, _ = hosted_venture
    owner, outsider = "per_trial_owner", "per_trial_outsider"
    headers = {"X-Subject": owner}
    created = client.post("/ventures", headers=headers, json={"raw_idea": idea})
    assert created.status_code == 201, created.text
    target = created.json()
    created = client.post("/ventures", headers=headers, json={"raw_idea": "A separate venture"})
    assert created.status_code == 201, created.text
    other = created.json()
    plan = f"## Trial outline\n\n{terms}\nRecord attendance and review the notes together."
    with bind(workspace_id=target["workspace_id"], venture_id=target["id"], actor=owner):
        source = conversations.create(target["id"], idempotency_key="saved-outline")["id"]
        with conversations.bind_conversation(target["id"], source):
            append_message(target["id"], role="user", body_md=f"Keep this trial outline for {idea}: {terms}")
            saved = append_message(target["id"], role="assistant", body_md=plan)
            for number in range(14):
                append_message(target["id"], role="user", body_md=f"Unrelated note number {number}.")
        conversations.update(target["id"], source, {"archived": True})
        fresh = conversations.create(target["id"], idempotency_key="fresh-outline")["id"]
    with bind(workspace_id=other["workspace_id"], venture_id=other["id"], actor=owner):
        foreign = conversations.create(other["id"], idempotency_key="separate-outline")["id"]
        with conversations.bind_conversation(other["id"], foreign):
            append_message(other["id"], role="assistant", body_md="Separate venture private trial: 83 invitations.")

    assert client.get(f"/ventures/{other['id']}/chat", headers=headers,
                      params={"conversation_id": source}).status_code == 404
    assert client.get(f"/ventures/{target['id']}/chat", headers={"X-Subject": outsider},
                      params={"conversation_id": source}).status_code == 404
    ensure_subject_person(outsider)
    with db()() as session:
        session.add(WorkspaceMember(id=new_id("wsm"), workspace_id=target["workspace_id"],
                                    person_id=outsider, role="viewer", created_at=utcnow()))
        session.commit()
    with bind(workspace_id=target["workspace_id"], venture_id=target["id"], actor=outsider):
        private = conversations.create(target["id"], idempotency_key="outsider-outline")["id"]
        with conversations.bind_conversation(target["id"], private):
            append_message(target["id"], role="assistant", body_md="Another account private trial: 61 invitations.")
    assert client.get(f"/ventures/{target['id']}/chat", headers={"X-Subject": outsider},
                      params={"conversation_id": source}).status_code == 404

    prompts = []
    serialized_plan = json.dumps(plan, ensure_ascii=False)[1:-1]
    async def complete(messages, **_kwargs):
        prompts.append(messages[1]["content"])
        return (plan + "\n\nNext step: Choose your first review date." if serialized_plan in prompts[-1]
                else "Your earlier outline is unavailable. Share it here to continue.")
    monkeypatch.setattr(assistant, "_complete", complete)
    questions = ("What was the trial outline in our earlier conversation?",
                 "Write the missing review questions for our earlier trial outline. Keep the agreed quantities.")
    with bind(workspace_id=target["workspace_id"], venture_id=target["id"], actor=owner):
        with conversations.bind_conversation(target["id"], fresh):
            assert list_messages(target["id"]) == []
            for question in questions:
                response = client.post(f"/ventures/{target['id']}/chat", headers=headers,
                                       json={"message": question, "conversation_id": fresh})
                assert response.status_code == 200, response.text
                reply = response.json()
                assert terms in reply["body_md"], reply
                assert serialized_plan in prompts[-1]
                assert saved["id"] in prompts[-1] and source in prompts[-1]
                assert "83 invitations" not in prompts[-1] and "61 invitations" not in prompts[-1]
            supplied = asyncio.run(assistant.question_messages(target["id"], questions[0], brief={"idea": {}},
                workspace_id=target["workspace_id"], supplied_evidence_only=True))
            assert terms not in str(supplied)
        with conversations.bind_conversation(target["id"]):
            shared = asyncio.run(assistant.question_messages(target["id"], questions[0], brief={"idea": {}},
                                                             workspace_id=target["workspace_id"]))
            assert terms not in str(shared) and "61 invitations" not in str(shared)
        assert next(message for message in list_messages(target["id"], conversation_id=source)
                    if message["id"] == saved["id"])["body_md"] == saved["body_md"]
    with (bind(workspace_id=target["workspace_id"], venture_id=target["id"], actor=outsider),
          conversations.bind_conversation(target["id"], private)):
        isolated = asyncio.run(assistant.question_messages(target["id"], questions[0], brief={"idea": {}},
                                                           workspace_id=target["workspace_id"]))
        assert terms not in str(isolated) and saved["id"] not in str(isolated)
    with (bind(workspace_id=other["workspace_id"], venture_id=other["id"], actor=owner),
          conversations.bind_conversation(other["id"], foreign)):
        isolated = asyncio.run(assistant.question_messages(other["id"], questions[0], brief={"idea": {}},
                                                           workspace_id=other["workspace_id"]))
        assert terms not in str(isolated) and saved["id"] not in str(isolated)


@pytest.mark.parametrize("audience", ["Enterprise incident responders", "Families planning nature walks"])
def test_default_reply_cleanup_keeps_bold_questions_below_a_heading(audience):
    load_company_os_app()
    from app.chat.assistant import _finalize_body

    questions = [f"What do {audience.lower()} need?", "How will you reach them?", "What have you tried?"]
    body = "Your idea is saved.\n\nQuestions to consider\n\n" + "\n".join(
        f"{index}. **{question}**" for index, question in enumerate(questions, 1))
    cleaned = _finalize_body(body, "hey")
    assert all(question in cleaned for question in questions)
    assert not cleaned.rstrip().endswith("Questions to consider")
    assert _finalize_body(body, "Write a list of questions") == body


def test_founder_understanding_stays_within_prompt_budget():
    load_company_os_app()
    from app.chat.assistant import FOUNDER_UNDERSTANDING
    from app.credits.estimate import count_prompt_tokens

    assert count_prompt_tokens([{"role": "system", "content": FOUNDER_UNDERSTANDING}]) < 150
    assert len(FOUNDER_UNDERSTANDING.encode("utf-8")) <= 420


def test_no_model_fallback_uses_display_brief_never_internal_prompt_labels():
    load_company_os_app()
    from app.chat.assistant import _fallback_answer
    brief = {"idea": {"name": "Coffee", "raw_idea": "Latest founder statement (wins over earlier fields): secret scaffolding",
        "idea_summary": "A coffee shop with online table reservations."}}
    for question in ("Hello", "Who is this for?"):
        answer = _fallback_answer(question, brief)
        assert "coffee shop with online table reservations" in answer
        assert "Latest founder" not in answer and "scaffolding" not in answer


@pytest.mark.parametrize("idea", [
    "Security incident evidence for enterprise response teams",
    "A wearable glucose monitor for people managing diabetes",
])
def test_first_minutes_fallback_answers_or_offers_an_honest_retry(idea):
    load_company_os_app()
    from app.chat.assistant import _fallback_answer

    brief = {"idea": {"idea_summary": idea}, "research": {"present": False},
             "website": {"waitlist_count": 0}}
    for question in ("What can you do?", "Thanks", "Who are my competitors?", "Who is this for?"):
        answer = _fallback_answer(question, brief)
        assert "waitlist" not in answer.lower()
        assert "ICP" not in answer and "brief" not in answer.lower()
        assert any(step in answer.lower() for step in ("ask", "tell me", "share", "say"))
        if "competitors" in question:
            assert "competitor" in answer.lower() and "not" in answer.lower()
        elif "for" not in question:
            assert "could not answer" in answer.lower()


@pytest.mark.parametrize("idea", [
    "Security incident evidence for enterprise response teams",
    "A wearable glucose monitor for people managing diabetes",
])
@pytest.mark.parametrize("reasoning_on", [False, True])
def test_first_minutes_prompt_answers_before_suggesting_unrequested_work(idea, reasoning_on, monkeypatch):
    load_company_os_app()
    from app.chat.assistant import SYSTEM_PROMPT, NEXT_MOVES_GUIDANCE, build_messages
    from app.chat.agent import TOOL_RULES
    from app.org.manager import CAPABILITIES, PERSONA_RULES, capability_block
    from app.tasks import reasoning

    monkeypatch.setattr(reasoning, "enabled", lambda: reasoning_on)

    messages = build_messages(question="What can you do?",
                              brief={"_rendered": idea, "idea": {"idea_summary": idea}}, history=[])
    assert idea in messages[1]["content"]
    assert "Confirm only before" not in SYSTEM_PROMPT
    assert "founder-confirmed cards" not in SYSTEM_PROMPT
    assert "does not replace consent" in SYSTEM_PROMPT
    assert "internal field names" in SYSTEM_PROMPT
    assert "saved evidence" in SYSTEM_PROMPT
    assert "General market explanations are hypotheses" in SYSTEM_PROMPT
    assert "Role preferences shape your answer, not its subject" in SYSTEM_PROMPT
    assert "Short follow-ups are not status requests" in SYSTEM_PROMPT
    assert "never start unrequested work" in SYSTEM_PROMPT.lower()
    assert "exactly the first action" not in NEXT_MOVES_GUIDANCE
    assert "relevant to the question" in NEXT_MOVES_GUIDANCE
    assert "verified spend" not in messages[0]["content"]
    assert "approval button" not in CAPABILITIES
    assert "founder confirms with the attached button" not in TOOL_RULES
    assert "internal field names" in PERSONA_RULES
    assert "separate reviews" not in capability_block()


@pytest.mark.parametrize("idea,founder_request", [
    ("Security incident evidence for enterprise response teams", "File my trademark application"),
    ("A wearable glucose monitor for people managing diabetes", "File my tax return"),
])
def test_unavailable_legal_work_stays_refused_with_a_relevant_next_step(idea, founder_request):
    load_company_os_app()
    from app.chat.guard import guard_reply

    reply = guard_reply("", founder_request, own_name=idea, read_history=False)
    assert reply["kind"] == "refusal"
    assert "qualified legal or tax professional" in reply["body_md"]
    assert "Ask me to organize" in reply["body_md"]
    assert "registered agent" not in reply["body_md"]


@pytest.mark.parametrize("domain", ["warehouse teams", "home gardeners"])
def test_assistant_system_prompt_contract(domain):
    load_company_os_app()
    from app.chat.assistant import SYSTEM_PROMPT, DIAGRAM_PROMPT, STATUS_CARD_MARKERS, build_messages, diagram_messages

    assert "personal assistant" in SYSTEM_PROMPT.lower() or "buddy" in SYSTEM_PROMPT.lower()
    assert "never invent" in SYSTEM_PROMPT.lower()
    assert "bullet" in SYSTEM_PROMPT.lower()
    assert "1-4" in SYSTEM_PROMPT or "short paragraphs" in SYSTEM_PROMPT.lower()
    assert "em dash" in SYSTEM_PROMPT.lower() or "en dash" in SYSTEM_PROMPT.lower()
    assert "\u2013" not in SYSTEM_PROMPT and "\u2014" not in SYSTEM_PROMPT
    assert DIAGRAM_PROMPT not in SYSTEM_PROMPT
    assert "needs-you" in SYSTEM_PROMPT.lower() or "needs you" in SYSTEM_PROMPT.lower()

    brief = {
        "_rendered": f"Venture: {domain}\nBuilds: last state=failed failure=provider down credits_consumed=15.3594",
        "idea": {"name": domain},
    }
    history = [
        {"role": "user", "body_md": "Earlier we talked about waitlist"},
        {"role": "assistant", "body_md": "Waitlist is at zero."},
    ]
    messages = build_messages(
        question="Why did the last build fail?",
        brief=brief,
        history=history,
    )
    assert messages[0]["role"] == "system"
    from app.core.local_mode import is_local
    from app.core.local_capabilities import UNAVAILABLE, capability, enabled
    expected = SYSTEM_PROMPT
    if is_local() and enabled("background_work"):
        expected = expected.replace(
            "You are the manager of this founder's venture inside Supymem (the name in the chat header, default Charlie), an AI assistant and not a human.",
            "You are the venture's selected manager, an AI cofounder inside Supymem, not a human. Speak in the first person.")
    original_prompt = messages[0]["content"]
    if is_local():
        # 77be6a460 adds local availability and coding-tool constraints to the base prompt.
        assert original_prompt.startswith(expected + "\n")
        assert "write a brief for your connected coding tool" in original_prompt
        for name in UNAVAILABLE:
            entry = capability(name)
            if entry["state"] == "not_in_this_version":
                assert entry["next_step"] in original_prompt
    else:
        assert original_prompt == expected
    assert messages[1]["role"] == "user"
    user = messages[1]["content"]
    assert "provider down" in user
    assert "15.3594" in user
    assert "Earlier we talked about waitlist" in user
    assert "Why did the last build fail?" in user
    for marker in STATUS_CARD_MARKERS:
        assert marker not in SYSTEM_PROMPT
    assert diagram_messages(messages, question="Why did the last build fail?") is messages
    assert diagram_messages(messages, question=f"Explain how a process works for {domain}") is messages
    question = f"Draw the process for {domain}"
    final = diagram_messages(messages, question=question)
    assert final[0]["content"].endswith(DIAGRAM_PROMPT)
    assert diagram_messages(messages, question=question, actions=[{"confirmation": {"action": "approve"}}]) is messages
    assert messages[0]["content"] == original_prompt


def test_published_email_answers_use_outreach_geography_and_hide_planner_labels(venture, monkeypatch):
    from app.autopilot import outreach
    from app.chat import assistant
    from app.db.models import GtmContact
    from app.gtm import journey
    from app.state import db
    from core.clock import utcnow
    from core.ids import new_id

    client, _, _ = venture
    cases = []
    for name, audience, country, code, expected in [
        ("StockBeacon", "warehouse managers in Germany", "Germany", "DE", 4),
        ("TrailTunes", "college students in Canada", "Canada", "CA", 0),
    ]:
        created = client.post("/ventures", json={"name": name, "raw_idea": f"A planning tool for {audience}"}).json()
        vid, wid = created["id"], created["workspace_id"]
        with db()() as session:
            journey._store(session, journey._row(session, vid, create=True),
                           {"icp": {"who": audience, "location": country, "confirmed": True, "chat_confirmed": True,
                                    "problem": "Planning is fragmented", "problem_basis": "Founder supplied"}})
            for label, geo, published, suppressed in [
                ("local1", code, True, False), ("local2", code, True, False), ("local3", code, True, False),
                ("foreign", "AU", True, False), ("unknown", None, True, False),
                ("guessed", code, False, False), ("suppressed", code, True, True),
            ]:
                email = f"{label}@{name.lower()}.example"
                session.add(GtmContact(id=new_id("gtmc"), workspace_id=wid, email=email, first_name=label,
                    source=f"venture:{vid}:public", recipient_geo=geo, email_confidence="risky" if published else "guess",
                    do_not_contact=suppressed, enrichment_json=json.dumps({"email_evidence": "published" if published else None,
                    "found_at": f"https://{name.lower()}.example/contact", "business": label}),
                    stage_changed_at=utcnow(), created_at=utcnow()))
            session.commit()
        cases.append((vid, wid, name, expected))

    prompts = []
    async def complete(messages, **kwargs):
        prompt = messages[-1]["content"]
        leads = json.loads(re.search(r"Saved outreach leads[^\n]*:\n([^\n]+)", prompt)[1])
        prompts.append(leads)
        body = "Status: Here are published emails for you to review.\n\n" + "\n".join(
            f"- {lead['name']}: {lead['email']} ({lead['country'] or 'country unverified'})." for lead in leads)
        return body + "\nNeeds you: Check geography and permissions before sending.\n**NEXT:** Review your draft"

    monkeypatch.setattr(assistant, "_complete", complete)
    question = "Which of my leads have a published email I can send to?"
    for vid, wid, name, expected in cases:
        assert all("foreign@" not in contact.email for contact in outreach.contacts(vid))
        reply = asyncio.run(assistant.answer_question(vid, question, workspace_id=wid))
        leads = prompts[-1]
        assert all(lead["email"].endswith(f"@{name.lower()}.example") for lead in leads)
        assert all(not any(label in lead["email"] for label in ("foreign", "guessed", "suppressed")) for lead in leads)
        assert "NEXT:" not in reply["body_md"] and "Status:" not in reply["body_md"] and "Needs you:" not in reply["body_md"]
        assert any(action.get("payload", {}).get("text") == "Review your draft" for action in reply["actions"])
        assert len(re.findall(r"(?m)^- ", reply["body_md"])) == len(leads)
        assert len(leads) == expected
        if expected:
            unknown = next(lead for lead in leads if lead["name"] == "unknown")
            assert not unknown["eligible"] and unknown["blocked_reason"]
        else:
            assert leads == []
        assert assistant._finalize_body("Status: Saved. Needs you: Review it. NEXT: Review your draft", question) == "Saved. Review it."
        assert assistant._finalize_body("**Status:** **Saved**\n**Needs you:** Review it.\nNEXT: Open work", question) == "**Saved**\nReview it."
        assert assistant._finalize_body("Review https://example.test/Status:ready and `scope:NEXT:value`.", question) == "Review https://example.test/Status:ready and `scope:NEXT:value`."

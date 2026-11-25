"""First-minutes failures always leave the founder a working next step."""
from __future__ import annotations

import pytest

from tests.security.test_connector_oauth_boundary import client as client
from tests.security.test_durable_journey_boundary import request as _signed_request, venture, run_queued
from tests.idea_fixtures import completion, install_provider
from tests.security.test_idea_clarifier_boundary import job_row, receipt, submit

@pytest.fixture(autouse=True)
def _fresh_working_memory(request, monkeypatch):
    # COS_TEST_APP_REUSE rebinds app.state.MEM; modules that imported the old store would see it closed.
    if "client" not in request.fixturenames:
        return
    request.getfixturevalue("client")
    import app.state
    from app.ventures import state_brief
    monkeypatch.setattr(state_brief, "MEM", app.state.MEM)


DOMAINS = [
    pytest.param("A reconciliation desk for freight invoices", id="b2b"),
    pytest.param("A meal planner for new parents with no time to cook", id="consumer"),
]
SKIP = "Skip the remaining questions and confirm my idea"


def _invalid(*args):
    response = completion()
    response.choices[0].message.content = '{"action":"neither"}'
    return response


@pytest.mark.parametrize("idea", DOMAINS)
def test_skip_after_invalid_clarification_reaches_a_confirmable_review(client, monkeypatch, idea):
    install_provider(monkeypatch, _invalid)
    vid, _, sid = venture(client)
    jid = submit(client, f"/ventures/{vid}/idea/sessions/{sid}", idea)
    run_queued("idea.clarify_turn")
    assert job_row(jid).last_error == "clarification_invalid_output"
    reply = _signed_request(client, "POST", f"/ventures/{vid}/chat", json={"message": SKIP})
    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert "Something went wrong" not in body["body_md"]
    assert any(a.get("action") == "idea.confirm" or (a.get("confirmation") or {}).get("action") == "idea.confirm"
               for a in body["actions"])



@pytest.mark.parametrize("idea", DOMAINS)
def test_invalid_clarification_leaves_a_real_way_forward(client, monkeypatch, idea):
    install_provider(monkeypatch, _invalid)
    vid, _, sid = venture(client)
    jid = submit(client, f"/ventures/{vid}/idea/sessions/{sid}", idea)
    run_queued("idea.clarify_turn")
    assert job_row(jid).last_error == "clarification_invalid_output"
    from app.chat.execute import _review_idea
    reply = _review_idea(vid)
    assert reply["actions"], reply
    assert any(a.get("action") == "job.retry" for a in reply["actions"])
    assert any(a.get("text") == "Skip the questions" for a in reply["actions"])
    shown = _signed_request(client, "POST", f"/ventures/{vid}/chat/confirm", json={"action": "idea.review", "payload": {}}).json()
    assert {"job.retry", "chat.reply"} <= {a.get("action") for a in shown["actions"]}, shown["actions"]
    status = _signed_request(client, "POST", f"/ventures/{vid}/chat", json={"message": "what is my status"}).json()
    assert "reading it now" not in status["body_md"], status["body_md"]


@pytest.mark.parametrize("idea", DOMAINS)
def test_an_empty_model_reply_offers_more_than_the_same_retry(client, monkeypatch, idea):
    from types import SimpleNamespace
    from app.chat import assistant, llm_router
    vid, _, _ = venture(client)

    async def empty(*_args, **_kwargs):
        return ""

    async def route(_venture, text, *_args, **_kwargs):
        return SimpleNamespace(clarify=None, acts=[("general", text)])

    monkeypatch.setattr(assistant, "_answer_body", empty)
    monkeypatch.setattr(llm_router, "route", route)
    body = _signed_request(client, "POST", f"/ventures/{vid}/chat", json={"message": f"please think about {idea} with me"}).json()
    names = [a.get("action") for a in body["actions"]]
    assert "recovery.alternative" in names, (names, body["body_md"])


@pytest.mark.parametrize("idea", DOMAINS)
def test_a_client_without_an_idempotency_key_still_gets_a_model_admission(client, monkeypatch, idea):
    from app.api import chat_api
    seen = []

    async def handle(*_args, request_id=None, **_kwargs):
        seen.append(request_id)
        raise RuntimeError("stop after admission")

    monkeypatch.setattr(chat_api, "handle_message", handle)
    vid, _, _ = venture(client)
    assert _signed_request(client, "POST", f"/ventures/{vid}/chat", json={"message": idea}).status_code == 200
    assert seen and seen[0]


@pytest.mark.parametrize("purpose", ["venture.chat.assistant", "outreach.draft_batch"])
def test_metered_replies_reserve_and_request_reasoning_headroom(monkeypatch, purpose):
    import asyncio
    from types import SimpleNamespace
    from app.credits import metered
    from app.llm import founder_provider
    from llm_gateway import provider_foundry, routing
    config = SimpleNamespace(api_mode="responses", provider="azure")
    monkeypatch.setattr(founder_provider, "register_workspace_llm_resolver", lambda: None)
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda _w: SimpleNamespace(source="platform_default"))
    monkeypatch.setattr(provider_foundry, "load_provider_config_from_env", lambda: config)
    monkeypatch.setattr(provider_foundry, "resolve_live_model", lambda *_a, **_k: "gpt-6.1-sol")
    monkeypatch.setattr(routing, "resolve_purpose_config", lambda _p, c, **_k: c)
    captured = {}

    async def call(fn, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(metered, "call", call)
    monkeypatch.setattr(metered, "_transmit", lambda *_a, **_k: None)
    asyncio.run(metered.llm([{"role": "user", "content": "hello"}], purpose=purpose, workspace_id="wrk_x", max_tokens=150, tier="flash"))
    assert captured["max_tokens"] >= 2048


@pytest.mark.parametrize("idea", DOMAINS)
def test_first_idea_turn_falls_back_to_a_question_when_the_model_output_is_unusable(client, monkeypatch, idea):
    from app.idea.service import post_message
    install_provider(monkeypatch, _invalid)
    vid, _, sid = venture(client)
    from app.db.supymem_models import IdeaSession
    from app.state import db
    with db()() as session:
        session.get(IdeaSession, sid).questions_asked = 0
        session.commit()
    jid = post_message(sid, idea, initial_idea=True, actor_subject="usr_alice")["job_id"]
    run_queued("idea.clarify_turn")
    state = _signed_request(client, "GET", f"/ventures/{vid}/idea/sessions/{sid}").json()
    assert job_row(jid).status != "failed_dead"
    assert state["turns"][-1]["role"] == "agent" and state["turns"][-1]["kind"] == "question", state["turns"][-1]


@pytest.mark.parametrize("message, question_extra", [
    pytest.param("proposal-with-stale-question", {"options": ["A", "B"]}, id="b2b"),
    pytest.param("proposal-with-many-options", {"options": ["One", "Two", "Three", "Four", "Five", "Six"]}, id="consumer"),
])
def test_clarifier_shape_slips_with_an_obvious_meaning_are_usable(message, question_extra):
    import json
    from app.idea.agent import parse_result
    understanding = {"summary": "Shared rota for small teams", "offer": "A shared rota"}
    question = {"text": "Who runs the rota today?", "field": "customer", "why": "x" * 900, **question_extra}
    stale = json.dumps({"case": "know", "action": "proposal", "message": message, "understanding": understanding,
                        "question": question})
    assert parse_result(stale, force_propose=False).question is None
    missing = json.dumps({"case": "know", "action": "question", "message": message, "understanding": understanding, "question": None})
    assert parse_result(missing, force_propose=False).action == "proposal"
    overlong = json.dumps({"case": "know", "action": "question", "message": message, "understanding": understanding, "question": question})
    parsed = parse_result("```json\n" + overlong + "\n```", force_propose=False)
    assert parsed.question.text == question["text"] and len(parsed.question.options) <= 4 and len(parsed.question.why) <= 600
    too_many = json.dumps({"case": "explore", "action": "proposal", "message": message, "understanding": {**understanding,
        "proposed_options": [{"title": str(i), "problem": "p", "customer": "c", "offer": "o", "first_test": "t"} for i in range(5)]}, "question": None})
    assert len(parse_result(too_many, force_propose=False).understanding.proposed_options) == 3
    # An action outside the contract is never guessed at.
    with pytest.raises(ValueError):
        parse_result(json.dumps({"action": "execute_shell"}), force_propose=False)
    # A cut question could lose its meaning: it stays protected.
    long_question = json.dumps({"case": "know", "action": "question", "message": message, "understanding": understanding,
                                "question": {**question, "text": "y" * 1300, "why": ""}})
    with pytest.raises(ValueError):
        parse_result(long_question, force_propose=False)


@pytest.mark.parametrize("founder_text", [
    pytest.param("A dispatch board for regional couriers", id="b2b"),
    pytest.param("A chore chart for families with teenagers", id="consumer"),
])
def test_intake_gives_a_reasoning_model_real_headroom(monkeypatch, founder_text):
    import asyncio
    from app.intake import application, smart
    timeouts = []
    real_wait_for = asyncio.wait_for

    async def recording(awaitable, timeout=None):
        timeouts.append(timeout)
        return await real_wait_for(awaitable, timeout)

    async def model(*_args, **_kwargs):
        return None

    monkeypatch.setattr(asyncio, "wait_for", recording)
    monkeypatch.setattr(smart, "_call_model", model)
    asyncio.run(application.interpret(founder_text, actor_id="person_synthetic"))
    assert timeouts and min(timeouts) >= 15, timeouts


@pytest.mark.parametrize("phrase, skips", [
    ("Skip the questions", True),
    ("skip the remaining questions and confirm my idea", True),
    ("Skip all questions and review the idea.", True),
    ("Skip the questions and build it", False),
])
def test_asking_to_skip_and_confirm_is_a_skip(phrase, skips):
    from app.gstack.office_hours import skips_all_questions
    assert skips_all_questions(phrase) is skips

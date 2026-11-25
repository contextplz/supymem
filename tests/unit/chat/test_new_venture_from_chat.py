"""'Start a new venture' typed inside a venture's chat creates a NEW venture through the entry flow.

Regression (prod, 4 Oct): the model invented an archive rule, then a bare "Start new venture" was saved as an answer
to the current venture's clarification question.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from tests.company_os_harness import load_company_os_app, local_chat as local_chat

IDEA = "Start a new venture: an AI tool that turns a gym's member check-in data into weekly churn-risk alerts for small gym owners."


@pytest.fixture()
def first_venture(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/nv.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_MEMORY_PATH", str(tmp_path / "mem.sqlite3"))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    main = load_company_os_app()
    from app import state

    state.reset_db_factory()
    state.MEM.clear()
    with TestClient(main.create_app()) as client:
        from app.core.local_mode import is_local
        if is_local():
            from core.local_security import _proxy_secret
            client.headers["X-Local-Proxy-Secret"] = _proxy_secret().decode("ascii")
        made = client.post("/ventures", json={"raw_idea": "LabHire helps QA engineers find roles", "name": "MemQA"})
        assert made.status_code == 201, made.text
        yield made.json()["id"]


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def say(vid, text):
    from app.chat.router import handle_message

    return run(handle_message(vid, text))


def count_ventures():
    from sqlalchemy import func, select

    from app.db.models import Venture
    from app.state import db

    with db()() as s:
        return s.scalar(select(func.count()).select_from(Venture))


@pytest.mark.parametrize("idea", ["Track warehouse equipment repairs", "Plan family camping trips"])
@pytest.mark.parametrize("header", ["x-local-proxy-secret", "X-Local-Proxy-Secret"])
def test_local_chat_fixture_keeps_invalid_owner_credentials_rejected(first_venture, idea, header):
    from app.core.local_mode import is_local
    if not is_local():
        pytest.skip("local owner boundary")
    main = load_company_os_app()
    before = count_ventures()
    with TestClient(main.create_app()) as client:
        response = client.post("/ventures", json={"raw_idea": idea},
            headers={header: "invalid-test-owner-credential"})
    assert response.status_code == 403
    assert count_ventures() == before


@pytest.mark.parametrize(("text", "idea"), [
    (IDEA, "an AI tool that turns a gym's member check-in data into weekly churn-risk alerts for small gym owners."),
    ("start a new venture", ""), ("Start new venture", ""), ("create another venture for a dog marketplace", "a dog marketplace"),
    ("i want to start a new startup: meal prep for nurses", "meal prep for nurses"),
])
def test_new_venture_commands_are_recognised(text, idea):
    load_company_os_app()
    from app.chat.intent import classify_intent
    from app.chat.new_venture import idea_of

    assert idea_of(text) == idea and classify_intent(text) == "new_venture"


@pytest.mark.parametrize("text", ["how do I start a new venture?", "what is a new venture", "start a new task", "open the new venture page"])
def test_questions_and_other_starts_are_not_new_venture(text):
    load_company_os_app()
    from app.chat.new_venture import idea_of

    assert idea_of(text) is None


@pytest.mark.parametrize("idea", ["Warehouse inspection scheduling", "Family holiday planning"])
def test_context_history_keeps_initial_records_and_excludes_only_new_venture_turns(idea):
    load_company_os_app()
    from app.chat.new_venture import context_history

    initial = {"role": "assistant", "kind": "text", "body_md": idea}
    mail = {"role": "assistant", "kind": "inbox_briefing", "body_md": idea}
    requested = {"id": "separate-request", "role": "user", "kind": "text", "body_md": "Start a new venture: " + idea}
    separate = {"role": "assistant", "kind": "text", "body_md": idea, "reply_to_message_id": requested["id"]}
    resumed = {"id": "current-request", "role": "user", "kind": "text", "body_md": "Explain my saved tests."}
    assert context_history([initial, mail, requested, separate, resumed, initial]) == [initial, mail, resumed, initial]
    assert context_history([initial, mail]) == [initial, mail]
    assert context_history([separate]) == []
    assert context_history([separate, resumed, initial]) == [resumed, initial]
    assert context_history([requested, resumed, separate, initial]) == [resumed, initial]
    assert context_history([{**separate, "kind": "new_venture", "reply_to_message_id": None}]) == []
    current_reply = {**initial, "reply_to_message_id": resumed["id"]}
    assert context_history([resumed, current_reply]) == [resumed, current_reply]
    checkpoint_reply = {key: value for key, value in separate.items() if key != "reply_to_message_id"}
    checkpoint_reply["citations_json"] = json.dumps([{"reply_to_message_id": requested["id"]}])
    assert context_history([requested, resumed, checkpoint_reply]) == [resumed]


@pytest.mark.parametrize("idea", [
    "An invoice platform for manufacturers. Finance teams reconcile disputed bills and pay per facility.",
    "An accessible holiday planner for families. Parents select trips and pay per itinerary.",
])
def test_complete_new_venture_command_cannot_be_replaced_by_model(first_venture, monkeypatch, idea):
    from app.chat import router, llm_router
    from types import SimpleNamespace
    selected = []

    async def reroute(*args, **kwargs):
        return SimpleNamespace(clarify=None, acts=[("assistant", idea)])

    async def prepare(venture_id, context, intent, *args, **kwargs):
        selected.append(intent)
        return {"kind": "text", "body_md": "Open your new conversation.", "actions": [], "citations": []}

    monkeypatch.setattr(llm_router, "route", reroute)
    monkeypatch.setattr("app.chat.idea_bridge.written_request", lambda *_: True)
    monkeypatch.setattr(router, "_prepare_intent", prepare)
    say(first_venture, "Start a new venture: " + idea)
    assert selected == ["new_venture"]


def test_requested_idea_creates_a_separate_venture_and_leaves_this_one_alone(first_venture):
    before = count_ventures()
    reply = say(first_venture, IDEA)
    assert not any(action.get("confirmation") for action in reply["actions"])
    href = reply["actions"][0]["href"]
    assert href.startswith("/v/") and href != f"/v/{first_venture}" and count_ventures() == before + 1


def test_bare_start_new_venture_asks_for_the_idea_and_is_never_a_clarification_answer(first_venture):
    before = count_ventures()
    reply = say(first_venture, "Start new venture")
    assert "What is the new venture" in reply["body_md"] and not reply["actions"] and count_ventures() == before
    assert reply["kind"] != "idea_clarification_pending"


def test_venture_limit_is_reported_with_the_real_message(first_venture, monkeypatch):
    from app.ventures import quota

    def full(_owner):
        raise quota.VentureLimitReached()

    monkeypatch.setattr(quota, "assert_can_create_venture", full)
    reply = say(first_venture, IDEA)
    assert quota.LIMIT_MESSAGE in reply["body_md"] and not reply["actions"]


@pytest.mark.parametrize("claim", [
    "Only one venture can be active at a time. MemQA will be archived and Beta QA Churn will become the active workspace.",
    "Your old venture gets archived.",
])
def test_claims_guard_removes_invented_archive_and_active_limit_rules(claim):
    load_company_os_app()
    from app.chat.context import ChatContext
    from app.chat.router import scrub_hallucinations

    out = scrub_hallucinations(f"Sure. {claim} Say go.", ChatContext(venture={"id": "v"}))
    assert "archived" not in out.replace("never archives", "") and "active workspace" not in out and "only one" not in out.lower()
    assert "never archives or replaces" in out and "Sure." in out


@pytest.mark.parametrize("vague", ["gym thing churn", "app for my mom's bakery orders??", "idk something with AI for lawyers in india"])
def test_vague_idea_after_bare_command_creates_only_after_enough_detail(first_venture, vague):
    before = count_ventures()
    asked = say(first_venture, "Start new venture")
    assert "What is the new venture" in asked["body_md"] and "Just reply here" in asked["body_md"]
    reply = say(first_venture, vague)
    if count_ventures() == before:
        assert reply["body_md"].strip() and "can't help" not in reply["body_md"].lower()
        reply = say(first_venture, "small business owners in India")
    assert not any(action.get("confirmation") for action in reply["actions"])
    assert count_ventures() == before + 1 and reply["actions"][0]["href"] != f"/v/{first_venture}"

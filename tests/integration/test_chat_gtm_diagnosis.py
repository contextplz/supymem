"""The confirmed GTM chat reuses one saved quote and one metered run."""

from __future__ import annotations

import pytest

pytest_plugins = ("tests.integration.test_chat_first_idea_bridge",)


@pytest.mark.parametrize("prompt", [
    "I want GTM. Help me get users.",
    "Give StudioSlot a go-to-market diagnosis and the smallest next acquisition experiment. "
    "This is a concept with no product, interviews, users or validated demand. "
    "Focus on finding three independent trainers for brief non-sales interviews. "
    "Use anonymized verbal workflow descriptions, not client chats or contact lists. Nothing has been sent.",
])
def test_existing_venture_get_users_uses_saved_quote_without_starting_work(client, monkeypatch, prompt):
    from app.gtm.god import generation
    from app.db.models import Job, PlannedTask
    from app.state import db
    from sqlalchemy import func, select

    preview = client.post("/entry/chat/preview", json={
        "text": "I want to build a startup for bakery owners to manage orders",
    }).json()["proposal"]
    created = client.post("/entry/chat/confirm", json={"proposal_id": preview["id"]})
    assert created.status_code == 200, created.text
    vid = created.json()["venture_id"]
    wrong_route = client.post(f"/ventures/{vid}/chat/gtm/route/confirm", json={
        "proposal_id": preview["id"],
    }, headers={"X-Subject": "founder"})
    assert wrong_route.status_code == 404
    monkeypatch.setattr(generation, "recent", lambda *_args: {"runs": []})
    monkeypatch.setattr(generation, "quote", lambda *_args: {
        "input_digest": "a" * 64, "resource_digest": "b" * 64,
        "ceiling_micro": 25000, "available_micro": 100000,
        "provider": "test", "model": "test-model", "payer": "platform_credits",
        "call_budget": 1, "sufficient_now": True,
    })
    starts = []
    def start(*args, **kwargs):
        starts.append((args, kwargs))
        return {"run_id": "ggr_existing_chat", "state": "queued", "ceiling_micro": 25000}
    monkeypatch.setattr(generation, "start", start)

    response = client.post(f"/ventures/{vid}/chat", json={
        "message": prompt, "idempotency_key": "existing-gtm-1",
    }, headers={"X-Subject": "founder"})
    assert response.status_code == 200, response.text
    card = response.json()
    assert card["kind"] == "gtm_skip_proposal"
    assert card["actions"][0]["confirmation"]["action"] == "gtm.route.confirm"
    # #571: a venture the founder already has is continued by name; the card still
    # discloses the skipped steps in its consequences.
    assert "as your existing venture" in card["body_md"] and "go-to-market review" in card["body_md"]
    assert any("Skip idea validation" in c for c in card["actions"][0]["confirmation"]["consequences"])
    proposal_id = card["actions"][0]["confirmation"]["resulting_state"]["proposal_id"]
    blocked = client.post(f"/ventures/{vid}/chat/gtm/quote", json={
        "request_id": "before-approval", "description": "Find bakery owners",
        "idea": "Bakery app",
    }, headers={"X-Subject": "founder"})
    assert blocked.status_code == 403
    assert blocked.json()["detail"]["error"]["code"] == "gtm_route_required"
    repeated = client.post(f"/ventures/{vid}/chat", json={
        "message": prompt, "idempotency_key": "existing-gtm-1",
    }, headers={"X-Subject": "founder"})
    assert repeated.json()["id"] == card["id"]
    other_actor = client.post(f"/ventures/{vid}/chat/gtm/route/confirm", json={
        "proposal_id": proposal_id,
    }, headers={"X-Subject": "another-founder"})
    assert other_actor.status_code == 404
    approved = client.post(f"/ventures/{vid}/chat/gtm/route/confirm", json={
        "proposal_id": proposal_id,
    }, headers={"X-Subject": "founder"})
    assert approved.status_code == 200, approved.text
    assert approved.json()["kind"] == "gtm_route_confirmed"
    assert approved.json()["actions"]
    assert not any(word in approved.json()["body_md"].lower() for word in (
        "diagnosis quote", "credit ceiling", "model call", "what does the product do", "who are you trying to reach", "yc says",
    ))
    assert client.post(f"/ventures/{vid}/chat/gtm/route/confirm", json={
        "proposal_id": proposal_id,
    }, headers={"X-Subject": "founder"}).json()["id"] == approved.json()["id"]
    assert not starts
    response = client.post(f"/ventures/{vid}/chat", json={
        "message": prompt, "idempotency_key": "existing-gtm-quote",
    }, headers={"X-Subject": "founder"})
    assert response.status_code == 200, response.text
    card = response.json()
    assert card["kind"] == "gtm_diagnosis_pending"
    assert card["confirmed_action"]["action"] == "gtm.diagnosis.start"
    assert "credit" not in card["body_md"].lower()
    quoted = next(row for row in client.get(f"/ventures/{vid}/chat").json()["items"]
                  if row["kind"] == "gtm_diagnosis_quote")
    # The exact current instruction, including no-send/discovery constraints,
    # belongs to the immutable metered input, not only the displayed card.
    assert quoted["actions"][0]["gtm_quote"]["input"]["problem"] == prompt
    quoted_idea = quoted["actions"][0]["gtm_quote"]["input"]["idea"]
    assert quoted_idea.startswith("Latest founder statement")
    assert "I want to build a startup for bakery owners to manage orders" in quoted_idea
    assert "Nothing is researched, published, or sent" in quoted["body_md"]
    assert len(starts) == 1 and starts[0][0][2] == quoted["actions"][0]["gtm_quote"]["input"]
    repeated = client.post(f"/ventures/{vid}/chat", json={
        "message": prompt, "idempotency_key": "existing-gtm-quote",
    }, headers={"X-Subject": "founder"})
    assert repeated.json()["id"] == card["id"]
    with db()() as session:
        assert session.scalar(select(func.count()).select_from(PlannedTask).where(PlannedTask.venture_id == vid)) == 0
        assert session.scalar(select(func.count()).select_from(Job).where(Job.venture_id == vid,
                             Job.kind == "gtm.god.run")) == 0


def test_chat_quote_confirmation_and_result_are_durable(client, monkeypatch):
    from app.api import chat_api
    from app.db.models import AnalyticsEvent, Job, PlannedTask
    from app.gtm.god import generation
    from app.state import db
    from sqlalchemy import func, select

    preview = client.post("/entry/chat/preview", json={
        "text": "I have an existing app for bakeries and want to find customers",
    })
    assert preview.status_code == 200, preview.text
    committed = client.post("/entry/chat/confirm", json={
        "proposal_id": preview.json()["proposal"]["id"],
    })
    assert committed.status_code == 200, committed.text
    vid, wid = committed.json()["venture_id"], committed.json()["workspace_id"]
    monkeypatch.setattr(chat_api, "_gtm_owner", lambda venture_id, request, x_subject: wid)
    calls = []
    quote = {"input_digest": "a" * 64, "resource_digest": "b" * 64,
             "ceiling_micro": 25000, "available_micro": 100000,
             "provider": "test", "model": "test-model", "payer": "platform_credits",
             "call_budget": 1, "sufficient_now": True}
    monkeypatch.setattr(generation, "recent", lambda *args: {"runs": []})
    monkeypatch.setattr(generation, "quote", lambda *args: quote)

    def start(*args, **kwargs):
        calls.append((args, kwargs))
        return {"run_id": "ggr_chat", "state": "queued", "ceiling_micro": 25000}

    monkeypatch.setattr(generation, "start", start)
    description = "The app manages bakery orders for owners who still use paper."
    quote_request = {
        "request_id": "quote-request-1",
        "description": description, "idea": "Bakery order app", "problem": description,
        "who": "Bakery owners", "users_count": 0,
    }
    response = client.post(f"/ventures/{vid}/chat/gtm/quote", json=quote_request)
    assert response.status_code == 200, response.text
    card = response.json()
    retry = client.post(f"/ventures/{vid}/chat/gtm/quote", json=quote_request)
    assert retry.json()["id"] == card["id"]
    assert card["kind"] == "gtm_diagnosis_pending"
    assert card["confirmed_action"]["action"] == "gtm.diagnosis.start"
    assert "credit" not in card["body_md"].lower()
    assert len(calls) == 2
    quoted = next(row for row in client.get(f"/ventures/{vid}/chat").json()["items"]
                  if row["kind"] == "gtm_diagnosis_quote")
    confirmed = client.post(f"/ventures/{vid}/chat/gtm/confirm", json={
        "quote_message_id": quoted["id"],
    })
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["kind"] == "gtm_diagnosis_pending"
    repeated = client.post(f"/ventures/{vid}/chat/gtm/confirm", json={
        "quote_message_id": quoted["id"],
    })
    assert repeated.json()["id"] == confirmed.json()["id"]
    assert calls[0][1] == calls[1][1]
    assert calls[0][1]["idempotency_key"] == f"chat-gtm:{quoted['id']}"
    assert calls[0][1]["ceiling_micro"] == quote["ceiling_micro"]
    history = client.get(f"/ventures/{vid}/chat").json()["items"]
    assert any(row["role"] == "user" and row["body_md"] == description for row in history)
    with db()() as session:
        assert session.scalar(select(func.count()).select_from(PlannedTask).where(PlannedTask.venture_id == vid)) == 0
        assert session.scalar(select(func.count()).select_from(Job).where(Job.venture_id == vid, Job.kind == "gtm.god.run")) == 0
        assert session.scalar(select(func.count()).select_from(AnalyticsEvent).where(
            AnalyticsEvent.venture_id == vid, AnalyticsEvent.event_name == "chat.message")) == 1
        assert session.scalar(select(func.count()).select_from(AnalyticsEvent).where(
            AnalyticsEvent.venture_id == vid, AnalyticsEvent.event_name == "feature.used")) == 1


def test_chat_confirmation_rejects_unstored_quote(client, monkeypatch):
    from app.api import chat_api
    from app.gtm.god import generation

    preview = client.post("/entry/chat/preview", json={
        "text": "I have an existing app for bakeries and want to find customers",
    })
    committed = client.post("/entry/chat/confirm", json={"proposal_id": preview.json()["proposal"]["id"]})
    vid, wid = committed.json()["venture_id"], committed.json()["workspace_id"]
    monkeypatch.setattr(chat_api, "_gtm_owner", lambda *args: wid)
    monkeypatch.setattr(generation, "start", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not start")))
    response = client.post(f"/ventures/{vid}/chat/gtm/confirm", json={"quote_message_id": "msg_unknown"})
    assert response.status_code == 404
    assert response.json()["detail"]["error"]["code"] == "gtm_quote_not_found"


def test_followup_questions_and_work_requests_do_not_make_quotes_or_tasks(client, monkeypatch):
    from app.api import chat_api
    from app.chat import assistant
    from app.db.models import PlannedTask
    from app.gtm.god import generation
    from app.state import db
    from sqlalchemy import func, select

    preview = client.post("/entry/chat/preview", json={
        "text": "I have an existing app for bakeries and want to find customers",
    })
    committed = client.post("/entry/chat/confirm", json={"proposal_id": preview.json()["proposal"]["id"]})
    vid, wid = committed.json()["venture_id"], committed.json()["workspace_id"]
    monkeypatch.setattr(chat_api, "_gtm_owner", lambda *args: wid)
    monkeypatch.setattr(generation, "recent", lambda *args: {"runs": []})
    calls = []
    monkeypatch.setattr(generation, "quote", lambda *args: calls.append(1) or {
        "input_digest": "a" * 64, "resource_digest": "b" * 64,
        "ceiling_micro": 25000, "available_micro": 100000,
        "provider": "test", "model": "test-model", "payer": "platform_credits",
        "call_budget": 1, "sufficient_now": True,
    })
    monkeypatch.setattr(generation, "start", lambda *args, **kwargs: {
        "run_id": "ggr_followup_chat", "state": "queued", "ceiling_micro": 25000})
    async def answer(*args, **kwargs):
        return {"body_md": "Ask three owners about their current workflow.", "actions": []}
    monkeypatch.setattr(assistant, "answer_question", answer)
    quote_request = {"request_id": "quote-request-1", "description": "Bakeries need customers",
                     "idea": "Bakery app", "problem": "Bakeries need customers"}
    first = client.post(f"/ventures/{vid}/chat/gtm/quote", json=quote_request)
    assert first.status_code == 200
    duplicate = client.post(f"/ventures/{vid}/chat/gtm/quote", json={**quote_request, "request_id": "quote-request-2"})
    assert duplicate.status_code == 409
    assert len(calls) == 1
    status = client.post(f"/ventures/{vid}/chat/gtm/followup", json={"message": "What is the quote status?"})
    assert status.status_code == 200
    assert first.json()["body_md"] in status.json()["body_md"]
    question = client.post(f"/ventures/{vid}/chat/gtm/followup", json={"message": "How do I find early users?"})
    assert question.status_code == 200
    assert "Ask three owners" in question.json()["body_md"]
    work = client.post(f"/ventures/{vid}/chat/gtm/followup", json={"message": "Draft and send outreach now"})
    assert work.status_code == 200
    assert "did not start work" in work.json()["body_md"]
    with db()() as session:
        assert session.scalar(select(func.count()).select_from(PlannedTask).where(PlannedTask.venture_id == vid)) == 0
    assert len(calls) == 1


def test_completed_diagnosis_is_saved_once_in_chat(client, monkeypatch):
    from app.api import chat_api
    from app.gtm.god import generation

    preview = client.post("/entry/chat/preview", json={
        "text": "I have an existing app for bakeries and want to find customers",
    })
    committed = client.post("/entry/chat/confirm", json={"proposal_id": preview.json()["proposal"]["id"]})
    vid, wid = committed.json()["venture_id"], committed.json()["workspace_id"]
    monkeypatch.setattr(chat_api, "_gtm_owner", lambda *args: wid)
    monkeypatch.setattr(generation, "recent", lambda *args: {"runs": [{
        "run_id": "ggr_done", "state": "completed", "reserved_micro": 0,
        "consumed_micro": 1000, "error_code": None,
        "result": {"diagnosis": {"bottleneck": "Unverified bakery demand",
            "rationale": "No interviews were reported", "assumptions": ["Owners need this"],
            "evidence_gaps": ["No interviews"], "next_actions": [{
                "instruction": "Interview three owners", "reason": "Test demand"}]}}
    }]})
    first = client.get(f"/ventures/{vid}/chat/gtm/status")
    second = client.get(f"/ventures/{vid}/chat/gtm/status")
    assert first.status_code == second.status_code == 200
    assert first.json()["message"]["id"] == second.json()["message"]["id"]
    body = first.json()["message"]["body_md"]
    assert "Unverified bakery demand" in body and "Interview three owners" in body
    assert "No outreach, research, or publication was started" in body


def test_gtm_chat_requires_an_authenticated_subject(client):
    from app.api.chat_api import _gtm_owner
    from fastapi import HTTPException
    from starlette.requests import Request
    import pytest

    request = Request({"type": "http", "method": "POST", "path": "/ventures/x/chat/gtm/quote",
                       "headers": []})
    with pytest.raises(HTTPException) as rejected:
        _gtm_owner("x", request, None)
    assert rejected.value.status_code == 403


def test_gtm_chat_requires_confirmed_route_for_same_actor(client, monkeypatch):
    from app.api.chat_api import _gtm_owner
    from app.db.supymem_models import EntryChatProposal
    from app.state import db
    from fastapi import HTTPException
    from sqlalchemy import select
    from starlette.requests import Request
    from types import SimpleNamespace
    import app.admin.authn as authn
    import pytest

    preview = client.post("/entry/chat/preview", json={
        "text": "I have an existing app for bakeries and want to find customers",
    })
    committed = client.post("/entry/chat/confirm", json={"proposal_id": preview.json()["proposal"]["id"]})
    vid, wid = committed.json()["venture_id"], committed.json()["workspace_id"]
    with db()() as session:
        proposal = session.scalar(select(EntryChatProposal).where(EntryChatProposal.target_venture_id == vid))
        actor = proposal.actor_id
    request = Request({"type": "http", "method": "POST", "path": f"/ventures/{vid}/chat/gtm/quote",
                       "headers": [(b"x-subject", actor.encode())]})
    monkeypatch.setattr(authn, "verify_caller", lambda request: SimpleNamespace(sub=actor))
    assert _gtm_owner(vid, request, actor) == wid
    # The gateway's local subject maps to the persisted Person id at entry.
    monkeypatch.setattr(authn, "verify_caller", lambda request: SimpleNamespace(sub="founder"))
    assert _gtm_owner(vid, request, "founder") == wid
    monkeypatch.setattr(authn, "verify_caller", lambda request: SimpleNamespace(sub="another-actor"))
    with pytest.raises(HTTPException):
        _gtm_owner(vid, request, actor)

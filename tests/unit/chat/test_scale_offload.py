import asyncio
from contextvars import ContextVar
from types import SimpleNamespace
import threading

from fastapi import HTTPException
import pytest
from starlette.requests import Request

from tests.company_os_harness import load_company_os_app


@pytest.mark.parametrize("domain", ["Supplier invoicing", "Personal fitness"])
@pytest.mark.parametrize("fail", [False, True])
@pytest.mark.parametrize("route", ["post", "confirm"])
def test_chat_db_units_offload_with_context_and_uncertain_outcome(monkeypatch, domain, fail, route):
    load_company_os_app()
    from app.api import chat_api
    from app.chat import requests
    from app.intake import evidence

    loop_thread = threading.get_ident()
    marker = ContextVar("offload_actor", default=None)
    calls = []

    def record(name):
        assert threading.get_ident() != loop_thread
        assert marker.get() == domain
        calls.append(name)

    class Session:
        def __enter__(self):
            record("open")
            return self

        def __exit__(self, *args):
            record("close")

        def get(self, *args):
            record("read")
            return SimpleNamespace(workspace_id="workspace")

    def db():
        record("factory")
        return Session

    monkeypatch.setattr(chat_api, "db", db)
    monkeypatch.setattr(chat_api, "assert_venture_access", lambda *args: record("authorize"))
    from app.admin import authn
    monkeypatch.setattr(authn, "verify_caller", lambda request: SimpleNamespace(sub="actor"))
    monkeypatch.setattr(evidence, "save_statement", lambda *args, **kwargs: record("statement"))

    def admit(*args):
        record("admit")
        return SimpleNamespace(id="request"), None

    def complete(admission, reply):
        record("complete")
        return reply

    monkeypatch.setattr(requests, "admit", admit)
    monkeypatch.setattr(requests, "complete", complete)
    def uncertain(admission, response=None):
        assert response is None
        record("uncertain")

    monkeypatch.setattr(requests, "uncertain", uncertain)
    reply = {"id": "reply", "role": "assistant", "body_md": domain}

    async def respond(*args, **kwargs):
        assert threading.get_ident() == loop_thread
        if fail:
            raise ValueError("stub reply failed")
        return reply

    monkeypatch.setattr(chat_api, "_generic_reply", respond)
    if route == "confirm":
        from app.org import manager

        monkeypatch.setattr(chat_api, "execute_action", respond)
        monkeypatch.setattr(chat_api, "_confirm_failed", lambda *args: record("confirm_failed"))
        monkeypatch.setattr(manager, "manager_speaker", lambda *args: {"id": "manager", "name": domain, "title": "Manager"})

        def save(*args, **kwargs):
            record("write")
            return reply

        monkeypatch.setattr(chat_api, "append_message", save)
    request = Request({"type": "http", "method": "POST", "path": "/chat", "headers": []})

    async def run():
        marker.set(domain)
        if route == "confirm":
            operation = chat_api.confirm_chat_action("venture", chat_api.ConfirmBody(action="stub.action", idempotency_key="nonce"), request, "actor")
        else:
            operation = chat_api.post_chat("venture", chat_api.ChatBody(message=domain, idempotency_key="nonce"), request, "actor")
        if fail:
            with pytest.raises(HTTPException) as error:
                await operation
            assert error.value.status_code == (400 if route == "confirm" else 409)
            assert error.value.detail["error"]["code"] == ("unknown_action" if route == "confirm" else "chat_request_outcome_unknown")
        else:
            result = await operation
            assert result["id"] == "reply"

    asyncio.run(run())
    assert calls[:4] == ["factory", "open", "read", "close"]
    assert calls[-1] == ("uncertain" if fail or route == "confirm" else "complete")


@pytest.mark.parametrize("domain", ["Supplier invoicing", "Personal fitness"])
@pytest.mark.parametrize("card", ["reply", "proposal", "replay"])
@pytest.mark.parametrize("waiting", [False, True])
def test_gtm_cards_keep_clarification_and_voice_context_off_loop(monkeypatch, domain, card, waiting):
    load_company_os_app()
    from tests.company_os_harness import configure_local_test_model

    configure_local_test_model()
    from app.api import chat_api
    from app.analytics import events
    from app.chat import customers, idea_bridge, intent, router
    from app.entry import chat_start
    from app.intake import evidence

    loop_thread = threading.get_ident()
    marker = ContextVar("gtm_offload_actor", default=None)
    calls = []

    def record(name):
        assert threading.get_ident() != loop_thread
        assert marker.get() == domain
        calls.append(name)

    def unit(name, result=None):
        def call(*args, **kwargs):
            record(name)
            return result
        return call

    from app.admin import authn
    monkeypatch.setattr(authn, "verify_caller", lambda request: SimpleNamespace(sub="actor"))
    monkeypatch.setattr(chat_api, "_chat_venture", unit("venture", SimpleNamespace(workspace_id="workspace")))
    monkeypatch.setattr(chat_api, "assert_venture_access", unit("authorize"))
    monkeypatch.setattr(evidence, "save_statement", unit("statement"))
    monkeypatch.setattr(events, "emit", unit("voice"))
    monkeypatch.setattr(intent, "classify_intent", lambda text: "gtm_review")
    monkeypatch.setattr(intent, "find_customers_request", lambda text: True)
    monkeypatch.setattr(intent, "requested_customer_search", lambda text: True)
    monkeypatch.setattr(idea_bridge, "pending_question", unit("pending", ("idea", "question") if waiting else None))
    monkeypatch.setattr(customers, "_view", unit("customers", {}))

    def owner(*args):
        record("owner")
        raise HTTPException(403, detail={"error": {"code": "gtm_route_required"}})

    monkeypatch.setattr(chat_api, "_gtm_owner", owner)
    actions = [{"label": "Review search", "action": "gtm.find"}]
    preview = {"status": "reply", "body_md": domain, "actions": actions} if card == "reply" else {
        "proposal": {"id": "proposal", "route": "gtm", "message": domain}}
    monkeypatch.setattr(chat_start, "preview", unit("preview", preview))
    existing = {"id": "reply", "role": "assistant", "body_md": domain, "actions": actions}
    monkeypatch.setattr(chat_api, "idempotent_message", unit("replay", existing))

    async def confirm(*args):
        assert threading.get_ident() == loop_thread
        assert marker.get() == domain
        calls.append("confirm")

    monkeypatch.setattr(chat_start, "confirm", confirm)

    def save(venture_id, **kwargs):
        record(kwargs["role"])
        return {"id": "reply", **kwargs}

    def clarify(venture_id, reply):
        record("clarify")
        return {**reply, "actions": [*reply["actions"], {"action": "idea.review"}]}

    monkeypatch.setattr(chat_api, "append_message", save)
    monkeypatch.setattr(router, "_continue_clarifying", clarify)
    async def started(_venture, reply, _actor, *, actions):
        return reply
    monkeypatch.setattr("app.chat.confirmations.start_requested_work", started)
    request = Request({"type": "http", "method": "POST", "path": "/chat", "headers": []})

    async def run():
        marker.set(domain)
        return await chat_api.post_chat("venture", chat_api.ChatBody(message=domain, source="voice",
            idempotency_key="nonce"), request, "actor")

    reply = asyncio.run(run())
    assert reply["id"] == "reply" and reply["body_md"] == domain
    assert "voice" in calls and "pending" in calls
    assert calls.count("clarify") == int(waiting and card == "reply")
    assert calls.count("user") == calls.count("assistant") == int(card == "reply")
    assert calls.count("confirm") == int(card != "reply")
    assert any(action.get("action") == "idea.review" for action in reply["actions"]) == (waiting and card == "reply")
    assert reply["actions"][:1] == actions


@pytest.mark.parametrize("domain", ["Supplier invoicing", "Personal fitness"])
@pytest.mark.parametrize("outcome", ["started", "not_started", "revoked", "manual"])
def test_standing_confirmation_db_units_offload_with_context(monkeypatch, domain, outcome):
    load_company_os_app()
    from app.api import chat_api
    from app.approvals.service import ApprovalError
    from app.chat import confirmations, requests
    from app.org import manager

    loop_thread = threading.get_ident()
    marker = ContextVar("standing_confirmation_actor", default=None)
    calls = []
    state = {"scope": domain}
    card = {"id": "card", "role": "assistant", "actions": [{"confirmation": {
        "action": "research.start", "resulting_state": state}}]}

    def unit(name, result=None):
        def call(*args, **kwargs):
            assert threading.get_ident() != loop_thread
            assert marker.get() == domain
            calls.append(name)
            return result
        return call

    async def execute(*args, **kwargs):
        assert threading.get_ident() == loop_thread
        assert kwargs.get("standing_grant_id") == (None if outcome == "manual" else "grant")
        if outcome == "revoked":
            kwargs["payload"].clear()
            raise ApprovalError("always_ask", "You need to approve this action each time.")
        return {"action_started": outcome != "not_started", "body_md": domain,
                "run_id": "job", "actions": []}

    def read_card(venture_id, message_id, action, payload, *, actor_subject=None):
        unit("card")()
        assert payload == {"scope": domain}
        assert actor_subject == "actor"
        return card

    def save(venture_id, **kwargs):
        unit("write")()
        return {"id": "reply", **kwargs}

    async def dispatch(venture_id, reply, actor_subject):
        assert threading.get_ident() == loop_thread
        calls.append("dispatch")
        return reply

    monkeypatch.setattr(chat_api, "execute_action", execute)
    monkeypatch.setattr(chat_api, "append_message", save)
    monkeypatch.setattr(manager, "manager_speaker", unit("speaker", {"id": "manager", "name": domain, "title": "Manager"}))
    monkeypatch.setattr(confirmations, "mark", unit("mark"))
    monkeypatch.setattr(confirmations, "card", read_card)
    monkeypatch.setattr(confirmations, "auto_dispatch", dispatch)
    monkeypatch.setattr(requests, "complete", lambda admission, reply: unit("complete", reply)())

    async def run():
        marker.set(domain)
        return await chat_api._execute_confirmed_action("venture", chat_api.ConfirmBody(
            action="research.start", payload=state, message_id="card"), "workspace", "actor",
            "nonce", SimpleNamespace(id="request"), standing_grant_id=None if outcome == "manual" else "grant")

    reply = asyncio.run(run())
    if outcome == "revoked":
        assert reply is card
        assert calls == ["mark", "card", "complete"]
    else:
        assert calls == ["speaker", "write", "mark", *(["dispatch"] if outcome == "manual" else []), "complete"]
        assert [action["action"] for action in reply["actions"]] == (
            ["chat.stop", "approvals.revoke"] if outcome == "started" else [])
        assert any(citation.get("standing_approval_id") == "grant" for citation in reply["citations"]) == (outcome == "started")


@pytest.mark.parametrize("domain", ["Supplier invoicing", "Personal fitness"])
@pytest.mark.parametrize("granted", [False, True])
def test_standing_auto_dispatch_db_units_offload_with_context(monkeypatch, domain, granted):
    load_company_os_app()
    from app.api import chat_api
    from app.approvals import standing
    from app.chat import confirmations, conversations
    from app import tenancy

    loop_thread = threading.get_ident()
    marker = ContextVar("standing_dispatch_actor", default=None)
    calls = []
    state = {"scope": domain}
    message = {"id": "card", "actions": [{"confirmation": {
        "action": "research.start", "resulting_state": state}}]}
    reply = {"id": "reply"}

    def unit(name, result):
        def call(*args, **kwargs):
            assert threading.get_ident() != loop_thread
            assert marker.get() == domain
            calls.append(name)
            return result
        return call

    async def confirm(venture_id, payload, workspace_id, actor_subject, *, standing_grant_id):
        assert threading.get_ident() == loop_thread
        assert (venture_id, payload.message_id, payload.payload, workspace_id, actor_subject, standing_grant_id) == (
            "venture", "card", state, "workspace", "actor", "grant")
        calls.append("confirm")
        message["actions"][0]["confirmation"]["state"] = "started"
        return reply

    monkeypatch.setattr(standing, "enabled", unit("enabled", True))
    monkeypatch.setattr(standing, "matching_grant", unit("grant", "grant" if granted else None))
    monkeypatch.setattr(confirmations, "card", unit("card", message))
    monkeypatch.setattr(tenancy, "current_or_none", lambda: SimpleNamespace(
        workspace_id="workspace", venture_id="venture", actor="actor"))
    monkeypatch.setattr(conversations, "current_conversation", lambda: SimpleNamespace(
        visibility="private", workspace_id="workspace", venture_id="venture", actor_id="actor"))
    monkeypatch.setattr(chat_api, "_confirm_action", confirm)

    async def run():
        marker.set(domain)
        return await confirmations.auto_dispatch("venture", message, "actor")

    result = asyncio.run(run())
    assert result is (reply if granted else message)
    assert calls == ["enabled", "card", "grant", *(["confirm", "card"] if granted else [])]

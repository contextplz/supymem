import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from tests.security.test_core_signed_boundary import client, headers, make_workspace


@pytest.fixture(params=[
    ("usr_alice", "usr_bob", "Maintenance records for logistics operators"),
    ("usr_bob", "usr_alice", "Weekend trail guides for families"),
])
def standing_chat(client, monkeypatch, request):
    from app.api import chat_api
    from app.approvals import standing
    from app.chat import execute
    from app.chat.actions import get_action
    from app.chat.confirm import ConfirmationCard
    from app.chat.conversations import bind_conversation
    from app.chat.store import append_message
    from app.credits.policy import get_policy, set_policy
    from app.tenancy import bind

    founder, other, idea = request.param
    set_policy("feature_flags", {**(get_policy("feature_flags") or {}), "approvals_policy_v1": True}, "test")
    make_workspace(client, founder)
    make_workspace(client, other)
    created = client.post("/ventures", json={"raw_idea": idea, "start": False},
                          headers=headers("/ventures", "POST", founder))
    assert created.status_code == 201, created.text
    venture, workspace = created.json()["id"], created.json()["workspace_id"]
    path = f"/ventures/{venture}/chat"
    conversation = client.post(path + "/conversations", json={"idempotency_key": "standing-chat"},
        headers=headers(path + "/conversations", "POST", founder))
    assert conversation.status_code == 200, conversation.text
    conversation_id = conversation.json()["id"]
    state = {"ceiling_micro": 5, "task_allowance_micro": 10}
    card = ConfirmationCard.for_action(get_action("research.start"), interpretation="Research your saved context.",
                                       resulting_state=state)
    with bind(workspace_id=workspace, venture_id=venture, actor=founder), bind_conversation(venture, conversation_id):
        saved = append_message(venture, role="assistant", kind="confirmation", body_md="Review your saved action.", actions=[card.as_action()])
        grant = standing.choose_card(workspace_id=workspace, founder_id=founder, detail={"choice": "always",
            "venture_id": venture, "message_id": saved["id"], "action": "research.start"})
    calls = []
    monkeypatch.setattr(execute, "_research_start", lambda *args, **kwargs: calls.append(args) or {
        "kind": "text", "action_started": True, "body_md": "Your research is queued.", "run_id": "job_saved", "actions": []})

    async def reply(venture_id, text, **kwargs):
        return append_message(venture_id, role="assistant", kind="confirmation", body_md="Review your saved action.", actions=[card.as_action()])

    monkeypatch.setattr(chat_api, "handle_message", reply)
    return {"client": client, "founder": founder, "other": other, "venture": venture, "workspace": workspace,
            "conversation": conversation_id, "path": path, "state": state, "grant": grant.id, "calls": calls,
            "card": card, "api": chat_api, "standing": standing}


def _post_standing_chat(chat, *, actor=None, conversation=None):
    return chat["client"].post(chat["path"], json={"message": "Review my saved context.",
        "conversation_id": conversation or chat["conversation"]},
        headers=headers(chat["path"], "POST", actor or chat["founder"]))


def test_revoke_missing_approval_returns_client_error_without_dispatch(standing_chat):
    chat = standing_chat
    path = chat["path"] + "/confirm"
    response = chat["client"].post(path, json={"action": "approvals.revoke",
        "payload": {"approval_id": "missing"}, "conversation_id": chat["conversation"]},
        headers=headers(path, "POST", chat["founder"]))
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["error"] == {
        "code": "unknown_action", "message": "That approval doesn't exist."}
    assert chat["calls"] == []


def test_chat_auto_dispatch_is_private_audited_single_use_and_revocable(standing_chat):
    from app.db.models import EventLog, WorkspaceMember
    from app.state import db
    from core.clock import utcnow
    from core.ids import new_id

    chat = standing_chat
    with db()() as session:
        session.add(WorkspaceMember(id=new_id("wmb"), workspace_id=chat["workspace"], person_id=chat["other"],
            role="owner", can_approve=1, created_at=utcnow()))
        session.commit()
    conversation_path = chat["path"] + "/conversations"
    other = chat["client"].post(conversation_path, json={"idempotency_key": "other-private-chat"},
                              headers=headers(conversation_path, "POST", chat["other"]))
    assert other.status_code == 200, other.text
    unrelated = _post_standing_chat(chat, actor=chat["other"], conversation=other.json()["id"])
    assert unrelated.status_code == 200, unrelated.text
    assert unrelated.json()["kind"] == "confirmation" and chat["calls"] == []
    response = _post_standing_chat(chat)
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert "Done automatically under your standing approval" in receipt["body_md"]
    assert "\u2014" not in receipt["body_md"]
    assert receipt["confirmed_action"]["state"] == "started"
    undo = next(action for action in receipt["actions"] if action.get("action") == "chat.stop")
    assert undo["payload"]["job_ids"] == ["job_saved"]
    assert undo["payload"]["nonce"] == receipt["client_request_id"]
    assert undo["payload"]["nonce"].startswith("standing-confirm:")
    revoke = next(action for action in receipt["actions"] if action.get("action") == "approvals.revoke")
    assert revoke["payload"]["approval_id"] == chat["grant"]
    assert len(chat["calls"]) == 1
    confirmation_path = chat["path"] + "/confirm"
    replay = chat["client"].post(confirmation_path, json={"action": "research.start", "payload": chat["state"],
        "message_id": receipt["confirmed_action"]["message_id"], "conversation_id": chat["conversation"]},
        headers=headers(confirmation_path, "POST", chat["founder"]))
    assert replay.status_code == 200, replay.text
    assert len(chat["calls"]) == 1
    with db()() as session:
        audits = session.scalars(select(EventLog).where(EventLog.topic == "approval.auto_approved")).all()
    assert len(audits) == 1
    assert audits[0].on_behalf_of == chat["founder"] and audits[0].venture_id == chat["venture"]
    assert json.loads(audits[0].payload_json)["standing_approval_id"] == chat["grant"]
    missing = chat["client"].post(confirmation_path, json={"action": revoke["action"], "payload": {"approval_id": "missing"},
        "conversation_id": chat["conversation"]}, headers=headers(confirmation_path, "POST", chat["founder"]))
    assert missing.status_code == 400, missing.text
    foreign = chat["client"].post(confirmation_path, json={"action": revoke["action"], "payload": revoke["payload"],
        "conversation_id": other.json()["id"]}, headers=headers(confirmation_path, "POST", chat["other"]))
    assert foreign.status_code == 400, foreign.text
    assert foreign.json()["detail"]["error"]["message"] == "That approval is not available to you."
    revoked = chat["client"].post(confirmation_path, json={"action": revoke["action"], "payload": revoke["payload"],
        "conversation_id": chat["conversation"]}, headers=headers(confirmation_path, "POST", chat["founder"]))
    assert revoked.status_code == 200, revoked.text
    assert "revoked" in revoked.json()["body_md"]
    after = _post_standing_chat(chat)
    assert after.status_code == 200, after.text
    assert after.json()["kind"] == "confirmation" and len(chat["calls"]) == 1
    grants_path = f"/workspaces/{chat['workspace']}/approvals"
    assert chat["client"].get(grants_path + "?status=standing", headers=headers(grants_path, subject=chat["founder"])).json()["items"] == []


@pytest.mark.parametrize("change", ["revoke", "expiry", "scope", "founder", "allowance", "flag_off"])
def test_chat_execution_rechecks_live_grant_after_matching(standing_chat, monkeypatch, change):
    from app.db.models import Approval, EventLog
    from app.state import db
    from core.clock import utcnow

    chat = standing_chat
    original = chat["api"].execute_action

    async def changed(venture_id, action, **kwargs):
        assert kwargs.get("standing_grant_id") == chat["grant"]
        if change == "revoke":
            chat["standing"].revoke(chat["grant"], chat["founder"])
        elif change == "flag_off":
            from app.credits.policy import get_policy, set_policy
            set_policy("feature_flags", {**get_policy("feature_flags"), "approvals_policy_v1": False}, "test")
        elif change == "allowance":
            kwargs["payload"]["task_allowance_micro"] = 4
        else:
            with db()() as session:
                grant = session.get(Approval, chat["grant"])
                if change == "expiry":
                    grant.expires_at = utcnow() - timedelta(seconds=1)
                else:
                    detail = json.loads(grant.detail_json)
                    detail["scope" if change == "scope" else "founder_id"] = "changed"
                    grant.detail_json = json.dumps(detail)
                session.commit()
        return await original(venture_id, action, **kwargs)

    monkeypatch.setattr(chat["api"], "execute_action", changed)
    response = _post_standing_chat(chat)
    assert response.status_code == 200, response.text
    assert response.json()["kind"] == "confirmation" and chat["calls"] == []
    assert not response.json()["actions"][0]["confirmation"].get("state")
    with db()() as session:
        assert session.scalar(select(EventLog.id).where(EventLog.topic == "approval.auto_approved")) is None
    monkeypatch.setattr(chat["api"], "execute_action", original)
    confirmation_path = chat["path"] + "/confirm"
    manual = chat["client"].post(confirmation_path, json={"action": "research.start", "payload": chat["state"],
        "message_id": response.json()["id"], "conversation_id": chat["conversation"]},
        headers=headers(confirmation_path, "POST", chat["founder"]))
    assert manual.status_code == 200, manual.text
    assert len(chat["calls"]) == 1
    assert "Done automatically" not in manual.json()["body_md"]


def test_chat_hard_rules_never_auto_dispatch_even_with_forged_grants(standing_chat, monkeypatch):
    from app.chat.confirm import ConfirmationCard
    from app.chat.store import append_message
    from app.db.models import Approval, EventLog
    from app.state import db

    chat = standing_chat
    for action, state in [(name, chat["state"]) for name in (
            "gtm.send", "social_post", "payments.purchase", "account.change", "deploy_production", "build.start",
            "form.submit", "connections.connect", "connections.disconnect", "delete")] + [
            ("research.start", {**chat["state"], "ceiling_micro": 11}),
            ("outreach.followup", {**chat["state"], "first_send": True, "sequence_approved": True, "sent_today": 0, "daily_cap": 1}),
            ("outreach.followup", {**chat["state"], "first_send": False, "sequence_approved": True, "sent_today": 1, "daily_cap": 1})]:
        verified = chat["standing"].card_action(action, state)
        with db()() as session:
            grant = session.get(Approval, chat["grant"])
            grant.subject_ref = chat["standing"]._key(chat["founder"], chat["venture"], verified)
            detail = json.loads(grant.detail_json)
            detail.update(action_type=action, scope=verified.scope)
            grant.detail_json = json.dumps(detail)
            session.commit()

        async def reply(venture_id, text, **kwargs):
            card = ConfirmationCard(action, "cap.write.work_start", "Review your saved action.", resulting_state=state)
            return append_message(venture_id, role="assistant", kind="confirmation", body_md="Review your saved action.", actions=[card.as_action()])

        monkeypatch.setattr(chat["api"], "handle_message", reply)
        response = _post_standing_chat(chat)
        assert response.status_code == 200, response.text
        assert response.json()["kind"] == "confirmation" and chat["calls"] == []
        assert response.json()["actions"][0]["confirmation"]["approval_policy"]["eligible"] is False
    with db()() as session:
        assert session.scalar(select(EventLog.id).where(EventLog.topic == "approval.auto_approved")) is None


def test_chat_keeps_hard_rule_card_visible_after_auto_dispatch(standing_chat, monkeypatch):
    from app.chat.actions import get_action
    from app.chat.confirm import ConfirmationCard
    from app.chat.store import append_message

    chat = standing_chat

    async def reply(venture_id, text, **kwargs):
        public = ConfirmationCard.for_action(get_action("social_post"), interpretation="Review your public post.",
                                            resulting_state=chat["state"])
        return append_message(venture_id, role="assistant", kind="confirmation", body_md="Review your saved actions.",
                              actions=[chat["card"].as_action(), public.as_action()])

    monkeypatch.setattr(chat["api"], "handle_message", reply)
    response = _post_standing_chat(chat)
    assert response.status_code == 200, response.text
    assert response.json()["kind"] == "confirmation" and len(chat["calls"]) == 1
    cards = {entry["confirmation"]["action"]: entry["confirmation"] for entry in response.json()["actions"]}
    assert cards["research.start"]["state"] == "started"
    assert not cards["social_post"].get("state") and cards["social_post"]["approval_policy"]["eligible"] is False
    history = chat["client"].get(chat["path"] + f"?conversation_id={chat['conversation']}",
                               headers=headers(chat["path"], subject=chat["founder"]))
    assert history.status_code == 200, history.text
    assert any("Done automatically under your standing approval" in item["body_md"] for item in history.json()["items"])


@pytest.mark.parametrize("founder,other,idea", [
    ("usr_alice", "usr_bob", "Maintenance records for logistics operators"),
    ("usr_bob", "usr_alice", "Weekend trail guides for families"),
])
def test_signed_card_choices_revoke_and_private_grants(client, monkeypatch, founder, other, idea):
    from app.approvals import standing
    from app.chat.actions import get_action
    from app.chat.confirm import ConfirmationCard
    from app.chat.conversations import bind_conversation
    from app.chat.store import append_message
    from app.db.models import WorkspaceMember
    from app.state import db
    from app.tenancy import bind
    from core.clock import utcnow

    from app.credits.policy import get_policy, set_policy
    set_policy("feature_flags", {**(get_policy("feature_flags") or {}), "approvals_policy_v1": True}, "test")
    make_workspace(client, founder)
    make_workspace(client, other)
    created = client.post("/ventures", json={"raw_idea": idea, "start": False},
                          headers=headers("/ventures", "POST", founder))
    assert created.status_code == 201, created.text
    venture = created.json()["id"]
    workspace = created.json()["workspace_id"]
    conversation_path = f"/ventures/{venture}/chat/conversations"
    conversation = client.post(conversation_path, json={"idempotency_key": "private-policy-card"},
                               headers=headers(conversation_path, "POST", founder))
    assert conversation.status_code == 200, conversation.text
    conversation_id = conversation.json()["id"]
    state = {"ceiling_micro": 0, "task_allowance_micro": 0}
    with bind(workspace_id=workspace, venture_id=venture, actor=founder), bind_conversation(venture, conversation_id):
        card = ConfirmationCard.for_action(get_action("research.start"), interpretation=f"Research {idea}",
                                           resulting_state=state).as_action()
        message = append_message(venture, role="assistant", kind="confirmation", body_md="Review your saved action.", actions=[card])
        hard = ConfirmationCard.for_action(get_action("social_post"), interpretation="Review a public post", resulting_state=state).as_action()
        hard_message = append_message(venture, role="assistant", kind="confirmation", body_md="Review this public action.", actions=[hard])
    assert card["confirmation"]["approval_policy"]["eligible"] is True
    approval_path = f"/workspaces/{workspace}/approvals"
    def choose(message_id, action, choice, actor=founder, claimed=None):
        return client.post(approval_path, json={"kind": "scope_grant", "subject_ref": "untrusted", "summary": "Untrusted",
            "requested_by": claimed or actor, "detail": {"standing_policy_v1": {
                "choice": choice, "venture_id": venture, "message_id": message_id, "action": action,
                "scope": "untrusted-broad-scope"}}}, headers=headers(approval_path, "POST", actor))

    allowed = choose(message["id"], "research.start", "always")
    assert allowed.status_code == 201, allowed.text
    grant_id = allowed.json()["id"]
    assert allowed.json()["detail"]["scope"] != "untrusted-broad-scope"
    with db()() as session:
        session.add(WorkspaceMember(id="shared-policy-member", workspace_id=workspace, person_id=other,
                                    role="owner", can_approve=1, created_at=utcnow()))
        session.commit()
    assert choose(message["id"], "research.start", "always", actor=other).status_code == 404
    assert choose(message["id"], "research.start", "always", actor=other, claimed=founder).status_code == 403
    assert client.get(approval_path + "?status=standing", headers=headers(approval_path, subject=other)).json()["items"] == []
    get_path = f"/approvals/{grant_id}"
    assert client.get(get_path, headers=headers(get_path, subject=other)).status_code == 404
    revoke_path = get_path + "/decide"
    assert client.post(revoke_path, json={"decision": "withdrawn", "decided_by": other},
                       headers=headers(revoke_path, "POST", other)).status_code == 404
    assert choose(hard_message["id"], "social_post", "always").status_code == 422
    denied_choice = choose(message["id"], "research.start", "deny")
    assert denied_choice.status_code == 201
    denied_path = f"/approvals/{denied_choice.json()['id']}"
    assert client.get(denied_path, headers=headers(denied_path, subject=other)).status_code == 404
    assert client.post(denied_path + "/decide", json={"decision": "approved", "decided_by": other},
                       headers=headers(denied_path + "/decide", "POST", other)).status_code == 404
    confirm_path = f"/ventures/{venture}/chat/confirm"
    denied = client.post(confirm_path, json={"conversation_id": conversation_id, "message_id": message["id"],
                         "action": "research.start", "payload": state}, headers=headers(confirm_path, "POST", founder))
    assert denied.status_code == 409, denied.text
    with db()() as session:
        member = session.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace,
                                                            WorkspaceMember.person_id == founder))
        member.can_approve = 0
        session.commit()
    revoked = client.post(revoke_path, json={"decision": "withdrawn", "decided_by": founder},
                          headers=headers(revoke_path, "POST", founder))
    assert revoked.status_code == 200, revoked.text
    with db()() as session:
        member = session.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace,
                                                            WorkspaceMember.person_id == founder))
        member.can_approve = 1
        session.commit()
    with bind(workspace_id=workspace, venture_id=venture, actor=founder):
        assert standing.auto_approve(workspace_id=workspace, venture_id=venture, founder_id=founder,
            action=standing.card_action("research.start", state), kind="workflow_write", subject_ref="task",
            summary="Research saved inputs", requested_by="agt_reader") is None
    ordinary = client.post(approval_path, json={"kind": "scope_grant", "subject_ref": "standing:ordinary",
        "summary": "Review a normal scope request", "requested_by": founder}, headers=headers(approval_path, "POST", founder))
    assert ordinary.status_code == 201, ordinary.text
    ordinary_path = f"/approvals/{ordinary.json()['id']}"
    assert client.get(ordinary_path, headers=headers(ordinary_path, subject=other)).status_code == 200

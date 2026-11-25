"""Signed exact-card admission, replay and changed-target boundaries."""
from datetime import timedelta

from tests.security.test_plan_boundary import client as client, ready as ready, compile
from tests.security.test_durable_journey_boundary import request, venture


def prepare(client, vid, plan_id):
    response = request(client, "POST", f"/ventures/{vid}/chat/proposals", json={"action": "plan.approve", "plan_id": plan_id})
    assert response.status_code == 200, response.text
    return response.json()


def test_exact_approval_replay_and_payload_cannot_replace_review(client, ready, monkeypatch):
    from app.chat import proposals
    vid, _, calls = ready
    plan = compile(client, vid)
    card = prepare(client, vid, plan["id"])
    assert card["state"] == "pending" and card["review"]["proposal"] == plan["proposal"]
    assert card["review"]["credit_ceiling_micro"] == 0
    base = f"/ventures/{vid}/chat/proposals/{card['id']}"
    assert request(client, "POST", base + "/confirm", json={"plan_id": "replacement"}).status_code == 422
    first = request(client, "POST", base + "/confirm", json={})
    assert first.status_code == 200, first.text
    assert first.json()["receipt"] == {"plan_id": plan["id"], "plan_state": "approved", "execution_started": False}
    def forbidden(*args, **kwargs):
        raise AssertionError("Duplicate confirmation re-executed")
    monkeypatch.setattr(proposals.compiler, "approve_plan", forbidden)
    assert request(client, "POST", base + "/confirm", json={}).json() == first.json()
    assert len(calls) == 1


def test_changed_plan_cancellation_expiry_and_cross_founder(client, ready):
    from app.db.supymem_models import ChatProposal
    from app.state import db
    from core.clock import utcnow
    vid, wid, _ = ready
    plan = compile(client, vid)
    stale = prepare(client, vid, plan["id"])
    root = f"/ventures/{vid}/chat/proposals"
    request(client, "PATCH", f"/ventures/{vid}/plans/{plan['id']}", json={"edit_version": 1, "patch": {"summary": "Changed after card review"}})
    assert request(client, "POST", root + f"/{stale['id']}/confirm", json={}).status_code == 409
    assert request(client, "GET", root + f"/{stale['id']}").json()["state"] == "needs_reconciliation"
    cancelled = prepare(client, vid, plan["id"])
    assert request(client, "POST", root + f"/{cancelled['id']}/cancel", json={}).json()["state"] == "cancelled"
    assert request(client, "POST", root + f"/{cancelled['id']}/confirm", json={}).status_code == 409
    expired = prepare(client, vid, plan["id"])
    with db()() as session:
        session.get(ChatProposal, expired["id"]).expires_at = utcnow() - timedelta(seconds=1)
        session.commit()
    assert request(client, "POST", root + f"/{expired['id']}/confirm", json={}).status_code == 409
    bob, _, _ = venture(client, "usr_bob")
    assert request(client, "GET", root + f"/{expired['id']}", "usr_bob").status_code == 404
    assert request(client, "GET", f"/ventures/{bob}/chat/proposals/{expired['id']}", "usr_bob").status_code == 404
    assert request(client, "POST", f"/ventures/{bob}/chat/proposals", "usr_bob", json={"action": "plan.approve", "plan_id": plan["id"]}).status_code == 404
    # Even an authorized colleague cannot consume this founder's card.
    from app.db.models import WorkspaceMember
    with db()() as session:
        session.add(WorkspaceMember(id="proposal-colleague", workspace_id=wid, person_id="usr_bob", role="admin", can_approve=1, created_at=utcnow()))
        session.commit()
    assert request(client, "GET", f"/ventures/{vid}/plans", "usr_bob").status_code == 200
    assert request(client, "GET", root + f"/{expired['id']}", "usr_bob").status_code == 404
    assert request(client, "POST", root + f"/{expired['id']}/confirm", "usr_bob", json={}).status_code == 404


def test_uncertain_outcome_is_frozen_and_never_retried(client, ready, monkeypatch):
    import pytest
    from app.chat import proposals
    vid, _, _ = ready
    plan = compile(client, vid)
    card = prepare(client, vid, plan["id"])
    calls = []
    def interrupted(*args, **kwargs):
        calls.append(args)
        raise RuntimeError("Injected uncertain outcome")
    monkeypatch.setattr(proposals.compiler, "approve_plan", interrupted)
    with pytest.raises(RuntimeError):
        proposals.confirm(vid, "usr_alice", card["id"])
    saved = proposals.get(vid, "usr_alice", card["id"])
    assert saved["state"] == "needs_reconciliation"
    assert saved["receipt"]["code"] == "chat_action_outcome_unknown"
    with pytest.raises(ValueError, match="not_confirmable"):
        proposals.confirm(vid, "usr_alice", card["id"])
    assert len(calls) == 1



def test_reconciliation_observes_committed_approval_without_redispatch(client, ready, monkeypatch):
    import pytest
    from app.chat import proposals
    vid, _, _ = ready
    plan = compile(client, vid)
    card = prepare(client, vid, plan["id"])
    original = proposals.compiler.approve_plan
    calls = []
    def after_commit(*args, **kwargs):
        calls.append(args)
        original(*args, **kwargs)
        raise RuntimeError("Lost response after durable approval")
    monkeypatch.setattr(proposals.compiler, "approve_plan", after_commit)
    with pytest.raises(RuntimeError):
        proposals.confirm(vid, "usr_alice", card["id"])
    result = request(client, "POST", f"/ventures/{vid}/chat/proposals/{card['id']}/reconcile", json={})
    assert result.status_code == 200, result.text
    assert result.json()["state"] == "succeeded" and result.json()["receipt"]["reconciled"] is True
    assert proposals.confirm(vid, "usr_alice", card["id"])["state"] == "succeeded"
    assert len(calls) == 1


def test_reconnect_lists_actor_scoped_cards_with_stable_pagination(client, ready):
    from app.db.models import WorkspaceMember
    from app.db.supymem_models import ChatProposal
    from app.state import db
    from core.clock import utcnow
    vid, wid, _ = ready
    plan = compile(client, vid)
    card = prepare(client, vid, plan["id"])
    bob, _, _ = venture(client, "usr_bob")
    now = utcnow()
    with db()() as session:
        original = session.get(ChatProposal, card["id"])
        original.created_at = now
        for index in range(51):
            session.add(ChatProposal(id=f"reconnect-{index:03d}", workspace_id=wid,
                venture_id=vid, actor_id="usr_alice", action=original.action,
                state="cancelled", arguments_json=original.arguments_json,
                review_json=original.review_json, created_at=now,
                updated_at=now, expires_at=original.expires_at))
        session.add(WorkspaceMember(id="reconnect-colleague", workspace_id=wid,
            person_id="usr_bob", role="admin", can_approve=1, created_at=now))
        session.commit()
    root = f"/ventures/{vid}/chat/proposals"
    first = request(client, "GET", root)
    assert first.status_code == 200, first.text
    first = first.json()
    assert len(first["items"]) == 50 and first["next_cursor"]
    second_response = request(client, "GET", root, params={"before": first["next_cursor"]})
    assert second_response.status_code == 200, second_response.text
    second = second_response.json()
    assert len(second["items"]) == 2 and second["next_cursor"] is None
    ids = [row["id"] for row in first["items"] + second["items"]]
    assert len(set(ids)) == 52 and card["id"] in ids
    assert request(client, "GET", root, "usr_bob").json() == {"items": [], "next_cursor": None}
    assert request(client, "GET", root, "usr_bob", params={"before": card["id"]}).status_code == 404
    assert request(client, "GET", f"/ventures/{bob}/chat/proposals", "usr_bob", params={"before": card["id"]}).status_code == 404

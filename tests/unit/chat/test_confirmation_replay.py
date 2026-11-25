"""Exact saved confirmation cards are single-use, including across reloads."""
import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.unit.chat.test_chat_request_status import chat_request  # noqa: F401
from tests.unit.chat.test_next_moves import DOMAINS


def test_confirmation_replays_existing_receipt_and_run_without_redispatch(chat_request, monkeypatch):
    harness = chat_request
    from app.chat.conversations import bind_conversation
    from app.chat.store import append_message, list_messages
    from app.db.models import Job, Venture
    from app.pagebuilder.api import KIND, _quote_fingerprint
    from app.tenancy import bind
    from core.clock import utcnow

    path = f"/ventures/{harness['vid']}/chat/confirm"
    calls = []
    entered, release = threading.Event(), threading.Event()

    async def execute(venture_id, action, **kwargs):
        calls.append((venture_id, action, kwargs))
        if kwargs["payload"].get("wait"):
            entered.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
        return {"body_md": "Your saved work started.", "run_id": f"run_{len(calls)}"}

    monkeypatch.setattr(harness["api"], "execute_action", execute)

    def save_card(arguments):
        with bind(workspace_id=harness["wid"], venture_id=harness["vid"], actor=harness["actor"]):
            with bind_conversation(harness["vid"], harness["cid"]):
                return append_message(harness["vid"], role="assistant", kind="confirmation", body_md="Review your action.",
                    actions=[{"label": "Do it", "confirmation": {"action": "pagebuilder.build", "resulting_state": arguments}}])

    def confirm(message, arguments, key="operation-one"):
        return harness["client"].post(path, headers={**harness["headers"](path, "POST"), "Idempotency-Key": key},
            json={"action": "pagebuilder.build", "message_id": message["id"], "payload": arguments,
                  "conversation_id": harness["cid"], "idempotency_key": key})

    for domain in ("industrial inventory buyers", "neighborhood pet sitting"):
        with harness["factory"]() as session:
            venture = session.get(Venture, harness["vid"])
            venture.raw_idea, venture.name = domain, domain
            session.commit()
        arguments = {"domain": domain}
        message = save_card(arguments)
        before = len(calls)
        first = confirm(message, arguments)
        assert first.status_code == 200, first.text
        replay = confirm(message, arguments, "different-client-nonce")
        assert replay.status_code == 200, replay.text
        assert first.json() == replay.json()
        assert len(calls) == before + 1
        assert calls[-1][2]["request_nonce"] == "operation-one"
        assert first.json()["confirmed_action"]["run_id"] == f"run_{len(calls)}"
        with bind(workspace_id=harness["wid"], venture_id=harness["vid"], actor=harness["actor"]):
            with bind_conversation(harness["vid"], harness["cid"]):
                saved = next(row for row in list_messages(harness["vid"]) if row["id"] == message["id"])
        assert saved["actions"][0]["confirmation"]["state"] == "started"
        assert confirm(message, {"domain": "changed"}).status_code == 409
        assert len(calls) == before + 1

        concurrent_arguments = {"domain": domain, "wait": True}
        concurrent_card = save_card(concurrent_arguments)
        entered.clear()
        release.clear()
        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(confirm, concurrent_card, concurrent_arguments)
            try:
                assert entered.wait(5)
                pending = confirm(concurrent_card, concurrent_arguments, "parallel-nonce")
                assert pending.status_code == 409, pending.text
                assert pending.json()["detail"]["error"]["code"] == "chat_request_in_progress"
            finally:
                release.set()
            finished = running.result(timeout=5)
        assert finished.status_code == 200, finished.text
        assert len(calls) == before + 2
        assert confirm(concurrent_card, concurrent_arguments).json() == finished.json()

        inputs = {"audience": domain}
        quote = {"resource_digest": domain, "ceiling_micro": 12000}
        revision = f"revision_{before}"
        fingerprint = _quote_fingerprint(harness["wid"], harness["actor"], harness["vid"], revision, inputs, quote, 1)
        legacy_arguments = {"quote_fingerprint": fingerprint, "ceiling_micro": quote["ceiling_micro"]}
        legacy = save_card(legacy_arguments)
        job_id = f"job_legacy_{before}"
        with harness["factory"]() as session:
            session.add(Job(id=job_id, venture_id=harness["vid"], workspace_id=harness["wid"], kind=KIND,
                status="succeeded", dedupe_key=job_id, created_at=utcnow(),
                payload_json=json.dumps({"_actor_subject": harness["actor"], "revision_id": revision,
                    "variant_count": 1, "_runtime": {"input": inputs, "quote": quote}, "result": {}})))
            session.commit()
        with bind(workspace_id=harness["wid"], venture_id=harness["vid"], actor=harness["actor"]):
            with bind_conversation(harness["vid"], harness["cid"]):
                restored = next(row for row in list_messages(harness["vid"]) if row["id"] == legacy["id"])
        assert restored["actions"][0]["confirmation"]["state"] == "started"
        existing = confirm(legacy, legacy_arguments)
        assert existing.status_code == 200, existing.text
        assert existing.json()["confirmed_action"]["run_id"] == job_id
        assert confirm(legacy, legacy_arguments, "new-nonce").json() == existing.json()
        assert len(calls) == before + 2
        fresh_card = save_card(legacy_arguments)
        with bind(workspace_id=harness["wid"], venture_id=harness["vid"], actor=harness["actor"]):
            with bind_conversation(harness["vid"], harness["cid"]):
                fresh = next(row for row in list_messages(harness["vid"]) if row["id"] == fresh_card["id"])
        assert "state" not in fresh["actions"][0]["confirmation"], "An explicit new review is not an older consumed card"


def test_restore_skips_legacy_string_actions():
    from app.chat import confirmations

    messages = [{"id": "m1", "actions": ["Open review", {"label": "x"}, {"confirmation": "legacy"}]},
                {"id": "m2", "actions": None}]
    assert confirmations.restore(messages, "ven_any") is messages


def _save_confirmation_cards(chat, idea, action, states):
    from app.chat.conversations import bind_conversation
    from app.chat.store import append_message
    from app.db.models import Venture
    from app.tenancy import bind

    with chat["factory"]() as session:
        venture = session.get(Venture, chat["vid"])
        venture.name, venture.raw_idea = idea, idea
        session.commit()
    with bind(workspace_id=chat["wid"], venture_id=chat["vid"], actor=chat["actor"]):
        with bind_conversation(chat["vid"], chat["cid"]):
            return append_message(chat["vid"], role="assistant", kind="confirmation", body_md=idea,
                actions=[{"label": f"Review {index + 1}", "confirmation": {
                    "action": action, "resulting_state": state}} for index, state in enumerate(states)])


def _confirm_saved_card(chat, message, action, state, *, actor_subject=None, standing_grant_id=None,
                        idempotency_key=None):
    from app.chat.conversations import bind_conversation
    from app.tenancy import bind

    payload = chat["api"].ConfirmBody(action=action, payload=state,
        message_id=message["id"] if message else None, conversation_id=chat["cid"], idempotency_key=idempotency_key)
    with bind(workspace_id=chat["wid"], venture_id=chat["vid"], actor=chat["actor"]):
        with bind_conversation(chat["vid"], chat["cid"]):
            return asyncio.run(chat["api"]._confirm_action(chat["vid"], payload, chat["wid"],
                chat["actor"] if actor_subject is None else actor_subject, standing_grant_id=standing_grant_id))


@pytest.mark.parametrize("idea", DOMAINS)
@pytest.mark.parametrize("custody", ["foreign_actor", "foreign_venture", "foreign_workspace",
                                     "explicit_actor", "explicit_actor_after_receipt"])
def test_saved_confirmation_requires_same_actor_venture_and_workspace(chat_request, monkeypatch, idea, custody):
    from fastapi import HTTPException
    from app.authz.workspace import ensure_subject_person
    from app.chat import experiments
    from app.chat.conversations import create
    from app.db.models import ChatMessage, Venture, WorkspaceMember
    from app.tenancy import bind
    from app.workspaces.service import create_workspace
    from core.clock import utcnow

    chat, calls = chat_request, []
    action, state = "experiment.run", {"experiment_id": "scan_custody", "domain": idea}

    async def execute(venture_id, name, payload, actor_subject):
        calls.append((venture_id, name, payload, actor_subject))
        return {"body_md": "Your reviewed experiment started.", "action_started": True, "run_id": "run_custody"}

    monkeypatch.setattr(experiments, "execute", execute)
    message = _save_confirmation_cards(chat, idea, action, [state])
    other_actor = "usr_other_custody"
    ensure_subject_person(other_actor)
    with chat["factory"]() as session:
        session.add(WorkspaceMember(id="wmb_other_custody", workspace_id=chat["wid"],
            person_id=other_actor, role="owner", can_approve=1, created_at=utcnow()))
        session.commit()
    attempted, explicit_actor = chat, None
    if custody == "foreign_actor":
        with bind(workspace_id=chat["wid"], venture_id=chat["vid"], actor=other_actor):
            conversation = create(chat["vid"], idempotency_key="other-custody-conversation")
        attempted = {**chat, "actor": other_actor, "cid": conversation["id"]}
    elif custody == "foreign_venture":
        other_venture = "ven_other_custody"
        with chat["factory"]() as session:
            session.add(Venture(id=other_venture, workspace_id=chat["wid"], name=idea, raw_idea=idea,
                stage="draft", created_at=utcnow(), updated_at=utcnow()))
            session.commit()
        with bind(workspace_id=chat["wid"], venture_id=other_venture, actor=chat["actor"]):
            conversation = create(other_venture, idempotency_key="other-venture-custody")
        attempted = {**chat, "vid": other_venture, "cid": conversation["id"]}
    elif custody == "foreign_workspace":
        workspace = create_workspace(kind="company", name=idea, owner_id=other_actor)
        with chat["factory"]() as session:
            session.get(ChatMessage, message["id"]).workspace_id = workspace.id
            session.commit()
    else:
        explicit_actor = other_actor
        if custody == "explicit_actor_after_receipt":
            receipt = _confirm_saved_card(chat, message, action, state)
            assert calls == [(chat["vid"], action, state, chat["actor"])]
            assert _confirm_saved_card(chat, message, action, state,
                actor_subject=f" {chat['actor']} ") == receipt
    before = list(calls)
    with pytest.raises(HTTPException) as rejected:
        _confirm_saved_card(attempted, message, action, state, actor_subject=explicit_actor)
    assert rejected.value.status_code in {403, 404, 409}
    assert calls == before
    with chat["factory"]() as session:
        confirmation = json.loads(session.get(ChatMessage, message["id"]).actions_json)[0]["confirmation"]
    assert confirmation.get("state") == ("started" if custody == "explicit_actor_after_receipt" else None)


@pytest.mark.parametrize("idea", DOMAINS)
@pytest.mark.parametrize("duplicate", [False, True], ids=["distinct-payloads", "ambiguous-duplicates"])
def test_same_action_cards_select_exact_second_experiment_and_receipt(chat_request, monkeypatch, idea, duplicate):
    from fastapi import HTTPException
    from app.chat import experiments
    from app.db.models import ChatMessage

    chat, calls = chat_request, []
    action = "experiment.run"
    first_state = {"experiment_id": "scan_first", "domain": idea}
    second_state = dict(first_state) if duplicate else {**first_state, "experiment_id": "scan_second"}

    async def execute(venture_id, name, payload, actor_subject):
        calls.append((venture_id, name, dict(payload), actor_subject))
        return {"body_md": f"Experiment {payload['experiment_id']} started.", "action_started": True,
                "run_id": "run_" + payload["experiment_id"]}

    monkeypatch.setattr(experiments, "execute", execute)
    message = _save_confirmation_cards(chat, idea, action, [first_state, second_state])
    if duplicate:
        with pytest.raises(HTTPException) as rejected:
            _confirm_saved_card(chat, message, action, second_state)
        assert rejected.value.status_code == 409
        assert calls == []
        with chat["factory"]() as session:
            cards = json.loads(session.get(ChatMessage, message["id"]).actions_json)
        assert all(not card["confirmation"].get("state") for card in cards)
        return
    second_receipt = _confirm_saved_card(chat, message, action, second_state)
    assert calls == [(chat["vid"], action, second_state, chat["actor"])]
    assert second_receipt["confirmed_action"]["run_id"] == "run_scan_second"
    with chat["factory"]() as session:
        cards = json.loads(session.get(ChatMessage, message["id"]).actions_json)
    assert not cards[0]["confirmation"].get("state")
    assert cards[1]["confirmation"]["state"] == "started"
    assert _confirm_saved_card(chat, message, action, second_state,
        idempotency_key="different-second-client-key") == second_receipt
    first_receipt = _confirm_saved_card(chat, message, action, first_state)
    assert first_receipt["id"] != second_receipt["id"]
    assert first_receipt["confirmed_action"]["run_id"] == "run_scan_first"
    assert calls == [(chat["vid"], action, second_state, chat["actor"]),
                     (chat["vid"], action, first_state, chat["actor"])]
    assert _confirm_saved_card(chat, message, action, second_state,
        idempotency_key="another-second-client-key") == second_receipt
    assert _confirm_saved_card(chat, message, action, first_state,
        idempotency_key="different-first-client-key") == first_receipt
    with pytest.raises(HTTPException) as changed:
        _confirm_saved_card(chat, message, action, {**second_state, "experiment_id": "scan_not_reviewed"})
    assert changed.value.status_code == 409
    assert changed.value.detail["error"]["code"] == "chat_confirmation_changed"
    with pytest.raises(HTTPException) as missing_id:
        _confirm_saved_card(chat, message, action, {"domain": idea})
    assert missing_id.value.status_code == 409
    assert missing_id.value.detail["error"]["code"] == "chat_confirmation_changed"
    with pytest.raises(HTTPException) as unsaved:
        _confirm_saved_card(chat, {"id": "missing-saved-card"}, action, second_state)
    assert unsaved.value.status_code == 409
    assert unsaved.value.detail["error"]["code"] == "chat_confirmation_not_found"
    assert len(calls) == 2


@pytest.mark.parametrize("idea", DOMAINS)
def test_experiment_decisions_do_not_execute_without_saved_confirmation(chat_request, monkeypatch, idea):
    from fastapi import HTTPException
    from app.chat import experiments

    chat, calls = chat_request, []

    async def decision(venture_id, name, payload, actor_subject):
        calls.append((venture_id, name, payload, actor_subject))
        return {"body_md": "Decision applied.", "action_started": True}

    monkeypatch.setattr(experiments, "decision_run", decision)
    for action in ("experiment.persevere", "experiment.pivot", "experiment.spinoff", "experiment.kill"):
        with pytest.raises(HTTPException) as rejected:
            _confirm_saved_card(chat, None, action, {"experiment_id": "scan_not_reviewed", "domain": idea})
        assert rejected.value.status_code == 400
        assert rejected.value.detail["error"]["code"] == "confirmation_card_required"
    assert calls == []


@pytest.mark.parametrize("idea", DOMAINS)
def test_revoked_standing_confirmation_blocks_pending_work_and_receipt_replay(chat_request, monkeypatch, idea):
    from fastapi import HTTPException
    from sqlalchemy import select
    from app.approvals import standing
    from app.chat import execute
    from app.chat.conversations import bind_conversation
    from app.db.models import Approval, ChatMessage, EventLog
    from app.tenancy import bind

    chat, calls = chat_request, []
    action, state = "research.start", {"ceiling_micro": 5, "task_allowance_micro": 10}
    original_policy = standing.get_policy
    monkeypatch.setattr(standing, "get_policy", lambda key: {"approvals_policy_v1": True}
        if key == "feature_flags" else original_policy(key))

    def research(venture_id, actor_subject, **kwargs):
        calls.append((venture_id, actor_subject, kwargs))
        return {"body_md": "Your reviewed research started.", "action_started": True, "run_id": "run_standing"}

    monkeypatch.setattr(execute, "_research_start", research)
    message = _save_confirmation_cards(chat, idea, action, [state])
    with bind(workspace_id=chat["wid"], venture_id=chat["vid"], actor=chat["actor"]):
        with bind_conversation(chat["vid"], chat["cid"]):
            grant = standing.choose_card(workspace_id=chat["wid"], founder_id=chat["actor"],
                detail={"choice": "always", "venture_id": chat["vid"], "message_id": message["id"], "action": action})
            assert standing.matching_grant(workspace_id=chat["wid"], venture_id=chat["vid"],
                founder_id=chat["actor"], action=standing.card_action(action, state)) == grant.id
    receipt = _confirm_saved_card(chat, message, action, state, standing_grant_id=grant.id)
    assert receipt["confirmed_action"]["run_id"] == "run_standing"
    assert receipt["confirmed_action"]["state"] == "started"
    assert len(calls) == 1
    assert _confirm_saved_card(chat, message, action, state, standing_grant_id=grant.id) == receipt
    pending = _save_confirmation_cards(chat, idea, action, [state])
    with bind(workspace_id=chat["wid"], venture_id=chat["vid"], actor=chat["actor"]):
        standing.revoke(grant.id, chat["actor"])
        assert standing.matching_grant(workspace_id=chat["wid"], venture_id=chat["vid"],
            founder_id=chat["actor"], action=standing.card_action(action, state)) is None
    blocked = _confirm_saved_card(chat, pending, action, state, standing_grant_id=grant.id)
    assert blocked["id"] == pending["id"] and blocked["kind"] == "confirmation"
    assert not blocked["actions"][0]["confirmation"].get("state")
    assert len(calls) == 1
    with pytest.raises(HTTPException) as revoked_replay:
        _confirm_saved_card(chat, message, action, state, standing_grant_id=grant.id)
    assert revoked_replay.value.status_code in {403, 409}
    assert revoked_replay.value.detail["error"]["code"] == "standing_approval_unavailable"
    assert len(calls) == 1
    with chat["factory"]() as session:
        assert session.get(Approval, grant.id).status == "withdrawn"
        assert len(session.scalars(select(EventLog).where(EventLog.topic == "approval.auto_approved")).all()) == 1
        consumed_card = json.loads(session.get(ChatMessage, message["id"]).actions_json)[0]["confirmation"]
        pending_card = json.loads(session.get(ChatMessage, pending["id"]).actions_json)[0]["confirmation"]
    assert consumed_card["state"] == "started"
    assert not pending_card.get("state")

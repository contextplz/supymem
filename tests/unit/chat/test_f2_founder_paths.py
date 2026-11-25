"""F2 acceptance through authenticated founder chat and saved card actions."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Event
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from tests.integration.test_postgres_public_memory_commands import _memory_id
from tests.security.test_core_signed_boundary import client, headers, make_workspace

pytestmark = pytest.mark.hosted_only


DOMAINS = [
    ("usr_cold_chain", "Cold-chain monitoring for hospital procurement teams", "hospital procurement teams", "pharmacy delivery teams"),
    ("usr_music", "Practice journals for amateur violinists", "amateur violinists", "community choir singers"),
]


@pytest.fixture(params=range(len(DOMAINS)), ids=["b2b-cold-chain", "consumer-music"])
def founder_path(client, monkeypatch, request):
    from app import memory, state
    from app.chat import router, store
    from app.memory.adapters.postgres import PostgresMemoryAdapter
    from app.ventures import state_brief

    monkeypatch.setattr(memory, "_ADAPTER", PostgresMemoryAdapter())
    monkeypatch.setattr(router, "MEM", state.MEM)
    monkeypatch.setattr(state_brief, "MEM", state.MEM)
    monkeypatch.setattr(store, "_schedule_diagram", lambda *args, **kwargs: None)
    founders = []
    for subject, idea, audience, replacement in DOMAINS:
        make_workspace(client, subject)
        response = client.post("/ventures", json={"raw_idea": idea, "start": False},
                               headers=headers("/ventures", "POST", subject))
        assert response.status_code == 201, response.text
        venture, workspace = response.json()["id"], response.json()["workspace_id"]
        path = f"/ventures/{venture}/chat"
        conversation = client.post(path + "/conversations", json={"idempotency_key": "founder-path"},
                                   headers=headers(path + "/conversations", "POST", subject))
        assert conversation.status_code == 200, conversation.text
        founders.append(SimpleNamespace(client=client, subject=subject, idea=idea, audience=audience,
            replacement=replacement, venture=venture, workspace=workspace, path=path,
            conversation=conversation.json()["id"]))
    selected = founders[request.param]
    selected.other = founders[1 - request.param]
    return selected


def post(founder, message, **payload):
    return founder.client.post(founder.path, json={"message": message,
        "conversation_id": founder.conversation, "idempotency_key": uuid4().hex, **payload},
        headers=headers(founder.path, "POST", founder.subject))


def click(founder, action, *, message_id=None, **payload):
    path = founder.path + "/confirm"
    return founder.client.post(path, json={"action": action["action"],
        "payload": action.get("confirmation", {}).get("resulting_state", action.get("payload", {})),
        "conversation_id": founder.conversation, "idempotency_key": uuid4().hex,
        **({"message_id": message_id} if message_id else {}),
        **payload}, headers=headers(path, "POST", founder.subject))


def history(founder):
    response = founder.client.get(founder.path, params={"conversation_id": founder.conversation},
                                  headers=headers(founder.path, subject=founder.subject))
    assert response.status_code == 200, response.text
    return response.json()["items"]


def saved_card(founder, action, state):
    from app.chat.actions import get_action
    from app.chat.confirm import ConfirmationCard
    from app.chat.conversations import bind_conversation
    from app.chat.store import append_message
    from app.tenancy import bind

    card = ConfirmationCard.for_action(get_action(action), interpretation="Review your saved action.",
                                       resulting_state=state)
    with bind(workspace_id=founder.workspace, venture_id=founder.venture, actor=founder.subject), \
            bind_conversation(founder.venture, founder.conversation):
        return append_message(founder.venture, role="assistant", kind="confirmation",
                              body_md="Review your saved action.", actions=[card.as_action()])


def test_memory_saved_from_chat_keeps_exact_founder_source(founder_path):
    from app.db.models import ChatMessage, InstitutionalMemory
    from app.db.supymem_models import IdeaRevision
    from app.state import db

    founder = founder_path
    with db()() as session:
        revisions = set(session.scalars(select(IdeaRevision.id)))
    statement = f"Our audience is {founder.audience}. Never publish without my approval."
    response = post(founder, "Remember: " + statement, idempotency_key="save-source")
    assert response.status_code == 200, response.text
    reply = response.json()
    assert reply["kind"] == "memory_saved"
    assert "I've saved that for you." in reply["body_md"] and "review or remove it in Settings" in reply["body_md"]
    assert "\u2014" not in reply["body_md"] and not reply["actions"]
    identity = _memory_id(response)
    with db()() as session:
        assert set(session.scalars(select(IdeaRevision.id))) == revisions
        row = session.get(InstitutionalMemory, identity)
        record = json.loads(row.record_json)
        assert (row.workspace_id, row.venture_id) == (founder.workspace, founder.venture)
        assert record["statement"] == statement and record["author"] == founder.subject
        [source] = record["evidence"]
        message = session.get(ChatMessage, source.removeprefix("chat:"))
        assert (message.author_person_id, message.conversation_id, message.body_md) == (
            founder.subject, founder.conversation, "Remember: " + statement)
        assert record["metadata"]["source_evidence"][0]["ref"] == source
    assert any(item["id"] == reply["id"] for item in history(founder))
    assert post(founder, "Remember: " + statement, idempotency_key="save-source").json() == reply


def test_memory_recall_correction_and_history_remain_source_scoped(founder_path):
    founder = founder_path
    for owner in (founder, founder.other):
        response = post(owner, f"Remember: Our audience is {owner.audience}.")
        assert response.status_code == 200 and response.json()["kind"] == "memory_saved", response.text
        if owner is founder:
            identity = _memory_id(response)
    recall = post(founder, "Who is my audience?")
    assert recall.status_code == 200, recall.text
    assert recall.json()["kind"] == "memory_recall"
    assert founder.audience in recall.json()["body_md"] and founder.other.audience not in recall.json()["body_md"]
    assert recall.json()["citations"][0]["source_ref"].startswith("chat:")
    corrected = post(founder, f"Correct memory {identity}: Our audience is {founder.replacement}.")
    assert corrected.status_code == 200 and corrected.json()["kind"] == "memory_corrected", corrected.text
    current_identity = _memory_id(corrected)
    current = post(founder, "Who is my audience?").json()
    assert founder.replacement in current["body_md"] and founder.audience not in current["body_md"]
    prior = post(founder, "What did I say before about audience?").json()
    assert founder.audience in prior["body_md"] and founder.replacement in prior["body_md"]
    foreign = post(founder.other, f"Forget memory {current_identity}").json()
    assert "No memory was changed" in foreign["body_md"]
    assert founder.replacement in post(founder, "Who is my audience?").json()["body_md"]
    assert founder.other.audience in post(founder.other, "Who is my audience?").json()["body_md"]


def test_known_facts_and_contradictions_reach_experiment_card(founder_path, monkeypatch):
    from app.connections import context_docs
    from app.credits.policy import set_policy
    from app.db.models import Venture
    from app.state import db
    from app.ventures import founder_facts

    founder = founder_path
    set_policy("feature_flags", {"known_check_v1": True, "experiments_autopilot_v1": True}, "test")
    with db()() as session:
        founder_facts.save(session, session.get(Venture, founder.venture),
            {"audience": founder_facts._fact(founder.audience),
             "problem": founder_facts._fact(f"{founder.audience} struggle to coordinate their work.")})
        session.commit()
    monkeypatch.setattr(context_docs, "research_context", lambda *args: {})
    response = click(founder, {"action": "experiment.list"})
    assert response.status_code == 200, response.text
    assert founder.audience in response.json()["body_md"]
    assert "First, who" not in response.json()["body_md"]
    monkeypatch.setattr(context_docs, "research_context", lambda *args: {"connected_context": {"documents": [
        {"title": "Your selected strategy", "excerpt": "Audience: " + founder.replacement}]}})
    conflict = click(founder, {"action": "experiment.list"})
    assert conflict.status_code == 200, conflict.text
    assert "conflicting values" in conflict.json()["body_md"]
    assert founder.audience in conflict.json()["body_md"] and founder.replacement in conflict.json()["body_md"]
    assert founder.other.audience not in conflict.json()["body_md"]
    assert {item.get("text") for item in conflict.json()["actions"]} == {founder.audience, founder.replacement}


def test_reasoning_progress_after_restart_reads_durable_answer(founder_path, monkeypatch):
    from app.chat import assistant
    from app.credits.policy import set_policy
    from app.db.models import Job
    from app.db.task_models import TaskRun
    from app.state import MEM, db, reset_db_factory
    from app.tasks import reasoning

    founder = founder_path
    set_policy("feature_flags", {"reasoning_v1": True}, "test")
    async def no_paid_call(*args, **kwargs):
        pytest.fail("Your queued deep question must not call a provider in the API.")
    monkeypatch.setattr(assistant, "_complete", no_paid_call)
    question = f"What are the tradeoffs in depth for {founder.idea}?"
    response = post(founder, question, idempotency_key="deep-question")
    assert response.status_code == 200, response.text
    reply = response.json()
    assert reply["kind"] == "deep_answer_pending", reply
    [progress] = reply["actions"]
    run_id = progress["payload"]["reasoning"]["run_id"]
    with db()() as session:
        run = session.get(TaskRun, run_id)
        job = session.get(Job, run.control_job_id)
        assert job.venture_id == founder.venture and job.workspace_id == founder.workspace
        assert json.loads(job.payload_json)["reasoning_v1"] is True
        journal = json.loads(run.evaluation_json)
        assert any(question in item["content"] for item in journal["messages"])
    answer = "Your saved analysis for " + founder.audience
    reasoning._save(run_id, founder.workspace, founder.venture, {**journal, "state": "complete", "last_body": answer})
    MEM.clear()
    reset_db_factory()
    forged = {**progress, "payload": {"reasoning": {**progress["payload"]["reasoning"],
        "state": "thinking", "spend_micro": 0, "cap_micro": 50_000_000}}}
    result = click(founder, forged)
    assert result.status_code == 200, result.text
    assert result.json()["kind"] == "deep_answer", result.json()
    assert answer in result.json()["body_md"] and "AI spend:" in result.json()["body_md"]
    assert result.json()["actions"][0]["payload"]["reasoning"]["state"] == "complete"
    assert result.json()["actions"][0]["payload"]["reasoning"]["cap_micro"] == 5_000_000
    foreign = click(founder.other, progress)
    assert foreign.status_code in {403, 404, 409}, foreign.text
    assert answer not in foreign.text
    path = founder.path + "/conversations"
    other_chat = founder.client.post(path, json={"idempotency_key": "other-reasoning-chat"},
                                     headers=headers(path, "POST", founder.subject))
    assert other_chat.status_code == 200, other_chat.text
    unrelated = click(founder, progress, conversation_id=other_chat.json()["id"])
    assert unrelated.status_code == 403 and answer not in unrelated.text, unrelated.text
    invalid = click(founder, {"action": "status", "payload": {"reasoning": {}}})
    assert invalid.status_code == 403, invalid.text
    set_policy("feature_flags", {}, "test")
    off = click(founder, progress)
    assert off.status_code == 200 and off.json()["kind"] != "deep_answer", off.text
    assert answer not in off.text


def test_standing_approval_exact_card_and_revoke_button(founder_path, monkeypatch):
    from app.api import chat_api
    from app.chat import execute
    from app.credits.policy import set_policy

    founder = founder_path
    set_policy("feature_flags", {"approvals_policy_v1": True}, "test")
    state = {"query": founder.idea, "ceiling_micro": 5, "task_allowance_micro": 10}
    card = saved_card(founder, "research.start", state)
    path = f"/workspaces/{founder.workspace}/approvals"
    grant = founder.client.post(path, json={"kind": "scope_grant", "subject_ref": card["id"],
        "summary": "Your confirmation choice", "requested_by": founder.subject,
        "detail": {"standing_policy_v1": {"choice": "always", "venture_id": founder.venture,
            "message_id": card["id"], "action": "research.start"}}},
        headers=headers(path, "POST", founder.subject))
    assert grant.status_code == 201, grant.text
    calls = []
    monkeypatch.setattr(execute, "_research_start", lambda *args, **kwargs: calls.append(args) or {
        "kind": "text", "body_md": "Your research is queued.", "action_started": True, "run_id": "job_test"})
    current_state = dict(state)
    async def prepare(venture_id, text, **kwargs):
        return saved_card(founder, "research.start", current_state)
    monkeypatch.setattr(chat_api, "handle_message", prepare)
    current_state["query"] = founder.replacement
    changed = post(founder, "Research my revised audience.")
    assert changed.status_code == 200 and changed.json()["kind"] == "confirmation", changed.text
    assert not calls
    current_state = dict(state)
    response = post(founder, "Research my saved audience.")
    assert response.status_code == 200, response.text
    assert "under your standing approval" in response.json()["body_md"] and len(calls) == 1
    revoke = next(item for item in response.json()["actions"] if item.get("action") == "approvals.revoke")
    assert revoke["payload"]["approval_id"] == grant.json()["id"]
    foreign = click(founder.other, revoke)
    assert foreign.status_code == 400, foreign.text
    assert foreign.json()["detail"]["error"]["message"] == "That approval is not available to you."
    revoked = click(founder, revoke)
    assert revoked.status_code == 200 and "Your standing approval is revoked" in revoked.json()["body_md"], revoked.text
    after = post(founder, "Research my saved audience again.")
    assert after.status_code == 200 and after.json()["kind"] == "confirmation", after.text
    assert len(calls) == 1


@pytest.mark.parametrize("decision", ["raise", "stop"])
def test_spend_cap_card_raise_and_stop_leave_no_paid_work(founder_path, monkeypatch, decision):
    from app.credits import spend
    from app.credits.policy import set_policy
    from app.db.models import Job
    from app.db.supymem_models import Experiment
    from app.experiments import autonomous, ledger
    from app.state import db
    from core.clock import utcnow

    founder = founder_path
    set_policy("feature_flags", {"spend_view_v1": True}, "test")
    experiment_id = "exp_founder_cap"
    with db()() as session:
        session.add(Experiment(id=experiment_id, workspace_id=founder.workspace, venture_id=founder.venture,
            kind=ledger.KIND, hypothesis=founder.idea, state="running", started_at=utcnow(),
            context=json.dumps({"runner": "system", "spend_cap_micro": 500_000, "spec": {"title": founder.idea}})))
        session.commit()
        jobs_before = list(session.scalars(select(Job.id)))
    spec = {"id": experiment_id, "workspace_id": founder.workspace, "venture_id": founder.venture,
        "title": founder.idea, "hypothesis": founder.idea, "fingerprint": "saved-scope", "ceiling_micro": 1_000_000}
    monkeypatch.setattr(autonomous, "available", lambda venture_id: [spec] if venture_id == founder.venture else [])
    card = saved_card(founder, "experiment.run", {"experiment_id": experiment_id,
        "fingerprint": spec["fingerprint"], "ceiling_micro": spec["ceiling_micro"]})
    capped = click(founder, card["actions"][0], message_id=card["id"])
    assert capped.status_code == 200 and capped.json()["kind"] == "confirmation", capped.text
    assert "Change the limit or stop" in capped.json()["body_md"]
    assert spend.view(founder.workspace, founder.venture)["experiments"][0]["status"] == "paused_at_cap"
    result = click(founder, capped.json()["actions"][0 if decision == "raise" else 1], message_id=capped.json()["id"])
    assert result.status_code == 200, result.text
    assert ("Your experiment setting is saved" if decision == "raise" else "You stopped this experiment") in result.json()["body_md"]
    assert spend.view(founder.workspace, founder.venture)["experiments"][0]["status"] == (
        "running" if decision == "raise" else "stopped")
    assert not spend.view(founder.other.workspace, founder.other.venture)["experiments"]
    with db()() as session:
        assert list(session.scalars(select(Job.id))) == jobs_before


def test_chief_of_staff_brief_is_private_chat_with_actionable_next_move(founder_path):
    from app.credits.policy import set_policy
    from app.inbox import store, weekly
    from app.state import db

    founder = founder_path
    set_policy("feature_flags", {"cos_brief_v1": True}, "test")
    store.ensure_schema(db().kw["bind"])
    store.update_settings(founder.workspace, {"timezone": "UTC"})
    now = datetime(2026, 10, 26, 8, 30, tzinfo=timezone.utc)
    saved = weekly.run_founder(founder.subject, founder.workspace, founder.venture, now=now)
    assert saved is not None
    founder.conversation = saved["conversation_id"]
    items = history(founder)
    [brief] = [item for item in items if item["kind"] == "cos_brief"]
    payload = next(citation["cos_brief"] for citation in brief["citations"] if "cos_brief" in citation)
    assert payload["week_of"] == "2026-10-26"
    assert len(payload["sections"]) == 3 and all(not section["items"] for section in payload["sections"])
    assert "\u2014" not in json.dumps(payload, ensure_ascii=False)
    assert brief["actions"] and all(item["action"] != "journey.skip_step" for item in brief["actions"])
    action = brief["actions"][0]
    assert payload["next_move"] == action
    result = click(founder, action)
    assert result.status_code == 200 and result.json()["body_md"], result.text
    assert weekly.run_founder(founder.subject, founder.workspace, founder.venture, now=now) is None
    path = founder.path
    foreign = founder.client.get(path, params={"conversation_id": founder.conversation},
                                 headers=headers(path, subject=founder.other.subject))
    assert foreign.status_code == 404, foreign.text


def test_scale_admission_replays_after_restart_without_duplicate_memory(founder_path):
    from app.db.models import InstitutionalMemory
    from app.state import MEM, db, reset_db_factory

    founder = founder_path
    message = f"Remember: Our audience is {founder.audience}."
    first = post(founder, message, idempotency_key="shared-nonce")
    assert first.status_code == 200 and first.json()["kind"] == "memory_saved", first.text
    MEM.clear()
    reset_db_factory()
    replay = post(founder, message, idempotency_key="shared-nonce")
    assert replay.status_code == 200 and replay.json() == first.json(), replay.text
    changed = post(founder, f"Remember: Our audience is {founder.replacement}.", idempotency_key="shared-nonce")
    assert changed.status_code == 409, changed.text
    other = post(founder.other, f"Remember: Our audience is {founder.other.audience}.", idempotency_key="shared-nonce")
    assert other.status_code == 200 and other.json()["kind"] == "memory_saved", other.text
    assert other.json()["id"] != first.json()["id"]
    with db()() as session:
        assert len(list(session.scalars(select(InstitutionalMemory).where(
            InstitutionalMemory.venture_id == founder.venture)))) == 1


def test_scale_admission_duplicate_in_flight_chat_does_not_repeat_work(founder_path, monkeypatch):
    from app.api import chat_api

    founder = founder_path
    entered, release = Event(), Event()
    calls = []
    original = chat_api.handle_message
    async def held(*args, **kwargs):
        calls.append(args)
        entered.set()
        assert await asyncio.to_thread(release.wait, 30)
        return await original(*args, **kwargs)
    monkeypatch.setattr(chat_api, "handle_message", held)
    message = f"Remember: Our audience is {founder.audience}."
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(post, founder, message, idempotency_key="in-flight")
        try:
            assert entered.wait(30)
            duplicate = post(founder, message, idempotency_key="in-flight")
            assert duplicate.status_code == 409, duplicate.text
            assert duplicate.json()["detail"]["error"]["code"] == "chat_request_in_progress"
            assert len(calls) == 1
        finally:
            release.set()
        first = pending.result(timeout=30)
    assert first.status_code == 200 and first.json()["kind"] == "memory_saved", first.text
    replay = post(founder, message, idempotency_key="in-flight")
    assert replay.status_code == 200 and replay.json() == first.json(), replay.text
    assert len(calls) == 1


def test_typed_and_voice_memory_commands_have_same_reply_and_sources(founder_path):
    founder = founder_path
    replies = []
    for source in ("typed", "voice"):
        saved = post(founder, f"Remember: Our audience is {founder.audience}.", source=source)
        assert saved.status_code == 200 and saved.json()["kind"] == "memory_saved", saved.text
        recall = post(founder, "Who is my audience?", source=source)
        assert recall.status_code == 200 and recall.json()["kind"] == "memory_recall", recall.text
        replies.append(recall.json())
    assert replies[0]["body_md"] == replies[1]["body_md"]
    assert replies[0]["actions"] == replies[1]["actions"]
    assert all(item["source_ref"].startswith("chat:") for reply in replies for item in reply["citations"])


def test_voice_cannot_confirm_work_but_typed_card_tap_can(founder_path, monkeypatch):
    from app.chat import execute

    founder = founder_path
    card = saved_card(founder, "research.start", {"query": founder.idea})
    calls = []
    monkeypatch.setattr(execute, "_research_start", lambda *args, **kwargs: calls.append(args) or {
        "kind": "text", "body_md": "Your research is queued.", "action_started": True, "run_id": "job_voice_test"})
    voice = click(founder, card["actions"][0], message_id=card["id"], source="voice")
    assert voice.status_code == 403 and voice.json()["detail"]["error"]["code"] == "voice_cannot_confirm", voice.text
    assert not calls
    typed = click(founder, card["actions"][0], message_id=card["id"], source="typed")
    assert typed.status_code == 200 and "Your research is queued" in typed.json()["body_md"], typed.text
    assert len(calls) == 1
    replay = click(founder, card["actions"][0], message_id=card["id"], source="typed")
    assert replay.status_code == 200 and replay.json() == typed.json(), replay.text
    assert len(calls) == 1

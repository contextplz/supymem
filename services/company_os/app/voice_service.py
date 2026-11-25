"""Owned voice receipts, replay fencing and private transcript persistence."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta
from types import SimpleNamespace

from fastapi import HTTPException, Request

from app.authz.workspace import assert_venture_access, subject_person_id
from app.core.local_mode import is_local
from app.db.models import AnalyticsEvent, CreditReservation, IdempotencyRecord, Venture
from app.db.session import serialize_writes
from app.state import db
from app.ventures.current_idea import current_idea
from core.clock import as_utc

def _session_receipt(session, event_id):
    if not is_local():
        return session.get(AnalyticsEvent, event_id)
    row = session.get(IdempotencyRecord, "voice-session:" + event_id)
    if row is None or row.scope != "voice-session":
        return None
    try:
        custody = json.loads(row.response_json)
        return SimpleNamespace(event_name="voice.session_started", person_id=custody["person_id"],
            workspace_id=custody["workspace_id"], venture_id=custody["venture_id"], occurred_at=row.created_at)
    except (KeyError, TypeError, ValueError):
        return None


def _unavailable_session():
    detail = {"error": {"code": "voice_session_unavailable",
        "message": "Your voice session is no longer available. Start a new call or keep typing."}} if is_local() else "not_found"
    return HTTPException(404, detail=detail)


def _reserve_tool(body, name: str, args: dict, person: str, venture_id: str, workspace_id: str, *, now, wall_s, steps, partial):
    """Durable reservations prevent process restarts/replays from repeating an unknown call."""
    scope = f"voice-tools:{body.session_event_id}"
    key = scope + ":call:" + hashlib.sha256(body.call_id.encode()).hexdigest()
    turn_key = scope + ":turn:" + hashlib.sha256(body.turn_id.encode()).hexdigest()
    fingerprint = hashlib.sha256(json.dumps([name, args, body.turn_id, body.conversation_id], sort_keys=True).encode()).hexdigest()
    now = now.replace(tzinfo=None)
    with db()() as session:
        serialize_writes(session, key=scope)
        receipt = _session_receipt(session, body.session_event_id)
        if (receipt is None or receipt.event_name != "voice.session_started"
                or receipt.person_id != person or receipt.venture_id != venture_id
                or receipt.workspace_id != workspace_id
                or receipt.occurred_at.replace(tzinfo=None) < now - timedelta(minutes=10)):
            raise _unavailable_session()
        call = session.get(IdempotencyRecord, key)
        if call:
            saved = json.loads(call.response_json)
            if saved["fingerprint"] != fingerprint:
                raise HTTPException(409, detail={"error": {"code": "voice_tool_call_conflict"}})
            return key, 0, 0, saved.get("reply") or {"body_md": "I'm still working on it. Do not repeat it; answer only from completed results.", "partial": True}
        turn = session.get(IdempotencyRecord, turn_key)
        state = json.loads(turn.response_json) if turn else {"steps": 0, "deadline": (now + timedelta(seconds=wall_s)).isoformat()}
        remaining = (datetime.fromisoformat(state["deadline"]) - now).total_seconds()
        if state["steps"] >= steps or remaining <= 0 or state.get("search_outcome_unknown"):
            return key, 0, 0, {"body_md": partial, "partial": True}
        from app.chat.agent import TURN_BUDGET_MICRO
        from app.credits.policy import web_search_credits
        from app.credits.units import credits_to_micro

        budget = max(0, TURN_BUDGET_MICRO - state.get("reserved_micro", 0))
        # Reserve before dispatch: an unknown paid result cannot spend this allowance again.
        reserved = min(budget, credits_to_micro(web_search_credits())) if name == "web_search" else 0
        state["reserved_micro"] = state.get("reserved_micro", 0) + reserved
        if name == "web_search":
            state["search_outcome_unknown"] = True
        state["steps"] += 1
        if turn:
            turn.response_json = json.dumps(state)
        else:
            session.add(IdempotencyRecord(key=turn_key, scope=scope, response_json=json.dumps(state), created_at=now))
        session.add(IdempotencyRecord(key=key, scope=scope, response_json=json.dumps({"fingerprint": fingerprint,
            "turn_key": turn_key, "reserved_micro": reserved, "search": name == "web_search"}), created_at=now))
        session.commit()
    return key, remaining, budget, None


def _save_tool_result(key: str, reply: dict, spent_micro: int, outcome_unknown: bool):
    with db()() as session:
        row = session.get(IdempotencyRecord, key)
        serialize_writes(session, key=row.scope)
        state = json.loads(row.response_json)
        if state["search"]:
            turn = session.get(IdempotencyRecord, state["turn_key"])
            turn_state = json.loads(turn.response_json)
            turn_state["search_outcome_unknown"] = outcome_unknown
            if not outcome_unknown:
                turn_state["reserved_micro"] -= max(0, state["reserved_micro"] - spent_micro)
            turn.response_json = json.dumps(turn_state)
        row.response_json = json.dumps({**state, "reply": reply})
        session.commit()


def _start_session(person, workspace_id, venture_id, build, idempotency_key, body, *, mint, clock):
    if body and body.idempotency_key is not None:
        if idempotency_key is not None and idempotency_key != body.idempotency_key:
            raise HTTPException(409, detail={"error": {"code": "voice_start_conflict",
                "message": "Retry your original start with the same key, or begin a new call."}})
        idempotency_key = body.idempotency_key
    if idempotency_key is None:
        return mint(person, workspace_id, venture_id, build)
    from app.connectors.secrets import decrypt_at_rest, encrypt_at_rest

    scope = "voice-start:" + hashlib.sha256(json.dumps([person, workspace_id, venture_id]).encode()).hexdigest()
    key = scope + ":" + hashlib.sha256(idempotency_key.encode()).hexdigest()
    session_id = "vsn_" + hashlib.sha256(key.encode()).hexdigest()
    fingerprint = hashlib.sha256(json.dumps(body.model_dump(exclude={"idempotency_key"}), sort_keys=True).encode()).hexdigest()
    with db()() as session:
        serialize_writes(session, key=scope)
        existing = session.get(IdempotencyRecord, key)
        if existing:
            saved = json.loads(existing.response_json)
            if saved["fingerprint"] != fingerprint:
                raise HTTPException(409, detail={"error": {"code": "voice_start_conflict",
                    "message": "This start does not match the original. Retry the original start or begin a new call."}})
            if "response" not in saved:
                raise HTTPException(409, detail={"error": {"code": "voice_start_pending",
                    "message": "Your call is still starting. Retry this start or type instead."}})
            response = json.loads(decrypt_at_rest(saved["response"]))
            expires_at = response.get("expires_at")
            if expires_at is None or expires_at <= clock().timestamp():
                raise HTTPException(409, detail={"error": {"code": "voice_start_expired",
                    "message": "That start has expired. Start a new call; this retry does not charge you."}})
            return response
        from sqlalchemy import select

        if session.scalar(select(CreditReservation.id).where(
                CreditReservation.request_id == session_id, CreditReservation.status != "released")):
            raise HTTPException(409, detail={"error": {"code": "voice_start_expired",
                "message": "That start cannot be repeated. Start a new call or type instead; this retry does not charge you."}})
        session.add(IdempotencyRecord(key=key, scope=scope,
            response_json=json.dumps({"fingerprint": fingerprint}), created_at=clock().replace(tzinfo=None)))
        session.commit()
    try:
        response = mint(person, workspace_id, venture_id, build, session_id=session_id)
    except HTTPException:
        with db()() as session:
            serialize_writes(session, key=scope)
            existing = session.get(IdempotencyRecord, key)
            session.delete(existing)
            session.commit()
        raise
    with db()() as session:
        serialize_writes(session, key=scope)
        existing = session.get(IdempotencyRecord, key)
        existing.response_json = json.dumps({"fingerprint": fingerprint,
            "response": encrypt_at_rest(json.dumps(response))})
        session.commit()
    return response


def _capture_voice_metric(body, request: Request, venture_id: str | None, *, clock):
    from app.admin.authn import verify_caller
    from app.analytics.usage import capture

    caller = verify_caller(request, required=True)
    person = subject_person_id(caller.sub)
    with db()() as session:
        started = _session_receipt(session, body.session_event_id)
        if (started is None or started.event_name != "voice.session_started"
                or started.person_id != person or started.venture_id != venture_id
                or started.occurred_at.replace(tzinfo=None) < clock().replace(tzinfo=None) - timedelta(minutes=20)):
            raise _unavailable_session()
        workspace_id = started.workspace_id
    if is_local():
        return {"ok": True}
    # Reuse authenticated analytics access checks and nonce deduplication. Bind the event to the
    # server's session receipt, so an actor cannot attach metrics to somebody else's call.
    return capture({"event": "voice.metric", "event_id": str(body.event_id),
                    "workspace_id": workspace_id, "venture_id": venture_id,
                    "props": body.model_dump(mode="json", exclude={"event_id"}, exclude_none=True)}, caller.sub)


def _entry_note_scope(person: str, target_venture_id: str | None = None) -> str:
    from app.surfaces.idempotency import ENTRY_VOICE_NOTE_SCOPE_PREFIX
    return (ENTRY_VOICE_NOTE_SCOPE_PREFIX + hashlib.sha256(person.encode()).hexdigest()
            + ":" + hashlib.sha256((target_venture_id or "").encode()).hexdigest())


def _owned_entry_receipt(session, event_id: str, person: str):
    receipt = _session_receipt(session, event_id)
    if (receipt is None or receipt.event_name != "voice.session_started"
            or (receipt.person_id, receipt.workspace_id, receipt.venture_id) != (person, None, None)):
        raise _unavailable_session()
    return receipt


def _assert_entry_note_target(session, target_venture_id: str, person: str) -> str:
    from app.db.models import Workspace
    _, workspace_id = assert_venture_access(target_venture_id, person)
    venture = session.get(Venture, target_venture_id)
    workspace = session.get(Workspace, workspace_id)
    if (venture is None or venture.archived_at is not None or workspace is None
            or workspace.archived_at is not None):
        raise HTTPException(404, "not_found")
    return workspace_id


def _entry_note_public(session, state: dict, *, read_only: bool = False) -> dict:
    from sqlalchemy import select
    from app.db.supymem_models import EntryChatProposal
    founders = [turn for turn in state["turns"] if turn["role"] == "founder"]
    keys = {f"voice_{state['session_event_id']}_{turn['id']}": turn for turn in founders if turn["final"]}
    proposals = session.scalars(select(EntryChatProposal).where(
        EntryChatProposal.actor_id == state["actor_id"], EntryChatProposal.request_id.in_(keys),
    )).all()
    matched = {}
    target = state["note"]["target_venture_id"]
    for row in proposals:
        if not row.text.strip().endswith(keys[row.request_id]["text"].strip()):
            continue
        if row.target_venture_id != target:
            # A new venture gains a target during commit. Only its retained creation
            # receipt proves that it began on the same untargeted start screen.
            if target is not None or row.state not in {"committing", "committed"}:
                continue
            try:
                receipt = json.loads(row.receipt_json or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(receipt, dict) or receipt.get("created_new") is not True:
                continue
            if row.state == "committed" and (receipt.get("status"), receipt.get("venture_id")) != ("committed", row.target_venture_id):
                continue
        if row.target_venture_id:
            try:
                workspace_id = _assert_entry_note_target(session, row.target_venture_id, state["actor_id"])
            except HTTPException:
                continue
            if target is None and row.state == "committed" and receipt.get("workspace_id") != workspace_id:
                continue
        matched[row.request_id] = row
    pending = any(row.state in {"classifying", "committing"} for row in matched.values())
    needs_edit = any(not turn["final"] for turn in founders) or any(
        key not in matched or matched[key].state not in {"pending", "clarify", "committed", "classifying", "committing"}
        for key in keys)
    return {**state["note"], "pending": pending, "needs_edit": needs_edit, "read_only": read_only}


def entry_voice_summary(body, request: Request, *, clock, has_speech, note_body):
    from app.admin.authn import verify_caller
    from app.secrets.detect import looks_like_secret, withheld_user_copy
    from core.copy import strip_emdash

    caller = verify_caller(request, required=True)
    person = subject_person_id(caller.sub)
    if body.conversation_id is not None:
        raise HTTPException(404, "not_found")
    key = "entry-voice-note_" + hashlib.sha256(json.dumps([person, body.session_event_id], separators=(",", ":")).encode()).hexdigest()
    fingerprint = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
    scope = _entry_note_scope(person, body.target_venture_id)
    with db()() as session:
        if body.target_venture_id:
            _assert_entry_note_target(session, body.target_venture_id, person)
        serialize_writes(session, key=key)
        existing = session.get(IdempotencyRecord, key)
        if existing:
            try:
                state = json.loads(existing.response_json)
            except (TypeError, ValueError):
                raise HTTPException(409, detail={"error": {"code": "voice_summary_conflict"}}) from None
            if (not isinstance(state, dict) or state.get("schema_version") != 1 or existing.scope != scope
                    or state.get("actor_id") != person or state.get("session_event_id") != body.session_event_id
                    or state.get("fingerprint") != fingerprint or not isinstance(state.get("note"), dict)
                    or state["note"].get("id") != key or state["note"].get("target_venture_id") != body.target_venture_id):
                raise HTTPException(409, detail={"error": {"code": "voice_summary_conflict"}})
            receipt = _session_receipt(session, body.session_event_id)
            if receipt is not None:
                receipt = _owned_entry_receipt(session, body.session_event_id, person)
            if not has_speech(body.turns):
                return {"ignored": True}
            return _entry_note_public(session, state,
                read_only=receipt is None or as_utc(receipt.occurred_at) < clock() - timedelta(minutes=20))
        receipt = _owned_entry_receipt(session, body.session_event_id, person)
        if as_utc(receipt.occurred_at) < clock() - timedelta(minutes=20):
            raise _unavailable_session()
        if not has_speech(body.turns):
            return {"ignored": True}
        turns = [turn.model_dump() for turn in body.turns]
        for turn in turns:
            if looks_like_secret(turn["text"]):
                turn["text"] = strip_emdash(withheld_user_copy())
        text = note_body(turns)
        note = {"id": key, "body_md": text, "created_at": clock().isoformat(),
                "started_at": as_utc(receipt.occurred_at).isoformat(), "target_venture_id": body.target_venture_id}
        state = {"schema_version": 1, "actor_id": person, "session_event_id": body.session_event_id,
                 "fingerprint": fingerprint, "turns": turns, "note": note}
        session.add(IdempotencyRecord(key=key, scope=scope, response_json=json.dumps(state), created_at=clock()))
        session.commit()
        return _entry_note_public(session, state)


def entry_voice_notes(person: str, target_venture_id: str | None = None, *, clock) -> list[dict]:
    from sqlalchemy import select
    from app.db.supymem_models import EntryChatProposal
    from app.entry.service import _assert_entry_actor
    _assert_entry_actor(person)
    with db()() as session:
        if target_venture_id:
            _assert_entry_note_target(session, target_venture_id, person)
        boundary = session.scalar(select(EntryChatProposal.updated_at).where(
            EntryChatProposal.actor_id == person, EntryChatProposal.state == "committed",
        ).order_by(EntryChatProposal.updated_at.desc()).limit(1))
        scope = _entry_note_scope(person, target_venture_id)
        query = select(IdempotencyRecord).where(
            IdempotencyRecord.scope == scope if target_venture_id else
            IdempotencyRecord.scope.startswith(scope.rsplit(":", 1)[0] + ":"))
        rows = session.scalars(query.order_by(IdempotencyRecord.created_at.desc()).limit(100)).all()
        notes = []
        for row in reversed(rows):
            try:
                state = json.loads(row.response_json)
                if not isinstance(state, dict) or state.get("schema_version") != 1 or state.get("actor_id") != person or state["note"]["id"] != row.key:
                    continue
                target = state["note"]["target_venture_id"]
                if row.scope != _entry_note_scope(person, target) or (target_venture_id and target != target_venture_id):
                    continue
                if target:
                    _assert_entry_note_target(session, target, person)
                receipt = _session_receipt(session, state["session_event_id"])
                read_only = (receipt is None or receipt.event_name != "voice.session_started"
                    or (receipt.person_id, receipt.workspace_id, receipt.venture_id) != (person, None, None)
                    or as_utc(receipt.occurred_at) < clock() - timedelta(minutes=20))
                note = _entry_note_public(session, state, read_only=read_only)
                started = as_utc(datetime.fromisoformat(note["started_at"]))
                earlier = not target_venture_id and (note["target_venture_id"] is not None or (boundary and started <= as_utc(boundary)))
                notes.append({**note, "partition": "earlier" if earlier else "fresh"})
            except (TypeError, ValueError, KeyError, HTTPException):
                continue  # Malformed private records never become another actor's history.
        return notes


def _unconfirmed_chat_in_conversation(venture_id: str, workspace_id: str, person_id: str) -> bool:
    from app.chat.requests import unconfirmed_in_conversation

    return unconfirmed_in_conversation(venture_id, workspace_id, person_id)


def _completed_voice_turns(venture_id: str, workspace_id: str, person_id: str,
                         session_event_id: str, turns: list) -> set[str] | None:
    from sqlalchemy import select
    from app.chat.conversations import resolve
    from app.chat.requests import _json
    from app.db.supymem_models import EntryChatProposal, FounderRequest

    conversation = resolve(venture_id)
    identities = {"frq_" + hashlib.sha256(_json([workspace_id, venture_id, person_id, conversation.id,
        f"voice_{session_event_id}_{turn.id}"]).encode()).hexdigest(): turn.id for turn in turns if turn.final}
    completed = set()
    with db()() as session:
        rows = session.scalars(select(FounderRequest).where(
            FounderRequest.id.in_(identities), FounderRequest.workspace_id == workspace_id,
            FounderRequest.venture_id == venture_id, FounderRequest.source == "chat_turn_v1",
            FounderRequest.outcome == "completed",
        )).all()
        for row in rows:
            try:
                value = json.loads(row.text)
            except (TypeError, ValueError):
                return None
            if (not isinstance(value, dict) or value.get("version") != 1
                    or (value.get("actor_id"), value.get("conversation_id")) != (person_id, conversation.id)
                    or not isinstance(value.get("response"), dict)):
                return None  # An unreadable later receipt cannot authorize older speech.
            completed.add(identities[row.id])
        entry_requests = {f"voice_{session_event_id}_{turn.id}": turn.id for turn in turns if turn.final}
        entries = session.scalars(select(EntryChatProposal).where(
            EntryChatProposal.actor_id == person_id, EntryChatProposal.target_venture_id == venture_id,
            EntryChatProposal.request_id.in_(entry_requests),
        )).all()
        for entry in entries:
            if entry.state not in {"pending", "clarify", "committing", "committed", "cancelled"}:
                return None
            completed.add(entry_requests[entry.request_id])
    return completed


async def voice_summary(venture_id: str, body, request: Request, *, clock, has_speech, note_body):
    from app.admin.authn import verify_caller
    from app.api.chat_api import ChatBody, post_chat
    from app.chat.conversations import bind_conversation
    from app.chat.store import append_message
    from app.tenancy import bind

    caller = verify_caller(request, required=True)
    person = subject_person_id(caller.sub)
    _, workspace_id = assert_venture_access(venture_id, caller.sub)
    with db()() as session:
        receipt = _session_receipt(session, body.session_event_id)
        if (receipt is None or receipt.event_name != "voice.session_started"
                or (receipt.person_id, receipt.venture_id, receipt.workspace_id) != (person, venture_id, workspace_id)
                or receipt.occurred_at.replace(tzinfo=None) < clock().replace(tzinfo=None) - timedelta(minutes=20)):
            raise _unavailable_session()
    founders = [turn for turn in body.turns if turn.role == "founder"]
    if not has_speech(body.turns):
        return {"ignored": True}
    # The complete dialogue stays context; only final founder words enter normal chat.
    text = note_body([turn.model_dump() for turn in body.turns])
    key = "voice-summary:" + body.session_event_id
    fingerprint = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
    with bind(workspace_id=workspace_id, venture_id=venture_id, actor=caller.sub):
        try:
            with bind_conversation(venture_id, body.conversation_id):
                with db()() as session:
                    serialize_writes(session, key=key)
                    row = session.get(IdempotencyRecord, key)
                    state = json.loads(row.response_json) if row else {"fingerprint": fingerprint, "done": []}
                    if state["fingerprint"] != fingerprint:
                        raise HTTPException(409, detail={"error": {"code": "voice_summary_conflict"}})
                    if row is None:
                        session.add(IdempotencyRecord(key=key, scope=key, response_json=json.dumps(state), created_at=clock()))
                        session.commit()
                saved = append_message(venture_id, role="assistant", kind="voice_summary", body_md=text, idempotency_key=key)
                pending = _unconfirmed_chat_in_conversation(venture_id, workspace_id, person)
                needs_edit = any(not turn.final for turn in founders)
                provider_error = state.get("provider_error")
                applied = set(state["done"]) | {turn.id for turn in founders if turn.posted}
                if not pending:
                    completed = _completed_voice_turns(venture_id, workspace_id, person, body.session_event_id, founders)
                    if completed is None:
                        pending = True
                    else:
                        applied.update(completed)
                last_applied = max((index for index, turn in enumerate(founders) if turn.final and turn.id in applied), default=-1)
                replay_gap = not pending and any(turn.final and turn.id not in applied for turn in founders[:last_applied])
                if replay_gap:
                    needs_edit = True
                for turn in founders:
                    if pending or replay_gap or provider_error:
                        break  # Complete notes are saved; never apply older speech after a newer turn.
                    if not turn.final or turn.id in applied:
                        continue
                    if _unconfirmed_chat_in_conversation(venture_id, workspace_id, person):
                        pending = True
                        break  # A new voice session cannot replace an earlier unresolved chat nonce.
                    if len(turn.text.strip()) > 4000:
                        needs_edit = True
                        break  # Preserve the complete notes; normal chat needs a shorter answer.
                    try:
                        reply = await post_chat(venture_id, ChatBody(message=turn.text, source="voice",
                            conversation_id=body.conversation_id,
                            idempotency_key=f"voice_{body.session_event_id}_{turn.id}"), request, caller.sub)
                        provider_error = reply.get("provider_error")
                    except Exception as exc:
                        logging.getLogger(__name__).warning("voice_chat_pending", extra={"error_class": type(exc).__name__})
                        pending = True
                        # Keep the same request key. An uncertain prior action must never be redispatched.
                        break
                    with db()() as session:
                        serialize_writes(session, key=key)
                        row = session.get(IdempotencyRecord, key)
                        state = json.loads(row.response_json)
                        if turn.id not in state["done"]:
                            state["done"].append(turn.id)
                        if provider_error:
                            state["provider_error"] = provider_error
                        row.response_json = json.dumps(state)
                        session.commit()
                return {**saved, "pending": pending, "needs_edit": needs_edit, "provider_error": provider_error}
        except (ValueError, PermissionError) as exc:
            raise HTTPException(404, "not_found") from exc


def venture_context(venture_id: str) -> tuple[str, str]:
    with db()() as session:
        venture = session.get(Venture, venture_id)
        name = venture.name if venture else "this venture"
        idea = " ".join(current_idea(venture, session=session).split())[:1200] if venture else ""
    return name, idea

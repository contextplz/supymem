"""Durable, scoped chat-turn admission. An uncertain turn is never redispatched."""
from dataclasses import dataclass, replace
from datetime import timedelta
import hashlib
import json
import logging
import os

from sqlalchemy import select

from app.authz.workspace import subject_person_id
from app.chat.conversations import resolve
from app.db.models import ChatConversation, Venture, Workspace, WorkspaceMember
from app.db.session import serialize_writes
from app.db.supymem_models import FounderRequest
from app.state import db
from app.tenancy import current
from core.clock import as_utc, utcnow


log = logging.getLogger(__name__)


# Default phase budgets: llm_router 3 x 10s + 3s backoff; assistant 3 x
# (120s quota wait + 120s transport) + 3s backoff; agent 25s plus an in-flight
# assistant call. Their conservative 781s total is below 900s, not a turn-wide
# deadline: router may dispatch multiple acts and transport timeouts are per I/O.
# ponytail: age-based expiry, raise CHAT_TURN_STALE_SECONDS for longer turns.
STALE_TURN_SECONDS = int(os.environ.get("CHAT_TURN_STALE_SECONDS", "900"))


class ChatRequestError(ValueError):
    def __init__(self, code: str, status: int = 409):
        super().__init__(code)
        self.code, self.status = code, status


@dataclass(frozen=True)
class Admission:
    id: str
    workspace_id: str
    venture_id: str
    actor_id: str
    conversation_id: str
    digest: str


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _authority(session, admission):
    scope = current()
    venture = session.get(Venture, admission.venture_id)
    workspace = session.get(Workspace, admission.workspace_id)
    conversation = session.get(ChatConversation, admission.conversation_id)
    member = session.scalar(select(WorkspaceMember).where(
        WorkspaceMember.workspace_id == admission.workspace_id,
        WorkspaceMember.person_id == admission.actor_id))
    if (scope.workspace_id != admission.workspace_id or scope.venture_id != admission.venture_id
            or subject_person_id(scope.actor) != admission.actor_id or venture is None or workspace is None
            or venture.workspace_id != admission.workspace_id or venture.archived_at is not None
            or workspace.archived_at is not None
            or (workspace.owner_id != admission.actor_id and member is None)
            or conversation is None or conversation.venture_id != admission.venture_id
            or conversation.workspace_id != admission.workspace_id
            or (conversation.visibility == "private" and conversation.creator_person_id != admission.actor_id)):
        raise ChatRequestError("chat_request_scope_unavailable", 404)
    try:
        settings = json.loads(workspace.settings_json or "{}")
    except (ValueError, TypeError):
        raise ChatRequestError("chat_request_scope_unavailable", 404) from None
    if not isinstance(settings, dict) or settings.get("blocked"):
        raise ChatRequestError("chat_request_scope_unavailable", 404)


def _envelope(admission):
    return {"version": 1, "actor_id": admission.actor_id,
            "conversation_id": admission.conversation_id, "request_digest": admission.digest}


def _load(row, admission):
    if (row is None or row.source != "chat_turn_v1" or row.workspace_id != admission.workspace_id
            or row.venture_id != admission.venture_id):
        raise ChatRequestError("chat_request_unavailable")
    try:
        value = json.loads(row.text)
    except (ValueError, TypeError):
        raise ChatRequestError("chat_request_unavailable") from None
    expected = _envelope(admission)
    if not isinstance(value, dict) or set(value) not in (set(expected), set(expected) | {"response"}):
        raise ChatRequestError("chat_request_unavailable")
    if any(value.get(key) != item for key, item in expected.items()):
        raise ChatRequestError("chat_request_payload_conflict")
    return value


def _admission(venture_id: str, actor_id: str, key: str, digest: str):
    if not isinstance(key, str) or not 1 <= len(key) <= 128:
        raise ChatRequestError("chat_request_key_invalid", 422)
    conversation = resolve(venture_id)
    identity = [conversation.workspace_id, venture_id, actor_id, conversation.id, key]
    return Admission("frq_" + hashlib.sha256(_json(identity).encode()).hexdigest(),
                     conversation.workspace_id, venture_id, actor_id, conversation.id, digest)


def status(venture_id: str, key: str):
    from app.chat.conversations import actor_id

    actor = actor_id()
    if not actor:
        raise ChatRequestError("chat_actor_required", 401)
    admission = _admission(venture_id, actor, key, "")
    with db()() as session:
        _authority(session, admission)
        row = session.get(FounderRequest, admission.id)
        if row is None:
            return {"state": "unknown", "missing": True}
        try:
            value = json.loads(row.text)
            if not isinstance(value, dict) or not isinstance(value.get("request_digest"), str):
                return {"state": "unknown"}
            value = _load(row, replace(admission, digest=value["request_digest"]))
        except (ValueError, TypeError, KeyError):
            return {"state": "unknown"}
        if row.outcome in {"completed", "unknown"} and isinstance(value.get("response"), dict):
            return {"state": "done"}
        return {"state": "pending" if row.outcome == "pending" else "unknown"}


def matches(venture_id: str, actor_id: str, key: str, payload: dict) -> bool:
    """Read only: this key already ran with exactly this payload."""
    admission = _admission(venture_id, actor_id, key, hashlib.sha256(_json(payload).encode()).hexdigest())
    with db()() as session:
        _authority(session, admission)
        row = session.get(FounderRequest, admission.id)
        if row is None:
            return False
        try:
            _load(row, admission)
        except ChatRequestError:
            return False
        return True


def admit(venture_id: str, actor_id: str, key: str, payload: dict):
    """Commit the reservation before yielding any authority to the async handler."""
    admission = _admission(venture_id, actor_id, key, hashlib.sha256(_json(payload).encode()).hexdigest())
    recoverable = False
    with db()() as session:
        _authority(session, admission)
        row = session.get(FounderRequest, admission.id)
        if row is not None:
            value = _load(row, admission)
            recoverable = row.outcome == "unknown" and not isinstance(value.get("response"), dict)
    recovered = None
    if recoverable:
        from app.chat.recovery import recover_request_reply

        recovered = recover_request_reply(admission)
    with db()() as session:
        serialize_writes(session, key="chat-request:" + admission.id)
        _authority(session, admission)
        row = session.get(FounderRequest, admission.id)
        if row is not None:
            value = _load(row, admission)
            if row.outcome == "completed" and isinstance(value.get("response"), dict):
                return admission, value["response"]
            if row.outcome == "unknown":
                response = value.get("response") or recovered
                if isinstance(response, dict):
                    response, encoded = _response(admission, response)
                    _authority(session, admission)
                    row.text, row.outcome, row.failure_reason, row.resolved_at = encoded, "completed", None, utcnow()
                    session.commit()
                    return admission, response
            code = "chat_request_in_progress" if row.outcome == "pending" else "chat_request_outcome_unknown"
            raise ChatRequestError(code)
        session.add(FounderRequest(id=admission.id, workspace_id=admission.workspace_id,
            venture_id=venture_id, source="chat_turn_v1", text=_json(_envelope(admission)),
            intent="chat.turn", intent_category="other", classification="chat_turn",
            outcome="pending", created_at=utcnow()))
        session.commit()
    return admission, None


def _response(admission: Admission, response: dict):
    if not isinstance(response, dict) or response.get("conversation_id") not in (None, admission.conversation_id):
        raise ChatRequestError("chat_request_reply_unavailable")
    # Projection is server-qualified, including legitimate legacy rows with null cid.
    response = {**response, "conversation_id": admission.conversation_id}
    value = {**_envelope(admission), "response": response}
    encoded = _json(value)
    if len(encoded.encode()) > 131072:
        raise ChatRequestError("chat_request_reply_unavailable")
    return response, encoded


def complete(admission: Admission, response: dict):
    response, encoded = _response(admission, response)
    with db()() as session:
        serialize_writes(session, key="chat-request:" + admission.id)
        _authority(session, admission)
        row = session.get(FounderRequest, admission.id, with_for_update=True)
        _load(row, admission)
        if row.outcome != "pending":
            log.error("chat_turn_unconfirmed", extra={"reason_code": "chat_reply_after_lease_loss",
                "venture_id": admission.venture_id, "chat_request_id": admission.id})
            raise ChatRequestError("chat_request_outcome_unknown")
        row.text, row.outcome, row.resolved_at = encoded, "completed", utcnow()
        session.commit()
    return response


def uncertain(admission: Admission, response: dict | None = None):
    """Cleanup retains the hold even when the caller's access has been revoked."""
    encoded = None
    if response is not None:
        try:
            _, encoded = _response(admission, response)
        except (ValueError, TypeError):
            pass
    with db()() as session:
        serialize_writes(session, key="chat-request:" + admission.id)
        row = session.get(FounderRequest, admission.id, with_for_update=True)
        _load(row, admission)
        if row.outcome == "pending":
            if encoded is not None:
                try:
                    _authority(session, admission)
                except ChatRequestError:
                    pass
                else:
                    row.text = encoded
            row.outcome, row.failure_reason = "unknown", "chat_turn_unconfirmed"
            session.commit()


def reconcile_stale_turns(*, now=None, limit=200) -> int:
    """Release abandoned admissions to Start fresh, never redispatch their work."""
    if limit <= 0:
        return 0
    cutoff = as_utc(now or utcnow()) - timedelta(seconds=STALE_TURN_SECONDS)
    with db()() as session:
        if session.get_bind().dialect.name == "sqlite":
            serialize_writes(session)
        rows = session.scalars(select(FounderRequest).where(
            FounderRequest.source == "chat_turn_v1", FounderRequest.outcome == "pending",
            FounderRequest.created_at < cutoff,
        ).order_by(FounderRequest.created_at, FounderRequest.id).limit(limit)
            .with_for_update(skip_locked=True)).all()
        for row in rows:
            row.outcome, row.failure_reason = "unknown", "chat_turn_unconfirmed"
        abandoned = [(row.venture_id, row.id) for row in rows]
        session.commit()
        for venture_id, request_id in abandoned:
            log.error("chat_turn_unconfirmed", extra={"reason_code": "chat_turn_abandoned",
                "venture_id": venture_id, "chat_request_id": request_id})
        return len(rows)


def unconfirmed_in_conversation(venture_id: str, workspace_id: str, person_id: str) -> bool:
    conversation = resolve(venture_id)
    cutoff = utcnow() - timedelta(seconds=STALE_TURN_SECONDS)
    with db()() as session:
        rows = session.scalars(select(FounderRequest).where(
            FounderRequest.workspace_id == workspace_id, FounderRequest.venture_id == venture_id,
            FounderRequest.source == "chat_turn_v1", FounderRequest.outcome.in_(("pending", "unknown")),
        )).all()
        for row in rows:
            try:
                value = json.loads(row.text)
            except (TypeError, ValueError):
                return True
            if isinstance(value, dict) and (value.get("actor_id"), value.get("conversation_id")) == (person_id, conversation.id):
                if row.outcome == "pending" or as_utc(row.created_at) >= cutoff:
                    return True
    return False

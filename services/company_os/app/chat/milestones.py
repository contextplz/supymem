"""Proactive chat posts: when a journey milestone ends, chat says so once, with one next step."""

from __future__ import annotations

from app.chat.navigation_map import in_chat

import hashlib
import json
import re
from dataclasses import replace
from datetime import timedelta

from sqlalchemy import event, func, select
from sqlalchemy.exc import IntegrityError

from app.chat.store import _clean
from app.core.local_mode import is_local
from app.org.characters import stamp_speaker
from app.org.manager import manager_speaker
from app.db.models import ActivityLine, ChatMessage, Venture
from app.db.session import serialize_writes
from app.state import db
from core.clock import utcnow
from core.ids import new_id

DAILY_MESSAGE_LIMIT = 14
# Internal ids (msg_, job_, rsd_, vlr_ ...) and absolute filesystem paths never reach a founder.
_ID = re.compile(r"\b[a-z]{2,6}_[0-9a-z]{12,32}\b")
_PATH = re.compile(r"~/\S+|(?<![\w/:.])/(?:[\w.\-]+/)+[\w.\-]*")


def scrub(text: str) -> str:
    return _clean(_PATH.sub("", _ID.sub("", text or "")))


def _scoped(venture_id: str, key: str) -> str:
    from app.chat.conversations import current_conversation
    scope = current_conversation()
    identity = [venture_id, "assistant", key]
    if scope and scope.visibility == "private" and scope.venture_id == venture_id:
        identity.append(scope.id)
    return "chat:" + hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()


def bump_milestone(venture_id: str, *, key: str, body_md: str) -> bool:
    """Rewrite an existing milestone, moving it to today only within the notice cap. False if absent."""
    with db()() as s:
        serialize_writes(s, key="chat-notices:" + venture_id)
        row = s.scalar(select(ChatMessage).where(ChatMessage.idempotency_key == _scoped(venture_id, key)))
        if row is None:
            return False
        now = utcnow()
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        count = s.scalar(select(func.count()).select_from(ChatMessage).where(ChatMessage.venture_id == venture_id,
            ChatMessage.role == "assistant", ChatMessage.created_at >= day, ChatMessage.created_at < day + timedelta(days=1)))
        row.body_md = stamp_speaker(scrub(body_md), manager_speaker(venture_id))
        if row.created_at.date() == now.date() or count < DAILY_MESSAGE_LIMIT:
            row.created_at = now
        s.commit()
        return True


def post_milestone(venture_id: str, *, key: str, body_md: str, actions: list | None = None,
                   kind: str, session=None, failure: dict | None = None, email_reason: str | None = None,
                   requested: bool = False, citations: list | None = None) -> bool:
    """Post once per (venture, key), suppressing repeated daily failures and notices after 14 assistant messages.

    Returns False when suppressed. Explicit replies remain available after the daily notice cap.

    Pass ``session`` to join the caller's transaction (no commit). Never raises: a chat
    post must not fail the journey step that triggered it.
    """
    scoped = _scoped(venture_id, key)
    own = session is None
    from app.chat import recovery
    failed = recovery.is_failure_kind(kind)  # every failure message: receipt + "Do it another way" (chat/recovery.py)
    requested = requested or (is_local() and failed and bool((failure or {}).get("job_id")))
    if failed:
        actions = recovery.decorate(actions, kind, failure)
    if failed or kind.endswith("_blocked") or kind == "needs_you":  # blockers always explainable
        from app.chat.blockers import with_explain
        actions = with_explain({"actions": actions}, "job_failed",
            **({"step": failure["step"]} if (failure or {}).get("step") in {"idea.board", "ceo_review"} else {}))["actions"]
    try:
        s = db()() if own else session
        try:
            if s.get_bind().dialect.name != "sqlite" or not s.connection().connection.driver_connection.in_transaction:
                serialize_writes(s, key="chat-notices:" + venture_id)
            pending_messages = [message for message in s.new if isinstance(message, ChatMessage)]
            if pending_messages:
                s.flush(pending_messages)
            if s.scalar(select(ChatMessage.id).where(ChatMessage.idempotency_key == scoped)):
                return False
            venture = s.get(Venture, venture_id)
            if venture is None:
                return False
            from app.chat.conversations import _legacy, capture_job_conversation, resolve
            conversation = capture_job_conversation(venture_id, venture.workspace_id)
            conversation_id = conversation["conversation_id"] if conversation else _legacy(s, venture).id
            now = utcnow()
            day = now.replace(hour=0, minute=0, second=0, microsecond=0)
            body = stamp_speaker(scrub(body_md), manager_speaker(venture_id))
            today = select(ChatMessage).where(ChatMessage.venture_id == venture_id, ChatMessage.role == "assistant",
                ChatMessage.created_at >= day, ChatMessage.created_at < day + timedelta(days=1))
            # the cap is for unprompted notices; work the founder started (a search's results) always lands
            if not requested and s.scalar(select(func.count()).select_from(today.subquery())) >= DAILY_MESSAGE_LIMIT:
                return False
            if failed and not requested and s.scalar(today.where(ChatMessage.kind == kind, ChatMessage.body_md == body,
                    ChatMessage.conversation_id == conversation_id).limit(1)):
                return False
            from app.chat.store import _REPLY_TO, _RID, _request_message, request_citations

            metadata = request_citations(citations)
            nonce = next((item[_RID] for item in metadata if _RID in item), None)
            if nonce:
                scope = resolve(venture_id, conversation_id, session=s)
                if request := _request_message(s, scope, nonce):
                    metadata = [item for item in metadata if _REPLY_TO not in item] + [{_REPLY_TO: request.id}]
            message_id = new_id("msg")
            s.add(ChatMessage(id=message_id, venture_id=venture_id, workspace_id=venture.workspace_id,
                conversation_id=conversation_id,
                role="assistant", kind=kind, body_md=body,
                citations_json=json.dumps(metadata),
                actions_json=json.dumps(in_chat(actions)) if actions else None,
                idempotency_key=scoped, created_at=now))
            if is_local() and key.startswith("gtm_results:"):
                s.add(ActivityLine(venture_id=venture_id, workspace_id=venture.workspace_id, phase="gtm",
                    kind="customer_search_result", status="done", text="Your customer search finished. Open your chat to review the result.", ts=now))
            if failed:
                recovery.record(venture_id, kind=kind, key=key, failure=failure, workspace_id=venture.workspace_id,
                                session=s)
            if own:
                s.commit()
            from app.email.triggers import on_milestone
            if own:
                on_milestone(venture_id, kind, key, body_md, human_reason=email_reason)  # never raises
            else:  # email only once the caller's transaction really commits
                event.listen(s, "after_commit", lambda _s: on_milestone(venture_id, kind, key, body_md, human_reason=email_reason), once=True)
            if kind.endswith(("_ready", "_completed", "_done", "_result", "_results")):
                from app.chat.next_moves import post
                if own:
                    post(venture_id, conversation_id=conversation_id, message_id=message_id)
                else:
                    from app.chat.proactive import _spawn
                    event.listen(s, "after_commit", lambda _s: _spawn(post, venture_id, conversation_id=conversation_id, message_id=message_id), once=True)
            return True
        finally:
            if own:
                s.close()
    except IntegrityError:
        return False  # lost a race with a concurrent retry
    except Exception as exc:  # noqa: BLE001
        from app.chat.recovery import log_founder_exception
        log_founder_exception(exc, venture_id, "milestone.post")
        return False


def link(label: str, href: str, style: str = "primary") -> dict:
    return {"label": label, "href": href, "style": style}

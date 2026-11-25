"""Validated private chat partitions; shared venture records retain their authority."""
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json

from sqlalchemy import and_, select, or_
from app.db.models import ChatConversation, ChatMessage, Venture
from app.db.session import serialize_writes
from app.state import db
from app.tenancy import current_or_none
from core.clock import utcnow
from core.ids import new_id


@dataclass(frozen=True)
class ConversationScope:
    id: str
    venture_id: str
    workspace_id: str | None
    actor_id: str | None
    visibility: str


_CURRENT: ContextVar[ConversationScope | None] = ContextVar("chat_conversation", default=None)


def current_conversation() -> ConversationScope | None:
    return _CURRENT.get()


def capture_job_conversation(venture_id, workspace_id):
    scope = _CURRENT.get()
    if not scope or scope.visibility != "private" or scope.venture_id != venture_id:
        return None
    if scope.workspace_id != workspace_id or scope.actor_id != actor_id():
        raise ValueError("job_conversation_scope_mismatch")
    return {"schema_version": 1, "conversation_id": scope.id, "venture_id": scope.venture_id,
            "workspace_id": scope.workspace_id, "actor_id": scope.actor_id}


@contextmanager
def bind_job_conversation(payload, *, venture_id, workspace_id):
    value = payload.get("_chat_conversation")
    if value is None:
        yield None
        return
    if (not isinstance(value, dict) or set(value) != {"schema_version", "conversation_id", "venture_id", "workspace_id", "actor_id"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["venture_id"] != venture_id or value["workspace_id"] != workspace_id
            or not value["actor_id"] or value["actor_id"] != actor_id()
            or not isinstance(value["conversation_id"], str)):
        raise ValueError("job_conversation_scope_mismatch")
    with bind_conversation(venture_id, value["conversation_id"]) as scope:
        if scope.visibility != "private" or scope.workspace_id != workspace_id or scope.actor_id != value["actor_id"]:
            raise ValueError("job_conversation_scope_mismatch")
        yield scope


@contextmanager
def job_receipt_conversation(payload, *, venture_id, workspace_id):
    """Bind a saved chat destination for receipt copy, granting no work authority."""
    if payload.get("_chat_conversation") is None:
        yield True
        return
    from contextlib import ExitStack
    from app.tenancy import bind
    with ExitStack() as contexts:
        try:
            contexts.enter_context(bind(workspace_id=workspace_id, venture_id=venture_id,
                                        actor=str(payload.get("_actor_subject") or "system")))
            contexts.enter_context(bind_job_conversation(payload, venture_id=venture_id, workspace_id=workspace_id))
        except (ValueError, PermissionError):
            yield False
            return
        yield True


def actor_id() -> str | None:
    scope = current_or_none()
    if scope and scope.actor not in {"local", "anonymous", "system"}:
        from app.authz.workspace import subject_person_id
        return subject_person_id(scope.actor)
    return None


def _venture(session, venture_id):
    row = session.get(Venture, venture_id)
    scope = current_or_none()
    if row is None or (scope and scope.venture_id and scope.venture_id != venture_id):
        raise ValueError("chat_conversation_not_found")
    if scope and scope.workspace_id not in {"none", row.workspace_id}:
        raise ValueError("chat_conversation_not_found")
    return row


def _legacy(session, venture, *, create=True):
    # Shared worker replies can join an existing write transaction.
    if create and session.get_bind().dialect.name == "postgresql":
        serialize_writes(session, key="chat-conversations:" + venture.id)
    key = "legacy:" + venture.id
    row = session.scalar(select(ChatConversation).where(ChatConversation.creation_key == key))
    if row is None:
        earlier = session.scalar(select(ChatMessage.id).where(ChatMessage.venture_id == venture.id,
            ChatMessage.workspace_id == venture.workspace_id, ChatMessage.conversation_id.is_(None)).limit(1))
        row = ChatConversation(id="cnv_" + hashlib.sha256(key.encode()).hexdigest()[:32],
            venture_id=venture.id, workspace_id=venture.workspace_id,
            creator_person_id=None, visibility="legacy_shared", title="Earlier shared chat" if earlier else "Your conversation",
            creation_key=key, created_at=utcnow(), updated_at=utcnow())
        if create:
            session.add(row)
            session.flush()
    if (row.venture_id != venture.id or row.workspace_id != venture.workspace_id
            or row.visibility != "legacy_shared" or row.creator_person_id is not None):
        raise ValueError("chat_conversation_not_found")
    return row


def _conversation(session, venture, conversation_id, actor, *, create=True):
    row = session.get(ChatConversation, conversation_id) if conversation_id else _legacy(session, venture, create=create)
    if (row is None or row.venture_id != venture.id or row.workspace_id != venture.workspace_id
            or (row.visibility == "private" and (not actor or row.creator_person_id != actor))):
        raise ValueError("chat_conversation_not_found")
    return row


def resolve(venture_id: str, conversation_id: str | None = None, *, session=None, create=True) -> ConversationScope:
    active = _CURRENT.get()
    if conversation_id is None and active:
        if active.venture_id != venture_id:
            raise ValueError("chat_conversation_not_found")
        conversation_id = active.id
    owns_session = session is None
    with (db()() if owns_session else nullcontext(session)) as session:
        if create and conversation_id is None:
            serialize_writes(session, key="chat-conversations:" + venture_id)
        venture = _venture(session, venture_id)
        actor = actor_id()
        row = _conversation(session, venture, conversation_id, actor, create=create)
        result = ConversationScope(row.id, venture_id, venture.workspace_id, actor, row.visibility)
        if owns_session and create:
            session.commit()
        return result


@contextmanager
def bind_conversation(venture_id: str, conversation_id: str | None = None, *, session=None):
    scope = resolve(venture_id, conversation_id) if session is None else resolve(venture_id, conversation_id, session=session)
    token = _CURRENT.set(scope)
    try:
        yield scope
    finally:
        _CURRENT.reset(token)


def pending_key(venture_id: str) -> str:
    scope = _CURRENT.get()
    if scope and scope.venture_id == venture_id and scope.visibility == "private":
        return f"pending_chat_query:{venture_id}:{scope.id}"
    return f"pending_chat_query:{venture_id}"


def serialize(row):
    def timestamp(value):
        return value.isoformat().replace("+00:00", "Z") + ("Z" if value.tzinfo is None else "")
    return {"id": row.id, "venture_id": row.venture_id, "workspace_id": row.workspace_id,
        "visibility": row.visibility, "title": row.title, "pinned": row.pinned_at is not None,
        "archived": row.archived_at is not None, "folder": row.folder,
        "created_at": timestamp(row.created_at), "updated_at": timestamp(row.updated_at)}


def list_conversations(venture_id: str, *, current: str | None = None, archived: bool = False):
    actor = actor_id()
    if not actor:
        raise PermissionError("chat_actor_required")
    with db()() as session:
        serialize_writes(session, key="chat-conversations:" + venture_id)
        venture = _venture(session, venture_id)
        legacy = _legacy(session, venture)
        has_messages = select(ChatMessage.id).where(
            ChatMessage.venture_id == venture_id,
            or_(ChatMessage.conversation_id == ChatConversation.id,
                and_(ChatConversation.visibility == "legacy_shared", ChatMessage.conversation_id.is_(None))),
        ).exists()
        query = select(ChatConversation).where(
            ChatConversation.venture_id == venture_id,
            ChatConversation.workspace_id == venture.workspace_id,
            or_(ChatConversation.visibility == "legacy_shared", ChatConversation.creator_person_id == actor),
            ChatConversation.archived_at.is_not(None) if archived else ChatConversation.archived_at.is_(None),
            or_(has_messages, ChatConversation.id == current),
        )
        rows = session.scalars(query.order_by(ChatConversation.pinned_at.is_not(None).desc(),
            ChatConversation.updated_at.desc(), ChatConversation.id.desc()).limit(100)).all()
        if current and all(row.id != current for row in rows):
            current_row = session.scalar(query.where(ChatConversation.id == current))
            if current_row:
                rows.append(current_row)
                rows.sort(key=lambda row: (row.pinned_at is not None, row.updated_at, row.id), reverse=True)
        items = [serialize(row) for row in rows]
        result = {"venture_id": venture_id, "workspace_id": venture.workspace_id, "actor_id": actor,
            "legacy_conversation_id": legacy.id, "items": items}
        session.commit()
        return result


def update(venture_id: str, conversation_id: str, changes: dict):
    actor = actor_id()
    if not actor:
        raise PermissionError("chat_actor_required")
    with db()() as session:
        serialize_writes(session, key="chat-messages:" + conversation_id)
        venture = _venture(session, venture_id)
        row = _conversation(session, venture, conversation_id, actor)
        now = utcnow()
        if "title" in changes:
            row.title = changes["title"]
        if "folder" in changes:
            row.folder = changes["folder"] or None
        for field in ("pinned", "archived"):
            if field in changes:
                attribute = field + "_at"
                setattr(row, attribute, (getattr(row, attribute) or now) if changes[field] else None)
        if changes:
            row.updated_at = now
        session.flush()
        result = serialize(row)
        session.commit()
        return result


def create(venture_id: str, *, idempotency_key: str, title: str = "New chat"):
    actor = actor_id()
    if not actor:
        raise PermissionError("chat_actor_required")
    title = title.strip() or "New chat"
    key = "private:" + hashlib.sha256(json.dumps([venture_id, actor, idempotency_key], separators=(",", ":")).encode()).hexdigest()
    with db()() as session:
        serialize_writes(session, key="chat-conversations:" + venture_id)
        venture = _venture(session, venture_id)
        row = session.scalar(select(ChatConversation).where(ChatConversation.creation_key == key))
        created = False
        if row is None:
            row = ChatConversation(id=new_id("cnv"), venture_id=venture_id, workspace_id=venture.workspace_id,
                creator_person_id=actor, visibility="private", title=title, creation_key=key,
                created_at=utcnow(), updated_at=utcnow())
            session.add(row)
            created = True
        session.flush()
        result, row_id, workspace_id = serialize(row), row.id, row.workspace_id
        session.commit()
    if created:
        from app.analytics.funnel import emit_for_venture
        emit_for_venture("chat.new_conversation", venture_id, key=f"conversation:{row_id}", person_id=actor,
                         workspace_id=workspace_id)
    return result

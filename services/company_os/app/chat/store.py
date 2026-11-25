"""Chat message persistence."""

from __future__ import annotations

import json
import hashlib
import re
from collections.abc import Callable
from contextvars import ContextVar
from datetime import timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError

from app.db.models import ChatMessage, ChatConversation, IdempotencyRecord
from app.db.session import serialize_writes
from app.chat.conversations import current_conversation, resolve
from app.chat.diagram_check import checked_parts
from app.state import db
from core.clock import as_utc, utcnow
from core.copy import strip_emdash
from core.ids import new_id

# Founders cannot open the worker's own loopback address.
_LOCAL_URL = re.compile(r"`?https?://(?:127\.0\.0\.1|localhost|\[::1\])(?::\d+)?[^\s`)]*`?", re.I)


# Nonce of the chat POST being handled; saved founder rows carry it so clients can reconcile optimistic twins.
CLIENT_REQUEST_ID: ContextVar[str | None] = ContextVar("chat_client_request_id", default=None)
# Empty background identity prevents legacy jobs from adopting a newer founder request.
REPLY_REQUEST_ID: ContextVar[str | None] = ContextVar("chat_reply_request_id", default=None)
_RID = "client_request_id"
_REPLY_TO = "reply_to_message_id"
_INTERPRETED = "interpreted_request"
_PARTS = "_reply_parts"
_IDEA_REVISION = "_idea_revision_id"
_RECOMMENDATION = "_recommendation_sha256"
_LOCAL_RECOMMENDATION_STAGE = "_local_recommendation_stage"


def save_voice_session_receipt(person, workspace_id, venture_id):
    from fastapi import HTTPException
    from sqlalchemy.exc import SQLAlchemyError
    identifier = new_id("ane")
    try:
        with db()() as session:
            session.add(IdempotencyRecord(key="voice-session:" + identifier, scope="voice-session",
                response_json=json.dumps({"person_id": person, "workspace_id": workspace_id, "venture_id": venture_id}),
                created_at=utcnow()))
            session.commit()
    except (OSError, SQLAlchemyError) as exc:
        raise HTTPException(503, detail={"error": {"code": "voice_session_not_saved",
            "message": "Your call could not be saved. Check that your local data folder is writable and has free space, then restart your app and try voice again. You can keep typing."}}) from exc
    return identifier


def request_citations(citations=None, *, request_id=None):
    metadata = list(citations or [])
    nonce = request_id or CLIENT_REQUEST_ID.get() or REPLY_REQUEST_ID.get()
    return [item for item in metadata if _RID not in item] + [{_RID: nonce}] if nonce else metadata


def _request_message(session, scope, nonce=None):
    query = select(ChatMessage).where(ChatMessage.venture_id == scope.venture_id,
        ChatMessage.role.in_(["user", "founder"]), _partition(scope)).order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
    if not nonce:
        return session.scalar(query.limit(1))
    query = query.where(ChatMessage.citations_json.contains(json.dumps({_RID: nonce}), autoescape=True))
    return next((row for row in session.scalars(query) if any(item.get(_RID) == nonce
        for item in json.loads(row.citations_json or "[]"))), None)


def _clean(body: str, *, assistant: bool = False) -> str:
    from app.chat.blockers import humanize_codes

    # A raw machine error code (desktop_..., cu_...) never reaches a founder.
    text = humanize_codes(strip_emdash(_LOCAL_URL.sub("the website preview", body or "")), include_failures=assistant)
    if assistant:
        from app.chat.confirm import without_usage_copy
        from app.org.characters import parse_speaker
        clean = without_usage_copy(re.sub(r"(?im)^Saved background status:[^\n]*\n?", "", text))
        return clean if parse_speaker(clean)[1].strip() else clean + "\nAsk me what you should do next."
    return text


def _as_manager(venture_id: str, body_md: str, *, preserve_exact: bool = False) -> str:
    """Every assistant line is spoken by the manager (Charlie), never an anonymous 'Team'."""
    from app.org.characters import stamp_speaker
    from app.org.manager import manager_speaker

    return stamp_speaker(body_md, manager_speaker(venture_id), preserve_exact=preserve_exact)


def _matches(row, body, kind):
    base_hash = next((item.get("base_body_sha256") for item in json.loads(row.citations_json or "[]")
                      if isinstance(item, dict) and item.get("source") == "yc_video_quote"), None)
    if base_hash is None:
        base_hash = next((item["next_moves"].get("base_body_sha256") for item in json.loads(row.citations_json or "[]")
                          if isinstance(item, dict) and isinstance(item.get("next_moves"), dict)), None)
    return row.kind == kind and (base_hash == hashlib.sha256(body.encode()).hexdigest() if base_hash else row.body_md == body)


def _recommendation_hash(body, actions):
    """Fingerprint the recommendation before tailoring its display, ignoring snapshot-only action metadata."""
    actions = [{key: value for key, value in action.items() if key != "state_hash"}
               if isinstance(action, dict) else action for action in actions or []]
    return hashlib.sha256(json.dumps([body, actions], sort_keys=True).encode()).hexdigest()


def _search_progress(citations):
    progress = next((item["gtm_search_progress"] for item in citations or [] if isinstance(item, dict)
        and isinstance(item.get("gtm_search_progress"), dict)), None)
    return progress if progress and all(progress.get(field) is None or isinstance(progress.get(field), str)
        for field in ("run_id", "job_id", "state", "audience")) and (progress.get("run_id") or progress.get("job_id")) else None


def append_message(
    venture_id: str,
    *,
    role: str,
    body_md: str,
    kind: str = "text",
    citations: list | None = None,
    actions: list | None = None,
    idempotency_key: str | None = None,
    preserve_exact: bool = False,
    conversation_id: str | None = None,
    output_authority: Callable | None = None,
    parts: list | None = None,
    recommendation_stage: str | None = None,
) -> dict:
    from app.org.characters import parse_speaker
    from app.core.local_mode import is_local

    if role == "assistant" and kind in {"next_moves", "status"} and is_local() and recommendation_stage is None and not preserve_exact:
        from app.chat.next_moves import short_reply

        body_md = short_reply(body_md)
    speaker, _ = parse_speaker(body_md)
    employee_reply = speaker is not None and speaker.get("id") != "manager"
    # Internal verified server evidence only; never populated from chat payloads.
    clean = (lambda body: body) if preserve_exact else (lambda body: _clean(body, assistant=role == "assistant"))
    # The database key is globally unique; namespace it before lookup/storage so
    # the same client nonce in another venture cannot retrieve private history.
    if role == "assistant":
        from app.chat.reply_safety import guard_reply
        guarded = guard_reply(venture_id, {"body_md": body_md, "kind": kind,
                                           "actions": actions, "citations": citations, "parts": parts})
        body_md, kind = guarded["body_md"], guarded["kind"]
        actions, citations, parts = guarded.get("actions"), guarded.get("citations"), guarded.get("parts")
        body_md = _as_manager(venture_id, body_md, preserve_exact=preserve_exact)
        from app.chat.navigation_map import in_chat
        # Only a message whose primary button offers research becomes the prerequisites message; links that show the
        # research page (turned into in-chat buttons below) and other replies keep their text.
        first = (actions or [None])[0]
        actions = in_chat(actions)
        if kind != "next_moves" and isinstance(first, dict) and (first.get("action") or first.get("name")) in {"research", "research.prepare"}:
            from app.chat.execute import research_needs_start
            from app.chat.build_path import research_prerequisites
            blocked = research_prerequisites(venture_id) if research_needs_start(venture_id) else None
            if blocked:
                actions = blocked["actions"]
                if not preserve_exact:
                    body_md = _as_manager(venture_id, blocked["body_md"])
                    kind, parts = blocked["kind"], None
    scope = resolve(venture_id, conversation_id)
    progress = None
    if role == "assistant":
        progress = _search_progress(citations)
        if progress and kind != "gtm_search_progress" and (not idempotency_key or kind in {"text", "gtm_search_failed"}):
            if kind == "gtm_search_failed" and progress.get("job_id"):
                idempotency_key = "gtm_search_failed:" + progress["job_id"]
            else:
                if progress.get("state") in {"ready", "needs_input"} and progress.get("run_id"):
                    receipt = idempotent_message(venture_id, role, "gtm_results:" + progress["run_id"], conversation_id=scope.id,
                        output_authority=output_authority)
                    if receipt and receipt["kind"] == kind and receipt["body_md"] == clean(body_md):
                        return receipt
                idempotency_key = "gtm-search-progress:" + hashlib.sha256(json.dumps([scope.id, progress, kind, clean(body_md), actions], sort_keys=True).encode()).hexdigest()
    from app.chat.milestone_hooks import started_job
    started = started_job(scope, kind, citations) if role == "assistant" and (
        not progress or any(isinstance(item, dict) and "job_started" in item for item in citations or [])) else None
    if started:
        idempotency_key = "job-started:" + started.id
        marker = {"job_started": {"job_id": started.id}}
        citations = citations if marker in citations else [*citations, marker]
    identity = [venture_id, role, idempotency_key]
    if scope.visibility == "private":
        identity.append(scope.id)
    scoped_key = ("chat:" + hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()) if idempotency_key else None
    factory = db()
    with factory() as session:
        serialize_writes(session, key="chat-messages:" + scope.id)
        if role == "assistant" and kind == "exploration_options":
            if output_authority is not None:
                output_authority(session, scope)
            from app.experiments import exploration
            if not exploration.enabled():
                raise KeyError("exploration")
            detail = next((item["exploration_options"] for item in citations or []
                           if isinstance(item, dict) and isinstance(item.get("exploration_options"), dict)), None)
            if not detail or not isinstance(detail.get("round_id"), str):
                raise ValueError("exploration_round_required")
            exploration.get_round(venture_id, detail["round_id"])
            previous = next((row for row in session.scalars(select(ChatMessage).where(
                ChatMessage.venture_id == venture_id, ChatMessage.role == "assistant",
                ChatMessage.kind == kind, _partition(scope))) if any(
                item.get("exploration_options", {}).get("round_id") == detail["round_id"]
                for item in json.loads(row.citations_json or "[]"))), None)
            if previous is None:
                previous = ChatMessage(id=new_id("msg"), venture_id=venture_id, workspace_id=scope.workspace_id,
                    conversation_id=scope.id, role=role, kind=kind, idempotency_key=scoped_key, created_at=utcnow())
                session.add(previous)
            previous.body_md = clean(body_md)
            previous.actions_json = json.dumps(actions or [])
            previous.citations_json = json.dumps(citations or [])
            session.commit()
            return _ser(previous, scope.id)
        if role == "assistant":
            serialize_writes(session, key="chat-notices:" + venture_id)
        if role == "assistant" and kind == "text" and not preserve_exact:
            serialize_writes(session, key="chat-coaching:" + venture_id)
        if output_authority is not None:
            output_authority(session, scope)
        if idempotency_key:
            existing = session.scalars(
                select(ChatMessage).where(
                    ChatMessage.venture_id == venture_id,
                    ChatMessage.role == role,
                    _partition(scope),
                    ChatMessage.idempotency_key.in_([scoped_key, idempotency_key]),
                )
            ).first()
            if existing:
                if not started and not (progress and kind == existing.kind == "gtm_search_progress") and not _matches(existing, clean(body_md), kind):
                    raise ValueError("chat_idempotency_conflict")
                return _ser(existing, scope.id)
        if role == "assistant" and (kind in {"experiments", "experiment_result"} or parse_speaker(body_md)[1].startswith("Your experiments:")):
            previous = session.scalar(select(ChatMessage).where(ChatMessage.venture_id == venture_id,
                ChatMessage.role == "assistant", _partition(scope))
                .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc()).limit(1))
            if previous and previous.body_md == clean(body_md):
                if actions is not None:
                    previous.actions_json = json.dumps(actions)
                if citations is not None:
                    previous.citations_json = json.dumps(citations)
                session.commit()
                return _ser(previous, scope.id)
        conversation = session.get(ChatConversation, scope.id)
        if (role in {"user", "founder"} and conversation.title == "New chat"
                and session.scalar(select(ChatMessage.id).where(
                    ChatMessage.venture_id == venture_id, _partition(scope),
                    ChatMessage.role.in_(["user", "founder"]),
                ).limit(1)) is None):
            title = re.sub(r"[^\w]+$", "", " ".join(re.findall(r"[^\W_]+(?:['’-][^\W_]+)*", body_md)[:6])[:60])
            if title:
                conversation.title = title
        request_row = None
        if role == "assistant" and REPLY_REQUEST_ID.get() != "":
            request_row = _request_message(session, scope, CLIENT_REQUEST_ID.get() or REPLY_REQUEST_ID.get())
        metadata = request_citations(citations) if role in {"user", "founder"} or role == "assistant" and not employee_reply else list(citations or [])
        if role in {"user", "founder"}:
            from app.chat.intent import find_customers_request
            if find_customers_request(body_md):
                from app.db.models import Venture
                from app.ventures.current_idea import current_revision
                revision = current_revision(session.get(Venture, venture_id), session)
                metadata = [item for item in metadata if _IDEA_REVISION not in item] + [{_IDEA_REVISION: revision.id if revision else None}]
        if request_row:
            metadata = [item for item in metadata if _REPLY_TO not in item] + [{_REPLY_TO: request_row.id}]
            metadata += [item for item in json.loads(request_row.citations_json or "[]") if _IDEA_REVISION in item]
        created_at = started.created_at if started else utcnow()
        if progress and started and request_row:
            created_at = max(as_utc(created_at), as_utc(request_row.created_at) + timedelta(microseconds=1))
        row = ChatMessage(
            id=new_id("msg"),
            venture_id=venture_id,
            workspace_id=scope.workspace_id,
            conversation_id=scope.id,
            author_person_id=scope.actor_id if role in {"user", "founder"} else None,
            role=role,
            kind=kind,
            body_md=clean(body_md),
            citations_json=json.dumps(metadata) if metadata else None,
            actions_json=json.dumps(actions) if actions else None,
            idempotency_key=scoped_key,
            created_at=created_at,
        )
        if recommendation_stage is not None:
            from app.chat.next_moves import admit_local_recommendation

            if kind != "next_moves" or not admit_local_recommendation(session, scope, row, recommendation_stage):
                return {}
        elif role == "assistant" and kind == "next_moves" and actions and not (employee_reply or started or progress):
            from app.chat.next_moves import _question_action

            if is_local() and not any(_question_action(action) or action.get("confirmation")
                                      for action in actions if isinstance(action, dict)):
                signature = _recommendation_hash(row.body_md, actions)
                previous = session.scalar(select(ChatMessage).where(ChatMessage.venture_id == venture_id,
                    ChatMessage.role == "assistant", ChatMessage.kind == kind, _partition(scope))
                    .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc()).limit(1))
                if previous is not None:
                    previous_signature = next((item[_RECOMMENDATION] for item in json.loads(previous.citations_json or "[]")
                                               if _RECOMMENDATION in item), None)
                    if previous_signature is None and _matches(previous, row.body_md, kind):
                        previous_signature = _recommendation_hash(row.body_md, json.loads(previous.actions_json or "[]"))
                    if signature == previous_signature:
                        return _ser(previous, scope.id)
                row.citations_json = json.dumps([*metadata, {_RECOMMENDATION: signature}])
        if role == "assistant" and kind == "text" and not preserve_exact and not employee_reply:
            from app.chat.ground import yc_quote_for_reply
            recent = session.scalars(select(ChatMessage.citations_json).where(
                ChatMessage.venture_id == venture_id, ChatMessage.role == "assistant",
            ).order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc()).limit(3)).all()
            user = request_row
            from app.db.models import Venture
            venture = session.get(Venture, venture_id)
            quote = yc_quote_for_reply(user.body_md, [json.loads(item or "[]") for item in recent],
                context=venture.raw_idea if venture else "", reply=body_md) if user else None
            if quote and _clean(quote["text"], assistant=True) == quote["text"]:
                base_body = row.body_md
                title = re.sub(r"[\[\]\r\n]", "", quote["title"])
                row.body_md += f'\n\nYC says: “{quote["text"]}”'
                row.citations_json = json.dumps([*json.loads(row.citations_json or "[]"),
                    {"source": "yc_video_quote", "id": quote["id"], "href": quote["url"],
                     "label": title, "base_body_sha256": hashlib.sha256(base_body.encode()).hexdigest()}])
        if parts and role == "assistant":
            user = request_row
            admitted = checked_parts(parts, question=user.body_md if user else "", body=row.body_md, kind=kind, actions=actions)
            admitted = [{**part, "text": clean(part["text"])} if part["type"] == "text" else part for part in admitted]
            _, prose = parse_speaker(row.body_md)
            original = "\n\n".join(part["text"] for part in admitted if part["type"] == "text" and part["text"].strip())
            if original and (prose == original or prose.startswith(original + "\n\n")):
                if prose != original:
                    admitted.append({"type": "text", "text": prose[len(original):].lstrip()})
                if any(part["type"] == "diagram" for part in admitted):
                    row.citations_json = json.dumps([*json.loads(row.citations_json or "[]"), {_PARTS: admitted}])
        session.add(row)
        if role == "assistant" and scope.visibility != "private" and not preserve_exact and kind == "text":
            from app.db.models import Venture
            from app.ventures.founder_facts import read, contradicts
            from app.ventures.startup_review import enabled
            from sqlalchemy.exc import SQLAlchemyError
            # Best effort: a missing/unavailable fact store must never fail the message.
            try:
                with session.begin_nested():
                    facts = read(session, session.get(Venture, venture_id)) if enabled(session, venture_id) else None
            except SQLAlchemyError as exc:
                from app.chat.recovery import log_founder_exception
                log_founder_exception(exc, venture_id, "store.fact_filter")
                facts = None
            if facts:
                filtered = [action for action in actions or [] if not contradicts(str(action.get("label") or ""), facts)]
                row.actions_json = json.dumps(filtered) if filtered else None
        conversation.updated_at = utcnow() if started else row.created_at
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            if not scoped_key:
                raise
            serialize_writes(session, key="chat-messages:" + scope.id)
            if output_authority is not None:
                output_authority(session, scope)
            existing = session.scalar(select(ChatMessage).where(
                ChatMessage.venture_id == venture_id, ChatMessage.role == role,
                _partition(scope),
                ChatMessage.idempotency_key == scoped_key,
            ))
            if existing is None:
                raise
            if not started and not _matches(existing, clean(body_md), kind):
                raise ValueError("chat_idempotency_conflict")
            return _ser(existing, scope.id)
        session.refresh(row)
        if role == "assistant" and not preserve_exact and not employee_reply and recommendation_stage is None:
            from app.chat.next_moves import attach
            request = request_row.body_md if request_row else ""
            original = (row.body_md, row.citations_json, row.actions_json)
            session.expunge(row)
            session.commit()
            # Next-step composition can save prerequisite evidence in its own transaction.
            attach(venture_id, row, scope=scope, request=request)
            serialize_writes(session, key="chat-messages:" + scope.id)
            resolve(venture_id, scope.id, session=session)
            if output_authority is not None:
                output_authority(session, scope)
            saved = session.get(ChatMessage, row.id)
            if saved is None:
                raise ValueError("Your saved reply was removed. Open your startup's chat and send your request again.")
            if (saved.body_md, saved.citations_json, saved.actions_json) == original:
                saved.body_md, saved.citations_json, saved.actions_json = row.body_md, row.citations_json, row.actions_json
            row = saved
            session.commit()
            session.refresh(row)
        if role in {"user", "founder"} and scope.visibility != "private" and kind != "new_venture":
            from app.ventures.founder_facts import schedule
            from app.db.supymem_models import ClarifierTurn
            from app.idea.agent import answer_metadata
            from sqlalchemy.exc import SQLAlchemyError
            # Best effort: fact capture must never fail or poison the founder's message transaction.
            try:
                with session.begin_nested():
                    latest = session.scalar(select(ClarifierTurn).where(ClarifierTurn.venture_id == venture_id,
                        ClarifierTurn.workspace_id == scope.workspace_id, ClarifierTurn.role == "founder")
                        .order_by(ClarifierTurn.created_at.desc(), ClarifierTurn.turn.desc()).limit(1))
                    question = session.scalar(select(ClarifierTurn).where(ClarifierTurn.venture_id == venture_id,
                        ClarifierTurn.workspace_id == scope.workspace_id, ClarifierTurn.role == "agent", ClarifierTurn.kind == "question")
                        .order_by(ClarifierTurn.created_at.desc(), ClarifierTurn.turn.desc()).limit(1))
            except SQLAlchemyError as exc:
                from app.chat.recovery import log_founder_exception
                log_founder_exception(exc, venture_id, "store.fact_context")
                latest, question = None, None
            metadata = json.loads(latest.options or "{}") if latest and latest.text == body_md else answer_metadata(body_md)
            from app.idea.service import _loads
            from app.clarify.dilemmas import detail_choices
            options = _loads(question.options) if question else {}
            if isinstance(options, dict):
                choices = [option for option in options.get("detail_source_options") or options.get("options") or [] if isinstance(option, str)]
                selected = answer_metadata(body_md, choices)
                if any(option in selected["suggested_text"] for option in detail_choices(choices, options.get("requires_detail") or [])):
                    metadata["founder_text"] = answer_metadata(selected["founder_text"].strip(), choices)["founder_text"].strip()
        result = _ser(row, scope.id)
    if role in {"user", "founder"} and kind != "new_venture":
        from app.memory.extraction import schedule_capture
        schedule_capture(result["id"])
    if role in {"user", "founder"} and scope.visibility != "private" and kind != "new_venture":
        schedule(venture_id, metadata.get("founder_text", body_md), scope.actor_id)
    if role == "assistant" and kind == "text":
        if not is_local():
            from app.ventures import idea_page
            idea_page.request_refresh(venture_id)
        if not preserve_exact:
            _schedule_diagram(venture_id, result, scope)
    return result


REPLY_WINDOW_S = 180


def _schedule_diagram(venture_id: str, message: dict, scope) -> None:
    """Saved plain assistant answers get the optional diagram step, but only as a direct reply to a founder message.

    Milestones, nudges and worker posts (no fresh founder message since the last assistant one) never pay for it.
    """
    from datetime import timedelta
    from app.chat.diagram_reply import schedule_diagram_step

    def naive(value):
        return value.replace(tzinfo=None) if value is not None and value.tzinfo else value

    question, skip = "", "not_a_reply"
    try:
        with db()() as session:
            founder = _request_message(session, scope, message.get(_RID))
            previous = session.scalar(select(ChatMessage.created_at).where(
                ChatMessage.venture_id == venture_id, ChatMessage.role == "assistant", _partition(scope),
                ChatMessage.id != message.get("id"),
            ).order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc()).limit(1))
        if founder is not None:
            asked = naive(founder.created_at)
            fresh = asked is not None and naive(utcnow()) - asked <= timedelta(seconds=REPLY_WINDOW_S)
            if fresh and (previous is None or naive(previous) <= asked):
                question, skip = founder.body_md or "", ""
    except Exception as exc:  # noqa: BLE001 - never fail a saved answer
        from app.chat.recovery import log_founder_exception
        log_founder_exception(exc, venture_id, "store.diagram_context")
        skip = "not_a_reply"
    schedule_diagram_step(venture_id, message, question=question, workspace_id=scope.workspace_id,
                          actions=message.get("actions"), skip=skip)


def _partition(scope):
    # Pre-migration local rows remain visible only in the explicit legacy feed.
    return or_(ChatMessage.conversation_id == scope.id, ChatMessage.conversation_id.is_(None)) if scope.visibility == "legacy_shared" else ChatMessage.conversation_id == scope.id


def idempotent_message(venture_id: str, role: str, key: str, *, conversation_id: str | None = None,
                       output_authority: Callable | None = None) -> dict | None:
    """Read a saved chat receipt before repeating an operation after a timeout."""
    scope = resolve(venture_id, conversation_id, create=False)
    identity = [venture_id, role, key]
    if scope.visibility == "private":
        identity.append(scope.id)
    scoped_key = "chat:" + hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
    with db()() as session:
        if output_authority is not None:
            serialize_writes(session, key="chat-messages:" + scope.id)
            output_authority(session, scope)
        row = session.scalar(select(ChatMessage).where(
            ChatMessage.venture_id == venture_id, ChatMessage.role == role,
            _partition(scope),
            ChatMessage.idempotency_key == scoped_key))
        if row is None:  # a per-job started row is keyed by its job, so its request id is in the citations
            row = session.scalar(select(ChatMessage).where(
                ChatMessage.venture_id == venture_id, ChatMessage.role == role, _partition(scope),
                ChatMessage.citations_json.contains(json.dumps({_RID: key}), autoescape=True),
                ChatMessage.citations_json.contains('"job_started"')).limit(1))
        return _ser(row, scope.id) if row else None


def _message_views(rows, venture_id, conversation_id):
    from app.chat.confirmations import restore
    from app.core.local_mode import is_sample_workspace
    from app.credits.policy import get_policy
    from app.inbox import MESSAGE_KINDS

    samples = {wid: is_sample_workspace(wid) for wid in {row.workspace_id for row in rows if row.role == "assistant"}}
    flags = get_policy("feature_flags") if any(row.kind in MESSAGE_KINDS for row in rows) else None
    return restore([_ser(row, conversation_id, sample=samples.get(row.workspace_id, False), flags=flags or {})
                    for row in reversed(rows)], venture_id)


def list_messages(venture_id: str, *, limit: int = 100, conversation_id: str | None = None) -> list[dict]:
    scope = resolve(venture_id, conversation_id, create=False)
    factory = db()
    with factory() as session:
        rows = session.scalars(
            select(ChatMessage)
            .where(ChatMessage.venture_id == venture_id, _partition(scope))
            .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
            .limit(max(1, min(limit, 200)))
        ).all()
        return _message_views(rows, venture_id, scope.id)


def page_messages(venture_id: str, *, before: str | None = None, limit: int = 100, conversation_id: str | None = None) -> tuple[list[dict], str | None]:
    """Return a stable, venture-scoped page from newest history toward oldest."""
    factory = db()
    page_size = max(1, min(limit, 100))
    bound = current_conversation()
    scope = bound if conversation_id is None and bound and bound.venture_id == venture_id else resolve(venture_id, conversation_id, create=False)
    with factory() as session:
        query = select(ChatMessage).where(ChatMessage.venture_id == venture_id, _partition(scope))
        if before:
            marker = session.scalar(select(ChatMessage).where(
                ChatMessage.id == before, ChatMessage.venture_id == venture_id, _partition(scope)))
            if marker is None:
                raise ValueError("chat_cursor_not_found")
            query = query.where(or_(
                ChatMessage.created_at < marker.created_at,
                and_(ChatMessage.created_at == marker.created_at, ChatMessage.id < marker.id),
            ))
        rows = session.scalars(query.order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
                               .limit(page_size + 1)).all()
        has_more = len(rows) > page_size
        rows = rows[:page_size]
        return _message_views(rows, venture_id, scope.id), rows[-1].id if has_more and rows else None


def save_interpreted_request(venture_id: str, message_id: str, interpreted: str) -> None:
    scope = resolve(venture_id)
    with db()() as session:
        serialize_writes(session, key="chat-messages:" + scope.id)
        row = session.scalar(select(ChatMessage).where(ChatMessage.id == message_id,
            ChatMessage.venture_id == venture_id, ChatMessage.role.in_(["user", "founder"]), _partition(scope)))
        if row is not None:
            metadata = [item for item in json.loads(row.citations_json or "[]") if _INTERPRETED not in item]
            row.citations_json = json.dumps(metadata + [{_INTERPRETED: interpreted}], ensure_ascii=False)
            session.commit()


def attach_diagram(message_id: str, part: dict, *, question: str = "") -> list[dict] | None:
    """Append one checked diagram to a saved assistant reply (the after-answer diagram step)."""
    from app.org.characters import parse_speaker
    with db()() as session:
        row = session.get(ChatMessage, message_id)
        if row is None or row.role != "assistant" or row.kind != "text":
            return None
        serialize_writes(session, key="chat-messages:" + (row.conversation_id or row.venture_id))
        cites = json.loads(row.citations_json or "[]")
        if any(_PARTS in c for c in cites):
            return None
        _, prose = parse_speaker(_clean(row.body_md, assistant=True))
        actions = json.loads(row.actions_json) if row.actions_json else []
        # The step already applied its own question rule; diagrams_allowed's word list would veto "confirmed order".
        parts = checked_parts([{"type": "text", "text": prose}, part], body=prose, actions=actions)
        if not any(p["type"] == "diagram" for p in parts):
            return None
        row.citations_json = json.dumps([*cites, {_PARTS: parts}])
        session.commit()
        return parts


def _ser(r: ChatMessage, admitted_conversation_id: str | None = None, *, sample: bool | None = None, flags=None) -> dict:
    # These server-selected kinds carry exact saved founder statements. Their
    # punctuation/URLs must match the source memory displayed for correction.
    cites = json.loads(r.citations_json) if r.citations_json else []
    exact_memory = (r.role == "user" and r.kind == "memory_command") or (r.role == "assistant" and r.kind == "memory_list")
    from app.org.characters import parse_speaker
    from app.inbox import MESSAGE_KINDS, QUOTED_HISTORY
    if flags is None and r.kind in MESSAGE_KINDS:
        from app.credits.policy import get_policy
        flags = get_policy("feature_flags")
    inbox_flag = "cos_brief_v1" if r.kind == "cos_brief" else "inbox_briefing_v1"
    visible_inbox = r.kind in MESSAGE_KINDS and isinstance(flags, dict) and flags.get(inbox_flag) is True
    body = (r.body_md if exact_memory or visible_inbox else QUOTED_HISTORY if r.kind in MESSAGE_KINDS
            else _clean(r.body_md, assistant=r.role == "assistant"))
    if visible_inbox:
        from app.chat.guard import _RULES
        if _RULES[0][1].search(body):
            body = QUOTED_HISTORY
    _, prose = parse_speaker(body)
    actions = (json.loads(r.actions_json) if r.actions_json else []) or []
    cap_notice = False
    cap_update = False
    if r.role == "assistant":
        from app.chat.confirm import visible_action, _LIMIT_REASON
        from app.credits.errors import LIMIT_MESSAGE
        actions = [visible_action(action) for action in actions if isinstance(action, dict)
                   and (action.get("confirmation") is None or isinstance(action.get("confirmation"), dict))]
        cap_notice = not exact_memory and any(isinstance(action.get("confirmation"), dict)
            and action["confirmation"].get("action") == "experiment.spend_cap" for action in actions)
        cap_update = not exact_memory and any(action.get("action") == "experiment.prepare" for action in actions) and (
            bool(re.search(r"\bcap\s+is\s+now\s+\$", r.body_md, flags=re.I)) or any(isinstance(cite, dict)
                and isinstance(cite.get("confirmed_action"), dict) and cite["confirmed_action"].get("action") == "experiment.spend_cap"
                and cite["confirmed_action"].get("state") == "started" for cite in cites))
        if cap_notice or cap_update:
            from app.credits.spend import CAP_MESSAGE, CAP_SAVED_MESSAGE

            body = CAP_MESSAGE.format(title="this run") if cap_notice else CAP_SAVED_MESSAGE
            _, prose = parse_speaker(body)
        if LIMIT_MESSAGE in body or (not exact_memory and r.kind not in MESSAGE_KINDS and _LIMIT_REASON.search(r.body_md)) or any(isinstance(action, dict)
                and action.get("disabled_reason") == LIMIT_MESSAGE for action in actions):
            body = LIMIT_MESSAGE
            _, prose = parse_speaker(body)
            actions = [{"label": "Buy more", "href": f"/v/{r.venture_id}/settings#settings-credits" if getattr(r, "venture_id", None)
                        else "/account#account-buy", "style": "primary", "kind": "recovery_link"}]
    parts = next((c[_PARTS] for c in cites if _PARTS in c), []) if r.role == "assistant" else []
    if r.kind in MESSAGE_KINDS:
        parts = []
    if r.role == "assistant" and not exact_memory and isinstance(parts, list):
        parts = [{**part, "text": _clean(part["text"], assistant=True)}
                 if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
                 else part for part in parts]
        if cap_notice or cap_update or "You have reached your usage limit" in body:
            parts = [{"type": "text", "text": prose}]
    parts = checked_parts(parts, body=prose, kind=r.kind, actions=actions)
    reply = {
        "id": r.id,
        "conversation_id": r.conversation_id or admitted_conversation_id,
        "role": r.role,
        "kind": r.kind,
        "body_md": body,
        "content": prose,
        "parts": parts or [{"type": "text", "text": prose}],
        "citations": [c for c in cites if "job_started" not in c and "next_moves" not in c and "_proactive" not in c and "confirmed_action" not in c and _RID not in c and _REPLY_TO not in c and _INTERPRETED not in c and _PARTS not in c and _IDEA_REVISION not in c and "founder_references" not in c and "gtm_search_progress" not in c and "exploration_options" not in c and "numbers_input" not in c and _LOCAL_RECOMMENDATION_STAGE not in c
            and _RECOMMENDATION not in c and not (c.get("source") == "yc_video_quote" and c.get("href") and c["href"] in r.body_md)],
        **({"numbers_input": state} if r.role == "assistant" and (state := next(
            (c["numbers_input"] for c in cites if "numbers_input" in c), None)) else {}),
        **({"next_moves": card} if r.role == "assistant" and (card := next(
            (c["next_moves"] for c in cites if isinstance(c.get("next_moves"), dict)), None)) else {}),
        **({"gtm_search_progress": progress} if (progress := _search_progress(cites)) else {}),
        **({key: value for key, value in next((c["exploration_options"] for c in cites if "exploration_options" in c), {}).items()
            if key in {"round_id", "options", "comparison", "state", "selected_option_id", "execution_mode", "status"}}
           if r.kind == "exploration_options" else {}),
        **({"founder_references": references} if (references := next(
            (c["founder_references"] for c in cites if "founder_references" in c), None)) else {}),
        **({_RID: rid} if (rid := next((c[_RID] for c in cites if _RID in c), None)) else {}),
        **({_REPLY_TO: anchor} if (anchor := next((c[_REPLY_TO] for c in cites if _REPLY_TO in c), None)) else {}),
        **({"confirmed_action": receipt} if (receipt := next((c["confirmed_action"] for c in cites if "confirmed_action" in c), None)) else {}),
        "actions": actions,
        "created_at": (r.created_at.isoformat().replace("+00:00", "Z") + ("Z" if r.created_at.tzinfo is None else "")) if r.created_at else None,
    }
    from app.core.local_mode import is_sample_workspace
    if r.role == "assistant" and (sample if sample is not None else is_sample_workspace(r.workspace_id)):
        label = "Sample answer, not market evidence.\n\n"
        reply.update(sample=True, body_md=label + reply["body_md"], content=label + reply["content"],
            parts=[{"type": "text", "text": label.strip()}, *reply["parts"]])
    if r.role == "assistant":
        from app.chat.navigation_map import in_chat_reply
        from app.chat.reply_safety import guard_reply

        return guard_reply(getattr(r, "venture_id", None), in_chat_reply(reply))
    return reply

"""Chat GET/POST + confirm/execute (32 / 83)."""

from __future__ import annotations
from contextvars import copy_context
import hashlib
import json

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.concurrency import run_in_threadpool

from app.chat import proposals
from app.builder import execution as managed_builds
from app.credits.errors import CreditError, InsufficientCredits
from app.chat.blockers import with_explain
from app.chat.execute import execute_action
from app.chat.router import handle_message
from app.chat.store import append_message, idempotent_message, list_messages, page_messages
from app.db.models import Venture
from app.authz.workspace import assert_venture_access, subject_person_id
from app.analytics.events import emit_once
from app.state import db
from app.api.voice_api import reject_voice_confirm
from app.chat.conversation_routes import ConversationRoute


def _chat_venture(venture_id: str):
    with db()() as session:
        venture = session.get(Venture, venture_id)
        if venture is None:
            raise HTTPException(404, "not_found")
        return venture


def _check_gtm_proposal(venture_id: str, proposal_id: str, actor: str):
    from app.db.supymem_models import EntryChatProposal

    with db()() as session:
        row = session.get(EntryChatProposal, proposal_id)
        if row is None or row.actor_id != actor or row.target_venture_id != venture_id or row.route != "gtm":
            raise HTTPException(404, detail={"error": {"code": "gtm_proposal_not_found"}})


def _gtm_owner(venture_id: str, request: Request, x_subject: str | None) -> str:
    """The chat bridge requires the same signed actor as the GTM operation API."""
    if not x_subject:
        raise HTTPException(403, detail={"error": {"code": "gtm_actor_required"}})
    from app.admin.authn import verify_caller

    caller = verify_caller(request)
    assert_venture_access(venture_id, caller.sub)
    from app.db.supymem_models import EntryChatProposal
    from sqlalchemy import select
    with db()() as session:
        venture = session.get(Venture, venture_id)
        if venture is None:
            raise HTTPException(404, "not_found")
        proposals_for_venture = session.scalars(select(EntryChatProposal).where(
            EntryChatProposal.target_venture_id == venture_id,
        )).all()
        confirmed = any(row.actor_id == subject_person_id(caller.sub)
                        and row.route == "gtm" and row.state == "committed"
                        for row in proposals_for_venture)
        # Older ventures predate the entry chat. Their authenticated owner may
        # use the same scoped quote; entry-created ventures retain their route gate.
        if not confirmed:
            # A confirmed "jump to find customers" in chat is the same founder choice as the GTM skip.
            from app.ventures.journey_overrides import read as journey_choices
            confirmed = journey_choices(venture_id, session=session)["target"] == "find_customers"
        if proposals_for_venture and not confirmed:
            raise HTTPException(403, detail={"error": {"code": "gtm_route_required"}})
        return venture.workspace_id


def _gtm_error(exc: ValueError) -> HTTPException:
    code = str(exc)
    if not code.startswith("gtm_"):
        code = "gtm_invalid_input"
    status = 409 if code in {"gtm_quote_changed", "gtm_idempotency_conflict"} else 402 if code == "gtm_insufficient_credits" else 403 if code in {"gtm_actor_required", "gtm_write_forbidden"} else 404 if code in {"gtm_scope_mismatch", "gtm_not_found", "gtm_venture_unavailable"} else 422
    return HTTPException(status, detail={"error": {"code": code}})

router = APIRouter(tags=["chat"], route_class=ConversationRoute)
_UNKEYED_UNKNOWN_MESSAGE = "You do not have a confirmed result yet. Ask to check your saved work before trying again."


class NewConversationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=8, max_length=128)
    title: str = Field(default="New chat", max_length=120)


@router.get("/ventures/{venture_id}/chat/conversations")
def get_conversations(venture_id: str, current: str | None = Query(default=None, min_length=1, max_length=128),
                      archived: bool = False):
    from app.chat.conversations import list_conversations
    try:
        return list_conversations(venture_id, current=current, archived=archived)
    except PermissionError as exc:
        raise HTTPException(401, detail={"error": {"code": "chat_actor_required"}}) from exc


@router.post("/ventures/{venture_id}/chat/conversations")
def new_conversation(venture_id: str, payload: NewConversationBody):
    from app.chat.conversations import create
    try:
        return create(venture_id, idempotency_key=payload.idempotency_key, title=payload.title)
    except PermissionError as exc:
        raise HTTPException(401, detail={"error": {"code": "chat_actor_required"}}) from exc
    except ValueError as exc:
        raise HTTPException(409, detail={"error": {"code": str(exc)}}) from exc


class UpdateConversationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(default="New chat", min_length=1, max_length=120)
    pinned: bool = Field(default=False, strict=True)
    archived: bool = Field(default=False, strict=True)
    folder: str | None = Field(default=None, max_length=60)

    @field_validator("title", mode="before")
    @classmethod
    def trim_title(cls, value):
        return value.strip() if isinstance(value, str) else value


@router.patch("/ventures/{venture_id}/chat/conversations/{conversation_id}")
def update_conversation(venture_id: str, conversation_id: str, payload: UpdateConversationBody):
    from app.chat.conversations import update
    try:
        return update(venture_id, conversation_id, payload.model_dump(exclude_unset=True))
    except PermissionError as exc:
        raise HTTPException(401, detail={"error": {"code": "chat_actor_required"}}) from exc
    except ValueError as exc:
        raise HTTPException(404, detail={"error": {"code": str(exc)}}) from exc


class ChatBody(BaseModel):
    conversation_id: str | None = Field(default=None, max_length=128)
    message: str | None = Field(default=None, max_length=4000)
    body: str | None = Field(default=None, max_length=4000)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)
    reply_to_question: bool = Field(default=False, strict=True)
    question_id: str | None = Field(default=None, max_length=64)  # the question a clicked chip belongs to
    source: str | None = Field(default=None, max_length=16)
    job_id: str | None = Field(default=None, min_length=1, max_length=128)

    def text(self) -> str:
        return ((self.message or self.body or "").strip())[:4000]


class ConfirmBody(BaseModel):
    message_id: str | None = Field(default=None, min_length=1, max_length=128)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)
    conversation_id: str | None = Field(default=None, max_length=128)
    source: str | None = Field(default=None, max_length=16)
    action: str = Field(min_length=1, max_length=64)
    payload: dict | None = None


class GtmChatQuoteBody(BaseModel):
    conversation_id: str | None = Field(default=None, max_length=128)
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=8, max_length=160)
    description: str = Field(min_length=1, max_length=4000)
    idea: str = Field(min_length=1, max_length=12000)
    problem: str = Field(default="", max_length=12000)
    who: str = Field(default="", max_length=2000)
    users_count: int = Field(default=0, ge=0, le=1_000_000_000)


class GtmChatConfirmBody(BaseModel):
    conversation_id: str | None = Field(default=None, max_length=128)
    model_config = ConfigDict(extra="forbid")
    source: str | None = Field(default=None, max_length=16)
    quote_message_id: str = Field(min_length=1, max_length=128)


class GtmChatFollowupBody(BaseModel):
    conversation_id: str | None = Field(default=None, max_length=128)
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=4000)


class GtmChatRouteConfirmBody(BaseModel):
    conversation_id: str | None = Field(default=None, max_length=128)
    model_config = ConfigDict(extra="forbid")
    source: str | None = Field(default=None, max_length=16)
    proposal_id: str = Field(min_length=1, max_length=128)


@router.post("/ventures/{venture_id}/chat/gtm/route/confirm")
async def gtm_chat_route_confirm(venture_id: str, payload: GtmChatRouteConfirmBody,
                                 request: Request,
                                 x_subject: str | None = Header(default=None, alias="X-Subject")):
    """Commit only the GTM skip reviewed in this venture's chat."""
    reject_voice_confirm(payload.source)
    from app.admin.authn import verify_caller
    from app.entry import chat_start

    if not x_subject:
        raise HTTPException(403, detail={"error": {"code": "gtm_actor_required"}})
    actor = subject_person_id(verify_caller(request).sub)
    (await run_in_threadpool(assert_venture_access, venture_id, actor))
    await run_in_threadpool(_check_gtm_proposal, venture_id, payload.proposal_id, actor)
    try:
        await chat_start.confirm(payload.proposal_id, actor)
    except chat_start.ChatStartError as exc:
        raise HTTPException(exc.status, detail={"error": {"code": exc.code}}) from exc
    return (await run_in_threadpool(idempotent_message, venture_id, "assistant", f"entry:{payload.proposal_id}:assistant"))


@router.post("/ventures/{venture_id}/chat/gtm/followup")
async def gtm_chat_followup(venture_id: str, payload: GtmChatFollowupBody, request: Request,
                            x_subject: str | None = Header(default=None, alias="X-Subject")):
    """Keep GTM discussion in chat without entering the generic work router."""
    import re
    from app.chat.assistant import answer_question
    from app.chat.intent import looks_like_question, split_speech_acts, WORK_INTENTS
    from llm_gateway.sole import workspace_llm_context

    wid = (await run_in_threadpool(_gtm_owner, venture_id, request, x_subject))
    text = payload.message.strip()
    if not text:
        raise HTTPException(422, detail={"error": {"code": "message_required"}})
    acts = split_speech_acts(text)
    is_question = looks_like_question(text) and not any(intent in WORK_INTENTS for intent, _ in acts)
    # Read the last saved GTM state before recording this follow-up. Status
    # answers come from that receipt, including an open quote or pending run.
    latest = next((row for row in reversed((await run_in_threadpool(list_messages, venture_id, limit=100)))
                   if row.get("kind") in {"gtm_diagnosis_quote", "gtm_diagnosis_pending",
                                          "gtm_diagnosis_completed", "gtm_diagnosis_failed"}), None)
    (await run_in_threadpool(append_message, venture_id, role="user", body_md=text, kind="text"))
    if not is_question:
        body = ("I can discuss that here, but this message did not start work. "
                "Review the saved diagnosis action or ask for a new diagnosis quote after the current run resolves. "
                "Research, publishing, and outreach each need their own review.")
        return (await run_in_threadpool(append_message, venture_id, role="assistant", body_md=body, kind="text"))
    if re.search(r"\b(?:status|update|progress|pending|quote|running|finished|done)\b", text, re.I) and latest:
        body = f"Latest saved GTM state: {latest['body_md']}"
        return (await run_in_threadpool(append_message, venture_id, role="assistant", body_md=body, kind="text"))
    with workspace_llm_context(wid):
        reply = await answer_question(venture_id, text, workspace_id=wid)
    return (await run_in_threadpool(append_message, venture_id, role="assistant", body_md=reply.get("body_md") or
                          "I could not answer that from the saved company context.", kind="text",
                          citations=reply.get("citations"), actions=reply.get("actions"), parts=reply.get("parts")))


@router.post("/ventures/{venture_id}/chat/gtm/quote")
def gtm_chat_quote(venture_id: str, payload: GtmChatQuoteBody, request: Request,
                   x_subject: str | None = Header(default=None, alias="X-Subject")):
    from app.gtm.god import generation

    wid = _gtm_owner(venture_id, request, x_subject)
    quote_key = f"gtm-quote:{x_subject}:{payload.request_id}"
    input_key = f"gtm-input:{x_subject}:{payload.request_id}"
    input_data = {"kind": "diagnose", "idea": payload.idea.strip(),
                  "problem": payload.problem.strip(), "who": payload.who.strip(),
                  "users_count": payload.users_count}
    existing = idempotent_message(venture_id, "assistant", quote_key)
    if existing:
        original = idempotent_message(venture_id, "user", input_key)
        actions = existing.get("actions") or []
        quoted_input = actions[0].get("gtm_quote", {}).get("input") if actions else None
        if not original or original["body_md"] != payload.description.strip() or (quoted_input and quoted_input != input_data):
            raise HTTPException(409, detail={"error": {"code": "gtm_idempotency_conflict"}})
        emit_once("chat.message", event_key=f"gtmchat:{original['id']}:input",
                  person_id=subject_person_id(x_subject), workspace_id=wid,
                  venture_id=venture_id, props={"source": "chat", "route": "gtm"})
        if existing.get("kind") == "gtm_diagnosis_quote":
            return gtm_chat_confirm(venture_id, GtmChatConfirmBody(quote_message_id=existing["id"]), request, x_subject)
        return existing
    latest = next((row for row in reversed(list_messages(venture_id, limit=100))
                   if row.get("kind") in {"gtm_diagnosis_quote", "gtm_diagnosis_pending",
                                          "gtm_diagnosis_completed", "gtm_diagnosis_failed"}), None)
    if latest and latest["kind"] in {"gtm_diagnosis_quote", "gtm_diagnosis_pending"}:
        raise HTTPException(409, detail={"error": {"code": "gtm_quote_unresolved"}})
    try:
        runs = generation.recent(wid, venture_id)["runs"]
        if any(row["state"] in {"queued", "running", "needs_input"} or row["reserved_micro"] > 0 for row in runs):
            raise ValueError("gtm_run_unresolved")
        quote = generation.quote(wid, venture_id, input_data)
    except ValueError as exc:
        raise _gtm_error(exc) from None
    append_message(venture_id, role="user", body_md=payload.description.strip(), kind="text",
                   idempotency_key=input_key)
    body = ("This is one review using only what you have told me. Nothing is researched, published, or sent. "
             f"Your saved idea: {input_data['idea']}. Your request: {payload.description.strip()}.")
    action = {"label": "Show status", "action": "status", "gtm_quote": {"input": input_data, "quote": {
        key: quote[key] for key in ("input_digest", "resource_digest", "ceiling_micro")}}}
    if not quote["sufficient_now"]:
        body = "You have reached your usage limit. Open Settings to get more, then retry."
    saved = append_message(venture_id, role="assistant", kind="gtm_diagnosis_quote" if quote["sufficient_now"] else "gtm_diagnosis_failed",
                           body_md=body, actions=[action] if quote["sufficient_now"] else [
                               {"label": "Open Settings", "href": f"/v/{venture_id}/settings", "style": "primary"}],
                           idempotency_key=quote_key)
    original = idempotent_message(venture_id, "user", input_key)
    if original:
        emit_once("chat.message", event_key=f"gtmchat:{original['id']}:input",
                  person_id=subject_person_id(x_subject), workspace_id=wid,
                  venture_id=venture_id, props={"source": "chat", "route": "gtm"})
    return gtm_chat_confirm(venture_id, GtmChatConfirmBody(quote_message_id=saved["id"]), request, x_subject) if quote["sufficient_now"] else saved


@router.post("/ventures/{venture_id}/chat/gtm/confirm")
def gtm_chat_confirm(venture_id: str, payload: GtmChatConfirmBody, request: Request,
                     x_subject: str | None = Header(default=None, alias="X-Subject")):
    from app.db.models import ChatMessage
    from app.gtm.god import generation

    reject_voice_confirm(payload.source)
    wid = _gtm_owner(venture_id, request, x_subject)
    from app.chat.conversations import resolve
    from app.chat.store import _partition
    from sqlalchemy import select
    conversation = resolve(venture_id)
    with db()() as session:
        row = session.scalar(select(ChatMessage).where(
            ChatMessage.id == payload.quote_message_id,
            ChatMessage.venture_id == venture_id, _partition(conversation)))
        if row is None or row.venture_id != venture_id or row.kind != "gtm_diagnosis_quote":
            raise HTTPException(404, detail={"error": {"code": "gtm_quote_not_found"}})
        import json
        actions = json.loads(row.actions_json or "[]")
    approval = actions[0].get("gtm_quote") if len(actions) == 1 and isinstance(actions[0], dict) else None
    if not isinstance(approval, dict) or not isinstance(approval.get("input"), dict) or not isinstance(approval.get("quote"), dict):
        raise HTTPException(409, detail={"error": {"code": "gtm_quote_unreadable"}})
    q = approval["quote"]
    try:
        run = generation.start(wid, venture_id, approval["input"],
            idempotency_key="chat-gtm:" + payload.quote_message_id,
            input_digest=q["input_digest"], resource_digest=q["resource_digest"],
            ceiling_micro=q["ceiling_micro"])
    except ValueError as exc:
        saved = append_message(venture_id, role="assistant", kind="gtm_diagnosis_failed",
            body_md=("You have reached your usage limit. Open Settings to get more, then retry." if str(exc) == "gtm_insufficient_credits"
                     else "Your diagnosis could not start. Review your saved inputs and account settings, then try again."),
            actions=[{"label": "Open Settings", "href": f"/v/{venture_id}/settings", "style": "primary"}],
            citations=[{"confirmed_action": {"message_id": payload.quote_message_id, "action": "gtm.diagnosis.start", "state": "not_started"}}],
            idempotency_key=f"gtm-rejected:{payload.quote_message_id}:{str(exc)}")
        return saved
    saved = append_message(venture_id, role="assistant", kind="gtm_diagnosis_pending",
        body_md=(f"Your diagnosis is {run['state']}. "
                 "I will show its saved result here when it is ready."),
        citations=[{"job_started": {"job_id": run.get("job_id")}}, {"confirmed_action": {"message_id": payload.quote_message_id, "action": "gtm.diagnosis.start", "state": "started", "run_id": run["run_id"]}}],
        idempotency_key=f"gtm-started:{run['run_id']}")
    for event in ("chat.tool_invoked", "feature.used"):
        emit_once(event, event_key=f"gtmchat:{saved['id']}:{event.replace('.', '_')}",
                  person_id=subject_person_id(x_subject), workspace_id=wid,
                  venture_id=venture_id,
                  props={"source": "chat", "tool": "gtm.diagnosis.start", "feature": "gtm",
                         **({"outcome": "returned"} if event == "chat.tool_invoked" else {})})
    return saved


@router.get("/ventures/{venture_id}/chat/gtm/status")
def gtm_chat_status(venture_id: str, request: Request,
                    x_subject: str | None = Header(default=None, alias="X-Subject")):
    from app.gtm.god import generation

    wid = _gtm_owner(venture_id, request, x_subject)
    try:
        runs = generation.recent(wid, venture_id)["runs"]
    except ValueError as exc:
        raise _gtm_error(exc) from None
    if not runs:
        return {"run": None, "message": None}
    run = runs[0]
    if run["state"] in {"queued", "running"}:
        return {"run": run, "message": None}
    diagnosis = (run.get("result") or {}).get("diagnosis") or {}
    if run["state"] == "completed" and diagnosis:
        body = (f"The provisional GTM diagnosis is ready. **Bottleneck:** {diagnosis.get('bottleneck', '')}\n\n"
                f"{diagnosis.get('rationale', '')}\n\n")
        for title, key in (("Assumptions", "assumptions"), ("Evidence gaps", "evidence_gaps")):
            values = diagnosis.get(key) or []
            if values:
                body += f"{title}: " + "; ".join(str(value) for value in values) + "\n\n"
        next_actions = diagnosis.get("next_actions") or []
        if next_actions:
            body += "Suggested next steps: " + "; ".join(
                f"{item.get('instruction', '')} ({item.get('reason', '')})" for item in next_actions) + "\n\n"
        body += "These steps are advisory. No outreach, research, or publication was started."
        kind = "gtm_diagnosis_completed"
    else:
        body = ("You have reached your usage limit. Open Settings to get more, then retry."
                if run.get("error_code") == "gtm_insufficient_credits" else
                f"Your diagnosis is {run['state']}. Review its saved status before another attempt.")
        kind = "gtm_diagnosis_failed"
    saved = append_message(venture_id, role="assistant", kind=kind, body_md=body,
        idempotency_key=f"gtm-final:{run['run_id']}:{run['state']}")
    return {"run": run, "message": saved}


@router.get("/ventures/{venture_id}/chat/requests/{nonce}")
def chat_request_status(venture_id: str, nonce: str):
    from app.chat import requests as chat_requests

    try:
        return chat_requests.status(venture_id, nonce)
    except chat_requests.ChatRequestError as exc:
        raise HTTPException(exc.status, detail={"error": {"code": exc.code}}) from exc


@router.get("/ventures/{venture_id}/chat")
def get_chat(venture_id: str, background_tasks: BackgroundTasks, before: str | None = None):
    from app.chat.conversations import current_conversation, resolve
    from app.core.local_mode import is_local

    scope = current_conversation()
    if scope is None:
        with db()() as session:
            if not session.get(Venture, venture_id):
                raise HTTPException(404, "not_found")
        scope = resolve(venture_id, create=False)
    try:
        items, next_cursor = page_messages(venture_id, before=before)
    except ValueError as exc:
        raise HTTPException(404, detail={"error": {"code": "chat_cursor_not_found"}}) from exc
    from app.chat.site_actions import refresh_page_actions
    items = refresh_page_actions(items, venture_id)
    if before is None:
        if is_local():
            from app.chat.next_moves import post
            background_tasks.add_task(copy_context().run, post, venture_id, conversation_id=scope.id)
        else:
            from app.chat.proactive import on_chat_open
            background_tasks.add_task(copy_context().run, on_chat_open, venture_id)
    return {"items": items, "next_cursor": next_cursor, "conversation_id": scope.id}


@router.post("/ventures/{venture_id}/chat")
async def post_chat(venture_id: str, payload: ChatBody, request: Request,
                    x_subject: str | None = Header(default=None, alias="X-Subject")):
    ven = await run_in_threadpool(_chat_venture, venture_id)
    workspace_id = ven.workspace_id
    text = payload.text()
    if not text and not (payload.message or payload.body or "").isspace():
        raise HTTPException(422, detail={"error": {"code": "message_required"}})
    # The gateway supplies the authenticated subject. Legacy local callers
    # without one still work, but cannot mint person-attributed analytics.
    actor_subject = None
    caller = None
    if x_subject:
        from app.admin.authn import verify_caller

        caller = verify_caller(request)
        (await run_in_threadpool(assert_venture_access, venture_id, caller.sub))
        actor_subject = subject_person_id(caller.sub)
    from app.intake.evidence import INPUT_KIND, save_statement
    origin = "voice" if (payload.source or "").strip().lower() == "voice" else "typed"
    (await run_in_threadpool(save_statement, venture_id, text, source_kind=origin))
    if origin == "voice":
        # ChatMessage has no metadata column; record voice origin in analytics only.
        from app.analytics.events import emit
        (await run_in_threadpool(emit, "feature.used", person_id=actor_subject, workspace_id=workspace_id,
             venture_id=venture_id, props={"feature": "voice_chat", "source": "voice"}))
    from app.chat.idea_bridge import pending_question, split_question_reply
    if actor_subject and not payload.reply_to_question:
        from app.chat import customers
        from app.chat.intent import classify_intent, requested_customer_search, _START_GETTING_USERS_RE, _GTM_DIAGNOSE_RE
        from app.ventures.state_brief import get_venture_state_brief

        if (classify_intent(text) == "gtm_review" and (requested_customer_search(text)
                or _START_GETTING_USERS_RE.match(text) or _GTM_DIAGNOSE_RE.match(text))
                and not ((waiting := await run_in_threadpool(pending_question, venture_id, "", force=True))
                    and all(await run_in_threadpool(split_question_reply, text)))):
            from app.chat.router import _continue_clarifying
            search = requested_customer_search(text)
            diagnose = not search and (bool(_GTM_DIAGNOSE_RE.match(text))
                or not ((await run_in_threadpool(get_venture_state_brief, venture_id, force=True)).get("gtm") or {}).get("present"))
            try:
                if search:
                    from app.chat.execute import local_capability_reply
                    if blocked := await run_in_threadpool(local_capability_reply, "find_customers"):
                        request_id = f"chat-gtm-skip:{venture_id}:{payload.idempotency_key or __import__('uuid').uuid4().hex}"
                        await run_in_threadpool(append_message, venture_id, role="user", body_md=text,
                                               idempotency_key=f"{request_id}:input")
                        return await run_in_threadpool(append_message, venture_id, role="assistant", body_md=blocked["body_md"],
                                                       actions=blocked["actions"], citations=blocked["citations"],
                                                       idempotency_key=request_id)
                if diagnose:
                    current = next((row for row in reversed((await run_in_threadpool(list_messages, venture_id, limit=100)))
                                    if row.get("kind") in {"gtm_diagnosis_quote", "gtm_diagnosis_pending",
                                                           "gtm_diagnosis_completed", "gtm_diagnosis_failed"}), None)
                    if current and current["kind"] in {"gtm_diagnosis_quote", "gtm_diagnosis_pending"}:
                        return current
                    from app.ventures.current_idea import current_idea

                    return (await run_in_threadpool(gtm_chat_quote, venture_id, GtmChatQuoteBody(
                        request_id=payload.idempotency_key or __import__("uuid").uuid4().hex,
                        description=text, idea=(await run_in_threadpool(current_idea, ven))[:12000], problem=text,
                    ), request, x_subject))
                if search and not (await run_in_threadpool(customers._view, venture_id)).get("discovery_started"):
                    (await run_in_threadpool(_gtm_owner, venture_id, request, x_subject))
            except HTTPException as exc:
                if (exc.status_code == 403 and isinstance(exc.detail, dict)
                        and exc.detail.get("error", {}).get("code") == "gtm_route_required"):
                    from app.entry import chat_start

                    request_id = f"chat-gtm-skip:{venture_id}:{payload.idempotency_key or __import__('uuid').uuid4().hex}"
                    try:
                        preview = (await run_in_threadpool(chat_start.preview, text=text, actor_id=actor_subject,
                                                     target_venture_id=venture_id,
                                                     request_id=request_id))
                    except chat_start.ChatStartError as error:
                        raise HTTPException(error.status, detail={"error": {"code": error.code}}) from error
                    if preview.get("status") == "reply":
                        (await run_in_threadpool(append_message, venture_id, role="user", body_md=text, idempotency_key=f"{request_id}:input"))
                        reply = (await run_in_threadpool(append_message, venture_id, role="assistant", body_md=preview["body_md"],
                                              actions=preview["actions"], idempotency_key=request_id))
                        return (await run_in_threadpool(_continue_clarifying, venture_id, reply)) if waiting else reply
                    proposal = preview.get("proposal")
                    if not proposal or proposal["route"] != "gtm":
                        raise HTTPException(422, detail={"error": {"code": "gtm_skip_unavailable"}})
                    if search:
                        # The founder asked for people: no skip card, no replayed message, the search just starts.
                        try:
                            await chat_start.confirm(proposal["id"], actor_subject)
                        except chat_start.ChatStartError as error:
                            raise HTTPException(error.status, detail={"error": {"code": error.code}}) from error
                        from app.chat.confirmations import start_requested_work

                        reply = await run_in_threadpool(idempotent_message, venture_id, "assistant", f"entry:{proposal['id']}:assistant")
                        return await start_requested_work(venture_id, reply, actor_subject, actions={"gtm.find"})
                    key = f"gtm-skip:{proposal['id']}"
                    existing = (await run_in_threadpool(idempotent_message, venture_id, "assistant", key))
                    if existing:
                        return (await run_in_threadpool(_continue_clarifying, venture_id, existing)) if waiting else existing
                    (await run_in_threadpool(append_message, venture_id, role="user", body_md=text, kind="text",
                                   idempotency_key=f"gtm-skip-input:{proposal['id']}"))
                    reply = (await run_in_threadpool(append_message, venture_id, role="assistant", kind="gtm_skip_proposal",
                                          body_md=proposal["message"],
                                          actions=[{"label": "Skip to go-to-market", "confirmation": {
                                              "action": "gtm.route.confirm",
                                              "interpretation": proposal["message"],
                                              "consequences": ["Skip idea validation and build setup for this venture.",
                                                               "No outreach or publication will start."],
                                              "resulting_state": {"proposal_id": proposal["id"]},
                                          }}], idempotency_key=key))
                    return (await run_in_threadpool(_continue_clarifying, venture_id, reply)) if waiting else reply
                if not diagnose or exc.status_code not in {402, 403, 409, 422}:
                    raise
    from app.chat import requests as chat_requests

    admission = None
    reply = None
    try:
        if actor_subject:
            from core.ids import new_id
            # A client without a key still needs an admission id: hosted replies refuse to dial without one, leaving an empty reply.
            admission, replay = (await run_in_threadpool(chat_requests.admit, venture_id, actor_subject, payload.idempotency_key or new_id("chatreq"),
                {"text": text, "reply_to_question": payload.reply_to_question,
                 "question_id": payload.question_id, "source": payload.source,
                 **({"job_id": payload.job_id} if payload.job_id else {})}))
            if replay is not None:
                from app.chat.navigation_map import in_chat_reply
                return in_chat_reply(replay)
        from app.chat.browser_prepare import bind_caller
        from app.chat.store import CLIENT_REQUEST_ID
        token = CLIENT_REQUEST_ID.set(payload.idempotency_key)
        input_token = INPUT_KIND.set(origin)
        try:
            with bind_caller(caller):
                reply = await _generic_reply(venture_id, payload, text, actor_subject, workspace_id,
                                             request_id=admission.id if admission else None)
        finally:
            CLIENT_REQUEST_ID.reset(token)
            INPUT_KIND.reset(input_token)
        from app.chat.navigation_map import in_chat_reply
        saved_reply = (await run_in_threadpool(chat_requests.complete, admission, reply)) if admission else reply
        try:
            if await request.is_disconnected():
                import logging

                logging.getLogger(__name__).error("chat_reply_after_disconnect", extra={
                    "reason_code": "chat_reply_after_disconnect", "venture_id": venture_id,
                    "chat_request_id": admission.id if admission else None})
        except Exception as exc:
            from app.chat.recovery import log_founder_exception

            log_founder_exception(exc, venture_id, "chat.request.disconnect_check")
        return in_chat_reply(saved_reply)
    except BaseException as exc:
        from app.chat.recovery import log_founder_exception, request_outcome_unknown

        log_founder_exception(exc, venture_id, "chat.request")
        if admission:
            (await run_in_threadpool(chat_requests.uncertain, admission, reply))
            if isinstance(exc, Exception):
                if not payload.idempotency_key:
                    (await run_in_threadpool(assert_venture_access, venture_id, actor_subject))
                    key = "chat-request-failure:" + admission.id
                    saved = await run_in_threadpool(idempotent_message, venture_id, "assistant", key)
                    if saved is None:
                        failure = request_outcome_unknown()["error"]
                        saved = await run_in_threadpool(append_message, venture_id, role="assistant", kind="error",
                            body_md=_UNKEYED_UNKNOWN_MESSAGE,
                            actions=[{**action, "label": "Check saved work"} for action in failure["actions"]],
                            idempotency_key=key)
                    from app.chat.navigation_map import in_chat_reply
                    return in_chat_reply(saved)
                # A handler can have saved work before any apparently ordinary
                # HTTP refusal. Never advertise it as safe to mint a new nonce.
                raise HTTPException(409, detail=request_outcome_unknown()) from None
        if isinstance(exc, chat_requests.ChatRequestError):
            if exc.code == "chat_request_outcome_unknown":
                raise HTTPException(exc.status, detail=request_outcome_unknown()) from None
            raise HTTPException(exc.status, detail={"error": {"code": exc.code}}) from exc
        raise


async def _generic_reply(venture_id, payload, text, actor_subject, workspace_id, *, request_id: str | None = None):
    from llm_gateway.errors import ProviderCallError
    from llm_gateway.sole import workspace_llm_context
    from app.core.local_mode import is_local
    from app.llm.provider_errors import confirmed_provider_rejection, provider_error_for_user
    from app.chat import recovery

    try:
        with workspace_llm_context(workspace_id):
            from app.analytics.chat_usage import attributed_chat_action

            with attributed_chat_action(actor_subject, workspace_id, venture_id):
                reply = await handle_message(venture_id, text, actor_subject=actor_subject,
                                        reply_to_question=payload.reply_to_question,
                                        question_id=payload.question_id, request_id=request_id,
                                        **({"job_id": payload.job_id} if payload.job_id else {}))
        from app.org.characters import parse_speaker
        if (not isinstance(reply, dict) or not reply.get("id") or reply.get("role", "assistant") != "assistant"
                or not isinstance(reply.get("body_md"), str) or not parse_speaker(reply["body_md"])[1].strip()):
            raise ValueError("chat_reply_missing")
        from app.chat.confirmations import auto_dispatch
        return await auto_dispatch(venture_id, reply, actor_subject)
    except ProviderCallError as exc:
        from app.llm import founder_provider

        recovery.log_founder_exception(exc, venture_id, "chat.provider")
        local = is_local()
        hosted = not local and (await run_in_threadpool(founder_provider.resolve_llm_config, workspace_id)).source != "founder_override"
        exc.hosted_chat_failure = hosted
        err = provider_error_for_user(exc)
        local_retry = local and err["retryable"]
        retry_actions = []
        if local_retry:
            retry_payload = {"text": text, **({"question_id": payload.question_id} if payload.question_id else {}),
                             **({"job_id": payload.job_id} if payload.job_id else {})}
            retry_actions = [{"label": "Try again", "action": "chat.reply", "payload": retry_payload, "style": "primary"}]
        elif hosted and err["retryable"]:
            retry_actions = [{"label": "Try again", "action": "chat.model_retry", "payload": {"text": text}, "style": "primary"}]
        fenced = (exc.code != "provider_output_invalid" and not confirmed_provider_rejection(exc)
                  and not local_retry
                  and not (hosted and getattr(exc, "platform_absorbed", False)))
        body = f"{'' if hosted or local else '**AI provider issue:** '}{err['message']}"
        if hint := err.get("hint"):
            body += f"\n\n{hint}"
        if fenced:
            failure = recovery.request_outcome_unknown()["error"]
            if not local:
                body = failure["message"] if getattr(payload, "idempotency_key", None) else _UNKEYED_UNKNOWN_MESSAGE
            else:
                body += " Check your saved work before trying again. Nothing was retried."
            retry_actions = failure["actions"]
        saved = (await run_in_threadpool(append_message,
            venture_id,
            role="assistant",
            body_md=body,
            kind="error",
            idempotency_key="chat-request-failure:" + request_id if request_id else None,
            actions=with_explain({"actions": retry_actions}, "llm_unavailable")["actions"]
            + [recovery.button("chat.provider", "provider_failed", "", "llm_unavailable")],
        ))
        (await run_in_threadpool(recovery.record, venture_id, kind="provider_failed", key=saved["id"], workspace_id=workspace_id,
                        failure={"step": "chat.provider", "code": "llm_unavailable"}))
        try:
            from app.analytics.chat_usage import record_provider_error_reply

            (await run_in_threadpool(record_provider_error_reply, actor_subject, workspace_id, venture_id))
        except Exception as error:  # noqa: BLE001
            recovery.log_founder_exception(error, venture_id, "chat.provider.telemetry")
            # A saved founder reply must survive a broken telemetry writer.
            pass
        if exc.code == "provider_output_invalid":
            return {**saved, "provider_error": exc.code}
        if hosted and getattr(exc, "platform_absorbed", False):
            return saved
        if fenced:
            # Saving an error card does not confirm the provider or credit
            # outcome. Retain the exact journal/key for explicit reconciliation.
            raise HTTPException(409, detail=recovery.request_outcome_unknown()) from None
        return saved
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - a crashed reply still gets a plain note and a way forward
        return (await run_in_threadpool(_action_failed_reply, venture_id, "chat.reply", {"text": text, "question_id": payload.question_id,
            **({"job_id": payload.job_id} if payload.job_id else {})}, exc, actor_subject, reply=True))


def _action_failed_reply(venture_id: str, action: str, args: dict, exc: Exception, actor_subject: str | None,
                         reply: bool = False):
    """A chat action or reply crashed on our side: log it, say so plainly and keep the venture moving."""
    from app.chat import recovery
    from app.chat.actions import REGISTRY
    from app.analytics.error_events import log_error_event
    from core.ids import new_id

    code = "chat_reply_failed" if reply else "chat_action_failed"
    log_error_event("chat", feature="chat", code=f"{code}:{type(exc).__name__}", venture_id=venture_id,
                    person_id=actor_subject, level=40)
    retry = REGISTRY.get(action)
    failed_intent = action
    if reply:
        from app.chat.intent import classify_intent
        failed_intent = classify_intent(str(args.get("text") or ""))
    step = {"idea.pressure_test.prepare": "idea.board", "idea.pressure_test": "idea.board", "ceo_review": "ceo_review"}.get(failed_intent, "chat.action")
    actions = [{"label": "Retry", "action": action, "payload": args, "style": "primary"}] \
        if retry and not retry.consequential and not action.startswith("recovery.") else []
    if reply:
        actions = [{"label": "Retry", "action": "chat.reply", "payload": args, "style": "primary"}]
    key = new_id("act")
    if not actions:
        actions = [{**recovery.button(step, "action_failed", key, code), "label": "Retry"}]
    failure = recovery.founder_failure(exc, venture_id, "your reply" if reply else "your requested action", actions[0],
        safety="Your saved work is kept." + (" Review the saved result before approving any new work." if not reply else ""))
    recovery.post_failure(
        venture_id, step=step, key=key, kind="action_failed", code=code, job_id=key, actions=failure["actions"], requested=True,
        body_md=failure["body_md"])
    if reply:
        from app.analytics.events import emit

        emit("chat.reply_error", person_id=actor_subject, venture_id=venture_id,
             props={"source": "chat", "failure_kind": "handler_exception"})
    saved = idempotent_message(venture_id, "assistant", key)
    if saved is None:  # the failure note could not be saved: surface the real error rather than hide it
        raise exc
    return saved


def _confirm_failed(venture_id: str, action: str, reason: str, status: int) -> None:
    try:
        from app.analytics.chat_usage import record_confirm_failure

        record_confirm_failure(venture_id, action, reason, status)
    except Exception as exc:  # noqa: BLE001 - telemetry never changes the founder's error
        from app.chat.recovery import log_founder_exception

        log_founder_exception(exc, venture_id, "chat.confirm.telemetry")
        pass


@router.post("/ventures/{venture_id}/chat/confirm")
async def confirm_chat_action(venture_id: str, payload: ConfirmBody, request: Request,
                              x_subject: str | None = Header(default=None, alias="X-Subject")):
    """Execute a consequential chat action after the founder confirms in the dock."""
    venture = await run_in_threadpool(_chat_venture, venture_id)
    workspace_id = venture.workspace_id
    actor_subject = None
    if x_subject:
        from app.admin.authn import verify_caller

        caller = verify_caller(request)
        (await run_in_threadpool(assert_venture_access, venture_id, caller.sub))
        actor_subject = subject_person_id(caller.sub)
    from app.ventures.journey_overrides import ACTIONS as journey_actions
    if payload.action in journey_actions and request.headers.get("x-workspace-id", workspace_id) != workspace_id:
        raise HTTPException(403, detail={"code": "journey_scope_mismatch"})
    return await _confirm_action(venture_id, payload, workspace_id, actor_subject,
                                 request_nonce=request.headers.get("Idempotency-Key"))


async def _confirm_action(venture_id, payload, workspace_id, actor_subject, *, request_nonce=None, standing_grant_id=None):
    from app.chat import confirmations, requests
    from app.chat.conversations import actor_id
    from app.core.local_mode import is_local

    actor = actor_subject or actor_id()
    if actor:
        actor = subject_person_id(actor)
    if payload.action == "autopilot.send_cap":
        from app.authz.roles import role_allows
        from app.chat.actions import get_action
        from app.workspaces.service import role_of

        (await run_in_threadpool(assert_venture_access, venture_id, actor))
        if not role_allows((await run_in_threadpool(role_of, workspace_id, subject_person_id(actor))), get_action(payload.action).required_permission):
            raise HTTPException(403, detail={"error": {"code": "change_autonomy_required"}})
    reject_voice_confirm(payload.source)
    admission = None
    confirmed_card = None
    nonce = payload.idempotency_key or request_nonce
    # Experiment decisions change or create ventures: they run only from a saved confirmation card.
    if (standing_grant_id or not is_local() and payload.action in {"experiment.persevere", "experiment.pivot", "experiment.spinoff", "experiment.kill"}) \
            and not payload.message_id:
        raise HTTPException(400, detail={"error": {"code": "confirmation_card_required"}})
    if payload.message_id:
        try:
            confirmed_card = (await run_in_threadpool(confirmations.card, venture_id, payload.message_id, payload.action,
                payload.payload or {}, actor_subject=actor))
        except ValueError as exc:
            if str(exc) == "chat_confirmation_changed" and actor and not standing_grant_id:
                key = f"{payload.message_id}:{payload.action}"
                digest = hashlib.sha256(json.dumps(payload.payload or {}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                for identity in (key, key + ":" + digest):
                    receipt_nonce = "confirm:" + hashlib.sha256(identity.encode()).hexdigest()
                    # The receipt only answers the exact request that ran: a changed payload stays refused.
                    if not await run_in_threadpool(requests.matches, venture_id, actor, receipt_nonce,
                            {"action": payload.action, "payload": payload.payload or {}, "message_id": payload.message_id}):
                        continue
                    receipt = await run_in_threadpool(idempotent_message, venture_id, "assistant", receipt_nonce)
                    if receipt and (receipt.get("confirmed_action") or {}).get("state") == "started":
                        return receipt
            raise HTTPException(409, detail={"error": {"code": str(exc)}}) from exc
        confirmation = next((item["confirmation"] for item in confirmed_card["actions"]
            if isinstance(item, dict) and isinstance(item.get("confirmation"), dict)
            and item["confirmation"].get("action") == payload.action
            and item["confirmation"].get("resulting_state", {}) == (payload.payload or {})), {})
        key = f"{payload.message_id}:{payload.action}"
        same_action_cards = sum(1 for item in confirmed_card["actions"] if isinstance(item, dict)
            and isinstance(item.get("confirmation"), dict) and item["confirmation"].get("action") == payload.action)
        if same_action_cards > 1:
            key += ":" + hashlib.sha256(json.dumps(payload.payload or {}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        nonce = ("standing-confirm:" if standing_grant_id else "confirm:") + hashlib.sha256(
            (f"{standing_grant_id}:{key}" if standing_grant_id else key).encode()).hexdigest()
        if confirmation.get("state") == "not_started":
            # A new click carries its own key; a replay without one answers from the saved receipt.
            nonce = payload.idempotency_key or request_nonce or nonce
        elif confirmation.get("state") is not None:
            nonce = confirmation.get("request_nonce") or nonce
        if same_action_cards > 1:
            payload = payload.model_copy(update={"idempotency_key": nonce})
    if standing_grant_id:
        from app.approvals import standing

        grant = (await run_in_threadpool(standing.matching_grant, workspace_id=workspace_id, venture_id=venture_id,
            founder_id=actor, action=standing.card_action(payload.action, payload.payload or {})))
        if grant != standing_grant_id:
            if not confirmation.get("state"):
                return confirmed_card
            raise HTTPException(409, detail={"error": {"code": "standing_approval_unavailable"}})
    if nonce and confirmed_card:
        receipt = (await run_in_threadpool(idempotent_message, venture_id, "assistant", nonce))
        if receipt:
            return receipt
    if nonce and actor:
        try:
            admission, replay = (await run_in_threadpool(requests.admit, venture_id, actor, nonce,
                {"action": payload.action, "payload": payload.payload or {}, "message_id": payload.message_id}))
            if replay is not None:
                return replay
        except requests.ChatRequestError as exc:
            if exc.code in {"chat_request_in_progress", "chat_request_outcome_unknown"}:
                receipt = (await run_in_threadpool(idempotent_message, venture_id, "assistant", nonce))
                if receipt:
                    return receipt
            raise HTTPException(exc.status, detail={"error": {"code": exc.code}}) from exc
    try:
        if confirmed_card:
            (await run_in_threadpool(confirmations.restore, [confirmed_card], venture_id))
            if confirmation.get("state") == "started":
                existing = (await run_in_threadpool(append_message, venture_id, role="assistant", body_md="This action already started. Check its saved result.",
                    idempotency_key=nonce, citations=[{"confirmed_action": {"message_id": payload.message_id,
                        "action": payload.action, "run_id": confirmation.get("run_id")}}]))
                return (await run_in_threadpool(requests.complete, admission, existing)) if admission else existing
            try:
                (await run_in_threadpool(confirmations.mark, venture_id, payload.message_id, payload.action, "starting", payload.payload or {}, request_nonce=nonce))
            except ValueError as exc:
                raise HTTPException(409, detail={"error": {"code": str(exc)}}) from exc
        return await _execute_confirmed_action(venture_id, payload, workspace_id, actor, nonce, admission,
            **({"standing_grant_id": standing_grant_id} if standing_grant_id else {}))
    finally:
        if admission:
            (await run_in_threadpool(requests.uncertain, admission))


async def _execute_confirmed_action(venture_id, payload, workspace_id, actor_subject, nonce, admission, *, standing_grant_id=None):
    from copy import deepcopy
    from app.approvals.service import ApprovalError
    from app.core.local_mode import is_local

    saved_state = deepcopy(payload.payload or {})
    try:
        from app.analytics.chat_usage import attributed_chat_action

        with attributed_chat_action(actor_subject, workspace_id, venture_id):
            result = await execute_action(venture_id, payload.action, payload=payload.payload or {},
                                          actor_subject=actor_subject, request_nonce=payload.idempotency_key or nonce,
                                          **({"confirmation_message_id": payload.message_id} if standing_grant_id or payload.action in {"autopilot.send_cap", "connections.disconnect", "routines.approve"} else {}),
                                          **({"standing_grant_id": standing_grant_id} if standing_grant_id else {}),
                                          **({"founder_initiated": True} if payload.action == "job.retry" else {}))
    except PermissionError as exc:
        (await run_in_threadpool(_confirm_failed, venture_id, payload.action, "forbidden", 403))
        raise HTTPException(403, detail={"error": {"code": str(exc)}}) from exc
    except KeyError as exc:
        (await run_in_threadpool(_confirm_failed, venture_id, payload.action, "not_found", 404))
        raise HTTPException(404, detail={"error": {"code": str(exc)}}) from exc
    except managed_builds.BuildError as exc:
        (await run_in_threadpool(_confirm_failed, venture_id, payload.action, "build_error", exc.status))
        raise HTTPException(exc.status, detail={"error": {"code": exc.code, "reasons": exc.reasons}}) from exc
    except InsufficientCredits as exc:
        (await run_in_threadpool(_confirm_failed, venture_id, payload.action, "insufficient_credits", 402))
        raise HTTPException(402, detail={"error": {"code": exc.code}}) from exc
    except CreditError as exc:
        if exc.code == "local_spend_limit":
            (await run_in_threadpool(_confirm_failed, venture_id, payload.action, exc.code, 402))
            raise HTTPException(402, detail={"error": {"code": exc.code, "message": str(exc)}}) from exc
        (await run_in_threadpool(_confirm_failed, venture_id, payload.action, "credit_error", 409))
        raise HTTPException(409, detail={"error": {"code": exc.code}}) from exc
    except ValueError as exc:
        if standing_grant_id and isinstance(exc, ApprovalError):
            from app.chat import confirmations, requests

            (await run_in_threadpool(confirmations.mark, venture_id, payload.message_id, payload.action, None, saved_state))
            saved = (await run_in_threadpool(confirmations.card, venture_id, payload.message_id, payload.action,
                saved_state, actor_subject=actor_subject))
            return (await run_in_threadpool(requests.complete, admission, saved)) if admission else saved
        if is_local() and str(exc) == "experiment_decision_invalid":
            (await run_in_threadpool(_confirm_failed, venture_id, payload.action, "experiment_decision_invalid", 400))
            raise HTTPException(400, detail={"error": {"code": "experiment_decision_invalid",
                "message": "Your market decision changed. Open your market map to review your saved results and choose again."}}) from exc
        (await run_in_threadpool(_confirm_failed, venture_id, payload.action, "unknown_action", 400))
        raise HTTPException(400, detail={"error": {"code": "unknown_action", "message": str(exc)}}) from exc
    except HTTPException:
        raise
    except Exception as exc:
        (await run_in_threadpool(_confirm_failed, venture_id, payload.action, "handler_exception", 500))
        return (await run_in_threadpool(_action_failed_reply, venture_id, payload.action, payload.payload or {}, exc, actor_subject))
    from app.org.characters import stamp_speaker
    from app.org.manager import manager_speaker

    confirmation_state = "starting" if result.get("action_started", True) is None else "started" if result.get("action_started") is True else "not_started" if result.get("action_started") is False or result.get("kind") in {"needs_you", "confirmation"} else "started"
    if standing_grant_id and confirmation_state == "started":
        result = {**result, "body_md": (result.get("body_md") or "Done.") +
            "\n\nDone automatically under your standing approval. You can undo running work or revoke below.",
            "actions": [*(result.get("actions") or []),
                *([{"label": "Undo if still running", "action": "chat.stop", "payload": {"job_ids": [result["run_id"]], "nonce": nonce},
                    "style": "secondary"}] if result.get("run_id") else []),
                {"label": "Revoke standing approval", "action": "approvals.revoke",
                 "payload": {"approval_id": standing_grant_id}, "style": "secondary"}],
            "citations": [*(result.get("citations") or []), {"standing_approval_id": standing_grant_id}]}
    saved = (await run_in_threadpool(append_message,
        venture_id,
        role="assistant",
        body_md=stamp_speaker(result.get("body_md") or "Done.", (await run_in_threadpool(manager_speaker, venture_id))),
        kind=result.get("kind") or "text",
        citations=[*(result.get("citations") or []), *([{"client_request_id": nonce}] if nonce else []), *([{"confirmed_action": {
            "message_id": payload.message_id, "action": payload.action, "run_id": result.get("run_id"),
            "state": confirmation_state}}] if payload.message_id else [])],
        actions=result.get("actions"),
        idempotency_key=nonce or result.get("idempotency_key"),
    ))
    if payload.message_id and confirmation_state != "starting":
        from app.chat.confirmations import mark
        (await run_in_threadpool(mark, venture_id, payload.message_id, payload.action, confirmation_state, saved_state))
    from app.chat import requests
    if not standing_grant_id:
        from app.chat.confirmations import auto_dispatch
        saved = await auto_dispatch(venture_id, saved, actor_subject)
    return (await run_in_threadpool(requests.complete, admission, saved)) if admission else saved


# Exact proposals use a separate contract while the legacy dock is migrated.
# The only executable action here is reviewed plan approval.


class ProposalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str
    plan_id: str = Field(min_length=1, max_length=128)


class ProposalDecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str | None = Field(default=None, max_length=16)


def proposal_call(fn, *args):
    try:
        return fn(*args)
    except KeyError as exc:
        raise HTTPException(404, detail={"error": {"code": "chat_proposal_not_found"}}) from exc
    except PermissionError as exc:
        raise HTTPException(403, detail={"error": {"code": "chat_proposal_forbidden"}}) from exc
    except ValueError as exc:
        raise HTTPException(409, detail={"error": {"code": "chat_proposal_review_required"}}) from exc


@router.post("/ventures/{venture_id}/chat/proposals")
def prepare_chat_proposal(venture_id: str, body: ProposalBody,
                          x_subject: str | None = Header(default=None, alias="X-Subject")):
    if body.action != "plan.approve":
        raise HTTPException(422, detail={"error": {"code": "chat_action_not_supported"}})
    return proposal_call(proposals.prepare_plan_approval, venture_id, x_subject, body.plan_id)


@router.get("/ventures/{venture_id}/chat/proposals")
def list_chat_proposals(venture_id: str, before: str | None = None,
                        x_subject: str | None = Header(default=None, alias="X-Subject")):
    return proposal_call(proposals.list_recent, venture_id, x_subject, before)


@router.get("/ventures/{venture_id}/chat/proposals/{proposal_id}")
def get_chat_proposal(venture_id: str, proposal_id: str,
                      x_subject: str | None = Header(default=None, alias="X-Subject")):
    return proposal_call(proposals.get, venture_id, x_subject, proposal_id)


@router.post("/ventures/{venture_id}/chat/proposals/{proposal_id}/confirm")
def confirm_exact_chat_proposal(venture_id: str, proposal_id: str, body: ProposalDecisionBody,
                                x_subject: str | None = Header(default=None, alias="X-Subject")):
    reject_voice_confirm(body.source)
    return proposal_call(proposals.confirm, venture_id, x_subject, proposal_id)


@router.post("/ventures/{venture_id}/chat/proposals/{proposal_id}/cancel")
def cancel_chat_proposal(venture_id: str, proposal_id: str, body: ProposalDecisionBody,
                         x_subject: str | None = Header(default=None, alias="X-Subject")):
    return proposal_call(proposals.cancel, venture_id, x_subject, proposal_id)


@router.post("/ventures/{venture_id}/chat/proposals/{proposal_id}/reconcile")
def reconcile_chat_proposal(venture_id: str, proposal_id: str, body: ProposalDecisionBody,
                             x_subject: str | None = Header(default=None, alias="X-Subject")):
    return proposal_call(proposals.reconcile, venture_id, x_subject, proposal_id)

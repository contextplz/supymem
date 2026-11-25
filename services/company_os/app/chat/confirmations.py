"""Recover confirmation receipts without dispatching saved actions again."""
import json
from datetime import datetime

from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app.chat.conversations import resolve
from app.db.models import ChatMessage, Job
from app.state import db
from core.clock import as_utc


async def start_requested_work(venture_id, reply, actor_subject=None, *, actions=None):
    from app.chat.execute import PRIVATE_WORK_ACTIONS, execute_action
    from app.credits.errors import InsufficientCredits

    if reply.get("action_started"):
        return reply
    requested = PRIVATE_WORK_ACTIONS if actions is None else actions
    cards = [item for item in reply.get("actions") or [] if isinstance(item, dict)
             and item.get("style") == "primary"
             and (item.get("confirmation") is None or isinstance(item["confirmation"], dict))
             and (item.get("confirmation") or item).get("action") in requested]
    if len(cards) == 1:
        from app.credits.errors import LIMIT_MESSAGE
        if cards[0].get("disabled_reason") == LIMIT_MESSAGE:
            from app.chat.blockers import explain
            return explain(venture_id, {"code": "insufficient_credits"})
    if len(cards) != 1 or cards[0].get("disabled_reason"):
        return reply
    confirmation = cards[0].get("confirmation") or cards[0]
    action = confirmation["action"]
    payload = (confirmation.get("resulting_state") if cards[0].get("confirmation") else confirmation.get("payload")) or {}
    if not isinstance(payload, dict):
        return reply
    try:
        if reply.get("id") or "_goal_step" in payload or action in {
                "autopilot.send_cap", "connections.disconnect", "routines.approve",
                "experiment.persevere", "experiment.pivot", "experiment.spinoff", "experiment.kill"}:
            from app.api.chat_api import ConfirmBody, _confirm_action
            from app.chat.store import append_message

            saved = reply if reply.get("id") else await run_in_threadpool(append_message,
                venture_id, role="assistant", body_md="Your requested action is starting.",
                kind="confirmation", actions=reply["actions"], citations=reply.get("citations"))
            scope = resolve(venture_id)
            result = await _confirm_action(venture_id, ConfirmBody(action=action, payload=payload, message_id=saved["id"]),
                scope.workspace_id, actor_subject)
        else:
            result = await execute_action(venture_id, action, payload=payload, actor_subject=actor_subject)
        if action == "build.app.create" and "build.start" in requested:
            follow = next((item for item in result.get("actions") or []
                if isinstance(item, dict) and item.get("action") == "build.prepare" and item.get("style") == "primary"
                and not item.get("disabled_reason")), None)
            if follow:
                prepared = await execute_action(venture_id, "build.prepare", payload={**(follow.get("payload") or {}), "run": False}, actor_subject=actor_subject)
                return await start_requested_work(venture_id, prepared, actor_subject, actions=requested - {"build.app.create"})
        return result
    except InsufficientCredits:
        from app.chat.blockers import explain

        return explain(venture_id, {"code": "insufficient_credits"})


def card(venture_id, message_id, action, payload, *, actor_subject=None):
    from app.authz.workspace import subject_person_id
    from app.chat.confirm import visible_action
    from app.chat.store import _partition, _ser

    scope = resolve(venture_id)
    if actor_subject is not None and subject_person_id(actor_subject) != (scope.actor_id or subject_person_id(None)):
        raise ValueError("chat_confirmation_not_found")
    with db()() as session:
        row = session.scalar(select(ChatMessage).where(ChatMessage.id == message_id,
            ChatMessage.venture_id == venture_id, ChatMessage.workspace_id == scope.workspace_id, _partition(scope)))
        if row is None or row.role != "assistant":
            raise ValueError("chat_confirmation_not_found")
        message = _ser(row, scope.id)
        if not any(isinstance(item, dict) and isinstance(item.get("confirmation"), dict) for item in message["actions"]):
            message["actions"].extend(visible_action(item) for item in json.loads(row.actions_json or "[]")
                if isinstance(item, dict) and isinstance(item.get("confirmation"), dict))
    matches = [item["confirmation"] for item in message["actions"]
               if isinstance(item, dict) and isinstance(item.get("confirmation"), dict)
               and item["confirmation"].get("state") != "cancelled"
               and item["confirmation"].get("action", item.get("action") or item.get("name")) == action
               and item["confirmation"].get("resulting_state", {}) == payload]
    if len(matches) != 1:
        raise ValueError("chat_confirmation_changed")
    return message


def restore(messages, venture_id):
    # Legacy rows can hold plain-string actions; only dict cards with a dict confirmation qualify.
    pending = [(message, item["confirmation"]) for message in messages for item in (message.get("actions") or [])
               if isinstance(item, dict) and isinstance(item.get("confirmation"), dict)
               and item["confirmation"].get("action") == "pagebuilder.build"
               and item["confirmation"].get("resulting_state", {}).get("quote_fingerprint")
               and item["confirmation"].get("state") != "started"]
    if not pending:
        return messages
    from app.pagebuilder.api import KIND, _quote_fingerprint

    scope = resolve(venture_id)
    # ponytail: legacy quote scans scale with saved runs, backfill card receipts if histories grow.
    with db()() as session:
        jobs = session.scalars(select(Job).where(Job.venture_id == venture_id,
            Job.workspace_id == scope.workspace_id, Job.kind == KIND)).all()
        for job in jobs:
            try:
                document = json.loads(job.payload_json)
            except (ValueError, TypeError):
                continue
            if not isinstance(document, dict):
                continue
            runtime = document.get("_runtime", {})
            if not isinstance(runtime, dict):
                continue
            quote, inputs = runtime.get("quote"), runtime.get("input")
            variants = document.get("variant_count", 1)
            if (not isinstance(quote, dict) or not isinstance(inputs, dict) or not document.get("revision_id")
                    or type(variants) is not int or not 1 <= variants <= 3
                    or type(quote.get("ceiling_micro")) is not int or not isinstance(quote.get("resource_digest"), str)):
                continue
            fingerprint = _quote_fingerprint(scope.workspace_id, document.get("_actor_subject"), venture_id,
                document["revision_id"], inputs, {**quote, "ceiling_micro": quote["ceiling_micro"] // variants}, variants)
            for message, confirmation in pending:
                try:
                    offered_at = datetime.fromisoformat(message["created_at"].replace("Z", "+00:00"))
                except (ValueError, TypeError, KeyError, AttributeError):
                    continue
                if as_utc(job.started_at or job.created_at) < as_utc(offered_at):
                    continue
                if (confirmation["resulting_state"]["quote_fingerprint"] == fingerprint
                        and confirmation["resulting_state"].get("ceiling_micro") == quote["ceiling_micro"]):
                    confirmation.update(state="started", run_id=job.id)
    return messages


def mark(venture_id, message_id, action, state, payload=None, *, request_nonce=None):
    from app.chat.store import _partition
    from app.db.session import serialize_writes

    scope = resolve(venture_id)
    with db()() as session:
        serialize_writes(session, key="chat-confirm:" + message_id)
        row = session.scalar(select(ChatMessage).where(ChatMessage.id == message_id,
            ChatMessage.venture_id == venture_id, ChatMessage.workspace_id == scope.workspace_id,
            _partition(scope)).with_for_update())
        if row is None or row.role != "assistant":
            raise ValueError("chat_confirmation_not_found")
        actions = json.loads(row.actions_json or "[]")
        matches = [item for item in actions
                   if isinstance(item, dict) and isinstance(item.get("confirmation"), dict)
                   and (item["confirmation"].get("action") or item.get("action") or item.get("name")) == action
                   and (payload is None or item["confirmation"].get("resulting_state", {}) == payload)]
        if len(matches) != 1:
            raise ValueError("chat_confirmation_changed")
        confirmation = matches[0]["confirmation"]
        if state == "starting" and confirmation.get("state") not in (None, "not_started"):
            raise ValueError("chat_request_in_progress")
        if state is None:
            confirmation.pop("state", None)
        else:
            confirmation["state"] = state
        if state == "cancelled":
            matches[0]["disabled_reason"] = "You declined this choice. Ask for your next step in chat."
        if request_nonce:
            confirmation["request_nonce"] = request_nonce
        row.actions_json = json.dumps(actions)
        session.commit()


async def auto_dispatch(venture_id, message, actor_subject):
    from app.approvals import standing
    from app.authz.workspace import subject_person_id
    from app.chat.conversations import current_conversation
    from app.tenancy import current_or_none

    if not await run_in_threadpool(standing.enabled):
        return message
    tenant, conversation = current_or_none(), current_conversation()
    if (not tenant or not conversation or conversation.visibility != "private"
            or conversation.venture_id != venture_id or tenant.venture_id != venture_id
            or conversation.workspace_id != tenant.workspace_id
            or conversation.actor_id != subject_person_id(actor_subject or tenant.actor)
            or conversation.actor_id != subject_person_id(tenant.actor) or not message.get("id")):
        return message
    message_id = message["id"]
    result = message
    for item in message.get("actions") or []:
        confirmation = item.get("confirmation") if isinstance(item, dict) else None
        if not isinstance(confirmation, dict) or confirmation.get("state") or item.get("disabled_reason"):
            continue
        action, state = confirmation.get("action"), confirmation.get("resulting_state") or {}
        if not isinstance(action, str) or not isinstance(state, dict):
            continue
        saved = await run_in_threadpool(card, venture_id, message_id, action, state)
        saved_card = next(entry["confirmation"] for entry in saved["actions"]
            if isinstance(entry, dict) and isinstance(entry.get("confirmation"), dict)
            and entry["confirmation"].get("action") == action
            and entry["confirmation"].get("resulting_state", {}) == state)
        if saved_card.get("state"):
            continue
        grant_id = await run_in_threadpool(standing.matching_grant,
            workspace_id=tenant.workspace_id, venture_id=venture_id,
            founder_id=conversation.actor_id, action=standing.card_action(action, state))
        if grant_id is None and "_goal_step" in state:
            from app.tasks.routines import matching_card_grant
            grant_id = await run_in_threadpool(matching_card_grant,
                tenant.workspace_id, venture_id, conversation.actor_id, action, state)
        if grant_id is None:
            continue
        from app.api.chat_api import ConfirmBody, _confirm_action

        result = await _confirm_action(venture_id, ConfirmBody(action=action, payload=state, message_id=saved["id"]),
            tenant.workspace_id, conversation.actor_id, standing_grant_id=grant_id)
        saved = await run_in_threadpool(card, venture_id, message_id, action, state)
    if result is not message and any(isinstance(entry, dict) and isinstance(entry.get("confirmation"), dict)
            and not entry["confirmation"].get("state") for entry in saved["actions"]):
        return saved
    return result

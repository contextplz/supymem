"""Provider-free, source-bound whole-message checkpoints. Originals are retained."""
import hashlib
import json
import re

from sqlalchemy import select
from app.chat.conversations import resolve
from app.chat.store import _partition
from app.db.models import ChatMessage, ChatContextCheckpoint
from app.db.session import serialize_writes
from app.memory.fuse import bm25_rank
from app.state import db
from core.clock import utcnow
from core.ids import new_id

_CONSTRAINT = re.compile(r"\b(remember|must|never|always|prefer|constraint|do not|don't|correction)\b", re.I)


def _encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _source(row):
    return {"id": row.id, "role": row.role, "kind": row.kind, "body_md": row.body_md,
            "created_at": row.created_at.isoformat(), "actions_json": row.actions_json,
            "citations_json": row.citations_json}


def _digest(rows):
    return hashlib.sha256(_encode([_source(row) for row in rows]).encode()).hexdigest()


def _item(row):
    from app.inbox import MESSAGE_KINDS, QUOTED_HISTORY
    if row.kind in MESSAGE_KINDS:
        return {"source_message_id": row.id, "role": "data", "kind": row.kind,
                "body_md": QUOTED_HISTORY, "evidence_kind": "untrusted_mail"}
    from app.secrets.detect import looks_like_secret
    if row.kind == "next_moves" or looks_like_secret(row.body_md):
        return None
    return {"source_message_id": row.id, "role": row.role, "kind": row.kind,
            "body_md": row.body_md, "evidence_kind": "founder_statement" if row.role in {"user", "founder"}
            else "historical_claim_not_current_state"}


def _bounded(value, all_ids, byte_budget):
    if byte_budget < 512:
        raise ValueError("chat_context_budget_too_small")
    value = dict(value)
    value["items"] = list(value["items"])
    while True:
        selected = {item["source_message_id"] for item in value["items"]}
        omitted = [mid for mid in all_ids if mid not in selected]
        value["omitted_count"] = len(omitted)
        value["omitted_source_ids"] = omitted[:8]
        value["omitted_source_ids_unlisted"] = max(0, len(omitted) - 8)
        if len(_encode(value).encode()) <= byte_budget:
            return value
        if value["items"]:
            value["items"].pop()
            continue
        # Count/watermark remain explicit even if no list of omitted ids fits.
        value["omitted_source_ids"] = []
        value["omitted_source_ids_unlisted"] = len(omitted)
        if len(_encode(value).encode()) <= byte_budget:
            return value
        raise ValueError("chat_context_budget_too_small")


def compact(venture_id: str, *, conversation_id=None, byte_budget=12000, tail_count=12):
    scope = resolve(venture_id, conversation_id)
    with db()() as session:
        serialize_writes(session, key="chat-messages:" + scope.id)
        rows = session.scalars(select(ChatMessage).where(ChatMessage.venture_id == venture_id, _partition(scope))
            .order_by(ChatMessage.created_at, ChatMessage.id)).all()
        from app.chat.new_venture import context_history
        allowed = {message["id"] for message in context_history([_source(row) for row in rows])}
        rows = [row for row in rows if row.id in allowed]
        prefix = rows[:-tail_count] if len(rows) > tail_count else []
        if not prefix:
            return None
        digest = _digest(prefix)
        previous = session.scalar(select(ChatContextCheckpoint).where(ChatContextCheckpoint.conversation_id == scope.id)
            .order_by(ChatContextCheckpoint.version.desc()).limit(1))
        if previous and previous.source_digest == digest:
            return previous.id
        ordered = sorted(prefix, key=lambda row: (row.role not in {"user", "founder"},
            not bool(_CONSTRAINT.search(row.body_md)), -row.created_at.timestamp(), row.id))
        items, omitted, size = [], [], 0
        for row in ordered:
            item = _item(row)
            if item is None:
                omitted.append(row.id)
                continue
            estimate = len(_encode(item).encode())
            if size + estimate > max(0, byte_budget):
                omitted.append(row.id)
            else:
                items.append(item)
                size += estimate
        value = _bounded({"schema_version": 1, "kind": "source_linked_whole_messages", "items": items,
            "omitted_source_ids": omitted, "omitted_count": len(omitted),
            "authority": "context_only_current_records_override_historical_claims"}, [row.id for row in prefix], byte_budget)
        checkpoint = ChatContextCheckpoint(id=new_id("ccp"), conversation_id=scope.id,
            version=previous.version + 1 if previous else 1, watermark_id=prefix[-1].id,
            source_ids_json=_encode([row.id for row in prefix]), source_digest=digest,
            payload_json=_encode(value), created_at=utcnow())
        session.add(checkpoint)
        session.commit()
        return checkpoint.id


def context_for_question(venture_id: str, question: str, *, conversation_id=None, byte_budget=16000):
    scope = resolve(venture_id, conversation_id)
    compact(venture_id, conversation_id=scope.id)
    with db()() as session:
        rows = session.scalars(select(ChatMessage).where(ChatMessage.venture_id == venture_id, _partition(scope))
            .order_by(ChatMessage.created_at, ChatMessage.id)).all()
        from app.chat.new_venture import context_history
        allowed = {message["id"] for message in context_history([_source(row) for row in rows])}
        rows = [row for row in rows if row.id in allowed]
        latest = session.scalar(select(ChatContextCheckpoint).where(ChatContextCheckpoint.conversation_id == scope.id)
            .order_by(ChatContextCheckpoint.version.desc()).limit(1))
        by_id = {row.id: row for row in rows}
        checkpoint_items, checkpoint_valid = [], False
        if latest:
            try:
                ids = json.loads(latest.source_ids_json)
                source = [by_id[mid] for mid in ids]
                value = json.loads(latest.payload_json)
                checkpoint_valid = (isinstance(ids, list) and bool(ids) and len(set(ids)) == len(ids)
                    and ids == [row.id for row in rows[:len(ids)]] and ids[-1] == latest.watermark_id
                    and _digest(source) == latest.source_digest and type(value.get("schema_version")) is int and value["schema_version"] == 1
                    and set(value) == {"schema_version", "kind", "items", "omitted_source_ids", "omitted_count", "omitted_source_ids_unlisted", "authority"}
                    and value["kind"] == "source_linked_whole_messages"
                    and value["authority"] == "context_only_current_records_override_historical_claims"
                    and all(item == _item(by_id[item["source_message_id"]]) for item in value["items"])
                    and type(value["omitted_count"]) is int and type(value["omitted_source_ids_unlisted"]) is int
                    and len(value["omitted_source_ids"]) + value["omitted_source_ids_unlisted"] == value["omitted_count"]
                    and len(ids) - len(value["items"]) == value["omitted_count"]
                    and len({item["source_message_id"] for item in value["items"]}) == len(value["items"])
                    and len(set(value["omitted_source_ids"])) == len(value["omitted_source_ids"])
                    and set(value["omitted_source_ids"]).issubset(set(ids) - {item["source_message_id"] for item in value["items"]}))
                if checkpoint_valid:
                    checkpoint_items = [by_id[item["source_message_id"]] for item in value["items"]]
            except (ValueError, KeyError, TypeError):
                checkpoint_valid = False
        ranked = bm25_rank(question, [(row.id, row.body_md) for row in rows], limit=12)
        candidates = list(reversed(rows[-12:])) + [by_id[mid] for mid in ranked] + checkpoint_items
        items, ids, size = [], set(), 0
        for row in candidates:
            if row.id in ids:
                continue
            item = _item(row)
            if item is None:
                continue
            estimate = len(_encode(item).encode())
            if size + estimate > max(0, byte_budget):
                continue
            items.append(item)
            ids.add(row.id)
            size += estimate
        omitted = [row.id for row in rows if row.id not in ids]
        return _bounded({"schema_version": 1, "conversation_id": scope.id,
            "source_watermark": rows[-1].id if rows else None,
            "checkpoint_id": latest.id if latest and checkpoint_valid else None,
            "checkpoint_valid": checkpoint_valid, "items": items,
            "omitted_count": len(omitted), "omitted_source_ids": omitted,
            "raw_history_retained": True, "authority": "context_only_current_records_override_historical_claims"}, [row.id for row in rows], byte_budget)

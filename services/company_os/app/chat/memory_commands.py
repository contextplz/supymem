"""Explicit source-backed venture memory commands; never model-selected writes."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
import sqlite3

from sqlalchemy.exc import SQLAlchemyError
from app.core.local_mode import is_local
from app.secrets.detect import looks_like_secret


_ID = r"mem_[A-Za-z0-9_-]{1,120}"
_REMEMBER = re.compile(
    r"\A(?:please\s+)?remember(?:(?:\s+(?:that|this))?\s*:\s*|\s+(?:that|this)\s+)(.+)\Z",
    re.I | re.S,
)
_REMEMBER_PREFIX = re.compile(r"\A(?:please\s+)?remember(?:[?:].*|\s+(?:that|this)\b.*|\s+what\b.*\?|\s*)\Z", re.I | re.S)
_EMPTY_REMEMBER = re.compile(r"\A(?:that|this)\s*:?\s*\Z", re.I)
_REMEMBER_QUESTION = re.compile(r"\A(?:what|why|when|where|who|which|how|whether)\b.*\?\Z", re.I | re.S)
_CORRECT = re.compile(rf"\Acorrect\s+memory\s+({_ID})\s*:\s*(.+)\Z", re.I | re.S)
_FORGET = re.compile(rf"\Aforget\s+memory\s+({_ID})\s*[.!]?\Z", re.I)
_LIST = re.compile(r"\A(?:list|show)\s+(?:my\s+|the\s+)?(?:saved\s+)?(?:venture\s+)?memories\s*[.!?]?\Z", re.I)
_HELP = re.compile(r"\A(?:memory\s+help|(?:correct|forget)\s+memory\b.*)\Z", re.I | re.S)
_FACT_QUERY = re.compile(r"\A(?:who\s+(?:is|are)\s+(?:my|our)\s+(?:audience|customers?|users?)|what\s+(?:is|are)\s+(?:my|our)\s+(?:audience|pricing|price|problem|solution)|what\s+do\s+you\s+know\s+about\s+(?:my|our)\s+(?:customers?|audience|pricing)|what\s+did\s+i\s+say\s+before\s+about\s+(?:audience|customers?|pricing|price|problem|solution))\s*[.!?]?\Z", re.I)
HELP = ('Say "Remember: <complete statement>", "List saved venture memories", "Forget memory mem_<id>", or '
        '"Correct memory mem_<id>: <complete replacement>". Use an exact ID from the list. '
        'These commands change saved venture context, not tasks, approvals or verified results.')
LIST_BYTES = 12000
_LIST_HEADER = "Current venture memories saved by you (up to 100; founder statements, not verified results):"
_OMITTED = "Omitted {count} whole memory statement(s) to keep this reply bounded. Open venture Settings to review saved memories."
STORAGE_RECOVERY = "Your saved note could not be confirmed. Check that your local data folder is writable and has free space, then restart your app and list saved venture memories before trying again."


@dataclass(frozen=True)
class MemoryCommand:
    action: str
    target: str = ""
    statement: str = ""


def parse(text: str) -> MemoryCommand | None:
    raw = (text or "").strip()
    if match := _REMEMBER.fullmatch(raw):
        statement = match[1].strip()
        if (not statement or statement.startswith(":") or _EMPTY_REMEMBER.fullmatch(statement)
                or _REMEMBER_QUESTION.fullmatch(statement)):
            return MemoryCommand("memory.help")
        return MemoryCommand("memory.remember", statement=statement)
    if _REMEMBER_PREFIX.fullmatch(raw):
        return MemoryCommand("memory.help")
    if match := _CORRECT.fullmatch(raw):
        return MemoryCommand("memory.correct", target=match[1], statement=match[2].strip())
    if match := _FORGET.fullmatch(raw):
        return MemoryCommand("memory.forget", target=match[1])
    if _LIST.fullmatch(raw):
        return MemoryCommand("memory.list")
    if _HELP.fullmatch(raw):
        return MemoryCommand("memory.help")
    if _FACT_QUERY.fullmatch(raw):
        from app.memory.context import subject_for_query
        return MemoryCommand("memory.history" if "before" in raw.lower() else "memory.recall",
                             target=subject_for_query(raw) or "", statement=raw)
    return None


def _reply(body, *, kind="text", citations=None):
    return {"kind": kind, "body_md": body, "actions": [], "citations": citations or []}


def list_reply(items) -> dict:
    """Preserve complete statements; leave room for speaker attribution and help."""
    lines, kept, omitted = [_LIST_HEADER], [], 0
    reserve = len(("\n\n" + _OMITTED.format(count=len(items)) + "\n\n" + HELP).encode()) + 512
    for item in items:
        statement = item.statement
        if looks_like_secret(statement):
            statement = "Your note contains a credential, so its text is hidden. Save your key in Settings > Keys and connections, then correct or forget this note using its ID."
        entry = "\n\n" + item.mem_id + "\n" + statement
        if len(("".join(lines) + entry).encode()) + reserve > LIST_BYTES:
            omitted += 1
        else:
            lines.append(entry)
            kept.append(item)
    if omitted:
        lines.append("\n\n" + _OMITTED.format(count=omitted))
    lines.append("\n\n" + HELP)
    return _reply("".join(lines), kind="memory_list",
                  citations=[{"label": item.mem_id, "href": "#memory:" + item.mem_id} for item in kept])


def _source(venture_id, source_message_id):
    from app.chat.conversations import resolve
    from app.db.models import ChatMessage
    from app.state import db
    from app.tenancy import current_or_none
    scope = current_or_none()
    if scope is None:
        raise ValueError("memory_command_source_invalid")
    chat = resolve(venture_id)
    with db()() as session:
        source = session.get(ChatMessage, source_message_id)
        if (not chat.actor_id or scope.actor != chat.actor_id or not source
                or (source.workspace_id, source.venture_id, source.conversation_id,
                    source.author_person_id, source.role)
                != (scope.workspace_id, venture_id, chat.id, chat.actor_id, "user")):
            raise ValueError("memory_command_source_invalid")
        return source.body_md, chat.actor_id


async def _local_items(adapter, venture_id, actor):
    from app.memory.evidence import validate_write_evidence
    from app.memory.model import RecallQuery
    from app.state import db
    hits = await adapter.recall(RecallQuery(venture_id=venture_id, text="",
        limit=max(100, (await adapter.stats(venture_id)).active), include_decayed=True))
    items = []
    with db()() as session:
        for hit in hits:
            if (hit.venture_id != venture_id or hit.author != actor or hit.kind.value in {"outcome", "failure"}
                    or hit.valid_to is not None or hit.superseded_by):
                continue
            try:
                validate_write_evidence(session, hit)
            except ValueError:
                continue
            items.append(hit)
    return items


async def _target(venture_id, mem_id, actor, adapter):
    if is_local() and getattr(adapter, "name", None) == "local":
        for hit in await _local_items(adapter, venture_id, actor):
            if hit.mem_id == mem_id:
                return hit
        raise ValueError("memory_command_target_unavailable")
    from app.db.models import InstitutionalMemory
    from app.memory.evidence import evidence_current
    from app.memory.model import MemoryRecord, MemoryKind
    from app.state import db
    from app.tenancy import current
    with db()() as session:
        row = session.get(InstitutionalMemory, mem_id)
        if not row or (row.workspace_id, row.venture_id) != (current().workspace_id, venture_id):
            raise ValueError("memory_command_target_unavailable")
        record = MemoryRecord.model_validate_json(row.record_json)
        if (record.mem_id != row.id or record.venture_id != venture_id or record.author != actor
                or record.tombstoned or record.valid_to is not None or record.superseded_by
                or record.kind in {MemoryKind.OUTCOME, MemoryKind.FAILURE}
                or not evidence_current(session, record)):
            raise ValueError("memory_command_target_unavailable")
        return record


async def dispatch(venture_id: str, source_message_id: str, *, action_name: str | None = None) -> dict:
    """The just-saved command is the only input; IDs/statements are never payload authority."""
    from app.memory import get_memory
    from app.memory.model import MemoryWrite, RecallQuery
    from app.ventures.founder_memory import infer_remember_kind, remember
    from app.ventures.state_brief import invalidate_venture_state_brief
    try:
        body, actor = _source(venture_id, source_message_id)
        command = parse(body)
        if command is None or command.action == "memory.help":
            return _reply(HELP)
        if action_name is not None and command.action != action_name:
            return _reply("Your saved instruction does not match this memory action. No memory was changed. " + HELP)
        if command.action in {"memory.remember", "memory.correct"} and looks_like_secret(command.statement):
            return _reply("Your note looks like it contains a key or password. No memory was changed. Save your key in Settings > Keys and connections, then send your note again without credentials.")
        if command.action in {"memory.recall", "memory.history"}:
            return fact_reply(venture_id, command.target, history=command.action == "memory.history")
        if command.action == "memory.list":
            try:
                adapter = get_memory()
                if is_local() and getattr(adapter, "name", None) == "local":
                    hits = (await _local_items(adapter, venture_id, actor))[:100]
                elif getattr(adapter, "name", None) != "postgres":
                    return _reply("Source-backed memory listing is unavailable in this storage mode. No memory was changed. " + HELP)
                else:
                    hits = await adapter.recall(RecallQuery(venture_id=venture_id, text="", limit=100))
            except (LookupError, OSError, RuntimeError, sqlite3.Error, SQLAlchemyError) as exc:
                from app.chat.recovery import log_founder_exception
                log_founder_exception(exc, venture_id, "memory.list")
                if is_local():
                    return _reply(STORAGE_RECOVERY)
                return _reply("Saved memory is unavailable; you can continue with existing venture context and unknown facts. No memory was changed. " + HELP)
            items = [h for h in hits if h.author == actor and h.kind.value not in {"outcome", "failure"}
                     and h.valid_to is None and not h.superseded_by]
            if not items:
                return _reply(("You have no saved notes for this venture. " if is_local() else
                    "No current source-backed memories saved by you are listed for this venture. ") + HELP)
            return list_reply(items)
        adapter = get_memory()
        if (command.action != "memory.remember" and getattr(adapter, "name", None) != "postgres"
                and not (is_local() and getattr(adapter, "name", None) == "local")):
            return _reply("Source-backed exact corrections are unavailable in this storage mode. No memory was changed. " + HELP)
        if command.action == "memory.remember":
            await remember(venture_id=venture_id, fact=command.statement,
                provenance="chat:" + source_message_id, kind=infer_remember_kind(command.statement))
            text = "I've saved that for you. You can review or remove it in Settings."
            kind = "memory_saved"
        elif command.action == "memory.correct":
            previous = await _target(venture_id, command.target, actor, adapter)
            write = MemoryWrite(venture_id=venture_id, kind=previous.kind, subject=previous.subject,
                statement=command.statement, evidence=["chat:" + source_message_id],
                confidence=previous.confidence, confidence_source="founder", author=actor,
                dedupe_key="chat-correction:" + hashlib.sha256(source_message_id.encode()).hexdigest(),
                entities=previous.entities, tags=previous.tags)
            await adapter.supersede(command.target, by=write, reason="explicit founder chat correction")
            text = "Updated your saved note. Your earlier statement stays in history. List saved venture memories to review your correction."
            kind = "memory_corrected"
        else:
            await _target(venture_id, command.target, actor, adapter)
            kwargs = {"source_message_id": source_message_id} if getattr(adapter, "name", None) == "postgres" else {}
            await adapter.forget(venture_id=venture_id, mem_id=command.target,
                reason="explicit founder chat forget", **kwargs)
            invalidate_venture_state_brief(venture_id)
            return _reply("Forgot your saved note. Your original chat and history stay saved. List saved venture memories to review what remains.", kind="memory_forgotten")
        invalidate_venture_state_brief(venture_id)
        return _reply(text, kind=kind, citations=[{"label": "Your saved note", "href": f"/v/{venture_id}/settings#settings-memory"}])
    except (ValueError, PermissionError, KeyError):
        return _reply("No memory was changed. The exact saved statement or owned current memory could not be verified, or it contains sensitive data. " + HELP)
    except (OSError, RuntimeError, sqlite3.Error, SQLAlchemyError) as exc:
        if not is_local():
            raise
        from app.chat.recovery import log_founder_exception
        log_founder_exception(exc, venture_id, "memory.command")
        return _reply(STORAGE_RECOVERY)


def fact_reply(venture_id: str, subject: str, *, history: bool = False) -> dict:
    """Quote the saved source, or abstain. No model-selected factual answer."""
    import json
    from datetime import datetime

    from app.memory.current import legacy_source_hits, select_current_reports, stored_report_is_current
    from app.memory import get_memory
    from app.memory.extraction import quoted_candidates
    from app.chat.conversations import actor_id
    from app.state import db
    from core.clock import as_utc

    try:
        with db()() as session:
            adapter = get_memory()
            selected = select_current_reports(session, venture_id=venture_id,
                legacy_hits=legacy_source_hits(adapter, venture_id), include_history=history,
                raw_legacy=getattr(adapter, "name", None) == "postgres")
            actor = actor_id()
            candidates = [{"value": fact.value, "at": as_utc(fact.valid_from),
                           "refs": [json.loads(fact.source_json)["ref"]],
                           "retired": fact.id not in selected["current_ids"]}
                          for fact in selected["facts"] if fact.subject == subject and fact.person_id == actor]
            for hit in selected["legacy"]:
                if hit.author == actor and any(item.subject == subject for item in quoted_candidates(hit.statement)):
                    candidates.append({"value": hit.statement,
                                       "at": as_utc(datetime.fromisoformat(selected["source_events"][hit.mem_id])),
                                       "refs": list(hit.evidence), "retired": hit.mem_id not in selected["current_ids"]})
            authority = selected["authorities"].get(subject)
            if not history and {"author": actor, "subject": subject} in selected["conflicts"]:
                return _reply("Your latest saved statements about that have the same source time and conflict. Review their sources in Memory in Settings before choosing what I should use.",
                              kind="memory_recall", citations=[{"label": "Review saved sources",
                                  "href": f"/v/{venture_id}/settings#memory"}])
            if authority and not history:
                candidates = [item for item in candidates if stored_report_is_current(authority, item["at"])]
                if not candidates:
                    if authority["status"] == "known":
                        reply = _reply("Your current idea records this:\n\n> " + authority["value"].replace("\n", "\n> "),
                                       kind="memory_recall", citations=[{"label": "Current idea record", "href": authority["href"]}])
                        if len(json.dumps(reply, ensure_ascii=False, separators=(",", ":")).encode()) <= LIST_BYTES:
                            return reply
                        return {**_reply("The current idea record is too long for this reply. Open your idea to read the complete statement.",
                                         kind="memory_recall", citations=[{"label": "Current idea record", "href": authority["href"]}]),
                                "omitted_count": 1}
                    return _reply("Your current idea leaves that open or has a newer statement awaiting clarification. Earlier saved statements remain in history. Tell me the current answer or review your idea.",
                                  kind="memory_recall", citations=[{"label": "Review current idea", "href": authority["href"]}])
            if not history and {"author": actor, "subject": subject} in selected["blocked"]:
                return _reply("Your newer saved statement about that was forgotten, withdrawn or cannot be verified. Tell me the current answer or review its source in Memory in Settings.",
                              kind="memory_recall", citations=[{"label": "Review saved sources",
                                  "href": f"/v/{venture_id}/settings#memory"}])
            if not candidates:
                return _reply("I don't have a source-backed statement from you about that yet. Tell me what I should use.", kind="memory_recall")
            latest = max(item["at"] for item in candidates)
            if not history:
                candidates = [item for item in candidates if item["at"] == latest]
                if len({item["value"] for item in candidates}) > 1:
                    return _reply("Your latest saved statements about that have the same source time and conflict. Review their sources in Memory in Settings before choosing what I should use.",
                                  kind="memory_recall", citations=[{"label": "Review saved sources",
                                      "href": f"/v/{venture_id}/settings#memory"}])
                candidates = candidates[:1]
            candidates.sort(key=lambda item: item["at"])
            rows, citations, seen, omitted = [], [], set(), 0
            for item in candidates:
                identity = (item["value"], tuple(item["refs"]))
                if identity in seen:
                    continue
                seen.add(identity)
                date = item["at"].strftime("%d %b %Y")
                status = "Earlier statement" if item["retired"] or item["at"] < latest or not stored_report_is_current(authority, item["at"]) else "Your saved statement"
                entry = f"{status} ({date}):\n\n> " + item["value"].replace("\n", "\n> ")
                sources = [{"label": "Open conversation",
                            "href": f"/v/{venture_id}?chat=1", "source_ref": ref} for ref in item["refs"]]
                proposed = _reply("\n\n".join([*rows, entry]), kind="memory_recall", citations=[*citations, *sources])
                if len(json.dumps(proposed, ensure_ascii=False, separators=(",", ":")).encode()) > LIST_BYTES - 512:
                    omitted += 1
                    continue
                rows.append(entry)
                citations.extend(sources)
            if omitted:
                rows.append(f"Omitted {omitted} whole source statements from this bounded reply. Open Memory in Settings to review their sources.")
                citations.append({"label": "Review saved sources", "href": f"/v/{venture_id}/settings#memory"})
            return {**_reply("\n\n".join(rows), kind="memory_recall", citations=citations), "omitted_count": omitted}
    except Exception as exc:
        from app.chat.recovery import founder_failure
        topic = subject.removeprefix("idea.")
        question = f"What did I say before about {topic}?" if history else f"What is my {topic}?"
        return {**founder_failure(exc, venture_id, "your saved sources", {"action": "chat.reply", "text": question, "retry": True}),
                "kind": "memory_recall"}

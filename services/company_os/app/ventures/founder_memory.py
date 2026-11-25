"""Founder-facing venture memory: list, forget, pack prefs into chat context."""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import logging
from contextvars import copy_context
from datetime import datetime
from typing import Any

from app.memory import MemoryKind, MemoryWrite, RecallQuery, TaskRef, get_memory
from app.tenancy import ScopeRequired, current_or_none
from app.ventures.state_brief import invalidate_venture_state_brief
from core.clock import as_utc

_PREF_KINDS = frozenset(
    {
        MemoryKind.FOUNDER_PREFERENCE,
        MemoryKind.STYLE_CORRECTION,
        MemoryKind.DECISION,
        MemoryKind.CONSTRAINT,
        MemoryKind.INSIGHT,
    }
)

_KIND_MAP = {
    "event": MemoryKind.OUTCOME,
    "preference": MemoryKind.FOUNDER_PREFERENCE,
    "commitment": MemoryKind.DECISION,
    "belief": MemoryKind.INSIGHT,
    "fact": MemoryKind.INSIGHT,
    "insight": MemoryKind.INSIGHT,
    "decision": MemoryKind.DECISION,
    "entity": MemoryKind.ENTITY,
}


class MemoryWriteError(ValueError):
    def __init__(self, code: str, message: str, *, suggestion: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.suggestion = suggestion


async def remember(*, venture_id: str, fact: str, provenance: str,
                   entity: str | None = None, kind: str = "fact") -> str:
    statement = (fact or "").strip()
    if not statement:
        raise MemoryWriteError("invalid_params", "fact is required")
    if not (provenance or "").strip():
        raise MemoryWriteError("provenance_required", "provenance is required",
                               suggestion="Pass where this fact came from (chat, dossier, founder).")
    if len(provenance) > 500:
        raise MemoryWriteError("invalid_params", "provenance must be ≤500 characters")
    mapped = _KIND_MAP.get((kind or "fact").strip().lower(), MemoryKind.INSIGHT)
    scope = current_or_none()
    founder_source = provenance.strip().startswith("chat:") and scope is not None
    subject = (entity or "general").strip() or "general"
    identity = [kind, subject, statement]
    if founder_source:
        identity.extend([scope.actor, provenance.strip()])
    key = "gbrain:" + hashlib.sha256(json.dumps(identity,
        ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    write = MemoryWrite(venture_id=venture_id, kind=mapped, subject=subject, statement=statement,
                        evidence=[provenance.strip()], confidence=0.7,
                        confidence_source="founder" if founder_source else "rule",
                        author=scope.actor if founder_source else "gbrain.remember",
                        dedupe_key=key, entities=[subject] if entity else [], tags=["gbrain"])
    try:
        return await get_memory().remember(write)
    except (ValueError, PermissionError) as exc:
        raise MemoryWriteError("memory_source_invalid", "I could not save that statement from an authorized, nonsecret source. Send a complete explicit remember instruction in this chat.") from exc


def _run_async(coro: Any) -> Any:
    """Run an async memory call from sync chat/brief paths."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        # Preserve signed actor/workspace/venture context when crossing a thread.
        # PostgreSQL memory enforces that scope on every read and write.
        return pool.submit(copy_context().run, asyncio.run, coro).result(timeout=15)


def infer_remember_kind(fact: str) -> str:
    """Map chat 'remember that…' text to a gbrain kind string."""
    lower = (fact or "").lower()
    if any(
        k in lower
        for k in (
            "prefer",
            "preference",
            "always",
            "never",
            "don't",
            "do not",
            "short answer",
            "style",
            "tone",
        )
    ):
        return "preference"
    if any(k in lower for k in ("decid", "decision", "we will", "milestone", "agreed")):
        return "decision"
    return "fact"


def memory_section_for_brief(venture_id: str) -> dict[str, Any]:
    """Bound whole source-backed statements; distinguish missing recall from empty."""
    vid = (venture_id or "").strip()
    empty = {"availability": "available", "preferences": [], "decisions": [],
             "constraints": [], "other": [], "omitted_count": 0, "omitted_memory_ids": []}
    if not vid:
        return empty
    try:
        pack = _run_async(get_memory().context_pack(
            venture_id=vid, task=TaskRef(title="venture context", tags=["chat", "agent", "planning"]),
            token_budget=1400))
        if pack.venture_id != vid or any(hit.venture_id != vid for hit in pack.memories):
            raise ValueError("memory_brief_scope_mismatch")
        result = {**empty, "preferences": [], "decisions": [], "constraints": [], "other": []}
        omitted = set(getattr(pack, "omitted_memory_ids", []) or [])
        upstream_count = max(len(omitted), int(getattr(pack, "omitted_count", 0) or 0))
        extra = set()
        used = 0
        for hit in pack.memories:
            row = {"id": hit.mem_id, "kind": hit.kind.value, "subject": hit.subject,
                   "statement": hit.statement, "evidence": list(hit.evidence), "author": hit.author}
            size = len(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode())
            if hit.valid_to is not None or hit.superseded_by or not hit.evidence or used + size > 1400:
                extra.add(hit.mem_id)
                continue
            used += size
            section = ("preferences" if hit.kind in {MemoryKind.FOUNDER_PREFERENCE, MemoryKind.STYLE_CORRECTION}
                       else "decisions" if hit.kind == MemoryKind.DECISION
                       else "constraints" if hit.kind == MemoryKind.CONSTRAINT else "other")
            result[section].append(row)
        result.update(token_estimate=used, truncated_kinds=list(pack.truncated_kinds or []),
                      omitted_count=upstream_count + len(extra - omitted),
                      omitted_memory_ids=sorted(omitted | extra))
        return result
    except Exception as exc:  # noqa: BLE001 - unavailable recall is not proof of no memory
        logging.getLogger(__name__).warning("memory_recall_failed", extra={"error_class": type(exc).__name__})
        return {**empty, "availability": "unavailable"}


def list_founder_memories(
    venture_id: str,
    *,
    kinds: list[str] | None = None,
    limit: int = 50,
    status: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """List active founder-visible memories for one venture only."""
    vid = (venture_id or "").strip()
    if not vid:
        return []
    if status is not None:
        status.update(availability="available", conflict_count=0)
    wanted: list[MemoryKind]
    if kinds:
        wanted = []
        for raw in kinds:
            try:
                wanted.append(MemoryKind(str(raw).strip().lower()))
            except ValueError:
                continue
        if not wanted:
            wanted = list(_PREF_KINDS)
    else:
        wanted = list(_PREF_KINDS)

    from app.tenancy import current_or_none
    scope = current_or_none()
    facts, events, conflicts = [], {}, []
    hits, local, legacy_failed, raw_legacy, blocked = [], False, False, False, 0
    from app.memory.current import legacy_source_hits, select_current_reports
    from app.state import db
    try:
        port = get_memory()
        local = getattr(port, "name", None) == "local"
        raw_legacy = getattr(port, "name", None) == "postgres"
        hits = (_run_async(port.recall(RecallQuery(venture_id=vid, text="", kinds=wanted, limit=max(limit, 20))))
                if scope is None or local else legacy_source_hits(port, vid))
    except Exception as exc:
        legacy_failed = True
        logging.getLogger(__name__).warning("memory_legacy_list_failed", extra={"error_class": type(exc).__name__})
        if status is not None:
            status["availability"] = "unavailable"
    if scope is not None:
        unqualified = [hit for hit in hits if local and not any(ref.startswith("chat:") for ref in hit.evidence)]
        try:
            with db()() as session:
                selected = select_current_reports(session, venture_id=vid, legacy_hits=hits, raw_legacy=raw_legacy)
                hits, facts, events, conflicts = [*selected["legacy"], *unqualified], selected["facts"], selected["source_events"], selected["conflicts"]
                blocked = selected["blocked_count"]
                if legacy_failed and status is not None and facts:
                    status["availability"] = "partial"
        except Exception as exc:
            logging.getLogger(__name__).warning("memory_fact_list_failed", extra={"error_class": type(exc).__name__})
            try:
                with db()() as session:
                    selected = select_current_reports(session, venture_id=vid, legacy_hits=hits, automatic_rows=[], raw_legacy=raw_legacy)
                    hits = [*selected["legacy"], *unqualified]
                    events, conflicts = selected["source_events"], selected["conflicts"]
                    blocked = selected["blocked_count"]
            except Exception:
                hits = unqualified
            if status is not None:
                status["availability"] = "partial" if hits else "unavailable"
    out: list[dict[str, Any]] = []
    for hit in hits:
        if hit.kind not in wanted or hit.venture_id != vid or hit.valid_to is not None or hit.superseded_by or not hit.evidence:
            continue
        out.append(
            {
                "id": hit.mem_id,
                "kind": hit.kind.value,
                "subject": hit.subject,
                "statement": hit.statement,
                "detail": hit.detail,
                "valid_from": hit.valid_from.isoformat() if hit.valid_from else None,
                "author": hit.author,
                "evidence": list(hit.evidence),
                "sources": [{"ref": reference} for reference in hit.evidence],
                "source": {"ref": hit.evidence[0]},
                "valid_to": None,
                "source_at": events.get(hit.mem_id),
                "qualification": "local_unqualified_saved_statement" if local and hit.mem_id not in events
                                 else "source_backed_saved_statement_context_only",
            }
        )
    if MemoryKind.INSIGHT in wanted:
        from app.memory.facts import project_fact
        for row in facts:
            item = project_fact(row)
            source = item["source"]
            out.append({**item, "kind": MemoryKind.INSIGHT.value,
                        "statement": item["value"], "author": item["person_id"],
                        "evidence": [source["ref"]], "sources": [source]})
    if blocked and status is not None:
        status.update(availability="partial" if out else "unavailable", blocked_count=blocked)
    if conflicts:
        if status is not None:
            status.update(availability="conflict", conflict_count=len(conflicts))
    out.sort(key=lambda item: as_utc(datetime.fromisoformat(item.get("source_at") or item["valid_from"])).timestamp()
             if item.get("source_at") or item.get("valid_from") else float("-inf"), reverse=True)
    return out[:limit]


def forget_founder_memory(venture_id: str, mem_id: str, *, reason: str = "founder forget") -> None:
    vid = (venture_id or "").strip()
    mid = (mem_id or "").strip()
    if not vid or not mid:
        raise ValueError("venture_id and mem_id are required")
    # Boundary is enforced inside the adapter (venture_id must match).
    try:
        if mid.startswith("fact_"):
            from app.memory.facts import forget_fact
            from app.state import db

            with db()() as session:
                forget_fact(session, venture_id=vid, fact_id=mid)
                session.commit()
        else:
            _run_async(
                get_memory().forget(venture_id=vid, mem_id=mid, reason=reason or "founder forget")
            )
    except (PermissionError, ScopeRequired) as exc:
        raise KeyError(mid) from exc
    invalidate_venture_state_brief(vid)

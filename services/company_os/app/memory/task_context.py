"""Bounded venture memory for planning and agent briefs, independent of chat.

This projection contains saved institutional statements. It is neither an
approval nor an outcome oracle; callers still resolve current execution records.
"""

from __future__ import annotations

import json

from app.db.models import Venture
from app.memory import get_memory
from app.memory.model import MemoryKind, TaskRef
from app.tenancy import bind, current_or_none
from app.ventures.founder_memory import _run_async

_KINDS = {MemoryKind.FOUNDER_PREFERENCE, MemoryKind.STYLE_CORRECTION,
          MemoryKind.DECISION, MemoryKind.CONSTRAINT, MemoryKind.INSIGHT}


def _bytes(value) -> int:
    return len(json.dumps(value, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"), allow_nan=False).encode())


def for_task(session, *, workspace_id: str, venture_id: str, title: str,
             tags: list[str] | None = None, byte_budget: int = 6000) -> dict:
    venture = session.get(Venture, venture_id)
    scope = current_or_none()
    if (not venture or not workspace_id or venture.workspace_id != workspace_id
            or scope and (scope.workspace_id, scope.venture_id) != (workspace_id, venture_id)):
        raise ValueError("memory_task_scope_invalid")
    if type(byte_budget) is not int or not 1024 <= byte_budget <= 16000:
        raise ValueError("memory_task_budget_invalid")
    result = {"schema_version": 1, "workspace_id": workspace_id, "venture_id": venture_id,
              "availability": "available", "effect_authority": False,
              "current_state_authority": False, "statements": [], "omitted_memory_ids": [],
              "omitted_count": 0, "truncated_kinds": []}
    try:
        # Existing signed requests/jobs keep their exact scope. Internal planning
        # already validates the owned venture before this read-only projection.
        with bind(workspace_id=workspace_id, venture_id=venture_id,
                  actor=scope.actor if scope else "system"):
            pack = _run_async(get_memory().context_pack(
                venture_id=venture_id, task=TaskRef(title=title, tags=tags or []),
                token_budget=max(1, byte_budget // 2)))
        if pack.venture_id != venture_id:
            raise ValueError("memory_task_pack_scope_invalid")
        candidates = [hit for hit in pack.memories if hit.kind in _KINDS]
        upstream_omitted = set(getattr(pack, "omitted_memory_ids", []) or [])
        omitted = list(upstream_omitted)
        result["truncated_kinds"] = sorted(set(pack.truncated_kinds or []))
        for hit in candidates:
            if hit.venture_id != venture_id:
                raise ValueError("memory_task_pack_scope_invalid")
            if hit.valid_to is not None or hit.superseded_by or not hit.evidence:
                omitted.append(hit.mem_id)
                continue
            item = {"id": hit.mem_id, "kind": hit.kind.value, "subject": hit.subject,
                    "statement": hit.statement, "evidence": list(hit.evidence),
                    "valid_from": hit.valid_from.isoformat(), "author": hit.author,
                    "qualification": "saved_founder_statement_context_only"}
            proposed = {**result, "statements": [*result["statements"], item]}
            # Leave room for omission metadata; never slice a constraint/value.
            if _bytes(proposed) > byte_budget - 512:
                omitted.append(hit.mem_id)
            else:
                result["statements"].append(item)
        result["omitted_count"] = max(len(upstream_omitted), int(getattr(pack, "omitted_count", 0) or 0)) + len(set(omitted) - upstream_omitted)
        for identity in sorted(set(omitted)):
            proposed = {**result, "omitted_memory_ids": [*result["omitted_memory_ids"], identity]}
            if _bytes(proposed) > byte_budget:
                break
            result["omitted_memory_ids"].append(identity)
        if _bytes(result) > byte_budget:
            raise ValueError("memory_task_metadata_budget_invalid")
        return result
    except Exception:  # noqa: BLE001 - memory availability must not manufacture facts
        return {**result, "availability": "unavailable", "statements": [],
                "omitted_memory_ids": [], "omitted_count": 0, "truncated_kinds": []}

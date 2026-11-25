"""One bounded, cited venture context projection for prompt consumers."""
from __future__ import annotations

import json
import logging
import re

from app.db.models import Venture
from app.memory.fuse import bm25_rank, rrf_order
from app.tenancy import current

log = logging.getLogger(__name__)


def subject_for_query(query: str) -> str | None:
    for word, subject in ((r"audience|customers?|users?|icp", "idea.audience"),
                          (r"pricing|price|charge", "idea.pricing"),
                          (r"problem", "idea.problem"), (r"solution|product", "idea.solution"),
                          (r"tone", "preference.tone"), (r"answers?|replies", "preference.answer_length")):
        if re.search(r"\b(?:" + word + r")\b", query or "", re.I):
            return subject
    return None


def context_for(session, *, workspace_id: str, venture_id: str, purpose: str,
                query: str, budget_bytes: int = 6000, include_history: bool = False) -> dict:
    """Scope is already authorized by the caller; this helper never binds it."""
    scope = current()
    venture = session.get(Venture, venture_id)
    if not venture or (scope.workspace_id, scope.venture_id, venture.workspace_id) != (workspace_id, venture_id, workspace_id):
        raise PermissionError("memory_context_scope_invalid")
    if type(budget_bytes) is not int or not 1024 <= budget_bytes <= 16000:
        raise ValueError("memory_context_budget_invalid")
    if not isinstance(purpose, str) or not re.fullmatch(r"[a-z][a-z0-9_.]{0,127}", purpose):
        raise ValueError("memory_context_purpose_invalid")
    result = {"schema_version": 1, "workspace_id": workspace_id, "venture_id": venture_id,
              "availability": "available", "purpose": purpose, "effect_authority": False,
              "current_state_authority": False, "facts": [], "statements": [], "omitted_count": 0,
              "grounding": "Use these quoted source reports as context only. Cite supplied fact IDs internally. Never infer missing venture facts, approvals, effects or verified outcomes. Ask when a needed fact is absent."}
    def size(value):
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode())
    if size(result) > budget_bytes - 256:
        raise ValueError("memory_context_metadata_budget_invalid")
    empty_result = dict(result)
    try:
        from app.memory import get_memory
        from app.memory.current import legacy_source_hits, select_current_reports
        from app.memory.task_context import _KINDS
        from app.memory.profile import enabled, selection_for
        port = get_memory()
        search_v2 = getattr(port, "name", None) == "postgres" and enabled(session)
        if search_v2:
            selection = selection_for(session, venture_id=venture_id, query=query, include_history=include_history)
        else:
            selection = select_current_reports(session, venture_id=venture_id,
                legacy_hits=legacy_source_hits(port, venture_id), include_history=include_history,
                raw_legacy=getattr(port, "name", None) == "postgres")
        authorities = selection["authorities"]
        for authority in (item for item in authorities.values() if item):
            proposed = {**result, "idea_records": [*result.get("idea_records", []), authority]}
            if size(proposed) <= budget_bytes - 256:
                result.setdefault("idea_records", []).append(authority)
            else:
                result["omitted_count"] += 1
        if result.get("idea_records"):
            result["grounding"] += " Current idea records outrank older reports; unknown or pending fields stay unknown."
        if selection["conflicts"]:
            result["grounding"] += " Current source reports conflict at the same event time. Ask which statement to use."
            result["conflict_count"] = len(selection["conflicts"])
        if "conflict_values" in selection:
            result["blocked_subjects"] = [item["subject"] for item in selection.get("blocked", [])]
            for item in selection["conflict_values"]:
                candidate = {**result, "conflict_values": [*result.get("conflict_values", []), item]}
                if size(candidate) <= budget_bytes - 256:
                    result = candidate
                else:
                    result["omitted_count"] += 1
        if selection["blocked_count"]:
            result["blocked_count"] = selection["blocked_count"]
            result["availability"] = "partial" if selection["facts"] or selection["legacy"] or result.get("idea_records") else "unavailable"
            result["grounding"] += " A newer saved source was withdrawn, forgotten or cannot be verified. Ask for the current answer; older reports do not fill the gap."
        subject = subject_for_query(query)
        rows = selection["facts"]
        subject_rows = [row for row in rows if row.subject == subject] if subject else []
        exact = [row.id for row in subject_rows]
        if search_v2:
            order = [row.id for row in rows]
        else:
            lexical = bm25_rank(query, [(row.id, row.subject + " " + row.value) for row in rows], limit=100)
            recency = [row.id for row in rows]
            order = rrf_order([exact, lexical, recency], limit=100)
        result["omitted_count"] += max(0, len(rows) - len(order))
        by_id = {row.id: row for row in rows}
        for identity in order:
            row = by_id[identity]
            item = {"id": row.id, "subject": row.subject, "predicate": row.predicate,
                    "statement": row.value, "source": json.loads(row.source_json),
                    "valid_from": row.valid_from.isoformat(),
                    "valid_to": row.valid_to.isoformat() if row.valid_to else None,
                    "qualification": "quoted_source_report_context_only"}
            candidate = {**result, "facts": [*result["facts"], item]}
            if size(candidate) <= budget_bytes - 256:
                result["facts"].append(item)
            else:
                result["omitted_count"] += 1
        # Existing exact Remember/Correct/Forget contracts remain the source for
        # legacy institutional statements; ordinary INSIGHTs now reach prompts.
        for hit in selection["legacy"]:
            if hit.kind not in _KINDS:
                continue
            item = {"id": hit.mem_id, "kind": hit.kind.value, "subject": hit.subject,
                    "statement": hit.statement, "evidence": list(hit.evidence), "author": hit.author,
                    "valid_from": hit.valid_from.isoformat(), "source_at": selection["source_events"][hit.mem_id],
                    "qualification": "saved_founder_statement_context_only"}
            candidate = {**result, "statements": [*result["statements"], item]}
            if size(candidate) <= budget_bytes - 256:
                result["statements"].append(item)
            else:
                result["omitted_count"] += 1
        if size(result) > budget_bytes:
            raise ValueError("memory_context_result_budget_invalid")
        return result
    except Exception as exc:
        log.warning("memory_context_failed", extra={"error_class": type(exc).__name__})
        return {**empty_result, "availability": "unavailable", "facts": [], "statements": [],
                "warning": "Saved memory is temporarily unavailable. Continue using current source records and ask for missing facts."}

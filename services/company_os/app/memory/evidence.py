"""Resolve institutional memory against owned source records, never model prose.

Remembered founder statements are reports/preferences, not verified app effects.
Accepted source and published source are distinct receipt kinds. Reading memory
does not grant consent, mutate a task, or contact a provider.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from app.db.models import ChatConversation, ChatMessage, Venture
from app.memory.model import MemoryKind

_REFERENCE = re.compile(r"\A(chat|accepted_source|publication):([A-Za-z0-9_-]{1,128})\Z")


def _digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _scope(session, venture_id: str) -> tuple[str, str]:
    row = session.get(Venture, venture_id)
    if not row or not row.workspace_id:
        raise ValueError("memory_source_scope_invalid")
    return row.workspace_id, row.id


def _resolve(session, reference: str, workspace_id: str, venture_id: str) -> dict[str, Any]:
    match = _REFERENCE.fullmatch(reference) if isinstance(reference, str) else None
    if not match:
        raise ValueError("memory_source_reference_invalid")
    kind, identity = match.groups()
    if kind == "chat":
        row = session.get(ChatMessage, identity)
        chat = session.get(ChatConversation, row.conversation_id) if row else None
        if (not row or (row.workspace_id, row.venture_id) != (workspace_id, venture_id)
                or row.role != "user" or not row.author_person_id or not chat
                or (chat.workspace_id, chat.venture_id) != (workspace_id, venture_id)
                or chat.visibility not in {"private", "legacy_shared"}
                or chat.visibility == "private" and chat.creator_person_id != row.author_person_id
                or chat.visibility == "legacy_shared" and chat.creator_person_id is not None):
            raise ValueError("memory_founder_source_invalid")
        from app.chat.memory_commands import parse
        command = parse(row.body_md)
        if command is None or command.action not in {"memory.remember", "memory.correct"} or not command.statement:
            raise ValueError("memory_explicit_remember_required")
        source = {"id": row.id, "workspace_id": workspace_id, "venture_id": venture_id,
                  "role": row.role, "author_person_id": row.author_person_id,
                  "conversation_id": row.conversation_id, "body_md": row.body_md}
        proof = {"ref": reference, "sha256": _digest(source), "kind": "founder_statement",
                 "statement": command.statement, "author_person_id": row.author_person_id}
        if command.action == "memory.correct":
            proof.update(operation="correct", target_mem_id=command.target)
        return proof

    from app.db.supymem_models import BuildRun, Deployment, FounderApp
    from app.hosting.service import HostingError, _accepted_source

    deployment = session.get(Deployment, identity) if kind == "publication" else None
    if kind == "publication":
        if (not deployment or (deployment.workspace_id, deployment.venture_id)
                != (workspace_id, venture_id) or deployment.state != "live"
                or deployment.target != "supymem_static" or not deployment.live_at
                or deployment.rolled_back_at):
            raise ValueError("memory_publication_source_invalid")
        app = session.get(FounderApp, deployment.app_id)
        # A deployment points to an artifact, not an arbitrary "latest" build.
        from sqlalchemy import select
        run = session.scalar(select(BuildRun).where(
            BuildRun.app_id == deployment.app_id, BuildRun.workspace_id == workspace_id,
            BuildRun.venture_id == venture_id, BuildRun.artifact_id == deployment.build_artifact_id))
    else:
        run = session.get(BuildRun, identity)
        app = session.get(FounderApp, run.app_id) if run else None
    if (not run or not app or (run.workspace_id, run.venture_id, app.workspace_id, app.venture_id)
            != (workspace_id, venture_id, workspace_id, venture_id)):
        raise ValueError("memory_build_source_invalid")
    try:
        # A memory receipt describes recorded history. Requiring current plan
        # dependencies here would recurse back through memory retrieval and
        # falsely erase an accepted source when the venture later replans.
        _, artifact, qa, capture, _ = _accepted_source(session, app, run.id, historical=True)
    except (HostingError, KeyError, TypeError, ValueError) as error:
        raise ValueError("memory_accepted_source_unverified") from error
    source = {"workspace_id": workspace_id, "venture_id": venture_id, "app_id": app.id,
              "build_run_id": run.id, "artifact_id": artifact.id, "artifact_digest": artifact.sha256,
              "qa_id": qa.id, "report_digest": qa.report_digest,
              "manifest_digest": capture["manifest_digest"], "run_spec_digest": _digest(json.loads(run.spec))}
    if deployment:
        from app.org.summary import published_site_is_public
        if not published_site_is_public(True, deployment.url):
            raise ValueError("memory_publication_source_invalid")
        source.update(deployment_id=deployment.id, url=deployment.url,
                      live_at=deployment.live_at.isoformat())
        statement = f"Reviewed static source {run.id} has a recorded live publication at {deployment.url}."
        receipt_kind = "recorded_static_publication"
    else:
        statement = f"Static source {run.id} was accepted with source-linked QA; publication is separate."
        receipt_kind = "accepted_static_source"
    return {"ref": reference, "sha256": _digest(source), "kind": receipt_kind, "statement": statement}


def validate_write_evidence(session, memory) -> list[dict[str, Any]]:
    from app.tenancy import current

    workspace_id, venture_id = _scope(session, memory.venture_id)
    scope = current()
    if (scope.workspace_id, scope.venture_id) != (workspace_id, venture_id):
        raise ValueError("memory_source_scope_invalid")
    if not memory.evidence or len(memory.evidence) > 8 or len(set(memory.evidence)) != len(memory.evidence):
        raise ValueError("memory_source_reference_invalid")
    resolved = [_resolve(session, ref, workspace_id, venture_id) for ref in memory.evidence]
    # Each source must support the complete statement. Mixed or conflicting
    # sources cannot be combined into a stronger generated claim.
    if any(row["statement"] != memory.statement for row in resolved):
        raise ValueError("memory_statement_source_mismatch")
    for row in resolved:
        if row["kind"] == "founder_statement":
            if (row["author_person_id"] != scope.actor or memory.author != row["author_person_id"]
                    or memory.kind in {MemoryKind.OUTCOME, MemoryKind.FAILURE}):
                raise ValueError("memory_founder_source_invalid")
        elif memory.kind != MemoryKind.OUTCOME:
            raise ValueError("memory_receipt_kind_invalid")
    return resolved


def evidence_current(session, record) -> bool:
    try:
        from app.chat.conversations import actor_id
        workspace_id, venture_id = _scope(session, record.venture_id)
        saved = record.metadata.get("source_evidence")
        if record.metadata.get("workspace_id") != workspace_id or not isinstance(saved, list) or not saved:
            return False
        current = [_resolve(session, ref, workspace_id, venture_id) for ref in record.evidence]
        if current != saved or any(row["statement"] != record.statement for row in current):
            return False
        from app.db.models import InstitutionalMemory
        from app.memory.model import MemoryRecord
        for proof in current:
            if proof.get("operation") == "correct":
                target = session.get(InstitutionalMemory, proof["target_mem_id"])
                if not target or (target.workspace_id, target.venture_id) != (workspace_id, venture_id):
                    return False
                previous = MemoryRecord.model_validate_json(target.record_json)
                if (previous.mem_id != target.id or previous.author != record.author
                        or previous.superseded_by != record.mem_id or previous.valid_to is None):
                    return False
        return all((row["kind"] == "founder_statement" and record.author == row["author_person_id"] == actor_id()
                    and record.kind not in {MemoryKind.OUTCOME, MemoryKind.FAILURE})
                   or (row["kind"] != "founder_statement" and record.kind == MemoryKind.OUTCOME) for row in current)
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def validate_forget_source(session, *, venture_id: str, mem_id: str, source_message_id: str) -> dict:
    """Re-admit the exact current conversation's saved Forget instruction."""
    from app.chat.conversations import resolve
    from app.chat.memory_commands import parse
    from app.tenancy import current
    workspace_id, owned_vid = _scope(session, venture_id)
    scope, chat = current(), resolve(venture_id)
    source = session.get(ChatMessage, source_message_id)
    if (not chat.actor_id or scope.actor != chat.actor_id
            or (scope.workspace_id, scope.venture_id) != (workspace_id, owned_vid)
            or not source or (source.workspace_id, source.venture_id, source.conversation_id,
                              source.author_person_id, source.role)
            != (workspace_id, venture_id, chat.id, chat.actor_id, "user")):
        raise ValueError("memory_forget_source_invalid")
    command = parse(source.body_md)
    if command is None or command.action != "memory.forget" or command.target != mem_id:
        raise ValueError("memory_forget_source_invalid")
    return {"source_message_id": source.id, "actor_id": chat.actor_id,
            "source_sha256": _digest({"body_md": source.body_md, "conversation_id": source.conversation_id,
                                      "actor_id": source.author_person_id, "target_mem_id": mem_id})}

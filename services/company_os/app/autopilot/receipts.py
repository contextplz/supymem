"""Private action-local evidence. Founder/model reply dictionaries are never receipts.

Queue IDs are candidates until a fresh scoped read proves committed admission.
Job success alone does not establish a task's artifact or business completion.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from app.db.models import Job, Venture, VentureDocument
from app.state import db
from app.tenancy import current_or_none


@dataclass
class Evidence:
    venture_id: str
    workspace_id: str
    actor: str
    jobs: set[str] = field(default_factory=set)
    documents: list[tuple[str, str, str | None]] = field(default_factory=list)
    document_provenance: dict[str, str] = field(default_factory=dict)
    persisted: list[tuple[type, str, dict]] = field(default_factory=list)
    failure: str | None = None

    def outcome(self) -> str:
        # All candidates must belong to this invocation's exact scope. A rolled
        # back, missing, foreign, or changed candidate cannot certify an effect.
        if max(len(self.jobs), len(self.persisted)) > 16 or len(self.documents) > 32:
            return "held"
        with db()() as session:
            venture = session.get(Venture, self.venture_id)
            if not venture or venture.workspace_id != self.workspace_id:
                return "held"
            job_states = []
            active = {"pending", "scheduled", "retry_wait", "running", "leased"}
            if self.jobs:
                for identifier in self.jobs:
                    row = session.get(Job, identifier)
                    if not row or (row.venture_id, row.workspace_id) != (self.venture_id, self.workspace_id):
                        return "held"
                    try:
                        payload = json.loads(row.payload_json)
                    except (ValueError, TypeError):
                        return "held"
                    if not isinstance(payload, dict) or payload.get("_actor_subject") != self.actor:
                        return "held"
                    job_states.append(row.status)
            if self.persisted:
                for model, identifier, expected in self.persisted:
                    row = session.get(model, identifier)
                    if not row or row.venture_id != self.venture_id or row.workspace_id != self.workspace_id:
                        return "held"
                    if any(getattr(row, key, None) != value for key, value in expected.items()):
                        return "held"
            if self.documents:
                for identifier, kind, body in self.documents:
                    row = session.get(VentureDocument, identifier)
                    if not row or row.venture_id != self.venture_id or row.kind != kind or row.body_md != body:
                        return "held"
                    if identifier in self.document_provenance and row.provenance_json != self.document_provenance[identifier]:
                        return "held"
            # Classify only after every candidate family validates. Active jobs
            # must not conceal a missing/foreign/changed companion artifact.
            if self.failure:
                return "failed" if self.failure == "preflight_no_effect" and not (self.jobs or self.documents or self.persisted) else "held"
            if any(state not in active for state in job_states):
                # Terminal queue state cannot prove failure before effects or goal completion.
                return "held"
            if self.jobs:
                return "queued"
            if self.documents or self.persisted:
                return "completed"
        return "held"


_current: ContextVar[Evidence | None] = ContextVar("autopilot_effect_evidence", default=None)


@contextmanager
def collect(venture_id: str):
    scope = current_or_none()
    if not scope or scope.venture_id != venture_id:
        raise PermissionError("autopilot_scope_mismatch")
    evidence = Evidence(venture_id, scope.workspace_id, scope.actor)
    token = _current.set(evidence)
    try:
        yield evidence
    finally:
        _current.reset(token)


def queued_job(identifier: str) -> None:
    evidence = _current.get()
    if evidence is not None:
        evidence.jobs.add(identifier)


def saved_document(identifier: str, kind: str, body: str | None, *, provenance: str | None = None) -> None:
    evidence = _current.get()
    if evidence is not None:
        evidence.documents.append((identifier, kind, body))
        if provenance is not None:
            evidence.document_provenance[identifier] = provenance


def failed() -> None:
    evidence = _current.get()
    if evidence is not None:
        evidence.failure = "action_failed"


def persisted_record(model: type, identifier: str, expected: dict) -> None:
    """Only source producers call this after their own effect-specific checks.

    Readback repeats exact scope and expected state; no model/payload API exists.
    """
    evidence = _current.get()
    if evidence is not None:
        evidence.persisted.append((model, identifier, dict(expected)))


def failed_before_effect() -> None:
    """Trusted domain producer only: a proven preflight rejection before mutation.

    Never call this from an arbitrary exception handler or provider status.
    Any companion effect candidate still forces an uncertainty hold.
    """
    evidence = _current.get()
    if evidence is not None:
        evidence.failure = "preflight_no_effect"

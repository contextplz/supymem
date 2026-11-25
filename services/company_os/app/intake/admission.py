"""Original entry-slot custody and validated output, without provider or billing I/O."""
from __future__ import annotations

import json
from datetime import timezone

from sqlalchemy import select, update

from app.db.models import Person, Venture, Workspace
from app.db.supymem_models import EntryChatProposal
from app.state import db
from core.clock import utcnow

OUTPUTS_KEY = "_intake_model_outputs"


class AdmissionError(ValueError):
    def __init__(self):
        super().__init__("intake model admission unavailable")


def parse_request_id(purpose: str, request_id: str) -> tuple[str, str | None]:
    """Parse only; billing recovery may precede current authority checks."""
    if type(request_id) is not str or not request_id or len(request_id) > 256:
        raise AdmissionError()
    parts = request_id.split(":", 2)
    if len(parts) != 3 or parts[0] != "entry" or not parts[1]:
        raise AdmissionError()
    if purpose == "intake.classify" and parts[2] == "classify":
        return parts[1], None
    if purpose == "intake.suggest_name" and parts[2].startswith("name:"):
        slot = parts[2][5:]
        if slot and len(slot) <= 120:
            return parts[1], slot
    raise AdmissionError()


def _metadata(raw):
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        raise AdmissionError() from None
    if type(value) is not dict or type(value.get(OUTPUTS_KEY, {})) is not dict:
        raise AdmissionError()
    return value


def _inspect(session, *, actor_id, workspace_id, purpose, request_id, lock=False):
    from app.authz.workspace import subject_person_id

    proposal_id, name_slot = parse_request_id(purpose, request_id)
    if type(actor_id) is not str or not actor_id or subject_person_id(actor_id) != actor_id:
        raise AdmissionError()
    query = select(EntryChatProposal).where(
        EntryChatProposal.id == proposal_id, EntryChatProposal.actor_id == actor_id)
    row = session.scalar(query.with_for_update() if lock else query)
    if row is None:
        raise AdmissionError()
    metadata = _metadata(row.receipt_json)
    record = metadata.get(OUTPUTS_KEY, {}).get(request_id)
    if record is not None:
        keys = {"actor_id", "proposal_id", "workspace_id", "target_venture_id", "purpose", "request_id", "output"}
        if type(record) is not dict or set(record) != keys:
            raise AdmissionError()
        if any(record[key] != value for key, value in {
            "actor_id": actor_id, "proposal_id": proposal_id, "workspace_id": workspace_id,
            "purpose": purpose, "request_id": request_id,
        }.items()):
            raise AdmissionError()
        target = record["target_venture_id"]
        # New-venture confirmation links a target after the original null-scope call.
        if (target is not None and row.target_venture_id != target or
                target is None and row.target_venture_id is not None and metadata.get("created_new") is not True):
            raise AdmissionError()
    else:
        target = None if metadata.get("created_new") is True else row.target_venture_id
        if name_slot is not None:
            requests = metadata.get("name_requests")
            if type(requests) is not dict or type(requests.get(name_slot)) is not dict:
                raise AdmissionError()
    if target is None:
        if workspace_id is not None:
            raise AdmissionError()  # Never substitute an accessible workspace for a new venture.
    else:
        venture = session.get(Venture, target)
        if (type(workspace_id) is not str or not workspace_id or venture is None or
                venture.workspace_id != workspace_id):
            raise AdmissionError()
    identity = {"actor_id": actor_id, "proposal_id": proposal_id, "workspace_id": workspace_id,
                "target_venture_id": target, "purpose": purpose, "request_id": request_id}
    return row, metadata, record, identity, name_slot


def identity(*, actor_id, workspace_id, purpose, request_id) -> dict:
    """Original row/slot identity only; no membership check or output disclosure."""
    with db()() as session:
        return _inspect(session, actor_id=actor_id, workspace_id=workspace_id,
                        purpose=purpose, request_id=request_id)[3]


def _authorize(session, context):
    from fastapi import HTTPException
    from app.authz.workspace import assert_venture_access
    from app.entry.service import _assert_entry_actor

    try:
        _assert_entry_actor(context["actor_id"])
        if session.get(Person, context["actor_id"]) is None:
            raise AdmissionError()
        target = context["target_venture_id"]
        if target is not None:
            venture = session.get(Venture, target)
            workspace = session.get(Workspace, context["workspace_id"])
            if (venture is None or venture.archived_at is not None or venture.stage in {"archived", "purged"}
                    or workspace is None or workspace.archived_at is not None
                    or assert_venture_access(target, context["actor_id"])[1] != context["workspace_id"]):
                raise AdmissionError()
    except HTTPException:
        raise AdmissionError() from None


def _output(purpose, output):
    """Root supplies domain-validated, secret-checked JSON, never a provider response."""
    if output is None:
        return None
    if type(output) is not dict:
        raise AdmissionError()
    try:
        if purpose == "intake.classify":
            from app.intake.smart import IntakeResult
            result = IntakeResult.model_validate(output).model_dump(mode="json")
            if result != output:
                raise AdmissionError()
        else:
            from app.intake.naming import clean_llm_name, is_valid_name
            if (set(output) != {"name"} or type(output["name"]) is not str
                    or clean_llm_name(output["name"]) != output["name"] or not is_valid_name(output["name"])):
                raise AdmissionError()
            result = output
        return json.loads(json.dumps(result, allow_nan=False))
    except (TypeError, ValueError):
        raise AdmissionError() from None


def read(*, actor_id, workspace_id, purpose, request_id) -> dict:
    with db()() as session:
        row, _, record, context, _ = _inspect(session, actor_id=actor_id, workspace_id=workspace_id,
                                          purpose=purpose, request_id=request_id)
        _authorize(session, context)
        if row.target_venture_id is not None and context["target_venture_id"] is None:
            linked = session.get(Venture, row.target_venture_id)
            if linked is None:
                raise AdmissionError()
            _authorize(session, {**context, "target_venture_id": linked.id,
                "workspace_id": linked.workspace_id})
        return {"available": record is not None,
                "output": _output(purpose, record["output"]) if record is not None else None}


record_read = read


def validate_before_dispatch(*, actor_id, workspace_id, purpose, request_id) -> dict:
    with db()() as session:
        row, metadata, record, context, name_slot = _inspect(session, actor_id=actor_id,
            workspace_id=workspace_id, purpose=purpose, request_id=request_id, lock=True)
        _authorize(session, context)
        expires = row.expires_at.replace(tzinfo=None) if row.expires_at.tzinfo is None else row.expires_at.astimezone(timezone.utc).replace(tzinfo=None)
        now = utcnow()
        now = now.replace(tzinfo=None) if now.tzinfo is None else now.astimezone(timezone.utc).replace(tzinfo=None)
        if record is not None or expires <= now:
            raise AdmissionError()
        if name_slot is None:
            if row.state != "classifying" or row.target_venture_id != context["target_venture_id"]:
                raise AdmissionError()
        elif (row.state != "pending" or row.target_venture_id is not None or
                metadata["name_requests"][name_slot].get("state") != "calling"):
            raise AdmissionError()
        return context


def checkpoint(*, actor_id, workspace_id, purpose, request_id, output) -> dict:
    """Persist the original slot's caller JSON before settlement; do not alter public state."""
    output = _output(purpose, output)
    with db()() as session:
        row, metadata, record, context, _ = _inspect(session, actor_id=actor_id,
            workspace_id=workspace_id, purpose=purpose, request_id=request_id, lock=True)
        wanted = {**context, "output": output}
        if record is not None:
            if record != wanted:
                raise AdmissionError()
            return {"available": True, "output": output}
        raw = row.receipt_json
        metadata.setdefault(OUTPUTS_KEY, {})[request_id] = wanted
        changed = session.execute(update(EntryChatProposal).where(
            EntryChatProposal.id == row.id, EntryChatProposal.actor_id == actor_id,
            EntryChatProposal.state == row.state,
            EntryChatProposal.target_venture_id.is_(None) if row.target_venture_id is None else EntryChatProposal.target_venture_id == row.target_venture_id,
            EntryChatProposal.receipt_json.is_(None) if raw is None else EntryChatProposal.receipt_json == raw,
        ).values(receipt_json=json.dumps(metadata, allow_nan=False)).execution_options(synchronize_session=False))
        if changed.rowcount != 1:
            raise AdmissionError()
        session.commit()
        session.expire_all()
        saved = _inspect(session, actor_id=actor_id, workspace_id=workspace_id,
                         purpose=purpose, request_id=request_id)[2]
        if saved != wanted:
            raise AdmissionError()
        return {"available": True, "output": output}

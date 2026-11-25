"""Current saved draft custody, explicit quote confirmation and durable jobs."""
from __future__ import annotations

import hashlib
import json
import os
import time

from fastapi import HTTPException
from sqlalchemy import select

from app.builder.access import authorize
from app.core.jobs import enqueue, publish_wake
from app.core.local_mode import is_local
from app.core.local_capabilities import enabled as capability_enabled, blocked_message
from app.db.models import CreditReservation, Job, VentureDocument
from app.db.session import serialize_writes
from app.db.supymem_models import IdeaRevision, IdeaSession
from app.gtm.journey import _draft_revision
from app.state import db
from .contracts import BADGE, DISCLAIMER, MAX_CALLS, MAX_OUTPUT_TOKENS, MAX_PERSONAS, MAX_PROMPT_TOKENS, MAX_SECONDS, VERSION, DraftRef

KIND = "simulation.rehearse"


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def enabled(workspace_id: str) -> bool:
    if is_local():
        return capability_enabled("run_for_me_experiments")
    allow = {x.strip() for x in os.getenv("SUPYMEM_SIMULATION_WORKSPACES", "").split(",") if x.strip()}
    return os.getenv("SUPYMEM_SIMULATION_ENABLED", "").lower() in {"true", "1"} and (not allow or workspace_id in allow)


def authority(session, workspace_id, venture_id, actor):
    if not enabled(workspace_id):
        if is_local():
            raise HTTPException(403, blocked_message("run_for_me_experiments"))
        raise KeyError("simulation_not_found")
    venture = authorize(session, workspace_id=workspace_id, venture_id=venture_id, actor=actor, mutate=True)
    if venture.archived_at:
        raise KeyError("simulation_not_found")


def snapshot(session, workspace_id: str, venture_id: str, actor: str, drafts: list[dict]) -> dict:
    authority(session, workspace_id, venture_id, actor)
    refs = [DraftRef.model_validate(d).model_dump() for d in drafts]
    if not 1 <= len(refs) <= 3 or len({d["doc_id"] for d in refs}) != len(refs):
        raise ValueError("simulation_drafts_required")
    idea = session.scalar(select(IdeaSession).where(IdeaSession.workspace_id == workspace_id,
        IdeaSession.venture_id == venture_id).order_by(IdeaSession.created_at.desc(), IdeaSession.id.desc()))
    revision = session.get(IdeaRevision, idea.confirmed_revision_id) if idea else None
    if not idea or idea.state != "confirmed" or idea.current_revision_id != idea.confirmed_revision_id or not revision or (
            revision.workspace_id, revision.venture_id, revision.session_id) != (workspace_id, venture_id, idea.id):
        raise ValueError("simulation_confirmed_revision_required")
    journey = session.scalar(select(VentureDocument).where(VentureDocument.venture_id == venture_id,
        VentureDocument.kind == "gtm_journey"))
    icp = json.loads(journey.provenance_json or "{}").get("icp", {}) if journey else {}
    if icp.get("confirmed") is not True or icp.get("needs_input") or not isinstance(icp.get("who"), str) or not 1 <= len(icp["who"]) <= 600:
        raise ValueError("simulation_confirmed_audience_required")
    variants = []
    for i, ref in enumerate(refs, 1):
        row = session.get(VentureDocument, ref["doc_id"])
        if not row or row.venture_id != venture_id or row.kind != "outreach_draft":
            raise KeyError("simulation_not_found")
        data = json.loads(row.provenance_json or "{}") | {"doc_id": row.id}
        if _draft_revision(data) != ref["revision"] or data.get("state") != "draft" or data.get("body") != row.body_md:
            raise ValueError("simulation_draft_changed")
        subject, body = data.get("subject"), data.get("body")
        if not isinstance(subject, str) or not 1 <= len(subject) <= 120 or not isinstance(body, str) or not 1 <= len(body) <= 2000:
            raise ValueError("simulation_draft_too_long")
        # Contact names, addresses and source URLs stay out of the provider context.
        variants.append({"id": f"v{i}", "subject": subject, "body": body})
    return {"revision_id": revision.id, "idea_digest": digest({"summary": revision.summary, "structured": revision.structured}),
            "audience": icp["who"], "variants": variants, "drafts": refs, "version": VERSION}


def resources(workspace_id: str) -> dict:
    from app.credits.estimate import estimate_llm_micro
    from app.llm.founder_provider import resolve_llm_config, resolved_provider_identity
    from app.llm.pricing import pricing_table
    config = resolve_llm_config(workspace_id)
    if not config.key_is_set:
        raise ValueError("simulation_provider_unavailable")
    ceiling = estimate_llm_micro(config.model, prompt_tokens=MAX_PROMPT_TOKENS, max_tokens=MAX_OUTPUT_TOKENS)
    return {"provider": config.provider, "model": config.model, "source": config.source,
        "provider_identity": resolved_provider_identity(config), "pricing_digest": digest(pricing_table()),
        "call_ceiling_micro": ceiling, "ceiling_micro": 0 if config.source == "founder_override" else MAX_CALLS * ceiling,
        "max_calls": MAX_CALLS, "max_personas": MAX_PERSONAS, "max_seconds": MAX_SECONDS,
        "max_output_tokens": MAX_OUTPUT_TOKENS, "max_prompt_tokens": MAX_PROMPT_TOKENS, "policy": VERSION}


def quote(workspace_id, venture_id, actor, drafts, *, epoch=None) -> dict:
    with db()() as session:
        frozen = snapshot(session, workspace_id, venture_id, actor, drafts)
        existing = existing_input(session, workspace_id, venture_id, actor, digest(frozen))
        if existing:
            # Recover the owned durable receipt without acquiring a fresh paid intent.
            row = session.get(Job, existing["job_id"])
            saved = json.loads(row.payload_json)["quote"]
            return {**saved, "existing_job": existing}
    # Five-minute server-derived window; start recomputes every billable input.
    now = int(time.time())
    epoch = now // 300 if epoch is None else epoch
    if type(epoch) is not int or epoch != now // 300:
        raise ValueError("simulation_quote_expired")
    result = {"workspace_id": workspace_id, "venture_id": venture_id, "actor_id": actor,
        "input_digest": digest(frozen), "resources": resources(workspace_id), "epoch": epoch,
        "expires_at": (epoch + 1) * 300, "badge": BADGE, "disclaimer": DISCLAIMER}
    return result | {"quote_digest": digest(result), "existing_job": existing}


def existing_input(session, workspace_id, venture_id, actor, input_digest):
    # Existing job storage is the index. No second paid intent for the same frozen input.
    rows = session.scalars(select(Job).where(Job.kind == KIND, Job.workspace_id == workspace_id,
        Job.venture_id == venture_id).order_by(Job.created_at.desc()))
    for row in rows:
        doc = json.loads(row.payload_json)
        if doc["actor_id"] == actor and doc["quote"]["input_digest"] == input_digest:
            return view(row, doc, session)
    return None


def view(row, document: dict, session) -> dict:
    runtime = document["_runtime"]
    operations = runtime["operations"]
    uncertain = any(x.get("state") in {"calling", "uncertain"} for x in operations.values())
    reservations = []
    for op in operations.values():
        usage = op.get("usage", {})
        reservation = session.get(CreditReservation, usage["reservation_id"]) if usage.get("reservation_id") else None
        if reservation and reservation.account_id == usage.get("account_id") and reservation.workspace_id == row.workspace_id and reservation.purpose == KIND:
            reservations.append(reservation)
    state = "completed" if runtime.get("result") else "needs_attention" if runtime.get("error_code") or row.status == "failed_dead" else "running" if row.status == "leased" else "queued"
    return {"job_id": row.id, "workspace_id": row.workspace_id, "venture_id": row.venture_id,
        "actor_id": document["actor_id"], "input_digest": document["quote"]["input_digest"],
        "state": state, "uncertain": uncertain, "error_code": runtime.get("error_code"),
        "credits_held": any(r.status in {"open", "active", "reconciling"} for r in reservations),
        "badge": BADGE, "disclaimer": DISCLAIMER, "result": runtime.get("result"),
        "credits_micro": sum(r.settled_micro or 0 for r in reservations if r.status == "settled"),
        "call_count": len(operations)}


def start(workspace_id, venture_id, actor, drafts, *, epoch, quote_digest, confirmed: bool, request_id: str) -> dict:
    if confirmed is not True or not isinstance(request_id, str) or not 1 <= len(request_id) <= 100:
        raise ValueError("simulation_confirmation_required")
    dedupe = "simulation:" + digest([workspace_id, venture_id, actor, request_id])
    with db()() as session:
        serialize_writes(session, key="simulation-venture:" + digest([workspace_id, venture_id]))
        frozen = snapshot(session, workspace_id, venture_id, actor, drafts)
        existing = session.scalar(select(Job).where(Job.dedupe_key == dedupe))
        if existing:
            document = json.loads(existing.payload_json)
            if document["quote"]["input_digest"] != digest(frozen) or document["quote"]["quote_digest"] != quote_digest:
                raise ValueError("simulation_request_changed")
            return view(existing, document, session)
        reviewed = quote(workspace_id, venture_id, actor, drafts, epoch=epoch)
        if reviewed["quote_digest"] != quote_digest or reviewed["input_digest"] != digest(frozen):
            raise ValueError("simulation_quote_changed")
        saved = existing_input(session, workspace_id, venture_id, actor, digest(frozen))
        if saved:
            return saved
        # A changed draft cannot be used to repeat an unresolved provider request.
        for row in session.scalars(select(Job).where(Job.kind == KIND, Job.workspace_id == workspace_id, Job.venture_id == venture_id)):
            old = json.loads(row.payload_json)
            if any(op.get("state") in {"calling", "uncertain", "responded"}
                    for op in old["_runtime"]["operations"].values()):
                raise ValueError("simulation_previous_outcome_unresolved")
        payload = {"actor_id": actor, "_actor_subject": actor, "snapshot": frozen, "quote": reviewed,
            "_runtime": {"operations": {}}}
        jid = enqueue(session, KIND, payload, workspace_id=workspace_id, venture_id=venture_id,
            dedupe_key=dedupe, max_attempts=1)
        session.flush()
        row = session.get(Job, jid)
        row.timeout_s = MAX_SECONDS
        result = view(row, json.loads(row.payload_json), session)
        session.commit()
    publish_wake()
    return result


def status(workspace_id, venture_id, actor, job_id) -> dict:
    with db()() as session:
        authority(session, workspace_id, venture_id, actor)
        row = session.get(Job, job_id)
        if not row or (row.kind, row.workspace_id, row.venture_id) != (KIND, workspace_id, venture_id):
            raise KeyError("simulation_not_found")
        document = json.loads(row.payload_json)
        if document["actor_id"] != actor:
            raise KeyError("simulation_not_found")
        current = snapshot(session, workspace_id, venture_id, actor, document["snapshot"]["drafts"])
        if digest(current) != document["quote"]["input_digest"]:
            raise ValueError("simulation_input_changed")
        return view(row, document, session)

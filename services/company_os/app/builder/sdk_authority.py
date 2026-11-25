"""Trusted controller seam for the pinned SDK's persisted event log.

The sandbox cannot call these methods with its model-only credential. An outside
controller must first read the SDK's durable event store; an on_event callback or
sandbox-supplied response digest is not proof. Only bounded metadata is mirrored.
"""
import json
import re

from sqlalchemy import select

from app.builder import model_proxy
from app.builder.model_proxy import BuildModelError, _authority, _locked, _response
from app.credits._txn import naive_utc
from app.db.supymem_models import BuildEvent, BuildModelCall
from app.state import db

SUPPORTED_EVENTS = {"SystemPromptEvent", "MessageEvent", "ActionEvent", "ObservationEvent", "AgentErrorEvent",
                    "ConversationErrorEvent", "ConversationStateUpdateEvent"}
FIELDS = {"event_id", "parent_id", "event_type", "llm_response_id", "tool_call_id", "action_id", "event_digest", "message_role", "source"}


def _controller(session, ctx, generation, controller_id, attempt, *, mutation=True):
    spec = _authority(session, ctx)
    expected = spec.get("controller_fence", {})
    if (generation, controller_id, attempt) != (spec.get("generation"), expected.get("id"), expected.get("attempt")):
        raise BuildModelError("build_controller_fence_lost")
    if mutation:
        model_proxy._fence(ctx, spec, generation, controller_id, attempt)
    return spec


def _mirrors(session, run_id):
    rows = session.scalars(select(BuildEvent).where(BuildEvent.build_run_id == run_id,
        BuildEvent.kind == "agent_event").order_by(BuildEvent.seq).limit(2001)).all()
    if len(rows) > 2000:
        raise BuildModelError("build_sdk_event_limit")
    return [(row, json.loads(row.payload)) for row in rows]


def read_receipt(run_id, invocation_id, *, generation, controller_id, attempt):
    with db()() as session:
        ctx = _locked(session, run_id)
        _controller(session, ctx, generation, controller_id, attempt, mutation=False)
        row = session.get(BuildModelCall, invocation_id)
        if not row or row.build_run_id != run_id or row.generation != generation:
            raise BuildModelError("build_invocation_conflict")
        expected = None
        if row.state in {"responded", "settled"} and not row.error_code:
            calls = _response(row)["choices"][0]["message"].get("tool_calls", [])
            expected = {"event_type": "ActionEvent" if calls else "MessageEvent", "tool_call_id": calls[0]["id"] if calls else None}
        return {"invocation_id": row.id, "response_id": row.id, "response_digest": row.response_digest,
                "input_event_id": row.input_event_id, "state": row.state, "sequence": row.sequence,
                "expected": expected, "error_code": row.error_code, "consumed": row.consumed_event_seq is not None,
                "consumed_event_seq": row.consumed_event_seq, "consumed_event_id": row.consumed_event_id}


def mirror(run_id, event: dict, *, generation, controller_id, attempt):
    """Idempotent append of controller-validated, already persisted SDK metadata."""
    if not isinstance(event, dict) or set(event) - FIELDS or event.get("event_type") not in SUPPORTED_EVENTS:
        raise BuildModelError("build_sdk_event_invalid")
    for key in ("event_id", "parent_id", "llm_response_id", "tool_call_id", "action_id"):
        value = event.get(key)
        if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value)):
            raise BuildModelError("build_sdk_event_identifier_invalid")
    if not event.get("event_id") or not re.fullmatch(r"[a-f0-9]{64}", str(event.get("event_digest", ""))):
        raise BuildModelError("build_sdk_event_digest_required")
    if event.get("message_role") not in {None, "user", "assistant", "system"} or event.get("source") not in {None, "agent", "user", "environment"}:
        raise BuildModelError("build_sdk_event_role_invalid")
    observed = {key: event.get(key) for key in sorted(FIELDS)}
    with db()() as session:
        ctx = _locked(session, run_id)
        spec = _controller(session, ctx, generation, controller_id, attempt)
        mirrors = _mirrors(session, run_id)
        for row, payload in mirrors:
            if payload["event_id"] == observed["event_id"]:
                if any(payload.get(key) != observed[key] for key in FIELDS):
                    raise BuildModelError("build_sdk_event_conflict")
                return {"seq": row.seq, "event_id": observed["event_id"]}
        if len(mirrors) >= 2000:
            raise BuildModelError("build_sdk_event_limit")
        leaf = spec.get("mirrored_cursor", spec.get("agent_cursor"))
        changes_leaf = observed["event_type"] != "ConversationStateUpdateEvent"
        if changes_leaf and (observed["parent_id"] != leaf or observed["event_id"] == leaf):
            raise BuildModelError("build_sdk_mirror_parent_conflict")
        response_id = observed["llm_response_id"]
        if response_id:
            receipt = session.get(BuildModelCall, response_id, with_for_update=True)
            if not receipt or receipt.build_run_id != run_id or receipt.generation != generation or receipt.state != "settled" or receipt.error_code:
                raise BuildModelError("build_sdk_receipt_unavailable")
            expected = _response(receipt)["choices"][0]["message"].get("tool_calls", [])
            if observed["event_type"] != ("ActionEvent" if expected else "MessageEvent"):
                raise BuildModelError("build_sdk_response_event_mismatch")
            if expected and observed["tool_call_id"] != expected[0]["id"]:
                raise BuildModelError("build_sdk_response_tool_mismatch")
            observed.update(invocation_id=receipt.id, response_digest=receipt.response_digest)
        elif observed["event_type"] == "ActionEvent" or (observed["event_type"] == "MessageEvent" and observed["message_role"] != "user"):
            raise BuildModelError("build_sdk_response_receipt_required")
        if observed["event_type"] in {"ObservationEvent", "AgentErrorEvent"}:
            if not any(payload.get("event_type") == "ActionEvent" and payload.get("tool_call_id") == observed["tool_call_id"]
                       and (observed["event_type"] != "ObservationEvent" or payload["event_id"] == observed["action_id"]) for _, payload in mirrors):
                raise BuildModelError("build_sdk_observation_action_missing")
        ctx.run.event_seq += 1
        row = BuildEvent(build_run_id=run_id, seq=ctx.run.event_seq, ts=naive_utc(), kind="agent_event",
                         payload=json.dumps(observed, sort_keys=True))
        session.add(row)
        if changes_leaf:
            spec["mirrored_cursor"] = observed["event_id"]
        ctx.run.spec = json.dumps(spec, sort_keys=True)
        session.commit()
        return {"seq": row.seq, "event_id": observed["event_id"]}


def advance_cursor(run_id, event_id, *, generation, controller_id, attempt):
    """After consuming the reply, advance through stored observations to SDK leaf."""
    with db()() as session:
        ctx = _locked(session, run_id)
        spec = _controller(session, ctx, generation, controller_id, attempt)
        receipts = session.scalars(select(BuildModelCall).where(BuildModelCall.build_run_id == run_id)).all()
        if any(row.sequence > spec.get("consumed_sequence", 0) for row in receipts):
            raise BuildModelError("build_prior_invocation_not_consumed")
        if event_id != spec.get("mirrored_cursor"):
            raise BuildModelError("build_sdk_cursor_not_mirrored_leaf")
        parents = {payload["event_id"]: payload.get("parent_id") for _, payload in _mirrors(session, run_id)}
        cursor, seen = event_id, set()
        while cursor != spec.get("agent_cursor"):
            if cursor is None or cursor in seen or cursor not in parents:
                raise BuildModelError("build_sdk_event_ancestry_unverified")
            seen.add(cursor)
            cursor = parents[cursor]
        spec["agent_cursor"] = event_id
        ctx.run.spec = json.dumps(spec, sort_keys=True)
        session.commit()
        return {"input_event_id": event_id}


# The external transport adapter uses these exact function contracts. It must
# keep register/consume/controller identity outside the role-free SDK task.
register = model_proxy.register
consume = model_proxy.consume

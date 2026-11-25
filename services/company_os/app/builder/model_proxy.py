"""Durable model invocation accounting, separate from the untrusted sandbox.

The controller must first persist a distinct invocation ID at its SDK event
boundary. Request hashes detect conflicting reuse; they never deduplicate two
intentional identical prompts. Provider calls are single-attempt and uncertain
outcomes retain their hold for reconciliation, including after worker death.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from datetime import timedelta

from sqlalchemy import func, select
from llm_gateway.boundary import BoundaryViolation

from app.credits import pools
from app.credits._txn import naive_utc
from app.db.models import Workspace, WorkspaceMember
from app.db.supymem_models import BuildEvent, BuildModelCall, BuildRun
from app.state import db


class BuildModelError(ValueError):
    def __init__(self, code: str, *, diagnostics: dict | None = None):
        super().__init__(code)
        self.diagnostics = diagnostics


logger = logging.getLogger(__name__)
# Layer total deadlines below the 350-second builder transport and 360-second
# control ALB. HTTP inactivity timeouts alone do not bound total duration.
PROVIDER_DEADLINE_SECONDS = 330
# Match the managed-builder envelope. This remains a hard serialized-byte cap;
# message and tool-count limits below still constrain request shape.
MAX_BUILD_MODEL_REQUEST_BYTES = 262_144
UNAVAILABLE_PROVIDER_TOOL = "supymem_unavailable_tool"
MAX_UNAVAILABLE_PROVIDER_TOOLS = 2


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _review_provider_tool(message: dict, offered: set[str], invocation_id: str) -> bool:
    """Turn an unoffered model tool into the SDK's inert unknown-tool error.

    OpenHands reports an unknown tool to the model as an ActionEvent and an
    AgentErrorEvent, so it can choose an offered tool on its next bounded step.
    Never pass the model's unknown name through: the SDK aliases names such as
    ``git`` to ``terminal``, which would turn a refused call into execution.
    """
    calls = message.get("tool_calls", [])
    if not calls:
        return False
    call = calls[0]
    fn = call.get("function") if isinstance(call, dict) else None
    if not isinstance(fn, dict) or not isinstance(fn.get("arguments"), str):
        raise BuildModelError("build_returned_tool_arguments_invalid")
    original_id = call.get("id")
    if not isinstance(original_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", original_id):
        # The SDK event/receipt chain needs a bounded stable ID. This is a
        # transport identifier only, never a grant of tool authority.
        call["id"] = "call_" + hashlib.sha256(
            (invocation_id + ":" + str(original_id)).encode()).hexdigest()[:32]
    name = fn.get("name")
    if call.get("type") != "function" or not isinstance(name, str) or name not in offered:
        call["type"] = "function"
        fn["name"] = UNAVAILABLE_PROVIDER_TOOL
        fn["arguments"] = "{}"
        return True
    if not isinstance(json.loads(fn["arguments"]), dict):
        raise BuildModelError("build_returned_tool_arguments_invalid")
    return False


def bounded_request(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) - {"model", "messages", "max_tokens", "tools", "stream"}:
        raise BuildModelError("build_model_request_invalid")
    if value.get("stream", False) is not False:
        raise BuildModelError("build_model_streaming_unavailable")
    maximum = value.get("max_tokens", 4096)
    messages, tools = value.get("messages"), value.get("tools", [])
    if type(maximum) is not int or not 1 <= maximum <= 16384:
        raise BuildModelError("build_model_output_limit")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 120:
        raise BuildModelError("build_model_messages_invalid")
    for message in messages:
        if not isinstance(message, dict) or set(message) - {"role", "content", "tool_calls", "tool_call_id", "reasoning_content"}:
            raise BuildModelError("build_model_message_invalid")
        if message.get("role") not in {"system", "user", "assistant", "tool"}:
            raise BuildModelError("build_model_message_invalid")
        if any(v is not None and not isinstance(v, str) for k, v in message.items() if k != "tool_calls"):
            raise BuildModelError("build_model_message_invalid")
        if "tool_calls" in message and (message["role"] != "assistant" or not isinstance(message["tool_calls"], list)):
            raise BuildModelError("build_model_message_invalid")
    if not isinstance(tools, list) or len(tools) > 32:
        raise BuildModelError("build_model_tools_invalid")
    for tool in tools:
        if not isinstance(tool, dict) or set(tool) != {"type", "function"} or tool["type"] != "function":
            raise BuildModelError("build_model_tools_invalid")
        fn = tool["function"]
        if not isinstance(fn, dict) or set(fn) - {"name", "description", "parameters"} or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,63}", str(fn.get("name", ""))):
            raise BuildModelError("build_model_tools_invalid")
        if not isinstance(fn.get("parameters"), dict):
            raise BuildModelError("build_model_tools_invalid")
    normalized = {"messages": messages, "tools": tools, "max_tokens": maximum, "model": value.get("model")}
    # Use the same compact representation as the builder's canonical encoder,
    # so neither side can accept a near-limit request that the other rejects.
    serialized = json.dumps(normalized, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":")).encode()
    if len(serialized) > MAX_BUILD_MODEL_REQUEST_BYTES:
        raise BuildModelError("build_model_input_limit")
    return normalized


def _locked(session, run_id):
    pool_id = session.scalar(select(BuildRun.reservation_id).where(BuildRun.id == run_id))
    if not pool_id:
        raise BuildModelError("build_run_not_managed")
    return pools.lock_pool(session, pool_id)


def _authority(session, ctx):
    spec = json.loads(ctx.run.spec or "{}")
    ws = session.get(Workspace, ctx.run.workspace_id)
    actor = spec.get("actor")
    member = session.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == ctx.run.workspace_id,
                                                         WorkspaceMember.person_id == actor))
    role = "owner" if ws and ws.owner_id == actor else member.role if member else None
    if not ws or ws.archived_at or json.loads(ws.settings_json or "{}").get("blocked") or role not in {"owner", "admin", "maintainer"}:
        raise BuildModelError("build_authority_revoked")
    return spec


def _fence(ctx, spec, generation, controller_id, attempt, *, provisioning=False):
    if (spec.get("lifecycle_version") in {3, 4}
            or ctx.run.trigger in {"coding_source_import", "subscription_product_diagnostic"}):
        raise BuildModelError("subscription_static_model_calls_forbidden")
    expected = spec.get("controller_fence", {})
    if (generation, controller_id, attempt) != (spec.get("generation"), expected.get("id"), expected.get("attempt")):
        raise BuildModelError("build_controller_fence_lost")
    if ctx.run.closed_at or spec.get("credentials_revoked") or ctx.pool.status != "active" or naive_utc(ctx.pool.expires_at) <= naive_utc():
        raise BuildModelError("build_run_not_accepting_calls")
    if ctx.run.state not in ({"running", "provisioning_sandbox"} if provisioning else {"running"}):
        raise BuildModelError("build_run_not_running")


def register(run_id: str, *, invocation_key: str, request: dict, generation: int,
             controller_id: str, attempt: int, input_event_id: str | None) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", invocation_key or ""):
        raise BuildModelError("build_invocation_id_invalid")
    if input_event_id is not None and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", input_event_id):
        raise BuildModelError("build_sdk_cursor_invalid")
    body = bounded_request(request)
    request_digest = digest(body)
    with db()() as session:
        ctx = _locked(session, run_id)
        spec = _authority(session, ctx)
        _fence(ctx, spec, generation, controller_id, attempt)
        if spec.get("builder_admission_closed_at"):
            raise BuildModelError("build_builder_admission_closed")
        prior = session.scalar(select(BuildModelCall).where(BuildModelCall.build_run_id == run_id,
            BuildModelCall.invocation_key == invocation_key).with_for_update().execution_options(populate_existing=True))
        if prior:
            if prior.request_digest != request_digest or prior.generation != generation or prior.input_event_id != input_event_id:
                raise BuildModelError("build_invocation_conflict")
            if prior.state == "allocated":
                prior.worker_id, prior.attempt = controller_id, attempt
                session.commit()
            return {"invocation_id": prior.id, "state": prior.state, "sequence": prior.sequence}
        if input_event_id != spec.get("agent_cursor"):
            raise BuildModelError("build_sdk_cursor_conflict")
        previous = session.scalar(select(func.max(BuildModelCall.sequence)).where(BuildModelCall.build_run_id == run_id)) or 0
        if previous != spec.get("consumed_sequence", 0):
            raise BuildModelError("build_prior_invocation_not_consumed")
        maximum = spec.get("runtime", {}).get("runtime_policy", {}).get("max_steps", 40)
        if type(maximum) is not int or not 1 <= maximum <= 80 or previous >= maximum:
            raise BuildModelError("build_model_step_limit")
        allowed_tools = spec.get("allowed_tool_names", [])
        if any(t["function"]["name"] not in allowed_tools for t in body["tools"]):
            raise BuildModelError("build_tool_not_allowed")
        config = spec.get("model_config", {})
        if body["model"] not in (None, config.get("model")) or config.get("source") != "platform_default" or config.get("provider") != "azure_foundry" or config.get("model") != "DeepSeek-V4-Pro":
            raise BuildModelError("build_model_configuration_unavailable")
        from app.builder.budgets import model_pricing, priced_usage
        pricing = model_pricing(config["model"])
        if not pricing or config.get("pricing") != pricing:
            raise BuildModelError("build_model_pricing_changed")
        # UTF-8 bytes plus per-message overhead bounds input more conservatively
        # than character/3.5 estimation for tool schemas and non-English text.
        input_bound = len(json.dumps(body, ensure_ascii=False).encode()) + 64 * len(body["messages"])
        ceiling = max(100, priced_usage(pricing, prompt_tokens=input_bound, completion_tokens=body["max_tokens"]))
        rows = session.scalars(select(BuildModelCall).where(BuildModelCall.build_run_id == run_id)).all()
        committed = sum(c.charged_micro or 0 if c.state == "settled" else c.ceiling_micro if c.state not in {"released", "failed"} else 0 for c in rows)
        if committed + ceiling > spec.get("model_ceiling_micro", 0):
            raise BuildModelError("build_model_budget_exhausted")
        cid = "bmc_" + hashlib.sha256((run_id + ":" + invocation_key).encode()).hexdigest()[:32]
        hold = pools.allocate(session, ctx, reservation_id="crv_" + cid[4:], amount_micro=ceiling,
                              purpose="build.model", model=config["model"], provider=config["provider"])
        now = naive_utc()
        row = BuildModelCall(id=cid, build_run_id=run_id, workspace_id=ctx.run.workspace_id, venture_id=ctx.run.venture_id,
            invocation_key=invocation_key, input_event_id=input_event_id, pricing_json=json.dumps(pricing, sort_keys=True), request_digest=request_digest, sequence=previous + 1,
            purpose="build.model", state="allocated", generation=generation, worker_id=controller_id,
            attempt=attempt, reservation_id=hold.reservation_id, provider=config["provider"], model=config["model"],
            ceiling_micro=ceiling, created_at=now, updated_at=now)
        session.add(row)
        session.commit()
        return {"invocation_id": cid, "state": row.state, "sequence": row.sequence}


def _row(session, ctx, invocation_id, body):
    row = session.scalar(select(BuildModelCall).where(BuildModelCall.id == invocation_id,
        BuildModelCall.build_run_id == ctx.run.id).with_for_update().execution_options(populate_existing=True))
    if row is None or row.request_digest != digest(body):
        raise BuildModelError("build_invocation_conflict")
    return row


def _response(row):
    from app.connectors.secrets import decrypt_at_rest
    saved = json.loads(decrypt_at_rest(row.response_ref))
    if saved.get("invocation_id") != row.id or saved.get("workspace_id") != row.workspace_id or digest(saved["response"]) != row.response_digest:
        raise BuildModelError("build_response_integrity_failed")
    return saved["response"]


def _settle(session, ctx, row):
    if row.state == "responded":
        result = pools.settle_child(session, ctx, reservation_id=row.reservation_id, actual_micro=row.actual_micro,
            prompt_tokens=row.prompt_tokens, completion_tokens=row.completion_tokens,
            metadata={"invocation_id": row.id, "invocation_key": row.invocation_key})
        row.charged_micro = result.actual_micro
        row.state, row.settled_at, row.updated_at = "settled", naive_utc(), naive_utc()


def _provider_call(workspace_id, body):
    from app.llm.founder_provider import resolve_llm_config
    from llm_gateway import sole
    from llm_gateway.provider_foundry import load_provider_config_from_env
    cfg = resolve_llm_config(workspace_id)
    if cfg.source != "platform_default" or cfg.provider != "azure_foundry" or cfg.model != "DeepSeek-V4-Pro":
        raise BuildModelError("build_model_configuration_changed")
    if os.environ.get("LLM_OFFLINE", "0").lower() in {"1", "true", "yes"}:
        raise BuildModelError("build_provider_offline")
    # This invocation was allocated against the platform pool. A workspace
    # settings edit must not switch its final dial to a founder-owned key.
    config = load_provider_config_from_env().model_copy(deep=True)
    if (config.source, config.provider, config.deployment_pro, config.base_url.rstrip("/")) != (
        "platform_default", cfg.provider, cfg.model, (cfg.base_url or "").rstrip("/")):
        raise BuildModelError("build_model_configuration_changed")
    # A priced 16k-token product turn can legitimately exceed two minutes.
    # Keep managed calls below the builder transport (350s) and ALB (360s).
    # This isolated route owns its layered deadline. The platform-wide default
    # is currently 120s, which is too short for a priced 16k-token build turn;
    # merely raising a ceiling would leave that ambient value active.
    config.timeout_seconds = 325
    from app.boundary import DataClass, Segment
    # Tool messages come from the isolated, reviewed build runtime. Mark their
    # provenance so ordinary command output (diffs, test tables, file listings)
    # is not mistaken for a bulk customer-data export. The boundary continues
    # to scan every message and still refuses secrets, PII and unknown blobs.
    segments = [
        Segment(
            text=message.get("content") or "",
            origin="reviewed_build_tool" if message.get("role") == "tool" else "first_party",
            declared_class=(DataClass.WORKSPACE_CODE if message.get("role") == "tool" else None),
            label=f"the {message.get('role', 'unknown')} build message",
        )
        for message in body["messages"]
    ]
    async def invoke():
        async with asyncio.timeout(PROVIDER_DEADLINE_SECONDS):
            return await sole.transmit(body["messages"], tools=body["tools"], purpose="build.model",
                workspace_id=workspace_id, tier="pro", max_tokens=body["max_tokens"], single_attempt=True,
                parallel_tool_calls=False, config=config, use_workspace_override=False, segments=segments)
    return asyncio.run(invoke())


def complete(run_id: str, invocation_id: str, request: dict, *, generation: int) -> dict:
    body = bounded_request(request)
    with db()() as session:
        ctx = _locked(session, run_id)
        spec = _authority(session, ctx)
        row = _row(session, ctx, invocation_id, body)
        if row.generation != generation or spec.get("generation") != generation:
            raise BuildModelError("build_model_generation_revoked")
        # Receipt recovery may settle known usage after cancellation/expiry. It
        # never authorizes another provider call or releases an uncertain hold.
        if row.state in {"responded", "settled"}:
            _settle(session, ctx, row)
            response = _response(row)
            error_code = row.error_code
            session.commit()
            if error_code:
                raise BuildModelError(error_code)
            return response
        if row.state in {"calling", "uncertain"}:
            raise BuildModelError("build_model_outcome_uncertain" if row.state == "uncertain" else "build_model_in_progress")
        _fence(ctx, spec, generation, row.worker_id, row.attempt)
        if spec.get("builder_admission_closed_at"):
            raise BuildModelError("build_builder_admission_closed")
        if row.state != "allocated":
            raise BuildModelError("build_model_call_closed")
        unavailable_count = spec.get("unavailable_provider_tools", 0)
        if type(unavailable_count) is not int or not 0 <= unavailable_count <= MAX_UNAVAILABLE_PROVIDER_TOOLS:
            raise BuildModelError("build_provider_tool_state_invalid")
        row.state, row.updated_at = "calling", naive_utc()
        wid = ctx.run.workspace_id
        session.commit()  # Before any provider I/O. Lost responses cannot redial.
    provider_started = time.monotonic()
    try:
        response = _provider_call(wid, body)
    except BoundaryViolation as exc:
        # The boundary runs before provider I/O.  A refusal is therefore a
        # known local outcome: release the child hold immediately and report a
        # precise error instead of retaining credits as if a response was lost.
        code = "build_model_boundary_blocked"
        with db()() as session:
            ctx = _locked(session, run_id)
            row = _row(session, ctx, invocation_id, body)
            pools.release_child(session, ctx, reservation_id=row.reservation_id, no_external_call=True)
            row.state, row.error_code, row.updated_at = "released", code, naive_utc()
            pools.mark_reconciliation(session, ctx, code)
            session.commit()
        logger.warning("Build model request blocked before provider I/O", extra={
            "invocation_id": invocation_id, "boundary_classes": getattr(exc, "classes", ()),
        })
        raise BuildModelError(code) from exc
    except BuildModelError as exc:
        # Only this module's explicit pre-I/O checks prove no external request.
        with db()() as session:
            ctx = _locked(session, run_id)
            row = _row(session, ctx, invocation_id, body)
            pools.release_child(session, ctx, reservation_id=row.reservation_id, no_external_call=True)
            row.state, row.error_code, row.updated_at = "released", str(exc), naive_utc()
            pools.mark_reconciliation(session, ctx, str(exc))
            session.commit()
        raise
    except Exception as exc:
        from llm_gateway.errors import ProviderCallError
        if isinstance(exc, ProviderCallError):
            suffix = exc.code if re.fullmatch(r"[a-z_]{1,48}", exc.code or "") else "error"
            code = "build_provider_" + suffix
            logger.warning("Build provider returned no usable receipt (%s; code=%s; status=%s; elapsed_ms=%d)",
                type(exc).__name__, suffix, exc.status, round((time.monotonic() - provider_started) * 1000), extra={
                "invocation_id": invocation_id, "provider_code": suffix,
                "provider_status": exc.status, "provider_retryable": bool(exc.retryable),
            })
        else:
            code = "build_provider_outcome_uncertain"
            logger.warning("Build provider returned no usable receipt (%s; elapsed_ms=%d)",
                type(exc).__name__, round((time.monotonic() - provider_started) * 1000), extra={
                "invocation_id": invocation_id, "provider_code": type(exc).__name__,
                "provider_status": None, "provider_retryable": False,
            })
        mark_uncertain(run_id, invocation_id, code)
        raise BuildModelError(code) from exc
    try:
        usage = response.usage
        counts = {"prompt_tokens": getattr(usage, "prompt_tokens", None), "completion_tokens": getattr(usage, "completion_tokens", None)}
        if any(type(value) is not int or value < 0 for value in counts.values()):
            raise BuildModelError("build_provider_usage_missing")
    except Exception as exc:
        mark_uncertain(run_id, invocation_id, "build_provider_usage_missing")
        raise BuildModelError("build_provider_usage_missing") from exc
    error_code = None
    unavailable_tool = False
    try:
        from llm_gateway.messages import assistant_message_preserving_reasoning
        message = assistant_message_preserving_reasoning(response)
        tool_calls = message.get("tool_calls", [])
        if len(tool_calls) > 1:
            # DeepSeek can ignore ``parallel_tool_calls=False``.  The managed
            # SDK contract proves one action per provider receipt, so preserve
            # only the first requested action.  The model receives that tool
            # result on the next turn and can request the remaining work then.
            # This serializes execution without redialing or trusting the
            # sandbox to choose among parallel actions.
            logger.info("Serialized provider tool calls", extra={
                "invocation_id": invocation_id, "provider_tool_call_count": len(tool_calls),
            })
            message["tool_calls"] = tool_calls[:1]
        offered = {tool["function"]["name"] for tool in body["tools"]}
        unavailable_tool = _review_provider_tool(message, offered, invocation_id)
        if unavailable_tool:
            if unavailable_count >= MAX_UNAVAILABLE_PROVIDER_TOOLS:
                raise BuildModelError("build_unavailable_tool_limit")
            logger.warning("Builder model selected an unavailable tool; SDK will record a no-effect tool error",
                           extra={"invocation_id": invocation_id, "unavailable_tool_count": unavailable_count + 1})
        finish_reason = response.choices[0].finish_reason
        if finish_reason not in {"stop", "tool_calls"}:
            logger.warning("Build model response incomplete", extra={
                "invocation_id": invocation_id,
                "finish_reason": str(finish_reason)[:32],
                "completion_tokens": counts["completion_tokens"],
            })
            raise BuildModelError("build_model_output_incomplete")
        result = {"id": invocation_id, "object": "chat.completion", "model": "DeepSeek-V4-Pro",
                  "choices": [{"index": 0, "message": message, "finish_reason": response.choices[0].finish_reason}],
                  "usage": {**counts, "total_tokens": sum(counts.values())}}
        if len(json.dumps(result).encode()) > 1048576:
            raise BuildModelError("build_provider_response_limit")
    except Exception as exc:
        # A malformed answer is still measured provider work, not a free retry.
        error_code = str(exc) if isinstance(exc, BuildModelError) else "build_model_invalid_response"
        result = {"id": invocation_id, "error": {"code": error_code}}
    try:
        from app.connectors.secrets import encrypt_at_rest
        encrypted = encrypt_at_rest(json.dumps({"invocation_id": invocation_id, "workspace_id": wid, "response": result}))
    except Exception as exc:
        mark_uncertain(run_id, invocation_id, "build_provider_response_requires_reconciliation")
        raise BuildModelError("build_provider_response_requires_reconciliation") from exc
    with db()() as session:
        ctx = _locked(session, run_id)
        row = _row(session, ctx, invocation_id, body)
        if row.state not in {"calling", "uncertain"}:
            raise BuildModelError("build_model_receipt_conflict")
        from app.builder.budgets import priced_usage
        row.actual_micro = priced_usage(json.loads(row.pricing_json), **counts)
        row.prompt_tokens, row.completion_tokens = counts["prompt_tokens"], counts["completion_tokens"]
        row.response_ref, row.response_digest = encrypted, digest(result)
        row.error_code = error_code
        if unavailable_tool and not error_code:
            current_spec = json.loads(ctx.run.spec or "{}")
            current_spec["unavailable_provider_tools"] = unavailable_count + 1
            ctx.run.spec = json.dumps(current_spec, sort_keys=True)
        if error_code:
            pools.mark_reconciliation(session, ctx, error_code)
        row.state, row.responded_at, row.updated_at = "responded", naive_utc(), naive_utc()
        session.commit()  # Persist encrypted reply before a settlement attempt.
    with db()() as session:
        ctx = _locked(session, run_id)
        row = _row(session, ctx, invocation_id, body)
        _settle(session, ctx, row)
        session.commit()
    if error_code:
        raise BuildModelError(error_code)
    return result


def mark_uncertain(run_id, invocation_id, code="build_worker_response_lost"):
    with db()() as session:
        ctx = _locked(session, run_id)
        row = session.get(BuildModelCall, invocation_id, with_for_update=True)
        if row is None or row.build_run_id != run_id:
            raise BuildModelError("build_invocation_conflict")
        if row.state == "calling":
            row.state, row.error_code, row.updated_at = "uncertain", code, naive_utc()
            pools.mark_reconciliation(session, ctx, code)
        session.commit()


def consume(run_id, invocation_id, *, mirrored_event_seq: int, generation: int, controller_id: str, attempt: int):
    """Advance only after a trusted controller durably mirrors the SDK event."""
    with db()() as session:
        ctx = _locked(session, run_id)
        spec = _authority(session, ctx)
        _fence(ctx, spec, generation, controller_id, attempt)
        row = session.get(BuildModelCall, invocation_id, with_for_update=True)
        event = session.scalar(select(BuildEvent).where(BuildEvent.build_run_id == run_id, BuildEvent.seq == mirrored_event_seq))
        evidence = json.loads(event.payload) if event and event.kind == "agent_event" else {}
        if not row or row.build_run_id != run_id or row.state != "settled" or row.error_code or evidence.get("invocation_id") != invocation_id or evidence.get("response_digest") != row.response_digest or evidence.get("llm_response_id") != row.id:
            raise BuildModelError("build_invocation_event_not_persisted")
        if row.consumed_event_seq is not None:
            if row.consumed_event_seq != mirrored_event_seq or row.consumed_event_id != evidence.get("event_id"):
                raise BuildModelError("build_invocation_consume_conflict")
            return  # Replaying an old acknowledgment must never rewind cursor.
        reply = _response(row)
        tool_calls = reply["choices"][0]["message"].get("tool_calls", [])
        expected_type = "ActionEvent" if tool_calls else "MessageEvent"
        if evidence.get("event_type") != expected_type or not evidence.get("event_id"):
            raise BuildModelError("build_invocation_event_type_invalid")
        if tool_calls and evidence.get("tool_call_id") != tool_calls[0]["id"]:
            raise BuildModelError("build_invocation_tool_event_missing")
        # The mirror must prove ancestry to the leaf at registration. SDK
        # callbacks alone are not persistence proof; only stored events count.
        cursor = evidence.get("parent_id")
        events = session.scalars(select(BuildEvent).where(BuildEvent.build_run_id == run_id,
            BuildEvent.kind == "agent_event", BuildEvent.seq < mirrored_event_seq).order_by(BuildEvent.seq.desc()).limit(500)).all()
        parents = {item.get("event_id"): item.get("parent_id") for e in events for item in [json.loads(e.payload)] if item.get("event_id")}
        seen = set()
        while cursor != row.input_event_id:
            if cursor is None or cursor in seen or cursor not in parents:
                raise BuildModelError("build_sdk_event_ancestry_unverified")
            seen.add(cursor)
            cursor = parents[cursor]
        if row.sequence not in {spec.get("consumed_sequence", 0), spec.get("consumed_sequence", 0) + 1}:
            raise BuildModelError("build_invocation_sequence_conflict")
        row.consumed_event_seq, row.consumed_event_id = mirrored_event_seq, evidence["event_id"]
        spec["consumed_sequence"] = row.sequence
        spec["consumed_event_seq"] = mirrored_event_seq
        spec["agent_cursor"] = evidence["event_id"]
        ctx.run.spec = json.dumps(spec, sort_keys=True)
        session.commit()


def reconcile_stale_calls(now=None, age_seconds=390, limit=200):
    cutoff = naive_utc(now) - timedelta(seconds=age_seconds)
    with db()() as session:
        stale = session.execute(select(BuildModelCall.build_run_id, BuildModelCall.id).where(
            BuildModelCall.state == "calling", BuildModelCall.updated_at <= cutoff)
            .order_by(BuildModelCall.updated_at).limit(limit)).all()
    for run_id, invocation_id in stale:
        mark_uncertain(run_id, invocation_id)
    return len(stale)

"""Single-attempt provider calls with durable response and credit receipts."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time

import httpx
from core.redact import redact_string

from app.core.jobs import FatalError, RetryableError
from app.core.local_mode import is_local
from app.research import execution as ex

log = logging.getLogger(__name__)


def _failure_diagnostics(exc: Exception, elapsed_seconds: float) -> dict:
    """Persist an allowlisted failure category, never exception text or bodies."""
    from openai import APIConnectionError, APITimeoutError
    from llm_gateway.errors import ProviderCallError
    from llm_gateway.boundary import BoundaryViolation
    category, status, provider_code = "internal", None, None
    current: BaseException | None = exc
    for _ in range(4):
        if current is None:
            break
        if isinstance(current, BoundaryViolation):
            category = "boundary"
        if isinstance(current, (TimeoutError, httpx.TimeoutException, APITimeoutError)):
            category = "timeout"
        elif category != "timeout" and isinstance(current, (httpx.TransportError, APIConnectionError)):
            category = "connection"
        candidate = getattr(current, "status_code", None) or getattr(current, "status", None)
        if isinstance(current, httpx.HTTPStatusError):
            candidate = current.response.status_code
        if type(candidate) is int and 100 <= candidate <= 599:
            status = candidate
            if category == "internal":
                category = "http"
        if isinstance(current, ProviderCallError) and current.code in {
            "bad_api_key", "rate_limited", "out_of_credits", "provider_down", "provider_error",
        }:
            provider_code = current.code
        current = current.__cause__
    result = {"category": category, "elapsed_ms": max(0, min(86_400_000, round(elapsed_seconds * 1000)))}
    if status is not None:
        result["http_status"] = status
    if provider_code is not None:
        result["provider_code"] = provider_code
    if isinstance(exc, BoundaryViolation) and isinstance(exc.ledger_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", exc.ledger_id):
        result["boundary_ledger_id"] = exc.ledger_id
    return result


def schema_diagnostics(error, schema: dict) -> dict:
    """Only code-owned schema paths/codes; rejected values and extra keys stay private."""
    from pydantic import ValidationError
    if not isinstance(error, ValidationError):
        return {}
    names = set()
    def fields(node):
        if isinstance(node, dict):
            for key in ("properties", "$defs"):
                names.update(name for name in node.get(key, {}) if isinstance(name, str) and len(name) <= 80)
            for value in node.values():
                fields(value)
        elif isinstance(node, list):
            for value in node:
                fields(value)
    fields(schema)
    if "journey" in schema.get("properties", {}) and "Milestone" in schema.get("$defs", {}):
        # Named generation slots normalize into the existing Proposal model
        # before Pydantic validation. Keep its fixed paths useful, never values.
        names.update({"milestones", "kind", "included"})
    codes = {"missing", "extra_forbidden", "string_type", "string_too_short", "string_too_long",
             "string_pattern_mismatch", "int_type", "int_parsing", "float_type", "float_parsing",
             "bool_type", "list_type", "dict_type", "model_type", "literal_error", "too_short",
             "too_long", "greater_than", "greater_than_equal", "less_than", "less_than_equal",
             "finite_number", "none_required", "value_error", "union_tag_invalid", "union_tag_not_found"}
    # Inspect context only to recognize exact code-owned exception arguments.
    # Never serialize context, exception prose, rejected values or arbitrary codes.
    from core.artifact_policy import ArtifactRejected
    rules = {"action_page_invalid", "action_target_invalid", "external_target_invalid", "target_id_invalid",
             "action_dependency_invalid", "artifact_path_invalid", "artifact_path_not_normalized",
             "artifact_credential_path", "landing_expectation_coverage_invalid"}
    issues = []
    for issue in error.errors(include_input=False, include_context=True, include_url=False)[:12]:
        location = [part if type(part) is int and 0 <= part <= 1000 else
                    part if isinstance(part, str) and part in names else "<field>"
                    for part in issue.get("loc", ())[:12]]
        safe_issue = {"path": location, "code": issue["type"] if issue["type"] in codes else "schema_error"}
        cause = issue.get("ctx", {}).get("error")
        if (issue["type"] == "value_error" and type(cause) in {ValueError, ArtifactRejected}
                and len(cause.args) == 1 and type(cause.args[0]) is str and cause.args[0] in rules):
            safe_issue["rule"] = cause.args[0]
        issues.append(safe_issue)
    return {"schema_issue_count": min(error.error_count(), 10000), "schema_issues": issues}


def _output_diagnostics(content: str, choice, error: Exception | None = None, *, schema: dict | None = None) -> dict:
    """Diagnose rejected output without retaining prompts, prose or reasoning."""
    finish = getattr(choice, "finish_reason", None)
    result = {"content_chars": len(content), "sha256": hashlib.sha256(content.encode()).hexdigest(),
              "finish_reason": finish if finish in {"stop", "length", "tool_calls", "content_filter"} else "unknown"}
    if error is not None:
        from pydantic import ValidationError
        from app.idea.validation_agent import ValidationPolicyError
        if schema is not None:
            result.update(schema_diagnostics(error, schema))
        result["validation_kind"] = ("json_syntax" if isinstance(error, json.JSONDecodeError)
                                     else "schema" if isinstance(error, ValidationError) else "domain")
        if isinstance(error, ValidationPolicyError) and error.code in {
            "method_coverage", "duplicate_citation", "unknown_citation", "absent_memory_synthesis",
            "missing_citation", "missing_memory_citation", "missing_memory_gap", "absent_memory_count",
            "absent_memory_verdict", "missing_evidence_gap", "missing_proposed_test", "discovery_privacy",
        }:
            result["validation_rule"] = error.code
        research_rules = {
            "research citation is outside provided persisted sources": "unknown_citation",
            "research quote is not present in its cited source excerpt": "quote_mismatch",
            "research assessment citations must be unique": "duplicate_citation",
            "research assessment requires a quoted observation for every citation": "missing_observation",
            "research service checks must have unique source ids": "duplicate_service",
            "research observation selections must be unique": "duplicate_observation_selection",
            "research quote index is outside provided excerpts": "quote_index_out_of_range",
            "research omission cannot fit the coverage gap limit": "omission_gap_limit",
            "research provider output exceeds limit or is invalid": "output_size_or_type",
        }
        if type(error) is ValueError and str(error) in research_rules:
            result["validation_rule"] = research_rules[str(error)]
        content_rules = {"content_topic_count", "content_unknown_evidence", "content_unknown_segment",
                         "content_duplicate_angle", "content_visual_unsupported", "content_output_too_large"}
        if type(error) is ValueError and str(error) in content_rules:
            result["validation_rule"] = str(error)
        plan_rules = {
            "duplicate_milestone_key", "first_paid_user_journey_required", "landing_first_required",
            "invalid_milestone_dependencies", "included_milestone_requires_excluded_dependency",
            "duplicate_task_key", "unknown_capability", "foreign_research_source", "foreign_resource",
            "reuse_evidence_required", "landing_expectations_task_mismatch",
            "landing_expectations_required", "landing_expectations_incomplete", "landing_concept_current_claim",
            "landing_expectations_unsupported_behavior", "founder_action_capability_mismatch", "founder_handoff_required",
            "native_product_source_acceptance_invalid", "native_product_source_contract_invalid",
            "native_product_source_coverage_invalid", "native_product_source_scope_invalid",
        }
        if type(error) is ValueError and str(error) in plan_rules:
            result["validation_rule"] = str(error)
            if str(error) == "first_paid_user_journey_required":
                from app.planning.author import journey_diagnostics
                result.update(journey_diagnostics(content))
    return result


def _failure(exc) -> tuple[str, str]:
    from app.connectors.protocol import AuthExpired, ConnectorError, RateLimited
    from llm_gateway.boundary import BoundaryViolation
    from llm_gateway.errors import ProviderCallError
    from app.signals.research import FirecrawlLimiterUnavailable, _firecrawl_host
    from app.signals.local_reader import UnbilledReadError
    from app.research.errors import SearchKeyRejected
    from app.research.local_providers import BuiltinSearchUnavailable, SearchEngineLimited
    if isinstance(exc, SearchKeyRejected):
        return "failed", str(exc)
    if isinstance(exc, BuiltinSearchUnavailable):
        return "failed", "search_unavailable"
    if isinstance(exc, SearchEngineLimited):
        return "failed", "search_engine_limited"
    if isinstance(exc, UnbilledReadError):
        return "failed", "research_provider_rejected"
    if isinstance(exc, FirecrawlLimiterUnavailable):
        return "retryable", "research_provider_slot_unavailable"
    status = getattr(exc, "status", None)
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
    if isinstance(exc, BoundaryViolation):
        return "failed", "research_boundary_blocked"
    if (isinstance(exc, ProviderCallError) and exc.code == "context_length_exceeded"
            and status in {400, 422} and is_local()):
        return "failed", "research_context_length_exceeded"
    if isinstance(exc, ProviderCallError) and exc.code == "out_of_credits":
        if type(exc.status) is int and exc.status == 402:
            return "failed", "research_provider_rejected"
        return "uncertain", "research_outcome_uncertain"
    if isinstance(exc, RateLimited) or status == 429:
        return "retryable", "research_provider_rate_limited"
    if (isinstance(exc, httpx.HTTPStatusError) and status == 503
            and _firecrawl_host(exc.request.url.host) and "retry-after" in exc.response.headers):
        return "retryable", "research_provider_rate_limited"
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.extensions.get("firecrawl_self_hosted") is True:
        return "failed", "research_provider_rejected"
    if isinstance(exc, AuthExpired) or status in {400, 401, 403, 404, 422}:
        return "failed", "research_provider_rejected"
    # Transport errors may follow a charged request. Credential-only failures
    # are safe to expose as a missing integration, but never disclose its body.
    if isinstance(exc, ConnectorError) and "credential" in str(exc).lower():
        return "failed", "research_credentials_unavailable"
    if isinstance(exc, (ProviderCallError, httpx.HTTPError, TimeoutError, ConnectorError)):
        return "uncertain", "research_outcome_uncertain"
    return "uncertain", "research_outcome_uncertain"


def _record_rejected(purpose: str, rule: str, prefix: str, did: str, row, payload: dict) -> None:
    try:
        from app.analytics.error_events import record_model_rejected
        from app.analytics.feature_catalog import job_feature

        record_model_rejected(operation_kind=purpose, rule=rule, feature=job_feature(prefix + "."), operation_id=did,
                              venture_id=row.venture_id, workspace_id=row.workspace_id, job_id=payload.get("_job_id"))
    except Exception:  # noqa: BLE001 - telemetry never changes a charged, rejected answer
        pass


def perform(did: str, key: str, payload: dict, *, purpose: str, provider: str,
            callback, estimate: int, model: str | None = None, byok: bool = False,
            parse=None, journal=ex, prefix="research", require_stop: bool = False, cap_at_reservation: bool = False, provider_identity: str | None = None, schema: dict | None = None, token_ceiling: int = 0, actual_micro=None,
            model_binding: dict | None = None, request_digest: str | None = None) -> dict:
    """Callbacks return a JSON-safe response or a provider chat response.

    Network callbacks execute once. A definite rejection can receive a new job
    attempt; ambiguous calls cannot. Invalid model output is charged but never
    retained as a response. Credit settlement is replayed independently of I/O.
    """
    saved = journal.recover(did, key, payload)
    if saved is not None:
        return saved
    if purpose.startswith("research.") and ex.cancel_requested(payload):
        raise FatalError("research_cancelled")
    if actual_micro is not None and (model is not None or not callable(actual_micro)):
        raise FatalError(prefix + "_cost_reader_invalid")
    from app.credits.accounts import get_account
    from app.credits.errors import CreditError
    from app.credits.estimate import actual_llm_micro
    from app.credits.gate import captured_receipt, charge_sync, pending_model_receipt, record_model_receipt, validate_model_capture
    from llm_gateway.usage import extract_confirmed_token_usage
    model_binding = validate_model_capture(model_binding, request_digest, purpose=purpose,
        provider=provider, model=model, estimate_micro=estimate, max_tokens=token_ceiling)
    if model_binding is not None and (type(byok) is not bool or type(cap_at_reservation) is not bool):
        raise FatalError(prefix + "_payer_policy_invalid")
    row = journal.read(did)
    account = None if is_local() else get_account(row.venture_id)
    if not is_local() and estimate and not byok and (not account or (account.scope != "person" and (account.scope != "venture" or account.workspace_id != row.workspace_id or account.venture_id != row.venture_id))):
        raise FatalError(prefix + "_credits_unavailable")
    # Public GitHub search is free; the founder pays their own explicit BYOK.
    passthrough = is_local() or byok or estimate == 0
    token = journal.prepare(did, key, payload)
    request_id = f"{did}:{key}:{payload['_job_attempt']}"
    usage = {}
    try:
        with charge_sync(workspace_id=row.workspace_id, account_id=account.id if account else None,
                         purpose=purpose, estimate_micro=estimate, model=model, provider=provider,
                         request_id=request_id,
                         payer="founder" if passthrough else None, venture_id=row.venture_id, max_tokens=token_ceiling,
                         model_binding=model_binding, request_digest=request_digest) as meter:
            if not passthrough and (meter.passthrough or not meter.hold):
                raise FatalError(prefix + "_credit_reservation_unavailable")
            usage = {"provider": provider, "model": model, "source": "byok" if byok or (is_local() and estimate > 0) else "free" if estimate == 0 else "platform",
                     "workspace_id": row.workspace_id, "venture_id": row.venture_id,
                     "purpose": purpose, "request_id": request_id, "task_invocation": meter.task_invocation,
                     "account_id": account.id if account else None, "estimate_micro": estimate,
                     "reservation_id": meter.hold.reservation_id if meter.hold else None}
            meter.source = usage["source"]
            if model_binding is not None:
                usage = pending_model_receipt(meter) | {"cap_at_reservation": cap_at_reservation}
            if provider_identity is not None:
                usage["provider_identity"] = provider_identity
                if getattr(meter, "local", False):
                    meter._local_provider_identity = provider_identity
            if getattr(meter, "local", False) and model_binding is None:
                usage = pending_model_receipt(meter)
            journal.begin(did, key, payload, usage, token)
            meter.protect_outcome()
            started = time.monotonic()
            try:
                result = callback()
            except Exception as exc:
                state, code = _failure(exc)
                code = code.replace("research_", prefix + "_", 1)
                diagnostics = _failure_diagnostics(exc, time.monotonic() - started)
                if state in {"retryable", "failed"}:
                    usage["definitive_unspent"] = True
                    if getattr(meter, "local", False):
                        meter.confirm_unspent()
                        usage = pending_model_receipt(meter) | {"definitive_unspent": True}
                journal.save_operation(did, key, {"state": "release_pending" if state in {"retryable", "failed"} else state,
                    "failure_state": state, "usage": usage, "error_code": code, "failure": diagnostics})
                log.warning("model_or_provider_call_failed", extra={"operation_id": did,
                    "operation_kind": purpose, "outcome_state": state, "error_code": code, "failure": diagnostics})
                if state in {"retryable", "failed"}:
                    meter.confirm_unspent()
                if state == "retryable":
                    retry_after_s = None
                    from app.signals.research import _firecrawl_host
                    if isinstance(exc, httpx.HTTPStatusError) and _firecrawl_host(exc.request.url.host):
                        from app.signals.research import _firecrawl_retry_after
                        retry_after_s = _firecrawl_retry_after(exc.response.headers.get("Retry-After"))
                    raise RetryableError(code, retry_after_s=retry_after_s) from exc
                raise FatalError(code) from exc
            error = None
            if model_binding is not None:
                from app.credits.metered import capture_model_receipt
                usage.update(capture_model_receipt(meter, result))
                # The first checkpoint precedes parsing as well as pricing. A
                # crash cannot turn a confirmed response into permission to dial.
                journal.checkpoint_model_receipt(did, key, usage, response={}, error_code=prefix + "_invalid_output")
                meter._model_receipt_checkpointed = True
                if usage["provider_receipt"] is not None:
                    choices = getattr(result, "choices", None) or []
                    content = str(getattr(getattr(choices[0], "message", None), "content", "") or "") if choices else ""
                    try:
                        if choices and getattr(choices[0], "finish_reason", None) == "length":
                            raise ValueError("provider_output_truncated")
                        if require_stop and (len(choices) != 1 or getattr(choices[0], "finish_reason", None) != "stop"):
                            raise ValueError("provider_output_incomplete")
                        result = parse(content)
                        if result is None:
                            raise ValueError("provider_output_null")
                    except (ValueError, TypeError) as exc:
                        usage["output"] = _output_diagnostics(content, choices[0] if choices else None, exc, schema=schema)
                        result, error = {}, prefix + ("_output_truncated" if choices and getattr(choices[0], "finish_reason", None) == "length" else "_invalid_output")
                        _record_rejected(purpose, error, prefix, did, row, payload)
                    journal.checkpoint_model_receipt(did, key, usage, response=result, error_code=error)
                try:
                    usage = record_model_receipt(meter, usage)
                except Exception as exc:
                    log.warning("model_receipt_settlement_failed", extra={"exception_type": type(exc).__name__,
                        "exception_message": redact_string(str(exc))[:512], "operation_id": did,
                        "operation_key": key, "reservation_id": usage.get("reservation_id"),
                        "model_receipt_error": usage.get("model_receipt_error")})
                    raise RetryableError(prefix + "_settlement_pending") from exc
            elif model:
                counts = extract_confirmed_token_usage(getattr(result, "usage", None))
                if counts is None:
                    code = prefix + "_outcome_uncertain"
                    journal.save_operation(did, key, {"state": "uncertain", "error_code": code})
                    raise FatalError(code)
                usage.update(counts)
                meter.record(prompt_tokens=counts["prompt_tokens"], completion_tokens=counts["completion_tokens"], cached_tokens=counts["cached_tokens"])
                actual = actual_llm_micro(model, prompt_tokens=counts["prompt_tokens"], completion_tokens=counts["completion_tokens"], cached_tokens=counts["cached_tokens"])
                provider_actual = actual
                overage = cap_at_reservation and not byok and actual > estimate
                if cap_at_reservation and not byok:
                    actual = min(actual, estimate)
                    usage.update(provider_overage_micro=max(0, provider_actual - actual))
                usage.update(actual_micro=actual, provider_actual_micro=provider_actual, provenance="provider_usage")
                meter.record_micro(actual, provider_actual_micro=provider_actual)
                if getattr(meter, "local", False):
                    usage = captured_receipt(meter) | {"provenance": "provider_usage"}
                choices = getattr(result, "choices", None) or []
                content = str(getattr(getattr(choices[0], "message", None), "content", "") or "") if choices else ""
                try:
                    if choices and getattr(choices[0], "finish_reason", None) == "length":
                        raise ValueError("provider_output_truncated")
                    if overage:
                        raise ValueError("provider_usage_above_ceiling")
                    if require_stop and (len(choices) != 1 or getattr(choices[0], "finish_reason", None) != "stop"):
                        raise ValueError("provider_output_incomplete")
                    result = parse(content)
                    if result is None:
                        raise ValueError("provider_output_null")
                except (ValueError, TypeError) as exc:
                    usage["output"] = _output_diagnostics(content, choices[0] if choices else None, exc, schema=schema)
                    log.warning("model_output_rejected", extra={"operation_id": did, "operation_kind": purpose,
                        "output_diagnostics": usage["output"]})
                    result, error = {}, prefix + ("_provider_usage_above_ceiling" if overage else "_output_truncated" if choices and getattr(choices[0], "finish_reason", None) == "length" else "_invalid_output")
                    _record_rejected(purpose, error, prefix, did, row, payload)
            else:
                provider_actual = estimate
                if provider == "firecrawl":
                    from app.credits.metered import firecrawl_actual_micro
                    provider_actual = firecrawl_actual_micro(result, estimate)
                if actual_micro is not None:
                    try:
                        provider_actual = actual_micro(result)
                        if type(provider_actual) is not int or provider_actual < 0:
                            raise ValueError("unconfirmed provider cost")
                    except Exception:
                        # A successful I/O followed by unknown cost must never
                        # be released as unspent or dialed again.
                        journal.save_operation(did, key, {"state": "uncertain", "usage": usage,
                            "error_code": prefix + "_outcome_uncertain"})
                        raise FatalError(prefix + "_outcome_uncertain") from None
                overage = cap_at_reservation and not byok and provider_actual > estimate
                actual = min(provider_actual, estimate) if cap_at_reservation and not byok else provider_actual
                usage.update(actual_micro=actual, provider_actual_micro=provider_actual,
                             prompt_tokens=None, completion_tokens=None, total_tokens=None, cached_tokens=0)
                meter.record_micro(actual, provider_actual_micro=provider_actual)
                if overage:
                    result, error = {}, prefix + "_provider_usage_above_ceiling"
                elif result is None:
                    result, error = {}, prefix + "_invalid_output"
            # Save even if this lease was lost during the request: this receipt
            # proves what was paid for, but applying sources/review is fenced.
            if model_binding is None:
                if getattr(meter, "local", False):
                    usage = usage | captured_receipt(meter)
                journal.save_operation(did, key, {"state": "responded", "response": result,
                                            "usage": usage, "error_code": error})
        return journal.recover(did, key, payload)
    except (FatalError, RetryableError):
        saved = journal.operation(did, key)
        if saved.get("state") == "reserving" and saved.get("token") == token:
            journal.save_operation(did, key, {"state": "release_pending" if usage else "retryable",
                "failure_state": "retryable", "usage": usage | {"definitive_unspent": True} if usage else {}})
        if journal.operation(did, key).get("state") == "release_pending":
            journal.recover(did, key, payload)
        raise
    except CreditError as exc:
        if journal.operation(did, key).get("state") in {"provider_responded", "responded", "completed", "release_pending"}:
            return journal.recover(did, key, payload)
        if exc.code == "insufficient_credits":
            journal.save_operation(did, key, {"required_micro": exc.required_micro,
                "error_code": prefix + "_insufficient_credits"}, payload=payload)
        raise FatalError(prefix + "_" + exc.code) from exc


# Automatic re-asks after a rejected model answer (the first keeps the legacy "-repair" journal key).
REPAIR_ATTEMPTS = 2
_ISSUE_HINTS = {
    "string_too_long": "shorten this string to within its schema maxLength without losing meaning",
    "string_too_short": "this string is too short or empty",
    "missing": "this required field is missing; use the exact schema field name",
    "extra_forbidden": "remove this field; it is not in the schema (use the exact schema field names)",
    "too_long": "this list has too many items",
    "too_short": "this list has too few items",
    "literal_error": "use one of the allowed schema values",
}


def _schema_issue_note(output: dict) -> str:
    """Name the exact rejected fields from the already-sanitized diagnostics (paths and codes only).

    Production plan.compile repeats failed on visuals[].name string_too_long and on
    precedent_applications entries with a renamed field, because the generic note
    never said which field broke. Paths here hold only schema names and indices.
    """
    issues = output.get("schema_issues") if isinstance(output.get("schema_issues"), list) else []
    lines = []
    for issue in issues[:8]:
        path = issue.get("path") if isinstance(issue, dict) else None
        if not isinstance(path, list):
            continue
        where = ".".join(str(part) for part in path)
        hint = _ISSUE_HINTS.get(issue.get("code"), "fix this field to match the schema")
        lines.append(f"- {where}: {hint}")
    return ("Rejected fields:\n" + "\n".join(lines) + "\n") if lines else ""

# Default interactive deadline; clarify and plan use their own provider deadlines.
INTERACTIVE_TIMEOUT_SECONDS = int(os.getenv("LLM_INTERACTIVE_TIMEOUT_SECONDS", "90"))


def _timed_out(exc: BaseException) -> bool:
    from llm_gateway.errors import ProviderCallError
    return isinstance(exc, TimeoutError) or (
        isinstance(exc, ProviderCallError) and exc.status is None and exc.retryable)


async def _timed_call(transmit, kwargs: dict, seconds: int | None, attempt: str):
    started = time.monotonic()
    outcome = "ok"
    try:
        if seconds is None:
            return await transmit(**kwargs)
        return await asyncio.wait_for(transmit(**kwargs), timeout=seconds)
    except BaseException as exc:
        outcome = "timeout" if _timed_out(exc) else "error"
        raise
    finally:
        log.info("model_call_timing", extra={"operation_kind": kwargs.get("purpose"), "attempt": attempt,
            "outcome": outcome, "duration_ms": round((time.monotonic() - started) * 1000)})


async def _transmit_with_deadline(transmit, kwargs: dict, seconds: int | None, fallback: dict | None = None):
    """One dial; timeout/connection uncertainty cannot justify another paid call.

    The legacy fallback argument remains accepted but never dispatches.
    """
    from llm_gateway.provider_foundry import loopback_provider

    config = kwargs.get("config")
    if seconds is not None and config is not None and loopback_provider(config):
        seconds = max(seconds, config.timeout_seconds + 2)
    return await _timed_call(transmit, kwargs, seconds, "primary")


def perform_llm(did, key, payload, *, dial, provider_timeout_seconds=None, **kwargs):
    from copy import deepcopy
    from llm_gateway import sole
    from llm_gateway.provider_foundry import deployment_for, endpoint_host, normalize_base_url
    from llm_gateway.routing import wire_api_mode
    binding = kwargs.get("model_binding")
    if binding is not None:
        dial = deepcopy(dial)
        config = dial.get("config")
        if (config is None or config.routing_purpose != kwargs["purpose"]
                or dial.get("purpose") != config.routing_purpose
                or dial.get("tier", config.captured_tier) != config.captured_tier
                or dial.get("single_attempt") is not True):
            raise ValueError("Research dial requires its captured route")
        api_mode = wire_api_mode(config)
        route = {"purpose": config.routing_purpose, "provider": config.provider,
            "deployment": deployment_for(config, config.captured_tier), "catalog_model": config.catalog_model,
            "api_mode": api_mode, "endpoint_host": endpoint_host(normalize_base_url(
                config.provider, config.base_url, api_mode=api_mode)),
            "max_output_tokens": dial.get("max_tokens")}
        if any(type(value) is not type(binding.get(key)) or value != binding[key] for key, value in route.items()):
            raise ValueError("Research dial differs from captured model binding")
        dial.update(config=config.model_copy(deep=True, update={"max_output_tokens": binding["max_output_tokens"]}),
            use_workspace_override=False, tier=config.captured_tier)
    return perform(did, key, payload, **kwargs,
        callback=lambda: asyncio.run(_transmit_with_deadline(sole.transmit, dial, provider_timeout_seconds)))


def synthesis_capture(config, messages, schema, *, purpose, max_tokens, request_id,
                      provider_identity, source_map=None):
    """Original route and bounded complete wire quote; no provider or money I/O."""
    import hashlib
    import json
    from urllib.parse import urlsplit
    from app.credits.provider_receipts import capture_pricing_snapshot, estimate_model_binding_micro
    from llm_gateway import sole
    from llm_gateway.provider_foundry import deployment_for, normalize_base_url
    from llm_gateway.routing import wire_api_mode

    if type(max_tokens) is not int or not 1 <= max_tokens <= 16384:
        raise ValueError("Research output exceeds its captured allowance")
    if type(request_id) is not str or not request_id or request_id.strip() != request_id:
        raise ValueError("Missing original research admission")
    if config.source != "founder_override":
        sole.require_env_api_key(config)
    # The SDK inserts its trusted clock after screening. Capture the reviewed
    # bounded clock policy rather than sampling or freezing an earlier time.
    config = config.model_copy(deep=True, update={"max_output_tokens": max_tokens})
    api_mode = wire_api_mode(config)
    prompt_tokens = sole.transport_prompt_tokens(messages, purpose=purpose, config=config,
        response_schema=schema, clock_text=" " * 256)
    binding = {"schema_version": 1, "purpose": purpose, "provider": config.provider,
        "deployment": deployment_for(config, config.captured_tier), "catalog_model": config.catalog_model,
        "api_mode": api_mode, "endpoint_host": urlsplit(normalize_base_url(config.provider,
            config.base_url, api_mode=api_mode)).hostname,
        "input_token_limit": prompt_tokens, "max_output_tokens": max_tokens,
        "requested_service_tier": "default", "pricing_snapshot": capture_pricing_snapshot(config.catalog_model)}
    estimate = estimate_model_binding_micro(binding)
    request = {"request_id": request_id, "binding": binding, "messages": messages, "response_schema": schema,
        "tools": None, "captured_tier": config.captured_tier, "reasoning_effort": config.reasoning_effort,
        "endpoint": normalize_base_url(config.provider, config.base_url, api_mode=api_mode),
        "timeout_seconds": config.timeout_seconds, "source_map": source_map,
        "provider_identity": provider_identity, "clock_policy": "server_clock_256_bytes",
        "repair_policy": {"max_calls": 1, "repair_calls": 0}}
    request_digest = hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False,
        allow_nan=False, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {"model_binding": binding, "request_digest": request_digest, "estimate": estimate,
        "token_ceiling": prompt_tokens + max_tokens}


def synthesize(did: str, payload: dict, *, messages: list, schema: dict, parse,
               purpose: str = "research.deep", key: str = "review", journal=ex, prefix="research", max_tokens: int = 4096, require_stop: bool = False, cap_at_reservation: bool = False, pin_provider: bool = False, provider_timeout_seconds: int | None = None, repair: bool = False, source_map: dict | None = None, prepare_loopback=None) -> dict:
    from app.credits.estimate import count_prompt_tokens, estimate_llm_micro
    from app.llm import founder_provider
    from llm_gateway import sole
    # Recover before checking current provider config: a saved valid answer does
    # not require the key/provider to still be reachable to settle and apply it.
    saved = journal.recover(did, key, payload)
    if saved is not None:
        return saved
    modern = purpose.startswith(("research.", "gtm.discovery.")) or purpose == "idea.clarify_turn"
    if provider_timeout_seconds is not None and (
        not pin_provider or type(provider_timeout_seconds) is not int or not 1 <= provider_timeout_seconds <= 300
    ):
        raise FatalError(prefix + "_invalid_provider_deadline")
    if not is_local() and os.environ.get("LLM_OFFLINE", "0").lower() in {"1", "true", "yes"}:
        raise FatalError(prefix + "_provider_offline")
    row = journal.read(did)
    if modern and (type(row.workspace_id) is not str or not row.workspace_id.strip()):
        raise FatalError(prefix + "_missing_workspace")
    founder_provider.register_workspace_llm_resolver()
    try:
        resolved = founder_provider.resolve_llm_config(row.workspace_id)
    except founder_provider.FounderLLMConfigError:
        if not is_local():
            raise
        raise FatalError(prefix + "_byok_unavailable") from None
    byok = resolved.source == "founder_override"
    if modern and resolved.workspace_id != row.workspace_id:
        raise FatalError(prefix + "_resources_changed")
    config = (founder_provider.to_provider_config(row.workspace_id, resolved_config=resolved) if pin_provider or modern else
              founder_provider.to_provider_config(row.workspace_id)) if byok else None
    if (pin_provider or modern) and not byok:
        from llm_gateway.provider_foundry import load_provider_config_from_env
        config = load_provider_config_from_env().model_copy(deep=True)
    if pin_provider and config is not None and (config.provider, config.deployment_pro, config.base_url.rstrip("/")) != (
        resolved.provider, resolved.model, (resolved.base_url or "").rstrip("/")):
        raise FatalError(prefix + "_resources_changed")
    if byok and config is None:
        raise FatalError(prefix + "_byok_unavailable")
    if provider_timeout_seconds is not None and not founder_provider.keyless_local_config(config):
        config = config.model_copy(update={"timeout_seconds": provider_timeout_seconds}, deep=True)
    from llm_gateway.routing import sol_output_cap
    if modern:
        from copy import deepcopy
        from llm_gateway.routing import resolve_purpose_config
        config = resolve_purpose_config(purpose, config)
        tokens = sol_output_cap(config, max_tokens)
        if prepare_loopback is not None and founder_provider.keyless_local_config(config):
            tokens = min(tokens, config.context_window // 4)
            messages, parse, source_map = prepare_loopback(config.context_window, tokens)
        messages, schema, source_map = deepcopy(messages), deepcopy(schema), deepcopy(source_map)
        identity = founder_provider.resolved_provider_identity(resolved)
        if is_local():
            from llm_gateway.provider_foundry import deployment_for
            model = deployment_for(config, config.captured_tier)
        else:
            captured = synthesis_capture(config, messages, schema, purpose=purpose, max_tokens=tokens,
                request_id=f"{did}:{key}:{payload['_job_attempt']}", provider_identity=identity, source_map=source_map)
    else:
        captured = sole.resolve_purpose_config(purpose,
            config or sole.load_provider_config_from_env(), tier="pro")
        tokens = sol_output_cap(captured, max_tokens)
        if tokens != max_tokens:
            config = captured
        model = captured.deployment_pro if tokens != max_tokens else resolved.model
    free_local = is_local() and (config.provider == "local" or founder_provider.keyless_local_config(config))
    if free_local:
        model = "local"
    dial = dict(messages=messages, purpose=purpose, workspace_id=row.workspace_id,
                tier=config.captured_tier if modern else "pro", max_tokens=tokens, config=config, response_schema=schema,
                single_attempt=True, trace_id=f"{did}:{key}",
                **({"use_workspace_override": False} if pin_provider or modern or tokens != max_tokens else {}))
    def attempt(call_key, call_dial):
        if modern and not is_local():
            return perform_llm(did, call_key, payload, purpose=purpose, provider=config.provider,
                model=captured["model_binding"]["deployment"], **captured,
                byok=byok, parse=parse, journal=journal, prefix=prefix, require_stop=require_stop,
                cap_at_reservation=cap_at_reservation, provider_identity=identity, schema=schema,
                dial=call_dial, provider_timeout_seconds=provider_timeout_seconds)
        prompt_tokens = (sole.transport_prompt_tokens(call_dial["messages"], purpose=purpose, config=config,
            response_schema=call_dial["response_schema"], clock_text=" " * 256) if modern else count_prompt_tokens(call_dial["messages"]))
        return perform_llm(did, call_key, payload, purpose=purpose, provider=resolved.provider,
            model=model, estimate=0 if free_local else estimate_llm_micro(model, prompt_tokens=prompt_tokens, max_tokens=call_dial["max_tokens"]),
            byok=byok, parse=parse, journal=journal, prefix=prefix, require_stop=require_stop, cap_at_reservation=cap_at_reservation,
            provider_identity=founder_provider.resolved_provider_identity(resolved) if pin_provider or modern else None, schema=schema,
            token_ceiling=prompt_tokens + call_dial["max_tokens"],
            dial=call_dial, provider_timeout_seconds=provider_timeout_seconds)
    try:
        return attempt(key, dial)
    except FatalError as exc:
        invalid = str(exc)
        # Modern known-paid failures are terminal. The old repair flag does
        # not approve an aggregate paid allowance for a captured request.
        if modern or not repair or invalid not in {prefix + "_invalid_output", prefix + "_output_truncated"}:
            raise
        # Up to REPAIR_ATTEMPTS automatic re-asks with the validator's rule fed back: a rejected
        # answer is a bounded re-ask, not a founder-visible failure. Each has its own receipt and
        # reservation; if all fail the original code is reported.
        rejected = {prefix + "_invalid_output", prefix + "_output_truncated"}
        failed_key, last = key, exc
        for n in range(1, (1 if purpose == "plan.compile" else REPAIR_ATTEMPTS) + 1):
            failed = journal.operation(did, failed_key)
            if failed.get("state") != "completed" or failed.get("usage", {}).get("settled") is not True:
                raise last
            output = failed.get("usage", {}).get("output", {})
            rule = output.get("validation_rule")
            truncated = str(last).endswith("_output_truncated")
            note = ("Your previous answer was rejected by the validator" + (f" (rule: {rule})" if rule else "")
                    + (f" ({output['validation_kind']} error)" if not rule and output.get("validation_kind") else "") + ". "
                    + ("It was cut off; answer again more concisely. " if truncated else "")
                    + ("Keep every string within its schema maxLength. " if output.get("validation_kind") == "schema" else "")
                    + _schema_issue_note(output)
                    + ("Use consenting adult interviews and anonymized verbal workflow descriptions only. "
                       "Do not request real private conversations, screenshots, child records or pickup observation. "
                       if rule == "discovery_privacy" else "")
                    + ("Return complete, untruncated journey_json following its field description: "
                       "root schema and steps; every step has name, actor, method, path and integer status. "
                       "actor is 0, 1 or null. Use 2-60 steps, no extra keys, and criteria naming existing steps. "
                       if rule == "native_product_source_contract_invalid" else "")
                    + "Answer again with the complete JSON only, following the schema exactly and citing only ids from the supplied lists.")
            repair_key = key + ("-repair" if n == 1 else f"-repair{n}")
            try:
                return attempt(repair_key, {**dial, "messages": [{**messages[0], "content": messages[0]["content"] + "\n\n" + note}, *messages[1:]],
                                            "max_tokens": min(16384, tokens * 3 // 2) if truncated else tokens})
            except FatalError as repair_exc:
                if str(repair_exc).endswith("_operation_in_progress"):
                    raise
                if str(repair_exc) not in rejected:
                    raise
                failed_key, last = repair_key, repair_exc
        raise exc from last

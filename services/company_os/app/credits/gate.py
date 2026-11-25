"""Charge facade — callers never touch reserve/settle directly (spec §5.1)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from app.core.local_mode import is_local
from app.credits.accounts import account_for_workspace, get_account_by_id
from app.credits.errors import CreditsUnavailable
from app.credits.estimate import actual_llm_micro
from app.credits.ledger import append, find_by_idempotency_key
from app.credits.policy import enforcement_enabled
from app.credits.reserve import Hold, SettleResult, release, reserve, settle
from app.db.models import CreditAccount, CreditReservation

log = logging.getLogger("app.credits.gate")
_call_allowance = ContextVar("credit_call_allowance", default=None)


@contextmanager
def bounded_calls(workspace_id: str, venture_id: str, admit):
    token = _call_allowance.set((workspace_id, venture_id, admit))
    try:
        yield
    finally:
        _call_allowance.reset(token)


def _task_usage(method: str, *args, **kwargs):
    """S03 owns run context/counters; older wallet-aware deployments have no tasks module."""
    try:
        from app.tasks import usage
    except ModuleNotFoundError as exc:
        if exc.name in {"app.tasks", "app.tasks.usage"}:
            if method != "before_call" and args and args[0]:
                raise CreditsUnavailable("task usage needs reconciliation") from exc
            return None
        raise
    return getattr(usage, method)(*args, **kwargs)


def _fail_closed() -> bool:
    return (os.environ.get("LLM_CREDITS_REQUIRE_GATE") or "1").strip() != "0"


def resolve_charge_target(workspace_id: str) -> CreditAccount | None:
    return account_for_workspace(workspace_id)


@dataclass
class Meter:
    """Usage sink yielded by ``charge`` / ``charge_sync``."""

    hold: Hold | None
    passthrough: bool
    purpose: str
    # Identifies this metering scope when the caller supplied no request id.
    # Must not be derived from id(): CPython reuses addresses after collection,
    # so two unrelated meters could form the same idempotency key and the second
    # usage row would be silently discarded as a duplicate.
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    model: str | None = None
    provider: str | None = None
    workspace_id: str | None = None
    account_id: str | None = None
    venture_id: str | None = None
    request_id: str | None = None
    payer: str | None = None
    recorded: bool = False
    outcome_protected: bool = False
    definitive_failure: bool = False
    actual_micro: int | None = None
    provider_actual_micro: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cached_tokens: int = 0
    settle_result: SettleResult | None = field(default=None, repr=False)
    task_invocation: str | None = None
    task_usage_failed: bool = False
    estimate_micro: int = 0
    source: str | None = None
    model_binding: dict | None = None
    request_digest: str | None = None
    token_ceiling: int = 0
    provider_receipt: dict | None = None
    billing_output_error: bool = False
    reasoning_tokens: int = 0
    cap_at_reservation: bool = False
    _admitted_model_call: bool = False
    _model_receipt_checkpointed: bool = False
    model_journal_key: str | None = None
    provider_output_error: str | None = None
    actor_id: str | None = None
    bounded: bool = False
    allowance_settle: object = field(default=None, repr=False)
    local: bool = False
    _local_provider_identity: str | None = field(default=None, repr=False)

    def record(self, *, prompt_tokens: int, completion_tokens: int, cached_tokens: int = 0) -> None:
        if (any(type(n) is not int or n < 0 for n in (prompt_tokens, completion_tokens, cached_tokens))
                or cached_tokens > prompt_tokens):
            raise ValueError("confirmed token counts must be non-negative ints")
        self.recorded = True
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.cached_tokens = cached_tokens

    def record_micro(self, n: int, *, provider_actual_micro: int | None = None) -> None:
        if type(n) is not int or n < 0:
            raise ValueError("actual micro-credits must be a non-negative int")
        if provider_actual_micro is not None:
            if type(provider_actual_micro) is not int or provider_actual_micro < n:
                raise ValueError("provider actual must be an int at least wallet actual")
            self.provider_actual_micro = provider_actual_micro
        if self.bounded:
            self.provider_actual_micro = max(n, self.provider_actual_micro or 0)
            n = min(n, self.estimate_micro)
        self.recorded = True
        self.actual_micro = n

    def protect_outcome(self) -> None:
        """Persist protection before a journaled provider call can leave Core."""
        if self.local:
            from app.credits import local_meter
            local_meter.protect_outcome(self)
        if self.hold is not None and not self.passthrough:
            from app.credits.reserve import protect_outcome
            protect_outcome(self.hold)
        self.outcome_protected = True

    def confirm_unspent(self) -> None:
        """The provider definitively rejected the operation before doing work."""
        self.definitive_failure = True


def _enter(
    *,
    workspace_id: str | None,
    account_id: str | None,
    purpose: str,
    model: str | None,
    provider: str | None,
    estimate_micro: int,
    request_id: str | None,
    payer: str | None,
    venture_id: str | None = None,
    max_tokens: int = 0,
    model_binding: dict | None = None,
    request_digest: str | None = None,
    actor_id: str | None = None,
) -> Meter:
    model_binding = validate_model_capture(model_binding, request_digest, purpose=purpose,
        provider=provider, model=model, estimate_micro=estimate_micro, max_tokens=max_tokens)
    if model_binding is not None and (type(request_id) is not str or not request_id or request_id.strip() != request_id):
        raise CreditsUnavailable("original model request identity is unavailable")
    from app.tenancy import current_or_none
    scope = current_or_none()
    venture_id = venture_id or (scope.venture_id if scope and scope.workspace_id == workspace_id else None)
    if actor_id is not None and (model_binding is None or workspace_id is not None or venture_id is not None):
        raise CreditsUnavailable("original model actor scope is unavailable")
    if is_local():
        if actor_id is not None:
            raise CreditsUnavailable("You need to use your local workspace for this request. "
                "Open your local workspace to continue.")
        if scope is not None and (workspace_id is not None and scope.workspace_id != workspace_id
                or venture_id is not None and scope.venture_id and scope.venture_id != venture_id):
            raise CreditsUnavailable("You cannot run this request in a different workspace. "
                "Open the workspace for your task before trying again.")
        from app.credits import local_meter
        allowance = _call_allowance.get()
        if allowance is not None and allowance[:2] != (workspace_id, venture_id):
            raise CreditsUnavailable("You cannot run this request for a different task. "
                "Open your task's workspace before trying again.")
        allowance_settle = None
        if allowance is not None:
            request_id = request_id or uuid.uuid4().hex
            allowance_settle = allowance[2](None, request_id, estimate_micro)
        try:
            task_invocation = _task_usage("before_call", estimate_micro=estimate_micro, max_tokens=max_tokens)
            if task_invocation is not None and (not isinstance(task_invocation, str) or not task_invocation):
                raise CreditsUnavailable("You cannot confirm this task right now. "
                    "Open your task list before trying again.")
        except Exception:
            if callable(allowance_settle):
                allowance_settle(None)
            raise
        meter = Meter(hold=None, passthrough=True, purpose=purpose, model=model, provider=provider,
            workspace_id=workspace_id, venture_id=venture_id, request_id=request_id, payer="founder",
            task_invocation=task_invocation, estimate_micro=estimate_micro, model_binding=model_binding,
            request_digest=request_digest, token_ceiling=max_tokens, _admitted_model_call=model_binding is not None,
            local=True, allowance_settle=allowance_settle if callable(allowance_settle) else None)
        try:
            local_meter.begin(meter)
        except Exception:
            try:
                if task_invocation:
                    _task_usage("abort_call", task_invocation, definitive=True)
            finally:
                if callable(allowance_settle):
                    allowance_settle(None)
            raise
        return meter
    passthrough = (
        (payer or "").strip().lower() == "founder" or not enforcement_enabled()
    )
    account: CreditAccount | None = None
    if account_id:
        account = get_account_by_id(account_id)
    elif workspace_id:
        account = resolve_charge_target(workspace_id)

    if actor_id is not None:
        from app.credits._txn import serialize_writes, session_factory
        with session_factory()() as session:
            if account is None:
                raise CreditsUnavailable("original model actor account is unavailable")
            serialize_writes(session, key=account.id)
            _check_model_actor(session, actor_id, session.get(CreditAccount, account.id), admission=True)

    if not passthrough:
        if account is None:
            if _fail_closed():
                raise CreditsUnavailable(
                    "credit metering is not available; refusing platform call"
                )
            passthrough = True
        elif estimate_micro < 1:
            passthrough = True

    hold: Hold | None = None
    if model_binding is not None:
        from app.credits.metered import assert_model_call_not_started
        assert_model_call_not_started(workspace_id=workspace_id, venture_id=venture_id,
                                      purpose=purpose, request_id=request_id, actor_id=actor_id)
    allowance = _call_allowance.get()
    allowance_settle = None
    if allowance is not None:
        if allowance[:2] != (workspace_id, venture_id) or account is None or passthrough and payer != "founder":
            raise CreditsUnavailable("credit allowance scope mismatch")
        request_id = request_id or uuid.uuid4().hex
        allowance_settle = allowance[2](account.id, request_id, estimate_micro)
    try:
        task_invocation = _task_usage("before_call", estimate_micro=estimate_micro, max_tokens=max_tokens)
        if task_invocation is not None and (not isinstance(task_invocation, str) or not task_invocation):
            raise CreditsUnavailable("task invocation is invalid")
    except Exception:
        if callable(allowance_settle):
            allowance_settle(None)
        raise
    if not passthrough and account is not None:
        try:
            hold = reserve(
                account_id=account.id,
                amount_micro=estimate_micro,
                purpose=purpose,
                model=model,
                provider=provider,
                workspace_id=workspace_id,
                request_id=request_id,
                venture_id=venture_id,
                task_invocation=task_invocation,
            )
        except Exception:
            if task_invocation:
                _task_usage("abort_call", task_invocation, definitive=True)
            if callable(allowance_settle):
                allowance_settle(None)
            raise


    return Meter(
        hold=hold,
        passthrough=passthrough or hold is None,
        purpose=purpose,
        model=model,
        provider=provider,
        workspace_id=workspace_id,
        account_id=account.id if account is not None else account_id,
        request_id=request_id,
        payer=payer,
        venture_id=venture_id,
        task_invocation=task_invocation,
        estimate_micro=estimate_micro,
        model_binding=model_binding,
        request_digest=request_digest,
        token_ceiling=max_tokens,
        _admitted_model_call=model_binding is not None,
        actor_id=actor_id,
        bounded=allowance is not None and not passthrough,
        allowance_settle=allowance_settle if callable(allowance_settle) else None,
    )


def _check_model_actor(session, actor_id, account, *, admission: bool) -> None:
    """New person-scope admission; funded cleanup keeps the saved original owner."""
    if (type(actor_id) is not str or not actor_id or actor_id.strip() != actor_id
            or account is None or account.owner_person_id != actor_id or account.workspace_id is not None):
        raise CreditsUnavailable("original model actor account is unavailable")
    if not admission:
        return
    from sqlalchemy import select
    from app.authz.workspace import subject_person_id
    from app.consumer.users import consumer_venture_id, plan_active
    from app.db.models import ConsumerUser, Person
    from app.tenancy import current_or_none
    scope = current_or_none()
    user = session.scalar(select(ConsumerUser).where(
        ConsumerUser.person_id == actor_id, ConsumerUser.credit_account_id == account.id))
    if (scope is None or scope.actor in {"anonymous", "system", "local", "founder"}
            or actor_id == subject_person_id(None) or subject_person_id(scope.actor) != actor_id
            or scope.workspace_id not in {None, "none"}
            or scope.venture_id not in {None, ""} or session.get(Person, actor_id) is None
            or user is None or not plan_active(user) or account.status != "active"
            or type(user.cognito_sub) is not str or not user.cognito_sub.strip()
            or not (account.scope == "person" and account.venture_id is None
                or account.scope == "venture" and account.venture_id == consumer_venture_id(user.cognito_sub))):
        raise CreditsUnavailable("original model actor account is unavailable")


def _primitive_copy(value):
    """Detach validated primitives, never SDK copy/coercion methods."""
    def inspect(node, depth=0):
        if depth > 12:
            raise CreditsUnavailable("provider receipt is invalid")
        if type(node) is dict:
            if any(type(key) is not str for key in node):
                raise CreditsUnavailable("provider receipt is invalid")
            for child in node.values():
                inspect(child, depth + 1)
        elif type(node) is list:
            for child in node:
                inspect(child, depth + 1)
        elif node is not None and type(node) not in {str, int, bool}:
            raise CreditsUnavailable("provider receipt is invalid")
    inspect(value)
    text = json.dumps(value, allow_nan=False)
    if len(text) > 262144:
        raise CreditsUnavailable("provider receipt is invalid")
    return json.loads(text)


def validate_model_capture(model_binding, request_digest, *, purpose, provider, model,
                           estimate_micro, max_tokens):
    """Optional frozen quote; the omitted pair keeps the accepted legacy path."""
    if model_binding is None and request_digest is None:
        return None
    from app.credits.provider_receipts import estimate_model_binding_micro, validate_model_binding
    binding = validate_model_binding(_primitive_copy(model_binding))
    if (type(request_digest) is not str or re.fullmatch(r"[a-f0-9]{64}", request_digest) is None
            or binding["purpose"] != purpose or binding["provider"] != provider
            or binding["deployment"] != model
            or type(max_tokens) is not int
            or max_tokens != binding["input_token_limit"] + binding["max_output_tokens"]
            or type(estimate_micro) is not int or estimate_micro < estimate_model_binding_micro(binding)):
        raise CreditsUnavailable("provider receipt binding mismatch")
    return binding


def pending_model_receipt(meter: Meter) -> dict:
    """Original custody before pricing; carries no invented cost or counts."""
    source = meter.source or (("free" if meter.estimate_micro == 0 else "byok") if meter.passthrough else "platform")
    usage = {"source": source, "reservation_id": meter.hold.reservation_id if meter.hold else None,
        "account_id": meter.account_id, "estimate_micro": meter.estimate_micro,
        "workspace_id": meter.workspace_id, "venture_id": meter.venture_id, "purpose": meter.purpose,
        "model": meter.model, "provider": meter.provider,
        "request_id": meter.request_id if meter.hold else meter.request_id or meter.trace_id,
        "task_invocation": meter.task_invocation, "payer": meter.payer,
        "model_binding": _primitive_copy(meter.model_binding), "request_digest": meter.request_digest,
        "token_ceiling": meter.token_ceiling}
    if meter.local:
        usage["local_call_id"] = meter.trace_id
        if meter._local_provider_identity is not None:
            usage["provider_identity"] = meter._local_provider_identity
    if meter.actor_id is not None:
        usage["actor_id"] = meter.actor_id
    return usage


def project_model_receipt(usage: dict) -> dict:
    """Price only the saved original snapshot, independently of live config."""
    from app.credits.provider_receipts import validate_confirmed_provider_receipt
    validate_model_capture(usage.get("model_binding"), usage.get("request_digest"),
        **{name: usage.get(name) for name in ("purpose", "provider", "model", "estimate_micro")},
        max_tokens=usage.get("token_ceiling"))
    if usage.get("model_binding") is None:
        raise CreditsUnavailable("provider receipt binding is unavailable")
    evidence = validate_confirmed_provider_receipt(_primitive_copy(usage.get("provider_receipt")),
        model_binding=usage["model_binding"], expected_request_digest=usage["request_digest"])
    raw = evidence["provider_actual_micro"]
    output_error = usage.get("provider_output_error")
    if output_error is not None and (type(output_error) is not str or output_error != "invalid_output"):
        raise CreditsUnavailable("provider output receipt is invalid")
    cap = usage.get("cap_at_reservation", False)
    if type(cap) is not bool:
        raise CreditsUnavailable("provider receipt cap is invalid")
    requested = min(raw, usage["estimate_micro"]) if cap and usage["source"] == "platform" else raw
    return {**usage, "actual_micro": requested, "provider_actual_micro": raw,
        "prompt_tokens": evidence["input_tokens"], "completion_tokens": evidence["output_tokens"],
        "total_tokens": evidence["total_tokens"], "cached_tokens": evidence["cached_tokens"],
        "reasoning_tokens": evidence["reasoning_tokens"], "provenance": "captured_catalog_usage",
        "billing_output_error": output_error is not None or not evidence["output_usable"] or raw > usage["estimate_micro"]}


def record_model_receipt(meter: Meter, usage: dict) -> dict:
    if not meter._model_receipt_checkpointed:
        raise CreditsUnavailable("provider receipt checkpoint is unavailable")
    original = pending_model_receipt(meter)
    if any(name not in usage or usage[name] != value for name, value in original.items()):
        raise CreditsUnavailable("provider receipt binding mismatch")
    priced = project_model_receipt(usage | ({"cap_at_reservation": True} if meter.bounded else {}))
    meter.provider_receipt = _primitive_copy(priced["provider_receipt"])
    meter.billing_output_error = priced["billing_output_error"]
    meter.reasoning_tokens = priced["reasoning_tokens"]
    meter.cap_at_reservation = priced.get("cap_at_reservation", False)
    meter.provider_output_error = priced.get("provider_output_error")
    meter.record(prompt_tokens=priced["prompt_tokens"], completion_tokens=priced["completion_tokens"],
                 cached_tokens=priced["cached_tokens"])
    meter.record_micro(priced["actual_micro"], provider_actual_micro=priced["provider_actual_micro"])
    return priced


def _check_model_projection(usage: dict) -> None:
    projected = project_model_receipt(usage)
    if any(name not in usage or usage[name] != projected[name] or type(usage[name]) is not type(projected[name])
           for name in ("actual_micro", "provider_actual_micro", "prompt_tokens", "completion_tokens",
                        "total_tokens", "cached_tokens", "reasoning_tokens", "billing_output_error")):
        raise CreditsUnavailable("provider receipt projection mismatch")


_BINDING = {
    "account_id": "account_id", "estimate_micro": "amount_micro", "workspace_id": "workspace_id",
    "venture_id": "venture_id", "purpose": "purpose", "model": "model", "provider": "provider",
    "request_id": "request_id", "task_invocation": "task_invocation",
}


def _counts(usage: dict, *, required: bool) -> tuple[int | None, int | None, int, int]:
    prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
    cached = usage.get("cached_tokens", 0)
    if prompt is None and completion is None and not required:
        if type(cached) is not int or cached != 0 or usage.get("total_tokens") is not None:
            raise CreditsUnavailable("provider usage needs reconciliation")
        return None, None, 0, 0
    total = usage.get("total_tokens")
    if (any(type(n) is not int or n < 0 for n in (prompt, completion, cached, total))
            or total != prompt + completion or cached > prompt
            or (required and "cached_tokens" not in usage)):
        raise CreditsUnavailable("provider usage needs reconciliation")
    return prompt, completion, cached, total


def _costs(usage: dict) -> tuple[int, int]:
    requested = usage.get("actual_micro")
    raw = usage.get("provider_actual_micro", requested)
    if type(requested) is not int or requested < 0 or type(raw) is not int or raw < requested:
        raise CreditsUnavailable("provider cost needs reconciliation")
    return requested, raw


def _meter_usage(meter: Meter) -> dict:
    requested = _actual_for_settle(meter)
    provider_actual = meter.provider_actual_micro if meter.provider_actual_micro is not None else requested
    if meter.bounded:
        requested = min(requested, meter.estimate_micro)
    source = meter.source or (("free" if meter.estimate_micro == 0 else "byok") if meter.passthrough else "platform")
    if source not in ({"byok", "free"} if meter.passthrough else {"platform"}):
        raise CreditsUnavailable("receipt payer is unavailable")
    usage = {"source": source,
        "reservation_id": meter.hold.reservation_id if meter.hold else None,
        "account_id": meter.account_id, "estimate_micro": meter.estimate_micro,
        "workspace_id": meter.workspace_id, "venture_id": meter.venture_id, "purpose": meter.purpose,
        "model": meter.model, "provider": meter.provider, "request_id": meter.request_id if meter.hold else meter.request_id or meter.trace_id,
        "task_invocation": meter.task_invocation, "payer": meter.payer,
        "actual_micro": requested, "provider_actual_micro": provider_actual,
        "prompt_tokens": meter.prompt_tokens, "completion_tokens": meter.completion_tokens,
        "cached_tokens": meter.cached_tokens,
        "total_tokens": (meter.prompt_tokens + meter.completion_tokens) if meter.prompt_tokens is not None and meter.completion_tokens is not None else None}
    if meter.local:
        usage["local_call_id"] = meter.trace_id
        if meter.model_binding is None:
            usage.update(model_binding=None, request_digest=meter.request_digest, token_ceiling=meter.token_ceiling)
        if meter._local_provider_identity is not None:
            usage["provider_identity"] = meter._local_provider_identity
    if meter.model_binding is not None:
        usage.update(model_binding=_primitive_copy(meter.model_binding), request_digest=meter.request_digest,
            token_ceiling=meter.token_ceiling, provider_receipt=_primitive_copy(meter.provider_receipt),
            billing_output_error=meter.billing_output_error, reasoning_tokens=meter.reasoning_tokens,
            cap_at_reservation=meter.cap_at_reservation or meter.bounded, provider_output_error=meter.provider_output_error)
        if meter.actor_id is not None:
            usage["actor_id"] = meter.actor_id
    return usage


def captured_receipt(meter: Meter) -> dict:
    """Snapshot confirmed usage for a server journal before the gate exits."""
    usage = _meter_usage(meter)
    _counts(usage, required=bool(meter.model))
    _costs(usage)
    return usage


def _check_passthrough(entry, usage: dict, counts) -> None:
    requested, raw = _costs(usage)
    meta = json.loads(entry.meta_json)
    if (entry.kind != "spend" or entry.amount_micro != 0 or entry.reservation_id is not None
            or any(getattr(entry, name) != usage[name] for name in ("account_id", "purpose", "model", "provider", "request_id"))
            or (entry.prompt_tokens, entry.completion_tokens) != counts[:2]
            or meta.get("passthrough") is not True or meta.get("actual_micro") != requested
            or meta.get("provider_actual_micro") != raw or meta.get("cached_tokens") != counts[2]
            or any(type(meta.get(name)) is not int for name in ("actual_micro", "provider_actual_micro", "estimate_micro", "cached_tokens"))
            or any(name not in meta or meta[name] != usage[name] for name in ("source", "estimate_micro", "workspace_id", "venture_id", "task_invocation"))):
        raise CreditsUnavailable("passthrough receipt needs reconciliation")


def _passthrough_ledger(meter: Meter, usage: dict, counts) -> None:
    if not meter.account_id:
        return  # BYOK task admission never invents a wallet or reservation.
    from app.credits._txn import serialize_writes, session_factory
    with session_factory()() as session:
        serialize_writes(session, key=meter.account_id)
        account = session.get(CreditAccount, meter.account_id)
        if account is None:
            raise CreditsUnavailable("credit_account_unavailable")
        key = f"passthrough:{usage['request_id']}"
        prior = find_by_idempotency_key(key, session)
        if prior is None:
            prior = append(session, account=account, kind="spend", amount_micro=0,
                reason="passthrough usage", actor_kind="system", actor_id=meter.payer or "system",
                idempotency_key=key, purpose=meter.purpose, model=meter.model, provider=meter.provider,
                prompt_tokens=meter.prompt_tokens, completion_tokens=meter.completion_tokens,
                request_id=usage["request_id"], meta={"passthrough": True, "payer": meter.payer,
                    "source": usage["source"], "estimate_micro": usage["estimate_micro"],
                    "actual_micro": usage["actual_micro"], "provider_actual_micro": usage["provider_actual_micro"],
                    "workspace_id": meter.workspace_id, "venture_id": meter.venture_id,
                    "task_invocation": meter.task_invocation, "cached_tokens": meter.cached_tokens})
        _check_passthrough(prior, usage, counts)
        session.commit()


def _actual_for_settle(meter: Meter) -> int:
    if meter.actual_micro is not None:
        return meter.actual_micro
    if meter.model_binding is not None:
        raise CreditsUnavailable("provider cost needs reconciliation")
    if meter.model and meter.prompt_tokens is not None and meter.completion_tokens is not None:
        return actual_llm_micro(meter.model, prompt_tokens=meter.prompt_tokens,
                                completion_tokens=meter.completion_tokens, cached_tokens=meter.cached_tokens)
    raise CreditsUnavailable("provider cost needs reconciliation")


def _check_hold(reservation, usage: dict) -> None:
    if (reservation is None or reservation.is_pool or reservation.parent_id is not None
            or type(usage.get("estimate_micro")) is not int or usage["estimate_micro"] < 1
            or any(name not in usage or usage[name] != getattr(reservation, column) for name, column in _BINDING.items())):
        raise CreditsUnavailable("reservation receipt scope mismatch")


def _check_settled(reservation, entry, usage: dict, counts) -> int:
    requested, raw = _costs(usage)
    meta = json.loads(entry.meta_json) if entry is not None else {}
    if (reservation.status != "settled" or entry is None or entry.kind != "spend"
            or entry.reservation_id != reservation.id or entry.account_id != reservation.account_id
            or type(reservation.settled_micro) is not int or entry.amount_micro != -reservation.settled_micro
            or any(getattr(entry, name) != usage[name] for name in ("purpose", "model", "provider", "request_id"))
            or (entry.prompt_tokens, entry.completion_tokens) != counts[:2]
            or meta.get("hold_micro") != usage["estimate_micro"]
            or meta.get("provider_actual_micro") != requested
            or meta.get("raw_provider_actual_micro", requested) != raw
            or type(meta.get("hold_micro")) is not int or type(meta.get("provider_actual_micro")) is not int
            or type(meta.get("raw_provider_actual_micro", requested)) is not int or type(meta.get("cached_tokens")) is not int
            or meta.get("clamped") is not bool(reservation.clamped) or meta.get("cached_tokens") != counts[2]
            or any(name not in meta or meta[name] != usage[name] for name in ("workspace_id", "venture_id", "task_invocation"))):
        raise CreditsUnavailable("settlement receipt needs reconciliation")
    return reservation.settled_micro


def _settle_receipt(usage: dict, counts) -> SettleResult:
    from app.credits._txn import session_factory
    hold = Hold(usage["reservation_id"], usage["account_id"], usage["estimate_micro"])
    with session_factory()() as session:
        reservation = session.get(CreditReservation, hold.reservation_id)
        _check_hold(reservation, usage)
        if reservation.status == "settled":
            _check_settled(reservation, find_by_idempotency_key(f"settle:{hold.reservation_id}", session), usage, counts)
        elif reservation.status not in {"active", "open", "reconciling"}:
            raise CreditsUnavailable("reservation is already closed")
    requested, raw = _costs(usage)
    result = settle(hold, actual_micro=requested, provider_actual_micro=raw,
                    prompt_tokens=counts[0], completion_tokens=counts[1], cached_tokens=counts[2])
    with session_factory()() as session:
        reservation = session.get(CreditReservation, hold.reservation_id)
        _check_hold(reservation, usage)
        entry = find_by_idempotency_key(f"settle:{hold.reservation_id}", session)
        charged = _check_settled(reservation, entry, usage, counts)
        if (not isinstance(result, SettleResult) or result.reservation_id != hold.reservation_id
                or result.account_id != hold.account_id or result.hold_micro != hold.amount_micro
                or result.actual_micro != charged or result.ledger_id != entry.id):
            raise CreditsUnavailable("settlement did not confirm its receipt")
    return result


def _legacy_receipt(reservation, account, entry, usage: dict, binding: dict) -> int:
    """Read only: the exact pre-attribution, no-task funded receipt cohort."""
    meta = json.loads(entry.meta_json)
    if (set(meta) != {"clamped", "hold_micro"} or type(meta["clamped"]) is not bool or type(meta["hold_micro"]) is not int
            or reservation.status != "settled" or reservation.is_pool or reservation.parent_id is not None
            or meta["clamped"] is not bool(reservation.clamped)
            or reservation.task_invocation is not None or usage.get("task_invocation")
            or reservation.venture_id is not None or account is None or account.scope != "venture"
            or binding.get("workspace_id") != account.workspace_id or binding.get("venture_id") != account.venture_id
            or not binding.get("request_id") or reservation.workspace_id != binding["workspace_id"]
            or reservation.request_id != binding["request_id"]
            or reservation.account_id != account.id or entry.account_id != account.id or entry.kind != "spend"
            or entry.reservation_id != reservation.id or entry.amount_micro != -reservation.settled_micro
            or meta["hold_micro"] != reservation.amount_micro
            or usage.get("account_id") != account.id or usage.get("estimate_micro") != reservation.amount_micro
            or any(name in _BINDING and name != "venture_id" and value != getattr(reservation, _BINDING[name])
                   for name, value in binding.items())
            or any(getattr(entry, name) != getattr(reservation, name) for name in ("purpose", "model", "provider", "request_id"))
            or any(name in usage and usage[name] != getattr(reservation, name) for name in ("purpose", "model", "provider", "request_id", "workspace_id"))
            or (entry.prompt_tokens, entry.completion_tokens) != (usage.get("prompt_tokens"), usage.get("completion_tokens"))):
        raise CreditsUnavailable("legacy receipt scope mismatch")
    prompt, completion, cached = usage.get("prompt_tokens"), usage.get("completion_tokens"), usage.get("cached_tokens", 0)
    if type(cached) is not int or cached < 0:
        raise CreditsUnavailable("legacy token counts are invalid")
    if prompt is None and completion is None:
        if cached or usage.get("total_tokens") is not None:
            raise CreditsUnavailable("legacy token counts are unavailable")
    elif (any(type(n) is not int or n < 0 for n in (prompt, completion)) or cached > prompt
            or ("total_tokens" in usage and (type(usage["total_tokens"]) is not int or usage["total_tokens"] != prompt + completion))):
        raise CreditsUnavailable("legacy token counts are invalid")
    charged = reservation.settled_micro
    if type(charged) is not int or charged < 0:
        raise CreditsUnavailable("legacy charge is unavailable")
    for name in ("actual_micro", "provider_actual_micro"):
        if name in usage and (type(usage[name]) is not int or usage[name] < 0
                or (usage[name] <= charged if meta["clamped"] else usage[name] != charged)):
            raise CreditsUnavailable("legacy provider cost shape is invalid")
    if "actual_micro" in usage and "provider_actual_micro" in usage and usage["provider_actual_micro"] < usage["actual_micro"]:
        raise CreditsUnavailable("legacy provider cost shape is invalid")
    return charged


def _bound_no_task_receipt(usage: dict, reservation, binding: dict) -> dict:
    """Accepted pre-hook journals omitted attribution already captured by the hold."""
    missing = set(_BINDING) - usage.keys()
    if not missing:
        return usage
    if (reservation is None or reservation.task_invocation is not None or usage.get("task_invocation")
            or not {"workspace_id", "venture_id", "request_id"}.issubset(binding)
            or any(binding[name] != getattr(reservation, name) for name in ("workspace_id", "venture_id", "request_id"))
            or not binding["request_id"]
            or missing - {"workspace_id", "venture_id", "purpose", "request_id", "task_invocation"}):
        raise CreditsUnavailable("receipt attribution is not proven")
    return {**usage, **{name: getattr(reservation, _BINDING[name]) for name in missing}}


def reconcile_receipt(usage: dict, expected_binding: dict | None = None) -> int:
    """Reconcile trusted saved custody, returning platform charged micro only.

    Task counters consume raw provider cost, independently of payer/wallet caps.
    A definitive_unspent receipt releases the same hold and aborts the same task.
    """
    from app.credits._txn import session_factory
    try:
        binding = expected_binding or {}
        if any(name not in {*_BINDING, "reservation_id", "source", "model_binding", "request_digest"} for name in binding):
            raise CreditsUnavailable("receipt binding is invalid")
        modern = ("model_binding", "request_digest")
        if any(binding.get(name) is not None for name in modern):
            if any(name not in binding or binding[name] is None or name not in usage or usage[name] is None
                   for name in modern):
                raise CreditsUnavailable("receipt binding mismatch")
            if json.dumps(_primitive_copy({name: usage[name] for name in modern}), sort_keys=True) != json.dumps(
                _primitive_copy({name: binding[name] for name in modern}), sort_keys=True):
                raise CreditsUnavailable("receipt binding mismatch")
        if any(name in usage and usage[name] != value for name, value in binding.items()):
            raise CreditsUnavailable("receipt binding mismatch")
        source = usage.get("source")
        unspent = usage.get("definitive_unspent") is True
        if not unspent and (usage.get("model_binding") is not None or usage.get("request_digest") is not None):
            _check_model_projection(usage)
        invocation = usage.get("task_invocation")
        if invocation is not None and (not isinstance(invocation, str) or not invocation):
            raise CreditsUnavailable("task invocation is invalid")
        if source not in {"platform", "byok", "free"}:
            raise CreditsUnavailable("receipt payer is unavailable")
        if is_local():
            from app.credits import local_meter
            if unspent:
                charged = local_meter.reconcile(_primitive_copy(usage))
                if invocation:
                    _task_usage("abort_call", invocation, definitive=True)
                return charged
            counts = _counts(usage, required=bool(usage.get("model")))
            _costs(usage)
            receipt = _primitive_copy(usage)
            receipt.pop("definitive_unspent", None)
            if receipt.get("model_binding") is not None:
                receipt.setdefault("provider_output_error", None)
                if receipt.pop("provenance", "captured_catalog_usage") != "captured_catalog_usage":
                    raise CreditsUnavailable("Check your saved task result and spending history, then try again.")
                receipt.pop("output", None)
            elif receipt.get("model") and receipt.get("provenance") == "provider_usage":
                receipt.pop("output", None)
            elif receipt.get("model") is None:
                for name in ("model_binding", "request_digest", "token_ceiling"):
                    if receipt.get(name) is None or (name == "token_ceiling"
                            and type(receipt[name]) is int and receipt[name] == 0):
                        receipt.pop(name, None)
            if "settled" in receipt and receipt.pop("settled") is not True:
                raise CreditsUnavailable("Check your saved task result and spending history, then try again.")
            if "credits_micro" in receipt:
                charged = receipt.pop("credits_micro")
                if type(charged) is not int or charged != 0:
                    raise CreditsUnavailable("Check your saved task result and spending history, then try again.")
            charged = local_meter.reconcile(receipt)
            if invocation:
                _task_usage("after_call", invocation, actual_micro=_costs(usage)[1], tokens_used=counts[3])
            return charged
        if source == "platform":
            with session_factory()() as session:
                reservation = session.get(CreditReservation, usage["reservation_id"])
                entry = find_by_idempotency_key(f"settle:{usage['reservation_id']}", session)
                meta = json.loads(entry.meta_json) if entry is not None else {}
                if entry is not None and set(meta) == {"clamped", "hold_micro"} and not unspent:
                    return _legacy_receipt(reservation, session.get(CreditAccount, usage["account_id"]), entry, usage, binding)
                usage = _bound_no_task_receipt(usage, reservation, binding)
                _check_hold(reservation, usage)
                if any(name in _BINDING and value != getattr(reservation, _BINDING[name])
                       for name, value in binding.items()):
                    raise CreditsUnavailable("receipt binding mismatch")
            if unspent:
                release(Hold(usage["reservation_id"], usage["account_id"], usage["estimate_micro"]), definitive=True)
                with session_factory()() as session:
                    reservation = session.get(CreditReservation, usage["reservation_id"])
                    _check_hold(reservation, usage)
                    if reservation.status != "released" or find_by_idempotency_key(f"settle:{reservation.id}", session) is not None:
                        raise CreditsUnavailable("reservation release is not confirmed")
                charged = 0
            else:
                counts = _counts(usage, required=bool(usage.get("model")))
                charged = _settle_receipt(usage, counts).actual_micro
        else:
            if (usage.get("reservation_id") is not None or any(name not in usage for name in _BINDING)
                    or type(usage["estimate_micro"]) is not int or usage["estimate_micro"] < 0):
                raise CreditsUnavailable("passthrough receipt scope mismatch")
            if usage["account_id"] is None and invocation:
                required_binding = {"workspace_id", "venture_id", "request_id", "source", "task_invocation"}
                if (not required_binding.issubset(binding) or not binding["workspace_id"]
                        or not binding["request_id"] or not binding["task_invocation"]):
                    raise CreditsUnavailable("task receipt lacks a trusted journal binding")
            charged = 0
            if not unspent:
                counts = _counts(usage, required=bool(usage.get("model")))
                if usage["account_id"] is not None:
                    entry = find_by_idempotency_key(f"passthrough:{usage['request_id']}")
                    if entry is None:
                        raise CreditsUnavailable("passthrough ledger is unavailable")
                    _check_passthrough(entry, usage, counts)
        invocation = usage.get("task_invocation")
        if invocation:
            if unspent:
                _task_usage("abort_call", invocation, definitive=True)
            else:
                _task_usage("after_call", invocation, actual_micro=_costs(usage)[1], tokens_used=counts[3])
        return charged
    except CreditsUnavailable:
        raise
    except Exception as exc:
        raise CreditsUnavailable("credit usage needs reconciliation") from exc


def _exit(meter: Meter) -> None:
    if meter.local:
        from app.credits import local_meter
        try:
            local_meter.finish(meter)
            if meter.task_invocation:
                if meter.recorded:
                    usage = captured_receipt(meter)
                    _task_usage("after_call", meter.task_invocation,
                        actual_micro=usage["provider_actual_micro"], tokens_used=_counts(usage, required=bool(meter.model))[3])
                else:
                    _task_usage("abort_call", meter.task_invocation,
                        definitive=meter.definitive_failure)
        except Exception as exc:
            meter.task_usage_failed = True
            if meter.task_invocation:
                _task_usage("abort_call", meter.task_invocation, definitive=False)
            raise CreditsUnavailable("You cannot confirm your task's spending right now. "
                "Check your spending history before trying again.") from exc
        finally:
            if meter.allowance_settle is not None:
                try:
                    meter.allowance_settle(meter)
                except Exception as exc:
                    meter.task_usage_failed = True
                    raise CreditsUnavailable("You cannot confirm this task's spending limit right now. "
                        "Open your task list before trying again.") from exc
        return
    """Persist financial receipt before task finalization; preserve provider errors."""
    try:
        if meter.recorded:
            usage = _meter_usage(meter)
            if meter.model_binding is not None:
                if not meter._model_receipt_checkpointed:
                    raise CreditsUnavailable("provider receipt checkpoint is unavailable")
                _check_model_projection(usage)
            counts = _counts(usage, required=bool(meter.model and meter.task_invocation))
            if meter.passthrough or meter.hold is None:
                _passthrough_ledger(meter, usage, counts)
            else:
                meter.settle_result = _settle_receipt(usage, counts)
            if meter.task_invocation:
                _task_usage("after_call", meter.task_invocation, actual_micro=_costs(usage)[1], tokens_used=counts[3])
            if meter.model_journal_key:
                from app.credits.metered import recover_model_call
                recover_model_call(meter.model_journal_key)
            return
        definitive = not meter.outcome_protected or meter.definitive_failure
        if meter.hold is not None and not meter.passthrough:
            if definitive:
                release(meter.hold, definitive=meter.definitive_failure)
            else:
                log.warning("credits.outcome_reconciliation_required reservation=%s", meter.hold.reservation_id)
        if meter.task_invocation:
            _task_usage("abort_call", meter.task_invocation, definitive=definitive)
        if meter.model_journal_key and meter._model_receipt_checkpointed and not meter.definitive_failure:
            # A failing evidence-only hook cannot lose the already-owned SDK
            # receipt. Reconcile its saved original quote, never the raw output.
            from app.credits.metered import recover_model_call
            recover_model_call(meter.model_journal_key)
        if meter.model_journal_key and meter.definitive_failure:
            from app.credits.metered import recover_model_call
            recover_model_call(meter.model_journal_key)
    except Exception:
        meter.task_usage_failed = True
        if meter.task_invocation:
            try:
                _task_usage("abort_call", meter.task_invocation, definitive=False)
            except Exception:
                log.exception("task usage reconciliation failed")
        log.exception("credits.receipt_reconciliation_required reservation=%s account=%s",
                      meter.hold.reservation_id if meter.hold else "none", meter.account_id)
    finally:
        if meter.allowance_settle is not None:
            try:
                meter.allowance_settle(meter)
            except Exception:
                meter.task_usage_failed = True
                log.exception("credits.allowance_reconciliation_required")

@asynccontextmanager
async def charge(
    *,
    workspace_id: str | None = None,
    account_id: str | None = None,
    purpose: str,
    estimate_micro: int,
    model: str | None = None,
    provider: str | None = None,
    request_id: str | None = None,
    payer: str | None = None,
    venture_id: str | None = None,
    max_tokens: int = 0,
    model_binding: dict | None = None,
    request_digest: str | None = None,
    actor_id: str | None = None,
) -> AsyncIterator[Meter]:
    meter = await asyncio.to_thread(
        _enter,
        workspace_id=workspace_id,
        account_id=account_id,
        purpose=purpose,
        model=model,
        provider=provider,
        estimate_micro=estimate_micro,
        request_id=request_id,
        payer=payer,
        venture_id=venture_id,
        max_tokens=max_tokens,
        model_binding=model_binding,
        request_digest=request_digest,
        **({"actor_id": actor_id} if actor_id is not None else {}),
    )
    try:
        yield meter
    except BaseException:
        await asyncio.to_thread(_exit, meter)
        raise
    else:
        await asyncio.to_thread(_exit, meter)
        if meter.task_usage_failed:
            raise CreditsUnavailable("credit usage needs reconciliation")
        if meter.billing_output_error:
            raise CreditsUnavailable("provider output is unusable")


@contextmanager
def charge_sync(
    *,
    workspace_id: str | None = None,
    account_id: str | None = None,
    purpose: str,
    estimate_micro: int,
    model: str | None = None,
    provider: str | None = None,
    request_id: str | None = None,
    payer: str | None = None,
    venture_id: str | None = None,
    max_tokens: int = 0,
    model_binding: dict | None = None,
    request_digest: str | None = None,
    actor_id: str | None = None,
) -> Iterator[Meter]:
    meter = _enter(
        workspace_id=workspace_id,
        account_id=account_id,
        purpose=purpose,
        model=model,
        provider=provider,
        estimate_micro=estimate_micro,
        request_id=request_id,
        payer=payer,
        venture_id=venture_id,
        max_tokens=max_tokens,
        model_binding=model_binding,
        request_digest=request_digest,
        **({"actor_id": actor_id} if actor_id is not None else {}),
    )
    try:
        yield meter
    except BaseException:
        _exit(meter)
        raise
    else:
        _exit(meter)
        if meter.task_usage_failed:
            raise CreditsUnavailable("credit usage needs reconciliation")
        if meter.billing_output_error:
            raise CreditsUnavailable("provider output is unusable")

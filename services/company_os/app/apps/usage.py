"""Record AI proxy usage against app_usage_records + credit ledger."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
import json
import re

from sqlalchemy import func, select

from app.credits.accounts import account_can_spend, account_for_workspace, get_account
from app.credits.errors import CreditsUnavailable, InsufficientCredits
from app.credits.reserve import Hold, protect_outcome, reserve, settle
from app.credits._txn import naive_utc, serialize_writes
from app.db.models import CreditAccount, CreditReservation, EventLog
from app.credits.units import MIN_RESERVE_MICRO
from app.db.supymem_models import AppUsageRecord, FounderApp
from app.state import db
from core.clock import utcnow


def month_used_micro(app_id: str) -> int:
    now = utcnow()
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    factory = db()
    with factory() as session:
        total = session.scalar(
            select(func.coalesce(func.sum(AppUsageRecord.credits_micro), 0)).where(
                AppUsageRecord.app_id == app_id,
                AppUsageRecord.window_start >= start,
            )
        )
        return int(total or 0)


def hold_for_proxy(
    *,
    workspace_id: str,
    venture_id: str,
    amount_micro: int,
    request_id: str,
    model: str,
    provider: str | None = None,
) -> Hold:
    amount = max(amount_micro, MIN_RESERVE_MICRO)
    account = account_for_workspace(workspace_id) or get_account(venture_id)
    if account is None:
        # No account means nobody pays: refuse instead of serving the model for free.
        raise InsufficientCredits("none", 0, amount)
    args = dict(account_id=account.id, amount_micro=amount, purpose="app.proxy",
                model=model, workspace_id=workspace_id, request_id=request_id)
    # Omitted provider retains the accepted legacy issuer's exact custody shape.
    if provider is not None:
        args.update(provider=provider, venture_id=venture_id)
    return reserve(**args)


@dataclass(frozen=True)
class ProxySettlement:
    """Billing/ceiling result only; payload and current authority remain caller-owned."""
    charged_micro: int
    ledger_entry_id: str
    output_usable: bool
    output_error: str | None


def _need(value: bool) -> None:
    if not value:
        raise CreditsUnavailable("app_proxy_receipt_binding")


def _primitive_tree(value, depth: int = 0) -> bool:
    """Reject opaque/subclassed SDK values before any copy/serialization hook."""
    if depth > 20:
        return False
    if value is None or type(value) in {str, int, bool}:
        return True
    if type(value) is dict:
        return all(type(key) is str and _primitive_tree(item, depth + 1) for key, item in value.items())
    if type(value) is list:
        return all(_primitive_tree(item, depth + 1) for item in value)
    return False


def _same(left, right) -> bool:
    _need(_primitive_tree(left) and _primitive_tree(right))
    return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(right, sort_keys=True, allow_nan=False)


def _event_id(hold: Hold, phase: str) -> str:
    return f"app-proxy:{hold.reservation_id}:{phase}"


def _read_event(session, hold: Hold, phase: str) -> dict | None:
    row = session.scalar(select(EventLog).where(EventLog.id == _event_id(hold, phase)))
    if row is None:
        return None
    payload = json.loads(row.payload_json)
    _need(_primitive_tree(payload) and type(payload) is dict and type(payload.get("schema_version")) is int
          and payload["schema_version"] == 1)
    usage = payload.get("usage")
    _need(type(usage) is dict and usage.get("reservation_id") == hold.reservation_id
          and usage.get("account_id") == hold.account_id and usage.get("estimate_micro") == hold.amount_micro
          and row.topic == "app.proxy.receipt." + phase and row.target_ref == hold.reservation_id
          and row.workspace_id == usage.get("workspace_id") and row.venture_id == usage.get("venture_id")
          and row.actor == row.actor_id == "app.proxy" and row.actor_kind == "system"
          and row.surface == "api" and row.outcome == "ok" and row.on_behalf_of is None
          and row.approval_id is None and row.denied_reason is None)
    return payload


def _append_event(session, hold: Hold, phase: str, payload: dict) -> None:
    # Financial custody must fail closed; the generic audit writer intentionally
    # swallows persistence errors and cannot serve as this journal.
    serialized = json.dumps(payload, sort_keys=True, allow_nan=False, separators=(",", ":"))
    _need(len(serialized.encode()) <= 65536)
    session.add(EventLog(id=_event_id(hold, phase), topic="app.proxy.receipt." + phase,
        workspace_id=payload["usage"]["workspace_id"], venture_id=payload["usage"]["venture_id"],
        actor="app.proxy", actor_id="app.proxy", actor_kind="system", surface="api",
        target_ref=hold.reservation_id, outcome="ok", ts=naive_utc(),
        payload_json=serialized))


def _hold_usage(session, hold: Hold, request_id: str) -> dict:
    row = session.get(CreditReservation, hold.reservation_id)
    _need(row is not None and row.account_id == hold.account_id and row.amount_micro == hold.amount_micro
          and row.request_id == request_id and row.purpose == "app.proxy" and not row.is_pool
          and row.parent_id is None)
    return {"source": "platform", "reservation_id": row.id, "account_id": row.account_id,
        "estimate_micro": row.amount_micro, "workspace_id": row.workspace_id, "venture_id": row.venture_id,
        "purpose": row.purpose, "model": row.model, "provider": row.provider,
        "request_id": row.request_id, "task_invocation": row.task_invocation}


def _saved(session, hold: Hold, request_id: str) -> dict:
    begin = _read_event(session, hold, "begin")
    _need(begin is not None and _same(begin["usage"], _hold_usage(session, hold, request_id)))
    row = session.get(CreditReservation, hold.reservation_id)
    immutable = {key: getattr(row, key) for key in ("parent_id", "is_pool", "remaining_micro")}
    immutable.update(created_at=row.created_at.isoformat(), expires_at=row.expires_at.isoformat())
    account = session.get(CreditAccount, hold.account_id)
    account_fields = {"id", "scope", "workspace_id", "venture_id", "owner_person_id"}
    _need(type(begin.get("account_snapshot")) is dict and set(begin["account_snapshot"]) == account_fields
          and _same(immutable, begin["hold_snapshot"]) and account is not None
          and _same({key: getattr(account, key) for key in account_fields}, begin["account_snapshot"]))
    return begin


def _safe_response(value: dict, begin: dict) -> dict:
    """Detach billing-only SDK evidence before any fallible price calculation."""
    from app.secrets.detect import looks_like_secret
    fields = {"schema_version", "authority", "requires_proposal_apply", "request_digest", "provider",
        "deployment", "catalog_model", "model", "api_mode", "input_tokens", "output_tokens",
        "total_tokens", "cached_tokens", "reasoning_tokens", "service_tier", "role_metadata"}
    _need(type(value) is dict and all(type(key) is str for key in value)
          and fields <= value.keys() and set(value) <= fields | {"result", "error_code"})
    receipt = {key: value[key] for key in fields}  # keep ORIGINAL values through strict validation
    _need(_primitive_tree(receipt))  # never retain/copy provider payload/text or run SDK object hooks
    _need(type(receipt["schema_version"]) is int and receipt["schema_version"] == 1
          and type(receipt["authority"]) is str and receipt["authority"] in {"observation_only", "proposal_only"}
          and type(receipt["requires_proposal_apply"]) is bool
          and receipt["request_digest"] == begin["request_digest"])
    bound = begin["capture"]["model_binding"]
    for key in ("provider", "deployment", "catalog_model", "api_mode"):
        _need(type(receipt[key]) is str and receipt[key] == bound[key])
    for key in ("model", "service_tier"):
        name = receipt[key]
        pattern = r"[a-z][a-z0-9_-]{0,31}" if key == "service_tier" else r"[A-Za-z0-9._:/-]{1,256}"
        _need(name is None and key == "service_tier" or type(name) is str and
              re.fullmatch(pattern, name) is not None and "://" not in name
              and not looks_like_secret(name))
    counters = {key: receipt[key] for key in
        ("input_tokens", "output_tokens", "total_tokens", "cached_tokens", "reasoning_tokens")}
    _need(all(type(n) is int and 0 <= n < 2**63 for n in counters.values())
          and counters["total_tokens"] == counters["input_tokens"] + counters["output_tokens"]
          and counters["cached_tokens"] <= counters["input_tokens"]
          and counters["reasoning_tokens"] <= counters["output_tokens"])
    role = {key: bound["deployment"] if key == "model" else bound[key] for key in
        ("purpose", "provider", "model", "catalog_model", "api_mode", "max_output_tokens", "endpoint_host")}
    _need(type(receipt["role_metadata"]) is dict and receipt["role_metadata"] == role
          and type(receipt["role_metadata"]["max_output_tokens"]) is int
          and all(type(receipt["role_metadata"][key]) is str for key in role if key != "max_output_tokens"))
    error = value.get("error_code")
    _need(error is None or type(error) is str and error in {"incomplete", "refused", "filtered", "invalid_output"})
    if error is not None:
        receipt["error_code"] = error
    return json.loads(json.dumps(receipt, allow_nan=False))


def begin_proxy_call(hold: Hold, *, app_id: str, request_id: str, capture: dict,
                     request_digest: str) -> None:
    """Persist the original quote and protect its hold before a single HTTP attempt.

    A duplicate begin never authorizes another dispatch, including after an
    unknown acknowledgment. The request digest is evidence, not an identity.
    """
    from app.credits.gate import validate_model_capture
    _need(type(capture) is dict and all(type(key) is str for key in capture) and set(capture) ==
          {"model_binding", "token_ceiling", "estimate_micro", "clock_policy", "request_digest"})
    _need(isinstance(request_digest, str) and re.fullmatch(r"[a-f0-9]{64}", request_digest) is not None
          and capture.get("request_digest", request_digest) == request_digest)
    _need(_primitive_tree(capture))
    captured = json.loads(json.dumps(capture, allow_nan=False))
    # Opaque captured provenance only. The caller owns proof that token/wire
    # allowances match its actual dispatch; this journal invents no clock bound.
    _need(type(captured["clock_policy"]) is str and
          re.fullmatch(r"[A-Za-z0-9_]{1,80}", captured["clock_policy"]) is not None)
    binding = captured["model_binding"]
    _need(type(binding) is dict and type(captured["estimate_micro"]) is int and captured["estimate_micro"] >= 0
          and max(captured["estimate_micro"], MIN_RESERVE_MICRO) == hold.amount_micro)
    captured["model_binding"] = validate_model_capture(binding, request_digest, purpose="app.proxy",
        provider=binding.get("provider"), model=binding.get("deployment"), estimate_micro=hold.amount_micro,
        max_tokens=captured["token_ceiling"])
    binding = captured["model_binding"]
    with db()() as session:
        serialize_writes(session, key=hold.account_id)
        _need(_read_event(session, hold, "begin") is None)
        usage = _hold_usage(session, hold, request_id)
        app = session.get(FounderApp, app_id)
        _need(app is not None and app.deleted_at is None and app.state not in {"retired", "deleted"}
              and (app.workspace_id, app.venture_id) ==
              (usage["workspace_id"], usage["venture_id"]) and usage["model"] == binding["deployment"]
              and usage["provider"] == binding["provider"])
        account = session.get(CreditAccount, hold.account_id)
        _need(account is not None and account.status == "active"
              and account_can_spend(session, account, usage["workspace_id"], usage["venture_id"]))
        row = session.get(CreditReservation, hold.reservation_id)
        _need(row.status in {"active", "open"} and row.remaining_micro is None)
        snapshot = {key: getattr(row, key) for key in ("parent_id", "is_pool", "remaining_micro")}
        snapshot.update(created_at=row.created_at.isoformat(), expires_at=row.expires_at.isoformat())
        payload = {"schema_version": 1, "app_id": app_id, "usage": usage, "hold_snapshot": snapshot,
            "account_snapshot": {key: getattr(account, key) for key in
                ("id", "scope", "workspace_id", "venture_id", "owner_person_id")},
            "capture": captured, "request_digest": request_digest, "window_start": naive_utc().isoformat()}
        _append_event(session, hold, "begin", payload)
        session.commit()
    protect_outcome(hold)
    with db()() as session:
        serialize_writes(session, key=hold.account_id)
        saved = _saved(session, hold, request_id)
        _need(session.get(CreditReservation, hold.reservation_id).status == "reconciling")
        app = session.get(FounderApp, app_id)
        _need(app is not None and app.deleted_at is None and app.state not in {"retired", "deleted"}
              and (app.workspace_id, app.venture_id) == (saved["usage"]["workspace_id"], saved["usage"]["venture_id"])
              and session.get(CreditAccount, hold.account_id).status == "active"
              and account_can_spend(session, session.get(CreditAccount, hold.account_id),
                                    saved["usage"]["workspace_id"], saved["usage"]["venture_id"]))
        _append_event(session, hold, "calling", saved)
        session.commit()


def _priced_outcome(hold: Hold, begin: dict, response: dict) -> dict:
    """Revalidate saved original billing and output status without a live price read."""
    from app.credits.provider_receipts import validate_confirmed_provider_receipt
    evidence = validate_confirmed_provider_receipt({key: value for key, value in response.items() if key != "error_code"},
        model_binding=begin["capture"]["model_binding"], expected_request_digest=begin["request_digest"])
    error = "model_mismatch" if "model_mismatch" in evidence["output_errors"] else (
        "incomplete" if not evidence["output_usable"] else None)
    error = error or response.get("error_code")
    if response.get("error_code") == "invalid_output":
        error = "invalid_output"
    usage = {**begin["usage"], "actual_micro": evidence["provider_actual_micro"],
        "provider_actual_micro": evidence["provider_actual_micro"], "prompt_tokens": evidence["input_tokens"],
        "completion_tokens": evidence["output_tokens"], "total_tokens": evidence["total_tokens"],
        "cached_tokens": evidence["cached_tokens"], "reasoning_tokens": evidence["reasoning_tokens"],
        "model_binding": begin["capture"]["model_binding"], "request_digest": begin["request_digest"],
        "token_ceiling": begin["capture"]["token_ceiling"],
        "provider_receipt": {key: value for key, value in response.items() if key != "error_code"},
        "billing_output_error": response.get("error_code") == "invalid_output" or not evidence["output_usable"]
            or evidence["provider_actual_micro"] > hold.amount_micro,
        "provenance": "captured_catalog_usage", "output_error": error}
    if response.get("error_code") == "invalid_output":
        usage["provider_output_error"] = "invalid_output"
    return {**begin, "usage": usage, "evidence": evidence}


def record_proxy_outcome(hold: Hold, *, request_id: str, confirmed_receipt: dict | None = None,
                         error_code: str | None = None) -> None:
    """Checkpoint complete SDK billing evidence before projecting any money."""
    with db()() as session:
        serialize_writes(session, key=hold.account_id)
        begin = _saved(session, hold, request_id)
        _need(_same(_read_event(session, hold, "calling"), begin))
        if confirmed_receipt is None:
            _need(error_code is None or type(error_code) is str and
                  error_code in {"provider_error", "usage_unknown", "transport_unknown"})
            if _read_event(session, hold, "uncertain") is None:
                _append_event(session, hold, "uncertain", {**begin, "error_code": error_code or "usage_unknown"})
                session.commit()
            return
        try:
            response = _safe_response(confirmed_receipt, begin)
        except CreditsUnavailable:
            if _read_event(session, hold, "uncertain") is None:
                _append_event(session, hold, "uncertain", {**begin, "error_code": "usage_unknown"})
                session.commit()
            raise
        old_response = _read_event(session, hold, "response")
        if old_response is None:
            _append_event(session, hold, "response", {**begin, "response": response})
            session.commit()  # confirmed counts survive unknown model/rate/tier and price failures
            serialize_writes(session, key=hold.account_id)
            _need(_same(_saved(session, hold, request_id), begin)
                  and _same(_read_event(session, hold, "response"), {**begin, "response": response}))
        else:
            _need(_same(old_response, {**begin, "response": response}))
        outcome = _priced_outcome(hold, begin, response)
        old = _read_event(session, hold, "responded")
        if old is not None:
            _need(_same(old, outcome))
            return
        _append_event(session, hold, "responded", outcome)
        session.commit()


def inspect_proxy_call(hold: Hold, *, request_id: str) -> str:
    """Inspect pending custody; this never grants a dispatch or unspent proof."""
    with db()() as session:
        serialize_writes(session, key=hold.account_id)
        _saved(session, hold, request_id)
        for phase in ("completed", "responded", "response", "uncertain", "calling", "begin"):
            if _read_event(session, hold, phase) is not None:
                return phase
    raise CreditsUnavailable("app_proxy_receipt_pending")


def _settle_saved_proxy(hold: Hold, *, request_id: str) -> ProxySettlement:
    """Recover the saved original receipt, then project app usage exactly once."""
    from app.credits.gate import reconcile_receipt
    from app.credits.ledger import find_by_idempotency_key
    with db()() as session:
        serialize_writes(session, key=hold.account_id)
        begin = _saved(session, hold, request_id)
        response = _read_event(session, hold, "response")
        cost = _read_event(session, hold, "responded")
    if cost is None and response is not None:
        # A known response may have reached disk before pricing failed. Only
        # that same saved evidence and original snapshot may be retried.
        record_proxy_outcome(hold, request_id=request_id, confirmed_receipt=response["response"])
    with db()() as session:
        serialize_writes(session, key=hold.account_id)
        begin = _saved(session, hold, request_id)
        response = _read_event(session, hold, "response")
        saved = deepcopy(_read_event(session, hold, "responded"))
        _need(response is not None and saved is not None)
        safe = _safe_response(response["response"], begin)
        _need(_same(response, {**begin, "response": safe})
              and _same(saved, _priced_outcome(hold, begin, safe)))
        original = begin["usage"]
        _need(_same({key: saved["usage"][key] for key in original}, original))
    usage = saved["usage"]
    binding = {**original, "model_binding": begin["capture"]["model_binding"],
               "request_digest": begin["request_digest"]}
    binding.pop("estimate_micro")
    charged = reconcile_receipt(usage, binding)
    _need(type(charged) is int and charged >= 0)
    with db()() as session:
        serialize_writes(session, key=hold.account_id)
        _need(_same(_saved(session, hold, request_id), begin) and _same(_read_event(session, hold, "responded"), saved))
        ledger = find_by_idempotency_key("settle:" + hold.reservation_id, session)
        _need(ledger is not None and ledger.amount_micro == -charged and ledger.account_id == hold.account_id
              and ledger.reservation_id == hold.reservation_id and ledger.request_id == request_id)
        meta = json.loads(ledger.meta_json)
        _need(type(meta.get("clamped")) is bool
              and meta.get("provider_actual_micro") == usage["actual_micro"]
              and meta.get("raw_provider_actual_micro") == usage["provider_actual_micro"])
        output_error = usage["output_error"]
        if meta["clamped"] or usage["provider_actual_micro"] > hold.amount_micro:
            output_error = "credit_overage"
        from datetime import datetime
        start = datetime.fromisoformat(begin["window_start"])
        row = session.scalar(select(AppUsageRecord).where(AppUsageRecord.app_id == begin["app_id"],
            AppUsageRecord.meter == "ai_tokens_out", AppUsageRecord.window_start == start,
            AppUsageRecord.source == request_id))
        projection = {"app_id": begin["app_id"], "workspace_id": original["workspace_id"],
            "meter": "ai_tokens_out", "quantity": usage["completion_tokens"], "credits_micro": charged,
            "source": request_id, "end_user_hash": None, "window_start": start,
            "window_end": start + timedelta(seconds=1), "ledger_entry_id": ledger.id}
        if row is None:
            session.add(AppUsageRecord(**projection))
        else:
            _need(all(getattr(row, key) == value for key, value in projection.items()))
        ledger_id = ledger.id
        completed = {**saved, "charged_micro": charged, "ledger_entry_id": ledger_id,
            "output_error": output_error}
        old = _read_event(session, hold, "completed")
        if old is None:
            _append_event(session, hold, "completed", completed)
        else:
            _need(_same(old, completed))
        session.commit()
    return ProxySettlement(charged, ledger_id, output_error is None, output_error)


def recover_proxy(hold: Hold, *, request_id: str) -> ProxySettlement:
    return _settle_saved_proxy(hold, request_id=request_id)


_MISSING = object()


def settle_proxy(hold: Hold | None, *, actual_micro=_MISSING, tokens_in=_MISSING, tokens_out=_MISSING,
                 request_id: str | None = None) -> int | ProxySettlement:
    """Accepted legacy kwargs or explicit saved modern request, never a mixed mode."""
    legacy = (actual_micro, tokens_in, tokens_out)
    if request_id is not None:
        _need(type(request_id) is str and bool(request_id) and hold is not None
              and all(value is _MISSING for value in legacy))
        return _settle_saved_proxy(hold, request_id=request_id)
    if any(value is _MISSING for value in legacy):
        raise TypeError("legacy settlement requires actual_micro, tokens_in and tokens_out")
    if hold is None:
        return actual_micro
    with db()() as session:
        serialize_writes(session, key=hold.account_id)
        _need(_read_event(session, hold, "begin") is None)
    # Release this inspection transaction before the existing independent settle.
    result = settle(hold, actual_micro=max(actual_micro, 0), prompt_tokens=tokens_in,
                    completion_tokens=tokens_out)
    return int(result.actual_micro)


def write_usage(
    *,
    app_id: str,
    workspace_id: str,
    meter: str,
    quantity: float,
    credits_micro: int,
    source: str,
    end_user_hash: str | None,
    ledger_entry_id: str | None = None,
) -> None:
    now = utcnow()
    factory = db()
    with factory() as session:
        session.add(
            AppUsageRecord(
                app_id=app_id,
                workspace_id=workspace_id,
                meter=meter,
                quantity=quantity,
                credits_micro=credits_micro,
                source=source,
                end_user_hash=end_user_hash,
                window_start=now,
                window_end=now + timedelta(seconds=1),
                ledger_entry_id=ledger_entry_id,
            )
        )
        session.commit()


__all__ = [
    "InsufficientCredits",
    "hold_for_proxy",
    "month_used_micro",
    "settle_proxy",
    "begin_proxy_call",
    "record_proxy_outcome",
    "inspect_proxy_call",
    "recover_proxy",
    "ProxySettlement",
    "write_usage",
]

"""Single paid calls through the existing credit gate, without automatic retries.

Durable jobs use ``research.metered.perform`` or save their own result and usage
in ``before_meter_exit`` to recover without repeating provider I/O.
These interactive calls protect unknown outcomes. Hosted chat may explicitly
absorb our liability before its fallback; other calls release only after a
definitive provider rejection. Fixed-event prices may settle at the estimate;
variable costs must supply an ``actual_micro`` receipt reader.
"""

from __future__ import annotations

import inspect
import hashlib
import json
import os
import re
from decimal import Decimal, InvalidOperation

from app.core.local_mode import is_local
from app.credits.errors import CreditsUnavailable
from app.credits.gate import (_primitive_copy, bounded_calls, captured_receipt, charge, charge_sync,
    pending_model_receipt, record_model_receipt)
from app.credits.units import usd_to_micro


def price_micro(name: str, quantity: int | str | Decimal = 1) -> int:
    """Price known usage without markup, using the deployed plan's unit rate."""
    from app.estimate.meters import load_meters

    free_local = False
    if is_local():
        free_local = name in {"builtin_search", "builtin_scrape", "duckduckgo_search", "email_send"}
        if name in {"firecrawl_search", "firecrawl_scrape"}:
            from urllib.parse import urlsplit
            from app.core.local_secrets import read_settings
            endpoint = read_settings("search")["values"].get("firecrawl_url")
            free_local = bool(endpoint and urlsplit(endpoint).hostname != "api.firecrawl.dev")
    row = (load_meters().get("meters") or {}).get(name) or {}
    override = os.environ.get(f"SUPYMEM_METER_{name.upper()}_USD_PER_UNIT")
    try:
        units = Decimal(str(quantity))
        rate = Decimal(0) if free_local else Decimal(override) if override is not None else Decimal(str(row["micro"])) / 1_000_000
        if not units.is_finite() or not rate.is_finite() or units < 0 or rate < 0:
            raise ValueError
    except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
        raise CreditsUnavailable("paid service pricing is not available") from exc
    return usd_to_micro(rate * units)


def _target(workspace_id: str | None, venture_id: str | None, account_id: str | None) -> dict:
    if not is_local() and venture_id and not account_id:
        from app.credits.accounts import account_for_venture

        account = account_for_venture(venture_id)
        if account is None:
            raise CreditsUnavailable()
        account_id = account.id
    return {"workspace_id": workspace_id, "account_id": account_id, "venture_id": venture_id}


def _confirm_rejection(meter, exc: Exception) -> None:
    from app.research.metered import _failure

    state, _ = _failure(exc)
    if state in {"failed", "retryable"}:
        meter.confirm_unspent()


def firecrawl_actual_micro(result, hosted_micro: int) -> int:
    """Self-hosted calls have no local provider bill; hosted VM accounting stays separate."""
    usage = getattr(result, "usage", None)
    if usage is None and isinstance(result, dict):
        usage = result.get("usage")
    if isinstance(usage, dict) and usage.get("firecrawl_self_hosted") is True:
        return (0 if price_micro("firecrawl_search") == 0 else hosted_micro) if is_local() else 1
    return hosted_micro


def _record(meter, result, estimate_micro: int, actual_micro) -> None:
    actual = actual_micro(result) if actual_micro is not None else estimate_micro
    if getattr(meter, "provider", None) == "firecrawl" and actual_micro is None:
        actual = firecrawl_actual_micro(result, actual)
    usage = getattr(result, "usage", None)
    if meter.model and usage is not None:
        from llm_gateway.usage import extract_confirmed_token_usage
        counts = extract_confirmed_token_usage(usage)
        if counts is None:
            raise CreditsUnavailable("provider usage needs reconciliation")
        meter.record(prompt_tokens=counts["prompt_tokens"], completion_tokens=counts["completion_tokens"],
                     cached_tokens=counts["cached_tokens"])
    meter.record_micro(actual)


def capture_model_receipt(meter, result) -> dict:
    return pending_model_receipt(meter) | capture_confirmed_model_receipt(result,
        model_binding=meter.model_binding, request_digest=meter.request_digest)


def capture_confirmed_model_receipt(result, *, model_binding: dict, request_digest: str) -> dict:
    """Normalize supplied SDK usage fields, excluding unset SDK defaults.

    Unknown or unsafe values become a fixed pending category, never plaintext
    strings, zero counts or a guessed fee. Known aliases remain saved even when
    the original catalog cannot price them.
    """
    from app.credits.provider_receipts import validate_model_binding
    from app.secrets.detect import looks_like_secret
    from llm_gateway.usage import extract_confirmed_token_usage
    binding = validate_model_binding(model_binding)
    if type(request_digest) is not str or re.fullmatch(r"[a-f0-9]{64}", request_digest) is None:
        raise CreditsUnavailable("provider receipt binding mismatch")
    usage = {}
    missing = object()
    def field(obj, name):
        supplied = getattr(obj, "model_fields_set", None)
        if isinstance(supplied, set) and name not in supplied:
            return missing
        return obj.get(name, missing) if type(obj) is dict else getattr(obj, name, missing)
    raw = field(result, "usage")
    candidates = []
    invalid = False
    for inp, out, in_details, out_details in (
        ("prompt_tokens", "completion_tokens", "prompt_tokens_details", "completion_tokens_details"),
        ("input_tokens", "output_tokens", "input_tokens_details", "output_tokens_details"),
    ):
        values = [field(raw, name) for name in (inp, out, in_details, out_details)]
        if all(value is missing for value in values):
            continue
        if any(value is missing or value is None for value in values[:2]):
            invalid = True
            break
        normalized = {"prompt_tokens": values[0], "completion_tokens": values[1],
            "total_tokens": field(raw, "total_tokens")}
        normalized.update({name: value for name, value in zip(
            ("prompt_tokens_details", "completion_tokens_details"), values[2:]) if value is not missing})
        candidate = extract_confirmed_token_usage(normalized)
        if candidate is None or candidates and candidate != candidates[0]:
            invalid = True
            break
        candidates.append(candidate)
    counts = candidates[0] if candidates and not invalid else None
    if counts is not None and any(value > 2**63 - 1 for value in counts.values()):
        counts = None
    model, tier = field(result, "model"), field(result, "service_tier")
    if tier is missing:
        tier = None
    error = ("counts_unknown" if counts is None else
        "model_unknown" if type(model) is not str or re.fullmatch(r"[A-Za-z0-9._:/-]{1,256}", model) is None
            or "://" in model or looks_like_secret(model) else
        "tier_unknown" if tier is not None and (type(tier) is not str or re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", tier) is None
            or looks_like_secret(tier)) else None)
    if error:
        return usage | {"provider_receipt": None, "model_receipt_error": error}
    role = {key: binding["deployment"] if key == "model" else binding[key] for key in
        ("purpose", "provider", "model", "catalog_model", "api_mode", "max_output_tokens", "endpoint_host")}
    receipt = {"schema_version": 1, "authority": "observation_only", "requires_proposal_apply": True,
        "request_digest": request_digest,
        **{key: binding[key] for key in ("provider", "deployment", "catalog_model", "api_mode")},
        "model": model, "service_tier": tier, "input_tokens": counts["prompt_tokens"],
        "output_tokens": counts["completion_tokens"], "total_tokens": counts["total_tokens"],
        "cached_tokens": counts["cached_tokens"], "reasoning_tokens": counts["reasoning_tokens"],
        "role_metadata": role}
    output_error = field(result, "provider_output_error")
    if output_error is not missing and output_error is not None:
        if type(output_error) is not str or output_error != "invalid_output":
            return usage | {"provider_receipt": None, "model_receipt_error": "model_unknown"}
        usage["provider_output_error"] = output_error
    return usage | {"provider_receipt": receipt}


def _sync_hook(hook, result, usage, name):
    saved = hook(result, _primitive_copy(usage))
    if inspect.isawaitable(saved):
        if inspect.iscoroutine(saved):
            saved.close()
        raise TypeError(name + " must be synchronous in call_sync")


def model_call_key(*, workspace_id, venture_id, purpose, request_id, actor_id=None) -> str:
    """One canonical server subcall; provider/account/rate/body never identify it."""
    person_scope = workspace_id is None and venture_id is None
    if (not (person_scope and type(actor_id) is str and actor_id and actor_id.strip() == actor_id
            or type(workspace_id) is str and workspace_id and actor_id is None)
            or type(purpose) is not str or not purpose
            or type(request_id) is not str or not request_id or venture_id is not None and type(venture_id) is not str):
        raise CreditsUnavailable("model receipt identity is unavailable")
    value = [workspace_id, venture_id, purpose, request_id]
    if person_scope:
        value.append(actor_id)
    value = _primitive_copy(value)
    return "core-model:" + hashlib.sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()


def _same(left, right):
    return json.dumps(_primitive_copy(left), sort_keys=True) == json.dumps(_primitive_copy(right), sort_keys=True)


def _need(value):
    if not value:
        raise CreditsUnavailable("model receipt needs reconciliation")


def _read_model_event(session, key, phase, begin=None):
    from sqlalchemy import select
    from app.db.models import EventLog
    row = session.scalar(select(EventLog).where(EventLog.id == key + ":" + phase))
    if row is None:
        return None
    payload = _primitive_copy(json.loads(row.payload_json))
    usage = (begin or payload)["usage"]
    _need(row.topic == "credits.model.receipt." + phase and row.workspace_id == usage["workspace_id"]
        and row.venture_id == usage["venture_id"] and row.target_ref == (usage["reservation_id"] or key)
        and row.actor == "credits.model" and row.actor_id == "credits.model" and row.actor_kind == "system"
        and row.surface == "internal" and row.outcome == "ok"
        and row.on_behalf_of is None and row.approval_id is None and row.denied_reason is None
        and model_call_key(**{name: usage[name] for name in
            ("workspace_id", "venture_id", "purpose", "request_id")}, actor_id=usage.get("actor_id")) == key)
    if begin is not None:
        _need(all(name in payload["usage"] and _same(payload["usage"][name], value)
                  for name, value in begin["usage"].items()))
    return payload


def _append_model_event(session, key, phase, payload):
    from app.credits._txn import naive_utc
    from app.db.models import EventLog
    payload = _primitive_copy(payload)
    usage = payload["usage"]
    session.add(EventLog(id=key + ":" + phase, topic="credits.model.receipt." + phase,
        workspace_id=usage["workspace_id"], venture_id=usage["venture_id"],
        target_ref=usage["reservation_id"] or key, actor="credits.model", actor_id="credits.model",
        actor_kind="system", surface="internal", outcome="ok", ts=naive_utc(),
        payload_json=json.dumps(payload, sort_keys=True, allow_nan=False)))


def _model_custody(session, begin, *, admission=False):
    from app.credits.gate import _check_hold, _check_model_actor, validate_model_capture
    from app.db.models import CreditAccount, CreditReservation
    usage = begin["usage"]
    validate_model_capture(usage["model_binding"], usage["request_digest"],
        **{name: usage[name] for name in ("purpose", "provider", "model", "estimate_micro")},
        max_tokens=usage["token_ceiling"])
    account = session.get(CreditAccount, usage["account_id"]) if usage["account_id"] else None
    snapshot = {name: getattr(account, name) for name in
        ("id", "scope", "workspace_id", "venture_id", "owner_person_id")} if account is not None else None
    _need(_same(snapshot, begin["account_snapshot"]))
    if "actor_id" in usage:
        _need(usage["workspace_id"] is None and usage["venture_id"] is None)
        _check_model_actor(session, usage["actor_id"], account, admission=admission)
    hold = session.get(CreditReservation, usage["reservation_id"]) if usage["reservation_id"] else None
    if usage["source"] == "platform":
        _check_hold(hold, usage)
        snapshot = {name: getattr(hold, name) for name in ("parent_id", "is_pool", "remaining_micro")}
        snapshot.update(created_at=hold.created_at.isoformat(), expires_at=hold.expires_at.isoformat())
        _need(_same(snapshot, begin["hold_snapshot"]))
    else:
        _need(usage["source"] in {"byok", "free"} and hold is None and begin["hold_snapshot"] is None)
    return hold


def _model_session_begin(session, key):
    begin = _read_model_event(session, key, "begin")
    _need(begin is not None)
    _model_custody(session, begin)
    return begin


def _safe_model_usage(usage, original):
    """Validate normalized metadata without pricing unknown model/tier aliases."""
    from app.credits.provider_receipts import _RECEIPT_FIELDS
    from app.secrets.detect import looks_like_secret
    output_error = usage.get("provider_output_error")
    _need(output_error is None or type(output_error) is str and output_error == "invalid_output")
    if output_error is not None:
        original = original | {"provider_output_error": output_error}
    receipt = usage.get("provider_receipt")
    error = usage.get("model_receipt_error")
    if receipt is None:
        _need(error in {"counts_unknown", "model_unknown", "tier_unknown"})
        return original | {"provider_receipt": None, "model_receipt_error": error}
    valid = type(receipt) is dict and set(receipt) == _RECEIPT_FIELDS
    counts = [receipt.get(name) for name in
        ("input_tokens", "output_tokens", "total_tokens", "cached_tokens", "reasoning_tokens")] if type(receipt) is dict else []
    if (not valid or any(type(n) is not int or not 0 <= n < 2**63 for n in counts)
            or counts[2] != counts[0] + counts[1] or counts[3] > counts[0] or counts[4] > counts[1]):
        return original | {"provider_receipt": None, "model_receipt_error": "counts_unknown"}
    bound = original["model_binding"]
    role = {name: bound["deployment"] if name == "model" else bound[name] for name in
        ("purpose", "provider", "model", "catalog_model", "api_mode", "max_output_tokens", "endpoint_host")}
    valid = (type(receipt["schema_version"]) is int and receipt["schema_version"] == 1
        and type(receipt["authority"]) is str and receipt["authority"] in {"proposal_only", "observation_only"}
        and type(receipt["requires_proposal_apply"]) is bool
        and _same(receipt["request_digest"], original["request_digest"])
        and all(_same(receipt[name], bound[name]) for name in ("provider", "deployment", "catalog_model", "api_mode"))
        and _same(receipt["role_metadata"], role))
    for name in ("model", "service_tier"):
        value = receipt[name]
        pattern = r"[a-z][a-z0-9_-]{0,31}" if name == "service_tier" else r"[A-Za-z0-9._:/-]{1,256}"
        if not (value is None and name == "service_tier" or type(value) is str and
            re.fullmatch(pattern, value) is not None and "://" not in value and not looks_like_secret(value)):
            valid = False
    if not valid:
        return original | {"provider_receipt": None, "model_receipt_error": "model_unknown"}
    return original | {"provider_receipt": receipt}


def _model_lock_key(key):
    """Read only to choose the original account lock; validate again under it."""
    from app.credits._txn import serialize_writes, session_factory
    with session_factory()() as session:
        serialize_writes(session, key=key)
        begin = _read_model_event(session, key, "begin")
    return begin["usage"]["account_id"] or key if begin is not None else None


def assert_model_call_not_started(*, workspace_id, venture_id, purpose, request_id, actor_id=None):
    from app.credits._txn import serialize_writes, session_factory
    key = model_call_key(workspace_id=workspace_id, venture_id=venture_id, purpose=purpose,
                         request_id=request_id, actor_id=actor_id)
    with session_factory()() as session:
        serialize_writes(session, key=key)
        _need(_read_model_event(session, key, "begin") is None)


def begin_model_call(meter) -> str:
    """Own BEGIN and protection before a single dispatch; duplicates never grant I/O."""
    if getattr(meter, "local", False):
        _need(meter._admitted_model_call and not meter.outcome_protected and not meter.recorded)
        meter.protect_outcome()
        meter.model_journal_key = meter.trace_id
        return meter.trace_id
    from app.credits._txn import serialize_writes, session_factory
    from app.credits.gate import validate_model_capture
    from app.db.models import CreditAccount, CreditReservation
    _need(meter._admitted_model_call and not meter.outcome_protected and not meter.recorded)
    # A losing duplicate must never release a hold belonging to a live BEGIN.
    meter.outcome_protected = True
    usage = pending_model_receipt(meter)
    validate_model_capture(usage["model_binding"], usage["request_digest"],
        **{name: usage[name] for name in ("purpose", "provider", "model", "estimate_micro")},
        max_tokens=usage["token_ceiling"])
    key = model_call_key(**{name: usage[name] for name in ("workspace_id", "venture_id", "purpose", "request_id")},
                         actor_id=usage.get("actor_id"))
    lock = usage["account_id"] or key
    with session_factory()() as session:
        serialize_writes(session, key=lock)
        previous = _read_model_event(session, key, "begin")
        if previous is not None:
            raise CreditsUnavailable("model receipt needs reconciliation")
        account = session.get(CreditAccount, usage["account_id"]) if usage["account_id"] else None
        hold = session.get(CreditReservation, usage["reservation_id"]) if usage["reservation_id"] else None
        payload = {"usage": usage, "account_snapshot": {name: getattr(account, name) for name in
            ("id", "scope", "workspace_id", "venture_id", "owner_person_id")} if account is not None else None,
            "hold_snapshot": {name: getattr(hold, name) for name in ("parent_id", "is_pool", "remaining_micro")} if hold is not None else None}
        if hold is not None:
            payload["hold_snapshot"].update(created_at=hold.created_at.isoformat(), expires_at=hold.expires_at.isoformat())
        _model_custody(session, payload, admission=True)
        _need(hold is None and meter.passthrough or hold is not None and not meter.passthrough
              and hold.status in {"active", "open"})
        # An unknown BEGIN/protect ACK must never be auto-released or retried.
        meter.model_journal_key = key
        _append_model_event(session, key, "begin", payload)
        session.commit()
    meter.protect_outcome()
    with session_factory()() as session:
        serialize_writes(session, key=lock)
        saved = _model_session_begin(session, key)
        _need(_same(saved, payload) and _read_model_event(session, key, "calling", saved) is None)
        hold = _model_custody(session, saved, admission=True)
        _need(hold is None or hold.status == "reconciling")
        _append_model_event(session, key, "calling", {"usage": usage})
        session.commit()
    return key


def checkpoint_model_call(meter, usage: dict) -> None:
    """Append only safe original billing evidence, before any pricing operation."""
    if getattr(meter, "local", False):
        from app.credits import local_meter
        meter._model_receipt_checkpointed = True
        record_model_receipt(meter, usage)
        local_meter.finish(meter)
        return
    from app.credits._txn import serialize_writes, session_factory
    key = meter.model_journal_key
    _need(key is not None)
    usage = _primitive_copy(usage)
    allowed = set(pending_model_receipt(meter)) | {"provider_receipt", "model_receipt_error", "provider_output_error"}
    _need(set(usage) <= allowed and "provider_receipt" in usage
          and usage.get("model_receipt_error") in {None, "counts_unknown", "model_unknown", "tier_unknown"})
    with session_factory()() as session:
        serialize_writes(session, key=meter.account_id or key)
        begin = _model_session_begin(session, key)
        _need(_same(pending_model_receipt(meter), begin["usage"]))
        _need(all(name in usage and _same(usage[name], value) for name, value in begin["usage"].items()))
        usage = _safe_model_usage(usage, begin["usage"])
        _need(_read_model_event(session, key, "calling", begin) is not None
              and _read_model_event(session, key, "failure", begin) is None)
        previous = _read_model_event(session, key, "response", begin)
        payload = {"usage": usage}
        if previous is not None:
            _need(_same(previous, payload))
            meter._model_receipt_checkpointed = True
            return
        _append_model_event(session, key, "response", payload)
        session.commit()
    meter._model_receipt_checkpointed = True


def record_model_failure(meter, exc: Exception) -> None:
    """Only the shared definitive-rejection classifier can authorize release."""
    if getattr(meter, "local", False):
        from app.llm.provider_errors import definitive_provider_rejection

        if definitive_provider_rejection(exc):
            meter.confirm_unspent()
        else:
            _confirm_rejection(meter, exc)
        return
    from app.credits._txn import serialize_writes, session_factory
    from app.research.metered import _failure
    state, _ = _failure(exc)
    key = meter.model_journal_key
    _need(key is not None)
    usage = pending_model_receipt(meter)
    if state in {"failed", "retryable"}:
        usage["definitive_unspent"] = True
    payload = {"usage": usage, "outcome": "rejected" if usage.get("definitive_unspent") else "unknown"}
    with session_factory()() as session:
        serialize_writes(session, key=meter.account_id or key)
        begin = _model_session_begin(session, key)
        _need(_same(pending_model_receipt(meter), begin["usage"]))
        _need(_read_model_event(session, key, "calling", begin) is not None
              and _read_model_event(session, key, "response", begin) is None
              and _read_model_event(session, key, "failure", begin) is None)
        _append_model_event(session, key, "failure", payload)
        session.commit()
    if usage.get("definitive_unspent"):
        meter.confirm_unspent()


async def _transmit(messages, **kwargs):
    from llm_gateway import sole

    return await sole.transmit(messages, **kwargs)


async def transmit_hosted_chat_fallback(meter, messages, *, config, max_tokens):
    from llm_gateway import sole

    _need(pending_model_receipt(meter)["source"] == "platform" and meter.payer != "founder"
        and meter.purpose == "venture.chat.assistant"
        and (meter.provider == config.provider or meter.provider == "bedrock" and sole.fallback_needed())
        and meter.hold is not None and not meter.passthrough and meter.outcome_protected
        and not meter.recorded and not meter.definitive_failure)
    return await _transmit(messages, purpose=meter.purpose, workspace_id=meter.workspace_id,
        config=config, provider=meter.provider, tier=config.captured_tier, max_tokens=max_tokens,
        single_attempt=True, use_workspace_override=False, hosted_chat_fallback=True)


def absorb_hosted_chat_failure(meter) -> None:
    """Persist our liability before releasing the founder's ambiguous chat hold."""
    from sqlalchemy import select

    from app.credits._txn import naive_utc, serialize_writes, session_factory
    from app.credits.reserve import release
    from app.db.models import CreditReservation, EventLog

    usage = pending_model_receipt(meter)
    _need(usage["source"] == "platform" and usage["purpose"] == "venture.chat.assistant"
        and meter.payer != "founder" and meter.hold is not None and not meter.passthrough
        and meter.outcome_protected and not meter.recorded and not meter._model_receipt_checkpointed)
    key = meter.model_journal_key
    event_id = (key or "chat-model:" + meter.hold.reservation_id) + ":absorbed"
    with session_factory()() as session:
        serialize_writes(session, key=meter.account_id)
        row = session.get(CreditReservation, meter.hold.reservation_id)
        _need(row is not None and row.account_id == meter.account_id
            and row.workspace_id == meter.workspace_id and row.purpose == meter.purpose
            and row.amount_micro == meter.hold.amount_micro and row.status == "reconciling"
            and row.settled_micro is None)
        if key:
            begin = _model_session_begin(session, key)
            _need(_same(usage, begin["usage"]) and _read_model_event(session, key, "calling", begin) is not None
                and _read_model_event(session, key, "response", begin) is None)
        previous = session.scalar(select(EventLog).where(EventLog.id == event_id))
        payload = {"usage": usage, "outcome": "platform_absorbed"}
        if previous is not None:
            _need(_same(json.loads(previous.payload_json), payload))
        elif key:
            _append_model_event(session, key, "absorbed", payload)
        else:
            session.add(EventLog(id=event_id, topic="credits.model.receipt.absorbed",
                workspace_id=meter.workspace_id, venture_id=meter.venture_id,
                target_ref=meter.hold.reservation_id, actor="credits.model", actor_id="credits.model",
                actor_kind="system", surface="internal", outcome="ok", ts=naive_utc(),
                payload_json=json.dumps(payload, sort_keys=True, allow_nan=False)))
        session.commit()
    release(meter.hold, definitive=True)
    with session_factory()() as session:
        row = session.get(CreditReservation, meter.hold.reservation_id)
        _need(row is not None and row.status == "released" and row.settled_micro is None)
    # The founder's liability is definitive, not the provider's outcome.
    meter.definitive_failure = True


def recover_model_call(key: str) -> dict | None:
    """Same original money only: no reserve, HTTP, current quote or output body."""
    if is_local():
        from app.credits import local_meter
        _need(type(key) is str and re.fullmatch(r"core-model:[a-f0-9]{64}", key) is not None)
        return local_meter.recover(key)
    from app.credits._txn import serialize_writes, session_factory
    from app.credits.gate import Meter, _counts, _passthrough_ledger, project_model_receipt, reconcile_receipt
    _need(type(key) is str and re.fullmatch(r"core-model:[a-f0-9]{64}", key) is not None)
    lock = _model_lock_key(key)
    if lock is None:
        return None
    with session_factory()() as session:
        serialize_writes(session, key=lock)
        begin = _model_session_begin(session, key)
        _need(_read_model_event(session, key, "calling", begin) is not None)
        absorbed = _read_model_event(session, key, "absorbed", begin)
        if absorbed is not None:
            from app.credits.reserve import Hold, release

            usage = begin["usage"]
            _need(usage["source"] == "platform" and usage["purpose"] == "venture.chat.assistant"
                and absorbed["outcome"] == "platform_absorbed"
                and _read_model_event(session, key, "response", begin) is None)
            session.commit()
            release(Hold(reservation_id=usage["reservation_id"], account_id=usage["account_id"],
                amount_micro=usage["estimate_micro"]), definitive=True)
            return {"credits_micro": 0, "output_usable": False, "outcome": "platform_absorbed"}
        response = _read_model_event(session, key, "response", begin)
        failure = _read_model_event(session, key, "failure", begin)
        _need((response is None) != (failure is None))
        _need(failure is None or failure["outcome"] == "rejected" and failure["usage"].get("definitive_unspent") is True)
        saved = response or failure
        completed = _read_model_event(session, key, "completed", begin)
        session.commit()
    usage = project_model_receipt(saved["usage"]) if response is not None else saved["usage"]
    binding = {name: begin["usage"][name] for name in ("workspace_id", "venture_id", "request_id", "purpose",
        "provider", "model", "source", "account_id", "estimate_micro", "reservation_id", "task_invocation",
        "model_binding", "request_digest")}
    if response is not None and usage["source"] in {"byok", "free"} and usage["account_id"]:
        meter = Meter(hold=None, passthrough=True, **{name: usage[name] for name in
            ("purpose", "model", "provider", "workspace_id", "venture_id", "account_id", "request_id", "payer",
             "estimate_micro", "source", "task_invocation", "model_binding", "request_digest", "token_ceiling")},
            **({"actor_id": usage["actor_id"]} if "actor_id" in usage else {}))
        meter._model_receipt_checkpointed = True
        record_model_receipt(meter, usage)
        _passthrough_ledger(meter, usage, _counts(usage, required=True))
    charged = reconcile_receipt(usage, binding)
    _need(type(charged) is int and charged >= 0)
    result = {"credits_micro": charged, "output_usable": response is not None and not usage["billing_output_error"],
              "outcome": "confirmed" if response is not None else "rejected"}
    payload = {"usage": usage, "result": result}
    with session_factory()() as session:
        serialize_writes(session, key=lock)
        current = _model_session_begin(session, key)
        _need(_same(current, begin) and _same(_read_model_event(session, key, "response" if response is not None else "failure", current), saved))
        previous = _read_model_event(session, key, "completed", current)
        _need(_same(previous, completed))
        if previous is not None:
            _need(_same(previous, payload))
        else:
            _append_model_event(session, key, "completed", payload)
            session.commit()
    return result


async def call(callback, *, purpose: str, estimate_micro: int,
               workspace_id: str | None = None, venture_id: str | None = None,
               account_id: str | None = None, provider: str | None = None,
               model: str | None = None, request_id: str | None = None,
               payer: str | None = None, actual_micro=None, max_tokens: int = 0,
               before_meter_exit=None, model_binding: dict | None = None,
               request_digest: str | None = None, before_model_receipt=None, actor_id: str | None = None):
    """Reserve, call once, settle known usage; uncertain outcomes retain holds."""
    if type(estimate_micro) is not int or estimate_micro < (0 if is_local() else 1):
        raise CreditsUnavailable("a paid call requires a positive reservation")
    if model_binding is not None or request_digest is not None:
        if actual_micro is not None or not callable(before_model_receipt):
            raise CreditsUnavailable("a frozen model call requires a receipt checkpoint")
    async with charge(**_target(workspace_id, venture_id, account_id), purpose=purpose,
                      estimate_micro=estimate_micro, provider=provider, model=model,
                      request_id=request_id, payer=payer, max_tokens=max_tokens,
                      model_binding=model_binding, request_digest=request_digest,
                      **({"actor_id": actor_id} if actor_id is not None else {})) as meter:
        if not is_local() and estimate_micro and payer != "founder" and (meter.passthrough or meter.hold is None):
            raise CreditsUnavailable()
        if model_binding is not None:
            begin_model_call(meter)
        else:
            meter.protect_outcome()
        try:
            result = await callback()
        except Exception as exc:
            if model_binding is not None:
                record_model_failure(meter, exc)
            else:
                _confirm_rejection(meter, exc)
            raise
        if model_binding is not None:
            usage = capture_model_receipt(meter, result)
            checkpoint_model_call(meter, usage)
            saved = before_model_receipt(result, _primitive_copy(usage))
            if inspect.isawaitable(saved):
                await saved
            record_model_receipt(meter, usage)
            if meter.billing_output_error:
                raise CreditsUnavailable("provider output is unusable")
        else:
            _record(meter, result, estimate_micro, actual_micro)
        if before_meter_exit is not None:
            saved = before_meter_exit(result, captured_receipt(meter))
            if inspect.isawaitable(saved):
                await saved
    if getattr(meter, "task_usage_failed", False) or (not is_local() and payer != "founder" and meter.settle_result is None):
        raise CreditsUnavailable("credit usage needs reconciliation")
    return result


def call_sync(callback, *, purpose: str, estimate_micro: int,
              workspace_id: str | None = None, venture_id: str | None = None,
              account_id: str | None = None, provider: str | None = None,
              model: str | None = None, request_id: str | None = None,
              payer: str | None = None, actual_micro=None, max_tokens: int = 0,
              before_meter_exit=None, model_binding: dict | None = None,
              request_digest: str | None = None, before_model_receipt=None, actor_id: str | None = None,
              validate_output=None):
    """Synchronous counterpart of ``call`` with the same credit semantics."""
    if type(estimate_micro) is not int or estimate_micro < (0 if is_local() else 1):
        raise CreditsUnavailable("a paid call requires a positive reservation")
    if model_binding is not None or request_digest is not None:
        if actual_micro is not None or not callable(before_model_receipt):
            raise CreditsUnavailable("a frozen model call requires a receipt checkpoint")
    with charge_sync(**_target(workspace_id, venture_id, account_id), purpose=purpose,
                     estimate_micro=estimate_micro, provider=provider, model=model,
                     request_id=request_id, payer=payer, max_tokens=max_tokens,
                     model_binding=model_binding, request_digest=request_digest,
                     **({"actor_id": actor_id} if actor_id is not None else {})) as meter:
        if not is_local() and estimate_micro and payer != "founder" and (meter.passthrough or meter.hold is None):
            raise CreditsUnavailable()
        if model_binding is not None:
            begin_model_call(meter)
        else:
            meter.protect_outcome()
        try:
            result = callback()
        except Exception as exc:
            if model_binding is not None:
                record_model_failure(meter, exc)
            else:
                _confirm_rejection(meter, exc)
            raise
        if model_binding is not None:
            usage = capture_model_receipt(meter, result)
            checkpoint_model_call(meter, usage)
            _sync_hook(before_model_receipt, result, usage, "before_model_receipt")
            record_model_receipt(meter, usage)
            if meter.billing_output_error:
                raise CreditsUnavailable("provider output is unusable")
        else:
            _record(meter, result, estimate_micro, actual_micro)
        if before_meter_exit is not None:
            saved = before_meter_exit(result, captured_receipt(meter))
            if inspect.isawaitable(saved):
                if inspect.iscoroutine(saved):
                    saved.close()
                raise TypeError("before_meter_exit must be synchronous in call_sync")
    if getattr(meter, "task_usage_failed", False) or (not is_local() and payer != "founder" and meter.settle_result is None):
        raise CreditsUnavailable("credit usage needs reconciliation")
    return result


async def llm(messages: list, *, purpose: str, workspace_id: str | None = None,
              venture_id: str | None = None, max_tokens: int = 4096,
              tier: str = "pro", request_id: str | None = None, before_meter_exit=None, **kwargs):
    """One gateway call priced by the configured model and confirmed usage."""
    if set(kwargs) & {"config", "provider", "single_attempt", "use_workspace_override", "passthrough_model"}:
        raise ValueError("metered LLM routing is resolved by the shared wrapper")
    from app.credits.estimate import actual_llm_micro, count_prompt_tokens, estimate_llm_micro
    from app.llm import founder_provider
    from llm_gateway import sole
    from llm_gateway.provider_foundry import load_provider_config_from_env, resolve_live_model
    from llm_gateway.usage import extract_confirmed_token_usage

    from app.tenancy import current_or_none
    scope = current_or_none()
    wid = workspace_id or sole.get_active_workspace_id() or (scope.workspace_id if scope else None)
    if (not wid or wid == "none") and venture_id:
        from app.workspaces.resolve import workspace_for_venture
        workspace = workspace_for_venture(venture_id)
        wid = workspace.id if workspace else None
    if not wid:
        if is_local():
            raise CreditsUnavailable("Open your workspace, then try this task again.")
        raise CreditsUnavailable()
    if is_local():
        byok = True
        try:
            resolved = founder_provider.resolve_llm_config(wid)
            config = founder_provider.to_provider_config(wid, resolved_config=resolved)
        except founder_provider.FounderLLMConfigError:
            raise CreditsUnavailable("Open Keys and connections and save your model provider's API key "
                "and model choices, then try this task again.") from None
    else:
        founder_provider.register_workspace_llm_resolver()
        resolved = founder_provider.resolve_llm_config(wid)
        byok = resolved.source == "founder_override"
        config = founder_provider.to_provider_config(wid, resolved_config=resolved) if byok else load_provider_config_from_env()
    if config is None:
        if is_local():
            raise CreditsUnavailable("Open Keys and connections and save your model provider's API key "
                "and model choices, then try this task again.")
        raise CreditsUnavailable()
    from llm_gateway.routing import resolve_purpose_config, sol_output_cap
    priced_config = resolve_purpose_config(purpose, config, tier=sole._resolved_tier(purpose, tier))
    model = resolve_live_model(priced_config, tier=priced_config.captured_tier)
    # Reasoning shares this budget with the answer: a low cap returns an empty reply that is still billed.
    max_tokens = sol_output_cap(priced_config, max_tokens)
    from app.credits.gate import _call_allowance
    prompt_tokens = sole.transport_prompt_tokens(messages, purpose=purpose, config=config,
        tools=kwargs.get("tools"), response_schema=kwargs.get("response_schema"), clock_text=" " * 256
    ) if _call_allowance.get() is not None else count_prompt_tokens(messages)
    if _call_allowance.get() is not None:
        prompt_tokens += len(json.dumps(kwargs, ensure_ascii=False, allow_nan=False).encode())
    free_local = founder_provider.keyless_local_config(priced_config)
    estimate = 0 if free_local else estimate_llm_micro(model, prompt_tokens=prompt_tokens, max_tokens=max_tokens)

    def actual(result):
        counts = extract_confirmed_token_usage(getattr(result, "usage", None))
        if counts is None:
            raise CreditsUnavailable("provider usage needs reconciliation")
        if free_local:
            return 0
        return actual_llm_micro(model, prompt_tokens=counts["prompt_tokens"], completion_tokens=counts["completion_tokens"],
                                cached_tokens=counts["cached_tokens"])

    return await call(lambda: _transmit(messages, purpose=purpose, workspace_id=wid,
                      tier=tier, max_tokens=max_tokens, config=config, single_attempt=True, **kwargs),
                      purpose=purpose, workspace_id=wid, venture_id=venture_id,
                      provider=config.provider, model=model, estimate_micro=estimate,
                      request_id=request_id, payer="founder" if byok else None, actual_micro=actual,
                      max_tokens=prompt_tokens + max_tokens, before_meter_exit=before_meter_exit)


async def search(provider, query: str, *, purpose: str = "research.search",
                 workspace_id: str | None = None, venture_id: str | None = None,
                 request_id: str | None = None):
    """Owner integration seam for a provider's fixed-price basic search call.

    Call this only when the provider transport does not already meter itself.
    Durable research workflows keep their journaled ``perform`` wrapper.
    """
    name = getattr(provider, "name", "")
    if name == "openworker":
        # The bridge may silently fall back to DuckDuckGo when a paid key is
        # missing. Its owner must meter inside the confirmed provider transport.
        if os.environ.get("OPENWORKER_SEARCH_PROVIDER", "duckduckgo").lower() != "duckduckgo":
            raise CreditsUnavailable("search provider pricing needs confirmation")
        name = "duckduckgo"
    if name in {"local_stub", "duckduckgo"} or is_local() and name == "builtin":
        if is_local():
            return await call(lambda: provider.search(query), purpose=purpose, estimate_micro=0, provider=name,
                workspace_id=workspace_id, venture_id=venture_id, request_id=request_id)
        return await provider.search(query)
    if name not in {"firecrawl", "exa", "tavily"}:
        raise CreditsUnavailable("search provider pricing is not available")
    from app.tenancy import current_or_none

    scope = current_or_none()
    wid = workspace_id or (scope.workspace_id if scope else None)
    vid = venture_id or (scope.venture_id if scope else None)
    batches = max(1, (getattr(provider, "_limit", 6) + 9) // 10) if name == "firecrawl" else 1
    estimate = price_micro(name + "_search", batches)
    return await call(lambda: provider.search(query), purpose=purpose, provider=name,
                      workspace_id=wid, venture_id=vid, estimate_micro=estimate, request_id=request_id)

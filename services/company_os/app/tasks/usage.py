"""Task budgets at the metered call boundary, independent of wallet settlement."""
import hashlib
import json
import re
from contextlib import contextmanager
from contextvars import ContextVar

from sqlalchemy import select

from core.ids import new_id
from app.db.session import serialize_writes
from app.db.task_models import Task, TaskEvent, TaskRun
from app.state import db
from app.tasks.contracts import blocker
from app.tasks import service
from app.tasks.service import block, event, guard, job_step

_CONTEXT: ContextVar[tuple[str, str] | None] = ContextVar("task_usage", default=None)
CALL_BINDING_FIELDS = frozenset({"schema_version", "purpose", "provider", "model", "catalog_model", "request_id",
    "captured_tier", "api_mode", "max_output_tokens", "reasoning_effort"})


def _call_binding(value):
    """Closed, credential-free trusted capture; never resolve a route here."""
    from app.secrets.detect import looks_like_secret
    if (not isinstance(value, dict) or set(value) != CALL_BINDING_FIELDS
            or type(value["schema_version"]) is not int or value["schema_version"] != 1):
        raise ValueError("task_call_binding_invalid")
    for key, maximum in (("purpose", 80), ("provider", 80), ("model", 200), ("catalog_model", 200), ("request_id", 200)):
        item = value[key]
        if item is None and key != "purpose":
            continue
        if (not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1," + str(maximum) + "}", item)
                or "://" in item or looks_like_secret(item)):
            raise ValueError("task_call_binding_invalid")
    for key, allowed in (("captured_tier", {"pro", "flash", "embed", "local", "passthrough"}),
            ("api_mode", {"chat_completions", "responses"}),
            ("reasoning_effort", {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"})):
        if value[key] is not None and (not isinstance(value[key], str) or value[key] not in allowed):
            raise ValueError("task_call_binding_invalid")
    if value["max_output_tokens"] is not None and (type(value["max_output_tokens"]) is not int
            or not 1 <= value["max_output_tokens"] <= 2_000_000):
        raise ValueError("task_call_binding_invalid")
    llm = any(value[key] is not None for key in ("model", "catalog_model", "captured_tier", "api_mode", "max_output_tokens", "reasoning_effort"))
    if llm and any(value[key] is None for key in ("provider", "model", "catalog_model", "captured_tier", "api_mode", "max_output_tokens")):
        raise ValueError("task_call_binding_invalid")
    return dict(value)


@contextmanager
def job_context(job_id, *, kind=None):
    from app.tasks.skills import get_skill
    from app.tasks import goals
    context = None
    if kind is None or kind in {"tasks.execute", "tasks.verify"} or get_skill(kind) is not None or goals.is_action(kind):
        with db()() as session:
            _, run, step = job_step(session, job_id)
            context = (run.id, step.id) if run and step and (kind is None or run.run_key != job_id) else None
    token = _CONTEXT.set(context)
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def before_call(estimate_micro: int, max_tokens: int = 0, *, call_binding=None) -> str | None:
    context = _CONTEXT.get()
    if context is None:
        return None
    if type(estimate_micro) is not int or type(max_tokens) is not int or estimate_micro < 0 or max_tokens < 0:
        raise ValueError("task_usage_invalid")
    capture = {"call_binding": _call_binding(call_binding)} if call_binding is not None else None
    if (capture is not None and capture["call_binding"]["max_output_tokens"] is not None
            and max_tokens < capture["call_binding"]["max_output_tokens"]):
        raise ValueError("task_call_binding_invalid")
    run_id, step_id = context
    invocation = new_id("tcl")
    with db()() as session:
        from app.tasks.locks import lock_step
        task, run, _ = lock_step(session, step_id)
        serialize_writes(session, key="task-budget:" + run_id)
        from app.core.jobs import FatalError
        try:
            _reserve(session, task, run, step_id, invocation, estimate_micro, max_tokens, data=capture)
        except FatalError as error:
            if str(error) in {"task_scope_changed", "task_budget_exhausted"}:
                session.commit()
            raise
        session.commit()
    return invocation


def _admission_guard(session, task, run, **counters):
    """Caller owns the locked Task transaction, including any refusal blocker."""
    from app.core.jobs import FatalError
    if task.state in service.TERMINAL | service.WAITING or run.state in service.TERMINAL | service.WAITING:
        raise FatalError("task_paused" if task.state == "paused" else "task_waiting")
    if not service.active_scope(session, task):
        block(session, task, service.scope_blocker(), run=run)
        raise FatalError("task_scope_changed")
    if not guard(session, task, run, **counters):
        block(session, task, blocker("budget_exhausted"), run=run)
        raise FatalError("task_budget_exhausted")


def _reserve(session, task, run, step_id, invocation, estimate, tokens, *, data=None):
    _admission_guard(session, task, run, cost_micro=estimate, tokens=tokens)
    run.reserved_cost_micro += estimate
    run.reserved_tokens += tokens
    event(session, task, "call_reserved", "This step is within its agreed limit.", run_id=run.id,
          step_id=step_id, data={"invocation": invocation, "cost_micro": estimate, "tokens": tokens, **(data or {})})


def after_call(invocation: str | None, actual_micro: int, tokens_used: int = 0):
    if invocation is None:
        return
    if type(actual_micro) is not int or type(tokens_used) is not int or actual_micro < 0 or tokens_used < 0:
        raise ValueError("task_usage_invalid")
    _settle(invocation, actual_micro, tokens_used, definitive=True)


def abort_call(invocation: str | None, *, definitive: bool):
    if invocation is not None:
        _settle(invocation, 0, 0, definitive=definitive)


def _settle(invocation, cost, tokens, *, definitive):
    with db()() as session:
        reservation = session.scalar(select(TaskEvent).where(TaskEvent.kind == "call_reserved",
            TaskEvent.data_json.contains('"invocation": ' + json.dumps(invocation))))
        if not reservation:
            raise ValueError("task_call_reservation_missing")
        from app.tasks.locks import lock_run
        task, run = lock_run(session, reservation.run_id)
        serialize_writes(session, key="task-budget:" + reservation.run_id)
        if json.loads(reservation.data_json).get("receipt_kind") == "device_order":
            raise ValueError("task_native_receipt_required")
        if not _settle_reserved(session, task, run, reservation, cost, tokens, definitive=definitive):
            return
        session.commit()


def _call_events(session, invocation, kinds):
    # Native callers can reserve and replay before flushing their surrounding
    # order transaction (the application factory has autoflush disabled).
    with session.no_autoflush:
        rows = list(session.scalars(select(TaskEvent).where(TaskEvent.kind.in_(kinds),
            TaskEvent.data_json.contains('"invocation": ' + json.dumps(invocation)))))
    rows += [row for row in session.new if isinstance(row, TaskEvent) and row.kind in kinds]
    return [row for row in rows if json.loads(row.data_json).get("invocation") == invocation]


def _settle_reserved(session, task, run, reservation, cost, tokens, *, definitive, data=None, strict=False):
    reserved = json.loads(reservation.data_json)
    invocation = reserved["invocation"]
    result = {"invocation": invocation, **({"cost_micro": cost, "tokens": tokens} if definitive else {}), **(data or {})}
    prior = [row for row in _call_events(session, invocation,
        ("call_settled",) if definitive else ("call_settled", "call_uncertain")) if row.run_id == reservation.run_id]
    if prior:
        if strict and definitive and (len(prior) != 1 or json.loads(prior[0].data_json) != result):
            raise ValueError("task_native_usage_conflict")
        return False
    active = task.state not in service.TERMINAL | service.WAITING and run.state not in service.TERMINAL | service.WAITING
    reason = json.loads(task.blocker_json or "{}")
    own_unknown = (task.state == run.state == "input_required"
        and all(reason.get(key) == value for key, value in blocker("outcome_uncertain").items()))
    if definitive:
        run.reserved_cost_micro -= reserved["cost_micro"]
        run.reserved_tokens -= reserved["tokens"]
        run.cost_micro += cost
        run.tokens_used += tokens
        event(session, task, "call_settled", "Usage was confirmed for this step.", run_id=run.id,
              step_id=reservation.step_id, data=result)
        from app.tasks.budgets import effective_limits
        limits = effective_limits(task, run)
        if (active or own_unknown) and (run.cost_micro > limits.cost_micro or run.tokens_used > limits.tokens):
            block(session, task, blocker("budget_exhausted"), run=run)
    else:
        event(session, task, "call_uncertain", "The provider result needs reconciliation before another call.",
              run_id=run.id, step_id=reservation.step_id, data=result)
        if active:
            block(session, task, blocker("outcome_uncertain"), run=run)
    return True


def _native_call(session, *, task_id, run_id, step_id, order_id, execution_id, review_digest,
                 invocation_id, request_digest):
    from app.tasks.locks import lock_step
    identifiers = (task_id, run_id, step_id, order_id, execution_id, invocation_id)
    if (any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value) for value in identifiers)
            or any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value) for value in (review_digest, request_digest))):
        raise ValueError("task_native_binding_invalid")
    task, run, step = lock_step(session, step_id)
    binding = {"task_id": task_id, "run_id": run_id, "step_id": step_id, "order_id": order_id,
               "execution_id": execution_id, "review_digest": review_digest, "invocation_id": invocation_id,
               "request_digest": request_digest}
    try:
        inputs = json.loads(step.inputs_json) if step else None
    except (TypeError, ValueError):
        inputs = None
    if (not task or not run or not step or (task.id, run.id) != (task_id, run_id)
            or step.runs_on not in {"device", "either"} or not isinstance(inputs, dict)
            or any(inputs.get(key) != value for key, value in (
                ("device_order_id", order_id), ("execution_id", execution_id), ("review_digest", review_digest)))):
        raise ValueError("task_native_binding_invalid")
    invocation = "native:" + hashlib.sha256(json.dumps([order_id, invocation_id], separators=(",", ":")).encode()).hexdigest()
    return task, run, step, invocation, {"receipt_kind": "device_order", "native_binding": binding}


def reserve_native_call(session, *, estimate_micro, max_tokens, **binding):
    """Trusted transport admission; no commit, provider call or wallet hold.

    The native adapter must lock Task → Run → Step before account/order locks,
    validate the canonical order and supply its immutable binding. ``max_tokens``
    includes the captured prompt and maximum output; it is not a client estimate.
    """
    if any(type(value) is not int or value < 0 for value in (estimate_micro, max_tokens)):
        raise ValueError("task_usage_invalid")
    task, run, step, invocation, data = _native_call(session, **binding)
    prior = _call_events(session, invocation, ("call_reserved",))
    expected = {"invocation": invocation, "cost_micro": estimate_micro, "tokens": max_tokens, **data}
    if prior:
        if (len(prior) != 1 or (prior[0].task_id, prior[0].run_id, prior[0].step_id) != (task.id, run.id, step.id)
                or json.loads(prior[0].data_json) != expected):
            raise ValueError("task_native_usage_conflict")
        return {"invocation": invocation, "admitted_now": False}
    _reserve(session, task, run, step.id, invocation, estimate_micro, max_tokens, data=data)
    return {"invocation": invocation, "admitted_now": True}


def settle_native_call(session, *, actual_micro, prompt_tokens=None, completion_tokens=None, cached_tokens=0, **binding):
    """Receipt-only accounting survives revocation; unknown usage keeps caps held.

    ``actual_micro`` is raw provider usage, never the clamped wallet charge.
    Only source-owned native receipts may supply these values. Cached input is
    a subset of the full prompt, so total usage is prompt + completion once.
    """
    if (any(value is not None and (type(value) is not int or value < 0)
            for value in (actual_micro, prompt_tokens, completion_tokens))
            or type(cached_tokens) is not int or cached_tokens < 0
            or prompt_tokens is None and cached_tokens > 0
            or prompt_tokens is not None and cached_tokens > prompt_tokens):
        raise ValueError("task_usage_invalid")
    task, run, step, invocation, data = _native_call(session, **binding)
    reservations = _call_events(session, invocation, ("call_reserved",))
    if not reservations:
        raise ValueError("task_call_reservation_missing")
    if (len(reservations) != 1 or (reservations[0].task_id, reservations[0].run_id, reservations[0].step_id)
            != (task.id, run.id, step.id)
            or any(json.loads(reservations[0].data_json).get(key) != value for key, value in data.items())):
        raise ValueError("task_native_usage_conflict")
    definitive = all(value is not None for value in (actual_micro, prompt_tokens, completion_tokens))
    if definitive:
        data.update(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, cached_tokens=cached_tokens)
    return _settle_reserved(session, task, run, reservations[0], actual_micro if definitive else 0,
        prompt_tokens + completion_tokens if definitive else 0, definitive=definitive, data=data, strict=True)


def mark_native_usage_deferred(session, **binding):
    """Original signed native receipt only; no accounting, commit or authority.

    Callers isolate this optional Task stop from canonical financial cleanup.
    A damaged budget or missing reservation does not grant another call, and
    unrelated waiting/terminal controls retain their original reason.
    """
    task, run, step, _, _ = _native_call(session, **binding)
    if task.state in service.TERMINAL | service.WAITING or run.state in service.TERMINAL | service.WAITING:
        return False
    from app.tasks.device import _reconciliation
    _reconciliation(session, task, run, step)
    return True


def reserve_native_tool(session, *, effect_digest, observation_digest, **binding):
    """Count a source-authorized effect once; this grants no native effect.

    The canonical native effect admission supplies the reviewed candidate and
    current observation digests before writing its effect, in this transaction.
    Provider calls do not use this helper; zero-provider effects do.
    """
    if any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value)
           for value in (effect_digest, observation_digest)):
        raise ValueError("task_native_binding_invalid")
    task, run, step, invocation, data = _native_call(session, **binding)
    expected = {"invocation": invocation, "effect_digest": effect_digest, "observation_digest": observation_digest,
                "tool_calls": 1, **data}
    prior = _call_events(session, invocation, ("native_tool_admitted",))
    if prior:
        if (len(prior) != 1 or (prior[0].task_id, prior[0].run_id, prior[0].step_id) != (task.id, run.id, step.id)
                or json.loads(prior[0].data_json) != expected):
            raise ValueError("task_native_usage_conflict")
        return {"invocation": invocation, "admitted_now": False}
    _admission_guard(session, task, run, tool_calls=1)
    run.tool_calls += 1
    event(session, task, "native_tool_admitted", "Your Mac is within this step's agreed action limit.",
          run_id=run.id, step_id=step.id, data=expected)
    return {"invocation": invocation, "admitted_now": True}


def before_tool():
    context = _CONTEXT.get()
    if context is None:
        return
    with db()() as session:
        from app.tasks.locks import lock_step
        task, run, _ = lock_step(session, context[1])
        serialize_writes(session, key="task-budget:" + context[0])
        from app.core.jobs import FatalError
        try:
            _admission_guard(session, task, run, tool_calls=1)
        except FatalError as error:
            if str(error) in {"task_scope_changed", "task_budget_exhausted"}:
                session.commit()
            raise
        run.tool_calls += 1
        session.commit()


@contextmanager
def run_context(run_id, *, step_id=None):
    """Verifier attribution comes from a DB-owned run, never a caller's identity."""
    from app.db.task_models import TaskStep
    with db()() as session:
        step = (session.get(TaskStep, step_id) if step_id is not None else
                session.scalar(select(TaskStep).where(TaskStep.run_id == run_id).order_by(TaskStep.position.desc()).limit(1)))
        if not step:
            raise ValueError("task_run_steps_missing")
        if step.run_id != run_id:
            raise ValueError("task_run_step_mismatch")
    token = _CONTEXT.set((run_id, step.id))
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def reconcile_usage(limit=100):
    """Recover task usage from S02's immutable, scoped spend receipts.

    Run outside the scheduler transaction: ``after_call`` owns its transaction.
    A release/expiry or missing token receipt is never proof of zero usage.
    """
    from datetime import timedelta
    from sqlalchemy import cast, exists, func
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.orm import aliased
    from app.credits._txn import naive_utc
    from app.core.jobs import STALE_LOCK_S
    from app.db.models import CreditAccount, CreditLedgerEntry, CreditReservation, Job, Venture, Workspace
    from app.db.task_models import TaskStep
    from app.tasks.billing import UNSUPPORTED_RECEIPT_FIELDS

    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("task_usage_reconcile_limit_invalid")
    factory = db()
    actions = []
    with factory() as session:
        settled = aliased(TaskEvent)
        def invocation_value(row):
            return (cast(row.data_json, JSONB)["invocation"].astext
                    if session.get_bind().dialect.name == "postgresql"
                    else func.json_extract(row.data_json, "$.invocation"))
        complete = exists(select(settled.id).where(settled.kind == "call_settled",
            settled.run_id == TaskEvent.run_id, invocation_value(settled) == invocation_value(TaskEvent)))
        has_spend = exists(select(CreditLedgerEntry.id).join(CreditReservation,
            CreditReservation.id == CreditLedgerEntry.reservation_id).where(CreditLedgerEntry.kind == "spend",
            CreditReservation.status == "settled", CreditReservation.is_pool.is_(False),
            CreditReservation.task_invocation == invocation_value(TaskEvent)))
        receipt_kind = (cast(TaskEvent.data_json, JSONB)["receipt_kind"].astext
                        if session.get_bind().dialect.name == "postgresql"
                        else func.json_extract(TaskEvent.data_json, "$.receipt_kind"))
        # Receipted calls come first. Already-settled rows cannot starve later
        # reservations, and linking an otherwise-settled run is crash-safe too.
        pending = session.scalars(select(TaskEvent).join(TaskRun, TaskRun.id == TaskEvent.run_id).where(
            TaskEvent.kind == "call_reserved", func.coalesce(receipt_kind, "") != "device_order",
            (~complete | (TaskRun.wallet_account_id.is_(None) & has_spend)))
            .order_by(has_spend.desc(), TaskEvent.created_at, TaskEvent.id).limit(limit)).all()
        for reserved_event in pending:
            invocation, scope_verified = None, False
            try:
                reserved = json.loads(reserved_event.data_json)
                invocation = reserved.get("invocation")
                if (not isinstance(invocation, str) or not 1 <= len(invocation) <= 128
                        or type(reserved.get("cost_micro")) is not int or reserved["cost_micro"] < 0
                        or type(reserved.get("tokens")) is not int or reserved["tokens"] < 0):
                    continue
                run = session.get(TaskRun, reserved_event.run_id)
                task = session.get(Task, run.task_id) if run else None
                step = session.get(TaskStep, reserved_event.step_id) if reserved_event.step_id else None
                if (not task or reserved_event.task_id != task.id
                        or (reserved_event.step_id and (not step or step.run_id != run.id))):
                    continue
                scope_verified = True
                duplicate = session.scalars(select(TaskEvent.id).where(TaskEvent.kind == "call_reserved",
                    invocation_value(TaskEvent) == invocation)).all()
                if len(duplicate) != 1:
                    actions.append((invocation, None))
                    continue
                workspace = session.get(Workspace, task.workspace_id)
                venture = session.get(Venture, task.venture_id) if task.venture_id else None
                if (not workspace or workspace.owner_id != task.owner_person_id
                        or (task.venture_id and (not venture or venture.workspace_id != task.workspace_id))):
                    actions.append((invocation, None))
                    continue
                ledger = []
                candidates = session.scalars(select(CreditLedgerEntry).where(CreditLedgerEntry.kind == "spend",
                    CreditLedgerEntry.meta_json.contains(invocation, autoescape=True)).order_by(CreditLedgerEntry.seq.desc()).limit(20))
                for entry in candidates:
                    meta = json.loads(entry.meta_json)
                    if isinstance(meta, dict) and meta.get("task_invocation") == invocation:
                        ledger.append((entry, meta))
                proof = None
                if len(ledger) == 1:
                    entry, meta = ledger[0]
                    account = session.get(CreditAccount, entry.account_id)
                    hold = session.get(CreditReservation, entry.reservation_id) if entry.reservation_id else None
                    owner_matches = account and account.owner_person_id == task.owner_person_id
                    account_matches = owner_matches and (account.scope == "person" or account.scope == "venture"
                        and (account.workspace_id, account.venture_id) == (task.workspace_id, task.venture_id))
                    if (account_matches and hold and not hold.is_pool and hold.status == "settled"
                            and hold.account_id == entry.account_id and hold.workspace_id == task.workspace_id
                            and (meta.get("workspace_id"), meta.get("venture_id")) == (task.workspace_id, task.venture_id)
                            and (entry.request_id, entry.purpose, entry.model, entry.provider)
                                == (hold.request_id, hold.purpose, hold.model, hold.provider)
                            and getattr(hold, "task_invocation", invocation) == invocation
                            and getattr(hold, "venture_id", task.venture_id) == task.venture_id
                            and run.wallet_account_id in {None, entry.account_id}
                            and type(entry.amount_micro) is int and entry.amount_micro <= 0
                            and hold.settled_micro == -entry.amount_micro
                            and type(hold.amount_micro) is int and hold.amount_micro > 0
                            and type(hold.settled_micro) is int
                            # Exact S02 reserve.settle allowance: funded 5%,
                            # capped at 10,000 micro. A hold is an estimate.
                            and 0 <= hold.settled_micro <= hold.amount_micro + min(10000, max(1, hold.amount_micro // 20))
                            and type(entry.prompt_tokens) is int and entry.prompt_tokens >= 0
                            and type(entry.completion_tokens) is int and entry.completion_tokens >= 0
                            and not any(UNSUPPORTED_RECEIPT_FIELDS.intersection(value) for value in (reserved, meta))):
                        if "raw_provider_actual_micro" in meta:
                            actual = meta["raw_provider_actual_micro"]
                        elif "provider_actual_micro" in meta:
                            actual = meta["provider_actual_micro"]
                        else:
                            actual = -entry.amount_micro if hold.clamped == 0 else None
                        if type(actual) is int and actual >= 0:
                            proof = {"run_id": run.id, "account_id": entry.account_id, "cost": actual,
                                     "tokens": entry.prompt_tokens + entry.completion_tokens, "ledger_id": entry.id}
                if proof is None and not ledger:
                    # A live worker is still awaiting a receipt, not an unknown
                    # outcome. Do not interrupt an active metered call.
                    evaluation = json.loads(run.evaluation_json or "{}")
                    live_ids = {identifier for identifier in (
                        evaluation.get("job_id") if evaluation.get("state") == "calling" else None,
                        step.job_id if step else None, getattr(run, "control_job_id", None)) if identifier}
                    live = False
                    for job_id in live_ids:
                        job = session.get(Job, job_id)
                        bound = job and (step and job.id == step.job_id or json.loads(job.payload_json).get("task_run_id") == run.id)
                        live |= bool(bound and (job.workspace_id, job.venture_id) == (task.workspace_id, task.venture_id)
                            and job.status in {"running", "leased"} and job.locked_at
                            and naive_utc(job.locked_at) > naive_utc() - timedelta(seconds=STALE_LOCK_S))
                    if live:
                        continue
                actions.append((invocation, proof))
            except (AttributeError, KeyError, TypeError, ValueError):
                if scope_verified:
                    actions.append((invocation, None))
    recovered = 0
    for invocation, proof in actions:
        if proof is None:
            abort_call(invocation, definitive=False)
            continue
        after_call(invocation, proof["cost"], proof["tokens"])
        with factory() as session:
            from app.tasks.locks import lock_run
            _, run = lock_run(session, proof["run_id"])
            serialize_writes(session, key="task-budget:" + proof["run_id"])
            if run.wallet_account_id in {None, proof["account_id"]}:
                run.wallet_account_id = proof["account_id"]
                prior = session.scalar(select(TaskEvent.id).where(TaskEvent.run_id == run.id,
                    TaskEvent.kind == "call_reconciled", TaskEvent.data_json.contains(
                        '"invocation": ' + json.dumps(invocation))))
                if not prior:
                    event(session, session.get(Task, run.task_id), "call_reconciled", "Confirmed usage was matched to this task.",
                          run_id=run.id, data={"invocation": invocation, "ledger_entry": proof["ledger_id"]})
                    recovered += 1
                session.commit()
    return recovered


def context():
    """Internal current run/step references for source-owned workflow receipts."""
    return _CONTEXT.get()

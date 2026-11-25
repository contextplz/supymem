"""Durable intake and actual mounted HTTP route; synthetic planner only."""
import asyncio
import hashlib
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.orm import sessionmaker

from tests.unit.tasks.test_task_lifecycle import lifecycle as lifecycle


@pytest.fixture()
def intake_db(lifecycle, monkeypatch, tmp_path):
    from app import state
    from app.db.models import Base, Person, Venture, Workspace
    from app.tasks import intake, planner
    d = lifecycle
    # Real mounted sync endpoints use FastAPI's worker thread. A file database
    # shares the same scoped rows across those connections without fake routes.
    engine = create_engine("sqlite:///" + str(tmp_path / "intake.db"), connect_args={"check_same_thread": False})
    names = set(inspect(d["factory"].kw["bind"]).get_table_names())
    Base.metadata.create_all(engine, tables=[table for table in Base.metadata.tables.values() if table.name in names])
    factory = sessionmaker(engine, expire_on_commit=False)
    with d["factory"]() as old, factory() as session:
        for model in (Person, Workspace, Venture):
            for row in old.scalars(select(model)):
                session.add(model(**{column.name: getattr(row, column.name) for column in model.__table__.columns}))
            session.flush()
        session.commit()
    d["factory"] = factory
    for module in (state, d["api"], d["usage"], d["execution"]):
        monkeypatch.setattr(module, "db", lambda: factory)
    calls, mode = [], {"value": "ok"}
    from tests.task_billing_fixtures import checkpoint, install_recovery, synthetic_receipt
    recovery = install_recovery(monkeypatch)
    module = ModuleType("app.credits.metered")
    async def llm(messages, **kwargs):
        calls.append((messages, kwargs))
        if mode["value"] == "timeout":
            raise TimeoutError("Private provider output must not be saved")
        if mode["value"] == "wait":
            mode["entered"].set()
            await mode["release"].wait()
        from app.tasks import goals
        body = {"title": "Welcome note", "goal": "Model-invented goal", "kind": "one_time", "trigger": {},
            "steps": [{"title": "Prepare the welcome note", "skill": "task.run", "inputs": {},
                       **({"subgoal": json.loads(messages[-1]["content"])["goal"]} if goals.enabled() else {})}]}
        response = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(body)))])
        receipt = synthetic_receipt(kwargs)
        checkpoint(response, kwargs, receipt)
        recovery.confirm(receipt)
        return response
    module.llm = llm
    monkeypatch.setitem(sys.modules, module.__name__, module)
    d.update(intake=intake, planner=planner, calls=calls, mode=mode, recovery=recovery,
        scope={"workspace_id": "ws_lifecycle", "venture_id": "ven_lifecycle", "owner_person_id": "usr_lifecycle"},
        body={"goal": "Draft a short welcome note", "timezone": None, "start": True, "contract": None},
        key=hashlib.sha256(b"usr_lifecycle:ws_lifecycle:ven_lifecycle:synthetic-intake").hexdigest())
    yield d
    engine.dispose()


def _begin(d, body=None, key=None, zone="UTC"):
    with d["factory"]() as session:
        row, flag = d["intake"].begin(session, **d["scope"], create_key=key or d["key"],
            body_input=d["body"] if body is None else body, accepted_zone=zone)
        session.commit()
        return row.id, flag


def _row(d, task_id):
    from app.db.task_models import Task
    with d["factory"]() as session:
        return session.get(Task, task_id)


def _journal(row):
    return json.loads(row.scope_json)["_planning"]


def _contract(d):
    from app.tasks.contracts import Contract
    return Contract(title="Welcome note", goal=d["body"]["goal"], steps=[{"title": "Prepare a welcome note", "skill": "task.run"}])


def _respond(d, task_id, contract=None):
    from tests.task_billing_fixtures import synthetic_receipt
    request_id = _journal(_row(d, task_id))["request_id"]
    receipt = synthetic_receipt({**d["scope"], "purpose": "task.plan", "request_id": request_id})
    with d["factory"]() as session:
        d["intake"].respond(session, **d["scope"], task_id=task_id,
            request_id=request_id, contract=contract or _contract(d), usage=receipt)
        session.commit()
    d["recovery"].confirm(receipt)


def _recover(d, task_id):
    from app.credits.errors import CreditError
    try:
        confirmed = d["intake"].reconcile(task_id=task_id, **d["scope"])
    except (CreditError, ValueError):
        confirmed = None
    with d["factory"]() as session:
        row = d["intake"].recover(session, **d["scope"], task_id=task_id, confirmed_billing=confirmed)
        session.commit()
        return row


def _count(d, model):
    with d["factory"]() as session:
        return session.scalar(select(func.count()).select_from(model))


def test_begin_durable_calling_before_transport_has_one_task_no_run_or_admission(intake_db):
    from app.db.models import Job
    from app.db.task_models import Task, TaskEvent, TaskRun
    d = intake_db
    task_id, flag = _begin(d)
    row = _row(d, task_id)
    assert flag and row.state == "working" and row.goal == d["body"]["goal"]
    assert _journal(row)["state"] == "calling" and _journal(row)["request_id"] == "task-plan:" + task_id + ":1"
    assert _count(d, Task) == 1 and _count(d, TaskRun) == _count(d, Job) == 0 and d["calls"] == []
    with d["factory"]() as session:
        events = session.scalars(select(TaskEvent).where(TaskEvent.task_id == task_id)).all()
        assert len(events) == 1 and events[0].kind == "planning"
        assert "ready" not in events[0].message and "welcome" not in events[0].data_json


def test_identical_duplicate_or_concurrent_begin_has_one_durable_attempt(intake_db):
    from app.db.task_models import TaskEvent
    d = intake_db
    task_id, first = _begin(d)
    replay, second = _begin(d)
    assert first and not second and task_id == replay
    assert _count(d, TaskEvent) == 1 and d["calls"] == []


@pytest.mark.parametrize("change", [{"goal": "Draft a different note"}, {"timezone": "Asia/Kolkata"}, {"start": False}, {"contract": {"title": "Different"}}])
def test_changed_raw_input_conflicts_before_another_paid_attempt(intake_db, change):
    d = intake_db
    _begin(d)
    with pytest.raises(d["intake"].IntakeConflict):
        _begin(d, {**d["body"], **change})
    assert d["calls"] == []


def test_accepted_timezone_is_frozen_when_person_default_changes(intake_db):
    d = intake_db
    task_id, _ = _begin(d, zone="Asia/Kolkata")
    assert _begin(d, zone="America/New_York") == (task_id, False)
    assert _journal(_row(d, task_id))["accepted_zone"] == "Asia/Kolkata"


def test_response_committed_then_crash_recovers_same_task_once_without_plan(intake_db):
    from app.db.models import Job
    from app.db.task_models import Task, TaskRun
    d = intake_db
    task_id, _ = _begin(d)
    _respond(d, task_id)
    assert _journal(_row(d, task_id))["state"] == "responded" and _count(d, TaskRun) == 0
    row = _recover(d, task_id)
    assert row.id == task_id and row.goal == d["body"]["goal"] and _journal(row)["state"] == "complete"
    assert _count(d, Task) == _count(d, TaskRun) == _count(d, Job) == 1
    assert _recover(d, task_id).id == task_id and _count(d, TaskRun) == 1 and d["calls"] == []


@pytest.mark.parametrize("state", ["calling", "uncertain", "failed"])
def test_saved_ambiguous_or_failed_request_never_redials_or_starts(intake_db, state):
    from app.db.task_models import TaskRun
    d = intake_db
    task_id, _ = _begin(d)
    if state != "calling":
        with d["factory"]() as session:
            d["intake"].fail(session, **d["scope"], task_id=task_id,
                request_id=_journal(_row(d, task_id))["request_id"], uncertain=state == "uncertain")
            session.commit()
    assert _begin(d) == (task_id, False)
    assert _journal(_recover(d, task_id))["state"] == state and _count(d, TaskRun) == 0 and d["calls"] == []


@pytest.mark.parametrize("state", ["paused", "canceled"])
def test_pause_or_cancel_during_call_saves_response_but_starts_nothing(intake_db, state):
    from app.db.task_models import Task, TaskRun
    d = intake_db
    task_id, _ = _begin(d)
    with d["factory"]() as session:
        task = session.get(Task, task_id)
        if state == "canceled":
            d["service"].cancel(session, task)
        else:
            task.state = "paused"
        session.commit()
    _respond(d, task_id)
    row = _recover(d, task_id)
    assert row.state == state and _journal(row)["state"] == "complete" and _count(d, TaskRun) == 0


@pytest.mark.parametrize("tamper", ["goal", "source_key", "request_id", "contract_hash", "owner", "accepted_zone", "accepted_at", "start"])
def test_saved_response_or_binding_tamper_cannot_start_work(intake_db, tamper):
    from app.db.models import Workspace
    from app.db.task_models import Task, TaskRun
    d = intake_db
    task_id, _ = _begin(d)
    _respond(d, task_id)
    with d["factory"]() as session:
        task = session.get(Task, task_id)
        scope = json.loads(task.scope_json)
        if tamper == "goal":
            task.goal = "A different caller goal"
        elif tamper == "owner":
            session.get(Workspace, "ws_lifecycle").owner_id = "another_founder"
        elif tamper == "contract_hash":
            scope["_planning"]["contract_hash"] = "f" * 64
        else:
            scope["_planning"][tamper] = "foreign"
        task.scope_json = json.dumps(scope)
        session.commit()
    with pytest.raises((d["intake"].IntakeConflict, PermissionError)):
        _recover(d, task_id)
    assert _count(d, TaskRun) == 0 and d["calls"] == []


def test_secret_or_changed_goal_response_is_discarded_without_redial(intake_db):
    d = intake_db
    task_id, _ = _begin(d)
    contract = _contract(d).model_copy(update={"goal": "Invented goal"})
    with pytest.raises(d["intake"].IntakeConflict):
        _respond(d, task_id, contract)
    contract = _contract(d).model_copy(update={"title": "sk-" + "a" * 48})
    with pytest.raises(ValueError):
        _respond(d, task_id, contract)
    assert _begin(d) == (task_id, False) and _journal(_row(d, task_id))["state"] == "calling"


def test_supplied_contract_or_start_false_uses_no_paid_planner(intake_db):
    from app.db.task_models import TaskRun
    d = intake_db
    task_id, flag = _begin(d, {**d["body"], "start": False, "contract": _contract(d).model_dump()})
    assert flag is False and _journal(_row(d, task_id))["state"] == "responded"
    assert _journal(_recover(d, task_id))["state"] == "complete" and _count(d, TaskRun) == 0 and d["calls"] == []


def test_old_direct_service_key_without_intake_marker_is_not_adopted_or_replanned(intake_db):
    d = intake_db
    with d["factory"]() as session:
        row = d["service"].create(session, **d["scope"], contract=_contract(d), start=False, create_key=d["key"])
        # Even an equal digest alone is not proof of this intake's source intent.
        row.create_digest = d["intake"]._hash(d["body"])
        session.commit()
    with pytest.raises(d["intake"].IntakeConflict):
        _begin(d)
    assert d["calls"] == []


@pytest.fixture()
def mounted(intake_db):
    from fastapi import FastAPI
    from app.tenancy import bind
    d = intake_db
    app = FastAPI()
    app.include_router(d["api"].router)
    @app.middleware("http")
    async def scoped(request, call_next):
        with bind(workspace_id="ws_lifecycle", venture_id="ven_lifecycle", actor=request.headers.get("x-subject", "usr_lifecycle")):
            return await call_next(request)
    d["app"] = app
    return d


async def _post(d, body=None):
    import httpx
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=d["app"]), base_url="http://synthetic.local") as client:
        return await client.post("/ventures/ven_lifecycle/handoffs", json=d["body"] if body is None else body,
            headers={"idempotency-key": "synthetic-intake"})


async def _action(d, task_id, name):
    import httpx
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=d["app"]), base_url="http://synthetic.local") as client:
        return await client.post(f"/ventures/ven_lifecycle/handoffs/{task_id}/{name}")


def test_mounted_auto_plan_repeat_uses_one_paid_attempt_and_same_task(mounted):
    from app.db.task_models import Task, TaskRun
    d = mounted
    first, second = asyncio.run(_post(d)), asyncio.run(_post(d))
    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"] and len(d["calls"]) == 1
    assert _count(d, Task) == _count(d, TaskRun) == 1
    request = d["calls"][0][1]
    assert request["request_id"] == "task-plan:" + first.json()["id"] + ":1" and request["purpose"] == "task.plan"


def test_mounted_changed_input_conflicts_before_second_paid_attempt(mounted):
    d = mounted
    first = asyncio.run(_post(d))
    second = asyncio.run(_post(d, {**d["body"], "goal": "Draft a different note"}))
    assert first.status_code == 202 and second.status_code == 409 and len(d["calls"]) == 1


@pytest.mark.parametrize("zone", ["Asia/Kolkata", "invalid/timezone"])
def test_mounted_repeat_changed_timezone_conflicts_without_changing_founder_preference(mounted, zone):
    from app.db.models import Person
    d = mounted
    first = asyncio.run(_post(d, {**d["body"], "timezone": "UTC"}))
    second = asyncio.run(_post(d, {**d["body"], "timezone": zone}))
    assert first.status_code == 202 and second.status_code == 409 and len(d["calls"]) == 1
    with d["factory"]() as session:
        assert session.get(Person, "usr_lifecycle").timezone == "UTC"


@pytest.mark.parametrize("action", ["pause", "cancel"])
def test_mounted_pause_cancel_during_actual_call_preserves_state_and_no_run(mounted, action):
    from app.db.task_models import Task, TaskRun
    d = mounted
    async def active():
        d["mode"].update(value="wait", entered=asyncio.Event(), release=asyncio.Event())
        request = asyncio.create_task(_post(d))
        await asyncio.wait_for(d["mode"]["entered"].wait(), 2)
        with d["factory"]() as session:
            task_id = session.scalar(select(Task.id))
        stopped = await _action(d, task_id, action)
        assert stopped.status_code == 200 and _count(d, TaskRun) == 0
        d["mode"]["release"].set()
        result = await request
        assert result.status_code == 202 and result.json()["state"] == ("paused" if action == "pause" else "canceled")
        duplicate = await _post(d)
        assert duplicate.status_code == 202 and duplicate.json()["id"] == task_id
        assert _journal(_row(d, task_id))["state"] == "complete"
    asyncio.run(active())
    assert len(d["calls"]) == 1 and _count(d, TaskRun) == 0


def test_mounted_calling_checkpoint_cannot_resume_or_start_a_run(mounted):
    from app.db.task_models import Task, TaskRun
    d = mounted
    async def active():
        d["mode"].update(value="wait", entered=asyncio.Event(), release=asyncio.Event())
        request = asyncio.create_task(_post(d))
        await asyncio.wait_for(d["mode"]["entered"].wait(), 2)
        with d["factory"]() as session:
            task = session.scalar(select(Task))
            assert d["service"].start_run(session, task, "forbidden-before-plan") is None
            assert d["service"].can_edit(session, task) is False
            task_id = task.id
            session.commit()
        resumed = await _action(d, task_id, "resume")
        assert resumed.status_code == 409 and _count(d, TaskRun) == 0
        d["mode"]["release"].set()
        assert (await request).status_code == 202
    asyncio.run(active())
    assert len(d["calls"]) == 1 and _count(d, TaskRun) == 1


def test_mounted_concurrent_auto_plan_has_one_call_and_no_run_before_response(mounted):
    from app.db.task_models import TaskRun
    d = mounted
    async def concurrent():
        d["mode"].update(value="wait", entered=asyncio.Event(), release=asyncio.Event())
        first = asyncio.create_task(_post(d))
        await asyncio.wait_for(d["mode"]["entered"].wait(), 2)
        duplicate = await _post(d)
        assert duplicate.status_code == 202 and len(d["calls"]) == 1 and _count(d, TaskRun) == 0
        d["mode"]["release"].set()
        completed = await first
        assert completed.status_code == 202 and completed.json()["id"] == duplicate.json()["id"]
    asyncio.run(concurrent())
    assert len(d["calls"]) == 1 and _count(d, TaskRun) == 1


def test_mounted_unknown_planner_result_is_saved_and_never_redialed(mounted):
    from app.db.task_models import Task, TaskRun
    d = mounted
    d["mode"]["value"] = "timeout"
    first, second = asyncio.run(_post(d)), asyncio.run(_post(d))
    assert first.status_code == second.status_code == 202 and len(d["calls"]) == 1
    assert first.json()["id"] == second.json()["id"] and _count(d, Task) == 1 and _count(d, TaskRun) == 0
    row = _row(d, first.json()["id"])
    assert _journal(row)["state"] == "uncertain" and "Private provider" not in row.scope_json + str(row.blocker_json)


def test_mounted_responded_checkpoint_recovers_same_task_without_another_plan(mounted):
    from app.db.task_models import TaskRun
    d = mounted
    task_id, _ = _begin(d)
    _respond(d, task_id)
    recovered = asyncio.run(_post(d))
    assert recovered.status_code == 202 and recovered.json()["id"] == task_id
    assert d["calls"] == [] and _count(d, TaskRun) == 1 and _journal(_row(d, task_id))["state"] == "complete"


@pytest.fixture()
def actual_metered_intake(mounted, monkeypatch):
    """Real sync S02 gate under an async shim, not live LLM routing or provider I/O."""
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    import importlib
    from app.credits import _txn, gate, policy, reserve
    from app.credits.grants import grant
    from app.db.append_only import _install_sqlite
    from app.db.models import CreditAccount, CreditLedgerEntry, CreditPolicy, CreditReservation
    from app.db.task_models import Task
    from app.email import triggers
    d = mounted
    for model in (CreditAccount, CreditLedgerEntry, CreditPolicy, CreditReservation):
        model.__table__.create(d["factory"].kw["bind"], checkfirst=True)
    with d["factory"].kw["bind"].begin() as connection:
        _install_sqlite(connection, "credit_ledger")
        _install_sqlite(connection, "task_events")
    monkeypatch.setattr(_txn, "db", lambda: d["factory"])
    monkeypatch.setattr(triggers, "on_credit_balance", lambda *_args: None)
    with d["factory"]() as session:
        now = d["clock"][0]
        session.add(CreditAccount(id="cra_intake_real_gate", scope="person", owner_person_id="usr_lifecycle", status="active",
            granted_micro=0, spent_micro=0, reserved_micro=0, version=0, created_at=now, updated_at=now))
        session.add(CreditPolicy(key="enforcement_enabled", value_json="true", updated_at=now, updated_by="synthetic"))
        session.commit()
    policy.invalidate()
    grant(account_id="cra_intake_real_gate", amount_micro=5000, reason="Synthetic fixture funding",
        actor_kind="system", actor_id="synthetic", idempotency_key="synthetic-intake-funding")
    monkeypatch.delitem(sys.modules, "app.credits.metered")
    metered = importlib.import_module("app.credits.metered")
    assert d["recovery"].original_reconcile is not None, "The approved S02 recovery ABI must be integrated"
    monkeypatch.setattr(gate, "reconcile_receipt", d["recovery"].original_reconcile)
    real_settle = reserve.settle
    options = {"settlement_failure": False, "missing_hook": False, "byok": False, "custody": None}
    provider_calls, saved_before_settle = [], []
    def settlement(*args, **kwargs):
        with d["factory"]() as session:
            task = session.scalar(select(Task))
            if task:
                journal = _journal(task)
                saved_before_settle.append((journal.get("state"), journal.get("billing")))
        if options["settlement_failure"]:
            from app.credits.errors import CreditsUnavailable
            raise CreditsUnavailable("Synthetic wallet settlement is temporarily unavailable")
        return real_settle(*args, **kwargs)
    monkeypatch.setattr(gate, "settle", settlement)
    monkeypatch.setattr(reserve, "settle", settlement)
    async def synthetic_llm(messages, **kwargs):
        def response():
            from app.db.models import Workspace
            provider_calls.append(kwargs["request_id"])
            if options["custody"]:
                with d["factory"]() as session:
                    workspace = session.get(Workspace, "ws_lifecycle")
                    if options["custody"] == "owner":
                        workspace.owner_id = "usr_another_owner"
                    else:
                        workspace.archived_at = d["clock"][0]
                    session.commit()
            body = {"title": "Welcome note", "goal": "Model-invented goal", "kind": "one_time", "trigger": {},
                "steps": [{"title": "Prepare the welcome note", "skill": "task.run", "inputs": {}}]}
            return SimpleNamespace(usage={"prompt_tokens": 120, "completion_tokens": 180, "total_tokens": 300,
                "prompt_tokens_details": {"cached_tokens": 20}},
                choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(body)))])
        return metered.call_sync(response, purpose=kwargs["purpose"], account_id="cra_intake_real_gate",
            workspace_id=kwargs["workspace_id"], venture_id=kwargs["venture_id"], provider="synthetic", model="synthetic-confirmed-model",
            estimate_micro=1000, actual_micro=lambda _result: 700, request_id=kwargs["request_id"], max_tokens=kwargs["max_tokens"],
            payer="founder" if options["byok"] else None,
            before_meter_exit=None if options["missing_hook"] else kwargs.get("before_meter_exit"))
    monkeypatch.setattr(metered, "llm", synthetic_llm)
    d.update(real_gate=gate, real_metered=metered, actual_options=options,
        provider_calls=provider_calls, saved_before_settle=saved_before_settle)
    return d


def _real_billing_rows(d):
    from app.db.models import CreditAccount, CreditLedgerEntry, CreditReservation
    with d["factory"]() as session:
        account = session.get(CreditAccount, "cra_intake_real_gate")
        holds = session.scalars(select(CreditReservation)).all()
        ledger = session.scalars(select(CreditLedgerEntry).order_by(CreditLedgerEntry.seq)).all()
        return (account.spent_micro, account.reserved_micro), holds, [
            (row.id, row.kind, row.amount_micro, row.reservation_id, row.meta_json, row.prev_hash, row.entry_hash) for row in ledger]


def test_real_metered_intake_saves_response_before_failed_wallet_exit_and_replays_original_hold(actual_metered_intake):
    from app.credits.ledger import verify_chain
    from app.db.task_models import TaskRun
    d = actual_metered_intake
    d["actual_options"]["settlement_failure"] = True
    first = asyncio.run(_post(d))
    assert first.status_code == 202
    task_id = first.json()["id"]
    journal = _journal(_row(d, task_id))
    assert journal["state"] == "responded" and journal["billing_pending"] and journal["contract_hash"]
    assert _row(d, task_id).state == "input_required" and _count(d, TaskRun) == 0
    assert d["saved_before_settle"] and all(state == "responded" and billing for state, billing in d["saved_before_settle"])
    original_billing, original_contract = journal["billing"], journal["contract_hash"]
    balance, holds, ledger = _real_billing_rows(d)
    assert balance == (0, 1000) and len(holds) == 1 and holds[0].status == "reconciling"
    from datetime import timedelta
    from app.credits.reserve import sweep_expired
    assert sweep_expired(d["clock"][0] + timedelta(days=2)) == 0
    assert _real_billing_rows(d)[0] == balance and _real_billing_rows(d)[2] == ledger
    hold_id = holds[0].id
    duplicate = asyncio.run(_post(d))
    assert duplicate.status_code == 202 and duplicate.json()["id"] == task_id and _count(d, TaskRun) == 0
    assert len(d["provider_calls"]) == 1 and _journal(_row(d, task_id))["billing"] == original_billing
    d["actual_options"]["settlement_failure"] = False
    recovered = asyncio.run(_post(d))
    assert recovered.status_code == 202 and recovered.json()["id"] == task_id
    assert _count(d, TaskRun) == 1 and len(d["provider_calls"]) == 1
    completed = _journal(_row(d, task_id))
    assert completed["state"] == "complete" and completed["contract_hash"] == original_contract and completed["billing"] == original_billing
    balance, holds, paid = _real_billing_rows(d)
    assert balance == (700, 0) and len(holds) == 1 and holds[0].id == hold_id and holds[0].status == "settled"
    spend = [entry for entry in paid if entry[1] == "spend"]
    assert len(spend) == 1 and spend[0][2] == -700 and spend[0][3] == hold_id
    assert json.loads(spend[0][4])["cached_tokens"] == 20 and verify_chain("cra_intake_real_gate") == (True, None)
    assert asyncio.run(_post(d)).status_code == 202 and _real_billing_rows(d)[2] == paid


def test_real_metered_intake_missing_hook_never_applies_a_returned_model_plan(actual_metered_intake):
    from app.db.task_models import TaskRun
    d = actual_metered_intake
    d["actual_options"]["missing_hook"] = True
    first, repeated = asyncio.run(_post(d)), asyncio.run(_post(d))
    assert first.status_code == repeated.status_code == 202 and first.json()["id"] == repeated.json()["id"]
    assert _count(d, TaskRun) == 0 and len(d["provider_calls"]) == 1
    assert _journal(_row(d, first.json()["id"]))["state"] == "calling"


def test_real_metered_intake_missing_cached_receipt_never_applies_or_creates_another_hold(actual_metered_intake):
    from app.db.task_models import TaskRun
    from app.tasks.locks import lock_task
    d = actual_metered_intake
    d["actual_options"]["settlement_failure"] = True
    first = asyncio.run(_post(d))
    assert first.status_code == 202
    task_id = first.json()["id"]
    with d["factory"]() as session:
        task = lock_task(session, task_id)
        scope = json.loads(task.scope_json)
        scope["_planning"].pop("billing")
        task.scope_json = json.dumps(scope)
        session.commit()
    d["actual_options"]["settlement_failure"] = False
    repeated = asyncio.run(_post(d))
    assert repeated.status_code == 202 and repeated.json()["id"] == task_id
    assert _row(d, task_id).state == "input_required" and _count(d, TaskRun) == 0
    assert len(d["provider_calls"]) == 1 and _real_billing_rows(d)[0] == (0, 1000)
    assert len(_real_billing_rows(d)[1]) == 1


def test_real_metered_intake_byok_has_no_hold_and_preserves_full_confirmed_tokens(actual_metered_intake):
    from app.db.task_models import TaskRun
    d = actual_metered_intake
    d["actual_options"]["byok"] = True
    first, repeated = asyncio.run(_post(d)), asyncio.run(_post(d))
    assert first.status_code == repeated.status_code == 202 and first.json()["id"] == repeated.json()["id"]
    assert _count(d, TaskRun) == 1 and len(d["provider_calls"]) == 1
    receipt = _journal(_row(d, first.json()["id"]))["billing"]["usage"]
    assert receipt["source"] == "byok" and receipt["reservation_id"] is None and receipt["task_invocation"] is None
    assert (receipt["prompt_tokens"], receipt["completion_tokens"], receipt["cached_tokens"], receipt["total_tokens"], receipt["provider_actual_micro"]) == (120, 180, 20, 300, 700)
    balance, holds, ledger = _real_billing_rows(d)
    assert balance == (0, 0) and holds == [] and all(entry[2] == 0 for entry in ledger if entry[1] == "spend")


@pytest.mark.parametrize("change", ["owner", "archive"])
def test_real_funded_intake_cleanup_after_custody_loss_does_not_apply_goal_or_call_again(actual_metered_intake, change):
    from app.credits.ledger import verify_chain
    from app.db.task_models import Task, TaskRun
    d = actual_metered_intake
    d["actual_options"].update(settlement_failure=True, custody=change)
    denied = asyncio.run(_post(d))
    assert denied.status_code == (403 if change == "owner" else 404) and len(d["provider_calls"]) == 1
    with d["factory"]() as session:
        task_id = session.scalar(select(Task.id))
    journal = _journal(_row(d, task_id))
    assert journal["state"] == "responded" and journal["billing"] and _count(d, TaskRun) == 0
    d["actual_options"]["settlement_failure"] = False
    confirmed = d["intake"].reconcile(task_id=task_id, **d["scope"])
    assert isinstance(confirmed, str) and len(confirmed) == 64
    with d["factory"]() as session, pytest.raises(PermissionError):
        d["intake"].recover(session, **d["scope"], task_id=task_id, confirmed_billing=confirmed)
    assert _count(d, TaskRun) == 0 and len(d["provider_calls"]) == 1
    assert _real_billing_rows(d)[0] == (700, 0) and verify_chain("cra_intake_real_gate") == (True, None)


@pytest.mark.parametrize("change", ["owner", "workspace_archive", "venture_archive"])
def test_funded_recovery_uses_fresh_custody_after_cached_read_before_task_lock(intake_db, change):
    from sqlalchemy import update
    from app.db.models import Venture, Workspace
    from app.db.task_models import TaskRun
    d = intake_db
    task_id, _ = _begin(d)
    _respond(d, task_id)
    confirmed = d["intake"].reconcile(task_id=task_id, **d["scope"])
    with d["factory"]() as session:
        cached_workspace = session.get(Workspace, "ws_lifecycle")
        cached_venture = session.get(Venture, "ven_lifecycle")
        # Models the API/chat read before waiting for the Task lock. The
        # independent writer commits while the reader's identity map stays stale.
        with d["factory"]() as writer:
            if change == "owner":
                writer.execute(update(Workspace).where(Workspace.id == cached_workspace.id).values(owner_id="usr_other"))
            else:
                model, cached = (Workspace, cached_workspace) if change == "workspace_archive" else (Venture, cached_venture)
                writer.execute(update(model).where(model.id == cached.id).values(archived_at=d["clock"][0]))
            writer.commit()
        assert cached_workspace.owner_id == "usr_lifecycle" and cached_workspace.archived_at is None
        assert cached_venture.archived_at is None
        with pytest.raises(PermissionError):
            d["intake"].recover(session, **d["scope"], task_id=task_id, confirmed_billing=confirmed)
        assert cached_workspace.owner_id == "usr_lifecycle" and cached_workspace.archived_at is None
        assert cached_venture.archived_at is None  # Read-only proof does not overwrite an owned identity.
    assert _count(d, TaskRun) == 0 and d["calls"] == []
    assert _journal(_row(d, task_id))["state"] == "responded"

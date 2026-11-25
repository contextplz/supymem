"""Real Task rows with synthetic trusted transport inputs; no provider/native I/O."""
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from tests.company_os_harness import load_company_os_app

NOW = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
BINDING = dict(task_id="tsk_native", run_id="trn_native", step_id="tst_native", order_id="dwo_native",
    execution_id="dex_native", review_digest="a" * 64, invocation_id="call_one", request_digest="b" * 64)
CALL_BINDING = dict(schema_version=1, purpose="task.plan", provider="openai", model="deployment_one",
    catalog_model="gpt-6.1-sol", request_id="task-plan:tsk_native", captured_tier="pro", api_mode="responses",
    max_output_tokens=3000, reasoning_effort="high")


@pytest.fixture()
def native_db(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", "sqlite:///:memory:")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    load_company_os_app()
    from app.credits import _txn
    from app.db.models import Base, CreditAccount, CreditLedgerEntry, CreditReservation, Job, Person, Venture, Workspace
    from app.db.supymem_models import Notice
    from app.db.task_models import Task, TaskEvent, TaskRun, TaskStep
    from app.state import set_factory
    from app.tasks import usage
    from app.tasks.contracts import Budget

    monkeypatch.setattr(_txn, "utcnow", lambda: NOW)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[model.__table__ for model in (
        Person, Workspace, Venture, CreditAccount, CreditLedgerEntry, CreditReservation,
        Job, Notice, Task, TaskRun, TaskStep, TaskEvent)])
    factory = sessionmaker(engine, autoflush=False, expire_on_commit=False)
    set_factory(factory)
    with factory() as session:
        moment = NOW.replace(tzinfo=None)
        session.add(Person(id="usr_native", display_name="Synthetic founder", created_at=moment))
        session.add(Workspace(id="ws_native", owner_id="usr_native", kind="venture", name="Synthetic",
            entry_mode="idea", created_at=moment, updated_at=moment))
        session.add(Venture(id="ven_native", workspace_id="ws_native", name="Synthetic", raw_idea="Synthetic task",
            stage="draft", created_at=moment, updated_at=moment))
        session.add(Task(id="tsk_native", workspace_id="ws_native", venture_id="ven_native", owner_person_id="usr_native",
            title="Synthetic native task", goal="Synthetic native task", budget_json=Budget().model_dump_json(), state="working",
            deadline_at=moment + timedelta(minutes=20), created_at=moment, updated_at=moment))
        session.add(TaskRun(id="trn_native", task_id="tsk_native", run_key="dex_native", state="working",
            started_at=moment, deadline_at=moment + timedelta(minutes=20), attempts=3, tool_calls=4))
        session.add(TaskStep(id="tst_native", run_id="trn_native", position=0, title="Synthetic native step",
            skill="device.work_order", state="working", runs_on="device", deadline_at=moment + timedelta(minutes=20),
            inputs_json=json.dumps({"device_order_id": "dwo_native", "execution_id": "dex_native", "review_digest": "a" * 64})))
        session.commit()
    yield SimpleNamespace(module=usage, factory=factory)
    engine.dispose()


def reserve(d, session=None, **changes):
    args = {**BINDING, "estimate_micro": 1000, "max_tokens": 2000, **changes}
    if session is not None:
        return d.module.reserve_native_call(session, **args)
    with d.factory() as session:
        result = d.module.reserve_native_call(session, **args)
        session.commit()
        return result


def settle(d, session=None, **changes):
    args = {**BINDING, "actual_micro": 1300, "prompt_tokens": 120, "completion_tokens": 180, "cached_tokens": 20, **changes}
    if session is not None:
        return d.module.settle_native_call(session, **args)
    with d.factory() as session:
        result = d.module.settle_native_call(session, **args)
        session.commit()
        return result


def tool(d, session=None, **changes):
    args = {**BINDING, "effect_digest": "c" * 64, "observation_digest": "d" * 64, **changes}
    if session is not None:
        return d.module.reserve_native_tool(session, **args)
    with d.factory() as session:
        result = d.module.reserve_native_tool(session, **args)
        session.commit()
        return result


def saved(d):
    from app.db.task_models import Task, TaskEvent, TaskRun, TaskStep
    with d.factory() as session:
        task, run = session.get(Task, "tsk_native"), session.get(TaskRun, "trn_native")
        return SimpleNamespace(state=(task.state, run.state), blocker=json.loads(task.blocker_json or "{}"),
            usage=(run.cost_micro, run.tokens_used, run.reserved_cost_micro, run.reserved_tokens),
            fixed=(task.budget_json, run.started_at, run.deadline_at, run.attempts, run.tool_calls, run.allowance_json),
            evidence=session.get(TaskStep, "tst_native").evidence_json,
            events=[(row.kind, json.loads(row.data_json)) for row in session.scalars(select(TaskEvent))])


def test_admission_replay_and_surrounding_transaction_rollback_are_atomic(native_db):
    from app.db.task_models import TaskStep
    d, before = native_db, saved(native_db)
    with d.factory() as session:
        first = reserve(d, session)
        assert first["admitted_now"] and not reserve(d, session)["admitted_now"]
        session.get(TaskStep, "tst_native").evidence_json = '{"order_marker":"uncommitted"}'
        session.rollback()
    after = saved(d)
    assert after.usage == (0, 0, 0, 0) and after.events == [] and after.evidence == before.evidence
    assert after.fixed == before.fixed


def test_raw_cost_and_full_prompt_plus_completion_settle_once_without_budget_reset(native_db):
    d, before = native_db, saved(native_db)
    admitted = reserve(d)
    assert not reserve(d)["admitted_now"]
    assert settle(d) and not settle(d)
    after = saved(d)
    assert after.usage == (1300, 300, 0, 0) and after.fixed == before.fixed
    reserved = next(data for kind, data in after.events if kind == "call_reserved")
    confirmed = next(data for kind, data in after.events if kind == "call_settled")
    assert reserved["invocation"] == confirmed["invocation"] == admitted["invocation"]
    assert confirmed["receipt_kind"] == "device_order" and confirmed["native_binding"] == BINDING
    assert (confirmed["prompt_tokens"], confirmed["completion_tokens"], confirmed["cached_tokens"], confirmed["tokens"]) == (120, 180, 20, 300)


def test_settlement_rollback_retains_the_original_reservation(native_db):
    d = native_db
    reserve(d)
    with d.factory() as session:
        assert settle(d, session)
        session.rollback()
    assert saved(d).usage == (0, 0, 1000, 2000)
    assert [kind for kind, _ in saved(d).events] == ["call_reserved"]


def test_deferred_native_usage_blocks_without_accounting_or_requiring_a_valid_budget(native_db):
    from app.db.task_models import Task, TaskStep
    d = native_db
    reserve(d)
    with d.factory() as session:
        session.get(Task, "tsk_native").budget_json = '{"damaged": true}'
        session.commit()
    before = saved(d)
    with d.factory() as session:
        assert d.module.mark_native_usage_deferred(session, **BINDING)
        session.commit()
    after = saved(d)
    assert after.usage == before.usage and after.fixed == before.fixed
    assert after.state == ("input_required", "input_required") and after.blocker["reconciliation_required"] is True
    assert not any(kind == "call_settled" for kind, _ in after.events)
    with d.factory() as session:
        step = session.get(TaskStep, "tst_native")
        assert step.state == "reconciliation_required" and step.lease_token is None
        version = step.lease_version
        assert not d.module.mark_native_usage_deferred(session, **BINDING)
        assert step.lease_version == version
        session.commit()
    assert saved(d).events == after.events


@pytest.mark.parametrize("state", ["paused", "canceled", "completed", "auth_required", "input_required", "approval_required"])
def test_deferred_native_usage_preserves_unrelated_controls_and_counters(native_db, state):
    from app.db.task_models import Task, TaskRun
    d = native_db
    reserve(d)
    with d.factory() as session:
        task = session.get(Task, "tsk_native")
        task.state = session.get(TaskRun, "trn_native").state = state
        task.blocker_json = json.dumps({"class": "needs_input", "reason": "Original control", "unblock_action": "Original action"})
        session.commit()
    before = saved(d)
    with d.factory() as session:
        assert not d.module.mark_native_usage_deferred(session, **BINDING)
        session.commit()
    after = saved(d)
    assert vars(after) == vars(before)


@pytest.mark.parametrize("changes", [
    {"estimate_micro": 1001}, {"max_tokens": 2001}, {"request_digest": "c" * 64},
    {"review_digest": "c" * 64}, {"execution_id": "dex_changed"}, {"order_id": "dwo_changed"},
    {"task_id": "tsk_changed"}, {"run_id": "trn_changed"}, {"step_id": "tst_changed"},
])
def test_replay_rejects_changed_estimate_or_immutable_binding(native_db, changes):
    d = native_db
    reserve(d)
    with pytest.raises(ValueError, match="task_native_(usage_conflict|binding_invalid)"):
        reserve(d, **changes)
    assert saved(d).usage == (0, 0, 1000, 2000) and len(saved(d).events) == 1


def test_native_invocation_namespace_includes_the_order(native_db):
    from app.db.task_models import TaskStep
    d = native_db
    first = reserve(d)
    with d.factory() as session:
        step = session.get(TaskStep, "tst_native")
        session.add(TaskStep(id="tst_second", run_id=step.run_id, position=1, title=step.title, skill=step.skill,
            state=step.state, runs_on=step.runs_on, deadline_at=step.deadline_at,
            inputs_json=json.dumps({"device_order_id": "dwo_second", "execution_id": "dex_second", "review_digest": "a" * 64})))
        session.commit()
    second = reserve(d, step_id="tst_second", order_id="dwo_second", execution_id="dex_second")
    assert first["invocation"] != second["invocation"] and saved(d).usage == (0, 0, 2000, 4000)


@pytest.mark.parametrize("changes", [{"estimate_micro": 500001}, {"max_tokens": 100001}])
def test_trusted_prompt_and_output_ceiling_must_fit_task_budget(native_db, changes):
    from app.core.jobs import FatalError
    d = native_db
    with d.factory() as session:
        with pytest.raises(FatalError, match="task_budget_exhausted"):
            reserve(d, session, **changes)
        session.commit()  # Native caller owns persistence of the refusal.
    assert saved(d).usage == (0, 0, 0, 0) and saved(d).blocker["class"] == "budget_exhausted"
    assert not any(kind == "call_reserved" for kind, _ in saved(d).events)


def test_admission_checks_fresh_custody_despite_cached_workspace(native_db):
    from app.core.jobs import FatalError
    from app.db.models import Workspace
    d = native_db
    with d.factory() as session:
        cached = session.get(Workspace, "ws_native")
        with d.factory() as changed:
            changed.get(Workspace, cached.id).owner_id = "usr_other"
            changed.commit()
        assert cached.owner_id == "usr_native"
        with pytest.raises(FatalError, match="task_scope_changed"):
            reserve(d, session)
        session.commit()
    assert saved(d).usage == (0, 0, 0, 0)


@pytest.mark.parametrize("state,reason", [
    ("canceled", None), ("completed", None), ("paused", None),
    ("auth_required", {"class": "needs_auth", "reason": "Reconnect", "unblock_action": "Reconnect"}),
    ("approval_required", {"class": "needs_approval", "reason": "Review", "unblock_action": "Review"}),
    ("input_required", {"class": "needs_input", "reason": "Choose", "unblock_action": "Choose"}),
])
def test_late_receipt_survives_custody_loss_and_preserves_unrelated_controls(native_db, state, reason):
    from app.db.models import Workspace
    from app.db.task_models import Task, TaskRun
    d = native_db
    reserve(d)
    with d.factory() as session:
        session.get(Workspace, "ws_native").owner_id = "usr_other"
        task = session.get(Task, "tsk_native")
        task.state = session.get(TaskRun, "trn_native").state = state
        task.blocker_json = json.dumps(reason) if reason else None
        session.commit()
    assert settle(d, actual_micro=600000)
    assert saved(d).usage == (600000, 300, 0, 0)
    assert saved(d).state == (state, state) and saved(d).blocker == (reason or {})


@pytest.mark.parametrize("changes", [{"actual_micro": None}, {"prompt_tokens": None, "cached_tokens": 0}, {"completion_tokens": None}])
def test_unknown_retains_reservations_until_known_receipt_without_false_zero(native_db, changes):
    d = native_db
    reserve(d)
    assert settle(d, **changes) and not settle(d, **changes)
    assert saved(d).usage == (0, 0, 1000, 2000)
    assert saved(d).state == ("input_required", "input_required")
    assert settle(d) and not settle(d)
    assert saved(d).usage == (1300, 300, 0, 0)
    assert saved(d).state == ("input_required", "input_required")


def test_known_overage_replaces_only_the_own_usage_unknown_blocker(native_db):
    d = native_db
    reserve(d)
    settle(d, actual_micro=None)
    assert settle(d, actual_micro=600000)
    assert saved(d).blocker["class"] == "budget_exhausted" and saved(d).usage == (600000, 300, 0, 0)


@pytest.mark.parametrize("changes", [
    {"actual_micro": 1301}, {"prompt_tokens": 121}, {"completion_tokens": 181}, {"cached_tokens": 21},
])
def test_confirmed_receipt_cannot_change_on_replay(native_db, changes):
    d = native_db
    reserve(d)
    settle(d)
    with pytest.raises(ValueError, match="task_native_usage_conflict"):
        settle(d, **changes)
    assert saved(d).usage == (1300, 300, 0, 0)


@pytest.mark.parametrize("changes", [
    {"actual_micro": True}, {"prompt_tokens": -1}, {"completion_tokens": "180"}, {"cached_tokens": 121},
])
def test_invalid_receipt_keeps_reservations(native_db, changes):
    d = native_db
    reserve(d)
    with pytest.raises(ValueError, match="task_usage_invalid"):
        settle(d, **changes)
    assert saved(d).usage == (0, 0, 1000, 2000)


def test_wallet_reconciler_skips_native_calls_before_its_scan_limit(native_db):
    d = native_db
    reserve(d)
    with d.module.run_context("trn_native"):
        generic = d.module.before_call(100, 10)
    assert d.module.reconcile_usage(limit=1) == 0
    uncertain = [data["invocation"] for kind, data in saved(d).events if kind == "call_uncertain"]
    assert uncertain == [generic] and saved(d).usage == (0, 0, 1100, 2010)


def test_native_receipt_cannot_use_the_generic_wallet_settlement_wrapper(native_db):
    d = native_db
    admitted = reserve(d)
    with pytest.raises(ValueError, match="task_native_receipt_required"):
        d.module.after_call(admitted["invocation"], 1300, 300)
    assert saved(d).usage == (0, 0, 1000, 2000)


def test_zero_provider_effect_counts_once_and_rolls_back_with_its_native_transaction(native_db):
    from app.db.task_models import TaskStep
    d, before = native_db, saved(native_db)
    with d.factory() as session:
        assert tool(d, session)["admitted_now"]
        assert not tool(d, session)["admitted_now"]
        session.get(TaskStep, "tst_native").evidence_json = '{"effect_marker":"uncommitted"}'
        session.rollback()
    assert saved(d).fixed == before.fixed and saved(d).evidence == before.evidence and saved(d).events == []
    assert tool(d)["admitted_now"] and not tool(d)["admitted_now"]
    after = saved(d)
    assert after.fixed[4] == before.fixed[4] + 1 and after.usage == (0, 0, 0, 0)
    assert len(after.events) == 1 and after.events[0][0] == "native_tool_admitted"
    assert after.events[0][1]["effect_digest"] == "c" * 64
    assert after.events[0][1]["observation_digest"] == "d" * 64


@pytest.mark.parametrize("changes", [
    {"effect_digest": "e" * 64}, {"observation_digest": "e" * 64}, {"request_digest": "e" * 64},
])
def test_effect_admission_rejects_changed_candidate_observation_or_binding(native_db, changes):
    d = native_db
    tool(d)
    with pytest.raises(ValueError, match="task_native_usage_conflict"):
        tool(d, **changes)
    assert saved(d).fixed[4] == 5 and len(saved(d).events) == 1


@pytest.mark.parametrize("state", ["paused", "canceled", "input_required", "auth_required", "approval_required"])
def test_no_new_native_effect_after_pause_cancel_unknown_or_other_blocker(native_db, state):
    from app.core.jobs import FatalError
    from app.db.task_models import Task, TaskRun
    from app.tasks.contracts import blocker
    d = native_db
    with d.factory() as session:
        task = session.get(Task, "tsk_native")
        task.state = session.get(TaskRun, "trn_native").state = state
        task.blocker_json = json.dumps(blocker("outcome_uncertain")) if state == "input_required" else None
        session.commit()
    with pytest.raises(FatalError, match="task_(paused|waiting)"):
        tool(d)
    assert saved(d).fixed[4] == 4 and not saved(d).events


def test_native_effect_obeys_original_tool_limit_without_hidden_extension(native_db):
    from app.core.jobs import FatalError
    from app.db.task_models import TaskRun
    d = native_db
    with d.factory() as session:
        session.get(TaskRun, "trn_native").tool_calls = 30
        session.commit()
    before = saved(d)
    with d.factory() as session:
        with pytest.raises(FatalError, match="task_budget_exhausted"):
            tool(d, session)
        session.commit()
    assert saved(d).fixed == before.fixed and saved(d).blocker["class"] == "budget_exhausted"
    assert not any(kind == "native_tool_admitted" for kind, _ in saved(d).events)


def test_native_effect_admission_checks_current_custody(native_db):
    from app.core.jobs import FatalError
    from app.db.models import Workspace
    d = native_db
    with d.factory() as session:
        session.get(Workspace, "ws_native").archived_at = NOW.replace(tzinfo=None)
        session.commit()
    with pytest.raises(FatalError, match="task_scope_changed"):
        tool(d)
    assert saved(d).fixed[4] == 4 and not saved(d).events


def test_original_before_tool_uses_same_policy_and_commits_its_counter(native_db):
    from app.core.jobs import FatalError
    from app.db.task_models import Task, TaskRun
    d = native_db
    with d.module.run_context("trn_native"):
        assert d.module.before_tool() is None
        assert saved(d).fixed[4] == 5
        with d.factory() as session:
            session.get(Task, "tsk_native").state = session.get(TaskRun, "trn_native").state = "paused"
            session.commit()
        with pytest.raises(FatalError, match="task_paused"):
            d.module.before_tool()
    assert saved(d).fixed[4] == 5


def test_cached_tokens_without_full_prompt_proof_is_invalid_and_never_releases_caps(native_db):
    d = native_db
    reserve(d)
    before = saved(d)
    with pytest.raises(ValueError, match="task_usage_invalid"):
        settle(d, actual_micro=1300, prompt_tokens=None, completion_tokens=180, cached_tokens=1)
    after = saved(d)
    assert after.usage == before.usage == (0, 0, 1000, 2000)
    assert after.events == before.events and after.state == before.state


def test_original_capture_binding_is_stored_once_as_a_snapshot_without_route_or_budget_changes(native_db, monkeypatch):
    from llm_gateway import routing
    d = native_db
    def unexpected_route_read(*args, **kwargs):
        raise AssertionError("Task usage must not resolve the captured route again")
    monkeypatch.setattr(routing, "resolve_tier", unexpected_route_read)
    original = dict(CALL_BINDING)
    passed = dict(original)
    with d.module.run_context("trn_native"):
        invocation = d.module.before_call(1200, 4200, call_binding=passed)
    passed["model"] = "changed_after_capture"
    reserved = [data for kind, data in saved(d).events if kind == "call_reserved"]
    assert len(reserved) == 1 and reserved[0]["call_binding"] == original
    assert saved(d).usage == (0, 0, 1200, 4200)
    d.module.after_call(invocation, 700, 300)
    d.module.after_call(invocation, 700, 300)
    assert [data for kind, data in saved(d).events if kind == "call_reserved"] == reserved
    assert saved(d).usage == (700, 300, 0, 0)


@pytest.mark.parametrize("changes", [
    {"schema_version": True}, {"schema_version": 2}, {"purpose": None}, {"purpose": "unbounded " * 20},
    {"provider": None}, {"model": None}, {"catalog_model": None}, {"captured_tier": None},
    {"api_mode": None}, {"max_output_tokens": None}, {"max_output_tokens": True}, {"max_output_tokens": 0},
    {"captured_tier": "priority"}, {"api_mode": "unknown"}, {"reasoning_effort": "unknown"},
    {"captured_tier": []}, {"api_mode": {}}, {"reasoning_effort": True}, {"request_id": 1},
    {"provider": "https://example.invalid"}, {"model": "https://example.invalid/v1"},
    {"model": "sk-proj-synthetic-secret-must-never-be-persisted"},
    {"api_key": "synthetic-secret-extra"}, {"base_url": "https://example.invalid"}, {"config": {}},
    {"service_tier": "priority"},
])
def test_malformed_or_secret_capture_is_rejected_before_counter_or_event(native_db, changes):
    d = native_db
    with d.module.run_context("trn_native"):
        with pytest.raises(ValueError, match="task_call_binding_invalid"):
            d.module.before_call(1200, 4200, call_binding={**CALL_BINDING, **changes})
    assert saved(d).usage == (0, 0, 0, 0) and saved(d).events == []


@pytest.mark.parametrize("binding", [
    {}, {key: value for key, value in CALL_BINDING.items() if key != "reasoning_effort"}, [], "opaque config",
])
def test_capture_requires_exact_closed_schema(native_db, binding):
    d = native_db
    with d.module.run_context("trn_native"):
        with pytest.raises(ValueError, match="task_call_binding_invalid"):
            d.module.before_call(1200, 4200, call_binding=binding)
    assert saved(d).usage == (0, 0, 0, 0) and saved(d).events == []


def test_capture_rejects_total_token_ceiling_smaller_than_its_captured_output_bound(native_db):
    d = native_db
    with d.module.run_context("trn_native"):
        with pytest.raises(ValueError, match="task_call_binding_invalid"):
            d.module.before_call(1200, 2999, call_binding=CALL_BINDING)
    assert saved(d).usage == (0, 0, 0, 0) and saved(d).events == []


def test_non_llm_explicit_none_route_fields_and_optional_request_remain_compatible(native_db):
    d = native_db
    binding = {key: None for key in CALL_BINDING}
    binding.update(schema_version=1, purpose="research.search", provider="search")
    with d.module.run_context("trn_native"):
        d.module.before_call(100, 0, call_binding=binding)
    assert saved(d).usage == (0, 0, 100, 0)
    assert next(data["call_binding"] for kind, data in saved(d).events if kind == "call_reserved") == binding


def test_complete_llm_capture_permits_none_reasoning_and_request_without_minting_an_id(native_db):
    d = native_db
    binding = {**CALL_BINDING, "reasoning_effort": None, "request_id": None, "captured_tier": "flash",
               "api_mode": "chat_completions"}
    with d.module.run_context("trn_native"):
        d.module.before_call(100, 3010, call_binding=binding)
    assert next(data["call_binding"] for kind, data in saved(d).events if kind == "call_reserved") == binding


@pytest.mark.parametrize("explicit_none", [False, True])
def test_legacy_capture_none_has_original_reservation_schema(native_db, explicit_none):
    d = native_db
    with d.module.run_context("trn_native"):
        d.module.before_call(100, 10, **({"call_binding": None} if explicit_none else {}))
    reservation = next(data for kind, data in saved(d).events if kind == "call_reserved")
    assert set(reservation) == {"invocation", "cost_micro", "tokens"}
    assert saved(d).usage == (0, 0, 100, 10)


def test_no_task_context_preserves_original_noop_even_with_malformed_capture(native_db):
    d = native_db
    assert d.module.context() is None
    assert d.module.before_call(-1, True, call_binding={"config": {"base_url": "ignored_outside_tasks"}}) is None
    assert saved(d).usage == (0, 0, 0, 0) and saved(d).events == []

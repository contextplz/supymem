"""Synthetic immutable wallet receipts exercise late task accounting, with no paid calls."""
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from tests.company_os_harness import load_company_os_app

NOW = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)


@pytest.fixture()
def usage_db(tmp_path, monkeypatch, request):
    monkeypatch.setenv("COMPANY_OS_DB_URL", "sqlite:///:memory:")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    load_company_os_app()
    from app.credits import _txn, reserve
    from app.db.append_only import _install_sqlite
    from app.db.models import Base, CreditAccount, CreditLedgerEntry, CreditPolicy, CreditReservation, Job, Person, Venture, Workspace
    from app.db.supymem_models import Notice
    from app.db.task_models import Task, TaskEvent, TaskRun, TaskStep
    from app.state import set_factory
    from app.tasks import usage
    from app.tasks.contracts import Budget

    captured = None
    if getattr(request, "param", None) == "modern":
        if not all(callable(getattr(reserve, name, None)) for name in ("validate_call_binding", "model_receipt_metadata")):
            pytest.skip("Owned S02 modern receipt validator ABI is not accepted in this source yet")
        captured = modern_binding()

    monkeypatch.setattr(_txn, "utcnow", lambda: NOW)
    engine = create_engine(f"sqlite:///{tmp_path / 'usage.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine, tables=[model.__table__ for model in (
        Person, Workspace, Venture, CreditAccount, CreditLedgerEntry, CreditPolicy, CreditReservation,
        Job, Notice, Task, TaskRun, TaskStep, TaskEvent)])
    with engine.begin() as connection:
        _install_sqlite(connection, "credit_ledger")
        _install_sqlite(connection, "task_events")
    set_factory(sessionmaker(engine, expire_on_commit=False))
    with Session(engine) as session:
        moment = NOW.replace(tzinfo=None)
        session.add(Person(id="usr_usage", display_name="Synthetic founder", created_at=moment))
        session.add(Workspace(id="ws_usage", owner_id="usr_usage", kind="venture", name="Synthetic",
            entry_mode="idea", created_at=moment, updated_at=moment))
        session.add(Venture(id="ven_usage", workspace_id="ws_usage", name="Synthetic", raw_idea="Synthetic task",
            stage="draft", created_at=moment, updated_at=moment))
        session.add(Task(id="tsk_usage", workspace_id="ws_usage", venture_id="ven_usage", owner_person_id="usr_usage",
            title="Synthetic task", goal="Synthetic task", budget_json=Budget().model_dump_json(), state="working",
            deadline_at=moment + timedelta(minutes=20), created_at=moment, updated_at=moment))
        session.add(TaskRun(id="trn_usage", task_id="tsk_usage", run_key="initial", state="working",
            started_at=moment, deadline_at=moment + timedelta(minutes=20)))
        session.add(TaskStep(id="tst_usage", run_id="trn_usage", position=0, title="Synthetic step", skill="task.run",
            state="working", deadline_at=moment + timedelta(minutes=20)))
        session.add(CreditAccount(id="acct_usage", scope="person", owner_person_id="usr_usage", granted_micro=100_000,
            created_at=moment, updated_at=moment))
        session.commit()
    with usage.run_context("trn_usage"):
        invocation = usage.before_call(1000, 2000, **({"call_binding": captured} if captured is not None else {}))
    yield SimpleNamespace(module=usage, engine=engine, invocation=invocation, call_binding=captured)
    engine.dispose()


def modern_binding():
    """Explicit synthetic route, validated by the actual owned S02 API."""
    return {"schema_version": 1, "purpose": "task.verify", "provider": "synthetic_provider", "model": "synthetic_model",
        "catalog_model": "synthetic_catalog", "request_id": "modern_usage_request", "captured_tier": "pro",
        "api_mode": "responses", "max_output_tokens": 1500, "reasoning_effort": "low"}


def require_modern_api():
    from app.credits import reserve
    if not all(callable(getattr(reserve, name, None)) for name in ("validate_call_binding", "model_receipt_metadata")):
        pytest.skip("Owned S02 modern receipt validator ABI is not accepted in this source yet")
    return reserve


def actual_modern_receipt(d, *, route=None, include_binding=True, output_error=None, cached_tokens=20):
    """Real wallet reserve/settle and immutable ledger; no provider is called."""
    reserve = require_modern_api()
    route = dict(route or d.call_binding or modern_binding())
    hold = reserve.reserve(account_id="acct_usage", workspace_id="ws_usage", venture_id="ven_usage",
        task_invocation=d.invocation, purpose=route["purpose"], provider=route["provider"], model=route["model"],
        request_id=route["request_id"], amount_micro=1000)
    metadata = {"catalog_model": route["catalog_model"], "actual_catalog_model": "synthetic_actual_catalog",
        "returned_model": "synthetic_provider_snapshot", "output_error": output_error}
    result = reserve.settle(hold, actual_micro=1100, provider_actual_micro=1500, prompt_tokens=120,
        completion_tokens=180, cached_tokens=cached_tokens, model_receipt=metadata,
        call_binding=route if include_binding else None)
    assert result.actual_micro == 1050  # Actual owned funded five-percent tolerance.
    return result


def receipt(d, *, invocation=None, charged=700, actual=700, meta_extra=None, owner=None,
            hold_changes=None, entry_changes=None, prompt_tokens=120, completion_tokens=180):
    from app.credits.ledger import append
    from app.db.models import CreditAccount, CreditReservation
    from app.db.session import serialize_writes
    invocation = invocation or d.invocation
    with Session(d.engine, expire_on_commit=False) as session:
        serialize_writes(session, key="acct_usage")
        account = session.get(CreditAccount, "acct_usage")
        if owner:
            account.owner_person_id = owner
        account.spent_micro += charged
        fields = {"id": "res_" + invocation, "account_id": account.id, "amount_micro": 1000,
            "status": "settled", "settled_micro": charged, "workspace_id": "ws_usage", "purpose": "task.verify",
            "model": "synthetic_model", "provider": "synthetic_provider", "request_id": "request_" + invocation,
            "expires_at": NOW + timedelta(minutes=15), "created_at": NOW, "settled_at": NOW}
        if hasattr(CreditReservation, "task_invocation"):
            fields["task_invocation"] = invocation
        if hasattr(CreditReservation, "venture_id"):
            fields["venture_id"] = "ven_usage"
        fields.update(hold_changes or {})
        hold = CreditReservation(**fields)
        session.add(hold)
        meta = {"task_invocation": invocation, "workspace_id": "ws_usage", "venture_id": "ven_usage"}
        if actual is not None:
            meta["provider_actual_micro"] = actual
        meta.update(meta_extra or {})
        kwargs = {"account": account, "kind": "spend", "amount_micro": -charged, "reason": "Synthetic confirmed usage",
            "actor_kind": "system", "actor_id": "system", "purpose": "task.verify", "model": "synthetic_model",
            "provider": "synthetic_provider", "request_id": "request_" + invocation, "reservation_id": hold.id,
            "idempotency_key": "settle:" + hold.id, "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "meta": meta}
        kwargs.update(entry_changes or {})
        row = append(session, **kwargs)
        session.commit()
        return row.id


def saved(d):
    from app.db.models import CreditLedgerEntry
    from app.db.task_models import Task, TaskRun, TaskEvent
    with Session(d.engine) as session:
        task, run = session.get(Task, "tsk_usage"), session.get(TaskRun, "trn_usage")
        return SimpleNamespace(task_state=task.state, run_state=run.state, wallet=run.wallet_account_id,
            cost=run.cost_micro, tokens=run.tokens_used, reserved=run.reserved_cost_micro, reserved_tokens=run.reserved_tokens,
            blocker=json.loads(task.blocker_json or "{}"), events=[(event.kind, json.loads(event.data_json)) for event in session.scalars(select(TaskEvent))],
            ledger=[(row.id, row.amount_micro, row.meta_json, row.prev_hash, row.entry_hash) for row in session.scalars(select(CreditLedgerEntry))])


def test_crash_after_wallet_settlement_recovers_exactly_once_without_ledger_mutation(usage_db):
    from app.credits.ledger import verify_chain
    d = usage_db
    receipt(d)
    original = saved(d).ledger
    assert d.module.reconcile_usage() == 1
    row = saved(d)
    assert (row.cost, row.tokens, row.reserved, row.reserved_tokens, row.wallet) == (700, 300, 0, 0, "acct_usage")
    assert d.module.reconcile_usage() == 0
    assert len([event for kind, event in saved(d).events if kind == "call_settled"]) == 1
    assert saved(d).ledger == original and verify_chain("acct_usage") == (True, None)


@pytest.mark.parametrize("actual,charged,expected", [(1500, 1000, 1500), (None, 700, 700), (0, 0, 0)])
def test_provider_actual_cost_wins_and_invalid_or_absent_value_uses_signed_spend(usage_db, actual, charged, expected):
    d = usage_db
    receipt(d, actual=actual, charged=charged)
    assert d.module.reconcile_usage() == 1
    assert saved(d).cost == expected and saved(d).reserved == 0


def test_raw_provider_cost_wins_over_wallet_call_cap_without_double_counting_cached_tokens(usage_db):
    d = usage_db
    receipt(d, charged=1000, actual=1000, meta_extra={"raw_provider_actual_micro": 1500, "cached_tokens": 20})
    original = saved(d).ledger
    assert d.module.reconcile_usage() == 1
    assert saved(d).cost == 1500 and saved(d).tokens == 300 and saved(d).reserved == 0
    assert d.module.reconcile_usage() == 0 and saved(d).ledger == original


@pytest.mark.parametrize("value", [True, None, -1, 1.5, "1500"])
def test_present_invalid_raw_cost_never_falls_back_to_charged_cost(usage_db, value):
    d = usage_db
    receipt(d, actual=700, meta_extra={"raw_provider_actual_micro": value})
    original = saved(d).ledger
    assert d.module.reconcile_usage() == 0 and saved(d).ledger == original
    assert saved(d).reserved == 1000 and saved(d).cost == 0 and saved(d).wallet is None


def test_invalid_older_cost_receipt_and_absent_clamped_cost_need_review(usage_db):
    d = usage_db
    receipt(d, actual=True)
    assert d.module.reconcile_usage() == 0 and saved(d).cost == 0 and saved(d).reserved == 1000


def test_missing_raw_and_older_cost_cannot_use_clamped_spend_as_provider_cost(usage_db):
    d = usage_db
    receipt(d, actual=None, hold_changes={"clamped": 1})
    assert d.module.reconcile_usage() == 0 and saved(d).cost == 0 and saved(d).reserved == 1000


@pytest.mark.parametrize("options", [
    {"owner": "usr_other"}, {"meta_extra": {"workspace_id": "ws_other"}}, {"meta_extra": {"venture_id": "ven_other"}},
    {"hold_changes": {"workspace_id": "ws_other"}}, {"entry_changes": {"reservation_id": "res_missing"}},
    {"hold_changes": {"settled_micro": 750}},
    {"entry_changes": {"purpose": "other.purpose"}}, {"entry_changes": {"request_id": "other_request"}},
])
def test_scope_mismatch_is_not_allocated_to_task(usage_db, options):
    d = usage_db
    receipt(d, **options)
    original = saved(d).ledger
    assert d.module.reconcile_usage() == 0
    row = saved(d)
    assert (row.cost, row.tokens, row.reserved, row.reserved_tokens, row.wallet) == (0, 0, 1000, 2000, None)
    assert row.task_state == "input_required" and row.ledger == original


@pytest.mark.parametrize("field", ["prompt_tokens", "completion_tokens"])
def test_missing_confirmed_tokens_retains_reservation_and_blocker(usage_db, field):
    d = usage_db
    receipt(d, **{field: None})
    assert d.module.reconcile_usage() == 0
    assert saved(d).reserved == 1000 and saved(d).reserved_tokens == 2000
    assert saved(d).task_state == "input_required" and saved(d).cost == 0


def test_no_spend_receipt_or_ttl_release_never_implies_zero_usage(usage_db):
    from app.db.models import CreditReservation
    d = usage_db
    with Session(d.engine) as session:
        session.add(CreditReservation(id="res_unproven_release", account_id="acct_usage", amount_micro=1000,
            status="released", workspace_id="ws_usage", request_id="unknown", purpose="task.verify",
            expires_at=NOW - timedelta(days=1), created_at=NOW - timedelta(days=2)))
        session.commit()
    assert d.module.reconcile_usage() == 0
    assert saved(d).reserved == 1000 and saved(d).reserved_tokens == 2000
    assert saved(d).task_state == "input_required"


def test_canceled_run_records_late_usage_and_remains_canceled(usage_db):
    from app.db.task_models import Task, TaskRun
    d = usage_db
    receipt(d)
    with Session(d.engine) as session:
        session.get(Task, "tsk_usage").state = session.get(TaskRun, "trn_usage").state = "canceled"
        session.commit()
    assert d.module.reconcile_usage() == 1
    assert saved(d).task_state == saved(d).run_state == "canceled"
    assert saved(d).cost == 700 and saved(d).reserved == 0


def test_after_call_commit_before_wallet_link_is_recovered_without_double_cost(usage_db):
    d = usage_db
    receipt(d)
    d.module.after_call(d.invocation, 700, 300)
    assert saved(d).wallet is None
    assert d.module.reconcile_usage() == 1
    assert saved(d).wallet == "acct_usage" and saved(d).cost == 700 and saved(d).tokens == 300
    assert d.module.reconcile_usage() == 0


def test_processed_reservations_cannot_starve_later_receipts(usage_db):
    d = usage_db
    receipt(d)
    assert d.module.reconcile_usage(limit=1) == 1
    with d.module.run_context("trn_usage"):
        second = d.module.before_call(1000, 2000)
    receipt(d, invocation=second)
    assert d.module.reconcile_usage(limit=1) == 1
    assert saved(d).cost == 1400 and saved(d).tokens == 600 and saved(d).reserved == 0


def test_ready_receipt_is_prioritized_ahead_of_unknown_call(usage_db):
    d = usage_db
    with d.module.run_context("trn_usage"):
        second = d.module.before_call(1000, 2000)
    receipt(d, invocation=second)
    assert d.module.reconcile_usage(limit=1) == 1
    assert saved(d).cost == 700 and saved(d).reserved == 1000


def test_scoped_legacy_venture_account_is_supported(usage_db):
    from app.db.models import CreditAccount
    d = usage_db
    with Session(d.engine) as session:
        account = session.get(CreditAccount, "acct_usage")
        account.scope, account.workspace_id, account.venture_id = "venture", "ws_usage", "ven_usage"
        session.commit()
    receipt(d)
    assert d.module.reconcile_usage() == 1
    assert saved(d).wallet == "acct_usage" and saved(d).cost == 700


def test_conflicting_run_wallet_binding_requires_reconciliation(usage_db):
    from app.db.task_models import TaskRun
    d = usage_db
    receipt(d)
    with Session(d.engine) as session:
        session.get(TaskRun, "trn_usage").wallet_account_id = "other_wallet"
        session.commit()
    assert d.module.reconcile_usage() == 0
    assert saved(d).wallet == "other_wallet" and saved(d).reserved == 1000 and saved(d).cost == 0


def test_live_worker_without_ledger_is_not_prematurely_blocked(usage_db):
    from app.db.models import Job
    from app.db.task_models import TaskStep
    d = usage_db
    with Session(d.engine) as session:
        session.add(Job(id="job_live_usage", kind="task.run", workspace_id="ws_usage", venture_id="ven_usage",
            payload_json="{}", status="leased", locked_by="synthetic_worker", locked_at=NOW, created_at=NOW))
        session.get(TaskStep, "tst_usage").job_id = "job_live_usage"
        session.commit()
    assert d.module.reconcile_usage() == 0
    assert saved(d).task_state == "working" and saved(d).reserved == 1000


def test_postgres_pending_query_compiles_without_provider_or_database_calls(usage_db, monkeypatch):
    from sqlalchemy.dialects import postgresql
    statements = []
    class ReadOnlySession:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return None
        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
        def scalars(self, statement):
            statements.append(str(statement.compile(dialect=postgresql.dialect())))
            return SimpleNamespace(all=lambda: [])
    monkeypatch.setattr(usage_db.module, "db", lambda: ReadOnlySession)
    assert usage_db.module.reconcile_usage() == 0
    assert "CAST(task_events.data_json AS JSONB)" in statements[0] and "NOT (EXISTS" in statements[0]


def test_recovered_receipt_does_not_clear_unknown_result_blocker(usage_db):
    d = usage_db
    d.module.abort_call(d.invocation, definitive=False)
    receipt(d)
    assert d.module.reconcile_usage() == 1
    assert saved(d).reserved == 0 and saved(d).task_state == "input_required"


@pytest.mark.parametrize("limit", [0, True, 1001])
def test_reconciliation_limit_is_bounded(usage_db, limit):
    with pytest.raises(ValueError, match="limit_invalid"):
        usage_db.module.reconcile_usage(limit)


def test_actual_wallet_funded_overage_recovers_task_counters_once(usage_db, monkeypatch):
    from app.credits import reserve
    from app.credits.ledger import verify_chain
    d = usage_db
    monkeypatch.setattr(reserve, "reservation_ttl_seconds", lambda: 900)
    hold = reserve.reserve(account_id="acct_usage", workspace_id="ws_usage", venture_id="ven_usage",
        task_invocation=d.invocation, purpose="task.verify", provider="synthetic_provider", model="synthetic_model",
        request_id="request_" + d.invocation, amount_micro=1000)
    settled = reserve.settle(hold, actual_micro=1100, prompt_tokens=120, completion_tokens=180, cached_tokens=20)
    assert settled.actual_micro == 1050  # S02's actual funded allowance, not a fabricated receipt.
    original = saved(d).ledger
    assert d.module.reconcile_usage() == 1
    row = saved(d)
    assert (row.cost, row.tokens, row.reserved, row.reserved_tokens, row.wallet) == (1100, 300, 0, 0, "acct_usage")
    assert d.module.reconcile_usage() == 0 and saved(d).ledger == original and verify_chain("acct_usage") == (True, None)


def test_signed_receipt_above_defined_wallet_allowance_is_not_accepted(usage_db):
    d = usage_db
    receipt(d, charged=1051, actual=1100)
    original = saved(d).ledger
    assert d.module.reconcile_usage() == 0 and saved(d).ledger == original
    assert saved(d).reserved == 1000 and saved(d).wallet is None and saved(d).cost == 0


@pytest.mark.parametrize("value", [True, 1.5, "1", None, -1])
@pytest.mark.parametrize("field", ["estimate_micro", "max_tokens"])
def test_task_reservation_rejects_non_integer_or_negative_counters_before_mutation(usage_db, value, field):
    d = usage_db
    before = saved(d)
    with d.module.run_context("trn_usage"), pytest.raises(ValueError, match="task_usage_invalid"):
        d.module.before_call(**{"estimate_micro": 1, "max_tokens": 1, field: value})
    after = saved(d)
    assert (after.reserved, after.reserved_tokens, after.events) == (before.reserved, before.reserved_tokens, before.events)


@pytest.mark.parametrize("value", [True, 1.5, "1", None, -1])
@pytest.mark.parametrize("field", ["actual_micro", "tokens_used"])
def test_task_settlement_rejects_non_integer_or_negative_counters_before_mutation(usage_db, value, field):
    d = usage_db
    before = saved(d)
    with pytest.raises(ValueError, match="task_usage_invalid"):
        d.module.after_call(d.invocation, **{"actual_micro": 1, "tokens_used": 1, field: value})
    after = saved(d)
    assert (after.cost, after.tokens, after.reserved, after.reserved_tokens, after.events) == (
        before.cost, before.tokens, before.reserved, before.reserved_tokens, before.events)


def test_successful_none_and_idempotent_after_call_are_not_failed_receipts(usage_db):
    d = usage_db
    assert d.module.after_call(None, 0, 0) is None
    assert d.module.after_call(d.invocation, 0, 0) is None
    before = saved(d)
    assert d.module.after_call(d.invocation, 0, 0) is None
    assert saved(d).events == before.events and saved(d).reserved == saved(d).reserved_tokens == 0


@pytest.mark.parametrize("with_ledger", [False, True])
def test_settled_no_hold_usage_is_not_reclassified_or_starved_by_missing_wallet_link(usage_db, with_ledger):
    from app.credits.ledger import append
    from app.db.models import CreditAccount
    d = usage_db
    d.module.after_call(d.invocation, 700, 300)
    if with_ledger:
        with Session(d.engine) as session:
            append(session, account=session.get(CreditAccount, "acct_usage"), kind="spend", amount_micro=0,
                reason="Synthetic BYOK passthrough usage", actor_kind="system", actor_id="system",
                idempotency_key="byok:" + d.invocation, prompt_tokens=120, completion_tokens=180,
                meta={"task_invocation": d.invocation, "workspace_id": "ws_usage", "venture_id": "ven_usage", "passthrough": True})
            session.commit()
    before = saved(d)
    assert d.module.reconcile_usage(limit=1) == 0
    assert saved(d).events == before.events and saved(d).task_state == "working" and saved(d).wallet is None
    with d.module.run_context("trn_usage"):
        second = d.module.before_call(1000, 2000)
    receipt(d, invocation=second)
    assert d.module.reconcile_usage(limit=1) == 1
    assert saved(d).cost == 1400 and saved(d).tokens == 600 and saved(d).reserved == 0


def test_modern_presence_without_owned_validator_retains_usage_and_immutable_ledger(usage_db, monkeypatch):
    from app.credits import reserve
    d = usage_db
    # A rejected synthetic immutable receipt is not a successful wallet proof.
    receipt(d, meta_extra={"call_binding": modern_binding(), "catalog_model": "synthetic_catalog",
        "actual_catalog_model": "synthetic_actual_catalog", "returned_model": "synthetic_snapshot", "output_error": None})
    original = saved(d).ledger
    monkeypatch.delattr(reserve, "validate_call_binding", raising=False)
    monkeypatch.delattr(reserve, "model_receipt_metadata", raising=False)
    assert d.module.reconcile_usage() == 0
    row = saved(d)
    assert (row.cost, row.tokens, row.reserved, row.reserved_tokens, row.wallet) == (0, 0, 1000, 2000, None)
    assert row.task_state == "input_required" and row.ledger == original
    assert any(kind == "call_uncertain" for kind, _ in row.events)


@pytest.mark.parametrize("usage_db", ["modern"], indirect=True)
def test_actual_modern_receipt_recovers_raw_cost_full_tokens_exactly_once(usage_db):
    from app.credits.ledger import verify_chain
    d = usage_db
    actual_modern_receipt(d)
    original = saved(d).ledger
    assert d.module.reconcile_usage() == 1
    row = saved(d)
    assert (row.cost, row.tokens, row.reserved, row.reserved_tokens, row.wallet) == (1500, 300, 0, 0, "acct_usage")
    assert d.module.reconcile_usage() == 0 and saved(d).ledger == original
    assert verify_chain("acct_usage") == (True, None)
    assert len([event for kind, event in saved(d).events if kind == "call_settled"]) == 1


@pytest.mark.parametrize("usage_db", ["modern"], indirect=True)
@pytest.mark.parametrize("field", ["purpose", "provider", "model", "catalog_model", "request_id", "captured_tier", "api_mode", "max_output_tokens", "reasoning_effort"])
def test_valid_settled_modern_route_cannot_replace_original_task_admission(usage_db, field):
    d = usage_db
    route = dict(d.call_binding)
    route[field] = {"captured_tier": "flash", "api_mode": "chat_completions", "max_output_tokens": 1600,
        "reasoning_effort": "high"}.get(field, "different_original_value")
    actual_modern_receipt(d, route=route)
    original = saved(d).ledger
    assert d.module.reconcile_usage() == 0
    row = saved(d)
    assert (row.cost, row.tokens, row.reserved, row.reserved_tokens, row.wallet) == (0, 0, 1000, 2000, None)
    assert row.ledger == original and row.task_state == "input_required"


@pytest.mark.parametrize("usage_db", ["modern"], indirect=True)
def test_modern_original_binding_cannot_be_stripped_from_confirmed_wallet_receipt(usage_db):
    d = usage_db
    actual_modern_receipt(d, include_binding=False)
    original = saved(d).ledger
    assert d.module.reconcile_usage() == 0 and saved(d).ledger == original
    assert saved(d).reserved == 1000 and saved(d).cost == 0 and saved(d).wallet is None


def test_modern_model_metadata_without_any_original_call_binding_cannot_upgrade_legacy_authority(usage_db):
    d = usage_db
    actual_modern_receipt(d, include_binding=False)
    original = saved(d).ledger
    assert d.module.reconcile_usage() == 0 and saved(d).ledger == original
    assert (saved(d).reserved, saved(d).reserved_tokens, saved(d).cost, saved(d).wallet) == (1000, 2000, 0, None)
    assert saved(d).task_state == "input_required"


@pytest.mark.parametrize("usage_db", ["modern"], indirect=True)
@pytest.mark.parametrize("output_error", ["model_mismatch", "incomplete", "refused", "filtered"])
def test_known_paid_output_error_still_accounts_confirmed_usage_without_applying_output(usage_db, output_error):
    from app.db.task_models import TaskStep
    d = usage_db
    actual_modern_receipt(d, output_error=output_error)
    original = saved(d).ledger
    with Session(d.engine) as session:
        checkpoint = session.get(TaskStep, "tst_usage").evidence_json
    assert d.module.reconcile_usage() == 1
    row = saved(d)
    assert (row.cost, row.tokens, row.reserved, row.reserved_tokens) == (1500, 300, 0, 0)
    assert row.ledger == original
    with Session(d.engine) as session:
        step = session.get(TaskStep, "tst_usage")
        assert step.state == "working" and step.evidence_json == checkpoint
    assert d.module.reconcile_usage() == 0 and saved(d).ledger == original


@pytest.mark.parametrize("usage_db", ["modern"], indirect=True)
@pytest.mark.parametrize("case", ["partial_call_binding", "partial_model_fields", "unknown_output_error", "cached_exceeds_prompt"])
def test_malformed_modern_immutable_receipt_never_releases_task_reservation(usage_db, case):
    d = usage_db
    route = dict(d.call_binding)
    metadata = {"call_binding": route, "catalog_model": "synthetic_catalog", "actual_catalog_model": "synthetic_actual_catalog",
        "returned_model": "synthetic_snapshot", "output_error": None, "cached_tokens": 20}
    if case == "partial_call_binding":
        route.pop("reasoning_effort")
    elif case == "partial_model_fields":
        metadata.pop("returned_model")
    elif case == "unknown_output_error":
        metadata["output_error"] = "unrecognized_paid_error"
    else:
        metadata["cached_tokens"] = 121
    # Deliberately malformed, synthetic immutable rows model historical bad
    # metadata. No owned successful settle or wallet proof is claimed here.
    receipt(d, meta_extra=metadata, hold_changes={"request_id": d.call_binding["request_id"]},
        entry_changes={"request_id": d.call_binding["request_id"]})
    original = saved(d).ledger
    assert d.module.reconcile_usage() == 0 and saved(d).ledger == original
    assert (saved(d).reserved, saved(d).reserved_tokens, saved(d).cost, saved(d).wallet) == (1000, 2000, 0, None)

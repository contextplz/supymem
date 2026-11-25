"""Trusted receipt recovery against scoped real SQLite ledger and synthetic S03 hooks."""
from __future__ import annotations

import json

import pytest


def task_hooks(monkeypatch):
    from app.credits import gate
    calls, finalized = [], {}
    def run(method, *args, **kwargs):
        calls.append((method, args, kwargs))
        if method == "before_call":
            return "inv_receipt"
        if method == "after_call":
            previous = finalized.setdefault(args[0], kwargs)
            assert previous == kwargs
        return None
    monkeypatch.setattr(gate, "_task_usage", run)
    return gate, calls, finalized


def held_receipt(account, *, invocation="inv_receipt", requested=7, raw=10):
    from app.credits.reserve import reserve
    vid = account.attribution_venture_id
    usage = {"source": "platform", "account_id": account.id, "estimate_micro": 7,
             "workspace_id": "ws_wallet:" + vid, "venture_id": vid, "purpose": "task.snapshot",
             "provider": "synthetic", "model": "priced-snapshot", "request_id": "run:review:1",
             "task_invocation": invocation, "actual_micro": requested, "provider_actual_micro": raw,
             "prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30, "cached_tokens": 8}
    hold = reserve(account_id=account.id, amount_micro=7, purpose=usage["purpose"], provider=usage["provider"],
                   model=usage["model"], workspace_id=usage["workspace_id"], venture_id=vid,
                   request_id=usage["request_id"], task_invocation=invocation)
    usage["reservation_id"] = hold.reservation_id
    return usage


def binding(usage):
    return {name: usage[name] for name in ("workspace_id", "venture_id", "request_id", "source", "task_invocation")}


def spend(usage):
    from app.credits.ledger import find_by_idempotency_key
    return find_by_idempotency_key("settle:" + usage["reservation_id"])


def test_capped_wallet_cost_and_raw_task_cost_are_bound_on_replay(account, monkeypatch):
    from app.credits.accounts import get_account_by_id
    from app.credits.ledger import verify_chain
    gate, calls, finalized = task_hooks(monkeypatch)
    usage = held_receipt(account)
    monkeypatch.setattr(gate, "actual_llm_micro", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("saved receipts are never repriced")))
    assert gate.reconcile_receipt(usage, binding(usage)) == 7
    first = spend(usage)
    assert (first.prompt_tokens, first.completion_tokens, first.amount_micro) == (10, 20, -7)
    meta = json.loads(first.meta_json)
    assert (meta["provider_actual_micro"], meta["raw_provider_actual_micro"], meta["cached_tokens"]) == (7, 10, 8)
    assert meta["task_invocation"] == "inv_receipt"
    assert finalized == {"inv_receipt": {"actual_micro": 10, "tokens_used": 30}}
    assert gate.reconcile_receipt(usage, binding(usage)) == 7
    assert spend(usage).id == first.id and get_account_by_id(account.id).spent_micro == 7
    assert not any(method == "before_call" for method, _, _ in calls)
    assert verify_chain(account.id) == (True, None)


@pytest.mark.parametrize("name,value", [("account_id", "other"), ("estimate_micro", 8), ("workspace_id", "other"),
    ("venture_id", "other"), ("purpose", "other"), ("model", "other"), ("provider", "other"),
    ("request_id", "other"), ("task_invocation", "other")])
def test_changed_immutable_custody_refuses_before_settle_or_counter(account, monkeypatch, name, value):
    from app.credits.accounts import get_account_by_id
    from app.credits.errors import CreditsUnavailable
    gate, calls, _ = task_hooks(monkeypatch)
    usage = held_receipt(account)
    changed = {**usage, name: value}
    with pytest.raises(CreditsUnavailable):
        gate.reconcile_receipt(changed, binding(usage))
    assert get_account_by_id(account.id).spent_micro == 0 and not calls and spend(usage) is None


@pytest.mark.parametrize("name,value", [("actual_micro", 6), ("provider_actual_micro", 11), ("cached_tokens", 7),
    ("prompt_tokens", 9), ("completion_tokens", 21), ("total_tokens", 29)])
def test_changed_settled_cost_or_counts_refuses_before_counter(account, monkeypatch, name, value):
    from app.credits.errors import CreditsUnavailable
    gate, calls, _ = task_hooks(monkeypatch)
    usage = held_receipt(account)
    assert gate.reconcile_receipt(usage, binding(usage)) == 7
    calls.clear()
    with pytest.raises(CreditsUnavailable):
        gate.reconcile_receipt({**usage, name: value}, binding(usage))
    assert not calls


@pytest.mark.parametrize("changes", [{"prompt_tokens": None}, {"completion_tokens": True}, {"total_tokens": 31},
    {"cached_tokens": 11}, {"actual_micro": None}, {"provider_actual_micro": True}])
def test_unknown_receipt_never_invents_zero_or_advances_counter(account, monkeypatch, changes):
    from app.credits.accounts import get_account_by_id
    from app.credits.errors import CreditsUnavailable
    gate, calls, _ = task_hooks(monkeypatch)
    usage = held_receipt(account)
    with pytest.raises(CreditsUnavailable):
        gate.reconcile_receipt({**usage, **changes}, binding(usage))
    assert not calls and get_account_by_id(account.id).spent_micro == 0


def test_settle_missing_confirmation_blocks_counter_and_later_recovers_same_hold(account, monkeypatch):
    from app.credits.errors import CreditsUnavailable
    gate, calls, _ = task_hooks(monkeypatch)
    usage = held_receipt(account)
    original = gate.settle
    def lost_confirmation(*args, **kwargs):
        original(*args, **kwargs)
        return None
    monkeypatch.setattr(gate, "settle", lost_confirmation)
    with pytest.raises(CreditsUnavailable):
        gate.reconcile_receipt(usage, binding(usage))
    assert not calls and spend(usage).amount_micro == -7
    monkeypatch.setattr(gate, "settle", original)
    assert gate.reconcile_receipt(usage, binding(usage)) == 7
    assert calls[-1][0] == "after_call"


def test_passthrough_tagged_ledger_recovers_raw_task_cost_without_hold(account, monkeypatch):
    from app.credits.ledger import find_by_idempotency_key
    gate, calls, finalized = task_hooks(monkeypatch)
    with gate.charge_sync(account_id=account.id, payer="founder", purpose="task.byok", estimate_micro=50,
        workspace_id="ws_wallet:" + account.attribution_venture_id, venture_id=account.attribution_venture_id,
        request_id="run:byok:1", model="priced-snapshot", provider="synthetic") as meter:
        meter.record(prompt_tokens=10, completion_tokens=20, cached_tokens=8)
        meter.record_micro(10)
        usage = gate.captured_receipt(meter)
        assert meter.hold is None
    entry = find_by_idempotency_key("passthrough:run:byok:1")
    meta = json.loads(entry.meta_json)
    assert entry.amount_micro == 0 and meta["task_invocation"] == "inv_receipt" and meta["provider_actual_micro"] == 10
    calls.clear()
    assert gate.reconcile_receipt(usage, binding(usage)) == 0
    assert finalized == {"inv_receipt": {"actual_micro": 10, "tokens_used": 30}}
    assert calls == [("after_call", ("inv_receipt",), {"actual_micro": 10, "tokens_used": 30})]


def test_walletless_byok_requires_complete_trusted_server_binding(account, monkeypatch):
    from app.credits.errors import CreditsUnavailable
    gate, calls, finalized = task_hooks(monkeypatch)
    usage = {"source": "byok", "reservation_id": None, "account_id": None, "estimate_micro": 50,
             "workspace_id": "trusted-desk", "venture_id": None, "purpose": "task.byok", "provider": "synthetic",
             "model": "priced-snapshot", "request_id": "trusted:run:1", "task_invocation": "inv_receipt",
             "actual_micro": 10, "provider_actual_micro": 10,
             "prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30, "cached_tokens": 8}
    with pytest.raises(CreditsUnavailable, match="trusted journal binding"):
        gate.reconcile_receipt(usage)
    assert not calls
    assert gate.reconcile_receipt(usage, binding(usage)) == 0
    assert finalized["inv_receipt"] == {"actual_micro": 10, "tokens_used": 30}


def test_missing_task_module_for_saved_invocation_refuses(account, monkeypatch):
    import sys
    from app.credits import gate
    from app.credits.errors import CreditsUnavailable
    usage = held_receipt(account)
    monkeypatch.setitem(sys.modules, "app.tasks", None)
    with pytest.raises(CreditsUnavailable, match="task usage"):
        gate.reconcile_receipt(usage, binding(usage))


def test_confirmed_release_precedes_successful_none_task_abort(account, monkeypatch):
    from app.credits.accounts import get_account_by_id
    from app.credits._txn import session_factory
    from app.db.models import CreditReservation
    gate, calls, _ = task_hooks(monkeypatch)
    usage = held_receipt(account)
    usage["definitive_unspent"] = True
    assert gate.reconcile_receipt(usage, binding(usage)) == 0
    assert calls == [("abort_call", ("inv_receipt",), {"definitive": True})]
    with session_factory()() as session:
        assert session.get(CreditReservation, usage["reservation_id"]).status == "released"
    assert get_account_by_id(account.id).reserved_micro == 0


def test_accepted_no_task_journal_missing_binding_uses_exact_hold_proof(account, monkeypatch):
    gate, calls, _ = task_hooks(monkeypatch)
    usage = held_receipt(account, invocation=None, requested=7, raw=7)
    assert gate.reconcile_receipt(usage, binding(usage)) == 7
    partial = {name: value for name, value in usage.items() if name not in {"workspace_id", "venture_id", "purpose", "request_id", "task_invocation"}}
    assert gate.reconcile_receipt(partial, binding(usage)) == 7
    assert not calls


def legacy_receipt(account, *, partial_meta=False, clamped=False):
    from app.credits._txn import naive_utc, session_factory
    from app.credits.ledger import append
    from app.db.models import CreditAccount, CreditReservation
    now = naive_utc()
    vid, wid, aid = account.attribution_venture_id, "ws_wallet:" + account.attribution_venture_id, "acc_legacy_bound"
    with session_factory()() as session:
        legacy = CreditAccount(id=aid, scope="venture", venture_id=vid, workspace_id=wid, owner_person_id=None,
            status="frozen", granted_micro=100, spent_micro=0, reserved_micro=0, version=0, created_at=now, updated_at=now)
        session.add(legacy)
        session.flush()
        append(session, account=legacy, kind="grant", amount_micro=100, reason="historical fixture", actor_kind="system", actor_id="test", idempotency_key="old:grant")
        legacy.spent_micro = 7
        hold = CreditReservation(id="crv_legacy_bound", account_id=aid, amount_micro=7, settled_micro=7, status="settled",
            purpose="research.deep", model="historic-model", provider="historic-provider", workspace_id=wid, venture_id=None,
            task_invocation=None, request_id="oldrun:review:1", clamped=int(clamped), expires_at=now, created_at=now, settled_at=now)
        session.add(hold)
        meta = {"clamped": clamped, "hold_micro": 7}
        if partial_meta:
            meta["task_invocation"] = None
        append(session, account=legacy, kind="spend", amount_micro=-7, reason="historical fixture", actor_kind="system", actor_id="test",
            idempotency_key="settle:" + hold.id, purpose=hold.purpose, model=hold.model, provider=hold.provider,
            prompt_tokens=2, completion_tokens=3, reservation_id=hold.id, request_id=hold.request_id, meta=meta)
        session.commit()
    usage = {"source": "platform", "reservation_id": "crv_legacy_bound", "account_id": aid, "estimate_micro": 7,
        "model": "historic-model", "provider": "historic-provider", "prompt_tokens": 2, "completion_tokens": 3,
        "actual_micro": 10 if clamped else 7}
    expected = {"workspace_id": wid, "venture_id": vid, "request_id": "oldrun:review:1"}
    return usage, expected


def test_exact_legacy_funded_receipt_needs_no_live_price_or_task_counter(account, monkeypatch):
    from app.credits.ledger import verify_chain
    gate, calls, _ = task_hooks(monkeypatch)
    usage, expected = legacy_receipt(account)
    monkeypatch.setattr(gate, "actual_llm_micro", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no historic repricing")))
    monkeypatch.setattr(gate, "settle", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no historic settlement rewrite")))
    assert gate.reconcile_receipt(usage, expected) == 7
    assert not calls and verify_chain(usage["account_id"]) == (True, None)


def test_expanded_legacy_metadata_cannot_fall_back(account, monkeypatch):
    from app.credits.errors import CreditsUnavailable
    gate, calls, _ = task_hooks(monkeypatch)
    usage, expected = legacy_receipt(account, partial_meta=True)
    with pytest.raises(CreditsUnavailable):
        gate.reconcile_receipt(usage, expected)
    assert not calls


@pytest.mark.parametrize("raw", [True, -1, 6])
def test_reserve_rejects_invalid_raw_cost_without_settling(account, raw):
    from app.credits.reserve import Hold, settle
    from app.credits.ledger import find_by_idempotency_key
    usage = held_receipt(account)
    with pytest.raises(ValueError):
        settle(Hold(usage["reservation_id"], account.id, 7), actual_micro=7, provider_actual_micro=raw)
    assert find_by_idempotency_key("settle:" + usage["reservation_id"]) is None


def test_legacy_clamped_paid_output_replays_stored_charge_and_cache_shape(account, monkeypatch):
    gate, calls, _ = task_hooks(monkeypatch)
    usage, expected = legacy_receipt(account, clamped=True)
    usage.update(provider_actual_micro=11, cached_tokens=2, total_tokens=5, response=b"durable output bytes")
    before = dict(usage)
    monkeypatch.setattr(gate, "settle", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("already funded")))
    for _ in range(2):
        assert gate.reconcile_receipt(usage, expected) == 7
    assert usage == before and usage["response"] == b"durable output bytes" and not calls


@pytest.mark.parametrize("cached", [True, -1, 3])
def test_legacy_cached_counts_are_validated_without_retroactive_pricing(account, monkeypatch, cached):
    from app.credits.errors import CreditsUnavailable
    gate, calls, _ = task_hooks(monkeypatch)
    usage, expected = legacy_receipt(account)
    usage["cached_tokens"] = cached
    with pytest.raises(CreditsUnavailable):
        gate.reconcile_receipt(usage, expected)
    assert not calls


def test_pool_child_is_rejected_before_any_financial_or_task_mutation(account, monkeypatch):
    from datetime import timedelta
    from types import SimpleNamespace
    from app.credits import pools
    from app.credits._txn import naive_utc, session_factory
    from app.credits.accounts import get_account_by_id
    from app.credits.errors import CreditsUnavailable
    from app.db.models import CreditAccount, CreditReservation
    gate, calls, _ = task_hooks(monkeypatch)
    vid = account.attribution_venture_id
    run = SimpleNamespace(id="run_pool_guard", runtime_version=1, reservation_id=None, closed_at=None,
                          state="running", workspace_id="ws_wallet:" + vid, venture_id=vid)
    with session_factory()() as session:
        wallet = session.get(CreditAccount, account.id)
        pool = pools.create_pool(session, account=wallet, run=run, reservation_id="crv_pool_guard", amount_micro=100,
                                 expires_at=naive_utc() + timedelta(hours=1))
        child = pools.allocate(session, pools.LockedPool(account=wallet, pool=pool, run=run), reservation_id="crv_child_guard",
                               amount_micro=7, purpose="task.child", model="snapshot", provider="synthetic")
        session.commit()
    usage = {"source": "platform", "reservation_id": child.reservation_id, "account_id": account.id,
             "estimate_micro": 7, "workspace_id": run.workspace_id, "venture_id": vid, "purpose": "task.child",
             "model": "snapshot", "provider": "synthetic", "request_id": run.id, "task_invocation": None,
             "actual_micro": 7, "provider_actual_micro": 10,
             "prompt_tokens": 10, "completion_tokens": 20, "cached_tokens": 8, "total_tokens": 30}
    monkeypatch.setattr(gate, "settle", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("child settlement must never be entered")))
    with pytest.raises(CreditsUnavailable, match="scope mismatch"):
        gate.reconcile_receipt(usage, binding(usage))
    with session_factory()() as session:
        assert session.get(CreditReservation, child.reservation_id).status == "active"
        assert session.get(CreditReservation, "crv_pool_guard").remaining_micro == 93
    assert get_account_by_id(account.id).reserved_micro == 100 and not calls


@pytest.mark.parametrize("source,estimate", [("platform", 7), ("byok", 7), ("free", 0)])
def test_tokenless_fixed_event_replays_validated_zero_cache(account, monkeypatch, source, estimate):
    from app.credits.ledger import entries_for_account
    gate, calls, finalized = task_hooks(monkeypatch)
    with gate.charge_sync(account_id=account.id, payer=None if source == "platform" else "founder",
        purpose="research.search", estimate_micro=estimate, provider="synthetic", model=None,
        workspace_id="ws_wallet:" + account.attribution_venture_id, venture_id=account.attribution_venture_id,
        request_id="fixed:" + source) as meter:
        meter.source = source
        meter.record_micro(estimate)
        usage = gate.captured_receipt(meter)
    # Previous perform receipts captured cost but omitted tokenless count fields.
    for name in ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens"):
        usage.pop(name)
    rows = [(row.id, row.entry_hash) for row in entries_for_account(account.id)["items"]]
    calls.clear()
    assert gate.reconcile_receipt(usage, binding(usage)) == (estimate if source == "platform" else 0)
    assert [(row.id, row.entry_hash) for row in entries_for_account(account.id)["items"]] == rows
    assert calls == [("after_call", ("inv_receipt",), {"actual_micro": estimate, "tokens_used": 0})]
    assert finalized["inv_receipt"] == {"actual_micro": estimate, "tokens_used": 0}


@pytest.fixture
def local_journal(credits_env, monkeypatch):
    from types import SimpleNamespace
    from app.core.operation_receipts import OperationJournal
    from app.credits import gate, local_meter
    from app.db.models import Venture
    from app.db.supymem_models import ResearchDossier
    from app.research.metered import perform
    from app.state import db
    from core.clock import utcnow
    from tests.unit.credits.test_core_model_receipts import DIGEST, binding as model_binding, sdk

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    with db()() as session:
        session.add(Venture(id="ven_receipt", workspace_id="ws_receipt", name="Receipt test",
            raw_idea="Receipt test", stage="draft", created_at=utcnow(), updated_at=utcnow()))
        session.flush()
        session.add(ResearchDossier(id="rsd_receipt", workspace_id="ws_receipt", venture_id="ven_receipt",
            kind="deep", state="running", summary=json.dumps({"_runtime": {"operations": {}}}), created_at=utcnow()))
        session.commit()

    def read(did):
        with db()() as session:
            row = session.get(ResearchDossier, did)
            session.expunge(row)
            return row

    def document(did):
        return json.loads(read(did).summary)

    def mutate(did, payload, update):
        with db()() as session:
            row = session.get(ResearchDossier, did)
            doc = json.loads(row.summary)
            result = update(row, doc, session)
            row.summary = json.dumps(doc)
            session.commit()
            return result

    def no_wallet(*args, **kwargs):
        pytest.fail("local settlement must not use hosted money")

    for name in ("enforcement_enabled", "resolve_charge_target", "get_account_by_id", "reserve", "settle", "append"):
        monkeypatch.setattr(gate, name, no_wallet)
    journal = OperationJournal(read=read, mutate=mutate, document=document,
        before_call=lambda *args: None, prefix="research")
    calls = []

    def invoke(key, domain, *, modern=True, callback=None, attempt=1):
        def provider():
            calls.append(key)
            if callback:
                return callback()
            response = sdk()
            response.choices[0].message.content = json.dumps({"domain": domain})
            return response
        return perform("rsd_receipt", key, {"_job_attempt": attempt, "_worker_id": "worker_receipt"},
            purpose="research.deep", provider="azure_openai", model="synthetic-deployment",
            callback=provider, estimate=1200, token_ceiling=200, journal=journal, parse=json.loads,
            byok=True, provider_identity=DIGEST,
            model_binding=model_binding() if modern else None, request_digest=DIGEST if modern else None)

    return SimpleNamespace(invoke=invoke, calls=calls, gate=gate, meter=local_meter, journal=journal,
        saved=lambda key: journal.operation("rsd_receipt", key))


@pytest.mark.parametrize("domain", ["industrial procurement", "home cooking"])
@pytest.mark.parametrize("modern", [False, True])
def test_local_journal_settles_sequential_calls_and_same_operation_retry(local_journal, domain, modern):
    flow = local_journal
    for key in ("first", "second", "first"):
        assert flow.invoke(key, domain, modern=modern) == {"domain": domain}
        assert flow.saved(key)["state"] == "completed"
        assert flow.saved(key)["usage"]["settled"] is True
        assert flow.saved(key)["usage"]["credits_micro"] == 0
    assert flow.calls == ["first", "second"]
    assert flow.meter.snapshot()["month_cost_usd_micro"] > 0
    with flow.meter._database() as connection:
        assert [row[0] for row in connection.execute("SELECT status FROM calls")] == ["confirmed", "confirmed"]


@pytest.mark.parametrize("domain", ["industrial procurement", "home cooking"])
@pytest.mark.parametrize("modern,interruption", [(True, "pricing"), (True, "finish"), (False, "finish")])
def test_local_journal_recovers_checkpoint_before_meter_finish(local_journal, monkeypatch, domain, modern, interruption):
    from app.core.jobs import RetryableError
    from app.credits.errors import CreditsUnavailable

    flow = local_journal
    module, method = (flow.gate, "record_model_receipt") if interruption == "pricing" else (flow.meter, "finish")
    interrupted_meters = []
    def interrupted(*args, **kwargs):
        interrupted_meters.append(args[0])
        if interruption == "finish":
            raise SystemExit("synthetic interrupted local settlement")
        raise OSError("synthetic interrupted local settlement")
    with monkeypatch.context() as patch:
        patch.setattr(module, method, interrupted)
        with pytest.raises((RetryableError, CreditsUnavailable, SystemExit)):
            flow.invoke("first", domain, modern=modern)
    assert flow.saved("first")["state"] in {"provider_responded", "responded"}
    if not modern:
        usage = flow.saved("first")["usage"]
        for name in ("model_binding", "request_digest", "token_ceiling"):
            usage.pop(name)
        flow.journal.save_operation("rsd_receipt", "first", {"usage": usage})
    assert flow.invoke("first", domain, modern=modern, attempt=2) == {"domain": domain}
    first_cost = flow.meter.snapshot()["month_cost_usd_micro"]
    flow.meter.finish(interrupted_meters[0])
    assert flow.invoke("second", domain, modern=modern) == {"domain": domain}
    assert flow.invoke("first", domain, modern=modern, attempt=2) == {"domain": domain}
    assert flow.calls == ["first", "second"]
    assert first_cost > 0 and flow.meter.snapshot()["month_cost_usd_micro"] == first_cost * 2


@pytest.mark.parametrize("domain", ["industrial procurement", "home cooking"])
@pytest.mark.parametrize("modern", [False, True])
@pytest.mark.parametrize("changed", ["request_id", "workspace_id", "venture_id", "provider_identity", "request_digest", "estimate_micro"])
def test_local_checkpoint_recovery_refuses_changed_custody(local_journal, monkeypatch, domain, modern, changed):
    from app.credits.errors import CreditsUnavailable

    flow = local_journal
    with monkeypatch.context() as patch:
        patch.setattr(flow.meter, "finish", lambda *_: (_ for _ in ()).throw(SystemExit("synthetic interrupted settlement")))
        with pytest.raises(SystemExit):
            flow.invoke("first", domain, modern=modern)
    usage = flow.saved("first")["usage"]
    altered = usage | {changed: usage[changed] + 1 if changed == "estimate_micro" else "a" * 64 if changed == "request_digest" else "another-scope"}
    with pytest.raises(CreditsUnavailable):
        flow.gate.reconcile_receipt(altered)
    with flow.meter._database() as connection:
        row = connection.execute("SELECT status, cost_usd_micro FROM calls").fetchone()
        assert (row["status"], row["cost_usd_micro"]) == ("pending", None)
    assert flow.calls == ["first"]


@pytest.mark.parametrize("domain", ["industrial procurement", "home cooking"])
def test_local_journal_retry_preserves_monthly_cap(local_journal, domain):
    from app.core.jobs import FatalError
    from app.core.local_secrets import update_settings

    flow = local_journal
    update_settings("spend", {"monthly_spend_cap": "0.0012"}, {})
    assert flow.invoke("first", domain) == {"domain": domain}
    with pytest.raises(FatalError, match="research_local_spend_limit"):
        flow.invoke("second", domain)
    assert flow.invoke("first", domain, attempt=2) == {"domain": domain}
    assert flow.calls == ["first"]
    assert flow.meter.snapshot()["month_cost_usd_micro"] == 248
    update_settings("spend", {"monthly_spend_cap": "0.01"}, {})
    assert flow.invoke("second", domain, attempt=2) == {"domain": domain}
    assert flow.calls == ["first", "second"]


@pytest.mark.parametrize("domain", ["industrial procurement", "home cooking"])
@pytest.mark.parametrize("rejected", [False, True])
def test_local_journal_rejected_calls_are_unspent_and_unknown_calls_stay_fenced(local_journal, domain, rejected):
    import httpx
    from app.core.jobs import FatalError

    flow = local_journal
    def fail():
        if rejected:
            request = httpx.Request("POST", "https://provider.example.invalid/v1")
            httpx.Response(401, request=request).raise_for_status()
        raise TimeoutError("synthetic unknown provider outcome")
    for attempt in (1, 2):
        with pytest.raises(FatalError):
            flow.invoke("first", domain, callback=fail, attempt=attempt)
    assert flow.calls == ["first"]
    assert flow.meter.snapshot()["month_cost_usd_micro"] == (0 if rejected else 1200)
    with flow.meter._database() as connection:
        assert connection.execute("SELECT status FROM calls").fetchone()[0] == ("unspent" if rejected else "unknown")


@pytest.mark.parametrize("domain", ["industrial procurement", "home cooking"])
def test_local_journal_recovery_finalizes_original_task_usage(local_journal, monkeypatch, domain):
    flow = local_journal
    _, calls, finalized = task_hooks(monkeypatch)
    task_usage = flow.gate._task_usage
    def interrupted(method, *args, **kwargs):
        if method == "after_call":
            raise SystemExit("synthetic interrupted task settlement")
        return task_usage(method, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(flow.gate, "_task_usage", interrupted)
        with pytest.raises(SystemExit):
            flow.invoke("first", domain)
    assert not finalized
    for _ in range(2):
        assert flow.invoke("first", domain, attempt=2) == {"domain": domain}
    assert finalized == {"inv_receipt": {"actual_micro": 248, "tokens_used": 120}}
    assert len([call for call in calls if call[0] == "before_call"]) == 1
    assert flow.calls == ["first"]

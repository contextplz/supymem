"""Variable nonmodel costs checkpoint and settle once after confirmed I/O."""
import json
from types import SimpleNamespace

import pytest


@pytest.fixture()
def operation(account, monkeypatch):
    from app.credits import gate
    from app.core.jobs import FatalError
    from app.core.operation_receipts import OperationJournal
    from app.research import metered

    row = SimpleNamespace(workspace_id="ws_wallet:" + account.attribution_venture_id,
                          venture_id=account.attribution_venture_id)
    doc = {"_runtime": {"operations": {}}}
    hooks, calls = [], []
    def task_usage(method, *args, **kwargs):
        hooks.append((method, args, kwargs))
        return "inv_nonmodel" if method == "before_call" else None
    monkeypatch.setattr(gate, "_task_usage", task_usage)
    def mutate(did, payload, update):
        if payload is not None and not payload.get("allowed", True):
            raise FatalError("quantity_source_changed")
        return update(row, doc, None)
    journal = OperationJournal(read=lambda _: row, document=lambda _: doc, mutate=mutate,
                               before_call=lambda *a: None, prefix="quantity")
    def run(*, reader=None, byok=False, cap=False, result=None, lose_lease=False, model=None):
        payload = {"_job_attempt": 1, "_worker_id": "worker", "allowed": True}
        def respond():
            calls.append(True)
            if lose_lease:
                payload["allowed"] = False
            return {"person": None, "no_hit": True} if result is None else result
        return metered.perform("run", "enrich", payload, purpose="quantity.enrich", provider="synthetic",
            estimate=30000, model=model, byok=byok, callback=respond, journal=journal, prefix="quantity",
            actual_micro=reader, cap_at_reservation=cap)
    return SimpleNamespace(run=run, doc=doc, hooks=hooks, calls=calls, journal=journal)


def entry(operation):
    from app.credits.ledger import find_by_idempotency_key
    usage = operation.doc["_runtime"]["operations"]["enrich"]["usage"]
    key = "settle:" + usage["reservation_id"] if usage["reservation_id"] else "passthrough:run:enrich:1"
    return find_by_idempotency_key(key)


def test_fixed_default_stays_exact_estimate(operation):
    assert operation.run() == {"person": None, "no_hit": True}
    assert entry(operation).amount_micro == -30000
    assert json.loads(entry(operation).meta_json)["raw_provider_actual_micro"] == 30000


@pytest.mark.parametrize("domain", ["warehouse maintenance", "family meal planning"])
def test_local_nonmodel_journal_keeps_frozen_cost_and_replays_once(operation, monkeypatch, tmp_path, domain):
    from app.credits import gate, local_meter
    from app.credits.errors import CreditsUnavailable

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    result = {"domain": domain}
    assert operation.run(reader=lambda _: 7, byok=True, result=result) == result
    assert operation.run(reader=lambda _: 7, byok=True, result=result) == result
    saved = operation.doc["_runtime"]["operations"]["enrich"]
    assert saved["state"] == "completed" and saved["usage"]["credits_micro"] == 0
    assert operation.calls == [True] and entry(operation) is None
    assert local_meter.snapshot()["month_cost_usd_micro"] == 7
    assert gate.reconcile_receipt(saved["usage"]) == 0
    for changed in ({"provider_actual_micro": 0}, {"local_call_id": "another-call"},
                    {"request_id": "another-request"}, {"credits_micro": 1}):
        with pytest.raises(CreditsUnavailable):
            gate.reconcile_receipt(saved["usage"] | changed)


def test_confirmed_byok_nohit_preserves_zero_raw_cost_and_one_call_on_replay(operation):
    result = operation.run(reader=lambda r: 0 if r == {"person": None, "no_hit": True} else pytest.fail("not confirmed"), byok=True)
    first = entry(operation)
    usage = operation.doc["_runtime"]["operations"]["enrich"]["usage"]
    assert first.amount_micro == 0 and usage["source"] == "byok"
    assert usage["actual_micro"] == usage["provider_actual_micro"] == 0
    assert operation.run(reader=lambda _: pytest.fail("replay reran cost reader"), byok=True) == result
    assert entry(operation).id == first.id and operation.calls == [True]
    assert sum(method == "before_call" for method, _, _ in operation.hooks) == 1
    assert all(kwargs["actual_micro"] == 0 and kwargs["tokens_used"] == 0
               for method, _, kwargs in operation.hooks if method == "after_call")


def test_positive_variable_cost_below_estimate_is_actual(operation):
    operation.run(reader=lambda _: 7000)
    assert entry(operation).amount_micro == -7000
    assert json.loads(entry(operation).meta_json)["raw_provider_actual_micro"] == 7000


@pytest.mark.parametrize("value", [-1, True, "0", None])
def test_unconfirmed_reader_value_remains_protected_without_redial(operation, value):
    from app.core.jobs import FatalError
    with pytest.raises(FatalError, match="quantity_outcome_uncertain"):
        operation.run(reader=lambda _: value)
    saved = operation.doc["_runtime"]["operations"]["enrich"]
    assert saved["state"] == "uncertain" and not saved.get("response")
    assert entry(operation) is None
    with pytest.raises(FatalError, match="quantity_outcome_uncertain"):
        operation.run(reader=lambda _: 0)
    assert operation.calls == [True]
    assert not any(method == "after_call" for method, _, _ in operation.hooks)


def test_reader_exception_cannot_be_reclassified_as_definitive_unspent(operation):
    from app.core.jobs import FatalError, RetryableError
    def failed(_):
        raise RetryableError("not a provider rejection")
    with pytest.raises(FatalError, match="quantity_outcome_uncertain"):
        operation.run(reader=failed)
    assert operation.doc["_runtime"]["operations"]["enrich"]["state"] == "uncertain"
    assert not any(method == "after_call" for method, _, _ in operation.hooks)
    assert entry(operation) is None


def test_known_cost_survives_lease_loss_before_output_fence(operation):
    from app.core.jobs import FatalError
    with pytest.raises(FatalError, match="quantity_source_changed"):
        operation.run(reader=lambda _: 7000, lose_lease=True)
    assert entry(operation).amount_micro == -7000
    saved = operation.doc["_runtime"]["operations"]["enrich"]
    assert saved["state"] == "completed" and saved["usage"]["settled"] is True
    assert operation.calls == [True]


def test_raw_overage_is_saved_but_capped_output_is_refused(operation):
    from app.core.jobs import FatalError
    with pytest.raises(FatalError, match="quantity_provider_usage_above_ceiling"):
        operation.run(reader=lambda _: 40000, cap=True)
    ledger = entry(operation)
    assert ledger.amount_micro == -30000
    assert json.loads(ledger.meta_json)["raw_provider_actual_micro"] == 40000
    assert operation.calls == [True]


@pytest.mark.parametrize("reader,model", [(1, None), (lambda _: 0, "priced-model")])
def test_invalid_reader_usage_refuses_before_prepare_or_provider(operation, reader, model):
    from app.core.jobs import FatalError
    with pytest.raises(FatalError, match="quantity_cost_reader_invalid"):
        operation.run(reader=reader, model=model)
    assert not operation.doc["_runtime"]["operations"] and not operation.calls and not operation.hooks

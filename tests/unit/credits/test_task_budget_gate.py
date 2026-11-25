"""Synthetic S03 hook checks around the real wallet gate, with no paid I/O."""
import pytest


def hooks(monkeypatch):
    from app.credits import gate
    calls = []
    def run(method, *args, **kwargs):
        calls.append((method, args, kwargs))
        return "inv_synthetic" if method == "before_call" else None
    monkeypatch.setattr(gate, "_task_usage", run)
    return gate, calls


def test_task_budget_precedes_reserve_and_records_confirmed_usage(account, monkeypatch):
    gate, calls = hooks(monkeypatch)
    with gate.charge_sync(account_id=account.id, purpose="task.plan", estimate_micro=500, max_tokens=200) as meter:
        assert calls == [("before_call", (), {"estimate_micro": 500, "max_tokens": 200})]
        meter.record(prompt_tokens=10, completion_tokens=20)
        meter.record_micro(100)
    assert calls[-1] == ("after_call", ("inv_synthetic",), {"actual_micro": 100, "tokens_used": 30})


def test_wallet_rejection_releases_task_budget_before_provider(account, monkeypatch):
    from app.credits.errors import InsufficientCredits
    gate, calls = hooks(monkeypatch)
    with pytest.raises(InsufficientCredits):
        with gate.charge_sync(account_id=account.id, purpose="task.plan", estimate_micro=account.granted_micro + 1):
            pytest.fail("provider must not run")
    assert calls[-1] == ("abort_call", ("inv_synthetic",), {"definitive": True})


def test_unknown_provider_outcome_retains_task_budget(account, monkeypatch):
    gate, calls = hooks(monkeypatch)
    with pytest.raises(TimeoutError):
        with gate.charge_sync(account_id=account.id, purpose="task.plan", estimate_micro=500) as meter:
            meter.protect_outcome()
            raise TimeoutError("synthetic provider outcome unknown")
    assert calls[-1] == ("abort_call", ("inv_synthetic",), {"definitive": False})


def test_definitive_provider_rejection_releases_task_budget(account, monkeypatch):
    gate, calls = hooks(monkeypatch)
    with gate.charge_sync(account_id=account.id, purpose="task.plan", estimate_micro=500) as meter:
        meter.protect_outcome()
        meter.confirm_unspent()
    assert calls[-1] == ("abort_call", ("inv_synthetic",), {"definitive": True})


def test_task_counter_failure_preserves_receipt_but_refuses_output(account, monkeypatch):
    from app.credits import gate, metered
    from app.credits.accounts import get_account_by_id
    from app.credits.errors import CreditsUnavailable
    from app.credits.ledger import entries_for_account
    import json
    calls = []
    def usage(method, *args, **kwargs):
        calls.append(method)
        if method == "before_call":
            return "inv_crash_bridge"
        if method == "after_call":
            raise RuntimeError("synthetic task-counter storage failure")
    monkeypatch.setattr(gate, "_task_usage", usage)
    with pytest.raises(CreditsUnavailable, match="reconciliation"):
        metered.call_sync(lambda: "provider output must not be applied", account_id=account.id,
                          purpose="task.plan", estimate_micro=500, actual_micro=lambda _: 100)
    assert get_account_by_id(account.id).spent_micro == 100
    row = entries_for_account(account.id)["items"][0]
    assert json.loads(row.meta_json)["task_invocation"] == "inv_crash_bridge"
    assert calls == ["before_call", "after_call", "abort_call"]


@pytest.mark.parametrize("with_wallet", [False, True])
def test_byok_obeys_same_budget_and_preserves_raw_usage(account, monkeypatch, with_wallet):
    from app.credits.accounts import get_account_by_id
    gate, calls = hooks(monkeypatch)
    with gate.charge_sync(account_id=account.id if with_wallet else None, payer="founder",
                          purpose="task.byok", estimate_micro=500, max_tokens=200,
                          model="snapshot", request_id="byok-budget") as meter:
        assert meter.hold is None and meter.task_invocation == "inv_synthetic"
        assert calls == [("before_call", (), {"estimate_micro": 500, "max_tokens": 200})]
        meter.record(prompt_tokens=10, completion_tokens=20, cached_tokens=8)
        meter.record_micro(7, provider_actual_micro=10)
    assert calls[-1] == ("after_call", ("inv_synthetic",), {"actual_micro": 10, "tokens_used": 30})
    assert get_account_by_id(account.id).spent_micro == 0


def test_byok_unknown_outcome_retains_nonwallet_task_uncertainty(account, monkeypatch):
    gate, calls = hooks(monkeypatch)
    with pytest.raises(TimeoutError):
        with gate.charge_sync(payer="founder", purpose="task.byok", estimate_micro=500) as meter:
            meter.protect_outcome()
            assert meter.outcome_protected and meter.hold is None
            raise TimeoutError("synthetic uncertain BYOK call")
    assert calls[-1] == ("abort_call", ("inv_synthetic",), {"definitive": False})


def test_byok_budget_refusal_prevents_dispatch(account, monkeypatch):
    from app.credits import gate
    dispatched = []
    def refuse(method, *args, **kwargs):
        assert method == "before_call" and kwargs == {"estimate_micro": 500, "max_tokens": 200}
        raise RuntimeError("synthetic same fixed task limit")
    monkeypatch.setattr(gate, "_task_usage", refuse)
    with pytest.raises(RuntimeError, match="fixed task limit"):
        with gate.charge_sync(account_id=account.id, payer="founder", purpose="task.byok",
                              estimate_micro=500, max_tokens=200):
            dispatched.append(True)
    assert not dispatched


def test_unknown_model_counts_or_cost_cannot_advance_byok_task(account, monkeypatch):
    from app.credits.errors import CreditsUnavailable
    gate, calls = hooks(monkeypatch)
    with pytest.raises(CreditsUnavailable):
        with gate.charge_sync(account_id=account.id, payer="founder", purpose="task.byok",
                              estimate_micro=500, model="snapshot") as meter:
            meter.protect_outcome()
            meter.record_micro(10)
    assert not any(method == "after_call" for method, _, _ in calls)
    assert calls[-1] == ("abort_call", ("inv_synthetic",), {"definitive": False})
    calls.clear()
    monkeypatch.setattr(gate, "actual_llm_micro", lambda *args, **kwargs: (_ for _ in ()).throw(CreditsUnavailable("unpriced model")))
    with pytest.raises(CreditsUnavailable):
        with gate.charge_sync(account_id=account.id, payer="founder", purpose="task.byok",
                              estimate_micro=500, model="snapshot") as meter:
            meter.protect_outcome()
            meter.record(prompt_tokens=10, completion_tokens=20)
    assert not any(method == "after_call" for method, _, _ in calls)


def test_byok_ledger_write_failure_blocks_output_and_task_counter(account, monkeypatch):
    from app.credits import gate
    from app.credits.errors import CreditsUnavailable
    gate, calls = hooks(monkeypatch)
    monkeypatch.setattr(gate, "append", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("synthetic ledger outage")))
    with pytest.raises(CreditsUnavailable):
        with gate.charge_sync(account_id=account.id, payer="founder", purpose="task.byok", estimate_micro=500) as meter:
            meter.record_micro(10)
    assert not any(method == "after_call" for method, _, _ in calls)
    assert meter.task_usage_failed

"""Synthetic task hooks exercise journal ordering; gate tests prove real ledger custody."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def receipt(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    load_company_os_app()
    from app.credits import gate
    from app.core.operation_receipts import OperationJournal
    row = SimpleNamespace(workspace_id="wsp_receipt", venture_id="ven_receipt")
    saved = {"state": "responded", "attempt": 1, "response": {"content": "saved"},
        "usage": {"account_id": "acc_receipt", "reservation_id": "hold_receipt", "estimate_micro": 20,
            "provider": "test", "model": "test", "source": "platform", "actual_micro": 12,
            "prompt_tokens": 8, "completion_tokens": 4, "cached_tokens": 6, "task_invocation": "inv_receipt"}}
    doc = {"_runtime": {"operations": {"review": deepcopy(saved)}}}
    events = []
    def mutate(did, payload, update):
        if payload is not None:
            events.append("fence")
            if not payload["allowed"]:
                from app.core.jobs import FatalError
                raise FatalError("receipt_source_changed")
        return update(row, doc, None)
    def validate(row, doc, session, usage):
        events.append("binding")
        if usage.get("provider") != "test":
            from app.core.jobs import FatalError
            raise FatalError("receipt_quote_changed")
    journal = OperationJournal(read=lambda _: row, document=lambda _: doc, mutate=mutate,
        before_call=lambda *args: pytest.fail("recovery must not consume another call"),
        validate_usage=validate, prefix="receipt")
    def reconcile(usage, expected):
        events.append("reconcile")
        assert expected == {"workspace_id": row.workspace_id, "venture_id": row.venture_id,
            "request_id": "run:review:1", "provider": "test", "model": "test", "source": "platform"} | (
                {"task_invocation": usage["task_invocation"]} if "task_invocation" in usage else {})
        return 0 if usage.get("definitive_unspent") else 10
    monkeypatch.setattr(gate, "reconcile_receipt", reconcile, raising=False)
    return SimpleNamespace(journal=journal, doc=doc, saved=saved, events=events)


@pytest.mark.parametrize("stage", ["projection", "settlement"])
def test_recovery_failure_logs_bounded_redacted_diagnostics(receipt, monkeypatch, caplog, stage):
    from app.core.jobs import RetryableError
    from app.credits import gate

    saved = receipt.doc["_runtime"]["operations"]["review"]
    saved["usage"]["model_receipt_error"] = "counts_unknown"
    secret = "sk-proj-" + "a" * 40
    def fail(*args):
        raise RuntimeError("synthetic failure " + secret + " " + "x" * 1000)
    if stage == "projection":
        saved["state"] = "provider_responded"
        monkeypatch.setattr(gate, "project_model_receipt", fail)
    else:
        monkeypatch.setattr(gate, "reconcile_receipt", fail)
    before = deepcopy(saved)
    with pytest.raises(RetryableError, match="receipt_settlement_pending"):
        receipt.journal.recover("run", "review")
    records = [record for record in caplog.records if record.name == "app.core.operation_receipts"
        and record.getMessage() == "model_receipt_recovery_failed"]
    assert len(records) == 1
    record = records[0]
    assert record.levelname == "WARNING" and record.exception_type == "RuntimeError"
    assert len(record.exception_message) == 512 and "[REDACTED]" in record.exception_message
    assert secret not in record.exception_message and record.exc_info is None
    assert record.operation_id == "run" and record.operation_key == "review"
    assert record.reservation_id == "hold_receipt" and record.model_receipt_error == "counts_unknown"
    assert saved == before


def test_recovery_confirms_financial_custody_before_current_output_fence(receipt):
    from app.core.jobs import FatalError
    with pytest.raises(FatalError, match="receipt_source_changed"):
        receipt.journal.recover("run", "review", {"allowed": False})
    assert receipt.events == ["binding", "reconcile", "fence"]
    saved = receipt.doc["_runtime"]["operations"]["review"]
    assert saved["state"] == "completed" and saved["usage"]["credits_micro"] == 10
    assert receipt.journal.recover("run", "review", {"allowed": True}) == {"content": "saved"}
    assert receipt.events[-3:] == ["binding", "reconcile", "fence"]


def test_changed_quote_refuses_before_financial_reconciliation(receipt):
    from app.core.jobs import FatalError
    receipt.doc["_runtime"]["operations"]["review"]["usage"]["provider"] = "foreign"
    with pytest.raises(FatalError, match="receipt_quote_changed"):
        receipt.journal.recover("run", "review", {"allowed": True})
    assert receipt.events == ["binding"]
    assert receipt.doc["_runtime"]["operations"]["review"]["state"] == "responded"


def test_older_retryable_receipt_still_requires_confirmed_release(receipt, monkeypatch):
    from app.credits import gate
    from app.core.jobs import RetryableError
    saved = receipt.doc["_runtime"]["operations"]["review"]
    saved.update(state="retryable", error_code="receipt_provider_rate_limited")
    saved["usage"].pop("task_invocation")
    original = gate.reconcile_receipt
    monkeypatch.setattr(gate, "reconcile_receipt", lambda *args: (_ for _ in ()).throw(RuntimeError("release failed")))
    with pytest.raises(RetryableError, match="receipt_settlement_pending"):
        receipt.journal.recover("run", "review")
    saved = receipt.doc["_runtime"]["operations"]["review"]
    assert saved["state"] == "release_pending" and saved["usage"]["definitive_unspent"] is True
    monkeypatch.setattr(gate, "reconcile_receipt", original)
    assert receipt.journal.recover("run", "review") is None
    assert saved["state"] == "retryable" and saved["usage"]["settled"] is True


def test_legacy_funded_receipt_passes_original_counts_without_inventing_metadata(receipt):
    usage = receipt.doc["_runtime"]["operations"]["review"]["usage"]
    usage.pop("task_invocation")
    usage.pop("cached_tokens")
    original = deepcopy(usage)
    assert receipt.journal.recover("run", "review") == {"content": "saved"}
    settled = receipt.doc["_runtime"]["operations"]["review"]["usage"]
    assert settled == original | {"credits_micro": 10, "settled": True}


def test_preexisting_null_response_never_becomes_a_missing_receipt(receipt):
    from app.core.jobs import FatalError
    receipt.doc["_runtime"]["operations"]["review"]["response"] = None
    for _ in range(2):
        with pytest.raises(FatalError, match="receipt_invalid_output"):
            receipt.journal.recover("run", "review", {"allowed": True})
    assert receipt.doc["_runtime"]["operations"]["review"]["state"] == "completed"
    assert receipt.events == ["binding", "reconcile", "binding", "reconcile"]


@pytest.mark.parametrize("state", ["calling", "uncertain"])
def test_unknown_paid_call_cannot_be_reclaimed_by_another_attempt(receipt, state):
    from app.core.jobs import FatalError
    receipt.doc["_runtime"]["operations"]["review"]["state"] = state
    with pytest.raises(FatalError, match="receipt_outcome_uncertain"):
        receipt.journal.recover("run", "review", {"_job_attempt": 2, "_worker_id": "other"})
    assert receipt.events == []


@pytest.mark.parametrize("minutes,confirmed", [(9, False), (10, False), (1, True)])
def test_interrupted_receipt_has_bounded_cleanup_without_redial(receipt, minutes, confirmed):
    from datetime import datetime, timedelta, timezone
    saved = receipt.doc["_runtime"]["operations"]["review"]
    now = datetime(2026, 10, 13, tzinfo=timezone.utc)
    saved.update(state="calling", started_at=(now - timedelta(minutes=minutes)).isoformat())
    def resolve(old, expired, *args):
        if confirmed:
            return {"state": "responded"}
        if expired:
            return {"state": "failed", "billing_resolution": "platform_absorbed",
                "error_code": "receipt_interrupted", "usage": {**old["usage"], "settled": True, "credits_micro": 0}}
    changed = receipt.journal.reconcile_interrupted("run", "review", resolve, now=now)
    assert changed == (confirmed or minutes >= 10)
    assert saved["state"] == ("completed" if confirmed else "failed" if changed else "calling")
    assert receipt.events == (["binding", "reconcile"] if confirmed else [])
    if not confirmed and changed:
        from app.core.jobs import FatalError
        with pytest.raises(FatalError, match="receipt_interrupted"):
            receipt.journal.recover("run", "review")
        receipt.journal.save_operation("run", "review", {"state": "responded", "response": {"late": True}})
        assert saved["state"] == "failed" and saved["late_receipt"]["response"] == {"late": True}


def test_absorbed_receipt_still_rejects_foreign_late_usage(receipt):
    from app.core.jobs import RetryableError
    saved = receipt.doc["_runtime"]["operations"]["review"]
    saved.update(state="failed", billing_resolution="platform_absorbed")
    with pytest.raises(RetryableError, match="receipt_settlement_pending"):
        receipt.journal.checkpoint_model_receipt("run", "review", {**saved["usage"], "account_id": "foreign"})
    assert "late_receipt" not in saved


@pytest.mark.parametrize("audience", ["industrial procurement", "home cooking"])
@pytest.mark.parametrize("estimate,can_retry", [(0, True), (20, True), (None, False), (False, False)])
def test_local_discovery_read_recovery_preserves_unknown_estimates(receipt, monkeypatch, audience, estimate, can_retry):
    from app.core.jobs import FatalError

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    saved = receipt.doc["_runtime"]["operations"]["review"]
    saved.update(state="uncertain", response={"audience": audience}, usage={
        "source": "byok" if estimate else "free", "reservation_id": None,
        "purpose": "gtm.discovery.marketrole", "estimate_micro": estimate, "local_call_id": "local_receipt"})
    receipt.journal.retry_reads = True
    before = deepcopy(saved)
    payload = {"_job_attempt": 2, "_worker_id": "retry_worker", "allowed": True}
    if not can_retry:
        with pytest.raises(FatalError, match="receipt_outcome_uncertain"):
            receipt.journal.recover("run", "review", payload)
        assert receipt.doc["_runtime"]["operations"]["review"] == before and receipt.events == []
    else:
        assert receipt.journal.recover("run", "review", payload) is None
        current = receipt.doc["_runtime"]["operations"]["review"]
        assert current["state"] == "retryable" and current["usage"] == {}
        assert current["interrupted_reads"] == [before]
        assert receipt.events == ["fence"]


@pytest.mark.parametrize("audience", ["industrial procurement", "home cooking"])
@pytest.mark.parametrize("model", [None, "saved-model"])
@pytest.mark.parametrize("state,same_delivery", [("uncertain", True), ("calling", False), ("calling", True)])
def test_local_priced_read_retry_preserves_previous_spend_custody(receipt, monkeypatch, audience, model, state, same_delivery):
    from app.core.jobs import FatalError

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    saved = receipt.doc["_runtime"]["operations"]["review"]
    saved.update(state=state, worker_id="retry_worker", response={"audience": audience}, usage={
        "source": "byok", "reservation_id": None, "model": model,
        "purpose": "gtm.discovery.marketrole", "estimate_micro": 20, "local_call_id": "previous-read"})
    receipt.journal.retry_reads = True
    before = deepcopy(saved)
    payload = {"_job_attempt": saved["attempt"] if same_delivery else saved["attempt"] + 1,
        "_worker_id": "retry_worker", "allowed": True}
    if state == "calling" and same_delivery:
        with pytest.raises(FatalError, match="receipt_operation_in_progress"):
            receipt.journal.recover("run", "review", payload)
        assert receipt.doc["_runtime"]["operations"]["review"] == before
        assert receipt.events == []
    else:
        assert receipt.journal.recover("run", "review", payload) is None
        current = receipt.doc["_runtime"]["operations"]["review"]
        assert current["state"] == "retryable" and current["usage"] == {}
        assert current["interrupted_reads"] == [before]
        assert receipt.events == ["fence"]

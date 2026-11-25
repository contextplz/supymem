"""Private checkpoints and actual Task counters with explicitly synthetic finance proofs."""
from copy import deepcopy
import json

import pytest
from sqlalchemy import select

from tests.task_billing_fixtures import checkpoint, install_recovery, synthetic_receipt
from tests.unit.tasks.test_task_lifecycle import lifecycle as lifecycle, _admit, _rows


@pytest.fixture()
def billing_db(lifecycle, monkeypatch):
    from app.tasks import billing
    d = lifecycle
    jid = _admit(d)
    task, run, steps = _rows(d)
    with d["usage"].job_context(jid):
        invocation = d["usage"].before_call(20, 100)
    d.update(billing=billing, recovery=install_recovery(monkeypatch), invocation=invocation,
        options={"workspace_id": task.workspace_id, "venture_id": task.venture_id, "purpose": "task.draft", "request_id": "synthetic-billing-request"},
        binding={"workspace_id": task.workspace_id, "venture_id": task.venture_id, "purpose": "task.draft", "request_id": "synthetic-billing-request",
            "run_id": run.id, "step_id": steps[0].id}, task_id=task.id, run_id=run.id, step_id=steps[0].id)
    d["receipt"] = synthetic_receipt(d["options"], invocation=invocation, estimate_micro=20, actual_micro=7, prompt=4, completion=6, cached=2)
    return d


def _capture(d, receipt=None):
    return d["billing"].capture(d["receipt"] if receipt is None else receipt, **d["binding"])


@pytest.fixture
def modern_billing(billing_db):
    from app.credits import reserve
    if not all(callable(getattr(reserve, name, None)) for name in ("model_receipt_metadata", "validate_call_binding")):
        pytest.skip("Pending accepted S02 receipt validator ABI; this is synthetic Task-only proof, not wallet/provider qualification.")
    d = billing_db
    # The fixture's legacy reservation was never transported. A new real Task
    # admission captures the synthetic dial instead of editing append-only data.
    d["usage"].abort_call(d["invocation"], definitive=True)
    route = {"schema_version": 1, "purpose": d["options"]["purpose"], "provider": "synthetic_provider",
        "model": "synthetic_deployment", "catalog_model": "synthetic_catalog", "request_id": d["options"]["request_id"],
        "captured_tier": "pro", "api_mode": "chat_completions", "max_output_tokens": 80, "reasoning_effort": "medium"}
    with d["usage"].run_context(d["run_id"], step_id=d["binding"]["step_id"]):
        invocation = d["usage"].before_call(20, 100, call_binding=route)
    d["invocation"] = invocation
    d["receipt"] = {**synthetic_receipt(d["options"], invocation=invocation, estimate_micro=20, actual_micro=7,
        prompt=4, completion=6, cached=2), "model": route["model"], "catalog_model": route["catalog_model"],
        "actual_catalog_model": route["catalog_model"], "returned_model": "synthetic_returned_model", "output_error": None,
        "call_binding": deepcopy(route)}
    return d


def test_legacy_checkpoint_shape_and_saved_hash_remain_exact(billing_db):
    d = billing_db
    receipt = {"source": "platform", "reservation_id": "synthetic_hold:legacy", "account_id": "synthetic_wallet",
        "estimate_micro": 20, "workspace_id": "wsp_legacy", "venture_id": "ven_legacy", "purpose": "task.plan",
        "model": "synthetic_model", "provider": "synthetic_provider", "request_id": "legacy-plan:1", "task_invocation": None,
        "actual_micro": 7, "provider_actual_micro": 7, "prompt_tokens": 4, "completion_tokens": 6, "cached_tokens": 2, "total_tokens": 10}
    saved = d["billing"].capture(receipt, workspace_id="wsp_legacy", venture_id="ven_legacy", purpose="task.plan", request_id="legacy-plan:1")
    assert saved["usage"] == receipt and len(saved["usage"]) == 17
    assert len(saved["binding"]) == 15 and saved["format"] == "task_billing_v1"
    assert saved["sha256"] == "b90c66485bac491467a8f72a812cc50cd61b9cc37ebc2372a2bf725b5431fecd"
    assert d["recovery"].calls == []


def test_modern_receipt_refuses_missing_owned_validator_dependency(billing_db, monkeypatch):
    from app.credits import reserve
    from app.credits.errors import CreditsUnavailable
    d = billing_db
    monkeypatch.delattr(reserve, "model_receipt_metadata", raising=False)
    monkeypatch.delattr(reserve, "validate_call_binding", raising=False)
    receipt = {**d["receipt"], "catalog_model": "synthetic_catalog", "actual_catalog_model": "synthetic_catalog",
        "returned_model": None, "output_error": None}
    with pytest.raises(CreditsUnavailable):
        _capture(d, receipt)
    assert d["recovery"].calls == []


@pytest.mark.parametrize("metadata", [{"catalog_model": "synthetic_catalog"},
    {"actual_catalog_model": "synthetic_catalog", "returned_model": None, "output_error": None},
    {"call_binding": None}, {"call_binding": {"provider": "invented"}}])
def test_partial_or_malformed_modern_fields_are_never_silently_dropped(billing_db, metadata):
    from app.credits.errors import CreditsUnavailable
    with pytest.raises(CreditsUnavailable):
        _capture(billing_db, {**billing_db["receipt"], **metadata})
    assert billing_db["recovery"].calls == []


def test_modern_checkpoint_keeps_original_route_and_pricing_provenance_for_replay(modern_billing):
    from app.db.task_models import TaskRun
    d = modern_billing
    saved = _capture(d)
    assert saved["usage"]["catalog_model"] == saved["binding"]["catalog_model"] == "synthetic_catalog"
    assert saved["usage"]["call_binding"] == saved["binding"]["call_binding"] == d["receipt"]["call_binding"]
    d["receipt"]["call_binding"]["reasoning_effort"] = "high"
    assert saved["usage"]["call_binding"]["reasoning_effort"] == "medium"
    d["recovery"].confirm(saved["usage"])
    assert d["billing"].reconcile(saved, **d["binding"]) == 7
    assert d["billing"].reconcile(saved, **d["binding"]) == 7
    forwarded = d["recovery"].calls[0][1]
    assert forwarded["catalog_model"] == "synthetic_catalog" and forwarded["call_binding"]["reasoning_effort"] == "medium"
    with d["factory"]() as session:
        run = session.get(TaskRun, d["run_id"])
        assert (run.cost_micro, run.tokens_used, run.reserved_cost_micro, run.reserved_tokens) == (7, 10, 0, 0)


@pytest.mark.parametrize("dropped", ["call_binding", "catalog_metadata", "both"])
def test_stripped_modern_receipt_cannot_claim_legacy_original_admission(modern_billing, dropped):
    from app.credits.errors import CreditsUnavailable
    d = modern_billing
    receipt = deepcopy(d["receipt"])
    if dropped in {"call_binding", "both"}:
        receipt.pop("call_binding")
    if dropped in {"catalog_metadata", "both"}:
        for field in ("catalog_model", "actual_catalog_model", "returned_model", "output_error"):
            receipt.pop(field)
    with pytest.raises(CreditsUnavailable):
        _capture(d, receipt)
    assert d["recovery"].calls == []


@pytest.mark.parametrize("field,value", [("captured_tier", "flash"), ("reasoning_effort", "high"),
    ("model", "foreign_deployment"), ("catalog_model", "foreign_catalog"), ("provider", "foreign_provider"), ("max_output_tokens", 81)])
def test_modern_route_cannot_replace_original_reserved_call_even_with_matching_receipt_top_fields(modern_billing, field, value):
    from app.credits.errors import CreditsUnavailable
    d = modern_billing
    receipt = deepcopy(d["receipt"])
    receipt["call_binding"][field] = value
    if field in {"model", "catalog_model", "provider"}:
        receipt[field] = value
    with pytest.raises(CreditsUnavailable):
        _capture(d, receipt)
    assert d["recovery"].calls == []


def test_legacy_admission_cannot_acquire_a_later_modern_route(modern_billing):
    from app.credits.errors import CreditsUnavailable
    from app.db.task_models import TaskEvent
    d = modern_billing
    with d["factory"]() as session:
        legacy = next(event for event in session.scalars(select(TaskEvent).where(TaskEvent.kind == "call_reserved"))
            if "call_binding" not in json.loads(event.data_json))
        invocation = json.loads(legacy.data_json)["invocation"]
    with pytest.raises(CreditsUnavailable):
        _capture(d, {**d["receipt"], "task_invocation": invocation})
    assert d["recovery"].calls == []


@pytest.mark.parametrize("error", ["model_mismatch", "incomplete", "refused", "filtered"])
def test_known_paid_output_error_reconciles_first_but_never_qualifies_for_apply(modern_billing, error):
    from app.credits.errors import CreditsUnavailable
    from app.db.task_models import TaskRun
    d = modern_billing
    receipt = {**d["receipt"], "output_error": error}
    saved = _capture(d, receipt)
    d["recovery"].confirm(receipt)
    with pytest.raises(CreditsUnavailable):
        d["billing"].validate_saved(saved, **d["binding"])
    assert d["recovery"].calls == []
    with pytest.raises(CreditsUnavailable, match="paid for but cannot be applied"):
        d["billing"].reconcile(saved, **d["binding"])
    assert len(d["recovery"].calls) == 1
    with d["factory"]() as session:
        run = session.get(TaskRun, d["run_id"])
        assert (run.cost_micro, run.tokens_used, run.reserved_cost_micro, run.reserved_tokens) == (7, 10, 0, 0)


@pytest.mark.parametrize("error", ["invented-provider-message", {}, []])
def test_unknown_output_error_cannot_be_captured_as_trusted_pricing(modern_billing, error):
    from app.credits.errors import CreditsUnavailable
    d = modern_billing
    with pytest.raises(CreditsUnavailable):
        _capture(d, {**d["receipt"], "output_error": error})
    assert d["recovery"].calls == []


def test_checkpoint_precedes_confirmed_fake_finance_and_recovery_settles_once(billing_db):
    from app.db.task_models import TaskRun, TaskEvent
    from app.credits.errors import CreditsUnavailable
    d = billing_db
    cached = []
    options = {**d["options"], "before_meter_exit": lambda response, receipt: cached.append((response, _capture(d, receipt)))}
    checkpoint("synthetic normalized response", options, d["receipt"])
    assert len(cached) == 1
    with pytest.raises(CreditsUnavailable):
        d["billing"].reconcile(cached[0][1], **d["binding"])
    d["recovery"].confirm(d["receipt"])
    assert d["billing"].reconcile(cached[0][1], **d["binding"]) == 7
    assert d["billing"].reconcile(cached[0][1], **d["binding"]) == 7
    with d["factory"]() as session:
        run = session.get(TaskRun, d["run_id"])
        assert (run.cost_micro, run.tokens_used, run.reserved_cost_micro, run.reserved_tokens) == (7, 10, 0, 0)
        assert len(session.scalars(select(TaskEvent).where(TaskEvent.kind == "call_settled")).all()) == 1


def test_capture_deep_snapshot_has_no_response_or_unrecognized_provider_fields(billing_db):
    d = billing_db
    receipt = {**d["receipt"], "raw_provider_body": "Private material must not persist here"}
    saved = _capture(d, receipt)
    receipt["actual_micro"] = 900
    assert saved["usage"]["actual_micro"] == 7 and "Private material" not in json.dumps(saved)


@pytest.mark.parametrize("field", ["workspace_id", "venture_id", "purpose", "request_id", "task_invocation", "estimate_micro"])
def test_wrong_original_scope_invocation_or_estimate_is_not_captured(billing_db, field):
    from app.credits.errors import CreditsUnavailable
    d = billing_db
    receipt = {**d["receipt"], field: 21 if field == "estimate_micro" else "foreign"}
    with pytest.raises(CreditsUnavailable):
        _capture(d, receipt)


@pytest.mark.parametrize("field", ["actual_micro", "provider_actual_micro", "prompt_tokens", "completion_tokens", "cached_tokens", "total_tokens"])
def test_invalid_integer_receipt_is_refused_before_apply(billing_db, field):
    from app.credits.errors import CreditsUnavailable
    with pytest.raises(CreditsUnavailable):
        _capture(billing_db, {**billing_db["receipt"], field: True})


@pytest.mark.parametrize("field", ["usage", "binding", "sha256", "format"])
def test_private_checkpoint_tamper_never_reconciles(billing_db, field):
    from app.credits.errors import CreditsUnavailable
    d = billing_db
    saved = _capture(d)
    d["recovery"].confirm(d["receipt"])
    changed = deepcopy(saved)
    if field == "usage":
        changed[field]["account_id"] = "foreign"
    elif field == "binding":
        changed[field]["step_id"] = "foreign"
    else:
        changed[field] = "foreign"
    with pytest.raises(CreditsUnavailable):
        d["billing"].reconcile(changed, **d["binding"])
    assert d["recovery"].calls == []


def test_duplicate_original_invocation_is_not_source_proof(billing_db):
    from app.db.task_models import TaskEvent
    from app.credits.errors import CreditsUnavailable
    d = billing_db
    with d["factory"]() as session:
        session.add(TaskEvent(id="evt_duplicate_billing", task_id=d["task_id"], run_id=d["run_id"], step_id=d["step_id"],
            kind="call_reserved", message="Synthetic duplicate", created_at=d["clock"][0],
            data_json=json.dumps({"invocation": d["invocation"], "cost_micro": 20, "tokens": 100})))
        session.commit()
    with pytest.raises(CreditsUnavailable):
        _capture(d)


@pytest.mark.parametrize("state", ["paused", "canceled", "custody_changed"])
def test_funded_cleanup_does_not_require_current_membership_or_resurrect_task(billing_db, state):
    from app.db.models import Workspace
    from app.db.task_models import Task, TaskRun
    from app.tasks.locks import lock_run
    d = billing_db
    saved = _capture(d)
    d["recovery"].confirm(d["receipt"])
    with d["factory"]() as session:
        task, run = lock_run(session, d["run_id"])
        if state == "custody_changed":
            session.get(Workspace, task.workspace_id).owner_id = "another_owner"
        else:
            task.state = run.state = state
        session.commit()
    assert d["billing"].reconcile(saved, **d["binding"]) == 7
    with d["factory"]() as session:
        assert session.get(TaskRun, d["run_id"]).cost_micro == 7
        assert session.get(Task, d["task_id"]).state == ("working" if state == "custody_changed" else state)


@pytest.mark.parametrize("source", ["byok", "free"])
def test_no_wallet_source_settles_raw_task_cost_without_fabricating_a_hold(billing_db, source):
    from app.db.task_models import TaskRun
    d = billing_db
    receipt = synthetic_receipt(d["options"], invocation=d["invocation"], source=source,
        estimate_micro=20, actual_micro=7 if source == "byok" else 0, prompt=4, completion=6, cached=2)
    saved = _capture(d, receipt)
    assert saved["usage"]["reservation_id"] is None
    d["recovery"].confirm(receipt)
    assert d["billing"].reconcile(saved, **d["binding"]) == 0
    with d["factory"]() as session:
        assert session.get(TaskRun, d["run_id"]).cost_micro == (7 if source == "byok" else 0)


def test_planning_without_run_requires_explicit_no_invocation(billing_db):
    from app.credits.errors import CreditsUnavailable
    d = billing_db
    receipt = synthetic_receipt(d["options"], invocation=None)
    captured = d["billing"].capture(receipt, **d["options"])
    d["recovery"].confirm(receipt)
    assert d["billing"].reconcile(captured, **d["options"]) == 0
    with pytest.raises(CreditsUnavailable):
        d["billing"].capture(d["receipt"], **d["options"])


@pytest.mark.parametrize("marker", [True, {}, [], "false", 0])
def test_successful_response_cannot_adopt_confirmed_rejection_cleanup_receipt(billing_db, marker):
    from app.credits.errors import CreditsUnavailable
    d = billing_db
    with pytest.raises(CreditsUnavailable):
        _capture(d, {**d["receipt"], "definitive_unspent": marker})


@pytest.mark.parametrize("source", [{}, [], None, True, "unknown"])
def test_malformed_source_raises_billing_error_without_provider_values(billing_db, source):
    from app.credits.errors import CreditsUnavailable
    d = billing_db
    with pytest.raises(CreditsUnavailable, match="saved billing receipt"):
        _capture(d, {**d["receipt"], "source": source})


def test_callback_capture_reuses_existing_session_without_flush_or_rollback(billing_db):
    from app.db.task_models import TaskStep
    from app.tasks.locks import lock_step
    d = billing_db
    with d["factory"]() as session:
        _, _, step = lock_step(session, d["step_id"])
        step.evidence_json = json.dumps({"state": "calling", "private": "Synthetic changed checkpoint"})
        captured = d["billing"].capture(d["receipt"], **d["binding"], session=session)
        assert session.is_modified(step)  # Read-only capture does not flush the funded journal.
        assert captured["binding"]["step_id"] == step.id
        session.commit()
    with d["factory"]() as session:
        assert "Synthetic changed" in session.get(TaskStep, d["step_id"]).evidence_json


def test_validate_saved_is_readonly_even_without_wallet_confirmation(billing_db):
    from app.db.task_models import TaskEvent, TaskRun
    from sqlalchemy import func
    d = billing_db
    captured = _capture(d)
    with d["factory"]() as session:
        before = session.scalar(select(func.count()).select_from(TaskEvent))
        run = session.get(TaskRun, d["run_id"])
        reserved = run.reserved_cost_micro, run.reserved_tokens
        assert d["billing"].validate_saved(captured, **d["binding"], session=session) == captured
        assert session.scalar(select(func.count()).select_from(TaskEvent)) == before
        assert (run.reserved_cost_micro, run.reserved_tokens) == reserved
        assert not session.dirty and d["recovery"].calls == [] and d["recovery"].proofs == {}

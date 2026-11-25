"""Actual SQLite journal/ledger ordering; no HTTP or provider calls."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select


@pytest.fixture()
def proxy(account, monkeypatch):
    from app.apps import usage
    from app.credits._txn import naive_utc
    from app.credits.provider_receipts import capture_pricing_snapshot
    from app.db.supymem_models import FounderApp
    from app.llm import pricing
    from app.state import db
    vid = account.attribution_venture_id
    wid = "ws_wallet:" + vid
    now = naive_utc()
    app_id = "app_proxy:" + vid
    with db()() as s:
        s.add(FounderApp(id=app_id, workspace_id=wid, venture_id=vid, slug=app_id,
            name="Synthetic proxy", kind="web", state="live", created_at=now, updated_at=now))
        s.commit()
    monkeypatch.setattr(pricing, "load_pricing", lambda: {"models": {
        "captured-model": {"input_per_mtok": "2", "cached_input_per_mtok": "0.1", "output_per_mtok": "10"},
        "other-model": {"input_per_mtok": "4", "output_per_mtok": "15"}}, "aliases": {}})
    bound = {"schema_version": 1, "purpose": "app.proxy", "provider": "azure_openai",
        "deployment": "deployment", "catalog_model": "captured-model", "api_mode": "responses",
        "endpoint_host": "synthetic.openai.azure.com", "input_token_limit": 400,
        "max_output_tokens": 20, "requested_service_tier": "default",
        "pricing_snapshot": capture_pricing_snapshot("captured-model")}
    capture = {"model_binding": bound, "token_ceiling": 420, "estimate_micro": 1000,
        "clock_policy": "server_clock_256_bytes", "request_digest": "a" * 64}
    hold = usage.hold_for_proxy(workspace_id=wid, venture_id=vid, amount_micro=1000,
        request_id="req_proxy", model="deployment", provider="azure_openai")
    receipt = {"schema_version": 1, "authority": "observation_only", "requires_proposal_apply": False,
        "request_digest": "a" * 64, "provider": "azure_openai", "deployment": "deployment",
        "catalog_model": "captured-model", "model": "captured-model", "api_mode": "responses",
        "input_tokens": 100, "output_tokens": 20, "total_tokens": 120, "cached_tokens": 80,
        "reasoning_tokens": 7, "service_tier": "default",
        "role_metadata": {key: bound["deployment"] if key == "model" else bound[key]
            for key in ("purpose", "provider", "model", "catalog_model", "api_mode", "max_output_tokens", "endpoint_host")}}
    return SimpleNamespace(usage=usage, hold=hold, capture=capture, receipt=receipt,
                           account=account, app_id=app_id, wid=wid, vid=vid)


def begin(p):
    p.usage.begin_proxy_call(p.hold, app_id=p.app_id, request_id="req_proxy",
        capture=p.capture, request_digest="a" * 64)


def state(p):
    from app.db.models import CreditAccount, CreditLedgerEntry, CreditReservation
    from app.db.supymem_models import AppUsageRecord
    from app.state import db
    with db()() as s:
        account = s.get(CreditAccount, p.hold.account_id)
        hold = s.get(CreditReservation, p.hold.reservation_id)
        ledger = list(s.scalars(select(CreditLedgerEntry).where(CreditLedgerEntry.reservation_id == hold.id)))
        records = list(s.scalars(select(AppUsageRecord).where(AppUsageRecord.source == "req_proxy")))
        return dict(spent=account.spent_micro, reserved=account.reserved_micro, status=hold.status,
            ledger=[(e.id, e.amount_micro, e.prompt_tokens, e.completion_tokens, e.meta_json) for e in ledger],
            records=[(r.ledger_entry_id, r.credits_micro, r.quantity) for r in records])


@pytest.mark.parametrize("prompt,output,fee", [(0, 0, 0), (1, 0, 2), (0, 1, 10)])
def test_true_zero_and_tiny_fees_have_no_actual_minimum_and_one_original_debit(proxy, prompt, output, fee):
    p = proxy
    begin(p)
    receipt = deepcopy(p.receipt)
    receipt.update(input_tokens=prompt, output_tokens=output, total_tokens=prompt + output,
                   cached_tokens=0, reasoning_tokens=0)
    p.usage.record_proxy_outcome(p.hold, request_id="req_proxy", confirmed_receipt=receipt)
    first = p.usage.settle_proxy(p.hold, request_id="req_proxy")
    old = state(p)
    assert first.charged_micro == fee and first.output_usable
    assert old["spent"] == fee and old["reserved"] == 0 and old["status"] == "settled"
    assert len(old["ledger"]) == len(old["records"]) == 1
    assert old["ledger"][0][1] == -fee and old["records"][0][:2] == (first.ledger_entry_id, fee)
    assert p.usage.recover_proxy(p.hold, request_id="req_proxy") == first
    assert state(p) == old


def test_known_checkpoint_precedes_financial_failure_and_recovers_without_reprice(proxy, monkeypatch):
    from app.credits import gate
    from app.credits.errors import CreditsUnavailable
    p = proxy
    begin(p)
    p.usage.record_proxy_outcome(p.hold, request_id="req_proxy", confirmed_receipt=p.receipt)
    assert p.usage.inspect_proxy_call(p.hold, request_id="req_proxy") == "responded"
    original = gate.reconcile_receipt
    monkeypatch.setattr(gate, "reconcile_receipt", lambda *a, **k: (_ for _ in ()).throw(CreditsUnavailable("write failed")))
    with pytest.raises(CreditsUnavailable):
        p.usage.settle_proxy(p.hold, request_id="req_proxy")
    assert state(p)["status"] == "reconciling" and not state(p)["ledger"]
    from app.credits import estimate
    from app.llm import pricing
    # Core revalidates the original frozen receipt to prevent a forged fee;
    # it must never consult today's price table or legacy cost calculator.
    monkeypatch.setattr(pricing, "load_pricing", lambda *a, **k: pytest.fail("live price table read"))
    monkeypatch.setattr(estimate, "actual_llm_micro", lambda *a, **k: pytest.fail("live model repricing"))
    monkeypatch.setattr(gate, "reconcile_receipt", original)
    settled = p.usage.recover_proxy(p.hold, request_id="req_proxy")
    assert settled.charged_micro == 248 and len(state(p)["ledger"]) == 1


def test_unpriced_returned_model_retains_safe_response_before_price_failure(proxy):
    from app.credits.errors import CreditsUnavailable
    p = proxy
    begin(p)
    receipt = deepcopy(p.receipt)
    receipt["model"] = "unpriced-returned-model"
    receipt["result"] = {"text": "must never enter billing journal"}
    with pytest.raises(CreditsUnavailable):
        p.usage.record_proxy_outcome(p.hold, request_id="req_proxy", confirmed_receipt=receipt)
    assert p.usage.inspect_proxy_call(p.hold, request_id="req_proxy") == "response"
    from app.state import db
    with db()() as s:
        saved = p.usage._read_event(s, p.hold, "response")
        assert saved["response"]["model"] == "unpriced-returned-model"
        assert saved["response"]["total_tokens"] == 120 and "result" not in saved["response"]
    with pytest.raises(CreditsUnavailable):
        p.usage.recover_proxy(p.hold, request_id="req_proxy")
    assert state(p)["status"] == "reconciling" and not state(p)["ledger"]


def test_saved_safe_response_retries_price_only_then_recovers_same_hold(proxy, monkeypatch):
    from app.credits import provider_receipts
    from app.credits.errors import CreditsUnavailable
    p = proxy
    begin(p)
    original = provider_receipts.validate_confirmed_provider_receipt
    monkeypatch.setattr(provider_receipts, "validate_confirmed_provider_receipt",
        lambda *a, **k: (_ for _ in ()).throw(CreditsUnavailable("price projection failed")))
    with pytest.raises(CreditsUnavailable):
        p.usage.record_proxy_outcome(p.hold, request_id="req_proxy", confirmed_receipt=p.receipt)
    assert p.usage.inspect_proxy_call(p.hold, request_id="req_proxy") == "response"
    monkeypatch.setattr(provider_receipts, "validate_confirmed_provider_receipt", original)
    first = p.usage.recover_proxy(p.hold, request_id="req_proxy")
    assert first.charged_micro == 248 and state(p)["reserved"] == 0
    assert len(state(p)["ledger"]) == 1


def test_new_begin_refuses_retired_app_but_funded_cleanup_survives_retirement(proxy):
    from app.credits._txn import naive_utc
    from app.credits.errors import CreditsUnavailable
    from app.db.supymem_models import FounderApp
    from app.state import db
    p = proxy
    with db()() as s:
        app = s.get(FounderApp, p.app_id)
        app.state = "retired"
        s.commit()
    with pytest.raises(CreditsUnavailable):
        begin(p)
    with db()() as s:
        app = s.get(FounderApp, p.app_id)
        app.state = "live"
        s.commit()
    begin(p)
    p.usage.record_proxy_outcome(p.hold, request_id="req_proxy", confirmed_receipt=p.receipt)
    with db()() as s:
        app = s.get(FounderApp, p.app_id)
        app.state = "retired"
        app.deleted_at = naive_utc()
        s.commit()
    assert p.usage.recover_proxy(p.hold, request_id="req_proxy").charged_micro == 248


def test_app_projection_failure_after_debit_recovers_without_second_charge(proxy, monkeypatch):
    p = proxy
    begin(p)
    p.usage.record_proxy_outcome(p.hold, request_id="req_proxy", confirmed_receipt=p.receipt)
    model = p.usage.AppUsageRecord
    original = model.__init__
    monkeypatch.setattr(model, "__init__", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("projection failed")))
    with pytest.raises(RuntimeError):
        p.usage.settle_proxy(p.hold, request_id="req_proxy")
    paid = state(p)
    assert paid["spent"] == 248 and paid["status"] == "settled" and not paid["records"]
    monkeypatch.setattr(model, "__init__", original)
    p.usage.recover_proxy(p.hold, request_id="req_proxy")
    assert state(p)["spent"] == 248 and len(state(p)["ledger"]) == len(state(p)["records"]) == 1


def test_unknown_never_releases_or_grants_another_dispatch(proxy, monkeypatch):
    from app.credits import reserve
    from app.credits.errors import CreditsUnavailable
    p = proxy
    monkeypatch.setattr(reserve, "release", lambda *a, **k: pytest.fail("unknown outcome released"))
    begin(p)
    p.usage.record_proxy_outcome(p.hold, request_id="req_proxy", error_code="transport_unknown")
    assert p.usage.inspect_proxy_call(p.hold, request_id="req_proxy") == "uncertain"
    with pytest.raises(CreditsUnavailable):
        begin(p)
    with pytest.raises(CreditsUnavailable):
        p.usage.recover_proxy(p.hold, request_id="req_proxy")
    assert state(p)["status"] == "reconciling" and state(p)["reserved"] == 1000
    assert not state(p)["ledger"] and not state(p)["records"]


def test_begin_protect_failure_has_safe_pending_inspection_and_no_duplicate_dispatch(proxy, monkeypatch):
    from app.credits.errors import CreditsUnavailable
    p = proxy
    monkeypatch.setattr(p.usage, "protect_outcome", lambda *a: (_ for _ in ()).throw(CreditsUnavailable("unknown protect ack")))
    with pytest.raises(CreditsUnavailable):
        begin(p)
    assert p.usage.inspect_proxy_call(p.hold, request_id="req_proxy") == "begin"
    with pytest.raises(CreditsUnavailable):
        begin(p)
    assert state(p)["spent"] == 0 and not state(p)["ledger"]


def test_retirement_during_protection_never_grants_dispatch(proxy, monkeypatch):
    from app.credits.errors import CreditsUnavailable
    from app.db.supymem_models import FounderApp
    from app.state import db
    p = proxy
    original = p.usage.protect_outcome
    def protect_then_retire(hold):
        original(hold)
        with db()() as s:
            s.get(FounderApp, p.app_id).state = "retired"
            s.commit()
    monkeypatch.setattr(p.usage, "protect_outcome", protect_then_retire)
    with pytest.raises(CreditsUnavailable):
        begin(p)
    assert p.usage.inspect_proxy_call(p.hold, request_id="req_proxy") == "begin"
    assert state(p)["status"] == "reconciling" and not state(p)["ledger"]


@pytest.mark.parametrize("error", ["model_mismatch", "refused"])
def test_known_unusable_response_is_charged_and_remains_terminal(proxy, error):
    p = proxy
    begin(p)
    receipt = deepcopy(p.receipt)
    if error == "model_mismatch":
        receipt["model"] = "other-model"
    else:
        receipt["error_code"] = error
    p.usage.record_proxy_outcome(p.hold, request_id="req_proxy", confirmed_receipt=receipt)
    paid = p.usage.settle_proxy(p.hold, request_id="req_proxy")
    assert not paid.output_usable and paid.output_error == error and paid.charged_micro > 0
    assert p.usage.recover_proxy(p.hold, request_id="req_proxy") == paid
    assert len(state(p)["ledger"]) == len(state(p)["records"]) == 1


def test_known_paid_invalid_output_checkpoints_before_price_and_recovers_once(proxy, monkeypatch):
    from app.credits import provider_receipts
    from app.credits.errors import CreditsUnavailable
    from app.state import db
    p = proxy
    begin(p)
    receipt = deepcopy(p.receipt)
    receipt.update(error_code="invalid_output", result={"private_body": "must-not-persist"})
    original = provider_receipts.validate_confirmed_provider_receipt
    monkeypatch.setattr(provider_receipts, "validate_confirmed_provider_receipt",
        lambda *a, **k: (_ for _ in ()).throw(CreditsUnavailable("price projection failed")))
    with pytest.raises(CreditsUnavailable):
        p.usage.record_proxy_outcome(p.hold, request_id="req_proxy", confirmed_receipt=receipt)
    assert p.usage.inspect_proxy_call(p.hold, request_id="req_proxy") == "response"
    with db()() as session:
        saved = p.usage._read_event(session, p.hold, "response")["response"]
        assert saved["error_code"] == "invalid_output" and saved["total_tokens"] == 120
        assert "result" not in saved and "must-not-persist" not in json.dumps(saved)
    monkeypatch.setattr(provider_receipts, "validate_confirmed_provider_receipt", original)
    paid = p.usage.recover_proxy(p.hold, request_id="req_proxy")
    assert paid.charged_micro == 248 and not paid.output_usable and paid.output_error == "invalid_output"
    with db()() as session:
        usage = p.usage._read_event(session, p.hold, "responded")["usage"]
        assert usage["provider_output_error"] == "invalid_output" and usage["billing_output_error"] is True
        assert "error_code" not in usage["provider_receipt"] and "provider_output_error" not in usage["provider_receipt"]
    before = state(p)
    assert len(before["ledger"]) == len(before["records"]) == 1
    assert p.usage.recover_proxy(p.hold, request_id="req_proxy") == paid and state(p) == before


@pytest.mark.parametrize("bad", [True, "arbitrary_output_diagnostic"])
def test_malformed_output_diagnostic_is_rejected_without_persisting_it(proxy, bad):
    from app.credits.errors import CreditsUnavailable
    from app.state import db
    p = proxy
    begin(p)
    receipt = deepcopy(p.receipt)
    receipt["error_code"] = bad
    with pytest.raises(CreditsUnavailable):
        p.usage.record_proxy_outcome(p.hold, request_id="req_proxy", confirmed_receipt=receipt)
    with db()() as session:
        assert p.usage._read_event(session, p.hold, "response") is None
        assert p.usage._read_event(session, p.hold, "responded") is None
    assert p.usage.inspect_proxy_call(p.hold, request_id="req_proxy") == "uncertain"
    assert state(p)["status"] == "reconciling" and not state(p)["ledger"] and not state(p)["records"]


@pytest.mark.parametrize("prompt,raw,charged,clamped", [(510, 1020, 1020, False), (1500, 3000, 1050, True)])
def test_over_admission_and_clamped_original_debit_never_applies_output(proxy, prompt, raw, charged, clamped):
    p = proxy
    begin(p)
    receipt = deepcopy(p.receipt)
    receipt.update(input_tokens=prompt, output_tokens=0, total_tokens=prompt, cached_tokens=0, reasoning_tokens=0)
    p.usage.record_proxy_outcome(p.hold, request_id="req_proxy", confirmed_receipt=receipt)
    first = p.usage.settle_proxy(p.hold, request_id="req_proxy")
    assert first.charged_micro == charged and not first.output_usable and first.output_error == "credit_overage"
    paid = state(p)
    meta = json.loads(paid["ledger"][0][4])
    assert meta["clamped"] is clamped and meta["provider_actual_micro"] == meta["raw_provider_actual_micro"] == raw
    assert p.usage.recover_proxy(p.hold, request_id="req_proxy") == first
    assert state(p) == paid and len(paid["ledger"]) == 1


def test_app_scope_and_request_binding_contradictions_never_apply(proxy):
    from app.credits.errors import CreditsUnavailable
    p = proxy
    bad = deepcopy(p.capture)
    bad["token_ceiling"] = 419
    with pytest.raises(CreditsUnavailable):
        p.usage.begin_proxy_call(p.hold, app_id=p.app_id, request_id="req_proxy", capture=bad, request_digest="a" * 64)
    with pytest.raises(CreditsUnavailable):
        p.usage.begin_proxy_call(p.hold, app_id="wrong-app", request_id="req_proxy", capture=p.capture, request_digest="a" * 64)
    assert state(p)["spent"] == 0 and not state(p)["records"]


@pytest.mark.parametrize("field", ["input_tokens", "model", "service_tier"])
def test_unsafe_sdk_counter_or_identity_strings_never_enter_any_journal_payload(proxy, field):
    from app.credits.errors import CreditsUnavailable
    from app.db.models import EventLog
    from app.state import db
    p = proxy
    begin(p)
    marker = "CREDENTIAL_QUERY_MARKER sk-proj-do-not-persist-query-credential"
    receipt = deepcopy(p.receipt)
    receipt[field] = marker
    receipt["result"] = {"query": marker}
    with pytest.raises(CreditsUnavailable):
        p.usage.record_proxy_outcome(p.hold, request_id="req_proxy", confirmed_receipt=receipt)
    with db()() as s:
        payloads = list(s.scalars(select(EventLog.payload_json)))
    assert payloads and all(marker not in value and "do-not-persist" not in value for value in payloads)
    assert p.usage.inspect_proxy_call(p.hold, request_id="req_proxy") == "uncertain"
    assert state(p)["status"] == "reconciling" and not state(p)["ledger"]


@pytest.mark.parametrize("field", ["input_tokens", "model", "role_metadata"])
def test_sdk_deepcopy_coercion_never_runs_or_fabricates_confirmed_evidence(proxy, field):
    from app.credits.errors import CreditsUnavailable
    from app.state import db
    p = proxy
    begin(p)
    calls = []
    class CoercingSDK:
        def __deepcopy__(self, memo):
            calls.append("copied")
            return p.receipt[field]
    receipt = deepcopy(p.receipt)
    receipt[field] = CoercingSDK()
    with pytest.raises(CreditsUnavailable):
        p.usage.record_proxy_outcome(p.hold, request_id="req_proxy", confirmed_receipt=receipt)
    assert calls == []
    with db()() as s:
        assert p.usage._read_event(s, p.hold, "response") is None
        assert p.usage._read_event(s, p.hold, "responded") is None
    assert state(p)["status"] == "reconciling" and not state(p)["ledger"]


@pytest.mark.parametrize("field", ["estimate_micro", "model_binding", "pricing_rate"])
def test_capture_deepcopy_coercion_never_runs_or_grants_dispatch(proxy, field):
    from app.credits.errors import CreditsUnavailable
    from app.state import db
    p = proxy
    calls = []
    class CoercingCapture:
        def __deepcopy__(self, memo):
            calls.append("copied")
            return 1000 if field == "estimate_micro" else p.capture["model_binding"] if field == "model_binding" else "2"
    capture = deepcopy(p.capture)
    if field == "pricing_rate":
        capture["model_binding"]["pricing_snapshot"]["models"]["captured-model"]["input_per_mtok"] = CoercingCapture()
    else:
        capture[field] = CoercingCapture()
    with pytest.raises(CreditsUnavailable):
        p.usage.begin_proxy_call(p.hold, app_id=p.app_id, request_id="req_proxy", capture=capture, request_digest="a" * 64)
    assert calls == []
    with db()() as s:
        assert p.usage._read_event(s, p.hold, "begin") is None
        assert p.usage._read_event(s, p.hold, "calling") is None
    assert state(p)["spent"] == 0 and not state(p)["ledger"]


def test_opaque_sdk_result_body_is_ignored_without_copy_hook(proxy):
    p = proxy
    calls = []
    class OpaqueBody:
        def __deepcopy__(self, memo):
            calls.append("copied")
            return {"fabricated": "body"}
    begin(p)
    receipt = deepcopy(p.receipt)
    receipt["result"] = OpaqueBody()
    p.usage.record_proxy_outcome(p.hold, request_id="req_proxy", confirmed_receipt=receipt)
    assert calls == [] and p.usage.settle_proxy(p.hold, request_id="req_proxy").charged_micro == 248


def test_original_workspace_or_venture_account_algorithm_is_unchanged(proxy, monkeypatch):
    from types import SimpleNamespace
    p = proxy
    calls = []
    monkeypatch.setattr(p.usage, "account_for_workspace", lambda ws: calls.append(("workspace", ws)) or SimpleNamespace(id="existing-workspace-account"))
    monkeypatch.setattr(p.usage, "get_account", lambda vid: pytest.fail("fallback queried with existing workspace account"))
    monkeypatch.setattr(p.usage, "reserve", lambda **kw: calls.append(("reserve", kw)) or kw)
    result = p.usage.hold_for_proxy(workspace_id=p.wid, venture_id=p.vid, amount_micro=1,
        request_id="original", model="deployment")
    assert result["account_id"] == "existing-workspace-account"
    monkeypatch.setattr(p.usage, "account_for_workspace", lambda ws: None)
    monkeypatch.setattr(p.usage, "get_account", lambda vid: SimpleNamespace(id="original-venture-fallback"))
    assert p.usage.hold_for_proxy(workspace_id=p.wid, venture_id=p.vid, amount_micro=1,
        request_id="original2", model="deployment")["account_id"] == "original-venture-fallback"


def test_legacy_issuer_preserves_accepted_reservation_shape(proxy, monkeypatch):
    p = proxy
    monkeypatch.setattr(p.usage, "reserve", lambda **kwargs: kwargs)
    result = p.usage.hold_for_proxy(workspace_id=p.wid, venture_id=p.vid, amount_micro=0,
        request_id="legacy-shape", model="deployment")
    assert set(result) == {"account_id", "amount_micro", "purpose", "model", "workspace_id", "request_id"}
    assert result["amount_micro"] == 100 and result["account_id"] == p.hold.account_id


def test_legacy_settle_kwargs_and_hold_none_stay_available(proxy):
    p = proxy
    assert p.usage.settle_proxy(None, actual_micro=2, tokens_in=1, tokens_out=0) == 2
    assert p.usage.settle_proxy(p.hold, actual_micro=248, tokens_in=100, tokens_out=20) == 248
    paid = state(p)
    assert paid["spent"] == 248 and len(paid["ledger"]) == 1 and not paid["records"]


@pytest.mark.parametrize("mixed", [{"actual_micro": 0}, {"tokens_in": 0, "tokens_out": 0}])
def test_legacy_and_modern_settlement_modes_cannot_mix(proxy, mixed):
    from app.credits.errors import CreditsUnavailable
    p = proxy
    with pytest.raises(CreditsUnavailable):
        p.usage.settle_proxy(p.hold, request_id="req_proxy", **mixed)
    assert state(p)["spent"] == 0 and not state(p)["ledger"]


def test_saved_modern_hold_cannot_use_legacy_fee_kwargs(proxy):
    from app.credits.errors import CreditsUnavailable
    p = proxy
    begin(p)
    with pytest.raises(CreditsUnavailable):
        p.usage.settle_proxy(p.hold, actual_micro=1, tokens_in=1, tokens_out=0)
    assert state(p)["spent"] == 0 and state(p)["status"] == "reconciling"
    assert not state(p)["ledger"]


def test_temporary_private_call_binding_capture_is_not_silently_accepted(proxy):
    from app.credits.errors import CreditsUnavailable
    p = proxy
    old = deepcopy(p.capture)
    old["call_binding"] = {"purpose": "app.proxy"}
    old.pop("token_ceiling")
    with pytest.raises(CreditsUnavailable):
        p.usage.begin_proxy_call(p.hold, app_id=p.app_id, request_id="req_proxy",
            capture=old, request_digest="a" * 64)
    assert state(p)["status"] == "active" and not state(p)["ledger"]


@pytest.mark.parametrize("field", ["workspace_id", "venture_id", "topic", "target_ref", "actor_id",
                                   "on_behalf_of", "approval_id", "denied_reason"])
def test_foreign_outer_eventlog_custody_never_grants_dispatch(proxy, monkeypatch, field):
    from app.credits.errors import CreditsUnavailable
    from app.db.models import EventLog
    p = proxy
    original = p.usage._append_event
    def wrong_outer(session, hold, phase, payload):
        original(session, hold, phase, payload)
        if phase == "begin":
            row = next(row for row in session.new if isinstance(row, EventLog))
            setattr(row, field, "foreign-synthetic")
    monkeypatch.setattr(p.usage, "_append_event", wrong_outer)
    # The genuine complete BEGIN is persisted once. Its next reader must reject
    # the altered outer field, independently of duplicate-BEGIN prevention.
    with pytest.raises(CreditsUnavailable):
        begin(p)
    with pytest.raises(CreditsUnavailable):
        p.usage.inspect_proxy_call(p.hold, request_id="req_proxy")
    with pytest.raises(CreditsUnavailable):
        p.usage.recover_proxy(p.hold, request_id="req_proxy")
    assert state(p)["status"] == "reconciling" and state(p)["spent"] == 0


@pytest.mark.parametrize("removed", [None, "scope", "owner_person_id"])
def test_saved_account_snapshot_cannot_drop_original_scope_fields(proxy, monkeypatch, removed):
    from app.credits.errors import CreditsUnavailable
    p = proxy
    original = p.usage._append_event
    def partial_snapshot(session, hold, phase, payload):
        if phase == "begin":
            payload = deepcopy(payload)
            if removed is None:
                payload["account_snapshot"] = {}
            else:
                payload["account_snapshot"].pop(removed)
        original(session, hold, phase, payload)
    monkeypatch.setattr(p.usage, "_append_event", partial_snapshot)
    with pytest.raises(CreditsUnavailable):
        begin(p)
    with pytest.raises(CreditsUnavailable):
        p.usage.inspect_proxy_call(p.hold, request_id="req_proxy")
    assert state(p)["spent"] == 0 and not state(p)["ledger"]


@pytest.mark.parametrize("bad", ["model", "tier", "output_limit", "refused", "invalid_output"])
def test_cleared_saved_output_error_cannot_apply_paid_unusable_output(proxy, monkeypatch, bad):
    from app.credits.errors import CreditsUnavailable
    p = proxy
    begin(p)
    receipt = deepcopy(p.receipt)
    if bad == "model":
        receipt["model"] = "other-model"
    elif bad == "tier":
        receipt["service_tier"] = "priority"
    elif bad == "output_limit":
        receipt.update(output_tokens=21, total_tokens=121)
    else:
        receipt["error_code"] = bad
    original = p.usage._append_event
    def clear_error(session, hold, phase, payload):
        if phase == "responded":
            payload = deepcopy(payload)
            assert payload["usage"]["output_error"] is not None
            payload["usage"]["output_error"] = None
        original(session, hold, phase, payload)
    monkeypatch.setattr(p.usage, "_append_event", clear_error)
    p.usage.record_proxy_outcome(p.hold, request_id="req_proxy", confirmed_receipt=receipt)
    with pytest.raises(CreditsUnavailable):
        p.usage.recover_proxy(p.hold, request_id="req_proxy")
    assert state(p)["status"] == "reconciling" and state(p)["spent"] == 0
    assert not state(p)["ledger"] and not state(p)["records"]


def test_foreign_funded_account_cannot_begin_with_claimed_app_scope(proxy):
    from app.credits.errors import CreditsUnavailable
    from app.credits.reserve import reserve
    from tests.credit_fixtures import ensure_test_wallet
    p = proxy
    foreign = ensure_test_wallet()
    hold = reserve(account_id=foreign.id, amount_micro=1000, purpose="app.proxy",
        model="deployment", provider="azure_openai", workspace_id=p.wid, venture_id=p.vid,
        request_id="foreign-synthetic")
    with pytest.raises(CreditsUnavailable):
        p.usage.begin_proxy_call(hold, app_id=p.app_id, request_id="foreign-synthetic",
            capture=p.capture, request_digest="a" * 64)
    from app.db.models import CreditReservation, EventLog
    from app.state import db
    with db()() as session:
        assert session.get(CreditReservation, hold.reservation_id).status == "active"
        assert session.scalar(select(EventLog).where(EventLog.target_ref == hold.reservation_id)) is None


def test_legacy_write_usage_projection_remains_available(proxy):
    from app.db.supymem_models import AppUsageRecord
    from app.state import db
    p = proxy
    p.usage.write_usage(app_id=p.app_id, workspace_id=p.wid, meter="ai_tokens_out", quantity=3,
        credits_micro=2, source="legacy-source", end_user_hash="synthetic-user-hash")
    with db()() as session:
        row = session.scalar(select(AppUsageRecord).where(AppUsageRecord.source == "legacy-source"))
        assert (row.app_id, row.workspace_id, row.quantity, row.credits_micro, row.end_user_hash) == (
            p.app_id, p.wid, 3, 2, "synthetic-user-hash")

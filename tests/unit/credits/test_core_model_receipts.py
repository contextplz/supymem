"""Original quote + real SQLite custody; SDK objects, no provider transport."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

DIGEST = "d" * 64


def binding():
    return {"schema_version": 1, "purpose": "research.deep", "provider": "azure_openai",
        "deployment": "synthetic-deployment", "catalog_model": "captured-model",
        "api_mode": "chat_completions", "endpoint_host": "synthetic.openai.azure.com",
        "input_token_limit": 100, "max_output_tokens": 100, "requested_service_tier": "default",
        "pricing_snapshot": {"schema_version": 1, "currency": "USD", "micro_per_usd": 1000000,
            "source": "configured_model_rate_table", "models": {
                "captured-model": {"input_per_mtok": "2", "output_per_mtok": "10", "cached_input_per_mtok": "0.1"},
                "other-model": {"input_per_mtok": "4", "output_per_mtok": "15"}},
            "aliases": {"captured-model-snapshot": "captured-model"},
            "service_tiers": {"default": "1", "priority": "2"}}}


def sdk(prompt=100, completion=20, cached=80, reasoning=7, *, model="captured-model", tier=None):
    from openai.types import CompletionUsage
    usage = CompletionUsage(prompt_tokens=prompt, completion_tokens=completion, total_tokens=prompt + completion,
        prompt_tokens_details={"cached_tokens": cached}, completion_tokens_details={"reasoning_tokens": reasoning})
    return SimpleNamespace(usage=usage, model=model, service_tier=tier,
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok":true}'), finish_reason="stop")])


@pytest.fixture()
def flow(account, monkeypatch):
    from app.credits import accounts, gate, metered, provider_receipts
    from app.core import operation_receipts
    from app.core.jobs import FatalError
    from app.db.supymem_models import ResearchDossier
    from app.research import metered as research
    from app.state import db
    from core.clock import utcnow
    root = Path(__file__).resolve().parents[3] / "services/company_os/app"
    for module, relative in ((gate, "credits/gate.py"), (metered, "credits/metered.py"),
        (research, "research/metered.py"), (operation_receipts, "core/operation_receipts.py"),
        (provider_receipts, "credits/provider_receipts.py")):
        assert Path(module.__file__).resolve() == root / relative
    vid, wid = account.attribution_venture_id, "ws_wallet:" + account.attribution_venture_id
    did = "rsd_core_receipt"
    with db()() as session:
        session.add(ResearchDossier(id=did, workspace_id=wid, venture_id=vid, kind="deep", state="running",
            summary=json.dumps({"_runtime": {"operations": {}}}), created_at=utcnow()))
        session.commit()
    events, calls, task, finalized = [], [], [], {}
    def read(_):
        with db()() as session:
            row = session.get(ResearchDossier, did)
            session.expunge(row)
            return row
    def document(_):
        return json.loads(read(did).summary)
    allowed = [True]
    def mutate(_, payload, update):
        if payload is not None and not allowed[0]:
            raise FatalError("research_source_changed")
        with db()() as session:
            row = session.get(ResearchDossier, did)
            doc = json.loads(row.summary)
            result = update(row, doc, session)
            row.summary = json.dumps(doc)
            session.commit()
            events.append(doc["_runtime"]["operations"].get("review", {}).get("state"))
            return result
    def hooks(method, *args, **kwargs):
        task.append((method, args, kwargs))
        if method == "before_call":
            assert set(kwargs) == {"estimate_micro", "max_tokens"}
            return "inv_core_receipt"
        if method == "after_call":
            assert finalized.setdefault(args[0], kwargs) == kwargs
    monkeypatch.setattr(gate, "_task_usage", hooks)
    journal = operation_receipts.OperationJournal(read=read, document=document, mutate=mutate,
        before_call=lambda *args: None, prefix="research")
    payload = {"_job_attempt": 1, "_worker_id": "synthetic_worker"}
    def invoke(result=None, *, before=None, capture=None, byok=False, cap=False, parser=json.loads, provider_identity=None):
        capture = binding() if capture is None else capture
        def callback():
            calls.append(True)
            if before:
                before()
            return result if result is not None else sdk()
        return research.perform(did, "review", payload, purpose="research.deep", provider="azure_openai",
            model="synthetic-deployment", callback=callback, estimate=1200, token_ceiling=200,
            journal=journal, parse=parser, byok=byok, cap_at_reservation=cap,
            model_binding=capture, request_digest=DIGEST, provider_identity=provider_identity)
    def saved():
        return journal.operation(did, "review")
    def wallet():
        return accounts.get_account_by_id(account.id)
    def entry():
        from app.credits.ledger import find_by_idempotency_key
        operation = saved()
        usage = operation.get("usage", {})
        return find_by_idempotency_key("settle:" + usage["reservation_id"]) if usage.get("reservation_id") else None
    return SimpleNamespace(invoke=invoke, saved=saved, wallet=wallet, entry=entry, journal=journal,
        account=account, did=did, payload=payload, calls=calls, events=events, task=task,
        finalized=finalized, allowed=allowed, gate=gate, metered=metered, helper=provider_receipts)


@pytest.mark.parametrize("domain", ["industrial procurement", "home cooking"])
def test_local_journal_reconciles_once_without_hosted_money_or_receipt_tampering(flow, monkeypatch, tmp_path, domain):
    from app.credits import local_meter
    from app.credits.errors import CreditsUnavailable

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    before = flow.wallet().spent_micro
    response = sdk()
    response.choices[0].message.content = json.dumps({"domain": domain})
    assert flow.invoke(response, byok=True, cap=True, provider_identity=DIGEST) == {"domain": domain}
    assert flow.invoke(response, byok=True, cap=True, provider_identity=DIGEST) == {"domain": domain}
    usage = flow.saved()["usage"]
    assert flow.saved()["state"] == "completed" and usage["settled"] is True
    assert usage["credits_micro"] == 0 and flow.wallet().spent_micro == before
    assert flow.gate.reconcile_receipt(usage) == 0
    assert len(flow.calls) == 1 and local_meter.snapshot()["month_cost_usd_micro"] == 248
    for changed in ({"provider_actual_micro": 0}, {"request_id": "another-request"},
                    {"local_call_id": "another-call"}, {"credits_micro": 1}, {"credits_micro": False},
                    {"settled": False}, {"provenance": "another-receipt"}, {"provider_identity": "another-provider"}):
        with pytest.raises(CreditsUnavailable):
            flow.gate.reconcile_receipt(usage | changed)


@pytest.mark.parametrize("domain", ["industrial procurement", "home cooking"])
def test_local_rejected_receipt_keeps_exact_unspent_custody(flow, monkeypatch, tmp_path, domain):
    import httpx
    from app.core.jobs import FatalError
    from app.credits.errors import CreditsUnavailable

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    before = flow.wallet().spent_micro
    def reject():
        request = httpx.Request("POST", "https://provider.example.invalid/v1")
        httpx.Response(401, request=request, text=domain).raise_for_status()
    with pytest.raises(FatalError, match="research_provider_rejected"):
        flow.invoke(before=reject, byok=True, cap=True, provider_identity=DIGEST)
    usage = flow.saved()["usage"]
    assert flow.saved()["state"] == "failed" and usage["settled"] is True
    assert usage["credits_micro"] == 0 and flow.wallet().spent_micro == before
    assert flow.gate.reconcile_receipt(usage) == 0
    original = {name: value for name, value in usage.items() if name not in {"settled", "credits_micro"}}
    assert flow.gate.reconcile_receipt(original) == 0
    for changed in ({"local_call_id": "another-call"}, {"request_id": "another-request"},
                    {"provider_identity": "another-provider"}, {"definitive_unspent": False},
                    {"workspace_id": "another-workspace"}, {"venture_id": "another-venture"},
                    {"settled": False}, {"credits_micro": 1}, {"credits_micro": False},
                    {"actual_micro": 0}, {"prompt_tokens": 0}, {"cap_at_reservation": "invalid"}):
        with pytest.raises(CreditsUnavailable):
            flow.gate.reconcile_receipt(usage | changed)


@pytest.mark.parametrize("prompt,completion,cached,reasoning,actual", [(0, 0, 0, 0, 0), (1, 0, 0, 0, 2), (100, 20, 80, 7, 248)])
def test_original_snapshot_real_sdk_exact_fee_and_one_debit(flow, monkeypatch, prompt, completion, cached, reasoning, actual):
    from app.credits import estimate
    from app.llm import pricing
    from app.credits.ledger import verify_chain
    def flip():
        monkeypatch.setattr(pricing, "load_pricing", lambda: pytest.fail("live pricing read after HTTP"))
        monkeypatch.setattr(estimate, "actual_llm_micro", lambda *a, **k: pytest.fail("legacy repricing used"))
        monkeypatch.setattr(flow.gate, "actual_llm_micro", lambda *a, **k: pytest.fail("gate repricing used"))
    response = sdk(prompt, completion, cached, reasoning)
    assert flow.invoke(response, before=flip) == {"ok": True}
    original = flow.entry()
    assert original.amount_micro == -actual and (original.prompt_tokens, original.completion_tokens) == (prompt, completion)
    receipt = flow.saved()["usage"]
    assert receipt["provider_actual_micro"] == actual and receipt["reasoning_tokens"] == reasoning
    assert receipt["provider_receipt"]["service_tier"] is None and receipt["cached_tokens"] == cached
    assert flow.invoke(response) == {"ok": True} and flow.calls == [True]
    assert flow.entry().id == original.id and flow.wallet().spent_micro == actual
    assert flow.finalized == {"inv_core_receipt": {"actual_micro": actual, "tokens_used": prompt + completion}}
    assert verify_chain(flow.account.id) == (True, None)


@pytest.mark.parametrize("model,tier,actual", [("other-model", None, 700), ("captured-model", "priority", 496)])
def test_recognized_actual_model_or_tier_is_charged_then_terminal_unusable(flow, model, tier, actual):
    from app.core.jobs import FatalError
    for _ in range(2):
        with pytest.raises(FatalError, match="provider_receipt_output_unusable"):
            flow.invoke(sdk(model=model, tier=tier))
    assert flow.calls == [True] and flow.wallet().spent_micro == actual
    assert flow.entry().amount_micro == -actual and flow.saved()["state"] == "completed"
    assert flow.saved()["usage"]["model"] == "synthetic-deployment"
    assert flow.saved()["usage"]["provider_receipt"]["model"] == model


def test_capped_wallet_keeps_raw_task_cost_and_replay_refuses_output(flow):
    from app.core.jobs import FatalError
    with pytest.raises(FatalError, match="provider_receipt_output_unusable"):
        flow.invoke(sdk(1000, 1000, 0, 7), cap=True)
    original = flow.entry()
    assert flow.wallet().spent_micro == 1200
    assert json.loads(original.meta_json)["raw_provider_actual_micro"] == 12000
    assert flow.finalized["inv_core_receipt"] == {"actual_micro": 12000, "tokens_used": 2000}
    with pytest.raises(FatalError, match="provider_receipt_output_unusable"):
        flow.invoke()
    assert flow.entry().id == original.id and flow.calls == [True]


def test_clamped_original_charge_is_never_usable(flow):
    from app.core.jobs import FatalError
    for _ in range(2):
        with pytest.raises(FatalError, match="provider_receipt_output_unusable"):
            flow.invoke(sdk(1000, 1000, 0, 7))
    assert flow.calls == [True] and flow.wallet().spent_micro == 1260
    assert json.loads(flow.entry().meta_json)["clamped"] is True
    assert flow.saved()["usage"]["provider_actual_micro"] == 12000


def test_confirmed_checkpoint_precedes_price_failure_and_same_snapshot_recovery(flow, monkeypatch):
    from app.core.jobs import RetryableError
    original = flow.helper.validate_confirmed_provider_receipt
    def fail(*args, **kwargs):
        saved = flow.saved()
        assert saved["state"] == "provider_responded" and saved["usage"]["provider_receipt"]["cached_tokens"] == 80
        assert "actual_micro" not in saved["usage"]
        raise RuntimeError("synthetic pricing projection failure")
    monkeypatch.setattr(flow.helper, "validate_confirmed_provider_receipt", fail)
    with pytest.raises(RetryableError, match="settlement_pending"):
        flow.invoke()
    assert flow.wallet().spent_micro == 0 and flow.wallet().reserved_micro == 1200
    monkeypatch.setattr(flow.helper, "validate_confirmed_provider_receipt", original)
    assert flow.invoke() == {"ok": True}
    assert flow.calls == [True] and flow.wallet().spent_micro == 248


def test_financial_failure_keeps_checkpoint_and_recovers_original_ledger_once(flow, monkeypatch):
    from app.core.jobs import RetryableError
    original = flow.gate.settle
    def fail(*args, **kwargs):
        assert "provider_receipt" in flow.saved()["usage"]
        raise RuntimeError("synthetic ledger write fault")
    monkeypatch.setattr(flow.gate, "settle", fail)
    with pytest.raises(RetryableError, match="settlement_pending"):
        flow.invoke()
    assert flow.saved()["state"] == "responded" and flow.calls == [True] and flow.wallet().spent_micro == 0
    monkeypatch.setattr(flow.gate, "settle", original)
    assert flow.invoke() == {"ok": True} and flow.wallet().spent_micro == 248 and flow.calls == [True]


@pytest.mark.parametrize("changes", [
    {"model": "unknown-safe-alias"}, {"service_tier": "unknown_tier"},
    {"model": "credential/query secret marker"}, {"service_tier": "credential/query secret marker"},
    {"usage": {"prompt_tokens": "credential/query secret marker", "completion_tokens": 20, "total_tokens": 120}},
    {"usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 119}},
    {"usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120, "prompt_tokens_details": {"cached_tokens": None}}},
])
def test_unknown_receipt_is_safe_pending_without_zero_fee_or_redial(flow, changes):
    from app.core.jobs import RetryableError
    result = sdk()
    for name, value in changes.items():
        setattr(result, name, value)
    for _ in range(2):
        with pytest.raises(RetryableError, match="settlement_pending"):
            flow.invoke(result)
    text = json.dumps(flow.saved())
    assert "credential/query secret marker" not in text and "actual_micro" not in flow.saved()["usage"]
    assert flow.calls == [True] and flow.wallet().spent_micro == 0 and flow.wallet().reserved_micro == 1200
    assert not flow.finalized


@pytest.mark.parametrize("field,value", [("requested_service_tier", None), ("deployment", "other"),
    ("provider", "other"), ("purpose", "other"), ("input_token_limit", 101)])
def test_contradictory_admission_refuses_before_prepare_hold_task_or_dispatch(flow, field, value):
    from app.credits.errors import CreditsUnavailable
    capture = binding()
    capture[field] = value
    with pytest.raises(CreditsUnavailable):
        flow.invoke(capture=capture)
    assert flow.saved() == {} and flow.calls == flow.task == [] and flow.wallet().reserved_micro == 0


class CoercingCopy:
    calls = []
    def __deepcopy__(self, memo):
        self.calls.append("copied")
        return 1
    def __str__(self):
        self.calls.append("stringified")
        return "captured-model"


def test_opaque_capture_never_executes_copy_or_coercion(flow):
    from app.credits.errors import CreditsUnavailable
    CoercingCopy.calls.clear()
    capture = binding()
    capture["input_token_limit"] = CoercingCopy()
    with pytest.raises(CreditsUnavailable):
        flow.invoke(capture=capture)
    assert CoercingCopy.calls == [] and flow.saved() == {} and flow.calls == flow.task == []


@pytest.mark.parametrize("where", ["count", "model", "tier"])
def test_opaque_sdk_never_executes_copy_or_fabricates_receipt(flow, where):
    from app.core.jobs import RetryableError
    CoercingCopy.calls.clear()
    result = sdk()
    if where == "count":
        result.usage = {"prompt_tokens": CoercingCopy(), "completion_tokens": 20, "total_tokens": 120}
    else:
        setattr(result, "model" if where == "model" else "service_tier", CoercingCopy())
    with pytest.raises(RetryableError):
        flow.invoke(result)
    assert CoercingCopy.calls == [] and flow.saved()["usage"]["provider_receipt"] is None
    assert "actual_micro" not in flow.saved()["usage"] and flow.wallet().spent_micro == 0


def test_funded_cleanup_survives_revocation_but_output_fence_still_refuses(flow, monkeypatch):
    from app.core.jobs import FatalError, RetryableError
    original = flow.gate.settle
    monkeypatch.setattr(flow.gate, "settle", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("fault")))
    with pytest.raises(RetryableError):
        flow.invoke()
    flow.allowed[0] = False
    monkeypatch.setattr(flow.gate, "settle", original)
    with pytest.raises(FatalError, match="source_changed"):
        flow.invoke()
    assert flow.saved()["state"] == "completed" and flow.wallet().spent_micro == 248 and flow.calls == [True]


def test_known_invalid_output_is_charged_and_never_reparsed_or_redialed(flow):
    from app.core.jobs import FatalError
    def invalid(content):
        raise ValueError("synthetic invalid output")
    for _ in range(2):
        with pytest.raises(FatalError, match="invalid_output"):
            flow.invoke(parser=invalid)
    assert flow.wallet().spent_micro == 248 and flow.calls == [True] and flow.saved()["response"] == {}


def test_byok_frozen_receipt_keeps_raw_budget_and_original_zero_debit(flow):
    assert flow.invoke(byok=True) == {"ok": True}
    assert flow.invoke(byok=True) == {"ok": True} and flow.calls == [True]
    assert flow.wallet().spent_micro == flow.wallet().reserved_micro == 0
    assert flow.finalized == {"inv_core_receipt": {"actual_micro": 248, "tokens_used": 120}}


@pytest.fixture()
def actor_wallet(credits_env, monkeypatch):
    """Existing signup custody, with no invented workspace or venture attribution."""
    from datetime import timedelta
    from app.credits import accounts, gate, metered
    from app.credits._txn import naive_utc
    from app.credits.grants import grant
    from app.db.models import ConsumerUser, CreditAccount, Person
    from app.db.supymem_models import EntryChatProposal
    from app.state import db
    from app.tenancy import bind
    monkeypatch.setattr(gate, "_task_usage", lambda *a, **k: None)

    def make(scope="person", actor="usr_original_actor"):
        now = naive_utc()
        sub, aid, uid = "sub:" + actor, "cra:" + actor, "csu:" + actor
        proposal_id = "ecp:" + actor
        with db()() as session:
            session.add(Person(id=actor, display_name="Synthetic actor", created_at=now))
            session.flush()
            session.add(CreditAccount(id=aid, scope="person" if scope == "person" else "venture",
                owner_person_id=actor, workspace_id=None, venture_id=None if scope == "person" else "consumer:" + sub,
                granted_micro=0, spent_micro=0, reserved_micro=0, status="active", version=0,
                created_at=now, updated_at=now))
            session.add(ConsumerUser(id=uid, cognito_sub=sub, email=actor + "@example.test", person_id=actor,
                credit_account_id=aid, plan="trial", status="active", plan_started_at=now,
                plan_expires_at=now + timedelta(days=14), created_at=now, updated_at=now))
            session.add(EntryChatProposal(id=proposal_id, actor_id=actor, request_id="original-preview",
                text="Synthetic original request", route="clarify", target_venture_id=None,
                skipped_steps_json="[]", message="Reading", next_step="clarify", state="classifying",
                receipt_json='{"created_new":true}', created_at=now, expires_at=now + timedelta(days=1), updated_at=now))
            session.commit()
        grant(account_id=aid, amount_micro=5000, reason="Synthetic preexisting signup funding",
            actor_kind="system", actor_id="system", idempotency_key="initial:" + aid)
        capture = binding()
        capture["purpose"] = "intake.classify"
        args = dict(workspace_id=None, venture_id=None, account_id=aid, actor_id=actor,
            purpose="intake.classify", provider="azure_openai", model="synthetic-deployment",
            estimate_micro=1200, max_tokens=200, request_id="entry:" + proposal_id + ":classify",
            model_binding=capture, request_digest=DIGEST)
        calls = []
        def invoke(callback=None, *, changes=None, checkpoint=None, context_actor=None):
            def dispatch():
                calls.append(True)
                return callback() if callback is not None else sdk()
            with bind(workspace_id="none", venture_id="", actor=context_actor or actor):
                return metered.call_sync(dispatch, **(args | (changes or {})),
                    before_model_receipt=checkpoint or (lambda *a: None))
        return SimpleNamespace(actor=actor, account_id=aid, user_id=uid, proposal_id=proposal_id,
            args=args, calls=calls, invoke=invoke, gate=gate, metered=metered,
            wallet=lambda: accounts.get_account_by_id(aid),
            key=metered.model_call_key(**{name: args[name] for name in
                ("workspace_id", "venture_id", "purpose", "request_id", "actor_id")}))
    return make


def test_actor_key_preserves_workspace_bytes_and_binds_original_person(credits_env):
    import hashlib
    from app.credits.metered import model_call_key
    args = dict(workspace_id="workspace", venture_id="venture", purpose="intake.classify", request_id="original")
    old = "core-model:" + hashlib.sha256(json.dumps(list(args.values()), separators=(",", ":")).encode()).hexdigest()
    assert model_call_key(**args) == old
    person = args | {"workspace_id": None, "venture_id": None, "actor_id": "usr_alice"}
    assert model_call_key(**person) != model_call_key(**(person | {"actor_id": "usr_bob"}))
    assert model_call_key(**person) == model_call_key(**person)


@pytest.mark.parametrize("changes", [{"actor_id": None}, {"actor_id": True}, {"actor_id": " "},
    {"actor_id": CoercingCopy()}, {"workspace_id": "none"}, {"venture_id": "foreign-venture"}])
def test_actor_key_rejects_absent_opaque_or_borrowed_scope(credits_env, changes):
    from app.credits.errors import CreditsUnavailable
    from app.credits.metered import model_call_key
    CoercingCopy.calls.clear()
    args = dict(workspace_id=None, venture_id=None, purpose="intake.classify", request_id="original", actor_id="usr_alice")
    with pytest.raises(CreditsUnavailable):
        model_call_key(**(args | changes))
    assert CoercingCopy.calls == []


@pytest.mark.parametrize("scope", ["person", "consumer_legacy"])
def test_original_actor_signup_scope_charges_once_and_preserves_null_attribution(actor_wallet, scope):
    from sqlalchemy import select
    from app.credits.ledger import find_by_idempotency_key, verify_chain
    from app.db.models import CreditLedgerEntry, CreditReservation
    from app.state import db
    flow = actor_wallet(scope)
    def checkpoint(result, usage):
        assert usage["actor_id"] == flow.actor
        assert usage["workspace_id"] is usage["venture_id"] is None
        assert model_events(flow)["response"]["usage"] == usage and "actual_micro" not in usage
    assert flow.invoke(checkpoint=checkpoint).model == "captured-model"
    events = model_events(flow)
    usage = events["begin"]["usage"]
    assert events["begin"]["account_snapshot"]["owner_person_id"] == flow.actor
    original = find_by_idempotency_key("settle:" + usage["reservation_id"])
    assert original.amount_micro == -248 and (original.prompt_tokens, original.completion_tokens) == (100, 20)
    # Null attribution is stored in settlement metadata, not ledger columns.
    metadata = json.loads(original.meta_json)
    assert metadata["workspace_id"] is metadata["venture_id"] is None
    result = flow.metered.recover_model_call(flow.key)
    assert result == {"credits_micro": 248, "output_usable": True, "outcome": "confirmed"}
    assert flow.metered.recover_model_call(flow.key) == result
    with pytest.raises(flow.gate.CreditsUnavailable):
        flow.invoke()
    assert flow.calls == [True] and flow.wallet().spent_micro == 248 and flow.wallet().reserved_micro == 0
    assert find_by_idempotency_key("settle:" + usage["reservation_id"]).id == original.id
    with db()() as session:
        assert len(session.scalars(select(CreditReservation)).all()) == 1
        assert len(session.scalars(select(CreditLedgerEntry).where(CreditLedgerEntry.kind == "grant")).all()) == 1
    assert verify_chain(flow.account_id) == (True, None)


@pytest.mark.parametrize("change", ["foreign_owner", "foreign_consumer", "missing_link", "inactive_plan",
    "expired_plan", "suspended_account", "generic_venture", "wrong_consumer_sub"])
def test_actor_admission_refuses_foreign_or_inactive_existing_custody_before_hold(actor_wallet, change):
    from datetime import timedelta
    from sqlalchemy import select
    from app.credits._txn import naive_utc
    from app.db.models import ConsumerUser, CreditAccount, CreditReservation, Person
    from app.state import db
    flow = actor_wallet()
    with db()() as session:
        user, account = session.get(ConsumerUser, flow.user_id), session.get(CreditAccount, flow.account_id)
        if change in {"foreign_owner", "foreign_consumer"}:
            session.add(Person(id="usr_foreign", display_name="Foreign actor", created_at=naive_utc()))
            session.flush()
            if change == "foreign_owner":
                account.owner_person_id = "usr_foreign"
            else:
                user.person_id = "usr_foreign"
        elif change == "missing_link":
            user.credit_account_id = None
        elif change == "inactive_plan":
            user.status = "suspended"
        elif change == "expired_plan":
            user.plan_expires_at = naive_utc() - timedelta(seconds=1)
        elif change == "suspended_account":
            account.status = "suspended"
        else:
            account.scope, account.venture_id = "venture", "ven_unrelated" if change == "generic_venture" else "consumer:other-sub"
        session.commit()
    with pytest.raises(flow.gate.CreditsUnavailable):
        flow.invoke()
    assert flow.calls == [] and model_events(flow) == {}
    assert flow.wallet().reserved_micro == flow.wallet().spent_micro == 0
    with db()() as session:
        assert session.scalars(select(CreditReservation)).all() == []


def test_actor_admission_uses_original_context_not_a_supplied_actor_label(actor_wallet):
    flow = actor_wallet()
    with pytest.raises(flow.gate.CreditsUnavailable):
        flow.invoke(context_actor="usr_foreign")
    assert flow.calls == [] and model_events(flow) == {} and flow.wallet().reserved_micro == 0


@pytest.mark.parametrize("stage", ["reserve", "protect"])
def test_actor_plan_revocation_before_dispatch_never_releases_ambiguous_original_hold(actor_wallet, monkeypatch, stage):
    from app.credits import reserve
    from app.db.models import ConsumerUser
    from app.state import db
    flow = actor_wallet()
    def revoke():
        with db()() as session:
            session.get(ConsumerUser, flow.user_id).status = "suspended"
            session.commit()
    if stage == "reserve":
        original = flow.gate.reserve
        def changed_reserve(**kwargs):
            hold = original(**kwargs)
            revoke()
            return hold
        monkeypatch.setattr(flow.gate, "reserve", changed_reserve)
    else:
        original = reserve.protect_outcome
        def changed_protect(hold):
            original(hold)
            revoke()
        monkeypatch.setattr(reserve, "protect_outcome", changed_protect)
    with pytest.raises(flow.gate.CreditsUnavailable):
        flow.invoke()
    assert flow.calls == [] and flow.wallet().spent_micro == 0 and flow.wallet().reserved_micro == 1200
    assert set(model_events(flow)) == (set() if stage == "reserve" else {"begin"})


def test_actor_known_paid_recovery_survives_plan_and_consumer_link_revocation(actor_wallet, monkeypatch):
    from app.credits import provider_receipts
    from app.credits.ledger import find_by_idempotency_key
    from app.db.models import ConsumerUser
    from app.state import db
    flow = actor_wallet()
    original_price = provider_receipts.validate_confirmed_provider_receipt
    def failed_price(*args, **kwargs):
        assert "provider_receipt" in model_events(flow)["response"]["usage"]
        raise RuntimeError("Synthetic price fault")
    monkeypatch.setattr(provider_receipts, "validate_confirmed_provider_receipt", failed_price)
    with pytest.raises(RuntimeError):
        flow.invoke()
    assert flow.wallet().spent_micro == 0 and flow.wallet().reserved_micro == 1200
    with db()() as session:
        user = session.get(ConsumerUser, flow.user_id)
        user.status, user.credit_account_id = "suspended", None
        session.commit()
    monkeypatch.setattr(provider_receipts, "validate_confirmed_provider_receipt", original_price)
    result = flow.metered.recover_model_call(flow.key)
    usage = model_events(flow)["begin"]["usage"]
    entry = find_by_idempotency_key("settle:" + usage["reservation_id"])
    assert result == {"credits_micro": 248, "output_usable": True, "outcome": "confirmed"}
    assert entry.amount_micro == -248 and flow.wallet().spent_micro == 248 and flow.wallet().reserved_micro == 0
    assert flow.metered.recover_model_call(flow.key) == result
    assert find_by_idempotency_key("settle:" + usage["reservation_id"]).id == entry.id and flow.calls == [True]


def test_actor_duplicate_begin_in_protect_gap_cannot_release_original_winner(actor_wallet, monkeypatch):
    from app.credits import reserve
    from app.credits.reserve import Hold
    from app.db.models import CreditReservation
    from app.state import db
    flow = actor_wallet()
    original = reserve.protect_outcome
    losers = []
    def interleave(hold):
        usage = model_events(flow)["begin"]["usage"]
        duplicate = flow.gate.Meter(hold=Hold(hold.reservation_id, hold.account_id, hold.amount_micro),
            passthrough=False, _admitted_model_call=True, **{name: usage[name] for name in
                ("purpose", "model", "provider", "workspace_id", "venture_id", "account_id", "actor_id",
                 "request_id", "payer", "estimate_micro", "source", "task_invocation", "model_binding", "request_digest", "token_ceiling")})
        with pytest.raises(flow.gate.CreditsUnavailable):
            flow.metered.begin_model_call(duplicate)
        flow.gate._exit(duplicate)
        with db()() as session:
            assert session.get(CreditReservation, hold.reservation_id).status == "active"
        assert duplicate.outcome_protected and not duplicate.definitive_failure
        losers.append(True)
        original(hold)
    monkeypatch.setattr(reserve, "protect_outcome", interleave)
    assert flow.invoke().model == "captured-model"
    assert losers == flow.calls == [True] and flow.wallet().spent_micro == 248 and flow.wallet().reserved_micro == 0


@pytest.mark.asyncio
async def test_async_actor_call_keeps_original_context_through_gate_thread(actor_wallet):
    from app.tenancy import bind
    flow = actor_wallet()
    async def callback():
        flow.calls.append(True)
        assert model_events(flow)["begin"]["usage"]["actor_id"] == flow.actor
        return sdk()
    with bind(workspace_id="none", venture_id="", actor=flow.actor):
        result = await flow.metered.call(callback, **flow.args, before_model_receipt=lambda *a: None)
    assert result.model == "captured-model" and flow.calls == [True]
    assert flow.wallet().spent_micro == 248 and flow.wallet().reserved_micro == 0


@pytest.mark.parametrize("code,status,released", [("out_of_credits", 402, True), ("out_of_credits", None, False),
    ("out_of_credits", 403, False), ("out_of_credits", 429, False), ("out_of_credits", 500, False),
    ("out_of_credits", 402.0, False), ("provider_down", 402, False)])
def test_actor_exact_out_of_credits_402_only_releases_original_hold(actor_wallet, code, status, released):
    from llm_gateway.errors import ProviderCallError
    flow = actor_wallet()
    def rejected():
        raise ProviderCallError(code, "Synthetic private provider message", status=status)
    with pytest.raises(ProviderCallError):
        flow.invoke(rejected)
    events = model_events(flow)
    assert flow.calls == [True] and flow.wallet().spent_micro == 0
    assert events["failure"]["usage"]["actor_id"] == flow.actor
    assert "Synthetic private provider message" not in json.dumps(events)
    if released:
        assert flow.wallet().reserved_micro == 0
        assert flow.metered.recover_model_call(flow.key) == {"credits_micro": 0, "output_usable": False, "outcome": "rejected"}
    else:
        assert flow.wallet().reserved_micro == 1200 and "completed" not in events
        with pytest.raises(flow.gate.CreditsUnavailable):
            flow.metered.recover_model_call(flow.key)


def test_pure_confirmed_capture_needs_no_meter_account_or_database(credits_env, monkeypatch):
    from app.credits import gate, metered
    from app import state
    monkeypatch.setattr(gate, "pending_model_receipt", lambda *a: pytest.fail("pure capture read a Meter"))
    monkeypatch.setattr(state, "db", lambda *a: pytest.fail("pure capture read a database"))
    result = sdk(model="captured-model-snapshot")
    evidence = metered.capture_confirmed_model_receipt(result, model_binding=binding(), request_digest=DIGEST)
    assert set(evidence) == {"provider_receipt"}
    receipt = evidence["provider_receipt"]
    assert receipt["model"] == "captured-model-snapshot" and receipt["service_tier"] is None
    assert receipt["catalog_model"] == "captured-model" and receipt["deployment"] == "synthetic-deployment"
    assert (receipt["input_tokens"], receipt["output_tokens"], receipt["total_tokens"],
        receipt["cached_tokens"], receipt["reasoning_tokens"]) == (100, 20, 120, 80, 7)
    assert set(receipt["role_metadata"]) == {"purpose", "provider", "model", "catalog_model", "api_mode", "max_output_tokens", "endpoint_host"}
    assert "choices" not in json.dumps(evidence) and "actual_micro" not in receipt


def test_partial_sdk_usage_breakdown_does_not_obscure_confirmed_counts(credits_env):
    from app.credits import metered
    from openai.types import CompletionUsage
    result = sdk()
    result.usage = CompletionUsage(prompt_tokens=100, completion_tokens=20, total_tokens=120,
        prompt_tokens_details={"cached_tokens": 80})
    receipt = metered.capture_confirmed_model_receipt(result, model_binding=binding(), request_digest=DIGEST)["provider_receipt"]
    assert receipt and receipt["cached_tokens"] == 80 and receipt["reasoning_tokens"] == 0
    result.usage.completion_tokens_details = None
    assert metered.capture_confirmed_model_receipt(result, model_binding=binding(), request_digest=DIGEST)["provider_receipt"] is None


def test_capture_meter_wrapper_keeps_original_pending_shape(credits_env):
    from app.credits import gate, metered
    meter = gate.Meter(hold=None, passthrough=True, purpose="research.deep", model="synthetic-deployment",
        provider="azure_openai", model_binding=binding(), request_digest=DIGEST, request_id="original",
        estimate_micro=1200, token_ceiling=200)
    result = sdk()
    assert metered.capture_model_receipt(meter, result) == gate.pending_model_receipt(meter) | metered.capture_confirmed_model_receipt(
        result, model_binding=binding(), request_digest=DIGEST)
    assert "actor_id" not in gate.pending_model_receipt(meter)


@pytest.mark.parametrize("bad", [None, True, {}, [], "invalid", 1, {"cached_tokens": None}, CoercingCopy()])
def test_pure_capture_missing_or_opaque_details_are_unknown_not_zero(credits_env, bad):
    from app.credits import metered
    CoercingCopy.calls.clear()
    result = sdk()
    result.usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
        "prompt_tokens_details": bad, "completion_tokens_details": {"reasoning_tokens": 7}}
    assert metered.capture_confirmed_model_receipt(result, model_binding=binding(), request_digest=DIGEST) == {
        "provider_receipt": None, "model_receipt_error": "counts_unknown"}
    assert CoercingCopy.calls == []


@pytest.mark.parametrize("details", [{}, {"prompt_tokens_details": None, "completion_tokens_details": None}])
@pytest.mark.parametrize("native", [False, True])
def test_pure_capture_unreported_breakdown_is_conservative_and_projectable(credits_env, details, native):
    from app.credits import metered, provider_receipts
    result = sdk(6, 2, model="captured-model")
    names = ("input_tokens", "output_tokens", "input_tokens_details", "output_tokens_details") if native else (
        "prompt_tokens", "completion_tokens", "prompt_tokens_details", "completion_tokens_details")
    result.usage = {names[0]: 6, names[1]: 2, "total_tokens": 8,
        **({names[2]: None, names[3]: None} if details else {})}
    evidence = metered.capture_confirmed_model_receipt(result, model_binding=binding(), request_digest=DIGEST)
    receipt = evidence["provider_receipt"]
    assert receipt is not None and receipt["cached_tokens"] == receipt["reasoning_tokens"] == 0
    projected = provider_receipts.validate_confirmed_provider_receipt(receipt, model_binding=binding(), expected_request_digest=DIGEST)
    assert projected["provider_actual_micro"] == 32 and projected["output_usable"] is True


@pytest.mark.parametrize("flag", [True, "unknown_output", "invalid_output"])
def test_pure_capture_closed_output_carrier_is_separate_from_fee_dto(credits_env, flag):
    from app.credits import metered
    result = sdk()
    result.provider_output_error = flag
    evidence = metered.capture_confirmed_model_receipt(result, model_binding=binding(), request_digest=DIGEST)
    if flag == "invalid_output":
        assert set(evidence) == {"provider_receipt", "provider_output_error"}
        assert evidence["provider_output_error"] == flag and "provider_output_error" not in evidence["provider_receipt"]
    else:
        assert evidence == {"provider_receipt": None, "model_receipt_error": "model_unknown"}


def test_pure_capture_rejects_original_opaque_binding_before_copy_or_sdk_read(credits_env):
    from app.credits import metered
    CoercingCopy.calls.clear()
    capture = binding()
    capture["input_token_limit"] = CoercingCopy()
    class UnreadSDK:
        @property
        def usage(self):
            pytest.fail("invalid original binding read the SDK")
    with pytest.raises(metered.CreditsUnavailable):
        metered.capture_confirmed_model_receipt(UnreadSDK(), model_binding=capture, request_digest=DIGEST)
    assert CoercingCopy.calls == []


def test_unknown_transport_keeps_hold_and_cannot_redial(flow):
    from app.core.jobs import FatalError
    def unknown():
        raise TimeoutError("synthetic unknown transport")
    for _ in range(2):
        with pytest.raises(FatalError, match="outcome_uncertain|operation_in_progress"):
            flow.invoke(before=unknown)
    assert flow.calls == [True] and flow.wallet().reserved_micro == 1200 and flow.wallet().spent_micro == 0


def test_safe_checkpoint_hook_runs_before_price_and_is_detached(flow, monkeypatch):
    from app.credits.provider_receipts import validate_confirmed_provider_receipt
    seen = []
    def checkpoint(result, usage):
        assert "actual_micro" not in usage and usage["provider_receipt"]["reasoning_tokens"] == 7
        seen.append(deepcopy(usage))
        usage["model_binding"]["pricing_snapshot"]["models"]["captured-model"]["input_per_mtok"] = "999"
    def price(*a, **k):
        assert len(seen) == 1
        return validate_confirmed_provider_receipt(*a, **k)
    monkeypatch.setattr(flow.helper, "validate_confirmed_provider_receipt", price)
    result = flow.metered.call_sync(sdk, purpose="research.deep", estimate_micro=1200,
        workspace_id="ws_wallet:" + flow.account.attribution_venture_id, venture_id=flow.account.attribution_venture_id,
        account_id=flow.account.id, provider="azure_openai", model="synthetic-deployment", request_id="interactive:1",
        max_tokens=200, model_binding=binding(), request_digest=DIGEST, before_model_receipt=checkpoint)
    assert result.model == "captured-model" and flow.wallet().spent_micro == 248


def test_missing_checkpoint_refuses_frozen_interactive_call_before_hold(flow):
    from app.credits.errors import CreditsUnavailable
    with pytest.raises(CreditsUnavailable, match="checkpoint"):
        flow.metered.call_sync(lambda: pytest.fail("must not dispatch"), purpose="research.deep", estimate_micro=1200,
            account_id=flow.account.id, provider="azure_openai", model="synthetic-deployment", max_tokens=200,
            model_binding=binding(), request_digest=DIGEST)
    assert flow.wallet().reserved_micro == 0 and flow.task == []


def test_direct_gate_cannot_settle_a_caller_fabricated_frozen_fee(flow):
    from app.credits.errors import CreditsUnavailable
    with pytest.raises(CreditsUnavailable, match="reconciliation"):
        with flow.gate.charge_sync(account_id=flow.account.id, purpose="research.deep", estimate_micro=1200,
            workspace_id="ws_wallet:" + flow.account.attribution_venture_id, venture_id=flow.account.attribution_venture_id,
            provider="azure_openai", model="synthetic-deployment", max_tokens=200,
            model_binding=binding(), request_digest=DIGEST, request_id="direct:1") as meter:
            meter.protect_outcome()
            meter.record(prompt_tokens=1, completion_tokens=0)
            meter.record_micro(0)
    assert flow.wallet().spent_micro == 0 and flow.wallet().reserved_micro == 1200
    assert not flow.finalized


def test_sdk_responses_counts_cache_and_reasoning_use_same_pure_receipt(flow):
    from openai.types.responses import ResponseUsage
    result = sdk()
    result.usage = ResponseUsage(input_tokens=100, output_tokens=20, total_tokens=120,
        input_tokens_details={"cached_tokens": 80, "cache_write_tokens": 0}, output_tokens_details={"reasoning_tokens": 7})
    capture = binding()
    capture["api_mode"] = "responses"
    assert flow.invoke(result, capture=capture) == {"ok": True}
    assert flow.saved()["usage"]["provider_receipt"]["api_mode"] == "responses"
    assert flow.wallet().spent_micro == 248


def test_lost_settle_ack_recovers_exact_original_debit_once(flow, monkeypatch):
    from app.core.jobs import RetryableError
    original = flow.gate.settle
    def lost(*a, **k):
        original(*a, **k)
        return None
    monkeypatch.setattr(flow.gate, "settle", lost)
    with pytest.raises(RetryableError, match="settlement_pending"):
        flow.invoke()
    first = flow.entry()
    assert flow.wallet().spent_micro == 248 and flow.calls == [True] and not flow.finalized
    monkeypatch.setattr(flow.gate, "settle", original)
    assert flow.invoke() == {"ok": True} and flow.entry().id == first.id
    assert flow.wallet().spent_micro == 248 and flow.calls == [True]


def test_counter_failure_retains_receipt_and_financial_custody_for_same_invocation(flow, monkeypatch):
    from app.core.jobs import RetryableError
    original = flow.gate._task_usage
    def fault(method, *a, **k):
        if method == "after_call":
            raise RuntimeError("synthetic task counter commit fault")
        return original(method, *a, **k)
    monkeypatch.setattr(flow.gate, "_task_usage", fault)
    with pytest.raises(RetryableError, match="settlement_pending"):
        flow.invoke()
    first = flow.entry()
    assert first.amount_micro == -248 and flow.saved()["state"] == "responded"
    monkeypatch.setattr(flow.gate, "_task_usage", original)
    assert flow.invoke() == {"ok": True} and flow.calls == [True] and flow.entry().id == first.id
    assert flow.finalized == {"inv_core_receipt": {"actual_micro": 248, "tokens_used": 120}}


@pytest.mark.asyncio
async def test_async_receipt_hook_is_awaited_before_pricing(flow):
    import asyncio
    events = []
    async def provider():
        return sdk()
    async def checkpoint(result, usage):
        assert "actual_micro" not in usage
        events.append("saving")
        await asyncio.sleep(0)
        events.append("saved")
    result = await flow.metered.call(provider, purpose="research.deep", estimate_micro=1200,
        workspace_id="ws_wallet:" + flow.account.attribution_venture_id, venture_id=flow.account.attribution_venture_id,
        account_id=flow.account.id, provider="azure_openai", model="synthetic-deployment", request_id="async:1",
        max_tokens=200, model_binding=binding(), request_digest=DIGEST, before_model_receipt=checkpoint)
    assert events == ["saving", "saved"] and result.model == "captured-model" and flow.wallet().spent_micro == 248


def test_sync_receipt_hook_closes_awaitable_without_false_settlement(flow):
    pending = []
    async def save(result, usage):
        pytest.fail("synchronous wrapper fired an async checkpoint")
    def checkpoint(result, usage):
        pending.append(save(result, usage))
        return pending[-1]
    with pytest.raises(TypeError, match="before_model_receipt must be synchronous"):
        flow.metered.call_sync(sdk, purpose="research.deep", estimate_micro=1200,
            workspace_id="ws_wallet:" + flow.account.attribution_venture_id, venture_id=flow.account.attribution_venture_id,
            account_id=flow.account.id, provider="azure_openai", model="synthetic-deployment", request_id="sync:1",
            max_tokens=200, model_binding=binding(), request_digest=DIGEST, before_model_receipt=checkpoint)
    # The hook never runs, but its failure cannot discard confirmed paid SDK
    # evidence already saved by the owner before invoking this hook.
    assert pending[0].cr_frame is None and flow.wallet().spent_micro == 248 and flow.wallet().reserved_micro == 0
    assert model_events(flow)["completed"]["result"]["credits_micro"] == 248
    with pytest.raises(flow.gate.CreditsUnavailable):
        flow.metered.call_sync(lambda: pytest.fail("invalid hook caused a redial"), purpose="research.deep", estimate_micro=1200,
            workspace_id="ws_wallet:" + flow.account.attribution_venture_id, venture_id=flow.account.attribution_venture_id,
            account_id=flow.account.id, provider="azure_openai", model="synthetic-deployment", request_id="sync:1",
            max_tokens=200, model_binding=binding(), request_digest=DIGEST, before_model_receipt=checkpoint)


@pytest.mark.parametrize("change", ["strip_both", "strip_binding", "strip_digest", "digest_mismatch", "bool_version", "opaque_copy"])
def test_trusted_modern_receipt_cannot_downgrade_to_legacy_fake_fee(flow, monkeypatch, change):
    from app.core.jobs import RetryableError
    from app.credits.errors import CreditsUnavailable
    original = flow.helper.validate_confirmed_provider_receipt
    monkeypatch.setattr(flow.helper, "validate_confirmed_provider_receipt",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("synthetic price projection pause")))
    with pytest.raises(RetryableError):
        flow.invoke()
    monkeypatch.setattr(flow.helper, "validate_confirmed_provider_receipt", original)
    usage = flow.gate.project_model_receipt(flow.saved()["usage"])
    expected = {name: deepcopy(usage[name]) for name in ("workspace_id", "venture_id", "request_id",
        "source", "task_invocation", "model_binding", "request_digest")}
    if change in {"strip_both", "strip_binding"}:
        usage.pop("model_binding")
    if change in {"strip_both", "strip_digest"}:
        usage.pop("request_digest")
    if change == "digest_mismatch":
        usage["request_digest"] = "e" * 64
    if change == "bool_version":
        usage["model_binding"]["schema_version"] = True
        # Type-exact binding also fences release, which does not price usage.
        usage["definitive_unspent"] = True
    if change == "opaque_copy":
        CoercingCopy.calls.clear()
        usage["model_binding"]["input_token_limit"] = CoercingCopy()
    usage.update(actual_micro=1, provider_actual_micro=1)
    flow.task.clear()
    with pytest.raises(CreditsUnavailable):
        flow.gate.reconcile_receipt(usage, expected)
    assert flow.wallet().spent_micro == 0 and flow.wallet().reserved_micro == 1200
    assert flow.entry() is None and flow.task == [] and flow.finalized == {}
    assert flow.saved()["state"] == "provider_responded" and flow.calls == [True]
    if change == "opaque_copy":
        assert CoercingCopy.calls == []


def test_explicit_none_binding_pair_preserves_historical_receipt_replay(flow):
    from app.credits.ledger import find_by_idempotency_key
    with flow.gate.charge_sync(account_id=flow.account.id, purpose="research.deep", estimate_micro=1200,
        provider="azure_openai", model="synthetic-deployment", request_id="legacy:1",
        model_binding=None, request_digest=None) as meter:
        meter.record(prompt_tokens=100, completion_tokens=20, cached_tokens=80)
        meter.record_micro(248)
        usage = flow.gate.captured_receipt(meter)
    first = find_by_idempotency_key("settle:" + meter.hold.reservation_id)
    expected = {name: usage[name] for name in ("workspace_id", "venture_id", "request_id", "source", "task_invocation")}
    expected.update(model_binding=None, request_digest=None)
    assert "model_binding" not in usage and "request_digest" not in usage
    assert flow.gate.reconcile_receipt(usage, expected) == 248
    assert find_by_idempotency_key("settle:" + meter.hold.reservation_id).id == first.id
    assert flow.wallet().spent_micro == 248


def interactive_args(flow, request="interactive:owned:1", capture=None):
    capture = binding() if capture is None else capture
    return dict(workspace_id="ws_wallet:" + flow.account.attribution_venture_id,
        venture_id=flow.account.attribution_venture_id, account_id=flow.account.id,
        purpose=capture["purpose"], provider=capture["provider"], model=capture["deployment"],
        estimate_micro=1200, max_tokens=200, request_id=request,
        model_binding=capture, request_digest=DIGEST)


def model_events(flow):
    from sqlalchemy import select
    from app.db.models import EventLog
    from app.state import db
    with db()() as session:
        rows = session.scalars(select(EventLog).where(EventLog.topic.like("credits.model.receipt.%")).order_by(EventLog.seq)).all()
        return {row.topic.rsplit(".", 1)[-1]: json.loads(row.payload_json) for row in rows}


def model_key(flow, request="interactive:owned:1"):
    return flow.metered.model_call_key(**{name: interactive_args(flow, request)[name] for name in
        ("workspace_id", "venture_id", "purpose", "request_id")})


def test_owned_wrapper_persists_begin_before_dispatch_and_safe_receipt_before_price(flow):
    seen = []
    def callback():
        events = model_events(flow)
        assert set(events) == {"begin", "calling"}
        assert events["begin"]["usage"]["model_binding"] == binding()
        seen.append(True)
        return sdk()
    def checkpoint(result, usage):
        assert set(model_events(flow)) == {"begin", "calling", "response"}
        assert "actual_micro" not in model_events(flow)["response"]["usage"]
    assert flow.metered.call_sync(callback, **interactive_args(flow), before_model_receipt=checkpoint).model == "captured-model"
    events = model_events(flow)
    assert set(events) == {"begin", "calling", "response", "completed"}
    assert events["completed"]["result"] == {"credits_micro": 248, "output_usable": True, "outcome": "confirmed"}
    assert "choices" not in json.dumps(events) and '"ok":' not in json.dumps(events)
    with pytest.raises(flow.gate.CreditsUnavailable):
        flow.metered.call_sync(callback, **interactive_args(flow), before_model_receipt=checkpoint)
    assert seen == [True] and flow.wallet().spent_micro == 248 and flow.wallet().reserved_micro == 0


def test_owned_financial_recovery_uses_saved_snapshot_after_price_fault(flow, monkeypatch):
    from app.llm import pricing
    original = flow.helper.validate_confirmed_provider_receipt
    monkeypatch.setattr(flow.helper, "validate_confirmed_provider_receipt",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("synthetic price fault")))
    with pytest.raises(RuntimeError):
        flow.metered.call_sync(sdk, **interactive_args(flow), before_model_receipt=lambda *a: None)
    assert set(model_events(flow)) == {"begin", "calling", "response"} and flow.wallet().spent_micro == 0
    monkeypatch.setattr(flow.helper, "validate_confirmed_provider_receipt", original)
    monkeypatch.setattr(pricing, "load_pricing", lambda: pytest.fail("recovery read live pricing"))
    result = flow.metered.recover_model_call(model_key(flow))
    assert result == {"credits_micro": 248, "output_usable": True, "outcome": "confirmed"}
    assert flow.metered.recover_model_call(model_key(flow)) == result and flow.wallet().spent_micro == 248
    assert flow.metered.recover_model_call("core-model:" + "a" * 64) is None


@pytest.mark.parametrize("field", ["prompt_tokens_details", "completion_tokens_details"])
@pytest.mark.parametrize("bad", [None, True, {}, CoercingCopy()])
def test_modern_details_have_no_manufactured_zero_or_alias_rescue(flow, field, bad):
    CoercingCopy.calls.clear()
    result = sdk()
    result.usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
        "prompt_tokens_details": {"cached_tokens": 80}, "completion_tokens_details": {"reasoning_tokens": 7}}
    result.usage[field] = bad
    # A full alternate valid alias must not rescue a malformed original shape.
    result.usage.update(input_tokens=100, output_tokens=20,
        input_tokens_details={"cached_tokens": 80}, output_tokens_details={"reasoning_tokens": 7})
    with pytest.raises(flow.gate.CreditsUnavailable):
        flow.metered.call_sync(lambda: result, **interactive_args(flow), before_model_receipt=lambda *a: None)
    assert model_events(flow)["response"]["usage"]["provider_receipt"] is None
    assert flow.wallet().spent_micro == 0 and flow.wallet().reserved_micro == 1200
    assert CoercingCopy.calls == [] and not flow.finalized


def test_contradictory_duplicate_sdk_aliases_remain_protected(flow):
    result = sdk()
    result.usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
        "prompt_tokens_details": {"cached_tokens": 80}, "completion_tokens_details": {"reasoning_tokens": 7},
        "input_tokens": 101, "output_tokens": 20, "input_tokens_details": {"cached_tokens": 80},
        "output_tokens_details": {"reasoning_tokens": 7}}
    with pytest.raises(flow.gate.CreditsUnavailable):
        flow.metered.call_sync(lambda: result, **interactive_args(flow), before_model_receipt=lambda *a: None)
    assert flow.wallet().spent_micro == 0 and model_events(flow)["response"]["usage"]["provider_receipt"] is None


@pytest.mark.parametrize("original_id", [None, "", " "])
def test_missing_modern_original_request_identity_refuses_before_wallet_resolution_or_io(flow, monkeypatch, original_id):
    from app.credits import gate
    monkeypatch.setattr(gate, "get_account_by_id", lambda *a: pytest.fail("resolved payer without original intent"))
    args = interactive_args(flow)
    args["request_id"] = original_id
    with pytest.raises(gate.CreditsUnavailable, match="original model request identity"):
        flow.metered.call_sync(lambda: pytest.fail("dispatch without original intent"), **args, before_model_receipt=lambda *a: None)
    assert flow.wallet().reserved_micro == flow.wallet().spent_micro == 0 and flow.task == [] and model_events(flow) == {}


def test_unknown_begin_ack_and_protection_ack_never_dispatch_or_reallocate(flow, monkeypatch):
    from app.credits import reserve
    original = reserve.protect_outcome
    def unknown(hold):
        original(hold)
        raise RuntimeError("synthetic protect ACK loss")
    monkeypatch.setattr(reserve, "protect_outcome", unknown)
    calls = []
    for _ in range(2):
        with pytest.raises((RuntimeError, flow.gate.CreditsUnavailable)):
            flow.metered.call_sync(lambda: calls.append(True) or sdk(), **interactive_args(flow), before_model_receipt=lambda *a: None)
    assert calls == [] and set(model_events(flow)) == {"begin"}
    assert flow.wallet().reserved_micro == 1200 and flow.wallet().spent_micro == 0
    with pytest.raises(flow.gate.CreditsUnavailable):
        flow.metered.recover_model_call(model_key(flow))


def test_shared_hold_losing_begin_cannot_release_winner_in_protect_gap(flow, monkeypatch):
    from app.credits.reserve import Hold
    from app.credits import reserve
    from app.credits._txn import session_factory
    from app.db.models import CreditReservation
    original = reserve.protect_outcome
    loser = []
    def interleave(hold):
        # Original BEGIN is committed, provider not authorized yet, hold active.
        winner_usage = model_events(flow)["begin"]["usage"]
        duplicate = flow.gate.Meter(hold=Hold(hold.reservation_id, hold.account_id, hold.amount_micro),
            passthrough=False, **{name: winner_usage[name] for name in ("purpose", "model", "provider", "workspace_id",
            "venture_id", "account_id", "request_id", "payer", "estimate_micro", "source", "task_invocation",
            "model_binding", "request_digest", "token_ceiling")}, _admitted_model_call=True)
        with pytest.raises(flow.gate.CreditsUnavailable):
            flow.metered.begin_model_call(duplicate)
        flow.gate._exit(duplicate)
        with session_factory()() as session:
            assert session.get(CreditReservation, hold.reservation_id).status == "active"
        assert duplicate.outcome_protected and not duplicate.definitive_failure
        loser.append(True)
        original(hold)
    monkeypatch.setattr(reserve, "protect_outcome", interleave)
    assert flow.metered.call_sync(sdk, **interactive_args(flow), before_model_receipt=lambda *a: None).model == "captured-model"
    assert loser == [True] and flow.wallet().spent_micro == 248 and flow.wallet().reserved_micro == 0


def test_definitive_owned_rejection_releases_same_hold_and_saved_failure_is_financial_only(flow):
    from llm_gateway.errors import ProviderCallError
    error = ProviderCallError("rate_limited", "synthetic rejection", status=429, retryable=True)
    def reject():
        raise error
    with pytest.raises(ProviderCallError):
        flow.metered.call_sync(reject, **interactive_args(flow), before_model_receipt=lambda *a: None)
    assert flow.wallet().spent_micro == flow.wallet().reserved_micro == 0
    assert flow.metered.recover_model_call(model_key(flow)) == {"credits_micro": 0, "output_usable": False, "outcome": "rejected"}
    assert "synthetic rejection" not in json.dumps(model_events(flow))


def test_unsafe_checkpoint_payload_strings_never_enter_owned_eventlog(flow):
    with pytest.raises(flow.gate.CreditsUnavailable):
        with flow.gate.charge_sync(**interactive_args(flow)) as meter:
            flow.metered.begin_model_call(meter)
            usage = flow.metered.capture_model_receipt(meter, sdk())
            usage["provider_receipt"]["model"] = "credential/query secret marker"
            flow.metered.checkpoint_model_call(meter, usage)
            flow.gate.record_model_receipt(meter, usage)
    assert "credential/query secret marker" not in json.dumps(model_events(flow))
    assert model_events(flow)["response"]["usage"]["provider_receipt"] is None and flow.wallet().spent_micro == 0


@pytest.mark.parametrize("foreign", [{"outcome": "denied"}, {"on_behalf_of": "foreign"}, {"approval_id": "foreign"}, {"denied_reason": "foreign"}])
def test_outer_denied_event_is_never_receipt_authority(flow, foreign):
    from app.db.models import EventLog
    from app.state import db
    from core.clock import utcnow
    key = model_key(flow)
    usage = {"workspace_id": "ws_wallet:" + flow.account.attribution_venture_id,
        "venture_id": flow.account.attribution_venture_id, "purpose": "research.deep", "request_id": "interactive:owned:1",
        "reservation_id": None, "account_id": flow.account.id}
    with db()() as session:
        session.add(EventLog(id=key + ":begin", topic="credits.model.receipt.begin", workspace_id=usage["workspace_id"],
            venture_id=usage["venture_id"], target_ref=key, actor="credits.model", actor_id="credits.model",
            actor_kind="system", surface="internal", ts=utcnow(), payload_json=json.dumps({"usage": usage}),
            **({"outcome": "ok"} | foreign)))
        session.commit()
    with pytest.raises(flow.gate.CreditsUnavailable):
        flow.metered.recover_model_call(key)
    assert flow.wallet().spent_micro == flow.wallet().reserved_micro == 0 and not flow.finalized


def test_distinct_reconciling_candidate_duplicate_is_never_released_as_unspent(flow):
    from app.credits.reserve import reserve, protect_outcome
    from app.credits._txn import session_factory
    from app.db.models import CreditReservation
    with flow.gate.charge_sync(**interactive_args(flow)) as winner:
        flow.metered.begin_model_call(winner)
        candidate = reserve(account_id=flow.account.id, amount_micro=1200, purpose="research.deep",
            model="synthetic-deployment", provider="azure_openai", workspace_id=winner.workspace_id,
            venture_id=winner.venture_id, request_id="older:unrelated", task_invocation=None)
        protect_outcome(candidate)
        old = flow.gate.Meter(hold=candidate, passthrough=False, purpose=winner.purpose, model=winner.model,
            provider=winner.provider, workspace_id=winner.workspace_id, venture_id=winner.venture_id,
            account_id=winner.account_id, request_id=winner.request_id, estimate_micro=winner.estimate_micro,
            model_binding=winner.model_binding, request_digest=winner.request_digest, token_ceiling=200,
            _admitted_model_call=True)
        with pytest.raises(flow.gate.CreditsUnavailable):
            flow.metered.begin_model_call(old)
        flow.gate._exit(old)
        with session_factory()() as session:
            assert session.get(CreditReservation, candidate.reservation_id).status == "reconciling"
        assert old.outcome_protected and not old.definitive_failure
        usage = flow.metered.capture_model_receipt(winner, sdk())
        flow.metered.checkpoint_model_call(winner, usage)
        flow.gate.record_model_receipt(winner, usage)
    assert flow.wallet().spent_micro == 248 and flow.wallet().reserved_micro == 1200


@pytest.mark.parametrize("model,tier,prompt,completion,cached,reasoning", [
    ("other-model", None, 100, 20, 80, 7), ("captured-model", "priority", 100, 20, 80, 7),
    ("captured-model", None, 101, 20, 80, 7), ("captured-model", None, 100, 101, 80, 7),
])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_paid_unusable_sdk_never_reaches_final_hook_or_redispatch(flow, model, tier, prompt, completion, cached, reasoning, asynchronous):
    import asyncio
    dispatched, hooks = [], []
    result = sdk(prompt, completion, cached, reasoning, model=model, tier=tier)
    def provider():
        dispatched.append(True)
        return result
    async def async_provider():
        return provider()
    for _ in range(2):
        with pytest.raises(flow.gate.CreditsUnavailable):
            if asynchronous:
                asyncio.run(flow.metered.call(async_provider, **interactive_args(flow),
                    before_model_receipt=lambda *a: None, before_meter_exit=lambda *a: hooks.append(True)))
            else:
                flow.metered.call_sync(provider, **interactive_args(flow),
                    before_model_receipt=lambda *a: None, before_meter_exit=lambda *a: hooks.append(True))
    assert dispatched == [True] and hooks == [] and flow.wallet().spent_micro > 0
    assert flow.metered.recover_model_call(model_key(flow))["output_usable"] is False


@pytest.fixture()
def composed_gateway(monkeypatch):
    """Canonical composition import; optional local proof reads owner bytes only."""
    import importlib
    import os
    import sys
    source = os.getenv("CORE_MVP_GATEWAY_SOURCE_ROOT")
    previous = {name: value for name, value in sys.modules.items() if name == "llm_gateway" or name.startswith("llm_gateway.")}
    if source:
        for name in previous:
            sys.modules.pop(name)
        monkeypatch.syspath_prepend(str(Path(source) / "packages"))
    try:
        sole = importlib.import_module("llm_gateway.sole")
        responses = pytest.importorskip("llm_gateway.responses", reason="accepted3c38 lacks central Responses adapter; composed-source qualification must execute this test")
        yield sole, responses, importlib.import_module("llm_gateway.provider_foundry")
    finally:
        if source:
            for name in list(sys.modules):
                if name == "llm_gateway" or name.startswith("llm_gateway."):
                    sys.modules.pop(name)
            sys.modules.update(previous)


@pytest.mark.asyncio
@pytest.mark.parametrize("wire_count", [100, "100", True, 100.5])
async def test_actual_sole_responses_sdk_capture_and_gate_no_coerced_wire_evidence(flow, composed_gateway, monkeypatch, record_property, wire_count, _details=None, _known=True, _output=None, _unusable=False):
    from openai import AsyncOpenAI, _base_client
    from pydantic import SecretStr
    import hashlib
    sole, responses, foundry = composed_gateway
    from llm_gateway.boundary import Decision
    httpx = getattr(_base_client, "httpx", None) or _base_client.httpx2
    for module in (sole, responses, foundry):
        record_property(module.__name__ + ".origin", str(Path(module.__file__).resolve()))
        record_property(module.__name__ + ".sha256", hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest())
    capture = binding()
    capture.update(provider="openai", api_mode="responses", catalog_model="gpt-6.1-sol", deployment="gpt-6.1-sol")
    capture["pricing_snapshot"]["models"]["gpt-6.1-sol"] = capture["pricing_snapshot"]["models"].pop("captured-model")
    capture["pricing_snapshot"]["aliases"] = {}
    cfg = foundry.ProviderConfig(provider="openai", base_url="https://synthetic.openai.azure.com/v1", api_key=SecretStr("offline"),
        deployment_pro="gpt-6.1-sol", deployment_flash="gpt-6.1-sol", catalog_model="gpt-6.1-sol", api_mode="responses",
        routing_purpose="research.deep", captured_tier="pro", source="platform_default", reasoning_effort="low")
    requests = []
    def respond(request):
        requests.append(request)
        assert model_events(flow).keys() == {"begin", "calling"}
        usage = {"input_tokens": wire_count, "output_tokens": 20, "total_tokens": 120,
            "input_tokens_details": {"cached_tokens": 80, "cache_write_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 7}}
        if _details is not None:
            usage.update(_details)
            for name, value in _details.items():
                if value == "missing_container":
                    usage.pop(name)
        return httpx.Response(200, json={"id": "offline", "object": "response", "created_at": 0, "status": "completed",
            "model": "gpt-6.1-sol", "service_tier": "default", "output": _output if _output is not None else [{"id": "message", "type": "message", "role": "assistant",
                "status": "completed", "content": [{"type": "output_text", "text": "saved", "annotations": []}]}],
            "usage": usage})
    client = AsyncOpenAI(api_key="offline", base_url=cfg.base_url,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)), max_retries=2)
    monkeypatch.setenv("LLM_API_KEY", "offline")
    monkeypatch.delenv("COMPANY_OS_PACKAGED", raising=False)
    monkeypatch.setattr(sole, "create_async_client_from_config", lambda _: (client, cfg))
    monkeypatch.setattr(sole, "_primary_chat", None)
    monkeypatch.setattr(sole, "_usage_recorder", lambda *a, **k: None)
    monkeypatch.setattr("llm_gateway.boundary._registered", lambda messages, **kwargs: Decision(messages=list(messages)))
    async def callback():
        return await sole.transmit([{"role": "user", "content": "offline"}], purpose="research.deep", config=cfg,
            single_attempt=True, use_workspace_override=False, max_tokens=100)
    args = interactive_args(flow, capture=capture)
    if _unusable:
        hooks = []
        for _ in range(2):
            with pytest.raises(flow.gate.CreditsUnavailable):
                await flow.metered.call(callback, **args, before_model_receipt=lambda *a: None,
                    before_meter_exit=lambda *a: hooks.append(True))
        assert flow.wallet().spent_micro == 248 and hooks == []
        events = model_events(flow)
        assert events["response"]["usage"]["provider_receipt"]["input_tokens"] == 100
        assert events["response"]["usage"]["provider_output_error"] == "invalid_output"
        assert events["completed"]["result"]["output_usable"] is False
    elif type(wire_count) is int and _known:
        result = await flow.metered.call(callback, **args, before_model_receipt=lambda *a: None)
        assert result.usage.prompt_tokens == 100 and flow.wallet().spent_micro == (400 if _details is not None else 248)
    else:
        with pytest.raises(flow.gate.CreditsUnavailable):
            await flow.metered.call(callback, **args, before_model_receipt=lambda *a: None)
        assert flow.wallet().spent_micro == 0 and not flow.finalized
    assert len(requests) == 1 and str(requests[0].url).endswith("/responses")
    assert json.loads(requests[0].content)["service_tier"] == "default"
    assert requests[0].headers["x-stainless-retry-count"] == "0" and client.is_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("deployment,chat_payload,env_drift", [
    ("supymem-gpt-6-1-sol", False, False), ("supymem-gpt-6-1-sol", True, False),
    ("DeepSeek-V4-Pro", True, False), ("supymem-gpt-6-1-sol", False, True),
])
async def test_versioned_foundry_project_receipt_matches_wire_without_redial(flow, composed_gateway, monkeypatch, deployment, chat_payload, env_drift):
    from openai import _base_client
    from llm_gateway.boundary import Decision

    sole, responses, foundry = composed_gateway
    httpx = getattr(_base_client, "httpx", None) or _base_client.httpx2
    for key in ("MODEL_ROUTING_OVERRIDES", "LLM_REASONING_EFFORT"):
        monkeypatch.delenv(key, raising=False)
    for key, value in {"LLM_PROFILE": "prod", "LLM_PROVIDER": "azure_foundry",
            "LLM_API_KEY": "offline", "LLM_API_VERSION": "2024-05-01-preview",
            "LLM_BASE_URL": "https://fixture-resource.services.ai.azure.com/api/projects/fixture-project",
            "LLM_DEPLOYMENT_NAME": deployment, "LLM_DEPLOYMENT_PRO": deployment,
            "LLM_DEPLOYMENT_FLASH": deployment}.items():
        monkeypatch.setenv(key, value)
    cfg = sole.resolve_purpose_config("research.deep", foundry.load_provider_config_from_env())
    if env_drift:
        monkeypatch.setenv("LLM_DEPLOYMENT_PRO", "supymem-pro")
        monkeypatch.setenv("LLM_DEPLOYMENT_FLASH", "supymem-flash")
        monkeypatch.setenv("MODEL_ROUTING_OVERRIDES", "{}")
    capture = binding()
    capture.update(provider=cfg.provider, deployment=deployment, catalog_model=cfg.catalog_model,
        api_mode=cfg.api_mode, endpoint_host="fixture-resource.services.ai.azure.com")
    snapshot = capture["pricing_snapshot"]
    snapshot["models"][cfg.catalog_model] = snapshot["models"].pop("captured-model")
    snapshot["aliases"] = {deployment: cfg.catalog_model}
    requests, clients = [], []
    usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
        "prompt_tokens_details": {"cached_tokens": 80, "cache_write_tokens": 0},
        "completion_tokens_details": {"reasoning_tokens": 7}}

    def respond(request):
        requests.append(request)
        assert set(model_events(flow)) == {"begin", "calling"}
        if chat_payload:
            return httpx.Response(200, json={"id": "chat_offline", "object": "chat.completion",
                "created": 0, "model": deployment, "usage": usage,
                "choices": [{"index": 0, "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "saved"}}]})
        return httpx.Response(200, json={"id": "resp_offline", "object": "response",
            "model": deployment, "status": "completed", "output": [],
            "usage": {"input_tokens": usage["prompt_tokens"], "output_tokens": usage["completion_tokens"],
                "total_tokens": usage["total_tokens"], "input_tokens_details": usage["prompt_tokens_details"],
                "output_tokens_details": usage["completion_tokens_details"]}})

    def transport(**kwargs):
        client = httpx.AsyncClient(**{**kwargs, "transport": httpx.MockTransport(respond)})
        clients.append(client)
        return client

    monkeypatch.setattr(foundry, "DefaultAsyncHttpxClient", transport)
    monkeypatch.setattr(sole, "boundary_decision", lambda messages, **kwargs: Decision(messages=messages))
    monkeypatch.setattr(sole, "_primary_chat", None)
    monkeypatch.setattr(sole, "_usage_recorder", None)
    monkeypatch.setattr(sole, "_require_platform_server", lambda: None)

    async def callback():
        return await sole.transmit([{"role": "user", "content": "Synthetic production receipt check"}],
            purpose="research.deep", config=cfg, max_tokens=100, single_attempt=True)

    args = interactive_args(flow, capture=capture) | {"before_model_receipt": lambda *args: None}
    if cfg.api_mode == "responses" and chat_payload:
        for _ in range(2):
            with pytest.raises(flow.gate.CreditsUnavailable):
                await flow.metered.call(callback, **args)
        evidence = model_events(flow)["response"]["usage"]
        assert evidence["provider_receipt"] is None and evidence["model_receipt_error"] == "counts_unknown"
        assert flow.wallet().spent_micro == 0 and flow.wallet().reserved_micro > 0
    else:
        result = await flow.metered.call(callback, **args)
        assert result.usage.prompt_tokens == 100
        receipt = model_events(flow)["response"]["usage"]["provider_receipt"]
        assert receipt["api_mode"] == capture["api_mode"] == cfg.api_mode
        assert (receipt["input_tokens"], receipt["output_tokens"], receipt["cached_tokens"], receipt["reasoning_tokens"]) == (100, 20, 80, 7)
        assert flow.wallet().spent_micro == 248 and flow.wallet().reserved_micro == 0
        with pytest.raises(flow.gate.CreditsUnavailable):
            await flow.metered.call(callback, **args)
        assert flow.wallet().spent_micro == 248
    expected_path = "/openai/v1/responses" if cfg.api_mode == "responses" else "/api/projects/fixture-project/openai/v1/chat/completions"
    assert len(requests) == 1 and requests[0].url.path == expected_path
    assert requests[0].headers["x-stainless-retry-count"] == "0"
    assert all(client.is_closed for client in clients)


@pytest.mark.asyncio
@pytest.mark.parametrize("details,known", [
    ({"input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}, True),
    ({"input_tokens_details": None}, False), ({"input_tokens_details": {}}, False),
    ({"input_tokens_details": True}, False), ({"output_tokens_details": {}}, False),
    ({"input_tokens_details": False}, False), ({"input_tokens_details": "invalid_container"}, False),
    ({"input_tokens_details": []}, False), ({"input_tokens_details": "missing_container"}, False),
])
async def test_actual_sole_explicit_zero_or_missing_detail_evidence(flow, composed_gateway, monkeypatch, record_property, details, known):
    await test_actual_sole_responses_sdk_capture_and_gate_no_coerced_wire_evidence(
        flow, composed_gateway, monkeypatch, record_property, 100, _details=details, _known=known)


@pytest.mark.asyncio
@pytest.mark.parametrize("output", [[None], [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": 123}]}]])
async def test_actual_sole_known_paid_malformed_body_checkpoints_charges_and_refuses(flow, composed_gateway, monkeypatch, record_property, output):
    await test_actual_sole_responses_sdk_capture_and_gate_no_coerced_wire_evidence(
        flow, composed_gateway, monkeypatch, record_property, 100, _output=output, _unusable=True)


def test_external_known_sdk_evidence_hook_failure_charges_saved_receipt_without_output_or_redial(flow):
    def failed_hook(*args):
        assert "response" in model_events(flow)
        raise RuntimeError("synthetic external evidence hook failure")
    with pytest.raises(RuntimeError, match="external evidence hook"):
        flow.metered.call_sync(sdk, **interactive_args(flow), before_model_receipt=failed_hook)
    assert flow.wallet().spent_micro == 248 and flow.wallet().reserved_micro == 0
    assert model_events(flow)["completed"]["result"]["credits_micro"] == 248
    with pytest.raises(flow.gate.CreditsUnavailable):
        flow.metered.call_sync(lambda: pytest.fail("known hook failure redialed"), **interactive_args(flow), before_model_receipt=failed_hook)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_text", [float("nan"), "\ud800"], ids=["nonfinite_json", "invalid_utf8"])
async def test_actual_raw_media_serialization_failure_keeps_original_paid_receipt(flow, composed_gateway, monkeypatch, bad_text):
    import hashlib
    from openai import AsyncOpenAI, _base_client
    from pydantic import SecretStr
    from llm_gateway.boundary import Decision

    sole, responses, foundry = composed_gateway
    expected = Path(__file__).resolve().parents[3] / "packages/llm_gateway"
    assert Path(sole.__file__).resolve() == expected / "sole.py"
    assert Path(responses.__file__).resolve() == expected / "responses.py"
    capture = binding()
    capture.update(provider="openai", api_mode="responses", catalog_model="gpt-6.1-sol", deployment="gpt-6.1-sol")
    capture["pricing_snapshot"]["models"]["gpt-6.1-sol"] = capture["pricing_snapshot"]["models"].pop("captured-model")
    capture["pricing_snapshot"]["aliases"] = {}
    cfg = foundry.ProviderConfig(provider="openai", base_url="https://synthetic.openai.azure.com/v1",
        api_key=SecretStr("offline"), deployment_pro="gpt-6.1-sol", deployment_flash="gpt-6.1-sol",
        catalog_model="gpt-6.1-sol", api_mode="responses", routing_purpose="research.deep", captured_tier="pro",
        source="platform_default", reasoning_effort="low", max_output_tokens=100)
    clock = "Synthetic UTC clock retained in the original wire quote."
    monkeypatch.setattr(sole, "time_context", lambda: clock)
    body = {"input": [{"role": "user", "content": "Synthetic raw-media request"}],
        "max_output_tokens": 100, "service_tier": "default"}
    wire = body | {"model": cfg.deployment_pro, "input": [{"role": "system", "content": clock}, *body["input"]],
        "store": False, "reasoning": {"effort": "low"}}
    digest = hashlib.sha256(json.dumps(wire, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    httpx = getattr(_base_client, "httpx", None) or _base_client.httpx2
    dispatched, carriers, final_hooks = [], [], []
    original_usage = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
        "input_tokens_details": {"cached_tokens": 80, "cache_write_tokens": 0},
        "output_tokens_details": {"reasoning_tokens": 7}}

    def respond(request):
        assert set(model_events(flow)) == {"begin", "calling"}
        assert json.loads(request.content) == wire
        dispatched.append(request)
        payload = {"id": "offline", "object": "response", "created_at": 0, "status": "completed",
            "model": "gpt-6.1-sol", "service_tier": "default", "usage": original_usage,
            "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": bad_text}]}]}
        return httpx.Response(200, content=json.dumps(payload, ensure_ascii=True, allow_nan=True).encode(),
            headers={"content-type": "application/json"})

    client = AsyncOpenAI(api_key="offline", base_url=cfg.base_url,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)), max_retries=2)
    monkeypatch.setenv("LLM_API_KEY", "offline")
    monkeypatch.delenv("COMPANY_OS_PACKAGED", raising=False)
    monkeypatch.setattr(sole, "create_async_client_from_config", lambda _: (client, cfg))
    monkeypatch.setattr(sole, "_usage_recorder", lambda *a, **k: None)
    monkeypatch.setattr("llm_gateway.boundary._registered", lambda messages, **kwargs: Decision(messages=list(messages)))

    async def callback():
        result = await sole.transmit_responses_request(body, purpose="research.deep", config=cfg,
            workspace_id="ws_wallet:" + flow.account.attribution_venture_id)
        assert result["usage"] == original_usage
        assert result["model"] == "gpt-6.1-sol" and result["service_tier"] == "default"
        assert result["status"] == "failed" and result["provider_output_error"] == "invalid_output"
        assert result["output"] == [] and result["error"] == {"code": "invalid_provider_output"}
        carriers.append(result)
        return result

    def checkpoint(result, usage):
        saved = model_events(flow)["response"]["usage"]
        assert saved["request_digest"] == digest and saved["model_binding"] == capture
        assert saved["provider_output_error"] == "invalid_output" and "actual_micro" not in saved
        assert saved["provider_receipt"]["input_tokens"] == 100
        assert saved["provider_receipt"]["cached_tokens"] == 80
        assert saved["provider_receipt"]["reasoning_tokens"] == 7

    args = interactive_args(flow, request="raw-media:original-intent", capture=capture)
    args["request_digest"] = digest
    for _ in range(2):
        with pytest.raises(flow.gate.CreditsUnavailable):
            await flow.metered.call(callback, **args, before_model_receipt=checkpoint,
                before_meter_exit=lambda *a: final_hooks.append(True))
    assert len(dispatched) == len(carriers) == 1 and final_hooks == [] and client.is_closed()
    assert str(dispatched[0].url).endswith("/responses") and dispatched[0].headers["x-stainless-retry-count"] == "0"
    assert flow.wallet().spent_micro == 248 and flow.wallet().reserved_micro == 0
    from app.credits.ledger import find_by_idempotency_key
    original_hold = model_events(flow)["begin"]["usage"]["reservation_id"]
    original_entry = find_by_idempotency_key("settle:" + original_hold)
    assert original_entry.amount_micro == -248 and (original_entry.prompt_tokens, original_entry.completion_tokens) == (100, 20)
    status = flow.metered.recover_model_call(flow.metered.model_call_key(**{name: args[name] for name in
        ("workspace_id", "venture_id", "purpose", "request_id")}))
    assert status == {"credits_micro": 248, "output_usable": False, "outcome": "confirmed"}
    assert find_by_idempotency_key("settle:" + original_hold).id == original_entry.id and flow.wallet().spent_micro == 248
    assert flow.finalized == {"inv_core_receipt": {"actual_micro": 248, "tokens_used": 120}}


@pytest.mark.asyncio
@pytest.mark.parametrize("audience", ["Warehouse dispatch teams", "Adult language learners"])
@pytest.mark.parametrize("receipt", ["responses", "raw_media"])
async def test_sdk_receipt_restores_boundary_filter(flow, composed_gateway, monkeypatch, record_property, audience, receipt):
    from app.boundary.filter import inspect_payload
    from llm_gateway import boundary

    monkeypatch.setattr(boundary, "_registered", inspect_payload)
    with monkeypatch.context() as isolated:
        if receipt == "responses":
            await test_actual_sole_responses_sdk_capture_and_gate_no_coerced_wire_evidence(
                flow, composed_gateway, isolated, record_property, 100)
        else:
            await test_actual_raw_media_serialization_failure_keeps_original_paid_receipt(
                flow, composed_gateway, isolated, float("nan"))
    assert boundary.resolve_filter() is inspect_payload
    with pytest.raises(boundary.BoundaryViolation):
        boundary.resolve_filter()([{"role": "user", "content": audience + " password=BoundaryFixtureCredentialValue"}],
            purpose="gtm.discovery.judge", workspace_id=flow.journal.read(flow.did).workspace_id, provider="fixture")

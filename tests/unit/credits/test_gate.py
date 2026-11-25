"""Charge facade: settle on success, release on error / forgotten record."""

from __future__ import annotations

import pytest

from tests.unit.chat.test_chat_request_status import chat_request as chat_request


@pytest.fixture(autouse=True)
def hosted_gate(monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)


@pytest.mark.parametrize("domain", ["Inventory planning for manufacturers", "Shared meal plans for families"])
def test_confirm_uses_funded_scoped_venture_not_first_workspace_venture(request, monkeypatch, domain):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    chat = request.getfixturevalue("chat_request")
    from app.chat.conversations import bind_conversation, create
    from app.chat.store import append_message
    from app.credits.accounts import ensure_account, get_account_by_id
    from app.credits.gate import charge_sync
    from app.credits.policy import WALLET_PHASE_KEY
    from app.db.models import CreditAccount, CreditPolicy, CreditReservation, Venture
    from app.tenancy import bind
    from core.clock import utcnow
    from sqlalchemy import select

    unfunded = chat["vid"]
    chat["vid"] = "ven_funded"
    with chat["factory"]() as session:
        session.add(CreditPolicy(key=WALLET_PHASE_KEY, value_json='"legacy"', updated_at=utcnow(), updated_by="test"))
        session.add(Venture(id=chat["vid"], workspace_id=chat["wid"], name="Funded venture",
            raw_idea=domain, stage="draft", created_at=utcnow(), updated_at=utcnow()))
        session.commit()
        assert session.scalar(select(Venture.id).where(Venture.workspace_id == chat["wid"]).limit(1)) == unfunded
    account = ensure_account(chat["vid"], workspace_id=chat["wid"], owner_person_id=chat["actor"])
    with chat["factory"]() as session:
        session.get(CreditAccount, account.id).workspace_id = None
        session.commit()
    calls = []
    original_execute = chat["api"].execute_action

    async def execute(venture_id, action, **kwargs):
        if action == "blocker.explain":
            return await original_execute(venture_id, action, **kwargs)
        with charge_sync(workspace_id=chat["wid"], purpose="web_search", estimate_micro=100,
                         request_id=kwargs["request_nonce"]) as meter:
            calls.append(meter.account_id)
            meter.record_micro(80)
        return {"body_md": "Your search is saved. Check its progress.", "run_id": "saved_search"}

    monkeypatch.setattr(chat["api"], "execute_action", execute)
    with bind(workspace_id=chat["wid"], venture_id=chat["vid"], actor=chat["actor"]):
        chat["cid"] = create(chat["vid"], idempotency_key="funded-conversation")["id"]
        with bind_conversation(chat["vid"], chat["cid"]):
            card = append_message(chat["vid"], role="assistant", kind="confirmation", body_md=domain,
                actions=[{"label": "Start", "confirmation": {"action": "gtm.find", "resulting_state": {}}}])
    path = f"/ventures/{chat['vid']}/chat/confirm"
    body = {"action": "gtm.find", "payload": {}, "message_id": card["id"], "conversation_id": chat["cid"]}
    response = chat["client"].post(path, headers=chat["headers"](path, "POST"), json=body)
    assert response.status_code == 200, response.text
    assert calls == [account.id]
    assert chat["client"].post(path, headers=chat["headers"](path, "POST"), json=body).json() == response.json()
    assert calls == [account.id]
    after = get_account_by_id(account.id)
    assert after.spent_micro == 80 and after.reserved_micro == 0
    with chat["factory"]() as session:
        holds = list(session.scalars(select(CreditReservation)))
        assert len(holds) == 1 and holds[0].venture_id == chat["vid"]
        session.get(CreditAccount, account.id).workspace_id = chat["wid"]
        session.commit()
    with bind(workspace_id=chat["wid"], venture_id=unfunded, actor=chat["actor"]):
        from app.credits.errors import CreditsUnavailable
        with pytest.raises(CreditsUnavailable):
            with charge_sync(workspace_id=chat["wid"], purpose="web_search", estimate_micro=100):
                pytest.fail("an unfunded venture borrowed the other venture's balance")
    with chat["factory"]() as session:
        depleted = session.get(CreditAccount, account.id)
        depleted.spent_micro = depleted.granted_micro
        session.commit()
    body.pop("message_id")
    body["idempotency_key"] = "at-the-limit"
    stopped = chat["client"].post(path, headers=chat["headers"](path, "POST"), json=body)
    assert stopped.status_code == 402 and stopped.json() == {"detail": {"error": {"code": "insufficient_credits"}}}
    assert calls == [account.id]
    notice = chat["client"].post(path, headers=chat["headers"](path, "POST"), json={
        "action": "blocker.explain", "payload": {"code": "insufficient_credits"}, "conversation_id": chat["cid"]})
    assert notice.status_code == 200, notice.text
    assert "Buy more to continue" in notice.json()["body_md"]
    assert notice.json()["kind"] == "text"
    assert notice.json()["actions"] == [{"label": "Buy more", "href": f"/v/{chat['vid']}/settings#settings-credits",
        "style": "primary", "kind": "recovery_link"}]


def test_charge_settles_on_success(account, monkeypatch) -> None:
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    from app.credits.accounts import get_account_by_id
    from app.credits.gate import charge_sync

    with charge_sync(
        account_id=account.id,
        purpose="llm",
        estimate_micro=5_000,
        model="luna",
    ) as meter:
        meter.record_micro(1_200)

    after = get_account_by_id(account.id)
    assert after is not None
    assert after.spent_micro == account.spent_micro + 1_200
    assert after.reserved_micro == 0
    assert meter.settle_result is not None
    assert meter.settle_result.actual_micro == 1_200


@pytest.mark.parametrize("shortfall_micro", [0, 1])
def test_charge_admits_exact_available_quote(account, shortfall_micro, monkeypatch) -> None:
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    from app.credits.accounts import balance, get_account_by_id
    from app.credits.errors import InsufficientCredits
    from app.credits.gate import charge_sync
    from app.credits.reserve import release, reserve
    from app.credits.units import credits_to_micro

    quote = credits_to_micro(300)
    prior = reserve(account_id=account.id, purpose="other_work",
                    amount_micro=int(balance(account)["available_micro"]) - quote + shortfall_micro)
    try:
        if shortfall_micro:
            with pytest.raises(InsufficientCredits) as exc:
                with charge_sync(account_id=account.id, purpose="build", estimate_micro=quote):
                    pytest.fail("a quote above the available balance was admitted")
            assert exc.value.available_micro == quote - shortfall_micro
            assert exc.value.required_micro == quote
        else:
            with charge_sync(account_id=account.id, purpose="build", estimate_micro=quote) as meter:
                assert meter.hold.amount_micro == quote
                meter.record_micro(quote)
        after = get_account_by_id(account.id)
        assert after.spent_micro == account.spent_micro + (0 if shortfall_micro else quote)
        assert after.reserved_micro == prior.amount_micro
    finally:
        release(prior)


def test_charge_releases_on_exception(account, monkeypatch) -> None:
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    from app.credits.accounts import get_account_by_id
    from app.credits.gate import charge_sync

    with pytest.raises(RuntimeError):
        with charge_sync(
            account_id=account.id,
            purpose="llm",
            estimate_micro=5_000,
        ):
            raise RuntimeError("provider failed")

    after = get_account_by_id(account.id)
    assert after is not None
    assert after.reserved_micro == 0
    assert after.spent_micro == account.spent_micro


def test_forgotten_record_releases(account, monkeypatch) -> None:
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    from app.credits.accounts import get_account_by_id
    from app.credits.gate import charge_sync

    with charge_sync(
        account_id=account.id,
        purpose="llm",
        estimate_micro=5_000,
    ) as meter:
        assert meter.hold is not None

    after = get_account_by_id(account.id)
    assert after is not None
    assert after.reserved_micro == 0
    assert after.spent_micro == account.spent_micro


def test_enforcement_disabled_is_passthrough(account, monkeypatch) -> None:
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    from app.credits.accounts import get_account_by_id
    from app.credits.gate import charge_sync
    from app.credits.policy import set_policy

    set_policy("enforcement_enabled", False, updated_by="test")
    with charge_sync(
        account_id=account.id,
        purpose="llm",
        estimate_micro=5_000,
        model="luna",
    ) as meter:
        assert meter.passthrough is True
        assert meter.hold is None
        meter.record(prompt_tokens=10, completion_tokens=4)

    after = get_account_by_id(account.id)
    assert after is not None
    assert after.reserved_micro == 0
    assert after.spent_micro == account.spent_micro


def test_byok_founder_never_reserves(account, monkeypatch) -> None:
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    from app.credits.accounts import get_account_by_id
    from app.credits.gate import charge_sync

    with charge_sync(
        account_id=account.id,
        purpose="llm",
        estimate_micro=account.granted_micro * 2,
        payer="founder",
        model="luna",
    ) as meter:
        assert meter.passthrough is True
        assert meter.hold is None
        meter.record(prompt_tokens=20, completion_tokens=8)

    after = get_account_by_id(account.id)
    assert after is not None
    assert after.reserved_micro == 0
    assert after.spent_micro == account.spent_micro


@pytest.mark.asyncio
async def test_async_charge_settles(account, monkeypatch) -> None:
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    from app.credits.accounts import get_account_by_id
    from app.credits.gate import charge

    async with charge(
        account_id=account.id,
        purpose="llm",
        estimate_micro=3_000,
    ) as meter:
        meter.record_micro(900)

    after = get_account_by_id(account.id)
    assert after is not None
    assert after.spent_micro == account.spent_micro + 900
    assert after.reserved_micro == 0


@pytest.fixture
def local_meter_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/local.db")
    monkeypatch.delenv("SUPYMEM_LOCAL_MONTHLY_CAP_USD", raising=False)
    from tests.company_os_harness import load_company_os_app
    load_company_os_app()
    from app.credits import gate, local_meter

    def no_hosted_money(*args, **kwargs):
        pytest.fail("local calls must not read or write hosted money")

    for name in ("enforcement_enabled", "get_account_by_id", "resolve_charge_target", "reserve", "append"):
        monkeypatch.setattr(gate, name, no_hosted_money)
    return gate, local_meter, tmp_path


def set_local_cap(value):
    from app.core.local_secrets import update_settings
    update_settings("spend", {"monthly_spend_cap": value}, {})


@pytest.mark.parametrize("domain", ["industrial procurement", "home cooking"])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_local_meter_records_provider_usage_without_credits(local_meter_env, domain, asynchronous):
    from types import SimpleNamespace

    from app.credits import metered

    _, local_meter, _ = local_meter_env
    result = SimpleNamespace(usage={"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20,
        "prompt_tokens_details": {"cached_tokens": 4}})
    options = {"purpose": "task.draft", "venture_id": domain, "workspace_id": domain,
        "provider": "test", "model": "test", "estimate_micro": 500, "actual_micro": lambda _: 125}

    if asynchronous:
        async def callback():
            return result
        assert await metered.call(callback, **options) is result
    else:
        assert metered.call_sync(lambda: result, **options) is result

    snapshot = local_meter.snapshot()
    assert snapshot["month_cost_usd_micro"] == 125
    assert len(snapshot["tasks"]) == 1
    task = snapshot["tasks"][0]
    assert task["venture_id"] == domain
    assert (task["prompt_tokens"], task["completion_tokens"], task["cached_tokens"]) == (12, 8, 4)
    assert task["cost_usd_micro"] == 125
    assert not any("credit" in key for key in snapshot)


@pytest.mark.parametrize("domain", ["industrial procurement", "home cooking"])
def test_local_monthly_cap_blocks_paid_calls_but_allows_free_calls(local_meter_env, domain):
    from app.core.local_secrets import update_settings
    from app.credits import metered

    _, local_meter, _ = local_meter_env
    update_settings("spend", {"monthly_spend_cap": "0.0005"}, {})
    assert metered.call_sync(lambda: domain, purpose="task.draft", estimate_micro=500) == domain
    called = []
    with pytest.raises(local_meter.LocalSpendLimit, match="Your monthly.*Keys and connections.*raise") as failure:
        metered.call_sync(lambda: called.append(True), purpose="task.draft", estimate_micro=1)
    assert called == []
    assert "credit" not in str(failure.value).lower()
    assert "\u2014" not in str(failure.value)
    assert metered.call_sync(lambda: domain, purpose="task.free", estimate_micro=0) == domain
    assert local_meter.snapshot()["month_cost_usd_micro"] == 500


@pytest.mark.parametrize("domain", ["industrial procurement", "home cooking"])
def test_saved_settings_cap_controls_admission(local_meter_env, monkeypatch, domain):
    from app.core.local_secrets import update_settings
    from app.credits import metered

    _, local_meter, _ = local_meter_env
    monkeypatch.delenv("SUPYMEM_LOCAL_MONTHLY_CAP_USD", raising=False)
    update_settings("smtp", {"sender_name": domain}, {})
    update_settings("spend", {"monthly_spend_cap": "0"}, {})
    called = []
    with pytest.raises(local_meter.LocalSpendLimit):
        metered.call_sync(lambda: called.append(domain), purpose="task.draft", estimate_micro=1)
    assert called == []
    update_settings("spend", {"monthly_spend_cap": "0.000002"}, {})
    assert metered.call_sync(lambda: domain, purpose="task.draft", estimate_micro=2) == domain
    monkeypatch.setenv("SUPYMEM_LOCAL_MONTHLY_CAP_USD", "0")
    assert local_meter.monthly_cap_usd_micro() == 2
    monkeypatch.delenv("SUPYMEM_LOCAL_MONTHLY_CAP_USD")
    update_settings("spend", {"monthly_spend_cap": None}, {})
    assert local_meter.monthly_cap_usd_micro() is None


@pytest.mark.parametrize("domain", ["Fleet maintenance", "Community gardening"])
def test_local_summary_groups_features_and_scopes_tasks(local_meter_env, domain):
    from app.core.local_secrets import update_settings
    from app.credits import metered

    _, local_meter, _ = local_meter_env
    update_settings("spend", {"monthly_spend_cap": "5"}, {})
    for purpose, cost in (("chat.reply", 100), ("research.search", 200), ("content.writer", 300)):
        metered.call_sync(lambda: domain, purpose=purpose, workspace_id="workspace", venture_id=domain,
                          estimate_micro=cost)
    metered.call_sync(lambda: "other", purpose="chat.reply", workspace_id="other", estimate_micro=400)
    report = local_meter.snapshot(workspace_id="workspace", venture_id=domain)
    assert report["month_cost_usd_micro"] == 600
    assert report["monthly_cap_usd_micro"] == 5_000_000
    assert report["by_feature_usd_micro"] == {"chat": 100, "research": 200, "content": 300}
    assert local_meter.snapshot()["month_cost_usd_micro"] == 1000
    assert all(task["venture_id"] == domain for task in report["tasks"])
    assert not any("credit" in key for key in report)


@pytest.mark.parametrize("domain", ["Fleet maintenance", "Community gardening"])
def test_local_cap_accepts_saved_numeric_values_and_checks_without_spending(local_meter_env, monkeypatch, domain):
    from app.credits import metered

    _, local_meter, _ = local_meter_env
    set_local_cap(0.01)
    monkeypatch.setenv("SUPYMEM_LOCAL_MONTHLY_CAP_USD", "0")
    assert local_meter.monthly_cap_usd_micro() == 10_000
    metered.call_sync(lambda: domain, purpose="research.search", estimate_micro=9_999)
    local_meter.check_limit(1)
    with pytest.raises(local_meter.LocalSpendLimit, match="Keys and connections"):
        local_meter.check_limit(2)
    local_meter.check_limit(0)
    for invalid in (-1, True, 1.5):
        with pytest.raises(ValueError, match="non-negative integer"):
            local_meter.check_limit(invalid)
    assert local_meter.snapshot()["month_cost_usd_micro"] == 9_999


@pytest.mark.parametrize("domain", ["Fleet maintenance", "Community gardening"])
def test_local_run_cost_includes_lifetime_pending_calls_and_exact_scope(local_meter_env, monkeypatch, domain):
    from datetime import datetime, timezone
    from app.credits import metered

    _, local_meter, _ = local_meter_env
    monkeypatch.setattr(local_meter, "utcnow", lambda: datetime(2026, 10, 31, tzinfo=timezone.utc))
    metered.call_sync(lambda: domain, purpose="experiment.research", estimate_micro=100,
                      workspace_id="workspace", venture_id=domain, request_id="run:part:step")
    monkeypatch.setattr(local_meter, "utcnow", lambda: datetime(2026, 11, 1, tzinfo=timezone.utc))

    def timeout():
        raise TimeoutError()

    with pytest.raises(TimeoutError):
        metered.call_sync(timeout, purpose="experiment.research", estimate_micro=200,
                          workspace_id="workspace", venture_id=domain, request_id="run:part")
    for workspace, venture, request_id in (("other", domain, "run:part:step"),
                                          ("workspace", "other", "run:part:step"),
                                          ("workspace", domain, "run:partial:step")):
        metered.call_sync(lambda: domain, purpose="experiment.research", estimate_micro=400,
                          workspace_id=workspace, venture_id=venture, request_id=request_id)
    assert local_meter.run_cost("workspace", domain, {"run:part"}) == 300
    assert local_meter.run_cost("workspace", domain, set()) == 0
    report = local_meter.snapshot(workspace_id="workspace", venture_id=domain, run_id="run:part")
    assert report["month_cost_usd_micro"] == 200
    assert report["tasks"][0]["unconfirmed_calls"] == 1


@pytest.mark.parametrize("domain", ["Fleet maintenance", "Community gardening"])
@pytest.mark.parametrize("rejected", [False, True])
def test_local_legacy_model_journal_keeps_canonical_cost_and_diagnostics(local_meter_env, monkeypatch, domain, rejected):
    import json
    from types import SimpleNamespace
    from app.core.jobs import FatalError
    from app.core.operation_receipts import OperationJournal
    from app.credits import estimate, gate
    from app.credits.errors import CreditsUnavailable
    from app.research import metered

    row = SimpleNamespace(workspace_id="workspace", venture_id=domain)
    document = {"_runtime": {"operations": {}}}
    calls, rejections = [], []

    def mutate(identifier, payload, update):
        return update(row, document, None)

    journal = OperationJournal(read=lambda identifier: row, document=lambda identifier: document,
        mutate=mutate, before_call=lambda *args: None, prefix="content")
    monkeypatch.setattr(estimate, "actual_llm_micro", lambda *args, **kwargs: 1000)
    monkeypatch.setattr(metered, "_record_rejected", lambda purpose, error, *args: rejections.append(error))

    def provider():
        calls.append(domain)
        return SimpleNamespace(usage={"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
            choices=[SimpleNamespace(message=SimpleNamespace(content="invalid" if rejected else json.dumps({"domain": domain})),
                                     finish_reason="stop")])

    def invoke():
        return metered.perform("run", "draft", {"_job_attempt": 1, "_worker_id": "worker"},
            purpose="content.draft", provider="synthetic", estimate=30_000, model="unlisted-model",
            byok=True, callback=provider, parse=json.loads, journal=journal, prefix="content")

    for attempt in range(2):
        if rejected:
            with pytest.raises(FatalError, match="content_invalid_output"):
                invoke()
        else:
            assert invoke() == {"domain": domain}
    usage = document["_runtime"]["operations"]["draft"]["usage"]
    assert usage["local_call_id"] and usage["provider_actual_micro"] == 1000
    assert usage["credits_micro"] == 0 and gate.reconcile_receipt(usage) == 0
    assert bool(usage.get("output")) is rejected
    assert bool(rejections) is rejected
    assert calls == [domain]
    with pytest.raises(CreditsUnavailable):
        gate.reconcile_receipt(usage | {"actual_micro": 999, "provider_actual_micro": 999})


@pytest.mark.parametrize("domain", ["Fleet maintenance", "Community gardening"])
@pytest.mark.parametrize("provider_name", ["builtin", "duckduckgo", "firecrawl"])
@pytest.mark.asyncio
async def test_local_free_search_is_metered_and_allowed_at_zero_cap(local_meter_env, domain, provider_name):
    from types import SimpleNamespace
    from app.core.local_secrets import update_settings
    from app.credits import metered

    _, local_meter, _ = local_meter_env
    update_settings("spend", {"monthly_spend_cap": "0"}, {})
    update_settings("search", {"firecrawl_url": "http://127.0.0.1:3210"}, {})
    calls = []
    result = {"usage": {"firecrawl_self_hosted": True}} if provider_name == "firecrawl" else domain

    async def search(query):
        calls.append(query)
        return result

    provider = SimpleNamespace(name=provider_name, search=search)
    assert await metered.search(provider, domain, workspace_id="workspace", venture_id=domain) == result
    assert calls == [domain]
    assert metered.price_micro(provider_name + "_search") == 0
    report = local_meter.snapshot()
    assert report["month_cost_usd_micro"] == 0 and len(report["tasks"]) == 1


@pytest.mark.parametrize("domain", ["Fleet maintenance", "Community gardening"])
def test_local_smtp_has_no_platform_fee_or_wallet_gate(local_meter_env, domain):
    from app.core.local_secrets import update_settings
    from app.credits import metered

    _, local_meter, _ = local_meter_env
    update_settings("spend", {"monthly_spend_cap": "0"}, {})
    cost = metered.price_micro("email_send", 3)
    assert cost == 0
    assert metered.call_sync(lambda: domain, purpose="outreach.send", provider="smtp", estimate_micro=cost) == domain
    assert local_meter.snapshot()["month_cost_usd_micro"] == 0


@pytest.mark.parametrize("domain", ["Fleet maintenance", "Community gardening"])
def test_local_account_bootstrap_and_snapshots_never_create_wallets(local_meter_env, monkeypatch, domain):
    from app.credits import accounts

    def no_wallet():
        pytest.fail("local account bootstrap must not open the wallet database")

    monkeypatch.setattr(accounts, "session_factory", no_wallet)
    assert accounts.ensure_account(domain, workspace_id=domain) is None
    assert accounts.account_for_venture(domain) is None
    assert accounts.account_for_workspace(domain) is None
    assert accounts.wallet_for(domain) is None
    assert accounts.founder_snapshot(domain) is None
    assert accounts.founder_snapshot_for_workspace(domain) is None


@pytest.mark.parametrize("domain", ["Fleet maintenance", "Community gardening"])
def test_local_build_quote_needs_no_wallet(local_meter_env, monkeypatch, domain):
    from types import SimpleNamespace
    from app.builder import execution

    def no_wallet(*args):
        pytest.fail("local build quotes must not resolve a wallet")

    monkeypatch.setattr(execution, "_account_id", no_wallet)
    monkeypatch.setattr(execution, "local_tool", lambda *args: "codex")
    monkeypatch.setattr(execution.budgets, "runtime_quote", lambda *args, **kwargs: {
        "ready": True, "reasons": [], "ceiling_micro": 300, "model_ceiling_micro": 100,
        "compute_ceiling_micro": 100, "artifact_ceiling_micro": 100, "model": "test", "runtime_policy": {}})
    saved = []
    session = SimpleNamespace(add=saved.append, commit=lambda: None)
    app = SimpleNamespace(id="app", workspace_id="workspace", venture_id=domain)
    quote = execution._quote(session, app, "owner", {"milestone": {"kind": "product"}})
    assert quote["ready"] is True
    assert "build_credit_account_missing" not in quote["reasons"]
    assert "available_micro" not in quote
    assert quote["mode"] == "connected_tool"
    assert not any("micro" in field for field in quote)
    assert saved


@pytest.mark.parametrize("domain", ["Fleet maintenance", "Community gardening"])
@pytest.mark.parametrize("capped", [False, True])
def test_local_build_start_checks_cap_without_account_or_hold(local_meter_env, monkeypatch, domain, capped):
    import json
    from contextlib import contextmanager
    from types import SimpleNamespace
    from app.builder import execution
    from app.build import device_orders
    from app.core import jobs
    from app.core.local_secrets import update_settings
    from app.db.supymem_models import BuildRun, FounderRequest

    _, local_meter, _ = local_meter_env
    update_settings("spend", {"monthly_spend_cap": "0" if capped else None}, {})
    inputs = {"plan_id": "plan", "plan_task_id": "task", "milestone": {"kind": "core"}, "source": {"kind": "empty"}}
    input_digest = execution.digest({"inputs": inputs, "mode": "connected_tool"})
    quote = SimpleNamespace(source="build_quote", related_id="app", workspace_id="workspace", venture_id=domain,
        outcome="pending", created_at=execution.naive_utc(), text=json.dumps({"actor": "owner",
        "quote": {"input_digest": input_digest, "ready": True, "reasons": []}, "inputs": inputs, "source_artifact_id": None}))
    lookups = iter([None, None])
    saved, queued = [], []
    session = SimpleNamespace(scalar=lambda *args: next(lookups),
                              get=lambda model, *args, **kwargs: quote if model is FounderRequest else saved[0] if model is BuildRun else None,
                              add=saved.append, commit=lambda: None)

    @contextmanager
    def transaction():
        yield session

    monkeypatch.setattr(execution, "db", lambda: transaction)
    monkeypatch.setattr(execution, "_app", lambda *args, **kwargs: SimpleNamespace(id="app", workspace_id="workspace", venture_id=domain))
    monkeypatch.setattr(execution, "_inputs", lambda *args, **kwargs: inputs)
    monkeypatch.setattr(execution, "local_tool", lambda *args: "codex")
    monkeypatch.setattr(execution, "serialize_writes", lambda *args, **kwargs: None)

    def no_wallet(*args, **kwargs):
        pytest.fail("local build starts must not resolve a wallet or reserve platform funds")

    monkeypatch.setattr(execution, "_account_id", no_wallet)
    monkeypatch.setattr(execution.pools, "create_pool", no_wallet)
    monkeypatch.setattr(execution.pools, "lock_account", no_wallet)
    monkeypatch.setattr(execution.budgets, "runtime_quote", no_wallet)
    monkeypatch.setattr(jobs, "enqueue", no_wallet)
    def tool_order(payload):
        queued.append(payload)
        spec = json.loads(saved[0].spec)
        spec["device_work_order_id"] = "order"
        saved[0].spec = json.dumps(spec)
    monkeypatch.setattr(execution, "run_local", tool_order)
    monkeypatch.setattr(device_orders, "retrieve", lambda *args: {"id": "order", "state": "approved", "review_path": "/builds"})
    options = {"actor": "owner", "quote_id": "quote", "input_digest": input_digest, "idempotency_key": "request"}
    result = execution.start("app", **options)
    assert result == {"build_run_id": saved[0].id, "device_work_order_id": "order", "state": "approved", "review_path": "/builds"}
    assert saved[0].reservation_id is None and saved[0].runtime_version == 0
    assert quote.outcome == "consumed" and len(queued) == 1


@pytest.mark.parametrize("domain", ["Fleet maintenance", "Community gardening"])
@pytest.mark.parametrize("started", [False, True])
def test_local_build_cancel_keeps_inflight_cleanup_without_wallet(local_meter_env, monkeypatch, domain, started):
    import json
    from contextlib import contextmanager
    from types import SimpleNamespace
    from app.builder import execution
    from app.db.models import Job
    from app.db.supymem_models import BuildRun

    run = BuildRun(id="build", app_id="app", workspace_id="workspace", venture_id=domain,
        runtime_version=1, state="running" if started else "queued", job_id="job", event_seq=0,
        spec=json.dumps({"cleanup_state": "running" if started else "not_started"}))
    job = SimpleNamespace(status="leased" if started else "scheduled", attempts=1 if started else 0)
    session = SimpleNamespace(get=lambda model, *args, **kwargs: run if model is BuildRun else job if model is Job else None,
                              add=lambda *args: None, commit=lambda: None)

    @contextmanager
    def transaction():
        yield session

    monkeypatch.setattr(execution, "db", lambda: transaction)
    monkeypatch.setattr(execution, "_app", lambda *args, **kwargs: None)
    monkeypatch.setattr(execution, "serialize_writes", lambda *args, **kwargs: None)

    def no_wallet(*args):
        pytest.fail("local cancellation must not lock a platform reservation")

    monkeypatch.setattr(execution.pools, "lock_pool", no_wallet)
    result = execution.cancel(run.id, actor="owner")
    assert result == {"state": "running" if started else "cancelled", "cleanup_state": "pending" if started else "complete"}
    assert execution.cancel(run.id, actor="owner") == result
    assert (run.closed_at is None) is started
    assert job.status == ("leased" if started else "cancelled")


@pytest.mark.parametrize("domain", ["Fleet maintenance", "Community gardening"])
def test_local_build_read_returns_only_its_metered_spend(local_meter_env, monkeypatch, domain):
    import json
    from types import SimpleNamespace
    from app.builder import execution
    from app.credits import metered
    from app.db.supymem_models import BuildRun
    from app.planning import publication_evidence
    from app.build import subscription_static_completion

    run = BuildRun(id="build", app_id="app", workspace_id="workspace", venture_id=domain,
                   runtime_version=1, state="running", reservation_id="old_wallet_hold", credits_used=900,
                   spec=json.dumps({"runtime": {"runtime_policy": {}}}))
    monkeypatch.setattr(publication_evidence, "landing_build_result", lambda *args: None)
    monkeypatch.setattr(subscription_static_completion, "publication_required", lambda *args: False)
    metered.call_sync(lambda: domain, purpose="build.model", estimate_micro=125,
                      workspace_id="workspace", venture_id=domain, request_id="build:step")
    metered.call_sync(lambda: domain, purpose="build.model", estimate_micro=250,
                      workspace_id="workspace", venture_id=domain, request_id="other:step")

    def no_wallet(*args):
        pytest.fail("local build reads must not load wallet reservations")

    report = execution._serialize(SimpleNamespace(get=no_wallet), run)
    assert report["usage"]["month_cost_usd_micro"] == 125
    assert report["usage"]["by_feature_usd_micro"] == {"builds": 125}
    assert not any("credit" in key or key in {"consumed_micro", "reserved_micro"} for key in report["usage"])


@pytest.mark.parametrize("domain", ["industrial procurement", "home cooking"])
@pytest.mark.parametrize("protected", [False, True])
def test_local_unknown_outcomes_count_until_confirmed_unspent(local_meter_env, domain, monkeypatch, protected):
    from app.core.local_secrets import update_settings
    gate, local_meter, _ = local_meter_env
    set_local_cap("0.0005")
    with pytest.raises(TimeoutError), gate.charge_sync(purpose="task.draft", estimate_micro=500, venture_id=domain) as meter:
        if protected:
            meter.protect_outcome()
        raise TimeoutError("provider outcome unknown")
    assert local_meter.snapshot()["month_cost_usd_micro"] == 500
    with pytest.raises(local_meter.LocalSpendLimit), gate.charge_sync(purpose="task.draft", estimate_micro=1):
        pytest.fail("an unknown paid outcome must not bypass the cap")
    set_local_cap("0.001")
    with gate.charge_sync(purpose="task.draft", estimate_micro=500) as meter:
        meter.protect_outcome()
        meter.confirm_unspent()
    assert local_meter.snapshot()["month_cost_usd_micro"] == 500


@pytest.mark.parametrize("cap", ["-1", "NaN", "Infinity", "not-a-number"])
@pytest.mark.parametrize("domain", ["industrial procurement", "home cooking"])
def test_local_invalid_cap_fails_before_provider_call(local_meter_env, monkeypatch, cap, domain):
    from app.core.local_secrets import update_settings
    from app.credits import metered
    from app.credits.errors import CreditsUnavailable

    set_local_cap(cap)
    called = []
    with pytest.raises(CreditsUnavailable, match="Keys and connections.*Monthly limit"):
        metered.call_sync(lambda: called.append(True), purpose="task.draft", venture_id=domain, estimate_micro=1)
    assert called == []


@pytest.mark.parametrize("domain", ["industrial procurement", "home cooking"])
@pytest.mark.parametrize("hook_failure", [False, True])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_local_frozen_receipt_survives_hooks_and_prevents_redispatch(
    local_meter_env, domain, hook_failure, asynchronous,
):
    from app.credits import metered
    from app.credits.errors import CreditsUnavailable

    from tests.unit.credits.test_core_model_receipts import DIGEST, binding, sdk

    gate, local_meter, _ = local_meter_env
    quote = binding()
    calls, saved = [], []

    def provider():
        calls.append(True)
        return sdk()

    async def async_provider():
        return provider()

    def checkpoint(result, usage):
        if hook_failure:
            raise RuntimeError("saved result hook failed")

    options = {"workspace_id": domain, "venture_id": domain, "purpose": quote["purpose"],
        "provider": quote["provider"], "model": quote["deployment"], "estimate_micro": 1200,
        "max_tokens": 200, "request_id": "original-request", "model_binding": quote,
        "request_digest": DIGEST, "before_model_receipt": checkpoint,
        "before_meter_exit": lambda result, receipt: saved.append(receipt)}

    async def invoke():
        if asynchronous:
            return await metered.call(async_provider, **options)
        return metered.call_sync(provider, **options)

    if hook_failure:
        with pytest.raises(RuntimeError, match="saved result hook failed"):
            await invoke()
        assert saved == []
    else:
        assert await invoke()
        assert gate.reconcile_receipt(saved[0]) == 0
        assert gate.reconcile_receipt(saved[0]) == 0
        with pytest.raises(CreditsUnavailable):
            gate.reconcile_receipt(saved[0] | {"provider_actual_micro": 0})
    with pytest.raises(CreditsUnavailable, match="already started"):
        await invoke()
    key = metered.model_call_key(workspace_id=domain, venture_id=domain,
        purpose=quote["purpose"], request_id=options["request_id"])
    expected = {"credits_micro": 0, "output_usable": True, "outcome": "confirmed"}
    assert metered.recover_model_call(key) == expected
    assert metered.recover_model_call(key) == expected
    assert calls == [True]
    task = local_meter.snapshot()["tasks"][0]
    assert (task["prompt_tokens"], task["completion_tokens"], task["cost_usd_micro"]) == (100, 20, 248)


@pytest.mark.parametrize("domain", ["industrial procurement", "home cooking"])
def test_local_cap_serializes_calls_and_resets_by_calendar_month(local_meter_env, domain, monkeypatch):
    from app.core.local_secrets import update_settings
    from concurrent.futures import ThreadPoolExecutor
    from datetime import datetime, timezone
    from threading import Barrier

    gate, local_meter, _ = local_meter_env
    set_local_cap("0.001")
    monkeypatch.setattr(local_meter, "utcnow", lambda: datetime(2026, 10, 31, tzinfo=timezone.utc))
    barrier = Barrier(2)

    def invoke():
        barrier.wait(timeout=10)
        try:
            with gate.charge_sync(purpose="task.draft", venture_id=domain, estimate_micro=700) as meter:
                meter.record_micro(700)
            return True
        except local_meter.LocalSpendLimit:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: invoke(), range(2)))
    assert sorted(outcomes) == [False, True]
    assert local_meter.snapshot()["month_cost_usd_micro"] == 700
    monkeypatch.setattr(local_meter, "utcnow", lambda: datetime(2026, 11, 1, tzinfo=timezone.utc))
    assert local_meter.snapshot()["month_cost_usd_micro"] == 0
    with gate.charge_sync(purpose="task.draft", venture_id=domain, estimate_micro=1000) as meter:
        meter.record_micro(1000)
    assert local_meter.snapshot()["month_cost_usd_micro"] == 1000


@pytest.mark.parametrize("domain", ["industrial procurement", "home cooking"])
def test_local_calls_keep_task_budget_admission_and_raw_usage(local_meter_env, domain, monkeypatch):
    from app.core.local_secrets import update_settings
    gate, local_meter, _ = local_meter_env
    calls = []

    def task_usage(method, *args, **kwargs):
        calls.append((method, args, kwargs))
        return "task-invocation" if method == "before_call" else None

    monkeypatch.setattr(gate, "_task_usage", task_usage)
    with gate.charge_sync(purpose="task.draft", venture_id=domain, estimate_micro=500, max_tokens=200) as meter:
        meter.record(prompt_tokens=10, completion_tokens=20)
        meter.record_micro(7, provider_actual_micro=10)
    assert calls == [("before_call", (), {"estimate_micro": 500, "max_tokens": 200}),
        ("after_call", ("task-invocation",), {"actual_micro": 10, "tokens_used": 30})]
    assert local_meter.snapshot()["month_cost_usd_micro"] == 10
    set_local_cap("0")
    with pytest.raises(local_meter.LocalSpendLimit), gate.charge_sync(purpose="task.draft", estimate_micro=1):
        pytest.fail("you must not start a paid call after reaching your cap")
    assert calls[-1] == ("abort_call", ("task-invocation",), {"definitive": True})

    def refuse(method, *args, **kwargs):
        raise RuntimeError("task admission refused")

    monkeypatch.setattr(gate, "_task_usage", refuse)
    with pytest.raises(RuntimeError, match="task admission refused"), gate.charge_sync(purpose="task.draft", estimate_micro=1):
        pytest.fail("local mode must not bypass task admission")


@pytest.mark.parametrize("domain", ["industrial procurement", "home cooking"])
@pytest.mark.asyncio
async def test_local_llm_uses_your_provider_config_without_hosted_resolution(local_meter_env, domain, monkeypatch):
    from types import SimpleNamespace

    from app.credits import estimate, metered
    from app.llm import founder_provider
    from llm_gateway import provider_foundry, sole

    _, local_meter, _ = local_meter_env
    config = provider_foundry.ProviderConfig(deployment_pro="test-model", deployment_flash="test-model",
                                           source="founder_override")
    monkeypatch.setattr(estimate, "pricing_table", lambda: {"test-model": {
        "input_per_mtok": "2", "output_per_mtok": "10", "cached_input_per_mtok": "0.1"}})

    def no_hosted_config(*args, **kwargs):
        pytest.fail("local calls must use your own provider configuration")

    monkeypatch.setattr(founder_provider, "register_workspace_llm_resolver", no_hosted_config)
    monkeypatch.setattr(provider_foundry, "load_provider_config_from_env", no_hosted_config)
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda workspace: SimpleNamespace(source="founder_override"))
    monkeypatch.setattr(founder_provider, "to_provider_config", lambda workspace, **kwargs: config)
    calls = []

    async def transmit(messages, **kwargs):
        assert kwargs["config"] is config and kwargs["single_attempt"] is True
        assert config.source == "founder_override"
        calls.append(messages)
        return SimpleNamespace(usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30,
            "prompt_tokens_details": {"cached_tokens": 8}})

    monkeypatch.setattr(sole, "transmit", transmit)
    await metered.llm([{"role": "user", "content": domain}], purpose="task.draft", workspace_id=domain)
    assert len(calls) == 1
    assert local_meter.snapshot()["month_cost_usd_micro"] == 205


@pytest.mark.parametrize("domain", ["industrial procurement", "home cooking"])
@pytest.mark.parametrize("changed_scope", ["workspace_id", "venture_id"])
def test_local_calls_refuse_cross_tenant_scope(local_meter_env, domain, changed_scope):
    from app.credits.errors import CreditsUnavailable
    from app.tenancy import bind

    gate, local_meter, _ = local_meter_env
    target = {"workspace_id": domain, "venture_id": domain}
    target[changed_scope] = "unrelated-scope"
    with (
        bind(workspace_id=domain, venture_id=domain),
        pytest.raises(CreditsUnavailable, match="Open the workspace"),
        gate.charge_sync(purpose="task.draft", estimate_micro=1, **target),
    ):
        pytest.fail("local mode must not cross tenant boundaries")
    assert local_meter.snapshot()["tasks"] == []


@pytest.mark.parametrize("domain", ["industrial procurement", "home cooking"])
def test_local_calls_preserve_bounded_admission_and_cleanup(local_meter_env, domain, monkeypatch):
    from app.core.local_secrets import update_settings
    gate, local_meter, _ = local_meter_env
    admitted, settled = [], []

    def admit(account_id, request_id, estimate):
        assert account_id is None and request_id
        admitted.append(estimate)
        return settled.append

    with gate.bounded_calls(domain, domain, admit):
        with gate.charge_sync(purpose="task.draft", workspace_id=domain, venture_id=domain, estimate_micro=500) as meter:
            meter.record_micro(100)
        assert admitted == [500] and settled == [meter]
        set_local_cap("0")
        with pytest.raises(local_meter.LocalSpendLimit), gate.charge_sync(
            purpose="task.draft", workspace_id=domain, venture_id=domain, estimate_micro=1,
        ):
            pytest.fail("a monthly refusal must not start a call")
        assert settled == [meter, None]

    def refuse(*args):
        raise PermissionError("your task limit refused admission")

    with (
        gate.bounded_calls(domain, domain, refuse),
        pytest.raises(PermissionError, match="task limit refused"),
        gate.charge_sync(purpose="task.draft", workspace_id=domain, venture_id=domain, estimate_micro=1),
    ):
        pytest.fail("local mode must not bypass your task's consent limit")
    assert local_meter.snapshot()["month_cost_usd_micro"] == 100


@pytest.mark.parametrize("domain", ["industrial procurement", "home cooking"])
@pytest.mark.parametrize("rejected", [False, True])
def test_local_frozen_failure_recovery_keeps_task_outcome(local_meter_env, domain, rejected, monkeypatch):
    import httpx
    from app.credits import metered
    from app.credits.errors import CreditsUnavailable

    from tests.unit.credits.test_core_model_receipts import DIGEST, binding

    gate, local_meter, _ = local_meter_env
    task_calls = []

    def task_usage(method, *args, **kwargs):
        task_calls.append((method, args, kwargs))
        return "task-invocation" if method == "before_call" else None

    monkeypatch.setattr(gate, "_task_usage", task_usage)
    failure = httpx.HTTPStatusError("provider rejected", request=httpx.Request("POST", "https://provider.test"),
        response=httpx.Response(401)) if rejected else TimeoutError("provider outcome unknown")

    def provider():
        raise failure

    quote = binding()
    with pytest.raises(type(failure)):
        metered.call_sync(provider, purpose=quote["purpose"], workspace_id=domain, venture_id=domain,
            provider=quote["provider"], model=quote["deployment"], estimate_micro=1200, max_tokens=200,
            request_id="original-request", model_binding=quote, request_digest=DIGEST,
            before_model_receipt=lambda *args: None)
    key = metered.model_call_key(workspace_id=domain, venture_id=domain,
        purpose=quote["purpose"], request_id="original-request")
    if rejected:
        assert metered.recover_model_call(key) == {"credits_micro": 0, "output_usable": False, "outcome": "rejected"}
        assert task_calls[-1] == ("abort_call", ("task-invocation",), {"definitive": True})
    else:
        with pytest.raises(CreditsUnavailable, match="Check your provider"):
            metered.recover_model_call(key)
        assert task_calls[-1] == ("abort_call", ("task-invocation",), {"definitive": False})
    assert local_meter.snapshot()["month_cost_usd_micro"] == (0 if rejected else 1200)

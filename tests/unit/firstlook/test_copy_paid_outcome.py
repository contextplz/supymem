"""Synthetic financial lifecycle; real persisted Firstlook admission and capture.

These provider-free cases do not qualify the SDK or real wallet settlement.
"""
from __future__ import annotations

from contextlib import contextmanager

import asyncio
import json
from types import SimpleNamespace

import pytest

from tests.unit.firstlook.test_generation_admission import admitted as admitted
from tests.company_os_harness import load_company_os_app


@pytest.fixture
def legacy_model(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("COS_FIRSTLOOK_CAPTURE_ENABLED", raising=False)
    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setenv("LLM_CASSETTE_MODE", "off")
    monkeypatch.setenv("FIRSTLOOK_COPY_MODE", "live")
    load_company_os_app()
    from app.firstlook import copy
    from app.credits import accounts, gate
    from app.chat import assistant
    from app.llm import founder_provider
    from app import state
    from llm_gateway import sole, provider_foundry

    account = SimpleNamespace(id="account", scope="person", owner_person_id="payer", workspace_id=None)
    # Synthetic authorization dependency; real wallet membership is tested on DBs.
    monkeypatch.setattr(accounts, "account_can_spend", lambda session, row, wid, vid:
                        row.scope == "person" and row.owner_person_id == "payer" and wid == "workspace" and vid == "venture")
    monkeypatch.setattr(accounts, "get_account", lambda _: account)
    monkeypatch.setattr(accounts, "account_for_workspace", lambda _: account)
    monkeypatch.setattr(founder_provider, "register_workspace_llm_resolver", lambda: None)
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda _: SimpleNamespace(
        source="platform_default", model="DeepSeek-V4-Pro", provider="azure_foundry"))
    monkeypatch.setattr(provider_foundry, "load_provider_config_from_env", lambda: provider_foundry.ProviderConfig(
        deployment_pro="Different-Pro-Model", deployment_flash="DeepSeek-V4-Pro"))
    monkeypatch.setattr(assistant, "_rejection_release_confirmed", lambda *a, **k: True)
    pending = []

    @contextmanager
    def session():
        yield SimpleNamespace(scalar=lambda _: pending[0] if pending else None)
    monkeypatch.setattr(state, "db", lambda: session)
    meters, calls = [], []

    @contextmanager
    def charge(**kwargs):
        meter = SimpleNamespace(hold=SimpleNamespace(reservation_id="hold"), passthrough=False,
            outcome_protected=False, recorded=False, definitive_failure=False, settle_result=None,
            kwargs=kwargs)
        meter.protect_outcome = lambda: setattr(meter, "outcome_protected", True)
        meter.confirm_unspent = lambda: setattr(meter, "definitive_failure", True)
        def record(**counts):
            meter.recorded = True
            meter.counts = counts
        meter.record = record
        meters.append(meter)
        try:
            yield meter
        finally:
            if meter.recorded:
                meter.settle_result = SimpleNamespace(actual_micro=1)
    monkeypatch.setattr(gate, "charge_sync", charge)

    def run(*, error=None, usage=None, body="not JSON"):
        async def transport(messages, **kwargs):
            assert meters[-1].outcome_protected is True
            calls.append(kwargs)
            if error is not None:
                raise error
            return SimpleNamespace(usage=usage, choices=[SimpleNamespace(
                message=SimpleNamespace(content=body))])
        monkeypatch.setattr(sole, "transmit", transport)
        return asyncio.run(copy.generate_copy("A bakery order board", workspace_id="workspace", venture_id="venture"))
    return copy, run, meters, calls, pending


@pytest.mark.parametrize("usage", [None, {}, {"prompt_tokens": 3},
    {"prompt_tokens": True, "completion_tokens": 1, "total_tokens": 2},
    {"prompt_tokens": -1, "completion_tokens": 1, "total_tokens": 0},
    {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 5}])
def test_default_off_unknown_usage_has_one_protected_attempt_and_no_plain_success(legacy_model, usage):
    from llm_gateway.errors import ProviderCallError
    _, run, meters, calls, _ = legacy_model
    with pytest.raises(ProviderCallError, match="usage is unconfirmed"):
        run(usage=usage)
    assert len(calls) == 1 and calls[0]["single_attempt"] is True
    assert calls[0]["workspace_id"] == "workspace" and calls[0]["max_tokens"] == 900
    assert meters[0].recorded is False and meters[0].definitive_failure is False


@pytest.mark.parametrize("error", [TimeoutError("Synthetic timeout"), ConnectionError("Synthetic connection"),
    RuntimeError("Synthetic provider failure")])
def test_default_off_transport_unknown_is_not_fallback_or_second_attempt(legacy_model, error):
    from llm_gateway.errors import ProviderCallError
    with pytest.raises(ProviderCallError) as failed:
        legacy_model[1](error=error)
    assert failed.value.code == "provider_outcome_unknown"
    assert len(legacy_model[3]) == 1 and legacy_model[2][0].recorded is False


def test_default_off_cancellation_keeps_protection_and_never_becomes_copy(legacy_model):
    with pytest.raises(asyncio.CancelledError):
        legacy_model[1](error=asyncio.CancelledError())
    assert len(legacy_model[3]) == 1 and legacy_model[2][0].outcome_protected is True
    assert legacy_model[2][0].recorded is False


def test_default_off_valid_zero_usage_is_known_paid_unusable_output(legacy_model):
    result = legacy_model[1](usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
    assert result[1:] == (0.0, True)
    assert legacy_model[2][0].counts == {"prompt_tokens": 0, "completion_tokens": 0}
    assert len(legacy_model[3]) == 1
    call = legacy_model[3][0]
    assert call["tier"] == "pro" and call["use_workspace_override"] is False
    assert call["config"].deployment_pro == "DeepSeek-V4-Pro"
    assert call["config"].source == "platform_default"
    assert legacy_model[2][0].kwargs["model"] == call["config"].deployment_pro


@pytest.mark.parametrize("sol", [False, True])
def test_default_off_captured_copy_cap_matches_its_credit_quote(legacy_model, monkeypatch, sol):
    from app.credits import estimate
    from llm_gateway import provider_foundry

    deployment = "supymem-gpt-6-1-sol" if sol else "DeepSeek-V4-Pro"
    monkeypatch.setattr(provider_foundry, "load_provider_config_from_env", lambda:
        provider_foundry.ProviderConfig(deployment_pro=deployment, deployment_flash=deployment))
    quoted = []
    monkeypatch.setattr(estimate, "estimate_llm_micro", lambda legacy_model, **kwargs:
        quoted.append(kwargs["max_tokens"]) or 1000)
    legacy_model[1](usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
    assert quoted == [2048 if sol else 900]
    assert legacy_model[3][0]["max_tokens"] == quoted[0]
    assert (legacy_model[3][0]["config"].api_mode == "responses") is sol


def test_default_off_complete_usage_and_honest_model_copy_preserve_normal_success(legacy_model):
    from tests.integration.test_postgres_firstlook_publication import COPY
    result = legacy_model[1](usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        body=json.dumps(COPY))
    assert result[2] is False and result[0]["headline"] == COPY["headline"]
    assert result[1] > 0 and len(legacy_model[3]) == 1
    assert legacy_model[2][0].recorded is True and legacy_model[2][0].settle_result is not None


def test_default_off_provider_503_is_held_not_a_known_rejection_or_free_draft(legacy_model):
    from llm_gateway.errors import ProviderCallError
    with pytest.raises(ProviderCallError):
        legacy_model[1](error=ProviderCallError("provider_down", "Synthetic failure", status=503, retryable=True))
    assert len(legacy_model[3]) == 1 and legacy_model[2][0].definitive_failure is False
    assert legacy_model[2][0].outcome_protected is True and legacy_model[2][0].recorded is False


def test_default_off_existing_unknown_blocks_fresh_transport_and_reservation(legacy_model):
    from llm_gateway.errors import ProviderCallError
    legacy_model[4].append("saved-hold")
    with pytest.raises(ProviderCallError):
        legacy_model[1]()
    assert legacy_model[2] == [] and legacy_model[3] == []


def test_default_off_missing_platform_credit_account_refuses_before_transport(legacy_model, monkeypatch):
    from app.credits import accounts
    from app.credits.errors import CreditsUnavailable
    monkeypatch.setattr(accounts, "get_account", lambda _: None)
    with pytest.raises(CreditsUnavailable):
        legacy_model[1]()
    assert legacy_model[2] == [] and legacy_model[3] == []


def test_default_off_wrong_founder_credit_wallet_refuses_before_transport(legacy_model, monkeypatch):
    from app.credits import accounts
    from app.credits.errors import CreditsUnavailable
    monkeypatch.setattr(accounts, "get_account", lambda _: SimpleNamespace(id="foreign", scope="person", owner_person_id="other", workspace_id=None))
    with pytest.raises(CreditsUnavailable):
        legacy_model[1]()
    assert legacy_model[2] == [] and legacy_model[3] == []


def test_default_off_missing_byok_capture_refuses_without_platform_dial(legacy_model, monkeypatch):
    from app.llm import founder_provider
    from app.credits.errors import CreditsUnavailable
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda _: SimpleNamespace(source="founder_override"))
    monkeypatch.setattr(founder_provider, "to_provider_config", lambda *a, **k: None)
    with pytest.raises(CreditsUnavailable):
        legacy_model[1]()
    assert legacy_model[2] == [] and legacy_model[3] == []


def test_default_off_byok_capture_and_payer_are_bound_without_an_ambient_resolver(legacy_model, monkeypatch):
    from app.llm import founder_provider
    from llm_gateway.provider_foundry import ProviderConfig
    captured = ProviderConfig(source="founder_override", deployment_pro="Reviewed-Byok-Model")
    resolved = SimpleNamespace(source="founder_override")
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda _: resolved)
    def capture(wid, *, resolved_config):
        assert wid == "workspace" and resolved_config is resolved
        return captured
    monkeypatch.setattr(founder_provider, "to_provider_config", capture)
    result = legacy_model[1](usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
    assert result[1:] == (0.0, True)
    assert legacy_model[3][0]["config"] is captured and legacy_model[3][0]["use_workspace_override"] is False
    assert legacy_model[2][0].kwargs["model"] == "Reviewed-Byok-Model"
    assert legacy_model[2][0].kwargs["payer"] == "founder"


@pytest.mark.parametrize("code,status", [("bad_api_key", 401), ("out_of_credits", 402), ("rate_limited", 429)])
def test_default_off_exact_rejection_is_released_but_never_a_fallback_draft(legacy_model, code, status):
    from llm_gateway.errors import ProviderCallError
    with pytest.raises(ProviderCallError) as error:
        legacy_model[1](error=ProviderCallError(code, "Synthetic rejection", status=status))
    assert error.value.unspent_confirmed is True
    assert len(legacy_model[3]) == 1 and legacy_model[2][0].definitive_failure is True


@pytest.mark.parametrize("kind", ["unavailable", "funds"])
def test_default_off_credit_rejection_on_context_entry_preserves_error_before_transport(legacy_model, monkeypatch, kind):
    from app.credits import gate
    from app.credits.errors import CreditsUnavailable, InsufficientCredits
    error = CreditsUnavailable("synthetic admission unavailable") if kind == "unavailable" else InsufficientCredits("account", 1, 2)
    @contextmanager
    def rejected(**kwargs):
        raise error
        yield
    monkeypatch.setattr(gate, "charge_sync", rejected)
    with pytest.raises(type(error)) as caught:
        legacy_model[1]()
    assert caught.value is error and legacy_model[2] == [] and legacy_model[3] == []


@pytest.mark.parametrize("rejected", [False, True])
def test_default_off_credit_failure_on_context_exit_closes_one_provider_attempt(legacy_model, monkeypatch, rejected):
    from app.credits import gate
    from app.credits.errors import CreditsUnavailable
    from llm_gateway.errors import ProviderCallError
    original = gate.charge_sync
    error = CreditsUnavailable("synthetic exit reconciliation unavailable")
    @contextmanager
    def exit_failure(**kwargs):
        with original(**kwargs) as meter:
            yield meter
        raise error
    monkeypatch.setattr(gate, "charge_sync", exit_failure)
    provider_error = ProviderCallError("rate_limited", "synthetic rejection", status=429) if rejected else None
    with pytest.raises(ProviderCallError) as caught:
        legacy_model[1](error=provider_error, usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30})
    assert caught.value.code == "provider_outcome_unknown" and caught.value.__cause__ is error
    assert caught.value.retryable is False and not getattr(caught.value, "unspent_confirmed", False)
    assert len(legacy_model[3]) == 1 and legacy_model[2][0].outcome_protected is True
    assert legacy_model[2][0].definitive_failure is rejected
    assert legacy_model[2][0].recorded is (not rejected)


def _usage(prompt=0, completion=0, *, cached=0, reasoning=0):
    return {"prompt_tokens": prompt, "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "prompt_tokens_details": {"cached_tokens": cached},
        "completion_tokens_details": {"reasoning_tokens": reasoning}}


COPY = {
    "headline": "We're building a calmer way for bakeries to take orders",
    "subhead": "Early idea for small bakery owners. Join the waitlist to hear first.",
    "points": [
        {"title": "The idea", "body": "A simple order board a bakery owner can review each morning."},
        {"title": "Who it is for", "body": "We want to start with one bakery and learn what actually helps."},
        {"title": "Status", "body": "Nothing here is final. We are talking to owners before writing code."},
    ],
    "cta": {"label": "Join the waitlist", "kind": "waitlist"},
    "audience": "bakery owners", "name_candidates": ["Order Board", "Crumb", "Loaf"],
    "assumptions": ["Bakery owners take orders by hand today."],
}


@pytest.fixture
def model(monkeypatch, admitted):
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setenv("LLM_CASSETTE_MODE", "off")
    monkeypatch.setenv("FIRSTLOOK_COPY_MODE", "live")
    monkeypatch.setenv("COS_FIRSTLOOK_CAPTURE_ENABLED", "1")
    monkeypatch.setenv("LLM_API_KEY", "synthetic-unit-key")
    monkeypatch.setenv("MODEL_ROUTING_OVERRIDES", "{}")
    from app.firstlook import copy
    from app.credits import accounts, gate, metered
    from app.credits.errors import CreditsUnavailable
    from app.intake import model_capture
    from app.llm import founder_provider
    from app.llm.provider_errors import definitive_provider_rejection
    from llm_gateway import sole, provider_foundry

    account = SimpleNamespace(id="account", scope="person", owner_person_id="person_firstlook", workspace_id=None)
    # Synthetic authorization dependency; real wallet membership is tested on DBs.
    monkeypatch.setattr(accounts, "account_can_spend", lambda session, row, wid, vid:
                        row.scope == "person" and row.owner_person_id == "person_firstlook"
                        and wid == "ws_firstlook" and vid == "ven_firstlook")
    monkeypatch.setattr(accounts, "get_account", lambda _: account)
    monkeypatch.setattr(accounts, "account_for_workspace", lambda _: account)
    monkeypatch.setattr(founder_provider, "register_workspace_llm_resolver", lambda: None)
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda _: SimpleNamespace(
        source="platform_default", model="gpt-6.1-sol", provider="azure_foundry"))
    monkeypatch.setattr(provider_foundry, "load_provider_config_from_env", lambda: provider_foundry.ProviderConfig(
        base_url="https://unit-resource.openai.azure.com", api_key="synthetic-unit-key",
        deployment_pro="Different-Pro-Model", deployment_flash="gpt-6.1-sol", source="platform_default"))
    meters, calls, captures, events = [], [], [], []
    saved, unknown = {}, set()
    capture_request = model_capture.capture_request

    def capture(*args, **kwargs):
        events.append("capture")
        result = capture_request(*args, **kwargs)
        captures.append(result)
        return result
    monkeypatch.setattr(model_capture, "capture_request", capture)

    def recover(key):
        events.append("recover")
        if key in unknown:
            raise CreditsUnavailable("original model receipt needs reconciliation")
        return saved.get(key)
    monkeypatch.setattr(metered, "recover_model_call", recover)

    async def paid_call(callback, *, before_model_receipt, **kwargs):
        # Only the financial lifecycle is synthetic. The owner helpers validate
        # the actual capture and original response fields, including details.
        binding = gate.validate_model_capture(kwargs["model_binding"], kwargs["request_digest"],
            **{name: kwargs[name] for name in ("purpose", "provider", "model", "estimate_micro", "max_tokens")})
        if kwargs.get("before_dispatch") is not None:
            kwargs["before_dispatch"]()
        byok = kwargs.get("payer") == "founder"
        meter = gate.Meter(hold=None if byok else SimpleNamespace(reservation_id="hold"),
            passthrough=byok, outcome_protected=True, model_binding=binding,
            request_digest=kwargs["request_digest"], token_ceiling=kwargs["max_tokens"],
            **{name: kwargs.get(name) for name in ("purpose", "provider", "model", "estimate_micro",
                "workspace_id", "venture_id", "account_id", "request_id", "payer")})
        meter.kwargs = kwargs
        meters.append(meter)
        key = metered.model_call_key(**{name: kwargs[name] for name in
            ("workspace_id", "venture_id", "purpose", "request_id")})
        unknown.add(key)
        try:
            result = await callback()
        except Exception as exc:
            if definitive_provider_rejection(exc):
                meter.confirm_unspent()
                if run.settlement_error is not None:
                    raise run.settlement_error from exc
                saved[key] = {"outcome": "rejected", "credits_micro": 0, "output_usable": False}
                unknown.remove(key)
            raise
        usage = metered.capture_model_receipt(meter, result)
        before_model_receipt(result, usage)
        meter._model_receipt_checkpointed = True
        gate.record_model_receipt(meter, usage)
        if run.settlement_error is not None:
            raise run.settlement_error
        meter.settle_result = SimpleNamespace(actual_micro=meter.actual_micro)
        saved[key] = {"outcome": "confirmed", "credits_micro": meter.actual_micro,
            "output_usable": not meter.billing_output_error}
        unknown.remove(key)
        if meter.billing_output_error:
            raise CreditsUnavailable("provider output is unusable")
        return result
    monkeypatch.setattr(metered, "call", paid_call)

    def run(*, error=None, usage=None, body="not JSON", admit=True,
            returned_model="gpt-6.1-sol", service_tier="default"):
        async def transport(messages, **kwargs):
            assert meters[-1].outcome_protected is True
            assert events.index("recover") < events.index("capture")
            config, captured_messages, schema, evidence = captures[-1]
            assert messages == captured_messages and kwargs["config"] is config
            assert kwargs["response_schema"] == schema
            assert evidence["model_binding"] == meters[-1].model_binding
            assert evidence["request_digest"] == meters[-1].request_digest
            calls.append(kwargs)
            if error is not None:
                raise error
            return SimpleNamespace(usage=usage, model=returned_model, service_tier=service_tier,
                choices=[SimpleNamespace(
                message=SimpleNamespace(content=body))])
        monkeypatch.setattr(sole, "transmit", transport)
        return asyncio.run(copy.generate_copy("A bakery order board", workspace_id="ws_firstlook",
            venture_id="ven_firstlook", generation_intent=admitted.intent if admit else None))
    run.admitted, run.captures, run.events = admitted, captures, events
    run.saved, run.unknown, run.settlement_error = saved, unknown, None
    return copy, run, meters, calls, []


@pytest.mark.parametrize("usage", [None, {}, {"prompt_tokens": 3},
    _usage() | {"prompt_tokens": True, "completion_tokens": 1, "total_tokens": 2},
    _usage() | {"prompt_tokens": -1, "completion_tokens": 1, "total_tokens": 0},
    _usage() | {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 5},
    _usage(3, 1, cached=4), _usage(3, 1, reasoning=2)])
def test_unknown_usage_has_one_protected_attempt_and_no_plain_success(model, usage):
    from llm_gateway.errors import ProviderCallError
    _, run, meters, calls, _ = model
    with pytest.raises(ProviderCallError) as failed:
        run(usage=usage)
    assert failed.value.code == "provider_outcome_unknown"
    assert len(calls) == 1 and calls[0]["single_attempt"] is True
    assert calls[0]["workspace_id"] == "ws_firstlook" and calls[0]["max_tokens"] == 2048
    assert meters[0].recorded is False and meters[0].definitive_failure is False


@pytest.mark.parametrize("error", [TimeoutError("Synthetic timeout"), ConnectionError("Synthetic connection"),
    RuntimeError("Synthetic provider failure")])
def test_transport_unknown_is_not_fallback_or_second_attempt(model, error):
    from llm_gateway.errors import ProviderCallError
    with pytest.raises(ProviderCallError) as failed:
        model[1](error=error)
    assert failed.value.code == "provider_outcome_unknown"
    assert len(model[3]) == 1 and model[2][0].recorded is False


def test_cancellation_keeps_protection_and_never_becomes_copy(model):
    with pytest.raises(asyncio.CancelledError):
        model[1](error=asyncio.CancelledError())
    assert len(model[3]) == 1 and model[2][0].outcome_protected is True
    assert model[2][0].recorded is False


def test_valid_zero_usage_is_known_paid_unusable_output(model):
    result = model[1](usage=_usage())
    assert result[1:] == (0.0, True)
    meter = model[2][0]
    assert (meter.prompt_tokens, meter.completion_tokens, meter.cached_tokens, meter.reasoning_tokens) == (0, 0, 0, 0)
    assert len(model[3]) == 1
    call = model[3][0]
    assert call["tier"] == "flash" and call["use_workspace_override"] is False
    assert call["config"].deployment_flash == "gpt-6.1-sol"
    assert call["config"].captured_tier == "flash" and call["config"].api_mode == "responses"
    assert call["config"].source == "platform_default"
    assert meter.kwargs["model"] == call["config"].deployment_flash
    assert call["max_tokens"] == call["config"].max_output_tokens == meter.model_binding["max_output_tokens"] == 2048
    assert call["response_schema"] == {"type": "json_object"}
    assert meter.token_ceiling == meter.model_binding["input_token_limit"] + 2048
    assert model[1].captures[0][3]["clock_policy"] == "server_clock_256_bytes"
    assert meter.settle_result.actual_micro == 0


@pytest.mark.parametrize("sol", [False, True])
def test_captured_copy_cap_matches_its_credit_quote(model, monkeypatch, sol):
    from app.intake import model_capture
    from llm_gateway import provider_foundry

    deployment = "supymem-gpt-6-1-sol" if sol else "DeepSeek-V4-Pro"
    monkeypatch.setattr(provider_foundry, "load_provider_config_from_env", lambda:
        provider_foundry.ProviderConfig(base_url="https://unit-resource.openai.azure.com",
            api_key="synthetic-unit-key", deployment_pro=deployment, deployment_flash=deployment))
    quoted = []
    estimate = model_capture.estimate_model_binding_micro
    monkeypatch.setattr(model_capture, "estimate_model_binding_micro", lambda binding:
        quoted.append(binding["max_output_tokens"]) or estimate(binding))
    model[1](usage=_usage(), returned_model=deployment)
    assert quoted == [2048 if sol else 900]
    assert model[3][0]["max_tokens"] == quoted[0]
    assert (model[3][0]["config"].api_mode == "responses") is sol


def test_complete_usage_and_honest_model_copy_preserve_normal_success(model):
    result = model[1](usage=_usage(10, 20, cached=3, reasoning=2),
        body=json.dumps(COPY))
    assert result[2] is False and result[0]["headline"] == COPY["headline"]
    assert result[1] > 0 and len(model[3]) == 1
    assert model[2][0].recorded is True and model[2][0].settle_result is not None
    assert (model[2][0].cached_tokens, model[2][0].reasoning_tokens) == (3, 2)


@pytest.mark.parametrize("details", [{}, {"prompt_tokens_details": None, "completion_tokens_details": None}])
def test_unreported_breakdown_charges_full_rates_once_and_recovers_copy(model, details):
    """No cache discount; all reasoning is already billed in completion tokens, so no undercharge."""
    _, run, meters, calls, _ = model
    result = run(usage={"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4, **details},
        body=json.dumps(COPY), service_tier=None)
    assert result[2] is False and result[0]["headline"] == COPY["headline"]
    assert result[1] == 16 / 1_000_000
    meter = meters[0]
    assert meter.recorded is True and meter.settle_result.actual_micro == 16
    assert (meter.prompt_tokens, meter.completion_tokens, meter.cached_tokens, meter.reasoning_tokens) == (3, 1, 0, 0)
    assert run() == result
    assert len(meters) == len(calls) == 1 and not run.unknown


def test_provider_503_is_held_not_a_known_rejection_or_free_draft(model):
    from llm_gateway.errors import ProviderCallError
    with pytest.raises(ProviderCallError):
        model[1](error=ProviderCallError("provider_down", "Synthetic failure", status=503, retryable=True))
    assert len(model[3]) == 1 and model[2][0].definitive_failure is False
    assert model[2][0].outcome_protected is True and model[2][0].recorded is False


def test_existing_unknown_blocks_fresh_transport_and_reservation(model):
    from app.db.models import CreditReservation
    from llm_gateway.errors import ProviderCallError
    source = model[1].admitted
    with source.db()() as session:
        session.add(CreditReservation(id="saved-hold", account_id="account", amount_micro=1,
            purpose="firstlook.copy", workspace_id="ws_firstlook", venture_id="ven_firstlook",
            status="reconciling", created_at=source.now, expires_at=source.now))
        session.commit()
    with pytest.raises(ProviderCallError):
        model[1]()
    assert model[2] == [] and model[3] == []
    assert model[1].captures == []


def test_missing_platform_credit_account_refuses_before_transport(model, monkeypatch):
    from app.credits import accounts
    from app.credits.errors import CreditsUnavailable
    monkeypatch.setattr(accounts, "get_account", lambda _: None)
    with pytest.raises(CreditsUnavailable):
        model[1]()
    assert model[2] == [] and model[3] == []


def test_missing_original_generation_intent_refuses_before_quote_or_dispatch(model):
    from app.credits.errors import CreditsUnavailable

    with pytest.raises(CreditsUnavailable):
        model[1](admit=False, usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
    assert model[2] == [] and model[3] == []
    assert model[1].captures == []


@pytest.mark.parametrize("intent", [False, True])
def test_default_off_capture_uses_main_path(model, monkeypatch, intent):
    # The founder requires flags-off parity with main, not a free fallback.
    monkeypatch.delenv("COS_FIRSTLOOK_CAPTURE_ENABLED", raising=False)
    calls = []
    async def legacy(idea_text, **kwargs):
        calls.append((idea_text, kwargs))
        return COPY, 0.25
    monkeypatch.setattr(model[0], "_call_model_legacy", legacy)
    result = model[1](admit=intent)
    assert result[1:] == (0.25, False)
    assert len(calls) == 1 and calls[0][1]["workspace_id"] == "ws_firstlook"
    assert model[2] == [] and model[3] == [] and model[1].captures == []


def test_enabled_capture_still_recovers_original_paid_output(model):
    first = model[1](usage=_usage(10, 20), body=json.dumps(COPY))
    assert model[1]() == first
    assert len(model[2]) == len(model[3]) == len(model[1].captures) == 1


def test_wrong_persisted_onboarding_state_refuses_before_quote_or_dispatch(model):
    from app.credits.errors import CreditsUnavailable
    from app.db.models import OnboardingRun
    source = model[1].admitted
    with source.db()() as session:
        session.get(OnboardingRun, source.ctx.run.id).status = "cancelled"
        session.commit()
    with pytest.raises(CreditsUnavailable):
        model[1](usage=_usage())
    assert model[2] == [] and model[3] == [] and model[1].captures == []


def test_lint_regeneration_keeps_distinct_original_slots_and_confirmed_cost(model):
    from app.db.models import OnboardingStep
    result = model[1](usage=_usage(10, 20),
        body=json.dumps(COPY | {"headline": "Trusted by 10,000 customers"}))
    assert result[2] is True and len(model[2]) == len(model[3]) == 2
    request_ids = [meter.request_id for meter in model[2]]
    assert request_ids == [f"{model[1].admitted.intent}:firstlook:{slot}"
        for slot in ("first", "lint-regenerate")]
    assert request_ids[0] != request_ids[1]
    assert result[1] == sum(meter.settle_result.actual_micro for meter in model[2]) / 1_000_000
    with model[1].admitted.db()() as session:
        output = json.loads(session.get(OnboardingStep, model[1].admitted.step.id).output_json)
    assert set(output["_firstlook_copy"]) == {"first", "lint-regenerate"}


def test_retry_recovers_original_money_and_copy_before_new_capture(model, monkeypatch):
    from app.llm import founder_provider
    from app.onboarding.state import record_failed, record_running
    run = model[1]
    original = run(usage=_usage(10, 20), body=json.dumps(COPY))
    record_failed(run.admitted.step, "interrupted_after_settlement")
    retry = record_running(run.admitted.ctx.run.id, "commit.lock_charter", "commit", run.admitted.key)
    assert retry.attempt == 2 and retry.id != run.admitted.step.id
    run.captures.clear()
    run.events.clear()
    def no_new_quote(*args, **kwargs):
        raise AssertionError("recovery consulted the current model route")
    monkeypatch.setattr(founder_provider, "resolve_llm_config", no_new_quote)
    assert run() == original
    assert run.events == ["recover"] and run.captures == []
    assert len(model[2]) == len(model[3]) == 1


def test_retry_without_original_money_refuses_a_fresh_quote(model):
    from app.credits.errors import CreditsUnavailable
    from app.onboarding.state import record_failed, record_running
    source = model[1].admitted
    record_failed(source.step, "interrupted_before_dispatch")
    retry = record_running(source.ctx.run.id, "commit.lock_charter", "commit", source.key)
    assert retry.attempt == 2
    with pytest.raises(CreditsUnavailable):
        model[1](usage=_usage())
    assert model[2] == [] and model[3] == [] and model[1].captures == []


def test_wrong_founder_credit_wallet_refuses_before_transport(model, monkeypatch):
    from app.credits import accounts
    from app.credits.errors import CreditsUnavailable
    monkeypatch.setattr(accounts, "get_account", lambda _: SimpleNamespace(id="foreign", scope="person", owner_person_id="other", workspace_id=None))
    with pytest.raises(CreditsUnavailable):
        model[1]()
    assert model[2] == [] and model[3] == []


def test_missing_byok_capture_refuses_without_platform_dial(model, monkeypatch):
    from app.llm import founder_provider
    from app.credits.errors import CreditsUnavailable
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda _: SimpleNamespace(source="founder_override"))
    monkeypatch.setattr(founder_provider, "to_provider_config", lambda *a, **k: None)
    with pytest.raises(CreditsUnavailable):
        model[1]()
    assert model[2] == [] and model[3] == []


def test_byok_capture_and_payer_are_bound_without_an_ambient_resolver(model, monkeypatch):
    from app.llm import founder_provider
    from llm_gateway.provider_foundry import ProviderConfig
    captured = ProviderConfig(source="founder_override", base_url="https://unit-byok.openai.azure.com",
        api_key="synthetic-byok-key", deployment_pro="Different-Byok-Pro",
        deployment_flash="reviewed-byok-deployment", catalog_model="gpt-6.1-sol")
    resolved = SimpleNamespace(source="founder_override")
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda _: resolved)
    def capture(wid, *, resolved_config):
        assert wid == "ws_firstlook" and resolved_config is resolved
        return captured
    monkeypatch.setattr(founder_provider, "to_provider_config", capture)
    result = model[1](usage=_usage())
    assert result[1:] == (0.0, True)
    dial = model[3][0]["config"]
    assert dial.source == "founder_override" and model[3][0]["use_workspace_override"] is False
    assert dial.api_key.get_secret_value() == "synthetic-byok-key"
    assert dial.deployment_flash == captured.deployment_flash == "reviewed-byok-deployment"
    assert model[3][0]["tier"] == "flash" and dial.captured_tier == "flash"
    assert model[2][0].kwargs["model"] == "reviewed-byok-deployment"
    assert model[2][0].kwargs["payer"] == "founder"


@pytest.mark.parametrize("code,status", [("bad_api_key", 401), ("out_of_credits", 402), ("rate_limited", 429)])
def test_exact_rejection_is_released_but_never_a_fallback_draft(model, code, status):
    from llm_gateway.errors import ProviderCallError
    with pytest.raises(ProviderCallError) as error:
        model[1](error=ProviderCallError(code, "Synthetic rejection", status=status))
    assert error.value.unspent_confirmed is True
    assert len(model[3]) == 1 and model[2][0].definitive_failure is True


@pytest.mark.parametrize("code,status", [("different_code", 402), ("out_of_credits", 500),
    ("out_of_credits", None)])
def test_nonmatching_credit_rejection_stays_protected_unknown(model, code, status):
    from llm_gateway.errors import ProviderCallError
    with pytest.raises(ProviderCallError) as error:
        model[1](error=ProviderCallError(code, "Synthetic unknown", status=status))
    assert error.value.code == "provider_outcome_unknown"
    assert not getattr(error.value, "unspent_confirmed", False)
    assert len(model[3]) == 1 and model[2][0].outcome_protected is True
    assert model[2][0].definitive_failure is False and model[2][0].recorded is False


@pytest.mark.parametrize("returned_model,service_tier", [(None, "default"),
    ("unpriced-model", "default"), ("gpt-6.1-sol", "unpriced-tier")])
def test_unconfirmed_returned_model_or_tier_never_becomes_a_paid_draft(model, returned_model, service_tier):
    from llm_gateway.errors import ProviderCallError
    with pytest.raises(ProviderCallError) as error:
        model[1](usage=_usage(10, 20), body=json.dumps(COPY),
            returned_model=returned_model, service_tier=service_tier)
    assert error.value.code == "provider_outcome_unknown"
    assert len(model[3]) == 1 and model[2][0].outcome_protected is True
    assert model[2][0].recorded is False and model[2][0].definitive_failure is False


@pytest.mark.parametrize("idea_text,audience", [
    ("A shift handoff board for warehouse supervisors", "warehouse supervisors"),
    ("A meal planning journal for families", "families"),
])
def test_local_copy_uses_saved_owner_model_without_a_platform_wallet(admitted, monkeypatch, idea_text, audience):
    from app.core.providers_local import save_settings
    from app.db.models import CreditReservation
    from app.firstlook import copy
    from llm_gateway import sole

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setenv("LLM_CASSETTE_MODE", "off")
    monkeypatch.setenv("FIRSTLOOK_COPY_MODE", "live")
    monkeypatch.delenv("COS_FIRSTLOOK_CAPTURE_ENABLED", raising=False)
    save_settings(provider="openai", base_url="https://provider.example.invalid/v1",
                  models={"chat": "gpt-6.1-sol"}, api_key="fixture-owner-key")
    calls = []

    async def transmit(messages, **kwargs):
        calls.append(kwargs)
        assert kwargs["config"].source == "founder_override"
        assert kwargs["config"].api_key.get_secret_value() == "fixture-owner-key"
        return SimpleNamespace(usage=_usage(), choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps(copy.fallback_copy(idea_text, audience=audience))))])

    monkeypatch.setattr(sole, "transmit", transmit)
    result = asyncio.run(copy.generate_copy(idea_text, audience=audience,
        workspace_id="ws_firstlook", venture_id="ven_firstlook"))
    assert result[0]["audience"] == audience and result[2] is False
    assert len(calls) == 1 and calls[0]["config"].deployment_pro == "gpt-6.1-sol"
    with admitted.db()() as session:
        assert session.query(CreditReservation).count() == 0


def test_deliberate_offline_copy_never_enters_charge_or_transport(model, monkeypatch):
    monkeypatch.setenv("FIRSTLOOK_COPY_MODE", "offline")
    result = model[1]()
    assert result[1:] == (0.0, True)
    assert model[2] == [] and model[3] == []


def test_onboarding_unknown_cannot_rerun_a_free_success(model, monkeypatch):
    from app.firstlook import service, steps
    from llm_gateway.errors import ProviderCallError
    calls = []
    async def unknown(*args, **kwargs):
        calls.append(kwargs)
        raise ProviderCallError("provider_outcome_unknown", "Synthetic unknown")
    monkeypatch.setattr(service, "generate_first_look", unknown)
    ctx = SimpleNamespace(out={}, venture=SimpleNamespace(raw_idea="A bakery board", id="venture",
        name="Board", workspace_id="workspace"))
    with pytest.raises(ProviderCallError):
        asyncio.run(steps.generate(ctx))
    assert len(calls) == 1 and calls[0].get("allow_model") is None


def test_site_confirm_unknown_reply_is_pause_without_replay_card(model, monkeypatch, tmp_path):
    from app.chat import site_actions
    from app.firstlook import service, versions
    from llm_gateway.errors import ProviderCallError
    monkeypatch.setattr(site_actions, "_venture", lambda _: ("workspace", "Bakery board", "Board"))
    monkeypatch.setattr(site_actions, "_saved_page_builds", lambda *args: [])
    path = tmp_path / "index.html"
    path.write_text("<html><body>Your saved page</body></html>")
    monkeypatch.setattr(versions, "get_latest", lambda *a, **k: SimpleNamespace(copy={}, archetype="generic", stage="first_look", path=str(path)))
    async def unknown(*args, **kwargs):
        raise ProviderCallError("provider_outcome_unknown", "PRIVATE SYNTHETIC ERROR")
    monkeypatch.setattr(service, "generate_first_look", unknown)
    result = asyncio.run(site_actions._rewrite("venture", {}, "actor"))
    assert result["kind"] == "needs_you" and "outcome is not confirmed" in result["body_md"]
    assert "PRIVATE SYNTHETIC ERROR" not in result["body_md"]
    assert all("action" not in action for action in result["actions"])


@pytest.mark.parametrize("kind", ["unavailable", "funds"])
def test_credit_rejection_on_context_entry_preserves_error_before_transport(model, monkeypatch, kind):
    from app.credits import metered
    from app.credits.errors import CreditsUnavailable, InsufficientCredits
    error = CreditsUnavailable("synthetic admission unavailable") if kind == "unavailable" else InsufficientCredits("account", 1, 2)
    async def rejected(callback, **kwargs):
        raise error
    monkeypatch.setattr(metered, "call", rejected)
    with pytest.raises(type(error)) as caught:
        model[1]()
    assert caught.value is error and model[2] == [] and model[3] == []


@pytest.mark.parametrize("rejected", [False, True])
def test_credit_failure_on_context_exit_closes_one_provider_attempt(model, rejected):
    from app.credits.errors import CreditsUnavailable
    from llm_gateway.errors import ProviderCallError
    error = CreditsUnavailable("synthetic exit reconciliation unavailable")
    model[1].settlement_error = error
    provider_error = ProviderCallError("rate_limited", "synthetic rejection", status=429) if rejected else None
    with pytest.raises(ProviderCallError) as caught:
        model[1](error=provider_error, usage=_usage(10, 20))
    assert caught.value.code == "provider_outcome_unknown"
    assert caught.value.__cause__.code == "credits_unavailable"
    assert caught.value.retryable is False and not getattr(caught.value, "unspent_confirmed", False)
    assert len(model[3]) == 1 and model[2][0].outcome_protected is True
    assert model[2][0].definitive_failure is rejected
    assert model[2][0].recorded is (not rejected)

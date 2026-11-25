"""A bounded provider wait must cancel once and retain uncertain classification."""
import asyncio
from types import SimpleNamespace

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture(scope="module")
def metered(tmp_path_factory):
    with pytest.MonkeyPatch.context() as env:
        env.setenv("COMPANY_OS_DATA_DIR", str(tmp_path_factory.mktemp("provider-deadline")))
        env.setenv("LLM_OFFLINE", "1")
        load_company_os_app()
        from app.research import metered
        yield metered


def test_total_deadline_cancels_single_dial_and_classifies_uncertain(metered):
    calls, cancelled = [], []

    async def transmit(**kwargs):
        calls.append(kwargs)
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    with pytest.raises(TimeoutError) as error:
        asyncio.run(metered._transmit_with_deadline(transmit, {"single_attempt": True}, 0.01))
    assert calls == [{"single_attempt": True}]
    assert cancelled == [True]
    assert metered._failure_diagnostics(error.value, 0.01)["category"] == "timeout"
    assert metered._failure(error.value)[0] == "uncertain"


def test_response_passes_through_without_second_dial(metered):
    response = object()
    calls = []

    async def transmit(**kwargs):
        calls.append(kwargs)
        return response

    assert asyncio.run(metered._transmit_with_deadline(transmit, {}, 1)) is response
    assert calls == [{}]


@pytest.mark.parametrize("sol", [False, True])
def test_short_planning_cap_matches_reservation_and_dispatch(metered, monkeypatch, sol):
    from app.credits import estimate
    from app.llm import founder_provider
    from llm_gateway import provider_foundry, sole

    deployment = "supymem-gpt-6-1-sol" if sol else "DeepSeek-V4-Pro"
    config = provider_foundry.ProviderConfig(source="platform_default",
        deployment_pro=deployment, deployment_flash=deployment)
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    monkeypatch.setenv("LLM_CASSETTE_MODE", "off")
    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.delenv("MODEL_ROUTING_OVERRIDES", raising=False)
    monkeypatch.setattr(sole, "load_provider_config_from_env", lambda: config)
    monkeypatch.setattr(founder_provider, "register_workspace_llm_resolver", lambda: None)
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda _: SimpleNamespace(
        source="platform_default", provider=config.provider, model=deployment))
    quoted, dispatched, reservations = [], [], []
    monkeypatch.setattr(estimate, "estimate_llm_micro", lambda model, **kwargs:
        quoted.append(kwargs["max_tokens"]) or 1000)
    async def transmit(**kwargs):
        dispatched.append(kwargs)
        return {}
    def perform(*args, **kwargs):
        reservations.append(kwargs)
        return kwargs["callback"]()
    monkeypatch.setattr(sole, "transmit", transmit)
    monkeypatch.setattr(metered, "perform", perform)
    journal = SimpleNamespace(recover=lambda *args: None,
        read=lambda _: SimpleNamespace(workspace_id="workspace"))
    metered.synthesize("run", {}, purpose="idea.validate", messages=[], schema={}, parse=lambda value: value,
        max_tokens=1200, journal=journal, cap_at_reservation=True)
    assert quoted == [2048 if sol else 1200]
    assert dispatched[0]["max_tokens"] == quoted[0]
    assert reservations[0]["token_ceiling"] == quoted[0]
    if sol:
        assert dispatched[0]["config"].api_mode == "responses"
        assert dispatched[0]["use_workspace_override"] is False


@pytest.mark.parametrize("seconds,pinned", [(0, True), (301, True), (True, True), (240, False)])
def test_invalid_deadline_rejected_before_provider_resolution(metered, seconds, pinned):
    journal = SimpleNamespace(recover=lambda *args: None)
    with pytest.raises(metered.FatalError, match="invalid_provider_deadline"):
        metered.synthesize("run", {}, messages=[], schema={}, parse=lambda x: x,
                           journal=journal, provider_timeout_seconds=seconds, pin_provider=pinned)


def _slow_then(outcomes):
    """Fake provider: each call consumes one outcome ('slow' hangs, else returns it)."""
    calls = []

    async def transmit(**kwargs):
        calls.append(kwargs)
        out = outcomes[len(calls) - 1]
        if out == "slow":
            await asyncio.Event().wait()
        return out

    return calls, transmit


def test_timeout_does_not_redial_fallback_deployment(metered):
    calls, transmit = _slow_then(["slow", "fast-answer"])
    with pytest.raises(TimeoutError):
        asyncio.run(metered._transmit_with_deadline(
            transmit, {"tier": "pro", "purpose": "idea.validate"}, 0.01, {"tier": "flash", "purpose": "idea.validate"}))
    assert [c["tier"] for c in calls] == ["pro"]


def test_timeout_on_both_raises_timeout_classified_uncertain(metered):
    calls, transmit = _slow_then(["slow", "slow"])
    with pytest.raises(TimeoutError) as error:
        asyncio.run(metered._transmit_with_deadline(transmit, {"tier": "pro"}, 0.01, {"tier": "flash"}))
    assert len(calls) == 1  # The unknown primary cannot justify another paid call.
    assert metered._failure(error.value)[0] == "uncertain"


def test_connection_uncertainty_never_redials_fallback(metered):
    from llm_gateway.errors import ProviderCallError
    calls = []

    async def transmit(**kwargs):
        calls.append(kwargs)
        raise ProviderCallError("provider_down", "closed connection failure", retryable=True)

    with pytest.raises(ProviderCallError) as error:
        asyncio.run(metered._transmit_with_deadline(transmit, {"single_attempt": True}, 1, {"tier": "flash"}))
    assert calls == [{"single_attempt": True}]
    assert metered._failure(error.value)[0] == "uncertain"


def test_non_timeout_error_is_not_retried(metered):
    calls = []

    async def transmit(**kwargs):
        calls.append(kwargs)
        raise ValueError("boom")

    with pytest.raises(ValueError):
        asyncio.run(metered._transmit_with_deadline(transmit, {}, 1, {"tier": "flash"}))
    assert len(calls) == 1


def test_timing_log_records_duration_and_kind(metered, caplog):
    import logging
    calls, transmit = _slow_then(["ok"])
    with caplog.at_level(logging.INFO, logger=metered.log.name):
        asyncio.run(metered._transmit_with_deadline(transmit, {"purpose": "idea.validate"}, 1))
    rec = next(r for r in caplog.records if r.getMessage() == "model_call_timing")
    assert rec.operation_kind == "idea.validate" and rec.outcome == "ok" and rec.duration_ms >= 0


def test_interactive_timeout_is_bounded(metered):
    assert metered.INTERACTIVE_TIMEOUT_SECONDS == 90

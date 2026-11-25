"""Server-owned persistence hooks with synthetic transport/gate; no paid calls."""
import asyncio
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def metered(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    load_company_os_app()
    from app.credits import metered as module
    from app.credits.gate import Meter
    from app.credits.reserve import Hold
    events, meters = [], []
    @contextmanager
    def charge(**kwargs):
        payer = kwargs.get("payer")
        meter = Meter(hold=None if payer == "founder" else Hold("hold_hook", "acc_hook", kwargs["estimate_micro"]),
            passthrough=payer == "founder", purpose=kwargs["purpose"],
            **{key: kwargs.get(key) for key in ("workspace_id", "venture_id", "account_id", "model", "provider", "request_id", "payer")},
            estimate_micro=kwargs["estimate_micro"], task_invocation="inv_hook")
        meter.protect_outcome = lambda: events.append("protected")
        meters.append(meter)
        try:
            yield meter
        finally:
            events.append("exit")
            if meter.recorded and meter.hold:
                meter.settle_result = SimpleNamespace(actual_micro=meter.actual_micro)
            if payer == "founder":
                meter.task_usage_failed = True
    @asynccontextmanager
    async def async_charge(**kwargs):
        with charge(**kwargs) as meter:
            yield meter
    monkeypatch.setattr(module, "charge_sync", charge)
    monkeypatch.setattr(module, "charge", async_charge)
    return module, events, meters


def response():
    return SimpleNamespace(usage={"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12,
        "prompt_tokens_details": {"cached_tokens": 6}, "completion_tokens_details": {"reasoning_tokens": 0}},
        body="saved provider response")


def arguments():
    return dict(purpose="task.draft", estimate_micro=1000, account_id="acc_hook",
        workspace_id="wsp_hook", venture_id="ven_hook", provider="test", model="test", request_id="run:step")


@pytest.mark.parametrize("self_hosted,expected", [(True, 1), (False, 1000)])
def test_firecrawl_settles_using_the_server_that_served_the_call(metered, self_hosted, expected):
    module, events, meters = metered
    result = SimpleNamespace(usage={"firecrawl_self_hosted": True} if self_hosted else {"creditsUsed": 1})
    options = arguments() | {"provider": "firecrawl", "model": None}
    from app.core.local_mode import is_local

    if is_local() and self_hosted:
        from app.core.local_secrets import update_settings

        update_settings("search", {"provider": "firecrawl", "firecrawl_url": "https://reader.example"}, {})
        expected = 0
    assert module.call_sync(lambda: result, **options) is result
    assert meters[0].actual_micro == expected
    assert events == ["protected", "exit"]


def test_sync_hook_receives_confirmed_original_meter_before_exit(metered):
    module, events, meters = metered
    def persist(result, usage):
        assert result.body == "saved provider response" and events == ["protected"]
        assert meters[0].recorded and meters[0].settle_result is None
        expected = {"source": "platform", "reservation_id": "hold_hook", "account_id": "acc_hook",
            "estimate_micro": 1000, "workspace_id": "wsp_hook", "venture_id": "ven_hook",
            "purpose": "task.draft", "model": "test", "provider": "test", "request_id": "run:step",
            "task_invocation": "inv_hook", "actual_micro": 175, "provider_actual_micro": 175,
            "prompt_tokens": 8, "completion_tokens": 4, "cached_tokens": 6, "total_tokens": 12}
        assert {key: usage[key] for key in expected} == expected
        usage["actual_micro"] = 0  # Persistence receives a copy, never the live meter.
        events.append("persisted")
    result = module.call_sync(response, **arguments(), actual_micro=lambda _: 175, before_meter_exit=persist)
    assert result.body == "saved provider response" and events == ["protected", "persisted", "exit"]
    assert meters[0].actual_micro == 175


@pytest.mark.asyncio
async def test_async_hook_is_awaited_before_exit(metered):
    module, events, meters = metered
    async def provider():
        return response()
    async def persist(result, usage):
        events.append("saving")
        await asyncio.sleep(0)
        assert meters[0].settle_result is None
        events.append("persisted")
    await module.call(provider, **arguments(), actual_micro=lambda _: 175, before_meter_exit=persist)
    assert events == ["protected", "saving", "persisted", "exit"]


def test_sync_hook_rejects_and_closes_coroutine_without_running_it(metered):
    module, events, meters = metered
    async def persist(result, usage):
        pytest.fail("sync wrapper must never fire an async hook")
    pending = []
    def hook(result, usage):
        value = persist(result, usage)
        pending.append(value)
        return value
    with pytest.raises(TypeError, match="must be synchronous"):
        module.call_sync(response, **arguments(), actual_micro=lambda _: 175, before_meter_exit=hook)
    assert pending[0].cr_frame is None and meters[0].recorded and not meters[0].definitive_failure
    assert events == ["protected", "exit"]


def test_persistence_failure_does_not_classify_known_provider_work_as_unspent(metered):
    module, events, meters = metered
    def persist(result, usage):
        raise RuntimeError("synthetic journal commit failure")
    with pytest.raises(RuntimeError, match="journal commit"):
        module.call_sync(response, **arguments(), actual_micro=lambda _: 175, before_meter_exit=persist)
    assert meters[0].recorded and meters[0].actual_micro == 175 and not meters[0].definitive_failure
    assert events == ["protected", "exit"]


@pytest.mark.asyncio
async def test_byok_task_counter_failure_withholds_output(metered):
    module, _, meters = metered
    from app.credits.errors import CreditsUnavailable
    async def provider():
        return response()
    with pytest.raises(CreditsUnavailable, match="reconciliation"):
        await module.call(provider, **arguments(), payer="founder", actual_micro=lambda _: 175)
    assert meters[0].recorded and meters[0].hold is None and meters[0].task_usage_failed


@pytest.mark.hosted_only
@pytest.mark.asyncio
async def test_llm_hook_is_private_and_receives_actual_cache_usage(metered, monkeypatch):
    module, events, _ = metered
    from app.credits import estimate
    from app.llm import founder_provider
    from llm_gateway import sole, provider_foundry
    config = provider_foundry.ProviderConfig(deployment_pro="gpt-6.1-sol", deployment_flash="gpt-6.1-sol")
    monkeypatch.setattr(estimate, "pricing_table", lambda: {"gpt-6.1-sol": {
        "input_per_mtok": "2", "output_per_mtok": "10", "cached_input_per_mtok": "0.1"}})
    monkeypatch.setattr(founder_provider, "register_workspace_llm_resolver", lambda: None)
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda _: SimpleNamespace(source="platform_default"))
    monkeypatch.setattr(provider_foundry, "load_provider_config_from_env", lambda: config)
    called, saved = [], []
    async def provider(*args, **kwargs):
        assert "before_meter_exit" not in kwargs and kwargs["single_attempt"] is True
        called.append(True)
        return response()
    monkeypatch.setattr(sole, "transmit", provider)
    def persist(result, usage):
        assert events == ["protected"]
        saved.append(usage)
    await module.llm([{"role": "user", "content": "synthetic prompt"}], purpose="task.draft",
        workspace_id="wsp_hook", request_id="run:step", before_meter_exit=persist)
    assert called == [True] and len(saved) == 1
    assert saved[0]["cached_tokens"] == 6 and saved[0]["total_tokens"] == 12
    assert saved[0]["actual_micro"] == 45 and saved[0]["task_invocation"] == "inv_hook"

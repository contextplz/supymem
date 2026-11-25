"""Desktop update policy is local; persisted desktop state never pauses cloud AI."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from desktop_runtime.update_policy import DEFAULT_UPDATE_ENDPOINT, evaluate_manifest
from llm_gateway.hosted import HostedUpdateRequired
from llm_gateway.provider_foundry import ProviderConfig
from llm_gateway.update_gate import enforce_forced_update, persist_update_status
from tests.unit.test_desktop_update_policy import FORCE_JSON, OPTIONAL_JSON


@pytest.fixture()
def update_env(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_APP_VERSION", "0.1.0")
    return tmp_path


def test_local_update_policy_requires_newer_mandatory_release(update_env):
    result = evaluate_manifest("0.1.0", DEFAULT_UPDATE_ENDPOINT, FORCE_JSON)
    persist_update_status(result)
    with pytest.raises(HostedUpdateRequired):
        enforce_forced_update(current_version="0.1.0")
    enforce_forced_update(current_version=result["version"])


def test_optional_update_and_byok_do_not_enforce_local_gate(update_env):
    persist_update_status(evaluate_manifest("0.1.0", DEFAULT_UPDATE_ENDPOINT, OPTIONAL_JSON))
    enforce_forced_update(current_version="0.1.0")
    persist_update_status(evaluate_manifest("0.1.0", DEFAULT_UPDATE_ENDPOINT, FORCE_JSON))
    enforce_forced_update(current_version="0.1.0", byok=True)


@pytest.mark.asyncio
async def test_cloud_provider_ignores_desktop_update_snapshot(update_env, monkeypatch):
    from llm_gateway import sole
    from llm_gateway.boundary import Decision

    persist_update_status(evaluate_manifest("0.1.0", DEFAULT_UPDATE_ENDPOINT, FORCE_JSON))
    called = []

    async def complete(**kwargs):
        called.append(kwargs)
        return {"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete)))
    def no_retry(**kwargs):
        assert kwargs == {"max_retries": 0}
        return client
    client.with_options = no_retry
    scanned = []
    def synthetic_scan(messages, **kwargs):
        scanned.append(kwargs["purpose"])
        return Decision(messages=messages)
    monkeypatch.setattr(sole, "boundary_decision", synthetic_scan)
    cfg = ProviderConfig(provider="azure_foundry", deployment_pro="DeepSeek-V4-Pro", api_key=SecretStr("test-only"))
    monkeypatch.setattr(sole, "open_env_client", lambda _config: (client, cfg))
    monkeypatch.setattr(sole, "resolve_override", lambda *_args, **_kwargs: None)
    await sole.env_chat_completion([{"role": "user", "content": "hello"}])
    assert len(called) == 1
    assert called[0]["model"] == "DeepSeek-V4-Pro"
    assert scanned == ["env_chat_completion"]


def test_current_version_does_not_enforce_stale_force(update_env):
    persist_update_status({"available": True, "current_version": "0.1.0", "version": "0.1.1",
                           "force": True, "mandatory": True, "can_defer": False,
                           "endpoint": DEFAULT_UPDATE_ENDPOINT})
    enforce_forced_update(current_version="0.1.1")

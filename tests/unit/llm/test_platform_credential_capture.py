"""Pure credential capture checks; no clients, app, database or provider calls."""

import json

import pytest
from llm_gateway.provider_foundry import (
    ProviderConfig,
    load_provider_config_from_env,
    pin_platform_credentials,
)
from llm_gateway.routing import resolve_purpose_config
from llm_gateway.errors import ProviderCallError
from llm_gateway.sole import require_env_api_key
from pydantic import SecretStr


@pytest.fixture
def platform_env(monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    monkeypatch.setenv("LLM_PROFILE", "prod")
    monkeypatch.setenv("LLM_API_KEY", "original-fixture-key")
    monkeypatch.delenv("MODEL_ROUTING_OVERRIDES", raising=False)
    monkeypatch.delenv("PAGEBUILDER_OPENAI_API_KEY", raising=False)


@pytest.mark.parametrize("replacement", ["replacement-fixture-key", None])
def test_canonical_capture_preserves_original_key_after_env_change(platform_env, monkeypatch, replacement):
    captured = resolve_purpose_config("venture.chat.assistant", load_provider_config_from_env())
    if replacement is None:
        monkeypatch.delenv("LLM_API_KEY")
    else:
        monkeypatch.setenv("LLM_API_KEY", replacement)
    assert require_env_api_key(captured) == "original-fixture-key"


def test_cloned_canonical_capture_rejects_substituted_key(platform_env):
    captured = load_provider_config_from_env().model_copy(deep=True, update={"api_key": SecretStr("substituted-fixture-key")})
    with pytest.raises(RuntimeError, match="captured platform credentials changed"):
        require_env_api_key(captured)


@pytest.mark.parametrize("key", [None, "", "   "])
def test_missing_canonical_key_is_not_an_authorized_pin(platform_env, monkeypatch, key):
    if key is None:
        monkeypatch.delenv("LLM_API_KEY")
    else:
        monkeypatch.setenv("LLM_API_KEY", key)
    captured = resolve_purpose_config("venture.chat.assistant", load_provider_config_from_env())
    assert captured._platform_api_key_pin is None and captured._platform_host_pin is None
    with pytest.raises(ProviderCallError, match="Your model is not configured") as failure:
        require_env_api_key(captured)
    assert failure.value.code == "model_not_configured"
    assert failure.value.retryable is False
    assert "LLM_API_KEY" not in str(failure.value)


def test_unpinned_explicit_config_still_rejects_alternate_key(platform_env):
    config = ProviderConfig(source="platform_default", api_key=SecretStr("alternate-fixture-key"))
    assert config._platform_api_key_pin is None
    with pytest.raises(RuntimeError, match="does not match LLM_API_KEY"):
        require_env_api_key(config)


def test_private_pin_is_not_serialized(platform_env):
    captured = load_provider_config_from_env()
    dumped = captured.model_dump()
    encoded = captured.model_dump_json()
    assert "_platform_api_key_pin" not in dumped
    assert "_platform_api_key_pin" not in encoded
    assert "original-fixture-key" not in str(dumped)
    assert "original-fixture-key" not in encoded


def test_pagebuilder_key_is_authorized_before_quote_and_remains_pinned(platform_env, monkeypatch):
    monkeypatch.setenv("PAGEBUILDER_OPENAI_API_KEY", "pagebuilder-fixture-key")
    config = ProviderConfig(provider="openai", source="platform_default",
        api_key=SecretStr("pagebuilder-fixture-key"), base_url="https://api.openai.com/v1")
    captured = pin_platform_credentials(config)
    assert captured is not config and config._platform_api_key_pin is None
    monkeypatch.delenv("LLM_API_KEY")
    monkeypatch.delenv("PAGEBUILDER_OPENAI_API_KEY")
    assert require_env_api_key(captured) == "pagebuilder-fixture-key"


def test_capture_helper_refuses_unapproved_key_and_byok(platform_env):
    config = ProviderConfig(source="platform_default", api_key=SecretStr("alternate-fixture-key"))
    with pytest.raises(RuntimeError, match="does not match LLM_API_KEY"):
        pin_platform_credentials(config)
    with pytest.raises(ValueError, match="requires a platform config"):
        pin_platform_credentials(config.model_copy(update={"source": "founder_override"}))


def test_capture_helper_refuses_an_empty_explicit_key(platform_env):
    with pytest.raises(RuntimeError, match="requires a configured API key"):
        pin_platform_credentials(ProviderConfig(source="platform_default"))


def test_data_cannot_forge_a_platform_pin(platform_env):
    forged = {"source": "platform_default", "api_key": "forged-" + "fixture",
              "_platform_api_key_pin": "forged-" + "fixture", "_platform_host_pin": "evil.example"}
    for config in (ProviderConfig.model_validate(forged), ProviderConfig(**forged),
                   ProviderConfig.model_validate_json(json.dumps(forged))):
        assert config._platform_api_key_pin is None and config._platform_host_pin is None
        with pytest.raises(RuntimeError, match="does not match LLM_API_KEY"):
            require_env_api_key(config)


@pytest.mark.asyncio
async def test_forged_platform_source_cannot_reach_captured_sdk_factory(platform_env, monkeypatch):
    from llm_gateway import sole

    monkeypatch.delenv("COMPANY_OS_PACKAGED", raising=False)
    monkeypatch.setattr(sole, "_primary_chat", None)
    monkeypatch.setattr(sole, "create_async_client_from_config",
        lambda config: pytest.fail("An unauthorized key must not reach the SDK"))
    captured = resolve_purpose_config("venture.chat.assistant", ProviderConfig(
        source="platform_default", api_key=SecretStr("forged-fixture-key")))
    with pytest.raises(RuntimeError, match="does not match LLM_API_KEY"):
        await sole._foundry_chat([], tools=None, tier="flash", max_tokens=2048,
            purpose="venture.chat.assistant", workspace_id="synthetic-workspace",
            captured_config=captured, single_attempt=True)


def test_pinned_key_cannot_be_redirected_to_another_host(platform_env, monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://pinned.services.ai.azure.com/openai/v1")
    captured = resolve_purpose_config("venture.chat.assistant", load_provider_config_from_env())
    assert require_env_api_key(captured) == "original-fixture-key"
    same_host = captured.model_copy(update={"base_url": "https://PINNED.services.ai.azure.com/other"})
    assert require_env_api_key(same_host) == "original-fixture-key"
    for url in ("https://evil.example/v1", "", "https://pinned.services.ai.azure.com.evil.example/v1"):
        with pytest.raises(RuntimeError, match="captured platform credentials changed"):
            require_env_api_key(captured.model_copy(update={"base_url": url}))


def test_groq_profile_remains_unpinned(platform_env, monkeypatch):
    monkeypatch.setenv("LLM_PROFILE", "test")
    monkeypatch.setenv("GROQ_API_KEY", "groq-fixture-key")
    config = load_provider_config_from_env()
    assert config.source == "profile_test" and config._platform_api_key_pin is None

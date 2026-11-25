"""Purpose routing is captured before holds and cannot alter provider credentials."""

from __future__ import annotations

import json

import pytest
from pydantic import SecretStr

from llm_gateway.provider_foundry import ProviderConfig
from llm_gateway.routing import UnroutedPurposeError, is_sol_model, resolve_purpose_config


@pytest.fixture
def config(monkeypatch):
    monkeypatch.delenv("MODEL_ROUTING_OVERRIDES", raising=False)
    return ProviderConfig(provider="azure_foundry", base_url="https://resource.services.ai.azure.com",
        api_key=SecretStr("offline-test"), deployment_pro="deepseek-pro", deployment_flash="deepseek-flash")


def test_default_route_captures_declared_tier_without_mutating_source(config):
    captured = resolve_purpose_config("venture.chat.route", config)
    assert captured is not config
    assert (captured.routing_purpose, captured.captured_tier, captured.catalog_model) == (
        "venture.chat.route", "flash", "deepseek-flash")
    assert captured.deployment_pro == captured.deployment_flash == "deepseek-flash"
    assert config.deployment_pro == "deepseek-pro"
    assert config.routing_purpose is None
    assert captured.reasoning_effort == "high"


def test_wildcard_and_exact_overrides_capture_deployment_price_api_and_effort(config, monkeypatch):
    monkeypatch.setenv("MODEL_ROUTING_OVERRIDES", json.dumps({
        "*": {"model": "sol-global-deployment", "catalog_model": "gpt-6.1-sol"},
        "research.deep": {"reasoning_effort": "medium"}}))
    captured = resolve_purpose_config("research.deep", config)
    assert captured.deployment_pro == captured.deployment_flash == "sol-global-deployment"
    assert (captured.catalog_model, captured.reasoning_effort, captured.api_mode) == (
        "gpt-6.1-sol", "medium", "responses")
    assert (captured.provider, captured.base_url, captured.api_key) == (
        config.provider, config.base_url, config.api_key)
    assert config.reasoning_effort == "high"


def test_sol_uses_low_for_short_calls_instead_of_legacy_high(config, monkeypatch):
    monkeypatch.setenv("MODEL_ROUTING_OVERRIDES", '{"*":{"model":"gpt-6.1-sol"}}')
    assert resolve_purpose_config("intake.classify", config).reasoning_effort == "low"


def test_exact_model_rollback_resets_inherited_sol_catalog(config, monkeypatch):
    monkeypatch.setenv("MODEL_ROUTING_OVERRIDES", json.dumps({
        "*": {"model": "sol-deploy", "catalog_model": "gpt-6.1-sol"},
        "research.deep": {"model": "DeepSeek-V4-Pro"}}))
    captured = resolve_purpose_config("research.deep", config)
    assert (captured.deployment_pro, captured.catalog_model, captured.api_mode) == (
        "DeepSeek-V4-Pro", "DeepSeek-V4-Pro", "chat_completions")


@pytest.mark.parametrize("overrides", [
    "[]", "null", "not-json", '{"*":"gpt-6.1-sol"}', '{"*":{"model":""}}',
    '{"*":{"provider":"openai"}}', '{"*":{"base_url":"https://example.com"}}',
    '{"*":{"api_key":"offline-test"}}', '{"*":{"model":6}}',
    '{"*":{"reasoning_effort":"none"}}', '{"*":{"reasoning_effort":"minimal"}}',
])
def test_invalid_operator_map_is_rejected_before_a_provider_can_be_used(config, monkeypatch, overrides):
    monkeypatch.setenv("MODEL_ROUTING_OVERRIDES", overrides)
    expected = TypeError if overrides in {"[]", "null"} else ValueError
    with pytest.raises(expected):
        resolve_purpose_config("research.deep", config)


def test_unused_unknown_override_purpose_is_also_rejected(config, monkeypatch):
    monkeypatch.setenv("MODEL_ROUTING_OVERRIDES", '{"not.registered":{"model":"gpt-6.1-sol"}}')
    with pytest.raises(UnroutedPurposeError):
        resolve_purpose_config("research.deep", config)


def test_explicit_tier_cannot_bypass_unknown_purpose(config):
    with pytest.raises(UnroutedPurposeError):
        resolve_purpose_config("not.registered", config, tier="pro")


def test_legacy_priced_pro_can_be_captured_for_a_declared_flash_purpose(config):
    captured = resolve_purpose_config("venture.chat.route", config, tier="pro")
    assert captured.captured_tier == "pro"
    assert captured.deployment_pro == captured.deployment_flash == "deepseek-pro"


def test_admitted_route_does_not_reread_operator_map(config, monkeypatch):
    monkeypatch.setenv("MODEL_ROUTING_OVERRIDES", '{"*":{"model":"gpt-6.1-sol"}}')
    captured = resolve_purpose_config("venture.chat.route", config, tier="pro")
    monkeypatch.setenv("MODEL_ROUTING_OVERRIDES", "invalid changed configuration")
    replay = resolve_purpose_config("venture.chat.route", captured)
    assert replay == captured and replay is not captured
    with pytest.raises(ValueError):
        resolve_purpose_config("research.deep", captured)
    with pytest.raises(ValueError):
        resolve_purpose_config("venture.chat.route", captured, tier="flash")


def test_byok_ignores_platform_overrides_and_keeps_own_models(config, monkeypatch):
    monkeypatch.setenv("MODEL_ROUTING_OVERRIDES", '{"*":{"api_key":"not allowed"}}')
    config.source = "founder_override"
    config.deployment_pro = "own-pro"
    config.deployment_flash = "own-flash"
    captured = resolve_purpose_config("research.deep", config)
    assert (captured.deployment_pro, captured.deployment_flash, captured.catalog_model) == (
        "own-pro", "own-flash", "own-pro")
    assert captured.api_key == config.api_key
    assert captured.routing_purpose == "research.deep"


def test_byok_sol_selects_responses_and_portable_effort(config):
    config.source = "founder_override"
    config.deployment_pro = "gpt-6.1-sol"
    captured = resolve_purpose_config("research.deep", config)
    assert (captured.api_mode, captured.reasoning_effort) == ("responses", "low")


@pytest.mark.parametrize("purpose,tier,model", [
    ("memory.embed", "embed", "text-embedding-3-small"), ("guard.injection", "local", "local"),
])
def test_reasoning_cutover_preserves_capability_routes(config, monkeypatch, purpose, tier, model):
    monkeypatch.setenv("MODEL_ROUTING_OVERRIDES", '{"*":{"model":"gpt-6.1-sol"}}')
    captured = resolve_purpose_config(purpose, config)
    assert (captured.captured_tier, captured.catalog_model) == (tier, model)
    assert captured.deployment_pro == config.deployment_pro
    assert captured.deployment_embed == config.deployment_embed
    assert captured.api_mode is None


@pytest.mark.parametrize("model,catalog,expected", [
    ("gpt-6.1-sol", None, True), ("gpt-6.1-sol-2026-09-29", None, True),
    ("azure-deployment", "gpt-6.1-sol", True), ("gpt-6-sol", None, False),
    ("gpt-5.6-sol", None, False), ("gpt-6.1-something", None, False),
])
def test_sol_recognition_uses_exact_catalog_family(model, catalog, expected):
    assert is_sol_model(model, catalog) is expected


def test_documented_ops_family_remains_supported(config):
    assert resolve_purpose_config("ops.brain_cycle", config).captured_tier == "flash"


@pytest.mark.parametrize("purpose", [
    "pagebuilder.brief_direction_plan", "pagebuilder.plan_repair_1", "pagebuilder.plan_repair_4",
    "pagebuilder.capability_review_1", "pagebuilder.capability_review_6", "pagebuilder.code",
    "pagebuilder.repair_1", "pagebuilder.repair_2", "device.ai_step.plan", "device.ai_step.verify",
])
def test_real_dynamic_reasoning_stages_are_explicitly_registered(config, purpose):
    assert resolve_purpose_config(purpose, config).captured_tier == "pro"


@pytest.mark.parametrize("purpose", ["pagebuilder.unknown_stage", "device.ai_step.unknown_kind"])
def test_new_dynamic_stage_does_not_inherit_a_family_fallback(config, purpose):
    with pytest.raises(UnroutedPurposeError):
        resolve_purpose_config(purpose, config, tier="pro")

@pytest.mark.parametrize('model,catalog', [('gpt-6.1-sol', 'gpt-6-luna'), ('DeepSeek-V4-Pro', 'gpt-6.1-sol'), ('gpt-6-luna', 'gpt-6.1-sol')])
def test_known_wire_model_cannot_pick_an_unrelated_pricing_catalog(config, monkeypatch, model, catalog):
    import json
    monkeypatch.setenv('MODEL_ROUTING_OVERRIDES', json.dumps({'research.deep': {'model': model, 'catalog_model': catalog}}))
    with pytest.raises(ValueError, match='different pricing catalog'):
        resolve_purpose_config('research.deep', config)

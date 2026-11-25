"""Pure native money evidence: immutable rates, strict provenance, no effects."""
from copy import deepcopy
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from tests.company_os_harness import load_company_os_app

DIGEST = "a" * 64


@pytest.fixture()
def helpers(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/unused.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SUPYMEM_CU_PRIORITY_MULTIPLIER", "2")
    load_company_os_app()
    from app.credits import provider_receipts
    from app import state
    assert Path(provider_receipts.__file__).resolve() == Path(__file__).resolve().parents[3] / "services/company_os/app/credits/provider_receipts.py"
    monkeypatch.setattr(state, "db", lambda *a, **k: pytest.fail("money helper accessed DB"))
    return provider_receipts


def config():
    return {"models": {
        "captured-model": {"input_per_mtok": "2.00", "cached_input_per_mtok": "0.10",
                           "output_per_mtok": "10", "cache_write_per_mtok": "900"},
        "other-model": {"input_per_mtok": "4", "cached_input_per_mtok": "0.2", "output_per_mtok": "15"},
        "local": {"input_per_mtok": 0, "output_per_mtok": 0},
        "default": {"input_per_mtok": 1, "output_per_mtok": 3}},
        "aliases": {"captured-model-2026-09-29": "captured-model", "llama3.2": "local"}}


def evidence(helpers, monkeypatch, *, table=None):
    from app.llm import pricing
    monkeypatch.setattr(pricing, "load_pricing", lambda: table if table is not None else config())
    snapshot = helpers.capture_pricing_snapshot("captured-model")
    binding = {"schema_version": 1, "purpose": "task.plan", "provider": "azure_openai",
        "deployment": "azure-deployment", "catalog_model": "captured-model", "api_mode": "responses",
        "endpoint_host": "synthetic.openai.azure.com", "input_token_limit": 10000,
        "max_output_tokens": 1000, "requested_service_tier": "default", "pricing_snapshot": snapshot}
    role = {key: binding["deployment"] if key == "model" else binding[key]
            for key in ("purpose", "provider", "model", "catalog_model", "api_mode", "max_output_tokens", "endpoint_host")}
    receipt = {"schema_version": 1, "authority": "proposal_only", "requires_proposal_apply": True,
        "request_digest": DIGEST, "provider": binding["provider"], "deployment": binding["deployment"],
        "catalog_model": binding["catalog_model"], "model": "captured-model", "api_mode": "responses",
        "input_tokens": 100, "output_tokens": 20, "total_tokens": 120, "cached_tokens": 80,
        "reasoning_tokens": 7, "service_tier": "default", "role_metadata": role}
    return binding, receipt


def validate(helpers, binding, receipt):
    return helpers.validate_confirmed_provider_receipt(receipt, model_binding=binding, expected_request_digest=DIGEST)


def test_normalized_decimal_snapshot_and_exact_known_charge(helpers, monkeypatch):
    binding, receipt = evidence(helpers, monkeypatch)
    assert binding["pricing_snapshot"]["models"]["captured-model"] == {
        "input_per_mtok": "2", "cached_input_per_mtok": "0.1", "output_per_mtok": "10", "cache_write_per_mtok": "900"}
    assert binding["pricing_snapshot"]["service_tiers"] == {"default": "1", "priority": "2"}
    before = deepcopy((binding, receipt))
    result = validate(helpers, binding, receipt)
    assert (binding, receipt) == before
    assert result["provider_actual_micro"] == 248 and result["cost_usd"] == "0.000248"
    assert result["model"] == result["actual_catalog_model"] == "captured-model"
    assert result["deployment"] == "azure-deployment" and result["catalog_model"] == "captured-model"
    assert result["reasoning_tokens"] == 7 and result["output_tokens"] == 20
    assert result["output_usable"] is True and result["output_errors"] == []
    assert result["cache_write_cost_applied"] is False


def test_live_catalog_alias_and_tier_flip_cannot_reprice(helpers, monkeypatch):
    table = config()
    binding, receipt = evidence(helpers, monkeypatch, table=table)
    receipt.update(model="captured-model-2026-09-29", service_tier="priority")
    binding["requested_service_tier"] = "priority"
    table["models"]["captured-model"]["input_per_mtok"] = "999"
    table["aliases"]["captured-model-2026-09-29"] = "other-model"
    table["service_tiers"] = {"priority": "9"}
    monkeypatch.setenv("SUPYMEM_CU_PRIORITY_MULTIPLIER", "9")
    from app.llm import pricing
    monkeypatch.setattr(pricing, "load_pricing", lambda: pytest.fail("receipt read live catalog"))
    result = validate(helpers, binding, receipt)
    assert result["provider_actual_micro"] == 496 and result["actual_catalog_model"] == "captured-model"
    assert result["service_tier"] == "priority" and result["output_usable"] is True


def test_recognized_model_mismatch_charges_actual_captured_row(helpers, monkeypatch):
    binding, receipt = evidence(helpers, monkeypatch)
    receipt["model"] = "other-model"
    result = validate(helpers, binding, receipt)
    assert result["provider_actual_micro"] == 396
    assert result["model"] == result["actual_catalog_model"] == "other-model"
    assert result["catalog_model"] == "captured-model" and result["deployment"] == "azure-deployment"
    assert result["output_usable"] is False and result["output_errors"] == ["model_mismatch"]


@pytest.mark.parametrize("actual", ["captured-model-2026-09-30", "azure-deployment", "unlisted-model", "local", "llama3.2", "default"])
def test_unknown_and_local_models_never_get_fallback_or_free_rate(helpers, monkeypatch, actual):
    binding, receipt = evidence(helpers, monkeypatch)
    receipt["model"] = actual
    with pytest.raises(helpers.CreditsUnavailable, match="provider_receipt_model_unknown"):
        validate(helpers, binding, receipt)


def test_full_input_long_context_threshold_and_cache_subset(helpers, monkeypatch):
    table = config()
    table["models"]["captured-model"].update(long_context_threshold_tokens=1000,
        long_context_input_per_mtok="4.00", long_context_output_per_mtok="15", long_context_cached_input_per_mtok="0.2")
    binding, receipt = evidence(helpers, monkeypatch, table=table)
    receipt.update(input_tokens=1000, output_tokens=100, total_tokens=1100, cached_tokens=500)
    result = validate(helpers, binding, receipt)
    assert result["provider_actual_micro"] == 2050 and result["long_context"] is False
    receipt.update(input_tokens=1001, total_tokens=1101)
    result = validate(helpers, binding, receipt)
    assert result["provider_actual_micro"] == 3604 and result["long_context"] is True
    del binding["pricing_snapshot"]["models"]["captured-model"]["long_cached_input_per_mtok"]
    assert validate(helpers, binding, receipt)["provider_actual_micro"] == 5504


def test_no_cache_rate_or_write_quantity_is_invented(helpers, monkeypatch):
    table = config()
    del table["models"]["captured-model"]["cached_input_per_mtok"]
    binding, receipt = evidence(helpers, monkeypatch, table=table)
    assert validate(helpers, binding, receipt)["provider_actual_micro"] == 400
    receipt["cache_write_tokens"] = 80
    with pytest.raises(helpers.CreditsUnavailable, match="provider_receipt_dto_unknown"):
        validate(helpers, binding, receipt)


@pytest.mark.parametrize("requested,actual,cost,usable,reported", [
    ("default", None, 248, True, False), ("default", "default", 248, True, True),
    ("priority", "priority", 496, True, True), ("default", "priority", 496, False, True),
    ("priority", "default", 248, False, True), (None, "default", 248, False, True)])
def test_tiers_preserve_actual_and_known_mismatch_cost(helpers, monkeypatch, requested, actual, cost, usable, reported):
    binding, receipt = evidence(helpers, monkeypatch)
    binding["requested_service_tier"] = requested
    receipt["service_tier"] = actual
    result = validate(helpers, binding, receipt)
    assert result["service_tier"] is actual and result["service_tier_reported"] is reported
    assert result["provider_actual_micro"] == cost and result["output_usable"] is usable
    assert result["output_errors"] == ([] if usable else ["service_tier_mismatch"])


@pytest.mark.parametrize("requested,actual", [(None, None), ("priority", None), ("default", "unknown"), ("priority", True), (True, "default")])
def test_unknown_tier_never_becomes_returned_default(helpers, monkeypatch, requested, actual):
    binding, receipt = evidence(helpers, monkeypatch)
    binding["requested_service_tier"], receipt["service_tier"] = requested, actual
    with pytest.raises(helpers.CreditsUnavailable, match="provider_receipt_tier_unknown"):
        validate(helpers, binding, receipt)


def test_known_overage_preserves_confirmed_counts_cost_and_all_errors(helpers, monkeypatch):
    binding, receipt = evidence(helpers, monkeypatch)
    binding.update(input_token_limit=50, max_output_tokens=10)
    receipt["role_metadata"]["max_output_tokens"] = 10
    result = validate(helpers, binding, receipt)
    assert result["input_tokens"] == 100 and result["output_tokens"] == 20 and result["total_tokens"] == 120
    assert result["provider_actual_micro"] == 248 and result["output_usable"] is False
    assert result["output_errors"] == ["input_limit_exceeded", "output_limit_exceeded"]


@pytest.mark.parametrize("key,value", [("input_tokens", True), ("output_tokens", "20"), ("cached_tokens", Decimal(80)),
    ("reasoning_tokens", -1), ("total_tokens", 121), ("cached_tokens", 101), ("reasoning_tokens", 21),
    ("input_tokens", 2**63), ("output_tokens", float("nan")), ("total_tokens", None)])
def test_malformed_or_inconsistent_counts_stay_unknown(helpers, monkeypatch, key, value):
    binding, receipt = evidence(helpers, monkeypatch)
    receipt[key] = value
    with pytest.raises(helpers.CreditsUnavailable, match="provider_receipt_counts_unknown"):
        validate(helpers, binding, receipt)


@pytest.mark.parametrize("key,value", [("purpose", "task.other"), ("provider", "other"), ("model", "other-deployment"),
    ("catalog_model", "other-model"), ("api_mode", "chat_completions"), ("max_output_tokens", 999),
    ("endpoint_host", "other.example.com"), ("max_output_tokens", True)])
def test_each_captured_role_binding_must_match(helpers, monkeypatch, key, value):
    binding, receipt = evidence(helpers, monkeypatch)
    receipt["role_metadata"][key] = value
    with pytest.raises(helpers.CreditsUnavailable, match="provider_receipt_binding_unknown"):
        validate(helpers, binding, receipt)


@pytest.mark.parametrize("key,value", [("provider", "other"), ("deployment", "other"),
    ("catalog_model", "other-model"), ("api_mode", "chat_completions"), ("request_digest", "b" * 64)])
def test_receipt_top_binding_and_digest_refuse_tampering(helpers, monkeypatch, key, value):
    binding, receipt = evidence(helpers, monkeypatch)
    receipt[key] = value
    with pytest.raises(helpers.CreditsUnavailable, match="provider_receipt_binding_unknown"):
        validate(helpers, binding, receipt)


@pytest.mark.parametrize("host", ["https://synthetic.openai.azure.com", "host.example:443", "user@host.example", "UPPER.example", "host/path", "-bad.example"])
def test_endpoint_is_captured_host_without_url_credentials_or_port(helpers, monkeypatch, host):
    binding, receipt = evidence(helpers, monkeypatch)
    binding["endpoint_host"] = receipt["role_metadata"]["endpoint_host"] = host
    with pytest.raises(helpers.CreditsUnavailable, match="provider_receipt_binding_unknown"):
        validate(helpers, binding, receipt)


@pytest.mark.parametrize("authority", ["proposal_only", "observation_only"])
def test_authority_label_is_preserved_without_permission_or_payload(helpers, monkeypatch, authority):
    binding, receipt = evidence(helpers, monkeypatch)
    opaque = object()
    receipt.update(authority=authority, result=opaque, error_code=opaque)
    result = validate(helpers, binding, receipt)
    assert result["authority"] == authority and result["requires_proposal_apply"] is True
    assert not {"result", "error_code", "role_metadata", "pricing_snapshot", "permission", "controller_flags"} & result.keys()
    assert all(value is not opaque for value in result.values())


@pytest.mark.parametrize("change", [{"authority": "execute"}, {"authority": "native_server_confirmed"},
    {"authority": True}, {"requires_proposal_apply": 1}, {"controller_flags": {}}, {"permission": True},
    {"api_key": "private"}, {"sdk_response": object()}, {"image": "private"}, {"schema_version": True}])
def test_nonfinal_authority_raw_payload_and_extra_fields_refuse(helpers, monkeypatch, change):
    binding, receipt = evidence(helpers, monkeypatch)
    receipt.update(change)
    with pytest.raises(helpers.CreditsUnavailable):
        validate(helpers, binding, receipt)


def test_old_three_key_usage_cannot_be_manufactured_into_rich_receipt(helpers, monkeypatch):
    binding, _ = evidence(helpers, monkeypatch)
    with pytest.raises(helpers.CreditsUnavailable, match="provider_receipt_dto_unknown"):
        validate(helpers, binding, {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120})


@pytest.mark.parametrize("rate", [True, -1, "NaN", "Infinity", "-Infinity", float("nan"), float("inf"),
    "1e-19", "1000001", "invalid", None])
def test_malformed_nonfinite_negative_or_unbounded_rates_fail_capture(helpers, monkeypatch, rate):
    from app.llm import pricing
    table = config()
    table["models"]["captured-model"]["input_per_mtok"] = rate
    monkeypatch.setattr(pricing, "load_pricing", lambda: table)
    with pytest.raises(helpers.CreditsUnavailable, match="provider_receipt_pricing_unknown"):
        helpers.capture_pricing_snapshot("captured-model")


@pytest.mark.parametrize("priority", ["NaN", "Infinity", "-1", "0", "11", "bad"])
def test_priority_environment_malformed_value_cannot_silently_fallback(helpers, monkeypatch, priority):
    from app.llm import pricing
    monkeypatch.setattr(pricing, "load_pricing", config)
    monkeypatch.setenv("SUPYMEM_CU_PRIORITY_MULTIPLIER", priority)
    with pytest.raises(helpers.CreditsUnavailable, match="provider_receipt_pricing_unknown"):
        helpers.capture_pricing_snapshot("captured-model")


@pytest.mark.parametrize("change", [
    {"cached_input_per_mtok": "-1"}, {"long_context_threshold": True},
    {"long_input_per_mtok": "4"}, {"unknown_rate": "1"},
    {"long_context_threshold": 100, "long_context_threshold_tokens": 101},
    {"long_context_threshold": 100, "long_input_per_mtok": "4", "long_context_input_per_mtok": "5"}])
def test_partial_or_conflicting_context_and_unknown_fields_fail_capture(helpers, monkeypatch, change):
    from app.llm import pricing
    table = config()
    table["models"]["captured-model"].update(change)
    monkeypatch.setattr(pricing, "load_pricing", lambda: table)
    with pytest.raises(helpers.CreditsUnavailable):
        helpers.capture_pricing_snapshot("captured-model")


def test_snapshot_rate_type_and_known_alias_target_are_strict(helpers, monkeypatch):
    binding, receipt = evidence(helpers, monkeypatch)
    binding["pricing_snapshot"]["models"]["captured-model"]["input_per_mtok"] = 2
    with pytest.raises(helpers.CreditsUnavailable, match="provider_receipt_pricing_unknown"):
        validate(helpers, binding, receipt)
    binding, receipt = evidence(helpers, monkeypatch)
    binding["pricing_snapshot"]["aliases"]["captured-model-2026-09-29"] = "unlisted-model"
    with pytest.raises(helpers.CreditsUnavailable, match="provider_receipt_pricing_unknown"):
        validate(helpers, binding, receipt)


def test_bounded_snapshot_and_no_paid_local_capture(helpers, monkeypatch):
    from app.llm import pricing
    table = config()
    monkeypatch.setattr(pricing, "load_pricing", lambda: table)
    snapshot = helpers.capture_pricing_snapshot("captured-model")
    assert "local" not in snapshot["models"] and "default" not in snapshot["models"]
    assert "llama3.2" not in snapshot["aliases"]
    for local in ("local", "llama3.2", "default"):
        with pytest.raises(helpers.CreditsUnavailable, match="provider_receipt_model_unknown"):
            helpers.capture_pricing_snapshot(local)
    table["models"] = {f"m{i}": {"input_per_mtok": "1", "output_per_mtok": "1"} for i in range(65)}
    with pytest.raises(helpers.CreditsUnavailable, match="provider_receipt_pricing_unknown"):
        helpers.capture_pricing_snapshot("m0")


def test_configured_tier_multipliers_are_copied_and_not_overridden_by_environment(helpers, monkeypatch):
    table = config()
    table["service_tiers"] = {"default": Decimal("1.000"), "priority": "2.5", "flex": "0.5"}
    binding, receipt = evidence(helpers, monkeypatch, table=table)
    receipt["service_tier"] = binding["requested_service_tier"] = "flex"
    assert binding["pricing_snapshot"]["service_tiers"] == {"default": "1", "priority": "2.5", "flex": "0.5"}
    assert validate(helpers, binding, receipt)["provider_actual_micro"] == 124


def test_decimal_negative_zero_normalizes_without_changing_known_paid_cost(helpers, monkeypatch):
    table = config()
    table["models"]["captured-model"]["cached_input_per_mtok"] = "-0.000"
    binding, receipt = evidence(helpers, monkeypatch, table=table)
    assert binding["pricing_snapshot"]["models"]["captured-model"]["cached_input_per_mtok"] == "0"
    assert validate(helpers, binding, receipt)["provider_actual_micro"] == 240


def test_real_config_capture_uses_explicit_rows_without_missing_dated_alias_invention(helpers, monkeypatch):
    import yaml
    from app.llm import pricing
    root = Path(__file__).resolve().parents[3]
    table = yaml.safe_load((root / "config/llm_pricing.yaml").read_text())
    monkeypatch.setattr(pricing, "load_pricing", lambda: table)
    snapshot = helpers.capture_pricing_snapshot("gpt-6.1-sol")
    assert snapshot["models"]["gpt-6.1-sol"]["input_per_mtok"] == "2"
    configured = table["models"]["gpt-6.1-sol"]
    long_cached = configured.get("long_cached_input_per_mtok", configured.get("long_context_cached_input_per_mtok"))
    if long_cached is None:
        assert "long_cached_input_per_mtok" not in snapshot["models"]["gpt-6.1-sol"]
    else:
        assert Decimal(snapshot["models"]["gpt-6.1-sol"]["long_cached_input_per_mtok"]) == Decimal(str(long_cached))
    assert snapshot["aliases"]["gpt-5.6-luna"] == "luna"
    assert snapshot["aliases"] == {alias: target for alias, target in table.get("aliases", {}).items()
        if target in snapshot["models"] and alias.casefold() not in {"local", "default"}}


def test_zero_counts_and_tiny_confirmed_cost_do_not_inherit_reserve_floor(helpers, monkeypatch):
    binding, receipt = evidence(helpers, monkeypatch)
    receipt.update(input_tokens=0, output_tokens=0, total_tokens=0, cached_tokens=0, reasoning_tokens=0)
    assert validate(helpers, binding, receipt)["provider_actual_micro"] == 0
    binding["pricing_snapshot"]["models"]["captured-model"]["input_per_mtok"] = "0.042"
    receipt.update(input_tokens=1, total_tokens=1)
    result = validate(helpers, binding, receipt)
    assert result["cost_usd"] == "0.000000042" and result["provider_actual_micro"] == 0


class EqualToAnything:
    def __eq__(self, other):
        return True


@pytest.mark.parametrize("section,key", [("receipt", "request_digest"), ("receipt", "provider"),
    ("receipt", "deployment"), ("role", "purpose"), ("role", "endpoint_host"),
    ("snapshot", "currency"), ("snapshot", "source")])
def test_non_json_objects_cannot_compare_equal_to_verified_evidence(helpers, monkeypatch, section, key):
    binding, receipt = evidence(helpers, monkeypatch)
    target = {"receipt": receipt, "role": receipt["role_metadata"], "snapshot": binding["pricing_snapshot"]}[section]
    target[key] = EqualToAnything()
    with pytest.raises(helpers.CreditsUnavailable):
        validate(helpers, binding, receipt)


@pytest.mark.parametrize("section,key", [("receipt", "requires_proposal_apply"), ("receipt", "role_metadata"),
    ("role", "catalog_model"), ("binding", "purpose"), ("binding", "endpoint_host")])
def test_required_binding_and_final_dto_fields_cannot_be_omitted(helpers, monkeypatch, section, key):
    binding, receipt = evidence(helpers, monkeypatch)
    target = {"receipt": receipt, "role": receipt["role_metadata"], "binding": binding}[section]
    del target[key]
    with pytest.raises(helpers.CreditsUnavailable):
        validate(helpers, binding, receipt)


@pytest.mark.parametrize("kind", ["aliases", "tiers", "tier_default", "alias_chain", "alias_conflict"])
def test_unbounded_or_conflicting_config_does_not_enter_snapshot(helpers, monkeypatch, kind):
    from app.llm import pricing
    table = config()
    if kind == "aliases":
        table["aliases"] = {f"alias{i}": "captured-model" for i in range(129)}
    elif kind == "tiers":
        table["service_tiers"] = {f"tier{i}": "1" for i in range(8)}  # Adding required default/priority exceeds8.
    elif kind == "tier_default":
        table["service_tiers"] = {"default": "2"}
    elif kind == "alias_chain":
        table["aliases"]["chained"] = "captured-model-2026-09-29"
    else:
        table["aliases"]["captured-model"] = "other-model"
    monkeypatch.setattr(pricing, "load_pricing", lambda: table)
    with pytest.raises(helpers.CreditsUnavailable, match="provider_receipt_pricing_unknown"):
        helpers.capture_pricing_snapshot("captured-model")


def test_sdk_mapping_subclass_and_unsupported_api_mode_refuse(helpers, monkeypatch):
    binding, receipt = evidence(helpers, monkeypatch)
    class SDKMapping(dict):
        pass
    with pytest.raises(helpers.CreditsUnavailable, match="provider_receipt_dto_unknown"):
        validate(helpers, binding, SDKMapping(receipt))
    binding["api_mode"] = receipt["api_mode"] = receipt["role_metadata"]["api_mode"] = "images"
    with pytest.raises(helpers.CreditsUnavailable, match="provider_receipt_binding_unknown"):
        validate(helpers, binding, receipt)


def test_exact_verified_azure_deployment_alias_preserves_raw_model_and_captured_catalog(helpers, monkeypatch):
    table = config()
    table["models"]["gpt-6.1-sol"] = deepcopy(table["models"]["captured-model"])
    table["aliases"]["supymem-gpt-6-1-sol"] = "gpt-6.1-sol"
    binding, receipt = evidence(helpers, monkeypatch, table=table)
    binding.update(catalog_model="gpt-6.1-sol", deployment="supymem-gpt-6-1-sol")
    receipt.update(catalog_model="gpt-6.1-sol", deployment="supymem-gpt-6-1-sol", model="supymem-gpt-6-1-sol")
    receipt["role_metadata"].update(catalog_model="gpt-6.1-sol", model="supymem-gpt-6-1-sol")
    result = validate(helpers, binding, receipt)
    assert result["model"] == result["deployment"] == "supymem-gpt-6-1-sol"
    assert result["actual_catalog_model"] == result["catalog_model"] == "gpt-6.1-sol"
    assert result["provider_actual_micro"] == 248 and result["output_usable"] is True
    receipt["model"] = "supymem-gpt-6-1-sol-v2"
    with pytest.raises(helpers.CreditsUnavailable, match="provider_receipt_model_unknown"):
        validate(helpers, binding, receipt)
    receipt["model"] = "supymem-gpt-6-1-sol"
    del binding["pricing_snapshot"]["aliases"]["supymem-gpt-6-1-sol"]
    with pytest.raises(helpers.CreditsUnavailable, match="provider_receipt_model_unknown"):
        validate(helpers, binding, receipt)


def test_pre_http_binding_validation_returns_detached_original_without_live_pricing(helpers, monkeypatch):
    binding, _ = evidence(helpers, monkeypatch)
    before = deepcopy(binding)
    from app.llm import pricing
    monkeypatch.setattr(pricing, "load_pricing", lambda: pytest.fail("binding admission read live prices"))
    monkeypatch.setenv("SUPYMEM_CU_PRIORITY_MULTIPLIER", "invalid-after-capture")
    detached = helpers.validate_model_binding(binding)
    assert detached == before and detached is not binding
    detached["pricing_snapshot"]["models"]["captured-model"]["input_per_mtok"] = "999"
    detached["pricing_snapshot"]["aliases"].clear()
    detached["pricing_snapshot"]["service_tiers"]["priority"] = "9"
    assert binding == before
    binding["pricing_snapshot"]["models"]["other-model"]["output_per_mtok"] = "1"
    assert detached["pricing_snapshot"]["models"]["other-model"]["output_per_mtok"] == "15"


@pytest.mark.parametrize("field,value", [("endpoint_host", "https://secret@host.example"),
    ("requested_service_tier", True), ("requested_service_tier", "uncaptured"), ("catalog_model", "local"),
    ("catalog_model", "captured-model-unknown-date"), ("input_token_limit", 0), ("input_token_limit", True),
    ("input_token_limit", 2**63), ("max_output_tokens", -1), ("max_output_tokens", "1000"),
    ("api_mode", "images"), ("purpose", None), ("schema_version", True)])
def test_pre_http_invalid_capture_refuses_before_any_receipt_or_provider(helpers, monkeypatch, field, value):
    binding, _ = evidence(helpers, monkeypatch)
    binding[field] = value
    with pytest.raises(helpers.CreditsUnavailable):
        helpers.validate_model_binding(binding)


@pytest.mark.parametrize("section,key,value", [("models", "input_per_mtok", "NaN"),
    ("models", "input_per_mtok", 2), ("service_tiers", "priority", "0"),
    ("aliases", "new-alias", "unknown-target")])
def test_pre_http_malformed_snapshot_refuses_before_receipt(helpers, monkeypatch, section, key, value):
    binding, _ = evidence(helpers, monkeypatch)
    target = binding["pricing_snapshot"][section]
    if section == "models":
        target = target["captured-model"]
    target[key] = value
    with pytest.raises(helpers.CreditsUnavailable, match="provider_receipt_pricing_unknown"):
        helpers.validate_model_binding(binding)


def test_pre_http_nullable_requested_tier_is_preserved_without_becoming_explicit_default(helpers, monkeypatch):
    binding, receipt = evidence(helpers, monkeypatch)
    binding["requested_service_tier"] = None
    detached = helpers.validate_model_binding(binding)
    assert detached["requested_service_tier"] is None
    receipt["service_tier"] = None
    with pytest.raises(helpers.CreditsUnavailable, match="provider_receipt_tier_unknown"):
        validate(helpers, detached, receipt)


@pytest.mark.parametrize("field", ["purpose", "provider", "deployment", "catalog_model"])
@pytest.mark.parametrize("unsafe", ["https://host.example/private", "sk-proj-syntheticCredential12345", "api_key:syntheticCredential12345"])
def test_mirrored_url_or_credential_shaped_binding_names_never_become_evidence(helpers, monkeypatch, field, unsafe):
    binding, receipt = evidence(helpers, monkeypatch)
    binding[field] = unsafe
    role_field = "model" if field == "deployment" else field
    receipt["role_metadata"][role_field] = unsafe
    if field in receipt:
        receipt[field] = unsafe
    if field == "catalog_model":
        binding["pricing_snapshot"]["models"][unsafe] = deepcopy(binding["pricing_snapshot"]["models"]["captured-model"])
        receipt["model"] = unsafe
    with pytest.raises(helpers.CreditsUnavailable, match="provider_receipt_binding_unknown"):
        helpers.validate_model_binding(binding)
    with pytest.raises(helpers.CreditsUnavailable, match="provider_receipt_binding_unknown"):
        validate(helpers, binding, receipt)


def test_known_actual_model_cannot_smuggle_credential_as_catalog_name(helpers, monkeypatch):
    binding, receipt = evidence(helpers, monkeypatch)
    unsafe = "sk-proj-syntheticCredential12345"
    binding["pricing_snapshot"]["models"][unsafe] = deepcopy(binding["pricing_snapshot"]["models"]["captured-model"])
    receipt["model"] = unsafe
    with pytest.raises(helpers.CreditsUnavailable, match="provider_receipt_pricing_unknown"):
        validate(helpers, binding, receipt)


@pytest.mark.parametrize("route", ["capture", "admission", "receipt"])
def test_positive_short_rates_with_zero_effective_long_base_are_unknown(helpers, monkeypatch, route):
    table = config()
    binding, receipt = evidence(helpers, monkeypatch, table=table)
    long_free = {"long_context_threshold": 50, "long_input_per_mtok": "0", "long_output_per_mtok": "0"}
    if route == "capture":
        table["models"]["captured-model"].update(long_free)
        def operation():
            return helpers.capture_pricing_snapshot("captured-model")
    else:
        binding["pricing_snapshot"]["models"]["captured-model"].update(long_free)
        receipt["cached_tokens"] = 0  # Uncached confirmed paid traffic must not become free.
        def operation():
            return helpers.validate_model_binding(binding) if route == "admission" else validate(helpers, binding, receipt)
    with pytest.raises(helpers.CreditsUnavailable, match="provider_receipt_pricing_unknown"):
        operation()


@pytest.mark.parametrize("reserved", ["local", "Local", "LOCAL", "default", "Default", "DEFAULT"])
def test_reserved_rows_or_aliases_cannot_become_paid_provider_identity(helpers, monkeypatch, reserved):
    table = config()
    table["models"][reserved] = deepcopy(table["models"]["captured-model"])
    table["aliases"][reserved] = "captured-model"
    binding, receipt = evidence(helpers, monkeypatch, table=table)
    assert reserved not in binding["pricing_snapshot"]["models"]
    assert reserved not in binding["pricing_snapshot"]["aliases"]
    receipt["model"] = reserved
    with pytest.raises(helpers.CreditsUnavailable, match="provider_receipt_model_unknown"):
        validate(helpers, binding, receipt)
    with pytest.raises(helpers.CreditsUnavailable, match="provider_receipt_model_unknown"):
        helpers.capture_pricing_snapshot(reserved)
    binding["pricing_snapshot"]["aliases"][reserved] = "captured-model"
    with pytest.raises(helpers.CreditsUnavailable, match="provider_receipt_pricing_unknown"):
        validate(helpers, binding, receipt)


def test_captured_alias_to_case_variant_reserved_target_is_refused(helpers, monkeypatch):
    binding, receipt = evidence(helpers, monkeypatch)
    binding["pricing_snapshot"]["models"]["Local"] = deepcopy(binding["pricing_snapshot"]["models"]["captured-model"])
    binding["pricing_snapshot"]["aliases"]["paid-looking"] = "Local"
    receipt["model"] = "paid-looking"
    with pytest.raises(helpers.CreditsUnavailable, match="provider_receipt_pricing_unknown"):
        validate(helpers, binding, receipt)


def test_dns_valid_secret_shaped_host_never_survives_detached_admission_binding(helpers, monkeypatch):
    binding, receipt = evidence(helpers, monkeypatch)
    binding["endpoint_host"] = receipt["role_metadata"]["endpoint_host"] = "sk-proj-syntheticcredential12345.example.com"
    with pytest.raises(helpers.CreditsUnavailable, match="provider_receipt_binding_unknown"):
        helpers.validate_model_binding(binding)


@pytest.mark.parametrize("route", ["capture", "receipt"])
def test_known_tier_label_cannot_smuggle_credential_into_money_evidence(helpers, monkeypatch, route):
    table = config()
    binding, receipt = evidence(helpers, monkeypatch, table=table)
    unsafe = "sk-proj-123456789123"
    if route == "capture":
        table["service_tiers"] = {unsafe: "1"}
        def operation():
            return helpers.capture_pricing_snapshot("captured-model")
    else:
        binding["pricing_snapshot"]["service_tiers"][unsafe] = "1"
        binding["requested_service_tier"] = receipt["service_tier"] = unsafe
        def operation():
            return validate(helpers, binding, receipt)
    with pytest.raises(helpers.CreditsUnavailable, match="provider_receipt_pricing_unknown"):
        operation()


def test_detached_original_does_not_copy_sdk_objects_hidden_in_dict_keys(helpers, monkeypatch):
    binding, _ = evidence(helpers, monkeypatch)
    class SDKKey(str):
        def __deepcopy__(self, memo):
            pytest.fail("SDK object copy ran during pure admission")
    purpose = binding.pop("purpose")
    binding[SDKKey("purpose")] = purpose
    with pytest.raises(helpers.CreditsUnavailable, match="provider_receipt_binding_unknown"):
        helpers.validate_model_binding(binding)


def quote_binding(helpers, monkeypatch, *, table=None, inputs=100, outputs=20, tier="default"):
    binding, _ = evidence(helpers, monkeypatch, table=table)
    binding.update(input_token_limit=inputs, max_output_tokens=outputs, requested_service_tier=tier)
    return binding


def test_quote_exact_short_bound_without_cache_discount_or_cache_write_invention(helpers, monkeypatch):
    binding = quote_binding(helpers, monkeypatch)
    original = deepcopy(binding)
    monkeypatch.setattr(helpers, "validate_confirmed_provider_receipt", lambda *a, **k: pytest.fail("quote made a confirmed receipt"))
    assert helpers.estimate_model_binding_micro(binding) == 400
    assert binding == original


@pytest.mark.parametrize("inputs,expected", [(1000, 3000), (1001, 5504)])
def test_quote_long_context_boundary_prices_full_admissible_input(helpers, monkeypatch, inputs, expected):
    table = config()
    table["models"]["captured-model"].update(long_context_threshold=1000, long_input_per_mtok="4",
        long_output_per_mtok="15", long_cached_input_per_mtok="0.2")
    binding = quote_binding(helpers, monkeypatch, table=table, inputs=inputs, outputs=100)
    assert helpers.estimate_model_binding_micro(binding) == expected


def test_quote_covers_expensive_short_domain_when_long_prices_are_lower(helpers, monkeypatch):
    table = config()
    table["models"]["captured-model"].update(long_context_threshold=1000, long_input_per_mtok="0.01",
        long_output_per_mtok="0.01", long_cached_input_per_mtok="0.005")
    binding = quote_binding(helpers, monkeypatch, table=table, inputs=2000, outputs=100)
    assert helpers.estimate_model_binding_micro(binding) == 3000  # Short1000, not cheaper long2000.


def test_quote_covers_output_only_short_context_at_zero_threshold(helpers, monkeypatch):
    table = config()
    table["models"]["captured-model"].update(output_per_mtok="1000", long_context_threshold=0,
        long_input_per_mtok="0.01", long_output_per_mtok="1")
    binding = quote_binding(helpers, monkeypatch, table=table, inputs=100, outputs=100)
    assert helpers.estimate_model_binding_micro(binding) == 100000


@pytest.mark.parametrize("long,expected", [(False, 700), (True, 8507)])
def test_quote_bounds_cached_rate_when_it_exceeds_full_input(helpers, monkeypatch, long, expected):
    table = config()
    table["models"]["captured-model"]["cached_input_per_mtok"] = "5"
    if long:
        table["models"]["captured-model"].update(long_context_threshold=1000,
            long_input_per_mtok="4", long_output_per_mtok="15", long_cached_input_per_mtok="7")
    binding = quote_binding(helpers, monkeypatch, table=table, inputs=1001 if long else 100, outputs=100 if long else 20)
    assert helpers.estimate_model_binding_micro(binding) == expected


def test_quote_missing_long_cache_uses_full_long_input_not_short_cache_rate(helpers, monkeypatch):
    table = config()
    table["models"]["captured-model"].update(cached_input_per_mtok="5", long_context_threshold=1000,
        long_input_per_mtok="4", long_output_per_mtok="15")
    binding = quote_binding(helpers, monkeypatch, table=table, inputs=1001, outputs=100)
    assert helpers.estimate_model_binding_micro(binding) == 6000  # Max(short6000,long5504).


def test_quote_ceil_rounding_and_reserve_floor_do_not_change_actual_half_up(helpers, monkeypatch):
    table = config()
    table["models"]["captured-model"].update(input_per_mtok="33.35", output_per_mtok="0", cached_input_per_mtok="0")
    binding = quote_binding(helpers, monkeypatch, table=table, inputs=3, outputs=1)
    assert helpers.estimate_model_binding_micro(binding) == 101  # 100.05 rounds UP.
    binding["pricing_snapshot"]["models"]["captured-model"]["input_per_mtok"] = "0.042"
    assert helpers.estimate_model_binding_micro(binding) == helpers.MIN_RESERVE_MICRO == 100


def test_quote_uses_only_explicit_captured_alias_and_requested_tier(helpers, monkeypatch):
    binding = quote_binding(helpers, monkeypatch, tier="priority")
    binding["catalog_model"] = "captured-model-2026-09-29"
    assert helpers.estimate_model_binding_micro(binding) == 800
    binding["catalog_model"] = "other-model"
    assert helpers.estimate_model_binding_micro(binding) == 1400  # Its own row4/15, not family equivalence.
    binding["catalog_model"] = "captured-model-2026-09-30"
    with pytest.raises(helpers.CreditsUnavailable, match="provider_receipt_model_unknown"):
        helpers.estimate_model_binding_micro(binding)


@pytest.mark.parametrize("tier", [None, "uncaptured", True])
def test_quote_unknown_or_unrequested_tier_fails_before_any_pricing_io(helpers, monkeypatch, tier):
    binding = quote_binding(helpers, monkeypatch)
    binding["requested_service_tier"] = tier
    monkeypatch.setattr(helpers, "capture_pricing_snapshot", lambda *a, **k: pytest.fail("quote reread prices"))
    monkeypatch.setattr(helpers, "validate_confirmed_provider_receipt", lambda *a, **k: pytest.fail("quote fabricated usage"))
    with pytest.raises(helpers.CreditsUnavailable, match="provider_receipt_tier_unknown"):
        helpers.estimate_model_binding_micro(binding)


def test_quote_snapshot_survives_live_model_rate_alias_and_tier_change(helpers, monkeypatch):
    table = config()
    binding = quote_binding(helpers, monkeypatch, table=table, tier="priority")
    before = deepcopy(binding)
    table["models"]["captured-model"]["input_per_mtok"] = "999"
    table["aliases"]["captured-model-2026-09-29"] = "other-model"
    monkeypatch.setenv("SUPYMEM_CU_PRIORITY_MULTIPLIER", "NaN")
    from app.llm import pricing
    monkeypatch.setattr(pricing, "load_pricing", lambda: pytest.fail("quote read live pricing"))
    monkeypatch.setattr(helpers, "capture_pricing_snapshot", lambda *a, **k: pytest.fail("quote recaptured models"))
    assert helpers.estimate_model_binding_micro(binding) == 800
    assert binding == before


@pytest.mark.parametrize("section,key,value", [("model", "input_per_mtok", "NaN"),
    ("model", "output_per_mtok", "Infinity"), ("model", "cached_input_per_mtok", "-1"),
    ("model", "input_per_mtok", 2), ("snapshot", "micro_per_usd", 100),
    ("snapshot", "currency", "EUR"), ("binding", "input_token_limit", True),
    ("binding", "max_output_tokens", 0), ("binding", "catalog_model", "local")])
def test_quote_malformed_or_mismatched_capture_never_produces_free_estimate(helpers, monkeypatch, section, key, value):
    binding = quote_binding(helpers, monkeypatch)
    target = {"binding": binding, "snapshot": binding["pricing_snapshot"],
        "model": binding["pricing_snapshot"]["models"]["captured-model"]}[section]
    target[key] = value
    with pytest.raises(helpers.CreditsUnavailable):
        helpers.estimate_model_binding_micro(binding)


def test_quote_bound_precision_preserves_exact_integer_ceiling_under_low_process_context(helpers, monkeypatch):
    table = config()
    rate = "999999.999999999999999999"
    table["models"]["captured-model"].update(input_per_mtok=rate, output_per_mtok="0", cached_input_per_mtok="0")
    table["service_tiers"] = {"default": "1", "priority": rate}
    inputs = 2**63 - 1
    binding = quote_binding(helpers, monkeypatch, table=table, inputs=inputs, outputs=1, tier="priority")
    scaled = 999999999999999999999999
    denominator = 10**36
    expected = (inputs * scaled * scaled + denominator - 1) // denominator
    with localcontext() as process_context:
        process_context.prec = 6
        assert helpers.estimate_model_binding_micro(binding) == expected
        assert process_context.prec == 6


@pytest.mark.parametrize("priority", [False, True])
def test_quote_dominates_representative_confirmed_same_capture_usage(helpers, monkeypatch, priority):
    table = config()
    table["models"]["captured-model"].update(long_context_threshold=1000,
        long_input_per_mtok="0.5", long_output_per_mtok="1", long_cached_input_per_mtok="3")
    binding, receipt = evidence(helpers, monkeypatch, table=table)
    binding.update(input_token_limit=1500, max_output_tokens=100, requested_service_tier="priority" if priority else "default")
    receipt["service_tier"] = binding["requested_service_tier"]
    receipt["role_metadata"]["max_output_tokens"] = 100
    quote = helpers.estimate_model_binding_micro(binding)
    assert quote == (9200 if priority else 4600)
    for inputs in [0, 1, 500, 1000, 1001, 1500]:
        for output in [0, 50, 100]:
            for cached in {0, inputs // 2, inputs}:
                receipt.update(input_tokens=inputs, output_tokens=output, total_tokens=inputs + output,
                               cached_tokens=cached, reasoning_tokens=output)
                result = validate(helpers, binding, receipt)
                assert result["output_usable"] is True
                assert quote >= result["provider_actual_micro"]

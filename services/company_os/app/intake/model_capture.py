"""Admission evidence for the intake and Firstlook JSON calls."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from urllib.parse import urlsplit

from llm_gateway import sole
from llm_gateway.provider_foundry import deployment_for, normalize_base_url
from llm_gateway.routing import resolve_purpose_config, sol_output_cap

from app.credits.errors import CreditsUnavailable
from app.credits.provider_receipts import (
    capture_pricing_snapshot, estimate_model_binding_micro, validate_model_binding,
)


def capture_request(messages, *, purpose, config, request_id, max_tokens, response_schema):
    """Capture the route, logical payload and bounded clock policy before a hold."""
    if type(request_id) is not str or not request_id:
        raise CreditsUnavailable("original model intent is required")
    config = resolve_purpose_config(purpose, config, tier="flash")
    max_tokens = sol_output_cap(config, max_tokens)
    config = config.model_copy(deep=True, update={"max_output_tokens": max_tokens})
    if config.source == "founder_override":
        if not config.api_key.get_secret_value().strip():
            raise CreditsUnavailable("founder model key is unavailable")
    else:
        sole.require_env_api_key(config)
    messages, schema = deepcopy(messages), deepcopy(response_schema)
    input_limit = sole.transport_prompt_tokens(messages, purpose=purpose, config=config,
        response_schema=schema, clock_text=" " * 256)
    if input_limit > 64_000:
        raise CreditsUnavailable("model input exceeds its allowance")
    binding = validate_model_binding({"schema_version": 1, "purpose": purpose,
        "provider": config.provider, "deployment": deployment_for(config, config.captured_tier),
        "catalog_model": config.catalog_model, "api_mode": config.api_mode,
        "endpoint_host": urlsplit(normalize_base_url(config.provider, config.base_url,
            api_mode=config.api_mode)).hostname,
        "input_token_limit": input_limit, "max_output_tokens": max_tokens,
        "requested_service_tier": "default",
        "pricing_snapshot": capture_pricing_snapshot(config.catalog_model)})
    captured = {"model_binding": binding, "estimate_micro": estimate_model_binding_micro(binding),
        "token_ceiling": input_limit + max_tokens, "clock_policy": "server_clock_256_bytes"}
    evidence = {"request_id": request_id, "capture": captured, "messages": messages,
        "response_schema": schema, "tools": None, "source": config.source,
        "key_fingerprint": hashlib.sha256(config.api_key.get_secret_value().encode()).hexdigest(),
        "reasoning_effort": config.reasoning_effort}
    captured["request_digest"] = hashlib.sha256(json.dumps(evidence, sort_keys=True,
        ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()).hexdigest()
    return config, messages, schema, captured

"""Declarative purpose → tier routing. Unrouted purpose is a hard error."""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml

from llm_gateway.ledger import MODEL_PRICING, load_pricing

if TYPE_CHECKING:
    from llm_gateway.provider_foundry import ProviderConfig

Tier = Literal["pro", "flash", "embed", "local", "passthrough"]
VALID_TIERS = frozenset({"pro", "flash", "embed", "local", "passthrough"})
PREFIX_TIERS = {"ops": "flash"}
VALID_SOL_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
REASONING_EFFORTS = ("low", "medium", "high", "xhigh", "max")
MODEL_OVERRIDE_FIELDS = frozenset({"model", "catalog_model", "reasoning_effort"})


def supported_reasoning_efforts(config: ProviderConfig) -> tuple[str, ...]:
    if config.api_mode == "responses" and is_sol_model(config.deployment_pro, config.catalog_model):
        return REASONING_EFFORTS
    return ()


def next_reasoning_effort(config: ProviderConfig, current: str, requested: str, *, phase: str) -> str | None:
    if phase not in {"planning", "tools", "synthesis"} or requested not in REASONING_EFFORTS:
        raise ValueError("Unsupported reasoning request")
    supported = supported_reasoning_efforts(config)
    if not supported:
        return None
    if current not in supported or requested not in supported:
        raise ValueError("Reasoning effort is unsupported by this deployment")
    if phase == "tools":
        return "low"
    return supported[min(supported.index(requested), supported.index(current) + 1)]

def _repo_root_candidates() -> list[Path]:
    """Monorepo checkout puts config/ at parents[2]; installed wheel lives under site-packages."""
    here = Path(__file__).resolve()
    roots = [here.parents[2]]
    if len(here.parents) > 3:
        roots.append(here.parents[3])
    return roots


def resolve_routing_path(path: str | None = None) -> Path:
    import os

    if path:
        return Path(path)
    env = (os.environ.get("MODEL_ROUTING_PATH") or "").strip()
    if env:
        return Path(env)
    candidates = [
        Path("/workspace/config/model_routing.yaml"),  # core/runtime image
        *[root / "config" / "model_routing.yaml" for root in _repo_root_candidates()],
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


REPO_ROOT = _repo_root_candidates()[0]
DEFAULT_ROUTING = resolve_routing_path()


class UnroutedPurposeError(KeyError):
    pass


@lru_cache(maxsize=4)
def load_routing(path: str | None = None) -> dict[str, Any]:
    p = resolve_routing_path(path)
    with p.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def clear_routing_cache() -> None:
    load_routing.cache_clear()


def resolve_tier(purpose: str, *, path: str | None = None) -> Tier:
    data = load_routing(path)
    routes = data.get("routes") or {}
    if purpose not in routes:
        family = purpose.split(".", 1)[0]
        if family in PREFIX_TIERS:
            return PREFIX_TIERS[family]  # type: ignore[return-value]
        raise UnroutedPurposeError(
            f"purpose {purpose!r} has no entry in model_routing.yaml"
        )
    tier = str(routes[purpose]).lower()
    if tier not in VALID_TIERS:
        raise UnroutedPurposeError(f"invalid tier {tier!r} for {purpose}")
    return tier  # type: ignore[return-value]


def catalog_model_for(model: str) -> str:
    return (load_pricing(str(MODEL_PRICING)).get("aliases") or {}).get(model, model)


def completion_token_key(model: str) -> str:
    return "max_completion_tokens" if re.search(r"(?:^|[-_/])gpt-[56](?:[.-]|$)", catalog_model_for(model).casefold()) else "max_tokens"


def is_sol_model(model: str, catalog_model: str | None = None) -> bool:
    """Recognize exact catalog names and registered Azure deployment bindings."""
    return any(re.fullmatch(r"gpt-6\.1-sol(?:-\d{4}-\d{2}-\d{2})?", catalog_model_for(name).strip(), re.IGNORECASE)
        for name in (model, catalog_model) if isinstance(name, str))


def wire_api_mode(config: ProviderConfig, model: str | None = None) -> str:
    """One protocol for a captured quote and its dial; contradictions never dial."""
    deployment = config.deployment_flash if config.captured_tier == "flash" else config.deployment_pro
    if config.routing_purpose is not None and (config.api_mode is None or model is not None and model != deployment):
        raise ValueError("Captured model protocol does not match deployment")
    identity = config.catalog_model or model or deployment
    from llm_gateway.provider_foundry import loopback_provider

    mode = "responses" if not loopback_provider(config) and is_sol_model(identity) else "chat_completions"
    if config.api_mode is not None and config.api_mode != mode:
        raise ValueError("Captured model protocol does not match catalog")
    return mode


def canonicalize_model_config(config: ProviderConfig, model: str, *,
                              catalog_model: str | None = None,
                              reasoning_effort: str | None = None) -> ProviderConfig:
    """Set wire identity on a newly built or privately captured config."""
    from llm_gateway.provider_foundry import loopback_provider

    if loopback_provider(config):
        config.catalog_model = model
        config.api_mode = "chat_completions"
        return config
    catalog = catalog_model_for(catalog_model or config.catalog_model or model)
    local_azure_alias = config.provider == "azure_foundry" and catalog_model is not None and catalog_model == config._local_azure_catalog_model
    normalized_model = catalog_model_for(model).casefold().replace("_", "-")
    normalized_catalog = catalog.casefold().replace("_", "-")
    if (not local_azure_alias and normalized_model.startswith(("gpt-", "deepseek-")) and normalized_model != normalized_catalog
            and not (is_sol_model(model) and is_sol_model(catalog))):
        raise ValueError("A known deployment cannot select a different pricing catalog")
    config.catalog_model = catalog
    if is_sol_model(catalog if local_azure_alias else model, catalog):
        config.api_mode = "responses"
        config.reasoning_effort = reasoning_effort or "low"
        if config.reasoning_effort not in VALID_SOL_EFFORTS:
            raise ValueError("Unsupported Sol reasoning effort")
    else:
        config.api_mode = "chat_completions"
        if reasoning_effort is not None:
            config.reasoning_effort = reasoning_effort
    return config


def sol_output_cap(config: ProviderConfig, cap: int) -> int:
    """Reserve and transmit Sol's shared reasoning/output budget with a 2048 floor."""
    if type(cap) is not int or cap <= 0:
        raise ValueError("Output cap must be a positive integer")
    return max(cap, 2048) if config.api_mode == "responses" else cap


def _model_overrides() -> dict[str, dict[str, str]]:
    """Validate the entire operator map before any route can reach a provider."""
    try:
        overrides = json.loads(os.environ.get("MODEL_ROUTING_OVERRIDES") or "{}")
    except (TypeError, ValueError) as exc:
        raise ValueError("MODEL_ROUTING_OVERRIDES must be a JSON object") from exc
    if not isinstance(overrides, dict):
        raise TypeError("MODEL_ROUTING_OVERRIDES must be a JSON object")
    for purpose, settings in overrides.items():
        if purpose != "*":
            resolve_tier(purpose)
        if not isinstance(settings, dict) or settings.keys() - MODEL_OVERRIDE_FIELDS:
            raise ValueError("Model routing overrides contain unsupported fields")
        if any(not isinstance(value, str) or not value.strip() for value in settings.values()):
            raise ValueError("Model routing override values must be nonempty strings")
        if "reasoning_effort" in settings and settings["reasoning_effort"] not in VALID_SOL_EFFORTS:
            raise ValueError("Model routing reasoning effort is unsupported")
    for purpose, source in (load_routing().get("model_route_aliases") or {}).items():
        if purpose not in overrides and source in overrides:
            overrides[purpose] = dict(overrides[source])
    return overrides


def purpose_is_overridden(purpose: str) -> bool:
    overrides = _model_overrides()
    return "*" in overrides or purpose in overrides


def resolve_purpose_config(purpose: str, config: ProviderConfig, tier: str | None = None) -> ProviderConfig:
    """Capture one purpose's deployment and pricing identity before admission.

    Captured configs are reused without consulting the live model map. Provider,
    host and key are never configurable through purpose routing.
    """
    if config.routing_purpose is not None or config.captured_tier is not None:
        if (config.routing_purpose != purpose or config.captured_tier not in VALID_TIERS
                or tier is not None and config.captured_tier != tier):
            raise ValueError("Captured model route does not match purpose and tier")
        return config.model_copy(deep=True)
    declared_tier = resolve_tier(purpose)
    selected_tier = tier or declared_tier
    if selected_tier not in VALID_TIERS:
        raise ValueError("Model routing tier is unsupported")
    captured = config.model_copy(deep=True)
    captured.routing_purpose = purpose
    captured.captured_tier = selected_tier
    overrides = {} if config.source == "founder_override" else _model_overrides()
    if selected_tier in {"embed", "local"}:
        captured.catalog_model = config.deployment_embed if selected_tier == "embed" else "local"
        return captured
    from llm_gateway.provider_foundry import local_mode_enabled

    if local_mode_enabled() and config._local_role_models:
        from app.core.providers_local import model_for_purpose

        model = model_for_purpose(purpose, selected_tier, config._local_role_models)
        captured.deployment_pro = captured.deployment_flash = model
        catalog = config._local_azure_catalog_model if model == config._local_role_models.get("chat") else None
        return canonicalize_model_config(captured, model, catalog_model=catalog)
    model = config.deployment_pro if selected_tier == "pro" else config.deployment_flash
    catalog = config.catalog_model or model
    effort = None
    if config.source != "founder_override":
        for settings in (overrides.get("*", {}), overrides.get(purpose, {})):
            if "model" in settings:
                model = settings["model"].strip()
                catalog = settings.get("catalog_model", model).strip()
            elif "catalog_model" in settings:
                catalog = settings["catalog_model"].strip()
            if "reasoning_effort" in settings:
                effort = settings["reasoning_effort"]
        # An explicit legacy tier must stay pinned when transmit resolves the YAML tier.
        captured.deployment_pro = captured.deployment_flash = model
    return canonicalize_model_config(captured, model, catalog_model=catalog, reasoning_effort=effort)


def concurrency_for(tier: str, path: str | None = None) -> int:
    data = load_routing(path)
    caps = data.get("concurrency") or {}
    return int(caps.get(tier) or 8)


def allow_downgrade_on_budget(path: str | None = None) -> bool:
    data = load_routing(path)
    return bool((data.get("overrides") or {}).get("allow_downgrade_on_budget", False))


def escalate_on_schema_failure(path: str | None = None) -> bool:
    data = load_routing(path)
    return bool((data.get("overrides") or {}).get("escalate_on_schema_failure", True))


def all_purposes(path: str | None = None) -> set[str]:
    data = load_routing(path)
    return set((data.get("routes") or {}).keys())


def prompt_budget(purpose: str, path: str | None = None) -> int | None:
    data = load_routing(path)
    budgets = data.get("prompt_budgets") or {}
    if purpose in budgets:
        return int(budgets[purpose])
    return budgets.get("agent.default")

"""Base URL normalization and Foundry/OpenAI-compatible client factory."""

from __future__ import annotations

import os
import re
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, PrivateAttr, SecretStr

DEFAULT_LOOPBACK_CONTEXT = 8192


def __getattr__(name: str):
    if name not in {"APIConnectionError", "AsyncOpenAI", "DefaultAsyncHttpxClient"}:
        raise AttributeError(name)
    import openai

    value = getattr(openai, name)
    globals()[name] = value
    return value


def _http_client(hooks: dict):
    from openai import DEFAULT_CONNECTION_LIMITS

    from llm_gateway.limiter import httpx
    from llm_gateway.provider_foundry import DefaultAsyncHttpxClient

    return DefaultAsyncHttpxClient(follow_redirects=False, trust_env=False, event_hooks=hooks,
        transport=httpx.AsyncHTTPTransport(retries=2, trust_env=False, limits=DEFAULT_CONNECTION_LIMITS))


class ProviderConfig(BaseModel):
    provider: Literal[
        "azure_foundry",
        "openai_compatible",
        "deepseek_direct",
        "anthropic",
        "openrouter",
        "local",
        "openai",
        "google",
        "xai",
    ] = "azure_foundry"
    base_url: str = ""
    api_key: SecretStr = Field(default_factory=lambda: SecretStr(""))
    deployment_pro: str = "DeepSeek-V4-Pro"
    deployment_flash: str = "DeepSeek-V4-Pro"
    deployment_embed: str = "text-embedding-3-small"
    reasoning_effort: str = "high"
    timeout_seconds: int = 120
    max_output_tokens: int = 4096
    context_window: int = Field(default=DEFAULT_LOOPBACK_CONTEXT, ge=2048, le=262144)
    # When set to founder_override, create_async_client_from_config may use this key.
    source: str | None = None
    # Captured before admission; deployment names and priced catalog IDs differ on Azure.
    routing_purpose: str | None = None
    captured_tier: str | None = None
    catalog_model: str | None = None
    api_mode: str | None = None
    _platform_api_key_pin: SecretStr | None = PrivateAttr(default=None)
    # The endpoint host the pinned key was authorized for; a copy aimed elsewhere is refused.
    _platform_host_pin: str | None = PrivateAttr(default=None)
    _local_role_models: dict[str, str] = PrivateAttr(default_factory=dict)
    _local_azure_catalog_model: str | None = PrivateAttr(default=None)

    def _pin_platform(self) -> None:
        if not self.api_key.get_secret_value().strip():
            return
        self._platform_api_key_pin = self.api_key
        self._platform_host_pin = endpoint_host(self.base_url)


def endpoint_host(base_url: str) -> str:
    return (urlparse((base_url or "").strip()).hostname or "").casefold()


def log_platform_routing_config() -> None:
    """Worker startup facts only; never keys, URL credentials, query or fragment."""
    if local_mode_enabled():
        return
    import logging

    from core.redact import redact_string

    from llm_gateway.routing import is_sol_model
    config = load_provider_config_from_env()
    parsed = urlparse(config.base_url)
    facts = {"provider": config.provider, "deployment_pro": config.deployment_pro,
        "deployment_flash": config.deployment_flash, "catalog_model": config.catalog_model,
        "api_mode": config.api_mode, "base_url_host": parsed.hostname or "", "base_url_path": parsed.path,
        "is_sol_pro": is_sol_model(config.deployment_pro, config.catalog_model),
        "is_sol_flash": is_sol_model(config.deployment_flash, config.catalog_model)}
    key = config.api_key.get_secret_value()
    facts = {name: redact_string(value, exact_values=[key] if key else []) if isinstance(value, str) else value
        for name, value in facts.items()}
    logging.getLogger("llm_gateway.routing").info("platform_routing_config", extra=facts)


def load_provider_config_from_env() -> ProviderConfig:
    """Load LLM_* from env.

    ``LLM_PROFILE=test`` switches to Groq (openai_compatible) using
    ``GROQ_API_KEY`` / ``GROQ_MODEL`` for cheap local checks. Production
    stays on ``LLM_PROVIDER`` (Azure Foundry DeepSeek V4 or Anthropic).
    """
    if local_mode_enabled():
        from app.core.providers_local import load_local_config

        return load_local_config()
    profile = (os.getenv("LLM_PROFILE") or "prod").strip().lower()
    if profile in {"test", "groq", "dev"}:
        groq_key = (os.getenv("GROQ_API_KEY") or "").strip()
        groq_model = (os.getenv("GROQ_MODEL") or "llama-3.3-70b-versatile").strip()
        return ProviderConfig(
            provider="openai_compatible",
            base_url="https://api.groq.com/openai/v1",
            api_key=SecretStr(groq_key),
            deployment_pro=groq_model,
            deployment_flash=groq_model,
            reasoning_effort="",
            timeout_seconds=int(os.getenv("LLM_TIMEOUT_SECONDS", "60")),
            max_output_tokens=int(os.getenv("LLM_MAX_TOKENS", "2048")),
            source="profile_test",
        )
    config = ProviderConfig(
        provider=os.getenv("LLM_PROVIDER", "azure_foundry"),  # type: ignore[arg-type]
        base_url=os.getenv("LLM_BASE_URL", "").rstrip("/"),
        api_key=SecretStr(os.getenv("LLM_API_KEY", "")),
        deployment_pro=os.getenv(
            "LLM_DEPLOYMENT_PRO",
            os.getenv("LLM_DEPLOYMENT_NAME", "DeepSeek-V4-Pro"),
        ),
        deployment_flash=os.getenv("LLM_DEPLOYMENT_FLASH", "DeepSeek-V4-Pro"),
        deployment_embed=os.getenv("LLM_DEPLOYMENT_EMBED", "text-embedding-3-small"),
        reasoning_effort=os.getenv("LLM_REASONING_EFFORT", "high"),
        timeout_seconds=int(os.getenv("LLM_TIMEOUT_SECONDS", "120")),
        max_output_tokens=int(os.getenv("LLM_MAX_TOKENS", "4096")),
        source="platform_default",
    )
    if config.deployment_pro == config.deployment_flash:
        from llm_gateway.routing import canonicalize_model_config

        canonicalize_model_config(config, config.deployment_pro,
            reasoning_effort=os.getenv("LLM_REASONING_EFFORT"))
    config._pin_platform()
    return config


def local_mode_enabled() -> bool:
    try:
        from app.core.local_mode import is_local
    except ModuleNotFoundError:
        return False
    return is_local()


def loopback_provider(config: ProviderConfig | None) -> bool:
    if config is None or not local_mode_enabled() or config.provider != "openai_compatible":
        return False
    from app.core.providers_local import _loopback

    return _loopback(endpoint_host(config.base_url))


def sample_mode_enabled() -> bool:
    if not local_mode_enabled():
        return False
    from app.core.local_mode import is_offline

    return is_offline()


def pin_platform_credentials(config: ProviderConfig) -> ProviderConfig:
    """Authorize an explicit platform key once before its quote and preserve it privately."""
    captured = config.model_copy(deep=True)
    if captured.source != "platform_default":
        raise ValueError("Platform credential capture requires a platform config")
    from llm_gateway.sole import require_env_api_key

    require_env_api_key(captured)
    if not captured.api_key.get_secret_value().strip():
        raise RuntimeError("Platform credential capture requires a configured API key")
    captured._pin_platform()
    return captured


def normalize_base_url(provider: str, base_url: str, *, api_mode: str | None = None, local_azure: bool = False) -> str:
    """Foundry OpenAI-compatible surface lives at <endpoint>/openai/v1."""
    if provider == "deepseek_direct" and not base_url:
        return "https://api.deepseek.com"
    if provider == "openrouter" and not base_url:
        return "https://openrouter.ai/api/v1"
    if provider == "openai" and not base_url:
        return "https://api.openai.com/v1"
    if provider == "xai" and not base_url:
        return "https://api.x.ai/v1"
    if provider == "google" and not base_url:
        return "https://generativelanguage.googleapis.com/v1beta/openai/"
    if provider == "anthropic" and not base_url:
        return "https://api.anthropic.com/v1"
    if provider == "local" and not base_url:
        return "http://localhost:11434/v1"
    u = (base_url or "").rstrip("/")
    if provider == "azure_foundry":
        if not u:
            return u
        if local_azure:
            parsed = urlparse(u)
            resource_path = parsed.path in {"", "/openai", "/openai/v1", "/v1"}
            legacy_path = re.fullmatch(
                r"/(?:api/projects|openai/deployments)/\w[\w.-]*(?:/openai(?:/v1)?)?", parsed.path, re.ASCII)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None
                    or parsed.params or parsed.query or parsed.fragment or not (resource_path or legacy_path)):
                raise ValueError("Azure requires an HTTPS resource endpoint")
            return f"https://{parsed.netloc}/openai/v1"
        if api_mode == "responses":
            parsed = urlparse(u)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("Azure Responses requires an HTTPS resource endpoint")
            return f"https://{parsed.netloc}/openai/v1"
        if u.endswith("/openai/v1"):
            return u
        if u.endswith("/openai"):
            return u + "/v1"
        return u + "/openai/v1"
    return u


def create_async_client(config: ProviderConfig | None = None) -> tuple[Any, ProviderConfig]:
    """Build the client for saved local settings or the configured hosted route."""
    import os

    if sample_mode_enabled():
        raise RuntimeError("You are using sample data. Save your AI settings to use your own model.")
    if local_mode_enabled():
        local = config if config is not None and config._local_role_models else load_provider_config_from_env()
        return create_async_client_from_config(local)
    # Always prefer the live process env so a passed-in config cannot swap keys.
    cfg = load_provider_config_from_env()
    if config is not None:
        # Allow overriding non-secret knobs (timeout, effort) but never the key/host/deployments
        # unless they match env — deployments already come from env via load_*.
        passed_key = config.api_key.get_secret_value().strip()
        env_key = (os.environ.get("LLM_API_KEY") or "").strip()
        if passed_key and env_key and passed_key != env_key:
            raise RuntimeError(
                "Refusing to create LLM client: api_key does not match LLM_API_KEY from .env"
            )
        if config.timeout_seconds:
            cfg.timeout_seconds = config.timeout_seconds
        # Empty explicitly disables reasoning for structured-output calls that
        # need the token budget reserved for final JSON.
        cfg.reasoning_effort = config.reasoning_effort
        if config.max_output_tokens:
            cfg.max_output_tokens = config.max_output_tokens

    key = cfg.api_key.get_secret_value()
    if not key:
        profile = (os.environ.get("LLM_PROFILE") or "prod").strip().lower()
        if profile in {"test", "groq", "dev"}:
            raise RuntimeError("GROQ_API_KEY is not configured (LLM_PROFILE=test)")
        raise RuntimeError("LLM_API_KEY is not configured")
    if not cfg.base_url and cfg.provider != "deepseek_direct":
        raise RuntimeError("LLM_BASE_URL is not configured")
    base = normalize_base_url(cfg.provider, cfg.base_url, api_mode=cfg.api_mode)
    from llm_gateway.limiter import request_hooks
    from llm_gateway.provider_foundry import AsyncOpenAI

    client = AsyncOpenAI(api_key=key, base_url=base, timeout=cfg.timeout_seconds,
        http_client=_http_client({"request": request_hooks(cfg)}))
    return client, cfg


def create_async_client_from_config(config: ProviderConfig) -> tuple[Any, ProviderConfig]:
    """Build a client from an explicit config (founder BYOK). Does not re-load env keys."""
    if sample_mode_enabled():
        raise RuntimeError("You are using sample data. Save your AI settings to use your own model.")
    if local_mode_enabled() and not config._local_role_models:
        from app.core.providers_local import LocalProviderError

        raise LocalProviderError("Your connection does not use your local settings. Save your provider in the setup page, then retry.")
    key = config.api_key.get_secret_value().strip()
    if not key:
        raise RuntimeError("API key is not configured for this workspace override.")
    base = normalize_base_url(config.provider, config.base_url, api_mode=config.api_mode)
    if local_mode_enabled():
        from app.core.providers_local import validate_endpoint

        base = validate_endpoint(base)
    if not base and config.provider not in {"deepseek_direct"}:
        raise RuntimeError("base_url is required for this provider.")
    hooks = {}
    if config.source == "founder_override":
        from core.env_contract import parse_app_env
        from core.research_web.guard import check_url, pin_request

        if parse_app_env() != "local" and not local_mode_enabled():
            try:
                reason = check_url(base)
                if reason:
                    raise ValueError(reason)
            except ValueError as exc:
                raise RuntimeError("The workspace provider URL must resolve to a public address.") from exc

            async def pin_founder_request(request):
                # openai>=3 only wraps transport errors; raise the SDK error so callers see one type.
                try:
                    pin_request(request)
                except PermissionError as exc:
                    raise APIConnectionError(request=request) from exc

            hooks = {"request": [pin_founder_request]}
    from llm_gateway.limiter import httpx, request_hooks
    hooks.setdefault("request", []).extend(request_hooks(config))
    if local_mode_enabled() and config.provider == "anthropic":
        from llm_gateway.local_transport import AnthropicMessagesClient

        return AnthropicMessagesClient(api_key=key, base_url=base,
            timeout=config.timeout_seconds, hooks=hooks), config
    # A provider redirect must never send a brokered key to another destination.
    from llm_gateway.provider_foundry import APIConnectionError, AsyncOpenAI

    loopback = loopback_provider(config)
    client = AsyncOpenAI(
        api_key=key, base_url=base or None,
        timeout=httpx.Timeout(config.timeout_seconds, connect=min(10, config.timeout_seconds)) if loopback else config.timeout_seconds,
        http_client=_http_client(hooks),
    )
    if loopback:
        from llm_gateway.local_transport import LoopbackJSONClient

        client = LoopbackJSONClient(client, context_window=config.context_window)
    return client, config


def redacted_status(config: ProviderConfig | None = None) -> dict[str, Any]:
    cfg = config or load_provider_config_from_env()
    host = urlparse(cfg.base_url).netloc if cfg.base_url else ""
    profile = (os.environ.get("LLM_PROFILE") or "prod").strip().lower()
    return {
        "provider": cfg.provider,
        "profile": profile,
        "configured": bool(
            cfg.api_key.get_secret_value()
            and (cfg.base_url or cfg.provider == "deepseek_direct")
        ),
        "endpoint_host": host,
        "deployment_pro": cfg.deployment_pro,
        "deployment_flash": cfg.deployment_flash,
        "deployment_embed": cfg.deployment_embed,
        "has_api_key": bool(cfg.api_key.get_secret_value()),
        "source": cfg.source,
    }


def deployment_for(cfg: ProviderConfig, tier: str) -> str:
    if tier == "pro":
        return cfg.deployment_pro
    if tier == "embed":
        return cfg.deployment_embed
    return cfg.deployment_flash


_PLATFORM_ALIASES = {
    "supymem-flash": "flash",
    "supymem-pro": "pro",
    "supymem-embed": "embed",
}


def resolve_live_model(
    cfg: ProviderConfig,
    *,
    tier: str,
    passthrough_model: str | None = None,
) -> str:
    """Map product aliases (supymem-flash) to the live Foundry deployment.

    Foundry does not host ``supymem-*``. Sending that name returns 400 and the
    OpenAI door used to swallow it as a fake assistant reply.
    """
    name = (passthrough_model or "").strip()
    if name:
        mapped = _PLATFORM_ALIASES.get(name)
        if mapped:
            return deployment_for(cfg, mapped)
        if name.startswith("supymem-"):
            return deployment_for(cfg, "flash" if tier == "passthrough" else tier)
        return name
    use = "flash" if tier == "passthrough" else tier
    return deployment_for(cfg, use)

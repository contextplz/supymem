"""Provider-agnostic OpenAI-compatible LLM client.

All live HTTP traffic goes through `llm_gateway`. Platform default uses process
`LLM_*` env (whatever operator configured — DeepSeek, Foundry, etc.). When a
workspace BYOK override is active (context / workspace_id), that founder key
and model are used instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse

from openai import AsyncOpenAI


@dataclass(frozen=True)
class ProviderConfig:
    provider: str
    base_url: str
    api_key: str
    deployment_name: str
    catalog_model: str = "DeepSeek-V4-Pro"
    reasoning_effort: str = "high"
    timeout_seconds: int = 120
    max_tokens: int = 2048


def load_provider_config_from_env() -> ProviderConfig:
    import os

    return ProviderConfig(
        provider=os.getenv("LLM_PROVIDER", "azure_foundry"),
        base_url=os.getenv("LLM_BASE_URL", "").rstrip("/"),
        api_key=os.getenv("LLM_API_KEY", ""),
        deployment_name=os.getenv(
            "LLM_DEPLOYMENT_NAME",
            os.getenv("LLM_DEPLOYMENT_PRO", "DeepSeek-V4-Pro"),
        ),
        catalog_model=os.getenv("LLM_CATALOG_MODEL", "DeepSeek-V4-Pro"),
        reasoning_effort=os.getenv("LLM_REASONING_EFFORT", "high"),
        timeout_seconds=int(os.getenv("LLM_TIMEOUT_SECONDS", "120")),
        max_tokens=int(os.getenv("LLM_MAX_TOKENS", "2048")),
    )


def normalize_base_url(provider: str, base_url: str) -> str:
    from llm_gateway.provider_foundry import normalize_base_url as _norm

    return _norm(provider, base_url)


def create_model_client(config: Optional[ProviderConfig] = None) -> tuple[AsyncOpenAI, ProviderConfig]:
    """Open the shared env-backed client. Ignores alternate api keys."""
    from llm_gateway.sole import open_env_client

    # Force env — never dial with a caller-supplied alternate key.
    client, foundry_cfg = open_env_client(None)
    # Project a back-compat ProviderConfig for callers/tests.
    cfg = ProviderConfig(
        provider=foundry_cfg.provider,
        base_url=foundry_cfg.base_url,
        api_key=foundry_cfg.api_key.get_secret_value(),
        deployment_name=foundry_cfg.deployment_pro,
        catalog_model=foundry_cfg.deployment_pro,
        reasoning_effort=foundry_cfg.reasoning_effort,
        timeout_seconds=foundry_cfg.timeout_seconds,
        max_tokens=foundry_cfg.max_output_tokens,
    )
    # Keep signature accepting config for tests that only check redaction / shape,
    # but live dialing always used env above.
    _ = config
    return client, cfg


def redacted_status(config: Optional[ProviderConfig] = None) -> dict[str, Any]:
    cfg = config or load_provider_config_from_env()
    host = urlparse(cfg.base_url).netloc if cfg.base_url else ""
    return {
        "provider": cfg.provider,
        "configured": bool(cfg.api_key and (cfg.base_url or cfg.provider == "deepseek_direct")),
        "endpoint_host": host,
        "deployment_name": cfg.deployment_name,
        "catalog_model": cfg.catalog_model,
        "has_api_key": bool(cfg.api_key),
    }


async def chat_completion(
    messages: list[dict[str, Any]],
    *,
    tools: Optional[list[dict[str, Any]]] = None,
    config: Optional[ProviderConfig] = None,
    workspace_id: str | None = None,
) -> Any:
    """Live completion via llm_gateway — platform env or workspace BYOK."""
    from llm_gateway.sole import env_chat_completion

    _ = config  # secrets never taken from this legacy shape; env/BYOK wins
    return await env_chat_completion(
        messages, tools=tools, tier="pro", workspace_id=workspace_id
    )

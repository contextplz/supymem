"""OpenAI-compatible adapter — always dials with LLM_API_KEY from the environment."""

from __future__ import annotations

from typing import Any

from openai import AsyncOpenAI

from llm_gateway.provider_foundry import ProviderConfig
from llm_gateway.routing import completion_token_key, is_sol_model, resolve_tier
from llm_gateway.sole import open_env_client


def create_openai_compatible_client(
    config: ProviderConfig | None = None,
) -> AsyncOpenAI:
    """Build a client. Alternate keys are rejected; env wins."""
    client, _cfg = open_env_client(config)
    return client


async def chat_raw(
    client: AsyncOpenAI,
    *,
    purpose: str,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    max_tokens: int = 4096,
    extra_body: dict[str, Any] | None = None,
) -> Any:
    resolve_tier(purpose)
    client = client.with_options(max_retries=0)
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        completion_token_key(model): max_tokens,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    if extra_body:
        kwargs["extra_body"] = extra_body
    if is_sol_model(model):
        from llm_gateway.responses import responses_chat

        return await responses_chat(client, **kwargs)
    return await client.chat.completions.create(**kwargs)

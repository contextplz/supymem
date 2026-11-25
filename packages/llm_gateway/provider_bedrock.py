"""Amazon Bedrock fallback — same ProviderConfig-shaped contract, Converse API.

Used only when Foundry returns 5xx/429 beyond the retry budget (docs/supymem/08 §1).
boto3 is imported at call time so unit tests can inject a converse function without AWS.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from pydantic import BaseModel, Field, SecretStr

from llm_gateway.errors import ProviderCallError
from llm_gateway.provider_foundry import ProviderConfig

log = logging.getLogger("llm_gateway.bedrock")

ConverseFn = Callable[[dict[str, Any]], Any]


class BedrockConfig(BaseModel):
    provider: str = "bedrock"
    region: str = "us-east-1"
    model_pro: str = "anthropic.claude-3-5-sonnet-20241022-v2:0"
    model_flash: str = "anthropic.claude-3-haiku-20240307-v1:0"
    timeout_seconds: int = 120
    max_output_tokens: int = 4096
    api_key: SecretStr = Field(default_factory=lambda: SecretStr(""))
    source: str | None = "bedrock_fallback"


def load_bedrock_config_from_env() -> BedrockConfig:
    return BedrockConfig(
        region=os.getenv("AWS_REGION", os.getenv("AWS_DEFAULT_REGION", "us-east-1")),
        model_pro=os.getenv(
            "LLM_FALLBACK_MODEL_PRO",
            "anthropic.claude-3-5-sonnet-20241022-v2:0",
        ),
        model_flash=os.getenv(
            "LLM_FALLBACK_MODEL_FLASH",
            "anthropic.claude-3-haiku-20240307-v1:0",
        ),
        timeout_seconds=int(os.getenv("LLM_TIMEOUT_SECONDS", "120")),
        max_output_tokens=int(os.getenv("LLM_MAX_TOKENS", "4096")),
    )


def model_for(cfg: BedrockConfig, tier: str) -> str:
    return cfg.model_pro if tier == "pro" else cfg.model_flash


def openai_messages_to_converse(messages: list[dict[str, Any]]) -> tuple[str | None, list[dict[str, Any]]]:
    system_parts: list[str] = []
    converse: list[dict[str, Any]] = []
    for msg in messages:
        role = str(msg.get("role") or "user")
        content = msg.get("content")
        text = content if isinstance(content, str) else str(content or "")
        if role == "system":
            system_parts.append(text)
            continue
        if role == "tool":
            converse.append(
                {
                    "role": "user",
                    "content": [{"text": f"[tool:{msg.get('tool_call_id') or 'result'}] {text}"}],
                }
            )
            continue
        mapped = "assistant" if role == "assistant" else "user"
        converse.append({"role": mapped, "content": [{"text": text}]})
    system = "\n\n".join(p for p in system_parts if p) or None
    return system, converse


def converse_to_openai(raw: Any, *, model: str) -> Any:
    """Shape a Bedrock Converse response like an OpenAI chat completion."""
    output = raw.get("output") if isinstance(raw, dict) else getattr(raw, "output", {}) or {}
    message = (output or {}).get("message") if isinstance(output, dict) else {}
    blocks = (message or {}).get("content") or []
    texts = []
    for block in blocks:
        if isinstance(block, dict) and "text" in block:
            texts.append(str(block["text"]))
    content = "".join(texts)
    usage = raw.get("usage") if isinstance(raw, dict) else {}
    prompt_tokens = int((usage or {}).get("inputTokens") or 0)
    completion_tokens = int((usage or {}).get("outputTokens") or 0)
    msg = SimpleNamespace(content=content, tool_calls=None, reasoning_content=None)
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    usage_ns = SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )
    resp = SimpleNamespace(
        choices=[choice],
        usage=usage_ns,
        model=model,
        provider_used="bedrock",
        raw=raw,
    )
    return resp


def _boto_converse(payload: dict[str, Any]) -> Any:
    try:
        import boto3  # type: ignore[import-untyped]
    except ImportError as exc:
        raise ProviderCallError(
            "provider_down",
            "Bedrock fallback requires boto3.",
            retryable=False,
        ) from exc
    options = {}
    if payload.pop("_single_attempt", False):
        from botocore.config import Config

        options["config"] = Config(retries={"total_max_attempts": 1})
    client = boto3.client(
        "bedrock-runtime",
        region_name=payload.pop("_region"),
        **options,
    )
    return client.converse(**payload)


_converse_fn: ConverseFn | None = None


def set_converse_fn(fn: ConverseFn | None) -> None:
    """Tests inject a fake Converse implementation here."""
    global _converse_fn
    _converse_fn = fn


def fallback_enabled() -> bool:
    return (os.getenv("LLM_FALLBACK_PROVIDER") or "").strip().lower() == "bedrock"


async def bedrock_chat(
    messages: list[dict[str, Any]],
    *,
    tier: str = "flash",
    max_tokens: int | None = None,
    config: BedrockConfig | None = None,
    converse_fn: ConverseFn | None = None,
    single_attempt: bool = False,
) -> Any:
    cfg = config or load_bedrock_config_from_env()
    model = model_for(cfg, tier)
    system, converse_msgs = openai_messages_to_converse(messages)
    payload: dict[str, Any] = {
        "modelId": model,
        "messages": converse_msgs or [{"role": "user", "content": [{"text": ""}]}],
        "inferenceConfig": {"maxTokens": max_tokens or cfg.max_output_tokens},
        "_region": cfg.region,
    }
    if system:
        payload["system"] = [{"text": system}]
    if single_attempt:
        payload["_single_attempt"] = True
    fn = converse_fn or _converse_fn or _boto_converse
    try:
        raw = fn(payload)
    except ProviderCallError:
        raise
    except Exception as exc:
        log.warning("bedrock_converse_failed", extra={"error_type": type(exc).__name__})
        raise ProviderCallError(
            "provider_down",
            "Bedrock fallback is unavailable right now.",
            retryable=True,
        ) from exc
    resp = converse_to_openai(raw, model=model)
    log.info("provider_used", extra={"provider_used": "bedrock", "tier": tier})
    return resp


def to_provider_config(cfg: BedrockConfig | None = None) -> ProviderConfig:
    """Surface Bedrock as a ProviderConfig for status/redaction callers."""
    b = cfg or load_bedrock_config_from_env()
    return ProviderConfig(
        provider="openai_compatible",
        base_url=f"bedrock://{b.region}",
        deployment_pro=b.model_pro,
        deployment_flash=b.model_flash,
        timeout_seconds=b.timeout_seconds,
        max_output_tokens=b.max_output_tokens,
        source="bedrock_fallback",
    )


__all__ = [
    "BedrockConfig",
    "bedrock_chat",
    "fallback_enabled",
    "load_bedrock_config_from_env",
    "model_for",
    "openai_messages_to_converse",
    "set_converse_fn",
    "to_provider_config",
]

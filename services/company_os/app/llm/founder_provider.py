"""Workspace-scoped BYOK LLM configuration.

The platform LLM configuration remains the default.  A founder override stores
only a credential-broker reference in Company OS; the raw API key stays in the
configured secret store and is never returned or logged here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

from core.env_contract import parse_app_env
from core.research_web.guard import check_url
from llm_gateway.local_transport import DEFAULT_ANTHROPIC_MODEL
from sqlalchemy import select

from app.connectors.secrets import store as secret_store
from app.db.models import FounderLLMSetting
from app.state import db

if TYPE_CHECKING:
    from llm_gateway.provider_foundry import ProviderConfig

Provider = Literal[
    "deepseek_direct",
    "azure_foundry",
    "openai_compatible",
    "anthropic",
    "openrouter",
    "local",
    "openai",
    "google",
    "xai",
]

SUPPORTED_PROVIDERS: frozenset[str] = frozenset(
    {
        "deepseek_direct",
        "azure_foundry",
        "openai_compatible",
        "anthropic",
        "openrouter",
        "local",
        "openai",
        "google",
        "xai",
    }
)


class FounderLLMConfigError(ValueError):
    """A founder-supplied provider configuration is invalid."""


@dataclass(frozen=True)
class FounderLLMConfig:
    workspace_id: str
    provider: Provider
    base_url: str | None
    model: str
    api_key_ref: str


@dataclass(frozen=True)
class ResolvedLLMConfig:
    workspace_id: str
    provider: Provider
    base_url: str | None
    model: str
    api_key_ref: str | None
    source: Literal["founder_override", "platform_default"]

    @property
    def key_is_set(self) -> bool:
        if self.source == "founder_override":
            return bool(self.api_key_ref)
        return bool(os.environ.get("LLM_API_KEY", "").strip())


@dataclass(frozen=True)
class LocalResolvedLLMConfig(ResolvedLLMConfig):
    local_config: ProviderConfig = field(repr=False, compare=False)


def never_log_api_key(_: str | None) -> str:
    """Return a fixed mask for UI/status use without retaining the secret."""
    return "********" if _ else ""


def _provider(value: str) -> Provider:
    if value not in SUPPORTED_PROVIDERS:
        raise FounderLLMConfigError("Choose a supported provider in Settings.")
    return value  # type: ignore[return-value]


def _clean_required(value: str, field: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise FounderLLMConfigError(f"{field} is required.")
    return cleaned


def _clean_url(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip().rstrip("/")
    if not cleaned:
        return None
    if not cleaned.startswith(("https://", "http://")):
        raise FounderLLMConfigError("base_url must start with http:// or https://.")
    return cleaned


def _checked_founder_url(provider: Provider, value: str | None) -> str | None:
    """Validate founder endpoints before storing or retrieving a credential."""
    from llm_gateway.provider_foundry import normalize_base_url

    try:
        cleaned = _clean_url(value)
        parsed = urlsplit(normalize_base_url(provider, cleaned or ""))
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("invalid endpoint")
        _ = parsed.port
        if parse_app_env() != "local":
            reason = check_url(parsed.geturl())
            if reason:
                raise ValueError(reason)
    except ValueError as exc:
        raise FounderLLMConfigError(
            "Use a public provider URL without credentials, a query, or a fragment."
        ) from exc
    return cleaned


def _from_row(row: FounderLLMSetting) -> FounderLLMConfig:
    return FounderLLMConfig(
        workspace_id=row.workspace_id,
        provider=_provider(row.provider),
        base_url=row.base_url,
        model=row.model,
        api_key_ref=row.api_key_ref,
    )


def get_founder_config(workspace_id: str) -> FounderLLMConfig | None:
    with db()() as session:
        row = session.scalar(
            select(FounderLLMSetting).where(FounderLLMSetting.workspace_id == workspace_id)
        )
        return _from_row(row) if row else None


def set_founder_config(
    *,
    workspace_id: str,
    provider: str,
    model: str,
    base_url: str | None,
    api_key: str | None,
) -> FounderLLMConfig:
    """Persist a configuration and broker the supplied key, never the key itself."""
    from app.core.local_mode import is_local

    if is_local():
        from app.core.providers_local import LocalProviderError, save_settings

        try:
            save_settings(provider="azure_openai" if provider == "azure_foundry" else provider,
                base_url=base_url, api_key=api_key, models={"chat": model})
            resolved = resolve_llm_config(workspace_id)
            return FounderLLMConfig(workspace_id, resolved.provider, resolved.base_url, resolved.model, resolved.api_key_ref)
        except LocalProviderError as exc:
            raise FounderLLMConfigError(str(exc)) from None
    checked_provider = _provider(provider)
    checked_model = _clean_required(model, "model")
    # Local OpenAI-compatible runtimes (Ollama default :11434, LM Studio :1234).
    if checked_provider == "local" and not (base_url or "").strip():
        base_url = "http://localhost:11434/v1"
    checked_url = _checked_founder_url(checked_provider, base_url)
    raw_key = (api_key or "").strip()

    with db()() as session:
        row = session.scalar(
            select(FounderLLMSetting).where(FounderLLMSetting.workspace_id == workspace_id)
        )
        if row and not raw_key and (
            row.provider != checked_provider or _clean_url(row.base_url) != checked_url
        ):
            raise FounderLLMConfigError(
                "Re-enter your API key when changing the provider or provider URL."
            )
        # Local runtimes ignore the key but OpenAI clients require a non-empty value.
        if checked_provider == "local" and not raw_key and row is None:
            raw_key = "ollama"
        if not raw_key and row is None:
            raise FounderLLMConfigError("api_key is required when creating an override.")

        old_ref = row.api_key_ref if row else None
        new_ref = old_ref
        if raw_key:
            new_ref = secret_store().put(
                workspace_id=workspace_id,
                provider=checked_provider,
                key="llm_api_key",
                value=raw_key,
            )

        if row is None:
            row = FounderLLMSetting(
                workspace_id=workspace_id,
                provider=checked_provider,
                base_url=checked_url,
                model=checked_model,
                api_key_ref=new_ref,
            )
            session.add(row)
        else:
            row.provider = checked_provider
            row.base_url = checked_url
            row.model = checked_model
            row.api_key_ref = new_ref
        session.commit()
        config = _from_row(row)

    if raw_key and old_ref and old_ref != new_ref:
        try:
            secret_store().forget(old_ref)
        except Exception:
            # The replacement is already committed. A vault retry/reconciler can
            # collect an orphan without exposing either credential to callers.
            pass
    return config


def clear_founder_config(workspace_id: str) -> bool:
    """Remove the override and its brokered secret, restoring platform defaults."""
    from app.core.local_mode import is_local

    if is_local():
        from app.core.providers_local import clear_settings

        return clear_settings()
    with db()() as session:
        row = session.scalar(
            select(FounderLLMSetting).where(FounderLLMSetting.workspace_id == workspace_id)
        )
        if row is None:
            return False
        secret_ref = row.api_key_ref
        session.delete(row)
        session.commit()
    try:
        secret_store().forget(secret_ref)
    except Exception:
        # Do not leave an active DB override when an external vault is transiently
        # unavailable. The broker can reconcile the unreferenced secret later.
        pass
    return True


def resolve_llm_config(workspace_id: str) -> ResolvedLLMConfig:
    """Resolve a usable founder override before the unchanged platform default."""
    from app.core.local_mode import is_local

    if is_local():
        from app.core.providers_local import (
            LocalProviderError,
            credential_identity,
            load_local_config,
        )

        try:
            config = load_local_config()
        except LocalProviderError as exc:
            raise FounderLLMConfigError(str(exc)) from None
        return LocalResolvedLLMConfig(workspace_id=workspace_id, provider=config.provider, base_url=config.base_url,
            model=config._local_role_models["chat"], api_key_ref=credential_identity(config),
            source="founder_override", local_config=config)
    founder = get_founder_config(workspace_id)
    if founder and founder.api_key_ref:
        return ResolvedLLMConfig(
            workspace_id=workspace_id,
            provider=founder.provider,
            base_url=founder.base_url,
            model=founder.model,
            api_key_ref=founder.api_key_ref,
            source="founder_override",
        )

    from llm_gateway.provider_foundry import load_provider_config_from_env

    # Settings must describe the same deployment used by the platform gateway.
    # Founder overrides above remain independent of the platform configuration.
    platform = load_provider_config_from_env()
    return ResolvedLLMConfig(
        workspace_id=workspace_id,
        provider=_provider(platform.provider),
        base_url=_clean_url(platform.base_url),
        model=platform.deployment_pro,
        api_key_ref=None,
        source="platform_default",
    )


def resolved_provider_identity(config: ResolvedLLMConfig) -> str:
    """Opaque identity of an already-resolved billable route; no raw credentials."""
    import hashlib
    import json
    fields = {"workspace_id": config.workspace_id, "provider": config.provider, "model": config.model,
        "endpoint": (config.base_url or "").rstrip("/"), "source": config.source, "credential_ref": config.api_key_ref}
    return hashlib.sha256(json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def to_provider_config(workspace_id: str, *, resolved_config: ResolvedLLMConfig | None = None):
    """Build an llm_gateway ProviderConfig for transmit when BYOK is set; else None."""
    from llm_gateway.provider_foundry import ProviderConfig
    from pydantic import SecretStr

    resolved = resolved_config or resolve_llm_config(workspace_id)
    if resolved.workspace_id != workspace_id:
        return None
    from app.core.local_mode import is_local

    if is_local():
        return resolved.local_config.model_copy(deep=True) if isinstance(resolved, LocalResolvedLLMConfig) else None
    if resolved.source != "founder_override" or not resolved.api_key_ref:
        return None
    # Stored endpoints and pinned configs may predate today's safety checks.
    # Re-check DNS before retrieving the key for every provider call.
    checked_url = _checked_founder_url(resolved.provider, resolved.base_url)
    try:
        raw = secret_store().get(resolved.api_key_ref, requester="llm_gateway")
    except Exception:
        return None
    if not (raw or "").strip():
        return None
    return ProviderConfig(
        provider=resolved.provider,  # type: ignore[arg-type]
        base_url=checked_url or "",
        api_key=SecretStr(raw.strip()),
        deployment_pro=resolved.model,
        deployment_flash=resolved.model,
        source="founder_override",
    )


def keyless_local_config(config) -> bool:
    from llm_gateway.provider_foundry import loopback_provider

    return loopback_provider(config)


async def compatible_provider_models(config) -> list[str] | None:
    from llm_gateway.errors import ProviderCallError
    from llm_gateway.local_transport import local_model_error, missing_model_message
    from llm_gateway.provider_foundry import create_async_client_from_config

    client, _ = create_async_client_from_config(config)
    try:
        listed = (await client.models.list()).data
        key = config.api_key.get_secret_value()
        models = sorted({model.id for model in listed if 0 < len(model.id) <= 256
            and all(character.isprintable() and not character.isspace() for character in model.id)
            and (not key or key not in model.id)})
    except Exception as exc:
        if getattr(exc, "status_code", None) in {404, 405, 501}:
            return None
        if getattr(exc, "status_code", None) is not None:
            raise local_model_error(exc.status_code) from None
        raise ProviderCallError("provider_down", "You could not list your installed models. Start your model server, check its service URL in Settings > Keys and connections, then test your connection again.") from None
    finally:
        await client.close()
    chosen = config._local_role_models["chat"]
    alias = chosen + ":latest" if urlsplit(config.base_url or "").port == 11434 and ":" not in chosen else chosen
    if chosen not in models and alias not in models:
        installed = " Your installed models: " + ", ".join(models) + "." if models else " You have no installed models yet."
        error = ProviderCallError("model_not_found", missing_model_message(config.base_url, chosen) + installed)
        error.models = models
        raise error
    return models


def local_vision_config(workspace_id: str):
    from app.core.local_mode import is_local

    if not is_local():
        return None
    try:
        resolved = resolve_llm_config(workspace_id)
    except FounderLLMConfigError:
        raise FounderLLMConfigError("Choose a vision model and add your key in Settings, then retry your task.") from None
    if resolved.provider not in {"openai", "openai_compatible", "anthropic"}:
        raise FounderLLMConfigError("Choose an Anthropic or OpenAI-compatible vision model in Settings > Keys and connections, then retry your task.")
    config = to_provider_config(workspace_id, resolved_config=resolved)
    if config is None:
        raise FounderLLMConfigError("Update your model key in Settings, then retry your task.")
    if config._local_role_models:
        from app.core.providers_local import LocalProviderError, model_for_purpose

        try:
            model = model_for_purpose("device.vision", "pro", config._local_role_models)
        except LocalProviderError:
            raise FounderLLMConfigError("Choose a vision model in Settings, then retry your task.") from None
        config.deployment_pro = config.deployment_flash = model
        config._local_role_models = {**config._local_role_models, "planning": model}
    return config


def register_workspace_llm_resolver() -> None:
    """Hook founder BYOK into transmit; local calls use the local spending ledger."""
    try:
        from llm_gateway.sole import set_usage_recorder, set_workspace_llm_resolver
    except Exception:
        return
    set_workspace_llm_resolver(to_provider_config)
    from app.core.local_mode import is_local

    if is_local():
        set_usage_recorder(None)
        return
    try:
        from app.llm.pricing import record_usage

        set_usage_recorder(record_usage)
    except Exception:
        pass


PLATFORM_DEFAULT_MODEL = "DeepSeek-V4-Pro"
PLATFORM_DEFAULT_LABEL = "DeepSeek V4 Pro (included)"


def platform_offer() -> dict[str, str]:
    from llm_gateway.provider_foundry import load_provider_config_from_env

    platform = load_provider_config_from_env()
    model = platform.deployment_pro
    return {
        "provider": platform.provider,
        "model": model,
        "label": f"{model} (included)",
        "disclosure": (
            f"Supymem uses {model} through "
            f"{'Azure AI Foundry' if platform.provider == 'azure_foundry' else platform.provider}. "
            "Bring your own key to switch provider or model. "
            "Keys are stored in an encrypted vault; we keep a reference, not the secret."
        ),
    }


DEFAULT_MODELS: dict[str, list[str]] = {
    "openai": ["gpt-5.6-sol", "terra", "luna"],
    "anthropic": [DEFAULT_ANTHROPIC_MODEL, "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"],
    "google": ["gemini-3.6-flash", "gemini-3.1-pro"],
    "deepseek_direct": ["DeepSeek-V4-Flash", "deepseek-v4-flash", "deepseek-chat", "DeepSeek-V4-Pro"],
    "xai": ["grok-4.5"],
    "openrouter": ["openrouter/auto"],
    "local": ["llama3.2", "qwen2.5"],
    "azure_foundry": ["DeepSeek-V4-Pro"],
    "openai_compatible": ["gpt-4o-mini"],
}

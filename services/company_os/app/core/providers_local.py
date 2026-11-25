"""Local provider settings and migration into the shared encrypted store."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import logging
import os
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from app.core.local_mode import is_local, is_offline
from app.core.local_process import check_private, file_lock, open_private
from app.core.local_secrets import ConnectionSecretRequired, acknowledge_legacy, get_secret, legacy_secret, read_settings, update_settings

Provider = Literal["openai", "azure_openai", "anthropic", "openai_compatible"]
Role = Literal["chat", "planning", "extraction", "vision", "realtime"]
PROVIDER_ENV = {
    "openai": ("OPENAI_API_KEY", "OPENAI_BASE_URL"),
    "azure_openai": ("AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT"),
    "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"),
    "openai_compatible": ("OPENAI_COMPATIBLE_API_KEY", "OPENAI_COMPATIBLE_BASE_URL"),
}
ROLES = ("chat", "planning", "extraction", "vision", "realtime")
PROVIDER_PROBE_TIMEOUT_SECONDS = 10
_FALLBACK_NOTICES: set[Path] = set()
_VERIFIED_PROBE = "verified_probe"
_tested_drafts: dict[Path, str] = {}


class LocalProviderError(ValueError):
    """Safe to show without exposing the settings or their credentials."""


class ProviderSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Provider
    base_url: str | None = None
    api_key: SecretStr | None = Field(default=None, repr=False)
    models: dict[Role, str] = Field(default_factory=dict)
    catalog_model: str | None = None
    context_window: int | None = Field(default=None, strict=True, ge=2048, le=262144)


def _require_local() -> None:
    if not is_local():
        raise LocalProviderError("Your local settings are disabled. Enable local mode, then retry.")


def _loopback(host: str | None) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def validate_endpoint(value: str) -> str:
    try:
        parsed = urlsplit(value.strip().rstrip("/"))
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or len(value) > 2048
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or any(character.isspace() for character in value)
                or parsed.scheme == "http" and not _loopback(parsed.hostname)):
            raise ValueError
        if parsed.port == 0:
            raise ValueError
    except ValueError:
        raise LocalProviderError("Use an HTTPS provider URL, or an HTTP loopback URL, without credentials or a query.") from None
    return parsed.geturl()


def _provider_endpoint(provider: str, value: str | None, *, show_invalid_azure_url: bool = False) -> str | None:
    from llm_gateway.provider_foundry import normalize_base_url

    if provider != "azure_openai":
        return normalize_base_url(provider, value or "") or None
    if not value:
        return None
    try:
        return normalize_base_url("azure_foundry", validate_endpoint(value), local_azure=True)
    except ValueError:
        if show_invalid_azure_url:
            return validate_endpoint(value)
        raise LocalProviderError("In Keys and connections, paste your Azure resource endpoint into Service URL and put your deployment name in Chat model, then retry.") from None


def _root() -> Path:
    from app.config import data_root

    return data_root() / "vault" / "providers"


@contextmanager
def _locked(root: Path):
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = open_private(root / ".lock", os.O_RDWR | os.O_CREAT)
        with os.fdopen(descriptor, "rb") as lock:
            check_private(lock)
            with file_lock(lock):
                yield
    except OSError:
        raise LocalProviderError("Your settings could not be opened or saved. Check your data directory permissions, then retry.") from None


def _read_private(path: Path) -> bytes:
    with os.fdopen(open_private(path, os.O_RDONLY), "rb") as stream:
        check_private(stream)
        payload = stream.read(1024 * 1024 + 1)
        if len(payload) > 1024 * 1024:
            raise ValueError
        return payload


def _cipher(root: Path, *, create: bool = False) -> Fernet:
    path = root / "machine.secret"
    if os.name == "nt":
        encoded = legacy_secret("machine.secret")
        if encoded is None:
            raise LocalProviderError("Open Keys and connections and re-enter your model key. Keep your previous encrypted provider files for recovery.")
        secret = base64.b64decode(encoded, validate=True)
    elif create and not path.exists():
        descriptor = open_private(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(os.urandom(32))
            stream.flush()
            os.fsync(stream.fileno())
    if os.name != "nt":
        secret = _read_private(path)
    if len(secret) != 32:
        raise ValueError
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"local-provider-keys-v1").derive(secret)
    return Fernet(base64.urlsafe_b64encode(key))


def _validate_document(document: dict) -> dict:
    from core.logging import register_local_secret

    if (not isinstance(document, dict) or not {"provider", "providers"} <= set(document)
            or set(document) - {"provider", "providers", "sources"}
            or document["provider"] not in {None, *PROVIDER_ENV}
            or not isinstance(document["providers"], dict)):
        raise ValueError
    for provider, settings in document["providers"].items():
        fields = dict(settings)
        account = fields.pop("key_ref", None)
        if account is not None:
            uuid.UUID(account)
        ProviderSettings.model_validate({**fields, "provider": provider})
        register_local_secret(fields.get("api_key") or "")
    sources = document.setdefault("sources", {})
    if not isinstance(sources, dict) or any(value not in {"app", "env"} for value in sources.values()):
        raise ValueError
    if document["provider"] is not None:
        sources.setdefault("provider", "app")
    for provider, fields in document["providers"].items():
        for name in ("base_url", "api_key", "context_window"):
            if name in fields or name == "api_key" and "key_ref" in fields:
                sources.setdefault(f"{provider}.{name}", "app")
        for role in fields.get("models", {}):
            sources.setdefault(f"{provider}.models.{role}", "app")
    return document


def _check_key_fields(key: str, endpoint: str | None, models: dict) -> None:
    key = key.strip()
    if key and any(key in value for value in (endpoint or "", *models.values())):
        raise LocalProviderError("Keep your key in the key field. Enter your provider URL and model names separately, then retry.")


def _read_legacy(root: Path) -> dict:
    path = root / "settings.enc"
    if not path.exists() and not path.is_symlink():
        return {"provider": None, "providers": {}}
    if os.name == "nt" and legacy_secret("machine.secret") is None:
        return {"provider": None, "providers": {}}
    try:
        return _validate_document(json.loads(_cipher(root).decrypt(_read_private(path))))
    except (OSError, ValueError, TypeError, InvalidToken):
        raise LocalProviderError("Your saved keys cannot be read. Restore your provider files and machine secret from a backup, then retry.") from None


def _load(root: Path) -> dict:
    path = root / "settings.json"
    try:
        from app.core import local_secrets

        with local_secrets._locked() as settings_root:
            _, stored_values, stored_secrets = local_secrets._load(settings_root)
        saved = stored_values.get("model")
        if saved:
            document = _validate_document(saved)
            keys = (stored_secrets or {}).get("model", {})
            if keys.get(_VERIFIED_PROBE):
                document[_VERIFIED_PROBE] = keys[_VERIFIED_PROBE]
            for provider, fields in document["providers"].items():
                if provider in keys:
                    fields["api_key"] = keys[provider]
            if stored_secrets is None or not (path.exists() or path.is_symlink()):
                return document
        legacy = _validate_document(json.loads(_read_private(path))) if path.exists() or path.is_symlink() else _read_legacy(root)
        if not saved:
            document = legacy
        if not legacy["providers"] or local_secrets._shell_material() is None:
            return document
        encrypted = root / "settings.enc"
        if not saved and encrypted.exists():
            previous = _read_legacy(root)
            if not previous["providers"]:
                return document
            for provider, fields in previous["providers"].items():
                current = legacy["providers"][provider]
                if any(current.get(name) != fields.get(name) for name in ("base_url", "models")):
                    raise ValueError
                if "api_key" in fields:
                    current["api_key"] = fields["api_key"]
        completed = []
        missing = False
        changed = not saved
        for provider, fields in legacy["providers"].items():
            account = fields.pop("key_ref", None)
            if saved and not account:
                continue
            current = document["providers"].get(provider)
            if saved and (current is None or "api_key" in current or
                    _provider_endpoint(provider, fields.get("base_url")) != _provider_endpoint(provider, current.get("base_url"))):
                completed.append(account)
                continue
            key = (legacy_secret(account) if account else None) or fields.get("api_key")
            if account and not key:
                missing = True
                continue
            _check_key_fields(key or "", current.get("base_url"), current.get("models") or {})
            if key is not None:
                current["api_key"] = key
                changed = True
            if account:
                completed.append(account)
        if changed:
            _write(root, document)
        if not missing and (changed or completed):
            acknowledge_legacy(completed)
            for filename in ("settings.json", "settings.enc", "machine.secret"):
                (root / filename).unlink(missing_ok=True)
        return document
    except (OSError, ValueError, TypeError, AttributeError, KeyError, InvalidTag) as exc:
        if isinstance(exc, LocalProviderError):
            raise
        raise LocalProviderError("Your saved provider settings could not be read or migrated. Check your data folder permissions or restore a backup, then restart your app.") from None


def _read(root: Path) -> dict:
    with _locked(root):
        return _load(root)


def _write(root: Path, document: dict) -> None:
    try:
        metadata = {"provider": document["provider"], "providers": {
            provider: {name: value for name, value in fields.items() if name not in {"api_key", "key_ref"}}
            for provider, fields in document["providers"].items()
        }}
        metadata["sources"] = document.get("sources", {})
        secrets = {provider: fields.get("api_key", "") for provider, fields in document["providers"].items() if "api_key" in fields}
        from app.core import local_secrets

        with local_secrets._locked() as settings_root:
            _, stored_values, stored_secrets = local_secrets._load(settings_root)
        previous = stored_values.get("model", {}).get("providers", {})
        secrets.update({provider: "" for provider in previous if provider not in metadata["providers"]})
        verified = document.get(_VERIFIED_PROBE)
        if _matches_probe(document, document["provider"], verified):
            secrets[_VERIFIED_PROBE] = verified
        elif (stored_secrets or {}).get("model", {}).get(_VERIFIED_PROBE):
            secrets[_VERIFIED_PROBE] = ""
        local_secrets._update_settings("model", metadata, secrets)
    except ConnectionSecretRequired as exc:
        raise LocalProviderError(str(exc)) from None
    except (OSError, ValueError, TypeError):
        raise LocalProviderError("Your keys could not be saved securely. Open Keys and connections, check your data folder permissions and retry.") from None


def _effective(provider: str, document: dict, *, show_invalid_azure_url: bool = False) -> ProviderSettings:
    stored = document["providers"].get(provider, {})
    endpoint = _provider_endpoint(provider, stored.get("base_url"), show_invalid_azure_url=show_invalid_azure_url)
    endpoint = validate_endpoint(endpoint) if endpoint else None
    key = (stored.get("api_key") or "").strip()
    models = dict(stored.get("models") or {})
    if provider == "anthropic":
        from llm_gateway.local_transport import DEFAULT_ANTHROPIC_MODEL
        models.setdefault("chat", DEFAULT_ANTHROPIC_MODEL)
    chat = models.get("chat", "")
    for role in ("planning", "extraction", "vision"):
        models.setdefault(role, chat)
    catalog_model = stored.get("catalog_model") if provider == "azure_openai" else None
    _check_key_fields(key, endpoint, {**models, "catalog": catalog_model or ""})
    return ProviderSettings(provider=provider, base_url=endpoint, api_key=SecretStr(key), models=models,
        catalog_model=catalog_model, context_window=stored.get("context_window"))


def _model_view() -> dict:
    _require_local()
    with _locked(_root()):
        document = _load(_root())
    return {"values": {"provider": document["provider"], "providers": {
        provider: {name: value for name, value in fields.items() if name not in {"api_key", "key_ref"}}
        for provider, fields in document["providers"].items()
    }}, "secrets_set": {provider: bool(fields.get("api_key")) for provider, fields in document["providers"].items()},
        "sources": document.get("sources", {})}


def _model_document() -> dict:
    read_settings("model")
    return _read(_root())


def _update_model(values: dict, secrets: dict, *, source: str) -> dict:
    _require_local()
    root = _root()
    with _locked(root):
        document = _load(root)
        sources = document.setdefault("sources", {})
        selected = values.get("provider")
        if selected is not None and selected not in PROVIDER_ENV and (source == "app" or sources.get("provider") != "app"):
            raise LocalProviderError("Choose your AI provider in Keys and connections, then import your .env again.")
        dirty = False
        if selected is not None and (source == "app" or sources.get("provider") != "app"):
            dirty = document["provider"] != selected or sources.get("provider") != source
            document["provider"] = selected
            sources["provider"] = source
        providers = values.get("providers", {})
        for provider in providers.keys() | secrets.keys():
            previous = document["providers"].get(provider, {})
            supplied = providers.get(provider, {})
            fields = {name: value for name, value in supplied.items() if name != "models"
                and (source == "app" or sources.get(f"{provider}.{name}") != "app")}
            models = {role: value for role, value in supplied.get("models", {}).items()
                if source == "app" or sources.get(f"{provider}.models.{role}") != "app"}
            if models:
                fields["models"] = models
            if provider in secrets and (source == "app" or sources.get(f"{provider}.api_key") != "app"):
                if source == "app" or _provider_endpoint(provider, supplied.get("base_url")) == _provider_endpoint(
                        provider, fields.get("base_url", previous.get("base_url")), show_invalid_azure_url=True):
                    fields["api_key"] = secrets[provider]
            if not fields:
                continue
            settings = _validated_settings({"provider": provider, **fields})
            candidate = _candidate(settings, document)
            for key in secrets.values():
                _check_key_fields(key, candidate["base_url"], {**candidate["models"], "catalog": candidate.get("catalog_model", "")})
            if (candidate == previous
                    and all(sources.get(f"{provider}.{name}") == source for name in fields if name != "models")
                    and all(sources.get(f"{provider}.models.{role}") == source for role in models)):
                continue
            for name in fields:
                if name != "models":
                    sources[f"{provider}.{name}"] = source
            if source == "app":
                if "api_key" in fields:
                    sources[f"{provider}.base_url"] = "app"
                if candidate["base_url"] != _provider_endpoint(provider, previous.get("base_url"), show_invalid_azure_url=True):
                    sources[f"{provider}.api_key"] = "app"
            for name in ("base_url", "api_key"):
                if name not in previous and not (name == "api_key" and "key_ref" in previous):
                    sources.setdefault(f"{provider}.{name}", source)
            sources.update({f"{provider}.models.{role}": source for role in models})
            document["providers"][provider] = candidate
            _write(root, document)
            dirty = False
        if dirty:
            _write(root, document)
    return _model_view()


def _usable(settings: ProviderSettings) -> bool:
    keyless = settings.provider == "openai_compatible" and _loopback(urlsplit(settings.base_url or "").hostname)
    return bool(settings.base_url and settings.models.get("chat") and (settings.api_key.get_secret_value() or keyless))


def _probe_identity(settings: ProviderSettings) -> str | None:
    if not _usable(settings):
        return None
    fields = settings.model_dump(exclude={"api_key"})
    fields["api_key"] = settings.api_key.get_secret_value()
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def _matches_probe(document: dict, provider: str | None, identity: str | None) -> bool:
    return (provider is not None and identity is not None and document["provider"] == provider
            and identity == _probe_identity(_effective(provider, document, show_invalid_azure_url=True)))


def configured_providers() -> dict:
    _require_local()
    from app.core.local_secrets import _shell_material
    from app.core.local_env_import import import_local_env

    if _shell_material() is None:
        return {"provider": None, "providers": {}, "sources": {}, "credentials_pending": True}
    import_local_env()
    root = _root()
    with _locked(root):
        document = _load(root)
    verified = document.get(_VERIFIED_PROBE)
    selected = document["provider"]
    if selected is not None and selected not in PROVIDER_ENV:
        raise LocalProviderError("Choose OpenAI, Azure OpenAI, Anthropic, or an OpenAI-compatible provider in Keys and connections.")
    providers = {}
    for provider in PROVIDER_ENV:
        try:
            settings = _effective(provider, document)
            configured = _usable(settings)
        except LocalProviderError:
            if provider != "azure_openai":
                raise
            settings = _effective(provider, document, show_invalid_azure_url=True)
            configured = False
        providers[provider] = {"configured": configured, "api_key_set": bool(settings.api_key.get_secret_value()),
            "verified": configured and provider == selected and verified is not None and _probe_identity(settings) == verified,
            "base_url": settings.base_url, "models": settings.models, "catalog_model": settings.catalog_model,
            **({"context_window": settings.context_window} if settings.context_window is not None else {})}
    return {"provider": selected, "providers": providers, "sources": document.get("sources", {})}


def _validated_settings(values: dict) -> ProviderSettings:
    try:
        settings = ProviderSettings.model_validate(values)
        models = list(settings.models.values())
        if settings.catalog_model is not None:
            models.append(settings.catalog_model)
        if (any(not model.strip() or len(model) > 256 or any(character.isspace() or not character.isprintable() for character in model)
                for model in models)
                or settings.catalog_model is not None and settings.provider != "azure_openai"
                or settings.api_key is not None and len(settings.api_key.get_secret_value()) > 16384):
            raise ValueError
    except (ValueError, TypeError) as exc:
        if isinstance(exc, ValidationError) and any(item["loc"] == ("context_window",) for item in exc.errors(include_input=False)):
            raise LocalProviderError("Enter a context window from 2,048 to 262,144 tokens in Settings > Keys and connections > Your AI provider, then save again.") from None
        raise LocalProviderError("Choose a supported provider and enter your model names and key in Keys and connections, then save again.") from None
    return settings


def _candidate(settings: ProviderSettings, document: dict) -> dict:
    previous = document["providers"].get(settings.provider, {})
    previous_endpoint = _provider_endpoint(settings.provider, previous.get("base_url"), show_invalid_azure_url=True)
    endpoint = _provider_endpoint(settings.provider, settings.base_url if settings.base_url is not None else previous.get("base_url"))
    models = {**previous.get("models", {}), **settings.models}
    if settings.provider == "anthropic":
        from llm_gateway.local_transport import DEFAULT_ANTHROPIC_MODEL
        models.setdefault("chat", DEFAULT_ANTHROPIC_MODEL)
    changed_endpoint = endpoint != previous_endpoint
    key = "" if changed_endpoint else previous.get("api_key")
    if settings.api_key is not None:
        key = settings.api_key.get_secret_value().strip()
    catalog_model = settings.catalog_model if "catalog_model" in settings.model_fields_set else previous.get("catalog_model")
    _check_key_fields(key or "", endpoint, {**models, "catalog": catalog_model or ""})
    context_window = settings.context_window if "context_window" in settings.model_fields_set else previous.get("context_window")
    return {"base_url": validate_endpoint(endpoint) if endpoint else None, "models": models,
        **({"api_key": key} if key is not None else {}),
        **({"catalog_model": catalog_model} if catalog_model is not None else {}),
        **({"context_window": context_window} if context_window is not None else {})}


def save_settings(**values) -> dict:
    return _store_settings(values)


def _store_settings(values: dict, *, sample: bool = False) -> dict:
    _require_local()
    settings = _validated_settings(values)
    root = _root()
    with _locked(root):
        document = _load(root)
        if sample and settings.provider in document["providers"]:
            raise LocalProviderError("Your saved service needs attention. Enter your provider URL, chat model and key in Keys and connections, then save and continue.")
        previous = document["providers"].get(settings.provider, {})
        candidate = _candidate(settings, document)
        sources = dict(document.get("sources", {}))
        sources["provider"] = "app"
        for name in ("base_url", "api_key"):
            if name not in previous or getattr(settings, name) is not None or name == "base_url" and settings.api_key is not None:
                sources[f"{settings.provider}.{name}"] = "app"
        if candidate["base_url"] != _provider_endpoint(settings.provider, previous.get("base_url"), show_invalid_azure_url=True):
            sources[f"{settings.provider}.api_key"] = "app"
        for name in settings.model_fields_set & {"context_window", "catalog_model"}:
            sources[f"{settings.provider}.{name}"] = "app"
        sources.update({f"{settings.provider}.models.{role}": "app" for role in settings.models})
        document["provider"] = settings.provider
        document["providers"][settings.provider] = candidate
        document["sources"] = sources
        tested = _tested_drafts.get(root)
        if _matches_probe(document, settings.provider, tested):
            document[_VERIFIED_PROBE] = tested
        _write(root, document)
        _tested_drafts.pop(root, None)
        (root / "sample").unlink(missing_ok=True)
        from app.core.local_secrets import _atomic_write

        _atomic_write(root / "live", b"1")
    return configured_providers()


def clear_settings() -> bool:
    _require_local()
    root = _root()
    with _locked(root):
        document = _load(root)
        selected = document["provider"]
        if selected is None:
            return False
        document["providers"].pop(selected, {})
        document["provider"] = None
        sources = {name: source for name, source in document.get("sources", {}).items()
                   if not name.startswith(f"{selected}.")}
        sources["provider"] = "app"
        document["sources"] = sources
        _write(root, document)
        (root / "live").unlink(missing_ok=True)
    return True


def load_local_config():
    _require_local()
    if _sample_selected():
        from llm_gateway.provider_foundry import ProviderConfig

        config = ProviderConfig(provider="local", source="founder_override",
            deployment_pro="sample", deployment_flash="sample", deployment_embed="")
        config._local_role_models = {role: config.deployment_pro if role == "planning" else config.deployment_flash for role in ROLES if role != "realtime"}
        return config
    document = _model_document()
    provider = document["provider"]
    if provider not in PROVIDER_ENV:
        raise LocalProviderError("Choose your provider in Keys and connections, save your key and chat model, then retry.")
    settings = _effective(provider, document)
    if not _usable(settings):
        raise LocalProviderError("Your provider is not configured. Save your key, provider URL, and chat model in Keys and connections, then retry.")

    return _provider_config(settings)


def _provider_config(settings: ProviderSettings):
    from llm_gateway.provider_foundry import DEFAULT_LOOPBACK_CONTEXT, ProviderConfig

    chat = settings.models["chat"]
    config = ProviderConfig(provider="azure_foundry" if settings.provider == "azure_openai" else settings.provider,
        base_url=settings.base_url, api_key=settings.api_key if settings.api_key.get_secret_value() else SecretStr("not-required"),
        deployment_pro=settings.models.get("planning", chat), deployment_flash=settings.models.get("extraction", chat),
        timeout_seconds=600 if settings.provider == "openai_compatible" and _loopback(urlsplit(settings.base_url or "").hostname) else 120,
        context_window=settings.context_window or DEFAULT_LOOPBACK_CONTEXT,
        reasoning_effort="", source="founder_override")
    config._local_role_models = dict(settings.models)
    config._local_azure_catalog_model = settings.catalog_model
    return config


def credential_identity(config) -> str:
    return "local:" + hashlib.sha256(json.dumps({"key": config.api_key.get_secret_value(),
        "provider": config.provider, "endpoint": config.base_url, "models": config._local_role_models}, sort_keys=True).encode()).hexdigest()


def model_for_purpose(purpose: str, tier: str, models: dict[str, str]) -> str:
    parts = purpose.split(".")
    if "vision" in parts or purpose == "device.browser_ai.actor":
        role = "vision"
    elif "realtime" in parts or "voice" in parts:
        role = "realtime"
    elif "chat" in parts or purpose in {"env_chat_completion", "byok_chat_completion", "app.proxy"}:
        role = "chat"
    else:
        role = "planning" if tier == "pro" else "extraction"
    model = models.get(role)
    if not model:
        raise LocalProviderError("Choose a model for this role in Keys and connections, then retry.")
    return model


def _local_api(request: Request) -> None:
    if not is_local():
        raise HTTPException(404, "Your local settings are disabled. Enable local mode, then retry.")
    origin = request.headers.get("origin")
    try:
        parsed_origin = urlsplit(origin) if origin is not None else None
        safe_origin = parsed_origin is None or (parsed_origin.scheme in {"http", "https"}
            and _loopback(parsed_origin.hostname) and parsed_origin.username is None
            and parsed_origin.password is None and not parsed_origin.query and not parsed_origin.fragment)
    except ValueError:
        safe_origin = False
    if (not request.client or not _loopback(request.client.host) or not _loopback(request.url.hostname)
            or not safe_origin):
        raise HTTPException(403, "Open your setup page on this machine using a loopback address, then retry.")


router = APIRouter(dependencies=[Depends(_local_api)], tags=["local-llm-settings"])


@router.get("/local/llm-settings")
def get_local_settings():
    try:
        return configured_providers()
    except LocalProviderError as exc:
        raise HTTPException(422, str(exc)) from None


async def _settings_body(request: Request) -> dict:
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(415, "Send your settings as JSON, then retry.")
    try:
        payload = await request.body()
        if len(payload) > 65536:
            raise ValueError
        values = json.loads(payload)
        if not isinstance(values, dict):
            raise TypeError
        return values
    except (ValueError, TypeError):
        raise HTTPException(422, "Send your provider, key, URL, and model names as JSON, then retry.") from None


@router.put("/local/llm-settings")
async def put_local_settings(request: Request):
    try:
        return save_settings(**await _settings_body(request))
    except LocalProviderError as exc:
        raise HTTPException(422, str(exc)) from None


def _provider_view() -> dict:
    if _sample_selected():
        return {"configured": True, "verified": False, "sample_mode": True, "sample_selected": True, "values": {}, "secrets_set": {"api_key": False}}
    status = configured_providers()
    selected = status["providers"].get(status["provider"], {})
    from app.core.local_secrets import _shell_material

    material = _shell_material()
    unreadable = (material is not None
                  and f"{status['provider']}.api_key" in status["sources"] and not selected.get("api_key_set"))
    return {"configured": selected.get("configured", False), "verified": selected.get("verified", False), "sample_mode": True, "sample_selected": False,
        **({"credentials_pending": True} if status.get("credentials_pending") else {}),
        "values": {"provider": status["provider"], "model": selected.get("models", {}).get("chat"),
            "base_url": selected.get("base_url"), "catalog_model": selected.get("catalog_model"),
            **({"context_window": selected["context_window"]} if "context_window" in selected else {})},
        "secrets_set": {"api_key": selected.get("api_key_set", False)}, **({"unreadable_secrets": ["api_key"]} if unreadable else {}),
        "sources": {field: status["sources"][name] for field, name in {
            "provider": "provider", "model": f"{status['provider']}.models.chat",
            "base_url": f"{status['provider']}.base_url", "api_key": f"{status['provider']}.api_key",
            "context_window": f"{status['provider']}.context_window",
            "catalog_model": f"{status['provider']}.catalog_model",
        }.items() if name in status["sources"]}}


@router.get("/local/providers")
def get_providers():
    from app.llm.founder_provider import DEFAULT_MODELS

    try:
        view = _provider_view()
        if view.get("credentials_pending"):
            raise HTTPException(503, {"code": "local_credentials_pending",
                "message": "Your saved keys are still opening. Unlock your system keychain, then choose Retry."},
                headers={"Cache-Control": "no-store"})
        return JSONResponse({**view, "default_models": DEFAULT_MODELS}, headers={"Cache-Control": "no-store"})
    except LocalProviderError as exc:
        raise HTTPException(422, str(exc)) from None


@router.post("/local/providers")
@router.put("/local/providers")
async def save_provider(request: Request):
    values = await _provider_body(request)
    try:
        save_settings(**values)
        return JSONResponse(_provider_view(), headers={"Cache-Control": "no-store"})
    except LocalProviderError as exc:
        raise HTTPException(422, str(exc)) from None


async def _provider_body(request: Request) -> dict:
    values = await _settings_body(request)
    context_window = values.get("context_window")
    if isinstance(context_window, str) and context_window.isascii() and context_window.isdecimal() and len(context_window) <= 6:
        values["context_window"] = int(context_window)
    if "model" in values:
        if "models" in values:
            raise HTTPException(422, "Send your chat model once, then retry.")
        values["models"] = {"chat": values.pop("model")}
    return values


def _sample_mode() -> bool:
    return is_offline()


def _sample_selected() -> bool:
    if not is_local():
        return False
    try:
        return _read_private(_root() / "sample") == b"1"
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        raise LocalProviderError("Your sample settings could not be read. Check your data folder permissions, then retry.") from None


@router.post("/local/providers/sample")
async def sample_provider(request: Request):
    await _settings_body(request)
    temporary = None
    try:
        root = _root()
        with _locked(root):
            with tempfile.NamedTemporaryFile(dir=root, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(b"1")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, root / "sample")
            temporary = None
        return JSONResponse(_provider_view(), headers={"Cache-Control": "no-store"})
    except LocalProviderError as exc:
        raise HTTPException(422, str(exc)) from None
    except (OSError, ValueError):
        raise HTTPException(422, "Your sample settings could not be saved. Check your data folder permissions, then retry.") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@router.post("/local/providers/test")
async def test_provider(request: Request):
    if _sample_mode():
        raise HTTPException(409, {"code": "sample_mode", "message": "You are using sample answers. Open Keys and connections, save your AI service, then test your connection."})
    from app.credits import metered
    from app.credits.errors import CreditsUnavailable
    from app.credits.estimate import actual_llm_micro, estimate_llm_micro
    from app.credits.local_meter import LocalSpendLimit
    from app.llm.founder_provider import compatible_provider_models, keyless_local_config
    from llm_gateway.errors import ProviderCallError
    from llm_gateway.routing import resolve_purpose_config, sol_output_cap
    from llm_gateway.sole import byok_chat_completion
    from llm_gateway.usage import extract_confirmed_token_usage

    root = _root()
    purpose = "venture.chat.route"
    try:
        settings = _validated_settings(await _provider_body(request))
        document = _model_document()
        candidate = _candidate(settings, document)
        settings = _effective(settings.provider, {"providers": {settings.provider: candidate}})
        if not _usable(settings):
            code, label = ("service_url_required", "Service URL") if not settings.base_url else (
                ("chat_model_required", "Chat model") if not settings.models.get("chat") else ("provider_key_required", "Model API key"))
            raise HTTPException(422, {"code": code, "message": f"In Keys and connections > Your AI provider, enter your {label}, then test again."})
        try:
            config = resolve_purpose_config(purpose, _provider_config(settings), tier="flash")
        except ValueError:
            raise LocalProviderError("In Keys and connections, check Chat model and the Azure model ID, then test again.") from None
        identity = _probe_identity(settings)
        saved = _matches_probe(document, settings.provider, identity)
        if saved:
            with _locked(root):
                current = _load(root)
                if _matches_probe(current, settings.provider, identity) and current.pop(_VERIFIED_PROBE, None):
                    _write(root, current)
        else:
            _tested_drafts.pop(root, None)
    except LocalProviderError as exc:
        raise HTTPException(422, str(exc)) from None

    loopback = keyless_local_config(config)
    if not loopback:
        config.timeout_seconds = PROVIDER_PROBE_TIMEOUT_SECONDS
    messages = [{"role": "user", "content": 'Reply with only this JSON object: {"connected":true}.'}]
    max_tokens = sol_output_cap(config, 400)
    config.max_output_tokens = max_tokens
    model = config.catalog_model

    def actual_cost(reply):
        usage = extract_confirmed_token_usage(reply.usage)
        if usage is None:
            raise CreditsUnavailable()
        if loopback:
            return 0
        return actual_llm_micro(model, prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"], cached_tokens=usage["cached_tokens"])

    try:
        models = None
        if loopback:
            discovery = config.model_copy(update={"timeout_seconds": PROVIDER_PROBE_TIMEOUT_SECONDS})
            async with asyncio.timeout(PROVIDER_PROBE_TIMEOUT_SECONDS):
                models = await compatible_provider_models(discovery)
        async with asyncio.timeout(config.timeout_seconds):
            reply = await metered.call(lambda: byok_chat_completion(messages, config=config,
                purpose=purpose, tier="flash", max_tokens=max_tokens, response_schema={"type": "json_object"}, single_attempt=True),
                purpose=purpose, provider=config.provider, model=model, max_tokens=max_tokens,
                estimate_micro=0 if loopback else estimate_llm_micro(model, prompt_tokens=512, max_tokens=max_tokens),
                actual_micro=actual_cost)
        try:
            choice = reply.choices[0]
            output = json.loads(choice.message.content) if getattr(choice, "finish_reason", "stop") == "stop" else None
        except (AttributeError, IndexError, TypeError, ValueError):
            output = None
        if not isinstance(output, dict) or output.get("connected") is not True:
            raise ProviderCallError("model_json_unsupported", "", status=400)
        if identity is not None:
            with _locked(root):
                current = _load(root)
                matches_saved = _matches_probe(current, settings.provider, identity)
                if matches_saved:
                    current[_VERIFIED_PROBE] = identity
                    _write(root, current)
                elif not saved:
                    _tested_drafts[root] = identity
                saved = matches_saved
        result = {"ok": True}
        if saved:
            result["verified"] = True
        if models is not None:
            result["models"] = models[:50]
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except HTTPException:
        raise
    except TimeoutError:
        if loopback:
            raise HTTPException(504, "Your connection test timed out. Start your model server and load your selected model, then test again in Keys and connections.") from None
        raise HTTPException(504, "Your connection test timed out. Check your service address in Keys and connections, then choose Test connection again.") from None
    except LocalSpendLimit as exc:
        raise HTTPException(409, {"code": exc.code, "message": "In Keys and connections, open Your monthly spend cap and raise or remove Monthly limit (USD), then test again."}) from None
    except CreditsUnavailable:
        raise HTTPException(502, {"code": "usage_unconfirmed", "message": "Your model's usage could not be confirmed. In Keys and connections, choose another Chat model, then test again."}) from None
    except LocalProviderError as exc:
        raise HTTPException(422, str(exc)) from None
    except ProviderCallError as exc:
        code = exc.code
        if code == "invalid_structured_output" or code == "provider_error" and exc.status in {400, 422}:
            code = "model_json_unsupported"
        elif code == "provider_error" and exc.status == 404:
            code = "model_not_found"
        copy = {
            "model_json_unsupported": "In Keys and connections, choose a Chat model that can return JSON, then test again.",
            "model_not_found": "In Keys and connections, check Chat model or load the model in your model server, then test again.",
            "bad_api_key": "In Keys and connections, update your model API key to match your service, then test again.",
        }
        detail = {"code": code, "message": copy.get(code, "In Keys and connections, check Service URL and Chat model, then test again.")}
        available = getattr(exc, "models", None) or models
        if available is not None:
            detail["models"] = available[:50]
        raise HTTPException(502, detail) from None
    except Exception:
        raise HTTPException(502, "Your connection could not be tested. Open Keys and connections, check your model API key, chat model and service URL, then try again.") from None


@router.get("/setup/status")
def setup_status():
    offline = is_offline()
    replay = os.environ.get("LLM_CASSETTE_MODE") == "replay"
    llm_mode = ("sample" if _sample_selected() else "replay" if replay else "offline" if offline
                else "live" if _provider_view()["configured"] else "unconfigured")
    return JSONResponse({"mode": "local", "llm_mode": llm_mode,
        "email_mode": os.environ.get("EMAIL_PROVIDER", "log")}, headers={"Cache-Control": "no-store"})

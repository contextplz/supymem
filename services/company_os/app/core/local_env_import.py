"""Import owner configuration into the shared local settings store.

Precedence, per field: app saves win; otherwise process environment (source runs
only), data-folder .env, then repository .env. Env-owned fields refresh on import.
Blank entries do not erase saved fields. Model settings use section ``model``:
values hold provider and providers metadata; secret names are provider slugs.
Both API and worker call import_local_env at startup; reads also import lazily.
The resolver canonicalizes LLM_OFFLINE for remaining process-level readers.
"""

from __future__ import annotations

import io
import json
import logging
import os
import threading
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

from app.core.local_mode import is_local

_IMPORTING = threading.local()
_UNKNOWN: set[str] = set()
_FIELDS = {
    "TAVILY_API_KEY": ("search", "api_key", True),
    "EXA_API_KEY": ("search", "api_key", True),
    "SUPYMEM_SEARCH_PROVIDER": ("search", "provider", False),
    "FIRECRAWL_SELF_HOSTED_URL": ("search", "firecrawl_url", False),
    "FIRECRAWL_API_KEY": ("search", "firecrawl_key", True),
    "FIRECRAWL_SELF_HOSTED_TOKEN": ("search", "firecrawl_key", True),
    "SMTP_HOST": ("smtp", "host", False),
    "SMTP_PORT": ("smtp", "port", False),
    "SMTP_FROM": ("smtp", "user", False),
    "SMTP_USER": ("smtp", "user", False),
    "SMTP_USERNAME": ("smtp", "user", False),
    "SMTP_PASSWORD": ("smtp", "password", True),
    "SMTP_SENDER_NAME": ("smtp", "sender_name", False),
    "SMTP_POSTAL_ADDRESS": ("smtp", "postal_address", False),
    "SENDER_NAME": ("smtp", "sender_name", False),
    "SENDER_POSTAL_ADDRESS": ("smtp", "postal_address", False),
    "IMAP_HOST": ("smtp", "imap_host", False),
    "IMAP_PORT": ("smtp", "imap_port", False),
    "SUPYMEM_IMAP_HOST": ("smtp", "imap_host", False),
    "SUPYMEM_IMAP_PORT": ("smtp", "imap_port", False),
    "SMTP_BOOKING_LINK": ("smtp", "booking_link", False),
    "SUPYMEM_BOOKING_LINK": ("smtp", "booking_link", False),
    "SUPYMEM_PUBLISH_PROVIDER": ("publishing", "provider", False),
    "VERCEL_TOKEN": ("publishing", "token", True),
    "VERCEL_PROJECT_NAME": ("publishing", "project", False),
    "NETLIFY_AUTH_TOKEN": ("publishing", "token", True),
    "NETLIFY_SITE_ID": ("publishing", "project", False),
    "AWS_ACCESS_KEY_ID": ("publishing", "access_key_id", True),
    "AWS_SECRET_ACCESS_KEY": ("publishing", "secret_access_key", True),
    "AWS_SESSION_TOKEN": ("publishing", "session_token", True),
    "AWS_DEFAULT_REGION": ("publishing", "region", False),
    "AWS_REGION": ("publishing", "region", False),
    "AWS_S3_BUCKET": ("publishing", "bucket", False),
    "AWS_S3_PREFIX": ("publishing", "prefix", False),
    "SUPYMEM_S3_BUCKET": ("publishing", "bucket", False),
    "SUPYMEM_S3_PREFIX": ("publishing", "prefix", False),
    "SUPYMEM_PUBLIC_URL": ("publishing", "site_url", False),
    "SUPYMEM_SITE_URL": ("publishing", "site_url", False),
    "SUPYMEM_WAITLIST_EMAIL": ("publishing", "waitlist_email", False),
    "COMPOSIO_API_KEY": ("composio", "api_key", True),
    "SUPYMEM_COMPOSIO_AUTH_CONFIGS": ("composio", "auth_configs", False),
    "COMFYUI_URL": ("images", "comfyui_url", False),
    "BFL_API_KEY": ("images", "bfl_api_key", True),
    "BFL_API_TOKEN": ("images", "bfl_api_key", True),
    "GEMINI_API_KEY": ("images", "gemini_api_key", True),
    "GOOGLE_API_KEY": ("images", "gemini_api_key", True),
    "GOOGLE_GENERATIVE_AI_API_KEY": ("images", "gemini_api_key", True),
    "PEXELS_API_KEY": ("images", "pexels_api_key", True),
    "PIXABAY_API_KEY": ("images", "pixabay_api_key", True),
    "SUPYMEM_LOCAL_MONTHLY_CAP_USD": ("spend", "monthly_spend_cap", False),
    "SUPYMEM_BACKUP_DIRECTORY": ("data", "backup_directory", False),
    "LLM_OFFLINE": ("runtime", "offline", False),
    "LLM_CASSETTE_MODE": ("runtime", "cassette_mode", False),
}
for _name, _field in {
    "HOST": "host", "PORT": "port", "USERNAME": "user", "PASSWORD": "password",
}.items():
    _FIELDS[f"SUPYMEM_SMTP_{_name}"] = ("smtp", _field, _field == "password")
_FIELDS["SUPYMEM_SENDER_NAME"] = ("smtp", "sender_name", False)
_FIELDS["SUPYMEM_SENDER_POSTAL_ADDRESS"] = ("smtp", "postal_address", False)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def env_path() -> Path:
    from app.secrets.dotenv_file import secrets_env_path

    return secrets_env_path()


def _environment() -> dict[str, str]:
    from app.core.providers_local import PROVIDER_ENV, ROLES

    known = set(_FIELDS) | {"SUPYMEM_LLM_PROVIDER"}
    known.update(name for names in PROVIDER_ENV.values() for name in names)
    known.update(f"SUPYMEM_MODEL_{role.upper()}" for role in ROLES)
    desktop = bool(os.environ.get("SUPYMEM_DESKTOP_INSTANCE_ID"))
    paths = [env_path()] if desktop else [repo_root() / ".env", env_path()]
    values = {}
    for path in dict.fromkeys(paths):
        try:
            with path.open(encoding="utf-8-sig") as source:
                content = source.read(1_048_577)
                if len(content) > 1_048_576:
                    raise ValueError("Your .env is too large. Keep only your settings in the .env shown in Keys and connections, then restart your app.")
                document = dotenv_values(stream=io.StringIO(content), interpolate=False)
        except FileNotFoundError:
            continue
        except UnicodeError:
            raise ValueError(f"Save {path} as UTF-8, or save your settings in Keys and connections, then restart your app.") from None
        except OSError:
            raise ValueError("Your .env could not be read. Check the .env in your data folder shown in Keys and connections, then restart your app.") from None
        for name, value in document.items():
            if value is None:
                continue
            if name not in known:
                if name not in _UNKNOWN:
                    _UNKNOWN.add(name)
                    logging.getLogger(__name__).warning("Ignored unknown .env variable %s", name)
            elif value.strip():
                values.pop(name, None)
                values[name] = value if _FIELDS.get(name, (None, None, False))[1] == "password" else value.strip()
    if not desktop:
        for name in sorted(known):
            if os.environ.get(name, "").strip():
                values.pop(name, None)
                value = os.environ[name]
                values[name] = value if _FIELDS.get(name, (None, None, False))[1] == "password" else value.strip()
    return values


def import_local_env(*, strict: bool = False) -> None:
    if not is_local() or getattr(_IMPORTING, "active", False):
        return
    _IMPORTING.active = True
    try:
        try:
            _import(_environment())
        except (OSError, ValueError):
            if strict:
                raise
            logging.getLogger(__name__).warning(
                "Your .env settings could not be imported. Open Keys and connections, correct the .env shown there or save your settings in the app, then retry."
            )
        from app.core.local_mode import is_process_offline

        # A venture lookup here can reopen the database during its own upgrade backup.
        os.environ["LLM_OFFLINE"] = "1" if is_process_offline() else "0"
    finally:
        _IMPORTING.active = False


def _import(environment: dict[str, str]) -> None:
    from app.api.local_settings import FIELD_INFO, SCHEMAS, SECTION_INFO
    from pydantic import ValidationError
    from app.core.local_secrets import read_settings, update_settings
    from app.core.providers_local import PROVIDER_ENV, ROLES, validate_endpoint
    from core.logging import register_local_secret

    secret_names = {name for name, (_, _, secret) in _FIELDS.items() if secret}
    secret_names.update(names[0] for names in PROVIDER_ENV.values())
    imported_secrets = [value for name, value in environment.items() if name in secret_names]
    for value in imported_secrets:
        register_local_secret(value)
    sections: dict[str, tuple[dict, dict]] = {}
    saved_sections = {}
    for name in environment:
        if name not in _FIELDS:
            continue
        section, field, secret = _FIELDS[name]
        if section not in saved_sections:
            saved_sections[section] = read_settings(section)
        if saved_sections[section]["sources"].get(field) == "app":
            continue
        value = environment[name]
        try:
            if secret:
                register_local_secret(value)
                if len(value) > 16384:
                    raise ValueError
            else:
                if len(value) > 2000 or any(ord(character) < 32 and not (field == "postal_address" and character == "\n") for character in value):
                    raise ValueError
                if any(secret and secret in value for secret in imported_secrets):
                    raise ValueError
                if field == "auth_configs":
                    try:
                        value = json.loads(value)
                    except ValueError:
                        raise ValueError("Open Keys and connections and correct your App authorization IDs JSON in the .env shown there, then retry.") from None
                elif field in {"port", "imap_port"}:
                    value = int(value)
                    if not 1 <= value <= 65535:
                        raise ValueError
                elif field == "monthly_spend_cap":
                    cap = Decimal(value)
                    if (not cap.is_finite() or not 0 <= cap <= Decimal("9999999999.99")
                            or cap != cap.quantize(Decimal("0.01"))):
                        raise ValueError
                    value = str(cap.quantize(Decimal("0.01")))
                elif field == "firecrawl_url":
                    parsed = urlsplit(value)
                    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                            or parsed.username is not None or parsed.password is not None
                            or parsed.query or parsed.fragment or parsed.port == 0
                            or any(character.isspace() for character in value)):
                        raise ValueError
                    value = parsed.geturl().rstrip("/")
                elif field in {"site_url", "booking_link"}:
                    value = validate_endpoint(value)
                    if name == "SUPYMEM_PUBLIC_URL" and urlsplit(value).scheme == "http":
                        continue
        except (ValueError, InvalidOperation):
            if field == "auth_configs":
                raise ValueError("Open Keys and connections and correct your App authorization IDs JSON in the .env shown there, then retry.") from None
            raise ValueError(f"Your {name} is invalid. Correct it in your .env or save the setting in Keys and connections, then restart your app.") from None
        values, secrets = sections.setdefault(section, ({}, {}))
        (secrets if secret else values)[field] = value
    if "search" in sections:
        values, secrets = sections["search"]
        if environment.get("SUPYMEM_SEARCH_PROVIDER"):
            values["provider"] = environment["SUPYMEM_SEARCH_PROVIDER"]
        elif environment.get("TAVILY_API_KEY"):
            values["provider"] = "tavily"
            secrets["api_key"] = environment["TAVILY_API_KEY"]
        elif environment.get("EXA_API_KEY"):
            values["provider"] = "exa"
        elif environment.get("FIRECRAWL_SELF_HOSTED_URL"):
            values["provider"] = "builtin"
        saved = saved_sections["search"]
        provider = saved["values"].get("provider") if saved["sources"].get("provider") == "app" else values.get("provider")
        key_name = {"tavily": "TAVILY_API_KEY", "exa": "EXA_API_KEY"}.get(provider)
        if key_name:
            if key_name in environment:
                secrets["api_key"] = environment[key_name]
            else:
                secrets.pop("api_key", None)
    if "publishing" in sections:
        values, secrets = sections["publishing"]
        if environment.get("SUPYMEM_PUBLISH_PROVIDER"):
            values["provider"] = environment["SUPYMEM_PUBLISH_PROVIDER"]
        elif environment.get("AWS_ACCESS_KEY_ID"):
            values["provider"] = "s3"
        elif environment.get("NETLIFY_AUTH_TOKEN"):
            values["provider"] = "netlify"
        elif environment.get("VERCEL_TOKEN"):
            values["provider"] = "vercel"
        saved = saved_sections["publishing"]
        provider = saved["values"].get("provider") if saved["sources"].get("provider") == "app" else values.get("provider")
        if provider in {"vercel", "netlify"}:
            token_name, project_name = ("VERCEL_TOKEN", "VERCEL_PROJECT_NAME") if provider == "vercel" else ("NETLIFY_AUTH_TOKEN", "NETLIFY_SITE_ID")
            for name, field in ((token_name, "token"), (project_name, "project")):
                target = secrets if field == "token" else values
                if name in environment:
                    target[field] = environment[name]
                else:
                    target.pop(field, None)
    providers, keys = {}, {}
    selected = environment.get("SUPYMEM_LLM_PROVIDER")
    for provider, (key_name, url_name) in PROVIDER_ENV.items():
        fields = {}
        if key_name in environment:
            keys[provider] = environment[key_name]
            register_local_secret(keys[provider])
            if selected is None:
                selected = provider
        if url_name in environment:
            fields["base_url"] = environment[url_name]
        if fields or provider in keys or provider == selected:
            providers[provider] = fields
    models = {role: environment[f"SUPYMEM_MODEL_{role.upper()}"] for role in ROLES if f"SUPYMEM_MODEL_{role.upper()}" in environment}
    if models:
        saved = read_settings("model")
        if selected is None or saved["sources"].get("provider") == "app":
            selected = saved["values"].get("provider") or selected
    if selected in PROVIDER_ENV and models:
        providers.setdefault(selected, {})["models"] = models
    if selected is not None or providers:
        update_settings("model", {"provider": selected, "providers": providers}, keys, source="env")
    for section, (values, secrets) in sections.items():
        for field, value in values.items():
            if any(secret and secret in str(value) for secret in secrets.values()):
                raise ValueError(f"Your {field} contains a key. Move it to its key field in Keys and connections or your .env, then restart your app.")
        if section in SCHEMAS:
            try:
                model = SCHEMAS[section].model_validate({**saved_sections[section]["values"], **values, **secrets})
            except ValidationError as exc:
                names = sorted({name for error in exc.errors() for name in error["loc"] if name in FIELD_INFO[section]})
                fields = ", ".join(FIELD_INFO[section][name][0] for name in names) or SECTION_INFO[section][0]
                raise ValueError(f"Open Keys and connections and correct your {fields} in the .env shown there, then retry.") from None
            validated = model.model_dump(mode="json")
            values = {name: validated[name] for name in values}
        update_settings(section, values, secrets, source="env")

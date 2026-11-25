"""Local-only configuration. Secret inputs never appear in API responses."""

from __future__ import annotations

import asyncio
import imaplib
import json
import os
import shutil
import smtplib
import sqlite3
import ssl
import tempfile
from decimal import Decimal
from email.errors import HeaderParseError
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import httpx
from cryptography.exceptions import InvalidTag
from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.routing import APIRoute
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    ValidationError,
    field_validator,
)
from starlette.background import BackgroundTask

from app.cli_local import LocalMaintenanceError
from app.config import data_root
from app.core.local_mode import is_local
from app.core.local_secrets import ConnectionSecretRequired, get_secret, read_settings, update_settings

Text = Annotated[str, StringConstraints(max_length=2000)]


def _url(value: str, *, https_only: bool = False) -> str:
    if value:
        parsed = urlsplit(value)
        if (parsed.scheme not in ({"https"} if https_only else {"http", "https"}) or not parsed.hostname
                or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment
                or any(character.isspace() for character in value) or parsed.port == 0):
            raise ValueError("Enter your service URL without credentials or extra parameters in Keys and connections, then try again.")
    return value


class SettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="after")
    @classmethod
    def validate_text(cls, value, info):
        if isinstance(value, SecretStr):
            secret = value.get_secret_value()
            if not secret.strip() or len(secret) > 8192:
                raise ValueError("Enter your replacement key or leave it unchanged.")
        elif isinstance(value, str) and any(
            ord(character) < 32 and not (info.field_name == "postal_address" and character == "\n")
            for character in value
        ):
            raise ValueError("Remove control characters and try again.")
        return value


class SMTPBody(SettingsBody):
    host: Text = ""
    port: int = Field(default=587, ge=1, le=65535)
    user: Text = ""
    password: SecretStr | None = None
    sender_name: Text = ""
    postal_address: Text = ""
    imap_host: Text = ""
    imap_port: int = Field(default=993, ge=1, le=65535)
    booking_link: Text = ""

    @field_validator("host", "imap_host")
    @classmethod
    def validate_host(cls, value):
        if any(character.isspace() or character in "/@?#" for character in value):
            raise ValueError("Enter only your mail server hostname in Keys and connections, then try again.")
        return value

    @field_validator("booking_link")
    @classmethod
    def validate_booking_link(cls, value):
        return _url(value, https_only=True)


class PublishingBody(SettingsBody):
    provider: Literal["vercel", "netlify", "s3"] | None = None
    project: Text = ""
    bucket: Text = ""
    region: Text = ""
    token: SecretStr | None = None
    access_key_id: SecretStr | None = None
    secret_access_key: SecretStr | None = None
    session_token: SecretStr | None = None
    prefix: Text = ""
    site_url: Text = ""
    waitlist_email: Text = ""

    @field_validator("site_url")
    @classmethod
    def validate_site_url(cls, value):
        return _url(value, https_only=True)

    @field_validator("prefix")
    @classmethod
    def validate_prefix(cls, value):
        if "\\" in value or any(part in {".", ".."} for part in value.split("/")):
            raise ValueError("Choose a page folder without parent-directory segments in Keys and connections, then try again.")
        return value

    @field_validator("waitlist_email")
    @classmethod
    def validate_email(cls, value):
        if value:
            from app.gtm.local_mail import _address

            try:
                _address(value)
            except (ValueError, HeaderParseError):
                raise ValueError("Enter your full Waitlist email address in Keys and connections, then save again.") from None
        return value


class SearchBody(SettingsBody):
    provider: Literal["builtin", "tavily", "exa", "openai", "firecrawl"] = "builtin"
    firecrawl_url: Text = ""
    firecrawl_key: SecretStr | None = None
    api_key: SecretStr | None = None

    @field_validator("firecrawl_url")
    @classmethod
    def validate_url(cls, value):
        from app.core.providers_local import validate_endpoint

        return validate_endpoint(value) if value else value


class ComposioBody(SettingsBody):
    api_key: SecretStr | None = None
    auth_configs: dict[Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")], Text] = Field(default_factory=dict, max_length=100)

    @field_validator("auth_configs")
    @classmethod
    def validate_auth_configs(cls, value):
        if any(not config.strip() or any(ord(character) < 32 for character in config) for config in value.values()):
            raise ValueError("Enter each app's authorization ID in Keys and connections, then try again.")
        return value


class ImagesBody(SettingsBody):
    comfyui_url: Text = ""
    bfl_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    pexels_api_key: SecretStr | None = None
    pixabay_api_key: SecretStr | None = None

    @field_validator("comfyui_url")
    @classmethod
    def validate_url(cls, value):
        from app.core.providers_local import validate_endpoint

        return validate_endpoint(value) if value else value


class SpendBody(SettingsBody):
    monthly_spend_cap: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)


class DataBody(SettingsBody):
    backup_directory: Text = ""

    @field_validator("backup_directory")
    @classmethod
    def validate_directory(cls, value):
        try:
            path = Path(value).expanduser()
        except RuntimeError:
            raise ValueError("Enter your full backup folder path and try again.") from None
        if value and not path.is_absolute():
            raise ValueError("Enter your full backup folder path and try again.")
        return str(path) if value else value


SCHEMAS = {"smtp": SMTPBody, "publishing": PublishingBody, "search": SearchBody,
           "composio": ComposioBody, "images": ImagesBody, "spend": SpendBody, "data": DataBody}
SECRET_FIELDS = {"smtp": ("password",), "publishing": ("token", "access_key_id", "secret_access_key", "session_token"),
                 "search": ("firecrawl_key", "api_key"), "composio": ("api_key",),
                 "images": ("bfl_api_key", "gemini_api_key", "pexels_api_key", "pixabay_api_key"), "spend": (), "data": ()}

SECTION_INFO = {
    "images": ("Your optional image keys", "Your content drafts work without image keys. Upload your own product screenshot or photo in Media Studio to add an image.", ["content"], "", "Image services receive only the image prompts and files you choose to send. Your saved keys stay on your machine."),
    "providers": ("Your AI provider", "Connect your own AI account, or use a model running on your machine.", ["content", "landing_pages", "find_customers"], "https://platform.openai.com/api-keys", "Your prompts and the context you choose to include go to your selected model service. Local models keep them on your machine."),
    "smtp": ("Your mail account", "Send outreach through your own mailbox and read replies. Your disclosure, opt-out and send limits stay in place.", ["send_email", "read_replies"], "https://support.google.com/accounts/answer/185833", "When you send mail, your mail server receives the recipient, subject, message and attachments. Reading replies uses your incoming mail server. Testing only signs in and sends no mail."),
    "publishing": ("Your page publishing account", "Publish a page only when you ask. Your destination receives the public page, not your private workspace.", ["publish_pages", "page_measurement"], "https://vercel.com/account/settings/tokens", "Publishing sends your public page files to the selected host. Waitlist links send a visitor's signup to your chosen address. Testing only checks your account or bucket."),
    "search": ("Your search and page reader", "Built-in search and page reading work without a key. You can optionally use your own search account or Firecrawl.", ["research", "find_customers"], "https://docs.tavily.com/documentation/quickstart", "Search sends your query to the selected search service. Your saved Firecrawl server, if set, gets page URLs. If you select Tavily, it can receive page URLs the built-in reader could not open. OpenAI search sends your query to your model service."),
    "composio": ("Your connected apps", "Use your own Composio project to connect the apps you choose. Authorize each app on the Connections screen after saving.", ["connected_apps", "content"], "https://dashboard.composio.dev/", "Composio receives app authorization details and the inputs to actions you request. Testing only lists one authorization configuration, without running an app action."),
    "images": ("Your image tools", "Your own uploads work without these optional settings. Connect a local image server or add your own image account keys.", ["content"], "https://docs.comfy.org/", "Your chosen image server receives your image prompt. A server running on your machine keeps it local. Online image services receive your image prompt or image search terms."),
    "spend": ("Your monthly spend cap", "Your app checks this limit before paid calls. You can also set a limit with your model provider.", ["background_work", "run_for_me_experiments"], "", "Your limit stays on your machine. Your model provider receives only the requests you choose to run."),
    "data": ("Your data folder and backup", "Choose your backup folder. Stop your app before replacing your data folder. Full backups include encrypted settings, not your encryption key. Open Your data and recovery for restore steps.", ["routines", "background_work"], "", "Your backups stay in the folder you choose. Your app does not upload them."),
}

FIELD_INFO = {
    "images": {
        "comfyui_url": ("Your ComfyUI service URL", "Use an image service you run yourself, or upload your own image in Media Studio.", ["COMFYUI_URL"]),
        "bfl_api_key": ("BFL image key", "Use your own BFL account when you request an image.", ["BFL_API_KEY", "BFL_API_TOKEN"]),
        "gemini_api_key": ("Gemini image key", "Use your own Gemini account when you request an image.", ["GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENERATIVE_AI_API_KEY"]),
        "pexels_api_key": ("Pexels stock key", "Use your own Pexels account to find a stock image.", ["PEXELS_API_KEY"]),
        "pixabay_api_key": ("Pixabay stock key", "Use your own Pixabay account to find a stock image.", ["PIXABAY_API_KEY"]),
    },
    "providers": {
        "provider": ("Model service", "Choose your model account or a local model server.", ["SUPYMEM_LLM_PROVIDER"]),
        "model": ("Chat model", "Enter the model or deployment name from your service.", ["SUPYMEM_MODEL_CHAT"]),
        "catalog_model": ("Azure model ID", "If your Azure deployment has a custom name, enter the underlying model ID here. Keep the deployment name in Chat model.", []),
        "base_url": ("Service URL", "Enter your Azure resource or local model server address when needed.", ["OPENAI_BASE_URL", "AZURE_OPENAI_ENDPOINT", "ANTHROPIC_BASE_URL", "OPENAI_COMPATIBLE_BASE_URL"]),
        "context_window": ("Context window", "For your local model server only. Match the server's token limit. Supymem uses 8,192 if you have not set a limit here.", []),
        "api_key": ("Model API key", "Use a key from your selected model account. For a local model server, leave this blank only if your server does not require a key.", ["OPENAI_API_KEY", "AZURE_OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_COMPATIBLE_API_KEY"]),
    },
    "smtp": {
        "host": ("Outgoing mail host", "Your mailbox provider's SMTP server address.", ["SUPYMEM_SMTP_HOST"]),
        "port": ("Outgoing mail port", "Usually 587 for secure mail submission or 465 for implicit TLS.", ["SUPYMEM_SMTP_PORT"]),
        "user": ("Mailbox address", "Your full email address, used to sign in and send mail.", ["SUPYMEM_SMTP_USERNAME"]),
        "password": ("Mail app password", "Create an app password in your mailbox account, rather than using your main password.", ["SUPYMEM_SMTP_PASSWORD"]),
        "sender_name": ("Sender name", "The name people see when you email them.", ["SUPYMEM_SENDER_NAME"]),
        "postal_address": ("Postal address", "Your sender address stays in every outreach email for compliance.", ["SUPYMEM_SENDER_POSTAL_ADDRESS"]),
        "imap_host": ("Incoming mail host", "Your mailbox provider's IMAP server for reading replies.", ["SUPYMEM_IMAP_HOST"]),
        "imap_port": ("Incoming mail port", "Usually 993 for secure incoming mail.", ["SUPYMEM_IMAP_PORT"]),
        "booking_link": ("Booking link", "Your HTTPS scheduling page, included when you ask for a call.", ["SUPYMEM_BOOKING_LINK"]),
    },
    "publishing": {
        "provider": ("Publishing service", "Choose the account that will host your public pages.", ["SUPYMEM_PUBLISH_PROVIDER"]),
        "project": ("Project or site ID", "Your Vercel project name or Netlify site ID.", ["VERCEL_PROJECT_NAME", "NETLIFY_SITE_ID"]),
        "token": ("Publishing token", "Your Vercel or Netlify access token.", ["VERCEL_TOKEN", "NETLIFY_AUTH_TOKEN"]),
        "bucket": ("S3 bucket", "The existing bucket where you want your public pages.", ["SUPYMEM_S3_BUCKET"]),
        "region": ("S3 region", "The region of your bucket.", ["AWS_REGION"]),
        "prefix": ("S3 page folder", "A folder prefix to separate published pages from other bucket files.", ["SUPYMEM_S3_PREFIX"]),
        "site_url": ("Public site URL", "Your reachable HTTPS address for page signups and measurement. Keep your app reachable there to receive them.", ["SUPYMEM_SITE_URL", "SUPYMEM_PUBLIC_URL"]),
        "access_key_id": ("S3 access key ID", "Your own access key with permission to publish to this bucket.", ["AWS_ACCESS_KEY_ID"]),
        "secret_access_key": ("S3 secret access key", "The secret paired with your access key ID.", ["AWS_SECRET_ACCESS_KEY"]),
        "session_token": ("S3 session token", "Include this only when you use temporary AWS credentials.", ["AWS_SESSION_TOKEN"]),
        "waitlist_email": ("Waitlist email", "Your inbox for visitor signups when you use an email-based waitlist.", ["SUPYMEM_WAITLIST_EMAIL"]),
    },
    "search": {
        "provider": ("Search service", "Built-in search needs no key. Other choices use your own account.", ["SUPYMEM_SEARCH_PROVIDER"]),
        "api_key": ("Search API key", "Your Tavily or Exa key. OpenAI uses your saved model key instead.", ["TAVILY_API_KEY", "EXA_API_KEY"]),
        "firecrawl_url": ("Firecrawl URL", "Optional URL of your own Firecrawl server for reading or searching pages.", ["FIRECRAWL_SELF_HOSTED_URL"]),
        "firecrawl_key": ("Firecrawl key", "Only needed if your Firecrawl server requires authentication.", ["FIRECRAWL_SELF_HOSTED_TOKEN"]),
    },
    "composio": {
        "api_key": ("Composio project key", "Create a project key in your Composio account.", ["COMPOSIO_API_KEY"]),
        "auth_configs": ("App authorization IDs", "Enter a JSON object mapping each app's toolkit slug to its Composio authorization configuration ID.", ["SUPYMEM_COMPOSIO_AUTH_CONFIGS"]),
    },
    "images": {
        "comfyui_url": ("ComfyUI URL", "Enter your own image server address. The default local address is http://127.0.0.1:8188.", ["COMFYUI_URL"]),
        "bfl_api_key": ("BFL image key", "Use your own BFL account key for generated images.", ["BFL_API_KEY", "BFL_API_TOKEN"]),
        "gemini_api_key": ("Gemini image key", "Use your own Gemini account key for generated images.", ["GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENERATIVE_AI_API_KEY"]),
        "pexels_api_key": ("Pexels image key", "Use your own Pexels key to search licensed stock photos.", ["PEXELS_API_KEY"]),
        "pixabay_api_key": ("Pixabay image key", "Use your own Pixabay key to search licensed stock photos.", ["PIXABAY_API_KEY"]),
    },
    "spend": {"monthly_spend_cap": ("Monthly limit (USD)", "Leave blank to remove your local monthly limit.", ["SUPYMEM_LOCAL_MONTHLY_CAP_USD"])},
    "data": {"backup_directory": ("Full backup folder path", "Choose an absolute path for your backups. Existing backups are not moved.", ["SUPYMEM_BACKUP_DIRECTORY"])},
}

HELP_TEXT = {"providers": "Get an API key", "smtp": "Create an app password", "publishing": "Create a token",
             "search": "Get an API key", "composio": "Get an API key", "images": "Get an API key"}
PROVIDER_URLS = {
    "providers": {"openai": "https://platform.openai.com/api-keys", "azure_openai": "https://portal.azure.com",
                  "anthropic": "https://console.anthropic.com/settings/keys", "openai_compatible": "https://platform.openai.com/api-keys",
                  "openrouter": "https://openrouter.ai/settings/keys", "ollama": "https://docs.ollama.com/quickstart",
                  "lmstudio": "https://lmstudio.ai/docs"},
    "smtp": {"gmail": SECTION_INFO["smtp"][3], "outlook": "https://account.microsoft.com/security"},
    "search": {"tavily": "https://docs.tavily.com/documentation/quickstart", "exa": "https://dashboard.exa.ai/api-keys",
               "openai": "https://platform.openai.com/api-keys", "firecrawl": "https://docs.firecrawl.dev/contributing/self-host"},
    "publishing": {"vercel": "https://vercel.com/account/settings/tokens", "netlify": "https://app.netlify.com/user/applications",
                   "s3": "https://console.aws.amazon.com/iam/home#/security_credentials"},
}
FIELD_PROVIDERS = {
    "providers": {"context_window": ["openai_compatible"], "catalog_model": ["azure_openai"]},
    "search": {"api_key": ["tavily", "exa"]},
    "publishing": {**{name: ["vercel", "netlify"] for name in ("project", "token")},
                   "site_url": ["vercel", "netlify", "s3"],
                   **{name: ["s3"] for name in ("bucket", "region", "prefix", "access_key_id", "secret_access_key", "session_token")}},
}


class LocalRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def local_handler(request: Request):
            if not is_local():
                raise HTTPException(404, "Your local settings are unavailable. Use your account settings.")
            try:
                response = await handler(request)
            except ConnectionSecretRequired as exc:
                raise HTTPException(422, str(exc)) from None
            except LocalMaintenanceError as exc:
                raise HTTPException(422, str(exc)) from None
            except (RequestValidationError, ValidationError) as exc:
                fields = FIELD_INFO.get(request.path_params.get("section"), {})
                names = sorted({part for error in exc.errors() for part in error["loc"] if part in fields})
                labels = [fields[name][0] for name in names]
                raise HTTPException(422, {"code": "invalid_local_settings", "fields": names,
                    "message": f"Open Keys and connections, check your {', '.join(labels) or 'connection fields'}, then save again."}) from None
            except (OSError, ValueError, InvalidTag, sqlite3.Error) as exc:
                raise HTTPException(503, "Your settings could not be opened or saved. Open Keys and connections, under Your data and recovery, check your data folder permissions and restore your settings backup and separately saved encryption recovery key if needed, then try again.") from exc
            response.headers["Cache-Control"] = "no-store"
            return response

        return local_handler


router = APIRouter(prefix="/local/settings", tags=["local-settings"], route_class=LocalRoute)


def _schema(section: str):
    if section not in SCHEMAS:
        raise HTTPException(404, "This setting is not available. Update your local app and try again.")
    return SCHEMAS[section]


def import_env_settings(section_name: str | None = None):
    from app.core.local_env_import import import_local_env

    try:
        import_local_env(strict=True)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


def _view(section: str, settings: dict) -> dict:
    if any(field in settings["values"] for field in SECRET_FIELDS[section]):
        raise ValueError("Restore your local settings from your backup.")
    model = _schema(section).model_validate(settings["values"])
    result = {"values": model.model_dump(mode="json", exclude=set(SECRET_FIELDS[section])), "secrets_set": {
        field: settings["secrets_set"].get(field, False) for field in SECRET_FIELDS[section]
    }, "sources": settings.get("sources", {})}
    unreadable = [name for name in settings.get("unreadable_secrets", []) if name in SECRET_FIELDS[section]]
    if unreadable:
        result["unreadable_secrets"] = unreadable
    if section == "data":
        result["data_folder"] = str(data_root())
    return result


def _requirements(section: str, values: dict) -> list[str]:
    if section == "smtp":
        from app.core.local_capabilities import SMTP_SEND_FIELDS

        return list(SMTP_SEND_FIELDS)
    if section == "composio":
        return ["api_key", "auth_configs"]
    if section == "publishing":
        if values.get("provider") == "s3":
            return ["provider", "bucket", "region", "prefix", "site_url", "access_key_id", "secret_access_key"]
        return ["provider", "project", "token"] if values.get("provider") in {"vercel", "netlify"} else ["provider"]
    if section == "search":
        provider = values.get("provider", "builtin")
        return ["api_key"] if provider in {"tavily", "exa"} else ["firecrawl_url"] if provider == "firecrawl" else []
    if section == "providers":
        return ["provider", "model"]
    return []


def _connection_status(section: str, view: dict) -> dict:
    values = view["values"]
    if section == "smtp":
        from app.core.local_capabilities import missing_mail_fields

        missing = missing_mail_fields(values, get_secret("smtp", "password"))
    else:
        missing = [name for name in _requirements(section, values)
                   if not (view["secrets_set"].get(name) if name in view["secrets_set"] else values.get(name))]
    if section == "search" and values.get("provider") == "openai":
        from app.core.providers_local import configured_providers

        providers = configured_providers()
        if not providers["providers"].get(providers["provider"], {}).get("api_key_set"):
            missing.append("saved model API key")
    if section == "providers":
        if view.get("verified"):
            return {"state": "ready", "missing": [], "next_step": "Your connection passed its test. You can update it under Your AI provider in Keys and connections."}
        if view.get("configured") and not view.get("sample_selected"):
            return {"state": "saved_not_tested", "missing": [], "next_step": "In Keys and connections, open Your AI provider and choose Test your connection."}
        missing = missing or ["model connection"]
    labels = [FIELD_INFO[section].get(name, (name,))[0] for name in missing]
    next_step = (f"Open Keys and connections, then add your {', '.join(labels)} under {SECTION_INFO[section][0]}."
                 if missing else "Your settings are ready. Test your connection before you start using it.")
    return {"state": "needs_setup" if missing else "ready", "missing": missing, "next_step": next_step}


def _field_states(section: str, view: dict) -> dict:
    states = {}
    for name in FIELD_INFO[section]:
        secret = name in SECRET_FIELDS.get(section, ("api_key",))
        value = view["values"].get(name)
        is_set = view["secrets_set"].get(name, False) if secret else value is not None and value != "" and value != {}
        states[name] = "from .env" if is_set and view.get("sources", {}).get(name) == "env" else "set" if is_set else "unreadable" if secret and name in view.get("unreadable_secrets", []) else "not set"
    return states


@router.get("/catalog")
def settings_catalog():
    from app.core.providers_local import _provider_view
    from app.core.local_env_import import env_path as settings_env_path

    import_error = None
    try:
        import_env_settings()
    except HTTPException as exc:
        import_error = exc.detail
    env_path = settings_env_path()
    sections = []
    for section, (title, description, capabilities, url, data_sent) in SECTION_INFO.items():
        schema = SCHEMAS.get(section)
        view = _provider_view() if section == "providers" else _view(section, read_settings(section))
        properties = schema.model_json_schema()["properties"] if schema else {name: {} for name in FIELD_INFO[section]}
        states = _field_states(section, view)
        fields = []
        for name, property_schema in properties.items():
            label, purpose, env = FIELD_INFO[section][name]
            secret = name in (SECRET_FIELDS.get(section, ()) if schema else ("api_key",))
            field_url = PROVIDER_URLS.get(section, {}).get(view["values"].get("provider"), url)
            if section == "search" and name.startswith("firecrawl_"):
                field_url = PROVIDER_URLS["search"]["firecrawl"]
            elif section == "images":
                field_url = {"bfl_api_key": "https://dashboard.bfl.ai/", "gemini_api_key": "https://aistudio.google.com/apikey",
                             "pexels_api_key": "https://www.pexels.com/api/", "pixabay_api_key": "https://pixabay.com/api/docs/"}.get(name, url)
            field = {"name": name, "label": label, "secret": secret, "purpose": purpose,
                     "capabilities": capabilities, "url": field_url, "data_sent": data_sent,
                     "required": name in _requirements(section, view["values"]), "state": states[name], "env": env}
            if providers := FIELD_PROVIDERS.get(section, {}).get(name):
                field["providers"] = providers
            if secret and name not in {"secret_access_key", "session_token"}:
                field["help_text"] = "Create access keys" if name == "access_key_id" else HELP_TEXT[section]
            property_schema = next((item for item in property_schema.get("anyOf", []) if item.get("type") != "null"), property_schema)
            if "enum" in property_schema:
                labels = {"builtin": "Built-in (no key needed)", "s3": "S3", "openai": "OpenAI", "firecrawl": "Firecrawl"}
                field["options"] = [{"value": item, "label": labels.get(item, item.title())} for item in property_schema["enum"]]
            if section == "providers" and name == "context_window":
                field.update(type="number", min=2048, max=262144, step="1")
            elif property_schema.get("type") in {"integer", "number"} or name == "monthly_spend_cap":
                field.update(type="number", min=property_schema.get("minimum", 0), max=property_schema.get("maximum"), step="0.01" if name == "monthly_spend_cap" else "1")
            elif name in {"auth_configs", "postal_address"}:
                field["type"] = "textarea"
            elif name.endswith("_url") or name == "booking_link":
                field["type"] = "url"
            fields.append(field)
        status = _connection_status(section, view)
        titles = {"research": "Research", "find_customers": "Find customers", "send_email": "Send email", "read_replies": "Read replies", "landing_pages": "Create landing pages", "publish_pages": "Publish pages", "page_measurement": "Measure your pages", "connected_apps": "Connected apps", "content": "Create content", "background_work": "Background work", "run_for_me_experiments": "Run experiments", "routines": "Routines"}
        sections.append({"id": section, "title": title, "description": description, "capabilities": capabilities, "unlocks": [titles[name] for name in capabilities],
                         "url": fields[0]["url"], "help_text": HELP_TEXT.get(section, ""), "data_sent": data_sent,
                         "path": "/api/local/providers" if section == "providers" else f"/api/local/settings/{section}",
                         "fields": fields, **status})
        if section in PROVIDER_URLS:
            sections[-1]["provider_urls"] = PROVIDER_URLS[section]
    from app.core.local_secrets import storage_notice
    return {"env_path": str(env_path), "sections": sections, "import_error": import_error, "storage_notice": storage_notice()}


def _test_mail(values: dict, *, incoming: bool):
    host = values.get("imap_host" if incoming else "host")
    password = get_secret("smtp", "password")
    if not host or not values.get("user") or not password:
        raise ValueError
    context = ssl.create_default_context()
    if incoming:
        with imaplib.IMAP4_SSL(host, values.get("imap_port", 993), ssl_context=context, timeout=5) as client:
            client.login(values["user"], password)
    else:
        port = values.get("port", 587)
        factory = smtplib.SMTP_SSL if port == 465 else smtplib.SMTP
        with factory(host, port, timeout=5, **({"context": context} if port == 465 else {})) as client:
            if port != 465:
                client.starttls(context=context)
            client.login(values["user"], password)


def _test_account(section: str, values: dict):
    provider = "composio" if section == "composio" else values.get("provider")
    if provider == "s3":
        import boto3
        from botocore.config import Config

        access_key = get_secret(section, "access_key_id")
        secret_key = get_secret(section, "secret_access_key")
        if not access_key or not secret_key or not values.get("bucket") or not values.get("region"):
            raise ValueError
        client = boto3.client("s3", region_name=values["region"], aws_access_key_id=access_key,
            aws_secret_access_key=secret_key, aws_session_token=get_secret(section, "session_token"),
            config=Config(connect_timeout=5, read_timeout=5, retries={"total_max_attempts": 1}))
        try:
            client.head_bucket(Bucket=values["bucket"])
        finally:
            client.close()
        return
    token = get_secret(section, "api_key" if provider == "composio" else "token")
    if not token:
        raise ValueError
    endpoints = {"vercel": "https://api.vercel.com/v2/user", "netlify": "https://api.netlify.com/api/v1/user",
                 "composio": "https://backend.composio.dev/api/v3.1/auth_configs?limit=1"}
    headers = {"x-api-key": token} if provider == "composio" else {"Authorization": "Bearer " + token}
    with httpx.Client(timeout=8, follow_redirects=False, trust_env=False) as client:
        response = client.get(endpoints[provider], headers=headers)
        if not 200 <= response.status_code < 300:
            raise ValueError


@router.post("/{section}/test")
async def test_settings(section: str):
    from starlette.concurrency import run_in_threadpool
    from app.credits.errors import CreditsUnavailable

    incoming = section == "imap"
    stored_section = "smtp" if incoming else section
    _schema(stored_section)
    import_env_settings(stored_section)
    values = _schema(stored_section).model_validate(read_settings(stored_section)["values"]).model_dump(mode="json", exclude=set(SECRET_FIELDS[stored_section]))
    if stored_section not in {"search", "smtp", "publishing", "composio"}:
        return {"ok": False, "next_step": f"Open Keys and connections and save your {SECTION_INFO[stored_section][0].lower()}. No service connection is needed."}
    try:
        if stored_section == "search":
            from app.research.local_providers import search, search_provider

            await asyncio.wait_for(search(search_provider(limit=1), "connectivity test", purpose="settings.search_test"), timeout=12)
        elif stored_section == "smtp":
            await run_in_threadpool(_test_mail, values, incoming=incoming)
        else:
            await run_in_threadpool(_test_account, stored_section, values)
    except CreditsUnavailable:
        return {"ok": False, "next_step": "Open Keys and connections and check your Monthly limit (USD) under Your monthly spend cap. Raise or remove that limit if you want to allow this paid connection test, then test again."}
    except Exception:
        if stored_section == "search":
            field = {"builtin": "internet connection; keep Search service set to Built-in",
                "tavily": "Search API key for Tavily", "exa": "Search API key for Exa",
                "firecrawl": "Firecrawl URL and Firecrawl key for your own reader server",
                "openai": "Model API key under Your AI provider, then choose OpenAI as your Search service"}[values["provider"]]
            return {"ok": False, "next_step": f"Open Keys and connections, check your {field} under Your search and page reader, save any changes, then test again."}
        field = "Incoming mail host, Incoming mail port and Mail app password" if incoming else {
            "smtp": "Outgoing mail host, Outgoing mail port, Mailbox address and Mail app password",
            "publishing": "S3 bucket, S3 region, S3 access key ID, S3 secret access key and S3 session token" if values.get("provider") == "s3" else "Publishing service and Publishing token",
            "composio": "Composio project key",
        }[stored_section]
        return {"ok": False, "next_step": f"Open Keys and connections, check your {field} under {SECTION_INFO[stored_section][0]}, save, then test again."}
    if stored_section == "smtp":
        return {"ok": True, "next_step": "Your mail sign-in works. No mail was sent. Open Settings > Capabilities to check whether you can send email or read replies."}
    return {"ok": True, "next_step": "Your connection works. You can use it when you ask. No mail, page or app action was sent."}


@router.get("/mode")
def local_mode():
    from app.core.local_env_import import env_path

    return {"local_mode": True, "env_path": str(env_path())}


@router.get("/export")
def export_data():
    from app.core.local_data import export_data

    skipped: set[str] = set()
    archive = export_data(skipped=skipped)
    return FileResponse(archive, media_type="application/zip", filename="my-data.zip",
                        headers={"X-Supymem-Skipped-Files": str(len(skipped))},
                        background=BackgroundTask(archive.unlink, missing_ok=True))


@router.post("/backup")
def create_backup():
    from app.core.local_data import create_backup

    skipped: set[str] = set()
    archive = create_backup(skipped=skipped)
    message = ("Your backup is partial. Copy any files you need into your data folder as regular files, then try again under Your data and recovery."
               if skipped else "Your backup is ready. Keep it private and copy it to safe storage.")
    return JSONResponse({"backup": archive, "message": message},
                        headers={"X-Supymem-Skipped-Files": str(len(skipped))})


@router.get("/backups")
def list_backups():
    from app.core.local_data import backups

    return {"backups": backups()}


@router.post("/restore")
def restore_data(body: Annotated[str, Body(embed=True)]):
    from app.core.local_data import queue_restore

    queue_restore(body)
    return {"message": "Your restore is ready. Restart your app to finish it. Your current data is backed up first."}


@router.post("/import")
async def import_data(request: Request):
    from app.core.local_data import queue_import

    if request.headers.get("content-type", "").split(";", 1)[0] != "application/zip":
        raise HTTPException(415, "Choose your ZIP data export, then retry the import.")
    root = data_root().resolve()
    with tempfile.TemporaryDirectory(prefix=".upload-", dir=root.parent) as temporary:
        archive = Path(temporary) / "data.zip"
        with archive.open("xb") as target:
            os.chmod(archive, 0o600)
            async for chunk in request.stream():
                if len(chunk) > shutil.disk_usage(root.parent).free:
                    raise HTTPException(413, "Your import needs more free disk space. Free some space, then retry.")
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        from starlette.concurrency import run_in_threadpool

        await run_in_threadpool(queue_import, archive)
    return {"message": "Your import is ready. Restart your app to finish it. Reconnect your providers if needed."}


@router.get("/{section}")
def get_settings(section: str):
    _schema(section)
    import_error = None
    try:
        import_env_settings(section)
    except HTTPException as exc:
        import_error = exc.detail
    view = _view(section, read_settings(section))
    return {**view, **_connection_status(section, view), "field_states": _field_states(section, view), "import_error": import_error}


@router.put("/{section}")
def put_settings(section: str, body: Annotated[Any, Body()] = None):
    model = _schema(section).model_validate(body)
    secrets = {field: value.get_secret_value() for field in SECRET_FIELDS[section]
               if (value := getattr(model, field)) is not None}
    values = model.model_dump(mode="json", exclude_unset=True, exclude=set(SECRET_FIELDS[section]))
    if section == "data" and "backup_directory" in values:
        from app.cli_local import BackupFolderError, _backup_directory

        try:
            _backup_directory(data_root().resolve(), values["backup_directory"])
        except BackupFolderError as error:
            raise HTTPException(422, {"code": "invalid_backup_folder", "fields": ["backup_directory"],
                                      "message": str(error)}) from None
    view = _view(section, update_settings(section, values, secrets))
    return {**view, **_connection_status(section, view), "field_states": _field_states(section, view)}

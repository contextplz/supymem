"""Company OS settings.

Reads env vars in this module only. ``APP_ENV`` (local|staging|production) is the
deployment environment. ``COMPANY_OS_ENV`` (local|cloud) is a *different* flag
used by coding-agent adapters — do not alias them.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from core.env_contract import coerce_bool
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.local_mode import is_local

REPO_ROOT = Path(__file__).resolve().parents[3]


def data_root(env: Mapping[str, str] | None = None) -> Path:
    """Writable data directory, matching the desktop defaults in local mode."""
    source = os.environ if env is None else env
    override = source.get("COMPANY_OS_DATA_DIR", "").strip() or source.get("SUPYMEM_DATA_DIR", "").strip()
    root = Path(override).expanduser().resolve() if override else REPO_ROOT / "data"
    if source.get("SUPYMEM_MODE", "").strip().lower() == "local":
        if not override:
            import json
            from app.cli_local import local_data_directory

            configuration = json.loads((REPO_ROOT / "src-tauri/tauri.local.json").read_text())
            root = local_data_directory(configuration["productName"], source)
        if os.name == "nt":
            from app.core.local_windows import private_directory

            private_directory(root)
            return root
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if os.fstat(descriptor).st_uid != os.geteuid():
                raise PermissionError("Your data folder belongs to another account. Choose a folder you own, then restart your app.")
            os.fchmod(descriptor, 0o700)
        finally:
            os.close(descriptor)
    return root


def is_local_env() -> bool:
    """True only for APP_ENV=local (or unset). Hosted envs must never touch process env/.env."""
    return os.environ.get("APP_ENV", "local").strip().lower() in {"", "local"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Deployment environment. Separate from COMPANY_OS_ENV (adapter selection).
    app_env: str = os.getenv("APP_ENV", "local")
    app_role: str = os.getenv("APP_ROLE", "api")
    core_port: int = 3300
    db_url: str = os.getenv(
        "COMPANY_OS_DB_URL",
        os.getenv(
            "DATABASE_URL",
            os.getenv("DB_URL", f"sqlite+aiosqlite:///{data_root() / 'company_os.db'}"),
        ),
    )
    warden_url: str = os.getenv("WARDEN_URL", "http://127.0.0.1:3400")
    llm_offline: bool = True
    guessed_emails: str = Field(default_factory=lambda: "off" if is_local() else "on")
    company_os_vault_secret: str = os.getenv("COMPANY_OS_VAULT_SECRET") or os.getenv("SESSION_SECRET") or os.getenv("COMPANY_OS_SECRET") or ""
    cos_internal_assertion_secret: str = os.getenv("COS_INTERNAL_ASSERTION_SECRET", "")
    company_os_require_signed_caller: bool = False
    admin_bootstrap_emails: str = os.getenv("ADMIN_BOOTSTRAP_EMAILS", "")
    admin_session_ttl_minutes: int = int(os.getenv("ADMIN_SESSION_TTL_MINUTES", "30") or "30")
    admin_require_totp: bool = False
    company_os_memory_facts_enabled: bool = False
    company_os_memory_model_extract_enabled: bool = False
    new_user_flow: str = "on"
    new_user_flow_allowlist: str = ""

    @field_validator("new_user_flow")
    @classmethod
    def _new_user_flow(cls, value: str) -> str:
        if value not in {"off", "allowlist", "on"}:
            raise ValueError("NEW_USER_FLOW must be off, allowlist, or on")
        return value

    @field_validator("guessed_emails", mode="before")
    @classmethod
    def _guessed_emails(cls, value):
        if value not in {"on", "off"}:
            raise ValueError("GUESSED_EMAILS must be on or off")
        return value

    @field_validator("company_os_vault_secret", mode="before")
    @classmethod
    def _vault_secret(cls, value):
        from app.connectors.secrets import vault_secret
        if not value and is_local():
            return ""
        return value or vault_secret()

    @field_validator("llm_offline", mode="before")
    @classmethod
    def _llm_offline(cls, value: object) -> bool:
        return coerce_bool(value, default=True)

    @field_validator("company_os_require_signed_caller", "admin_require_totp",
                     "company_os_memory_facts_enabled", "company_os_memory_model_extract_enabled", mode="before")
    @classmethod
    def _off_flag(cls, value: object) -> bool:
        return coerce_bool(value, default=False)


settings = Settings()

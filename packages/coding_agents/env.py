"""Allowlisted child environments: subscription CLIs never receive server secrets."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable


# HOME/USER/LOGNAME preserve macOS subscription and Keychain discovery. Provider
# keys, proxy configuration and cloud credential endpoints are never inherited.
CHILD_ENV_VARS = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE",
    "TMPDIR", "TMP", "TEMP", "SYSTEMROOT", "WINDIR",
})


def is_local() -> bool:
    """Desktop workers also run without the API package installed."""
    try:
        from app.core.local_mode import is_local as local_mode
    except ImportError:
        return os.environ.get("SUPYMEM_MODE", "").strip().lower() == "local"
    return local_mode()


def hosted_execution() -> bool:
    """Local owner mode takes precedence over legacy deployment signals."""
    if is_local():
        return False
    return (
        os.environ.get("APP_ENV", "local").strip().lower() != "local"
        or os.environ.get("COMPANY_OS_ENV", "local").strip().lower() != "local"
        or os.environ.get("COMPANY_OS_REQUIRE_SIGNED_CALLER", "").strip().lower()
        in {"1", "true", "yes", "on"}
        or any(name in os.environ for name in (
            "ECS_CONTAINER_METADATA_URI", "ECS_CONTAINER_METADATA_URI_V4",
        ))
    )


COMPANY_OS_OWNED_AUTH_VARS = {
    "LLM_API_KEY",
    "AZURE_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "CURSOR_API_KEY",
    "CODEX_API_KEY",
    "DEEPSEEK_API_KEY",
    "GOOGLE_API_KEY",
    "XAI_API_KEY",
    "OPENROUTER_API_KEY",
    "ZERNIO_API_KEY",
    "GITHUB_TOKEN",
    "LINEAR_API_KEY",
    "STRIPE_SECRET_KEY",
    "STRIPE_PUBLISHABLE_KEY",
    "STRIPE_WEBHOOK_SECRET",
    "RAZORPAY_KEY_ID",
    "RAZORPAY_KEY_SECRET",
    "RAZORPAY_WEBHOOK_SECRET",
    "FIRECRAWL_API_KEY",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_ACCESS_KEY_ID",
}

# Vars Company OS set from its own .env (explicitly tracked).
_OWNED: set[str] = set()
# Optional separate config dict (never merged into parent process os.environ by callers).
_CONFIG: dict[str, str] = {}


def load_dotenv_file(path: Path | str | None = None) -> dict[str, str]:
    """Parse a .env file into a dict without mutating os.environ."""
    p = Path(path) if path else Path(__file__).resolve().parents[2] / ".env"
    out: dict[str, str] = {}
    if not p.is_file():
        return out
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip("'").strip('"')
        out[key] = val
    return out


def set_company_os_config(config: dict[str, str], *, mark_auth: bool = True) -> None:
    """Store Company OS config separately; optionally mark auth keys as owned."""
    global _CONFIG
    _CONFIG = dict(config)
    if mark_auth:
        owned = [k for k in config if k in COMPANY_OS_OWNED_AUTH_VARS]
        mark_company_os_owned(owned)
        # Also record in env marker for child processes that only see env.
        existing = set(filter(None, os.environ.get("_COS_OWNED_AUTH", "").split(",")))
        existing |= set(owned)
        os.environ["_COS_OWNED_AUTH"] = ",".join(sorted(existing))


def company_os_config() -> dict[str, str]:
    return dict(_CONFIG)


def mark_company_os_owned(names: Iterable[str]) -> None:
    _OWNED.update(names)


def child_env(base: dict[str, str] | None = None) -> dict[str, str]:
    source = base if base is not None else os.environ
    owned = _OWNED | set(filter(None, source.get("_COS_OWNED_AUTH", "").split(",")))
    return {key: value for key, value in source.items()
            if key in CHILD_ENV_VARS and key not in owned}

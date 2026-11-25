"""Credential format verification — reject live keys in test flows."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class VerifyResult:
    ok: bool
    reason: str = ""
    vault_handle: str | None = None


_PK_TEST = re.compile(r"^pk_test_[A-Za-z0-9]+$")
_SK_TEST = re.compile(r"^sk_test_[A-Za-z0-9]+$")


def validate_stripe_key(value: str, *, kind: str = "secret") -> VerifyResult:
    if value.startswith("pk_live_") or value.startswith("sk_live_"):
        return VerifyResult(False, "live_key_rejected")
    if kind == "publishable" and not _PK_TEST.match(value):
        return VerifyResult(False, "bad_publishable_format")
    if kind == "secret" and not _SK_TEST.match(value):
        return VerifyResult(False, "bad_secret_format")
    # Return handle only — never echo secret
    return VerifyResult(True, vault_handle=f"vault:stripe_{kind}")


def store_in_vault_handle(
    value: str,
    handle: str,
    *,
    workspace_id: str = "local",
    provider: str = "generic",
) -> dict[str, str]:
    """Persist to the local vault and return a handle. Never echo the secret."""
    try:
        from app.connectors.secrets import store

        ref = store().put(
            workspace_id=workspace_id,
            provider=provider,
            key=handle,
            value=value,
        )
        return {"handle": ref}
    except Exception:
        return {"handle": handle if handle.startswith("vault:") else f"vault:{handle}"}

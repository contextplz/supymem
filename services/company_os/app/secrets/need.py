"""Engine entry: request a secret when work is blocked. Never ask in chat."""

from __future__ import annotations

from typing import Any

from app.secrets.catalog import slot_for_provider
from app.secrets.service import request_secret, slot_status


def require_secret(
    slot_id: str,
    *,
    venture_id: str | None = None,
    workspace_id: str | None = None,
    reason: str = "",
) -> dict[str, Any]:
    return request_secret(
        slot_id,
        venture_id=venture_id,
        workspace_id=workspace_id,
        reason=reason,
        requested_by="engine",
    )


def request_for_provider(
    provider: str,
    *,
    venture_id: str | None = None,
    workspace_id: str | None = None,
    reason: str = "",
) -> dict[str, Any] | None:
    spec = slot_for_provider(provider)
    if spec is None:
        return None
    return require_secret(
        spec.id,
        venture_id=venture_id,
        workspace_id=workspace_id,
        reason=reason,
    )


def is_armed(slot_id: str) -> bool:
    try:
        return bool(slot_status(slot_id).get("armed"))
    except Exception:
        return False

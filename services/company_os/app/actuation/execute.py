"""Safe entry point for invoking registered actuation tools."""

from __future__ import annotations

import os
from typing import Any

from app.actuation.contract import ActuationMode, ActuationResult, assert_live_is_honest
from app.actuation.registry import get


def _channels_dry_run() -> bool:
    return os.environ.get("CHANNELS_DRY_RUN", "true").lower() != "false"


async def execute(
    tool_id: str,
    venture_id: str,
    payload: dict[str, Any],
    *,
    allow_live: bool = False,
) -> ActuationResult:
    """Invoke a registered tool, defaulting safely to a simulated result."""
    spec = get(tool_id)
    if spec is None:
        return ActuationResult(
            mode=ActuationMode.REFUSED,
            tool_id=tool_id,
            venture_id=venture_id,
            ok=False,
            reason="unknown_tool",
        )

    if _channels_dry_run() or not allow_live:
        reason = "dry_run_env" if _channels_dry_run() else "live_not_allowed"
        return ActuationResult(
            mode=ActuationMode.SIMULATED,
            tool_id=tool_id,
            venture_id=venture_id,
            ok=True,
            reason=reason,
            dry_run=True,
            provider_response={"payload": payload},
        )

    result = await spec.handler(venture_id, payload)
    try:
        assert_live_is_honest(result)
    except ValueError:
        return result.model_copy(
            update={
                "mode": ActuationMode.FAILED,
                "ok": False,
                "reason": "live_without_external_ref",
            }
        )
    return result

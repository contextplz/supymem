"""Shared publish contract."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class PublishResult:
    ok: bool
    mode: str  # "live" | "simulated"
    external_id: str | None = None
    external_url: str | None = None
    error: str | None = None
    #: Set when the platform told us to stop rather than retry.
    fatal: bool = False
    status_code: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "mode": self.mode,
            "external_id": self.external_id,
            "external_url": self.external_url,
            "error": self.error,
            "fatal": self.fatal,
            "status_code": self.status_code,
        }


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def dry_run_enabled() -> bool:
    """Default is on. Publishing also needs the global execution opt-in."""
    from app.approvals.external import execution_enabled
    from app.core.local_mode import is_local
    if is_local():
        return not execution_enabled("content_publish")

    if not execution_enabled():
        return True
    explicit = os.environ.get("CONTENT_DRY_RUN")
    if explicit is not None:
        return _truthy(explicit)
    shared = os.environ.get("CHANNELS_DRY_RUN")
    if shared is not None:
        return _truthy(shared)
    return True


def simulated(platform: str) -> PublishResult:
    from core.ids import new_id

    fake = new_id("sim")
    return PublishResult(
        ok=True,
        mode="simulated",
        external_id=fake,
        external_url=f"dryrun://{platform}/{fake}",
    )


#: Statuses where retrying the same body will not help.
FATAL_STATUSES = frozenset({400, 401, 403, 404, 422})


def is_fatal(status_code: int) -> bool:
    return status_code in FATAL_STATUSES

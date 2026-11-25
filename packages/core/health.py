"""Shared liveness / readiness payload shape.

Probes themselves stay in each service (they know their engine and
dependencies). This module only defines the JSON contract so a load balancer
and an operator see the same keys on every process.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any


@dataclass(frozen=True)
class Check:
    """One dependency probe. ``detail`` must never contain a secret or a URL with credentials."""

    name: str
    ok: bool
    hard: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "hard": self.hard, "detail": self.detail}


def liveness_payload(service: str) -> dict[str, str]:
    """Process is up. No I/O. Always paired with HTTP 200."""
    body = {"status": "ok", "service": service}
    instance = os.environ.get("SUPYMEM_DESKTOP_INSTANCE_ID", "")
    if os.environ.get("SUPYMEM_MODE", "").strip().lower() == "local" and instance:
        body["desktop_instance"] = instance
    return body


def readiness_payload(
    service: str,
    checks: list[Check],
    *,
    app_env: str = "local",
) -> tuple[dict[str, Any], int]:
    """Build the /ready body.

    ``ready`` is false (HTTP 503) when any *hard* check failed. Soft failures
    set ``degraded`` but still return 200 — the process can serve, with gaps.
    """
    ready = all(c.ok for c in checks if c.hard)
    degraded = any(not c.ok for c in checks)
    body: dict[str, Any] = {
        "ready": ready,
        "service": service,
        "app_env": app_env,
        "degraded": degraded,
        "checks": {c.name: c.as_dict() for c in checks},
    }
    return body, 200 if ready else 503

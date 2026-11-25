from __future__ import annotations

from typing import Any

from app.build.gates.result import GateResult


def run(ctx: dict[str, Any] | None) -> GateResult:
    ctx = ctx or {}
    return GateResult(
        name="migration_check",
        passed=bool(ctx.get("migrations_ok")),
        detail="migrations",
    )

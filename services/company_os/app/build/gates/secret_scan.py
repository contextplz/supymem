from __future__ import annotations

from typing import Any

from app.build.gates.result import GateResult


def run(ctx: dict[str, Any] | None) -> GateResult:
    ctx = ctx or {}
    leaks = ctx.get("secret_leaks") or []
    return GateResult(name="secret_scan", passed=not leaks, detail=str(leaks))

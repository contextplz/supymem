from __future__ import annotations

from typing import Any

from app.build.gates.result import GateResult


def run(ctx: dict[str, Any] | None) -> GateResult:
    ctx = ctx or {}
    findings = ctx.get("static_findings") or []
    return GateResult(name="static_scan", passed=not findings, detail=str(findings))

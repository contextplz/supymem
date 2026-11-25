"""Twelve build gates (doc 09 §6). Engagements use a subset of named runners."""

from __future__ import annotations

from typing import Any

from app.build.gates import (
    error_handling,
    migration_check,
    secret_scan,
    static_scan,
    unit_tests,
)
from app.build.gates.result import GateResult

GATE_NAMES = (
    "typecheck",
    "lint",
    "unit",
    "integration",
    "security",
    "auth_enforcement",
    "scaffold_present",
    "llm_safety",
    "component_budget",
    "boot",
    "smoke_ui",
    "peer_review",
)


def _pass(name: str, ok: bool, detail: str = "") -> GateResult:
    return GateResult(name=name, passed=bool(ok), detail=detail)


def run_all(ctx: dict[str, Any] | None) -> list[GateResult]:
    ctx = ctx or {}
    components = list(ctx.get("components") or [])
    author = str(ctx.get("author_agent_id") or "")
    reviewer = str(ctx.get("reviewer_agent_id") or "")
    verdict = str(ctx.get("peer_verdict") or "")
    readiness = float(ctx.get("readiness_score") or 0)
    return [
        _pass("typecheck", bool(ctx.get("schema_ok")), "schema"),
        _pass("lint", not ctx.get("static_findings"), "static"),
        _pass("unit", bool(ctx.get("unit_tests_ok")), "unit"),
        _pass("integration", not ctx.get("blocking_issues"), "acceptance"),
        _pass("security", not ctx.get("secret_leaks"), "secrets"),
        _pass("auth_enforcement", bool(ctx.get("auth_live_ok")), "live 401"),
        _pass("scaffold_present", bool(ctx.get("scaffold_ok")), "scaffold"),
        _pass("llm_safety", not ctx.get("vulns"), "llm/deps"),
        _pass("component_budget", len(components) <= 20, f"{len(components)} components"),
        _pass("boot", bool(ctx.get("migrations_ok")), "boot/migrations"),
        _pass("smoke_ui", bool(ctx.get("error_boundary")) and readiness >= 0.5, "smoke"),
        _pass(
            "peer_review",
            bool(author) and bool(reviewer) and author != reviewer and verdict == "approve",
            "peer",
        ),
    ]


__all__ = [
    "GATE_NAMES",
    "GateResult",
    "error_handling",
    "migration_check",
    "run_all",
    "secret_scan",
    "static_scan",
    "unit_tests",
]

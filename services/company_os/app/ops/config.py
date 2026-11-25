"""Ops runtime flags — SANDBOX_MODE, OPS_EXECUTOR, Claude CLI mock."""

from __future__ import annotations

import os

VALID_EXECUTORS = frozenset({"mock", "gateway", "claude_cli", "qm"})


def _truthy(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def sandbox_mode() -> bool:
    """Hard gate for real channel writes. Defaults true (PS-4)."""
    return _truthy("SANDBOX_MODE", "true")


def ops_executor() -> str:
    """How agents obtain model output: mock | gateway | claude_cli | qm."""
    value = (os.environ.get("OPS_EXECUTOR") or "gateway").strip().lower()
    if value not in VALID_EXECUTORS:
        return "gateway"
    return value


def claude_cli_mock() -> bool:
    return _truthy("CLAUDE_CLI_MOCK", "false")


def claude_cli_mock_response() -> str:
    return os.environ.get(
        "CLAUDE_CLI_MOCK_RESPONSE",
        '{"result": "Mock Claude response for testing"}',
    )


def ops_api_key() -> str | None:
    key = (os.environ.get("OPS_API_KEY") or os.environ.get("API_KEY") or "").strip()
    return key or None


def stripe_webhook_secret() -> str | None:
    secret = (os.environ.get("STRIPE_WEBHOOK_SECRET") or "").strip()
    return secret or None

"""Shadow-mode write simulation (40 §6).

Reads and agent/rule/human steps run for real. External writes are logged as
intended actions and never reach a connector.
"""

from __future__ import annotations

from typing import Any


def is_shadow_run(*, workflow_status: str, run_shadow: bool) -> bool:
    """A run is shadowed when the workflow is in shadow status or the flag is set."""
    return run_shadow or workflow_status in {"shadow", "draft"}


def record_intended_write(
    simulated_writes: list[dict[str, Any]],
    *,
    step_n: int,
    label: str,
    provider: str,
    op: str,
    args: dict[str, Any] | None = None,
    target: str | None = None,
) -> dict[str, Any]:
    """Append one intended write. Caller persists ``simulated_writes`` on the run."""
    entry = {
        "step": step_n,
        "label": label,
        "kind": "connector_write",
        "provider": provider,
        "op": op,
        "args": args or {},
        "target": target,
        "executed": False,
        "shadow": True,
    }
    simulated_writes.append(entry)
    return entry


def summarize(simulated_writes: list[dict[str, Any]]) -> str:
    if not simulated_writes:
        return "No external writes were intended."
    parts = [
        f"would have called {w['provider']}.{w['op']}"
        + (f" on {w['target']}" if w.get("target") else "")
        for w in simulated_writes
    ]
    return "; ".join(parts)

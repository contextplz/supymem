"""Normalize coding-agent event streams."""

from __future__ import annotations

from typing import Any

from coding_agents.protocol import AgentEvent


def normalize_events(raw_lines: list[dict[str, Any]], adapter: Any) -> list[AgentEvent]:
    out: list[AgentEvent] = []
    for line in raw_lines:
        ev = adapter.parse_event(line)
        if ev is not None:
            out.append(ev)
    return out

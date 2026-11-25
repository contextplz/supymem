"""In-process registry of governed actuation tools."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.actuation.contract import ActuationResult

ToolHandler = Callable[[str, dict[str, Any]], Awaitable[ActuationResult]]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    tool_id: str
    handler: ToolHandler
    description: str = ""


_TOOLS: dict[str, ToolSpec] = {}


def _load_builtin_tools() -> None:
    # Deferred import avoids a registry/tool import cycle while making execute() usable at boot.
    from app.actuation import tools  # noqa: F401


def register(spec: ToolSpec) -> ToolSpec:
    """Register a tool, rejecting ambiguous duplicate IDs."""
    if spec.tool_id in _TOOLS:
        raise ValueError(f"tool already registered: {spec.tool_id}")
    _TOOLS[spec.tool_id] = spec
    return spec


def get(tool_id: str) -> ToolSpec | None:
    if tool_id not in _TOOLS:
        _load_builtin_tools()
    return _TOOLS.get(tool_id)


def all_tools() -> tuple[ToolSpec, ...]:
    _load_builtin_tools()
    return tuple(_TOOLS.values())

"""Adapter selection with infra-failure fallthrough (doc 08 §5 / doc 91 §4 / doc 95 §4)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from coding_agents.detect import DetectedAgent, detect
from coding_agents.env import is_local

Mode = Literal["local", "cloud", "inhouse_hosted", "auto"]
FailureClass = Literal["infra", "task"]


@dataclass
class Selection:
    adapter_name: str
    mode: Mode
    reason: str
    detected: DetectedAgent | None = None


DEFAULT_LOCAL_PREFS = ["opencode", "cursor", "claude", "codex", "inhouse"]
DEFAULT_CLOUD_PREFS = ["codex_cloud", "cursor_cloud", "claude_routine"]
CONNECT_TOOL_MESSAGE = "Connect your coding tool in Settings > Keys and connections > Coding tools, then ask in your venture chat to build again."


def hosted_inhouse_name() -> str:
    """Stub vs real builder. Default stays ``inhouse`` (doc 96 Rule 6 / 104)."""
    raw = (os.environ.get("CODING_AGENT") or "").strip().lower()
    if raw in {"openhands_builder", "openhands"}:
        return "openhands_builder"
    return "inhouse"


def select(
    *,
    kind: str = "code",
    prefs: list[str] | None = None,
    detected: list[DetectedAgent] | None = None,
    mode: Mode = "auto",
    bridge_connected: bool = True,
    cloud_keys: dict[str, bool] | None = None,
    inhouse_hosted_available: bool = True,
    failed: list[tuple[str, FailureClass]] | None = None,
) -> Selection | None:
    """Pick the next adapter.

    - Infrastructure failures (binary missing, auth, crash before first event) fall through.
    - Task failures do not fall through — caller must not re-invoke select with a task failure
      for the same Work Order (documented; enforced by leaving failed list empty for task).
    """
    failed = failed or []
    local = is_local()
    if local:
        mode = "local"
        if failed:
            return None
    cloud_keys = cloud_keys or {}
    prefs = list(prefs or (["codex", "claude", "cursor", "opencode"] if local else DEFAULT_LOCAL_PREFS))
    if local:
        prefs = [name for name in prefs if name in {"codex", "claude", "cursor", "opencode"}]
    found = {d.name: d for d in (detected if detected is not None else detect())}
    skipped = {name for name, cls in failed if cls == "infra"}

    # Structured-output preference: Codex when available (doc 08 §5).
    if not local and kind in {"structured", "schema"} and "codex" in found and "codex" not in skipped:
        if mode in {"auto", "local"} and bridge_connected:
            return Selection("codex", "local", "structured_output_prefers_codex", found["codex"])

    if mode in {"auto", "local"} and bridge_connected:
        for name in prefs:
            if name in skipped:
                continue
            if name in {"inhouse", "inhouse_stub"}:
                adapter = hosted_inhouse_name()
                if adapter in skipped:
                    adapter = "inhouse"
                if adapter in skipped:
                    continue
                mode_name: Mode = "inhouse_hosted" if adapter == "openhands_builder" else "local"
                return Selection(adapter, mode_name, "inhouse_local_fallback", None)
            if name == "openhands_builder":
                if "openhands_builder" in skipped:
                    continue
                return Selection(
                    "openhands_builder", "inhouse_hosted", "preference:openhands_builder", None
                )
            if name in found:
                if local and found[name].auth_mode == "api_key":
                    return None
                return Selection(name, "local", f"preference:{name}", found[name])

    if mode in {"auto", "cloud"}:
        for name in DEFAULT_CLOUD_PREFS:
            if name in skipped:
                continue
            key_ok = False
            if name == "cursor_cloud":
                key_ok = bool(cloud_keys.get("CURSOR_API_KEY"))
            elif name == "codex_cloud":
                key_ok = bool(cloud_keys.get("CODEX_API_KEY") or cloud_keys.get("OPENAI_API_KEY"))
            elif name == "claude_routine":
                # Connector-dependent work must not use Routines while beta bugs remain.
                if kind in {"connector", "mcp"}:
                    continue
                key_ok = bool(cloud_keys.get("ANTHROPIC_API_KEY") or cloud_keys.get("CLAUDE_ROUTINE_TOKEN"))
            if key_ok:
                return Selection(name, "cloud", f"cloud_key:{name}", None)

    if mode in {"auto", "inhouse_hosted"} and inhouse_hosted_available:
        adapter = hosted_inhouse_name()
        if adapter in skipped:
            adapter = "inhouse"
        if adapter not in skipped and "inhouse" not in skipped and "inhouse_hosted" not in skipped:
            return Selection(adapter, "inhouse_hosted", "inhouse_hosted_fallback", None)

    return None


def should_fall_through(failure_class: FailureClass) -> bool:
    return failure_class == "infra"

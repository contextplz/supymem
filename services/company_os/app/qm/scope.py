"""Workspace ↔ QM conversation/scope mapping (doc 55 §2).

All Company OS callers MUST use this module — never hand-roll threadRef/channelRef
strings (doc 58 R6).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ScopeRef:
    """Conversation fields Company OS sends on TurnRequest (not a QM ScopeId string)."""

    kind: str  # "dm" | "channel"
    thread_ref: str
    channel_ref: str | None = None
    channel_name: str | None = None

    def conversation_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": self.kind,
            "threadRef": self.thread_ref,
        }
        if self.channel_ref is not None:
            out["channelRef"] = self.channel_ref
        if self.channel_name is not None:
            out["channelName"] = self.channel_name
        return out


def workspace_to_scope(workspace: Any) -> ScopeRef:
    """Map a Company OS Workspace (or duck-typed object) to QM conversation fields.

    QM scope resolution (src/resolution/resolution-service.ts):
      dm      → personal:<actor.id>
      channel → channel:<channelRef|threadRef>
      group   → group:<ref>

    Company OS kinds:
      desk    → dm  (threadRef desk:<person_id>; QM personal scope = actor.externalId)
      venture → channel:venture:<workspace_id>
      company → not used for turns (org scope is config-only); raises ValueError
    """
    kind = getattr(workspace, "kind", None)
    if kind is None and isinstance(workspace, dict):
        kind = workspace.get("kind")
    wid = getattr(workspace, "id", None)
    if wid is None and isinstance(workspace, dict):
        wid = workspace.get("id")
    name = getattr(workspace, "name", None)
    if name is None and isinstance(workspace, dict):
        name = workspace.get("name")
    owner_id = getattr(workspace, "owner_id", None)
    if owner_id is None and isinstance(workspace, dict):
        owner_id = workspace.get("owner_id")

    if not kind or not wid:
        raise ValueError("workspace_to_scope requires workspace.kind and workspace.id")

    if kind == "desk":
        person = owner_id or "unknown"
        return ScopeRef(kind="dm", thread_ref=f"desk:{person}")

    if kind == "venture":
        ref = f"venture:{wid}"
        return ScopeRef(
            kind="channel",
            thread_ref=ref,
            channel_ref=ref,
            channel_name=str(name) if name else None,
        )

    if kind == "company":
        raise ValueError(
            "company workspaces map to QM org scope only; do not start turns as the org "
            "(doc 55 §2). Use a venture or desk workspace instead."
        )

    raise ValueError(f"unsupported workspace.kind for QM scope mapping: {kind!r}")

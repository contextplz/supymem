"""BYO coding-agent adapters. Cloud builds must not import local cursor/claude/codex CLIs."""

from __future__ import annotations

from coding_agents.env import hosted_execution


def _guard_cloud(name: str) -> None:
    # Local BYO CLIs are excluded from Company-OS-operated cloud (LD-03 / doc 08 §2).
    # Cloud *vendor* adapters (cursor_cloud etc.) are allowed — they use the founder's key
    # against the vendor's own infrastructure, never our servers running their CLI.
    if hosted_execution() and name in {
        "cursor",
        "claude",
        "codex",
        "opencode",
    }:
        raise RuntimeError(f"BYO adapter {name!r} excluded from cloud builds (LD-03)")


def load_adapter(name: str):
    _guard_cloud(name)
    if name == "cursor":
        from coding_agents.cursor import CursorAdapter

        return CursorAdapter()
    if name == "claude":
        from coding_agents.claude import ClaudeAdapter

        return ClaudeAdapter()
    if name == "codex":
        from coding_agents.codex import CodexAdapter

        return CodexAdapter()
    if name == "cursor_cloud":
        from coding_agents.cursor_cloud import CursorCloudAdapter

        return CursorCloudAdapter()
    if name == "codex_cloud":
        from coding_agents.codex_cloud import CodexCloudAdapter

        return CodexCloudAdapter()
    if name == "claude_routine":
        from coding_agents.claude_routine import ClaudeRoutineAdapter

        return ClaudeRoutineAdapter()
    if name in {"inhouse", "inhouse_stub"}:
        from coding_agents.inhouse import InhouseAdapter

        return InhouseAdapter()
    if name == "qm":
        from coding_agents.qm import QmAdapter

        return QmAdapter()
    if name == "opencode":
        from coding_agents.opencode import OpenCodeAdapter

        return OpenCodeAdapter()
    if name == "openhands_builder":
        from coding_agents.openhands_builder import OpenHandsBuilderAdapter

        return OpenHandsBuilderAdapter()
    raise KeyError(name)

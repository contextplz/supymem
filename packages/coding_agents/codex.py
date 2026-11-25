"""Codex adapter — recorded fixtures for CI; live CLI when CODING_AGENT_LIVE=1.

Flags re-checked at implementation time (doc 91 D4.6):
- `codex exec --json` for non-interactive JSONL
- `--sandbox workspace-write` when allow_write (default is read-only — silent no-op otherwise)
- `--skip-git-repo-check` for generated venture repos
- `--ephemeral` to avoid littering session files
"""

from __future__ import annotations

import os
from typing import Any

from coding_agents.protocol import AgentEvent, AgentEventKind, CodingRequest, CodingResult
from coding_agents.env import is_local


class CodexAdapter:
    name = "codex"
    write_flag = "--sandbox"

    def __init__(self, *, recorded: list[dict[str, Any]] | None = None) -> None:
        self.recorded = recorded

    def build_argv(self, req: CodingRequest) -> list[str]:
        argv = ["codex", "exec", "--json", "--skip-git-repo-check", "--ephemeral"]
        if req.allow_write:
            argv += [self.write_flag, "workspace-write"]
        else:
            argv += [self.write_flag, "read-only"]
        if req.model:
            argv += ["--model", req.model]
        # Codex uses -C / cwd; subprocess runner sets cwd=workdir.
        argv.append(req.prompt)
        return argv

    def parse_event(self, line: dict[str, Any]) -> AgentEvent | None:
        kind_s = str(line.get("type") or line.get("kind") or "message")
        item = line.get("item") if isinstance(line.get("item"), dict) else {}
        item_type = item.get("type")
        mapping = {
            "thread.started": "started",
            "turn.started": "started",
            "turn.completed": "finished",
            "turn.failed": "failed",
            "error": "failed",
            "item.completed": "tool_result",
        }
        kind_s = mapping.get(kind_s, kind_s)
        if item_type in {"agent_message", "reasoning"}:
            kind_s = "message"
        elif item_type == "command_execution":
            kind_s = "tool_result" if line.get("type") == "item.completed" else "tool_call"
        elif item_type == "file_change":
            kind_s = "file_changed" if line.get("type") == "item.completed" else "tool_call"
        try:
            kind = AgentEventKind(kind_s)
        except ValueError:
            return None
        usage = line.get("usage") if isinstance(line.get("usage"), dict) else {}
        raw_text = item.get("text") or item.get("command") or line.get("text") or line.get("message")
        text = raw_text if isinstance(raw_text, str) else None
        changes = item.get("changes") if isinstance(item.get("changes"), list) else []
        path = next((change.get("path") for change in changes if isinstance(change, dict)), line.get("path"))
        error = line.get("error")
        if kind == AgentEventKind.FAILED and isinstance(error, dict):
            text = error.get("message") if isinstance(error.get("message"), str) else text
        return AgentEvent(
            kind=kind,
            text=text,
            path=path,
            tool_name="Bash" if item_type == "command_execution" else item_type,
            tool_call_id=item.get("id"),
            session_id=line.get("thread_id") or line.get("session_id"),
            tokens_in=usage.get("input_tokens"),
            tokens_out=usage.get("output_tokens"),
            reasoning_tokens=usage.get("reasoning_output_tokens"),
            raw=line,
        )

    async def run(self, req: CodingRequest) -> CodingResult:
        argv = self.build_argv(req)
        write_flag_used = self.write_flag in argv and "workspace-write" in argv
        events: list[AgentEvent] = []
        changed: list[str] = []
        if self.recorded is not None:
            for raw in self.recorded:
                ev = self.parse_event(raw)
                if ev is None:
                    continue
                events.append(ev)
                if ev.kind == AgentEventKind.FILE_CHANGED and ev.path:
                    changed.append(ev.path)
            ok = write_flag_used if req.allow_write else True
            if req.allow_write and not write_flag_used:
                return CodingResult(
                    ok=False,
                    error="missing_write_flag",
                    events=events,
                    changed_files=changed,
                    write_flag_used=False,
                    argv=argv,
                )
            return CodingResult(
                ok=ok,
                session_id="rec",
                changed_files=changed,
                events=events,
                write_flag_used=write_flag_used,
                argv=argv,
            )
        if is_local() or os.environ.get("CODING_AGENT_LIVE", "0").lower() in {"1", "true", "yes"}:
            from coding_agents.subprocess_runner import run_live

            return await run_live(adapter=self, req=req)
        return CodingResult(
            ok=False, error="live_not_enabled", argv=argv, write_flag_used=write_flag_used
        )

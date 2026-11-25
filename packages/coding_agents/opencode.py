"""OpenCode adapter — SST/anomalyco CLI as a Plan/Build coding engine."""

from __future__ import annotations

import shutil
from typing import Any

from coding_agents.protocol import AgentEvent, AgentEventKind, CodingRequest, CodingResult
from coding_agents.env import is_local


class OpenCodeAdapter:
    name = "opencode"
    write_flag = "run"

    def __init__(self, *, recorded: list[dict[str, Any]] | None = None) -> None:
        self.recorded = recorded

    def binary(self) -> str:
        from desktop_runtime.agent_status import tool_binary

        return tool_binary(self.name) or "opencode"

    def build_argv(self, req: CodingRequest) -> list[str]:
        argv = [self.binary(), "run", "--format", "json"]
        if req.resume_session_id:
            argv += ["--session", req.resume_session_id]
        if req.model:
            argv += ["--model", req.model]
        argv.append(req.prompt)
        return argv

    def parse_event(self, line: dict[str, Any]) -> AgentEvent | None:
        kind_s = str(line.get("type") or line.get("kind") or "message")
        mapping = {
            "text": "message",
            "tool": "tool_call",
            "tool_use": "tool_call",
            "step_start": "started",
            "step_finish": "usage",
            "result": "finished",
            "error": "failed",
        }
        kind_s = mapping.get(kind_s, kind_s)
        part = line.get("part") if isinstance(line.get("part"), dict) else {}
        if (line.get("type") or line.get("kind")) == "step_finish" and part.get("reason") == "stop":
            kind_s = "finished"
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        if part.get("type") == "tool":
            kind_s = "tool_result" if state.get("status") == "completed" else "tool_call"
            if state.get("status") == "error":
                kind_s = "failed"
        try:
            kind = AgentEventKind(kind_s)
        except ValueError:
            return None
        inputs = state.get("input") if isinstance(state.get("input"), dict) else {}
        text = inputs.get("command") or state.get("output") or part.get("text") or line.get("text") or line.get("message")
        if isinstance(text, dict):
            text = str(text.get("text") or text)
        return AgentEvent(
            kind=kind,
            text=text if isinstance(text, str) else None,
            path=inputs.get("filePath") or inputs.get("path") or line.get("path"),
            tool_name=part.get("tool"),
            tool_call_id=part.get("callID"),
            session_id=line.get("sessionID") or line.get("session_id"),
            raw=line,
        )

    async def run(self, req: CodingRequest) -> CodingResult:
        argv = self.build_argv(req)
        if self.recorded is not None:
            events = [ev for raw in self.recorded if (ev := self.parse_event(raw))]
            changed = [ev.path for ev in events if ev.path]
            return CodingResult(
                ok=True,
                session_id="rec",
                changed_files=[p for p in changed if p],
                events=events,
                write_flag_used=True,
                argv=argv,
            )
        if not is_local() and not shutil.which("opencode"):
            return CodingResult(ok=False, error="binary_missing", argv=argv, write_flag_used=True)
        from coding_agents.subprocess_runner import run_live

        return await run_live(adapter=self, req=req)

"""Cursor adapter — recorded/fake mode for tests; real CLI when CODING_AGENT_LIVE=1."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import json

from coding_agents.protocol import AgentEvent, AgentEventKind, CodingRequest, CodingResult
from coding_agents.env import is_local


def sandbox_failure(line: dict[str, Any]) -> str | None:
    error = line.get("error")
    call = line.get("tool_call")
    if isinstance(call, dict) and line.get("subtype") == "completed":
        for tool in call.values():
            result = tool.get("result") if isinstance(tool, dict) else None
            if not isinstance(result, dict):
                continue
            success = result.get("success")
            error = result.get("error") or (success if isinstance(success, dict)
                and type(success.get("exitCode")) is int and success["exitCode"] != 0 else error)
    if error is None and (line.get("type") == "error" or line.get("is_error") is True
            or str(line.get("subtype", "")).startswith("error")):
        error = line
    if error is None or not any(marker in json.dumps(error).lower() for marker in (
            "sandbox", "outside workspace", "outside the workspace",
            "permission denied", "operation not permitted")):
        return None
    message = (error.get("message") or error.get("stderr")) if isinstance(error, dict) else error
    return message if isinstance(message, str) and message else "Your tool could not safely change files inside your build folder."


class CursorAdapter:
    name = "cursor"
    write_flag = "--force"

    def __init__(self, *, recorded: list[dict[str, Any]] | None = None) -> None:
        self.recorded = recorded

    def build_argv(self, req: CodingRequest) -> list[str]:
        from desktop_runtime.agent_status import tool_binary

        argv = [tool_binary(self.name) or "cursor-agent"]
        argv += ["-p", "--print", "--output-format", "stream-json", "--stream-partial-output"]
        argv += ["--trust", "--workspace", str(req.workdir)]
        if req.allow_write:
            argv.append(self.write_flag)
        argv += ["--sandbox", "disabled" if req.allow_network else "enabled"]
        if req.model:
            argv += ["--model", req.model]
        if req.resume_session_id:
            argv += ["--resume", req.resume_session_id]
        if os_environ_truthy("CODING_AGENT_APPROVE_MCPS"):
            argv.append("--approve-mcps")
        argv.append(req.prompt)
        return argv

    def parse_event(self, line: dict[str, Any]) -> AgentEvent | None:
        kind_s = str(line.get("type") or line.get("kind") or "message")
        # Map common Cursor stream-json types onto our enum when possible.
        mapping = {
            "assistant": "message",
            "tool_call": "tool_call",
            "tool_result": "tool_result",
            "result": "finished",
            "error": "failed",
            "system": "started",
        }
        kind_s = mapping.get(kind_s, kind_s)
        if line.get("type") == "result" and (line.get("is_error") is True
                or str(line.get("subtype", "")).startswith("error")):
            kind_s = "failed"
        call = line.get("tool_call") if isinstance(line.get("tool_call"), dict) else {}
        name = next(iter(call), None)
        tool = call.get(name) if isinstance(call.get(name), dict) else {}
        args = tool.get("args") if isinstance(tool.get("args"), dict) else {}
        if line.get("type") == "tool_call" and line.get("subtype") == "completed":
            kind_s = "tool_result"
        try:
            kind = AgentEventKind(kind_s)
        except ValueError:
            return None
        text = args.get("command") or line.get("text") or line.get("message") or line.get("result")
        if isinstance(text, dict):
            content = text.get("content")
            text = "".join(part.get("text", "") for part in content
                if isinstance(part, dict) and isinstance(part.get("text"), str)) if isinstance(content, list) else text.get("text")
        path = args.get("path") or args.get("file_path") or line.get("path")
        if not path and isinstance(line.get("tool_call"), dict):
            path = (line["tool_call"].get("write") or {}).get("path")
        failure = sandbox_failure(line)
        if failure:
            kind, text = AgentEventKind.FAILED, failure
        elif name in {"writeToolCall", "editToolCall"} and path:
            kind = AgentEventKind.FILE_CHANGED
        return AgentEvent(
            kind=kind,
            text=text if isinstance(text, str) else None,
            path=path,
            tool_name=name,
            tool_call_id=line.get("call_id") or line.get("tool_call_id"),
            session_id=line.get("session_id") or line.get("chatId"),
            raw=line,
        )

    async def run(self, req: CodingRequest) -> CodingResult:
        argv = self.build_argv(req)
        write_flag_used = self.write_flag in argv
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
        # Live path via subprocess_runner when enabled.
        import os

        if is_local() or os.environ.get("CODING_AGENT_LIVE", "0").lower() in {"1", "true", "yes"}:
            from coding_agents.subprocess_runner import run_live

            return await run_live(adapter=self, req=req)
        return CodingResult(ok=False, error="live_not_enabled", argv=argv, write_flag_used=write_flag_used)


def os_environ_truthy(name: str) -> bool:
    import os

    return os.environ.get(name, "").lower() in {"1", "true", "yes"}

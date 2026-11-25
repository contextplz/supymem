"""Claude Code adapter — recorded fixtures for CI; live CLI when CODING_AGENT_LIVE=1.

Flags re-checked against Claude Code CLI docs at implementation time (doc 91 §2 / D4.6):
- `-p` / `--print` for non-interactive
- `--output-format stream-json` for NDJSON events
- `--dangerously-skip-permissions` when allow_write (subscription / headless)
- Do NOT pass `--bare` when founder relies on subscription auth (breaks OAuth/keychain)
- Pass `--bare` only when CLAUDE_AUTH_MODE=api_key
"""

from __future__ import annotations

import os
from typing import Any

from coding_agents.protocol import AgentEvent, AgentEventKind, CodingRequest, CodingResult
from coding_agents.env import is_local


class ClaudeAdapter:
    name = "claude"
    write_flag = "--dangerously-skip-permissions"

    def __init__(self, *, recorded: list[dict[str, Any]] | None = None) -> None:
        self.recorded = recorded

    def build_argv(self, req: CodingRequest) -> list[str]:
        argv = ["claude", "-p", "--output-format", "stream-json"]
        auth_mode = os.environ.get("CLAUDE_AUTH_MODE", "subscription").lower()
        if auth_mode == "api_key" and not is_local():
            argv.append("--bare")
        if req.allow_write:
            argv.append(self.write_flag)
        # Claude Code uses cwd; we set cwd in the subprocess runner to workdir.
        if req.model:
            argv += ["--model", req.model]
        argv.append(req.prompt)
        return argv

    def parse_event(self, line: dict[str, Any]) -> AgentEvent | None:
        kind_s = str(line.get("type") or line.get("kind") or "message")
        mapping = {
            "assistant": "message",
            "result": "finished",
            "error": "failed",
        }
        kind_s = mapping.get(kind_s, kind_s)
        content = (line.get("message") or {}).get("content", []) if isinstance(line.get("message"), dict) else []
        tool = next((item for item in content if isinstance(item, dict)
                     and item.get("type") in {"tool_use", "tool_result"}), {}) if isinstance(content, list) else {}
        if tool:
            kind_s = "tool_call" if tool.get("type") == "tool_use" else "tool_result"
        if line.get("type") == "result" and (line.get("is_error") is True
                or str(line.get("subtype", "")).startswith("error")):
            kind_s = "failed"
        try:
            kind = AgentEventKind(kind_s)
        except ValueError:
            return None
        inputs = tool.get("input") if isinstance(tool.get("input"), dict) else {}
        raw_text = inputs.get("command") or tool.get("content") or line.get("text") or line.get("result")
        if not raw_text and isinstance(content, list):
            raw_text = "\n".join(item["text"] for item in content
                if isinstance(item, dict) and isinstance(item.get("text"), str))
        text = raw_text if isinstance(raw_text, str) else None
        usage = line.get("usage") if isinstance(line.get("usage"), dict) else {}
        return AgentEvent(
            kind=kind,
            text=text,
            path=inputs.get("file_path") or line.get("path"),
            tool_name=tool.get("name"),
            tool_call_id=tool.get("id") or tool.get("tool_use_id"),
            tokens_in=usage.get("input_tokens"),
            tokens_out=usage.get("output_tokens"),
            session_id=line.get("session_id"),
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
        if is_local() or os.environ.get("CODING_AGENT_LIVE", "0").lower() in {"1", "true", "yes"}:
            from coding_agents.subprocess_runner import run_live

            return await run_live(adapter=self, req=req)
        return CodingResult(ok=False, error="live_not_enabled", argv=argv, write_flag_used=write_flag_used)

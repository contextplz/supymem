"""Claude Code Routines (research preview) — founder's own token (doc 95 §2).

Labeled beta: skip for connector/MCP-dependent Work Orders via select().
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from coding_agents.protocol import AgentEvent, AgentEventKind, CodingRequest, CodingResult


class ClaudeRoutineAdapter:
    name = "claude_routine"
    write_flag = "--dangerously-skip-permissions"

    def build_argv(self, req: CodingRequest) -> list[str]:
        return ["claude-routine", str(req.workdir), req.prompt]

    def parse_event(self, line: dict[str, Any]) -> AgentEvent | None:
        return AgentEvent(kind=AgentEventKind.MESSAGE, text=str(line.get("text") or ""), raw=line)

    async def run(self, req: CodingRequest) -> CodingResult:
        token = (
            os.environ.get("CLAUDE_ROUTINE_TOKEN", "").strip()
            or os.environ.get("ANTHROPIC_API_KEY", "").strip()
        )
        routine_id = os.environ.get("CLAUDE_ROUTINE_ID", "").strip()
        if not token or not routine_id:
            return CodingResult(ok=False, error="auth_failure", argv=self.build_argv(req))

        if os.environ.get("CLAUDE_ROUTINE_DRY_RUN", "").lower() in {"1", "true", "yes"}:
            return CodingResult(
                ok=True,
                session_id="claude_routine_dry",
                events=[AgentEvent(kind=AgentEventKind.FINISHED, text="dry_run")],
                argv=self.build_argv(req),
                write_flag_used=True,
            )

        url = (
            os.environ.get("CLAUDE_ROUTINE_FIRE_URL")
            or f"https://api.anthropic.com/v1/claude_code/routines/{routine_id}/fire"
        )
        payload = {"text": req.prompt}
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "anthropic-beta": "experimental-cc-routine-2026-04-01",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as resp:
                body = json.loads(resp.read().decode("utf-8") or "{}")
            return CodingResult(
                ok=True,
                session_id=str(body.get("session_id") or body.get("id") or "claude_routine"),
                events=[
                    AgentEvent(
                        kind=AgentEventKind.FINISHED,
                        text=str(body.get("url") or body.get("status") or "fired"),
                        raw=body if isinstance(body, dict) else {},
                    )
                ],
                argv=self.build_argv(req),
                write_flag_used=True,
            )
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                return CodingResult(ok=False, error="auth_failure", argv=self.build_argv(req))
            return CodingResult(ok=False, error=f"exit_{exc.code}", argv=self.build_argv(req))
        except Exception as exc:  # noqa: BLE001
            return CodingResult(ok=False, error=f"binary_missing:{exc}", argv=self.build_argv(req))

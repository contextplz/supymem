"""In-house coding agent — used when no BYO adapter is available."""

from __future__ import annotations

from typing import Any

from coding_agents.env import is_local
from coding_agents.protocol import AgentEvent, AgentEventKind, CodingRequest, CodingResult
from coding_agents.select import CONNECT_TOOL_MESSAGE


class InhouseAdapter:
    name = "inhouse"
    write_flag = "--write"

    def build_argv(self, req: CodingRequest) -> list[str]:
        return ["inhouse-coder", self.write_flag, str(req.workdir), req.prompt]

    def parse_event(self, line: dict[str, Any]) -> AgentEvent | None:
        return AgentEvent(kind=AgentEventKind.MESSAGE, text=str(line), raw=line)

    async def run(self, req: CodingRequest) -> CodingResult:
        if is_local():
            return CodingResult(ok=False, error=CONNECT_TOOL_MESSAGE)
        # Deterministic offline: create a marker file if allow_write
        changed: list[str] = []
        if req.allow_write:
            target = req.workdir / "inhouse_change.txt"
            target.write_text("ok\n", encoding="utf-8")
            changed.append(str(target.relative_to(req.workdir)))
        return CodingResult(
            ok=True,
            session_id="inhouse",
            changed_files=changed,
            events=[AgentEvent(kind=AgentEventKind.FINISHED, text="done")],
            write_flag_used=True,
            argv=self.build_argv(req),
        )

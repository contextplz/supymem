"""QM harness as a BYO coding-agent provider (doc 55 §6, 56 QM-P4)."""

from __future__ import annotations

import os
from typing import Any

from coding_agents.protocol import AgentEvent, AgentEventKind, CodingRequest, CodingResult


class QmAdapter:
    name = "qm"
    write_flag = "--write"

    def build_argv(self, req: CodingRequest) -> list[str]:
        return ["qm-turn", self.write_flag, str(req.workdir), req.prompt[:80]]

    def parse_event(self, line: dict[str, Any]) -> AgentEvent | None:
        return AgentEvent(kind=AgentEventKind.MESSAGE, text=str(line), raw=line)

    async def run(self, req: CodingRequest) -> CodingResult:
        # Import Company OS QM client only when this adapter runs inside company_os.
        try:
            from app.qm.client import run_turn
            from app.qm.scope import ScopeRef
            from app.qm import settings as qm_settings
        except ImportError as exc:
            return CodingResult(
                ok=False,
                error=f"QM client unavailable: {exc}",
                argv=self.build_argv(req),
            )

        venture_id = os.environ.get("QM_BUILD_VENTURE_ID") or "build"
        scope = ScopeRef(
            kind="channel",
            thread_ref=f"venture:{venture_id}",
            channel_ref=f"venture:{venture_id}",
            channel_name="build",
        )
        text = (
            "You are building an MVP in a real sandbox for Company OS onboarding.\n"
            f"Working directory on the agent computer should mirror: {req.workdir}\n\n"
            f"{req.prompt}\n\n"
            "Write the resulting files. Prefer a simple static landing page "
            "(index.html + README.md + package.json) unless the brief says otherwise. "
            "When finished, reply with a short summary of files written."
        )
        try:
            result = await run_turn(
                scope=scope,
                text=text,
                idempotency_key=f"build:{venture_id}:{hash(req.prompt) & 0xFFFFFFFF:x}",
                timeout_s=qm_settings.qm_build_turn_timeout_s(),
                read_only=False,
            )
        except Exception as exc:  # noqa: BLE001
            return CodingResult(
                ok=False,
                error=str(exc),
                events=[AgentEvent(kind=AgentEventKind.FAILED, text=str(exc))],
                argv=self.build_argv(req),
            )

        ok = result.status in {"ok", "silent", "react"}
        return CodingResult(
            ok=ok,
            session_id=result.run_id,
            changed_files=[],  # QM sandbox files are not mirrored into workdir in v1
            events=[
                AgentEvent(
                    kind=AgentEventKind.FINISHED if ok else AgentEventKind.FAILED,
                    text=result.reply or result.reason or result.status,
                )
            ],
            write_flag_used=True,
            argv=self.build_argv(req),
            error=None if ok else (result.reason or result.status),
        )

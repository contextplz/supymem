"""Cursor Cloud Agent adapter — founder's own CURSOR_API_KEY (doc 95 §2)."""

from __future__ import annotations

import os
from typing import Any

from coding_agents.protocol import AgentEvent, AgentEventKind, CodingRequest, CodingResult


class CursorCloudAdapter:
    name = "cursor_cloud"
    write_flag = "--force"  # unused for cloud; Protocol requires the attribute

    def build_argv(self, req: CodingRequest) -> list[str]:
        return ["cursor-sdk-cloud", str(req.workdir), req.prompt]

    def parse_event(self, line: dict[str, Any]) -> AgentEvent | None:
        return AgentEvent(kind=AgentEventKind.MESSAGE, text=str(line.get("text") or ""), raw=line)

    async def run(self, req: CodingRequest) -> CodingResult:
        api_key = os.environ.get("CURSOR_API_KEY", "").strip()
        if not api_key:
            return CodingResult(ok=False, error="auth_failure", argv=self.build_argv(req))

        # Prefer the official SDK when installed; otherwise return a clear infra error.
        try:
            from cursor_sdk import Agent, LocalAgentOptions  # type: ignore
        except ImportError:
            try:
                # Some installs expose the cloud path via Agent.create(cloud=...)
                from cursor_sdk import Agent  # type: ignore

                LocalAgentOptions = None  # noqa: N806
            except ImportError:
                return CodingResult(
                    ok=False,
                    error="binary_missing",
                    argv=self.build_argv(req),
                )

        try:
            # Cloud runtime: repos cloned on Cursor-hosted VMs.
            # We pass the founder's key only — never a platform key.
            kwargs: dict[str, Any] = {
                "api_key": api_key,
                "model": req.model or "composer-2.5",
            }
            # Prefer cloud if the SDK supports it; fall back to local cwd for smoke.
            try:
                from cursor_sdk import CloudAgentOptions  # type: ignore

                kwargs["cloud"] = CloudAgentOptions(
                    repos=[{"url": str(req.workdir), "branch": "main"}]
                )
            except Exception:  # noqa: BLE001
                if LocalAgentOptions is not None:
                    kwargs["local"] = LocalAgentOptions(cwd=str(req.workdir))

            # Sync SDK surface varies; support both prompt and create/send.
            if hasattr(Agent, "prompt"):
                result = Agent.prompt(req.prompt, **kwargs)  # type: ignore[arg-type]
                status = getattr(result, "status", "finished")
                ok = status in {"finished", "ok", "completed", None}
                return CodingResult(
                    ok=ok,
                    session_id=getattr(result, "id", None) or "cursor_cloud",
                    events=[
                        AgentEvent(
                            kind=AgentEventKind.FINISHED if ok else AgentEventKind.FAILED,
                            text=str(getattr(result, "result", "") or ""),
                        )
                    ],
                    argv=self.build_argv(req),
                    write_flag_used=True,
                    error=None if ok else str(status),
                )
            return CodingResult(ok=False, error="binary_missing", argv=self.build_argv(req))
        except Exception as exc:  # noqa: BLE001
            return CodingResult(ok=False, error=f"auth_failure:{exc}", argv=self.build_argv(req))

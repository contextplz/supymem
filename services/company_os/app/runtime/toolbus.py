"""Toolbus — every tool call is warden-checked. No exceptions."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from app.core.warden_client import CheckResponse, WardenClient

ToolHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]] | dict[str, Any]]


class ToolBus:
    def __init__(self, warden: WardenClient | None = None) -> None:
        self.warden = warden or WardenClient()
        self._handlers: dict[str, ToolHandler] = {}
        self._register_builtins()

    def _register_builtins(self) -> None:
        async def _echo(args: dict[str, Any]) -> dict[str, Any]:
            return {"ok": True, "echo": args}

        for name in (
            "list_agents",
            "set_priority",
            "emit_signal",
            "summarize_runs",
            "propose_code",
            "propose_config",
            "check_policy",
            "run_oracle",
            "run_rl_smoke",
            "ingest_feedback",
            "list_feedback",
            "approve_proposal",
            "block_proposal",
        ):
            self._handlers[name] = _echo

    def register(self, name: str, handler: ToolHandler) -> None:
        self._handlers[name] = handler

    async def call(
        self,
        tool: str,
        args: dict[str, Any],
        *,
        venture_id: str = "",
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        verdict: CheckResponse = await self.warden.check(
            capability=f"tool:{tool}",
            venture_id=venture_id,
            agent_id=agent_id,
            target=tool,
            args_digest="",
        )
        if verdict.decision != "allow":
            return {
                "ok": False,
                "error": "warden_denied",
                "reason": verdict.reason,
                "decision": verdict.decision,
            }
        handler = self._handlers.get(tool)
        if handler is None:
            return {"ok": False, "error": "unknown_tool", "tool": tool}
        result = handler(args)
        if hasattr(result, "__await__"):
            result = await result  # type: ignore[misc]
        return {"ok": True, "result": result}


toolbus = ToolBus()

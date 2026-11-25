"""Agent runtime loop — warden on every tool, kill switch, reasoning_content."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.warden_client import WardenClient
from app.runtime.handlers import install_handlers, reset_tool_context, tool_context
from app.runtime.toolbus import ToolBus, toolbus
from harness import TurnConfig, run_turn
from llm_gateway.messages import assistant_message_preserving_reasoning


@dataclass
class AgentRunResult:
    status: str
    steps: int = 0
    messages: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    final_text: str = ""


class AgentRuntime:
    def __init__(
        self,
        *,
        warden: WardenClient | None = None,
        bus: ToolBus | None = None,
        llm: Any | None = None,
    ) -> None:
        self.warden = warden or WardenClient()
        self.bus = bus or toolbus
        self.llm = llm
        install_handlers(self.bus)

    async def run(
        self,
        *,
        venture_id: str,
        agent_id: str,
        goal: str,
        system_prompt: str,
        tools: list[str],
        max_steps: int = 8,
        recorded_steps: list[dict[str, Any]] | None = None,
        live: bool = False,
        workspace_id: str | None = None,
        facts: dict[str, Any] | None = None,
        inbox: Any = None,
        on_pre_step: Any = None,
        model_tier: str = "flash",
    ) -> AgentRunResult:
        async def _call(name: str, args: dict[str, Any]) -> dict[str, Any]:
            from app.memory.scope import bound_memory_scope
            with bound_memory_scope(venture_id, workspace_id=workspace_id):
                return await self.bus.call(
                    name, args, venture_id=venture_id, agent_id=agent_id,
                )

        token = tool_context(
            venture_id=venture_id, agent_id=agent_id, workspace_id=workspace_id
        )
        try:
            turned = await run_turn(
                TurnConfig(
                    venture_id=venture_id,
                    agent_id=agent_id,
                    goal=goal,
                    system_prompt=system_prompt,
                    tools=list(tools or []),
                    max_steps=max_steps,
                    recorded_steps=recorded_steps,
                    live=live and recorded_steps is None,
                    workspace_id=workspace_id,
                    model_tier=model_tier,
                    facts=facts or {},
                    tool_caller=_call,
                    kill_switch=self.warden.kill_switch,
                    llm=self.llm,
                    inbox=inbox,
                    on_pre_step=on_pre_step,
                )
            )
        finally:
            reset_tool_context(token)
        return AgentRunResult(
            status=turned.status,
            steps=turned.steps,
            messages=turned.messages,
            reason=turned.reason,
            tool_results=turned.tool_results,
            final_text=turned.final_text,
        )


def preserve_reasoning_from_response(resp: Any) -> dict[str, Any]:
    return assistant_message_preserving_reasoning(resp)

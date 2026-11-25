"""Base ops agent — mock / LLM gateway / Claude CLI / QM (docs 49, 55–56)."""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
from abc import ABC, abstractmethod
from typing import Any

from coding_agents.env import child_env, hosted_execution

from app.ops import config as ops_config

log = logging.getLogger(__name__)


class BaseOpsAgent(ABC):
    agent_type: str = "base"

    def __init__(self) -> None:
        self._qm_task: dict[str, Any] = {}
        self._qm_context: dict[str, Any] = {}

    @abstractmethod
    def run(self, task: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        """Execute the agent. Must return a dict with at least ``summary``."""

    def timed_run(self, task: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        # Stash for OPS_EXECUTOR=qm (call_claude has no task/context args today).
        self._qm_task = task or {}
        self._qm_context = context or {}
        start = time.monotonic()
        result = self.run(task, context)
        if not isinstance(result, dict):
            result = {"summary": str(result)}
        result = dict(result)
        result["duration_secs"] = round(time.monotonic() - start, 4)
        return result

    def call_claude(self, prompt: str) -> str:
        """Return model text. Never spawns a subprocess in mock mode."""
        if ops_config.claude_cli_mock() or ops_config.ops_executor() == "mock":
            return self._mock_result()

        executor = ops_config.ops_executor()
        if executor == "gateway":
            return self._call_gateway(prompt)
        if executor == "claude_cli":
            return self._call_claude_cli(prompt)
        if executor == "qm":
            return self._call_qm(prompt)
        return self._mock_result()

    def call_claude_json(self, prompt: str) -> dict[str, Any]:
        raw = self.call_claude(prompt)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {"summary": raw, "raw": raw}
        if isinstance(parsed, dict):
            return parsed
        return {"summary": str(parsed), "value": parsed}

    def _mock_result(self) -> str:
        payload = ops_config.claude_cli_mock_response()
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return payload
        if isinstance(data, dict) and "result" in data:
            result = data["result"]
            return result if isinstance(result, str) else json.dumps(result)
        return payload if isinstance(payload, str) else json.dumps(data)

    def _call_gateway(self, prompt: str) -> str:
        from llm_gateway import sole

        async def _run() -> str:
            resp = await sole.env_chat_completion(
                [{"role": "user", "content": prompt}],
                tier="flash",
                max_tokens=1024,
            )
            choice = resp.choices[0].message.content if resp.choices else ""
            return choice or ""

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    return pool.submit(lambda: asyncio.run(_run())).result()
            return loop.run_until_complete(_run())
        except RuntimeError:
            return asyncio.run(_run())

    def _call_claude_cli(self, prompt: str) -> str:
        if hosted_execution():
            raise RuntimeError("cloud_forbids_byo")
        proc = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "json"],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
            env=child_env(),
        )
        if proc.returncode != 0:
            raise RuntimeError(f"claude CLI failed: {proc.stderr.strip()}")
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("claude CLI returned non-JSON") from exc
        if isinstance(data, dict) and "result" in data:
            result = data["result"]
            return result if isinstance(result, str) else json.dumps(result)
        return proc.stdout

    def _call_qm(self, prompt: str) -> str:
        """Run one QM turn for this ops agent (doc 55 §5). Sync wrapper over async client."""
        from app.qm.client import QmClientError, run_turn
        from app.qm.scope import ScopeRef, workspace_to_scope
        from app.qm import settings as qm_settings
        from app.workspaces.resolve import LOCAL_PERSON_ID

        ctx = getattr(self, "_qm_context", None) or {}
        task = getattr(self, "_qm_task", None) or {}
        workspace_id = ctx.get("workspace_id") or ""
        workspace_kind = ctx.get("workspace_kind") or "venture"
        workspace_name = ctx.get("workspace_name") or ctx.get("company", {}).get("name")
        owner_id = ctx.get("owner_id") or ctx.get("person_id") or LOCAL_PERSON_ID

        if workspace_kind == "desk":
            scope = workspace_to_scope(
                {
                    "kind": "desk",
                    "id": workspace_id or "desk",
                    "owner_id": owner_id,
                    "name": workspace_name,
                }
            )
        elif workspace_id:
            scope = workspace_to_scope(
                {
                    "kind": "venture",
                    "id": workspace_id,
                    "name": workspace_name,
                    "owner_id": owner_id,
                }
            )
        else:
            # Fallback: personal DM scope so a turn can still run without a workspace.
            scope = ScopeRef(kind="dm", thread_ref=f"desk:{owner_id}")

        skill_name = f"ops-{self.agent_type.replace('_', '-')}"
        text = (
            f"Use the skill at skills/{skill_name}/SKILL.md to complete this task.\n\n"
            f"{prompt}"
        )
        task_id = task.get("id") or task.get("task_id") or "anon"
        attempt = task.get("attempt") or 1
        idem = f"ops-task:{task_id}:attempt-{attempt}:{self.agent_type}"

        async def _run() -> str:
            result = await run_turn(
                scope=scope,
                text=text,
                idempotency_key=idem,
                timeout_s=qm_settings.qm_ops_turn_timeout_s(),
                person_id=str(owner_id),
            )
            if result.status in {"failed", "refused"}:
                raise RuntimeError(
                    f"QM turn {result.run_id} {result.status}: {result.reason or result.reply or 'no detail'}"
                )
            return result.reply or ""

        def _bridge() -> str:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    import concurrent.futures

                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                        return pool.submit(lambda: asyncio.run(_run())).result()
                return loop.run_until_complete(_run())
            except RuntimeError:
                return asyncio.run(_run())

        try:
            return _bridge()
        except QmClientError as exc:
            raise RuntimeError(f"QM client error: {exc}") from exc

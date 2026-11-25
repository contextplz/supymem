"""In-house hosted builder — HTTP client to ``services/builder`` (doc 104 B3).

Implements the existing ``CodingAdapter`` Protocol. Does not import OpenHands;
the builder process owns the sandbox.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from coding_agents.env import is_local
from coding_agents.protocol import AgentEvent, AgentEventKind, CodingRequest, CodingResult
from coding_agents.select import CONNECT_TOOL_MESSAGE


class OpenHandsBuilderAdapter:
    name = "openhands_builder"
    write_flag = "--write"

    def build_argv(self, req: CodingRequest) -> list[str]:
        return ["openhands-builder", self.write_flag, str(req.workdir), req.prompt[:80]]

    def parse_event(self, line: dict[str, Any]) -> AgentEvent | None:
        return AgentEvent(kind=AgentEventKind.MESSAGE, text=str(line), raw=line)

    def _url(self) -> str:
        return (os.environ.get("BUILDER_SERVICE_URL") or "http://127.0.0.1:8090").rstrip("/")

    def _headers(self) -> dict[str, str]:
        token = (os.environ.get("BUILDER_SERVICE_TOKEN") or "").strip()
        if not token:
            return {}
        return {"Authorization": f"Bearer {token}"}

    def _estimate_micro(self) -> int:
        raw = (os.environ.get("BUILDER_CREDIT_RESERVE_MICRO") or "50000").strip()
        try:
            return max(int(raw), 0)
        except ValueError:
            return 50_000

    async def _post(self, req: CodingRequest) -> tuple[CodingResult, int | None, int | None]:
        argv = self.build_argv(req)
        empty = CodingResult(ok=False, argv=argv)
        payload = {
            "prompt": req.prompt,
            "workdir": str(req.workdir),
            "timeout_s": req.timeout_s,
            "allow_write": req.allow_write,
            "allow_network": req.allow_network,
            "model": req.model,
            "preview": True,
            "venture_slug": req.workdir.name,
            "workspace_id": os.environ.get("BUILDER_WORKSPACE_ID"),
        }
        try:
            async with httpx.AsyncClient(timeout=float(req.timeout_s) + 30.0) as client:
                response = await client.post(
                    f"{self._url()}/run",
                    json=payload,
                    headers=self._headers(),
                )
        except httpx.ConnectError:
            return empty.model_copy(update={"error": "infra"}), None, None
        except httpx.TimeoutException:
            return empty.model_copy(update={"error": "timed_out"}), None, None

        if response.status_code == 401:
            return empty.model_copy(update={"error": "auth_failure"}), None, None
        if response.status_code >= 500:
            return empty.model_copy(update={"error": "infra"}), None, None
        try:
            body = response.json()
        except Exception:  # noqa: BLE001
            return empty.model_copy(update={"error": "infra"}), None, None

        events = []
        for raw in body.get("events") or []:
            try:
                events.append(AgentEvent.model_validate(raw))
            except Exception:  # noqa: BLE001
                continue
        result = CodingResult(
            ok=bool(body.get("ok")),
            session_id=body.get("session_id"),
            changed_files=list(body.get("changed_files") or []),
            events=events,
            error=body.get("error"),
            write_flag_used=True,
            argv=argv,
            preview_url=body.get("preview_url"),
        )
        prompt_tokens = body.get("prompt_tokens")
        completion_tokens = body.get("completion_tokens")
        return result, prompt_tokens, completion_tokens

    async def run(self, req: CodingRequest) -> CodingResult:
        if is_local():
            return CodingResult(ok=False, error=CONNECT_TOOL_MESSAGE)
        argv = self.build_argv(req)
        try:
            from app.credits.errors import CreditsUnavailable, InsufficientCredits
            from app.credits.gate import charge
        except Exception:  # noqa: BLE001 — unit tests / native without company_os
            result, _, _ = await self._post(req)
            return result

        try:
            async with charge(
                workspace_id=os.environ.get("BUILDER_WORKSPACE_ID"),
                purpose="builder",
                estimate_micro=self._estimate_micro(),
                model=req.model,
                provider="openhands_builder",
            ) as meter:
                result, prompt_tokens, completion_tokens = await self._post(req)
                if prompt_tokens is not None:
                    meter.record(
                        prompt_tokens=int(prompt_tokens or 0),
                        completion_tokens=int(completion_tokens or 0),
                    )
                else:
                    meter.record_micro(0)
                return result
        except (InsufficientCredits, CreditsUnavailable):
            return CodingResult(ok=False, error="insufficient_credits", argv=argv)
        except Exception as exc:  # noqa: BLE001
            return CodingResult(ok=False, error=f"infra:{type(exc).__name__}", argv=argv)

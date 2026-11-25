"""OpenAI Codex Cloud adapter — founder's own CODEX_API_KEY / OPENAI_API_KEY (doc 95 §2)."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from coding_agents.protocol import AgentEvent, AgentEventKind, CodingRequest, CodingResult


class CodexCloudAdapter:
    name = "codex_cloud"
    write_flag = "--sandbox"

    def build_argv(self, req: CodingRequest) -> list[str]:
        return ["codex-cloud", str(req.workdir), req.prompt]

    def parse_event(self, line: dict[str, Any]) -> AgentEvent | None:
        return AgentEvent(kind=AgentEventKind.MESSAGE, text=str(line.get("text") or ""), raw=line)

    async def run(self, req: CodingRequest) -> CodingResult:
        api_key = (
            os.environ.get("CODEX_API_KEY", "").strip()
            or os.environ.get("OPENAI_API_KEY", "").strip()
        )
        if not api_key:
            return CodingResult(ok=False, error="auth_failure", argv=self.build_argv(req))

        base = os.environ.get("CODEX_CLOUD_BASE_URL", "https://api.openai.com").rstrip("/")
        # Best-effort task create. Exact OpenAI Codex Cloud REST paths evolve; we keep
        # this adapter thin and treat HTTP/auth failures as infra fallthrough.
        payload = {
            "prompt": req.prompt,
            "workdir": str(req.workdir),
            "model": req.model or "codex-mini-latest",
        }
        url = os.environ.get("CODEX_CLOUD_TASKS_URL", f"{base}/v1/codex/tasks")
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        # Allow offline/unit tests to skip the network.
        if os.environ.get("CODEX_CLOUD_DRY_RUN", "").lower() in {"1", "true", "yes"}:
            return CodingResult(
                ok=True,
                session_id="codex_cloud_dry",
                events=[AgentEvent(kind=AgentEventKind.FINISHED, text="dry_run")],
                argv=self.build_argv(req),
                write_flag_used=True,
            )
        try:
            with urllib.request.urlopen(request, timeout=60) as resp:
                body = json.loads(resp.read().decode("utf-8") or "{}")
            return CodingResult(
                ok=True,
                session_id=str(body.get("id") or body.get("task_id") or "codex_cloud"),
                events=[
                    AgentEvent(
                        kind=AgentEventKind.FINISHED,
                        text=str(body.get("status") or "submitted"),
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

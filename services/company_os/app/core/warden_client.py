"""Local owner policy or fail-closed Warden HTTP client."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError

from app.core.local_mode import is_local

log = logging.getLogger("company_os.warden_client")

DEFAULT_TIMEOUT_S = 0.5
DEFAULT_RETRIES = 1  # one retry → two total attempts


class CheckResponse(BaseModel):
    decision: str
    reason: str
    quota_remaining: dict[str, int] = Field(default_factory=dict)
    expires_in_s: int = 0


DenyEmitter = Callable[[str, str], None]


def _local_check(capability: str) -> CheckResponse:
    name = capability.removeprefix("tool:")
    required = {
        "send_email": "send_email", "message_customer": "send_email",
        "publish_pages": "publish_pages", "publish": "publish_pages", "deploy": "publish_pages",
        "connected_apps": "connected_apps", "connect_systems": "connected_apps",
        "post_public": "connected_apps", "manage_apps": "connected_apps",
    }.get(name)
    if name.startswith("connection:"):
        required = "connected_apps"
    if required:
        try:
            from app.core.local_capabilities import blocked_message, enabled
        except ImportError:
            # shortcut: setup-only gating without local_capabilities, use owner switches when that module ships.
            from app.core.local_secrets import get_secret, read_settings

            section = {"send_email": "smtp", "publish_pages": "publishing", "connected_apps": "composio"}[required]
            values = read_settings(section)["values"]
            if required == "send_email":
                ready = bool(all(values.get(field) for field in ("host", "user", "postal_address"))
                             and get_secret(section, "password"))
                next_step = "Open Keys and connections > Email, save your mailbox host, user, password and postal address, then retry."
            elif required == "connected_apps":
                ready = bool(get_secret(section, "api_key") and values.get("auth_configs"))
                next_step = "Open Keys and connections > Connected apps, save your Composio key and connect the app you want to use, then retry."
            else:
                provider = values.get("provider")
                ready = bool(values.get("project") and get_secret(section, "token")) if provider in {"vercel", "netlify"} else bool(
                    provider == "s3" and values.get("bucket") and values.get("region")
                    and get_secret(section, "access_key_id") and get_secret(section, "secret_access_key"))
                next_step = (
                    "Open Keys and connections > Publishing, save your S3 bucket, region, access key ID and secret access key, then retry."
                    if provider == "s3" else
                    "Open Keys and connections > Publishing, choose Vercel or Netlify and save your project and publishing token, then retry."
                )
        else:
            ready = enabled(required)
            next_step = blocked_message(required) if not ready else ""
        return CheckResponse(decision="allow" if ready else "deny", reason="" if ready else next_step)
    if name == "db:destructive":
        return CheckResponse(decision="deny", reason="Ask for the specific data change in chat before changing or deleting your data.")
    if name in {
        "read", "list", "get", "search", "plan", "request_work", "approve", "change_autonomy", "run_workflow",
        "set_priority", "emit_signal", "check_policy", "run_oracle", "run_rl_smoke",
        "ingest_feedback", "approve_proposal", "block_proposal",
    } or name.startswith(("read_", "list_", "get_", "search_", "draft_", "propose_", "summarize_")) or name == "explore_web":
        return CheckResponse(decision="allow", reason="")
    return CheckResponse(decision="deny", reason="Ask in chat for a supported read, draft, email, publishing or connected-app action.")


def _signed(path: str, method: str) -> dict[str, str]:
    """Warden requires a system assertion when the shared secret is configured."""
    secret = (os.environ.get("COS_INTERNAL_ASSERTION_SECRET") or "").strip()
    if not secret:
        return {}
    from core.assertion import sign_assertion

    token = sign_assertion(secret, sub="company_os", email="", role="system", path=path, method=method)
    return {"X-COS-Assertion": token}


def _default_emit(reason: str, detail: str) -> None:
    """Best-effort governance.denied emission without hard-depending on eventbus."""
    try:
        # Fire-and-forget sync wrapper: schedule if loop running, else skip.
        import asyncio

        from app.core.eventbus import bus

        async def _pub() -> None:
            await bus.publish(
                "governance.denied",
                {"reason": reason, "detail": detail},
                actor="warden_client",
            )

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_pub())
        except RuntimeError:
            log.warning("governance.denied reason=%s detail=%s", reason, detail)
    except Exception:  # noqa: BLE001
        log.warning("governance.denied reason=%s detail=%s", reason, detail)


class WardenClient:
    """Local policy in process; hosted timeout, error or malformed response denies."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        retries: int = DEFAULT_RETRIES,
        emit: DenyEmitter | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = (
            base_url
            or os.environ.get("WARDEN_URL")
            or "http://127.0.0.1:3400"
        ).rstrip("/")
        self.timeout_s = timeout_s
        self.retries = retries
        self.emit = emit or _default_emit
        self._transport = transport
        # Never cache allows — a stale allow after kill-switch is a safety bug.
        self._cached_allow: None = None

    def _deny_unavailable(self, detail: str) -> CheckResponse:
        self.emit("warden_unavailable", detail)
        return CheckResponse(decision="deny", reason="warden_unavailable")

    async def check(
        self,
        *,
        capability: str,
        venture_id: str = "",
        agent_id: str | None = None,
        target: str | None = None,
        args_digest: str = "",
        risk: str = "low",
        shadow: bool = False,
        autonomy_overrides: dict[str, str] | None = None,
        cost_usd: float | None = None,
        active_agents: int | None = None,
        **_extra: Any,
    ) -> CheckResponse:
        if is_local():
            response = _local_check(capability)
            if response.decision != "allow":
                self.emit("local_policy_denied", response.reason)
            return response
        payload = {
            "venture_id": venture_id,
            "agent_id": agent_id,
            "capability": capability,
            "target": target,
            "args_digest": args_digest,
            "risk": risk,
            "shadow": shadow,
            "autonomy_overrides": autonomy_overrides,
            "cost_usd": cost_usd,
            "active_agents": active_agents,
        }
        attempts = self.retries + 1
        last_err: Exception | None = None
        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout_s,
            transport=self._transport,
        ) as client:
            for _ in range(attempts):
                try:
                    r = await client.post("/check", json=payload, headers=_signed("/check", "POST"))
                    r.raise_for_status()
                    data = r.json()
                    resp = CheckResponse.model_validate(data)
                    if resp.decision not in {"allow", "deny", "escalate"}:
                        return self._deny_unavailable("malformed_decision")
                    return resp
                except (httpx.HTTPError, ValidationError, ValueError, KeyError) as exc:
                    last_err = exc
                    continue
        return self._deny_unavailable(str(last_err) if last_err else "unknown")

    async def kill_switch(self) -> bool:
        if is_local():
            return False
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout_s,
                transport=self._transport,
            ) as client:
                r = await client.get("/kill-switch", headers=_signed("/kill-switch", "GET"))
                r.raise_for_status()
                return bool(r.json().get("kill_switch"))
        except Exception:  # noqa: BLE001 — fail closed: treat as active
            self.emit("warden_unavailable", "kill_switch_probe_failed")
            return True


# Module-level default used by runtime code.
warden = WardenClient()

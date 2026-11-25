"""Authenticated hosted LLM HTTP surface.

Mounted on Company OS (and later ECS) at ``/api/v1/llm``. Azure is contacted
only after a user JWT is accepted and credits are reserved. BYOK usage
records a platform fee and never calls Azure.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from llm_gateway.hosted import BYOK_PLATFORM_FEE_MICRO, PUBLIC_MODEL_ID

Authenticate = Callable[[str | None], str]
AzureComplete = Callable[..., Awaitable[Any]]
ChargeDefault = Callable[..., Any]
DebitByokFee = Callable[[str], None]


class AuthFailed(Exception):
    pass


class HostedDenied(Exception):
    """Plan expiry / zero credits — do not call Azure."""

    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        self.detail = detail
        super().__init__(detail)


@dataclass
class HostedLLMHooks:
    authenticate: Authenticate
    azure_complete: AzureComplete
    charge_default: ChargeDefault | None = None
    debit_byok_fee: DebitByokFee | None = None
    azure_calls: list[dict[str, Any]] = field(default_factory=list)
    byok_fees: list[str] = field(default_factory=list)

    def gateway_enabled(self) -> bool:
        return (os.environ.get("HOSTED_LLM_GATEWAY_ENABLED") or "").strip() == "1"


def _bearer(authorization: str | None) -> str | None:
    raw = (authorization or "").strip()
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    return raw or None


def create_hosted_llm_router(hooks: HostedLLMHooks, *, prefix: str = "") -> APIRouter:
    router = APIRouter(prefix=prefix, tags=["hosted-llm"])

    def _require_user(authorization: str | None) -> str:
        token = _bearer(authorization)
        try:
            return hooks.authenticate(token)
        except AuthFailed as exc:
            raise HTTPException(status_code=401, detail="authentication required") from exc

    @router.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {
            "ok": True,
            "service": "hosted-llm",
            "public_model": PUBLIC_MODEL_ID,
            "enabled": hooks.gateway_enabled(),
        }

    @router.post("/v1/chat/completions")
    async def chat_completions(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> Any:
        if not hooks.gateway_enabled():
            raise HTTPException(status_code=503, detail="hosted LLM gateway is not enabled")
        user_id = _require_user(authorization)
        body = await request.json()
        # Ignore any client-supplied api_key / Azure deployment name.
        body.pop("api_key", None)
        model = body.get("model") or PUBLIC_MODEL_ID
        if model not in {PUBLIC_MODEL_ID, "company-os-default"}:
            raise HTTPException(status_code=400, detail="unknown model")
        messages = body.get("messages") or []
        call = {
            "user_id": user_id,
            "messages": messages,
            "max_tokens": body.get("max_tokens"),
            "purpose": body.get("purpose") or "hosted_default",
            "workspace_id": body.get("workspace_id"),
        }
        if hooks.charge_default is not None:
            try:
                hooks.charge_default(user_id, route="spend_default_llm")
            except HostedDenied as exc:
                raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
        hooks.azure_calls.append(call)
        result = await hooks.azure_complete(**call)
        if isinstance(result, dict):
            result = {**result, "model": PUBLIC_MODEL_ID}
        return JSONResponse(result)

    @router.post("/anthropic/v1/messages")
    async def anthropic_messages(
        request: Request,
        authorization: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None, alias="x-api-key"),
    ) -> Any:
        if not hooks.gateway_enabled():
            raise HTTPException(status_code=503, detail="hosted LLM gateway is not enabled")
        presented = _bearer(authorization) or (x_api_key or "").strip()
        user_id = _require_user(presented)
        body = await request.json()
        body.pop("api_key", None)
        call = {
            "user_id": user_id,
            "messages": body.get("messages") or [],
            "max_tokens": body.get("max_tokens"),
            "purpose": "hosted_anthropic",
            "workspace_id": (body.get("metadata") or {}).get("workspace_id"),
        }
        if hooks.charge_default is not None:
            try:
                hooks.charge_default(user_id, route="spend_default_llm")
            except HostedDenied as exc:
                raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
        hooks.azure_calls.append(call)
        return await hooks.azure_complete(**call)

    @router.post("/usage/byok")
    async def byok_usage(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        user_id = _require_user(authorization)
        body = await request.json()
        _ = body
        hooks.byok_fees.append(user_id)
        if hooks.debit_byok_fee is not None:
            hooks.debit_byok_fee(user_id)
        return {
            "ok": True,
            "route": "spend_byok_platform_fee",
            "fee_micro": BYOK_PLATFORM_FEE_MICRO,
            "azure_contacted": False,
        }

    return router


def static_token_authenticator(expected: str) -> Authenticate:
    def _auth(token: str | None) -> str:
        presented = (token or "").strip()
        if not expected or not presented or not secrets.compare_digest(presented, expected):
            raise AuthFailed("invalid token")
        return "usr_hosted"

    return _auth

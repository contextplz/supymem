"""Desktop → hosted LLM gateway (no Azure keys on the device).

Default model for a signed-in desktop is an HTTPS hop to OUR server
(``HOSTED_LLM_BASE_URL``) with a user JWT. The server holds ``LLM_*`` in
Secrets Manager and debits the credits ledger. BYOK never uses this path.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from core.desktop_env import is_packaged_desktop

from llm_gateway.errors import ProviderCallError

PUBLIC_MODEL_ID = "company-os-default"
PUBLIC_MODEL_LABEL = "Company OS default"
BYOK_PLATFORM_FEE_MICRO = 100


class HostedAuthRequired(ProviderCallError):
    def __init__(self, message: str = "Sign in to use Company OS default.") -> None:
        super().__init__("auth_required", message, status=401)


class HostedCreditsRequired(ProviderCallError):
    def __init__(
        self,
        message: str = "Not enough credits for Company OS default. Top up on the website.",
    ) -> None:
        super().__init__("credit_required", message, status=402)


class HostedPlanExpired(ProviderCallError):
    def __init__(
        self,
        message: str = "Your 14-day plan expired. Buy a pack on the website to use Company OS default.",
    ) -> None:
        super().__init__("plan_expired", message, status=402)


class HostedUpdateRequired(ProviderCallError):
    def __init__(
        self,
        message: str = "This version of Company OS must be updated before using Company OS default.",
    ) -> None:
        super().__init__("update_required", message, status=426)


def hosted_session() -> tuple[str, str] | None:
    base = (os.environ.get("HOSTED_LLM_BASE_URL") or "").strip().rstrip("/")
    token = (
        os.environ.get("HOSTED_ACCOUNT_TOKEN")
        or os.environ.get("HOSTED_LLM_TOKEN")
        or ""
    ).strip()
    if base and token:
        return base, token
    return None


def platform_env_allowed() -> bool:
    """Founder laptop ``LLM_*`` path. Packaged desktop must not dial Azure."""
    return not is_packaged_desktop()


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        # Never send platform LLM_API_KEY. Ignore any client-supplied key.
    }


async def hosted_chat_completion(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    max_tokens: int | None = None,
    purpose: str = "",
    workspace_id: str | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> Any:
    session = hosted_session()
    if session is None:
        raise HostedAuthRequired()
    base, token = session
    payload: dict[str, Any] = {
        "model": PUBLIC_MODEL_ID,
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": False,
        "purpose": purpose,
    }
    if workspace_id:
        payload["workspace_id"] = workspace_id
    if tools:
        payload["tools"] = tools
    url = f"{base}/v1/chat/completions"
    own_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=120.0)
    try:
        resp = await client.post(url, json=payload, headers=_headers(token))
    finally:
        if own_client:
            await client.aclose()
    if resp.status_code == 401:
        raise HostedAuthRequired()
    if resp.status_code == 402:
        raise HostedCreditsRequired()
    if resp.status_code >= 400:
        raise ProviderCallError(
            "hosted_gateway_error",
            f"Hosted LLM gateway returned HTTP {resp.status_code}",
            status=resp.status_code,
            retryable=resp.status_code >= 500,
        )
    return resp.json()


__all__ = [
    "BYOK_PLATFORM_FEE_MICRO",
    "PUBLIC_MODEL_ID",
    "PUBLIC_MODEL_LABEL",
    "HostedAuthRequired",
    "HostedCreditsRequired",
    "HostedPlanExpired",
    "HostedUpdateRequired",
    "hosted_chat_completion",
    "hosted_session",
    "is_packaged_desktop",
    "platform_env_allowed",
]

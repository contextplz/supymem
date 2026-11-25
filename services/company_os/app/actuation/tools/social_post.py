"""Publish social content through Zernio (Outstand fallback). Never fabricate live."""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any

import httpx

from app.actuation.contract import ActuationMode, ActuationResult, assert_live_is_honest
from app.actuation.providers import zernio as zernio_api
from app.actuation.registry import ToolSpec, register
from app.actuation.social import resolve_social_key, wanted_platforms


def _outstand_endpoint() -> str:
    return os.getenv("OUTSTAND_API_URL", "https://api.outstand.so/v1/posts")


async def _publish_zernio(
    venture_id: str,
    api_key: str,
    payload: dict[str, Any],
) -> ActuationResult:
    content = str(payload.get("content") or payload.get("text") or payload.get("body") or "").strip()
    if not content:
        return ActuationResult(
            mode=ActuationMode.FAILED,
            tool_id="social.post",
            venture_id=venture_id,
            ok=False,
            reason="empty_content",
        )
    wanted = wanted_platforms(payload)
    idempotency_key = hashlib.sha256(
        json.dumps(
            {"venture_id": venture_id, "content": content, "platforms": wanted},
            sort_keys=True,
        ).encode()
    ).hexdigest()
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            accounts = await zernio_api.list_accounts(client, api_key)
            platforms = zernio_api.select_platforms(accounts, wanted=wanted or None)
            if not platforms:
                return ActuationResult(
                    mode=ActuationMode.FAILED,
                    tool_id="social.post",
                    venture_id=venture_id,
                    ok=False,
                    reason="no_connected_accounts",
                    provider_response={"accounts": len(accounts), "wanted": wanted},
                )
            body: dict[str, Any] = {
                "content": content,
                "platforms": platforms,
                "publishNow": bool(payload.get("publishNow", True)),
                "isDraft": bool(payload.get("isDraft", False)),
                "visibility": str(payload.get("visibility") or "public"),
                "title": str(payload.get("title") or content[:80]),
            }
            if payload.get("scheduledFor"):
                body["scheduledFor"] = payload["scheduledFor"]
                body["publishNow"] = False
            data = await zernio_api.create_post(
                client,
                api_key,
                body,
                idempotency_key=f"social:{venture_id}:{idempotency_key}",
            )
    except httpx.HTTPStatusError as exc:
        return ActuationResult(
            mode=ActuationMode.FAILED,
            tool_id="social.post",
            venture_id=venture_id,
            ok=False,
            reason=f"zernio_http:{exc.response.status_code}",
        )
    except (httpx.HTTPError, ValueError) as exc:
        return ActuationResult(
            mode=ActuationMode.FAILED,
            tool_id="social.post",
            venture_id=venture_id,
            ok=False,
            reason=f"zernio_error:{exc.__class__.__name__}",
        )
    external_ref = zernio_api.extract_post_id(data)
    if not external_ref:
        return ActuationResult(
            mode=ActuationMode.FAILED,
            tool_id="social.post",
            venture_id=venture_id,
            ok=False,
            reason="zernio_response_missing_post_id",
            provider_response=data,
        )
    result = ActuationResult(
        mode=ActuationMode.LIVE,
        tool_id="social.post",
        venture_id=venture_id,
        ok=True,
        external_ref=external_ref,
        external_url=zernio_api.extract_post_url(data),
        provider_response={
            "provider": "zernio",
            "platforms": platforms,
            "id": external_ref,
        },
    )
    assert_live_is_honest(result)
    return result


async def _publish_outstand(
    venture_id: str,
    api_key: str,
    payload: dict[str, Any],
) -> ActuationResult:
    body = {**payload, "metadata": {**dict(payload.get("metadata") or {}), "venture_id": venture_id}}
    idempotency_key = hashlib.sha256(
        json.dumps(body, sort_keys=True, default=str).encode()
    ).hexdigest()
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                _outstand_endpoint(),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Idempotency-Key": f"social:{venture_id}:{idempotency_key}",
                },
                json=body,
            )
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return ActuationResult(
            mode=ActuationMode.FAILED,
            tool_id="social.post",
            venture_id=venture_id,
            ok=False,
            reason=f"outstand_error:{exc.__class__.__name__}",
        )
    data = data if isinstance(data, dict) else {}
    external_ref = data.get("id") or data.get("post_id") or data.get("external_id")
    if not external_ref:
        return ActuationResult(
            mode=ActuationMode.FAILED,
            tool_id="social.post",
            venture_id=venture_id,
            ok=False,
            reason="outstand_response_missing_post_id",
            provider_response=data,
        )
    result = ActuationResult(
        mode=ActuationMode.LIVE,
        tool_id="social.post",
        venture_id=venture_id,
        ok=True,
        external_ref=str(external_ref),
        external_url=str(data.get("url") or data.get("post_url") or "") or None,
        provider_response=data,
    )
    assert_live_is_honest(result)
    return result


async def social_post(venture_id: str, payload: dict[str, Any]) -> ActuationResult:
    provider = resolve_social_key(venture_id)
    if provider is None:
        return ActuationResult(
            mode=ActuationMode.SIMULATED,
            tool_id="social.post",
            venture_id=venture_id,
            ok=True,
            reason="no_credential",
            provider_response={"payload": payload},
        )
    name, api_key = provider
    if name == "outstand":
        return await _publish_outstand(venture_id, api_key, payload)
    return await _publish_zernio(venture_id, api_key, payload)


register(ToolSpec(tool_id="social.post", handler=social_post, description="Publish through Zernio or Outstand"))

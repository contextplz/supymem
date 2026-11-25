"""Transactional email tool backed by Resend when explicitly configured."""

from __future__ import annotations

import os

import httpx

from app.actuation.contract import ActuationMode, ActuationResult
from app.actuation.registry import ToolSpec, register
from app.credits.errors import CreditError
from app.credits.metered import call, price_micro


async def email_send(venture_id: str, payload: dict[str, object]) -> ActuationResult:
    """Send exactly one email, or truthfully report simulation/failure."""
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        return ActuationResult(
            mode=ActuationMode.SIMULATED,
            tool_id="email.send",
            venture_id=venture_id,
            ok=True,
            reason="no_credential",
            provider_response={"payload": payload},
        )
    async def send():
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
            )
        response.raise_for_status()
        return response

    try:
        recipients = sum(len(value) if isinstance(value, list) else 1
                         for field in ("to", "cc", "bcc") if (value := payload.get(field)))
        response = await call(send, purpose="actuation.email.send", provider="resend",
                              venture_id=venture_id, estimate_micro=price_micro("email_send", max(1, recipients)))
        response_data = response.json()
        message_id = response_data.get("id")
        if not message_id:
            return ActuationResult(
                mode=ActuationMode.FAILED,
                tool_id="email.send",
                venture_id=venture_id,
                ok=False,
                reason="resend_response_missing_message_id",
                provider_response=response_data,
            )
        return ActuationResult(
            mode=ActuationMode.LIVE,
            tool_id="email.send",
            venture_id=venture_id,
            ok=True,
            external_ref=str(message_id),
            provider_response=response_data,
        )
    except CreditError as exc:
        return ActuationResult(mode=ActuationMode.REFUSED, tool_id="email.send",
                               venture_id=venture_id, ok=False, reason=exc.code)
    except httpx.HTTPError:
        return ActuationResult(
            mode=ActuationMode.FAILED,
            tool_id="email.send",
            venture_id=venture_id,
            ok=False,
            reason="email_send_unavailable",
        )


register(ToolSpec(tool_id="email.send", handler=email_send, description="Send email through Resend"))

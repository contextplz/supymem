"""email.read — IMAP reply sweep (doc 115 §7).

v1 records the tool and returns honest empty / simulated results until mailbox
credentials are connected. Never scrapes LinkedIn/X DMs.
"""

from __future__ import annotations

from app.actuation.contract import ActuationMode, ActuationResult
from app.actuation.registry import ToolSpec, register


async def email_read(venture_id: str, payload: dict[str, object]) -> ActuationResult:
    mailbox_id = payload.get("mailbox_id")
    if not mailbox_id:
        return ActuationResult(
            mode=ActuationMode.SIMULATED,
            tool_id="email.read",
            venture_id=venture_id,
            ok=True,
            reason="no_mailbox",
            provider_response={"replies": []},
        )
    # Live IMAP (imap-tools) lands with mailbox connect. Stub is intentional.
    return ActuationResult(
        mode=ActuationMode.SIMULATED,
        tool_id="email.read",
        venture_id=venture_id,
        ok=True,
        reason="imap_not_connected",
        provider_response={"mailbox_id": mailbox_id, "replies": []},
    )


register(
    ToolSpec(
        tool_id="email.read",
        handler=email_read,
        description="Read inbound replies from a connected mailbox (IMAP)",
    )
)

"""GitHub webhook verification.

GitHub signs the raw body with the hook secret and sends no timestamp, so
freshness cannot be checked and replay protection is the delivery id
(``X-GitHub-Delivery``) alone. The body is never parsed before the signature
matches.
"""

from __future__ import annotations

import json

from app.connectors.protocol import WebhookEvent
from app.connectors.webhooks import WebhookRejected, header, verify_hex

SIGNATURE_HEADER = "X-Hub-Signature-256"
EVENT_HEADER = "X-GitHub-Event"
DELIVERY_HEADER = "X-GitHub-Delivery"
EVENTS = ("push", "pull_request", "issues", "check_run")


def verify(raw: bytes, headers: dict[str, str], secret: str) -> WebhookEvent:
    signature = header(headers, SIGNATURE_HEADER)
    digest = signature.removeprefix("sha256=")
    if not verify_hex(secret, raw, digest):
        raise WebhookRejected("signature mismatch")
    delivery = header(headers, DELIVERY_HEADER)
    if not delivery:
        raise WebhookRejected("missing delivery id")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise WebhookRejected("unparseable body") from exc
    repo = (payload.get("repository") or {}).get("full_name")
    return WebhookEvent(
        provider="github",
        delivery_id=delivery,
        event=header(headers, EVENT_HEADER) or "unknown",
        payload=payload,
        target=repo,
    )

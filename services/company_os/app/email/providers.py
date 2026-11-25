"""Provider adapters. Add one by writing ``send(msg) -> provider_id`` and listing it in PROVIDERS."""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

import httpx

from app.core.local_mode import is_local

log = logging.getLogger("app.email.providers")


class ProviderError(Exception):
    def __init__(self, code: str, retryable: bool):
        super().__init__(code)
        self.code, self.retryable = code, retryable


@dataclass
class Outgoing:
    to: str
    subject: str
    html: str
    text: str
    sender: str
    reply_to: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    idempotency_key: str = ""


def resend(msg: Outgoing) -> str:
    key = os.environ.get("RESEND_API_KEY", "")
    if not key:
        raise ProviderError("resend_not_configured", False)
    body = {"from": msg.sender, "to": [msg.to], "subject": msg.subject, "html": msg.html,
            "text": msg.text, "headers": msg.headers}
    if msg.reply_to:
        body["reply_to"] = msg.reply_to
    try:
        r = httpx.post("https://api.resend.com/emails", json=body, timeout=15.0,
                       headers={"Authorization": f"Bearer {key}", "Idempotency-Key": msg.idempotency_key[:256]})
    except httpx.HTTPError as exc:
        raise ProviderError("resend_unreachable:" + type(exc).__name__, True) from exc
    if r.status_code == 200:
        try:
            provider_id = r.json().get("id")
        except (ValueError, AttributeError):
            provider_id = None
        if not isinstance(provider_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", provider_id):
            # Acceptance may already have happened. Do not automatically dial
            # again or record a sent receipt with no provider identity.
            raise ProviderError("resend_response_unconfirmed", False)
        return provider_id
    # Codes only: provider bodies can echo the recipient address.
    raise ProviderError(f"resend_http_{r.status_code}", r.status_code in {408, 425, 429} or r.status_code >= 500)


def ses(msg: Outgoing) -> str:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError

    params = {
        "FromEmailAddress": msg.sender, "Destination": {"ToAddresses": [msg.to]},
        "Content": {"Simple": {
            "Subject": {"Data": msg.subject, "Charset": "UTF-8"},
            "Body": {"Html": {"Data": msg.html, "Charset": "UTF-8"}, "Text": {"Data": msg.text, "Charset": "UTF-8"}},
            "Headers": [{"Name": k, "Value": v} for k, v in msg.headers.items()]}},
    }
    if msg.reply_to:
        params["ReplyToAddresses"] = [msg.reply_to]
    if os.environ.get("SES_CONFIGURATION_SET"):
        params["ConfigurationSetName"] = os.environ["SES_CONFIGURATION_SET"]
    try:
        client = boto3.client("sesv2", region_name=os.environ.get("SES_REGION") or os.environ.get("AWS_REGION", "us-east-1"))
        return str(client.send_email(**params)["MessageId"])
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "unknown")
        raise ProviderError(("ses_" + code)[:80], code in {"Throttling", "TooManyRequestsException", "ServiceUnavailable", "InternalFailure"}) from exc
    except BotoCoreError as exc:
        raise ProviderError("ses_unreachable:" + type(exc).__name__, True) from exc


def log_only(msg: Outgoing) -> str:
    """Dev adapter: nothing leaves the process; the recipient is not logged."""
    log.info("email_log_only bytes=%d", len(msg.html))
    return "log_" + msg.idempotency_key[:24]


PROVIDERS = {"resend": resend, "ses": ses, "log": log_only}


def provider_name() -> str:
    return "log" if is_local() else os.environ.get("EMAIL_PROVIDER", "log").strip().lower()


def current_provider():
    try:
        return PROVIDERS[provider_name()]
    except KeyError:
        raise ProviderError("email_provider_unknown", False) from None

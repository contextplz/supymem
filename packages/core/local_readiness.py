"""Metadata-free local readiness used by Compose and the desktop supervisor."""

import hmac
import os
import re


def readiness_headers(nonce: str | None) -> dict[str, str] | None:
    headers = {"Cache-Control": "no-store"}
    key = os.environ.get("SUPYMEM_LOCAL_READINESS_KEY")
    if key is not None:
        if not nonce or not re.fullmatch(r"[0-9a-f]{32}", nonce):
            return None
        headers["X-Local-Ready-Proof"] = hmac.digest(key.encode(), nonce.encode(), "sha256").hex()
    return headers

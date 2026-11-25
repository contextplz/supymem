"""PKCE helpers for the desktop bridge (RFC 7636)."""

from __future__ import annotations

import base64
import hashlib
import secrets
from typing import Any
from urllib.parse import urlencode


def new_verifier() -> str:
    return secrets.token_urlsafe(64)[:128]


def challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def authorize_url(gateway_url: str, *, code_challenge: str, device_name: str = "Supymem Desktop") -> str:
    base = gateway_url.rstrip("/")
    qs = urlencode(
        {
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "device_name": device_name,
        }
    )
    return f"{base}/auth/device/approve?{qs}"


def token_body(verifier: str, code: str) -> dict[str, Any]:
    return {"code": code, "code_verifier": verifier}

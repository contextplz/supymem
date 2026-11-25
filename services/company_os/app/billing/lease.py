"""Ed25519-signed entitlement lease for the desktop process (doc 112 §3.2).

Private key comes from ENTITLEMENT_PRIVATE_KEY (32-byte hex or base64).
On AWS this is an Secrets Manager value injected as an env var — never baked
into the AMI. Public key is ENTITLEMENT_PUBLIC_KEY (or derived).

Local/dev without keys: a process-local ephemeral keypair is generated so the
loop still works. Those leases will not verify on another machine, which is
correct — you do not ship unsigned production leases.
"""

from __future__ import annotations

import base64
import json
import os
import threading
from datetime import timedelta

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from app.billing.entitlements import PlanView
from app.billing.plans import OFFLINE_GRACE_DAYS
from core.clock import as_utc, utcnow

_lock = threading.Lock()
_priv: Ed25519PrivateKey | None = None
_pub: Ed25519PublicKey | None = None
_ephemeral = False


def _decode_key(raw: str) -> bytes:
    text = raw.strip()
    if not text:
        raise ValueError("empty key")
    if all(c in "0123456789abcdefABCDEF" for c in text) and len(text) in {64, 128}:
        return bytes.fromhex(text)
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _load_keys() -> tuple[Ed25519PrivateKey, Ed25519PublicKey, bool]:
    global _priv, _pub, _ephemeral
    with _lock:
        if _priv is not None and _pub is not None:
            return _priv, _pub, _ephemeral
        raw_priv = os.environ.get("ENTITLEMENT_PRIVATE_KEY", "").strip()
        raw_pub = os.environ.get("ENTITLEMENT_PUBLIC_KEY", "").strip()
        if raw_priv:
            _priv = Ed25519PrivateKey.from_private_bytes(_decode_key(raw_priv)[:32])
            _pub = _priv.public_key()
            _ephemeral = False
        else:
            _priv = Ed25519PrivateKey.generate()
            _pub = _priv.public_key()
            _ephemeral = True
        # Public-only override is for verifiers (desktop). Never swap it in
        # when we just generated an ephemeral signer — the pair would not match.
        if raw_pub and not _ephemeral:
            _pub = Ed25519PublicKey.from_public_bytes(_decode_key(raw_pub)[:32])
        return _priv, _pub, _ephemeral


def public_key_b64() -> str:
    _, pub, _ = _load_keys()
    raw = pub.public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def private_key_b64() -> str:
    """Export the in-process private key. Used only by tests / local keygen."""
    priv, _, _ = _load_keys()
    raw = priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def is_ephemeral() -> bool:
    _load_keys()
    return _ephemeral


def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_lease(plan: PlanView, *, subject: str) -> dict[str, object]:
    priv, _, ephemeral = _load_keys()
    now = utcnow()
    expires = now + timedelta(days=OFFLINE_GRACE_DAYS)
    body = {
        "sub": subject,
        "plan": plan.tier,
        "status": plan.status,
        "features": list(plan.features),
        "seats_used": plan.seats_used,
        "seats_max": plan.seats,
        "workspace_id": plan.workspace_id,
        "issued_at": now.isoformat(),
        "expires_at": expires.isoformat(),
        "ephemeral": ephemeral,
    }
    signature = base64.urlsafe_b64encode(priv.sign(_canonical(body))).decode("ascii").rstrip("=")
    return {**body, "signature": signature}


def verify_lease(lease: dict[str, object], *, public_key_b64_override: str | None = None) -> bool:
    sig = lease.get("signature")
    if not isinstance(sig, str) or not sig:
        return False
    body = {k: v for k, v in lease.items() if k != "signature"}
    if public_key_b64_override:
        pub = Ed25519PublicKey.from_public_bytes(_decode_key(public_key_b64_override)[:32])
    else:
        _, pub, _ = _load_keys()
    pad = "=" * (-len(sig) % 4)
    try:
        pub.verify(base64.urlsafe_b64decode(sig + pad), _canonical(body))
    except Exception:
        return False
    exp = str(body.get("expires_at") or "")
    if exp:
        try:
            from datetime import datetime

            parsed = as_utc(datetime.fromisoformat(exp.replace("Z", "+00:00")))
            if parsed is not None and parsed < utcnow():
                return False
        except ValueError:
            return False
    return True

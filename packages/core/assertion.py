"""HMAC caller assertion shared by gateway (signer) and company_os (verifier).

Token format (doc 99 §6.2):

    X-COS-Assertion: base64url(payload_json) "." base64url(hmac_sha256(secret, payload_json))
    payload = {sub, email, role, path, method, nonce, iat, exp}

Path and method are bound into the MAC so a captured read-assertion cannot be
replayed at a write endpoint. Nonces are single-use in a bounded LRU.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from .canonical import canonical_json
from .clock import utcnow

MIN_SECRET_BYTES = 32
DEFAULT_TTL_SECONDS = 60
DEFAULT_NONCE_CAPACITY = 10_000

class AssertionFailure(Exception):
    """Typed assertion rejection. ``reason`` is a closed set of machine codes."""

    reason: str = "bad_format"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.reason)
        self.message = message or self.reason


class BadFormat(AssertionFailure):
    reason = "bad_format"


class BadSignature(AssertionFailure):
    reason = "bad_signature"


class Expired(AssertionFailure):
    reason = "expired"


class PathMismatch(AssertionFailure):
    reason = "path_mismatch"


class MethodMismatch(AssertionFailure):
    reason = "method_mismatch"


class Replayed(AssertionFailure):
    reason = "replayed"


class WeakSecret(AssertionFailure):
    reason = "weak_secret"


@dataclass(frozen=True)
class AssertionPayload:
    sub: str
    email: str
    role: str
    path: str
    method: str
    nonce: str
    iat: int
    exp: int
    device_id: str | None = None


class NonceStore(Protocol):
    def burn(self, nonce: str) -> bool:
        """Record ``nonce``. Return True if it was new; False if already seen."""
        ...


class LruNonceStore:
    """Bounded single-use nonce cache. Evicts oldest entries past ``capacity``."""

    def __init__(self, capacity: int = DEFAULT_NONCE_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self.capacity = capacity
        self._seen: OrderedDict[str, None] = OrderedDict()

    def burn(self, nonce: str) -> bool:
        if nonce in self._seen:
            return False
        self._seen[nonce] = None
        self._seen.move_to_end(nonce)
        while len(self._seen) > self.capacity:
            self._seen.popitem(last=False)
        return True

    def __len__(self) -> int:
        return len(self._seen)

    def clear(self) -> None:
        self._seen.clear()


_NONCE_STORE = LruNonceStore()


def default_nonce_store() -> LruNonceStore:
    return _NONCE_STORE


def reset_nonce_store() -> None:
    _NONCE_STORE.clear()


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    pad = "=" * ((4 - len(text) % 4) % 4)
    try:
        return base64.urlsafe_b64decode(text + pad)
    except Exception as exc:
        raise BadFormat("assertion is not valid base64url") from exc


def _require_secret(secret: str) -> bytes:
    if not isinstance(secret, str) or not secret:
        raise WeakSecret("assertion secret is missing")
    key = secret.encode("utf-8")
    if len(key) < MIN_SECRET_BYTES:
        raise WeakSecret("assertion secret must be at least 32 bytes")
    return key


def _norm_path(path: str) -> str:
    p = path.strip() or "/"
    if not p.startswith("/"):
        p = "/" + p
    return p


def _norm_method(method: str) -> str:
    return method.strip().upper()


def _payload_dict(
    *,
    sub: str,
    email: str,
    role: str,
    path: str,
    method: str,
    nonce: str,
    iat: int,
    exp: int,
    device_id: str | None = None,
) -> dict[str, Any]:
    payload = {
        "email": email,
        "exp": exp,
        "iat": iat,
        "method": method,
        "nonce": nonce,
        "path": path,
        "role": role,
        "sub": sub,
    }
    if device_id is not None:
        payload["device_id"] = _device_id(device_id)
    return payload


def sign_assertion(
    secret: str,
    *,
    sub: str,
    email: str,
    role: str,
    path: str,
    method: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    now: datetime | None = None,
    nonce: str | None = None,
    device_id: str | None = None,
) -> str:
    key = _require_secret(secret)
    ts = int((now or utcnow()).timestamp())
    payload = _payload_dict(
        sub=sub,
        email=email or "",
        role=role,
        path=_norm_path(path),
        method=_norm_method(method),
        nonce=nonce or secrets.token_urlsafe(16),
        iat=ts,
        exp=ts + int(ttl_seconds),
        device_id=device_id,
    )
    payload_json = canonical_json(payload)
    mac = hmac.new(key, payload_json.encode("utf-8"), hashlib.sha256).digest()
    return f"{_b64url_encode(payload_json.encode('utf-8'))}.{_b64url_encode(mac)}"


def _parse_token(token: str) -> tuple[bytes, bytes]:
    if not token or not isinstance(token, str) or token.count(".") != 1:
        raise BadFormat("assertion must be payload.mac")
    left, right = token.split(".", 1)
    if not left or not right:
        raise BadFormat("assertion must be payload.mac")
    return _b64url_decode(left), _b64url_decode(right)


def _device_id(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"dev_[A-Za-z0-9_-]{1,120}", value):
        raise BadFormat("invalid device identity")
    return value


def _parse_payload(payload_json: bytes) -> AssertionPayload:
    import json

    try:
        obj = json.loads(payload_json.decode("utf-8"))
    except Exception as exc:
        raise BadFormat("assertion payload is not JSON") from exc
    if not isinstance(obj, dict):
        raise BadFormat("assertion payload must be an object")
    try:
        return AssertionPayload(
            sub=str(obj["sub"]),
            email=str(obj.get("email") or ""),
            role=str(obj["role"]),
            path=str(obj["path"]),
            method=str(obj["method"]),
            nonce=str(obj["nonce"]),
            iat=int(obj["iat"]),
            exp=int(obj["exp"]),
            device_id=_device_id(obj["device_id"]) if "device_id" in obj else None,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise BadFormat("assertion payload is missing required fields") from exc


def verify_assertion(
    secret: str,
    token: str,
    *,
    path: str,
    method: str,
    now: datetime | None = None,
    nonce_store: NonceStore | None = None,
) -> AssertionPayload:
    key = _require_secret(secret)
    payload_json, mac = _parse_token(token)
    expected = hmac.new(key, payload_json, hashlib.sha256).digest()
    if len(mac) != len(expected) or not hmac.compare_digest(mac, expected):
        raise BadSignature("assertion signature mismatch")

    # Re-canonicalise so extra whitespace / key order cannot sneak through a
    # signature that was computed over a different encoding. The MAC already
    # covers the exact bytes; this check binds the parsed fields to those bytes.
    parsed = _parse_payload(payload_json)
    canonical = canonical_json(
        _payload_dict(
            sub=parsed.sub,
            email=parsed.email,
            role=parsed.role,
            path=parsed.path,
            method=parsed.method,
            nonce=parsed.nonce,
            iat=parsed.iat,
            exp=parsed.exp,
            device_id=parsed.device_id,
        )
    ).encode("utf-8")
    if not hmac.compare_digest(payload_json, canonical):
        raise BadFormat("assertion payload is not canonical JSON")

    ts = int((now or utcnow()).timestamp())
    if ts >= parsed.exp:
        raise Expired("assertion has expired")

    if _norm_path(parsed.path) != _norm_path(path):
        raise PathMismatch("assertion path does not match the request")
    if _norm_method(parsed.method) != _norm_method(method):
        raise MethodMismatch("assertion method does not match the request")

    store = nonce_store if nonce_store is not None else _NONCE_STORE
    if not store.burn(parsed.nonce):
        raise Replayed("assertion nonce has already been used")
    return parsed

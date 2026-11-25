"""HMAC caller assertion: every failure reason, tamper, expiry, replay."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest

from core.assertion import (
    MIN_SECRET_BYTES,
    AssertionFailure,
    BadFormat,
    BadSignature,
    Expired,
    LruNonceStore,
    MethodMismatch,
    PathMismatch,
    Replayed,
    WeakSecret,
    sign_assertion,
    verify_assertion,
)
from core.canonical import canonical_json

SECRET = "a" * MIN_SECRET_BYTES
NOW = datetime(2026, 8, 17, 12, 0, 0, tzinfo=timezone.utc)


def _sign(**kwargs):
    defaults = dict(
        secret=SECRET,
        sub="usr_alice",
        email="alice@example.com",
        role="user",
        path="/admin/session",
        method="GET",
        now=NOW,
    )
    defaults.update(kwargs)
    return sign_assertion(**defaults)


def _verify(token: str, **kwargs):
    defaults = dict(
        secret=SECRET,
        path="/admin/session",
        method="GET",
        now=NOW,
        nonce_store=LruNonceStore(),
    )
    defaults.update(kwargs)
    secret = defaults.pop("secret")
    return verify_assertion(secret, token, **defaults)


def _split(token: str) -> tuple[dict, bytes]:
    left, right = token.split(".", 1)
    pad = "=" * ((4 - len(left) % 4) % 4)
    payload = json.loads(base64.urlsafe_b64decode(left + pad))
    pad_r = "=" * ((4 - len(right) % 4) % 4)
    mac = base64.urlsafe_b64decode(right + pad_r)
    return payload, mac


def test_roundtrip() -> None:
    token = _sign()
    parsed = _verify(token)
    assert parsed.sub == "usr_alice"
    assert parsed.email == "alice@example.com"
    assert parsed.role == "user"
    assert parsed.path == "/admin/session"
    assert parsed.method == "GET"
    assert parsed.exp == parsed.iat + 60


def test_rejects_short_secret_at_sign() -> None:
    with pytest.raises(WeakSecret):
        sign_assertion("too-short", sub="s", email="", role="user", path="/", method="GET")


def test_rejects_short_secret_at_verify() -> None:
    token = _sign()
    with pytest.raises(WeakSecret):
        _verify(token, secret="still-too-short-to-be-valid")


@pytest.mark.parametrize(
    "token",
    ["", "noperiod", "a.", ".b", "a.b.c"],
)
def test_bad_format(token: str) -> None:
    with pytest.raises(BadFormat):
        _verify(token)


def test_non_json_payload_is_bad_signature_or_format() -> None:
    """MAC is checked before JSON, so garbage bytes surface as bad_signature."""
    token = "YQ.YQ"  # payload "a", mac "a"
    with pytest.raises((BadSignature, BadFormat)):
        _verify(token)
    token = _sign()
    left, right = token.split(".", 1)
    flipped = bytes(b ^ 0xFF for b in base64.urlsafe_b64decode(right + "==")).hex()
    # Corrupt the mac bytes and re-encode.
    pad = "=" * ((4 - len(right) % 4) % 4)
    mac = bytearray(base64.urlsafe_b64decode(right + pad))
    mac[0] ^= 0x01
    bad = left + "." + base64.urlsafe_b64encode(bytes(mac)).rstrip(b"=").decode("ascii")
    with pytest.raises(BadSignature):
        _verify(bad)
    del flipped


@pytest.mark.parametrize("field", ["sub", "email", "role", "path", "method", "nonce", "iat", "exp"])
def test_tamper_each_field_fails_signature(field: str) -> None:
    token = _sign()
    payload, _mac = _split(token)
    if field in {"iat", "exp"}:
        payload[field] = int(payload[field]) + 1
    else:
        payload[field] = str(payload[field]) + "x"
    body = canonical_json(payload).encode("utf-8")
    _, right = token.split(".", 1)
    tampered = (
        base64.urlsafe_b64encode(body).rstrip(b"=").decode("ascii")
        + "."
        + right
    )
    with pytest.raises(BadSignature):
        _verify(tampered)


def test_expired() -> None:
    token = _sign(ttl_seconds=1)
    later = NOW + timedelta(seconds=2)
    with pytest.raises(Expired):
        _verify(token, now=later)


def test_path_mismatch() -> None:
    token = _sign(path="/admin/session", method="GET")
    with pytest.raises(PathMismatch):
        _verify(token, path="/admin/credits/grant")


def test_method_mismatch() -> None:
    token = _sign(path="/admin/session", method="GET")
    with pytest.raises(MethodMismatch):
        _verify(token, method="POST")


def test_replayed() -> None:
    store = LruNonceStore()
    token = _sign()
    _verify(token, nonce_store=store)
    with pytest.raises(Replayed):
        _verify(token, nonce_store=store)


def test_fresh_nonce_store_accepts_same_token() -> None:
    token = _sign()
    _verify(token, nonce_store=LruNonceStore())
    _verify(token, nonce_store=LruNonceStore())


def test_lru_evicts_oldest() -> None:
    store = LruNonceStore(capacity=2)
    t1 = _sign(nonce="n1")
    t2 = _sign(nonce="n2")
    t3 = _sign(nonce="n3")
    _verify(t1, nonce_store=store)
    _verify(t2, nonce_store=store)
    _verify(t3, nonce_store=store)  # evicts n1
    # n1 is gone, so replaying t1 is allowed by the cache — that's the bound.
    _verify(t1, nonce_store=store)
    with pytest.raises(Replayed):
        _verify(t3, nonce_store=store)


def test_every_failure_reason_is_distinct() -> None:
    reasons = {
        BadFormat.reason,
        BadSignature.reason,
        Expired.reason,
        PathMismatch.reason,
        MethodMismatch.reason,
        Replayed.reason,
    }
    assert reasons == {
        "bad_format",
        "bad_signature",
        "expired",
        "path_mismatch",
        "method_mismatch",
        "replayed",
    }
    assert issubclass(BadFormat, AssertionFailure)


def test_canonical_json_is_used() -> None:
    token = _sign()
    payload, _ = _split(token)
    assert canonical_json(payload) == json.dumps(payload, sort_keys=True, separators=(",", ":"))


def test_device_identity_is_optional_and_signed():
    assert _verify(_sign()).device_id is None
    token = _sign(device_id="dev_verified_mac")
    assert _verify(token).device_id == "dev_verified_mac"
    payload, _ = _split(token)
    payload["device_id"] = "dev_other_mac"
    altered = base64.urlsafe_b64encode(canonical_json(payload).encode()).decode().rstrip("=")
    with pytest.raises(BadSignature):
        _verify(altered + "." + token.split(".")[1])


@pytest.mark.parametrize("device", ["", "device", "dev_a\n", 42, [], "dev_" + "a" * 121])
def test_invalid_device_identity_rejected_at_signing(device):
    with pytest.raises(BadFormat):
        _sign(device_id=device)

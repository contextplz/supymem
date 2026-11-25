"""Signed credit lease for local hosted-LLM halt (no website round-trip).

The server issues an Ed25519-signed snapshot of remaining credits and plan
expiry. The packaged desktop verifies that file on disk before any HTTP hop
to Azure. A remaining balance of 0, an expired plan, a stale lease, or a
bad signature all deny Company OS default locally. BYOK never consults this
file.

The public key is not a secret (it ships with the app). The private key
never leaves the server / ECS task.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from llm_gateway.hosted import (
    HostedAuthRequired,
    HostedCreditsRequired,
    HostedPlanExpired,
)

LEASE_VERSION = 1
LEASE_FILENAME = "credit_lease.json"
DEFAULT_LEASE_TTL = timedelta(hours=24)
TRIAL_DAYS = 14


class LeaseError(Exception):
    """Local lease failed verification or is not usable for hosted default."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: str) -> datetime:
    raw = (value or "").strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _b64url(data: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    import base64

    padded = text + "=" * ((4 - len(text) % 4) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def generate_keypair() -> tuple[str, str]:
    """Return ``(private_hex, public_hex)`` Ed25519 raw keys."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        PublicFormat,
    )

    key = Ed25519PrivateKey.generate()
    priv = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()).hex()
    pub = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    return priv, pub


def _private_key_from_hex(hex_key: str):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    raw = bytes.fromhex((hex_key or "").strip())
    if len(raw) != 32:
        raise LeaseError("invalid_key", "CREDIT_LEASE_PRIVATE_KEY must be 32 raw bytes hex")
    return Ed25519PrivateKey.from_private_bytes(raw)


def _public_key_from_hex(hex_key: str):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    raw = bytes.fromhex((hex_key or "").strip())
    if len(raw) != 32:
        raise LeaseError("invalid_key", "CREDIT_LEASE_PUBLIC_KEY must be 32 raw bytes hex")
    return Ed25519PublicKey.from_public_bytes(raw)


def private_key_hex() -> str:
    return (os.environ.get("CREDIT_LEASE_PRIVATE_KEY") or "").strip()


def _public_key_files() -> list[Path]:
    import sys

    paths: list[Path] = []
    override = (os.environ.get("CREDIT_LEASE_PUBLIC_KEY_FILE") or "").strip()
    if override:
        paths.append(Path(override).expanduser())
    repo = (os.environ.get("COMPANY_OS_REPO_ROOT") or "").strip()
    if repo:
        root = Path(repo)
        paths.append(root / "config" / "credit_lease.public.env")
        paths.append(root / "src-tauri" / "resources" / "credit_lease.public.env")
    here = Path(__file__).resolve()
    if len(here.parents) >= 2:
        paths.append(here.parents[2] / "config" / "credit_lease.public.env")
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        paths.append(Path(meipass) / "credit_lease.public.env")
    data = (os.environ.get("COMPANY_OS_DATA_DIR") or "").strip()
    if data:
        paths.append(Path(data).expanduser() / "credit_lease.public.env")
    return paths


def public_key_hex() -> str:
    env = (os.environ.get("CREDIT_LEASE_PUBLIC_KEY") or "").strip()
    if env:
        return env
    for pinned in _public_key_files():
        if not pinned.is_file():
            continue
        try:
            text = pinned.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            if line.startswith("CREDIT_LEASE_PUBLIC_KEY="):
                value = line.split("=", 1)[1].strip()
                if value:
                    return value
    return ""


def _canonical_payload(fields: Mapping[str, Any]) -> bytes:
    body = {k: fields[k] for k in sorted(fields) if k != "signature"}
    return json.dumps(body, separators=(",", ":"), sort_keys=True, ensure_ascii=True).encode(
        "utf-8"
    )


def sign_fields(fields: Mapping[str, Any], *, private_hex: str | None = None) -> str:
    key_hex = (private_hex or private_key_hex()).strip()
    if not key_hex:
        env = (os.environ.get("APP_ENV") or "local").strip().lower()
        if env in {"", "local"}:
            priv, pub = generate_keypair()
            os.environ["CREDIT_LEASE_PRIVATE_KEY"] = priv
            if not public_key_hex():
                os.environ["CREDIT_LEASE_PUBLIC_KEY"] = pub
            key_hex = priv
        else:
            raise LeaseError("missing_private_key", "CREDIT_LEASE_PRIVATE_KEY is not set")
    signature = _private_key_from_hex(key_hex).sign(_canonical_payload(fields))
    return _b64url(signature)


def verify_signature(fields: Mapping[str, Any], *, public_hex: str | None = None) -> None:
    key_hex = (public_hex or public_key_hex()).strip()
    if not key_hex:
        raise LeaseError("missing_public_key", "CREDIT_LEASE_PUBLIC_KEY is not set")
    sig = fields.get("signature")
    if not isinstance(sig, str) or not sig.strip():
        raise LeaseError("invalid_signature", "lease is missing a signature")
    try:
        _public_key_from_hex(key_hex).verify(_b64url_decode(sig.strip()), _canonical_payload(fields))
    except Exception as exc:
        raise LeaseError("invalid_signature", "lease signature is not valid") from exc


@dataclass(frozen=True)
class CreditLease:
    user_id: str
    email: str
    cognito_sub: str
    remaining_micro: int
    remaining_credits: str
    plan: str
    plan_expires_at: datetime
    lease_expires_at: datetime
    issued_at: datetime
    nonce: str
    signature: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "v": LEASE_VERSION,
            "user_id": self.user_id,
            "email": self.email,
            "cognito_sub": self.cognito_sub,
            "remaining_micro": self.remaining_micro,
            "remaining_credits": self.remaining_credits,
            "plan": self.plan,
            "plan_expires_at": _iso(self.plan_expires_at),
            "lease_expires_at": _iso(self.lease_expires_at),
            "issued_at": _iso(self.issued_at),
            "nonce": self.nonce,
            "signature": self.signature,
        }


def issue_lease(
    *,
    user_id: str,
    email: str,
    cognito_sub: str,
    remaining_micro: int,
    remaining_credits: str,
    plan: str,
    plan_expires_at: datetime,
    lease_ttl: timedelta | None = None,
    now: datetime | None = None,
    nonce: str | None = None,
    private_hex: str | None = None,
) -> CreditLease:
    import secrets

    issued = now or _now()
    ttl = lease_ttl or DEFAULT_LEASE_TTL
    payload: dict[str, Any] = {
        "v": LEASE_VERSION,
        "user_id": user_id,
        "email": email,
        "cognito_sub": cognito_sub,
        "remaining_micro": int(remaining_micro),
        "remaining_credits": remaining_credits,
        "plan": plan,
        "plan_expires_at": _iso(plan_expires_at),
        "lease_expires_at": _iso(issued + ttl),
        "issued_at": _iso(issued),
        "nonce": nonce or secrets.token_urlsafe(16),
    }
    payload["signature"] = sign_fields(payload, private_hex=private_hex)
    return lease_from_dict(payload)


def lease_from_dict(raw: Mapping[str, Any]) -> CreditLease:
    try:
        remaining = raw["remaining_micro"]
        if type(remaining) is not int:
            raise LeaseError("invalid_lease", "remaining_micro must be int")
        return CreditLease(
            user_id=str(raw["user_id"]),
            email=str(raw["email"]),
            cognito_sub=str(raw["cognito_sub"]),
            remaining_micro=remaining,
            remaining_credits=str(raw["remaining_credits"]),
            plan=str(raw["plan"]),
            plan_expires_at=_parse_dt(str(raw["plan_expires_at"])),
            lease_expires_at=_parse_dt(str(raw["lease_expires_at"])),
            issued_at=_parse_dt(str(raw["issued_at"])),
            nonce=str(raw["nonce"]),
            signature=str(raw["signature"]),
        )
    except LeaseError:
        raise
    except Exception as exc:
        raise LeaseError("invalid_lease", "lease payload is not usable") from exc


def lease_path() -> Path:
    override = (os.environ.get("COMPANY_OS_CREDIT_LEASE_PATH") or "").strip()
    if override:
        return Path(override).expanduser()
    data = (os.environ.get("COMPANY_OS_DATA_DIR") or "").strip()
    if data:
        return Path(data).expanduser() / LEASE_FILENAME
    return Path.home() / ".companyos" / LEASE_FILENAME


def write_lease(lease: CreditLease, path: Path | None = None) -> Path:
    dest = path or lease_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(lease.as_dict(), indent=2) + "\n", encoding="utf-8")
    return dest


def read_lease_file(path: Path | None = None) -> dict[str, Any] | None:
    dest = path or lease_path()
    if not dest.is_file():
        return None
    try:
        raw = json.loads(dest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def load_verified_lease(
    path: Path | None = None,
    *,
    public_hex: str | None = None,
    now: datetime | None = None,
) -> CreditLease:
    raw = read_lease_file(path)
    if raw is None:
        raise LeaseError("missing_lease", "no credit lease on disk")
    verify_signature(raw, public_hex=public_hex)
    lease = lease_from_dict(raw)
    moment = now or _now()
    if lease.remaining_micro <= 0:
        raise HostedCreditsRequired(
            "Not enough credits for Company OS default. Top up on the website."
        )
    if lease.plan_expires_at <= moment:
        raise HostedPlanExpired()
    if lease.lease_expires_at <= moment:
        raise HostedAuthRequired(
            "Credit lease expired. Open Company OS from the website to refresh."
        )
    return lease


def enforce_local_hosted_lease(
    *,
    byok: bool = False,
    packaged: bool | None = None,
    now: datetime | None = None,
) -> CreditLease | None:
    """Deny hosted default locally. Never performs HTTP.

    Returns the verified lease when hosted default is allowed. Returns None
    when this process is a founder laptop with no lease file (env ``LLM_*``).
    """
    if byok:
        return None
    from llm_gateway.hosted import is_packaged_desktop

    is_packaged = is_packaged_desktop() if packaged is None else packaged
    raw = read_lease_file()
    if raw is None:
        if is_packaged:
            raise HostedAuthRequired(
                "Open Company OS from the website to use Company OS default, or set a key in Settings."
            )
        return None
    try:
        return load_verified_lease(now=now)
    except (HostedCreditsRequired, HostedPlanExpired, HostedAuthRequired):
        raise
    except LeaseError as exc:
        raise HostedAuthRequired(
            "Credit lease is not valid. Open Company OS from the website to refresh."
        ) from exc


__all__ = [
    "DEFAULT_LEASE_TTL",
    "LEASE_FILENAME",
    "LEASE_VERSION",
    "TRIAL_DAYS",
    "CreditLease",
    "LeaseError",
    "enforce_local_hosted_lease",
    "generate_keypair",
    "issue_lease",
    "lease_from_dict",
    "lease_path",
    "load_verified_lease",
    "private_key_hex",
    "public_key_hex",
    "read_lease_file",
    "sign_fields",
    "verify_signature",
    "write_lease",
]

"""Connector credentials live in the vault; the database holds a ref (43 §2).

``connections.secret_ref`` is the only thing that is ever persisted, logged or
narrated. In local tenancy — the default and forever-supported mode — the value
stays in this process and is dropped on revoke; hosted modes swap the store, not
the call sites.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Protocol

from core.ids import new_id
from core.clock import utcnow


def _workspace_ref(meta: dict[str, dict[str, str]], workspace_id: str, provider: str, key: str) -> str | None:
    """Select an exact credential binding; never guess among legacy rotations."""
    if not workspace_id:
        return None
    matches = {ref: m for ref, m in meta.items()
               if m.get("workspace_id") == workspace_id and m.get("provider") == provider and m.get("key") == key}
    if not matches:
        return None
    latest = max(m.get("created_at", "") for m in matches.values())
    refs = [ref for ref, m in matches.items() if m.get("created_at", "") == latest]
    return refs[0] if len(refs) == 1 else None


class SecretStore(Protocol):
    def put(self, *, workspace_id: str, provider: str, key: str, value: str) -> str: ...

    def get(self, ref: str, *, requester: str) -> str: ...

    def forget(self, ref: str) -> None: ...

    def find(self, *, workspace_id: str, provider: str, key: str, requester: str) -> str | None: ...


class LocalSecretStore:
    """In-memory vault (tests / ephemeral). Prefer FileSecretStore in production."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}
        self._meta: dict[str, dict[str, str]] = {}

    def put(self, *, workspace_id: str, provider: str, key: str, value: str) -> str:
        ref = f"vault:{new_id('vlt')}"
        from core.logging import register_local_secret

        register_local_secret(value)
        self._values[ref] = value
        self._meta[ref] = {
            "workspace_id": workspace_id,
            "provider": provider,
            "key": key,
            "created_at": utcnow().isoformat(),
        }
        return ref

    def get(self, ref: str, *, requester: str) -> str:
        try:
            return self._values[ref]
        except KeyError as exc:
            raise KeyError(f"no secret for ref {ref}") from exc

    def forget(self, ref: str) -> None:
        self._values.pop(ref, None)
        self._meta.pop(ref, None)

    def find(self, *, workspace_id: str, provider: str, key: str, requester: str) -> str | None:
        ref = _workspace_ref(self._meta, workspace_id, provider, key)
        return self._values.get(ref) if ref else None

    def refs(self, workspace_id: str | None = None) -> list[str]:
        if workspace_id is None:
            return sorted(self._meta)
        return sorted(
            ref
            for ref, meta in self._meta.items()
            if meta["workspace_id"] == workspace_id
        )

    def values(self, workspace_id: str | None = None) -> list[str]:
        if workspace_id is None:
            return [v for v in self._values.values() if v]
        return [
            self._values[ref]
            for ref, meta in self._meta.items()
            if meta["workspace_id"] == workspace_id and ref in self._values
        ]


LOCAL_VAULT_SECRET = "company-os-local-vault"


def vault_secret() -> str:
    configured = (
        os.environ.get("COMPANY_OS_VAULT_SECRET")
        or os.environ.get("SESSION_SECRET")
        or os.environ.get("COMPANY_OS_SECRET")
    )
    if configured:
        return configured
    from app.core.local_mode import is_local

    if not is_local():
        return LOCAL_VAULT_SECRET
    from app.config import data_root
    from app.core.local_process import file_lock, open_private

    root = data_root() / "vault"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    from app.core.local_windows import credential_key

    with os.fdopen(open_private(root / "master.lock", os.O_RDWR | os.O_CREAT), "rb") as lock, file_lock(lock):
        value = credential_key(root / "master.key", create=not (root / "secrets.bin").exists()).hex()
        from core.logging import register_local_secret

        register_local_secret(value)
        return value


def _vault_secret() -> str:
    return vault_secret()


def _derive_vault_key() -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    kdf = Scrypt(salt=b"company-os-founder-vault-v1", length=32, n=2**14, r=8, p=1)
    return kdf.derive(_vault_secret().encode("utf-8"))


def encrypt_at_rest(plaintext: str) -> str:
    """AES-GCM blob (base64 of nonce || ciphertext). Same key as FileSecretStore."""
    import base64

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    token = nonce + AESGCM(_derive_vault_key()).encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(token).decode("ascii")


def decrypt_at_rest(token: str) -> str:
    import base64

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    raw = base64.b64decode(token.encode("ascii"))
    nonce, ciphertext = raw[:12], raw[12:]
    return AESGCM(_derive_vault_key()).decrypt(nonce, ciphertext, None).decode("utf-8")


class FileSecretStore:
    """Durable encrypted vault under data/vault/secrets.bin (AES-GCM).

    The database only stores a vault:… reference. Raw keys never go in SQLite
    or API responses. Legacy plaintext secrets.json is imported once, then removed.
    """

    def __init__(self, path: Path | None = None) -> None:
        from app.config import data_root

        root = data_root() / "vault"
        root.mkdir(parents=True, exist_ok=True)
        self.path = path or (root / "secrets.bin")
        self._legacy = root / "secrets.json"
        self._lock = threading.Lock()
        if not self.path.exists():
            if self._legacy.exists() and path is None:
                try:
                    legacy = self._document(self._legacy.read_bytes())
                except Exception:
                    raise RuntimeError("vault data is unreadable; refusing to replace it") from None
                self._write(legacy)
                # Verify the encrypted replacement before the existing migration cleanup.
                if self._read() != legacy:
                    raise RuntimeError("vault data is unreadable; refusing to replace it")
                try:
                    self._legacy.unlink()
                except OSError:
                    pass
            else:
                self._write({"values": {}, "meta": {}})
        else:
            self._read()

    def _fernet_key(self) -> bytes:
        return _derive_vault_key()

    @staticmethod
    def _document(raw: bytes) -> dict[str, Any]:
        data = json.loads(raw.decode("utf-8"))
        if (not isinstance(data, dict) or not isinstance(data.get("values"), dict)
                or not isinstance(data.get("meta"), dict)
                or any(not isinstance(ref, str) or not isinstance(value, str) for ref, value in data["values"].items())
                or any(not isinstance(ref, str) or not isinstance(meta, dict) for ref, meta in data["meta"].items())):
            raise ValueError("invalid vault document")
        from core.logging import register_local_secret

        for value in data["values"].values():
            register_local_secret(value)
        return data

    def _read(self) -> dict[str, Any]:
        try:
            if os.name == "nt":
                from app.core.local_windows import private_open

                with os.fdopen(private_open(self.path, os.O_RDONLY), "rb") as stream:
                    raw = stream.read()
            else:
                raw = self.path.read_bytes()
        except OSError:
            raise RuntimeError("vault data is unreadable; refusing to replace it") from None
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM

            aes = AESGCM(self._fernet_key())
            pt = aes.decrypt(raw[:12], raw[12:], None)
        except Exception:
            # A random ciphertext nonce can begin with '{'. Authenticate first;
            # only a complete, recognized legacy document may use the old format.
            try:
                return self._document(raw)
            except Exception:
                raise RuntimeError("vault data is unreadable; refusing to replace it") from None
        try:
            return self._document(pt)
        except Exception:
            raise RuntimeError("vault data is unreadable; refusing to replace it") from None

    def _write(self, data: dict[str, Any]) -> None:
        from core.logging import register_local_secret

        for value in data["values"].values():
            register_local_secret(value)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        blob = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM

            aes = AESGCM(self._fernet_key())
            nonce = os.urandom(12)
            payload = nonce + aes.encrypt(nonce, blob, None)
        except Exception as exc:
            raise RuntimeError("vault cannot encrypt; refusing to write plaintext") from exc
        if os.name == "nt":
            from app.core.local_windows import atomic_write

            atomic_write(self.path, payload)
            return
        tmp = self.path.with_suffix(".tmp")
        tmp.write_bytes(payload)
        tmp.replace(self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def put(self, *, workspace_id: str, provider: str, key: str, value: str) -> str:
        ref = f"vault:{new_id('vlt')}"
        with self._lock:
            data = self._read()
            data.setdefault("values", {})[ref] = value
            data.setdefault("meta", {})[ref] = {
                "workspace_id": workspace_id,
                "provider": provider,
                "key": key,
                "created_at": utcnow().isoformat(),
            }
            self._write(data)
        return ref

    def get(self, ref: str, *, requester: str) -> str:
        with self._lock:
            data = self._read()
            try:
                return data["values"][ref]
            except KeyError as exc:
                raise KeyError(f"no secret for ref {ref}") from exc

    def forget(self, ref: str) -> None:
        with self._lock:
            data = self._read()
            data.get("values", {}).pop(ref, None)
            data.get("meta", {}).pop(ref, None)
            self._write(data)

    def find(self, *, workspace_id: str, provider: str, key: str, requester: str) -> str | None:
        with self._lock:
            data = self._read()
            ref = _workspace_ref(data.get("meta") or {}, workspace_id, provider, key)
            return (data.get("values") or {}).get(ref) if ref else None

    def refs(self, workspace_id: str | None = None) -> list[str]:
        with self._lock:
            meta = self._read().get("meta") or {}
        if workspace_id is None:
            return sorted(meta)
        return sorted(
            ref for ref, m in meta.items() if m.get("workspace_id") == workspace_id
        )

    def values(self, workspace_id: str | None = None) -> list[str]:
        """Plaintext values for the boundary vault detector only."""
        with self._lock:
            data = self._read()
            vals = data.get("values") or {}
            meta = data.get("meta") or {}
        if workspace_id is None:
            return [str(v) for v in vals.values() if v]
        return [
            str(vals[ref])
            for ref, m in meta.items()
            if ref in vals and m.get("workspace_id") == workspace_id and vals[ref]
        ]


class GatewayVaultStore:
    """Hosted tenancy: the gateway owns the encrypted vault (43 §2)."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def put(self, *, workspace_id: str, provider: str, key: str, value: str) -> str:
        import httpx

        resp = httpx.post(
            f"{self.base_url}/vault",
            json={
                "venture_id": workspace_id,
                "key": key,
                "value": value,
                "provider": provider,
            },
            timeout=5.0,
        )
        resp.raise_for_status()
        return f"vault:{resp.json()['handle']}"

    def get(self, ref: str, *, requester: str) -> str:
        # The gateway never returns a stored value to a client; a provider call
        # is proxied through it instead. Reaching here means a caller wants the
        # raw credential in this process, which hosted mode does not allow.
        raise PermissionError("hosted vault does not release secret values")

    def find(self, *, workspace_id: str, provider: str, key: str, requester: str) -> str | None:
        raise PermissionError("hosted vault does not release secret values")

    def forget(self, ref: str) -> None:
        import httpx

        handle = ref.removeprefix("vault:")
        httpx.delete(f"{self.base_url}/vault/{handle}", timeout=5.0)


_STORE: SecretStore | None = None


def store() -> SecretStore:
    global _STORE
    if _STORE is None:
        url = os.environ.get("CONNECTOR_VAULT_URL")
        if url:
            _STORE = GatewayVaultStore(url)
        elif os.environ.get("COMPANY_OS_EPHEMERAL_VAULT", "").strip() in {
            "1",
            "true",
            "yes",
        }:
            _STORE = LocalSecretStore()
        else:
            _STORE = FileSecretStore()
    return _STORE


def reset_store() -> None:
    global _STORE
    _STORE = None

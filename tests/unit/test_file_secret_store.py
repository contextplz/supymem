"""Durable FileSecretStore survives process reset via the same data dir."""

from __future__ import annotations

import sys
import types

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_windows_credential_keys_persist_isolate_and_fail_closed(cos, monkeypatch, domain):
    import ctypes
    import os

    from app.core import local_windows

    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    entries = {}
    writes = []
    allocations = {}
    errors = [0]
    original_api = local_windows._api
    original_write = local_windows._write_credential

    if os.name != "nt":
        monkeypatch.setattr(ctypes, "get_last_error", lambda: errors[0], raising=False)
        monkeypatch.setattr(local_windows, "private_open", lambda path, flags: os.open(path, flags | os.O_NOFOLLOW, 0o600))

    def atomic_write(path, payload):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_suffix(".pending")
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    if os.name != "nt":
        monkeypatch.setattr(local_windows, "atomic_write", atomic_write)

    def read(target, kind, flags, output):
        assert kind == 1 and flags == 0
        if target not in entries:
            errors[0] = 1168
            if os.name == "nt":
                ctypes.set_last_error(errors[0])
            return 0
        blob = ctypes.create_string_buffer(entries[target])
        record = local_windows._Credential(size=len(entries[target]), blob=ctypes.cast(blob, local_windows.POINTER))
        allocations[ctypes.addressof(record)] = (record, blob)
        ctypes.cast(output, ctypes.POINTER(ctypes.POINTER(local_windows._Credential)))[0] = ctypes.pointer(record)
        return 1

    def write(pointer, flags):
        record = ctypes.cast(pointer, ctypes.POINTER(local_windows._Credential)).contents
        assert flags == 0 and record.type == 1 and record.persist == 2
        writes.append(record.target)
        entries[record.target] = ctypes.string_at(record.blob, record.size)
        return 1

    functions = {"CredReadW": read, "CredWriteW": write,
                 "CredFree": lambda pointer: allocations.pop(ctypes.addressof(pointer.contents))}
    monkeypatch.setattr(local_windows, "_api", lambda library, name, arguments, result=local_windows.DWORD:
                        functions[name] if name in functions else original_api(library, name, arguments, result))
    path = cos / domain / "local-key.bin"
    with pytest.raises(ValueError, match="backup"):
        local_windows.credential_key(path)
    key = local_windows.credential_key(path, create=True)
    assert len(key) == 32
    assert local_windows.credential_key(path, create=True) == key
    assert len(writes) == 1 and path.is_file()
    assert len(path.read_bytes()) == 64
    assert writes[0].startswith("com.supymem.local/key/")
    other_path = cos / (domain + "-other") / path.name
    other = local_windows.credential_key(other_path, create=True)
    assert other != key
    from pathlib import Path

    original_resolve = Path.resolve
    with monkeypatch.context() as aliases:
        aliases.setattr(Path, "resolve", lambda candidate, *args, **kwargs:
                        other_path if candidate == path else original_resolve(candidate, *args, **kwargs))
        matches = local_windows.credential_key(path) == key
        assert matches, "Your saved key must not change when a file alias is added."
    entries[writes[0]] = b"corrupt"
    with pytest.raises(ValueError, match="backup"):
        local_windows.credential_key(path, create=True)
    assert len(writes) == 2 and not allocations
    entries.clear()
    monkeypatch.setattr(local_windows, "_write_credential", lambda target, value: None)
    with pytest.raises(ValueError, match="securely"):
        local_windows.credential_key(path, create=True)
    from types import SimpleNamespace

    from app.connectors import secrets
    from app.core import local_process, local_secrets, providers_local

    cos = cos / "windows" / domain
    cos.mkdir(parents=True, mode=0o700)
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(cos))
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(local_windows, "_write_credential", original_write)
    windows_os = SimpleNamespace(**{**vars(os), "name": "nt"})
    for module in (secrets, local_process, local_secrets, providers_local):
        monkeypatch.setattr(module, "os", windows_os)
    if os.name != "nt":
        import fcntl

        monkeypatch.setattr(local_windows, "private_open", lambda path, flags: os.open(path, flags | os.O_NOFOLLOW, 0o600))

    def lock(stream, *, exclusive=True, blocking=True, unlock=False):
        flags = fcntl.LOCK_UN if unlock else (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | (0 if blocking else fcntl.LOCK_NB)
        fcntl.flock(stream, flags)

    if os.name != "nt":
        monkeypatch.setattr(local_windows, "lock_file", lock)
        monkeypatch.setattr(local_windows, "atomic_write", atomic_write)
    with local_process.process_lock(), local_process.process_lock():
        with pytest.raises(local_process.LocalProcessBusy):
            with local_process.process_lock(exclusive=True):
                pytest.fail("Maintenance must not run while your app owns the data.")
    with pytest.raises(BlockingIOError, match="body"):
        with local_process.process_lock():
            raise BlockingIOError("body")
    local_secrets.update_settings("smtp", {}, {"password": domain})
    assert local_secrets.get_secret("smtp", "password") == domain
    provider_root = cos / "providers"
    provider_root.mkdir()
    import base64
    monkeypatch.setenv("SUPYMEM_LOCAL_KEY_PIPE", "1")
    monkeypatch.setattr(local_secrets, "_material_started", True)
    monkeypatch.setattr(local_secrets, "_material", {"key": None, "legacy": {"machine.secret": base64.b64encode(os.urandom(32)).decode()}, "storage": "keyfile"})
    cipher = providers_local._cipher(provider_root, create=True)
    assert providers_local._cipher(provider_root).decrypt(cipher.encrypt(domain.encode())) == domain.encode()
    credentials = {}

    def keychain(root, account, action="get", password=None):
        if action == "set":
            credentials[account] = password
        elif action == "delete":
            credentials.pop(account, None)
        else:
            return credentials.get(account)

    monkeypatch.setattr(providers_local, "_root", lambda: provider_root)
    monkeypatch.delenv("SUPYMEM_DESKTOP_INSTANCE_ID", raising=False)
    secret = domain + "-provider-credential"
    providers_local.save_settings(provider="openai_compatible", base_url=f"https://{domain}/v1",
                                  api_key=secret, models={"chat": domain})
    assert (cos / "local-settings.json").is_file() and not credentials
    monkeypatch.setenv("SUPYMEM_DESKTOP_INSTANCE_ID", domain)
    assert providers_local.load_local_config().api_key.get_secret_value() == secret
    assert not credentials
    assert not (provider_root / "settings.enc").exists()
    assert secret.encode() not in (cos / "local-settings.json").read_bytes()
    vault = secrets.FileSecretStore()
    ref = vault.put(workspace_id=domain, provider="synthetic", key="credential", value=domain)
    assert secrets.FileSecretStore().get(ref, requester="test") == domain
    vault_master = cos / "vault" / "master.key"
    assert len(vault_master.read_bytes()) in {60, 64}
    assert all(value not in vault_master.read_bytes() for value in entries.values())
    marker = cos / "local-key.keychain"
    assert marker.is_file() and marker.stat().st_mode & 0o077 == 0
    assert not (cos / "local-key.bin").exists()
    assert not any(cos.rglob("machine.secret"))
    assert domain.encode() not in vault.path.read_bytes()


@pytest.fixture()
def cos(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/company_os.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CONNECTOR_VAULT_URL", raising=False)
    for env_key in ("COMPANY_OS_VAULT_SECRET", "SESSION_SECRET", "COMPANY_OS_SECRET"):
        monkeypatch.delenv(env_key, raising=False)
    if "argon2" not in sys.modules:
        argon2 = types.ModuleType("argon2")
        exceptions = types.ModuleType("argon2.exceptions")

        class PasswordHasher:
            pass

        class VerifyMismatchError(Exception):
            pass

        argon2.PasswordHasher = PasswordHasher  # type: ignore[attr-defined]
        exceptions.VerifyMismatchError = VerifyMismatchError  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "argon2", argon2)
        monkeypatch.setitem(sys.modules, "argon2.exceptions", exceptions)
    load_company_os_app()
    from app.connectors.secrets import reset_store

    reset_store()
    return tmp_path


def test_file_secret_store_roundtrip(cos, monkeypatch):
    monkeypatch.delenv("COMPANY_OS_EPHEMERAL_VAULT", raising=False)
    from app.connectors.secrets import FileSecretStore, reset_store, store

    reset_store()
    s1 = store()
    assert isinstance(s1, FileSecretStore)
    ref = s1.put(
        workspace_id="wsp_a", provider="openai", key="llm_api_key", value="sk-test"
    )
    assert s1.get(ref, requester="test") == "sk-test"

    reset_store()
    s2 = store()
    assert s2.get(ref, requester="test") == "sk-test"
    raw = (cos / "vault" / "secrets.bin").read_bytes()
    assert b"sk-test" not in raw


def test_ephemeral_flag_uses_memory(cos, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_EPHEMERAL_VAULT", "1")
    from app.connectors.secrets import LocalSecretStore, reset_store, store

    reset_store()
    s = store()
    assert isinstance(s, LocalSecretStore)


@pytest.mark.parametrize("domain", ["industrial purchasing", "family meal planning"])
def test_local_startup_keeps_saved_data_when_credentials_need_reentry(cos, monkeypatch, domain):
    from app.config import Settings
    from app.connectors import secrets
    from app.core import local_env_import, local_secrets, providers_local

    for name in ("COMPANY_OS_VAULT_SECRET", "SESSION_SECRET", "COMPANY_OS_SECRET", "SUPYMEM_LOCAL_KEY_PIPE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(local_env_import, "_environment", lambda: {})
    monkeypatch.setattr(local_secrets, "_native_material", lambda **kwargs: None)
    providers_local.save_settings(provider="openai", api_key=domain + "-private", models={"chat": "saved-model"})
    vault = secrets.FileSecretStore()
    vault.put(workspace_id=domain, provider="synthetic", key="credential", value=domain)
    preserved = {path: path.read_bytes() for path in (cos / "local-settings.json", cos / "vault/master.key", vault.path)}
    (cos / "local-key.bin").unlink()
    local_secrets._atomic_write(cos / "local-key.keychain", b"1")

    assert Settings(_env_file=None).company_os_vault_secret == ""
    status = providers_local.configured_providers()["providers"]["openai"]
    assert status["models"]["chat"] == "saved-model"
    assert not status["configured"]
    assert not (cos / "local-key.bin").exists()
    with pytest.raises(RuntimeError, match="refusing to replace"):
        secrets.FileSecretStore()
    assert all(path.read_bytes() == content for path, content in preserved.items())


@pytest.mark.parametrize("domain", ["inventory.example", "gardening.example"])
def test_local_vault_uses_private_persistent_master_key(cos, monkeypatch, domain):
    import stat
    from concurrent.futures import ThreadPoolExecutor

    from app.config import Settings
    from app.connectors import secrets
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    for env_key in ("COMPANY_OS_VAULT_SECRET", "SESSION_SECRET", "COMPANY_OS_SECRET"):
        monkeypatch.delenv(env_key, raising=False)
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    with ThreadPoolExecutor(max_workers=4) as executor:
        values = list(executor.map(lambda unused: secrets.vault_secret(), range(4)))
    assert all(value != secrets.LOCAL_VAULT_SECRET for value in values)
    assert len(set(values)) == 1
    master = cos / "vault" / "master.key"
    assert master.is_file()
    assert stat.S_IMODE(master.stat().st_mode) == 0o600
    assert Settings().company_os_vault_secret == ""
    vault = secrets.FileSecretStore()
    ref = vault.put(workspace_id=domain, provider="synthetic", key="credential", value=domain)
    assert secrets.FileSecretStore().get(ref, requester="test") == domain
    token = secrets.encrypt_at_rest(domain)
    assert secrets.decrypt_at_rest(token) == domain
    raw = vault.path.read_bytes()
    public_key = Scrypt(salt=b"company-os-founder-vault-v1", length=32, n=2**14, r=8, p=1).derive(
        secrets.LOCAL_VAULT_SECRET.encode())
    with pytest.raises(InvalidTag):
        AESGCM(public_key).decrypt(raw[:12], raw[12:], None)
    master.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        secrets.vault_secret()
    master.chmod(0o600)
    assert secrets.vault_secret() == values[0]
    assert stat.S_IMODE(master.stat().st_mode) == 0o600
    other = cos / "other"
    other.mkdir()
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(other))
    assert secrets.vault_secret() != values[0]
    with pytest.raises(InvalidTag):
        secrets.decrypt_at_rest(token)
    copied = other / "secrets.bin"
    copied.write_bytes(raw)
    with pytest.raises(RuntimeError, match="refusing to replace"):
        secrets.FileSecretStore(copied)
    assert copied.read_bytes() == raw
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    assert secrets.vault_secret() == secrets.LOCAL_VAULT_SECRET
    assert Settings().company_os_vault_secret == secrets.LOCAL_VAULT_SECRET
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_VAULT_SECRET", "synthetic-explicit-vault-secret")
    assert secrets.vault_secret() == "synthetic-explicit-vault-secret"
    assert master.is_file()


@pytest.mark.parametrize("domain", ["inventory.example", "gardening.example"])
def test_native_wrapping_key_seals_your_vault_and_blocks_folder_copies(cos, monkeypatch, domain):
    import base64
    import os
    import shutil

    from app.connectors import secrets
    from app.core import local_secrets

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    key_file = cos / "local-key.bin"
    wrapping = key_file.read_bytes() if key_file.exists() else os.urandom(32)
    monkeypatch.setattr(local_secrets, "_shell_material", lambda: {
        "key": base64.b64encode(wrapping).decode(), "legacy": {}, "storage": "keychain"})
    vault = secrets.FileSecretStore()
    reference = vault.put(workspace_id=domain, provider="synthetic", key="credential", value=domain)
    master = cos / "vault/master.key"
    assert len(master.read_bytes()) == 60
    assert secrets.vault_secret().encode() not in master.read_bytes()
    assert not (cos / "local-key.bin").exists()
    assert secrets.FileSecretStore().get(reference, requester="test") == domain
    copied = cos.parent / (cos.name + "-copied")
    shutil.copytree(cos, copied)
    encrypted = (copied / "vault/secrets.bin").read_bytes()
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(copied))
    monkeypatch.setattr(local_secrets, "_shell_material", lambda: {"key": None, "legacy": {}, "storage": "keyfile"})
    with pytest.raises(RuntimeError, match="refusing to replace"):
        secrets.FileSecretStore()
    assert (copied / "vault/secrets.bin").read_bytes() == encrypted

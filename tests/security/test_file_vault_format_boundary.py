"""Real AES-GCM/file format regressions using only temporary synthetic vaults."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import pytest

SOURCE = Path(__file__).resolve().parents[2] / "services/company_os/app/connectors/secrets.py"
TEST_KEY = "synthetic-vault-format-test-key-2026-strong-and-local-only"
VALUE = "synthetic-private-vault-value"
BRACE_NONCE = b"{" + bytes(range(1, 12))
PLAIN_NONCE = b"\x00" + bytes(range(1, 12))
READ_ERROR = "vault data is unreadable; refusing to replace it"


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    for key, value in {
        "APP_ENV": "local", "COMPANY_OS_DATA_DIR": str(tmp_path),
        "COMPANY_OS_WORKING_MEMORY_PATH": str(tmp_path / "memory.db"),
        "COMPANY_OS_DB_URL": f"sqlite:///{tmp_path / 'synthetic.db'}",
        "COMPANY_OS_VAULT_SECRET": TEST_KEY, "SESSION_SECRET": TEST_KEY,
        "LLM_OFFLINE": "1", "CHANNELS_DRY_RUN": "true",
    }.items():
        monkeypatch.setenv(key, value)
    # Import the exact vault module without app/bootstrap/config side effects.
    app = ModuleType("app")
    app.__path__ = []
    config = ModuleType("app.config")
    config.data_root = lambda: tmp_path
    app.config = config
    monkeypatch.setitem(sys.modules, "app", app)
    monkeypatch.setitem(sys.modules, "app.config", config)
    spec = importlib.util.spec_from_file_location("_file_vault_format_under_test", SOURCE)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    moment = [datetime(2026, 10, 8, tzinfo=timezone.utc)]
    monkeypatch.setattr(module, "utcnow", lambda: moment[0])
    return SimpleNamespace(module=module, root=tmp_path, path=tmp_path / "synthetic.bin", moment=moment)


def _brace_nonce(monkeypatch):
    original = os.urandom
    monkeypatch.setattr(os, "urandom", lambda size: BRACE_NONCE if size == 12 else original(size))


def _put(store, value=VALUE, workspace="workspace_synthetic"):
    return store.put(workspace_id=workspace, provider="synthetic_provider", key="synthetic_key", value=value)


def _find(store, workspace="workspace_synthetic", provider="synthetic_provider", key="synthetic_key"):
    return store.find(workspace_id=workspace, provider=provider, key=key, requester="synthetic_test")


def _legacy():
    return {"values": {"vault:synthetic_legacy": VALUE}, "meta": {"vault:synthetic_legacy": {
        "workspace_id": "workspace_synthetic", "provider": "synthetic_provider", "key": "synthetic_key",
        "created_at": "2025-01-01T00:00:00+00:00",
    }}}


def _encrypted(module, payload, nonce=PLAIN_NONCE):
    return nonce + AESGCM(module._derive_vault_key()).encrypt(nonce, payload, None)


def _refused(operation, *, sensitive, same_message=None):
    with pytest.raises(RuntimeError) as caught:
        operation()
    message = str(caught.value)
    assert message == READ_ERROR
    assert not any(value in message for value in sensitive)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__ is True
    if same_message is not None:
        assert message == same_message
    return message


def test_brace_nonce_survives_initialization_put_rotation_reopen_and_ciphertext_copy(vault, monkeypatch):
    _brace_nonce(monkeypatch)
    module, path = vault.module, vault.path
    store = module.FileSecretStore(path)
    assert path.read_bytes()[:12] == BRACE_NONCE and store.refs() == []
    first = _put(store)
    vault.moment[0] += timedelta(seconds=1)
    second = _put(store, "synthetic-rotated-value")
    other = _put(store, "synthetic-other-workspace", "workspace_other")
    raw = path.read_bytes()
    assert raw[:12] == BRACE_NONCE and VALUE.encode() not in raw
    assert b"synthetic-rotated-value" not in raw and b"workspace_synthetic" not in raw
    decoded = json.loads(AESGCM(module._derive_vault_key()).decrypt(raw[:12], raw[12:], None))
    assert decoded["meta"][first]["workspace_id"] == "workspace_synthetic"
    assert decoded["meta"][second]["created_at"] > decoded["meta"][first]["created_at"]
    copy = vault.root / "ciphertext-copy.bin"
    copy.write_bytes(raw)
    for candidate in (path, copy):
        reopened = module.FileSecretStore(candidate)
        assert reopened.get(first, requester="synthetic_test") == VALUE
        assert reopened.get(second, requester="synthetic_test") == "synthetic-rotated-value"
        assert reopened.refs("workspace_synthetic") == sorted([first, second])
        assert reopened.refs("workspace_other") == [other]
        assert _find(reopened) == "synthetic-rotated-value"
        assert _find(reopened, "workspace_other") == "synthetic-other-workspace"
        assert _find(reopened, provider="different_provider") is None
        assert _find(reopened, key="different_key") is None
        assert _find(reopened, workspace="") is None


def test_existing_nonce_ciphertext_format_without_brace_remains_readable(vault):
    original = _legacy()
    raw = _encrypted(vault.module, json.dumps(original).encode())
    vault.path.write_bytes(raw)
    reopened = vault.module.FileSecretStore(vault.path)
    assert reopened.get("vault:synthetic_legacy", requester="synthetic_test") == VALUE
    assert _find(reopened) == VALUE and reopened.refs() == ["vault:synthetic_legacy"]
    assert vault.path.read_bytes() == raw


@pytest.mark.parametrize("separate_legacy_file", [False, True])
def test_valid_plaintext_legacy_preserves_refs_scopes_and_migrates_to_ciphertext(vault, monkeypatch, separate_legacy_file):
    _brace_nonce(monkeypatch)
    root = vault.root / "vault"
    root.mkdir()
    legacy = root / "secrets.json" if separate_legacy_file else vault.path
    legacy.write_bytes(json.dumps(_legacy()).encode())
    store = vault.module.FileSecretStore() if separate_legacy_file else vault.module.FileSecretStore(vault.path)
    assert store.get("vault:synthetic_legacy", requester="synthetic_test") == VALUE
    assert store.refs("workspace_synthetic") == ["vault:synthetic_legacy"] and _find(store) == VALUE
    rotated = _put(store, "synthetic-rotated-value")
    path = root / "secrets.bin" if separate_legacy_file else vault.path
    assert path.read_bytes()[:12] == BRACE_NONCE and VALUE.encode() not in path.read_bytes()
    assert not separate_legacy_file or not legacy.exists()
    reopened = vault.module.FileSecretStore(path)
    assert reopened.get("vault:synthetic_legacy", requester="synthetic_test") == VALUE
    assert reopened.get(rotated, requester="synthetic_test") == "synthetic-rotated-value"
    assert _find(reopened) == "synthetic-rotated-value"


@pytest.mark.parametrize("bad", [b"", b"{invalid-json", b"[]", b"{}", b'{"values":[],"meta":{}}', b'{"values":{},"meta":[]}'])
def test_invalid_separate_legacy_is_retained_without_ciphertext_or_unlink(vault, bad):
    root = vault.root / "vault"
    root.mkdir()
    legacy = root / "secrets.json"
    legacy.write_bytes(bad)
    message = None
    for _ in range(2):
        message = _refused(vault.module.FileSecretStore, sensitive=[VALUE, str(legacy)], same_message=message)
        assert legacy.read_bytes() == bad
        assert not (root / "secrets.bin").exists() and not (root / "secrets.tmp").exists()


@pytest.mark.parametrize("damage", ["empty", "truncated_nonce", "truncated_tag", "tag_tamper", "wrong_key", "legacy_json", "legacy_shape", "encrypted_json", "encrypted_shape"])
def test_corrupt_existing_vault_never_resets_or_overwrites_and_original_refs_survive(vault, monkeypatch, damage):
    module, path = vault.module, vault.path
    store = module.FileSecretStore(path)
    ref = _put(store)
    original = path.read_bytes()
    if damage == "empty":
        raw = b""
    elif damage == "truncated_nonce":
        raw = original[:11]
    elif damage == "truncated_tag":
        raw = original[:-1]
    elif damage == "tag_tamper":
        raw = original[:-1] + bytes([original[-1] ^ 1])
    elif damage == "wrong_key":
        raw = original
        monkeypatch.setenv("COMPANY_OS_VAULT_SECRET", TEST_KEY + "-different")
    elif damage == "legacy_json":
        raw = b'{"values":{"vault:synthetic":"synthetic-private-vault-value"}'
    elif damage == "legacy_shape":
        raw = b'{"values":[],"meta":{}}'
    elif damage == "encrypted_json":
        raw = _encrypted(module, b"{invalid-json", BRACE_NONCE)
    else:
        raw = _encrypted(module, b'{"values":[],"meta":{}}', BRACE_NONCE)
    path.write_bytes(raw)
    sensitive = [VALUE, "synthetic-incoming-value", ref, str(path), TEST_KEY]
    message = _refused(lambda: module.FileSecretStore(path), sensitive=sensitive)
    for operation in (
        lambda: _put(store, "synthetic-incoming-value"),
        lambda: store.get(ref, requester="synthetic_test"), lambda: _find(store), lambda: store.refs(),
    ):
        _refused(operation, sensitive=sensitive, same_message=message)
        assert path.read_bytes() == raw
    assert not path.with_suffix(".tmp").exists()
    monkeypatch.setenv("COMPANY_OS_VAULT_SECRET", TEST_KEY)
    path.write_bytes(original)
    reopened = module.FileSecretStore(path)
    assert reopened.refs() == [ref] and reopened.get(ref, requester="synthetic_test") == VALUE
    assert _find(reopened) == VALUE


def test_unreadable_existing_vault_retains_bytes_and_never_accepts_put(vault, monkeypatch):
    store = vault.module.FileSecretStore(vault.path)
    ref = _put(store)
    original = vault.path.read_bytes()
    read = Path.read_bytes

    def unreadable(path):
        if path == vault.path:
            raise PermissionError("synthetic private access detail")
        return read(path)

    monkeypatch.setattr(Path, "read_bytes", unreadable)
    sensitive = [VALUE, ref, str(vault.path), "synthetic private access detail"]
    message = _refused(lambda: vault.module.FileSecretStore(vault.path), sensitive=sensitive)
    _refused(lambda: _put(store, "synthetic-incoming-value"), sensitive=sensitive, same_message=message)
    assert read(vault.path) == original and not vault.path.with_suffix(".tmp").exists()

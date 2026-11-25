from __future__ import annotations

import hashlib
import os
import shutil
from types import SimpleNamespace

import pytest

from tests.unit.test_cli_local import local_cli


@pytest.fixture
def windows_keys(local_cli, monkeypatch):
    from app.core import local_secrets, local_windows

    windows_os = SimpleNamespace(**{**vars(os), "name": "nt"})
    monkeypatch.setattr(local_secrets, "os", windows_os)
    credentials = {}
    monkeypatch.setattr(local_windows, "_read_credential", credentials.get)
    monkeypatch.setattr(local_windows, "_read_password_credential", lambda target: None)
    monkeypatch.setattr(local_windows, "_write_credential", lambda target, value: credentials.update({target: value}))

    def write(path, content):
        path.write_bytes(content)
        path.chmod(0o600)

    monkeypatch.setattr(local_windows, "atomic_write", write)
    return credentials


def test_windows_key_reference_survives_directory_move(local_cli, monkeypatch, windows_keys):
    cli, root, database, domain = local_cli
    from app.connectors.secrets import FileSecretStore
    from app.core import local_secrets, providers_local
    from tests.unit.test_cli_local import seed

    path = root / "local-key.bin"
    identity = os.path.normcase(str(path.parent.resolve() / path.name))
    reference = hashlib.sha256(identity.encode()).hexdigest()
    key = os.urandom(32)
    windows_keys["com.supymem.local/key/" + reference] = key
    for section, field in (("smtp", "password"), ("search", "api_key"), ("publishing", "token"), ("composio", "api_key")):
        local_secrets.update_settings(section, {}, {field: domain + section})
    providers_local.save_settings(provider="openai", api_key=domain + "model", models={"chat": "local-model"})
    vault_ref = FileSecretStore().put(workspace_id=domain, provider="synthetic", key="credential", value=domain)
    assert (root / "local-key.keychain").read_text() == reference
    assert not path.exists()
    moved = root.parent / "moved"
    seed(database, domain)
    shutil.move(root, moved)
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(moved))
    assert local_secrets._key(moved) == key
    archive = cli._backup(moved, moved / database.name, root.parent / "backups")
    restored = root.parent / "restored"
    cli._restore(archive, restored, restored / database.name)
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(restored))
    assert local_secrets._key(restored) == key
    for section, field in (("smtp", "password"), ("search", "api_key"), ("publishing", "token"), ("composio", "api_key"), ("model", "openai")):
        assert local_secrets.get_secret(section, field) == domain + section
    assert FileSecretStore().get(vault_ref, requester="test") == domain
    assert not (restored / path.name).exists()
    assert key not in (restored / "local-key.keychain").read_bytes()
    assert len(windows_keys) == 1


def test_source_defaults_match_desktop_data_folder(local_cli, monkeypatch):
    import json
    from pathlib import Path
    from app.config import data_root
    from app.cli_local import local_data_directory
    from app.db.session import resolve_database_url
    from sqlalchemy.engine import make_url

    cli, root, _, _domain = local_cli
    home = root.parent / "home"
    environment = {"SUPYMEM_MODE": "local", "HOME": str(home), "USERPROFILE": str(home)}
    configuration = json.loads((Path(cli.__file__).parents[3] / "src-tauri/tauri.local.json").read_text())
    expected = local_data_directory(configuration["productName"], environment)
    assert data_root(environment) == expected
    assert Path(make_url(resolve_database_url(environment)).database) == expected / "database.sqlite3"


def test_windows_legacy_keys_survive_first_directory_move(local_cli, monkeypatch, windows_keys):
    cli, root, _database, _domain = local_cli
    from app.core import local_secrets

    path = root / "local-key.bin"
    original = os.path.normcase(str(path))
    key = os.urandom(32)
    reference = hashlib.sha256(original.encode()).hexdigest()
    windows_keys["com.supymem.local/key/" + reference] = key
    cli._record_roots(root, [str(root)])
    moved = root.parent / "moved"
    shutil.move(root, moved)
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(moved))
    stale = os.path.normcase(str(moved / path.name))
    windows_keys["com.supymem.local/key/" + hashlib.sha256(stale.encode()).hexdigest()] = os.urandom(32)
    assert local_secrets._key(moved) == key
    local_secrets.update_settings("smtp", {}, {"password": _domain})
    assert local_secrets.get_secret("smtp", "password") == _domain
    assert (moved / "local-key.keychain").read_text() == reference
    assert not (moved / path.name).exists()


def test_windows_missing_reference_never_replaces_your_key(local_cli, windows_keys):
    _, root, _, domain = local_cli
    from app.core import local_secrets

    local_secrets.update_settings("smtp", {}, {"password": domain})
    encrypted = (root / "local-settings.json").read_bytes()
    marker = (root / "local-key.keychain").read_bytes()
    windows_keys.clear()
    with pytest.raises(ValueError, match="recovery key"):
        local_secrets._native_material(create=True)
    assert not windows_keys
    assert (root / "local-key.keychain").read_bytes() == marker
    assert (root / "local-settings.json").read_bytes() == encrypted

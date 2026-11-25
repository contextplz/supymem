"""Private, atomic local settings shared by settings and provider credentials."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from core.logging import register_local_secret

from app.core.local_mode import is_local
from app.core.local_process import check_private, file_lock, open_private


def _read_private(path: Path) -> bytes | None:
    try:
        descriptor = open_private(path, os.O_RDONLY)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as source:
        check_private(source)
        content = source.read(1_048_577)
        if len(content) > 1_048_576:
            raise ValueError("Local settings file is too large.")
        return content


def _atomic_write(path: Path, content: bytes) -> None:
    if os.name == "nt":
        from app.core.local_windows import atomic_write

        atomic_write(path, content)
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as target:
            temporary = Path(target.name)
            os.fchmod(target.fileno(), 0o600)
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def _locked():
    from app.config import data_root

    if not is_local():
        raise ValueError("Local settings are disabled. Use your account settings.")
    root = data_root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = open_private(root / "local-settings.lock", os.O_CREAT | os.O_RDWR)
    with os.fdopen(descriptor, "rb") as lock:
        check_private(lock)
        with file_lock(lock):
            yield root


class ConnectionSecretRequired(ValueError):
    pass


_UNREADABLE_VAULT = ("Unlock your system keychain and reopen your app, then try again. If you cannot open your saved keys, "
                     "open Keys and connections > Your data and recovery and restore your backup and encryption recovery key.")

_material: dict | None = None
_material_started = False
_material_lock = threading.Lock()


def _macos_password(account: str, password: str | None = None) -> str | None:
    import ctypes

    security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
    core = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    security.SecKeychainSetUserInteractionAllowed.argtypes = [ctypes.c_bool]
    if security.SecKeychainSetUserInteractionAllowed(False) != 0:
        raise OSError("Your system keychain is unavailable.")
    service, username = b"Supymem Local", account.encode()
    size, data, item = ctypes.c_uint32(), ctypes.c_void_p(), ctypes.c_void_p()
    find = security.SecKeychainFindGenericPassword
    find.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32,
        ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    find.restype = ctypes.c_int32
    free = security.SecKeychainItemFreeContent
    free.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    core.CFRelease.argtypes = [ctypes.c_void_p]
    try:
        status = find(None, len(service), service, len(username), username,
            ctypes.byref(size), ctypes.byref(data), ctypes.byref(item))
        if status not in {0, -25300}:
            raise OSError("Your system keychain is locked.")
        if password is None:
            return ctypes.string_at(data, size.value).decode() if status == 0 else None
        encoded = password.encode()
        if status == 0:
            modify = security.SecKeychainItemModifyAttributesAndData
            modify.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p]
            status = modify(item, None, len(encoded), encoded)
        else:
            add = security.SecKeychainAddGenericPassword
            add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32,
                ctypes.c_char_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_void_p]
            status = add(None, len(service), service, len(username), username, len(encoded), encoded, ctypes.byref(item))
        if status != 0:
            raise OSError("Your system keychain could not save the key.")
        return password
    finally:
        if data.value:
            free(None, data)
        if item.value:
            core.CFRelease(item)


def _native_material(*, create: bool = False, seed_key: bytes | None = None) -> dict | None:
    from app.config import data_root

    root = data_root()
    path = root / "local-key.bin"
    account = hashlib.sha256(str(root.resolve()).encode()).hexdigest()
    reference = None
    if os.name == "nt":
        from app.core.local_windows import _read_credential, _read_password_credential, _write_credential

        marker = _read_private(root / "local-key.keychain")
        previous = _read_private(path)
        saved_reference = marker if _windows_reference(marker) else previous if _windows_reference(previous) else None
        reference = saved_reference.decode() if saved_reference else hashlib.sha256(os.path.normcase(str(path.resolve())).encode()).hexdigest()
        if saved_reference is None:
            from app.cli_local import _directory_roots

            for origin in _directory_roots(root, create=False)[1:]:
                candidate = hashlib.sha256(os.path.normcase(str(Path(origin) / path.name)).encode()).hexdigest()
                if _read_credential(f"com.supymem.local/key/{candidate}") is not None:
                    reference = candidate
                    saved_reference = candidate.encode()
                    break
        target = f"com.supymem.local/key/{reference}"

        def read():
            nonlocal reference
            if saved_reference is None:
                value = _read_password_credential(f"{account}@Supymem Local")
                if value is not None:
                    reference = None
                    return base64.b64decode(value, validate=True)
            return _read_credential(target)

        def write(key):
            _write_credential(target, key)
    else:
        helper = shutil.which("secret-tool") if sys.platform != "darwin" else None
        if sys.platform != "darwin" and helper is None:
            return None

        def password(value=None):
            if sys.platform == "darwin":
                return _macos_password(account, value)
            arguments = [helper, "lookup" if value is None else "store", *([] if value is None else ["--label=Local settings"]),
                "service", "Supymem Local", "username", account]
            result = subprocess.run(arguments, input=value, capture_output=True, text=True, timeout=2)
            if result.returncode != 0 and not (value is None and result.returncode == 1 and not result.stderr):
                raise OSError("Your system keychain is unavailable.")
            return result.stdout.strip() or None if value is None else value

        def read():
            value = password()
            return base64.b64decode(value, validate=True) if value else None

        def write(key):
            password(base64.b64encode(key).decode())
    try:
        key = read()
    except (ValueError, UnicodeError):
        raise ConnectionSecretRequired("Open Keys and connections and re-enter your keys. Keep your previous encrypted settings for recovery.") from None
    except (OSError, subprocess.SubprocessError):
        if create:
            raise ConnectionSecretRequired("Unlock your system keychain, then reopen Keys and connections and save your key again.") from None
        return None
    if key is None and create:
        if os.name == "nt" and saved_reference is not None:
            raise ConnectionSecretRequired("Open Keys and connections, under Your data and recovery, restore your separately saved encryption recovery key, then restart your app.")
        previous = _read_private(path)
        if (previous is not None and len(previous) != 32 and not _windows_reference(previous)
                or seed_key is not None and (len(seed_key) != 32 or previous is not None and len(previous) == 32 and previous != seed_key)):
            raise ConnectionSecretRequired("Restore your separately saved encryption recovery key, then restart your app.")
        key = seed_key if seed_key is not None else previous if previous is not None and len(previous) == 32 else AESGCM.generate_key(bit_length=256)
        try:
            write(key)
            if read() != key:
                raise OSError
        except (OSError, subprocess.SubprocessError):
            raise ConnectionSecretRequired("Unlock your system keychain, then reopen Keys and connections and save your key again.") from None
    if key is not None and len(key) != 32:
        raise ConnectionSecretRequired("Open Keys and connections and re-enter your keys. Keep your previous encrypted settings for recovery.")
    return {"key": base64.b64encode(key).decode() if key is not None else None,
        "legacy": {}, "storage": "keychain", "reference": reference}


def _shell_material() -> dict | None:
    global _material_started
    if not os.environ.get("SUPYMEM_LOCAL_KEY_PIPE"):
        return _native_material() or {"key": None, "legacy": {}, "storage": "keyfile"}
    with _material_lock:
        if not _material_started:
            _material_started = True
            threading.Thread(target=_receive_material, daemon=True).start()
        material = _material
    if os.name == "nt" and material is not None and material.get("storage") == "keyfile":
        native = _native_material()
        if native is not None and native.get("key") is not None:
            if material.get("key") is not None and base64.b64decode(material["key"], validate=True) != base64.b64decode(native["key"], validate=True):
                raise ConnectionSecretRequired("Restore your separately saved encryption recovery key, then restart your app.")
            return {**material, **native, "legacy": material["legacy"]}
    return material


def _receive_material() -> None:
    global _material
    try:
        with os.fdopen(os.dup(0), "rb") as source:
            payload = source.readline(1_048_577)
        material = json.loads(payload)
        if (len(payload) > 1_048_576 or not isinstance(material, dict)
                or material.get("storage") not in {"keychain", "keyfile"}
                or not isinstance(material.get("legacy"), dict)):
            raise ValueError
        if material.get("key") is not None:
            key = base64.b64decode(material["key"], validate=True)
            if len(key) != 32:
                raise ValueError
    except (OSError, ValueError, TypeError):
        material = {"key": None, "legacy": {}, "storage": "keyfile"}
    with _material_lock:
        _material = material


def legacy_secret(account: str) -> str | None:
    material = _shell_material()
    value = material.get("legacy", {}).get(account) if material is not None else None
    if value is not None and not isinstance(value, str):
        raise ValueError("Open Keys and connections, re-enter your saved keys, then retry.")
    return value


def acknowledge_legacy(accounts: list[str]) -> None:
    if not accounts or not os.environ.get("SUPYMEM_LOCAL_KEY_PIPE"):
        return
    with _locked() as root:
        _acknowledge_legacy(root, accounts)


def _acknowledge_legacy(root: Path, accounts: list[str]) -> None:
    path = root / "local-credentials-migrated.json"
    previous = _read_private(path)
    completed = json.loads(previous) if previous else []
    _atomic_write(path, json.dumps(sorted(set(completed + accounts))).encode())


def storage_notice() -> str:
    from app.config import data_root

    material = _shell_material()
    notices = []
    if material is not None and material.get("storage") == "keyfile":
        if sys.platform == "linux":
            notices.append("Your keys are encrypted in a protected file because no system keychain was found. Keep your data folder private.")
        else:
            notices.append("Your keys are encrypted with a key file in your data folder. Keep that folder private. Unlock your system keychain, restart your app, then reopen Keys and connections to store the encryption key separately.")
    if material is not None and material.get("storage") == "keychain" and os.name != "nt":
        master = _read_private(data_root() / "vault/master.key")
        if master is not None and len(master) == 64 and all(character in b"0123456789abcdef" for character in master):
            notices.append("Your connected app keys are encrypted with a key file in your data folder. Keep that folder private. Keep your full backups private too; copying them can also copy the encryption key.")
    if material is not None and (material.get("needs_reentry") or (
            material.get("key") is None and (data_root() / "local-key.keychain").exists()
            and not (data_root() / "local-key.bin").exists())):
        notices.append(_UNREADABLE_VAULT if material.get("key") is None and (data_root() / "local-settings.json").exists()
                       else "Open Keys and connections and re-enter any missing keys. Your previous encrypted files are kept in your data folder.")
    return " ".join(notices)


def _windows_reference(content: bytes | None) -> bool:
    return os.name == "nt" and content is not None and len(content) == 64 and all(byte in b"0123456789abcdef" for byte in content)


def _key(root: Path, *, create: bool = False) -> bytes:
    path = root / "local-key.bin"
    material = _shell_material()
    if material is None:
        raise ConnectionSecretRequired("Your keys are still opening. Open Keys and connections and retry in a moment.")
    encoded = material.get("key")
    key = _read_private(path)
    if os.name == "nt" and material.get("storage") == "keyfile" and os.environ.get("SUPYMEM_LOCAL_KEY_PIPE") and (create or key is not None or encoded is not None):
        native = _native_material()
        if native is not None and native.get("key") is None:
            native = _native_material(create=True, seed_key=base64.b64decode(encoded, validate=True) if encoded is not None else None)
        if native is not None:
            material = {**material, **native, "legacy": material["legacy"]}
            encoded = material.get("key")
    if encoded is None and material.get("storage") == "keychain" and not os.environ.get("SUPYMEM_LOCAL_KEY_PIPE"):
        material = _native_material(create=create or key is not None) or material
        encoded = material.get("key")
    if _windows_reference(key):
        if encoded is not None or create:
            backup = root / "local-key.keychain-recovery.bin"
            if _read_private(backup) not in {None, key}:
                backup = root / f"local-key.keychain-recovery-{os.urandom(8).hex()}.bin"
            _atomic_write(backup, key)
        key = None
    if encoded is not None:
        stored = base64.b64decode(encoded, validate=True)
        if len(stored) != 32 or key is not None and key != stored:
            raise ValueError("Restore your separately saved encryption recovery key, then restart your app.")
        if material.get("storage") == "keyfile" and key is None:
            _atomic_write(path, stored)
        if material.get("storage") == "keychain":
            _mark_keychain(root, material)
        return stored
    if key is None:
        if not create:
            raise ValueError("Restore your separately saved encryption recovery key, then restart your app.")
        key = AESGCM.generate_key(bit_length=256)
    if len(key) != 32:
        raise ValueError("Restore your separately saved encryption recovery key, then restart your app.")
    if _read_private(path) is None or _windows_reference(_read_private(path)):
        _atomic_write(path, key)
    return key


def _mark_keychain(root: Path, material: dict | None = None) -> None:
    material = material if material is not None else _shell_material()
    if material is not None and material.get("storage") == "keychain":
        marker = root / "local-key.keychain"
        reference = material.get("reference")
        content = reference.encode() if reference else b"shell"
        if _read_private(marker) != content:
            _atomic_write(marker, content)
        (root / "local-key.bin").unlink(missing_ok=True)


def _load(root: Path) -> tuple[dict, dict, dict | None]:
    content = _read_private(root / "local-settings.json")
    state = json.loads(content) if content is not None else {"values": {}, "secrets": None}
    if not isinstance(state, dict) or not {"values", "secrets"} <= set(state) or set(state) - {"values", "secrets", "sources"}:
        raise ValueError("Restore your local settings from your backup.")
    values = state["values"]
    if not isinstance(values, dict) or any(not isinstance(value, dict) for value in values.values()):
        raise ValueError("Restore your local settings from your backup.")
    sources = state.setdefault("sources", {})
    if not isinstance(sources, dict) or any(
        not isinstance(fields, dict) or any(not isinstance(value, str) or value not in {"app", "env"} for value in fields.values())
        for fields in sources.values()
    ):
        raise ValueError("Restore your local settings from your backup.")
    secrets = {}
    if state["secrets"] is not None:
        if not isinstance(state["secrets"], str):
            raise ValueError("Restore your local settings from your backup.")
        token = base64.b64decode(state["secrets"], validate=True)
        material = _shell_material()
        key_file = _read_private(root / "local-key.bin")
        if material is None or (material.get("key") is None
                and (key_file is None or _windows_reference(key_file))
                and (_read_private(root / "local-key.keychain") is not None or material.get("needs_reentry")
                     or _windows_reference(key_file))):
            return state, values, None
        wrapping_key = _key(root)
        secrets = json.loads(AESGCM(wrapping_key).decrypt(token[:12], token[12:], b"local-settings"))
        if not isinstance(secrets, dict) or any(
            not isinstance(fields, dict) or any(not isinstance(value, str) for value in fields.values())
            for fields in secrets.values()
        ):
            raise ValueError("Restore your local settings from your backup.")
        _mark_keychain(root)
        if legacy_secret("settings-wrapping-key") == base64.b64encode(wrapping_key).decode("ascii"):
            _acknowledge_legacy(root, ["settings-wrapping-key"])
    sources = state.setdefault("sources", {})
    if not isinstance(sources, dict) or any(
        not isinstance(fields, dict) or any(not isinstance(value, str) or value not in {"app", "env"} for value in fields.values())
        for fields in sources.values()
    ):
        raise ValueError("Restore your local settings from your backup.")
    for section in values.keys() | secrets.keys():
        fields = sources.setdefault(section, {})
        secret_fields = set() if section == "model" else secrets.get(section, {}).keys()
        if section == "model":
            for name in secrets.get(section, {}):
                fields.pop(name, None)
        for name in values.get(section, {}).keys() | secret_fields:
            fields.setdefault(name, "app")
    for fields in secrets.values():
        for value in fields.values():
            register_local_secret(value)
    return state, values, secrets


def _view(section: str, values: dict, secrets: dict | None, sources: dict) -> dict:
    secrets = secrets or {}
    fields = values.get(section, {})
    provenance = fields.get("sources", {}) if section == "model" else sources.get(section, {})
    visible = {name: value for name, value in fields.items() if name != "sources"} if section == "model" else fields
    material = _shell_material()
    unreadable = material is not None
    result = {"values": visible, "sources": provenance, "secrets_set": {
        name: bool(value) for name, value in secrets.get(section, {}).items()
    }}
    missing = [name for name in provenance if name not in visible and not secrets.get(section, {}).get(name)] if unreadable else []
    if missing:
        result["unreadable_secrets"] = missing
    return result


def read_settings(section: str) -> dict:
    from app.core.local_env_import import import_local_env

    import_local_env()
    if section == "model":
        from app.core.providers_local import _model_view

        return _model_view()
    with _locked() as root:
        state, values, secrets = _load(root)
        return _view(section, values, secrets, state["sources"])


def get_secret(section: str, name: str) -> str | None:
    from app.core.local_env_import import import_local_env

    import_local_env()
    if section == "model":
        return _model_snapshot()["providers"].get(name, {}).get("api_key") or None
    with _locked() as root:
        _, _, secrets = _load(root)
        return (secrets or {}).get(section, {}).get(name)


def _model_snapshot() -> dict:
    from app.core.local_env_import import import_local_env
    from app.core.providers_local import _read, _require_local, _root

    _require_local()
    import_local_env()
    return _read(_root())


def update_settings(section: str, values: dict, secrets: dict[str, str], *, source: str = "app") -> dict:
    if source not in {"app", "env"}:
        raise ValueError("Choose app settings or a .env import, then retry.")
    if section == "model":
        from app.core.providers_local import _update_model

        return _update_model(values, secrets, source=source)
    return _update_settings(section, values, secrets, source=source)


def _update_settings(section: str, values: dict, secrets: dict[str, str], *, source: str = "app") -> dict:
    with _locked() as root:
        state, stored_values, stored_secrets = _load(root)
        sources = state["sources"].setdefault(section, {})
        values = {name: value for name, value in values.items() if source == "app" or sources.get(name) != "app"}
        secrets = {name: value for name, value in secrets.items() if source == "app" or sources.get(name) != "app"}
        if (all(stored_values.get(section, {}).get(name) == value and sources.get(name) == source for name, value in values.items())
                and all(stored_secrets is not None and stored_secrets.get(section, {}).get(name) == value
                        and sources.get(name) == source for name, value in secrets.items())):
            return _view(section, stored_values, stored_secrets, state["sources"])
        destinations = {
            "smtp": ("host", "port", "user", "imap_host", "imap_port"),
            "search": ("provider", "firecrawl_url"),
            "publishing": ("provider", "project", "bucket", "region", "prefix", "site_url"),
        }
        previous = stored_values.get(section, {})
        changed = {name for name in destinations.get(section, ()) if name in values and values[name] != previous.get(name)}
        if stored_secrets is None and (secrets or changed or section == "model"):
            raise ConnectionSecretRequired(_UNREADABLE_VAULT)
        protected = (stored_secrets or {}).get(section, {})
        if section == "search":
            protected = {name: protected.get(name) for name in (
                (["api_key"] if "provider" in changed and values.get("provider") in ("tavily", "exa") else [])
                + (["firecrawl_key"] if "firecrawl_url" in changed and values.get("firecrawl_url") else []))}
        elif section == "publishing":
            provider = values.get("provider", previous.get("provider"))
            if provider in ("vercel", "netlify"):
                changed.intersection_update(("provider", "project", "site_url"))
                protected = {"token": protected.get("token")}
            elif provider == "s3":
                changed.discard("project")
                protected = {name: protected.get(name) for name in ("access_key_id", "secret_access_key", "session_token")}
        missing = [name for name, value in protected.items() if value and not secrets.get(name)] if changed else []
        if missing:
            fields = ", ".join(name.replace("_", " ") for name in missing)
            raise ConnectionSecretRequired(f"Open Keys and connections, re-enter your {fields} for this changed connection, then save again.")
        stored_values.setdefault(section, {}).update(values)
        source_fields = values.keys() if section == "model" else values.keys() | secrets.keys()
        sources.update({name: source for name in source_fields})
        if secrets:
            for value in secrets.values():
                register_local_secret(value)
            stored_secrets.setdefault(section, {}).update(secrets)
            nonce = os.urandom(12)
            wrapping_key = _key(root, create=True)
            token = nonce + AESGCM(wrapping_key).encrypt(
                nonce, json.dumps(stored_secrets).encode(), b"local-settings"
            )
            state["secrets"] = base64.b64encode(token).decode("ascii")
        _atomic_write(root / "local-settings.json", json.dumps(state, allow_nan=False).encode())
        if stored_secrets is not None and state["secrets"] is not None:
            _mark_keychain(root)
        if secrets and legacy_secret("settings-wrapping-key") == base64.b64encode(wrapping_key).decode("ascii"):
            _acknowledge_legacy(root, ["settings-wrapping-key"])
        return _view(section, stored_values, stored_secrets, state["sources"])

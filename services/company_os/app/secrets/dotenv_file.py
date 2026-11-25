"""Write one env key at a time. Never log the value."""

from __future__ import annotations

import os
import re
from pathlib import Path

from app.config import REPO_ROOT, data_root, is_local_env
from app.core.local_mode import is_local

_KEY = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")


def secrets_env_path() -> Path:
    override = os.environ.get("COMPANY_OS_SECRETS_ENV", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return data_root() / ".env"


def fingerprint(value: str) -> str:
    text = (value or "").strip()
    if len(text) < 4:
        return "set"
    return text[-4:]


def read_env_file(path: Path | None = None) -> dict[str, str]:
    p = path or secrets_env_path()
    out: dict[str, str] = {}
    if not p.is_file():
        return out
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip("'").strip('"')
        if key:
            out[key] = val
    return out


def env_is_set(env_key: str) -> bool:
    if not _KEY.match(env_key or ""):
        return False
    if is_local():
        from app.core.local_env_import import _FIELDS
        from app.core.local_secrets import get_secret, read_settings
        from app.core.providers_local import PROVIDER_ENV

        for provider, names in PROVIDER_ENV.items():
            if env_key == names[0] or provider == "azure_openai" and env_key == "AZURE_API_KEY":
                return bool(get_secret("model", provider))
        if env_key in _FIELDS:
            section, name, secret = _FIELDS[env_key]
            return bool(get_secret(section, name) if secret else read_settings(section)["values"].get(name))
        from app.connectors.secrets import FileSecretStore
        from app.secrets.catalog import slot_for_env_key

        spec = slot_for_env_key(env_key)
        return bool(spec and (data_root() / "vault/secrets.bin").exists() and FileSecretStore().find(workspace_id="machine", provider=spec.provider, key=env_key,
            requester="secret.status"))
    if (os.environ.get(env_key) or "").strip():
        return True
    return bool((read_env_file().get(env_key) or "").strip())


def upsert_env(env_key: str, value: str, *, path: Path | None = None, mirror: bool = True) -> Path:
    """Replace or append ``env_key`` in the secrets .env. Also sets process env.

    Local only: on hosted envs the process env holds platform secrets shared by every tenant.
    """
    if not (is_local() or is_local_env()):
        raise PermissionError("env_write_refused")
    if not _KEY.match(env_key or ""):
        raise ValueError("env_key must be an uppercase NAME.")
    raw = (value or "").strip()
    if not raw:
        raise ValueError("value is required.")
    if is_local():
        from app.core.local_env_import import _FIELDS
        from app.core.local_secrets import read_settings, update_settings
        from app.core.providers_local import PROVIDER_ENV

        for provider, names in PROVIDER_ENV.items():
            if env_key == names[0] or provider == "azure_openai" and env_key == "AZURE_API_KEY":
                selected = read_settings("model")["values"].get("provider")
                update_settings("model", {} if selected else {"provider": provider}, {provider: raw})
                return data_root() / "local-settings.json"
        if env_key in _FIELDS:
            section, name, secret = _FIELDS[env_key]
            if not secret:
                raise ValueError("Open Keys and connections and save this setting on its connection screen, then retry.")
            update_settings(section, {}, {name: raw})
        else:
            from app.connectors.secrets import FileSecretStore
            from app.secrets.catalog import slot_for_env_key

            spec = slot_for_env_key(env_key)
            if spec is None:
                raise ValueError("Open Keys and connections and choose the named connection for this key, then retry.")
            FileSecretStore().put(workspace_id="machine", provider=spec.provider, key=env_key, value=raw)
        return data_root() / "local-settings.json"
    target = path or secrets_env_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = target.read_text(encoding="utf-8") if target.is_file() else ""
    lines = existing.splitlines()
    written = False
    out: list[str] = []
    quoted = _quote(raw)
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key == env_key:
                out.append(f"{env_key}={quoted}")
                written = True
                continue
        out.append(line)
    if not written:
        if out and out[-1].strip():
            out.append("")
        out.append(f"{env_key}={quoted}")
    text = "\n".join(out).rstrip() + "\n"
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(text)
        stream.truncate()
    os.environ[env_key] = raw
    try:
        from coding_agents.env import mark_company_os_owned

        mark_company_os_owned([env_key])
    except Exception:
        pass
    if mirror:
        _mirror_repo_env(env_key, raw)
    return target


def _quote(value: str) -> str:
    if re.search(r"[\s#\"']", value):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _mirror_repo_env(env_key: str, value: str) -> None:
    """Local dev also loads repo ``.env``. Keep the same key in sync, never commit it."""
    if not is_local_env():
        return
    repo_env = REPO_ROOT / ".env"
    if not repo_env.is_file():
        return
    if secrets_env_path().resolve() == repo_env.resolve():
        return
    try:
        upsert_env(env_key, value, path=repo_env, mirror=False)
    except Exception:
        return

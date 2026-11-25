"""Request and save secret slots. Values never appear in request records or status."""

from __future__ import annotations

import json
import os
import re
import threading
from typing import Any

from cryptography.exceptions import InvalidTag

from app.config import data_root, is_local_env
from app.core.local_mode import is_local
from app.secrets.catalog import SlotSpec, get_slot, slot_for_env_key, slot_for_provider
from app.secrets.dotenv_file import env_is_set, fingerprint, secrets_env_path, upsert_env
from core.clock import utcnow
from core.env_contract import parse_app_env
from core.ids import new_id

_LOCK = threading.Lock()
_REQUESTS: dict[str, dict[str, Any]] = {}
ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")


class SecretSlotError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def reset_secret_slots() -> None:
    with _LOCK:
        _REQUESTS.clear()
    path = _requests_path()
    if path.exists():
        path.unlink()


def _requests_path():
    return data_root() / "secret_slot_requests.json"


def _load() -> dict[str, dict[str, Any]]:
    path = _requests_path()
    if not path.is_file():
        return dict(_REQUESTS)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return dict(_REQUESTS)
    if isinstance(data, dict):
        merged = {**data, **_REQUESTS}
        return {k: v for k, v in merged.items() if isinstance(v, dict)}
    return dict(_REQUESTS)


def _save(rows: dict[str, dict[str, Any]]) -> None:
    _REQUESTS.clear()
    _REQUESTS.update(rows)
    path = _requests_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, indent=2), encoding="utf-8")


def _spec_or_raise(slot_id: str) -> SlotSpec:
    spec = get_slot(slot_id) or slot_for_provider(slot_id)
    if spec is None:
        raise SecretSlotError("unknown_slot", f"No secret slot named {slot_id}.")
    return spec


def _request_workspace(venture_id: str | None, workspace_id: str | None) -> str | None:
    """Hosted request fields bind to the verified tenant; they cannot grant access."""
    if is_local() or parse_app_env() == "local":
        return workspace_id
    from app.tenancy import current_or_none

    scope = current_or_none()
    if scope is None or scope.workspace_id in {"", "none"} or scope.actor == "anonymous":
        raise SecretSlotError("workspace_scope_required", "Choose a workspace before requesting credentials.")
    if workspace_id and workspace_id != scope.workspace_id:
        raise SecretSlotError("workspace_mismatch", "The credential request belongs to another workspace.")
    if venture_id:
        from app.db.models import Venture
        from app.state import db

        with db()() as session:
            venture = session.get(Venture, venture_id)
            if venture is None or venture.workspace_id != scope.workspace_id:
                raise SecretSlotError("workspace_mismatch", "The venture does not belong to this workspace.")
    return scope.workspace_id


def _status_for(spec: SlotSpec, *, workspace_id: str | None = None) -> dict[str, Any]:
    local = is_local() or parse_app_env() == "local"
    if not local:
        workspace_id = _request_workspace(None, workspace_id)
    fields = []
    armed = True
    for key in spec.env_keys:
        if local:
            try:
                set_ = env_is_set(key)
            except (OSError, ValueError, RuntimeError, InvalidTag):
                raise SecretSlotError("local_key_unreadable", "Your saved keys could not be opened. Open Keys and connections, under Your data and recovery, restore your intact backup, then retry.") from None
        else:
            from app.connectors.secrets import store

            try:
                set_ = bool(store().find(workspace_id=workspace_id, provider=spec.provider, key=key, requester="secret.status"))
            except Exception:
                set_ = False
        armed = armed and set_
        fields.append({"env_key": key, "armed": set_})
    return {
        "slot_id": spec.id,
        "label": spec.label,
        "armed": armed,
        "env_refs": list(spec.env_keys),
        "fields": fields,
        "path": str(data_root() / "local-settings.json" if is_local() else secrets_env_path()),
    }


def slot_status(slot_id: str) -> dict[str, Any]:
    return _status_for(_spec_or_raise(slot_id))


def request_secret(
    slot_id: str,
    *,
    venture_id: str | None = None,
    workspace_id: str | None = None,
    reason: str = "",
    requested_by: str = "engine",
) -> dict[str, Any]:
    """Open a UI form. Returns schema + refs. Never accepts a value."""
    spec = _spec_or_raise(slot_id)
    workspace_id = _request_workspace(venture_id, workspace_id)
    status = _status_for(spec, workspace_id=workspace_id)
    if status["armed"]:
        return {
            "status": "armed",
            "request_id": None,
            "reason": reason or spec.why,
            "form": spec.form(),
            **status,
        }
    with _LOCK:
        rows = _load()
        for row in rows.values():
            if (
                row.get("slot_id") == spec.id
                and row.get("status") == "pending"
                and row.get("venture_id") == venture_id
                and row.get("workspace_id") == workspace_id
            ):
                existing = row
                break
        else:
            existing = None
        if existing is None:
            rid = new_id("ssr")
            existing = {
                "id": rid,
                "slot_id": spec.id,
                "status": "pending",
                "venture_id": venture_id,
                "workspace_id": workspace_id,
                "reason": reason or spec.why,
                "requested_by": requested_by,
                "env_refs": list(spec.env_keys),
                "created_at": utcnow().isoformat(),
            }
            rows[rid] = existing
            _save(rows)
    return {
        "status": "pending",
        "request_id": existing["id"],
        "reason": existing["reason"],
        "form": spec.form(),
        **status,
    }


def pending_requests(
    *, venture_id: str | None = None, workspace_id: str | None = None
) -> list[dict[str, Any]]:
    workspace_id = _request_workspace(venture_id, workspace_id)
    rows = _load()
    out: list[dict[str, Any]] = []
    for row in rows.values():
        if row.get("status") != "pending":
            continue
        if venture_id and row.get("venture_id") not in {None, "", venture_id}:
            continue
        if workspace_id and row.get("workspace_id") not in {None, "", workspace_id}:
            continue
        if parse_app_env() != "local":
            if not row.get("workspace_id") and not row.get("venture_id"):
                continue  # An unscoped platform request is never a founder request.
            try:
                row_workspace = _request_workspace(row.get("venture_id"), row.get("workspace_id"))
            except SecretSlotError:
                continue
            if row_workspace != workspace_id:
                continue
        spec = get_slot(str(row.get("slot_id") or ""))
        if spec is None:
            continue
        item = {
            "id": row["id"],
            "reason": row.get("reason") or spec.why,
            "requested_by": row.get("requested_by") or "engine",
            "venture_id": row.get("venture_id"),
            "workspace_id": row.get("workspace_id"),
            "form": spec.form(),
            **_status_for(spec, workspace_id=workspace_id),
        }
        out.append(item)
    return out


def save_secret(
    slot_id: str,
    values: dict[str, str],
    *,
    workspace_id: str | None = None,
) -> dict[str, Any]:
    """UI-only write. ``values`` maps env_key → secret. Response has fingerprints only."""
    workspace_id = _request_workspace(None, workspace_id)
    spec = _spec_or_raise(slot_id)
    if not values:
        raise SecretSlotError("missing_value", f"Open Keys and connections and enter your {spec.label} key, then save again.")
    if any(k in {n.lower() for n in values} for k in ("value", "token", "secret")) and len(values) == 1:
        # Single anonymous field — bind to the first env key.
        only = next(iter(values.values()))
        values = {spec.env_keys[0]: only}
    local = is_local() or is_local_env()
    if local and any(len(value) > 16384 for value in values.values() if value):
        raise SecretSlotError("invalid_value", f"Open Keys and connections and enter only your {spec.label} key, then save again.")
    if not local and not workspace_id:
        # Hosted: the process env / "machine" vault are platform-wide; founders write only to
        # their own workspace vault.
        raise SecretSlotError("workspace_required", "Hosted secrets must belong to a workspace.")
    fingerprints: dict[str, str] = {}
    existing = {field["env_key"]: field["armed"] for field in _status_for(spec, workspace_id=workspace_id)["fields"]} if local else {}
    for key in spec.env_keys:
        raw = (values.get(key) or "").strip()
        if not raw:
            if local and existing[key]:
                continue
            raise SecretSlotError("missing_value", f"Open Keys and connections and enter your {spec.label} key, then save again.")
        if local:
            try:
                upsert_env(key, raw)
            except (OSError, ValueError, RuntimeError, InvalidTag):
                raise SecretSlotError("local_key_save_failed", f"Your key could not be saved. Open Keys and connections, check your {spec.label} connection and data-folder permissions, then retry.") from None
            if not is_local():
                _put_vault(workspace_id, spec.provider, key, raw)
        else:
            from app.connectors.secrets import store

            store().put(workspace_id=str(workspace_id), provider=spec.provider, key=key, value=raw)
        fingerprints[key] = "set" if is_local() else fingerprint(raw)
    _fulfill(spec.id, workspace_id=workspace_id)
    _maybe_llm(spec, values, workspace_id)
    status = _status_for(spec, workspace_id=workspace_id)
    return {
        "status": "armed" if local else "stored",
        "fingerprints": fingerprints,
        **status,
    }


def _fulfill(slot_id: str, *, workspace_id: str | None = None) -> None:
    local = is_local() or parse_app_env() == "local"
    if not local:
        workspace_id = _request_workspace(None, workspace_id)
    with _LOCK:
        rows = _load()
        changed = False
        for row in rows.values():
            if row.get("slot_id") == slot_id and row.get("status") == "pending":
                if not local:
                    if not row.get("workspace_id") and not row.get("venture_id"):
                        continue
                    try:
                        row_workspace = _request_workspace(row.get("venture_id"), row.get("workspace_id"))
                    except SecretSlotError:
                        continue
                    if row_workspace != workspace_id:
                        continue
                row["status"] = "fulfilled"
                changed = True
        if changed:
            _save(rows)


def _put_vault(workspace_id: str | None, provider: str, key: str, value: str) -> None:
    try:
        from app.connectors.secrets import store

        store().put(
            workspace_id=workspace_id or "machine",
            provider=provider,
            key=key,
            value=value,
        )
    except Exception:
        return


def _maybe_llm(spec: SlotSpec, values: dict[str, str], workspace_id: str | None) -> None:
    if is_local() or spec.group != "llm" or not workspace_id:
        return
    raw = ""
    for key in spec.env_keys:
        raw = (values.get(key) or (os.environ.get(key) if is_local_env() else "") or "").strip()
        if raw:
            break
    if not raw:
        return
    try:
        from app.llm.founder_provider import set_founder_config

        resolved = None
        try:
            from app.llm.founder_provider import resolve_llm_config

            resolved = resolve_llm_config(workspace_id)
        except Exception:
            resolved = None
        set_founder_config(
            workspace_id=workspace_id,
            provider=spec.provider,
            model=(resolved.model if resolved else "default"),
            base_url=(resolved.base_url if resolved else None),
            api_key=raw,
        )
    except Exception:
        return


def public_catalog() -> list[dict[str, Any]]:
    from app.secrets.catalog import all_slots

    return [s.public() for s in all_slots()]


def reject_value_in_tool_args(args: dict[str, Any]) -> None:
    banned = {"value", "secret", "token", "api_key", "password", "key", "values"}
    for name in banned:
        if args.get(name):
            raise SecretSlotError(
                "value_not_allowed",
                "Tools cannot submit secret values. Open Keys and connections and save your key in its connection form, then retry.",
            )


def resolve_slot_id(slot_id: str | None, env_key: str | None, provider: str | None) -> str:
    if slot_id:
        spec = get_slot(slot_id) or slot_for_provider(slot_id)
        if spec:
            return spec.id
    if env_key and ENV_KEY_RE.match(env_key):
        spec = slot_for_env_key(env_key)
        if spec:
            return spec.id
        raise SecretSlotError(
            "unknown_slot",
            f"{env_key} is not a catalogued secret. Ask for a named slot.",
        )
    if provider:
        spec = slot_for_provider(provider)
        if spec:
            return spec.id
    raise SecretSlotError("unknown_slot", "Name a catalogued secret slot.")

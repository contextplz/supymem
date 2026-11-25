"""Self-hosted mode: one person runs Supymem on their own machine with their own keys.

Hosted production never sets SUPYMEM_MODE, so every caller keeps today's behaviour there.
"""
import json
import os


def is_local() -> bool:
    return os.environ.get("SUPYMEM_MODE", "").strip().lower() == "local"


def is_sample_mode() -> bool:
    if not is_local():
        return False
    from app.core.providers_local import _sample_selected
    from app.tenancy import current_or_none

    scope = current_or_none()
    if scope and scope.workspace_id not in {"", "none"}:
        return is_sample_workspace(scope.workspace_id)
    return _sample_selected()


def is_sample_workspace(workspace_id: str) -> bool:
    if not workspace_id or not is_local():
        return False
    from app.db.models import Workspace
    from app.state import db
    from fastapi import HTTPException

    with db()() as session:
        workspace = session.get(Workspace, workspace_id)
        if not workspace:
            return False
        try:
            values = json.loads(workspace.settings_json or "{}")
            if not isinstance(values, dict):
                raise ValueError
        except (ValueError, TypeError):
            raise HTTPException(503, "Your venture settings could not be read safely. Open Settings > Keys and connections > Your data folder and backup, restore your backup, then reopen your venture.") from None
        return values.get("sample") is True


def sample_view(view: dict, sample: bool) -> dict:
    if not is_local():
        return view
    view["sample"] = sample
    if sample and not view["name"].startswith("Sample: "):
        view["name"] = "Sample: " + view["name"]
    return view


def is_offline() -> bool:
    return _is_offline(process_wide=False)


def is_process_offline() -> bool:
    return _is_offline(process_wide=True)


def _is_offline(*, process_wide: bool) -> bool:
    if os.environ.get("LLM_CASSETTE_MODE", "").strip().lower() == "replay":
        return True
    if is_local():
        from app.core.providers_local import _sample_selected, configured_providers
        from app.core.local_secrets import read_settings

        sample = _sample_selected() if process_wide else is_sample_mode()
        if sample:
            return True
        providers = configured_providers()
        if providers["providers"].get(providers["provider"], {}).get("configured"):
            return False
        values = read_settings("runtime")["values"]
        if values.get("cassette_mode") == "replay":
            return True
        value = os.environ.get("LLM_OFFLINE", values.get("offline", "0"))
    else:
        value = os.environ.get("LLM_OFFLINE", "0")
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def is_test_mode() -> bool:
    if is_sample_mode():
        return True
    return is_local() and os.environ.get("EMAIL_PROVIDER") == "log" and is_offline()

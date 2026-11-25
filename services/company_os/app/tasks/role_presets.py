"""Venture-scoped focus and suggestion text, never authorization or actuation."""
import json
from typing import Literal

from fastapi import HTTPException

from app.db.models import Workspace

RolePreset = Literal["sales", "chief_of_staff", "product"]
DEFAULT_PRESET: RolePreset = "chief_of_staff"
PRESETS = {
    "sales": {
        "label": "Sales",
        "focus": "Focus on customer conversations, sales opportunities and follow-ups.",
        "suggestions": "Suggest the next customer conversation or sales follow-up for you to review.",
    },
    "chief_of_staff": {
        "label": "Chief of Staff",
        "focus": "Focus on your priorities, open decisions and work that needs your attention.",
        "suggestions": "Suggest the next priority or decision for you to review.",
    },
    "product": {
        "label": "Product",
        "focus": "Focus on user needs, product feedback and what to improve next.",
        "suggestions": "Suggest the next user question or product improvement for you to review.",
    },
}


def _workspace_preferences(session, venture_id, *, lock=False):
    from app.api.handoffs_api import _owner
    workspace, _ = _owner(session, venture_id)
    workspace = session.get(Workspace, workspace.id, with_for_update=lock, populate_existing=True)
    if lock:
        _owner(session, venture_id)
    try:
        settings = json.loads(workspace.settings_json or "{}")
        presets = settings.get("charlie_role_presets", {})
        if not isinstance(presets, dict):
            raise ValueError("Invalid role preferences")
    except (TypeError, ValueError, AttributeError) as exc:
        raise HTTPException(409, "Your saved role preferences could not be read. Refresh your settings.") from exc
    return workspace, settings, presets


def _view(preset):
    if not isinstance(preset, str) or preset not in PRESETS:
        raise HTTPException(409, "Your saved role could not be read. Refresh your settings.")
    return {"role_preset": preset, **PRESETS[preset]}


def read_role_preset(session, venture_id: str) -> dict[str, str]:
    """Chat callers use the current founder scope and receive focus text only."""
    _, _, presets = _workspace_preferences(session, venture_id)
    return _view(presets.get(venture_id, DEFAULT_PRESET))


def save_role_preset(session, venture_id: str, preset: RolePreset) -> dict[str, str]:
    if not isinstance(preset, str) or preset not in PRESETS:
        raise HTTPException(422, "Choose Sales, Chief of Staff or Product, then save your role.")
    workspace, settings, presets = _workspace_preferences(session, venture_id, lock=True)
    presets[venture_id] = preset
    settings["charlie_role_presets"] = presets
    workspace.settings_json = json.dumps(settings, sort_keys=True)
    return _view(preset)

"""Workspaces: the Phase 10 top-level container (doc 35)."""

from app.workspaces.resolve import (
    ensure_desk,
    local_person,
    workspace_for_venture,
    workspace_or_404,
)
from app.workspaces.service import (
    create_workspace,
    get_workspace,
    list_workspaces,
    rename_workspace,
    set_autonomy,
    set_observe_level,
)

__all__ = [
    "create_workspace",
    "ensure_desk",
    "get_workspace",
    "list_workspaces",
    "local_person",
    "rename_workspace",
    "set_autonomy",
    "set_observe_level",
    "workspace_for_venture",
    "workspace_or_404",
]

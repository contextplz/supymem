"""Canonical founder authorization shared by build requests and run rechecks."""
import json

from sqlalchemy import select

from app.db.models import Venture, Workspace, WorkspaceMember


def authorize(session, *, workspace_id: str, venture_id: str, actor: str, mutate: bool = False):
    venture = session.get(Venture, venture_id)
    workspace = session.get(Workspace, workspace_id)
    if not venture or venture.workspace_id != workspace_id or not workspace or workspace.archived_at:
        raise KeyError("build_scope_not_found")
    member = session.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace_id,
                                                         WorkspaceMember.person_id == actor))
    role = "owner" if workspace.owner_id == actor else member.role if member else None
    if role is None:
        raise KeyError("build_scope_not_found")
    if json.loads(workspace.settings_json or "{}").get("blocked"):
        raise PermissionError("workspace_blocked")
    if mutate and role not in {"owner", "admin", "maintainer"}:
        raise PermissionError("build_manage_apps_required")
    return venture

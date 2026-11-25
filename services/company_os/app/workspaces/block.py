"""Workspace block/unblock for admin (docs/supymem/17 §3). AWS purge is hosted-only."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from app.core.fsm import advance
from app.db.models import Person, Workspace
from app.db.supymem_models import FounderApp
from app.state import db
from core.clock import utcnow


def _settings(ws: Workspace) -> dict[str, Any]:
    try:
        return json.loads(ws.settings_json or "{}")
    except json.JSONDecodeError:
        return {}


def is_blocked(workspace_id: str) -> bool:
    factory = db()
    with factory() as session:
        ws = session.get(Workspace, workspace_id)
        if ws is None:
            return False
        return bool(_settings(ws).get("blocked"))


def set_blocked(workspace_id: str, blocked: bool, *, actor: str) -> dict[str, Any]:
    factory = db()
    now = utcnow()
    with factory() as session:
        ws = session.get(Workspace, workspace_id)
        if ws is None:
            raise KeyError("workspace")
        settings = _settings(ws)
        settings["blocked"] = blocked
        settings["blocked_at"] = now.isoformat() if blocked else None
        settings["blocked_by"] = actor if blocked else None
        ws.settings_json = json.dumps(settings, sort_keys=True)
        ws.updated_at = now
        if blocked:
            apps = list(
                session.scalars(
                    select(FounderApp).where(
                        FounderApp.workspace_id == workspace_id,
                        FounderApp.deleted_at.is_(None),
                        FounderApp.state == "live",
                    )
                )
            )
            for app in apps:
                advance(session, app, "suspended", actor=actor, reason="workspace_blocked")
        session.commit()
        return {"workspace_id": workspace_id, "blocked": blocked}


def request_delete(workspace_id: str, *, actor: str) -> dict[str, Any]:
    factory = db()
    now = utcnow()
    with factory() as session:
        ws = session.get(Workspace, workspace_id)
        if ws is None:
            raise KeyError("workspace")
        settings = _settings(ws)
        pending = settings.get("delete_pending_actor")
        if not pending:
            settings["delete_pending_actor"] = actor
            ws.settings_json = json.dumps(settings, sort_keys=True)
            ws.updated_at = now
            session.commit()
            return {"workspace_id": workspace_id, "state": "awaiting_second_admin"}
        if pending == actor:
            raise PermissionError("dual_control")
        settings["delete_grace_until"] = (now + timedelta(days=7)).isoformat()
        settings["delete_confirmed_by"] = actor
        ws.settings_json = json.dumps(settings, sort_keys=True)
        ws.updated_at = now
        session.commit()
        return {
            "workspace_id": workspace_id,
            "state": "grace",
            "purge": "deferred_hosted",
        }


def search_founders(*, email: str | None = None, workspace_id: str | None = None) -> list[dict[str, Any]]:
    factory = db()
    with factory() as session:
        q = select(Workspace)
        if workspace_id:
            q = q.where(Workspace.id == workspace_id)
        rows = list(session.scalars(q.limit(50)))
        out: list[dict[str, Any]] = []
        for ws in rows:
            owner = session.get(Person, ws.owner_id)
            owner_email = (owner.email or "") if owner else ""
            if email and email.lower() not in owner_email.lower() and owner_email.lower() != email.lower():
                continue
            settings = _settings(ws)
            out.append(
                {
                    "workspace_id": ws.id,
                    "name": ws.name,
                    "owner_id": ws.owner_id,
                    "email": owner_email,
                    "blocked": bool(settings.get("blocked")),
                    "delete_grace_until": settings.get("delete_grace_until"),
                }
            )
        return out

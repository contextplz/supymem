"""Hosted-tenant helpers: tenancy, AUP, DPA (doc 130 OT-32 / OT-36)."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from core.clock import utcnow

from app.db.models import GtmEngineState, Workspace
from app.gtm.relationships.service import OutreachError, _settings
from app.state import db

HOSTED_TENANCIES = frozenset({"managed", "self_hosted"})


def workspace_tenancy(workspace_id: str) -> str:
    with db()() as session:
        row = session.get(Workspace, workspace_id)
        if row is None:
            return "local"
        return (row.tenancy or "local").strip().lower()


def is_hosted(workspace_id: str) -> bool:
    return workspace_tenancy(workspace_id) in HOSTED_TENANCIES


def assert_aup_accepted(workspace_id: str) -> None:
    """Hosted ingest/send requires AUP. Local desk skips."""
    if not is_hosted(workspace_id):
        return
    with db()() as session:
        settings = _settings(session, workspace_id)
        if settings.aup_accepted_at is None:
            raise OutreachError("aup_required", 403)


def accept_aup(workspace_id: str) -> dict[str, Any]:
    with db()() as session:
        settings = _settings(session, workspace_id)
        now = utcnow()
        settings.aup_accepted_at = now
        settings.updated_at = now
        session.commit()
        return {"aup_accepted_at": now.isoformat()}


def assert_dpa_accepted(workspace_id: str) -> None:
    """Hosted processing of prospect PII / send_enabled requires DPA click-wrap."""
    if not is_hosted(workspace_id):
        return
    with db()() as session:
        settings = _settings(session, workspace_id)
        if getattr(settings, "dpa_accepted_at", None) is None:
            raise OutreachError("dpa_required", 403)


def accept_dpa(workspace_id: str) -> dict[str, Any]:
    with db()() as session:
        settings = _settings(session, workspace_id)
        now = utcnow()
        settings.dpa_accepted_at = now
        settings.updated_at = now
        session.commit()
        return {"dpa_accepted_at": now.isoformat()}


def set_send_enabled(workspace_id: str, enabled: bool) -> dict[str, Any]:
    if enabled:
        assert_dpa_accepted(workspace_id)
    with db()() as session:
        from app.db.session import serialize_writes
        serialize_writes(session, key=f"gtm-sending:{workspace_id}")
        settings = _settings(session, workspace_id)
        now = utcnow()
        if enabled and not settings.send_enabled:
            try:
                status = json.loads(settings.onboarding_status or "{}")
            except (ValueError, TypeError):
                status = {}
            status = status if isinstance(status, dict) else {}
            status["send_resumed_at"] = now.isoformat()
            status.pop("send_pause_reason", None)
            settings.onboarding_status = json.dumps(status)
            engine = session.get(GtmEngineState, workspace_id)
            if engine is not None:
                engine.send_enabled = 1
                engine.updated_at = now
        settings.send_enabled = 1 if enabled else 0
        settings.updated_at = now
        session.commit()
        return {"send_enabled": bool(settings.send_enabled)}


def dpa_accepted_at(workspace_id: str) -> datetime | None:
    with db()() as session:
        settings = _settings(session, workspace_id)
        return getattr(settings, "dpa_accepted_at", None)

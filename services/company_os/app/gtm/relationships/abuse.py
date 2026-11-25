"""Per-tenant cadence caps and send kill-switch (doc 130 OT-34 / OT-38).

Copy caps are counted in ``cadence.py`` (OT-44). This module owns pause/resume
and calls that counter so the 5/2/5 rule is not duplicated.
"""

from __future__ import annotations

import json
from typing import Any

from core.clock import utcnow
from sqlalchemy import func, select

from app.db.models import GtmContact, GtmEngineState, GtmOutreachSettings, GtmTouch
from app.gtm.relationships.cadence import remaining_caps
from app.gtm.relationships.hosted import assert_dpa_accepted, is_hosted
from app.gtm.relationships.service import OutreachError, _settings
from app.state import db


def pause_sends(workspace_id: str, *, reason: str = "admin_pause") -> dict[str, Any]:
    """Kill switch: settings + engine send_enabled=0 (Composio execute stays off)."""
    now = utcnow()
    with db()() as session:
        from app.db.session import serialize_writes
        serialize_writes(session, key=f"gtm-sending:{workspace_id}")
        settings = _settings(session, workspace_id)
        if reason == "bounce_rate":
            counts = bounce_counts(session, workspace_id)
            if counts["sent"] < 10 or counts["bounced"] / counts["sent"] <= 0.05:
                return {"send_enabled": bool(settings.send_enabled), "reason": None}
        settings.send_enabled = 0
        try:
            status = json.loads(settings.onboarding_status or "{}")
        except (ValueError, TypeError):
            status = {}
        status = status if isinstance(status, dict) else {}
        status["send_pause_reason"] = reason
        settings.onboarding_status = json.dumps(status)
        settings.updated_at = now
        engine = session.get(GtmEngineState, workspace_id)
        if engine is None:
            engine = GtmEngineState(
                workspace_id=workspace_id,
                discovery_enabled=0,
                send_enabled=0,
                updated_at=now,
            )
            session.add(engine)
        else:
            engine.send_enabled = 0
            engine.updated_at = now
        session.commit()
    return {"send_enabled": False, "reason": reason}


def resume_sends(workspace_id: str) -> dict[str, Any]:
    from app.gtm.relationships.hosted import set_send_enabled

    return set_send_enabled(workspace_id, True)


def bounce_counts(session, workspace_id: str) -> dict[str, int | str | None]:
    from app.gtm.journey import _json, _signal_date
    settings = session.get(GtmOutreachSettings, workspace_id)
    resumed = _signal_date(_json(settings.onboarding_status).get("send_resumed_at")) if settings else None
    sends = select(func.count()).select_from(GtmTouch).where(GtmTouch.workspace_id == workspace_id,
        GtmTouch.direction == "out", GtmTouch.channel == "email", GtmTouch.simulated == 0, GtmTouch.sent_at.is_not(None))
    bounces = select(func.count()).select_from(GtmContact).where(GtmContact.workspace_id == workspace_id,
        GtmContact.bounced_at.is_not(None), GtmContact.times_emailed > 0)
    if resumed:
        sends = sends.where(GtmTouch.sent_at > resumed)
        bounces = bounces.where(GtmContact.bounced_at > resumed)
    return {"sent": session.scalar(sends) or 0, "bounced": session.scalar(bounces) or 0,
        "send_resumed_at": resumed.isoformat() if resumed else None}


def assert_send_allowed(workspace_id: str) -> None:
    """Relationships copy/sent. Engine send_enabled defaults to 0 and is not this gate."""
    if is_hosted(workspace_id):
        assert_dpa_accepted(workspace_id)
    with db()() as session:
        settings = session.get(GtmOutreachSettings, workspace_id)
        if settings is not None and int(settings.send_enabled or 0) == 0:
            raise OutreachError("send_paused", 403)


def assert_cadence(workspace_id: str, *, kind: str = "connect") -> None:
    """6th connect copy (or matching email/dm cap) → 429. Caps live in cadence.py."""
    caps = remaining_caps(workspace_id)
    if kind in {"dm", "linkedin_dm"}:
        bucket = "dm"
    elif kind in {"email", "gmail_composio"}:
        bucket = "email"
    else:
        bucket = "connect"
    if caps["remaining"].get(bucket, 0) <= 0:
        raise OutreachError("cadence_cap", 429)


def assert_copy_or_send(workspace_id: str, *, kind: str = "connect") -> None:
    assert_send_allowed(workspace_id)
    assert_cadence(workspace_id, kind=kind)

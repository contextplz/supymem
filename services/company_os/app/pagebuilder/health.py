"""Durable renderer readiness; stale workers never promise a working preview."""
import json
from datetime import timedelta, timezone
from sqlalchemy import select
from core.clock import utcnow
from app.audit.writer import audit
from app.db.models import EventLog
from app.state import db

TOPIC = "pagebuilder.renderer_health"


def current():
    from app.core.local_mode import is_local

    if is_local():
        return {"ready": True, "reason": None, "checked_at": None, "preview_method": "local_html"}
    with db()() as session:
        row = session.scalar(select(EventLog).where(EventLog.topic == TOPIC).order_by(EventLog.seq.desc()).limit(1))
        if row is None:
            return {"ready": False, "reason": "warming", "checked_at": None}
        at = row.ts if row.ts.tzinfo else row.ts.replace(tzinfo=timezone.utc)
        value = json.loads(row.payload_json)
        fresh = timedelta(0) <= utcnow() - at <= timedelta(minutes=12)
        return {"ready": fresh and value.get("ready") is True,
                "reason": value.get("reason", "warming") if fresh else "warming", "checked_at": at.isoformat()}


def record(ready, reason=None):
    previous = current()
    reason = None if ready else "pagebuilder_renderer_unavailable"
    audit(TOPIC, actor_id="pagebuilder_worker", actor_kind="system", target_ref="pagebuilder_renderer",
          outcome="ok" if ready else "error", payload={"ready": bool(ready), "reason": reason})
    if not ready and (previous["ready"] or previous["reason"] != reason):
        from app.error_reports.service import record as alert
        alert(source="worker", feature="pagebuilder", code=reason, place="pagebuilder.renderer")


def probe():
    from app.pagebuilder.rendering import require_renderer
    try:
        require_renderer()
    except ValueError:
        record(False)
    else:
        record(True)

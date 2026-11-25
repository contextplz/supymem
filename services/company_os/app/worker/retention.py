"""Daily retention purge from seeded ``retention_policies``."""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import delete, select, text

from app.db.session import make_engine, make_session_factory
from app.state import db
from core.clock import utcnow

log = logging.getLogger("app.worker.retention")

_SAFE_TABLES = frozenset(
    {
        "build_events",
        "agent_runtime_steps",
        "site_visits",
        "app_usage_records",
        "security_events",
    }
)


def purge_expired() -> int:
    try:
        factory = db()
    except Exception:
        engine = make_engine()
        factory = make_session_factory(engine)
    now = utcnow()
    removed = 0
    with factory() as session:
        try:
            from app.db.supymem_models import RetentionPolicy
        except Exception:
            return 0
        rows = list(session.scalars(select(RetentionPolicy)))
        for policy in rows:
            if policy.action != "delete" or policy.table_name not in _SAFE_TABLES:
                continue
            cutoff = now - timedelta(days=int(policy.days))
            ts_col = "created_at"
            if policy.table_name in {"security_events", "site_visits", "app_usage_records"}:
                ts_col = "ts" if policy.table_name != "app_usage_records" else "window_start"
            if policy.table_name == "security_events":
                ts_col = "ts"
            if policy.table_name == "site_visits":
                ts_col = "ts"
            try:
                result = session.execute(
                    text(f"DELETE FROM {policy.table_name} WHERE {ts_col} < :cutoff"),
                    {"cutoff": cutoff},
                )
                removed += result.rowcount or 0
            except Exception:
                log.exception("retention purge failed for %s", policy.table_name)
        session.commit()
    return removed

"""Atomic planned-task checkout.

Ported from Paperclip ``server/src/services/issues.ts`` ``checkout``
(UPDATE … SET status=in_progress, checkoutRunId=… WHERE assignee + checkoutRunId
is null or same-run). Company OS has one execution lock per venture, so the
driver claim also requires that no other row is already ``running``.

MIT: Paperclip AI. Do not run vendor/paperclip in-process.
"""

from __future__ import annotations

from sqlalchemy import select, update

from app.db.models import PlannedTask
from app.state import db
from core.clock import utcnow


def claim_task(venture_id: str, task_id: str) -> dict | None:
    """Compare-and-swap one open runnable task to ``running``.

    Returns the serialized row on success, or ``None`` if another worker
    already claimed it (or it is not open/runnable).
    """
    factory = db()
    now = utcnow()
    with factory() as session:
        result = session.execute(
            update(PlannedTask)
            .where(
                PlannedTask.id == task_id,
                PlannedTask.venture_id == venture_id,
                PlannedTask.status == "open",
                PlannedTask.runnable == 1,
            )
            .values(status="running", updated_at=now)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            session.rollback()
            return None
        row = session.get(PlannedTask, task_id)
        session.commit()
        from app.planning.store import _serialize_task

        return _serialize_task(row) if row else None


def claim_next_open_task(venture_id: str) -> dict | None:
    """Claim the first open runnable task if this venture has nothing running.

    The ``NOT EXISTS running`` predicate is in the same UPDATE as the status
    flip so two autopilot ticks cannot both start work.
    """
    factory = db()
    now = utcnow()
    with factory() as session:
        running_exists = (
            select(PlannedTask.id)
            .where(
                PlannedTask.venture_id == venture_id,
                PlannedTask.status == "running",
            )
            .exists()
        )
        first_open = (
            select(PlannedTask.id)
            .where(
                PlannedTask.venture_id == venture_id,
                PlannedTask.status == "open",
                PlannedTask.runnable == 1,
                ~running_exists,
            )
            .order_by(PlannedTask.rank.asc(), PlannedTask.updated_at.desc())
            .limit(1)
            .scalar_subquery()
        )
        result = session.execute(
            update(PlannedTask)
            .where(PlannedTask.id == first_open)
            .values(status="running", updated_at=now)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            session.rollback()
            return None
        row = session.scalars(
            select(PlannedTask).where(
                PlannedTask.venture_id == venture_id,
                PlannedTask.status == "running",
            )
        ).first()
        session.commit()
        from app.planning.store import _serialize_task

        return _serialize_task(row) if row else None

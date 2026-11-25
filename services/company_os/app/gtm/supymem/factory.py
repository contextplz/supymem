"""Per-workspace Run/Stop, buffers, and volume ramp."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, select

from app.db.models import GtmContact, GtmEngineState
from app.state import db
from core.clock import as_utc, utcnow

log = logging.getLogger(__name__)

EMAIL_MONTHLY = (200, 400, 700, 1000, 1300, 1600, 1800, 2000)
LINKEDIN_MONTHLY = (50, 100, 180, 250, 350, 400, 450, 500)
DISCOVERY_COOLDOWN_SEC = 15 * 60


def _week_cap(seq: tuple[int, ...], week_index: int) -> int:
    i = max(1, min(int(week_index or 1), len(seq))) - 1
    return seq[i]


def daily_email_cap(week_index: int) -> int:
    return max(4, round(_week_cap(EMAIL_MONTHLY, week_index) / 30))


def daily_linkedin_cap(week_index: int) -> int:
    return max(2, round(_week_cap(LINKEDIN_MONTHLY, week_index) / 30))


def get_or_create_state(workspace_id: str) -> dict[str, Any]:
    with db()() as session:
        row = session.get(GtmEngineState, workspace_id)
        if row is None:
            row = GtmEngineState(
                workspace_id=workspace_id,
                discovery_enabled=0,
                send_enabled=0,
                icp_query="B2B SaaS founders",
                find_batch_size=25,
                ready_email_buffer=50,
                ready_linkedin_buffer=20,
                week_index=1,
                updated_at=utcnow(),
            )
            session.add(row)
            session.commit()
        return serialize_state(row)


def serialize_state(row: GtmEngineState) -> dict[str, Any]:
    return {
        "workspace_id": row.workspace_id,
        "discovery_enabled": bool(row.discovery_enabled),
        "send_enabled": bool(row.send_enabled),
        "icp_query": row.icp_query,
        "find_batch_size": row.find_batch_size,
        "ready_email_buffer": row.ready_email_buffer,
        "ready_linkedin_buffer": row.ready_linkedin_buffer,
        "week_index": row.week_index,
        "email_monthly_target": _week_cap(EMAIL_MONTHLY, row.week_index),
        "linkedin_monthly_target": _week_cap(LINKEDIN_MONTHLY, row.week_index),
        "email_daily_cap": daily_email_cap(row.week_index),
        "linkedin_daily_cap": daily_linkedin_cap(row.week_index),
        "last_discovery_at": row.last_discovery_at.isoformat() if row.last_discovery_at else None,
        "last_learn_at": row.last_learn_at.isoformat() if row.last_learn_at else None,
    }


def set_run(
    workspace_id: str,
    *,
    discovery: bool | None = None,
    send: bool | None = None,
    icp_query: str | None = None,
    week_index: int | None = None,
    find_batch_size: int | None = None,
) -> dict[str, Any]:
    get_or_create_state(workspace_id)
    with db()() as session:
        row = session.get(GtmEngineState, workspace_id)
        assert row is not None
        if discovery is not None:
            row.discovery_enabled = 1 if discovery else 0
        if send is not None:
            if send:
                from app.gtm.relationships.hosted import assert_dpa_accepted

                assert_dpa_accepted(workspace_id)
            row.send_enabled = 1 if send else 0
        if icp_query:
            row.icp_query = icp_query.strip()[:200]
        if week_index is not None:
            row.week_index = max(1, min(int(week_index), 8))
        if find_batch_size is not None:
            row.find_batch_size = max(5, min(int(find_batch_size), 80))
        row.updated_at = utcnow()
        session.commit()
        return serialize_state(row)


def funnel(workspace_id: str) -> dict[str, Any]:
    with db()() as session:
        rows = list(
            session.execute(
                select(GtmContact.relationship_stage, func.count())
                .where(GtmContact.workspace_id == workspace_id)
                .group_by(GtmContact.relationship_stage)
            )
        )
        counts = {str(stage): int(n) for stage, n in rows}
        email_ready = session.scalar(
            select(func.count())
            .select_from(GtmContact)
            .where(
                GtmContact.workspace_id == workspace_id,
                GtmContact.do_not_contact == 0,
                GtmContact.email.isnot(None),
                GtmContact.email_confidence.in_(("safe", "risky")),
                GtmContact.replied_at.is_(None),
                GtmContact.bounced_at.is_(None),
                GtmContact.times_emailed == 0,
            )
        ) or 0
        li_ready = session.scalar(
            select(func.count())
            .select_from(GtmContact)
            .where(
                GtmContact.workspace_id == workspace_id,
                GtmContact.do_not_contact == 0,
                GtmContact.linkedin_url.isnot(None),
                GtmContact.replied_at.is_(None),
                GtmContact.times_linkedin == 0,
            )
        ) or 0
        emailed = session.scalar(
            select(func.count())
            .select_from(GtmContact)
            .where(GtmContact.workspace_id == workspace_id, GtmContact.times_emailed > 0)
        ) or 0
        replied = session.scalar(
            select(func.count())
            .select_from(GtmContact)
            .where(GtmContact.workspace_id == workspace_id, GtmContact.replied_at.isnot(None))
        ) or 0
    return {
        "by_stage": counts,
        "email_ready": int(email_ready),
        "linkedin_ready": int(li_ready),
        "emailed": int(emailed),
        "replied": int(replied),
    }


def discovery_needed(workspace_id: str) -> bool:
    state = get_or_create_state(workspace_id)
    if not state["discovery_enabled"]:
        return False
    with db()() as session:
        row = session.get(GtmEngineState, workspace_id)
        if row and row.last_discovery_at:
            last = as_utc(row.last_discovery_at)
            age = (utcnow() - last).total_seconds() if last else DISCOVERY_COOLDOWN_SEC
            if age < DISCOVERY_COOLDOWN_SEC:
                return False
    fun = funnel(workspace_id)
    return (
        fun["email_ready"] < state["ready_email_buffer"]
        or fun["linkedin_ready"] < state["ready_linkedin_buffer"]
    )


def mark_discovered(workspace_id: str) -> None:
    with db()() as session:
        row = session.get(GtmEngineState, workspace_id)
        if row is None:
            return
        row.last_discovery_at = utcnow()
        row.updated_at = utcnow()
        session.commit()


def apply_mailbox_ramp(workspace_id: str) -> int:
    """Set Gmail daily_send_limit from the current week ramp (min 8, max 40)."""
    from app.db.models import GtmEmailAccount
    from app.connections import mailboxes
    if mailboxes.enabled():
        with db()() as session:
            caps = []
            for account in session.scalars(select(GtmEmailAccount).where(GtmEmailAccount.workspace_id == workspace_id)):
                first_connected = session.scalar(select(func.min(GtmEmailAccount.created_at)).where(
                    func.lower(func.trim(GtmEmailAccount.email)) == (account.email or "").strip().casefold()))
                age = max(0, (utcnow() - as_utc(first_connected)).days)
                account.daily_send_limit = min(40, daily_email_cap(age // 7 + 1))
                account.updated_at = utcnow()
                caps.append(account.daily_send_limit)
            session.commit()
            return max(caps, default=0)

    cap = min(40, max(8, daily_email_cap(get_or_create_state(workspace_id)["week_index"])))
    with db()() as session:
        for acc in session.scalars(
            select(GtmEmailAccount).where(GtmEmailAccount.workspace_id == workspace_id)
        ):
            acc.daily_send_limit = cap
            acc.updated_at = utcnow()
        session.commit()
    return cap

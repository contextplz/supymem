"""Third time is a workflow (36 §8).

Proposed once per fingerprint, ever. Nagging is worse than missing the
automation, so the audit log — which is append-only — is the record of whether
we already asked, rather than a mutable flag someone could reset.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from app.db.models import Engagement, EventLog
from app.planning.dedupe import fingerprint_title
from app.state import db
from core.clock import utcnow

WINDOW_DAYS = 30
PROPOSE_AT = 3

PROPOSED_TOPIC = "engagement.workflow_proposed"
DECLINED_TOPIC = "engagement.workflow_declined"

_FILLER = re.compile(
    r"\b(?:please|could you|can you|for me|again|today|this week|the|a|an)\b"
)


def fingerprint(request_verbatim: str, target_ref: str | None = None) -> str:
    """Normalise the way doc 32 does, then fold in the target."""
    body = _FILLER.sub(" ", request_verbatim.lower())
    body = re.sub(r"[^a-z0-9 ]+", " ", body)
    return fingerprint_title(f"{body} :: {target_ref or ''}")


@dataclass(frozen=True)
class RecurrenceVerdict:
    fingerprint: str
    occurrences: int
    should_propose: bool
    reason: str = ""
    title: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "occurrences": self.occurrences,
            "should_propose": self.should_propose,
            "reason": self.reason,
            "title": self.title,
        }


def _already_asked(session: Any, workspace_id: str, fp: str) -> bool:
    row = session.scalars(
        select(EventLog).where(
            EventLog.workspace_id == workspace_id,
            EventLog.topic.in_((PROPOSED_TOPIC, DECLINED_TOPIC)),
            EventLog.target_ref == fp,
        )
    ).first()
    return row is not None


def check(
    workspace_id: str,
    *,
    requested_by: str,
    fingerprint_value: str,
    now: Any = None,
) -> RecurrenceVerdict:
    """Count near-identical requests by the same person inside the window."""
    moment = now or utcnow()
    since = moment - timedelta(days=WINDOW_DAYS)
    factory = db()
    with factory() as session:
        rows = session.scalars(
            select(Engagement).where(
                Engagement.workspace_id == workspace_id,
                Engagement.requested_by == requested_by,
                Engagement.fingerprint == fingerprint_value,
                Engagement.created_at >= since,
            )
        ).all()
        count = len(rows)
        title = rows[-1].title if rows else ""
        asked = _already_asked(session, workspace_id, fingerprint_value)

    if asked:
        return RecurrenceVerdict(
            fingerprint_value,
            count,
            False,
            reason="already asked about this one once",
            title=title,
        )
    if count < PROPOSE_AT:
        return RecurrenceVerdict(
            fingerprint_value,
            count,
            False,
            reason=f"seen {count} time(s) in {WINDOW_DAYS} days",
            title=title,
        )
    return RecurrenceVerdict(
        fingerprint_value,
        count,
        True,
        reason=f"the {count}rd time in {WINDOW_DAYS} days",
        title=title,
    )


def record_proposal(
    workspace_id: str, *, fingerprint_value: str, requested_by: str, title: str
) -> None:
    from app.audit.writer import audit

    audit(
        PROPOSED_TOPIC,
        workspace_id=workspace_id,
        actor_id=requested_by,
        actor_kind="person",
        surface="internal",
        target_ref=fingerprint_value,
        payload={"title": title},
    )


def record_decline(
    workspace_id: str, *, fingerprint_value: str, requested_by: str
) -> None:
    """A decline is permanent: never ask about this fingerprint again."""
    from app.audit.writer import audit

    audit(
        DECLINED_TOPIC,
        workspace_id=workspace_id,
        actor_id=requested_by,
        actor_kind="person",
        surface="api",
        target_ref=fingerprint_value,
        payload={},
    )


def was_declined(workspace_id: str, fingerprint_value: str) -> bool:
    factory = db()
    with factory() as session:
        row = session.scalars(
            select(EventLog).where(
                EventLog.workspace_id == workspace_id,
                EventLog.topic == DECLINED_TOPIC,
                EventLog.target_ref == fingerprint_value,
            )
        ).first()
        return row is not None

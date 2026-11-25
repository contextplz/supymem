"""Outreach funnel rates with real numerators and denominators (OT-23)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Literal

from core.clock import utcnow
from sqlalchemy import func, select

from app.db.models import GtmContact, GtmOutcome, GtmTouch
from app.state import db

DMeaning = Literal["of_sent", "of_accepted", "of_meetings", "count"]

OUTCOME_KINDS = {
    "identified": "prospect_identified",
    "accepted": "connection_accepted",
    "replied": "reply_after_accept",
    "positive_reply": "positive_reply",
    "meeting": "meeting_booked",
    "qualified_pain": "qualified_pain",
    "second_meeting": "second_meeting",
    "pilot": "pilot_started",
    "converted": "pilot_converted",
    "referral": "referral_received",
    "opt_out": "opt_out",
    "unknown_person": "unknown_person_report",
}


def _rate(n: int, d: int) -> float | None:
    if d <= 0:
        return None
    return round(n / d, 4)


def _stage(n: int, d: int, d_meaning: DMeaning) -> dict[str, Any]:
    return {"n": int(n), "d": int(d), "d_meaning": d_meaning, "rate": _rate(n, d)}


def _count_outcomes(session: Any, workspace_id: str, kind: str, since: Any) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(GtmOutcome)
            .where(
                GtmOutcome.workspace_id == workspace_id,
                GtmOutcome.kind == kind,
                GtmOutcome.occurred_at >= since,
            )
        )
        or 0
    )


def funnel_metrics(workspace_id: str, *, window_days: int = 30) -> dict[str, Any]:
    since = utcnow() - timedelta(days=max(1, int(window_days)))
    with db()() as session:
        identified = int(
            session.scalar(
                select(func.count())
                .select_from(GtmContact)
                .where(
                    GtmContact.workspace_id == workspace_id,
                    GtmContact.created_at >= since,
                )
            )
            or 0
        )
        identified_events = _count_outcomes(
            session, workspace_id, OUTCOME_KINDS["identified"], since
        )
        identified = max(identified, identified_events)
        sent = int(
            session.scalar(
                select(func.count())
                .select_from(GtmTouch)
                .where(
                    GtmTouch.workspace_id == workspace_id,
                    GtmTouch.sent_at.is_not(None),
                    GtmTouch.sent_at >= since,
                )
            )
            or 0
        )
        accepted = _count_outcomes(
            session, workspace_id, OUTCOME_KINDS["accepted"], since
        )
        replied = _count_outcomes(session, workspace_id, OUTCOME_KINDS["replied"], since)
        meetings = _count_outcomes(
            session, workspace_id, OUTCOME_KINDS["meeting"], since
        )
        qualified = _count_outcomes(
            session, workspace_id, OUTCOME_KINDS["qualified_pain"], since
        )
        second = _count_outcomes(
            session, workspace_id, OUTCOME_KINDS["second_meeting"], since
        )
        pilots = _count_outcomes(session, workspace_id, OUTCOME_KINDS["pilot"], since)
        converted = _count_outcomes(
            session, workspace_id, OUTCOME_KINDS["converted"], since
        )
        referrals = _count_outcomes(
            session, workspace_id, OUTCOME_KINDS["referral"], since
        )
        opt_outs = _count_outcomes(session, workspace_id, OUTCOME_KINDS["opt_out"], since)
        unknown = _count_outcomes(
            session, workspace_id, OUTCOME_KINDS["unknown_person"], since
        )
        positive = _count_outcomes(
            session, workspace_id, OUTCOME_KINDS["positive_reply"], since
        )
        with_quote = int(
            session.scalar(
                select(func.count())
                .select_from(GtmTouch)
                .where(
                    GtmTouch.workspace_id == workspace_id,
                    GtmTouch.sent_at.is_not(None),
                    GtmTouch.sent_at >= since,
                    GtmTouch.body_text.is_not(None),
                    GtmTouch.body_text != "",
                )
            )
            or 0
        )
        attached = int(
            session.scalar(
                select(func.count())
                .select_from(GtmContact)
                .where(
                    GtmContact.workspace_id == workspace_id,
                    GtmContact.sku_key.is_not(None),
                    GtmContact.created_at >= since,
                )
            )
            or 0
        )

    stages = {
        "identified": _stage(identified, identified, "count"),
        "sent": _stage(sent, identified if identified else sent, "count"),
        "accepted": _stage(accepted, sent, "of_sent"),
        "replied": _stage(replied, accepted, "of_accepted"),
        "meeting": _stage(meetings, accepted, "of_accepted"),
        "qualified_pain": _stage(qualified, meetings, "of_meetings"),
        "second_meeting": _stage(second, meetings, "of_meetings"),
        "pilot": _stage(pilots, meetings if meetings else pilots, "of_meetings"),
        "converted": _stage(converted, pilots, "count"),
        "referral": _stage(referrals, converted if converted else referrals, "count"),
    }
    leading = {
        "sends_with_quote_or_trigger": _stage(with_quote, sent, "of_sent"),
        "accept_rate_rolling_7d": stages["accepted"],
        "positive_reply_rate": _stage(positive, accepted, "of_accepted"),
        "qualified_pain_rate_call1": _stage(qualified, meetings, "of_meetings"),
        "founder_hours_per_qualified_meeting": {
            "n": None,
            "d": meetings,
            "d_meaning": "of_meetings",
            "rate": None,
        },
        "artifact_attach_rate": _stage(attached, identified, "count"),
        "opt_outs": _stage(opt_outs, sent, "of_sent"),
        "unknown_person_reports": _stage(unknown, sent, "of_sent"),
        "time_to_first_substantive_reply_hours": {
            "n": None,
            "d": replied,
            "d_meaning": "of_accepted",
            "rate": None,
        },
    }
    return {
        "window_days": int(window_days),
        "stages": stages,
        "leading": leading,
    }


__all__ = ["funnel_metrics"]

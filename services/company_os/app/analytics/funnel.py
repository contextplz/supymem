"""Beta funnel: one ordered step list, the shared emit helpers and the per-founder read.

Every step is either a closed ``analytics_events`` name or a durable domain table
(``kind="table"``) whose rows are already the canonical record. Event names here are
merged into ``events.ALLOWED`` so the funnel and the write allow-list cannot drift
(see tests/unit/analytics/test_beta_funnel.py). Closed labels only: no text, emails or secrets.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

log = logging.getLogger("app.analytics")

# (key, label, kind, source). kind "event": tuple of analytics event names;
# kind "table": a key handled in ``_table_counts``.
FUNNEL: tuple[tuple[str, str, str, Any], ...] = (
    ("invite_redeemed", "Invite code redeemed", "event", ("invite.redeemed",)),
    ("signup", "Signed up (admitted)", "event", ("signup",)),
    ("first_launch", "App launched, signed in", "event", ("session.active",)),
    ("venture_created", "Venture created", "event", ("venture.created",)),
    ("venture_limit_reached", "Venture limit hit", "event", ("venture.limit_reached",)),
    ("clarification_answered", "Clarification answered", "table", "clarification_answers"),
    ("research_started", "Research started", "event", ("research.started",)),
    ("research_completed", "Research completed", "event", ("research.completed",)),
    ("research_failed", "Research failed", "event", ("research.failed",)),
    ("concept_draft", "Concept landing page drafted", "event", ("site.draft_generated",)),
    ("waitlist_enabled", "Waitlist enabled", "event", ("waitlist.enabled",)),
    ("waitlist_disabled", "Waitlist closed", "event", ("waitlist.disabled",)),
    ("waitlist_tested", "Waitlist tested", "event", ("waitlist.tested", "waitlist.test_failed")),
    ("page_published", "Page published", "event", ("site.published",)),
    ("page_unpublished", "Page unpublished", "event", ("site.unpublished",)),
    ("page_publish_failed", "Publish or takedown failed", "event", ("site.publish_failed", "site.unpublish_failed")),
    ("public_page_views", "Public page views", "table", "page_views"),
    ("real_waitlist_signups", "Real waitlist signups (no tests)", "table", "waitlist_signups"),
    ("chat_message", "Chat message sent", "event", ("chat.message",)),
    ("memory_remember", "Memory remembered", "event", ("chat.tool_invoked:memory.remember",)),
    ("memory_forget", "Memory forgotten", "event", ("chat.tool_invoked:memory.forget",)),
    ("new_chat", "New Chat", "event", ("chat.new_conversation",)),
    ("gmail_connected", "Gmail connected", "event", ("gmail.connected",)),
    ("outreach_drafts", "Outreach drafts created", "event", ("outreach.drafts_created",)),
    ("batch_approved", "Outreach batch approved", "event", ("outreach.batch_approved",)),
    ("email_sent", "Outreach emails sent", "event", ("outreach.email_sent",)),
    ("email_held", "Outreach emails held", "event", ("outreach.email_held",)),
    ("reply_received", "Outreach replies", "event", ("outreach.reply_received",)),
    ("credits_spent", "Credit spends", "table", "credit_spends"),
    ("credits_requested", "Credits requested", "event", ("credits.requested",)),
    ("needs_you_shown", "Needs-you shown", "event", ("needs_you.created",)),
    ("needs_you_resolved", "Needs-you resolved", "event", ("needs_you.resolved",)),
    ("failure_shown", "Failure card shown", "event", ("recovery.failure_shown",)),
    ("failure_alternative", "Alternative offered or DIY result sent",
     "event", ("recovery.alternative_offered", "recovery.result_submitted")),
    ("god_mode_requested", "God mode requested", "event", ("god_mode.requested",)),
    ("feedback", "Feedback or bug report", "event", ("feedback.submitted",)),
)

# Names this module introduces or the funnel depends on; merged into events.ALLOWED.
FUNNEL_EVENT_NAMES = frozenset(
    name.split(":")[0] for _k, _l, kind, src in FUNNEL if kind == "event" for name in src
)


def owner_of(session: Any, venture_id: str | None, workspace_id: str | None = None) -> tuple[str | None, str | None]:
    """(workspace_id, owner person id) for a venture/workspace. Never raises."""
    try:
        from app.db.models import Venture, Workspace

        if venture_id:
            venture = session.get(Venture, venture_id)
            if venture is not None:
                workspace_id = workspace_id or venture.workspace_id
        ws = session.get(Workspace, workspace_id) if workspace_id else None
        return workspace_id, (ws.owner_id if ws is not None else None)
    except Exception:  # noqa: BLE001
        return workspace_id, None


def emit_for_venture(name: str, venture_id: str | None, *, key: str | None = None,
                     person_id: str | None = None, workspace_id: str | None = None,
                     props: dict[str, Any] | None = None) -> None:
    """Emit attributed to the venture's owner unless a person is given. Never raises.

    ``key`` makes it once-per-identity (retries cannot double count).
    """
    try:
        from app.analytics.events import emit, emit_once
        from app.state import db

        if person_id is None or workspace_id is None:
            with db()() as session:
                workspace_id, owner = owner_of(session, venture_id, workspace_id)
            person_id = person_id or owner
        if name in {"journey.step_entered", "journey.step_completed"}:
            person_id = None
        if key:
            emit_once(name, event_key=hashlib.sha256(key.encode()).hexdigest()[:40], person_id=person_id, workspace_id=workspace_id,
                      venture_id=venture_id, props=props)
        else:
            emit(name, person_id=person_id, workspace_id=workspace_id, venture_id=venture_id, props=props)
    except Exception:  # noqa: BLE001 - telemetry never breaks a product path
        log.exception("funnel emit failed for %s", name)


# Job kind -> {outcome: event}. The worker and watchdog are the one place every job passes.
JOB_EVENTS = {
    "research.deep": {"started": "research.started", "completed": "research.completed", "failed": "research.failed"},
    "deploy.run": {"failed": "site.publish_failed"},
    "deploy.unpublish": {"completed": "site.unpublished", "failed": "site.unpublish_failed"},
}


def stage_job_outcome(session: Any, job: Any, outcome: str, *, person_id: str | None = None) -> None:
    """Stage the funnel event for a job outcome in the caller's transaction, once per job."""
    name = JOB_EVENTS.get(getattr(job, "kind", ""), {}).get(outcome)
    if not name:
        return
    try:
        from app.analytics.events import emit_in_session
        from app.analytics.error_events import closed_label

        workspace_id, owner = owner_of(session, job.venture_id, job.workspace_id)
        emit_in_session(session, name, event_key=f"{job.id}:{outcome}".lower(), person_id=person_id or owner,
                        workspace_id=workspace_id, venture_id=job.venture_id,
                        props={"job_id": job.id, "kind": closed_label(job.kind)})
    except Exception:  # noqa: BLE001
        log.exception("funnel job event failed")


# ---------------------------------------------------------------- read side
def _event_counts(session: Any, people: set[str]) -> dict[str, dict[str, int]]:
    """person -> step key -> count, from analytics_events attributed to the person or, when the
    emitter had no actor, to the owner of the venture/workspace."""
    from sqlalchemy import func, select
    from sqlalchemy.orm import aliased

    from app.db.models import AnalyticsEvent as E, Venture, Workspace

    wv, ww = aliased(Workspace), aliased(Workspace)
    pid = func.coalesce(E.person_id, wv.owner_id, ww.owner_id)
    wanted = {n.split(":")[0] for _k, _l, kind, src in FUNNEL if kind == "event" for n in src}
    rows = session.execute(
        select(pid.label("pid"), E.event_name, E.props_json, func.count().label("n"))
        .select_from(E)
        .outerjoin(Venture, Venture.id == E.venture_id)
        .outerjoin(wv, wv.id == Venture.workspace_id)
        .outerjoin(ww, ww.id == E.workspace_id)
        .where(E.event_name.in_(wanted))
        .group_by(pid, E.event_name, E.props_json)
    ).all()
    out: dict[str, dict[str, int]] = {}
    import json

    for person, name, props, n in rows:
        if person not in people:
            continue
        tool = ""
        if name == "chat.tool_invoked":
            try:
                tool = json.loads(props or "{}").get("tool", "")
            except ValueError:
                tool = ""
        token = f"{name}:{tool}" if tool else name
        for key, _label, kind, src in FUNNEL:
            if kind == "event" and (token in src or name in src):
                out.setdefault(person, {})
                out[person][key] = out[person].get(key, 0) + int(n)
    return out


def _table_counts(session: Any, people: set[str]) -> dict[str, dict[str, int]]:
    """Durable domain tables that are already the record for a step, by venture owner."""
    from sqlalchemy import func, select

    from app.db.models import CreditAccount, CreditLedgerEntry, Venture, Workspace
    from app.db.supymem_models import AppEvent, ClarifierTurn, FounderApp, VentureWaitlistEntry

    owner = Workspace.owner_id
    queries = {
        "clarification_answers": select(owner, func.count()).select_from(ClarifierTurn)
        .join(Venture, Venture.id == ClarifierTurn.venture_id).join(Workspace, Workspace.id == Venture.workspace_id)
        .where(ClarifierTurn.role == "founder", ClarifierTurn.turn > 1).group_by(owner),
        "page_views": select(owner, func.count()).select_from(AppEvent)
        .join(FounderApp, FounderApp.id == AppEvent.app_id).join(Workspace, Workspace.id == FounderApp.workspace_id)
        .where(AppEvent.name.in_(("landing_view", "page_view"))).group_by(owner),
        "waitlist_signups": select(owner, func.count()).select_from(VentureWaitlistEntry)
        .join(Workspace, Workspace.id == VentureWaitlistEntry.workspace_id)
        .where(VentureWaitlistEntry.is_test.is_(False)).group_by(owner),
        "credit_spends": select(CreditAccount.owner_person_id, func.count()).select_from(CreditLedgerEntry)
        .join(CreditAccount, CreditAccount.id == CreditLedgerEntry.account_id)
        .where(CreditLedgerEntry.kind == "spend").group_by(CreditAccount.owner_person_id),
    }
    key_for = {src: key for key, _l, kind, src in FUNNEL if kind == "table"}
    out: dict[str, dict[str, int]] = {}
    for src, query in queries.items():
        for person, n in session.execute(query).all():
            if person in people:
                out.setdefault(person, {})[key_for[src]] = int(n)
    return out


def founder_funnel() -> dict[str, Any]:
    """Per-founder step counts for everyone admitted to the beta, plus how many founders reached each step."""
    from sqlalchemy import select

    from app.db.models import Person
    from app.db.supymem_models import ReferralAdmission
    from app.state import db

    with db()() as session:
        cohort = session.execute(
            select(ReferralAdmission.person_id, ReferralAdmission.created_at, ReferralAdmission.legacy, Person.email)
            .outerjoin(Person, Person.id == ReferralAdmission.person_id)
            .order_by(ReferralAdmission.created_at)
        ).all()
        people = {row[0] for row in cohort}
        events, tables = _event_counts(session, people), _table_counts(session, people)
        voice = voice_summary(session, people)
    founders = []
    reached = {key: 0 for key, *_ in FUNNEL}
    for person, admitted, legacy, email in cohort:
        counts = {key: events.get(person, {}).get(key, 0) + tables.get(person, {}).get(key, 0) for key, *_ in FUNNEL}
        for key, n in counts.items():
            reached[key] += 1 if n else 0
        founders.append({"person_id": person, "email": email, "admitted_at": admitted.isoformat() if admitted else None,
                         "legacy": bool(legacy), "counts": counts})
    return {"steps": [{"key": k, "label": label} for k, label, *_ in FUNNEL],
            "founders_total": len(founders), "founders_reached": reached, "founders": founders, "voice": voice}


def voice_summary(session: Any, people: set[str]) -> dict[str, Any]:
    """Thirty-day client observations for beta founders; no transcripts or per-call payloads."""
    import json
    from datetime import timedelta

    from sqlalchemy import select
    from app.db.models import AnalyticsEvent
    from core.clock import utcnow

    keys = ("barge_in_accepted", "barge_in_ignored", "response_cancelled",
            "duplicate_response_blocked", "transcription_failed")
    modes = {mode: {**dict.fromkeys(keys, 0), "turn_latency_samples": 0,
                    "average_turn_latency_ms": None} for mode in ("vad", "ptt")}
    latency = {mode: [] for mode in modes}
    observed = set()
    rows = session.execute(select(AnalyticsEvent.id, AnalyticsEvent.event_name, AnalyticsEvent.props_json)
        .where(AnalyticsEvent.person_id.in_(people),
               AnalyticsEvent.occurred_at >= utcnow() - timedelta(days=30),
               AnalyticsEvent.event_name.in_(("voice.session_started", "voice.metric")))).all()
    sessions = {r[0] for r in rows if r[1] == "voice.session_started"}
    for event_id, name, raw in rows:
        if name == "voice.session_started":
            continue
        try:
            props = json.loads(raw)
        except (TypeError, ValueError):
            continue
        mode, key = props.get("mode"), props.get("metric")
        if mode not in modes or props.get("session_event_id") not in sessions:
            continue
        observed.add(props["session_event_id"])
        if key in keys:
            modes[mode][key] += 1
        elif key == "turn_latency" and type(props.get("duration_ms")) is int and 0 <= props["duration_ms"] <= 600000:
            latency[mode].append(props["duration_ms"])
    for mode, values in latency.items():
        modes[mode]["turn_latency_samples"] = len(values)
        modes[mode]["average_turn_latency_ms"] = round(sum(values) / len(values)) if values else None
    return {"window_days": 30, "sessions_minted": len(sessions), "sessions_observed": len(observed),
            "modes": modes}

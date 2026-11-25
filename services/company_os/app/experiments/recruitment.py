"""Keep a chat-prepared test and its outreach in the same venture."""
from __future__ import annotations

import json
import re
import time

from sqlalchemy import select

from app.db.models import Venture, VentureDocument
from app.chat.conversations import current_conversation
from app.db.supymem_models import Experiment
from app.experiments import ledger
from app.state import db
from core.clock import utcnow
from core.copy import founder_copy


def _retire_drafts(venture_id: str, draft_ids: set[str]) -> None:
    from app.autopilot import outreach

    if not draft_ids:
        return
    with db()() as session:
        rows = session.scalars(select(VentureDocument).where(VentureDocument.venture_id == venture_id,
            VentureDocument.kind == outreach.KIND, VentureDocument.id.in_(draft_ids)).with_for_update())
        for row in rows:
            data = json.loads(row.provenance_json or "{}")
            if data.get("state") in {"draft", "manual"}:
                data["state"] = "review_abandoned"
                row.provenance_json, row.updated_at = json.dumps(data), utcnow()
        session.commit()


def selected(venture_id: str, experiment_id: str | None = None) -> dict | None:
    conversation = current_conversation()
    conversation_id = conversation.id if conversation else None
    with db()() as session:
        venture = session.get(Venture, venture_id)
        if not venture:
            return None
        direction = ledger.current_direction(session, venture_id)
        rows = session.scalars(select(Experiment).where(Experiment.venture_id == venture_id,
            Experiment.workspace_id == venture.workspace_id, Experiment.kind == ledger.KIND,
            Experiment.state.in_(("proposed", "running"))))
        matches = [row for row in rows if ledger._ctx(row).get("outreach_step")
            and ledger._ctx(row).get("direction", "") == direction
            and (experiment_id or ledger._ctx(row).get("outreach_conversation_id") == conversation_id)
            and (not experiment_id or row.id == experiment_id)]
        row = max(matches, key=lambda item: ledger._ctx(item).get("outreach_prepared_at", 0), default=None)
        return ledger._view(row) if row else None


def prepare(venture_id: str, payload: dict, *, plan_only: bool = False) -> dict:
    from app.chat import experiments
    from app.autopilot import outreach

    running = not plan_only and payload.get("run") is True
    reply = experiments.review_test_run(venture_id, {**payload, "run": True})
    selected_id = payload.get("experiment_id") or next((action.get("payload", {}).get("experiment_id")
        for action in reply.get("actions", []) if action.get("action") == "experiment.recommendation.prepare"), None)
    row = (ledger.get(selected_id, venture_id) if selected_id else
        next((item for item in ledger.list_for_venture(venture_id)
              if item["hypothesis"] == experiments._norm(payload.get("hypothesis"))), None))
    if not row or row["state"] not in {"proposed", "running"} or not re.search(r"\b(?:interviews?|recruit\w*|outreach|e-?mails?)\b", row["action"], re.I):
        return reply
    snapshot = experiments._experiment_snapshot(venture_id) or {}
    audience = (snapshot.get("journey") or {}).get("icp") or {}
    conversation = current_conversation()
    with db()() as session:
        saved = session.get(Experiment, row["id"], with_for_update=True)
        if not saved or saved.venture_id != venture_id or ledger._ctx(saved).get("runner") == "system":
            return reply
        workspace_id = saved.workspace_id
        older = () if plan_only else session.scalars(select(Experiment).where(Experiment.venture_id == venture_id,
            Experiment.workspace_id == saved.workspace_id, Experiment.kind == ledger.KIND, Experiment.id != saved.id))
        previous_drafts = {identifier for item in older
            if ledger._ctx(item).get("outreach_step") and ledger._ctx(item).get("outreach_conversation_id") == (conversation.id if conversation else None)
            for identifier in ledger._ctx(item).get("outreach_draft_ids") or []}
        saved.context = json.dumps({**ledger._ctx(saved), "outreach_step": True, "outreach_prepared_at": time.time_ns(),
            "outreach_conversation_id": conversation.id if conversation else None,
            "outreach_audience": {key: audience.get(key) for key in ("who", "problem")}})
        session.commit()
    if previous_drafts:
        _retire_drafts(venture_id, previous_drafts)
    if running:
        row = ledger.start(row["id"], venture_id)
    with db()() as session:
        people = [contact for contact in outreach.contacts(venture_id) if not contact.times_emailed
            and not outreach._suppressed(session, workspace_id, contact)]
    step = ("You can use the people already found for your outreach step. " if people else
            "Your outreach step is to find people who match your customer, then draft interview invitations. ")
    return {**reply, "body_md": founder_copy((reply["body_md"].replace("Prepared your experiment:", "Your test is running:", 1)
        if running else reply["body_md"]) + "\n\n"
        + (f"Your customer: {audience['who'].rstrip('.!? ,;:…')}. " if audience.get("who") else "")
        + (f"Your problem to test: {audience['problem'].rstrip('.!? ,;:…')}. " if audience.get("problem") else "")
        + step + "You can ask for invitation drafts here. Nothing is sent. "
        "After you contact people, tell me what you tried and what happened here to record this test."),
        "actions": reply["actions"] if plan_only else [({"label": "Draft interview invitations", "action": "outreach.draft",
            "payload": {"batch": True, "experiment_id": row["id"], "run": True}, "style": "primary"} if people else
            {"label": "Find people for your test", "action": "gtm.find",
             "payload": {"note": row["hypothesis"], "retry": True, "recruitment_opt_in": audience.get("mode") == "consumer"}, "style": "primary"}),
            {"label": "Review your test", "action": "experiment.list", "payload": {}}]}


async def draft(venture_id: str, row: dict, request: str = "") -> dict:
    from app.autopilot import outreach
    from app.chat.experiments import _experiment_snapshot, _reply

    with db()() as session:
        saved = session.get(Experiment, row["id"])
        context = ledger._ctx(saved)
        workspace_id = saved.workspace_id
    snapshot = _experiment_snapshot(venture_id) or {}
    audience = context.get("outreach_audience") or {}
    current = (snapshot.get("journey") or {}).get("icp") or {}
    if any(audience.get(key) != current.get(key) for key in ("who", "problem")):
        return _reply("Your customer or problem changed. Ask me to prepare your test again so the invitations match your current people. Nothing was sent.",
            [{"label": "Prepare your test again", "action": "experiment.run_review", "payload": {"experiment_id": row["id"], "run": True}}])
    angle = founder_copy(f"Your ask: a {outreach._interview_minutes(row['action'])}-minute call. "
        f"Interview invitations for {audience.get('who') or 'your customer'} about "
        f"{audience.get('problem') or 'your customer problem'}. "
        f"Test: {row['hypothesis']}. "
        f"Plan: {row['action']}. What to record: {row['measure']}. {request}")
    with db()() as session:
        eligible = {contact.id for contact in outreach.contacts(venture_id) if contact.email
            and not contact.times_emailed and not outreach._suppressed(session, workspace_id, contact)}
    _retire_drafts(venture_id, {item["doc_id"] for item in outreach.drafts(venture_id, "draft", "manual")
        if item.get("contact_id") in eligible and item.get("channel") == "email" and item.get("day") == 1
        and not any(item.get(key) for key in ("experiment_id", "channel_experiment_id", "waitlist_entry_id"))
        and outreach._interview_minutes(item.get("body") or "") != outreach._interview_minutes(row["action"])})
    before = {item["doc_id"] for item in outreach.drafts(venture_id)}
    reply = await outreach.draft_batch(venture_id, request=angle, review_only=True)
    if reply is None:
        existing = [item for item in outreach.drafts(venture_id, "draft", "manual")
            if item.get("contact_id") in eligible and item.get("channel") == "email" and item.get("day") == 1
            and not any(item.get(key) for key in ("experiment_id", "channel_experiment_id", "waitlist_entry_id"))]
        if not existing:
            return _reply("You have no new eligible people to invite for this test. Find more people or tell me which saved invitation to change. Nothing was sent.",
                [{"label": "Find people for your test", "action": "gtm.find", "payload": {"note": row["hypothesis"]}},
                 {"label": "Show your drafts", "action": "outreach.drafts"}])
        made = [item["doc_id"] for item in existing]
    else:
        batch_id = next((action.get("confirmation", {}).get("resulting_state", {}).get("batch_id")
            for action in reply.get("actions", []) if action.get("confirmation")), None)
        made = [item["doc_id"] for item in outreach.drafts(venture_id)
            if item["doc_id"] not in before and (not batch_id or item.get("batch_id") == batch_id)]
    with db()() as session:
        saved = session.get(Experiment, row["id"], with_for_update=True)
        latest = ledger._ctx(saved)
        saved.context = json.dumps({**latest, "outreach_draft_ids": sorted(set(latest.get("outreach_draft_ids") or []) | set(made))})
        session.commit()
    from app.chat import customers

    invitations = [item for item in outreach.drafts(venture_id, "draft", "manual") if item["doc_id"] in made]
    body = "\n\n".join(f"```text\nTo: {item['to']}\nSubject: {item['subject']}\n\n{item['body']}\n```" for item in invitations)
    review = customers._approval_payload(venture_id, [item for item in invitations if item.get("channel") == "email"])
    return _reply("Your invitations for this test are saved for the people you found. Nothing was sent. "
        "Tell me who you contacted and what happened here to record your result.\n\n" + body,
        ([{"label": "Send your invitations", "action": "gtm.send", "payload": review, "style": "primary"}] if review["doc_ids"] else []) +
        [{"label": "Show your drafts", "action": "outreach.drafts"},
         {"label": "Review your test", "action": "experiment.list", "payload": {}}])

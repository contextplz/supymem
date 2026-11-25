"""One reviewed field correction, followed by a fresh research admission."""
from datetime import timedelta
import json
import re

from app.chat.actions import get_action
from app.chat.confirm import ConfirmationCard
from app.chat.proposals import _scope
from app.db.models import Venture
from app.db.supymem_models import ChatProposal, IdeaSession
from app.state import db
from app.ventures.current_idea import current_revision, revision_fields
from core.clock import utcnow
from core.ids import new_id


def parse(text: str) -> dict | None:
    # ponytail: explicit English customer/price edits, extend from real missed founder requests.
    match = re.fullmatch(
        r"(?:please\s+)?(?:actually[ ,]+)?(?:change\s+(?:the\s+)?|(?:the\s+)?)"
        r"(?P<field>target customer|customer|price|pricing)\s+(?:to|is|=)\s+"
        r"(?P<value>[^\n]{1,2000}?)\s*(?:[,;.]\s*|\s+and\s+|\s+then\s+)"
        r"(?:please\s+)?(?:rerun|re-run|redo)\s+(?:the\s+)?research[.!]*",
        (text or "").strip(), re.I,
    )
    if not match:
        return None
    value = match["value"].strip()
    if not value or re.search(r"\b(?:and|then)\s+(?:send|publish|delete|charge|build|change)\b", value, re.I):
        return None
    return {"customer" if "customer" in match["field"].lower() else "pricing_hypothesis": value}


def prepare(venture_id: str, text: str, actor: str | None) -> dict:
    patch = parse(text)
    if patch is None:
        return {"kind": "text", "body_md": "Tell me your exact audience or price and whether you want to rerun research. Nothing changed yet.",
                "actions": [{"label": "Edit idea", "href": f"/v/{venture_id}/idea"}], "citations": []}
    wid, person = _scope(venture_id, actor)
    now = utcnow()
    with db()() as session:
        venture = session.get(Venture, venture_id)
        revision = current_revision(venture, session)
        idea = session.get(IdeaSession, revision.session_id) if revision else None
        label, value = next(iter(patch.items()))
        effects = [f"Save {label.replace('_', ' ')} as {value} in your updated idea.",
                   "Start a new research run using that revision. Old results remain saved."]
        if idea is None or idea.state != "confirmed" or idea.confirmed_revision_id != revision.id:
            fields = revision_fields(revision) if revision else {"summary": venture.raw_idea or ""}
            fields.update(patch)
            effects.insert(0, "Confirm these idea details for research:\n" + "\n".join(
                f"{key.replace('_', ' ').title()}: {fields[key]}" for key in (
                    "summary", "problem", "customer", "offer", "first_user_path", "pricing_hypothesis",
                    "workflow", "desired_outcome", "requirements", "constraints", "assumptions", "open_questions", "risks")
                if fields.get(key)))
        proposal = ChatProposal(id=new_id("cpr"), workspace_id=wid, venture_id=venture_id, actor_id=person,
            action="research.update_and_rerun", state="pending", created_at=now, updated_at=now,
            expires_at=now + timedelta(minutes=15),
            arguments_json=json.dumps({"revision_id": revision.id if revision else None, "patch": patch,
                                       "raw_idea": venture.raw_idea if revision is None else None}),
            review_json=json.dumps({"title": "Save correction and rerun research", "effects": effects}))
        session.add(proposal)
        session.commit()
        card = ConfirmationCard.for_action(get_action(proposal.action), interpretation=effects[0],
            consequences=effects, resulting_state={"proposal_id": proposal.id})
    return {"kind": "confirmation", "body_md": "Your correction and fresh research are ready to start.",
            "actions": [card.as_action()], "citations": []}


def apply(venture_id: str, actor: str, args: dict, request_key: str) -> dict:
    from app.db.session import serialize_writes
    from app.idea.agent import Understanding
    from app.idea.service import append_revision
    from app.research.service import start
    from app.autopilot import receipts
    from app.ventures.state_brief import invalidate_venture_state_brief

    with db()() as session:
        serialize_writes(session, key="research-page-choice:" + venture_id)
        from app.chat.store import CLIENT_REQUEST_ID
        from app.chat.work_control import check_stopped
        check_stopped(session, CLIENT_REQUEST_ID.get())
        venture = session.get(Venture, venture_id, with_for_update=True)
        revision = current_revision(venture, session)
        idea = session.get(IdeaSession, revision.session_id) if revision else None
        if ((revision.id if revision else None) != args["revision_id"]
                or (revision is None and venture.raw_idea != args.get("raw_idea"))):
            raise ValueError("research_inputs_changed")
        fields = revision_fields(revision) if revision else {"summary": venture.raw_idea or ""}
        candidate = {key: value for key, value in fields.items() if key in Understanding.model_fields}
        candidate.update(args["patch"])
        fields.update(Understanding.model_validate(candidate).model_dump())
        if fields.get("_website_answers"):
            fields["_website_answers"] = {key: value for key, value in fields["_website_answers"].items() if key not in args["patch"]}
        if revision:
            fields["_corrects_revision_id"] = revision.id
        now = utcnow()
        if idea and idea.state != "confirmed":
            corrected = idea
            corrected.state = "confirmed"
            corrected.confirmed_at = now
            corrected.confirmed_by_person_id = actor
        else:
            corrected = IdeaSession(id=new_id("ids"), workspace_id=venture.workspace_id, venture_id=venture_id,
                case=idea.case if idea else "know", state="confirmed", created_at=now, updated_at=now,
                confirmed_at=now, confirmed_by_person_id=actor)
            session.add(corrected)
        session.flush()
        updated = append_revision(session, corrected, fields, source="founder")
        corrected.confirmed_revision_id = updated.id
        revision_id = updated.id
        session.commit()
    invalidate_venture_state_brief(venture_id)
    try:
        result = start(venture_id, expected_revision_id=revision_id, request_key=request_key)
    except RuntimeError as exc:
        from app.chat.recovery import founder_failure
        return {**founder_failure(exc, venture_id, "your research", {"action": "jobs.status", "payload": {}},
            safety="The corrected value is saved. Check Research before starting another run."), "revision_id": revision_id}
    if not result.get("job_id"):
        return {**result, "revision_id": revision_id}
    receipts.queued_job(result["job_id"])
    return {**result, "revision_id": revision_id, "kind": "text",
            "body_md": "The corrected value is saved in a new idea revision, and fresh research is queued. Previous research remains saved.",
            "actions": [{"label": "Open research", "href": f"/v/{venture_id}/research"}], "citations": []}


def confirm(venture_id: str, actor: str | None, payload: dict) -> dict:
    from app.chat import proposals

    if set(payload) != {"proposal_id"} or not isinstance(payload["proposal_id"], str):
        raise ValueError("research_review_required")
    proposal = proposals.get(venture_id, actor, payload["proposal_id"])
    if proposal["action"] != "research.update_and_rerun":
        raise ValueError("research_review_required")
    if proposal["state"] in {"executing", "needs_reconciliation"}:
        return {"kind": "needs_you", "body_md": "This request has an unconfirmed outcome. Check saved research before requesting a new run. I have not retried it.",
                "actions": [{"label": "Check research", "href": f"/v/{venture_id}/research"}], "citations": []}
    proposal = proposals.confirm(venture_id, actor, payload["proposal_id"])
    return proposal["receipt"]

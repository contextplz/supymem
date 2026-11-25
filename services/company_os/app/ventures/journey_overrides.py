"""Founder-confirmed journey choices, separate from completion evidence."""
from __future__ import annotations

import json
import re
from datetime import timedelta, timezone

from sqlalchemy import select

from app.authz.workspace import subject_person_id
from app.db.models import Venture, VentureDocument
from app.db.supymem_models import ChatProposal
from app.gtm.god.generation import authority
from app.state import db
from core.clock import utcnow
from core.ids import new_id

STEPS = ("clarification", "validation", "research", "landing_page", "find_customers")
NOTE = "Lower confidence: skipped checks are not evidence. We will use your saved idea and website, and label assumptions."
ACTIONS = {"journey.skip_step", "journey.jump_to", "journey.restore_step", "venture.set_website"}


def parse(text: str) -> tuple[str, dict] | None:
    from app.chat.llm_router import _command_evidence
    source = _command_evidence(text).strip()
    if re.fullmatch(r"skip (?:optional )?questions(?: and keep going)?[.!]?", source, re.I):
        return "journey.skip_step", {"step": "clarification", "why": text[:2000]}
    website = re.match(r"^(?:i|we)\s+already\s+have\s+(?:a\s+)?(?:website|site)\s*(?:at|:)\s*(https?://[^\s]+)", source, re.I)
    if website:
        return "venture.set_website", {"url": website[1].rstrip(".,"), "why": text[:2000]}
    match = re.match(r"^(?:(?:please|let['’]s|i\s+(?:want|insist)\s+to)\s+)?(skip(?:\s+(?:to|straight to))?|jump\s+to|restore|undo\s+(?:skipping|skip))\s+(?:the\s+)?"
                     r"(clarification|clarify|validation|validate|research|landing[ _]page|find[ _]customers|gtm|go.to.market)\b", source, re.I)
    if not match:
        return None
    verb, step = match[1].lower(), re.sub(r"[ _]+", "_", match[2].lower())
    step = {"clarify": "clarification", "validate": "validation", "gtm": "find_customers", "go-to-market": "find_customers",
            "go_to_market": "find_customers"}.get(step, step)
    action = "journey.restore_step" if verb.startswith(("restore", "undo")) else "journey.jump_to" if "to" in verb else "journey.skip_step"
    return action, {"step": step, "why": text[:2000]}


def plan_after_skip(text: str) -> str | None:
    from app.chat.intent import _PLAN_GENERATE_RE, _PLAN_LEAD
    from app.chat.llm_router import _command_evidence, _prohibited_act

    parsed = parse(text)
    if not parsed or parsed[0] not in {"journey.skip_step", "journey.jump_to"}:
        return None
    clauses = re.split(r"\s+and\s+(?:then\s+)?", _command_evidence(text, preserve_length=True), maxsplit=1, flags=re.I)
    if len(clauses) == 2:
        request = clauses[1].strip()
        if not _prohibited_act("plan_generate", request) and not re.search(r"\b(?:if|unless)\b", request, re.I) and (
                _PLAN_GENERATE_RE.match(request) or re.match(
                _PLAN_LEAD + r"(?:make|generate|create|draft|prepare|write)\s+(?:me\s+)?(?:the|my|our|a)\s+"
                r"(?:(?:new|fresh|different|another|provisional|execution|build|startup)\s+)*plan\b", request, re.I)):
            return text[len(text) - len(clauses[1]):].strip()
    return None


def read(venture_id: str, *, session=None) -> dict:
    if session is None:
        with db()() as reader:
            return read(venture_id, session=reader)
    row = session.get(VentureDocument, f"journey_overrides:{venture_id}")
    try:
        data = json.loads(row.body_md or "{}") if row and row.venture_id == venture_id and row.kind == "journey_overrides" else {}
    except (TypeError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    return {**({"startup_context": data["startup_context"]} if isinstance(data.get("startup_context"), dict) else {}),
            "skipped": data.get("skipped") if isinstance(data.get("skipped"), dict) else {},
            "target": data.get("target") if data.get("target") in STEPS else None,
            "history": data.get("history") if isinstance(data.get("history"), list) else []}


def chose_customers(venture_id: str) -> bool:
    """A saved customer goal for an existing product needs no automatic idea validation."""
    from app.db.supymem_models import EntryChatProposal
    with db()() as session:
        if read(venture_id, session=session)["target"] == "find_customers":
            return True
        from app.entry.chat_start import _GTM
        from app.intake.smart import _owned_urls
        venture = session.get(Venture, venture_id)
        if venture and _GTM.search(venture.raw_idea or "") and _owned_urls(venture.raw_idea or ""):
            return True
        return session.scalar(select(EntryChatProposal.id).where(EntryChatProposal.target_venture_id == venture_id,
            EntryChatProposal.route == "gtm", EntryChatProposal.state == "committed").limit(1)) is not None


def _scope(venture_id, actor):
    from app.tenancy import current_or_none
    with db()() as session:
        venture = session.get(Venture, venture_id)
        if not venture:
            raise KeyError("venture_not_found")
        wid = venture.workspace_id
    scope = current_or_none()
    if scope and (scope.workspace_id != wid or scope.venture_id not in {"", venture_id}):
        raise PermissionError("journey_scope_mismatch")
    return wid, subject_person_id(authority(wid, venture_id, write=True, actor=actor))


def prepare(venture_id, action, payload, actor):
    from app.chat.actions import get_action
    from app.chat.confirm import ConfirmationCard
    from app.chat.recovery import clean_url
    wid, actor = _scope(venture_id, actor)
    if action not in ACTIONS or set(payload) - {"step", "why", "url"}:
        raise ValueError("journey_invalid_action")
    why = payload.get("why")
    if not isinstance(why, str) or not why.strip() or len(why) > 2000:
        raise ValueError("journey_reason_required")
    args = {"why": why.strip()}
    if action == "venture.set_website":
        url = clean_url(payload.get("url"))
        if not url:
            raise ValueError("journey_invalid_website")
        args["url"] = url
        interpretation = f"Record {url} as your existing live site (founder-reported, not verified). No journey steps are skipped."
    else:
        step = payload.get("step")
        if step not in STEPS:
            raise ValueError("journey_invalid_step")
        args["step"] = step
        skipped = list(STEPS[:STEPS.index(step)]) if action == "journey.jump_to" else [step]
        interpretation = (f"Restore {step.replace('_', ' ')} to your journey." if action == "journey.restore_step" else
                          f"{'Jump to' if action == 'journey.jump_to' else 'Skip'} {step.replace('_', ' ')}. "
                          f"Skipped by you: {', '.join(item.replace('_', ' ') for item in skipped) or 'none'}.")
    now, proposal_id = utcnow(), new_id("cpr")
    with db()() as session:
        session.add(ChatProposal(id=proposal_id, workspace_id=wid, venture_id=venture_id, actor_id=actor,
            action=action, state="pending", arguments_json=json.dumps(args), review_json=json.dumps({"interpretation": interpretation}),
            created_at=now, updated_at=now, expires_at=now + timedelta(minutes=15)))
        session.commit()
    card = ConfirmationCard.for_action(get_action(action), interpretation=interpretation,
        consequences=[NOTE, "Reversible. No new work starts, no credits are spent, nothing is published or sent. Already running work is not cancelled."],
        resulting_state={"proposal_id": proposal_id})
    return {"kind": "confirmation", "body_md": "I recommend the standard next step, but the choice is yours. " + interpretation,
            "actions": [card.as_action()], "citations": []}


def apply(venture_id, action, proposal_id, actor):
    wid, actor = _scope(venture_id, actor)
    now = utcnow()
    with db()() as session:
        from app.db.session import serialize_writes
        serialize_writes(session, key="journey-overrides:" + venture_id)
        session.get(Venture, venture_id, with_for_update=True)
        proposal = session.scalar(select(ChatProposal).where(ChatProposal.id == proposal_id,
            ChatProposal.workspace_id == wid, ChatProposal.venture_id == venture_id,
            ChatProposal.actor_id == actor, ChatProposal.action == action).with_for_update())
        if proposal is None:
            raise KeyError("journey_proposal_not_found")
        if proposal.state == "succeeded":
            return json.loads(proposal.receipt_json)
        if proposal.state != "pending" or proposal.expires_at.replace(tzinfo=timezone.utc) <= now.replace(tzinfo=timezone.utc):
            raise ValueError("journey_proposal_expired")
        args = json.loads(proposal.arguments_json)
        data = read(venture_id, session=session)
        event = {"action": action.removeprefix("journey."), "who": actor, "when": now.isoformat(), **args}
        step = args.get("step")
        if action == "venture.set_website":
            site = session.get(VentureDocument, f"external_site:{venture_id}")
            if site is None:
                site = VentureDocument(id=f"external_site:{venture_id}", venture_id=venture_id,
                    kind="external_site", title="Your existing website", created_at=now)
                session.add(site)
            site.path, site.updated_at = args["url"], now
            site.body_md = json.dumps({"url": args["url"], "status": "unverified", "audit": None,
                "note": "Your own live site (founder-reported, not checked by Supymem)"})
            site.provenance_json = json.dumps(event)
        elif action == "journey.restore_step":
            data["skipped"].pop(step, None)
            data["target"] = step
        else:
            skipped = STEPS[:STEPS.index(step)] if action == "journey.jump_to" else [step]
            for item in skipped:
                data["skipped"][item] = event
            if action == "journey.jump_to":
                data["skipped"].pop(step, None)
            data["target"] = step if action == "journey.jump_to" else next(
                (item for item in STEPS[STEPS.index(step) + 1:] if item not in data["skipped"]), None)
        data["history"].append(event)
        row = session.get(VentureDocument, f"journey_overrides:{venture_id}")
        if row is None:
            row = VentureDocument(id=f"journey_overrides:{venture_id}", venture_id=venture_id,
                kind="journey_overrides", title="Your journey choices", created_at=now)
            session.add(row)
        row.body_md, row.provenance_json, row.updated_at = json.dumps(data), json.dumps(event), now
        if action == "journey.restore_step" and step in {"clarification", "validation"}:
            _restore_context(session, venture_id, wid, step, data["skipped"])
        elif {"clarification", "validation"} & data["skipped"].keys():
            _save_context(session, venture_id, wid, actor, skipped=data["skipped"])
        result = {"kind": "text", "body_md": "Saved your choice. " + NOTE,
            "actions": [{"label": "Review your journey", "href": f"/v/{venture_id}"}], "citations": []}
        proposal.state, proposal.receipt_json, proposal.updated_at = "succeeded", json.dumps(result), now
        session.commit()
    from app.ventures.state_brief import invalidate_venture_state_brief
    invalidate_venture_state_brief(venture_id)
    return result


def _save_context(session, venture_id, wid, actor, *, skipped):
    from app.core.fsm import advance
    from app.db.supymem_models import IdeaSession
    from app.idea.service import append_revision
    from app.ventures.current_idea import current_idea, current_revision, revision_fields
    from app.ventures.startup_review import effective_fields
    venture = session.get(Venture, venture_id)
    summary = current_idea(venture, session=session)
    idea = session.scalar(select(IdeaSession).where(IdeaSession.workspace_id == wid,
        IdeaSession.venture_id == venture_id, IdeaSession.state != "abandoned")
        .order_by(IdeaSession.updated_at.desc(), IdeaSession.created_at.desc(), IdeaSession.id.desc()).limit(1))
    if idea and idea.state == "confirmed":
        return
    if "clarification" not in skipped and (idea is None or idea.state not in {"reviewing", "proposing", "validating"}):
        return
    confirm = "validation" in skipped
    fields = effective_fields(session, venture, revision_fields(current_revision(venture, session)))
    if fields.get("_founder_override") and idea and idea.state == "reviewing" and not confirm:
        return
    now = utcnow()
    if idea is None:
        idea = IdeaSession(id=new_id("ids"), workspace_id=wid, venture_id=venture_id, case="know",
            state="open", created_at=now, updated_at=now)
        session.add(idea)
        session.flush()
    append_revision(session, idea, {**fields, "summary": summary, "_founder_override": True, "_missing_idea_summary": not bool(summary.strip()),
        "limitations": [NOTE]}, source="founder_override",
        summary=summary or "No idea summary supplied; use placeholders, not invented product claims.")
    next_states = {"open": "recon", "recon": "clarifying", "clarifying": "proposing",
                   "proposing": "reviewing", "validating": "reviewing", "reviewing": "confirmed"}
    destination = "confirmed" if confirm else "reviewing"
    while idea.state != destination:
        advance(session, idea, next_states[idea.state], actor=actor, reason="founder_journey_override")
    if confirm:
        idea.confirmed_revision_id, idea.confirmed_at, idea.confirmed_by_person_id = idea.current_revision_id, now, actor


def _restore_context(session, venture_id, wid, step, skipped):
    from app.db.supymem_models import IdeaSession
    from app.idea.service import append_revision
    from app.ventures.current_idea import current_revision, revision_fields
    venture = session.get(Venture, venture_id)
    revision = current_revision(venture, session)
    fields = revision_fields(revision)
    if not fields.get("_founder_override"):
        return
    if not ({"clarification", "validation"} & skipped.keys()):
        fields.pop("_founder_override", None)
    now = utcnow()
    idea = IdeaSession(id=new_id("ids"), workspace_id=wid, venture_id=venture_id, case="know",
        state="clarifying" if step == "clarification" else "reviewing", created_at=now, updated_at=now)
    session.add(idea)
    session.flush()
    append_revision(session, idea, fields, source="founder", summary=revision.summary)

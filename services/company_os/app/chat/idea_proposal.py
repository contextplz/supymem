"""In-chat initial approval ("Looks right") and Edit for a reviewable idea proposal.

The clarifier only shows a proposal. GStack starts solely from an explicit
Looks-right bound to the proposal's revision; a replay or double-click finds the
same dedupe-keyed job and never queues or charges a second run. Edit asks for
the correction in chat; the founder's reply goes through the saved clarification
question path, which writes a new revision and re-shows the proposal, so the old
Looks-right is stale and a fresh one is required. The later final confirmation
is separate and bound to the saved revision.
"""
from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import select

from app.db.models import Job, Venture
from app.db.supymem_models import ClarifierTurn, IdeaRevision, IdeaSession
from app.state import db
from core.clock import utcnow
from core.ids import new_id

EDIT_QUESTION = ("What should change in this proposal? Type the correction here, for example the customer, "
                 "problem or offer. I will update the idea and show it again for a fresh Looks right. "
                 "Nothing has started yet.")
GSTACK_METHODS = ["gstack_office_hours", "gstack_ceo_review", "gbrain_think"]


# shortcut: explicit English requests to read your saved idea, extend when other phrasing is missed.
_READ_IDEA = re.compile(r"\s*(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+)?(?:"
    r"(?:show(?:\s+me)?|read(?:\s+me)?)\s+(?:(?:the|my|our|this)\s+)?(?:summary|recap)\s+of\s+"
    r"(?:my|our|this|the\s+current)\s+idea|(?:summari[sz]e|recap)\s+(?:my|our|this|the\s+current)\s+idea)\s*[.!?]?\s*", re.I)
_REVIEW_IDEA = re.compile(r"\s*(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+)?"
    r"review\s+(?:my|our|this|the(?:\s+current)?)\s+(?:saved\s+)?idea"
    r"(?:\s+(?:before|so)\s+(?:i|we)\s+(?:(?:can|could)\s+)?(?:confirm|approve|accept)"
    r"(?:\s+(?:it|this|that|(?:my|our|this|the)\s+idea))?)?\s*[.!?]?\s*", re.I)


def requested_saved_review(venture_id: str, text: str) -> bool:
    reading = _READ_IDEA.fullmatch(text)
    if not reading and not _REVIEW_IDEA.fullmatch(text):
        return False
    with db()() as session:
        venture = session.get(Venture, venture_id)
        if not venture or not venture.workspace_id:
            return False
        idea = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == venture_id,
            IdeaSession.workspace_id == venture.workspace_id, IdeaSession.state != "abandoned")
            .order_by(IdeaSession.created_at.desc()).limit(1))
        if not idea or not idea.current_revision_id:
            return False
        return True


# shortcut: English confirmation directives, extend when missed founder requests provide evidence.
_SUMMARY_OK = re.compile(r"(?:(?:yes|yeah|yep)[,\s]+)?(?:this|that|the|my)\s+(?:summary|recap|proposal|description)\s+"
                         r"(?:looks?|is|seems?|sounds?)\s+(?:right|correct|accurate|good|fine)(?:\s+to\s+me)?[.!]*", re.I)
_THIS_OK = re.compile(r"(?:(?:yes|yeah|yep)[,\s]+)?(?:this|that|it)\s+(?:looks?|is|seems?|sounds?)\s+"
                      r"(?:right|correct|accurate|good|fine)(?:\s+to\s+me)?[.!]*", re.I)
_IDEA_CHOSEN = re.compile(r"(?:(?:yes|yeah|yep)[,\s]+)?(?:this|that)\s+is\s+(?:the|my|our)\s+idea\s+(?:i|we)\s+"
                          r"(?:want\s+to|would\s+like\s+to|will)\s+(?:pursue|build|go\s+with|work\s+on)[.!]*", re.I)
_IDEA_OK = re.compile(r"(?:(?:yes|yeah|yep)[,\s]+)?(?:this|that|it|(?:this|that|the|my)\s+"
                      r"(?:summary|recap|proposal|description))\s+(?:captures?|matches?|describes?)\s+"
                      r"(?:the|my|our|this)\s+idea[.!]*", re.I)
_IDEA_COMMAND = re.compile(r"\b(?:confirm|approve|accept)(?:\s+(?:(?:(?:my|the|this|our)\s+)?"
                          r"(?:idea|summary)|this|that|it|now|yet)\b|\s*(?:[.!;]|$))", re.I)
# "Confirm my idea, and do not search" limits what follows the confirmation; it does not take it back.
_LIMIT_AFTER = re.compile(r",?\s*(?:(?:and|but)\s+(?:please\s+)?(?:do\s+not|don['’]t|never)\b|without\b)[^.!;\n]*[.!;]?\s*$", re.I)


def clear_approval(text: str, *, question_showing: bool = False) -> bool:
    """The founder plainly approves their idea. "This looks right" counts only when no question could be its subject."""
    from app.core.local_mode import is_local

    if re.search(r'["“”`]|(?<!\w)[\x27‘’]', text, re.I) or not is_local() and re.search(r"\bskip\b", text, re.I):
        return False
    stripped = text.strip()
    if stripped.casefold().rstrip(".!") == "confirm" or _SUMMARY_OK.fullmatch(stripped) or _IDEA_CHOSEN.fullmatch(stripped):
        return True
    if not question_showing and _THIS_OK.fullmatch(stripped):
        return True
    approved = False
    for sentence in re.split(r"(?<=[.!;\n])\s*", text):
        conditional = re.search(r"\b(?:unless|if|when|once|after|before|until|assuming|provided|as\s+long\s+as)\b", sentence, re.I)
        limitation = _LIMIT_AFTER.search(sentence)
        if limitation and _IDEA_COMMAND.search(limitation.group()):
            return False
        sentence = _LIMIT_AFTER.sub("", sentence)
        if conditional or re.search(r"\b(?:not|never)\b|['\"?]|\bdon['’]t\b", sentence, re.I):
            if _IDEA_COMMAND.search(sentence):
                return False
            continue
        if _SUMMARY_OK.fullmatch(sentence.strip()) or _IDEA_CHOSEN.fullmatch(sentence.strip()) or _IDEA_OK.fullmatch(sentence.strip()):
            approved = True
        elif re.search(r"(?:^|\band\s+|,\s*)(?:(?:please|just)\s+)*"
                     r"(?:I\s+(?:(?:want|would\s+like)\s+to\s+)?)?(?:confirm|approve|accept)\s+"
                     r"(?:(?:(?:my|the|this|our)\s+)?(?:idea|summary)|this|that|it)(?:\s+now)?"
                     + (r"(?:\s+(?:using|with|based\s+on)\s+[^.!;?]+)?" if is_local() else "")
                     + r"\s*(?:[.!;]|$)", sentence, re.I):
            approved = True
    return approved


def typed_confirm(venture_id: str, actor_subject: str | None) -> dict[str, Any]:
    from app.chat.execute import _confirm_idea
    from app.core.jobs import ACTIVE_JOB_STATUSES
    from app.idea.service import get_session, loads
    from app.intake.evidence import INPUT_KIND

    with db()() as session:
        venture = session.get(Venture, venture_id)
        row = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == venture_id,
            IdeaSession.workspace_id == venture.workspace_id, IdeaSession.state != "abandoned")
            .order_by(IdeaSession.created_at.desc()).limit(1)) if venture else None
        saved = get_session(row.id) if row and row.current_revision_id else None
        working = row and any(loads(job.payload_json).get("session_id") == row.id
            and (job.kind == "idea.clarify_turn" or loads(job.payload_json).get("revision_id") == row.current_revision_id)
            for job in session.scalars(select(Job).where(Job.venture_id == venture_id,
                Job.workspace_id == row.workspace_id, Job.kind.in_(["idea.clarify_turn", "idea.validate"]),
                Job.status.in_(ACTIVE_JOB_STATUSES))))
    if working:
        return confirmation_pending()
    if not saved:
        reply = review(venture_id)
        return {**reply, "body_md": "There is no saved idea to confirm yet. " + reply["body_md"]}
    if INPUT_KIND.get() == "voice":  # voice never confirms on its own, the same as every other card
        return _reply("Choose the confirmation on screen to continue. Nothing was started.", actions=[{
            "label": "Confirm my idea", "action": "idea.confirm", "style": "primary",
            "payload": {"session_id": saved["id"], "revision_id": saved["current_revision"]["id"], "skip_open_questions": True}}])
    return _confirm_idea(venture_id, {"session_id": saved["id"], "revision_id": saved["current_revision"]["id"],
                                    "skip_open_questions": True}, actor_subject)


def confirmation_pending() -> dict[str, Any]:
    return _reply("I'm still working on your idea, so it is not confirmed yet. I will not confirm it automatically. "
                  "When your summary is ready, tell me to confirm it.", actions=[
                      {"label": "Check your summary", "action": "idea.review", "style": "primary"}])


def confirmation_payload(venture_id: str, text: str) -> dict[str, str] | None:
    from app.ventures.current_idea import current_revision

    if not clear_approval(text):
        return None
    with db()() as session:
        venture = session.get(Venture, venture_id)
        revision = current_revision(venture, session) if venture else None
        row = session.get(IdeaSession, revision.session_id) if revision else None
        if row is None:
            return None
        return {"session_id": row.id, "revision_id": revision.id}


def _claims_confirmed_opening(body: str) -> bool:
    """A reply opening "Confirmed: ..." to a founder asking about their idea claims the idea is confirmed."""
    from app.chat.rephrase import INPUT

    return (bool(re.match(r"\W*(?:idea\s+)?confirmed\s*[:.!—–-]", re.sub(r"<!--.*?-->", "", body, flags=re.S), re.I))
            and bool(re.search(r"\b(?:confirm\w*|idea|summary)\b", str((INPUT.get() or {}).get("original") or ""), re.I)))


def guard_confirmation(venture_id: str, reply: dict[str, Any]) -> dict[str, Any]:
    from app.chat.reply_safety import _normalized
    from app.ventures.current_idea import current_revision

    body = str(reply.get("body_md") or "")
    # Only a claim that it happened: "for the confirmed idea" in a notice is not one.
    if not re.search(r"\bconfirmed\s+idea\s+(?:is|has)\b|\bidea(?:\s+summary)?\s+(?:is|has\s+been)\s+confirmed\b|"
                     r"\bI(?:['’]ve|\s+have)?\s+(?:just\s+)?confirmed\s+(?:(?:your|the|this)\s+)?idea\b",
                     _normalized(body), re.I) and not _claims_confirmed_opening(body):
        return reply
    with db()() as session:
        venture = session.get(Venture, venture_id)
        revision = current_revision(venture, session) if venture else None
        row = session.get(IdeaSession, revision.session_id) if revision else None
        if row and row.state == "confirmed" and row.confirmed_revision_id == revision.id:
            return reply
    safe = _reply("Your idea is not confirmed yet. Review your saved idea, then tell me to confirm it.",
                  actions=[{"label": "Review your idea", "action": "idea.review", "style": "primary"}])
    return {**reply, **safe, "content": safe["body_md"], "parts": [{"type": "text", "text": safe["body_md"]}]}


def question_open(turn: dict | None, state: str) -> bool:
    """The founder can answer this turn; the same states pending_question accepts."""
    return bool(turn and turn["role"] == "agent" and turn["kind"] == "question" and state not in {"confirmed", "abandoned"})


def review(venture_id: str) -> dict[str, Any]:
    from app.idea.service import get_session
    from app.idea.runner import _question_reply
    from app.idea.validation_agent import _plain_strings, review_markdown
    from core.copy import founder_copy

    with db()() as session:
        venture = session.get(Venture, venture_id)
        row = session.scalar(select(IdeaSession).where(
            IdeaSession.venture_id == venture_id, IdeaSession.workspace_id == venture.workspace_id,
            IdeaSession.state != "abandoned").order_by(IdeaSession.created_at.desc()).limit(1)) if venture else None
        if row is None:
            return _reply("Tell me the customer, problem or offer you have in mind. I will help you fill in your idea.")
        saved = get_session(row.id)
    revision = saved.get("current_revision") or {}
    lines = [founder_copy(saved.get("idea_summary") or revision.get("summary") or "Your saved idea")]
    actions = []
    latest = (saved.get("turns") or [])[-1:]
    turn = latest[0] if latest else None
    if question_open(turn, saved["state"]):
        if (turn.get("options") or {}).get("phase") == "gstack":
            from app.gstack.office_hours import question_reply
            body, actions = question_reply(turn["id"])
        else:
            body, actions = _question_reply(turn["text"], turn.get("options") or {}, turn["id"],
                                            facts=(revision.get("structured") or {}).get("founder_facts", {}))
        return _reply("\n\n".join([*lines, founder_copy(body)]), "idea_clarification_ready", actions)
    if turn and turn["role"] == "agent" and turn["kind"] == "proposal" and saved["state"] == "reviewing":
        lines.append(founder_copy(turn["text"]))
        actions = proposal_actions(saved["id"], revision["id"])
    elif saved["state"] != "confirmed" and (failed := _failed_clarification(row)):
        from app.chat import failed_jobs
        from app.gstack.office_hours import SKIP
        lines.append(failed_jobs.body("idea.clarify_turn", failed) + " Retry, or skip the questions and review what you shared.")
        actions = [*failed_jobs.failure_actions(venture_id, "idea.clarify_turn", failed),
                   {"label": SKIP, "action": "chat.reply", "text": SKIP, "payload": {"text": SKIP}, "style": "secondary"}]
    reports = set()
    for run in saved.get("validation_runs") or []:
        if run["state"] != "complete" or run["revision_id"] != revision.get("id"):
            continue
        report = _plain_strings(run["report"])
        key = json.dumps(report, sort_keys=True)
        if key in reports:
            continue
        if not reports:
            lines = []
        reports.add(key)
        lines.append(review_markdown(report, revision.get("structured")))
        for test in report.get("proposed_tests") or []:
            actions.append({"label": "Design an experiment", "action": "experiment.recommendation.prepare",
                            "payload": {"hypothesis": test["hypothesis"], "run": False}, "style": "primary"})
    if reports:
        actions = [action for action in actions if action["action"] == "experiment.recommendation.prepare"]
        if saved["state"] != "confirmed":
            actions.append({"label": "Accept this review", "action": "idea.confirm", "payload": {
                "session_id": saved["id"], "revision_id": revision["id"]}})
    actions.append({"label": "Show experiments here", "action": "experiment.list"})
    return _reply("\n\n".join(lines), actions=actions)


def _failed_clarification(row: IdeaSession) -> str | None:
    """The error code when this idea's newest clarification turn died, so it can never finish on its own."""
    from app.db.models import Job
    from app.idea.service import loads

    with db()() as session:
        job = session.scalar(select(Job).where(Job.venture_id == row.venture_id, Job.workspace_id == row.workspace_id,
            Job.kind == "idea.clarify_turn").order_by(Job.created_at.desc(), Job.id.desc()).limit(1))
    if job is None or job.status not in {"failed", "failed_dead", "poison"} or loads(job.payload_json).get("session_id") != row.id:
        return None
    return job.last_error or "job_failed"


def proposal_actions(session_id: str, revision_id: str) -> list[dict[str, Any]]:
    payload = {"session_id": session_id, "revision_id": revision_id}
    return [{"label": "Looks right", "action": "idea.proposal.approve", "payload": payload, "style": "primary"},
            {"label": "Edit", "action": "idea.proposal.edit", "payload": payload, "style": "secondary"}]


def gstack_dedupe_key(session_id: str, revision_id: str) -> str:
    return f"idea.validate:{session_id}:{revision_id}"


def _reply(body: str, kind: str = "text", actions: list | None = None) -> dict[str, Any]:
    return {"kind": kind, "body_md": body, "actions": actions or [], "citations": []}


_STALE = _reply("That proposal was replaced by a newer version, so I did not start anything. "
                "Use the buttons on the latest proposal.", "needs_you")


def _locked(session, venture_id: str, payload: dict) -> tuple[IdeaSession, str, ClarifierTurn | None]:
    sid, rid = str(payload.get("session_id") or ""), str(payload.get("revision_id") or "")
    row = session.get(IdeaSession, sid, with_for_update=True) if sid and rid else None
    if row is None or row.venture_id != venture_id:
        raise KeyError("idea_session")
    latest = session.scalar(select(ClarifierTurn).where(ClarifierTurn.session_id == row.id)
                            .order_by(ClarifierTurn.turn.desc()).limit(1))
    return row, rid, latest


def _is_open_proposal(row: IdeaSession, rid: str, latest: ClarifierTurn | None) -> bool:
    return (row.state == "reviewing" and row.current_revision_id == rid and latest is not None
            and latest.role == "agent" and latest.kind == "proposal" and latest.revision_id == rid)


def approve(venture_id: str, payload: dict) -> dict[str, Any]:
    from app.core.fsm import advance
    from app.core.jobs import enqueue

    with db()() as session:
        row, rid, latest = _locked(session, venture_id, payload)
        key = gstack_dedupe_key(row.id, rid)
        if session.scalar(select(Job.id).where(Job.dedupe_key == key)) is not None:
            session.commit()
            return _reply("You already approved this proposal, so I did not start it again. "
                          "The idea review continues in this chat.", "idea_clarification_pending")
        if not _is_open_proposal(row, rid, latest):
            session.commit()
            return _STALE
        advance(session, row, "clarifying")
        advance(session, row, "validating")
        enqueue(session, "idea.validate", {"session_id": row.id, "revision_id": rid, "methods": list(GSTACK_METHODS),
                                           "stage": "gstack_questions"},
                workspace_id=row.workspace_id, venture_id=row.venture_id, dedupe_key=key)
        from app.idea.service import loads

        revision = session.get(IdeaRevision, rid)
        structured = loads(revision.structured) if revision else {}
        session.commit()
    from app.knowledge.pmf import idea_references

    reply = _reply("Thanks. I'm starting the review of this proposal. I'll ask a few questions "
                  "here in chat; you can answer or skip them. Nothing was built or sent.", "idea_clarification_pending")
    reply["citations"] = idea_references(structured)
    return reply


def edit(venture_id: str, payload: dict) -> dict[str, Any]:
    from app.idea.service import _next_turn_number

    with db()() as session:
        row, rid, latest = _locked(session, venture_id, payload)
        if (latest is not None and latest.role == "agent" and latest.kind == "question"
                and latest.text == EDIT_QUESTION and latest.revision_id == rid and row.current_revision_id == rid):
            session.commit()
            return _reply(EDIT_QUESTION, "idea_clarification_ready")  # replayed Edit: same saved question
        if not _is_open_proposal(row, rid, latest):
            session.commit()
            return _STALE
        # A saved agent question makes the founder's next chat message the
        # correction (chat.idea_bridge.pending_question -> idea.service.post_message).
        session.add(ClarifierTurn(id=new_id("ict"), session_id=row.id, workspace_id=row.workspace_id,
                                  venture_id=row.venture_id, turn=_next_turn_number(session, row.id),
                                  role="agent", kind="question", text=EDIT_QUESTION, revision_id=rid, created_at=utcnow()))
        session.commit()
    return _reply(EDIT_QUESTION, "idea_clarification_ready")

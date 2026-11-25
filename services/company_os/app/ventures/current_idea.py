"""The latest scoped idea revision is the current brief; creation text is a fallback."""
from __future__ import annotations

import json
import re
from sqlalchemy import select
from app.db.supymem_models import ClarifierTurn, IdeaRevision, IdeaSession
from app.state import db
from core.copy import founder_copy


def intake_pending(venture_id, *, session=None):
    from app.db.models import Venture
    from app.ventures.build_prerequisites import answers_pending
    from app.ventures.journey_overrides import read

    if session is None:
        with db()() as reader:
            return intake_pending(venture_id, session=reader)
    venture = session.get(Venture, venture_id)
    if not venture:
        raise KeyError("venture")
    if answers_pending(session, venture):
        return True
    if "clarification" in read(venture_id, session=session)["skipped"]:
        return False
    idea = session.scalar(select(IdeaSession).where(
        IdeaSession.workspace_id == venture.workspace_id, IdeaSession.venture_id == venture_id,
        IdeaSession.state != "abandoned",
    ).order_by(IdeaSession.updated_at.desc(), IdeaSession.created_at.desc(), IdeaSession.id.desc()).limit(1))
    return not idea or idea.state != "confirmed" or idea.current_revision_id != idea.confirmed_revision_id


def skipped_questions_confirmed(venture, session):
    """Skipping the questions and confirming that exact summary finishes intake; a later edit reopens it."""
    revision = current_revision(venture, session)
    idea = session.get(IdeaSession, revision.session_id) if revision else None
    return bool(idea and idea.state == "confirmed" and idea.confirmed_revision_id == revision.id
                and revision_fields(revision).get("_questions_skipped"))


def current_revision(venture, session):
    idea = session.scalar(select(IdeaSession).where(
        IdeaSession.workspace_id == venture.workspace_id,
        IdeaSession.venture_id == venture.id, IdeaSession.state != "abandoned",
    ).order_by(IdeaSession.updated_at.desc(), IdeaSession.created_at.desc(), IdeaSession.id.desc()).limit(1))
    revision = session.get(IdeaRevision, idea.current_revision_id) if idea and idea.current_revision_id else None
    if revision and (revision.workspace_id, revision.venture_id, revision.session_id) == (venture.workspace_id, venture.id, idea.id):
        return revision
    return None


def revision_fields(revision):
    try:
        fields = json.loads(revision.structured or "{}") if revision else {}
    except (TypeError, ValueError):
        return {}
    return fields if isinstance(fields, dict) else {}


def _is_idea_control(text):
    from app.idea.agent import is_options_request
    from app.idea.service import CHIPS
    return (text.strip().casefold().rstrip(".!?") in {choice.casefold() for choice in CHIPS} | {"just propose", "propose now"}
        or is_options_request(text)
        or bool(re.fullmatch(r"(?:suggest|propose|give|show)\b"
            r"(?=[^.!?\n]*\b(?:ideas?|directions?|options?|choices?)\b)(?=[^.!?\n]*\b(?:me|myself)\b)"
            r"[^.!?\n]+[.!?]*", text, re.I)))


def latest_founder_statement(revision, session):
    if not revision or revision.source != "founder":
        return ""
    turn = session.scalar(select(ClarifierTurn).where(
        ClarifierTurn.workspace_id == revision.workspace_id, ClarifierTurn.venture_id == revision.venture_id,
        ClarifierTurn.session_id == revision.session_id, ClarifierTurn.role == "founder",
    ).order_by(ClarifierTurn.turn.desc()).limit(1))
    if not turn or turn.kind == "chip" or _is_idea_control(turn.text):
        return ""
    # New writes bind the exact source. For older pending copies, unchanged fields
    # and a matching saved source identify an unparsed founder revision.
    same_write = (turn.revision_id is None and revision.diff_from_prev == "{}"
                  and revision.summary == turn.text[:200]
                  and turn.created_at.replace(tzinfo=None) == revision.created_at.replace(tzinfo=None))
    if turn.revision_id == revision.id or same_write:
        return turn.text
    return ""


def current_idea(venture, *, session=None, for_display=False) -> str:
    if session is None:
        with db()() as reader:
            return current_idea(venture, session=reader, for_display=for_display)
    revision = current_revision(venture, session)
    fields = revision_fields(revision)
    from app.ventures.startup_review import effective_fields
    fields = effective_fields(session, venture, fields)
    founder = latest_founder_statement(revision, session)
    from app.idea.agent import founder_description
    # Prompts keep every joined answer; only what founders read drops the labels.
    shown = founder_description if for_display else str
    if for_display and founder:
        return shown(founder)
    parts = (["Latest founder statement (wins over earlier fields): " + founder, "Earlier fields (may be outdated):"] if founder else [])
    # Structured fields can contain a correction newer than the summary text.
    for key in ("customer", "offer", "problem", "first_user_path", "pricing_hypothesis", "workflow", "desired_outcome", "constraints", "requirements"):
        value = fields.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(f"{key.replace('_', ' ').title()}: {shown(value.strip())}")
        elif isinstance(value, list) and value:
            values = [item for item in value if isinstance(item, str) and item.strip()]
            if values:
                parts.append(f"{key.title()}: {'; '.join(values)}")
    if founder and not fields.get("problem") and venture.raw_idea and venture.raw_idea != founder:
        parts.append("Earlier creation brief (may be outdated): " + venture.raw_idea)
    if for_display and not parts and isinstance(fields.get("summary"), str) and fields["summary"].strip():
        return shown(fields["summary"].strip())
    if not founder and revision and not fields.get("_missing_idea_summary") and (revision.summary or "").strip() and not _is_idea_control(revision.summary) and not (for_display and parts):
        parts.append(("Summary (current fields above win on conflict): " if parts else "") + shown(revision.summary.strip()))
    return "\n".join(parts) if parts else shown(venture.raw_idea or "")


_PERSONAL = re.compile(r"(?:i|i['’]m|we|we['’]re|my|our|please)\b", re.I)
_REQUEST = re.compile(r"(?:please|can you|could you|help me)\b", re.I)
_LEAD_IN = re.compile(r"(?:(?:i['’]?m|i am|we['’]?re|we are)\s+[a-z]+\s+(?=(?:an?|the|my|our)\b)"
                      r"|(?:i|we)\s+(?:want|plan|need|hope|would like)\s+to\s+[a-z]+\s+)", re.I)


def _restated(text):
    """A pasted paragraph is not a summary: keep the sentence naming the product, without the founder's first person."""
    from app.idea.agent import core_answer_status
    sentences = [part.strip() for part in re.split(r"\n+|(?<=[.!?])\s+(?=[A-Z])", text) if part.strip()]
    # Saved fields and model summaries are not first-person prose, so they stay whole.
    # shortcut: English first-person and product-for-audience cues, use a model summary for other phrasing.
    if not any(_PERSONAL.match(sentence) for sentence in sentences):
        return text
    kept = [sentence for sentence in sentences if "://" not in sentence and not _REQUEST.match(sentence)] or sentences
    products = [match[0] for sentence in kept if not _PERSONAL.match(sentence)
                and (match := re.search(r"\b(?:an?|the)\s+[^.!?\n]+?\bfor\s+\S[^.!?\n]*[.!?]?", sentence, re.I))]
    best = next((sentence for sentence in kept if _LEAD_IN.match(sentence)), None) or next(iter(products), None) or next(
        (sentence for sentence in kept if core_answer_status(sentence, 0, field="offer") == (True, False)), kept[0])
    best = _LEAD_IN.sub("", best, count=1)
    return best[:1].upper() + best[1:]


def idea_summary(venture, *, session=None) -> str:
    from app.idea.agent import summary_without_unsure
    return summary_without_unsure(founder_copy(_restated(current_idea(venture, session=session, for_display=True))))


def pitch_line(venture, *, session=None) -> str:
    """What the venture makes, for outreach copy. The raw opening can be a request to us ("Please find 20 buyers...")."""
    if session is None:
        with db()() as reader:
            return pitch_line(venture, session=reader)
    offer = revision_fields(current_revision(venture, session)).get("offer")
    return _restated(offer.strip()) if isinstance(offer, str) and offer.strip() else idea_summary(venture, session=session)

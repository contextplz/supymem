"""'Start a new venture: <idea>' from any venture chat uses the real entry flow (entry.chat_start).

Preview makes a proposal; the requested-work path creates a NEW venture. The venture whose chat
this is is never touched, and nothing here invents rules (no archiving, no one-active limit): the only limit is the
beta venture quota that chat_start already enforces.
"""
from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any

from app.chat.actions import get_action
from app.chat.confirm import ConfirmationCard

NEW_VENTURE_RE = re.compile(
    r"^\s*(?:please\s+)?(?:(?:i\s+(?:want|would\s+like|'d\s+like)\s+to|let'?s|can\s+(?:you|we)|could\s+(?:you|we))\s+)?"
    r"(?:start|create|begin|launch|open|make|set\s+up|add)\s+(?:a\s+|an\s+|one\s+)?(?:brand[\s-]+)?(?:new|another|second|fresh)\s+(?:venture|startup|company)\b"
    r"(?:\s*[:\-,.]\s*|\s+(?:for|about|called|named)\s+|\s+)?(?P<idea>.*)$", re.I | re.S)


def idea_of(text: str) -> str | None:
    """None when this is not a new-venture command; '' when it is but names no idea."""
    m = NEW_VENTURE_RE.match(text or "")
    return None if m is None else m.group("idea").strip()


def context_history(history: list[dict]) -> list[dict]:
    result = []
    separate = False
    requests = {}
    for message in history:
        if message.get("role") in {"user", "founder"}:
            separate = message.get("kind") == "new_venture" or idea_of(str(message.get("body_md") or "")) is not None
            if message.get("id"):
                requests[message["id"]] = separate
        excluded = separate or message.get("kind") == "new_venture"
        if message.get("role") == "assistant":
            reply_to = message.get("reply_to_message_id") or next((cite["reply_to_message_id"]
                for cite in json.loads(message.get("citations_json") or "[]")
                if isinstance(cite, dict) and cite.get("reply_to_message_id")), None)
            if reply_to:
                # shortcut: replies whose source is clipped are omitted; include the source turn to retain them.
                excluded = message.get("kind") == "new_venture" or requests.get(reply_to, True)
        if not excluded:
            result.append(message)
    return result


def _reply(body: str, actions: list | None = None, kind: str = "text") -> dict[str, Any]:
    return {"kind": kind, "body_md": body, "actions": actions or [], "citations": []}


_ASKED = "new_venture_asked:{}"


def pending_idea(venture_id: str, text: str, *, consume: bool = True) -> str | None:
    """The founder's plain answer to Charlie's new-venture question, joined to what they said before; else None."""
    import time

    from app.state import MEM

    asked = MEM.get(_ASKED.format(venture_id))
    raw = (text or "").strip()
    if not isinstance(asked, dict) or time.time() - asked.get("at", 0) > 1800 or not raw or len(raw) > 1500:
        return None
    from app.chat.customers import last_reply_asked

    if not last_reply_asked(venture_id, ("Just reply here; I have not created anything",)):
        return None
    from app.chat.intent import classify_intent

    if idea_of(raw) is not None:
        return None  # a fresh "start a new venture: ..." routes normally
    if classify_intent(raw) not in {"general", "assistant", "change", "status"} or re.fullmatch(r"(?:cancel|stop|never ?mind|no)\W*", raw, re.I):
        if consume:
            MEM.pop(_ASKED.format(venture_id), None)
        return None
    if consume:
        MEM.pop(_ASKED.format(venture_id), None)
    before = str(asked.get("idea") or "")
    return f"{before}\nClarification: {raw}" if before else raw


def _ask(venture_id: str, idea: str, question: str) -> dict[str, Any]:
    import time

    from app.state import MEM

    MEM[_ASKED.format(venture_id)] = {"idea": idea[:1200], "at": time.time()}
    return {**_reply(f"{question} Just reply here; I have not created anything and this venture is unchanged."),
            "citations": [{"question": True}]}


def prepare(venture_id: str, text: str, actor_subject: str | None, *, idea: str | None = None) -> dict[str, Any]:
    from app.authz.workspace import ensure_subject_person
    from app.entry import chat_start

    idea = idea if idea is not None else (idea_of(text) or "")
    actor = ensure_subject_person(actor_subject)
    try:
        chat_start._assert_can_start(actor)  # the beta venture limit, same check the start screen uses
        if len(idea) < 12:
            return _ask(venture_id, idea, "What is the new venture? A rough line is fine, like \"app for my mom's bakery orders\".")
        out = chat_start.preview(text=idea, actor_id=actor)
    except chat_start.ChatStartError as exc:
        return _reply(f"{exc.message} Nothing was created and your current venture is unchanged. Open All ventures or describe a different idea.")
    if out.get("status") == "reply":
        return _reply(out["body_md"], out["actions"])
    if out.get("status") != "proposal":
        return _ask(venture_id, idea, out.get("question") or "Tell me a bit more about the new venture.")
    prop = out["proposal"]
    card = replace(ConfirmationCard.for_action(
        get_action("venture.start_new"), interpretation="Create a NEW venture from this idea.",
        consequences=["A separate new venture is created. This venture is not changed, archived or replaced.",
                      "Beta accounts can have up to 2 ventures; the new one counts toward that."],
        resulting_state={"proposal_id": prop["id"]}), confirm_label="Create new venture")
    return _reply(f"Create a new venture from this idea? {idea[:240]}\n\nThis venture stays exactly as it is.",
                  [card.as_action()], "confirmation")


async def execute(venture_id: str, payload: dict[str, Any], actor_subject: str | None) -> dict[str, Any]:
    from app.authz.workspace import ensure_subject_person, assert_venture_access
    from app.entry import chat_start
    from app.db.supymem_models import EntryChatProposal
    from app.state import db

    actor = ensure_subject_person(actor_subject)
    proposal_id = str(payload.get("proposal_id") or "")
    try:
        receipt = await chat_start.confirm(proposal_id, actor)
    except Exception as exc:
        with db()() as session:
            proposal = session.get(EntryChatProposal, proposal_id)
            destination = proposal.target_venture_id if proposal and proposal.actor_id == actor else None
        if destination:
            assert_venture_access(destination, actor)
            from app.chat.recovery import log_founder_exception
            log_founder_exception(exc, venture_id, "venture.start_new")
            return _reply("Your new venture was created, but I could not finish your reply. Open it to check your saved work before retrying this same request.",
                          [{"label": "Open new venture", "href": f"/v/{destination}", "style": "primary"}])
        if isinstance(exc, chat_start.ChatStartError):
            return _reply(f"{exc.message} Nothing was created. Open All ventures or describe a different idea.")
        raise
    new_id = receipt.get("venture_id")
    return _reply("Your new venture is created. Open it to continue there.",
                  [{"label": "Open new venture", "href": f"/v/{new_id}", "style": "primary"}] if new_id else [])

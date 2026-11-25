"""Answer "how is Acme doing?" / "switch to Acme" for ventures the founder can already access.

This chat controls one venture, so it never acts on the other one: it gives a one-line
saved status and an "Open <name>" link. Only ventures in workspaces the caller may see
are considered; an unknown or inaccessible name returns None so the caller refuses
without confirming that anything by that name exists.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select

from app.chat.actions import question_reply

_VERBS = (r"(?:how(?:'s|\s+is)|how\s+are|what\s+about|what(?:'s|\s+is)\s+(?:up\s+with|the\s+status\s+of)|"
          r"status\s+of|update\s+on|switch\s+to|go\s+to|open|show(?:\s+me)?|check\s+on|tell\s+me\s+about)")


def _which(hits: list[tuple[str, str]]) -> dict[str, Any] | None:
    if not hits:
        return None
    names = ", ".join(name for _, name in hits[:5])
    return question_reply(f"Which one do you mean: {names}?")


def portfolio_context(text: str, names: list[str]) -> bool:
    """Supplied venture names may be context for ideation, never a retargeted command."""
    if not (request := re.search(r"\b(?:compare|comparison|portfolio|(?:recommend|suggest|brainstorm)\b[^.!?]{0,100}\b(?:ideas?|startups?|ventures?|products?))\b", text, re.I)):
        return False
    from app.chat.intent import _experiment_intent

    result_text = text if re.match(r"^\s*(?:please\s+)?(?:log|record|save)\b", text, re.IGNORECASE) else text[:request.start()]
    if _experiment_intent(result_text) == "experiment_record":
        return False
    work = r"(?:build|deploy|publish|send|post|schedule|delete|archive|pause|pausing|resume|rename|rewrite|update|change|start|run|cancel|approve|save|edit|switch|open|go)"
    for name in names:
        if not name:
            continue
        target = re.escape(name)
        if re.search(rf"\b{work}\b[^.!?;]{{0,100}}\b{target}(?!\w)|"
                     rf"\b(?:for|within|in|on)\s+{target}(?!\w)[^.!?;]{{0,100}}\b{work}\b", text, re.I):
            return False
    return True


def supplied_portfolio_context(text: str) -> bool:
    """Explicit comparison of supplied concepts, never foreign record access."""
    if not portfolio_context(text, []):
        return False
    if not re.search(r"\b(?:use|using)\s+only\b[^.!?;]{0,100}(?:\b(?:supplied|my|these)\b[^.!?;]{0,50}\b(?:descriptions|facts)\b|\b(?:descriptions|facts)\s+supplied\b)", text, re.I):
        return False
    clauses = []
    for clause in re.split(r"[.!?;]\s*", text):
        clause = clause.strip()
        if re.match(r"^(?:do not|don['’]?t)\b|^no\s+(?:search|research|build|publish|sending|execution|effects)\b", clause, re.I):
            if re.search(r"\b(?:but|then|instead|however)\b", clause, re.I):
                return False
            continue
        clauses.append(clause)
    active = ". ".join(clauses)
    # Do not allow a positive read/effect hidden beside the supplied facts.
    if re.search(r"\b(?:inspect|retrieve|fetch|query|access|read|show|list|open|switch|check|search|research|build|deploy|publish|send|post|schedule|delete|archive|pause|resume|rename|rewrite|start|run|cancel|approve|save|edit|execute|contact|create|record)\b", active, re.I):
        return False
    if re.search(r"\b(?:other|another|foreign)\s+(?:founders?|users?|customers?|companies|workspaces?|ventures?)\b|\bven_\w+", active, re.I):
        return False
    # Foreign identifiers stay refused even when placed in a negated clause.
    return not re.search(r"\bven_\w+", text, re.I)


def other_venture_reply(venture_id: str, text: str, actor_subject: str | None) -> dict[str, Any] | None:
    if actor_subject and re.match(r"^\s*(?:please\s+)?(?:switch\s+to|open|go\s+to)\b", text, re.I):
        from app.entry.chat_start import _venture_switch

        if switch := _venture_switch(text, actor_subject):
            return {"kind": "navigation", "body_md": switch["body_md"], "actions": switch["actions"], "citations": []}
    from app.authz.workspace import accessible_workspace_ids, assert_venture_access
    from app.db.models import Venture
    from app.state import db
    from app.ventures.next_step import next_step
    from app.ventures.state_brief import get_venture_state_brief

    with db()() as session:
        current = session.get(Venture, venture_id)
        if current is None:
            return None
        scope = accessible_workspace_ids(actor_subject) if actor_subject else {current.workspace_id}
        rows = session.scalars(select(Venture).where(
            Venture.workspace_id.in_(scope), Venture.archived_at.is_(None), Venture.id != venture_id)).all()
        others = [(v.id, (v.name or "").strip()) for v in rows]
        others_ws = [(v.id, (v.name or "").strip(), v.workspace_id) for v in rows]
    mentioned = [(vid, name) for vid, name in others if len(name) >= 3 and re.search(
        rf"\b{re.escape(name)}(?!\w)", text, re.I)]
    if mentioned and portfolio_context(text, [name for _, name in mentioned]):
        return None  # founder-supplied comparison context; no other venture state is read
    hits = [(vid, name) for vid, name in others if name and re.search(
        rf"\b{_VERBS}\s+(?:the\s+|my\s+|our\s+)?{re.escape(name)}(?!\w)", text, re.I)]
    if not hits:
        # Any plain mention (also how voice phrases it) gets "switch, then ask again", never a bare refusal.
        hits = [(vid, name) for vid, name in others if len(name) >= 3 and re.search(
            rf"\b{re.escape(name)}(?!\w)", text, re.I)]
        if len(hits) != 1:
            return _which(hits)
        other_id, name = hits[0]
        if actor_subject:
            try:
                assert_venture_access(other_id, actor_subject)
            except Exception as exc:  # noqa: BLE001
                from app.chat.recovery import log_founder_exception
                log_founder_exception(exc, other_id, "other_venture.access")
                return None
        return {"kind": "navigation",
                "body_md": f"That's about {name}. Please switch to it, then ask me again.",
                "actions": [{"label": f"Switch to {name}", "href": f"/v/{other_id}", "style": "primary"}],
                "citations": []}
    if len(hits) > 1:
        return _which(hits)
    other_id, name = hits[0]
    if actor_subject:
        try:
            assert_venture_access(other_id, actor_subject)
        except Exception as exc:  # noqa: BLE001 - an inaccessible venture must look like an unknown one
            from app.chat.recovery import log_founder_exception
            log_founder_exception(exc, other_id, "other_venture.access")
            return None
    from app.tenancy import bind

    workspaces = {vid: wid for vid, _name, wid in others_ws}
    # The brief refuses another venture's scope, so read it inside that venture's own scope.
    with bind(workspace_id=workspaces[other_id], venture_id=other_id, actor=actor_subject or "system"):
        step = next_step(other_id)
        paused = bool((get_venture_state_brief(other_id).get("idea") or {}).get("paused"))
    status = "paused" if paused else f"{step['title']}. {step['detail']}".strip()
    return {"kind": "navigation",
            "body_md": (f"{name}: {status} This chat stays on your current venture; nothing was changed "
                        f"on {name}."),
            "actions": [{"label": f"Open {name}", "href": f"/v/{other_id}", "style": "primary"}],
            "citations": []}

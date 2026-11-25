"""Draft bodies for founder-led copy. Never interpolates a fake personal hook."""

from __future__ import annotations

import json
from typing import Any

from app.gtm.relationships.honesty import check_draft
from app.gtm.relationships.ladder import can_transition
from app.gtm.relationships.score import personalize_decision

_CALENDAR_HINT = ("calendly", "cal.com", "book a time", "calendar.google")


def render_draft(
    *,
    kind: str,
    first_name: str | None,
    quote: str | None,
    trigger_code: str | None,
    trigger_confidence: str | None,
    company: str | None,
    product_name: str | None = None,
    ask_rung: str = "none",
    hook: str | None = None,
) -> dict[str, Any]:
    mode = personalize_decision(
        quote=quote,
        trigger_code=trigger_code,
        trigger_confidence=trigger_confidence,
    )
    target_rung = {
        "connect": "connect",
        "question": "question",
        "value_return": "value_return",
        "call_15": "call_15",
    }.get(kind, "connect")
    ladder = can_transition(ask_rung or "none", target_rung)
    first = (first_name or "").strip() or "there"

    if kind in {"connect", "question", "value_return"} and mode != "ok":
        body = None
        honesty = check_draft(
            body="",
            kind=kind,
            ask_rung=ask_rung,
            product_name=product_name,
            hook=hook,
        )
        return {
            "blocked": True,
            "reason": "personalize_refused",
            "personalize": mode,
            "kind": kind,
            "body": None,
            "honesty": honesty.__dict__,
            "ladder_ok": ladder.ok,
        }

    if kind == "connect" and mode == "ok" and quote:
        body = (
            f"Hey {first},\n\n"
            f"You wrote “{quote}”. That stuck with me.\n\n"
            f"Would be glad to connect; no pitch.\n"
        )
    elif kind == "question" and quote:
        body = (
            f"Hey {first},\n\n"
            f"Still curious about “{quote}”. What’s the painful part of that workflow today?\n"
        )
    elif kind == "value_return":
        body = f"Hey {first},\n\nSharing a one-pager in case it helps. No meeting ask.\n"
    else:
        body = f"Hey {first},\n\nA note about {hook or (company or 'your work')}.\n"

    honesty = check_draft(
        body=body,
        kind=kind,
        ask_rung=ask_rung,
        product_name=product_name,
        hook=hook or quote,
    )
    blocked = honesty.blocked or not ladder.ok
    reason = None
    if honesty.blocked:
        reason = honesty.flags[0] if honesty.flags else "honesty"
    elif not ladder.ok:
        reason = "illegal_ladder"
    return {
        "blocked": blocked,
        "reason": reason,
        "personalize": mode,
        "kind": kind,
        "body": None if blocked else body,
        "honesty": {
            "blocked": honesty.blocked,
            "flags": list(honesty.flags),
            "overridable": honesty.overridable,
        },
        "ladder_ok": ladder.ok,
        "open_url": None,
    }


def body_has_calendar(body: str) -> bool:
    low = (body or "").lower()
    return any(h in low for h in _CALENDAR_HINT)


def dump_honesty(honesty: dict[str, Any]) -> str:
    return json.dumps(honesty)

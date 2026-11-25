"""Concierge chat — 'still stuck' produces fresh diagnosis."""

from __future__ import annotations

from typing import Any


def respond(message: str, *, last_step: dict[str, Any] | None = None) -> dict[str, Any]:
    msg = (message or "").lower()
    if "stuck" in msg or "still" in msg:
        tip = (last_step or {}).get("if_stuck") or "Try a hard refresh, then re-open the deep link."
        return {
            "kind": "diagnosis",
            "text": f"Live diagnosis: {tip}",
            "repeat": False,
            "step_id": (last_step or {}).get("id"),
        }
    return {
        "kind": "step",
        "text": (last_step or {}).get("title") or "Follow the next step",
        "repeat": False,
    }

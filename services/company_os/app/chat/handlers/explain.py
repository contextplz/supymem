"""Explain handler, cite clarification answers / charter."""

from __future__ import annotations

from app.chat.context import ChatContext


def handle(ctx: ChatContext, text: str) -> dict:
    # Audience question
    if "audience" in text.lower():
        ans = next((a for a in ctx.answers if a.get("slot") == "audience"), None)
        if ans and ans.get("chosen"):
            return {
                "kind": "text",
                "body_md": f"I picked that audience because you chose: **{ans['chosen']}**.",
                "citations": [{"label": "Your answer", "href": "#validation"}],
            }
    if ctx.charter:
        return {
            "kind": "text",
            "body_md": (
                f"From the locked plan: {ctx.charter.get('one_liner', '')}. "
                f"Audience: {ctx.charter.get('audience', 'not set')}."
            ),
            "citations": [{"label": "Plan", "href": "#charter"}],
        }
    return {
        "kind": "text",
        "body_md": "No saved decision explains that choice yet; you can continue with a draft, but missing facts stay unknown.",
        "actions": [{"label": "Choose your next step", "action": "next_moves"}],
        "citations": [],
    }

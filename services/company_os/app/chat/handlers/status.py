"""Factual + status handlers. Status questions go through the venture assistant."""

from __future__ import annotations

import re

from app.chat.context import ChatContext
from app.chat.refuse import refuse_unknown


def handle_factual(ctx: ChatContext, text: str) -> dict:
    if re.search(r"\b(draft|write|send|to \w+)\b", text or "", re.I) and re.search(
        r"\b(email|outreach)\b", text or "", re.I
    ):
        return handle(ctx, text)
    if re.search(
        r"(what('?s| is)|where('?s| is)|show me).{0,24}(email|e-mail)|(email|e-mail) address",
        text or "",
        re.I,
    ):
        if ctx.email:
            return {
                "kind": "text",
                "body_md": f"Your address is **{ctx.email}**.",
                "citations": [{"label": "Email", "href": "#email"}],
            }
        return refuse_unknown(ctx, "email is reserved during setup")
    if re.search(r"url|website|site|preview", text, re.I):
        url = ctx.website.get("url")
        if url:
            return {
                "kind": "text",
                "body_md": f"Your preview is at {url}.",
                "citations": [{"label": "Website", "href": url}],
            }
        return refuse_unknown(ctx, "the build finishes")
    if re.search(r"repo|repository", text, re.I):
        repo = next((r for r in ctx.resources if r["resource"] == "repo"), None)
        if repo:
            return {
                "kind": "text",
                "body_md": f"Your repository is **{repo['display']}** (local until GitHub is connected).",
                "citations": [{"label": "Repository", "href": repo.get("url") or "#"}],
            }
        return refuse_unknown(ctx, "provisioning")
    return handle(ctx, text)


def handle(ctx: ChatContext, text: str) -> dict:
    """Sync status path used by execute_action — brief-grounded prose, no card dump."""
    from app.chat.next_moves import compose
    return compose(str((ctx.venture or {}).get("id") or ""))

"""One metered, JSON-returning model call for God mode (DeepSeek V4 Pro through the workspace's own credit meter).

Returns None when the model is unavailable (no credits, no provider, offline): every caller has a templated fallback,
so a missing model never stops the venture.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.db.models import Venture
from app.state import db


async def ask_json(venture_id: str, purpose: str, system: str, prompt: str, schema: dict[str, Any],
                   max_tokens: int = 3000) -> dict[str, Any] | None:
    from app.gtm.supymem import draft

    with db()() as s:
        ven = s.get(Venture, venture_id)
        wid = ven.workspace_id if ven else None
    if not wid:
        return None
    try:
        text, _meta = await asyncio.to_thread(draft._canonical_complete, prompt, workspace_id=wid, purpose=purpose,
                                              system=system, max_tokens=max_tokens, response_schema=schema)
    except Exception:  # noqa: BLE001 - the fallback path always exists
        return None
    return draft._parse_json(text or "")


def brief_text(venture_id: str) -> str:
    """The venture brief (idea, audience, research, GTM diagnosis) as compact text for a prompt."""
    from app.ventures.state_brief import get_venture_state_brief, render_brief_for_llm

    try:
        return render_brief_for_llm(get_venture_state_brief(venture_id))[:6000]
    except Exception:  # noqa: BLE001
        with db()() as s:
            ven = s.get(Venture, venture_id)
        return f"Idea: {(ven.raw_idea if ven else '') or ''}"

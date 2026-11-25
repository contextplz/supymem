"""Content-free, idempotent receipts for the chat-first entry journey."""

from __future__ import annotations

from app.analytics.events import emit_once

# These labels are product-owned, never derived from founder text or proposal copy.
ROUTE_FEATURE = {"startup": "idea", "personal_build": "builds", "gtm": "gtm"}
PREVIEW_ROUTES = frozenset({*ROUTE_FEATURE, "clarify"})


def record_preview(proposal_id: str, actor_id: str, route: str) -> None:
    if route not in PREVIEW_ROUTES:
        return
    emit_once("chat.message", event_key=f"entry:{proposal_id}:preview",
              person_id=actor_id, props={"source": "entry_chat", "route": route})


def record_confirm(proposal_id: str, actor_id: str, workspace_id: str,
                   venture_id: str, route: str) -> None:
    feature = ROUTE_FEATURE.get(route)
    if feature is None:
        return
    emit_once("entry.chat_confirmed", event_key=f"entry:{proposal_id}:confirm",
              person_id=actor_id, workspace_id=workspace_id, venture_id=venture_id,
              props={"source": "entry_chat", "route": route, "feature": feature})

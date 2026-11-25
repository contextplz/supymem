"""Founder control pack: where work lives, export, and a stuck-debug bundle.

The Product Hunt failure mode we are avoiding: a founder cannot find the
landing page, the plan, or the data, cannot pause, and cannot leave with
their assets. This module is the honest answer to those questions.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_, select

from app.db.models import ChatConversation, ChatMessage, PlannedTask, Venture, VentureDocument
from app.state import MEM, db

DOMAIN_POLICY = (
    "Your domain, repo, and hosting stay yours. We only serve a local preview "
    "until you connect infrastructure you control. Leaving never holds your assets."
)


def control_block(
    venture_id: str,
    *,
    website: dict[str, Any] | None = None,
    research_href: str | None = None,
    paused: bool = False,
    credits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    site = website or {}
    return {
        "you_own_this": True,
        "paused": paused,
        "domain_policy": DOMAIN_POLICY,
        "posts_need_you": True,
        "landing": {
            "url": site.get("url"),
            "simulated": bool(site.get("simulated")) if site.get("state") != "empty" else True,
            "href": f"/v/{venture_id}/site",
        },
        "plan_href": f"/v/{venture_id}/charter",
        "research_href": research_href,
        "work_href": f"/v/{venture_id}/tasks",
        "now_href": f"/v/{venture_id}/now",
        "settings_href": f"/v/{venture_id}/settings",
        "export_path": f"/ventures/{venture_id}/export",
        "support_path": f"/ventures/{venture_id}/support-bundle",
        "files": f"ventures/{venture_id}/ on this machine",
        "credits": credits,
    }


def export_pack(venture_id: str) -> dict[str, Any] | None:
    """JSON a founder can download. No secrets, no API keys."""
    factory = db()
    with factory() as session:
        ven = session.get(Venture, venture_id)
        if not ven:
            return None
        from app.ventures.startup_review import PRIVATE_KINDS
        docs = session.scalars(
            select(VentureDocument).where(VentureDocument.venture_id == venture_id, VentureDocument.kind.not_in(PRIVATE_KINDS))
        ).all()
        tasks = session.scalars(
            select(PlannedTask).where(PlannedTask.venture_id == venture_id,
                or_(PlannedTask.workspace_id == ven.workspace_id, PlannedTask.workspace_id.is_(None)))
        ).all()
        name = ven.name
        raw_idea = ven.raw_idea
        stage = ven.stage
        created = ven.created_at.isoformat() + "Z" if ven.created_at else None
        workspace_id = ven.workspace_id
        from app.planning.publication_evidence import public_site_evidence

        publication = public_site_evidence(session, workspace_id=workspace_id, venture_id=venture_id) if workspace_id else None
        doc_items = [{"id": d.id, "kind": d.kind, "title": d.title, "body_md": d.body_md} for d in docs]
        task_items = [
            {
                "id": t.id,
                "title": t.title,
                "detail": t.detail,
                "why_now": t.why_now,
                "status": t.status,
                "runnable": bool(t.runnable) if t.runnable is not None else True,
            }
            for t in tasks
        ]
        from app.chat.conversations import actor_id

        conversations = session.scalars(select(ChatConversation).where(
            ChatConversation.venture_id == venture_id, ChatConversation.workspace_id == workspace_id,
            or_(ChatConversation.visibility == "legacy_shared", ChatConversation.creator_person_id == actor_id()),
        ).order_by(ChatConversation.created_at, ChatConversation.id)).all()
        messages = session.scalars(select(ChatMessage).where(
            ChatMessage.venture_id == venture_id,
            or_(ChatMessage.workspace_id == workspace_id, ChatMessage.workspace_id.is_(None)),
            or_(ChatMessage.conversation_id.in_([row.id for row in conversations]), ChatMessage.conversation_id.is_(None)),
            ChatMessage.role.in_(("user", "founder", "assistant")),
        ).order_by(ChatMessage.created_at, ChatMessage.id)).all()
        conversation_items = [{"id": row.id, "title": row.title,
            "archived_at": row.archived_at.isoformat() if row.archived_at else None,
            "messages": []} for row in conversations]
        by_id = {item["id"]: item for item in conversation_items}
        for message in messages:
            conversation_id = message.conversation_id
            if conversation_id not in by_id:
                item = {"id": conversation_id, "title": "Conversation", "archived_at": None, "messages": []}
                by_id[conversation_id] = item
                conversation_items.append(item)
            by_id[conversation_id]["messages"].append({"id": message.id, "role": message.role,
                "body_md": message.body_md, "created_at": message.created_at.isoformat()})

    from app.ventures.founder_memory import list_founder_memories

    memory_status = {}
    # shortcut: memory recall returns at most 100 items, add paging when exports need older memories.
    memory_items = list_founder_memories(venture_id, limit=100, status=memory_status)
    if len(memory_items) == 100:
        memory_status["availability"] = "partial"
    memory_status["note"] = "Includes up to 100 current saved memories, your saved conversations, task details and available document text. Files on this machine are not bundled."
    if memory_status.get("availability") == "unavailable":
        memory_status["note"] = "Saved memories could not be read. Try downloading again. Your saved conversations, task details and available document text are included. Files on this machine are not bundled."

    charter = MEM.get(f"charter:{venture_id}") or {}
    content = charter.get("content") if isinstance(charter, dict) else {}
    if not isinstance(content, dict):
        content = {}
    website_mem = MEM.get(f"website:{venture_id}") or {}
    cached_stage = website_mem.get("stage")
    website = {
        "url": publication["url"] if publication else website_mem.get("url"),
        "stage": "live" if publication else (None if cached_stage == "live" else cached_stage),
        # Cached flags are not publication receipts. The helper qualifies
        # exact venture deployments or unambiguous legacy workspace sites.
        "published": publication is not None,
    }
    pack = {
        "venture": {
            "id": venture_id,
            "name": name,
            "raw_idea": raw_idea,
            "stage": stage,
            "workspace_id": workspace_id,
            "created_at": created,
            "one_liner": content.get("one_liner"),
        },
        "charter": {
            "one_liner": content.get("one_liner"),
            "wedge": content.get("wedge"),
            "mission": content.get("mission"),
        },
        "website": website,
        "documents": doc_items,
        "conversations": conversation_items,
        "memories": {"items": memory_items, **memory_status},
        "tasks": task_items,
        "files": f"ventures/{venture_id}/ on this machine",
        "domain_policy": DOMAIN_POLICY,
        "you_own_this": True,
    }
    from app.munder.redact import redact_tree

    return redact_tree(pack)


def support_bundle(
    venture_id: str,
    *,
    onboarding: dict[str, Any] | None = None,
    paused_reason: str | None = None,
) -> dict[str, Any] | None:
    """Sanitized snapshot for 'I'm stuck' — no keys, no env."""
    pack = export_pack(venture_id)
    if pack is None:
        return None
    factory = db()
    with factory() as session:
        stuck = session.scalars(
            select(PlannedTask).where(
                PlannedTask.venture_id == venture_id,
                PlannedTask.status.in_(("running", "blocked", "failed", "open")),
            )
        ).all()
        errors = [
            {
                "id": t.id,
                "title": t.title,
                "status": t.status,
                "last_error": (t.last_error or "")[:400] or None,
            }
            for t in stuck
            if t.last_error or t.status in {"running", "blocked", "failed"}
        ]
    return {
        "venture_id": venture_id,
        "name": pack["venture"]["name"],
        "stage": pack["venture"]["stage"],
        "onboarding": onboarding or {},
        "paused_reason": paused_reason,
        "task_errors": errors,
        "files": pack["files"],
        "note": "Paste this into support. It contains no API keys.",
    }

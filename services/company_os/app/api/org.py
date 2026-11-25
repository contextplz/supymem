"""Org compose + get."""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from app.core.stages import transition
from app.db.models import Venture
from app.org.lifecycle import materialize_org
from app.org.summary import activate_for_stage, persist_org, resolve_org, summarize_agents
from app.state import MEM, db
from core.clock import utcnow
from core.enums import VentureStage
from core.errors import IllegalTransition

router = APIRouter(tags=["org"])


@router.post("/ventures/{venture_id}/org/compose")
def org_compose(venture_id: str):
    factory = db()
    with factory() as session:
        ven = session.get(Venture, venture_id)
        if not ven:
            raise HTTPException(404, "not_found")
        locked = MEM.get(f"charter:{venture_id}") or {}
        content = locked.get("content") or {}
        if isinstance(locked.get("content_json"), str):
            content = json.loads(locked["content_json"])
        brief_meta = MEM.get(f"brief:{venture_id}") or {}
        charter = {
            **content,
            "current_stage": "org_planning",
            "gtm_order": content.get("gtm_order") or "product_first",
            "build_archetype": content.get("build_archetype")
            or (brief_meta.get("classification") or {}).get("build_archetype"),
            "regulatory_surface": content.get("regulatory_surface")
            or (brief_meta.get("classification") or {}).get("regulatory_surface")
            or "none",
            "monetization": content.get("monetization")
            or (brief_meta.get("classification") or {}).get("monetization")
            or "subscription",
            "integrations": content.get("integrations") or [],
            "armed_channels": int(content.get("armed_channels") or 0),
            "work_order_tracks": int(content.get("work_order_tracks") or 2),
            "one_liner": ven.raw_idea,
        }
        brief = {
            "complexity_score": (brief_meta.get("complexity") or {}).get("score")
            or content.get("complexity_score")
            or 0.4,
            "build_archetype": charter["build_archetype"],
            "regulatory_surface": charter["regulatory_surface"],
            "monetization": charter["monetization"],
        }
        result = materialize_org(
            venture_id=venture_id, charter=charter, brief=brief, stage=ven.stage or "org_planning"
        )
        try:
            ven.stage = transition(ven.stage, VentureStage.ORG_PLANNING).value
        except IllegalTransition:
            ven.stage = VentureStage.ORG_PLANNING.value
        ven.updated_at = utcnow()
        session.commit()
        persist_org(venture_id, result)
        result = activate_for_stage(venture_id, ven.stage)
        summary = summarize_agents(result.get("agents") or [], venture_id=venture_id)
        payload = {**result, "summary": summary, "stage": ven.stage}
        try:
            from app.org.desk import attach_work_briefs

            payload = attach_work_briefs(venture_id, payload)
        except Exception:
            pass
        return payload


@router.get("/ventures/{venture_id}/org")
def org_get(venture_id: str):
    factory = db()
    with factory() as session:
        ven = session.get(Venture, venture_id)
        stage = ven.stage if ven else None
    org = resolve_org(venture_id)
    if org.get("agents"):
        org = activate_for_stage(venture_id, stage)
    summary = summarize_agents(org.get("agents") or [], venture_id=venture_id)
    org = {**org, "summary": summary, "stage": stage}
    try:
        from app.org.desk import attach_work_briefs

        org = attach_work_briefs(venture_id, org)
    except Exception:
        pass
    return org


@router.post("/ventures/{venture_id}/org/activate")
def org_activate(venture_id: str):
    """Promote shadow-mode agents so they can execute work for this venture."""
    factory = db()
    with factory() as session:
        ven = session.get(Venture, venture_id)
        if not ven:
            raise HTTPException(404, "not_found")
        stage = ven.stage
    org = resolve_org(venture_id)
    agents = list(org.get("agents") or [])
    if not agents:
        raise HTTPException(
            409,
            detail={"error": {"code": "no_agents", "message": "Compose a team first."}},
        )
    from app.org.heartbeat import mark_idle

    for agent in agents:
        status = str(agent.get("status") or "").lower()
        if status in {"quarantined", "shadow", "ready", "draft", "paused"}:
            agent["status"] = "active"
            try:
                from app.org.registry import registry

                inst = registry.get_instance(str(agent.get("id")))
                if inst is not None:
                    inst.status = "active"
            except Exception:
                pass
            aid = str(agent.get("id") or "")
            if aid:
                try:
                    mark_idle(aid, venture_id)
                except Exception:
                    pass
    org = {**org, "agents": agents, "agent_count": len(agents)}
    persist_org(venture_id, org)
    # Force-activate even if stage isn't live yet
    summary = summarize_agents(agents, venture_id=venture_id)
    payload = {**org, "summary": summary, "stage": stage}
    try:
        from app.org.desk import attach_work_briefs

        payload = attach_work_briefs(venture_id, payload)
    except Exception:
        pass
    return payload


@router.get("/ventures/{venture_id}/org/agents/{agent_id}")
def org_agent_desk(venture_id: str, agent_id: str):
    """Expanded desk: did / doing / next plus today's attributed log."""
    org = resolve_org(venture_id)
    from app.org.desk import agent_desk

    desk = agent_desk(venture_id, agent_id, org.get("agents") or [])
    if not desk:
        raise HTTPException(404, "not_found")
    return desk

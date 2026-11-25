"""Shared helpers for resolving venture org/agent state for UI surfaces."""

from __future__ import annotations

import json
import ipaddress
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from app.db.models import Venture, Workspace
from app import state
from app.state import db


# Roster employment — not evidence anyone is actually running.
HIRED_ACTIVE = frozenset({"active", "running"})
WATCHING = frozenset({"quarantined", "shadow"})
READY = frozenset({"draft", "ready", "pending"})
PAUSED = frozenset({"paused", "idle"})
# Back-compat alias: callers that meant "configured active" should not
# treat this as a live working count.
WORKING = HIRED_ACTIVE


def published_site_is_public(published: bool, url: str | None) -> bool:
    """A private/loopback preview is not evidence of a public landing page."""
    if not published or not isinstance(url, str):
        return False
    try:
        parsed = urlsplit(url.strip())
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not host or host == "localhost" or host.endswith(".localhost"):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return "." in host
    except ValueError:
        return False


def _org_path(venture_id: str) -> Path:
    from app.config import data_root

    d = data_root() / "ventures" / venture_id
    d.mkdir(parents=True, exist_ok=True)
    return d / "org.json"


def persist_org(venture_id: str, org: dict[str, Any]) -> dict[str, Any]:
    from app.org.characters import enrich_org

    org = enrich_org(org)
    state.MEM[f"org:{venture_id}"] = org
    try:
        _org_path(venture_id).write_text(
            json.dumps(org, indent=2, default=str), encoding="utf-8"
        )
    except OSError:
        pass
    return org


def pack_agents(venture_id: str) -> list[dict[str, Any]]:
    """Day-one named crew from the workspace agent pack, if any."""
    factory = db()
    with factory() as session:
        venture = session.get(Venture, venture_id)
        workspace = (
            session.get(Workspace, venture.workspace_id)
            if venture and venture.workspace_id
            else None
        )
        raw = workspace.settings_json if workspace else None
    if isinstance(raw, str):
        try:
            settings = json.loads(raw)
        except json.JSONDecodeError:
            settings = {}
    elif isinstance(raw, dict):
        settings = raw
    else:
        settings = {}
    configured = (settings.get("agent_pack") or {}).get("agents") or []
    agents: list[dict[str, Any]] = []
    for index, agent in enumerate(configured):
        if not isinstance(agent, dict):
            continue
        agents.append(
            {
                "id": str(agent.get("id") or f"configured-{index}"),
                "name": str(agent.get("name") or agent.get("id") or "Agent"),
                "role": str(agent.get("role") or agent.get("id") or "specialist"),
                "title": str(agent.get("title") or ""),
                "status": str(agent.get("status") or "ready"),
                "charge": agent.get("charge"),
                "source": "configured_pack",
            }
        )
    return agents


def merge_rosters(
    composed: dict[str, Any] | None,
    pack: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Keep pack people; add composed seats whose role is not already filled.

    Pack rows default to ``ready`` from workspace settings. If the materialized
    org already activated that seat, keep the live status so Activate sticks.
    """
    from app.org.characters import canonical_role, enrich_org

    base = dict(composed or {})
    composed_agents = [a for a in (base.get("agents") or []) if isinstance(a, dict)]
    pack_agents_in = [dict(a) for a in (pack or []) if isinstance(a, dict)]
    by_id = {
        str(a.get("id") or ""): a for a in composed_agents if str(a.get("id") or "")
    }
    by_role = {
        canonical_role(str(a.get("role") or "")): a
        for a in composed_agents
        if canonical_role(str(a.get("role") or ""))
    }
    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    for agent in pack_agents_in:
        key = canonical_role(str(agent.get("role") or ""))
        if key in seen:
            continue
        row = dict(agent)
        existing = by_id.get(str(row.get("id") or "")) or by_role.get(key)
        if existing and existing.get("status"):
            row["status"] = existing["status"]
        merged.append(row)
        seen.add(key)
    added_composed = 0
    for agent in composed_agents:
        key = canonical_role(str(agent.get("role") or ""))
        if key in seen:
            continue
        merged.append(dict(agent))
        seen.add(key)
        added_composed += 1
    out = enrich_org({**base, "agents": merged, "agent_count": len(merged)})
    if pack_agents_in and added_composed:
        out["source"] = "composed+pack"
    elif pack_agents_in and not composed_agents:
        out["source"] = "configured_pack"
    elif composed_agents:
        out.setdefault("source", base.get("source") or "composed")
    return out


def persist_pack_org(venture_id: str, agent_pack: dict[str, Any] | None) -> dict[str, Any] | None:
    """Materialize the entry pack so the office has named people before compose."""
    agents = []
    for index, agent in enumerate((agent_pack or {}).get("agents") or []):
        if not isinstance(agent, dict):
            continue
        agents.append(
            {
                "id": str(agent.get("id") or f"configured-{index}"),
                "name": str(agent.get("name") or agent.get("id") or "Agent"),
                "role": str(agent.get("role") or agent.get("id") or "specialist"),
                "title": str(agent.get("title") or ""),
                "status": str(agent.get("status") or "ready"),
                "source": "configured_pack",
            }
        )
    if not agents:
        return None
    persisted = persist_org(
        venture_id,
        {
            "agent_count": len(agents),
            "agents": agents,
            "rationale": [],
            "rejected": [],
            "source": "configured_pack",
        },
    )
    from app.org.heartbeat import mark_idle

    for agent in agents:
        aid = str(agent.get("id") or "")
        if not aid:
            continue
        try:
            # Hired is not working. A fresh heartbeat is the only honest pulse.
            mark_idle(aid, venture_id)
        except Exception:  # noqa: BLE001 — a missing instance must not block entry
            continue
    return persisted


def _load_persisted_org(venture_id: str) -> dict[str, Any] | None:
    path = _org_path(venture_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict) and (data.get("agents") or []):
        state.MEM[f"org:{venture_id}"] = data
        return data
    return None


def resolve_org(venture_id: str) -> dict[str, Any]:
    """Prefer materialized org; fold in pack seats the compose step dropped."""
    from app.org.characters import canonical_role, enrich_org

    materialized = state.MEM.get(f"org:{venture_id}")
    if isinstance(materialized, dict) and (materialized.get("agents") or []):
        org = enrich_org(materialized)
    else:
        persisted = _load_persisted_org(venture_id)
        org = enrich_org(persisted) if persisted else None

    pack = pack_agents(venture_id)
    if org is None:
        return enrich_org(
            {
                "agent_count": len(pack),
                "agents": pack,
                "rationale": [],
                "rejected": [],
                "source": "configured_pack" if pack else "not_configured",
            }
        )

    merged = merge_rosters(org, pack)
    before = {canonical_role(str(a.get("role") or "")) for a in (org.get("agents") or [])}
    after = {canonical_role(str(a.get("role") or "")) for a in (merged.get("agents") or [])}
    if after != before:
        persist_org(venture_id, merged)
        return merged
    return merged


def _fresh_working_ids(
    *,
    venture_id: str | None = None,
    live: list[dict[str, Any]] | None = None,
) -> set[str]:
    """Agent ids whose live row is currently working (stale beats are blocked)."""
    rows = live
    if rows is None and venture_id:
        try:
            from app.org.heartbeat import list_live_status

            rows = list_live_status(venture_id)
        except Exception:  # noqa: BLE001 — a quiet pulse must not break the tile
            rows = []
    out: set[str] = set()
    for row in rows or []:
        if str(row.get("state") or "").lower() != "working":
            continue
        aid = str(row.get("agent_id") or "")
        if aid:
            out.add(aid)
    return out


def summarize_agents(
    agents: list[dict[str, Any]] | None,
    *,
    venture_id: str | None = None,
    live: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Roster counts. ``working`` is fresh heartbeats only — never hired/active."""
    rows = [a for a in (agents or []) if isinstance(a, dict)]
    watching = sum(1 for a in rows if str(a.get("status") or "").lower() in WATCHING)
    ready = sum(1 for a in rows if str(a.get("status") or "").lower() in READY)
    paused = sum(1 for a in rows if str(a.get("status") or "").lower() in PAUSED)
    hired = len(rows)
    working_ids = _fresh_working_ids(venture_id=venture_id, live=live)
    if working_ids:
        working = sum(1 for a in rows if str(a.get("id") or "") in working_ids)
    else:
        working = 0
    roles = [str(a.get("role") or a.get("name") or "agent") for a in rows]
    return {
        "hired": hired,
        "working": working,
        "watching": watching,
        "ready": ready,
        "paused": paused,
        "active": working,
        "quarantined": watching,
        "roles": roles,
        "state": "ready" if hired else "empty",
    }


def activate_for_stage(venture_id: str, stage: str | None) -> dict[str, Any]:
    """When a venture is live/building, promote shadow and day-one ready agents."""
    org = resolve_org(venture_id)
    agents = list(org.get("agents") or [])
    if not agents:
        return org
    live_stages = {"building", "live", "operate", "demand_testing", "hosting", "readiness_review"}
    if (stage or "") not in live_stages:
        return org
    changed = False
    for agent in agents:
        status = str(agent.get("status") or "").lower()
        role = str(agent.get("role") or "").lower()
        should = status in READY or (status in WATCHING and role != "researcher")
        if not should:
            continue
        agent["status"] = "active"
        changed = True
        try:
            from app.org.registry import registry

            inst = registry.get_instance(str(agent.get("id")))
            if inst is not None and inst.status in {"quarantined", "ready", "draft", "shadow"}:
                inst.status = "active"
        except Exception:
            pass
    if changed:
        org = {**org, "agents": agents, "agent_count": len(agents)}
        persist_org(venture_id, org)
    return org


_AWAITING_DETAIL = {
    "awaiting_clarification": "Answer a few questions so the team can plan.",
    "awaiting_validation_review": "Research is ready, read the verdict.",
    "awaiting_validation_decision": "Decide whether to proceed.",
}


def operational_status(
    *,
    stage: str | None,
    onboarding_status: str | None = None,
    paused_reason: str | None = None,
    agent_summary: dict[str, Any] | None = None,
    signals: dict[str, Any] | None = None,
    founder_paused: bool = False,
) -> dict[str, str]:
    """Human-facing venture status for hub cards and headers.

    Prefer observable signals (site/team/onboarding done) over a stale stage
    string so ventures that already shipped a site don't look like "Draft".
    """
    stage = (stage or "draft").lower()
    signals = signals or {}
    summary = agent_summary or {}
    working = int(summary.get("working") or 0)
    hired = int(summary.get("hired") or signals.get("hired") or 0)
    watching = int(summary.get("watching") or 0)
    has_site = bool(signals.get("has_site"))
    website_published = bool(signals.get("website_published"))
    onboarding_done = onboarding_status == "done"

    reason = (paused_reason or "").strip()
    if founder_paused or reason == "venture_paused":
        return {
            "code": "paused",
            "label": "Paused",
            "detail": reason.replace("_", " ") if reason and reason != "venture_paused"
            else "You paused this venture. Nothing runs until you resume it.",
        }
    if onboarding_status == "paused" or stage == "paused":
        # A run waiting on the founder is not the same as a venture the founder
        # stopped: one needs a decision, the other needs nothing.
        if reason in _AWAITING_DETAIL:
            return {
                "code": "needs_you",
                "label": "Needs you",
                "detail": _AWAITING_DETAIL[reason],
            }
        return {
            "code": "paused",
            "label": "Paused",
            "detail": (reason or "Waiting on you or a provider.").replace("_", " "),
        }
    if stage in {"archived", "purged"}:
        return {"code": "archived", "label": "Archived", "detail": "In the recovery window."}

    # A durable public deployment is stronger evidence than a stale venture
    # stage. Founder publication deliberately does not rewrite the onboarding
    # stage, so status consumers must prefer this observable signal.
    if website_published:
        return {
            "code": "live",
            "label": "Landing page live",
            "detail": "Your early-access page is live. Next: get your first sign-ups.",
        }

    live_stages = {
        "building",
        "live",
        "operate",
        "demand_testing",
        "hosting",
        "readiness_review",
        "fork_choice",
    }
    setup_stages = {
        "draft",
        "intake",
        "classifying",
        "clarifying",
        "charter_review",
        "org_planning",
    }
    # A pack + a first-look file is not a live company. Only the founder-finished
    # run, or an explicit live stage, may claim that.
    effectively_live = stage in live_stages or onboarding_done

    if stage in setup_stages and not effectively_live:
        return {
            "code": "setup",
            "label": "In setup",
            "detail": "Not live yet, still configuring this venture.",
        }

    # Independent QA acceptance makes a product ready for the founder's
    # launch decision; it does not publish the product or make the company
    # publicly live. Keep this gate truthful on every surface that consumes
    # the shared operational status.
    if stage == "readiness_review":
        return {
            "code": "readiness_review",
            "label": "Ready to launch",
            "detail": "The accepted product remains private until you choose to publish it.",
        }

    if hired and working == 0 and watching:
        return {
            "code": "watching",
            "label": "Live · watching",
            "detail": f"{watching} agents in shadow mode, not executing yet. Open Agents → Activate team.",
        }
    if effectively_live or stage in {"live", "operate", "building", "hosting"}:
        detail_bits: list[str] = []
        if hired:
            team = f"{hired} {'teammate' if hired == 1 else 'teammates'}"
            if working:
                detail_bits.append(f"{working} of {team} working")
            else:
                detail_bits.append(team)
        elif has_site:
            detail_bits.append("Site is up")
        return {
            "code": "live" if stage != "building" else "building",
            "label": "Building" if stage == "building" and not onboarding_done else "Live",
            "detail": " · ".join(detail_bits) if detail_bits else "Site and ops are online.",
        }
    if stage == "building":
        return {"code": "building", "label": "Building", "detail": "Product work is in progress."}
    return {
        "code": stage,
        "label": stage.replace("_", " ").title(),
        "detail": "",
    }

"""Dashboard aggregate (29 §3)."""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter

from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import load_only

from app.db.models import (
    PlannedTask,
    SiteVersion,
    Venture,
    VentureDocument,
    VentureResource,
)
from app.db.supymem_models import Deployment, VentureWaitlistEntry
from app.onboarding.state import AUTONOMY_WORDS, active_run, latest_run
from app.planning.store import current_plan_rows, list_open_tasks
from app.state import MEM, db

router = APIRouter(tags=["dashboard"])


def _public_site_url(workspace_id: str | None, version: int | None = None) -> str | None:
    if not workspace_id:
        return None
    from app.core.local_mode import is_local
    if is_local():
        core = os.environ.get("CORE_PORT") or "3300"
        query = f"?v={version}" if version else ""
        return f"http://127.0.0.1:{core}/w/{workspace_id}/site/{query}"
    core = os.environ.get("CORE_PORT") or "3300"
    if os.environ.get("PYTEST_CURRENT_TEST"):
        base = f"http://127.0.0.1:{core}"
    else:
        base = (
            os.environ.get("PUBLIC_SITE_BASE")
            or os.environ.get("PUBLIC_GATEWAY_URL")
            or "https://gateway.supymem.com"
        ).rstrip("/")
    q = f"?v={version}" if version else ""
    return f"{base}/w/{workspace_id}/site/{q}"


def _prefer_openable_url(url: str | None, workspace_id: str | None, version: int | None) -> str | None:
    """Rewrite file://, loopback, and dead HostLocal ports to the served first-look route."""
    public = _public_site_url(workspace_id, version)
    if not url:
        return public
    host = ""
    try:
        from urllib.parse import urlparse

        host = (urlparse(url).hostname or "").lower()
    except Exception:
        host = ""
    if url.startswith("file:") or ":417" in url or host in {"localhost", "127.0.0.1", "0.0.0.0"}:
        return public or url
    return url

EMPTY = {
    "next_up": "No tasks yet, they arrive after your plan is locked.",
    "website": "No site yet, a first look appears right after understand.",
    "documents": "Your mission, roadmap and market research land here.",
    "email": "gets its own address during setup.",
    "social": "Drafts appear here. Nothing posts without your say-so.",
    "business": "No visitors yet, that's expected before launch.",
    "team": "Your agents are hired after the plan is locked.",
    "validation": "Your answers and the demand read show up here.",
    "cost": "$0.00 so far today.",
    "governance": "Nothing has been done without asking.",
    "evolution": "No evolution cycles yet.",
    "tasks": "No tasks yet, they arrive after your plan is locked.",
}


def _external_site(venture_id: str):
    try:
        from app.chat.recovery import external_site

        return external_site(venture_id)
    except Exception:  # noqa: BLE001
        return None


def _tile(state: str, **kwargs) -> dict:
    return {"state": state, **kwargs}


def _handoff_next_step(run: dict, fallback: dict) -> dict:
    """Use the already-scoped current handoff action over an unrelated workflow."""
    if fallback.get("key") == "gtm.results":
        return fallback
    action = run.get("action") or {}
    if (run.get("state") == "completed" and action.get("label") == "Review reachable leads"
            and str(fallback.get("key") or "").startswith("gtm.")
            and str(action.get("href") or "").startswith("/gtm?venture=")):
        return {"key": "gtm.review", "stage": "Customers", "title": action["label"],
                "detail": run.get("activity"), "cta_label": action["label"], "href": action["href"],
                "chat_action": None, "payload": None}
    if run.get("stage") != "Your task" or run.get("state") != "needs_you" or not action.get("label") or not action.get("href"):
        return fallback
    return {"key": "handoff.needs_you", "stage": "Your task", "title": run.get("activity") or "Your task needs attention",
            "detail": run.get("next_step") or "Review the required action.", "cta_label": action["label"],
            "href": action["href"], "chat_action": None, "payload": None}


def _founder_credits(venture_id: str, *, workspace_id: str | None = None) -> dict | None:
    try:
        from app.credits.accounts import founder_snapshot

        return founder_snapshot(venture_id, workspace_id=workspace_id)
    except Exception:
        return None


@router.get("/ventures/{venture_id}/dashboard")
def get_dashboard(venture_id: str, request: Request, response: Response):
    factory = db()
    with factory() as session:
        ven = session.get(Venture, venture_id)
        if not ven:
            raise HTTPException(404, "not_found")
        resources = {
            r.resource: r
            for r in session.scalars(
                select(VentureResource).where(VentureResource.venture_id == venture_id)
            ).all()
        }
        from app.ventures.startup_review import PRIVATE_KINDS
        docs = session.scalars(
            select(VentureDocument).where(VentureDocument.venture_id == venture_id, VentureDocument.kind.not_in(PRIVATE_KINDS))
            .options(load_only(VentureDocument.kind, VentureDocument.title, VentureDocument.updated_at))
        ).all()
        task_rows = session.scalars(
            select(PlannedTask).where(
                PlannedTask.venture_id == venture_id,
                PlannedTask.status.in_(("open", "running")),
            )
        ).yield_per(100)
        counts = Counter(task.status for task in current_plan_rows(session, task_rows))
        open_n, running_n = counts["open"], counts["running"]

    run = active_run(venture_id) or latest_run(venture_id)
    charter = MEM.get(f"charter:{venture_id}") or {}
    content = charter.get("content") or {}
    website_mem = MEM.get(f"website:{venture_id}") or {}
    demand = MEM.get(f"demand:{venture_id}") or {}
    answers = MEM.get(f"answers:{venture_id}") or {}
    questions = MEM.get(f"questions:{venture_id}") or []
    flags = {}
    if run:
        try:
            flags = json.loads(run.flags_json or "{}")
        except json.JSONDecodeError:
            flags = {}

    level = ven.autonomy_level or "L1"
    label = AUTONOMY_WORDS.get(level, "Suggest")

    # next_up
    tasks_out = []
    for t in list_open_tasks(venture_id, limit=3):
        if not t.get("why_now"):
            continue
        tasks_out.append(t)
    headline = (run.next_up_headline if run else None) or (
        "Here's what I'd tackle next, in this order." if tasks_out else None
    )
    if tasks_out:
        next_up = _tile("ready", headline=headline, tasks=tasks_out)
    else:
        next_up = _tile("empty", note=EMPTY["next_up"], headline=None, tasks=[])

    host = resources.get("hosting")

    site_row = None
    has_live_deployment = False
    interest_count = 0
    if ven.workspace_id:
        with factory() as session:
            site_row = session.scalars(
                select(SiteVersion)
                .where(SiteVersion.workspace_id == ven.workspace_id)
                .order_by(SiteVersion.version.desc())
                .limit(1)
            ).first()
            interest_count = int(session.scalar(select(func.count()).select_from(VentureWaitlistEntry).where(
                VentureWaitlistEntry.workspace_id == ven.workspace_id,
                VentureWaitlistEntry.venture_id == ven.id,
                VentureWaitlistEntry.is_test.is_(False))) or 0)
            if site_row is not None:
                session.expunge(site_row)
            has_live_deployment = bool(
                session.scalar(
                    select(Deployment.id)
                    .where(
                        Deployment.venture_id == venture_id,
                        Deployment.state == "live",
                    )
                    .limit(1)
                )
            )

    raw_url = site_row.url if site_row else None
    version = site_row.version if site_row else None
    web_url = _prefer_openable_url(
        raw_url,
        ven.workspace_id,
        int(version) if isinstance(version, int) else None,
    )
    from app.core.local_mode import is_local
    local_publication = None
    if is_local():
        from app.pagebuilder.local_publish import _publication
        local_publication = _publication(venture_id, ven.workspace_id)
        has_live_deployment = bool(local_publication and local_publication.get("state") == "live")
        if has_live_deployment:
            web_url = local_publication["url"]
    site_simulated = True if host is None else bool(host.simulated)
    public_waitlist = bool(
        web_url
        and "127.0.0.1" not in web_url
        and not web_url.startswith("file:")
        and not os.environ.get("PYTEST_CURRENT_TEST")
    )
    if (site_row is not None or local_publication and local_publication.get("state") == "live") and web_url:
        website = _tile(
            "ready",
            url=web_url,
            simulated=False if public_waitlist else site_simulated,
            interest_mode="live" if public_waitlist else ("simulated" if site_simulated else "live"),
            published=bool(
                website_mem.get("published")
                or (site_row.published if site_row else False)
                or has_live_deployment
            ),
            provider=(host.provider if host else "local"),
            stage=(site_row.stage if site_row else website_mem.get("stage")),
            version=version,
            interest_count=interest_count,
            # Prefer live iframeable URL over a missing screenshot path.
            screenshot_url=web_url or (site_row.screenshot_path if site_row else None),
        )
    else:
        website = _tile("empty", note=EMPTY["website"])

    if docs:
        documents = _tile(
            "ready",
            items=[{"id": d.id, "kind": d.kind, "title": d.title, "updated_at": d.updated_at.isoformat() + "Z"} for d in docs],
        )
    else:
        documents = _tile("empty", note=EMPTY["documents"], items=[])

    email_r = resources.get("email")
    if email_r:
        email = _tile(
            "ready",
            address=email_r.display,
            simulated=bool(email_r.simulated),
            outbox=1,
            sent=0,
        )
    else:
        email = _tile("empty", note=f"{ven.name} {EMPTY['email']}")

    from app.org.summary import (
        activate_for_stage,
        operational_status,
        published_site_is_public,
        summarize_agents,
    )

    org = activate_for_stage(venture_id, ven.stage)
    agents = org.get("agents") or []
    agent_summary = summarize_agents(agents, venture_id=venture_id)
    active = int(agent_summary.get("working") or 0)
    if agents:
        hired_n = int(agent_summary["hired"] or 0)
        working_n = int(agent_summary["working"] or 0)
        watching_n = int(agent_summary["watching"] or 0)
        note = f"{hired_n} hired"
        if working_n:
            note += f" · {working_n} working"
        if watching_n:
            note += f" · {watching_n} watching (shadow)"
        team = _tile(
            "ready",
            hired=hired_n,
            working=working_n,
            watching=watching_n,
            ready=agent_summary["ready"],
            paused=agent_summary["paused"],
            active=hired_n,
            quarantined=agent_summary["quarantined"],
            roles=agent_summary["roles"],
            note=note,
        )
    else:
        team = _tile("empty", note=EMPTY["team"], active=0, hired=0, working=0, watching=0)

    unvalidated = bool(flags.get("unvalidated"))
    q_answered = len(answers) if answers else 0
    # count unique question answers
    if questions:
        q_answered = sum(1 for q in questions if q["id"] in answers or q.get("slot_key") in answers)
    brief = MEM.get(f"brief:{ven.id}") or {}
    research = brief.get("research") if isinstance(brief.get("research"), dict) else {}
    persisted_validation = None
    if not demand:
        # This is the same saved scan read used by the validation page. A
        # cold working store does not mean that research never ran. Do not
        # hydrate MEM, regenerate research, or consult a superseded run.
        from app.api.validation import _validation_from_persisted_research

        saved_run = latest_run(venture_id)
        if saved_run and saved_run.workspace_id in (None, ven.workspace_id):
            persisted_validation = _validation_from_persisted_research(venture_id)
            current_run = latest_run(venture_id)
            if not current_run or current_run.id != saved_run.id or current_run.workspace_id != saved_run.workspace_id:
                persisted_validation = None
        if persisted_validation:
            demand = {
                "demand": "unknown",
                "confidence": persisted_validation.get("confidence"),
                "recommendation": persisted_validation.get("recommendation"),
                "competitors": persisted_validation.get("competitors") or [],
            }
    competitors = demand.get("competitors") or research.get("competitors") or []
    recommendation = demand.get("recommendation")
    if questions or demand or competitors:
        val_state = "degraded" if unvalidated or (persisted_validation or {}).get("verdict") == "Unavailable" else "ready"
        validation = _tile(
            val_state,
            questions_answered=q_answered,
            rounds=1 if questions else 0,
            demand=demand.get("demand", "unknown"),
            confidence=demand.get("confidence", 0.0),
            recommendation=recommendation,
            competitors=competitors[:5],
            queries=demand.get("queries") or research.get("queries") or [],
            unvalidated=unvalidated,
        )
        if persisted_validation:
            validation.update(
                source="persisted_research",
                verdict=persisted_validation.get("verdict"),
                source_count=persisted_validation.get("source_count", 0),
                mode=persisted_validation.get("mode"),
            )
    else:
        validation = _tile("empty", note=EMPTY["validation"], unvalidated=False)

    cost_usd = round((run.cost_usd if run else 0.0) or 0.0, 4)
    usage = {}
    if ven.workspace_id:
        try:
            from app.llm.pricing import workspace_usage_summary

            usage = workspace_usage_summary(ven.workspace_id)
        except Exception:
            usage = {}
    meter_usd = float(usage.get("estimate_usd_total") or 0)
    cost_state = "ready" if (usage.get("calls") or meter_usd or cost_usd) else "empty"
    from app.ventures.lifecycle import pause_fields

    pause = pause_fields(ven.id, workspace_id=ven.workspace_id)
    if pause["paused"]:
        onboarding_status = "paused"
        status_reason = pause["paused_reason"]
    else:
        onboarding_status = run.status if run else None
        status_reason = getattr(run, "paused_reason", None) if run else None
    op_status = operational_status(
        stage=ven.stage,
        onboarding_status=onboarding_status,
        paused_reason=status_reason,
        agent_summary=agent_summary,
        signals={
            "has_site": website.get("state") not in (None, "empty"),
            "has_team": bool(agents),
            "website_published": bool(has_live_deployment or published_site_is_public(
                bool(website.get("published")), raw_url)),
        },
        founder_paused=pause["paused"],
    )
    research_available = False
    try:
        from app.config import data_root

        research_available = (
            data_root() / "ventures" / venture_id / "research" / "validation_dossier.md"
        ).is_file()
    except Exception:
        research_available = False
    from app.concierge.human import list_open_human_tasks
    from app.ventures.founder_control import control_block

    pending_approvals = 0
    if ven.workspace_id:
        try:
            from app.approvals.service import list_approvals

            pending_approvals = len(list_approvals(ven.workspace_id, status="pending"))
        except Exception:
            pending_approvals = 0

    needs_you = [
        {
            "id": item["id"],
            "title": item["title"],
            "why": item.get("why"),
            "provider": item.get("provider"),
            "href": item.get("href") or (
                f"/v/{venture_id}/connect/{item['provider']}" if item.get("provider") else None
            ),
            "action_label": (item.get("guide") or {}).get("open_label") or "Do it now",
            "steps": item.get("steps") or [],
        }
        for item in list_open_human_tasks(venture_id)
    ]
    from app.ventures.next_step import founder_next_step as venture_next_step
    from app.ventures.run_status import snapshot as run_status_snapshot

    run_status = run_status_snapshot(venture_id, ven.workspace_id, paused=bool(pause["paused"]))
    payload = {
        "next_step": _handoff_next_step(run_status, venture_next_step(venture_id)),
        "run_status": run_status,
        "venture": {
            "id": ven.id,
            "name": ven.name,
            "stage": ven.stage,
            "status": op_status,
            "one_liner": content.get("one_liner"),
            "created_at": ven.created_at.isoformat() + "Z" if ven.created_at else None,
        },
        "onboarding": {
            "status": run.status if run else None,
            "paused_reason": getattr(run, "paused_reason", None) if run else None,
            "phases_done": run.phases_done if run else 0,
            "phases_total": run.phases_total if run else 7,
            "run_id": run.id if run else None,
        },
        "research": {
            "available": research_available,
            "href": f"/v/{venture_id}/research" if research_available else None,
        },
        "governance": {
            "autonomy": {
                "level": level,
                "label": label,
                "description": "I propose; you approve everything." if level == "L1" else "",
            },
            "kill_switch": False,
            "pending_approvals": pending_approvals,
            "caps": {
                "spend_daily_usd": 5.0,
                "spend_used_usd": round(meter_usd or cost_usd, 4),
                "agents_max": 8,
                "agents_active": active,
            },
        },
        "tiles": {
            "next_up": next_up,
            "tasks": _tile(
                "ready" if open_n or running_n else "empty",
                open=open_n,
                running=running_n,
                done_7d=0,
                note=None if open_n or running_n else EMPTY["tasks"],
            ),
            "website": website,
            "external_site": _external_site(venture_id),
            "documents": documents,
            "email": email,
            "social": _tile("empty", drafts=0, posted=0, channels=[], note=EMPTY["social"]),
            "business": _tile(
                "empty",
                visitors_7d=0,
                signups_7d=0,
                revenue_usd=0,
                note=EMPTY["business"],
            ),
            "team": team,
            "validation": validation,
            "cost": _tile(
                cost_state,
                today_usd=float((usage.get("this_month") or {}).get("estimate_usd") or cost_usd),
                onboarding_usd=cost_usd,
                cap_usd=5.0,
                top_purpose="onboarding" if cost_usd and not usage.get("calls") else None,
                prompt_tokens_total=int(usage.get("prompt_tokens_total") or 0),
                completion_tokens_total=int(usage.get("completion_tokens_total") or 0),
                total_tokens=int(usage.get("total_tokens") or 0),
                estimate_usd_total=float(usage.get("estimate_usd_total") or 0),
                estimate_usd_month=float(usage.get("estimate_usd_month") or 0),
                prompt_tokens_month=int(usage.get("prompt_tokens_month") or 0),
                completion_tokens_month=int(usage.get("completion_tokens_month") or 0),
                cached_tokens_total=int(usage.get("cached_tokens_total") or 0),
                reasoning_tokens_total=int(usage.get("reasoning_tokens_total") or 0),
                calls=int(usage.get("calls") or 0),
                last_run_usd=usage.get("last_run_usd"),
                last_run_at=usage.get("last_run_at"),
                forecast=usage.get("forecast"),
                credits=_founder_credits(venture_id, workspace_id=ven.workspace_id),
            ),
            "evolution": _tile("empty", cycles=0, proposals_open=0, last_cycle_at=None, note=EMPTY["evolution"]),
        },
        "needs_you": needs_you,
        "control": control_block(
            venture_id,
            website=website if website.get("state") != "empty" else None,
            research_href=f"/v/{venture_id}/research" if research_available else None,
            paused=bool(pause["paused"]),
            credits=_founder_credits(venture_id, workspace_id=ven.workspace_id),
        ),
    }
    try:
        from app.munder import hive_snapshot

        payload["hive"] = hive_snapshot(venture_id)
    except Exception:  # noqa: BLE001
        payload["hive"] = {
            "breaker": "healthy",
            "inbox_backlog": 0,
            "blocked_cards": 0,
            "open_human_qa": 0,
            "cards": 0,
        }
    etag = 'W/"' + hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16] + '"'
    payload["etag"] = etag
    if_none = request.headers.get("if-none-match")
    if if_none and if_none == etag:
        return Response(status_code=304)
    response.headers["ETag"] = etag
    return payload


@router.get("/ventures/{venture_id}/dashboard/tile/{tile}")
def get_tile(venture_id: str, tile: str, request: Request, response: Response):
    dash = get_dashboard(venture_id, request, response)
    if isinstance(dash, Response):
        return dash
    tiles = dash.get("tiles") or {}
    if tile not in tiles:
        raise HTTPException(404, "unknown_tile")
    return tiles[tile]


@router.get("/ventures/{venture_id}/export")
def export_venture(venture_id: str):
    from app.ventures.founder_control import export_pack

    pack = export_pack(venture_id)
    if pack is None:
        raise HTTPException(404, "not_found")
    return pack


@router.post("/ventures/{venture_id}/export")
def prepare_venture_export(venture_id: str, request: Request):
    """The clicked download action, distinct from the read-only projection."""
    from app.admin.authn import verify_caller
    from app.authz.workspace import assert_venture_access
    from app.email.venture_receipts import on_export_prepared

    caller = verify_caller(request, required=True)
    assert_venture_access(venture_id, caller.sub)
    pack = export_venture(venture_id)
    on_export_prepared(venture_id, caller.sub, pack)
    return pack


@router.get("/ventures/{venture_id}/support-bundle")
def get_support_bundle(venture_id: str):
    from app.ventures.founder_control import support_bundle
    from app.ventures.lifecycle import pause_fields

    factory = db()
    with factory() as session:
        ven = session.get(Venture, venture_id)
        if not ven:
            raise HTTPException(404, "not_found")
        workspace_id = ven.workspace_id
    run = active_run(venture_id) or latest_run(venture_id)
    pause = pause_fields(venture_id, workspace_id=workspace_id)
    pack = support_bundle(
        venture_id,
        onboarding={
            "status": run.status if run else None,
            "phase": getattr(run, "phase", None) if run else None,
            "paused_reason": getattr(run, "paused_reason", None) if run else None,
        },
        paused_reason=pause.get("paused_reason"),
    )
    if pack is None:
        raise HTTPException(404, "not_found")
    return pack

"""Polsia-compatible ops REST surface under /api/v1 (doc 49 PS-6).

Thin handlers: parse args, call ops services, shape responses. No ORM here.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field, field_validator

from app.api.ops_auth import (
    OpsAuthRoute,
    require_ops_auth,
    require_workspace,
    resolve_ops_person,
)
from app.audit.writer import audit
from app.ops import VALID_AGENT_TYPES
from app.ops.services import activity as activity_svc
from app.ops.services import company as company_svc
from app.ops.services import deployments as deploy_svc
from app.ops.services import finance as finance_svc
from app.ops.services import memory as memory_svc
from app.ops.services import reports as reports_svc
from app.ops.services import social as social_svc
from app.ops.services import tasks as tasks_svc
from app.ops import config as ops_config
from app.ops.services import operations as operations_svc
from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk
from app.tenancy import current_or_none

router = APIRouter(prefix="/api/v1", tags=["ops"], route_class=OpsAuthRoute)
hosted_router = APIRouter(prefix="/api/v1", tags=["ops"], route_class=OpsAuthRoute)
public_router = APIRouter(tags=["public"])
ROUTERS = [public_router]


def _require_api_key(x_api_key: str | None) -> None:
    # In hosted mode the verified gateway assertion and middleware-selected
    # workspace are the authority.  A shared ops key is only legacy/local
    # compatibility and must never become a substitute for membership.
    scope = current_or_none()
    if scope is not None and scope.workspace_id != "none" and scope.actor != "anonymous":
        return
    expected = ops_config.ops_api_key()
    if expected is None:
        return
    if not x_api_key or x_api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


def _workspace(workspace_id: str | None, x_workspace_id: str | None) -> str:
    scope = current_or_none()
    requested = {value for value in (workspace_id, x_workspace_id) if value}
    if len(requested) > 1:
        raise HTTPException(status_code=400, detail={"code": "workspace_mismatch"})
    if scope is not None and scope.workspace_id != "none":
        # The auth middleware already checked membership of the selector. Do
        # not let a route re-resolve arbitrary caller input after that check.
        if requested and scope.workspace_id not in requested:
            raise HTTPException(status_code=404, detail={"code": "not_found"})
        return scope.workspace_id
    wid = next(iter(requested), None)
    if wid:
        return wid
    desk = ensure_desk(LOCAL_PERSON_ID)
    return desk.id


def _auth_workspace(x_api_key: str | None, workspace_id: str | None, x_workspace_id: str | None) -> str:
    person = resolve_ops_person()
    ws = require_workspace(person, workspace_id, x_workspace_id)
    require_ops_auth(x_api_key, ws.tenancy)
    return ws.id


def _require_operator() -> None:
    """Operator-only routes: the passphrase operator (local or staging), never a founder's account session."""
    person = resolve_ops_person()
    if person != LOCAL_PERSON_ID and not person.startswith("usr_staging_"):
        raise HTTPException(status_code=403, detail={"code": "operator_only"})


class ConfigBody(BaseModel):
    name: str | None = None
    mission: str | None = None
    vision: str | None = None
    description: str | None = None
    target_market: str | None = None
    value_prop: str | None = None
    website_url: str | None = None
    github_repo: str | None = None
    product_type: str | None = None
    industry: str | None = None
    timezone: str | None = None
    daily_cycle_hour: int | None = None
    pricing_model: Any = None
    goals: Any = None
    kpis: Any = None


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=512)
    agent_type: str
    description: str | None = None
    priority: int = 3


class MemoryCreate(BaseModel):
    category: str = "general"
    title: str
    content: str
    source: str | None = None
    tags: list[str] | None = None


class TriggerBody(BaseModel):
    task_title: str | None = None
    description: str | None = None


class AdProposalBody(BaseModel):
    platform: str
    idea: str
    goal: str = "leads"
    landing_url: str
    audience: str
    geography: str = "US"
    daily_budget_usd: float
    name: str | None = None
    headline: str | None = None
    description: str | None = None


class AdDecisionBody(BaseModel):
    decision: str


class DeployTrigger(BaseModel):
    service_name: str = "web"
    health_check_url: str | None = None


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/config")
def get_config(
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    cfg = company_svc.get_config(wid)
    if cfg is None:
        raise HTTPException(status_code=404, detail="No company config yet")
    return cfg


@router.put("/config")
def put_config(
    body: ConfigBody,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    fields = body.model_dump(exclude_unset=True)
    updated = company_svc.upsert_config(wid, fields)
    audit(
        "ops.config.updated",
        workspace_id=wid,
        actor_id=LOCAL_PERSON_ID,
        actor_kind="person",
        surface="api",
        target_ref=updated["id"],
        outcome="ok",
        payload={"fields": sorted(fields.keys())},
    )
    return updated


@router.get("/dashboard/summary")
def dashboard_summary(
    workspace_id: str | None = None,
    venture_id: str | None = Query(None, min_length=1, max_length=128),
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    _require_api_key(x_api_key)
    wid = _workspace(workspace_id, x_workspace_id)
    try:
        result = operations_svc.summary(wid, venture_id=venture_id)
    except KeyError:
        raise HTTPException(404, detail={"code": "not_found"}) from None
    if venture_id is None:
        cfg = company_svc.get_config(wid) or {}
        report = reports_svc.get_daily_report(wid)
        result.update(kpis=cfg.get("kpis") or {},
                      kpis_basis="workspace_configured_values_not_verified_product_analytics",
                      last_report_date=report["report_date"] if report else None)
    return result


@router.get("/dashboard/activity")
def dashboard_activity(
    workspace_id: str | None = None,
    limit: int = 50,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    return activity_svc.get_recent_activity(wid, limit=min(limit, 100))


@router.get("/dashboard/reports/daily")
def daily_report(
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    return reports_svc.get_daily_report(wid)


@router.get("/agents/status")
def agents_status(
    workspace_id: str | None = None,
    venture_id: str | None = Query(None, min_length=1, max_length=128),
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    _require_api_key(x_api_key)
    wid = _workspace(workspace_id, x_workspace_id)
    try:
        return operations_svc.agent_status(wid, venture_id=venture_id)
    except KeyError:
        raise HTTPException(404, detail={"code": "not_found"}) from None


@router.post("/agents/{agent_type}/trigger", status_code=202)
def trigger_agent(
    agent_type: str,
    body: TriggerBody | None = None,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    if agent_type not in VALID_AGENT_TYPES:
        raise HTTPException(status_code=422, detail=f"Unknown agent_type: {agent_type}")
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    payload = body or TriggerBody()
    title = payload.task_title or f"Manual trigger: {agent_type}"
    task = tasks_svc.create_task(
        wid,
        title=title,
        agent_type=agent_type,
        description=payload.description,
        source="api",
        priority=2,
    )
    audit(
        "ops.agent.triggered",
        workspace_id=wid,
        actor_id=f"agent:{agent_type}",
        actor_kind="agent",
        on_behalf_of=LOCAL_PERSON_ID,
        surface="api",
        target_ref=task["id"],
        outcome="ok",
        payload={"agent_type": agent_type, "title": title},
    )
    # Sync run for local/tests so the activity feed updates immediately.
    # HTTP contract matches Polsia: 202 + status=queued + task_id.
    tasks_svc.execute_task(wid, task["id"])
    return {"status": "queued", "task_id": task["id"]}


@router.get("/tasks")
def list_tasks(
    workspace_id: str | None = None,
    venture_id: str | None = Query(None, min_length=1, max_length=128),
    limit: int = Query(50, ge=1, le=200),
    status: str | None = Query(None, max_length=32),
    agent_type: str | None = Query(None, max_length=128),
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    _require_api_key(x_api_key)
    wid = _workspace(workspace_id, x_workspace_id)
    try:
        return operations_svc.list_tasks(wid, venture_id=venture_id, status=status, agent_type=agent_type, limit=limit)
    except KeyError:
        raise HTTPException(404, detail={"code": "not_found"}) from None
    except ValueError:
        raise HTTPException(422, detail={"code": "invalid_task_filter"}) from None


@router.post("/tasks", status_code=201)
def create_task(
    body: TaskCreate,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    if body.agent_type not in VALID_AGENT_TYPES:
        raise HTTPException(status_code=422, detail=f"Unknown agent_type: {body.agent_type}")
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    return tasks_svc.create_task(
        wid,
        title=body.title,
        agent_type=body.agent_type,
        description=body.description,
        priority=body.priority,
        source="api",
    )


@router.get("/tasks/{task_id}")
def get_task(
    task_id: str,
    workspace_id: str | None = None,
    venture_id: str | None = Query(None, min_length=1, max_length=128),
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    _require_api_key(x_api_key)
    wid = _workspace(workspace_id, x_workspace_id)
    try:
        found = operations_svc.get_task(wid, task_id, venture_id=venture_id)
    except KeyError:
        raise HTTPException(404, detail={"code": "not_found"}) from None
    if found is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return found


@router.get("/memory")
def list_or_search_memory(
    q: str | None = None,
    category: str | None = None,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    if q:
        return memory_svc.search_memory(wid, q)
    return memory_svc.list_memories(wid, category=category)


@router.post("/memory", status_code=201)
def create_memory(
    body: MemoryCreate,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    return memory_svc.store_memory(
        wid,
        category=body.category,
        title=body.title,
        content=body.content,
        source=body.source,
        tags=body.tags,
    )


@router.get("/social/posts")
def social_posts(
    status: str | None = None,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    return social_svc.list_posts(wid, status=status)


@router.get("/finance/summary")
def finance_summary(
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    return finance_svc.get_summary(wid)


@router.get("/finance/revenue")
def finance_revenue(
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    return finance_svc.list_revenue(wid)


@router.get("/finance/expenses")
def finance_expenses(
    category: str | None = None,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    return finance_svc.list_expenses(wid, category=category)


@hosted_router.post("/finance/stripe/webhook")
async def stripe_webhook(
    request: Request,
    stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"),
):
    # Workspace comes from Stripe metadata / account map — never the caller (OT-07).
    payload = await request.body()
    try:
        result = finance_svc.process_stripe_webhook(payload, stripe_signature)
    except finance_svc.WebhookError as exc:
        return JSONResponse(status_code=exc.status, content={"detail": str(exc)})
    audit(
        "ops.stripe.webhook",
        workspace_id=result.get("workspace_id"),
        actor_id="stripe",
        actor_kind="system",
        surface="webhook",
        target_ref=result.get("id"),
        outcome="ok",
        payload={"received": True},
    )
    return result


@router.get("/deployments")
def list_deployments(
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    return deploy_svc.list_deployments(wid)


@router.post("/deployments", status_code=201)
def create_deployment(
    body: DeployTrigger,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    return deploy_svc.record_deployment(
        wid,
        service_name=body.service_name,
        status="pending",
        trigger_source="api",
        health_check_url=body.health_check_url,
        summary=f"Recorded deploy for {body.service_name}",
    )


@router.get("/deployments/latest")
def latest_deployments(
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    return deploy_svc.latest_deployments(wid)


@router.post("/deployments/trigger", status_code=202)
def trigger_deploy(
    body: DeployTrigger,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    result = deploy_svc.trigger_deployment(
        wid, service_name=body.service_name, health_check_url=body.health_check_url
    )
    audit(
        "ops.deployment.triggered",
        workspace_id=wid,
        actor_id=LOCAL_PERSON_ID,
        actor_kind="person",
        surface="api",
        target_ref=result["id"],
        outcome="ok",
        payload={"service_name": body.service_name, "status": result["status"]},
    )
    return result


# ---------------------------------------------------------------------------
# GTM outreach (docs 115–117) — Loop 1 customer-acquisition surface
# ---------------------------------------------------------------------------


class DiscoverBody(BaseModel):
    product_hint: str | None = None
    queries: list[str] | None = None
    limit: int = 12
    venture_id: str | None = None


@router.post("/outreach/discover")
async def outreach_discover(
    body: DiscoverBody | None = None,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """Retired web harvest. Replaced by POST /outreach/ingest (paste) — OT-20."""
    _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    return JSONResponse(
        status_code=410,
        content={"blocked": "web_icp_discover_retired", "use": "/outreach/ingest"},
    )


class ReplyBody(BaseModel):
    contact_id: str
    body: str
    classification: str = "human_positive"
    message_id: str | None = None
    in_reply_to: str | None = None


class ArmBody(BaseModel):
    """Scope and selected contacts for a draft-only outreach sequence."""

    contact_ids: list[str] = Field(default_factory=list)
    venture_id: str | None = None
    product_name: str | None = None


@router.get("/outreach/prospects")
def outreach_prospects(
    limit: int = 100,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.relationships.service import list_queue

    return list_queue(wid, limit=min(limit, 500)).get("items") or []


@router.get("/outreach/today")
def outreach_today(
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.relationships.service import list_queue, outreach_status

    queue = list_queue(wid)
    status = outreach_status(wid)
    return {"queue": queue, "status": status}


@router.post("/outreach/first-five")
async def outreach_first_five(
    body: ArmBody | None = None,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """Retired auto-sweep drafts. Replaced by First Five Minutes onboarding — OT-26."""
    _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    return JSONResponse(
        status_code=410,
        content={"blocked": "first_five_retired", "use": "/outreach/ingest"},
    )


@router.post("/outreach/arm")
def outreach_arm(
    body: ArmBody,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.relationships import outreach as outreach_svc

    if not body.contact_ids:
        raise HTTPException(status_code=400, detail="contact_ids required")
    result = outreach_svc.arm_sequence(
        wid,
        contact_ids=body.contact_ids,
        armed_by=LOCAL_PERSON_ID,
        product_name=body.product_name,
    )
    audit(
        "ops.outreach.armed",
        workspace_id=wid,
        actor_id=LOCAL_PERSON_ID,
        actor_kind="person",
        surface="api",
        target_ref=result["sequence_id"],
        outcome="ok",
        payload={"enrolled": len(result["enrolled"]), "refused": len(result["refused"])},
    )
    return result


@router.post("/outreach/reply")
def outreach_reply(
    body: ReplyBody,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.relationships import outreach as outreach_svc

    try:
        out = outreach_svc.record_inbound_reply(
            wid,
            body.contact_id,
            body=body.body,
            classification=body.classification,
            message_id=body.message_id,
            in_reply_to=body.in_reply_to,
        )
    except ValueError as exc:
        if str(exc) == "contact_not_found":
            raise HTTPException(status_code=404, detail="Not found") from exc
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit_outreach_action(
        "ops.outreach.reply",
        wid,
        target_ref=body.contact_id,
        payload={"classification": body.classification},
    )
    return out


@router.post("/outreach/signal-sweep")
async def outreach_signal_sweep(
    body: ArmBody | None = None,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """Layer A HTTP disabled in Phases 0–2. Collectors stay for OT-48 behind layer_a_ingest."""
    _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    return JSONResponse(
        status_code=403,
        content={"blocked": "layer_a_http_disabled"},
    )


class IngestBody(BaseModel):
    source: str
    raw_text: str = ""
    url: str | None = None
    trigger_code: str | None = None
    company_name: str | None = None
    enrich: bool = False


class PromoteBody(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    role: str | None = None


class ScoreBody(BaseModel):
    contact_id: str


class ContactPatchBody(BaseModel):
    why_this_week: str | None = None
    recipient_geo: str | None = None
    mutual_count: int | None = None
    role: str | None = None
    score_override: str | None = None
    score_override_reason: str | None = None


class DraftCreateBody(BaseModel):
    contact_id: str
    kind: str = "connect"
    send_mode: str | None = None


class DraftPatchBody(BaseModel):
    body: str | None = None
    why_this_week: str | None = None


class SentBody(BaseModel):
    channel: str = "linkedin_connect"
    send_mode: str | None = None


class OutcomeBody(BaseModel):
    outcome: str


class SkuAttachBody(BaseModel):
    contact_id: str
    reviewed: bool = False


class OnboardingPatchBody(BaseModel):
    who: str | None = None
    roles: list[str] | None = None
    excludes: list[str] | None = None
    sender_geo: str | None = None
    sender_postal_address: str | None = None
    outbound_mode: str | None = None
    voice: dict[str, Any] | None = None
    aup_accepted: bool | None = None
    dpa_accepted: bool | None = None


class AupBody(BaseModel):
    accepted: bool = True


class DpaBody(BaseModel):
    accepted: bool = True


class PauseBody(BaseModel):
    reason: str = "admin_pause"


def _outreach_err(exc: Exception) -> HTTPException:
    from app.gtm.relationships.service import OutreachError
    from app.gtm.relationships.skus import SkuAttachError

    if isinstance(exc, OutreachError):
        return HTTPException(status_code=exc.status, detail=exc.code)
    if isinstance(exc, SkuAttachError):
        code = 404 if exc.code == "contact_not_found" else 409
        return HTTPException(status_code=code, detail=exc.code)
    if isinstance(exc, ValueError) and str(exc) in {
        "contact_not_found",
        "signal_not_found",
        "draft_not_found",
        "touch_not_found",
    }:
        return HTTPException(status_code=404, detail="Not found")
    return HTTPException(status_code=400, detail=str(exc))


def _audit_outreach_action(
    topic: str,
    workspace_id: str,
    *,
    target_ref: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    audit(
        topic,
        workspace_id=workspace_id,
        actor_id=LOCAL_PERSON_ID,
        actor_kind="person",
        surface="api",
        target_ref=target_ref,
        outcome="ok",
        payload=payload or {},
    )


@router.get("/outreach/status")
def outreach_status(
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.relationships.examples import labelled_examples
    from app.gtm.relationships.service import outreach_status as status_svc

    out = status_svc(wid)
    out["examples"] = labelled_examples()
    return out


@router.post("/outreach/ingest")
def outreach_ingest(
    body: IngestBody,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.relationships.ingest import ingest_paste

    if body.enrich:
        body = body.model_copy(update={"enrich": False})
    try:
        out = ingest_paste(
            wid,
            source=body.source,
            raw_text=body.raw_text,
            url=body.url,
            trigger_code=body.trigger_code,
            company_name=body.company_name,
            enrich=False,
        )
    except ValueError as exc:
        raise _outreach_err(exc) from exc
    _audit_outreach_action(
        "ops.outreach.ingest",
        wid,
        target_ref=str(out.get("contact_id") or out.get("signal_id") or ""),
        payload={"source": body.source},
    )
    return out


@router.post("/outreach/signals/{signal_id}/promote")
def outreach_promote(
    signal_id: str,
    body: PromoteBody | None = None,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.relationships.ingest import promote_signal_to_layer_b

    fields = body.model_dump() if body else {}
    try:
        return promote_signal_to_layer_b(wid, signal_id, contact_fields=fields)
    except ValueError as exc:
        raise _outreach_err(exc) from exc


@router.get("/outreach/queue")
def outreach_queue(
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.relationships.service import list_queue

    return list_queue(wid)


@router.post("/outreach/score")
def outreach_score(
    body: ScoreBody,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.relationships.service import OutreachError, score_and_store

    try:
        return score_and_store(wid, body.contact_id)
    except (OutreachError, ValueError) as exc:
        raise _outreach_err(exc) from exc


@router.get("/outreach/contacts/{contact_id}")
def outreach_get_contact(
    contact_id: str,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.relationships.service import OutreachError, get_contact

    try:
        return get_contact(wid, contact_id)
    except (OutreachError, ValueError) as exc:
        raise _outreach_err(exc) from exc


@router.patch("/outreach/contacts/{contact_id}")
def outreach_patch_contact(
    contact_id: str,
    body: ContactPatchBody,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.relationships.service import OutreachError, patch_contact

    try:
        fields = body.model_dump(exclude_none=True)
        out = patch_contact(wid, contact_id, fields)
    except (OutreachError, ValueError) as exc:
        raise _outreach_err(exc) from exc
    if "score_override" in fields:
        _audit_outreach_action(
            "ops.outreach.score_override",
            wid,
            target_ref=contact_id,
            payload={"score_override": fields.get("score_override")},
        )
    return out


@router.post("/outreach/drafts")
def outreach_create_draft(
    body: DraftCreateBody,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.relationships.service import OutreachError, create_draft

    if body.send_mode == "unipile_live":
        raise HTTPException(status_code=409, detail="send_mode_unipile_forbidden")
    try:
        return create_draft(wid, body.contact_id, kind=body.kind)
    except (OutreachError, ValueError) as exc:
        raise _outreach_err(exc) from exc


@router.patch("/outreach/drafts/{draft_id}")
def outreach_patch_draft(
    draft_id: str,
    body: DraftPatchBody,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.relationships.service import OutreachError, edit_draft
    try:
        return edit_draft(wid, draft_id, body.body)
    except OutreachError as exc:
        raise HTTPException(status_code=exc.status, detail="Not found") from exc
    except ValueError as exc:
        raise _outreach_err(exc) from exc


@router.post("/outreach/drafts/{draft_id}/copy")
def outreach_copy_draft(
    draft_id: str,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.relationships.service import OutreachError, mark_copied

    try:
        out = mark_copied(wid, draft_id)
    except (OutreachError, ValueError) as exc:
        raise _outreach_err(exc) from exc
    _audit_outreach_action(
        "ops.outreach.copy",
        wid,
        target_ref=draft_id,
        payload={"copied_at": out.get("copied_at")},
    )
    return out


@router.post("/outreach/drafts/{draft_id}/sent")
def outreach_sent_draft(
    draft_id: str,
    body: SentBody | None = None,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.relationships.service import OutreachError, mark_sent

    payload = body or SentBody()
    if payload.send_mode == "unipile_live":
        raise HTTPException(status_code=409, detail="send_mode_unipile_forbidden")
    try:
        out = mark_sent(wid, draft_id, channel=payload.channel)
    except (OutreachError, ValueError) as exc:
        raise _outreach_err(exc) from exc
    _audit_outreach_action(
        "ops.outreach.sent",
        wid,
        target_ref=draft_id,
        payload={"channel": payload.channel, "sent_at": out.get("sent_at")},
    )
    return out


@router.post("/outreach/touches/{touch_id}/outcome")
def outreach_touch_outcome(
    touch_id: str,
    body: OutcomeBody,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.relationships.service import OutreachError, log_outcome

    try:
        return log_outcome(wid, touch_id, body.outcome)
    except (OutreachError, ValueError) as exc:
        raise _outreach_err(exc) from exc


@router.get("/outreach/funnel")
def outreach_funnel(
    workspace_id: str | None = None,
    window_days: int = Query(default=30, ge=1, le=365),
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.relationships.funnel import funnel_metrics

    return funnel_metrics(wid, window_days=window_days)


@router.post("/outreach/skus/{sku_key}/attach")
def outreach_sku_attach(
    sku_key: str,
    body: SkuAttachBody,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.relationships.skus import attach_sku

    try:
        out = attach_sku(wid, body.contact_id, sku_key, reviewed=body.reviewed)
    except Exception as exc:
        raise _outreach_err(exc) from exc
    _audit_outreach_action(
        "ops.outreach.attach_sku",
        wid,
        target_ref=body.contact_id,
        payload={"sku_key": sku_key},
    )
    return out


@router.get("/outreach/onboarding")
def outreach_onboarding_get(
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.relationships.service import onboarding_status

    return onboarding_status(wid)


@router.patch("/outreach/onboarding")
def outreach_onboarding_patch(
    body: OnboardingPatchBody,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.relationships.service import OutreachError, patch_onboarding

    try:
        return patch_onboarding(wid, body.model_dump(exclude_none=True))
    except (OutreachError, ValueError) as exc:
        raise _outreach_err(exc) from exc


@router.post("/outreach/aup")
def outreach_accept_aup(
    body: AupBody | None = None,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.relationships.hosted import accept_aup
    from app.gtm.relationships.service import OutreachError

    if body is not None and body.accepted is False:
        raise HTTPException(status_code=400, detail="aup_not_accepted")
    try:
        return accept_aup(wid)
    except OutreachError as exc:
        raise _outreach_err(exc) from exc


@router.post("/outreach/dpa")
def outreach_accept_dpa(
    body: DpaBody | None = None,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.relationships.hosted import accept_dpa
    from app.gtm.relationships.service import OutreachError

    if body is not None and body.accepted is False:
        raise HTTPException(status_code=400, detail="dpa_not_accepted")
    try:
        return accept_dpa(wid)
    except OutreachError as exc:
        raise _outreach_err(exc) from exc


@router.post("/outreach/admin/pause")
def outreach_admin_pause(
    body: PauseBody | None = None,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.relationships.abuse import pause_sends

    payload = body or PauseBody()
    return pause_sends(wid, reason=payload.reason)


class UnsubscribeBody(BaseModel):
    token: str


@public_router.get("/public/unsubscribe")
def public_unsubscribe_preview(token: Annotated[str, Query(alias="t")] = ""):
    from html import escape
    from urllib.parse import quote
    from app.gtm.relationships.service import OutreachError
    from app.gtm.relationships.unsubscribe import preview_unsubscribe

    try:
        preview_unsubscribe(token)
    except OutreachError as exc:
        raise HTTPException(status_code=exc.status, detail="You can reply to your email with no thanks to stop these emails.") from exc
    action = escape("/public/unsubscribe?t=" + quote(token), quote=True)
    return HTMLResponse('<!doctype html><html lang="en"><meta charset="utf-8"><title>Stop these emails</title>'
        '<h1>Stop these emails</h1><p>You can stop further emails from this sender.</p>'
        f'<form method="post" action="{action}"><button type="submit">Stop these emails</button></form></html>',
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@public_router.post("/public/unsubscribe")
def public_unsubscribe(body: UnsubscribeBody | None = None, token: Annotated[str | None, Query(alias="t")] = None):
    """CAN-SPAM opt-out. No ops API key. Token is bound to one workspace."""
    from app.gtm.relationships.service import OutreachError
    from app.gtm.relationships.unsubscribe import honor_unsubscribe_token

    try:
        if body is None and not token:
            raise HTTPException(status_code=422, detail="You can use the opt-out link in your email or reply with no thanks.")
        result = honor_unsubscribe_token(body.token if body else token)
        if body is not None:
            return result
        return HTMLResponse('<!doctype html><html lang="en"><meta charset="utf-8"><title>Emails stopped</title>'
            '<h1>Emails stopped</h1><p>You will not receive further emails from this sender. You can close this page.</p></html>',
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
    except OutreachError as exc:
        if exc.code == "unsubscribe_unavailable":
            raise HTTPException(
                status_code=503,
                detail="Unsubscribe is temporarily unavailable. Please try again.",
            ) from exc
        raise HTTPException(status_code=exc.status, detail="You can reply to your email with no thanks to stop these emails.") from exc


# ---------------------------------------------------------------------------
# AI GTM — OpenLeads find + JSON / Excel export
# ---------------------------------------------------------------------------


class AiGtmFindBody(BaseModel):
    query: str = "B2B SaaS founders"
    count: int = Field(default=50, ge=1, le=200)
    industry: str | None = "B2B"
    source: str | None = "auto"
    deep: bool = True
    verified_only: bool = False
    auto_outreach: bool = False
    region: str | None = None
    icp_profile: str = "saas"


class AiGtmExportBody(BaseModel):
    leads: list[dict[str, Any]]
    format: str = Field(default="json", pattern="^(json|xlsx|excel|csv)$")


class AiGtmLabGenerateBody(BaseModel):
    """Generate leads for Marketing Lab — never starts outreach."""

    count: int = Field(default=15, ge=1, le=80)
    random: bool = False
    scenario_id: str | None = None
    industry: str | None = None
    role: str | None = None
    company_size: str | None = None
    geo: str | None = None
    keyword: str | None = None
    query: str | None = None
    source: str | None = "auto"
    deep: bool = True
    verified_only: bool = False
    icp_profile: str = "saas"
    # Lab path never requires send_enabled; discovery flag optional for continuous ticks.
    enable_discovery: bool = False


class AiGtmImportBody(BaseModel):
    emails: str = ""
    linkedin_urls: str = ""
    enrich: bool = True


class AiGtmOutreachBody(BaseModel):
    contact_ids: list[str] | None = None
    auto_send: bool = False
    product_name: str | None = None
    approval_id: str | None = None
    mailbox_id: str | None = None

    @field_validator("mailbox_id", mode="before")
    @classmethod
    def validate_mailbox_id(cls, value):
        from app.connections.mailboxes import enabled
        return value if enabled() else None


class ExternalExecutionBody(BaseModel):
    approval_id: str | None = None


class ExternalApprovalDecisionBody(BaseModel):
    approval_id: str
    decision: str = Field(pattern="^(approved|rejected)$")
    note: str | None = Field(default=None, max_length=1000)


class AiGtmGmailConnectBody(BaseModel):
    user_id: str | None = None
    intent: str | None = None
    mailbox_id: str | None = None


class AiGtmRunBody(BaseModel):
    discovery: bool | None = None
    send: bool | None = None
    icp_query: str | None = None
    week_index: int | None = None
    find_batch_size: int | None = None


class AiGtmMarketProfileBody(BaseModel):
    name: str = "Primary market"
    seller: dict[str, Any] = Field(default_factory=dict)
    product: dict[str, Any] = Field(default_factory=dict)
    segment: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class AiGtmGenerateThesisBody(BaseModel):
    use_llm: bool = True


class AiGtmThesisApprovalBody(BaseModel):
    actor_id: str | None = None


class AiGtmExperimentStatusBody(BaseModel):
    status: str = Field(pattern="^(running|paused|completed|cancelled)$")


class AiGtmOutcomeBody(BaseModel):
    kind: str
    thesis_id: str | None = None
    experiment_id: str | None = None
    contact_id: str | None = None
    channel: str | None = None
    variant: str | None = None
    value: float | None = None
    currency: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AiGtmContactPatchBody(BaseModel):
    hook: str | None = None
    preferred_channel: str | None = None
    sequence_kind: str | None = None
    do_not_contact: bool | None = None
    sequence: dict[str, Any] | None = None
    regenerate_day: int | None = None


class AiGtmOpenGtmPipelineBody(BaseModel):
    industry: str = "B2B SaaS"
    region: str = "San Francisco"
    limit: int = Field(default=10, ge=1, le=30)
    language: str = "en"
    icp_profile: str = "saas"
    ingest: bool = True
    auto_outreach: bool = False


class AiGtmOpenGtmQualifyBody(BaseModel):
    leads: list[dict[str, Any]]
    icp_profile: str = "saas"


class AiGtmOpenGtmMessageBody(BaseModel):
    leads: list[dict[str, Any]]
    language: str = "en"
    region: str = ""


class AiGtmOpenGtmResearchBody(BaseModel):
    domain: str | None = None
    company: str | None = None
    industry: str | None = None
    leads: list[dict[str, Any]] | None = None


class AiGtmOpenGtmContextBody(BaseModel):
    url: str
    country: str | None = None
    language: str | None = None


class AiGtmOpenGtmAnalyticsBody(BaseModel):
    url: str | None = None
    company: str | None = None
    industry: str | None = None
    health_only: bool = False
    mentions_only: bool = False


class AiGtmOpenGtmKeywordsBody(BaseModel):
    domain: str
    limit: int = Field(default=50, ge=1, le=200)
    language: str = "en"
    region: str = "US"


class AiGtmOpenGtmBlogBody(BaseModel):
    domain: str
    keyword: str
    word_count: int = Field(default=2000, ge=500, le=5000)
    language: str = "en"
    country: str = "United States"


class AiGtmOpenGtmSyncBody(BaseModel):
    leads: list[dict[str, Any]]
    dry_run: bool = True


@router.get("/ai-gtm/status")
def ai_gtm_status(
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.lead_pipeline import status as pipeline_status
    from app.gtm.supymem.pipeline import enrich_status

    base = pipeline_status()
    extra = enrich_status()
    from app.gtm.supymem.factory import funnel, get_or_create_state
    from app.gtm.supymem.linkedin import configured as unipile_ready

    base["hunter"] = extra["hunter"]
    base["brightdata"] = extra["brightdata"]
    base["gmail"] = extra["gmail"]
    base["llm"] = extra["llm"]
    base["dry_run"] = extra["dry_run"]
    base["linkedin_dm"] = extra["linkedin_dm"]
    base["unipile"] = extra.get("unipile") or {"ready": unipile_ready()}
    base["engine"] = get_or_create_state(wid)
    base["funnel"] = funnel(wid)
    from app.gtm.localization.experiments import execution_context

    base["localization"] = execution_context(wid)
    return base


@router.post("/ai-gtm/find")
async def ai_gtm_find(
    body: AiGtmFindBody,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """Find public contacts, persist evidence, and require separate send approval."""
    import asyncio

    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.localization.experiments import execution_context

    execution = execution_context(wid)
    if not execution["allowed"]:
        raise HTTPException(status_code=409, detail=execution["reason"])
    from app.gtm.lead_pipeline import find_leads_combined
    from app.gtm.supymem.pipeline import ingest_leads, start_outreach

    try:
        result = await asyncio.to_thread(
            find_leads_combined,
            query=body.query,
            count=body.count,
            industry=body.industry,
            source=body.source,
            deep=body.deep,
            verified_only=body.verified_only,
            use_scrapy=True,
            region=body.region,
            icp_profile=body.icp_profile,
        )
    except Exception as exc:  # noqa: BLE001
        import subprocess as _sp

        if isinstance(exc, _sp.TimeoutExpired):
            raise HTTPException(status_code=504, detail="Lead find timed out") from exc
        if isinstance(exc, RuntimeError):
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if isinstance(exc, ValueError):
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        raise HTTPException(status_code=500, detail="Lead find failed") from exc

    ingested = ingest_leads(wid, list(result.get("leads") or []), enrich=False)
    result["leads"] = ingested.get("leads") or result.get("leads")
    result["contacts"] = ingested.get("contacts") or []
    result["ingested"] = ingested.get("ingested") or 0
    result["hunter_filled"] = ingested.get("hunter_filled") or 0
    outreach = None
    if body.auto_outreach and result.get("leads"):
        outreach = start_outreach(wid, auto_send=False)
        result["outreach"] = outreach
        extra = ""
        if outreach.get("blocker"):
            extra = f" Outreach blocked: {outreach['blocker']}"
        elif outreach.get("enrolled"):
            sent_n = len(outreach.get("sent") or [])
            extra = (
                f" Queued {len(outreach['enrolled'])} emails"
                f" ({sent_n} sent this tick"
                f"{', simulated' if outreach.get('dry_run') else ''})."
            )
        result["message"] = (result.get("message") or "") + extra

    audit(
        "ops.ai_gtm.find",
        workspace_id=wid,
        actor_id=LOCAL_PERSON_ID,
        actor_kind="person",
        surface="api",
        target_ref=wid,
        outcome="ok",
        payload={
            "count": result.get("count", 0),
            "openleads": result.get("openleads_count", 0),
            "scrapy": result.get("scrapy_count", 0),
            "opengtm": result.get("opengtm_count", 0),
            "ingested": result.get("ingested", 0),
            "query": body.query,
            "source": body.source,
        },
    )
    return result


@router.post("/ai-gtm/export")
def ai_gtm_export(
    body: AiGtmExportBody,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """Download the current lead set as JSON, CSV, or Excel (.xlsx)."""
    _auth_workspace(x_api_key, None, None)
    from app.gtm.lead_pipeline import (
        leads_to_csv_bytes,
        leads_to_json_bytes,
        leads_to_xlsx_bytes,
    )

    fmt = body.format.lower()
    if fmt in ("xlsx", "excel"):
        data = leads_to_xlsx_bytes(body.leads)
        return Response(
            content=data,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": 'attachment; filename="ai_gtm_leads.xlsx"'},
        )
    if fmt == "csv":
        data = leads_to_csv_bytes(body.leads)
        return Response(
            content=data,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="ai_gtm_leads.csv"'},
        )
    data = leads_to_json_bytes(body.leads)
    return Response(
        content=data,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="ai_gtm_leads.json"'},
    )


@router.get("/ai-gtm/lab/scenarios")
def ai_gtm_lab_scenarios(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """Named ICP situations for lab lead generation."""
    _auth_workspace(x_api_key, None, None)
    from app.gtm.lead_scenarios import list_presets

    return {"scenarios": list_presets()}


@router.post("/ai-gtm/lab/generate")
async def ai_gtm_lab_generate(
    body: AiGtmLabGenerateBody,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """Safe lab path: generate + persist leads without outreach / send_enabled.

    Does not require discovery_enabled. Optionally flips discovery on for
    background ticks while leaving send_enabled untouched.
    """
    import asyncio

    _require_api_key(x_api_key)
    wid = _workspace(workspace_id, x_workspace_id)
    from app.gtm.lead_scenarios import resolve_scenario
    from app.gtm.localization.experiments import execution_context
    from app.gtm.lead_pipeline import find_leads_combined
    from app.gtm.supymem import factory as gtm_factory
    from app.gtm.supymem.pipeline import ingest_leads, list_contacts

    execution = execution_context(wid)
    if not execution["allowed"]:
        raise HTTPException(status_code=409, detail=execution["reason"])

    scenario = resolve_scenario(
        scenario_id=body.scenario_id,
        randomize=body.random,
        industry=body.industry,
        role=body.role,
        company_size=body.company_size,
        geo=body.geo,
        keyword=body.keyword,
        query=body.query,
    )
    if body.enable_discovery:
        gtm_factory.set_run(wid, discovery=True)

    try:
        result = await asyncio.to_thread(
            find_leads_combined,
            query=scenario["query"],
            count=body.count,
            industry=scenario.get("industry") or "B2B",
            source=body.source,
            deep=body.deep,
            verified_only=body.verified_only,
            use_scrapy=True,
            region=scenario.get("geo"),
            icp_profile=body.icp_profile,
        )
    except Exception as exc:  # noqa: BLE001
        import subprocess as _sp

        if isinstance(exc, _sp.TimeoutExpired):
            raise HTTPException(status_code=504, detail="Lead generate timed out") from exc
        if isinstance(exc, RuntimeError):
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if isinstance(exc, ValueError):
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        raise HTTPException(status_code=500, detail="Lead generate failed") from exc

    ingested = ingest_leads(wid, list(result.get("leads") or []), enrich=False)
    gtm_factory.mark_discovered(wid)
    leads = ingested.get("leads") or result.get("leads") or []
    contacts = ingested.get("contacts") or []
    engine = gtm_factory.get_or_create_state(wid)
    fun = gtm_factory.funnel(wid)

    out = {
        "ok": True,
        "lab": True,
        "auto_outreach": False,
        "scenario": scenario,
        "query": scenario["query"],
        "industry": scenario.get("industry"),
        "region": scenario.get("geo"),
        "count": len(leads),
        "safe": result.get("safe", 0),
        "risky": result.get("risky", 0),
        "unverified": result.get("unverified", 0),
        "state": result.get("state"),
        "shortfall": result.get("shortfall"),
        "errors": result.get("errors") or [],
        "source": result.get("source") or body.source,
        "leads": leads,
        "contacts": contacts,
        "ingested": ingested.get("ingested") or 0,
        "hunter_filled": ingested.get("hunter_filled") or 0,
        "message": (result.get("message") or f"Lab found {len(leads)} contacts.") + " Outreach not started.",
        "engine": engine,
        "funnel": fun,
        "openleads_count": result.get("openleads_count"),
        "scrapy_count": result.get("scrapy_count"),
        "opengtm_count": result.get("opengtm_count"),
    }
    audit(
        "ops.ai_gtm.lab_generate",
        workspace_id=wid,
        actor_id=LOCAL_PERSON_ID,
        actor_kind="person",
        surface="api",
        target_ref=wid,
        outcome="ok",
        payload={
            "count": out["count"],
            "ingested": out["ingested"],
            "scenario_id": scenario.get("id"),
            "mode": scenario.get("mode"),
            "query": scenario["query"],
            "enable_discovery": body.enable_discovery,
        },
    )
    return out


@router.get("/ai-gtm/contacts")
def ai_gtm_contacts(
    limit: int = 200,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.supymem.pipeline import list_contacts, list_mailboxes

    return {"contacts": list_contacts(wid, limit=min(limit, 500)), "mailboxes": list_mailboxes(wid)}


@router.post("/ai-gtm/import")
def ai_gtm_import(
    body: AiGtmImportBody,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """Paste emails and/or LinkedIn profile URLs. LinkedIn is research-only."""
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.supymem.pipeline import import_identities

    result = import_identities(
        wid,
        emails=body.emails,
        linkedin_urls=body.linkedin_urls,
        enrich=body.enrich,
    )
    audit(
        "ops.ai_gtm.import",
        workspace_id=wid,
        actor_id=LOCAL_PERSON_ID,
        actor_kind="person",
        surface="api",
        target_ref=wid,
        outcome="ok",
        payload={"imported": result.get("imported", 0)},
    )
    return result


@router.get("/ai-gtm/gmail/accounts")
def ai_gtm_gmail_accounts(
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.supymem.pipeline import sync_gmail_accounts

    try:
        return sync_gmail_accounts(wid)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail={
            "code": "gmail_accounts_unavailable",
            "message": "Gmail accounts could not be loaded. Please try again.",
        }) from exc


@router.post("/ai-gtm/gmail/connect")
def ai_gtm_gmail_connect(
    body: AiGtmGmailConnectBody | None = None,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    _require_api_key(x_api_key)
    wid = _workspace(workspace_id, x_workspace_id)
    from app.content.oauth.callbacks import start_managed
    from app.content.oauth.attempts import OAuthError
    from app.gtm.supymem.composio_gmail import user_id_for
    from app.core.local_mode import is_local

    if body and body.user_id is not None and body.user_id != user_id_for(wid):
        raise HTTPException(403, detail={"code": "provider_identity_mismatch"})
    if is_local():
        from app.connections.hub import connect_provider
        return connect_provider(wid, "gmail")
    try:
        from app.connections.mailboxes import enabled
        options = {"intent": body.intent if body else None, "mailbox_id": body.mailbox_id if body else None} if enabled() else {}
        return start_managed(workspace_id=wid, platform="gmail", **options)
    except OAuthError as exc:
        raise HTTPException(409, detail={"code": exc.code, "message": exc.message}) from exc


@router.get("/ai-gtm/gmail/callback")
def ai_gtm_gmail_callback(request: Request):
    from app.content.oauth.callbacks import callback_response

    return callback_response("gmail", request)


@router.post("/ai-gtm/outreach/start")
def ai_gtm_outreach_start(
    body: AiGtmOutreachBody | None = None,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.supymem.pipeline import start_outreach
    from app.connections.mailboxes import enabled

    result = start_outreach(
        wid,
        contact_ids=body.contact_ids if body else None,
        auto_send=bool(body and body.auto_send),
        approval_id=body.approval_id if body else None,
        product_name=body.product_name if body else None,
        **({"mailbox_id": body.mailbox_id if body else None} if enabled() else {}),
    )
    audit(
        "ops.ai_gtm.outreach",
        workspace_id=wid,
        actor_id=LOCAL_PERSON_ID,
        actor_kind="person",
        surface="api",
        target_ref=wid,
        outcome="ok",
        payload={"enrolled": len(result.get("enrolled") or [])},
    )
    return result


@router.post("/ai-gtm/outreach/tick")
def ai_gtm_outreach_tick(
    body: ExternalExecutionBody | None = None,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.supymem.pipeline import tick_send_queue

    return tick_send_queue(wid, approval_id=body.approval_id if body else None)


def _approval_actor() -> str:
    """Resolve the verified gateway subject to the canonical workspace person."""
    scope = current_or_none()
    if scope is None or scope.actor in {"", "anonymous", "system"}:
        return LOCAL_PERSON_ID
    from app.authz.workspace import subject_person_id

    return subject_person_id(scope.actor)


def _decide_external_review(
    *, workspace_id: str, approval_id: str, subject_ref: str, body: ExternalApprovalDecisionBody
) -> dict[str, Any]:
    """Use the generic role-aware approval service; never trust client actor IDs."""
    from app.approvals import service

    view = service.get_approval(approval_id)
    if (
        view is None
        or view.workspace_id != workspace_id
        or view.kind != "external_comms"
        or view.subject_ref != subject_ref
    ):
        raise HTTPException(status_code=404, detail={"code": "external_review_not_found"})
    try:
        decided = service.decide(
            approval_id,
            decision=body.decision,
            decided_by=_approval_actor(),
            note=body.note,
            surface="desktop",
        )
    except service.ApprovalError as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code, "message": exc.message}) from exc
    return decided.to_dict()


@router.post("/ai-gtm/outreach/queue/{queue_id}/review")
def ai_gtm_queue_review(
    queue_id: str,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """Create a server-derived review for one queued email; no digest input."""
    _require_api_key(x_api_key)
    wid = _workspace(workspace_id, x_workspace_id)
    from app.approvals.external import gtm_email_review, propose_external_approval

    try:
        target, review = gtm_email_review(wid, queue_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": str(exc)}) from exc
    approval = propose_external_approval(
        workspace_id=wid,
        subject_ref=queue_id,
        action="gtm_email_send",
        target=target,
        summary=f"Send reviewed email to {review['recipient']}",
        review=review,
    )
    audit(
        "outbound.review_requested",
        workspace_id=wid,
        actor_id=_approval_actor(),
        actor_kind="person",
        target_ref=queue_id,
        approval_id=approval["id"],
        payload={"action": "gtm_email_send", "credit_status": review["credit_status"]},
    )
    return {"approval": approval, "review": review}


@router.post("/ai-gtm/outreach/queue/{queue_id}/approval")
def ai_gtm_queue_approval(
    queue_id: str,
    body: ExternalApprovalDecisionBody,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    _require_api_key(x_api_key)
    wid = _workspace(workspace_id, x_workspace_id)
    return _decide_external_review(
        workspace_id=wid, approval_id=body.approval_id, subject_ref=queue_id, body=body
    )


@router.post("/ai-gtm/outreach/queue/{queue_id}/execute")
def ai_gtm_queue_execute(
    queue_id: str,
    body: ExternalExecutionBody,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """Execute exactly one approved, priced email; never sweep the queue."""
    _require_api_key(x_api_key)
    wid = _workspace(workspace_id, x_workspace_id)
    from app.gtm.supymem.pipeline import execute_approved_queue

    result = execute_approved_queue(wid, queue_id, approval_id=body.approval_id)
    if result.get("skipped"):
        code = str(result.get("reason") or result.get("error") or "outbound_execution_failed")
        status = 402 if code == "insufficient_credits" else 409
        raise HTTPException(status_code=status, detail={"code": code})
    return result


@router.post("/ai-gtm/run")
def ai_gtm_run(
    body: AiGtmRunBody,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.supymem.factory import set_run

    state = set_run(
        wid,
        discovery=body.discovery,
        send=body.send,
        icp_query=body.icp_query,
        week_index=body.week_index,
        find_batch_size=body.find_batch_size,
    )
    audit(
        "ops.ai_gtm.run",
        workspace_id=wid,
        actor_id=LOCAL_PERSON_ID,
        actor_kind="person",
        surface="api",
        target_ref=wid,
        outcome="ok",
        payload={"discovery": state["discovery_enabled"], "send": state["send_enabled"]},
    )
    return state


@router.post("/ai-gtm/stop")
def ai_gtm_stop(
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.supymem.factory import set_run

    return set_run(wid, discovery=False, send=False)


@router.post("/ai-gtm/department/tick")
def ai_gtm_department_tick(
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.supymem.workers import tick_department

    return tick_department(wid)


@router.get("/ai-gtm/contacts/{contact_id}")
def ai_gtm_contact_get(
    contact_id: str,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.supymem.pipeline import get_contact

    row = get_contact(wid, contact_id)
    if row is None:
        raise HTTPException(status_code=404, detail="contact not found")
    return row


@router.patch("/ai-gtm/contacts/{contact_id}")
def ai_gtm_contact_patch(
    contact_id: str,
    body: AiGtmContactPatchBody,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.supymem.pipeline import regenerate_contact_draft, update_contact

    if body.regenerate_day:
        row = regenerate_contact_draft(wid, contact_id, body.regenerate_day)
    else:
        patch = body.model_dump(exclude_unset=True)
        patch.pop("regenerate_day", None)
        row = update_contact(wid, contact_id, patch)
    if row is None:
        raise HTTPException(status_code=404, detail="contact not found")
    return row


@router.post("/ai-gtm/linkedin/start")
def ai_gtm_linkedin_start(
    body: AiGtmOutreachBody | None = None,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.relationships.compliance import assert_linkedin_send_allowed
    from app.gtm.supymem.linkedin import enroll_linkedin

    assert_linkedin_send_allowed("human_clipboard")
    return enroll_linkedin(wid, body.contact_ids if body else None)


@router.get("/ai-gtm/learning")
def ai_gtm_learning(
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.supymem.learning import summarize

    return summarize(wid)


@router.get("/ai-gtm/market-profiles")
def ai_gtm_market_profiles(
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.localization.service import list_market_profiles

    return {"profiles": list_market_profiles(wid)}


@router.post("/ai-gtm/market-profiles")
def ai_gtm_market_profile_create(
    body: AiGtmMarketProfileBody,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.localization.service import create_market_profile

    try:
        result = create_market_profile(
            wid,
            name=body.name,
            seller=body.seller,
            product=body.product,
            segment=body.segment,
            constraints=body.constraints,
            evidence=body.evidence,
            created_by=LOCAL_PERSON_ID,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit(
        "ops.ai_gtm.market_profile.create",
        workspace_id=wid,
        actor_id=LOCAL_PERSON_ID,
        actor_kind="person",
        surface="api",
        target_ref=result["id"],
        outcome="ok",
        payload={"version": result["version"]},
    )
    return result


@router.get("/ai-gtm/theses")
def ai_gtm_theses(
    profile_id: str | None = None,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.localization.experiments import execution_context
    from app.gtm.localization.service import list_theses

    return {
        "theses": list_theses(wid, profile_id),
        "execution": execution_context(wid),
    }


@router.post("/ai-gtm/market-profiles/{profile_id}/theses")
async def ai_gtm_thesis_generate(
    profile_id: str,
    body: AiGtmGenerateThesisBody | None = None,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.localization.service import generate_thesis

    try:
        result = await generate_thesis(
            wid, profile_id, use_llm=body.use_llm if body else True
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit(
        "ops.ai_gtm.thesis.generate",
        workspace_id=wid,
        actor_id=LOCAL_PERSON_ID,
        actor_kind="person",
        surface="api",
        target_ref=result["id"],
        outcome="ok",
        payload={"profile_id": profile_id, "model": result["model"]},
    )
    return result


@router.post("/ai-gtm/theses/{thesis_id}/approve")
def ai_gtm_thesis_approve(
    thesis_id: str,
    body: AiGtmThesisApprovalBody | None = None,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.localization.service import approve_thesis

    actor = (body.actor_id if body else None) or LOCAL_PERSON_ID
    try:
        result = approve_thesis(wid, thesis_id, actor_id=actor)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit(
        "ops.ai_gtm.thesis.approve",
        workspace_id=wid,
        actor_id=actor,
        actor_kind="person",
        surface="api",
        target_ref=thesis_id,
        outcome="ok",
        payload={"profile_id": result["profile_id"], "version": result["version"]},
    )
    return result


@router.get("/ai-gtm/experiments")
def ai_gtm_experiments(
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.localization.experiments import list_experiments

    return {"experiments": list_experiments(wid)}


@router.post("/ai-gtm/theses/{thesis_id}/experiments")
def ai_gtm_experiment_compile(
    thesis_id: str,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.localization.experiments import compile_experiment

    try:
        return compile_experiment(wid, thesis_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/ai-gtm/experiments/{experiment_id}/status")
def ai_gtm_experiment_status(
    experiment_id: str,
    body: AiGtmExperimentStatusBody,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.localization.experiments import set_experiment_status

    try:
        return set_experiment_status(wid, experiment_id, body.status)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/ai-gtm/experiments/{experiment_id}/summary")
def ai_gtm_experiment_summary(
    experiment_id: str,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.localization.experiments import summarize_experiment

    try:
        return summarize_experiment(wid, experiment_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/ai-gtm/outcomes")
def ai_gtm_outcome_record(
    body: AiGtmOutcomeBody,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.localization.experiments import record_outcome

    return record_outcome(
        wid,
        body.kind,
        thesis_id=body.thesis_id,
        experiment_id=body.experiment_id,
        contact_id=body.contact_id,
        channel=body.channel,
        variant=body.variant,
        value=body.value,
        currency=body.currency,
        metadata=body.metadata,
    )


# ---------------------------------------------------------------------------
# OpenGTM thin bridge — discover / qualify / message / AEO / content
# ---------------------------------------------------------------------------


def _opengtm_exc(exc: Exception) -> HTTPException:
    import subprocess as _sp

    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail={"code": "opengtm_invalid_input", "message": "Invalid research tool input."})
    if isinstance(exc, _sp.TimeoutExpired):
        return HTTPException(status_code=504, detail={
            "code": "opengtm_timeout", "message": "The research tool timed out. Please try again."
        })
    if isinstance(exc, RuntimeError):
        return HTTPException(status_code=503, detail={
            "code": "opengtm_unavailable", "message": "This research tool is unavailable. Try another research option."
        })
    return HTTPException(status_code=500, detail={
        "code": "opengtm_failed", "message": "The research tool could not complete. Please try again."
    })


@router.get("/ai-gtm/opengtm/status")
def ai_gtm_opengtm_status(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    _auth_workspace(x_api_key, None, None)
    from app.gtm import opengtm_bridge as ogtm

    return ogtm.status()


@router.post("/ai-gtm/opengtm/pipeline")
async def ai_gtm_opengtm_pipeline(
    body: AiGtmOpenGtmPipelineBody,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """Full OpenGTM loop: discover → research → ICP qualify → patterns A–E."""
    import asyncio

    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.localization.experiments import execution_context

    execution = execution_context(wid)
    if not execution["allowed"]:
        raise HTTPException(status_code=409, detail=execution["reason"])
    from app.gtm import opengtm_bridge as ogtm
    from app.gtm.supymem.pipeline import ingest_leads, start_outreach

    try:
        result = await asyncio.to_thread(
            ogtm.pipeline,
            industry=body.industry,
            region=body.region,
            limit=body.limit,
            language=body.language,
            icp_profile=body.icp_profile,
            no_sync=True,
            dry_run=True,
        )
    except Exception as exc:  # noqa: BLE001
        raise _opengtm_exc(exc) from exc

    if body.ingest and result.get("leads"):
        ingested = ingest_leads(wid, list(result["leads"]), enrich=True)
        result["leads"] = ingested.get("leads") or result["leads"]
        result["contacts"] = ingested.get("contacts") or []
        result["ingested"] = ingested.get("ingested") or 0
        if body.auto_outreach:
            result["outreach"] = start_outreach(wid, auto_send=False)

    audit(
        "ops.ai_gtm.opengtm.pipeline",
        workspace_id=wid,
        actor_id=LOCAL_PERSON_ID,
        actor_kind="person",
        surface="api",
        target_ref=wid,
        outcome="ok",
        payload={
            "count": result.get("count", 0),
            "hot": result.get("hot", 0),
            "industry": body.industry,
            "region": body.region,
        },
    )
    return result


@router.post("/ai-gtm/opengtm/qualify")
async def ai_gtm_opengtm_qualify(
    body: AiGtmOpenGtmQualifyBody,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    import asyncio

    _auth_workspace(x_api_key, None, None)
    from app.gtm import opengtm_bridge as ogtm

    try:
        return await asyncio.to_thread(
            ogtm.qualify, body.leads, icp_profile=body.icp_profile
        )
    except Exception as exc:  # noqa: BLE001
        raise _opengtm_exc(exc) from exc


@router.post("/ai-gtm/opengtm/message")
async def ai_gtm_opengtm_message(
    body: AiGtmOpenGtmMessageBody,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    import asyncio

    _auth_workspace(x_api_key, None, None)
    from app.gtm import opengtm_bridge as ogtm

    try:
        return await asyncio.to_thread(
            ogtm.message,
            body.leads,
            language=body.language,
            region=body.region,
        )
    except Exception as exc:  # noqa: BLE001
        raise _opengtm_exc(exc) from exc


@router.post("/ai-gtm/opengtm/research")
async def ai_gtm_opengtm_research(
    body: AiGtmOpenGtmResearchBody,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    import asyncio

    _auth_workspace(x_api_key, None, None)
    from app.gtm import opengtm_bridge as ogtm

    try:
        return await asyncio.to_thread(
            ogtm.research,
            domain=body.domain,
            company=body.company,
            industry=body.industry,
            leads=body.leads,
        )
    except Exception as exc:  # noqa: BLE001
        raise _opengtm_exc(exc) from exc


@router.post("/ai-gtm/opengtm/context")
async def ai_gtm_opengtm_context(
    body: AiGtmOpenGtmContextBody,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    import asyncio

    _auth_workspace(x_api_key, None, None)
    from app.gtm import opengtm_bridge as ogtm

    try:
        return await asyncio.to_thread(
            ogtm.context,
            url=body.url,
            country=body.country,
            language=body.language,
        )
    except Exception as exc:  # noqa: BLE001
        raise _opengtm_exc(exc) from exc


@router.post("/ai-gtm/opengtm/analytics")
async def ai_gtm_opengtm_analytics(
    body: AiGtmOpenGtmAnalyticsBody,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    import asyncio

    _auth_workspace(x_api_key, None, None)
    from app.gtm import opengtm_bridge as ogtm

    try:
        return await asyncio.to_thread(
            ogtm.analytics,
            url=body.url,
            company=body.company,
            industry=body.industry,
            health_only=body.health_only,
            mentions_only=body.mentions_only,
        )
    except Exception as exc:  # noqa: BLE001
        raise _opengtm_exc(exc) from exc


@router.post("/ai-gtm/opengtm/keywords")
async def ai_gtm_opengtm_keywords(
    body: AiGtmOpenGtmKeywordsBody,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    import asyncio

    _auth_workspace(x_api_key, None, None)
    from app.gtm import opengtm_bridge as ogtm

    try:
        return await asyncio.to_thread(
            ogtm.keywords,
            domain=body.domain,
            limit=body.limit,
            language=body.language,
            region=body.region,
        )
    except Exception as exc:  # noqa: BLE001
        raise _opengtm_exc(exc) from exc


@router.post("/ai-gtm/opengtm/blog")
async def ai_gtm_opengtm_blog(
    body: AiGtmOpenGtmBlogBody,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    import asyncio

    _auth_workspace(x_api_key, None, None)
    from app.gtm import opengtm_bridge as ogtm

    try:
        return await asyncio.to_thread(
            ogtm.blog,
            domain=body.domain,
            keyword=body.keyword,
            word_count=body.word_count,
            language=body.language,
            country=body.country,
        )
    except Exception as exc:  # noqa: BLE001
        raise _opengtm_exc(exc) from exc


@router.post("/ai-gtm/opengtm/sync")
async def ai_gtm_opengtm_sync(
    body: AiGtmOpenGtmSyncBody,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    import asyncio

    _auth_workspace(x_api_key, None, None)
    from app.gtm import opengtm_bridge as ogtm

    try:
        return await asyncio.to_thread(
            ogtm.sync_crm, body.leads, dry_run=body.dry_run
        )
    except Exception as exc:  # noqa: BLE001
        raise _opengtm_exc(exc) from exc


class AiGtmOpenGtmDiscoverBody(BaseModel):
    industry: str = "B2B SaaS"
    region: str = "San Francisco"
    limit: int = Field(default=10, ge=1, le=50)
    validate_urls: bool = True
    ingest: bool = False


class AiGtmOpenGtmSitemapBody(BaseModel):
    url: str
    max_urls: int = Field(default=500, ge=1, le=2000)
    validate_urls: bool = False


class AiGtmOpenGtmOutreachBody(BaseModel):
    leads: list[dict[str, Any]] = Field(default_factory=list)
    daily_limit: int = Field(default=20, ge=1, le=100)
    state_file: str | None = None
    touch: int | None = None


@router.post("/ai-gtm/opengtm/discover")
async def ai_gtm_opengtm_discover(
    body: AiGtmOpenGtmDiscoverBody,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    import asyncio

    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm import opengtm_bridge as ogtm
    from app.gtm.supymem.pipeline import ingest_leads

    try:
        result = await asyncio.to_thread(
            ogtm.discover,
            industry=body.industry,
            region=body.region,
            limit=body.limit,
            validate=body.validate_urls,
        )
    except Exception as exc:  # noqa: BLE001
        raise _opengtm_exc(exc) from exc
    if body.ingest and result.get("leads"):
        ingested = ingest_leads(wid, list(result["leads"]), enrich=True)
        result["ingested"] = ingested.get("ingested")
        result["contacts"] = ingested.get("contacts")
        result["leads"] = ingested.get("leads") or result["leads"]
    return result


@router.post("/ai-gtm/opengtm/sitemap")
async def ai_gtm_opengtm_sitemap(
    body: AiGtmOpenGtmSitemapBody,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    import asyncio

    _auth_workspace(x_api_key, None, None)
    from app.gtm import opengtm_bridge as ogtm

    try:
        return await asyncio.to_thread(
            ogtm.sitemap,
            url=body.url,
            max_urls=body.max_urls,
            validate=body.validate_urls,
        )
    except Exception as exc:  # noqa: BLE001
        raise _opengtm_exc(exc) from exc


@router.post("/ai-gtm/opengtm/outreach/load")
async def ai_gtm_opengtm_outreach_load(
    body: AiGtmOpenGtmOutreachBody,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    import asyncio

    _auth_workspace(x_api_key, None, None)
    from app.gtm import opengtm_bridge as ogtm

    try:
        return await asyncio.to_thread(
            ogtm.outreach_load,
            body.leads,
            state_file=body.state_file,
            daily_limit=body.daily_limit,
        )
    except Exception as exc:  # noqa: BLE001
        raise _opengtm_exc(exc) from exc


@router.get("/ai-gtm/opengtm/outreach/status")
def ai_gtm_opengtm_outreach_status(
    state_file: str | None = None,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    _auth_workspace(x_api_key, None, None)
    from app.gtm import opengtm_bridge as ogtm

    try:
        return ogtm.outreach_status(state_file=state_file)
    except Exception as exc:  # noqa: BLE001
        raise _opengtm_exc(exc) from exc


@router.get("/ai-gtm/opengtm/outreach/queue")
def ai_gtm_opengtm_outreach_queue(
    state_file: str | None = None,
    touch: int | None = None,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    _auth_workspace(x_api_key, None, None)
    from app.gtm import opengtm_bridge as ogtm

    try:
        return ogtm.outreach_queue(state_file=state_file, touch=touch)
    except Exception as exc:  # noqa: BLE001
        raise _opengtm_exc(exc) from exc


# ---------------------------------------------------------------------------
# Content engine (doc 119)
# ---------------------------------------------------------------------------


class ContentBriefBody(BaseModel):
    brief: Any = None
    source_name: str | None = None
    venture_id: str = Field(min_length=1, max_length=160)


class ContentGenerationBody(BaseModel):
    model_config = {"extra": "forbid"}
    venture_id: str = Field(min_length=1, max_length=160)
    kind: str
    brief_id: str | None = None
    topic_id: str | None = None
    platform: str | None = None
    count: int = Field(default=12, ge=1, le=12)


class ContentDraftBody(ContentGenerationBody):
    idempotency_key: str = Field(min_length=8, max_length=160)
    input_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    resource_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    ceiling_micro: int = Field(ge=0)


def _content_generation_request(body):
    return {key: value for key, value in body.model_dump().items() if key in
        {"kind", "brief_id", "topic_id", "platform", "count"} and value is not None}


def _content_generation_error(exc):
    code = str(exc)
    import re
    safe = code if re.fullmatch(r"content_[a-z_]{1,80}", code) else "content_generation_unavailable"
    detail = {"code": safe}
    from app.core.local_mode import is_local
    if is_local():
        from app.content.agent.policy import content_blocked_message
        detail["message"] = content_blocked_message() if safe == "content_disabled" else (
            "Open Content and check your saved draft status before trying again. If your model cannot connect, "
            "check your provider, model and key in Keys and connections.")
    return HTTPException(status_code=404 if safe == "content_not_found" else 409, detail=detail)


@router.post("/content/generation/quote")
def content_generation_quote(body: ContentGenerationBody, workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key")):
    _require_api_key(x_api_key)
    from app.content.generation import quote
    try:
        return quote(_workspace(workspace_id, x_workspace_id), body.venture_id, _content_generation_request(body))
    except ValueError as exc:
        raise _content_generation_error(exc) from exc


@router.get("/content/generation/runs")
def content_generation_runs(venture_id: str, workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key")):
    _require_api_key(x_api_key)
    from app.content.generation import recent
    try:
        return recent(_workspace(workspace_id, x_workspace_id), venture_id)
    except ValueError as exc:
        raise _content_generation_error(exc) from exc


@router.get("/content/generation/runs/{run_id}")
def content_generation_run(run_id: str, venture_id: str, workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key")):
    _require_api_key(x_api_key)
    from app.content.generation import get
    try:
        return get(_workspace(workspace_id, x_workspace_id), venture_id, run_id)
    except ValueError as exc:
        raise _content_generation_error(exc) from exc


def _start_content(body, wid, kind):
    from app.content.generation import start
    if body.kind != kind:
        raise HTTPException(422, detail={"code": "content_kind_invalid"})
    try:
        result = start(wid, body.venture_id, _content_generation_request(body), idempotency_key=body.idempotency_key,
            input_digest=body.input_digest, resource_digest=body.resource_digest, ceiling_micro=body.ceiling_micro)
        return JSONResponse(status_code=202, content=result)
    except ValueError as exc:
        raise _content_generation_error(exc) from exc


class ContentOAuthStartBody(BaseModel):
    return_to: str | None = None


def _console_url() -> str:
    import os

    return (
        os.environ.get("CONSOLE_URL") or "http://127.0.0.1:3100"
    ).rstrip("/")


@router.get("/content/status")
def content_status(
    workspace_id: str | None = None,
    venture_id: str | None = None,
    sync: bool = False,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    import os

    from app.content import brief as brief_store
    from app.content import director, engine
    from app.content.oauth import flow, provider_status
    from app.content.platforms import dry_run_enabled

    synced: list[dict] = []
    if sync:
        try:
            from app.content import composio_social

            if composio_social.configured():
                synced = composio_social.sync_workspace(wid)
        except Exception as exc:
            # Status must still render when Composio is briefly unreachable.
            synced = []
            _ = exc

    if venture_id:
        from app.content.generation import authority
        try:
            authority(wid, venture_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail={"code": "content_scope_mismatch"}) from exc
    current = brief_store.active(wid, venture_id)
    agent_info: dict = {}
    try:
        from app.content.agent import agent_status as agent_status_fn

        agent_info = agent_status_fn(wid)
    except Exception:
        agent_info = {"enabled": False}

    return {
        "workspace_id": wid,
        "brief": current,
        "has_brief": current is not None,
        "accounts": flow.list_accounts(wid),
        "providers": provider_status(),
        "dry_run": dry_run_enabled(),
        "engine_enabled": os.environ.get(
            "CONTENT_ENGINE_ENABLED", ""
        ).strip().lower()
        in {"1", "true", "yes", "on"},
        "queued_topics": director.queued_count(wid, venture_id=venture_id, brief_id=current.get("id") if current else None),
        "next_scheduled": engine.next_run_hint(wid),
        "composio_synced": synced,
        "composio_configured": bool(os.environ.get("COMPOSIO_API_KEY", "").strip()),
        "agent": agent_info,
    }


@router.get("/content/brief/template")
def content_brief_template(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    _auth_workspace(x_api_key, None, None)
    from app.content.brief import TEMPLATE

    return TEMPLATE


@router.post("/content/brief")
def content_brief_upload(
    body: ContentBriefBody,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.content import brief as brief_store

    payload = body.brief if body.brief is not None else None
    if payload is None:
        raise HTTPException(status_code=400, detail="brief is required")
    from app.content.generation import authority
    try:
        authority(wid, body.venture_id, write=True)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"code": "content_scope_mismatch"}) from exc
    try:
        saved = brief_store.save(
            workspace_id=wid, venture_id=body.venture_id, data=payload, source_name=body.source_name
        )
    except brief_store.BriefError as exc:
        raise HTTPException(
            status_code=422, detail={"errors": exc.errors}
        ) from exc
    audit(
        "content.brief.saved",
        workspace_id=wid,
        actor_id=LOCAL_PERSON_ID,
        actor_kind="person",
        surface="api",
        target_ref=saved["id"],
        payload={"version": saved["version"]},
    )
    return saved


@router.get("/content/accounts")
def content_accounts(
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.content.oauth import flow

    return {"accounts": flow.list_accounts(wid)}


@router.post("/content/accounts/sync")
def content_accounts_sync(
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """Pull freshly authorized Composio connections into this workspace."""
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.content import composio_social
    from app.content.oauth import flow

    if not composio_social.configured():
        raise HTTPException(
            status_code=409,
            detail={"code": "composio_missing", "message": "COMPOSIO_API_KEY is not set."},
        )
    try:
        synced = composio_social.sync_workspace(wid)
    except composio_social.ComposioSocialError as exc:
        raise HTTPException(
            status_code=502, detail={"code": exc.code, "message": exc.message}
        ) from exc
    return {"synced": synced, "accounts": flow.list_accounts(wid)}


@router.post("/content/oauth/{platform}/start")
def content_oauth_start(
    platform: str,
    body: ContentOAuthStartBody | None = None,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.content.oauth import flow

    try:
        started = flow.start_authorization(
            workspace_id=wid,
            platform=platform,
            return_to=body.return_to if body else None,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except flow.OAuthError as exc:
        raise HTTPException(
            status_code=409, detail={"code": exc.code, "message": exc.message}
        ) from exc
    audit(
        "content.oauth.start",
        workspace_id=wid,
        actor_id=flow.attempts.initiating_actor(wid),
        actor_kind="person",
        surface="api",
        target_ref=platform,
        payload={"auth_via": started.get("auth_via", "native")},
    )
    return started


@router.get("/content/oauth/{platform}/callback")
def content_oauth_callback(platform: str, request: Request):
    from app.content.oauth.callbacks import callback_response

    return callback_response(platform, request)


@router.post("/content/accounts/{account_id}/revoke")
def content_account_revoke(
    account_id: str,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    _require_api_key(x_api_key)
    wid = _workspace(workspace_id, x_workspace_id)
    from app.content.oauth import flow

    try:
        return flow.revoke_account(account_id, workspace_id=wid)
    except flow.OAuthError as exc:
        code = 404 if exc.code == "no_account" else 403 if exc.code in {
            "actor_required", "workspace_admin_required", "authorization_revoked"} else 409
        raise HTTPException(status_code=code, detail={"code": exc.code, "message": exc.message}) from exc


@router.post("/content/accounts/forget")
def content_accounts_forget(
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """Wipe every channel for this workspace (local + Composio). Fresh start."""
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.content.oauth import flow

    try:
        result = flow.forget_all(wid)
    except flow.OAuthError as exc:
        raise HTTPException(status_code=403, detail={"code": exc.code, "message": exc.message}) from exc
    audit(
        "content.accounts.forget",
        workspace_id=wid,
        actor_id=LOCAL_PERSON_ID,
        actor_kind="person",
        surface="api",
        payload={
            "revoked": len(result.get("revoked_accounts") or []),
            "composio_removed": len(result.get("composio_removed") or []),
        },
    )
    return result


@router.get("/content/agent")
def content_agent_status(
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.content.agent import agent_status

    return agent_status(wid)


@router.post("/content/agent/tick")
async def content_agent_tick(
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """Run one full agent cycle now: keepalive → sense → decide → act. Operator only."""
    _require_operator()
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.content.agent import run_agent_tick

    return await run_agent_tick(workspace_id=wid)


@router.post("/content/agent/keepalive")
def content_agent_keepalive(
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.content.agent import keepalive

    return keepalive.check_and_heal(wid)


@router.post("/content/draft")
async def content_draft(
    body: ContentDraftBody,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    _require_api_key(x_api_key)
    wid = _workspace(workspace_id, x_workspace_id)
    return _start_content(body, wid, "draft")


@router.post("/content/tick")
async def content_tick_now(
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.content.engine import tick

    return await tick(workspace_id=wid)


@router.post("/content/topics/refill")
async def content_topics_refill(
    body: ContentDraftBody,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    _require_api_key(x_api_key)
    wid = _workspace(workspace_id, x_workspace_id)
    return _start_content(body, wid, "topics")


@router.get("/content/posts")
def content_posts(
    limit: int = 40,
    venture_id: str | None = None,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.content.engine import recent_posts
    if venture_id:
        from app.content.generation import posts
        try:
            return {"posts": posts(wid, venture_id, limit)}
        except ValueError as exc:
            raise _content_generation_error(exc) from exc
    return {"posts": recent_posts(wid, limit=max(1, min(limit, 200)))}


class ContentPostEditBody(BaseModel):
    model_config = {"extra": "forbid"}
    venture_id: str
    body: str = Field(min_length=1, max_length=8000)
    title: str = Field(default="", max_length=200)
    expected_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


@router.put("/content/posts/{post_id}/draft")
def content_post_edit(post_id: str, body: ContentPostEditBody, workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key")):
    _require_api_key(x_api_key)
    from app.content.generation import edit_post
    try:
        return edit_post(_workspace(workspace_id, x_workspace_id), body.venture_id, post_id,
            body=body.body, title=body.title, expected_digest=body.expected_digest)
    except ValueError as exc:
        raise _content_generation_error(exc) from exc


@router.post("/content/posts/{post_id}/review")
def content_post_review_request(
    post_id: str,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """Create a generic approval from the persisted content revision."""
    _require_api_key(x_api_key)
    wid = _workspace(workspace_id, x_workspace_id)
    from app.core.local_mode import is_local
    if is_local():
        from app.approvals.external import local_content_draft_review
        try:
            return local_content_draft_review(wid, post_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail={"message": str(exc)}) from exc
    from app.approvals.external import content_post_review, propose_external_approval

    try:
        target, review = content_post_review(wid, post_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": str(exc)}) from exc
    approval = propose_external_approval(
        workspace_id=wid,
        subject_ref=post_id,
        action="content_publish",
        target=target,
        summary=f"Publish reviewed {review['channel']} content to {review['destination']}",
        review=review,
    )
    audit(
        "outbound.review_requested",
        workspace_id=wid,
        actor_id=_approval_actor(),
        actor_kind="person",
        target_ref=post_id,
        approval_id=approval["id"],
        payload={"action": "content_publish", "credit_status": review["credit_status"]},
    )
    return {"approval": approval, "review": review}


@router.post("/content/posts/{post_id}/approval")
def content_post_approval(
    post_id: str,
    body: ExternalApprovalDecisionBody,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    _require_api_key(x_api_key)
    wid = _workspace(workspace_id, x_workspace_id)
    return _decide_external_review(
        workspace_id=wid, approval_id=body.approval_id, subject_ref=post_id, body=body
    )


@router.post("/content/posts/{post_id}/execute")
def content_post_execute(
    post_id: str,
    body: ExternalExecutionBody,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    _require_api_key(x_api_key)
    wid = _workspace(workspace_id, x_workspace_id)
    from app.content.engine import execute_approved_post

    result = asyncio.run(execute_approved_post(wid, post_id, approval_id=body.approval_id))
    if not result.get("ok"):
        code = str(result.get("reason") or "outbound_execution_failed")
        status = 402 if code == "insufficient_credits" else 409
        detail = {"code": code}
        if code == "external_execution_disabled" and result.get("message"):
            detail["message"] = result["message"]
        raise HTTPException(status_code=status, detail=detail)
    return result


@router.get("/content/events")
def content_events(
    limit: int = 50,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.content.engine import recent_events

    return {"events": recent_events(wid, limit=max(1, min(limit, 200)))}


# ── Marketing strategist → plan → jobs → execution tick ─────────────────


@router.get("/marketing/status")
def marketing_status(
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.marketing.executor import status as execution_status

    return execution_status(wid)


@router.post("/marketing/plan")
def marketing_plan_create(
    body: dict | None = None,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """Strategist: brief → executable plan → first week of jobs."""
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    payload = body or {}
    from app.marketing.strategist import create_plan_from_brief

    try:
        return create_plan_from_brief(
            workspace_id=wid,
            horizon_days=int(payload.get("horizon_days") or 30),
            compile_days=int(payload.get("compile_days") or 7),
            activate=bool(payload.get("activate", True)),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/marketing/plan/from-research")
def marketing_plan_from_research(
    body: dict | None = None,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """Research → ContentBrief → MarketingPlan handoff."""
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    payload = body or {}
    from app.marketing.handoff import create_plan_from_research

    try:
        return create_plan_from_research(
            workspace_id=wid,
            venture_id=payload.get("venture_id"),
            research=payload.get("research") if isinstance(payload.get("research"), dict) else None,
            horizon_days=int(payload.get("horizon_days") or 30),
            compile_days=int(payload.get("compile_days") or 7),
            activate=bool(payload.get("activate", True)),
            replace_brief=bool(payload.get("replace_brief", True)),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/marketing/learning")
def marketing_learning(
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.marketing import learning

    return learning.summarize(wid)


@router.get("/marketing/lab/status")
def marketing_lab_status(
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """Consolidated readiness for the Marketing Lab GUI (no secrets)."""
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.marketing import lab

    return lab.lab_status(wid)


@router.get("/marketing/lab/log")
def marketing_lab_log_get(
    limit: int = 200,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.marketing import lab

    return {"entries": lab.read_lab_log(workspace_id=wid, limit=max(1, min(limit, 500))), "path": str(lab.lab_log_path(wid))}


@router.post("/marketing/lab/log")
def marketing_lab_log_post(
    body: dict | None = None,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """Append a client or server lab log entry (append-only JSONL)."""
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.marketing import lab

    payload = dict(body or {})
    if payload.get("clear"):
        return lab.clear_lab_log(workspace_id=wid)
    entry = lab.append_lab_log(payload, workspace_id=wid)
    return {"ok": True, "entry": entry}


@router.post("/marketing/lab/jobs")
def marketing_lab_jobs_enqueue(
    body: dict | None = None,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """Enqueue one or more marketing jobs for the lab harness."""
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    payload = body or {}
    from app.marketing import lab

    kinds = payload.get("kinds") or payload.get("kind")
    if isinstance(kinds, str):
        kinds = [kinds]
    if not isinstance(kinds, list) or not kinds:
        raise HTTPException(status_code=400, detail="kinds required (list or string)")
    return lab.enqueue_lab_jobs(
        workspace_id=wid,
        kinds=[str(k) for k in kinds],
        platform=payload.get("platform"),
        payload=payload.get("payload") if isinstance(payload.get("payload"), dict) else {
            k: v for k, v in payload.items() if k not in {"kinds", "kind", "platform", "payload"}
        },
    )


@router.post("/marketing/lab/content")
def marketing_lab_content_manual(
    body: dict | None = None,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """Schedule a manual post from founder text + optional publish/check jobs."""
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    payload = body or {}
    from app.marketing import lab

    result = lab.schedule_manual_content(
        workspace_id=wid,
        platform=str(payload.get("platform") or "linkedin"),
        body=str(payload.get("body") or payload.get("text") or ""),
        image_path=str(payload.get("image_path") or payload.get("image_url") or "") or None,
        also_publish_job=bool(payload.get("also_publish_job", True)),
        also_check_job=bool(payload.get("also_check_job", True)),
    )
    try:
        lab.append_lab_log(
            {
                "level": "success" if result.get("ok") else "error",
                "category": "blocked" if result.get("block_reason") else (
                    "success" if result.get("ok") else "error"
                ),
                "action": "lab.content.manual",
                "workspace_id": wid,
                "request": {
                    "platform": payload.get("platform"),
                    "body_len": len(str(payload.get("body") or payload.get("text") or "")),
                },
                "response": result,
                "error": result.get("error") or result.get("block_reason"),
            },
            workspace_id=wid,
        )
    except Exception:
        pass
    if not result.get("ok"):
        status = 400 if result.get("error") == "body_required" else 409
        raise HTTPException(status_code=status, detail=result)
    return result


@router.get("/marketing/lab/mail/status")
def marketing_lab_mail_status(
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """Gmail connection readiness + synced accounts (no secrets)."""
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.marketing import lab

    return lab.mail_status(wid)


@router.post("/marketing/lab/mail/connect")
def marketing_lab_mail_connect(
    body: dict | None = None,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """Start Composio Gmail OAuth — returns redirect_url for one-click Connect mail."""
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.marketing import lab

    try:
        return lab.start_mail_connect(wid)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail={
            "code": "gmail_connect_unavailable",
            "message": "Gmail could not be connected. Please try again.",
        }) from exc


@router.post("/marketing/lab/mail/test-send")
def marketing_lab_mail_test_send(
    body: dict | None = None,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """Send one test email via connected Gmail (no cold-mail prospect pipeline)."""
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    payload = body or {}
    from app.marketing import lab
    from app.connections.mailboxes import enabled

    result = lab.send_lab_test_email(
        workspace_id=wid,
        to=str(payload.get("to") or payload.get("recipient") or ""),
        subject=str(payload.get("subject") or ""),
        body=str(payload.get("body") or payload.get("text") or ""),
        confirm=bool(payload.get("confirm")),
        approval_id=str(payload.get("approval_id") or "") or None,
        **({"mailbox_id": payload.get("mailbox_id")} if enabled() else {}),
    )
    if not result.get("ok"):
        code = 400 if result.get("error") in {
            "invalid_to",
            "subject_required",
            "body_required",
            "confirm_required",
        } else 409
        raise HTTPException(status_code=code, detail=result)
    audit(
        "ops.marketing.lab.mail_test_send",
        workspace_id=wid,
        actor_id=LOCAL_PERSON_ID,
        actor_kind="person",
        surface="api",
        target_ref=str(payload.get("to") or "")[:120],
        outcome="ok",
        payload={
            "to": payload.get("to"),
            "subject": payload.get("subject"),
            "provider_message_id": result.get("provider_message_id"),
            "dry_run": result.get("dry_run"),
        },
    )
    return result


@router.post("/marketing/lab/linkedin/action")
def marketing_lab_linkedin_action(
    body: dict | None = None,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """Run a LinkedIn organic action (comment, repost, DM queue, heal, etc.)."""
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    payload = dict(body or {})
    from app.content import linkedin_actions
    from app.marketing import lab

    action = str(payload.get("action") or "").strip()
    if not action:
        raise HTTPException(status_code=400, detail={"error": "action_required"})
    from app.gtm.relationships.compliance import ComplianceError

    try:
        result = linkedin_actions.execute(
            workspace_id=wid,
            action=action,
            body=str(payload.get("body") or payload.get("text") or ""),
            target_urn=str(
                payload.get("target_urn")
                or payload.get("target")
                or payload.get("post_urn")
                or ""
            ),
            parent_comment_urn=str(payload.get("parent_comment_urn") or ""),
            url=str(payload.get("url") or ""),
            video_path=str(payload.get("video_path") or ""),
            video_url=str(payload.get("video_url") or ""),
            image_path=str(payload.get("image_path") or ""),
            title=str(payload.get("title") or ""),
            approval_id=str(payload.get("approval_id") or "") or None,
        )
    except ComplianceError as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "unipile_outreach_forbidden", "message": str(exc)},
        ) from exc
    try:
        lab.append_lab_log(
            {
                "level": "success" if result.get("ok") else "error",
                "category": (
                    "NeedsYou"
                    if result.get("needs_you")
                    else ("success" if result.get("ok") else "error")
                ),
                "action": f"lab.linkedin.{action}",
                "workspace_id": wid,
                "request": {
                    "action": action,
                    "has_target": bool(
                        payload.get("target_urn") or payload.get("target")
                    ),
                    "body_len": len(str(payload.get("body") or "")),
                },
                "response": {
                    k: result.get(k)
                    for k in (
                        "ok",
                        "mode",
                        "external_id",
                        "external_url",
                        "error",
                        "needs_you",
                        "backend",
                        "message",
                    )
                },
                "error": result.get("error") or result.get("message"),
            },
            workspace_id=wid,
        )
    except Exception:
        pass
    return result


@router.post("/marketing/ads/enable")
def marketing_ads_enable(
    body: dict | None = None,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """Enable a PAUSED ad — always spend_money NeedsYou unless approval present."""
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    payload = body or {}
    from app.marketing import lab

    result = lab.enqueue_ads_enable_job(
        workspace_id=wid,
        platform=str(payload.get("platform") or "meta"),
        resource_id=str(
            payload.get("resource_id")
            or payload.get("campaign_id")
            or payload.get("resource_name")
            or ""
        ),
        approval_id=str(payload.get("approval_id") or "") or None,
    )
    outcome = result.get("outcome") or result
    return {
        **(outcome if isinstance(outcome, dict) else {"ok": False}),
        "job_id": result.get("job_id"),
        "workspace_id": wid,
    }


def _ads_error(exc: Exception) -> HTTPException:
    from app.marketing.ads.service import AdsError

    if isinstance(exc, AdsError):
        return HTTPException(status_code=exc.status, detail={"code": exc.code, "message": str(exc)})
    return HTTPException(status_code=500, detail="ads_workflow_failed")


@router.get("/ads/readiness")
def ads_readiness(
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.marketing.adapters import ads_google, ads_meta

    return {
        "workspace_id": wid,
        "providers": {"google": ads_google.available(), "meta": ads_meta.available()},
        "creation": "Campaigns are created PAUSED after approval.",
        "activation": "Activation is not automated; it remains a spend_money action.",
    }


@router.get("/ads/proposals")
def ads_proposals(
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.marketing.ads import service

    return {"items": service.list_proposals(wid)}


@router.post("/ads/proposals", status_code=201)
def ads_proposal_create(
    body: AdProposalBody,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.marketing.ads import service

    try:
        return service.create_proposal(wid, body.model_dump())
    except Exception as exc:
        raise _ads_error(exc) from exc


@router.post("/ads/proposals/{proposal_id}/request-launch")
def ads_proposal_request_launch(
    proposal_id: str,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.marketing.ads import service

    try:
        return service.request_launch(wid, proposal_id)
    except Exception as exc:
        raise _ads_error(exc) from exc


@router.post("/ads/proposals/{proposal_id}/decide")
def ads_proposal_decide(
    proposal_id: str,
    body: AdDecisionBody,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    if body.decision not in {"approved", "rejected"}:
        raise HTTPException(status_code=422, detail="decision must be approved or rejected")
    from app.marketing.ads import service

    try:
        return service.decide_launch(
            wid,
            proposal_id,
            decision=body.decision,
            decided_by=resolve_ops_person(),
        )
    except Exception as exc:
        raise _ads_error(exc) from exc


@router.post("/ads/proposals/{proposal_id}/create-paused")
def ads_proposal_create_paused(
    proposal_id: str,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.marketing.ads import service

    try:
        return service.create_paused(wid, proposal_id)
    except Exception as exc:
        raise _ads_error(exc) from exc


@router.get("/ads/campaigns")
def ads_campaigns(
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.marketing.ads import service

    return {"items": service.list_campaigns(wid)}


@router.get("/marketing/plan")
def marketing_plan_get(
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.marketing import store as marketing_store

    plan = marketing_store.active_plan(wid)
    if plan is None:
        return {"plan": None}
    return {
        "plan": plan,
        "job_counts": marketing_store.job_counts(wid, plan_id=plan["id"]),
    }


@router.get("/marketing/jobs")
def marketing_jobs_list(
    status: str | None = None,
    limit: int = 80,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.marketing import store as marketing_store

    plan = marketing_store.active_plan(wid)
    return {
        "jobs": marketing_store.list_jobs(
            wid,
            plan_id=plan["id"] if plan else None,
            status=status,
            limit=max(1, min(limit, 200)),
        )
    }


@router.post("/marketing/tick")
async def marketing_tick_now(
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """Execution tick — run due marketing jobs with verification."""
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.marketing.executor import run_tick

    return await run_tick(workspace_id=wid)


@router.post("/marketing/jobs/{job_id}/resume")
def marketing_job_resume(
    job_id: str,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """NeedsYou cleared — re-queue a blocked job."""
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.marketing.executor import resume_blocked

    try:
        return {"ok": True, "job": resume_blocked(wid, job_id)}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ── Marketing media machine (doc 122) ───────────────────────────────────


@router.get("/marketing/media/capabilities")
def marketing_media_capabilities(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    _auth_workspace(x_api_key, None, None)
    from app.marketing.media.capabilities import capabilities

    from app.worker.handlers import hosted_mode
    return capabilities(hosted=hosted_mode() or bool(request.headers.get("X-COS-Assertion")))


@router.get("/marketing/assets")
def marketing_assets_list(
    kind: str | None = None,
    limit: int = 80,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.marketing.media import store as media_store

    return {"assets": media_store.list_assets(wid, kind=kind, limit=limit)}


@router.get("/marketing/assets/{asset_id}/file")
def marketing_asset_file(
    asset_id: str,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from pathlib import Path

    from app.marketing.media import store as media_store

    asset = media_store.get_asset(wid, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="asset_not_found")
    path = Path(asset["path"])
    root = media_store.MEDIA_ROOT.resolve()
    try:
        path.resolve().relative_to(root)
    except ValueError:
        raise HTTPException(status_code=404, detail="asset_path_rejected") from None
    if not path.is_file():
        raise HTTPException(status_code=404, detail="asset_missing_on_disk")
    mime = asset.get("mime") or "application/octet-stream"
    inline = mime in {"image/png", "image/jpeg", "image/webp", "image/gif"}
    return FileResponse(path, media_type=mime, filename=None if inline else path.name, headers={
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox; default-src 'none'",
    })


@router.post("/marketing/assets")
async def marketing_asset_upload(
    file: UploadFile = File(...),
    kind: str = Form("product"),
    title: str | None = Form(None),
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.marketing.media.service import MAX_UPLOAD_BYTES, ingest_upload

    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="file_too_large")
    result = ingest_upload(
        workspace_id=wid,
        kind=kind,
        filename=file.filename or "upload.bin",
        data=data,
        title=title,
    )
    return result


@router.post("/marketing/assets/{asset_id}/review")
def marketing_asset_review(
    asset_id: str,
    body: dict | None = None,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.marketing.media import store as media_store

    status = str((body or {}).get("status") or "approved")
    try:
        return {"ok": True, "asset": media_store.set_review(wid, asset_id, status)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/marketing/media/jobs")
def marketing_media_jobs(
    limit: int = 40,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.marketing import store as marketing_store
    from app.marketing.media.service import ensure_media_plan

    plan = ensure_media_plan(wid)
    return {
        "plan": plan,
        "jobs": marketing_store.list_jobs(wid, plan_id=plan["id"], limit=max(1, min(limit, 100))),
    }


@router.post("/marketing/media/image")
def marketing_media_image(
    body: dict | None = None,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.marketing.media.service import run_generate_image

    return run_generate_image(wid, body or {})


@router.post("/marketing/media/video")
def marketing_media_video(
    body: dict | None = None,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.marketing.media.service import run_generate_video

    return run_generate_video(wid, body or {})


@router.post("/marketing/media/demo")
def marketing_media_demo(
    body: dict | None = None,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """Record a product-demo walkthrough or ingest a Recordly export. Does not publish."""
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.marketing.media.service import run_record_product_demo

    payload = dict(body or {})
    payload.setdefault("engine", "product_demo")
    return run_record_product_demo(wid, payload)


@router.post("/marketing/media/browser")
def marketing_media_browser(
    body: dict | None = None,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.marketing.media.service import run_operate_browser

    return run_operate_browser(wid, body or {})


@router.post("/marketing/media/desktop")
def marketing_media_desktop(
    body: dict | None = None,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.marketing.media.service import run_operate_app

    return run_operate_app(wid, body or {})


@router.post("/marketing/media/ads")
def marketing_media_ads(
    body: dict | None = None,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.marketing.media.service import run_create_ad

    return run_create_ad(wid, body or {})


# ---------------------------------------------------------------------------
# Community Department (Loop 2) — /community/*
# ---------------------------------------------------------------------------


@router.get("/community/status")
def community_status(
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.community import service

    return service.status(wid)


@router.post("/community/thesis")
def community_thesis(
    body: dict | None = None,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.community import service

    return service.run_thesis(wid, body or {}, persist=True)


@router.post("/community/plan")
def community_plan(
    body: dict | None = None,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.community import service

    return service.create_plan(wid, body or {})


@router.post("/community/discord/connect")
def community_discord_connect(
    body: dict | None = None,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.community import service

    result = service.connect_discord(wid)
    if not result.get("ok") and result.get("needs_you"):
        raise HTTPException(status_code=409, detail=result)
    return result


@router.get("/community/discord/status")
def community_discord_status(
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.community import service

    return service.discord_status(wid)


@router.post("/community/discord/provision")
def community_discord_provision(
    body: dict | None = None,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    payload = body or {}
    from app.community import service

    result = service.provision_discord(
        wid,
        guild_id=str(payload.get("guild_id") or "").strip() or None,
        plan_id=str(payload.get("plan_id") or "").strip() or None,
        approval_id=str(payload.get("approval_id") or "").strip() or None,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result)
    return result


@router.post("/community/discord/action")
def community_discord_action(
    body: dict | None = None,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    payload = body or {}
    from app.community import service

    result = service.discord_action(
        wid,
        action=str(payload.get("action") or "status"),
        params=payload.get("params")
        if isinstance(payload.get("params"), dict)
        else {
            k: v
            for k, v in payload.items()
            if k not in {"action", "workspace_id"}
        },
    )
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result)
    return result


@router.post("/community/lab/log")
def community_lab_log(
    body: dict | None = None,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    _auth_workspace(x_api_key, None, None)
    from app.community import service

    entry = body or {}
    service.append_lab_log(entry if isinstance(entry, dict) else {"raw": entry})
    return {"ok": True}


# ---------------------------------------------------------------------------
# Connections hub — single place to connect / refresh all operators
# ---------------------------------------------------------------------------


class ConnectionsConnectBody(BaseModel):
    return_to: str | None = None
    intent: str | None = None
    mailbox_id: str | None = None


class GmailVerificationBody(BaseModel):
    venture_id: str | None = None
    mailbox_id: str | None = None


class GmailDefaultBody(BaseModel):
    mailbox_id: str | None
    venture_id: str | None = None


class GmailDisconnectBody(BaseModel):
    confirmed: bool = False
    expected_updated_at: str


def _require_multi_gmail() -> None:
    from app.connections.mailboxes import enabled
    if not enabled():
        raise HTTPException(404, detail={"code": "multi_gmail_disabled"})


@router.get("/connections/gmail/mailboxes")
def connections_gmail_mailboxes(
    venture_id: str | None = None,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    if venture_id is not None:
        from app.connections.verification import _validated_venture
        try:
            _validated_venture(wid, venture_id)
        except ValueError as exc:
            raise HTTPException(404, detail={"code": "venture_unavailable"}) from exc
    from app.connections.mailboxes import enabled
    if not enabled():
        return {"multi_gmail_v1": False, "workspace_id": wid, "mailboxes": []}
    from app.gtm.supymem.pipeline import list_mailboxes
    accounts = list_mailboxes(wid, include_usage=False)
    from app.connections.mailboxes import MailboxUnavailable, mailbox_defaults
    try:
        defaults = mailbox_defaults(wid, venture_id=venture_id)
    except MailboxUnavailable as exc:
        raise HTTPException(404, detail={"code": str(exc),
            "message": "Open your startup again, then return to Connected apps and refresh your mailboxes."}) from exc
    result = {"multi_gmail_v1": True, "workspace_id": wid, "mailboxes": accounts,
              **defaults}
    if venture_id is not None:
        override = defaults["venture_default_mailbox_id"]
        if override is not None:
            accounts = [{**account, "is_default": account["id"] == override} for account in accounts]
        result.update(venture_id=venture_id, venture_default_mailbox_id=override, mailboxes=accounts)
    return result


@router.post("/connections/gmail/mailboxes/default")
def connections_gmail_default(
    body: GmailDefaultBody,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    _require_multi_gmail()
    from app.connections.mailboxes import MailboxUnavailable, save_default_mailbox
    from app.content.oauth.attempts import authorize_actor, OAuthError
    try:
        authorize_actor(wid, resolve_ops_person())
        save_default_mailbox(wid, body.mailbox_id, venture_id=body.venture_id)
    except OAuthError as exc:
        raise HTTPException(403, detail={"code": exc.code}) from exc
    except MailboxUnavailable as exc:
        raise HTTPException(404, detail={"code": str(exc)}) from exc
    return {"ok": True, "mailbox_id": body.mailbox_id, "venture_id": body.venture_id}


@router.post("/connections/gmail/mailboxes/add")
def connections_gmail_add(
    body: ConnectionsConnectBody | None = None,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    _require_multi_gmail()
    from app.connections.hub import connect_provider
    return connect_provider(wid, "gmail", return_to=body.return_to if body else None, intent="add")


@router.delete("/connections/gmail/mailboxes/{mailbox_id}")
def connections_gmail_disconnect(
    mailbox_id: str,
    body: GmailDisconnectBody,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    _require_multi_gmail()
    from app.connections.hub import disconnect_mailbox
    from app.content.oauth.attempts import OAuthError
    try:
        disconnect_mailbox(wid, mailbox_id, confirmed=body.confirmed, expected_updated_at=body.expected_updated_at)
    except OAuthError as exc:
        raise HTTPException(403, detail={"code": exc.code}) from exc
    except ValueError as exc:
        raise HTTPException(409, detail={"code": str(exc)}) from exc
    return {"ok": True, "mailbox_id": mailbox_id, "status": "revoked"}


@router.post("/connections/gmail/mailboxes/{mailbox_id}/verify")
def connections_gmail_verify_mailbox(
    mailbox_id: str,
    body: GmailVerificationBody | None = None,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    _require_multi_gmail()
    return connections_gmail_verify(GmailVerificationBody(venture_id=body.venture_id if body else None, mailbox_id=mailbox_id),
                                    workspace_id, x_workspace_id, x_api_key)


@router.get("/connections/status")
def connections_hub_status(
    background_tasks: BackgroundTasks,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from datetime import datetime, timedelta

    from core.clock import as_utc, utcnow

    from app.connections.hub import hub_status, refresh_discord_status
    from app.core.local_mode import is_local

    status = hub_status(wid)
    if is_local():
        return status
    discord = status["connections"].get("discord", {})
    checked = discord.get("last_sync")
    if discord.get("connectable") and discord.get("connection_id") and (
            not checked or discord.get("status") == "pending" or
            utcnow() - as_utc(datetime.fromisoformat(checked)) >= timedelta(minutes=5)):
        background_tasks.add_task(refresh_discord_status, wid)
    return status


@router.post("/connections/{provider}/connect")
def connections_hub_connect(
    provider: str,
    body: ConnectionsConnectBody | None = None,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.connections.hub import connect_provider
    from app.connections.mailboxes import enabled

    result = connect_provider(
        wid,
        provider,
        return_to=body.return_to if body else None,
        **({"intent": body.intent if body else None, "mailbox_id": body.mailbox_id if body else None} if enabled() else {}),
    )
    # NeedsYou / Fix payloads are intentional operator guidance — return 200 so
    # the hub can render missing env vars. Unknown provider is the only 404.
    if result.get("error") == "unknown_provider":
        raise HTTPException(status_code=404, detail=result)
    return result


@router.post("/connections/{provider}/sync")
def connections_hub_sync(
    provider: str,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.connections.hub import sync_provider

    return sync_provider(wid, provider)


@router.get("/connections/gmail/status")
def gmail_reconnect_status(
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.supymem.pipeline import mail_readiness
    mail = mail_readiness(wid)
    result = {"needs_reconnect": mail["needs_reconnect"], "connected": mail["connected"]}
    from app.core.local_mode import is_local

    if is_local():
        result["next_step"] = ("You can return to your saved emails." if mail["connected"] else
            "Configure your mail in Settings, then try again.")
    return result


@router.post("/connections/gmail/verify")
def connections_gmail_verify(
    body: GmailVerificationBody | None = None,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """Run a read-only Gmail profile call for this workspace's saved account."""
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.connections.verification import verify

    try:
        from app.connections.mailboxes import enabled
        return verify(wid, actor_id=resolve_ops_person(), venture_id=body.venture_id if body else None,
                      **({"mailbox_id": body.mailbox_id if body else None} if enabled() else {}))
    except ValueError as exc:
        if str(exc) in {"venture_unavailable", "mailbox_unavailable"}:
            raise HTTPException(status_code=404, detail={"code": str(exc)}) from exc
        raise


@router.get("/connections/gmail/verification")
def connections_gmail_verification(
    venture_id: str | None = None,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    mailbox_id: str | None = None,
):
    """Restore only the latest read-only provider receipt for this workspace."""
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.connections.verification import latest

    try:
        from app.connections.mailboxes import enabled
        return latest(wid, venture_id=venture_id, **({"mailbox_id": mailbox_id} if enabled() else {}))
    except ValueError as exc:
        if str(exc) in {"venture_unavailable", "mailbox_unavailable"}:
            raise HTTPException(status_code=404, detail={"code": str(exc)}) from exc
        raise

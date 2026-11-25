"""Additive outreach routes for OT-39–OT-48. Same /api/v1 prefix as ops_api."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from app.api.ops_auth import OpsAuthRoute, require_ops_auth, require_workspace, resolve_ops_person

router = APIRouter(prefix="/api/v1", tags=["ops"], route_class=OpsAuthRoute)


def _auth_workspace(
    x_api_key: str | None,
    workspace_id: str | None,
    x_workspace_id: str | None,
) -> str:
    person = resolve_ops_person()
    ws = require_workspace(person, workspace_id, x_workspace_id)
    require_ops_auth(x_api_key, ws.tenancy)
    return ws.id


def _err(exc: Exception) -> HTTPException:
    status = getattr(exc, "status", 400)
    code = getattr(exc, "code", str(exc))
    payload = getattr(exc, "payload", None)
    if isinstance(payload, dict):
        return HTTPException(status_code=status, detail={"blocked": code, **payload})
    return HTTPException(status_code=status, detail=code)


class EvidenceBody(BaseModel):
    provider: str
    action: str


class EdgeBody(BaseModel):
    from_contact_id: str
    to_contact_id: str
    source: str
    domain: str | None = None


class SkuMintBody(BaseModel):
    period: str
    title: str
    body_md: str
    kind: str = "benchmark"
    sku_key: str | None = None
    match_tags: list[str] | None = None


class SkuApproveBody(BaseModel):
    reviewed: bool = False


class InterviewBody(BaseModel):
    contact_id: str
    notes_md: str
    kind: str = "first_meeting"
    mom_test_ok: bool = False
    next_step: str | None = None


class PilotCreateBody(BaseModel):
    contact_id: str
    use_case: str
    success_criterion: str
    team: str | None = None
    baseline: dict[str, Any] | None = None
    duration_days: int = 14


class PilotPatchBody(BaseModel):
    status: str | None = None
    permission_to_demo: bool | None = None
    paid_decision: bool | None = None
    success_criterion: str | None = None


class LayerADoc(BaseModel):
    kind: str = "job"
    title: str | None = None
    url: str | None = None
    evidence_url: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class LayerABody(BaseModel):
    documents: list[LayerADoc]
    shared: bool = False


@router.get("/outreach/evidence-sources")
def outreach_evidence_list(
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.relationships.evidence import list_evidence_sources

    return {"sources": list_evidence_sources(wid)}


@router.post("/outreach/evidence-sources")
def outreach_evidence_record(
    body: EvidenceBody,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.relationships.evidence import EvidenceError, record_evidence_source

    try:
        return record_evidence_source(wid, provider=body.provider, action=body.action)
    except EvidenceError as exc:
        raise _err(exc) from exc


@router.post("/outreach/network-edges")
def outreach_network_edge(
    body: EdgeBody,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.relationships.edges import NetworkEdgeError, record_edge

    try:
        return record_edge(
            wid,
            from_contact_id=body.from_contact_id,
            to_contact_id=body.to_contact_id,
            source=body.source,
            domain=body.domain,
        )
    except NetworkEdgeError as exc:
        raise _err(exc) from exc


@router.post("/outreach/skus/mint")
def outreach_sku_mint(
    body: SkuMintBody,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.relationships.assets import AssetFactoryError, mint_monthly_sku

    try:
        return mint_monthly_sku(
            wid,
            period=body.period,
            kind=body.kind,
            title=body.title,
            body_md=body.body_md,
            sku_key=body.sku_key,
            match_tags=body.match_tags,
        )
    except AssetFactoryError as exc:
        raise _err(exc) from exc


@router.post("/outreach/skus/{sku_key}/approve")
def outreach_sku_approve(
    sku_key: str,
    body: SkuApproveBody,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.relationships.assets import approve_sku
    from app.gtm.relationships.skus import SkuAttachError

    try:
        return approve_sku(wid, sku_key, reviewed=body.reviewed)
    except SkuAttachError as exc:
        raise _err(exc) from exc


@router.post("/outreach/interviews")
def outreach_interview_create(
    body: InterviewBody,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.relationships.interviews import InterviewError, capture_interview

    try:
        return capture_interview(
            wid,
            contact_id=body.contact_id,
            notes_md=body.notes_md,
            kind=body.kind,
            mom_test_ok=body.mom_test_ok,
            next_step=body.next_step,
        )
    except InterviewError as exc:
        raise _err(exc) from exc


@router.get("/outreach/interviews")
def outreach_interview_list(
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.relationships.interviews import list_interviews

    return {"items": list_interviews(wid)}


@router.get("/outreach/pilots")
def outreach_pilot_list(
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.relationships.pilots import list_pilots

    return {"items": list_pilots(wid)}


@router.post("/outreach/pilots")
def outreach_pilot_create(
    body: PilotCreateBody,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.relationships.pilots import PilotError, create_pilot

    try:
        return create_pilot(
            wid,
            contact_id=body.contact_id,
            use_case=body.use_case,
            success_criterion=body.success_criterion,
            team=body.team,
            baseline=body.baseline,
            duration_days=body.duration_days,
        )
    except PilotError as exc:
        raise _err(exc) from exc


@router.patch("/outreach/pilots/{pilot_id}")
def outreach_pilot_patch(
    pilot_id: str,
    body: PilotPatchBody,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.relationships.pilots import PilotError, patch_pilot

    try:
        return patch_pilot(wid, pilot_id, body.model_dump(exclude_none=True))
    except PilotError as exc:
        raise _err(exc) from exc


@router.post("/outreach/retention/null-pastes")
def outreach_retention_null(
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.relationships.retention import null_stale_paste_pii

    return null_stale_paste_pii(wid)


@router.post("/outreach/layer-a/ingest")
def outreach_layer_a_ingest(
    body: LayerABody,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    from app.gtm.relationships.layer_a import LayerAError, ingest_public_documents

    target = None if body.shared else wid
    try:
        return ingest_public_documents(
            target,
            [d.model_dump() for d in body.documents],
        )
    except LayerAError as exc:
        raise _err(exc) from exc

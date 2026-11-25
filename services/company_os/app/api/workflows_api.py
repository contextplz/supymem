"""Workflow routes (40 §8). Argument parsing plus a service call."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.authz.gate import Denied
from app.workflows import service
from app.workspaces.resolve import LOCAL_PERSON_ID

router = APIRouter(tags=["workflows"])


class CreateBody(BaseModel):
    name: str
    description: str = ""
    source: str = "described"
    trigger: dict = Field(default_factory=lambda: {"kind": "manual"})
    steps: list[dict] = Field(default_factory=list)
    shadow_mode: bool = True
    autonomy: str = "propose"
    owner_id: str | None = None


class RunBody(BaseModel):
    trigger_payload: dict = Field(default_factory=dict)
    idempotency_key: str | None = None
    shadow_mode: bool | None = None


class PauseBody(BaseModel):
    reason: str | None = None


def _fail(exc: service.WorkflowError) -> HTTPException:
    status = {
        "not_found": 404,
        "paused": 409,
        "retired": 409,
        "not_paused": 409,
        "too_many_steps": 422,
        "invalid_step": 422,
        "invalid_source": 422,
    }.get(exc.code, 422)
    return HTTPException(
        status, detail={"error": {"code": exc.code, "message": exc.message}}
    )


@router.post("/workspaces/{workspace_id}/workflows", status_code=201)
async def create_workflow(workspace_id: str, body: CreateBody):
    try:
        view = await service.create_workflow(
            workspace_id=workspace_id,
            name=body.name,
            description=body.description,
            trigger=body.trigger,
            steps=body.steps,
            source=body.source,
            shadow_mode=body.shadow_mode,
            autonomy=body.autonomy,
            owner_id=body.owner_id or LOCAL_PERSON_ID,
            actor_id=LOCAL_PERSON_ID,
            surface="api",
        )
    except Denied as exc:
        raise HTTPException(
            403, detail={"error": {"code": "denied", "message": str(exc)}}
        ) from exc
    except service.WorkflowError as exc:
        raise _fail(exc) from exc
    return view.to_dict()


@router.get("/workspaces/{workspace_id}/workflows")
def list_workflows(workspace_id: str):
    items = service.list_workflows(workspace_id)
    return {"items": [i.to_dict() for i in items]}


@router.get("/workflows/{workflow_id}")
def get_workflow(workflow_id: str):
    view = service.get_workflow(workflow_id)
    if view is None:
        raise HTTPException(
            404,
            detail={
                "error": {
                    "code": "not_found",
                    "message": "That workflow doesn't exist.",
                }
            },
        )
    return view.to_dict()


@router.post("/workflows/{workflow_id}/run")
async def run_workflow(workflow_id: str, body: RunBody | None = None):
    body = body or RunBody()
    try:
        run = await service.start_run(
            workflow_id,
            trigger_payload=body.trigger_payload,
            idempotency_key=body.idempotency_key,
            shadow_mode=body.shadow_mode,
            actor_id=LOCAL_PERSON_ID,
            surface="api",
        )
    except Denied as exc:
        raise HTTPException(
            403, detail={"error": {"code": "denied", "message": str(exc)}}
        ) from exc
    except service.WorkflowError as exc:
        raise _fail(exc) from exc
    return run.to_dict()


@router.post("/workflows/{workflow_id}/pause")
def pause_workflow(workflow_id: str, body: PauseBody | None = None):
    body = body or PauseBody()
    try:
        view = service.pause_workflow(
            workflow_id,
            reason=body.reason,
            actor_id=LOCAL_PERSON_ID,
            surface="api",
        )
    except service.WorkflowError as exc:
        raise _fail(exc) from exc
    return view.to_dict()


@router.post("/workflows/{workflow_id}/resume")
def resume_workflow(workflow_id: str):
    try:
        view = service.resume_workflow(
            workflow_id, actor_id=LOCAL_PERSON_ID, surface="api"
        )
    except service.WorkflowError as exc:
        raise _fail(exc) from exc
    return view.to_dict()


@router.get("/workflows/{workflow_id}/runs")
def list_runs(workflow_id: str):
    if service.get_workflow(workflow_id) is None:
        raise HTTPException(
            404,
            detail={
                "error": {
                    "code": "not_found",
                    "message": "That workflow doesn't exist.",
                }
            },
        )
    return {"items": [r.to_dict() for r in service.list_runs(workflow_id)]}

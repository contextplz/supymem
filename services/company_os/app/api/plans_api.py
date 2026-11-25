"""Signed founder resource intake, durable planning and review approval."""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.authz.workspace import assert_venture_access
from app.planning import compiler, resources

router = APIRouter(tags=["plans"])


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CompileBody(Body):
    mode: str = "full"
    reason: str | None = None
    revision_id: str | None = None
    base_plan_id: str | None = None
    instruction: str | None = Field(default=None, max_length=8000)


class RequestBody(Body):
    text: str = Field(min_length=1, max_length=8000)


class PatchBody(Body):
    edit_version: int = Field(ge=1)
    patch: dict


class ApproveBody(Body):
    review_token: str = Field(min_length=64, max_length=64)


class InventoryBody(Body):
    assets: list[dict] = Field(max_length=resources.MAX_ASSETS)
    complete: bool


class ApplyEditBody(Body):
    operation: str
    args: dict = Field(default_factory=dict)


def invoke(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except PermissionError as exc:
        raise HTTPException(403, detail={"error": {"code": "plan_approval_forbidden"}}) from exc
    except KeyError as exc:
        raise HTTPException(404, detail={"error": {"code": "not_found"}}) from exc
    except ValueError as exc:
        code = str(exc)
        if code == "plan_model_required":
            raise HTTPException(422, detail={"error": {"code": code, "message": "Open Keys and connections and save your model-provider key, then generate your plan again."}}) from None
        # Pydantic validation details may echo private plan input. Return only a
        # stable code for contract errors, never raw provider or founder payload.
        if len(code) > 100 or "\n" in code:
            code = "invalid_plan_input"
        conflict = code in {"confirmed_revision_required", "plan_research_required", "plan_resource_inventory_required",
            "plan_base_unavailable", "plan_not_editable", "plan_edit_conflict", "plan_inputs_changed", "plan_review_conflict", "plan_not_reviewable", "plan_version_superseded", "plan_claim_review_required"}
        raise HTTPException(409 if conflict else 422, detail={"error": {"code": code}}) from exc


@router.get("/ventures/{venture_id}/resources")
def get_resources(venture_id: str, x_subject: str | None = Header(default=None, alias="X-Subject")):
    _, wid = assert_venture_access(venture_id, x_subject)
    return invoke(resources.snapshot, wid, venture_id)


@router.put("/ventures/{venture_id}/resources")
def put_resources(venture_id: str, body: InventoryBody, x_subject: str | None = Header(default=None, alias="X-Subject")):
    _, wid = assert_venture_access(venture_id, x_subject)
    invoke(resources.update_inventory, wid, venture_id, body.assets, body.complete)
    return invoke(resources.snapshot, wid, venture_id)


@router.get("/ventures/{venture_id}/plans")
def plans(venture_id: str, x_subject: str | None = Header(default=None, alias="X-Subject")):
    assert_venture_access(venture_id, x_subject)
    return {"items": compiler.list_plans(venture_id)}


@router.get("/ventures/{venture_id}/plans/{plan_id}")
def plan(venture_id: str, plan_id: str, x_subject: str | None = Header(default=None, alias="X-Subject")):
    assert_venture_access(venture_id, x_subject)
    return invoke(compiler.get_plan, plan_id)


@router.post("/ventures/{venture_id}/plans/compile")
def compile_now(venture_id: str, body: CompileBody | None = None, x_subject: str | None = Header(default=None, alias="X-Subject")):
    _, wid = assert_venture_access(venture_id, x_subject)
    body = body or CompileBody()
    if body.mode != "full":
        raise HTTPException(422, detail={"error": {"code": "unsupported_plan_mode"}})
    return invoke(compiler.start, venture_id, wid, revision_id=body.revision_id,
                  base_plan_id=body.base_plan_id, instruction=body.instruction or body.reason)


@router.post("/ventures/{venture_id}/plans/estimate-generation")
def generation_estimate(venture_id: str, body: CompileBody | None = None, x_subject: str | None = Header(default=None, alias="X-Subject")):
    _, wid = assert_venture_access(venture_id, x_subject)
    body = body or CompileBody()
    return invoke(compiler.generation_quote, venture_id, wid, base_plan_id=body.base_plan_id, instruction=body.instruction or body.reason)


@router.patch("/ventures/{venture_id}/plans/{plan_id}")
def patch_plan(venture_id: str, plan_id: str, body: PatchBody, x_subject: str | None = Header(default=None, alias="X-Subject")):
    assert_venture_access(venture_id, x_subject)
    return invoke(compiler.patch_plan, plan_id, edit_version=body.edit_version, patch=body.patch)


@router.post("/ventures/{venture_id}/plans/{plan_id}/requests")
def plan_request(venture_id: str, plan_id: str, body: RequestBody, x_subject: str | None = Header(default=None, alias="X-Subject")):
    _, wid = assert_venture_access(venture_id, x_subject)
    return invoke(compiler.start, venture_id, wid, base_plan_id=plan_id, instruction=body.text)


@router.get("/ventures/{venture_id}/plans/{plan_id}/estimate")
def plan_estimate(venture_id: str, plan_id: str, x_subject: str | None = Header(default=None, alias="X-Subject")):
    assert_venture_access(venture_id, x_subject)
    data = invoke(compiler.get_plan, plan_id)
    return {"estimate": data["estimate"], "feasibility": data["feasibility"], "resources": data["resources"], "usage": data["usage"]}


@router.post("/ventures/{venture_id}/plans/{plan_id}/approve")
def plan_approve(venture_id: str, plan_id: str, body: ApproveBody, x_subject: str | None = Header(default=None, alias="X-Subject")):
    assert_venture_access(venture_id, x_subject)
    return invoke(compiler.approve_plan, plan_id, person_id=x_subject, review_token=body.review_token)


@router.post("/ventures/{venture_id}/plans/{plan_id}/edit-intent")
def plan_edit_intent(venture_id: str, plan_id: str, body: RequestBody, x_subject: str | None = Header(default=None, alias="X-Subject")):
    assert_venture_access(venture_id, x_subject)
    return invoke(compiler.edit_intent, body.text)


@router.post("/ventures/{venture_id}/plans/{plan_id}/apply-edit")
def plan_apply_edit(venture_id: str, plan_id: str, body: ApplyEditBody, x_subject: str | None = Header(default=None, alias="X-Subject")):
    assert_venture_access(venture_id, x_subject)
    return invoke(compiler.apply_edit, plan_id, body.operation, body.args)


@router.get("/capabilities")
def capabilities():
    # Workspace-specific availability belongs to the authenticated resource view.
    return {"items": [], "status": "workspace_required", "resource_path": "/ventures/{venture_id}/resources"}

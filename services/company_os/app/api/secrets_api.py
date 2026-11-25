"""Secret-slot HTTP surface. The value POST is UI-only and never echoed."""

from __future__ import annotations

from core.env_contract import EnvContractError, parse_app_env
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.secrets.service import (
    SecretSlotError,
    pending_requests,
    public_catalog,
    request_secret,
    resolve_slot_id,
    save_secret,
    slot_status,
)

def require_local_secret_slots() -> None:
    try:
        local = parse_app_env() == "local"
    except EnvContractError:
        local = False
    if not local:
        raise HTTPException(403, detail={"code": "local_secret_slots_only",
            "message": "Use workspace connections to save credentials."})


router = APIRouter(tags=["secret-slots"], dependencies=[Depends(require_local_secret_slots)])


class RequestBody(BaseModel):
    slot_id: str | None = None
    env_key: str | None = None
    provider: str | None = None
    venture_id: str | None = None
    workspace_id: str | None = None
    reason: str = ""


class SaveBody(BaseModel):
    values: dict[str, str] = Field(default_factory=dict)
    workspace_id: str | None = None

    model_config = {"extra": "forbid"}


def _fail(exc: SecretSlotError) -> HTTPException:
    return HTTPException(
        422 if exc.code != "unknown_slot" else 404,
        detail={"error": {"code": exc.code, "message": exc.message}},
    )


@router.get("/secret-slots")
def catalog():
    return {"items": public_catalog()}


@router.get("/secret-slots/pending")
def list_pending(venture_id: str | None = None, workspace_id: str | None = None):
    return {"items": pending_requests(venture_id=venture_id, workspace_id=workspace_id)}


@router.get("/secret-slots/{slot_id}/status")
def status(slot_id: str):
    try:
        return slot_status(slot_id)
    except SecretSlotError as exc:
        raise _fail(exc) from exc


@router.post("/secret-slots/request")
def open_request(body: RequestBody):
    try:
        slot_id = resolve_slot_id(body.slot_id, body.env_key, body.provider)
        return request_secret(
            slot_id,
            venture_id=body.venture_id,
            workspace_id=body.workspace_id,
            reason=body.reason,
            requested_by="engine",
        )
    except SecretSlotError as exc:
        raise _fail(exc) from exc


@router.post("/secret-slots/{slot_id}/value")
async def save(slot_id: str, body: SaveBody, request: Request):
    # Values stay on this request. Do not attach them to audit, logs, or the response.
    _ = request
    try:
        return save_secret(slot_id, body.values, workspace_id=body.workspace_id)
    except SecretSlotError as exc:
        raise _fail(exc) from exc

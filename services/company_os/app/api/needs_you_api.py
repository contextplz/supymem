"""Founder's "needs you" list, the Resolve/Continue call, and the Mac runtime's needs_you report."""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.authz.workspace import assert_venture_access
from app.needs_you import hooks, service
from app.build import device_invocations
from app.credits.errors import CreditError
from app.admin.authn import verify_caller

router = APIRouter(tags=["needs_you"])


class DeviceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str = Field(min_length=1, max_length=80)
    pause_kind: str = Field(min_length=1, max_length=40)  # captcha | login | permission | guardrail ...
    prompt: str = Field(default="", max_length=500)


@router.get("/ventures/{venture_id}/needs-you")
def list_needs_you(venture_id: str, x_subject: str | None = Header(default=None, alias="X-Subject")):
    assert_venture_access(venture_id, x_subject)
    return {"requests": service.list_open(venture_id), "reminders_paused": service.reminders_paused(venture_id)}


class RemindersBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paused: bool


@router.post("/ventures/{venture_id}/needs-you/reminders")
def set_needs_you_reminders(venture_id: str, body: RemindersBody, x_subject: str | None = Header(default=None, alias="X-Subject")):
    """Pause or resume the repeating blocker reminders (new blockers are still announced once)."""
    assert_venture_access(venture_id, x_subject)
    return {"reminders_paused": service.set_reminders_paused(venture_id, body.paused)}


@router.post("/ventures/{venture_id}/needs-you/{request_id}/resolve")
def resolve_needs_you(venture_id: str, request_id: str, x_subject: str | None = Header(default=None, alias="X-Subject")):
    assert_venture_access(venture_id, x_subject)
    from app.build.device_resume import ResumeUnavailable
    try:
        row = service.resolve(request_id, venture_id=venture_id, actor=x_subject)
    except ResumeUnavailable as exc:
        raise HTTPException(409, detail={"code": "device_resume_unavailable",
            "message": "Continue in the active Mac task, or review its recovery status before retrying. This request has not resumed execution."}) from exc
    if row is None:
        raise HTTPException(404, "not_found_or_already_resolved")
    return row


@router.post("/ventures/{venture_id}/needs-you/device", status_code=202)
def device_needs_you(venture_id: str, body: DeviceBody, request: Request):
    caller = verify_caller(request, required=True)
    try:
        # Reuse the same saved claim, reservation, review and duration_seconds
        # authority boundary as the native worker. A blocker is not an effect grant.
        device_invocations.check_current(venture_id, caller, body.order_id)
    except PermissionError as exc:
        raise HTTPException(403, "authenticated_device_required") from exc
    except KeyError as exc:
        raise HTTPException(404, "device_order_not_found") from exc
    except (ValueError, TypeError, CreditError) as exc:
        raise HTTPException(409, "device_order_not_active") from exc
    return {"id": hooks.from_device_event(venture_id, body.order_id, body.pause_kind, body.prompt)}

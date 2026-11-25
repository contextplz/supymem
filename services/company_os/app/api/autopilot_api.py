"""God mode (beta) settings and the Autopilot timeline for one venture."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.admin.deps import AdminContext, require_admin
from app.authz.roles import role_allows
from app.authz.workspace import assert_venture_access, ensure_subject_person, subject_person_id
from app.autopilot import brief, god_requests, pmf, service
from app.workspaces.service import role_of

router = APIRouter(tags=["autopilot"])


class AutopilotBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    god_mode: bool | None = None
    # Required when turning God mode on: the founder saw the beta warning and confirmed it.
    acknowledged_beta: bool = False
    daily_cap_credits: int | None = Field(default=None, ge=0, le=100_000)
    allow_send: bool | None = None
    send_max_per_day: int | None = Field(default=None, ge=0)
    confirmed_send_max: bool = False
    allow_mac: bool | None = None
    continue_when_offline: bool | None = None


class OutreachLogBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    who: str = Field(min_length=1, max_length=200)  # email, handle or name of the person
    direction: Literal["out", "in"]  # out: I sent it. in: they replied.
    channel: str = Field(default="email", max_length=40)
    subject: str = Field(default="", max_length=300)
    note: str = Field(default="", max_length=2000)


@router.post("/ventures/{venture_id}/outreach-log", status_code=201)
def log_outreach(venture_id: str, body: OutreachLogBody,
                 x_subject: str | None = Header(default=None, alias="X-Subject")):
    """The founder sent a message by hand, or got a reply. Without this the PMF loop is blind to both."""
    _, ws = assert_venture_access(venture_id, x_subject)
    touch = pmf.log_touch(venture_id, ws, body.who, body.direction, channel=body.channel, subject=body.subject,
                          note=body.note, logged_by=x_subject)
    return {"id": touch, "sent": pmf.read_metric(venture_id, "outbound_touches"),
            "replies": pmf.read_metric(venture_id, "outbound_replies")}


@router.get("/ventures/{venture_id}/autopilot")
def get_autopilot(venture_id: str, x_subject: str | None = Header(default=None, alias="X-Subject")):
    assert_venture_access(venture_id, x_subject)
    from app.needs_you.service import list_open

    timeline = service.timeline(venture_id)
    observed = service.status(venture_id)
    return {**service.public_settings(venture_id), "status": observed, "pmf_journal": pmf.journal(venture_id),
            "timeline": timeline, "needs_you": list_open(venture_id), "brief": brief.brief(venture_id, timeline, observed)}


@router.put("/ventures/{venture_id}/autopilot")
def put_autopilot(venture_id: str, body: AutopilotBody,
                  x_subject: str | None = Header(default=None, alias="X-Subject")):
    _, workspace_id = assert_venture_access(venture_id, x_subject)
    if not role_allows(role_of(workspace_id, subject_person_id(x_subject)), "change_autonomy"):
        raise HTTPException(403, detail={"code": "workspace_admin_required"})
    actor = subject_person_id(x_subject)
    if body.god_mode and not god_requests.beta_gate_open(venture_id):
        from app.core.local_mode import is_local
        from app.core.local_capabilities import blocked_message
        if is_local():
            raise HTTPException(403, detail={"error": {"code": "background_work_disabled", "message": blocked_message("background_work")}})
        raise HTTPException(403, detail={"error": {"code": "god_mode_early_access",
                                                   "message": "God mode is in early access. Request access and we will enable it for you."}})
    if body.god_mode and not body.acknowledged_beta:
        raise HTTPException(409, detail={"error": {"code": "god_mode_warning_unconfirmed", "message": service.WARNING}})
    try:
        out = service.update_settings(venture_id, god_mode=body.god_mode, daily_cap_credits=body.daily_cap_credits,
                                      allow_send=body.allow_send, send_max_per_day=body.send_max_per_day,
                                      allow_mac=body.allow_mac, continue_when_offline=body.continue_when_offline,
                                      confirmed_send_max=body.confirmed_send_max, actor_subject=actor)
    except KeyError:
        raise HTTPException(404, "not_found") from None
    except PermissionError as exc:
        raise HTTPException(403, detail={"code": str(exc)}) from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    timeline = service.timeline(venture_id)
    observed = service.status(venture_id)
    return {**out, "status": observed, "pmf_journal": pmf.journal(venture_id), "timeline": timeline,
            "brief": brief.brief(venture_id, timeline, observed)}


class GodModeRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str = Field(default="", max_length=god_requests.NOTE_MAX)


@router.get("/ventures/{venture_id}/god-mode/requests/me")
def my_god_mode_request(venture_id: str, viewed: bool = False,
                        x_subject: str | None = Header(default=None, alias="X-Subject")):
    _, ws = assert_venture_access(venture_id, x_subject)
    return god_requests.my_status(venture_id, ensure_subject_person(x_subject), ws, viewed=viewed)


@router.post("/ventures/{venture_id}/god-mode/requests")
def request_god_mode(venture_id: str, body: GodModeRequestBody,
                     x_subject: str | None = Header(default=None, alias="X-Subject")):
    """Interest capture only: records the request, never enables God mode."""
    _, ws = assert_venture_access(venture_id, x_subject)
    return god_requests.request_access(venture_id, ws, ensure_subject_person(x_subject), body.note)


@router.get("/admin/god-mode/requests")
def admin_god_mode_requests(status: str | None = None, ctx: AdminContext = Depends(require_admin("users.view"))):
    return god_requests.admin_list(status)


@router.post("/admin/god-mode/requests/{request_id}/approve")
def admin_approve_god_mode(request_id: str, ctx: AdminContext = Depends(require_admin("policies.write"))):
    out = god_requests.approve(request_id, ctx.admin.id or f"bootstrap:{ctx.caller.sub}")
    if out is None:
        raise HTTPException(404, "not_found")
    return out

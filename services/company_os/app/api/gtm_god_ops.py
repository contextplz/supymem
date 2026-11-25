"""GTM God HTTP surface. Same /api/v1 prefix as ops."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_core import PydanticCustomError

from app.api.ops_auth import OpsAuthRoute, require_ops_auth, require_workspace, resolve_ops_person
from app.gtm.god.service import get_status
from app.gtm.god import generation
from app.gtm.god.generation_schema import Request

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


class StartBody(BaseModel):
    idea: str = Field(min_length=1, max_length=12000)
    problem: str = Field(default="", max_length=12000)
    who: str = Field(default="", max_length=2000)
    users_count: int = Field(default=0, ge=0)
    venture_id: str | None = None


class HumanBody(BaseModel):
    note: str = Field(default="", max_length=12000)


@router.get("/gtm/god")
def god_status(
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    return get_status(wid)


@router.post("/gtm/god/start")
def god_start(
    body: StartBody,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    raise HTTPException(409, detail={"code": "gtm_quote_required"})


@router.post("/gtm/god/tick")
def god_tick(
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    raise HTTPException(409, detail={"code": "gtm_quote_required"})


@router.post("/gtm/god/tasks/{task_id}/complete")
def god_complete(
    task_id: str,
    body: HumanBody,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    raise HTTPException(409, detail={"code": "gtm_quote_required"})


class RunBody(Request):
    input_digest: str = Field(pattern="^[a-f0-9]{64}$")
    resource_digest: str = Field(pattern="^[a-f0-9]{64}$")
    ceiling_micro: int = Field(ge=0)


def invoke(fn, *args, **kwargs):
    from pydantic import ValidationError
    from app.gtm.contact_work import ContactWorkError
    from app.gtm.relationships.service import OutreachError
    try:
        return fn(*args, **kwargs)
    except ContactWorkError as exc:
        raise HTTPException(404 if exc.code == "gtm_not_found" else 422,
            detail={"code": exc.code, "message": str(exc)}) from None
    except OutreachError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code}) from None
    except ValueError as exc:
        code = "gtm_invalid_input" if isinstance(exc, ValidationError) else str(exc)
        if code == "gtm_provider_unavailable":
            raise HTTPException(422, detail={"code": code, "message": "Open Keys and connections and save your model-provider key, then run your diagnosis again."}) from None
        if not code.startswith("gtm_") or len(code) > 100:
            code = "gtm_invalid_input"
        status = 422
        if code in {"gtm_quote_changed", "gtm_idempotency_conflict"}:
            status = 409
        elif code == "gtm_insufficient_credits":
            status = 402
        elif code in {"gtm_actor_required", "gtm_write_forbidden"}:
            status = 403
        elif code in {"gtm_scope_mismatch", "gtm_not_found", "gtm_venture_unavailable"}:
            status = 404
        raise HTTPException(status, detail={"code": code}) from None


@router.post("/ventures/{venture_id}/gtm/god/quotes")
def diagnosis_quote(venture_id: str, body: Request,
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key")):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    return invoke(generation.quote, wid, venture_id, body.model_dump())


@router.post("/ventures/{venture_id}/gtm/god/runs", status_code=202)
def diagnosis_start(venture_id: str, body: RunBody,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=160),
    workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key")):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    accepted = {key: getattr(body, key) for key in ("input_digest", "resource_digest", "ceiling_micro")}
    return invoke(generation.start, wid, venture_id, body.model_dump(exclude=set(accepted)),
        idempotency_key=idempotency_key, **accepted)


@router.get("/ventures/{venture_id}/gtm/god/runs")
def diagnosis_runs(venture_id: str, workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key")):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    return invoke(generation.recent, wid, venture_id)


@router.get("/ventures/{venture_id}/gtm/god/runs/{run_id}")
def diagnosis_run(venture_id: str, run_id: str, workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key")):
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    return invoke(generation.get, wid, venture_id, run_id)


class JourneyPrepareBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    retry: bool = False
    note: str | None = Field(default=None, max_length=2000)
    recruitment_opt_in: bool = False


class JourneyIcpBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    who: str = Field(min_length=8, max_length=600)
    problem: str | None = Field(default=None, min_length=1, max_length=2000)
    search_queries: list[Annotated[str, Field(min_length=1, max_length=256)]] | None = Field(default=None, min_length=1, max_length=18)
    note: str | None = Field(default=None, max_length=2000)
    recruitment_opt_in: bool = False


class JourneyProspectPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    country: str = Field(min_length=2, max_length=2, pattern="^[A-Z]{2}$")
    lawful_basis: Literal["none", "consent", "legitimate_interest"] = "none"
    lawful_basis_note: str = Field(default="", max_length=1000)


class JourneyProspectUpdate(BaseModel):
    """Confirm country/basis and/or skip. Unsupported countries are rejected by the journey with a plain code."""
    model_config = ConfigDict(extra="forbid")
    country: str | None = Field(default=None, min_length=2, max_length=2, pattern="^[A-Za-z]{2}$")
    lawful_basis: Literal["none", "consent", "legitimate_interest"] = "none"
    lawful_basis_note: str = Field(default="", max_length=1000)
    skip: bool | None = None


class JourneyProspectConfirmItem(JourneyProspectUpdate):
    contact_id: str = Field(min_length=1, max_length=100)


class JourneyProspectConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[JourneyProspectConfirmItem] = Field(min_length=1, max_length=50)


class JourneyProspectBody(JourneyProspectPatch):
    name: str = Field(min_length=1, max_length=150)
    email: str = Field(min_length=3, max_length=254)
    company: str = Field(default="", max_length=150)
    source_url: str = Field(min_length=8, max_length=2048)


class JourneyDraftBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: str = Field(min_length=64, max_length=64, pattern="^[a-f0-9]{64}$")
    decision: Literal["draft", "approved", "skipped"] = "draft"
    subject: str | None = Field(default=None, min_length=1, max_length=120)
    body: str | None = Field(default=None, min_length=1, max_length=4000)
    mailbox_id: str | None = Field(default=None, min_length=1)

    @field_validator("mailbox_id", mode="before")
    @classmethod
    def validate_mailbox_id(cls, value):
        from app.connections.mailboxes import enabled
        if not enabled():
            raise PydanticCustomError("extra_forbidden", "Extra inputs are not permitted")
        return value


class JourneySendBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    batch_id: str = Field(min_length=1, max_length=100)
    draft_ids: list[str] = Field(min_length=1, max_length=5)
    approval_digest: str = Field(pattern="^[a-f0-9]{64}$")
    cap: int = Field(ge=1, le=5)
    self_only: bool = False


class JourneySettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allow_sending: bool | None = None
    auto_approve_first_emails: bool | None = None
    followup_days: list[int] | None = Field(default=None, max_length=4)
    sender_postal_address: str | None = Field(default=None, max_length=500)
    booking_link: str | None = Field(default=None, max_length=300)
    aup_accepted: bool = False
    dpa_accepted: bool = False


class JourneyContactBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str | None = Field(default=None, max_length=8000)
    outcome: Literal["interested", "not_now", "not_a_fit", "opted_out"] | None = None


class JourneyResumeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reviewed_bounced_addresses: Literal[True]


def _journey_call(fn, *args, **kwargs):
    from app.approvals.external import OutboundExecutionError
    from app.approvals.service import ApprovalError
    try:
        result = invoke(fn, *args, **kwargs)
        from app.connections.mailboxes import enabled
        if enabled() and isinstance(result, dict) and isinstance(result.get("gmail"), dict):
            from app.gtm.supymem.pipeline import list_mailboxes
            result = {**result, "gmail": {**result["gmail"], "multi_gmail_v1": True, "mailboxes": list_mailboxes(args[0])}}
        return result
    except (OutboundExecutionError, ApprovalError) as exc:
        raise HTTPException(409, detail={"code": exc.code}) from None


@router.get("/ventures/{venture_id}/gtm/journey")
def journey_status(venture_id: str, workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key")):
    from app.gtm import journey
    return _journey_call(journey.status, _auth_workspace(x_api_key, workspace_id, x_workspace_id), venture_id)


@router.post("/ventures/{venture_id}/gtm/journey/prepare", status_code=202)
def journey_prepare(venture_id: str, body: JourneyPrepareBody, workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key")):
    from app.gtm import journey
    return _journey_call(journey.prepare, _auth_workspace(x_api_key, workspace_id, x_workspace_id), venture_id, retry=body.retry, note=body.note, recruitment_opt_in=body.recruitment_opt_in)


@router.patch("/ventures/{venture_id}/gtm/journey/icp")
def journey_icp(venture_id: str, body: JourneyIcpBody, workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key")):
    from app.gtm import journey
    return _journey_call(journey.update_icp, _auth_workspace(x_api_key, workspace_id, x_workspace_id), venture_id, body.who, search_queries=body.search_queries, note=body.note, recruitment_opt_in=body.recruitment_opt_in, problem=body.problem)


@router.post("/ventures/{venture_id}/gtm/journey/prospects")
def journey_add_prospect(venture_id: str, body: JourneyProspectBody, workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key")):
    from app.gtm import journey
    return _journey_call(journey.add_prospect, _auth_workspace(x_api_key, workspace_id, x_workspace_id), venture_id, body.model_dump())


@router.patch("/ventures/{venture_id}/gtm/journey/prospects/{contact_id}")
def journey_patch_prospect(venture_id: str, contact_id: str, body: JourneyProspectUpdate, workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key")):
    from app.gtm import journey
    return _journey_call(journey.patch_prospect, _auth_workspace(x_api_key, workspace_id, x_workspace_id), venture_id, contact_id, body.model_dump())


@router.post("/ventures/{venture_id}/gtm/journey/prospects/{contact_id}/{action}")
def journey_contact_action(venture_id: str, contact_id: str, action: Literal["reply", "reply-draft", "brief", "call-notes", "sent", "call-booked"], body: JourneyContactBody, workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key")):
    from app.gtm import journey
    return _journey_call(journey.contact_action, _auth_workspace(x_api_key, workspace_id, x_workspace_id), venture_id, contact_id, action, body.model_dump())


@router.post("/ventures/{venture_id}/gtm/journey/ask-sending")
def journey_ask_sending(venture_id: str, workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key")):
    from app.gtm import journey
    return _journey_call(journey.ask_sending, _auth_workspace(x_api_key, workspace_id, x_workspace_id), venture_id)


@router.post("/ventures/{venture_id}/gtm/journey/resume-sending")
def journey_resume_sending(venture_id: str, body: JourneyResumeBody, workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key")):
    from app.gtm import journey
    return _journey_call(journey.resume_sending, _auth_workspace(x_api_key, workspace_id, x_workspace_id), venture_id,
        reviewed_bounced_addresses=body.reviewed_bounced_addresses)


@router.post("/ventures/{venture_id}/gtm/journey/prospects/confirm")
def journey_confirm_prospects(venture_id: str, body: JourneyProspectConfirm, workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key")):
    from app.gtm import journey
    return _journey_call(journey.confirm_prospects, _auth_workspace(x_api_key, workspace_id, x_workspace_id), venture_id, body.model_dump()["items"])


@router.patch("/ventures/{venture_id}/gtm/journey/drafts/{draft_id}")
def journey_patch_draft(venture_id: str, draft_id: str, body: JourneyDraftBody, workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key")):
    from app.gtm import journey
    return _journey_call(journey.patch_draft, _auth_workspace(x_api_key, workspace_id, x_workspace_id), venture_id, draft_id, body.model_dump(exclude_none=True))


@router.patch("/ventures/{venture_id}/gtm/journey/settings")
def journey_settings(venture_id: str, body: JourneySettingsBody, workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key")):
    from app.gtm import journey
    return _journey_call(journey.update_settings, _auth_workspace(x_api_key, workspace_id, x_workspace_id), venture_id, body.model_dump(exclude_unset=True))


@router.post("/ventures/{venture_id}/gtm/journey/send")
async def journey_send(venture_id: str, body: JourneySendBody, workspace_id: str | None = None,
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key")):
    from app.gtm import journey
    from app.approvals.external import OutboundExecutionError
    from app.approvals.service import ApprovalError
    wid = _auth_workspace(x_api_key, workspace_id, x_workspace_id)
    try:
        return await journey.send(wid, venture_id, body.model_dump())
    except (ValueError, OutboundExecutionError, ApprovalError) as exc:
        code = str(exc) if isinstance(exc, ValueError) else exc.code
        from app.chat.customers import send_block_message
        message = send_block_message(code)
        if not code.startswith(("gtm_", "outbound_")) and code not in {"suppressed_or_replied", "email_evidence_required"}:
            code = "gtm_send_blocked"
        raise HTTPException(409, detail={"code": code, "message": message}) from None

"""Signed-founder, opt-in outreach rehearsal; no customer evidence writes."""
from fastapi import APIRouter, HTTPException, Request
from pydantic import Field

from app.admin.authn import verify_caller
from app.authz.workspace import subject_person_id
from app.simulation import service
from app.simulation.contracts import DraftRef, StrictModel

router = APIRouter(prefix="/api/v1", tags=["simulation"])


class QuoteBody(StrictModel):
    drafts: list[DraftRef] = Field(min_length=1, max_length=3)


class StartBody(QuoteBody):
    epoch: int
    quote_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    confirmed: bool
    request_id: str = Field(min_length=1, max_length=100)


def actor(request):
    caller = verify_caller(request, required=True)
    if caller.role != "user" or caller.legacy or not caller.sub:
        raise HTTPException(403, detail={"code": "simulation_founder_required"})
    return subject_person_id(caller.sub)


def invoke(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except KeyError as exc:
        raise HTTPException(404, detail={"code": "simulation_not_found"}) from exc
    except PermissionError as exc:
        raise HTTPException(403, detail={"code": "simulation_access_denied"}) from exc
    except ValueError as exc:
        # Only code-owned errors cross the API; parser/input details stay private.
        code = str(exc) if str(exc).startswith("simulation_") and len(str(exc)) < 100 else "simulation_invalid_input"
        raise HTTPException(409, detail={"code": code}) from exc


@router.post("/ventures/{venture_id}/simulation/rehearse/quote")
def quote(venture_id: str, workspace_id: str, body: QuoteBody, request: Request):
    return invoke(service.quote, workspace_id, venture_id, actor(request), [x.model_dump() for x in body.drafts])


@router.post("/ventures/{venture_id}/simulation/rehearse", status_code=202)
def start(venture_id: str, workspace_id: str, body: StartBody, request: Request):
    return invoke(service.start, workspace_id, venture_id, actor(request), [x.model_dump() for x in body.drafts],
        **body.model_dump(exclude={"drafts"}))


@router.get("/ventures/{venture_id}/simulation/rehearse/{job_id}")
def status(venture_id: str, workspace_id: str, job_id: str, request: Request):
    return invoke(service.status, workspace_id, venture_id, actor(request), job_id)

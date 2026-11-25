"""Additive, default-off signed human QA/review boundary for server candidates."""
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.admin.authn import verify_caller
from app.builder import execution
from app.builder.model_proxy import BuildModelError
from app.hosting.service import HostingError, queue_accepted_build
from app.credits.errors import CreditError
from app.state import db
from app.db.supymem_models import BuildRun
from . import owns
from . import admission, inputs

router = APIRouter(prefix="/ventures/{venture_id}/pagebuilder-admission", tags=["pagebuilder"])


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    job_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    variant: int = Field(ge=0, le=2)
    plan_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,128}$")
    plan_task_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,128}$")

    @model_validator(mode="after")
    def plan_pair(self):
        if (self.plan_id is None) != (self.plan_task_id is None):
            raise ValueError("plan_and_task_required_together")
        return self


class Admission(Selection):
    quote_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    quote_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    approve_qa_ceiling_micro: int = Field(gt=0)
    confirmed: Literal[True]

    @field_validator("confirmed", mode="before")
    @classmethod
    def actual_confirmation(cls, value):
        if value is not True:
            raise ValueError("founder_confirmation_required")
        return value


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_artifact_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    report_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    decision: Literal["accept", "reject"]
    feedback: str | None = Field(default=None, max_length=12000)
    confirmed: Literal[True]
    actual_confirmation = field_validator("confirmed", mode="before")(Admission.actual_confirmation.__func__)


class Publish(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    confirmed: Literal[True]
    actual_confirmation = field_validator("confirmed", mode="before")(Admission.actual_confirmation.__func__)


class PublishSelection(Selection):
    confirmed: Literal[True]
    actual_confirmation = field_validator("confirmed", mode="before")(Admission.actual_confirmation.__func__)


class PublicationRetry(Publish):
    expected_attempt: int = Field(ge=0)


def caller(request):
    from app.api.voice_api import reject_voice_confirm
    reject_voice_confirm(request.headers.get("X-SupyMem-Source"))
    return verify_caller(request, required=True)


def invoke(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except KeyError as exc:
        raise HTTPException(404, detail={"code": "pagebuilder_admission_not_found"}) from exc
    except PermissionError as exc:
        raise HTTPException(403, detail={"code": "pagebuilder_admission_forbidden"}) from exc
    except BuildModelError as exc:
        raise HTTPException(409, detail={"code": str(exc)}) from exc
    except execution.BuildError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code}) from exc
    except HostingError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code}) from exc
    except CreditError as exc:
        raise HTTPException(409, detail={"code": exc.code}) from exc
    except ValueError as exc:
        raise HTTPException(409, detail={"code": "pagebuilder_admission_inputs_changed"}) from exc


def owned(venture_id, person, run_id):
    _wid, actor = inputs.authority(venture_id, person)
    with db()() as session:
        run = session.get(BuildRun, run_id)
        import json
        spec = json.loads(run.spec) if run else {}
        if not run or not owns(run, spec) or run.venture_id != venture_id or spec["actor"] != actor:
            raise KeyError("pagebuilder_admission_not_found")
        execution._app(session, run.app_id, actor, mutate=True)
        return actor, run.app_id


@router.post("/quote")
def quote(venture_id: str, body: Selection, request: Request):
    return invoke(admission.estimate, venture_id, caller(request), body.model_dump())


@router.post("/admit")
def admit(venture_id: str, body: Admission, request: Request):
    return invoke(admission.admit, venture_id, caller(request), body.model_dump(include=set(Selection.model_fields)),
        **body.model_dump(include={"quote_id", "quote_digest", "approve_qa_ceiling_micro"}))


@router.post("/publish", status_code=202)
def publish_candidate(venture_id: str, body: PublishSelection, request: Request):
    from . import publication
    return invoke(publication.request, venture_id, caller(request), body.model_dump(include=set(Selection.model_fields)))


@router.get("/publish/{run_id}")
def publication_status(venture_id: str, run_id: str, request: Request):
    from . import publication
    return invoke(publication.status, venture_id, caller(request), run_id)


@router.post("/publish/{run_id}/retry", status_code=202)
def retry_publication(venture_id: str, run_id: str, body: PublicationRetry, request: Request):
    from . import publication
    return invoke(publication.retry, venture_id, caller(request), run_id, expected_attempt=body.expected_attempt)


@router.get("/{run_id}")
def status(venture_id: str, run_id: str, request: Request):
    actor, _app = invoke(owned, venture_id, caller(request), run_id)
    return invoke(execution.read, run_id, actor=actor)


@router.post("/{run_id}/review")
def review(venture_id: str, run_id: str, body: Review, request: Request):
    actor, _app = invoke(owned, venture_id, caller(request), run_id)
    return invoke(execution.review, run_id, actor=actor, **body.model_dump(exclude={"confirmed"}))


@router.post("/{run_id}/publish")
def publish(venture_id: str, run_id: str, body: Publish, request: Request):
    actor, app = invoke(owned, venture_id, caller(request), run_id)
    return invoke(queue_accepted_build, app, run_id, actor=actor)

"""Founder-scoped review actions; persistence and provider calls live in the service."""
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.authz.workspace import assert_venture_access
from app.gtm.god.generation import authority
from app.ventures import startup_review

router = APIRouter(tags=["venture journey"])


def scope(venture_id: str, x_subject: str = Header(alias="X-Subject")):
    _, workspace_id = assert_venture_access(venture_id, x_subject)
    try:
        authority(workspace_id, venture_id, write=True, actor=x_subject)
    except ValueError:
        raise HTTPException(403, "This action is unavailable for this venture.") from None
    return x_subject


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: str = Field(min_length=64, max_length=64)
    field: str = Field(min_length=1, max_length=40)
    value: str = Field(min_length=1, max_length=2000)


class Back(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: str = Field(min_length=64, max_length=64)
    field: str = Field(min_length=1, max_length=40)


class Priorities(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: str = Field(min_length=64, max_length=64)
    text: str = Field(max_length=2000)


def call(fn, *args):
    try:
        return fn(*args)
    except KeyError:
        raise HTTPException(404, "This step is only part of new ventures' setup. Open your saved idea to continue.") from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@router.get("/ventures/{venture_id}/context")
def get(venture_id: str, actor=Depends(scope)):
    return call(startup_review.get, venture_id)


@router.post("/ventures/{venture_id}/context/answer")
def answer(venture_id: str, body: Answer, actor=Depends(scope)):
    return call(startup_review.save_answer, venture_id, body.revision, body.field, body.value, actor)


@router.post("/ventures/{venture_id}/context/back")
def back(venture_id: str, body: Back, actor=Depends(scope)):
    return call(startup_review.go_back, venture_id, body.revision, body.field)


@router.post("/ventures/{venture_id}/context/skip")
def skip(venture_id: str, body: Back, actor=Depends(scope)):
    return call(startup_review.skip_question, venture_id, body.revision, body.field)


@router.post("/ventures/{venture_id}/context/priorities")
async def priorities(venture_id: str, body: Priorities, actor=Depends(scope)):
    try:
        return await startup_review.save_priorities(venture_id, body.revision, body.text, actor)
    except KeyError:
        raise HTTPException(404, "This step is only part of new ventures' setup. Open your saved idea to continue.") from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None

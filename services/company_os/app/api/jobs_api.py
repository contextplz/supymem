"""Read durable founder job state without exposing internal payloads or secrets."""
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.core import job_status
from app.core.job_status import _view

router = APIRouter(tags=["jobs"])


class StopChatBody(BaseModel):
    conversation_id: str | None = Field(default=None, max_length=128)
    nonce: str | None = Field(default=None, min_length=1, max_length=128)
    job_ids: list[str] = Field(default_factory=list, max_length=100)


@router.get("/ventures/{venture_id}/chat/work")
def chat_work(venture_id: str, conversation_id: str | None = Query(None, max_length=128)):
    try:
        return job_status.chat_work(venture_id, conversation_id)
    except ValueError as exc:
        raise HTTPException(404, detail={"code": "chat_conversation_not_found"}) from exc


@router.post("/ventures/{venture_id}/chat/stop")
def stop_chat(venture_id: str, body: StopChatBody):
    try:
        return job_status.chat_work(venture_id, body.conversation_id, stop=True, nonce=body.nonce, job_ids=body.job_ids)
    except PermissionError as exc:
        raise HTTPException(403, detail={"code": "request_work_required"}) from exc
    except ValueError as exc:
        raise HTTPException(404, detail={"code": "chat_conversation_not_found"}) from exc


@router.get("/workspaces/{workspace_id}/jobs")
def list_jobs(workspace_id: str, limit: int = Query(50, ge=1, le=200),
              venture_id: str | None = Query(None, max_length=128),
              kinds: list[str] = Query(default=[], max_length=10), active_only: bool = False):
    return job_status.list_jobs(workspace_id, limit, venture_id, kinds, active_only)


@router.get("/jobs/{job_id}")
def read_job(job_id: str):
    result = job_status.read_job(job_id)
    if result is None:
        raise HTTPException(404, detail={"code": "not_found"})
    return result

"""Founder task cards/timeline through the ordinary venture flow."""
from fastapi import APIRouter, Header
from pydantic import BaseModel, ConfigDict, Field

from app.tasks.service import handoff_session_factory as db
from app.tasks import service
from app.tasks.contracts import Contract
from app.tasks.service import handoff_owner as _owner, handoff_task as _task, locked_handoff as _locked_task

router = APIRouter(tags=["tasks"])


class TaskBody(BaseModel):
    goal: str = Field(min_length=1, max_length=8000)
    timezone: str | None = Field(None, max_length=80)
    contract: Contract | None = None
    start: bool = True


@router.get("/ventures/{venture_id}/handoffs")
def list_tasks(venture_id: str):
    return service.list_handoffs(venture_id, session_factory=db())


@router.post("/ventures/{venture_id}/handoffs/preview")
async def preview(venture_id: str, body: TaskBody):
    return await service.preview_handoff(venture_id, body, session_factory=db())


@router.post("/ventures/{venture_id}/handoffs", status_code=202)
async def create(venture_id: str, body: TaskBody, idempotency_key: str | None = Header(None, max_length=128)):
    return await service.create_handoff(venture_id, body, idempotency_key, session_factory=db())


@router.get("/ventures/{venture_id}/handoffs/{handoff_id}")
def read_task(venture_id: str, handoff_id: str):
    return service.read_handoff(venture_id, handoff_id, session_factory=db())


@router.get("/ventures/{venture_id}/handoffs/{handoff_id}/result")
def task_result(venture_id: str, handoff_id: str):
    return service.handoff_result(venture_id, handoff_id, session_factory=db())


@router.post("/ventures/{venture_id}/handoffs/{handoff_id}/cancel")
def cancel(venture_id: str, handoff_id: str):
    return service.cancel_handoff(venture_id, handoff_id, session_factory=db())


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: str):
    return service.cancel_handoff_task(task_id, session_factory=db())


@router.post("/ventures/{venture_id}/handoffs/{handoff_id}/pause")
def pause(venture_id: str, handoff_id: str):
    return service.pause_handoff(venture_id, handoff_id, session_factory=db())


class ResumeTaskBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    continue_budget: bool = False
    expected_updated_at: str | None = Field(None, max_length=80)


@router.post("/ventures/{venture_id}/handoffs/{handoff_id}/resume")
def resume(venture_id: str, handoff_id: str, body: ResumeTaskBody | None = None,
           idempotency_key: str | None = Header(None, max_length=128)):
    return service.resume_handoff(venture_id, handoff_id, body, idempotency_key, session_factory=db())


class EditTaskBody(TaskBody):
    expected_updated_at: str = Field(min_length=1, max_length=80)


@router.patch("/ventures/{venture_id}/handoffs/{handoff_id}")
def edit_task(venture_id: str, handoff_id: str, body: EditTaskBody):
    return service.edit_handoff(venture_id, handoff_id, body, session_factory=db())


from app.tasks.preferences import router as preferences_router  # noqa: E402
ROUTERS = [preferences_router]

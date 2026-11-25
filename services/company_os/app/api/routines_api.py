"""Private routine settings, using the task ownership boundary."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.tasks import routines

router = APIRouter(tags=["routines"])


class RoutineBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str = Field(min_length=1, max_length=8000)
    timezone: str | None = Field(None, max_length=80)
    quiet_hours: dict[str, str] | None = None


class RoutineSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timezone: str = Field(min_length=1, max_length=80)
    quiet_hours: dict[str, str]


@router.get("/ventures/{venture_id}/routines")
def list_routines(venture_id: str):
    return {"items": routines.list_routines(venture_id)}


@router.post("/ventures/{venture_id}/routines")
async def create_routine(venture_id: str, body: RoutineBody):
    try:
        return await routines.create_from_settings(venture_id, body.goal, timezone=body.timezone, quiet=body.quiet_hours)
    except PermissionError as exc:
        raise HTTPException(403, "Open your venture's Routines screen from your own account, then save your routine again.") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/ventures/{venture_id}/routines/{routine_id}/pause")
def pause_routine(venture_id: str, routine_id: str):
    return routines.change(venture_id, routine_id)


@router.patch("/ventures/{venture_id}/routines/{routine_id}")
def save_settings(venture_id: str, routine_id: str, body: RoutineSettings):
    try:
        return routines.settings(venture_id, routine_id, timezone=body.timezone, quiet=body.quiet_hours)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.delete("/ventures/{venture_id}/routines/{routine_id}")
def delete_routine(venture_id: str, routine_id: str):
    return routines.change(venture_id, routine_id, delete=True)

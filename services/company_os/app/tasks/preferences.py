"""Owner-only timezone and venture focus preferences; no task mutation."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.db.models import Person
from app.state import db
from app.tasks.contracts import timezone_name
from app.tasks.role_presets import RolePreset, read_role_preset, save_role_preset

router = APIRouter(tags=["task preferences"])


class PreferencesBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timezone: str = Field(min_length=1, max_length=80)
    only_if_unconfirmed: bool = False


class RolePresetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role_preset: RolePreset


def _person(session, venture_id, *, lock=False):
    # Reuse the task API's owner gate after routers have initialized.
    from app.api.handoffs_api import _owner
    _, person = _owner(session, venture_id)
    if person is None:
        raise HTTPException(404, "Not found.")
    return session.get(Person, person.id, with_for_update=lock, populate_existing=True)


def _view(person):
    try:
        zone = timezone_name(person.timezone)
        return {"timezone": zone, "confirmed": bool(person.timezone)}
    except ValueError:
        return {"timezone": "UTC", "confirmed": False}


@router.get("/ventures/{venture_id}/task-preferences")
def get_preferences(venture_id: str):
    with db()() as session:
        return _view(_person(session, venture_id))


@router.patch("/ventures/{venture_id}/task-preferences")
def set_preferences(venture_id: str, body: PreferencesBody):
    with db()() as session:
        person = _person(session, venture_id, lock=True)
        if body.only_if_unconfirmed and _view(person)["confirmed"]:
            return _view(person)
        try:
            zone = body.timezone.strip()
            if not zone:
                raise ValueError("Choose a valid timezone")
            person.timezone = timezone_name(zone)
        except ValueError as exc:
            raise HTTPException(422, "Choose a valid timezone.") from exc
        result = _view(person)
        session.commit()
        return result


@router.get("/ventures/{venture_id}/task-preferences/role-preset")
def get_role_preferences(venture_id: str):
    with db()() as session:
        return read_role_preset(session, venture_id)


@router.patch("/ventures/{venture_id}/task-preferences/role-preset")
def set_role_preferences(venture_id: str, body: RolePresetBody):
    with db()() as session:
        result = save_role_preset(session, venture_id, body.role_preset)
        session.commit()
        return result

"""Live presence for a workspace (doc 112 P7)."""

from __future__ import annotations

from app import presence as _service

from fastapi import APIRouter, Header
from pydantic import BaseModel, ConfigDict

from app import presence
from app.authz.workspace import assert_workspace_access, subject_person_id

router = APIRouter(tags=["presence"])


class BeatBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    surface: str | None = None


def _display_name(person_id: str) -> str | None:
    return _service.presence_display_name(person_id)


@router.get("/workspaces/{workspace_id}/presence")
def get_presence(
    workspace_id: str,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    assert_workspace_access(workspace_id, x_subject)
    return presence.list_live(workspace_id)


@router.post("/workspaces/{workspace_id}/presence")
def post_presence(
    workspace_id: str,
    body: BeatBody,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    assert_workspace_access(workspace_id, x_subject)
    person_id = subject_person_id(x_subject)
    return presence.beat(
        workspace_id,
        person_id,
        display_name=_display_name(person_id),
        surface=body.surface,
    )

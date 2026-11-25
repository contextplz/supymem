"""Landing-page builder session controls."""

from __future__ import annotations

from app.onboarding.steps import build as _service

from typing import Literal

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel, Field, field_validator


router = APIRouter(tags=["builder"])

LOCAL_ADAPTERS = {"cursor", "claude", "codex", "opencode"}
CLOUD_ADAPTERS = {"cloud", "stub", "hosted"}


class BuilderChoiceBody(BaseModel):
    adapter: Literal["cursor", "claude", "codex", "opencode", "cloud"]

    @field_validator("adapter")
    @classmethod
    def local_opencode_only(cls, adapter):
        if adapter == "opencode" and not _service.is_local():
            raise ValueError("Choose Cursor, Claude Code, Codex, or cloud for this hosted session.")
        return adapter


class BuilderProgressBody(BaseModel):
    text: str = Field(min_length=1, max_length=200)


def _venture(venture_id: str):
    return _service.builder_venture(venture_id)


def _prompt_for(ven) -> str:
    return _service.builder_prompt_for(ven)


def _choice_payload(venture_id: str) -> dict:
    return _service.builder_choice_payload(venture_id)


@router.get("/ventures/{venture_id}/builder/choice")
def get_builder_choice(venture_id: str):
    return _service.builder_get_builder_choice(venture_id)


@router.post("/ventures/{venture_id}/builder/choice")
async def choose_builder(venture_id: str, payload: BuilderChoiceBody):
    return await _service.builder_choose_builder(venture_id, payload)


class BuilderDoneBody(BaseModel):
    ready: bool = True
    html: str | None = None
    files: dict[str, str] | None = None


class BuilderPageChunk(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    index: int = Field(ge=0, le=200)
    total: int = Field(ge=1, le=200)
    b64: str = Field(min_length=1, max_length=8000)


@router.post("/ventures/{venture_id}/builder/failed")
async def fail_builder_session(venture_id: str, payload: BuilderProgressBody):
    return await _service.builder_fail_builder_session(venture_id, payload)


@router.post("/ventures/{venture_id}/builder/done")
async def finish_builder_session(
    venture_id: str, payload: BuilderDoneBody | None = Body(default=None)
):
    return await _service.builder_finish_builder_session(venture_id, payload)


@router.post("/ventures/{venture_id}/builder/page")
async def receive_builder_page_chunk(venture_id: str, payload: BuilderPageChunk):
    return await _service.builder_receive_builder_page_chunk(venture_id, payload)


@router.post("/ventures/{venture_id}/builder/progress")
async def builder_progress(venture_id: str, payload: BuilderProgressBody):
    return await _service.builder_builder_progress(venture_id, payload)


async def complete_builder_because_page_exists(venture_id: str, run) -> bool:
    return await _service.builder_complete_builder_because_page_exists(venture_id, run)

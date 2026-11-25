"""Coding agent Protocol — shared event/result shapes (08 §3)."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
import re
from typing import Any, AsyncIterator, Protocol

from pydantic import BaseModel, Field, model_validator

from core.redact import redact_string


_PRIVATE_FIELD = re.compile(r"(?:^|[_-])(?:api[_-]?keys?|passwords?|secret(?:[_-]?access[_-]?key)?s?|token|authorization|credentials?)$", re.I)
_PRIVATE_VALUE = re.compile(r"(?:api[_-]?key|secret(?:[_-]?access[_-]?key)?|token|password)['\"]?\s*[:=]\s*['\"]?[A-Za-z0-9_./+\-=]{16,}", re.I)


def safe_event_data(value: Any) -> Any:
    if isinstance(value, str):
        return _PRIVATE_VALUE.sub("[REDACTED]", redact_string(value)).replace("—", ": ")
    if isinstance(value, dict):
        return {key: "[REDACTED]" if _PRIVATE_FIELD.search(key)
                else safe_event_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [safe_event_data(item) for item in value]
    return value


class AgentEventKind(StrEnum):
    STARTED = "started"
    MESSAGE = "message"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    FILE_CHANGED = "file_changed"
    USAGE = "usage"
    FINISHED = "finished"
    FAILED = "failed"


class AgentEvent(BaseModel):
    kind: AgentEventKind
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    session_id: str | None = None
    text: str | None = None
    tool_name: str | None = None
    tool_call_id: str | None = None
    path: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    reasoning_tokens: int | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def scrub_private_values(cls, value):
        return safe_event_data(value)


class CodingRequest(BaseModel):
    prompt: str
    workdir: Path
    output_schema: dict[str, Any] | None = None
    resume_session_id: str | None = None
    model: str | None = None
    timeout_s: int = 1800
    allow_write: bool = True
    allow_network: bool = False


class CodingResult(BaseModel):
    ok: bool
    session_id: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    events: list[AgentEvent] = Field(default_factory=list)
    error: str | None = None
    write_flag_used: bool = False
    argv: list[str] = Field(default_factory=list)
    preview_url: str | None = None

    @model_validator(mode="before")
    @classmethod
    def scrub_private_values(cls, value):
        return safe_event_data(value)


class CodingAdapter(Protocol):
    name: str
    write_flag: str

    def build_argv(self, req: CodingRequest) -> list[str]: ...
    def parse_event(self, line: dict[str, Any]) -> AgentEvent | None: ...
    async def run(self, req: CodingRequest) -> CodingResult: ...

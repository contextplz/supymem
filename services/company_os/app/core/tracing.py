"""Lightweight span helpers (18 §2) — no OpenTelemetry collector locally."""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from core.clock import utcnow
from core.ids import new_id
from core.redact import redact

SpanKind = Literal["http", "llm", "tool", "subprocess", "job", "warden", "db"]

_current_trace: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "trace_id", default=None
)
_current_span: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "span_id", default=None
)

_SPAN_SINK: list[Span] = []


@dataclass
class Span:
    id: str
    parent_id: str | None
    trace_id: str
    name: str
    kind: SpanKind
    start: datetime
    end: datetime | None = None
    status: str = "ok"
    attrs: dict[str, Any] = field(default_factory=dict)

    def finish(self, *, status: str = "ok", **attrs: Any) -> None:
        self.end = utcnow()
        self.status = status
        if attrs:
            self.attrs.update(redact(attrs) if isinstance(attrs, dict) else attrs)


def current_trace_id() -> str | None:
    return _current_trace.get()


def current_span_id() -> str | None:
    return _current_span.get()


@contextmanager
def start_span(
    name: str,
    *,
    kind: SpanKind = "http",
    trace_id: str | None = None,
    attrs: dict[str, Any] | None = None,
) -> Iterator[Span]:
    parent = _current_span.get()
    tid = trace_id or _current_trace.get() or new_id("tr")
    span = Span(
        id=new_id("sp"),
        parent_id=parent,
        trace_id=tid,
        name=name,
        kind=kind,
        start=utcnow(),
        attrs=redact(attrs or {}),
    )
    tok_t = _current_trace.set(tid)
    tok_s = _current_span.set(span.id)
    try:
        yield span
        if span.end is None:
            span.finish(status="ok")
    except Exception as exc:
        span.finish(status="error", error=str(exc))
        raise
    finally:
        _SPAN_SINK.append(span)
        _current_span.reset(tok_s)
        _current_trace.reset(tok_t)


def drain_spans() -> list[Span]:
    """Return and clear buffered spans (tests / local sink flush)."""
    out = list(_SPAN_SINK)
    _SPAN_SINK.clear()
    return out

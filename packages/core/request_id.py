"""x-request-id generate / accept / echo (docs/supymem/21 §1). Starlette-free ASGI."""

from __future__ import annotations

from typing import Any
from contextlib import contextmanager
from contextvars import ContextVar
import logging
import re

from core.ids import new_id

HEADER = "x-request-id"
HEADER_BYTES = b"x-request-id"
_request_id = ContextVar("observability_request_id", default=None)


def current_request_id() -> str | None:
    return _request_id.get()


@contextmanager
def request_context(value):
    safe = value if isinstance(value, str) and re.fullmatch(r"req_[0-9abcdefghjkmnpqrstvwxyz]{12}", value) else None
    token = _request_id.set(safe)
    try:
        yield
    finally:
        _request_id.reset(token)


def resolve_request_id(incoming: str | None, *, generated_only=False) -> str:
    raw = (incoming or "").strip()
    if re.fullmatch(r"req_[0-9abcdefghjkmnpqrstvwxyz]{12}", raw) or (
        not generated_only and 8 <= len(raw) <= 128 and all(char.isalnum() or char in "-_" for char in raw)
    ):
        return raw
    return new_id("req")


class RequestIdMiddleware:
    def __init__(self, app: Any, *, generated_only=False) -> None:
        self.app = app
        self.generated_only = generated_only

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        incoming = None
        for key, value in scope.get("headers") or []:
            if key.lower() == HEADER_BYTES:
                incoming = value.decode("latin-1")
                break
        rid = resolve_request_id(incoming, generated_only=self.generated_only)
        scope.setdefault("state", {})
        if isinstance(scope["state"], dict):
            scope["state"]["request_id"] = rid

        async def send_with_id(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers") or [])
                headers = [(key, value) for key, value in headers if key.lower() != HEADER_BYTES]
                headers.append((HEADER_BYTES, rid.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        with request_context(rid):
            try:
                await self.app(scope, receive, send_with_id)
            finally:
                logging.getLogger("app.request").info("http_request_completed", extra={"request_id": current_request_id(),
                                                                                     "trace_id": current_request_id()})


def from_request(request: Any) -> str | None:
    state = getattr(request, "state", None)
    return getattr(state, "request_id", None)

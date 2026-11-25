"""Closed, durable receipts for Core HTTP failures.

Only static route templates and fixed labels are saved. Request bodies, query
strings, headers, exception messages and tracebacks never enter the receipt.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from typing import Any

from app.analytics.error_events import log_error_event
from app.analytics.feature_catalog import route_feature
from app.error_reports.service import top_frame
from app.db.models import AnalyticsEvent
from app.state import db
from core.clock import utcnow
from core.ids import new_id

log = logging.getLogger(__name__)

_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
_ROUTE_TEMPLATE = re.compile(r"/[a-zA-Z0-9_./{}:-]{0,159}\Z")
_FAMILIES = frozenset({"admin", "api", "auth", "health", "internal", "public", "ready", "ventures", "workspaces", "w"})
# RequestIdMiddleware also accepts caller-provided X-Request-ID values. Only
# opaque IDs in our generated shape may enter a private error receipt.
_REQUEST_ID = re.compile(r"req_[0-9abcdefghjkmnpqrstvwxyz]{12}\Z")
_ERROR_CLASSES = frozenset({
    "RuntimeError", "TimeoutError", "ConnectionError", "OSError", "ValueError",
    "TypeError", "KeyError", "AttributeError", "AssertionError", "PermissionError",
    "DatabaseError", "IntegrityError", "OperationalError",
})


def closed_error_class(value: object) -> str:
    return value if isinstance(value, str) and value in _ERROR_CLASSES | {"none", "other"} else "other" if value else "none"


def _route_label(scope: dict[str, Any]) -> str:
    route = scope.get("route")
    template = getattr(route, "path", None)
    if isinstance(template, str) and _ROUTE_TEMPLATE.fullmatch(template):
        return template
    path = scope.get("path")
    first = path.strip("/").split("/", 1)[0] if isinstance(path, str) else ""
    return "/" + first if first in _FAMILIES else "/other"


# A matched route answering one of these is a product bug worth counting; 401 is
# token expiry noise and an unmatched 404 is scanner noise.
REQUEST_ERROR_STATUSES = frozenset({400, 402, 403, 404, 409, 410, 413, 422, 429})
_ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,47}\Z")
_SCOPE_ID = re.compile(r"[A-Za-z0-9_:-]{1,128}\Z")
_DEDUPE_S, _MAX_TRACKED, _MAX_PER_MINUTE = 60.0, 2048, 120
_seen: dict[tuple, float] = {}
_budget = [0.0, 0]
_gate = threading.Lock()


def _admit_request_error(key: tuple) -> bool:
    """Bound 4xx volume: one row per (person, route, status) per minute, at most N rows a minute per process."""
    now = time.monotonic()
    with _gate:
        if now - _budget[0] >= 60:
            _budget[0], _budget[1] = now, 0
        if _budget[1] >= _MAX_PER_MINUTE or now - _seen.get(key, -_DEDUPE_S) < _DEDUPE_S:
            return False
        if len(_seen) >= _MAX_TRACKED:
            for old in [k for k, at in _seen.items() if now - at >= _DEDUPE_S] or list(_seen):
                _seen.pop(old, None)
        _seen[key] = now
        _budget[1] += 1
        return True


def response_error_code(body: bytes) -> str | None:
    """Closed ``error.code`` from a small JSON error body, else None. The body is never stored."""
    try:
        data = json.loads(body[:4096])
    except (TypeError, ValueError):
        return None
    detail = data.get("detail", data) if isinstance(data, dict) else None
    for node in (detail, data if isinstance(data, dict) else None):
        if isinstance(node, str) and _ERROR_CODE.fullmatch(node):
            return node
        if isinstance(node, dict):
            inner = node.get("error") if isinstance(node.get("error"), dict) else node
            code = inner.get("code")
            if isinstance(code, str) and _ERROR_CODE.fullmatch(code):
                return code
    return None


def _path_id(scope: dict[str, Any], name: str) -> str | None:
    params = scope.get("path_params")
    value = params.get(name) if isinstance(params, dict) else None
    return value if isinstance(value, str) and _SCOPE_ID.fullmatch(value) else None


# Absent optional resources: the console models these as normal empty states.
_EXPECTED_404 = frozenset({
    ("/ventures/{venture_id}/onboarding", "no_onboarding"),
    ("/ventures/{venture_id}/charter", "no_charter"),
    ("/workspaces/{workspace_id}/site", "no_site"),
})


def record_http_failure(scope: dict[str, Any], *, status: int, failure_kind: str,
                        error_class: str | None = None, error_code: str | None = None,
                        frame: str = "") -> None:
    """Best-effort independent transaction; error recording cannot break a request."""
    http_failure = 500 <= status <= 599 and failure_kind in {"http_5xx", "unhandled_exception"}
    stream_failure = 200 <= status <= 499 and failure_kind == "stream_exception"
    request_failure = (status in REQUEST_ERROR_STATUSES and failure_kind == "http_4xx"
                       and (status != 404 or scope.get("route") is not None))
    if not (http_failure or stream_failure or request_failure):
        return
    method = scope.get("method")
    state = scope.get("state")
    rid = state.get("request_id") if isinstance(state, dict) else None
    verified = state.get("cos_verified_caller") if isinstance(state, dict) else None
    caller = verified[3] if isinstance(verified, tuple) and len(verified) == 4 else None
    person_id = None
    if caller is not None and not getattr(caller, "legacy", True):
        sub = getattr(caller, "sub", None)
        if isinstance(sub, str) and re.fullmatch(r"[a-zA-Z0-9_:-]{1,128}", sub):
            person_id = sub
    route = _route_label(scope)
    if (route == "/webhooks/email/resend" and status == 503 and failure_kind == "http_5xx"
            and isinstance(state, dict) and state.get("email_callback_unmatched") is True):
        # A verified callback can arrive before the send receipt commits. Its
        # retry must not enqueue an error email while that sender holds a lock.
        return
    if request_failure and status == 404 and (route, error_code) in _EXPECTED_404:
        return
    feature = route_feature(route)
    if http_failure:
        from core.error_event import observability_metric

        observability_metric("http_5xx", feature)
    venture_id, workspace_id = _path_id(scope, "venture_id"), _path_id(scope, "workspace_id")
    props = {
        "service": "core",
        "route": route,
        "method": method if method in _METHODS else "OTHER",
        "status": status,
        "failure_kind": failure_kind,
        "error_class": closed_error_class(error_class),
        "feature": feature,
    }
    name = "server.error"
    if request_failure:
        name = "server.request_error"
        props["error_code"] = error_code if isinstance(error_code, str) and _ERROR_CODE.fullmatch(error_code) else "none"
        del props["error_class"]
        if not _admit_request_error((person_id, route, status)):
            return
    code = props.get("error_code") or props["error_class"]
    log_error_event("server", feature=feature, code=code if code != "none" else failure_kind,
                    venture_id=venture_id, person_id=person_id, status=status, place=route, frame=frame,
                    workspace_id=workspace_id, level=logging.WARNING if request_failure else logging.ERROR)
    incoming = next((value.decode("latin-1") for key, value in scope.get("headers", [])
                     if key.lower() == b"x-request-id"), None)
    if isinstance(rid, str) and _REQUEST_ID.fullmatch(rid) and (incoming is None or _REQUEST_ID.fullmatch(incoming)):
        props["request_id"] = rid
    try:
        with db()() as session:
            session.add(AnalyticsEvent(
                id=new_id("srv"), person_id=person_id, workspace_id=workspace_id,
                venture_id=venture_id, event_name=name,
                props_json=json.dumps(props, sort_keys=True), occurred_at=utcnow(),
            ))
            session.commit()
    except Exception:
        # The primary failure may itself be a database outage. Do not mask it,
        # recurse into analytics, or log potentially sensitive exception text.
        log.warning("Core server-error receipt unavailable")


class ServerErrorCaptureMiddleware:
    """Record one Core 5xx per request, including uncaught exceptions."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        recorded = False
        response_started = False
        response_status: int | None = None
        pending_4xx: int | None = None

        async def record_4xx(code: str | None) -> None:
            nonlocal pending_4xx
            status, pending_4xx = pending_4xx, None
            if status is not None:
                await asyncio.to_thread(record_http_failure, scope, status=status,
                                        failure_kind="http_4xx", error_code=code)

        async def send_and_record(message: dict[str, Any]) -> None:
            nonlocal recorded, response_started, response_status, pending_4xx
            if message.get("type") == "http.response.start":
                response_started = True
                status = message.get("status")
                response_status = status if isinstance(status, int) else None
                if isinstance(status, int) and status >= 500 and not recorded:
                    recorded = True
                    await asyncio.to_thread(record_http_failure, scope, status=status, failure_kind="http_5xx")
                elif status in REQUEST_ERROR_STATUSES:
                    pending_4xx = status
            await send(message)
            if message.get("type") == "http.response.body" and pending_4xx is not None:
                await record_4xx(response_error_code(message.get("body") or b""))

        try:
            await self.app(scope, receive, send_and_record)
            await record_4xx(None)  # a 4xx that never sent a body
        except Exception as exc:
            if not recorded and (not response_started or response_status is not None):
                # Once headers have gone out, the client may already hold a
                # non-5xx status. Save that failure without inventing an HTTP 500.
                await asyncio.to_thread(record_http_failure, scope,
                    status=500 if not response_started else response_status,
                    failure_kind="unhandled_exception" if not response_started else "stream_exception",
                    error_class=type(exc).__name__, frame=top_frame(exc))
            raise

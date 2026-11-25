"""Standard ``error_event`` log line for every non-core service (gateway, warden, ...).

One CloudWatch filter on ``error_event`` covers all of them (see scripts/infra/configure_error_alarm.py), same
line shape as ``services/company_os/app/analytics/error_events.py``::

    error_event {"app_version":..,"code":..,"component":"gateway","intent":..,"source":..,"status":..,"venture":..}

Closed labels only: no request text, query string, exception message or visitor data.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

log = logging.getLogger("app.error_event")
metric_log = logging.getLogger("app.observability")
_CODE = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z")
_ID = re.compile(r"[A-Za-z0-9_:-]{1,128}\Z")


def observability_metric(metric: str, feature: str, *, service: str = "core") -> None:
    if metric not in {"dead_end", "http_5xx"}:
        return
    body = {"_aws": {"Timestamp": int(time.time() * 1000), "CloudWatchMetrics": [{
        "Namespace": "Supymem/Observability", "Dimensions": [["feature", "service"]],
        "Metrics": [{"Name": metric, "Unit": "Count"}]}]},
        "feature": _closed(feature), "service": _closed(service), metric: 1}
    try:
        metric_log.info(json.dumps(body, separators=(",", ":")), extra={"emf": body})
    except Exception:
        pass


def _closed(value: object, fallback: str = "other") -> str:
    return value if isinstance(value, str) and _CODE.fullmatch(value) else fallback


def error_event_body(component: str, *, source: str, intent: str, code: str, status: int | None = None,
                     venture_id: str | None = None) -> dict[str, Any]:
    body = {"component": _closed(component), "source": _closed(source), "intent": _closed(intent),
            "code": _closed(code), "status": int(status) if status is not None else None,
            "venture": venture_id if isinstance(venture_id, str) and _ID.fullmatch(venture_id) else None,
            "app_version": _closed(os.environ.get("APP_VERSION") or os.environ.get("IMAGE_TAG"), "unknown")}
    return {k: v for k, v in body.items() if v is not None}


def log_error_event(component: str, *, source: str, intent: str, code: str, status: int | None = None,
                    venture_id: str | None = None) -> None:
    try:
        body = error_event_body(component, source=source, intent=intent, code=code, status=status, venture_id=venture_id)
        log.error("error_event %s", json.dumps(body, sort_keys=True))
    except Exception:  # noqa: BLE001 - logging never breaks the request
        pass


def route_intent(scope: dict[str, Any]) -> str:
    """``post_public_waitlists_venture_id``: method plus the matched route template, never the raw path."""
    template = getattr(scope.get("route"), "path", None)
    method = str(scope.get("method") or "x").lower()
    if not isinstance(template, str):
        return f"{method}_unmatched"
    return (method + "_" + re.sub(r"[^a-z0-9]+", "_", template.lower()).strip("_"))[:60]


class ErrorEventMiddleware:
    """Pure-ASGI: one ``error_event`` per 5xx response or uncaught exception, attributed to the venture in the path."""

    def __init__(self, app: Any, component: str) -> None:
        self.app, self.component = app, component

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        logged = False

        def emit(code: str, status: int) -> None:
            params = scope.get("path_params") or {}
            feature = "shell"
            if self.component == "gateway":
                path = str((params or {}).get("path") or "")
                parts = path.strip("/").split("/")
                suffix = parts[2] if len(parts) > 2 and parts[0] == "ventures" else parts[0] if parts else ""
                feature = {"idea": "idea", "research": "research", "plans": "planning", "gtm": "gtm_diagnosis",
                           "chat": "chat", "device-work-orders": "computer_use", "credits": "credits",
                           "analytics": "analytics", "waitlist": "waitlist", "tasks": "execution"}.get(suffix, "shell")
            observability_metric("http_5xx", feature, service=self.component)
            log_error_event(self.component, source="http", intent=route_intent(scope), code=code, status=status,
                            venture_id=params.get("venture_id") if isinstance(params, dict) else None)

        async def watch(message: dict[str, Any]) -> None:
            nonlocal logged
            status = message.get("status")
            if message.get("type") == "http.response.start" and isinstance(status, int) and status >= 500 and not logged:
                logged = True
                emit("http_5xx", status)
            await send(message)

        try:
            await self.app(scope, receive, watch)
        except Exception as exc:
            if not logged:
                emit(type(exc).__name__, 500)
            raise

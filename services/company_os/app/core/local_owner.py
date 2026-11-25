"""Single-owner identity and the network boundary for self-hosted requests."""

from __future__ import annotations

import asyncio
import os
import secrets
from contextvars import ContextVar

from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send
from core.local_security import (LocalNetworkMiddleware, _proxy_secret, check_request, local_readiness_request,
                                 owner_secret_path, public_request)

from app.core.local_mode import is_local

_OWNER: ContextVar = ContextVar("local_owner", default=None)


def ensure_owner():
    from app.workspaces.resolve import ensure_desk, local_person

    try:
        ensure_desk()
        return local_person()
    except (OSError, SQLAlchemyError) as exc:
        raise HTTPException(503, detail={
            "code": "local_auth_not_ready",
            "message": "Check your database connection and data directory permissions, then restart your app and try again.",
        }) from exc


def current_owner():
    owner = _OWNER.get()
    if owner is None:
        raise HTTPException(503, detail={
            "code": "local_auth_not_ready",
            "message": "Complete your local setup, then restart your app and try again.",
        })
    return owner


def _ensure_proxy_secret() -> None:
    from app.config import data_root

    data_root()
    destination = owner_secret_path()
    if not destination.exists():
        temporary = destination.with_name(secrets.token_hex(16) + ".tmp")
        if os.name == "nt":
            from app.core.local_windows import private_directory, private_open

            private_directory(destination.parent)
            descriptor = private_open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                if os.name != "nt":
                    os.fchmod(stream.fileno(), 0o600)
                stream.write(secrets.token_hex(32).encode())
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError:
                pass
        finally:
            os.unlink(temporary)
    _proxy_secret()


def request_owner(connection: HTTPConnection):
    check_request(connection)
    if public_request(connection):
        raise HTTPException(403, detail={"code": "local_owner_required",
                            "message": "Open your desktop app and retry this action from your workspace."})
    owner = getattr(connection.state, "local_owner", None)
    if owner is None:
        owner = ensure_owner()
        connection.state.local_owner = owner
    return owner


class LocalOwnerMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.boundary = LocalNetworkMiddleware(self._owned_request)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if is_local() and scope["type"] == "http" and scope["path"].endswith("/e") and scope["method"] in {"POST", "HEAD"}:
            from urllib.parse import urlsplit
            from starlette.requests import Request
            from starlette.responses import Response
            from app.api.events_ingest import track
            from app.product_analytics.beacon import public_events_url

            endpoint = public_events_url()
            connection = HTTPConnection(scope)
            hosts = connection.headers.getlist("host")
            if endpoint and urlsplit(endpoint).path == scope["path"] and len(hosts) == 1 and hosts[0].lower() == urlsplit(endpoint).netloc.lower():
                if scope["method"] == "HEAD":
                    await Response(status_code=204, headers={"X-Page-Measurement": "local", "Cache-Control": "no-store"})(scope, receive, send)
                    return
                scope = {**scope, "headers": [(name, value) for name, value in scope["headers"]
                    if name.lower() not in {b"x-subject", b"x-cos-assertion", b"authorization", b"x-api-key",
                        b"x-workspace-id", b"cookie", b"x-local-proxy-secret"}],
                    "state": {**scope.get("state", {}), "local_owner": None}}
                token = _OWNER.set(None)
                try:
                    result = await track(Request(scope, receive=receive))
                finally:
                    _OWNER.reset(token)
                response = result if isinstance(result, Response) else JSONResponse(result)
                response.headers["Access-Control-Allow-Origin"] = "*"
                response.headers["Cache-Control"] = "no-store"
                await response(scope, receive, send)
                return
        await self.boundary(scope, receive, send)

    async def _owned_request(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not is_local():
            await self.app(scope, receive, send)
            return
        if scope["type"] == "lifespan":
            await asyncio.to_thread(_ensure_proxy_secret)
            try:
                await asyncio.to_thread(ensure_owner)
            except HTTPException as exc:
                if exc.status_code != 503:
                    raise
        elif scope["type"] in {"http", "websocket"}:
            connection = HTTPConnection(scope)
            try:
                check_request(connection)
                if local_readiness_request(connection):
                    from app.health import ready_response
                    from core.local_readiness import readiness_headers

                    headers = readiness_headers(connection.headers.get("x-local-ready-nonce"))
                    status = 403 if headers is None else (await asyncio.to_thread(ready_response)).status_code
                    await Response(status_code=204 if status == 200 else status,
                                   headers=headers or {"Cache-Control": "no-store"})(scope, receive, send)
                    return
                if public_request(connection):
                    scope["headers"] = [(key, value) for key, value in scope["headers"] if key.lower() not in {
                        b"x-subject", b"x-cos-assertion", b"authorization", b"x-api-key", b"x-workspace-id",
                        b"cookie", b"x-local-proxy-secret"}]
                    connection.state.local_owner = None
                    token = _OWNER.set(None)
                    try:
                        await self.app(scope, receive, send)
                    finally:
                        _OWNER.reset(token)
                    return
                if (scope["type"] == "http" and scope["method"] in {"GET", "HEAD"}
                        and scope["path"] in {"/health", "/ready"}):
                    await self.app(scope, receive, send)
                    return
                owner = await asyncio.to_thread(ensure_owner)
            except HTTPException as exc:
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008,
                                "reason": exc.detail["message"] if exc.status_code == 503
                                    else "Open your local app directly and try again."})
                else:
                    await JSONResponse({"detail": exc.detail}, status_code=exc.status_code)(scope, receive, send)
                return
            connection.state.local_owner = owner
            scope["headers"] = [
                (key, value) for key, value in scope["headers"]
                if key.lower() not in {b"x-subject", b"x-cos-assertion"}
            ] + [(b"x-subject", owner.id.encode("utf-8"))]
            if scope["type"] == "http":
                path, method = scope["path"], scope["method"]
                if (path == "/auth/session" and method == "GET") or (path == "/auth/login" and method == "POST"):
                    await JSONResponse({"authenticated": True, "subject": owner.id, "email": owner.email,
                                        "mode": "local", "expires_at": None, "csrf": None},
                                       headers={"Cache-Control": "no-store"})(scope, receive, send)
                    return
                if path == "/auth/logout" and method == "POST":
                    await JSONResponse({"ok": True})(scope, receive, send)
                    return
                if path in {"/auth/cognito/start", "/auth/cognito/exchange"}:
                    await JSONResponse({"detail": {"code": "local_signin_unnecessary",
                        "message": "You are already signed in. Open your workspace to continue."}},
                        status_code=409)(scope, receive, send)
                    return
        if scope["type"] in {"http", "websocket"}:
            token = _OWNER.set(owner)
            try:
                await self.app(scope, receive, send)
            finally:
                _OWNER.reset(token)
        else:
            await self.app(scope, receive, send)


if __name__ == "__main__" and is_local():
    _ensure_proxy_secret()

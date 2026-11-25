"""Authenticated desktop bridge with real PKCE exchange and scoped delivery.

The packaged app receives short-lived access credentials only from its native
Keychain owner. Standalone development PKCE credentials remain in process memory;
there is no plaintext credential cache or synthetic connection.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from core.local_security import local_browser_origins
from desktop_runtime import hosted, offline, pkce
from desktop_runtime.device_gateway import UpstreamRejected
from desktop_runtime.crash_report import clear_crash_owner, pending_crash_summaries, report_error, save_crash_owner
from desktop_runtime.local_http import valid_loopback_host

log = logging.getLogger("desktop_runtime.bridge")


class NativeRuntimeHeartbeat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
    session_id: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
    app_version: str = Field(pattern=r"^\d{1,4}\.\d{1,4}\.\d{1,4}$")


def _error(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": {"code": code}})


def _expected_token() -> str:
    token = os.environ.get("BRIDGE_TOKEN", "").strip()
    if not token:
        token = secrets.token_urlsafe(32)
        os.environ["BRIDGE_TOKEN"] = token
    return token


def _gateway() -> str:
    value = ((os.environ.get("COMPANY_OS_URL") or "http://127.0.0.1:3300")
             if hosted._is_local() else hosted.gateway_url()).rstrip("/")
    parsed = urlsplit(value)
    local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if hosted._is_local() and not local:
        raise ValueError("Open Settings > Keys and connections and use a local API address, then restart your app.")
    if (parsed.scheme != "https" and not (parsed.scheme == "http" and local)) or parsed.username or parsed.password:
        raise ValueError("bridge_requires_secure_gateway")
    if parsed.path or parsed.query or parsed.fragment or not parsed.hostname:
        raise ValueError("invalid_gateway_origin")
    return value


def _api_url(origin: str, path: str) -> str:
    if hosted._is_local() and path.startswith("/api/") and urlsplit(path).path.split("/", 3)[2] != "v1":
        path = path[4:]
    return origin + path


def _valid_path(path: str) -> bool:
    decoded = unquote(path)
    # Refuse nested escaping; different proxy/router decode counts must not turn
    # a permitted /api path into an authentication endpoint or another origin.
    if unquote(decoded) != decoded:
        return False
    parsed = urlsplit(decoded)
    return (decoded.startswith("/api/") and not parsed.scheme and not parsed.netloc
            and not parsed.fragment and "\\" not in decoded
            and not any(part in {".", ".."} for part in parsed.path.split("/"))
            and not any(ord(char) < 32 for char in decoded))


@dataclass
class BridgeSession:
    gateway: str
    account_id: str | None = None
    device_id: str | None = None
    access_token: str | None = field(default=None, repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    verifier: str | None = field(default=None, repr=False)
    verifier_created: float = 0
    expires_at: float = 0
    connected: bool = False
    last_error: str | None = None
    lock: Any = field(default_factory=threading.RLock, repr=False)
    activity_retired: Any = field(default=None, repr=False)
    auth_epoch: int = 0

    def scope(self) -> dict[str, str]:
        if not self.account_id:
            raise _error(401, "verified_account_required")
        return {"gateway": self.gateway, "account_id": self.account_id}

    def activity_scope_current(self, scope) -> bool | None:
        # Local visual custody only; never refresh credentials or call Core.
        # Busy authentication changes suppress the hint rather than delay work.
        if not self.lock.acquire(blocking=False):
            return None
        try:
            return self.connected and (self.gateway, self.account_id, self.device_id) == scope
        finally:
            self.lock.release()

    def _retire_activity(self):
        retire = self.activity_retired
        if retire is not None:
            try:
                retire()
            except Exception:
                pass  # An optional visual callback cannot affect authentication.

    def clear(self, *, retire: bool = True) -> None:
        if retire:
            self.auth_epoch += 1
            self._retire_activity()
        self.account_id = self.device_id = self.access_token = self.refresh_token = self.verifier = None
        self.expires_at = self.verifier_created = 0
        self.connected = False
        self.last_error = None

    def accept_tokens(self, data: dict[str, Any], *, same_account: bool = False) -> None:
        token, subject, device = data.get("access_token"), data.get("subject"), data.get("device_id")
        expires = data.get("expires_in")
        if not (isinstance(token, str) and token and isinstance(subject, str) and subject.startswith("usr_")
                and isinstance(device, str) and device and isinstance(expires, (int, float)) and 0 < expires <= 86400):
            raise _error(502, "invalid_device_token_response")
        if same_account and (subject != self.account_id or device != self.device_id):
            self.clear()
            raise _error(401, "device_identity_changed")
        if (self.account_id, self.device_id) != (subject, device):
            self.auth_epoch += 1
            self._retire_activity()
        self.account_id, self.device_id, self.access_token = subject, device, token
        self.refresh_token = data.get("refresh_token") if isinstance(data.get("refresh_token"), str) else None
        self.expires_at = time.time() + expires
        self.connected, self.last_error = True, None
        self.verifier = None


def create_bridge_app(*, transport: httpx.BaseTransport | None = None,
                      local_core: str | None = None) -> FastAPI:
    if local_core is not None:
        from app.core.local_mode import is_local
        address = urlsplit(local_core)
        if (not is_local() or address.scheme != "http" or address.hostname != "127.0.0.1"
                or address.username or address.password or address.path or address.query or address.fragment):
            raise ValueError("local_build_connection_invalid")
    token = secrets.token_urlsafe(32) if local_core is not None else _expected_token()
    native_owned = local_core is None and os.environ.get("BRIDGE_AUTH_OWNER") == "native"
    native_token = os.environ.get("BRIDGE_NATIVE_TOKEN", "")
    if native_owned and len(native_token) < 32:
        raise ValueError("native_bridge_secret_required")
    session = BridgeSession("http://127.0.0.1" if local_core is not None else _gateway())
    local = local_core is not None or hosted._is_local()
    allowed_origins = local_browser_origins() if local else {
        "https://app.supymem.com", "https://app.staging.supymem.com",
        "http://127.0.0.1:3100", "http://localhost:3100", "tauri://localhost", "http://tauri.localhost",
    }
    local_review = native_owned and os.environ.get("SUPYMEM_LOCAL_REVIEW") == "enabled"
    offline.recover_interrupted()
    app = FastAPI(title="Supymem Local Bridge", version="0.2.0")
    app.state.bridge_session = session
    app.state.model_http = None

    @app.on_event("startup")
    async def start_model_transport():
        from desktop_runtime.agent_status import start_probes
        start_probes()
        from desktop_runtime.cancellable_http import CancellableHTTP
        app.state.model_http = CancellableHTTP(transport=transport)
        if local_core is None and hosted._is_local():
            from app.core.local_owner import _ensure_proxy_secret, ensure_owner
            from app.admin.authn import CallerContext
            from app.build.device_orders import device_caller
            from core.local_security import _proxy_secret

            owner = ensure_owner()
            _ensure_proxy_secret()
            caller = CallerContext(sub=owner.id, email=owner.email or "", role="user",
                                   path="", method="", exp=0, nonce="")
            session.account_id = owner.id
            session.device_id = device_caller(caller).device_id
            session.access_token = _proxy_secret().decode("ascii")
            session.expires_at = time.time() + 3600
            session.connected = True

    @app.exception_handler(Exception)
    async def _unhandled(_request, _exc):  # 5xx from a bridge handler: log + queue a closed event
        report_error("desktop_bridge", "handle_request", label="Mac uncaught_exception")
        return JSONResponse(status_code=500, content={"detail": {"error": {"code": "bridge_internal_error"}}})

    def request_gateway(method: str, path: str, *, body: Any = None,
                        bearer: str | None = None, key: str | None = None,
                        content: bytes | None = None) -> httpx.Response:
        headers = {}
        if local_core is not None:
            from core.local_security import _proxy_secret
            if not session.access_token or not secrets.compare_digest(
                    session.access_token.encode(), _proxy_secret()):
                raise PermissionError("native_worker_account_changed")
            headers["X-Local-Proxy-Secret"] = session.access_token
        elif bearer:
            headers.update(auth_headers(bearer))
        if key:
            headers["Idempotency-Key"] = key
        if content is not None:
            headers["Content-Type"] = "application/zip"
        timeout = 60 if content is not None else 40 if path.endswith("/desktop-vision") else 15
        with httpx.Client(transport=transport, timeout=timeout,
                          follow_redirects=False, trust_env=False) as client:
            response = client.request(method, _api_url(local_core or session.gateway, path), json=body if content is None else None,
                                      content=content, headers=headers)
            if local_core is not None and (not session.access_token or not secrets.compare_digest(
                    session.access_token.encode(), _proxy_secret())):
                raise PermissionError("native_worker_account_changed")
            return response

    def auth_headers(bearer: str) -> dict[str, str]:
        if local_core is not None or hosted._is_local():
            return {"X-Local-Proxy-Secret": bearer}
        return {"Authorization": f"Bearer {bearer}"}

    def token_request(path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = request_gateway("POST", path, body=body)
        except httpx.HTTPError:
            session.connected, session.last_error = False, "gateway_unreachable"
            raise _error(503, "gateway_unreachable") from None
        if not response.is_success:
            session.connected, session.last_error = False, "device_authentication_failed"
            raise _error(response.status_code if response.status_code in {400, 401, 403, 429} else 502,
                         "device_authentication_failed")
        try:
            value = response.json()
        except ValueError:
            raise _error(502, "invalid_device_token_response") from None
        if not isinstance(value, dict):
            raise _error(502, "invalid_device_token_response")
        return value

    def access_token() -> str:
        session.scope()
        if local_core is not None and not session.access_token:
            raise _error(401, "local_owner_required")
        if hosted._is_local() and session.access_token:
            from core.local_security import _proxy_secret
            if not secrets.compare_digest(session.access_token.encode(), _proxy_secret()):
                session.clear()
                raise _error(401, "local_owner_required")
            if local_core is not None:
                return session.access_token
            session.expires_at = time.time() + 3600
        if not native_owned and session.expires_at <= time.time() + 15 and session.refresh_token:
            try:
                data = token_request("/auth/device/refresh", {
                    "device_id": session.device_id, "refresh_token": session.refresh_token,
                })
                session.accept_tokens(data, same_account=True)
            except HTTPException:
                session.access_token = session.refresh_token = None
                session.connected = False
                raise
        if not session.access_token or session.expires_at <= time.time():
            session.connected = False
            raise _error(401, "device_authentication_required")
        return session.access_token

    def replay_outbox() -> int:
        scope = session.scope()
        bearer = access_token()
        count = 0
        for item in offline.pending(**scope):
            key = item["idempotency_key"]
            if not offline.claim(key, **scope):
                continue
            try:
                response = request_gateway(item["method"], item["path"], body=item["body"], bearer=bearer, key=key)
            except httpx.HTTPError:
                offline.record_result(key, **scope, state="uncertain", error="delivery_outcome_unknown")
                session.connected, session.last_error = False, "delivery_outcome_unknown"
                log.warning("bridge_delivery_uncertain", extra={"account_id": session.account_id, "idempotency_key": key})
                break
            semantic_failure = False
            if response.is_success and response.content:
                try:
                    data = response.json()
                    semantic_failure = isinstance(data, dict) and (data.get("ok") is False or bool(data.get("error")))
                except ValueError:
                    pass  # A successful empty/text response is also a valid API acknowledgment.
            if response.is_success and not semantic_failure:
                offline.record_result(key, **scope, state="delivered", status=response.status_code)
                session.connected, session.last_error = True, None
                log.info("bridge_delivery_ack", extra={"account_id": session.account_id,
                         "idempotency_key": key, "upstream_status": response.status_code})
                count += 1
            else:
                state = ("auth_required" if response.status_code in {401, 403} else
                         "uncertain" if response.status_code >= 500 else "rejected")
                offline.record_result(key, **scope, state=state, status=response.status_code,
                                      error="upstream_rejected" if state == "rejected" else state)
                session.last_error = state
                if state == "auth_required":
                    session.connected = False
                    session.access_token = session.refresh_token = None
                break
        return count

    if not local and native_owned and os.environ.get("SUPYMEM_LINKEDIN_SEARCH", "1") != "0" and "PYTEST_CURRENT_TEST" not in os.environ:
        from desktop_runtime import linkedin_search
        linkedin_search.start(
            lambda method, path, body: request_gateway(method, path, body=body, bearer=access_token()),
            lambda: session.account_id if session.connected else None)

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        from core.request_id import HEADER, resolve_request_id
        rid = resolve_request_id(request.headers.get(HEADER))
        origins = request.headers.getlist("origin")
        if origins and (len(origins) != 1 or origins[0] not in allowed_origins):
            return JSONResponse(status_code=403, content={"error": {"code": "bridge_origin_denied",
                "message": "Open your desktop app on this machine, then retry."}}, headers={HEADER: rid})
        path = request.scope["path"]
        if path.startswith("/native/"):
            supplied = request.headers.get("x-native-token", "")
            if origins or not native_owned or not secrets.compare_digest(supplied.encode(), native_token.encode()):
                return JSONResponse(status_code=401, content={"error": {"code": "native_auth_required"}}, headers={HEADER: rid})
        elif path in {"/health", "/ready", "/config"} or (local and path == "/local-ready"):
            server = request.scope.get("server")
            if not valid_loopback_host(request.headers.getlist("host"), server[1] if server else None):
                return JSONResponse(status_code=403, content={"error": {"code": "bridge_host_denied",
                    "message": "Quit and reopen your app, then retry."}}, headers={HEADER: rid, "Cache-Control": "no-store"})
        else:
            bearer = request.headers.get("authorization", "")
            supplied = request.headers.get("x-bridge-token") or (bearer[7:] if bearer.lower().startswith("bearer ") else "")
            if not secrets.compare_digest(supplied.encode(), token.encode()):
                return JSONResponse(status_code=401, content={"error": {"code": "bridge_unauthorized"}}, headers={HEADER: rid})
        response = await call_next(request)
        response.headers[HEADER] = rid
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/health")
    @app.get("/ready")
    def health():
        payload = {"status": "ok", "ready": True, "service": "bridge", "connected": session.connected}
        if hosted._is_local():
            payload["desktop_instance"] = os.environ.get("SUPYMEM_DESKTOP_INSTANCE_ID")
        return payload

    if local:
        @app.get("/local-ready")
        def local_ready(request: Request):
            from core.local_readiness import readiness_headers

            headers = readiness_headers(request.headers.get("x-local-ready-nonce"))
            return Response(status_code=204 if headers is not None else 403, headers=headers)

    @app.get("/config")
    def config():
        payload = hosted.window_supymem()
        payload.pop("bridgeToken", None)
        return payload

    @app.get("/status")
    def status():
        refresh_idle_worker()
        with session.lock:
            return {"connected": session.connected, "last_error": session.last_error,
                    "account_id": session.account_id, "device_id": session.device_id,
                    "pending": len(offline.pending(**session.scope())) if session.account_id else 0}

    @app.post("/native/clear")
    def native_clear():
        with session.lock:
            session.clear()
            clear_crash_owner()
        with worker_lock:
            if active_worker["worker"]:
                active_worker["worker"].cancel()
        return {"ok": True}

    @app.post("/native/session")
    def native_session(body: dict[str, Any]):
        with session.lock:
            bearer, subject = body.get("access_token"), body.get("account_id")
            device, expires = body.get("device_id"), body.get("expires_at")
            continuing = (session.connected and session.expires_at > time.time()
                          and (session.account_id, session.device_id) == (subject, device))
            # Remove prior credentials under the lock before verifying new ones.
            # Same live device renewal retires custody only if admission fails.
            session.clear(retire=not continuing)
            clear_crash_owner()
            if (not isinstance(bearer, str) or not bearer or len(bearer) > 16384
                    or not isinstance(subject, str) or not (subject.startswith("usr_") or
                        hosted._is_local() and subject.startswith("per_") or
                        local_review and session.gateway == "http://127.0.0.1:3200" and subject == "founder")
                    or not isinstance(device, str) or not device
                    or not isinstance(expires, (int, float)) or isinstance(expires, bool)
                    or not time.time() < expires <= time.time() + 3600
                    or "refresh_token" in body):
                if continuing:
                    session.clear()
                raise _error(400, "invalid_native_session")
            try:
                if hosted._is_local():
                    from core.local_security import _proxy_secret
                    from app.core.local_owner import ensure_owner
                    identity = ({"authenticated": True, "subject": ensure_owner().id}
                                if secrets.compare_digest(bearer.encode(), _proxy_secret()) else {})
                else:
                    response = request_gateway("GET", "/auth/session", bearer=bearer)
                    identity = response.json() if response.is_success else {}
            except (httpx.HTTPError, ValueError):
                if continuing:
                    session.clear()
                raise _error(503, "gateway_unreachable") from None
            if (not isinstance(identity, dict) or identity.get("authenticated") is not True
                    or identity.get("subject") != subject):
                if continuing:
                    session.clear()
                raise _error(401, "native_session_identity_mismatch")
            session.account_id, session.device_id = subject, device
            session.access_token, session.expires_at = bearer, expires
            session.connected = True
            save_crash_owner(subject, device)
            return {"ok": True, "account_id": subject, "device_id": device}

    @app.post("/native/telemetry/runtime")
    def native_runtime_heartbeat(body: NativeRuntimeHeartbeat):
        """Forward a native-owned process tick through the signed user session."""
        if local:
            refresh_idle_worker()
            return {"ok": True}
        with session.lock:
            bearer = access_token()
            subject = session.account_id
        payload = {
            "event": "desktop.process_heartbeat",
            "event_id": body.event_id,
            "context_subject": subject,
            "workspace_id": None,
            "venture_id": None,
            "props": {
                "feature": "settings",
                "platform": "macos",
                "app_version": body.app_version,
                "session_id": body.session_id,
            },
        }
        try:
            response = request_gateway("POST", "/api/telemetry", body=payload, bearer=bearer)
        except httpx.HTTPError:
            raise _error(503, "runtime_telemetry_unavailable") from None
        if response.status_code in {401, 403}:
            with session.lock:
                session.connected = False
            raise _error(401, "device_authentication_required")
        if not response.is_success:
            raise _error(502, "runtime_telemetry_rejected")
        # The native-owned heartbeat is also the bounded retry cadence. A
        # failed crash receipt stays local and cannot block the heartbeat.
        with session.lock:
            device = session.device_id
        if subject and device:
            try:
                pending_crashes = pending_crash_summaries(subject, device)[:10]
            except OSError:
                pending_crashes = []
            for path, crash_payload in pending_crashes:
                try:
                    crash_response = request_gateway("POST", "/api/telemetry", body=crash_payload, bearer=bearer)
                except httpx.HTTPError:
                    break
                try:
                    receipt = crash_response.json() if crash_response.is_success else None
                except ValueError:
                    receipt = None
                if (not isinstance(receipt, dict) or not isinstance(receipt.get("id"), str)
                        or not receipt["id"].startswith("usage_")):
                    break
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    break  # The same stable receipt can be retried next tick.
        refresh_idle_worker()
        return {"ok": True}

    def native_order_gateway(venture_id: str, order_id: str):
        from desktop_runtime.device_gateway import WorkOrderGateway
        with session.lock:
            access_token()
            identity = (session.gateway, session.account_id, session.device_id)
            auth_epoch = session.auth_epoch
        def owner_current():
            return ((session.gateway, session.account_id, session.device_id) == identity
                    and session.auth_epoch == auth_epoch and session.connected)
        def bound_request(method, path, body):
            with session.lock:
                if not owner_current():
                    raise PermissionError("native_worker_account_changed")
                bearer = access_token()
            try:
                response = request_gateway(method, path, body=body, bearer=bearer)
                if len(response.content) > 262144:
                    raise ValueError("native_worker_response_bounds")
                payload = response.json()
            except (httpx.HTTPError, ValueError):
                raise RuntimeError("native_worker_request_unconfirmed") from None
            with session.lock:
                if not owner_current():
                    raise PermissionError("native_worker_account_changed")
                if response.status_code == 401:
                    session.connected = False
                    session._retire_activity()
            if not response.is_success or not isinstance(payload, dict):
                # Keep the status and the server's conflict code so a definite 4xx
                # (nothing happened) is told apart from an unknown outcome.
                detail = payload.get("detail") if isinstance(payload, dict) else None
                code = detail.get("code") if isinstance(detail, dict) else None
                raise UpstreamRejected(response.status_code, code if isinstance(code, str) else "")
            return payload
        def bound_cancellable_request(method, path, body, *, stop, deadline):
            owned = f"/api/ventures/{venture_id}/device-work-orders/{order_id}"
            if (method, path) not in {("GET", owned), ("GET", owned + "/authority"),
                                      ("POST", owned + "/ai-step"),
                                      ("POST", owned + "/browser-ai-call"),
                                      ("POST", owned + "/human-pause"),
                                      ("GET", owned + "/human-resume"),
                                      ("POST", owned + "/human-resume/ack"),
                                      ("POST", owned + "/human-pause/clear")}:
                raise PermissionError("native_model_request_scope_invalid")
            if stop.is_set():
                from desktop_runtime.cancellable_http import ModelRequestStopped
                raise ModelRequestStopped()
            with session.lock:
                if not owner_current():
                    raise PermissionError("native_worker_account_changed")
                bearer = access_token()
            controller = app.state.model_http
            if controller is None:
                raise RuntimeError("native_model_transport_unavailable")
            try:
                response = controller.request(method, _api_url(local_core or identity[0], path), body=body,
                    headers=auth_headers(bearer), stop=stop, deadline=deadline)
                if len(response.content) > 262144:
                    raise ValueError("native_worker_response_bounds")
                payload = response.json()
            except (httpx.HTTPError, ValueError):
                raise RuntimeError("native_worker_request_unconfirmed") from None
            with session.lock:
                if not owner_current():
                    raise PermissionError("native_worker_account_changed")
                if response.status_code == 401:
                    session.connected = False
            if not response.is_success or not isinstance(payload, dict):
                detail = payload.get("detail") if isinstance(payload, dict) else None
                code = detail.get("code") if isinstance(detail, dict) else None
                raise UpstreamRejected(response.status_code, code if isinstance(code, str) else "")
            return payload
        def bound_upload(path, blob):
            with session.lock:
                if not owner_current():
                    raise PermissionError("native_worker_account_changed")
                bearer = access_token()
            try:
                response = request_gateway("PUT", path, bearer=bearer, content=blob)
                if len(response.content) > 262144:
                    raise ValueError("native_worker_response_bounds")
                payload = response.json()
            except (httpx.HTTPError, ValueError):
                raise RuntimeError("native_coding_source_upload_unconfirmed") from None
            with session.lock:
                if not owner_current():
                    raise PermissionError("native_worker_account_changed")
                if response.status_code == 401:
                    session.connected = False
            if not response.is_success or not isinstance(payload, dict):
                raise PermissionError("native_coding_source_upstream_rejected")
            return payload
        def bound_download(path, *, stop, deadline, max_bytes):
            if path != f"/api/ventures/{venture_id}/device-work-orders/{order_id}/pagebuilder-reference":
                raise PermissionError("native_reference_request_scope_invalid")
            with session.lock:
                if not owner_current():
                    raise PermissionError("native_worker_account_changed")
                bearer = access_token()
            controller = app.state.model_http
            if controller is None:
                raise RuntimeError("native_model_transport_unavailable")
            try:
                response = controller.request("GET", _api_url(local_core or identity[0], path), body=None,
                    headers={**auth_headers(bearer), "Accept": "application/zip"},
                    stop=stop, deadline=deadline, max_bytes=max_bytes)
            except (httpx.HTTPError, ValueError):
                raise RuntimeError("native_reference_download_unconfirmed") from None
            with session.lock:
                if not owner_current():
                    raise PermissionError("native_worker_account_changed")
                if response.status_code == 401:
                    session.connected = False
            if (not response.is_success
                    or response.headers.get("Content-Type", "").split(";", 1)[0] != "application/zip"):
                raise PermissionError("native_reference_upstream_rejected")
            return response.content
        return WorkOrderGateway(venture_id=venture_id, order_id=order_id, device_id=identity[2],
                                request=bound_request, upload=bound_upload,
                                cancellable_request=bound_cancellable_request, download=bound_download,
                                account_id=identity[1], local_review_actor=("per_local000000"
                                    if local_review and identity[0] == "http://127.0.0.1:3200"
                                    and identity[1] == "founder" else None))

    # Private native owner only. No browser/client-supplied bearer, URL, process
    # argv or work-order manifest crosses this boundary.
    app.state.native_order_gateway = native_order_gateway
    worker_lock = threading.Lock()
    active_worker = {"worker": None, "thread": None, "scope": None}
    app.state.local_active_worker = active_worker
    def retire_worker_activity():
        worker = active_worker["worker"]
        if worker is not None:
            worker.stop_product_preview()
            worker.cancel()
    session.activity_retired = retire_worker_activity

    def worker_snapshot(worker):
        preview = worker.product_preview_status()
        receipt = dict(worker.last)
        if preview is not None:
            receipt["product_preview"] = preview
        watch = worker.watch.snapshot()
        return {**receipt, "watch": watch} if watch else receipt

    def native_worker_scope():
        with session.lock:
            try:
                access_token()
            except HTTPException:
                retire_worker_activity()
                raise
            if not session.device_id:
                raise _error(401, "authenticated_device_required")
            return session.gateway, session.account_id, session.device_id

    def refresh_idle_worker():
        with worker_lock:
            worker, thread = active_worker["worker"], active_worker["thread"]
            if (worker is None or thread is None or thread.is_alive()
                    or worker.closed and worker.last.get("state") == "stopped"
                    and worker.last.get("credits_reconciled") is True):
                return
            venture_id, order_id = worker.gateway.venture_id, worker.gateway.order_id
        try:
            native_order_status(venture_id, order_id)
        except (HTTPException, ValueError, PermissionError, RuntimeError, OSError, sqlite3.Error):
            pass

    @app.get("/native/desktop/permissions")
    def native_desktop_permissions():
        """Local TCC status is useful before account sign-in; native token is required."""
        from desktop_runtime.peekaboo_driver import PeekabooDriver
        try:
            return {"permissions": PeekabooDriver.permissions_status()}
        except (ValueError, RuntimeError):
            raise _error(503, "native_desktop_permissions_unavailable") from None

    @app.post("/native/desktop/permissions/request")
    def native_desktop_permission_request(payload: dict):
        kind = payload.get("kind") if isinstance(payload, dict) else None
        if kind not in {"screen-recording", "accessibility", "event-synthesizing"}:
            raise _error(400, "native_desktop_permission_kind_invalid")
        from desktop_runtime.peekaboo_driver import PeekabooDriver
        try:
            return {"permissions": PeekabooDriver.request_permission(kind)}
        except (ValueError, RuntimeError):
            raise _error(503, "native_desktop_permission_request_unavailable") from None

    @app.get("/native/desktop/apps")
    def native_desktop_apps(bundle_id: str | None = None):
        """Read-only inventory for reviewed, exact-app desktop tasks.

        Only the signed shell may call this private route. The browser never
        receives a Peekaboo path, local socket, credential, or raw tool handle.
        """
        native_worker_scope()
        from desktop_runtime.peekaboo_driver import PeekabooDriver
        try:
            apps = PeekabooDriver.running_apps()
        except (ValueError, RuntimeError):
            raise _error(503, "native_desktop_inventory_unavailable") from None
        installed = PeekabooDriver.installed_apps()
        result = {"items": [{"name": app.name, "bundle_id": app.bundle_id,
                            "pid": app.pid, "process_start_identity": app.process_start_identity}
                           for app in apps],
                  "installed": [{"name": app.name, "bundle_id": app.bundle_id}
                                for app in installed],
                  "default_browser_bundle": PeekabooDriver.default_browser_bundle(installed)}
        try:
            result["permissions"] = PeekabooDriver.permissions_status()
        except (ValueError, RuntimeError):
            result["permissions"] = {"status_unavailable": True}
        if bundle_id is not None:
            chosen = next((item for item in apps if item.bundle_id == bundle_id), None)
            if chosen is None:
                raise _error(409, "native_desktop_app_not_running")
            try:
                result["windows"] = PeekabooDriver.running_windows(chosen)
            except (ValueError, RuntimeError):
                raise _error(503, "native_desktop_window_inventory_unavailable") from None
        return result

    @app.post("/native/desktop/apps/launch")
    def native_desktop_app_launch(payload: dict):
        """Cold launches need a reviewed creation profile, unavailable in beta."""
        native_worker_scope()
        bundle_id = payload.get("bundle_id") if isinstance(payload, dict) else None
        ensure_window = payload.get("ensure_window", False) if isinstance(payload, dict) else False
        if not isinstance(bundle_id, str) or len(bundle_id) > 200:
            raise _error(400, "native_desktop_bundle_invalid")
        if type(ensure_window) is not bool:
            raise _error(400, "native_desktop_window_request_invalid")
        # A signed session establishes identity, not authority to open an app
        # or create a window. Existing-window work orders cannot grant either.
        # Refuse even a raw shell RPC before calling any native launch method.
        raise _error(409, "desktop_app_launch_unavailable")

    @app.get("/native/coding-agents")
    def native_coding_agents():
        from desktop_runtime.agent_status import statuses
        return {"agents": [{key: agent[key] for key in ("name", "installed", "signed_in", "subscription_login", "available")}
                           for agent in statuses()]}

    @app.post("/native/device-work-orders/{venture_id}/{order_id}/soft-stop")
    def native_loop_soft_stop(venture_id: str, order_id: str):
        scope = native_worker_scope()
        with worker_lock:
            worker, thread = active_worker["worker"], active_worker["thread"]
            if (not worker or not thread or not thread.is_alive()
                    or active_worker["scope"] != scope
                    or worker.gateway.order_id != order_id
                    or worker.gateway.venture_id != venture_id):
                raise _error(404, "native_execution_not_found")
            order = worker.gateway.read()
            if not order.get("manifest", {}).get("coding_loop_verification"):
                raise _error(409, "coding_loop_stop_unavailable")
            worker.stop_after_iteration.set()
            worker.progress(worker.last["state"], stop_after_iteration=True)
            return dict(worker.last)

    def run_requested_worker(worker, scope):
        from desktop_runtime import device_journal
        from desktop_runtime.device_worker import DeviceWorker
        worker.run()
        if not hosted._is_local():
            return
        try:
            if native_worker_scope() != scope:
                worker.cancel()
                return
            order = worker.next_requested_build()
            if order is None:
                return
            gateway = native_order_gateway(order["venture_id"], order["id"])
            with worker_lock:
                if worker.cancelled.is_set() or active_worker["worker"] is not worker:
                    return
                device_journal.create(scope, order["venture_id"], order["id"])
                following = DeviceWorker(gateway,
                    lambda receipt: device_journal.save(scope, order["venture_id"], order["id"], receipt),
                    activity_current=lambda: session.activity_scope_current(scope))
                following.requested_plan_order_id = worker.gateway.order_id
                active_worker["worker"] = following
            following.run()
        except (HTTPException, ValueError, PermissionError, RuntimeError, OSError, sqlite3.Error):
            worker.progress(worker.last["state"], continuation_message=
                "Open your venture build result and choose Start to continue from your saved plan. If your tool needs setup, open Settings > Keys and connections > Coding tools, reconnect it, then choose Start.")

    @app.post("/native/device-work-orders/{venture_id}/{order_id}/start")
    def native_order_start(venture_id: str, order_id: str, request: Request):
        from desktop_runtime import device_journal
        from desktop_runtime.device_worker import DeviceWorker
        if not hosted._is_local() and os.environ.get("SUPYMEM_NATIVE_BROWSER_ENABLED") != "1":
            raise _error(503, "native_browser_not_enabled")
        scope = native_worker_scope()
        refresh_idle_worker()
        gateway = native_order_gateway(venture_id, order_id)
        cua_socket = request.headers.get("x-supymem-cua-socket")
        cua_host_pid = request.headers.get("x-supymem-cua-host-pid")
        cua_protocol = request.headers.get("x-supymem-cua-protocol")
        with worker_lock:
            worker, thread = active_worker["worker"], active_worker["thread"]
            if worker and (thread is not None and thread.is_alive() or not worker.closed):
                raise _error(409, "native_executor_busy_or_unreconciled")
            try:
                order = gateway.read()  # Verify current owner before persisting local identity.
            except (ValueError, PermissionError, RuntimeError):
                raise _error(409, "native_order_verification_unavailable") from None
            manifest = order.get("manifest") or {}
            if (manifest.get("coding_loop_verification") is not None
                    or manifest.get("coding_verification") is not None):
                cua_socket = cua_host_pid = cua_protocol = None
            else:
                from desktop_runtime.cua_session import OBSERVATION_PROTOCOL
                if cua_protocol not in {None, OBSERVATION_PROTOCOL} or cua_protocol and not cua_socket:
                    raise _error(409, "desktop_cua_protocol_unsupported")
                if cua_socket is not None:
                    if (not cua_socket.startswith("/private/tmp/supymem-cua-")
                            or not cua_socket.endswith("/driver.sock")
                            or not cua_host_pid or not cua_host_pid.isdecimal()
                            or not 0 < int(cua_host_pid) < 2**31):
                        raise _error(409, "desktop_cua_host_invalid")
            descriptor = (order.get("manifest") or {}).get("coding_loop_verification") or {}
            # Existing Start/Status/Cancel also own previews on a settled coding order.
            if order.get("state") == "stopped" and descriptor.get("phase") == "implement":
                matching = (worker is not None and active_worker["scope"] == scope
                    and worker.gateway.order_id == order_id and worker.gateway.venture_id == venture_id)
                local = device_journal.read(scope, venture_id, order_id)
                if not isinstance(local, dict):
                    raise _error(409, "native_product_preview_unavailable")
                if not matching:
                    if worker is not None:
                        if not worker.closed:
                            raise _error(409, "native_executor_busy_or_unreconciled")
                        preview = worker.stop_product_preview()
                        if (preview and preview.get("status") in {"ready", "failed", "expired"}
                                and preview.get("processes_stopped") is not True):
                            raise _error(409, "native_product_preview_cleanup_unconfirmed")
                    gateway.execution_id = (local.get("evidence") or {}).get("execution_id")
                    worker = DeviceWorker(gateway, lambda receipt: device_journal.save(scope, venture_id, order_id, receipt),
                        activity_current=lambda: session.activity_scope_current(scope))
                    worker.last, worker.closed = dict(local), True
                active_worker.update(worker=worker, thread=thread if matching else None, scope=scope)
                try:
                    worker.start_product_preview(order)
                except (ValueError, OSError, RuntimeError):
                    raise _error(409, "native_product_preview_unavailable") from None
                return dict(worker.last)
            if order.get("state") == "stopped" and descriptor:
                raise _error(409, "native_product_preview_unavailable")
            if worker:
                preview = worker.stop_product_preview()
                if (preview and preview.get("status") in {"ready", "failed", "expired"}
                        and preview.get("processes_stopped") is not True):
                    raise _error(409, "native_product_preview_cleanup_unconfirmed")
            try:
                if order.get("state") == "waiting_for_device":
                    device_journal.create(scope, venture_id, order_id, resume=True)
                else:
                    device_journal.create(scope, venture_id, order_id)
            except (OSError, sqlite3.Error, ValueError):
                raise _error(409, "native_journal_unavailable") from None
            worker = DeviceWorker(
                gateway, lambda receipt: device_journal.save(scope, venture_id, order_id, receipt),
                cua_socket=cua_socket, cua_host_pid=int(cua_host_pid) if cua_socket else None,
                cua_protocol=cua_protocol, activity_current=lambda: session.activity_scope_current(scope))
            thread = threading.Thread(target=run_requested_worker, args=(worker, scope), name="supymem-owned-browser", daemon=True)
            active_worker.update(worker=worker, thread=thread, scope=scope)
            thread.start()
            if not hosted._is_local():
                from desktop_runtime.device_resume import start_resume_monitor
                start_resume_monitor(worker, thread)
            return {"order_id": order_id, "state": "starting", "task_verified": False}

    @app.get("/native/device-work-orders/{venture_id}/{order_id}/status")
    def native_order_status(venture_id: str, order_id: str):
        from desktop_runtime import device_journal, product_receipts
        scope = native_worker_scope()
        gateway = native_order_gateway(venture_id, order_id)
        order = gateway.read()
        loop = (order.get("manifest") or {}).get("coding_loop_verification")

        def with_result_folder(receipt):
            if not (local and isinstance(loop, dict) and loop.get("phase") == "implement"
                    and order.get("state") == "stopped" and order.get("closed_at")
                    and order.get("device_id") == scope[2] and (order.get("native_stop") or {}).get("processes_stopped") is True
                    and order["native_stop"].get("task_verified") is True
                    and not order.get("stop_reconciliation_required") and not order.get("inference_reconciliation_required")
                    and receipt.get("order_id") == order_id and receipt.get("state") == "stopped"
                    and receipt.get("task_verified") is True
                    and not any(receipt.get(key) for key in ("journal_unconfirmed", "actual_usage_unconfirmed", "effect_outcome_unconfirmed"))):
                return receipt
            from core.artifact_policy import read_file
            from desktop_runtime.paths import data_root, venture_workspace_root

            slug = loop.get("workspace_slug")
            if not isinstance(slug, str) or len(slug) > 64 or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
                return receipt
            try:
                root = venture_workspace_root(slug, data_root()).resolve()
                workspace = root / slug
                if workspace.resolve(strict=True) == workspace and read_file(workspace, ".supymem-venture").decode().strip() == venture_id:
                    return {**receipt, "result_folder": str(workspace)}
            except (OSError, ValueError, RuntimeError):
                pass
            return receipt

        with worker_lock:
            worker = active_worker["worker"]
            matching = (worker and active_worker["scope"] == scope
                and worker.gateway.order_id == order_id and worker.gateway.venture_id == venture_id)
            if matching and active_worker["thread"] is not None and active_worker["thread"].is_alive():
                if order.get("state") == "cancelled":
                    worker.stop_product_preview()
                receipt = worker_snapshot(worker)
                if receipt.get("state") == "stopped":
                    receipt["state"] = "closing"
                return receipt
            receipt = device_journal.read(scope, venture_id, order_id)
            receipt_scope = scope
            if receipt is None and order.get("state") in {"cancelled", "stopped"}:
                receipt = device_journal.read_for_cancelled_recovery(
                    scope, venture_id, order_id, order.get("device_id"))
                if receipt is not None:
                    receipt_scope = (scope[0], scope[1], order["device_id"])
            if receipt and receipt_scope == scope and product_receipts.cleanup_pending(order, receipt):
                try:
                    receipt = product_receipts.reconcile(gateway, order, receipt,
                        lambda packet: device_journal.save(scope, venture_id, order_id, packet))
                except (ValueError, RuntimeError, OSError, sqlite3.Error):
                    pass
                else:
                    if matching:
                        worker.last, worker.closed = dict(receipt), True
                    return receipt
            if receipt is None and matching:
                receipt = dict(worker.last)
            accepted = False
            if receipt:
                evidence = receipt.get("evidence") or {}
                execution_id = (receipt.get("execution_id") or evidence.get("execution_id")
                    or (order.get("execution_id") if receipt.get("order_id") == order_id else None))
                native_stop = order.get("native_stop") or {}
                settled = order.get("state") in {"stopped", "cancelled"} and bool(order.get("closed_at"))
                accepted = (native_stop.get("execution_id") == execution_id
                    and native_stop.get("evidence_digest") == receipt.get("evidence_digest")
                    and native_stop.get("processes_stopped") is True)
                reviewed = order.get("reviewed_at")
                if (isinstance(execution_id, str) and execution_id == order.get("execution_id")
                        and (settled or accepted or reviewed)):
                    updated = {**receipt, "state": "stopped", "credits_reconciled": bool(settled),
                               "task_verified": receipt.get("task_verified") is True
                               and native_stop.get("task_verified") is True}
                    uncertainty = order.get("unknown_usage_reconciliation") or order.get("lease_reconciliation") or {}
                    for key in ("actual_usage_unconfirmed", "effect_outcome_unconfirmed"):
                        if uncertainty.get(key):
                            updated[key] = True
                    if reviewed:
                        updated["reconciliation_reviewed_at"] = reviewed
                        if not settled:
                            updated.update(task_verified=False, actual_usage_unconfirmed=True,
                                           effect_outcome_unconfirmed=True)
                    if updated != receipt:
                        device_journal.save(receipt_scope, venture_id, order_id, updated)
                    receipt = updated
                    if matching:
                        worker.last = dict(receipt)
                        worker.closed = True
            if matching:
                if (order.get("state") != "stopped" or not order.get("closed_at") or not accepted
                        or order.get("device_id") != scope[2] or worker.last.get("task_verified") is not True
                        or any(worker.last.get(key) for key in ("journal_unconfirmed", "actual_usage_unconfirmed", "effect_outcome_unconfirmed"))):
                    worker.stop_product_preview()
                return with_result_folder(worker_snapshot(worker))
            if receipt and (receipt.get("product_preview") or {}).get("status") == "ready":
                receipt = {**receipt, "product_preview": {**receipt["product_preview"],
                    "status": "expired", "ready": False, "stop_available": False}}
                receipt["product_preview"].pop("url", None)
        if receipt and receipt["state"] not in {"stopped", "not_started", "reconciliation_required"}:
            if order.get("state") == "waiting_for_device":
                return {**receipt, "state": "waiting_for_device",
                        "message": "Waiting for your Mac — it resumes when your Mac is awake"}
            return {**receipt, "state": "interrupted_reconciliation_required"}
        if receipt is None and local and loop and order.get("state") == "stopped" and order.get("closed_at"):
            stop = order.get("native_stop") or {}
            if (order.get("execution_id") and stop.get("execution_id") == order["execution_id"]
                    and stop.get("processes_stopped") is True and not order.get("stop_reconciliation_required")
                    and not order.get("inference_reconciliation_required")):
                verified = stop.get("task_verified") is True and stop.get("verification_kind") == "workspace_loop_v1"
                outcome = ("plan_ready" if loop.get("phase") == "plan" else "pass") if verified else "stopped"
                return with_result_folder({"order_id": order_id, "state": "stopped", "task_verified": verified,
                        "credits_reconciled": True, "evidence": {"processes_stopped": True, "outcome": outcome}})
        return with_result_folder(receipt) if receipt else {"order_id": order_id, "state": "not_started", "task_verified": False}

    @app.post("/native/device-work-orders/{venture_id}/{order_id}/source-upload")
    def native_coding_source_upload(venture_id: str, order_id: str):
        """Retry custody of one stopped order from its native-owned frozen bytes."""
        from desktop_runtime import device_journal, source_receipt
        scope = native_worker_scope()
        gateway = native_order_gateway(venture_id, order_id)
        try:
            order = gateway.read()
            local = device_journal.read(scope, venture_id, order_id)
            frozen = local.get("source_receipt") if isinstance(local, dict) else None
            if (order.get("state") != "stopped" or not isinstance(frozen, dict)
                    or local.get("state") != "stopped" or frozen.get("device_id") != scope[2]):
                raise ValueError("native_coding_source_unavailable")
            stored = gateway.store_coding_source(frozen, source_receipt.load_archive(frozen))
            updated = {**local, "source_storage_state": stored["state"],
                       "source_storage_sha256": stored["sha256"]}
            device_journal.save(scope, venture_id, order_id, updated)
            return updated
        except (ValueError, PermissionError, RuntimeError, OSError, sqlite3.Error):
            raise _error(409, "native_coding_source_upload_unconfirmed") from None

    @app.post("/native/device-work-orders/{venture_id}/{order_id}/app-navigation-result")
    def native_app_navigation_result(venture_id: str, order_id: str, body: dict[str, Any]):
        """One signed-shell route readback for an already claimed self-navigation."""
        scope = native_worker_scope()
        if set(body) != {"observed_url"}:
            raise _error(400, "native_navigation_result_invalid")
        with worker_lock:
            worker = active_worker["worker"]
            if (not worker or active_worker["scope"] != scope
                    or worker.gateway.order_id != order_id
                    or worker.gateway.venture_id != venture_id):
                raise _error(404, "native_execution_not_found")
            if not worker.submit_app_navigation_result(body["observed_url"]):
                raise _error(409, "native_navigation_result_rejected")
        return {"order_id": order_id, "state": "result_received"}

    @app.post("/native/device-work-orders/{venture_id}/{order_id}/cancel")
    def native_order_cancel(venture_id: str, order_id: str):
        scope = native_worker_scope()
        with worker_lock:
            worker = active_worker["worker"]
            if (not worker or active_worker["scope"] != scope
                    or order_id not in {worker.gateway.order_id, getattr(worker, "requested_plan_order_id", None)}
                    or worker.gateway.venture_id != venture_id):
                raise _error(404, "native_execution_not_found")
            if worker.closed and worker.last.get("product_preview"):
                worker.stop_product_preview()
                return worker_snapshot(worker)
            worker.stop_product_preview()
            worker.cancel()
        return {"order_id": order_id, "state": "cancellation_requested"}

    def running_worker():
        worker, thread = active_worker["worker"], active_worker["thread"]
        preview = (worker.last.get("product_preview") or {}) if worker is not None else {}
        return worker if worker is not None and (thread is not None and thread.is_alive()
            or preview.get("stop_available") is True) else None

    @app.get("/native/emergency-stop/status")
    def native_emergency_stop_status():
        """Is any device task running? Local only: no account, no Core."""
        with worker_lock:
            worker = running_worker()
            return {"active": worker is not None,
                    "order_id": worker.gateway.order_id if worker else None,
                    "venture_id": worker.gateway.venture_id if worker else None,
                    "stopped_by_user": bool(worker and worker.stopped_by_user)}

    @app.post("/native/emergency-stop")
    def native_emergency_stop(body: dict[str, Any] | None = None):
        """One-press Stop for whatever device task runs. Works signed out or with Core down."""
        source = (body or {}).get("source")
        source = source if source in {"app", "chat", "cursor", "menu", "shortcut"} else "app"
        with worker_lock:
            worker = running_worker()
            if worker is None:
                return {"stopped": False, "active": False}
            preview = worker.stop_product_preview()
            if worker.closed:
                return {"stopped": bool(preview and preview.get("processes_stopped") is True),
                    "active": True, "order_id": worker.gateway.order_id, "product_preview": preview}
            result = worker.emergency_stop(source)
        return {"stopped": True, "active": True, **result}

    @app.post("/native/device-work-orders/{venture_id}/{order_id}/continue")
    def native_order_continue(venture_id: str, order_id: str):
        """Resume after a Needs-you founder handoff in the owned browser."""
        scope = native_worker_scope()
        with worker_lock:
            worker = active_worker["worker"]
            if (not worker or active_worker["scope"] != scope or worker.gateway.order_id != order_id
                    or worker.gateway.venture_id != venture_id):
                raise _error(404, "native_execution_not_found")
            if not worker.continue_founder_handoff():
                raise _error(409, "native_continue_not_available")
            return dict(worker.last)

    @app.post("/native/device-work-orders/{venture_id}/{order_id}/handoff")
    def native_order_handoff(venture_id: str, order_id: str):
        """Founder-clicked open of the headed owned browser for a Needs-you pause."""
        scope = native_worker_scope()
        with worker_lock:
            worker = active_worker["worker"]
            if (not worker or active_worker["scope"] != scope or worker.gateway.order_id != order_id
                    or worker.gateway.venture_id != venture_id):
                raise _error(404, "native_execution_not_found")
            if not worker.request_founder_handoff():
                raise _error(409, "native_handoff_not_available")
            return dict(worker.last)

    @app.post("/native/device-work-orders/{venture_id}/{order_id}/reconcile")
    def native_order_reconcile(venture_id: str, order_id: str):
        # An unhandled error here surfaced as a bare 5xx
        # (native_order_bridge_server_error). Reads of the order or journal can
        # fail transiently while the gateway is unreachable or the bridge is
        # still warming; report that as a retryable, coded refusal.
        try:
            return _native_order_reconcile(venture_id, order_id)
        except HTTPException:
            raise
        except (ValueError, PermissionError, KeyError, TypeError, AttributeError):
            raise _error(409, "cu_reconcile_unavailable") from None
        except (RuntimeError, OSError, sqlite3.Error, httpx.HTTPError):  # PermissionError is caught above
            raise _error(503, "cu_reconcile_unavailable") from None

    def _native_order_reconcile(venture_id: str, order_id: str):
        """Reconcile only an explicitly supported saved native outcome."""
        from desktop_runtime import device_journal
        scope = native_worker_scope()
        gateway = native_order_gateway(venture_id, order_id)
        order = gateway.read()
        with worker_lock:
            running_this_order = (active_worker["worker"] is not None
                and active_worker["worker"].gateway.order_id == order_id
                and active_worker["thread"] is not None
                and active_worker["thread"].is_alive())
            manifest = order.get("manifest") or {}
            if manifest.get("coding_loop_verification") or manifest.get("deploy_verification"):
                from desktop_runtime.product_receipts import reconcile
                receipt = device_journal.read(scope, venture_id, order_id)
                if running_this_order or not isinstance(receipt, dict):
                    raise _error(409, "native_product_receipts_unavailable")
                try:
                    return reconcile(gateway, order, receipt,
                        lambda packet: device_journal.save(scope, venture_id, order_id, packet))
                except (ValueError, PermissionError, RuntimeError):
                    raise _error(409, "native_product_receipts_unconfirmed") from None
        receipt = device_journal.read(scope, venture_id, order_id)
        receipt_scope = scope
        if receipt is None and order.get("state") == "cancelled":
            original_device = order.get("device_id")
            receipt = device_journal.read_for_cancelled_recovery(scope, venture_id, order_id,
                                                                 original_device)
            if receipt is not None:
                receipt_scope = (scope[0], scope[1], original_device)
        evidence = receipt.get("evidence", {}) if isinstance(receipt, dict) else {}
        inventory = evidence.get("inventory", {}) if isinstance(evidence, dict) else {}
        execution_id = evidence.get("execution_id")
        evidence_digest = receipt.get("evidence_digest") if isinstance(receipt, dict) else None
        coding = evidence.get("coding", {}) if isinstance(evidence, dict) else {}
        # A prior local driver rejected this exact action before calling
        # Peekaboo. The consumed server admission still needs an explicit
        # no-dispatch receipt and billed Jev usage before a new order may run.
        # Never apply this recovery to an ambiguous native I/O result.
        invocations = order.get("invocations") if isinstance(order, dict) else None
        invocation_id = receipt.get("invocation_id") if isinstance(receipt, dict) else None
        invocation = (invocations.get(invocation_id) if isinstance(invocations, dict)
                      and isinstance(invocation_id, str) else None)
        effect = invocation.get("effect") if isinstance(invocation, dict) else None
        native_stop = order.get("native_stop") if isinstance(order, dict) else None
        driver_code = receipt.get("driver_failure_code") if isinstance(receipt, dict) else None
        provider_refused_before_dispatch = (isinstance(driver_code, str) and
            re.fullmatch(r"desktop_pre_dispatch_[a-z0-9_]{1,60}", driver_code) is not None)
        if (order.get("state") == "cancelled" and isinstance(receipt, dict)
                and receipt.get("state") == "stopped" and receipt.get("credits_reconciled") is False
                and receipt.get("task_verified") is False
                and isinstance(execution_id, str) and order.get("execution_id") == execution_id
                and isinstance(evidence_digest, str) and native_stop is None
                and not running_this_order):
            # The local stop can precede the founder's server-side cancel, or
            # its first signed receipt can lose a network race. Retry only the
            # exact durable stop proof; never launch or replay an effect.
            try:
                closed = gateway.stop_receipt(execution_id, evidence_digest)
                if closed.get("state") == "stopped":
                    saved = {**receipt, "credits_reconciled": True,
                             "outcome": "stopped_known_usage_reconciled"}
                    device_journal.save(receipt_scope, venture_id, order_id, saved)
                    return saved
                order = gateway.read()
                native_stop = order.get("native_stop")
                invocations = order.get("invocations")
            except (ValueError, PermissionError, RuntimeError):
                raise _error(409, "native_stop_receipt_retry_unavailable") from None
        if (order.get("state") == "cancelled" and isinstance(receipt, dict)
                and receipt.get("state") == "stopped" and receipt.get("credits_reconciled") is False
                and receipt.get("task_verified") is False
                and isinstance(execution_id, str) and order.get("execution_id") == execution_id
                and isinstance(evidence_digest, str)
                and native_stop == {"execution_id": execution_id,
                    "evidence_digest": evidence_digest, "processes_stopped": True}
                and isinstance(invocations, dict)
                and all(call.get("state") == "completed" and
                        (not call.get("effect") or call["effect"].get("state") in {"returned", "not_dispatched"})
                        for call in invocations.values())
                and not running_this_order):
            try:
                result = gateway.retry_stopped_known_usage(execution_id, evidence_digest)
                saved = {**receipt, "credits_reconciled": result.get("state") == "stopped",
                         "outcome": "stopped_known_usage_reconciled"}
                device_journal.save(receipt_scope, venture_id, order_id, saved)
                return saved
            except (ValueError, PermissionError, RuntimeError):
                raise _error(409, "native_known_usage_reconciliation_unavailable") from None
        if (order.get("state") == "cancelled" and isinstance(receipt, dict)
                and receipt.get("state") == "stopped" and receipt.get("credits_reconciled") is False
                and receipt.get("task_verified") is False
                and isinstance(execution_id, str) and order.get("execution_id") == execution_id
                and isinstance(evidence_digest, str)
                and native_stop == {"execution_id": execution_id,
                    "evidence_digest": evidence_digest, "processes_stopped": True}
                and isinstance(invocations, dict) and bool(invocations)
                and receipt.get("failure_code") == "computer_use_scope_denied"
                and any(call.get("state") in {"calling", "uncertain"}
                        or (call.get("effect") or {}).get("state") in {"admitted", "uncertain"}
                        for call in invocations.values())
                and not running_this_order):
            try:
                result = gateway.settle_stopped_unknown_usage(execution_id, evidence_digest)
                saved = {**receipt, "credits_reconciled": result.get("state") == "stopped",
                         "outcome": "stopped_unknown_usage_ceiling_reconciled",
                         "actual_usage_unconfirmed": result.get("actual_usage_unconfirmed", True),
                         "effect_outcome_unconfirmed": result.get("effect_outcome_unconfirmed", True)}
                device_journal.save(receipt_scope, venture_id, order_id, saved)
                return saved
            except (ValueError, PermissionError, RuntimeError):
                raise _error(409, "native_unknown_usage_reconciliation_unavailable") from None
        if (isinstance(receipt, dict) and receipt.get("state") == "stopped"
                and receipt.get("credits_reconciled") is False
                and receipt.get("failure_code") == "computer_use_scope_denied"
                and not running_this_order):
            # Surface a bounded, non-secret cause instead of misreporting this
            # as an interrupted *startup*. The hold stays untouched.
            expected = {"execution_id": execution_id,
                "evidence_digest": evidence_digest, "processes_stopped": True}
            if order.get("state") != "cancelled":
                reason = "order_state"
            elif not isinstance(execution_id, str) or order.get("execution_id") != execution_id:
                reason = "execution_identity"
            elif not isinstance(evidence_digest, str) or native_stop != expected:
                reason = "stop_receipt"
            elif not isinstance(invocations, dict) or not invocations:
                reason = "invocations_missing"
            elif any(call.get("effect") and call["effect"].get("state") not in {"returned", "not_dispatched"}
                     for call in invocations.values()):
                reason = "effect_uncertain"
            else:
                reason = "usage_state"
            raise _error(409, "native_stopped_reconciliation_" + reason)
        if (order.get("state") == "cancelled" and isinstance(receipt, dict)
                and receipt.get("state") == "stopped" and receipt.get("tool") == "desktop"
                and receipt.get("task_verified") is False
                and receipt.get("credits_reconciled") is False
                and (receipt.get("failure_code") == "computer_use_effect_outcome_uncertain"
                     or provider_refused_before_dispatch
                     and receipt.get("failure_code") == "native_worker_upstream_rejected")
                and (driver_code == "desktop_fresh_observation_required" or provider_refused_before_dispatch)
                and evidence.get("outcome") == "execution_requires_reconciliation"
                and isinstance(execution_id, str) and order.get("execution_id") == execution_id
                and isinstance(evidence_digest, str)
                and isinstance(invocations, dict) and len(invocations) == 1
                and isinstance(effect, dict) and effect.get("state") in {"admitted", "uncertain", "not_dispatched"}
                and isinstance(effect.get("digest"), str)
                and isinstance(native_stop, dict)
                and native_stop.get("execution_id") == execution_id
                and native_stop.get("evidence_digest") == evidence_digest
                and native_stop.get("processes_stopped") is True
                and not running_this_order):
            try:
                gateway.effect_receipt(invocation_id, effect["digest"], "not_dispatched", driver_code)
                result = gateway.reconcile_desktop_pre_dispatch(execution_id, evidence_digest)
                saved = {**receipt, "credits_reconciled": result.get("state") == "stopped",
                         "outcome": "pre_dispatch_rejected_reconciled"}
                device_journal.save(receipt_scope, venture_id, order_id, saved)
                return saved
            except (ValueError, PermissionError, RuntimeError):
                raise _error(409, "native_desktop_pre_dispatch_reconciliation_unavailable") from None
        uncertain_types = [value for value in (invocations or {}).values()
            if isinstance(value, dict) and isinstance(value.get("effect"), dict)
            and value["effect"].get("state") == "uncertain"] if isinstance(invocations, dict) else []
        if (order.get("state") == "cancelled" and isinstance(receipt, dict)
                and receipt.get("state") == "stopped" and receipt.get("tool") == "desktop"
                and receipt.get("task_verified") is False
                and receipt.get("credits_reconciled") is False
                and receipt.get("failure_code") == "computer_use_effect_outcome_uncertain"
                and driver_code == "desktop_effect_outcome_uncertain"
                and evidence.get("outcome") == "execution_requires_reconciliation"
                and isinstance(execution_id, str) and order.get("execution_id") == execution_id
                and isinstance(evidence_digest, str)
                and isinstance(native_stop, dict)
                and native_stop == {"execution_id": execution_id,
                    "evidence_digest": evidence_digest, "processes_stopped": True}
                and len(uncertain_types) == 1
                and uncertain_types[0].get("proposal", {}).get("operation") == "type"
                and not running_this_order):
            try:
                result = gateway.reconcile_stopped_desktop_text(execution_id, evidence_digest)
                saved = {**receipt, "credits_reconciled": result.get("state") == "stopped",
                         "outcome": "reviewed_text_effect_outcome_unverified_reconciled"}
                device_journal.save(receipt_scope, venture_id, order_id, saved)
                return saved
            except (ValueError, PermissionError, RuntimeError):
                raise _error(409, "native_desktop_text_reconciliation_unavailable") from None
        click_ack = order.get("uncertain_desktop_click_ack") or {}
        if (order.get("state") == "cancelled" and isinstance(receipt, dict)
                and receipt.get("state") == "stopped" and receipt.get("tool") == "desktop"
                and receipt.get("task_verified") is False
                and receipt.get("credits_reconciled") is False
                and receipt.get("failure_code") == "computer_use_effect_outcome_uncertain"
                and receipt.get("operation") == "click"
                and evidence.get("outcome") == "execution_requires_reconciliation"
                and isinstance(execution_id, str) and order.get("execution_id") == execution_id
                and isinstance(evidence_digest, str)
                and isinstance(native_stop, dict)
                and native_stop == {"execution_id": execution_id,
                    "evidence_digest": evidence_digest, "processes_stopped": True}
                and click_ack.get("execution_id") == execution_id
                and click_ack.get("evidence_digest") == evidence_digest
                and len(uncertain_types) == 1
                and uncertain_types[0].get("proposal", {}).get("operation") == "click"
                and click_ack.get("effect_digest") == uncertain_types[0]["effect"].get("digest")
                and not running_this_order):
            try:
                result = gateway.reconcile_acknowledged_desktop_click(execution_id, evidence_digest)
                saved = {**receipt, "credits_reconciled": result.get("state") == "stopped",
                         "outcome": "founder_reviewed_click_outcome_unverified_reconciled"}
                device_journal.save(receipt_scope, venture_id, order_id, saved)
                return saved
            except (ValueError, PermissionError, RuntimeError):
                raise _error(409, "native_desktop_click_reconciliation_unavailable") from None
        if (isinstance(receipt, dict) and receipt.get("state") == "reconciliation_required"
                and receipt.get("tool") == "coding" and receipt.get("credits_reconciled") is False
                and evidence.get("outcome") == "changes_require_acceptance_verification"
                and evidence.get("processes_stopped") is False
                and inventory.get("inventory_complete") is True
                and coding.get("artifact_verified") is True
                and coding.get("task_acceptance_verified") is False
                and isinstance(execution_id, str) and order.get("execution_id") == execution_id):
            from desktop_runtime.owned_processes import OwnedProcesses
            if not OwnedProcesses.receipt_stopped(inventory):
                raise _error(409, "native_process_cleanup_reconciliation_unavailable")
            repaired_evidence = {**evidence, "processes_stopped": True}
            repaired_digest = hashlib.sha256(json.dumps(repaired_evidence, sort_keys=True,
                separators=(",", ":")).encode()).hexdigest()
            try:
                result = gateway.reconcile_process_cleanup(execution_id, repaired_digest)
                saved = {**receipt, "state": "stopped", "credits_reconciled": result.get("state") == "stopped",
                         "evidence": repaired_evidence, "evidence_digest": repaired_digest,
                         "outcome": "changes_require_acceptance_verification"}
                device_journal.save(scope, venture_id, order_id, saved)
                return saved
            except (ValueError, PermissionError, RuntimeError):
                raise _error(409, "native_process_cleanup_reconciliation_unavailable") from None
        if (isinstance(receipt, dict) and receipt.get("state") == "stopped"
                and receipt.get("credits_reconciled") is False
                and receipt.get("failure_code") == "browser_effect_outcome_uncertain"
                and evidence.get("outcome") == "execution_requires_reconciliation"
                and evidence.get("processes_stopped") is True
                and isinstance(execution_id, str) and isinstance(evidence_digest, str)):
            try:
                result = gateway.reconcile_stopped_navigation(execution_id, evidence_digest)
                receipt["credits_reconciled"] = result.get("state") == "stopped"
                device_journal.save(scope, venture_id, order_id, receipt)
                return receipt
            except (ValueError, PermissionError, RuntimeError):
                raise _error(409, "native_navigation_reconciliation_unavailable") from None
        legacy_empty_startup = (receipt.get("failure_code") == "native_worker_upstream_rejected"
            and evidence.get("close_returned") is None
            and receipt.get("tool") == "coding") if isinstance(receipt, dict) else False
        with worker_lock:
            running_old_order = (active_worker["worker"] is not None
                and active_worker["worker"].gateway.order_id == order_id
                and active_worker["thread"] is not None
                and active_worker["thread"].is_alive())
        if (order.get("state") != "cancelled" or not isinstance(receipt, dict)
                or receipt.get("state") != "reconciliation_required"
                or receipt.get("task_verified") is not False
                or evidence.get("outcome") != "execution_requires_reconciliation"
                or (evidence.get("close_returned") is not True and not legacy_empty_startup)
                or evidence.get("processes_stopped") is not False
                or inventory.get("inventory_complete") is not True or inventory.get("processes") != []
                or order.get("execution_id") != execution_id or running_old_order):
            raise _error(409, "native_startup_reconciliation_unavailable")
        result = gateway.reconcile_startup_failure(execution_id, evidence_digest)
        saved = {**receipt, "state": "stopped", "credits_reconciled": result.get("state") == "stopped",
                 "outcome": "pre_inference_startup_failure_reconciled"}
        device_journal.save(receipt_scope, venture_id, order_id, saved)
        return saved

    @app.on_event("shutdown")
    async def stop_native_worker():
        from desktop_runtime.agent_status import stop_probes
        stop_probes()
        with worker_lock:
            worker, thread = active_worker["worker"], active_worker["thread"]
            if worker:
                worker.stop_product_preview()
                worker.cancel()
        if app.state.model_http is not None:
            await app.state.model_http.close()
        if thread and thread.is_alive():
            await asyncio.to_thread(thread.join, timeout=20)

    @app.get("/native/device-work-orders/{venture_id}/{order_id}/preflight")
    def native_order_preflight(venture_id: str, order_id: str):
        import importlib.util

        from desktop_runtime import device_journal
        try:
            gateway = native_order_gateway(venture_id, order_id)
            order = gateway.read()
        except (PermissionError, ValueError, RuntimeError):
            raise _error(409, "native_order_preflight_unconfirmed") from None
        dependency = importlib.util.find_spec("playwright") is not None
        try:
            device_journal.probe()
            journal_ready = True
        except (OSError, sqlite3.Error, ValueError):
            journal_ready = False
        manifest = order.get("manifest", {})
        compatible = (manifest.get("applications") == ["com.google.Chrome"]
            and bool(manifest.get("origins")) and "browser.read" in manifest.get("actions", [])
            and set(manifest.get("actions", [])) <= {"browser.read", "browser.interact"})
        # Existence of a dependency is only a preflight, never a launch receipt.
        return {"order_id": order_id, "state": order.get("state"), "browser_dependency_present": dependency,
                "browser_scope_compatible": compatible, "journal_ready": journal_ready,
                "execution_started": False}

    @app.get("/native/notices")
    def native_notices():
        from desktop_runtime import local_notices as local_pause_notices
        from desktop_runtime import notice_delivery as delivery
        with session.lock:
            bearer = access_token()
            local = local_pause_notices.pending(actor_id=session.account_id)
            try:
                response = request_gateway("GET", "/api/desktop/notices", bearer=bearer)
                payload = response.json()
            except (httpx.HTTPError, ValueError):
                raise _error(503, "gateway_unreachable") from None
            if not response.is_success or not isinstance(payload, dict):
                raise _error(response.status_code if response.status_code in {401, 403, 429} else 502,
                             "notice_delivery_failed")
            remote = payload.get("notices") if isinstance(payload.get("notices"), list) else []
            annotated = []
            for item in remote:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                    continue
                annotated.append(delivery.annotate(item))
            annotated = [item for item in annotated if not delivery.is_settled(item.get("id"))]
            return {**payload, "recipient_subject": session.account_id, "notices": [*local, *annotated][:20]}

    @app.post("/native/notices/ack")
    def native_ack_notices(body: dict[str, Any]):
        with session.lock:
            access_token()
            if body.get("recipient_subject") != session.account_id:
                raise _error(409, "notice_ack_account_changed")
            return ack_notices_scoped(body)

    def ack_notices_scoped(body: dict[str, Any]):
        from desktop_runtime import local_notices as local_pause_notices
        from desktop_runtime import notice_delivery as delivery
        results = body.get("results")
        ids = body.get("ids")
        # Prefer channel-separated results. Legacy ids still clear both channels.
        if results is not None:
            if (not isinstance(results, list) or len(results) > 20
                    or any(not isinstance(entry, dict) or not isinstance(entry.get("id"), str)
                           or not entry.get("id") or len(entry["id"]) > 100 for entry in results)):
                raise _error(400, "invalid_notice_ack")
            local_entries = [entry for entry in results
                             if str(entry.get("id", "")).startswith("not_local_")]
            remote_entries = [entry for entry in results
                              if not str(entry.get("id", "")).startswith("not_local_")]
            local_report = local_pause_notices.record_results(local_entries, actor_id=session.account_id)
            remote_settled = delivery.record_results(remote_entries)
            remote_ids = remote_settled.get("settled_ids") or []
            if not remote_ids:
                return {
                    "acked": local_report.get("settled", 0),
                    "local": local_report,
                    "remote": remote_settled,
                    "statuses": [
                        *(local_report.get("statuses") or []),
                        *(remote_settled.get("statuses") or []),
                    ],
                }
            with session.lock:
                bearer = access_token()
                try:
                    response = request_gateway(
                        "POST", "/api/desktop/notices/ack",
                        body={"ids": remote_ids}, bearer=bearer)
                    payload = response.json()
                except (httpx.HTTPError, ValueError):
                    raise _error(503, "gateway_unreachable") from None
                if not response.is_success or not isinstance(payload, dict):
                    raise _error(response.status_code if response.status_code in {401, 403, 429} else 502,
                                 "notice_ack_failed")
                remote_acked = int(payload.get("acked") or 0) if isinstance(payload.get("acked"), int) else 0
                return {
                    **payload,
                    "acked": remote_acked + int(local_report.get("settled") or 0),
                    "local": local_report,
                    "remote": remote_settled,
                    "statuses": [
                        *(local_report.get("statuses") or []),
                        *(remote_settled.get("statuses") or []),
                    ],
                }
        if (not isinstance(ids, list) or len(ids) > 20
                or any(not isinstance(value, str) or not value or len(value) > 100 for value in ids)):
            raise _error(400, "invalid_notice_ack")
        local_acked = local_pause_notices.ack(ids, actor_id=session.account_id)
        remote_ids = [value for value in ids if not str(value).startswith("not_local_")]
        delivery.force_settle(remote_ids)
        if not remote_ids:
            return {"acked": local_acked}
        with session.lock:
            bearer = access_token()
            try:
                response = request_gateway("POST", "/api/desktop/notices/ack", body={"ids": remote_ids}, bearer=bearer)
                payload = response.json()
            except (httpx.HTTPError, ValueError):
                raise _error(503, "gateway_unreachable") from None
            if not response.is_success or not isinstance(payload, dict):
                raise _error(response.status_code if response.status_code in {401, 403, 429} else 502,
                             "notice_ack_failed")
            remote_acked = int(payload.get("acked") or 0) if isinstance(payload.get("acked"), int) else 0
            return {**payload, "acked": remote_acked + local_acked}

    @app.post("/auth/pkce/start")
    def pkce_start(body: dict[str, Any] | None = None):
        if native_owned:
            raise _error(409, "native_auth_required")
        with session.lock:
            session.clear()
            session.verifier, session.verifier_created = pkce.new_verifier(), time.time()
            challenge = pkce.challenge(session.verifier)
            return {"authorize_url": pkce.authorize_url(session.gateway, code_challenge=challenge,
                    device_name=str((body or {}).get("device_name") or "Supymem Desktop")), "code_challenge": challenge}

    @app.post("/auth/pkce/complete")
    def pkce_complete(body: dict[str, Any]):
        if native_owned:
            raise _error(409, "native_auth_required")
        with session.lock:
            if not session.verifier or time.time() - session.verifier_created > 300 or not body.get("code"):
                raise _error(400, "pkce_incomplete_or_expired")
            data = token_request("/auth/device/token", pkce.token_body(session.verifier, str(body["code"])))
            session.accept_tokens(data)
            return {"ok": True, "account_id": session.account_id, "device_id": session.device_id,
                    "gateway": session.gateway}

    @app.post("/connect")
    def connect(body: dict[str, Any] | None = None):
        with session.lock:
            if (body or {}).get("ws_url"):
                raise _error(409, "remote_dispatch_requires_authorized_work_orders")
            # Identity comes only from the verified PKCE exchange, never from body.account_id.
            bearer = access_token()
            try:
                response = request_gateway("GET", "/auth/session", bearer=bearer)
                data = response.json() if response.is_success else {}
            except (httpx.HTTPError, ValueError):
                session.connected, session.last_error = False, "gateway_unreachable"
                raise _error(503, "gateway_unreachable") from None
            if not isinstance(data, dict) or not data.get("authenticated") or data.get("subject") != session.account_id:
                session.clear()
                raise _error(401, "device_authentication_required")
            session.connected, session.last_error = True, None
            replayed = replay_outbox()
            return {"ok": True, "connected": session.connected, "replayed": replayed}

    @app.post("/disconnect")
    def disconnect():
        if native_owned:
            raise _error(409, "native_auth_required")
        with session.lock:
            session.clear()
        return {"ok": True}

    @app.post("/outbox")
    def queue_outbox(body: dict[str, Any], request: Request):
        key = request.headers.get("idempotency-key") or body.get("idempotency_key") or ""
        method, path = str(body.get("method") or "POST").upper(), str(body.get("path") or "")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", key):
            raise _error(400, "idempotency_required")
        if method not in {"POST", "PUT", "PATCH", "DELETE"} or not _valid_path(path):
            raise _error(400, "invalid_outbox_operation")
        with session.lock:
            scope = session.scope()
            try:
                result = offline.enqueue(method, path, body.get("body"), idempotency_key=key, **scope)
            except ValueError:
                raise _error(409, "idempotency_conflict") from None
            if session.connected and not result["already_sent"]:
                try:
                    replay_outbox()
                except HTTPException:
                    pass  # The durable operation remains queued; expose its actual state below.
            item = next((row for row in offline.pending(**scope) if row["idempotency_key"] == key), None)
            result.update(sent=item is None, state=item["state"] if item else "delivered")
            if item and item["last_error"]:
                result["error"] = item["last_error"]
            return result

    @app.get("/outbox")
    def list_outbox():
        with session.lock:
            return {"items": offline.pending(**session.scope())}

    @app.post("/outbox/replay")
    def replay():
        with session.lock:
            count = replay_outbox()
            return {"replayed": count, "remaining": len(offline.pending(**session.scope()))}

    @app.post("/dispatch")
    def dispatch_local(body: dict[str, Any]):
        # No caller-supplied workdir can become a host command. The native local
        # core route remains available with its consent checks; hosted dispatch
        # needs a scoped and authenticated work-order contract first.
        raise _error(409, "remote_dispatch_requires_authorized_work_orders")

    return app


def local_build_router(core_url: str):
    from fastapi import APIRouter, Depends
    from app.core.local_owner import current_owner
    from core.local_security import _proxy_secret

    bridge = create_bridge_app(local_core=core_url)
    session = bridge.state.bridge_session

    def bind_order(venture_id: str, order_id: str):
        if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) for value in (venture_id, order_id)):
            raise _error(400, "native_order_identity_invalid")
        owner = current_owner().id
        credential = _proxy_secret().decode("ascii")
        worker = bridge.state.local_active_worker["worker"]
        if (worker is not None and worker.gateway.venture_id == venture_id
                and order_id in {worker.gateway.order_id, getattr(worker, "requested_plan_order_id", None)}):
            if (session.account_id == owner and session.access_token is not None
                    and secrets.compare_digest(session.access_token, credential)):
                return
        with httpx.Client(timeout=15, trust_env=False, follow_redirects=False) as client:
            response = client.get(_api_url(core_url, f"/api/ventures/{venture_id}/device-work-orders/{order_id}"),
                                  headers={"X-Local-Proxy-Secret": credential})
        if not response.is_success or len(response.content) > 262144:
            raise _error(409, "native_order_review_unavailable")
        record = response.json()
        if (not isinstance(record, dict) or record.get("actor_id") != owner
                or record.get("id") != order_id or record.get("venture_id") != venture_id
                or not isinstance(record.get("device_id"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", record["device_id"])
                or not record.get("manifest", {}).get("coding_loop_verification")
                or not secrets.compare_digest(credential.encode(), _proxy_secret())):
            raise _error(403, "native_order_identity_changed")
        with session.lock:
            identity = (session.gateway, owner, record["device_id"])
            if (session.gateway, session.account_id, session.device_id) != identity or session.access_token != credential:
                session.clear()
            session.gateway, session.account_id, session.device_id = identity
            session.access_token = credential
            session.connected = True

    controls = APIRouter()
    controls.routes = [route for route in bridge.router.routes if getattr(route, "path", "").startswith(
        "/native/device-work-orders/") and route.path.rsplit("/", 1)[-1] in {
            "preflight", "start", "status", "cancel", "reconcile", "continue", "soft-stop"}]
    router = APIRouter()
    router.include_router(controls, dependencies=[Depends(bind_order)])
    detection = APIRouter()
    detection.routes = [route for route in bridge.router.routes if getattr(route, "path", "") == "/native/coding-agents"]
    router.include_router(detection)
    emergency = APIRouter()
    emergency.routes = [route for route in bridge.router.routes if getattr(route, "path", "") in {
        "/native/emergency-stop", "/native/emergency-stop/status"}]
    router.include_router(emergency, dependencies=[Depends(current_owner)])
    for handler in bridge.router.on_startup:
        router.add_event_handler("startup", handler)
    for handler in bridge.router.on_shutdown:
        router.add_event_handler("shutdown", handler)
    return router


async def run_bridge_client() -> None:
    raise SystemExit("Hosted local execution requires an authorized work-order channel; use the authenticated desktop session.")

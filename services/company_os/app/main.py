"""Company OS FastAPI — port 3300."""

from __future__ import annotations

from contextlib import asynccontextmanager, nullcontext
import asyncio
from functools import cache
import logging
import os
import re

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.api import mount_routers
from app import bootstrap as bootstrap_mod
from app import state as state_mod

# Back-compat for tests that clear MEM / reset DB via main module attributes
_MEM = state_mod.MEM
_factory = None
_engine = None
_VENTURE_PATH = re.compile(r"^/ventures/(ven_[^/]+)(?:/|$)")
_WORKSPACE_PATH = re.compile(r"^/workspaces/(wsp_[^/]+)(?:/|$)")


@asynccontextmanager
async def lifespan(app: FastAPI):
    from desktop_runtime.agent_status import start_probes
    start_probes()
    from app.core.local_process import process_lock
    from app.core.local_worker import lifespan as worker_lifespan
    from app.core.local_env_import import import_local_env
    import_local_env()

    with process_lock():
        swept = bootstrap_mod.bootstrap()
        if swept:
            from app.onboarding.resume import auto_resume_runs

            asyncio.create_task(auto_resume_runs(swept))
        try:
            async with worker_lifespan():
                yield
        finally:
            from desktop_runtime.agent_status import stop_probes
            stop_probes()
            from app.chat.rephrase import drain
            await drain()


# Signed (subject, email) pairs already written to Person; the email repair runs once per process.
_EMAIL_SYNCED: set[tuple[str, str]] = set()


@cache
def _hosted_router_mount():
    try:
        from app.hosted_routes import mount_hosted_routers
    except ModuleNotFoundError as exc:
        if exc.name != "app.hosted_routes":
            raise
        logging.getLogger(__name__).info("Hosted routes are not included in this source tree.")
        return None
    return mount_hosted_routers


def create_app() -> FastAPI:
    from desktop_runtime.hosted import validate_local_state

    validate_local_state()
    from core.env_contract import ensure_boot_env

    from app.admin.authn import validate_security_settings

    # Fail closed before the ASGI app exists so a bad env never serves traffic.
    ensure_boot_env(service="core")
    validate_security_settings()
    from core.logging import install_sensitive_url_filters

    install_sensitive_url_filters()
    app = FastAPI(title="Company OS Core", version="0.1.0", lifespan=lifespan)
    from core.request_id import RequestIdMiddleware

    from app.core.local_mode import is_local
    from app.core.local_owner import LocalOwnerMiddleware
    from app.diagnostics.server_errors import ServerErrorCaptureMiddleware
    if is_local() or os.environ.get("COS_JSON_LOGS", "").strip() in {"1", "true", "yes"}:
        from core.logging import configure_json_logging

        configure_json_logging("company_os")

    @app.middleware("http")
    async def enforce_venture_scope(request: Request, call_next):
        """Verify identity before resolving tenant-owned resources."""
        from app.admin.authn import require_signed_caller, verify_caller
        from app.authz.http import (
            bind_subject_header,
            enforce_hosted_contract,
            independent_auth,
            matched_handler,
            resource_scope,
        )
        from app.tenancy import Scope, reset_scope, set_scope

        module, params = matched_handler(request)
        independent = independent_auth(request, module)
        subject = request.state.local_owner.id if is_local() and not independent else None
        scope = None
        try:
            if not independent:
                signed = require_signed_caller() or bool(request.headers.get("X-COS-Assertion"))
                if signed:
                    caller = verify_caller(request)
                    if not caller.sub:
                        raise HTTPException(401, detail={"code": "unauthenticated"})
                    subject = caller.sub
                    bind_subject_header(request, subject)
                    enforce_hosted_contract(request, module)
                else:
                    # Explicit local mode preserves existing passphrase/test flows.
                    subject = request.headers.get("X-Subject")
                if subject and subject.strip() == "system":
                    raise HTTPException(401, detail={"code": "unauthenticated"})
                # Resolve resource IDs even when a nonexistent suffix is requested.
                # This preserves the same non-disclosure response for foreign IDs.
                match = _VENTURE_PATH.match(request.url.path)
                if match:
                    params = {**params, "venture_id": match.group(1)}
                match = _WORKSPACE_PATH.match(request.url.path)
                if match:
                    params = {**params, "workspace_id": match.group(1)}
                scope = await resource_scope(request, module, params, subject, signed=signed)
                if is_local() and scope and scope.workspace_id not in {"", "none"}:
                    from app.core.providers_local import _sample_selected
                    from app.core.local_mode import is_sample_workspace
                    if _sample_selected() and not await asyncio.to_thread(is_sample_workspace, scope.workspace_id):
                        raise HTTPException(409, "Your real venture stays separate from sample mode. Open your workspace and create a sample venture, or open Settings > Keys and connections > Your AI provider and save your model to return to live work.")
                if signed and caller.role == "user" and caller.email and (subject, caller.email) not in _EMAIL_SYNCED:
                    from app.authz.workspace import ensure_subject_person

                    ensure_subject_person(subject, email=caller.email)
                    # ponytail: per-process memo, bounded by clearing; a shared cache if replicas grow large
                    if len(_EMAIL_SYNCED) > 50_000:
                        _EMAIL_SYNCED.clear()
                    _EMAIL_SYNCED.add((subject, caller.email))
                from app.core.ratelimit import check_workspace
                from core.ratelimit import expensive_action

                if subject and expensive_action(request.url.path, request.method):
                    await asyncio.to_thread(check_workspace, scope.workspace_id if scope else f"pre-workspace:{subject}")
        except HTTPException as exc:
            logging.getLogger("app.authz.http").warning(
                "request authorization denied",
                extra={"status_code": exc.status_code, "route_module": module, "method": request.method},
            )
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)
        token = set_scope(scope or Scope(workspace_id="none", venture_id="", actor=subject or "anonymous"))
        try:
            from core.time_context import timezone_context
            from app.tasks.time_context import founder_timezone
            zone = founder_timezone(subject)
            with timezone_context(zone) if zone else nullcontext():
                response = await call_next(request)
            if scope is not None:
                if scope.venture_id:
                    response.headers["X-COS-Venture-ID"] = scope.venture_id
                response.headers["X-COS-Workspace-ID"] = scope.workspace_id
            return response
        finally:
            reset_scope(token)

    # Register after scope enforcement so this wraps both the authorization
    # middleware and the routed application, including their unhandled 5xxs.
    app.add_middleware(ServerErrorCaptureMiddleware)
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(LocalOwnerMiddleware)

    if is_local():
        from desktop_runtime.bridge import local_build_router
        from app.config import settings
        app.include_router(local_build_router(os.environ.get("COMPANY_OS_URL",
            f"http://127.0.0.1:{settings.core_port}").rstrip("/")))

    @app.get("/health")
    def health():
        from app.health import health_response

        return health_response()

    @app.get("/ready")
    def ready():
        from app.health import ready_response

        return ready_response()

    if is_local():
        from app.api.local_settings import import_env_settings
        try:
            import_env_settings()
        except HTTPException:
            pass
        from app.api.local_health import router as local_health_router
        app.include_router(local_health_router)
        from app.api.secrets_api import router as local_secret_slots_router
        app.include_router(local_secret_slots_router)

    mount_routers(app)
    if not is_local() and (mount_hosted_routers := _hosted_router_mount()):
        mount_hosted_routers(app)
    try:
        from app.ops.ws import router as ops_ws_router

        from app.admin.authn import require_signed_caller

        if is_local() or not require_signed_caller():
            app.include_router(ops_ws_router)
    except ModuleNotFoundError:
        pass
    try:
        from app.mcp.server import router as mcp_router

        app.include_router(mcp_router)
    except ModuleNotFoundError:
        pass
    return app


app = create_app()

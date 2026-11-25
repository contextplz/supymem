"""HTTP identity contracts and resource ownership before endpoint side effects.

Alternate authentication is tied to a specific mounted handler, never an
entire URL prefix. New routes require a gateway assertion by default.
"""

from __future__ import annotations

import json
import re
from typing import Any

from fastapi import HTTPException, Request
from starlette.routing import Match

from app.authz.workspace import assert_venture_access, assert_workspace_access
from app.core.local_mode import is_local
from app.tenancy import Scope


def matched_handler(request: Request) -> tuple[str, dict[str, Any]]:
    for route in request.app.routes:
        # Newer FastAPI versions preserve included routers instead of flattening
        # them. Their effective contexts include the fully composed path and
        # actual handler; the wrapper's matches() deliberately omits both.
        contexts = getattr(route, "effective_route_contexts", None)
        candidates = contexts() if contexts else (route,)
        for candidate in candidates:
            match, child = candidate.matches(request.scope)
            if match == Match.FULL:
                endpoint = child.get("endpoint") or getattr(candidate, "endpoint", None)
                return getattr(endpoint, "__module__", ""), child.get("path_params", {})
    return "", {}


def independent_auth(request: Request, module: str) -> bool:
    """Handlers with their own bearer/webhook/internal-secret authorization."""
    path, method = request.url.path, request.method
    if path == "/local/health" and module == "app.api.local_health":
        if is_local():
            return method in {"GET", "HEAD"}
    if path in {"/health", "/ready", "/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}:
        return method in {"GET", "HEAD"}
    if module == "app.api.build_runtime_api" and method == "POST":
        # Model-only run bearer and trusted-controller secret have separate
        # audiences. No prefix-wide exception and no alternative founder auth.
        return bool(re.fullmatch(r"/internal/builds/[A-Za-z0-9_-]{1,128}/(?:model-invocations(?:/[A-Za-z0-9_-]{1,128}/consume)?|v1/chat/completions)", path))
    if module == "app.api.build_intent_api" and method == "POST":
        return bool(re.fullmatch(r"/internal/builds/[A-Za-z0-9_-]{1,128}/intent/(?:prepare|synchronize)", path))
    if module == "app.api.ops_api" and path == "/public/unsubscribe":
        return method in {"GET", "POST"}  # The handler verifies the workspace-bound token.
    if module == "app.api.connections_api" and path == "/api/v1/connections/apollo/callback":
        return method == "GET"  # Persisted one-use OAuth state authenticates this return.
    if module == "app.api.ops_api" and method == "GET":
        if is_local() and re.fullmatch(r"/api/v1/content/oauth/[A-Za-z0-9_-]+/callback", path):
            return True
        # These exact callbacks validate a persisted one-time attempt, including
        # its founder, workspace, provider and connection receipt. Start/status
        # endpoints and unknown providers still require a signed assertion.
        from app.connections.toolkits import TOOLKITS

        return path in {
            "/api/v1/content/oauth/google_calendar/callback",
            "/api/v1/content/oauth/linkedin/callback",
            "/api/v1/content/oauth/reddit/callback",
            "/api/v1/content/oauth/x/callback",
            "/api/v1/ai-gtm/gmail/callback",
        } or path in {f"/api/v1/content/oauth/{platform}/callback" for platform in TOOLKITS}
    if module == "app.api.browser_qualification_api":
        return method in {"GET", "HEAD", "POST"} and bool(re.fullmatch(
            r"/public/browser-qualification/[A-Za-z0-9_-]{32,128}/[A-Za-z0-9_./-]{1,512}", path))
    if module == "app.api.waitlist_api":
        if is_local():
            from core.local_security import public_request

            return public_request(request)
        return method in {"GET", "POST"} and bool(re.fullmatch(
            r"/public/waitlists/[A-Za-z0-9_-]{1,128}(?:/pricing/exp_[a-f0-9]{24}/[a-z][a-z0-9_]{0,19}|/surveys/exp_[a-f0-9]{24}/[A-Za-z0-9_-]{20,128})?", path))
    if module == "app.api.beta_api":
        return (method == "POST" and path == "/public/beta/verify") or (method == "GET" and path == "/public/beta/download")
    if module == "app.api.email_api":
        # Unsubscribe link (token-signed) and provider webhooks (signature-verified in the handler).
        return (path == "/public/email/unsubscribe" and method in {"GET", "POST"}) or (
            method == "POST" and path in {"/webhooks/email/resend", "/webhooks/email/ses"})
    if module == "app.api.events_ingest":
        # Anonymous hosted-product beacon only. Telemetry stays signed.
        return method == "POST" and path == "/e"
    if module == "app.api.firstlook_api":
        return (
            method in {"GET", "HEAD"} and path.startswith("/w/")
        ) or (method == "POST" and path.startswith(("/w/", "/api/w/")) and path.endswith("/interest"))
    if module in {"app.api.v1", "app.api.openai_api"}:
        return True  # Their handlers validate scoped API keys.
    if module == "app.api.payments_api":
        return method == "POST" and path.startswith("/webhooks/payments/")
    if module == "app.api.internal_ops":
        return method == "POST" and path == "/internal/ops/run-cycle"
    if module == "app.api.internal_tick":
        return method == "POST" and path == "/internal/tick"
    return False


def bind_subject_header(request: Request, subject: str) -> None:
    """Legacy endpoint Header dependencies read only the verified identity."""
    request.scope["headers"] = [
        (key, value) for key, value in request.scope["headers"] if key.lower() != b"x-subject"
    ] + [(b"x-subject", subject.encode("utf-8"))]
    # Starlette caches the Headers wrapper; clear it after replacing scope data.
    if hasattr(request, "_headers"):
        del request._headers


def _not_found() -> HTTPException:
    return HTTPException(404, detail={"error": {"code": "not_found", "message": "Not found."}})


def resource_workspace(kind: str, resource_id: str) -> tuple[str, str]:
    from app.db import models
    from app.state import db

    names = {
        "approval_id": "Approval", "connection_id": "Connection",
        "engagement_id": "Engagement", "workflow_id": "Workflow",
        "member_id": "WorkspaceMember", "slate_id": "OpportunitySlate",
        "opportunity_id": "Opportunity",
    }
    if kind == "suggestion_id":
        from app.db.supymem_models import Suggestion

        with db()() as session:
            suggestion = session.get(Suggestion, resource_id)
            venture = session.get(models.Venture, suggestion.venture_id) if suggestion else None
            if (venture is None or not venture.workspace_id or venture.archived_at is not None
                    or venture.stage == "purged"):
                raise _not_found()
            return venture.workspace_id, venture.id
    model = getattr(models, names[kind])
    with db()() as session:
        row = session.get(model, resource_id)
        if row is None or not row.workspace_id:
            raise _not_found()
        return row.workspace_id, getattr(row, "venture_id", None) or ""


JOURNEY_MODULES = {"app.api.idea_api", "app.api.plans_api", "app.api.apps_api", "app.api.jobs_api", "app.api.dossiers_api"}


def journey_resources(identifiers: dict[str, Any], *, job_kind: str | None = None) -> list[tuple[str, str]]:
    """Bind nested IDs to one venture, including same-workspace sibling objects."""
    from app.db import supymem_models as models
    from app.db.models import Job
    from app.state import db

    mapping = {"session_id": models.IdeaSession, "revision_id": models.IdeaRevision,
               "rev": models.IdeaRevision, "run_id": models.ValidationRun,
               "plan_id": models.ExecutionPlan, "base_plan_id": models.ExecutionPlan, "app_id": models.FounderApp,
               "build_id": models.BuildRun, "incident_id": models.AppIncident,
               "key_id": models.AppApiKey, "job_id": Job, "dossier_id": models.ResearchDossier,
               "previous_dossier_id": models.ResearchDossier, "finding_id": models.ResearchFinding}
    mapping.update({
        "experiment.run": {"run_id": None, "experiment_id": models.Experiment},
        "experiment.beliefs.tick": {"experiment_id": models.Experiment},
    }.get(job_kind, {}))
    if job_kind == "experiment.run" and not (identifiers.get("experiment_id") and identifiers.get("run_id")):
        raise _not_found()
    scopes = []
    with db()() as session:
        for key, model in mapping.items():
            if model is None or not identifiers.get(key):
                continue
            row = session.get(model, str(identifiers[key]))
            if row is None or getattr(row, "deleted_at", None):
                raise _not_found()
            if job_kind == "experiment.run" and key == "experiment_id":
                try:
                    context = json.loads(row.context or "{}")
                except (TypeError, ValueError):
                    raise _not_found() from None
                if not isinstance(context, dict) or context.get("run_id") != identifiers["run_id"]:
                    raise _not_found()
            for parent in ("workspace_id", "venture_id", "session_id", "app_id", "plan_id", "dossier_id"):
                if key != parent and identifiers.get(parent) and getattr(row, parent, identifiers[parent]) != identifiers[parent]:
                    raise _not_found()
            wid, vid = row.workspace_id, getattr(row, "venture_id", None)
            if not vid and getattr(row, "app_id", None):
                app = session.get(models.FounderApp, row.app_id)
                if app is None or app.deleted_at or app.workspace_id != wid:
                    raise _not_found()
                vid = app.venture_id
            if not wid:
                raise _not_found()  # Global operator jobs are not founder resources.
            scopes.append((wid, vid or ""))
    if len({vid for _, vid in scopes if vid}) > 1 or len({wid for wid, _ in scopes}) > 1:
        raise _not_found()
    return scopes


async def resource_scope(
    request: Request, module: str, params: dict[str, Any], subject: str | None, *, signed: bool,
) -> Scope | None:
    """Resolve path/body resources to the same authorized tenant before mutation."""
    if is_local():
        from app.core.local_owner import current_owner

        subject = current_owner().id
        signed = True
    if module in {"app.admin.router", "app.admin.internal"}:
        # Identity was verified centrally. These handlers apply their separate
        # admin capability/MFA/session gate; an authorized operator need not be a
        # member of each founder's workspace.
        return None
    scopes: list[tuple[str, str]] = []
    if params.get("venture_id"):
        vid, wid = assert_venture_access(str(params["venture_id"]), subject)
        scopes.append((wid, vid))
    if params.get("workspace_id"):
        wid = assert_workspace_access(str(params["workspace_id"]), subject)
        scopes.append((wid, ""))
    handoff_id = (params.get("handoff_id") or params.get("task_id")) if module == "app.api.handoffs_api" else None
    if handoff_id:
        # Only this handler module owns durable Task IDs. Older task_id routes
        # retain their PlannedTask handling, independent of the ID's spelling.
        try:
            from app.db.task_models import Task
        except ImportError:
            raise _not_found() from None
        from app.authz.workspace import subject_person_id
        from app.state import db

        with db()() as session:
            task = session.get(Task, str(handoff_id))
            if (task is None or not task.workspace_id or not task.venture_id
                    or (params.get("venture_id") and task.venture_id != params["venture_id"])):
                raise _not_found()
            vid, wid = assert_venture_access(task.venture_id, subject)
            if wid != task.workspace_id:
                raise _not_found()
            if request.method in {"POST", "PUT", "PATCH", "DELETE"} and task.owner_person_id != subject_person_id(subject):
                raise HTTPException(403, detail={"code": "handoff_owner_required"})
            scopes.append((wid, vid))
    kinds = {
        "approval_id", "connection_id", "engagement_id", "workflow_id",
        "member_id", "slate_id", "opportunity_id", "suggestion_id",
    }
    for kind in kinds & params.keys():
        wid, vid = resource_workspace(kind, str(params[kind]))
        assert_workspace_access(wid, subject)
        scopes.append((wid, vid))

    # These older routes accept tenant/resource identifiers outside the path.
    # Validate only their declared fields rather than arbitrary product JSON.
    body: dict[str, Any] = {}
    inspect_body = module in {
        "app.api.entry_api", "app.api.coding_api", "app.api.approvals_api",
        "app.api.idealab_api", "app.api.firstlook_api",
    }
    if inspect_body and request.method in {"POST", "PUT", "PATCH"}:
        try:
            if module == "app.api.entry_api" and request.headers.get("content-type", "").startswith("multipart/form-data"):
                await request.body()
                async with request.form() as form:
                    body = dict(form)
            else:
                data = await request.json()
                body = data if isinstance(data, dict) else {}
        except ValueError:
            pass  # Pydantic returns the normal input-validation error.
        for name in ("workspace_id", "active_workspace_id"):
            if body.get(name):
                wid = assert_workspace_access(str(body[name]), subject)
                scopes.append((wid, ""))
        if body.get("venture_id"):
            vid, wid = assert_venture_access(str(body["venture_id"]), subject)
            scopes.append((wid, vid))
        selectors = [(kind, body[kind]) for kind in ("opportunity_id", "slate_id") if body.get(kind)]
        if module == "app.api.idealab_api":
            if body.get("previous_slate_id"):
                selectors.append(("slate_id", body["previous_slate_id"]))
            entries = body.get("reject_reasons")
            if isinstance(entries, list):
                selectors.extend(("opportunity_id", entry["opportunity_id"])
                    for entry in entries if isinstance(entry, dict) and entry.get("opportunity_id"))
        for kind, resource_id in selectors:
            wid, vid = resource_workspace(kind, str(resource_id))
            assert_workspace_access(wid, subject)
            scopes.append((wid, vid))
        if module == "app.api.approvals_api":
            for aid in body.get("ids", []) if isinstance(body.get("ids"), list) else []:
                wid, vid = resource_workspace("approval_id", str(aid))
                assert_workspace_access(wid, subject)
                scopes.append((wid, vid))
            if signed:
                for field in ("requested_by", "decided_by"):
                    if body.get(field) and body[field] != subject:
                        raise HTTPException(403, detail={"code": "actor_mismatch"})

    if module in JOURNEY_MODULES:
        identifiers = dict(params)
        if request.method in {"POST", "PATCH", "PUT"}:
            try:
                data = await request.json()
            except ValueError:
                data = {}
            if isinstance(data, dict):
                for name in ("revision_id", "base_plan_id"):
                    if data.get(name):
                        identifiers[name] = data[name]
        for wid, vid in journey_resources(identifiers):
            assert_workspace_access(wid, subject)
            if params.get("venture_id") and vid != params["venture_id"]:
                raise _not_found()
            scopes.append((wid, vid))

    if signed and module in {"app.api.ops_api", "app.api.outreach_ops", "app.api.gtm_god_ops"}:
        # This legacy surface shares /api/v1 with the separately authenticated
        # public API. Its optional global key does not establish tenant identity.
        candidates = request.query_params.getlist("workspace_id") + request.headers.getlist("x-workspace-id")
        if not candidates or not all(candidates):
            raise HTTPException(400, detail={"code": "workspace_required"})
        for wid in candidates:
            assert_workspace_access(wid, subject)
            scopes.append((wid, ""))
        for venture_id in request.query_params.getlist("venture_id"):
            vid, wid = assert_venture_access(venture_id, subject)
            scopes.append((wid, vid))
        content_type = request.headers.get("content-type", "application/json").split(";", 1)[0].strip().lower()
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and (
            content_type == "application/json" or (
                content_type.startswith("application/") and content_type.endswith("+json")
            )
        ):
            try:
                data = await request.json()
            except ValueError:
                data = {}
            if isinstance(data, dict) and isinstance(data.get("venture_id"), str) and data["venture_id"]:
                vid, wid = assert_venture_access(data["venture_id"], subject)
                scopes.append((wid, vid))
    if scopes:
        if len({wid for wid, _ in scopes}) != 1:
            raise _not_found()
        if signed and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            from app.authz.workspace import subject_person_id
            from app.workspaces.service import role_of

            role = role_of(scopes[0][0], subject_person_id(subject))
            if role in {None, "viewer"}:
                raise HTTPException(403, detail={"code": "read_only_workspace"})
            # Changing authority or credentials requires an administrative role.
            path = request.url.path.rstrip("/")
            if (path.endswith(("/autonomy", "/observe-level", "/tenancy", "/invites", "/ingest-key", "/hermes/bootstrap"))
                    or {"llm-settings", "api-keys"} & set(path.split("/"))) and role not in {"owner", "admin"}:
                raise HTTPException(403, detail={"code": "workspace_admin_required"})
        return Scope(scopes[0][0], next((vid for _, vid in scopes if vid), ""), subject or "local")
    return None


def enforce_hosted_contract(request: Request, module: str) -> None:
    """Refuse local/global helpers until they have a tenant-safe hosted contract."""
    if is_local():
        return
    path = request.url.path.rstrip("/")
    # These legacy helpers execute on the API host and have no tenant-bound
    # browser/file isolation, provider approval or durable metering contract.
    # Block aliases (video can dispatch demo) before endpoint/body processing.
    if module == "app.api.ops_api" and path in {
        "/api/v1/marketing/media/image", "/api/v1/marketing/media/video",
        "/api/v1/marketing/media/demo", "/api/v1/marketing/media/browser",
        "/api/v1/marketing/media/desktop", "/api/v1/marketing/media/ads",
        "/api/v1/marketing/lab/jobs", "/api/v1/marketing/ads/enable",
    }:
        raise HTTPException(403, detail={"code": "hosted_media_unavailable",
            "message": "Hosted Media execution is unavailable pending isolated, tenant-approved processing. Uploads and saved assets remain available. Use Ads for reviewed campaign proposals."})
    if (
        module == "app.mcp.server"
        or path in {"/events/recent", "/trace", "/llm/usage", "/api/v1/finance/stripe/webhook"}
        or (module == "app.api.coding_api" and path in {"/coding/agents", "/coding/consent", "/coding/sessions/plan", "/coding/dispatch"})
    ):
        raise HTTPException(
            403,
            detail={"code": "tenant_contract_required", "message": "This local operation is not available on the hosted API."},
        )

"""Founder apps, builds, terminal, sleep/wake (docs/supymem/25)."""

from __future__ import annotations

from app.infra import apps as _service

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, model_validator

from app.authz.workspace import assert_venture_access, subject_person_id
from app.builder.ctl import terminal_exec
from app.builder import execution as managed_builds
from app.core.local_mode import is_local
from app.provisioning import runtime_intents
from app.infra.apps import create_draft, get_app, list_apps, sleep_app, slug_available, wake_app
from app.landing.service import (
    capture_lead,
    generate_and_publish,
    get_site_by_app,
    ingest_visit,
    list_leads,
    rollback,
)

def _execution_contract(request: Request) -> None:
    from app.worker.handlers import hosted_mode
    if not hosted_mode() or request.method == "GET":
        return
    endpoint = request.scope.get("endpoint")
    if getattr(endpoint, "__name__", "") in {"create_app", "create_api_key", "revoke_api_key", "ingest_key", "add_lead", "open_app_incident", "estimate_build", "estimate_site_revision", "start_build", "cancel_build", "review_build", "publish_static_build", "unpublish_static_deployment", "request_site_revision", "request_private_correction", "estimate_private_correction"}:
        return
    raise HTTPException(503, detail={"code": "capability_unavailable",
                        "message": "This execution path requires a verified hosted provider integration."})


router = APIRouter(tags=["apps"], dependencies=[Depends(_execution_contract)])


class CreateAppBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    slug: str = Field(min_length=3, max_length=63, pattern=r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])$")
    kind: str = "service"
    build_mode: str | None = None
    deploy_target: str | None = None


class TerminalBody(BaseModel):
    command: str


class LeadBody(BaseModel):
    email: str
    fields: dict | None = None


class IngestBody(BaseModel):
    path: str = "/"
    session: str | None = None
    referrer_host: str | None = None
    utm: dict | None = None
    event: str = "page_view"
    country: str | None = None
    device: str | None = None
    value: dict | None = None


def _app_or_404(app_id: str):
    return _service.apps_app_or_404(app_id)


@router.get("/ventures/{venture_id}/apps")
def venture_apps(
    venture_id: str,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    assert_venture_access(venture_id, x_subject)
    return {"items": list_apps(venture_id)}


@router.post("/ventures/{venture_id}/apps", status_code=201)
def create_app(
    venture_id: str,
    body: CreateAppBody,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    _, workspace_id = assert_venture_access(venture_id, x_subject)
    try:
        return create_draft(workspace_id=workspace_id, venture_id=venture_id, name=body.name,
                            slug=body.slug, kind=body.kind, build_mode=body.build_mode or "supymem",
                            deploy_target=body.deploy_target or "supymem", actor=subject_person_id(x_subject),
                            idempotency_key=idempotency_key)
    except PermissionError as exc:
        raise HTTPException(403, detail={"code": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(409, detail={"code": str(exc)}) from exc


@router.get("/apps/slug-available")
def check_slug(slug: str = Query(...)):
    return slug_available(slug)


@router.get("/apps/{app_id}")
def read_app(
    app_id: str,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    app = _app_or_404(app_id)
    assert_venture_access(app.venture_id, x_subject)
    return get_app(app_id)


@router.post("/apps/{app_id}/sleep")
def sleep(
    app_id: str,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    app = _app_or_404(app_id)
    assert_venture_access(app.venture_id, x_subject)
    return sleep_app(app_id)


@router.post("/apps/{app_id}/wake")
def wake(
    app_id: str,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    app = _app_or_404(app_id)
    assert_venture_access(app.venture_id, x_subject)
    return wake_app(app_id)


def _managed(operation, **kwargs):
    from app.credits.errors import CreditError, InsufficientCredits
    try:
        return operation(**kwargs)
    except managed_builds.BuildError as exc:
        detail = {"code": exc.code, "reasons": exc.reasons}
        if is_local():
            detail["message"] = ("Open Builds, refresh your saved plan and source, then send a new build request."
                if exc.code != "build_idempotency_conflict" else "Open Builds to watch your existing request, or send a new request for different work.")
        raise HTTPException(exc.status, detail=detail) from exc
    except KeyError as exc:
        detail = {"code": "not_found"}
        if is_local():
            detail["message"] = "Open Builds in your venture, refresh your saved plan and files, then retry your request."
        raise HTTPException(404, detail=detail) from exc
    except PermissionError as exc:
        raise HTTPException(403, detail={"code": str(exc)}) from exc
    except InsufficientCredits as exc:
        raise HTTPException(402, detail={"code": exc.code, "available_micro": exc.available_micro,
                                         "required_micro": exc.required_micro}) from exc
    except CreditError as exc:
        if exc.code == "local_spend_limit":
            raise HTTPException(402, detail={"code": exc.code, "message": str(exc)}) from exc
        raise HTTPException(409, detail={"code": exc.code}) from exc


class EstimateBuildBody(BaseModel):
    model_config = {"extra": "forbid", "strict": True}
    plan_id: str = Field(min_length=1, max_length=128)
    plan_task_id: str = Field(min_length=1, max_length=128)
    source_artifact_id: str | None = Field(default=None, max_length=128)
    requested_mode: str = "supymem"


class StartBuildBody(BaseModel):
    model_config = {"extra": "forbid", "strict": True}
    quote_id: str | None = Field(default=None, min_length=1, max_length=128)
    input_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    approve_budget_micro: int | None = Field(default=None, gt=0)
    instruction: str | None = Field(default=None, min_length=1, max_length=1200)
    plan_task_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_hosted_budget(self):
        if not is_local():
            if self.quote_id is None or self.input_digest is None or self.approve_budget_micro is None:
                raise ValueError("quote_id, input_digest and approve_budget_micro are required")
        elif (self.quote_id is None) != (self.input_digest is None) or (self.quote_id is not None and self.instruction is not None):
            raise ValueError("Open Builds and refresh your saved request, or send a new instruction in your venture chat.")
        return self


class PublishStaticBody(BaseModel):
    model_config = {"extra": "forbid", "strict": True}
    build_id: str = Field(min_length=1, max_length=128)


class SiteRevisionBody(BaseModel):
    model_config = {"extra": "forbid", "strict": True}
    deployment_id: str = Field(min_length=1, max_length=128)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    instruction: str = Field(min_length=8, max_length=2000)


class EstimateSiteRevisionBody(BaseModel):
    model_config = {"extra": "forbid", "strict": True}
    revision_request_id: str = Field(min_length=1, max_length=128)


@router.post("/apps/{app_id}/builds/estimate")
def estimate_build(app_id: str, body: EstimateBuildBody,
                   x_subject: str | None = Header(default=None, alias="X-Subject")):
    return _managed(managed_builds.estimate, app_id=app_id, actor=subject_person_id(x_subject), **body.model_dump())


@router.post("/apps/{app_id}/builds", status_code=202)
def start_build(app_id: str, body: StartBuildBody,
                idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                x_subject: str | None = Header(default=None, alias="X-Subject")):
    if is_local():
        app = _app_or_404(app_id)
        assert_venture_access(app.venture_id, x_subject)
        if body.quote_id is not None and body.input_digest is not None:
            return _managed(managed_builds.start, app_id=app_id, actor=subject_person_id(x_subject),
                            idempotency_key=idempotency_key, **body.model_dump(exclude={"instruction", "plan_task_id"}))
        return _managed(managed_builds.request_local, app_id=app_id, actor=subject_person_id(x_subject),
                        instruction=body.instruction or "Build the app from my saved idea and plan.",
                        plan_task_id=body.plan_task_id, idempotency_key=idempotency_key)
    return _managed(managed_builds.start, app_id=app_id, actor=subject_person_id(x_subject),
                    idempotency_key=idempotency_key, **body.model_dump(exclude={"instruction", "plan_task_id"}))


@router.get("/apps/{app_id}/builds")
def app_builds(app_id: str, x_subject: str | None = Header(default=None, alias="X-Subject")):
    return {"items": _managed(managed_builds.listing, app_id=app_id, actor=subject_person_id(x_subject))}


@router.get("/apps/{app_id}/runtime")
def app_runtime(app_id: str, x_subject: str | None = Header(default=None, alias="X-Subject")):
    return {"runtime": _managed(runtime_intents.founder_status, app_id=app_id,
                                actor=subject_person_id(x_subject))}


@router.get("/builds/{build_id}")
def read_build(build_id: str, x_subject: str | None = Header(default=None, alias="X-Subject")):
    return _managed(managed_builds.read, run_id=build_id, actor=subject_person_id(x_subject))


@router.get("/builds/{build_id}/events")
def build_events(build_id: str, after: int = Query(default=0, ge=0),
                 x_subject: str | None = Header(default=None, alias="X-Subject")):
    return _managed(managed_builds.events, run_id=build_id, actor=subject_person_id(x_subject), after=after)


@router.post("/builds/{build_id}/cancel")
def cancel_build(build_id: str, x_subject: str | None = Header(default=None, alias="X-Subject")):
    return _managed(managed_builds.cancel, run_id=build_id, actor=subject_person_id(x_subject))


@router.post("/apps/{app_id}/terminal/sessions")
def terminal(
    app_id: str,
    body: TerminalBody,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    app = _app_or_404(app_id)
    assert_venture_access(app.venture_id, x_subject)
    result = terminal_exec(body.command)
    return result | {"session_id": None, "ws_url": None}


@router.get("/apps/{app_id}/qa")
def qa_list(
    app_id: str,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    return _service.apps_qa_list(app_id, x_subject)


def _qa_read(build_id, qa_id, subject, object_id=None):
    from app.builder.qa_receipts import read
    from app.builder.model_proxy import BuildModelError
    try:
        result = _managed(read, run_id=build_id, qa_id=qa_id, actor=subject_person_id(subject), object_id=object_id)
    except BuildModelError as exc:
        raise HTTPException(409, detail={"code": str(exc)}) from exc
    if object_id is None:
        return result
    blob, checksum, media = result
    return Response(blob, media_type=media, headers={"Content-Length": str(len(blob)), "X-Supymem-Artifact-SHA256": checksum,
                    "Cache-Control": "no-store", "Content-Disposition": "attachment", "X-Content-Type-Options": "nosniff"})


@router.get("/builds/{build_id}/qa/{qa_id}")
def qa_detail(build_id: str, qa_id: str, x_subject: str | None = Header(default=None, alias="X-Subject")):
    return _qa_read(build_id, qa_id, x_subject)


@router.get("/builds/{build_id}/qa/{qa_id}/report")
def qa_report(build_id: str, qa_id: str, x_subject: str | None = Header(default=None, alias="X-Subject")):
    return _qa_read(build_id, qa_id, x_subject, "report.json")


@router.get("/builds/{build_id}/qa/{qa_id}/evidence/{evidence_id}")
def qa_evidence(build_id: str, qa_id: str, evidence_id: str, x_subject: str | None = Header(default=None, alias="X-Subject")):
    return _qa_read(build_id, qa_id, x_subject, evidence_id)


@router.get("/apps/{app_id}/site")
def site(
    app_id: str,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    app = _app_or_404(app_id)
    assert_venture_access(app.venture_id, x_subject)
    try:
        return get_site_by_app(app_id)
    except KeyError as exc:
        raise HTTPException(404, detail={"code": "site_not_created"}) from exc


@router.get("/apps/{app_id}/deployments")
def deployments(
    app_id: str,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    app = _app_or_404(app_id)
    assert_venture_access(app.venture_id, x_subject)
    from app.hosting.service import list_deployments

    return {"items": list_deployments(app_id)}


@router.get("/apps/{app_id}/deployments/{deployment_id}/public-copy")
def published_copy_audit(
    app_id: str,
    deployment_id: str,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    app = _app_or_404(app_id)
    assert_venture_access(app.venture_id, x_subject)
    from app.hosting.service import HostingError, audit_published_copy

    try:
        return audit_published_copy(app_id, deployment_id)
    except HostingError as exc:
        from app.core.local_mode import is_local
        if is_local():
            raise HTTPException(exc.status, detail=exc.code if exc.code.startswith(("Open ", "Your ")) else "Open Website and review your saved page, then check Settings > Keys and connections > Publishing before trying again.") from None
        raise HTTPException(exc.status, detail={"code": exc.code}) from exc


@router.get("/apps/{app_id}/site/revisions")
def site_revisions(app_id: str, x_subject: str | None = Header(default=None, alias="X-Subject")):
    from app.hosting.revisions import revision_status
    from app.hosting.service import HostingError
    try:
        return revision_status(app_id, actor=subject_person_id(x_subject))
    except HostingError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code}) from exc


@router.post("/apps/{app_id}/site/revisions", status_code=202)
def request_site_revision(app_id: str, body: SiteRevisionBody,
                          x_subject: str | None = Header(default=None, alias="X-Subject")):
    from app.hosting.revisions import request_revision
    from app.hosting.service import HostingError
    try:
        return request_revision(app_id, actor=subject_person_id(x_subject), **body.model_dump())
    except HostingError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code}) from exc


@router.post("/apps/{app_id}/site/revisions/estimate")
def estimate_site_revision(app_id: str, body: EstimateSiteRevisionBody,
                           x_subject: str | None = Header(default=None, alias="X-Subject")):
    return _managed(managed_builds.estimate_revision, app_id=app_id,
                    actor=subject_person_id(x_subject), **body.model_dump())


@router.post("/apps/{app_id}/deployments", status_code=202)
def publish_static_build(
    app_id: str,
    body: PublishStaticBody,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    app = _app_or_404(app_id)
    assert_venture_access(app.venture_id, x_subject)
    from app.hosting.service import HostingError, queue_accepted_build

    try:
        return queue_accepted_build(
            app_id,
            body.build_id,
            actor=subject_person_id(x_subject),
        )
    except HostingError as exc:
        from app.core.local_mode import is_local

        if is_local():
            raise HTTPException(exc.status, detail=exc.code if exc.code.startswith(("Open ", "Your ", "In Settings > ")) else "Open Website and review your saved page, then check Settings > Keys and connections > Publishing before trying again.") from None
        raise HTTPException(exc.status, detail={"code": exc.code}) from exc


@router.post("/apps/{app_id}/deployments/{deployment_id}/unpublish", status_code=202)
def unpublish_static_deployment(app_id: str, deployment_id: str,
    x_subject: str | None = Header(default=None, alias="X-Subject")):
    app = _app_or_404(app_id)
    assert_venture_access(app.venture_id, x_subject)
    from app.hosting.unpublish import queue
    from app.hosting.service import HostingError
    try:
        return queue(app_id, deployment_id, actor=subject_person_id(x_subject))
    except HostingError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code}) from exc


@router.post("/apps/{app_id}/site/versions/{version}/publish")
def publish_version(
    app_id: str,
    version: str,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    return _service.apps_publish_version(app_id, version, x_subject)


@router.get("/apps/{app_id}/site/leads")
def site_leads(
    app_id: str,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    app = _app_or_404(app_id)
    assert_venture_access(app.venture_id, x_subject)
    site = get_site_by_app(app_id)
    return {"items": list_leads(site["id"])}


@router.post("/apps/{app_id}/site/leads")
def add_lead(
    app_id: str,
    body: LeadBody,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    app = _app_or_404(app_id)
    assert_venture_access(app.venture_id, x_subject)
    site = get_site_by_app(app_id)
    return capture_lead(site["id"], body.email, body.fields)


@router.post("/ingest/{analytics_key}")
def ingest(analytics_key: str, body: IngestBody):
    ingest_visit(
        analytics_key,
        {
            "path": body.path,
            "session": body.session,
            "referrer_host": body.referrer_host,
            "utm": body.utm,
            "event": body.event,
            "country": body.country,
            "device": body.device,
            "value": body.value,
        },
    )
    return {"ok": True}


@router.post("/apps/{app_id}/site/rollback")
def site_rollback(
    app_id: str,
    version: str = Query(...),
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    app = _app_or_404(app_id)
    assert_venture_access(app.venture_id, x_subject)
    site = get_site_by_app(app_id)
    try:
        return rollback(site["id"], version)
    except FileNotFoundError as exc:
        raise HTTPException(404, detail={"error": {"code": "version_missing"}}) from exc


class IterateBody(BaseModel):
    text: str


class IncidentBody(BaseModel):
    summary: str
    kind: str = "5xx"
    log_line: str | None = None


class KeyBody(BaseModel):
    scopes: list[str] | None = None
    monthly_cap_credits: int | None = None


@router.get("/apps/{app_id}/incidents")
def incidents(
    app_id: str,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    app = _app_or_404(app_id)
    assert_venture_access(app.venture_id, x_subject)
    from app.debug.service import list_incidents

    return {"items": list_incidents(app_id)}


@router.post("/apps/{app_id}/incidents", status_code=201)
def open_app_incident(
    app_id: str,
    body: IncidentBody,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    app = _app_or_404(app_id)
    assert_venture_access(app.venture_id, x_subject)
    from app.debug.service import append_log, open_incident

    if body.log_line:
        append_log(app_id, body.log_line)
    return open_incident(app_id, app.workspace_id, summary=body.summary, kind=body.kind)


@router.post("/incidents/{incident_id}/debug")
def debug_incident(
    incident_id: str,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    from app.debug.service import get_incident, start_debug

    try:
        data = get_incident(incident_id)
    except KeyError as exc:
        raise HTTPException(404, detail={"error": {"code": "not_found"}}) from exc
    app = _app_or_404(data["app_id"])
    assert_venture_access(app.venture_id, x_subject)
    return start_debug(incident_id)


@router.post("/apps/{app_id}/iterate")
def iterate(
    app_id: str,
    body: IterateBody,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    app = _app_or_404(app_id)
    assert_venture_access(app.venture_id, x_subject)
    from app.iterate.service import request_iteration

    return request_iteration(app_id, body.text)


@router.get("/apps/{app_id}/api-keys")
def list_api_keys(
    app_id: str,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    app = _app_or_404(app_id)
    assert_venture_access(app.venture_id, x_subject)
    from app.apps.keys import list_keys

    return {"items": list_keys(app_id)}


@router.post("/apps/{app_id}/api-keys", status_code=201)
def create_api_key(
    app_id: str,
    body: KeyBody,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    app = _app_or_404(app_id)
    assert_venture_access(app.venture_id, x_subject)
    from app.apps.keys import create_key

    return create_key(app_id, scopes=body.scopes, monthly_cap_credits=body.monthly_cap_credits)


@router.post("/apps/{app_id}/api-keys/{key_id}/revoke")
def revoke_api_key(
    app_id: str,
    key_id: str,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    app = _app_or_404(app_id)
    assert_venture_access(app.venture_id, x_subject)
    from app.apps.keys import revoke_key

    try:
        return revoke_key(key_id)
    except KeyError as exc:
        raise HTTPException(404, detail={"error": {"code": "not_found"}}) from exc


@router.post("/apps/{app_id}/ingest-key", status_code=201)
def ingest_key(
    app_id: str,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    app = _app_or_404(app_id)
    assert_venture_access(app.venture_id, x_subject)
    from app.apps.keys import ensure_ingest_key

    return ensure_ingest_key(app_id)


@router.get("/apps/{app_id}/analytics/overview")
def analytics_overview(
    app_id: str,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    app = _app_or_404(app_id)
    assert_venture_access(app.venture_id, x_subject)
    from app.product_analytics.service import overview

    return overview(app_id)


@router.get("/apps/{app_id}/analytics/funnel")
def analytics_funnel(
    app_id: str,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    app = _app_or_404(app_id)
    assert_venture_access(app.venture_id, x_subject)
    from app.product_analytics.service import funnel

    return funnel(app_id)


@router.get("/apps/{app_id}/analytics/events")
def analytics_events(
    app_id: str,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    app = _app_or_404(app_id)
    assert_venture_access(app.venture_id, x_subject)
    from app.product_analytics.service import events

    return events(app_id)


@router.get("/apps/{app_id}/analytics/retention")
def analytics_retention(
    app_id: str,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    app = _app_or_404(app_id)
    assert_venture_access(app.venture_id, x_subject)
    from app.product_analytics.service import retention

    return retention(app_id)


@router.get("/apps/{app_id}/analytics/revenue")
def analytics_revenue(
    app_id: str,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    app = _app_or_404(app_id)
    assert_venture_access(app.venture_id, x_subject)
    from app.product_analytics.service import revenue

    return revenue(app_id)


@router.get("/apps/{app_id}/analytics/health")
def analytics_health(
    app_id: str,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    app = _app_or_404(app_id)
    assert_venture_access(app.venture_id, x_subject)
    from app.product_analytics.service import health

    return health(app_id)


@router.get("/apps/{app_id}/analytics/ai")
def analytics_ai(
    app_id: str,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    app = _app_or_404(app_id)
    assert_venture_access(app.venture_id, x_subject)
    from app.product_analytics.service import ai_usage

    return ai_usage(app_id)


class BuildReviewBody(BaseModel):
    model_config = {"extra": "forbid", "strict": True}
    source_artifact_id: str = Field(min_length=1, max_length=128)
    report_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    decision: str = Field(pattern=r"^(accept|reject)$")
    feedback: str | None = Field(default=None, max_length=2000)


@router.post("/builds/{build_id}/review")
def review_build(build_id: str, body: BuildReviewBody, x_subject: str | None = Header(default=None, alias="X-Subject")):
    return _managed(managed_builds.review, run_id=build_id, actor=subject_person_id(x_subject), **body.model_dump())


class PrivateCorrectionBody(BaseModel):
    model_config = {"extra": "forbid", "strict": True}
    source_artifact_id: str = Field(min_length=1, max_length=128)
    report_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    instruction: str = Field(min_length=8, max_length=2000)


@router.get("/builds/{build_id}/corrections/availability")
def private_correction_availability(build_id: str, x_subject: str | None = Header(default=None, alias="X-Subject")):
    from app.builder.private_corrections import availability
    return _managed(availability, build_id=build_id, actor=subject_person_id(x_subject))


@router.post("/builds/{build_id}/corrections", status_code=202)
def request_private_correction(build_id: str, body: PrivateCorrectionBody,
                               x_subject: str | None = Header(default=None, alias="X-Subject")):
    from app.builder.private_corrections import request
    return _managed(request, build_id=build_id, actor=subject_person_id(x_subject), **body.model_dump())


@router.post("/builds/{build_id}/corrections/estimate")
def estimate_private_correction(build_id: str, body: EstimateSiteRevisionBody,
                                x_subject: str | None = Header(default=None, alias="X-Subject")):
    actor = subject_person_id(x_subject)
    from app.builder.private_corrections import estimate
    return _managed(estimate, build_id=build_id, actor=actor, request_id=body.revision_request_id)

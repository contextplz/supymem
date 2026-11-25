"""Durable, exact-deployment static takedown; source and objects are retained."""
from __future__ import annotations

from sqlalchemy import select
from core.ids import new_id
from core.hosting_contracts import host_mapping, public_hostname
from app.builder.access import authorize
from app.core.jobs import ACTIVE_JOB_STATUSES, enqueue
from app.db.models import Job
from app.db.session import serialize_writes
from app.db.supymem_models import AppDomain, BuildArtifact, BuildRun, Deployment, FounderApp
from app.hosting.service import AwsStaticPublisher, HostingConfig, HostingError, _json, _serialize
from app.state import db


def intents(session, app: FounderApp):
    rows = session.scalars(select(Job).where(Job.kind == "deploy.unpublish",
        Job.workspace_id == app.workspace_id, Job.venture_id == app.venture_id)
        .order_by(Job.created_at.desc())).all()
    return [row for row in rows if _json(row.payload_json).get("app_id") == app.id]


def assert_no_takedown(session, app):
    if any(row.status in ACTIVE_JOB_STATUSES for row in intents(session, app)):
        raise HostingError("hosting_unpublish_in_progress")


def receipt(session, app, deployment_id):
    row = next((row for row in intents(session, app)
                if _json(row.payload_json).get("deployment_id") == deployment_id), None)
    if not row:
        return None
    return {"job_id": row.id, "state": row.status,
            "error": "hosting_unpublish_verification_pending" if row.last_error else None}


def _owned(session, app_id, deployment_id):
    app = session.get(FounderApp, app_id)
    dep = session.get(Deployment, deployment_id)
    if (not app or app.deleted_at is not None or not dep
        or (dep.app_id, dep.workspace_id, dep.venture_id) != (app.id, app.workspace_id, app.venture_id)
        or dep.target != "supymem_static"):
        raise HostingError("hosting_deployment_not_found", status=404)
    return app, dep


def _authorize(session, app, actor):
    try:
        authorize(session, workspace_id=app.workspace_id, venture_id=app.venture_id, actor=actor, mutate=True)
    except KeyError as exc:
        raise HostingError("hosting_app_not_found", status=404) from exc
    except PermissionError as exc:
        raise HostingError(str(exc), status=403) from exc


def _firstlook_baseline(session, app, dep, config):
    """A firstlook concept page has no build/QA record; its exact source is the recorded SiteVersion."""
    from app.db.models import SiteVersion

    if app.build_mode != "firstlook":
        raise HostingError("hosting_unpublish_baseline_unverified")
    source_ids, seen = [], set()
    current = dep
    while current is not None:
        if (current.id in seen or current.target != "supymem_static"
                or (current.app_id, current.workspace_id, current.venture_id)
                != (app.id, app.workspace_id, app.venture_id)
                or not str(current.build_artifact_id or "").startswith("firstlook:")):
            raise HostingError("hosting_unpublish_baseline_unverified")
        seen.add(current.id)
        version_id = current.build_artifact_id.removeprefix("firstlook:")
        site = session.get(SiteVersion, version_id)
        if not site or (site.workspace_id, site.venture_id) != (app.workspace_id, app.venture_id):
            raise HostingError("hosting_unpublish_baseline_unverified")
        if version_id not in source_ids:
            source_ids.append(version_id)
        if not current.previous_deployment_id:
            break
        current = session.get(Deployment, current.previous_deployment_id)
        if current is None:
            raise HostingError("hosting_unpublish_baseline_unverified")
    try:
        mapping = host_mapping(app_id=app.id, deployment_id=dep.id, generation=dep.version, enabled=True)
        hostname = _serving_host(session, app, dep, config)
    except (KeyError, ValueError, TypeError) as exc:
        raise HostingError("hosting_unpublish_baseline_unverified") from exc
    if dep.url != f"https://{hostname}/":
        raise HostingError("hosting_unpublish_baseline_changed")
    return {"schema_version": 1, "app_id": app.id, "deployment_id": dep.id,
            "version": dep.version, "hostname": hostname, "mapping": mapping,
            "firstlook_source_ids": source_ids}


def _serving_host(session, app, dep, config) -> str:
    """The host this deployment is live on: the address, or a retired one still serving it after an address change."""
    from app.hosting.site_address import aliases

    hostname = public_hostname(app.slug, config.public_domain)
    return next((h for h in aliases(app.id, session) if dep.url == f"https://{h}/" != f"https://{hostname}/"), hostname)


def _baseline(session, app, dep, config):
    if str(dep.build_artifact_id or "").startswith("firstlook:"):
        return _firstlook_baseline(session, app, dep, config)
    artifact = session.get(BuildArtifact, dep.build_artifact_id) if dep.build_artifact_id else None
    run = session.get(BuildRun, artifact.build_run_id) if artifact else None
    capture = _json(run.spec).get("artifact_capture", {}) if run else {}
    if (not artifact or not run
        or (artifact.app_id, artifact.workspace_id) != (app.id, app.workspace_id)
        or (run.app_id, run.workspace_id, run.venture_id) != (app.id, app.workspace_id, app.venture_id)
        or capture.get("artifact_id") != artifact.id or capture.get("sha256") != artifact.sha256):
        raise HostingError("hosting_unpublish_baseline_unverified")
    try:
        mapping = host_mapping(app_id=app.id, deployment_id=dep.id,
                               generation=_json(run.spec).get("generation"), enabled=True)
        hostname = _serving_host(session, app, dep, config)
    except (KeyError, ValueError, TypeError) as exc:
        raise HostingError("hosting_unpublish_baseline_unverified") from exc
    if dep.url != f"https://{hostname}/":
        raise HostingError("hosting_unpublish_baseline_changed")
    return {"schema_version": 1, "app_id": app.id, "deployment_id": dep.id,
            "version": dep.version, "hostname": hostname, "mapping": mapping}


def queue(app_id: str, deployment_id: str, *, actor: str):
    config = HostingConfig.from_environment()
    with db()() as session:
        serialize_writes(session, key="static-publish:" + app_id)
        app, dep = _owned(session, app_id, deployment_id)
        _authorize(session, app, actor)
        if dep.state == "unpublished":
            return {**_serialize(dep), "unpublish": receipt(session, app, dep.id)}
        current = session.scalars(select(Deployment).where(Deployment.app_id == app.id,
                                                          Deployment.state == "live")).all()
        if dep.state != "live" or len(current) != 1 or current[0].id != dep.id:
            raise HostingError("hosting_unpublish_baseline_changed")
        busy = session.scalar(select(Deployment.id).where(Deployment.app_id == app.id,
            Deployment.state.in_(("pending", "packaging", "pushing", "creating_infra", "health_checking"))))
        uncertain = session.scalar(select(Deployment.id).where(Deployment.app_id == app.id,
            Deployment.error == "hosting_public_state_uncertain"))
        if busy or uncertain:
            raise HostingError("hosting_publication_in_progress")
        previous = next((row for row in intents(session, app) if row.status in ACTIVE_JOB_STATUSES), None)
        if previous:
            if _json(previous.payload_json).get("deployment_id") != dep.id:
                raise HostingError("hosting_unpublish_in_progress")
            return {**_serialize(dep), "unpublish": receipt(session, app, dep.id)}
        payload = {**_baseline(session, app, dep, config), "actor": actor}
        # A failed exhausted job gets a fresh retry intent; the effect identity
        # remains the exact deployment, never a future publication on this host.
        intent = new_id("off")
        payload["intent_id"] = intent
        job_id = enqueue(session, "deploy.unpublish", payload,
            workspace_id=app.workspace_id, venture_id=app.venture_id,
            dedupe_key="static-unpublish:" + intent, effect_key="static-unpublish:" + dep.id,
            priority=30, max_attempts=5)
        session.commit()
        result = {**_serialize(dep), "unpublish": {"job_id": job_id, "state": "scheduled", "error": None}}
        scope = app.workspace_id, app.venture_id
    from app.audit.writer import audit
    audit("hosting.unpublish_queued", workspace_id=scope[0], venture_id=scope[1],
          actor_id=actor, actor_kind="person", surface="api", target_ref=deployment_id,
          payload={"app_id": app_id, "job_id": job_id})
    return result


def execute(payload: dict, *, provider=None):
    provider = provider or AwsStaticPublisher.configured()
    app_id, dep_id = payload["app_id"], payload["deployment_id"]
    with db()() as session:
        serialize_writes(session, key="static-publish:" + app_id)
        app, dep = _owned(session, app_id, dep_id)
        intent = next((row for row in intents(session, app)
                       if _json(row.payload_json).get("intent_id") == payload.get("intent_id")), None)
        frozen = _json(intent.payload_json) if intent else {}
        if (not intent or any(payload.get(key) != value for key, value in frozen.items())
            or (dep.state != "unpublished" and intent.status not in ACTIVE_JOB_STATUSES)):
            raise HostingError("hosting_unpublish_intent_invalid")
        _authorize(session, app, frozen.get("actor"))
        if dep.state == "unpublished":
            return _serialize(dep)
        expected = _baseline(session, app, dep, provider.config)
        if (dep.state != "live" or any(payload.get(key) != value for key, value in expected.items())
            or payload.get("workspace_id") != app.workspace_id or payload.get("venture_id") != app.venture_id):
            raise HostingError("hosting_unpublish_baseline_changed")
    provider.disable(payload["hostname"], payload["mapping"])
    provider.verify_offline(payload["hostname"], payload["mapping"])
    from app.hosting.site_address import aliases

    for alias_host in (h for h in aliases(app_id) if h != payload["hostname"]):  # retired addresses go offline too
        provider.disable(alias_host, payload["mapping"])
        provider.verify_offline(alias_host, payload["mapping"])
    with db()() as session:
        serialize_writes(session, key="static-publish:" + app_id)
        app, dep = _owned(session, app_id, dep_id)
        if dep.state == "unpublished":
            return _serialize(dep)
        if dep.state != "live" or _baseline(session, app, dep, provider.config) != expected:
            raise HostingError("hosting_unpublish_baseline_changed")
        # Re-read provider custody after public readback. Never mark a later
        # publication offline or claim completion from a queued acknowledgement.
        provider.verify_disabled_mapping(payload["hostname"], payload["mapping"])
        dep.state, dep.error = "unpublished", None
        if str(dep.build_artifact_id or "").startswith("firstlook:"):
            from app.db.models import SiteVersion

            for source_id in expected["firstlook_source_ids"]:
                site = session.get(SiteVersion, source_id)
                site.published = 0  # verified offline includes this app's superseded public versions
        domain = session.scalar(select(AppDomain).where(AppDomain.app_id == app.id, AppDomain.kind == "supymem"))
        if domain and domain.hostname == payload["hostname"]:
            domain.state = "disabled"
        session.commit()
        result = _serialize(dep)
        scope = app.workspace_id, app.venture_id
    from app.audit.writer import audit
    audit("hosting.unpublished", workspace_id=scope[0], venture_id=scope[1],
          actor_id=payload["actor"], actor_kind="person", surface="internal", target_ref=dep_id,
          payload={"app_id": app_id, "hostname": payload["hostname"], "verified_offline": True})
    return result

"""Reviewed static-app selection and immutable previous publication identity."""
from __future__ import annotations

import json
import os
from urllib.parse import urlsplit

from sqlalchemy import select

from app.db.supymem_models import BuildArtifact, BuildRun, Deployment, FounderApp


def _live(session, app):
    rows = session.scalars(select(Deployment).where(
        Deployment.app_id == app.id, Deployment.workspace_id == app.workspace_id,
        Deployment.venture_id == app.venture_id, Deployment.state == "live",
    ).order_by(Deployment.version.desc())).all()
    if len(rows) > 1 or any(row.target != "supymem_static" for row in rows):
        raise ValueError("subscription_static_publication_baseline_ambiguous")
    return rows[0] if rows else None


def _hostname(app):
    from core.hosting_contracts import public_hostname
    return public_hostname(app.slug, os.environ.get("PUBLIC_SITE_DOMAIN", "supymem.com"))


def _identity(session, app, deployment):
    hostname = _hostname(app)
    result = {"schema_version": 1, "app_id": app.id, "hostname": hostname,
              "deployment_id": None, "deployment_version": None,
              "source_artifact_id": None, "source_sha256": None,
              "source_manifest_digest": None, "previous_mapping": None}
    if deployment is None:
        return result
    if ((deployment.app_id, deployment.workspace_id, deployment.venture_id)
            != (app.id, app.workspace_id, app.venture_id)
            or deployment.target != "supymem_static" or not deployment.live_at):
        raise ValueError("subscription_static_publication_baseline_unverified")
    url = urlsplit(deployment.url or "")
    from app.hosting.site_address import aliases

    if url.scheme == "https" and url.netloc != hostname and url.netloc in aliases(app.id, session):
        # Live at a retired address that still serves it: the new address has no mapping yet.
        return {**result, "serving_deployment_id": deployment.id}
    if (url.scheme != "https" or url.netloc != hostname or url.path not in ("", "/")
            or url.query or url.fragment):
        raise ValueError("subscription_static_publication_baseline_unverified")
    artifact = session.get(BuildArtifact, deployment.build_artifact_id) if deployment.build_artifact_id else None
    if artifact is None:
        raise ValueError("subscription_static_publication_baseline_unverified")
    # Historical published custody/QA does not inherit a newly superseded plan.
    from app.hosting.service import _accepted_source, HostingError
    try:
        run, accepted, _qa, capture, _manifest = _accepted_source(
            session, app, artifact.build_run_id, historical=True)
    except HostingError as error:
        raise ValueError("subscription_static_publication_baseline_unverified") from error
    if accepted.id != artifact.id or json.loads(run.spec).get("artifact_profile") not in (
            None, "static_landing_v1"):
        raise ValueError("subscription_static_publication_baseline_unverified")
    from core.hosting_contracts import host_mapping
    try:
        previous_mapping = host_mapping(app_id=app.id, deployment_id=deployment.id,
                                        generation=capture["generation"], enabled=True)
    except (ValueError, TypeError, KeyError) as error:
        raise ValueError("subscription_static_publication_baseline_unverified") from error
    return {**result, "previous_mapping": previous_mapping, "deployment_id": deployment.id,
            "deployment_version": deployment.version, "source_artifact_id": artifact.id,
            "source_sha256": artifact.sha256, "source_manifest_digest": capture["manifest_digest"]}


def reviewed(session, workspace_id, venture_id, *, failed_run_id=None):
    apps = session.scalars(select(FounderApp).where(
        FounderApp.workspace_id == workspace_id, FounderApp.venture_id == venture_id,
        FounderApp.deleted_at.is_(None))).all()
    selected = None
    if not apps:
        # Local coding can be reviewed before an app exists. Source custody/QA
        # require a bound app and therefore a new review after one is created.
        return {"schema_version": 1, "app_id": None, "hostname": None,
                "deployment_id": None, "deployment_version": None,
                "source_artifact_id": None, "source_sha256": None,
                "source_manifest_digest": None, "previous_mapping": None}
    if failed_run_id:
        failed = session.get(BuildRun, failed_run_id)
        if failed and (failed.workspace_id, failed.venture_id) == (workspace_id, venture_id):
            selected = next((app for app in apps if app.id == failed.app_id), None)
    if selected is None:
        published = [app for app in apps if _live(session, app) is not None]
        if len(published) == 1:
            selected = published[0]
        elif not published and len(apps) == 1:
            selected = apps[0]
    if selected is None:
        raise ValueError("subscription_static_app_ambiguous")
    return _identity(session, selected, _live(session, selected))


def assert_current(session, app, baseline, *, accepted_artifact_id=None):
    """Keep the reviewed predecessor or our exact idempotent publication."""
    if (not isinstance(baseline, dict) or baseline.get("schema_version") != 1
            or baseline.get("app_id") != app.id or baseline.get("hostname") != _hostname(app)):
        raise ValueError("subscription_static_publication_baseline_changed")
    prior_id = baseline.get("serving_deployment_id") or baseline.get("deployment_id")
    prior = session.get(Deployment, prior_id) if prior_id else None
    if prior_id and prior is None:
        raise ValueError("subscription_static_publication_baseline_changed")
    if _identity(session, app, prior) != baseline:
        raise ValueError("subscription_static_publication_baseline_changed")
    live = _live(session, app)
    if live and accepted_artifact_id and live.build_artifact_id == accepted_artifact_id:
        if urlsplit(live.url or "").netloc != baseline["hostname"]:
            raise ValueError("subscription_static_publication_baseline_changed")
        return
    if (live.id if live else None) != prior_id:
        raise ValueError("subscription_static_publication_baseline_changed")


def snapshot(session, plan, snapshot, *, source_import_run_id=None):
    if source_import_run_id:
        run = session.get(BuildRun, source_import_run_id)
        spec = json.loads(run.spec or "{}") if run else {}
        baseline = spec.get("inputs", {}).get("publication_baseline")
        app = session.get(FounderApp, run.app_id) if run else None
        if (run is None or app is None or app.deleted_at is not None
                or run.trigger != "coding_source_import" or spec.get("lifecycle_version") != 3
                or (run.workspace_id, run.venture_id, run.plan_task_id)
                != (plan.workspace_id, plan.venture_id, snapshot["task_id"])):
            raise ValueError("subscription_static_publication_baseline_changed")
        assert_current(session, app, baseline, accepted_artifact_id=run.artifact_id)
    else:
        baseline = reviewed(session, plan.workspace_id, plan.venture_id,
                            failed_run_id=snapshot.get("closed_failed_build_run_id"))
    return {**snapshot, "app_id": baseline["app_id"], "publication_baseline": baseline}

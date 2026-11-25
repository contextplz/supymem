"""A landing-page copy request is bound to a live tenant source, never public bytes."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from tests.integration.test_postgres_build_review_readiness import no_dml_or_locks

from tests.integration.test_postgres_managed_lifecycle import (
    journey_db,  # noqa: F401
    lifecycle_run as lifecycle_run,
    pg_database,  # noqa: F401
    planning,  # noqa: F401
    ready_plan,  # noqa: F401
    step,
)
from tests.integration.test_postgres_static_publication import (
    _accept,
    _hosting_environment,
    _verified_waitlist,
)


def assert_review_read_only(d, build_id):
    """The real public/private revision GET cannot create or lock ingest keys."""
    from app.builder.execution import read
    from app.db.supymem_models import AppApiKey
    with no_dml_or_locks(d['engine']):
        view = read(build_id, actor=d['actor'])
    assert view['review_readiness']['status'] == 'ready', view['review_readiness']
    with d['factory']() as session:
        keys = session.scalars(select(AppApiKey).where(AppApiKey.app_id == d['app_id'],
            AppApiKey.workspace_id == d['wid'], AppApiKey.prefix.startswith('pk_app_'), AppApiKey.state == 'active')).all()
        assert keys
        ids = [key.id for key in keys]
        for key in keys:
            key.state = 'revoked'
        count = session.query(AppApiKey).filter_by(app_id=d['app_id']).count()
        session.commit()
    with no_dml_or_locks(d['engine']):
        missing = read(build_id, actor=d['actor'])
    assert missing['review_readiness']['status'] == 'check_unavailable'
    assert missing['review_readiness']['code'] == 'build_review_check_unavailable'
    assert missing['review'] == view['review'] and missing['usage'] == view['usage']
    with d['factory']() as session:
        assert session.query(AppApiKey).filter_by(app_id=d['app_id']).count() == count
        for key_id in ids:
            session.get(AppApiKey, key_id).state = 'active'
        session.commit()


def test_revision_intake_is_source_bound_durable_and_does_not_publish(lifecycle_run, monkeypatch):
    from app.db.supymem_models import BuildArtifact, Deployment, FounderRequest
    from app.hosting.revisions import request_revision, revision_status
    from app.hosting.service import HostingError, queue_accepted_build

    d = _accept(lifecycle_run)
    _hosting_environment(monkeypatch)
    _verified_waitlist(d)
    queued = queue_accepted_build(d["app_id"], d["build_run_id"], actor=d["actor"])
    with pytest.raises(HostingError, match="hosting_live_static_site_required"):
        revision_status(d["app_id"], actor=d["actor"])
    with d["factory"]() as session:
        deployment = session.get(Deployment, queued["id"])
        deployment.state = "live"
        artifact = session.get(BuildArtifact, deployment.build_artifact_id)
        digest = artifact.sha256
        session.commit()

    baseline = revision_status(d["app_id"], actor=d["actor"])["baseline"]
    assert baseline == {"deployment_id": queued["id"], "build_id": d["build_run_id"],
                        "source_artifact_id": artifact.id, "source_sha256": digest}
    instruction = "Remove the outdated footer instruction. Preserve the existing waitlist CTA."
    first = request_revision(d["app_id"], actor=d["actor"], instruction=instruction,
                             deployment_id=queued["id"], source_sha256=digest)
    second = request_revision(d["app_id"], actor=d["actor"], instruction=instruction,
                              deployment_id=queued["id"], source_sha256=digest)
    assert first == second
    assert first["state"] == "pending"
    assert revision_status(d["app_id"], actor=d["actor"])["requests"] == [first]
    with d["factory"]() as session:
        assert session.get(Deployment, queued["id"]).state == "live"
        assert session.get(FounderRequest, first["id"]).source == "website_revision"
        assert session.get(BuildArtifact, artifact.id).sha256 == digest

    with pytest.raises(HostingError, match="hosting_revision_baseline_changed"):
        request_revision(d["app_id"], actor=d["actor"], instruction=instruction,
                         deployment_id=queued["id"], source_sha256="0" * 64)
    with pytest.raises(HostingError, match="hosting_revision_baseline_changed"):
        request_revision(d["app_id"], actor=d["actor"], instruction=instruction,
                         deployment_id="dep_stale", source_sha256=digest)
    with pytest.raises(HostingError, match="hosting_app_not_found"):
        revision_status(d["app_id"], actor="person_other_tenant")


def test_revision_reuses_verified_static_lifecycle_without_reopening_done_plan(lifecycle_run, monkeypatch):
    from app.builder import execution
    from app.db.supymem_models import BuildArtifact, BuildRun, Deployment, PlanTask
    from app.hosting.revisions import request_revision
    from app.hosting.service import HostingConfig, publish_accepted_build, queue_accepted_build

    d = _accept(lifecycle_run)
    _hosting_environment(monkeypatch)
    _verified_waitlist(d)
    published = queue_accepted_build(d["app_id"], d["build_run_id"], actor=d["actor"])
    with d["factory"]() as session:
        live = session.get(Deployment, published["id"])
        live.state = "live"
        original = session.get(BuildRun, d["build_run_id"])
        task = session.get(PlanTask, original.plan_task_id)
        assert task.state == "done" and task.build_run_id == original.id
        # Accepted sites published before artifact_profile existed must remain
        # editable through a new reviewed revision, as with the live LabHire page.
        original_spec = json.loads(original.spec)
        original_spec.pop("artifact_profile")
        original.spec = json.dumps(original_spec)
        source_sha = session.get(BuildArtifact, live.build_artifact_id).sha256
        session.commit()
    request = request_revision(d["app_id"], actor=d["actor"],
        instruction="Remove the obsolete footer and preserve the managed waitlist link.",
        deployment_id=published["id"], source_sha256=source_sha)
    quote = execution.estimate_revision(d["app_id"], actor=d["actor"], revision_request_id=request["id"])
    assert quote["ready"], quote
    started = execution.start(d["app_id"], actor=d["actor"], quote_id=quote["quote_id"],
                              input_digest=quote["input_digest"], approve_budget_micro=quote["ceiling_micro"],
                              idempotency_key="revision-1")
    with d["factory"]() as session:
        revised = session.get(BuildRun, started["build_run_id"])
        task = session.get(PlanTask, revised.plan_task_id)
        assert task.state == "done" and task.build_run_id == d["build_run_id"]
        spec = json.loads(revised.spec)
        assert spec["inputs"]["source"]["kind"] == "artifact"
        assert spec["inputs"]["revision"]["request_id"] == request["id"]
        assert "data-supymem-analytics" in spec["inputs"]["product_context"]["analytics_beacon_script"]

    # Existing sealed builder and QA fixtures remain the execution path. The
    # revision source is seeded; the completed plan task is not reused as a
    # mutable work queue row.
    seeded = []
    monkeypatch.setattr(d["m"].task_client, "seed_source", lambda *args, **kwargs: seeded.append((args, kwargs)))
    # The shared fixture has the first build's SDK snapshot in memory. A new
    # build gets its own sandbox event stream, so expose only its new event.
    prior_reader = d["m"].intent_bridge.read_events
    monkeypatch.setattr(d["m"].intent_bridge, "read_events", lambda *args: prior_reader(*args)[-1:])
    d["commands"].clear()
    d["qa_state"].clear()
    d["qa_state"]["phase"] = "running"
    revised_d = {**d, "build_run_id": started["build_run_id"]}
    step(revised_d, "build.run")
    assert d["commands"]
    d["commands"][0].update(status="completed", driver_status="finished")
    for _ in range(5):
        value = step(revised_d)
        if value["spec"].get("phase") == "qa_ready":
            break
    assert value["spec"].get("phase") == "qa_ready", {
        "state": value["state"], "phase": value["spec"].get("phase"),
        "error": value["spec"].get("artifact_capture_error"),
    }
    assert seeded and seeded[0][1]["source_artifact_id"]
    step(revised_d)
    revised_d["qa_state"]["phase"] = "completed"
    step(revised_d)
    completed = step(revised_d)
    assert completed["closed"] and completed["state"] == "succeeded"
    view = execution.read(started["build_run_id"], actor=d["actor"])
    assert view["qa_state"] == "passed" and view["review"]["status"] == "pending"
    assert view["revision_instruction"] == request["instruction"]
    assert_review_read_only(d, started['build_run_id'])
    execution.review(started["build_run_id"], actor=d["actor"],
                     source_artifact_id=view["artifact_id"],
                     report_digest=view["review"]["report_digest"], decision="accept")
    newer = queue_accepted_build(d["app_id"], started["build_run_id"], actor=d["actor"])
    assert newer["id"] != published["id"]

    class Provider:
        config = HostingConfig.from_environment()

        def __init__(self):
            self.switched = []

        def put_files(self, **_kwargs):
            pass

        def switch(self, hostname, mapping, *, expected_previous_deployment_id=None):
            self.switched.append((hostname, mapping, expected_previous_deployment_id))
            return None

        def verify_public(self, *_args):
            pass

    provider = Provider()
    live_revision = publish_accepted_build(newer["id"], d["app_id"], started["build_run_id"],
                                            actor=d["actor"], provider=provider)
    assert live_revision["state"] == "live"
    assert len(provider.switched) == 1
    assert provider.switched[0][2] == published["id"]
    with d["factory"]() as session:
        original_task = session.get(PlanTask, revised.plan_task_id)
        assert original_task.state == "done" and original_task.build_run_id == d["build_run_id"]


def test_private_correction_runs_qa_review_and_publication_without_reopening_history(lifecycle_run, monkeypatch):
    from app.builder import execution
    from app.db.supymem_models import BuildRun, Deployment, PlanTask
    from app.hosting.service import HostingConfig, publish_accepted_build, queue_accepted_build

    d = _accept(lifecycle_run)
    from app.builder.private_corrections import request as correction_request
    before = execution.read(d["build_run_id"], actor=d["actor"])
    request = correction_request(d["build_run_id"], actor=d["actor"],
        source_artifact_id=before["artifact_id"], report_digest=before["review"]["report_digest"],
        instruction="Remove the obsolete footer and preserve the managed waitlist link.")
    quote = execution.estimate_revision(d["app_id"], actor=d["actor"], revision_request_id=request["id"])
    assert quote["ready"], quote
    started = execution.start(d["app_id"], actor=d["actor"], quote_id=quote["quote_id"],
                              input_digest=quote["input_digest"], approve_budget_micro=quote["ceiling_micro"],
                              idempotency_key="revision-1")
    with d["factory"]() as session:
        revised = session.get(BuildRun, started["build_run_id"])
        task = session.get(PlanTask, revised.plan_task_id)
        assert task.state == "done" and task.build_run_id == d["build_run_id"]
        spec = json.loads(revised.spec)
        assert spec["inputs"]["source"]["kind"] == "artifact"
        assert spec["inputs"]["revision"]["request_id"] == request["id"]
        assert "data-supymem-analytics" in spec["inputs"]["product_context"]["analytics_beacon_script"]

    # Existing sealed builder and QA fixtures remain the execution path. The
    # revision source is seeded; the completed plan task is not reused as a
    # mutable work queue row.
    seeded = []
    monkeypatch.setattr(d["m"].task_client, "seed_source", lambda *args, **kwargs: seeded.append((args, kwargs)))
    # The shared fixture has the first build's SDK snapshot in memory. A new
    # build gets its own sandbox event stream, so expose only its new event.
    prior_reader = d["m"].intent_bridge.read_events
    monkeypatch.setattr(d["m"].intent_bridge, "read_events", lambda *args: prior_reader(*args)[-1:])
    d["commands"].clear()
    d["qa_state"].clear()
    d["qa_state"]["phase"] = "running"
    revised_d = {**d, "build_run_id": started["build_run_id"]}
    step(revised_d, "build.run")
    assert d["commands"]
    d["commands"][0].update(status="completed", driver_status="finished")
    for _ in range(5):
        value = step(revised_d)
        if value["spec"].get("phase") == "qa_ready":
            break
    assert value["spec"].get("phase") == "qa_ready", {
        "state": value["state"], "phase": value["spec"].get("phase"),
        "error": value["spec"].get("artifact_capture_error"),
    }
    assert seeded and seeded[0][1]["source_artifact_id"]
    step(revised_d)
    revised_d["qa_state"]["phase"] = "completed"
    step(revised_d)
    completed = step(revised_d)
    assert completed["closed"] and completed["state"] == "succeeded"
    view = execution.read(started["build_run_id"], actor=d["actor"])
    assert view["qa_state"] == "passed" and view["review"]["status"] == "pending"
    assert view["revision_instruction"] == request["instruction"]
    assert_review_read_only(d, started['build_run_id'])
    execution.review(started["build_run_id"], actor=d["actor"],
                     source_artifact_id=view["artifact_id"],
                     report_digest=view["review"]["report_digest"], decision="accept")
    _hosting_environment(monkeypatch)
    _verified_waitlist(d)
    newer = queue_accepted_build(d["app_id"], started["build_run_id"], actor=d["actor"])
    with d["factory"]() as session:
        assert session.get(Deployment, newer["id"]).build_artifact_id == view["artifact_id"]

    class Provider:
        config = HostingConfig.from_environment()

        def __init__(self):
            self.switched = []

        def put_files(self, **_kwargs):
            pass

        def switch(self, hostname, mapping, *, expected_previous_deployment_id=None, expected_absent=False):
            assert expected_absent is True
            self.switched.append((hostname, mapping, expected_previous_deployment_id))
            return None

        def verify_public(self, *_args):
            pass

    provider = Provider()
    live_revision = publish_accepted_build(newer["id"], d["app_id"], started["build_run_id"],
                                            actor=d["actor"], provider=provider)
    assert live_revision["state"] == "live"
    assert len(provider.switched) == 1
    assert provider.switched[0][2] is None
    with d["factory"]() as session:
        original_task = session.get(PlanTask, revised.plan_task_id)
        assert original_task.state == "done" and original_task.build_run_id == d["build_run_id"]

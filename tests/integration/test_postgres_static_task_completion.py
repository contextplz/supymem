"""Real source QA/review/hosting transactions; owned cloud/readback fixtures."""
import json

import pytest

from tests.integration import test_postgres_subscription_static as source
from tests.integration.test_postgres_static_publication import _hosting_environment, _verified_waitlist

pg_database = source.pg_database
journey_db = source.journey_db
planning_standard = source.planning_standard
planning = source.planning
ready_plan = source.ready_plan
source_ready = source.source_ready
source_cloud = source.source_cloud


@pytest.fixture(autouse=True)
def hosting_before_reviewed_plan(monkeypatch):
    _hosting_environment(monkeypatch)


@pytest.fixture
def accepted_source(source_cloud):
    from app.builder.execution import read, review
    from app.db.supymem_models import PlanTask, PlanMilestone
    d = source_cloud
    source.source_step(d)
    d["qa_state"]["phase"] = "completed"
    source.source_step(d)
    assert source.source_step(d)["state"] == "succeeded"
    view = read(d["build_run_id"], actor=d["actor"])
    assert view["publication_required"] is True
    args = dict(actor=d["actor"], source_artifact_id=view["artifact_id"],
                report_digest=view["review"]["report_digest"], decision="accept")
    assert review(d["build_run_id"], **args) == review(d["build_run_id"], **args)
    with d["factory"]() as session:
        task = session.get(PlanTask, d["plan_task_id"])
        assert task.state == "waiting_for_founder" and task.completed_at is None
        assert session.get(PlanMilestone, task.milestone_id).state == "review"
    view = read(d["build_run_id"], actor=d["actor"])
    assert view["review"]["status"] == "accepted"
    assert view["task_completion"]["state"] == "pending_publication"
    return d


class Provider:
    def __init__(self):
        from app.hosting.service import HostingConfig
        self.config = HostingConfig.from_environment()
        self.effects = []

    def put_files(self, **_kwargs):
        self.effects.append("upload")

    def switch(self, *_args, **_kwargs):
        self.effects.append("switch")
        return None

    def verify_public(self, *_args):
        self.effects.append("verified_public_readback")

    def restore(self, *_args):
        self.effects.append("restore")


def test_source_acceptance_enables_hosting_but_only_verified_publication_completes_task(accepted_source, monkeypatch):
    from app.builder.execution import read
    from app.db.supymem_models import PlanTask, PlanMilestone
    from app.hosting.service import queue_accepted_build, publish_accepted_build
    d = accepted_source
    _verified_waitlist(d)
    queued = queue_accepted_build(d["app_id"], d["build_run_id"], actor=d["actor"])
    with d["factory"]() as session:
        assert session.get(PlanTask, d["plan_task_id"]).state == "waiting_for_founder"
    provider = Provider()
    args = dict(actor=d["actor"], provider=provider)
    published = publish_accepted_build(queued["id"], d["app_id"], d["build_run_id"], **args)
    assert published["state"] == "live"
    assert publish_accepted_build(queued["id"], d["app_id"], d["build_run_id"], **args) == published
    assert provider.effects == ["upload", "switch", "verified_public_readback"]
    with d["factory"]() as session:
        task = session.get(PlanTask, d["plan_task_id"])
        assert task.state == "done" and task.completed_at
        assert session.get(PlanMilestone, task.milestone_id).state == "done"
    completion = read(d["build_run_id"], actor=d["actor"])["task_completion"]
    assert completion["state"] == "completed" and completion["deployment_id"] == published["id"]
    assert completion["source_artifact_id"] == queued["build_artifact_id"]
    assert completion["conversion_receipt"]["build_id"] == d["build_run_id"]
    monkeypatch.setenv("PUBLIC_GATEWAY_URL", "https://changed-gateway.example.test")
    assert read(d["build_run_id"], actor=d["actor"])["publication_required"] is True


@pytest.mark.parametrize("fault", [None, "schema_version", "workspace_id", "plan_task_id", "source_artifact_id", "inputs_digest", "report_digest"])
def test_read_keeps_exact_saved_publication_obligation_when_live_configuration_changes(accepted_source, monkeypatch, fault):
    from app.build.subscription_static_completion import publication_required
    from app.builder.execution import read
    from app.db.supymem_models import BuildRun
    from app.hosting.service import HostingError, queue_accepted_build
    d = accepted_source
    monkeypatch.setenv("PUBLIC_GATEWAY_URL", "https://changed-gateway.example.test")
    with d["factory"]() as session:
        run = session.get(BuildRun, d["build_run_id"])
        spec = json.loads(run.spec)
        assert not publication_required(session, run, spec)
        if fault:
            spec["lifecycle"]["task_completion"][fault] = "foreign-or-tampered"
            run.spec = json.dumps(spec)
            session.commit()
    view = read(d["build_run_id"], actor=d["actor"])
    assert view["publication_required"] is (fault is None)
    assert view["task_completion"]["state"] == "pending_publication"
    # Retaining the display obligation grants no authority under changed inputs.
    with pytest.raises(HostingError):
        queue_accepted_build(d["app_id"], d["build_run_id"], actor=d["actor"])


@pytest.mark.parametrize("fault", ["no_submission", "closed_after_queue", "public_readback_failed", "foreign_completion", "stale_plan"])
def test_unverified_or_foreign_publication_never_completes_source_accepted_task(accepted_source, fault):
    from app.builder.execution import read
    from app.conversion import waitlist
    from app.db.supymem_models import BuildRun, ExecutionPlan, PlanTask, PlanMilestone
    from app.hosting.service import HostingError, queue_accepted_build, publish_accepted_build
    d = accepted_source
    if fault == "no_submission":
        with pytest.raises(HostingError, match="hosting_conversion_verification_required"):
            queue_accepted_build(d["app_id"], d["build_run_id"], actor=d["actor"])
    else:
        _verified_waitlist(d)
        queued = queue_accepted_build(d["app_id"], d["build_run_id"], actor=d["actor"])
        provider = Provider()
        if fault == "closed_after_queue":
            waitlist.configure(d["vid"], d["wid"], enabled=False, actor=d["actor"])
        elif fault == "public_readback_failed":
            def fail(*_args):
                raise HostingError("hosting_public_readback_failed")
            provider.verify_public = fail
        elif fault == "stale_plan":
            with d["factory"]() as session:
                plan = session.get(ExecutionPlan, d["plan_id"])
                doc = json.loads(plan.structured)
                doc["proposal"]["milestones"][0]["tasks"][0]["landing_expectations"]["contract"]["product_name"] = "Unapproved change"
                plan.structured = json.dumps(doc)
                session.commit()
        else:
            with d["factory"]() as session:
                run = session.get(BuildRun, d["build_run_id"])
                spec = json.loads(run.spec)
                spec["lifecycle"]["task_completion"]["venture_id"] = "ven_foreign"
                run.spec = json.dumps(spec)
                session.commit()
        with pytest.raises(HostingError):
            publish_accepted_build(queued["id"], d["app_id"], d["build_run_id"], actor=d["actor"], provider=provider)
        if fault in {"closed_after_queue", "stale_plan"}:
            assert provider.effects == []
    with d["factory"]() as session:
        task = session.get(PlanTask, d["plan_task_id"])
        assert task.state == "waiting_for_founder" and task.completed_at is None
        assert session.get(PlanMilestone, task.milestone_id).state == "review"
    assert read(d["build_run_id"], actor=d["actor"])["review"]["status"] == "accepted"


def test_private_and_revision_sources_do_not_inherit_publication_from_prose(accepted_source):
    from app.build.subscription_static_completion import publication_required
    from app.db.supymem_models import BuildRun
    d = accepted_source
    with d["factory"]() as session:
        run = session.get(BuildRun, d["build_run_id"])
        spec = json.loads(run.spec)
        assert publication_required(session, run, spec)
        spec["inputs"]["task"]["landing_expectations"]["contract"]["actions"] = []
        spec["inputs"]["task"]["description"] = "Publish is a word in this private task; it grants no publication obligation."
        assert not publication_required(session, run, spec)
        spec = json.loads(run.spec)
        spec["inputs"]["revision"] = {"kind": "private_build_correction", "request_id": "private_fixture"}
        assert not publication_required(session, run, spec)

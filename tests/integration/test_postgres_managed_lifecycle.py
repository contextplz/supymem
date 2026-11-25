"""Migrated DB orchestration. AWS/model-command/browser bytes are named fixtures.

Actual sealed browser acceptance is separately retained in test_static_qa_*.
"""
import copy
import hashlib
import json
from uuid import uuid4

import pytest

from tests.fargate_fixtures import FakeAWS
from tests.lifecycle_fixtures import runtime, qa_definition
from tests.qa_storage_fixtures import CONTRACT, MemoryS3, report_fixture
from tests.integration.test_postgres_auth_security import pg_database as pg_database
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db
from tests.integration.test_postgres_planning import planning as planning
from tests.integration.test_postgres_build_intents import ready_plan as ready_plan, quote, start
from tests.integration.test_postgres_fargate_lifecycle import claim, saved


@pytest.fixture()
def lifecycle_run(ready_plan, monkeypatch, tmp_path):
    from app.builder import budgets, orchestration as m, qa_lifecycle as q
    from app.builder.artifact_store import S3ArtifactStore, ArtifactStoreConfig
    from app.infra.fargate_build import FargateBuildProvider, FargateProfile
    from app.infra.fargate_qa import FargateQaProvider, QaProfile
    from app.db.supymem_models import ExecutionPlan, BuildArtifact, BuildRun
    from app.credits._txn import naive_utc
    from core.artifact_snapshot import freeze_source, manifest_digest
    d = ready_plan
    from app.conversion.waitlist import public_url
    landing_contract = copy.deepcopy(CONTRACT)
    landing_contract["actions"] = [{"id": "early_access", "kind": "external_link", "role": "link",
        "name": "Request early access", "page": "index.html", "target": public_url(d["vid"]), "depends_on": []}]
    monkeypatch.setenv("COMPANY_OS_VAULT_SECRET", "local-lifecycle-test-vault-at-least-thirty-two")
    with d["factory"]() as session:
        plan = session.get(ExecutionPlan, d["plan_id"])
        doc = json.loads(plan.structured)
        task = doc["proposal"]["milestones"][0]["tasks"][0]
        task["landing_expectations"] = {"contract": landing_contract, "criteria": [
            {"criterion": i, "summary": "Check the reading-list offer and early-access destination", "text_indices": [0], "action_ids": ["early_access"]} for i in range(len(task["acceptance"]))]}
        plan.structured = json.dumps(doc)
        session.commit()
    policy = runtime()
    monkeypatch.setattr(budgets, "runtime_quote", lambda wid, **_kwargs: policy)
    quoted = quote(d)
    assert quoted["ready"], quoted
    accepted = start(d, quoted)
    builder_profile = FargateProfile.from_dict(policy["builder_profile"])
    qa_profile = QaProfile.from_dict(policy["qa_profile"])
    builder, qa = FakeAWS(builder_profile), FakeAWS(qa_profile)
    qa.definition = qa_definition(qa_profile)
    original_launch = qa.run_task
    def qa_launch(**body):
        result = original_launch(**body)
        for item in qa.tasks.values():
            item["containers"][0]["name"] = "static-qa"
        return result
    qa.run_task = qa_launch
    monkeypatch.setattr(m, "configured_provider", lambda expected: FargateBuildProvider(builder_profile, ecs=builder, ec2=builder))
    monkeypatch.setattr(q, "configured_provider", lambda expected: FargateQaProvider(qa_profile, ecs=qa, ec2=qa))
    commands, raw_events = [], []
    from types import SimpleNamespace
    from app.builder import model_proxy
    monkeypatch.setattr(model_proxy, "_provider_call", lambda wid, body: SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20),
        choices=[SimpleNamespace(message=SimpleNamespace(content="Synthetic completed command response", tool_calls=None), finish_reason="stop")]))
    monkeypatch.setattr(m.task_client, "state", lambda rid, gen: {"build_id": rid, "generation": gen, "commands": commands})
    def submit(rid, gen, body):
        row = {"command_id": body["command_id"], "kind": "start", "status": "running"}
        commands.append(row)
        with d["factory"]() as session:
            spec = json.loads(session.get(BuildRun, rid).spec)
        fence = {"generation": gen, "controller_id": spec["controller_fence"]["id"], "attempt": spec["controller_fence"]["attempt"]}
        request = {"messages": [{"role": "user", "content": "Complete the isolated lifecycle fixture"}], "max_tokens": 128}
        cid = model_proxy.register(rid, invocation_key="fixture-invocation", input_event_id=None, request=request, **fence)["invocation_id"]
        model_proxy.complete(rid, cid, request, generation=gen)
        raw_events.append(json.dumps({"kind": "MessageEvent", "id": "fixture-sdk-event", "parent_id": None,
            "source": "agent", "llm_message": {"role": "assistant"}, "llm_response_id": cid}))
        return row
    monkeypatch.setattr(m.task_client, "submit", submit)
    monkeypatch.setattr(m.intent_bridge, "read_events", lambda *args: raw_events)
    config = ArtifactStoreConfig("test-artifacts", "managed-builds", "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111", "us-east-1", "123456789012")
    s3 = MemoryS3()
    store = S3ArtifactStore(config, s3)
    monkeypatch.setattr(S3ArtifactStore, "configured", lambda: store)
    source = tmp_path / "source"
    source.mkdir()
    (source / "index.html").write_text(f'<!doctype html><title>Acorn</title><h1>Acorn</h1><p>Reading lists</p><a href="{public_url(d["vid"])}">Request early access</a>')
    manifest, blob = freeze_source(source)
    def capture(rid, command_id, generation, controller_id, attempt):
        scope = {"workspace_id": d["wid"], "venture_id": d["vid"], "app_id": d["app_id"], "build_id": rid, "generation": generation}
        meta = {"manifest_digest": manifest_digest(manifest), "archive_sha256": hashlib.sha256(blob).hexdigest(), "size_bytes": len(blob)}
        receipt = store.persist(blob, meta, scope)
        with d["factory"]() as session:
            run = session.get(BuildRun, rid)
            spec = json.loads(run.spec)
            artifact_id = run.artifact_id or "bar_" + uuid4().hex
            if not run.artifact_id:
                session.add(BuildArtifact(id=artifact_id, build_run_id=rid, workspace_id=d["wid"], app_id=d["app_id"], kind="source_zip",
                    s3_key=receipt["key"], sha256=receipt["sha256"], size_bytes=len(blob), created_at=naive_utc()))
            spec.update(artifact_verified=True, artifact_capture={**receipt, "artifact_id": artifact_id, "manifest": manifest, "state": "stored"})
            run.artifact_id, run.spec = artifact_id, json.dumps(spec)
            session.commit()
    monkeypatch.setattr("app.builder.artifacts.import_completed_artifact", capture)
    state = {"phase": "running"}
    def progress(binding, request, reader, *, permit_execution):
        permit_execution()
        assert reader() == blob
        state.update(input=request, id=binding["qa_id"])
        return {"phase": state["phase"]}
    monkeypatch.setattr(q.qa_transport, "progress", progress)
    def result(binding, current):
        if "frozen_report" not in state:
            state["frozen_report"] = report_fixture(state, binding["build_id"])
        return state["frozen_report"]
    monkeypatch.setattr(q.qa_transport, "result", result)
    return {**d, **accepted, "m": m, "q": q, "builder": builder, "qa": qa, "commands": commands,
            "qa_state": state, "policy": policy, "s3": s3, "store": store}


def step(d, kind="build.watch"):
    _, payload = claim(d, kind)
    (d["m"].run if kind == "build.run" else d["m"].watch)(payload)
    return saved(d)


def retire(d):
    started = step(d, "build.run")
    assert d["commands"], started
    d["commands"][0].update(status="completed", driver_status="finished")
    value = step(d)
    assert value["state"] == "syncing_artifacts" and value["spec"]["builder_admission_closed_at"]
    assert not d["qa"].launches
    value = step(d)
    assert value["spec"]["lifecycle"]["builder"]["cleanup_state"] == "complete"
    assert value["spec"]["phase"] == "qa_ready" and not value["closed"]
    return value


def test_product_javascript_preflight_schedules_one_bounded_feedback_command(lifecycle_run, monkeypatch):
    from app.builder.model_proxy import BuildModelError
    from app.db.supymem_models import BuildRun
    from app.builder import artifacts

    d = lifecycle_run
    with d["factory"]() as session:
        run = session.get(BuildRun, d["build_run_id"])
        spec = json.loads(run.spec)
        spec["artifact_profile"] = "founder_product_v1"
        run.spec = json.dumps(spec)
        session.commit()
    step(d, "build.run")
    d["commands"][0].update(status="completed", driver_status="finished")
    monkeypatch.setattr(artifacts, "import_completed_artifact",
                        lambda *args, **kwargs: (_ for _ in ()).throw(BuildModelError("artifact_product_javascript_invalid")))

    value = step(d)
    spec = value["spec"]
    assert spec["artifact_repair_attempts"] == 1
    assert spec["managed_command"]["kind"] == "feedback"
    assert spec["managed_command"]["body"]["max_steps"] == 8
    assert spec["artifact_capture_error"] == "artifact_product_javascript_invalid"
    assert not spec.get("builder_admission_closed_at")
    content = json.loads(spec["managed_command"]["body"]["text"])
    assert content["product_contract"]["profile"] == "founder_product_v1"
    assert "node --check" in content["objective"]


@pytest.mark.parametrize(("code", "expected"), [
    ("artifact_product_visual_contract_invalid", "two substantial"),
    ("artifact_product_route_contract_invalid", "/api/product"),
    ("artifact_product_record_contract_invalid", "dictionaries"),
])
@pytest.mark.parametrize(("max_steps", "repair_steps"), [(40, 8), (56, 24)])
def test_product_contract_preflight_uses_the_same_single_bounded_repair_window(
    lifecycle_run, monkeypatch, code, expected, max_steps, repair_steps,
):
    from app.builder.model_proxy import BuildModelError
    from app.db.supymem_models import BuildRun
    from app.builder import artifacts

    d = lifecycle_run
    with d["factory"]() as session:
        run = session.get(BuildRun, d["build_run_id"])
        spec = json.loads(run.spec)
        spec["artifact_profile"] = "founder_product_v1"
        spec["runtime"]["runtime_policy"]["max_steps"] = max_steps
        run.spec = json.dumps(spec)
        session.commit()
    step(d, "build.run")
    d["commands"][0].update(status="completed", driver_status="finished")
    monkeypatch.setattr(artifacts, "import_completed_artifact",
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            BuildModelError(code)))

    value = step(d)
    spec = value["spec"]
    assert spec["artifact_repair_attempts"] == 1
    assert spec["managed_command"]["kind"] == "feedback"
    assert spec["managed_command"]["body"]["max_steps"] == repair_steps
    assert spec["artifact_capture_error"] == code
    assert not spec.get("builder_admission_closed_at")
    content = json.loads(spec["managed_command"]["body"]["text"])
    assert expected in content["objective"]


def test_product_visual_diagnostics_reach_the_single_repair_command(lifecycle_run, monkeypatch):
    from app.builder.model_proxy import BuildModelError
    from app.db.supymem_models import BuildRun
    from app.builder import artifacts

    d = lifecycle_run
    with d["factory"]() as session:
        run = session.get(BuildRun, d["build_run_id"])
        spec = json.loads(run.spec)
        spec["artifact_profile"] = "founder_product_v1"
        spec["runtime"]["runtime_policy"]["max_steps"] = 56
        run.spec = json.dumps(spec)
        session.commit()
    step(d, "build.run")
    d["commands"][0].update(status="completed", driver_status="finished")
    report = {"page_readable": True, "candidate_count": 1, "qualified_distinct_count": 0,
              "candidates": [{"element_index": 1, "tag": "img", "semantic": True,
                              "name_ok": True, "distinct_name": True, "width_ok": True,
                              "height_ok": True, "responsive": False}]}
    monkeypatch.setattr(artifacts, "import_completed_artifact",
                        lambda *args, **kwargs: (_ for _ in ()).throw(BuildModelError(
                            "artifact_product_visual_contract_invalid", diagnostics=report)))

    spec = step(d)["spec"]
    command = spec["managed_command"]["body"]
    content = json.loads(command["text"])
    assert command["max_steps"] == 24
    assert content["visual_diagnostics"] == report
    assert spec["artifact_capture_diagnostics"] == report
    assert spec["artifact_repair_attempts"] == 1
    assert not spec.get("builder_admission_closed_at")


def test_product_step_limit_uses_the_single_bounded_completion_window(lifecycle_run):
    from app.db.supymem_models import BuildRun

    d = lifecycle_run
    with d["factory"]() as session:
        run = session.get(BuildRun, d["build_run_id"])
        spec = json.loads(run.spec)
        spec["artifact_profile"] = "founder_product_v1"
        run.spec = json.dumps(spec)
        session.commit()

    initial = step(d, "build.run")
    assert initial["spec"]["managed_command"]["body"]["max_steps"] == 32
    d["commands"][0].update(status="failed", driver_status="running", error_code="managed_step_limit")

    value = step(d)
    spec = value["spec"]
    assert not value["closed"] and spec["phase"] == "running"
    assert spec["product_completion_attempts"] == 1
    assert spec["managed_command"]["kind"] == "feedback"
    assert spec["managed_command"]["body"]["max_steps"] == 8
    assert "remaining work" in spec["managed_command"]["body"]["text"]

    d["commands"].append({"command_id": spec["managed_command"]["id"], "kind": "feedback",
                          "status": "failed", "driver_status": "running", "error_code": "managed_step_limit"})
    result = step(d)
    for _ in range(4):
        if result["closed"]:
            break
        result = step(d)
    assert result["closed"] and result["state"] == "failed"
    with d["factory"]() as session:
        assert session.get(BuildRun, d["build_run_id"]).error_code == "build_step_limit"


def test_builder_retires_before_separate_qa_and_all_usage_closes_before_review(lifecycle_run):
    from app.builder.execution import read, review
    from app.db.models import Venture
    from app.db.supymem_models import PlanTask, PlanMilestone
    d = lifecycle_run
    retire(d)
    assert len(d["builder"].stops) == 1 and not d["qa"].launches
    step(d)
    assert len(d["qa"].launches) == 1 and len(d["builder"].launches) == 1
    d["qa_state"]["phase"] = "completed"
    step(d)
    result = step(d)
    assert result["closed"] and result["reserved"] == 0 and result["state"] == "succeeded", result
    assert result["spent"] >= 180 and result["spent"] < 10000
    value = read(d["build_run_id"], actor=d["actor"])
    assert value["verified_execution"] and value["artifact_id"] and value["review"]["status"] == "pending"
    assert value["included_overhead"]["founder_price_micro"] == 0 and value["included_overhead"]["aws_actual_cost"] is None
    body = {"actor": d["actor"], "source_artifact_id": value["artifact_id"], "report_digest": value["review"]["report_digest"], "decision": "accept"}
    assert review(d["build_run_id"], **body) == review(d["build_run_id"], **body)
    with d["factory"]() as session:
        task = session.get(PlanTask, d["plan_task_id"])
        assert task.state == "done"
        milestone = session.get(PlanMilestone, task.milestone_id)
        venture = session.get(Venture, d["vid"])
    assert milestone.state == "done"
    assert venture.stage == "readiness_review"


def test_logging_preflight_failure_releases_without_ambiguous_hold(lifecycle_run, monkeypatch):
    from app.infra.fargate_build import FargateBuildProvider, FargateProfile
    d = lifecycle_run
    provider = FargateBuildProvider(FargateProfile.from_dict(d["policy"]["builder_profile"]),
                                    ecs=d["builder"], ec2=d["builder"])

    def denied():
        raise PermissionError("fixture describe denied")

    provider.verify_no_task_logs = denied
    monkeypatch.setattr(d["m"], "configured_provider", lambda _expected: provider)
    _, payload = claim(d, "build.run")
    d["m"].run(payload)
    snapshot = saved(d)
    assert snapshot["closed"] and snapshot["reserved"] == snapshot["spent"] == 0
    assert not d["builder"].launches and not d["builder"].tasks


@pytest.mark.parametrize("command_error,run_error", [
    ("agent_initialization_failed", "build_command_requires_review"),
    ("managed_model_http_504", "build_model_transport_timeout"),
    ("managed_request_limit", "build_model_input_limit"),
    ("managed_step_limit", "build_step_limit"),
])
def test_failed_managed_build_returns_plan_task_to_a_reviewable_retry(lifecycle_run, command_error, run_error):
    """A real sandbox failure must not permanently dead-end the desktop flow."""
    from app.db.supymem_models import PlanMilestone, PlanTask
    d = lifecycle_run
    initial = step(d, "build.run")
    assert d["commands"], initial
    d["commands"][0].update(status="failed", driver_status="failed", error_code=command_error)

    result = step(d)
    for _ in range(4):
        if result["closed"]:
            break
        result = step(d)
    assert result["closed"] and result["state"] == "failed"
    from app.db.supymem_models import BuildRun
    with d["factory"]() as session:
        assert session.get(BuildRun, d["build_run_id"]).error_code == run_error
    with d["factory"]() as session:
        task = session.get(PlanTask, d["plan_task_id"])
        milestone = session.get(PlanMilestone, task.milestone_id)
        assert task.state == milestone.state == "failed"
        # Simulate a release that closed the run before advancing its plan
        # rows; the reviewed estimate must repair this historical dead-end.
        task.state = milestone.state = "running"
        session.commit()

    reviewed_retry = quote(d)
    assert reviewed_retry["ready"] is True
    with d["factory"]() as session:
        task = session.get(PlanTask, d["plan_task_id"])
        milestone = session.get(PlanMilestone, task.milestone_id)
        assert task.state == milestone.state == "failed"
    accepted = start(d, reviewed_retry, key="reviewed-retry")
    assert accepted["build_run_id"] != d["build_run_id"]
    with d["factory"]() as session:
        task = session.get(PlanTask, d["plan_task_id"])
        milestone = session.get(PlanMilestone, task.milestone_id)
        assert task.build_run_id == accepted["build_run_id"]
        assert task.state == milestone.state == "ready"
    retry_run = {**d, **accepted}
    retry_started = step(retry_run, "build.run")
    assert retry_started["state"] == "running" and len(d["commands"]) == 2


def test_cancel_after_builder_stop_never_launches_qa(lifecycle_run):
    from app.builder.execution import cancel
    from app.db.supymem_models import PlanTask
    d = lifecycle_run
    retire(d)
    cancel(d["build_run_id"], actor=d["actor"])
    result = step(d)
    assert result["closed"] and result["reserved"] == 0 and result["state"] == "cancelled"
    assert not d["qa"].launches
    with d["factory"]() as session:
        task = session.get(PlanTask, d["plan_task_id"])
        assert task.state == "failed"
        # A cancelled pre-launch/reconciliation path may restore the exact
        # bound task to ready. Its closed cancelled run remains the authority
        # that makes another reviewed estimate safe.
        task.state = "ready"
        session.commit()
    retry_quote = quote(d)
    assert retry_quote["ready"] is True


def test_lost_qa_launch_then_rejection_preserves_possible_task_and_holds(lifecycle_run):
    d = lifecycle_run
    retire(d)
    d["qa"].lose_launch_response = True
    step(d)
    assert len(d["qa"].tasks) == 1 and not saved(d)["closed"]
    d["qa"].rejected = True
    step(d)
    assert not saved(d)["closed"] and saved(d)["reserved"] > 0
    step(d)  # discovery-only proves existing task, without another RunTask
    assert len(d["qa"].launches) == 2 and len(d["qa"].tasks) == 1


def test_expiry_or_suspension_during_qa_permits_only_stop_and_settlement(lifecycle_run):
    from app.db.models import CreditAccount
    from app.db.supymem_models import BuildRun
    d = lifecycle_run
    retire(d)
    step(d)
    with d["factory"]() as session:
        run = session.get(BuildRun, d["build_run_id"])
        from app.db.models import CreditReservation
        pool = session.get(CreditReservation, run.reservation_id)
        session.get(CreditAccount, pool.account_id).status = "suspended"
        session.commit()
    result = step(d)
    assert result["closed"] and result["state"] == "failed" and result["reserved"] == 0
    assert len(d["qa"].launches) == 1 and len(d["qa"].stops) == 1
    assert result["spec"]["execution_verified"] is False


def test_unknown_terminal_compute_retains_hold_and_recovers_watch(lifecycle_run):
    from app.builder import orchestration
    d = lifecycle_run
    retire(d)
    step(d)
    d["qa_state"]["phase"] = "completed"
    step(d)
    task = next(iter(d["qa"].tasks.values()))
    task["pullStartedAt"] = None
    result = step(d)
    assert result["closed"] is None and result["reserved"] > 0
    assert result["spec"]["lifecycle"]["qa"]["cleanup_state"] == "complete"
    assert orchestration.enqueue_orphaned_watches() == 0  # one existing durable watch remains
    from datetime import timedelta
    task["pullStartedAt"] = task["stoppedAt"] - timedelta(seconds=91)
    result = step(d)  # later authoritative timing recovers the held charge, no new task
    assert result["closed"] and result["reserved"] == 0 and len(d["qa"].launches) == 1


def test_failed_browser_checks_store_real_report_but_cannot_mark_execution_verified(lifecycle_run, monkeypatch):
    from app.builder.execution import read, review, BuildError
    from core.artifact_snapshot import canonical
    d = lifecycle_run
    retire(d)
    step(d)
    original = d["q"].qa_transport.result
    def failure(binding, current):
        blob, images = original(binding, current)
        report = json.loads(blob)
        report["cases"][3]["status"] = "failed"
        report["cases"][3]["observed"] = "Required offer text missing (synthetic negative fixture)"
        report["outcome"] = "failed"
        return canonical(report), images
    monkeypatch.setattr(d["q"].qa_transport, "result", failure)
    d["qa_state"]["phase"] = "completed"
    step(d)
    result = step(d)
    assert result["closed"] and result["state"] == "failed" and result["reserved"] == 0
    value = read(d["build_run_id"], actor=d["actor"])
    assert value["artifact_id"] and value["qa_state"] == "failed" and not value["verified_execution"]
    with pytest.raises(BuildError, match="review_not_ready"):
        review(d["build_run_id"], actor=d["actor"], source_artifact_id=value["artifact_id"], report_digest="a"*64, decision="accept")


def test_untrusted_builder_logs_rejected_before_any_cloud_call(lifecycle_run):
    d = lifecycle_run
    d["builder"].definition["containerDefinitions"][0]["logConfiguration"] = {"logDriver": "awslogs"}
    result = step(d, "build.run")
    assert result["closed"] and result["reserved"] == 0 and not d["builder"].launches


def test_qa_source_upload_ack_loss_recovers_exact_report_and_debit(lifecycle_run):
    d = lifecycle_run
    retire(d)
    step(d)
    d["qa_state"]["phase"] = "completed"
    d["s3"].lose_head_after_put = True
    result = step(d)
    assert result["closed"] is None and not d["qa"].stops
    step(d)  # Same frozen report resumes the immutable S3 receipt.
    result = step(d)
    assert result["closed"] and result["state"] == "succeeded" and result["reserved"] == 0
    assert len(d["qa"].launches) == len(d["qa"].stops) == 1


def test_assembled_review_requires_signed_owner_and_exact_source_report(lifecycle_run, monkeypatch):
    from fastapi.testclient import TestClient
    from app.builder.execution import read
    from app.db.models import WorkspaceMember
    from app.authz.workspace import ensure_subject_person
    from tests.security.test_core_signed_boundary import SECRET, headers
    from core.clock import utcnow
    d = lifecycle_run
    retire(d)
    step(d)
    d["qa_state"]["phase"] = "completed"
    step(d)
    step(d)
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", SECRET)
    monkeypatch.setenv("ADMIN_REQUIRE_TOTP", "1")
    monkeypatch.setenv("ADMIN_BOOTSTRAP_EMAILS", "operator@example.test")
    from app.main import create_app
    client = TestClient(create_app())
    value = read(d["build_run_id"], actor=d["actor"])
    path = f"/builds/{d['build_run_id']}/review"
    body = {"source_artifact_id": value["artifact_id"], "report_digest": value["review"]["report_digest"], "decision": "accept"}
    assert client.post(path, json=body, headers={"X-Subject": d["actor"]}).status_code == 401
    assert client.post(path, json=body, headers=headers(path, "POST", "foreign-founder")).status_code == 404
    viewer = "viewer-" + uuid4().hex
    ensure_subject_person(viewer)
    with d["factory"]() as session:
        session.add(WorkspaceMember(id="member-" + uuid4().hex, workspace_id=d["wid"], person_id=viewer, role="viewer", created_at=utcnow()))
        session.commit()
    assert client.post(path, json=body, headers=headers(path, "POST", viewer)).status_code == 403
    assert client.post(path, json={**body, "report_digest": "0"*64}, headers=headers(path, "POST", d["actor"])).status_code == 409
    response = client.post(path, json=body, headers=headers(path, "POST", d["actor"]))
    assert response.status_code == 200 and response.json()["review"]["status"] == "accepted", response.text
    assert client.post(path, json=body, headers=headers(path, "POST", d["actor"])).json() == response.json()
    assert client.post(path, json={**body, "decision": "reject"}, headers=headers(path, "POST", d["actor"])).status_code == 409
    client.close()


def test_rejected_founder_feedback_is_bound_to_the_next_reviewed_build(lifecycle_run):
    from app.builder.execution import review
    from app.db.supymem_models import BuildRun, FounderRequest

    d = lifecycle_run
    retire(d)
    step(d)
    d["qa_state"]["phase"] = "completed"
    step(d)
    step(d)
    with d["factory"]() as session:
        prior = session.get(BuildRun, d["build_run_id"])
        receipt = json.loads(prior.spec)["lifecycle"]["review"]
    feedback = "Preserve saved documents when only the application status changes."
    result = review(d["build_run_id"], actor=d["actor"], source_artifact_id=prior.artifact_id,
                    report_digest=receipt["report_digest"], decision="reject", feedback=feedback)
    assert result["review"]["status"] == "rejected"

    next_quote = quote(d)
    assert next_quote["ready"]
    with d["factory"]() as session:
        saved = json.loads(session.get(FounderRequest, next_quote["quote_id"]).text)
    assert saved["inputs"]["product_context"]["prior_founder_review"] == {
        "build_run_id": d["build_run_id"], "feedback": feedback,
        "source_artifact_id": prior.artifact_id,
    }
    next_run = start(d, next_quote, "after-founder-changes")
    with d["factory"]() as session:
        spec = json.loads(session.get(BuildRun, next_run["build_run_id"]).spec)
    assert spec["inputs"]["product_context"]["prior_founder_review"]["feedback"] == feedback


def test_pool_cannot_close_by_forging_only_global_cleanup_boolean(lifecycle_run):
    from app.builder.model_proxy import _locked
    from app.credits import pools
    from app.credits.errors import CreditError
    d = lifecycle_run
    step(d, "build.run")
    with d["factory"]() as session:
        ctx = _locked(session, d["build_run_id"])
        spec = json.loads(ctx.run.spec)
        spec["cleanup_state"] = "complete"
        ctx.run.spec = json.dumps(spec)
        with pytest.raises(CreditError, match="cleanup_unresolved"):
            pools.close_pool(session, ctx)
    assert saved(d)["closed"] is None


def test_missing_planner_static_expectations_never_becomes_executable_quote(ready_plan, monkeypatch):
    from app.builder import budgets
    from app.db.supymem_models import BuildRun, ExecutionPlan
    from sqlalchemy import select
    with ready_plan["factory"]() as session:
        plan = session.get(ExecutionPlan, ready_plan["plan_id"])
        doc = json.loads(plan.structured)
        doc["proposal"]["milestones"][0]["tasks"][0].pop("landing_expectations", None)
        plan.structured = json.dumps(doc)
        session.commit()
    monkeypatch.setattr(budgets, "runtime_quote", lambda wid, **_kwargs: runtime())
    response = quote(ready_plan)
    assert response["ready"] is False and "build_static_expectations_required" in response["reasons"]
    assert response["expectations"] is None
    with ready_plan["factory"]() as session:
        assert not session.scalar(select(BuildRun.id).where(BuildRun.app_id == ready_plan["app_id"]))


def test_first_product_build_uses_empty_source_and_product_profile(ready_plan, monkeypatch):
    from app.builder import budgets
    from app.db.supymem_models import BuildRun, ExecutionPlan, PlanMilestone

    with ready_plan["factory"]() as session:
        plan = session.get(ExecutionPlan, ready_plan["plan_id"])
        doc = json.loads(plan.structured)
        milestone = doc["proposal"]["milestones"][0]
        milestone["kind"] = "core"
        milestone["tasks"][0]["landing_expectations"] = None
        plan.structured = json.dumps(doc)
        session.get(PlanMilestone, doc["entity_ids"]["milestones"][milestone["key"]]).kind = "core"
        session.commit()

    policy = runtime()
    monkeypatch.setattr(budgets, "runtime_quote", lambda _wid, **_kwargs: policy)
    reviewed = quote(ready_plan)
    assert reviewed["ready"] is True
    assert reviewed["artifact_profile"] == "founder_product_v1"
    assert reviewed["expectations"] is None
    accepted = start(ready_plan, reviewed, key="first-product")
    with ready_plan["factory"]() as session:
        spec = json.loads(session.get(BuildRun, accepted["build_run_id"]).spec)
    assert spec["artifact_profile"] == "founder_product_v1"
    assert spec["inputs"]["source"]["kind"] == "empty"


def test_rejected_product_repair_quote_binds_verified_prior_source(ready_plan, monkeypatch):
    from app.builder import budgets
    from app.credits._txn import naive_utc
    from app.db.supymem_models import BuildArtifact, BuildRun, ExecutionPlan, PlanMilestone, PlanTask
    from core.artifact_snapshot import manifest_digest

    with ready_plan["factory"]() as session:
        plan = session.get(ExecutionPlan, ready_plan["plan_id"])
        doc = json.loads(plan.structured)
        milestone = doc["proposal"]["milestones"][0]
        milestone["kind"] = "core"
        milestone["tasks"][0]["landing_expectations"] = None
        plan.structured = json.dumps(doc)
        session.get(PlanMilestone, doc["entity_ids"]["milestones"][milestone["key"]]).kind = "core"
        session.commit()

    policy = runtime()
    monkeypatch.setattr(budgets, "runtime_quote", lambda _wid, **_kwargs: policy)
    first = start(ready_plan, quote(ready_plan), key="first-repair-source")
    with ready_plan["factory"]() as session:
        run = session.get(BuildRun, first["build_run_id"])
        task = session.get(PlanTask, run.plan_task_id)
        milestone = session.get(PlanMilestone, task.milestone_id)
        artifact_id = "bar_repair_fixture"
        session.add(BuildArtifact(id=artifact_id, build_run_id=run.id, app_id=run.app_id,
                                  workspace_id=run.workspace_id, kind="source_zip",
                                  s3_key="verified/source.zip", sha256="a" * 64,
                                  size_bytes=123, created_at=naive_utc()))
        spec = json.loads(run.spec)
        spec["artifact_verified"] = True
        manifest = [{"path": "product/routes.py", "size_bytes": 1, "sha256": "b" * 64}]
        spec["artifact_capture"] = {"state": "stored", "artifact_id": artifact_id,
                                    "sha256": "a" * 64, "size_bytes": 123,
                                    "key": "verified/source.zip", "manifest": manifest,
                                    "manifest_digest": manifest_digest(manifest)}
        spec["lifecycle"]["review"] = {"status": "rejected", "decision": "reject",
                                         "feedback": "Preserve and repair the saved code."}
        run.spec = json.dumps(spec)
        run.artifact_id = artifact_id
        run.state = "succeeded"
        run.closed_at = naive_utc()
        task.state = milestone.state = "failed"
        session.commit()

    next_quote = quote(ready_plan)
    assert next_quote["ready"]
    from app.db.supymem_models import FounderRequest
    with ready_plan["factory"]() as session:
        saved = json.loads(session.get(FounderRequest, next_quote["quote_id"]).text)
    assert saved["inputs"]["source"] == {
        "kind": "artifact", "id": artifact_id, "sha256": "a" * 64, "size_bytes": 123,
    }
    assert saved["inputs"]["product_context"]["prior_founder_review"]["feedback"] == "Preserve and repair the saved code."
    repaired = start(ready_plan, next_quote, key="second-repair-source")
    _, payload = claim({**ready_plan, **repaired})
    from app.builder.orchestration import _prepare
    prepared = _prepare(payload)
    assert prepared["launch"]["phase"] == "prepared"
    assert prepared["inputs"]["source"]["id"] == artifact_id


def test_repair_source_transport_reads_exact_scoped_prior_capture(lifecycle_run, monkeypatch):
    from app.builder.orchestration import _seed_repair_source
    from app.builder import task_client
    from app.db.supymem_models import BuildArtifact, BuildRun

    d = lifecycle_run
    retire(d)
    with d["factory"]() as session:
        run = session.get(BuildRun, d["build_run_id"])
        source = {"kind": "artifact", "id": run.artifact_id,
                  "sha256": session.get(BuildArtifact, run.artifact_id).sha256,
                  "size_bytes": session.get(BuildArtifact, run.artifact_id).size_bytes}
        expected = json.loads(run.spec)["artifact_capture"]
    observed = []
    monkeypatch.setattr(task_client, "seed_source", lambda *args, **kwargs: observed.append((args, kwargs)))
    _seed_repair_source(d["build_run_id"], 1, source, "static_landing_v1")
    assert len(observed) == 1
    assert observed[0][0] == (d["build_run_id"], 1)
    assert observed[0][1]["profile"] == "static_landing_v1"
    assert observed[0][1]["sha256"] == expected["sha256"]
    assert observed[0][1]["manifest"] == expected["manifest"]
    assert hashlib.sha256(observed[0][1]["blob"]).hexdigest() == expected["sha256"]
    with pytest.raises(Exception, match="build_repair_source_scope_invalid"):
        _seed_repair_source(d["build_run_id"], 1, {**source, "sha256": "0" * 64}, "static_landing_v1")


def test_first_product_build_can_run_ahead_of_business_milestones(ready_plan, monkeypatch):
    from app.builder import budgets
    from app.db.supymem_models import ExecutionPlan, PlanMilestone

    with ready_plan["factory"]() as session:
        plan = session.get(ExecutionPlan, ready_plan["plan_id"])
        doc = json.loads(plan.structured)
        milestone = doc["proposal"]["milestones"][0]
        milestone["kind"] = "core"
        milestone["depends_on"] = ["m2"]
        milestone["tasks"][0]["landing_expectations"] = None
        plan.structured = json.dumps(doc)
        session.get(PlanMilestone, doc["entity_ids"]["milestones"][milestone["key"]]).kind = "core"
        dependency = session.get(PlanMilestone, doc["entity_ids"]["milestones"]["m2"])
        assert dependency.state == "pending"
        session.commit()

    policy = runtime()
    monkeypatch.setattr(budgets, "runtime_quote", lambda _wid, **_kwargs: copy.deepcopy(policy))
    reviewed = quote(ready_plan)
    assert reviewed["ready"] is True
    assert reviewed["artifact_profile"] == "founder_product_v1"

    accepted = start(ready_plan, reviewed, key="build-ahead")
    assert accepted["state"] == "queued"
    with ready_plan["factory"]() as session:
        dependency = session.get(PlanMilestone, doc["entity_ids"]["milestones"]["m2"])
        assert dependency.state == "pending"


def test_crash_before_launch_intent_recovered_by_one_server_watch(lifecycle_run):
    from app.db.models import Job
    from app.db.supymem_models import BuildRun
    d = lifecycle_run
    job, _ = claim(d)
    with d["factory"]() as session:
        session.get(Job, job.id).status = "failed_dead"
        session.commit()
    # The sweeper scans all unfinished runs in this integration database, so
    # unrelated orphaned fixtures may also be adopted. Assert the exact run's
    # one-watch fence rather than a global count.
    assert d["m"].enqueue_orphaned_watches() >= 1
    with d["factory"]() as session:
        first_watch = json.loads(session.get(BuildRun, d["build_run_id"]).spec)["watch_job_id"]
    assert first_watch != job.id
    d["m"].enqueue_orphaned_watches()
    with d["factory"]() as session:
        assert json.loads(session.get(BuildRun, d["build_run_id"]).spec)["watch_job_id"] == first_watch
    result = step(d)
    assert result["state"] == "running" and len(d["builder"].launches) == 1


def test_expired_never_started_v2_pool_reconciles_without_infrastructure(lifecycle_run):
    from datetime import timedelta
    from app.builder import reconcile
    from app.builder.model_proxy import _locked
    from app.credits import pools
    from app.credits._txn import naive_utc
    d = lifecycle_run
    with d["factory"]() as session:
        ctx = _locked(session, d["build_run_id"])
        ctx.pool.expires_at = naive_utc() - timedelta(seconds=1)
        pools.mark_reconciliation(session, ctx, "build_budget_expired")
        session.commit()
    assert reconcile.reconcile_held_builds()["closed"] == 1
    assert saved(d)["closed"] and saved(d)["reserved"] == 0 and not d["builder"].launches


def test_uncertain_model_usage_stays_held_then_platform_absorbs_after_cleanup(lifecycle_run, monkeypatch):
    from app.builder import model_proxy
    d = lifecycle_run
    calls = []
    def unknown(wid, body):
        calls.append(True)
        raise TimeoutError("synthetic lost provider response")
    monkeypatch.setattr(model_proxy, "_provider_call", unknown)
    step(d, "build.run")
    step(d)
    result = step(d)
    assert result["closed"] is None and result["reserved"] > 0 and not result["spec"]["execution_verified"]
    assert result["spec"]["cleanup_state"] == "complete" and not d["qa"].launches
    step(d)
    assert calls == [True] and len(d["builder"].stops) == 1
    from datetime import timedelta
    from sqlalchemy import select
    from app.builder import reconcile
    from app.credits._txn import naive_utc
    from app.db.supymem_models import BuildModelCall
    with d["factory"]() as session:
        row = session.scalar(select(BuildModelCall).where(BuildModelCall.build_run_id == d["build_run_id"],
                                                           BuildModelCall.state == "uncertain"))
        row.updated_at = naive_utc() - timedelta(seconds=reconcile.PLATFORM_ABSORPTION_SECONDS + 1)
        session.commit()
    outcome = reconcile.reconcile_held_builds()
    assert outcome["absorbed"] == 1 and outcome["closed"] == 1
    final = saved(d)
    assert final["closed"] and final["reserved"] == 0 and final["state"] == "failed"
    from app.builder.execution import events
    activity = events(d["build_run_id"], actor=d["actor"])["items"]
    assert [event["kind"] for event in activity].count("provider_cost_absorbed") == 1


def test_resource_changes_after_capture_prevent_new_qa_spend(lifecycle_run):
    from app.planning.resources import update_inventory
    d = lifecycle_run
    retire(d)
    update_inventory(d["wid"], d["vid"], [], False)
    result = step(d)
    assert result["closed"] and result["state"] == "failed" and not d["qa"].launches
    assert result["spec"]["artifact_verified"] and not result["spec"]["execution_verified"]


def test_concurrent_founder_reviews_commit_one_task_transition(lifecycle_run):
    from tests.integration.test_postgres_auth_security import concurrent
    from app.builder.execution import read, review
    from app.db.supymem_models import BuildEvent
    from sqlalchemy import select
    d = lifecycle_run
    retire(d)
    step(d)
    d["qa_state"]["phase"] = "completed"
    step(d)
    step(d)
    value = read(d["build_run_id"], actor=d["actor"])
    body = {"actor": d["actor"], "source_artifact_id": value["artifact_id"], "report_digest": value["review"]["report_digest"], "decision": "accept"}
    results = concurrent(lambda: review(d["build_run_id"], **body))
    assert all(result == results[0] for result in results)
    with d["factory"]() as session:
        assert len(list(session.scalars(select(BuildEvent.id).where(BuildEvent.build_run_id == d["build_run_id"], BuildEvent.kind == "founder_review")))) == 1

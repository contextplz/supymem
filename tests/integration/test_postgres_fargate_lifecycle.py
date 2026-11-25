"""Real migrated PostgreSQL/leases/accounting; only AWS and task HTTP are fixtures."""
import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from tests.fargate_fixtures import FakeAWS, profile_data
from tests.integration.test_postgres_auth_security import pg_database as pg_database
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db
from tests.integration.test_postgres_planning import planning as planning
from tests.integration.test_postgres_build_intents import ready_plan as ready_plan, quote, start


@pytest.fixture()
def cloud(ready_plan, monkeypatch):
    from app.builder import budgets, orchestration as m
    from app.infra.fargate_build import FargateBuildProvider, FargateProfile
    d = ready_plan
    monkeypatch.setenv("COMPANY_OS_VAULT_SECRET", "local-fargate-test-vault-at-least-thirty-two")
    runtime = {"ready": True, "reasons": [], "ceiling_micro": 10000,
        "model_ceiling_micro": 7000, "compute_ceiling_micro": 2000, "artifact_ceiling_micro": 1000,
        "model": "DeepSeek-V4-Pro", "model_config": {"model": "DeepSeek-V4-Pro", "provider": "azure_foundry", "source": "platform_default"},
        "runtime_policy": {"profile": "fargate_openhands_v1", "timeout_seconds": 1800, "max_steps": 40,
                           "tool_names": ["terminal", "file_editor", "finish", "think"]},
        "pricing": {"source": "isolated test fixture"}, "compute_pricing": {"source": "isolated test fixture", "micro_per_second": "1.5", "minimum_seconds": 60},
        "fargate_profile": profile_data()}
    monkeypatch.setattr(budgets, "runtime_quote", lambda wid, **_kwargs: runtime)
    accepted = start(d, quote(d))
    profile = FargateProfile.from_dict(profile_data())
    aws = FakeAWS(profile)
    provider = FargateBuildProvider(profile, ecs=aws, ec2=aws)
    monkeypatch.setattr(m, "configured_provider", lambda expected: provider)
    commands = []
    monkeypatch.setattr(m.task_client, "state", lambda rid, gen: {"build_id": rid, "generation": gen, "commands": commands})
    def submit(rid, gen, body):
        row = {"command_id": body["command_id"], "kind": body["kind"], "status": "running"}
        commands.append(row)
        return row
    monkeypatch.setattr(m.task_client, "submit", submit)
    return {**d, **accepted, "m": m, "aws": aws, "provider": provider, "commands": commands}


def claim(d, kind="build.run", worker="cloud-test"):
    from app.core.jobs import lease
    from app.credits._txn import naive_utc
    from app.db.models import Job
    with d["factory"]() as session:
        for job in session.scalars(select(Job).where(Job.kind == kind, Job.status == "scheduled")):
            job.run_after = job.scheduled_for = naive_utc()
        session.commit()
    with d["factory"]() as session:
        jobs = lease(session, worker, kinds=[kind], n=100)
        session.commit()
    job = next(row for row in jobs if row.payload["build_run_id"] == d["build_run_id"])
    return job, {**job.payload, "_job_id": job.id, "_worker_id": job.worker_id, "_job_attempt": job.attempts}


def saved(d):
    from app.db.models import CreditAccount, CreditReservation
    from app.db.supymem_models import BuildRun
    with d["factory"]() as session:
        run = session.get(BuildRun, d["build_run_id"])
        spec = json.loads(run.spec)
        pool = session.get(CreditReservation, run.reservation_id)
        account = session.get(CreditAccount, pool.account_id)
        return {"spec": spec, "state": run.state, "arn": run.sandbox_ref, "closed": run.closed_at,
                "remaining": pool.remaining_micro, "reserved": account.reserved_micro, "spent": run.credits_used,
                "pool_state": pool.status}


def test_real_run_persists_exact_encrypted_launch_before_cloud_and_single_command(cloud):
    d = cloud
    _, payload = claim(d)
    original = d["provider"].launch
    def launch(body):
        snapshot = saved(d)
        assert snapshot["spec"]["launch"]["phase"] == "calling"
        assert snapshot["remaining"] == 7000 and snapshot["reserved"] == 10000
        assert "MANAGED_MODEL_TOKEN" not in json.dumps(snapshot["spec"])
        return original(body)
    d["provider"].launch = launch
    d["m"].run(payload)
    snapshot = saved(d)
    assert snapshot["state"] == "running" and snapshot["arn"] and snapshot["spec"]["managed_task"]
    assert len(d["commands"]) == len(d["aws"].tasks) == 1
    _, watch = claim(d, "build.watch")
    d["m"].watch(watch)
    assert len(d["commands"]) == len(d["aws"].tasks) == 1
    assert not saved(d)["spec"]["execution_verified"]


def test_failed_command_retains_safe_sandbox_diagnostics_before_cleanup(cloud):
    d = cloud
    _, payload = claim(d)
    d["m"].run(payload)
    d["commands"][0].update(
        status="failed",
        driver_status="error",
        error_code="managed_intent_connection_failed",
        detail="must never be persisted: token=secret",
    )
    _, watch = claim(d, "build.watch")
    d["m"].watch(watch)
    command = saved(d)["spec"]["managed_command"]
    assert command["state"] == "failed"
    assert command["driver_status"] == "error"
    assert command["error_code"] == "managed_intent_connection_failed"
    assert "detail" not in command


def test_failed_command_drops_unbounded_sandbox_diagnostics(cloud):
    d = cloud
    _, payload = claim(d)
    d["m"].run(payload)
    d["commands"][0].update(status="failed", driver_status="error\nsecret", error_code="x" * 129)
    _, watch = claim(d, "build.watch")
    d["m"].watch(watch)
    command = saved(d)["spec"]["managed_command"]
    assert "driver_status" not in command and "error_code" not in command


def test_response_lost_run_task_recovers_same_token_and_task(cloud):
    d = cloud
    d["aws"].lose_launch_response = True
    _, payload = claim(d)
    d["m"].run(payload)
    assert saved(d)["spec"]["launch"]["phase"] == "uncertain"
    assert saved(d)["reserved"] == 10000 and len(d["aws"].tasks) == 1
    _, watch = claim(d, "build.watch")
    d["m"].watch(watch)
    assert saved(d)["state"] == "running" and len(d["aws"].tasks) == 1
    assert len({entry["clientToken"] for entry in d["aws"].launches}) == 1


def test_rejected_task_releases_only_definite_no_external_execution(cloud):
    d = cloud
    d["aws"].rejected = True
    _, payload = claim(d)
    d["m"].run(payload)
    snapshot = saved(d)
    assert snapshot["closed"] and snapshot["reserved"] == snapshot["spent"] == 0
    assert not d["aws"].tasks and not d["commands"]


def test_cancel_after_lease_before_launch_releases_without_task(cloud):
    from app.builder import execution
    d = cloud
    _, payload = claim(d)
    execution.cancel(d["build_run_id"], actor=d["actor"])
    d["m"].run(payload)
    assert saved(d)["closed"] and saved(d)["reserved"] == 0
    assert not d["aws"].launches


def test_revoked_founder_can_only_watch_stop_and_settle(cloud):
    from app.db.models import Workspace
    from app.worker.main import _job_scope
    d = cloud
    _, payload = claim(d)
    d["m"].run(payload)
    with d["factory"]() as session:
        row = session.get(Workspace, d["wid"])
        row.settings_json = json.dumps({"blocked": True})
        session.commit()
    job, watch = claim(d, "build.watch")
    scope = _job_scope(job)
    assert scope.workspace_id == d["wid"]
    d["m"].watch(watch)
    snapshot = saved(d)
    assert d["aws"].stops == [snapshot["arn"]] and len(d["commands"]) == 1
    assert snapshot["spec"]["cleanup_state"] == "complete" and snapshot["spent"] > 0
    assert snapshot["reserved"] > 0 and not snapshot["closed"]  # Unknown log/artifact usage held.
    job.payload["watch_receipt"] = "forged"
    with pytest.raises(Exception, match="build_watch_scope_invalid"):
        _job_scope(job)


def test_finished_agent_does_not_claim_artifact_or_qa_or_stop(cloud):
    d = cloud
    _, payload = claim(d)
    d["m"].run(payload)
    d["commands"][0].update(status="completed", driver_status="finished")
    _, watch = claim(d, "build.watch")
    d["m"].watch(watch)
    snapshot = saved(d)
    assert snapshot["spec"]["phase"] == "artifact_pending"
    assert snapshot["spec"]["artifact_capture"]["state"] == "awaiting_durable_storage"
    assert snapshot["spec"]["qa_state"] == "not_run" and not snapshot["spec"]["execution_verified"]
    assert not d["aws"].stops


def test_late_ambiguous_launch_does_not_reissue_expired_idempotency_token(cloud):
    from app.db.supymem_models import BuildRun
    from app.credits._txn import naive_utc
    d = cloud
    d["aws"].lose_launch_response = True
    _, payload = claim(d)
    d["m"].run(payload)
    with d["factory"]() as session:
        row = session.get(BuildRun, d["build_run_id"])
        spec = json.loads(row.spec)
        spec["launch"]["first_attempt_at"] = (naive_utc() - timedelta(minutes=6)).isoformat()
        row.spec = json.dumps(spec)
        session.commit()
    _, watch = claim(d, "build.watch")
    d["m"].watch(watch)
    assert len(d["aws"].launches) == 1 and saved(d)["reserved"] == 10000


def test_old_lease_cannot_bind_a_late_aws_response(cloud):
    from app.db.models import Job
    d = cloud
    _, payload = claim(d)
    original = d["provider"].launch
    def launch(body):
        arn = original(body)
        with d["factory"]() as session:
            job = session.get(Job, payload["_job_id"])
            job.locked_by = "new-worker"
            job.attempts += 1
            session.commit()
        return arn
    d["provider"].launch = launch
    with pytest.raises(Exception, match="job_fence_lost"):
        d["m"].run(payload)
    assert saved(d)["arn"] is None and len(d["aws"].tasks) == 1 and not d["commands"]


def test_cancelled_ambiguous_task_is_discovered_and_stopped_without_relaunch(cloud):
    from app.builder import execution
    d = cloud
    d["aws"].lose_launch_response = True
    _, payload = claim(d)
    d["m"].run(payload)
    execution.cancel(d["build_run_id"], actor=d["actor"])
    _, watch = claim(d, "build.watch")
    d["m"].watch(watch)
    assert len(d["aws"].launches) == 1 and len(d["aws"].stops) == 1
    assert saved(d)["spec"]["cleanup_state"] == "complete" and not d["commands"]


def test_no_discovery_result_holds_budget_without_assuming_task_absence(cloud):
    from app.builder import execution
    d = cloud
    d["aws"].lose_launch_response = True
    _, payload = claim(d)
    d["m"].run(payload)
    execution.cancel(d["build_run_id"], actor=d["actor"])
    d["aws"].list_tasks = lambda **kwargs: {"taskArns": []}
    _, watch = claim(d, "build.watch")
    d["m"].watch(watch)
    snapshot = saved(d)
    assert len(d["aws"].launches) == 1 and snapshot["reserved"] == 10000
    assert snapshot["spec"]["cleanup_state"] != "complete" and not snapshot["closed"]


def test_stop_response_loss_rechecks_authoritative_stopped_task_before_retry(cloud):
    from app.builder import execution
    d = cloud
    _, payload = claim(d)
    d["m"].run(payload)
    execution.cancel(d["build_run_id"], actor=d["actor"])
    d["aws"].lose_stop_response = True
    _, watch = claim(d, "build.watch")
    d["m"].watch(watch)
    assert saved(d)["spec"]["cleanup_state"] != "complete"
    _, next_watch = claim(d, "build.watch", "replacement-worker")
    d["m"].watch(next_watch)
    assert len(d["aws"].stops) == 1 and saved(d)["spec"]["cleanup_state"] == "complete"


def test_exhausted_worker_recovers_via_server_watch_and_fences_late_original(cloud):
    from app.db.models import Job
    from app.core.jobs import FatalError
    d = cloud
    _, payload = claim(d)
    spec = d["m"]._prepare(payload)
    request = d["m"]._launch_request(payload, spec)
    original = d["provider"].launch
    def crash(body):
        original(body)
        raise SystemExit("fixture process crash")
    d["provider"].launch = crash
    with pytest.raises(SystemExit):
        d["m"]._cloud_launch(payload, d["provider"], request)
    with d["factory"]() as session:
        row = session.get(Job, payload["_job_id"])
        row.status = "failed_dead"
        row.locked_by = row.locked_at = None
        session.commit()
    assert d["m"].enqueue_orphaned_watches() == 1
    assert d["m"].enqueue_orphaned_watches() == 0
    with pytest.raises(FatalError, match="job_fence_lost"):
        d["m"].run(payload)
    d["provider"].launch = original
    _, watch = claim(d, "build.watch", "recovery-controller")
    d["m"].watch(watch)
    assert saved(d)["state"] == "running" and len(d["aws"].tasks) == 1


def test_rejected_retry_cannot_refund_a_prior_response_lost_task(cloud):
    d = cloud
    d["aws"].lose_launch_response = True
    _, payload = claim(d)
    d["m"].run(payload)
    d["aws"].rejected = True
    _, watch = claim(d, "build.watch")
    d["m"].watch(watch)
    snapshot = saved(d)
    assert snapshot["reserved"] == 10000 and snapshot["closed"] is None
    assert snapshot["spec"]["launch"]["discovery_only"] is True
    calls = len(d["aws"].launches)
    _, recovery = claim(d, "build.watch")
    d["m"].watch(recovery)
    assert len(d["aws"].launches) == calls and len(d["aws"].tasks) == 1
    assert saved(d)["arn"] and saved(d)["state"] == "running"


def test_changed_approved_inputs_before_worker_fail_without_cloud_or_hold(cloud):
    from app.planning.resources import update_inventory
    d = cloud
    update_inventory(d["wid"], d["vid"], [], False)
    _, payload = claim(d)
    d["m"].run(payload)
    snapshot = saved(d)
    assert snapshot["state"] == "failed" and snapshot["closed"]
    assert snapshot["reserved"] == snapshot["spent"] == 0 and not d["aws"].launches


def test_cloud_command_has_confirmed_product_context_and_constraints(cloud):
    d = cloud
    _, payload = claim(d)
    d["m"].run(payload)
    command = saved(d)["spec"]["managed_command"]["body"]
    content = json.loads(command["text"])
    expected = saved(d)["spec"]["inputs"]["product_context"]
    assert content["product"] == expected and expected["revision_id"]
    assert expected["idea"] and expected["first_paid_user_goal"]
    assert content["landing_contract"] == saved(d)["spec"]["inputs"]["task"].get("landing_expectations", {}).get("contract")
    assert "every contract.required_text value verbatim as visible body text" in content["objective"]
    assert "visible in the initial page state before any click or keyboard action" in content["objective"]
    assert "sole occurrence inside hidden, collapsed" in content["objective"]
    assert "accessibility role must equal action.role" in content["objective"]
    assert "accessible name must equal action.name exactly" in content["objective"]
    assert "external-link hrefs must copy action.target exactly" in content["objective"]
    assert "all network access blocked" in content["objective"]
    assert "Do not reference external stylesheets" in content["objective"]
    assert "Do not add undeclared buttons, links, forms" in content["objective"]
    assert "Never render a form, input, submit control" in content["objective"]
    assert "local-only waitlist/signup simulation" in content["objective"]
    assert "fake production behavior and is forbidden" in content["objective"]


def test_attestation_policy_failure_revokes_commands_then_stops_owned_task(cloud):
    d = cloud
    d["aws"].eni["Association"] = {"PublicIp": "203.0.113.1"}
    _, payload = claim(d)
    d["m"].run(payload)
    assert not d["commands"] and saved(d)["spec"]["credentials_revoked"]
    _, watch = claim(d, "build.watch")
    d["m"].watch(watch)
    assert d["aws"].stops and saved(d)["spec"]["cleanup_state"] == "complete"


def test_repeated_completion_preserves_durable_artifact_receipt(cloud, monkeypatch):
    from app.builder import artifacts
    from app.db.supymem_models import BuildRun
    d = cloud
    _, payload = claim(d)
    d["m"].run(payload)
    d["commands"][0].update(status="completed", driver_status="finished")
    calls = []
    def store(rid, cid, gen, controller, attempt):
        calls.append(cid)
        with d["factory"]() as session:
            row = session.get(BuildRun, rid)
            spec = json.loads(row.spec)
            if spec.get("artifact_capture", {}).get("state") == "stored":
                assert spec["artifact_capture"]["artifact_id"] == "artifact-fixture-receipt"
            spec["artifact_capture"] = {"state": "stored", "command_id": cid, "artifact_id": "artifact-fixture-receipt"}
            row.spec = json.dumps(spec)
            session.commit()
        return {"artifact_id": "artifact-fixture-receipt"}
    monkeypatch.setattr(artifacts, "import_completed_artifact", store)
    for _ in range(2):
        _, watch = claim(d, "build.watch")
        d["m"].watch(watch)
    spec = saved(d)["spec"]
    assert spec["phase"] == "qa_pending" and spec["artifact_capture"]["state"] == "stored"
    assert spec["qa_state"] == "not_run" and not spec["execution_verified"]
    assert len(calls) == 2 and not d["aws"].stops


def test_credit_account_suspended_after_launch_permits_only_cleanup(cloud):
    from app.db.models import CreditAccount, CreditReservation
    from app.db.supymem_models import BuildRun
    d = cloud
    _, payload = claim(d)
    d["m"].run(payload)
    with d["factory"]() as session:
        row = session.get(BuildRun, d["build_run_id"])
        pool = session.get(CreditReservation, row.reservation_id)
        session.get(CreditAccount, pool.account_id).status = "suspended"
        session.commit()
    _, watch = claim(d, "build.watch")
    d["m"].watch(watch)
    snapshot = saved(d)
    assert snapshot["spec"]["credentials_revoked"]
    assert snapshot["spec"]["reconciliation_reason"] == "build_credit_account_suspended"
    assert snapshot["spec"]["cleanup_state"] == "complete" and len(d["commands"]) == 1
    assert len(d["aws"].tasks) == len(d["aws"].stops) == 1
    assert snapshot["spent"] > 0 and not snapshot["closed"]

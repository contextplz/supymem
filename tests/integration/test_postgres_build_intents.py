"""Approved plan → quoted budget → durable build intent on migrated PostgreSQL.

The sole runtime quote seam is a fixture; no cloud task, SDK or provider runs.
"""
import json
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.integration.test_postgres_auth_security import concurrent, pg_database as pg_database
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db
from tests.integration.test_postgres_planning import planning as planning, run
from tests.security.test_core_signed_boundary import SECRET, headers


@pytest.fixture()
def ready_plan(planning, monkeypatch):
    from app.planning import compiler, execution as planning_execution
    from app.infra.apps import create_draft
    d = planning
    from app.db.models import Job
    with d["factory"]() as session:
        session.get(Job, d["job_id"]).priority = -100
        session.commit()
    assert run(d).id == d["job_id"]
    plan = compiler.get_plan(planning_execution.identifier(d["job_id"]))
    compiler.approve_plan(plan["id"], person_id=d["actor"], review_token=plan["review_token"])
    app = create_draft(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"], name="Bakery test",
                       slug="build-" + uuid.uuid4().hex)
    return {**d, "app_id": app["id"], "plan_id": plan["id"], "plan_task_id": plan["milestones"][0]["tasks"][0]["id"]}


def quote(d):
    from app.builder.execution import estimate
    return estimate(d["app_id"], actor=d["actor"], plan_id=d["plan_id"], plan_task_id=d["plan_task_id"])


def accepted_runtime(monkeypatch):
    from app.builder import budgets
    monkeypatch.setattr(budgets, "runtime_quote", lambda wid, **_kwargs: {"ready": True, "reasons": [], "ceiling_micro": 10000,
        "model_ceiling_micro": 7000, "compute_ceiling_micro": 2000, "artifact_ceiling_micro": 1000,
        "model": "DeepSeek-V4-Pro", "model_config": {"model": "DeepSeek-V4-Pro", "provider": "azure_foundry", "source": "platform_default"},
        "runtime_policy": {"profile": "isolated-test-fixture", "timeout_seconds": 1800},
        "pricing": {"source": "test fixture only"}})


def start(d, q, key="approved-build"):
    from app.builder.execution import start
    return start(d["app_id"], actor=d["actor"], quote_id=q["quote_id"], input_digest=q["input_digest"],
                 approve_budget_micro=q["ceiling_micro"], idempotency_key=key)


def test_unaccepted_runtime_quote_has_unknown_budget_and_no_hold(ready_plan):
    from app.db.supymem_models import BuildRun
    q = quote(ready_plan)
    assert q["ready"] is False and q["ceiling_micro"] is None
    assert "build_runtime_configuration_unverified" in q["reasons"]
    with ready_plan["factory"]() as session:
        assert not session.scalar(select(BuildRun.id).where(BuildRun.app_id == ready_plan["app_id"]))


def test_concurrent_intent_retries_make_one_job_run_and_pool(ready_plan, monkeypatch):
    from app.db.models import CreditAccount, CreditReservation, Job
    from app.db.supymem_models import BuildRun
    d = ready_plan
    accepted_runtime(monkeypatch)
    q = quote(d)
    assert q["ready"]
    results = concurrent(lambda: start(d, q))
    assert len({r["build_run_id"] for r in results}) == 1
    with d["factory"]() as session:
        rows = session.scalars(select(BuildRun).where(BuildRun.app_id == d["app_id"])).all()
        assert len(rows) == 1 and rows[0].job_id
        job = session.get(Job, rows[0].job_id)
        assert job.kind == "build.run" and job.status == "scheduled"
        pool = session.get(CreditReservation, rows[0].reservation_id)
        account = session.get(CreditAccount, pool.account_id)
        assert pool.is_pool and pool.remaining_micro == 10000 and account.reserved_micro == 10000


def test_approval_mismatch_and_changed_inventory_do_not_reserve(ready_plan, monkeypatch):
    from app.builder import execution
    from app.planning.resources import update_inventory
    d = ready_plan
    accepted_runtime(monkeypatch)
    q = quote(d)
    with pytest.raises(execution.BuildError, match="approval_mismatch"):
        execution.start(d["app_id"], actor=d["actor"], quote_id=q["quote_id"], input_digest=q["input_digest"],
                        approve_budget_micro=1, idempotency_key="wrong-budget")
    update_inventory(d["wid"], d["vid"], [], False)
    with pytest.raises(execution.BuildError, match="inputs_changed"):
        start(d, q)


def test_consumed_quote_cannot_make_second_run_and_body_key_mismatch_conflicts(ready_plan, monkeypatch):
    from app.builder import execution
    d = ready_plan
    accepted_runtime(monkeypatch)
    q = quote(d)
    result = start(d, q)
    execution.cancel(result["build_run_id"], actor=d["actor"])
    with pytest.raises(execution.BuildError, match="quote_consumed"):
        start(d, q, "second-key")
    changed = {**q, "input_digest": "0" * 64}
    with pytest.raises(execution.BuildError, match="idempotency_conflict"):
        start(d, changed)
    assert start(d, q)["build_run_id"] == result["build_run_id"]


def test_queued_cancel_is_atomic_but_leased_cancel_waits_for_cleanup(ready_plan, monkeypatch):
    from app.builder import execution
    from app.core.jobs import lease
    d = ready_plan
    accepted_runtime(monkeypatch)
    result = start(d, quote(d))
    cancelled = execution.cancel(result["build_run_id"], actor=d["actor"])
    assert cancelled == {"state": "cancelled", "cleanup_state": "complete"}
    assert execution.read(result["build_run_id"], actor=d["actor"])["usage"]["reserved_micro"] == 0
    next_run = start(d, quote(d), "next-run")
    with d["factory"]() as session:
        assert next_run["job_id"] in {j.id for j in lease(session, "test-cloud-launcher", kinds=["build.run"], n=100)}
        session.commit()
    pending = execution.cancel(next_run["build_run_id"], actor=d["actor"])
    assert pending["state"] == "cancelling" and pending["cleanup_state"] != "complete"
    assert execution.read(next_run["build_run_id"], actor=d["actor"])["usage"]["reserved_micro"] == 10000


def test_manual_founder_task_is_never_an_executable_capability(ready_plan, monkeypatch):
    from app.builder.execution import estimate, BuildError
    from app.db.supymem_models import ExecutionPlan
    d = ready_plan
    accepted_runtime(monkeypatch)
    with d["factory"]() as session:
        row = session.get(ExecutionPlan, d["plan_id"])
        doc = json.loads(row.structured)
        doc["proposal"]["milestones"][0]["tasks"][0]["implementation"] = "founder_action"
        row.structured = json.dumps(doc)
        session.commit()
    with pytest.raises(BuildError, match="requires_founder_action"):
        estimate(d["app_id"], actor=d["actor"], plan_id=d["plan_id"], plan_task_id=d["plan_task_id"])


def test_build_dependencies_use_persisted_milestone_done_state(ready_plan):
    from app.builder.execution import BuildError
    from app.db.supymem_models import ExecutionPlan, PlanMilestone
    d = ready_plan
    with d["factory"]() as session:
        plan = session.get(ExecutionPlan, d["plan_id"])
        doc = json.loads(plan.structured)
        # Keep the first landing task and explicitly bind it to another real
        # normalized milestone, whose FSM terminal state is "done".
        doc["proposal"]["milestones"][0]["depends_on"] = ["m2"]
        dependency_id = doc["entity_ids"]["milestones"]["m2"]
        plan.structured = json.dumps(doc)
        session.commit()
    with pytest.raises(BuildError, match="dependencies_incomplete"):
        quote(d)
    with d["factory"]() as session:
        session.get(PlanMilestone, dependency_id).state = "done"
        session.commit()
    assert quote(d)["ready"] is False  # dependency passes; runtime still gated


def test_real_assembled_http_boundary_build_creation_replay_and_foreign_denial(ready_plan, monkeypatch):
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", SECRET)
    monkeypatch.setenv("COMPANY_OS_VAULT_SECRET", "isolated-build-vault-more-than-32-bytes")
    monkeypatch.setenv("ADMIN_REQUIRE_TOTP", "1")
    monkeypatch.setenv("ADMIN_BOOTSTRAP_EMAILS", "operator@example.test")
    from app.main import create_app
    from app.db.models import WorkspaceMember
    from app.authz.workspace import ensure_subject_person
    from core.clock import utcnow
    d = ready_plan
    accepted_runtime(monkeypatch)
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", SECRET)
    # No TestClient lifespan: fixture already selected the migrated isolated DB.
    client = TestClient(create_app())
    def request(method, path, subject=None, **kwargs):
        h = headers(path, method, subject or d["actor"])
        h["Idempotency-Key"] = "http-build"
        return client.request(method, path, headers=h, **kwargs)
    path = f"/apps/{d['app_id']}/builds/estimate"
    response = request("POST", path, json={"plan_id": d["plan_id"], "plan_task_id": d["plan_task_id"]})
    assert response.status_code == 200, response.text
    q = response.json()
    path = f"/apps/{d['app_id']}/builds"
    payload = {"quote_id": q["quote_id"], "input_digest": q["input_digest"], "approve_budget_micro": q["ceiling_micro"]}
    response = request("POST", path, json=payload)
    assert response.status_code == 202, response.text
    rid = response.json()["build_run_id"]
    assert request("POST", path, json=payload).json()["build_run_id"] == rid
    assert request("GET", f"/builds/{rid}", "foreign-founder").status_code == 404
    assert client.get(f"/builds/{rid}", headers={"X-Subject": d["actor"]}).status_code == 401
    viewer = "viewer-" + uuid.uuid4().hex
    ensure_subject_person(viewer)
    with d["factory"]() as session:
        session.add(WorkspaceMember(id="member-" + uuid.uuid4().hex, workspace_id=d["wid"], person_id=viewer, role="viewer", created_at=utcnow()))
        session.commit()
    assert request("GET", f"/builds/{rid}", viewer).status_code == 200
    assert request("POST", f"/builds/{rid}/cancel", viewer).status_code == 403
    assert request("POST", f"/ventures/{d['vid']}/apps", viewer, json={"name": "No", "slug": "viewer-forbidden"}).status_code == 403
    assert request("GET", f"/builds/{rid}").json()["verified_execution"] is False
    from app.db.supymem_models import BuildRun, QaRun
    legacy_id = "legacy-" + uuid.uuid4().hex
    with d["factory"]() as session:
        session.add(BuildRun(id=legacy_id, app_id=d["app_id"], workspace_id=d["wid"], venture_id=d["vid"],
                            trigger="legacy", agent="fixture", state="succeeded", runtime_version=0))
        session.flush()
        session.add(QaRun(id="legacy-qa-" + uuid.uuid4().hex, app_id=d["app_id"], workspace_id=d["wid"],
            venture_id=d["vid"], build_run_id=legacy_id, state="passed", passed=123, failed=0, created_at=utcnow()))
        session.commit()
    legacy = request("GET", f"/builds/{legacy_id}").json()
    assert legacy["state"] == "unverified" and legacy["qa_state"] == "not_run"
    assert legacy["usage"]["consumed_micro"] is None
    qa = request("GET", f"/apps/{d['app_id']}/qa").json()["items"][0]
    assert qa["state"] == "not_run" and qa["passed"] is None and qa["failed"] is None
    assert request("POST", f"/builds/{rid}/cancel").status_code == 200
    client.close()


def test_draft_creation_replay_requires_same_actor_and_body(journey_db):
    from app.infra.apps import create_draft
    d = journey_db
    options = {"workspace_id": d["wid"], "venture_id": d["vid"], "actor": d["actor"],
               "name": "Bakery draft", "slug": "draft-" + uuid.uuid4().hex, "idempotency_key": "draft-intent"}
    results = concurrent(lambda: create_draft(**options))
    assert len({row["id"] for row in results}) == 1
    with pytest.raises(ValueError, match="idempotency_conflict"):
        create_draft(**{**options, "name": "Changed product"})
    with pytest.raises(KeyError):
        create_draft(**{**options, "actor": "other-founder"})
    legacy = {**options, "slug": "legacy-" + uuid.uuid4().hex, "idempotency_key": None}
    assert create_draft(**legacy)["id"] == create_draft(**legacy)["id"]


def test_expired_never_leased_job_reconciles_without_desktop(ready_plan, monkeypatch):
    from app.builder.reconcile import reconcile_held_builds
    from app.builder.execution import read
    from app.credits.pools import expire_pools
    from app.credits._txn import naive_utc
    from app.db.models import CreditReservation, Job
    from app.db.supymem_models import BuildRun
    d = ready_plan
    accepted_runtime(monkeypatch)
    result = start(d, quote(d))
    with d["factory"]() as session:
        row = session.get(BuildRun, result["build_run_id"])
        session.get(CreditReservation, row.reservation_id).expires_at = naive_utc() - timedelta(seconds=1)
        session.commit()
    expire_pools(naive_utc())
    assert reconcile_held_builds()["closed"] >= 1
    assert read(result["build_run_id"], actor=d["actor"])["usage"]["reserved_micro"] == 0
    with d["factory"]() as session:
        assert session.get(Job, result["job_id"]).status == "cancelled"

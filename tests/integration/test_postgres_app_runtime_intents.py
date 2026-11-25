"""Real PostgreSQL admission for one immutable founder-app runtime generation."""
import json
import os
import secrets
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from sqlalchemy.engine import make_url

pytest_plugins = (
    "tests.integration.test_postgres_auth_security",
    "tests.integration.test_postgres_journey_jobs",
)


def accepted_product(d):
    from app.db.supymem_models import (
        BuildArtifact,
        BuildRun,
        ExecutionPlan,
        FounderApp,
        PlanMilestone,
        PlanTask,
        QaRun,
    )
    from core.clock import utcnow
    from core.ids import new_id

    now = utcnow()
    ids = {name: new_id(prefix) for name, prefix in {
        "app": "app", "plan": "pln", "milestone": "mls", "task": "tsk",
        "build": "bld", "artifact": "bar", "qa": "qar",
    }.items()}
    from core.artifact_snapshot import manifest_digest

    sha, contract, report = "a" * 64, "c" * 64, "d" * 64
    product_manifest = [
        {"path": "product/__init__.py", "size_bytes": 0, "sha256": "1" * 64},
        {"path": "product/routes.py", "size_bytes": 100, "sha256": "2" * 64},
        {"path": "product/static/index.html", "size_bytes": 100, "sha256": "3" * 64},
    ]
    manifest = manifest_digest(product_manifest)
    spec = {
        "execution_verified": True,
        "artifact_capture": {"state": "stored", "artifact_id": ids["artifact"],
                             "sha256": sha, "manifest_digest": manifest,
                             "bucket": "selfhost-prod-build-artifacts-123456789012",
                             "key": "products/accepted.zip", "version_id": "version-fixture-1",
                             "size_bytes": 100, "storage_config_digest": "9" * 64,
                             "manifest": product_manifest},
        "lifecycle": {
            "review": {"status": "accepted", "decision": "accept",
                       "source_artifact_id": ids["artifact"], "report_digest": report},
            "completion": {"source_artifact_id": ids["artifact"],
                           "verified_execution": True, "verified_report": True},
        },
    }
    slug = "runtime-" + ids["app"][-8:]
    with d["factory"]() as session:
        session.add(FounderApp(id=ids["app"], workspace_id=d["wid"], venture_id=d["vid"],
            slug=slug, name="Runtime product", kind="service", state="draft",
            build_mode="supymem", deploy_target="supymem", created_at=now, updated_at=now))
        session.add(ExecutionPlan(id=ids["plan"], workspace_id=d["wid"], venture_id=d["vid"],
            version=1, state="approved", approved_at=now, approved_by=d["actor"], created_at=now))
        session.flush()
        session.add(PlanMilestone(id=ids["milestone"], plan_id=ids["plan"], workspace_id=d["wid"],
            venture_id=d["vid"], order=1, title="Useful product", kind="core", scope="conditional"))
        session.flush()
        session.add(PlanTask(id=ids["task"], milestone_id=ids["milestone"], workspace_id=d["wid"],
            venture_id=d["vid"], order=1, title="Build useful product", agent_role="mini_swe",
            tool_hints='["build.run"]', state="done", created_at=now))
        session.add(BuildRun(id=ids["build"], app_id=ids["app"], workspace_id=d["wid"],
            venture_id=d["vid"], plan_task_id=ids["task"], trigger="founder", agent="managed",
            state="succeeded", spec=json.dumps(spec), runtime_version=1, created_at=now,
            closed_at=now, finished_at=now))
        session.flush()
        session.add(BuildArtifact(id=ids["artifact"], build_run_id=ids["build"], app_id=ids["app"],
            workspace_id=d["wid"], kind="source_zip", s3_key="products/accepted.zip",
            sha256=sha, size_bytes=100, created_at=now))
        session.flush()
        session.add(QaRun(id=ids["qa"], app_id=ids["app"], build_run_id=ids["build"],
            workspace_id=d["wid"], venture_id=d["vid"], state="passed", suites='["product"]',
            passed=8, failed=0, unsupported=0, runtime_version=1, managed_build_id=ids["build"],
            generation=1, source_artifact_id=ids["artifact"], source_sha256=sha,
            source_manifest_digest=manifest, contract_digest=contract, input_digest="e" * 64,
            report_digest=report, created_at=now, finished_at=now))
        session.commit()
    return ids, manifest, contract, slug + ".supymem.com"


def test_runtime_intent_is_evidence_bound_idempotent_and_conflict_safe(journey_db):
    from app.provisioning import runtime_intents
    d = journey_db
    ids, manifest, contract, hostname = accepted_product(d)
    args = {"app_id": ids["app"], "workspace_id": d["wid"], "venture_id": d["vid"], "actor": d["actor"],
        "plan_task_id": ids["task"], "build_run_id": ids["build"],
        "source_artifact_id": ids["artifact"], "qa_run_id": ids["qa"],
        "source_manifest_digest": manifest, "qa_contract_digest": contract,
        "hostname": hostname}
    first = runtime_intents.admit(**args)
    assert first["generation"] == 1 and first["phase"] == "source_accepted"
    source = runtime_intents.controller_view(first["id"])["receipts"]["source"]
    assert source["version_id"] == "version-fixture-1" and source["sha256"] == "a" * 64
    assert runtime_intents.admit(**args) == first
    with pytest.raises(runtime_intents.RuntimeIntentError, match="intent_conflict"):
        runtime_intents.admit(**{**args, "hostname": "runtime-product-v2.supymem.com"})
    with pytest.raises(runtime_intents.RuntimeIntentError, match="source_not_accepted"):
        runtime_intents.admit(**{**args, "source_manifest_digest": "f" * 64})


def test_founder_runtime_status_is_owner_scoped_and_receipt_free(journey_db):
    from app.provisioning import runtime_intents

    d = journey_db
    ids, manifest, contract, hostname = accepted_product(d)
    assert runtime_intents.founder_status(app_id=ids["app"], actor=d["actor"]) is None
    admitted = runtime_intents.admit(
        app_id=ids["app"], workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"],
        plan_task_id=ids["task"], build_run_id=ids["build"],
        source_artifact_id=ids["artifact"], qa_run_id=ids["qa"],
        source_manifest_digest=manifest, qa_contract_digest=contract, hostname=hostname,
    )
    status = runtime_intents.founder_status(app_id=ids["app"], actor=d["actor"])
    assert status == {
        "id": admitted["id"], "build_run_id": ids["build"], "generation": 1,
        "state": "pending", "phase": "source_accepted", "preview_url": None,
    }
    assert "receipts" not in status and "request_digest" not in status
    with pytest.raises(KeyError, match="build_scope_not_found"):
        runtime_intents.founder_status(app_id=ids["app"], actor="per_unrelated")


def test_venture_status_projects_runtime_setup_then_private_review(journey_db):
    from app.db.supymem_models import AppRuntimeIntent
    from app.provisioning import runtime_intents
    from app.ventures.run_status import snapshot

    d = journey_db
    ids, manifest, contract, hostname = accepted_product(d)
    admitted = runtime_intents.admit(
        app_id=ids["app"], workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"],
        plan_task_id=ids["task"], build_run_id=ids["build"],
        source_artifact_id=ids["artifact"], qa_run_id=ids["qa"],
        source_manifest_digest=manifest, qa_contract_digest=contract, hostname=hostname,
    )
    with d["factory"]() as session:
        runtime = session.get(AppRuntimeIntent, admitted["id"])
        runtime.phase, runtime.state = "image_building", "provisioning"
        session.commit()
    view = snapshot(d["vid"], d["wid"])
    assert view["state"] == "working" and view["stage"] == "Application setup"
    assert "isolated application image" in view["activity"]
    assert view["action"] == {"label": "View application setup",
                              "href": f"/v/{d['vid']}/builds?app={ids['app']}&run={ids['build']}"}
    assert "private application preview" in view["next_step"]
    assert snapshot(d["vid"], "wsp_other")["state"] == "idle"
    with d["factory"]() as session:
        runtime = session.get(AppRuntimeIntent, admitted["id"])
        runtime.phase, runtime.state = "runtime_qa", "review"
        session.commit()
    view = snapshot(d["vid"], d["wid"])
    assert view["state"] == "needs_you" and view["stage"] == "Application QA"
    assert "account isolation are unverified" in view["activity"]
    assert view["action"]["href"] == f"/v/{d['vid']}/builds?app={ids['app']}&run={ids['build']}"
    assert snapshot(d["vid"], d["wid"], paused=True)["state"] == "paused"


def test_runtime_intent_rejects_static_landing_as_product_source(journey_db):
    from app.db.supymem_models import BuildRun
    from app.provisioning import runtime_intents
    from core.artifact_snapshot import manifest_digest

    d = journey_db
    ids, _, contract, hostname = accepted_product(d)
    landing_manifest = [{"path": "index.html", "size_bytes": 100, "sha256": "4" * 64}]
    landing_digest = manifest_digest(landing_manifest)
    with d["factory"]() as session:
        run = session.get(BuildRun, ids["build"])
        spec = json.loads(run.spec)
        spec["artifact_capture"]["manifest"] = landing_manifest
        spec["artifact_capture"]["manifest_digest"] = landing_digest
        run.spec = json.dumps(spec)
        session.commit()
    with pytest.raises(runtime_intents.RuntimeIntentError, match="source_not_accepted"):
        runtime_intents.admit(
            app_id=ids["app"], workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"],
            plan_task_id=ids["task"], build_run_id=ids["build"], source_artifact_id=ids["artifact"],
            qa_run_id=ids["qa"], source_manifest_digest=landing_digest,
            qa_contract_digest=contract, hostname=hostname)


def test_runtime_intent_rejects_founder_action_as_product_authority(journey_db):
    from app.db.supymem_models import PlanTask
    from app.provisioning import runtime_intents
    d = journey_db
    ids, manifest, contract, hostname = accepted_product(d)
    with d["factory"]() as session:
        session.get(PlanTask, ids["task"]).tool_hints = '["founder.action"]'
        session.commit()
    with pytest.raises(runtime_intents.RuntimeIntentError, match="task_not_ready"):
        runtime_intents.admit(app_id=ids["app"], workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"],
            plan_task_id=ids["task"], build_run_id=ids["build"], source_artifact_id=ids["artifact"],
            qa_run_id=ids["qa"], source_manifest_digest=manifest, qa_contract_digest=contract,
            hostname=hostname)


def test_runtime_intent_persists_database_receipts_without_credentials(journey_db):
    from app.provisioning import runtime_intents

    d = journey_db
    ids, manifest, contract, hostname = accepted_product(d)
    admitted = runtime_intents.admit(
        app_id=ids["app"], workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"],
        plan_task_id=ids["task"], build_run_id=ids["build"], source_artifact_id=ids["artifact"],
        qa_run_id=ids["qa"], source_manifest_digest=manifest, qa_contract_digest=contract,
        hostname=hostname)
    name = f"supymem/founder-apps/{ids['app']}/database-{admitted['request_digest'][:16]}"
    secret = {"secret_name": name,
              "secret_arn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:" + name + "-ABC123"}
    view = runtime_intents.record_database_secret(admitted["id"], secret)
    assert view["phase"] == "database_secret_created" and view["receipts"]["database_secret"] == secret
    assert runtime_intents.record_database_secret(admitted["id"], secret)["receipts"] == view["receipts"]

    launch = {
        "task_arn": "arn:aws:ecs:us-east-1:123456789012:task/selfhost-os-prod-apps/" + "a" * 32,
        "task_definition_arn": "arn:aws:ecs:us-east-1:123456789012:task-definition/"
                               "supymem-founder-db-" + "b" * 24 + ":1",
        "client_token": "c" * 64,
    }
    view = runtime_intents.record_database_launch(admitted["id"], launch)
    assert view["phase"] == "database_provisioning" and view["receipts"]["database_launch"] == launch
    observation = {"state": "STOPPED", "exit_code": 0, "succeeded": True,
                   "stopped_reason": "EssentialContainerExited"}
    view = runtime_intents.record_database_observation(admitted["id"], observation)
    assert view["state"] == "provisioning"
    assert view["receipts"]["database"] == {"status": "ready", "verified_by": "one_shot_task"}
    assert "password" not in json.dumps(view["receipts"]).lower()
    suffix = __import__("hashlib").sha256(ids["app"].encode()).hexdigest()[:24]
    pool = "us-east-1_ABC123fixture"
    auth = {
        "pool_id": pool,
        "pool_arn": "arn:aws:cognito-idp:us-east-1:123456789012:userpool/" + pool,
        "client_id": "a" * 26,
        "issuer": "https://cognito-idp.us-east-1.amazonaws.com/" + pool,
        "domain": f"https://supymem-app-{suffix}.auth.us-east-1.amazoncognito.com",
        "redirect_uri": "https://" + hostname + "/auth/callback",
        "app_origin": "https://" + hostname,
    }
    view = runtime_intents.record_auth(admitted["id"], auth)
    assert view["phase"] == "auth_provisioned" and view["receipts"]["auth"] == auth
    assert runtime_intents.record_auth(admitted["id"], auth)["receipts"] == view["receipts"]
    image_tag = f"app-{suffix}-g1-{admitted['request_digest'][:16]}"
    image_launch = {
        "build_id": "selfhost-os-prod-founder-image:12345678-1234-1234-1234-123456789abc",
        "idempotency_token": __import__("hashlib").sha256((admitted["id"] + ":image").encode()).hexdigest(),
        "image_tag": image_tag, "project_name": "selfhost-os-prod-founder-image",
    }
    view = runtime_intents.record_image_launch(admitted["id"], image_launch)
    assert view["phase"] == "image_building"
    pending = {"state": "IN_PROGRESS", "image_uri": None, "scan_status": None,
               "critical": None, "high": None}
    assert runtime_intents.record_image_observation(admitted["id"], pending)["phase"] == "image_building"
    safe = {"state": "SUCCEEDED",
            "image_uri": "123456789012.dkr.ecr.us-east-1.amazonaws.com/supymem-founder-apps@sha256:" + "f" * 64,
            "scan_status": "COMPLETE", "critical": 0, "high": 0}
    view = runtime_intents.record_image_observation(admitted["id"], safe)
    assert view["phase"] == "image_ready" and view["receipts"]["image"]["status"] == "ready"
    migration_launch = {
        "task_arn": "arn:aws:ecs:us-east-1:123456789012:task/selfhost-os-prod-apps/" + "d" * 32,
        "task_definition_arn": "arn:aws:ecs:us-east-1:123456789012:task-definition/"
                               "supymem-founder-migration-" + "e" * 24 + ":1",
        "client_token": __import__("hashlib").sha256(
            (admitted["id"] + ":migration").encode()).hexdigest(),
    }
    view = runtime_intents.record_migration_launch(admitted["id"], migration_launch)
    assert view["phase"] == "image_ready" and view["receipts"]["migration_launch"] == migration_launch
    view = runtime_intents.record_migration_observation(admitted["id"], observation)
    assert view["phase"] == "runtime_migrated"
    assert view["receipts"]["migration"] == {"status": "verified", "verified_by": "one_shot_task"}
    service = {
        "task_definition_arn": "arn:aws:ecs:us-east-1:123456789012:task-definition/"
                               f"supymem-founder-app-{suffix}:1",
        "target_group_arn": "arn:aws:elasticloadbalancing:us-east-1:123456789012:"
                            f"targetgroup/supa-{suffix}/" + "1" * 16,
        "listener_rule_arn": "arn:aws:elasticloadbalancing:us-east-1:123456789012:listener-rule/"
                             "app/selfhost-os-prod-apps/" + "2" * 16 + "/" + "3" * 16 + "/" + "4" * 16,
        "service_arn": "arn:aws:ecs:us-east-1:123456789012:service/selfhost-os-prod-apps/"
                       f"supymem-founder-app-{suffix}",
    }
    view = runtime_intents.record_service(admitted["id"], service)
    assert view["phase"] == "service_staged" and view["state"] == "staged"
    service_observation = {"state": "HEALTHY", "desired": 1, "running": 1, "pending": 0,
                           "target_states": ["healthy"], "healthy": True, "failed": False}
    view = runtime_intents.record_service_observation(admitted["id"], service_observation)
    assert view["receipts"]["service_observation"]["healthy"] is True
    dns = {"change_id": "/change/ABC123", "hostname": hostname}
    view = runtime_intents.record_dns_launch(admitted["id"], dns)
    assert view["phase"] == "preview_live"
    view = runtime_intents.record_dns_observation(
        admitted["id"], {"status": "INSYNC", "ready": True})
    assert view["phase"] == "runtime_qa" and view["state"] == "review"
    founder_view = runtime_intents.founder_status(app_id=ids["app"], actor=d["actor"])
    assert founder_view["preview_url"] == "https://" + hostname + "/"
    assert "receipts" not in founder_view


@pytest.mark.skipif(
    not (Path(__file__).resolve().parents[2] / "services/company_os/app/provisioning/run_database.py").is_file(),
    reason="Hosted database runner is not in the public local release",
)
def test_isolated_database_runner_uses_injected_values_without_exposing_password(monkeypatch):
    from tests.integration.test_postgres_auth_security import service_modules

    with service_modules("company_os"):
        from app.provisioning import postgres_apps
        from app.provisioning.run_database import run

        url = make_url(os.environ["TEST_POSTGRES_URL"])
        admin_params = {"host": url.host, "port": url.port or 5432, "user": url.username,
                        "password": url.password, "dbname": "postgres"}
        suffix = uuid4().hex[:12]
        admin_user, admin_password = "runtime_admin_" + suffix, secrets.token_urlsafe(40)
        app_id, app_password = "app_" + uuid4().hex, secrets.token_urlsafe(40)
        with psycopg.connect(**admin_params, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE ROLE {} LOGIN CREATEDB CREATEROLE PASSWORD {}").format(
                sql.Identifier(admin_user), sql.Literal(admin_password)))
        monkeypatch.setenv("APP_ENV", "test")
        monkeypatch.setenv("APP_ID", app_id)
        monkeypatch.setenv("WORKSPACE_ID", "wsp_runtime_runner")
        monkeypatch.setenv("FOUNDER_DATABASE_HOST", url.host)
        monkeypatch.setenv("FOUNDER_DATABASE_PORT", str(url.port or 5432))
        monkeypatch.setenv("FOUNDER_DATABASE_ADMIN_USERNAME", admin_user)
        monkeypatch.setenv("FOUNDER_DATABASE_ADMIN_PASSWORD", admin_password)
        monkeypatch.setenv("APP_DATABASE_PASSWORD", app_password)
        monkeypatch.setenv("FOUNDER_DATABASE_ALLOW_LOOPBACK", "1")
        database, role = postgres_apps.names(app_id)
        try:
            receipt = run()
            assert receipt == {"app_id": app_id, "workspace_id": "wsp_runtime_runner",
                               "database": database, "role": role, "connection_limit": 20}
            assert admin_password not in json.dumps(receipt) and app_password not in json.dumps(receipt)
        finally:
            with psycopg.connect(**admin_params, autocommit=True) as admin:
                admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database)))
                admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
                admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(admin_user)))

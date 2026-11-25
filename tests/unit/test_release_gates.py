"""Release gates must reject unknown/missing evidence without changing services."""

from __future__ import annotations

import json
import subprocess

import pytest

from tests.company_os_harness import ROOT

if not (ROOT / "scripts/infra/deploy_services.py").is_file():
    pytest.skip("Hosted deployment tooling is not in the public local release.", allow_module_level=True)

from scripts.infra import deploy_services as deploy
from scripts.smoke import check_payload


# Synthetic CLI transport edges; no AWS request is made by these tests.
UNKNOWN_MUTATION_RESULTS = [
    (255, ""), (255, "private unknown CLI failure"),
    (255, "Connection reset by peer: private"),
    (255, "Connection was closed before we received a valid response from endpoint URL: private"),
    (130, "private interrupted"), (-15, "private terminated"), (-9, ""),
    (1, "private disconnected"), (254, "private unrecognized service response"),
    (255, "An error occurred (AccessDeniedException) when calling the UpdateService operation: private"),
]


def release_plan():
    return {"env": "production", "cluster": "cluster", "services": [
        {"kind": name, "name": name, "previous": f"{name}:old",
         "candidate": {"family": name, "containerDefinitions": [{"name": "app", "image": "image@sha256:abc"}]},
         "network": {"awsvpcConfiguration": {"subnets": ["private"]}}, "platformVersion": "LATEST"}
        for name in ["core-api", "gateway", "worker-general", "warden"]
    ]}


def previous_image_read(operation, payload):
    """Existing rollback fixtures supply real pinned-image read shapes."""
    if operation == "describe-task-definition":
        kind = payload["taskDefinition"].split(":")[0]
        repo = {"core-api": "core", "worker-general": "core"}.get(kind, kind)
        return {"taskDefinition": {"containerDefinitions": [
            {"image": f"{deploy.REGISTRY}/selfhost-os/{repo}@sha256:" + "e" * 64}]}}
    if operation == "describe-images":
        assert payload["imageIds"] == [{"imageDigest": "sha256:" + "e" * 64}]
        return {"imageDetails": [{"imageDigest": "sha256:" + "e" * 64}]}
    return None


@pytest.mark.parametrize("fault", ["missing", "unreadable", "wrong_digest", "malformed_image"])
def test_previous_image_guard_refuses_before_migration_or_service_changes(monkeypatch, fault):
    calls = []
    plan = release_plan()
    monkeypatch.setattr(deploy, "migrate", lambda _: pytest.fail("Unverified rollback baseline reached migration"))

    def fake_aws(service_name, operation, **payload):
        calls.append((service_name, operation))
        if operation == "describe-task-definition" and fault == "malformed_image":
            return {"taskDefinition": {"containerDefinitions": [{"image": "private.invalid/core:latest"}]}}
        if operation == "describe-images" and payload["repositoryName"] == "selfhost-os/gateway":
            if fault == "unreadable":
                raise deploy.ReleaseError("ECR read unavailable")
            return {"imageDetails": [] if fault == "missing" else [{"imageDigest": "sha256:" + "f" * 64}]}
        value = previous_image_read(operation, payload)
        assert value is not None, "Guard reached mutation: " + operation
        return value

    monkeypatch.setattr(deploy, "aws", fake_aws)
    with pytest.raises(deploy.ReleasePending, match="Previous.*images"):
        deploy.apply_release(plan)
    assert all(operation in {"describe-task-definition", "describe-images"} for _, operation in calls)
    if fault != "malformed_image":
        assert calls.count(("ecs", "describe-task-definition")) == 2


@pytest.mark.parametrize("unreadable", [False, True])
def test_image_loss_before_rollback_keeps_all_current_revisions(monkeypatch, unreadable):
    plan, updates = release_plan(), []
    failed = False
    original_error = deploy.ReleaseError("local smoke unavailable")
    monkeypatch.setattr(deploy, "migrate", lambda _: "migration:success")
    monkeypatch.setattr(deploy, "wait", lambda *_args, **_kwargs: None)

    def bad_smoke(_):
        nonlocal failed
        failed = True
        raise original_error

    def fake_aws(service_name, operation, **payload):
        if operation == "describe-images" and failed and payload["repositoryName"] == "selfhost-os/gateway":
            if unreadable:
                raise deploy.ReleaseError("ECR read unavailable")
            return {"imageDetails": []}
        value = previous_image_read(operation, payload)
        if value is not None:
            return value
        if operation == "register-task-definition":
            return {"taskDefinition": {"taskDefinitionArn": payload["family"] + ":new"}}
        if operation == "update-service":
            updates.append((payload["service"], payload["taskDefinition"]))
            return {}
        if operation == "describe-services":
            return {"services": [{"serviceName": row["name"], "taskDefinition": row["name"] + ":new",
                "runningCount": 1, "desiredCount": 1} for row in plan["services"]]}
        pytest.fail(operation)

    monkeypatch.setattr(deploy, "aws", fake_aws)
    monkeypatch.setattr(deploy, "smoke", bad_smoke)
    with pytest.raises(deploy.ReleasePending, match="rollback") as caught:
        deploy.apply_release(plan)
    assert caught.value.__cause__ is original_error
    assert updates == [(row["name"], row["name"] + ":new") for row in plan["services"]]


def test_staging_requires_a_frozen_release_tag_before_aws(monkeypatch):
    monkeypatch.setattr(deploy, "aws", lambda *a, **kw: pytest.fail("Must validate tag first"))
    with pytest.raises(deploy.ReleaseError, match="staging-<40 hex"):
        deploy.plan_release("staging", "e34e61df", frozen_staging_images=True)


def test_production_preserve_warden_excludes_warden():
    assert deploy.release_repositories("production", "a" * 40, False, preserve_warden=True) == {
        "gateway": "gateway", "core-api": "core", "worker-general": "core",
    }


def test_production_service_selection_is_unchanged():
    assert deploy.release_repositories("production", "sha-" + "a" * 40, False) == {
        "gateway": "gateway", "core-api": "core", "worker-general": "core", "warden": "warden",
    }


def test_preserve_warden_is_refused_for_frozen_staging():
    with pytest.raises(deploy.ReleaseError, match="production-only"):
        deploy.release_repositories("staging", "staging-" + "a" * 40, True, preserve_warden=True)


@pytest.mark.parametrize("env,frozen", [("production", True), ("staging", False)])
def test_freeze_verification_rejected_outside_supported_release_paths(monkeypatch, env, frozen):
    monkeypatch.setattr(deploy, "aws", lambda *a, **kw: pytest.fail("AWS must not be called"))
    tag = ("staging-" + "a" * 40) if frozen else "a" * 40
    with pytest.raises(deploy.ReleaseError, match="requires frozen staging or production"):
        deploy.plan_release(env, tag, frozen_staging_images=frozen, verify_freeze_migration=True)



def test_ordinary_staging_release_selection_is_unchanged():
    assert deploy.release_repositories("staging", "sha-" + "a" * 40, False) == {
        "gateway": "gateway", "core-api": "core", "worker-general": "core", "warden": "warden",
    }


def test_frozen_staging_selector_is_refused_in_production():
    with pytest.raises(deploy.ReleaseError, match="staging-only"):
        deploy.release_repositories("production", "staging-" + "a" * 40, True)


def test_staging_missing_image_stops_before_service_mutation(monkeypatch):
    calls = []

    def fake_aws(service_name, operation, **payload):
        calls.append((service_name, operation, payload))
        if operation == "list-tasks":
            return {"taskArns": []}
        if operation == "describe-services":
            return {"services": [{"serviceName": name} for name in payload["services"]]}
        if operation == "describe-images":
            return {"imageDetails": []}
        pytest.fail(operation)

    monkeypatch.setattr(deploy, "aws", fake_aws)
    with pytest.raises(deploy.ReleaseError, match="No immutable digest"):
        deploy.plan_release("staging", "staging-" + "a" * 40, frozen_staging_images=True)
    assert {operation for _, operation, _ in calls} <= {"list-tasks", "describe-services", "describe-images"}


def test_staging_plan_selects_core_worker_console_gateway_and_retains_warden(monkeypatch):
    calls = []
    tag = "staging-" + "b" * 40

    def fake_aws(service_name, operation, **payload):
        calls.append((operation, payload))
        if operation == "list-tasks":
            return {"taskArns": []}
        if operation == "describe-services":
            return {"services": [{
                "serviceName": name, "status": "ACTIVE", "desiredCount": 1,
                "deployments": [], "taskDefinition": f"{name}:9",
                "networkConfiguration": {"awsvpcConfiguration": {"subnets": ["private"]}},
            } for name in payload["services"]]}
        if operation == "describe-images":
            return {"imageDetails": [{"imageDigest": "sha256:" + "c" * 64}]}
        if operation == "describe-task-definition":
            name = payload["taskDefinition"]
            console = name.endswith("-console:9")
            return {"taskDefinition": {
                "family": name.rsplit(":", 1)[0],
                "containerDefinitions": [{"name": "app", "image": "prior", "environment": [
                    {"name": "APP_ENV", "value": "staging"},
                    {"name": "NEXT_PUBLIC_GATEWAY_URL", "value": "https://gateway.staging.supymem.com"},
                ], "secrets": [] if console else [
                    {"name": "COS_INTERNAL_ASSERTION_SECRET", "valueFrom": "arn:staging"},
                    {"name": "DATABASE_URL", "valueFrom": "arn:staging"},
                    {"name": "LLM_API_KEY", "valueFrom": "arn:staging"},
                    {"name": "LLM_BASE_URL", "valueFrom": "arn:staging"},
                    {"name": "SESSION_SECRET", "valueFrom": "arn:session"},
                    {"name": "GATEWAY_USERS_DB", "valueFrom": "arn:users"},
                    {"name": "CONSOLE_ORIGIN", "valueFrom": "arn:origin"},
                ]}],
            }}
        pytest.fail(operation)

    monkeypatch.setattr(deploy, "aws", fake_aws)
    plan = deploy.plan_release("staging", tag, frozen_staging_images=True)
    assert [row["kind"] for row in plan["services"]] == ["core-api", "worker-general", "console", "gateway"]
    image_repositories = {payload["repositoryName"] for operation, payload in calls if operation == "describe-images"}
    service_names = next(payload["services"] for operation, payload in calls if operation == "describe-services")
    assert image_repositories == {"selfhost-os/core", "selfhost-os/console", "selfhost-os/gateway"}
    assert service_names == ["selfhost-os-staging-core-api", "selfhost-os-staging-worker-general", "selfhost-os-staging-console", "selfhost-os-staging-gateway"]


def test_migration_runs_before_selected_staging_service_updates(monkeypatch):
    plan = {"env": "staging", "cluster": "cluster", "services": [
        {"kind": name, "name": name, "previous": f"{name}:old",
         "candidate": {"family": name, "containerDefinitions": [{"name": "app", "image": "image@sha256:abc"}]},
         "network": {"awsvpcConfiguration": {"subnets": ["private"]}}, "platformVersion": "LATEST"}
        for name in ["core-api", "worker-general", "console", "gateway"]
    ]}
    order = []
    monkeypatch.setattr(deploy, "migrate", lambda _: order.append("migration") or "migration:success")
    monkeypatch.setattr(deploy, "wait", lambda *a, **kw: None)
    monkeypatch.setattr(deploy, "smoke", lambda env: None)

    def fake_aws(service_name, operation, **payload):
        prior = previous_image_read(operation, payload)
        if prior is not None:
            return prior
        if operation == "register-task-definition":
            order.append("register")
            return {"taskDefinition": {"taskDefinitionArn": payload["family"] + ":new"}}
        if operation == "update-service":
            order.append("update")
            return {}
        if operation == "describe-services":
            return {"services": [{"serviceName": row["name"], "taskDefinition": row["name"] + ":new", "runningCount": 1, "desiredCount": 1} for row in plan["services"]]}
        pytest.fail(operation)

    monkeypatch.setattr(deploy, "aws", fake_aws)
    deploy.apply_release(plan)
    assert order[0] == "migration"
    assert order.count("update") == 4


def test_failed_migration_never_updates_a_service(monkeypatch):
    calls = []

    def fake_aws(service_name, operation, **payload):
        prior = previous_image_read(operation, payload)
        if prior is not None:
            return prior
        calls.append(operation)
        if operation == "register-task-definition":
            return {"taskDefinition": {"taskDefinitionArn": "migration:2"}}
        if operation == "run-task":
            return {"tasks": [{"taskArn": "task:migration"}]}
        if operation == "describe-tasks":
            return {"tasks": [{"lastStatus": "STOPPED", "containers": [{"exitCode": 1}]}]}
        raise AssertionError(operation)

    monkeypatch.setattr(deploy, "aws", fake_aws)
    monkeypatch.setattr(deploy, "wait", lambda *a, **kw: None)
    with pytest.raises(deploy.ReleaseError, match="Migration failed"):
        deploy.apply_release(release_plan())
    assert "update-service" not in calls


def test_migration_timeout_keeps_the_existing_task_authoritative(monkeypatch):
    def fake_aws(service_name, operation, **payload):
        prior = previous_image_read(operation, payload)
        if prior is not None:
            return prior
        if operation == "register-task-definition":
            return {"taskDefinition": {"taskDefinitionArn": "migration:2"}}
        if operation == "run-task":
            return {"tasks": [{"taskArn": "task:still-running"}]}
        raise AssertionError(operation)

    def timeout(*args, **kwargs):
        raise deploy.ReleaseError("observation timeout")

    monkeypatch.setattr(deploy, "aws", fake_aws)
    monkeypatch.setattr(deploy, "wait", timeout)
    with pytest.raises(deploy.ReleasePending, match="task:still-running"):
        deploy.apply_release(release_plan())


def test_failed_release_restores_previous_revisions(monkeypatch):
    updates = []
    plan = release_plan()
    monkeypatch.setattr(deploy, "migrate", lambda p: "migration:success")
    monkeypatch.setattr(deploy, "wait", lambda *a, **kw: None)

    def fake_aws(service_name, operation, **payload):
        prior = previous_image_read(operation, payload)
        if prior is not None:
            return prior
        if operation == "register-task-definition":
            return {"taskDefinition": {"taskDefinitionArn": payload["family"] + ":new"}}
        if operation == "update-service":
            updates.append((payload["service"], payload["taskDefinition"]))
            return {}
        if operation == "describe-services":
            return {"services": [{"serviceName": row["name"], "taskDefinition": row["name"] + ":new", "runningCount": 1, "desiredCount": 1} for row in plan["services"]]}
        raise AssertionError(operation)

    def bad_smoke(env):
        assert env == "production"
        raise deploy.ReleaseError("authenticated smoke failed")

    monkeypatch.setattr(deploy, "aws", fake_aws)
    monkeypatch.setattr(deploy, "smoke", bad_smoke)
    with pytest.raises(deploy.ReleaseError, match="Previous service revisions restored"):
        deploy.apply_release(plan)
    assert updates[-4:] == [(row["name"], row["previous"]) for row in reversed(plan["services"])]


def test_in_progress_deployment_is_not_restarted_after_wait_timeout(monkeypatch):
    plan = release_plan()
    updates = []
    monkeypatch.setattr(deploy, "migrate", lambda p: "migration:success")

    def timeout(*args, **kwargs):
        raise deploy.ReleaseError("observation timeout")

    def fake_aws(service_name, operation, **payload):
        prior = previous_image_read(operation, payload)
        if prior is not None:
            return prior
        if operation == "register-task-definition":
            return {"taskDefinition": {"taskDefinitionArn": payload["family"] + ":new"}}
        if operation == "update-service":
            updates.append(payload["taskDefinition"])
            return {}
        if operation == "describe-services":
            return {"services": [{"serviceName": row["name"], "deployments": [{"rolloutState": "IN_PROGRESS"}]} for row in plan["services"]]}
        raise AssertionError(operation)

    monkeypatch.setattr(deploy, "wait", timeout)
    monkeypatch.setattr(deploy, "aws", fake_aws)
    with pytest.raises(deploy.ReleasePending):
        deploy.apply_release(plan)
    assert len(updates) == 4
    assert all(item.endswith(":new") for item in updates)


@pytest.mark.parametrize("tag", ["main", "latest", "", "abc; echo exploit"])
def test_mutable_or_invalid_image_tag_is_refused_before_aws(monkeypatch, tag):
    monkeypatch.setattr(deploy, "aws", lambda *a, **kw: pytest.fail("Must validate tag first"))
    with pytest.raises(deploy.ReleaseError):
        deploy.plan_release("production", tag)


@pytest.mark.parametrize("read_error", [True, False])
def test_unavailable_deployment_status_does_not_roll_back(monkeypatch, read_error):
    updates = []
    monkeypatch.setattr(deploy, "migrate", lambda p: "migration:success")
    monkeypatch.setattr(deploy, "wait", lambda *a, **kw: None)

    def fake_aws(service_name, operation, **payload):
        prior = previous_image_read(operation, payload)
        if prior is not None:
            return prior
        if operation == "register-task-definition":
            return {"taskDefinition": {"taskDefinitionArn": payload["family"] + ":new"}}
        if operation == "update-service":
            updates.append(payload["taskDefinition"])
            return {}
        if operation == "describe-services":
            if read_error:
                raise deploy.ReleaseError("AWS status connection unavailable")
            return {"services": [], "failures": [{"reason": "MISSING"}]}
        raise AssertionError(operation)

    monkeypatch.setattr(deploy, "aws", fake_aws)
    with pytest.raises(deploy.ReleasePending):
        deploy.apply_release(release_plan())
    assert len(updates) == 4
    assert all(item.endswith(":new") for item in updates)


def test_task_definition_preserves_secret_refs_and_limits():
    original = {"family": "core", "revision": 19, "taskDefinitionArn": "old", "cpu": "512",
                "containerDefinitions": [{"name": "app", "image": "old", "secrets": [{"name": "DB", "valueFrom": "arn:secret"}]}]}
    candidate = deploy.candidate_task(original, "new@digest")
    assert candidate["cpu"] == "512"
    assert candidate["containerDefinitions"][0]["secrets"] == original["containerDefinitions"][0]["secrets"]
    assert "revision" not in candidate
    assert original["containerDefinitions"][0]["image"] == "old"


def test_smoke_rejects_200_from_wrong_service_or_environment():
    assert not check_payload("ai-proxy", 200, '{"service":"gateway","status":"ok"}')
    assert not check_payload("gateway", 200, "<html>login page</html>")
    ready = {"service": "gateway", "ready": True, "app_env": "production",
             "checks": {"users_db": {"ok": True}, "company_os": {"ok": True}}}
    assert check_payload("gateway-readiness", 200, json.dumps(ready), "production")
    assert not check_payload("gateway-readiness", 200, json.dumps(ready), "staging")
    ready["checks"]["company_os"]["ok"] = False
    assert not check_payload("gateway-readiness", 200, json.dumps(ready), "production")


@pytest.mark.parametrize("found", [[], [{"imageDigest": ""}], [{"imageDigest": "sha256:invalid"}]])
def test_missing_gateway_digest_rejects_before_task_registration_or_updates(monkeypatch, found):
    calls = []
    def fake_aws(service_name, operation, **payload):
        calls.append(operation)
        if operation == "list-tasks":
            return {"taskArns": []}
        if operation == "describe-services":
            return {"services": [{"serviceName": name} for name in payload["services"]]}
        if operation == "describe-images":
            return {"imageDetails": found if payload["repositoryName"] == "selfhost-os/gateway" else
                    [{"imageDigest": "sha256:" + "d" * 64}]}
        pytest.fail("Missing Gateway reached " + operation)
    monkeypatch.setattr(deploy, "aws", fake_aws)
    with pytest.raises(deploy.ReleaseError, match="No immutable digest for gateway"):
        deploy.plan_release("staging", "staging-" + "a" * 40, frozen_staging_images=True)
    assert set(calls) <= {"list-tasks", "describe-services", "describe-images"}


def test_failed_frozen_release_restores_gateway_and_other_previous_definitions(monkeypatch):
    plan = release_plan()
    plan["env"] = "staging"
    plan["services"] = [row for row in plan["services"] if row["kind"] != "warden"]
    core = plan["services"][0]
    import copy
    console = copy.deepcopy(core)
    console.update(kind="console", name="console", previous="console:old")
    console["candidate"]["family"] = "console"
    plan["services"].append(console)
    updates = []
    monkeypatch.setattr(deploy, "migrate", lambda _: "migration:success")
    monkeypatch.setattr(deploy, "wait", lambda *args, **kwargs: None)
    def failed_smoke(_):
        raise deploy.ReleaseError("gateway readiness failed")
    monkeypatch.setattr(deploy, "smoke", failed_smoke)
    def fake_aws(service_name, operation, **payload):
        prior = previous_image_read(operation, payload)
        if prior is not None:
            return prior
        if operation == "register-task-definition":
            return {"taskDefinition": {"taskDefinitionArn": payload["family"] + ":new"}}
        if operation == "update-service":
            updates.append((payload["service"], payload["taskDefinition"]))
            return {}
        if operation == "describe-services":
            return {"services": [{"serviceName": row["name"], "taskDefinition": row["name"] + ":new",
                                   "runningCount": 1, "desiredCount": 1} for row in plan["services"]]}
        pytest.fail(operation)
    monkeypatch.setattr(deploy, "aws", fake_aws)
    with pytest.raises(deploy.ReleaseError, match="Previous service revisions restored"):
        deploy.apply_release(plan)
    assert updates[-4:] == [(row["name"], row["previous"]) for row in reversed(plan["services"])]
    assert ("gateway", "gateway:old") in updates and not any(name == "warden" for name, _ in updates)


@pytest.mark.parametrize("operation,pending", [
    ("describe-services", False), ("run-task", True),
    ("update-service", True), ("register-task-definition", True),
    ("unreviewed-operation", True),
])
def test_aws_process_timeout_is_bounded_and_never_exposes_inputs(monkeypatch, operation, pending):
    calls = []

    def timeout(command, **kwargs):
        calls.append((command, kwargs))
        raise subprocess.TimeoutExpired(command, kwargs["timeout"], output="private-output", stderr="private-stderr")

    monkeypatch.setattr(deploy.subprocess, "run", timeout)
    expected = deploy.ReleasePending if pending else deploy.ReleaseError
    with pytest.raises(expected) as caught:
        deploy.aws("ecs", operation, token="private-input")
    assert isinstance(caught.value, deploy.ReleasePending) is pending
    assert len(calls) == 1 and calls[0][1]["timeout"] == 120
    assert caught.value.__suppress_context__
    assert "private" not in str(caught.value) and "--cli-input-json" not in str(caught.value)


@pytest.mark.parametrize("operation", ["register-task-definition", "run-task", "update-service"])
@pytest.mark.parametrize("stderr", [
    "Read timeout on endpoint URL: private", "Connect timeout on endpoint URL: private",
    "private RequestTimeout details",
    "Connection was closed before we received a valid response from endpoint URL: private",
])
def test_cli_transport_failure_does_not_imply_mutation_rejection(monkeypatch, operation, stderr):
    monkeypatch.setattr(deploy.subprocess, "run", lambda *a, **kw:
                        subprocess.CompletedProcess(a[0], 255, "", stderr))
    with pytest.raises(deploy.ReleasePending) as caught:
        deploy.aws("ecs", operation)
    assert "private" not in str(caught.value)


@pytest.mark.parametrize("operation", ["register-task-definition", "run-task", "update-service"])
@pytest.mark.parametrize("returncode,stderr", UNKNOWN_MUTATION_RESULTS)
def test_unknown_cli_mutation_result_is_pending_without_retry(monkeypatch, operation, returncode, stderr):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, returncode, "", stderr)

    monkeypatch.setattr(deploy.subprocess, "run", run)
    with pytest.raises(deploy.ReleasePending, match="acknowledgement unresolved") as caught:
        deploy.aws("ecs", operation, token="private-input")
    assert len(calls) == 1 and "private" not in str(caught.value)


@pytest.mark.parametrize("operation,returncode,stderr", [
    ("describe-services", 255, "Connection was closed before we received a valid response from endpoint URL: private"),
    ("update-service", 253, "Unable to locate credentials: private"),
    ("update-service", 2, "Unknown options: private"),
    ("update-service", 252, "Parameter validation failed: private"),
    ("update-service", 253, "The config profile (ConnectionClosedError) could not be found: private"),
    ("update-service", 254, "An error occurred (InvalidParameterException) when calling the UpdateService operation: private ConnectionClosedError"),
    ("run-task", 254, "An error occurred (AccessDeniedException) when calling the RunTask operation: private"),
])
def test_cli_read_and_definite_rejections_remain_errors(monkeypatch, operation, returncode, stderr):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, returncode, "", stderr)

    monkeypatch.setattr(deploy.subprocess, "run", run)
    with pytest.raises(deploy.ReleaseError, match="request-failed") as caught:
        deploy.aws("ecs", operation, token="private-input")
    assert not isinstance(caught.value, deploy.ReleasePending)
    assert len(calls) == 1 and "private" not in str(caught.value)


@pytest.mark.parametrize("operation,ack_operation,code", [
    (operation, ack_operation, code)
    for operation, ack_operation, codes in [
        ("register-task-definition", "RegisterTaskDefinition", ["InvalidParameterException", "AccessDeniedException"]),
        ("run-task", "RunTask", ["InvalidParameterException", "AccessDeniedException", "ClusterNotFoundException"]),
        ("update-service", "UpdateService", ["InvalidParameterException", "AccessDeniedException", "ClusterNotFoundException", "ServiceNotFoundException"]),
    ] for code in codes
])
def test_operation_bound_service_rejection_remains_definite(monkeypatch, operation, ack_operation, code):
    stderr = f"An error occurred ({code}) when calling the {ack_operation} operation: private rejection"
    monkeypatch.setattr(deploy.subprocess, "run", lambda *a, **kw:
                        subprocess.CompletedProcess(a[0], 254, "", stderr))
    with pytest.raises(deploy.ReleaseError) as caught:
        deploy.aws("ecs", operation)
    assert not isinstance(caught.value, deploy.ReleasePending) and "private" not in str(caught.value)


@pytest.mark.parametrize("service,operation,returncode,stdout,stderr", [
    ("ecs", "update-service", 254, "", "An error occurred (ServerException) when calling the UpdateService operation: private"),
    ("ecs", "update-service", 254, "", "An error occurred (RequestTimeout) when calling the UpdateService operation: private"),
    ("ecs", "register-task-definition", 254, "", "An error occurred (ClusterNotFoundException) when calling the RegisterTaskDefinition operation: private"),
    ("ecs", "run-task", 254, "", "An error occurred (ServiceNotFoundException) when calling the RunTask operation: private"),
    ("ecs", "update-service", 254, "", "An error occurred (InvalidParameterException) when calling the RunTask operation: private"),
    ("ecs", "unreviewed-operation", 254, "", "An error occurred (InvalidParameterException) when calling the UpdateService operation: private"),
    ("unreviewed-service", "update-service", 254, "", "An error occurred (InvalidParameterException) when calling the UpdateService operation: private"),
    ("ecs", "update-service", 254, "private-output", "An error occurred (InvalidParameterException) when calling the UpdateService operation: private"),
    ("ecs", "update-service", 254, "", "private transport detail\nAn error occurred (InvalidParameterException) when calling the UpdateService operation: private"),
    ("ecs", "update-service", 254, "", "An error occurred (InvalidParameterException) when calling the UpdateService operation:"),
    ("s3", "unreviewed-operation", 2, "", "private skipped transfer"),
])
def test_ambiguous_service_or_cli_output_cannot_authorize_recovery(monkeypatch, service, operation, returncode, stdout, stderr):
    monkeypatch.setattr(deploy.subprocess, "run", lambda *a, **kw:
                        subprocess.CompletedProcess(a[0], returncode, stdout, stderr))
    with pytest.raises(deploy.ReleasePending) as caught:
        deploy.aws(service, operation)
    assert "private" not in str(caught.value)


@pytest.mark.parametrize("operation,reply", [
    ("run-task", "private-invalid-json"), ("run-task", ""), ("run-task", "null"),
    ("run-task", "[]"), ("run-task", '{}'), ("run-task", '{"tasks":null}'),
    ("run-task", '{"tasks":[{}]}'), ("run-task", '{"failures":[null]}'),
    ("run-task", '{"tasks":[]}'), ("run-task", '{"failures":[]}'),
    ("run-task", '{"tasks":[],"failures":[]}'), ("run-task", '{"tasks":[],"failures":[{}]}'),
    ("run-task", '{"failures":[{"reason":""}]}'),
    ("register-task-definition", '{"taskDefinition":{}}'),
    ("register-task-definition", '{"taskDefinition":{"taskDefinitionArn":null}}'),
    ("update-service", '{"service":{}}'),
])
def test_mutation_lost_or_malformed_ack_is_pending_without_retry(monkeypatch, operation, reply):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, reply, "private-stderr")

    monkeypatch.setattr(deploy.subprocess, "run", run)
    with pytest.raises(deploy.ReleasePending) as caught:
        deploy.aws("ecs", operation, token="private-input")
    assert len(calls) == 1 and "private" not in str(caught.value)


def test_readonly_empty_and_unreadable_replies_do_not_become_mutation_pending(monkeypatch):
    reply = ""
    monkeypatch.setattr(deploy.subprocess, "run", lambda *a, **kw:
                        subprocess.CompletedProcess(a[0], 0, reply, ""))
    assert deploy.aws("ecs", "describe-services") == {}
    reply = "private-invalid-json"
    with pytest.raises(deploy.ReleaseError) as caught:
        deploy.aws("ecs", "describe-services")
    assert not isinstance(caught.value, deploy.ReleasePending) and "private" not in str(caught.value)


def test_confirmed_capacity_refusal_json_keeps_existing_subnet_fallback(monkeypatch):
    plan = release_plan()
    plan["services"][0]["network"]["awsvpcConfiguration"]["subnets"] = ["first", "second"]
    calls = []

    def run(command, **kwargs):
        operation = command[2]
        payload = json.loads(command[-1])
        if operation == "register-task-definition":
            response = {"taskDefinition": {"taskDefinitionArn": "migration:1"}}
        elif operation == "run-task":
            calls.append(payload["networkConfiguration"]["awsvpcConfiguration"]["subnets"])
            response = ({"tasks": [], "failures": [{"reason": "Capacity is unavailable at this time"}]}
                        if len(calls) == 1 else {"tasks": [{"taskArn": "task:1"}]})
        elif operation == "describe-tasks":
            response = {"tasks": [{"lastStatus": "STOPPED", "containers": [{"exitCode": 0}]}]}
        else:
            pytest.fail(operation)
        return subprocess.CompletedProcess(command, 0, json.dumps(response), "")

    monkeypatch.setattr(deploy.subprocess, "run", run)
    monkeypatch.setattr(deploy, "wait", lambda *a, **kw: None)
    assert deploy.migrate(plan) == "task:1"
    assert calls == [["first"], ["second"]]


@pytest.mark.parametrize("stderr,marker", [
    ("private ResourceNotFoundException details", "ResourceNotFoundException"),
    ("private arbitrary AWS diagnostics", "request-failed"),
])
def test_aws_failure_keeps_only_closed_optional_secret_marker(monkeypatch, stderr, marker):
    monkeypatch.setattr(deploy.subprocess, "run", lambda *a, **kw:
                        subprocess.CompletedProcess(a[0], 1, "private-output", stderr))
    with pytest.raises(deploy.ReleaseError, match=marker) as caught:
        deploy.aws("secretsmanager", "describe-secret", SecretId="private-reference")
    assert "private" not in str(caught.value)


@pytest.mark.parametrize("timeout", [True, False])
def test_ecs_observation_bound_emits_only_closed_flushed_phases(monkeypatch, timeout):
    printed = []
    monkeypatch.setattr("builtins.print", lambda value, **kwargs: printed.append((json.loads(value), kwargs)))

    def run(command, **kwargs):
        assert kwargs["timeout"] == 660
        if timeout:
            raise subprocess.TimeoutExpired(command, 660, stderr="private-stderr")
        return subprocess.CompletedProcess(command, 1, "", "private-stderr")

    monkeypatch.setattr(deploy.subprocess, "run", run)
    with pytest.raises(deploy.ReleaseError) as caught:
        deploy.wait("services-stable", cluster="private-cluster", services=["private-service"])
    assert "private" not in str(caught.value)
    assert printed == [({"phase": "service-stabilization", "outcome": outcome}, {"flush": True})
                       for outcome in ["started", "timed-out" if timeout else "unresolved"]]


@pytest.mark.parametrize("reply", [*UNKNOWN_MUTATION_RESULTS, None, "connection-closed", '{"tasks":[]}', '{"tasks":[],"failures":[]}', '{"failures":[{}]}'])
def test_migration_launch_unknown_ack_never_retries_another_subnet(monkeypatch, reply):
    plan = release_plan()
    plan["services"][0]["network"]["awsvpcConfiguration"]["subnets"] = ["first", "second"]
    real_aws, launches = deploy.aws, []

    def dispatch(service_name, operation, **payload):
        if operation == "register-task-definition":
            return {"taskDefinition": {"taskDefinitionArn": "migration:1"}}
        assert operation == "run-task"
        launches.append(payload["networkConfiguration"])
        return real_aws(service_name, operation, **payload)

    monkeypatch.setattr(deploy, "aws", dispatch)
    def run(command, **kwargs):
        if reply is None:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        if isinstance(reply, tuple):
            return subprocess.CompletedProcess(command, reply[0], "", reply[1])
        if reply == "connection-closed":
            return subprocess.CompletedProcess(command, 255, "", "Connection was closed before we received a valid response from endpoint URL: private")
        return subprocess.CompletedProcess(command, 0, reply, "")

    monkeypatch.setattr(deploy.subprocess, "run", run)
    with pytest.raises(deploy.ReleasePending):
        deploy.migrate(plan)
    assert len(launches) == 1 and launches[0]["awsvpcConfiguration"]["subnets"] == ["first"]


@pytest.mark.parametrize("failure", ["process-timeout", "cli-timeout", "null", "[]"])
def test_migration_result_unknown_read_retains_existing_task_custody(monkeypatch, failure):
    real_aws, launches = deploy.aws, []

    def dispatch(service_name, operation, **payload):
        if operation == "register-task-definition":
            return {"taskDefinition": {"taskDefinitionArn": "migration:1"}}
        if operation == "run-task":
            launches.append(payload)
            return {"tasks": [{"taskArn": "task:already-started"}]}
        assert operation == "describe-tasks" and payload["tasks"] == ["task:already-started"]
        return real_aws(service_name, operation, **payload)

    def run(command, **kwargs):
        if failure == "cli-timeout":
            return subprocess.CompletedProcess(command, 255, "", "Read timeout on endpoint URL: private")
        if failure in {"null", "[]"}:
            return subprocess.CompletedProcess(command, 0, failure, "")
        raise subprocess.TimeoutExpired(command, kwargs["timeout"], stderr="private-stderr")

    monkeypatch.setattr(deploy, "aws", dispatch)
    monkeypatch.setattr(deploy, "wait", lambda *a, **kw: None)
    monkeypatch.setattr(deploy.subprocess, "run", run)
    with pytest.raises(deploy.ReleasePending, match="task:already-started") as caught:
        deploy.migrate(release_plan())
    assert len(launches) == 1 and "do not launch another migration" in str(caught.value)
    assert "private" not in str(caught.value)


@pytest.mark.parametrize("failure", ["update-timeout", "cli-update-timeout", "update-ack-lost", "status-null", "status-list", "smoke-timeout", "smoke-failed", "rollback-timeout", "rollback-ack-lost"])
def test_bounded_release_preserves_unknown_ack_and_verified_rollback(monkeypatch, capsys, failure):
    plan, updates = release_plan(), []
    real_aws = deploy.aws
    monkeypatch.setattr(deploy, "migrate", lambda _: "migration:success")
    monkeypatch.setattr(deploy, "wait", lambda *a, **kw: None)

    def dispatch(service_name, operation, **payload):
        prior = previous_image_read(operation, payload)
        if prior is not None:
            return prior
        if operation == "register-task-definition":
            return {"taskDefinition": {"taskDefinitionArn": payload["family"] + ":new"}}
        if operation == "update-service":
            updates.append(payload["taskDefinition"])
            if failure in {"update-timeout", "cli-update-timeout", "update-ack-lost"} or (failure in {"rollback-timeout", "rollback-ack-lost"} and payload["taskDefinition"].endswith(":old")):
                return real_aws(service_name, operation, **payload)
            return {}
        if operation == "describe-services":
            if failure in {"status-null", "status-list"}:
                return real_aws(service_name, operation, **payload)
            return {"services": [{"serviceName": row["name"], "taskDefinition": row["name"] + ":new",
                                   "runningCount": 1, "desiredCount": 1} for row in plan["services"]]}
        pytest.fail(operation)

    def run(command, **kwargs):
        assert kwargs["timeout"] == (120 if command[0] == "aws" else 60)
        if failure == "cli-update-timeout":
            return subprocess.CompletedProcess(command, 255, "", "Read timeout on endpoint URL: private")
        if command[0] == "aws" and failure in {"update-ack-lost", "rollback-ack-lost"}:
            return subprocess.CompletedProcess(command, 0, '{}', "private-stderr")
        if command[0] == "aws" and failure in {"status-null", "status-list"}:
            return subprocess.CompletedProcess(command, 0, "null" if failure == "status-null" else "[]", "")
        if command[0] == "aws" or failure == "smoke-timeout":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"], stderr="private-stderr")
        assert kwargs["capture_output"] is True and kwargs["text"] is True
        return subprocess.CompletedProcess(command, 1, "private-health-body", "private-stderr")

    monkeypatch.setattr(deploy, "aws", dispatch)
    monkeypatch.setattr(deploy.subprocess, "run", run)
    with pytest.raises(deploy.ReleaseError) as caught:
        deploy.apply_release(plan)
    assert isinstance(caught.value, deploy.ReleasePending) is (failure != "smoke-failed")
    expected_count = {"update-timeout": 1, "cli-update-timeout": 1, "update-ack-lost": 1, "status-null": 4, "status-list": 4, "smoke-timeout": 4, "smoke-failed": 8, "rollback-timeout": 5, "rollback-ack-lost": 5}[failure]
    assert len(updates) == expected_count
    if failure in {"update-timeout", "cli-update-timeout", "update-ack-lost", "status-null", "status-list", "smoke-timeout"}:
        assert all(revision.endswith(":new") for revision in updates)
    elif failure == "smoke-failed":
        assert updates[-4:] == [row["previous"] for row in reversed(plan["services"])]
        assert "Previous service revisions restored" in str(caught.value)
    else:
        assert updates[-1] == plan["services"][-1]["previous"]
        assert "restored" not in str(caught.value)
    output = capsys.readouterr().out
    assert "private" not in output and "private" not in str(caught.value)
    assert all(set(json.loads(line)) == {"phase", "outcome"} for line in output.splitlines())


@pytest.mark.parametrize("lost_ack", ["forward", "rollback"])
@pytest.mark.parametrize("returncode,stderr", [*UNKNOWN_MUTATION_RESULTS, (None, "private undecodable output")])
def test_unknown_cli_full_release_retains_update_custody(monkeypatch, capsys, lost_ack, returncode, stderr):
    """Synthetic CLI edge; execute the actual migration/release/rollback flow."""
    plan, calls, updates = release_plan(), [], []

    def run(command, **kwargs):
        if command[0] == "python3":
            assert lost_ack == "rollback" and command[1:] == ["scripts/smoke.py", "--env", "production", "--require"]
            return subprocess.CompletedProcess(command, 1, "private-health-body", "private-stderr")
        assert command[:2] == ["aws", "ecs"] or command[:2] == ["aws", "ecr"]
        operation, payload = command[2], json.loads(command[-1])
        calls.append(operation)
        response = previous_image_read(operation, payload)
        if response is None:
            if operation == "register-task-definition":
                response = {"taskDefinition": {"taskDefinitionArn": payload["family"] + ":new"}}
            elif operation == "run-task":
                assert payload["count"] == 1
                response = {"tasks": [{"taskArn": "task:migration"}]}
            elif operation == "wait":
                return subprocess.CompletedProcess(command, 0, "", "")
            elif operation == "describe-tasks":
                response = {"tasks": [{"lastStatus": "STOPPED", "containers": [{"exitCode": 0}]}]}
            elif operation == "update-service":
                updates.append((payload["service"], payload["taskDefinition"]))
                if lost_ack == "forward" or payload["taskDefinition"].endswith(":old"):
                    if returncode is None:
                        raise UnicodeDecodeError("utf-8", b"private\xff", 7, 8, "private undecodable output")
                    return subprocess.CompletedProcess(command, returncode, "", stderr)
                response = {"service": {"taskDefinition": payload["taskDefinition"]}}
            elif operation == "describe-services":
                response = {"services": [{"serviceName": row["name"], "taskDefinition": row["name"] + ":new",
                                         "runningCount": 1, "desiredCount": 1} for row in plan["services"]]}
            else:
                pytest.fail(operation)
        return subprocess.CompletedProcess(command, 0, json.dumps(response), "")

    monkeypatch.setattr(deploy.subprocess, "run", run)
    with pytest.raises(deploy.ReleasePending, match="acknowledgement unresolved") as caught:
        deploy.apply_release(plan)
    assert calls.count("run-task") == 1, "Unknown update ACK must not replay the migration"
    if lost_ack == "forward":
        assert updates == [(plan["services"][0]["name"], plan["services"][0]["name"] + ":new")]
        assert "describe-services" not in calls
    else:
        assert updates == [(row["name"], row["name"] + ":new") for row in plan["services"]] + [
            (plan["services"][-1]["name"], plan["services"][-1]["previous"])]
    assert "restored" not in str(caught.value)
    output = capsys.readouterr()
    assert "private" not in str(caught.value) + output.out + output.err


@pytest.mark.parametrize("operation,pending", [("register-task-definition", True), ("run-task", True),
                                             ("update-service", True), ("describe-services", False)])
def test_aws_output_decode_failure_retains_unknown_mutation_and_private_read_error(monkeypatch, operation, pending):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        raise UnicodeDecodeError("utf-8", b"private\xff", 7, 8, "private undecodable output")

    monkeypatch.setattr(deploy.subprocess, "run", run)
    with pytest.raises(deploy.ReleaseError) as caught:
        deploy.aws("ecs", operation, token="private-input")
    assert isinstance(caught.value, deploy.ReleasePending) is pending
    assert len(calls) == 1 and "private" not in str(caught.value)

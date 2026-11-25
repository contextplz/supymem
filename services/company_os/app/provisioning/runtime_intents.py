"""Admission boundary for connected founder-application deployments.

This module persists authority before any cloud effect. It deliberately does
not call AWS; the fenced controller advances the saved intent phase by phase.
"""
from __future__ import annotations

from core.deployment import infrastructure
import hashlib
import json
import re
from typing import Any

from core.application_source import validate_product_manifest
from core.artifact_policy import ArtifactRejected
from core.artifact_snapshot import manifest_digest
from core.clock import utcnow
from core.ids import new_id
from sqlalchemy import func, select

from app.builder.access import authorize
from app.db.session import serialize_writes
from app.db.supymem_models import (
    AppDomain,
    AppRuntimeIntent,
    BuildArtifact,
    BuildRun,
    ExecutionPlan,
    FounderApp,
    PlanMilestone,
    PlanTask,
    QaRun,
)
from app.state import db

PRODUCT_CAPABILITIES = frozenset({"build.run"})
OPEN_STATES = frozenset({"pending", "provisioning", "reconciling", "staged", "review"})


class RuntimeIntentError(ValueError):
    pass


def product_hostname(app_id: str, slug: str) -> str:
    """Keep the functional app separate from its already-live landing page."""
    if (not re.fullmatch(r"app_[a-z0-9]{8,64}", app_id)
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,47}[a-z0-9])?", slug)):
        raise RuntimeIntentError("app_runtime_hostname_input_invalid")
    stem = slug[:34].rstrip("-")
    suffix = hashlib.sha256(app_id.encode()).hexdigest()[:8]
    return f"{stem}-app-{suffix}.supymem.com"


def _json(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeIntentError("app_runtime_receipt_invalid") from exc
    if not isinstance(value, dict):
        raise RuntimeIntentError("app_runtime_receipt_invalid")
    return value


def _digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _serialize(row: AppRuntimeIntent) -> dict[str, Any]:
    def stamp(value):
        return value.replace(tzinfo=None).isoformat()

    return {
        "id": row.id,
        "app_id": row.app_id,
        "workspace_id": row.workspace_id,
        "venture_id": row.venture_id,
        "plan_task_id": row.plan_task_id,
        "build_run_id": row.build_run_id,
        "source_artifact_id": row.source_artifact_id,
        "qa_run_id": row.qa_run_id,
        "domain_id": row.domain_id,
        "hostname": row.hostname,
        "generation": row.generation,
        "request_digest": row.request_digest,
        "state": row.state,
        "phase": row.phase,
        "approved_by": row.approved_by,
        "approved_at": stamp(row.approved_at),
        "created_at": stamp(row.created_at),
        "updated_at": stamp(row.updated_at),
    }


def _controller_view(row: AppRuntimeIntent) -> dict[str, Any]:
    value = _serialize(row)
    value.update({
        "source_sha256": row.source_sha256,
        "source_manifest_digest": row.source_manifest_digest,
        "qa_contract_digest": row.qa_contract_digest,
        "receipts": _json(row.receipts_json),
    })
    return value


def controller_view(intent_id: str) -> dict[str, Any]:
    """Return an internal controller snapshot without a founder credential."""
    with db()() as session:
        row = session.get(AppRuntimeIntent, intent_id)
        if row is None:
            raise KeyError("app_runtime_intent_not_found")
        return _controller_view(row)


def founder_status(*, app_id: str, actor: str) -> dict[str, Any] | None:
    """Show the owner a safe deployment checkpoint, never provider receipts or secrets."""
    with db()() as session:
        app = session.get(FounderApp, app_id)
        if app is None or app.deleted_at:
            raise KeyError("app_runtime_not_found")
        authorize(session, workspace_id=app.workspace_id, venture_id=app.venture_id,
                  actor=actor, mutate=False)
        row = session.scalar(select(AppRuntimeIntent).where(
            AppRuntimeIntent.app_id == app_id,
            AppRuntimeIntent.workspace_id == app.workspace_id,
            AppRuntimeIntent.venture_id == app.venture_id,
        ).order_by(AppRuntimeIntent.generation.desc()).limit(1))
        if row is None:
            return None
        ready_for_private_qa = row.phase in {"runtime_qa", "publication_review", "live"}
        return {
            "id": row.id,
            "build_run_id": row.build_run_id,
            "generation": row.generation,
            "state": row.state,
            "phase": row.phase,
            "preview_url": f"https://{row.hostname}/" if ready_for_private_qa else None,
        }


def record_database_secret(intent_id: str, receipt: dict[str, str]) -> dict[str, Any]:
    """Persist the credential resource identity, never its secret value."""
    with db()() as session:
        serialize_writes(session, key="founder-runtime-intent:" + intent_id)
        row = session.get(AppRuntimeIntent, intent_id, with_for_update=True)
        if row is None:
            raise KeyError("app_runtime_intent_not_found")
        name, arn = receipt.get("secret_name"), receipt.get("secret_arn")
        expected = f"supymem/founder-apps/{row.app_id}/database-{row.request_digest[:16]}"
        if (name != expected or not isinstance(arn, str)
                or not re.fullmatch(rf"arn:aws:secretsmanager:us-east-1:\d{{12}}:secret:{re.escape(expected)}-[A-Za-z0-9]{{6}}", arn)
                or set(receipt) != {"secret_name", "secret_arn"}):
            raise RuntimeIntentError("app_runtime_database_secret_receipt_invalid")
        receipts = _json(row.receipts_json)
        prior = receipts.get("database_secret")
        if prior is not None:
            if prior != receipt:
                raise RuntimeIntentError("app_runtime_database_secret_receipt_conflict")
            return _controller_view(row)
        if row.phase != "source_accepted" or row.state not in {"pending", "reconciling"}:
            raise RuntimeIntentError("app_runtime_phase_conflict")
        receipts["database_secret"] = receipt
        row.receipts_json = json.dumps(receipts, sort_keys=True, separators=(",", ":"))
        row.phase, row.state, row.updated_at = "database_secret_created", "provisioning", utcnow()
        session.commit()
        return _controller_view(row)


def record_database_launch(intent_id: str, receipt: dict[str, str]) -> dict[str, Any]:
    """Persist ECS identity before waiting for the one-shot task to finish."""
    with db()() as session:
        serialize_writes(session, key="founder-runtime-intent:" + intent_id)
        row = session.get(AppRuntimeIntent, intent_id, with_for_update=True)
        if row is None:
            raise KeyError("app_runtime_intent_not_found")
        task_arn, definition, token = (
            receipt.get("task_arn"), receipt.get("task_definition_arn"), receipt.get("client_token"))
        if (not isinstance(task_arn, str) or not re.fullmatch(
                infrastructure('arn:aws:ecs:us-east-1:\\d{12}:task/${PRODUCTION_PREFIX}-apps/[a-f0-9-]+'), task_arn)
                or not isinstance(definition, str) or not re.fullmatch(
                    r"arn:aws:ecs:us-east-1:\d{12}:task-definition/supymem-founder-db-[a-f0-9]{24}:[1-9][0-9]*",
                    definition)
                or not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{64}", token)
                or set(receipt) != {"task_arn", "task_definition_arn", "client_token"}):
            raise RuntimeIntentError("app_runtime_database_launch_receipt_invalid")
        receipts = _json(row.receipts_json)
        prior = receipts.get("database_launch")
        if prior is not None:
            if prior != receipt:
                raise RuntimeIntentError("app_runtime_database_launch_receipt_conflict")
            return _controller_view(row)
        if (row.phase != "database_secret_created" or row.state not in {"provisioning", "reconciling"}
                or "database_secret" not in receipts):
            raise RuntimeIntentError("app_runtime_phase_conflict")
        receipts["database_launch"] = receipt
        row.receipts_json = json.dumps(receipts, sort_keys=True, separators=(",", ":"))
        row.phase, row.state, row.updated_at = "database_provisioning", "provisioning", utcnow()
        session.commit()
        return _controller_view(row)


def record_database_observation(intent_id: str, observation: dict[str, object]) -> dict[str, Any]:
    """Record a verified task outcome; failed/unknown effects remain recoverable."""
    state, exit_code, succeeded = (
        observation.get("state"), observation.get("exit_code"), observation.get("succeeded"))
    if (state not in {"PROVISIONING", "PENDING", "ACTIVATING", "RUNNING", "DEACTIVATING", "STOPPED"}
            or (exit_code is not None and (not isinstance(exit_code, int) or not 0 <= exit_code <= 255))
            or not isinstance(succeeded, bool)
            or succeeded != (state == "STOPPED" and exit_code == 0)
            or set(observation) != {"state", "exit_code", "succeeded", "stopped_reason"}):
        raise RuntimeIntentError("app_runtime_database_observation_invalid")
    with db()() as session:
        serialize_writes(session, key="founder-runtime-intent:" + intent_id)
        row = session.get(AppRuntimeIntent, intent_id, with_for_update=True)
        if row is None:
            raise KeyError("app_runtime_intent_not_found")
        receipts = _json(row.receipts_json)
        if row.phase != "database_provisioning" or "database_launch" not in receipts:
            raise RuntimeIntentError("app_runtime_phase_conflict")
        receipts["database_observation"] = observation
        if succeeded:
            receipts["database"] = {"status": "ready", "verified_by": "one_shot_task"}
            row.state = "provisioning"
        elif state == "STOPPED":
            row.state = "reconciling"
        row.receipts_json = json.dumps(receipts, sort_keys=True, separators=(",", ":"))
        row.updated_at = utcnow()
        session.commit()
        return _controller_view(row)


def record_auth(intent_id: str, receipt: dict[str, str]) -> dict[str, Any]:
    """Bind one dedicated end-user identity pool to the runtime generation."""
    required = {"pool_id", "pool_arn", "client_id", "issuer", "domain", "redirect_uri", "app_origin"}
    if set(receipt) != required or any(not isinstance(value, str) for value in receipt.values()):
        raise RuntimeIntentError("app_runtime_auth_receipt_invalid")
    with db()() as session:
        serialize_writes(session, key="founder-runtime-intent:" + intent_id)
        row = session.get(AppRuntimeIntent, intent_id, with_for_update=True)
        if row is None:
            raise KeyError("app_runtime_intent_not_found")
        suffix = hashlib.sha256(row.app_id.encode()).hexdigest()[:24]
        pool_id = receipt["pool_id"]
        arn_match = re.fullmatch(r"arn:aws:cognito-idp:us-east-1:(\d{12}):userpool/(us-east-1_[A-Za-z0-9]+)",
                                 receipt["pool_arn"])
        expected_origin = "https://" + row.hostname
        if (not arn_match or arn_match[2] != pool_id
                or not re.fullmatch(r"[a-z0-9]{10,128}", receipt["client_id"])
                or receipt["issuer"] != f"https://cognito-idp.us-east-1.amazonaws.com/{pool_id}"
                or receipt["domain"] != f"https://supymem-app-{suffix}.auth.us-east-1.amazoncognito.com"
                or receipt["app_origin"] != expected_origin
                or receipt["redirect_uri"] != expected_origin + "/auth/callback"):
            raise RuntimeIntentError("app_runtime_auth_receipt_invalid")
        receipts = _json(row.receipts_json)
        prior = receipts.get("auth")
        if prior is not None:
            if prior != receipt:
                raise RuntimeIntentError("app_runtime_auth_receipt_conflict")
            return _controller_view(row)
        if (row.phase != "database_provisioning" or row.state not in {"provisioning", "reconciling"}
                or receipts.get("database", {}).get("status") != "ready"):
            raise RuntimeIntentError("app_runtime_phase_conflict")
        receipts["auth"] = receipt
        row.receipts_json = json.dumps(receipts, sort_keys=True, separators=(",", ":"))
        row.phase, row.state, row.updated_at = "auth_provisioned", "provisioning", utcnow()
        session.commit()
        return _controller_view(row)


def record_image_launch(intent_id: str, receipt: dict[str, str]) -> dict[str, Any]:
    """Persist the exact CodeBuild identity before observing image publication."""
    required = {"build_id", "idempotency_token", "image_tag", "project_name"}
    if set(receipt) != required or any(not isinstance(value, str) for value in receipt.values()):
        raise RuntimeIntentError("app_runtime_image_launch_receipt_invalid")
    with db()() as session:
        serialize_writes(session, key="founder-runtime-intent:" + intent_id)
        row = session.get(AppRuntimeIntent, intent_id, with_for_update=True)
        if row is None:
            raise KeyError("app_runtime_intent_not_found")
        suffix = hashlib.sha256(row.app_id.encode()).hexdigest()[:24]
        expected_tag = f"app-{suffix}-g{row.generation}-{row.request_digest[:16]}"
        if (receipt["project_name"] != infrastructure('${PRODUCTION_PREFIX}-founder-image')
                or not re.fullmatch(infrastructure('${PRODUCTION_PREFIX}-founder-image:[a-f0-9-]{36}'), receipt["build_id"])
                or receipt["idempotency_token"] != hashlib.sha256((intent_id + ":image").encode()).hexdigest()
                or receipt["image_tag"] != expected_tag):
            raise RuntimeIntentError("app_runtime_image_launch_receipt_invalid")
        receipts = _json(row.receipts_json)
        prior = receipts.get("image_launch")
        if prior is not None:
            if prior != receipt:
                raise RuntimeIntentError("app_runtime_image_launch_receipt_conflict")
            return _controller_view(row)
        if row.phase != "auth_provisioned" or row.state not in {"provisioning", "reconciling"}:
            raise RuntimeIntentError("app_runtime_phase_conflict")
        receipts["image_launch"] = receipt
        row.receipts_json = json.dumps(receipts, sort_keys=True, separators=(",", ":"))
        row.phase, row.state, row.updated_at = "image_building", "provisioning", utcnow()
        session.commit()
        return _controller_view(row)


def record_image_observation(intent_id: str, observation: dict[str, object]) -> dict[str, Any]:
    """Accept only a scanned immutable image; unsafe terminal results remain failed."""
    required = {"state", "image_uri", "scan_status", "critical", "high"}
    state = observation.get("state")
    if (set(observation) != required
            or state not in {"IN_PROGRESS", "SUCCEEDED", "FAILED", "FAULT", "STOPPED", "TIMED_OUT"}
            or any(value is not None and (type(value) is not int or value < 0)
                   for value in (observation.get("critical"), observation.get("high")))):
        raise RuntimeIntentError("app_runtime_image_observation_invalid")
    with db()() as session:
        serialize_writes(session, key="founder-runtime-intent:" + intent_id)
        row = session.get(AppRuntimeIntent, intent_id, with_for_update=True)
        if row is None:
            raise KeyError("app_runtime_intent_not_found")
        receipts = _json(row.receipts_json)
        if row.phase != "image_building" or "image_launch" not in receipts:
            raise RuntimeIntentError("app_runtime_phase_conflict")
        launch = receipts["image_launch"]
        image_uri = observation.get("image_uri")
        expected_repo = infrastructure('${AWS_ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/supymem-founder-apps@')
        if (state == "SUCCEEDED"
                and (not isinstance(image_uri, str)
                     or not re.fullmatch(re.escape(expected_repo) + r"sha256:[a-f0-9]{64}", image_uri)
                     or observation.get("scan_status") not in {"IN_PROGRESS", "COMPLETE"}
                     or type(observation.get("critical")) is not int
                     or type(observation.get("high")) is not int)):
            raise RuntimeIntentError("app_runtime_image_observation_invalid")
        if state != "SUCCEEDED" and any(observation.get(key) is not None for key in
                                         ("image_uri", "scan_status", "critical", "high")):
            raise RuntimeIntentError("app_runtime_image_observation_invalid")
        receipts["image_observation"] = observation
        safe = (state == "SUCCEEDED" and observation.get("scan_status") == "COMPLETE"
                and observation.get("critical") == 0 and observation.get("high") == 0)
        if safe:
            receipts["image"] = {"status": "ready", "image_uri": image_uri,
                                 "image_tag": launch["image_tag"], "scan": "complete_no_critical_or_high"}
            row.phase, row.state = "image_ready", "provisioning"
        elif state in {"FAILED", "FAULT", "STOPPED", "TIMED_OUT"} or (
                state == "SUCCEEDED" and observation.get("scan_status") == "COMPLETE"):
            row.state = "reconciling"
        row.receipts_json = json.dumps(receipts, sort_keys=True, separators=(",", ":"))
        row.updated_at = utcnow()
        session.commit()
        return _controller_view(row)


def record_migration_launch(intent_id: str, receipt: dict[str, str]) -> dict[str, Any]:
    """Persist the one-shot migration task identity before observing it."""
    required = {"task_arn", "task_definition_arn", "client_token"}
    if (set(receipt) != required
            or not re.fullmatch(infrastructure('arn:aws:ecs:us-east-1:\\d{12}:task/${PRODUCTION_PREFIX}-apps/[a-f0-9-]+'),
                                str(receipt.get("task_arn", "")))
            or not re.fullmatch(r"arn:aws:ecs:us-east-1:\d{12}:task-definition/"
                                r"supymem-founder-migration-[a-f0-9]{24}:[1-9][0-9]*",
                                str(receipt.get("task_definition_arn", "")))
            or not re.fullmatch(r"[a-f0-9]{64}", str(receipt.get("client_token", "")))):
        raise RuntimeIntentError("app_runtime_migration_launch_receipt_invalid")
    with db()() as session:
        serialize_writes(session, key="founder-runtime-intent:" + intent_id)
        row = session.get(AppRuntimeIntent, intent_id, with_for_update=True)
        if row is None:
            raise KeyError("app_runtime_intent_not_found")
        expected_token = hashlib.sha256((intent_id + ":migration").encode()).hexdigest()
        if receipt["client_token"] != expected_token:
            raise RuntimeIntentError("app_runtime_migration_launch_receipt_invalid")
        receipts = _json(row.receipts_json)
        prior = receipts.get("migration_launch")
        if prior is not None:
            if prior != receipt:
                raise RuntimeIntentError("app_runtime_migration_launch_receipt_conflict")
            return _controller_view(row)
        if (row.phase != "image_ready" or row.state not in {"provisioning", "reconciling"}
                or receipts.get("image", {}).get("status") != "ready"):
            raise RuntimeIntentError("app_runtime_phase_conflict")
        receipts["migration_launch"] = receipt
        row.receipts_json = json.dumps(receipts, sort_keys=True, separators=(",", ":"))
        row.state, row.updated_at = "provisioning", utcnow()
        session.commit()
        return _controller_view(row)


def record_migration_observation(intent_id: str, observation: dict[str, object]) -> dict[str, Any]:
    """Advance only after the exact migration task exits successfully."""
    state, exit_code, succeeded = (
        observation.get("state"), observation.get("exit_code"), observation.get("succeeded"))
    if (state not in {"PROVISIONING", "PENDING", "ACTIVATING", "RUNNING", "DEACTIVATING", "STOPPED"}
            or (exit_code is not None and (not isinstance(exit_code, int) or not 0 <= exit_code <= 255))
            or not isinstance(succeeded, bool)
            or succeeded != (state == "STOPPED" and exit_code == 0)
            or set(observation) != {"state", "exit_code", "succeeded", "stopped_reason"}):
        raise RuntimeIntentError("app_runtime_migration_observation_invalid")
    with db()() as session:
        serialize_writes(session, key="founder-runtime-intent:" + intent_id)
        row = session.get(AppRuntimeIntent, intent_id, with_for_update=True)
        if row is None:
            raise KeyError("app_runtime_intent_not_found")
        receipts = _json(row.receipts_json)
        if row.phase != "image_ready" or "migration_launch" not in receipts:
            raise RuntimeIntentError("app_runtime_phase_conflict")
        receipts["migration_observation"] = observation
        if succeeded:
            receipts["migration"] = {"status": "verified", "verified_by": "one_shot_task"}
            row.phase, row.state = "runtime_migrated", "provisioning"
        elif state == "STOPPED":
            row.state = "reconciling"
        row.receipts_json = json.dumps(receipts, sort_keys=True, separators=(",", ":"))
        row.updated_at = utcnow()
        session.commit()
        return _controller_view(row)


def record_service(intent_id: str, receipt: dict[str, str]) -> dict[str, Any]:
    """Bind the exact task, target, listener rule and ECS service to this generation."""
    required = {"task_definition_arn", "target_group_arn", "listener_rule_arn", "service_arn"}
    if set(receipt) != required or any(not isinstance(value, str) for value in receipt.values()):
        raise RuntimeIntentError("app_runtime_service_receipt_invalid")
    with db()() as session:
        serialize_writes(session, key="founder-runtime-intent:" + intent_id)
        row = session.get(AppRuntimeIntent, intent_id, with_for_update=True)
        if row is None:
            raise KeyError("app_runtime_intent_not_found")
        suffix = hashlib.sha256(row.app_id.encode()).hexdigest()[:24]
        if (not re.fullmatch(rf"arn:aws:ecs:us-east-1:\d{{12}}:task-definition/"
                            rf"supymem-founder-app-{suffix}:[1-9][0-9]*", receipt["task_definition_arn"])
                or not re.fullmatch(rf"arn:aws:elasticloadbalancing:us-east-1:\d{{12}}:"
                                    rf"targetgroup/supa-{suffix}/[a-f0-9]{{16}}",
                                    receipt["target_group_arn"])
                or not re.fullmatch(infrastructure('arn:aws:elasticloadbalancing:us-east-1:\\d{12}:listener-rule/app/${PRODUCTION_PREFIX}-apps/[a-f0-9]{16}/[a-f0-9]{16}/[a-f0-9]{16}'), receipt["listener_rule_arn"])
                or not re.fullmatch(f'{infrastructure('arn:aws:ecs:us-east-1:\\d{12}:service/${PRODUCTION_PREFIX}-apps/supymem-founder-app-')}{suffix}',
                                    receipt["service_arn"])):
            raise RuntimeIntentError("app_runtime_service_receipt_invalid")
        receipts = _json(row.receipts_json)
        prior = receipts.get("service")
        if prior is not None:
            if prior != receipt:
                raise RuntimeIntentError("app_runtime_service_receipt_conflict")
            return _controller_view(row)
        if (row.phase != "runtime_migrated" or row.state not in {"provisioning", "reconciling"}
                or receipts.get("migration", {}).get("status") != "verified"):
            raise RuntimeIntentError("app_runtime_phase_conflict")
        receipts["service"] = receipt
        row.receipts_json = json.dumps(receipts, sort_keys=True, separators=(",", ":"))
        row.phase, row.state, row.updated_at = "service_staged", "staged", utcnow()
        session.commit()
        return _controller_view(row)


def record_service_observation(intent_id: str, observation: dict[str, object]) -> dict[str, Any]:
    """Persist health without exposing DNS until the exact service is ready."""
    required = {"state", "desired", "running", "pending", "target_states", "healthy", "failed"}
    if (set(observation) != required or observation.get("state") not in {"PROVISIONING", "HEALTHY", "FAILED"}
            or any(type(observation.get(key)) is not int or observation[key] < 0
                   for key in ("desired", "running", "pending"))
            or not isinstance(observation.get("target_states"), list)
            or any(value not in {"initial", "healthy", "unhealthy", "draining", "unused", "unavailable"}
                   for value in observation["target_states"])
            or type(observation.get("healthy")) is not bool or type(observation.get("failed")) is not bool
            or observation["healthy"] != (observation["state"] == "HEALTHY")
            or observation["failed"] != (observation["state"] == "FAILED")):
        raise RuntimeIntentError("app_runtime_service_observation_invalid")
    with db()() as session:
        serialize_writes(session, key="founder-runtime-intent:" + intent_id)
        row = session.get(AppRuntimeIntent, intent_id, with_for_update=True)
        if row is None:
            raise KeyError("app_runtime_intent_not_found")
        receipts = _json(row.receipts_json)
        if row.phase != "service_staged" or "service" not in receipts:
            raise RuntimeIntentError("app_runtime_phase_conflict")
        receipts["service_observation"] = observation
        row.state = "reconciling" if observation["failed"] else "staged"
        row.receipts_json = json.dumps(receipts, sort_keys=True, separators=(",", ":"))
        row.updated_at = utcnow()
        session.commit()
        return _controller_view(row)


def record_dns_launch(intent_id: str, receipt: dict[str, str]) -> dict[str, Any]:
    """Persist the exact Route53 change only after service health succeeds."""
    if (set(receipt) != {"change_id", "hostname"}
            or not re.fullmatch(r"/change/[A-Z0-9]+", str(receipt.get("change_id", "")))):
        raise RuntimeIntentError("app_runtime_dns_receipt_invalid")
    with db()() as session:
        serialize_writes(session, key="founder-runtime-intent:" + intent_id)
        row = session.get(AppRuntimeIntent, intent_id, with_for_update=True)
        if row is None:
            raise KeyError("app_runtime_intent_not_found")
        if receipt.get("hostname") != row.hostname:
            raise RuntimeIntentError("app_runtime_dns_receipt_invalid")
        receipts = _json(row.receipts_json)
        prior = receipts.get("dns_launch")
        if prior is not None:
            if prior != receipt:
                raise RuntimeIntentError("app_runtime_dns_receipt_conflict")
            return _controller_view(row)
        if (row.phase != "service_staged" or row.state != "staged"
                or receipts.get("service_observation", {}).get("healthy") is not True):
            raise RuntimeIntentError("app_runtime_phase_conflict")
        receipts["dns_launch"] = receipt
        row.receipts_json = json.dumps(receipts, sort_keys=True, separators=(",", ":"))
        row.phase, row.updated_at = "preview_live", utcnow()
        session.commit()
        return _controller_view(row)


def record_dns_observation(intent_id: str, observation: dict[str, object]) -> dict[str, Any]:
    """Advance to runtime QA after Route53 reports the admitted change in sync."""
    if (set(observation) != {"status", "ready"} or observation.get("status") not in {"PENDING", "INSYNC"}
            or type(observation.get("ready")) is not bool
            or observation["ready"] != (observation["status"] == "INSYNC")):
        raise RuntimeIntentError("app_runtime_dns_observation_invalid")
    with db()() as session:
        serialize_writes(session, key="founder-runtime-intent:" + intent_id)
        row = session.get(AppRuntimeIntent, intent_id, with_for_update=True)
        if row is None:
            raise KeyError("app_runtime_intent_not_found")
        receipts = _json(row.receipts_json)
        if row.phase != "preview_live" or "dns_launch" not in receipts:
            raise RuntimeIntentError("app_runtime_phase_conflict")
        receipts["dns_observation"] = observation
        if observation["ready"]:
            row.phase, row.state = "runtime_qa", "review"
        row.receipts_json = json.dumps(receipts, sort_keys=True, separators=(",", ":"))
        row.updated_at = utcnow()
        session.commit()
        return _controller_view(row)


def admit(*, app_id: str, workspace_id: str, venture_id: str, actor: str,
          plan_task_id: str, build_run_id: str, source_artifact_id: str,
          qa_run_id: str, source_manifest_digest: str, qa_contract_digest: str,
          hostname: str, reservation_id: str | None = None) -> dict[str, Any]:
    """Persist one idempotent, evidence-bound runtime generation."""
    if (not re.fullmatch(r"[a-f0-9]{64}", source_manifest_digest)
            or not re.fullmatch(r"[a-f0-9]{64}", qa_contract_digest)
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.supymem\.com", hostname)):
        raise RuntimeIntentError("app_runtime_input_invalid")
    now = utcnow()
    with db()() as session:
        serialize_writes(session, key="founder-runtime:" + app_id)
        authorize(session, workspace_id=workspace_id, venture_id=venture_id, actor=actor, mutate=True)
        app = session.get(FounderApp, app_id, with_for_update=True)
        if app is None or app.deleted_at or (app.workspace_id, app.venture_id) != (workspace_id, venture_id):
            raise KeyError("app_runtime_not_found")
        task = session.get(PlanTask, plan_task_id)
        milestone = session.get(PlanMilestone, task.milestone_id) if task else None
        plan = session.get(ExecutionPlan, milestone.plan_id) if milestone else None
        try:
            hints = json.loads(task.tool_hints) if task else None
        except (TypeError, json.JSONDecodeError):
            hints = None
        if (task is None or milestone is None or plan is None
                or (task.workspace_id, task.venture_id, task.state) != (workspace_id, venture_id, "done")
                or (milestone.workspace_id, milestone.venture_id) != (workspace_id, venture_id)
                or milestone.scope == "out"
                or (plan.workspace_id, plan.venture_id) != (workspace_id, venture_id)
                or plan.state not in {"approved", "executing"} or not plan.approved_at
                or not plan.approved_by or plan.superseded_by
                or not isinstance(hints, list) or not hints
                or any(not isinstance(item, str) for item in hints)
                or not set(hints).issubset(PRODUCT_CAPABILITIES)):
            raise RuntimeIntentError("app_runtime_task_not_ready")
        run = session.get(BuildRun, build_run_id)
        artifact = session.get(BuildArtifact, source_artifact_id)
        qa = session.get(QaRun, qa_run_id)
        spec = _json(run.spec) if run else {}
        capture = spec.get("artifact_capture", {})
        review = spec.get("lifecycle", {}).get("review", {})
        completion = spec.get("lifecycle", {}).get("completion", {})
        if (run is None or artifact is None or qa is None
                or (run.app_id, run.workspace_id, run.venture_id, run.plan_task_id)
                != (app_id, workspace_id, venture_id, plan_task_id)
                or run.runtime_version != 1 or run.state != "succeeded" or not run.closed_at
                or spec.get("execution_verified") is not True
                or review.get("status") != "accepted" or review.get("decision") != "accept"
                or review.get("source_artifact_id") != source_artifact_id
                or completion.get("source_artifact_id") != source_artifact_id
                or completion.get("verified_execution") is not True
                or completion.get("verified_report") is not True
                or artifact.kind != "source_zip"
                or (artifact.build_run_id, artifact.app_id, artifact.workspace_id)
                != (build_run_id, app_id, workspace_id)
                or capture.get("state") != "stored" or capture.get("artifact_id") != source_artifact_id
                or capture.get("sha256") != artifact.sha256
                or capture.get("manifest_digest") != source_manifest_digest
                or capture.get("key") != artifact.s3_key
                or capture.get("size_bytes") != artifact.size_bytes
                or not isinstance(capture.get("bucket"), str)
                or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", capture["bucket"])
                or not isinstance(capture.get("version_id"), str)
                or not 1 <= len(capture["version_id"]) <= 1024 or capture["version_id"] == "null"
                or not re.fullmatch(r"[a-f0-9]{64}", str(capture.get("storage_config_digest", "")))
                or qa.state != "passed" or qa.failed != 0 or qa.unsupported != 0 or qa.passed <= 0
                or (qa.build_run_id, qa.app_id, qa.workspace_id, qa.venture_id, qa.source_artifact_id,
                    qa.source_sha256, qa.source_manifest_digest, qa.contract_digest)
                != (build_run_id, app_id, workspace_id, venture_id, source_artifact_id,
                    artifact.sha256, source_manifest_digest, qa_contract_digest)
                or qa.report_digest != review.get("report_digest")):
            raise RuntimeIntentError("app_runtime_source_not_accepted")
        try:
            product_manifest = capture.get("manifest")
            validate_product_manifest(product_manifest)
            if manifest_digest(product_manifest) != source_manifest_digest:
                raise ArtifactRejected("application_manifest_digest_invalid")
        except (ArtifactRejected, TypeError, KeyError, ValueError) as exc:
            raise RuntimeIntentError("app_runtime_source_not_accepted") from exc
        domain = session.scalar(select(AppDomain).where(AppDomain.hostname == hostname).with_for_update())
        if domain is not None and (domain.app_id, domain.workspace_id) != (app_id, workspace_id):
            raise RuntimeIntentError("app_runtime_hostname_taken")
        if domain is None:
            domain = AppDomain(id=new_id("adn"), app_id=app_id, workspace_id=workspace_id,
                               hostname=hostname, kind="product", state="pending",
                               created_at=now)
            session.add(domain)
            session.flush()
        source_receipt = {key: capture[key] for key in (
            "bucket", "key", "version_id", "sha256", "size_bytes",
            "manifest_digest", "storage_config_digest",
        )}
        request = {
            "app_id": app_id, "workspace_id": workspace_id, "venture_id": venture_id,
            "plan_task_id": plan_task_id, "build_run_id": build_run_id,
            "source_artifact_id": source_artifact_id, "source_sha256": artifact.sha256,
            "source_manifest_digest": source_manifest_digest, "qa_run_id": qa_run_id,
            "qa_contract_digest": qa_contract_digest, "domain_id": domain.id,
            "hostname": hostname, "reservation_id": reservation_id,
            "approved_by": actor,
        }
        request_digest = _digest({**request, "source_receipt": source_receipt})
        prior = session.scalar(select(AppRuntimeIntent).where(
            AppRuntimeIntent.app_id == app_id,
            AppRuntimeIntent.request_digest == request_digest,
        ))
        if prior is not None:
            return _serialize(prior)
        active = session.scalar(select(AppRuntimeIntent).where(
            AppRuntimeIntent.app_id == app_id,
            AppRuntimeIntent.state.in_(OPEN_STATES),
        ).with_for_update())
        if active is not None:
            raise RuntimeIntentError("app_runtime_intent_conflict")
        generation = (session.scalar(select(func.max(AppRuntimeIntent.generation)).where(
            AppRuntimeIntent.app_id == app_id)) or 0) + 1
        row = AppRuntimeIntent(
            id=new_id("ari"), generation=generation, request_digest=request_digest,
            state="pending", phase="source_accepted",
            receipts_json=json.dumps({"source": source_receipt}, sort_keys=True, separators=(",", ":")),
            approved_at=now, created_at=now, updated_at=now, **request,
        )
        session.add(row)
        session.commit()
        return _serialize(row)

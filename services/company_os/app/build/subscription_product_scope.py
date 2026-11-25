"""Dormant exact first-product-app binding; no selection by venture/name.

This does not borrow a static publication baseline or authorize replacing it.
The initial scope requires an explicit founder-created draft with no prior run
or deployment; product repair/hosting baselines require a separate contract.
"""

from __future__ import annotations

import json
import re

from sqlalchemy import select

from app.build import device_orders, native_product_source as native
from app.builder.access import authorize
from app.db.supymem_models import (
    BuildRun,
    Deployment,
    ExecutionPlan,
    FounderApp,
    FounderRequest,
    PlanMilestone,
    PlanTask,
)


def review_first_app(session, plan, selected: dict, *, app_id: str, actor: str,
                     active_run_id: str | None = None) -> dict:
    native.assert_current(plan, selected)
    authorize(
        session, workspace_id=plan.workspace_id, venture_id=plan.venture_id, actor=actor
    )
    app = (
        session.get(FounderApp, app_id) if isinstance(app_id, str) and app_id else None
    )
    if (
        app is None
        or app.deleted_at is not None
        or app.state != "draft"
        or (app.workspace_id, app.venture_id) != (plan.workspace_id, plan.venture_id)
        or app.build_mode != "supymem"
        or app.deploy_target != "supymem"
        or session.scalar(
            select(Deployment.id).where(Deployment.app_id == app.id).limit(1)
        )
    ):
        raise ValueError("subscription_product_first_app_unavailable")
    rows = session.scalars(
        select(FounderRequest).where(
            FounderRequest.related_id == app.id,
            FounderRequest.workspace_id == app.workspace_id,
            FounderRequest.venture_id == app.venture_id,
            FounderRequest.source == "app_create",
            FounderRequest.classification == "founder_approved",
            FounderRequest.outcome == "applied",
        )
    ).all()
    expected = {
        "name": app.name,
        "slug": app.slug,
        "kind": app.kind,
        "build_mode": app.build_mode,
        "deploy_target": app.deploy_target,
    }
    matches = []
    for row in rows:
        try:
            payload = json.loads(row.text)
            if payload.get("actor") == actor and payload.get("request") == expected:
                matches.append(row)
        except (TypeError, ValueError):
            continue
    if len(matches) != 1:
        raise ValueError("subscription_product_first_app_not_reviewed")
    baseline = {
        "schema_version": 1,
        "mode": "first_product_no_publication_v1",
        "app_id": app.id,
        "workspace_id": app.workspace_id,
        "venture_id": app.venture_id,
        "creation_request_id": matches[0].id,
        "creation_actor": actor,
        "app": expected,
        "prior_build_run_id": None,
        "prior_deployment_id": None,
    }
    bound = {**selected, "app_id": app.id, "product_app_baseline": baseline}
    runs = session.scalars(select(BuildRun).where(BuildRun.app_id == app.id)).all()
    for run in runs:
        try:
            spec = json.loads(run.spec)
            if (run.id != active_run_id or run.trigger != "subscription_product_diagnostic"
                    or run.runtime_version != 1
                    or (run.workspace_id, run.venture_id, run.plan_task_id)
                    != (plan.workspace_id, plan.venture_id, selected["task_id"])
                    or spec.get("lifecycle_version") != 4
                    or spec.get("source_identity", {}).get("task_snapshot") != bound):
                raise ValueError()
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise ValueError("subscription_product_first_app_unavailable") from error
    # An alleged active run must exist; otherwise its identifier is not an
    # exception to the reviewed first-app/no-prior-execution baseline.
    if active_run_id is not None and (len(runs) != 1 or runs[0].id != active_run_id):
        raise ValueError("subscription_product_first_app_unavailable")
    return bound


def current(session, venture_id: str, order_id: str, caller, *, active_run_id=None) -> dict:
    """Recover a future settled exact order/source binding, without admitting it.

    The new native capability remains unavailable and no device callsite can
    produce these orders yet. This helper never stores, reserves or launches.
    """
    from app.db.supymem_models import CodingSourceReceipt

    wid, actor, device = device_orders._scope(venture_id, caller)
    authorize(session, workspace_id=wid, venture_id=venture_id, actor=actor)
    _row, order = device_orders._saved_order(
        session, venture_id, order_id, wid, actor, device
    )
    saved = order.get("task_snapshot") or {}
    verification = (order.get("manifest") or {}).get("coding_verification") or {}
    stop = order.get("native_stop") or {}
    if (
        order.get("id") != order_id
        or order.get("state") != "stopped"
        or not order.get("claimed_at")
        or not order.get("closed_at")
        or not order.get("settlement_ledger_id")
        or stop.get("processes_stopped") is not True
        or stop.get("execution_id") != order.get("execution_id")
        or verification.get("operation") != "workspace_edit_v1"
        or verification.get("provider") not in {"codex", "claude"}
        or (order.get("manifest") or {}).get("task_id") != saved.get("task_id")
    ):
        raise ValueError("subscription_product_source_order_unsettled")
    task = session.get(PlanTask, saved.get("task_id"))
    milestone = session.get(PlanMilestone, task.milestone_id) if task else None
    plan = session.get(ExecutionPlan, milestone.plan_id) if milestone else None
    if plan is None or (plan.workspace_id, plan.venture_id) != (wid, venture_id):
        raise ValueError("subscription_product_source_scope_changed")
    selected = native.snapshot(
        plan, device_orders._task_snapshot(session, wid, venture_id, task.id)
    )
    selected = review_first_app(
        session, plan, selected, app_id=saved.get("app_id"), actor=actor,
        active_run_id=active_run_id,
    )
    if selected != saved:
        raise ValueError("subscription_product_source_scope_changed")
    source = session.get(CodingSourceReceipt, order_id)
    if (
        source is None
        or source.state != "stored"
        or (
            source.workspace_id,
            source.venture_id,
            source.app_id,
            source.actor_id,
            source.device_id,
            source.plan_task_id,
            source.execution_id,
            source.provider,
        )
        != (
            wid,
            venture_id,
            saved["app_id"],
            actor,
            device,
            saved["task_id"],
            order["execution_id"],
            verification["provider"],
        )
    ):
        raise ValueError("subscription_product_source_custody_changed")
    try:
        receipt = json.loads(source.receipt_json)
        if (
            not isinstance(receipt, dict)
            or not receipt.get("version_id")
            or receipt["version_id"] == "null"
            or not isinstance(source.sha256, str)
            or not re.fullmatch(r"[a-f0-9]{64}", source.sha256)
            or not isinstance(source.manifest_sha256, str)
            or not re.fullmatch(r"[a-f0-9]{64}", source.manifest_sha256)
            or type(source.size_bytes) is not int
            or not 0 < source.size_bytes <= 33554432
            or receipt.get("sha256") != source.sha256
            or receipt.get("size_bytes") != source.size_bytes
            or receipt.get("manifest_digest") != source.manifest_sha256
        ):
            raise ValueError()
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("subscription_product_source_receipt_invalid") from error
    return {
        "scope": {
            "workspace_id": wid,
            "venture_id": venture_id,
            "app_id": saved["app_id"],
            "actor_id": actor,
            "caller_subject": caller.sub,
            "device_id": device,
            "order_id": order_id,
            "execution_id": order["execution_id"],
            "provider": source.provider,
        },
        "task_snapshot": saved,
        "source": {
            "sha256": source.sha256,
            "manifest_digest": source.manifest_sha256,
            "size_bytes": source.size_bytes,
            "storage_receipt": receipt,
        },
        "runtime_qa_verified": False,
        "hosting_authorized": False,
    }

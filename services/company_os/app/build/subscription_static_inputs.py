"""Immutable source+approved-contract identity for subscription static QA."""
from __future__ import annotations

import json

from app.build import coding_source_intake as intake, device_orders
from app.builder import execution
from app.builder.model_proxy import digest
from app.db.supymem_models import BuildRun, CodingSourceReceipt, FounderApp


def current(session, venture_id: str, order_id: str, caller, *, active_run_id=None) -> tuple[dict, FounderApp]:
    """Recheck ownership, source custody and live approved scope before effects."""
    order, app_id = intake._scope(session, venture_id, order_id, caller)
    source = session.get(CodingSourceReceipt, order_id)
    if source is None or source.state != "stored":
        raise intake.SourceIntakeError("subscription_static_source_not_stored")
    app = execution._app(session, app_id, order["actor_id"], mutate=True)
    saved = order.get("task_snapshot") or {}
    if active_run_id:
        active = session.get(BuildRun, active_run_id)
        spec = json.loads(active.spec or "{}") if active else {}
        if (active is None or active.trigger != "coding_source_import"
                or spec.get("lifecycle_version") != 3
                or (active.workspace_id, active.venture_id, active.app_id, active.plan_task_id)
                != (order["workspace_id"], venture_id, app_id, saved["task_id"])
                or spec.get("source_import", {}).get("identity", {}).get("scope", {}).get("order_id") != order_id):
            raise intake.SourceIntakeError("subscription_static_source_scope_mismatch")
        live = device_orders._coding_snapshot_profile(session, device_orders._task_snapshot(
            session, order["workspace_id"], venture_id, saved["task_id"],
            source_import_run_id=active_run_id), source_import_run_id=active_run_id)
    else:
        live = device_orders._review_task_snapshot(session, order["workspace_id"], venture_id, order["manifest"])
    if live != saved or saved.get("artifact_profile") != "static_landing_v1":
        error = intake.SourceIntakeError("subscription_static_approved_inputs_changed")
        error.review_reason = (
            "subscription_static_profile_changed" if saved.get("artifact_profile") != "static_landing_v1" else
            "subscription_static_publication_baseline_changed" if live.get("publication_baseline") != saved.get("publication_baseline") else
            "subscription_static_contract_changed" if live.get("landing_expectations") != saved.get("landing_expectations") else
            "subscription_static_task_snapshot_changed")
        raise error
    # This also checks current dependency/idea provenance and superseding plans.
    inputs = execution._inputs(session, app, saved["plan_id"], saved["task_id"], None,
                               active_run_id=active_run_id, source_import=True)
    from app.build.subscription_static_baseline import assert_current
    baseline = saved.get("publication_baseline")
    try:
        assert_current(session, app, baseline,
                       accepted_artifact_id=active.artifact_id if active_run_id else None)
    except ValueError as error:
        raise intake.SourceIntakeError(str(error)) from error
    inputs["publication_baseline"] = baseline
    expected = saved["landing_expectations"]
    if (inputs["milestone"]["kind"] != "landing"
            or inputs["task"].get("landing_expectations") != expected):
        raise intake.SourceIntakeError("subscription_static_approved_inputs_changed")
    if ((source.workspace_id, source.venture_id, source.app_id, source.actor_id, source.device_id,
         source.plan_task_id, source.execution_id, source.provider)
            != (order["workspace_id"], venture_id, app_id, order["actor_id"], order["device_id"],
                saved["task_id"], order["execution_id"], order["manifest"]["coding_verification"]["provider"])):
        raise intake.SourceIntakeError("subscription_static_source_scope_mismatch")
    try:
        receipt = json.loads(source.receipt_json)
        if (not isinstance(receipt, dict) or not receipt.get("version_id")
                or receipt["version_id"] == "null" or receipt.get("sha256") != source.sha256
                or receipt.get("size_bytes") != source.size_bytes
                or receipt.get("manifest_digest") != source.manifest_sha256):
            raise ValueError()
    except (TypeError, ValueError, KeyError) as error:
        raise intake.SourceIntakeError("subscription_static_source_receipt_invalid") from error
    identity = {"schema_version": 1, "policy_id": "subscription_static_source_qa_v1",
        "scope": {"workspace_id": source.workspace_id, "venture_id": venture_id, "app_id": app_id,
                  "actor_id": source.actor_id, "caller_subject": caller.sub,
                  "device_id": source.device_id, "order_id": order_id,
                  "execution_id": source.execution_id, "provider": source.provider},
        "source": {"sha256": source.sha256, "manifest_digest": source.manifest_sha256,
                   "size_bytes": source.size_bytes, "storage_receipt": receipt},
        "task_snapshot": saved, "contract_digest": digest(expected)}
    return {"identity": identity, "fingerprint": digest(identity), "inputs": inputs}, app


def run_id(fingerprint: str) -> str:
    # One immutable source+contract identity has one admission regardless of
    # changed caller idempotency keys or a lost response after committing it.
    import re
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
        raise ValueError("subscription_static_fingerprint_invalid")
    return "bsi_" + fingerprint[:48]

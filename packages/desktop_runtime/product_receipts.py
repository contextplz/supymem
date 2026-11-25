"""Explicit replay of saved evidence only: no claim, CLI run or deployment."""
from __future__ import annotations

import hashlib
import json
import re
from types import SimpleNamespace

from desktop_runtime.coding_loop import post_pending
from desktop_runtime.hosted import _is_local as is_local
from desktop_runtime.owned_processes import OwnedProcesses


def cleanup_pending(order: dict, local: dict) -> bool:
    return (is_local() and bool((order.get("manifest") or {}).get("coding_loop_verification"))
            and local.get("state") in {"reconciliation_required", "stopped"}
            and (local.get("evidence") or {}).get("processes_stopped") is False)


def reconcile(gateway, order: dict, local: dict, save) -> dict:
    manifest = order.get("manifest") or {}
    evidence = local.get("evidence") or {}
    execution = evidence.get("execution_id")
    digest = local.get("evidence_digest")
    repair = cleanup_pending(order, local)
    if (not (manifest.get("coding_loop_verification") or manifest.get("deploy_verification"))
            or (order.get("state") not in {"claimed", "cancelled", "stopped"}
                and not (repair and order.get("state") == "waiting_for_device"))
            or (not repair and (local.get("state") != "stopped" or evidence.get("processes_stopped") is not True))
            or local.get("order_id") != gateway.order_id or order.get("id") != gateway.order_id
            or order.get("device_id") != gateway.device_id or not isinstance(execution, str)
            or not re.fullmatch(r"dex_[A-Za-z0-9]+", execution) or execution != order.get("execution_id")
            or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
            or hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest() != digest):
        raise ValueError("native_product_receipts_unavailable")
    if repair:
        if not OwnedProcesses.receipt_stopped(evidence.get("inventory")):
            raise ValueError("native_product_receipts_unconfirmed")
        evidence = {**evidence, "processes_stopped": True, "task_verified": False}
        digest = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    # Restore receipt-only protocol identity from the exact existing Core claim.
    # No call in this function creates or renews execution authority.
    gateway.execution_id, gateway.claimed = execution, True
    gateway.review_digest = order["review_digest"]
    worker = SimpleNamespace(last=dict(local))
    def progress(state, **values):
        worker.last.update(state=state, **values)
        save(worker.last)
    worker.progress = progress
    post_pending(worker, gateway)
    if worker.last.get("pending_activity"):
        raise ValueError("native_product_receipts_unconfirmed")
    if any(not row.get("posted") for row in worker.last.get("iteration_receipts", [])):
        raise ValueError("native_product_receipts_unconfirmed")
    if worker.last.get("plan") and not worker.last.get("plan_posted"):
        raise ValueError("native_product_receipts_unconfirmed")
    deploy = worker.last.get("deploy_receipt")
    if deploy and not worker.last.get("deploy_receipt_posted"):
        gateway.deploy_receipt(deploy)
        progress(worker.last["state"], deploy_receipt_posted=True)
    settled = gateway.stop_receipt(digest, True, task_verified=evidence.get("task_verified") is True)
    if repair:
        if settled.get("state") != "stopped":
            raise ValueError("native_product_receipts_unconfirmed")
        worker.last.update(evidence=evidence, evidence_digest=digest, task_verified=False)
    progress("stopped", credits_reconciled=settled.get("state") == "stopped")
    return worker.last

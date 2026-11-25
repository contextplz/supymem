"""Close known managed outcomes only after both task purposes and costs settle."""
from __future__ import annotations

import json
from sqlalchemy import select

from app.core.fsm import advance
from app.credits import pools
from app.credits._txn import naive_utc
from app.db.supymem_models import BuildModelCall, PlanTask, PlanMilestone, QaRun
from . import lifecycle
from .model_proxy import BuildModelError, _response, _settle, digest


def finish(session, ctx, spec):
    if spec.get("lifecycle_version") == 3:
        from app.build.subscription_static_controller import finish as source_finish
        return source_finish(session, ctx, spec)
    from .execution import _event
    if not lifecycle.managed(spec) or not lifecycle.cleanup_complete(spec):
        return False
    spec["cleanup_state"] = "complete"
    spec["builder_admission_closed_at"] = spec.get("builder_admission_closed_at") or naive_utc().isoformat()
    ctx.run.spec = json.dumps(spec, sort_keys=True)
    rows = session.scalars(select(BuildModelCall).where(BuildModelCall.build_run_id == ctx.run.id).order_by(BuildModelCall.sequence).with_for_update()).all()
    for row in rows:
        if row.state == "allocated":
            pools.release_child(session, ctx, reservation_id=row.reservation_id, no_external_call=True)
            row.state, row.error_code = "released", "build_closed_before_model_call"
        elif row.state == "responded":
            _response(row)  # Require the stored encrypted response's integrity.
            _settle(session, ctx, row)
    spec = json.loads(ctx.run.spec)
    if any(row.state in {"calling", "uncertain", "responded"} for row in rows):
        spec["phase"] = spec["lifecycle"]["phase"] = "usage_reconciliation_required"
        ctx.run.spec = json.dumps(spec, sort_keys=True)
        return False
    from app.db.models import CreditReservation
    if session.scalar(select(CreditReservation.id).where(CreditReservation.parent_id == ctx.pool.id,
                                                       CreditReservation.status.in_(pools.OPEN_CHILD)).limit(1)):
        spec["phase"] = spec["lifecycle"]["phase"] = "usage_reconciliation_required"
        ctx.run.spec = json.dumps(spec, sort_keys=True)
        return False
    qa = session.get(QaRun, spec["lifecycle"].get("qa_id")) if spec["lifecycle"].get("qa_id") else None
    receipt = json.loads(qa.receipt_json) if qa else {}
    from .qa_receipts import serialize
    qa_verified = bool(qa and serialize(qa)["verified_report"])
    if qa and (qa.build_run_id, qa.workspace_id, qa.venture_id, qa.app_id, qa.generation, qa.source_artifact_id) != (
            ctx.run.id, ctx.run.workspace_id, ctx.run.venture_id, ctx.run.app_id, spec["generation"], ctx.run.artifact_id):
        raise BuildModelError("qa_receipt_scope_invalid")
    normal = spec["lifecycle"]["builder"].get("normal_stop_intent")
    if normal:
        expected_calls = [{"id": row.id, "response_digest": row.response_digest, "consumed_event_id": row.consumed_event_id} for row in rows]
        if (not rows or any(row.state != "settled" or row.error_code or not row.consumed_event_id for row in rows)
                or normal.get("source_artifact_id") != ctx.run.artifact_id or normal.get("source_receipt_digest") != digest(spec.get("artifact_capture"))
                or normal.get("generation") != spec["generation"] or normal.get("command_id") != spec["managed_command"]["id"]
                or normal.get("command_digest") != spec["managed_command"]["body_digest"]
                or normal.get("final_sdk", {}).get("calls") != expected_calls
                or normal.get("final_sdk", {}).get("cursor") != spec.get("agent_cursor")):
            raise BuildModelError("build_final_receipt_changed")
    success = bool(normal and spec.get("artifact_verified") and qa_verified and receipt.get("outcome") == "passed"
                   and not spec.get("credentials_revoked") and not ctx.run.error_code)
    if success:
        # Neither a passed report nor STOPPED alone is complete execution.
        required = {"builder", "qa"}
        if any(not spec["lifecycle"].get(purpose, {}).get("settlement_receipt") for purpose in required):
            raise BuildModelError("build_settlement_receipt_missing")
        spec.update(execution_verified=True, qa_state="passed", phase="awaiting_founder_review")
        spec["lifecycle"]["review"] = {"status": "pending", "source_artifact_id": ctx.run.artifact_id,
            "report_digest": qa.report_digest, "qa_id": qa.id, "contract_digest": qa.contract_digest}
        advance(session, ctx.run, "succeeded", actor="build", reason="Immutable source, isolated QA and task usage verified")
    else:
        spec.update(execution_verified=False, qa_state=(receipt.get("outcome") if qa_verified else "not_run"), phase="cancelled" if not ctx.run.error_code else "failed")
        target = "cancelled" if not ctx.run.error_code else "failed"
        if ctx.run.state != target:
            advance(session, ctx.run, target, actor="build")
    spec["lifecycle"]["phase"] = spec["phase"]
    spec["lifecycle"]["completion"] = {"source_artifact_id": ctx.run.artifact_id,
        "qa_id": qa.id if qa else None, "verified_report": qa_verified, "verified_execution": success,
        "report_complete": receipt.get("report_complete"),
        "builder_receipt_digest": digest(spec["lifecycle"]["builder"]), "qa_receipt_digest": digest(spec["lifecycle"]["qa"])}
    task = session.get(PlanTask, ctx.run.plan_task_id)
    revision = bool(spec["inputs"].get("revision"))
    if (not task or (task.workspace_id, task.venture_id) != (ctx.run.workspace_id, ctx.run.venture_id)
            or (not revision and task.build_run_id != ctx.run.id)):
        raise BuildModelError("build_plan_task_binding_invalid")
    if not revision:
        if success and task.state == "running":
            advance(session, task, "qa", actor="build")
            advance(session, task, "waiting_for_founder", actor="build")
        elif not success and task.state in {"running", "qa"}:
            advance(session, task, "failed", actor="build")
        if not success:
            milestone = session.get(PlanMilestone, task.milestone_id)
            if milestone and milestone.state in {"running", "qa", "review"}:
                advance(session, milestone, "failed", actor="build")
        if success:
            milestone = session.get(PlanMilestone, task.milestone_id)
            states = list(session.scalars(select(PlanTask.state).where(PlanTask.milestone_id == task.milestone_id)))
            if states and all(state in {"waiting_for_founder", "done"} for state in states):
                if milestone.state == "running":
                    advance(session, milestone, "qa", actor="build")
                if milestone.state == "qa":
                    advance(session, milestone, "review", actor="build")
    if success:
        from app.chat.milestone_hooks import build_ready
        build_ready(session, ctx.run, qa, revision=revision)
    elif ctx.run.state == "failed":
        from app.chat.milestone_hooks import build_ended
        build_ended(session, ctx.run)
    if qa and qa_verified:
        qa.state = "passed" if success else "failed"
        receipt["completion"] = {"build_run_id": ctx.run.id, "source_artifact_id": ctx.run.artifact_id,
            "report_digest": qa.report_digest, "verified_execution": success, "generation": spec["generation"]}
        qa.receipt_json = json.dumps(receipt, sort_keys=True)
    ctx.run.finished_at = naive_utc()
    ctx.run.spec = json.dumps(spec, sort_keys=True)
    pools.close_pool(session, ctx)
    _event(session, ctx.run, "state", {"state": ctx.run.state, "phase": spec["phase"],
        "message": "Static landing checks passed; founder review is ready." if success else "Build ended; review its status, recorded usage, and any available evidence."})
    from app.analytics.error_events import closed_code, log_error_event
    from app.analytics.events import emit_in_session
    emit_in_session(session, "build.finished", event_key=f"build:{ctx.run.id}", workspace_id=ctx.run.workspace_id,
                    venture_id=ctx.run.venture_id, props={"feature": "builds", "outcome": closed_code(ctx.run.state),
                    "success": bool(success), "error_code": closed_code(ctx.run.error_code, "none")})
    if not success:
        log_error_event("worker", feature="builds", code=closed_code(ctx.run.error_code, "build_not_successful"),
                        venture_id=ctx.run.venture_id, kind="build.run")
    return True

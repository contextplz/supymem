"""Authorized build intent, reviewed budget, durable state and safe founder reads."""
from __future__ import annotations

import hashlib
import json
from datetime import timedelta

from sqlalchemy import select

from app.builder import budgets
from app.builder.access import authorize
from app.builder.model_proxy import digest
from app.core.fsm import advance
from app.core.local_mode import is_local
from app.credits import pools
from app.credits._txn import naive_utc, serialize_writes
from app.db.models import CreditAccount, CreditReservation, Job, Venture
from app.db.supymem_models import BuildArtifact, BuildEvent, BuildRun, ExecutionPlan, FounderApp, FounderRequest, PlanMilestone, PlanTask
from app.state import db
from core.ids import new_id


class BuildError(ValueError):
    def __init__(self, code, *, status=409, reasons=None):
        self.code, self.status, self.reasons = code, status, reasons or []
        super().__init__(code)


def _app(session, app_id, actor, mutate=False):
    app = session.get(FounderApp, app_id)
    if not app or app.deleted_at:
        raise KeyError("app")
    authorize(session, workspace_id=app.workspace_id, venture_id=app.venture_id, actor=actor, mutate=mutate)
    return app


def _account_id(session, app):
    from app.credits.accounts import account_for_venture
    account = account_for_venture(app.venture_id, session=session)
    return account.id if account else None


def _latest_founder_change_request(session, app, plan_task_id, *, active_run_id=None):
    """Carry the founder's latest rejected milestone review into its next build.

    A closed review is authoritative task feedback; an unfinished or unrelated
    run is not. The bounded text is included in the quoted input digest so it
    cannot change silently between estimate, admission and isolated launch.
    """
    rows = session.scalars(select(BuildRun).where(
        BuildRun.app_id == app.id,
        BuildRun.workspace_id == app.workspace_id,
        BuildRun.venture_id == app.venture_id,
        BuildRun.plan_task_id == plan_task_id,
        BuildRun.runtime_version == 1,
        BuildRun.closed_at.is_not(None),
        BuildRun.id != active_run_id,
    ).order_by(BuildRun.created_at.desc(), BuildRun.id.desc()).limit(8)).all()
    for row in rows:
        try:
            review = json.loads(row.spec).get("lifecycle", {}).get("review", {})
        except (TypeError, ValueError):
            continue
        feedback = review.get("feedback")
        if (review.get("decision") == "reject" and review.get("status") == "rejected"
                and isinstance(feedback, str) and feedback.strip()):
            return {"build_run_id": row.id, "feedback": feedback[:2000],
                    "source_artifact_id": row.artifact_id}
        if review.get("decision") == "accept":
            break
    return None


def _inputs(session, app, plan_id, plan_task_id, source_artifact_id, *, active_run_id=None, source_import=False):
    from app.planning.compiler import _current
    plan = session.get(ExecutionPlan, plan_id)
    if not plan or (plan.workspace_id, plan.venture_id) != (app.workspace_id, app.venture_id):
        raise KeyError("plan")
    if plan.state not in {"approved", "executing"} or not plan.approved_at or not plan.approved_by:
        raise BuildError("build_plan_approval_required")
    doc = json.loads(plan.structured or "{}")
    if doc.get("method") != "metered_foundry_plan":
        raise BuildError("build_plan_provenance_unverified")
    live, stale = _current(plan, doc, session, include_builder_runtime=not source_import)
    if stale:
        reasons = []
        if source_import:
            if live is None:
                reasons = ["build_review_check_unavailable"]
            else:
                from app.builder.review_readiness import plan_input_reason
                reasons = [plan_input_reason(doc, live)]
        raise BuildError("build_plan_inputs_changed", reasons=reasons)
    if session.scalar(select(ExecutionPlan.id).where(ExecutionPlan.venture_id == app.venture_id,
        ExecutionPlan.version > plan.version, ExecutionPlan.state.in_(["approved", "founder_review"])).limit(1)):
        raise BuildError("build_plan_superseded")
    selected, milestone = None, None
    for m in doc.get("proposal", {}).get("milestones", []):
        for task in m["tasks"]:
            if doc["entity_ids"]["tasks"].get(m["key"] + ":" + task["key"]) == plan_task_id:
                selected, milestone = task, m
    model = session.get(PlanTask, plan_task_id)
    if selected is None or model is None or (model.workspace_id, model.venture_id) != (app.workspace_id, app.venture_id):
        raise KeyError("plan_task")
    expected_mid = doc["entity_ids"]["milestones"].get(milestone["key"])
    if model.milestone_id != expected_mid:
        raise BuildError("build_plan_task_binding_invalid")
    if selected["implementation"] == "founder_action":
        raise BuildError("build_task_requires_founder_action")
    if active_run_id is None:
        # A terminal build must not strand its approved plan task forever.  A
        # failed task is executable again only when its exact bound run is
        # durably closed; this prevents a second estimate while cleanup or
        # usage reconciliation is still in progress.  ``pending`` can also be
        # bound to a closed run when a queued build was cancelled before the
        # sandbox started.
        task_ready = model.state == "pending"
        if model.build_run_id:
            prior = session.get(BuildRun, model.build_run_id)
            prior_closed = bool(prior and prior.closed_at and prior.plan_task_id == model.id
                and (prior.workspace_id, prior.venture_id, prior.app_id) == (app.workspace_id, app.venture_id, app.id)
                and prior.state in {"succeeded", "rejected", "failed", "cancelled"})
            # Repair releases that closed a failed run before they advanced the
            # bound plan rows.  The exact closed run is the recovery receipt;
            # an active, foreign or successful run can never take this path.
            if prior_closed and prior.state in {"rejected", "failed", "cancelled"} and model.state in {"running", "qa"}:
                advance(session, model, "failed", actor="build", reason="Closed failed build reconciled")
                milestone_row = session.get(PlanMilestone, model.milestone_id)
                if milestone_row and milestone_row.state in {"running", "qa"}:
                    advance(session, milestone_row, "failed", actor="build", reason="Closed failed build reconciled")
            if model.state in {"failed", "pending", "ready"} and prior_closed:
                task_ready = True
            elif model.state == "failed" or not prior_closed:
                task_ready = False
        elif model.state == "failed":
            task_ready = False
    else:
        active = session.get(BuildRun, active_run_id)
        source_review = bool(active and active.trigger in {"coding_source_import", "pagebuilder_source_qa"}
            and (active.app_id, active.workspace_id, active.venture_id, active.plan_task_id)
                == (app.id, app.workspace_id, app.venture_id, model.id)
            and json.loads(active.spec or "{}").get("lifecycle_version") == 3)
        allowed = {"pending", "ready", "running", "qa"}
        if source_review:
            allowed.update({"waiting_for_founder", "done"})
        task_ready = model.build_run_id == active_run_id and model.state in allowed
    native_static_source = bool(source_import and milestone["kind"] == "landing"
        and selected["capability_id"] == "coding.subscription_static")
    if (selected["capability_id"] != "build.run" and not native_static_source) or not milestone["included"] or not task_ready:
        raise BuildError("build_task_not_executable")
    # Initial managed profile supports newly generated code. Incorporating code
    # requires an independently verified source/license manifest first.
    if selected["implementation"] != "new_code":
        raise BuildError("build_reuse_manifest_required")
    # A first-party core product build is an isolated new-code operation with
    # an empty source tree.  Earlier plan milestones describe business order
    # (landing, discovery, outreach), but their output is not an input to this
    # build.  Let the founder build ahead without marking those milestones
    # complete; their own actions and evidence gates remain pending.  Other
    # artifact profiles still honor the reviewed milestone order.
    if milestone["kind"] != "core":
        for key in milestone.get("depends_on", []):
            dep = session.get(PlanMilestone, doc["entity_ids"]["milestones"].get(key))
            if not dep or dep.state != "done":
                raise BuildError("build_task_dependencies_incomplete")
    prior_review = _latest_founder_change_request(session, app, plan_task_id,
                                                   active_run_id=active_run_id)
    # A rejected, verified product is the repair base for the same approved
    # task. Rebuilding from an empty tree discarded fixes on every retry.
    if (not source_artifact_id and milestone["kind"] == "core" and prior_review
            and prior_review.get("source_artifact_id")):
        source_artifact_id = prior_review["source_artifact_id"]
    source = {"kind": "empty", "sha256": digest({"files": []})}
    if source_artifact_id:
        artifact = session.get(BuildArtifact, source_artifact_id)
        origin = session.get(BuildRun, artifact.build_run_id) if artifact else None
        if not artifact or artifact.app_id != app.id or artifact.workspace_id != app.workspace_id:
            raise KeyError("source_artifact")
        prior_spec = json.loads(origin.spec) if origin else {}
        capture = prior_spec.get("artifact_capture", {})
        if (not origin or (origin.app_id, origin.workspace_id, origin.venture_id) != (app.id, app.workspace_id, app.venture_id)
                or origin.runtime_version != 1 or prior_spec.get("artifact_verified") is not True
                or prior_spec.get("artifact_profile") != ("founder_product_v1" if milestone["kind"] == "core" else "static_landing_v1")
                or artifact.kind != "source_zip" or capture.get("state") != "stored"
                or capture.get("artifact_id") != artifact.id or capture.get("sha256") != artifact.sha256
                or capture.get("size_bytes") != artifact.size_bytes or capture.get("key") != artifact.s3_key):
            raise BuildError("build_source_artifact_unverified")
        from core.artifact_snapshot import manifest_digest, validate_manifest
        try:
            validate_manifest(capture.get("manifest"))
        except ValueError as exc:
            raise BuildError("build_source_artifact_unverified") from exc
        if manifest_digest(capture["manifest"]) != capture.get("manifest_digest"):
            raise BuildError("build_source_artifact_unverified")
        source = {"kind": "artifact", "id": artifact.id, "sha256": artifact.sha256, "size_bytes": artifact.size_bytes}
    elif app.repo_id:
        raise BuildError("build_source_artifact_required")
    product_context = {"revision_id": plan.revision_id, "idea": live["context"]["idea"],
        "plan_summary": plan.summary, "first_paid_user_goal": plan.goal_first_paid_user,
        "product_name": app.name}
    memory = live["context"].get("institutional_memory")
    if memory and (memory["statements"] or memory["omitted_count"]):
        product_context["institutional_memory"] = memory
    if prior_review:
        product_context["prior_founder_review"] = prior_review
    if len(json.dumps(product_context).encode()) > 20000:
        raise BuildError("build_product_context_too_large")
    return {"plan_id": plan.id, "plan_task_id": plan_task_id, "plan_version": plan.version,
        "approval": {"by": plan.approved_by, "at": plan.approved_at.isoformat()},
        "dependency_digest": live["dependency_digest"], "product_context": product_context, "task": selected,
        "milestone": {"kind": milestone["kind"], "key": milestone["key"], "title": milestone["title"]}, "source": source}


def _recheck_inputs(session, app, expected, *, actor, active_run_id=None, read_only=False):
    if expected.get("revision"):
        from app.hosting.revisions import build_inputs
        from app.hosting.service import HostingError
        try:
            return build_inputs(session, app, expected["revision"]["request_id"], actor=actor, read_only=read_only)
        except HostingError as exc:
            raise BuildError(exc.code, status=exc.status) from exc
    return _inputs(session, app, expected["plan_id"], expected["plan_task_id"],
                   expected["source"].get("id"), active_run_id=active_run_id, source_import=is_local())


def estimate(app_id, *, actor, plan_id, plan_task_id, source_artifact_id=None, requested_mode="supymem"):
    if requested_mode != "supymem":
        raise BuildError("build_mode_unavailable")
    with db()() as session:
        app = _app(session, app_id, actor, mutate=True)
        inputs = _inputs(session, app, plan_id, plan_task_id, source_artifact_id, source_import=is_local())
        return _quote(session, app, actor, inputs, source_artifact_id=source_artifact_id)


def estimate_revision(app_id, *, actor, revision_request_id):
    from app.hosting.revisions import build_inputs
    from app.hosting.service import HostingError
    with db()() as session:
        app = _app(session, app_id, actor, mutate=True)
        try:
            inputs = build_inputs(session, app, revision_request_id, actor=actor, read_only=is_local())
        except HostingError as exc:
            raise BuildError(exc.code, status=exc.status) from exc
        return _quote(session, app, actor, inputs, source_artifact_id=inputs["source"]["id"],
                      revision_request_id=revision_request_id)


def _quote(session, app, actor, inputs, *, source_artifact_id=None, revision_request_id=None):
    if is_local():
        from fastapi import HTTPException

        try:
            local_tool(app.venture_id, actor)
            ready, next_step = True, "Ask me to build your saved scope with your connected coding tool."
        except HTTPException as exc:
            if not isinstance(exc.detail, dict) or exc.detail.get("code") != "build_tool_needs_setup":
                raise
            ready, next_step = False, exc.detail["message"]
        result = {"quote_id": new_id("bq"), "input_digest": digest({"inputs": inputs, "mode": "connected_tool"}),
                  "mode": "connected_tool", "ready": ready, "reasons": [] if ready else [next_step],
                  "next_step": next_step}
        session.add(FounderRequest(id=result["quote_id"], workspace_id=app.workspace_id,
            venture_id=app.venture_id, source="build_quote", related_id=app.id, outcome="pending",
            created_at=naive_utc(), text=json.dumps({"actor": actor, "quote": result, "inputs": inputs,
                "source_artifact_id": source_artifact_id, "revision_request_id": revision_request_id}, sort_keys=True)))
        session.commit()
        return result
    policy = budgets.runtime_quote(app.workspace_id, product=inputs["milestone"]["kind"] != "landing")
    local = is_local()
    account_id = None if local else _account_id(session, app)
    account = session.get(CreditAccount, account_id) if account_id else None
    available = account.granted_micro - account.spent_micro - account.reserved_micro if account else None
    reasons = list(policy["reasons"])
    if not local and not account:
        reasons.append("build_credit_account_missing")
    elif not local and account.status != "active":
        reasons.append("build_credit_account_suspended")
    ceiling = policy.get("ceiling_micro")
    parts = [policy.get(k) for k in ("model_ceiling_micro", "compute_ceiling_micro", "artifact_ceiling_micro")]
    from . import lifecycle, expectations
    is_v2 = policy.get("lifecycle_version") == 2
    artifact_profile = "static_landing_v1" if inputs["milestone"]["kind"] == "landing" else "founder_product_v1"
    priced = all(type(value) is int and value >= (0 if local else 1) for value in parts) and ceiling == sum(parts)
    review = None
    if is_v2:
        try:
            lifecycle.validate_runtime(policy)
            priced = True
        except ValueError:
            priced = False
        if artifact_profile == "static_landing_v1":
            try:
                review = expectations.founder_view(inputs["task"])
            except ValueError:
                reasons.append("build_static_expectations_required")
    if not priced:
        reasons.append("build_budget_unknown")
    elif available is not None and ceiling > available:
        reasons.append("insufficient_credits")
    quote_id = new_id("bq")
    now = naive_utc()
    result = {"quote_id": quote_id, "input_digest": digest({"inputs": inputs, "runtime": policy}),
        "expires_at": (now + timedelta(minutes=5)).isoformat() + "Z", "available_micro": available,
        "ceiling_micro": ceiling, "model_ceiling_micro": parts[0], "compute_ceiling_micro": parts[1],
        "artifact_ceiling_micro": parts[2], "model": policy["model"], "runtime_policy": policy["runtime_policy"],
        "ready": bool(policy["ready"] and not reasons), "reasons": sorted(set(reasons))}
    if is_v2:
        result.update(lifecycle_version=2, builder_compute_ceiling_micro=policy["builder_compute_ceiling_micro"],
                      qa_compute_ceiling_micro=policy["qa_compute_ceiling_micro"], included_overhead=policy["included_overhead"],
                      expectations=review, artifact_profile=artifact_profile)
    saved = {"quote": result, "inputs": inputs, "runtime": policy, "actor": actor,
             "source_artifact_id": source_artifact_id, "revision_request_id": revision_request_id,
             "account_id": account_id}
    session.add(FounderRequest(id=quote_id, workspace_id=app.workspace_id, venture_id=app.venture_id,
        source="build_quote", related_id=app.id, text=json.dumps(saved, sort_keys=True),
        classification="budget_review", outcome="pending", created_at=now))
    session.commit()
    if local:
        return {key: value for key, value in result.items() if key not in {
            "available_micro", "ceiling_micro", "model_ceiling_micro", "compute_ceiling_micro",
            "artifact_ceiling_micro", "builder_compute_ceiling_micro", "qa_compute_ceiling_micro"}} | {
            "estimated_cost_usd_micro": parts[0]}
    return result


def _event(session, run, kind, payload):
    run.event_seq += 1
    session.add(BuildEvent(build_run_id=run.id, seq=run.event_seq, ts=naive_utc(), kind=kind,
                           payload=json.dumps(payload, sort_keys=True)))


def local_caller(actor):
    from app.admin.authn import CallerContext

    return CallerContext(sub=actor, email="", role="user", path="", method="POST", exp=0, nonce="")


def local_tool(venture_id, actor):
    from fastapi import HTTPException
    from app.builder.ctl import local_tool as connected_tool

    try:
        return connected_tool(venture_id, caller=local_caller(actor))
    except HTTPException as exc:
        if isinstance(exc.detail, dict) and exc.detail.get("code") == "build_tool_needs_setup":
            raise HTTPException(exc.status_code, detail={**exc.detail, "message":
                "Open Settings > Keys and connections > Coding tools, connect your coding tool and allow me to use it, then retry your build."}) from None
        raise


def request_local(app_id, *, actor, instruction, plan_task_id=None, idempotency_key=None):
    from fastapi import HTTPException
    from app.secrets.detect import looks_like_secret

    if not instruction.strip():
        raise HTTPException(422, detail={"code": "build_request_required", "message":
            "Describe what you want built in your venture chat, then send your request."})
    if looks_like_secret(instruction):
        raise HTTPException(409, detail={"code": "build_request_contains_secret", "message":
            "Remove credentials from your request. Save keys in Settings > Keys and connections, then retry."})
    if idempotency_key is not None and not 1 <= len(idempotency_key) <= 200:
        raise BuildError("build_idempotency_key_required", status=400)
    key = hashlib.sha256((actor + ":" + idempotency_key).encode()).hexdigest() if idempotency_key else None
    body_digest = digest({"instruction": instruction, "plan_task_id": plan_task_id})
    with db()() as session:
        serialize_writes(session, key="build:" + app_id)
        app = _app(session, app_id, actor, mutate=True)
        prior = session.scalar(select(BuildRun).where(BuildRun.app_id == app.id,
            BuildRun.idempotency_key_hash == key)) if key else None
        if prior:
            if prior.request_digest != body_digest:
                raise BuildError("build_idempotency_conflict")
            run_id = prior.id
        else:
            local_tool(app.venture_id, actor)
            inputs = None
            if plan_task_id:
                task = session.get(PlanTask, plan_task_id)
                milestone = session.get(PlanMilestone, task.milestone_id) if task else None
                if (not milestone or (task.workspace_id, task.venture_id) != (app.workspace_id, app.venture_id)):
                    raise KeyError("plan_task")
                inputs = _inputs(session, app, milestone.plan_id, plan_task_id, None, source_import=True)
            run_id = new_id("bld")
            session.add(BuildRun(id=run_id, app_id=app.id, workspace_id=app.workspace_id, venture_id=app.venture_id,
                plan_task_id=plan_task_id, trigger="founder_approved", agent="connected_tool", state="queued",
                runtime_version=0, idempotency_key_hash=key, request_digest=body_digest, event_seq=0, created_at=naive_utc(),
                spec=json.dumps({"actor": actor, "instruction": instruction, "inputs": inputs, "mode": "connected_tool"})))
            session.commit()
    run_local({"build_run_id": run_id})
    with db()() as session:
        return _accepted(session, session.get(BuildRun, run_id))


def _start_local(app_id, *, actor, quote_id, input_digest, approve_budget_micro, idempotency_key):
    if not idempotency_key or not 1 <= len(idempotency_key) <= 200:
        raise BuildError("build_idempotency_key_required", status=400)
    key = hashlib.sha256((actor + ":" + idempotency_key).encode()).hexdigest()
    body_digest = digest({"quote_id": quote_id, "input_digest": input_digest, "approve_budget_micro": approve_budget_micro})
    with db()() as session:
        serialize_writes(session, key="build:" + app_id)
        app = _app(session, app_id, actor, mutate=True)
        prior = session.scalar(select(BuildRun).where(BuildRun.app_id == app.id, BuildRun.idempotency_key_hash == key))
        if prior:
            if prior.request_digest != body_digest:
                raise BuildError("build_idempotency_conflict")
            run_id = prior.id
        else:
            quote = session.get(FounderRequest, quote_id)
            if (not quote or quote.source != "build_quote" or quote.related_id != app.id
                    or (quote.workspace_id, quote.venture_id) != (app.workspace_id, app.venture_id)):
                raise KeyError("quote")
            saved = json.loads(quote.text)
            if saved["actor"] != actor:
                raise KeyError("quote")
            if quote.outcome != "pending":
                raise BuildError("build_quote_consumed")
            if naive_utc(quote.created_at) + timedelta(minutes=5) <= naive_utc():
                raise BuildError("build_quote_expired")
            if input_digest != saved["quote"]["input_digest"]:
                raise BuildError("build_quote_inputs_changed")
            local_tool(app.venture_id, actor)
            current = _recheck_inputs(session, app, saved["inputs"], actor=actor, read_only=True)
            if digest(current) != digest(saved["inputs"]):
                raise BuildError("build_quote_inputs_changed")
            run_id = new_id("bld")
            session.add(BuildRun(id=run_id, app_id=app.id, workspace_id=app.workspace_id, venture_id=app.venture_id,
                plan_task_id=current.get("plan_task_id"), trigger="founder_approved", agent="connected_tool", state="queued",
                runtime_version=0, idempotency_key_hash=key, request_digest=body_digest, event_seq=0, created_at=naive_utc(),
                spec=json.dumps({"actor": actor, "inputs": current, "mode": "connected_tool", "quote_id": quote_id})))
            quote.outcome, quote.resolved_at = "consumed", naive_utc()
            session.commit()
    run_local({"build_run_id": run_id})
    with db()() as session:
        return _accepted(session, session.get(BuildRun, run_id))


def run_local(payload):
    from app.builder.ctl import request_local_build
    from app.core.jobs import FatalError, LOCAL_MANAGED_BUILD_CODE

    with db()() as session:
        run = session.get(BuildRun, payload["build_run_id"])
        if run is None:
            raise KeyError("build")
        spec = json.loads(run.spec or "{}")
        actor = spec.get("actor") or payload.get("_actor_subject")
        if not actor:
            from app.core.local_owner import current_owner
            actor = current_owner().id
        app = _app(session, run.app_id, actor, mutate=True)
        if ((run.workspace_id, run.venture_id) != (app.workspace_id, app.venture_id)
                or any(payload.get(field) not in (None, getattr(run, field)) for field in ("workspace_id", "venture_id"))):
            raise KeyError("build")
        if spec.get("device_work_order_id") or run.closed_at:
            return
        if run.runtime_version == 1 or run.sandbox_ref or run.reservation_id:
            raise FatalError(LOCAL_MANAGED_BUILD_CODE)
        if spec.get("cancel_requested"):
            advance(session, run, "cancelled", actor=actor, reason="founder_cancelled")
            run.finished_at = run.closed_at = naive_utc()
            session.commit()
            return
        if run.trigger != "founder_approved":
            raise FatalError("Open your venture chat and ask for the build you want; no new coding work has started.")
        provider = local_tool(app.venture_id, actor)
        inputs = spec.get("inputs")
        repair = bool(inputs and inputs.get("source", {}).get("kind") == "artifact")
        if inputs:
            current = _recheck_inputs(session, app, inputs, actor=actor, active_run_id=run.id if run.runtime_version == 1 else None,
                                      read_only=True)
            if digest(inputs) != digest(current):
                raise BuildError("build_quote_inputs_changed")
            task = current["task"]
            context = current.get("product_context", {})
            brief = "Build only this saved scope. Do not publish, contact anyone or purchase anything.\n" + json.dumps({
                "plan_task_id": current.get("plan_task_id"), "title": task.get("title"),
                "description": task.get("description"), "acceptance": task.get("acceptance"),
                "source": current["source"], "revision": current.get("revision"),
                "requested_change": spec.get("instruction"),
                "revision_instruction": context.get("revision_instruction"),
                "prior_founder_review": context.get("prior_founder_review")}, sort_keys=True)
            task_id = current.get("plan_task_id") if repair or current["milestone"]["kind"] == "landing" else None
        else:
            brief = spec.get("instruction") or spec.get("prompt") or "Build the app from my saved idea and approved plan."
            task_id = run.plan_task_id
        venture_id, run_id = run.venture_id, run.id
    order = request_local_build(venture_id, brief, caller=local_caller(actor), provider=provider,
                                repair=repair, plan_task_id=task_id)
    with db()() as session:
        run = session.get(BuildRun, run_id)
        spec = json.loads(run.spec or "{}")
        spec.update(mode="connected_tool", actor=actor, device_work_order_id=order["id"])
        run.spec, run.runtime_version = json.dumps(spec, sort_keys=True), 0
        task = session.get(PlanTask, run.plan_task_id) if run.plan_task_id else None
        if task and task.build_run_id == run.id:
            task.build_run_id = None
        cancelled = spec.get("cancel_requested") or run.closed_at is not None
        session.commit()
    if cancelled:
        from app.build import device_orders
        device_orders.emergency_stop(venture_id, local_caller(actor), order["id"])


def watch_local(payload):
    from app.build import device_orders
    from app.core.jobs import FatalError, LOCAL_MANAGED_BUILD_CODE

    with db()() as session:
        run = session.get(BuildRun, payload["build_run_id"])
        if run is None:
            raise KeyError("build")
        spec = json.loads(run.spec or "{}")
        if spec.get("device_work_order_id"):
            _app(session, run.app_id, spec["actor"])
            if any(payload.get(field) not in (None, getattr(run, field)) for field in ("workspace_id", "venture_id")):
                raise KeyError("build")
            return device_orders.retrieve(run.venture_id, local_caller(spec["actor"]), spec["device_work_order_id"])
        if (run.runtime_version == 1 or run.sandbox_ref or run.reservation_id
                or run.trigger in {"coding_source_import", "pagebuilder_source_qa", "subscription_product_diagnostic"}):
            raise FatalError(LOCAL_MANAGED_BUILD_CODE)
    return run_local(payload)


def start(app_id, *, actor, quote_id, input_digest, approve_budget_micro=None, idempotency_key):
    if is_local():
        return _start_local(app_id, actor=actor, quote_id=quote_id, input_digest=input_digest,
                            approve_budget_micro=approve_budget_micro, idempotency_key=idempotency_key)
    if not idempotency_key or not 1 <= len(idempotency_key) <= 200:
        raise BuildError("build_idempotency_key_required", status=400)
    key = hashlib.sha256((actor + ":" + idempotency_key).encode()).hexdigest()
    body_digest = digest({"quote_id": quote_id, "input_digest": input_digest, "approve_budget_micro": approve_budget_micro})
    with db()() as session:
        if is_local():
            serialize_writes(session, key="build:" + app_id)
        app = _app(session, app_id, actor, mutate=True)
        local = is_local()
        account = None
        if not local:
            account_id = _account_id(session, app)
            if not account_id:
                raise BuildError("build_credit_account_missing", status=402)
            account = pools.lock_account(session, account_id)
        prior = session.scalar(select(BuildRun).where(BuildRun.app_id == app.id,
            BuildRun.idempotency_key_hash == key).with_for_update())
        if prior:
            if prior.request_digest != body_digest:
                raise BuildError("build_idempotency_conflict")
            return _accepted(session, prior)
        if session.scalar(select(BuildRun.id).where(BuildRun.app_id == app.id, BuildRun.runtime_version == 1, BuildRun.closed_at.is_(None))):
            raise BuildError("build_active")
        # Inventory, plan approval, and quote consumption serialize with their
        # venture mutation gate, after the canonical credit-account lock.
        session.get(Venture, app.venture_id, with_for_update=True)
        quote = session.scalar(select(FounderRequest).where(FounderRequest.id == quote_id).with_for_update())
        if not quote or quote.source != "build_quote" or quote.related_id != app.id or (quote.workspace_id, quote.venture_id) != (app.workspace_id, app.venture_id):
            raise KeyError("quote")
        saved = json.loads(quote.text)
        quoted = saved["quote"]
        if saved["actor"] != actor:
            raise KeyError("quote")
        if quote.outcome != "pending":
            raise BuildError("build_quote_consumed")
        if naive_utc(quote.created_at) + timedelta(minutes=5) <= naive_utc():
            raise BuildError("build_quote_expired")
        if input_digest != quoted["input_digest"] or (not local and (
                type(approve_budget_micro) is not int or approve_budget_micro != quoted["ceiling_micro"])):
            raise BuildError("build_budget_approval_mismatch")
        if saved.get("revision_request_id"):
            from app.hosting.revisions import build_inputs
            from app.hosting.service import HostingError
            try:
                current = build_inputs(session, app, saved["revision_request_id"], actor=actor)
            except HostingError as exc:
                raise BuildError(exc.code, status=exc.status) from exc
        else:
            current = _inputs(session, app, saved["inputs"]["plan_id"], saved["inputs"]["plan_task_id"], saved["source_artifact_id"])
        runtime = budgets.runtime_quote(app.workspace_id, product=current["milestone"]["kind"] != "landing")
        if digest({"inputs": current, "runtime": runtime}) != input_digest:
            raise BuildError("build_quote_inputs_changed")
        if not quoted["ready"] or not runtime["ready"]:
            raise BuildError("build_capability_unavailable", status=503, reasons=quoted["reasons"])
        if local:
            from app.credits.local_meter import check_limit
            check_limit(runtime["model_ceiling_micro"])
        from app.core.jobs import enqueue
        from app.tenancy import bind
        rid = new_id("bld")
        run = BuildRun(id=rid, app_id=app.id, workspace_id=app.workspace_id, venture_id=app.venture_id,
            plan_task_id=current["plan_task_id"], trigger="founder_approved", agent="openhands", state="queued",
            runtime_version=1, idempotency_key_hash=key, request_digest=body_digest, event_seq=0, created_at=naive_utc(),
            spec=json.dumps({"actor": actor, "quote_id": quote_id, "inputs": current, "runtime": runtime,
                "generation": 1, "allowed_tool_names": runtime["runtime_policy"].get("tool_names", []), "model_config": runtime["model_config"], "model_ceiling_micro": runtime["model_ceiling_micro"],
                "cleanup_state": "not_started", "execution_verified": False, "qa_state": "not_run"}, sort_keys=True))
        if runtime.get("lifecycle_version") == 2:
            from . import lifecycle, expectations
            artifact_profile = "static_landing_v1" if current["milestone"]["kind"] == "landing" else "founder_product_v1"
            if artifact_profile == "static_landing_v1":
                expectations.validate(current["task"])
            spec = json.loads(run.spec)
            spec.update(lifecycle_version=2, artifact_profile=artifact_profile,
                        lifecycle=lifecycle.initial(runtime, naive_utc()))
            run.spec = json.dumps(spec, sort_keys=True)
        session.add(run)
        if not current.get("revision"):
            task = session.get(PlanTask, current["plan_task_id"])
            if task.state == "failed":
                advance(session, task, "ready", actor="founder", reason="Founder approved a reviewed build retry")
                milestone = session.get(PlanMilestone, task.milestone_id)
                if milestone and milestone.state == "failed":
                    advance(session, milestone, "ready", actor="founder", reason="Founder approved a reviewed build retry")
            task.build_run_id = rid
        if not local:
            pools.create_pool(session, account=account, run=run, reservation_id=new_id("crv"),
                amount_micro=approve_budget_micro, expires_at=naive_utc() + timedelta(seconds=sum(runtime["deadlines"].values()) + 120 if runtime.get("lifecycle_version") == 2 else runtime["runtime_policy"]["timeout_seconds"] + 600))
        with bind(workspace_id=app.workspace_id, venture_id=app.venture_id, actor=actor):
            run.job_id = enqueue(session, "build.run", {"build_run_id": rid, "runtime_version": 1},
                workspace_id=app.workspace_id, venture_id=app.venture_id, dedupe_key="build.run:" + rid)
        quote.outcome, quote.resolved_at = "consumed", naive_utc()
        _event(session, run, "queued", {"message": "Your build is queued."} if local else {
            "message": "Reviewed build queued", "reserved_micro": approve_budget_micro})
        session.commit()
        return _accepted(session, run)


def _accepted(session, run):
    spec = json.loads(run.spec or "{}")
    if is_local() and spec.get("mode") == "connected_tool":
        from app.build import device_orders
        if not spec.get("device_work_order_id"):
            return {"build_run_id": run.id, "device_work_order_id": None, "state": run.state, "review_path": "/builds"}
        order = device_orders.retrieve(run.venture_id, local_caller(spec["actor"]), spec["device_work_order_id"])
        return {"build_run_id": run.id, "device_work_order_id": order["id"], "state": order["state"],
                "review_path": order["review_path"]}
    if is_local():
        return {"build_run_id": run.id, "job_id": run.job_id, "state": run.state,
            "estimated_cost_usd_micro": json.loads(run.spec)["runtime"]["model_ceiling_micro"]}
    usage = _serialize(session, run)["usage"]
    return {"build_run_id": run.id, "job_id": run.job_id, "state": run.state,
            "reserved_micro": usage["reserved_micro"],
            "approved_budget_micro": json.loads(run.spec)["runtime"]["ceiling_micro"]}


def _recorded_publication_required(session, run, spec):
    """Keep an owned saved obligation visible when live configuration changes.

    This is a read projection only. Publication admission still regenerates
    and checks the current contract and the complete pending receipt.
    """
    from app.build.subscription_static_completion import pending
    saved = spec.get("lifecycle", {}).get("task_completion")
    if (not isinstance(saved, dict) or saved.get("state") not in {"pending_publication", "completed"}
            or type(saved.get("schema_version")) is not int
            or spec.get("lifecycle", {}).get("review", {}).get("status") != "accepted"
            or run.trigger not in {"coding_source_import", "pagebuilder_source_qa"} or spec.get("lifecycle_version") != 3
            or spec.get("artifact_profile") != "static_landing_v1" or spec.get("inputs", {}).get("revision")):
        return False
    task = session.get(PlanTask, run.plan_task_id) if run.plan_task_id else None
    milestone = session.get(PlanMilestone, task.milestone_id) if task else None
    plan_id = spec.get("inputs", {}).get("plan_id")
    plan = session.get(ExecutionPlan, plan_id) if plan_id else None
    artifact = session.get(BuildArtifact, run.artifact_id) if run.artifact_id else None
    if (not task or (task.workspace_id, task.venture_id, task.build_run_id)
            != (run.workspace_id, run.venture_id, run.id)
            or not plan or (plan.workspace_id, plan.venture_id) != (run.workspace_id, run.venture_id)
            or not milestone or (milestone.plan_id, milestone.workspace_id, milestone.venture_id, milestone.kind)
            != (plan.id, run.workspace_id, run.venture_id, "landing")
            or not artifact or (artifact.build_run_id, artifact.app_id, artifact.workspace_id)
            != (run.id, run.app_id, run.workspace_id)):
        return False
    try:
        expected = pending(run, task, spec)
    except (KeyError, TypeError, ValueError):
        return False
    return all(saved.get(key) == value for key, value in expected.items() if key != "state")


def _serialize(session, run):
    local = is_local()
    if run.trigger == "subscription_product_diagnostic":
        if local:
            try:
                saved = json.loads(run.spec or "{}")
            except (TypeError, ValueError):
                saved = {}
            spec = saved if isinstance(saved, dict) else {}
            diagnostic = spec.get("diagnostic")
            return {
                "id": run.id, "build_run_id": run.id, "app_id": run.app_id,
                "workspace_id": run.workspace_id, "venture_id": run.venture_id,
                "plan_task_id": run.plan_task_id, "runtime_version": run.runtime_version,
                "state": run.state, "phase": diagnostic.get("phase", run.state) if isinstance(diagnostic, dict) else run.state,
                "cleanup_state": spec.get("cleanup_state", "not_run"),
                "mode": "product_diagnostic_only", "qa_state": "not_accepted", "verified": False,
                "verified_execution": False, "runtime_qa_verified": False,
                "product_accepted": False, "hosting_authorized": False,
                "error_code": run.error_code, "closed": run.closed_at is not None,
            }
        from app.build.subscription_product_diagnostic_admission import view
        return view(run)
    spec = json.loads(run.spec or "{}") if local else {}
    if local and spec.get("mode") == "connected_tool":
        accepted = _accepted(session, run)
        return {**accepted, "id": run.id, "app_id": run.app_id, "plan_task_id": run.plan_task_id,
                "mode": "connected_tool", "runtime_version": run.runtime_version, "verified_execution": False,
                "artifact_id": None, "job_id": None, "completion_status": "awaiting_checked_source"}
    managed = run.runtime_version == 1
    spec = json.loads(run.spec or "{}") if managed else {}
    from app.planning.publication_evidence import landing_build_result
    from app.build.subscription_static_completion import publication_required
    pool = session.get(CreditReservation, run.reservation_id) if not local and managed and run.reservation_id else None
    children = session.scalars(select(CreditReservation).where(CreditReservation.parent_id == pool.id)).all() if pool else []
    outstanding = sum(c.amount_micro for c in children if c.status in {"active", "open"})
    verified = bool(managed and spec.get("execution_verified"))
    charge_lines = []
    for purpose in sorted({row.purpose for row in children}):
        selected = [row for row in children if row.purpose == purpose]
        charge_lines.append({"purpose": purpose, "consumed_micro": sum(row.settled_micro or 0 for row in selected if row.status == "settled"),
                             "unresolved_micro": sum(row.amount_micro for row in selected if row.status in pools.OPEN_CHILD)})
    result = {"id": run.id, "app_id": run.app_id,
        "landing_page": landing_build_result(session, run, spec) if managed else None,
        "quote_id": spec.get("quote_id") if managed else None,
        "correction_request_id": spec.get("inputs", {}).get("revision", {}).get("request_id")
            if spec.get("inputs", {}).get("revision", {}).get("kind") == "private_build_correction" else None,
        "plan_id": spec.get("inputs", {}).get("plan_id"),
        "revision_instruction": spec.get("inputs", {}).get("product_context", {}).get("revision_instruction")
            if spec.get("inputs", {}).get("revision") else None,
        "plan_task_id": run.plan_task_id, "state": run.state if managed else "unverified",
        "legacy_state": run.state if not managed else None, "phase": spec.get("phase", run.state) if managed else "not_run",
        "runtime_version": run.runtime_version, "verified_execution": verified,
        "mode": spec.get("mode", "managed_builder"),
        "verified_source_import": bool(spec.get("lifecycle", {}).get("completion", {}).get("verified_source_import")),
        "source_custody_verified": bool(spec.get("source_import_verified")),
        "source_provider": spec.get("source_import", {}).get("identity", {}).get("scope", {}).get("provider"),
        "created_at": run.created_at.isoformat() + "Z" if run.created_at else None,
        "started_at": run.started_at.isoformat() + "Z" if run.started_at else None,
        "finished_at": run.finished_at.isoformat() + "Z" if run.finished_at else None,
        "job_id": run.job_id, "policy_summary": spec.get("runtime", {}).get("runtime_policy"),
        "usage": {"reserved_micro": (pool.remaining_micro + outstanding) if pool else None,
                  "consumed_micro": run.credits_used if managed else None, "unresolved_micro": outstanding if pool else None, "charge_lines": charge_lines},
        "artifact_id": run.artifact_id if managed and spec.get("artifact_verified") else None,
        "artifact_profile": spec.get("artifact_profile") if managed else None,
        "qa_state": spec.get("qa_state", "not_run") if managed else "not_run",
        "lifecycle_version": spec.get("lifecycle_version", 1) if managed else 0,
        "review": spec.get("lifecycle", {}).get("review"),
        "publication_required": publication_required(session, run, spec) or _recorded_publication_required(session, run, spec),
        "task_completion": spec.get("lifecycle", {}).get("task_completion"),
        "qa_run_id": spec.get("lifecycle", {}).get("qa_id"),
        "artifact_status": "stored" if managed and spec.get("artifact_verified") else "not_available",
        "completion_status": "closed" if run.closed_at else "reconciling" if spec.get("reconciliation_required") else "active" if managed else "not_run",
        "cleanup_by_purpose": {purpose: spec.get("lifecycle", {}).get(purpose, {}).get("cleanup_state", "not_started") for purpose in ("builder", "qa")} if spec.get("lifecycle_version") in {2, 3} else None,
        "included_overhead": spec.get("runtime", {}).get("included_overhead"), "error_code": run.error_code if managed else None,
        "needs_input": spec.get("needs_input"), "cleanup_state": spec.get("cleanup_state", "not_run")}
    if is_local():
        from app.credits.local_meter import snapshot
        result["usage"] = snapshot(workspace_id=run.workspace_id, venture_id=run.venture_id, run_id=run.id)
    return result


def read(run_id, *, actor):
    with db()() as session:
        run = session.get(BuildRun, run_id)
        if not run:
            raise KeyError("build")
        app = _app(session, run.app_id, actor)
        if (run.workspace_id, run.venture_id) != (app.workspace_id, app.venture_id):
            raise KeyError("build")
        from app.builder.review_readiness import read as review_readiness
        return {**_serialize(session, run), 'review_readiness': review_readiness(session, run, actor=actor)}


def listing(app_id, *, actor):
    with db()() as session:
        app = _app(session, app_id, actor)
        rows = session.scalars(select(BuildRun).where(BuildRun.app_id == app.id, BuildRun.workspace_id == app.workspace_id,
            BuildRun.venture_id == app.venture_id).order_by(BuildRun.created_at.desc(), BuildRun.id).limit(100)).all()
        from app.builder.review_readiness import read as review_readiness
        return [{**_serialize(session, row), 'review_readiness': review_readiness(session, row, actor=actor)} for row in rows]


def events(run_id, *, actor, after=0):
    summary = read(run_id, actor=actor)
    if summary["runtime_version"] != 1:
        return {"items": [], "next": max(0, after), "provenance": "legacy_unverified"}
    with db()() as session:
        rows = session.scalars(select(BuildEvent).where(BuildEvent.build_run_id == run_id, BuildEvent.seq > max(0, after),
            BuildEvent.kind.in_(["queued", "state", "cancel_requested", "usage", "needs_input", "artifact", "error", "qa_source_prepared", "qa_report_stored", "founder_review", "provider_cost_absorbed"])).order_by(BuildEvent.seq).limit(200)).all()
        items = [{"seq": r.seq, "kind": r.kind, "ts": r.ts.isoformat() + "Z", "payload": json.loads(r.payload)} for r in rows]
        return {"items": items, "next": rows[-1].seq if rows else max(0, after)}


def cancel(run_id, *, actor):
    if is_local():
        from app.build import device_orders

        with db()() as session:
            run = session.get(BuildRun, run_id)
            if run is None:
                raise KeyError("build")
            _app(session, run.app_id, actor, mutate=True)
            spec = json.loads(run.spec or "{}")
            if spec.get("device_work_order_id"):
                return device_orders.emergency_stop(run.venture_id, local_caller(actor), spec["device_work_order_id"])
            if spec.get("mode") == "connected_tool":
                if not run.closed_at:
                    spec["cancel_requested"] = True
                    advance(session, run, "cancelled", actor=actor, reason="founder_cancelled")
                    run.finished_at = run.closed_at = naive_utc()
                    run.spec = json.dumps(spec, sort_keys=True)
                    session.commit()
                return {"state": run.state, "cleanup_state": "complete"}
    with db()() as session:
        if is_local():
            serialize_writes(session, key="build:" + run_id)
        run = session.get(BuildRun, run_id)
        if not run or run.runtime_version != 1:
            raise KeyError("build")
        _app(session, run.app_id, actor, mutate=True)
        if is_local() and not run.reservation_id:
            if run.closed_at:
                return {"state": run.state, "cleanup_state": "complete"}
            session.get(Venture, run.venture_id, with_for_update=True)
            spec = json.loads(run.spec)
            if not spec.get("cancel_requested"):
                spec["cancel_requested"] = True
                job = session.get(Job, run.job_id, with_for_update=True) if run.job_id else None
                if job and job.status == "scheduled" and job.attempts == 0 and spec.get("cleanup_state") == "not_started":
                    job.status, job.finished_at = "cancelled", naive_utc()
                    advance(session, run, "cancelling", actor=actor, reason="founder_cancelled")
                    advance(session, run, "cancelled", actor=actor, reason="unleased_build_cancelled")
                    run.closed_at = run.finished_at = naive_utc()
                    spec["cleanup_state"] = "complete"
                else:
                    spec["cleanup_state"] = "pending"
                run.spec = json.dumps(spec, sort_keys=True)
                _event(session, run, "cancel_requested", {"message": "You requested cancellation. Check your build for cleanup progress."})
            session.commit()
            return {"state": run.state, "cleanup_state": spec["cleanup_state"]}
        ctx = pools.lock_pool(session, run.reservation_id)
        if ctx.run.closed_at:
            return {"state": ctx.run.state, "cleanup_state": "complete"}
        spec = json.loads(ctx.run.spec)
        if not spec.get("cancel_requested"):
            pools.mark_reconciliation(session, ctx, "founder_cancelled", actor=actor)
            spec = json.loads(ctx.run.spec)
            spec["cancel_requested"] = True
            ctx.run.spec = json.dumps(spec, sort_keys=True)
            _event(session, ctx.run, "cancel_requested", {"message": "Cancellation requested; cleanup and usage are being reconciled."})
            job = session.get(Job, ctx.run.job_id, with_for_update=True) if ctx.run.job_id else None
            if job and job.status == "scheduled" and job.attempts == 0 and spec.get("cleanup_state") == "not_started":
                # A never-leased launch has no cloud task. Same transaction stops
                # its job before releasing the pool; leased launches require the
                # trusted controller to stop/reconcile them first.
                job.status, job.finished_at = "cancelled", naive_utc()
                spec["cleanup_state"] = "complete"
                ctx.run.spec = json.dumps(spec, sort_keys=True)
                if ctx.run.state == "cancelling":
                    advance(session, ctx.run, "cancelled", actor=actor, reason="unleased_build_cancelled")
                ctx.run.finished_at = naive_utc()
                pools.close_pool(session, ctx)
            else:
                spec["cleanup_state"] = "pending"
                ctx.run.spec = json.dumps(spec, sort_keys=True)
        session.commit()
        return {"state": ctx.run.state, "cleanup_state": json.loads(ctx.run.spec)["cleanup_state"]}


def _queue_runtime_after_accept(run_id: str, actor: str):
    """Idempotently hand an accepted product build to the runtime controller."""
    if is_local():
        return None
    from app.db.supymem_models import QaRun
    from app.provisioning import runtime_intents

    with db()() as session:
        run = session.get(BuildRun, run_id)
        if run is None:
            raise KeyError("build")
        app = session.get(FounderApp, run.app_id)
        task = session.get(PlanTask, run.plan_task_id) if run.plan_task_id else None
        spec = json.loads(run.spec or "{}")
        if run.trigger == "subscription_product_diagnostic" or spec.get("lifecycle_version") == 4:
            raise BuildError("product_diagnostic_not_product_acceptance")
        review = spec.get("lifecycle", {}).get("review", {})
        if spec.get("artifact_profile") != "founder_product_v1":
            return None
        qa = session.get(QaRun, review.get("qa_id")) if review.get("qa_id") else None
        try:
            hints = json.loads(task.tool_hints) if task else None
        except (TypeError, json.JSONDecodeError):
            hints = None
        if (app is None or task is None or not isinstance(hints, list) or not hints
                or any(not isinstance(item, str) for item in hints)
                or not set(hints).issubset(runtime_intents.PRODUCT_CAPABILITIES)):
            return None
        if (qa is None or review.get("status") != "accepted" or review.get("decision") != "accept"
                or review.get("source_artifact_id") != run.artifact_id):
            raise BuildError("build_runtime_admission_not_ready")
        args = {
            "app_id": app.id, "workspace_id": app.workspace_id, "venture_id": app.venture_id,
            "actor": actor, "plan_task_id": task.id, "build_run_id": run.id,
            "source_artifact_id": run.artifact_id, "qa_run_id": qa.id,
            "source_manifest_digest": qa.source_manifest_digest,
            "qa_contract_digest": qa.contract_digest,
            "hostname": runtime_intents.product_hostname(app.id, app.slug),
        }
    admitted = runtime_intents.admit(**args)
    with db()() as session:
        from app.core.jobs import enqueue

        enqueue(session, "infra.provision",
                {"schema_version": 1, "runtime_intent_id": admitted["id"]},
                workspace_id=admitted["workspace_id"], venture_id=admitted["venture_id"],
                dedupe_key="founder-runtime-database:" + admitted["id"], max_attempts=20)
        session.commit()
    return admitted


def review(run_id, *, actor, source_artifact_id, report_digest, decision, feedback=None, effect_fence=None):
    """Review this immutable deliverable; never start another build or deploy."""
    from app.core.stages import progress
    from app.core.fsm import advance
    from app.builder.model_proxy import _locked
    from app.builder.review_readiness import require as require_review_ready
    from core.enums import VentureStage
    if decision not in {"accept", "reject"} or feedback is not None and (not isinstance(feedback, str) or len(feedback) > 2000):
        raise BuildError("build_review_invalid", status=400)
    accepted = False
    with db()() as session:
        run = session.get(BuildRun, run_id)
        if not run or run.runtime_version != 1:
            raise KeyError("build")
        _app(session, run.app_id, actor, mutate=True)
        ctx = _locked(session, run_id)
        if effect_fence is not None:
            effect_fence(session)
        spec = json.loads(ctx.run.spec)
        if ctx.run.trigger == "subscription_product_diagnostic" or spec.get("lifecycle_version") == 4:
            raise BuildError("product_diagnostic_not_product_acceptance")
        reviewed = spec.get("lifecycle", {}).get("review", {})
        wanted = {"decision": decision, "feedback": feedback, "actor": actor,
                  "source_artifact_id": source_artifact_id, "report_digest": report_digest}
        if reviewed.get("decision"):
            if any(reviewed.get(key) != value for key, value in wanted.items()):
                raise BuildError("build_review_conflict")
            accepted = reviewed.get("decision") == "accept"
            if accepted:
                require_review_ready(session, ctx.run, spec, actor=actor, repeat=True)
                venture = session.get(Venture, ctx.run.venture_id, with_for_update=True)
                venture.stage = progress(venture.stage, VentureStage.READINESS_REVIEW).value
            result = {"build_run_id": run_id, "review": reviewed}
            session.commit()
        else:
            venture = session.get(Venture, ctx.run.venture_id, with_for_update=True)
            require_review_ready(session, ctx.run, spec, actor=actor,
                                 source_artifact_id=source_artifact_id, report_digest=report_digest)
            from app.pagebuilder_admission.inputs import no_plan
            server_without_plan = no_plan(ctx.run, spec)
            task = session.get(PlanTask, ctx.run.plan_task_id) if ctx.run.plan_task_id else None
            awaiting_publication = False
            if spec["inputs"].get("revision"):
                request = session.get(FounderRequest, spec["inputs"]["revision"]["request_id"])
                if decision == "accept":
                    request.outcome = "approved"
                    request.resolved_at = naive_utc()
            elif not server_without_plan:
                from app.build.subscription_static_completion import pending, publication_required
                awaiting_publication = decision == "accept" and publication_required(session, ctx.run, spec)
                if not awaiting_publication:
                    advance(session, task, "done" if decision == "accept" else "failed", actor="founder", reason=feedback)
            reviewed.update(**wanted, review_origin="founder", status="accepted" if decision == "accept" else "rejected", reviewed_at=naive_utc().isoformat() + "Z")
            if not spec["inputs"].get("revision") and awaiting_publication:
                spec["lifecycle"]["task_completion"] = pending(ctx.run, task, spec)
                spec["phase"] = "source_accepted_pending_publication"
            if not spec["inputs"].get("revision") and not server_without_plan:
                milestone = session.get(PlanMilestone, task.milestone_id)
                if decision == "reject" and milestone.state == "review":
                    advance(session, milestone, "failed", actor="founder", reason=feedback)
                if decision == "accept" and milestone.state == "review" and not session.scalar(select(PlanTask.id).where(PlanTask.milestone_id == milestone.id, PlanTask.state != "done").limit(1)):
                    advance(session, milestone, "done", actor="founder")
            if decision == "accept":
                venture.stage = progress(venture.stage, VentureStage.READINESS_REVIEW).value
                from app.chat.milestone_hooks import build_accepted
                build_accepted(session, ctx.run, revision=bool(spec["inputs"].get("revision")))
            _event(session, ctx.run, "founder_review", {"decision": decision, "source_artifact_id": source_artifact_id,
                "report_digest": report_digest, "plan_task_id": task.id if task else None})
            ctx.run.spec = json.dumps(spec, sort_keys=True)
            session.commit()
            result = {"build_run_id": run_id, "review": reviewed}
            accepted = decision == "accept"
    if accepted:
        result["runtime_intent"] = _queue_runtime_after_accept(run_id, actor)
    return result

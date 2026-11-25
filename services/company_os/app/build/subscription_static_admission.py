"""Reviewed, idempotent admission for QA of settled subscription static source."""
from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import select

from app.build import subscription_static_inputs as inputs, subscription_static_policy as policy
from app.build.coding_source_intake import SourceIntakeError
from app.builder import execution
from app.builder.model_proxy import digest
from app.core.fsm import advance
from app.credits import pools
from app.credits._txn import naive_utc
from app.db.models import VentureDocument
from app.db.session import serialize_writes
from app.db.supymem_models import BuildRun, PlanTask
from app.state import db

QUOTE_KIND = "subscription_static_qa_quote"


def _quote_view(quote):
    runtime = quote["runtime"]
    return {"quote_id": quote["id"], "quote_digest": digest(quote), "expires_at": quote["expires_at"] + "Z",
            "source_fingerprint": quote["fingerprint"], "qa_ceiling_micro": runtime["qa_compute_ceiling_micro"],
            "model_ceiling_micro": 0, "builder_compute_ceiling_micro": 0,
            "artifact_profile": "static_landing_v1", "policy_id": policy.POLICY_ID,
            "message": "Run independent static browser QA on this exact saved source. No builder or model will run."}


def _existing(session, venture_id, order_id, caller):
    from app.build import coding_source_intake as intake
    from app.db.supymem_models import CodingSourceReceipt
    order, app_id = intake._scope(session, venture_id, order_id, caller)
    source = session.get(CodingSourceReceipt, order_id)
    if source is None or source.state != "stored":
        return None
    # Read-only idempotency recovery is permitted after this run advanced the
    # plan task. No new reservation, QA launch or acceptance occurs here.
    rows = session.scalars(select(BuildRun).where(BuildRun.app_id == app_id,
        BuildRun.workspace_id == order["workspace_id"], BuildRun.venture_id == venture_id,
        BuildRun.trigger == "coding_source_import")).all()
    for run in rows:
        frozen = json.loads(run.spec).get("source_import", {}).get("identity", {})
        scope = frozen.get("scope", {})
        captured = frozen.get("source", {})
        if (scope.get("order_id") == order_id and scope.get("actor_id") == order["actor_id"]
                and scope.get("device_id") == order["device_id"]
                and captured.get("sha256") == source.sha256
                and captured.get("manifest_digest") == source.manifest_sha256
                and captured.get("storage_receipt") == json.loads(source.receipt_json)
                and frozen.get("task_snapshot") == order.get("task_snapshot")):
            return run
    return None


def _lock_intent(session, venture_id, order_id, caller):
    from app.build import coding_source_intake as intake
    _order, app_id = intake._scope(session, venture_id, order_id, caller)
    serialize_writes(session, key="subscription-static:" + app_id)


def estimate(venture_id, order_id, caller):
    with db()() as session:
        _lock_intent(session, venture_id, order_id, caller)
        prior = _existing(session, venture_id, order_id, caller)
        if prior:
            return {"existing_run": execution._serialize(session, prior)}
        runtime = policy.configured()
        envelope, app = inputs.current(session, venture_id, order_id, caller)
        fingerprint = envelope["fingerprint"]
        serialize_writes(session, key="subscription-static:" + app.id)
        prior = session.get(BuildRun, inputs.run_id(fingerprint))
        if prior:
            return {"existing_run": execution._serialize(session, prior)}
        now = naive_utc()
        quote_id = "ssq_" + digest({"fingerprint": fingerprint, "runtime": runtime})[:48]
        row = session.get(VentureDocument, quote_id)
        if row is not None:
            quote = json.loads(row.provenance_json)
            if quote["expires_at"] > now.isoformat():
                return _quote_view(quote)
        quote = {"id": quote_id, "schema_version": 1, "fingerprint": fingerprint,
                 "identity": envelope["identity"], "inputs": envelope["inputs"], "runtime": runtime,
                 "expires_at": (now + timedelta(minutes=10)).isoformat()}
        if row is None:
            row = VentureDocument(id=quote_id, venture_id=venture_id, kind=QUOTE_KIND, title="Review static source QA budget",
                created_at=now, updated_at=now, provenance_json=json.dumps(quote, sort_keys=True))
            session.add(row)
        else:
            row.provenance_json = json.dumps(quote, sort_keys=True)
            row.updated_at = now
        session.commit()
        return _quote_view(quote)


def admit(venture_id, order_id, caller, *, quote_id, quote_digest, approve_qa_ceiling_micro=None):
    with db()() as session:
        _lock_intent(session, venture_id, order_id, caller)
        row = session.get(VentureDocument, quote_id)
        if row is None or row.venture_id != venture_id or row.kind != QUOTE_KIND:
            raise SourceIntakeError("subscription_static_quote_not_found", 404)
        quote = json.loads(row.provenance_json)
        if digest(quote) != quote_digest or quote["identity"]["scope"]["order_id"] != order_id:
            raise SourceIntakeError("subscription_static_quote_changed")
        if approve_qa_ceiling_micro is not None and (type(approve_qa_ceiling_micro) is not int
                or approve_qa_ceiling_micro != quote["runtime"]["qa_compute_ceiling_micro"]):
            raise SourceIntakeError("subscription_static_budget_approval_changed")
        prior = _existing(session, venture_id, order_id, caller)
        if prior:
            return execution._serialize(session, prior)
        envelope, app = inputs.current(session, venture_id, order_id, caller)
        serialize_writes(session, key="subscription-static:" + app.id)
        expected_id = inputs.run_id(envelope["fingerprint"])
        prior = session.get(BuildRun, expected_id)
        if prior:
            if prior.request_digest != envelope["fingerprint"]:
                raise SourceIntakeError("subscription_static_intent_conflict")
            return execution._serialize(session, prior)
        now = naive_utc()
        runtime = policy.validate(quote["runtime"])
        if (quote["expires_at"] <= now.isoformat() or envelope["fingerprint"] != quote["fingerprint"]
                or envelope["inputs"] != quote["inputs"]):
            raise SourceIntakeError("subscription_static_quote_changed")
        account_id = execution._account_id(session, app)
        if not account_id:
            raise SourceIntakeError("subscription_static_credit_account_missing")
        account = pools.lock_account(session, account_id)
        app = session.scalar(select(type(app)).where(type(app).id == app.id).with_for_update())
        open_run = session.scalar(select(BuildRun.id).where(BuildRun.app_id == app.id,
            BuildRun.runtime_version == 1, BuildRun.closed_at.is_(None)))
        if open_run:
            raise SourceIntakeError("subscription_static_app_has_open_run")
        # Recheck after acquiring account/app locks; no charge or job exists yet.
        current, _app = inputs.current(session, venture_id, order_id, caller)
        if current != envelope:
            raise SourceIntakeError("subscription_static_approved_inputs_changed")
        bounds = runtime["deadlines"]
        deadline = now + timedelta(seconds=sum(bounds.values()))
        lifecycle = {"phase": "importing_source", "queue_deadline": (now + timedelta(seconds=bounds["queue_seconds"])).isoformat(),
            "total_deadline": deadline.isoformat(), "builder": {"cleanup_state": "complete", "no_external_call": True},
            "qa": {"cleanup_state": "pending"}, "qa_required": True, "review": {"status": "not_ready"}}
        spec = {"lifecycle_version": 3, "mode": "subscription_static_source_qa", "generation": 1,
            "actor": envelope["identity"]["scope"]["actor_id"], "inputs": envelope["inputs"], "runtime": runtime,
            "artifact_profile": "static_landing_v1", "phase": "importing_source", "cleanup_state": "pending",
            "execution_verified": False, "source_import_verified": False, "qa_state": "not_run",
            "source_import": {"state": "prepared", **envelope}, "lifecycle": lifecycle}
        run = BuildRun(id=expected_id, app_id=app.id, workspace_id=app.workspace_id, venture_id=venture_id,
            plan_task_id=envelope["inputs"]["plan_task_id"], trigger="coding_source_import", agent="subscription_source_qa",
            state="syncing_artifacts", runtime_version=1, request_digest=envelope["fingerprint"],
            idempotency_key_hash=envelope["fingerprint"], created_at=now, started_at=now,
            spec=json.dumps(spec, sort_keys=True))
        session.add(run)
        session.flush()
        pools.create_pool(session, account=account, run=run, reservation_id="crv_" + envelope["fingerprint"][:48],
                          amount_micro=runtime["ceiling_micro"], expires_at=deadline)
        ctx = pools.lock_pool(session, run.reservation_id)
        hold_id = "crv_qa_" + envelope["fingerprint"][:44]
        pools.allocate(session, ctx, reservation_id=hold_id, amount_micro=runtime["qa_compute_ceiling_micro"],
                       purpose="build.compute.qa", provider="aws")
        spec["lifecycle"]["holds"] = {"qa": hold_id}
        task = session.get(PlanTask, run.plan_task_id)
        if task.state == "failed":
            predecessor_id = envelope["identity"]["task_snapshot"].get("closed_failed_build_run_id")
            if not predecessor_id or task.build_run_id != predecessor_id:
                raise SourceIntakeError("subscription_static_retry_binding_invalid")
            advance(session, task, "ready", actor="founder", reason="Founder approved fresh reviewed source QA retry")
        task.build_run_id = run.id
        from app.builder.orchestration import _followup, _progress_plan
        _progress_plan(session, ctx, spec)
        _followup(session, ctx, spec, delay=0)
        execution._event(session, run, "subscription_source_qa_admitted", {
            "source_fingerprint": envelope["fingerprint"], "order_id": order_id,
            "provider": envelope["identity"]["scope"]["provider"], "model_budget_micro": 0,
            "builder_budget_micro": 0, "qa_budget_micro": runtime["qa_compute_ceiling_micro"]})
        session.commit()
        return execution._serialize(session, run)

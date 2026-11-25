"""Explicit reviewed QA budget for one genuine server source/approved contract."""
from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import select

from app.build import subscription_static_policy as policy
from app.builder import execution
from app.builder.model_proxy import BuildModelError, digest
from app.core.fsm import advance
from app.credits import pools
from app.credits._txn import naive_utc
from app.db.models import VentureDocument
from app.db.session import serialize_writes
from app.db.supymem_models import BuildRun, PlanTask
from app.state import db
from . import ORIGIN, TRIGGER
from . import inputs

QUOTE_KIND = "pagebuilder_source_qa_quote"


def run_id(fingerprint):
    return "bpg_" + fingerprint[:48]


def _quote_view(value):
    ceiling = value["runtime"]["qa_compute_ceiling_micro"]
    return {"quote_id": value["id"], "quote_digest": digest(value), "expires_at": value["expires_at"] + "Z",
        "source_fingerprint": value["fingerprint"], "qa_ceiling_micro": ceiling, "credits": ceiling / 10000,
        "model_ceiling_micro": 0, "builder_compute_ceiling_micro": 0, "source_origin": ORIGIN,
        "message": "Independent QA of the exact saved server draft. Review and publishing require separate founder approval."}


def _lock(session, venture_id, caller, selection):
    wid, _actor = inputs.authority(venture_id, caller)
    serialize_writes(session, key="pagebuilder-source-admission:" + wid + ":" + venture_id)


def estimate(venture_id, caller, selection):
    with db()() as session:
        _lock(session, venture_id, caller, selection)
        prior = inputs.prior(session, venture_id, caller, selection)
        if prior and inputs.qa_retry(prior, json.loads(prior.spec)) is None:
            return {"existing_run": execution._serialize(session, prior)}
        envelope, app = inputs.current(session, venture_id, caller, selection)
        runtime = policy.configured()
        now = naive_utc()
        qid = "pgq_" + digest({"fingerprint": envelope["fingerprint"], "runtime": runtime})[:48]
        row = session.get(VentureDocument, qid)
        quote = {"id": qid, "identity": envelope["identity"], "fingerprint": envelope["fingerprint"],
            "inputs": envelope["inputs"], "runtime": runtime, "expires_at": (now + timedelta(minutes=10)).isoformat()}
        if row:
            saved = json.loads(row.provenance_json)
            if saved["expires_at"] > now.isoformat():
                return _quote_view(saved)
            row.provenance_json = json.dumps(quote, sort_keys=True)
            row.updated_at = now
        else:
            session.add(VentureDocument(id=qid, venture_id=venture_id, kind=QUOTE_KIND,
                title="Review independent server source QA", provenance_json=json.dumps(quote, sort_keys=True), created_at=now, updated_at=now))
        session.commit()
        return _quote_view(quote)


def admit(venture_id, caller, selection, *, quote_id, quote_digest, approve_qa_ceiling_micro):
    with db()() as session:
        _lock(session, venture_id, caller, selection)
        row = session.get(VentureDocument, quote_id)
        if row is None or row.venture_id != venture_id or row.kind != QUOTE_KIND:
            raise KeyError("pagebuilder_admission_quote_not_found")
        quote = json.loads(row.provenance_json)
        if (digest(quote) != quote_digest or inputs.selection(quote["identity"]) != selection
                or quote["identity"]["scope"]["actor_id"] != inputs.authority(venture_id, caller)[1]
                or type(approve_qa_ceiling_micro) is not int
                or approve_qa_ceiling_micro != quote["runtime"]["qa_compute_ceiling_micro"]):
            raise BuildModelError("pagebuilder_admission_quote_changed")
        prior = inputs.prior(session, venture_id, caller, selection)
        attempt = inputs.qa_retry(prior, json.loads(prior.spec)) if prior else 0
        if attempt is None:
            return execution._serialize(session, prior)
        envelope, app = inputs.current(session, venture_id, caller, selection)
        if quote["identity"] != envelope["identity"] or quote["inputs"] != envelope["inputs"]:
            raise BuildModelError("pagebuilder_admission_quote_changed")
        now = naive_utc()
        if quote["expires_at"] <= now.isoformat():
            raise BuildModelError("pagebuilder_admission_quote_expired")
        runtime = policy.validate(quote["runtime"])
        account_id = execution._account_id(session, app)
        if not account_id:
            raise BuildModelError("pagebuilder_admission_credits_unavailable")
        account = pools.lock_account(session, account_id)
        app = session.scalar(select(type(app)).where(type(app).id == app.id).with_for_update())
        if session.scalar(select(BuildRun.id).where(BuildRun.app_id == app.id, BuildRun.runtime_version == 1, BuildRun.closed_at.is_(None))):
            raise BuildModelError("pagebuilder_admission_open_run")
        if inputs.current(session, venture_id, caller, selection)[0] != envelope:
            raise BuildModelError("pagebuilder_admission_inputs_changed")
        bounds, fingerprint = runtime["deadlines"], envelope["fingerprint"]
        # A retried QA attempt binds the same source fingerprint under a fresh run, pool and hold.
        key = fingerprint if attempt == 0 else digest({"fingerprint": fingerprint, "qa_attempt": attempt})
        deadline = now + timedelta(seconds=sum(bounds.values()))
        spec = {"lifecycle_version": 3, "source_origin": ORIGIN, "mode": "pagebuilder_source_qa", "generation": 1,
            "actor": envelope["identity"]["scope"]["actor_id"], "inputs": envelope["inputs"], "runtime": runtime,
            "artifact_profile": "static_landing_v1", "phase": "importing_source", "cleanup_state": "pending",
            "execution_verified": False, "source_import_verified": False, "qa_state": "not_run", "qa_attempt": attempt,
            "source_import": {"state": "prepared", **envelope},
            "lifecycle": {"phase": "importing_source", "queue_deadline": (now + timedelta(seconds=bounds["queue_seconds"])).isoformat(),
                "total_deadline": deadline.isoformat(), "builder": {"cleanup_state": "complete", "no_external_call": True},
                "qa": {"cleanup_state": "pending"}, "qa_required": True, "review": {"status": "not_ready"}}}
        run = BuildRun(id=run_id(key), app_id=app.id, workspace_id=app.workspace_id, venture_id=venture_id,
            plan_task_id=selection["plan_task_id"], trigger=TRIGGER, agent="pagebuilder_source_qa", state="syncing_artifacts",
            runtime_version=1, request_digest=fingerprint, idempotency_key_hash=key, created_at=now, started_at=now,
            spec=json.dumps(spec, sort_keys=True))
        session.add(run)
        session.flush()
        pools.create_pool(session, account=account, run=run, reservation_id="crv_" + key[:48],
            amount_micro=runtime["ceiling_micro"], expires_at=deadline)
        ctx = pools.lock_pool(session, run.reservation_id)
        hold_id = "crv_qa_" + key[:44]
        pools.allocate(session, ctx, reservation_id=hold_id, amount_micro=runtime["qa_compute_ceiling_micro"], purpose="build.compute.qa", provider="aws")
        spec["lifecycle"]["holds"] = {"qa": hold_id}
        from app.builder.orchestration import _followup, _progress_plan
        if not inputs.no_plan(run, spec):
            task = session.get(PlanTask, run.plan_task_id)
            if task.state == "failed":
                advance(session, task, "ready", actor="founder", reason="Founder approved independent server source QA")
            task.build_run_id = run.id
            _progress_plan(session, ctx, spec)
        _followup(session, ctx, spec, delay=0)
        execution._event(session, run, "pagebuilder_source_qa_admitted", {"source_fingerprint": fingerprint,
            "source_origin": ORIGIN, "qa_attempt": attempt, "model_budget_micro": 0, "builder_budget_micro": 0, "qa_budget_micro": runtime["qa_compute_ceiling_micro"]})
        session.commit()
        return execution._serialize(session, run)

"""No-plan server sources retain the same QA, money and founder receipt gates."""
from contextlib import nullcontext
import json
from types import SimpleNamespace

import pytest

from tests.integration.test_postgres_auth_security import service_modules


@pytest.fixture()
def deliverable(monkeypatch):
    with service_modules("company_os"):
        from app.build import subscription_static_controller as controller
        from app.builder import execution, model_proxy
        from app.chat import milestone_hooks
        from app.pagebuilder_admission.inputs import NO_PLAN_CONTRACT
        from core.clock import utcnow

        values = {"contract_source": NO_PLAN_CONTRACT, "plan_id": None, "plan_task_id": None,
                  "product_context": {"revision_id": "revision_one"}, "milestone": {"kind": "landing"}}
        scope = {"plan_id": None, "plan_task_id": None, "workspace_id": "workspace_one", "venture_id": "venture_one",
                 "app_id": "app_one", "actor_id": "founder_one", "provider": "pagebuilder", "revision_id": "revision_one"}
        spec = {"actor": "founder_one", "lifecycle_version": 3, "source_origin": "pagebuilder_candidate_v1",
                "artifact_profile": "static_landing_v1", "generation": 1, "inputs": values,
                "source_import_verified": True, "artifact_verified": True, "execution_verified": False,
                "source_import": {"identity": {"scope": scope, "inputs_digest": model_proxy.digest(values)}},
                "lifecycle": {"qa_id": "qa_one", "builder": {"cleanup_state": "complete", "no_external_call": True},
                    "qa": {"cleanup_state": "complete", "terminal_observation": {"state": "STOPPED"}, "settlement_receipt": "settled"},
                    "review": {"status": "pending", "source_artifact_id": "artifact_one", "report_digest": "report_one", "qa_id": "qa_one"}}}
        run = SimpleNamespace(id="run_one", app_id="app_one", workspace_id="workspace_one", venture_id="venture_one",
            trigger="pagebuilder_source_qa", plan_task_id=None, runtime_version=1, state="succeeded", closed_at=utcnow(),
            artifact_id="artifact_one", spec=json.dumps(spec), error_code=None)
        qa = SimpleNamespace(id="qa_one", build_run_id=run.id, workspace_id=run.workspace_id, venture_id=run.venture_id,
            app_id=run.app_id, generation=1, source_artifact_id=run.artifact_id, report_digest="report_one",
            contract_digest="contract_one", receipt_json=json.dumps({"outcome": "passed"}), finished_at=None)
        child = SimpleNamespace(purpose="build.compute.qa", status="settled")
        rows = {("BuildRun", run.id): run, ("QaRun", qa.id): qa,
                ("Venture", run.venture_id): SimpleNamespace(stage="draft")}
        events, proof = [], {"code": None}
        session = SimpleNamespace(get=lambda model, key, **_kw: rows.get((model.__name__, key)), commit=lambda: None,
            scalar=lambda _query: None, scalars=lambda query: SimpleNamespace(all=lambda:
                [child] if query.column_descriptions[0]["entity"].__name__ == "CreditReservation" else []))
        ctx = SimpleNamespace(run=run, pool=SimpleNamespace(id="pool_one"))
        monkeypatch.setattr(execution, "db", lambda: lambda: nullcontext(session))
        monkeypatch.setattr(execution, "_app", lambda *_args, **_kw: None)
        monkeypatch.setattr(execution, "_event", lambda _s, _r, kind, data: events.append((kind, data)))
        monkeypatch.setattr(execution, "_queue_runtime_after_accept", lambda *_args: None)
        monkeypatch.setattr(model_proxy, "_locked", lambda *_args: ctx)
        monkeypatch.setattr(milestone_hooks, "build_accepted", lambda *_args, **_kw: None)
        monkeypatch.setattr(controller, "source_review_code", lambda *_args: proof["code"])
        yield SimpleNamespace(controller=controller, execution=execution, spec=spec, run=run, qa=qa,
                              child=child, session=session, ctx=ctx, events=events, proof=proof)


def test_no_plan_review_accepts_exact_receipt_without_plan_completion(deliverable):
    d, checked = deliverable, []

    def fence(session):
        assert session is d.session
        checked.append(True)

    args = {"actor": "founder_one", "source_artifact_id": "artifact_one", "report_digest": "report_one",
            "decision": "accept", "effect_fence": fence}
    first = d.execution.review(d.run.id, **args)
    assert d.execution.review(d.run.id, **args) == first
    assert checked == [True, True]
    saved = json.loads(d.run.spec)
    assert saved["lifecycle"]["review"]["actor"] == args["actor"]
    assert saved["lifecycle"]["review"]["status"] == "accepted" and saved["execution_verified"] is False
    assert "task_completion" not in saved["lifecycle"]
    assert d.events == [("founder_review", {"decision": "accept", "source_artifact_id": "artifact_one",
                                           "report_digest": "report_one", "plan_task_id": None})]


def test_refused_publication_effect_fence_cannot_accept_a_source(deliverable):
    from app.core.jobs import FatalError

    d = deliverable

    def refused(session):
        assert session is d.session
        raise FatalError("publication_lease_lost")

    before = d.run.spec
    with pytest.raises(FatalError, match="publication_lease_lost"):
        d.execution.review(d.run.id, actor="founder_one", source_artifact_id="artifact_one",
                           report_digest="report_one", decision="accept", effect_fence=refused)
    assert d.run.spec == before and not d.events


@pytest.mark.parametrize("fault", ["actor", "artifact", "report", "source_proof", "native_origin"])
def test_no_plan_review_never_waives_founder_or_source_proof(deliverable, fault):
    d = deliverable
    args = {"actor": "founder_one", "source_artifact_id": "artifact_one", "report_digest": "report_one", "decision": "accept"}
    if fault in {"actor", "artifact", "report"}:
        args[{"actor": "actor", "artifact": "source_artifact_id", "report": "report_digest"}[fault]] = "changed"
    elif fault == "source_proof":
        d.proof["code"] = "build_review_source_proof_invalid"
    else:
        d.run.trigger = "coding_source_import"
    with pytest.raises(d.execution.BuildError):
        d.execution.review(d.run.id, **args)
    assert json.loads(d.run.spec)["lifecycle"]["review"]["status"] == "pending" and not d.events


@pytest.mark.parametrize("outcome", ["passed", "failed", "unsettled"])
def test_no_plan_finish_queues_only_after_success_and_closed_qa_costs(deliverable, monkeypatch, outcome):
    from app.pagebuilder_admission import publication

    d, order = deliverable, []
    d.run.state, d.run.closed_at = "running", None
    d.qa.receipt_json = json.dumps({"outcome": "failed" if outcome == "failed" else "passed"})
    if outcome == "unsettled":
        d.child.status = "open"
    monkeypatch.setattr(d.controller.qa_receipts, "serialize", lambda _qa: {"verified_report": True})
    monkeypatch.setattr(d.controller, "recheck", lambda *_args, **_kw: order.append("source_recheck"))
    monkeypatch.setattr(d.controller, "advance", lambda _s, run, state, **_kw: setattr(run, "state", state))

    def close(_session, ctx):
        ctx.run.closed_at = True
        saved = json.loads(ctx.run.spec)
        saved["reconciliation_required"] = False
        ctx.run.spec = json.dumps(saved)
        order.append("closed_pool")

    def queue(session, run, spec):
        assert session is d.session and run.closed_at and spec["reconciliation_required"] is False
        assert d.qa.state == "passed" and spec["execution_verified"] is False
        order.append("publication_requested")

    monkeypatch.setattr(d.controller.pools, "close_pool", close)
    monkeypatch.setattr(publication, "queue_if_requested", queue)
    assert d.controller.finish(d.session, d.ctx, d.spec) is (outcome != "unsettled")
    assert order == (["source_recheck", "closed_pool", "publication_requested"] if outcome == "passed"
                     else ["closed_pool"] if outcome == "failed" else [])
    if outcome != "unsettled":
        assert d.run.state == ("succeeded" if outcome == "passed" else "failed")
        saved = json.loads(d.run.spec)
        assert saved["lifecycle"]["completion"]["verified_execution"] is False
        assert "task_completion" not in saved["lifecycle"]

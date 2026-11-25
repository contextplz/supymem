"""Legacy template paths now require confirmed evidence and durable execution."""
from tests.company_os_harness import load_company_os_app
import pytest
from tests.unit.gtm.test_journey import journey_db


@pytest.mark.parametrize("idea", ["Invoice reconciliation for clinic purchasing teams", "Weekend trail guides for families"])
@pytest.mark.parametrize("with_research", [False, True])
def test_local_plan_compiles_without_hosted_prerequisites(journey_db, monkeypatch, idea, with_research):
    import json
    from app.chat.recovery import _skeleton
    from app.core.jobs import lease
    from app.db.models import Venture
    from app.db.supymem_models import ResearchDossier, ResearchFinding
    from app.planning import author, compiler, execution, resources
    from app.research import metered
    from core.clock import utcnow

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    data = journey_db
    with data["factory"]() as session:
        session.get(Venture, data["vid"]).raw_idea = idea
        if with_research:
            session.add(ResearchDossier(id="local_sources", workspace_id=data["wid"], venture_id=data["vid"],
                revision_id=None, kind="deep", state="complete", created_at=utcnow(),
                summary=json.dumps({"review_kind": "retrieved_sources_only", "coverage_gaps": []})))
            session.add(ResearchFinding(id="local_source", dossier_id="local_sources", workspace_id=data["wid"],
                venture_id=data["vid"], category="competitor", source="builtin", title="Public alternative", url="https://example.org/alternative",
                summary="Observed alternative for " + idea, evidence=json.dumps({"observed_at": utcnow().isoformat()}),
                created_at=utcnow()))
        session.commit()
    monkeypatch.setattr(resources, "_provider_metadata", lambda *_: {
        "provider": "openai", "model": "local-model", "credential_source": "founder_override",
        "credential_state": "configured", "health": "unverified"})
    requests = []

    def synthesize(pid, payload, **kwargs):
        context = json.loads(execution.read(pid).structured)["_runtime"]["context"]
        requests.append(context)
        assert context["idea"]["summary"] == idea
        prompt = author.messages(context)[0]["content"]
        assert "for the owner's connected coding tool" in prompt
        assert "Settings > Keys and connections > Coding tools" in prompt
        return kwargs["parse"](json.dumps(_skeleton(idea, "Test your assumptions with real customers.")))

    monkeypatch.setattr(metered, "synthesize", synthesize)
    started = compiler.start(data["vid"], data["wid"])
    with data["factory"]() as session:
        job = lease(session, "local-plan-worker", kinds=["plan.compile"])[0]
        assert job.id == started["job_id"]
        payload = {**job.payload, "_job_id": job.id, "_worker_id": job.worker_id, "_job_attempt": job.attempts}
        session.commit()
    plan = compiler.run_compile(payload)
    assert plan["state"] == "founder_review" and plan["reviewable"] and not plan["stale_dependencies"]
    assert bool(plan["sources"]) is with_research
    assert plan["provisional"] is (not with_research)
    if not with_research:
        assert plan["summary"].startswith("Provisional plan:")
        assert any("research" in gap.lower() for gap in plan["evidence_gaps"])
    assert "gateway.supymem.com" not in json.dumps(requests)
    assert compiler.run_compile(payload)["id"] == plan["id"] and len(requests) == 1


@pytest.mark.parametrize("idea", ["Inspection checklists for construction teams", "Recipes for home fermentation"])
def test_local_plan_missing_model_does_not_enqueue_work(journey_db, monkeypatch, idea):
    from sqlalchemy import select
    from app.db.models import Job, Venture
    from app.planning import compiler, resources

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    data = journey_db
    with data["factory"]() as session:
        session.get(Venture, data["vid"]).raw_idea = idea
        session.commit()
    monkeypatch.setattr(resources, "_provider_metadata", lambda *_: {
        "provider": None, "model": None, "credential_source": "unknown", "credential_state": "not_checked"})
    with pytest.raises(ValueError, match="^plan_model_required$"):
        compiler.start(data["vid"], data["wid"])
    with data["factory"]() as session:
        assert session.scalar(select(Job.id).where(Job.kind == "plan.compile")) is None


def test_conversational_intent_does_not_claim_an_unimplemented_edit():
    load_company_os_app()
    from app.planning.compiler import edit_intent
    result = edit_intent("Move payments before onboarding")
    assert result["operation"] == "revise_plan"
    assert result["requires_generation"] and result["preview_diff"] == []
    with pytest.raises(ValueError):
        edit_intent(" ")


def test_legacy_catalogue_maturity_cannot_prove_scope():
    load_company_os_app()
    from app.planning.compiler import _task_scope
    assert _task_scope({"capability_id": "build.run"}, {"build.run": {"maturity": "GA"}})[0] == "out"
    assert _task_scope({"capability_id": "founder.action"}, {})[0] == "conditional"
    manual = {"capability_id": "build.run", "implementation": "founder_action"}
    assert _task_scope(manual, {"build.run": {"state": "unavailable"}})[0] == "out"
    scope, reason = _task_scope(manual, {"build.run": {"state": "implemented"}})
    assert scope == "conditional" and "not authorized" in reason


def test_capability_is_only_in_scope_for_declared_milestone_kind():
    load_company_os_app()
    from app.planning.compiler import _task_scope

    capabilities = {
        "build.run": {
            "state": "implemented",
            "supported_milestone_kinds": ["landing"],
        }
    }
    task = {"capability_id": "build.run", "implementation": "new_code"}

    assert _task_scope(task, capabilities, "landing")[0] == "conditional"
    scope, reason = _task_scope(task, capabilities, "core")
    assert scope == "out"
    assert "not available for core milestones" in reason


def test_native_subscription_landing_does_not_reopen_paid_builder_or_autopilot():
    load_company_os_app()
    from app.planning.compiler import _task_scope, _approved_work
    capabilities = {'build.run': {'state': 'unavailable'}, 'coding.subscription_static': {
        'state': 'implemented', 'supported_milestone_kinds': ['landing']}}
    native = {'capability_id': 'coding.subscription_static', 'implementation': 'new_code'}
    assert _task_scope(native, capabilities, 'landing')[0] == 'conditional'
    assert _task_scope(native, capabilities, 'core')[0] == 'out'
    assert _task_scope({'capability_id': 'build.run'}, capabilities, 'landing')[0] == 'out'
    doc = {'proposal': {'milestones': [{'key': 'landing', 'kind': 'landing', 'included': True,
        'title': 'Reviewed landing', 'description': 'Native source only', 'tasks': [dict(native,
        key='source', title='Create reviewed copy', description='Use exact contract', acceptance=['Record source custody'])]}]},
        'entity_ids': {'tasks': {'landing:source': 'tsk_native'}},
        'scope_details': {'tsk_native': {'scope': 'conditional'}}}
    work = _approved_work('plan_native', doc)
    assert work[0]['runnable'] is False


def test_future_native_product_work_never_uses_generic_autopilot_even_if_conditional():
    load_company_os_app()
    from app.planning.compiler import _approved_work
    doc = {"proposal": {"milestones": [{"key": "m3", "kind": "core", "included": True,
        "title": "Proposed core", "description": "Requires reviewed native source and independent product QA.",
        "tasks": [{"key": "t1", "title": "Prepare exact proposed workflow",
                   "description": "No execution ready yet", "acceptance": ["Preserve the exact workflow"],
                   "capability_id": "coding.subscription_product_source", "implementation": "new_code"}]}]},
        "entity_ids": {"tasks": {"m3:t1": "task_future"}},
        "scope_details": {"task_future": {"scope": "conditional"}}}
    projected = _approved_work("plan_future", doc)
    assert len(projected) == 1 and projected[0]["runnable"] is False
    assert "Preserve the exact workflow" in projected[0]["detail"]

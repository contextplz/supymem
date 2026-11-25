"""Assembled signed routes exercise plan generation, editing and approval."""
import json

import pytest
from sqlalchemy import select

from tests.security.test_connector_oauth_boundary import client as client
from tests.security.test_durable_journey_boundary import confirm_idea, request, run_queued, venture
from tests.idea_fixtures import install_provider
from tests.planning_fixtures import install_planner
from tests.research_fixtures import install_research


@pytest.fixture()
def ready(client, monkeypatch):
    install_provider(monkeypatch)
    vid, wid, sid = venture(client)
    confirm_idea(client, vid, sid)
    install_research(monkeypatch)
    run_queued("research.deep")
    result = request(client, "PUT", f"/ventures/{vid}/resources", json={"assets": [], "complete": True})
    assert result.status_code == 200, result.text
    calls = install_planner(monkeypatch)
    return vid, wid, calls


def compile(client, vid):
    result = request(client, "POST", f"/ventures/{vid}/plans/compile", json={})
    assert result.status_code == 200, result.text
    jid = result.json()["job_id"]
    assert request(client, "POST", f"/ventures/{vid}/plans/compile", json={}).json()["job_id"] == jid
    run_queued("plan.compile")
    from app.db.models import Job
    from app.state import db
    with db()() as session:
        job = session.get(Job, jid)
        assert job.status == "succeeded", job.last_error
    rows = request(client, "GET", f"/ventures/{vid}/plans").json()["items"]
    return request(client, "GET", f"/ventures/{vid}/plans/{rows[0]['id']}").json()


def test_waitlist_observation_is_private_and_does_not_invalidate_plan_on_new_lead(client, ready):
    import hashlib
    from app.conversion.waitlist import capture, configure, consent_text, VERSION
    from app.db.models import Venture
    from app.planning.execution import inputs
    from app.state import db
    vid, wid, _ = ready
    with db()() as session:
        before = inputs(session, wid, vid)
        name = session.get(Venture, vid).name
    configure(vid, wid, enabled=True, actor="usr_alice")
    capture(vid, {"email": "private-lead@fixture.example", "consent": "yes", "consent_version": VERSION,
        "consent_digest": hashlib.sha256(consent_text(name).encode()).hexdigest()})
    with db()() as session:
        after = inputs(session, wid, vid)
    observed = after["context"]["managed_conversion_observation"]
    assert observed["enabled"] is True and observed["total"] == 1
    assert observed["submission_verified"] is False and observed["email_verified"] is False
    assert "private-lead@fixture.example" not in json.dumps(after["context"])
    assert after["dependency_digest"] == before["dependency_digest"]
    assert request(client, "GET", f"/ventures/{vid}/resources", "usr_bob").status_code in {403, 404}


def test_plan_metered_review_edits_approval_no_build(client, ready):
    from app.db.models import Job
    from app.state import db
    vid, wid, calls = ready
    plan = compile(client, vid)
    assert plan["method"] == "metered_foundry_plan" and plan["state"] == "founder_review"
    assert plan["estimate"]["total_upfront_credits"] is None and plan["feasibility"]["ok"] is None
    assert plan["usage"]["credits_micro"] > 0 and len(calls) == 1
    assert calls[0][1]["max_tokens"] == 16384
    assert plan["milestones"][0]["scope"] == "out" and plan["milestones"][0]["estimate_credits"] is None
    assert "_runtime" not in json.dumps(plan) and "reservation_id" not in json.dumps(plan)
    base = f"/ventures/{vid}/plans/{plan['id']}"
    response = request(client, "PATCH", base, json={"edit_version": plan["edit_version"], "patch": {"summary": "A founder reviewed bakery plan"}})
    assert response.status_code == 200, response.text
    edited = response.json()
    assert edited["edit_version"] == 2 and edited["review_token"] != plan["review_token"]
    from app.db.supymem_models import FounderRequest
    with db()() as session:
        audit = session.scalar(select(FounderRequest).where(FounderRequest.related_id == plan["id"], FounderRequest.source == "plan_manual_edit"))
        assert json.loads(audit.text)["before"]["summary"] == plan["summary"]
        assert json.loads(audit.text)["actor"] == "usr_alice"
    assert request(client, "PATCH", base, json={"edit_version": 1, "patch": {"summary": "stale"}}).status_code == 409
    assert request(client, "POST", base + "/approve", json={"review_token": plan["review_token"]}).status_code == 409
    result = request(client, "POST", base + "/approve", json={"review_token": edited["review_token"]})
    assert result.status_code == 200, result.text
    assert result.json()["plan"]["state"] == "approved" and result.json()["execution_started"] is False
    queue = request(client, "GET", f"/ventures/{vid}/tasks").json()["items"]
    expected = [
        task["title"]
        for milestone in edited["proposal"]["milestones"]
        for task in milestone["tasks"]
    ][:5]
    assert [task["title"] for task in queue] == expected
    assert queue[0]["runnable"] is False
    assert "capability:build.run" in queue[0]["tags"]
    assert any(tag.startswith("plan_task:") for tag in queue[0]["tags"])
    assert queue[1]["runnable"] is False
    task_ids = [task["id"] for task in queue]
    repeated = request(client, "POST", base + "/approve", json={"review_token": edited["review_token"]})
    assert repeated.status_code == 200
    assert [
        task["id"]
        for task in request(client, "GET", f"/ventures/{vid}/tasks").json()["items"]
    ] == task_ids
    assert request(client, "PATCH", base, json={"edit_version": 2, "patch": {"summary": "changed approval"}}).status_code == 409
    with db()() as session:
        assert not session.scalars(select(Job).where(Job.venture_id == vid, Job.kind == "build.run")).all()
    assert len(calls) == 1


def test_current_research_and_inventory_gate_and_foreign_resources(client, monkeypatch):
    install_provider(monkeypatch)
    vid, _, sid = venture(client)
    assert request(client, "POST", f"/ventures/{vid}/plans/compile", json={}).status_code == 409
    confirm_idea(client, vid, sid)
    result = request(client, "POST", f"/ventures/{vid}/plans/compile", json={})
    assert result.json()["detail"]["error"]["code"] == "plan_research_required"
    install_research(monkeypatch)
    run_queued("research.deep")
    result = request(client, "POST", f"/ventures/{vid}/plans/compile", json={})
    assert result.json()["detail"]["error"]["code"] == "plan_resource_inventory_required"
    assert request(client, "GET", f"/ventures/{vid}/resources", "usr_bob").status_code in {403, 404}
    assert request(client, "PUT", f"/ventures/{vid}/resources", "usr_bob", json={"assets": [], "complete": True}).status_code in {403, 404}


def test_changed_inventory_invalidates_approval_and_foreign_plan_denied(client, ready):
    vid, _, _ = ready
    plan = compile(client, vid)
    base = f"/ventures/{vid}/plans/{plan['id']}"
    assert request(client, "GET", base, "usr_bob").status_code in {403, 404}
    assert request(client, "POST", base + "/requests", "usr_bob", json={"text": "steal"}).status_code in {403, 404}
    request(client, "PUT", f"/ventures/{vid}/resources", json={"assets": [{"kind": "coding_agent", "name": "Founder tool", "exists": True}], "complete": True})
    assert request(client, "GET", base).json()["stale_dependencies"] is True
    response = request(client, "POST", base + "/approve", json={"review_token": plan["review_token"]})
    assert response.status_code == 409 and response.json()["detail"]["error"]["code"] == "plan_inputs_changed"


def test_conversation_generates_new_review_draft_and_dependency_edits_rollback(client, ready):
    vid, _, calls = ready
    original = compile(client, vid)
    base = f"/ventures/{vid}/plans/{original['id']}"
    response = request(client, "POST", base + "/requests", json={"text": "Test a manual paid offer before building the order board."})
    assert response.status_code == 200, response.text
    run_queued("plan.compile")
    assert len(calls) == 2
    plans = request(client, "GET", f"/ventures/{vid}/plans").json()["items"]
    revised = request(client, "GET", f"/ventures/{vid}/plans/{plans[0]['id']}").json()
    assert revised["id"] != original["id"] and revised["state"] == "founder_review"
    old = request(client, "GET", base).json()
    assert old["reviewable"] is False and old["newer_plan_id"] == revised["id"]
    assert request(client, "POST", base + "/approve", json={"review_token": old["review_token"]}).status_code == 409
    assert request(client, "PATCH", base, json={"edit_version": 1, "patch": {"summary": "older draft edit"}}).status_code == 409
    milestones = revised["proposal"]["milestones"]
    milestones[0]["depends_on"] = [milestones[-1]["key"]]
    response = request(client, "PATCH", f"/ventures/{vid}/plans/{revised['id']}", json={"edit_version": 1, "patch": {"milestones": milestones}})
    assert response.status_code == 422
    assert request(client, "GET", f"/ventures/{vid}/plans/{revised['id']}").json()["edit_version"] == 1


def test_real_empty_research_allows_plan_with_explicit_evidence_gaps(client, monkeypatch):
    install_provider(monkeypatch)
    vid, _, sid = venture(client)
    confirm_idea(client, vid, sid)
    install_research(monkeypatch, empty=True)
    run_queued("research.deep")
    request(client, "PUT", f"/ventures/{vid}/resources", json={"assets": [], "complete": True})
    calls = install_planner(monkeypatch)
    quote = request(client, "POST", f"/ventures/{vid}/plans/estimate-generation", json={})
    assert quote.status_code == 200 and quote.json()["ceiling_micro"] > 0
    assert quote.json()["sufficient_now"] is True and len(calls) == 0
    plan = compile(client, vid)
    assert plan["sources"] == [] and plan["evidence_gaps"]
    assert "No usable public source" in json.dumps(json.loads(calls[0][0][-1]["content"])["research_gaps"])


def test_contributor_may_request_work_but_cannot_approve(client, ready):
    from app.db.models import WorkspaceMember
    from app.state import db
    vid, wid, _ = ready
    plan = compile(client, vid)
    with db()() as session:
        member = session.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == wid, WorkspaceMember.person_id == "usr_alice"))
        member.role = "contributor"
        session.commit()
    response = request(client, "POST", f"/ventures/{vid}/plans/{plan['id']}/approve", json={"review_token": plan["review_token"]})
    assert response.status_code == 403, response.text


@pytest.mark.parametrize("subject", ["usr_alice", "founder"])
def test_plan_approval_resolves_subject_to_real_workspace_member(client, ready, subject):
    from app.authz.workspace import subject_person_id
    from app.db.models import Person, WorkspaceMember
    from app.db.supymem_models import ExecutionPlan
    from app.state import db
    from core.clock import utcnow
    from core.ids import new_id
    vid, wid, _ = ready
    plan = compile(client, vid)
    if subject == "founder":
        person_id = subject_person_id(subject)
        with db()() as session:
            if session.get(Person, person_id) is None:
                session.add(Person(id=person_id, display_name="Local founder", created_at=utcnow()))
                session.flush()
            session.add(WorkspaceMember(id=new_id("wsm"), workspace_id=wid, person_id=person_id,
                                        role="maintainer", created_at=utcnow()))
            session.commit()
    path = f"/ventures/{vid}/plans/{plan['id']}"
    result = request(client, "POST", path + "/approve", subject, json={"review_token": plan["review_token"]})
    assert result.status_code == 200, result.text
    with db()() as session:
        assert session.get(ExecutionPlan, plan["id"]).approved_by == subject_person_id(subject)


@pytest.mark.parametrize("repaired", [True, False])
def test_native_contract_repair_is_once_settled_and_failure_has_founder_action(client, ready, monkeypatch, repaired):
    from app.db.models import Job
    from app.db.supymem_models import ExecutionPlan
    from app.state import db
    from tests.planning_fixtures import proposal
    vid, _, _ = ready
    def respond(context, options):
        value = proposal(context)
        if len(calls) == 1 or not repaired:
            core = value["milestones"][2]["tasks"][0]
            core.update(capability_id="coding.subscription_product_source", product_source_expectations={
                "profile": "native_python_product_source_v1", "journey_json": json.dumps({
                    "schema": "local_product_journey_v1", "steps": [
                        {"name": "create_order", "method": "POST", "path": "/api/product/orders", "status": 201},
                        {"name": "read_order", "method": "GET", "path": "/api/product/orders", "status": 200}]}),
                "criteria": [{"criterion": 0, "step_names": ["create_order", "read_order"]}]})
        return value
    calls = install_planner(monkeypatch, respond)
    response = request(client, "POST", f"/ventures/{vid}/plans/compile", json={})
    jid = response.json()["job_id"]
    run_queued("plan.compile")
    assert len(calls) == 2
    assert "every step has name, actor, method, path and integer status" in calls[1][0][0]["content"]
    with db()() as session:
        assert session.get(Job, jid).status == "succeeded"
        row = session.scalar(select(ExecutionPlan).where(ExecutionPlan.venture_id == vid))
        operations = json.loads(row.structured)["_runtime"]["operations"]
        assert set(operations) == {"plan", "plan-repair"}
        assert all(op["state"] == "completed" and op["usage"]["settled"] for op in operations.values())
        pid = row.id
    plan = request(client, "GET", f"/ventures/{vid}/plans/{pid}").json()
    assert plan["generation_state"] == ("completed" if repaired else "failed")
    if repaired:
        assert plan["proposal"] and plan["generation_recovery"] is None
    else:
        assert plan["proposal"] is None and not plan["reviewable"]
        assert plan["generation_recovery"]["href"] == f"/v/{vid}/plan#plan-create"
        assert plan["generation_recovery"]["message"] in plan["limitations"]
        assert "native_product_source_contract_invalid" not in json.dumps(plan)


def test_complete_native_product_plan_compiles_on_first_attempt(client, ready, monkeypatch):
    from tests.planning_fixtures import proposal
    from tests.unit.planning.test_author import _native_product_expectations
    vid, _, _ = ready
    def respond(context, options):
        value = proposal(context)
        expected = _native_product_expectations()
        journey = json.loads(expected["journey_json"])
        journey["steps"][0]["body"] = {"workflow": "Synthetic bakery order details " * 80}
        expected["journey_json"] = json.dumps(journey)
        value["milestones"][2]["tasks"][0].update(
            capability_id="coding.subscription_product_source", product_source_expectations=expected)
        return value
    calls = install_planner(monkeypatch, respond)
    plan = compile(client, vid)
    assert len(calls) == 1 and plan["generation_state"] == "completed"
    core = plan["milestones"][2]["tasks"][0]
    assert len(core["product_source_expectations"]["journey_json"]) > 2000
    assert core["scope"] == "out" and not plan["execution_ready"]


def test_uncertain_plan_keeps_receipt_and_returns_manual_founder_action_without_repair(client, ready, monkeypatch):
    from app.db.models import Job
    from app.db.supymem_models import ExecutionPlan
    from app.state import db
    from llm_gateway.errors import ProviderCallError
    vid, _, _ = ready
    def timeout(context, options):
        raise ProviderCallError("provider_down", "Synthetic timeout")
    calls = install_planner(monkeypatch, timeout)
    jid = request(client, "POST", f"/ventures/{vid}/plans/compile", json={}).json()["job_id"]
    run_queued("plan.compile")
    with db()() as session:
        assert session.get(Job, jid).status == "succeeded"
        row = session.scalar(select(ExecutionPlan).where(ExecutionPlan.venture_id == vid))
        before = json.loads(row.structured)["_runtime"]["operations"]
        assert before["plan"]["state"] == "uncertain"
        pid = row.id
    assert len(calls) == 1
    plan = request(client, "GET", f"/ventures/{vid}/plans/{pid}").json()
    assert plan["generation_state"] == "failed" and plan["usage"]["has_unsettled"]
    assert plan["generation_recovery"]["href"] == f"/v/{vid}/research"
    from app.planning.execution import journal
    from app.core.jobs import FatalError
    with pytest.raises(FatalError, match="plan_outcome_uncertain"):
        journal.recover(pid, "plan")
    with db()() as session:
        assert json.loads(session.get(ExecutionPlan, pid).structured)["_runtime"]["operations"] == before
    assert len(calls) == 1


def test_explicit_byok_plan_uses_schema_without_platform_debit(client, ready, monkeypatch):
    from app.connectors.secrets import reset_store
    from app.llm import founder_provider
    from app.credits.accounts import get_account
    from llm_gateway import sole
    from tests.planning_fixtures import proposal
    from types import SimpleNamespace
    vid, wid, _ = ready
    # Keep this spend-boundary assertion independent of any FileSecretStore
    # instance left behind by an earlier tmp_path-backed test. The database is
    # reset per harness load, while the connector vault is process-global.
    monkeypatch.setenv("COMPANY_OS_EPHEMERAL_VAULT", "1")
    reset_store()
    founder_provider.set_founder_config(workspace_id=wid, provider="azure_foundry", model="founder-deployment", base_url="https://byok.example.test/openai/v1", api_key="founder-key")
    seen = []
    async def byok(messages, **kwargs):
        seen.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(proposal(json.loads(messages[-1]["content"])))), finish_reason="stop")],
                               usage=SimpleNamespace(prompt_tokens=300, completion_tokens=1200, total_tokens=1500))
    monkeypatch.setattr(sole, "byok_chat_completion", byok)
    before = get_account(vid).spent_micro
    quote = request(client, "POST", f"/ventures/{vid}/plans/estimate-generation", json={}).json()
    assert quote["payer"] == "founder" and quote["ceiling_micro"] == 0
    plan = compile(client, vid)
    assert get_account(vid).spent_micro == before and plan["usage"]["credits_micro"] == 0
    assert seen[0]["single_attempt"] is True and seen[0]["response_schema"]
    assert plan["usage"]["operations"][0]["source"] == "byok"


def test_contradictory_saved_claim_blocks_approval_until_audited_edit_without_regeneration(client, ready):
    from app.db.supymem_models import ExecutionPlan, FounderRequest
    from app.state import db
    vid, _, calls = ready
    plan = compile(client, vid)
    with db()() as session:
        row = session.get(ExecutionPlan, plan["id"])
        doc = json.loads(row.structured)
        doc["_runtime"]["context"]["managed_conversion_observation"] = {
            "submission_verified": True, "verified_at": "2026-09-19T10:49:02Z", "total": 0,
        }
        doc["proposal"]["evidence_gaps"] = ["The managed waitlist has not been tested to receive submissions."]
        row.structured = json.dumps(doc)
        session.commit()
    base = f"/ventures/{vid}/plans/{plan['id']}"
    saved = request(client, "GET", base).json()
    assert saved["reviewable"] is True  # Manual correction remains available.
    assert saved["review_findings"][0]["code"] == "conversion_snapshot_conflict"
    refused = request(client, "POST", base + "/approve", json={"review_token": saved["review_token"]})
    assert refused.status_code == 409 and "plan_claim_review_required" in refused.text
    with db()() as session:
        assert session.get(ExecutionPlan, plan["id"]).state == "founder_review"
    repaired = request(client, "PATCH", base, json={"edit_version": saved["edit_version"], "patch": {
        "evidence_gaps": ["Recheck the dated waitlist submission/storage verification before publication; no demand is proven."]}})
    assert repaired.status_code == 200, repaired.text
    fixed = repaired.json()
    assert fixed["review_findings"] == []
    assert len(calls) == 1
    assert request(client, "POST", base + "/approve", json={"review_token": saved["review_token"]}).status_code == 409
    approved = request(client, "POST", base + "/approve", json={"review_token": fixed["review_token"]})
    assert approved.status_code == 200 and approved.json()["execution_started"] is False
    with db()() as session:
        audit = session.scalar(select(FounderRequest).where(FounderRequest.related_id == plan["id"], FounderRequest.source == "plan_manual_edit"))
        assert json.loads(audit.text)["actor"] == "usr_alice"

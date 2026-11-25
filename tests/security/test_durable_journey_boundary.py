"""New APIs run through the assembled signed founder boundary and durable jobs."""
from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import select

from tests.security.test_connector_oauth_boundary import client as client
from tests.security.test_core_signed_boundary import headers


IDEA = {"problem": "Small bakeries lose custom cake orders in email", "customer": "Neighbourhood bakery owners",
        "offer": "An order intake board", "workflow": "Customer submits an order and bakery confirms",
        "first_user_path": "Founder invites one bakery", "pricing_hypothesis": "Monthly subscription after trial",
        "desired_outcome": "First bakery pays for its order board"}


@pytest.fixture(autouse=True)
def provider_network(client, monkeypatch):
    from tests.idea_fixtures import install_provider
    install_provider(monkeypatch)


def request(client, method, path, subject="usr_alice", **kwargs):
    return client.request(method, path, headers=headers(path, method, subject), **kwargs)


def venture(client, subject="usr_alice"):
    response = request(client, "POST", "/ventures", subject, json={"name": "Bakery", "raw_idea": IDEA["problem"], "start": False})
    assert response.status_code == 201, response.text
    vid, wid = response.json()["id"], response.json()["workspace_id"]
    response = request(client, "POST", f"/ventures/{vid}/idea/sessions", subject)
    assert response.status_code == 201, response.text
    return vid, wid, response.json()["id"]


def test_initial_idea_job_keeps_authenticated_founder_authority(client):
    from app.db.models import Job
    from app.state import db

    response = request(
        client,
        "POST",
        "/ventures",
        "usr_alice",
        json={"name": "Bakery", "raw_idea": IDEA["problem"], "start": False},
    )
    assert response.status_code == 201, response.text
    with db()() as session:
        job = session.scalar(
            select(Job).where(
                Job.venture_id == response.json()["id"],
                Job.kind == "idea.clarify_turn",
            )
        )
        assert job is not None
        assert json.loads(job.payload_json).get("_actor_subject") == "usr_alice"


def run_queued(kind, *, through_job_id=None):
    from app.core.jobs import lease
    from app.state import db
    from app.worker.handlers import install_default_handlers
    from app.worker.main import _run_one
    install_default_handlers()
    last_job_id = None
    for _ in range(10):
        with db()() as session:
            jobs = lease(session, "acceptance-worker", kinds=[kind], n=1)
            session.commit()
        if not jobs:
            assert last_job_id is not None
            return last_job_id
        last_job_id = jobs[0].id
        asyncio.run(_run_one(db(), jobs[0], {jobs[0].id}))
        if through_job_id is not None and last_job_id == through_job_id:
            return last_job_id
    raise AssertionError(f"job {through_job_id} was not reached")


def confirm_idea(client, vid, sid):
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    assert request(client, "PATCH", base + "/revision", json={"patch": IDEA}).status_code == 200
    response = request(client, "POST", base + "/propose")
    assert response.status_code == 200, response.text
    jid = run_queued("idea.clarify_turn", through_job_id=response.json()["job_id"])
    from app.db.models import Job
    from app.state import db
    with db()() as session:
        assert session.get(Job, jid).status == "succeeded", session.get(Job, jid).last_error
    looks_right = request(client, "POST", f"/ventures/{vid}/chat/confirm", json={"action": "idea.proposal.approve",
        "payload": {"session_id": sid, "revision_id": request(client, "GET", base).json()["current_revision"]["id"]}})
    assert looks_right.status_code == 200, looks_right.text
    run_queued("idea.validate")  # GStack office-hours questions
    skipped = request(client, "POST", base + "/messages", json={"text": "Skip — run the review with what you have"})
    assert skipped.status_code == 200, skipped.text
    run_queued("idea.validate")
    revision = request(client, "GET", base).json()["current_revision"]["id"]
    response = request(client, "POST", base + "/confirm", json={"revision_id": revision})
    assert response.status_code == 200, response.text
    return response.json()


def test_confirmed_direction_can_fork_review_without_changing_saved_history_or_job(client):
    from app.db.models import Job
    from app.db.supymem_models import ExecutionPlan, IdeaSession, IdeaRevision, ResearchDossier
    from app.state import db
    from core.clock import utcnow

    vid, wid, sid = venture(client)
    confirmed = confirm_idea(client, vid, sid)
    old_revision = confirmed["session"]["current_revision"]["id"]
    with db()() as session:
        session.add(ResearchDossier(id="rsd_saved_direction", workspace_id=wid, venture_id=vid,
            revision_id=old_revision, kind="deep", state="complete", summary="{}", created_at=utcnow()))
        session.add(ExecutionPlan(id="epl_saved_direction", workspace_id=wid, venture_id=vid,
            revision_id=old_revision, version=1, state="founder_review", summary="Saved plan",
            structured="{}", created_at=utcnow()))
        session.commit()
    path = f"/ventures/{vid}/idea/sessions/{sid}/correct-direction"
    body = {"case": "explore", "revision_id": old_revision}
    assert request(client, "POST", path, "usr_bob", json=body).status_code in {403, 404}
    corrected = request(client, "POST", path, json=body)
    assert corrected.status_code == 201, corrected.text
    fresh = corrected.json()
    assert fresh["id"] != sid and fresh["case"] == "explore"
    assert fresh["state"] == "clarifying"
    assert fresh["current_revision"]["structured"]["_corrects_revision_id"] == old_revision
    assert fresh["turns"][-1]["kind"] == "question"
    reopened = request(client, "POST", f"/ventures/{vid}/idea/sessions").json()
    assert reopened["id"] == fresh["id"]
    assert reopened["initial_job_id"] is None and len(reopened["turns"]) == 1
    from app.research.service import start as start_research
    with pytest.raises(RuntimeError, match="research_confirmation_required"):
        start_research(vid)
    rerun = request(client, "POST", f"/ventures/{vid}/research/dossiers/rsd_saved_direction/rerun",
        json={"phase": "all"})
    assert rerun.status_code == 409 and "research_confirmation_required" in rerun.text
    assert request(client, "POST", path, json=body).status_code == 409
    answer = request(client, "POST", f"/ventures/{vid}/idea/sessions/{fresh['id']}/messages",
        json={"text": "Explore a different customer and offer"})
    assert answer.status_code == 200, answer.text
    run_queued("idea.clarify_turn", through_job_id=answer.json()["job_id"])
    after_clarification = request(client, "POST", f"/ventures/{vid}/idea/sessions").json()
    assert after_clarification["id"] == fresh["id"]
    assert after_clarification["current_revision"]["structured"]["_corrects_revision_id"] == old_revision
    assert after_clarification["initial_job_id"] is None
    with db()() as session:
        old = session.get(IdeaSession, sid)
        assert old.state == "confirmed" and old.confirmed_revision_id == old_revision
        assert session.get(IdeaRevision, old_revision).session_id == sid
        assert session.get(Job, confirmed["dossier_job_id"]).status == "scheduled"
        dossier = session.get(ResearchDossier, "rsd_saved_direction")
        plan = session.get(ExecutionPlan, "epl_saved_direction")
        assert (dossier.state, dossier.revision_id) == ("complete", old_revision)
        assert (plan.state, plan.revision_id, plan.summary) == ("founder_review", old_revision, "Saved plan")


def test_confirmed_answer_reopens_saved_choices_without_charging_or_rewriting_history(client):
    from app.db.models import Job
    from app.db.supymem_models import ClarifierTurn, IdeaRevision, ResearchDossier
    from app.state import db
    from core.clock import utcnow

    vid, wid, sid = venture(client)
    confirmed = confirm_idea(client, vid, sid)
    old_revision = confirmed["session"]["current_revision"]["id"]
    with db()() as session:
        turns = session.scalars(select(ClarifierTurn).where(ClarifierTurn.session_id == sid).order_by(ClarifierTurn.turn)).all()
        number = turns[-1].turn + 1
        session.add_all([
            ClarifierTurn(id="ict_saved_customer_question", session_id=sid, workspace_id=wid, venture_id=vid,
                turn=number, role="agent", kind="question", text="Who pays?",
                options=json.dumps({"field": "customer", "options": ["Bakery owner", "Consultant"], "selection_mode": "single"}),
                created_at=utcnow()),
            ClarifierTurn(id="ict_saved_customer_answer", session_id=sid, workspace_id=wid, venture_id=vid,
                turn=number + 1, role="founder", kind="answer", text="Bakery owner", created_at=utcnow()),
            ResearchDossier(id="rsd_saved_answer", workspace_id=wid, venture_id=vid,
                revision_id=old_revision, kind="deep", state="complete", summary="{}", created_at=utcnow()),
        ])
        old_structured = session.get(IdeaRevision, old_revision).structured
        before_jobs = session.scalars(select(Job.id).where(Job.venture_id == vid)).all()
        session.commit()
    path = f"/ventures/{vid}/idea/sessions/{sid}/correct-answer"
    body = {"revision_id": old_revision, "question_id": "ict_saved_customer_question",
            "text": "Consultant", "patch": {"customer": "Consultant"}}
    assert request(client, "POST", path, "usr_bob", json=body).status_code in {403, 404}
    assert request(client, "POST", path, json={**body, "patch": {"offer": "Consultant"}}).status_code == 422
    assert request(client, "POST", path, json={**body, "question_id": "ict_missing"}).status_code == 422
    response = request(client, "POST", path, json=body)
    assert response.status_code == 201, response.text
    fresh = response.json()
    assert fresh["id"] != sid
    assert fresh["current_revision"]["structured"]["customer"] == "Consultant"
    assert fresh["current_revision"]["structured"]["_corrects_revision_id"] == old_revision
    copied = next(turn for turn in fresh["turns"] if turn["text"] == "Who pays?")
    assert copied["options"]["options"] == ["Bakery owner", "Consultant"]
    assert fresh["current_revision"]["structured"]["_answer_corrections"][copied["id"]] == "Consultant"
    assert request(client, "POST", f"/ventures/{vid}/idea/sessions").json()["id"] == fresh["id"]
    with db()() as session:
        assert session.get(IdeaRevision, old_revision).structured == old_structured
        assert session.get(ResearchDossier, "rsd_saved_answer").revision_id == old_revision
        assert session.scalars(select(Job.id).where(Job.venture_id == vid)).all() == before_jobs
    assert request(client, "POST", path, json=body).status_code == 409


def test_coherent_signed_idea_plan_job_application_journey(client, monkeypatch):
    from app.db.models import Job
    from app.db.supymem_models import AppAuthPool, AppDatabase, IdeaSession
    from app.state import db
    monkeypatch.setenv("IDEA_SYNC", "1")  # Hosted clients cannot force in-request workers.
    vid, wid, sid = venture(client)
    confirmed = confirm_idea(client, vid, sid)
    with db()() as session:
        assert session.get(IdeaSession, sid).confirmed_by_person_id == "usr_alice"
        assert session.get(Job, confirmed["dossier_job_id"]).status == "scheduled"
    from tests.research_fixtures import install_research
    install_research(monkeypatch)
    run_queued("research.deep")
    result = request(client, "GET", f"/jobs/{confirmed['dossier_job_id']}")
    assert result.json()["status"] == "succeeded"
    assert "payload" not in result.json() and "_actor_subject" not in result.text
    from tests.planning_fixtures import install_planner
    assert request(client, "PUT", f"/ventures/{vid}/resources", json={"assets": [], "complete": True}).status_code == 200
    install_planner(monkeypatch)
    compiled = request(client, "POST", f"/ventures/{vid}/plans/compile", json={})
    assert compiled.status_code == 200, compiled.text
    job_id = compiled.json()["job_id"]
    assert request(client, "GET", f"/jobs/{job_id}").json()["status"] == "scheduled"
    run_queued("plan.compile")
    assert request(client, "GET", f"/jobs/{job_id}").json()["status"] == "succeeded"
    plans = request(client, "GET", f"/ventures/{vid}/plans").json()["items"]
    plan = request(client, "GET", f"/ventures/{vid}/plans/{plans[0]['id']}").json()
    assert plan["method"] == "metered_foundry_plan" and plan["execution_ready"] is False
    assert plan["milestones"][-1]["kind"] == "first_paid_user"
    approved = request(client, "POST", f"/ventures/{vid}/plans/{plan['id']}/approve", json={"review_token": plan["review_token"]})
    assert approved.status_code == 200 and approved.json()["execution_started"] is False
    app = request(client, "POST", f"/ventures/{vid}/apps", json={"name": "Order board", "slug": "bakery-board"})
    assert app.status_code == 201, app.text
    assert app.json()["state"] == "draft"
    with db()() as session:
        assert not session.scalars(select(AppDatabase)).all()
        assert not session.scalars(select(AppAuthPool)).all()
    jobs = request(client, "GET", f"/workspaces/{wid}/jobs").json()["items"]
    assert len(jobs) >= 3 and all(row["workspace_id"] == wid for row in jobs)


def test_foreign_and_sibling_session_mutations_are_denied(client):
    from app.db.models import Venture
    from app.state import db
    alice, wid, sid_a = venture(client)
    bob, _, sid_b = venture(client, "usr_bob")
    for suffix, method, body in [("/messages", "POST", {"text": "steal"}), ("/revision", "PATCH", {"patch": IDEA}),
                                  ("/case", "POST", {"case": "explore"}), ("/revisions", "GET", None),
                                  ("/confirm", "POST", {})]:
        result = request(client, method, f"/ventures/{alice}/idea/sessions/{sid_b}" + suffix, json=body)
        assert result.status_code == 404, result.text
    # Even another venture inside the same authorized workspace cannot be mixed.
    with db()() as session:
        session.get(Venture, bob).workspace_id = wid
        session.commit()
    assert request(client, "POST", f"/ventures/{bob}/idea/sessions/{sid_a}/messages", json={"text": "mixed sibling"}).status_code == 404


def test_revision_patch_and_validation_cannot_cross_tenant_or_sibling_venture(client):
    from app.db.models import Venture
    from app.state import db

    alice, wid, _ = venture(client)
    bob, _, sid_b = venture(client, "usr_bob")
    before_session = request(client, "GET", f"/ventures/{bob}/idea/sessions/{sid_b}", "usr_bob").json()
    before = before_session["current_revision"]
    path = f"/ventures/{alice}/idea/sessions/{sid_b}/revision"
    assert request(client, "PATCH", path, json={"patch": {"offer": "Injected"}}).status_code == 404
    validate_path = f"/ventures/{alice}/idea/sessions/{sid_b}/validate"
    assert request(client, "POST", validate_path, json={}).status_code == 404
    with db()() as session:
        session.get(Venture, bob).workspace_id = wid
        session.commit()
    assert request(client, "PATCH", path, json={"patch": {"offer": "Injected"}}).status_code == 404
    assert request(client, "POST", validate_path, json={}).status_code == 404
    # The workspace reassignment above is only for the sibling-venture check;
    # inspect the durable revision directly after that synthetic fixture change.
    from app.db.models import Job
    from app.db.supymem_models import IdeaSession, IdeaRevision
    with db()() as session:
        row = session.get(IdeaSession, sid_b)
        revision = session.get(IdeaRevision, row.current_revision_id)
        assert revision.id == before["id"]
        assert "Injected" not in revision.structured
        assert row.state == before_session["state"]
        assert not session.scalars(select(Job).where(Job.venture_id == bob, Job.kind == "idea.validate")).all()


def test_candidate_and_validation_actions_queue_without_fake_results(client):
    from app.db.supymem_models import ValidationRun
    from app.state import db
    vid, _, sid = venture(client)
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    result = request(client, "POST", base + "/messages", json={"text": "Suggest up to three tailored, unvalidated product directions for me to review."})
    assert result.status_code == 200 and result.json()["job_id"]
    assert result.json()["session"]["current_revision"]["source"] == "founder"
    assert request(client, "POST", base + "/validate", json={}).status_code == 409
    run_queued("idea.clarify_turn", through_job_id=result.json()["job_id"])
    validation = request(client, "POST", base + "/validate", json={})
    assert validation.status_code == 200 and validation.json()["job_id"]
    with db()() as session:
        assert not session.scalars(select(ValidationRun)).all()


def test_foreign_plan_revision_job_and_application_are_denied(client, monkeypatch):
    alice, _, sid = venture(client)
    confirmed = confirm_idea(client, alice, sid)
    from tests.research_fixtures import install_research
    from tests.planning_fixtures import install_planner
    install_research(monkeypatch)
    run_queued("research.deep")
    request(client, "PUT", f"/ventures/{alice}/resources", json={"assets": [], "complete": True})
    install_planner(monkeypatch)
    result = request(client, "POST", f"/ventures/{alice}/plans/compile", json={})
    run_queued("plan.compile")
    plan_id = request(client, "GET", f"/ventures/{alice}/plans").json()["items"][0]["id"]
    bob, _, _ = venture(client, "usr_bob")
    path = f"/ventures/{bob}/plans/{plan_id}"
    for method, suffix, body in [("GET", "", None), ("GET", "/estimate", None), ("POST", "/approve", {}),
                                  ("POST", "/apply-edit", {"operation": "none"}), ("POST", "/requests", {"text": "steal"})]:
        assert request(client, method, path + suffix, "usr_bob", json=body).status_code == 404
    assert request(client, "GET", f"/jobs/{result.json()['job_id']}", "usr_bob").status_code == 404
    revision = confirmed["session"]["current_revision"]["id"]
    assert request(client, "POST", f"/ventures/{bob}/plans/compile", "usr_bob", json={"revision_id": revision}).status_code == 404
    app = request(client, "POST", f"/ventures/{alice}/apps", json={"name": "Board", "slug": "isolated-board"}).json()
    assert request(client, "GET", f"/apps/{app['id']}", "usr_bob").status_code == 404


def test_draft_application_cannot_claim_host_execution_or_deployment(client, monkeypatch):
    from app.builder import ctl
    monkeypatch.setattr(ctl, "terminal_exec", lambda *args: pytest.fail("host terminal executed"))
    vid, _, _ = venture(client)
    app = request(client, "POST", f"/ventures/{vid}/apps", json={"name": "Board", "slug": "safe-draft"}).json()
    for suffix, body in [("/terminal/sessions", {"command": "env"}), ("/sleep", {}), ("/wake", {}),
                         ("/iterate", {"text": "run code"}), ("/site/versions/latest/publish", {})]:
        response = request(client, "POST", f"/apps/{app['id']}" + suffix, json=body)
        assert response.status_code == 503, response.text
    assert request(client, "GET", f"/apps/{app['id']}/site").status_code == 404
    assert request(client, "GET", f"/apps/{app['id']}").json()["state"] == "draft"


def test_immutable_confirmed_revision_and_missing_resources(client):
    vid, _, sid = venture(client)
    confirm_idea(client, vid, sid)
    result = request(client, "PATCH", f"/ventures/{vid}/idea/sessions/{sid}/revision", json={"patch": {"offer": "changed"}})
    assert result.status_code == 409
    assert request(client, "POST", f"/ventures/{vid}/idea/sessions/{sid}/case", json={"case": "explore"}).status_code == 409
    assert request(client, "GET", "/jobs/job_absent").status_code == 404
    assert request(client, "GET", "/apps/app_absent").status_code == 404


def test_foreign_application_key_cannot_be_revoked_through_owned_application(client):
    alice, _, _ = venture(client)
    bob, _, _ = venture(client, "usr_bob")
    a = request(client, "POST", f"/ventures/{alice}/apps", json={"name": "Alice", "slug": "alice-keys"}).json()
    b = request(client, "POST", f"/ventures/{bob}/apps", "usr_bob", json={"name": "Bob", "slug": "bob-keys"}).json()
    key = request(client, "POST", f"/apps/{a['id']}/api-keys", json={"scopes": ["ai"]})
    assert key.status_code == 201, key.text
    assert request(client, "POST", f"/apps/{b['id']}/api-keys/{key.json()['id']}/revoke", "usr_bob").status_code == 404
    items = request(client, "GET", f"/apps/{a['id']}/api-keys").json()["items"]
    assert items[0]["state"] == "active" and "secret" not in items[0]


def test_blocking_handler_keeps_event_loop_and_tenant_context_available(client, monkeypatch):
    from threading import Event
    from app.worker import handlers
    from app.tenancy import bind, current
    progress = Event()
    def blocking(payload):
        assert current().workspace_id == "test-workspace" and current().actor == "usr_alice"
        assert progress.wait(timeout=2), "worker handler blocked heartbeat/event loop"
    monkeypatch.setitem(handlers.HANDLERS, "plan.compile", blocking)
    async def run():
        with bind(workspace_id="test-workspace", venture_id="test-venture", actor="usr_alice"):
            task = asyncio.create_task(handlers.dispatch("plan.compile", {}))
            await asyncio.sleep(0.01)
            progress.set()
            await task
    asyncio.run(run())


def test_legacy_inline_call_cannot_bypass_hosted_provider_gate(client):
    from app.core.jobs import FatalError
    from app.worker.run_sync import run_kind
    with pytest.raises(FatalError, match="capability_unavailable:infra.provision"):
        run_kind("infra.provision", {"workspace_id": "not-real", "venture_id": "not-real"})


def test_job_status_never_returns_legacy_provider_error_text(client):
    from app.db.models import Job
    from app.state import db
    vid, _, sid = venture(client)
    job_id = request(client, "POST", f"/ventures/{vid}/idea/sessions/{sid}/messages", json={"text": "Help clarify the first bakery offer"}).json()["job_id"]
    with db()() as session:
        session.get(Job, job_id).last_error = "secret_provider_token_from_legacy_job"
        session.commit()
    result = request(client, "GET", f"/jobs/{job_id}")
    assert result.json()["error_code"] == "job_failed"
    assert result.json()["created_at"].endswith("+00:00")
    assert "secret_provider" not in result.text
    with db()() as session:
        session.get(Job, job_id).last_error = "plan_outcome_uncertain:private_provider_response"
        session.commit()
    result = request(client, "GET", f"/jobs/{job_id}")
    assert result.json()["error_code"] == "plan_outcome_uncertain"
    assert "private_provider_response" not in result.text


def test_hosted_entry_starts_saved_idea_without_legacy_onboarding(client, monkeypatch):
    from app.credits.accounts import account_for_venture
    from app.db.models import Job, Workspace
    from app.db.supymem_models import IdeaSession
    from app.onboarding import spawn
    from app.state import db
    async def forbidden(*args, **kwargs):
        pytest.fail("Hosted entry must not start the legacy local onboarding pipeline")
    monkeypatch.setattr(spawn, "start_onboarding", forbidden)
    for text, expected_case in [("A booking page for one independent rehearsal studio", "know"), ("", "explore")]:
        response = request(client, "POST", "/entry", json={"text": text, "explicit_mode": "idea", "confirm": True, "start": True})
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["next_step"] == "idea" and data["onboarding"] is None
        vid = data["venture_id"]
        with db()() as session:
            idea = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == vid))
            assert idea is not None and idea.case == expected_case
            credit = account_for_venture(vid, session=session)
            assert credit is not None and credit.scope == "person"
            assert credit.workspace_id is None and credit.venture_id is None
            assert credit.owner_person_id == session.get(Workspace, data["workspace"]["id"]).owner_id
            assert credit.granted_micro > 0
            job = session.scalar(select(Job).where(Job.venture_id == vid))
            if text:
                assert job is not None and job.kind == "idea.clarify_turn"
                assert json.loads(job.payload_json).get("_actor_subject") == "usr_alice"
            else:
                assert job is None
        assert request(client, "GET", f"/ventures/{vid}/idea/sessions").json()["items"][0]["id"] == idea.id
        assert request(client, "GET", f"/ventures/{vid}/idea/sessions", subject="usr_bob").status_code == 404


def _proposed_idea(client):
    vid, _, sid = venture(client)
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    request(client, "POST", base + "/case", json={"case": "explore"})
    assert request(client, "PATCH", base + "/revision", json={"patch": IDEA}).status_code == 200
    response = request(client, "POST", base + "/propose")
    run_queued("idea.clarify_turn", through_job_id=response.json()["job_id"])
    return vid, sid


def test_chat_research_start_accepts_validation_confirms_idea_and_queues_research(client):
    from app.db.models import Job
    from app.db.supymem_models import IdeaSession, ValidationRun
    from app.state import db

    vid, sid = _proposed_idea(client)
    revision = request(client, "GET", f"/ventures/{vid}/idea/sessions/{sid}").json()["current_revision"]["id"]
    assert request(client, "POST", f"/ventures/{vid}/chat/confirm", json={"action": "idea.proposal.approve",
        "payload": {"session_id": sid, "revision_id": revision}}).status_code == 200  # founder's Looks right
    run_queued("idea.validate")  # GStack office-hours questions
    skipped = request(client, "POST", f"/ventures/{vid}/idea/sessions/{sid}/messages",
                      json={"text": "Skip — run the review with what you have"})
    assert skipped.status_code == 200, skipped.text
    run_queued("idea.validate")
    with db()() as session:
        runs = session.scalars(select(ValidationRun).where(ValidationRun.session_id == sid)).all()
        assert runs and all(r.state == "complete" for r in runs)
    reply = request(client, "POST", f"/ventures/{vid}/chat/confirm", json={"action": "research.start"})
    assert reply.status_code == 200, reply.text
    assert reply.json()["kind"] == "text" and "research will start" in reply.json()["body_md"]
    with db()() as session:
        assert session.get(IdeaSession, sid).state == "confirmed"
        assert all(r.founder_decision == "accept" for r in session.scalars(
            select(ValidationRun).where(ValidationRun.session_id == sid)))
        assert len(session.scalars(select(Job).where(Job.venture_id == vid, Job.kind == "research.deep")).all()) == 1
    again = request(client, "POST", f"/ventures/{vid}/chat/confirm", json={"action": "research.start"})
    assert again.status_code == 200 and again.json()["kind"] == "text"
    with db()() as session:
        assert len(session.scalars(select(Job).where(Job.venture_id == vid, Job.kind == "research.deep")).all()) == 1


def test_chat_research_start_without_finished_validation_needs_founder_and_queues_nothing(client):
    from app.db.models import Job
    from app.db.supymem_models import IdeaSession
    from app.state import db

    vid, sid = _proposed_idea(client)  # validation never ran
    reply = request(client, "POST", f"/ventures/{vid}/chat/confirm", json={"action": "research.start"})
    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert body["kind"] == "needs_you" and "confirm your idea" in body["body_md"] and "Nothing was started" in body["body_md"]
    assert body["actions"][0]["action"] == "idea.review" and not body["actions"][0].get("href")  # chat-first: stays in chat
    with db()() as session:
        assert session.get(IdeaSession, sid).state != "confirmed"
        assert not session.scalars(select(Job).where(Job.venture_id == vid, Job.kind == "research.deep")).all()

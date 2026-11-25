import asyncio
import json

import pytest
from sqlalchemy import select

from tests.company_os_harness import load_company_os_app


@pytest.fixture
def founder(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/controls.db")
    monkeypatch.setenv("COMPANY_OS_MEMORY_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", "founder-controls-test-secret-at-least-32-bytes")
    load_company_os_app()
    from app import state
    from app.authz.workspace import ensure_subject_person
    from app.chat.conversations import bind_conversation, create
    from app.core.local_mode import is_local
    from app.core.local_owner import ensure_owner
    from app.db.models import Venture
    from app.db.supymem_models import IdeaSession
    from app.idea.service import append_revision
    from app.tenancy import bind
    from app.workspaces.service import create_workspace
    from core.clock import utcnow

    state.reset_db_factory()
    state.MEM.clear()
    actor = ensure_owner().id if is_local() else "usr_founder_controls"
    ensure_subject_person(actor)
    workspace = create_workspace(kind="company", name="Controls", owner_id=actor)
    vid = "ven_founder_controls"
    with state.db()() as session:
        now = utcnow()
        session.add(Venture(id=vid, workspace_id=workspace.id, name="Controls", raw_idea="Booking for teachers",
                            stage="draft", created_at=now, updated_at=now))
        session.flush()
        idea = IdeaSession(id="ids_controls", workspace_id=workspace.id, venture_id=vid, case="know",
                           state="confirmed", created_at=now, updated_at=now, confirmed_at=now,
                           confirmed_by_person_id=actor)
        session.add(idea)
        session.flush()
        revision = append_revision(session, idea, {"summary": "Booking for teachers", "problem": "Lost bookings",
            "customer": "teachers", "offer": "Booking software", "pricing_hypothesis": "$19",
            "first_user_path": "Interview teachers", "_questions_skipped": is_local()}, source="founder")
        idea.confirmed_revision_id = revision.id
        original = revision.id
        session.commit()
    with bind(workspace_id=workspace.id, venture_id=vid, actor=actor):
        cid = create(vid, idempotency_key="controls-chat")["id"]
        with bind_conversation(vid, cid):
            yield {"vid": vid, "wid": workspace.id, "actor": actor, "cid": cid, "original": original}


@pytest.mark.parametrize("idea", ["Warehouse task scheduling", "Family cooking meetups"])
def test_u67_validated_startup_confirmation_offers_next_step_in_chat(founder, monkeypatch, idea):
    from app.chat.execute import execute_action
    from app.db.models import Venture
    from app.idea import service
    from app.state import db
    from app.ventures import startup_review

    with db()() as session:
        session.get(Venture, founder["vid"]).raw_idea = idea
        session.commit()
    calls = []
    def confirm(session_id, **kwargs):
        calls.append((session_id, kwargs))
        return {"next_href": f"/v/{founder['vid']}/context", "session": {"current_revision": {"structured": {}}}}
    monkeypatch.setattr(service, "confirm", confirm)
    monkeypatch.setattr(startup_review, "get", lambda _: {"complete": True})
    result = asyncio.run(execute_action(founder["vid"], "idea.confirm", payload={"session_id": "ids_controls", "revision_id": founder["original"]}, actor_subject=founder["actor"]))
    assert result["kind"] == "navigation"
    assert result["actions"] == [{"label": "Choose my next step", "action": "whats_next", "style": "primary"}]
    assert calls[0][1]["start_research"] is False and calls[0][1]["accept_validation"] is True
    assert calls[0][1]["venture_id"] == founder["vid"]


@pytest.mark.parametrize("text,patch", [
    ("change the target customer to dentists and rerun the research", {"customer": "dentists"}),
    ("change the target customer to home gardeners and rerun the research", {"customer": "home gardeners"}),
    ("actually the price is $49, redo research", {"pricing_hypothesis": "$49"}),
])
@pytest.mark.parametrize("idea_state", ["confirmed", "reviewing", "clarifying", "open", "skipped"])
def test_review_required_revision_saved_and_new_research_started_once(founder, monkeypatch, text, patch, idea_state):
    from app.chat.actions import get_action
    from app.chat.execute import execute_action
    from app.chat.intent import classify_intent, split_speech_acts
    from app.chat import research_revision
    from app.db.models import Job, Venture, VentureDocument
    from app.db.supymem_models import IdeaSession
    from app.research import service
    from app.state import db
    from app.ventures.current_idea import current_revision, revision_fields
    from core.clock import utcnow

    with db()() as session:
        idea = session.get(IdeaSession, "ids_controls")
        if idea_state == "skipped":
            idea.state = "abandoned"
            session.add(VentureDocument(id=f"journey_overrides:{founder['vid']}", venture_id=founder["vid"],
                kind="journey_overrides", title="Journey choices", created_at=utcnow(), updated_at=utcnow(),
                body_md=json.dumps({"skipped": {"clarification": True, "validation": True}})))
            expected_revision = None
        else:
            idea.state = idea_state
            if idea_state != "confirmed":
                idea.confirmed_revision_id = None
                idea.confirmed_at = None
            expected_revision = founder["original"]
        session.commit()
    assert classify_intent(text) == "research.update_and_rerun"
    assert split_speech_acts(text) == [("research.update_and_rerun", text)]
    action = get_action("research.update_and_rerun")
    assert action.consequential and action.required_permission == "request_work"
    with pytest.raises(ValueError, match="research_review_required"):
        asyncio.run(execute_action(founder["vid"], action.name, payload={"patch": patch}, actor_subject=founder["actor"]))
    reply = research_revision.prepare(founder["vid"], text, founder["actor"])
    card = next(item["confirmation"] for item in reply["actions"] if item.get("confirmation"))
    assert any(value in " ".join(card["consequences"]) for value in patch.values())
    if idea_state != "confirmed":
        assert "Booking" in " ".join(card["consequences"])
    with db()() as session:
        current = current_revision(session.get(Venture, founder["vid"]), session)
        assert (current.id if current else None) == expected_revision
        assert session.scalar(select(Job.id).where(Job.kind == "research.deep")) is None
    real_start = service.start
    calls = []
    def start(vid, **kwargs):
        calls.append(kwargs)
        return real_start(vid, **kwargs)
    monkeypatch.setattr(service, "start", start)
    result = asyncio.run(execute_action(founder["vid"], card["action"], payload=card["resulting_state"], actor_subject=founder["actor"]))
    repeated = asyncio.run(execute_action(founder["vid"], card["action"], payload=card["resulting_state"], actor_subject=founder["actor"]))
    assert repeated == result and len(calls) == 1
    with db()() as session:
        revision = current_revision(session.get(Venture, founder["vid"]), session)
        assert revision.id != founder["original"]
        assert (revision.session_id == "ids_controls") == (idea_state not in {"confirmed", "skipped"})
        idea = session.get(IdeaSession, revision.session_id)
        assert idea.state == "confirmed" and idea.confirmed_revision_id == revision.id
        assert all(revision_fields(revision)[key] == value for key, value in patch.items())
        jobs = session.scalars(select(Job).where(Job.kind == "research.deep")).all()
        assert len(jobs) == 1 and json.loads(jobs[0].payload_json)["revision_id"] == revision.id
        assert jobs[0].id == result["job_id"]


def test_correction_with_no_research_job_keeps_the_recovery_action(founder, monkeypatch):
    from app.chat import research_revision
    from app.research import service

    monkeypatch.setattr(service, "start", lambda *args, **kwargs: {
        "kind": "needs_you", "body_md": "Add a description before starting research.",
        "actions": [{"label": "Add research topic", "href": f"/v/{founder['vid']}/idea"}], "citations": []})
    offered = research_revision.prepare(founder["vid"], "the price is $49, redo research", founder["actor"])
    result = research_revision.confirm(founder["vid"], founder["actor"], offered["actions"][0]["confirmation"]["resulting_state"])
    assert result["kind"] == "needs_you" and result["actions"] and result["revision_id"]


@pytest.mark.parametrize("idea_state", ["confirmed", "reviewing"])
def test_stale_review_cannot_overwrite_new_revision(founder, idea_state):
    from app.chat import research_revision
    from app.db.supymem_models import IdeaSession
    from app.state import db

    with db()() as session:
        idea = session.get(IdeaSession, "ids_controls")
        idea.state = idea_state
        if idea_state != "confirmed":
            idea.confirmed_revision_id = None
        session.commit()
    first = research_revision.prepare(founder["vid"], "the price is $49, redo research", founder["actor"])
    second = research_revision.prepare(founder["vid"], "the price is $59, redo research", founder["actor"])
    research_revision.confirm(founder["vid"], founder["actor"], first["actions"][0]["confirmation"]["resulting_state"])
    with pytest.raises(ValueError, match="research_inputs_changed"):
        research_revision.confirm(founder["vid"], founder["actor"], second["actions"][0]["confirmation"]["resulting_state"])


def test_stop_uses_cancel_requested_preserves_lease_and_blocks_late_enqueue(founder):
    from app.chat.conversations import bind_conversation, create
    from app.chat.store import CLIENT_REQUEST_ID
    from app.chat.work_control import work
    from app.core.jobs import complete_job, enqueue
    from app.db.models import Job
    from app.state import db

    def queued():
        with db()() as session:
            jid = enqueue(session, "research.deep", {}, workspace_id=founder["wid"], venture_id=founder["vid"])
            session.commit()
            return jid
    scheduled, leased = queued(), queued()
    with db()() as session:
        row = session.get(Job, leased)
        row.status, row.locked_by, row.attempts = "leased", "worker", 1
        session.commit()
    other_cid = create(founder["vid"], idempotency_key="other-chat")["id"]
    with bind_conversation(founder["vid"], other_cid):
        unrelated = queued()
    assert len(work(founder["vid"], founder["cid"])["items"]) == 2
    work(founder["vid"], founder["cid"], stop=True, nonce="turn-to-stop")
    work(founder["vid"], founder["cid"], stop=True, nonce="turn-to-stop")
    assert work(founder["vid"], founder["cid"])["items"] == []
    with db()() as session:
        assert session.get(Job, scheduled).status == "cancelled"
        running = session.get(Job, leased)
        assert running.status == "leased" and running.cancel_requested and running.attempts == 1
        assert session.get(Job, unrelated).cancel_requested is False
        complete_job(session, leased, worker_id="worker", attempt=1)
        session.commit()
        assert session.get(Job, leased).status == "cancelled"
    token = CLIENT_REQUEST_ID.set("turn-to-stop")
    try:
        with pytest.raises(RuntimeError, match="chat_work_stopped"):
            queued()
    finally:
        CLIENT_REQUEST_ID.reset(token)
    assert queued()  # A corrected request is not blocked.


def test_stop_routes_stay_service_only(founder):
    from fastapi import FastAPI
    from tests.company_os_harness import company_os_test_client as TestClient
    from app.api.jobs_api import router
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        assert client.get(f"/ventures/{founder['vid']}/chat/work", params={"conversation_id": founder["cid"]}).status_code == 200
        assert client.post(f"/ventures/{founder['vid']}/chat/stop", json={"conversation_id": founder["cid"], "nonce": "turn"}).status_code == 200
        assert client.get(f"/ventures/{founder['vid']}/chat/work", params={"conversation_id": "cnv_missing"}).status_code == 404
        assert client.post(f"/ventures/{founder['vid']}/chat/stop", json={"conversation_id": "cnv_missing", "nonce": "turn"}).status_code == 404


@pytest.mark.parametrize("audience", ["warehouse coordinators", "home gardeners"])
def test_http_requested_revision_starts_once_and_stop_cancels_its_job(founder, audience):
    from fastapi import FastAPI
    from tests.company_os_harness import company_os_test_client as TestClient
    from app.api import chat_api, jobs_api
    from app.core.local_mode import is_local
    from app.db.models import Job, Venture
    from app.state import db
    from app.ventures.current_idea import current_revision, revision_fields
    from core.assertion import sign_assertion

    def headers(path):
        return {"X-Subject": founder["actor"], "X-COS-Assertion": sign_assertion(
            "founder-controls-test-secret-at-least-32-bytes", sub=founder["actor"], email="", role="user", path=path, method="POST")}

    app = FastAPI()
    app.include_router(chat_api.router)
    app.include_router(jobs_api.router)
    with TestClient(app) as client:
        path = f"/ventures/{founder['vid']}/chat"
        body = {"message": f"change the target customer to {audience} and rerun the research",
                "conversation_id": founder["cid"], "idempotency_key": "requested-research"}
        first = client.post(path, headers=headers(path), json=body)
        assert first.status_code == 200, first.text
        assert "fresh research is queued" in first.json()["body_md"]
        assert not any(action.get("confirmation") for action in first.json()["actions"])
        links = [action["href"] for action in first.json()["actions"] if action.get("href")]
        assert links == ([f"/v/{founder['vid']}/research"] if is_local() else [])
        replay = client.post(path, headers=headers(path), json=body)
        assert replay.status_code == 200 and replay.json() == first.json()
        jobs = client.get(path + "/work", params={"conversation_id": founder["cid"]}).json()["items"]
        assert len(jobs) == 1
        stopped = client.post(path + "/stop", json={"conversation_id": founder["cid"], "nonce": "requested-research"})
        assert stopped.status_code == 200, stopped.text
        with db()() as session:
            row = session.get(Job, jobs[0]["id"])
            assert row.cancel_requested and json.loads(row.payload_json)["_chat_request_nonce"] == "requested-research"
            assert session.scalar(select(Job.id).where(Job.id != row.id, Job.kind == "research.deep")) is None
            revision = current_revision(session.get(Venture, founder["vid"]), session)
            assert revision.id != founder["original"] and revision_fields(revision)["customer"] == audience


def test_delayed_stop_never_cancels_corrected_work(founder):
    from app.chat.store import CLIENT_REQUEST_ID
    from app.chat.work_control import work
    from app.core.jobs import enqueue
    from app.db.models import Job
    from app.state import db
    ids = []
    for nonce in ("old-turn", "corrected-turn"):
        token = CLIENT_REQUEST_ID.set(nonce)
        try:
            with db()() as session:
                ids.append(enqueue(session, "research.deep", {}, workspace_id=founder["wid"], venture_id=founder["vid"]))
                session.commit()
        finally:
            CLIENT_REQUEST_ID.reset(token)
    work(founder["vid"], founder["cid"], stop=True, nonce="old-turn", job_ids=[])
    work(founder["vid"], founder["cid"], stop=True, nonce="old-turn", job_ids=[ids[0]])
    with db()() as session:
        assert session.get(Job, ids[0]).cancel_requested
        assert not session.get(Job, ids[1]).cancel_requested


@pytest.mark.parametrize("idea,customer", [("Stock counts for pharmacies", "pharmacy operators"),
                                           ("Trail planning for hikers", "weekend hikers")])
@pytest.mark.parametrize("text", ["what to do next?", "what should I do next?", "what's next?"])
def test_next_moves_reply_does_not_wait_for_or_replace_running_research(founder, monkeypatch, idea, customer, text):
    from app.chat import llm_router, router
    from app.chat.store import CLIENT_REQUEST_ID
    from app.chat.work_control import work
    from app.core.jobs import enqueue, lease
    from app.db.models import Job, Venture
    from app.db.supymem_models import IdeaSession
    from app.idea.service import append_revision
    from app.state import db

    def no_model_or_general_context(*args, **kwargs):
        pytest.fail("next-moves requests must not wait for a provider or general chat context")

    monkeypatch.setattr(llm_router, "route", no_model_or_general_context)
    monkeypatch.setattr(router, "assemble", no_model_or_general_context)
    token = CLIENT_REQUEST_ID.set("research-turn")
    try:
        with db()() as session:
            session.get(Venture, founder["vid"]).raw_idea = idea
            current = session.get(IdeaSession, "ids_controls")
            revision = append_revision(session, current, {"summary": idea, "customer": customer}, source="founder")
            current.confirmed_revision_id = revision.id
            job_id = enqueue(session, "research.deep", {"idea": idea}, workspace_id=founder["wid"],
                             venture_id=founder["vid"], dedupe_key="research-under-test")
            session.commit()
            lease(session, "worker", kinds=["research.deep"])
            session.commit()
            row = session.get(Job, job_id)
            original = (row.payload_json, row.locked_by, row.locked_at, row.started_at, row.attempts)
    finally:
        CLIENT_REQUEST_ID.reset(token)
    token = CLIENT_REQUEST_ID.set("next-turn")
    try:
        reply = asyncio.run(router.handle_message(founder["vid"], text, actor_subject=founder["actor"]))
    finally:
        CLIENT_REQUEST_ID.reset(token)
    assert reply["kind"] == "next_moves" and reply["body_md"] and len(reply["actions"]) == 1
    assert {item["id"] for item in work(founder["vid"], founder["cid"])["items"]} == {job_id}
    work(founder["vid"], founder["cid"], stop=True, nonce="next-turn", job_ids=[])
    with db()() as session:
        jobs = session.scalars(select(Job).where(Job.kind == "research.deep")).all()
        assert [row.id for row in jobs] == [job_id]
        row = jobs[0]
        assert row.status == "leased" and not row.cancel_requested
        assert (row.payload_json, row.locked_by, row.locked_at, row.started_at, row.attempts) == original


def test_stop_cancels_parent_task_without_replaying_its_steps(founder):
    from app.chat.work_control import work
    from app.db.models import Job
    from app.db.task_models import TaskRun, TaskStep
    from app.state import db
    from app.tasks.contracts import Contract
    from app.tasks.service import create
    with db()() as session:
        task = create(session, workspace_id=founder["wid"], venture_id=founder["vid"], owner_person_id=founder["actor"],
            contract=Contract(title="Waitlist summary", goal="Summarize waitlist signups",
                              steps=[{"title": "Summarize", "skill": "tasks.waitlist_summary"}]))
        task_id = task.id
        run = session.scalar(select(TaskRun).where(TaskRun.task_id == task_id))
        run_id, jid = run.id, run.control_job_id
        session.commit()
    work(founder["vid"], founder["cid"], stop=True, job_ids=[jid])
    with db()() as session:
        from app.db.task_models import Task
        assert session.get(Task, task_id).state == "canceled"
        assert session.get(TaskRun, run_id).state == "canceled"
        assert session.scalar(select(TaskStep.state).where(TaskStep.run_id == run_id)) == "canceled"
        assert session.get(Job, jid).cancel_requested


def test_unknown_rerun_never_replays_the_start_call(founder, monkeypatch):
    from app.chat import research_revision
    from app.research import service
    calls = []
    def uncertain(*args, **kwargs):
        calls.append(kwargs)
        raise OSError("lost acknowledgement")
    monkeypatch.setattr(service, "start", uncertain)
    offered = research_revision.prepare(founder["vid"], "the price is $49, redo research", founder["actor"])
    payload = offered["actions"][0]["confirmation"]["resulting_state"]
    with pytest.raises(OSError):
        research_revision.confirm(founder["vid"], founder["actor"], payload)
    retry = research_revision.confirm(founder["vid"], founder["actor"], payload)
    assert len(calls) == 1 and retry["kind"] == "needs_you" and retry["actions"]


def test_read_only_founder_cannot_cancel_saved_work(founder):
    from app.chat.work_control import work
    from app.db.models import WorkspaceMember
    from app.state import db
    with db()() as session:
        membership = session.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == founder["wid"],
                                                                 WorkspaceMember.person_id == founder["actor"]))
        membership.role = "viewer"
        session.commit()
    with pytest.raises(PermissionError, match="request_work_required"):
        work(founder["vid"], founder["cid"], stop=True, nonce="forbidden-stop")

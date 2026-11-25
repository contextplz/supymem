import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from tests.company_os_harness import company_os_test_client, load_company_os_app, local_chat as local_chat
from tests.unit.test_local_mode import local_owner_app


@pytest.mark.parametrize("idea", ["A purchasing checklist for workshops", "A plant care diary for apartment gardeners"])
def test_local_idea_page_routes_accept_your_requests_without_hosted_identity(local_owner_app, monkeypatch, idea):
    from app.api import experiments_api, idea_api
    from app.db.supymem_models import ClarifierTurn, IdeaRevision, IdeaSession, ValidationRun
    from app.idea import validation
    from app.state import db
    from core.clock import utcnow

    with company_os_test_client(local_owner_app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        venture = client.post("/ventures", json={"raw_idea": idea, "start": False}).json()
        venture_id, workspace_id = venture["id"], venture["workspace_id"]
        session_id = "local_idea_session"
        now = utcnow()
        with db()() as session:
            session.add(IdeaSession(id=session_id, venture_id=venture_id, workspace_id=workspace_id,
                case="know", state="open", created_at=now, updated_at=now))
            session.flush()
            session.add(IdeaRevision(id="revision", session_id=session_id, venture_id=venture_id,
                workspace_id=workspace_id, rev=1, source="founder", summary=idea, created_at=now))
            session.flush()
            session.add(ClarifierTurn(id="question", session_id=session_id, venture_id=venture_id,
                workspace_id=workspace_id, revision_id="revision", turn=1, role="agent", kind="question", created_at=now))
            session.add(ValidationRun(id="run", session_id=session_id, venture_id=venture_id,
                workspace_id=workspace_id, revision_id="revision", method="board", created_at=now))
            session.commit()
        saved = {"id": session_id, "venture_id": venture_id, "workspace_id": workspace_id, "state": "open", "turns": []}
        for name in ("open_session", "get_session", "post_message", "retry_failed_clarification",
                     "review_clarification_manually", "set_case", "start_direction_correction",
                     "start_confirmed_answer_correction", "reopen_assumed_questions", "patch_revision",
                     "decide_validation", "confirm"):
            monkeypatch.setattr(idea_api, name, lambda *args, **kwargs: saved)
        monkeypatch.setattr(validation, "start_validation", lambda *args, **kwargs: saved)
        monkeypatch.setattr(idea_api, "list_revisions", lambda *args: [])
        monkeypatch.setattr(experiments_api, "start_proposed", lambda vid, test: {"id": "experiment", "state": "running"})
        base = f"/ventures/{venture_id}"
        for path in (base, base + "/plans", base + "/idea-page", base + "/idea/sessions"):
            response = client.get(path)
            assert response.status_code == 200, (path, response.text)
        assert client.post(base + "/experiments/start", json={"hypothesis": idea, "action": "Ask your users about their current workflow", "success_measure": "Record their answers"}).status_code == 200
        assert client.post(base + "/idea/sessions").status_code == 201
        path = base + f"/idea/sessions/{session_id}"
        assert client.get(path).status_code == 200
        for method, suffix, body in [
            ("POST", "/messages", {"text": idea}),
            ("POST", "/retry-clarification", {}),
            ("POST", "/manual-review", {"revision_id": "revision", "patch": {"offer": idea}}),
            ("POST", "/case", {"case": "know"}),
            ("POST", "/correct-direction", {"case": "know", "revision_id": "revision"}),
            ("POST", "/reopen-questions", {"revision_id": "revision"}),
            ("POST", "/correct-answer", {"revision_id": "revision", "question_id": "question", "text": idea, "patch": {"offer": idea}}),
            ("PATCH", "/revision", {"patch": {"offer": idea}}),
            ("POST", "/validate", {"revision_id": "revision"}),
            ("POST", "/validations/run/decide", {"decision": "accepted"}),
            ("POST", "/confirm", {"revision_id": "revision", "start_research": False}),
            ("GET", "/revisions", None),
        ]:
            response = client.request(method, path + suffix, json=body)
            assert response.status_code in {200, 201}, (suffix, response.text)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    monkeypatch.setenv("IDEA_PAGE_REFRESH", "on")
    load_company_os_app()
    from app import state
    from app.api.idea_page_api import router
    from app.authz import workspace
    from app.db.models import GtmTouch, Job, Person, Venture, VentureDocument, Workspace, WorkspaceMember
    from app.credits import policy
    from app.experiments import ledger
    from app.ventures import idea_page, lifecycle
    from core.clock import utcnow

    engine = create_engine(f"sqlite:///{tmp_path}/idea.db")
    from app.db.local_sqlite import migrate_sqlite

    with monkeypatch.context() as local:
        local.setenv("SUPYMEM_MODE", "local")
        local.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
        migrate_sqlite(str(engine.url))
    factory = sessionmaker(engine)
    monkeypatch.setattr(state, "_engine", engine)
    monkeypatch.setattr(state, "_factory", factory)
    monkeypatch.setattr(policy, "session_factory", lambda: factory)
    for module in (state, workspace, idea_page, lifecycle):
        monkeypatch.setattr(module, "db", lambda: factory)
    monkeypatch.setattr(ledger, "list_for_venture", lambda venture_id: [
        {"hypothesis": "Clinic managers will reply to a problem interview", "state": "concluded", "outcome": "supported",
         "result_text": "3 of 10 replied", "learning": "", "source": "validation"},
        {"hypothesis": "Landing page gets 20 waitlist signups", "state": "proposed", "outcome": None,
         "result_text": "", "learning": "", "source": "research"}])
    now = utcnow()
    with factory() as session:
        for owner, workspace_id, venture_id in [("usr_a", "wsp_a", "ven_a"), ("usr_b", "wsp_b", "ven_b")]:
            session.add(Person(id=owner, display_name=owner, created_at=now))
            session.add(Workspace(id=workspace_id, kind="company", name=workspace_id, entry_mode="idea", owner_id=owner,
                                  created_at=now, updated_at=now))
            session.add(Venture(id=venture_id, workspace_id=workspace_id, name=venture_id, raw_idea="Clinic scheduling",
                                stage="draft", created_at=now, updated_at=now))
        for direction in ("out", "out", "in"):
            session.add(GtmTouch(id=f"t_{direction}_{len(session.new)}", workspace_id="wsp_a", venture_id="ven_a",
                                 contact_id="c", direction=direction, simulated=0, created_at=now))
        session.add(GtmTouch(id="t_sim", workspace_id="wsp_a", venture_id="ven_a", contact_id="c", direction="out",
                             simulated=1, created_at=now))
        session.commit()
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, factory=factory, module=idea_page, job=Job)
    engine.dispose()


BRIEF = {"idea": {"name": "Clinic", "raw_idea": "Scheduling for small dental clinics", "audience": "Dental clinic managers"},
         "idea_page": "Current idea page (updated old): stale"}


def _stub_model(monkeypatch, reply):
    from app.credits import metered
    from app.ventures import state_brief
    calls = []

    async def llm(messages, **kwargs):
        calls.append((messages, kwargs))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=reply))])

    monkeypatch.setattr(metered, "llm", llm)
    monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda venture_id, force=False: dict(BRIEF))
    return calls


@pytest.mark.parametrize("domain,goal,audience,problem", [
    ("invoice-harbor.io", "Invoice Harbor needs more paying customers", "Finance teams at manufacturers", "Supplier bills need duplicate entry"),
    ("pantry-bloom.app", "Help me increase revenue", "Home cooks planning weekly dinners", "Planning dinner takes too long"),
])
def test_model_cannot_store_your_growth_goal_as_the_customer_problem(env, monkeypatch, domain, goal, audience, problem):
    from app.ventures import state_brief
    for website_read in (False, True):
        calls = _stub_model(monkeypatch, json.dumps({"summary": f"You run {domain}", "problem": goal,
                                                   "customers": {"current": []}}))
        known = {"desired_outcome": goal, **({"audience": audience, "problem": problem} if website_read else {})}
        monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda vid, force=False: {"idea": known})
        asyncio.run(env.module.refresh({"venture_id": "ven_a", "_requested": True}))
        page = env.module.read("ven_a")["page"]
        assert page["problem"] == (problem if website_read else "unknown")
        assert page["customers"]["current"] == ([audience] if website_read else [])
        assert ("What problem does your product solve for your customers?" in page["open_questions"]) is not website_read
        assert "is not the customer problem" in calls[0][0][0]["content"]


def test_generation_uses_saved_state_marks_unknowns_and_never_invents_experiments(env, monkeypatch):
    calls = _stub_model(monkeypatch, "Here: " + json.dumps({
        "summary": "Scheduling for small dental clinics.", "customers": {"current": ["Dental clinic managers"], "alternatives": []},
        "problem": "", "offer": None, "open_questions": ["Will clinics pay?"], "what_changed": [],
        "experiments": [{"name": "Invented 500 signups", "status": "done"}]}))
    asyncio.run(env.module.refresh({"venture_id": "ven_a", "_requested": True}))
    messages, kwargs = calls[0]
    assert (kwargs["purpose"], kwargs["tier"], kwargs["venture_id"]) == ("venture.chat.assistant", "flash", "ven_a")
    assert "Never invent" in messages[0]["content"] and '"unknown"' in messages[0]["content"]
    sent = json.loads(messages[1]["content"])
    assert "Dental clinic managers" in sent["saved_state"] and "stale" not in sent["saved_state"]
    page = env.module.read("ven_a")["page"]
    assert page["summary"] == "Scheduling for small dental clinics."
    assert page["customers"] == {"current": ["Dental clinic managers"], "alternatives": []}
    assert page["problem"] == page["offer"] == "unknown"
    names = [row["name"] for row in page["experiments"]]
    assert "Invented 500 signups" not in names
    assert page["experiments"][0] == {"name": "Clinic managers will reply to a problem interview", "source": "Your validation review",
                                      "status": "Complete", "result": "Worked: 3 of 10 replied"}
    assert page["experiments"][1]["status"] == "Planned" and page["experiments"][1]["result"] == "unknown"
    assert page["experiments"][-1]["result"] == "2 sent, 1 reply"  # simulated touches excluded


@pytest.mark.parametrize("idea", ["Freight invoices for dispatchers", "Weekend watercolor lessons"])
def test_idea_history_labels_stopped_experiments_without_changing_results(env, monkeypatch, idea):
    from app.db.supymem_models import Experiment
    from app.experiments import ledger

    monkeypatch.setattr(ledger, "list_for_venture", lambda venture_id: [
        {"hypothesis": f"Another test for {idea}", "state": "future_state", "outcome": "future_outcome",
         "result_text": "Saved notes.", "source": "future_source"},
    ])
    with env.factory() as session:
        session.add(Experiment(id="stopped_outreach", venture_id="ven_a", workspace_id="wsp_a", kind=ledger.KIND,
                               state="aborted", hypothesis=idea, context=json.dumps({"runner": "system",
                                   "spec": {"source": "charlie_generated", "runner": "outreach_split"},
                                   "result": {"outcome": "inconclusive", "insight": "Two replies came in."}})))
        session.commit()
    env.module.save("ven_a", {"summary": idea, "experiments": [{"name": idea, "status": "done",
        "source": "charlie_generated", "result": "inconclusive: Two replies came in."}]})
    history = {row["name"]: row for row in env.module.read("ven_a")["page"]["experiments"]}
    stopped, unknown = history[idea], history[f"Another test for {idea}"]
    assert stopped == {"name": idea, "status": "Stopped", "source": "Suggested by Charlie",
                       "result": "Too early to tell: Two replies came in."}
    assert unknown["status"] == "Status not recorded" and unknown["source"] == "Saved experiment"
    assert unknown["result"] == "Saved notes."


def test_unusable_model_reply_keeps_the_last_good_page(env, monkeypatch):
    env.module.save("ven_a", {"summary": "Old good page"})
    _stub_model(monkeypatch, "not json")
    asyncio.run(env.module.refresh({"venture_id": "ven_a", "_requested": True}))
    assert env.module.read("ven_a")["page"] == {"summary": "Old good page"}


def test_unsure_answers_stay_open_in_generated_and_saved_summaries(env, monkeypatch):
    from app.idea.agent import Understanding, summary_without_unsure
    from app.ventures import current_idea, state_brief
    for venture_id, audience in [("ven_a", "Freight dispatchers"), ("ven_b", "Families sharing recipes")]:
        known = f"You want to help {audience}."
        for answer in ["idk", "not sure", "no idea", "dunno", "I don't know"]:
            quoted = f'{known} You stated "{answer}" about when the problem starts. You have not tested demand.'
            expected = f"{known} This point is still open. You have not tested demand."
            assert Understanding(summary=quoted).summary == expected
            with monkeypatch.context() as patch:
                patch.setattr(current_idea, "current_idea", lambda *args, **kwargs: quoted)
                assert current_idea.idea_summary(None) == expected
            calls = _stub_model(monkeypatch, json.dumps({"summary": quoted}))
            monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda vid, force=False: {
                "idea": {"name": audience, "raw_idea": known, "audience": audience}})
            asyncio.run(env.module.refresh({"venture_id": venture_id, "_requested": True}))
            assert env.module.read(venture_id)["page"]["summary"] == expected
            assert "Unsure answers" in calls[0][0][0]["content"]
            assert audience in json.loads(calls[0][0][1]["content"])["saved_state"]
            env.module.save(venture_id, {"summary": quoted})
            assert env.module.read(venture_id)["page"]["summary"] == expected
        assert Understanding(summary="You are not sure which price to test.").summary == "You are not sure which price to test."
        for opening, closing in [("'", "'"), ('"', '"'), ("“", "”"), ("‘", "’")]:
            text = f"Customer: {audience}\nYou stated {opening}idk{closing} about demand."
            assert summary_without_unsure(text) == f"Customer: {audience}\nThis point is still open."
        text = f"Customer: {audience}\nYou stated ‘I’m not sure’ about demand."
        assert summary_without_unsure(text) == f"Customer: {audience}\nThis point is still open."


def test_refreshes_are_debounced_to_one_trailing_job_per_window(env):
    start = datetime(2026, 10, 22, 10, 0, 10, tzinfo=timezone.utc)
    with env.factory() as session:
        for offset in (0, 30, 200):
            env.module.schedule(session, "wsp_a", "ven_a", actor="usr_a", now=start + timedelta(seconds=offset), requested=True)
        session.commit()
        jobs = session.scalars(select(env.job)).all()
        assert len(jobs) == 1 and jobs[0].kind == "idea.page_refresh"
        assert jobs[0].run_after.replace(tzinfo=timezone.utc) == datetime(2026, 10, 22, 10, 5, tzinfo=timezone.utc)
        env.module.schedule(session, "wsp_a", "ven_a", actor="usr_a", now=start + timedelta(seconds=300), requested=True)
        env.module.schedule(session, "wsp_a", "ven_a", actor=None, now=start + timedelta(seconds=900), requested=True)  # no actor: skipped
        session.commit()
        assert len(session.scalars(select(env.job)).all()) == 2


def test_finished_research_schedules_a_refresh(env, monkeypatch):
    from app.core.jobs import complete_job
    monkeypatch.setattr("app.tasks.locks.lock_for_job", lambda session, job_id: None)
    monkeypatch.setattr("app.tasks.service.after_job", lambda session, job_id, **kwargs: None)
    from core.clock import utcnow
    with env.factory() as session:
        session.add(env.job(id="job_r", kind="research.deep", venture_id="ven_a", workspace_id="wsp_a", status="running",
                            payload_json=json.dumps({"_actor_subject": "usr_a"}), priority=0, attempts=1, max_attempts=1,
                            timeout_s=900, created_at=utcnow()))
        session.commit()
        complete_job(session, "job_r")
        session.commit()
        from app.core.local_mode import is_local
        expected = [] if is_local() else ["idea.page_refresh"]
        assert [job.kind for job in session.scalars(select(env.job).where(env.job.kind == "idea.page_refresh"))] == expected


@pytest.mark.parametrize("audience", ["Manufacturing procurement teams", "Apartment gardening groups"])
def test_local_idea_refresh_only_runs_when_requested(env, monkeypatch, audience):
    from app.core.local_secrets import update_settings
    from app.ventures import state_brief
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    update_settings("capabilities", {"background_work": False}, {})
    calls = _stub_model(monkeypatch, json.dumps({"summary": f"You help {audience}."}))
    monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda *args, **kwargs: {
        "idea": {"name": audience, "audience": audience, "raw_idea": f"You help {audience}."}})
    with env.factory() as session:
        assert not env.module.schedule(session, "wsp_a", "ven_a", actor="usr_a")
    assert not env.module.request_refresh("ven_a", actor="usr_a")
    asyncio.run(env.module.refresh({"venture_id": "ven_a"}))
    assert not calls and env.module.read("ven_a") is None
    asyncio.run(env.module.refresh({"venture_id": "ven_a", "_requested": True}))
    assert len(calls) == 1 and env.module.read("ven_a")["page"]["summary"] == f"You help {audience}."
    assert env.module.request_refresh("ven_a", actor="usr_a", requested=True)


def test_api_requires_identity_and_workspace_membership(env):
    get = lambda subject, venture="ven_a": env.client.get(f"/ventures/{venture}/idea-page",
                                                          headers={"X-Subject": subject} if subject else {})
    assert get(None).status_code == 401
    assert get("usr_b").status_code == 404
    assert get("usr_a").json() == {"venture_id": "ven_a", "page": None, "updated_at": None}
    env.module.save("ven_a", {"summary": "Saved"})
    body = get("usr_a").json()
    assert body["page"] == {"summary": "Saved"} and body["updated_at"]
    assert get("usr_a").headers["cache-control"] == "private, no-store"


def test_charlie_context_includes_the_current_idea_page(env):
    from app.ventures.state_brief import render_brief_for_llm
    assert env.module.context_line("ven_a") == ""
    env.module.save("ven_a", {"summary": "Scheduling for dental clinics", "customers": {"current": ["Clinic managers"]},
                              "problem": "Missed appointments", "offer": "unknown", "open_questions": ["Will they pay?"]})
    line = env.module.context_line("ven_a")
    rendered = render_brief_for_llm({"idea": {"name": "Clinic"}, "idea_page": line})
    assert "Current idea page" in rendered and "Clinic managers" in rendered and "Will they pay?" in rendered


@pytest.mark.parametrize("domain", ["dental clinics", "public libraries"])
def test_page_preserves_new_writes_and_serves_historical_copy(env, domain):
    from app.db.models import VentureDocument
    from app.gtm.relationships.email_compliance import NO_DASH_RULE
    from core.clock import utcnow

    text = f"Founder wants to help {domain}—the founder hasn't named an offer."
    expected = f"You want to help {domain}, you haven't named an offer."
    page = {"summary": text, "problem": "The founder’s problem–still untested", "offer": "unknown",
            "customers": {"current": [domain], "alternatives": []}, "open_questions": [text], "what_changed": [text],
            "experiments": [{"name": text, "source": "validation", "status": "planned", "result": "2–3 responses"}]}
    with env.factory() as session:
        session.add(VentureDocument(id="idea_page:ven_a", venture_id="ven_a", kind="idea_page", title="Idea",
                                   body_md=json.dumps(page), created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    served = env.module.read("ven_a")["page"]
    assert served["summary"] == served["open_questions"][0] == expected
    assert served["experiments"][0]["name"] == expected
    assert served["experiments"][0]["result"] == "2-3 responses"
    assert env.client.get("/ventures/ven_a/idea-page", headers={"X-Subject": "usr_a"}).json()["page"] == served
    env.module.save("ven_a", page)
    with env.factory() as session:
        assert json.loads(session.get(VentureDocument, "idea_page:ven_a").body_md) == page
    assert env.module.normalize(page, page["experiments"]) == page
    assert env.module.read("ven_a")["page"] == served
    assert NO_DASH_RULE in env.module.PROMPT and '"you"' in env.module.PROMPT


def test_what_we_know_links_both_ledgers_without_changing_the_idea(env, monkeypatch):
    from app.chat import experiments
    from app.db.supymem_models import Experiment
    from app.experiments import ledger
    from core.clock import utcnow

    monkeypatch.setattr(ledger, "db", lambda: env.factory)
    for venture_id, workspace_id, audience in (("ven_a", "wsp_a", "freight dispatchers"),
                                               ("ven_b", "wsp_b", "weekend hikers")):
        env.module.save(venture_id, {"summary": f"An offer for {audience}.", "problem": "Unchanged problem"})
        with env.factory() as session:
            session.add(Experiment(id=f"{venture_id}_manual", venture_id=venture_id, workspace_id=workspace_id,
                kind=ledger.KIND, state="concluded", hypothesis=f"{audience} will pay", outcome="refuted",
                learnings="0 commitments from 10 conversations", ended_at=utcnow()))
            session.add(Experiment(id=f"{venture_id}_system", venture_id=venture_id, workspace_id=workspace_id,
                kind=ledger.KIND, state="concluded", hypothesis=f"{audience} describe the problem", ended_at=utcnow(),
                context=json.dumps({"runner": "system", "spec": {"question": "Q1"},
                                    "result": {"outcome": "supported", "insight": "3 qualifying public sources"}})))
            session.add(Experiment(id=f"{venture_id}_open", venture_id=venture_id, workspace_id=workspace_id,
                kind=ledger.KIND, state="proposed", hypothesis="Not run yet"))
            session.commit()
        page = env.module.read(venture_id)["page"]
        assert page["summary"] == f"An offer for {audience}." and page["problem"] == "Unchanged problem"
        assert len(page["what_we_know"]) == 2
        assert all(audience in row["text"] and row["href"].startswith(f"/v/{venture_id}/experiments?experiment_id=")
                   for row in page["what_we_know"])
        assert any("3 qualifying public sources" in row["text"] for row in page["what_we_know"])
        assert any("0 commitments from 10 conversations" in row["text"] for row in page["what_we_know"])
        for result in page["what_we_know"]:
            experiment_id = result["href"].split("experiment_id=")[1]
            reply = asyncio.run(experiments.execute(venture_id, "experiment.list", {"experiment_id": experiment_id}, None))
            assert reply["kind"] == "experiment_result"
        missing = asyncio.run(experiments.execute(venture_id, "experiment.list", {"experiment_id": "other_venture"}, None))
        assert "could not find" in missing["body_md"]

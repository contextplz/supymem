"""Persisted projection respects tenant scope, attention and pause precedence."""
import json
from datetime import timedelta
import pytest
from sqlalchemy.orm import sessionmaker
from tests.company_os_harness import load_company_os_app


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    load_company_os_app()
    from app.db.models import Base, Venture
    from app.db.session import make_engine
    from app.ventures import run_status
    engine = make_engine(f"sqlite:///{tmp_path}/status.db")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    from core.clock import utcnow
    with factory() as session:
        session.add(Venture(id="v1", workspace_id="w1", name="Test", raw_idea="Test idea", stage="idea", created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    monkeypatch.setattr(run_status, "db", lambda: factory)
    return factory, run_status


def add_job(factory, *, id="job_test", venture="v1", workspace="w1", status="leased", age=0, kind="idea.clarify_turn"):
    from app.db.models import Job
    from core.clock import utcnow
    with factory() as session:
        session.add(Job(id=id, kind=kind, venture_id=venture,
                        workspace_id=workspace, payload_json='{"secret":"never expose"}',
                        status=status, created_at=utcnow()+timedelta(seconds=age),
                        last_error="private provider detail" if status == "failed_dead" else None))
        session.commit()


def test_dashboard_exposes_active_research_progress_from_saved_receipts(env):
    factory, module = env
    from app.db.models import Job
    from app.db.supymem_models import ResearchDossier
    from app.research.execution import identifier
    from core.clock import utcnow
    add_job(factory, kind="research.deep")
    for index in range(6):
        add_job(factory, id=f"newer_job_{index}", age=index + 1)
    with factory() as session:
        session.get(Job, "job_test").started_at = utcnow()
        session.add(ResearchDossier(id=identifier("job_test"), workspace_id="w1", venture_id="v1",
            revision_id="revision", kind="deep", state="running", budget_calls=10, calls_used=1,
            created_at=utcnow(), summary=json.dumps({"_runtime": {"job_id": "job_test",
                "progress_plan": ["prior_category", "prior_complaints", "review"],
                "operations": {"prior_category": {"state": "completed"},
                               "prior_complaints": {"state": "calling"}}}})))
        session.commit()
    result = module.snapshot("v1", "w1")
    assert result["research_progress"]["percent"] == 33
    assert result["research_progress"]["step_label"] == "Reading customer complaints"
    assert result["research_progress"]["started_at"]
    assert module.snapshot("v2", "w1").get("research_progress") is None


def test_dashboard_exposes_find_customers_progress_without_internal_notes(env):
    factory, module = env
    from app.db.models import VentureDocument
    from core.clock import utcnow
    add_job(factory, kind="gtm.journey.prepare")
    with factory() as session:
        session.add(VentureDocument(id="gtm_progress", venture_id="v1", kind="gtm_journey", title="Find customers",
            provenance_json=json.dumps({"job_id": "job_test", "state": "running", "_runtime": {"progress_step": "searching",
                "progress_plan": {"planning": ["discovery-plan"], "searching": ["search-a", "search-b"]},
                "operations": {"discovery-plan": {"state": "completed"}, "search-a": {"state": "calling", "private": "secret"}}}}),
            created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    progress = module.snapshot("v1", "w1")["research_progress"]
    from app.core.local_mode import is_local
    assert progress["percent"] == 10 and progress["step_label"] == ("Searching..." if is_local() else "Searching public sources")
    assert progress["steps"]["searching"] == {"planned": 2, "completed": 0}
    assert "secret" not in json.dumps(progress)
    assert set(progress) == {"state", "percent", "run_id", "step_label", "started_at", "steps"}
    from app.gtm.progress import compute_progress
    with factory() as session:
        document = session.get(VentureDocument, "gtm_progress")
        data = json.loads(document.provenance_json)
        data["_runtime"]["progress_percent"] = 55
        document.provenance_json = json.dumps(data)
        session.commit()
    assert module.snapshot("v1", "w1")["research_progress"] == compute_progress(data)
    assert compute_progress(data)["percent"] == 55


@pytest.mark.parametrize("status,expected", [("scheduled", "queued"), ("running", "working"), ("retry_wait", "queued")])
def test_active_page_build_supersedes_older_failed_page(env, status, expected):
    factory, module = env
    from app.db.models import Job
    add_job(factory, id="previous", kind="pagebuilder.build", status="failed_dead")
    add_job(factory, id="retry", kind="pagebuilder.build", status=status, age=10)
    with factory() as session:
        session.get(Job, "previous").last_error = "pagebuilder_plan_invalid"
        session.commit()
    result = module.snapshot("v1", "w1")
    assert result["state"] == expected
    assert result["job_id"] == "retry"
    assert result["stage"] == "Landing page"
    assert "failed" not in result["next_step"].lower()
    assert result["action"]["href"] == "/v/v1/site"


@pytest.mark.parametrize("requeued", [False, True])
@pytest.mark.parametrize("matching_revision", [False, True])
def test_page_retry_retires_old_handoff_banner_not_saved_failure(env, requeued, matching_revision):
    factory, module = env
    from app.db.models import Job, Person, Workspace
    from app.db.task_models import Task, TaskRun, TaskStep
    from app.tasks.contracts import blocker
    from core.clock import utcnow

    now = utcnow()
    earlier = now - timedelta(minutes=5)
    add_job(factory, id="previous", kind="pagebuilder.build", status="scheduled" if requeued else "failed_dead", age=-300)
    if not requeued:
        add_job(factory, id="retry", kind="pagebuilder.build", status="scheduled")
    with factory() as session:
        session.add(Person(id="p1", display_name="Founder", created_at=now))
        session.add(Workspace(id="w1", kind="venture", name="Test", entry_mode="idea", owner_id="p1",
            created_at=now, updated_at=now))
        session.get(Job, "previous").payload_json = json.dumps({"revision_id": "r1"})
        if not requeued:
            session.get(Job, "retry").payload_json = json.dumps({"revision_id": "r1" if matching_revision else "r2"})
        session.add(Task(id="old_task", workspace_id="w1", venture_id="v1", owner_person_id="p1", title="Build page",
            goal="Build page", state="input_required", blocker_json=json.dumps(blocker("pagebuilder_plan_invalid")),
            scope_json=json.dumps({"steps": [{"skill": "pagebuilder.build", "inputs": {"revision_id": "r1"}}]}),
            deadline_at=now, created_at=earlier, updated_at=earlier))
        session.flush()
        session.add(TaskRun(id="old_run", task_id="old_task", run_key="previous", state="input_required",
            reserved_cost_micro=123, deadline_at=now, started_at=earlier))
        session.flush()
        session.add(TaskStep(id="old_step", run_id="old_run", position=0, title="Build page",
            skill="pagebuilder.build", job_id="previous", state="failed", deadline_at=now))
        session.commit()
    with factory() as session:
        assert module._superseded_handoff(session, session.get(Task, "old_task")) is (requeued or matching_revision)
    result = module.snapshot("v1", "w1")
    if requeued or matching_revision:
        assert result["state"] == "queued"
        assert result["stage"] == "Landing page"
    else:
        assert result["state"] == "needs_you"
    with factory() as session:
        assert session.get(Task, "old_task").state == "input_required"
        assert session.get(TaskRun, "old_run").reserved_cost_micro == 123


@pytest.mark.parametrize("invalid_ready", [False, True])
@pytest.mark.parametrize("historical_requeue", [False, True])
def test_latest_failed_candidate_is_not_hidden_by_older_ready_landing_attempt(env, monkeypatch, invalid_ready, historical_requeue):
    factory, module = env
    from app.db.models import Job
    from app.db.supymem_models import BuildRun, FounderApp
    from core.clock import utcnow
    monkeypatch.setattr("app.pagebuilder.api._candidate_current", lambda _doc: True)
    now = utcnow()
    add_job(factory, kind="pagebuilder.build", status="failed_dead")
    with factory() as session:
        candidate = session.get(Job, "job_test")
        candidate.last_error = "pagebuilder_candidate_invalid"
        candidate.payload_json = json.dumps({"_runtime": {"state": "ready" if invalid_ready else "failed", "error": None}})
        candidate.finished_at = now + timedelta(seconds=10)
        if historical_requeue:
            candidate.created_at = now - timedelta(minutes=5)
            session.add(Job(id="older_good_candidate", kind="pagebuilder.build", workspace_id="w1", venture_id="v1",
                status="succeeded", created_at=now, payload_json=json.dumps({"_runtime": {"state": "ready"}})))
        session.add(FounderApp(id="landing_old", workspace_id="w1", venture_id="v1", name="Old preview",
            slug="old-preview", kind="landing", created_at=now, updated_at=now))
        session.flush()
        session.add(BuildRun(id="older_qa", app_id="landing_old", workspace_id="w1", venture_id="v1", runtime_version=1,
            trigger="pagebuilder_source_qa", agent="managed", state="succeeded", closed_at=now, created_at=now,
            spec=json.dumps({"artifact_profile": "static_landing_v1", "artifact_verified": True, "qa_state": "passed"})))
        session.commit()
    result = module.snapshot("v1", "w1")
    assert result["state"] == "failed" and result["job_id"] == "job_test"
    assert "source could not be verified" in result["activity"]
    assert "landing_page" not in result
    assert result["action"] == {"label": "Build my page again", "href": "/v/v1/site?build=1"}
    assert "ready" not in result["next_step"]


@pytest.mark.parametrize("state,usable", [("complete", True), ("partial", True), ("partial", False)])
@pytest.mark.parametrize("error", ["research_failed", "job_outcome_uncertain"])
def test_current_report_advances_past_older_research_failure_without_changing_holds(env, monkeypatch, state, usable, error):
    from app.core.local_mode import is_local
    from app.db.models import Job, Person, Workspace
    from app.db.supymem_models import IdeaRevision, IdeaSession, ResearchDossier, ResearchFinding
    from app.db.task_models import Task, TaskRun
    from app.tasks.contracts import blocker
    from app.ventures import next_step, stage
    from app.api.dashboard import _handoff_next_step
    from core.clock import utcnow

    factory, module = env
    monkeypatch.setattr(next_step, "db", lambda: factory)
    monkeypatch.setattr(stage, "db", lambda: factory)
    monkeypatch.setattr("app.pagebuilder.api.enabled", lambda: True)
    monkeypatch.setattr("app.ventures.state_brief._website_section", lambda *_: {"live": False, "waitlist_enabled": False})
    now = utcnow()
    earlier = now - timedelta(minutes=5)
    add_job(factory, kind="research.deep", status="failed_dead", age=-300)
    with factory() as session:
        session.get(Job, "job_test").last_error = error
        session.add(Person(id="p1", display_name="Founder", created_at=now))
        session.add(Workspace(id="w1", kind="venture", name="Test", entry_mode="idea", owner_id="p1",
                              created_at=now, updated_at=now))
        session.add(IdeaSession(id="s1", workspace_id="w1", venture_id="v1", case="know", state="confirmed",
                                current_revision_id="r1", confirmed_revision_id="r1", confirmed_at=now,
                                created_at=now, updated_at=now))
        session.flush()
        session.add(IdeaRevision(id="r1", session_id="s1", workspace_id="w1", venture_id="v1", rev=1,
                                 source="founder", structured="{}", created_at=now))
        session.add(ResearchDossier(id="report", workspace_id="w1", venture_id="v1", revision_id="r1",
                                    kind="deep", state=state, summary='{"review_kind":"retrieved_sources_only"}',
                                    created_at=now, completed_at=now))
        session.add(ResearchDossier(id="old_report", workspace_id="w1", venture_id="v1", revision_id="r1",
                                    kind="deep", state="failed", created_at=earlier))
        session.add(Task(id="stale", workspace_id="w1", venture_id="v1", owner_person_id="p1", title="Old research",
                         goal="Research this idea", state="input_required", blocker_json=json.dumps(blocker(error)),
                         scope_json=json.dumps({"steps": [{"skill": "research.deep", "inputs": {}}]}),
                         deadline_at=now, created_at=earlier, updated_at=earlier))
        session.flush()
        session.add(TaskRun(id="old_run", task_id="stale", run_key="job_test", state="input_required",
                            reserved_cost_micro=123, reserved_tokens=456, deadline_at=now, started_at=earlier))
        if usable:
            session.add(ResearchFinding(id="finding", dossier_id="report", workspace_id="w1", venture_id="v1",
                                       category="direct", title="Saved alternative", source="web", url="https://example.test/alternative",
                                       evidence=json.dumps({"observed_at": now.isoformat(), "evidence_kind": "web"}), created_at=now))
        session.commit()

    facts = next_step._facts("v1")
    step = next_step._founder_resolve("v1", "/v/v1", facts)
    view = module.snapshot("v1", "w1")
    projected = _handoff_next_step(view, step)
    assert facts["dossier_state"] == state
    assert view["research_report_ready"] is usable
    if usable:
        assert facts["research_report_ready"]
        assert (step["key"], step["chat_action"], step["href"]) == (("venture.context", "startup.review", "/v/v1/context")
            if is_local() else ("site.build", None, "/v/v1/site?build=1"))
        assert (view["stage"], view["state"]) == ("Research", "completed")
        assert view["action"]["label"] == "Review research"
        if state == "partial":
            assert view["activity"] == "Research is ready. Review it before planning."
        assert projected["key"] == step["key"]
    else:
        assert not facts["research_report_ready"]
        assert step["key"] != "plan.generate"
        assert projected["key"] == "handoff.needs_you"
    with factory() as session:
        assert session.get(Job, "job_test").last_error == error
        assert session.get(Task, "stale").state == "input_required"
        hold = session.get(TaskRun, "old_run")
        assert (hold.reserved_cost_micro, hold.reserved_tokens) == (123, 456)
    if usable:
        assert module.snapshot("v1", "w1", paused=True)["state"] == "paused"
        add_job(factory, id="current_plan", kind="plan.compile", status="scheduled", age=1)
        assert module.snapshot("v1", "w1")["state"] == "queued"
        assert next_step._founder_resolve("v1", "/v/v1", next_step._facts("v1"))["key"] == "work.running"
        with factory() as session:
            session.get(Job, "current_plan").status = "succeeded"
            session.commit()
        add_job(factory, id="newer_research", kind="research.deep", status="failed_dead", age=1)
        assert module.snapshot("v1", "w1")["state"] == "failed"
    with factory() as session:
        session.get(IdeaSession, "s1").current_revision_id = "r2"
        session.commit()
        assert next_step.current_research_report(session, session.get(IdeaSession, "s1")) is None
    assert module.snapshot("v1", "w1")["stage"] == "Your task"
    assert not module.snapshot("v1", "w1")["research_report_ready"]


def test_active_work_preferred_over_newer_finished_and_cross_tenant(env):
    factory, module = env
    add_job(factory)
    add_job(factory, id="job_finished", status="succeeded", age=1)
    add_job(factory, id="job_other", workspace="w2", age=2)
    view = module.snapshot("v1", "w1")
    assert view["job_id"] == "job_test"
    assert view["state"] == "working"
    assert view["continues_when_closed"] is True
    assert view["progress"] is None
    assert "secret" not in str(view)
    assert module.snapshot("v2", "w1")["job_id"] is None


def test_research_backoff_is_retrying_not_waiting_for_a_worker(env):
    factory, module = env
    add_job(factory, kind="research.deep", status="retry_wait")
    from app.db.models import Job
    with factory() as session:
        session.get(Job, "job_test").last_error = "research_provider_rate_limited:private detail"
        session.commit()
    view = module.snapshot("v1", "w1")
    assert view["state"] == "queued" and view["activity"] == "Retrying in a moment"
    assert "private" not in str(view)


@pytest.mark.parametrize("receipt_workspace,expected", [("w1", "needs_you"), ("w2", "working")])
def test_saved_question_retires_only_its_scoped_completed_clarification(env, monkeypatch, receipt_workspace, expected):
    factory, module = env
    from app.db.supymem_models import ClarifierTurn, IdeaClarificationRun, IdeaRevision, IdeaSession
    from app.ventures import next_step, stage
    from core.clock import utcnow
    monkeypatch.setattr(next_step, "db", lambda: factory)
    monkeypatch.setattr(stage, "get_stage", lambda _id: {"stage": "idea", "confirmed_by_founder": False})
    add_job(factory)
    with factory() as session:
        session.add(IdeaSession(id="idea_saved", workspace_id="w1", venture_id="v1", case="know", state="clarifying",
                                current_revision_id="revision_saved", created_at=utcnow(), updated_at=utcnow()))
        session.flush()
        session.add(IdeaRevision(id="revision_saved", session_id="idea_saved", workspace_id="w1", venture_id="v1",
                                 rev=1, source="agent", summary="Saved idea", structured="{}", created_at=utcnow()))
        session.flush()
        session.add(ClarifierTurn(id="question_saved", session_id="idea_saved", workspace_id="w1", venture_id="v1",
                                 turn=1, role="agent", kind="question", text="Who is it for?", created_at=utcnow()))
        session.add(IdeaClarificationRun(id="receipt_saved", job_id="job_test", session_id="idea_saved",
                                        workspace_id=receipt_workspace, venture_id="v1", revision_id="revision_saved",
                                        state="completed", attempt=1, snapshot="{}", created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    view = module.snapshot("v1", "w1")
    assert view["state"] == expected
    assert bool(view["background_work"]) == (expected == "working")
    assert next_step._facts("v1")["active_jobs"] == (expected == "working")


@pytest.mark.parametrize("has_validation", [False, True])
def test_proposal_and_accepted_validation_have_the_same_next_step_and_status(env, monkeypatch, has_validation):
    from app.core.local_mode import is_local
    factory, module = env
    from app.db.supymem_models import IdeaRevision, IdeaSession, ValidationRun
    from app.idea.validation import METHODS
    from app.ventures import next_step, stage
    from core.clock import utcnow
    monkeypatch.setattr(next_step, "db", lambda: factory)
    monkeypatch.setattr(stage, "get_stage", lambda _id: {"stage": "idea", "confirmed_by_founder": False})
    with factory() as session:
        session.add(IdeaSession(id="idea_review", workspace_id="w1", venture_id="v1", case="know", state="reviewing",
                                current_revision_id="revision_review", created_at=utcnow(), updated_at=utcnow()))
        session.flush()
        session.add(IdeaRevision(id="revision_review", session_id="idea_review", workspace_id="w1", venture_id="v1",
                                 rev=1, source="agent", summary="Saved idea", structured="{}", created_at=utcnow()))
        session.flush()
        if has_validation:
            for method in METHODS:
                session.add(ValidationRun(id=f"validation_{method}", workspace_id="w1", venture_id="v1", session_id="idea_review",
                                          revision_id="revision_review", method=method, state="complete", founder_decision="accept",
                                          report="{}", created_at=utcnow()))
        session.commit()
    facts = next_step._facts("v1")
    assert facts["validation_done"] == has_validation
    view = module.snapshot("v1", "w1")
    step = next_step._resolve("v1", "/v/v1", facts)
    assert view["state"] == "needs_you"
    if is_local() and has_validation:
        assert view["action"] == {"label": "Review your startup and priorities", "href": "/v/v1/context"}
        assert next_step._founder_resolve("v1", "/v/v1", facts)["key"] == "venture.context"
    else:
        assert view["action"]["label"] == ("Review validation" if has_validation else "Review idea")
        assert step["title"] == ("Review your idea validation" if has_validation else "Check your idea summary")


def test_chat_status_prefers_active_build_and_exact_action_over_stale_slate(env):
    from app.chat.brief import render_brief

    answer = render_brief({
        "name": "LabHire", "stage": "readiness_review", "autonomy": "L1",
        "open": [{"title": "Old static landing task"}],
        "workflow": {
            "state": "working", "stage": "Building", "activity": "Building the approved product.",
            "next_step": "Wait for verified QA receipts.",
            "action": {"label": "View build progress", "href": "/v/v1/builds?run=b1"},
        },
    })
    assert "Building the approved product" in answer
    assert "Wait for verified QA receipts" in answer
    assert "Nobody is mid-task" not in answer
    assert "Old static landing task" not in answer


def test_failure_sanitized_and_pause_overrides(env):
    factory, module = env
    add_job(factory, status="failed_dead")
    view = module.snapshot("v1", "w1")
    assert view["state"] == "failed"
    assert view["action"]["href"] == "/v/v1/idea#journey-action"
    assert "private provider" not in str(view)
    paused = module.snapshot("v1", "w1", paused=True)
    assert paused["state"] == "paused"
    assert paused["action"] is None
    assert paused["continues_when_closed"] is False


def test_failed_exploration_has_neutral_copy_and_chat_retry_without_scheduling(env):
    from app.db.models import Job

    factory, module = env
    add_job(factory, kind="experiment.exploration.prepare", status="failed_dead")
    with factory() as session:
        session.get(Job, "job_test").last_error = "handler_failed:private founder idea and key"
        session.commit()
    view = module.snapshot("v1", "w1")
    assert view["state"] == "failed"
    assert "on our side" in view["activity"] and "idea is saved" in view["activity"]
    assert view["action"] == {"label": "Retry", "href": "/v/v1/now"}
    assert not any(word in view["activity"].lower() for word in ("credits", "model", "private founder", "—"))
    with factory() as session:
        assert session.get(Job, "job_test").status == "failed_dead"
        assert session.query(Job).count() == 1
    assert module.snapshot("v1", "w1", paused=True)["action"] is None
    other = module.snapshot("v1", "other-workspace")
    assert other["job_id"] is None and other["action"] is None


@pytest.mark.parametrize("code", ["clarification_credits_unavailable", "research_credits_unavailable", "invented_insufficient_credits"])
def test_unconfirmed_credit_cause_never_requests_credits(env, code):
    from app.db.models import Job

    factory, module = env
    add_job(factory, kind="research.deep" if code.startswith("research_") else "idea.clarify_turn", status="failed_dead")
    with factory() as session:
        session.get(Job, "job_test").last_error = code
        session.commit()
    view = module.snapshot("v1", "w1")
    assert view["state"] == "failed"
    assert view["action"]["label"] != "Request credits"
    assert "not enough credits" not in view["activity"].lower()


def test_saved_question_becomes_exact_attention_link(env):
    factory, module = env
    from app.db.supymem_models import IdeaSession, ClarifierTurn
    from core.clock import utcnow
    now = utcnow()
    with factory() as session:
        session.add(IdeaSession(id="s1", workspace_id="w1", venture_id="v1", case="know", state="clarifying", created_at=now, updated_at=now))
        session.flush()
        session.add(ClarifierTurn(id="t1", session_id="s1", workspace_id="w1", venture_id="v1", turn=1, role="agent", kind="question", text="Who is this for?", created_at=now))
        session.commit()
    view = module.snapshot("v1", "w1")
    assert view["state"] == "needs_you"
    assert view["action"] == {"label": "Answer question", "href": "/v/v1/idea#journey-action"}
    with factory() as session:
        session.add(ClarifierTurn(id="t2", session_id="s1", workspace_id="w1", venture_id="v1", turn=2, role="founder", kind="answer", text="Designers", created_at=now))
        session.commit()
    assert module.snapshot("v1", "w1")["action"] is None


@pytest.mark.parametrize("state", ["partial", "failed", "completed"])
def test_research_result_not_just_worker_success(env, state):
    factory, module = env
    add_job(factory, kind="research.deep", status="succeeded")
    from app.db.supymem_models import ResearchDossier
    from core.clock import utcnow
    with factory() as session:
        session.add(ResearchDossier(id=module.research_identifier("job_test"),
                    workspace_id="w1", venture_id="v1", kind="deep", state=state,
                    created_at=utcnow()))
        session.commit()
    view = module.snapshot("v1", "w1")
    assert view["state"] == "needs_you"
    assert view["action"] == {"label": "Run research again", "href": "/v/v1/research"}
    assert "Research is ready" not in view["activity"]
    assert module.snapshot("v1", "w1", paused=True)["state"] == "paused"
    add_job(factory, id="job_active", kind="plan.compile", age=1)
    assert module.snapshot("v1", "w1")["state"] == "working"


def test_research_dossier_scope_is_checked(env):
    factory, module = env
    add_job(factory, kind="research.deep", status="succeeded")
    from app.db.supymem_models import ResearchDossier
    from core.clock import utcnow
    with factory() as session:
        session.add(ResearchDossier(id=module.research_identifier("job_test"),
                    workspace_id="w2", venture_id="v1", kind="deep", state="partial",
                    created_at=utcnow()))
        session.commit()
    view = module.snapshot("v1", "w1")
    assert view["state"] == "needs_you"
    assert not view["research_report_ready"]
    assert view["action"] == {"label": "Run research again", "href": "/v/v1/research"}


def test_confirmed_personal_project_supersedes_only_terminal_legacy_research(env):
    factory, module = env
    from app.db.models import Job, Person, VentureDocument, Workspace
    from app.db.supymem_models import IdeaRevision, IdeaSession, ResearchDossier
    from core.clock import utcnow

    now = utcnow()
    add_job(factory, kind="research.deep", status="succeeded", age=-120)
    with factory() as session:
        session.add(Person(id="p1", display_name="Founder", created_at=now))
        session.add(Workspace(id="w1", kind="venture", name="Personal", entry_mode="idea",
                              owner_id="p1", settings_json='{"project_intent":"personal"}',
                              created_at=now, updated_at=now))
        session.flush()
        session.add(IdeaSession(id="s1", workspace_id="w1", venture_id="v1", case="know",
                                state="confirmed", current_revision_id="r1", confirmed_revision_id="r1",
                                confirmed_at=now, created_at=now, updated_at=now))
        session.flush()
        session.add(IdeaRevision(id="r1", session_id="s1", workspace_id="w1", venture_id="v1",
                                 rev=1, source="founder", structured='{"project_intent":"personal"}',
                                 created_at=now))
        session.add(ResearchDossier(id=module.research_identifier("job_test"), workspace_id="w1",
                                    venture_id="v1", kind="deep", state="partial", created_at=now))
        session.commit()

    personal = module.snapshot("v1", "w1")
    assert personal["state"] == "needs_you"
    assert personal["stage"] == "Local build"
    assert personal["action"] == {"label": "Review local build", "href": "/v/v1/idea#journey-action"}
    assert personal["changed_at"] == now.isoformat()

    with factory() as session:
        session.add(VentureDocument(id="mac_cancelled", venture_id="v1", kind="device_work_order_v1",
                                    title="Cancelled preparation", provenance_json='{"state":"cancelled"}',
                                    created_at=now + timedelta(seconds=1),
                                    updated_at=now + timedelta(seconds=1)))
        session.commit()
    after_cancel = module.snapshot("v1", "w1")
    assert after_cancel["stage"] == "Local build"
    assert after_cancel["state"] == "needs_you"
    assert after_cancel["action"] == {"label": "Review local build", "href": "/v/v1/idea#journey-action"}
    assert after_cancel["next_step"] == "Review local build"

    with factory() as session:
        session.get(Workspace, "w1").settings_json = '{"project_intent":"startup"}'
        session.commit()
    startup = module.snapshot("v1", "w1")
    assert startup["stage"] == "Research"
    assert startup["action"] == {"label": "Run research again", "href": "/v/v1/research"}

    with factory() as session:
        session.get(Workspace, "w1").settings_json = '{"project_intent":"personal"}'
        session.get(Job, "job_test").finished_at = now + timedelta(seconds=1)
        session.commit()
    late_research = module.snapshot("v1", "w1")
    assert late_research["stage"] == "Local build"
    assert late_research["action"] == {"label": "Review local build", "href": "/v/v1/idea#journey-action"}
    assert late_research["next_step"] == "Review local build"

    with factory() as session:
        session.get(Job, "job_test").finished_at = None
        session.add(VentureDocument(id="mac_pending", venture_id="v1", kind="device_work_order_v1",
                                    title="Review task", provenance_json='{"state":"pending"}',
                                    created_at=now + timedelta(seconds=1),
                                    updated_at=now + timedelta(seconds=1)))
        session.commit()
    assert module.snapshot("v1", "w1")["action"]["href"] == "/v/v1/device-work-orders/mac_pending"

    add_job(factory, id="active_build", kind="build.run", status="running", age=120)
    with factory() as session:
        session.get(VentureDocument, "mac_pending").updated_at = now - timedelta(seconds=121)
        session.commit()
    active = module.snapshot("v1", "w1")
    assert active["job_id"] == "active_build"
    assert active["state"] == "working"
    assert active["stage"] == "Building"

    with factory() as session:
        session.get(Job, "active_build").status = "failed_dead"
        session.commit()
    failed = module.snapshot("v1", "w1")
    assert failed["state"] == "failed"
    assert failed["action"] == {"label": "Review build and retry", "href": "/v/v1/builds"}


@pytest.mark.parametrize("plan_state,workspace,expected", [
    ("founder_review", "w1", "needs_you"),
    ("approved", "w1", "completed"),
    ("founder_review", "w2", "completed"),
])
def test_completed_plan_job_retains_scoped_review_action(env, plan_state, workspace, expected):
    factory, module = env
    add_job(factory, kind="plan.compile", status="succeeded")
    from app.db.supymem_models import ExecutionPlan
    from core.clock import utcnow
    with factory() as session:
        session.add(ExecutionPlan(id=module.plan_identifier("job_test"),
                    workspace_id=workspace, venture_id="v1", state=plan_state,
                    version=1, created_at=utcnow()))
        session.commit()
    view = module.snapshot("v1", "w1")
    assert view["state"] == expected
    if expected == "needs_you":
        assert view["action"] == {"label": "Review execution plan", "href": "/v/v1/plan#plan-review"}
    else:
        assert view["action"] is None
    assert module.snapshot("v1", "w1", paused=True)["state"] == "paused"
    add_job(factory, id="job_active", kind="build.run", age=1)
    assert module.snapshot("v1", "w1")["state"] == "working"


@pytest.mark.parametrize("kind,expected", [("plan.compile", "Review the generated plan"), ("build.watch", "verified build and QA"), ("deploy.run", "live URL")])
def test_next_checkpoint_is_scoped_and_does_not_claim_approval(env, kind, expected):
    factory, module = env
    add_job(factory, kind=kind)
    view = module.snapshot("v1", "w1")
    assert expected in view["next_step"]
    assert view["action"] is None
    assert "resume" in module.snapshot("v1", "w1", paused=True)["next_step"]


@pytest.mark.parametrize('build_state,workspace,expected', [
    ('failed', 'w1', 'failed'), ('cancelled', 'w1', 'cancelled'),
    ('succeeded', 'w1', 'needs_you'), ('running', 'w1', 'working'),
    ('failed', 'w2', 'needs_you'),
])
@pytest.mark.parametrize("job_status", ["succeeded", "scheduled", "pending", "retry_wait", "leased", "running"])
def test_successful_build_monitor_does_not_mask_domain_result(env, build_state, workspace, expected, job_status):
    import json
    factory, module = env
    add_job(factory, kind='build.watch', status=job_status)
    from app.db.models import Job
    from app.db.supymem_models import BuildRun, FounderApp
    from core.clock import utcnow
    with factory() as session:
        job = session.get(Job, 'job_test')
        job.payload_json = json.dumps({'build_run_id': 'build1'})
        session.add(FounderApp(id='app1', workspace_id=workspace, venture_id='v1', name='Test', slug='test-status', kind='web', created_at=utcnow(), updated_at=utcnow()))
        session.flush()
        session.add(BuildRun(id='build1', app_id='app1', workspace_id=workspace, venture_id='v1',
            trigger='test', agent='managed', state=build_state, runtime_version=1,
            created_at=utcnow(), closed_at=None if build_state == 'running' else utcnow(),
            error_code='private diagnostic never expose'))
        session.commit()
    result = module.snapshot('v1', 'w1')
    if workspace == 'w2' and job_status != 'succeeded':
        expected = 'queued' if job_status in {'scheduled', 'pending', 'retry_wait'} else 'working'
    assert result['state'] == expected
    assert 'private diagnostic' not in str(result)
    if workspace == 'w2':
        if job_status == 'succeeded':
            assert 'could not be verified' in result['activity']
        else:
            assert result['action'] is None
    else:
        assert result['action']['href'] == '/v/v1/builds?app=app1&run=build1' + (
            '#build-review' if result['action']['label'] == 'Review build evidence' else '')
    assert module.snapshot('v1', 'w1', paused=True)['state'] == 'paused'


def test_rejected_build_points_to_saved_run_review(env):
    import json
    factory, module = env
    add_job(factory, kind="build.watch", status="succeeded")
    from app.db.models import Job
    from app.db.supymem_models import BuildRun, FounderApp
    from core.clock import utcnow
    with factory() as session:
        session.get(Job, "job_test").payload_json = json.dumps({"build_run_id": "rejected_build"})
        session.add(FounderApp(id="app1", workspace_id="w1", venture_id="v1", name="Test", slug="test-status", kind="web", created_at=utcnow(), updated_at=utcnow()))
        session.flush()
        session.add(BuildRun(id="rejected_build", app_id="app1", workspace_id="w1", venture_id="v1",
            trigger="test", agent="managed", state="succeeded", runtime_version=1,
            spec=json.dumps({"execution_verified": True, "lifecycle": {"review": {"status": "rejected"}}}),
            created_at=utcnow(), closed_at=utcnow()))
        session.commit()
    result = module.snapshot("v1", "w1")
    assert "Changes requested" in result["activity"]
    assert result["action"] == {"label": "Review requested changes", "href": "/v/v1/builds?app=app1&run=rejected_build"}
    assert "estimate" not in result["activity"].lower()


def test_rejected_build_review_survives_later_chat_slate_and_dismissal(env):
    factory, module = env
    from app.chat.journey import asks_for_guidance, guided_answer
    from app.ventures.state_brief import _attention
    from app.db.models import PlannedTask
    from app.db.supymem_models import BuildRun, FounderApp
    from core.clock import utcnow

    now = utcnow()
    with factory() as session:
        session.add(FounderApp(id="app_review", workspace_id="w1", venture_id="v1",
            name="Product", slug="product-review-status", kind="web", created_at=now, updated_at=now))
        session.flush()
        session.add(BuildRun(id="build_review", app_id="app_review", workspace_id="w1", venture_id="v1",
            trigger="test", agent="managed", state="succeeded", runtime_version=1,
            spec=json.dumps({"execution_verified": True, "lifecycle": {"review": {"status": "rejected"}}}),
            created_at=now - timedelta(minutes=10), closed_at=now - timedelta(minutes=5)))
        for task_id, status, age in (("stale_static", "open", 0), ("dismissed_chat", "dismissed", 1)):
            session.add(PlannedTask(id=task_id, venture_id="v1", workspace_id="w1",
                title="Build honest early-access static artifact" if status == "open" else "Read-only GTM task",
                why_now="Saved task", tags_json="[]", est_minutes=30, runnable=0,
                source="chat", status=status, rank=0, created_at=now + timedelta(seconds=age),
                updated_at=now + timedelta(seconds=age)))
        session.commit()

    status = module.snapshot("v1", "w1")
    assert status["state"] == "needs_you"
    assert status["activity"].startswith("Changes requested")
    assert status["action"] == {"label": "Review requested changes", "href": "/v/v1/builds?app=app_review&run=build_review"}
    question = "What is the single next approved step for LabHire right now? Answer only; do not start or prepare a task."
    assert asks_for_guidance(question)
    answer = guided_answer("v1", {"idea": {"name": "LabHire"}, "attention": {"needs_you": []}}, status)
    assert "Changes requested" in answer["body_md"]
    assert answer["actions"][0]["label"] == "Review requested changes"
    attention = _attention("v1", workflow=status,
        builds={"last": {"state": "succeeded", "review_status": "rejected"}},
        device={}, approvals=[], recent_activity=[])
    assert "Latest build has changes requested: review saved feedback and build evidence" in attention["needs_you"]


def test_builder_deadline_is_named_in_status_without_exposing_private_error(env):
    import json
    factory, module = env
    add_job(factory, kind="build.watch", status="succeeded")
    from app.db.models import Job
    from app.db.supymem_models import BuildRun, FounderApp
    from core.clock import utcnow
    with factory() as session:
        session.get(Job, "job_test").payload_json = json.dumps({"build_run_id": "deadline_build"})
        session.add(FounderApp(id="app1", workspace_id="w1", venture_id="v1", name="Test", slug="test-status", kind="web", created_at=utcnow(), updated_at=utcnow()))
        session.flush()
        session.add(BuildRun(id="deadline_build", app_id="app1", workspace_id="w1", venture_id="v1",
            trigger="test", agent="managed", state="failed", runtime_version=1,
            error_code="build_runtime_deadline", created_at=utcnow(), closed_at=utcnow()))
        session.commit()
    result = module.snapshot("v1", "w1")
    assert result["state"] == "failed"
    assert "reviewed time limit" in result["activity"]
    assert result["action"]["href"] == "/v/v1/builds?app=app1&run=deadline_build"


def test_private_landing_status_uses_short_plain_english(env, monkeypatch):
    factory, module = env
    from app.db.models import Job
    from app.db.supymem_models import BuildRun, FounderApp
    from app.pagebuilder import api
    from core.clock import utcnow
    monkeypatch.setattr(api, "_candidate_current", lambda doc: True)
    now = utcnow()
    with factory() as session:
        session.add(FounderApp(id="private_app", workspace_id="w1", venture_id="v1", name="Private",
            slug="private-status", kind="landing", created_at=now, updated_at=now))
        session.flush()
        session.add(Job(id="private_candidate", kind="pagebuilder.build", workspace_id="w1", venture_id="v1",
            status="succeeded", created_at=now, payload_json=json.dumps({"_runtime": {"state": "ready"}})))
        session.add(BuildRun(id="private_run", app_id="private_app", workspace_id="w1", venture_id="v1",
            trigger="pagebuilder_source_qa", agent="managed", state="running", runtime_version=1,
            spec=json.dumps({"artifact_profile": "static_landing_v1", "qa_state": "pending",
                "source_import": {"identity": {"scope": {"job_id": "private_candidate"}}}}), created_at=now))
        session.commit()
    result = module.snapshot("v1", "w1")
    assert result["state"] == "ready"
    assert result["activity"] == "Your landing page is ready. Final checks and publishing are pending; this preview is private."
    assert len(result["activity"]) < 100


@pytest.mark.parametrize("job_status", ["succeeded", "failed_dead", "scheduled"])
@pytest.mark.parametrize("usable", ["live", "preview", "missing", "foreign"])
def test_landing_qa_failure_projects_usable_page_not_product_failure(env, monkeypatch, job_status, usable):
    factory, module = env
    from app.db.models import Job
    from app.db.supymem_models import BuildRun, Deployment, FounderApp
    from app.pagebuilder import api
    from core.clock import utcnow
    add_job(factory, kind="build.watch", status=job_status)
    monkeypatch.setattr(api, "_candidate_current", lambda doc: doc.get("tested_ready") is True)
    now = utcnow()
    with factory() as session:
        session.get(Job, "job_test").payload_json = json.dumps({"build_run_id": "landing_run"})
        session.add(FounderApp(id="landing_app", workspace_id="w1", venture_id="v1", name="Oximoron",
            slug="oximoron-status", kind="landing", created_at=now, updated_at=now))
        session.flush()
        spec = {"artifact_profile": "static_landing_v1", "qa_state": "failed",
                "source_import": {"identity": {"scope": {"job_id": "candidate"}}}}
        session.add(BuildRun(id="landing_run", app_id="landing_app", workspace_id="w1", venture_id="v1",
            trigger="pagebuilder_source_qa", agent="managed", state="failed", runtime_version=1,
            error_code="qa_runtime_deadline", spec=json.dumps(spec), created_at=now, closed_at=now))
        if usable in {"live", "foreign"}:
            session.add(Deployment(id="landing_live", app_id="landing_app", workspace_id="w1" if usable == "live" else "w2",
                venture_id="v1", target="supymem_static", state="live", version=1,
                url="https://oximoron.supymem.com/", live_at=now))
        if usable == "preview":
            session.add(Job(id="candidate", kind="pagebuilder.build", workspace_id="w1", venture_id="v1",
                status="succeeded", created_at=now + timedelta(seconds=1), payload_json=json.dumps({"tested_ready": True, "variant_count": 1,
                    "_runtime": {"state": "ready", "stage": "ready", "progress": 1, "quote": {"payer": "founder"}, "operations": {}}})))
        session.commit()
    result = module.snapshot("v1", "w1")
    if usable in {"live", "preview"}:
        assert result["state"] == "ready"
        assert "still available" in result["activity"]
        assert result["action"] == {"label": "Visit landing page" if usable == "live" else "Review page",
            "href": "https://oximoron.supymem.com/" if usable == "live" else "/v/v1/site?app=landing_app&run=landing_run#build-review"}
        assert result["landing_page"]["qa_note"]
        if usable == "live":
            assert result["secondary_action"]["href"] == "/v/v1/site?app=landing_app&run=landing_run#build-review"
        assert "QA notes" not in result["activity"]
        from app.chat.journey import _guided_base
        answer = _guided_base("v1", {"builds": {"last": {"state": "failed"}}}, result)
        assert "/builds" not in answer["actions"][0]["href"]
        assert "Review its saved result and usage before choosing a new estimate" not in answer["body_md"]
        from app.builder.execution import _serialize
        with factory() as session:
            assert _serialize(session, session.get(BuildRun, "landing_run"))["landing_page"]["available"]
            assert session.get(BuildRun, "landing_run").state == "failed"
    else:
        assert result["state"] == "failed"
        assert result["action"] == {"label": "Try again", "href": "/v/v1/site?build=1"}
        assert result["secondary_action"]["href"] == "/v/v1/site?app=landing_app&run=landing_run#build-review"


@pytest.mark.parametrize("audience", ["warehouse dispatchers", "weekend gardeners"])
def test_failed_landing_status_names_the_check_without_inventing_research(env, audience):
    factory, module = env
    from app.db.models import Job
    from app.db.supymem_models import BuildRun, FounderApp
    from core.clock import utcnow

    add_job(factory, kind="build.watch", status="failed_dead")
    now = utcnow()
    with factory() as session:
        session.get(Job, "job_test").payload_json = json.dumps({"build_run_id": "failed_page"})
        session.add(FounderApp(id="page_app", workspace_id="w1", venture_id="v1", name=audience,
            slug="failed-page", kind="landing", created_at=now, updated_at=now))
        session.flush()
        session.add(BuildRun(id="failed_page", app_id="page_app", workspace_id="w1", venture_id="v1",
            trigger="pagebuilder_source_qa", agent="managed", state="failed", runtime_version=1,
            error_code="pagebuilder_qa_failed:sections.0.headline: String should have at most 140 characters",
            spec=json.dumps({"artifact_profile": "static_landing_v1", "qa_state": "failed"}), created_at=now, closed_at=now))
        session.commit()
    status = module.snapshot("v1", "w1")
    assert "heading" in status["activity"].lower() and "140" in status["activity"]
    assert "research are saved" not in status["activity"] and "sections.0" not in status["activity"]
    assert "Website" in status["activity"] and "Retry" in status["activity"]


def test_queued_monitor_shows_saved_qa_phase(env):
    import json
    factory, module = env
    add_job(factory, kind="build.watch", status="scheduled")
    from app.db.models import Job
    from app.db.supymem_models import BuildRun, FounderApp
    from core.clock import utcnow
    with factory() as session:
        session.get(Job, "job_test").payload_json = json.dumps({"build_run_id": "qa_build"})
        session.add(FounderApp(id="qa_app", workspace_id="w1", venture_id="v1", name="Test", slug="qa-status", kind="web", created_at=utcnow(), updated_at=utcnow()))
        session.flush()
        session.add(BuildRun(id="qa_build", app_id="qa_app", workspace_id="w1", venture_id="v1", trigger="test", agent="managed", state="syncing_artifacts", runtime_version=1, spec=json.dumps({"phase": "qa_running"}), created_at=utcnow()))
        session.commit()
    result = module.snapshot("v1", "w1")
    assert result["state"] == "working"
    assert result["stage"] == "Testing"
    assert result["activity"] == "Independent browser QA is checking the built product."
    assert result["action"]["href"] == "/v/v1/builds?app=qa_app&run=qa_build"
    assert result["progress"] is None


def test_newer_planned_work_replaces_stale_background_job(env):
    factory, module = env
    add_job(factory, status="succeeded", age=-120)
    from app.db.models import PlannedTask
    from core.clock import utcnow

    now = utcnow()
    with factory() as session:
        session.add(PlannedTask(
            id="task_recent", venture_id="v1", workspace_id="w1",
            title="Prepare the reviewed channel matrix", detail=None,
            why_now="Founder requested it", tags_json="[]", est_minutes=30,
            runnable=1, source="chat", status="done", rank=1,
            created_at=now, updated_at=now, finished_at=now,
        ))
        session.commit()

    result = module.snapshot("v1", "w1")
    assert result["state"] == "completed"
    assert result["stage"] == "Company work"
    assert result["activity"] == "Finished Prepare the reviewed channel matrix."
    assert result["action"] == {"label": "Open saved work", "href": "/v/v1/tasks"}
    assert "Founder requested" not in str(result)


def test_open_planned_task_is_saved_work_not_a_background_queue(env):
    factory, module = env
    from app.db.models import PlannedTask
    from core.clock import utcnow

    now = utcnow()
    with factory() as session:
        session.add(PlannedTask(
            id="task_open", venture_id="v1", workspace_id="w1",
            title="Build the landing page", detail=None,
            why_now="Approved plan", tags_json="[]", est_minutes=30,
            runnable=1, source="plan", status="open", rank=1,
            created_at=now, updated_at=now,
        ))
        session.commit()

    result = module.snapshot("v1", "w1")
    assert result["state"] == "idle"
    assert result["continues_when_closed"] is False
    assert "No work is in progress" in result["activity"]
    assert result["action"] == {"label": "Open saved work", "href": "/v/v1/tasks"}

    add_job(factory, id="job_active", age=-60)
    active = module.snapshot("v1", "w1")
    assert active["state"] == "working"
    assert active["job_id"] == "job_active"


@pytest.mark.parametrize("state,expected,label", [
    ("pending", "needs_you", "Review Mac task"),
    ("approved", "needs_you", "Start reviewed Mac task"),
    ("claimed", "working", "Review Mac task"),
    ("cancelled", "cancelled", "Review Mac task"),
    ("stopped", "needs_you", "Review Mac task"),
])
def test_newer_mac_task_has_exact_status_and_link(env, state, expected, label):
    factory, module = env
    add_job(factory, status="succeeded", age=-120)
    from app.db.models import VentureDocument
    from core.clock import utcnow

    now = utcnow()
    body = {"state": state, "private_instruction": "never expose this"}
    if state in {"claimed", "stopped"}:
        body["claimed_at"] = now.isoformat()
    if state == "stopped":
        body.update(closed_at=now.isoformat(), native_stop={"processes_stopped": True})
    with factory() as session:
        session.add(VentureDocument(
            id="dwo_recent", venture_id="v1", kind="device_work_order_v1",
            title="Safe reviewed task", path=None, body_md=None,
            provenance_json=json.dumps(body), created_at=now, updated_at=now,
        ))
        session.commit()

    result = module.snapshot("v1", "w1")
    assert result["state"] == expected
    assert result["stage"] == "Computer use"
    assert result["action"] == {"label": label, "href": "/v/v1/device-work-orders/dwo_recent"}
    assert "private_instruction" not in str(result)
    if state == "stopped":
        assert "review the result" in result["activity"]
        assert "completed" not in result["activity"].lower()


def test_cancelled_mac_task_with_uncertain_click_remains_actionable(env):
    factory, module = env
    from app.db.models import PlannedTask, VentureDocument
    from core.clock import utcnow

    now = utcnow()
    with factory() as session:
        session.add(VentureDocument(
            id="dwo_uncertain_click", venture_id="v1", kind="device_work_order_v1",
            title="Review installed app", path=None, body_md=None,
            provenance_json=json.dumps({
                "state": "cancelled", "claimed_at": now.isoformat(),
                "stop_reconciliation_required": True,
                "native_stop": {"processes_stopped": True},
                "invocations": {"attempt": {
                    "proposal": {"operation": "click"},
                    "effect": {"state": "uncertain"},
                }},
            }), created_at=now - timedelta(minutes=2),
            updated_at=now - timedelta(minutes=1),
        ))
        # A newer saved plan item must not bury unresolved Mac review.
        session.add(PlannedTask(
            id="task_newer", venture_id="v1", workspace_id="w1",
            title="Prepare next step", detail=None, why_now="Saved plan",
            tags_json="[]", est_minutes=30, runnable=1, source="plan",
            status="open", rank=1, created_at=now, updated_at=now,
        ))
        session.commit()

    pending = module.snapshot("v1", "w1")
    assert pending["state"] == "needs_you"
    assert pending["action"] == {"label": "Review Mac task", "href": "/v/v1/device-work-orders/dwo_uncertain_click"}
    assert "uncertain click" in pending["activity"]
    assert "credit reconciliation" in pending["activity"]
    assert pending["continues_when_closed"] is False

    with factory() as session:
        session.add(VentureDocument(
            id="dwo_later", venture_id="v1", kind="device_work_order_v1",
            title="Later cancelled task", path=None, body_md=None,
            provenance_json=json.dumps({"state": "cancelled"}),
            created_at=now + timedelta(minutes=1),
            updated_at=now + timedelta(minutes=1),
        ))
        session.commit()
    assert module.snapshot("v1", "w1")["action"]["href"].endswith("/dwo_uncertain_click")

    with factory() as session:
        order = session.get(VentureDocument, "dwo_uncertain_click")
        body = json.loads(order.provenance_json)
        body.pop("stop_reconciliation_required")
        order.provenance_json = json.dumps(body)
        session.commit()
    settled = module.snapshot("v1", "w1")
    assert settled["state"] == "idle"
    assert settled["action"] == {"label": "Open saved work", "href": "/v/v1/tasks"}


def test_verified_mac_history_does_not_hide_a_current_saved_task(env):
    factory, module = env
    from app.db.models import PlannedTask, VentureDocument
    from core.clock import utcnow

    now = utcnow()
    with factory() as session:
        session.add(PlannedTask(
            id="task_current", venture_id="v1", workspace_id="w1",
            title="Review the next saved task", detail=None, why_now="Current plan",
            tags_json="[]", est_minutes=10, runnable=1, source="plan",
            status="open", rank=1, created_at=now, updated_at=now,
        ))
        session.add(VentureDocument(
            id="dwo_verified_old", venture_id="v1", kind="device_work_order_v1",
            title="Earlier verification", path=None, body_md=None,
            provenance_json=json.dumps({
                "state": "stopped", "claimed_at": now.isoformat(), "closed_at": now.isoformat(),
                "native_stop": {"processes_stopped": True, "task_verified": True,
                                "verification_kind": "git_status_v1"},
            }), created_at=now + timedelta(seconds=1),
            updated_at=now + timedelta(seconds=1),
        ))
        session.commit()

    result = module.snapshot("v1", "w1")
    assert result["state"] == "idle"
    assert result["action"] == {"label": "Open saved work", "href": "/v/v1/tasks"}


def test_verified_mac_receipt_can_complete_an_older_queued_job(env):
    factory, module = env
    from app.db.models import VentureDocument
    from core.clock import utcnow

    add_job(factory, status="scheduled", age=-120)
    now = utcnow()
    with factory() as session:
        session.add(VentureDocument(
            id="dwo_verified_after_queue", venture_id="v1", kind="device_work_order_v1",
            title="Completed Mac task", provenance_json=json.dumps({
                "state": "stopped", "claimed_at": now.isoformat(), "closed_at": now.isoformat(),
                "native_stop": {"processes_stopped": True, "task_verified": True,
                                "verification_kind": "git_status_v1"},
            }), created_at=now, updated_at=now,
        ))
        session.commit()
    result = module.snapshot("v1", "w1")
    assert result["state"] == "completed"
    assert result["action"] == {"label": "Open Mac result", "href": "/v/v1/device-work-orders/dwo_verified_after_queue"}


def test_unverified_mac_stop_requires_review_and_pause_still_wins(env):
    factory, module = env
    from app.db.models import VentureDocument
    from core.clock import utcnow

    now = utcnow()
    with factory() as session:
        session.add(VentureDocument(
            id="dwo_uncertain", venture_id="v1", kind="device_work_order_v1",
            title="Uncertain task", path=None, body_md=None,
            provenance_json=json.dumps({"state": "stopped", "claimed_at": now.isoformat()}),
            created_at=now, updated_at=now,
        ))
        session.commit()
    result = module.snapshot("v1", "w1")
    assert result["state"] == "needs_you"
    assert "final receipt needs review" in result["activity"]
    assert module.snapshot("v1", "w1", paused=True)["state"] == "paused"


def test_verified_mac_receipt_completes_status_but_cancelled_does_not(env):
    factory, module = env
    from app.db.models import VentureDocument
    from core.clock import utcnow

    now = utcnow()
    body = {
        "state": "stopped", "claimed_at": now.isoformat(), "closed_at": now.isoformat(),
        "native_stop": {"processes_stopped": True, "task_verified": True,
                        "verification_kind": "git_status_v1"},
    }
    with factory() as session:
        row = VentureDocument(
            id="dwo_verified", venture_id="v1", kind="device_work_order_v1",
            title="Verify venture workspace", path=None, body_md=None,
            provenance_json=json.dumps(body), created_at=now, updated_at=now,
        )
        session.add(row)
        session.commit()

    result = module.snapshot("v1", "w1")
    assert result["state"] == "completed"
    assert "independently verified" in result["activity"]
    assert result["action"]["href"] == "/v/v1/device-work-orders/dwo_verified"

    body["cancelled_at"] = now.isoformat()
    with factory() as session:
        row = session.get(VentureDocument, "dwo_verified")
        row.provenance_json = json.dumps(body)
        session.commit()
    assert module.snapshot("v1", "w1")["state"] == "needs_you"


def test_clean_process_stop_does_not_claim_failed_mac_goal_finished(env):
    factory, module = env
    from app.db.models import VentureDocument
    from core.clock import utcnow
    now = utcnow()
    with factory() as session:
        session.add(VentureDocument(id="dwo_failed_start", venture_id="v1",
            kind="device_work_order_v1", title="TextEdit note", body_md="",
            provenance_json=json.dumps({"state": "stopped", "claimed_at": now.isoformat(),
                "closed_at": now.isoformat(), "native_stop": {"processes_stopped": True,
                "task_verified": False, "reason_code": "cu_session_unavailable"}}),
            created_at=now, updated_at=now))
        session.commit()
    add_job(factory, id="gtm_active", kind="gtm.god.run", age=-60)
    add_job(factory, id="other_tenant", workspace="w2", age=1)
    view = module.snapshot("v1", "w1")
    assert [j["job_id"] for j in view["background_work"]] == ["gtm_active"]
    assert view["background_work"][0]["state"] == "working"
    assert view["state"] == "needs_you"
    assert "stopped" in view["activity"]
    assert "finished" not in view["activity"]
    assert view["action"]["href"].endswith("dwo_failed_start")


def test_find_customers_job_is_working_and_not_hidden_by_idle_next_task(env):
    factory, module = env
    add_job(factory, kind="gtm.journey.prepare")
    view = module.snapshot("v1", "w1")
    assert view["state"] == "working" and view["activity"] == "Finding your first customers"
    assert view["background_work"][0]["href"] == "/gtm"
    from types import SimpleNamespace
    task = SimpleNamespace(status="open", runnable=True, title="Publish landing page", finished_at=None, updated_at=None, created_at=__import__("core.clock", fromlist=["utcnow"]).utcnow())
    module._project_recent_task(view, "v1", task)
    assert view["state"] == "working" and "No work is in progress" not in view["activity"]


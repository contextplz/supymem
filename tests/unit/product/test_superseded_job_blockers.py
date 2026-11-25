"""New job results replace founder attention, never uncertain usage history."""
import json
from datetime import timedelta

import pytest

from tests.unit.product.test_run_status import add_job, env  # noqa: F401


@pytest.fixture
def blocked_research(env):  # noqa: F811 - shared pytest fixture
    from app.db.models import CreditReservation, Job, Person, Venture, Workspace
    from app.tasks import service
    from core.clock import utcnow

    factory, module = env
    now = utcnow()
    with factory() as session:
        session.add(Person(id="founder", display_name="Founder", created_at=now))
        session.flush()
        session.add(Workspace(id="w1", owner_id="founder", kind="company", name="Test",
                              entry_mode="idea", created_at=now, updated_at=now))
        session.flush()
        session.add(Venture(id="v2", workspace_id="w1", name="Other", raw_idea="Other idea", stage="idea",
                            created_at=now, updated_at=now))
        session.commit()
    add_job(factory, id="old_research", kind="research.deep", status="failed_dead", age=-120)
    with factory() as session:
        job = session.get(Job, "old_research")
        job.last_error = "research_outcome_uncertain"
        service.attach_job(session, job)
        session.flush()
        service.after_job(session, job.id, error=job.last_error)
        session.add(CreditReservation(id="uncertain_hold", account_id="account", amount_micro=100,
                    status="reconciling", purpose="research.deep", workspace_id="w1", venture_id="v1",
                    request_id=job.id, created_at=now, expires_at=now + timedelta(days=1)))
        session.commit()
    return factory, module


@pytest.mark.parametrize("job_status,report_state", [("succeeded", "complete"), ("succeeded", "partial"),
                                                     ("partial", "partial")])
@pytest.mark.parametrize("error", ["research_outcome_uncertain", "research_provider_timed_out"])
@pytest.mark.parametrize("audience", ["Freight dispatchers", "Families planning hikes"])
def test_new_research_report_supersedes_old_blocker_without_settling_hold(blocked_research, monkeypatch,
                                                                       job_status, report_state, error, audience):
    from app.chat import failed_jobs
    from app.db.models import CreditReservation, Job
    from app.db.supymem_models import IdeaRevision, IdeaSession, ResearchDossier, ResearchFinding
    from app.db.task_models import Task, TaskRun
    from app.tasks.contracts import blocker
    from core.clock import utcnow

    factory, module = blocked_research
    before = module.snapshot("v1", "w1")
    assert before["state"] == "needs_you"
    with factory() as session:
        task_id = session.query(Task).one().id
    assert before["action"] == {"label": "Run research again", "href": f"/v/v1/tasks#research-{task_id}"}
    add_job(factory, id="new_research", kind="research.deep", status=job_status)
    now = utcnow()
    with factory() as session:
        session.get(Job, "old_research").last_error = error
        task = session.query(Task).one()
        task.blocker_json = json.dumps(blocker(error))
        session.add(IdeaSession(id="confirmed_idea", workspace_id="w1", venture_id="v1", case="know",
            state="confirmed", current_revision_id="current_idea", confirmed_revision_id="current_idea",
            confirmed_at=now, created_at=now, updated_at=now))
        session.flush()
        session.add(IdeaRevision(id="current_idea", session_id="confirmed_idea", workspace_id="w1",
            venture_id="v1", rev=1, source="founder", structured=json.dumps({"customer": audience}),
            created_at=now))
        session.add(ResearchDossier(id=module.research_identifier("new_research"), workspace_id="w1",
                    venture_id="v1", revision_id="current_idea", kind="deep", state=report_state,
                    created_at=now, completed_at=now,
                    summary=json.dumps({"review": {"overview": f"Saved alternatives for {audience}."},
                        "coverage_gaps": []})))
        session.add(ResearchFinding(id="new_source", dossier_id=module.research_identifier("new_research"),
            workspace_id="w1", venture_id="v1", category="source", title=audience, source="web",
            url="https://example.test/source", evidence=json.dumps({"observed_at": now.isoformat(), "evidence_kind": "web"}), created_at=now))
        session.commit()
        saved_blocker = task.blocker_json
        saved_run_state = session.query(TaskRun).one().state

    view = module.snapshot("v1", "w1")
    assert view["job_id"] is None
    assert view["stage"] == "Research"
    assert view["state"] == "completed" and view["research_report_ready"] is True
    assert view["changed_at"] != before["changed_at"]
    assert "earlier action" not in str(view)
    assert view["action"] == {"label": "Review research", "href": "/v/v1/research"}
    if report_state == "partial":
        assert view["activity"] == "Research is ready. Review it before planning."
    monkeypatch.setattr(failed_jobs, "db", lambda: factory)
    assert failed_jobs.latest_failed("v1", "w1", "research.deep") is None
    with factory() as session:
        assert session.get(ResearchDossier, module.research_identifier("new_research")).state == report_state
        assert session.get(Job, "old_research").status == "failed_dead"
        assert session.query(Task).one().blocker_json == saved_blocker
        assert session.query(TaskRun).one().state == saved_run_state
        hold = session.get(CreditReservation, "uncertain_hold")
        assert (hold.status, hold.amount_micro, hold.settled_micro, hold.settled_at) == ("reconciling", 100, None, None)
    assert module.snapshot("v1", "w1", paused=True)["state"] == "paused"


@pytest.mark.parametrize("case", ["running", "failed", "cancelled", "no_report", "failed_report", "pending_report",
                                 "empty_report", "invalid_report", "error_report", "unfinished_report", "other_kind",
                                 "other_workspace", "other_venture", "older", "custom_task", "recurring_task"])
def test_only_a_later_scoped_result_replaces_an_admitted_job_blocker(blocked_research, case):
    from app.db.supymem_models import ResearchDossier
    from app.db.task_models import Task, TaskRun
    from core.clock import utcnow

    factory, module = blocked_research
    status = {"running": "leased", "failed": "failed_dead", "cancelled": "cancelled"}.get(case, "succeeded")
    add_job(factory, id="new_job", kind="plan.compile" if case == "other_kind" else "research.deep",
            workspace="w2" if case == "other_workspace" else "w1",
            venture="v2" if case == "other_venture" else "v1", status=status, age=-240 if case == "older" else 0)
    now = utcnow()
    with factory() as session:
        if case != "no_report":
            summary = {"empty_report": "{}", "invalid_report": "not json",
                       "error_report": '{"error_code":"research_outcome_uncertain"}'}.get(case, '{"review":{}}')
            session.add(ResearchDossier(id=module.research_identifier("new_job"), kind="deep",
                        workspace_id="w2" if case == "other_workspace" else "w1",
                        venture_id="v2" if case == "other_venture" else "v1",
                        state={"failed_report": "failed", "pending_report": "running"}.get(case, "complete"),
                        summary=summary, created_at=now, completed_at=None if case == "unfinished_report" else now))
        if case == "custom_task":
            session.query(TaskRun).one().run_key = "initial"
        elif case == "recurring_task":
            session.query(Task).one().kind = "scheduled"
        session.commit()
    view = module.snapshot("v1", "w1")
    assert view["state"] == "needs_you"
    assert view["stage"] == "Your task"
    assert view["activity"] == "Your research was interrupted."
    with factory() as session:
        task_id = session.query(Task).one().id
    assert view["action"] == {"label": "Run research again", "href": f"/v/v1/tasks#research-{task_id}"}


@pytest.mark.parametrize("saved_artifact", [False, True])
def test_other_job_kinds_require_their_verified_result_before_superseding(blocked_research, saved_artifact):
    from app.db.models import Job, PlannedTask, VentureDocument
    from app.db.task_models import Task, TaskStep
    from app.tasks.contracts import blocker
    from core.clock import utcnow

    factory, module = blocked_research
    add_job(factory, id="new_task", kind="task.run", status="succeeded")
    now = utcnow()
    with factory() as session:
        job = session.get(Job, "old_research")
        job.kind, job.last_error = "task.run", "job_outcome_uncertain"
        session.query(Task).one().blocker_json = json.dumps(blocker(job.last_error))
        session.query(TaskStep).one().skill = "task.run"
        session.get(Job, "new_task").payload_json = '{"task_id":"saved_work"}'
        session.add(PlannedTask(id="saved_work", workspace_id="w1", venture_id="v1", title="Summarize evidence",
                    why_now="Review sources", status="done", created_at=now, updated_at=now, finished_at=now))
        if saved_artifact:
            session.add(VentureDocument(id="task-note:saved_work", venture_id="v1", kind="task_note",
                        title="Summarize evidence", body_md="Status: completed\nSaved summary.",
                        provenance_json='{"task_id":"saved_work","source":"task.run"}',
                        created_at=now, updated_at=now))
        session.commit()
    view = module.snapshot("v1", "w1")
    assert view["state"] == ("completed" if saved_artifact else "needs_you")
    assert ("earlier action" in view["activity"]) is not saved_artifact


@pytest.mark.parametrize("error,interrupted", [("research_outcome_uncertain", True), ("research_interrupted", True),
    ("job_outcome_uncertain", False), ("task_outcome_uncertain", False), ("credit_reconciliation_required", False)])
def test_only_interrupted_research_gets_research_recovery_copy(error, interrupted):
    from app.tasks.contracts import blocker

    reason = blocker(error)
    assert reason["class"] == "needs_input"
    assert reason["reason"] == ("Your research was interrupted." if interrupted
        else "An earlier action needs its result checked before we can continue.")
    assert reason["unblock_action"] == ("Open Research and choose Run research again, or ask to skip for now." if interrupted
        else "Review the earlier result; it will not be repeated automatically.")

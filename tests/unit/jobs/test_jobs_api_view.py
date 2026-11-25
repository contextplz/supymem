"""A founder can distinguish a live lease from a stalled saved job."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import pytest

from tests.company_os_harness import load_company_os_app
from tests.unit.jobs.test_lease import factory


def test_job_view_exposes_lease_heartbeat_without_worker_identity_or_payload():
    load_company_os_app()
    from app.api.jobs_api import _view
    heartbeat = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    job = SimpleNamespace(
        id="job_1", workspace_id="wsp_1", venture_id="ven_1", kind="idea.validate",
        status="leased", attempts=1, max_attempts=3, last_error=None,
        payload_json='{"revision_id":"rev_1","private_prompt":"do not expose"}',
        created_at=heartbeat, scheduled_for=heartbeat, started_at=heartbeat,
        locked_at=heartbeat, finished_at=None, locked_by="secret_worker_identity",
    )
    result = _view(job)
    assert result["locked_at"] == "2026-09-28T12:00:00+00:00"
    assert result["revision_id"] == "rev_1"
    assert "locked_by" not in result
    assert "private_prompt" not in str(result)


@pytest.mark.parametrize("code", ["research_provider_rejected", "research_provider_rate_limited", "research_boundary_blocked",
                                 "pagebuilder_renderer_unavailable", "pagebuilder_generation_failed", "pagebuilder_job_failed"])
def test_failure_codes_survive_job_projection_without_diagnostics(code):
    load_company_os_app()
    from app.api.jobs_api import _view
    row = SimpleNamespace(id="job_saved", workspace_id="wsp_1", venture_id="ven_1", kind="pagebuilder.build" if code.startswith("pagebuilder_") else "research.deep",
        status="failed_dead", attempts=1, max_attempts=3, last_error=code + ":private diagnostic",
        payload_json="{}", created_at=None, scheduled_for=None, started_at=None, locked_at=None, finished_at=None)
    result = _view(row)
    assert result["error_code"] == code
    assert "private diagnostic" not in str(result)


@pytest.mark.parametrize("idea", ["Scheduling for dental practices", "Meal planning for home cooks"])
def test_wait_projection_matches_both_lease_deadlines_and_keeps_retry_details_private(monkeypatch, idea):
    load_company_os_app()
    from app.core import jobs
    from app.core.job_status import _view

    clock = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(jobs, "utcnow", lambda: clock)
    future = clock + timedelta(minutes=5)
    row = SimpleNamespace(id="saved_work", workspace_id="workspace", venture_id="venture", kind="research.deep",
        status="scheduled", attempts=1, max_attempts=3, last_error=None,
        payload_json=f'{{"private_prompt":"{idea}"}}', cancel_requested=False,
        created_at=clock - timedelta(minutes=10), started_at=None, locked_at=None, finished_at=None,
        run_after=clock, scheduled_for=future.replace(tzinfo=None))
    for run_after, scheduled_for in ((clock, future), (future, clock), (None, future), (future, None)):
        row.run_after, row.scheduled_for = run_after, scheduled_for
        wait = _view(row)["wait"]
        assert wait == {"reason": "scheduled", "label": "Starting soon. Your work is scheduled for later.",
                        "until": future.isoformat()}
    row.run_after, row.scheduled_for, row.status = future, clock, "retry_wait"
    row.last_error = "provider_retryable:private-provider-diagnostic"
    result = _view(row)
    assert result["wait"]["reason"] == "retry_backoff"
    assert "temporary problem" in result["wait"]["label"]
    assert result["wait"]["until"] == future.isoformat()
    assert idea not in str(result) and "private-provider-diagnostic" not in str(result)
    row.last_error = "rate_limited:private-limit-diagnostic"
    assert _view(row)["wait"]["reason"] == "usage_limit"
    assert "usage limit" in _view(row)["wait"]["label"]
    row.run_after = row.scheduled_for = clock
    assert _view(row)["wait"] == {"reason": "queued", "label": "Starting soon. Your work is ready to try again.", "until": clock.isoformat()}
    row.cancel_requested = True
    assert _view(row)["wait"]["reason"] == "cancel_requested"
    for status in ("leased", "running", "succeeded", "failed_dead", "cancelled"):
        row.status = status
        assert _view(row)["wait"] is None


@pytest.mark.parametrize("idea", ["Scheduling for dental practices", "Meal planning for home cooks"])
def test_retry_projection_keeps_saved_progress_without_claiming_active_search(monkeypatch, idea):
    load_company_os_app()
    from app.core import jobs
    from app.core.job_status import _view
    import json

    clock = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(jobs, "utcnow", lambda: clock)
    row = SimpleNamespace(id="saved_research", workspace_id="workspace", venture_id="venture", kind="research.deep",
        status="retry_wait", attempts=1, max_attempts=3, last_error="provider_retryable",
        payload_json=json.dumps({"idea": idea}), cancel_requested=False,
        created_at=clock, started_at=clock, locked_at=None, finished_at=None,
        run_after=clock + timedelta(minutes=5), scheduled_for=clock)
    dossier = SimpleNamespace(workspace_id=row.workspace_id, venture_id=row.venture_id, summary=json.dumps({"_runtime": {
        "job_id": row.id, "snapshot": {"idea": idea}, "progress_plan": ["search", "review"],
        "operations": {"search": {"state": "completed"}}}}))
    session = SimpleNamespace(get=lambda *args: dossier, scalar=lambda *args: None)
    result = _view(row, session)
    assert result["progress"]["percent"] == 50
    assert result["progress"]["step_label"] == result["wait"]["label"]
    assert "temporary problem" in result["progress"]["step_label"]
    assert idea not in str(result)


@pytest.mark.parametrize("idea", ["Scheduling for dental practices", "Meal planning for home cooks"])
def test_paused_task_wait_never_promises_a_scheduled_restart(factory, idea):
    load_company_os_app()
    from app.core.job_status import _view
    from app.db.models import Job, Person, Venture, Workspace
    from app.db.task_models import Task, TaskRun, TaskStep
    import json

    clock = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
    with factory() as session:
        session.add(Person(id="founder", display_name="Test person", created_at=clock))
        session.flush()
        for workspace_id in ("workspace", "another_workspace"):
            session.add(Workspace(id=workspace_id, owner_id="founder", kind="company", name=idea,
                                  entry_mode="idea", created_at=clock, updated_at=clock))
        session.flush()
        session.add(Venture(id="venture", workspace_id="workspace", name=idea, raw_idea=idea,
                            stage="idea", created_at=clock, updated_at=clock))
        job_id = "paused_research"
        session.add(Job(id=job_id, kind="research.deep", payload_json=json.dumps({"idea": idea}),
                        workspace_id="workspace", venture_id="venture", status="scheduled", created_at=clock))
        session.flush()
        session.add(Task(id="paused_task", workspace_id="workspace", venture_id="venture", owner_person_id="founder",
                         title=idea, goal=idea, state="paused", deadline_at=clock + timedelta(days=1),
                         created_at=clock, updated_at=clock))
        session.flush()
        session.add(TaskRun(id="paused_run", task_id="paused_task", run_key="run", state="paused",
                            started_at=clock, deadline_at=clock + timedelta(days=1)))
        session.flush()
        session.add(TaskStep(id="paused_step", run_id="paused_run", position=0, title=idea, skill="research.deep",
                             inputs_json=json.dumps({"idea": idea}), job_id=job_id, deadline_at=clock + timedelta(days=1)))
        row = session.get(Job, job_id)
        row.run_after = clock + timedelta(minutes=1)
        session.flush()
        result = _view(row, session)
        assert result["wait"] == {"reason": "paused", "label": "Paused. Resume this work when you are ready.", "until": None}
        assert result["progress"]["step_label"] == result["wait"]["label"]
        assert idea not in str(result)
        row.cancel_requested = True
        assert _view(row, session)["wait"]["reason"] == "cancel_requested"
        row.cancel_requested = False
        session.get(Task, "paused_task").workspace_id = "another_workspace"
        session.flush()
        assert _view(row, session)["wait"]["reason"] != "paused"

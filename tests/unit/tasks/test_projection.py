"""Synthetic SQLite projections; no worker, provider or user action executes."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from tests.unit.product.test_run_status import add_job, env  # noqa: F401 - shared SQLite fixture


@pytest.fixture
def projection(env):  # noqa: F811 - pytest injects the imported fixture
    factory, module = env
    from app.db.models import Person, Venture, Workspace
    from core.clock import set_clock

    now = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
    set_clock(lambda: now)
    with factory() as session:
        for person in ("usr_projection", "usr_other"):
            session.add(Person(id=person, display_name="Synthetic founder", created_at=now))
        session.flush()
        for workspace, owner in (("w1", "usr_projection"), ("w2", "usr_other")):
            session.add(Workspace(id=workspace, owner_id=owner, kind="company", entry_mode="idea", name="Synthetic workspace", created_at=now, updated_at=now))
        session.add(Venture(id="v2", workspace_id="w2", name="Other", raw_idea="Fixture", stage="draft", created_at=now, updated_at=now))
        session.commit()
    yield factory, module
    set_clock(None)


def task(factory, *, id="tsk_projection", state="working", kind="one_time", age=0,
         workspace="w1", venture="v1", owner="usr_projection", skill="research.deep", runs_on="server", blocker=None):
    from app.db.task_models import Task
    from core.clock import utcnow
    now = utcnow().replace(tzinfo=None)
    with factory() as session:
        session.add(Task(id=id, workspace_id=workspace, venture_id=venture, owner_person_id=owner,
            title="Summarize the evidence", goal="Prepare a concise summary", kind=kind, state=state,
            scope_json=json.dumps({"steps": [{"skill": skill, "runs_on": runs_on}]}),
            budget_json=json.dumps({"cost_micro": 500000, "tokens": 100000, "private": "never show this"}),
            blocker_json=json.dumps(blocker) if blocker else None,
            deadline_at=now + timedelta(hours=1), deadline_is_estimate=True,
            next_fire_at=now + timedelta(days=1) if kind == "scheduled" else None,
            created_at=now, updated_at=now + timedelta(seconds=age),
            last_run_at=now if state == "working" else None))
        session.commit()
    return id


def link(factory, task_id, job_id, *, state="working", skill="research.deep"):
    from app.db.task_models import TaskRun, TaskStep
    from core.clock import utcnow
    now = utcnow().replace(tzinfo=None)
    with factory() as session:
        session.add(TaskRun(id="run_" + task_id, task_id=task_id, run_key="synthetic-run", state=state,
            started_at=now, deadline_at=now + timedelta(minutes=30)))
        session.flush()
        session.add(TaskStep(id="step_" + task_id, run_id="run_" + task_id, position=0, title="Prepare the summary",
            skill=skill, job_id=job_id, state=state, deadline_at=now + timedelta(minutes=30)))
        session.commit()


@pytest.mark.parametrize("state,skill,path,label", [
    ("auth_required", "research.deep", "connections", "Connect the required account"),
    ("approval_required", "pagebuilder.publish", "site", "Review the website action"),
    ("approval_required", "email.send", "tasks", "Review the required action"),
    ("input_required", "research.deep", "tasks", "Review what the task needs"),
])
def test_task_card_links_one_existing_action_and_keeps_budget_private(projection, state, skill, path, label):
    from app.db.task_models import Task
    from app.tasks import service
    factory, _ = projection
    task_id = task(factory, state=state, skill=skill)
    with factory() as session:
        view = service.view(session, session.get(Task, task_id))
    assert view["action"] == {"label": label, "href": "/v/v1/" + path}
    assert view["deadline_is_estimate"] is True and view["deadline_at"].endswith("+00:00")
    assert not any(key in view for key in ("budget", "tokens", "cost_micro"))
    assert "never show this" not in str(view)
    assert "tsk_projection" not in view["action"]["label"]


def test_old_blocker_precedes_new_unrelated_history_and_uses_its_exact_next_action(projection):
    factory, module = projection
    task(factory, state="auth_required", age=-100, blocker={"class": "needs_auth",
        "reason": "The mailbox connection needs your attention.", "unblock_action": "Connect the mailbox to continue."})
    add_job(factory, id="finished", kind="content.draft", status="succeeded", age=10)
    view = module.snapshot("v1", "w1")
    assert view["state"] == "needs_you" and view["stage"] == "Your task"
    assert view["activity"] == "The mailbox connection needs your attention."
    assert view["action"]["href"] == "/v/v1/connections"
    assert view["next_step"] == "Connect the mailbox to continue."
    assert view["deadline_is_estimate"] is True and view["deadline_at"]
    assert view["background_work"] == []


@pytest.mark.parametrize("entrypoint", ["status", "finish", "snapshot"])
@pytest.mark.parametrize("result_kind", ["reachable_leads", "channels", "need_signals", "prospects"])
def test_successful_journey_reverifies_older_blocked_tasks(projection, monkeypatch, entrypoint, result_kind):
    import asyncio
    from sqlalchemy import select
    from app.db.models import GtmContact, Job, VentureDocument
    from app.db.task_models import Task, TaskEvent, TaskRun, TaskStep
    from app.gtm import journey
    from app.tasks import service
    from core.clock import utcnow

    factory, module = projection
    consumer = result_kind in {"channels", "need_signals"}
    audience = "Parents planning family trips" if consumer else "Independent bakery owners"
    problem = "family trip itineraries" if consumer else "bakery orders"
    monkeypatch.setattr(journey, "db", lambda: factory)
    monkeypatch.setattr(journey.outreach, "db", lambda: factory)
    monkeypatch.setattr(journey, "authority", lambda *args, **kwargs: None)
    monkeypatch.setattr(journey, "_icp_seed", lambda *args: {"idea": {
        "audience": audience, "problem": problem, "confirmed": True}})
    monkeypatch.setattr(journey.outreach, "drafts", lambda *args, **kwargs: [])
    monkeypatch.setattr(journey, "_draft_ready", lambda *args: 0)

    async def no_discovery(*args, **kwargs):
        return []

    monkeypatch.setattr(journey, "_discover", no_discovery)
    now = utcnow().replace(tzinfo=None)
    with factory() as session:
        document = VentureDocument(id="journey_projection", venture_id="v1", kind="gtm_journey",
            title="First customers", body_md="", created_at=now, updated_at=now)
        session.add(document)
        old_task_ids = []
        for index in range(2):
            payload = {"journey_id": document.id, "prepare_id": f"old_prepare_{index}"}
            job = Job(id=f"old_gtm_{index}", kind="gtm.journey.prepare", workspace_id="w1", venture_id="v1",
                payload_json=json.dumps(payload), status="succeeded", created_at=now - timedelta(minutes=10-index))
            session.add(job)
            session.flush()
            service.attach_job(session, job)
            document.provenance_json = json.dumps({"job_id": job.id, "run_id": payload["prepare_id"], "state": "needs_input"})
            session.flush()
            service.after_job(session, job.id)
            old_task, _, _ = service.job_step(session, job.id)
            assert old_task.state == "input_required"
            old_task_ids.append(old_task.id)
        payload = {"journey_id": document.id, "prepare_id": "new_prepare", "_actor_subject": "usr_projection"}
        job = Job(id="new_gtm", kind="gtm.journey.prepare", workspace_id="w1", venture_id="v1",
            payload_json=json.dumps(payload), status="leased" if entrypoint == "finish" else "succeeded",
            locked_by="fixture_worker", attempts=1, created_at=now)
        session.add(job)
        session.flush()
        service.attach_job(session, job)
        data = {"job_id": job.id, "run_id": "new_prepare", "state": "queued" if entrypoint == "finish" else "ready",
            "icp": {"who": audience, "confirmed": True}, "discovery_opt_in": True, "recruitment_opt_in": consumer}
        if result_kind == "prospects":
            session.add(GtmContact(id="projection_prospect", workspace_id="w1", source="venture:v1:public",
                created_at=now, stage_changed_at=now))
        else:
            data[result_kind] = [{"id": "fixture_result", "name": "Synthetic public result", "value_exchange": "Discuss booking workflows",
                "source_url": "https://example.test/booking", "quote": "We need easier booking"}]
        document.provenance_json = json.dumps(data)
        session.flush()
        if entrypoint != "finish":
            service.after_job(session, job.id)
        session.commit()
    if entrypoint == "status":
        journey.status("w1", "v1", authorize=False)
    elif entrypoint == "finish":
        asyncio.run(journey.run_prepare({**payload, "workspace_id": "w1", "venture_id": "v1",
            "_job_id": "new_gtm", "_worker_id": "fixture_worker", "_job_attempt": 1}))
    else:
        module.snapshot("v1", "w1")
    with factory() as session:
        for task_id in old_task_ids:
            old_task = session.get(Task, task_id)
            assert old_task.state == "completed"
            assert old_task.blocker_json is None
            run = session.scalar(select(TaskRun).where(TaskRun.task_id == task_id))
            step = session.scalar(select(TaskStep).where(TaskStep.run_id == run.id))
            assert run.state == step.state == "completed"
            assert json.loads(step.evidence_json)[0]["state"] == "ready"
        completed = session.scalar(select(TaskEvent.id).where(TaskEvent.task_id.in_(old_task_ids), TaskEvent.kind == "completed"))
        assert completed
        if entrypoint == "finish":
            service.job_step(session, "new_gtm")
            session.get(Job, "new_gtm").status = "succeeded"
            service.after_job(session, "new_gtm")
            session.commit()
    view = module.snapshot("v1", "w1")
    assert view["state"] == "completed"
    has_leads = result_kind in {"prospects", "reachable_leads"}
    label = "Review reachable leads" if has_leads else "Find customers"
    step = "review" if has_leads else "prepare"
    assert view["action"] == {"label": label, "href": f"/gtm?venture=v1&step={step}"}
    assert view["next_step"] == label
    assert "clearer audience" not in view["activity"]
    with factory() as session:
        assert len(session.scalars(select(TaskEvent).where(TaskEvent.task_id.in_(old_task_ids), TaskEvent.kind == "completed")).all()) == 2


@pytest.mark.parametrize("newer_blocker", [False, True])
def test_new_journey_result_retires_only_older_gtm_banner(projection, monkeypatch, newer_blocker):
    from app.db.models import Job, VentureDocument
    from app.db.task_models import TaskRun, TaskStep
    from app.tasks import service
    from core.clock import utcnow

    factory, module = projection
    monkeypatch.setattr(service, "reverify_gtm_tasks", lambda *args, **kwargs: 0, raising=False)
    reason = {"class": "needs_input", "reason": "Prospect research needs a clearer audience or a contact you know.",
        "unblock_action": "Review your audience or add one known contact."}
    task_id = task(factory, state="input_required", age=30 if newer_blocker else -30,
        skill="gtm.journey.prepare", blocker=reason)
    add_job(factory, id="older_gtm", kind="gtm.journey.prepare", status="succeeded", age=-60)
    add_job(factory, id="latest_gtm", kind="gtm.journey.prepare", status="succeeded", age=10)
    link(factory, task_id, "older_gtm", state="input_required", skill="gtm.journey.prepare")
    task(factory, id="latest_task", state="completed", age=20, skill="gtm.journey.prepare")
    now = utcnow()
    with factory() as session:
        session.get(TaskRun, "run_" + task_id).run_key = "older_gtm"
        for job_id, prepare_id in (("older_gtm", "older_prepare"), ("latest_gtm", "latest_prepare")):
            session.get(Job, job_id).payload_json = json.dumps({"journey_id": "new_journey", "prepare_id": prepare_id})
        session.get(TaskStep, "step_" + task_id).inputs_json = session.get(Job, "older_gtm").payload_json
        session.get(Job, "latest_gtm").finished_at = now + timedelta(seconds=10)
        session.add(VentureDocument(id="new_journey", venture_id="v1", kind="gtm_journey", title="First customers",
            body_md="", provenance_json=json.dumps({"job_id": "latest_gtm", "run_id": "latest_prepare",
                "state": "ready", "reachable_leads": [{"id": "fixture_lead", "value_exchange": "Discuss booking workflows",
                    "source_url": "https://example.test/booking", "quote": "We need easier booking"}]}), created_at=now, updated_at=now))
        session.commit()
    view = module.snapshot("v1", "w1")
    if newer_blocker:
        assert view["state"] == "needs_you" and view["activity"] == reason["reason"]
    else:
        assert view["state"] == "completed" and view["next_step"] == "Review reachable leads"


@pytest.mark.parametrize("audience", ["warehouse dispatchers", "weekend gardeners"])
@pytest.mark.parametrize("source_blocker", [False, True])
def test_replaced_gtm_blocker_reverifies_once_newer_journey_has_leads(projection, audience, source_blocker):
    from app.db.models import Job, VentureDocument
    from app.db.task_models import Task, TaskStep
    from app.tasks import service
    from app.tasks.contracts import Budget
    from core.clock import utcnow

    factory, _ = projection
    reason = {"class": "needs_input", "reason": "The prospect research was replaced by a newer request.",
        "unblock_action": "Review the current prospect research result."}
    if source_blocker:
        reason["reason"] = "Your public sources could not be read. Retry finding " + audience + "."
    task_id = task(factory, state="input_required", skill="gtm.journey.prepare", blocker=reason)
    add_job(factory, id="replaced_gtm", kind="gtm.journey.prepare", status="succeeded", age=-60)
    add_job(factory, id="current_gtm", kind="gtm.journey.prepare", status="succeeded", age=-10)
    link(factory, task_id, "replaced_gtm", state="input_required", skill="gtm.journey.prepare")
    now = utcnow()
    with factory() as session:
        session.get(Task, task_id).budget_json = Budget().model_dump_json()
        for job_id, prepare_id in (("replaced_gtm", "replaced_prepare"), ("current_gtm", "current_prepare")):
            session.get(Job, job_id).payload_json = json.dumps({"journey_id": "replaced_journey", "prepare_id": prepare_id})
        session.get(TaskStep, "step_" + task_id).inputs_json = session.get(Job, "replaced_gtm").payload_json
        session.add(VentureDocument(id="replaced_journey", venture_id="v1", kind="gtm_journey", title="First customers",
            body_md="", provenance_json=json.dumps({"job_id": "current_gtm", "run_id": "current_prepare",
                "state": "ready", "reachable_leads": [{"id": "fixture_lead", "source_url": "https://example.test/source",
                    "quote": audience + " need easier planning"}]}), created_at=now, updated_at=now))
        session.commit()
    with factory() as session:
        assert service.reverify_gtm_tasks(session, workspace_id="w1", venture_id="v1") == 1
        session.commit()
    with factory() as session:
        assert session.get(Task, task_id).state == "completed"


@pytest.mark.parametrize("leads", [False, True])
def test_dashboard_next_step_reviews_completed_gtm_result_only_with_leads(projection, leads):
    from app.api.dashboard import _handoff_next_step
    from app.db.models import VentureDocument
    from core.clock import utcnow

    factory, module = projection
    task(factory, state="completed", skill="gtm.journey.prepare")
    if leads:
        with factory() as session:
            session.add(VentureDocument(id="journey_result", venture_id="v1", kind="gtm_journey", title="Reachable leads",
                provenance_json='{"reachable_leads":[{"id":"fixture_lead","value_exchange":"Discuss booking workflows"}]}', created_at=utcnow(), updated_at=utcnow()))
            session.commit()
    view = module.snapshot("v1", "w1")
    fallback = {"key": "gtm.outreach", "cta_label": "Find customers", "href": "/gtm?venture=v1"}
    step = _handoff_next_step(view, fallback)
    if leads:
        assert step["cta_label"] == step["title"] == "Review reachable leads"
        assert step["href"] == "/gtm?venture=v1&step=review" and step["chat_action"] is None
    else:
        assert step is fallback
        assert view["next_step"] == "Find customers"
    other = {"key": "site.publish", "cta_label": "Open the private product", "href": "/v/v1/site"}
    assert _handoff_next_step(view, other) is other


@pytest.mark.parametrize("gate", ["empty", "running", "other_venture", "paused", "auth", "budget", "unsettled", "count"])
def test_journey_reverification_preserves_unresolved_work(projection, gate):
    from app.db.models import Job, VentureDocument
    from app.db.task_models import Task, TaskRun, TaskStep
    from app.tasks import service
    from app.tasks.contracts import Budget
    from core.clock import utcnow

    factory, _ = projection
    state = "paused" if gate == "paused" else "auth_required" if gate == "auth" else "input_required"
    reason = {"class": "needs_auth" if gate == "auth" else "budget_exhausted" if gate == "budget" else "needs_input",
        "reason": "Prospect research needs a clearer audience or a contact you know.", "unblock_action": "Review the task."}
    task_id = task(factory, state=state, skill="gtm.journey.prepare", blocker=reason)
    add_job(factory, id="blocked_gtm", kind="gtm.journey.prepare", status="succeeded", age=-10)
    link(factory, task_id, "blocked_gtm", state=state, skill="gtm.journey.prepare")
    now = utcnow()
    with factory() as session:
        session.get(Task, task_id).budget_json = Budget().model_dump_json()
        job = session.get(Job, "blocked_gtm")
        job.payload_json = json.dumps({"journey_id": "blocked_journey", "prepare_id": "blocked_prepare"})
        step = session.get(TaskStep, "step_" + task_id)
        step.inputs_json = job.payload_json
        if gate == "count":
            step.done_test_json = json.dumps({"spec": {"predicate": "at_least", "metric": "prospect_count", "count": 2}})
        if gate == "unsettled":
            session.get(TaskRun, "run_" + task_id).reserved_cost_micro = 1
        session.add(VentureDocument(id="blocked_journey", venture_id="v2" if gate == "other_venture" else "v1",
            kind="gtm_journey", title="First customers", body_md="", created_at=now, updated_at=now,
            provenance_json=json.dumps({"job_id": job.id, "run_id": "blocked_prepare",
                "state": "running" if gate == "running" else "ready",
                "reachable_leads": [] if gate == "empty" else [{"id": "fixture_lead", "source_url": "https://example.test/source",
                    "quote": "We need easier planning"}]})))
        session.commit()
    with factory() as session:
        service.reverify_gtm_tasks(session, workspace_id="w1", venture_id="v1")
        session.commit()
    with factory() as session:
        saved = session.get(Task, task_id)
        if gate == "count":
            assert saved.state == "failed" and json.loads(saved.blocker_json)["class"] == "impossible"
        elif gate == "unsettled":
            assert saved.state == "input_required"
            assert json.loads(saved.blocker_json) == service.blocker("usage_reconciliation_required")
        else:
            assert saved.state == state and json.loads(saved.blocker_json) == reason


def test_blocked_task_does_not_count_as_active_work_and_unrelated_job_is_preserved(projection):
    factory, module = projection
    task_id = task(factory, state="approval_required", skill="pagebuilder.publish")
    add_job(factory, id="blocked", kind="pagebuilder.publish", status="scheduled", age=10)
    link(factory, task_id, "blocked", state="approval_required", skill="pagebuilder.publish")
    add_job(factory, id="other-work", kind="content.draft", status="leased")
    view = module.snapshot("v1", "w1")
    assert view["state"] == "needs_you" and view["action"]["href"] == "/v/v1/site"
    assert [job["job_id"] for job in view["background_work"]] == ["other-work"]
    assert view["continues_when_closed"] is False


@pytest.mark.parametrize("state,expected", [("paused", "paused"), ("canceled", "cancelled")])
def test_stopped_handoff_with_unsettled_job_never_claims_to_be_running(projection, state, expected):
    factory, module = projection
    task_id = task(factory, state=state, age=1)
    add_job(factory, id="stopping", kind="research.deep", status="leased")
    link(factory, task_id, "stopping")
    view = module.snapshot("v1", "w1")
    assert view["state"] == expected and view["background_work"] == []
    assert view["continues_when_closed"] is False
    assert "Saved" in view["activity"]


def test_paused_backlog_does_not_hide_older_real_active_work(projection):
    factory, module = projection
    for index in range(6):
        task_id = task(factory, id=f"tsk_paused_{index}", state="paused", age=index + 1)
        add_job(factory, id=f"paused_job_{index}", status="scheduled", age=index + 1)
        link(factory, task_id, f"paused_job_{index}")
    add_job(factory, id="active_work", kind="content.draft", status="leased")
    view = module.snapshot("v1", "w1")
    assert view["state"] == "working"
    assert [job["job_id"] for job in view["background_work"]] == ["active_work"]


@pytest.mark.parametrize("kind", ["scheduled", "event"])
def test_standing_task_between_runs_does_not_invent_current_execution(projection, kind):
    factory, module = projection
    task_id = task(factory, state="submitted", kind=kind)
    add_job(factory, id="finished-run", kind="research.deep", status="leased")
    link(factory, task_id, "finished-run", state="completed")
    view = module.snapshot("v1", "w1")
    assert view["state"] == "idle" and view["background_work"] == []
    assert "waiting for" in view["activity"] and "worker" not in view["activity"]
    assert view["continues_when_closed"] is True
    assert view["deadline_at"] == ("2026-10-09T12:15:00+00:00" if kind == "scheduled" else None)


def test_task_progress_and_background_job_share_run_estimate_and_global_pause_wins(projection):
    factory, module = projection
    task_id = task(factory)
    add_job(factory, id="active-task", kind="research.deep", status="leased")
    link(factory, task_id, "active-task")
    view = module.snapshot("v1", "w1")
    assert view["state"] == "working" and view["continues_when_closed"] is True
    assert view["background_work"][0]["deadline_at"] == view["deadline_at"]
    assert view["background_work"][0]["activity"] == "Summarize the evidence"
    assert view["background_work"][0]["deadline_is_estimate"] is True
    paused = module.snapshot("v1", "w1", paused=True)
    assert paused["state"] == "paused" and paused["action"] is None
    assert "resume the venture" in paused["next_step"]


def test_device_task_does_not_promise_whole_task_continues_when_mac_closes(projection):
    factory, module = projection
    task(factory, runs_on="device")
    assert module.snapshot("v1", "w1")["continues_when_closed"] is False


def test_queued_run_and_background_card_do_not_claim_a_step_started(projection):
    factory, module = projection
    task_id = task(factory)
    add_job(factory, id="queued-task", kind="research.deep", status="scheduled")
    link(factory, task_id, "queued-task", state="submitted")
    view = module.snapshot("v1", "w1")
    assert view["state"] == view["background_work"][0]["state"] == "queued"
    assert "queued for its next step" in view["activity"]


@pytest.mark.parametrize("scope", [
    {"workspace": "w2"}, {"venture": "v2"}, {"owner": "usr_other"},
])
def test_wrong_workspace_venture_or_owner_never_enters_founder_status(projection, scope):
    factory, module = projection
    task(factory, state="auth_required", **scope)
    view = module.snapshot("v1", "w1")
    assert view["state"] == "idle" and view["action"] is None
    assert view["deadline_at"] is None

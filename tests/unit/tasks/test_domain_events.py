"""Real signup/reply producers with synthetic scoped records and no network."""
import asyncio
from datetime import timedelta
import hashlib
import json

import pytest
from sqlalchemy import func, select

from tests.unit.tasks.test_task_lifecycle import lifecycle as lifecycle


@pytest.fixture()
def producers(lifecycle, monkeypatch):
    from app.conversion import waitlist
    from app.db.models import GtmContact, GtmEmailAccount, GtmEmailEvent, GtmEmailSendQueue, GtmLinkedInQueue, GtmTouch, Venture, Workspace
    from app.db.supymem_models import VentureWaitlist, VentureWaitlistEntry
    from app.gtm.supymem import pipeline
    from app.gtm.shared_models import PublicResearchCache, SharedAddressValidity, SharedEmailOutcome, SharedEmailPattern
    d = lifecycle
    engine = d["factory"].kw["bind"]
    for model in (VentureWaitlist, VentureWaitlistEntry, GtmContact, GtmTouch, GtmEmailAccount, GtmEmailEvent, GtmEmailSendQueue, GtmLinkedInQueue,
                  SharedEmailOutcome, SharedAddressValidity, SharedEmailPattern, PublicResearchCache):
        model.__table__.create(engine, checkfirst=True)
    monkeypatch.setattr(waitlist, "db", lambda: d["factory"])
    monkeypatch.setattr(waitlist, "select_venture_app", lambda *a: None)
    monkeypatch.setattr(pipeline, "db", lambda: d["factory"])
    monkeypatch.setattr(pipeline.learning, "record", lambda *a, **kw: None)
    with d["factory"]() as session:
        session.add(Workspace(id="foreign", owner_id="usr_lifecycle", kind="company", entry_mode="idea",
            name="Other synthetic workspace", created_at=d["clock"][0], updated_at=d["clock"][0]))
        session.flush()
        session.add(Venture(id="foreign", workspace_id="foreign", name="Other synthetic venture", raw_idea="Fixture",
            stage="draft", created_at=d["clock"][0], updated_at=d["clock"][0]))
        session.add(VentureWaitlist(venture_id="ven_lifecycle", workspace_id="ws_lifecycle", enabled=True,
            consent_version=waitlist.VERSION, enabled_by="usr_lifecycle", updated_at=d["clock"][0]))
        session.add(GtmContact(id="contact_events", workspace_id="ws_lifecycle", email="synthetic@startup.example",
            created_at=d["clock"][0], stage_changed_at=d["clock"][0]))
        session.flush()
        session.add(GtmTouch(id="sent_events", workspace_id="ws_lifecycle", venture_id="ven_lifecycle",
            contact_id="contact_events", direction="out", simulated=0, provider_thread_id="thread_events",
            sent_at=d["clock"][0] - timedelta(days=1), created_at=d["clock"][0] - timedelta(days=1)))
        session.commit()
    d.update(waitlist=waitlist, pipeline=pipeline)
    return d


def _standing(d, event_type, *, task_id="task_events", workspace_id="ws_lifecycle", venture_id="ven_lifecycle", filter=None):
    from app.db.task_models import Task
    with d["factory"]() as session:
        session.add(Task(id=task_id, workspace_id=workspace_id, venture_id=venture_id, owner_person_id="usr_lifecycle",
            title="Synthetic event task", goal="Summarize recorded activity", kind="event", state="submitted",
            trigger_json=json.dumps({"event_type": event_type, "filter": filter or {}}),
            scope_json=json.dumps({"estimate_seconds": 60, "steps": [{"title": "Summarize",
                "skill": "tasks.waitlist_summary", "inputs": {}}]}), budget_json="{}",
            deadline_at=d["clock"][0] + timedelta(minutes=1), created_at=d["clock"][0], updated_at=d["clock"][0]))
        session.commit()


def _signup_payload(d, email="synthetic@startup.example"):
    from app.db.models import Venture
    with d["factory"]() as session:
        venture = session.get(Venture, "ven_lifecycle")
        consent = d["waitlist"].consent_text(venture.name)
    return {"email": email, "consent": True, "consent_version": d["waitlist"].VERSION,
        "consent_digest": hashlib.sha256(consent.encode()).hexdigest()}


def _receipt_spy(d, monkeypatch, *, fail_after=False):
    from app.db.models import GtmTouch
    from app.db.supymem_models import VentureWaitlistEntry
    original, emissions = d["service"].emit_domain_event, []
    def emit(session, **kwargs):
        model = VentureWaitlistEntry if kwargs["event_type"] == "waitlist.signup" else GtmTouch
        source = session.get(model, kwargs["event_id"])
        assert source is not None, "Domain event was emitted before its authoritative source row existed"
        assert (source.workspace_id, source.venture_id) == (kwargs["workspace_id"], kwargs["venture_id"])
        emissions.append(kwargs)
        result = original(session, **kwargs)
        if fail_after:
            raise RuntimeError("synthetic transaction failure after task outbox writes")
        return result
    monkeypatch.setattr(d["service"], "emit_domain_event", emit)
    return emissions


def test_waitlist_producer_records_source_and_one_scoped_task_run(producers, monkeypatch):
    from app.db.models import Job
    from app.db.supymem_models import VentureWaitlistEntry
    from app.db.task_models import TaskRun
    d = producers
    _standing(d, "waitlist.signup", filter={"source": "managed_waitlist"})
    _standing(d, "waitlist.signup", task_id="foreign_events", workspace_id="foreign", venture_id="foreign")
    emitted = _receipt_spy(d, monkeypatch)
    payload = _signup_payload(d)
    assert d["waitlist"].capture("ven_lifecycle", payload) == {"received": True, "email_verified": False}
    assert d["waitlist"].capture("ven_lifecycle", payload) == {"received": True, "email_verified": False}
    assert len(emitted) == 1 and emitted[0]["data"] == {"source": "managed_waitlist"}
    assert "@" not in json.dumps(emitted) and "email" not in emitted[0]["data"]
    with d["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(VentureWaitlistEntry)) == 1
        runs = session.scalars(select(TaskRun)).all()
        assert len(runs) == 1 and runs[0].task_id == "task_events"
        assert session.scalar(select(func.count()).select_from(Job).where(Job.kind == "tasks.execute")) == 1


def test_waitlist_test_probe_never_emits_or_starts_task(producers, monkeypatch):
    from app.db.supymem_models import VentureWaitlist, VentureWaitlistEntry
    from app.db.task_models import TaskRun
    d = producers
    _standing(d, "waitlist.signup")
    emitted = _receipt_spy(d, monkeypatch)
    token = "synthetic-probe-" + "token"
    with d["factory"]() as session:
        row = session.get(VentureWaitlist, "ven_lifecycle")
        row.probe_hash = hashlib.sha256(token.encode()).hexdigest()
        row.probe_expires_at = d["clock"][0] + timedelta(minutes=10)
        session.commit()
    payload = {**_signup_payload(d, "synthetic@qa.invalid"), "test_token": token}
    d["waitlist"].capture("ven_lifecycle", payload)
    with d["factory"]() as session:
        assert session.scalar(select(VentureWaitlistEntry)).is_test
        assert session.scalar(select(func.count()).select_from(TaskRun)) == 0
    assert emitted == []


def test_waitlist_event_and_source_roll_back_together(producers, monkeypatch):
    from app.db.models import Job
    from app.db.supymem_models import VentureWaitlistEntry
    from app.db.task_models import Task, TaskRun
    d = producers
    _standing(d, "waitlist.signup")
    emitted = _receipt_spy(d, monkeypatch, fail_after=True)
    with pytest.raises(RuntimeError, match="synthetic transaction failure"):
        d["waitlist"].capture("ven_lifecycle", _signup_payload(d))
    assert len(emitted) == 1
    with d["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(VentureWaitlistEntry)) == 0
        assert session.scalar(select(func.count()).select_from(TaskRun)) == 0
        assert session.scalar(select(func.count()).select_from(Job)) == 0
        assert session.get(Task, "task_events").state == "submitted"


def _apply(d, **changes):
    return d["pipeline"].apply_reply("contact_events", body="Private synthetic reply body",
        classification=changes.pop("classification", "human_positive"), **{
            "venture_id": "ven_lifecycle", "provider_thread_id": "thread_events",
            "provider_message_id": "message_events", **changes})


def test_attributed_human_reply_emits_scoped_run_without_private_content(producers, monkeypatch):
    from app.db.models import GtmTouch
    from app.db.task_models import TaskRun
    d = producers
    _standing(d, "outreach.reply", filter={"classification": "human_positive"})
    emitted = _receipt_spy(d, monkeypatch)
    assert _apply(d) and not _apply(d)
    assert len(emitted) == 1 and emitted[0]["data"] == {"classification": "human_positive"}
    assert "Private" not in json.dumps(emitted) and "@" not in json.dumps(emitted)
    with d["factory"]() as session:
        touch = session.get(GtmTouch, emitted[0]["event_id"])
        assert touch.direction == "in" and touch.simulated == 0 and touch.message_id == "message_events"
        assert session.scalar(select(func.count()).select_from(TaskRun)) == 1


@pytest.mark.parametrize("mode", ["automated", "unattributed"])
def test_automated_or_unattributed_reply_never_starts_task(producers, monkeypatch, mode):
    from app.db.models import GtmTouch
    from app.db.task_models import TaskRun
    d = producers
    _standing(d, "outreach.reply")
    emitted = _receipt_spy(d, monkeypatch)
    if mode == "automated":
        assert _apply(d, classification="automated")
    else:
        assert _apply(d, venture_id=None, provider_thread_id=None, provider_message_id=None)
    with d["factory"]() as session:
        assert session.scalar(select(GtmTouch.id).where(GtmTouch.direction == "in"))
        assert session.scalar(select(func.count()).select_from(TaskRun)) == 0
    assert emitted == []


def test_reply_event_and_contact_source_roll_back_together(producers, monkeypatch):
    from app.db.models import GtmContact, GtmTouch, Job
    from app.db.task_models import TaskRun
    d = producers
    _standing(d, "outreach.reply")
    emitted = _receipt_spy(d, monkeypatch, fail_after=True)
    with pytest.raises(RuntimeError, match="synthetic transaction failure"):
        _apply(d)
    assert len(emitted) == 1
    with d["factory"]() as session:
        assert session.get(GtmContact, "contact_events").replied_at is None
        assert session.scalar(select(GtmTouch.id).where(GtmTouch.direction == "in")) is None
        assert session.scalar(select(func.count()).select_from(TaskRun)) == 0
        assert session.scalar(select(func.count()).select_from(Job)) == 0


def test_unconfirmed_reply_thread_does_not_emit(producers, monkeypatch):
    from app.db.models import GtmTouch
    from app.db.task_models import TaskRun
    d = producers
    _standing(d, "outreach.reply")
    emitted = _receipt_spy(d, monkeypatch)
    with pytest.raises(ValueError, match="reply_thread_scope_unconfirmed"):
        _apply(d, provider_thread_id="foreign_thread")
    with d["factory"]() as session:
        assert session.scalar(select(GtmTouch.id).where(GtmTouch.direction == "in")) is None
        assert session.scalar(select(func.count()).select_from(TaskRun)) == 0
    assert emitted == []


@pytest.mark.parametrize("blocked", [False, True], ids=["busy", "waiting_for_approval"])
def test_events_arriving_during_active_run_queue_durably_then_run_in_order(producers, blocked):
    from app.db.models import Job
    from app.db.task_models import Task, TaskEvent, TaskRun, TaskStep
    d = producers
    _standing(d, "waitlist.signup")
    d["waitlist"].capture("ven_lifecycle", _signup_payload(d, "first@startup.example"))
    with d["factory"]() as session:
        first = session.scalar(select(TaskRun).where(TaskRun.task_id == "task_events"))
        first_id = first.id
        first_step_id = session.scalar(select(TaskStep.id).where(TaskStep.run_id == first.id))
        if blocked:
            task = session.get(Task, "task_events")
            task.state = first.state = "approval_required"
            session.get(TaskStep, first_step_id).state = "approval_required"
            task.blocker_json = json.dumps({"class": "needs_approval", "reason": "Synthetic pending approval",
                "unblock_action": "Review the synthetic action."})
        session.commit()
    d["clock"][0] += timedelta(minutes=1)
    second_payload = _signup_payload(d, "second@startup.example")
    d["waitlist"].capture("ven_lifecycle", second_payload)
    d["waitlist"].capture("ven_lifecycle", second_payload)
    with d["factory"]() as session:
        runs = session.scalars(select(TaskRun).where(TaskRun.task_id == "task_events")).all()
        assert len(runs) == 2, "The second authoritative source event was lost while the first run was active"
        second = next(run for run in runs if run.id != first_id)
        second_id = second.id
        assert second.state == "queued"
        task = session.get(Task, "task_events")
        assert task.state == ("approval_required" if blocked else "working")
        assert d["service"].view(session, task)["steps"][0]["id"] == first_step_id
        assert session.scalar(select(func.count()).select_from(Job).where(Job.kind == "tasks.execute")) == 1
        # Synthetic completion of the first checkpoint, not a live worker or
        # provider run. It must release exactly the next queued event intent.
        task.state, task.blocker_json = "working", None
        first = session.get(TaskRun, first_id)
        first.state = "working"
        step = session.get(TaskStep, first_step_id)
        step.state = "completed"
        step.evidence_json = json.dumps([{"ref": "synthetic:first_event_receipt", "signup_count": 1}])
        d["service"].complete_run(session, task, first)
        session.commit()
    with d["factory"]() as session:
        for _ in range(2):
            d["service"].fire_due(session, now=d["clock"][0])
            session.flush()
        session.commit()
        assert session.get(TaskRun, first_id).state == "completed"
        assert session.get(TaskRun, second_id).state in {"submitted", "working"}
        assert session.scalar(select(func.count()).select_from(TaskRun).where(
            TaskRun.task_id == "task_events", TaskRun.state.in_(("submitted", "working")))) == 1
        assert session.scalar(select(func.count()).select_from(Job).where(Job.kind == "tasks.execute")) == 2
        prior_view = d["service"].view(session, session.get(Task, "task_events"))
        events_before = session.scalar(select(func.count()).select_from(TaskEvent).where(TaskEvent.task_id == "task_events"))
    # A delayed control delivery from the completed run cannot reset the task
    # or announce its old completion while the next event is active.
    asyncio.run(d["execution"].execute({"task_run_id": first_id,
        "workspace_id": "ws_lifecycle", "venture_id": "ven_lifecycle"}))
    with d["factory"]() as session:
        assert d["service"].view(session, session.get(Task, "task_events")) == prior_view
        assert session.scalar(select(func.count()).select_from(TaskEvent).where(TaskEvent.task_id == "task_events")) == events_before

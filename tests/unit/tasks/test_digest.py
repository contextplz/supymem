"""Synthetic read-only digest qualification; no email or live provider runs."""
import asyncio
from datetime import timedelta
import json

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from tests.unit.tasks.test_drafts import draft_db as draft_db
from tests.unit.tasks.test_task_lifecycle import lifecycle as lifecycle
from tests.unit.tasks.test_result import results as results
from tests.unit.tasks.test_verification import NOW, judge_db as judge_db, verify


def _complete(factory):
    from app.db.supymem_models import Notice
    from app.db.task_models import TaskStep
    from app.tasks import service
    from app.tasks.locks import lock_run, lock_step
    Notice.__table__.create(factory.kw["bind"], checkfirst=True)
    with factory() as session:
        task, run = lock_run(session, "trn_result")
        for step_id in session.scalars(select(TaskStep.id).where(TaskStep.run_id == run.id)):
            lock_step(session, step_id)
        task.state = run.state = "working"
        service.complete_run(session, task, run)
        assert run.state == "completed"
        session.commit()


@pytest.fixture
def digest_db(results):
    _complete(results[0])
    return results


def _items(data, person="usr_result", **window):
    from app.tasks.digest import digest_items
    factory, _, now = data
    with factory() as session:
        return digest_items(session, person, window.get("since", now - timedelta(days=1)), window.get("until", now))


def test_unmigrated_task_schema_keeps_legacy_digest_read_only():
    from tests.company_os_harness import load_company_os_app
    load_company_os_app()
    from sqlalchemy import create_engine
    from app.db.models import Person
    from app.tasks.digest import digest_items
    from core.clock import utcnow
    engine = create_engine("sqlite:///:memory:")
    now = utcnow()
    try:
        with Session(engine) as session:
            pending = Person(id="usr_pending", display_name="Synthetic founder", created_at=now)
            session.add(pending)
            assert digest_items(session, pending.id, now - timedelta(days=1), now) == []
            assert pending in session.new
    finally:
        engine.dispose()


def test_actual_completion_producer_qualifies_saved_result_with_plain_public_title(digest_db):
    from app.db.task_models import TaskEvent
    items = _items(digest_db)
    assert items == ["Finished: Prepare a short note. Open Supymem to review the saved result."]
    assert not any(value in " ".join(items) for value in ("tsk_result", "job_result", "planned_result", "12345", "PROVIDER PAYLOAD"))
    with digest_db[0]() as session:
        completed = session.scalar(select(TaskEvent).where(TaskEvent.kind == "completed"))
        assert len(json.loads(completed.data_json)["contract_digest"]) == 64


def test_legacy_done_job_still_needs_current_saved_source_proof(digest_db):
    from app.db.models import Job, VentureDocument
    with digest_db[0]() as session:
        session.get(Job, "job_result").status = "done"
        session.commit()
    assert len(_items(digest_db)) == 1
    with digest_db[0]() as session:
        session.get(VentureDocument, "task-note:planned_result").body_md += "Changed after completion"
        session.commit()
    assert _items(digest_db) == []


def test_reader_never_flushes_unrelated_pending_changes_or_calls_any_effect(digest_db, monkeypatch):
    from app.db.models import Person
    from app.tasks import device, service, usage
    from app.tasks.digest import digest_items
    from app.email import service as email
    def forbidden(*args, **kwargs):
        pytest.fail("Digest reads cannot write, settle, call a device or send email")
    monkeypatch.setattr(device, "attach_stop_receipt", forbidden)
    monkeypatch.setattr(usage, "after_call", forbidden)
    monkeypatch.setattr(service, "event", forbidden)
    monkeypatch.setattr(email, "send_email", forbidden)
    factory, _, now = digest_db
    engine, statements = factory.kw["bind"], []
    def only_reads(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
        assert statement.lstrip().upper().startswith("SELECT")
    with factory() as session:
        session.get(Person, "usr_result").display_name = "Uncommitted display name"
        dirty = set(session.dirty)
        event.listen(engine, "before_cursor_execute", only_reads)
        try:
            assert digest_items(session, "usr_result", now - timedelta(days=1), now)
            assert set(session.dirty) == dirty and not session.new and not session.deleted
        finally:
            event.remove(engine, "before_cursor_execute", only_reads)
    assert statements
    with factory() as session:
        assert session.get(Person, "usr_result").display_name == "Synthetic founder"


@pytest.mark.parametrize("since_offset,until_offset,expected", [(0, 1, 0), (-1, 0, 1), (-2, -1, 0), (1, 0, 0)])
def test_existing_digest_window_is_exclusive_since_and_inclusive_until(digest_db, since_offset, until_offset, expected):
    now = digest_db[2]
    assert len(_items(digest_db, since=now + timedelta(seconds=since_offset),
        until=now + timedelta(seconds=until_offset))) == expected


@pytest.mark.parametrize("change", ["missing_event", "missing_contract", "missing_artifact", "changed_goal", "changed_check", "source_scope", "source_body", "source_text", "source_payload"])
def test_missing_or_tampered_current_completion_proof_is_omitted(digest_db, change):
    from app.db.models import Job, VentureDocument
    from app.db.task_models import Task, TaskEvent
    with digest_db[0]() as session:
        task = session.get(Task, "tsk_result")
        completed = session.scalar(select(TaskEvent).where(TaskEvent.kind == "completed"))
        if change == "missing_event":
            session.delete(completed)
        elif change == "missing_contract":
            completed.data_json = "{}"
        elif change == "missing_artifact":
            data = json.loads(completed.data_json)
            data.pop("artifact_digest")
            completed.data_json = json.dumps(data)
        elif change == "changed_goal":
            task.goal = "Prepare a different result"
        elif change == "changed_check":
            task.done_test_json = json.dumps({"type": "deterministic", "spec": {"predicate": "at_least", "metric": "source_count", "count": 3}})
        elif change == "source_scope":
            session.get(VentureDocument, "task-note:planned_result").venture_id = "foreign"
        elif change == "source_body":
            session.get(VentureDocument, "task-note:planned_result").body_md = "A forged note lacks a completion checkpoint"
        elif change == "source_text":
            session.get(VentureDocument, "task-note:planned_result").body_md += "A forged conclusion retaining the original Status: completed line"
        else:
            session.get(Job, "job_result").payload_json = json.dumps({"task_id": "foreign"})
        session.commit()
    assert _items(digest_db) == []


@pytest.mark.parametrize("state", ["working", "failed", "canceled"])
def test_new_current_run_suppresses_old_completion_even_for_standing_work(report_digest, state):
    from app.db.task_models import TaskRun
    with report_digest[0]() as session:
        session.add(TaskRun(id="trn_current", task_id="tsk_result", run_key="later", state=state,
            started_at=report_digest[2] + timedelta(seconds=1), deadline_at=report_digest[2] + timedelta(minutes=5)))
        session.commit()
    assert _items(report_digest, until=report_digest[2] + timedelta(seconds=2)) == []


def test_future_queued_run_does_not_hide_the_current_verified_completion(digest_db):
    from app.db.task_models import TaskRun
    with digest_db[0]() as session:
        session.add(TaskRun(id="trn_future", task_id="tsk_result", run_key="future", state="queued",
            started_at=digest_db[2] + timedelta(seconds=1), deadline_at=digest_db[2] + timedelta(minutes=5)))
        session.commit()
    assert len(_items(digest_db)) == 1


@pytest.mark.parametrize("state", ["working", "failed"])
def test_every_current_checkpoint_must_still_be_completed(digest_db, state):
    from app.db.task_models import TaskStep
    with digest_db[0]() as session:
        session.get(TaskStep, "tst_result").state = state
        session.commit()
    assert _items(digest_db) == []


def test_whole_task_count_uses_fresh_source_receipt_instead_of_saved_claim(results):
    from app.db.task_models import Task, TaskStep
    with results[0]() as session:
        session.get(Task, "tsk_result").done_test_json = json.dumps({"type": "deterministic",
            "spec": {"predicate": "at_least", "metric": "source_count", "count": 3}})
        step = session.get(TaskStep, "tst_result")
        step.evidence_json = json.dumps([{**item, "source_count": 3} for item in json.loads(step.evidence_json)])
        session.commit()
    _complete(results[0])
    # The note's real source receipt has no verified sources despite the saved count.
    assert _items(results) == []


@pytest.mark.parametrize("scope", ["recipient", "task_owner", "workspace_owner", "venture_workspace", "workspace_archived", "venture_archived"])
def test_only_the_current_owner_and_active_matching_scope_can_receive_item(digest_db, scope):
    from app.db.models import Venture, Workspace
    from app.db.task_models import Task
    with digest_db[0]() as session:
        if scope == "task_owner":
            session.get(Task, "tsk_result").owner_person_id = "foreign"
        elif scope == "workspace_owner":
            session.get(Workspace, "wsp_result").owner_id = "foreign"
        elif scope == "venture_workspace":
            session.get(Venture, "ven_result").workspace_id = "foreign"
        elif scope == "workspace_archived":
            session.get(Workspace, "wsp_result").archived_at = digest_db[2]
        elif scope == "venture_archived":
            session.get(Venture, "ven_result").archived_at = digest_db[2]
        session.commit()
    assert _items(digest_db, person="foreign" if scope == "recipient" else "usr_result") == []


@pytest.mark.parametrize("mode", ["reserved", "uncertain", "orphaned_reservation", "old_run_reservation"])
def test_unsettled_task_usage_never_qualifies_completion(digest_db, mode):
    from app.db.task_models import TaskEvent, TaskRun
    with digest_db[0]() as session:
        run = session.get(TaskRun, "trn_result")
        if mode == "reserved":
            run.reserved_cost_micro = 1
        elif mode == "uncertain":
            run.evaluation_json = json.dumps({"state": "uncertain"})
        elif mode == "old_run_reservation":
            session.add(TaskRun(id="trn_older", task_id="tsk_result", run_key="older", state="failed", reserved_tokens=1,
                started_at=digest_db[2] - timedelta(days=1), deadline_at=digest_db[2]))
        else:
            session.add(TaskEvent(id="tev_orphaned", task_id="tsk_result", run_id=run.id, kind="call_reserved",
                message="Private usage checkpoint", data_json=json.dumps({"invocation": "provider-private"}), created_at=digest_db[2]))
        session.commit()
    assert _items(digest_db) == []


@pytest.mark.parametrize("location", ["device", "either"])
def test_native_checkpoints_are_skipped_before_mutating_verifier_can_run(digest_db, monkeypatch, location):
    from app.db.task_models import TaskStep
    from app.tasks import device
    monkeypatch.setattr(device, "attach_stop_receipt", lambda *a, **kw: pytest.fail("Native receipt attachment is a write"))
    with digest_db[0]() as session:
        session.get(TaskStep, "tst_result").runs_on = location
        session.commit()
    assert _items(digest_db) == []


@pytest.mark.parametrize("field", ["title", "goal", "artifact"])
def test_secret_shaped_text_is_withheld_wholesale(digest_db, field):
    from app.db.models import VentureDocument
    from app.db.task_models import Task, TaskEvent
    from app.tasks.digest import completed_contract_digest
    with digest_db[0]() as session:
        if field == "artifact":
            session.get(VentureDocument, "task-note:planned_result").body_md += "\nsk-" + "x" * 24
        else:
            task = session.get(Task, "tsk_result")
            setattr(task, field, "sk-" + "x" * 24)
            completed = session.scalar(select(TaskEvent).where(TaskEvent.kind == "completed"))
            completed.data_json = json.dumps({**json.loads(completed.data_json), "contract_digest": completed_contract_digest(task)})
        session.commit()
    assert _items(digest_db) == []


def test_title_sanitizes_private_identifiers_without_rendering_source_contents(results):
    from app.db.task_models import Task
    with results[0]() as session:
        session.get(Task, "tsk_result").title = "Review tsk_result job_01abcdefghjk msg_private_receipt 0123456789abcdef — saved notes"
        session.commit()
    _complete(results[0])
    item = _items(results)[0]
    assert "saved notes" in item and "\u2014" not in item
    assert not any(identifier in item for identifier in ("tsk_result", "job_01abcdefghjk", "msg_private_receipt", "0123456789abcdef"))


@pytest.fixture
def report_digest(results):
    from app.db.session import serialize_writes
    from app.db.supymem_models import VentureWaitlistEntry
    from app.db.task_models import Task, TaskStep
    from app.tasks.reports import write_report
    with results[0]() as session:
        serialize_writes(session)
        task = session.get(Task, "tsk_result")
        task.kind, task.goal, task.title = "scheduled", "Summarize waitlist sign-ups and replies", "Weekly summary"
        session.add(VentureWaitlistEntry(id="signup_digest", workspace_id="wsp_result", venture_id="ven_result",
            email="never-send@example.test", consent_version="synthetic", consent_text="Synthetic", is_test=False,
            created_at=results[2] - timedelta(hours=1)))
        session.flush()
        receipt = write_report(session, task_id=task.id, run_id="trn_result", workspace_id="wsp_result", venture_id="ven_result", now=results[2])
        step = session.get(TaskStep, "tst_result")
        step.skill, step.job_id, step.evidence_json = "tasks.waitlist_summary", None, json.dumps(receipt["evidence"])
        session.commit()
    _complete(results[0])
    return results


def test_inline_report_rechecks_saved_sources_without_returning_private_contact_data(report_digest):
    from app.db.supymem_models import VentureWaitlistEntry
    assert _items(report_digest) == ["Finished: Weekly summary. Open Supymem to review the saved result."]
    with report_digest[0]() as session:
        session.delete(session.get(VentureWaitlistEntry, "signup_digest"))
        session.commit()
    assert _items(report_digest) == []


def test_digest_is_bounded_to_eight_distinct_current_tasks(report_digest):
    from app.db.session import serialize_writes
    from app.db.task_models import Task, TaskEvent, TaskRun, TaskStep
    from app.tasks import service
    from app.tasks.reports import write_report
    factory, _, now = report_digest
    with factory() as session:
        serialize_writes(session)
        for index in range(9):
            task = Task(id=f"tsk_digest_{index}", workspace_id="wsp_result", venture_id="ven_result",
                owner_person_id="usr_result", title=f"Summary {index}", goal="Summarize sign-ups",
                kind="scheduled", state="working", deadline_at=now, created_at=now, updated_at=now)
            session.add(task)
            session.flush()
            run = TaskRun(id=f"trn_digest_{index}", task_id=task.id, run_key="first", state="working",
                started_at=now, deadline_at=now)
            session.add(run)
            session.flush()
            receipt = write_report(session, task_id=task.id, run_id=run.id,
                workspace_id="wsp_result", venture_id="ven_result", now=now)
            session.add(TaskStep(id=f"tst_digest_{index}", run_id=run.id, position=0, title="Prepare summary",
                skill="tasks.waitlist_summary", state="completed", evidence_json=json.dumps(receipt["evidence"]), deadline_at=now))
            session.flush()
            service.complete_run(session, task, run)
        session.flush()
        completion = session.scalar(select(TaskEvent).where(TaskEvent.task_id == "tsk_result", TaskEvent.kind == "completed"))
        session.add(TaskEvent(id="tev_duplicate", task_id=completion.task_id, run_id=completion.run_id,
            kind=completion.kind, message=completion.message, data_json=completion.data_json, created_at=now))
        session.commit()
    items = _items(report_digest)
    assert len(items) == len(set(items)) == 8


def test_changed_report_goal_and_quiet_standing_zero_are_omitted(report_digest):
    from app.db.session import serialize_writes
    from app.db.supymem_models import VentureWaitlistEntry
    from app.db.models import VentureDocument
    from app.db.task_models import Task, TaskEvent, TaskStep
    from app.tasks.reports import write_report
    with report_digest[0]() as session:
        session.get(Task, "tsk_result").goal = "Summarize something different"
        session.commit()
    assert _items(report_digest) == []
    # A forged completion event still cannot turn a verified quiet zero into news.
    with report_digest[0]() as session:
        serialize_writes(session)
        session.delete(session.get(VentureWaitlistEntry, "signup_digest"))
        session.delete(session.get(VentureDocument, "task-report:trn_result"))
        session.flush()
        receipt = write_report(session, task_id="tsk_result", run_id="trn_result", workspace_id="wsp_result", venture_id="ven_result", now=report_digest[2])
        assert receipt["quiet"] is True
        session.get(TaskStep, "tst_result").evidence_json = json.dumps(receipt["evidence"])
        from app.tasks.digest import completed_artifact_digest, completed_contract_digest
        completed = session.scalar(select(TaskEvent).where(TaskEvent.kind == "completed"))
        from app.db.task_models import TaskRun
        task, run = session.get(Task, "tsk_result"), session.get(TaskRun, "trn_result")
        completed.data_json = json.dumps({"contract_digest": completed_contract_digest(task),
            "artifact_digest": completed_artifact_digest(session, task, run)})
        session.commit()
    assert _items(report_digest) == []


@pytest.mark.parametrize("mutation", [None, "body", "goal", "score", "invalid", "tolerance"])
def test_rubric_requires_current_artifact_contract_and_valid_score(judge_db, mutation):
    from app.db.models import VentureDocument
    from app.db.task_models import Task, TaskRun
    from app.tasks.digest import digest_items
    assert verify(judge_db)["passed"] is True
    calls = len(judge_db.calls)
    with Session(judge_db.engine) as session:
        task, run = session.get(Task, "tsk_verify"), session.get(TaskRun, "trn_verify")
        evaluation = json.loads(run.evaluation_json)
        if mutation == "body":
            session.get(VentureDocument, "task-note:planned_verify").body_md += "Changed saved artifact"
        elif mutation == "goal":
            task.goal = "A different request"
        elif mutation == "score":
            evaluation["score"] = 0.5
        elif mutation == "invalid":
            evaluation["valid"] = False
        elif mutation == "tolerance":
            task.tolerance = 0.9
        run.evaluation_json = json.dumps(evaluation)
        session.commit()
    with Session(judge_db.engine) as session:
        assert bool(digest_items(session, "usr_verify", NOW - timedelta(days=1), NOW)) is (mutation is None)
    assert len(judge_db.calls) == calls


def test_inline_reply_reader_uses_actual_source_receipt_without_model_or_contact_body(draft_db):
    from app.db.models import GtmTouch
    from app.db.task_models import TaskStep
    from app.tasks.digest import digest_items
    d = draft_db
    with d["factory"]() as session:
        session.get(TaskStep, "step_draft").state = "submitted"
        session.commit()
    asyncio.run(d["execution"].execute({"task_run_id": "run_draft", "workspace_id": "ws_lifecycle", "venture_id": "ven_lifecycle"}))
    calls = len(d["calls"])
    now = d["clock"][0]
    with d["factory"]() as session:
        assert digest_items(session, "usr_lifecycle", now - timedelta(days=1), now) == [
            "Finished: Draft a reply. Open Supymem to review the saved result."]
    with d["factory"]() as session:
        session.get(GtmTouch, "reply_draft").body_text = "Changed source after its saved draft"
        session.commit()
    with d["factory"]() as session:
        assert digest_items(session, "usr_lifecycle", now - timedelta(days=1), now) == []
    assert len(d["calls"]) == calls == 1

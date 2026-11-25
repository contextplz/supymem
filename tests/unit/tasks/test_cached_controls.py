"""Cached ORM identities must not defeat a founder pause or repeat paid work."""
import asyncio
from contextlib import contextmanager
import json
import sys

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from tests.unit.tasks.test_device import device_db as device_db
from tests.unit.tasks.test_drafts import _draft, draft_db as draft_db
from tests.unit.tasks.test_task_lifecycle import lifecycle as lifecycle
from tests.unit.tasks.test_progress import _record, _reflect, progress_db as progress_db
from tests.unit.tasks.test_verification import judge_db as judge_db, verify


def _cached_factory(module, engine, monkeypatch):
    cached = Session(engine, expire_on_commit=False)
    @contextmanager
    def transaction():
        try:
            yield cached
        finally:
            if cached.in_transaction():
                cached.rollback()
    monkeypatch.setattr(module, "db", lambda: transaction)
    return cached


def _state(factory, task_id, run_id, state):
    from app.db.task_models import Task, TaskRun
    with factory() as session:
        session.get(Task, task_id).state = state
        session.get(TaskRun, run_id).state = state
        session.commit()


def test_paid_reply_response_refreshes_cached_pause_then_recovers_without_new_call(draft_db, monkeypatch):
    from app.db.models import VentureDocument
    from app.db.task_models import Task, TaskRun, TaskStep
    d = draft_db
    cached = _cached_factory(d["drafts"], d["factory"].kw["bind"], monkeypatch)
    transport = sys.modules["app.credits.metered"]
    original = transport.llm
    async def pause_after_paid_response(*args, **kwargs):
        response = await original(*args, **kwargs)
        _state(d["factory"], "task_draft", "run_draft", "paused")
        return response
    monkeypatch.setattr(transport, "llm", pause_after_paid_response)
    try:
        assert _draft(d)["verified"] is False
        with d["factory"]() as session:
            assert session.get(Task, "task_draft").state == session.get(TaskRun, "run_draft").state == "paused"
            journal = json.loads(session.get(TaskStep, "step_draft").evidence_json)
            assert journal["state"] == "responded" and journal["response_digest"]
            assert session.get(VentureDocument, "task-draft:run_draft") is None
            run = session.get(TaskRun, "run_draft")
            assert (run.cost_micro, run.tokens_used, run.reserved_cost_micro, run.reserved_tokens) == (7, 10, 0, 0)
        _state(d["factory"], "task_draft", "run_draft", "working")
        assert _draft(d)["verified"] is True
        assert len(d["calls"]) == 1
    finally:
        cached.close()


def test_paid_rubric_response_refreshes_cached_pause_and_reuses_saved_judgment(judge_db, monkeypatch):
    from app.db.task_models import Task, TaskRun
    d = judge_db
    cached = _cached_factory(d.module, d.engine, monkeypatch)
    def pause():
        _state(lambda: Session(d.engine), "tsk_verify", "trn_verify", "paused")
    d.during_call = pause
    try:
        assert verify(d) == {"state": "stopped"}
        with Session(d.engine) as session:
            task, run = session.get(Task, "tsk_verify"), session.get(TaskRun, "trn_verify")
            assert task.state == run.state == "paused"
            judgment = json.loads(run.evaluation_json)
            assert judgment["state"] == "responded" and judgment["valid"] is True
            assert (run.cost_micro, run.tokens_used, run.reserved_cost_micro, run.reserved_tokens) == (32, 300, 0, 0)
        _state(lambda: Session(d.engine), "tsk_verify", "trn_verify", "working")
        assert verify(d) == {"state": "recovered"}
        with Session(d.engine) as session:
            assert session.get(Task, "tsk_verify").state == session.get(TaskRun, "trn_verify").state == "completed"
        assert len(d.calls) == 1
    finally:
        cached.close()


def test_paid_reflection_refreshes_cached_pause_without_returning_next_query(progress_db, monkeypatch):
    from app.db.task_models import Task, TaskStep
    d = progress_db
    _record(d)
    _record(d)
    cached = _cached_factory(d["progress"], d["factory"].kw["bind"], monkeypatch)
    d["mode"]["value"] = "paused"
    try:
        with pytest.raises(d["progress"].ResearchStopped, match="research_progress_stopped"):
            _reflect(d)
        with d["factory"]() as session:
            assert session.get(Task, d["task_id"]).state == "paused"
            journal = json.loads(session.get(TaskStep, d["step_id"]).evidence_json)[d["progress"].KEY]
            assert journal["state"] == "switched" and journal["newquery"] == d["mode"]["query"]
        _state(d["factory"], d["task_id"], d["run_id"], "working")
        assert _reflect(d) == d["mode"]["query"]
        assert len(d["calls"]) == 1
    finally:
        cached.close()


def test_device_claim_refreshes_cached_pause_and_locks_parent_before_children(device_db):
    from app.db.task_models import Task, TaskRun, TaskStep
    d, locked = device_db, []
    with Session(d.engine, expire_on_commit=False) as session:
        cached_rows = [session.get(model, identifier) for model, identifier in (
            (Task, "tsk_device"), (TaskRun, "trn_device"), (TaskStep, "tst_device"))]
        _state(lambda: Session(d.engine), "tsk_device", "trn_device", "paused")
        def observe(execute_state):
            statement = execute_state.statement
            if getattr(statement, "_for_update_arg", None) is not None:
                entity = statement.column_descriptions[0].get("entity")
                locked.append(entity.__tablename__)
        event.listen(session, "do_orm_execute", observe)
        with d.bind(workspace_id="ws_device", venture_id="ven_device", actor="usr_device"):
            result = d.module.claim_step(session, "tsk_device", "tst_device", "dev_one")
        assert result["accepted"] is False and result["reason"] == "task_not_active"
        assert locked[:3] == ["tasks", "task_runs", "task_steps"]
        assert cached_rows[0].state == cached_rows[1].state == "paused"
        session.commit()


def test_device_reentry_keeps_its_owned_dirty_lease_and_usage(device_db):
    from app.db.task_models import TaskRun, TaskStep
    d = device_db
    with Session(d.engine, expire_on_commit=False) as session, d.bind(
            workspace_id="ws_device", venture_id="ven_device", actor="usr_device"):
        lease = d.module.claim_step(session, "tsk_device", "tst_device", "dev_one")
        heartbeat = d.module.heartbeat_step(session, "tsk_device", "tst_device", "dev_one", lease["lease_token"], lease["lease_version"])
        assert heartbeat["accepted"] is True and heartbeat["lease_version"] == lease["lease_version"]
        assert session.get(TaskRun, "trn_device").attempts == 1
        assert session.get(TaskStep, "tst_device").lease_token is not None
        session.commit()


def test_execution_refreshes_cached_pause_before_admission_or_inline_work(draft_db, monkeypatch):
    from app.db.task_models import Task, TaskRun, TaskStep
    d = draft_db
    with d["factory"]() as session:
        session.get(TaskStep, "step_draft").state = "submitted"
        session.commit()
    cached = _cached_factory(d["execution"], d["factory"].kw["bind"], monkeypatch)
    cached_rows = [cached.get(model, identifier) for model, identifier in (
        (Task, "task_draft"), (TaskRun, "run_draft"), (TaskStep, "step_draft"))]
    _state(d["factory"], "task_draft", "run_draft", "paused")
    try:
        asyncio.run(d["execution"].execute({"task_run_id": "run_draft", "workspace_id": "ws_lifecycle", "venture_id": "ven_lifecycle"}))
        with d["factory"]() as session:
            assert session.get(Task, "task_draft").state == session.get(TaskRun, "run_draft").state == "paused"
            assert session.get(TaskStep, "step_draft").state == "submitted"
        assert d["calls"] == [] and cached_rows
    finally:
        cached.close()

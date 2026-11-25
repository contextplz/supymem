"""Custody changes stop new admission but never discard confirmed in-flight usage."""
import json

import pytest
from sqlalchemy import func, select, update

from tests.unit.tasks.test_task_lifecycle import lifecycle as lifecycle, _admit, _claim, _contract, _rows


def _change(d, change):
    from app.db.models import Person, Venture, Workspace
    with d["factory"]() as session:
        if change == "owner":
            session.add(Person(id="usr_scope_other", display_name="Other synthetic owner", created_at=d["clock"][0]))
            session.get(Workspace, "ws_lifecycle").owner_id = "usr_scope_other"
        elif change == "missing_person":
            session.get(Workspace, "ws_lifecycle").owner_id = "usr_scope_missing"
        elif change == "workspace_archive":
            session.get(Workspace, "ws_lifecycle").archived_at = d["clock"][0]
        elif change == "venture_archive":
            session.get(Venture, "ven_lifecycle").archived_at = d["clock"][0]
        else:
            session.add(Workspace(id="ws_scope_other", owner_id="usr_lifecycle", kind="company", entry_mode="idea",
                name="Other synthetic workspace", created_at=d["clock"][0], updated_at=d["clock"][0]))
            session.get(Venture, "ven_lifecycle").workspace_id = "ws_scope_other"
        session.commit()


@pytest.mark.parametrize("change", ["owner", "workspace_archive", "venture_archive", "venture_move", "missing_person"])
def test_new_task_cannot_be_created_outside_active_custody(lifecycle, change):
    from app.db.models import Job
    from app.db.task_models import Task, TaskRun
    d = lifecycle
    _change(d, change)
    with d["factory"]() as session, pytest.raises(PermissionError):
        d["service"].create(session, workspace_id="ws_lifecycle", venture_id="ven_lifecycle",
            owner_person_id="usr_scope_missing" if change == "missing_person" else "usr_lifecycle", contract=_contract())
    with d["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(Task)) == 0
        assert session.scalar(select(func.count()).select_from(TaskRun)) == 0
        assert session.scalar(select(func.count()).select_from(Job)) == 0


@pytest.mark.parametrize("change", ["owner", "workspace_archive", "venture_archive", "venture_move"])
def test_worker_admission_rechecks_scope_before_same_attempt_shortcut(lifecycle, change):
    from app.db.task_models import Task, TaskRun
    d = lifecycle
    jid = _admit(d)
    claimed = _claim(d)
    with d["factory"]() as session:
        assert d["service"].before_job(session, jid, claimed.attempts)
        session.commit()
    task, run, _ = _rows(d)
    _change(d, change)
    with d["factory"]() as session:
        assert d["service"].before_job(session, jid, claimed.attempts) is False
        session.commit()
        current = session.get(Task, task.id)
        assert current.state == "input_required" and "ownership changed" in json.loads(current.blocker_json)["reason"]
        assert session.get(TaskRun, run.id).attempts == 1


@pytest.mark.parametrize("method", ["before_call", "before_tool"])
@pytest.mark.parametrize("change", ["owner", "workspace_archive", "venture_archive", "venture_move"])
def test_paid_and_tool_boundaries_recheck_custody_before_new_work(lifecycle, method, change):
    from app.core.jobs import FatalError
    from app.db.task_models import Task, TaskEvent, TaskRun
    d = lifecycle
    jid = _admit(d)
    _, run, _ = _rows(d)
    _change(d, change)
    with d["usage"].job_context(jid), pytest.raises(FatalError):
        getattr(d["usage"], method)(*(100, 100) if method == "before_call" else ())
    with d["factory"]() as session:
        saved = session.get(TaskRun, run.id)
        assert (saved.cost_micro, saved.tokens_used, saved.tool_calls, saved.reserved_cost_micro, saved.reserved_tokens) == (0, 0, 0, 0, 0)
        assert not session.scalar(select(TaskEvent.id).where(TaskEvent.kind == "call_reserved"))
        task = session.get(Task, saved.task_id)
        assert json.loads(task.blocker_json)["class"] == "needs_input"
        assert "ownership changed" in json.loads(task.blocker_json)["reason"]


def test_guard_refreshes_cached_custody_and_requires_matching_parent_run(lifecycle):
    from app.db.models import Workspace
    d = lifecycle
    _admit(d)
    task, run, _ = _rows(d)
    with d["factory"]() as session:
        cached = session.get(Workspace, "ws_lifecycle")
        assert d["service"].guard(session, task, run)
        session.execute(update(Workspace).where(Workspace.id == cached.id).values(archived_at=d["clock"][0]),
            execution_options={"synchronize_session": False})
        assert cached.archived_at is None
        assert d["service"].guard(session, task, run) is False
        assert cached.archived_at is None  # Fresh scalar proof does not rewrite an owned identity.
    with d["factory"]() as session:
        run.task_id = "another_task"
        assert d["service"].guard(session, task, run) is False


def test_completed_job_verifies_in_original_owner_scope_and_restores_caller(lifecycle, monkeypatch):
    from app.tasks import skills
    from app.tenancy import bind, current
    d = lifecycle
    jid = _admit(d)
    seen = []
    def verify(*args, **kwargs):
        scope = current()
        seen.append((scope.workspace_id, scope.venture_id, scope.actor))
        return {"verified": False, "pending": True, "evidence": []}
    monkeypatch.setattr(skills, "verify_result", verify)
    with d["factory"]() as session, bind(workspace_id="other_workspace", venture_id="other_venture", actor="system"):
        d["service"].after_job(session, jid)
        assert (current().workspace_id, current().venture_id, current().actor) == (
            "other_workspace", "other_venture", "system")
        session.commit()
    assert seen == [("ws_lifecycle", "ven_lifecycle", "usr_lifecycle")]


@pytest.mark.parametrize("change", ["owner", "workspace_archive", "venture_archive", "venture_move"])
def test_late_job_result_cannot_complete_after_custody_changes(lifecycle, monkeypatch, change):
    from app.db.task_models import Task, TaskRun
    from app.tasks import skills
    d = lifecycle
    jid = _admit(d)
    task, run, _ = _rows(d)
    _change(d, change)
    monkeypatch.setattr(skills, "verify_result", lambda *a, **kw: pytest.fail("Lost custody cannot verify or apply a result"))
    with d["factory"]() as session:
        d["service"].after_job(session, jid)
        session.commit()
        assert session.get(Task, task.id).state == session.get(TaskRun, run.id).state == "input_required"
        assert json.loads(session.get(Task, task.id).blocker_json)["reason"] == d["service"].scope_blocker()["reason"]


def test_inline_call_attributes_usage_to_its_current_step_in_a_multistep_run(lifecycle):
    from app.db.task_models import TaskEvent, TaskStep
    d = lifecycle
    _admit(d)
    _, run, steps = _rows(d)
    with d["factory"]() as session:
        session.add(TaskStep(id="tst_later_scope", run_id=run.id, position=1, title="Later work",
            skill="tasks.reply_draft", state="submitted", deadline_at=run.deadline_at))
        session.commit()
    with d["usage"].run_context(run.id, step_id=steps[0].id):
        invocation = d["usage"].before_call(100, 100)
    with d["factory"]() as session:
        reserved = session.scalar(select(TaskEvent).where(TaskEvent.kind == "call_reserved"))
        assert reserved.run_id == run.id and reserved.step_id == steps[0].id
    d["usage"].after_call(invocation, 70, 60)
    with pytest.raises(ValueError, match="task_run_step_mismatch"):
        with d["usage"].run_context("foreign_run", step_id=steps[0].id):
            pytest.fail("A foreign run cannot acquire the step's usage context")


@pytest.mark.parametrize("change", ["owner", "workspace_archive", "venture_archive"])
def test_confirmed_inflight_usage_settles_after_custody_stop_without_resurrection(lifecycle, change):
    from app.db.task_models import Task, TaskEvent, TaskRun
    d = lifecycle
    jid = _admit(d)
    task, run, _ = _rows(d)
    with d["usage"].job_context(jid):
        invocation = d["usage"].before_call(100, 100)
    _change(d, change)
    with d["factory"]() as session:
        assert d["service"].before_job(session, jid, 1) is False
        session.commit()
    assert d["usage"].after_call(invocation, 70, 60) is None
    assert d["usage"].after_call(invocation, 70, 60) is None
    with d["factory"]() as session:
        current = session.get(TaskRun, run.id)
        assert (current.cost_micro, current.tokens_used, current.reserved_cost_micro, current.reserved_tokens) == (70, 60, 0, 0)
        assert current.state == session.get(Task, task.id).state == "input_required"
        assert len(session.scalars(select(TaskEvent).where(TaskEvent.kind == "call_settled")).all()) == 1


@pytest.mark.parametrize("state", ["auth_required", "approval_required", "input_required", "failed", "rejected", "completed", "paused", "canceled"])
@pytest.mark.parametrize("definitive", [True, False])
def test_late_usage_cannot_replace_an_unrelated_requirement_or_terminal_control(lifecycle, state, definitive):
    from app.db.task_models import Task, TaskRun
    from app.tasks.locks import lock_run
    d = lifecycle
    jid = _admit(d)
    task, run, _ = _rows(d)
    with d["usage"].job_context(jid):
        invocation = d["usage"].before_call(1, 1)
    reason = {"class": "needs_input", "reason": "Preserve this founder requirement", "unblock_action": "Review the original requirement"}
    with d["factory"]() as session:
        current, current_run = lock_run(session, run.id)
        current.state = current_run.state = state
        current.blocker_json = json.dumps(reason)
        current.budget_json = json.dumps({"cost_micro": 10})
        session.commit()
    if definitive:
        d["usage"].after_call(invocation, 11, 12)
    else:
        d["usage"].abort_call(invocation, definitive=False)
    with d["factory"]() as session:
        current, current_run = session.get(Task, task.id), session.get(TaskRun, run.id)
        assert current.state == current_run.state == state and json.loads(current.blocker_json) == reason
        assert (current_run.cost_micro, current_run.tokens_used, current_run.reserved_cost_micro, current_run.reserved_tokens) == (
            (11, 12, 0, 0) if definitive else (0, 0, 1, 1))


@pytest.mark.parametrize("state", ["completed", "failed", "canceled", "rejected"])
def test_duplicate_admission_never_resurrects_terminal_task_after_custody_loss(lifecycle, state):
    from app.db.task_models import Task, TaskEvent, TaskRun
    from app.tasks.locks import lock_run
    d = lifecycle
    jid = _admit(d)
    task, run, _ = _rows(d)
    with d["factory"]() as session:
        current, current_run = lock_run(session, run.id)
        current.state = current_run.state = state
        session.commit()
    _change(d, "owner")
    with d["factory"]() as session:
        count = session.scalar(select(func.count()).select_from(TaskEvent))
        assert d["service"].before_job(session, jid, 1) is False
        session.commit()
        assert session.get(Task, task.id).state == session.get(TaskRun, run.id).state == state
        assert session.scalar(select(func.count()).select_from(TaskEvent)) == count


@pytest.mark.parametrize("state", ["input_required", "auth_required", "approval_required", "paused"])
@pytest.mark.parametrize("side", ["task", "run"])
def test_same_working_attempt_cannot_bypass_current_waiting_state(lifecycle, state, side):
    from app.db.task_models import Task, TaskEvent, TaskRun
    from app.tasks.locks import lock_run
    d = lifecycle
    jid = _admit(d)
    claimed = _claim(d)
    run_id = _rows(d)[1].id
    with d["factory"]() as session:
        assert d["service"].before_job(session, jid, claimed.attempts)
        task, run = lock_run(session, run_id)
        (task if side == "task" else run).state = state
        task.blocker_json = json.dumps({"class": "needs_auth", "reason": "Preserved synthetic connection requirement"})
        session.commit()
    with d["factory"]() as session:
        count = session.scalar(select(func.count()).select_from(TaskEvent))
        assert d["service"].before_job(session, jid, claimed.attempts) is False
        session.commit()
        assert session.get(Task if side == "task" else TaskRun, task.id if side == "task" else run.id).state == state
        assert "Preserved" in session.get(Task, task.id).blocker_json
        assert session.get(TaskRun, run.id).attempts == 1
        assert session.scalar(select(func.count()).select_from(TaskEvent)) == count


def test_same_working_attempt_does_not_start_queued_event_run(lifecycle):
    from app.db.task_models import TaskEvent, TaskRun
    from app.tasks.locks import lock_run
    d = lifecycle
    jid = _admit(d)
    claimed = _claim(d)
    run_id = _rows(d)[1].id
    with d["factory"]() as session:
        assert d["service"].before_job(session, jid, claimed.attempts)
        _, run = lock_run(session, run_id)
        run.state = "queued"
        session.commit()
    with d["factory"]() as session:
        count = session.scalar(select(func.count()).select_from(TaskEvent))
        assert d["service"].before_job(session, jid, claimed.attempts) is False
        session.commit()
        assert session.get(TaskRun, run_id).state == "queued" and session.get(TaskRun, run_id).attempts == 1
        assert session.scalar(select(func.count()).select_from(TaskEvent)) == count


@pytest.mark.parametrize("settings", ['{"blocked":true}', '{"not-valid-json"'])
def test_blocked_or_invalid_workspace_settings_deny_creation_and_new_work(lifecycle, settings):
    from app.core.jobs import FatalError
    from app.db.models import Workspace
    from app.db.task_models import TaskRun
    d = lifecycle
    jid = _admit(d)
    _, run, _ = _rows(d)
    with d["factory"]() as session:
        session.get(Workspace, "ws_lifecycle").settings_json = settings
        session.commit()
    with d["factory"]() as session, pytest.raises(PermissionError):
        d["service"].create(session, workspace_id="ws_lifecycle", venture_id="ven_lifecycle",
            owner_person_id="usr_lifecycle", contract=_contract())
    with d["usage"].job_context(jid), pytest.raises(FatalError, match="task_scope_changed"):
        d["usage"].before_call(100, 100)
    with d["factory"]() as session:
        current = session.get(TaskRun, run.id)
        assert current.reserved_cost_micro == current.reserved_tokens == current.tokens_used == current.cost_micro == 0

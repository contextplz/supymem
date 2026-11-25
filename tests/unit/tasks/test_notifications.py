"""Synthetic task notice receipts on a minimal SQLite schema; no messages send."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tests.company_os_harness import load_company_os_app


@pytest.fixture
def notices(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    load_company_os_app()
    from app import desktop_notices
    from app.db.models import Base, Person, Venture, Workspace
    from app.db.supymem_models import Notice
    from app.db.task_models import Task, TaskEvent, TaskRun, TaskStep
    from core.clock import set_clock
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[model.__table__ for model in (Person, Workspace, Venture, Notice, Task, TaskRun, TaskEvent, TaskStep)])
    factory = sessionmaker(engine)
    now = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
    set_clock(lambda: now)
    monkeypatch.setattr(desktop_notices, "db", lambda: factory)
    with factory() as session:
        session.add(Person(id="usr_notice", display_name="Synthetic founder", created_at=now))
        session.add(Person(id="usr_other", display_name="Other founder", created_at=now))
        session.flush()
        session.add(Workspace(id="wsp_notice", owner_id="usr_notice", kind="company", entry_mode="idea", name="Synthetic", created_at=now, updated_at=now))
        session.flush()
        session.add(Venture(id="ven_notice", workspace_id="wsp_notice", name="Notice proof", raw_idea="Fixture", stage="draft", created_at=now, updated_at=now))
        session.commit()
    yield factory, desktop_notices, now.replace(tzinfo=None)
    set_clock(None)
    engine.dispose()


def seed(data, *, state="auth_required", kind="one_time", finished=False, age=0, person="usr_notice", task_owner="usr_notice", workspace="wsp_notice", target="/v/ven_notice/now", notice_kind="task:tsk_notice"):
    factory, _, now = data
    from app.db.supymem_models import Notice
    from app.db.task_models import Task, TaskRun
    with factory() as session:
        session.add(Task(id="tsk_notice", workspace_id=workspace, venture_id="ven_notice", owner_person_id=task_owner,
            title="Review the saved evidence", goal="Summarize evidence", kind=kind, state=state,
            blocker_json=json.dumps({"class": "needs_auth", "reason": "Connect the mailbox to continue.", "unblock_action": "Review the account connection."}),
            deadline_at=now - timedelta(minutes=1), created_at=now - timedelta(minutes=10), updated_at=now - timedelta(seconds=1)))
        session.flush()
        session.add(TaskRun(id="trn_notice", task_id="tsk_notice", run_key="first", state="completed" if finished else state,
            started_at=now - timedelta(minutes=10), deadline_at=now - timedelta(minutes=1),
            finished_at=now - timedelta(seconds=1) if finished else None))
        session.add(Notice(id="not_notice", workspace_id="wsp_notice", person_id=person, kind=notice_kind,
            title="Old provider title with internal IDs", body="old_call_id private provider body", target_path=target,
            created_at=now + timedelta(seconds=age), read_at=None))
        session.commit()


def pending(data):
    return data[1].pending(workspace_ids={"wsp_notice"}, person_id="usr_notice")


def test_current_blocker_is_verified_from_scoped_state_and_old_copy_is_not_shown(notices):
    seed(notices)
    result = pending(notices)
    assert len(result) == 1
    notice = result[0]
    assert notice["title"] == "Notice proof: Task needs your attention"
    assert notice["body"] == "Review the saved evidence: Connect the mailbox to continue. Review the account connection."
    assert notice["current_state_verified"] is True and notice["target_path"] == "/v/ven_notice/now"
    assert not any(value in notice["title"] + notice["body"] for value in ("tsk_notice", "old_call_id", "provider"))


@pytest.mark.parametrize("kind,state", [("one_time", "completed"), ("scheduled", "submitted")])
def test_only_current_finished_run_can_confirm_completion(notices, kind, state):
    seed(notices, state=state, kind=kind, finished=True)
    notice = pending(notices)[0]
    assert "Task finished" in notice["title"] and "result checks passed" in notice["body"]
    assert notice["current_state_verified"] is True


def test_customer_search_completion_is_in_chat_only_but_blockers_still_notify(notices, monkeypatch):
    from app import ui_config
    from app.db.task_models import Task, TaskRun, TaskStep
    flags = {"founder_ui": "home"}
    monkeypatch.setattr(ui_config, "get_policy", lambda key: flags)
    seed(notices)
    factory, _, now = notices
    with factory() as session:
        session.add(TaskStep(id="tst_customer_search", run_id="trn_notice", position=0,
            title="Find prospects and prepare drafts", skill="gtm.journey.prepare", state="auth_required", deadline_at=now))
        session.commit()
    assert pending(notices)[0]["title"] == "Notice proof: Task needs your attention"
    with factory() as session:
        session.get(Task, "tsk_notice").state = "completed"
        run = session.get(TaskRun, "trn_notice")
        run.state, run.finished_at = "completed", now - timedelta(seconds=1)
        session.get(TaskStep, "tst_customer_search").state = "completed"
        session.commit()
    assert pending(notices) == []
    flags["founder_ui"] = "classic"
    assert "Task finished" in pending(notices)[0]["title"]


def test_old_completion_never_describes_a_new_standing_run_as_finished(notices):
    from app.db.task_models import Task, TaskRun
    seed(notices, state="submitted", kind="scheduled", finished=True)
    factory, _, now = notices
    with factory() as session:
        session.get(Task, "tsk_notice").state = "working"
        session.add(TaskRun(id="trn_new", task_id="tsk_notice", run_key="second", state="working",
            started_at=now + timedelta(seconds=1), deadline_at=now + timedelta(minutes=10)))
        session.commit()
    assert pending(notices) == []


def test_acknowledged_completion_does_not_reappear_through_an_older_blocker_notice(notices):
    seed(notices, state="completed", finished=True, age=-60)
    assert pending(notices) == []


def test_a_stopped_event_run_reports_its_saved_reason_without_stopping_the_standing_task(notices):
    from app.db.task_models import TaskEvent, TaskRun
    seed(notices, state="submitted", kind="event", finished=True)
    factory, _, now = notices
    with factory() as session:
        session.get(TaskRun, "trn_notice").state = "rejected"
        session.add(TaskEvent(id="tev_stopped", task_id="tsk_notice", run_id="trn_notice", kind="blocker",
            message="Reply should not be followed up", created_at=now - timedelta(seconds=1),
            data_json=json.dumps({"class": "policy", "run_only": True,
                "reason": "This reply asked not to be contacted.", "unblock_action": "Review the saved reply."})))
        session.commit()
    result = pending(notices)[0]
    assert "Task run stopped" in result["title"] and "asked not to be contacted" in result["body"]
    assert result["current_state_verified"] is True


def test_eta_notice_requires_current_overdue_run_and_its_persisted_event(notices):
    from app.db.task_models import TaskEvent
    seed(notices, state="working")
    assert pending(notices) == []
    factory, _, now = notices
    with factory() as session:
        session.add(TaskEvent(id="tev_eta", task_id="tsk_notice", run_id="trn_notice", kind="eta_slip",
            message="Synthetic persisted ETA slip", created_at=now - timedelta(seconds=1)))
        session.commit()
    notice = pending(notices)[0]
    assert "Task estimate changed" in notice["title"] and "longer than its estimate" in notice["body"]
    assert notice["current_state_verified"] is True


@pytest.mark.parametrize("changes", [{"task_owner": "usr_other"}, {"person": None}, {"workspace": "foreign_workspace"},
    {"target": "/v/ven_other/now"}, {"notice_kind": "task:tsk_missing"}, {"notice_kind": "task:invalid"}])
def test_foreign_owner_unaddressed_foreign_target_and_missing_source_fail_closed(notices, changes):
    seed(notices, **changes)
    assert pending(notices) == []


def test_obsolete_blocker_is_suppressed_and_distinct_task_receipts_dedupe(notices):
    from app.db.supymem_models import Notice
    from app.db.task_models import Task
    seed(notices)
    factory, _, now = notices
    with factory() as session:
        session.add(Notice(id="not_duplicate", workspace_id="wsp_notice", person_id="usr_notice", kind="task:tsk_notice",
            title="Older title", body="Old copy", target_path="/v/ven_notice/now", created_at=now - timedelta(milliseconds=1), read_at=None))
        session.commit()
    assert len(pending(notices)) == 1
    with factory() as session:
        session.get(Task, "tsk_notice").state = "paused"
        session.commit()
    assert pending(notices) == []
    with factory() as session:
        assert session.get(Notice, "not_notice").read_at is None


def test_dashboard_next_step_uses_only_current_handoff_blockers():
    load_company_os_app()
    from app.api.dashboard import _handoff_next_step
    fallback = {"key": "plan.review", "title": "Review a historical plan"}
    run = {"stage": "Your task", "state": "needs_you", "activity": "A mailbox connection is required.",
           "next_step": "Connect your mailbox.", "action": {"label": "Connect the required account", "href": "/v/ven_notice/connections"}}
    step = _handoff_next_step(run, fallback)
    assert step["key"] == "handoff.needs_you" and step["href"] == run["action"]["href"]
    assert step["title"] == run["activity"] and step["detail"] == run["next_step"]
    assert step["chat_action"] is None and step["payload"] is None
    for change in ({"state": "working"}, {"state": "paused"}, {"stage": "Research"}, {"action": None}):
        assert _handoff_next_step({**run, **change}, fallback) is fallback

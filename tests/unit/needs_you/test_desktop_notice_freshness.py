"""Desktop copy derives current exact scoped requirements, never old execution claims."""
from datetime import timedelta

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def notice_db(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/notice.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("NEEDS_YOU_QUIET_HOURS", "0-0")
    load_company_os_app()
    from app.state import reset_db_factory
    reset_db_factory()
    monkeypatch.setattr("app.email.triggers.on_needs_you", lambda *args, **kwargs: None)
    from tests.unit.test_desktop_notices import _seed_notice_venture
    _seed_notice_venture()
    from app.db.models import Venture
    from app.state import db
    with db()() as session:
        return session.get(Venture, "ven_1").workspace_id


def _human(status="open", *, task_id="ht_exact", venture_id="ven_1"):
    from app.db.models import HumanTask
    from core.clock import utcnow
    return HumanTask(id=task_id, venture_id=venture_id, title="Current founder requirement", why="Review the saved result. I am carrying on with everything else.",
        provider="manual", category="manual", status=status, priority=1, blocks_json="[]", lead_time_days=0,
        steps_json="[]", expects_json="{}", created_at=utcnow(), updated_at=utcnow())


def test_closed_human_notice_is_suppressed_without_mutating_task_or_history(notice_db):
    from app import desktop_notices
    from app.db.models import HumanTask
    from app.db.supymem_models import Notice
    from app.state import db
    with db()() as session:
        session.add(_human("done"))
        session.commit()
    nid = desktop_notices.enqueue(title="Old needs you", body="I am carrying on with everything else.", venture_id="ven_1",
        kind="human_task", target_path="/v/ven_1#attention-ht_exact")
    assert desktop_notices.pending(workspace_ids={notice_db}) == []
    with db()() as session:
        assert session.get(HumanTask, "ht_exact").status == "done"
        assert session.get(Notice, nid).read_at is None  # GET projection is read-only


def test_current_task_replaces_old_copy_and_keeps_verified_venture_name(notice_db):
    from app import desktop_notices
    from app.state import db
    with db()() as session:
        session.add(_human())
        session.commit()
    desktop_notices.enqueue(title="Old title", body="Three old blockers and active work", venture_id="ven_1",
        kind="human_task", target_path="/v/ven_1#attention-ht_exact")
    row = desktop_notices.pending(workspace_ids={notice_db})[0]
    assert row["title"] == "Notice QA: Supymem needs you: Current founder requirement"
    assert row["body"] == "Review the saved result."
    assert row["venture_id"] == "ven_1" and row["venture_name"] == "Notice QA"
    assert row["current_state_verified"] is True


def test_needs_you_count_uses_only_current_exact_workspace_rows(notice_db):
    from app import desktop_notices
    from app.db.supymem_models import NeedsYouRequest, Notice
    from app.state import db
    from core.clock import utcnow
    now = utcnow()
    with db()() as session:
        for identifier, state in (("ny_open", "open"), ("ny_resolved", "resolved")):
            session.add(NeedsYouRequest(id=identifier, workspace_id=notice_db, venture_id="ven_1", reason_code="real_world_action",
                title="Read saved evidence", what_we_need="Review the result. I carry on with everything else.", how_to_resume="Open saved evidence.",
                blocking_ref=identifier, severity="normal", status=state, created_at=now))
        session.commit()
    nid = desktop_notices.enqueue(title="Supymem has 3 blockers", body="I am carrying on with everything else.", kind="needs_you", venture_id="ven_1")
    row = desktop_notices.pending(workspace_ids={notice_db})[0]
    assert row["title"] == "Notice QA: Supymem has a blocker"
    assert "carrying" not in row["body"] and "carry on" not in row["body"] and "3 blockers" not in row["body"]
    with db()() as session:
        session.get(NeedsYouRequest, "ny_open").status = "resolved"
        session.commit()
    assert desktop_notices.pending(workspace_ids={notice_db}) == []
    with db()() as session:
        assert session.get(Notice, nid).read_at is None


def test_foreign_target_missing_task_and_archived_venture_do_not_certify_notice(notice_db):
    from app import desktop_notices
    from app.db.models import Venture
    from app.db.supymem_models import Notice
    from app.state import db
    from core.clock import utcnow
    with db()() as session:
        session.add(_human(task_id="ht_foreign", venture_id="ven_other"))
        session.commit()
    for task in ("ht_foreign", "ht_missing"):
        desktop_notices.enqueue(title="Wrong source", body="Old requirement", kind="human_task", venture_id="ven_1",
            target_path=f"/v/ven_1#attention-{task}")
    nid = desktop_notices.enqueue(title="Mismatch", body="Wrong workspace", venture_id="ven_1")
    with db()() as session:
        session.get(Notice, nid).workspace_id = "foreign_workspace"
        session.commit()
    assert desktop_notices.pending() == []
    with db()() as session:
        session.get(Venture, "ven_1").archived_at = utcnow()
        session.commit()
    assert desktop_notices.pending() == []


def test_filtered_history_cannot_starve_pending_limit_and_distinct_tasks_remain(notice_db):
    from app import desktop_notices
    from app.db.supymem_models import Notice
    from app.state import db
    from core.clock import utcnow
    with db()() as session:
        session.add_all([_human(task_id="ht_one"), _human(task_id="ht_two")])
        session.commit()
    first = desktop_notices.enqueue(title="First", body="Saved", kind="human_task", venture_id="ven_1", target_path="/v/ven_1#attention-ht_one")
    desktop_notices.enqueue(title="Second", body="Saved", kind="human_task", venture_id="ven_1", target_path="/v/ven_1#attention-ht_two")
    for _ in range(25):
        desktop_notices.enqueue(title="Resolved history", body="No task", kind="human_task", venture_id="ven_1", target_path="/v/ven_1#attention-ht_missing")
    one = desktop_notices.pending(limit=1, workspace_ids={notice_db})
    assert len(one) == 1 and one[0]["target_path"].endswith("ht_two")
    both = desktop_notices.pending(workspace_ids={notice_db})
    assert {row["target_path"] for row in both} == {"/v/ven_1#attention-ht_one", "/v/ven_1#attention-ht_two"}
    with db()() as session:
        session.get(Notice, first).created_at = utcnow() - timedelta(days=30)
        session.commit()
    assert len(desktop_notices.pending(workspace_ids={notice_db})) == 2  # a real open task never expires by age alone


def test_unknown_saved_notice_does_not_present_historical_activity_as_current(notice_db):
    from app import desktop_notices
    desktop_notices.enqueue(title="Earlier update", body="I am carrying on with everything else. Three saved events.", venture_id="ven_1")
    row = desktop_notices.pending(workspace_ids={notice_db})[0]
    assert row["title"] == "Notice QA: Saved notice: Earlier update"
    assert row["body"].startswith("Saved ") and "carrying" not in row["body"]
    assert row["current_state_verified"] is False

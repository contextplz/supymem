"""Replan must reuse fingerprints without UNIQUE failures."""

from __future__ import annotations

import pytest


@pytest.fixture()
def planning_env(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/replan.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    from tests.company_os_harness import load_company_os_app
    load_company_os_app()
    from app import state

    state.reset_db_factory()
    state.db()  # create tables
    return tmp_path


@pytest.mark.parametrize("title", ["Review the warehouse pilot", "Review the family cooking workshop"])
def test_same_plan_refresh_preserves_dismissed_task_binding(planning_env, title):
    from app.db.models import PlannedTask
    from app.planning.store import save_tasks
    from app.state import db

    task = {"title": title, "why_now": "Review the proposal", "tags": ["plan_task:bound_task"]}
    first = save_tasks("venture", [task], source="approved_plan:current")
    with db()() as session:
        session.get(PlannedTask, first[0]["id"]).status = "dismissed"
        session.commit()
    assert save_tasks("venture", [task], source="approved_plan:current") == []
    changed = {**task, "tags": ["plan_task:changed_task"]}
    assert len(save_tasks("venture", [changed], source="approved_plan:replacement")) == 1


def test_save_tasks_replan_upserts_fingerprints(planning_env):
    from app.planning.store import save_tasks

    first = save_tasks(
        "ven_replan_fp_test",
        [
            {
                "title": "Interview five buyers",
                "why_now": "Validate demand",
                "est_minutes": 40,
                "tags": ["research"],
            },
            {
                "title": "Ship waitlist CTA",
                "why_now": "Capture interest",
                "est_minutes": 30,
                "tags": ["product"],
            },
        ],
        source="planner",
    )
    assert len(first) == 2

    # Same titles / fingerprints via replan must not raise IntegrityError.
    second = save_tasks(
        "ven_replan_fp_test",
        [
            {
                "title": "Interview five buyers",
                "why_now": "Still the top gap",
                "est_minutes": 45,
                "tags": ["research"],
            },
            {
                "title": "Ship waitlist CTA",
                "why_now": "Close the loop",
                "est_minutes": 25,
                "tags": ["product"],
            },
            {
                "title": "Draft competitor brief",
                "why_now": "New angle",
                "est_minutes": 35,
                "tags": ["research"],
            },
        ],
        source="replan",
    )
    assert len(second) == 3
    assert second[0]["title"] == "Interview five buyers"


def test_transactional_save_preserves_blocked_state_and_can_roll_back(planning_env):
    from app.planning.store import list_open_tasks, save_tasks
    from app.state import db

    with db()() as session:
        saved = save_tasks(
            "ven_atomic_plan_test",
            [
                {
                    "title": "Founder completes provider setup",
                    "why_now": "The approved plan requires a founder-owned credential.",
                    "runnable": False,
                }
            ],
            source="approved_plan:pln_atomic",
            session=session,
            publish=False,
        )
        assert saved[0]["runnable"] is False
        session.rollback()

    assert list_open_tasks("ven_atomic_plan_test") == []


def test_transactional_save_cannot_publish_before_commit(planning_env):
    from app.planning.store import save_tasks
    from app.state import db

    with db()() as session, pytest.raises(ValueError, match="cannot_publish_uncommitted_tasks"):
        save_tasks(
            "ven_uncommitted_publish_test",
            [{"title": "Draft offer", "why_now": "Test demand"}],
            session=session,
        )

"""GM-1/2/4: the Mac heartbeat gate, the lifetime cap and per-tick ceiling, and Stop everything."""
import asyncio
from datetime import timedelta

import pytest

from tests.unit.autopilot.test_god_mode import _fake_chat, _on, env  # noqa: F401


def _beat(person, age_min):
    from app.db.models import AnalyticsEvent
    from app.state import db
    from core.clock import utcnow
    from core.ids import new_id

    with db()() as s:
        s.add(AnalyticsEvent(id=new_id("ae"), person_id=person, event_name="desktop.process_heartbeat",
                             occurred_at=utcnow() - timedelta(minutes=age_min)))
        s.commit()


def test_tick_all_skips_silent_mac_unless_continue_when_offline(env, monkeypatch):
    client, vid = env
    from app.autopilot import service
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    monkeypatch.setattr("app.worker.handlers.hosted_mode", lambda: True)
    seen = []

    async def fake_tick(v):
        seen.append(v)
    monkeypatch.setattr(service, "tick", fake_tick)
    _on(client, vid)
    owner = service._owner(vid)
    asyncio.run(service.tick_all())
    assert seen == []  # never seen = offline
    assert "Mac is offline" in service.status(vid)["working_on"]
    assert client.put(f"/ventures/{vid}/autopilot", json={"continue_when_offline": True}).json()["continue_when_offline"]
    asyncio.run(service.tick_all())
    assert seen == [vid]  # founder opted in
    client.put(f"/ventures/{vid}/autopilot", json={"continue_when_offline": False})
    seen.clear()
    _beat(owner, 30)
    asyncio.run(service.tick_all())
    assert seen == []  # stale
    _beat(owner, 1)
    asyncio.run(service.tick_all())
    assert seen == [vid]  # fresh


def test_legacy_daily_and_lifetime_caps_never_refuse_work(env):
    client, vid = env
    from app.autopilot import guard
    from app.credits.units import MICRO_PER_CREDIT
    _on(client, vid)
    s = guard.get_settings(vid)
    assert s["lifetime_cap_credits"] is None
    s.update(daily_cap_credits=0, lifetime_cap_credits=2500,
             spent_today_micro=5000 * MICRO_PER_CREDIT, lifetime_spent_micro=5000 * MICRO_PER_CREDIT)
    assert guard.check(vid, "research.start", settings=s).ok
    assert guard.check(vid, "research.start", settings=s, human_approved=True).ok
    assert guard.check_mac_manifest(vid, {"actions": ["browser.read"], "credit_ceiling_micro": 300 * MICRO_PER_CREDIT}, settings=s).ok
    assert guard.check(vid, "plan.generate", settings=s).ok  # unquoted, free


def test_fourth_consequential_step_in_a_tick_is_refused(env, monkeypatch):
    client, vid = env
    from app.autopilot import service
    _fake_chat(monkeypatch, [])
    _on(client, vid)
    done = []

    async def run():
        token = service._tick_used.set([0])
        try:
            return [(await service._execute(vid, "plan.generate", {}, "t", service._owner(vid), done))[1] for _ in range(4)]
        finally:
            service._tick_used.reset(token)
    stops = asyncio.run(run())
    assert stops[3] == "tick_ceiling" and "tick_ceiling" not in stops[:3]


def test_stop_everything_cancels_work_and_turns_god_mode_off(env):
    client, vid = env
    from app.chat import execute
    from app.db.models import PlannedTask
    from app.state import db
    from core.clock import utcnow
    _on(client, vid)
    with db()() as s:
        for i, st in enumerate(("open", "running")):
            s.add(PlannedTask(id=f"pt_stop{i}", venture_id=vid, title=f"t{i}", why_now="x", status=st, runnable=1,
                              created_at=utcnow(), updated_at=utcnow()))
        s.commit()
    asyncio.run(execute._pause(vid))
    with db()() as s:
        assert {r.status for r in s.query(PlannedTask).filter(PlannedTask.id.like("pt_stop%"))} == {"dismissed"}
    assert client.get(f"/ventures/{vid}/autopilot").json()["god_mode"] is False


@pytest.mark.parametrize("title", ["Reconcile warehouse invoices", "Plan weekend family hikes"])
def test_pause_preserves_work_until_stop_everything_is_confirmed(env, title):
    from app.autopilot.service import _owner
    from app.chat import execute
    from app.chat.router import handle_message
    from app.db.models import PlannedTask
    from app.planning.store import save_tasks
    from app.state import db

    client, vid = env
    save_tasks(vid, [{"title": title, "why_now": "Saved work", "runnable": True}], source="planner")
    with db()() as session:
        tasks = session.query(PlannedTask).filter_by(venture_id=vid).all()
        task_ids = {task.id for task in tasks}
        tasks[0].status = "running"
        session.commit()
    _on(client, vid)
    paused = asyncio.run(execute.execute_action(vid, "pause"))
    assert "saved tasks are kept" in paused["body_md"]
    with db()() as session:
        assert {task.id for task in session.query(PlannedTask).filter_by(venture_id=vid)} == task_ids
        assert all(task.status == "open" for task in session.query(PlannedTask).filter_by(venture_id=vid))
    asyncio.run(execute.execute_action(vid, "resume"))
    _on(client, vid)
    actor = _owner(vid)
    reply = asyncio.run(handle_message(vid, "stop everything", actor_subject=actor))
    assert reply["kind"] != "confirmation"
    stopped = reply
    assert "cancelled" in stopped["body_md"]
    with db()() as session:
        tasks = session.query(PlannedTask).filter_by(venture_id=vid).all()
        assert {task.id for task in tasks} == task_ids
        assert all(task.status == "dismissed" and not task.runnable for task in tasks)
    assert client.get(f"/ventures/{vid}/autopilot").json()["god_mode"] is False

"""God mode runs the whole lifecycle: step -> move state machine, drivers, blocked-on-human branching,
never-stuck recovery, the floors for approved suggestions, and the proposals API."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest

from tests.unit.autopilot.test_god_mode import _fake_chat, _on, _step, env as _shared_env

env = _shared_env


def _msgs(vid):
    from app.chat.store import list_messages

    return [m["body_md"] for m in list_messages(vid, limit=50)]


# ---- journey step -> move (the lifecycle state machine) ----

@pytest.mark.parametrize("key,chat_action,expect_action,human,wait", [
    ("idea.clarify", None, "idea.answer", False, False),
    ("idea.review_validation", None, "idea.advance", False, False),
    ("research.start", "research.start", "research.start", False, False),
    ("plan.generate", "plan.generate", "plan.generate", False, False),
    ("plan.review", "plan.approve", "plan.approve", False, False),
    ("build.first", None, "build.prepare", False, False),
    ("work.next_task", "build.prepare", "build.prepare", False, False),
    ("site.publish", "site.publish", "site.publish", False, False),
    ("gtm.outreach", None, "outreach.draft", False, False),
    ("gtm.diagnose", "gtm.diagnosis", None, True, False),  # the diagnosis is the founder's own description; nothing to run
    ("experiment.run", None, None, True, False),
    ("experiment.review", None, None, True, False),
    ("build.verify_product", None, None, True, False),  # hosting hook: a founder decision until it is wired
    ("work.running", None, None, False, True),
    ("idea.validating", None, None, False, True),
])
def test_every_journey_step_has_a_move(env, key, chat_action, expect_action, human, wait):
    from app.autopilot import lifecycle

    mv = lifecycle._step_move({"key": key, "title": "T", "detail": "d", "chat_action": chat_action, "href": "/x"})
    assert (mv.action, mv.human, mv.wait) == (expect_action, human, wait)


@pytest.mark.parametrize("question_id", ["", "question_current"])
def test_clarification_move_waits_until_an_exact_question_exists(env, monkeypatch, question_id):
    from app.autopilot import lifecycle

    monkeypatch.setattr(lifecycle, "_open_question_id", lambda _: question_id)
    move = lifecycle._step_move({"key": "idea.clarify", "title": "Answer", "chat_action": None}, env[1])
    assert move.wait is (not question_id)
    assert move.action == ("idea.answer" if question_id else None)
    assert move.payload == ({"question_id": question_id} if question_id else {})


def test_executable_step_describes_the_concrete_effect(env):
    from app.autopilot import lifecycle

    detail = "Your paying customers are demand evidence. Test the next repeatable acquisition channel."
    move = lifecycle._step_move({"key": "gtm.outreach", "title": "Grow what's already working",
                                 "detail": detail, "chat_action": "outreach.draft"})
    assert move.will_do == detail
    assert "Runs the step" not in move.will_do


# ---- drivers ----

def _turn(vid, role, kind, text, options, age_min, n):
    from core.ids import new_id
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.state import db
    from core.clock import utcnow
    from sqlalchemy import select

    with db()() as s:
        idea = s.scalar(select(IdeaSession).where(IdeaSession.venture_id == vid))
        s.add(ClarifierTurn(id=new_id("ict"), session_id=idea.id, workspace_id=idea.workspace_id, venture_id=vid,
                            turn=n, role=role, kind=kind, text=text, options=json.dumps(options) if options else None,
                            created_at=utcnow() - timedelta(minutes=age_min)))
        s.commit()
        return idea.id


def test_silent_founder_gets_an_ai_assumed_answer_and_a_present_one_is_left_alone(env, monkeypatch):
    client, vid = env
    from app.autopilot import lifecycle

    from app.db.supymem_models import IdeaSession
    from app.state import db
    from sqlalchemy import select

    with db()() as s:
        sid = s.scalar(select(IdeaSession.id).where(IdeaSession.venture_id == vid))
    if sid is None:
        sid = client.post(f"/ventures/{vid}/idea/sessions").json()["id"]
    sent = []
    monkeypatch.setattr("app.idea.service.post_message", lambda *a, **k: sent.append((a, k)) or {})
    _turn(vid, "agent", "question", "Who pays first?", ["Freelance designers", "Agencies"], 1, 900)
    assert lifecycle.answer_clarification(vid, "u1")["kind"] == "wait" and sent == []  # asked 1 minute ago
    _turn(vid, "agent", "question", "Who pays first?", ["Freelance designers", "Agencies"], 30, 901)
    out = lifecycle.answer_clarification(vid, "u1")
    assert "AI-assumed" in out["body_md"] and sent[0][0][1] == lifecycle.ASSUMED + "Freelance designers"


def test_unknown_driver_is_refused(env):
    client, vid = env
    from app.autopilot import lifecycle

    with pytest.raises(ValueError):
        asyncio.run(lifecycle.run_driver(vid, "idea.explode", {}, None))


# ---- blocked on a human: ask once, keep going on other branches ----

def test_human_only_step_asks_once_and_other_branches_still_run(env, monkeypatch):
    client, vid = env
    from app.autopilot import lifecycle, service

    _on(client, vid)
    human = {"key": "experiment.run", "title": "Run: interview 5 designers", "detail": "Real people only.",
             "chat_action": None, "href": f"/v/{vid}/experiments"}
    calls = _fake_chat(monkeypatch, [human, human])
    # The pending human step persists until the founder supplies its result;
    # status reads must not consume a synthetic finite journey queue.
    monkeypatch.setattr("app.ventures.next_step.next_step", lambda _v: human)
    branch = lifecycle.Move("branch.outreach", "Draft outreach", "w", "d", "outreach.draft", {})
    monkeypatch.setattr(lifecycle, "branches", lambda _v: [branch])
    out = asyncio.run(service.tick(vid))
    assert [c[0] for c in calls] == ["outreach.draft"] and out["done"] == ["outreach.draft"]
    events = service.timeline(vid)
    assert any(e["outcome"] == "needs_you" and "interview 5 designers" in e["what"] for e in events)
    asked = [m for m in _msgs(vid) if "Supymem needs you" in m]
    assert len(asked) == 1
    asyncio.run(service.tick(vid))  # same day: no second card
    assert len([m for m in _msgs(vid) if "Supymem needs you" in m]) == 1
    st = service.status(vid)
    # The separately queued clarifier is not working yet. Human requests must
    # remain visible while that durable background queue is projected.
    assert st["state"] == "queued"
    move = lifecycle._step_move(human)
    assert st["waiting_on_you"] == [{"title": human["title"], "why": move.why, "href": human["href"]}]


def test_status_and_daily_summary(env, monkeypatch):
    client, vid = env
    from app.autopilot import service
    from core import clock

    assert service.status(vid)["state"] == "off"
    _on(client, vid)
    service.record(vid, "research.start", "Started research")
    service.record(vid, "human:experiment.run", "Needs you: run the test", outcome="needs_you")
    st = client.get(f"/ventures/{vid}/autopilot").json()
    assert st["status"]["state"] != "off" and st["allow_mac"] is False
    assert st["brief"]["now"] and st["brief"]["waiting_on_you"] == [] and "Started deep research" in [r["text"] for r in st["brief"]["recent"]]
    real = clock.utcnow()
    monkeypatch.setattr(service, "utcnow", lambda: real.replace(hour=19))
    assert service.daily_summary(vid) is True
    assert service.daily_summary(vid) is False  # once a day
    body = [m for m in _msgs(vid) if "Today with God mode" in m][0]
    assert "**Started today:** Started deep research" in body and "**Waiting on you:**" not in body
    assert "Attempting" not in body and "Guarded" not in body


# ---- never stuck ----

def test_an_exception_holds_for_review_without_automatic_alternative(env, monkeypatch):
    client, vid = env
    from app.autopilot import service
    from app.chat import execute, recovery

    _on(client, vid)
    calls = []

    async def boom(venture_id, name, *, payload=None, actor_subject=None):
        calls.append(name)
        if name == "plan.generate":
            raise RuntimeError("provider timeout")
        return {"kind": "text", "body_md": "ok", "actions": []}

    async def alt(venture_id, payload, ws=None, actor=None):
        assert payload["step"] == "plan.compile"
        card = {"label": "Use the starter plan", "action": "recovery.plan_template",
                "confirmation": {"action": "recovery.plan_template"}}
        return {"kind": "confirmation", "body_md": "x", "actions": [card], "citations": []}

    monkeypatch.setattr(execute, "execute_action", boom)
    monkeypatch.setattr(recovery, "alternative", alt)
    monkeypatch.setattr("app.ventures.next_step.next_step", lambda _v: _step("plan.generate"))
    asyncio.run(service.tick(vid))
    assert calls == ["plan.generate"]  # Unknown effects never admit an automatic replay.
    assert not any("provider timeout" in m for m in _msgs(vid))
    assert any("could not be confirmed" in m for m in _msgs(vid))
    assert any(e["outcome"] == "held" for e in service.timeline(vid))


# ---- floors for an approved suggestion ----

def test_human_approved_skips_god_mode_and_cap_but_not_credentials_sends_or_the_allow_list(env):
    client, vid = env
    from app.autopilot import guard
    from app.credits.units import MICRO_PER_CREDIT

    big = {"ceiling_micro": 2_000 * MICRO_PER_CREDIT}
    assert guard.check(vid, "plan.generate").reason == "god_mode_off"
    assert guard.check(vid, "plan.generate", big, human_approved=True).ok
    assert guard.check(vid, "build.start", {"api_key": "x"}, human_approved=True).reason == "credentials_never_entered"
    assert guard.check(vid, "social_post", human_approved=True).reason == "sending_not_allowed"
    assert guard.check(vid, "pause", human_approved=True).reason == "action_not_allowed"
    _on(client, vid, daily_cap_credits=1)
    assert guard.check(vid, "plan.generate", big).ok


def test_allow_mac_actions_widens_scope_but_never_terminal_or_credentials(env):
    client, vid = env
    from app.autopilot import guard

    _on(client, vid)
    click = {"actions": ["browser.read", "browser.interact"], "credit_ceiling_micro": 0}
    assert guard.check_mac_manifest(vid, click).reason == "mac_scope_not_read_only"
    assert client.put(f"/ventures/{vid}/autopilot", json={"allow_mac": True}).json()["allow_mac"] is True
    assert guard.check_mac_manifest(vid, click).ok
    assert guard.check_mac_manifest(vid, {**click, "actions": ["terminal.execute"]}).reason == "mac_scope_not_read_only"
    assert guard.check_mac_manifest(vid, {**click, "fill_values": {"password": "x"}}).reason == "credentials_never_entered"


# ---- proposals API ----

def test_propose_and_execute_a_suggestion_with_god_mode_off(env, monkeypatch):
    client, vid = env
    from app.autopilot import actions, lifecycle, service

    calls = _fake_chat(monkeypatch, [_step("research.start", title="Start deep research")] * 3)
    monkeypatch.setattr(lifecycle, "branches", lambda _v: [
        lifecycle.Move("branch.outreach", "Draft outreach", "w", "Writes drafts.", "outreach.draft", {})])
    props = actions.propose_next_actions(vid, 5)
    assert [p.kind for p in props] == ["research.start", "outreach.draft"]
    first = props[0]
    assert first.estimated_credits == 20 and first.reversible and not first.requires_mac and first.id.startswith("mv_")
    assert first.id == actions.propose_next_actions(vid, 5)[0].id  # stable id
    assert len(actions.propose_next_actions(vid, 1)) == 1
    out = asyncio.run(actions.execute_proposed_action(vid, first, service._owner(vid)))
    assert out["ok"] and calls[0][0] == "research.start"  # God mode is off; the founder said go ahead


def test_human_only_proposals_do_nothing_and_secrets_are_refused(env, monkeypatch):
    client, vid = env
    from app.autopilot import actions, service

    calls = _fake_chat(monkeypatch, [{"key": "experiment.run", "title": "Run the test", "chat_action": None, "href": "/x"}])
    props = actions.propose_next_actions(vid)
    assert props[0].human_only and props[0].kind == "human"
    assert asyncio.run(actions.execute_proposed_action(vid, props[0], "u1"))["reason"] == "needs_you"
    bad = {"kind": "build.start", "payload": {"password": "x"}, "title": "t"}
    assert asyncio.run(actions.execute_proposed_action(vid, bad, service._owner(vid)))["reason"] == "credentials_never_entered"
    assert calls == []


def test_publish_and_mac_proposals_are_marked_irreversible_or_mac(env):
    from app.autopilot import actions, lifecycle

    pub = actions._to_proposal(lifecycle.Move("k", "Publish", "w", "d", "site.publish", {}))
    mac = actions._to_proposal(lifecycle.Move("k", "Mac", "w", "d", "mac.approve", {"order_id": "o"}))
    assert not pub.reversible and mac.requires_mac and not mac.reversible


def test_a_finished_build_waiting_for_the_founder_is_a_human_step_not_a_new_build(env, monkeypatch):
    """After QA passes, accepting is the founder's call. The journey must say so (one ask) rather than offer to prepare
    another build, which used to end 'held' and silently stall the venture."""
    client, vid = env
    from app.autopilot import lifecycle
    from app.ventures import next_step as ns

    facts = {"paused": False, "gtm_entry": False, "first_question_pending": False, "active_jobs": False, "personal": False,
             "idea_confirmed": True, "plan_state": "approved", "plan_has_proposal": True, "exp_review": False, "exp_run": None,
             "external_site": False, "build_review_pending": True, "products": 1, "accepted_build": False,
             "product_attempt": False, "accepted_product": False, "workspace_id": "w", "dossier_state": "complete"}
    step = ns._resolve(vid, f"/v/{vid}", facts)
    assert step["key"] == "build.review" and step["chat_action"] is None
    mv = lifecycle._step_move(step, vid)
    assert mv.human and mv.action is None and mv.human_href.endswith("/builds")

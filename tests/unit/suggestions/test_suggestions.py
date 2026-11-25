"""Suggestions: generator dedupe and cooldown, the decide API, and accept -> guarded execution (mocked)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from tests.company_os_harness import company_os_test_client as TestClient

from tests.company_os_harness import load_company_os_app
from tests.company_os_harness import configure_local_test_model


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/sug.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_MEMORY_PATH", str(tmp_path / "mem.sqlite3"))
    monkeypatch.setenv("COMPANY_OS_BRAIN_GRAPH_PATH", str(tmp_path / "graph.sqlite3"))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    main = load_company_os_app()
    configure_local_test_model()
    from app import state as state_mod
    from app.memory import reset_memory_for_tests

    monkeypatch.setattr("app.suggestions.proposals.god_actions", lambda: None)  # tests below opt in to God mode's API
    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    reset_memory_for_tests(tmp_path / "mem.sqlite3")
    with TestClient(main.create_app()) as client:
        created = client.post("/ventures", json={"raw_idea": "a marketplace for restoring vintage synthesizers"})
        assert created.status_code == 201, created.text
        yield client, created.json()["id"]


def _proposals(monkeypatch, items):
    shaped = [{"key": k, "title": f"Do {k}", "why": "because", "plan_text": "I will do it", "est_credits": 20,
               "action_kind": k, "payload": {}, "requires_mac": False} for k in items]
    monkeypatch.setattr("app.suggestions.proposals.propose_next_actions", lambda _v, limit=5: [dict(p) for p in shaped])


def _rows(vid):
    from app.db.supymem_models import Suggestion
    from app.state import db
    from sqlalchemy import select

    with db()() as s:
        return list(s.scalars(select(Suggestion).where(Suggestion.venture_id == vid)))


def test_fallback_plan_uses_step_detail(env, monkeypatch):
    from app.suggestions import proposals

    detail = "Test the next repeatable acquisition channel."
    monkeypatch.setattr("app.ventures.next_step.next_step", lambda _vid: {
        "key": "gtm.outreach", "title": "Grow what's already working", "detail": detail,
        "chat_action": "outreach.draft", "payload": {},
    })
    assert proposals.propose_next_actions(env[1])[0]["title"] == "Draft 3 outreach emails for review"


def test_generate_dedupes_and_respects_the_dismiss_cooldown(env, monkeypatch):
    client, vid = env
    from app.state import db
    from app.suggestions import service
    from core.clock import utcnow

    _proposals(monkeypatch, ["research.start", "waitlist.enable"])
    assert len(service.generate(vid)) == 2
    assert service.generate(vid) == []  # already waiting: no duplicates
    first = _rows(vid)[0]
    assert service.dismiss(first.id, "not_now")
    assert service.generate(vid) == []  # dismissed: cooldown
    with db()() as s:  # a week later it may come back
        row = s.get(type(first), first.id)
        row.decided_at = utcnow() - timedelta(days=8)
        s.commit()
    assert len(service.generate(vid)) == 1
    assert [r.status for r in _rows(vid)].count("dismissed") == 1


def test_generate_caps_fresh_cards(env, monkeypatch):
    _, vid = env
    from app.suggestions import service

    _proposals(monkeypatch, ["research.start", "idea.answer", "outreach.draft", "idea.advance", "waitlist.enable", "recovery.plan_template"])
    assert len(service.generate(vid)) == service.MAX_NEW


def test_generate_enqueues_a_desktop_notice(env, monkeypatch):
    _, vid = env
    from app import desktop_notices
    from app.suggestions import service

    _proposals(monkeypatch, ["research.start"])
    service.generate(vid)
    assert any(n["kind"] == "suggestions" and n["target_path"] == f"/v/{vid}/suggestions"
               for n in desktop_notices.pending())


def test_proposals_never_offer_publishing_or_sends(monkeypatch):
    from app.suggestions import proposals

    assert "site.publish" not in proposals.EXECUTABLE_KINDS and "social_post" not in proposals.EXECUTABLE_KINDS
    monkeypatch.setattr("app.ventures.next_step.next_step",
                        lambda _v: {"key": "site.publish", "title": "Publish", "chat_action": "site.publish"})
    assert proposals.propose_next_actions("ven_x") == []


def test_decide_api_save_dismiss_and_conflicts(env, monkeypatch):
    client, vid = env
    from app.suggestions import service

    _proposals(monkeypatch, ["research.start", "waitlist.enable"])
    service.generate(vid)
    listed = client.get("/suggestions").json()
    assert listed["new"] == 2 and listed["saved"] == 0
    a, b = (i["id"] for i in listed["items"])
    r = client.post(f"/suggestions/{a}/decision", json={"decision": "save"})
    assert r.status_code == 200 and r.json()["status"] == "saved"
    r = client.post(f"/suggestions/{b}/decision", json={"decision": "dismiss", "feedback": "too_expensive"})
    assert r.json()["status"] == "dismissed" and r.json()["result_ref"] == "dismissed:too_expensive"
    assert client.post(f"/suggestions/{b}/decision", json={"decision": "dismiss"}).status_code == 409
    assert client.post("/suggestions/sug_missing/decision", json={"decision": "save"}).status_code == 404
    after = client.get("/suggestions").json()
    assert (after["new"], after["saved"]) == (0, 1)
    assert [i["id"] for i in client.get("/suggestions?status=history").json()["items"]] == [b]


def test_accept_runs_the_guarded_action_and_tells_chat(env, monkeypatch):
    client, vid = env
    from app.chat import execute
    from app.suggestions import service

    calls = []

    async def fake(venture_id, name, *, payload=None, actor_subject=None):
        calls.append(name)
        return {"kind": "text", "body_md": f"{name} finished", "actions": []}

    monkeypatch.setattr(execute, "execute_action", fake)
    _proposals(monkeypatch, ["research.start"])
    service.generate(vid)
    sid = _rows(vid)[0].id
    r = client.post(f"/suggestions/{sid}/decision", json={"decision": "accept"})
    assert r.status_code == 200 and r.json()["status"] == "accepted" and r.json()["result_state"] == "needs_you", r.text
    assert calls == ["research.start"]
    assert client.post(f"/suggestions/{sid}/decision", json={"decision": "accept"}).status_code == 409  # never runs twice
    chat = client.get(f"/ventures/{vid}/chat").json()
    text = str(chat)
    assert "On it:" in text and "research.start finished" in text


def test_accept_does_not_confirm_hidden_followups(env, monkeypatch):
    from app.autopilot import receipts
    from app.chat import execute
    from app.db.models import VentureDocument
    from app.state import db
    from app.suggestions import service
    from core.clock import utcnow

    client, vid = env
    calls = []
    async def producer(venture_id, name, **kwargs):
        calls.append(name)
        with db()() as session:
            session.add(VentureDocument(id="one_receipt", venture_id=vid, kind="draft", title="Draft",
                body_md="Saved", created_at=utcnow(), updated_at=utcnow()))
            session.commit()
        receipts.saved_document("one_receipt", "draft", "Saved")
        return {"kind": "text", "body_md": "Saved", "actions": [{"action": "plan.resources_none", "payload": {}}]}
    monkeypatch.setattr(execute, "execute_action", producer)
    _proposals(monkeypatch, ["research.start"])
    sid = service.generate(vid)[0]
    result = client.post(f"/suggestions/{sid}/decision", json={"decision": "accept"})
    assert result.json()["status"] == "done" and calls == ["research.start"]

@pytest.mark.parametrize("god_enabled,second_credits", [(False, 12), (True, 12), (True, 8)])
def test_saved_card_credit_maximum_rejects_current_cost_above_approval(env, monkeypatch, god_enabled, second_credits):
    from app.autopilot import guard
    from app.chat import execute
    from app.suggestions import service
    from app.credits.units import MICRO_PER_CREDIT

    client, vid = env
    monkeypatch.setattr(guard, "check", lambda *args, **kwargs: guard.Verdict(True, cost_micro=21 * MICRO_PER_CREDIT))
    async def forbidden(*args, **kwargs):
        pytest.fail("An over-budget card must not execute")
    monkeypatch.setattr(execute, "execute_action", forbidden)
    _proposals(monkeypatch, ["research.start"])
    sid = service.generate(vid)[0]
    result = client.post(f"/suggestions/{sid}/decision", json={"decision": "accept"})
    assert result.json()["status"] == "accepted" and result.json()["result_state"] == "needs_you"
    assert "credit quote" in result.json()["result_message"]

def test_accept_failure_posts_recovery_and_marks_failed(env, monkeypatch):
    client, vid = env
    from app.chat import execute
    from app.suggestions import service

    async def boom(*a, **k):
        raise RuntimeError("provider down")

    monkeypatch.setattr(execute, "execute_action", boom)
    _proposals(monkeypatch, ["research.start"])
    service.generate(vid)
    sid = _rows(vid)[0].id
    r = client.post(f"/suggestions/{sid}/decision", json={"decision": "accept"})
    assert r.status_code == 200 and r.json()["status"] == "accepted" and r.json()["result_state"] == "needs_you"
    assert "Needs you" in str(client.get(f"/ventures/{vid}/chat").json())


def test_accept_refuses_an_action_outside_the_allow_list(env, monkeypatch):
    client, vid = env
    from app.chat import execute
    from app.suggestions import service

    ran = []

    async def fake(*a, **k):
        ran.append(a)
        return {"kind": "text", "body_md": "x", "actions": []}

    monkeypatch.setattr(execute, "execute_action", fake)
    _proposals(monkeypatch, ["research.start"])
    service.generate(vid)
    from app.db.supymem_models import Suggestion
    from app.state import db

    sid = _rows(vid)[0].id
    with db()() as s:  # a tampered row can never reach execute_action
        s.get(Suggestion, sid).action_kind = "pause"
        s.commit()
    r = client.post(f"/suggestions/{sid}/decision", json={"decision": "accept"})
    assert r.status_code == 200 and ran == []


def test_god_mode_proposals_become_cards_and_human_steps_are_skipped(env, monkeypatch):
    from types import SimpleNamespace

    from app.autopilot.actions import ProposedAction
    from app.suggestions import proposals

    def mk(**k):
        return ProposedAction(**{"id": "mv_1", "title": "Start research", "why": "w", "will_do": "I will research",
                                 "estimated_credits": 12.5, "kind": "research.start", **k})

    fake = SimpleNamespace(propose_next_actions=lambda v, n: [mk(), mk(id="mv_2", human_only=True, kind="human"),
                                                              mk(id="mv_3", kind="site.publish")],
                           execute_proposed_action=None)
    monkeypatch.setattr(proposals, "god_actions", lambda: fake)
    cards = proposals.propose_next_actions(env[1])
    assert [(c["key"], c["est_credits"], c["title"]) for c in cards] == [("mv_1", 13, "Start a research job for the confirmed idea")]


def test_accept_uses_god_modes_executor_when_deployed(env, monkeypatch):
    client, vid = env
    from types import SimpleNamespace

    from app.suggestions import proposals, service
    from app.credits.units import MICRO_PER_CREDIT

    seen = []

    async def run(venture_id, action, actor, *, approved_ceiling_micro=None):
        seen.append((venture_id, action["kind"], action["title"]))
        assert approved_ceiling_micro == 20 * MICRO_PER_CREDIT
        return {"ok": True, "reason": "", "done": [action["kind"]], "completion_verified": True}

    monkeypatch.setattr(proposals, "god_actions", lambda: SimpleNamespace(execute_proposed_action=run))
    _proposals(monkeypatch, ["research.start"])
    service.generate(vid)
    sid = _rows(vid)[0].id
    r = client.post(f"/suggestions/{sid}/decision", json={"decision": "accept"})
    assert r.json()["status"] == "done" and seen == [(vid, "research.start", "Start a research job for the confirmed idea")]

    async def refuse(venture_id, action, actor, **kwargs):
        return {"ok": False, "reason": "credentials_never_entered", "done": []}

    monkeypatch.setattr(proposals, "god_actions", lambda: SimpleNamespace(execute_proposed_action=refuse))
    _proposals(monkeypatch, ["waitlist.enable"])
    service.generate(vid)
    sid2 = [r.id for r in _rows(vid) if r.action_kind == "waitlist.enable"][0]
    declined = client.post(f"/suggestions/{sid2}/decision", json={"decision": "accept"}).json()
    assert declined["status"] == "accepted" and declined["result_state"] == "needs_you"


@pytest.mark.parametrize("result,state", [
    ({"ok": True, "reason": "queued", "done": []}, "needs_you"),
    ({"ok": True, "reason": "in_progress", "done": ["research.start"], "completion_verified": True}, "needs_you"),
    ({"ok": True, "reason": "", "done": ["research.start"], "completion_verified": True}, "done"),
    ({"ok": True, "reason": "", "done": ["research.start"]}, "needs_you"),
    ({"ok": True, "reason": "", "done": [], "completion_verified": True}, "needs_you"),
    ({"ok": False, "reason": "needs_you", "done": []}, "needs_you"),
    ({"ok": False, "reason": "held", "done": []}, "needs_you"),
])
def test_accept_history_requires_source_qualified_completion(env, monkeypatch, result, state):
    from types import SimpleNamespace

    from app.suggestions import proposals, service

    client, vid = env
    calls = []

    async def run(*args, **kwargs):
        calls.append(args)
        return result

    monkeypatch.setattr(proposals, "god_actions", lambda: SimpleNamespace(execute_proposed_action=run))
    _proposals(monkeypatch, ["research.start"])
    service.generate(vid)
    sid = _rows(vid)[0].id
    response = client.post(f"/suggestions/{sid}/decision", json={"decision": "accept"})
    assert response.status_code == 200
    assert response.json()["status"] == ("done" if state == "done" else "accepted")
    assert response.json()["result_state"] == state
    history = client.get("/suggestions?status=history").json()["items"]
    saved = next(row for row in history if row["id"] == sid)
    assert saved["status"] == response.json()["status"] and saved["result_state"] == state and saved["result_href"]
    assert client.post(f"/suggestions/{sid}/decision", json={"decision": "accept"}).status_code == 409
    assert len(calls) == 1
    if state != "done":
        assert "Done: Do plan.generate" not in str(client.get(f"/ventures/{vid}/chat").json())


def test_mac_navigation_is_not_a_one_tap_suggestion(env, monkeypatch):
    from app.suggestions import service
    _proposals(monkeypatch, ["mac.task.prepare"])
    assert service.generate(env[1]) == []

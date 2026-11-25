"""God mode (beta): guard floors, the autopilot tick, the kill switch, the timeline and the chat intents."""

from __future__ import annotations

import asyncio

import pytest
from tests.company_os_harness import company_os_client as TestClient, configure_local_test_model, load_company_os_app


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/god.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_MEMORY_PATH", str(tmp_path / "mem.sqlite3"))
    monkeypatch.setenv("COMPANY_OS_BRAIN_GRAPH_PATH", str(tmp_path / "graph.sqlite3"))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    main = load_company_os_app()
    from app import state as state_mod
    from app.memory import reset_memory_for_tests

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    reset_memory_for_tests(tmp_path / "mem.sqlite3")
    configure_local_test_model()
    from app.core.local_mode import is_local

    if is_local():
        from app.core.local_secrets import update_settings

        update_settings("capabilities", {"background_work": True}, {})
        update_settings("spend", {"monthly_spend_cap": "5"}, {})
    with TestClient(main.create_app()) as client:
        created = client.post("/ventures", json={"raw_idea": "a marketplace for restoring vintage synthesizers"})
        assert created.status_code == 201, created.text
        yield client, created.json()["id"]


def _on(client, vid, **extra):
    r = client.put(f"/ventures/{vid}/autopilot", json={"god_mode": True, "acknowledged_beta": True, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def _fake_chat(monkeypatch, steps, replies=None):
    """next_step walks `steps`; execute_action records (name, payload) and answers from `replies`."""
    from app.chat import execute

    calls, queue = [], list(steps)
    monkeypatch.setattr("app.ventures.next_step.next_step",
                        lambda _v: queue.pop(0) if queue else {"key": "idle", "title": "Idle", "chat_action": None})

    async def fake(venture_id, name, *, payload=None, actor_subject=None):
        calls.append((name, payload or {}))
        reply = (replies or {}).get(name, {"kind": "text", "body_md": f"{name} done", "actions": []})
        out = reply(calls) if callable(reply) else reply
        # Test executor owns an actual persisted artifact. A confirmation quote
        # creates no completion evidence, exactly like the production contract.
        if out.get("kind") == "text":
            from app.autopilot.receipts import saved_document
            from app.db.models import VentureDocument
            from app.state import db
            from core.clock import utcnow
            from core.ids import new_id

            identifier = new_id("test_effect")
            body = f"Owned test result for {name}"
            with db()() as session:
                session.add(VentureDocument(id=identifier, venture_id=venture_id, kind="test_effect",
                    title=name, body_md=body, created_at=utcnow(), updated_at=utcnow()))
                session.commit()
            saved_document(identifier, "test_effect", body)
        return out

    monkeypatch.setattr(execute, "execute_action", fake)
    return calls


def _step(action, payload=None, title="Do the thing"):
    return {"key": action, "title": title, "detail": "", "chat_action": action, "payload": payload}


# ---- guard floors ----

def test_guard_floors(env):
    client, vid = env
    from app.autopilot import guard
    from app.credits.units import MICRO_PER_CREDIT

    assert guard.check(vid, "research.start").reason == "god_mode_off"
    _on(client, vid, daily_cap_credits=30)
    assert guard.check(vid, "research.start").ok
    assert guard.check(vid, "plan.generate", {"ceiling_micro": 31 * MICRO_PER_CREDIT}).ok
    assert guard.check(vid, "plan.generate", {"ceiling_micro": 30 * MICRO_PER_CREDIT}).ok
    assert guard.check(vid, "build.start", {"app_id": "a", "api_key": "sk-live"}).reason == "credentials_never_entered"
    assert guard.check(vid, "build.start", {"nested": [{"card_number": "4242"}]}).reason == "credentials_never_entered"
    assert guard.check(vid, "pause").reason == "action_not_allowed"
    assert guard.check(vid, "build.cancel").reason == "action_not_allowed"
    assert guard.check(vid, "social_post").reason == "sending_not_allowed"  # no external sends by default


def test_sending_needs_its_own_toggle_and_a_daily_max(env):
    client, vid = env
    from app.autopilot import guard, service

    _on(client, vid)
    out = client.put(f"/ventures/{vid}/autopilot", json={"allow_send": True, "send_max_per_day": 1}).json()
    assert out["allow_send"] and out["send_max_per_day"] == 1
    assert guard.check(vid, "social_post").ok
    service.record(vid, "social_post", "sent one", credits_micro=0)
    assert guard.check(vid, "social_post").reason == "send_limit_reached"  # consent given, today's max used up: not a consent question
    off = client.put(f"/ventures/{vid}/autopilot", json={"allow_send": False}).json()
    assert off["send_max_per_day"] == 0 and guard.check(vid, "social_post").reason == "sending_not_allowed"


def test_mac_manifest_only_read_only_inside_floors(env):
    client, vid = env
    from app.autopilot import guard

    _on(client, vid, daily_cap_credits=5)
    read = {"actions": ["browser.read"], "credit_ceiling_micro": 1000}
    assert guard.check_mac_manifest(vid, read).ok
    assert guard.check_mac_manifest(vid, {**read, "actions": ["browser.read", "browser.interact"]}).reason == "mac_scope_not_read_only"
    assert guard.check_mac_manifest(vid, {**read, "credit_ceiling_micro": 10**9}).ok
    assert guard.check_mac_manifest(vid, {**read, "fill_values": {"password": "x"}}).reason == "credentials_never_entered"


# ---- settings API and timeline ----

def test_enabling_needs_the_warning_confirm_and_is_logged(env):
    client, vid = env
    r = client.put(f"/ventures/{vid}/autopilot", json={"god_mode": True})
    assert r.status_code == 409 and r.json()["detail"]["error"]["code"] == "god_mode_warning_unconfirmed"
    got = client.get(f"/ventures/{vid}/autopilot").json()
    assert got["god_mode"] is False and got["daily_cap_credits"] is None and got["allow_send"] is False
    assert got["lifetime_cap_credits"] is None
    assert "beta" in got["warning"].lower() and got["floors"]
    assert "review recorded results" in got["warning"]
    assert "Pausing blocks new work" in got["warning"] and "running work may finish" in got["warning"]
    floor = next(f for f in got["floors"] if "Pausing a venture" in f)
    assert "blocks new work" in floor and "new autonomous steps" in floor
    assert "Work already running may finish" in floor and "Unconfirmed usage remains unresolved" in floor
    assert "immediately" not in floor and "stops everything" not in floor
    on = _on(client, vid)
    assert on["god_mode"] and on["timeline"][0]["action"] == "god_mode.on"
    off = client.put(f"/ventures/{vid}/autopilot", json={"god_mode": False}).json()
    assert not off["god_mode"] and off["timeline"][0]["action"] == "god_mode.off"
    assert "New autonomous steps are disabled" in off["timeline"][0]["why"]
    assert "Work already running may finish" in off["timeline"][0]["why"]
    assert client.put(f"/ventures/{vid}/autopilot", json={"daily_cap_credits": -1}).status_code == 422


# ---- the tick ----

@pytest.mark.parametrize("waitlist_enabled", [False, True])
@pytest.mark.parametrize("violations", [[], ["Unverified public claim"]])
def test_god_tick_prepares_waitlist_then_publishes_inside_its_existing_guard(env, monkeypatch, waitlist_enabled, violations):
    client, vid = env
    from app.autopilot import service

    _on(client, vid)
    monkeypatch.setattr("app.ventures.state_brief._website_section", lambda *_: {"waitlist_enabled": waitlist_enabled})
    monkeypatch.setattr("app.autopilot.claims.site_violations", lambda _: violations)
    def confirmation(action):
        return {"kind": "confirmation", "actions": [{"action": action, "payload": {}}]}
    calls = _fake_chat(monkeypatch, [_step("site.publish", title="Publish your landing page")], {
        "waitlist.enable": confirmation("waitlist.test"), "waitlist.test": confirmation("site.publish")})
    result = asyncio.run(service.tick(vid))
    expected = [] if waitlist_enabled else ["waitlist.enable", "waitlist.test"]
    assert [name for name, _ in calls] == expected + ([] if violations else ["site.publish"])
    if violations:
        assert result["status"].startswith("public_claims_unverified")
    else:
        assert result["done"] == ["site.publish"]
    assert service._forbidden.get() == frozenset() and service._effects.get() is None


@pytest.mark.parametrize("suggestion_origin", [False, True])
def test_pmf_children_use_the_origin_permission_model(env, monkeypatch, suggestion_origin):
    client, vid = env
    from app.autopilot import actions, pmf, service

    _on(client, vid)
    monkeypatch.setattr("app.autopilot.claims.site_violations", lambda _: [])
    calls = _fake_chat(monkeypatch, [_step("pmf.iterate")])
    admitted = []
    async def advance(_vid, run_action, **kwargs):
        admitted.append(await run_action("site.publish", {}))
        return []
    monkeypatch.setattr(pmf, "advance", advance)
    monkeypatch.setattr(pmf, "journal", lambda _: [])
    if suggestion_origin:
        asyncio.run(actions.execute_proposed_action(vid, {"kind": "pmf.iterate", "title": "PMF", "payload": {}}, service._owner(vid)))
    else:
        asyncio.run(service.tick(vid))
    assert admitted == [not suggestion_origin]
    assert [name for name, _ in calls] == ([] if suggestion_origin else ["site.publish"])
    assert service._forbidden.get() == frozenset() and service._effects.get() is None


def test_tick_runs_next_step_through_the_registry_and_confirms_its_card(env, monkeypatch):
    client, vid = env
    from app.autopilot import service
    from app.chat.actions import REGISTRY
    from app.credits.units import MICRO_PER_CREDIT

    _on(client, vid)
    card = {"label": "Generate plan", "name": "chat.confirm", "action": "plan.generate",
            "confirmation": {"action": "plan.generate", "resulting_state": {"ceiling_micro": 30 * MICRO_PER_CREDIT}}}
    calls = _fake_chat(monkeypatch, [_step("plan.generate", title="Generate your execution plan")],
                       {"plan.generate": lambda c: {"kind": "confirmation", "body_md": "Quote ready", "actions": [card]}
                        if len(c) == 1 else {"kind": "text", "body_md": "Plan generated", "actions": []}})
    out = asyncio.run(service.tick(vid))
    assert [c[0] for c in calls] == ["plan.generate", "plan.generate"] and all(c[0] in REGISTRY for c in calls)
    assert calls[1][1] == {"ceiling_micro": 30 * MICRO_PER_CREDIT}
    assert out["done"] == ["plan.generate"]  # A quote is preparation, not completion.
    events = [e for e in service.timeline(vid) if e["action"] == "plan.generate"]
    results = [e for e in events if e["outcome"] != "attempt"]
    attempts = [e for e in events if e["outcome"] == "attempt"]
    assert len(results) == 2 and len(attempts) == 2
    assert [e["outcome"] for e in results] == ["completed", "prepared"]
    assert "Next step: Generate your execution plan" in results[0]["why"]
    assert sum(e["credits"] for e in events) == 30
    assert client.get(f"/ventures/{vid}/autopilot").json()["spent_today_credits"] == 30
    from app.chat.store import list_messages

    assert any("Autopilot update" in m["body_md"] for m in list_messages(vid, limit=20))


@pytest.mark.hosted_only
@pytest.mark.parametrize("available_credits", [300, 299])
def test_tick_above_old_cap_stops_only_for_insufficient_credits(env, monkeypatch, available_credits):
    client, vid = env
    from app.autopilot import guard, lifecycle, service
    from app.chat import execute
    from app.credits.accounts import ensure_account, get_account_by_id
    from app.credits.errors import InsufficientCredits
    from app.credits.gate import charge_sync
    from app.db.models import CreditAccount
    from app.state import db
    from app.credits.units import MICRO_PER_CREDIT

    _on(client, vid, daily_cap_credits=5)
    account = ensure_account(vid)
    with db()() as session:
        session.get(CreditAccount, account.id).granted_micro = available_credits * MICRO_PER_CREDIT
        session.commit()
    monkeypatch.setattr(lifecycle, "branches", lambda _v: [])
    monkeypatch.setattr("app.autopilot.outreach.housekeeping", _noop_housekeeping)
    calls = _fake_chat(monkeypatch, [_step("plan.generate", {"ceiling_micro": 300 * MICRO_PER_CREDIT})])
    persist = execute.execute_action

    async def metered(venture_id, name, *, payload=None, actor_subject=None):
        try:
            with charge_sync(account_id=account.id, purpose="llm", estimate_micro=payload["ceiling_micro"]) as meter:
                reply = await persist(venture_id, name, payload=payload, actor_subject=actor_subject)
                meter.record_micro(payload["ceiling_micro"])
                return reply
        except InsufficientCredits:
            return {"kind": "needs_you", "body_md": "Insufficient credits. Add credits to continue.", "actions": []}

    monkeypatch.setattr(execute, "execute_action", metered)
    result = asyncio.run(service.tick(vid))
    assert result["status"] == ("waiting" if available_credits == 300 else "needs_you")
    assert len(calls) == (1 if available_credits == 300 else 0)
    assert result["done"] == (["plan.generate"] if available_credits == 300 else [])
    after = get_account_by_id(account.id)
    assert after.spent_micro == (300 * MICRO_PER_CREDIT if available_credits == 300 else 0)
    assert after.reserved_micro == 0
    assert guard.get_settings(vid)["daily_cap_credits"] is None
    assert not any(e["action"] == "cap" for e in service.timeline(vid))


async def _noop_housekeeping(_venture_id):
    return None


def test_tick_leaves_disallowed_and_waiting_steps_alone(env, monkeypatch):
    client, vid = env
    from app.autopilot import service

    _on(client, vid)
    calls = _fake_chat(monkeypatch, [_step("social_post"), {"key": "idea.clarify", "title": "Answer", "chat_action": None}])
    assert asyncio.run(service.tick(vid))["status"] == "sending_not_allowed"
    assert calls == []
    assert asyncio.run(service.tick(vid))["status"] == "waiting"


def test_turning_off_or_pausing_stops_the_loop_immediately(env, monkeypatch):
    client, vid = env
    from app.autopilot import service
    from app.ventures.lifecycle import pause

    _on(client, vid)
    calls = _fake_chat(monkeypatch, [_step("research.start"), _step("work.start", {"task_id": "t"})])
    from app.chat import execute

    inner = execute.execute_action

    async def switch_off_midway(venture_id, name, **kw):
        out = await inner(venture_id, name, **kw)
        client.put(f"/ventures/{venture_id}/autopilot", json={"god_mode": False})
        return out

    monkeypatch.setattr(execute, "execute_action", switch_off_midway)
    out = asyncio.run(service.tick(vid))
    assert [c[0] for c in calls] == ["research.start"] and out["status"] == "god_mode_off"
    assert asyncio.run(service.tick(vid))["status"] == "god_mode_off"
    _on(client, vid)
    pause(vid, reason="test")
    calls.clear()
    assert asyncio.run(service.tick(vid))["status"] == "paused" and calls == []


def test_tick_all_only_visits_god_mode_ventures(env, monkeypatch):
    client, vid = env
    from app.autopilot import service

    seen = []

    async def fake_tick(v):
        seen.append(v)

    monkeypatch.setattr(service, "tick", fake_tick)
    assert asyncio.run(service.tick_all()) == 0
    _on(client, vid)
    assert asyncio.run(service.tick_all()) == 1 and seen == [vid]


# ---- chat ----

def test_chat_intents_and_confirm_card(env):
    client, vid = env
    from app.chat.intent import classify_intent
    from app.core.local_mode import is_local

    assert classify_intent("turn on god mode") == "god_on" and classify_intent("Turn off God mode.") == "god_off"
    assert classify_intent("enable god mode") == "god_on" and classify_intent("disable god mode") == "god_off"
    card = client.post(f"/ventures/{vid}/chat", json={"message": "turn on god mode"}).json()
    if is_local():
        assert card["kind"] == "text" and "Your requested background work is on" in card["body_md"]
        assert "saved spending limit and send permissions still apply" in card["body_md"]
        assert "Sending stays off until you allow it" in card["body_md"]
        assert card["actions"][0]["href"] == "/settings/local#spend-title"
    else:
        assert card["kind"] == "confirmation" and "beta" in card["body_md"].lower()
        assert "review recorded results" in card["body_md"]
        assert "Pausing blocks new work" in card["body_md"] and "running work may finish" in card["body_md"]
        assert card["actions"][0]["confirmation"]["action"] == "autopilot.god_on"
        assert client.get(f"/ventures/{vid}/autopilot").json()["god_mode"] is False
        ok = client.post(f"/ventures/{vid}/chat/confirm", json={"action": "autopilot.god_on"})
        assert ok.status_code == 200, ok.text
    assert client.get(f"/ventures/{vid}/autopilot").json()["god_mode"] is True
    client.post(f"/ventures/{vid}/chat", json={"message": "turn off god mode"})
    assert client.get(f"/ventures/{vid}/autopilot").json()["god_mode"] is False


def test_a_step_the_journey_keeps_offering_is_not_repeated(env, monkeypatch):
    client, vid = env
    from app.autopilot import service

    _on(client, vid)
    calls = _fake_chat(monkeypatch, [_step("gtm.diagnosis")] * 3)
    assert asyncio.run(service.tick(vid))["done"] == ["gtm.diagnosis"]
    assert asyncio.run(service.tick(vid))["status"] == "repeat_limit"
    assert [c[0] for c in calls] == ["gtm.diagnosis"]


@pytest.mark.parametrize("job_state,expected", [("scheduled", "queued"), ("leased", "working"), ("failed_dead", "failed")])
def test_enabled_godmode_reports_durable_job_state_not_proposed_next_move(env, job_state, expected):
    from app.autopilot import service
    from app.db.models import Job, Venture
    from sqlalchemy import select
    from app.state import db
    from core.clock import utcnow

    client, vid = env
    _on(client, vid)
    with db()() as session:
        venture = session.get(Venture, vid)
        # Fixture onboarding queues validation; settle only these owned jobs so
        # an unrelated active fixture step cannot supersede this exact receipt.
        for existing in session.scalars(select(Job).where(Job.venture_id == vid)):
            existing.status = "cancelled"
        session.add(Job(id="godmode_observed_job", kind="research.deep", venture_id=vid,
                        workspace_id=venture.workspace_id, payload_json="{}", status=job_state,
                        created_at=utcnow(), last_error="job_actor_missing" if job_state == "failed_dead" else None))
        session.commit()
    observed = service.status(vid)
    assert observed["state"] == expected
    assert not observed["working_on"].startswith("Next:")
    assert "Working on everything else" not in observed["working_on"]
    response = client.get(f"/ventures/{vid}/autopilot").json()
    assert response["status"]["state"] == expected
    assert response["brief"]["now"] == response["status"]["working_on"]
    saved = client.put(f"/ventures/{vid}/autopilot", json={"daily_cap_credits": 200}).json()
    assert saved["brief"]["now"] == saved["status"]["working_on"]
    capped = client.put(f"/ventures/{vid}/autopilot", json={"daily_cap_credits": 0}).json()
    assert capped["status"]["limit_reached"] is False
    assert capped["status"]["state"] == expected
    assert capped["brief"]["now"] == capped["status"]["working_on"]
    assert "credit cap" not in capped["brief"]["now_detail"]


def test_enabled_godmode_without_execution_does_not_claim_work_is_running(env):
    from app.autopilot import service

    client, vid = env
    _on(client, vid)
    observed = service.status(vid)
    assert observed["state"] != "working"
    response = client.get(f"/ventures/{vid}/autopilot").json()
    assert response["brief"]["now"] == response["status"]["working_on"]
    assert response["brief"]["now"] != "Working on the parts that do not need you."

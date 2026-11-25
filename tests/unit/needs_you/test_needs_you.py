"""needs_you: the decision table, dedupe/batching, delivery, reminders, resume callbacks and wiring points."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from tests.company_os_harness import company_os_test_client as TestClient

from tests.company_os_harness import load_company_os_app

load_company_os_app()  # puts services/company_os on sys.path
from app.needs_you.policy import Blocker, Plan, must_ask_human, try_autonomous_first  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ny.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_MEMORY_PATH", str(tmp_path / "mem.sqlite3"))
    monkeypatch.setenv("COMPANY_OS_BRAIN_GRAPH_PATH", str(tmp_path / "graph.sqlite3"))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    monkeypatch.setenv("NEEDS_YOU_QUIET_HOURS", "0-0")
    monkeypatch.setenv("NEEDS_YOU_REMIND_MINUTES", "240")
    monkeypatch.setenv("NEEDS_YOU_EMAIL_REMIND_MINUTES", "240")
    main = load_company_os_app()
    from app import state as state_mod
    from app.memory import reset_memory_for_tests

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    reset_memory_for_tests(tmp_path / "mem.sqlite3")
    with TestClient(main.create_app()) as client:
        created = client.post("/ventures", json={"raw_idea": "a marketplace for restoring vintage synthesizers"})
        assert created.status_code == 201, created.text
        yield client, created.json()["id"]


def _ask(vid, ref="x:1", code="captcha", severity="blocking", **kw):
    from app.needs_you.service import request_human

    return request_human(vid, reason_code=code, title="T", what_we_need="do it", how_to_resume="then tap",
                         blocking_ref=ref, severity=severity, **kw)


def _notices():
    from app import desktop_notices

    return [n for n in desktop_notices.pending() if n["kind"] == "needs_you"]


def _notice_history():
    from sqlalchemy import select
    from app.db.supymem_models import Notice
    from app.state import db

    with db()() as session:
        return [{"id": row.id, "title": row.title, "read_at": row.read_at}
                for row in session.scalars(select(Notice).where(Notice.kind == "needs_you")
                    .order_by(Notice.created_at, Notice.id))]


# ---- decision table ----

@pytest.mark.parametrize("code", ["captcha", "login", "2fa", "payment", "os_permission", "screen_recording",
                                  "consent_send", "sending_not_allowed", "consent_delete", "insufficient_credits",
                                  "credit_cap", "real_world_action", "ambiguous_high_stakes", "verification"])
def test_human_only_reasons_always_ask(code):
    assert must_ask_human(Blocker(code, retries_left=3, other_work_available=True))


@pytest.mark.parametrize("code", ["timeout", "provider_error", "handler_failed", "rate_limited", ""])
def test_everything_else_stays_autonomous_until_exhausted(code):
    assert not must_ask_human(Blocker(code))
    assert must_ask_human(Blocker(code, exhausted=True))


def test_autonomous_order_retry_then_alternative_then_other_branch_then_none():
    assert try_autonomous_first(Blocker("timeout", retries_left=2, step="build")).action == "retry"
    assert try_autonomous_first(Blocker("timeout", step="build")) == Plan("alternative", "site_template")
    assert try_autonomous_first(Blocker("timeout", step="research.deep", other_work_available=True)).action == "continue_other"
    assert try_autonomous_first(Blocker("timeout", step="research.deep")).action == "none"


def test_escalate_never_pings_for_an_autonomous_blocker(env):
    from app.needs_you.policy import escalate
    from app.needs_you.service import list_open

    _, vid = env
    assert escalate(vid, Blocker("timeout", "job:1", step="build")).action == "alternative"
    assert list_open(vid) == []
    assert isinstance(escalate(vid, Blocker("timeout", "job:2", step="research.deep", exhausted=True)), str)
    assert len(list_open(vid)) == 1


# ---- request_human: dedupe, delivery, batching ----

def test_dedupe_card_and_notice(env):
    from app.chat.store import list_messages
    from app.needs_you.service import list_open

    _, vid = env
    first = _ask(vid)
    assert first and _ask(vid) == first
    assert len(list_open(vid)) == 1
    cards = [m for m in list_messages(vid, limit=20) if "Supymem needs you" in m["body_md"]]
    assert len(cards) == 1
    assert any(a.get("action") == "needs_you.resolve" for a in cards[0]["actions"])
    assert len(_notices()) == 1


def test_blocker_hides_internal_background_status_and_legacy_parallel_claim(env, monkeypatch):
    from app.chat.store import list_messages
    from app.needs_you.service import request_human, remind_due
    from app.ventures import run_status

    _, vid = env
    monkeypatch.setattr(run_status, "snapshot", lambda *_: {"activity": "Idle. No task is currently running."})
    request_human(vid, reason_code="experiment", title="Open the experiment",
                  what_we_need="Open the saved experiment. I am carrying on with everything else.",
                  how_to_resume="Press Continue after opening it.", blocking_ref="experiment:exp_one")
    remind_due(now=_future(minutes=6))
    cards = [m for m in list_messages(vid, limit=30) if "Supymem needs you" in m["body_md"]]
    assert len(cards) == 1
    assert "Open the saved experiment" in cards[0]["body_md"]
    assert "Saved background status" not in cards[0]["body_md"]
    assert "carrying on" not in cards[0]["body_md"]
    assert all("carrying on" not in notice["body"] for notice in _notices())


def test_distinct_experiment_ids_remain_distinct_even_with_same_title(env):
    from app.needs_you.service import list_open

    _, vid = env
    first = _ask(vid, ref="experiment:exp_one", code="experiment")
    second = _ask(vid, ref="experiment:exp_two", code="experiment")
    assert first != second
    assert _ask(vid, ref="experiment:exp_one", code="experiment") == first
    assert len(list_open(vid)) == 2


def test_second_need_is_batched_into_one_notice(env):
    from app.needs_you.service import remind_due

    _, vid = env
    _ask(vid, ref="a:1")
    _ask(vid, ref="a:2")
    assert len(_notices()) == 1  # the second rides the next batched notice, no second ping
    assert remind_due(now=_future(minutes=2)) == {"expired": 0, "digests": 0, "reminders": 0}
    assert remind_due(now=_future(minutes=6))["reminders"] == 0
    assert remind_due(now=_future(minutes=11))["digests"] == 1
    notices = _notice_history()
    assert len(notices) == 2 and "2 blockers" not in notices[-1]["title"]
    current = _notices()
    assert len(current) == 1


def _future(**kw):
    from core.clock import utcnow

    return utcnow() + timedelta(**kw)


def test_quiet_hours_defer_non_urgent_but_not_blocking(env, monkeypatch):
    monkeypatch.setenv("NEEDS_YOU_QUIET_HOURS", "0-24")  # whole day is quiet
    from app.needs_you import service

    _, vid = env
    _ask(vid, ref="q:1", code="consent_send", severity="normal")
    assert _notices() == []
    _ask(vid, ref="q:2", severity="blocking")
    assert len(_notices()) == 1
    assert service.remind_due()["digests"] == 0


@pytest.mark.parametrize("idea,zone", [("Reconcile invoices for warehouse teams", "America/Los_Angeles"),
                                     ("Plan weekend walks for families", "Asia/Kolkata")])
def test_reminders_wait_hours_respect_founder_nights_and_leave_card_unchanged(env, monkeypatch, idea, zone):
    from app.chat.store import list_messages
    from app.db.models import Person, Venture, Workspace
    from app.needs_you import service
    from app.state import db

    _, vid = env
    base = datetime(2026, 10, 1, 16, tzinfo=ZoneInfo(zone)).astimezone(timezone.utc)
    monkeypatch.setattr(service, "utcnow", lambda: base)
    monkeypatch.setenv("NEEDS_YOU_QUIET_HOURS", "22-8")
    with db()() as session:
        venture = session.get(Venture, vid)
        venture.raw_idea = idea
        owner = session.get(Person, session.get(Workspace, venture.workspace_id).owner_id)
        owner.timezone = zone
        session.commit()
    rid = _ask(vid)
    before = [m for m in list_messages(vid, limit=30) if "Supymem needs you" in m["body_md"]]
    assert service.remind_due(now=base + timedelta(minutes=6))["reminders"] == 0
    assert service.remind_due(now=base + timedelta(hours=1, minutes=59))["reminders"] == 0
    assert service.remind_due(now=base + timedelta(hours=2))["reminders"] == 1
    assert service.remind_due(now=base + timedelta(hours=6))["reminders"] == 0
    assert service.remind_due(now=base + timedelta(hours=15))["reminders"] == 0
    assert service.remind_due(now=base + timedelta(hours=16))["reminders"] == 1
    assert service.remind_due(now=base + timedelta(hours=24))["reminders"] == 1
    n = _notices()
    history = _notice_history()
    assert len(history) == 4 and history[-1]["title"].startswith("Reminder")
    assert len(n) == 1 and all(row["read_at"] is None for row in history)
    assert "Hey, I have a blocker" in n[-1]["body"] and "1. then tap" in n[-1]["body"] and "Then tap Continue" in n[-1]["body"]
    cards = [m for m in list_messages(vid, limit=30) if "Supymem needs you" in m["body_md"]]
    assert cards == before and len(cards) == 1
    assert "Saved background status" not in cards[0]["body_md"] and "reminder" not in cards[0]["body_md"]
    service.resolve(rid, venture_id=vid)
    assert service.list_open(vid) == [] and service.remind_due(now=base + timedelta(days=2))["reminders"] == 0
    assert _notices() == [] and len(_notice_history()) == 4


def test_remind_interval_is_configurable_and_pausable(env, monkeypatch):
    from app.needs_you import service

    _, vid = env
    monkeypatch.setenv("NEEDS_YOU_REMIND_MINUTES", "30")
    _ask(vid)
    assert service.remind_due(now=_future(minutes=6))["reminders"] == 0
    assert service.remind_due(now=_future(hours=2))["reminders"] == 1
    assert service.remind_due(now=_future(hours=3))["reminders"] == 0
    service.set_reminders_paused(vid, True)
    assert service.remind_due(now=_future(hours=4))["reminders"] == 0
    service.set_reminders_paused(vid, False)
    assert service.remind_due(now=_future(hours=4))["reminders"] == 1
    monkeypatch.setenv("NEEDS_YOU_REMIND_MINUTES", "0")
    assert service.remind_due(now=_future(days=2))["reminders"] == 0


def test_reminders_email_every_four_hours_not_every_tick(env, monkeypatch):
    from app.needs_you import service

    calls = []
    monkeypatch.setattr("app.email.triggers.on_needs_you", lambda v, t, b, ref: calls.append(ref))
    _, vid = env
    _ask(vid)
    service.remind_due(now=_future(hours=2))
    service.remind_due(now=_future(hours=3))
    assert calls == []
    service.remind_due(now=_future(hours=6))
    assert len(calls) == 1 and calls[0].endswith(":remind:1")


def test_quiet_hours_silence_blocking_reminders_too(env, monkeypatch):
    from app.needs_you import service

    _, vid = env
    _ask(vid, ref="b:1", severity="blocking")
    _ask(vid, ref="n:1", code="consent_send", severity="normal")
    monkeypatch.setenv("NEEDS_YOU_QUIET_HOURS", "0-24")
    assert service.remind_due(now=_future(hours=6)) == {"expired": 0, "digests": 0, "reminders": 0}
    assert len(_notice_history()) == 1


@pytest.mark.parametrize("code", ["login", "consent_send", "credit_cap", "os_permission", "captcha", "payment",
                                  "gmail_connect", "two_factor", "totally_new_reason"])
def test_every_request_has_an_explanation_and_numbered_steps(env, code):
    from app.needs_you.service import list_open, request_human

    _, vid = env
    request_human(vid, reason_code=code, title="T", what_we_need="", how_to_resume="", blocking_ref=f"t:{code}")
    row = list_open(vid)[0]
    assert row["what_we_need"].strip() and row["how_to_resume"].startswith("1. ")
    assert "2. " in row["how_to_resume"]


def test_expiry_and_new_request_after_resolve(env):
    from app.needs_you.service import list_open, remind_due

    _, vid = env
    _ask(vid, deadline=_future(minutes=1))
    assert remind_due(now=_future(minutes=2))["expired"] == 1
    assert list_open(vid) == []
    assert _ask(vid)  # an expired ref may be asked again


# ---- resolve + resume callback + API ----

@pytest.mark.hosted_only
def test_resolve_runs_resume_callback_and_records_time(env):
    from app.needs_you import service
    from app.analytics.events import emit  # noqa: F401  (ensures prefix allowed)

    client, vid = env
    seen = []
    service.RESUME["demo"] = seen.append
    rid = _ask(vid, ref="demo:42")
    assert client.get(f"/ventures/{vid}/needs-you").json()["requests"][0]["id"] == rid
    r = client.post(f"/ventures/{vid}/needs-you/{rid}/resolve")
    assert r.status_code == 200 and r.json()["status"] == "resolved"
    assert seen and seen[0]["blocking_ref"] == "demo:42"
    assert client.post(f"/ventures/{vid}/needs-you/{rid}/resolve").status_code == 404
    from sqlalchemy import select
    from app.db.models import AnalyticsEvent
    from app.state import db

    with db()() as s:
        names = set(s.scalars(select(AnalyticsEvent.event_name)).all())
    assert {"needs_you.created", "needs_you.resolved"} <= names


def test_chat_continue_button_resolves(env):
    from app.chat.execute import execute_action
    from app.needs_you.service import list_open

    _, vid = env
    rid = _ask(vid)
    reply = asyncio.run(execute_action(vid, "needs_you.resolve", payload={"request_id": rid}, actor_subject=None))
    assert "your update is saved" in reply["body_md"] and list_open(vid) == []


# ---- wiring ----

def test_failure_with_human_only_code_asks_once_and_ordinary_failure_does_not(env):
    from app.chat import recovery
    from app.needs_you.service import list_open

    _, vid = env
    recovery.post_failure(vid, step="build", key="f1", body_md="Build failed.", kind="build_failed", code="timeout", job_id="j1")
    assert list_open(vid) == []
    recovery.post_failure(vid, step="host.deploy", key="f2", body_md="Login needed.", kind="host_failed", code="login_required", job_id="j2")
    recovery.post_failure(vid, step="build", key="f3", body_md="Gave up.", kind="build_failed", code="timeout", job_id="j3", exhausted=True)
    assert {r["blocking_ref"] for r in list_open(vid)} == {"job:j2", "job:j3"}


def test_god_mode_old_caps_do_not_ask_but_insufficient_credits_do(env, monkeypatch):
    from app.needs_you.hooks import from_godmode
    from app.needs_you.service import list_open

    client, vid = env
    client.put(f"/ventures/{vid}/autopilot", json={"god_mode": True, "acknowledged_beta": True, "daily_cap_credits": 5})
    from_godmode(vid, "cap", "daily_cap_reached")
    from_godmode(vid, "cap", "lifetime_cap_reached")
    assert list_open(vid) == []
    from_godmode(vid, "plan.generate", "insufficient_credits")
    assert [r["reason_code"] for r in list_open(vid)] == ["credit_cap"]
    listed = client.get(f"/ventures/{vid}/autopilot").json()
    assert listed["needs_you"][0]["reason_code"] == "credit_cap"
    assert listed["needs_you"][0]["how_to_resume"] == "1. Add credits."


def test_legacy_godmode_cap_requests_are_closed(env):
    from app.autopilot import service as ap
    from app.needs_you.service import list_open, request_human

    _client, vid = env
    for ref in ["godmode:cap", "godmode:bigquote:300"]:
        request_human(vid, reason_code="credit_cap", title="Legacy cap", what_we_need="Raise the cap",
                      how_to_resume="Raise the cap", blocking_ref=ref)
    assert len(list_open(vid)) == 2
    ap.clear_stale_asks(vid, "build.prepare")
    assert list_open(vid) == []


def test_device_event_and_mac_permission_stop(env, monkeypatch):
    from app.chat.mac_stop import post_mac_stopped
    from app.needs_you.service import list_open

    client, vid = env
    from app.db.models import VentureDocument
    from app.state import db
    from app.build.device_orders import KIND
    from core.clock import utcnow

    with db()() as session:
        session.add(VentureDocument(id="dwo_1", venture_id=vid, kind=KIND, title="Test order", body_md="", created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    from app.api import needs_you_api
    from app.admin.authn import CallerContext
    monkeypatch.setattr(needs_you_api, "verify_caller", lambda *args, **kwargs: CallerContext("actor", "", "user", "", "POST", 1, "n", device_id="device"))
    # This hook test does not fabricate the persisted claim schema. The migrated
    # PostgreSQL boundary test exercises actual prepare/approve/claim authority.
    monkeypatch.setattr(needs_you_api.device_invocations, "check_current", lambda *args: None)
    r = client.post(f"/ventures/{vid}/needs-you/device", json={"order_id": "dwo_1", "pause_kind": "recaptcha",
                                                              "prompt": "Solve the CAPTCHA."})
    assert r.status_code == 202
    post_mac_stopped(vid, "dwo_2", "cu_permission_missing")
    rows = {x["blocking_ref"]: x["reason_code"] for x in list_open(vid)}
    assert rows["device:dwo_1"] == "captcha"
    assert rows["device:dwo_2"] == "os_permission"


def test_hosting_login_request_resolves_when_the_url_verifies(env, monkeypatch):
    from app.needs_you.service import list_open, resolve_ref

    _, vid = env
    from app.needs_you.hooks import hosting_login

    hosting_login(vid, "vercel")
    assert [r["blocking_ref"] for r in list_open(vid)] == ["host:vercel"]
    assert resolve_ref(vid, "host:vercel") and list_open(vid) == []


@pytest.mark.hosted_only
def test_unknown_device_order_does_not_create_request(env):
    client, vid = env
    response = client.post(f"/ventures/{vid}/needs-you/device", json={"order_id": "dwo_missing", "pause_kind": "captcha"})
    assert response.status_code == 401  # unsigned callers never reach order lookup
    from app.needs_you.service import list_open
    assert list_open(vid) == []


def test_internal_errors_never_enter_email_outbox(env, monkeypatch):
    from app.db.models import ChatMessage
    from app.db.supymem_models import NeedsYouRequest
    from app.email import triggers
    from app.needs_you.service import list_open
    from app.state import db
    _, vid = env
    sent = []
    monkeypatch.setattr(triggers, "send_email", lambda *args, **kwargs: sent.append((args, kwargs)))
    triggers.on_milestone(vid, "build_blocked", "failure", "Provider failed; approval is needed")
    triggers.on_milestone(vid, "needs_you", "failure", "Everything failed", human_reason="exhausted")
    triggers.on_milestone(vid, "needs_you", "failure", "Approve to fix failure")
    assert sent == []
    triggers.on_milestone(vid, "needs_you", "captcha", "Complete verification", human_reason="captcha")
    assert sent == []  # Human wording alone is not a persisted request/card authority.
    request_id = _ask(vid, ref="legitimate-captcha", code="captcha")
    assert request_id and list_open(vid)[0]["id"] == request_id
    assert len(sent) == 1
    args, kwargs = sent[0]
    assert args[1] == "needs_you" and args[2]["reason"] == "captcha"
    assert args[2]["_needs_request_id"] == request_id
    assert kwargs["ref"] == f"{vid}:needs_you:needs_you:{request_id}"
    with db()() as session:
        request = session.get(NeedsYouRequest, request_id)
        card = session.get(ChatMessage, args[2]["_needs_card_id"])
        assert request.status == "open" and request.venture_id == vid
        assert card.venture_id == vid and card.kind == "needs_you" and card.role == "assistant"

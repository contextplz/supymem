"""Golden-path chat control: caps, build review, outreach batch, credits, status and navigation.

Each action goes through the same service function the console uses; consequential ones return a card first.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from tests.company_os_harness import company_os_test_client as TestClient, load_company_os_app

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("venture_id", ["ven_invoicing", "ven_gardening"])
def test_local_copy_usage_requests_show_local_spend_without_a_team_request(monkeypatch, venture_id):
    import sys
    from types import SimpleNamespace

    load_company_os_app()
    from app.chat import jarvis
    from app.core import local_secrets
    from app.credits import local_meter

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(local_secrets, "read_settings", lambda section: {"values": {"monthly_spend_cap": "30"}})
    monkeypatch.setattr(local_meter, "snapshot", lambda **kwargs: {"month_cost_usd_micro": 1_250_000})
    monkeypatch.setitem(sys.modules, "app.api.payments_api", SimpleNamespace(
        record_credit_request=lambda *args: pytest.fail("No hosted request may be recorded")))
    for reply in (jarvis.credits_request(venture_id), jarvis.execute_credits_request(venture_id, "owner"), jarvis.spend_today(venture_id)):
        assert "$1.25" in reply["body_md"] and "$30.00" in reply["body_md"]
        assert "Settings > Keys and connections" in reply["body_md"]
        assert reply["kind"] != "confirmation"
        assert all("confirmation" not in action for action in reply["actions"])
        assert "team" not in reply["body_md"] and "credits" not in reply["body_md"]
    def unavailable(**kwargs):
        raise OSError("private local path must not appear")
    monkeypatch.setattr(local_meter, "snapshot", unavailable)
    reply = jarvis.spend_today(venture_id)
    assert "writable" in reply["body_md"] and "restart your app" in reply["body_md"]
    assert "private local path" not in reply["body_md"]


@pytest.fixture()
def venture(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/j.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_MEMORY_PATH", str(tmp_path / "mem.sqlite3"))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    monkeypatch.delenv("SUPYMEM_OUTBOUND_EXECUTION_ENABLED", raising=False)
    main = load_company_os_app()
    from app import state

    state.reset_db_factory()
    state.MEM.clear()
    with TestClient(main.create_app(), base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        made = client.post("/ventures", json={"raw_idea": "LabHire helps QA engineers find roles", "name": "LabHire"})
        assert made.status_code == 201, made.text
        yield made.json()["id"]


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def say(vid: str, text: str) -> dict:
    from app.chat.router import handle_message
    from app.db.models import Venture
    from app.state import db
    from app.tenancy import bind
    from app.workspaces.resolve import LOCAL_PERSON_ID

    with db()() as session:
        workspace_id = session.get(Venture, vid).workspace_id
    with bind(workspace_id=workspace_id, venture_id=vid, actor=LOCAL_PERSON_ID):
        return run(handle_message(vid, text, actor_subject=LOCAL_PERSON_ID))


def card(reply: dict) -> dict:
    cards = [a["confirmation"] for a in reply["actions"] if a.get("confirmation")]
    assert cards, reply
    return cards[0]


@pytest.mark.parametrize(("text", "intent"), [
    ("set daily cap to 50", "jarvis.cap_daily"), ("Set the daily spend limit to 20 credits", "jarvis.cap_daily"),
    ("allow sending up to 10 emails a day", "jarvis.cap_send"), ("allow sending up to 5/day", "jarvis.cap_send"),
    ("show build status", "jarvis.build_status"), ("hows the build going", "jarvis.build_status"),
    ("accept build", "jarvis.build_accept"), ("accept the latest build", "jarvis.build_accept"),
    ("approve this batch", "jarvis.batch_approve"), ("show my drafts", "outreach.drafts"),
    ("draft outreach to founders", "outreach_draft"), ("what did I spend today", "jarvis.spend_today"),
    ("request credits", "jarvis.credits_request"), ("i need more credits", "jarvis.credits_request"),
    ("kitna credit bacha hai", "credits.balance"), ("aaj kitna kharch hua", "jarvis.spend_today"),
    ("new chat", "jarvis.new_chat"), ("naya chat", "jarvis.new_chat"),
    ("what's blocked", "jarvis.blocked"), ("what do you need from me?", "jarvis.blocked"),
    ("what are you doing right now", "jarvis.activity"), ("kya kar rahe ho abhi", "jarvis.activity"),
    ("gor mode on kr do", "god_on"), ("god mode band", "god_off"), ("sab kuch rok do", "pause"),
    ("landing page bana do", ("start_product_build", "pagebuilder.build")), ("research shuru karo", "research"),
])
def test_phrases_route_to_the_golden_path_intent(text, intent):
    load_company_os_app()
    from app.chat.intent import classify_intent
    from app.core.local_mode import is_local

    assert classify_intent(text) == (intent[is_local()] if isinstance(intent, tuple) else intent)


@pytest.mark.parametrize("text", [
    "how do I set the daily cap?", "should I accept the build?", "don't accept the build", "what is a batch",
    "why did you request credits", "accept the terms",
])
def test_questions_and_negations_never_hit_golden_path_actions(text):
    load_company_os_app()
    from app.chat.intent import classify_intent

    assert not classify_intent(text).startswith("jarvis.")


def test_daily_cap_commands_and_legacy_cards_cannot_limit_work(venture):
    from app.autopilot import guard
    from app.chat.execute import execute_action

    for value in [0, 50, 100000]:
        reply = say(venture, f"set daily cap to {value}")
        assert reply["kind"] == "text" and 'Say "pause work"' in reply["body_md"]
        assert not reply["actions"]
    run(execute_action(venture, "autopilot.set_cap", payload={"cap": 0}))
    assert guard.get_settings(venture)["daily_cap_credits"] is None


@pytest.mark.parametrize("idea", ["Inventory alerts for distribution teams", "Weekend activities for families"])
def test_account_controls_do_not_display_usage_or_spending_caps(venture, idea):
    from app.chat import jarvis
    from app.db.models import Venture
    from app.state import db

    with db()() as session:
        session.get(Venture, venture).raw_idea = idea
        session.commit()
    for reply in [jarvis.daily_cap(venture, "set daily cap"), jarvis.spend_today(venture)]:
        assert "credit" not in reply["body_md"].lower()
        assert "spending cap" not in reply["body_md"].lower()
        assert "usage limit" not in reply["body_md"].lower()
        assert reply["actions"] or "say" in reply["body_md"].lower()


def test_send_cap_request_uses_the_settings_service_without_a_card(venture):
    from app.autopilot import guard

    reply = say(venture, "allow sending up to 12 emails a day")
    assert not any(action.get("confirmation") for action in reply["actions"])
    from app.core.local_mode import is_local

    if is_local():
        from app.core.local_capabilities import blocked_message, enabled

        assert not enabled("send_email")
        assert blocked_message("send_email") in reply["body_md"]
    else:
        assert "not switched on" in reply["body_md"]
    assert any(action.get("action") == "autopilot.send_off" for action in reply["actions"])
    s = guard.get_settings(venture)
    assert (s["allow_send"], s["send_max_per_day"]) == (True, 12)
    assert not any(a.get("confirmation") for a in say(venture, "allow sending up to 5000 per day")["actions"])
    assert guard.get_settings(venture)["send_max_per_day"] == 12


@pytest.mark.parametrize("idea", ["Inventory alerts for workshops", "Weekend camping for families"])
def test_god_mode_and_pause_still_gate_correctly(venture, idea):
    from app.autopilot import guard
    from app.core.local_mode import is_local
    from app.core.local_secrets import update_settings
    from app.db.models import Venture
    from app.state import db

    with db()() as session:
        session.get(Venture, venture).raw_idea = idea
        session.commit()
    if is_local():
        update_settings("capabilities", {"background_work": True}, {})
        for text in ("turn on god mode", "gor mode on kr do"):
            reply = say(venture, text)
            assert reply["kind"] != "confirmation" and not any(action.get("confirmation") for action in reply["actions"])
            assert "no daily or lifetime spending cap" not in reply["body_md"]
            assert "never send to real people" not in reply["body_md"]
            assert guard.get_settings(venture)["god_mode"] is True
    else:
        assert card(say(venture, "turn on god mode"))["action"] == "autopilot.god_on"
        assert card(say(venture, "gor mode on kr do"))["action"] == "autopilot.god_on"
        assert guard.get_settings(venture)["god_mode"] is False
    paused = say(venture, "sab kuch rok do")
    assert not any(action.get("confirmation") for action in paused["actions"])
    from app.ventures.lifecycle import is_paused
    assert is_paused(venture)


def _b(**kw):
    base = {"id": "bld_1", "app_id": "app_1", "state": "succeeded", "closed": True, "artifact_id": "art_1", "verified": True,
            "qa": "passed", "review": {"status": "pending", "report_digest": "a" * 64}, "phase": None, "diagnostic": False}
    return {**base, **kw}


@pytest.mark.parametrize(("build", "ok", "words"), [
    (None, False, "No build"),
    (_b(), True, ""),
    (_b(state="running", closed=False), False, "not finished"),
    (_b(verified=False, qa="failed"), False, "QA has not passed"),
    (_b(review={"status": "accepted"}), False, "already accepted"),
    (_b(review={"status": "rejected"}), False, "asked for changes"),
    (_b(review={"status": "pending"}), False, "no saved QA report"),
    (_b(diagnostic=True), False, "diagnostic"),
])
def test_review_readiness_says_exactly_why(build, ok, words):
    load_company_os_app()
    from app.chat.jarvis import review_readiness

    got_ok, why = review_readiness(build)
    assert got_ok is ok and words in why


def test_build_accept_without_a_ready_build_explains_and_offers_no_card(venture):
    reply = say(venture, "accept build")
    assert "No build has run" in reply["body_md"] and not any(a.get("confirmation") for a in reply["actions"])
    assert "No build has run" in say(venture, "show build status")["body_md"]


def test_build_accept_request_executes_through_checked_builder_review(venture, monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    from app.builder import execution
    from app.chat import jarvis
    from app.db.supymem_models import BuildRun
    from app.workspaces.resolve import LOCAL_PERSON_ID

    monkeypatch.setattr(jarvis, "_latest_build", lambda *_: _b())
    seen = {}
    monkeypatch.setattr(execution, "review", lambda run_id, **kw: seen.update(run_id=run_id, **kw))

    class Run:
        venture_id = venture

    from sqlalchemy.orm import Session
    original_get = Session.get
    monkeypatch.setattr(Session, "get", lambda self, model, key, *args, **kwargs:
                        Run() if model is BuildRun else original_get(self, model, key, *args, **kwargs))
    out = say(venture, "accept the build")
    assert seen["decision"] == "accept" and seen["actor"] == LOCAL_PERSON_ID and "accepted" in out["body_md"]
    assert seen["run_id"] == "bld_1" and seen["source_artifact_id"] == "art_1" and seen["report_digest"] == "a" * 64
    assert not any(action.get("confirmation") for action in out["actions"])


@pytest.mark.parametrize("idea", ["Inventory alerts for distribution teams", "Art classes for local families"])
def test_batch_approve_without_drafts_sends_nothing(venture, idea):
    from app.db.models import Venture
    from app.state import db

    with db()() as session:
        session.get(Venture, venture).raw_idea = idea
        session.commit()
    reply = say(venture, "approve this batch")
    assert "Nothing was approved or sent" in reply["body_md"]
    assert reply["actions"][0]["label"] == "Show drafts"


def test_batch_approval_reviews_without_sending(venture, monkeypatch):
    from app.autopilot import outreach
    from app.chat import customers

    calls = []
    async def approve(venture_id, action, payload, actor):
        calls.append((venture_id, action, payload))
        return {"kind": "text", "body_md": "Your 2 drafts are approved. Ask to send them when you are ready.", "actions": []}

    monkeypatch.setattr(customers, "execute", approve)
    monkeypatch.setattr(outreach, "drafts", lambda *_a, **_k: [{"channel": "email", "batch_id": "bat_1"}, {"channel": "email", "batch_id": "bat_1"}])
    reply = say(venture, "approve this batch")
    assert calls == [(venture, "gtm.approve", {"all": True})]
    assert not reply["actions"] and "Ask to send" in reply["body_md"]


@pytest.mark.parametrize("mode", ["local", "hosted"])
def test_account_questions_and_requests_run_without_cards(request, monkeypatch, mode):
    if mode == "local":
        monkeypatch.setenv("SUPYMEM_MODE", "local")
    else:
        monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    venture = request.getfixturevalue("venture")
    from app.chat.execute import execute_action

    assert "Settings" in say(venture, "what did I spend today")["body_md"]
    assert "credit" not in say(venture, "what did I spend today")["body_md"]
    if mode == "local":
        requested = say(venture, "request credits")
        assert "Settings > Keys and connections" in requested["body_md"]
        assert "recorded model spending this month" in requested["body_md"]
        assert "credits" not in requested["body_md"] and "team" not in requested["body_md"]
        assert requested["kind"] != "confirmation"
        assert not any(action.get("confirmation") for action in requested["actions"])
        return
    requested = say(venture, "request credits")
    assert not any(action.get("confirmation") for action in requested["actions"])
    assert "saved for the team" in requested["body_md"]
    assert "already has" in run(execute_action(venture, "credits.request"))["body_md"]


@pytest.mark.parametrize("idea,what_we_need", [
    ("Stock tracking for distribution teams", "Pass your warehouse account check"),
    ("Weekend plans for local families", "Pass your family calendar check"),
])
def test_blocked_and_activity_read_the_same_services_as_right_now(venture, idea, what_we_need):
    from app.db.models import Venture
    from app.state import db

    with db()() as session:
        session.get(Venture, venture).raw_idea = idea
        session.commit()
    assert say(venture, "what's blocked")["body_md"]
    assert say(venture, "what are you doing right now")["body_md"]
    from app.needs_you import service
    from app.chat.next_moves import compose

    service.request_human(venture, reason_code="captcha", title="Solve a captcha", what_we_need=what_we_need, how_to_resume="Tap done", blocking_ref="t1")
    out = say(venture, "what's blocked")
    from app.core.local_mode import is_local

    assert out["kind"] == ("next_moves" if is_local() else "status")
    assert what_we_need in out["body_md"]
    if not is_local():
        assert "Here's the one thing only you can do: " + what_we_need in out["body_md"]
    assert out["actions"] == (compose(venture, requested=True)["actions"] if is_local() else [])
    state_hash = compose(venture)["state_hash"]
    assert state_hash and all(action.get("state_hash") == state_hash for action in out["actions"])
    assert "credit" not in out["body_md"].lower() and not out.get("action_started")


def test_new_chat_returns_the_client_action(venture):
    assert say(venture, "new chat")["actions"][0]["name"] == "chat.new_conversation"


@pytest.mark.parametrize("domain", ["Distribution planning", "Balcony gardening"])
def test_local_settings_navigation_keeps_hosted_routing(monkeypatch, domain):
    load_company_os_app()
    from app.chat.intent import COMPANY_SCREEN_PATHS, requested_venture_screen

    for mode in (None, "local"):
        if mode is None:
            monkeypatch.delenv("SUPYMEM_MODE", raising=False)
        else:
            monkeypatch.setenv("SUPYMEM_MODE", mode)
        expected = "local settings" if mode == "local" else None
        assert requested_venture_screen("open local settings") == expected
        assert requested_venture_screen(f"open {domain} local settings", domain) == expected
        assert requested_venture_screen("open local capabilities") == ("local capabilities" if mode == "local" else None)
        assert requested_venture_screen("open account") == "account"
    assert COMPANY_SCREEN_PATHS["local settings"] == "/settings/local"
    assert COMPANY_SCREEN_PATHS["local capabilities"] == "/settings/local/capabilities"


def test_every_founder_page_is_reachable_by_name():
    load_company_os_app()
    from app.chat.intent import COMPANY_SCREEN_PATHS, VENTURE_SCREEN_ROUTES, classify_intent, requested_venture_screen

    internal = ("admin/", "operator/", "ops/", "auth/", "invite/", "setup/", "suggestions/overlay", "welcome/", "new/", "gtm/lab", "pagebuilder-lab/", "lab/diagrams")
    assert "<AdminGate>" in (ROOT / "apps/console/src/app/pagebuilder-lab/layout.tsx").read_text()
    assert "<AdminGate>" in (ROOT / "apps/console/src/app/lab/diagrams/layout.tsx").read_text()
    skip = ("connections/", "v/[ventureId]/connect/", "v/[ventureId]/device-work-orders/")
    pages = sorted(str(p.relative_to(ROOT / "apps/console/src/app")) for p in (ROOT / "apps/console/src/app").rglob("page.tsx"))
    reachable = {(f"v/[ventureId]/{r}/page.tsx" if r else "v/[ventureId]/page.tsx") for r in VENTURE_SCREEN_ROUTES.values()
                 if r not in COMPANY_SCREEN_PATHS}
    reachable |= {f"{p.strip('/')}/page.tsx" for p in COMPANY_SCREEN_PATHS.values()}
    missing = [p for p in pages if p != "page.tsx" and p not in reachable
               and not p.startswith(internal) and not p.startswith(skip)]
    assert missing == [], missing
    assert requested_venture_screen("open the account page") == "account"
    assert requested_venture_screen("open simulate") == "simulate"
    for text, route in {
        "open guide": "guide", "open the guide page": "guide",
        "open capabilities": "capabilities", "show me the capabilities screen": "capabilities",
        "do not open guide": None, "don't open capabilities": None,
        "open guide and send an email": None,
    }.items():
        assert requested_venture_screen(text) == route
        if route:
            assert classify_intent(text) == "open_screen"
    assert requested_venture_screen("open My Company guide", "My Company") == "guide"
    assert requested_venture_screen("open Other Company guide", "My Company") is None
    assert "guide" not in COMPANY_SCREEN_PATHS
    assert COMPANY_SCREEN_PATHS[requested_venture_screen("open capabilities")] == "/guide"


@pytest.mark.parametrize("venture_id", ["ven_freight", "ven_gardening"])
def test_support_bundle_is_reachable_only_in_local_chat(monkeypatch, venture_id):
    load_company_os_app()
    from app.chat.context import ChatContext
    from app.chat.intent import classify_intent, requested_venture_screen
    from app.chat.router import _dispatch_intent

    text = "Open my support bundle"
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    intent = classify_intent(text)
    assert intent == "open_screen"
    reply = asyncio.run(_dispatch_intent(venture_id, ChatContext(venture={"id": venture_id}), intent, text, None))
    assert reply["actions"] == [{"label": "Open screen", "href": f"/settings/local/support?venture={venture_id}", "style": "primary"}]
    monkeypatch.delenv("SUPYMEM_MODE")
    assert requested_venture_screen(text) is None

"""The hourly worker reconciles real state, stays quiet and survives reruns."""
import json
from datetime import timedelta, timezone

import pytest

from tests.unit.test_email_engine import env as env  # noqa: F401
from tests.unit.test_email_engine import _rows


@pytest.fixture
def lifecycle(env, monkeypatch):
    from app.email import service, triggers
    monkeypatch.setenv("SUPYMEM_EMAIL_LIFECYCLE_ENABLED", "1")
    monkeypatch.setenv("SUPYMEM_EMAIL_TEMPLATE_ALLOWLIST", "welcome,finish_setup,get_mac_app,idea_stalled,research_started,beta_feedback,credits_low,credits_exhausted,problem_received,problem_resolved,milestone,progress_summary,task_finished,task_failed,action_failed,outreach_drafts_ready,outreach_sent,replies_received,nudge_d2,nudge_d5,nudge_d14,nudge_d30,digest_daily,weekly_insights")
    now = service.utcnow().replace(hour=12, minute=0, second=0, microsecond=0)
    monkeypatch.setattr(service, "utcnow", lambda: now)
    monkeypatch.setattr(triggers, "utcnow", lambda: now)
    return env[1], now


def _only(monkeypatch, *templates):
    monkeypatch.setenv("SUPYMEM_EMAIL_TEMPLATE_ALLOWLIST", ",".join(templates))


def _venture(state, now, *, created=None):
    from app.db.models import Venture, Workspace, WorkspaceMember
    with state.db()() as s:
        s.add(Workspace(id="ws_life", kind="venture", name="Test venture", entry_mode="idea", owner_id="usr_a", created_at=now, updated_at=now))
        s.add(Venture(id="ven_life", workspace_id="ws_life", name="Test venture", raw_idea="A real idea", stage="idea", created_at=created or now, updated_at=now))
        s.add(WorkspaceMember(id="wm_life_owner", workspace_id="ws_life", person_id="usr_a",
            role="owner", can_approve=1, created_at=now))
        s.commit()


def _chat(state, now, kind, *, ident="msg_life", actions=None, conversation_id=None, source_key=None):
    from app.chat.milestones import _scoped
    from app.db.models import ChatMessage
    with state.db()() as s:
        s.add(ChatMessage(id=ident, venture_id="ven_life", workspace_id="ws_life", role="assistant", kind=kind,
            body_md="Your saved work is ready.", created_at=now, actions_json=json.dumps(actions) if actions else None,
            conversation_id=conversation_id, idempotency_key=_scoped("ven_life", source_key) if source_key else None))
        s.commit()


def _admission(state, now, *, code="LIFE2345", active=True, max_uses=1):
    from app.db.supymem_models import BetaCode, BetaCodeRedemption
    with state.db()() as s:
        s.add(BetaCode(code=code, owner_person_id="usr_a", active=active, max_uses=max_uses, created_at=now))
        s.flush()
        s.add(BetaCodeRedemption(person_id="usr_a", user_id="usr_a", code=code, created_at=now))
        s.commit()


def _run(now):
    from app.email.triggers import run_schedule
    return run_schedule(now)


@pytest.mark.parametrize("idea", ["Stock alerts for hardware distributors", "Weekend cooking clubs for new residents"])
def test_monday_learning_digest_uses_recorded_results_budget_and_existing_delivery_consent(lifecycle, monkeypatch, idea):
    from app.credits import policy
    from app.credits.units import credits_to_micro
    from app.db.models import Venture, Workspace
    from app.db.supymem_models import Experiment, IdeaRevision, IdeaSession
    from app.email import providers, service, templates, triggers
    from app.experiments import beliefs, ledger

    state, now = lifecycle
    monday = now.replace(hour=20) + timedelta(days=(0 - now.weekday()) % 7)
    monkeypatch.setattr(service, "utcnow", lambda: monday)
    monkeypatch.setattr(triggers, "utcnow", lambda: monday)
    monkeypatch.setattr("core.clock.utcnow", lambda: monday.replace(tzinfo=timezone.utc))
    _only(monkeypatch, "weekly_learning", "weekly_insights")
    _venture(state, now)
    original_policy = policy.get_policy
    flag = {"on": False}
    monkeypatch.setattr(policy, "get_policy", lambda key, default=None:
        {"experiments_autopilot_v1": flag["on"]} if key == "feature_flags" else original_policy(key, default))
    with state.db()() as session:
        session.get(Venture, "ven_life").name = idea
        session.add(IdeaSession(id="learning_idea", workspace_id="ws_life", venture_id="ven_life",
            case="know", state="confirmed", current_revision_id="learning_revision", confirmed_revision_id="learning_revision",
            confirmed_at=now, confirmed_by_person_id="usr_a", created_at=now, updated_at=now))
        session.flush()
        session.add(IdeaRevision(id="learning_revision", session_id="learning_idea", workspace_id="ws_life",
            venture_id="ven_life", rev=1, source="founder", summary=idea,
            structured=json.dumps({"problem": idea}), created_at=now))
        saved = beliefs.row(session, "ven_life", create=True)
        saved.context = json.dumps({"beliefs": [{"id": "belief_digest", "statement": idea, "status": "disproven",
            "type": "pain", "confidence": 0.8, "evidence": [
                {"experiment_id": "digest_before", "outcome": "supported"},
                {"experiment_id": "digest_flip", "outcome": "refuted"}]}],
            "plan": {"id": "plan_digest", "state": "active", "tests": [], "approved_at": now.isoformat(),
                "ceiling_micro": credits_to_micro(100), "reserved_micro": credits_to_micro(30)}})
        for ident, outcome, days, cost in (("before", "supported", 8, 50), ("flip", "refuted", 2, 20),
                                            ("uncertain", "inconclusive", 1, None), ("future", "supported", -1, 40)):
            result = {"outcome": outcome}
            if cost is not None:
                result["actual_micro"] = credits_to_micro(cost)
            session.add(Experiment(id="digest_" + ident, venture_id="ven_life", workspace_id="ws_life",
                kind=ledger.KIND, state="concluded", hypothesis=idea + " " + ident, outcome=outcome,
                ended_at=monday - timedelta(days=days), context=json.dumps({"runner": "system", "direction": "learning_revision", "result": result})))
        session.add(Workspace(id="ws_other_learning", kind="venture", name="Other workspace", entry_mode="idea", owner_id="usr_b",
            created_at=now, updated_at=now))
        session.flush()
        session.add(Venture(id="ven_other_learning", workspace_id="ws_other_learning", name="Private other work", raw_idea="Unrelated work",
            stage="idea", created_at=now, updated_at=now))
        session.flush()
        session.add(Experiment(id="digest_private", venture_id="ven_other_learning", workspace_id="ws_other_learning",
            kind=ledger.KIND, state="concluded", hypothesis="Private other result", outcome="supported", ended_at=monday - timedelta(days=1),
            context=json.dumps({"result": {"actual_micro": credits_to_micro(900)}})))
        session.add(Experiment(id="digest_wrong_workspace", venture_id="ven_life", workspace_id="ws_other_learning",
            kind=ledger.KIND, state="concluded", hypothesis="Private mismatched workspace", outcome="supported", ended_at=monday - timedelta(days=1),
            context=json.dumps({"result": {"actual_micro": credits_to_micro(900)}})))
        session.commit()
    assert _run(monday)["queued"] == 0
    flag["on"] = True
    assert _run(monday.replace(hour=5))["queued"] == 0
    assert _run(monday)["queued"] == 1
    assert _run(monday)["queued"] == 0
    row = _rows(state, template="weekly_learning")[0]
    context = json.loads(row.context_json)
    assert context["path"] == "/v/ven_life/market-map"
    assert idea in context["changes"][0] and "didn't work" in context["changes"][0]
    assert idea in context["surprise"] and "didn't work" in context["surprise"]
    assert context["ended_tests"] == [idea + ": " + idea + " flip"]
    assert context["budgets"] == [idea + ": 70.00 credits not committed to tests"]
    assert context["recorded_spend"] == "20.00 credits; some test costs are not recorded"
    assert context["next_step"].startswith("Consider a pivot") and idea + " flip" in context["next_step"]
    assert "private" not in row.context_json.lower() and "future" not in row.context_json and "900" not in row.context_json
    subject, html, plain = templates.render("weekly_learning", context, unsubscribe_url="https://example.test/opt-out",
        prefs_url="https://example.test/preferences")
    assert subject == "Your week of learning" and "Unsubscribe:" in plain
    assert "/v/ven_life/market-map" in html and "/v/ven_life/market-map" in plain
    assert "\u2014" not in plain and "hypothesis" not in plain and "disproven" not in plain
    flag["on"] = False
    with state.db()() as session:
        assert triggers.refresh_context(session, "usr_a", "weekly_learning", context) is None
        assert triggers.refresh_context(session, "usr_a", "weekly_learning", {**context, "_email_delivery_payload": {}}) is None
    assert _run(monday)["queued"] == 0
    assert not _rows(state, template="weekly_insights")
    flag["on"] = True
    calls = []
    monkeypatch.setitem(providers.PROVIDERS, "log", lambda message: calls.append(message) or "log_learning")
    service.set_prefs("usr_a", {"digests": False})
    assert service.deliver(row.id) == "suppressed"
    assert calls == []
    service.set_prefs("usr_a", {"digests": True})
    restored = service.send_email("usr_a", "weekly_learning", context, ref="consent-restored")
    assert restored and service.deliver(restored) == "sent"
    assert service.deliver(restored) == "sent" and len(calls) == 1
    assert "/v/ven_life/market-map" in calls[0].html
    with state.db()() as session:
        session.get(Workspace, "ws_life").owner_id = "usr_b"
        session.commit()
        assert triggers.refresh_context(session, "usr_a", "weekly_learning", context) is None
        assert triggers.refresh_context(session, "usr_a", "weekly_learning", {**context, "_email_delivery_payload": {}}) is None


def test_welcome_and_two_setup_reminders_survive_restarts(lifecycle, monkeypatch):
    from app.db.models import Person
    from app.email import service
    state, now = lifecycle
    _only(monkeypatch, "welcome", "finish_setup")
    _admission(state, now)
    with state.db()() as s:
        s.get(Person, "usr_a").created_at = now - timedelta(hours=2)
        s.commit()
    assert _run(now)["queued"] == 2
    assert _run(now)["queued"] == 0
    later = now + timedelta(days=1, minutes=1)
    monkeypatch.setattr(service, "utcnow", lambda: later)
    assert _run(later)["queued"] == 1
    assert _run(later + timedelta(hours=1))["queued"] == 0
    rows = _rows(state, person_id="usr_a")
    assert [r.template for r in rows] == ["welcome", "finish_setup", "finish_setup"]
    assert {json.loads(r.context_json)["attempt"] for r in rows if r.template == "finish_setup"} == {1, 2}
    assert _run(now + timedelta(days=20))["queued"] == 0


def test_setup_stops_when_a_venture_exists_and_mac_requires_no_device(lifecycle, monkeypatch):
    from app.db.models import Person
    from app.db.supymem_models import DesktopDevice
    state, now = lifecycle
    _only(monkeypatch, "finish_setup", "get_mac_app")
    _admission(state, now)
    with state.db()() as s:
        s.get(Person, "usr_a").created_at = now - timedelta(days=2)
        s.add(DesktopDevice(id="dev_life", person_id="usr_a", name="Mac", last_seen_at=now, platform="macos"))
        s.commit()
    _venture(state, now)
    _run(now)
    assert not _rows(state, person_id="usr_a")
    with state.db()() as s:
        s.get(DesktopDevice, "dev_life").revoked_at = now
        s.commit()
    _run(now)
    assert [r.template for r in _rows(state, person_id="usr_a")] == ["get_mac_app"]
    context = json.loads(_rows(state, template="get_mac_app")[0].context_json)
    assert context["admission_code"] == "LIFE2345" and "path" not in context


def test_mac_download_uses_existing_admission_even_when_invite_is_full(lifecycle, monkeypatch):
    from app.api import beta_api
    from app.db.models import Person
    from app.email import templates
    state, now = lifecycle
    _only(monkeypatch, "get_mac_app")
    monkeypatch.setenv("PUBLIC_GATEWAY_URL", "https://gateway.supymem.com")
    monkeypatch.setenv("BETA_DMG_URL", beta_api.DEFAULT_DMG_URL)
    _admission(state, now, max_uses=1)
    with state.db()() as s:
        s.get(Person, "usr_a").created_at = now - timedelta(days=2)
        s.commit()
    assert _run(now)["queued"] == 1
    context = json.loads(_rows(state, template="get_mac_app")[0].context_json)
    _, html, plain = templates.render("get_mac_app", context)
    assert "https://gateway.supymem.com/public/beta/download?code=LIFE2345" in html
    assert "https://gateway.supymem.com/public/beta/download?code=LIFE2345" in plain
    assert beta_api.verify(beta_api.VerifyBody(code="LIFE2345"), x_client_hash="email-full-invite").status_code == 404
    response = beta_api.download(code="LIFE2345", x_client_hash="email-existing-download", x_client_ua="unit")
    assert response.status_code == 302 and response.headers["location"] == beta_api.DEFAULT_DMG_URL


def test_stalled_idea_quotes_only_the_latest_unanswered_question(lifecycle, monkeypatch):
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    state, now = lifecycle
    _only(monkeypatch, "idea_stalled")
    _venture(state, now - timedelta(days=2))
    with state.db()() as s:
        s.add(IdeaSession(id="ids_life", venture_id="ven_life", workspace_id="ws_life", case="know", state="clarifying", created_at=now - timedelta(days=2), updated_at=now - timedelta(days=1, hours=1)))
        s.flush()
        s.add(ClarifierTurn(id="ctr_life", session_id="ids_life", venture_id="ven_life", workspace_id="ws_life", turn=1, role="agent", kind="question", text="Who has this problem?", created_at=now - timedelta(days=1)))
        s.commit()
    _run(now)
    row = _rows(state, template="idea_stalled")[0]
    assert row.category == "engagement_reminders"
    assert json.loads(row.context_json)["detail"] == "Who has this problem?"
    assert json.loads(row.context_json)["path"] == "/v/ven_life/idea"
    _run(now)
    assert len(_rows(state, template="idea_stalled")) == 1


def test_answered_question_and_confirmed_idea_do_not_nag(lifecycle, monkeypatch):
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    state, now = lifecycle
    _only(monkeypatch, "idea_stalled")
    _venture(state, now)
    with state.db()() as s:
        s.add(IdeaSession(id="ids_life", venture_id="ven_life", workspace_id="ws_life", case="know", state="clarifying", created_at=now - timedelta(days=2), updated_at=now - timedelta(days=2)))
        s.flush()
        s.add(ClarifierTurn(id="ctr_life", session_id="ids_life", venture_id="ven_life", workspace_id="ws_life", turn=2, role="founder", kind="answer", text="An answer", created_at=now - timedelta(days=1)))
        s.commit()
    _run(now)
    assert not _rows(state, template="idea_stalled")


def test_progress_waits_two_hours_then_recovers_an_active_founder(lifecycle, monkeypatch):
    from app.db.models import AnalyticsEvent
    from app.email import service
    state, now = lifecycle
    _only(monkeypatch, "milestone", "progress_summary")
    _venture(state, now)
    _chat(state, now - timedelta(hours=1), "plan_ready")
    assert _run(now)["queued"] == 0
    later = now + timedelta(hours=2)
    monkeypatch.setattr(service, "utcnow", lambda: later)
    with state.db()() as s:
        s.add(AnalyticsEvent(id="ane_active", person_id="usr_a", event_name="session.active", occurred_at=later))
        s.commit()
    assert _run(later)["queued"] == 0
    quiet = later + timedelta(hours=1, seconds=1)
    monkeypatch.setattr(service, "utcnow", lambda: quiet)
    assert _run(quiet)["queued"] == 1
    assert _run(quiet)["queued"] == 0
    row = _rows(state, template="milestone")[0]
    assert json.loads(row.context_json)["path"] == "/v/ven_life/plan"
    assert json.loads(row.context_json)["source_ids"] == ["chat:msg_life"]


def test_nearby_progress_batches_once_and_archived_or_old_events_stay_silent(lifecycle, monkeypatch):
    from app.db.models import Venture
    state, now = lifecycle
    _only(monkeypatch, "milestone", "progress_summary", "task_finished")
    _venture(state, now)
    _chat(state, now - timedelta(hours=4), "plan_ready")
    _chat(state, now - timedelta(hours=3), "mac_task_finished", ident="msg_mac", actions=[{"href": "/v/ven_life/device-work-orders/dwo_life"}])
    _chat(state, now - timedelta(days=9), "plan_ready", ident="msg_old")
    assert _run(now)["queued"] == 1
    assert _run(now)["queued"] == 0
    row = _rows(state, template="progress_summary")[0]
    context = json.loads(row.context_json)
    assert context["path"] == "/v/ven_life/device-work-orders/dwo_life"
    assert context["source_ids"] == ["chat:msg_life", "chat:msg_mac"]
    with state.db()() as s:
        s.get(Venture, "ven_life").archived_at = now
        s.commit()
    _chat(state, now - timedelta(hours=3), "plan_ready", ident="msg_archived")
    assert _run(now)["queued"] == 0


def test_private_conversation_of_another_person_never_emails_owner(lifecycle, monkeypatch):
    from app.db.models import ChatConversation
    state, now = lifecycle
    _only(monkeypatch, "milestone", "task_finished")
    _venture(state, now)
    with state.db()() as s:
        s.add(ChatConversation(id="conv_private", venture_id="ven_life", workspace_id="ws_life", visibility="private", creator_person_id="usr_b", title="Private", creation_key="private-life", created_at=now, updated_at=now))
        s.commit()
    _chat(state, now - timedelta(hours=3), "plan_ready", conversation_id="conv_private")
    _run(now)
    assert not _rows(state, person_id="usr_a")


def test_legacy_shared_progress_remains_visible_without_private_other_chat(lifecycle, monkeypatch):
    from app.db.models import ChatConversation
    from app.email.triggers import _activity
    state, now = lifecycle
    _only(monkeypatch, "milestone")
    _venture(state, now)
    with state.db()() as s:
        s.add_all([
            ChatConversation(id="conv_legacy", venture_id="ven_life", workspace_id="ws_life", visibility="legacy_shared", creator_person_id=None, title="Earlier shared chat", creation_key="legacy:ven_life", created_at=now, updated_at=now),
            ChatConversation(id="conv_private", venture_id="ven_life", workspace_id="ws_life", visibility="private", creator_person_id="usr_b", title="Private", creation_key="private-life", created_at=now, updated_at=now),
        ])
        s.commit()
    _chat(state, now - timedelta(hours=4), "plan_ready", ident="msg_private", conversation_id="conv_private")
    _chat(state, now - timedelta(hours=3), "plan_ready", ident="msg_shared", conversation_id="conv_legacy")
    assert _run(now)["queued"] == 1
    assert _run(now)["queued"] == 0
    context = json.loads(_rows(state, template="milestone")[0].context_json)
    assert context["source_ids"] == ["chat:msg_shared"]
    with state.db()() as s:
        assert _activity(s, "usr_a", now - timedelta(days=7), now) == ["Test venture: Your saved work is ready."]


def test_waitlist_milestones_exclude_test_entries_and_need_no_app(lifecycle, monkeypatch):
    from app.db.supymem_models import VentureWaitlist, VentureWaitlistEntry
    state, now = lifecycle
    _only(monkeypatch, "milestone", "progress_summary")
    _venture(state, now)
    with state.db()() as s:
        s.add(VentureWaitlist(venture_id="ven_life", workspace_id="ws_life", enabled=True, enabled_by="usr_a", consent_version="early-access-v1", updated_at=now))
        s.flush()
        for i in range(11):
            s.add(VentureWaitlistEntry(id=f"wle_{i}", venture_id="ven_life", workspace_id="ws_life", email=f"owned-fixture-{i}@example.test", consent_version="early-access-v1", consent_text="Consented", is_test=i == 10, created_at=now - timedelta(hours=3)))
        s.commit()
    _run(now)
    row = _rows(state, template="progress_summary")[0]
    assert json.loads(row.context_json)["source_ids"] == ["waitlist:ven_life:1", "waitlist:ven_life:10"]
    assert "unverified interest" in row.context_json
    assert "example.test" not in row.context_json
    _run(now)
    assert len(_rows(state, template="progress_summary")) == 1


@pytest.mark.parametrize("spent,reserved,expected,percent", [(79, 21, None, None), (80, 0, "credits_low", 80), (96, 0, "credits_low", 95), (100, 0, "credits_exhausted", 100)])
def test_credits_count_actual_spend_and_only_highest_threshold(lifecycle, monkeypatch, spent, reserved, expected, percent):
    from app.db.models import CreditAccount
    state, now = lifecycle
    _only(monkeypatch, "credits_low", "credits_exhausted")
    _venture(state, now)
    with state.db()() as s:
        s.add(CreditAccount(id="ca_life", venture_id="ven_life", workspace_id="ws_life", owner_person_id="usr_a", granted_micro=100, spent_micro=spent, reserved_micro=reserved, created_at=now, updated_at=now))
        s.commit()
    _run(now)
    _run(now)
    rows = _rows(state, person_id="usr_a")
    assert len(rows) == (1 if expected else 0)
    if expected:
        assert rows[0].template == expected and rows[0].category == "transactional"
        assert json.loads(rows[0].context_json)["percent"] == percent
        assert json.loads(rows[0].context_json)["path"] == "/v/ven_life/settings"


def test_problem_ack_and_resolution_use_status_without_copying_report(lifecycle, monkeypatch):
    from app.db.supymem_models import ProblemReport
    state, now = lifecycle
    _only(monkeypatch, "problem_received", "problem_resolved")
    with state.db()() as s:
        s.add(ProblemReport(id="prb_life", person_id="usr_a", message="PRIVATE NOTE NEVER COPIED", created_at=now - timedelta(hours=1), status="open"))
        s.commit()
    _run(now)
    _run(now)
    assert len(_rows(state, template="problem_received")) == 1
    with state.db()() as s:
        s.get(ProblemReport, "prb_life").status = "resolved"
        s.commit()
    _run(now)
    _run(now)
    assert len(_rows(state, template="problem_resolved")) == 1
    assert all("PRIVATE NOTE" not in r.context_json for r in _rows(state))


def test_daily_is_opt_in_and_empty_weeks_do_not_send(lifecycle, monkeypatch):
    from app.db.supymem_models import EmailPreference
    state, now = lifecycle
    _only(monkeypatch, "digest_daily", "weekly_insights")
    _venture(state, now)
    # Choose Tuesday after configured hour, irrespective of the test date.
    tuesday = now.replace(hour=20) + timedelta(days=(1 - now.weekday()) % 7)
    _chat(state, tuesday - timedelta(hours=1), "plan_ready")
    with state.db()() as s:
        pref = s.get(EmailPreference, "usr_a")
        if pref:
            s.delete(pref)
        s.commit()
    _run(tuesday)
    assert not _rows(state, template="digest_daily")
    from app.email.service import set_prefs
    set_prefs("usr_a", {"digests": True})
    _run(tuesday)
    _run(tuesday)
    assert len(_rows(state, template="digest_daily")) == 1
    _run(tuesday + timedelta(days=13))
    assert not _rows(state, template="weekly_insights")


def test_nudges_use_real_next_step_and_one_30_day_winback(lifecycle, monkeypatch):
    from app.db.models import Person
    from app.email import service
    state, now = lifecycle
    _only(monkeypatch, "nudge_d5", "nudge_d30")
    _venture(state, now)
    with state.db()() as s:
        s.get(Person, "usr_a").created_at = now - timedelta(days=5)
        s.commit()
    _run(now)
    row = _rows(state, template="nudge_d5")[0]
    assert json.loads(row.context_json)["path"] == "/v/ven_life/idea"
    assert "question" in json.loads(row.context_json)["next_step"].lower()
    later = now + timedelta(days=25, minutes=1)
    monkeypatch.setattr(service, "utcnow", lambda: later)
    _run(later.replace(tzinfo=timezone.utc))
    _run(later)
    assert len(_rows(state, template="nudge_d30")) == 1
    _run(later + timedelta(days=20))
    assert len(_rows(state, template="nudge_d30")) == 1


def test_feedback_is_once_after_first_venture(lifecycle, monkeypatch):
    state, now = lifecycle
    _only(monkeypatch, "beta_feedback")
    _venture(state, now, created=now - timedelta(days=3))
    _run(now)
    _run(now)
    row = _rows(state, template="beta_feedback")[0]
    # Beta middleware hides /feedback; the reachable venture CTA accompanies reply-by-email copy.
    assert json.loads(row.context_json)["path"] == "/v/ven_life"
    assert len(_rows(state, template="beta_feedback")) == 1


def test_unsubscribed_and_bounced_founders_are_never_queued(lifecycle, monkeypatch):
    from app.email import service
    state, now = lifecycle
    _only(monkeypatch, "beta_feedback")
    _venture(state, now, created=now - timedelta(days=3))
    service.set_prefs("usr_a", {"engagement_reminders": False})
    assert _run(now)["queued"] == 0
    assert _rows(state, template="beta_feedback")[0].status == "suppressed"
    # A different event also respects address suppression.
    service.suppress("a@example.test", "complained")
    _only(monkeypatch, "milestone")
    _chat(state, now - timedelta(hours=3), "plan_ready")
    assert _run(now)["queued"] == 0
    assert _rows(state, template="milestone")[0].status == "suppressed"


def test_older_acknowledged_report_gets_one_resolved_followup(lifecycle, monkeypatch):
    from app.db.supymem_models import ProblemReport
    from app.email import service
    state, now = lifecycle
    _only(monkeypatch, "problem_received", "problem_resolved")
    with state.db()() as s:
        s.add(ProblemReport(id="prb_old", person_id="usr_a", message="PRIVATE OLD REPORT", created_at=now - timedelta(days=14), status="open"))
        s.add(ProblemReport(id="prb_legacy", person_id="usr_a", message="PRIVATE NEVER ACKED", created_at=now - timedelta(days=14), status="resolved"))
        s.commit()
    service.deliver(service.send_email("usr_a", "problem_received", {"path": "/"}, ref="prb_old"))
    _run(now)
    assert not _rows(state, template="problem_resolved")
    with state.db()() as s:
        s.get(ProblemReport, "prb_old").status = "resolved"
        s.commit()
    _run(now)
    _run(now)
    rows = _rows(state, template="problem_resolved")
    assert len(rows) == 1
    assert rows[0].idempotency_key == service._key("usr_a", "problem_resolved", "prb_old")
    assert "PRIVATE" not in rows[0].context_json


def test_weekly_signup_only_activity_has_next_step_and_excludes_tests(lifecycle, monkeypatch):
    from app.db.supymem_models import VentureWaitlist, VentureWaitlistEntry
    from app.email.service import set_prefs
    state, now = lifecycle
    _only(monkeypatch, "weekly_insights")
    _venture(state, now)
    set_prefs("usr_a", {"digests": True})
    monday = now.replace(hour=20) + timedelta(days=(0 - now.weekday()) % 7)
    with state.db()() as s:
        s.add(VentureWaitlist(venture_id="ven_life", workspace_id="ws_life", enabled=True, enabled_by="usr_a", consent_version="early-access-v1", updated_at=now))
        s.flush()
        for i in range(2):
            s.add(VentureWaitlistEntry(id=f"wle_weekly_{i}", venture_id="ven_life", workspace_id="ws_life", email=f"weekly-fixture-{i}@example.test", consent_version="early-access-v1", consent_text="Consented", is_test=i == 1, created_at=monday - timedelta(days=1)))
        s.commit()
    _run(monday)
    _run(monday)
    rows = _rows(state, template="weekly_insights")
    assert len(rows) == 1
    context = json.loads(rows[0].context_json)
    assert context["signup_count"] == 1 and context["next_step"]
    assert context["path"] == "/v/ven_life/idea"


@pytest.mark.parametrize("ambient", ["none", "scheduler", "matching"])
def test_guidance_reader_uses_current_recipient_and_restores_scope(lifecycle, monkeypatch, ambient):
    from contextlib import nullcontext
    from app.db.models import Venture
    from app.email import triggers
    from app.tenancy import Scope, bind, current_or_none
    from app.ventures import next_step

    state, now = lifecycle
    _venture(state, now)
    with state.db()() as s:
        venture = s.get(Venture, "ven_life")
    original = next_step.next_step
    seen = []
    def read(venture_id):
        seen.append(current_or_none())
        assert current_or_none() == Scope("ws_life", "ven_life", "usr_a")
        return original(venture_id)
    monkeypatch.setattr(next_step, "next_step", read)
    context = (nullcontext() if ambient == "none" else
        bind(workspace_id="none" if ambient == "scheduler" else "ws_life", venture_id="", actor="system" if ambient == "scheduler" else "usr_a"))
    with context:
        before = current_or_none()
        result = triggers._next_step(venture, "usr_a")
        assert result["path"] == "/v/ven_life/idea" and result["venture_name"] == "Test venture"
        assert seen == [Scope("ws_life", "ven_life", "usr_a")]
        assert current_or_none() == before


@pytest.mark.parametrize("change", ["transfer", "membership_revoked_after_transfer", "venture_stage_archived", "workspace_archived", "venture_archived", "workspace_blocked"])
def test_guidance_reader_rechecks_current_custody_before_private_facts(lifecycle, monkeypatch, change):
    from app.db.models import Venture, Workspace, WorkspaceMember
    from app.email import triggers
    from app.tenancy import bind, current_or_none
    from app.ventures import next_step

    state, now = lifecycle
    _venture(state, now)
    with state.db()() as s:
        venture = s.get(Venture, "ven_life")
    with state.db()() as s:
        workspace = s.get(Workspace, "ws_life")
        member = s.get(WorkspaceMember, "wm_life_owner")
        if change == "transfer":
            workspace.owner_id = "usr_b"
        elif change == "membership_revoked_after_transfer":
            workspace.owner_id = "usr_b"
            s.delete(member)
        elif change == "venture_stage_archived":
            s.get(Venture, "ven_life").stage = "archived"
        elif change == "workspace_archived":
            workspace.archived_at = now
        elif change == "venture_archived":
            s.get(Venture, "ven_life").archived_at = now
        else:
            workspace.settings_json = '{"blocked":true}'
        s.commit()
        if change == "transfer":
            assert s.get(WorkspaceMember, "wm_life_owner").role == "owner"
    monkeypatch.setattr(next_step, "_facts", lambda _: pytest.fail("Denied email guidance must not read private facts"))
    with bind(workspace_id="none", venture_id="", actor="system"):
        before = current_or_none()
        result = triggers._next_step(venture, "usr_a")
        assert result["path"] == "/" and result["venture_name"] == "Your venture"
        assert result["next_step"] == "Review your venture"
        assert current_or_none() == before


def test_guidance_reader_cannot_select_a_different_recipient(lifecycle, monkeypatch):
    from app.db.models import Venture
    from app.email import triggers
    from app.tenancy import current_or_none
    from app.ventures import next_step

    state, now = lifecycle
    _venture(state, now)
    with state.db()() as s:
        venture = s.get(Venture, "ven_life")
    before = current_or_none()
    monkeypatch.setattr(next_step, "_facts", lambda _: pytest.fail("Foreign recipient must not read private facts"))
    result = triggers._next_step(venture, "usr_b")
    assert result["path"] == "/" and result["venture_name"] == "Your venture"
    assert current_or_none() == before


@pytest.mark.parametrize("workspace_id,venture_id,actor", [
    ("ws_other", "ven_life", "usr_a"), ("ws_life", "ven_other", "usr_a"), ("ws_life", "ven_life", "usr_b")])
def test_guidance_reader_cannot_replace_foreign_ambient_scope(lifecycle, monkeypatch, workspace_id, venture_id, actor):
    from app.db.models import Venture
    from app.email import triggers
    from app.tenancy import bind, current_or_none
    from app.ventures import next_step

    state, now = lifecycle
    _venture(state, now)
    with state.db()() as s:
        venture = s.get(Venture, "ven_life")
    monkeypatch.setattr(next_step, "_facts", lambda _: pytest.fail("Foreign scope must not read private facts"))
    with bind(workspace_id=workspace_id, venture_id=venture_id, actor=actor):
        before = current_or_none()
        result = triggers._next_step(venture, "usr_a")
        assert result["path"] == "/" and result["venture_name"] == "Your venture"
        assert current_or_none() == before


def test_guidance_reader_restores_scheduler_scope_after_failed_read(lifecycle, monkeypatch):
    from app.db.models import Venture
    from app.email import triggers
    from app.tenancy import Scope, bind, current_or_none
    from app.ventures import next_step

    state, now = lifecycle
    _venture(state, now)
    with state.db()() as s:
        venture = s.get(Venture, "ven_life")
    def read(venture_id):
        assert current_or_none() == Scope("ws_life", "ven_life", "usr_a")
        raise RuntimeError("fixture_read_failed")
    monkeypatch.setattr(next_step, "next_step", read)
    with bind(workspace_id="none", venture_id="", actor="system"):
        before = current_or_none()
        with pytest.raises(RuntimeError, match="fixture_read_failed"):
            triggers._next_step(venture, "usr_a")
        assert current_or_none() == before


def test_guidance_reader_preserves_read_access_for_current_viewer(lifecycle):
    from app.db.models import Venture, WorkspaceMember
    from app.email import triggers
    from app.tenancy import current_or_none

    state, now = lifecycle
    _venture(state, now)
    with state.db()() as s:
        s.get(WorkspaceMember, "wm_life_owner").role = "viewer"
        s.commit()
        venture = s.get(Venture, "ven_life")
    before = current_or_none()
    result = triggers._next_step(venture, "usr_a")
    assert result["path"] == "/v/ven_life/idea"
    assert current_or_none() == before


def test_guidance_reader_preserves_current_owner_read_access_without_membership(lifecycle):
    from app.db.models import Venture, WorkspaceMember
    from app.email import triggers
    from app.tenancy import current_or_none

    state, now = lifecycle
    _venture(state, now)
    with state.db()() as s:
        s.delete(s.get(WorkspaceMember, "wm_life_owner"))
        s.commit()
        venture = s.get(Venture, "ven_life")
    before = current_or_none()
    result = triggers._next_step(venture, "usr_a")
    assert result["path"] == "/v/ven_life/idea"
    assert current_or_none() == before


@pytest.mark.parametrize("recipient", ["system", "anonymous", "local", "founder", "canonical_local", " usr_a "])
def test_guidance_reader_never_binds_reserved_person_as_normal_recipient(lifecycle, monkeypatch, recipient):
    from app.workspaces.resolve import LOCAL_PERSON_ID
    from app.db.models import Person, Venture, Workspace, WorkspaceMember
    from app.email import triggers
    from app.tenancy import bind, current_or_none
    from app.ventures import next_step

    state, now = lifecycle
    recipient = LOCAL_PERSON_ID if recipient == "canonical_local" else recipient
    _venture(state, now)
    with state.db()() as s:
        if s.get(Person, recipient) is None:
            s.add(Person(id=recipient, display_name="Reserved fixture", created_at=now))
            s.flush()
        s.get(Workspace, "ws_life").owner_id = recipient
        s.get(WorkspaceMember, "wm_life_owner").person_id = recipient
        s.commit()
        venture = s.get(Venture, "ven_life")
    seen = []
    original = next_step.next_step
    def read(venture_id):
        seen.append(current_or_none())
        return original(venture_id)
    monkeypatch.setattr(next_step, "next_step", read)
    monkeypatch.setattr(next_step, "_facts", lambda _: pytest.fail("Reserved recipient must not read private facts"))
    with bind(workspace_id="none", venture_id="", actor="system"):
        before = current_or_none()
        result = triggers._next_step(venture, recipient)
        assert result["path"] == "/" and result["venture_name"] == "Your venture"
        assert seen == []
        assert current_or_none() == before

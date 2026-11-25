"""Lifecycle hooks and durable event receipts cannot break core product flows."""
import json
from datetime import timedelta

import pytest

from tests.unit.email.test_lifecycle_schedule import env as env, lifecycle as lifecycle  # noqa: F401
from tests.unit.email.test_lifecycle_schedule import _admission, _chat, _only, _run, _venture
from tests.unit.test_email_engine import _rows


def test_hooks_fail_closed_and_logs_have_no_personal_data(lifecycle, monkeypatch, caplog):
    from app.db.supymem_models import NeedsYouRequest
    from app.email import triggers
    state, now = lifecycle
    _venture(state, now)
    with state.db()() as s:
        s.add(NeedsYouRequest(id="nyr_hook", venture_id="ven_life", workspace_id="ws_life", reason_code="captcha", title="Approve", blocking_ref="pending-hook", status="open", created_at=now))
        s.commit()
    _chat(state, now, "needs_you", source_key="decision")
    def broken(*args, **kwargs):
        raise RuntimeError("private@example.test PERSONAL MESSAGE")
    monkeypatch.setattr(triggers, "send_email", broken)
    triggers.on_welcome("usr_a")
    triggers.on_needs_you("ven_life", "Approve", "PRIVATE BODY", "nyr_hook:remind:1")
    triggers.on_milestone("ven_life", "needs_you", "decision", "PRIVATE BODY", human_reason="consent_send")
    triggers.on_god_mode_requested("ven_life", "PRIVATE BODY")
    assert "private@example.test" not in caplog.text and "PRIVATE" not in caplog.text
    assert not _rows(state)


def test_welcome_hook_and_hourly_fallback_share_one_key(lifecycle, monkeypatch):
    from app.db.models import Person
    from app.email.triggers import on_welcome
    state, now = lifecycle
    _only(monkeypatch, "welcome")
    _admission(state, now)
    with state.db()() as s:
        s.get(Person, "usr_a").created_at = now - timedelta(hours=2)
        s.commit()
    on_welcome("usr_a")
    on_welcome("usr_a")
    assert _run(now)["queued"] == 0
    assert len(_rows(state, template="welcome")) == 1


def test_unaccepted_build_and_preview_are_not_page_live(lifecycle, monkeypatch):
    state, now = lifecycle
    _only(monkeypatch, "milestone", "task_finished")
    _venture(state, now)
    for kind in ("first_look_ready", "build_ready", "build_accepted", "site_live"):
        _chat(state, now - timedelta(hours=3), kind, ident=f"msg_{kind}")
    _run(now)
    assert not _rows(state, person_id="usr_a")


def test_research_started_requires_a_confirmed_running_job(lifecycle, monkeypatch):
    from app.core.jobs import enqueue
    from app.db.models import Job
    from app.db.supymem_models import IdeaSession, ResearchDossier
    state, now = lifecycle
    _only(monkeypatch, "research_started")
    _venture(state, now)
    with state.db()() as s:
        s.add(IdeaSession(id="ids_running", venture_id="ven_life", workspace_id="ws_life", case="know", state="confirmed", confirmed_revision_id="rev_confirmed", confirmed_at=now - timedelta(hours=2), created_at=now - timedelta(hours=3), updated_at=now))
        ident = enqueue(s, "research.deep", {}, workspace_id="ws_life", venture_id="ven_life", dedupe_key="test-research-running")
        s.commit()
    _run(now)
    assert not _rows(state, template="research_started")
    with state.db()() as s:
        s.get(Job, ident).status = "running"
        s.commit()
    _run(now)
    _run(now)
    row = _rows(state, template="research_started")[0]
    assert row.category == "product_updates"
    assert json.loads(row.context_json)["path"] == "/v/ven_life/research"
    assert len(_rows(state, template="research_started")) == 1
    with state.db()() as s:
        s.add(ResearchDossier(id="rsd_complete", venture_id="ven_life", workspace_id="ws_life", kind="deep", revision_id="rev_confirmed", state="complete", created_at=now))
        s.query(__import__("app.db.supymem_models", fromlist=["EmailMessage"]).EmailMessage).filter_by(template="research_started").delete()
        s.commit()
    _run(now)
    assert not _rows(state, template="research_started")


def test_credit_settlement_commits_even_when_email_hook_breaks(lifecycle, monkeypatch):
    from app.credits.reserve import reserve, settle
    from app.db.models import CreditAccount
    from app.email import triggers
    state, now = lifecycle
    _venture(state, now)
    with state.db()() as s:
        s.add(CreditAccount(id="ca_settle", venture_id="ven_life", workspace_id="ws_life", owner_person_id="usr_a", granted_micro=100, created_at=now, updated_at=now))
        s.commit()
    hold = reserve(account_id="ca_settle", amount_micro=90, purpose="test.email.lifecycle")
    def broken(_account):
        raise RuntimeError("Email is unavailable")
    monkeypatch.setattr(triggers, "on_credit_balance", broken)
    result = settle(hold, actual_micro=80)
    assert result.actual_micro == 80
    with state.db()() as s:
        account = s.get(CreditAccount, "ca_settle")
        assert account.spent_micro == 80 and account.reserved_micro == 0
    assert settle(hold, actual_micro=80).ledger_id == result.ledger_id


def test_credit_hook_and_scheduler_dedupe_one_crossing(lifecycle, monkeypatch):
    from app.db.models import CreditAccount
    from app.email.triggers import on_credit_balance
    state, now = lifecycle
    _only(monkeypatch, "credits_low")
    _venture(state, now)
    with state.db()() as s:
        s.add(CreditAccount(id="ca_hook", venture_id="ven_life", workspace_id="ws_life", owner_person_id="usr_a", granted_micro=100, spent_micro=80, created_at=now, updated_at=now))
        s.commit()
    on_credit_balance("ca_hook")
    on_credit_balance("ca_hook")
    _run(now)
    assert len(_rows(state, template="credits_low")) == 1


def test_gmail_receipts_and_replies_are_real_scoped_and_content_free(lifecycle, monkeypatch):
    from app.db.models import GtmContact, GtmTouch
    state, now = lifecycle
    _only(monkeypatch, "outreach_sent", "replies_received")
    _venture(state, now)
    with state.db()() as s:
        s.add(GtmContact(id="gtmc_life", workspace_id="ws_life", email="fixture-recipient@example.test", stage_changed_at=now, created_at=now))
        s.flush()
        for ident, direction, simulated, vid in (("touch_sent", "out", 0, "ven_life"), ("touch_reply", "in", 0, "ven_life"), ("touch_simulated", "out", 1, "ven_life"), ("touch_unattributed", "in", 0, None)):
            s.add(GtmTouch(id=ident, workspace_id="ws_life", venture_id=vid, contact_id="gtmc_life", direction=direction, channel="email", send_mode="founder_gmail", simulated=simulated, message_id=ident, provider_thread_id=None if direction == "out" else "owned-thread", body_text="PRIVATE EMAIL CONTENT", sent_at=now - timedelta(hours=2), created_at=now - timedelta(hours=2)))
        s.commit()
    _run(now)
    _run(now)
    assert len(_rows(state, template="outreach_sent")) == 1
    assert len(_rows(state, template="replies_received")) == 1
    for template in ("outreach_sent", "replies_received"):
        row = _rows(state, template=template)[0]
        assert json.loads(row.context_json)["count"] == 1
        assert json.loads(row.context_json)["path"] == "/v/ven_life" + ("?chat=1" if template == "replies_received" else "")
        assert "PRIVATE EMAIL" not in row.context_json and "example.test" not in row.context_json
    assert _rows(state, template="outreach_sent")[0].category == "transactional"
    assert _rows(state, template="replies_received")[0].category == "product_updates"


def test_only_review_held_outreach_batches_are_notified(lifecycle, monkeypatch):
    from app.db.models import VentureDocument
    state, now = lifecycle
    _only(monkeypatch, "outreach_drafts_ready")
    _venture(state, now)
    with state.db()() as s:
        for ident, state_name, held in (("draft_review", "draft", True), ("draft_auto", "draft", False), ("draft_sent", "sent", True)):
            s.add(VentureDocument(id=ident, venture_id="ven_life", kind="outreach_draft", title="PRIVATE RECIPIENT", body_md="PRIVATE COPY", provenance_json=json.dumps({"batch_id": ident, "state": state_name, "channel": "email", "requires_batch_approval": held}), created_at=now - timedelta(hours=3), updated_at=now))
        s.commit()
    _run(now)
    _run(now)
    rows = _rows(state, template="outreach_drafts_ready")
    assert len(rows) == 1 and json.loads(rows[0].context_json)["count"] == 1
    assert json.loads(rows[0].context_json)["path"] == "/v/ven_life?chat=1"
    assert "PRIVATE" not in rows[0].context_json


def test_terminal_retry_decision_is_once_and_superseded_failure_does_not_email(lifecycle, monkeypatch):
    from app.core.jobs import enqueue
    from app.db.models import Job
    state, now = lifecycle
    _only(monkeypatch, "action_failed")
    _venture(state, now)
    with state.db()() as s:
        ident = enqueue(s, "research.deep", {}, workspace_id="ws_life", venture_id="ven_life", dedupe_key="test-failed-research")
        s.flush()
        job = s.get(Job, ident)
        job.status, job.created_at = "failed_dead", now - timedelta(hours=2)
        s.commit()
    _chat(state, now - timedelta(hours=1), "research_failed", actions=[{"action": "job.retry", "payload": {"kind": "research.deep"}}], source_key=f"research_failed:{ident}")
    _run(now)
    _run(now)
    rows = _rows(state, template="action_failed")
    assert len(rows) == 1 and rows[0].category == "transactional"
    assert json.loads(rows[0].context_json)["path"] == "/v/ven_life/research"
    with state.db()() as s:
        enqueue(s, "research.deep", {}, workspace_id="ws_life", venture_id="ven_life", dedupe_key="newer-research")
        s.commit()
    # A separate later saved error card cannot resurrect an already superseded job.
    _chat(state, now - timedelta(minutes=1), "research_failed", ident="msg_later_error", actions=[{"action": "job.retry", "payload": {"kind": "research.deep"}}])
    _run(now)
    assert len(_rows(state, template="action_failed")) == 1


def test_default_rollout_still_sends_latest_legacy_milestone(lifecycle, monkeypatch):
    state, now = lifecycle
    monkeypatch.delenv("SUPYMEM_EMAIL_LIFECYCLE_ENABLED")
    monkeypatch.delenv("SUPYMEM_EMAIL_TEMPLATE_ALLOWLIST")
    _venture(state, now)
    _chat(state, now - timedelta(hours=4), "plan_ready")
    _chat(state, now - timedelta(hours=3), "plan_ready", ident="msg_latest")
    _run(now)
    rows = _rows(state, template="milestone")
    assert len(rows) == 1
    assert json.loads(rows[0].context_json)["source_ids"] == ["chat:msg_latest"]
    assert not _rows(state, template="progress_summary")


@pytest.mark.parametrize("kind,what,path", [("research_ready", "research", "/v/ven_life/research"), ("build_accepted", "build", "/v/ven_life/builds?app=app_life&run=bld_life#build-review"), ("site_live", "landing", "/v/ven_life/site")])
def test_real_value_moments_have_exact_routes_and_evidence(lifecycle, monkeypatch, kind, what, path):
    from app.db.supymem_models import BuildRun, Deployment, FounderApp, ResearchDossier
    state, now = lifecycle
    _only(monkeypatch, "milestone")
    _venture(state, now)
    with state.db()() as s:
        s.add(FounderApp(id="app_life", venture_id="ven_life", workspace_id="ws_life", slug="lifecycle-fixture", name="Test app", kind="landing", state="draft", created_at=now, updated_at=now))
        s.flush()
        if kind == "research_ready":
            s.add(ResearchDossier(id="rsd_life", venture_id="ven_life", workspace_id="ws_life", kind="deep", state="partial", created_at=now - timedelta(hours=4), completed_at=now - timedelta(hours=3)))
        elif kind == "build_accepted":
            s.add(BuildRun(id="bld_life", app_id="app_life", venture_id="ven_life", workspace_id="ws_life", trigger="founder", agent="codex", state="succeeded", runtime_version=1, spec=json.dumps({"qa_state": "passed", "lifecycle": {"review": {"status": "accepted"}}}), created_at=now - timedelta(hours=4), closed_at=now - timedelta(hours=3)))
        else:
            s.add(Deployment(id="dep_life", app_id="app_life", venture_id="ven_life", workspace_id="ws_life", target="supymem_static", state="live", version=1, url="https://fixture.supymem.com", live_at=now - timedelta(hours=3)))
        s.commit()
    _chat(state, now - timedelta(hours=3), kind)
    _run(now)
    _run(now)
    rows = _rows(state, template="milestone")
    assert len(rows) == 1 and rows[0].category == "product_updates"
    context = json.loads(rows[0].context_json)
    assert context["what"] == what and context["path"] == path
    if kind == "research_ready":
        assert context["verdict"] == "Research could not find sources"
        assert "no usable sources" in " ".join(context["items"]).lower()
    if kind == "site_live":
        assert context["url"] == "https://fixture.supymem.com"


def test_failed_build_links_to_its_exact_persisted_app_and_run(lifecycle, monkeypatch):
    from app.db.supymem_models import BuildRun, FounderApp
    state, now = lifecycle
    _only(monkeypatch, "action_failed")
    _venture(state, now)
    with state.db()() as s:
        s.add(FounderApp(id="app_failed", venture_id="ven_life", workspace_id="ws_life", slug="failed-fixture", name="Failed app", kind="landing", state="draft", created_at=now, updated_at=now))
        s.flush()
        s.add(BuildRun(id="bld_failed", app_id="app_failed", venture_id="ven_life", workspace_id="ws_life", trigger="founder", agent="codex", state="failed", runtime_version=1, spec="{}", created_at=now - timedelta(hours=2), closed_at=now - timedelta(hours=1)))
        s.commit()
    _chat(state, now - timedelta(hours=1), "build_failed", actions=[{"href": "/v/ven_life/builds?app=app_failed&run=bld_failed#build-review"}], source_key="build_failed:bld_failed")
    _run(now)
    _run(now)
    rows = _rows(state, template="action_failed")
    assert len(rows) == 1 and rows[0].category == "transactional"
    assert json.loads(rows[0].context_json)["path"] == "/v/ven_life/builds?app=app_failed&run=bld_failed#build-review"


@pytest.mark.parametrize("usable", ["missing", "live", "preview", "foreign"])
def test_landing_failure_email_requires_no_usable_page_and_keeps_exact_route(lifecycle, monkeypatch, usable, caplog):
    from app.db.models import Job
    from app.db.supymem_models import BuildRun, Deployment, FounderApp
    from app.email import providers, service
    from app.pagebuilder import api
    state, now = lifecycle
    _only(monkeypatch, "action_failed")
    _venture(state, now)
    monkeypatch.setattr(api, "_candidate_current", lambda doc: doc.get("tested_ready") is True)
    with state.db()() as s:
        s.add(FounderApp(id="app_landing", venture_id="ven_life", workspace_id="ws_life", slug="landing-mail",
            name="Landing page", kind="landing", state="draft", created_at=now, updated_at=now))
        s.flush()
        s.add(BuildRun(id="bld_landing", app_id="app_landing", venture_id="ven_life", workspace_id="ws_life",
            trigger="pagebuilder_source_qa", agent="managed", state="failed", runtime_version=1,
            spec=json.dumps({"artifact_profile": "static_landing_v1", "qa_state": "failed",
                "source_import": {"identity": {"scope": {"job_id": "landing_candidate"}}}}),
            created_at=now - timedelta(hours=2), closed_at=now - timedelta(hours=1)))
        s.commit()
    path = "/v/ven_life/site?app=app_landing&run=bld_landing#build-review"
    _chat(state, now - timedelta(hours=1), "build_failed", actions=[{"href": path}], source_key="build_failed:bld_landing")
    _run(now)
    queued = _rows(state, template="action_failed")
    assert len(queued) == 1 and json.loads(queued[0].context_json)["path"] == path
    with state.db()() as s:
        if usable in {"live", "foreign"}:
            s.add(Deployment(id="landing_live", app_id="app_landing", venture_id="ven_life",
                workspace_id="ws_life" if usable == "live" else "foreign-workspace", target="supymem_static",
                state="live", version=1, url="https://landing.supymem.com/", live_at=now))
        elif usable == "preview":
            s.add(Job(id="landing_candidate", kind="pagebuilder.build", venture_id="ven_life", workspace_id="ws_life",
                status="succeeded", created_at=now, payload_json=json.dumps({"tested_ready": True, "_runtime": {"state": "ready"}})))
        s.commit()
    seen = []
    monkeypatch.setitem(providers.PROVIDERS, "log", lambda message: seen.append(message) or "landing-mail")
    assert service.deliver(queued[0].id) == ("suppressed" if usable in {"live", "preview"} else "sent")
    assert len(seen) == int(usable in {"missing", "foreign"})
    assert "email_schedule_person_failed" not in caplog.text


def test_superseded_pending_build_is_not_announced_after_a_newer_failure(lifecycle, monkeypatch):
    from app.chat.milestones import _scoped
    from app.db.models import ChatMessage
    from app.db.supymem_models import BuildRun, FounderApp
    state, now = lifecycle
    _only(monkeypatch, "task_finished", "progress_summary")
    _venture(state, now)
    spec = json.dumps({"qa_state": "passed", "lifecycle": {"review": {"status": "pending"}}})
    with state.db()() as s:
        s.add(FounderApp(id="app_pending", venture_id="ven_life", workspace_id="ws_life", slug="pending-fixture", name="Pending app", kind="landing", state="draft", created_at=now, updated_at=now))
        s.flush()
        s.add_all([
            BuildRun(id="bld_pending", app_id="app_pending", venture_id="ven_life", workspace_id="ws_life", trigger="founder", agent="codex", state="succeeded", runtime_version=1, spec=spec, created_at=now - timedelta(hours=5), closed_at=now - timedelta(hours=4)),
            BuildRun(id="bld_latest", app_id="app_pending", venture_id="ven_life", workspace_id="ws_life", trigger="founder", agent="codex", state="failed", runtime_version=1, spec="{}", created_at=now - timedelta(hours=4), closed_at=now - timedelta(hours=3)),
        ])
        s.commit()
    _chat(state, now - timedelta(hours=4), "build_ready")
    with state.db()() as s:
        s.get(ChatMessage, "msg_life").idempotency_key = _scoped("ven_life", "build_ready:bld_pending")
        s.commit()
    assert _run(now)["queued"] == 0
    assert not _rows(state, person_id="usr_a")
    with state.db()() as s:
        latest = s.get(BuildRun, "bld_latest")
        latest.state, latest.spec = "succeeded", spec
        s.commit()
    href = "/v/ven_life/builds?app=app_pending&run=bld_latest#build-review"
    _chat(state, now - timedelta(hours=3), "build_ready", ident="msg_latest", actions=[{"href": href}])
    with state.db()() as s:
        s.get(ChatMessage, "msg_latest").idempotency_key = _scoped("ven_life", "build_ready:bld_latest")
        s.commit()
    assert _run(now)["queued"] == 1
    assert _run(now)["queued"] == 0
    context = json.loads(_rows(state, template="task_finished")[0].context_json)
    assert context["path"] == href and context["source_ids"] == ["chat:msg_latest"]


def test_internal_beta_summary_and_credit_requests_go_only_to_founder(lifecycle, monkeypatch):
    from app.db.models import FeedbackEvent
    from app.db.supymem_models import BetaCode, BetaCodeRedemption
    state, now = lifecycle
    _only(monkeypatch, "signup_summary", "credit_request_admin")
    monkeypatch.setenv("EMAIL_ADMIN_NOTIFY", "unauthorized@example.test")
    with state.db()() as s:
        s.add(BetaCode(code="LIFE2345", owner_person_id="usr_a", label="Fixture", active=True, created_at=now))
        s.flush()
        s.add(BetaCodeRedemption(person_id="usr_a", user_id="usr_a", code="LIFE2345", created_at=now - timedelta(days=1)))
        s.add(FeedbackEvent(id="fb_credit_life", venture_id="ven_life", source="founder", raw_text="PRIVATE CREDIT REQUEST", topic="credit_request", created_at=now - timedelta(hours=1)))
        s.commit()
    _run(now.replace(hour=20))
    _run(now.replace(hour=21))
    summaries = _rows(state, template="signup_summary")
    requests = _rows(state, template="credit_request_admin")
    assert len(summaries) == len(requests) == 1
    assert all(row.to_email == "operator@example.test" for row in summaries + requests)
    assert json.loads(summaries[0].context_json)["count"] == 1
    assert all("PRIVATE" not in row.context_json and "example.test" not in row.context_json for row in summaries + requests)


@pytest.mark.parametrize("desktop,web_signup", [(False, True), (True, False)])
def test_welcome_download_line_only_without_a_linked_device(lifecycle, monkeypatch, desktop, web_signup):
    from app.db.supymem_models import DesktopDevice
    from app.email.triggers import on_welcome
    state, now = lifecycle
    _only(monkeypatch, "welcome")
    _admission(state, now)
    if desktop:
        with state.db()() as s:
            s.add(DesktopDevice(id="dev_welcome", person_id="usr_a", name="Mac", platform="macos", last_seen_at=now))
            s.commit()
    on_welcome("usr_a")
    rows = _rows(state, template="welcome")
    assert len(rows) == 1 and json.loads(rows[0].context_json)["web_signup"] is web_signup
    context = json.loads(rows[0].context_json)
    assert context.get("admission_code") == ("LIFE2345" if web_signup else None)


@pytest.mark.parametrize("admission", ["missing", "inactive", "malformed"])
def test_onboarding_skips_download_without_a_usable_existing_admission(lifecycle, monkeypatch, admission):
    from sqlalchemy import func, select
    from app.db.models import Person
    from app.db.supymem_models import BetaCode, BetaCodeRedemption
    from app.email.triggers import on_welcome
    state, now = lifecycle
    _only(monkeypatch, "welcome", "get_mac_app")
    if admission != "missing":
        _admission(state, now, code="ABCD１２３４" if admission == "malformed" else "LIFE2345", active=admission != "inactive")
    with state.db()() as s:
        s.get(Person, "usr_a").created_at = now - timedelta(days=2)
        s.commit()
    on_welcome("usr_a")
    _run(now)
    context = json.loads(_rows(state, template="welcome")[0].context_json)
    assert context["web_signup"] is False and "admission_code" not in context
    assert not _rows(state, template="get_mac_app")
    with state.db()() as s:
        expected = 0 if admission == "missing" else 1
        assert s.scalar(select(func.count()).select_from(BetaCode)) == expected
        assert s.scalar(select(func.count()).select_from(BetaCodeRedemption)) == expected


def test_late_committed_gmail_receipt_is_not_lost_or_double_counted(lifecycle, monkeypatch):
    from app.db.models import GtmContact, GtmTouch
    state, now = lifecycle
    _only(monkeypatch, "outreach_sent")
    _venture(state, now)
    with state.db()() as s:
        s.add(GtmContact(id="gtmc_late", workspace_id="ws_life", stage_changed_at=now, created_at=now))
        s.flush()
        s.add(GtmTouch(id="touch_initial", workspace_id="ws_life", venture_id="ven_life", contact_id="gtmc_late", direction="out", channel="email", send_mode="founder_gmail", simulated=0, message_id="receipt_initial", provider_thread_id="thread_late", sent_at=now - timedelta(hours=2), created_at=now - timedelta(hours=2)))
        s.commit()
    _run(now)
    with state.db()() as s:
        s.add(GtmTouch(id="touch_late", workspace_id="ws_life", venture_id="ven_life", contact_id="gtmc_late", direction="out", channel="email", send_mode="founder_gmail", simulated=0, message_id="receipt_late", provider_thread_id="thread_late", sent_at=now - timedelta(hours=2), created_at=now))
        s.commit()
    _run(now)
    _run(now)
    rows = _rows(state, template="outreach_sent")
    assert len(rows) == 2
    contexts = [json.loads(row.context_json) for row in rows]
    assert sum(ctx["count"] for ctx in contexts) == 2
    assert all(ctx["path"] == "/v/ven_life" for ctx in contexts)
    assert {source for ctx in contexts for source in ctx["source_ids"]} == {"touch:touch_initial", "touch:touch_late"}

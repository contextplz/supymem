"""Queued email must remain relevant and owned before crossing the provider boundary."""
import asyncio
import json
from datetime import timedelta

import pytest

from tests.unit.email.test_lifecycle_schedule import env as env, lifecycle as lifecycle  # noqa: F401
from tests.unit.email.test_lifecycle_schedule import _admission, _chat, _only, _run, _venture
from tests.unit.test_email_engine import _needs_context, _rows


def _draft(state, now, ident, *, batch="batch_current", status="draft", channel="email"):
    from app.db.models import VentureDocument
    with state.db()() as s:
        s.add(VentureDocument(id=ident, venture_id="ven_life", kind="outreach_draft", title="PRIVATE PROSPECT", body_md="PRIVATE DRAFT", provenance_json=json.dumps({"batch_id": batch, "state": status, "channel": channel, "requires_batch_approval": True}), created_at=now - timedelta(hours=3), updated_at=now))
        s.commit()


def _record_provider(monkeypatch):
    from app.email import providers
    seen = []
    monkeypatch.setitem(providers.PROVIDERS, "log", lambda message: seen.append(message) or "log_relevance")
    return seen


def _saved_card(state, now, key, kind, actions, *, email_reason=None):
    from sqlalchemy import select
    from app.chat.milestones import _scoped, post_milestone
    from app.db.models import ChatMessage
    assert post_milestone("ven_life", key=key, kind=kind, body_md="Review the saved step.",
        actions=actions, email_reason=email_reason)
    with state.db()() as s:
        card = s.scalar(select(ChatMessage).where(ChatMessage.idempotency_key == _scoped("ven_life", key)))
        card.created_at = now - timedelta(hours=1)
        s.commit()
        return card.id


def _failure_email(state, now, source):
    """Use real persisted failures and the normal milestone/scheduler path."""
    from app.core.jobs import enqueue
    from app.db.models import Job
    from app.db.supymem_models import BuildRun, Deployment, FounderApp
    if source == "job":
        with state.db()() as s:
            ident = enqueue(s, "research.deep", {}, venture_id="ven_life", workspace_id="ws_life", dedupe_key="relevance-failed-research")
            s.flush()
            row = s.get(Job, ident)
            row.status, row.attempts, row.created_at = "failed_dead", 1, now - timedelta(hours=2)
            s.commit()
        key, kind, path = f"research_failed:{ident}", "research_failed", "/v/ven_life/research"
        actions = [{"action": "job.retry", "payload": {"kind": "research.deep"}}]
    else:
        ident = "bld_relevance" if source == "build" else "dep_relevance"
        with state.db()() as s:
            s.add(FounderApp(id="app_relevance", venture_id="ven_life", workspace_id="ws_life", slug="relevance", name="Saved app", kind="landing", state="draft", created_at=now, updated_at=now))
            s.flush()
            if source == "build":
                s.add(BuildRun(id=ident, app_id="app_relevance", venture_id="ven_life", workspace_id="ws_life", trigger="founder", agent="codex", state="failed", runtime_version=1, spec="{}", created_at=now - timedelta(hours=2), closed_at=now - timedelta(hours=1)))
            else:
                s.add(Deployment(id=ident, app_id="app_relevance", venture_id="ven_life", workspace_id="ws_life", target="supymem_static", state="failed", version=1))
            s.commit()
        key, kind, path = ((f"build_failed:{ident}", "build_failed", "/v/ven_life/builds?app=app_relevance&run=bld_relevance#build-review")
            if source == "build" else (f"deploy_failed:{ident}", "site_publish_failed", "/v/ven_life/site"))
        actions = [{"href": path}]
    card_id = _saved_card(state, now, key, kind, actions)
    _run(now)
    message = _rows(state, template="action_failed")[0]
    assert json.loads(message.context_json)["path"] == path
    return message.id, ident, card_id


@pytest.mark.parametrize("template", ["welcome", "beta_invite", "needs_you", "god_mode_requested", "action_failed", "credits_low", "credits_exhausted", "problem_received", "problem_resolved", "outreach_sent", "error_alert", "god_mode_admin", "signup_summary", "credit_request_admin", "email_health_alert"])
def test_global_quiet_keeps_transactional_receipts_and_internal_alerts_queued(lifecycle, monkeypatch, template):
    from sqlalchemy import select
    from app.core import jobs
    from app.db.models import AnalyticsEvent, CreditAccount, GtmContact, GtmTouch, Job, Person
    from app.email import service, triggers
    from app.email.fixtures import FIXTURES
    from app.worker.handlers import install_default_handlers
    from app.worker.main import _run_one
    state, now = lifecycle
    _only(monkeypatch, template)
    _venture(state, now)
    clock = [now]
    for module in (service, triggers, jobs):
        monkeypatch.setattr(module, "utcnow", lambda: clock[0])
    internal = template in service.INTERNAL_TEMPLATES
    person = "admin:founder" if internal else "invite:existing" if template == "beta_invite" else "usr_a"
    context = {**FIXTURES[template]}
    if "path" in context and context["path"].startswith("/v/"):
        context["path"] = context["path"].replace("ven_demo", "ven_life")
    with state.db()() as session:
        if internal:
            session.get(Person, "usr_a").email = "operator@example.test"
        if template.startswith("credits_"):
            session.add(CreditAccount(id="credit_quiet", venture_id="ven_life", workspace_id="ws_life", owner_person_id="usr_a", granted_micro=100, spent_micro=100, created_at=now, updated_at=now))
        if template == "outreach_sent":
            session.add(GtmContact(id="contact_quiet", workspace_id="ws_life", created_at=now, stage_changed_at=now))
            session.flush()
            session.add(GtmTouch(id="touch_quiet", venture_id="ven_life", workspace_id="ws_life", contact_id="contact_quiet", direction="out", channel="email", simulated=0, send_mode="founder_gmail", message_id="provider_quiet", sent_at=now - timedelta(hours=2), created_at=now))
            context["source_ids"] = ["touch:touch_quiet"]
        session.add(AnalyticsEvent(id="activity_quiet", person_id="usr_a", event_name="session.active", occurred_at=now, props_json="{}"))
        session.commit()
    address = "operator@example.test" if internal else "a@example.test"
    if template == "needs_you":
        context = _needs_context(state, context)
    mid = (_failure_email(state, now, "job")[0] if template == "action_failed"
        else service.send_email(person, template, context, to_email=address, ref="quiet"))
    assert _rows(state, id=mid)[0].status == "queued"
    seen = _record_provider(monkeypatch)
    install_default_handlers()
    def work():
        with state.db()() as session:
            claimed = jobs.lease(session, "owned-quiet-worker", kinds=["email.send"], n=10, now=clock[0])
            session.commit()
        for job in claimed:
            asyncio.run(_run_one(state.db(), job, {job.id}))
        return claimed
    assert len(work()) == 1 and not seen
    row = _rows(state, id=mid)[0]
    if template == "credits_low":
        assert row.status == "suppressed" and row.error == "source_no_longer_eligible"
        assert work() == [] and not seen
        return
    assert row.status == "queued" and row.error == "quiet_after_activity"
    assert "_email_first_attempt_at" not in json.loads(row.context_json)
    with state.db()() as session:
        future = session.scalar(select(Job).where(Job.dedupe_key.startswith("email.send:" + mid + ":defer:")))
        assert future.run_after == now + timedelta(hours=1, seconds=1) and future.attempts == 0
    assert work() == [] and not seen
    clock[0] = future.run_after
    assert len(work()) == 1 and len(seen) == 1
    assert service.deliver(mid) == "sent" and work() == [] and len(seen) == 1


def test_pseudo_internal_recipient_quiet_includes_owned_workspace_activity(lifecycle, monkeypatch):
    from app.db.models import AnalyticsEvent, Person
    from app.email import service
    from app.email.fixtures import FIXTURES
    state, now = lifecycle
    _only(monkeypatch, "email_health_alert")
    _venture(state, now)
    with state.db()() as session:
        session.get(Person, "usr_a").email = "operator@example.test"
        session.add(AnalyticsEvent(id="workspace_quiet", workspace_id="ws_life", event_name="ui.screen_view", occurred_at=now, props_json="{}"))
        session.commit()
    mid = service.send_email("admin:founder", "email_health_alert", FIXTURES["email_health_alert"], to_email="operator@example.test")
    seen = _record_provider(monkeypatch)
    assert service.deliver(mid) == "deferred" and not seen


def _freeze_if_requested(monkeypatch, mid, frozen):
    from app.core.jobs import RetryableError
    from app.email import providers, service
    seen = []
    def provider(message):
        seen.append(message)
        if frozen and len(seen) == 1:
            raise providers.ProviderError("resend_http_503", True)
        return "log_source_relevance"
    monkeypatch.setitem(providers.PROVIDERS, "log", provider)
    if frozen:
        with pytest.raises(RetryableError):
            service.deliver(mid)
    return seen


@pytest.mark.parametrize("frozen", [False, True])
@pytest.mark.parametrize("change", ["pending", "resolved", "expired", "deadline", "deleted", "foreign"])
def test_delayed_request_reminder_requires_the_same_open_request(lifecycle, monkeypatch, frozen, change):
    from app.db.models import Workspace
    from app.db.supymem_models import NeedsYouRequest
    from app.email import service, triggers
    state, now = lifecycle
    _only(monkeypatch, "needs_you")
    _venture(state, now)
    with state.db()() as s:
        s.add(NeedsYouRequest(id="nyr_relevance", venture_id="ven_life", workspace_id="ws_life", reason_code="captcha", title="Complete the verification", blocking_ref="pending-verification", status="open", created_at=now))
        s.commit()
    triggers.on_needs_you("ven_life", "Complete the verification", "A CAPTCHA needs you.", "nyr_relevance:remind:1")
    row = _rows(state, template="needs_you")[0]
    assert json.loads(row.context_json)["_needs_request_id"] == "nyr_relevance"
    seen = _freeze_if_requested(monkeypatch, row.id, frozen)
    with state.db()() as s:
        request = s.get(NeedsYouRequest, "nyr_relevance")
        if change == "deleted":
            s.delete(request)
        elif change == "deadline":
            request.deadline = now
        elif change == "foreign":
            s.add(Workspace(id="ws_foreign", owner_id="usr_b", name="Other", kind="venture", entry_mode="idea", created_at=now, updated_at=now))
            s.flush()
            request.workspace_id = "ws_foreign"
        elif change != "pending":
            request.status = change
        s.commit()
    assert service.deliver(row.id, attempt=2 if frozen else 1) == ("sent" if change == "pending" else "suppressed")
    assert len(seen) == int(frozen) + int(change == "pending")
    if frozen and change == "pending":
        assert seen[0].html == seen[1].html and seen[0].text == seen[1].text


@pytest.mark.parametrize("frozen", [False, True])
@pytest.mark.parametrize("change", ["pending", "resolved", "deleted", "private_other"])
def test_delayed_human_card_requires_the_same_visible_pending_card(lifecycle, monkeypatch, frozen, change):
    from app.db.models import ChatConversation, ChatMessage
    from app.email import service
    state, now = lifecycle
    _only(monkeypatch, "needs_you")
    _venture(state, now)
    card_id = _saved_card(state, now, "human-consent:prepare", "needs_you", [{"href": "/v/ven_life/now"}], email_reason="consent_send")
    row = _rows(state, template="needs_you")[0]
    assert json.loads(row.context_json)["_needs_card_id"] == card_id
    seen = _freeze_if_requested(monkeypatch, row.id, frozen)
    with state.db()() as s:
        card = s.get(ChatMessage, card_id)
        if change == "deleted":
            s.delete(card)
        elif change == "resolved":
            card.kind = "needs_you_resolved"
        elif change == "private_other":
            s.add(ChatConversation(id="ccv_other", venture_id="ven_life", workspace_id="ws_life", creator_person_id="usr_b", visibility="private", title="Private", creation_key="private-relevance", created_at=now, updated_at=now))
            s.flush()
            card.conversation_id = "ccv_other"
        s.commit()
    assert service.deliver(row.id, attempt=2 if frozen else 1) == ("sent" if change == "pending" else "suppressed")
    assert len(seen) == int(frozen) + int(change == "pending")


def test_request_milestone_stamps_its_saved_card_and_does_not_email_private_other_content(lifecycle, monkeypatch):
    import hashlib
    from app.db.models import ChatConversation, ChatMessage
    from app.db.supymem_models import NeedsYouRequest
    from app.email import triggers
    state, now = lifecycle
    _only(monkeypatch, "needs_you")
    _venture(state, now)
    with state.db()() as s:
        s.add(NeedsYouRequest(id="nyr_relevance", venture_id="ven_life", workspace_id="ws_life", reason_code="captcha", title="Complete the verification", blocking_ref="pending-verification", status="open", created_at=now))
        s.add(ChatConversation(id="ccv_other", venture_id="ven_life", workspace_id="ws_life", creator_person_id="usr_b", visibility="private", title="Private", creation_key="private-relevance", created_at=now, updated_at=now))
        s.flush()
        digest = hashlib.sha256(json.dumps(["ven_life", "assistant", "needs_you:nyr_relevance", "ccv_other"], separators=(",", ":")).encode()).hexdigest()
        s.add(ChatMessage(id="msg_private_request", venture_id="ven_life", workspace_id="ws_life", role="assistant", kind="needs_you", conversation_id="ccv_other", idempotency_key="chat:" + digest, body_md="PRIVATE OTHER CONTENT", created_at=now))
        s.commit()
    triggers.on_milestone("ven_life", "needs_you", "needs_you:nyr_relevance", "PRIVATE OTHER CONTENT", human_reason="captcha")
    assert not _rows(state, template="needs_you")
    _chat(state, now, "needs_you", ident="msg_shared_request", source_key="needs_you:nyr_relevance", actions=[{"action": "needs_you.resolve", "payload": {"request_id": "nyr_relevance"}}])
    # Even with a shared source using the same event key, caller text cannot copy another conversation.
    triggers.on_milestone("ven_life", "needs_you", "needs_you:nyr_relevance", "PRIVATE OTHER CONTENT", human_reason="captcha")
    ctx = json.loads(_rows(state, template="needs_you")[0].context_json)
    assert ctx["_needs_card_id"] == "msg_shared_request" and ctx["_needs_request_id"] == "nyr_relevance"
    assert ctx["detail"] == "Your saved work is ready." and "PRIVATE" not in json.dumps(ctx)


@pytest.mark.parametrize("frozen", [False, True])
@pytest.mark.parametrize("source", ["job", "build", "deployment"])
@pytest.mark.parametrize("change", ["pending", "running", "succeeded", "superseded", "deleted", "no_action"])
def test_delayed_failure_receipt_requires_current_exact_retry_source(lifecycle, monkeypatch, frozen, source, change):
    from app.core.jobs import enqueue
    from app.db.models import ChatMessage, Job
    from app.db.supymem_models import BuildRun, Deployment
    from app.email import service
    state, now = lifecycle
    _only(monkeypatch, "action_failed")
    _venture(state, now)
    mid, ident, card_id = _failure_email(state, now, source)
    seen = _freeze_if_requested(monkeypatch, mid, frozen)
    model = {"job": Job, "build": BuildRun, "deployment": Deployment}[source]
    with state.db()() as s:
        row = s.get(model, ident)
        if change == "deleted":
            s.delete(row)
        elif change == "no_action":
            s.get(ChatMessage, card_id).actions_json = "[]"
        elif change == "superseded":
            if source == "job":
                latest_id = enqueue(s, "research.deep", {}, venture_id="ven_life", workspace_id="ws_life", dedupe_key="newer-relevance-research")
                s.flush()
                s.get(Job, latest_id).created_at = now
            elif source == "build":
                s.add(BuildRun(id="bld_newer", app_id="app_relevance", venture_id="ven_life", workspace_id="ws_life", trigger="founder", agent="codex", state="running", runtime_version=1, spec="{}", created_at=now))
            else:
                s.add(Deployment(id="dep_newer", app_id="app_relevance", venture_id="ven_life", workspace_id="ws_life", target="supymem_static", state="pending", version=2))
        elif change in {"running", "succeeded"}:
            if source == "job":
                row.status = change
            else:
                row.state = "live" if source == "deployment" and change == "succeeded" else change
        s.commit()
    assert service.deliver(mid, attempt=2 if frozen else 1) == ("sent" if change == "pending" else "suppressed")
    assert len(seen) == int(frozen) + int(change == "pending")
    if frozen and change == "pending":
        assert seen[0].html == seen[1].html and seen[0].text == seen[1].text


@pytest.mark.parametrize("template", ["needs_you", "action_failed"])
def test_legacy_source_free_action_notices_fail_closed_at_delivery(lifecycle, monkeypatch, template):
    from app.email import service
    state, now = lifecycle
    _only(monkeypatch, template)
    _venture(state, now)
    mid = service.send_email("usr_a", template, {"path": "/v/ven_life/now", "detail": "Old notice without attributable evidence"}, ref="legacy")
    assert _rows(state, id=mid)[0].status == "queued"
    seen = _record_provider(monkeypatch)
    assert service.deliver(mid) == "suppressed" and not seen


@pytest.mark.parametrize("frozen", [False, True])
def test_same_failed_job_after_retry_does_not_revive_an_old_receipt(lifecycle, monkeypatch, frozen):
    from app.db.models import Job
    from app.email import service
    state, now = lifecycle
    _only(monkeypatch, "action_failed")
    _venture(state, now)
    mid, ident, _ = _failure_email(state, now, "job")
    seen = _freeze_if_requested(monkeypatch, mid, frozen)
    with state.db()() as s:
        s.get(Job, ident).attempts += 1
        s.commit()
    assert service.deliver(mid, attempt=2 if frozen else 1) == "suppressed"
    assert len(seen) == int(frozen)


@pytest.mark.parametrize("template", ["error_alert", "god_mode_admin", "signup_summary", "credit_request_admin", "email_health_alert"])
def test_internal_alerts_admit_only_the_fixed_founder_address(lifecycle, monkeypatch, template):
    from app.email import service
    from app.email.fixtures import FIXTURES
    state, _ = lifecycle
    _only(monkeypatch, template)
    seen = _record_provider(monkeypatch)
    refused = service.send_email("admin:alternate", template, FIXTURES[template], to_email="alternate@example.test")
    refused_row = _rows(state, id=refused)[0]
    assert refused_row.status == "suppressed" and refused_row.error == "internal_recipient_disallowed"
    assert service.deliver(refused) == "suppressed" and not seen
    admitted = service.send_email("admin:founder", template, FIXTURES[template], to_email=" OPERATOR@EXAMPLE.TEST ")
    assert service.deliver(admitted) == "sent" and len(seen) == 1
    assert seen[0].to == "operator@example.test"


@pytest.mark.parametrize("template", ["error_alert", "god_mode_admin", "signup_summary", "credit_request_admin", "email_health_alert"])
def test_legacy_queued_internal_alert_to_another_address_is_suppressed(lifecycle, monkeypatch, template):
    from app.db.supymem_models import EmailMessage
    from app.email import service
    from app.email.fixtures import FIXTURES
    state, _ = lifecycle
    _only(monkeypatch, template)
    mid = service.send_email("admin:founder", template, FIXTURES[template], to_email="operator@example.test")
    with state.db()() as session:
        session.get(EmailMessage, mid).to_email = "alternate@example.test"
        session.commit()
    seen = _record_provider(monkeypatch)
    assert service.deliver(mid) == "suppressed" and not seen
    row = _rows(state, id=mid)[0]
    assert row.error == "internal_recipient_disallowed" and row.sent_at is None and row.provider_id is None


@pytest.mark.parametrize("change", ["transfer", "archive"])
def test_queued_venture_progress_cancels_when_ownership_is_lost(lifecycle, monkeypatch, change):
    from app.db.models import Workspace
    from app.email import service
    state, now = lifecycle
    _only(monkeypatch, "milestone")
    _venture(state, now)
    _chat(state, now - timedelta(hours=3), "plan_ready")
    _run(now)
    row = _rows(state, template="milestone")[0]
    with state.db()() as s:
        workspace = s.get(Workspace, "ws_life")
        if change == "transfer":
            workspace.owner_id = "usr_b"
        else:
            workspace.archived_at = now
        s.commit()
    seen = _record_provider(monkeypatch)
    assert service.deliver(row.id) == "suppressed" and not seen


@pytest.mark.parametrize("final_state", ["sent", "skipped"])
def test_exact_draft_notification_cancels_after_batch_is_handled(lifecycle, monkeypatch, final_state):
    from app.db.models import VentureDocument
    from app.email import service
    state, now = lifecycle
    _only(monkeypatch, "outreach_drafts_ready")
    _venture(state, now)
    _draft(state, now, "draft_current")
    _run(now)
    row = _rows(state, template="outreach_drafts_ready")[0]
    with state.db()() as s:
        document = s.get(VentureDocument, "draft_current")
        data = json.loads(document.provenance_json)
        data["state"] = final_state
        document.provenance_json = json.dumps(data)
        s.commit()
    # A different pending batch must never revive the handled notification.
    _draft(state, now, "draft_other", batch="batch_other")
    seen = _record_provider(monkeypatch)
    assert service.deliver(row.id) == "suppressed" and not seen


def test_draft_delivery_refreshes_only_current_pending_exact_batch(lifecycle, monkeypatch):
    from app.db.models import VentureDocument
    from app.email import service
    state, now = lifecycle
    _only(monkeypatch, "outreach_drafts_ready")
    _venture(state, now)
    for ident in ("draft_pending", "draft_sent", "draft_skipped"):
        _draft(state, now, ident)
    _run(now)
    row = _rows(state, template="outreach_drafts_ready")[0]
    with state.db()() as s:
        for ident, status in (("draft_sent", "sent"), ("draft_skipped", "skipped")):
            document = s.get(VentureDocument, ident)
            data = json.loads(document.provenance_json)
            data["state"] = status
            document.provenance_json = json.dumps(data)
        s.commit()
    _draft(state, now, "draft_other", batch="batch_other")
    _draft(state, now, "draft_linkedin", channel="linkedin")
    seen = _record_provider(monkeypatch)
    assert service.deliver(row.id) == "sent" and len(seen) == 1
    context = json.loads(_rows(state, id=row.id)[0].context_json)
    assert context["count"] == 1 and context["_draft_ids"] == ["draft_pending"]
    assert "PRIVATE" not in seen[0].text


@pytest.mark.parametrize("change", ["rename", "handled"])
def test_frozen_draft_retry_preserves_copy_and_cancels_invalid_sources(lifecycle, monkeypatch, change):
    from app.core.jobs import RetryableError
    from app.db.models import Venture, VentureDocument
    from app.email import providers, service
    state, now = lifecycle
    _only(monkeypatch, "outreach_drafts_ready")
    _venture(state, now)
    _draft(state, now, "draft_current")
    _run(now)
    row = _rows(state, template="outreach_drafts_ready")[0]
    seen = []
    def provider(message):
        seen.append(message)
        if len(seen) == 1:
            raise providers.ProviderError("resend_http_503", True)
        return "log_retried"
    monkeypatch.setitem(providers.PROVIDERS, "log", provider)
    with pytest.raises(RetryableError):
        service.deliver(row.id)
    with state.db()() as s:
        if change == "rename":
            s.get(Venture, "ven_life").name = "Renamed after preparation"
        else:
            document = s.get(VentureDocument, "draft_current")
            data = json.loads(document.provenance_json)
            data["state"] = "sent"
            document.provenance_json = json.dumps(data)
        s.commit()
    assert service.deliver(row.id, attempt=2) == ("sent" if change == "rename" else "suppressed")
    if change == "rename":
        assert len(seen) == 2 and seen[0].html == seen[1].html and seen[0].text == seen[1].text
    else:
        assert len(seen) == 1


@pytest.mark.parametrize("frozen", [False, True])
@pytest.mark.parametrize("change", ["transfer", "archive_venture"])
def test_weekly_refresh_protects_secondary_workspace_content(lifecycle, monkeypatch, frozen, change):
    from app.core.jobs import RetryableError
    from app.db.models import ChatMessage, Venture, Workspace, WorkspaceMember
    from app.email import providers, service
    state, now = lifecycle
    _only(monkeypatch, "weekly_insights")
    _venture(state, now)
    with state.db()() as s:
        s.add(Workspace(id="ws_current", kind="venture", name="Current", entry_mode="idea", owner_id="usr_a", created_at=now, updated_at=now))
        s.flush()
        s.add(WorkspaceMember(id="wm_current_owner", workspace_id="ws_current", person_id="usr_a",
            role="owner", can_approve=1, created_at=now))
        s.add(Venture(id="ven_current", workspace_id="ws_current", name="Current", raw_idea="A real idea", stage="idea", created_at=now, updated_at=now))
        s.flush()
        for ident, vid, wid, text in (("msg_former", "ven_life", "ws_life", "PRIVATE FORMER RESULT"), ("msg_current", "ven_current", "ws_current", "Current owned result")):
            s.add(ChatMessage(id=ident, venture_id=vid, workspace_id=wid, role="assistant", kind="plan_ready", body_md=text, created_at=now - timedelta(hours=3)))
        s.commit()
    monday = now.replace(hour=20) + timedelta(days=(0 - now.weekday()) % 7)
    _run(monday)
    row = _rows(state, template="weekly_insights")[0]
    seen = []
    def provider(message):
        seen.append(message)
        if frozen:
            raise providers.ProviderError("resend_http_503", True)
        return "log_weekly"
    monkeypatch.setitem(providers.PROVIDERS, "log", provider)
    if frozen:
        with pytest.raises(RetryableError):
            service.deliver(row.id)
    with state.db()() as s:
        if change == "transfer":
            s.get(Workspace, "ws_life").owner_id = "usr_b"
        else:
            s.get(Venture, "ven_life").archived_at = now
        s.commit()
    assert service.deliver(row.id, attempt=2 if frozen else 1) == ("suppressed" if frozen else "sent")
    if frozen:
        assert len(seen) == 1
    else:
        assert len(seen) == 1 and "PRIVATE FORMER" not in seen[0].text and "Current owned result" in seen[0].text


def test_weekly_gtm_only_activity_counts_real_scoped_receipts_and_review_drafts(lifecycle, monkeypatch):
    from app.db.models import GtmContact, GtmTouch
    from app.email import service
    state, now = lifecycle
    _only(monkeypatch, "weekly_insights")
    _venture(state, now)
    _draft(state, now, "draft_current")
    _draft(state, now, "draft_sent", status="sent")
    _draft(state, now, "draft_linkedin", channel="linkedin")
    with state.db()() as s:
        s.add(GtmContact(id="contact_weekly", workspace_id="ws_life", email="private-prospect@example.test", stage_changed_at=now, created_at=now))
        s.flush()
        for ident, direction, simulated, wid, message, thread in (("sent", "out", 0, "ws_life", "sent-receipt", None), ("sent_duplicate", "out", 0, "ws_life", "sent-receipt", None), ("reply", "in", 0, "ws_life", "reply-receipt", "owned-thread"), ("simulated", "out", 1, "ws_life", "fake", None), ("wrong_scope", "out", 0, "ws_other", "wrong", None), ("empty", "out", 0, "ws_life", "", None), ("no_thread", "in", 0, "ws_life", "unattributed", None)):
            s.add(GtmTouch(id=ident, venture_id="ven_life", workspace_id=wid, contact_id="contact_weekly", direction=direction, channel="email", simulated=simulated, send_mode="founder_gmail", message_id=message, provider_thread_id=thread, sent_at=now - timedelta(hours=2), created_at=now - timedelta(hours=2), body_text="PRIVATE REPLY"))
        s.commit()
    monday = now.replace(hour=20) + timedelta(days=(0 - now.weekday()) % 7)
    _run(monday)
    row = _rows(state, template="weekly_insights")[0]
    context = json.loads(row.context_json)
    assert any("1 outreach drafts await" in item for item in context["items"])
    assert any("1 approved outreach messages accepted by Gmail" in item for item in context["items"])
    assert any("1 replies received" in item for item in context["items"])
    seen = _record_provider(monkeypatch)
    assert service.deliver(row.id) == "sent" and len(seen) == 1
    assert "PRIVATE" not in seen[0].text and "example.test" not in seen[0].text


def test_duplicate_gmail_receipt_does_not_claim_a_second_sent_message(lifecycle, monkeypatch):
    from app.db.models import GtmContact, GtmTouch
    state, now = lifecycle
    _only(monkeypatch, "outreach_sent")
    _venture(state, now)
    with state.db()() as s:
        s.add(GtmContact(id="contact_duplicate", workspace_id="ws_life", stage_changed_at=now, created_at=now))
        s.flush()
        for ident in ("receipt_first", "receipt_duplicate"):
            s.add(GtmTouch(id=ident, venture_id="ven_life", workspace_id="ws_life", contact_id="contact_duplicate", direction="out", channel="email", simulated=0, send_mode="founder_gmail", message_id="same-provider-message", sent_at=now - timedelta(hours=2), created_at=now))
        s.commit()
    _run(now)
    row = _rows(state, template="outreach_sent")[0]
    assert json.loads(row.context_json)["count"] == 1
    with state.db()() as s:
        s.add(GtmTouch(id="receipt_late_duplicate", venture_id="ven_life", workspace_id="ws_life", contact_id="contact_duplicate", direction="out", channel="email", simulated=0, send_mode="founder_gmail", message_id="same-provider-message", sent_at=now - timedelta(hours=2), created_at=now))
        s.commit()
    _run(now)
    assert len(_rows(state, template="outreach_sent")) == 1


@pytest.mark.parametrize("template", ["finish_setup", "get_mac_app"])
def test_prepared_onboarding_reminder_cancels_after_completion(lifecycle, monkeypatch, template):
    from app.db.models import Person
    from app.db.supymem_models import DesktopDevice
    from app.email import service
    state, now = lifecycle
    _only(monkeypatch, template)
    _admission(state, now)
    with state.db()() as s:
        s.get(Person, "usr_a").created_at = now - timedelta(days=2)
        s.commit()
    _run(now)
    row = _rows(state, template=template)[0]
    if template == "finish_setup":
        _venture(state, now)
    else:
        with state.db()() as s:
            s.add(DesktopDevice(id="device_new", person_id="usr_a", name="Mac", platform="macos", last_seen_at=now))
            s.commit()
    seen = _record_provider(monkeypatch)
    assert service.deliver(row.id) == "suppressed" and not seen


def test_prepared_nudge_does_not_arrive_after_its_checkpoint_window(lifecycle, monkeypatch):
    from app.db.models import Person
    from app.email import service, triggers
    state, now = lifecycle
    _only(monkeypatch, "nudge_d5")
    with state.db()() as s:
        s.get(Person, "usr_a").created_at = now - timedelta(days=5)
        s.commit()
    _run(now)
    row = _rows(state, template="nudge_d5")[0]
    later = now + timedelta(days=10)
    monkeypatch.setattr(service, "utcnow", lambda: later)
    monkeypatch.setattr(triggers, "utcnow", lambda: later)
    seen = _record_provider(monkeypatch)
    assert service.deliver(row.id) == "suppressed" and not seen

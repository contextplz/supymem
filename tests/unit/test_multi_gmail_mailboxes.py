from datetime import timedelta
from contextlib import contextmanager
import json
from types import SimpleNamespace

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture(autouse=True)
def hosted_mailboxes(monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)


@pytest.fixture(params=["industrial-pumps.example", "home-gardening.example"], ids=["b2b", "consumer"])
def local_email(mailbox_db, monkeypatch, request, tmp_path):
    from secrets import token_urlsafe

    domain = request.param
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / "local-mail"))
    from app.core.local_secrets import update_settings
    update_settings("smtp", {"host": f"mail.{domain}", "port": 587, "user": f"you@{domain}",
        "sender_name": "Alex Rowan", "postal_address": "16 Cedar Road, Toronto, Canada",
        "imap_host": f"mail.{domain}", "imap_port": 993}, {"password": token_urlsafe(24)})
    update_settings("capabilities", {"send_email": True, "read_replies": True}, {})
    from app.credits import gate, metered
    monkeypatch.setattr(metered, "charge_sync", gate.charge_sync)
    monkeypatch.delenv("SUPYMEM_PUBLIC_URL", raising=False)
    monkeypatch.delenv("COMPOSIO_API_KEY", raising=False)
    return *mailbox_db, domain


def test_local_mailbox_is_owned_and_hosted_mode_ignores_settings(local_email, monkeypatch):
    from app.db.models import GtmEmailAccount
    from app.gtm import local_mail

    db, mailboxes, workspace_id, domain = local_email
    with db()() as session:
        account = local_mail.ensure_account(session, workspace_id)
        session.commit()
        assert account.email == f"you@{domain}"
        assert mailboxes.resolve_send_mailbox(session, workspace_id)["mailbox_id"] == account.id
        assert [ref["mailbox_id"] for ref in mailboxes.list_read_mailboxes(session, workspace_id)] == [account.id]
        other = session.get(GtmEmailAccount, "foreign").workspace_id
        with pytest.raises(mailboxes.MailboxUnavailable):
            local_mail.ensure_account(session, other)
        with pytest.raises(mailboxes.MailboxUnavailable):
            mailboxes.resolve_send_mailbox(session, workspace_id, mailbox_id="foreign")
        monkeypatch.delenv("SUPYMEM_MODE")
        assert local_mail.ensure_account(session, workspace_id) is None
        assert {ref["mailbox_id"] for ref in mailboxes.list_read_mailboxes(session, workspace_id)} == {"tools", "recipes"}


def test_local_reused_mailbox_restores_owned_send_binding(local_email):
    from app.db.models import GtmEmailAccount
    from app.gtm import local_mail
    from app.gtm.supymem import composio_gmail
    from core.clock import utcnow

    factory, mailboxes, workspace_id, domain = local_email
    with factory()() as session:
        session.add(GtmEmailAccount(id="local-existing", workspace_id=workspace_id, provider="smtp",
            email=f"you@{domain}", composio_user_id="previous-user", app_user_id="previous-user",
            connected_account_id="local-existing-account", status="active", created_at=utcnow(), updated_at=utcnow()))
        session.commit()
        mailbox = mailboxes.resolve_send_mailbox(session, workspace_id)
        session.commit()
        assert mailbox["mailbox_id"] == "local-existing"
        assert mailbox["composio_user_id"] == composio_gmail.user_id_for(workspace_id)
        assert session.get(GtmEmailAccount, mailbox["mailbox_id"]).app_user_id == mailbox["composio_user_id"]
        assert session.query(GtmEmailAccount).filter_by(provider="smtp").count() == 1
    local_mail.check_binding(mailbox["composio_user_id"], mailbox["connected_account_id"])
    with pytest.raises(composio_gmail.DefiniteFailure):
        local_mail.check_binding("previous-user", mailbox["connected_account_id"])


@pytest.mark.parametrize("multi", [False, True])
def test_fresh_local_smtp_verification_persists_native_binding(local_email, monkeypatch, multi):
    from sqlalchemy import select
    from app.db.models import GtmEmailAccount
    from app.gtm import local_mail
    from app.gtm.supymem import pipeline

    factory, mailboxes, workspace_id, domain = local_email
    monkeypatch.setattr(mailboxes, "enabled", lambda: multi)
    verified = []

    @contextmanager
    def smtp(config):
        verified.append(config.username)
        yield SimpleNamespace(send_message=lambda *_args, **_kwargs: pytest.fail("Verification must not send email"))

    monkeypatch.setattr(local_mail, "_smtp", smtp)
    result = pipeline.verify_gmail_connection(workspace_id)
    assert result["ok"] is True and result["status"] == "verified"
    assert result["email"] == f"you@{domain}" and result["log_id"] is None
    assert verified == [f"you@{domain}"]
    with factory()() as session:
        account = session.scalar(select(GtmEmailAccount).where(GtmEmailAccount.provider == "smtp"))
        assert account is not None and account.connected_account_id == result["account_id"]
        assert account.workspace_id == workspace_id


@pytest.mark.parametrize("multi", [False, True])
def test_fresh_local_outreach_mailbox_persists_binding(local_email, monkeypatch, multi):
    from app.autopilot import outreach
    from app.db.models import GtmEmailAccount
    from app.gtm import local_mail

    factory, mailboxes, workspace_id, domain = local_email
    monkeypatch.setattr(mailboxes, "enabled", lambda: multi)
    sender = outreach._mailbox(workspace_id)
    assert sender["email"] == f"you@{domain}"
    with factory()() as session:
        account = session.query(GtmEmailAccount).filter_by(provider="smtp").one()
        assert account.connected_account_id == sender["account"] and account.workspace_id == workspace_id
    local_mail.check_binding(sender["user"], sender["account"])


@pytest.mark.parametrize("sample", [False, True])
def test_local_relationship_email_draft_has_saved_compliant_footer(local_email, monkeypatch, sample):
    from app.core import local_mode
    from app.db.models import GtmContact, GtmLinkedInQueue
    from app.gtm.relationships import service
    from app.gtm.relationships.email_compliance import OPT_OUT
    from core.clock import utcnow

    factory, _, workspace_id, domain = local_email
    monkeypatch.setattr(local_mode, "is_test_mode", lambda: sample)
    with factory()() as session:
        session.add(GtmContact(id="copy-recipient", workspace_id=workspace_id, email=f"reader@{domain}",
            first_name="Morgan", dossier_hook=f"the work described on {domain}",
            enrichment_json=json.dumps({"contact_source_url": f"https://{domain}/contact"}),
            recipient_geo="CA", lawful_basis="consent", lawful_basis_note="You recorded permission.",
            lawful_basis_at=utcnow(), stage_changed_at=utcnow(), created_at=utcnow()))
        session.commit()
    result = service.create_draft(workspace_id, "copy-recipient", "email")
    edited = service.edit_draft(workspace_id, result["id"], "Would you like to share your experience?")
    assert OPT_OUT in edited["body"] and "16 Cedar Road, Toronto, Canada" in edited["body"]
    with factory()() as session:
        session.get(GtmLinkedInQueue, result["id"]).body = "A previously saved invitation."
        session.commit()
    service.mark_copied(workspace_id, result["id"])
    with factory()() as session:
        body = session.get(GtmLinkedInQueue, result["id"]).body
        assert all(line in body for line in (f"Your email address is listed publicly on {domain}.", OPT_OUT,
            "Alex Rowan", f"you@{domain}", "16 Cedar Road, Toronto, Canada"))
        assert "Sample sender address" not in body


def test_local_relationship_draft_edit_preserves_validation_error(local_email, monkeypatch):
    from fastapi import HTTPException
    from app.api import ops_api
    from app.gtm.relationships import service

    _, _, workspace_id, domain = local_email
    message = "Open Keys and connections > Your mail account and check Sender name, then try again."

    def edit_draft(workspace_id, draft_id, body):
        assert domain in body
        raise ValueError(message)

    monkeypatch.setattr(ops_api, "_auth_workspace", lambda *_: workspace_id)
    monkeypatch.setattr(service, "edit_draft", edit_draft)
    with pytest.raises(HTTPException) as error:
        ops_api.outreach_patch_draft("saved-draft", ops_api.DraftPatchBody(body="Your invitation for " + domain))
    assert error.value.status_code == 400 and error.value.detail == message


def test_local_unsubscribe_uses_private_machine_signing_material(local_email, monkeypatch):
    from app.core.local_owner import _ensure_proxy_secret
    from app.db.models import GtmContact
    from app.gtm.relationships import unsubscribe
    from core.clock import utcnow

    factory, _, workspace_id, domain = local_email
    for name in ("OUTREACH_UNSUB_SECRET", "ACCOUNT_SESSION_SECRET", "SESSION_SECRET"):
        monkeypatch.delenv(name, raising=False)
    address = f"reader@{domain}"
    with factory()() as session:
        session.add(GtmContact(id="stop-recipient", workspace_id=workspace_id, email=address,
            stage_changed_at=utcnow(), created_at=utcnow()))
        session.commit()
    token = unsubscribe.issue_unsubscribe_token(workspace_id, "stop-recipient", address)
    assert unsubscribe._decode(token) == (workspace_id, "stop-recipient", address)
    _ensure_proxy_secret()
    assert unsubscribe.issue_unsubscribe_token(workspace_id, "stop-recipient", address) == token
    import base64
    raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    tampered = base64.urlsafe_b64encode(raw.replace(workspace_id.encode(), b"another-workspace")).decode()
    assert unsubscribe._decode(tampered) is None
    assert unsubscribe.honor_unsubscribe_token(token)["ok"] is True
    with factory()() as session:
        assert session.get(GtmContact, "stop-recipient").do_not_contact == 1


@pytest.mark.parametrize("host,imap_host,expected", [
    ("smtp.gmail.com", "", "imap.gmail.com"),
    ("smtp.office365.com", "", "outlook.office365.com"),
    ("smtp.gmail.com", "inbox.example", "inbox.example"),
])
def test_local_saved_imap_and_booking_settings(local_email, monkeypatch, host, imap_host, expected):
    from app.core.local_secrets import update_settings
    from app.gtm import contact_work, local_mail

    _, _, _, domain = local_email
    link = f"https://calendar.{domain}/your-call"
    update_settings("smtp", {"imap_host": imap_host, "imap_port": 1993, "booking_link": link,
        "host": host, "port": 587, "user": f"you@{domain}", "sender_name": "Alex Rowan",
        "postal_address": "16 Cedar Road, Toronto, Canada"}, {"password": "saved-mailbox-password"})
    monkeypatch.setenv("SUPYMEM_IMAP_HOST", "stale.example")
    monkeypatch.setenv("SUPYMEM_IMAP_PORT", "999")
    config = local_mail.settings()
    assert (config.imap_host, config.imap_port, config.booking_link) == (expected, 1993, link)
    assert contact_work.booking_link("unused-venture") == link
    with pytest.raises(ValueError, match="Keys and connections > Email"):
        contact_work.set_booking_link("unused-venture", f"http://calendar.{domain}/your-call")
    assert contact_work.booking_link("unused-venture") == link
    contact_work.set_booking_link("unused-venture", None)
    assert contact_work.booking_link("unused-venture") is None
    assert local_mail.settings().booking_link == ""
    update_settings("smtp", {"imap_host": None, "booking_link": None}, {"password": "saved-mailbox-password"})
    assert local_mail.settings().imap_host and local_mail.settings().booking_link == ""


@pytest.mark.parametrize("allowed", [False, True])
def test_local_send_and_read_use_owner_capabilities(local_email, monkeypatch, allowed):
    from app.core.local_capabilities import blocked_message, enabled
    from app.core.local_secrets import update_settings
    from app.gtm import local_mail
    from app.gtm.supymem import composio_gmail, pipeline, workers

    _, _, workspace_id, domain = local_email
    update_settings("capabilities", {"send_email": allowed, "read_replies": allowed}, {})
    monkeypatch.setattr(local_mail, "enabled", enabled)
    monkeypatch.setenv("SUPYMEM_OUTBOUND_EXECUTION_ENABLED", "false")
    monkeypatch.setenv("CHANNELS_DRY_RUN", "true")
    assert pipeline._dry_run() is not allowed
    if not allowed:
        monkeypatch.setattr(local_mail, "_smtp", lambda *_: pytest.fail("Sending while switched off"))
        with pytest.raises(composio_gmail.DefiniteFailure) as error:
            local_mail.send(to=f"reader@{domain}", subject="Question", body="saved")
        assert str(error.value) == blocked_message("send_email")
        result = workers.tick_replies(workspace_id)
        assert result["skipped"] == "read_replies_disabled"
        assert result["message"] == blocked_message("read_replies")
        assert workers.tick_bounces(workspace_id)["skipped"] == "read_replies_disabled"
        assert pipeline._send_one("not-sent", simulated=False)["reason"] == "send_email_disabled"
        assert pipeline.tick_send_queue(workspace_id)["processed"] == []


def test_local_stop_reply_ignores_quoted_footer_and_suppresses(local_email, monkeypatch):
    from app.gtm.supymem import workers

    _, _, _, domain = local_email
    assert workers._reply_opt_out(f"STOP\n\n> Reply STOP to stop these emails.\n> From: you@{domain}")
    assert not workers._reply_opt_out(f"Sounds good\n\n> Reply STOP to stop these emails.\n> From: you@{domain}")


def test_local_booking_mark_requires_recorded_outreach(local_email):
    from app.db.models import GtmContact, GtmTouch, Venture
    from app.gtm import contact_work, journey
    from core.clock import utcnow

    factory, _, workspace_id, domain = local_email
    with factory()() as session:
        session.add(Venture(id="booking-venture", workspace_id=workspace_id, name=domain,
            raw_idea=domain, stage="draft", created_at=utcnow(), updated_at=utcnow()))
        session.add(GtmContact(id="booked-recipient", workspace_id=workspace_id, email=f"reader@{domain}",
            source="founder_import", stage_changed_at=utcnow(), created_at=utcnow()))
        session.commit()
    with pytest.raises(contact_work.ContactWorkError, match="recorded outreach") as refused:
        journey._manual_outcome(workspace_id, "booking-venture", "booked-recipient", booked=True)
    assert refused.value.code == "gtm_booked_requires_sent_message"
    with factory()() as session:
        session.add(GtmTouch(id="booking-sent", workspace_id=workspace_id, venture_id="booking-venture",
            contact_id="booked-recipient", direction="out", channel="email", simulated=0, sent_at=utcnow(),
            message_id=f"<sent@{domain}>", provider_thread_id=f"<sent@{domain}>", created_at=utcnow()))
        session.commit()
    journey._manual_outcome(workspace_id, "booking-venture", "booked-recipient", booked=True)
    first = contact_work.view(workspace_id, "booking-venture")["manual_outreach"][0]
    assert first["touch_id"] == "booking-sent" and first["booked_at"]
    contact_work.mark_booked(workspace_id, "booking-venture", "booked-recipient")
    assert contact_work.view(workspace_id, "booking-venture")["manual_outreach"] == [first]
    from app.gtm.relationships.suppression import email_suppressed, opted_out
    with factory()() as session:
        contact = session.get(GtmContact, "booked-recipient")
        assert email_suppressed(session, workspace_id, contact)
        assert not email_suppressed(session, workspace_id, contact, replied_ok=True)
        assert not opted_out(session, workspace_id, contact)
    with pytest.raises(ValueError):
        contact_work.mark_booked(workspace_id, "another-venture", "booked-recipient")
    with factory()() as session:
        session.add(GtmContact(id="manually-contacted", workspace_id=workspace_id, email=f"second@{domain}",
            source="founder_import", stage_changed_at=utcnow(), created_at=utcnow()))
        session.commit()
    contact_work._put("booking-venture", "manually-contacted", "manual_outreach", json.dumps(
        {"id": "manually-contacted", "sent_at": utcnow().isoformat(), "source_url": f"https://{domain}/contact"}))
    contact_work.mark_booked(workspace_id, "booking-venture", "manually-contacted")
    records = contact_work.view(workspace_id, "booking-venture")["manual_outreach"]
    assert next(record for record in records if record["id"] == "manually-contacted")["booked_at"]


def test_local_manual_channel_booking_requires_recorded_outreach(local_email, monkeypatch):
    import hashlib
    from app.db.models import Venture
    from app.gtm import contact_work, journey
    from core.clock import utcnow

    factory, _, workspace_id, domain = local_email
    with factory()() as session:
        session.add(Venture(id="channel-venture", workspace_id=workspace_id, name=domain,
            raw_idea=domain, stage="draft", created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    source_url = f"https://{domain}/community"
    channel_id = hashlib.sha256(source_url.encode()).hexdigest()[:24]
    view = {"reachable_leads": [], "industry_experts": [], "prospects": [],
        "channels": [{"name": domain, "url": source_url}], "need_signals": [], "manual_outreach": []}
    monkeypatch.setattr(journey, "status", lambda *args, **kwargs: view)
    with pytest.raises(ValueError, match="recorded outreach"):
        journey._manual_outcome(workspace_id, "channel-venture", channel_id, booked=True)
    journey._manual_outcome(workspace_id, "channel-venture", channel_id, booked=False)
    view["manual_outreach"] = contact_work.view(workspace_id, "channel-venture")["manual_outreach"]
    journey._manual_outcome(workspace_id, "channel-venture", channel_id, booked=True)
    booked = contact_work.view(workspace_id, "channel-venture")["manual_outreach"][0]
    assert booked["source_url"] == source_url and booked["sent_at"] and booked["booked_at"]


def test_local_calendar_is_not_attempted_without_connected_apps(local_email, monkeypatch):
    from app.connections import bookings
    from app.gtm import local_mail
    from core.clock import utcnow

    factory, _, _, _ = local_email
    monkeypatch.setattr(local_mail, "enabled", lambda name: False, raising=False)
    monkeypatch.setattr(bookings, "_events", lambda *_: pytest.fail("Calendar access was not requested"))
    with factory()() as session:
        result = bookings.read_bookings(session, "unused-venture", utcnow(), utcnow() + timedelta(days=1),
            feature_flag="goals_v1")
    assert result["available"] is False
    assert "mark" in result["next_step"].lower()


def test_local_smtp_hub_uses_saved_settings(local_email, monkeypatch):
    from app.connections import hub
    from app.core.local_secrets import update_settings

    _, _, _, domain = local_email
    update_settings("smtp", {"host": f"smtp.{domain}", "user": f"you@{domain}"}, {"password": "saved-password"})
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_FROM", raising=False)
    card = hub._smtp_card()
    assert card["ready"] and card["configured"] and not card["missing"]
    assert "saved-password" not in json.dumps(card)


def test_local_calendar_uses_generic_adapter_not_mailbox_binding(local_email, monkeypatch):
    from app.connections import bookings
    from app.content import composio_social
    from app.gtm import local_mail
    from app.gtm.supymem import composio_gmail

    _, _, workspace_id, domain = local_email
    monkeypatch.setattr(local_mail, "enabled", lambda name: name == "connected_apps")
    monkeypatch.setattr(composio_gmail, "execute_tool", lambda *_args, **_kwargs: pytest.fail("Calendar is not a mailbox"))
    requests = []
    def request(method, path, **kwargs):
        requests.append((method, path, kwargs["json_body"]))
        return {"successful": True, "data": {"items": [{"summary": domain}]}}
    monkeypatch.setattr(composio_social, "_request", request)
    account = {"workspace_id": workspace_id, "platform": "google_calendar", "provider_user_id": "your-calendar",
        "connected_account_id": "your-calendar-connection"}
    assert bookings._call(account, "GOOGLECALENDAR_FIND_EVENT", {"timeMin": "2026-11-14"})["items"][0]["summary"] == domain
    assert requests[0][:2] == ("POST", "/tools/execute/GOOGLECALENDAR_FIND_EVENT")
    assert requests[0][2]["user_id"] == account["provider_user_id"]
    assert requests[0][2]["connected_account_id"] == account["connected_account_id"]
    with pytest.raises(ValueError):
        bookings._call(account, "GOOGLECALENDAR_CREATE_EVENT", {})
    assert len(requests) == 1


@pytest.mark.parametrize("status", ["succeeded", "recovery_required"])
def test_local_test_send_uses_exact_metered_executor(local_email, monkeypatch, status):
    from app.approvals import external
    from app.gtm.supymem import composio_gmail, pipeline

    _, _, workspace_id, domain = local_email
    sent = []
    monkeypatch.setattr(composio_gmail, "send_gmail_email", lambda **kwargs: sent.append(kwargs) or
        {"provider_message_id": f"<test@{domain}>", "thread_id": f"<test@{domain}>"})
    def execute(**kwargs):
        assert kwargs["provider"] == "smtp" and kwargs["approval_id"] == "your-approved-email"
        assert kwargs["action"] == "gtm_test_email"
        assert kwargs["target"]["to"] == f"reader@{domain}" and kwargs["target"]["body"] == "Your reviewed test"
        receipt = kwargs["invoke"]()
        return external.OutboundExecution(status, "ok" if status == "succeeded" else "provider_outcome_unknown", receipt)
    monkeypatch.setattr(external, "execute_metered_external_action", execute)
    monkeypatch.setenv("SUPYMEM_OUTBOUND_EXECUTION_ENABLED", "false")
    monkeypatch.setenv("CHANNELS_DRY_RUN", "true")
    result = pipeline.send_test_email(workspace_id, to=f"reader@{domain}", subject="Your test",
        body="Your reviewed test", confirm=True, approval_id="your-approved-email")
    assert len(sent) == 1 and result["ok"] is (status == "succeeded") and result["dry_run"] is False
    if status == "recovery_required":
        assert "Sent mailbox" in result["block_reason"]


@pytest.mark.parametrize("outcome", ["sent", "refused", "unconfirmed"])
def test_local_approved_mail_execution_has_no_hosted_gates(local_email, monkeypatch, outcome):
    from app.approvals import external
    from app.core.local_secrets import update_settings
    from app.db.models import Approval, Venture
    from app.gtm.supymem.composio_gmail import DefiniteFailure
    from core.clock import utcnow

    factory, _, workspace_id, domain = local_email
    target = {"venture_id": "your-venture", "draft_id": "your-draft", "recipient": f"reader@{domain}", "body": "Your reviewed email"}
    with factory()() as session:
        session.add(Venture(id="your-venture", workspace_id=workspace_id, name=domain, raw_idea=domain,
            stage="draft", created_at=utcnow(), updated_at=utcnow()))
        session.add(Approval(id="local-approved-mail", workspace_id=workspace_id, kind="external_comms",
            subject_ref="your-draft", summary="Your requested outreach", risk="high", requested_by="you",
            status="approved", created_at=utcnow(), expires_at=utcnow() + timedelta(days=1), detail_json=json.dumps({"external_action": "gtm_email_send",
                "execution_digest": external.action_digest("gtm_email_send", target), "target": target})))
        session.commit()
    monkeypatch.setenv("SUPYMEM_OUTBOUND_EXECUTION_ENABLED", "false")
    calls = []
    def deliver():
        calls.append(domain)
        if outcome == "refused":
            raise DefiniteFailure("Your mailbox refused this recipient.")
        if outcome == "unconfirmed":
            raise TimeoutError("Your delivery is unconfirmed.")
        return {"provider_message_id": f"<sent@{domain}>"}
    args = {"workspace_id": workspace_id, "approval_id": "local-approved-mail", "action": "gtm_email_send",
        "provider": "smtp", "target": target, "invoke": deliver}
    update_settings("capabilities", {"send_email": False}, {})
    with pytest.raises(external.OutboundExecutionError) as disabled:
        external.execute_metered_external_action(**args)
    assert str(disabled.value) == external.blocked_message("send_email")
    update_settings("capabilities", {"send_email": True}, {})
    with pytest.raises(external.OutboundExecutionError):
        external.execute_metered_external_action(**(args | {"target": target | {"body": "A changed email"}}))
    assert calls == []
    from app.credits import metered
    from app.credits.errors import CreditsUnavailable
    with monkeypatch.context() as failed_setup:
        def unavailable(**kwargs):
            raise ValueError("Your email check could not start.")
        failed_setup.setattr(metered, "charge_sync", unavailable)
        with pytest.raises(external.OutboundExecutionError, match="Your monthly spend cap"):
            external.execute_metered_external_action(**args)
        def unwritable(**kwargs):
            raise CreditsUnavailable("Check your local data folder permissions and free space before trying again.")
        failed_setup.setattr(metered, "charge_sync", unwritable)
        with pytest.raises(external.OutboundExecutionError, match="local data folder permissions"):
            external.execute_metered_external_action(**args)
    assert calls == []
    result = external.execute_metered_external_action(**args)
    assert result.status == {"sent": "succeeded", "refused": "failed_definitive", "unconfirmed": "recovery_required"}[outcome]
    with pytest.raises(external.OutboundExecutionError):
        external.execute_metered_external_action(**args)
    assert calls == [domain]
    assert external.outbound_rate("gtm_email_send", "smtp").amount_micro == 0
    from app.credits import local_meter
    spending = local_meter.snapshot(workspace_id=workspace_id, venture_id="your-venture")
    assert len(spending["tasks"]) == 1
    assert spending["tasks"][0]["purpose"] == "outbound:gtm_email_send"
    assert spending["month_cost_usd_micro"] == 0
    if outcome == "unconfirmed":
        external.resolve_outreach_execution(workspace_id=workspace_id, venture_id="your-venture", draft_id="your-draft",
            approval_id="local-approved-mail", outcome="not_sent", actor_subject="you")
        with factory()() as session:
            state = json.loads(session.get(Approval, "local-approved-mail").detail_json)["external_execution"]
            assert state["delivery_resolution"]["outcome"] == "not_sent"


def test_local_requested_content_execution_has_no_hosted_gates(local_email, monkeypatch):
    from secrets import token_urlsafe
    from app.approvals import external
    from app.core.local_secrets import update_settings
    from app.db.models import Approval
    from core.clock import utcnow

    factory, _, workspace_id, domain = local_email
    update_settings("composio", {"auth_configs": {"linkedin": "test-linkedin-auth"}}, {"api_key": token_urlsafe(24)})
    update_settings("capabilities", {"connected_apps": True}, {})
    target = {"post_id": "requested-post", "platform": "linkedin", "body": f"Your reviewed note about {domain}"}
    with factory()() as session:
        session.add(Approval(id="requested-publication", workspace_id=workspace_id, kind="external_comms",
            subject_ref="requested-post", summary="Your requested publication", risk="high", requested_by="you",
            status="approved", created_at=utcnow(), expires_at=utcnow() + timedelta(days=1),
            detail_json=json.dumps({"external_action": "content_publish", "target": target,
                "execution_digest": external.action_digest("content_publish", target)})))
        session.commit()
    calls = []
    args = {"workspace_id": workspace_id, "approval_id": "requested-publication", "action": "content_publish",
        "provider": "composio_linkedin", "target": target, "invoke": lambda: calls.append(domain) or {"ok": True}}
    with pytest.raises(external.OutboundExecutionError):
        external.execute_metered_external_action(**{**args, "target": {**target, "body": "Changed copy"}})
    assert not calls
    result = external.execute_metered_external_action(**args)
    assert result.status == "succeeded" and calls == [domain]
    with pytest.raises(external.OutboundExecutionError):
        external.execute_metered_external_action(**args)
    assert calls == [domain] and external.outbound_rate("content_publish", "composio_linkedin").amount_micro == 0


@pytest.mark.parametrize("allowed", [False, True])
def test_local_check_replies_command_reads_only_when_requested(local_email, monkeypatch, allowed):
    import asyncio
    from app.chat import customers
    from app.db.models import Venture, Workspace
    from app.gtm import local_mail
    from app.gtm import journey
    from core.clock import utcnow
    from app.core.local_secrets import update_settings

    update_settings("capabilities", {"read_replies": allowed}, {})

    factory, _, workspace_id, domain = local_email
    with factory()() as session:
        actor = session.get(Workspace, workspace_id).owner_id
        session.add(Venture(id="reply-check-venture", workspace_id=workspace_id, name=domain, raw_idea=domain,
            stage="draft", created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    monkeypatch.setattr(local_mail, "enabled", lambda name: allowed)
    monkeypatch.setattr(customers, "_view", lambda *_: {"results": {"sent": 0, "replied": 0, "interested": 0, "negative": 0}})
    monkeypatch.setattr(journey.outreach, "contacts", lambda *_: [])
    calls = []
    async def poll(venture_id, **kwargs):
        calls.append((venture_id, kwargs))
        return 0
    monkeypatch.setattr(journey.outreach, "poll_replies", poll)
    response = asyncio.run(customers.run_command("reply-check-venture", "replies", None, "Check replies", actor))
    assert calls == ([("reply-check-venture", {"force": True})] if allowed else [])
    if not allowed:
        assert local_mail.blocked_message("read_replies") in response["body_md"]
    asyncio.run(customers.run_command("reply-check-venture", "sent_count", None, "How many sent?", actor))
    assert len(calls) == int(allowed)


def test_local_reply_stop_footer_replaces_dead_links(local_email):
    from app.gtm.relationships.email_compliance import OPT_OUT, complete_body, with_opt_out_link
    from app.gtm.relationships.unsubscribe import REPLY_OPT_OUT

    factory, _, workspace_id, domain = local_email
    contact = SimpleNamespace(id="your-recipient", workspace_id=workspace_id, email=f"reader@{domain}",
        enrichment_json=json.dumps({"contact_source_url": f"https://{domain}/contact"}))
    reviewed = "Your reviewed email\n\nStop these emails: https://old.example/public/unsubscribe?t=old"
    assert with_opt_out_link(contact, reviewed) == reviewed
    with factory()() as session:
        body = complete_body(session, workspace_id, contact, reviewed, "Your owner")
    assert REPLY_OPT_OUT in body and OPT_OUT in body and "https://old.example" not in body
    assert with_opt_out_link(contact, body) == body


@pytest.mark.parametrize("previous,current", [
    ("imported", "public"), ("directory", "public"), ("public", "imported"),
])
@pytest.mark.parametrize("line_ending", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_local_draft_footer_keeps_only_the_current_contact_source(local_email, previous, current, line_ending):
    from app.gtm.relationships.email_compliance import complete_body, source_line

    factory, _, workspace_id, domain = local_email
    evidence = {
        "imported": {"founder_import": True},
        "directory": {"lead_library": {"email_published": False}},
        "public": {"contact_source_url": f"https://{domain}/contact"},
    }
    contact = SimpleNamespace(id="source-recipient", workspace_id=workspace_id,
        email=f"reader@{domain}", enrichment_json=json.dumps(evidence[previous]))
    old_source = source_line(contact)
    contact.enrichment_json = json.dumps(evidence[current])
    with factory()() as session:
        body = complete_body(session, workspace_id, contact,
            "Would you share your experience?" + line_ending * 2 + old_source + line_ending, "Your owner")
        assert source_line(contact) in body and old_source not in body
        assert complete_body(session, workspace_id, contact, body, "Your owner") == body


@pytest.mark.parametrize("line_ending", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_local_edited_draft_cannot_add_a_false_source_disclosure(local_email, line_ending):
    from app.db.models import GtmContact
    from app.gtm.relationships.email_compliance import complete_body, validate_send
    from core.clock import utcnow

    factory, _, workspace_id, domain = local_email
    contact = GtmContact(id="edited-source-recipient", workspace_id=workspace_id,
        email=f"reader@{domain}", recipient_geo="CA", lawful_basis="consent",
        lawful_basis_note="You recorded permission.", lawful_basis_at=utcnow(),
        enrichment_json=json.dumps({"contact_source_url": f"https://{domain}/contact"}))
    with factory()() as session:
        body = complete_body(session, workspace_id, contact, "Would you share your experience?", "Your owner")
        validate_send(session, workspace_id, contact, body, "Your owner")
        with pytest.raises(ValueError, match="gtm_source_disclosure_required"):
            validate_send(session, workspace_id, contact,
                body + line_ending * 2 + "I have your address from my own contacts." + line_ending, "Your owner")


def test_local_composio_uses_saved_key_not_environment(local_email, monkeypatch):
    from secrets import token_urlsafe
    from app.content import composio_social
    from app.core.local_secrets import update_settings

    saved_key, environment_key = token_urlsafe(24), token_urlsafe(24)
    monkeypatch.setenv("COMPOSIO_API_KEY", environment_key)
    assert not composio_social.configured()
    update_settings("composio", {}, {"api_key": saved_key})
    update_settings("capabilities", {"connected_apps": True}, {})
    assert composio_social.api_key() == saved_key
    platform = next(iter(composio_social.AUTH_CONFIG_ENV))
    monkeypatch.setenv(composio_social.AUTH_CONFIG_ENV[platform], "stale-authorization")
    assert not composio_social.connectable(platform)
    with pytest.raises(composio_social.ComposioSocialError, match="Keys and connections"):
        composio_social.ensure_auth_config(platform)
    update_settings("composio", {"auth_configs": {composio_social.toolkit_for(platform): "saved-authorization"}}, {})
    assert composio_social.connectable(platform)
    assert composio_social.ensure_auth_config(platform) == "saved-authorization"
    monkeypatch.delenv("SUPYMEM_MODE")
    assert composio_social.api_key() == environment_key


@pytest.mark.parametrize("has_link", [False, True])
def test_local_call_invitation_does_not_require_calendar_setup(local_email, monkeypatch, has_link):
    from app.core.local_secrets import update_settings
    from app.db.models import GtmContact, GtmTouch, Venture
    from app.gtm import contact_work, local_mail
    from app.autopilot import outreach
    from app.gtm.relationships.email_compliance import OPT_OUT
    from core.clock import utcnow

    factory, _, workspace_id, domain = local_email
    link = f"https://calendar.{domain}/your-call" if has_link else ""
    update_settings("smtp", {"booking_link": link}, {})
    with factory()() as session:
        session.add(Venture(id="invitation-venture", workspace_id=workspace_id, name=domain, raw_idea=domain,
            stage="draft", created_at=utcnow(), updated_at=utcnow()))
        account = local_mail.ensure_account(session, workspace_id)
        session.add(GtmContact(id="invitation-recipient", workspace_id=workspace_id, email=f"reader@{domain}",
            source="venture:invitation-venture:founder_import", contact_provenance="founder_import", email_confidence="safe",
            gmail_thread_id=f"<sent@{domain}>", times_emailed=1, recipient_geo="CA", lawful_basis="consent",
            lawful_basis_note="You recorded permission.", lawful_basis_at=utcnow(),
            enrichment_json=json.dumps({"contact_source_url": f"https://{domain}/contact",
                "founder_import": {"lawful_basis_attested": True, "email_check": "mx"}}),
            stage_changed_at=utcnow(), created_at=utcnow()))
        session.flush()
        for direction, classification in (("out", None), ("in", "human_positive")):
            session.add(GtmTouch(id=f"invitation-{direction}", workspace_id=workspace_id, venture_id="invitation-venture",
                email_account_id=account.id, contact_id="invitation-recipient", direction=direction, channel="email",
                classification=classification, provider_thread_id=f"<sent@{domain}>", message_id=f"<{direction}@{domain}>",
                simulated=0, sent_at=utcnow(), created_at=utcnow()))
        session.commit()
    monkeypatch.setattr(contact_work, "_facts", lambda *_: domain)
    monkeypatch.setattr(contact_work, "_reply_text", lambda *_args, **_kwargs:
        ("Thanks for replying. Could we find a short time to talk?", "template"))
    contact_work.draft_reply(workspace_id, "invitation-venture", "invitation-recipient")
    drafts = outreach.drafts("invitation-venture", "draft")
    assert len(drafts) == 1 and drafts[0]["state"] == "draft"
    body = drafts[0]["body"]
    assert (f"https://calendar.{domain}/your-call" in body) is has_link and "None" not in body
    assert OPT_OUT in body and "I have your address from my own contacts." in body
    assert f"you@{domain}" in body and "16 Cedar Road, Toronto, Canada" in body


def test_local_saved_smtp_settings_keep_current_sender_in_review_and_transport(local_email, monkeypatch):
    from secrets import token_urlsafe
    from app.core.local_secrets import update_settings
    from app.db.models import GtmContact
    from app.gtm import local_mail
    from app.gtm.relationships.email_compliance import OPT_OUT, complete_body, validate_send
    from core.clock import utcnow

    factory, _, workspace_id, domain = local_email
    password = token_urlsafe(24)
    update_settings("smtp", {"host": f"smtp.{domain}", "port": 465, "user": f"sender@{domain}",
        "sender_name": "Morgan Reed", "postal_address": "28 Pine Road, Toronto, Canada"}, {"password": password})
    contact = GtmContact(id="saved-smtp-recipient", workspace_id=workspace_id, email=f"reader@{domain}",
        enrichment_json=json.dumps({"contact_source_url": f"https://{domain}/contact"}), recipient_geo="CA",
        lawful_basis="consent", lawful_basis_note="You recorded permission.", lawful_basis_at=utcnow(),
        source="founder_import", stage_changed_at=utcnow(), created_at=utcnow())
    delivered = []

    @contextmanager
    def smtp(config):
        assert (config.host, config.port, config.username, config.password) == (f"smtp.{domain}", 465, f"sender@{domain}", password)
        yield SimpleNamespace(send_message=lambda message, **kwargs: delivered.append(message) or {})

    monkeypatch.setattr(local_mail, "_smtp", smtp)
    assert local_mail.configured()
    with factory()() as session:
        session.add(contact)
        session.commit()
        body = complete_body(session, workspace_id, contact, "Would you like to hear more?", "Old identity")
        assert all(line in body for line in ("Morgan Reed", f"sender@{domain}", "28 Pine Road, Toronto, Canada",
            f"Your email address is listed publicly on {domain}.", OPT_OUT))
        assert complete_body(session, workspace_id, contact, body, f"sender@{domain}") == body
        validate_send(session, workspace_id, contact, body, "Old identity")
    receipt = local_mail.send(to=contact.email, subject="A question", body=body)
    assert len(delivered) == 1 and str(delivered[0]["From"]).endswith(f"<sender@{domain}>")
    assert receipt["provider_message_id"]
    assert local_mail.configured() and local_mail.settings().username == f"sender@{domain}"


def test_local_read_status_without_smtp_keeps_send_validation(local_email, monkeypatch):
    from app.db.models import GtmEmailAccount
    from app.gtm.supymem import pipeline
    from app.ventures.state_brief import _connections_section

    factory, mailboxes, workspace_id, _ = local_email
    from app.core.local_secrets import get_secret, update_settings
    update_settings("smtp", {"host": ""}, {"password": get_secret("smtp", "password")})
    with factory()() as session:
        for multi in (True, False):
            monkeypatch.setattr(mailboxes, "enabled", lambda multi=multi: multi)
            assert mailboxes.list_read_mailboxes(session, workspace_id) == []
            assert pipeline._active_mailbox(session, workspace_id) is None
            assert _connections_section(session, GtmEmailAccount, workspace_id)["gmail"]["connected"] is False
            with pytest.raises(mailboxes.MailboxUnavailable, match="Keys and connections"):
                mailboxes.resolve_send_mailbox(session, workspace_id)


def test_local_missing_smtp_settings_use_capability_next_step(local_email, monkeypatch):
    from app.core.local_capabilities import blocked_message
    from app.core.local_secrets import update_settings
    from app.gtm import local_mail

    _, _, _, domain = local_email
    update_settings("smtp", {"host": "", "user": f"you@{domain}"}, {"password": "test-mail-password"})
    with pytest.raises(ValueError) as error:
        local_mail.settings()
    assert str(error.value) == blocked_message("send_email")


def test_local_footer_and_public_optout_fail_closed(local_email, monkeypatch):
    from app.core.local_secrets import update_settings
    from urllib.parse import parse_qs, urlparse

    from app.db.models import GtmContact
    from app.gtm.relationships.email_compliance import (
        OPT_OUT,
        complete_body,
        validate_send,
        with_opt_out_link,
    )
    from app.gtm.relationships.unsubscribe import (
        honor_unsubscribe_token,
        unsubscribe_url,
    )
    from core.clock import utcnow

    db, _, workspace_id, domain = local_email
    contact = GtmContact(id="local-recipient", workspace_id=workspace_id, email=f"reader@{domain}",
        enrichment_json=json.dumps({"contact_source_url": f"https://{domain}/contact"}), recipient_geo="CA",
        lawful_basis="consent", lawful_basis_note="You recorded permission.", lawful_basis_at=utcnow(),
        source="founder_import", stage_changed_at=utcnow(), created_at=utcnow())
    with db()() as session:
        session.add(contact)
        session.commit()
        body = complete_body(session, workspace_id, contact, "Would you like to hear more?", "Old identity")
        assert "Alex Rowan" in body and "16 Cedar Road, Toronto, Canada" in body
        assert f"Your email address is listed publicly on {domain}." in body and OPT_OUT in body
        assert "Stop these emails:" not in body
        assert with_opt_out_link(contact, body) == body
        assert unsubscribe_url(workspace_id, contact.id, contact.email) == ""
        validate_send(session, workspace_id, contact, body, "Old identity")
        removed = body.replace(OPT_OUT, "")
        assert with_opt_out_link(contact, removed) == removed
        with pytest.raises(ValueError, match="gtm_draft_one_ask_and_optout_required"):
            validate_send(session, workspace_id, contact, removed, "Old identity")
        from app.db.models import Person, Workspace
        session.get(Person, session.get(Workspace, workspace_id).owner_id).display_name = "Alex Rowan"
        update_settings("smtp", {"sender_name": "Alex"}, {})
        short_name = complete_body(session, workspace_id, contact, body, "Old identity")
        assert "Alex" in short_name.splitlines()
        validate_send(session, workspace_id, contact, short_name, "Old identity")
        update_settings("smtp", {"sender_name": "Alex Rowan", "postal_address": ""}, {})
        with pytest.raises(ValueError):
            validate_send(session, workspace_id, contact, body, "Old identity")
        update_settings("smtp", {"postal_address": "16 Cedar Road, Toronto, Canada"}, {})
        from app.core.local_owner import _ensure_proxy_secret
        _ensure_proxy_secret()
        monkeypatch.setenv("SUPYMEM_PUBLIC_URL", f"https://instance.{domain}")
        with monkeypatch.context() as secrets:
            for key in ("OUTREACH_UNSUB_SECRET", "ACCOUNT_SESSION_SECRET", "SESSION_SECRET"):
                secrets.delenv(key, raising=False)
            assert unsubscribe_url(workspace_id, contact.id, contact.email).startswith(f"https://instance.{domain}/public/unsubscribe?t=")
        linked = complete_body(session, workspace_id, contact, body, "Old identity")
        url = unsubscribe_url(workspace_id, contact.id, contact.email)
        assert url.startswith(f"https://instance.{domain}/public/unsubscribe?t=") and url in linked
        with pytest.raises(ValueError):
            validate_send(session, workspace_id, contact, body, "Old identity")
        validate_send(session, workspace_id, contact, linked, "Old identity")
        monkeypatch.delenv("SUPYMEM_PUBLIC_URL")
        with pytest.raises(ValueError):
            validate_send(session, workspace_id, contact, linked, "Old identity")
        repaired = complete_body(session, workspace_id, contact, linked, "Old identity")
        assert "Stop these emails:" not in repaired
        validate_send(session, workspace_id, contact, repaired, "Old identity")
    assert honor_unsubscribe_token(parse_qs(urlparse(url).query)["t"][0])["contact_id"] == contact.id


def test_local_smtp_uses_tls_and_preserves_uncertain_delivery(local_email, monkeypatch):
    import smtplib

    from app.gtm import local_mail
    from app.gtm.supymem import composio_gmail

    _, _, _, domain = local_email
    calls = []

    class SMTP:
        def __init__(self, host, port, **kwargs):
            assert (host, port) == (f"mail.{domain}", 587)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def starttls(self, **kwargs):
            calls.append("tls")
        def login(self, username, password):
            assert username == f"you@{domain}" and password
            calls.append("login")
        def send_message(self, message, **kwargs):
            calls.append(message)
            return {}

    monkeypatch.setattr(smtplib, "SMTP", SMTP)
    receipt = local_mail.send(to=f"reader@{domain}", subject="A question", body="Alex Rowan\n16 Cedar Road, Toronto, Canada")
    assert calls[:2] == ["tls", "login"]
    assert str(calls[2]["From"]).endswith(f"<you@{domain}>")
    assert receipt["provider_message_id"] == receipt["rfc_message_id"] == receipt["thread_id"]
    def refused(self, message, **kwargs):
        raise smtplib.SMTPRecipientsRefused({f"reader@{domain}": (550, b"refused")})
    monkeypatch.setattr(SMTP, "send_message", refused)
    with pytest.raises(composio_gmail.DefiniteFailure):
        local_mail.send(to=f"reader@{domain}", subject="A question", body="saved")
    def uncertain(self, message, **kwargs):
        raise TimeoutError("timeout")
    monkeypatch.setattr(SMTP, "send_message", uncertain)
    with pytest.raises(RuntimeError) as failed:
        local_mail.send(to=f"reader@{domain}", subject="A question", body="saved")
    assert not isinstance(failed.value, composio_gmail.DefiniteFailure)


@pytest.mark.parametrize("media_type", ["plain", "html"])
def test_local_imap_reads_reply_without_marking_seen(local_email, monkeypatch, media_type):
    import imaplib
    from email.message import EmailMessage

    from app.gtm import local_mail
    from app.gtm.supymem import composio_gmail, workers

    db, _, workspace_id, domain = local_email
    with db()() as session:
        account = local_mail.ensure_account(session, workspace_id)
        session.commit()
        account_id, user_id = account.connected_account_id, account.composio_user_id
    reply = EmailMessage()
    reply["From"] = f"reader@{domain}"
    reply["To"] = f"you@{domain}"
    reply["Message-ID"] = f"<reply@{domain}>"
    reply["In-Reply-To"] = f"<sent@{domain}>"
    reply["References"] = f"<sent@{domain}>"
    reply.set_content("<p>STOP</p>" if media_type == "html" else "STOP", subtype=media_type)

    class IMAP:
        def __init__(self, host, port, **kwargs):
            assert (host, port) == (f"mail.{domain}", 993)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def login(self, username, password):
            assert username == f"you@{domain}" and password
        def select(self, folder, readonly):
            assert readonly is True
            return "OK", [b"1"]
        def response(self, code):
            return code, [b"7" if code == "UIDVALIDITY" else b"12"]
        def uid(self, command, *args):
            if command == "search":
                return "OK", [b"11"]
            assert command == "fetch" and args[1] == "(BODY.PEEK[])"
            return "OK", [(b"11", reply.as_bytes())]

    monkeypatch.setattr(imaplib, "IMAP4_SSL", IMAP)
    messages = composio_gmail.fetch_inbound_from(composio_user_id=user_id,
        connected_account_id=account_id, from_email=f"reader@{domain}")
    message = workers._iter_messages(messages)[0]
    assert workers._message_text(message).strip() == "STOP"
    assert workers._reply_opt_out(workers._message_text(message))
    assert composio_gmail.message_headers(message)["in-reply-to"] == f"<sent@{domain}>"
    assert message["threadId"] == f"<sent@{domain}>"
    history = composio_gmail.fetch_history(composio_user_id=user_id, connected_account_id=account_id, start_history_id="7:10")
    assert history["historyId"] == "7:11" and history["history"][0]["messagesAdded"][0]["message"]["id"] == message["id"]
    original_uid = IMAP.uid
    with monkeypatch.context() as larger_thread:
        def uid(self, command, *args):
            if command == "search":
                return "OK", [" ".join(str(number) for number in range(1, 202)).encode()]
            return original_uid(self, command, *args)
        larger_thread.setattr(IMAP, "uid", uid)
        messages, progress = workers._bootstrap_replies(workspace_id, user_id, account_id,
            [("touch", "contact", None, f"<sent@{domain}>", f"<sent@{domain}>")], None)
        assert len(messages) == workers._fetch_limit()
        assert "message:imap:7:201" in progress["bootstrap_tasks"]
    from app.db.models import GtmContact, GtmSuppression, GtmTouch
    from core.clock import utcnow
    from sqlalchemy import select

    monkeypatch.setattr(workers.mailboxes, "get_policy", lambda key: {"multi_gmail_v1": False})
    with db()() as session:
        session.add(GtmContact(id="imap-recipient", workspace_id=workspace_id, email=f"reader@{domain}",
            times_emailed=1, source="founder_import", stage_changed_at=utcnow(), created_at=utcnow()))
        session.flush()
        session.add(GtmTouch(id="imap-sent", workspace_id=workspace_id, contact_id="imap-recipient",
            email_account_id=account.id, direction="out", channel="email", simulated=0, sent_at=utcnow(),
            message_id=f"<sent@{domain}>", provider_thread_id=f"<sent@{domain}>", created_at=utcnow()))
        session.commit()
    assert workers.tick_replies(workspace_id)["marked"] == ["imap-recipient"]
    with db()() as session:
        assert session.get(GtmContact, "imap-recipient").do_not_contact == 1
        assert session.scalar(select(GtmSuppression).where(GtmSuppression.email == f"reader@{domain}")) is not None


@pytest.mark.parametrize("outcome", ["accepted", "refused", "unconfirmed"])
@pytest.mark.parametrize("capability_enabled", [False, True])
def test_local_outreach_keeps_send_gates_and_reservations(local_email, monkeypatch, outcome, capability_enabled):
    from app.db.models import GtmContact, GtmEmailEvent, GtmOutreachSettings
    from app.gtm import local_mail
    from app.gtm.relationships.email_compliance import complete_body
    from app.gtm.supymem import composio_gmail
    from core.clock import utcnow
    from sqlalchemy import select

    db, mailboxes, workspace_id, domain = local_email
    from app.core.local_secrets import update_settings
    monkeypatch.setattr(local_mail, "enabled", lambda name: capability_enabled)
    monkeypatch.setattr(mailboxes, "get_policy", lambda key: {"multi_gmail_v1": False})
    calls = []

    @contextmanager
    def smtp(config):
        def deliver(message, **kwargs):
            calls.append(message)
            if outcome == "refused":
                raise composio_gmail.DefiniteFailure("Your mailbox refused the recipient. Check the address and try again.")
            if outcome == "unconfirmed":
                raise RuntimeError("Your delivery is unconfirmed. Check your mailbox before trying again.")
            return {}
        yield SimpleNamespace(send_message=deliver)

    monkeypatch.setattr(local_mail, "_smtp", smtp)
    with db()() as session:
        account = local_mail.ensure_account(session, workspace_id)
        account.daily_send_limit = 1
        contact = GtmContact(id="smtp-recipient", workspace_id=workspace_id, email=f"reader@{domain}",
            enrichment_json=json.dumps({"contact_source_url": f"https://{domain}/contact"}), recipient_geo="CA",
            lawful_basis="consent", lawful_basis_note="You recorded permission.", lawful_basis_at=utcnow(),
            source="founder_import", stage_changed_at=utcnow(), created_at=utcnow())
        session.add(contact)
        session.add(GtmOutreachSettings(workspace_id=workspace_id, send_enabled=True, created_at=utcnow(), updated_at=utcnow()))
        session.commit()
        body = complete_body(session, workspace_id, contact, "Would you like to hear more?", account.email)
        args = {"workspace_id": workspace_id, "composio_user_id": account.composio_user_id,
            "connected_account_id": account.connected_account_id, "to": contact.email, "subject": "A question", "body": body}
    with db()() as session:
        session.get(GtmContact, "smtp-recipient").do_not_contact = 1
        session.commit()
    with pytest.raises(composio_gmail.DefiniteFailure):
        composio_gmail.send_gmail_email(**args)
    with db()() as session:
        session.get(GtmContact, "smtp-recipient").do_not_contact = 0
        session.get(GtmOutreachSettings, workspace_id).send_enabled = False
        session.commit()
    with pytest.raises(composio_gmail.DefiniteFailure):
        composio_gmail.send_gmail_email(**args)
    with db()() as session:
        session.get(GtmOutreachSettings, workspace_id).send_enabled = True
        session.commit()
    update_settings("smtp", {"postal_address": ""}, {})
    with pytest.raises(composio_gmail.DefiniteFailure):
        composio_gmail.send_gmail_email(**args)
    assert calls == []
    update_settings("smtp", {"postal_address": "16 Cedar Road, Toronto, Canada"}, {})
    if not capability_enabled:
        with pytest.raises(composio_gmail.DefiniteFailure):
            composio_gmail.send_gmail_email(**args)
        assert calls == []
        with db()() as session:
            assert composio_gmail.mailbox_slots_used_today(session, session.get(type(account), account.id)) == 0
        return
    if outcome == "accepted":
        assert composio_gmail.send_gmail_email(**args)["provider_message_id"]
        with pytest.raises(composio_gmail.DefiniteFailure):
            composio_gmail.send_gmail_email(**args)
    else:
        error = composio_gmail.DefiniteFailure if outcome == "refused" else RuntimeError
        with pytest.raises(error):
            composio_gmail.send_gmail_email(**args)
        if outcome == "unconfirmed":
            with pytest.raises(composio_gmail.DefiniteFailure):
                composio_gmail.send_gmail_email(**args)
    assert len(calls) == 1
    with db()() as session:
        events = list(session.scalars(select(GtmEmailEvent.event_type).where(GtmEmailEvent.email_account_id == account.id)))
        assert events.count("gmail_reserved") == 1
        assert events.count("gmail_released") == (outcome == "refused")
        assert events.count("gmail_sent") == (outcome == "accepted")


@pytest.fixture()
def mailbox_db(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/mailboxes.db")
    monkeypatch.delenv("COMPOSIO_PROJECT", raising=False)
    monkeypatch.delenv("OPS_API_KEY", raising=False)
    load_company_os_app()
    from app.connections import mailboxes
    from app.db.models import GtmEmailAccount
    from app.authz.workspace import ensure_subject_person
    from app.state import db
    from app.workspaces.service import create_workspace
    from core.clock import utcnow

    now = utcnow()
    first = create_workspace(kind="company", name="Build tools", owner_id=ensure_subject_person("founder_tools"))
    second = create_workspace(kind="company", name="Home cooking", owner_id=ensure_subject_person("founder_recipes"))
    with db()() as session:
        for mailbox_id, workspace_id, email, offset in (
            ("tools", first.id, "you@buildtools.example", 0),
            ("recipes", first.id, "you@homecooking.example", 1),
            ("foreign", second.id, "you@another.example", 2),
        ):
            session.add(GtmEmailAccount(
                id=mailbox_id, workspace_id=workspace_id, provider="gmail", email=email,
                connected_account_id=f"ca_{mailbox_id}", composio_user_id=f"gmail:{workspace_id}",
                status="active", created_at=now, updated_at=now + timedelta(seconds=offset),
            ))
        session.commit()
        workspace_id = first.id
    monkeypatch.setattr(mailboxes, "get_policy", lambda key: {"multi_gmail_v1": True})
    from app.credits import metered
    @contextmanager
    def charge(**kwargs):
        yield SimpleNamespace(record_micro=lambda amount: None)
    monkeypatch.setattr(metered, "charge_sync", charge)
    return db, mailboxes, workspace_id


@pytest.mark.parametrize("audience,domain", [
    ("Precision parts distributors", "precision-parts.example"),
    ("Weekend canoe learners", "weekend-canoes.example"),
])
def test_disabled_mailbox_read_returns_empty_without_enabling_writes(mailbox_db, monkeypatch, audience, domain):
    from app.api import ops_api
    from app.db.models import Venture, Workspace
    from app.tenancy import bind
    from core.clock import utcnow
    from fastapi import HTTPException

    db, mailboxes, workspace_id = mailbox_db
    with db()() as session:
        workspace = session.get(Workspace, workspace_id)
        actor = workspace.owner_id
        session.add(Venture(id="disabled-mail-venture", workspace_id=workspace_id, name=domain,
            raw_idea=audience, stage="draft", created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    monkeypatch.setattr(mailboxes, "enabled", lambda: False)
    with bind(workspace_id=workspace_id, venture_id="disabled-mail-venture", actor=actor):
        for venture_id in (None, "disabled-mail-venture"):
            result = ops_api.connections_gmail_mailboxes(workspace_id=workspace_id, venture_id=venture_id,
                x_workspace_id=None, x_api_key=None)
            assert result == {"multi_gmail_v1": False, "workspace_id": workspace_id, "mailboxes": []}
        with pytest.raises(HTTPException) as unavailable:
            ops_api.connections_gmail_mailboxes(workspace_id=workspace_id, venture_id="another-venture",
                x_workspace_id=None, x_api_key=None)
        assert unavailable.value.status_code == 404
        with pytest.raises(HTTPException) as disabled:
            ops_api.connections_gmail_default(ops_api.GmailDefaultBody(mailbox_id="tools"),
                workspace_id=workspace_id, x_workspace_id=None, x_api_key=None)
        assert disabled.value.status_code == 404


def test_owned_mailboxes_and_explicit_sender_fail_closed(mailbox_db):
    db, mailboxes, workspace_id = mailbox_db
    from app.db.models import GtmEmailAccount

    with db()() as session:
        assert {ref["mailbox_id"] for ref in mailboxes.list_read_mailboxes(session, workspace_id)} == {"tools", "recipes"}
        for mailbox_id in ("tools", "recipes"):
            assert mailboxes.resolve_send_mailbox(session, workspace_id, mailbox_id=mailbox_id)["mailbox_id"] == mailbox_id
        for mailbox_id, email in (("tools", "you@buildtools.xn--p1ai"), ("recipes", "you@homecooking.example")):
            account = session.get(GtmEmailAccount, mailbox_id)
            account.email = email
            assert mailboxes.resolve_send_mailbox(session, workspace_id, mailbox_id=mailbox_id)["email"] == email
            for suffix in ("\r\n", "\r\nBcc: injected@example.test", "a" * 321):
                account.email = email + suffix
                with pytest.raises(mailboxes.MailboxUnavailable):
                    mailboxes.resolve_send_mailbox(session, workspace_id, mailbox_id=mailbox_id)
            account.email = email
        for mailbox_id in ("foreign", "missing", ""):
            with pytest.raises(mailboxes.MailboxUnavailable):
                mailboxes.resolve_send_mailbox(session, workspace_id, mailbox_id=mailbox_id)
        session.get(GtmEmailAccount, "tools").status = "needs_reconnect"
        with pytest.raises(mailboxes.MailboxUnavailable):
            mailboxes.resolve_send_mailbox(session, workspace_id, mailbox_id="tools")
        assert [ref["mailbox_id"] for ref in mailboxes.list_read_mailboxes(session, workspace_id)] == ["recipes"]
        session.get(GtmEmailAccount, "recipes").email = None
        with pytest.raises(mailboxes.MailboxUnavailable):
            mailboxes.resolve_send_mailbox(session, workspace_id, mailbox_id="recipes")


def test_untrusted_mailbox_ids_fail_closed_in_the_shared_contract(mailbox_db):
    db, mailboxes, workspace_id = mailbox_db
    with db()() as session:
        for domain_mailbox in ("tools", "recipes"):
            mailboxes.set_default_mailbox(session, workspace_id, domain_mailbox)
            for invalid in ("", 0, False, [], {}):
                with pytest.raises(mailboxes.MailboxUnavailable):
                    mailboxes.resolve_send_mailbox(session, workspace_id, mailbox_id=invalid)


def test_flag_off_delegates_to_legacy_selection_and_verification(mailbox_db, monkeypatch):
    db, mailboxes, workspace_id = mailbox_db
    from app.gtm.supymem import pipeline

    for flags in (None, {}, {"multi_gmail_v1": False}, {"multi_gmail_v1": 1}):
        monkeypatch.setattr(mailboxes, "get_policy", lambda key: flags)
        with db()() as session:
            expected = pipeline._active_mailbox(session, workspace_id).id
            assert mailboxes.resolve_send_mailbox(session, workspace_id, mailbox_id="foreign")["mailbox_id"] == expected
            assert [ref["mailbox_id"] for ref in mailboxes.list_read_mailboxes(session, workspace_id)] == [expected]
        proof = {"ok": True, "account_id": "legacy"}
        monkeypatch.setattr(pipeline, "verify_gmail_connection", lambda wid: proof)
        assert mailboxes.verify_gmail_connection(workspace_id, mailbox_id="foreign") is proof


def test_defaults_persist_and_revoked_or_foreign_defaults_never_fall_back(mailbox_db):
    db, mailboxes, workspace_id = mailbox_db
    from app.db.models import GtmEmailAccount, Venture, Workspace
    from core.clock import utcnow
    from app.gtm.supymem import pipeline
    with db()() as session:
        session.add(Venture(id="venture_tools", workspace_id=workspace_id, name="Build tools", raw_idea="Build tools",
                            stage="draft", created_at=utcnow(), updated_at=utcnow()))
        session.commit()
        assert mailboxes.resolve_send_mailbox(session, workspace_id)["mailbox_id"] == "recipes"
        session.commit()
        mailboxes.set_default_mailbox(session, workspace_id, "tools")
        mailboxes.set_default_mailbox(session, workspace_id, "recipes", venture_id="venture_tools")
        session.commit()
    with db()() as session:
        assert mailboxes.resolve_send_mailbox(session, workspace_id)["mailbox_id"] == "tools"
        assert pipeline._legacy_active_mailbox(session, workspace_id).id == "recipes"
        assert pipeline._active_mailbox(session, workspace_id).id == "tools"
        assert mailboxes.resolve_send_mailbox(session, workspace_id, venture_id="venture_tools")["mailbox_id"] == "recipes"
        assert mailboxes.resolve_send_mailbox(session, workspace_id, venture_id="venture_tools", mailbox_id="tools")["mailbox_id"] == "tools"
        mailboxes.set_default_mailbox(session, workspace_id, None, venture_id="venture_tools")
        assert mailboxes.resolve_send_mailbox(session, workspace_id, venture_id="venture_tools")["mailbox_id"] == "tools"
        mailboxes.set_default_mailbox(session, workspace_id, "recipes", venture_id="venture_tools")
        with pytest.raises(mailboxes.MailboxUnavailable):
            mailboxes.set_default_mailbox(session, workspace_id, None)
        session.get(GtmEmailAccount, "recipes").status = "revoked"
        with pytest.raises(mailboxes.MailboxUnavailable):
            mailboxes.resolve_send_mailbox(session, workspace_id, venture_id="venture_tools")
        session.get(Workspace, workspace_id).default_gmail_mailbox_id = "foreign"
        with pytest.raises(mailboxes.MailboxUnavailable):
            mailboxes.resolve_send_mailbox(session, workspace_id)
        assert pipeline._active_mailbox(session, workspace_id) is None
        with pytest.raises(mailboxes.MailboxUnavailable):
            mailboxes.set_default_mailbox(session, workspace_id, "foreign")
        with pytest.raises(mailboxes.MailboxUnavailable):
            mailboxes.resolve_send_mailbox(session, workspace_id, venture_id="missing", mailbox_id="tools")


@pytest.mark.parametrize("event_type", ["SENT", "sent", "sent_metadata"])
def test_conversations_use_original_mailbox_and_unbound_history_stays_unbound(mailbox_db, event_type):
    db, mailboxes, workspace_id = mailbox_db
    from app.db.models import GtmContact, GtmEmailAccount, GtmEmailEvent
    from core.clock import utcnow
    with db()() as session:
        for contact_id, email in (("contact_tools", "buyer@buildtools.example"), ("contact_recipes", "cook@homecooking.example")):
            session.add(GtmContact(id=contact_id, workspace_id=workspace_id, email=email, gmail_thread_id="same-thread",
                                   created_at=utcnow(), stage_changed_at=utcnow()))
        session.commit()
        assert mailboxes.mailbox_for_conversation(session, contact_id="contact_tools") is None
        for contact_id, mailbox_id in (("contact_tools", "recipes"), ("contact_recipes", "tools")):
            session.add(GtmEmailEvent(id=f"simulated-{contact_id}", workspace_id=workspace_id, contact_id=contact_id,
                                     email_account_id=mailbox_id, event_type="SENT",
                                     metadata_json=json.dumps({"thread_id": "same-thread", "simulated": True}), created_at=utcnow()))
        session.commit()
        assert mailboxes.mailbox_for_conversation(session, contact_id="contact_tools") is None
        for contact_id, mailbox_id in (("contact_tools", "tools"), ("contact_recipes", "recipes")):
            session.add(GtmEmailEvent(id=f"sent-{contact_id}", workspace_id=workspace_id, contact_id=contact_id,
                                     email_account_id=mailbox_id, event_type=event_type,
                                     metadata_json=json.dumps({"thread_id": "same-thread"}), created_at=utcnow()))
        session.commit()
        mailboxes.set_default_mailbox(session, workspace_id, "recipes")
        assert mailboxes.mailbox_for_conversation(session, contact_id="contact_tools", thread_id="same-thread")["mailbox_id"] == "tools"
        assert mailboxes.mailbox_for_conversation(session, contact_id="contact_recipes", thread_id="same-thread")["mailbox_id"] == "recipes"
        with pytest.raises(mailboxes.MailboxUnavailable):
            mailboxes.mailbox_for_conversation(session, thread_id="same-thread")
        session.get(GtmEmailAccount, "tools").status = "revoked"
        with pytest.raises(mailboxes.MailboxUnavailable):
            mailboxes.mailbox_for_conversation(session, contact_id="contact_tools")


def test_conversation_lookup_cannot_cross_the_current_founder_scope(mailbox_db):
    db, mailboxes, workspace_id = mailbox_db
    from app.db.models import GtmContact, GtmEmailAccount, GtmEmailEvent
    from app.tenancy import bind
    from core.clock import utcnow
    with db()() as session:
        for contact_id, mailbox_id in (("contact_tools", "tools"), ("contact_foreign", "foreign")):
            account = session.get(GtmEmailAccount, mailbox_id)
            session.add(GtmContact(id=contact_id, workspace_id=account.workspace_id, email=account.email,
                                   created_at=utcnow(), stage_changed_at=utcnow()))
            session.add(GtmEmailEvent(id="sent-" + contact_id, workspace_id=account.workspace_id, contact_id=contact_id,
                                     email_account_id=mailbox_id, event_type="sent", created_at=utcnow()))
        session.commit()
        with bind(workspace_id=workspace_id, venture_id="", actor="founder_tools"):
            assert mailboxes.mailbox_for_conversation(session, contact_id="contact_tools")["mailbox_id"] == "tools"
            with pytest.raises(mailboxes.MailboxUnavailable):
                mailboxes.mailbox_for_conversation(session, contact_id="contact_foreign")


@pytest.mark.parametrize("status", ["active", "needs_reconnect"])
def test_add_another_keeps_original_default_and_reconnect_only_repairs_target(mailbox_db, monkeypatch, status):
    db, mailboxes, workspace_id = mailbox_db
    from app.db.models import GtmEmailAccount, Workspace
    from app.gtm.supymem import composio_gmail, pipeline
    with db()() as session:
        session.get(Workspace, workspace_id).default_gmail_mailbox_id = "tools"
        session.get(GtmEmailAccount, "tools").status = status
        session.commit()
    monkeypatch.setattr(composio_gmail, "get_gmail_profile", lambda user, account: {
        "email": "you@homecooking.example" if account == "ca_added" else "you@buildtools.example"})
    result = pipeline.sync_gmail_accounts(workspace_id, connections=[{
        "id": "ca_added", "user_id": f"gmail:{workspace_id}", "status": "ACTIVE"}], intent="add")
    assert result["synced"] == 1
    with db()() as session:
        assert session.get(Workspace, workspace_id).default_gmail_mailbox_id == "tools"
        assert session.get(GtmEmailAccount, "tools").status == status
        assert session.get(GtmEmailAccount, "recipes").connected_account_id == "ca_recipes"
    assert pipeline.sync_gmail_accounts(workspace_id, connections=[{
        "id": "ca_reconnected", "user_id": f"gmail:{workspace_id}", "status": "ACTIVE"}],
        intent="reconnect", mailbox_id="tools")["synced"] == 1
    with db()() as session:
        assert session.get(GtmEmailAccount, "tools").connected_account_id == "ca_reconnected"
        assert session.get(GtmEmailAccount, "recipes").connected_account_id == "ca_recipes"
    with pytest.raises(mailboxes.MailboxUnavailable):
        pipeline.sync_gmail_accounts(workspace_id, connections=[{
            "id": "ca_wrong", "user_id": f"gmail:{workspace_id}", "status": "ACTIVE"}],
            intent="reconnect", mailbox_id="recipes")


def test_sync_refreshes_stored_accounts_across_unrelated_domains_without_org_inventory(mailbox_db, monkeypatch):
    db, mailboxes, workspace_id = mailbox_db
    from app.gtm.supymem import composio_gmail, pipeline
    from app.authz.workspace import ensure_subject_person
    from app.workspaces.service import create_workspace
    from app.content.oauth import callbacks
    monkeypatch.setenv("COMPOSIO_GMAIL_AUTH_CONFIG_ID", "auth_gmail")
    monkeypatch.setattr(callbacks, "known_connections", lambda *args, **kwargs: [])
    def account(account_id, user, **extra):
        return {"id": account_id, "user_id": user, "status": "ACTIVE", "toolkit": {"slug": "gmail"},
                "auth_config": {"id": "auth_gmail"}, **extra}
    calls = []
    def request(method, path, *, params):
        calls.append((path, params))
        assert path in {"/connected_accounts/ca_tools", "/connected_accounts/ca_recipes"}
        return {"data": account(path.rsplit("/", 1)[-1], f"gmail:{workspace_id}")}
    monkeypatch.setattr(composio_gmail, "_request", request)
    monkeypatch.setattr(composio_gmail, "get_gmail_profile", lambda user, account: {
        "email": "you@buildtools.example" if account == "ca_tools" else "you@homecooking.example"})
    result = pipeline.sync_gmail_accounts(workspace_id)
    assert result["synced"] == 2
    assert calls == [("/connected_accounts/ca_recipes", None), ("/connected_accounts/ca_tools", None)]
    from app.db.models import GtmEmailAccount
    from sqlalchemy import select
    with db()() as session:
        rows = {row.connected_account_id: row for row in session.scalars(select(GtmEmailAccount))}
        assert rows["ca_tools"].workspace_id == rows["ca_recipes"].workspace_id == workspace_id
        assert rows["ca_foreign"].workspace_id != workspace_id


@pytest.mark.parametrize("failure", ["owner", "auth", "shared", "revoked", "revoked_active", "revoked_receipt"])
def test_sync_invalidates_only_the_changed_mailbox_and_never_revives_a_disconnected_one(mailbox_db, monkeypatch, failure):
    db, mailboxes, workspace_id = mailbox_db
    from app.gtm.supymem import composio_gmail, pipeline
    from app.connections.composio_migration import mark_gmail_reconnect
    from app.content.oauth import callbacks
    from app.db.models import GtmEmailAccount
    monkeypatch.setenv("COMPOSIO_GMAIL_AUTH_CONFIG_ID", "auth_owned")
    monkeypatch.setattr(callbacks, "known_connections", lambda *args, **kwargs: [])
    tools = {"id": "ca_tools", "user_id": f"gmail:{workspace_id}", "status": "ACTIVE",
             "toolkit": {"slug": "gmail"}, "auth_config": {"id": "auth_owned"}}
    recipes = {**tools, "id": "ca_recipes"}
    if failure == "owner":
        tools["user_id"] = "gmail:another_founder"
    elif failure == "auth":
        tools["auth_config"] = {"id": "foreign_auth"}
    elif failure == "shared":
        tools["experimental"] = {"account_type": "SHARED"}
    else:
        tools["status"] = "INACTIVE" if failure == "revoked" else "ACTIVE"
        with db()() as session:
            session.get(GtmEmailAccount, "tools").status = "revoked"
            session.commit()
    def request(method, path, **kwargs):
        assert path in {"/connected_accounts/ca_tools", "/connected_accounts/ca_recipes"}
        return {"data": tools if path.endswith("ca_tools") else recipes}
    monkeypatch.setattr(composio_gmail, "_request", request)
    assert pipeline.sync_gmail_accounts(workspace_id)["synced"] == 1
    if failure.startswith("revoked"):
        mark_gmail_reconnect("ca_tools")
        if failure == "revoked_receipt":
            assert pipeline.sync_gmail_accounts(workspace_id, connections=[tools], intent="add")["synced"] == 0
    with db()() as session:
        assert session.get(GtmEmailAccount, "tools").status == ("revoked" if failure.startswith("revoked") else "needs_reconnect")
        assert session.get(GtmEmailAccount, "recipes").status == "active"
        assert session.get(GtmEmailAccount, "foreign").status == "active"
        with pytest.raises(mailboxes.MailboxUnavailable):
            mailboxes.resolve_send_mailbox(session, workspace_id, mailbox_id="tools")


def test_per_mailbox_verification_receipts_and_disconnect_are_isolated(mailbox_db, monkeypatch):
    db, mailboxes, workspace_id = mailbox_db
    from app.connections import verification, hub
    from app.gtm.supymem import composio_gmail
    from app.content import composio_social
    from app.db.models import GtmEmailAccount
    calls = []
    def verify(user, account_id):
        calls.append(account_id)
        return {"successful": True, "log_id": f"log_{account_id}",
                "email": "you@buildtools.example" if account_id == "ca_tools" else "you@homecooking.example"}
    monkeypatch.setattr(composio_gmail, "verify_gmail_profile", verify)
    for mailbox_id in ("tools", "recipes"):
        result = verification.verify(workspace_id, actor_id="founder_tools", mailbox_id=mailbox_id)
        assert result["ok"] is True and result["account_id"] == mailbox_id
        assert verification.latest(workspace_id, mailbox_id=mailbox_id)["receipt"]["account_id"] == mailbox_id
    assert calls == ["ca_tools", "ca_recipes"]
    with pytest.raises(mailboxes.MailboxUnavailable):
        verification.verify(workspace_id, actor_id="founder_tools", mailbox_id="foreign")
    assert calls == ["ca_tools", "ca_recipes"]
    monkeypatch.setattr(hub, "disconnect_accounts", lambda wid, provider: {"tools": "ca_tools", "recipes": "ca_recipes"})
    removed = []
    monkeypatch.setattr(composio_social, "delete_connection", lambda connection_id, **kwargs: removed.append(connection_id))
    with db()() as session:
        version = session.get(GtmEmailAccount, "tools").updated_at.isoformat()
    with pytest.raises(ValueError, match="connection_confirmation_required"):
        hub.disconnect_mailbox(workspace_id, "tools", confirmed=False, expected_updated_at=version)
    with pytest.raises(ValueError, match="connection_changed_review_again"):
        hub.disconnect_mailbox(workspace_id, "tools", confirmed=True, expected_updated_at="stale")
    assert not removed
    hub.disconnect_mailbox(workspace_id, "tools", confirmed=True, expected_updated_at=version)
    assert removed == ["ca_tools"]
    with db()() as session:
        assert session.get(GtmEmailAccount, "tools").status == "revoked"
        assert session.get(GtmEmailAccount, "recipes").status == "active"


def test_reconnecting_one_inbox_leaves_other_jobs_drafts_and_notices_paused(mailbox_db):
    db, mailboxes, workspace_id = mailbox_db
    from app.connections.composio_migration import mark_gmail_reconnect, resume_gmail_work, gmail_notice
    from app.db.models import ChatMessage, GtmEmailAccount, Job, Venture, VentureDocument
    from sqlalchemy import select
    from core.clock import utcnow
    with db()() as session:
        session.add(Venture(id="venture_inboxes", workspace_id=workspace_id, name="Your business", raw_idea="Your business",
                            stage="draft", created_at=utcnow(), updated_at=utcnow()))
        for mailbox_id in ("tools", "recipes"):
            session.add(Job(id=f"job-{mailbox_id}", workspace_id=workspace_id, venture_id="venture_inboxes", kind="experiment.run",
                            status="pending", payload_json="{}", attempts=0, max_attempts=1,
                            idempotency_key=f"key-{mailbox_id}", created_at=utcnow()))
            session.add(VentureDocument(id=f"draft-{mailbox_id}", venture_id="venture_inboxes", kind="outreach_draft",
                                        title="Your email", created_at=utcnow(), updated_at=utcnow(),
                                        provenance_json=json.dumps({"mailbox_id": mailbox_id, "channel": "email", "state": "draft",
                                                                   "scheduled_send": {"job_id": f"job-{mailbox_id}", "approval_id": f"approval-{mailbox_id}"}})))
        session.commit()
    for mailbox_id in ("tools", "recipes"):
        mark_gmail_reconnect(f"ca_{mailbox_id}")
    with db()() as session:
        assert session.get(Job, "job-tools").status == session.get(Job, "job-recipes").status == "paused"
        account = session.get(GtmEmailAccount, "tools")
        account.status = "active"
        resume_gmail_work(session, account)
        gmail_notice(session, account, reconnected=True)
        session.commit()
    with db()() as session:
        assert session.get(Job, "job-tools").status == "pending"
        assert session.get(Job, "job-recipes").status == "paused"
        assert "error" not in json.loads(session.get(VentureDocument, "draft-tools").provenance_json)
        paused = json.loads(session.get(VentureDocument, "draft-recipes").provenance_json)
        assert paused["error"] == "gmail_reconnect_required"
        assert paused["scheduled_send"]["approval_id"] == "approval-recipes"
        unresolved = list(session.scalars(select(ChatMessage).where(ChatMessage.kind == "needs_you")))
        assert len(unresolved) == 1
        assert json.loads(unresolved[0].actions_json)[0]["payload"]["mailbox_id"] == "recipes"


def test_oauth_intent_is_bound_to_the_exact_owned_mailbox(mailbox_db, monkeypatch):
    db, mailboxes, workspace_id = mailbox_db
    from app.content.oauth import attempts, callbacks
    from app.gtm.supymem import composio_gmail
    from app.db.models import ContentOAuthState
    monkeypatch.setattr(attempts, "initiating_actor", lambda wid: "founder_tools")
    calls = []
    def create(user, *, callback):
        calls.append(user)
        return {"redirect_url": "https://connect.example/authorize", "app_user_id": user,
                "connection_id": f"ca_oauth_{len(calls)}", "auth_config_id": "auth_owned"}
    monkeypatch.setattr(composio_gmail, "create_gmail_connection", create)
    for intent, mailbox_id in (("add", None), ("reconnect", "tools"), ("reconnect", "recipes")):
        started = callbacks.start_managed(workspace_id=workspace_id, platform="gmail", intent=intent, mailbox_id=mailbox_id)
        with db()() as session:
            stored = session.get(ContentOAuthState, attempts.digest(started["state"]))
            assert (stored.gmail_intent, stored.gmail_mailbox_id) == (intent, mailbox_id)
            assert stored.actor_subject == "founder_tools" and stored.provider_user_id == f"gmail:{workspace_id}"
    with pytest.raises(attempts.OAuthError, match="This inbox is unavailable"):
        callbacks.start_managed(workspace_id=workspace_id, platform="gmail", intent="reconnect", mailbox_id="foreign")
    assert len(calls) == 3


def test_reconnect_callback_rechecks_target_access_before_provider_calls(mailbox_db, monkeypatch):
    db, mailboxes, workspace_id = mailbox_db
    from app.authz.workspace import ensure_subject_person
    from app.content.oauth import callbacks, attempts
    from app.db.models import WorkspaceMember
    from app.workspaces.service import create_workspace
    from core.clock import utcnow
    parent = create_workspace(kind="company", name="Another venture", owner_id="founder_tools")
    delegate = ensure_subject_person("delegate")
    with db()() as session:
        session.add(WorkspaceMember(id="member-parent", workspace_id=parent.id, person_id=delegate,
                                    role="admin", created_at=utcnow()))
        session.commit()
    calls = []
    monkeypatch.setattr(callbacks, "_verified_connection", lambda *args: calls.append(args))
    for mailbox_id in ("tools", "recipes"):
        with pytest.raises(attempts.OAuthError, match="Workspace access is no longer available"):
            callbacks._complete_managed({"platform": "gmail", "workspace_id": parent.id,
                                         "actor_subject": delegate, "gmail_intent": "reconnect", "gmail_mailbox_id": mailbox_id})
    assert not calls


def test_add_watch_does_not_complete_against_an_already_connected_inbox(mailbox_db, monkeypatch):
    db, mailboxes, workspace_id = mailbox_db
    from app.chat import connect
    from app.connections import hub
    from app.content.oauth.attempts import digest
    from app.db.models import ContentOAuthState, GtmEmailAccount, Venture
    from core.clock import utcnow
    import secrets
    state = secrets.token_urlsafe(32)
    with db()() as session:
        session.add(Venture(id="venture_watch", workspace_id=workspace_id, name="Your venture", raw_idea="Your venture",
                            stage="draft", created_at=utcnow(), updated_at=utcnow()))
        session.add(ContentOAuthState(id=digest(state), workspace_id=workspace_id, platform="gmail", actor_subject="founder_tools",
                                      auth_method="composio", provider_user_id=f"gmail:{workspace_id}", auth_config_id="auth_owned",
                                      connected_account_id="ca_watched", gmail_intent="add", redirect_uri="https://callback.example",
                                      expires_at=utcnow() + timedelta(minutes=15), created_at=utcnow()))
        session.commit()
    connect._save_watch("venture_watch", "gmail", workspace_id, oauth_state=state)
    monkeypatch.setattr(hub, "sync_provider", lambda *args: None)
    monkeypatch.setattr(connect, "_is_connected", lambda *args: True)
    assert connect.check("venture_watch", "gmail") == "waiting"
    with db()() as session:
        session.get(ContentOAuthState, digest(state)).outcome = "active"
        session.get(GtmEmailAccount, "recipes").connected_account_id = "ca_watched"
        session.commit()
    assert connect.check("venture_watch", "gmail") == "connected"


def test_console_mailbox_api_uses_local_ids_and_enforces_workspace_and_flag(mailbox_db, monkeypatch):
    db, mailboxes, workspace_id = mailbox_db
    from fastapi.testclient import TestClient
    from app import main
    from app.db.models import GtmEmailAccount
    from core.assertion import sign_assertion
    secret = "multi-gmail-test-assertion-secret-32-bytes"
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", secret)
    def headers(method, path, wid=workspace_id):
        return {"X-Workspace-Id": wid, "X-COS-Assertion": sign_assertion(secret, sub="founder_tools",
                email="you@buildtools.example", role="user", path=path, method=method)}
    with db()() as session:
        foreign_workspace_id = session.get(GtmEmailAccount, "foreign").workspace_id
    with TestClient(main.create_app()) as client:
        path = "/api/v1/connections/gmail/mailboxes"
        response = client.get(path, headers=headers("GET", path))
        assert response.status_code == 200, response.text
        listed = response.json()["mailboxes"]
        assert response.json()["workspace_id"] == workspace_id
        assert "workspace_default_mailbox_id" in response.json()
        assert all("daily_send_limit" in account and "cap" not in account for account in listed)
        assert {account["id"] for account in listed} == {"tools", "recipes"}
        assert all("connected_account_id" not in account and "updated_at" in account for account in listed)
        default_path = path + "/default"
        changed = client.post(default_path, headers=headers("POST", default_path), json={"mailbox_id": "tools"})
        assert changed.status_code == 200, changed.text
        assert client.post(default_path, headers=headers("POST", default_path), json={"mailbox_id": "foreign"}).status_code == 404
        assert client.get(path, headers=headers("GET", path, foreign_workspace_id)).status_code == 404
        from app.gtm.supymem import composio_gmail
        from app.content import composio_social
        monkeypatch.setattr(composio_gmail, "configured", lambda: True)
        monkeypatch.setattr(composio_gmail, "create_gmail_connection", lambda user, **kwargs: {
            "redirect_url": "https://connect.example/authorize", "app_user_id": user,
            "connection_id": "ca_new_oauth", "auth_config_id": "auth_owned"})
        add_path = path + "/add"
        added = client.post(add_path, headers=headers("POST", add_path))
        assert added.status_code == 200 and added.json()["intent"] == "add", added.text
        assert "ca_new_oauth" not in added.text
        reconnect_path = "/api/v1/connections/gmail/connect"
        reconnected = client.post(reconnect_path, headers=headers("POST", reconnect_path),
                                   json={"intent": "reconnect", "mailbox_id": "tools"})
        assert reconnected.status_code == 200 and reconnected.json()["mailbox_id"] == "tools", reconnected.text
        verified = []
        def profile(user, account_id):
            verified.append(account_id)
            return {"successful": True, "email": "you@homecooking.example", "log_id": "log_recipes"}
        monkeypatch.setattr(composio_gmail, "verify_gmail_profile", profile)
        verify_path = path + "/recipes/verify"
        verified_response = client.post(verify_path, headers=headers("POST", verify_path))
        assert verified_response.status_code == 200, verified_response.text
        assert verified_response.json()["account_id"] == "recipes" and verified == ["ca_recipes"]
        foreign_path = path + "/foreign/verify"
        assert client.post(foreign_path, headers=headers("POST", foreign_path)).status_code == 404
        assert verified == ["ca_recipes"]
        removed = []
        monkeypatch.setattr(composio_social, "delete_connection", lambda connection_id, **kwargs: removed.append(connection_id))
        version = next(account["updated_at"] for account in listed if account["id"] == "recipes")
        delete_path = path + "/recipes"
        deleted = client.request("DELETE", delete_path, headers=headers("DELETE", delete_path),
                                 json={"confirmed": True, "expected_updated_at": version})
        assert deleted.status_code == 200, deleted.text
        assert removed == ["ca_recipes"]
        remaining = {account["id"]: account for account in client.get(path, headers=headers("GET", path)).json()["mailboxes"]}
        assert remaining["tools"]["status"] == "active" and remaining["tools"]["is_default"] is True
        assert remaining["recipes"]["status"] == "revoked"
        monkeypatch.setattr(mailboxes, "get_policy", lambda key: {})
        disabled = client.get(path, headers=headers("GET", path))
        assert disabled.status_code == 200
        assert disabled.json() == {"multi_gmail_v1": False, "workspace_id": workspace_id, "mailboxes": []}
        assert client.get(path, headers=headers("GET", path, foreign_workspace_id)).status_code == 404
        assert client.post(default_path, headers=headers("POST", default_path), json={"mailbox_id": "tools"}).status_code == 404


def test_chat_add_reconnect_and_disconnect_bind_one_local_inbox(mailbox_db, monkeypatch):
    db, mailboxes, workspace_id = mailbox_db
    from app.chat import connect
    from app.connections import hub
    monkeypatch.setattr(connect, "_scope", lambda venture_id: (workspace_id, "you@buildtools.example"))
    monkeypatch.setattr(connect, "_is_connected", lambda *args: True)
    monkeypatch.setattr(connect, "_save_watch", lambda *args, **kwargs: None)
    calls = []
    def start(wid, provider, **kwargs):
        calls.append(kwargs)
        return {"redirect_url": "https://connect.example/authorize", "oauth_state": "new-attempt"}
    monkeypatch.setattr(hub, "connect_provider", start)
    assert connect.handle("venture", "add another gmail")["watch"] == "gmail"
    assert calls == [{"intent": "add"}]
    for mailbox_id in ("tools", "recipes"):
        assert connect.handle("venture", f"reconnect gmail inbox {mailbox_id}")["watch"] == "gmail"
        assert calls[-1] == {"intent": "reconnect", "mailbox_id": mailbox_id}
    assert connect.handle("venture", "reconnect gmail")["kind"] == "navigation"
    assert len(calls) == 3
    monkeypatch.setattr(hub, "disconnect_accounts", lambda *args: {"tools": "ca_tools", "recipes": "ca_recipes"})
    disconnected = []
    monkeypatch.setattr(hub, "disconnect_mailbox", lambda wid, mailbox_id, **kwargs:
                        disconnected.append((wid, mailbox_id, kwargs)))
    from app.gtm.supymem.pipeline import list_mailboxes
    accounts = {account["id"]: account for account in list_mailboxes(workspace_id)}
    assert connect.disconnect("venture", {"provider": "gmail"})["kind"] == "navigation"
    assert not disconnected
    for mailbox_id in ("tools", "recipes"):
        reply = connect.disconnect("venture", {"provider": "gmail", "mailbox_id": mailbox_id})
        assert disconnected[-1] == (workspace_id, mailbox_id,
            {"confirmed": True, "expected_updated_at": accounts[mailbox_id]["updated_at"]})
        assert reply["action_started"] and not any(action.get("confirmation") for action in reply["actions"])
        assert f"ca_{mailbox_id}" not in json.dumps(reply)
    assert len(disconnected) == 2


@pytest.mark.parametrize("original_status", ["active", "needs_reconnect"])
def test_first_add_bootstraps_the_existing_sender_not_the_new_inbox(mailbox_db, monkeypatch, original_status):
    db, mailboxes, workspace_id = mailbox_db
    from app.db.models import GtmEmailAccount, Workspace
    from app.gtm.supymem import composio_gmail, pipeline
    with db()() as session:
        session.delete(session.get(GtmEmailAccount, "recipes"))
        session.get(GtmEmailAccount, "tools").status = original_status
        assert session.get(Workspace, workspace_id).default_gmail_mailbox_id is None
        session.commit()
    monkeypatch.setattr(composio_gmail, "get_gmail_profile", lambda *args: {"email": "you@homecooking.example"})
    result = pipeline.sync_gmail_accounts(workspace_id, connections=[{
        "id": "ca_second", "user_id": f"gmail:{workspace_id}", "status": "ACTIVE"}], intent="add")
    assert result["synced"] == 1
    with db()() as session:
        assert session.get(Workspace, workspace_id).default_gmail_mailbox_id == "tools"
        assert session.get(GtmEmailAccount, "tools").status == original_status
        if original_status == "needs_reconnect":
            with pytest.raises(mailboxes.MailboxUnavailable):
                mailboxes.resolve_send_mailbox(session, workspace_id)
        else:
            assert mailboxes.resolve_send_mailbox(session, workspace_id)["mailbox_id"] == "tools"


@pytest.mark.parametrize("capped", [False, True])
def test_queue_keeps_pinned_senders_after_default_switch_and_skips_only_capped_inbox(mailbox_db, monkeypatch, capped):
    db, mailboxes, workspace_id = mailbox_db
    from app.db.models import GtmContact, GtmEmailSendQueue
    from app.gtm.localization import experiments
    from app.gtm.supymem import composio_gmail, pipeline
    from core.clock import utcnow
    now = utcnow()
    with db()() as session:
        mailboxes.set_default_mailbox(session, workspace_id, "recipes")
        for offset, mailbox_id in enumerate(("foreign", "tools", "recipes")):
            contact_id = f"queue_contact_{mailbox_id}"
            session.add(GtmContact(
                id=contact_id, workspace_id=workspace_id, email=f"recipient@{mailbox_id}.example",
                stage_changed_at=now, created_at=now,
            ))
            session.add(GtmEmailSendQueue(
                id=f"queue_{mailbox_id}", workspace_id=workspace_id, contact_id=contact_id,
                email_account_id=mailbox_id, sequence_day=1, status="queued", attempts=0,
                scheduled_for=now - timedelta(seconds=3 - offset),
                idempotency_key=f"queue:{mailbox_id}", created_at=now, updated_at=now,
            ))
        session.commit()
    monkeypatch.setattr(experiments, "execution_context", lambda wid: {"allowed": True})
    monkeypatch.setattr(pipeline, "_dry_run", lambda: True)
    monkeypatch.setattr(pipeline, "mailbox_send_max", lambda *args: 1)
    monkeypatch.setattr(composio_gmail, "mailbox_slots_used_today", lambda session, account: int(capped and account.id == "tools"))
    sent = []
    def send(job_id, **kwargs):
        sent.append(job_id)
        return {"sent": True}
    monkeypatch.setattr(pipeline, "_send_one", send)
    pipeline.tick_send_queue(workspace_id, limit=2)
    assert sent == (["queue_recipes"] if capped else ["queue_tools", "queue_recipes"])
    with db()() as session:
        assert session.get(GtmEmailSendQueue, "queue_foreign").status == "queued"
        assert session.get(GtmEmailSendQueue, "queue_tools").email_account_id == "tools"


@pytest.mark.parametrize("mailbox_id", ["tools", "recipes"])
@pytest.mark.parametrize("failure", ["revoked", "foreign", "invalid_email", "conversation", "unbound"])
def test_send_revalidates_the_pinned_mailbox_without_fallback(mailbox_db, mailbox_id, failure):
    db, mailboxes, workspace_id = mailbox_db
    from app.db.models import GtmContact, GtmEmailAccount, GtmEmailEvent, GtmEmailSendQueue
    from app.gtm.supymem import pipeline
    from core.clock import utcnow
    now = utcnow()
    other_mailbox_id = "recipes" if mailbox_id == "tools" else "tools"
    with db()() as session:
        mailboxes.set_default_mailbox(session, workspace_id, other_mailbox_id)
        if failure == "revoked":
            session.get(GtmEmailAccount, mailbox_id).status = "revoked"
        elif failure == "invalid_email":
            session.get(GtmEmailAccount, mailbox_id).email = "invalid"
        session.add(GtmContact(
            id="send_contact", workspace_id=workspace_id, email="recipient@buildtools.example",
            stage_changed_at=now, created_at=now,
        ))
        session.add(GtmEmailSendQueue(
            id="send_job", workspace_id=workspace_id, contact_id="send_contact",
            email_account_id="foreign" if failure == "foreign" else mailbox_id,
            sequence_day=2 if failure in {"conversation", "unbound"} else 1,
            status="sending", attempts=1, scheduled_for=now, idempotency_key="send:test",
            created_at=now, updated_at=now,
        ))
        if failure == "conversation":
            session.add(GtmEmailEvent(id="original-send", workspace_id=workspace_id, contact_id="send_contact",
                                     email_account_id=other_mailbox_id, event_type="SENT", created_at=now))
        session.commit()
    assert pipeline._send_one_admitted("send_job", simulated=False) == {
        "skipped": True, "reason": "mailbox_unavailable", "contact_id": "send_contact"}
    with db()() as session:
        job = session.get(GtmEmailSendQueue, "send_job")
        assert job.status == "queued" and job.email_account_id != other_mailbox_id

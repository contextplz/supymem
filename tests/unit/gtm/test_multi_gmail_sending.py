import asyncio
import json
from contextlib import contextmanager
from datetime import timedelta

import pytest
from sqlalchemy import select

from tests.unit.gtm.test_journey import journey_db


pytestmark = pytest.mark.hosted_only


@pytest.fixture(params=[0, 1], ids=["freight-founder", "family-founder"])
def inboxes(journey_db, monkeypatch, request):
    from app.authz.workspace import ensure_subject_person
    from app.credits.accounts import ensure_account
    from app.credits.policy import set_policy
    from app.db.models import CreditAccount, GtmContact, GtmEmailAccount, Venture
    from app.gtm.relationships.service import _settings
    from app.workspaces.service import create_workspace
    from core.clock import utcnow
    data = journey_db
    second_actor = "usr_family_inboxes"
    ensure_subject_person(second_actor)
    second = create_workspace(kind="company", name="Family planning", owner_id=second_actor)
    now = utcnow()
    founders = [(data["wid"], data["vid"], data["actor"], "dispatch.example", "Freight coordinators"),
                (second.id, "ven_family_inboxes", second_actor, "picnic.example", "Families planning outdoor meals")]
    with data["factory"]() as session:
        session.add(Venture(id=founders[1][1], workspace_id=second.id, name="Outdoor meals",
            raw_idea="Help families plan outdoor meals", stage="draft", created_at=now, updated_at=now))
        session.get(Venture, data["vid"]).raw_idea = "Help freight coordinators plan deliveries"
        session.commit()
    for index, (workspace_id, venture_id, actor, domain, audience) in enumerate(founders):
        credit = ensure_account(venture_id, workspace_id=workspace_id, owner_person_id=actor)
        with data["factory"]() as session:
            session.get(CreditAccount, credit.id).granted_micro = 10_000_000
            _settings(session, workspace_id).sender_postal_address = "42 Market Street, Toronto, ON, Canada"
            for suffix, email_domain in (("a", domain), ("b", f"alternate-{domain}")):
                session.add(GtmEmailAccount(id=f"inbox-{index}-{suffix}", workspace_id=workspace_id,
                    email=f"you@{email_domain}", composio_user_id=f"user-{index}-{suffix}",
                    connected_account_id=f"ca-{index}-{suffix}", status="active", daily_send_limit=40,
                    created_at=now - timedelta(days=49), updated_at=now))
            session.add(GtmContact(id=f"contact-{index}", workspace_id=workspace_id,
                email=f"person@{domain}", email_confidence="safe", recipient_geo="CA", lawful_basis="consent",
                lawful_basis_at=now, lawful_basis_note="You have recorded their permission for this message.",
                source=f"venture:{venture_id}:public", enrichment_json=json.dumps({"source_url": f"https://{domain}/contact"}),
                stage_changed_at=now, created_at=now))
            session.commit()
    set_policy("feature_flags", {"multi_gmail_v1": True}, "test")
    from app.autopilot import outreach
    monkeypatch.setattr(outreach, "_brief", lambda vid: {"idea": {"audience": next(
        founder[4] for founder in founders if founder[1] == vid)}})
    monkeypatch.setattr(outreach, "site_url", lambda vid: None)
    selected = request.param
    workspace_id, venture_id, actor, domain, audience = founders[selected]
    from app.tenancy import bind
    with bind(workspace_id=workspace_id, venture_id=venture_id, actor=actor):
        yield {**data, "wid": workspace_id, "vid": venture_id, "actor": actor, "domain": domain,
                "audience": audience, "contact": f"contact-{selected}", "a": f"inbox-{selected}-a",
                "b": f"inbox-{selected}-b", "foreign": f"inbox-{1-selected}-a"}
    set_policy("feature_flags", {"multi_gmail_v1": False}, "test")


def draft(data, **changes):
    from app.autopilot import outreach
    body = {"contact_id": data["contact"], "to": f"person@{data['domain']}", "channel": "email", "day": 1,
            "subject": "A question for you", "body": "Would you like to hear more?", "state": "draft",
            "batch_id": "review-inbox", "mailbox_id": data["a"], **changes}
    document_id = outreach._save_draft(data["vid"], body)
    return next(saved for saved in outreach.drafts(data["vid"]) if saved["doc_id"] == document_id)


def test_outreach_api_ignores_legacy_extra_mailbox_values_with_flags_off(inboxes, monkeypatch):
    from app.api import ops_api
    from app.credits.policy import set_policy
    from app.gtm.supymem import pipeline
    set_policy("feature_flags", {"multi_gmail_v1": False, "inbox_briefing_v1": False}, "test")
    expected = {"enrolled": [inboxes["contact"]], "queued": 1, "dry_run": True}
    calls = []
    monkeypatch.setattr(ops_api, "_auth_workspace", lambda *args: inboxes["wid"])
    monkeypatch.setattr(ops_api, "audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(pipeline, "start_outreach", lambda workspace_id, **kwargs: calls.append((workspace_id, kwargs)) or expected)
    for mailbox_id in (None, "", inboxes["foreign"], [], {}, False, 123):
        body = ops_api.AiGtmOutreachBody(contact_ids=[inboxes["contact"]], mailbox_id=mailbox_id)
        assert json.dumps(ops_api.ai_gtm_outreach_start(body, workspace_id=inboxes["wid"])) == json.dumps(expected)
        assert calls[-1] == (inboxes["wid"], {"contact_ids": [inboxes["contact"]], "auto_send": False,
            "approval_id": None, "product_name": None})
    set_policy("feature_flags", {"multi_gmail_v1": True}, "test")
    with pytest.raises(ValueError):
        ops_api.AiGtmOutreachBody(mailbox_id=[])
    assert ops_api.AiGtmOutreachBody(mailbox_id=inboxes["b"]).mailbox_id == inboxes["b"]


def test_draft_sender_is_persisted_before_review_and_flag_off_is_legacy(inboxes):
    from app.autopilot import outreach
    from app.credits.policy import set_policy
    saved = draft(inboxes, mailbox_id=inboxes["b"])
    assert saved["sender"]["mailbox_id"] == saved["mailbox_id"] == inboxes["b"]
    assert saved["sender"]["email"] in saved["body"]
    assert f"Your email address is listed publicly on {inboxes['domain']}." in saved["body"]
    assert outreach.OPT_OUT in saved["body"]
    set_policy("feature_flags", {"multi_gmail_v1": False}, "test")
    legacy = draft(inboxes, mailbox_id=None, batch_id="legacy-inbox")
    assert "sender" not in legacy
    assert set(outreach._mailbox(inboxes["wid"])) == {"user", "account", "email", "workspace_id"}


def test_batch_drafting_honors_the_explicit_sender_and_reviews_the_final_footer(inboxes, monkeypatch):
    from app.autopilot import outreach
    async def no_model(*args, **kwargs):
        return {}
    monkeypatch.setattr(outreach, "_llm_drafts", no_model)
    monkeypatch.setattr(outreach, "saved_pitch", lambda vid: f"Help {inboxes['audience'].lower()} coordinate plans")
    reply = asyncio.run(outreach.draft_batch(inboxes["vid"], 1, mailbox_id=inboxes["b"]))
    saved = outreach.drafts(inboxes["vid"])[0]
    assert saved["mailbox_id"] == inboxes["b"]
    assert saved["sender"]["email"] in reply["body_md"]
    assert saved["sender"]["email"] in saved["body"]
    assert reply["actions"][0]["confirmation"]["resulting_state"]["draft_ids"] == [saved["doc_id"]]


def test_review_projection_exposes_local_mailbox_ids_only(inboxes):
    from app.credits.policy import set_policy
    from app.gtm import journey
    saved = draft(inboxes)
    saved["scheduled_send"] = {"sender": saved["sender"], "mailbox_id": saved["mailbox_id"]}
    public = journey._public_draft(saved)
    assert set(public["sender"]) == set(public["scheduled_send"]["sender"]) == {"mailbox_id", "email"}
    assert public["sender"]["mailbox_id"] == inboxes["a"]
    assert "account" in saved["sender"]
    set_policy("feature_flags", {"multi_gmail_v1": False}, "test")
    assert journey._public_draft(saved) is saved


def test_sender_resolution_uses_the_draft_writers_transaction(inboxes, monkeypatch):
    from app.autopilot import outreach
    from app.connections import mailboxes
    from app.db.models import Workspace
    original = mailboxes.resolve_send_mailbox
    sessions = []
    def initialize_default(session, workspace_id, **kwargs):
        sessions.append(session)
        session.get(Workspace, workspace_id).name = "Initialized in the draft transaction"
        return original(session, workspace_id, **kwargs)
    monkeypatch.setattr(mailboxes, "resolve_send_mailbox", initialize_default)
    with inboxes["factory"]() as session:
        session.get(Workspace, inboxes["wid"]).name = "Draft write in progress"
        session.flush()
        sender = outreach.draft_mailbox(inboxes["wid"], inboxes["vid"], {"mailbox_id": inboxes["a"], "day": 1}, session=session)
        assert sender["mailbox_id"] == inboxes["a"] and sessions == [session]
        session.commit()
    with inboxes["factory"]() as session:
        assert session.get(Workspace, inboxes["wid"]).name == "Initialized in the draft transaction"


def test_real_defaults_and_draft_api_bind_the_selected_local_inbox(inboxes, monkeypatch):
    from app.autopilot import outreach
    from app.api import gtm_god_ops
    from app.api import ops_api
    from app.connections import mailboxes
    from app.gtm import journey
    with inboxes["factory"]() as session:
        mailboxes.set_default_mailbox(session, inboxes["wid"], inboxes["a"])
        mailboxes.set_default_mailbox(session, inboxes["wid"], inboxes["b"], venture_id=inboxes["vid"])
        session.commit()
    monkeypatch.setattr(ops_api, "_auth_workspace", lambda *args: inboxes["wid"])
    listed = ops_api.connections_gmail_mailboxes(venture_id=inboxes["vid"])
    assert listed["workspace_id"] == inboxes["wid"] and listed["venture_id"] == inboxes["vid"]
    assert listed["workspace_default_mailbox_id"] == inboxes["a"] and listed["venture_default_mailbox_id"] == inboxes["b"]
    assert [mailbox["id"] for mailbox in listed["mailboxes"] if mailbox["is_default"]] == [inboxes["b"]]
    saved = draft(inboxes, mailbox_id=None, review_state="approved")
    assert saved["mailbox_id"] == inboxes["b"]
    monkeypatch.setattr(gtm_god_ops, "_auth_workspace", lambda *args: inboxes["wid"])
    monkeypatch.setattr(journey, "authority", lambda *args, **kwargs: inboxes["actor"])
    monkeypatch.setattr(journey, "status", lambda *args, **kwargs: {})
    body = gtm_god_ops.JourneyDraftBody(expected_revision=journey._draft_revision(saved), mailbox_id=inboxes["a"])
    gtm_god_ops.journey_patch_draft(inboxes["vid"], saved["doc_id"], body)
    changed = outreach.drafts(inboxes["vid"])[0]
    assert changed["mailbox_id"] == inboxes["a"] and changed["review_state"] == "draft"


def test_journey_http_projection_uses_the_local_mailbox_contract_only_when_enabled(inboxes):
    from app.api.gtm_god_ops import _journey_call
    from app.credits.policy import set_policy
    saved = {"gmail": {"connected": True, "sender": f"you@{inboxes['domain']}"}}
    projected = _journey_call(lambda wid: saved, inboxes["wid"])
    assert projected["gmail"]["multi_gmail_v1"] is True
    assert {mailbox["id"] for mailbox in projected["gmail"]["mailboxes"]} == {inboxes["a"], inboxes["b"]}
    assert all("daily_send_limit" in mailbox and "updated_at" in mailbox and "connected_account_id" not in mailbox
        for mailbox in projected["gmail"]["mailboxes"])
    set_policy("feature_flags", {"multi_gmail_v1": False, "inbox_briefing_v1": False}, "test")
    assert _journey_call(lambda wid: saved, inboxes["wid"]) is saved


def test_test_email_binds_secondary_inbox_before_approval_and_never_substitutes(inboxes, monkeypatch):
    from app.approvals import external
    from app.connections import mailboxes
    from app.db.models import GtmEmailAccount
    from app.gtm.supymem import pipeline
    monkeypatch.setattr(pipeline, "_dry_run", lambda: False)
    monkeypatch.setattr(pipeline, "mail_readiness", lambda wid: {"configured": True, "connected": True})
    approvals = []
    monkeypatch.setattr(external, "consume_external_approval", lambda **kwargs: approvals.append(kwargs) or (False, "approval_required"))
    arguments = {"to": f"person@{inboxes['domain']}", "subject": "A question", "body": "Would you like to hear more?", "confirm": True}
    result = pipeline.send_test_email(inboxes["wid"], mailbox_id=inboxes["b"], **arguments)
    assert result["error"] == "approval_required" and approvals[0]["target"]["mailbox_id"] == inboxes["b"]
    with inboxes["factory"]() as session:
        mailboxes.set_default_mailbox(session, inboxes["wid"], inboxes["a"])
        session.get(GtmEmailAccount, inboxes["b"]).status = "revoked"
        session.commit()
    for mailbox_id in (inboxes["b"], inboxes["foreign"]):
        with pytest.raises(mailboxes.MailboxUnavailable):
            pipeline.send_test_email(inboxes["wid"], mailbox_id=mailbox_id, **arguments)
    assert len(approvals) == 1


def test_legacy_enrollment_and_tick_share_the_resolved_secondary_inbox(inboxes, monkeypatch):
    from app.db.models import GtmEmailSendQueue, GtmTouch
    from app.gtm.supymem import pipeline
    calls = []
    tick = pipeline.tick_send_queue
    monkeypatch.setattr(pipeline, "_dry_run", lambda: True)
    monkeypatch.setattr(pipeline, "tick_send_queue", lambda wid, **kwargs: calls.append(kwargs) or {"processed": []})
    result = pipeline.start_outreach(inboxes["wid"], contact_ids=[inboxes["contact"]], mailbox_id=inboxes["b"], auto_send=True)
    assert result["enrolled"] == [inboxes["contact"]]
    with inboxes["factory"]() as session:
        queued = session.get(GtmEmailSendQueue, result["queue_ids"][0])
        assert queued.email_account_id == calls[0]["mailbox_id"] == inboxes["b"]
    monkeypatch.setattr(pipeline, "tick_send_queue", tick)
    receipt = pipeline.tick_send_queue(inboxes["wid"], mailbox_id=inboxes["b"])
    assert len(receipt["processed"]) == 1 and receipt["processed"][0]["simulated"] is True
    with inboxes["factory"]() as session:
        sent = session.scalar(select(GtmTouch).where(GtmTouch.contact_id == inboxes["contact"], GtmTouch.direction == "out"))
        assert sent.email_account_id == inboxes["b"]


def test_confirmed_delivery_persists_the_native_inbox_on_its_touch(inboxes):
    from app.autopilot import outreach
    from app.connections.mailboxes import mailbox_for_conversation
    from app.db.models import GtmTouch
    saved = draft(inboxes, mailbox_id=inboxes["b"])
    with inboxes["factory"]() as session:
        touch_id = outreach._record_sent(session, inboxes["vid"], inboxes["wid"], saved,
            {"provider_message_id": "confirmed-message", "thread_id": "confirmed-thread"})
        session.commit()
        touch = session.get(GtmTouch, touch_id)
        assert touch.email_account_id == inboxes["b"] and touch.message_id == "confirmed-message"
        assert mailbox_for_conversation(session, contact_id=inboxes["contact"], thread_id="confirmed-thread")["mailbox_id"] == inboxes["b"]


def test_both_flags_off_preserve_legacy_api_and_sender_bytes(inboxes, monkeypatch):
    from app.autopilot import outreach
    from app.api.gtm_god_ops import JourneyDraftBody
    from app.credits.policy import set_policy
    from app.gtm.supymem import pipeline
    from app.ui_config import ui_config
    from pydantic import ValidationError
    set_policy("feature_flags", {"multi_gmail_v1": False, "inbox_briefing_v1": False}, "test")
    with inboxes["factory"]() as session:
        account = pipeline._active_mailbox(session, inboxes["wid"])
        expected = {"user": account.composio_user_id, "account": account.connected_account_id,
            "email": account.email, "workspace_id": inboxes["wid"]}
    assert json.dumps(outreach._mailbox(inboxes["wid"], mailbox_id=inboxes["foreign"]), sort_keys=True) == json.dumps(expected, sort_keys=True)
    assert json.dumps(ui_config(inboxes["actor"]), separators=(",", ":")) == '{"founder_ui":"chat"}'
    for mailbox_id in (inboxes["b"], None):
        with pytest.raises(ValidationError) as rejected:
            JourneyDraftBody(expected_revision="a" * 64, mailbox_id=mailbox_id)
        assert rejected.value.errors()[0]["type"] == "extra_forbidden"


@pytest.mark.parametrize("failure", ["foreign", "revoked", "missing", "identity-swapped", "invalid-selector", "invalid-email"])
def test_unavailable_or_changed_binding_never_calls_provider(inboxes, monkeypatch, failure):
    from app.autopilot import outreach
    from app.connections.mailboxes import MailboxUnavailable
    from app.db.models import GtmEmailAccount
    saved = draft(inboxes)
    with inboxes["factory"]() as session:
        if failure == "revoked":
            session.get(GtmEmailAccount, inboxes["a"]).status = "needs_reconnect"
        if failure == "identity-swapped":
            session.get(GtmEmailAccount, inboxes["a"]).connected_account_id = "replacement-identity"
        if failure == "invalid-email":
            session.get(GtmEmailAccount, inboxes["a"]).email = "not-an-address"
        session.commit()
    if failure in {"foreign", "missing"}:
        saved.pop("sender")
        saved["mailbox_id"] = inboxes["foreign"] if failure == "foreign" else "missing-inbox"
    if failure == "invalid-selector":
        saved.pop("sender")
        saved["mailbox_id"] = 0
    monkeypatch.setattr(outreach, "_send_one", lambda *args: pytest.fail("No wrong-inbox call"))
    with pytest.raises(MailboxUnavailable):
        outreach.draft_mailbox(inboxes["wid"], inboxes["vid"], saved)
    assert [action["label"] for action in outreach.mailbox_failure(inboxes["vid"])["actions"]] == ["Choose another inbox", "Reconnect"]


def test_batch_digest_binds_each_sender_but_not_the_current_default(inboxes, monkeypatch):
    from app.autopilot import outreach
    from app.gtm import journey
    from app.gtm.supymem import pipeline
    from app.db.models import GtmEmailAccount, GtmContact
    first = draft(inboxes, review_state="approved")
    second = draft(inboxes, mailbox_id=inboxes["b"], review_state="approved")
    with inboxes["factory"]() as session:
        contacts = {inboxes["contact"]: session.get(GtmContact, inboxes["contact"])}
    context = {"drafts": [first, second], "contacts": contacts, "mailbox": first["sender"], "cap": 5,
               "quote": {"credit_estimate_micro": 1}, "postal": "42 Market Street"}
    reviewed = journey._batch_review(inboxes["vid"], "review-inbox", context=context)
    monkeypatch.setattr(pipeline, "_active_mailbox", lambda session, wid: session.get(GtmEmailAccount, inboxes["b"]))
    context["mailbox"] = second["sender"]
    assert journey._batch_review(inboxes["vid"], "review-inbox", context=context)["approval_digest"] == reviewed["approval_digest"]
    assert outreach.draft_mailbox(inboxes["wid"], inboxes["vid"], first) == first["sender"]
    context["drafts"][0] = {**first, "sender": second["sender"], "mailbox_id": inboxes["b"]}
    assert journey._batch_review(inboxes["vid"], "review-inbox", context=context)["approval_digest"] != reviewed["approval_digest"]


def test_approved_send_uses_the_bound_inbox_after_default_switch(inboxes, monkeypatch):
    from app.approvals.external import propose_external_approval
    from app.approvals.service import decide
    from app.authz.workspace import subject_person_id
    from app.autopilot import outreach
    from app.db.models import GtmEmailAccount, GtmEmailEvent
    from app.gtm.supymem import composio_gmail, pipeline
    from app.tenancy import bind
    saved = draft(inboxes, review_state="approved", requires_journey_review=True)
    with bind(workspace_id=inboxes["wid"], venture_id=inboxes["vid"], actor=inboxes["actor"]):
        approval = propose_external_approval(workspace_id=inboxes["wid"], subject_ref=saved["doc_id"],
            action="gtm_email_send", target=outreach.journey_send_target(inboxes["vid"], saved, saved["sender"]),
            summary="Send your reviewed email", review={"sender": saved["sender"]["email"]})
        decide(approval["id"], decision="approved", decided_by=subject_person_id(inboxes["actor"]), surface="internal")
        monkeypatch.setattr(pipeline, "_active_mailbox", lambda session, wid: session.get(GtmEmailAccount, inboxes["b"]))
        calls = []
        monkeypatch.setattr(composio_gmail, "_send_gmail_email", lambda **kwargs: calls.append(kwargs) or
            {"provider_message_id": "confirmed-message", "thread_id": "confirmed-thread"})
        token = outreach._DELIVERY_DRAFT_ID.set(saved["doc_id"])
        try:
            receipt = outreach._journey_send_one(inboxes["vid"], saved, saved["sender"], approval["id"], None)
        finally:
            outreach._DELIVERY_DRAFT_ID.reset(token)
    assert receipt["provider_message_id"] == "confirmed-message"
    assert [call["connected_account_id"] for call in calls] == [saved["sender"]["account"]]
    with inboxes["factory"]() as session:
        event = session.scalar(select(GtmEmailEvent).where(GtmEmailEvent.event_type == "gmail_sent"))
        assert event.email_account_id == inboxes["a"]


def test_changing_sender_resets_review_and_updates_footer(inboxes, monkeypatch):
    from app.autopilot import outreach
    from app.gtm import journey
    saved = draft(inboxes, review_state="approved")
    monkeypatch.setattr(journey, "authority", lambda *args, **kwargs: inboxes["actor"])
    monkeypatch.setattr(journey, "status", lambda *args, **kwargs: {})
    journey.patch_draft(inboxes["wid"], inboxes["vid"], saved["doc_id"], {"mailbox_id": inboxes["b"],
        "decision": "approved", "expected_revision": journey._draft_revision(saved)})
    changed = outreach.drafts(inboxes["vid"])[0]
    assert changed["review_state"] == "draft" and changed["mailbox_id"] == inboxes["b"]
    assert saved["sender"]["email"] not in changed["body"]
    assert changed["sender"]["email"] in changed["body"]


def test_reselecting_a_reconnected_inbox_requires_fresh_review(inboxes, monkeypatch):
    from app.autopilot import outreach
    from app.db.models import GtmEmailAccount
    from app.gtm import journey
    from app.tenancy import bind
    saved = draft(inboxes, review_state="approved")
    with inboxes["factory"]() as session:
        session.get(GtmEmailAccount, inboxes["a"]).connected_account_id = "reconnected-provider-account"
        session.commit()
    monkeypatch.setattr(journey, "authority", lambda *args, **kwargs: inboxes["actor"])
    monkeypatch.setattr(journey, "status", lambda *args, **kwargs: {})
    with bind(workspace_id=inboxes["wid"], venture_id=inboxes["vid"], actor=inboxes["actor"]):
        journey.patch_draft(inboxes["wid"], inboxes["vid"], saved["doc_id"], {"mailbox_id": inboxes["a"],
            "decision": "approved", "expected_revision": journey._draft_revision(saved)})
    revised = next(row for row in outreach.drafts(inboxes["vid"]) if row["doc_id"] == saved["doc_id"])
    assert revised["review_state"] == "draft" and revised["mailbox_id"] == saved["mailbox_id"]
    assert revised["sender"]["account"] == "reconnected-provider-account"
    assert outreach.draft_mailbox(inboxes["wid"], inboxes["vid"], revised) == revised["sender"]


def test_manual_batch_confirmation_cannot_authorize_a_changed_sender(inboxes, monkeypatch):
    from app.autopilot import outreach
    saved = draft(inboxes)
    reviewed = outreach._manual_review(inboxes["vid"], saved["batch_id"])
    payload = {"batch_id": saved["batch_id"], "approved_by_founder": True,
               "draft_ids": [saved["doc_id"]], "mailbox_digest": outreach._manual_digest(reviewed)}
    different = outreach._mailbox(inboxes["wid"], mailbox_id=inboxes["b"])
    outreach._save_draft(inboxes["vid"], {**saved, "sender": different, "mailbox_id": inboxes["b"]}, saved["doc_id"])
    monkeypatch.setattr(outreach, "_send_one", lambda *args: pytest.fail("Changed sender must be reviewed again"))
    reply = asyncio.run(outreach.send_batch(inboxes["vid"], payload, actor_subject=inboxes["actor"]))
    assert reply["kind"] == "needs_you" and "changed after review" in reply["body_md"]


def test_disabling_the_flag_does_not_replace_an_already_bound_sender(inboxes, monkeypatch):
    from app.autopilot import outreach
    from app.credits.policy import set_policy
    from app.db.models import GtmEmailAccount
    from app.gtm.supymem import pipeline
    saved = draft(inboxes)
    reviewed = outreach._manual_review(inboxes["vid"], saved["batch_id"])
    set_policy("feature_flags", {"multi_gmail_v1": False}, "test")
    monkeypatch.setattr(pipeline, "_active_mailbox", lambda session, wid: session.get(GtmEmailAccount, inboxes["b"]))
    monkeypatch.setattr(outreach, "_send_one", lambda *args: pytest.fail("Never substitute the legacy default"))
    reply = asyncio.run(outreach.send_batch(inboxes["vid"], {"batch_id": saved["batch_id"], "approved_by_founder": True,
        "draft_ids": [saved["doc_id"]], "mailbox_digest": outreach._manual_digest(reviewed)}, actor_subject=inboxes["actor"]))
    assert reply["code"] == "mailbox_unavailable"


@pytest.mark.parametrize("scope", ["missing", "foreign"])
def test_provider_boundary_never_infers_or_crosses_the_founders_workspace(inboxes, monkeypatch, scope):
    from app.db.models import GtmEmailAccount
    from app.gtm.supymem import composio_gmail
    with inboxes["factory"]() as session:
        account = session.get(GtmEmailAccount, inboxes["foreign"] if scope == "foreign" else inboxes["a"])
        user_id, connection_id = account.composio_user_id, account.connected_account_id
    monkeypatch.setattr(composio_gmail, "_send_gmail_email", lambda **kwargs: pytest.fail("No unauthorized provider call"))
    with pytest.raises(composio_gmail.DefiniteFailure, match="mailbox_unavailable"):
        composio_gmail.send_gmail_email(composio_user_id=user_id, connected_account_id=connection_id,
            to=f"person@{inboxes['domain']}", subject="A question", body="May I share more?",
            workspace_id=None if scope == "missing" else inboxes["wid"])


def test_manual_batch_send_gets_an_exact_metered_mailbox_approval(inboxes, monkeypatch):
    from app.autopilot import outreach
    from app.db.models import Approval
    from app.gtm.supymem import composio_gmail
    from app.tenancy import bind
    saved = draft(inboxes)
    reviewed = outreach._manual_review(inboxes["vid"], saved["batch_id"])
    calls = []
    monkeypatch.setattr(composio_gmail, "_send_gmail_email", lambda **kwargs: calls.append(kwargs) or
        {"provider_message_id": "manual-confirmed", "thread_id": "manual-thread"})
    with bind(workspace_id=inboxes["wid"], venture_id=inboxes["vid"], actor=inboxes["actor"]):
        reply = asyncio.run(outreach.send_batch(inboxes["vid"], {"batch_id": saved["batch_id"], "approved_by_founder": True,
            "draft_ids": [saved["doc_id"]], "mailbox_digest": outreach._manual_digest(reviewed)}, actor_subject=inboxes["actor"]))
    assert len(calls) == 1 and calls[0]["connected_account_id"] == saved["sender"]["account"]
    assert reply["kind"] not in {"needs_you", "wait"}
    with inboxes["factory"]() as session:
        approval = session.scalar(select(Approval).where(Approval.subject_ref == saved["doc_id"]))
        detail = json.loads(approval.detail_json)
        assert detail["target"]["sender"] == saved["sender"]
        assert detail["external_execution"]["state"] == "succeeded"


@pytest.mark.parametrize("selection", ["b", "foreign"])
def test_legacy_queue_adapter_never_silently_substitutes_an_inbox(inboxes, monkeypatch, selection):
    from app.gtm.supymem import pipeline
    from app.marketing.adapters.gtm_adapter import GtmOutreachAdapter
    monkeypatch.setattr(pipeline, "_dry_run", lambda: True)
    calls = []
    monkeypatch.setattr(pipeline, "start_outreach", lambda *args, **kwargs: calls.append(kwargs) or
        {"enrolled": [inboxes["contact"]], "queued": 1, "sent": [], "dry_run": True})
    reply = asyncio.run(GtmOutreachAdapter().run({"kind": "enqueue_outreach", "workspace_id": inboxes["wid"],
        "venture_id": inboxes["vid"], "payload": {"mailbox_id": inboxes[selection], "contact_ids": [inboxes["contact"]]}}))
    if selection == "foreign":
        assert reply["status"] == "blocked" and reply["error"] == "mailbox_unavailable" and not calls
        assert reply["actions"][0]["label"] == "Choose another inbox"
    else:
        assert reply["ok"] is True and calls[0]["mailbox_id"] == inboxes["b"]


def test_conversation_pin_uses_insert_only_receipts_and_never_a_new_default(inboxes, monkeypatch):
    from app.autopilot import outreach
    from app.connections.mailboxes import MailboxUnavailable
    from app.db.models import GtmContact, GtmEmailAccount, GtmEmailEvent
    from app.gtm.supymem import pipeline
    from core.clock import utcnow
    first = draft(inboxes)
    with inboxes["factory"]() as session:
        session.get(GtmContact, inboxes["contact"]).gmail_thread_id = "original-thread"
        session.add(GtmEmailEvent(id="original-send", workspace_id=inboxes["wid"], contact_id=inboxes["contact"],
            email_account_id=inboxes["a"], event_type="sent_metadata", metadata_json=json.dumps({"thread_id": "original-thread"}), created_at=utcnow()))
        session.commit()
    monkeypatch.setattr(pipeline, "_active_mailbox", lambda session, wid: session.get(GtmEmailAccount, inboxes["b"]))
    followup = draft(inboxes, day=3, mailbox_id=None)
    assert followup["sender"] == first["sender"]
    with pytest.raises(MailboxUnavailable):
        draft(inboxes, day=3, mailbox_id=inboxes["b"])
    with inboxes["factory"]() as session:
        assert session.get(GtmEmailEvent, "original-send").email_account_id == inboxes["a"]
        session.get(GtmEmailAccount, inboxes["a"]).status = "revoked"
        session.commit()
    with pytest.raises(MailboxUnavailable):
        outreach.draft_mailbox(inboxes["wid"], inboxes["vid"], followup)


def test_legacy_conversation_without_sender_receipt_is_not_reassigned(inboxes):
    from app.connections.mailboxes import MailboxUnavailable
    from app.db.models import GtmContact
    with inboxes["factory"]() as session:
        session.get(GtmContact, inboxes["contact"]).gmail_thread_id = "unattributed-history"
        session.commit()
    with pytest.raises(MailboxUnavailable):
        draft(inboxes, day=3, mailbox_id=None)


def test_reply_uses_the_original_mailbox_and_thread_even_with_sequence_day_one(inboxes, monkeypatch):
    from app.autopilot import outreach
    from app.db.models import GtmContact, GtmEmailEvent, GtmTouch
    from app.gtm.supymem import composio_gmail
    from app.tenancy import bind
    from core.clock import utcnow
    with inboxes["factory"]() as session:
        contact = session.get(GtmContact, inboxes["contact"])
        contact.times_emailed, contact.gmail_thread_id, contact.replied_at = 1, "reply-original", utcnow()
        session.add(GtmEmailEvent(id="reply-origin", workspace_id=inboxes["wid"], contact_id=contact.id,
            email_account_id=inboxes["a"], event_type="sent_metadata", metadata_json=json.dumps({"thread_id": "reply-original"}),
            created_at=utcnow() - timedelta(days=2)))
        session.commit()
    saved = draft(inboxes, kind="reply", day=1, mailbox_id=None)
    reviewed = outreach._manual_review(inboxes["vid"], saved["batch_id"])
    calls = []
    monkeypatch.setattr(composio_gmail, "_send_gmail_email", lambda **kwargs: calls.append(kwargs) or
        {"provider_message_id": "reply-confirmed", "thread_id": "reply-original"})
    with bind(workspace_id=inboxes["wid"], venture_id=inboxes["vid"], actor=inboxes["actor"]):
        asyncio.run(outreach.send_batch(inboxes["vid"], {"batch_id": saved["batch_id"], "approved_by_founder": True,
            "draft_ids": [saved["doc_id"]], "mailbox_digest": outreach._manual_digest(reviewed)}, actor_subject=inboxes["actor"]))
    assert len(calls) == 1
    assert calls[0]["thread_id"] == "reply-original" and calls[0]["connected_account_id"] == saved["sender"]["account"]
    with inboxes["factory"]() as session:
        sent = session.scalar(select(GtmTouch).where(GtmTouch.message_id == "reply-confirmed"))
        assert sent.email_account_id == inboxes["a"]


def test_mailbox_age_ramp_and_same_email_quota_do_not_reset_on_reconnect(inboxes):
    from app.autopilot import guard
    from app.db.models import GtmEmailAccount, GtmEmailEvent
    from app.gtm.supymem import composio_gmail, factory
    from core.clock import utcnow
    with inboxes["factory"]() as session:
        old = session.get(GtmEmailAccount, inboxes["a"])
        fresh = session.get(GtmEmailAccount, inboxes["b"])
        fresh.created_at = utcnow()
        replacement = GtmEmailAccount(id="reconnected-same-email", workspace_id=inboxes["wid"], email=old.email.upper(),
            composio_user_id="reconnected-user", connected_account_id="reconnected-ca", status="active", daily_send_limit=40,
            created_at=utcnow(), updated_at=utcnow())
        session.add(replacement)
        session.add(GtmEmailEvent(id="uncertain-reservation", workspace_id=inboxes["wid"], email_account_id=old.id,
            contact_id=inboxes["contact"], event_type="gmail_reserved", metadata_json="{}", created_at=utcnow()))
        session.flush()
        assert guard.mailbox_send_max(session, fresh) == factory.daily_email_cap(1)
        assert guard.mailbox_send_max(session, old) == guard.mailbox_send_max(session, replacement)
        assert composio_gmail.mailbox_slots_used_today(session, old) == 1
        assert composio_gmail.mailbox_slots_used_today(session, replacement) == 1
        assert composio_gmail.mailbox_slots_used_today(session, fresh) == 0
        assert composio_gmail.workspace_slots_used_today(session, inboxes["wid"]) == 1


def test_workspace_aliases_share_one_stable_send_admission(inboxes, monkeypatch):
    from app.autopilot import outreach, send_admission
    from app.gtm.supymem import composio_gmail
    from app.workspaces.service import create_workspace
    alias = create_workspace(kind="company", name="Another project", owner_id=inboxes["actor"])
    sender = outreach._mailbox(inboxes["wid"], mailbox_id=inboxes["a"])
    keys = []
    @contextmanager
    def admit(key):
        keys.append(key)
        yield True
    monkeypatch.setattr(send_admission, "try_send_admission", admit)
    monkeypatch.setattr(composio_gmail, "_send_gmail_admitted", lambda **kwargs: {"provider_message_id": "admitted"})
    for workspace_id in (inboxes["wid"], alias.id):
        composio_gmail.send_gmail_email(composio_user_id=sender["user"], connected_account_id=sender["account"],
            to=f"person@{inboxes['domain']}", subject="A question", body="May I share more?", workspace_id=workspace_id)
    assert len(keys) == 2 and keys[0] == keys[1] and keys[0].startswith("gmail-owner:")


@pytest.mark.parametrize("sent_days_ago", [0, 2])
def test_sender_receipts_do_not_count_other_inboxes_or_double_count_touches(inboxes, sent_days_ago):
    from app.autopilot import outreach
    from app.db.models import GtmEmailAccount, GtmTouch
    from app.gtm.supymem import composio_gmail
    from core.clock import utcnow
    saved = draft(inboxes, mailbox_id=inboxes["b"])
    sent_at = utcnow() - timedelta(days=sent_days_ago)
    with inboxes["factory"]() as session:
        session.add(GtmTouch(id="mailbox-touch", workspace_id=inboxes["wid"], venture_id=inboxes["vid"],
            contact_id=inboxes["contact"], direction="out", channel="email", simulated=0,
            message_id="same-provider-id", sent_at=sent_at, created_at=utcnow()))
        outreach._record_mailbox_sent(session, inboxes["wid"], saved,
            {"provider_message_id": "same-provider-id", "thread_id": "thread-b", "sent_at": sent_at.isoformat()}, "mailbox-touch")
        session.commit()
        assert composio_gmail.mailbox_slots_used_today(session, session.get(GtmEmailAccount, inboxes["a"])) == 0
        assert composio_gmail.mailbox_slots_used_today(session, session.get(GtmEmailAccount, inboxes["b"])) == int(sent_days_ago == 0)
        assert composio_gmail.workspace_slots_used_today(session, inboxes["wid"]) == int(sent_days_ago == 0)
        assert composio_gmail.venture_slots_used_today(session, inboxes["vid"]) == int(sent_days_ago == 0)


def test_contact_dedupe_and_workspace_ceiling_cannot_be_bypassed_by_inbox_change(inboxes, monkeypatch):
    from app.autopilot import outreach
    from app.db.models import GtmEmailEvent
    from app.gtm.supymem import composio_gmail
    from core.clock import utcnow
    saved = draft(inboxes, mailbox_id=inboxes["b"])
    monkeypatch.setattr(composio_gmail, "_send_gmail_email", lambda **kwargs: pytest.fail("No bypass send"))
    with inboxes["factory"]() as session:
        session.add(GtmEmailEvent(id="other-inbox-reservation", workspace_id=inboxes["wid"],
            email_account_id=inboxes["a"], contact_id=inboxes["contact"], event_type="gmail_reserved", created_at=utcnow()))
        session.commit()
    with pytest.raises(composio_gmail.DefiniteFailure, match="already sent or unconfirmed"):
        outreach._send_one(saved["sender"], saved["to"], saved["subject"], saved["body"], None)
    with inboxes["factory"]() as session:
        session.add(GtmEmailEvent(id="released-other-inbox", workspace_id=inboxes["wid"],
            email_account_id=inboxes["a"], event_type="gmail_released",
            idempotency_key="gmail-release:other-inbox-reservation", created_at=utcnow()))
        for index in range(15):
            session.add(GtmEmailEvent(id=f"workspace-reserved-{index}", workspace_id=inboxes["wid"],
                email_account_id=inboxes["a"], event_type="gmail_reserved", created_at=utcnow()))
        session.commit()
    with pytest.raises(composio_gmail.DefiniteFailure, match="workspace daily send limit"):
        outreach._send_one(saved["sender"], saved["to"], saved["subject"], saved["body"], None)


def test_cold_contact_cap_does_not_charge_receipted_sends_from_another_inbox(inboxes, monkeypatch):
    from app.autopilot import outreach
    from app.db.models import GtmContact, GtmEmailEvent, GtmTouch
    from app.gtm.supymem import composio_gmail
    from core.clock import utcnow
    with inboxes["factory"]() as session:
        contact = session.get(GtmContact, inboxes["contact"])
        contact.recipient_geo = "US"
        contact.lawful_basis = "legitimate_interest"
        previous = GtmContact(id="previous-cold-contact", workspace_id=inboxes["wid"], email=f"prior@{inboxes['domain']}",
            lawful_basis="legitimate_interest", source=f"venture:{inboxes['vid']}:public", stage_changed_at=utcnow(), created_at=utcnow())
        session.add(previous)
        session.add(GtmTouch(id="previous-cold-touch", workspace_id=inboxes["wid"], venture_id=inboxes["vid"],
            contact_id=previous.id, direction="out", channel="email", simulated=0, message_id="other-inbox-message",
            sent_at=utcnow(), created_at=utcnow()))
        session.add(GtmEmailEvent(id="previous-cold-receipt", workspace_id=inboxes["wid"], contact_id=previous.id,
            email_account_id=inboxes["b"], event_type="sent_metadata", provider_message_id="other-inbox-message",
            metadata_json=json.dumps({"touch_id": "previous-cold-touch"}), created_at=utcnow()))
        session.commit()
    saved = draft(inboxes, mailbox_id=inboxes["a"])
    calls = []
    monkeypatch.setenv("OUTREACH_NEW_CONTACTS_PER_MAILBOX_DAY", "1")
    monkeypatch.setattr(composio_gmail, "_send_gmail_email", lambda **fields: calls.append(fields) or {"provider_message_id": "new-cold-message"})
    outreach._send_one(saved["sender"], saved["to"], saved["subject"], saved["body"], None)
    assert len(calls) == 1 and calls[0]["connected_account_id"] == saved["sender"]["account"]


def test_legacy_send_cannot_bypass_its_venture_ceiling_with_another_inbox(inboxes, monkeypatch):
    from app.autopilot import outreach
    from app.autopilot.service import update_settings
    from app.db.models import GtmEmailEvent
    from app.gtm.supymem import composio_gmail
    from app.tenancy import bind
    from core.clock import utcnow
    with bind(workspace_id=inboxes["wid"], venture_id=inboxes["vid"], actor=inboxes["actor"]):
        update_settings(inboxes["vid"], allow_send=True, send_max_per_day=1)
    saved = draft(inboxes)
    with inboxes["factory"]() as session:
        session.add(GtmEmailEvent(id="venture-reservation", workspace_id=inboxes["wid"], email_account_id=inboxes["b"],
            event_type="gmail_reserved", metadata_json=json.dumps({"venture_id": inboxes["vid"]}), created_at=utcnow()))
        session.commit()
    monkeypatch.setattr(composio_gmail, "_send_gmail_email", lambda **kwargs: pytest.fail("No venture cap bypass"))
    with pytest.raises(composio_gmail.DefiniteFailure, match="venture daily send limit"):
        outreach._send_one(saved["sender"], saved["to"], saved["subject"], saved["body"], None)


def test_queue_approval_digest_includes_the_persisted_local_mailbox(inboxes):
    from app.approvals.external import action_digest, gtm_email_review
    from app.connections.mailboxes import MailboxUnavailable
    from app.db.models import GtmContact, GtmEmailSendQueue
    from core.clock import utcnow
    with inboxes["factory"]() as session:
        session.get(GtmContact, inboxes["contact"]).sequence_state_json = json.dumps({"1": {"subject": "A question", "body": "May I share more?"}})
        session.add(GtmEmailSendQueue(id="queued-review", workspace_id=inboxes["wid"], contact_id=inboxes["contact"],
            email_account_id=inboxes["a"], sequence_day=1, status="queued", scheduled_for=utcnow(),
            idempotency_key="contact-day", created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    target, review = gtm_email_review(inboxes["wid"], "queued-review")
    assert review["mailbox_id"] == inboxes["a"]
    original = action_digest("gtm_email_send", target)
    with inboxes["factory"]() as session:
        session.get(GtmEmailSendQueue, "queued-review").email_account_id = inboxes["b"]
        session.commit()
    with pytest.raises(MailboxUnavailable):
        action_digest("gtm_email_send", target)
    target.pop("sender")
    assert action_digest("gtm_email_send", target) != original


@pytest.mark.parametrize("action", ["gtm_email_send", "gtm_test_email"])
def test_approval_cannot_bind_another_founders_queue_or_inbox(inboxes, action):
    from app.approvals.external import propose_external_approval
    from app.connections.mailboxes import MailboxUnavailable
    from app.db.models import GtmEmailAccount, GtmEmailSendQueue
    from core.clock import utcnow
    target = {"mailbox_id": inboxes["foreign"], "to": f"person@{inboxes['domain']}", "subject": "Hello", "body": "May I share more?"}
    if action == "gtm_email_send":
        with inboxes["factory"]() as session:
            account = session.get(GtmEmailAccount, inboxes["foreign"])
            foreign_contact = "contact-" + inboxes["foreign"].split("-")[1]
            session.add(GtmEmailSendQueue(id="foreign-queue", workspace_id=account.workspace_id, contact_id=foreign_contact,
                email_account_id=account.id, sequence_day=1, status="queued", scheduled_for=utcnow(),
                idempotency_key="foreign-contact-day", created_at=utcnow(), updated_at=utcnow()))
            session.commit()
        target = {"queue_id": "foreign-queue"}
    with pytest.raises(MailboxUnavailable):
        propose_external_approval(workspace_id=inboxes["wid"], subject_ref="foreign-review", action=action,
            target=target, summary="Review your email", review={})


def test_scheduled_send_persists_the_exact_reviewed_sender(inboxes):
    from app.db.models import Job, VentureDocument
    from app.db.supymem_models import Experiment
    from app.experiments import outreach_tests
    from core.clock import utcnow
    saved = draft(inboxes, requires_journey_review=True, review_state="approved", experiment_id="timing-test")
    with inboxes["factory"]() as session:
        session.add(Experiment(id="timing-test", workspace_id=inboxes["wid"], venture_id=inboxes["vid"], kind="test",
            hypothesis="Try a different local window", action="email", state="running", started_at=utcnow(),
            context=json.dumps({"run_id": "timing-run", "spec": {"max_duration_days": 3}})))
        session.commit()
    due = (utcnow() + timedelta(hours=1)).isoformat()
    assert outreach_tests.schedule_send(inboxes["vid"], saved, {"journey_approvals": {saved["doc_id"]: "reviewed-approval"}},
        inboxes["actor"], {"due_at": due})
    with inboxes["factory"]() as session:
        scheduled = json.loads(session.get(VentureDocument, saved["doc_id"]).provenance_json)["scheduled_send"]
        assert json.loads(session.get(Job, scheduled["job_id"]).payload_json)["mailbox_id"] == inboxes["a"]
    assert scheduled["sender"] == saved["sender"] and scheduled["mailbox_id"] == saved["mailbox_id"]


def test_scheduled_unavailable_inbox_pauses_only_its_job_and_shows_recovery(inboxes, monkeypatch):
    from app.autopilot import outreach
    from app.chat import milestones
    from app.core.jobs import lease
    from app.db.models import GtmEmailAccount, Job
    from app.db.supymem_models import Experiment
    from app.experiments import autonomous, outreach_tests
    from core.clock import utcnow
    saved = draft(inboxes, requires_journey_review=True, review_state="approved", experiment_id="paused-timing")
    with inboxes["factory"]() as session:
        session.add(Experiment(id="paused-timing", workspace_id=inboxes["wid"], venture_id=inboxes["vid"], kind="test",
            hypothesis="Try a different local window", action="email", state="running", started_at=utcnow() - timedelta(minutes=2),
            context=json.dumps({"run_id": "paused-run", "spec": {"max_duration_days": 3, "catalog_key": "E21"}})))
        session.commit()
    assert outreach_tests.schedule_send(inboxes["vid"], saved,
        {"journey_approvals": {saved["doc_id"]: "saved-approval"}}, inboxes["actor"],
        {"due_at": (utcnow() - timedelta(minutes=1)).isoformat()})
    with inboxes["factory"]() as session:
        session.get(GtmEmailAccount, inboxes["a"]).status = "needs_reconnect"
        jobs = lease(session, "mailbox-worker", kinds=[autonomous.KIND], n=1)
        session.commit()
    assert len(jobs) == 1
    async def blocked_send(*args, **kwargs):
        return outreach.mailbox_failure(inboxes["vid"])
    monkeypatch.setattr(outreach, "send_batch", blocked_send)
    posted = []
    monkeypatch.setattr(milestones, "post_milestone", lambda *args, **kwargs: posted.append(kwargs))
    job = jobs[0]
    asyncio.run(outreach_tests.send_scheduled_job(job.payload | {"_job_id": job.id,
        "_worker_id": "mailbox-worker", "_job_attempt": job.attempts}))
    with inboxes["factory"]() as session:
        assert session.get(Job, job.id).status == "paused"
        assert session.get(GtmEmailAccount, inboxes["b"]).status == "active"
    assert posted[0]["actions"][0]["label"] == "Choose another inbox"

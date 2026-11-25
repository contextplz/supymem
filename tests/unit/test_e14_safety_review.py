import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tests.unit.test_multi_gmail_mailboxes import mailbox_db
from tests.unit.gtm.test_multi_gmail_reading import inboxes, _reads, _dsn


@pytest.fixture()
def founder_ventures(mailbox_db):
    from app.db.models import Venture, Workspace
    from app.state import db
    from core.clock import utcnow
    cases = []
    with db()() as session:
        for index, mailbox_id in enumerate(("tools", "foreign")):
            from app.db.models import GtmEmailAccount
            mailbox = session.get(GtmEmailAccount, mailbox_id)
            workspace = session.get(Workspace, mailbox.workspace_id)
            venture_id = f"review-venture-{index}"
            session.add(Venture(id=venture_id, workspace_id=workspace.id, name="Your saved idea",
                raw_idea="Build tooling for engineering teams" if index == 0 else "Cooking lessons for families",
                stage="draft", created_at=utcnow(), updated_at=utcnow()))
            cases.append({"wid": workspace.id, "owner": workspace.owner_id, "vid": venture_id, "mailbox_id": mailbox.id})
        session.commit()
    return cases


def test_delegated_admin_cannot_expand_mailboxes_without_membership(founder_ventures, monkeypatch):
    from app.api import ops_auth, ops_api
    from app.authz.workspace import ensure_subject_person
    from app.connections import mailboxes
    from app.db.models import GtmEmailAccount, WorkspaceMember
    from app.gtm.supymem import pipeline
    from app.state import db
    from app.tenancy import bind
    from app.workspaces.service import create_workspace
    from core.clock import utcnow
    for index, case in enumerate(founder_ventures):
        delegate = ensure_subject_person(f"review-delegate-{index}")
        private = create_workspace(kind="company", name="Your private workspace", owner_id=case["owner"])
        private_id = f"private-mailbox-{index}"
        with db()() as session:
            session.add(WorkspaceMember(id=f"delegate-member-{index}", workspace_id=case["wid"], person_id=delegate,
                role="admin", created_at=utcnow()))
            session.add(GtmEmailAccount(id=private_id, workspace_id=private.id,
                email=f"you@{'engineering' if index == 0 else 'family-recipes'}.example", provider="gmail", status="active",
                connected_account_id=f"private-account-{index}", composio_user_id=f"private-user-{index}",
                created_at=utcnow(), updated_at=utcnow()))
            session.commit()
        with bind(workspace_id=case["wid"], venture_id=case["vid"], actor=delegate):
            result = ops_api.connections_gmail_mailboxes(workspace_id=case["wid"], x_workspace_id=None, x_api_key=None)
            assert private_id not in {row["id"] for row in result["mailboxes"]}
            with db()() as session:
                assert private_id not in {ref["mailbox_id"] for ref in mailboxes.list_read_mailboxes(session, case["wid"])}
                with pytest.raises(mailboxes.MailboxUnavailable):
                    mailboxes.resolve_send_mailbox(session, case["wid"], mailbox_id=private_id)
                with pytest.raises(mailboxes.MailboxUnavailable):
                    mailboxes.set_default_mailbox(session, case["wid"], private_id)
        monkeypatch.setattr(ops_auth, "person_from_hosted_token", lambda token: delegate)
        token = ops_auth.bind_authorization("Bearer your-session")
        try:
            with db()() as session:
                assert private.id not in pipeline._gmail_workspace_ids(session, case["wid"])
            with bind(workspace_id="none", venture_id="", actor="anonymous"):
                with db()() as session:
                    assert private.id not in pipeline._gmail_workspace_ids(session, case["wid"])
        finally:
            ops_auth.reset_authorization(token)
        with bind(workspace_id=case["wid"], venture_id=case["vid"], actor=case["owner"]):
            with db()() as session:
                assert mailboxes.resolve_send_mailbox(session, case["wid"], mailbox_id=private_id)["mailbox_id"] == private_id
        with db()() as session:
            session.add(WorkspaceMember(id=f"private-member-{index}", workspace_id=private.id, person_id=delegate,
                role="admin", created_at=utcnow()))
            session.commit()
        with bind(workspace_id=case["wid"], venture_id=case["vid"], actor=delegate):
            with db()() as session:
                assert mailboxes.resolve_send_mailbox(session, case["wid"], mailbox_id=private_id)["mailbox_id"] == private_id


@pytest.mark.parametrize("historical", [False, True])
def test_disabling_multi_gmail_never_recounts_ingested_or_historical_replies(inboxes, monkeypatch, historical):
    from app.credits.policy import set_policy
    from app.db.models import GtmTouch
    from app.gtm.supymem import workers
    from core.clock import utcnow
    _reads(inboxes, monkeypatch, shared_thread=False)
    if historical:
        with inboxes["factory"]() as session:
            for case in inboxes["cases"]:
                session.add(GtmTouch(id="qualified-" + case["contact_id"], workspace_id=inboxes["wid"],
                    contact_id=case["contact_id"], venture_id=case["venture_id"], direction="in", simulated=0,
                    message_id=workers._mailbox_message_key(case["mailbox_id"], "shared-message"), created_at=utcnow()))
            session.commit()
    workers.tick_replies(inboxes["wid"])
    with inboxes["factory"]() as session:
        before = list(session.scalars(select(GtmTouch.id).where(GtmTouch.direction == "in")))
    assert len(before) == 2
    set_policy("feature_flags", {"multi_gmail_v1": False}, "test")
    assert workers.tick_replies(inboxes["wid"])["marked"] == []
    with inboxes["factory"]() as session:
        assert list(session.scalars(select(GtmTouch.id).where(GtmTouch.direction == "in"))) == before


def test_flag_off_reply_scope_keeps_legacy_ambiguity_fail_closed(inboxes, monkeypatch):
    from app.credits.policy import set_policy
    from app.db.models import GtmTouch
    from app.gtm.supymem import workers
    set_policy("feature_flags", {"multi_gmail_v1": False, "inbox_briefing_v1": False}, "test")
    _reads(inboxes, monkeypatch)
    assert workers.tick_replies(inboxes["wid"]) == {"marked": [], "replies": []}
    with inboxes["factory"]() as session:
        assert list(session.scalars(select(GtmTouch.id).where(GtmTouch.direction == "in"))) == []


def _shared_contact(inboxes, *, shared_venture=False):
    from app.db.models import GtmContact, GtmEmailEvent, GtmTouch
    from core.clock import utcnow
    contact_id = "shared-review-contact"
    with inboxes["factory"]() as session:
        session.add(GtmContact(id=contact_id, workspace_id=inboxes["wid"], email="shared-" + inboxes["cases"][0]["email"],
            times_emailed=2, gmail_thread_id="colliding-thread", stage_changed_at=utcnow(), created_at=utcnow()))
        session.flush()
        for index, case in enumerate(inboxes["cases"]):
            touch_id = f"shared-provider-{index}"
            session.add(GtmTouch(id=touch_id, workspace_id=inboxes["wid"], contact_id=contact_id,
                venture_id=inboxes["cases"][0]["venture_id"] if shared_venture else case["venture_id"],
                email_account_id=case["mailbox_id"], direction="out", channel="email",
                simulated=0, message_id=f"provider-id-{index}", provider_thread_id="colliding-thread",
                sent_at=utcnow(), created_at=utcnow()))
            session.add(GtmEmailEvent(id=f"shared-metadata-{index}", workspace_id=inboxes["wid"], contact_id=contact_id,
                email_account_id=case["mailbox_id"], event_type="sent_metadata", idempotency_key="sent-metadata:" + touch_id,
                metadata_json=json.dumps({"rfc_message_id": f"<shared-{index}@example.org>", "thread_id": "colliding-thread"}),
                created_at=utcnow()))
        session.commit()
    return contact_id


def test_rfc_metadata_with_thread_fields_attributes_provider_only_outbound_ids(inboxes):
    from app.db.models import GtmTouch
    from app.gtm.supymem import pipeline
    contact_id = _shared_contact(inboxes)
    for index, case in enumerate(inboxes["cases"]):
        assert pipeline.apply_reply(contact_id, body="Your invitation is interesting", classification="human_positive",
            venture_id=case["venture_id"], email_account_id=case["mailbox_id"], provider_message_id="colliding-reply",
            provider_thread_id=None, in_reply_to=f"<shared-{index}@example.org>") is True
    with inboxes["factory"]() as session:
        touches = list(session.scalars(select(GtmTouch).where(GtmTouch.contact_id == contact_id, GtmTouch.direction == "in")))
        assert {(touch.email_account_id, touch.venture_id) for touch in touches} == {
            (case["mailbox_id"], case["venture_id"]) for case in inboxes["cases"]}


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("paused", [False, True])
def test_raw_reply_dedupe_never_hides_a_distinct_inbox_reply_for_the_same_contact(inboxes, legacy, paused):
    from app.db.models import GtmEmailAccount, GtmTouch
    from app.gtm.supymem import pipeline
    from core.clock import utcnow
    contact_id = _shared_contact(inboxes, shared_venture=paused)
    first, second = inboxes["cases"]
    with inboxes["factory"]() as session:
        if paused:
            session.get(GtmEmailAccount, first["mailbox_id"]).status = "needs_reconnect"
        session.add(GtmTouch(id="unconfirmed-shared-preview", workspace_id=inboxes["wid"], contact_id=contact_id,
            venture_id=first["venture_id"], direction="out", simulated=0,
            provider_thread_id="colliding-thread", created_at=utcnow()))
        session.add(GtmTouch(id="previous-raw-reply", workspace_id=inboxes["wid"], contact_id=contact_id,
            venture_id=first["venture_id"], email_account_id=None if legacy else first["mailbox_id"],
            direction="in", simulated=0, message_id="same-raw-id", provider_thread_id="colliding-thread", created_at=utcnow()))
        session.commit()
    for index, case in (((1, second),) if paused else ((1, second), (0, first))):
        result = pipeline.apply_reply(contact_id, body="Your message reached me", classification="human_positive",
            venture_id=first["venture_id"] if paused else case["venture_id"],
            email_account_id=case["mailbox_id"], provider_message_id="same-raw-id",
            provider_thread_id="colliding-thread", in_reply_to=f"<shared-{index}@example.org>")
        assert result is (index == 1)
    with inboxes["factory"]() as session:
        touches = list(session.scalars(select(GtmTouch).where(GtmTouch.contact_id == contact_id, GtmTouch.direction == "in")))
        assert len(touches) == 2
        assert any(touch.email_account_id == second["mailbox_id"] for touch in touches)


def test_unconsented_untracked_personal_inbox_has_zero_bounce_reads(inboxes, monkeypatch):
    from app.credits.policy import set_policy
    from app.db.models import GtmEmailAccount, Workspace
    from app.gtm.supymem import composio_gmail, workers
    from app.inbox.store import InboxBriefingState
    from core.clock import utcnow
    set_policy("feature_flags", {"multi_gmail_v1": True, "inbox_briefing_v1": True}, "test")
    with inboxes["factory"]() as session:
        owner = session.get(Workspace, inboxes["wid"]).owner_id
        session.add(GtmEmailAccount(id="personal-review-inbox", workspace_id=inboxes["wid"],
            email="you@personal.example", connected_account_id="personal-review-account", composio_user_id="personal-review-user",
            provider="gmail", status="active", created_at=utcnow(), updated_at=utcnow()))
        session.add(InboxBriefingState(id="personal-review-consent", founder_id=owner, workspace_id=inboxes["wid"],
            mailbox_id="personal-review-inbox", consent=0))
        session.commit()
    calls = []
    by_account = {case["ca"]: case for case in inboxes["cases"]}
    def profile(user, account):
        assert account in by_account
        calls.append(account)
        return {"raw": {"historyId": "20"}}
    def query(**kwargs):
        account = kwargs["connected_account_id"]
        assert account in by_account
        calls.append(account)
        return [_dsn(by_account[account]["email"])]
    monkeypatch.setattr(composio_gmail, "get_gmail_profile", profile)
    monkeypatch.setattr(composio_gmail, "fetch_query", query)
    result = workers.tick_bounces(inboxes["wid"])
    assert "personal-review-account" not in calls
    assert len(result["marked"]) == 2
    assert next(row for row in result["mailboxes"] if row["mailbox_id"] == "personal-review-inbox")["skipped"] == "no_tracked_contacts"


@pytest.mark.parametrize("scan", ["history", "bounce"])
@pytest.mark.parametrize("limit", ["pages", "messages", "time"])
def test_read_budgets_keep_the_bad_checkpoint_and_allow_later_inboxes(inboxes, monkeypatch, scan, limit):
    from app.db.models import GtmEmailEvent
    from app.gtm.supymem import composio_gmail, workers
    from core.clock import utcnow
    first, second = inboxes["cases"]
    clock, calls = [0], []
    monkeypatch.setattr(workers, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    if scan == "history":
        with inboxes["factory"]() as session:
            for case in inboxes["cases"]:
                session.add(GtmEmailEvent(id="initial-budget-" + case["mailbox_id"], workspace_id=inboxes["wid"],
                    email_account_id=case["mailbox_id"], event_type="gmail_bounce_checkpoint",
                    metadata_json=json.dumps({"history_id": "1"}), created_at=utcnow()))
            session.commit()
    def page(account):
        calls.append(account)
        if account == first["ca"]:
            if limit == "time":
                clock[0] += workers._READ_MAX_SECONDS + 1
            count = workers._READ_MAX_MESSAGES + 1 if limit == "messages" else 0
            return {"messages": [{"id": f"large-{index}"} for index in range(count)], "nextPageToken": f"next-{len(calls)}"}
        return {"messages": [_dsn(second["email"])]}
    def history(**kwargs):
        data = page(kwargs["connected_account_id"])
        return {"historyId": "20", "nextPageToken": data.get("nextPageToken"),
            "history": [{"messagesAdded": [{"message": {"id": message["id"]}} for message in data["messages"]]}]}
    monkeypatch.setattr(composio_gmail, "fetch_history", history)
    monkeypatch.setattr(composio_gmail, "fetch_message", lambda **kwargs: _dsn(second["email"]))
    monkeypatch.setattr(composio_gmail, "get_gmail_profile", lambda *args: {"raw": {"historyId": "20"}})
    monkeypatch.setattr(composio_gmail, "fetch_query", lambda **kwargs: page(kwargs["connected_account_id"]))
    monkeypatch.setattr(composio_gmail, "execute_tool", lambda tool, **kwargs: page(kwargs["connected_account_id"]))
    result = workers.tick_bounces(inboxes["wid"])
    assert result["marked"] == [second["contact_id"]]
    assert sum(account == first["ca"] for account in calls) <= workers._READ_MAX_PAGES
    with inboxes["factory"]() as session:
        bad = list(session.scalars(select(GtmEmailEvent).where(GtmEmailEvent.email_account_id == first["mailbox_id"],
            GtmEmailEvent.event_type == "gmail_bounce_checkpoint")))
        checkpoints = [json.loads(row.metadata_json) for row in bad]
        if scan == "history":
            assert checkpoints == [{"history_id": "1"}]
        else:
            assert all("history_id" not in checkpoint and checkpoint["target_history_id"] == "20"
                       and checkpoint["bootstrap_page"] for checkpoint in checkpoints)
        assert session.scalar(select(GtmEmailEvent.id).where(GtmEmailEvent.email_account_id == second["mailbox_id"],
            GtmEmailEvent.event_type == "gmail_bounce_checkpoint", GtmEmailEvent.metadata_json.contains('"history_id": "20"')))


def test_chat_offer_consent_exposes_the_committed_ui_config_and_revocation_contract(founder_ventures):
    from app.credits.policy import set_policy
    from app.inbox import briefing
    from app.tenancy import bind
    from app.ui_config import ui_config
    set_policy("feature_flags", {"multi_gmail_v1": True, "inbox_briefing_v1": True}, "test")
    for case in founder_ventures:
        with bind(workspace_id=case["wid"], venture_id=case["vid"], actor=case["owner"]):
            for consent in (True, False):
                briefing.action(case["wid"], case["vid"], "inbox.briefing.consent",
                    {"mailbox_id": case["mailbox_id"], "consent": consent}, case["owner"])
                config = ui_config(case["owner"])
                assert config["inbox_briefing_v1"] is True
                selected = next(row for row in config["inbox_briefing"]["mailboxes"] if row["mailbox_id"] == case["mailbox_id"])
                assert selected["consent"] is consent and "enabled" not in selected
                assert "quiet_hours" in config["inbox_briefing"]["settings"]


def test_committed_mailbox_routes_publish_the_native_console_fields_for_both_founders(founder_ventures):
    from app.api import ops_api
    from app.tenancy import bind
    for case in founder_ventures:
        with bind(workspace_id=case["wid"], venture_id=case["vid"], actor=case["owner"]):
            result = ops_api.connections_gmail_mailboxes(workspace_id=case["wid"], venture_id=case["vid"],
                x_workspace_id=None, x_api_key=None)
        assert result["workspace_id"] == case["wid"] and result["venture_id"] == case["vid"]
        selected = next(row for row in result["mailboxes"] if row["id"] == case["mailbox_id"])
        assert selected["daily_send_limit"] >= 0 and "cap" not in selected
        assert isinstance(selected["updated_at"], str)

import base64
import hashlib
import json
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import MetaData, create_engine, event, select
from sqlalchemy.exc import IntegrityError

from tests.company_os_harness import load_company_os_app


def test_shared_connected_accounts_enqueue_once_per_owner_and_window(inboxes):
    from app.connections.composio_pacing import RateLimited
    from app.core import scheduled
    from app.db.models import GtmEmailAccount, Job, Workspace
    from app.gtm.supymem import workers
    from app.tenancy import bind
    from app.workspaces.service import create_workspace
    from core.clock import utcnow

    for workspace in (inboxes, inboxes["other"]):
        with inboxes["factory"]() as session:
            owner = session.get(Workspace, workspace["wid"]).owner_id
        case = workspace["cases"][0 if workspace is inboxes else 1]
        mailbox_ids = {case["mailbox_id"]}
        workspace_ids = [workspace["wid"]]
        for index in range(19):
            sibling = create_workspace(kind="company", name=f"Shared inbox {index}", owner_id=owner)
            workspace_ids.append(sibling.id)
            mailbox_id = f"shared_{case['mailbox_id']}_{index}"
            mailbox_ids.add(mailbox_id)
            with inboxes["factory"]() as session:
                session.add(GtmEmailAccount(id=mailbox_id, workspace_id=sibling.id, email=case["email"],
                    composio_user_id=case["uid"], connected_account_id=case["ca"], status="active",
                    created_at=utcnow(), updated_at=utcnow()))
                session.commit()
        for window in ("20261103T120", "20261103T121"):
            for workspace_id in workspace_ids:
                with bind(workspace_id=workspace_id, venture_id="", actor=owner):
                    scheduled.enqueue_mailboxes(workspace_id=workspace_id, window=window)
            with inboxes["factory"]() as session:
                jobs = [row for row in session.scalars(select(Job).where(Job.kind == "gtm.gmail.read"))
                    if json.loads(row.payload_json).get("mailbox_id") in mailbox_ids
                    and json.loads(row.payload_json).get("window") == window]
                assert len(jobs) == 1
                assert jobs[0].workspace_id in workspace_ids
        with inboxes["factory"]() as session:
            from app.gtm.supymem import pipeline
            assert set(pipeline._gmail_workspace_ids(session, workspace["wid"])) == set(workspace_ids)
        calls = []
        workers._fanout(workspace["wid"], lambda workspace_id, mailbox_id, calls=calls:
            calls.append(mailbox_id) or {"marked": [], "replies": []}, replies=True)
        assert len(set(calls) & mailbox_ids) == 1
        claim = workers.claim_read(workspace["wid"], case["mailbox_id"])
        with pytest.raises(RateLimited):
            workers.claim_read(workspace_ids[-1], mailbox_id)
        workers.release_read(claim)
        assert workers.claim_read(workspace_ids[-1], mailbox_id, window="20261103T121") is None


def test_read_fixtures_preserve_connected_account_uniqueness(inboxes):
    from app.db.models import GtmEmailAccount
    from core.clock import utcnow

    for case in inboxes["cases"]:
        with inboxes["factory"]() as session:
            session.add(GtmEmailAccount(id="duplicate-" + case["mailbox_id"], workspace_id=inboxes["other"]["wid"],
                email=case["email"], composio_user_id=case["uid"], connected_account_id=case["ca"], status="active",
                created_at=utcnow(), updated_at=utcnow()))
            with pytest.raises(IntegrityError, match=r"gtm_email_accounts\.connected_account_id"):
                session.flush()


@pytest.mark.parametrize("case_index", [0, 1], ids=["b2b", "consumer"])
@pytest.mark.parametrize("reader", ["replies", "bounces"])
def test_superseded_read_preserves_checkpoint_and_next_read_finds_reply(inboxes, monkeypatch, case_index, reader):
    import asyncio

    from app.core import jobs, scheduled
    from app.db.models import (
        GtmContact,
        GtmEmailAccount,
        GtmEmailEvent,
        GtmTouch,
        Job,
        Venture,
        Workspace,
    )
    from app.gtm.supymem import composio_gmail, workers
    from app.tenancy import bind
    from app.worker import handlers
    from app.worker import main as worker
    from app.workspaces.service import create_workspace
    from core.clock import utcnow

    case = inboxes["cases"][case_index]
    event_type = "gmail_sync_checkpoint" if reader == "replies" else "gmail_bounce_checkpoint"
    with inboxes["factory"]() as session:
        owner = session.get(Workspace, inboxes["wid"]).owner_id
        checkpoint_id = "skipped-checkpoint-" + case["mailbox_id"]
        session.add(GtmEmailEvent(id=checkpoint_id, workspace_id=inboxes["wid"],
            email_account_id=case["mailbox_id"], event_type=event_type,
            metadata_json=json.dumps({"history_id": "40"}), created_at=utcnow()))
        session.commit()
    sibling = create_workspace(kind="company", name="Shared reply venture", owner_id=owner)
    alias_id = "shared-" + case["mailbox_id"]
    contact_id = "shared-" + case["contact_id"]
    venture_id = "shared-" + case["venture_id"]
    with inboxes["factory"]() as session:
        session.add(GtmEmailAccount(id=alias_id, workspace_id=sibling.id, email=case["email"],
            composio_user_id=case["uid"], connected_account_id=case["ca"], status="active",
            created_at=utcnow(), updated_at=utcnow()))
        session.add(Venture(id=venture_id, workspace_id=sibling.id, name="Shared reply", raw_idea="Unrelated venture",
            stage="draft", created_at=utcnow(), updated_at=utcnow()))
        session.add(GtmContact(id=contact_id, workspace_id=sibling.id, email="alias-" + case["email"],
            source=f"venture:{venture_id}:founder_import", times_emailed=1, stage_changed_at=utcnow(), created_at=utcnow()))
        session.flush()
        session.add(GtmTouch(id="shared-send-" + case["mailbox_id"], workspace_id=sibling.id,
            venture_id=venture_id, contact_id=contact_id, email_account_id=alias_id, direction="out", channel="email",
            simulated=0, sent_at=utcnow(), message_id="<shared-sent@example.test>", provider_thread_id="alias-thread",
            created_at=utcnow()))
        session.commit()
    calls = []
    def history(**kwargs):
        calls.append(kwargs)
        assert kwargs["connected_account_id"] == case["ca"]
        assert kwargs["start_history_id"] == "40"
        return {"historyId": "60", "history": [{"messagesAdded": [{"message": {"id": "skipped-reply"}}]}]}
    monkeypatch.setattr(composio_gmail, "fetch_history", history)
    reply = {
        "id": "skipped-reply", "threadId": "alias-thread", "body_text": "Stop emailing me.",
        "headers": {"from": "alias-" + case["email"], "in-reply-to": "<shared-sent@example.test>"}}
    monkeypatch.setattr(composio_gmail, "fetch_message", lambda **kwargs:
        reply if reader == "replies" else {**_dsn("alias-" + case["email"]), "id": "skipped-reply"})
    monkeypatch.setattr(workers, "tick_bounces" if reader == "replies" else "tick_replies",
        lambda *args, **kwargs: {"marked": [], "replies": []})
    monkeypatch.setattr(handlers, "HANDLERS", {"gtm.gmail.read": handlers._gmail_read})
    with bind(workspace_id=inboxes["wid"], venture_id="", actor=owner):
        for window in ("20261103T120", "20261103T121"):
            scheduled.enqueue_mailboxes(workspace_id=inboxes["wid"], window=window)
        payload = {"workspace_id": inboxes["wid"], "mailbox_id": case["mailbox_id"], "window": "20261103T120"}
        with inboxes["factory"]() as session:
            leased = jobs.lease(session, "stale-reader", kinds=["gtm.gmail.read"], n=10)
            stale = next(job for job in leased if job.payload.get("mailbox_id") == case["mailbox_id"]
                and job.payload.get("window") == payload["window"])
            session.commit()
        asyncio.run(worker._run_one(inboxes["factory"], stale, {(stale.id, stale.attempts)}))
        assert calls == []
        with inboxes["factory"]() as session:
            assert session.get(Job, stale.id).status == "succeeded"
            assert json.loads(session.get(GtmEmailEvent, checkpoint_id).metadata_json) == {"history_id": "40"}
            assert session.scalar(select(GtmEmailEvent.id).where(GtmEmailEvent.email_account_id == case["mailbox_id"],
                GtmEmailEvent.event_type == event_type, GtmEmailEvent.id != checkpoint_id)) is None
            assert session.get(GtmContact, case["contact_id"]).do_not_contact == 0
        asyncio.run(handlers._gmail_read({**payload, "window": "20261103T121"}))
        assert len(calls) == 1
        asyncio.run(handlers._gmail_read({**payload, "window": "20261103T121"}))
        assert len(calls) == 1
    with inboxes["factory"]() as session:
        contact = session.get(GtmContact, contact_id)
        assert contact.do_not_contact == 1 if reader == "replies" else contact.bounced_at is not None
        assert session.get(GtmContact, case["contact_id"]).do_not_contact == 0
        assert all(session.get(GtmContact, other["contact_id"]).do_not_contact == 0 for other in inboxes["other"]["cases"])
        assert session.scalar(select(Job.id).where(Job.kind == "gtm.gmail.read")) is not None


def test_read_budget_refusals_preserve_retry_budget_without_error_events(inboxes, monkeypatch, caplog):
    import asyncio
    import logging

    from app.autopilot import outreach
    from app.connections.composio_pacing import AdmissionLimited, RateLimited
    from app.core import jobs
    from app.db.models import Job, Workspace
    from app.inbox import store
    from app.tenancy import bind
    from app.worker import handlers
    from app.worker import main as worker
    from core import clock

    async def refuse(*args, **kwargs):
        raise AdmissionLimited(83)
    monkeypatch.setattr(outreach, "poll_replies", refuse)
    monkeypatch.setattr(handlers, "HANDLERS", {"gtm.gmail.read": handlers._gmail_read})
    caplog.set_level(logging.WARNING)
    for workspace in (inboxes, inboxes["other"]):
        with inboxes["factory"]() as session:
            owner = session.get(Workspace, workspace["wid"]).owner_id
        for case in workspace["cases"]:
            with bind(workspace_id=workspace["wid"], venture_id="", actor=owner), inboxes["factory"]() as session:
                job_id = jobs.enqueue(session, "gtm.gmail.read", {"mailbox_id": case["mailbox_id"], "_actor_subject": owner},
                    workspace_id=workspace["wid"], max_attempts=1)
                session.commit()
                before = clock.utcnow()
                for _attempt in range(2):
                    job = jobs.lease(session, "budget-reader", kinds=["gtm.gmail.read"], now=before)[0]
                    session.commit()
                    asyncio.run(worker._run_one(inboxes["factory"], job, {(job.id, job.attempts)}))
                    session.expire_all()
                    saved = session.get(Job, job_id)
                    assert saved.status == "retry_wait"
                    assert saved.max_attempts - saved.attempts == 1
                    assert (saved.run_after.replace(tzinfo=before.tzinfo) - before).total_seconds() >= 82
                    warning_count = sum(record.getMessage() == "gmail_read_deferred" for record in caplog.records)
                    with pytest.raises(RateLimited):
                        asyncio.run(handlers._gmail_read(job.payload))
                    assert sum(record.getMessage() == "gmail_read_deferred" for record in caplog.records) == warning_count
                    assert json.loads(session.scalar(select(store.InboxBriefingState).where(
                        store.InboxBriefingState.mailbox_id == case["mailbox_id"])).data_json)["gmail_failures"] == 0
                    before = saved.run_after.replace(tzinfo=before.tzinfo)
                    monkeypatch.setattr(clock, "utcnow", lambda before=before: before)
                    monkeypatch.setattr(jobs, "utcnow", lambda before=before: before)
                saved.run_after = saved.scheduled_for = before + timedelta(days=1)
                session.commit()
    assert not [record for record in caplog.records if record.name == "app.error_event"]


def test_background_ventures_share_bounded_mailbox_reads(inboxes, monkeypatch):
    import asyncio

    from app.autopilot import outreach
    from app.core import jobs
    from app.db.models import (
        GtmContact,
        GtmEmailEvent,
        GtmTouch,
        Job,
        Venture,
        Workspace,
    )
    from app.gtm.supymem import workers
    from app.tenancy import bind
    from app.worker import handlers
    from app.workspaces.service import create_workspace
    from core.clock import utcnow

    actual_poll = outreach._poll
    monkeypatch.setattr(outreach, "_poll", lambda *args, **kwargs: pytest.fail("Background reads must use the mailbox queue"))
    for workspace, domain, audience in zip((inboxes, inboxes["other"]), ("invoice-audits.example", "ceramics-club.example"), ("Accounting teams", "Home potters")):
        with inboxes["factory"]() as session:
            owner = session.get(Workspace, workspace["wid"]).owner_id
        targets = [(f"{domain}-{index}", create_workspace(kind="company", name=domain, owner_id=owner).id) for index in range(8)]
        with inboxes["factory"]() as session:
            for venture_id, workspace_id in targets:
                session.add(Venture(id=venture_id, workspace_id=workspace_id, name=domain,
                    raw_idea=f"{domain} for {audience}", stage="draft", created_at=utcnow(), updated_at=utcnow()))
            case = workspace["cases"][0]
            contact_id, touch_id = "alias-contact-" + domain, "alias-send-" + domain
            session.add(GtmContact(id=contact_id, workspace_id=targets[0][1], email="reader@" + domain,
                source=f"venture:{targets[0][0]}:founder_import", times_emailed=1,
                stage_changed_at=utcnow(), created_at=utcnow()))
            session.flush()
            session.add(GtmTouch(id=touch_id, workspace_id=targets[0][1], venture_id=targets[0][0],
                contact_id=contact_id, email_account_id=case["mailbox_id"], direction="out", channel="email",
                send_mode="founder_gmail", simulated=0, sent_at=utcnow(), message_id="<alias@" + domain + ">",
                provider_thread_id="alias-thread", created_at=utcnow()))
            session.add(GtmEmailEvent(id="alias-metadata-" + domain, workspace_id=targets[0][1],
                contact_id=contact_id, email_account_id=case["mailbox_id"], event_type="sent_metadata",
                idempotency_key="sent-metadata:" + touch_id,
                metadata_json=json.dumps({"rfc_message_id": "<alias@" + domain + ">", "thread_id": "previous-thread"}), created_at=utcnow()))
            session.commit()
        for venture_id, workspace_id in targets * 2:
            with bind(workspace_id=workspace_id, venture_id=venture_id, actor=owner):
                assert asyncio.run(outreach.poll_replies(venture_id)) == 0
        with bind(workspace_id=workspace["wid"], venture_id="", actor=owner):
            with inboxes["factory"]() as session:
                queued = list(session.scalars(select(Job).where(Job.kind == "gtm.gmail.read", Job.workspace_id == workspace["wid"])))
                assert len(queued) == len(workspace["cases"])
                payload = json.loads(queued[0].payload_json) | {"workspace_id": workspace["wid"]}
            reads = []
            async def poll(*args, reads=reads, **kwargs):
                reads.append(kwargs["mailbox_id"])
                return {"retry_after_s": 0}
            with monkeypatch.context() as patched:
                patched.setattr(outreach, "poll_replies", poll)
                asyncio.run(handlers._gmail_read(payload))
            assert reads == [payload["mailbox_id"]]
            from app.connections.composio_pacing import RateLimited
            with pytest.raises(RateLimited) as deferred:
                workers.claim_read(workspace["wid"], payload["mailbox_id"])
            assert deferred.value.retry_after_s >= 599
            with inboxes["factory"]() as session:
                leased = jobs.lease(session, "bounded-reader", kinds=["gtm.gmail.read"])[0]
                jobs.fail_job(session, leased.id, deferred.value, now=utcnow())
                assert session.get(Job, leased.id).run_after.replace(tzinfo=None) >= utcnow().replace(tzinfo=None) + timedelta(seconds=598)
                session.commit()
            from app.connections.composio_pacing import BACKGROUND
            from app.gtm.supymem import composio_gmail
            from core import clock
            def fetch_thread(*, thread_id, domain=domain, **kwargs):
                assert BACKGROUND.get()
                return {"messages": [{"id": "alias-reply-" + domain, "threadId": "alias-thread",
                    "body_text": "No thanks. Stop emailing me.", "headers": {"from": "reader@" + domain,
                    "in-reply-to": "<alias@" + domain + ">"}}]} if thread_id == "alias-thread" else {"messages": []}
            future = clock.utcnow() + timedelta(seconds=601)
            with monkeypatch.context() as patched:
                patched.setattr(clock, "utcnow", lambda future=future: future)
                patched.setattr(workers, "tick_bounces", lambda *args, **kwargs: {"marked": []})
                patched.setattr(composio_gmail, "get_gmail_profile", lambda *args: {"raw": {"historyId": "20"}})
                patched.setattr(composio_gmail, "fetch_thread", fetch_thread)
                claim = workers.claim_read(workspace["wid"], case["mailbox_id"])
                result = actual_poll(targets[0][0], mailbox_id=case["mailbox_id"], read_claim=claim)
                workers.release_read(claim)
            assert [reply["venture_id"] for reply in result["replies"]] == [targets[0][0]]
            assert not BACKGROUND.get()
            with inboxes["factory"]() as session:
                assert session.get(GtmContact, contact_id).do_not_contact == 1
                assert session.get(GtmContact, case["contact_id"]).do_not_contact == 0
                sidecar = session.scalar(select(GtmEmailEvent).where(GtmEmailEvent.idempotency_key == "conversation-mailbox:" + touch_id))
                assert sidecar.workspace_id == targets[0][1] and sidecar.email_account_id == case["mailbox_id"]


def test_rate_limited_read_waits_retry_after(inboxes, monkeypatch):
    import asyncio

    from app.connections.composio_pacing import RateLimited
    from app.core import jobs
    from app.db.models import GtmEmailEvent, Job, Workspace
    from app.gtm.supymem import composio_gmail, workers
    from app.tenancy import bind
    from app.worker import handlers
    from core import clock
    from core.clock import utcnow

    fetched, refused = [], set()
    def history(**kwargs):
        return {"historyId": "110", "history": [{"messagesAdded": [{"message": {"id": ident}}
            for ident in ("first", "second", "third")]}]}
    def message(**kwargs):
        account, ident = kwargs["connected_account_id"], kwargs["message_id"]
        fetched.append((account, ident))
        if ident == "second" and account not in refused:
            refused.add(account)
            failed_at = utcnow() + timedelta(seconds=10)
            monkeypatch.setattr(clock, "_clock", lambda: failed_at)
            raise RateLimited(125)
        return {"id": ident, "labelIds": ["SENT"]}
    monkeypatch.setattr(composio_gmail, "fetch_history", history)
    monkeypatch.setattr(composio_gmail, "fetch_message", message)
    monkeypatch.setattr(workers, "tick_bounces", lambda *args, **kwargs: {"marked": []})
    for index, workspace in enumerate((inboxes, inboxes["other"])):
        with inboxes["factory"]() as session:
            owner = session.get(Workspace, workspace["wid"]).owner_id
            case = workspace["cases"][index]
            session.add(GtmEmailEvent(id="history-start-" + case["mailbox_id"], workspace_id=workspace["wid"],
                email_account_id=case["mailbox_id"], event_type="gmail_sync_checkpoint", created_at=utcnow(),
                metadata_json=json.dumps({"history_id": "100"})))
            session.commit()
        with bind(workspace_id=workspace["wid"], venture_id="", actor=owner), inboxes["factory"]() as session:
            job_id = jobs.enqueue(session, "gtm.gmail.read", {"mailbox_id": case["mailbox_id"]}, workspace_id=workspace["wid"])
            session.commit()
            row = jobs.lease(session, "limited-reader", kinds=["gtm.gmail.read"])[0]
            session.commit()
            payload = row.payload | {"workspace_id": workspace["wid"]}
            with pytest.raises(RateLimited) as error:
                asyncio.run(handlers._gmail_read(payload))
            moment = utcnow()
            assert jobs.fail_job(session, job_id, error.value, now=moment) == "retry_wait"
            session.commit()
            assert session.get(Job, job_id).run_after.replace(tzinfo=None) >= moment.replace(tzinfo=None) + timedelta(seconds=125)
            assert jobs.lease(session, "limited-reader", kinds=["gtm.gmail.read"], now=moment + timedelta(seconds=124)) == []
            cursor = session.scalar(select(GtmEmailEvent).where(GtmEmailEvent.email_account_id == case["mailbox_id"],
                GtmEmailEvent.event_type == "gmail_sync_checkpoint").order_by(GtmEmailEvent.created_at.desc()))
            assert json.loads(cursor.metadata_json)["pending_ids"] == ["second", "third"]
            retry_at = session.get(Job, job_id).run_after
            row = jobs.lease(session, "limited-reader", kinds=["gtm.gmail.read"], now=retry_at)[0]
            session.commit()
            with monkeypatch.context() as patched:
                patched.setattr(clock, "_clock", lambda retry_at=retry_at: retry_at)
                asyncio.run(handlers._gmail_read(row.payload))
            assert [ident for account, ident in fetched if account == case["ca"]] == ["first", "second", "second", "third"]


def test_history_batches_resume_without_skipping_other_founders_and_backoff_is_per_inbox(inboxes, monkeypatch):
    from app.connections.composio_pacing import RateLimited
    from app.core import scheduled
    from app.db.models import Job, Workspace
    from app.gtm.supymem import composio_gmail, workers
    from app.tenancy import bind
    monkeypatch.setenv("GMAIL_READ_BATCH_SIZE", "2")
    monkeypatch.setenv("SCHEDULED_FANOUT_BATCH_SIZE", "1")
    fetched = []
    deferred = set()
    def history(**kwargs):
        return {"historyId": "90", "history": [{"messagesAdded": [{"message": {"id": str(index)}} for index in range(7)]}]}
    def message(**kwargs):
        fetched.append((kwargs["connected_account_id"], kwargs["message_id"]))
        if kwargs["message_id"] == "3" and kwargs["connected_account_id"] not in deferred:
            deferred.add(kwargs["connected_account_id"])
            raise RateLimited(30)
        return {"id": kwargs["message_id"]}
    monkeypatch.setattr(composio_gmail, "fetch_history", history)
    monkeypatch.setattr(composio_gmail, "fetch_message", message)
    selected = [inboxes, inboxes["other"]]
    for workspace in selected:
        with inboxes["factory"]() as session:
            owner = session.get(Workspace, workspace["wid"]).owner_id
        with bind(workspace_id=workspace["wid"], venture_id="", actor=owner):
            for mailbox in workspace["cases"]:
                scheduled.enqueue_mailboxes(mailbox["venture_id"], window="reading-window")
            with inboxes["factory"]() as session:
                first_page = list(session.scalars(select(Job).where(Job.kind == "gtm.gmail.read", Job.workspace_id == workspace["wid"])))
                assert len(first_page) == 2
                assert any(json.loads(row.payload_json).get("mailbox_id") == workspace["cases"][0]["mailbox_id"] for row in first_page)
            for mailbox in workspace["cases"]:
                scheduled.enqueue_mailboxes(workspace_id=workspace["wid"], cursor=mailbox["mailbox_id"], window="reading-window")
        with inboxes["factory"]() as session:
            queued = list(session.scalars(select(Job).where(Job.kind == "gtm.gmail.read", Job.workspace_id == workspace["wid"])))
            assert len(queued) == 4 and all(row.venture_id is None for row in queued)
            assert sum(bool(json.loads(row.payload_json).get("mailbox_id")) for row in queued) == 2
        case = workspace["cases"][0]
        progress = "50"
        seen = []
        for _ in range(6):
            before = len(fetched)
            messages, progress = workers._history_messages(workspace["wid"], case["uid"], case["ca"], progress)
            assert len(fetched) - before <= 2
            seen.extend(message["id"] for message in messages)
            progress = json.loads(json.dumps(progress))
            if progress == "90":
                break
        assert progress == "90" and sorted(seen) == [str(index) for index in range(7)]
    first, second = selected
    claim = workers.claim_read(first["wid"], first["cases"][0]["mailbox_id"])
    workers.release_read(claim, error=RateLimited(120))
    with pytest.raises(RateLimited):
        workers.claim_read(first["wid"], first["cases"][0]["mailbox_id"])
    other = workers.claim_read(second["wid"], second["cases"][0]["mailbox_id"])
    workers.release_read(other)
    from app.workspaces.service import create_workspace
    with inboxes["factory"]() as session:
        owner = session.get(Workspace, second["wid"]).owner_id
    sibling = create_workspace(kind="company", name="Another workspace", owner_id=owner)
    with pytest.raises(RateLimited) as sibling_wait:
        workers.claim_read(sibling.id, second["cases"][0]["mailbox_id"])
    assert sibling_wait.value.retry_after_s >= 599
    with pytest.raises(RateLimited) as shared_backoff:
        workers.claim_read(second["wid"], second["cases"][0]["mailbox_id"])
    assert shared_backoff.value.retry_after_s >= 119
    from app.autopilot import outreach
    from app.credits.policy import set_policy
    from app.db.models import Venture
    empty_id = second["cases"][0]["venture_id"] + "-empty"
    with inboxes["factory"]() as session:
        source = session.get(Venture, second["cases"][0]["venture_id"])
        session.add(Venture(id=empty_id, workspace_id=second["wid"], name="Another idea", raw_idea="Unused project",
            stage="draft", created_at=source.created_at, updated_at=source.updated_at))
        session.commit()
    reads = []
    monkeypatch.setattr(workers, "tick_bounces", lambda wid, **kwargs: reads.append(("bounces", wid, kwargs)) or {"marked": []})
    monkeypatch.setattr(workers, "tick_replies", lambda wid, **kwargs: reads.append(("replies", wid, kwargs)) or {"replies": []})
    with bind(workspace_id=second["wid"], venture_id=empty_id, actor=owner):
        assert outreach._poll(empty_id, mailbox_id=second["cases"][0]["mailbox_id"])["replies"] == []
    assert [kind for kind, _, _ in reads] == ["bounces", "replies"]
    set_policy("feature_flags", {"multi_gmail_v1": False}, "test")
    for workspace in selected:
        with inboxes["factory"]() as session:
            founder = session.get(Workspace, workspace["wid"]).owner_id
        with bind(workspace_id=workspace["wid"], venture_id="", actor=founder):
            assert scheduled.enqueue_mailboxes(workspace_id=workspace["wid"], window="legacy-window") == 1
        with inboxes["factory"]() as session:
            legacy = list(session.scalars(select(Job).where(Job.kind == "gtm.gmail.read", Job.workspace_id == workspace["wid"], Job.dedupe_key.like("%legacy-window"))))
            assert len(legacy) == 1 and json.loads(legacy[0].payload_json)["mailbox_id"] == workspace["cases"][1]["mailbox_id"]


@pytest.fixture(params=[0, 1], ids=["business", "consumer"])
def inboxes(tmp_path, monkeypatch, request):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/multi-read.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("COMPOSIO_PROJECT", raising=False)
    load_company_os_app()
    from app import state
    from app.authz.workspace import ensure_subject_person
    from app.connections import composio_migration
    from app.credits import metered
    from app.credits.policy import set_policy
    from app.db.models import (
        GtmContact,
        GtmEmailAccount,
        GtmEmailEvent,
        GtmTouch,
        Venture,
    )
    from app.gtm.relationships.service import _settings
    from app.gtm.supymem import composio_gmail
    from app.workspaces.service import create_workspace
    from core.clock import utcnow

    if request.node.originalname in {"test_shared_connected_accounts_enqueue_once_per_owner_and_window",
            "test_superseded_read_preserves_checkpoint_and_next_read_finds_reply"}:
        table = GtmEmailAccount.__table__.to_metadata(MetaData())
        table.constraints = {constraint for constraint in table.constraints
            if constraint.name != "uq_gtm_email_account_ca"}
        engine = create_engine(f"sqlite:///{tmp_path}/multi-read.db")
        try:
            table.create(engine)
        finally:
            engine.dispose()
    state.reset_db_factory()
    set_policy("feature_flags", {"multi_gmail_v1": True}, "test")
    monkeypatch.setattr(composio_migration, "reconcile_gmail_project", lambda wid: {})
    monkeypatch.setattr(composio_gmail, "_request", lambda *args, **kwargs: pytest.fail("No network in mailbox tests"))
    metered_workspaces = []

    @contextmanager
    def read_meter(**kwargs):
        assert kwargs["purpose"] == "gtm.gmail.read"
        assert kwargs["estimate_micro"] == 0
        metered_workspaces.append(kwargs["workspace_id"])
        yield type("ReadMeter", (), {"record_micro": lambda self, amount: None})()

    monkeypatch.setattr(metered, "charge_sync", read_meter)
    workspaces = []
    now = utcnow()
    for founder, domains in enumerate((("stockroom.example", "trailclub.example"), ("dispatch.example", "craftclub.example"))):
        owner = ensure_subject_person(f"usr_read_{founder}")
        workspace = create_workspace(kind="company", name=f"Read fixture {founder}", owner_id=owner)
        cases = []
        with state.db()() as session:
            for index, domain in enumerate(domains):
                account_id, contact_id, venture_id = (f"account_{founder}_{index}", f"contact_{founder}_{index}", f"venture_{founder}_{index}")
                session.add(GtmEmailAccount(id=account_id, workspace_id=workspace.id, email=f"you@{domain}",
                    composio_user_id=f"provider_user_{founder}_{index}", connected_account_id=f"ca_read_{founder}_{index}",
                    status="active", created_at=now, updated_at=now + timedelta(seconds=index)))
                session.add(Venture(id=venture_id, workspace_id=workspace.id, name=f"Read idea {founder} {index}",
                    raw_idea="Inventory reconciliation for wholesalers" if index == 0 else "Weekend art classes for families",
                    stage="draft", created_at=now, updated_at=now))
                session.add(GtmContact(id=contact_id, workspace_id=workspace.id, email=f"reader@{domain}",
                    first_name=f"Reader {founder} {index}", source=f"venture:{venture_id}:founder_import",
                    times_emailed=10, gmail_thread_id="shared-thread", stage_changed_at=now, created_at=now))
                session.flush()
                for send in range(10):
                    touch_id = f"send_{founder}_{index}_{send}"
                    session.add(GtmTouch(id=touch_id, workspace_id=workspace.id, venture_id=venture_id,
                        contact_id=contact_id, direction="out", channel="email", simulated=0, send_mode="founder_gmail", sent_at=now,
                        provider_thread_id="shared-thread", message_id=f"<sent_{send}@{domain}>",
                        in_reply_to=f"provider-sent-{send}", created_at=now))
                    session.add(GtmEmailEvent(id=f"source_{founder}_{index}_{send}", workspace_id=workspace.id,
                        contact_id=contact_id, email_account_id=account_id, event_type="sent_metadata",
                        idempotency_key="sent-metadata:" + touch_id, provider_message_id=f"provider-sent-{send}",
                        metadata_json=json.dumps({"thread_id": "shared-thread", "rfc_message_id": f"<sent_{send}@{domain}>"}), created_at=now))
                cases.append({"mailbox_id": account_id, "contact_id": contact_id, "venture_id": venture_id,
                    "ca": f"ca_read_{founder}_{index}", "uid": f"provider_user_{founder}_{index}", "email": f"reader@{domain}"})
            _settings(session, workspace.id).send_enabled = True
            session.commit()
        workspaces.append({"wid": workspace.id, "cases": cases})

    def no_history_rewrites(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("UPDATE "):
            assert not any(table in statement.lower() for table in ("gtm_touches", "gtm_email_events", "connection_events"))

    with state.db()() as session:
        engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", no_history_rewrites)
    data = {**workspaces[request.param], "other": workspaces[1 - request.param], "factory": state.db(),
        "metered": metered_workspaces}
    yield data
    event.remove(engine, "before_cursor_execute", no_history_rewrites)


@pytest.mark.parametrize("reader", ["replies", "bounces"])
def test_bootstrap_profile_recovery_restarts_snapshot_before_advancing_cursor(inboxes, monkeypatch, reader):
    from app.gtm.supymem import composio_gmail, workers

    monkeypatch.setenv("GMAIL_READ_BATCH_SIZE", "2")
    monkeypatch.setattr(composio_gmail, "get_gmail_profile", lambda *args: {"raw": {"historyId": "90"}})
    calls = []

    def thread(**kwargs):
        calls.append((kwargs["connected_account_id"], kwargs["thread_id"]))
        return {"messages": [{"id": kwargs["thread_id"]}]}

    def query(**kwargs):
        calls.append((kwargs["connected_account_id"], kwargs.get("page_token")))
        if not kwargs.get("page_token"):
            return {"messages": [{"id": "restart-bounce"}], "nextPageToken": "pending-page"}
        return {"messages": [{"id": "last-bounce"}]}

    monkeypatch.setattr(composio_gmail, "fetch_thread", thread)
    monkeypatch.setattr(composio_gmail, "fetch_query", query)
    for case in inboxes["cases"]:
        if reader == "replies":
            tracked = [("touch", case["contact_id"], case["venture_id"], ident, None, None)
                for ident in ("completed-thread", "pending-thread")]
            checkpoint = {"target_history_id": None, "bootstrap_tasks": ["thread:pending-thread"]}
            messages, next_history = workers._bootstrap_replies(inboxes["wid"], case["uid"], case["ca"], tracked, checkpoint)
            assert [message["id"] for message in messages] == ["completed-thread"]
            assert calls[-1] == (case["ca"], "completed-thread")
            assert next_history["target_history_id"] == "90"
            assert next_history["bootstrap_tasks"] == ["thread:pending-thread"]
            messages, next_history = workers._bootstrap_replies(inboxes["wid"], case["uid"], case["ca"], tracked,
                json.loads(json.dumps(next_history)))
            assert [message["id"] for message in messages] == ["pending-thread"]
            assert calls[-1] == (case["ca"], "pending-thread")
        else:
            checkpoint = {"target_history_id": None, "bootstrap_page": "pending-page", "page_tokens": ["pending-page"]}
            messages, next_history = workers._bootstrap_bounces(inboxes["wid"], case["uid"], case["ca"], checkpoint)
            assert [message["id"] for message in messages] == ["restart-bounce"]
            assert calls[-1] == (case["ca"], None)
            assert next_history["target_history_id"] == "90" and next_history["bootstrap_page"] == "pending-page"
            messages, next_history = workers._bootstrap_bounces(inboxes["wid"], case["uid"], case["ca"],
                json.loads(json.dumps(next_history)))
            assert [message["id"] for message in messages] == ["last-bounce"]
            assert calls[-1] == (case["ca"], "pending-page")
        assert next_history == "90"
    assert inboxes["metered"] == [inboxes["wid"]] * 6


@pytest.mark.parametrize("reader", ["history", "bootstrap"])
def test_slow_gmail_batches_commit_progress_before_time_budget(inboxes, monkeypatch, reader):
    from app.gtm.supymem import composio_gmail, workers

    monkeypatch.setenv("GMAIL_READ_BATCH_SIZE", "3")
    elapsed = [0]
    monkeypatch.setattr(workers.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(composio_gmail, "get_gmail_profile", lambda *args: {"raw": {"historyId": "90"}})
    monkeypatch.setattr(composio_gmail, "fetch_history", lambda **kwargs: {"historyId": "90",
        "history": [{"messagesAdded": [{"message": {"id": ident}} for ident in ("first", "second")]}]})

    def fetch(**kwargs):
        elapsed[0] += workers._READ_MAX_SECONDS + 1
        ident = kwargs.get("message_id") or kwargs["thread_id"]
        return {"id": ident} if reader == "history" else {"messages": [{"id": ident}]}

    monkeypatch.setattr(composio_gmail, "fetch_message", fetch)
    monkeypatch.setattr(composio_gmail, "fetch_thread", fetch)
    for case in inboxes["cases"]:
        tracked = [("touch", case["contact_id"], case["venture_id"], ident, None, None)
            for ident in ("first", "second")]
        checkpoint = "50" if reader == "history" else None
        for expected in ("first", "second"):
            if reader == "history":
                messages, checkpoint = workers._history_messages(inboxes["wid"], case["uid"], case["ca"], checkpoint)
            else:
                messages, checkpoint = workers._bootstrap_replies(inboxes["wid"], case["uid"], case["ca"], tracked, checkpoint)
            assert [message["id"] for message in messages] == [expected]
            checkpoint = json.loads(json.dumps(checkpoint))
        assert checkpoint == "90"


def test_mailbox_lease_recovery_fences_stale_release(inboxes, monkeypatch):
    from app.connections.composio_pacing import RateLimited
    from app.gtm.supymem import workers
    from app.inbox import store
    from core import clock

    now = clock.utcnow()
    monkeypatch.setattr(clock, "utcnow", lambda: now)
    for case in inboxes["cases"]:
        stale = workers.claim_read(inboxes["wid"], case["mailbox_id"])
        with pytest.raises(RateLimited):
            workers.claim_read(inboxes["wid"], case["mailbox_id"])
        now += timedelta(minutes=11)
        replacement = workers.claim_read(inboxes["wid"], case["mailbox_id"])
        workers.release_read(stale, error=RateLimited(120))
        with inboxes["factory"]() as session:
            data = json.loads(session.get(store.InboxBriefingState, replacement[1]).data_json)
        assert data["gmail_lease_id"] == replacement[2]
        assert "gmail_backoff_until" not in data
        workers.release_read(replacement, error=RateLimited(120))
        workers.release_read(stale)
        with pytest.raises(RateLimited) as blocked:
            workers.claim_read(inboxes["wid"], case["mailbox_id"])
        assert blocked.value.retry_after_s == 120
    other = inboxes["other"]
    independent = workers.claim_read(other["wid"], other["cases"][0]["mailbox_id"])
    workers.release_read(independent)


@pytest.mark.parametrize("reader", ["replies", "bounces"])
@pytest.mark.parametrize("loss", ["replaced", "expired", "released", "active"])
def test_gmail_handler_fences_late_checkpoints_with_the_read_claim(inboxes, monkeypatch, reader, loss):
    import asyncio

    from app.core.jobs import RetryableError
    from app.db.models import GtmEmailEvent, Workspace
    from app.gtm.supymem import composio_gmail, workers
    from app.tenancy import bind
    from app.worker import handlers
    from core import clock

    now = [clock.utcnow()]
    monkeypatch.setattr(clock, "utcnow", lambda: now[0])
    claims = []
    claim_read = workers.claim_read

    def capture_claim(*args, **kwargs):
        claim = claim_read(*args, **kwargs)
        claims.append(claim)
        return claim

    monkeypatch.setattr(workers, "claim_read", capture_claim)
    monkeypatch.setattr(workers, "tick_bounces" if reader == "replies" else "tick_replies",
        lambda *args, **kwargs: {"marked": [], "replies": []})
    event_type = "gmail_sync_checkpoint" if reader == "replies" else "gmail_bounce_checkpoint"
    with inboxes["factory"]() as session:
        owner = session.get(Workspace, inboxes["wid"]).owner_id

    for case in inboxes["cases"]:
        with inboxes["factory"]() as session:
            session.add(GtmEmailEvent(id="old-lease-cursor-" + case["mailbox_id"], workspace_id=inboxes["wid"],
                email_account_id=case["mailbox_id"], event_type=event_type,
                metadata_json=json.dumps({"history_id": "50"}), created_at=now[0]))
            session.commit()

        def history(case=case, **kwargs):
            assert kwargs["connected_account_id"] == case["ca"] and kwargs["start_history_id"] == "50"
            if loss == "released":
                workers.release_read(claims[-1])
            elif loss != "active":
                now[0] += timedelta(minutes=11)
                if loss == "replaced":
                    claim_read(inboxes["wid"], case["mailbox_id"])
                    with inboxes["factory"]() as session:
                        session.add(GtmEmailEvent(id="replacement-cursor-" + case["mailbox_id"], workspace_id=inboxes["wid"],
                            email_account_id=case["mailbox_id"], event_type=event_type,
                            metadata_json=json.dumps({"history_id": "200"}), created_at=now[0]))
                        session.commit()
            return {"historyId": "100", "history": []}

        monkeypatch.setattr(composio_gmail, "fetch_history", history)
        with bind(workspace_id=inboxes["wid"], venture_id="", actor=owner):
            if loss == "active":
                asyncio.run(handlers._gmail_read({"workspace_id": inboxes["wid"], "mailbox_id": case["mailbox_id"]}))
            else:
                with pytest.raises(RetryableError, match="gmail_read_lease_lost"):
                    asyncio.run(handlers._gmail_read({"workspace_id": inboxes["wid"], "mailbox_id": case["mailbox_id"]}))
        with inboxes["factory"]() as session:
            checkpoints = list(session.scalars(select(GtmEmailEvent).where(
                GtmEmailEvent.email_account_id == case["mailbox_id"], GtmEmailEvent.event_type == event_type)))
            expected = ["200", "50"] if loss == "replaced" else ["100", "50"] if loss == "active" else ["50"]
            assert sorted(json.loads(row.metadata_json)["history_id"] for row in checkpoints) == expected


def test_query_pagination_uses_existing_read_helper_and_keeps_legacy_arguments(inboxes, monkeypatch):
    from app.gtm.supymem import composio_gmail
    calls = []
    monkeypatch.setattr(composio_gmail, "execute_tool", lambda slug, **kwargs: calls.append((slug, kwargs)) or {"data": {"messages": []}})
    for case in inboxes["cases"]:
        arguments = {"composio_user_id": case["uid"], "connected_account_id": case["ca"], "query": "in:inbox", "include_payload": True}
        assert composio_gmail.fetch_query(**arguments) == {"messages": []}
        assert calls[-1] == ("GMAIL_FETCH_EMAILS", {"user_id": case["uid"], "connected_account_id": case["ca"],
            "arguments": {"query": "in:inbox", "max_results": 15, "include_payload": True, "verbose": False}})
        assert composio_gmail.fetch_query(**arguments, page_token="next-page") == {"messages": []}
        assert calls[-1][0] == "GMAIL_FETCH_EMAILS" and calls[-1][1]["arguments"]["page_token"] == "next-page"


def _reply(case, *, booking=False):
    headers = {"from": case["email"], "in-reply-to": "<sent_0@" + case["email"].split("@", 1)[1] + ">"}
    if booking:
        headers.update({"content-type": "text/calendar; method=REPLY", "subject": "Accepted: Interview"})
    return {"id": "shared-message", "threadId": "shared-thread", "headers": headers,
        "body_text": "PARTSTAT=ACCEPTED" if booking else "Yes, interested"}


def _reads(data, monkeypatch, *, booking=False, shared_thread=True):
    from app.gtm.supymem import composio_gmail

    calls = []
    by_ca = {case["ca"]: case for case in data["cases"]}

    def fetch(**kwargs):
        case = by_ca[kwargs["connected_account_id"]]
        assert kwargs["composio_user_id"] == case["uid"]
        calls.append(case["mailbox_id"])
        message = _reply(case, booking=booking)
        if not shared_thread:
            message["threadId"] = "forwarded-thread"
        return {"messages": [message]}

    monkeypatch.setattr(composio_gmail, "fetch_thread", fetch)
    monkeypatch.setattr(composio_gmail, "get_gmail_profile", lambda *args: {"raw": {}})
    return calls


@pytest.mark.parametrize("checkpoint_kind", ["fresh", "saved_seeds", "overflow"])
def test_reply_bootstrap_uses_bounded_thread_reads_for_raw_sent_ids(inboxes, monkeypatch, checkpoint_kind):
    from app.gtm.supymem import composio_gmail, workers

    calls = _reads(inboxes, monkeypatch)
    read_thread = composio_gmail.fetch_thread
    def fetch_thread(**kwargs):
        result = read_thread(**kwargs)
        result["messages"][0].update(id=kwargs["thread_id"], threadId=kwargs["thread_id"])
        return result
    monkeypatch.setattr(composio_gmail, "fetch_thread", fetch_thread)
    monkeypatch.setenv("GMAIL_READ_BATCH_SIZE", "2")
    monkeypatch.setattr(composio_gmail, "get_gmail_profile", lambda *args: {"raw": {"historyId": "100"}})
    by_ca = {case["ca"]: case for case in inboxes["cases"]}
    def fetch_message(**kwargs):
        assert checkpoint_kind == "overflow", "Tracked sends with threads must use the thread read helper"
        case = by_ca[kwargs["connected_account_id"]]
        assert kwargs["composio_user_id"] == case["uid"]
        calls.append(case["mailbox_id"])
        return {**_reply(case), "id": kwargs["message_id"]}
    monkeypatch.setattr(composio_gmail, "fetch_message", fetch_message)
    for index, case in enumerate(inboxes["cases"]):
        tracked = [("first-touch", case["contact_id"], case["venture_id"], "shared-thread", "first-sent", None),
            ("second-touch", case["contact_id"], case["venture_id"], "second-thread", "second-sent", None)]
        if index:
            tracked.reverse()
        checkpoint = {"target_history_id": "100", "bootstrap_tasks": ["message:second-sent",
            "thread:second-thread", "message:first-sent", "thread:shared-thread"]} if checkpoint_kind == "saved_seeds" else None
        if checkpoint_kind == "overflow":
            checkpoint = {"target_history_id": "100", "bootstrap_tasks": ["message:spill-reply", "thread:shared-thread"]}
        for batch in range(2):
            before = len(calls)
            messages, checkpoint = workers._bootstrap_replies(inboxes["wid"], case["uid"], case["ca"], tracked, checkpoint)
            assert calls[before:] == [case["mailbox_id"]]
            assert len(messages) == 1 and messages[0]["headers"]["from"] == case["email"]
            assert messages[0]["id"] == (("spill-reply" if checkpoint_kind == "overflow" else "second-thread"), "shared-thread")[batch]
            if batch == 0:
                checkpoint = json.loads(json.dumps(checkpoint))
                assert checkpoint["bootstrap_tasks"] == ["thread:shared-thread"]
            else:
                assert checkpoint == "100"
    assert set(inboxes["metered"]) == {inboxes["wid"]}


@pytest.mark.parametrize("enabled", [False, True])
def test_reply_fanout_and_mailbox_qualified_dedupe_keep_flag_off_shape(inboxes, monkeypatch, enabled):
    from app.credits.policy import set_policy
    from app.db.models import GtmEmailEvent, GtmTouch
    from app.gtm.supymem import workers

    set_policy("feature_flags", {"multi_gmail_v1": enabled}, "test")
    calls = _reads(inboxes, monkeypatch, shared_thread=enabled)
    result = workers.tick_replies(inboxes["wid"])
    expected = inboxes["cases"] if enabled else [inboxes["cases"][1]]
    assert set(result["marked"]) == {case["contact_id"] for case in expected}
    assert set(calls) == {case["mailbox_id"] for case in expected}
    assert set(result) == ({"marked", "replies", "mailboxes"} if enabled else {"marked", "replies"})
    assert len({reply["message_id"] for reply in result["replies"]}) == len(expected)
    if enabled:
        assert all(reply["provider_message_id"] == "shared-message" for reply in result["replies"])
    assert workers.tick_replies(inboxes["wid"])["marked"] == []
    with inboxes["factory"]() as session:
        touches = list(session.scalars(select(GtmTouch).where(GtmTouch.direction == "in")))
        assert len(touches) == len(expected)
        receipts = list(session.scalars(select(GtmEmailEvent).where(GtmEmailEvent.event_type == "gmail_reply_attribution")))
        assert {receipt.email_account_id for receipt in receipts} == ({case["mailbox_id"] for case in expected} if enabled else set())
        assert all(receipt.provider_message_id == "shared-message" for receipt in receipts)
        if enabled:
            assert {(touch.email_account_id, touch.message_id) for touch in touches} == {
                (case["mailbox_id"], "shared-message") for case in expected}
        if not enabled:
            assert touches[0].message_id == "shared-message"
            assert set(result["replies"][0]) == {"contact_id", "venture_id", "kind", "body", "message_id"}
    assert set(inboxes["metered"]) == {inboxes["wid"]}


def test_reply_fetch_failure_does_not_advance_one_inbox_or_stop_another(inboxes, monkeypatch):
    from app.db.models import GtmEmailEvent
    from app.gtm.supymem import composio_gmail, workers
    from core.clock import utcnow

    bad, good = inboxes["cases"]
    with inboxes["factory"]() as session:
        for case in inboxes["cases"]:
            session.add(GtmEmailEvent(id="cursor_" + case["mailbox_id"], workspace_id=inboxes["wid"],
                email_account_id=case["mailbox_id"], event_type="gmail_sync_checkpoint", created_at=utcnow(),
                metadata_json=json.dumps({"history_id": "100"})))
        session.commit()

    def history(**kwargs):
        assert kwargs["start_history_id"] == "100"
        return {"historyId": "110", "history": [{"messagesAdded": [{"message": {"id": "shared-message"}}]}]}

    def message(**kwargs):
        if kwargs["connected_account_id"] == bad["ca"]:
            raise RuntimeError("broken inbox")
        return _reply(good)

    monkeypatch.setattr(composio_gmail, "fetch_history", history)
    monkeypatch.setattr(composio_gmail, "fetch_message", message)
    result = workers.tick_replies(inboxes["wid"])
    assert result["marked"] == [good["contact_id"]]
    assert next(item for item in result["mailboxes"] if item["mailbox_id"] == bad["mailbox_id"])["error"] == "gmail_message_fetch_failed"
    with inboxes["factory"]() as session:
        cursors = list(session.scalars(select(GtmEmailEvent).where(GtmEmailEvent.event_type == "gmail_sync_checkpoint")))
        assert [json.loads(row.metadata_json)["history_id"] for row in cursors if row.email_account_id == bad["mailbox_id"]] == ["100"]
        assert {json.loads(row.metadata_json)["history_id"] for row in cursors if row.email_account_id == good["mailbox_id"]} == {"100", "110"}


@pytest.mark.parametrize("invalid", ["foreign", "revoked", "missing"])
def test_explicit_read_selection_fails_closed_without_fallback(inboxes, monkeypatch, invalid):
    from app.connections.mailboxes import MailboxUnavailable
    from app.db.models import GtmEmailAccount
    from app.gtm.supymem import workers

    calls = _reads(inboxes, monkeypatch)
    selected = inboxes["other"]["cases"][0]["mailbox_id"] if invalid == "foreign" else "missing"
    if invalid == "revoked":
        selected = inboxes["cases"][0]["mailbox_id"]
        with inboxes["factory"]() as session:
            session.get(GtmEmailAccount, selected).status = "needs_reconnect"
            session.commit()
    for tick in (workers.tick_replies, workers.tick_bounces):
        with pytest.raises(MailboxUnavailable):
            tick(inboxes["wid"], mailbox_id=selected)
    assert calls == []


def test_reconnect_during_poll_does_not_record_reply_or_stop_other_inbox(inboxes, monkeypatch):
    from app.db.models import GtmEmailAccount, GtmTouch
    from app.gtm.supymem import composio_gmail, workers

    _reads(inboxes, monkeypatch)
    bad, good = inboxes["cases"]

    def fetch(**kwargs):
        case = bad if kwargs["connected_account_id"] == bad["ca"] else good
        if case is bad:
            with inboxes["factory"]() as session:
                session.get(GtmEmailAccount, bad["mailbox_id"]).status = "needs_reconnect"
                session.commit()
        return {"messages": [_reply(case)]}

    monkeypatch.setattr(composio_gmail, "fetch_thread", fetch)
    assert workers.tick_replies(inboxes["wid"])["marked"] == [good["contact_id"]]
    with inboxes["factory"]() as session:
        assert not session.scalar(select(GtmTouch.id).where(GtmTouch.direction == "in", GtmTouch.contact_id == bad["contact_id"]))


def _dsn(email):
    text = f"Final-Recipient: rfc822; {email}\r\nAction: failed\r\nStatus: 5.1.1\r\n"
    return {"id": "same-dsn", "payload": {"mimeType": "message/delivery-status",
        "body": {"data": base64.urlsafe_b64encode(text.encode()).decode()}}}


def test_bounce_fanout_attributes_each_inbox_and_keeps_workspace_pause(inboxes, monkeypatch):
    from app.db.models import GtmEmailEvent, GtmOutreachSettings
    from app.gtm.supymem import composio_gmail, workers

    cases = {case["ca"]: case for case in inboxes["cases"]}
    monkeypatch.setattr(composio_gmail, "get_gmail_profile", lambda *args: {"raw": {"historyId": "10"}})
    monkeypatch.setattr(composio_gmail, "fetch_query", lambda **kwargs: [_dsn(cases[kwargs["connected_account_id"]]["email"]),
        _dsn(inboxes["other"]["cases"][0]["email"])])
    result = workers.tick_bounces(inboxes["wid"])
    assert set(result["marked"]) == {case["contact_id"] for case in inboxes["cases"]}
    with inboxes["factory"]() as session:
        assert not session.get(GtmOutreachSettings, inboxes["wid"]).send_enabled
        assert session.get(GtmOutreachSettings, inboxes["other"]["wid"]).send_enabled
        receipts = list(session.scalars(select(GtmEmailEvent).where(GtmEmailEvent.event_type == "gmail_bounce_attribution")))
        assert {row.email_account_id for row in receipts} == {case["mailbox_id"] for case in inboxes["cases"]}
        assert all(row.provider_message_id == "same-dsn" for row in receipts)
        cursors = list(session.scalars(select(GtmEmailEvent).where(GtmEmailEvent.event_type == "gmail_bounce_checkpoint")))
        assert len(cursors) == 2
    monkeypatch.setattr(composio_gmail, "fetch_history", lambda **kwargs: {"historyId": "20", "history": []})
    assert workers.tick_bounces(inboxes["wid"])["marked"] == []


def test_bounce_failure_keeps_independent_checkpoint(inboxes, monkeypatch):
    from app.db.models import GtmEmailEvent
    from app.gtm.supymem import composio_gmail, workers

    bad, good = inboxes["cases"]
    monkeypatch.setattr(composio_gmail, "get_gmail_profile", lambda *args: {"raw": {"historyId": "30"}})

    def fetch(**kwargs):
        if kwargs["connected_account_id"] == bad["ca"]:
            raise RuntimeError("broken inbox")
        return [_dsn(good["email"])]

    monkeypatch.setattr(composio_gmail, "fetch_query", fetch)
    result = workers.tick_bounces(inboxes["wid"])
    assert result["marked"] == [good["contact_id"]]
    with inboxes["factory"]() as session:
        assert not session.scalar(select(GtmEmailEvent.id).where(GtmEmailEvent.email_account_id == bad["mailbox_id"],
            GtmEmailEvent.event_type == "gmail_bounce_checkpoint"))


def test_historical_hashed_replies_without_sidecar_receipts_are_not_duplicated(inboxes, monkeypatch):
    from app.db.models import GtmEmailEvent, GtmTouch
    from app.gtm.supymem import workers
    from core.clock import utcnow
    _reads(inboxes, monkeypatch)
    with inboxes["factory"]() as session:
        for case in inboxes["cases"]:
            session.add(GtmTouch(id="historical_" + case["contact_id"], workspace_id=inboxes["wid"], venture_id=case["venture_id"],
                contact_id=case["contact_id"], direction="in", simulated=0,
                message_id=workers._mailbox_message_key(case["mailbox_id"], "shared-message"), created_at=utcnow()))
        session.commit()
        assert not session.scalar(select(GtmEmailEvent.id).where(GtmEmailEvent.event_type == "gmail_reply_attribution"))
    assert workers.tick_replies(inboxes["wid"])["marked"] == []
    with inboxes["factory"]() as session:
        assert {touch.id for touch in session.scalars(select(GtmTouch).where(GtmTouch.direction == "in"))} == {
            "historical_" + case["contact_id"] for case in inboxes["cases"]}


def test_legacy_inbound_and_bounce_receipts_keep_their_dedupe(inboxes, monkeypatch):
    from app.db.models import GtmEmailEvent, GtmTouch
    from app.gtm.supymem import workers
    from core.clock import utcnow

    _reads(inboxes, monkeypatch)
    with inboxes["factory"]() as session:
        for case in inboxes["cases"]:
            session.add(GtmTouch(id="legacy_" + case["contact_id"], workspace_id=inboxes["wid"], venture_id=case["venture_id"],
                contact_id=case["contact_id"], direction="in", simulated=0, message_id="shared-message", created_at=utcnow()))
            key = "bounce:" + hashlib.sha256(json.dumps([inboxes["wid"], "same-dsn", case["email"]]).encode()).hexdigest()
            session.add(GtmEmailEvent(id="old_bounce_" + case["contact_id"], workspace_id=inboxes["wid"],
                contact_id=case["contact_id"], event_type="bounce", provider_message_id="same-dsn", idempotency_key=key, created_at=utcnow()))
        session.commit()
    assert workers.tick_replies(inboxes["wid"])["marked"] == []
    for case in inboxes["cases"]:
        assert workers._apply_delivery_status(_dsn(case["email"]), {case["email"]: case["contact_id"]},
            email_account_id=case["mailbox_id"]) == []
    with inboxes["factory"]() as session:
        assert not session.scalar(select(GtmEmailEvent.id).where(GtmEmailEvent.event_type.in_(
            ["gmail_reply_attribution", "gmail_bounce_attribution"])))


def test_booking_reply_headers_are_attributed_without_changing_booking_reads(inboxes, monkeypatch):
    from app.connections import bookings
    from app.db.models import GtmEmailEvent
    from app.gtm.supymem import workers

    _reads(inboxes, monkeypatch, booking=True)
    monkeypatch.setattr(bookings, "_call", lambda *args: pytest.fail("Gmail attribution must not read calendars"))
    assert len(workers.tick_replies(inboxes["wid"])["marked"]) == 2
    assert workers.tick_replies(inboxes["wid"])["marked"] == []
    with inboxes["factory"]() as session:
        receipts = list(session.scalars(select(GtmEmailEvent).where(GtmEmailEvent.event_type == "booking_reply_headers")))
        assert {row.email_account_id for row in receipts} == {case["mailbox_id"] for case in inboxes["cases"]}
        assert all(row.provider_message_id == "shared-message" and "text/calendar" in row.metadata_json for row in receipts)


def test_projections_enumerate_authorized_mailboxes_when_legacy_selection_is_empty(inboxes, monkeypatch):
    from app.chat import next_moves
    from app.db.models import GtmEmailAccount
    from app.gtm.relationships import evidence, service
    from app.gtm.supymem import pipeline
    from app.ventures import state_brief

    monkeypatch.setattr(pipeline, "_active_mailbox", lambda *args: None)
    expected = {case["mailbox_id"] for case in inboxes["cases"]}
    with inboxes["factory"]() as session:
        gmail = state_brief._connections_section(session, GtmEmailAccount, inboxes["wid"])["gmail"]
    assert gmail["connected"] and {row["mailbox_id"] for row in gmail["mailboxes"]} == expected
    assert "email" not in json.dumps(gmail)
    for rows in (evidence.list_evidence_sources(inboxes["wid"]), service.onboarding_status(inboxes["wid"])["evidence_sources"]):
        gmail = next(row for row in rows if row["provider"] == "gmail")
        assert gmail["status"] == "connected" and {row["mailbox_id"] for row in gmail["mailboxes"]} == expected
    assert next_moves._journey_snapshot(inboxes["wid"], inboxes["cases"][0]["venture_id"])["gmail"]["connected"]


@pytest.mark.parametrize("reader", ["replies", "bounces"])
@pytest.mark.parametrize("failure", ["empty_message", "repeated_page"])
def test_invalid_history_never_advances_a_mailbox_checkpoint(inboxes, monkeypatch, reader, failure):
    from app.db.models import GtmEmailEvent
    from app.gtm.supymem import composio_gmail, workers
    from core.clock import utcnow

    event_type = "gmail_sync_checkpoint" if reader == "replies" else "gmail_bounce_checkpoint"
    with inboxes["factory"]() as session:
        for case in inboxes["cases"]:
            session.add(GtmEmailEvent(id="start_" + case["mailbox_id"], workspace_id=inboxes["wid"],
                email_account_id=case["mailbox_id"], event_type=event_type, metadata_json=json.dumps({"history_id": "50"}), created_at=utcnow()))
        session.commit()

    def history(**kwargs):
        return {"historyId": "60", "history": [{"messagesAdded": [{"message": {"id": "missing"}}]}],
            **({"nextPageToken": "same-page"} if failure == "repeated_page" else {})}

    monkeypatch.setattr(composio_gmail, "fetch_history", history)
    monkeypatch.setattr(composio_gmail, "fetch_message", lambda **kwargs: {})
    result = getattr(workers, "tick_" + reader)(inboxes["wid"])
    assert result["marked"] == [] and all("error" in row for row in result["mailboxes"])
    with inboxes["factory"]() as session:
        checkpoints = list(session.scalars(select(GtmEmailEvent).where(GtmEmailEvent.event_type == event_type)))
        assert len(checkpoints) == 2 and all(json.loads(row.metadata_json)["history_id"] == "50" for row in checkpoints)


@pytest.mark.parametrize("reader", ["replies", "bounces"])
def test_expired_history_restarts_only_that_mailbox(inboxes, monkeypatch, reader):
    from app.db.models import GtmEmailEvent
    from app.gtm.supymem import composio_gmail, workers
    from core.clock import utcnow

    event_type = "gmail_sync_checkpoint" if reader == "replies" else "gmail_bounce_checkpoint"
    expired, good = inboxes["cases"]
    with inboxes["factory"]() as session:
        for case in inboxes["cases"]:
            session.add(GtmEmailEvent(id="old_cursor_" + case["mailbox_id"], workspace_id=inboxes["wid"],
                email_account_id=case["mailbox_id"], event_type=event_type, metadata_json=json.dumps({"history_id": "old"}), created_at=utcnow()))
        session.commit()

    def history(**kwargs):
        if kwargs["connected_account_id"] == expired["ca"]:
            raise composio_gmail.DefiniteFailure("history expired", status_code=404)
        return {"historyId": "good-next", "history": []}

    _reads(inboxes, monkeypatch)
    monkeypatch.setattr(composio_gmail, "fetch_history", history)
    monkeypatch.setattr(composio_gmail, "get_gmail_profile", lambda uid, ca: {"raw": {"historyId": "restarted"}})
    monkeypatch.setattr(composio_gmail, "fetch_query", lambda **kwargs: [_dsn(expired["email"])])
    assert getattr(workers, "tick_" + reader)(inboxes["wid"])["marked"] == [expired["contact_id"]]
    with inboxes["factory"]() as session:
        checkpoints = list(session.scalars(select(GtmEmailEvent).where(GtmEmailEvent.event_type == event_type)))
        assert {json.loads(row.metadata_json)["history_id"] for row in checkpoints if row.email_account_id == expired["mailbox_id"]} == {"old", "restarted"}
        assert {json.loads(row.metadata_json)["history_id"] for row in checkpoints if row.email_account_id == good["mailbox_id"]} == {"old", "good-next"}


def test_bounce_bootstrap_paginates_with_the_same_mailbox_binding(inboxes, monkeypatch):
    from app.gtm.supymem import composio_gmail, workers

    calls = []
    cases = {case["ca"]: case for case in inboxes["cases"]}
    monkeypatch.setattr(composio_gmail, "get_gmail_profile", lambda *args: {"raw": {"historyId": "90"}})

    def next_page(slug, **kwargs):
        case = cases[kwargs["connected_account_id"]]
        assert slug == "GMAIL_FETCH_EMAILS" and kwargs["user_id"] == case["uid"]
        assert kwargs["arguments"]["max_results"] == workers._fetch_limit()
        if not kwargs["arguments"].get("page_token"):
            return {"data": {"messages": [], "nextPageToken": "next"}}
        assert kwargs["arguments"]["page_token"] == "next"
        calls.append(case["mailbox_id"])
        return {"data": {"messages": [_dsn(case["email"])]}}

    monkeypatch.setattr(composio_gmail, "execute_tool", next_page)
    assert workers.tick_bounces(inboxes["wid"])["marked"] == []
    assert set(workers.tick_bounces(inboxes["wid"])["marked"]) == {case["contact_id"] for case in inboxes["cases"]}
    assert set(calls) == {case["mailbox_id"] for case in inboxes["cases"]}


def test_known_historical_sender_gets_insert_only_thread_attribution(inboxes, monkeypatch):
    from app.db.models import GtmEmailEvent, GtmTouch
    from app.gtm.supymem import workers
    from core.clock import utcnow

    _reads(inboxes, monkeypatch)
    with inboxes["factory"]() as session:
        for case in inboxes["cases"]:
            touch_id = "history_" + case["contact_id"]
            session.add(GtmTouch(id=touch_id, workspace_id=inboxes["wid"], venture_id=case["venture_id"],
                contact_id=case["contact_id"], direction="out", channel="email", simulated=0, sent_at=utcnow(),
                provider_thread_id="shared-thread", message_id="historical-send", created_at=utcnow()))
            session.add(GtmEmailEvent(id="history_event_" + case["contact_id"], workspace_id=inboxes["wid"],
                email_account_id=case["mailbox_id"], contact_id=case["contact_id"], event_type="SENT",
                provider_message_id="historical-send", metadata_json=json.dumps({"thread_id": "shared-thread"}), created_at=utcnow()))
        session.commit()
    assert len(workers.tick_replies(inboxes["wid"])["marked"]) == 2
    with inboxes["factory"]() as session:
        for case in inboxes["cases"]:
            touch_id = "history_" + case["contact_id"]
            receipt = session.scalar(select(GtmEmailEvent).where(GtmEmailEvent.idempotency_key == "conversation-mailbox:" + touch_id))
            assert receipt.email_account_id == case["mailbox_id"] and json.loads(receipt.metadata_json)["thread_id"] == "shared-thread"
            assert session.get(GtmTouch, touch_id).message_id == "historical-send"


def test_shared_contact_with_colliding_threads_uses_exact_rfc_references(inboxes, monkeypatch):
    from app.db.models import GtmEmailAccount, GtmEmailEvent, GtmTouch
    from app.gtm.supymem import composio_gmail, workers
    from core.clock import utcnow

    contact_id = inboxes["cases"][0]["contact_id"]
    by_ca = {}
    with inboxes["factory"]() as session:
        for index, case in enumerate(inboxes["cases"]):
            session.get(GtmEmailAccount, case["mailbox_id"]).status = "needs_reconnect"
            account_id = "colliding_" + case["mailbox_id"]
            ca = "colliding_" + case["ca"]
            rfc_id = "<original@" + case["email"].split("@", 1)[1] + ">"
            session.add(GtmEmailAccount(id=account_id, workspace_id=inboxes["wid"], email="you@" + case["email"].split("@", 1)[1],
                composio_user_id=case["uid"], connected_account_id=ca, status="active", created_at=utcnow(), updated_at=utcnow()))
            session.flush()
            touch_id = "colliding_send_" + case["mailbox_id"]
            session.add(GtmTouch(id=touch_id, workspace_id=inboxes["wid"], venture_id=case["venture_id"], contact_id=contact_id,
                direction="out", channel="email", simulated=0, sent_at=utcnow(), provider_thread_id="colliding-thread",
                message_id=rfc_id, created_at=utcnow()))
            session.add(GtmEmailEvent(id="colliding_metadata_" + case["mailbox_id"], workspace_id=inboxes["wid"],
                contact_id=contact_id, email_account_id=account_id, event_type="sent_metadata",
                idempotency_key="sent-metadata:" + touch_id, metadata_json=json.dumps({"thread_id": "colliding-thread", "rfc_message_id": rfc_id}), created_at=utcnow()))
            by_ca[ca] = {"account_id": account_id, "rfc_id": rfc_id, "email": case["email"], "venture_id": case["venture_id"]}
        session.commit()
    monkeypatch.setattr(composio_gmail, "get_gmail_profile", lambda *args: {"raw": {}})

    def fetch(**kwargs):
        case = by_ca[kwargs["connected_account_id"]]
        return {"messages": [{"id": "same-reply", "threadId": "colliding-thread", "body_text": "Yes, interested",
            "headers": {"from": case["email"], "in-reply-to": case["rfc_id"]}}]}

    monkeypatch.setattr(composio_gmail, "fetch_thread", fetch)
    result = workers.tick_replies(inboxes["wid"])
    assert len(result["replies"]) == 2 and {reply["venture_id"] for reply in result["replies"]} == {case["venture_id"] for case in by_ca.values()}
    assert workers.tick_replies(inboxes["wid"])["marked"] == []


def test_corrupt_attribution_fails_closed_for_its_contact_not_other_inboxes(inboxes, monkeypatch):
    from app.db.models import GtmEmailEvent
    from app.gtm.supymem import workers
    from core.clock import utcnow

    _reads(inboxes, monkeypatch)
    bad, good = inboxes["cases"]
    with inboxes["factory"]() as session:
        session.add(GtmEmailEvent(id="corrupt_event", workspace_id=inboxes["wid"], email_account_id=bad["mailbox_id"],
            contact_id=bad["contact_id"], event_type="sent_metadata", metadata_json="not-json", created_at=utcnow()))
        session.commit()
    assert workers.tick_replies(inboxes["wid"])["marked"] == [good["contact_id"]]


def test_legacy_rfc_metadata_is_preserved_in_an_insert_only_mailbox_sidecar(inboxes, monkeypatch):
    from app.db.models import GtmEmailEvent, GtmTouch
    from app.gtm.supymem import composio_gmail, workers
    from core.clock import utcnow

    by_ca = {}
    with inboxes["factory"]() as session:
        for case in inboxes["cases"]:
            touch_id = "old_send_" + case["contact_id"]
            rfc_id = "<historical@" + case["email"].split("@", 1)[1] + ">"
            session.add(GtmTouch(id=touch_id, workspace_id=inboxes["wid"], venture_id=case["venture_id"],
                contact_id=case["contact_id"], direction="out", channel="email", simulated=0, sent_at=utcnow(),
                provider_thread_id="old-thread", message_id="old-provider-message", created_at=utcnow()))
            session.add(GtmEmailEvent(id="old_send_event_" + case["contact_id"], workspace_id=inboxes["wid"],
                contact_id=case["contact_id"], email_account_id=case["mailbox_id"], event_type="SENT", provider_message_id="old-provider-message",
                metadata_json=json.dumps({"thread_id": "old-thread"}), created_at=utcnow()))
            session.add(GtmEmailEvent(id="old_rfc_" + case["contact_id"], workspace_id=inboxes["wid"], contact_id=case["contact_id"],
                event_type="sent_metadata", idempotency_key="sent-metadata:" + touch_id,
                metadata_json=json.dumps({"rfc_message_id": rfc_id}), created_at=utcnow()))
            by_ca[case["ca"]] = {**case, "rfc_id": rfc_id, "touch_id": touch_id}
        session.commit()
    monkeypatch.setattr(composio_gmail, "get_gmail_profile", lambda *args: {"raw": {}})

    def fetch(**kwargs):
        case = by_ca[kwargs["connected_account_id"]]
        return {"messages": [{"id": "forwarded-reply", "threadId": "forwarded-thread", "body_text": "Yes, interested",
            "headers": {"from": case["email"], "in-reply-to": case["rfc_id"]}}]}

    monkeypatch.setattr(composio_gmail, "fetch_thread", fetch)
    assert len(workers.tick_replies(inboxes["wid"])["marked"]) == 2
    with inboxes["factory"]() as session:
        for case in by_ca.values():
            original = session.get(GtmEmailEvent, "old_rfc_" + case["contact_id"])
            assert original.email_account_id is None and json.loads(original.metadata_json) == {"rfc_message_id": case["rfc_id"]}
            sidecar = session.scalar(select(GtmEmailEvent).where(GtmEmailEvent.idempotency_key == "conversation-mailbox:" + case["touch_id"]))
            assert sidecar.email_account_id == case["mailbox_id"] and json.loads(sidecar.metadata_json)["rfc_message_id"] == case["rfc_id"]


def test_explicit_touch_attribution_wins_over_colliding_provider_ids(inboxes):
    from app.gtm.supymem import workers

    touches = [SimpleNamespace(id="explicit_" + case["mailbox_id"], channel="email", contact_id=inboxes["cases"][0]["contact_id"],
        email_account_id=case["mailbox_id"], provider_thread_id="shared-thread", message_id="provider-sent-0", in_reply_to=None)
        for case in inboxes["cases"]]
    with inboxes["factory"]() as session:
        for touch in touches:
            assert workers._touches_for_mailbox(session, inboxes["wid"], touch.email_account_id, touches) == [touch]


def test_discovered_rfc_headers_are_persisted_without_updating_thread_sidecars(inboxes, monkeypatch):
    from app.db.models import GtmEmailEvent, GtmTouch
    from app.gtm.supymem import composio_gmail, workers
    from core.clock import utcnow

    by_ca = {}
    with inboxes["factory"]() as session:
        for case in inboxes["cases"]:
            touch_id = "late_rfc_" + case["contact_id"]
            rfc_id = "<late@" + case["email"].split("@", 1)[1] + ">"
            session.add(GtmTouch(id=touch_id, workspace_id=inboxes["wid"], venture_id=case["venture_id"],
                contact_id=case["contact_id"], direction="out", channel="email", simulated=0, sent_at=utcnow(),
                provider_thread_id="late-thread", message_id="late-provider-id", created_at=utcnow()))
            session.add(GtmEmailEvent(id="late_send_" + case["contact_id"], workspace_id=inboxes["wid"],
                contact_id=case["contact_id"], email_account_id=case["mailbox_id"], event_type="SENT", provider_message_id="late-provider-id",
                metadata_json=json.dumps({"thread_id": "late-thread"}), created_at=utcnow()))
            by_ca[case["ca"]] = {**case, "rfc_id": rfc_id, "touch_id": touch_id}
        session.commit()
    monkeypatch.setattr(composio_gmail, "get_gmail_profile", lambda *args: {"raw": {}})

    def fetch(**kwargs):
        case = by_ca[kwargs["connected_account_id"]]
        return {"messages": [{"id": "late-provider-id", "labelIds": ["SENT"], "headers": {"message-id": case["rfc_id"]}},
            {"id": "late-reply", "threadId": "forwarded-thread", "body_text": "Yes, interested",
                "headers": {"from": case["email"], "in-reply-to": case["rfc_id"]}}]}

    monkeypatch.setattr(composio_gmail, "fetch_thread", fetch)
    assert len(workers.tick_replies(inboxes["wid"])["marked"]) == 2
    with inboxes["factory"]() as session:
        for case in by_ca.values():
            thread = session.scalar(select(GtmEmailEvent).where(GtmEmailEvent.idempotency_key == "conversation-mailbox:" + case["touch_id"]))
            rfc = session.scalar(select(GtmEmailEvent).where(GtmEmailEvent.idempotency_key == "sent-metadata:" + case["touch_id"]))
            assert json.loads(thread.metadata_json)["rfc_message_id"] is None
            assert json.loads(rfc.metadata_json)["rfc_message_id"] == case["rfc_id"]
            assert rfc.email_account_id == case["mailbox_id"]

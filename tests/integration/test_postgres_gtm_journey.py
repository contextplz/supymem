"""Migrated PostgreSQL: both GTM entries, real worker, exact approvals, credits."""
import asyncio
import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from tests.integration.test_postgres_auth_security import pg_database as pg_database
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db


GTM_IDEA = {"problem": "Bakery phone orders get lost in manual notes",
    "audience": "Independent bakery owners handling phone orders"}


@pytest.mark.parametrize("body,headers,nested,automated", [
    ("Acknowledged. Unsubscribe from these updates.", {"Auto-Submitted": "auto-replied"}, False, True),
    ("Not interested; remove me", [{"name": "Auto-Submitted", "value": "auto-replied"}], True, True),
    ("Not interested; stop emailing", {"Auto-Submitted": "no"}, False, False),
    ("Remove me; I am on vacation.", None, False, False),
], ids=["auto-unsubscribe-footer", "auto-negative-payload-header", "human-stop", "human-stop-vacation"])
def test_saved_reply_automation_and_opt_out_have_distinct_metrics_and_lifecycle_sources(journey_db, monkeypatch, body, headers, nested, automated):
    from app.autopilot import outreach, pmf
    from app.chat import milestones
    from app.db.models import AnalyticsEvent, GtmContact, GtmEmailAccount, GtmLearningEvent, GtmSuppression, GtmTouch, Venture
    from app.email import triggers
    from app.gtm import journey
    from app.gtm.supymem import composio_gmail, pipeline
    from app.tenancy import bind
    from core.clock import utcnow
    d, now = journey_db, utcnow()
    cid, thread, message_id = "contact_" + d["vid"], "thread_" + d["vid"], "reply_" + d["vid"]
    with d["factory"]() as session:
        session.add(GtmEmailAccount(id="mail_" + d["vid"], workspace_id=d["wid"], app_user_id=d["actor"],
            email="founder@example.test", composio_user_id="fixture_user", connected_account_id="ca_" + d["vid"],
            status="active", created_at=now, updated_at=now))
        session.add(GtmContact(id=cid, workspace_id=d["wid"], email="owner@example.test", times_emailed=1,
            email_confidence="safe", source=f"venture:{d['vid']}:manual", recipient_geo="CA", lawful_basis="consent",
            enrichment_json=json.dumps({"found_at": "https://bakery.example/contact"}), stage_changed_at=now, created_at=now))
        session.flush()
        session.add(GtmTouch(id="sent_" + d["vid"], workspace_id=d["wid"], venture_id=d["vid"], contact_id=cid,
            direction="out", channel="email", simulated=0, send_mode="founder_gmail", message_id="sent-message_" + d["vid"],
            provider_thread_id=thread, sent_at=now - timedelta(days=2), created_at=now - timedelta(days=2)))
        session.commit()
    message = {"id": message_id, "threadId": thread, "snippet": body}
    message.update({"payload": {"headers": headers}} if nested else {"headers": headers})
    monkeypatch.setattr(composio_gmail, "fetch_thread", lambda **kw: {"messages": [message]})
    monkeypatch.setattr(pipeline, "utcnow", lambda: now - timedelta(hours=2))
    notes = []
    monkeypatch.setattr(milestones, "post_milestone", lambda *a, **kw: notes.append(kw))
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        assert asyncio.run(outreach.poll_replies(d["vid"], force=True)) == 1
        assert asyncio.run(outreach.poll_replies(d["vid"], force=True)) == 0
        result = journey.status(d["wid"], d["vid"])["results"]
        assert result["replied"] == result["negative"] == int(not automated) and result["interested"] == 0
        assert len(result["replies"]) == 1
        assert result["replies"][0]["classification"] == ("automated" if automated else "human_negative")
        assert pmf.read_metric(d["vid"], "outbound_replies") == int(not automated)
        assert pmf.read_metric(d["vid"], "outbound_touches") == 1
        assert outreach.schedule_followups(d["vid"]) == 0
    assert len(notes) == 1 and notes[0]["kind"] == "outreach_reply"
    with d["factory"]() as session:
        contact, venture = session.get(GtmContact, cid), session.get(Venture, d["vid"])
        assert contact.do_not_contact == 1 and contact.unsubscribed_at is not None
        assert contact.replied_at is not None and contact.relationship_stage == "cooling"
        assert session.scalar(select(GtmSuppression.id).where(GtmSuppression.workspace_id == d["wid"], GtmSuppression.email == contact.email))
        touches = list(session.scalars(select(GtmTouch).where(GtmTouch.venture_id == d["vid"], GtmTouch.direction == "in")))
        assert len(touches) == 1 and touches[0].message_id == message_id
        assert triggers._real_touch(touches[0], venture, "in") is (not automated)
        for _ in range(2):
            eligible = []
            triggers._outreach(session, venture, d["actor"], triggers._naive(now), lambda owner, template, *a, **kw: eligible.append(template))
            assert ("replies_received" in eligible) is (not automated)
        activity = triggers._activity(session, d["actor"], now - timedelta(days=7), now)
        assert any("1 replies received" in item for item in activity) is (not automated)
        kinds = list(session.scalars(select(GtmLearningEvent.kind).where(GtmLearningEvent.contact_id == cid)))
        assert kinds == ["reply_automated" if automated else "reply_human"]
        events = list(session.scalars(select(AnalyticsEvent.id).where(AnalyticsEvent.venture_id == d["vid"], AnalyticsEvent.event_name == "outreach.reply_received")))
        assert len(events) == int(not automated)


@pytest.mark.parametrize("has_site", [False, True], ids=["skip-to-gtm", "published-product"])
@pytest.mark.parametrize("journey_db", [GTM_IDEA], indirect=True, ids=["confirmed-problem"])
def test_gtm_journey_migrated_worker_review_metered_send_reply(journey_db, monkeypatch, has_site):
    from app.autopilot import outreach
    from app.autopilot.service import update_settings
    from app.core.jobs import lease
    from app.credits.accounts import ensure_account
    from app.credits.policy import set_policy
    from app.db.models import CreditAccount, CreditLedgerEntry, GtmEmailAccount, GtmTouch, Job, Person, VentureDocument
    from app.db.supymem_models import EmailMessage
    from app.email import providers as email_providers, service as email_service, triggers as email_triggers
    from app.gtm import journey
    from app.gtm.supymem import composio_gmail
    from app.tenancy import bind
    from app.worker.handlers import install_default_handlers
    from app.worker.main import _run_one
    from core.clock import utcnow
    d = journey_db
    monkeypatch.setenv("GTM_ENRICH_ENABLED", "0")
    monkeypatch.setenv("SUPYMEM_OUTBOUND_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("CHANNELS_DRY_RUN", "false")
    monkeypatch.setenv("EMAIL_PROVIDER", "log")
    monkeypatch.setenv("EMAIL_PHYSICAL_ADDRESS", "Synthetic fixture business address")
    monkeypatch.setenv("EMAIL_TOKEN_SECRET", "synthetic-gtm-email-token-key-more-than-32")
    monkeypatch.setenv("SUPYMEM_EMAIL_TEMPLATE_ALLOWLIST", "outreach_drafts_ready,outreach_sent")
    monkeypatch.delenv("SUPYMEM_EMAIL_LIFECYCLE_ENABLED", raising=False)
    lifecycle_calls = []
    monkeypatch.setitem(email_providers.PROVIDERS, "log", lambda outgoing: lifecycle_calls.append(outgoing) or f"lifecycle-{len(lifecycle_calls)}")
    monkeypatch.setattr(outreach, "site_url", lambda vid: "https://order-board.example/product" if has_site else None)
    discoveries = []
    from tests.gtm_fixtures import qualified_bakery, quality_model
    monkeypatch.setattr(journey, "_discovery_model", quality_model)
    async def discover(who, search_queries=None, **context):
        discoveries.append((who, search_queries, context))
        return [await qualified_bakery({"email": "owner@bakery.example", "email_confidence": "published", "enriched": True, "organization": "Test Bakery",
            "website": "https://bakery.example/", "source_url": "https://forum.example/bakery-orders",
            "contact_source_url": "https://bakery.example/contact", "contact_quote": "Contact Test Bakery at owner@bakery.example",
            "signals": [{"kind": "customer_voice", "quote": "We bakery owners lose phone orders in manual notes.",
                "source_url": "https://forum.example/bakery-orders", "published_at": utcnow().isoformat(),
                "observed_at": utcnow().isoformat(), "confidence": "observed", "company_evidence_url": "https://forum.example/bakery-orders",
                "why_now": "The owner reports losing phone orders."}]}, who, **context)]
    monkeypatch.setattr(journey, "_discover", discover)
    account = ensure_account(d["vid"], workspace_id=d["wid"], owner_person_id=d["actor"])
    with d["factory"]() as session:
        person = session.get(Person, d["actor"])
        person.email, person.created_at = "founder-fixture@example.test", utcnow() - timedelta(days=30)
        session.get(CreditAccount, account.id).granted_micro = 1_000_000
        session.add(GtmEmailAccount(id="gea_" + d["vid"], workspace_id=d["wid"], app_user_id=d["actor"], email="founder@gmail.example", composio_user_id="fixture_composio", connected_account_id="ca_" + d["vid"], status="active", created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    set_policy("outbound_provider_rates", {"version": "offline-gmail-v1", "rates": {"gtm_email_send:composio_gmail": {
        "action": "gtm_email_send", "provider": "composio_gmail", "price_usd": "0.01", "currency": "USD", "source": "Integration fixture"}}}, "test")
    install_default_handlers()
    def deliver_scheduled_email():
        with d["factory"]() as session:
            jobs = lease(session, "gtm-email-pg", kinds=["email.send"], n=100)
            session.commit()
        for job in jobs:
            asyncio.run(_run_one(d["factory"], job, {job.id}))
        with d["factory"]() as session:
            assert all(session.get(Job, job.id).status == "succeeded" for job in jobs)
        return len(jobs)
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        assert not journey.status(d["wid"], d["vid"])["discovery_started"]
        assert not journey.prepare(d["wid"], d["vid"], authorize=False)["discovery_started"]
        with d["factory"]() as session:
            assert not session.scalar(select(VentureDocument.id).where(VentureDocument.venture_id == d["vid"], VentureDocument.kind == journey.KIND))
        view = journey.update_icp(d["wid"], d["vid"], "Independent bakery owners handling phone orders")
        assert view["state"] == "queued" and view["discovery_started"]
        assert journey.prepare(d["wid"], d["vid"])["job_id"] == view["job_id"]
        with d["factory"]() as session:
            jobs = lease(session, "gtm-journey-pg", kinds=["gtm.journey.prepare"], n=1)
            session.commit()
        assert jobs and jobs[0].id == view["job_id"]
        asyncio.run(_run_one(d["factory"], jobs[0], {jobs[0].id}))
        with d["factory"]() as session:
            recorded_job = session.get(Job, view["job_id"])
            assert recorded_job.status == "succeeded", recorded_job.last_error
        state = journey.status(d["wid"], d["vid"])
        assert len(discoveries) == 1 and discoveries[0][2]["problem"] == view["icp"]["problem"]
        assert discoveries[0][2]["payload"]["_job_id"] == view["job_id"]
        assert len(state["prospects"]) == 1 and not state["drafts"]
        assert state["prospects"][0]["email_status"] == "published" and state["prospects"][0]["signals"]
        assert state["prospects"][0]["blocked_reason"] == "unknown_jurisdiction"
        state = journey.patch_prospect(d["wid"], d["vid"], state["prospects"][0]["id"], {
            "country": "CA", "lawful_basis": "consent", "lawful_basis_note": "Express permission captured by the founder for this product."})
        assert not state["drafts"]
        drafted = journey.draft_for_review(d["vid"], contact_id=state["prospects"][0]["id"])
        assert drafted["kind"] == "text" and all(not action.get("confirmation") for action in drafted["actions"])
        state = journey.status(d["wid"], d["vid"])
        assert len(state["drafts"]) == 1 and ("order-board.example" in state["drafts"][0]["body"]) == has_site
        # The canonical hourly producer owns notifications. Reconciliation of
        # the same saved batch is durable and cannot send a second email.
        review_time = utcnow() + timedelta(hours=2)
        monkeypatch.setattr(email_service, "utcnow", lambda: review_time)
        monkeypatch.setattr(email_triggers, "utcnow", lambda: review_time)
        email_triggers.run_schedule(review_time)
        assert deliver_scheduled_email() == 1
        email_triggers.run_schedule(review_time)
        assert deliver_scheduled_email() == 0
        with d["factory"]() as session:
            notices = list(session.scalars(select(EmailMessage).where(EmailMessage.person_id == d["actor"], EmailMessage.template == "outreach_drafts_ready")))
            assert len(notices) == 1 and notices[0].status == "sent"
            assert json.loads(notices[0].context_json)["path"] == f"/gtm?venture={d['vid']}"
        assert len(lifecycle_calls) == 1
        update_settings(d["vid"], allow_send=True, send_max_per_day=5)
        draft = state["drafts"][0]
        state = journey.patch_draft(d["wid"], d["vid"], draft["doc_id"], {"decision": "approved", "expected_revision": draft["revision"]})
        batch = state["batches"][0]
        request = {k: batch[k] for k in ("batch_id", "draft_ids", "approval_digest", "cap")}
        calls = []
        monkeypatch.setattr(outreach, "_send_one", lambda *args: calls.append(args) or {"provider_message_id": "fake-receipt-1", "thread_id": "fake-thread-1"})
        sent = asyncio.run(journey.send(d["wid"], d["vid"], request))
        assert sent["results"]["sent"] == 1 and len(calls) == 1
        with pytest.raises(ValueError, match="gtm_approval_changed"):
            asyncio.run(journey.send(d["wid"], d["vid"], request))
        assert len(calls) == 1 and len(lifecycle_calls) == 1
        receipt_time = review_time + timedelta(hours=1)
        monkeypatch.setattr(email_service, "utcnow", lambda: receipt_time)
        monkeypatch.setattr(email_triggers, "utcnow", lambda: receipt_time)
        email_triggers.run_schedule(receipt_time)
        assert deliver_scheduled_email() == 1
        email_triggers.run_schedule(receipt_time)
        assert deliver_scheduled_email() == 0
        with d["factory"]() as session:
            receipts = list(session.scalars(select(EmailMessage).where(EmailMessage.person_id == d["actor"], EmailMessage.template == "outreach_sent")))
            assert len(receipts) == 1 and receipts[0].status == "sent"
            context = json.loads(receipts[0].context_json)
            touch = session.scalar(select(GtmTouch).where(GtmTouch.venture_id == d["vid"], GtmTouch.direction == "out"))
            assert context["count"] == 1 and context["source_ids"] == [f"touch:{touch.id}"]
        assert len(lifecycle_calls) == 2
        monkeypatch.setattr(composio_gmail, "fetch_thread", lambda **kwargs: {"messages": [{"id": "fake-reply-1", "threadId": "fake-thread-1", "snippet": "Yes, interested in a demo"}]})
        assert asyncio.run(outreach.poll_replies(d["vid"], force=True)) == 1
        state = journey.status(d["wid"], d["vid"])
        assert state["results"]["replied"] == state["results"]["interested"] == 1
    with d["factory"]() as session:
        assert session.get(Job, view["job_id"]).status == "succeeded"
        assert session.get(CreditAccount, account.id).spent_micro == 10_000
        assert session.get(CreditAccount, account.id).reserved_micro == 0
        charges = session.scalars(select(CreditLedgerEntry).where(
            CreditLedgerEntry.account_id == account.id, CreditLedgerEntry.kind == "spend",
            CreditLedgerEntry.purpose == "outbound:gtm_email_send")).all()
        assert len(charges) == 1 and charges[0].amount_micro == -10_000 and charges[0].provider == "composio_gmail"
        assert len(list(session.scalars(select(GtmTouch).where(GtmTouch.workspace_id == d["wid"], GtmTouch.venture_id == d["vid"])))) == 2


@pytest.mark.parametrize("journey_db", [GTM_IDEA], indirect=True, ids=["confirmed-problem"])
def test_periodic_maintain_tick_leases_dispatches_and_prepares_only_eligible_scopes(journey_db, monkeypatch):
    from app.tasks.scheduler import dispatch_tick
    from app.core.jobs import enqueue, lease
    from app.db.models import IdeaBrief, Job, Venture, VentureDocument, Workspace
    from app.db.supymem_models import Deployment, FounderApp
    from app.gtm import journey
    from app.gtm.supymem import composio_gmail
    from app.tenancy import bind
    from app.worker.catalog import enabled_kinds, tick_kinds
    from app.worker.handlers import install_default_handlers
    from app.worker.main import _run_one
    from app.workspaces.service import create_workspace
    from core.clock import utcnow
    from core.ids import new_id

    d, now = journey_db, utcnow()
    prepared, polled = [], []
    real_prepare = journey.prepare
    def prepare(wid, vid, **kwargs):
        prepared.append((wid, vid, kwargs))
        return real_prepare(wid, vid, **kwargs)
    async def poll(vid):
        polled.append(vid)
    monkeypatch.setattr(journey, "prepare", prepare)
    monkeypatch.setattr(journey.outreach, "poll_replies", poll)
    monkeypatch.setattr(journey.outreach, "schedule_followups", lambda vid: None)
    monkeypatch.setenv("WORKER_KINDS", "gtm.journey.*")
    monkeypatch.setenv("SCHEDULED_FANOUT_SPREAD_SECONDS", "0")
    monkeypatch.setattr(composio_gmail, "fetch_inbound_from", lambda **kwargs: {"messages": []})
    monkeypatch.setattr(journey.outreach, "_send_one", lambda *args: pytest.fail("The maintainer may prepare, never send"))
    assert {"gtm.journey.prepare", "gtm.journey.maintain"} <= enabled_kinds()
    assert "gtm.journey.maintain" in tick_kinds("5m")
    published_ws = create_workspace(kind="company", name="Published fixture", owner_id=d["actor"])
    unrelated_ws = create_workspace(kind="company", name="Unrelated fixture", owner_id=d["actor"])
    archived_ws = create_workspace(kind="company", name="Archived fixture", owner_id=d["actor"])
    published, unrelated, archived_venture, archived_workspace_venture = [new_id("ven") for _ in range(4)]
    with d["factory"]() as session:
        session.get(Workspace, d["wid"]).settings_json = json.dumps({"entry_goal": "gtm"})
        archived_workspace = session.get(Workspace, archived_ws.id)
        archived_workspace.settings_json, archived_workspace.archived_at = json.dumps({"entry_goal": "gtm"}), now
        for vid, wid, archived in ((published, published_ws.id, None), (unrelated, unrelated_ws.id, None),
                                   (archived_venture, d["wid"], now), (archived_workspace_venture, archived_ws.id, None)):
            session.add(Venture(id=vid, workspace_id=wid, name="Owned maintenance fixture", raw_idea="Bakery orders",
                stage="draft", archived_at=archived, created_at=now, updated_at=now))
        session.flush()
        for vid in (d["vid"], published, unrelated, archived_venture, archived_workspace_venture):
            session.add(IdeaBrief(id=new_id("ibr"), venture_id=vid, domain="commerce", business_model="saas",
                build_archetype="web", audience="Independent bakery owners handling phone orders",
                regulatory_surface="none", novelty="fixture", complexity_score=1, uncertainty_score=1,
                build_feasibility=1, complexity_factors_json="{}", research_notes_json="{}",
                classifier_version="fixture", created_at=now))
        app_id = new_id("app")
        session.add(FounderApp(id=app_id, workspace_id=published_ws.id, venture_id=published, slug=app_id,
            name="Owned static page", kind="website", state="live", created_at=now, updated_at=now))
        session.flush()
        session.add(Deployment(id=new_id("dep"), app_id=app_id, workspace_id=published_ws.id, venture_id=published,
            build_artifact_id="owned-static-artifact", target="supymem_static", state="live", version=1,
            url="https://owned-product.example", started_at=now, live_at=now))
        scoped = enqueue(session, "gtm.journey.maintain", {"_actor_subject": d["actor"]},
            workspace_id=d["wid"], venture_id=d["vid"], dedupe_key="scoped-maintain:" + d["vid"])
        session.commit()
    install_default_handlers()
    def work_maintain():
        claimed = []
        while True:
            with d["factory"]() as session:
                jobs = lease(session, "owned-maintain-worker", kinds=["gtm.journey.maintain"], n=100)
                session.commit()
            if not jobs:
                break
            for job in jobs:
                asyncio.run(_run_one(d["factory"], job, {(job.id, job.attempts)}))
            claimed.extend(jobs)
        assert claimed
        with d["factory"]() as session:
            assert all(session.get(Job, job.id).status == "succeeded" for job in claimed)
        return claimed
    assert [job.id for job in work_maintain()] == [scoped]
    assert prepared == [(d["wid"], d["vid"], {"authorize": False})] and not polled
    with d["factory"]() as session:
        assert not session.scalar(select(VentureDocument.id).where(VentureDocument.venture_id == d["vid"], VentureDocument.kind == journey.KIND))
        assert not session.scalar(select(VentureDocument.id).where(VentureDocument.venture_id == published, VentureDocument.kind == journey.KIND))
        assert not session.scalar(select(Job.id).where(Job.kind == "gtm.journey.prepare", Job.workspace_id.in_([d["wid"], published_ws.id])))
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        started = journey.update_icp(d["wid"], d["vid"], "Independent bakery owners handling phone orders")
    assert started["state"] == "queued"
    prepared.clear()
    tick = dispatch_tick("5m")
    assert tick["enqueued"] == ["gtm.journey.maintain"]
    assert dispatch_tick("5m")["ids"] == tick["ids"]
    work_maintain()
    fixture_ventures = {d["vid"], published, unrelated, archived_venture, archived_workspace_venture}
    assert {(wid, vid) for wid, vid, _ in prepared if vid in fixture_ventures} == {(d["wid"], d["vid"]), (published_ws.id, published)}
    assert all(kwargs == {"authorize": False} for _, _, kwargs in prepared)
    assert [vid for vid in polled if vid in fixture_ventures] == [d["vid"]]
    with d["factory"]() as session:
        row = session.scalar(select(VentureDocument).where(VentureDocument.venture_id == d["vid"], VentureDocument.kind == journey.KIND))
        data = json.loads(row.provenance_json)
        job = session.get(Job, data["job_id"])
        assert data["state"] == "queued" and job.id == started["job_id"] and job.kind == "gtm.journey.prepare" and job.status == "scheduled"
        assert json.loads(job.payload_json)["_actor_subject"] == d["actor"]
        assert job.venture_id == d["vid"] and job.workspace_id == d["wid"]
        assert list(session.scalars(select(Job.id).where(Job.kind == "gtm.journey.prepare", Job.workspace_id.in_([d["wid"], published_ws.id])))) == [started["job_id"]]
        for vid in (published, unrelated, archived_venture, archived_workspace_venture):
            assert not session.scalar(select(VentureDocument.id).where(VentureDocument.venture_id == vid, VentureDocument.kind == journey.KIND))

"""Reviewed logic ports: no external code, network or automatic messages."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from tests.unit.gtm.test_contact_work import _sent
from tests.unit.gtm.test_journey import journey_db
from tests.unit.gtm.test_journey_signals import _leased_payload, quality_response

__all__ = ["journey_db"]


@pytest.fixture(params=[pytest.param("hosted", marks=pytest.mark.hosted_only), "local"])
def safety_runtime(journey_db, monkeypatch, request):
    # shortcut: runtime cases use a seeded schema, use the native local app fixture when testing startup or owner provisioning.
    if request.param == "local":
        from tests.company_os_harness import configure_local_gtm
        monkeypatch.setenv("SUPYMEM_MODE", "local")
        configure_local_gtm(monkeypatch)
    return request.param


@pytest.mark.parametrize("phrase", ["take me off", "do not contact", "delete my data", "unsubscribe", "remove me"])
def test_opt_out_overrides_interest_and_legal_threats(journey_db, monkeypatch, phrase):
    from app.gtm import contact_work
    from app.gtm.supymem import workers
    from app.db.models import GtmContact, GtmTouch
    from app.gtm.god import llm
    monkeypatch.setattr(llm, "json_chat", lambda **kw: pytest.fail("must not call a model"))
    cid = _sent(journey_db, monkeypatch)
    text = f"Yes interested, {phrase}. I will take legal action."
    assert workers._reply_intent(text) == "unsubscribe"
    assert workers._classify_reply(text) == "human_negative"
    contact_work.log_reply(journey_db["wid"], journey_db["vid"], cid, text, "interested")
    with journey_db["factory"]() as session:
        assert session.get(GtmContact, cid).do_not_contact
        touch = session.scalar(select(GtmTouch).where(GtmTouch.direction == "in"))
        assert touch.outcome == "unsubscribe" and touch.classification == "human_negative"


@pytest.mark.parametrize("text,expected", [("Wrong person, contact my colleague", "wrong_person"),
    ("How does this work?", "question"), ("Not now, contact me in March", "not_now"),
    ("Yes, I will take legal action", "escalate"), ("Fuck off", "escalate"), ("Not interested", "human_negative")])
def test_reply_taxonomy_without_a_model(text, expected):
    from app.gtm.supymem.workers import _classify_reply
    assert _classify_reply(text) == expected


def test_wrong_person_produces_a_reviewed_unsent_referral_ask(journey_db, monkeypatch):
    from app.gtm import contact_work, journey
    from app.gtm.supymem import composio_gmail
    from app.gtm.god import llm
    monkeypatch.setattr(llm, "json_chat", lambda **kw: pytest.fail("referral needs no model"))
    monkeypatch.setattr(composio_gmail, "send_gmail_email", lambda **kw: pytest.fail("must not send"))
    cid = _sent(journey_db, monkeypatch)
    view = journey.contact_action(journey_db["wid"], journey_db["vid"], cid, "reply", {"text": "Wrong person, sorry", "outcome": "wrong_person"})
    assert view["results"]["interested"] == 0 and view["results"]["replies"][0]["outcome"] == "wrong_person"
    assert "right person" in view["contact_work"][cid]["referral_ask"]
    contact_work.draft_reply(journey_db["wid"], journey_db["vid"], cid)
    reply = next(d for d in journey.outreach.drafts(journey_db["vid"]) if d.get("kind") == "reply")
    assert reply["state"] == "draft" and reply["requires_journey_review"] and reply["requires_batch_approval"]
    assert "right person" in reply["body"] and "None" not in reply["body"]
    journey.patch_draft(journey_db["wid"], journey_db["vid"], reply["doc_id"], {"decision": "approved", "expected_revision": journey._draft_revision(reply)})


def test_legal_reply_stops_contact_and_leaves_founder_action(journey_db, monkeypatch):
    from app.gtm import journey
    from app.db.models import GtmContact
    from app.needs_you import service
    requests = []
    monkeypatch.setattr(service, "request_human", lambda *args, **kw: requests.append(kw))
    cid = _sent(journey_db, monkeypatch)
    view = journey.contact_action(journey_db["wid"], journey_db["vid"], cid, "reply", {"text": "I will take legal action", "outcome": "interested"})
    assert view["results"]["negative"] == 1 and view["results"]["replies"][0]["outcome"] == "escalate"
    assert "Gmail" in view["contact_work"][cid]["reply_attention"]
    assert requests[0]["actions"] and "Skip" in requests[0]["how_to_resume"]
    with journey_db["factory"]() as session:
        assert session.get(GtmContact, cid).do_not_contact


@pytest.mark.parametrize("text,expected", [("not now; reconnect 2027-03-15", "2027-03-15"),
    ("under contract until March", "2027-03-01"), ("after November 3rd, 2026", "2026-11-03"),
    ("in 2 weeks", "2026-10-28"), ("next week", "2026-10-21"), ("tomorrow", "2026-10-15"),
    ("2026-02-30", None), ("2025-10-14", None), ("later", None)])
def test_not_now_date_parser(text, expected):
    from app.gtm.contact_work import follow_up_date
    due = follow_up_date(text, datetime(2026, 10, 14, tzinfo=timezone.utc))
    assert (due.date().isoformat() if due else None) == expected


def test_not_now_persists_date_and_does_not_schedule_a_message(journey_db, monkeypatch):
    from app.gtm import journey
    from app.db.models import GtmContact, GtmEmailSendQueue
    from app.gtm.relationships.cadence import company_block
    cid = _sent(journey_db, monkeypatch)
    view = journey.contact_action(journey_db["wid"], journey_db["vid"], cid, "reply", {"text": "Not now, please reconnect on 2099-03-15", "outcome": "not_now"})
    assert "2099-03-15" in view["contact_work"][cid]["follow_up"]
    with journey_db["factory"]() as session:
        contact = session.get(GtmContact, cid)
        assert json.loads(contact.enrichment_json)["follow_up_not_before"].startswith("2099-03-15")
        assert company_block(session, journey_db["wid"], contact, reply=True) == "follow_up_not_due"
        assert not session.scalar(select(GtmEmailSendQueue.id))


@pytest.mark.parametrize("total,simulated,paused", [(9, False, False), (10, False, True), (20, False, False), (10, True, False)])
def test_bounce_threshold_pauses_workspace_not_just_contact(journey_db, monkeypatch, total, simulated, paused):
    from app.db.models import GtmOutreachSettings, GtmTouch
    from app.gtm import journey
    from app.gtm.supymem.pipeline import apply_bounce
    from app.gtm.relationships.service import _settings
    from app.needs_you import service
    from core.clock import utcnow
    requests = []
    monkeypatch.setattr(service, "request_human", lambda *args, **kw: requests.append(kw))
    cid = _sent(journey_db, monkeypatch)
    with journey_db["factory"]() as session:
        for index in range(total - 1):
            session.add(GtmTouch(id=f"out_{index}", workspace_id=journey_db["wid"], contact_id=cid,
                direction="out", channel="email", sent_at=utcnow(), simulated=int(simulated), created_at=utcnow()))
        _settings(session, journey_db["wid"])
        session.commit()
    apply_bounce(cid)
    apply_bounce(cid)
    with journey_db["factory"]() as session:
        assert bool(session.get(GtmOutreachSettings, journey_db["wid"]).send_enabled) == (not paused)
    view = journey.status(journey_db["wid"], journey_db["vid"])
    if paused:
        assert view["send_blocked_reason"] == "send_paused" and "Resume sending" in view["sending_alert"]
        assert requests and "Click Resume sending" in requests[0]["how_to_resume"]


@pytest.mark.parametrize("text", ["As an AI, I'd love to talk.", "Let's delve into your workflow.",
    "I hope this finds you well.", "Hi {first_name}, let's talk.", "Hi {{ first_name }}, let's talk.",
    "See https://one.example and https://two.example", "See https://one.example"])
def test_wider_draft_gate_keeps_stricter_no_link_boundary(text):
    from app.gtm.discovery import _safe_note
    with pytest.raises(ValueError, match="draft_not_safe_or_short"):
        _safe_note(text)


def test_failed_model_draft_keeps_safe_founder_template(journey_db, monkeypatch):
    from app.gtm import journey, reachable
    async def model(payload, stage, context):
        if stage in {"marketrole", "passages"}:
            return quality_response(stage, context)
        if stage == "reachjudge":
            row = context["candidates"][0]
            return {"items": [{"id": row["id"], "kind": "person", "quote": row["evidence_pages"][0]["text"], "name": "Jane Smith", "role": "Engineer",
                "affiliation": "", "url": row["url"], "fit": 90, "why": "Published drawing work", "value_exchange": "Learn from drawing review experience"}]}
        return {"items": [{"id": 0, "body": "Hi {first_name}, as an AI I can supercharge your work.", "connection_note": "Hope this finds you well."}]}
    monkeypatch.setattr(journey, "_discovery_model", model)
    context = {"mode": "B2B", "audience": "Engineering consultancies", "problem": "A tool for drawing review", "competitors": [], "offer": ""}
    journey.update_icp(journey_db["wid"], journey_db["vid"], context["audience"])
    leads = asyncio.run(reachable.discover(None, [{"kind": "person", "url": "https://www.linkedin.com/in/jane", "title": "Jane Smith - Engineer",
        "snippet": "Jane Smith is an Engineer reviewing structural drawings."}], [], context, _leased_payload(journey_db)))
    assert leads[0]["draft_source"] == "template" and "I'm researching a tool for drawing review" in leads[0]["draft"]
    assert "as an AI" not in leads[0]["draft"] and "{" not in leads[0]["draft"]


@pytest.mark.parametrize("url", ["https://calendly.com/jane/15min", "https://cal.com/jane/intro", "https://calendar.app.google/abc",
    "https://calendar.google.com/calendar/appointments/schedules/abc", "https://meetings.hubspot.com/jane"])
def test_booking_must_be_published_on_own_page_and_ranks_first(url):
    from app.gtm import enrich, reachable
    page = "https://consultancy.example/contact"
    facts = enrich.extract("consultancy.example", [(page, f'<a href="{url}">Book a call</a><a href="mailto:info@consultancy.example">Email</a>')])
    route = reachable.contact_routes(facts)[0]
    assert route["kind"] == "booking" and route["url"] == url and route["source_url"] == page
    assert url in route["quote"]
    assert not enrich.extract("consultancy.example", [("https://thirdparty.example", f'<a href="{url}">Book</a>')])["booking"]
    blocked = enrich.extract("consultancy.example", [(page, f'<a href="{url}">Book</a>No solicitation')])
    assert not reachable.contact_routes(blocked)


@pytest.mark.parametrize("url", ["http://cal.com/jane", "https://cal.com.evil.example/jane", "https://u:p@cal.com/jane",
    "https://localhost/jane", "https://cal.com", "https://calendar.google.com/calendar/u/0"])
def test_booking_rejects_unsafe_or_not_booking_urls(url):
    from app.gtm.enrich import booking_url
    assert not booking_url(url)


def test_hiring_extract_is_inferred_and_filters_closed_roles():
    from app.gtm.enrich import hiring_signals
    url = "https://consultancy.example/careers"
    rows = hiring_signals(url, '<script><h2>Hiring manager</h2></script><h2>Structural Engineer</h2><a>Position closed</a><h3>No open positions</h3><li>Senior Project Manager</li>')
    assert [row["quote"] for row in rows] == ["Structural Engineer", "Senior Project Manager"]
    assert all(row["confidence"] == "inferred" and row["source_url"] == url and row["recency"] == "unknown" and row["published_at"] is None for row in rows)


def test_read_business_uses_configured_provider_for_linked_careers_and_streams_signal(journey_db, monkeypatch):
    from app.gtm import journey, reachable
    pages, traces = [], []
    monkeypatch.setattr(reachable, "trace", lambda *args, **kw: traces.append((args, kw)))
    async def public(self, url, **kw):
        pages.append((url, kw.get("main_only", True)))
        return '<h2>Structural Engineer</h2>' if url.endswith("/careers") else '<h1>Drawing Consultancy</h1><p>Reviews structural drawings</p><a href="/careers">Careers</a><a href="https://cal.com/drawing/intro">Book a call</a>'
    class Provider:
        async def scrape(self, url, **kw):
            return await public(self, url, **kw)
    async def model(payload, stage, context):
        if stage in {"marketrole", "passages"}:
            return quality_response(stage, context)
        if stage == "reachjudge":
            row = context["candidates"][0]
            return {"items": [{"id": row["id"], "kind": "business", "quote": "Drawing Consultancy", "name": "Drawing Consultancy",
                "affiliation": "", "url": row["evidence_pages"][0]["url"], "fit": 90, "why": "Published drawing review work", "value_exchange": "Share drawing research"}]}
        assert context["candidates"][0]["signals"][0]["confidence"] == "inferred"
        return {"items": []}
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fixture")
    monkeypatch.setenv("GTM_ENRICH_ENABLED", "1")
    monkeypatch.setattr(reachable.PublicPages, "scrape", lambda *args, **kw: pytest.fail("configured provider must not bypass Firecrawl"))
    monkeypatch.setattr(journey, "_discovery_model", model)
    context = {"mode": "B2B", "audience": "Structural engineering consultancies", "problem": "Review structural drawings", "competitors": [], "offer": ""}
    journey.update_icp(journey_db["wid"], journey_db["vid"], context["audience"])
    payload = _leased_payload(journey_db)
    leads = asyncio.run(reachable.discover(Provider(), [{"kind": "business", "url": "https://consultancy.example", "title": "Drawing Consultancy"}], [], context, payload))
    assert pages == [("https://consultancy.example/", True), ("https://consultancy.example/", False),
        ("https://consultancy.example/careers", False)]
    assert leads, traces
    assert leads[0]["signals"][0]["source_url"].endswith("/careers")
    saved = journey._journal(payload).document(payload["journey_id"])["reachable_leads"]
    assert saved[0]["signals"][0]["confidence"] == "inferred"


@pytest.mark.parametrize("name", ["Request Service", "Commercial Services", "Privacy Policy", "Company Solutions"])
def test_navigation_headings_are_not_people(name):
    from app.gtm.enrich import _people
    assert _people(name + "\nFounder") == []
    assert _people("Jane Smith\nFounder")[0]["name"] == "Jane Smith"


@pytest.mark.parametrize("url", ["https://directory.example/", "https://top10.example/", "https://localjournal.com/", "https://best-of.example/"])
def test_junk_domains_are_not_company_sites(url):
    from app.gtm.enrich import classify
    assert classify(url) == "directory"
    assert classify("https://engineering.example/") == "company"


def test_plan_and_signal_playbook_prompts_are_specific_and_retain_recency(journey_db, monkeypatch):
    from app.gtm import journey
    from app.research import metered
    messages = []
    def synthesize(*args, **kw):
        messages.append(kw["messages"])
        return {"items": []}
    monkeypatch.setattr(metered, "synthesize", synthesize)
    journey.update_icp(journey_db["wid"], journey_db["vid"], "Engineering consultancies")
    payload = _leased_payload(journey_db)
    asyncio.run(journey._discovery_model(payload, "plan", {}))
    asyncio.run(journey._discovery_model(payload, "reachdraft", {"candidates": [{"id": 0, "text": "Published engineer", "evidence_label": "fit_only", "signals": [{"recency": "older", "published_at": "2020-01-01"}]}]}))
    assert "literal job titles" in messages[0][0]["content"] and "title variants" in messages[0][0]["content"]
    assert all(phrase in messages[1][0]["content"] for phrase in ("exactly one", "never stack", "older than 90 days", "honest role-based"))
    assert all(phrase in messages[1][0]["content"] for phrase in ("evidence_label fit_only", "using only the supplied homepage evidence",
        "Do not claim they have the problem", "ask whether the problem applies to them", "Does the problem I'm researching affect your work?"))
    assert "older" in messages[1][1]["content"] and "2020-01-01" in messages[1][1]["content"]
    assert "fit_only" in messages[1][1]["content"]


def test_company_guard_uses_five_business_days_three_contacts_and_uncertain_delivery(journey_db, monkeypatch):
    from app.db.models import GtmCompany, GtmContact, GtmTouch, VentureDocument
    from app.gtm.relationships import cadence
    now = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(cadence, "utcnow", lambda: now)
    with journey_db["factory"]() as session:
        session.add(GtmCompany(id="company", workspace_id=journey_db["wid"], canonical_name="Engineering", created_at=now))
        for index in range(4):
            session.add(GtmContact(id=f"person_{index}", workspace_id=journey_db["wid"], company_id="company", email=f"person{index}@engineering.example", created_at=now, stage_changed_at=now))
        session.flush()
        contact = session.get(GtmContact, "person_1")
        session.add(GtmTouch(id="company_out", workspace_id=journey_db["wid"], contact_id="person_0", direction="out", channel="email",
            sent_at=datetime(2026, 10, 9, 12, tzinfo=timezone.utc), simulated=0, created_at=now))
        session.flush()
        assert cadence.company_block(session, journey_db["wid"], contact) == "company_contact_hold"
        assert cadence.company_block(session, journey_db["wid"], contact, reply=True) is None
        monkeypatch.setattr(cadence, "utcnow", lambda: datetime(2026, 10, 16, 12, tzinfo=timezone.utc))
        assert cadence.company_block(session, journey_db["wid"], contact) is None
        session.add(VentureDocument(id="uncertain", venture_id=journey_db["vid"], kind="outreach_draft", title="Held", body_md="draft",
            provenance_json=json.dumps({"contact_id": "person_0", "state": "delivery_unconfirmed"}), created_at=now, updated_at=now))
        session.flush()
        assert cadence.company_block(session, journey_db["wid"], contact) == "company_delivery_unconfirmed"
        session.delete(session.get(VentureDocument, "uncertain"))
        for index in (1, 2):
            session.add(GtmTouch(id=f"out_{index}", workspace_id=journey_db["wid"], contact_id=f"person_{index}", direction="out", channel="email",
                sent_at=now - timedelta(days=30), simulated=0, created_at=now))
        session.flush()
        assert cadence.company_block(session, journey_db["wid"], session.get(GtmContact, "person_3")) == "company_contact_limit"


def test_bounce_pause_blocks_queue_before_provider_or_drafting(journey_db, monkeypatch):
    from app.db.models import GtmEmailAccount, GtmEmailSendQueue
    from app.gtm.relationships.abuse import pause_sends
    from app.gtm.supymem import pipeline
    from core.clock import utcnow
    cid = _sent(journey_db, monkeypatch)
    from app.core.local_mode import is_local
    if is_local():
        from tests.unit.gtm.test_journey import _mail
        _mail(journey_db)
    now = utcnow()
    with journey_db["factory"]() as session:
        session.add(GtmEmailAccount(id="mailbox", workspace_id=journey_db["wid"], email="founder@engineering.example",
            connected_account_id="live", composio_user_id="founder", created_at=now, updated_at=now))
        session.flush()
        session.add(GtmEmailSendQueue(id="queue", workspace_id=journey_db["wid"], contact_id=cid, email_account_id="mailbox",
            sequence_day=1, status="sending", idempotency_key="queue-fixture", scheduled_for=now, created_at=now, updated_at=now))
        session.commit()
    pause_sends(journey_db["wid"])
    monkeypatch.setattr(pipeline, "_send_one_admitted", lambda *args, **kw: pytest.fail("pause must stop before any provider work"))
    result = pipeline._send_one("queue", simulated=False)
    assert result["skipped"] and result["reason"] == "send_paused"
    with journey_db["factory"]() as session:
        assert session.get(GtmEmailSendQueue, "queue").status == "queued"


def test_company_admission_blocks_parallel_sender_before_provider(journey_db, monkeypatch):
    from app.autopilot import outreach
    from app.autopilot.send_admission import try_send_admission
    from app.approvals.external import OutboundExecutionError
    from app.db.models import GtmContact
    cid = _sent(journey_db, monkeypatch)
    with journey_db["factory"]() as session:
        company_id = session.get(GtmContact, cid).company_id
    monkeypatch.setattr(outreach, "_journey_send_one_admitted", lambda *args, **kw: pytest.fail("no simultaneous company send"))
    with try_send_admission(f"company:{journey_db['wid']}:{company_id}") as admitted:
        assert admitted
        with pytest.raises(OutboundExecutionError) as error:
            outreach._journey_send_one(journey_db["vid"], {"contact_id": cid}, {}, "unused", None)
        assert error.value.code == "company_contact_hold"


def test_company_batch_holds_colleagues_without_discarding_reviewed_drafts(journey_db, monkeypatch):
    from app.gtm import journey
    from app.autopilot import outreach
    from tests.unit.gtm.test_journey import _add, _mail, _review
    sent = []
    for name in ("Jane", "Bob", "Ada"):
        _add(journey_db, monkeypatch, name=name, email=name.lower() + "@bakery.example")
    rows = outreach.drafts(journey_db["vid"])
    for row in rows:
        outreach._save_draft(journey_db["vid"], row | {"batch_id": rows[0]["batch_id"]}, row["doc_id"])
    _mail(journey_db)
    _, request = _review(journey_db)
    monkeypatch.setattr(outreach, "_send_one", lambda *args: sent.append(args) or {"provider_message_id": "once", "thread_id": "once"})
    view = asyncio.run(journey.send(journey_db["wid"], journey_db["vid"], request))
    assert len(sent) == 1 and sorted(row["state"] for row in view["drafts"]) == ["draft", "draft", "sent"]
    assert "hold" in view["receipt"]["body_md"] and "stay saved" in view["receipt"]["body_md"]
    assert sum(prospect["blocked_reason"] == "company_contact_hold" for prospect in view["prospects"]) == 2


@pytest.mark.parametrize("domain,names,legacy,expected", [
    ("gmail.com", (None, None), False, 2),
    ("googlemail.com", (None, None), False, 2),
    ("outlook.com", ("Cedar Dispatch", "Maple Lessons"), False, 2),
    ("proton.me", (None, None), False, 2),
    ("gmx.net", (None, None), False, 2),
    ("gmail.com", (None, None), True, 2),
    ("gmail.com", ("Juniper Studio", "Juniper Studio"), False, 1),
    ("hotmail.com", ("Birch Logistics", "Birch Logistics"), False, 1),
    ("freightdesk.example", ("Cedar Dispatch", "Maple Lessons"), False, 1),
    ("lessonhouse.example", (None, None), False, 1),
])
def test_company_holds_distinguish_personal_mailboxes(journey_db, monkeypatch, domain, names, legacy, expected):
    from app.autopilot import outreach
    from app.db.models import GtmCompany, GtmContact, GtmTouch
    from app.gtm import journey
    from app.gtm.relationships.store import upsert_company
    from core.clock import utcnow

    from tests.unit.gtm.test_journey import _add, _mail, _review

    for index, recipient in enumerate(("Riley", "Morgan")):
        _add(journey_db, monkeypatch, name=recipient, email=f"{recipient.lower()}@{domain}")
        with journey_db["factory"]() as session:
            if legacy:
                company = session.get(GtmCompany, "personal_provider")
                if company is None:
                    company = GtmCompany(id="personal_provider", workspace_id=journey_db["wid"],
                        canonical_name=domain, domain=domain, created_at=utcnow())
                    session.add(company)
            else:
                company = upsert_company(session, workspace_id=journey_db["wid"], name=names[index] or domain, domain=domain)
            contact = session.scalar(select(GtmContact).where(GtmContact.email == f"{recipient.lower()}@{domain}"))
            contact.company_id = company.id
            session.commit()
    rows = outreach.drafts(journey_db["vid"])
    for row in rows:
        outreach._save_draft(journey_db["vid"], row | {"batch_id": rows[0]["batch_id"]}, row["doc_id"])
    _mail(journey_db)
    _, request = _review(journey_db)
    sent = []
    monkeypatch.setattr(outreach, "_send_one", lambda *args: sent.append(args) or {
        "provider_message_id": f"delivery_{len(sent)}", "thread_id": f"thread_{len(sent)}"})
    view = asyncio.run(journey.send(journey_db["wid"], journey_db["vid"], request))
    assert len(sent) == expected
    assert sum(row["state"] == "sent" for row in view["drafts"]) == expected
    assert sum(prospect["blocked_reason"] == "company_contact_hold" for prospect in view["prospects"]) == 2 - expected
    with journey_db["factory"]() as session:
        touches = list(session.scalars(select(GtmTouch).where(GtmTouch.direction == "out")))
        assert len(touches) == expected and all(touch.simulated == 0 for touch in touches)


@pytest.mark.parametrize("domain", ["gmail.com", "dispatchworks.example"])
def test_opt_out_blocks_plus_variants_of_the_same_mailbox(journey_db, domain, safety_runtime):
    from app.db.models import GtmContact, GtmSuppression
    from app.gtm.relationships.suppression import email_suppressed, opted_out
    from core.clock import utcnow

    with journey_db["factory"]() as session:
        now = utcnow()
        for index, local in enumerate(("river_lane", "RIVER_LANE+Research", "river_lane+other", "riverXlane+research", "another")):
            session.add(GtmContact(id=f"mailbox_{index}", workspace_id=journey_db["wid"], email=f"{local}@{domain}",
                created_at=now, stage_changed_at=now))
        suppression = GtmSuppression(id="mailbox_stop", workspace_id=journey_db["wid"], email=f"river_lane@{domain}",
            reason="public_unsubscribe", source="opt_out", created_at=now)
        session.add(suppression)
        session.flush()
        for suppressed_emails in (None, set(), {f"river_lane@{domain}"}):
            for index in range(5):
                contact = session.get(GtmContact, f"mailbox_{index}")
                assert email_suppressed(session, journey_db["wid"], contact, replied_ok=True,
                    suppressed_emails=suppressed_emails) == (index < 3)
                assert opted_out(session, journey_db["wid"], contact) == (index < 3)
        suppression.email = f"river_lane+old@{domain}"
        session.flush()
        session.info.pop("gtm_owner_suppression", None)
        for index in range(5):
            contact = session.get(GtmContact, f"mailbox_{index}")
            assert email_suppressed(session, journey_db["wid"], contact, suppressed_emails=set()) == (index < 3)
            assert opted_out(session, journey_db["wid"], contact) == (index < 3)


@pytest.mark.hosted_only
@pytest.mark.parametrize("domain", ["gmail.com", "dispatchworks.example"])
def test_shared_opt_out_blocks_plus_variants_of_the_same_mailbox(journey_db, domain):
    from app.gtm.email_learning import globally_suppressed, prefetch_suppression, suppress

    with journey_db["factory"]() as session:
        emails = [f"{local}@{domain}" for local in
                  ("river_lane", "RIVER_LANE+Research", "river_lane+other", "riverXlane+research", "another")]
        for address in (f"river_lane@{domain}", f"river_lane+old@{domain}"):
            suppress(session, address)
            session.flush()
            prefetch_suppression(session, emails)
            assert [globally_suppressed(session, email) for email in emails] == [True, True, True, False, False]

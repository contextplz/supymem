import asyncio
import hashlib
import hmac
import json
import sys
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tests.unit.gtm.test_journey import journey_db
from tests.unit.gtm.test_journey_signals import _leased_payload


def _facts():
    from app.gtm import enrich
    return enrich.extract("consultancy.example", [("https://consultancy.example/team", """
        <p>Arjun Rao, Principal<br>arjun.rao@consultancy.example</p>
        <p>Jane Patel, Structural Engineer</p>
        <p>Contact our office: info@consultancy.example</p>
        """)])


def _event(session, workspace, kind, when, *, email="jane.patel@consultancy.example", pattern="first.last", suffix=""):
    from app.gtm.email_learning import opaque
    from app.gtm.shared_models import SharedEmailOutcome
    session.add(SharedEmailOutcome(event_key=opaque(f"{workspace}:{kind}:{email}:{suffix}", "event"),
        address_hash=opaque(email), workspace_hash=opaque(workspace, "workspace"), domain=email.rsplit("@", 1)[-1],
        pattern=pattern, kind=kind, observed_at=when))
    session.flush()


@pytest.mark.hosted_only
def test_generated_status_reads_batch_learning_without_writes_or_network(journey_db, monkeypatch):
    import copy
    from sqlalchemy import event
    from app.db.models import GtmCompany, GtmContact
    from app.gtm import email_learning, enrich, journey, reachable
    from core.clock import utcnow
    now = utcnow()
    current = {"who": "Engineering consultancies", "competitors": [], "offer": "Drawing software", "problem": "Review drawings"}
    leads = []
    with journey_db["factory"]() as session:
        for index in range(50):
            host = f"consultancy{index}.example"
            email = f"jane.patel@{host}"
            url = f"https://{host}/team"
            route = {"kind": "email", "route_type": "guess", "email": email, "pattern": "first.last", "mx_status": "unchecked", "source_url": url}
            data = {"website": f"https://{host}/", "person_name": "Jane Patel", "person_quote": "Jane Patel, Engineer",
                "person_source_url": url, "email_route": route}
            session.add(GtmCompany(id=f"batch_company_{index}", workspace_id=journey_db["wid"], canonical_name=f"Consultancy {index}", domain=host, created_at=now))
            session.flush()
            session.add(GtmContact(id=f"batch_contact_{index}", workspace_id=journey_db["wid"], company_id=f"batch_company_{index}", email=email,
                email_confidence="guess", recipient_geo="US", lawful_basis="legitimate_interest", source=f"venture:{journey_db['vid']}:public",
                enrichment_json=json.dumps(data), stage_changed_at=now, created_at=now))
            leads.append({"id": str(index), "name": f"Consultancy {index}", "country": "US", "routes": [route]})
        _event(session, journey_db["wid"], "soft_bounce", now, email=leads[0]["routes"][0]["email"])
        email_learning.suppress(session, leads[1]["routes"][0]["email"])
        session.commit()
    def forbidden(*args, **kwargs):
        pytest.fail("status read attempted provider lookup or learning write")
    for target, name in [(enrich, "mx_status"), (email_learning, "candidate"), (email_learning, "learned_patterns"),
            (email_learning, "refresh"), (journey, "_icp")]:
        monkeypatch.setattr(target, name, forbidden)
    statements = []
    def count_sql(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    engine = journey_db["factory"].kw["bind"]
    event.listen(engine, "before_cursor_execute", count_sql)
    try:
        query_counts = []
        for size in (2, 50):
            statements.clear()
            visible = reachable.visible(copy.deepcopy(leads[:size]), journey_db["wid"], venture_id=journey_db["vid"])
            assert len(visible) == size - 2
            assert {lead["id"] for lead in visible} == {str(index) for index in range(2, size)}
            assert all(lead["routes"] for lead in visible)
            query_counts.append(len(statements))
        assert query_counts[0] == query_counts[1] <= 10
        statements.clear()
        prospects = journey._prospects(journey_db["vid"], wid=journey_db["wid"], icp=current, current=current)
        assert len(prospects) == 50
        assert next(lead for lead in prospects if lead["id"] == "batch_contact_0")["email"] is None
        assert next(lead for lead in prospects if lead["id"] == "batch_contact_1")["email"] is None
        assert len(statements) <= 20
        assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    finally:
        event.remove(engine, "before_cursor_execute", count_sql)


@pytest.mark.hosted_only
def test_named_published_pattern_and_single_primary_guess(journey_db, monkeypatch):
    from app.gtm import enrich, reachable
    monkeypatch.setattr(enrich, "mx_status", lambda domain: "mx")
    facts = _facts()
    assert facts["people"][0]["email"] == "arjun.rao@consultancy.example"
    routes = enrich.guessed_routes(facts, "consultancy.example", journey_db["wid"])
    assert len(routes) == 1
    route = routes[0]
    assert route["email"] == "jane.patel@consultancy.example" and route["route_type"] == "pattern"
    assert route["confidence"] == 80 and "arjun.rao@consultancy.example" in route["pattern_quote"]
    published = reachable.contact_routes(facts)[0]
    assert published["route_type"] == "published" and published["confidence"] == 100
    assert reachable.route_priority(published) < reachable.route_priority(route) < reachable.route_priority({**route, "confidence": 40})
    merged = enrich.merge({"website": "https://consultancy.example/"}, {**facts, "email": None, "guessed_routes": routes})
    assert enrich.generated_route_allowed(merged, route["email"])
    assert not enrich.generated_route_allowed({**merged, "person_name": "Other Person"}, route["email"])


@pytest.mark.parametrize("address", ["info", "sales", "nobody", "arjun.rao@other.example"])
@pytest.mark.hosted_only
def test_only_provable_named_same_domain_patterns(journey_db, monkeypatch, address):
    from app.gtm import enrich
    monkeypatch.setattr(enrich, "mx_status", lambda domain: "mx")
    facts = _facts()
    facts["people"][0]["email"] = address if "@" in address else address + "@consultancy.example"
    route = enrich.guessed_routes(facts, "consultancy.example", journey_db["wid"])[0]
    assert route["route_type"] == "guess" and route["confidence"] == 40


@pytest.mark.parametrize("pattern", ["first.last", "first", "flast", "firstl", "first_last", "f.last", "last"])
@pytest.mark.hosted_only
def test_all_named_company_patterns(journey_db, monkeypatch, pattern):
    from app.gtm import enrich
    monkeypatch.setattr(enrich, "mx_status", lambda domain: "mx")
    facts = _facts()
    facts["people"][0]["email"] = enrich.email_patterns("Arjun Rao")[pattern] + "@consultancy.example"
    route = enrich.guessed_routes(facts, "consultancy.example", journey_db["wid"])[0]
    assert route["pattern"] == pattern
    assert route["email"] == enrich.email_patterns("Jane Patel")[pattern] + "@consultancy.example"


@pytest.mark.parametrize("country,domain,b2b,switch", [("DE", "consultancy.example", True, "on"),
    ("AT", "consultancy.example", True, "on"), ("IN", "consultancy.de", True, "on"),
    ("IN", "consultancy.at", True, "on"), ("IN", "consultancy.example", False, "on"),
    ("IN", "consultancy.example", True, "off")])
def test_compliance_and_switch_preserve_published_routes(journey_db, monkeypatch, country, domain, b2b, switch):
    from app.gtm import enrich, reachable
    from app.config import settings
    monkeypatch.setattr(settings, "guessed_emails", switch)
    monkeypatch.setattr(enrich, "mx_status", lambda domain: pytest.fail("must not resolve gated domains"))
    facts = _facts()
    facts["country"] = country
    before = reachable.contact_routes(facts)
    assert not enrich.guessed_routes(facts, domain, journey_db["wid"], b2b=b2b)
    assert reachable.contact_routes(facts) == before


@pytest.mark.parametrize("mx,confidence", [("mx", 40), ("unchecked", 30), ("no_mx", None)])
@pytest.mark.hosted_only
def test_mx_gate_and_unchecked_discount(journey_db, monkeypatch, mx, confidence):
    from app.gtm import enrich
    monkeypatch.setattr(enrich, "mx_status", lambda domain: mx)
    facts = _facts()
    facts["people"] = facts["people"][1:]
    routes = enrich.guessed_routes(facts, "consultancy.example", journey_db["wid"])
    assert (routes[0]["confidence"] if routes else None) == confidence


def test_dns_is_lazy_mx_only_and_handles_missing_error_null_and_nxdomain(journey_db, monkeypatch):
    from app.gtm.enrich import mx_status
    monkeypatch.setitem(sys.modules, "dns", None)
    assert mx_status("consultancy.example") == "unchecked"
    class NXDOMAIN(Exception):
        pass
    class NoAnswer(Exception):
        pass
    calls = []
    def resolve(domain, record_type, **kwargs):
        calls.append((domain, record_type, kwargs))
        return [SimpleNamespace(exchange="mail.example.")]
    resolver = SimpleNamespace(resolve=resolve, NXDOMAIN=NXDOMAIN, NoAnswer=NoAnswer)
    monkeypatch.setitem(sys.modules, "dns", SimpleNamespace(resolver=resolver))
    monkeypatch.setitem(sys.modules, "dns.resolver", resolver)
    assert mx_status("consultancy.example") == "mx"
    assert calls == [("consultancy.example", "MX", {"lifetime": 3, "search": False})]
    for error, expected in [(NXDOMAIN(), "no_mx"), (NoAnswer(), "no_mx"), (TimeoutError(), "unchecked")]:
        def fail(*args, **kwargs):
            raise error
        resolver.resolve = fail
        assert mx_status("consultancy.example") == expected
    resolver.resolve = lambda *args, **kwargs: [SimpleNamespace(exchange=".")]
    assert mx_status("consultancy.example") == "no_mx"


@pytest.mark.parametrize("workspaces,deliveries,bounces,retired", [(1, 0, 8, False), (2, 0, 2, True),
    (2, 1, 2, False), (3, 2, 3, True), (3, 3, 3, False)])
@pytest.mark.hosted_only
def test_address_retirement_distinct_workspace_thresholds(journey_db, workspaces, deliveries, bounces, retired):
    from app.gtm.email_learning import opaque, refresh
    from core.clock import utcnow
    now = utcnow()
    with journey_db["factory"]() as session:
        for index in range(deliveries):
            _event(session, f"delivery-{index}", "delivery", now)
        for index in range(bounces):
            _event(session, f"bounce-{index % workspaces}", "hard_bounce", now, suffix=str(index))
        row = refresh(session, opaque("jane.patel@consultancy.example"), "consultancy.example", now=now)
        assert (row.status == "retired") == retired
        assert row.hard_bounce_workspaces == workspaces


@pytest.mark.hosted_only
def test_delivery_waits_72_hours_and_any_bounce_disqualifies(journey_db):
    from app.gtm.email_learning import opaque, refresh
    from core.clock import utcnow
    now = utcnow()
    with journey_db["factory"]() as session:
        _event(session, "first", "send", now)
        row = refresh(session, opaque("jane.patel@consultancy.example"), "consultancy.example", now=now + timedelta(hours=71))
        assert row.status == "unknown" and row.deliveries == 0
        row = refresh(session, row.address_hash, "consultancy.example", now=now + timedelta(hours=72))
        assert row.status == "deliverable" and row.delivery_workspaces == 1
        _event(session, "first", "soft_bounce", now + timedelta(hours=5))
        row = refresh(session, row.address_hash, "consultancy.example", now=now + timedelta(hours=72))
        assert row.status == "unknown" and row.hard_bounce_workspaces == 0


@pytest.mark.parametrize("workspaces,sends,bounces,retired", [(1, 6, 6, False), (2, 6, 6, False),
    (3, 4, 4, False), (3, 6, 3, False), (3, 5, 3, True)])
@pytest.mark.hosted_only
def test_domain_pattern_retirement_thresholds(journey_db, workspaces, sends, bounces, retired):
    from app.gtm.email_learning import opaque, refresh
    from app.gtm.shared_models import SharedEmailPattern
    from core.clock import utcnow
    now = utcnow()
    with journey_db["factory"]() as session:
        for index in range(sends):
            _event(session, f"founder-{index % workspaces}", "send", now, email=f"person.{index}@consultancy.example")
        for index in range(bounces):
            _event(session, f"founder-{index % workspaces}", "hard_bounce", now, email=f"person.{index}@consultancy.example")
        refresh(session, opaque("person.0@consultancy.example"), "consultancy.example", "first.last", now=now)
        row = session.get(SharedEmailPattern, ("consultancy.example", "first.last"))
        assert bool(row.retired_at) == retired
        assert row.workspace_count == workspaces


@pytest.mark.hosted_only
def test_decay_and_retirement_expiry_are_low_confidence(journey_db, monkeypatch):
    from app.gtm import email_learning, enrich
    from app.gtm.shared_models import SharedEmailPattern
    from core.clock import utcnow
    now = utcnow()
    with journey_db["factory"]() as session:
        _event(session, "delivery", "delivery", now - timedelta(days=90))
        row = email_learning.refresh(session, email_learning.opaque("jane.patel@consultancy.example"), "consultancy.example", now=now)
        assert row.deliveries == pytest.approx(.5)
        row.retired_at = now - timedelta(days=90)
        pattern = SharedEmailPattern(domain="consultancy.example", pattern="first.last", published_pattern=True,
            retired_at=now - timedelta(days=90), last_seen=now)
        session.add(pattern)
        session.commit()
    monkeypatch.setattr(email_learning, "utcnow", lambda: now)
    monkeypatch.setattr(enrich, "mx_status", lambda domain: "mx")
    facts = _facts()
    facts["people"] = facts["people"][1:]
    route = enrich.guessed_routes(facts, "consultancy.example", journey_db["wid"])[0]
    assert route["confidence"] <= 30
    assert not email_learning.candidate(route["email"], journey_db["wid"])["blocked"]


@pytest.mark.hosted_only
def test_other_founder_delivery_confirms_but_no_leads_are_shared(journey_db, monkeypatch):
    from app.gtm import email_learning, enrich
    from app.db.models import GtmContact, VentureDocument
    from core.clock import utcnow
    with journey_db["factory"]() as session:
        _event(session, "other-workspace", "send", utcnow() - timedelta(hours=73))
        session.commit()
    monkeypatch.setattr(enrich, "mx_status", lambda domain: "mx")
    facts = _facts()
    facts["people"] = facts["people"][1:]
    route = enrich.guessed_routes(facts, "consultancy.example", journey_db["wid"])[0]
    assert route["confidence"] == 90 and route["confidence_label"] == "Confirmed working by other founders"
    assert not email_learning.candidate(route["email"], "other-workspace")["confirmed"]
    with journey_db["factory"]() as session:
        assert not list(session.scalars(select(GtmContact)))
        assert not list(session.scalars(select(VentureDocument).where(VentureDocument.kind == "gtm.shared.leads")))


@pytest.mark.hosted_only
def test_hmac_global_optout_and_no_raw_address_or_workspace(journey_db):
    from app.gtm import email_learning
    from app.config import settings
    from app.db.models import GtmContact
    from app.gtm.shared_models import SharedAddressValidity, SharedEmailOutcome
    from app.gtm.relationships.suppression import email_suppressed
    email = "Jane.Patel@consultancy.example"
    key = hmac.new(settings.company_os_vault_secret.encode(), b"gtm-shared:address", hashlib.sha256).digest()
    expected = hmac.new(key, email.lower().encode(), hashlib.sha256).hexdigest()
    assert email_learning.opaque(" " + email + " ") == expected
    assert email_learning.opaque(email, "workspace") != expected
    with journey_db["factory"]() as session:
        email_learning.suppress(session, email)
        session.commit()
        contact = GtmContact(workspace_id="other-founder", email=email, enrichment_json="{}")
        assert email_suppressed(session, "other-founder", contact, suppressed_emails=set())
        row = session.get(SharedAddressValidity, expected)
        assert row.suppressed and len(row.address_hash) == 64
        assert "email" not in row.__table__.columns
        assert "workspace_id" not in SharedEmailOutcome.__table__.columns
        assert not list(session.scalars(select(SharedEmailOutcome)))


@pytest.fixture
def shared_secret_unavailable(journey_db, monkeypatch):
    from app.config import settings
    from app.gtm import email_learning
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "company_os_vault_secret", "company-os-local-vault")
    monkeypatch.setattr(email_learning, "_secret_error_logged", False)


@pytest.mark.hosted_only
def test_missing_secret_skips_shared_features_once_without_touching_pending_transaction(journey_db, shared_secret_unavailable, caplog):
    from app.gtm import email_learning
    from app.db.models import GtmContact, GtmSuppression
    from app.gtm.relationships.suppression import email_suppressed
    from app.gtm.shared_models import SharedAddressValidity, SharedEmailOutcome, SharedEmailPattern
    from core.clock import utcnow
    contact = SimpleNamespace(email="jane.patel@consultancy.example", workspace_id=journey_db["wid"])
    with pytest.raises(ValueError, match="gtm_shared_secret_unavailable"):
        email_learning.opaque(contact.email)
    with journey_db["factory"]() as session:
        session.add(GtmSuppression(id="workspace_stop", workspace_id=contact.workspace_id, email=contact.email,
            source="opt_out", created_at=utcnow()))
        session.info["gtm_shared_suppression"] = {"old_hash": True}
        email_learning.prefetch_suppression(session, [contact.email])
        assert "gtm_shared_suppression" not in session.info
        assert not email_learning.globally_suppressed(session, contact.email)
        assert not email_learning.globally_suppressed(None, contact.email)
        email_learning.record(session, contact, "send", "receipt")
        email_learning.suppress(session, contact.email)
        session.commit()
        assert session.get(GtmSuppression, "workspace_stop") is not None
        recipient = GtmContact(workspace_id=contact.workspace_id, email=contact.email, enrichment_json="{}")
        assert email_suppressed(session, contact.workspace_id, recipient)
        for model in (SharedAddressValidity, SharedEmailOutcome, SharedEmailPattern):
            assert not list(session.scalars(select(model)))
    assert email_learning.learned_patterns("consultancy.example") == []
    assert email_learning.retired_patterns("consultancy.example") == set()
    assert email_learning.candidate(contact.email, contact.workspace_id) == {"blocked": False, "confirmed": False, "expired": False}
    errors = [record for record in caplog.records if record.name == email_learning.__name__ and record.levelname == "ERROR"]
    assert len(errors) == 1 and "workspace suppression remains active" in errors[0].message
    assert contact.email not in errors[0].message


@pytest.mark.hosted_only
def test_configuring_secret_restores_shared_protection_without_restart(journey_db, shared_secret_unavailable, monkeypatch):
    from app.config import settings
    from app.gtm import email_learning
    email = "jane.patel@consultancy.example"
    assert not email_learning.globally_suppressed(None, email)
    monkeypatch.setattr(settings, "company_os_vault_secret", "configured-server-secret-32bytes-long")
    with journey_db["factory"]() as session:
        email_learning.suppress(session, email)
        session.commit()
    assert email_learning.globally_suppressed(None, email)


@pytest.mark.hosted_only
def test_missing_secret_discovery_uses_current_public_evidence_not_shared_patterns(journey_db, shared_secret_unavailable, monkeypatch):
    from app.gtm import enrich, reachable
    monkeypatch.setattr(enrich, "mx_status", lambda domain: "mx")
    facts = _facts()
    route = enrich.guessed_routes(facts, "consultancy.example", journey_db["wid"])[0]
    assert route["route_type"] == "pattern" and route["confidence"] == 80
    lead = {"id": "jane", "name": route["name"], "routes": [route], "country": "IN", "fit": 85}
    assert reachable.visible([lead], journey_db["wid"])[0]["routes"] == [route]
    facts["people"] = facts["people"][1:]
    route = enrich.guessed_routes(facts, "consultancy.example", journey_db["wid"])[0]
    assert route["route_type"] == "guess" and route["confidence"] == 40


def test_missing_secret_confirmed_send_is_committed_once_and_workspace_optout_still_blocks(journey_db, shared_secret_unavailable, monkeypatch):
    from app.autopilot import outreach
    from app.db.models import CreditAccount, GtmTouch
    from app.gtm import journey
    from app.gtm.shared_models import SharedEmailOutcome
    from tests.unit.gtm.test_journey import _add, _mail, _review
    data = journey_db
    contact_id = _add(data, monkeypatch)["prospects"][0]["id"]
    _mail(data)
    _, request = _review(data)
    sent = []
    monkeypatch.setattr(outreach, "_send_one", lambda *args: sent.append(args[1]) or {"provider_message_id": "receipt-1", "thread_id": "thread-1"})
    result = asyncio.run(journey.send(data["wid"], data["vid"], request))
    assert result["results"]["sent"] == 1 and sent == ["ada@bakery.example"]
    with data["factory"]() as session:
        from app.core.local_mode import is_local

        if is_local():
            assert data["account"] is None
        else:
            assert session.get(CreditAccount, data["account"]).spent_micro == 10_000
        assert len(list(session.scalars(select(GtmTouch).where(GtmTouch.direction == "out")))) == 1
        assert not list(session.scalars(select(SharedEmailOutcome)))
    with pytest.raises(ValueError, match="gtm_approval_changed"):
        asyncio.run(journey.send(data["wid"], data["vid"], request))
    outreach.record_opt_out(data["wid"], contact_id, "ada@bakery.example")
    state = journey.status(data["wid"], data["vid"])
    assert state["prospects"][0]["blocked_reason"] == "suppressed_or_replied" and len(sent) == 1


@pytest.mark.parametrize("outcome", ["opt_out", "public_unsubscribe", "bounce"])
def test_missing_secret_durable_workspace_outcomes_still_stop_contact(journey_db, shared_secret_unavailable, monkeypatch, outcome):
    from app.db.models import GtmContact, GtmSuppression
    from app.gtm import email_learning, journey
    from app.gtm.relationships import unsubscribe
    from app.gtm.relationships.suppression import email_suppressed
    from app.gtm.supymem import pipeline
    from tests.unit.gtm.test_contact_work import _sent
    data = journey_db
    contact_id = _sent(data, monkeypatch)
    if outcome == "opt_out":
        assert pipeline.apply_reply(contact_id, body="remove me", classification="human_negative")
    elif outcome == "public_unsubscribe":
        token = unsubscribe.issue_unsubscribe_token(data["wid"], contact_id, "ada@bakery.example")
        assert unsubscribe.honor_unsubscribe_token(token)["ok"]
    else:
        pipeline.apply_bounce(contact_id, reason="5.1.1 mailbox does not exist")
    with data["factory"]() as session:
        contact = session.get(GtmContact, contact_id)
        assert email_suppressed(session, data["wid"], contact)
        if outcome == "bounce":
            assert contact.bounced_at is not None
        else:
            assert contact.do_not_contact and contact.unsubscribed_at
            assert session.scalar(select(GtmSuppression.id).where(GtmSuppression.workspace_id == data["wid"]))
        other = GtmContact(workspace_id="another_workspace", email=contact.email, enrichment_json="{}")
        assert not email_suppressed(session, other.workspace_id, other)
    assert email_learning.candidate("ada@bakery.example", data["wid"])["blocked"] == (outcome == "bounce")
    assert journey.status(data["wid"], data["vid"])["prospects"][0]["blocked_reason"] == "suppressed_or_replied"


@pytest.mark.hosted_only
def test_bounce_lowers_reply_raises_and_events_are_idempotent(journey_db):
    from app.gtm import email_learning
    from app.gtm.shared_models import SharedEmailPattern, SharedEmailOutcome
    from core.clock import utcnow
    contact = SimpleNamespace(email="jane.patel@consultancy.example", workspace_id=journey_db["wid"],
        enrichment_json=json.dumps({"email_route": {"route_type": "pattern", "pattern": "first.last"}}))
    with journey_db["factory"]() as session:
        email_learning.record(session, contact, "send", "send-1")
        pattern = session.get(SharedEmailPattern, ("consultancy.example", "first.last"))
        initial = pattern.confidence
        email_learning.record(session, contact, "hard_bounce", "bounce-1")
        assert pattern.confidence < initial
        lowered = pattern.confidence
        email_learning.record(session, contact, "reply", "reply-1")
        assert pattern.confidence > lowered
        email_learning.record(session, contact, "reply", "reply-1")
        assert len(list(session.scalars(select(SharedEmailOutcome).where(SharedEmailOutcome.kind == "reply")))) == 1
        contact.workspace_id = "other-founder"
        email_learning.record(session, contact, "reply", "reply-1")
        assert len(list(session.scalars(select(SharedEmailOutcome).where(SharedEmailOutcome.kind == "reply")))) == 2
        session.commit()


@pytest.mark.hosted_only
def test_local_bounce_never_reoffered_and_soft_bounce_never_retires(journey_db):
    from app.gtm import email_learning
    from app.gtm.shared_models import SharedEmailPattern
    from core.clock import utcnow
    with journey_db["factory"]() as session:
        for index in range(5):
            _event(session, journey_db["wid"] if index == 0 else f"other-{index}", "soft_bounce", utcnow(), suffix=str(index))
        row = email_learning.refresh(session, email_learning.opaque("jane.patel@consultancy.example"), "consultancy.example", "first.last")
        assert row.status == "unknown" and row.hard_bounce_workspaces == 0
        pattern = session.get(SharedEmailPattern, ("consultancy.example", "first.last"))
        assert pattern.retired_at is None and pattern.confidence < 40
        session.commit()
    assert email_learning.candidate("jane.patel@consultancy.example", journey_db["wid"])["blocked"]
    assert not email_learning.candidate("jane.patel@consultancy.example", "new-founder")["blocked"]


def test_testimonial_or_nonstaff_name_never_gets_guessed_mail(journey_db, monkeypatch):
    from app.gtm import enrich
    monkeypatch.setattr(enrich, "mx_status", lambda domain: pytest.fail("not a staff member"))
    for page, prefix in [("https://consultancy.example/team", "<h2>Testimonials</h2>"), ("https://consultancy.example/", "")]:
        facts = enrich.extract("consultancy.example", [(page, prefix + "<p>Jane Patel, Head of Communications</p>")])
        assert not enrich.guessed_routes(facts, "consultancy.example", journey_db["wid"])


@pytest.mark.hosted_only
def test_later_team_page_verifies_name_seen_on_homepage(journey_db, monkeypatch):
    from app.gtm import enrich
    monkeypatch.setattr(enrich, "mx_status", lambda domain: "mx")
    facts = enrich.extract("consultancy.example", [("https://consultancy.example/", "Jane Patel, Structural Engineer"),
        ("https://consultancy.example/team", "Jane Patel, Structural Engineer")])
    route = enrich.guessed_routes(facts, "consultancy.example", journey_db["wid"])[0]
    assert route["source_url"] == "https://consultancy.example/team" and route["route_type"] == "guess"


@pytest.mark.hosted_only
def test_streamed_judged_guess_is_ingested_privately_and_switch_off_blocks_send(journey_db, monkeypatch):
    from app.gtm import enrich, journey, reachable
    from app.config import settings
    from app.db.models import GtmContact
    from app.gtm.relationships.suppression import email_suppressed
    monkeypatch.setattr(enrich, "mx_status", lambda domain: "mx")
    facts = _facts()
    route = enrich.guessed_routes(facts, "consultancy.example", journey_db["wid"])[0]
    journey.update_icp(journey_db["wid"], journey_db["vid"], "Independent bakery owners")
    payload = _leased_payload(journey_db)
    lead = {"id": "person", "name": "Jane Patel", "company": "Engineering Consultants", "country": "IN", "fit": 85,
        "source_url": route["source_url"], "routes": [route], "judging_status": "judged", "why": "Drawing review workflow", "signals": []}
    reachable._publish(journey._journal(payload), payload, [lead])
    with journey_db["factory"]() as session:
        contact = session.scalar(select(GtmContact).where(GtmContact.email == route["email"]))
        assert contact.workspace_id == journey_db["wid"] and contact.email_confidence == "guess"
        data = json.loads(contact.enrichment_json)
        assert data["email_route"]["confidence"] == 80 and data["person_name"] == "Jane Patel"
        assert not email_suppressed(session, journey_db["wid"], contact)
        monkeypatch.setattr(settings, "guessed_emails", "off")
        assert email_suppressed(session, journey_db["wid"], contact)


@pytest.mark.hosted_only
def test_retired_pattern_not_reintroduced_as_guess_and_one_founder_cannot_retire(journey_db, monkeypatch):
    from app.gtm import email_learning, enrich
    from app.gtm.shared_models import SharedEmailPattern
    from core.clock import utcnow
    now = utcnow()
    with journey_db["factory"]() as session:
        for index in range(6):
            _event(session, f"sender-{index % 3}", "send", now, email=f"person.{index}@consultancy.example")
            _event(session, "one-bouncer", "hard_bounce", now, email=f"person.{index}@consultancy.example")
        email_learning.refresh(session, email_learning.opaque("person.0@consultancy.example"), "consultancy.example", "first.last", now=now)
        pattern = session.get(SharedEmailPattern, ("consultancy.example", "first.last"))
        assert pattern.retired_at is None
        pattern.retired_at = now
        session.commit()
    monkeypatch.setattr(enrich, "mx_status", lambda domain: "mx")
    facts = _facts()
    facts["people"] = facts["people"][1:]
    assert not enrich.guessed_routes(facts, "consultancy.example", journey_db["wid"])


@pytest.mark.hosted_only
def test_public_cache_seven_days_and_paid_hit_never_charges(journey_db, monkeypatch):
    from app.gtm import email_learning, journey
    from core.clock import utcnow
    now = utcnow()
    monkeypatch.setattr(email_learning, "utcnow", lambda: now)
    identity = ["structural engineering consultancies", 0, None]
    key = email_learning.cache_key("firecrawl", "firecrawl_search", identity)
    email_learning.cache_save(key, [{"url": "https://consultancy.example"}])
    journey.update_icp(journey_db["wid"], journey_db["vid"], "Independent bakery owners")
    payload = _leased_payload(journey_db)
    async def forbidden():
        pytest.fail("cache hit must not call the provider")
    result = asyncio.run(journey._signal_call(payload, "domain_search", identity[0], forbidden, meter="firecrawl_search"))
    assert result == [{"url": "https://consultancy.example"}]
    assert asyncio.run(journey._signal_call(payload, "domain_search", identity[0], forbidden, meter="firecrawl_search")) == result
    operation = journey._journal(payload).operation(payload["journey_id"], "domain_search-" + hashlib.sha256(identity[0].encode()).hexdigest()[:24])
    assert operation["cache_hit"] and operation["usage"] == {}
    monkeypatch.setattr(email_learning, "utcnow", lambda: now + timedelta(days=7))
    assert email_learning.cached(key) is None


@pytest.mark.hosted_only
def test_scrape_cache_preserves_public_metadata_but_not_usage(journey_db):
    from app.gtm import email_learning
    from app.signals.research import ScrapedText
    key = email_learning.cache_key("firecrawl", "firecrawl_scrape", "https://consultancy.example")
    text = ScrapedText("Published public page", metadata={"publishedTime": "2026-10-15", "statusCode": 200}, usage={"creditsUsed": 1})
    email_learning.cache_save(key, text)
    saved = email_learning.cached(key)
    assert str(saved) == str(text) and saved.metadata == text.metadata and saved.usage == {}


@pytest.mark.parametrize("text", ["Contact morgan.blake@orbitcomponents.example",
    '<a href="mailto:alex.rivers%40wardsystems.example">Alex Rivers</a>',
    "Contact morgan [at] practiceclub [dot] example",
    "https://orbitcomponents.example/contact?email=morgan.blake%40orbitcomponents.example",
    '<script>const contact = "alex.rivers@wardsystems.example";</script>',
    pytest.param("δοκιμή@café.example", id="unicode-address")])
def test_shared_public_cache_never_keeps_contact_addresses(journey_db, text):
    from app.gtm import email_learning
    from app.gtm.shared_models import PublicResearchCache
    from app.signals.research import ScrapedText
    from core.clock import utcnow

    for response in (text, {"snippet": text}, {text: "Public page"}, (text,), ScrapedText("Public page", metadata={"title": text}, usage={})):
        key = email_learning.cache_key("firecrawl", "firecrawl_scrape", response)
        email_learning.cache_save(key, response)
        with journey_db["factory"]() as session:
            assert session.get(PublicResearchCache, key) is None
            stored = {"cache_type": "scraped_text", "text": str(response), "metadata": response.metadata} if hasattr(response, "metadata") else response
            session.add(PublicResearchCache(cache_key=key, response_json=json.dumps(stored), saved_at=utcnow()))
            session.commit()
        assert email_learning.cached(key) is None


def test_uncertain_call_never_replays_even_with_cached_result(journey_db):
    from app.gtm import email_learning, journey
    journey.update_icp(journey_db["wid"], journey_db["vid"], "Independent bakery owners")
    payload = _leased_payload(journey_db)
    identity = "uncertain query"
    operation = "domain_search-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
    journey._journal(payload).save_operation(payload["journey_id"], operation, {"state": "uncertain", "usage": {}})
    email_learning.cache_save(email_learning.cache_key("firecrawl", "firecrawl_search", [identity, 0, None]), [])
    async def forbidden():
        pytest.fail("an uncertain paid call must never replay")
    assert asyncio.run(journey._signal_call(payload, "domain_search", identity, forbidden, meter="firecrawl_search")) is None


@pytest.mark.hosted_only
def test_paid_cache_reuses_one_receipt_across_changed_query_bucket(journey_db):
    from app.gtm import journey
    from app.db.models import CreditAccount
    journey.update_icp(journey_db["wid"], journey_db["vid"], "Independent bakery owners")
    payload = _leased_payload(journey_db)
    calls = []
    async def provider():
        calls.append(1)
        return [{"url": "https://consultancy.example/team"}]
    first = asyncio.run(journey._signal_call(payload, "signal_search", "engineering consultants", provider, meter="firecrawl_search"))
    with journey_db["factory"]() as session:
        spent = session.get(CreditAccount, journey_db["account"]).spent_micro
    assert spent > 0
    assert asyncio.run(journey._signal_call(payload, "domain_search", "engineering consultants", provider, meter="firecrawl_search")) == first
    with journey_db["factory"]() as session:
        assert session.get(CreditAccount, journey_db["account"]).spent_micro == spent
    assert calls == [1]


def test_public_pages_reuse_url_without_fetching_again(journey_db, monkeypatch):
    from app.gtm.reachable import PublicPages
    from app.gtm import scrapy_leads
    calls = []
    def fetch(url, *, allow_missing=False):
        calls.append(url)
        if url.endswith("/robots.txt"):
            assert allow_missing
            return "User-agent: *\nAllow: /"
        return "Published company team"
    monkeypatch.setattr(scrapy_leads, "_fetch", fetch)
    for _ in range(2):
        assert asyncio.run(PublicPages().scrape("https://consultancy.example/team")) == "Published company team"
    assert calls == ["https://consultancy.example/robots.txt", "https://consultancy.example/team"]


@pytest.mark.parametrize("reason,hard", [("550 5.1.1 mailbox does not exist", True), ("452 4.2.2 mailbox full", False), ("temporary rate limit", False)])
@pytest.mark.hosted_only
def test_worker_preserves_mailbox_nonexistence_classification(journey_db, monkeypatch, reason, hard):
    from app.gtm.supymem import workers, pipeline, composio_gmail
    from app.gtm.shared_models import SharedAddressValidity
    from app.gtm.email_learning import opaque
    from tests.unit.gtm.test_contact_work import _sent
    from tests.unit.gtm.test_journey import _mail
    cid = _sent(journey_db, monkeypatch)
    _mail(journey_db)
    status = "5.1.1" if hard else "4.2.2"
    monkeypatch.setattr(composio_gmail, "fetch_query", lambda **kwargs: {"messages": [{"id": "dsn-1",
        "body": f"Content-Type: message/delivery-status\n\nFinal-Recipient: rfc822; ADA@bakery.example\nAction: failed\nStatus: {status}\nDiagnostic-Code: smtp; {reason}"}]})
    result = workers.tick_bounces(journey_db["wid"])
    assert result["marked"] == [cid]
    with journey_db["factory"]() as session:
        row = session.get(SharedAddressValidity, opaque("ada@bakery.example"))
        assert row.hard_bounce_workspaces == int(hard)


@pytest.mark.hosted_only
def test_expired_retirement_requires_new_multi_founder_evidence(journey_db, monkeypatch):
    from app.gtm import email_learning
    from core.clock import utcnow
    now = utcnow()
    email = "jane.patel@consultancy.example"
    with journey_db["factory"]() as session:
        _event(session, "old-first", "hard_bounce", now - timedelta(days=91))
        _event(session, "old-second", "hard_bounce", now - timedelta(days=91))
        row = email_learning.refresh(session, email_learning.opaque(email), "consultancy.example", now=now - timedelta(days=91))
        assert row.status == "retired"
        _event(session, "new-first", "hard_bounce", now)
        row = email_learning.refresh(session, row.address_hash, "consultancy.example", now=now)
        assert row.status == "unknown"
        _event(session, "new-second", "hard_bounce", now)
        row = email_learning.refresh(session, row.address_hash, "consultancy.example", now=now)
        assert row.status == "retired"


def test_judged_lead_with_published_email_becomes_contact_and_review_draft(journey_db):
    from app.autopilot import outreach
    from app.db.models import GtmContact
    from app.gtm import journey, reachable
    facts = _facts()
    route = next(route for route in reachable.contact_routes(facts) if route.get("email") == "info@consultancy.example")
    journey.update_icp(journey_db["wid"], journey_db["vid"], "Independent bakery owners")
    payload = _leased_payload(journey_db)
    lead = {"id": "office", "name": "Engineering Consultants", "company": "Engineering Consultants", "country": "IN", "fit": 85,
        "source_url": route["source_url"], "routes": [route], "judging_status": "judged", "why": "Drawing review workflow", "signals": []}
    reachable._publish(journey._journal(payload), payload, [lead])
    with journey_db["factory"]() as session:
        contact = session.scalar(select(GtmContact).where(GtmContact.email == "info@consultancy.example"))
        assert contact.email_confidence == "risky" and contact.first_name is None
        assert json.loads(contact.enrichment_json)["email_route"] is None
    assert journey._draft_ready_admitted(journey_db["wid"], journey_db["vid"], review_only=True) == 1
    draft = next(d for d in outreach.drafts(journey_db["vid"]) if d["to"] == "info@consultancy.example")
    assert draft["channel"] == "email" and draft["requires_batch_approval"] and draft["body"].startswith("Hi Engineering Consultants team,")  # no named person: greet the company

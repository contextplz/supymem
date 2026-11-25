"""Real durable GTM state and approval/metering; provider network is replaced."""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select

from tests.company_os_harness import configure_local_gtm, load_company_os_app


@pytest.mark.parametrize("audience", ["Freight dispatch coordinators", "Parents choosing swimming lessons"])
def test_contact_permission_button_says_what_it_checks_without_changing_the_action(journey_db, audience):
    from app.gtm import journey

    result = journey._next_step(journey_db["vid"], {"icp": {"who": audience}, "prospects": [
        {"id": "contact-permission", "name": audience, "eligible": False, "blocked_reason": "unknown_jurisdiction"}]})
    assert result["reason"] == "permission_required"
    assert result["actions"] == [{"label": "Check if you can email this person", "action": "gtm.eligibility",
        "payload": {"contact_id": "contact-permission"}, "style": "primary"}]


@pytest.fixture()
def journey_db(tmp_path, monkeypatch, request):
    async def no_registry_network(*args, **kwargs):
        return []
    async def no_reader_network(*args, **kwargs):
        from app.signals.research import ScrapedText
        return ScrapedText("", metadata={}, usage={})
    monkeypatch.delenv("COMPANIES_HOUSE_API_KEY", raising=False)
    for key in ("FIRECRAWL_API_KEY", "EXA_API_KEY", "TAVILY_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GTM_ENRICH_ENABLED", "0")
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/journey.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("SUPYMEM_OUTBOUND_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("CHANNELS_DRY_RUN", "false")
    load_company_os_app()
    from app.core.local_mode import is_local
    configure_local_gtm(monkeypatch)
    from app.signals.research import DuckDuckGoProvider, HttpResearchProvider, StructuredBusinessProvider
    monkeypatch.setattr(StructuredBusinessProvider, "search", no_registry_network)
    monkeypatch.setattr(DuckDuckGoProvider, "search", no_registry_network)
    if is_local() and "builtin_http_transport" not in request.fixturenames:
        monkeypatch.setattr(HttpResearchProvider, "search", no_registry_network)
        monkeypatch.setattr(HttpResearchProvider, "scrape", no_reader_network)
    monkeypatch.setattr("app.gtm.scrapy_leads._fetch", lambda url: "")
    from app.authz.workspace import ensure_subject_person
    from app.autopilot import outreach
    from app.credits.accounts import ensure_account
    from app.credits.policy import set_policy
    from app.db.models import CreditAccount, Venture
    from app.state import db
    from app.tenancy import bind
    from app.workspaces.service import create_workspace
    from app.core.local_owner import ensure_owner
    from core.clock import utcnow
    if is_local():
        from secrets import token_urlsafe
        monkeypatch.setenv("SUPYMEM_PUBLIC_URL", "https://your-instance.example")
        monkeypatch.setenv("OUTREACH_UNSUB_SECRET", token_urlsafe(32))
    actor = ensure_owner().id if is_local() else "usr_gtm_journey"
    ensure_subject_person(actor)
    workspace = create_workspace(kind="company", name="Journey fixture", owner_id=actor)
    now = utcnow()
    with db()() as session:
        session.add(Venture(id="ven_journey", workspace_id=workspace.id, name="Order board", raw_idea="Keep bakery orders in one place", stage="draft", created_at=now, updated_at=now))
        session.commit()
    account = None
    if not is_local():
        account = ensure_account("ven_journey", workspace_id=workspace.id, owner_person_id=actor)
        if account is not None:
            with db()() as session:
                session.get(CreditAccount, account.id).granted_micro = 10_000_000
                session.commit()
    set_policy("outbound_provider_rates", {"version": "test-gmail-v1", "rates": {"gtm_email_send:composio_gmail": {
        "action": "gtm_email_send", "provider": "composio_gmail", "price_usd": "0.01", "currency": "USD", "source": "Offline test contract"}}}, "test")
    monkeypatch.setattr(outreach, "_brief", lambda vid: {"idea": {"audience": "Independent bakery owners", "problem": "bakery orders phone", "confirmed": True}, "research": {"competitors": ["OldBoard"]}})
    from app.gtm import journey
    stored_seed = journey._icp_seed
    monkeypatch.setattr(journey, "_icp_seed", lambda vid: outreach._brief(vid))
    with bind(workspace_id=workspace.id, venture_id="ven_journey", actor=actor):
        yield {"wid": workspace.id, "vid": "ven_journey", "actor": actor, "factory": db(), "account": account.id if account is not None else None, "stored_seed": stored_seed, "monkeypatch": monkeypatch}


def _assert_credit_usage(data, session, *, spent_micro, reserved_micro=None):
    from app.db.models import CreditAccount

    if data["account"] is None:
        assert session.scalar(select(CreditAccount.id)) is None
        return
    account = session.get(CreditAccount, data["account"])
    assert account.spent_micro == spent_micro
    if reserved_micro is not None:
        assert account.reserved_micro == reserved_micro


@pytest.mark.parametrize("audience,idea,story,excluded", [
    ("commuting students in Japan", "A meal planning app for commuting students in Japan. Students struggle to schedule dinners.",
     "My sister dismissed 'silver lanyard' yesterday and missed supper twice.",
     ("sister", "dismissed", "silver", "lanyard", "yesterday", "supper", "twice")),
    ("freight coordinators in Kenya", "A dispatch tool for freight coordinators in Kenya. Freight coordinators struggle to manage delivery schedules.",
     "My client discarded 'ruby docket' yesterday and lost a freight booking twice.",
     ("client", "discarded", "ruby", "docket", "yesterday", "twice")),
])
def test_saved_anecdote_icp_recovers_searches_without_rewriting_history(journey_db, monkeypatch, audience, idea, story, excluded):
    import json
    from app.db.models import Venture, VentureDocument
    from app.gtm import journey
    from app.ventures import founder_facts, startup_review

    data = journey_db
    expected = startup_review.problem_prefill(idea)
    monkeypatch.setattr(journey, "_icp_seed", lambda vid: {"idea": {"audience": audience, "problem": story, "confirmed": True}})
    saved = {"who": audience, "problem": story, "problem_basis": "From the problem you entered.",
        "confirmed": True, "chat_confirmed": True, "competitors": [], "search_queries": [f'{audience} "{story}"']}
    with data["factory"]() as session:
        venture = session.get(Venture, data["vid"])
        venture.raw_idea = idea
        startup_review._document(session, venture.id, "internal_new_user_flow", {"enabled": True})
        original_facts = {**founder_facts.empty(), "audience": founder_facts._fact(audience),
            "problem": founder_facts._fact(story, "clarification")}
        startup_review._document(session, venture.id, startup_review.INTERNAL_KIND, {"founder_facts": original_facts})
        row = journey._row(session, venture.id, create=True)
        original = json.dumps({"state": "failed", "icp": saved,
            "discovery_plan": [{"query": saved["search_queries"][0], "intent": "customer_voice"}],
            "expert_discovery_plan": [{"query": f'{story} educator contact booking', "intent": "person"}]})
        row.provenance_json = original
        session.commit()
    base = journey._icp(data["vid"])
    original_base = dict(base)
    for invalid in (story, f"Find me {audience} I can email"):
        restored = journey._journey_icp(data["vid"], saved | {"problem": invalid}, base=base)
        assert restored["problem"] == expected
        assert restored["search_queries"] == journey._search_queries(audience, expected, [], context=restored)
        context = {"idea": idea, "problem": invalid}
        queries = list(restored["search_queries"])
        queries += journey._search_queries(audience, invalid, context=context)
        queries += [item["query"] for item in journey._fallback_plan(audience, invalid, [], context=context)]
        queries += journey._search_queries(audience, invalid)
        queries += [item["query"] for item in journey._fallback_plan(audience, invalid, [])]
        assert all(word not in " ".join(queries).casefold() for word in excluded)
        assert invalid.casefold() not in " ".join(queries).casefold()
        assert journey._search_queries(audience, invalid, context=context) == journey._search_queries(audience, expected, context=context)
        assert journey._fallback_plan(audience, invalid, [], context=context) == journey._fallback_plan(audience, expected, [], context=context)
    restored = journey.status(data["wid"], data["vid"])["icp"]
    assert restored["problem"] == expected
    assert all(word not in " ".join(restored["search_queries"]).casefold() for word in excluded)
    assert base == original_base and saved["problem"] == story
    with data["factory"]() as session:
        assert journey._row(session, data["vid"]).provenance_json == original
        # ae6e532af filters the effective facts without rewriting the stored ones.
        document = session.get(VentureDocument, f"{startup_review.INTERNAL_KIND}:{data['vid']}")
        assert json.loads(document.body_md)["founder_facts"] == original_facts


@pytest.mark.parametrize("answer", ["Not sure", "I don't know", "unsure", "n/a", "skip", "-", ""])
@pytest.mark.parametrize("audience_source", ["founder_facts", "labelled", "both"])
def test_unknown_audience_never_prefills_icp_or_labelled_problem(journey_db, monkeypatch, answer, audience_source):
    from app.db.models import Venture
    from app.gtm import journey
    from app.ventures import founder_facts, startup_review

    data = journey_db
    monkeypatch.setattr(journey, "_icp_seed", lambda vid: {"idea": {"audience": "Independent bakery owners"}})
    with data["factory"]() as session:
        venture = session.get(Venture, data["vid"])
        venture.raw_idea = "A tool that tracks flour inventory"
        if audience_source != "founder_facts":
            venture.raw_idea += f"\nAudience: {answer}"
        startup_review._document(session, venture.id, "internal_new_user_flow", {"enabled": True})
        session.flush()
        facts = founder_facts.empty()
        for field in ("audience", "problem"):
            if field == "audience" and audience_source == "labelled":
                continue
            facts[field] = {"value": answer, "source": "founder message", "quote": answer, "confidence": .95}
        startup_review._document(session, venture.id, startup_review.INTERNAL_KIND, {"founder_facts": facts})
        session.commit()
        assert founder_facts.read(session, venture)["audience"]["value"] is None
        assert founder_facts.policy(facts, "problem") == "ask"
        assert "audience" not in founder_facts.literal_facts(venture.raw_idea)
    icp = journey._icp(data["vid"])
    assert icp["who"] == ""
    from app.core.local_mode import is_local
    assert icp["suggested_who"] == ("" if is_local() else "Independent bakery owners")
    # 61ca4e4c7: a sourced problem can be shown without authorizing a search for an unknown audience.
    assert icp["problem"] == "A tool that tracks flour inventory"
    assert icp["needs_input"]
    assert not icp["search_queries"]
    saved = journey._journey_icp(data["vid"], {**icp, "who": answer,
        "problem": f"A tool that tracks flour inventory\nAudience: {answer}", "confirmed": True}, base=icp)
    assert saved["who"] == "" and "Audience:" not in saved["problem"]
    with pytest.raises(ValueError, match="gtm_invalid_input"):
        journey.update_icp(data["wid"], data["vid"], answer)


def test_unanswered_audience_prefills_inferred_seed(journey_db, monkeypatch):
    from app.db.models import Venture
    from app.gtm import journey
    from app.ventures import founder_facts, startup_review

    data = journey_db
    audience = "Independent dental practices in the UK"
    monkeypatch.setattr(journey, "_icp_seed", lambda vid: {"idea": {"audience": audience}})
    with data["factory"]() as session:
        venture = session.get(Venture, data["vid"])
        venture.raw_idea = "A tool that tracks dental inventory"
        startup_review._document(session, venture.id, "internal_new_user_flow", {"enabled": True})
        startup_review._document(session, venture.id, startup_review.INTERNAL_KIND, {"founder_facts": founder_facts.empty()})
        session.commit()
    icp = journey._icp(data["vid"])
    from app.core.local_mode import is_local
    assert icp["who"] == ("" if is_local() else audience)
    assert icp["suggested_who"] == ""
    if is_local():
        assert icp["needs_input"]
        assert icp["known_checks"]["audience"]["status"] == "missing"


def _add(d, monkeypatch, *, country="CA", basis="consent", email="ada@bakery.example", name="Ada"):
    from app.gtm import journey
    monkeypatch.setattr(journey.outreach, "site_url", lambda vid: None)
    journey.add_prospect(d["wid"], d["vid"], {"name": name, "company": "Ada Bakery", "email": email,
        "source_url": "https://bakery.example/contact", "country": country, "lawful_basis": basis,
        "lawful_basis_note": "The contact explicitly agreed to hear about the bakery order board."})
    journey._draft_ready(d["wid"], d["vid"])
    return journey.status(d["wid"], d["vid"], authorize=False)


@pytest.mark.parametrize("audience", ["Factory dispatch coordinators", "Families planning home gardens"])
def test_contact_permission_does_not_start_unrequested_drafts(journey_db, monkeypatch, audience):
    from app.autopilot import outreach
    from app.gtm import journey

    data = journey_db
    journey.update_icp(data["wid"], data["vid"], audience, problem="Coordinate your plans", start=False)
    _add(data, monkeypatch, country="", basis="")
    contact = outreach.contacts(data["vid"])[0]
    before = outreach.drafts(data["vid"])
    calls = []
    monkeypatch.setattr(journey, "_draft_ready", lambda *args: calls.append(args))
    change = {"country": "CA", "lawful_basis": "consent", "lawful_basis_note": "You have permission for this research email."}
    journey.patch_prospect(data["wid"], data["vid"], contact.id, change)
    journey.confirm_prospects(data["wid"], data["vid"], [{"contact_id": contact.id, **change}])
    assert calls == [] and outreach.drafts(data["vid"]) == before
    saved = outreach.contacts(data["vid"])[0]
    assert saved.recipient_geo == "CA" and saved.lawful_basis == "consent"
    assert saved.lawful_basis_note == change["lawful_basis_note"]


@pytest.fixture
def local_journey_db(monkeypatch, request):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    return request.getfixturevalue("journey_db")


@pytest.mark.parametrize("domain", ["industrial-pumps.example", "home-gardening.example"])
@pytest.mark.parametrize("configured", [False, True])
def test_local_ask_sending_applies_owner_switch_without_platform_request(local_journey_db, monkeypatch, domain, configured):
    from secrets import token_urlsafe
    from app.core.local_capabilities import blocked_message, enabled
    from app.core.local_secrets import read_settings, update_settings
    from app.gtm import journey, local_mail
    from app.needs_you import service

    data = local_journey_db
    monkeypatch.setenv("SUPYMEM_OUTBOUND_EXECUTION_ENABLED", "false")
    monkeypatch.setattr(service, "_deliver", lambda *args, **kwargs: None)
    service.request_human(data["vid"], reason_code="gtm_sending_off", title="Your earlier sending request",
        what_we_need="Your sending setup needs attention.", how_to_resume="Open Settings > Keys and connections.",
        blocking_ref="gtm-sending-off", chat_card=False)
    monkeypatch.setattr(service, "request_human", lambda *args, **kwargs: pytest.fail("You do not need platform approval."))
    monkeypatch.setattr(local_mail, "send", lambda **kwargs: pytest.fail("Enabling sending must not send anything."))
    update_settings("capabilities", {"send_email": False}, {})
    if configured:
        update_settings("smtp", {"host": f"smtp.{domain}", "user": f"you@{domain}",
            "sender_name": "Your sender", "postal_address": "10 Market Street, Toronto, Canada"},
            {"password": token_urlsafe(24)})
    for _attempt in range(2):
        result = journey.ask_sending(data["wid"], data["vid"])
        assert read_settings("capabilities")["values"]["send_email"] is True
        assert enabled("send_email") is configured
        assert service.list_open(data["vid"]) == []
        assert result["receipt"]["kind"] == "text"
        assert result["receipt"]["body_md"] == ("You turned on email sending. Open Find customers and choose the saved emails you want to send. Nothing was sent."
            if configured else blocked_message("send_email"))


@pytest.mark.parametrize("audience", ["Industrial quality inspectors", "Families sharing recipes"])
def test_local_journey_fixture_configures_owned_mailbox(local_journey_db, audience):
    from app.connections.mailboxes import resolve_send_mailbox
    from app.core.local_capabilities import enabled
    from app.core.local_owner import ensure_owner
    from app.db.models import Venture, Workspace
    from app.gtm.local_mail import configured
    from app.gtm.supymem import composio_gmail
    from app.gtm.supymem.composio_gmail import DefiniteFailure, user_id_for

    data = local_journey_db
    _mail(data)
    assert configured()
    assert enabled("send_email") and enabled("read_replies")
    assert composio_gmail.get_gmail_profile(user_id_for(data["wid"]), "ca_fixture")["email"] == "founder@gmail.example"
    with pytest.raises(DefiniteFailure):
        composio_gmail.get_gmail_profile("another-user", "ca_fixture")
    with data["factory"]() as session:
        session.get(Venture, data["vid"]).raw_idea = audience
        assert session.get(Workspace, data["wid"]).owner_id == ensure_owner().id
        assert resolve_send_mailbox(session, data["wid"], venture_id=data["vid"])["mailbox_id"] == "mail_journey"


@pytest.mark.parametrize("audience,address", [
    ("Industrial quality inspectors", "18 Foundry Road, Toronto, Canada"),
    ("Families sharing recipes", "29 Orchard Lane, Toronto, Canada"),
])
def test_local_sending_address_update_is_persisted(local_journey_db, audience, address):
    from app.core.local_secrets import read_settings
    from app.db.models import Venture
    from app.gtm import journey
    from app.gtm.local_mail import settings

    data = local_journey_db
    _mail(data)
    with data["factory"]() as session:
        session.get(Venture, data["vid"]).raw_idea = audience
        session.commit()
    state = journey.update_settings(data["wid"], data["vid"], {"sender_postal_address": address})
    assert state["sender_postal_address"] == address
    assert read_settings("smtp")["values"]["postal_address"] == address
    assert settings().postal_address == address


def _mail(d, *, sending_address=True):
    from secrets import token_urlsafe
    from app.autopilot import outreach
    from app.core.local_mode import is_local
    from app.db.models import GtmEmailAccount
    from app.gtm.supymem.composio_gmail import user_id_for
    from core.clock import utcnow
    if is_local():
        from app.core.local_secrets import read_settings, update_settings
        from app.gtm import local_mail
        from app.core.local_capabilities import enabled

        monkeypatch = d["monkeypatch"]
        postal = (read_settings("smtp")["values"].get("postal_address") or "1 Founder Street, New York, NY 10001") if sending_address else ""
        update_settings("smtp", {"host": "127.0.0.1", "port": 587, "user": "founder@gmail.example",
            "sender_name": outreach.founder_signature(d["vid"]), "imap_host": "127.0.0.1", "imap_port": 993,
            "postal_address": postal}, {"password": token_urlsafe(24)})
        update_settings("capabilities", {"send_email": True, "read_replies": True}, {})
        monkeypatch.setattr(local_mail, "enabled", enabled)
        monkeypatch.setattr(local_mail, "_smtp", lambda *_args: pytest.fail("Replace your SMTP transport in this test before sending."))
        monkeypatch.setattr(local_mail, "_imap", lambda *_args: pytest.fail("Replace your IMAP transport in this test before reading."))

        def profile(user_id, connected_account_id):
            local_mail.check_binding(user_id, connected_account_id)
            return {"email": local_mail.settings().username, "raw": {}}

        monkeypatch.setattr("app.gtm.supymem.composio_gmail.get_gmail_profile", profile)
    with d["factory"]() as session:
        session.add(GtmEmailAccount(id="mail_journey", workspace_id=d["wid"], provider="smtp" if is_local() else "gmail", email="founder@gmail.example", composio_user_id=user_id_for(d["wid"]) if is_local() else "user_fixture", app_user_id=user_id_for(d["wid"]) if is_local() else None, connected_account_id="ca_fixture", status="active", created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    if not sending_address:
        return
    from app.gtm.relationships.email_compliance import complete_body, sender_address
    from app.gtm.relationships.service import _settings
    with d["factory"]() as session:
        postal = sender_address(session, d["wid"])
        _settings(session, d["wid"]).sender_postal_address = postal or "1 Founder Street, New York, NY 10001"
        session.commit()
        from app.db.models import GtmContact
        for draft in outreach.drafts(d["vid"], "draft"):
            contact = session.get(GtmContact, draft["contact_id"])
            body = complete_body(session, d["wid"], contact, draft["body"], outreach.founder_signature(d["vid"]))
            outreach._save_draft(d["vid"], draft | {"body": body, "review_state": "draft"}, draft["doc_id"])


def test_consumer_prefills_prefer_typed_problem_and_specific_founder_audience(journey_db):
    import json
    from app.db.models import Venture
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.gtm import journey
    from app.ventures import startup_review
    from core.clock import utcnow

    data = journey_db
    now = utcnow()
    with data["factory"]() as session:
        venture = session.get(Venture, data["vid"])
        venture.raw_idea = "PupilLog QA: an app for first-time parents in the US logging school reading"
        startup_review._document(session, venture.id, "internal_new_user_flow", {"enabled": True})
        session.add(IdeaSession(id="pupillog_session", workspace_id=data["wid"], venture_id=venture.id,
            case="know", state="confirmed", current_revision_id="pupillog_revision", confirmed_revision_id="pupillog_revision",
            created_at=now, updated_at=now))
        session.flush()
        session.add(IdeaRevision(id="pupillog_revision", session_id="pupillog_session", workspace_id=data["wid"],
            venture_id=venture.id, rev=1, source="agent", summary="School reading log", created_at=now,
            structured=json.dumps({"customer": "Assumed: new parents", "problem": "Assumed: New parents experience anxiety",
                                   "offer": "School reading log"})))
        session.commit()
    current = startup_review.get(data["vid"])
    assert current["question"]["field"] == "problem"
    saved = startup_review.save_answer(data["vid"], current["revision"], "problem", "Parents lose track of assigned books between households")
    assert saved["question"]["field"] == "progress"
    icp = journey._icp(data["vid"])
    assert icp["problem"] == "Parents lose track of assigned books between households"
    assert icp["who"] == "first-time parents in the US logging school reading"
    assert icp["mode"] == "consumer"
    refreshed = journey._journey_icp(data["vid"], {**icp, "problem": "Assumed: anxiety", "who": "new parents",
        "problem_basis": "Confirmed idea"}, base=icp)
    assert refreshed["problem"] == icp["problem"] and refreshed["who"] == icp["who"]
    explicit = journey._journey_icp(data["vid"], {**icp, "problem": "Founder-edited pain", "who": "Parents in Ohio",
        "problem_basis": "Founder supplied"}, base=icp)
    assert explicit["problem"] == "Founder-edited pain" and explicit["who"] == "Parents in Ohio"
    confirmed = journey._journey_icp(data["vid"], {**explicit, "problem_basis": "Your saved idea", "confirmed": True}, base={**icp, "problem_basis": "Your saved idea"})
    assert confirmed["problem"] == "Founder-edited pain" and confirmed["who"] == "Parents in Ohio"
    corrected = journey._journey_icp(data["vid"], {**confirmed}, base=icp)
    assert corrected["problem"] == icp["problem"] and corrected["who"] == icp["who"]


@pytest.mark.parametrize("candidates", [
    ["8/month.", "PupPath: an app for first-time US dog owners who struggle to keep up with puppy training; 5-minute daily video lessons and a progress tracker, $8/month."],
    ["Dog owners struggle with training.", "PupPath: an app for first-time US dog owners who struggle to keep up with puppy training; 5-minute daily video lessons and a progress tracker, $8/month."],
])
def test_u37_problem_prefill_prefers_the_fullest_description(journey_db, candidates):
    from app.ventures.startup_review import problem_prefill
    assert "first-time US dog owners" in problem_prefill(*candidates)
    assert problem_prefill("8/month.") == ""


@pytest.mark.parametrize("website", ["https://supymem.com", "supymem.com"])
def test_u37_problem_prefill_does_not_parse_urls_or_colon_lists_as_pain(journey_db, website):
    from app.ventures.startup_review import problem_prefill
    description = f"Our website is {website}. Supymem is a Mac app for first-time startup founders: it clarifies the idea, researches the market, finds first customers and drafts outreach so they book their first customer call. Pricing: $49/month per founder."
    result = problem_prefill(description)
    assert result == ""
    assert not any(fragment in result.lower() for fragment in ("//", "supymem.com", "is a mac."))
    assert problem_prefill(description, "A solo technical founder has a prototype but zero customer calls booked.") == "A solo technical founder has a prototype but zero customer calls booked."
    assert problem_prefill(f"Visit {website}. Founders struggle to book customer calls.") == "Founders struggle to book customer calls."
    assert problem_prefill("PupPath: puppy-training app for first-time dog owners: daily lessons and a progress tracker.") == "First-time dog owners need help with puppy training."
    assert problem_prefill("Pricing: $49/month per founder.") == ""
    assert problem_prefill("Bakery owners are unsure how much flour is left.", stated=True) == "Bakery owners are unsure how much flour is left."
    for label in ("Audience", "Clarification", "Customer", "Target audience", "Workflow", "Problem"):
        assert problem_prefill(f"A tool that tracks flour inventory\n{label}: Not sure") == "A tool that tracks flour inventory"
    assert problem_prefill("Supymem is an app for founders who need help clarifying ideas, researching markets and drafting their first customer outreach.", "A solo technical founder has a prototype but zero customer calls booked.") == "A solo technical founder has a prototype but zero customer calls booked."


@pytest.mark.parametrize("description,audience,pain", [
    ("An app for independent repair workshops: it tracks equipment service requests.",
     "Independent repair workshops", "Repair workshop owners lose service requests between paper logs."),
    ("An app for home gardeners: it tracks plant watering schedules.",
     "Home gardeners", "Home gardeners struggle to keep watering schedules across their plants."),
])
def test_u37_problem_prefill_prefers_gstack_pain_over_the_first_message(journey_db, monkeypatch, description, audience, pain):
    import json
    from app.db.models import Venture
    from app.db.supymem_models import ClarifierTurn, IdeaRevision, IdeaSession
    from app.gtm import journey
    from app.ventures import startup_review
    from core.clock import utcnow

    data, now = journey_db, utcnow()
    monkeypatch.setattr(journey, "_icp_seed", lambda vid: {})
    with data["factory"]() as session:
        venture = session.get(Venture, data["vid"])
        venture.raw_idea = description
        startup_review._document(session, venture.id, "internal_new_user_flow", {"enabled": True})
        session.add(IdeaSession(id="supymem_session", workspace_id=data["wid"], venture_id=venture.id,
            case="know", state="confirmed", current_revision_id="supymem_revision", confirmed_revision_id="supymem_revision",
            created_at=now, updated_at=now))
        session.flush()
        session.add(IdeaRevision(id="supymem_revision", session_id="supymem_session", workspace_id=data["wid"],
            venture_id=venture.id, rev=1, source="agent", structured=json.dumps({"offer": description, "customer": audience}), created_at=now))
        session.add(ClarifierTurn(id="supymem_question", session_id="supymem_session", workspace_id=data["wid"],
            venture_id=venture.id, turn=1, role="agent", kind="question", text="What pain have you seen?",
            options=json.dumps({"phase": "gstack", "gstack_id": "Q1", "gstack_name": "Demand evidence"}), created_at=now))
        session.add(ClarifierTurn(id="supymem_answer", session_id="supymem_session", workspace_id=data["wid"],
            venture_id=venture.id, turn=2, role="founder", kind="answer", text=pain, created_at=now))
        session.commit()
    assert startup_review.get(data["vid"])["question"]["prefill"] == pain
    from app.core.local_mode import is_local
    icp = journey._icp(data["vid"])
    assert icp["problem"] == pain
    if is_local():
        assert not icp["confirmed"]


def test_u37_prefills_use_scoped_founder_messages_not_only_creation_text(journey_db, monkeypatch):
    import json
    from app.db.models import Venture
    from app.db.supymem_models import ClarifierTurn, IdeaRevision, IdeaSession
    from app.gtm import journey
    from app.ventures import startup_review
    from core.clock import utcnow

    data, now = journey_db, utcnow()
    description = "PupPath: an app for first-time US dog owners who struggle to keep up with puppy training; 5-minute daily video lessons and a progress tracker, $8/month."
    monkeypatch.setattr(journey, "_icp_seed", lambda vid: {})
    with data["factory"]() as session:
        venture = session.get(Venture, data["vid"])
        venture.raw_idea = "8/month."
        startup_review._document(session, venture.id, "internal_new_user_flow", {"enabled": True})
        session.add(IdeaSession(id="puppath_session", workspace_id=data["wid"], venture_id=venture.id,
            case="know", state="confirmed", current_revision_id="puppath_revision", confirmed_revision_id="puppath_revision",
            created_at=now, updated_at=now))
        session.flush()
        session.add(IdeaRevision(id="puppath_revision", session_id="puppath_session", workspace_id=data["wid"],
            venture_id=venture.id, rev=1, source="agent", structured=json.dumps({"offer": "Puppy training", "customer": "First-time US dog owners"}), created_at=now))
        for index, (role, kind, workspace_id, text) in enumerate([
            ("founder", "answer", data["wid"], "8/month."),
            ("founder", "answer", data["wid"], description),
            ("agent", "question", data["wid"], "Parents struggle with sleep. " * 20),
            ("founder", "answer", "other_workspace", "Bakeries struggle with orders. " * 20),
            ("founder", "chip", data["wid"], "Coffee owners struggle with beans. " * 20),
        ], 1):
            session.add(ClarifierTurn(id=f"puppath_turn_{index}", session_id="puppath_session", workspace_id=workspace_id,
                venture_id=venture.id, turn=index, role=role, kind=kind, text=text, created_at=now))
        session.commit()
    current = startup_review.get(data["vid"])
    assert current["question"]["field"] == "problem"
    assert "first-time US dog owners" in current["question"]["prefill"]
    assert not any(text in current["question"]["prefill"] for text in (
        "Parents struggle with sleep", "Bakeries struggle with orders", "Coffee owners struggle with beans"))
    from app.core.local_mode import is_local
    icp = journey._icp(data["vid"])
    assert icp["problem"] == current["question"]["prefill"]
    if is_local():
        assert not icp["confirmed"]
    else:
        assert icp["problem_basis"] == "From your saved description."


@pytest.mark.parametrize("clarification,seed_problem,expected_basis", [
    ("", "", "From your saved description."),
    ("8/month.", "", "From your saved description."),
    ("Dog owners struggle to keep up with puppy training.", "", "From your clarification answer."),
    ("", "Bakeries lose phone orders", "From your saved idea."),
])
def test_u30_basis_matches_the_problem_actually_prefilled(journey_db, monkeypatch, clarification, seed_problem, expected_basis):
    from app.core.local_mode import is_local
    from app.db.models import Venture
    from app.gtm import journey
    from app.ventures import startup_review

    data = journey_db
    confirmed_problem = startup_review.problem_prefill(clarification, stated=True) or seed_problem
    monkeypatch.setattr(journey, "_icp_seed", lambda vid: {"idea": {"problem": seed_problem, "confirmed": True}})
    with data["factory"]() as session:
        venture = session.get(Venture, data["vid"])
        venture.raw_idea = "Order-tracking app for independent bakery owners"
        startup_review._document(session, venture.id, "internal_new_user_flow", {"enabled": True})
        session.commit()
        if is_local() and confirmed_problem:
            from app.ventures.founder_facts import capture
            capture(session, venture, confirmed_problem, source="clarification", field="problem")
            session.commit()
        context = startup_review.review(session, venture)
    context["checklist"]["problem"].update(value=clarification, source="Your clarification")
    monkeypatch.setattr(startup_review, "review", lambda session, venture: context)
    icp = journey._icp(data["vid"])
    if is_local():
        assert icp["problem"] == (confirmed_problem or "Independent bakery owners need help with order tracking.")
        assert icp["problem_basis"] == ("From your saved context." if confirmed_problem else expected_basis)
        if not confirmed_problem:
            assert not icp["confirmed"]
    else:
        assert icp["problem"]
        assert icp["problem_basis"] == expected_basis


def test_consumer_search_cost_note_is_plain_and_keeps_credit_ceiling(journey_db):
    from app.gtm import journey
    data = journey_db
    view = journey.status(data["wid"], data["vid"])
    assert "credit" not in view["estimate"]["basis"].lower()
    assert "Nothing is posted or sent." in view["estimate"]["basis"]
    assert view["estimate"]["ceiling_micro"] == journey._search_estimate(view["icp"])


@pytest.mark.parametrize("founder,name,audience,problem", [
    ("Mira", "Stock ledger", "Warehouse purchasing teams", "Reconcile inventory invoices"),
    ("Noah", "Trail circles", "Families planning weekend walks", "Find accessible walking routes"),
])
def test_ready_drafts_open_saved_review_for_unrelated_founders(journey_db, monkeypatch, founder, name, audience, problem):
    import json
    from types import SimpleNamespace
    from app.autopilot import outreach
    from app.chat import customers
    from app.credits import metered
    from app.db.models import Venture
    from app.gtm import journey

    data = journey_db
    with data["factory"]() as session:
        venture = session.get(Venture, data["vid"])
        venture.name, venture.raw_idea = name, f"Offer: {problem}. Problem: {problem}. . Audience: {audience}"
        row = journey._row(session, data["vid"], create=True)
        row.provenance_json = json.dumps({"playbooks": [{"key": "problem_interview"}]})
        session.commit()
    icp = journey._icp(data["vid"]) | {"who": audience, "problem": problem, "confirmed": True}
    monkeypatch.setattr(journey, "_icp", lambda _: dict(icp))
    for index, recipient in enumerate(("Ari", "Lee", "Remy")):
        assert journey._ingest(data["wid"], data["vid"], [{"organization": audience, "first_name": recipient,
            "source_url": f"https://recipients{index}.example/contact", "email": f"{recipient.lower()}@recipients{index}.example",
            "country": "US", "enriched": True, "evidence_label": "fit_only"}]) == 1
    _mail(data)
    monkeypatch.setattr(outreach, "founder_signature", lambda _: founder)
    calls = []
    pitch_calls = []
    async def modeled(messages, **kwargs):
        if kwargs["purpose"] == "venture.chat.assistant":
            pitch_calls.append(messages)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content=f"a tool to {problem.lower()} for {audience.lower()}"))])
        assert kwargs["purpose"] == "outreach.draft_batch"
        template = json.loads(messages[-1]["content"])["template"]
        calls.append(template)
        body = template + "\n\nI would value hearing what you do today before deciding what to build next."
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"subject": "A short research question", "body": body})))])
    monkeypatch.setattr(metered, "llm", modeled)
    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.delenv("LLM_CASSETTE_MODE", raising=False)
    result = journey.draft_for_review(data["vid"])
    assert "3 drafts ready for review" in result["body_md"] and "Nothing was sent." in result["body_md"]
    assert all(recipient in result["body_md"] for recipient in ("Ari", "Lee", "Remy"))
    assert len(calls) == 3
    assert len(pitch_calls) == 1
    assert result["actions"] == [{"label": "Review 3 drafts", "action": "gtm.status",
                                  "payload": {"show_saved": True}, "style": "primary"}]
    action = result["actions"][0]
    review = asyncio.run(customers.execute(data["vid"], action["action"], action["payload"], data["actor"]))
    drafts = outreach.drafts(data["vid"], "draft", "manual")
    assert len(drafts) == 3
    for draft in drafts:
        assert draft["body"] in review["body_md"] and draft["subject"] in review["body_md"]
        assert name in draft["body"] and founder in draft["body"]
        assert draft["contact_id"] and draft["to"] and draft["source"] == "model"
        assert all(bad not in draft["body"] for bad in ("Offer:", "Problem:", ". .", f"The {name} team"))
        assert "Your email address is listed publicly on" in draft["body"]
        assert outreach.OPT_OUT in draft["body"]
        assert draft["review_state"] == "draft" and not draft.get("sent_at")
    assert "**Email drafts**" in review["body_md"]
    assert not any(item.get("action") == "gtm.send" for item in review["actions"])


@pytest.mark.parametrize("name,idea", [("Invoice desk", "Reconcile vendor invoices for factories"),
                                     ("Walk together", "Plan accessible walks for families")])
def test_no_people_means_find_customers_not_recipientless_drafts(journey_db, name, idea):
    from app.autopilot import outreach
    from app.db.models import Venture
    from app.gtm import journey
    from app.needs_you import readiness

    data = journey_db
    with data["factory"]() as session:
        venture = session.get(Venture, data["vid"])
        venture.name, venture.raw_idea = name, idea
        session.commit()
    outreach._save_draft(data["vid"], {"to": "", "subject": "Legacy draft", "body": "Hi there",
        "state": "draft", "suggestion_draft": True, "awaiting_recipient": True})
    result = journey.draft_for_review(data["vid"])
    assert result["body_md"].startswith("No people to write to yet.")
    assert result["actions"] == [{"label": "Find customers", "action": "gtm.find", "payload": {"prepare": True}, "style": "primary"}]
    state = journey.status(data["wid"], data["vid"])
    assert state["drafts"] == [] and readiness._ready_drafts(data["vid"]) == 0
    assert readiness.missing(data["vid"]) == []
    settings = journey.guard.get_settings(data["vid"])
    assert readiness.allow_send(data["vid"])["actions"][0]["action"] == "gtm.find"
    assert journey.guard.get_settings(data["vid"]) == settings
    draft = outreach.drafts(data["vid"])[0]
    with pytest.raises(ValueError, match="gtm_draft_recipient_required"):
        journey.patch_draft(data["wid"], data["vid"], draft["doc_id"],
            {"decision": "approved", "expected_revision": journey._draft_revision(draft)})
    assert len(outreach.drafts(data["vid"])) == 1


def _review(d):
    from app.gtm import journey
    from app.autopilot.service import update_settings
    update_settings(d["vid"], allow_send=True, send_max_per_day=5)
    state = journey.status(d["wid"], d["vid"])
    for draft in state["drafts"]:
        state = journey.patch_draft(d["wid"], d["vid"], draft["doc_id"], {"decision": "approved", "expected_revision": draft["revision"]})
    return state, {k: state["batches"][0][k] for k in ("batch_id", "draft_ids", "approval_digest", "cap")}


@pytest.mark.parametrize("name,email", [("Warehouse planners", "lee@stockroom.example"), ("Home pottery learners", "lee@clayclub.example")])
def test_requested_send_preserves_company_hold_reason(journey_db, monkeypatch, name, email):
    from app.autopilot import outreach
    from app.chat import customers
    from app.db.models import GtmContact, GtmTouch
    from app.gtm import journey
    from core.clock import utcnow

    data = journey_db
    _add(data, monkeypatch, name=name, email=email)
    _mail(data)
    _, request = _review(data)
    contact = outreach.contacts(data["vid"])[0]
    with data["factory"]() as session:
        now = utcnow()
        session.add(GtmContact(id="recent_peer", workspace_id=data["wid"], company_id=contact.company_id,
            email="peer@" + email.split("@", 1)[1], created_at=now, stage_changed_at=now))
        session.add(GtmTouch(id="recent_delivery", workspace_id=data["wid"], contact_id="recent_peer",
            direction="out", channel="email", sent_at=now, simulated=0, created_at=now))
        session.commit()
        assert journey._block(session, data["wid"], session.get(GtmContact, contact.id)) == "company_contact_hold"
    monkeypatch.setattr(outreach, "_send_one", lambda *_args: pytest.fail("A company hold must prevent delivery"))
    with pytest.raises(ValueError, match="^company_contact_hold$") as refused:
        asyncio.run(journey.send(data["wid"], data["vid"], request))
    reply = customers._send_refusal(data["vid"], refused.value)
    assert "company" in reply["body_md"] and "wait" in reply["body_md"]
    assert "permission" not in reply["body_md"] and "company_contact_hold" not in reply["body_md"]
    assert reply["action_started"] is False and reply["actions"]
    assert not outreach.drafts(data["vid"], "sent")


def _work(d, view):
    from app.core.jobs import lease, complete_job
    from app.gtm import journey
    with d["factory"]() as session:
        jobs = lease(session, "journey-test", kinds=["gtm.journey.prepare"], n=1)
        session.commit()
    assert len(jobs) == 1 and jobs[0].id == view["job_id"]
    job = jobs[0]
    asyncio.run(journey.run_prepare(job.payload | {"_job_id": job.id, "_worker_id": "journey-test", "_job_attempt": job.attempts}))
    with d["factory"]() as session:
        complete_job(session, job.id, worker_id="journey-test", attempt=job.attempts)
        session.commit()


def _expire_search_replay(d, job_id):
    from app.db.models import Job
    from core.clock import utcnow
    with d["factory"]() as session:
        job = session.get(Job, job_id)
        job.finished_at = utcnow() - timedelta(seconds=job.timeout_s + 1)
        session.commit()


@pytest.mark.hosted_only
def test_repeated_empty_searches_show_public_fallback_notice_once(journey_db, monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    from app.gtm import journey
    from app.research.prior_art import adapters
    from app.signals.research import DuckDuckGoProvider, HttpResearchProvider
    from tests.unit.gtm.test_journey_signals import fake_discovery_models

    public_searches = []
    async def search(*args, **kwargs):
        return []
    async def public_search(provider, query):
        public_searches.append(query)
        return []
    async def hn(*args, **kwargs):
        return {"items": []}
    original_call = journey._signal_call
    async def signal_call(payload, bucket, identity, callback, **kwargs):
        if bucket == "public_search":
            return await callback()
        return await original_call(payload, bucket, identity, callback, **kwargs)
    monkeypatch.setenv("FIRECRAWL_API_KEY", "synthetic")
    monkeypatch.setattr(HttpResearchProvider, "search", search)
    monkeypatch.setattr(HttpResearchProvider, "scrape", lambda *args, **kwargs: pytest.fail("unexpected scrape"))
    monkeypatch.setattr(DuckDuckGoProvider, "search", public_search)
    monkeypatch.setattr(adapters, "search_hn", hn)
    monkeypatch.setattr(journey, "_signal_call", signal_call)
    fake_discovery_models(monkeypatch)
    expected = ("The first search did not produce reachable customers. We also checked public web sources. "
        "No reachable matches were verified. Refine the audience or add a contact you know.")
    view = journey.update_icp(journey_db["wid"], journey_db["vid"], "Independent bakery owners")
    for run in range(4):
        if run:
            _expire_search_replay(journey_db, view["job_id"])
            view = journey.prepare(journey_db["wid"], journey_db["vid"], retry=True)
        _work(journey_db, view)
        view = journey.status(journey_db["wid"], journey_db["vid"])
        assert view["state"] == "needs_input"
        assert not view["prospects"] and not view["reachable_leads"]
        assert view["discovery_notice"] == expected
    assert len(public_searches) == 8


def test_firecrawl_prospects_use_more_bounded_signal_queries(journey_db, monkeypatch, caplog):
    from app.gtm import journey
    from tests.unit.gtm.test_journey_signals import signal_provider
    calls, _, _ = signal_provider(monkeypatch)
    audience = "startups who have raised serieas A and need to understand there consumers across all their channels"
    with caplog.at_level("INFO", logger=journey.__name__):
        _work(journey_db, journey.update_icp(journey_db["wid"], journey_db["vid"], audience))
    with journey_db["factory"]() as session:
        data = journey._json(journey._row(session, journey_db["vid"]).provenance_json)
    used = data["_runtime"]["calls_used"]
    assert 8 < used["signal_search"] <= journey.BUSINESS_SEARCH_QUERIES
    assert used.get("broaden_search", 0) <= journey.CALL_CEILINGS["broaden_search"]
    assert len(calls) <= used["signal_search"] + used.get("broaden_search", 0)
    assert all(len(query) <= 256 for query, _ in calls)
    assert not any("-OldBoard" in query for query, _ in calls)
    assert len(journey._prospects(journey_db["vid"])) <= 5
    assert audience not in str([r.__dict__ for r in caplog.records])


@pytest.mark.hosted_only
def test_edited_search_queries_are_saved_and_used_with_metering(journey_db, monkeypatch):
    from app.gtm import journey
    from app.db.models import CreditAccount
    from tests.unit.gtm.test_journey_signals import signal_provider
    d = journey_db
    calls, _, _ = signal_provider(monkeypatch)
    queries = journey._search_queries("Independent bakery owners", "bakery orders phone", ["OldBoard"])
    queries[0] += " Bengaluru"
    view = journey.update_icp(d["wid"], d["vid"], "Independent bakery owners", search_queries=queries)
    assert view["icp"]["search_queries"] == queries
    _work(d, view)
    assert [query for query, _ in calls[:len(queries)]] == queries
    with d["factory"]() as session:
        assert session.get(CreditAccount, d["account"]).spent_micro > 0


@pytest.mark.parametrize("queries", [[], [" "], ["one two three four five six seven eight nine"], ["a", "b", "c", "d"]])
def test_invalid_search_queries_cannot_queue_discovery(journey_db, queries):
    from app.gtm import journey
    d = journey_db
    with pytest.raises(ValueError, match="gtm_search_queries_invalid"):
        journey.update_icp(d["wid"], d["vid"], "Independent bakery owners", search_queries=queries)
    assert journey.status(d["wid"], d["vid"])["job_id"] is None


def test_later_query_failure_keeps_candidates_already_found(journey_db, monkeypatch):
    from app.gtm import journey
    from tests.unit.gtm.test_journey_signals import signal_provider
    calls, _, _ = signal_provider(monkeypatch, fail_after=1)
    d = journey_db
    _work(d, journey.update_icp(d["wid"], d["vid"], "Independent bakery owners"))
    leads = journey._prospects(d["vid"])
    assert len(calls) == journey.BUSINESS_SEARCH_QUERIES + journey.CALL_CEILINGS["broaden_search"] and len(leads) == 1
    assert leads[0]["website"] == "https://www.acme.example/"


def test_background_prepare_is_durable_deduped_and_honest_without_keys(journey_db, monkeypatch):
    from app.gtm import journey
    d = journey_db
    calls = []
    async def discover(who, search_queries=None, **kwargs):
        calls.append(who)
        return [{"email": "public@bakery.example", "first_name": "Ari", "organization": "Public Bakery", "website": "https://bakery.example/", "source_url": "https://bakery.example/contact", "why": "Public search candidate; fit unverified", "enriched": True,
            "market_role": {"role_in_market": "buyer", "icp_fit": 0.9, "anti_icp": False,
                "homepage_quote": "Independent bakery handling customer orders by phone."}}]
    monkeypatch.setattr(journey, "_discover", discover)
    view = journey.update_icp(d["wid"], d["vid"], "Independent bakery owners handling orders by phone")
    assert view["state"] == "queued" and view["estimate"]["ceiling_micro"] == journey._search_estimate(view["icp"])
    assert journey.prepare(d["wid"], d["vid"])["job_id"] == view["job_id"]
    _work(d, view)
    state = journey.status(d["wid"], d["vid"])
    assert len(calls) == 1 and len(state["prospects"]) == 1, (state["error_code"], state["message"])
    assert state["prospects"][0]["blocked_reason"] == "unknown_jurisdiction"
    assert not state["drafts"] and state["results"]["sent"] == 0
    assert journey.prepare(d["wid"], d["vid"])["job_id"] == view["job_id"]


def test_missing_search_has_retry_and_no_fake_prospects(journey_db, monkeypatch):
    from app.gtm import journey
    async def unavailable(who, search_queries=None, **kwargs):
        raise RuntimeError("provider key secret must not escape")
    monkeypatch.setattr(journey, "_discover", unavailable)
    d = journey_db
    view = journey.update_icp(d["wid"], d["vid"], "Independent bakery owners")
    _work(d, view)
    state = journey.status(d["wid"], d["vid"])
    assert state["state"] == "needs_input" and state["error_code"] == "public_search_unavailable"
    assert state["prospects"] == state["drafts"] == []
    retry = journey.prepare(d["wid"], d["vid"], retry=True)
    assert retry["job_id"] != view["job_id"]


@pytest.mark.parametrize("audience,mode", [("Freight dispatch coordinators", "company"),
    ("Parents choosing swimming lessons", "consumer")])
def test_requested_search_does_not_treat_imported_contacts_as_discovery(journey_db, monkeypatch, audience, mode):
    from app.autopilot import outreach
    from app.db.models import GtmContact
    from app.gtm import journey
    from core.clock import utcnow

    data, calls = journey_db, []
    with data["factory"]() as session:
        session.add(GtmContact(id="known_contact", workspace_id=data["wid"], email="known@example.test",
            source=f"venture:{data['vid']}:founder", stage_changed_at=utcnow(), created_at=utcnow()))
        session.commit()
    monkeypatch.setattr(journey, "_icp_seed", lambda _: {"idea": {
        "audience": audience, "problem": "Coordinating recurring activities", "confirmed": True, "mode": mode}})
    from app.core.local_mode import is_local
    if is_local():
        journey.update_icp(data["wid"], data["vid"], audience, problem="Coordinating recurring activities",
            recruitment_opt_in=mode == "consumer", start=False)

    async def discover(who, search_queries=None, **kwargs):
        calls.append(who)
        return []

    monkeypatch.setattr(journey, "_discover", discover)
    view = journey.prepare(data["wid"], data["vid"], note=f"Find ten {audience}. Do not send anything.", recruitment_opt_in=mode == "consumer")
    _work(data, view)
    assert calls == [audience]
    assert journey.status(data["wid"], data["vid"])["results"]["sent"] == 0


def test_empty_completed_search_can_search_again_without_reusing_empty_seeds(journey_db, monkeypatch):
    from app.gtm import journey
    d, calls = journey_db, []
    async def discover(who, search_queries=None, **kwargs):
        calls.append(who)
        return [] if len(calls) == 1 else [{"website": "https://bakery.example/", "source_url": "https://bakery.example/", "organization": "Bakery", "enriched": True,
            "market_role": {"role_in_market": "buyer", "icp_fit": 0.9, "anti_icp": False,
                "homepage_quote": "Independent bakery handling customer orders by phone."}}]
    monkeypatch.setattr(journey, "_discover", discover)
    view = journey.update_icp(d["wid"], d["vid"], "Independent bakery owners")
    _work(d, view)
    assert journey.status(d["wid"], d["vid"])["error_code"] == "no_public_candidates"
    _expire_search_replay(d, view["job_id"])
    _work(d, journey.prepare(d["wid"], d["vid"], retry=True))
    assert len(calls) == 2 and len(journey.status(d["wid"], d["vid"])["prospects"]) == 1


@pytest.mark.parametrize("authorize", [True, False], ids=["founder", "maintenance"])
@pytest.mark.parametrize("saved_rows", [None, [], [{"website": "https://stale.example/", "organization": "Stale Bakery"}]], ids=["completed", "empty", "checkpointed"])
@pytest.mark.parametrize("runtime", [pytest.param("hosted", marks=pytest.mark.hosted_only), "local"])
@pytest.mark.parametrize("audience", ["Freight dispatch coordinators", "Parents choosing swimming lessons", "Independent bakery owners", "Apartment gardeners"])
def test_same_key_retry_only_founder_restarts_discovery(journey_db, monkeypatch, authorize, saved_rows, runtime, audience):
    import json
    from app.core.local_mode import is_local
    from app.db.models import CreditAccount, VentureDocument
    from app.gtm import journey
    d, searches, provider_calls = journey_db, [], []
    if runtime == "local":
        monkeypatch.setenv("SUPYMEM_MODE", "local")
        configure_local_gtm(monkeypatch)
    monkeypatch.setattr("app.autopilot.outreach._brief", lambda vid: {
        "idea": {"audience": audience, "problem": "Coordinate requests", "confirmed": True}})

    async def provider():
        provider_calls.append(1)
        return []

    async def discover(who, queries=None, **kwargs):
        searches.append((who, kwargs["note"]))
        for _ in range(2):
            assert await journey._signal_call(kwargs["payload"], "signal_search", "same-query", provider, meter="firecrawl_search") == []
        return []

    monkeypatch.setattr(journey, "_discover", discover)
    view = journey.update_icp(d["wid"], d["vid"], audience, note="Avoid Acme", recruitment_opt_in=True)
    _work(d, view)
    _expire_search_replay(d, view["job_id"])
    _add(d, monkeypatch)
    from app.db.models import GtmContact
    with d["factory"]() as session:
        for contact in session.scalars(select(GtmContact)):  # the founder skipped every prospect: nothing usable remains
            contact.enrichment_json = json.dumps({**json.loads(contact.enrichment_json or "{}"), "skipped": True})
        session.commit()
    with d["factory"]() as session:
        row = session.scalar(select(VentureDocument).where(VentureDocument.kind == journey.KIND))
        did = row.id
        data = json.loads(row.provenance_json)
        data["discovered_for"] = data["discovery_key"]
        checkpoints = {"discovery_plan": [{"query": "old query", "intent": "business"}],
            "enrichment_progress": {"pages": {"stale.example": [["https://stale.example/", "Paid page"]]}},
            "channels": [], "need_signals": [], "reachable_leads": []}
        if saved_rows is not None:
            checkpoints["discovery_rows"] = saved_rows
        data.update(checkpoints)
        data["_runtime"]["calls_used"]["signal_search"] = journey.CALL_CEILINGS["signal_search"]
        before = data["_runtime"]
        spent = 0 if is_local() else session.get(CreditAccount, d["account"]).spent_micro
        row.provenance_json = json.dumps(data)
        session.commit()
    assert (spent == 0 if is_local() else spent > 0)
    assert provider_calls == [1]

    retry = journey.prepare(d["wid"], d["vid"], retry=True, authorize=authorize)
    assert retry["job_id"] != view["job_id"] and retry["discovery_note"] == "Avoid Acme"
    with d["factory"]() as session:
        queued = json.loads(session.get(VentureDocument, did).provenance_json)
    if authorize:
        assert not any(field in queued for field in ("discovery_plan", "discovery_rows", "enrichment_progress"))
        assert all(not queued.get(field) for field in ("channels", "need_signals", "reachable_leads"))
        assert queued["_runtime"]["operations"] == queued["_runtime"]["calls_used"] == {}
        assert queued["_runtime"]["ceilings"] == before["ceilings"]
    else:
        assert queued["_runtime"]["operations"] == before["operations"]
        assert queued["_runtime"]["calls_used"] == before["calls_used"]
        assert all(queued.get(field) == value for field, value in checkpoints.items() if field != "discovery_rows" or value != [])
    assert journey.prepare(d["wid"], d["vid"], retry=True, authorize=authorize)["job_id"] == retry["job_id"]
    _work(d, retry)
    assert searches == [(audience, "Avoid Acme")] * (2 if authorize else 1)
    assert len(provider_calls) == (2 if is_local() and authorize else 1)
    with d["factory"]() as session:
        after = json.loads(session.get(VentureDocument, did).provenance_json)
        assert after["discovery_key"] == data["discovery_key"]
        assert after["discovered_for"] == data["discovered_for"]
        assert "force_discovery" not in after
        assert after["_runtime"]["calls_used"].get("signal_search", 0) == (int(is_local()) if authorize else journey.CALL_CEILINGS["signal_search"])
        _assert_credit_usage(d, session, spent_micro=spent)


@pytest.mark.parametrize("audience", ["Freight dispatchers", "Community gardeners"])
def test_saved_leads_keeps_outreach_blocker_and_remedy(journey_db, monkeypatch, audience):
    from app.gtm import journey, leads

    step = {"reason": "cap_reached", "body_md": f"Your emails to {audience} stay saved. Ask me to review your daily send limit."}
    calls = []

    def saved_status(workspace_id, venture_id, *, authorize):
        calls.append((workspace_id, venture_id, authorize))
        return {"outreach_next_step": step}

    monkeypatch.setattr(journey, "status", saved_status)
    data = journey_db
    result = leads.saved_leads(data["wid"], data["vid"])
    assert result["outreach_next_step"] == step
    assert calls == [(data["wid"], data["vid"], False)]


def test_founder_search_again_with_a_usable_prospect_fetches_more(journey_db, monkeypatch):
    from app.gtm import journey
    d, searches = journey_db, []

    async def discover(who, queries=None, **kwargs):
        searches.append(who)
        return []

    monkeypatch.setattr(journey, "_discover", discover)
    first = journey.update_icp(d["wid"], d["vid"], "Independent bakery owners")
    _work(d, first)
    _expire_search_replay(d, first["job_id"])
    _add(d, monkeypatch)
    _work(d, journey.prepare(d["wid"], d["vid"], retry=True))
    assert searches == ["Independent bakery owners"] * 2
    assert len(journey._prospects(d["vid"])) == 1


@pytest.mark.parametrize("pending, legacy_key, has_matches", [(None, False, True), ("uncertain", False, True),
    ("provider_responded", True, True), ("uncertain", False, False)])
@pytest.mark.parametrize("audience,quote", [
    ("home bakers who take custom cake orders through Instagram or WhatsApp in the United States",
        "Home bakers take custom cake orders through Instagram or WhatsApp."),
    ("home-based pottery sellers in the United States who sell custom pieces through social media",
        "Home-based pottery sellers sell custom pieces through social media."),
])
def test_changed_audience_search_replaces_leads_and_scopes_geography(journey_db, monkeypatch, pending, legacy_key, has_matches, audience, quote):
    import json
    from app.db.models import Venture, VentureDocument
    from app.gtm import journey
    from app.research.prior_art import adapters
    from app.signals.research import HttpResearchProvider
    from tests.unit.gtm.test_journey_signals import quality_response

    data = journey_db
    async def empty(*args, **kwargs):
        return []
    with monkeypatch.context() as initial:
        initial.setattr(journey, "_discover", empty)
        _work(data, journey.update_icp(data["wid"], data["vid"], "Independent bakery owners in India"))
    old_urls = ["https://bakery.in/", "https://sample-india-bakery.test/", "https://bakery.pk/", "https://bakery.ae/", "https://sample-us-bakery.test/"]
    with data["factory"]() as session:
        row = session.scalar(select(VentureDocument).where(VentureDocument.kind == journey.KIND))
        saved = json.loads(row.provenance_json)
        previous_key = saved["discovery_key"]
        saved.update(discovered_for=previous_key, reachable_leads=[{"id": url, "source_url": url, "country": country} for url, country in
            zip(old_urls, ("IN", "IN", "PK", "AE", "US"))], seen_lead_urls=old_urls,
            discovery_plan=[{"query": "stale bakery query", "intent": "business"}],
            broadened_discovery_plan=[{"query": "stale broadened query", "intent": "business"}],
            discovery_rows=[{"website": old_urls[0]}], enrichment_progress={"pages": {"bakery.in": []}})
        if legacy_key:
            saved.pop("discovery_key")
        if pending:
            saved["_runtime"]["operations"]["discovery-plan"] = {"state": pending, "attempt": 1}
            saved["_runtime"]["calls_used"]["discovery-plan"] = 1
        row.provenance_json = json.dumps(saved)
        session.commit()
    journey.update_icp(data["wid"], data["vid"], audience, start=False)
    with data["factory"]() as session:
        row = session.scalar(select(VentureDocument).where(VentureDocument.kind == journey.KIND))
        saved = json.loads(row.provenance_json)
        saved["icp"].update(idea="WhatsApp orders for mom's bakery in India", location="India", geography="India")
        session.get(Venture, data["vid"]).raw_idea = saved["icp"]["idea"]
        row.provenance_json = json.dumps(saved)
        session.commit()

    searches, plans = [], []
    profile = "https://www.linkedin.com/in/ada-maker"
    hits = [{"url": url, "title": "US Bakery" if country == "US" else "Other Bakery", "snippet": quote,
        "country": country} for url, country in zip(old_urls, ("IN", "IN", "PK", "AE", "US"))]
    async def search(provider, query, **kwargs):
        searches.append((query, kwargs))
        if has_matches and any(target in query for target in ("site:linkedin.com", "social media profile", "marketplace seller profile")):
            return [{"url": profile, "title": "Ada Maker", "snippet": "Ada Maker sells from home in the United States. " + quote}]
        return [hit for hit in hits if has_matches or hit["country"] != "US"] if "contact" in query and "site:" not in query else []
    async def scrape(provider, url, **kwargs):
        return f"US Bakery\n{quote}\n123 Main Street, Boston, MA 02110\nUnited States\nEmail hello@sample-us-bakery.test"
    async def model(payload, stage, context):
        if stage in {"marketrole", "passages"}:
            return quality_response(stage, context)
        if stage == "plan":
            plans.append(context)
            return {"queries": [{"query": "home bakery contact", "intent": "business"},
                {"query": "custom cake orders contact", "intent": "business"}]}
        if stage == "reachjudge":
            return {"items": [{"id": candidate["id"], "kind": candidate["kind"], "name": candidate["title"], "quote": quote,
                "url": candidate["url"], "affiliation": "", "location": "United States", "fit": 90,
                "why": "Takes cake orders through Instagram or WhatsApp", "value_exchange": "Discuss cake ordering workflows"}
                for candidate in context["candidates"]]}
        return {"items": []}
    monkeypatch.setenv("FIRECRAWL_API_KEY", "synthetic-key")
    monkeypatch.setenv("GTM_ENRICH_ENABLED", "1")
    monkeypatch.setattr(HttpResearchProvider, "search", search)
    monkeypatch.setattr(HttpResearchProvider, "scrape", scrape)
    monkeypatch.setattr(adapters, "search_hn", empty)
    monkeypatch.setattr(journey, "_discovery_model", model)
    monkeypatch.setattr(journey, "_draft_ready", lambda *args: 0)
    view = journey.prepare(data["wid"], data["vid"], retry=True, recruitment_opt_in=True)
    with data["factory"]() as session:
        queued = json.loads(session.scalar(select(VentureDocument).where(VentureDocument.kind == journey.KIND)).provenance_json)
    assert queued.get("reachable_leads", []) == queued.get("seen_lead_urls", []) == []
    assert not any(field in queued for field in ("discovery_rows", "discovery_plan", "broadened_discovery_plan", "enrichment_progress"))
    if pending:
        assert queued["_runtime"]["operations"]["discovery-plan"] == {"state": pending, "attempt": 1}
        assert queued["_runtime"]["calls_used"]["discovery-plan"] == 1
    _work(data, view)
    with data["factory"]() as session:
        result = json.loads(session.scalar(select(VentureDocument).where(VentureDocument.kind == journey.KIND)).provenance_json)
    assert searches and plans[0]["audience"] == audience
    assert plans[0]["idea"] == "WhatsApp orders for mom's bakery in India"
    assert plans[0]["location"] == plans[0]["geography"] == "United States"
    assert all("United States" in query and "India" not in query for query, _ in searches)
    assert all(item["intent"] != "business" and "contact" not in item["query"] for item in result["discovery_plan"])
    assert all("sample-us-bakery.test" not in kwargs.get("exclude_domains", []) for _, kwargs in searches)
    assert result["discovery_key"] != previous_key
    if has_matches:
        assert result["discovered_for"] == result["discovery_key"]
    assert {lead["source_url"] for lead in result.get("reachable_leads", [])} == ({profile} if has_matches else set())
    assert all(lead["country"] == "US" for lead in result.get("reachable_leads", []))


@pytest.mark.parametrize("url, country, text, mismatch", [
    ("https://bakery.pk", "", "Cake orders", True), ("https://bakery.ae", "", "Cake orders", True),
    ("https://bakery.in", "", "We ship to the United States", True),
    ("https://sample-bakery.test", "IN", "We ship to the United States", True),
    ("https://sample-bakery.test", "", "Cake orders", False),  # unknown country stays: most US .com sites never name it
    ("https://sample-bakery.test", "", "Home baker in India shipping to the United States", True),
    ("https://sample-bakery.test", "US", "Cake orders", False),
    ("https://sample-bakery.test", "", "Home baker in the United States", False)])
def test_audience_country_drops_only_positive_other_country_evidence(journey_db, url, country, text, mismatch):
    from app.gtm import journey
    assert journey._geography_mismatch("Home bakers in the United States", url, text, country, require_match=True) is mismatch


def test_publication_drops_other_country_leads_and_keeps_unknown(journey_db):
    from types import SimpleNamespace
    from app.gtm import reachable

    def lead(name, country, location=""):
        return {"id": name, "source_url": f"https://{name}.com/", "country": country, "location": location,
            "fit": 90, "quote": "Home bakers take custom cake orders.", "routes": [], "judging_status": "judged"}
    document = {"icp": {"who": "Home bakers in the United States", "location": "India"},
        "reachable_leads": [lead("old-india", "IN", "We ship to the United States"), lead("old-us", "US")]}
    journal = SimpleNamespace(mutate=lambda identifier, payload, update: update(None, document, None))
    reachable._publish(journal, {"journey_id": "publication-test"}, [lead("new-pakistan", "PK"), lead("new-uae", "AE"),
        lead("unknown", "") | {"quote": "We ship cakes to the United States."}, lead("new-us", "US")])
    assert {item["id"] for item in document["reachable_leads"]} == {"old-us", "new-us", "unknown"}


def test_founder_note_is_stored_and_used_to_plan_targeting(journey_db, monkeypatch):
    from app.gtm import journey
    from app.db.models import Job, VentureDocument
    import json
    d, plans, searches = journey_db, [], []
    async def discover(who, queries=None, **kwargs):
        plans.append(kwargs)
        searches.append(queries)
        return []
    monkeypatch.setattr(journey, "_discover", discover)
    view = journey.update_icp(d["wid"], d["vid"], "Independent bakery owners", note="  Avoid Acme; target Bengaluru  ")
    assert view["discovery_started"] and view["discovery_note"] == "Avoid Acme; target Bengaluru"
    assert view["venture_name"] == "Order board"
    with d["factory"]() as session:
        assert json.loads(session.get(Job, view["job_id"]).payload_json)["discovery_note"] == view["discovery_note"]
    assert journey.prepare(d["wid"], d["vid"], note="Replace guidance")["discovery_note"] == view["discovery_note"]
    _work(d, view)
    assert searches == [view["icp"]["search_queries"]]
    assert plans[0]["note"] == view["discovery_note"] and plans[0]["problem"]
    with d["factory"]() as session:
        assert json.loads(session.scalar(select(VentureDocument).where(VentureDocument.kind == journey.KIND)).provenance_json)["discovery_note"] == view["discovery_note"]
    with pytest.raises(ValueError, match="gtm_invalid_input"):
        journey.prepare(d["wid"], d["vid"], note="x" * 2001)


def test_enrichment_retry_keeps_the_same_job_seeds_and_note(journey_db, monkeypatch):
    import json
    from app.core.jobs import RetryableError, complete_job, fail_job, lease
    from app.db.models import Job, VentureDocument
    from app.gtm import journey
    from core.clock import utcnow
    d, searches, attempts = journey_db, [], []
    async def discover(who, queries=None, **kwargs):
        searches.append(queries)
        return [{"website": "https://bakery.example/", "source_url": "https://bakery.example/", "organization": "Bakery",
            "market_role": {"role_in_market": "buyer", "icp_fit": 0.9, "anti_icp": False,
                "homepage_quote": "Independent bakery handling customer orders by phone."}}]
    async def enrich(rows, wid, vid, *, progress, checkpoint, context, payload):
        attempts.append(rows)
        if len(attempts) == 1:
            progress["pages"] = {"bakery.example": [["https://bakery.example/", "Paid homepage"]]}
            checkpoint()
            raise RetryableError("gtm_enrichment_slot_unavailable", retry_after_s=30)
        assert progress["pages"]["bakery.example"][0][1] == "Paid homepage"
        return {}
    monkeypatch.setattr(journey, "_discover", discover)
    monkeypatch.setattr(journey.enrich, "enrich_seeds", enrich)
    view = journey.update_icp(d["wid"], d["vid"], "Independent bakery owners", note="Avoid Acme")
    def claim():
        with d["factory"]() as session:
            job = lease(session, "retry-test", kinds=["gtm.journey.prepare"])[0]
            session.commit()
        return job, job.payload | {"_job_id": job.id, "_worker_id": "retry-test", "_job_attempt": job.attempts}
    job, payload = claim()
    with pytest.raises(RetryableError) as failure:
        asyncio.run(journey.run_prepare(payload))
    with d["factory"]() as session:
        assert fail_job(session, job.id, failure.value, worker_id="retry-test", attempt=job.attempts) == "retry_wait"
        session.get(Job, job.id).run_after = utcnow() - timedelta(seconds=1)
        session.get(Job, job.id).scheduled_for = utcnow() - timedelta(seconds=1)
        session.commit()
        data = json.loads(session.scalar(select(VentureDocument).where(VentureDocument.kind == journey.KIND)).provenance_json)
        assert data["discovery_rows"] == attempts[0] and data["discovery_note"] == "Avoid Acme"
    assert journey.status(d["wid"], d["vid"])["job_status"] == "retry_wait"
    job, payload = claim()
    asyncio.run(journey.run_prepare(payload))
    with d["factory"]() as session:
        complete_job(session, job.id, worker_id="retry-test", attempt=job.attempts)
        session.commit()
    assert job.id == view["job_id"] and len(searches) == 1 and len(attempts) == 2
    assert len(journey.status(d["wid"], d["vid"])["prospects"]) == 1


def _search_provider(monkeypatch, replies):
    import ddgs
    from app.signals.research import HttpResearchProvider
    from app.research.prior_art import adapters
    from app.gtm import journey
    from app.core.local_mode import is_local
    monkeypatch.setenv("FIRECRAWL_API_KEY", "synthetic-key")
    calls = []

    async def no_model(*args, **kwargs):
        return None

    async def search(self, query, **kwargs):
        calls.append(kwargs)
        reply = replies[min(len(calls) - 1, len(replies) - 1)]
        if isinstance(reply, Exception):
            raise reply
        return [{**row, "url": row.get("href") or row.get("url")} for row in reply]

    async def hn(*args, **kwargs):
        return {"items": []}

    monkeypatch.setattr(HttpResearchProvider, "search", search)
    if is_local():
        monkeypatch.setattr(journey, "_discovery_model", no_model)
    monkeypatch.setattr(adapters, "search_hn", hn)
    monkeypatch.setattr(ddgs, "DDGS", lambda *args, **kwargs: pytest.fail("no keyless fallback"))
    return calls


@pytest.mark.parametrize("first", [[], [{"href": "https://wikipedia.org/wiki/Bakery"}]])
def test_public_search_never_qualifies_bare_company_hits(journey_db, monkeypatch, first):
    from app.gtm import journey
    from app.core.local_mode import is_local
    calls = _search_provider(monkeypatch, [first, [{"href": "https://bakery.example/contact", "title": "Bakery"}]])
    d = journey_db
    _work(d, journey.update_icp(d["wid"], d["vid"], "Independent bakery owners",
        search_queries=["Independent bakery owners", "Independent bakeries"]))
    state = journey.status(d["wid"], d["vid"])
    assert state["state"] == "needs_input" and state["error_code"] == ("public_sources_unavailable" if is_local() else "no_public_candidates")

    assert state["prospects"] == []
    assert 0 < len(calls) <= journey.BUSINESS_SEARCH_QUERIES if is_local() else len(calls) == 4


@pytest.mark.parametrize("reply,code", [
    (RuntimeError("private exception text https://private.example"), "public_search_unavailable"),
    ([], "no_public_candidates"),
    ([{"href": "https://wikipedia.org/wiki/Bakery?token=fixture-private-trace"}, {"href": "https://www.linkedin.com/company/private"},
      {"href": "http://localhost/"}, {"href": "http://[invalid"}], "no_public_candidates"),
])
def test_public_search_reports_blocked_or_empty_with_content_free_logs(journey_db, monkeypatch, caplog, reply, code):
    from app.gtm import journey
    from app.core.local_mode import is_local
    calls = _search_provider(monkeypatch, [reply])
    d, audience = journey_db, "Private fixture audience that must never be logged"
    with caplog.at_level("INFO", logger=journey.__name__):
        _work(d, journey.update_icp(d["wid"], d["vid"], audience))
    state = journey.status(d["wid"], d["vid"])
    assert state["state"] == "needs_input" and state["error_code"] == code
    assert state["prospects"] == state["drafts"] == []
    assert 0 < len(calls) <= journey.BUSINESS_SEARCH_QUERIES + journey.CALL_CEILINGS["broaden_search"] if is_local() else len(calls) == journey.BUSINESS_SEARCH_QUERIES
    if code == "public_search_blocked":
        assert "Add a contact you know" in state["message"] and "Retry" not in state["message"]
    else:
        if is_local() and code == "public_search_unavailable":
            from app.research.local_providers import SEARCH_UNAVAILABLE
            assert SEARCH_UNAVAILABLE in state["message"]
        else:
            assert "Retry search" in state["message"] if code == "public_search_unavailable" else "add someone you know" in state["message"]
    discovery = next(r for r in caplog.records if r.message == "gtm_journey_discovery")
    assert discovery.ingested == discovery.prospects == 0 and discovery.error_code == code
    records = str([r.__dict__ for r in caplog.records])
    assert audience not in records and "private exception text" not in records and "https://private.example" not in records
    assert "https://wikipedia.org" not in str(discovery.__dict__)
    assert "fixture-private-trace" not in records


@pytest.mark.parametrize("replies", [[[], RuntimeError("private failure")], [RuntimeError("private failure"), []]])
def test_keyless_search_is_never_a_provider_fallback(journey_db, monkeypatch, replies):
    from app.gtm import journey
    calls = _search_provider(monkeypatch, replies)
    monkeypatch.delenv("FIRECRAWL_API_KEY")
    monkeypatch.delenv("FIRECRAWL_SELF_HOSTED_URL", raising=False)
    from app.core.local_mode import is_local

    if is_local():
        from app.core.local_secrets import update_settings

        update_settings("search", {"provider": "firecrawl", "firecrawl_url": ""}, {"firecrawl_key": ""})
    d = journey_db
    view = journey.update_icp(d["wid"], d["vid"], "Independent bakery owners",
        search_queries=["Independent bakery owners", "Independent bakeries"])
    if is_local():
        from app.core.local_capabilities import blocked_message
        assert view["job_id"] is None
        assert view["message"] == blocked_message("find_customers")
    else:
        _work(d, view)
    state = journey.status(d["wid"], d["vid"])
    assert state["error_code"] == "public_search_blocked" and state["prospects"] == []
    assert calls == []


@pytest.mark.parametrize("url,title,host", [
    ("https://n8n.io/", "Automated lead generation & contact enrichment", "n8n.io"),
    ("https://workflowstacks.com/tools/mengram", "Mengram: AI Memory for Founders", "workflowstacks.com"),
])
def test_discovery_headlines_never_become_verified_company_identity(journey_db, monkeypatch, url, title, host):
    """Synthetic crawl from recorded headline mismatches; no actual contact data."""
    from app.gtm import journey
    calls = _search_provider(monkeypatch, [[{"url": url, "title": title}]])
    d = journey_db
    _work(d, journey.update_icp(d["wid"], d["vid"], "Founders improving customer outreach"))
    from app.core.local_mode import is_local
    ceiling = journey.BUSINESS_SEARCH_QUERIES + journey.CALL_CEILINGS["broaden_search"]
    assert 0 < len(calls) <= ceiling if is_local() else len(calls) == ceiling
    assert journey._prospects(d["vid"]) == []


def test_no_site_needed_country_permission_and_suppression_block_drafts(journey_db, monkeypatch):
    from app.gtm import journey
    from app.db.models import GtmSuppression
    from core.clock import utcnow
    d = journey_db
    blocked = _add(d, monkeypatch, country="DE", basis="none")
    assert not blocked["drafts"] and blocked["prospects"][0]["blocked_reason"] == "eprivacy_opt_in_required"
    with d["factory"]() as session:
        session.add(GtmSuppression(id="sup_test", workspace_id=d["wid"], email="ada@bakery.example", source="opt_out", created_at=utcnow()))
        session.commit()
    state = journey.patch_prospect(d["wid"], d["vid"], blocked["prospects"][0]["id"], {"country": "CA", "lawful_basis": "consent", "lawful_basis_note": "They agreed by reply on 3 May."})
    assert not state["drafts"] and state["prospects"][0]["blocked_reason"] == "suppressed_or_replied"


@pytest.mark.parametrize("name,email,audience", [("Invoice auditing", "auditor@invoice-audits.example", "Accounting teams"), ("Ceramics classes", "learner@ceramics-club.example", "Home potters")])
@pytest.mark.parametrize("provider_called", [False, True])
@pytest.mark.hosted_only
def test_paced_send_retries_only_before_provider_call(journey_db, monkeypatch, caplog, name, email, audience, provider_called):
    import json

    from app.autopilot import outreach
    from app.chat import customers
    from app.connections import composio_pacing as pacing
    from app.db.models import Approval, CreditAccount, Job, Venture
    from app.gtm import journey
    from app.gtm.supymem import composio_gmail
    from app.worker import handlers

    from tests.unit.intake.test_contact_import import _commit, _preview

    data = journey_db
    with data["factory"]() as session:
        session.get(Venture, data["vid"]).name = name
        session.get(Venture, data["vid"]).raw_idea = f"{name} for {audience}"
        session.commit()
    brief = outreach._brief(data["vid"])
    brief["idea"].update(audience=audience, problem=name)
    monkeypatch.setattr(outreach, "_brief", lambda venture_id: brief)
    monkeypatch.setattr(outreach, "site_url", lambda venture_id: None)
    _commit(data, _preview(data, monkeypatch, f"name,email,country\n{name},{email},CA"))
    _mail(data)
    journey.draft_for_review(data["vid"])
    _, request = _review(data)
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("COMPOSIO_ORG_RPM", "6000")
    monkeypatch.setenv("COMPOSIO_ACCOUNT_RPM", "2")
    monkeypatch.setattr(pacing, "_BUCKETS", {})
    now = [100.0]
    monkeypatch.setattr(pacing.time, "monotonic", lambda: now[0])
    calls = []
    class Client:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def request(self, *args, **kwargs):
            calls.append(email)
            if provider_called:
                raise pacing.RateLimited(90)
            from types import SimpleNamespace
            return SimpleNamespace(status_code=200, headers={}, json=lambda: {"successful": True})
    monkeypatch.setattr(composio_gmail.httpx, "Client", Client)
    monkeypatch.setattr(composio_gmail, "_headers", dict)
    def send(box, *args):
        composio_gmail._request("POST", "/tools/execute/GMAIL_SEND_EMAIL", json={"connected_account_id": box["account"]})
        return {"provider_message_id": "paced-receipt", "thread_id": "paced-thread"}
    monkeypatch.setattr(outreach, "_send_one", send)
    if not provider_called:
        pacing.acquire("ca_fixture")
        pacing.acquire("ca_fixture")
    reply = asyncio.run(customers.execute(data["vid"], "gtm.send", request, data["actor"]))
    saved = outreach.drafts(data["vid"])[0]
    with data["factory"]() as session:
        queued = list(session.scalars(select(Job).where(Job.kind == "gtm.outreach.retry", Job.venture_id == data["vid"])))
        approval = session.scalar(select(Approval).where(Approval.subject_ref == saved["doc_id"]))
        execution = json.loads(approval.detail_json)["external_execution"]
        _assert_credit_usage(data, session, spent_micro=0, reserved_micro=10_000 if provider_called else 0)
    if provider_called:
        assert saved["state"] == "delivery_unconfirmed" and calls == [email]
        assert not queued and execution["state"] == "unknown"
        assert "automatically again" in reply["body_md"]
    else:
        assert saved["state"] == "draft" and not calls
        assert saved["body"] and saved["error"] == "composio_rate_limited"
        assert len(queued) == 1 and execution["state"] == "retry_wait"
        assert reply["kind"] == "wait" and "Gmail is busy" in reply["body_md"]
        assert "nothing was sent yet" in reply["body_md"].lower() and "retry by itself" in reply["body_md"]
        assert "check your saved" not in reply["body_md"].lower()
        assert all(record.message != "founder_action_failed" for record in caplog.records)
        payload = json.loads(queued[0].payload_json) | {"venture_id": data["vid"]}
        now[0] += 61
        asyncio.run(handlers._outreach_retry(payload))
        asyncio.run(handlers._outreach_retry(payload))
        assert calls == [email] and outreach.drafts(data["vid"])[0]["state"] == "sent"
        with data["factory"]() as session:
            _assert_credit_usage(data, session, spent_micro=10_000, reserved_micro=0)
            assert session.scalar(select(Approval).where(Approval.subject_ref == saved["doc_id"])).id == approval.id


@pytest.mark.parametrize("name,email,audience", [("Invoice auditing", "auditor@invoice-audits.example", "Accounting teams"), ("Ceramics classes", "learner@ceramics-club.example", "Home potters")])
def test_requested_send_flow_has_no_automatic_sending_consent_card(journey_db, monkeypatch, name, email, audience):
    from app.autopilot import guard, outreach
    from app.autopilot.service import update_settings
    from app.chat import connect
    from app.db.models import Venture
    from app.gtm import journey
    from app.needs_you import readiness

    from tests.unit.intake.test_contact_import import _commit, _preview

    data = journey_db
    with data["factory"]() as session:
        session.get(Venture, data["vid"]).name = name
        session.get(Venture, data["vid"]).raw_idea = f"{name} for {audience}"
        session.commit()
    brief = outreach._brief(data["vid"])
    brief["idea"].update(audience=audience, problem=name)
    monkeypatch.setattr(outreach, "_brief", lambda venture_id: brief)
    monkeypatch.setattr(outreach, "site_url", lambda venture_id: None)
    _commit(data, _preview(data, monkeypatch, f"name,email,country\n{name},{email},CA"))
    _mail(data)
    journey.draft_for_review(data["vid"])
    monkeypatch.setattr(connect, "_is_connected", lambda *args: True)
    assert not guard.get_settings(data["vid"])["allow_send"]
    assert all(item.reason_code != "consent_send" for item in readiness.missing(data["vid"]))
    _, request = _review(data)
    update_settings(data["vid"], allow_send=False)
    review = journey._batch_review(data["vid"], request["batch_id"])
    request = {key: review[key] for key in ("batch_id", "draft_ids", "approval_digest", "cap")}
    monkeypatch.setattr(outreach, "_send_one", lambda *args: {"provider_message_id": "requested-receipt", "thread_id": "requested-thread"})
    asyncio.run(journey.send(data["wid"], data["vid"], request))
    assert all(item.reason_code != "consent_send" for item in readiness.missing(data["vid"]))
    assert not guard.get_settings(data["vid"])["allow_send"]


def test_exact_approved_send_metered_once_and_no_autopilot_bypass(journey_db, monkeypatch):
    from app.autopilot import outreach
    from app.db.models import CreditAccount, GtmTouch
    from app.gtm import journey
    from app.gtm.relationships.unsubscribe import unsubscribe_url
    d = journey_db
    initial = _add(d, monkeypatch)
    assert len(initial["drafts"]) == 1
    draft = initial["drafts"][0]
    link = unsubscribe_url(d["wid"], draft["contact_id"], draft["to"])
    assert draft["body"].count(link) == 1
    assert len(draft["body"].split()) <= 120 and draft["body"].replace(link, "").count("?") == 1 and "no thanks" in draft["body"]
    _mail(d)
    state, request = _review(d)
    sent = []
    monkeypatch.setattr(outreach, "_send_one", lambda box, to, subject, body, thread: sent.append(to) or {"provider_message_id": "receipt-1", "thread_id": "thread-1"})
    bypass = asyncio.run(outreach.send_batch(d["vid"], {"approved_by_founder": True, "batch_id": request["batch_id"]}, actor_subject=d["actor"]))
    assert bypass["kind"] == "needs_you" and not sent
    result = asyncio.run(journey.send(d["wid"], d["vid"], request))
    assert result["results"]["sent"] == 1 and sent == ["ada@bakery.example"], result["drafts"][0].get("error")
    with d["factory"]() as session:
        _assert_credit_usage(d, session, spent_micro=10_000)
        assert len(list(session.scalars(select(GtmTouch).where(GtmTouch.venture_id == d["vid"])))) == 1
    with pytest.raises(ValueError, match="gtm_approval_changed"):
        asyncio.run(journey.send(d["wid"], d["vid"], request))
    assert len(sent) == 1


@pytest.mark.parametrize("name,email", [("Factory purchasing", "buyer@factory.example"), ("Family gardening", "learner@garden.example")])
def test_requested_reviewed_send_does_not_enable_automatic_sending(journey_db, monkeypatch, name, email):
    from app.autopilot import guard, outreach
    from app.autopilot.service import update_settings
    from app.gtm import journey

    data = journey_db
    _add(data, monkeypatch, name=name, email=email)
    _mail(data)
    _, request = _review(data)
    update_settings(data["vid"], allow_send=False)
    reviewed = journey._batch_review(data["vid"], request["batch_id"])
    request = {key: reviewed[key] for key in ("batch_id", "draft_ids", "approval_digest", "cap")}
    sent = []
    monkeypatch.setattr(outreach, "_send_one", lambda box, to, subject, body, thread: sent.append(to) or {"provider_message_id": "requested-send", "thread_id": "requested-thread"})
    result = asyncio.run(journey.send(data["wid"], data["vid"], request))
    assert result["results"]["sent"] == 1 and sent == [email]
    assert guard.get_settings(data["vid"])["allow_send"] is False
    with pytest.raises(ValueError, match="gtm_approval_changed"):
        asyncio.run(journey.send(data["wid"], data["vid"], request))
    assert sent == [email]


def test_edit_recipient_sender_and_cap_invalidate_exact_approval(journey_db, monkeypatch):
    from app.db.models import GtmContact, GtmEmailAccount
    from app.gtm import journey
    from app.autopilot import outreach
    d = journey_db
    _add(d, monkeypatch)
    _mail(d)
    state, request = _review(d)
    def never_send(*args):
        pytest.fail("No provider I/O for changed approval")
    monkeypatch.setattr(outreach, "_send_one", never_send)
    with d["factory"]() as session:
        contact = session.get(GtmContact, state["drafts"][0]["contact_id"])
        contact.email = "different@bakery.example"
        session.commit()
    with pytest.raises(ValueError, match="gtm_approval_changed"):
        asyncio.run(journey.send(d["wid"], d["vid"], request))
    with d["factory"]() as session:
        session.get(GtmContact, state["drafts"][0]["contact_id"]).email = "ada@bakery.example"
        session.get(GtmEmailAccount, "mail_journey").email = "other@gmail.example"
        session.commit()
    with pytest.raises(ValueError, match="gtm_approval_changed"):
        asyncio.run(journey.send(d["wid"], d["vid"], request))
    with d["factory"]() as session:
        session.get(GtmEmailAccount, "mail_journey").email = "founder@gmail.example"
        session.commit()
    with pytest.raises(ValueError, match="gtm_approval_changed"):
        asyncio.run(journey.send(d["wid"], d["vid"], request | {"cap": 1}))
    edit = journey.patch_draft(d["wid"], d["vid"], state["drafts"][0]["doc_id"], {"decision": "approved", "expected_revision": state["drafts"][0]["revision"], "body": state["drafts"][0]["body"].replace("short reply", "brief reply")})
    assert edit["drafts"][0]["review_state"] == "draft"
    with pytest.raises(ValueError, match="gtm_approval_changed"):
        asyncio.run(journey.send(d["wid"], d["vid"], request))


@pytest.mark.parametrize("day", [1, 3])
@pytest.mark.parametrize("review_only", [False, True])
def test_journey_subset_never_sends_unreviewed_generic_batch_siblings(journey_db, monkeypatch, day, review_only):
    from app.autopilot import outreach
    from app.autopilot.service import update_settings
    from app.db.models import CreditAccount, GtmContact, GtmTouch
    from app.gtm import journey

    d = journey_db
    for name, email in (("Ada", "ada@bakery.example"), ("Bob", "bob@bakery.example"), ("Cara", "cara@bakery.example")):
        _add(d, monkeypatch, name=name, email=email)
    rows = outreach.drafts(d["vid"])
    batch = rows[0]["batch_id"]
    for row in rows:
        outreach._save_draft(d["vid"], row | {"batch_id": batch, "day": day,
            "requires_batch_approval": review_only, "requires_journey_review": False,
            "review_state": "draft"}, row["doc_id"])
    with d["factory"]() as session:
        for row in rows:
            session.get(GtmContact, row["contact_id"]).gmail_thread_id = "existing-fixture-thread"
        session.commit()
    _mail(d)
    update_settings(d["vid"], allow_send=True, send_max_per_day=5)
    state = journey.patch_draft(d["wid"], d["vid"], rows[0]["doc_id"], {"decision": "approved", "expected_revision": next(row["revision"] for row in journey.status(d["wid"], d["vid"])["drafts"] if row["doc_id"] == rows[0]["doc_id"])})
    reviewed = next(b for b in state["batches"] if b["batch_id"] == batch)
    request = {k: reviewed[k] for k in ("batch_id", "draft_ids", "approval_digest", "cap")}
    assert request["draft_ids"] == [rows[0]["doc_id"]]
    calls = []
    monkeypatch.setattr(outreach, "_send_one", lambda box, to, subject, body, thread:
        calls.append(to) or {"provider_message_id": f"fixture-{len(calls)}", "thread_id": "existing-fixture-thread"})
    result = asyncio.run(journey.send(d["wid"], d["vid"], request))
    assert calls == [rows[0]["to"]]
    assert result["results"]["sent"] == 1
    untouched = [row for row in result["drafts"] if row["doc_id"] not in request["draft_ids"]]
    assert len(untouched) == 2 and all(row["state"] == "draft" and row["review_state"] == "draft" for row in untouched)
    with d["factory"]() as session:
        _assert_credit_usage(d, session, spent_micro=10_000, reserved_micro=0)
        assert len(list(session.scalars(select(GtmTouch).where(GtmTouch.venture_id == d["vid"])))) == 1


def test_stale_individual_review_cannot_approve_or_overwrite_another_edit(journey_db, monkeypatch):
    from app.gtm import journey
    d = journey_db
    shown = _add(d, monkeypatch)["drafts"][0]
    changed_body = shown["body"].replace("short reply", "brief reply")
    changed = journey.patch_draft(d["wid"], d["vid"], shown["doc_id"], {
        "expected_revision": shown["revision"], "decision": "draft", "body": changed_body})["drafts"][0]
    assert changed["revision"] != shown["revision"] and changed["review_state"] == "draft"
    for decision, edits in (("approved", {}), ("draft", {"body": shown["body"]}), ("skipped", {})):
        with pytest.raises(ValueError, match="gtm_draft_changed"):
            journey.patch_draft(d["wid"], d["vid"], shown["doc_id"], {
                "expected_revision": shown["revision"], "decision": decision, **edits})
    current = journey.status(d["wid"], d["vid"])["drafts"][0]
    assert current["body"] == changed_body and current["review_state"] == "draft"
    approved = journey.patch_draft(d["wid"], d["vid"], current["doc_id"], {
        "expected_revision": current["revision"], "decision": "approved"})
    assert approved["drafts"][0]["review_state"] == "approved"


@pytest.mark.parametrize("body", ["No automation.", "Too long " * 121])
def test_unusable_inherited_drafts_can_be_skipped(journey_db, monkeypatch, body):
    from app.autopilot import outreach
    from app.gtm import journey
    d = journey_db
    shown = _add(d, monkeypatch)["drafts"][0]
    outreach._save_draft(d["vid"], shown | {"body": body, "requires_journey_review": False}, shown["doc_id"])
    shown = journey.status(d["wid"], d["vid"])["drafts"][0]
    skipped = journey.patch_draft(d["wid"], d["vid"], shown["doc_id"], {
        "expected_revision": shown["revision"], "decision": "skipped"})
    assert skipped["drafts"][0]["state"] == skipped["drafts"][0]["review_state"] == "skipped"
    assert skipped["results"]["sent"] == 0


def test_mailbox_change_after_digest_does_not_grant_new_sender_approval(journey_db, monkeypatch):
    from app.autopilot import outreach
    from app.db.models import CreditAccount, GtmEmailAccount
    from app.gtm import journey
    d = journey_db
    _add(d, monkeypatch)
    _mail(d)
    state, request = _review(d)
    assert all("_sender" not in b for b in state["batches"])
    original = journey._batch_review
    replaced = []
    def replace_after_review(vid, batch, **kwargs):
        reviewed = original(vid, batch, **kwargs)
        if not replaced:
            from app.core.local_mode import is_local
            if is_local():
                from secrets import token_urlsafe
                from app.core.local_secrets import update_settings
                update_settings("smtp", {"user": "changed@gmail.example"}, {"password": token_urlsafe(24)})
            else:
                with d["factory"]() as session:
                    box = session.get(GtmEmailAccount, "mail_journey")
                    box.email, box.connected_account_id = "changed@gmail.example", "changed-account"
                    session.commit()
            replaced.append(True)
        return reviewed
    monkeypatch.setattr(journey, "_batch_review", replace_after_review)
    monkeypatch.setattr(outreach, "_send_one", lambda *a: pytest.fail("A new mailbox has not been reviewed"))
    result = asyncio.run(journey.send(d["wid"], d["vid"], request))
    assert result["results"]["sent"] == 0 and result["drafts"][0]["review_state"] == "draft"
    with d["factory"]() as session:
        _assert_credit_usage(d, session, spent_micro=0, reserved_micro=0)


@pytest.mark.parametrize("phase", ["digest", "metering"])
@pytest.mark.parametrize("same_version", [False, True])
@pytest.mark.hosted_only
def test_rate_change_after_review_cannot_raise_approved_send_cost(journey_db, monkeypatch, phase, same_version):
    from app.approvals import external
    from app.autopilot import outreach
    from app.credits.policy import set_policy
    from app.db.models import CreditAccount
    from app.gtm import journey
    d = journey_db
    _add(d, monkeypatch)
    _mail(d)
    state, request = _review(d)
    changed = []
    def change_rate():
        if not changed:
            set_policy("outbound_provider_rates", {"version": "test-gmail-v1" if same_version else "test-gmail-v2",
                "rates": {"gtm_email_send:composio_gmail": {"action": "gtm_email_send", "provider": "composio_gmail",
                "price_usd": "0.50", "currency": "USD", "source": "Changed offline fixture rate"}}}, "test")
            changed.append(True)
    if phase == "digest":
        original = journey._batch_review
        def change_after_digest(vid, batch, **kwargs):
            reviewed = original(vid, batch, **kwargs)
            change_rate()
            return reviewed
        monkeypatch.setattr(journey, "_batch_review", change_after_digest)
    else:
        original = external.begin_metered_external_execution
        def change_at_metering(**kwargs):
            change_rate()
            return original(**kwargs)
        monkeypatch.setattr(external, "begin_metered_external_execution", change_at_metering)
    monkeypatch.setattr(outreach, "_send_one", lambda *a: pytest.fail("The changed credit cost was not approved"))
    result = asyncio.run(journey.send(d["wid"], d["vid"], request))
    assert changed and result["results"]["sent"] == 0
    assert result["drafts"][0]["state"] == "draft" and result["drafts"][0]["review_state"] == "draft"
    with d["factory"]() as session:
        _assert_credit_usage(d, session, spent_micro=0, reserved_micro=0)


def test_ambiguous_delivery_holds_quota_and_never_retries(journey_db, monkeypatch):
    from app.autopilot import outreach
    from app.db.models import CreditAccount, GtmTouch
    from app.gtm import journey
    d = journey_db
    _add(d, monkeypatch)
    _mail(d)
    state, request = _review(d)
    calls = []
    def timeout(*args):
        calls.append("attempt")
        raise TimeoutError("provider acknowledgement lost")
    monkeypatch.setattr(outreach, "_send_one", timeout)
    result = asyncio.run(journey.send(d["wid"], d["vid"], request))
    assert result["drafts"][0]["state"] == "delivery_unconfirmed" and result["results"]["sent"] == 0, result["drafts"][0].get("error")
    assert outreach.send_slots_used_today(d["vid"]) == 1
    with d["factory"]() as session:
        _assert_credit_usage(d, session, spent_micro=0, reserved_micro=10_000)
        assert not session.scalar(select(GtmTouch.id).where(GtmTouch.venture_id == d["vid"]))
    with pytest.raises(ValueError, match="gtm_approval_changed"):
        asyncio.run(journey.send(d["wid"], d["vid"], request))
    assert len(calls) == 1


def test_missing_gmail_and_late_optout_prevent_provider(journey_db, monkeypatch):
    from app.autopilot import outreach
    from app.db.models import GtmSuppression
    from app.gtm import journey
    from core.clock import utcnow
    d = journey_db
    _add(d, monkeypatch)
    state, request = _review(d)
    monkeypatch.setattr(outreach, "_send_one", lambda *a: pytest.fail("A held send cannot call Gmail"))
    with pytest.raises(ValueError, match="gtm_gmail_required"):
        asyncio.run(journey.send(d["wid"], d["vid"], request))
    _mail(d)
    state, request = _review(d)
    with d["factory"]() as session:
        session.add(GtmSuppression(id="sup_late", workspace_id=d["wid"], email="ada@bakery.example", source="opt_out", created_at=utcnow()))
        session.commit()
    with pytest.raises(ValueError, match="^suppressed_or_replied$"):
        asyncio.run(journey.send(d["wid"], d["vid"], request))


def test_foreign_venture_and_unauthorized_worker_cannot_read_or_prepare(journey_db):
    from app.core.jobs import FatalError
    from app.gtm import journey
    d = journey_db
    with pytest.raises(ValueError):
        journey.status("foreign_workspace", d["vid"])
    with pytest.raises(FatalError, match="scope_or_lease"):
        asyncio.run(journey.run_prepare({"workspace_id": d["wid"], "venture_id": d["vid"]}))


def test_followups_keep_journey_review_and_replies_are_attributed(journey_db, monkeypatch):
    from app.autopilot import outreach
    from app.core.local_mode import is_local
    from app.gtm import journey
    from app.gtm.supymem import composio_gmail, workers
    from core.clock import utcnow
    monkeypatch.setattr(composio_gmail, "fetch_query", lambda **kwargs: {"messages": []})
    d = journey_db
    _add(d, monkeypatch, country="US", basis="legitimate_interest")
    _mail(d)
    postal = "1 Main Street, New York, NY 10001"
    journey.update_settings(d["wid"], d["vid"], {"sender_postal_address": postal})
    state, request = _review(d)
    monkeypatch.setattr(outreach, "_send_one", lambda *a: {"provider_message_id": "m1", "thread_id": "thread-1"})
    result = asyncio.run(journey.send(d["wid"], d["vid"], request))
    first = result["drafts"][0]
    outreach._save_draft(d["vid"], first | {"sent_at": (utcnow() - timedelta(days=4)).isoformat()}, first["doc_id"])
    if is_local():
        from app.core.local_secrets import update_settings
        assert outreach.schedule_followups(d["vid"]) == 0
        update_settings("capabilities", {"background_work": True}, {})
        journey.update_settings(d["wid"], d["vid"], {"allow_sending": True})
    assert outreach.schedule_followups(d["vid"]) == 1
    followup = outreach.drafts(d["vid"], "draft")[0]
    assert followup["requires_journey_review"] and followup["review_state"] == "draft"
    assert postal in followup["body"]
    monkeypatch.setattr(workers, "tick_bounces", lambda *args, **kwargs: {"marked": []})
    monkeypatch.setattr(composio_gmail, "fetch_thread", lambda **kwargs: {"messages": [{"id": "unrelated", "threadId": "other-thread", "snippet": "Yes, interested"}]})
    assert asyncio.run(outreach.poll_replies(d["vid"], force=True)) == 0
    monkeypatch.setattr(composio_gmail, "fetch_thread", lambda **kwargs: {"messages": [{"id": "reply-1", "threadId": "thread-1", "snippet": "Yes, interested"}]})
    assert asyncio.run(outreach.poll_replies(d["vid"], force=True)) == 1
    state = journey.status(d["wid"], d["vid"])
    assert state["results"]["replied"] == 1 and state["results"]["interested"] == 1
    assert asyncio.run(outreach.poll_replies(d["vid"], force=True)) == 0


@pytest.mark.parametrize("body,headers,expected", [
    ("Automatic reply: I am out of the office. Please book time with my assistant.", None, "automated"),
    ("I forwarded this to our bookkeeping team yesterday.", None, "human_neutral"),
    ("Yes, interested in a demo", None, "human_positive"),
    ("Let's talk. We can book a call.", None, "human_positive"),
    ("Yes, sounds good", {"Auto-Submitted": "auto-replied"}, "automated"),
    ("Yes, sounds good", [{"name": "Auto-Submitted", "value": "auto-generated"}], "automated"),
    ("Yes, sounds good", {"Auto-Submitted": "no"}, "human_positive"),
    ("Not interested; remove me", {"Auto-Submitted": "auto-replied"}, "automated"),
    ("Acknowledged. Unsubscribe from these updates.", [{"name": "Auto-Submitted", "value": "auto-replied"}], "automated"),
    ("Not interested; remove me", {"Auto-Submitted": "no"}, "human_negative"),
    ("Remove me; I am on vacation.", None, "human_negative"),
])
def test_reply_triage_uses_automation_evidence_and_phrase_boundaries(body, headers, expected):
    load_company_os_app(reset=False)
    from app.gtm.supymem.workers import _classify_reply
    assert _classify_reply(body, headers=headers) == expected


@pytest.mark.parametrize("body,headers,opt_out", [
    ("Automatic reply: I am out of the office. Please book time with my assistant.", None, False),
    ("Yes, interested", [{"name": "Auto-Submitted", "value": "auto-replied"}], False),
    ("Acknowledged. Unsubscribe from these updates.", {"Auto-Submitted": "auto-replied"}, True),
    ("Not interested; remove me", [{"name": "Auto-Submitted", "value": "auto-generated"}], True),
])
def test_automated_attributed_response_pauses_followups_without_human_demand(journey_db, monkeypatch, body, headers, opt_out):
    from app.autopilot import outreach, pmf
    from app.chat import milestones
    from app.db.models import AnalyticsEvent, GtmContact, GtmLearningEvent, GtmSuppression, GtmTouch, Venture
    from app.email import triggers
    from app.gtm import journey
    from app.gtm.supymem import composio_gmail
    from core.clock import utcnow
    monkeypatch.setattr(composio_gmail, "fetch_query", lambda **kwargs: {"messages": []})
    d = journey_db
    _add(d, monkeypatch)
    _mail(d)
    state, request = _review(d)
    monkeypatch.setattr(outreach, "_send_one", lambda *a: {"provider_message_id": "owned-send", "thread_id": "owned-thread"})
    sent = asyncio.run(journey.send(d["wid"], d["vid"], request))["drafts"][0]
    outreach._save_draft(d["vid"], sent | {"sent_at": (utcnow() - timedelta(days=4)).isoformat()}, sent["doc_id"])
    notes = []
    monkeypatch.setattr(milestones, "post_milestone", lambda *a, **kw: notes.append(kw))
    monkeypatch.setattr(composio_gmail, "fetch_thread", lambda **kw: {"messages": [{
        "id": "owned-auto", "threadId": "owned-thread", "snippet": body, "headers": headers}]})
    from app.gtm.supymem import pipeline, workers
    def legacy_automated_record(workspace_id):
        if not pipeline.apply_reply(sent["contact_id"], body=body, classification="automated", venture_id=d["vid"],
                provider_thread_id="owned-thread", provider_message_id="owned-auto"):
            return {"replies": []}
        return {"replies": [{"contact_id": sent["contact_id"], "venture_id": d["vid"], "kind": "automated", "body": body, "message_id": "owned-auto"}]}
    monkeypatch.setattr(workers, "tick_replies", legacy_automated_record)
    monkeypatch.setattr(workers, "tick_bounces", lambda *args, **kwargs: {"marked": []})
    assert asyncio.run(outreach.poll_replies(d["vid"], force=True)) == 1
    result = journey.status(d["wid"], d["vid"])["results"]
    assert result["replied"] == result["interested"] == result["negative"] == 0
    assert result["replies"][0]["classification"] == "automated"
    assert notes[0]["kind"] == "outreach_reply" and "automated response" in notes[0]["body_md"]
    assert "person answered" not in notes[0]["body_md"] and "sounds interested" not in notes[0]["body_md"]
    with d["factory"]() as session:
        contact = session.get(GtmContact, sent["contact_id"])
        assert contact.replied_at is not None and contact.do_not_contact == int(opt_out) and contact.relationship_stage == "cooling"
        assert (contact.unsubscribed_at is not None) is opt_out
        assert bool(session.scalar(select(GtmSuppression.id).where(GtmSuppression.workspace_id == d["wid"], GtmSuppression.email == contact.email))) is opt_out
        kinds = list(session.scalars(select(GtmLearningEvent.kind).where(GtmLearningEvent.contact_id == contact.id)))
        assert "reply_automated" in kinds and "reply_human" not in kinds and "reply_positive" not in kinds
        touch = session.scalar(select(GtmTouch).where(GtmTouch.contact_id == contact.id, GtmTouch.direction == "in"))
        venture = session.get(Venture, d["vid"])
        assert not triggers._real_touch(touch, venture, "in")
        eligible = []
        triggers._outreach(session, venture, d["actor"], triggers._naive(utcnow() + timedelta(hours=2)), lambda owner, template, *a, **kw: eligible.append(template))
        assert "replies_received" not in eligible
        assert not session.scalar(select(AnalyticsEvent.id).where(AnalyticsEvent.venture_id == d["vid"], AnalyticsEvent.event_name == "outreach.reply_received"))
    assert pmf.read_metric(d["vid"], "outbound_replies") == 0
    assert outreach.schedule_followups(d["vid"]) == 0
    assert asyncio.run(outreach.poll_replies(d["vid"], force=True)) == 0


@pytest.mark.parametrize("address", ["1 Main Street, New York, NY 10001", "88 Harbour Road, Portland, ME 04101"])
def test_address_and_terms_saved_together_finish_in_one_request(journey_db, address):
    from app.gtm.relationships.service import _settings, patch_onboarding
    from app.state import db

    d = journey_db
    patch_onboarding(d["wid"], {"sender_postal_address": address, "aup_accepted": True, "dpa_accepted": True})
    with db()() as session:
        settings = _settings(session, d["wid"])
        assert settings.sender_postal_address == address
        assert settings.aup_accepted_at is not None and settings.dpa_accepted_at is not None


def test_us_postal_address_recovers_draft_and_admin_pause_blocks_send(journey_db, monkeypatch):
    from app.gtm import journey
    from app.gtm.relationships.abuse import pause_sends
    d = journey_db
    _mail(d, sending_address=False)
    state = _add(d, monkeypatch, country="US", basis="legitimate_interest")
    assert not state["drafts"] and state["prospects"][0]["blocked_reason"] == "us_postal_address_required"
    state = journey.update_settings(d["wid"], d["vid"], {"sender_postal_address": "1 Main Street, New York, NY 10001"})
    assert state["sender_postal_address"] == "1 Main Street, New York, NY 10001" and len(state["drafts"]) == 1
    assert state["sender_postal_address"] in state["drafts"][0]["body"]
    state, request = _review(d)
    pause_sends(d["wid"])
    with pytest.raises(ValueError, match="^send_paused$"):
        asyncio.run(journey.send(d["wid"], d["vid"], request))


@pytest.mark.hosted_only
def test_hosted_policy_acceptance_is_explicit_before_processing(journey_db, monkeypatch):
    from app.db.models import Workspace
    from app.gtm import journey
    d = journey_db
    with d["factory"]() as session:
        session.get(Workspace, d["wid"]).tenancy = "managed"
        session.commit()
    with pytest.raises(ValueError, match="gtm_aup_required"):
        _add(d, monkeypatch)
    assert journey.prepare(d["wid"], d["vid"])["processing_blocked_reason"] == "aup_required"
    state = journey.update_settings(d["wid"], d["vid"], {"aup_accepted": True})
    assert state["processing_blocked_reason"] == "dpa_required"
    state = journey.update_settings(d["wid"], d["vid"], {"dpa_accepted": True})
    assert state["processing_blocked_reason"] is None
    assert _add(d, monkeypatch)["drafts"]


@pytest.mark.hosted_only
def test_sender_charges_shared_founder_wallet_once_with_exact_venture_attribution(journey_db, monkeypatch):
    import json
    from app.credits.accounts import ensure_account
    from app.db.models import CreditAccount, CreditLedgerEntry, Venture
    from app.gtm import journey
    from app.autopilot import outreach
    from core.clock import utcnow
    d = journey_db
    with d["factory"]() as session:
        session.add(Venture(id="ven_sibling", workspace_id=d["wid"], name="Sibling", raw_idea="Another business", stage="draft", created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    sibling = ensure_account("ven_sibling", workspace_id=d["wid"], owner_person_id=d["actor"])
    assert sibling.id == d["account"] and sibling.scope == "person"
    monkeypatch.setattr("app.approvals.external.account_for_workspace",
        lambda wid: pytest.fail("Venture send must resolve its explicit venture payer"))
    _add(d, monkeypatch)
    _mail(d)
    state, request = _review(d)
    sent = []
    monkeypatch.setattr(outreach, "_send_one", lambda *a: sent.append(a) or {
        "provider_message_id": "m_exact", "thread_id": "t_exact"})
    assert asyncio.run(journey.send(d["wid"], d["vid"], request))["results"]["sent"] == 1
    with pytest.raises(ValueError, match="gtm_approval_changed"):
        asyncio.run(journey.send(d["wid"], d["vid"], request))
    assert len(sent) == 1
    with d["factory"]() as session:
        assert session.get(CreditAccount, d["account"]).spent_micro == 10_000
        entries = session.scalars(select(CreditLedgerEntry).where(
            CreditLedgerEntry.account_id == sibling.id, CreditLedgerEntry.kind == "spend")).all()
        assert len(entries) == 1 and entries[0].amount_micro == -10_000
        attribution = json.loads(entries[0].meta_json)
        assert attribution["venture_id"] == d["vid"] and attribution["workspace_id"] == d["wid"]


def test_definite_failure_refunds_and_new_review_can_retry(journey_db, monkeypatch):
    from app.autopilot import outreach
    from app.db.models import CreditAccount
    from app.gtm import journey
    from app.gtm.supymem.composio_gmail import DefiniteFailure
    d = journey_db
    _add(d, monkeypatch)
    _mail(d)
    state, request = _review(d)
    def refused(*args):
        raise DefiniteFailure("expired_oauth")
    monkeypatch.setattr(outreach, "_send_one", refused)
    result = asyncio.run(journey.send(d["wid"], d["vid"], request))
    assert result["drafts"][0]["state"] == "draft" and result["results"]["sent"] == 0
    with d["factory"]() as session:
        _assert_credit_usage(d, session, spent_micro=0, reserved_micro=0)
    state, request = _review(d)
    monkeypatch.setattr(outreach, "_send_one", lambda *a: {"provider_message_id": "retry-receipt", "thread_id": "retry-thread"})
    assert asyncio.run(journey.send(d["wid"], d["vid"], request))["results"]["sent"] == 1


def test_partial_batch_preserves_exact_outcomes_credits_and_later_success(journey_db, monkeypatch):
    from app.autopilot import outreach
    from app.db.models import CreditAccount, GtmTouch
    from app.gtm import journey

    d = journey_db
    for name, email in (("Ada", "ada@bakery.example"), ("Bob", "bob@bakery.example"), ("Cara", "cara@bakery.example")):
        journey.add_prospect(d["wid"], d["vid"], {"name": name, "company": name + " Bakery", "email": email,
            "source_url": f"https://{name.lower()}bakery.example/contact", "country": "CA", "lawful_basis": "consent",
            "lawful_basis_note": "The contact agreed to hear about the bakery order board."})
    journey._draft_ready(d["wid"], d["vid"])
    rows = outreach.drafts(d["vid"])
    batch = rows[0]["batch_id"]
    for row in rows:
        outreach._save_draft(d["vid"], row | {"batch_id": batch}, row["doc_id"])
    _mail(d)
    state, request = _review(d)
    calls = []
    def provider(box, to, subject, body, thread):
        calls.append(to)
        if len(calls) == 2:
            raise TimeoutError("Owned provider receipt lost")
        return {"provider_message_id": f"owned-{len(calls)}", "thread_id": f"thread-{len(calls)}"}
    monkeypatch.setattr(outreach, "_send_one", provider)
    result = asyncio.run(journey.send(d["wid"], d["vid"], request))
    assert sorted(row["state"] for row in result["drafts"]) == ["delivery_unconfirmed", "draft", "sent"]
    assert len(calls) == 2 and result["results"]["sent"] == 1
    with d["factory"]() as session:
        _assert_credit_usage(d, session, spent_micro=10_000, reserved_micro=10_000)
        assert len(list(session.scalars(select(GtmTouch).where(GtmTouch.venture_id == d["vid"])))) == 1
    with pytest.raises(ValueError, match="gtm_approval_changed"):
        asyncio.run(journey.send(d["wid"], d["vid"], request))
    assert len(calls) == 2
    remaining = next(row for row in result["drafts"] if row["state"] == "draft")
    state = journey.patch_draft(d["wid"], d["vid"], remaining["doc_id"], {"decision": "approved", "expected_revision": remaining["revision"]})
    next_request = {k: state["batches"][0][k] for k in ("batch_id", "draft_ids", "approval_digest", "cap")}
    later = asyncio.run(journey.send(d["wid"], d["vid"], next_request))
    assert len(calls) == 3 and later["results"]["sent"] == 2
    assert sorted(row["state"] for row in later["drafts"]) == ["delivery_unconfirmed", "sent", "sent"]
    with d["factory"]() as session:
        _assert_credit_usage(d, session, spent_micro=20_000, reserved_micro=10_000)
        assert len(list(session.scalars(select(GtmTouch).where(GtmTouch.venture_id == d["vid"])))) == 2
    with pytest.raises(ValueError, match="gtm_approval_changed"):
        asyncio.run(journey.send(d["wid"], d["vid"], next_request))
    assert len(calls) == 3


def test_search_queries_are_noun_phrases_without_trailing_clauses():
    from app.gtm import journey
    assert journey._search_queries("Independent bakery owners") == ["Independent bakery owners"]
    qs = journey._search_queries("Shopify agencies managing multiple client stores regularly ingest product feeds")
    assert qs == ["Shopify agencies managing multiple client stores"]
    qs = journey._search_queries("Founders improving customer outreach")
    assert qs[-1].endswith("outreach companies") and not any("regularly" in q for q in qs)


def test_search_again_resets_budget_but_keeps_unsettled_calls(journey_db, monkeypatch):
    from app.gtm import journey
    async def empty(who, search_queries=None, **kwargs):
        return []
    monkeypatch.setattr(journey, "_discover", empty)
    d = journey_db
    _work(d, journey.update_icp(d["wid"], d["vid"], "Independent bakery owners"))
    exhausted = {f"discovery-passages-{index}": 1 for index in range(60)} | {"signal_search": 8, "discovery-plan": 1}
    with journey.db()() as session:
        row = journey._row(session, d["vid"])
        data = journey._json(row.provenance_json)
        data["_runtime"]["calls_used"] = exhausted
        data["_runtime"]["operations"]["discovery-plan"] = {"state": "uncertain", "attempt": 1}
        journey._store(session, row, data)
        session.commit()
    _expire_search_replay(d, data["job_id"])
    journey.prepare(d["wid"], d["vid"], retry=True)
    with journey.db()() as session:
        runtime = journey._json(journey._row(session, d["vid"]).provenance_json)["_runtime"]
    # The unknown-outcome call is never redialled; everything else gets a fresh budget.
    assert runtime["operations"] == {"discovery-plan": {"state": "uncertain", "attempt": 1}}
    assert runtime["calls_used"] == {"discovery-plan": 1}


def test_saved_leads_outside_the_audience_country_are_hidden_across_domains():
    from app.gtm.journey import in_audience_geography

    cases = [
        ({"who": "independent bakeries in the United States"}, "https://crumbhouse.pk/", "https://sample-oven.test/"),
        ({"who": "dental clinics", "geography": "Australia"}, "https://smileworks.ae/", "https://brightteeth.com.au/"),
    ]
    for icp, foreign, local in cases:
        assert not in_audience_geography(icp, {"name": "Lead", "source_url": foreign})
        assert in_audience_geography(icp, {"name": "Lead", "source_url": local})
        assert in_audience_geography({"who": "anyone"}, {"name": "Lead", "source_url": foreign})

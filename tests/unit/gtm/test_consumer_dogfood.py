"""Consumer discovery regressions use synthetic public-source fixtures; no live network or sends."""
import asyncio
import json
from datetime import timedelta

import pytest

from tests.unit.gtm.test_journey import journey_db as journey_db, _work
from tests.unit.gtm.test_journey_signals import _leased_payload, quality_response

AUDIENCE = "US parents of grade-school children"
PROBLEM = "Tracking school forms"


@pytest.mark.parametrize("fallback", ["results", "failed", "empty", "robots_denied"])
def test_consumer_primary_outage_uses_bounded_public_fallback_and_retry(journey_db, monkeypatch, fallback):
    from app.gtm import journey, scrapy_leads
    from app.research.prior_art import adapters
    from app.signals.research import DuckDuckGoProvider, HttpResearchProvider
    from core.clock import utcnow

    source = "https://forum.example/threads/school-forms"
    quote = "I need help tracking school forms and keep missing the deadlines."
    calls = {"primary": [], "public": [], "pages": []}
    async def primary(self, query, **kwargs):
        calls["primary"].append(query)
        error = RuntimeError("provider unavailable")
        error.status = 429
        raise error
    async def public(self, query):
        calls["public"].append(query)
        if fallback == "failed":
            error = RuntimeError("public search unavailable")
            error.status = 429
            raise error
        return [] if fallback == "empty" else [{"url": source, "title": "School forms discussion", "snippet": "Parents discuss tracking school forms"},
            {"url": "http://127.0.0.1/private", "title": "Private", "snippet": quote}]
    def fetch(url, *, allow_missing=False):
        calls["pages"].append(url)
        if url.endswith("/robots.txt"):
            assert allow_missing
            return "User-agent: *\nDisallow: /" if fallback == "robots_denied" else "User-agent: *\nAllow: /"
        assert url == source
        return f"Parents in the United States\n{utcnow().date().isoformat()}\n{quote}"
    async def scrape(self, url, **kwargs):
        pytest.fail("Public fallback must not use Firecrawl pages")
    async def hn(*args, **kwargs):
        return {"items": []}
    async def model(payload, stage, context):
        if stage in {"marketrole", "passages"}:
            return quality_response(stage, context)
        if stage == "plan":
            return {"queries": [{"query": f"school forms community topic {index}", "intent": "customer_voice"} for index in range(8)]}
        if stage == "judge":
            assert context["_fallback"]
            return {"items": [{"id": row["id"], "kind": "customer_voice", "quote": quote, "affiliation": "", "name": "", "url": row["url"]}
                for row in context["candidates"]]}
        return {"items": []}
    monkeypatch.setenv("FIRECRAWL_API_KEY", "synthetic")
    from app.core.local_mode import is_local

    if is_local():
        from app.core.local_secrets import update_settings

        update_settings("search", {"provider": "firecrawl", "firecrawl_url": "https://reader.example"}, {"firecrawl_key": "synthetic"})
    monkeypatch.setattr(HttpResearchProvider, "search", primary)
    monkeypatch.setattr(HttpResearchProvider, "scrape", scrape)
    monkeypatch.setattr(DuckDuckGoProvider, "search", public)
    monkeypatch.setattr(scrapy_leads, "_fetch", fetch)
    monkeypatch.setattr(adapters, "search_hn", hn)
    monkeypatch.setattr(journey, "_discovery_model", model)

    view = journey.update_icp(journey_db["wid"], journey_db["vid"], AUDIENCE, problem=PROBLEM, recruitment_opt_in=True)
    for attempt in range(2):
        _work(journey_db, view)
        view = journey.status(journey_db["wid"], journey_db["vid"])
        assert len(calls["primary"]) == 2 * (attempt + 1) * journey.CALL_CEILINGS["signal_search"]
        if is_local():
            assert calls["public"] == calls["pages"] == []
            assert view["state"] != "ready" and view["error_code"]
            with journey_db["factory"]() as session:
                document = json.loads(journey._row(session, journey_db["vid"]).provenance_json)
                assert document["source_access"] == "unavailable"
            if not attempt:
                view = journey.prepare(journey_db["wid"], journey_db["vid"], retry=True, recruitment_opt_in=True)
            continue
        assert len(calls["public"]) == (attempt + 1 if fallback == "failed" else 1) * journey.CALL_CEILINGS["public_search"]
        assert "public web sources" in view["discovery_notice"] or attempt and fallback == "results"
        assert "No public profiles found" not in view["people_search_notice"]
        if fallback == "results":
            assert view["state"] == "ready"
            if not attempt:
                assert not view["error_code"]
            assert view["need_signals"][0]["source_url"] == source
            assert view["need_signals"][0]["quote"] == quote
            assert source in calls["pages"]
        else:
            assert view["state"] == "needs_input" and view["error_code"] == "public_search_unavailable"
            assert view["source_access"] == "unavailable" and not view["need_signals"]
            assert view["people_search_notice"] == "Search is temporarily unavailable, so we couldn't look for people this time. Nothing was lost. Try again in a few minutes."
            assert source not in calls["pages"]
        with journey_db["factory"]() as session:
            row = journey._row(session, journey_db["vid"])
            runtime = json.loads(row.provenance_json)["_runtime"]
        assert runtime["calls_used"].get("public_search", 0) <= journey.CALL_CEILINGS["public_search"]
        assert runtime["calls_used"].get("public_page", 0) <= journey.CALL_CEILINGS["public_page"]
        assert not any("127.0.0.1" in url for url in calls["pages"])
        if not attempt:
            view = journey.prepare(journey_db["wid"], journey_db["vid"], retry=True, recruitment_opt_in=True)


@pytest.mark.parametrize("audience,context,consumer", [
    ("first-time US dog owners", {}, True),
    ("busy US parents of picky toddlers", {}, True),
    ("small accounting firms (5-50 staff)", {}, False),
    ("bakery owners", {}, False),
    ("salon owners", {}, False),
    ("gym owners in Austin", {}, False),
    ("Airbnb hosts", {}, False),
    ("first-time managers", {}, False),
    ("freelance designers", {}, False),
    ("homeowners in Texas", {}, True),
    ("pet owners", {}, True),
    ("travellers", {}, True),
    ("gamers", {}, True),
    ("patients", {}, True),
    ("shoppers", {}, True),
    ("runners", {}, True),
    ("couples", {}, True),
    ("renters", {}, True),
    ("new owners of bicycles", {}, True),
    ("new computer owners", {}, True),
    ("owners of a business", {}, False),
    ("owners of a practice", {}, False),
    ("owners of a store", {}, False),
    ("first-time clinic owners", {}, False),
    ("first-time restaurant owners", {}, False),
    ("parents working in accounting teams", {}, False),
    ("roles at firms", {}, False),
    ("unspecified audience", {}, False),
    ("unspecified audience", {"idea": "Puppy training app", "pricing_hypothesis": "$8/month"}, True),
    ("unspecified audience", {"idea": "Puppy training app", "pricing": "free + premium"}, True),
    ("teams", {"idea": "Team scheduling app", "pricing": "$8/month"}, False),
    ("dog owners", {"customer_type": "B2B"}, False),
    ("firms", {"customer_type": "B2C"}, True),
    ("dog owners", {"business_model": "B2B subscription"}, False),
    ("unspecified audience", {"business_model": "B2C subscription"}, True),
    ("dog owners", {"business_model": "Subscription for businesses"}, False),
    ("unspecified audience", {"idea": "B2B accounting app", "pricing": "$8/month"}, False),
])
def test_shared_consumer_classifier_and_gstack_wording(journey_db, audience, context, consumer):
    from app.gstack import office_hours
    from app.gtm import journey
    from app.idea.classify import is_consumer
    icp = {**context, "who": audience}
    assert is_consumer(icp) is consumer
    assert journey._consumer(audience, context) is consumer
    picks = office_hours.default_picks({**context, "audience": audience})
    assert ("life situation" in picks[2]["text"]) is consumer
    plan = journey._fallback_plan(audience, "daily training", [], context=icp)
    assert any(item["intent"] == "business" for item in plan) is not consumer
    assert bool(journey._decision_maker_queries(audience, context=icp)) is not consumer


@pytest.mark.parametrize("audience,idea,consumer", [
    ("balcony growers", "A personal planting diary app for tracking growth", True),
    ("weekend bakers", "A pantry and meal preparation app for households", True),
    ("warehouse coordinators", "A household delivery dispatch app for operations teams", False),
    ("restaurant chefs", "A personal recipe costing app for restaurant companies", False),
])
def test_personal_use_apps_keep_their_consumer_scope(journey_db, audience, idea, consumer):
    from app.gstack import office_hours
    from app.gtm import journey
    from app.idea.classify import is_consumer

    context = {"idea": idea}
    assert is_consumer(audience, context) is consumer
    assert journey._consumer(audience, context) is consumer
    assert ("life situation" in office_hours.default_picks(context | {"audience": audience})[2]["text"]) is consumer
    assert any(item["intent"] == "business" for item in journey._fallback_plan(audience, "tracking progress", [], context=context)) is not consumer
    assert not is_consumer(audience, context | {"customer_type": "B2B"})


@pytest.mark.parametrize("sector,audience,idea,context,consumer", [
    ("enterprise", "people who are procurement directors", "Purchase approval workflows for large employers", {}, False),
    ("enterprise", "people working as operations managers", "Workforce shift planning and absence coverage", {}, False),
    ("enterprise", "people who are security officers", "Incident response coordination for corporate security", {}, False),
    ("enterprise", "customer support directors", "Call routing and service-level reporting", {}, False),
    ("enterprise", "people who are compliance administrators", "Employee access reviews and audit evidence", {}, False),
    ("enterprise", "parents returning to work", "Personal calendar app for balancing childcare and commuting", {}, True),
    ("enterprise", "students applying for internships", "Application tracker app with interview reminders", {}, True),
    ("enterprise", "people looking for jobs advertised by hiring managers", "Personal job search and interview preparation app", {}, True),
    ("enterprise", "individuals learning spreadsheet skills", "Self-paced spreadsheet lessons for career changes", {}, True),
    ("enterprise", "people preparing for a manager interview", "Interview practice app for individual job seekers", {}, True),
    ("health", "US hospital patient flow directors", "ICU bed transfer and handoff coordination software", {}, False),
    ("health", "people who are patient flow directors at US hospitals", "Ward transfer scheduling and escalation workflow", {}, False),
    ("health", "hospital patient safety managers", "Incident review and quality improvement workflows", {}, False),
    ("health", "people who are clinical operations coordinators", "Outpatient appointment capacity planning", {}, False),
    ("health", "patient access directors", "Referral intake and appointment backlog management", {}, False),
    ("health", "individual patients tracking blood pressure", "Personal blood pressure diary and reminder app", {}, True),
    ("health", "patients at hospitals", "App for tracking personal recovery after discharge", {}, True),
    ("health", "parents with children needing pediatric care", "Personal symptom diary and appointment reminder app", {}, True),
    ("health", "patients who consult medical directors", "Second-opinion appointment preparation app", {}, True),
    ("health", "people seeking care from clinical managers", "Personal care navigation and symptom tracking app", {}, True),
    ("fintech", "people who are payment operations directors", "Cross-border payment exception reconciliation", {}, False),
    ("fintech", "people working as treasury analysts", "Liquidity forecasting and settlement monitoring", {}, False),
    ("fintech", "people who are fraud risk officers", "Transaction investigation and case review workflows", {}, False),
    ("fintech", "people who are settlement engineers", "Payment ledger reconciliation infrastructure", {}, False),
    ("fintech", "people who are financial reporting accountants", "Multi-currency close and audit reporting", {}, False),
    ("fintech", "people managing personal spending", "Household budget and bill reminder app", {}, True),
    ("fintech", "individuals comparing mortgage offers", "Personal mortgage affordability calculator", {}, True),
    ("fintech", "students tracking student debt", "Personal student loan repayment planner", {}, True),
    ("fintech", "people advised by financial managers", "Personal investment goal tracking app", {}, True),
    ("fintech", "parents who are bank officers", "Personal savings app for their children's education", {}, True),
    ("hardware", "people who are plant maintenance engineers", "Predictive equipment maintenance sensors", {}, False),
    ("hardware", "people who are production managers", "Factory downtime monitoring hardware", {}, False),
    ("hardware", "people who are component sourcing directors", "Electronic component supply qualification", {}, False),
    ("hardware", "people who are quality assurance coordinators", "Automated production-line inspection cameras", {}, False),
    ("hardware", "people who are safety officers", "Industrial hazard monitoring wearables", {}, False),
    ("hardware", "homeowners monitoring indoor air quality", "Home air quality sensor and companion app", {}, True),
    ("hardware", "people repairing personal computers", "Home computer repair kit and tutorial app", {}, True),
    ("hardware", "home users organizing connected devices", "Personal smart home device manager", {}, True),
    ("hardware", "gamers customizing keyboards", "Programmable mechanical keyboard kit", {}, True),
    ("hardware", "parents monitoring sleeping babies", "Home baby monitor with sleep alerts", {}, True),
    ("space", "people who are mission operations directors", "Satellite fleet maneuver coordination", {}, False),
    ("space", "people who are flight software engineers", "Orbital fault diagnosis and replay tooling", {}, False),
    ("space", "people who are launch integration managers", "Payload integration and readiness tracking", {}, False),
    ("space", "people who are payload program coordinators", "Satellite payload verification workflow", {}, False),
    ("space", "people who are satellite mission analysts", "Orbital collision risk and maneuver planning", {}, False),
    ("space", "astronomy enthusiasts", "Stargazing app with observing recommendations", {}, True),
    ("space", "hobbyists assembling telescopes", "Home telescope kit and alignment app", {}, True),
    ("space", "families spotting satellites", "Satellite visibility alerts for backyard observers", {}, True),
    ("space", "students studying orbital mechanics", "Interactive orbital mechanics learning app", {}, True),
    ("space", "people visiting a space center with mission directors", "Personal visitor itinerary and exhibit guide app", {}, True),
    ("apps", "people who are hotel operations managers", "Room turnover and housekeeping dispatch app", {}, False),
    ("apps", "people who are library program coordinators", "Public event staffing and room booking app", {}, False),
    ("apps", "people who are community recreation directors", "Facility scheduling and participation reporting", {}, False),
    ("apps", "people who are transport dispatch managers", "Passenger service disruption coordination", {}, False),
    ("apps", "people who are museum visitor experience directors", "Exhibit capacity and queue management", {}, False),
    ("apps", "people who meditate before bed", "Personal meditation and sleep routine app", {}, True),
    ("apps", "runners training for a marathon", "Personal training plan and recovery tracker", {}, True),
    ("apps", "parents who coordinate children's activities", "Family activity calendar and reminders", {}, True),
    ("apps", "renters splitting household expenses", "Shared household bill tracking app", {}, True),
    ("apps", "people who are first-time parents", "Baby feeding and sleep diary app", {}, True),
    ("enterprise", "PEOPLE WHO ARE PAYMENT OPERATIONS DIRECTORS", "Payment exception review workflow", {}, False),
    ("health", "hospital patient-flow directors", "Bed availability and ward handoff monitoring", {}, False),
    ("health", "hospital patient flow directors", "Personal sleep and fitness app", {"customer_type": "B2C"}, True),
    ("fintech", "people who are treasury managers", "Personal spending diary app", {"business_model": "B2C subscription"}, True),
    ("hardware", "people who are maintenance engineers", "Home woodworking project planner", {"audience_type": {"value": "individual consumers"}}, True),
    ("space", "people who are flight engineers", "Personal stargazing journal", {"customer_type": "personal"}, True),
    ("health", "patients whose care coordinator is unavailable", "Personal appointment and symptom journal", {}, True),
    ("health", "people looking for a medical director for a second opinion", "Personal second-opinion preparation app", {}, True),
    ("health", "individual patients referred to care coordinators", "Personal referral status and reminder app", {}, True),
    ("health", "patients with a patient safety manager handling their complaint", "Personal medical record and complaint tracker", {}, True),
    ("health", "patients visiting medical directors", "Personal visit preparation and medication diary", {}, True),
    ("health", "people who are parents with advice from medical directors", "Family symptom and medication journal", {}, True),
    ("enterprise", "people who are operations managers", "Personal task diary and productivity app", {}, True),
    ("fintech", "people who are treasury directors", "Personal spending diary and savings app", {}, True),
    ("hardware", "people who are maintenance engineers", "Personal home woodworking project planner", {}, True),
    ("space", "people who are mission directors", "Personal stargazing journal app", {}, True),
    ("health", "individual patients awaiting care coordinators", "Appointment preparation and symptom diary app", {}, True),
    ("apps", "parents meeting school directors", "Family school meeting and activity planner", {}, True),
    ("enterprise", "people who are procurement directors", "Purchase approval and audit workflows", {"customer_type": {"value": "people"}}, False),
    ("apps", "unspecified audience", "Personal reminder app", {"customer_type": "people"}, True),
    ("apps", "parents comparing programs from tutoring companies", "Family education program comparison app", {"customer_type": "people"}, True),
    ("health", "patients attending physical therapy clinics", "Personal rehabilitation and appointment diary", {"customer_type": "people"}, True),
    ("health", "patient awaiting a care coordinator", "Appointment and care navigation app", {}, True),
    ("health", "patient needing a hospital director", "Medical complaint and visit preparation app", {}, True),
    ("apps", "people who are operations managers", "Daily relaxation reminder app", {"customer_type": "people", "business_model": "B2C subscription"}, True),
    ("apps", "people who are patient safety directors and parents", "Family schedule coordination app", {}, True),
])
def test_professional_role_context_preserves_consumer_gate(sector, audience, idea, context, consumer):
    from tests.company_os_harness import load_company_os_app
    load_company_os_app()
    from app.gstack import office_hours
    from app.gtm import journey
    from app.idea.classify import is_consumer

    context = {**context, "idea": idea}
    assert is_consumer(audience, context) is consumer, (sector, audience)
    for field in ("who", "customer", "audience"):
        assert is_consumer({**context, field: audience}) is consumer, (sector, audience, field)
    assert journey._consumer({**context, "who": audience}) is consumer
    picks = office_hours.default_picks({**context, "audience": audience})
    assert ("life situation" in picks[2]["text"]) is consumer


@pytest.mark.parametrize("customer_type,consumer", [("B2C", True), ("B2B", False)])
def test_puppath_saved_icp_type_controls_wording_and_company_contacts(journey_db, monkeypatch, customer_type, consumer):
    from app.db.models import Venture
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.gstack import office_hours
    from app.gtm import journey
    from core.clock import utcnow
    now = utcnow()
    with journey_db["factory"]() as session:
        venture = session.get(Venture, journey_db["vid"])
        venture.raw_idea = "PupPath QA puppy training app, $8/month"
        session.add(IdeaSession(id="pup_session", workspace_id=venture.workspace_id, venture_id=venture.id,
            case="know", state="confirmed", current_revision_id="pup_revision", confirmed_revision_id="pup_revision", created_at=now, updated_at=now))
        session.flush()
        session.add(IdeaRevision(id="pup_revision", session_id="pup_session", workspace_id=venture.workspace_id,
            venture_id=venture.id, rev=1, source="agent", summary="Puppy training", created_at=now,
            structured=json.dumps({"customer": "first-time US dog owners", "customer_type": customer_type,
                "offer": "5-minute daily puppy training video lessons and progress tracker", "problem": "Puppy training frustration", "pricing_hypothesis": "$8/month"})))
        session.commit()
        snapshot = office_hours.saved_snapshot(session, venture.id, "pup_revision")
        assert ("life situation" in office_hours.default_picks(snapshot)[2]["text"]) is consumer
    icp = journey._icp(journey_db["vid"])
    assert icp["customer_type"] == customer_type and icp["mode"] == ("consumer" if consumer else "company")
    assert journey._journey_icp(journey_db["vid"], {**icp, "mode": "company"}, base=icp)["mode"] == icp["mode"]
    if consumer:
        monkeypatch.setattr(journey, "_icp", lambda _: icp)
        assert journey._prospects(journey_db["vid"]) == []


def test_u18_community_reason_is_not_scraped_rules(journey_db):
    from app.gtm import journey
    rules = "Rules 1 Don't Be a Jerk Remember the human. No promotion."
    channel = journey._channel({"url": "https://www.reddit.com/r/ParentFormsFixtures/", "title": "Parents sharing school paperwork tips", "snippet": rules},
        AUDIENCE, PROBLEM, [], "School forms app")
    assert channel and rules not in channel["why_fits"] and "parents" in channel["why_fits"]
    assert channel["rules_url"] == "https://www.reddit.com/r/ParentFormsFixtures/about/rules/"


@pytest.mark.parametrize("partner", [False, True])
def test_u38_channel_reason_is_natural_and_price_free(journey_db, partner):
    from app.gtm import journey
    hit = {"url": "https://taras.example/" if partner else "https://www.reddit.com/r/puppy101/",
        "title": "Tara's School for Dogs" if partner else "First-time dog owners community",
        "snippet": "First-time dog owners discuss puppy training in this community."}
    channel = journey._channel(hit, "first-time US dog owners, $8/month", "First-time dog owners feel overwhelmed.", [], "PupPath")
    assert channel
    assert "first-time US dog owners" in channel["why_fits"]
    assert not any(raw in channel["why_fits"] for raw in ("$8", "public gathering place", "ask about First-time", ".;"))
    assert ("organisation" in channel["why_fits"]) is partner


def test_u38_launchdesk_reason_never_pastes_problem_keywords(journey_db):
    from app.gtm import journey
    audience = "A solo technical founder, 3 months after leaving a job, who has a prototype but zero customer calls booked."
    problem = "First-time founders prototype t find. They can’t find customers."
    channel = journey._channel({"url": "https://www.reddit.com/r/founders/", "title": "Solo technical founders community",
        "snippet": "A community for solo technical founders."}, audience, problem, [], "LaunchDesk")
    assert channel and channel["why_fits"].startswith("You may meet a solo technical founder")
    assert not any(raw in channel["why_fits"] for raw in (".;", "prototype t find", "ask about", "A public gathering"))
    assert "can’t" in journey._keywords("Founders can’t find customers")


def test_u19_geography_mismatch_is_dropped_but_unknown_is_kept(journey_db):
    from app.gtm import journey
    hit = {"url": "https://www.nct.org.uk/", "title": "NCT parents community", "snippet": "A UK charity and community for new parents. Join us."}
    assert journey._channel(hit, AUDIENCE, PROBLEM, [], "School forms app") is None
    assert not journey._geography_mismatch(AUDIENCE, "https://www.reddit.com/r/ParentFormsFixtures/", "Parents community")
    assert not journey._geography_mismatch(AUDIENCE, hit["url"], "Parents in the United States and UK")


@pytest.mark.parametrize("days,recency", [(3, "recent"), (91, None), (366, None)])
def test_u20_reddit_json_preserves_author_date_and_recency(journey_db, days, recency):
    from app.gtm import journey
    from app.signals.research import _sources_from_response
    from core.clock import utcnow
    now = utcnow()
    url = "https://www.reddit.com/r/ParentFormsFixtures/comments/abc123/school_forms/"
    post = {"id": "abc123", "is_self": True, "selftext": "I need help tracking school forms and keep missing the deadlines.",
        "author": "sample_school_parent", "created_utc": (now - timedelta(days=days)).timestamp()}
    raw = json.dumps([{"data": {"children": [{"data": post}]}}])
    captured = journey._reddit_post(raw, url)
    assert journey._reddit_post(raw, url.replace("abc123", "wrong")) == {}
    rows = _sources_from_response("firecrawl", {"success": True, "data": [{"url": url, **captured}]})
    assert rows[0]["author"] == post["author"] and rows[0]["created_utc"] == post["created_utc"]
    signal = journey._qualify_signal(rows[0] | {"kind": "customer_voice"}, captured["content"], {}, PROBLEM, AUDIENCE, now,
        judged={"quote": post["selftext"]})
    if recency is None:
        assert signal is None
    else:
        assert signal["author"] == "sample_school_parent" and signal["recency"] == recency
        assert signal["published_at"].startswith((now - timedelta(days=days)).date().isoformat())
        assert signal["date_status"] == "published"
        assert ("15-minute" in signal["draft"]) == (recency == "recent")
        assert signal["author_url"] == "https://www.reddit.com/user/sample_school_parent/"


def test_u20_public_json_does_not_depend_on_reddit_html(journey_db, monkeypatch):
    from app.gtm import journey, scrapy_leads
    from app.research.prior_art import adapters
    from app.signals.research import HttpResearchProvider
    from core.clock import utcnow
    monkeypatch.setenv("FIRECRAWL_API_KEY", "synthetic")
    url = "https://www.reddit.com/r/ParentFormsFixtures/comments/abc123/school_forms/"
    post = {"id": "abc123", "is_self": True, "selftext": "I need help tracking school forms and keep missing the deadlines.",
        "author": "sample_school_parent", "created_utc": (utcnow() - timedelta(days=3)).timestamp()}
    fetched = []
    def fetch(endpoint, *, allow_json=False, allow_missing=False):
        fetched.append(endpoint)
        if endpoint.endswith("/robots.txt"):
            assert allow_missing
            return "User-agent: *\nAllow: /"
        assert allow_json
        return json.dumps([{"data": {"children": [{"data": post}]}}])
    async def search(self, query, **kwargs):
        return [{"url": url, "title": "School forms help", "snippet": "Access denied"}] if "site:reddit.com/r/ParentFormsFixtures" in query else []
    async def scrape(self, source, **kwargs):
        pytest.fail("A verified public JSON post must not require an HTML scrape")
    async def hn(*args, **kwargs):
        return {"items": []}
    async def model(payload, stage, context):
        if stage in {"marketrole", "passages"}:
            return quality_response(stage, context)
        return {"queries": [{"query": "site:reddit.com/r/ParentFormsFixtures school forms help US", "intent": "customer_voice"},
            {"query": "parents community US", "intent": "community"}]} if stage == "plan" else {"items": []}
    monkeypatch.setattr(scrapy_leads, "_fetch", fetch)
    monkeypatch.setattr(HttpResearchProvider, "search", search)
    monkeypatch.setattr(HttpResearchProvider, "scrape", scrape)
    monkeypatch.setattr(adapters, "search_hn", hn)
    monkeypatch.setattr(journey, "_discovery_model", model)
    journey.update_icp(journey_db["wid"], journey_db["vid"], AUDIENCE, problem=PROBLEM, recruitment_opt_in=True)
    payload = _leased_payload(journey_db)
    asyncio.run(journey._discover(AUDIENCE, problem=PROBLEM, offer="School forms app", payload=payload))
    doc = journey._journal(payload).document(payload["journey_id"])
    assert fetched == ["https://www.reddit.com/robots.txt", url.rstrip("/") + ".json"]
    assert doc["need_signals"][0]["author"] == "sample_school_parent"
    assert doc["need_signals"][0]["recency"] == "recent"


def test_u20_old_need_does_not_become_a_reachable_outreach_target(journey_db, monkeypatch):
    from app.gtm import journey, reachable
    journey.update_icp(journey_db["wid"], journey_db["vid"], AUDIENCE, problem=PROBLEM, recruitment_opt_in=True)
    payload = _leased_payload(journey_db)
    signal = {"recency": "older", "author_url": "https://www.reddit.com/user/sample_school_parent/",
        "source_url": "https://www.reddit.com/r/ParentFormsFixtures/comments/abc123/", "author": "sample_school_parent", "quote": "I need help tracking school forms"}
    context = {"audience": AUDIENCE, "problem": PROBLEM, "mode": "B2C", "competitors": [], "offer": "School forms app"}
    assert asyncio.run(reachable.discover(None, [], [signal], context, payload)) == []


def test_u21_consumer_expert_discovery_uses_published_booking_routes(journey_db, monkeypatch):
    from app.gtm import journey, reachable
    from app.research.prior_art import adapters
    from app.signals.research import HttpResearchProvider
    monkeypatch.setenv("FIRECRAWL_API_KEY", "synthetic")
    searches = []
    async def search(self, query, **kwargs):
        searches.append(query)
        if "parenting educator" in query:
            return [{"url": "https://school-forms-guide.example/", "title": "Jane Doe Parenting Educator", "snippet": "Jane Doe parenting educator in the United States."}]
        return []
    async def hn(*args, **kwargs):
        return {"items": []}
    async def model(payload, stage, context):
        if stage in {"marketrole", "passages"}:
            return quality_response(stage, context)
        if stage == "plan":
            return {"queries": [{"query": "parents community US", "intent": "community"},
                {"query": "site:linkedin.com/in parents educator US", "intent": "person"},
                {"query": "site:x.com parents educator US", "intent": "person"}]}
        if stage == "reachjudge":
            return {"items": [{"id": row["id"], "kind": row["kind"], "name": "Jane Doe", "quote": "Jane Doe parenting educator in the United States.",
                "url": row["url"], "affiliation": "", "fit": 90, "why": "Helps parents with school forms and can offer insights.",
                "value_exchange": "Share research findings in exchange for school form tracking insights.", "lead_type": "insight"} for row in context["candidates"]]}
        return {"items": []}
    async def enrich_site(provider, url, *args, **kwargs):
        assert kwargs["published_mailboxes"]
        text = "Jane Doe parenting educator in the United States."
        return {"contacts": [{"email": "jane@school-forms-guide.example", "kind": "named_person", "source_url": url, "quote": text}],
            "booking": {"url": "https://calendly.com/school-forms-guide/intro", "source_url": url, "quote": "Book an introductory call"},
            "contact_form": None, "phone": None, "company_text": text, "evidence_pages": [{"url": url, "text": text}],
            "people": [], "profile_urls": [], "country": "US", "careers_url": None, "name": "Jane Doe"}
    monkeypatch.setattr(HttpResearchProvider, "search", search)
    monkeypatch.setattr(adapters, "search_hn", hn)
    monkeypatch.setattr(journey, "_discovery_model", model)
    monkeypatch.setattr(reachable.enrich, "enrich_site", enrich_site)
    monkeypatch.setattr(reachable.enrich, "guessed_routes", lambda *args, **kwargs: pytest.fail("experts must not use guessed emails"))
    journey.update_icp(journey_db["wid"], journey_db["vid"], AUDIENCE, problem=PROBLEM, recruitment_opt_in=True)
    payload = _leased_payload(journey_db)
    assert asyncio.run(journey._discover(AUDIENCE, problem=PROBLEM, offer="School forms app", payload=payload)) == []
    doc = journey._journal(payload).document(payload["journey_id"])
    assert doc["people_search_notice"] == "No public profiles found for LinkedIn and X."
    assert any("lactation consultant" in query for query in searches)
    assert any("parenting podcast host" in query for query in searches)
    assert not doc["reachable_leads"]
    assert not any(query.get("expert_role") for query in doc["discovery_plan"])
    assert len(doc["discovery_plan"]) + len(doc["expert_discovery_plan"]) <= journey.CALL_CEILINGS["signal_search"]
    leads = doc["industry_experts"]
    assert leads and leads[0]["expert_role"] == "parenting educator" and leads[0]["lead_type"] == "insight"
    assert leads[0]["routes"][0]["kind"] == "booking" and "15-minute" in leads[0]["draft"]
    view = journey.status(journey_db["wid"], journey_db["vid"])
    assert view["reachable_leads"] == [] and view["industry_experts"][0]["id"] == leads[0]["id"]
    assert view["will_search"]
    assert not journey._prospects(journey_db["vid"])


def test_u21_need_only_results_offer_search_again(journey_db):
    from app.gtm import journey
    from app.db.models import VentureDocument
    from sqlalchemy import select
    journey.update_icp(journey_db["wid"], journey_db["vid"], AUDIENCE, problem=PROBLEM)
    with journey_db["factory"]() as session:
        row = session.scalar(select(VentureDocument).where(VentureDocument.kind == journey.KIND))
        doc = json.loads(row.provenance_json)
        doc.update(state="ready", need_signals=[{"quote": "I need help tracking school forms", "source_url": "https://www.reddit.com/r/ParentFormsFixtures/comments/abc123/"}])
        row.provenance_json = json.dumps(doc)
        session.commit()
    view = journey.status(journey_db["wid"], journey_db["vid"])
    assert view["will_search"] and view["phase"] == "prospects" and not view["prospects"]


def test_u21_retry_preserves_communities_when_there_are_no_businesses(journey_db):
    from app.gtm import journey
    journey.update_icp(journey_db["wid"], journey_db["vid"], AUDIENCE, problem=PROBLEM, recruitment_opt_in=True)
    payload = _leased_payload(journey_db)
    journal = journey._journal(payload)
    channel = {"name": "r/ParentFormsFixtures", "url": "https://www.reddit.com/r/ParentFormsFixtures/", "why_fits": "Parents share experience", "draft": "Open to a call?"}
    def finish(row, doc, session):
        from app.db.models import Job
        doc.update(channels=[channel], state="ready")
        session.get(Job, doc["job_id"]).status = "succeeded"
    journal.mutate(payload["journey_id"], payload, finish)
    view = journey.prepare(journey_db["wid"], journey_db["vid"], retry=True, recruitment_opt_in=True)
    # The community survives the retry; its suggested post is regenerated from the venture's saved facts.
    assert [{k: v for k, v in c.items() if k != "draft"} for c in view["channels"]] == [{k: v for k, v in channel.items() if k != "draft"}]
    assert "?" in view["channels"][0]["draft"] and not view["prospects"]


def test_u33_search_result_without_author_or_date_survives_blocked_reddit_json(journey_db, monkeypatch):
    from app.gtm import journey, scrapy_leads
    from app.research.prior_art import adapters
    from app.signals.research import HttpResearchProvider
    from core.clock import utcnow
    who, problem = "Busy US parents of picky toddlers", "Planning meals for picky toddlers"
    url = "https://www.reddit.com/r/toddlers/comments/abc123/picky_meals/"
    quote = "I need help planning meals for my picky toddler and cannot keep cooking separate dinners."
    hit = {"url": url, "title": "Picky toddler meals", "snippet": quote,
        "content": "Access denied", "metadata": {"statusCode": 403, "publishedTime": "unparseable"}}
    async def search(self, query, **kwargs):
        return [hit] if "site:reddit.com/r/toddlers" in query else []
    async def model(payload, stage, context):
        if stage in {"marketrole", "passages"}:
            return quality_response(stage, context)
        return {"queries": [{"query": "site:reddit.com/r/toddlers picky meals", "intent": "customer_voice"},
            {"query": "parents community US", "intent": "community"}]} if stage == "plan" else {"items": []}
    async def hn(*args, **kwargs):
        return {"items": []}
    monkeypatch.setenv("FIRECRAWL_API_KEY", "synthetic")
    monkeypatch.setattr(scrapy_leads, "_fetch", lambda *args, **kwargs: None)
    monkeypatch.setattr(HttpResearchProvider, "search", search)
    monkeypatch.setattr(HttpResearchProvider, "scrape", lambda *args, **kwargs: pytest.fail("Reddit snippets must not require a scrape"))
    monkeypatch.setattr(adapters, "search_hn", hn)
    monkeypatch.setattr(journey, "_discovery_model", model)
    journey.update_icp(journey_db["wid"], journey_db["vid"], who, problem=problem, recruitment_opt_in=True)
    payload = _leased_payload(journey_db)
    asyncio.run(journey._discover(who, problem=problem, offer="Meal planning app", payload=payload))
    signals = journey._journal(payload).document(payload["journey_id"])["need_signals"]
    assert len(signals) == 1 and signals[0]["quote"] == quote
    assert signals[0]["recency"] == "unknown" and "Date unknown" in signals[0]["why_now"]
    assert not signals[0].get("author") and signals[0]["published_at"] is None
    raw = json.dumps([{"data": {"children": [{"data": {"id": "abc123", "selftext": quote}}]}}])
    assert journey._reddit_post(raw, url) == {"content": quote}
    assert journey._qualify_signal(hit | {"kind": "customer_voice"}, quote,
        {"publishedTime": (utcnow() - timedelta(days=91)).isoformat()}, problem, who, utcnow()) is None


@pytest.mark.parametrize("who,problem,roles", [
    ("Busy US parents of picky toddlers", "Planning meals for picky toddlers",
        ["pediatric dietitian", "toddler feeding specialist", "parenting podcast host"]),
    ("first-time US dog owners", "Puppy training frustration", ["dog training association", "puppy training coach", "dog behavior consultant"]),
])
def test_u31_consumer_experts_have_reserved_searches_and_published_booking_routes(journey_db, monkeypatch, who, problem, roles):
    from app.gtm import journey, reachable
    from app.research.prior_art import adapters
    from app.signals.research import HttpResearchProvider
    source = "https://feeding-expert.example/podcast/contact"
    quote = "Jane Doe pediatric dietitian helping parents feed picky toddlers in the United States."
    searches = []
    async def search(self, query, **kwargs):
        searches.append(query)
        return [{"url": source, "title": "Jane Doe podcast contact", "snippet": quote}] if roles[0] in query else []
    async def model(payload, stage, context):
        if stage in {"marketrole", "passages"}:
            return quality_response(stage, context)
        if stage == "plan":
            return {"queries": [{"query": f"parent community topic {index}", "intent": "community"} for index in range(8)]}
        if stage == "reachjudge":
            return {"items": [{"id": row["id"], "kind": "business", "name": "Jane Doe", "url": source,
                "quote": quote, "affiliation": "", "fit": 90, "why": "Published feeding expertise.",
                "value_exchange": "Exchange research findings about toddler meals.", "lead_type": "insight"} for row in context["candidates"]]}
        return {"items": []}
    async def enrich_site(*args, **kwargs):
        assert kwargs["published_mailboxes"]
        return {"contacts": [], "booking": {"url": "https://calendly.com/feeding-expert/intro", "source_url": source, "quote": "Book an introductory call"},
            "contact_form": None, "phone": None, "company_text": quote, "evidence_pages": [{"url": source, "text": quote}],
            "people": [], "profile_urls": [], "country": "US", "careers_url": None, "name": "Jane Doe"}
    async def hn(*args, **kwargs):
        return {"items": []}
    monkeypatch.setenv("FIRECRAWL_API_KEY", "synthetic")
    monkeypatch.setattr(HttpResearchProvider, "search", search)
    monkeypatch.setattr(adapters, "search_hn", hn)
    monkeypatch.setattr(journey, "_discovery_model", model)
    monkeypatch.setattr(reachable.enrich, "enrich_site", enrich_site)
    monkeypatch.setattr(reachable.enrich, "guessed_routes", lambda *args, **kwargs: pytest.fail("Expert routes must be published"))
    journey.update_icp(journey_db["wid"], journey_db["vid"], who, problem=problem, recruitment_opt_in=True)
    payload = _leased_payload(journey_db)
    assert asyncio.run(journey._discover(who, problem=problem, offer="Meal planning app", payload=payload)) == []
    doc = journey._journal(payload).document(payload["journey_id"])
    assert len(searches) == journey.CALL_CEILINGS["signal_search"]
    assert {item["expert_role"] for item in doc["expert_discovery_plan"]} == set(roles)
    assert doc["industry_experts"][0]["routes"][0]["kind"] == "booking"
    assert not doc["reachable_leads"] and not journey._prospects(journey_db["vid"])

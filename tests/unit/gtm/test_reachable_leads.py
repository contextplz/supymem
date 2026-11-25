"""Reachable discovery uses real SQLite receipts and synthetic public providers."""
import asyncio
from copy import deepcopy
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

from tests.unit.gtm.test_journey import journey_db as journey_db, _work
from tests.unit.gtm.test_journey_signals import quality_response


@pytest.mark.parametrize("fixture", [
    "fixtures/reachable_live_failures.json",
    "fixtures/reachable_second_run.json",
    "fixtures/discovery_public_replay.json",
    "fixtures/discovery_public_replay2.json",
    "fixtures/directory_physio.json",
    "fixtures/lead_eval.json",
    "../../fixtures/lead_library/synthetic_leads.jsonl",
])
def test_prospect_fixtures_use_only_reserved_hosts(fixture):
    text = (Path(__file__).parent / fixture).read_text()
    hosts = {urlsplit(url).hostname for url in re.findall(r"https?://[^\s/<>\"']+", text)}
    hosts.update(re.findall(r"[\w.+-]+@([a-zA-Z0-9.-]+\.[a-zA-Z]+)\b", text))
    hosts.update(re.findall(r"\bsite:([a-zA-Z0-9.-]+\.[a-zA-Z]+)\b", text))
    assert hosts
    assert all(host and (host in {"example.org", "example.net", "example.com"} or
        host.endswith((".example", ".test", ".invalid", ".example.org", ".example.net", ".example.com"))) for host in hosts)


@pytest.mark.parametrize("audience,b2b", [("Laboratory inventory managers", True), ("Weekend birdwatchers", False)])
@pytest.mark.parametrize("address", ["stopped@public.example", "Straße@public.example"])
def test_stopped_route_identity_remains_excluded(journey_db, monkeypatch, audience, b2b, address):
    from app.db.models import GtmSuppression
    from app.gtm import reachable
    from core.clock import utcnow

    data = journey_db
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    profile = "https://www.linkedin.com/in/stopped-person/"
    with data["factory"]() as session:
        session.add(GtmSuppression(id="stopped-route", workspace_id=data["wid"], email=address,
            linkedin_url=profile, reason="opt_out", created_at=utcnow()))
        session.commit()
    lead = {"id": "stopped", "name": audience, "routes": [
        {"kind": "email", "email": address, "route_type": "published"},
        {"kind": "website", "url": "https://public.example/contact"}], "people": [], "value_exchange": "Interview"}
    aliases = [{**lead, "id": key, "routes": [{"kind": "website", "url": "https://public.example/"}],
        key: ["https://linkedin.com/in/stopped-person"] if key == "related_profiles" else "https://linkedin.com/in/stopped-person"}
        for key in ("related_profiles", "source_url", "profile_url", "author_url")]
    assert reachable.visible(deepcopy([lead, *aliases]), data["wid"], b2b=b2b) == []


@pytest.mark.parametrize("audience,domain,b2b", [("factory maintenance buyers", "factory-tools.example", True), ("families learning music", "family-music.example", False)])
@pytest.mark.hosted_only
def test_suppressed_published_routes_show_opt_out_without_exposing_an_address(journey_db, audience, domain, b2b):
    from app.chat import customers
    from app.gtm import email_learning, reachable
    from core.clock import utcnow

    def lead(label):
        email = f"{label}@{domain}"
        url = f"https://{domain}/{label}"
        return {"id": label, "name": audience, "kind": "person", "source_url": url, "quote": audience,
                "routes": [{"kind": "email", "route_type": "published", "email": email, "quote": email},
                           {"kind": "website", "url": url}]}

    with journey_db["factory"]() as session:
        email_learning.suppress(session, f"stopped@{domain}")
        retired = email_learning.refresh(session, email_learning.opaque(f"retired@{domain}"), domain)
        retired.retired_at = utcnow()
        session.commit()
    shown = reachable.visible([lead("stopped"), lead("retired"), lead("available")], journey_db["wid"], b2b=b2b)
    stopped, retired, available = shown
    assert stopped["email_status"] == "opted_out"
    body = customers._lead_block(1, stopped)
    assert "opted out" in body and "none published" not in body and f"stopped@{domain}" not in body
    draft = asyncio.run(customers._draft_named(journey_db["vid"], {"name": audience, "lead": stopped},
        f"Draft an email to {audience}. Do not send it.", journey_db["actor"]))
    assert "opted out" in draft["body_md"] and "no published email" not in draft["body_md"]
    assert f"stopped@{domain}" not in draft["body_md"]
    assert "opted out" not in customers._lead_block(2, retired)
    assert all(not route.get("email") for person in (stopped, retired) for route in person["routes"])
    assert reachable.published_route(available)["email"] == f"available@{domain}"
    with journey_db["factory"]() as session:
        assert email_learning.globally_suppressed(session, f"stopped@{domain}")


@pytest.mark.parametrize("audience,problem,mode,message", [
    ("Packaging plant maintenance managers in the United States", "Schedule equipment inspections", "B2B",
     "Find 10 named plant maintenance managers at US packaging plants with publicly listed work emails. Do not email anyone yet."),
    ("Parents helping children practice music in the United Kingdom", "Schedule daily instrument practice", "B2C",
     "Find 10 people helping their children practice music in the United Kingdom. Do not email anyone yet."),
])
def test_people_search_publishes_people_without_unrequested_drafts(journey_db, monkeypatch, audience, problem, mode, message):
    from app.autopilot import outreach
    from app.chat import customers
    from app.gtm import journey, reachable
    from tests.unit.gtm.test_journey_signals import _leased_payload

    data = journey_db
    monkeypatch.setattr(outreach, "_brief", lambda vid: {"idea": {"audience": audience, "problem": problem, "confirmed": True}, "research": {}})
    journey.update_icp(data["wid"], data["vid"], audience, problem=problem, recruitment_opt_in=mode == "B2C", note=message)
    payload = _leased_payload(data)
    journal = journey._journal(payload)
    names = [f"Alex {surname}" for surname in ("Carter", "Bennett", "Wright", "Morris", "Grant", "Lewis", "Parker", "Reed", "Stone", "Hayes")]
    role = "Maintenance manager" if mode == "B2B" else "Parent"
    candidates = [{"url": f"https://person{index}.example/team", "title": name, "kind": "person",
        "country": "US" if mode == "B2B" else "GB", "text": f"{name}. {role}. {problem}", "quality_selected": True,
        "evidence_label": "need_passage", "pages": [{"url": f"https://person{index}.example/team", "text": f"{name}. {role}. {problem}", "relevance": 0.95}],
        "routes": [{"kind": "email", "email": f"person{index}@example.test", "source_url": f"https://person{index}.example/team",
            "quote": f"{name} person{index}@example.test", "route_type": "published"}]} for index, name in enumerate(names)]
    journal.mutate(payload["journey_id"], payload, lambda row, doc, session: (
        doc["_runtime"].update(reachable_candidates_quality_v1=candidates),
        doc.update(search_target={"target_type": "person", "requested_count": 10})))
    drafted = []

    async def judge(payload, stage, context):
        return [{"id": candidate["id"], "kind": candidate["kind"], "name": candidate["title"], "role": role, "affiliation": "",
            "url": candidate["url"], "quote": candidate["text"], "fit": 90, "why": problem, "value_exchange": problem}
            for candidate in context["candidates"]]

    async def model(*args, **kwargs):
        pytest.fail("Finding people must not compose email drafts")

    monkeypatch.setattr(journey, "_judge_batch", judge)
    monkeypatch.setattr(journey, "_draft_ready", lambda *args: drafted.append(args) or 0)
    monkeypatch.setattr(journey, "_discovery_model", model)
    context = {"mode": mode, "audience": audience, "problem": problem, "offer": "", "competitors": [],
        "target_type": "person", "requested_count": 10}
    people = []

    async def discover(*args, payload, **kwargs):
        people.extend(await reachable.discover(None, [], [], context, payload))
        return []

    monkeypatch.setattr(journey, "_discover", discover)
    asyncio.run(journey.run_prepare(payload))
    assert len(people) == 10
    assert not drafted, "The publication path must not start unrequested email drafts"
    view = journey.status(data["wid"], data["vid"])
    reply = customers.render(data["vid"], view)
    assert "10 of 10 people" in reply["body_md"] and "Draft 1" not in reply["body_md"]
    assert not outreach.drafts(data["vid"])


@pytest.mark.parametrize("audience,problem,mode,country,domain", [
    ("Security operations teams in the US", "Investigate infrastructure alerts", "B2B", "US", "alertdesk.example"),
    ("Parents helping children practice music in the United Kingdom", "Schedule daily instrument practice", "B2C", "GB", "practicejournal.co.uk"),
])
def test_reachable_publishes_batches_before_prepare_finishes(journey_db, monkeypatch, audience, problem, mode, country, domain):
    from app.core.local_mode import is_local
    from sqlalchemy import select
    from app.autopilot import outreach
    from app.db.models import GtmContact, GtmTouch, Venture
    from app.gtm import journey, reachable
    from tests.unit.gtm.test_journey import _mail
    from tests.unit.gtm.test_journey_signals import _leased_payload
    from tests.unit.gtm.test_leads_to_drafts import _lead

    data = journey_db
    _mail(data)
    with data["factory"]() as session:
        venture = session.get(Venture, data["vid"])
        venture.raw_idea = f"{problem} for {audience}"
        session.commit()
    monkeypatch.setattr(outreach, "_brief", lambda vid: {"idea": {"audience": audience, "problem": problem, "confirmed": True}, "research": {}})
    journey.update_icp(data["wid"], data["vid"], audience, problem=problem, recruitment_opt_in=mode == "B2C")
    payload = _leased_payload(data)
    journal = journey._journal(payload)
    candidates = []
    for index in range(25):
        url = f"https://contact{index}.{domain}/"
        name = f"Reader {index}" if mode == "B2C" else f"Team {index}"
        text = f"{name}: {problem}. Email hello@contact{index}.{domain}."
        candidates.append({"kind": "person" if mode == "B2C" else "business", "url": url, "title": name,
            "text": text, "country": country, "quality_selected": True, "evidence_label": "need_passage",
            "pages": [{"url": url, "text": text, "relevance": 0.95}], "routes": [{"kind": "email",
                "email": f"hello@contact{index}.{domain}", "source_url": url, "quote": text, "route_type": "published"}]})
    journal.mutate(payload["journey_id"], payload, lambda row, doc, session: (
        doc["_runtime"].update(reachable_candidates_quality_v1=candidates),
        doc["icp"].update(customer_type="consumer" if mode == "B2C" else "business"),
        doc.update(force_discovery=True, discovery_plan=[{"query": f"{audience} source {index}", "intent": "person" if mode == "B2C" else "business"}
            for index in range(15)])))
    older = [_lead(f"old-{index}", f"Earlier business {index}", f"old{index}.{domain}")
        | {"country": country, "fit": 100, "kind": "person" if mode == "B2C" else "business"} for index in range(30)]
    reachable._publish(journal, payload, older)
    with data["factory"]() as session:
        for contact in session.scalars(select(GtmContact)):
            contact.times_emailed = 1
        session.commit()
    candidate_ids = {hashlib.sha256(candidate["url"].encode()).hexdigest()[:24] for candidate in candidates}
    judged, increments, published_ids, drafting_counts = [], [], {lead["id"] for lead in older}, []
    original_publish, original_draft = reachable._publish, journey._draft_ready
    def publish(journal, payload, leads, **kwargs):
        new = [lead for lead in leads if lead.get("judging_status") == "judged" and lead["id"] not in published_ids]
        ingested = original_publish(journal, payload, leads, **kwargs)
        if new:
            assert journal.document(payload["journey_id"])["state"] == "running"
            increments.append((len(new), len(judged)))
            published_ids.update(lead["id"] for lead in new)
            with data["factory"]() as session:
                assert len(list(session.scalars(select(GtmContact)))) == len(published_ids)
        return ingested
    def draft(wid, vid):
        with data["factory"]() as session:
            drafting_counts.append(len(list(session.scalars(select(GtmContact)))))
        return original_draft(wid, vid)
    def trace(payload, url, event, *args, **kwargs):
        if event == "judged":
            assert candidate_ids <= {lead["id"] for lead in journal.document(payload["journey_id"])["reachable_leads"]}
            judged.append(url)
    async def judge(payload, stage, context):
        return [{"id": candidate["id"], "kind": candidate["kind"], "name": candidate["title"], "affiliation": "",
            "url": candidate["url"], "quote": candidate["text"], "fit": 90, "why": problem, "value_exchange": problem}
            for candidate in context["candidates"]]
    async def model(payload, stage, context):
        assert stage == "reachdraft"
        return {"items": []}
    async def discover(*args, payload, **kwargs):
        context = {"mode": mode, "audience": audience, "problem": problem, "offer": "", "competitors": []}
        assert len(await reachable.discover(None, [], [], context, payload)) == 25
        return []
    monkeypatch.setattr(reachable, "_publish", publish)
    monkeypatch.setattr(reachable, "trace", trace)
    monkeypatch.setattr(journey, "_draft_ready", draft)
    monkeypatch.setattr(journey, "_judge_batch", judge)
    monkeypatch.setattr(journey, "_discovery_model", model)
    monkeypatch.setattr(journey, "_discover", discover)
    asyncio.run(journey.run_prepare(payload))
    assert increments == ([(8, 8), (8, 16), (8, 24), (1, 25)] if is_local() else [(10, 10), (10, 20), (5, 25)])
    view = journey.status(data["wid"], data["vid"])
    assert view["state"] == "ready" and view["research_progress"]["percent"] == 100
    assert len(view["reachable_leads"]) == 30 and len(published_ids) == 55
    assert candidate_ids <= {lead["id"] for lead in view["reachable_leads"]}
    with data["factory"]() as session:
        contacts = list(session.scalars(select(GtmContact)))
        assert len(contacts) == len({contact.email for contact in contacts}) == 55
        assert {contact.recipient_geo for contact in contacts} == {country}
        assert not list(session.scalars(select(GtmTouch)))
    if mode == "B2B":
        assert drafting_counts[:3] == ([55] if is_local() else [40, 50, 55])
        assert len(outreach.drafts(data["vid"], "draft")) == 25
    else:
        assert not drafting_counts and not outreach.drafts(data["vid"], "draft")


@pytest.mark.parametrize("audience,country,domain", [
    ("Security operations teams in the US", "US", "alertdesk.example"),
    ("Parents helping children practice music in the United Kingdom", "GB", "practicejournal.co.uk"),
])
def test_final_reachable_publication_removes_pending_without_duplicate_contacts(journey_db, audience, country, domain):
    from sqlalchemy import select
    from app.db.models import GtmContact
    from app.gtm import journey, reachable
    from tests.unit.gtm.test_journey_signals import _leased_payload
    from tests.unit.gtm.test_leads_to_drafts import _lead

    data = journey_db
    journey.update_icp(data["wid"], data["vid"], audience, recruitment_opt_in=country == "GB")
    payload = _leased_payload(data)
    journal = journey._journal(payload)
    plan = [{"query": f"{audience} source {index}", "intent": "business"} for index in range(15)]
    journal.mutate(payload["journey_id"], payload, lambda row, doc, session: doc.update(discovery_plan=plan))
    limit = journey._keep_limit(len(plan))
    candidates, kept = [], []
    for lead_type in ("customer", "insight"):
        group = [_lead(f"{lead_type}-{index}", f"{lead_type} contact {index}", f"{lead_type}{index}.{domain}", lead_type=lead_type)
            | {"country": country} for index in range(limit + 3)]
        candidates.extend(group)
        kept.extend(group[:limit])
    reachable._publish(journal, payload, [lead | {"judging_status": "pending"} for lead in candidates])
    for start in range(0, len(kept), reachable.PUBLISH_BATCH_SIZE):
        reachable._publish(journal, payload, kept[start:start + reachable.PUBLISH_BATCH_SIZE])
    doc = journal.document(payload["journey_id"])
    assert all(len(doc[field]) == limit + 3 for field in ("reachable_leads", "industry_experts"))
    reachable._publish(journal, payload, kept, final=True)
    reachable._publish(journal, payload, kept, final=True)
    doc = journal.document(payload["journey_id"])
    for field in ("reachable_leads", "industry_experts"):
        assert len(doc[field]) == limit
        assert all(lead["judging_status"] == "judged" for lead in doc[field])
        assert len({lead["id"] for lead in doc[field]}) == limit
    with data["factory"]() as session:
        contacts = list(session.scalars(select(GtmContact)))
        assert len(contacts) == len({contact.email for contact in contacts}) == len(kept)


def fixture_models(monkeypatch, *, consumer=False):
    from app.gtm import journey
    seen = []
    async def model(payload, stage, context):
        if stage in {"marketrole", "passages"}:
            return quality_response(stage, context)
        seen.append((stage, context))
        if stage == "plan":
            return {"queries": [{"query": "dental practices Austin", "intent": "business"},
                {"query": "dental practices Boston", "intent": "business"},
                {"query": 'site:linkedin.com/in "Owner" "dental"', "intent": "person"},
                {"query": 'site:x.com "dental" "no-shows"', "intent": "person"},
                {"query": "site:reddit.com/user dental appointments", "intent": "person"},
                {"query": "dental forum needs", "intent": "community"}] if not consumer else [
                    {"query": 'site:linkedin.com/in "Swim Coach" "Bangalore"', "intent": "person"},
                    {"query": "site:x.com Bangalore swimming parents", "intent": "person"},
                    {"query": "site:reddit.com/user Bangalore swimming", "intent": "person"}]}
        if stage == "reachjudge":
            items = []
            for row in context["candidates"]:
                name = row["title"].split(" | ")[0]
                page = row["evidence_pages"][0]
                items.append({"id": row["id"], "kind": row["kind"], "quote": page["text"] if row["kind"] == "person" else page["text"].splitlines()[0],
                    "affiliation": "", "name": name, "url": page["url"],
                    "role": "Owner" if name == "Alice Jones" else "Swim Coach" if name == "Raj Kumar" else "",
                    "company": "Austin Dental" if name == "Alice Jones" else "", "location": "", "fit": 90,
                    "why": "This practice fits the independent dental customer and appointment workflow.",
                    "value_exchange": {"Alice Jones": "Owns Austin Dental and decides how the independent practice handles appointments.",
                        "Raj Kumar": "Teaches Bangalore parents' children to swim; serves the same customers as a local partner.",
                        "Sam Rao": "Runs dental reception and publicly asks for help reducing patient no-shows.",
                        "Maya Rao": "Bangalore parent publicly asking for children's swimming lessons.",
                        "dental-person": "Runs dental reception and needs help with missed appointments.",
                        "swim-parent": "Bangalore parent asking for children's swimming lessons."}.get(name, "")})
            return {"items": items}
        if stage == "reachdraft":
            return {"items": [{"id": row["id"], "body": "Hi, I'm exploring appointment workflows. Would you be open to a 15-minute call? Please reply with a time.",
                "connection_note": "Hi, I'm researching appointment workflows. Open to a 15-minute call? Reply with a time."} for row in context["candidates"]]}
        return {"items": []}
    monkeypatch.setattr(journey, "_discovery_model", model)
    return seen


@pytest.mark.parametrize("stage,ceiling", [("judge", 4), ("reachjudge", 11)])
@pytest.mark.parametrize("second_pass", ["qualified", "rejected", "omitted", "exhausted"])
def test_judge_coverage_retries_only_missing_ids_with_shared_budget(journey_db, monkeypatch, stage, ceiling, second_pass):
    from types import SimpleNamespace
    from app.gtm import journey, reachable
    from app.gtm.discovery import Judgements, parse_response
    from tests.idea_fixtures import install_provider
    from tests.unit.gtm.test_journey_signals import _leased_payload

    journey.update_icp(journey_db["wid"], journey_db["vid"], "Small logistics firms")
    payload = _leased_payload(journey_db)
    candidates = [{"id": index, "kind": "business", "url": f"https://firm{index}.example/", "title": f"Firm {index}",
        "text": "Published logistics operations", "evidence_pages": [{"url": f"https://firm{index}.example/", "text": "Published logistics operations"}]}
        for index in range(3)]
    verdicts = [{"id": index, "kind": "business", "quote": "Published logistics operations", "affiliation": "",
        "name": f"Firm {index}", "url": candidates[index]["url"], "fit": 85, "why": "Handles the target workflow."} for index in range(3)]
    traces = []
    monkeypatch.setattr(reachable, "trace", lambda *args, **facts: traces.append((args, facts)))
    def respond(messages, options):
        from jsonschema import Draft202012Validator
        supplied = json.loads(messages[-1]["content"])["candidates"]
        ids = [row["id"] for row in supplied]
        schema = json.JSONDecoder().raw_decode(messages[0]["content"].split("\nSchema:\n", 1)[1])[0]
        assert schema["properties"]["items"]["minItems"] == schema["properties"]["items"]["maxItems"] == len(ids)
        if ids:
            assert schema["$defs"]["Rejection"]["properties"]["id"]["enum"] == ids
            complete = {"items": [{"id": index, "reject_reason": "Not a customer."} for index in ids]}
            validator = Draft202012Validator(schema)
            assert validator.is_valid(complete)
            assert not validator.is_valid({"items": complete["items"][:-1]})
            if len(ids) > 1:
                assert not validator.is_valid({"items": [complete["items"][0]] * len(ids)})
        if len(supplied) == 3:
            answer = {"items": [verdicts[0]]}
        elif supplied:
            assert ids == [1, 2]
            answer = {"items": verdicts[1:] if second_pass == "qualified" else
                [{"id": index, "reject_reason": "Competing vendor, not a customer."} for index in ids] if second_pass == "rejected" else []}
        else:
            answer = {"items": []}
        return SimpleNamespace(model="DeepSeek-V4-Pro", choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(answer)))],
            usage=SimpleNamespace(prompt_tokens=300, completion_tokens=200, total_tokens=500))
    calls = install_provider(monkeypatch, respond)
    journal = journey._journal(payload)
    if second_pass == "exhausted":
        def fill(row, doc, session):
            for batch in range(1, ceiling):
                doc["_runtime"]["calls_used"][f"discovery-{stage}-{batch}"] = 1
        journal.mutate(payload["journey_id"], payload, fill)
    context = {"mode": "B2B", "audience": "Small logistics firms", "problem": "logistics operations", "candidates": candidates, "_batch": 0}
    result = asyncio.run(journey._judge_batch(payload, stage, context))
    assert [row["id"] for row in result] == ([0, 1, 2] if second_pass == "qualified" else [0])
    assert len(calls) == (1 if second_pass == "exhausted" else 2)
    assert journal.document(payload["journey_id"])["_runtime"]["calls_used"]["discovery-" + stage] <= ceiling
    omissions = [args[1] for args, facts in traces if len(args) > 3 and args[3] == "judge_omitted_candidate"]
    assert omissions == ([candidates[index]["url"] for index in (1, 2)] if second_pass in {"omitted", "exhausted"} else [])
    assert asyncio.run(journey._judge_batch(payload, stage, context)) == result
    assert len(calls) == (1 if second_pass == "exhausted" else 2)
    assert parse_response(Judgements, '{"items":[{"id":1,"reject_reason":" "}]}') == {"items": []}
    for batch in range(1, ceiling):
        try:
            asyncio.run(journey._discovery_model(payload, stage, context | {"_batch": batch, "candidates": []}))
        except journey.FatalError as error:
            assert str(error) == "gtm_call_budget_exhausted"
    assert len(calls) <= ceiling
    with pytest.raises(journey.FatalError, match="gtm_call_budget_exhausted"):
        asyncio.run(journey._discovery_model(payload, stage, context | {"_batch": ceiling - 1, "_omitted": True}))
    assert len(calls) <= ceiling


@pytest.mark.parametrize("audience,country", [("Small architecture firms in Canada", "Canada"),
    ("Enterprise finance teams in Germany", "Germany")])
def test_planned_queries_do_not_repeat_audience_location(journey_db, audience, country):
    from app.gtm import journey

    context = {"location": country.upper(), "geography": country.lower()}
    queries = journey._search_queries(audience, "workflow delays", context=context)
    queries += [item["query"] for item in journey._fallback_plan(audience, "workflow delays", [], context=context)]
    queries += [item["query"] for item in journey._decision_maker_queries(audience, context=context)]
    assert all(len(re.findall(r"\b" + re.escape(country) + r"\b", query, re.I)) == 1 for query in queries)


@pytest.mark.parametrize("audience,roles", [("Small architecture firms in Canada", ("owner", "principal")),
    ("Enterprise finance teams in Germany", ("head of", "director")), ("Home espresso enthusiasts US Canada", ())])
def test_discovery_plan_adds_b2b_decision_makers_not_b2c(journey_db, monkeypatch, audience, roles):
    from app.core.local_mode import is_local
    from app.gtm import journey
    from app.research.prior_art import adapters
    from app.signals.research import HttpResearchProvider
    from tests.unit.gtm.test_journey_signals import _leased_payload

    searches = []
    async def model(payload, stage, context):
        return {"queries": [{"query": f"workflow community {index}", "intent": "community"} for index in range(8)]}
    async def search(provider, query, **options):
        searches.append((query, options))
        return []
    async def hn(*args, **kwargs):
        return {"items": []}
    monkeypatch.setattr(journey, "_discovery_model", model)
    monkeypatch.setattr(HttpResearchProvider, "search", search)
    monkeypatch.setattr(HttpResearchProvider, "scrape", lambda *args, **kwargs: pytest.fail("Unexpected page fetch"))
    monkeypatch.setattr(adapters, "search_hn", hn)
    monkeypatch.setenv("FIRECRAWL_API_KEY", "synthetic")
    journey.update_icp(journey_db["wid"], journey_db["vid"], audience)
    if not roles:
        journey.prepare(journey_db["wid"], journey_db["vid"], recruitment_opt_in=True)
    payload = _leased_payload(journey_db)
    asyncio.run(journey._discover(audience, problem="workflow delays", payload=payload))
    document = journey._journal(payload).document(payload["journey_id"])
    plan = document["discovery_plan"]
    ceilings = document["_runtime"]["ceilings"]
    assert len(plan) + len(document["expert_discovery_plan"]) == ceilings["signal_search"]
    initial = journey.BUSINESS_SEARCH_QUERIES if roles else ceilings["signal_search"]
    assert len(searches) == initial + (ceilings["broaden_search"] if roles or is_local() else 0)
    targeted = journey._decision_maker_queries(audience)
    expected_roles = ([roles[0]] * 5 if is_local() else roles) if roles else ()
    assert len(targeted) == len(expected_roles)
    for query, role in zip(targeted, expected_roles):
        assert role in query["query"] and query in plan
        if is_local():
            assert not any(site in query["query"] for site in ("linkedin.com", "x.com"))
        else:
            assert f'"{role}"' in query["query"]
        assert all(not options.get("include_content") for text, options in searches if text == query["query"])
    if not roles:
        assert not any(row["intent"] == "person" for row in plan)


def providers(monkeypatch, expert_roles=()):
    from app.signals.research import HttpResearchProvider, ScrapedText
    from app.research.prior_art import adapters
    searches, pages = [], []
    async def search(provider, query, **kwargs):
        searches.append((query, kwargs))
        assert not kwargs.get("include_content")
        if query.endswith("contact booking") or query.endswith("contact") and any(query.startswith('"' + role + '"') for role in expert_roles):
            return []
        if "linkedin" in query:
            name, role = ("Raj Kumar", "Swim Coach") if "Swim" in query else ("Alice Jones", "Owner")
            return [{"url": "https://www.linkedin.com/in/alice", "title": name + " | " + role,
                "snippet": name + " | " + role + (" teaches Bangalore parents' children to swim." if "Swim" in query else " at Austin Dental, Austin US. I decide how our independent dental practice handles appointments."),
                "content": "Must never be used as profile evidence"}]
        if "site:x.com" in query:
            return [{"url": "https://twitter.com/frontdesk", "title": "Maya Rao" if "swimming" in query else "Sam Rao",
                "snippet": "I am a Bangalore parent looking for children's swimming lessons." if "swimming" in query else "I run dental reception and need help reducing patient no-shows.",
                "content": "Must never be used as profile evidence"}]
        if "reddit" in query:
            from core.clock import utcnow
            name = "swim-parent" if "swimming" in query else "dental-person"
            return [{"url": f"https://www.reddit.com/user/{name}/", "title": name,
                "metadata": {"created_utc": utcnow().timestamp()},
                "snippet": "I am a Bangalore parent looking for children's swimming lessons." if "swimming" in query else "I run dental reception and need help with missed appointments."}]
        if "forum" in query:
            return []
        start = 0 if "Austin" in query else 5
        return [{"url": f"https://practice{index}.example/contact", "title": f"Practice {index}", "snippet": "Independent dental practice in Austin US"} for index in range(start, start + 10)] + [
            {"url": "https://yelp.com/dental", "title": "Directory", "snippet": "Dentists"},
            {"url": "https://vendor.example/", "title": "Vendor", "snippet": "We sell our appointment software"}]
    async def scrape(provider, url, **kwargs):
        if not kwargs.get("main_only", True):
            pages.append(url)
        assert not any(host in url for host in ("linkedin", "reddit", "x.com", "twitter.com", "yelp", "vendor"))
        index = int(url.split("practice")[1].split(".")[0])
        host = f"practice{index}.example"
        if url.endswith("/contact"):
            text = f"Contact Practice {index}\n<form action='/send'>Send message</form>\nhello@{host}\nPhone: +1 202 555 0101"
        elif url.endswith("/team"):
            text = f"<address>Jane Doe, Practice Manager jane@{host}</address>" if index == 0 else "Our team"
        else:
            text = f"Practice {index} is an independent dental practice in Austin, US.\n[Contact](/contact) [Team](/team)"
        return ScrapedText(text, metadata={}, usage={})
    async def hn(*args, **kwargs):
        return {"items": []}
    monkeypatch.setenv("FIRECRAWL_API_KEY", "synthetic")
    monkeypatch.setenv("GTM_ENRICH_ENABLED", "1")
    monkeypatch.setattr(HttpResearchProvider, "search", search)
    monkeypatch.setattr(HttpResearchProvider, "scrape", scrape)
    monkeypatch.setattr(adapters, "search_hn", hn)
    return searches, pages


def test_business_people_routes_evidence_budget_and_suppression(journey_db, monkeypatch):
    from app.core.local_mode import is_local
    from app.gtm import journey, contact_work
    from app.db.models import GtmContact, GtmSuppression, GtmTouch, VentureDocument
    from core.clock import utcnow
    state = journey_db
    searches, pages = providers(monkeypatch)
    from app.signals.research import HttpResearchProvider
    original_search = HttpResearchProvider.search
    async def search(provider, query, **kwargs):
        return [row for row in await original_search(provider, query, **kwargs)
            if not row["title"].startswith("Practice") or int(row["title"].split()[-1]) < 7]
    monkeypatch.setattr(HttpResearchProvider, "search", search)
    models = fixture_models(monkeypatch)
    edited_queries = ["dental practices Austin", 'site:linkedin.com/in "Owner" dental', 'site:x.com "dental" "no-shows"'] if is_local() else None
    _work(state, journey.update_icp(state["wid"], state["vid"], "Independent dental practices in Austin, US",
        problem="Reduce dental appointment no-shows", search_queries=edited_queries))
    view = journey.status(state["wid"], state["vid"])
    with state["factory"]() as session:
        runtime = json.loads(session.query(VentureDocument).filter_by(kind=journey.KIND).one().provenance_json)["_runtime"]
    ceilings = runtime["ceilings"]
    leads = view["reachable_leads"]
    assert view["state"] == "ready" and not view["error_code"]
    assert len([lead for lead in leads if lead["company"].startswith("Practice")]) == 7
    assert {f"https://practice{index}.example/{path}" for index in range(7) for path in ("", "contact")} <= set(pages)
    assert len(pages) <= ceilings["signal_scrape"] + ceilings["companies"] * ceilings["contact_pages"]
    assert all(lead["source_url"] and lead["quote"] for lead in leads)
    assert all("15-minute" in lead["draft"] and "Reply with a time." in lead["draft"] for lead in leads)
    practice = next(lead for lead in leads if lead["company"] == "Practice 0")
    if is_local():
        assert practice["person"] == "Jane Doe"
        named = next(route for route in practice["routes"] if route.get("email") == "jane@practice0.example")
        assert named["source_url"] == "https://practice0.example/team" and named["email"] in named["quote"]
    else:
        assert practice["person"] is None and practice["routes"][0]["email_kind"] == "business_role"
    assert any(route["kind"] == "contact_form" for route in practice["routes"])
    profile = next(lead for lead in leads if lead["name"] == "Alice Jones")
    assert profile["role"] == "Owner" and profile["company"] == "Austin Dental"
    assert profile["source_url"] == "https://www.linkedin.com/in/alice"
    assert len(profile["connection_note"]) <= 300
    people = [lead for lead in leads if not lead["company"].startswith("Practice")]
    assert [lead["routes"][0]["kind"] for lead in people] == ["linkedin", "x"]
    x_person = next(lead for lead in people if lead["handle"])
    assert x_person["name"] == "Sam Rao" and x_person["handle"] == "@frontdesk"
    assert x_person["profile_url"] == "https://x.com/frontdesk" and x_person["source_url"] == "https://x.com/frontdesk"
    assert len(x_person["x_note"]) <= 280 and "no-shows" in x_person["value_exchange"]
    assert all("Must never" not in str(context) for stage, context in models if stage == "reachjudge")
    assert "LinkedIn profiles" in view["discovery_summary"] and "X profiles" in view["discovery_summary"]
    assert "discovery_queries" not in view
    contact_work.set_booking_link(state["vid"], "https://cal.com/founder/15")
    with_booking = journey.status(state["wid"], state["vid"])["reachable_leads"]
    assert all("https://cal.com/founder/15" in lead["draft"] for lead in with_booking)
    assert all(len(lead["x_note"]) <= 280 and "https://cal.com/founder/15" in lead["x_note"] for lead in with_booking)
    with state["factory"]() as session:
        session.add(GtmSuppression(id="suppress", workspace_id=state["wid"], email="hello@practice1.example", reason="opt_out", created_at=utcnow()))
        session.add(GtmSuppression(id="suppress-profile", workspace_id=state["wid"], linkedin_url=profile["source_url"], reason="opt_out", created_at=utcnow()))
        session.add(GtmSuppression(id="suppress-x", workspace_id=state["wid"], linkedin_url="https://twitter.com/frontdesk", reason="opt_out", created_at=utcnow()))
        session.add(GtmContact(id="stop-profile", workspace_id=state["wid"], first_name="Alice", last_name="Jones", linkedin_url=profile["source_url"], do_not_contact=1, created_at=utcnow(), stage_changed_at=utcnow()))
        assert session.query(GtmTouch).count() == 0
        session.commit()
    visible = journey.status(state["wid"], state["vid"])["reachable_leads"]
    assert not any(lead["name"] in {"Practice 1", "Alice Jones", "Sam Rao"} for lead in visible)
    search_buckets = ("signal_search", "broaden_search", "domain_search", "person_site_search")
    assert len(searches) <= sum(ceilings[bucket] for bucket in search_buckets)
    assert all(runtime["calls_used"].get(bucket, 0) <= ceilings[bucket] for bucket in search_buckets)


def test_consumer_people_are_search_only_and_no_company_contacts(journey_db, monkeypatch):
    from app.core.local_mode import is_local
    from app.gtm import journey
    from app.db.models import GtmContact, GtmTouch
    monkeypatch.delenv("EMAIL_PROVIDER", raising=False)
    expert_roles = [item["expert_role"] for item in journey._expert_queries("Bangalore parents", "Find children's swimming lessons in Bangalore")]
    searches, pages = providers(monkeypatch, expert_roles=expert_roles)
    fixture_models(monkeypatch, consumer=True)
    state = journey_db
    journey.update_icp(state["wid"], state["vid"], "Bangalore parents", problem="Find children's swimming lessons in Bangalore")
    _work(state, journey.prepare(state["wid"], state["vid"], retry=True, recruitment_opt_in=True))
    view = journey.status(state["wid"], state["vid"])
    assert {lead["name"] for lead in view["reachable_leads"]} == {"Raj Kumar", "Maya Rao", "swim-parent"}
    customer_searches = [query for query, _ in searches if not query.endswith("contact booking")
        and not any(query.startswith('"' + role + '"') for role in expert_roles)]
    assert pages == [] and len(customer_searches) == 5 and len(searches) <= journey.CALL_CEILINGS["signal_search"] and not view["can_send"]
    assert view["industry_experts"] == []
    with state["factory"]() as session:
        contacts = session.query(GtmContact).all()
        if is_local():
            assert len(contacts) == len(view["reachable_leads"])
            assert all(not contact.email and contact.lawful_basis == "none" for contact in contacts)
        else:
            assert not contacts
        assert session.query(GtmTouch).count() == 0


def test_published_contacts_stay_first_party_and_attributed(journey_db, monkeypatch):
    from app.gtm import enrich, reachable
    monkeypatch.setattr(enrich, "_FREE_MAIL", enrich._FREE_MAIL | {"personal-mail.example"})
    facts = enrich.extract("studio.example", [("https://studio.example/contact", "<form>Send message</form>\nContact: studio@personal-mail.example\nAlex Doe, Owner alex@studio.example"),
        ("https://other.example/contact", "hijack@other.example")], published_mailboxes=True)
    assert {contact["email"] for contact in facts["contacts"]} == {"studio@personal-mail.example", "alex@studio.example"}
    assert facts["contact_form"]["url"] == "https://studio.example/contact"
    assert not enrich.extract("studio.example", [("https://studio.example/", "<form>Subscribe to newsletter</form>")])["contact_form"]
    assert enrich.extract("studio.example", [("https://studio.example/team", "Dr. Alice Jones\nPractice Manager")])["people"][0]["name"] == "Alice Jones"
    assert not enrich.extract("studio.example", [("https://studio.example/", "<script>mailto:unpublished@studio.example</script>")])["contacts"]
    source = "https://reddit.com/r/espresso/comments/abc/question"
    text = "Posted by [u/beans](https://reddit.com/user/beans)\nI need better espresso beans."
    assert reachable.author_routes(source, text, "I need better espresso beans.")["author"] == "beans"
    multi = text + "\n\nPosted by [u/other](https://reddit.com/user/other)\nI need a coffee grinder."
    assert reachable.author_routes(source, multi, "I need a coffee grinder.")["author"] == "other"
    assert not reachable.author_routes(source, "[u/other](https://reddit.com/user/other)\n\nI need beans.", "I need beans.")


@pytest.mark.hosted_only
def test_linkedin_search_display_does_not_remove_fetch_ban(journey_db, monkeypatch):
    from app.gtm import journey, reachable
    from app.signals.research import HttpResearchProvider
    requests = []
    original = httpx.AsyncClient
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"success": True, "data": {"web": []}})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr("app.signals.research._pace_firecrawl", lambda key: None)
    provider = HttpResearchProvider("firecrawl", "synthetic")
    asyncio.run(provider.search('site:linkedin.com/in "Owner" dental'))
    asyncio.run(provider.search('site:linkedin.com/in "Owner" dental', include_content=True))
    assert "linkedin.com" not in requests[0]["excludeDomains"] and "scrapeOptions" not in requests[0]
    assert "linkedin.com" in requests[1]["excludeDomains"]
    assert journey._signal_url("https://linkedin.com/in/alice") is None
    with pytest.raises(ValueError, match="linkedin_fetch_forbidden"):
        asyncio.run(provider.scrape("https://www.linkedin.com/in/alice"))
    assert len(requests) == 2
    assert reachable.reach_url("https://linkedin.com/in/alice")
    assert not reachable.reach_url("http://localhost/profile")


@pytest.mark.parametrize("host", ["x.com", "twitter.com", "www.x.com", "mobile.twitter.com"])
@pytest.mark.hosted_only
def test_x_results_allowed_but_all_social_fetches_forbidden(journey_db, monkeypatch, host):
    from app.gtm import journey, reachable
    from app.signals.research import HttpResearchProvider
    requests = []
    original = httpx.AsyncClient
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"success": True, "data": {"web": []}})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr("app.signals.research._pace_firecrawl", lambda key: None)
    provider = HttpResearchProvider("firecrawl", "synthetic")
    for domain in ("x.com", "twitter.com"):
        asyncio.run(provider.search(f'site:{domain} dental "no-shows"'))
        asyncio.run(provider.search(f'site:{domain} dental "no-shows"', include_content=True))
        assert all(social not in requests[-2]["excludeDomains"] for social in ("x.com", "twitter.com"))
        assert "scrapeOptions" not in requests[-2]
        assert all(social in requests[-1]["excludeDomains"] for social in ("x.com", "twitter.com"))
    url = f"https://{host}/frontdesk/status/12345"
    assert journey._signal_url(url) is None and reachable.reach_url(url)
    with pytest.raises(ValueError, match="x_fetch_forbidden"):
        asyncio.run(provider.scrape(url))
    assert len(requests) == 4


@pytest.mark.parametrize("host", ["linkedin.com", "x.com", "twitter.com", "mobile.x.com"])
def test_social_search_normalization_never_uses_page_content(journey_db, host):
    from app.signals.research import _sources_from_response
    rows = _sources_from_response("firecrawl", {"success": True, "data": {"web": [
        {"url": f"https://{host}/person", "title": "Public name", "snippet": "Published search snippet",
            "content": "Unfetched page body must not become evidence", "markdown": "Must not be used"},
        {"url": f"https://{host}/other", "title": "Other name", "content": "Must not become a snippet", "markdown": "Must not be used"}]}})
    assert rows[0]["snippet"] == "Published search snippet" and rows[1]["snippet"] == ""
    assert all("content" not in row for row in rows)


def test_judge_cannot_invent_profile_facts_or_keep_low_fit(journey_db, monkeypatch):
    from app.gtm import journey, reachable
    from tests.unit.gtm.test_journey_signals import _leased_payload
    state = journey_db
    providers(monkeypatch)
    calls = []
    async def model(payload, stage, context):
        if stage in {"marketrole", "passages"}:
            return quality_response(stage, context)
        if stage == "plan":
            return {"queries": [{"query": "site:linkedin.com/in dental Owner Austin", "intent": "person"},
                {"query": "site:reddit.com/user espresso", "intent": "person"}]}
        if stage == "reachjudge":
            calls.append(stage)
            row = context["candidates"][0]
            if context.get("_omitted"):
                return {"items": [{"id": row["id"], "reject_reason": "No qualifying verdict for this profile."}]}
            base = {"id": row["id"], "kind": "person", "quote": row["evidence_pages"][0]["text"], "affiliation": "", "name": "Alice Jones", "url": row["url"], "role": "Owner", "fit": 90, "why": "Decision-making role fits.", "value_exchange": "Owns the independent dental practice and decides its appointment workflow."}
            return {"items": [base | {"company": "Invented Inc"}, base | {"location": "London"}, base | {"quote": "Invented evidence quote"}, base | {"fit": 40}]}
        return {"items": []}
    monkeypatch.setattr(journey, "_discovery_model", model)
    journey.update_icp(state["wid"], state["vid"], "Independent dental practices Austin US")
    payload = _leased_payload(state)
    context = {"mode": "B2B", "audience": "Independent dental practices Austin US", "problem": "appointment workflow", "competitors": [], "offer": ""}
    hits = [{"kind": "person", "url": f"https://www.linkedin.com/in/{name.lower()}", "title": f"{name} Jones - Owner at Austin Dental",
        "snippet": f"{name} Jones owns Austin Dental, an independent dental practice."} for name in ("Alice", "Bob")]
    asyncio.run(reachable.discover(None, hits, [], context, payload))
    doc = journey._journal(payload).document(payload["journey_id"])
    assert calls == ["reachjudge", "reachjudge"] and len(doc["reachable_leads"]) == 1
    lead = doc["reachable_leads"][0]
    assert lead["name"] == "Alice Jones" and lead["company"] == "Austin Dental" and lead["location"] == ""
    assert lead["quote"] != "Invented evidence quote" and lead["fit"] == 90


def test_weakly_related_people_without_value_exchange_are_excluded(journey_db, monkeypatch):
    from app.gtm import journey, reachable
    from tests.unit.gtm.test_journey_signals import _leased_payload
    drafts = []
    async def model(payload, stage, context):
        if stage in {"marketrole", "passages"}:
            return quality_response(stage, context)
        if stage == "reachjudge":
            return {"items": [{"id": row["id"], "kind": "person", "quote": row["evidence_pages"][0]["text"], "affiliation": "", "name": "Alex Lee",
                "url": row["url"], "role": "Designer", "fit": 95, "why": "Related creative industry; could network.",
                "value_exchange": "   "} for row in context["candidates"]]}
        drafts.append(stage)
        return {"items": []}
    monkeypatch.setattr(journey, "_discovery_model", model)
    journey.update_icp(journey_db["wid"], journey_db["vid"], "Independent dental practices")
    journey.prepare(journey_db["wid"], journey_db["vid"], retry=True)
    hits = [{"kind": "person", "url": url, "title": "Alex Lee | Designer", "snippet": "Designer who enjoys dental logos."}
        for url in ("https://linkedin.com/in/alex", "https://x.com/alex/status/123")]
    context = {"mode": "B2B", "audience": "Independent dental practices", "problem": "patient no-shows", "competitors": [], "offer": ""}
    assert asyncio.run(reachable.discover(None, hits, [], context, _leased_payload(journey_db))) == []
    assert drafts == []
    assert reachable.visible([{"name": "Alex Lee", "routes": [{"kind": "linkedin", "url": hits[0]["url"]}],
        "why": "Shared industry", "value_exchange": ""}], journey_db["wid"]) == []


def test_x_profiles_only_never_use_status_evidence(journey_db, monkeypatch):
    from app.gtm import journey, reachable
    from tests.unit.gtm.test_journey_signals import _leased_payload
    async def model(payload, stage, context):
        if stage in {"marketrole", "passages"}:
            return quality_response(stage, context)
        row = context["candidates"][0]
        if stage == "reachjudge":
            assert len(context["candidates"]) == 1
            assert len(row["evidence_pages"]) == 1
            page = row["evidence_pages"][0]
            return {"items": [{"id": row["id"], "kind": "person", "quote": page["text"], "affiliation": "", "name": "Sam Rao",
                "url": page["url"], "fit": 90, "why": "Concrete workflow problem.",
                "value_exchange": "Runs dental reception and asks for help reducing patient no-shows."}]}
        return {"items": [{"id": row["id"], "body": "I'm researching no-shows. Open to a 15-minute call? Reply with a time.",
            "connection_note": "I'm researching no-shows. Open to a 15-minute call? Reply with a time."}]}
    monkeypatch.setattr(journey, "_discovery_model", model)
    journey.update_icp(journey_db["wid"], journey_db["vid"], "Independent dental practices")
    journey.prepare(journey_db["wid"], journey_db["vid"], retry=True)
    hits = [{"kind": "person", "url": "https://x.com/frontdesk", "title": "Sam Rao", "snippet": "I run dental reception and need help reducing patient no-shows."},
        {"kind": "person", "url": "https://twitter.com/frontdesk/status/123", "title": "Sam Rao", "snippet": "I run dental reception and need help reducing patient no-shows."}]
    context = {"mode": "B2B", "audience": "Independent dental practices", "problem": "patient no-shows", "competitors": [], "offer": ""}
    leads = asyncio.run(reachable.discover(None, hits, [], context, _leased_payload(journey_db)))
    assert len(leads) == 1 and leads[0]["source_url"] == hits[0]["url"]
    assert leads[0]["handle"] == "@frontdesk" and leads[0]["routes"][0]["url"] == hits[0]["url"]
    assert leads[0]["routes"][0]["source_url"] == hits[0]["url"]


def test_people_source_priority_precedes_fit(journey_db):
    from app.gtm.reachable import rank
    leads = [{"id": kind, "fit": fit, "routes": [{"kind": kind}]} for kind, fit in (("forum", 100), ("x", 90), ("linkedin", 70))]
    assert [lead["id"] for lead in sorted(leads, key=rank)] == ["linkedin", "x", "forum"]


def test_one_invalid_model_item_does_not_discard_valid_neighbors(journey_db):
    from app.gtm.discovery import Judgements, ReachDrafts, parse_response
    valid = {"id": 0, "kind": "person", "quote": "I handle dental appointment scheduling.", "affiliation": "", "name": "Jane Doe", "url": "https://linkedin.com/in/jane"}
    rejected = []
    answer = parse_response(Judgements, json.dumps({"items": [valid, valid | {"id": 1, "quote": ""}]}), rejected=lambda row, issues: rejected.append((row["id"], issues)))
    assert len(answer["items"]) == 1 and rejected[0][0] == 1
    safe = {"id": 0, "body": "I'm researching scheduling. Open to a 15-minute call? Reply with a time.", "connection_note": "Open to a 15-minute call?"}
    assert len(parse_response(ReachDrafts, json.dumps({"items": [safe, safe | {"id": 1, "body": "Email me at guessed@example.com"}]}))["items"]) == 1
    with pytest.raises(ValueError):
        parse_response(Judgements, json.dumps({"items": "not a list"}))


def test_rate_limited_site_and_invalid_draft_keep_json_safe_leads(journey_db, monkeypatch):
    from app.gtm import enrich, journey, reachable
    from app.gtm.discovery import ReachDrafts, parse_response
    from tests.unit.gtm.test_journey_signals import _leased_payload
    fixture_models(monkeypatch)
    original_model = journey._discovery_model
    async def model(payload, stage, context):
        answer = await original_model(payload, stage, context)
        if stage == "reachdraft":
            answer["items"][0]["body"] = "Contact guessed@example.com"
            def rejected(row, issues):
                journey._journal(payload).mutate(payload["journey_id"], payload,
                    lambda row, doc, session: doc["_runtime"].setdefault("rejected", []).append(issues))
            return parse_response(ReachDrafts, json.dumps(answer), rejected=rejected)
        return answer
    async def limited(*args, **kwargs):
        raise httpx.HTTPStatusError("rate limited", request=httpx.Request("POST", "https://api.firecrawl.dev/v2/scrape"), response=httpx.Response(429))
    monkeypatch.setattr(journey, "_discovery_model", model)
    monkeypatch.setattr(enrich, "enrich_site", limited)
    monkeypatch.setenv("GTM_ENRICH_ENABLED", "1")
    context = {"mode": "B2B", "audience": "Independent dental practices", "problem": "patient no-shows", "competitors": [], "offer": ""}
    hits = [{"kind": "business", "url": "https://practice.example/", "title": "Practice", "snippet": "Independent dental practice"},
        {"kind": "person", "url": "https://www.linkedin.com/in/alice", "title": "Alice Jones | Owner", "snippet": "Owner at Austin Dental. I decide how our independent dental practice handles appointments."},
        {"kind": "person", "url": "https://x.com/frontdesk", "title": "Sam Rao", "snippet": "I run dental reception and need help reducing patient no-shows."}]
    journey.update_icp(journey_db["wid"], journey_db["vid"], context["audience"])
    payload = _leased_payload(journey_db)
    leads = asyncio.run(reachable.discover(object(), hits, [], context, payload))
    assert len(leads) == 2 and all(lead["draft"] for lead in leads)
    assert {lead["draft_source"] for lead in leads} == {"model", "template"}
    document = journey._journal(payload).document(payload["journey_id"])
    assert "draft_not_safe_or_short" in json.dumps(document)
    assert "ctx" not in document["_runtime"]["rejected"][0][0]


@pytest.mark.parametrize("url,expected", [
    ("https://uk.linkedin.com/in/fixture-profile-123/?trk=public_profile#about", "https://www.linkedin.com/in/fixture-profile-123"),
    ("https://mobile.twitter.com/DesignerJane?ref_src=search#bio", "https://x.com/designerjane"),
    ("https://www.x.com/DesignerJane/?s=20", "https://x.com/designerjane")])
def test_reach_profile_urls_are_canonical_without_tracking(journey_db, url, expected):
    from app.gtm import reachable
    assert reachable.reach_url(url) == expected


def test_eval_records_and_replays_only_safe_rate_limit_headers(tmp_path):
    from contextlib import ExitStack
    import importlib.util
    spec = importlib.util.spec_from_file_location("eval_reachable_leads", Path(__file__).resolve().parents[3] / "scripts/qa/eval_reachable_leads.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    Exchanges = module.Exchanges
    file = tmp_path / "rate-limit.json"
    recorder = Exchanges(file)
    original = httpx.AsyncClient.send
    async def record():
        def respond(request):
            return httpx.Response(429, headers={"Retry-After": "2", "Set-Cookie": "private", "Authorization": "private"}, json={"error": "rate limited"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            request = client.build_request("POST", "https://api.firecrawl.dev/v2/search", json={"query": "dental"})
            await recorder.send(original, client, request)
    asyncio.run(record())
    assert json.loads(file.read_text())["exchanges"][0]["headers"] == {"Retry-After": "2"}
    async def replay():
        with ExitStack() as patches:
            Exchanges(file, replay=True).install(patches)
            async with httpx.AsyncClient() as client:
                response = await client.post("https://api.firecrawl.dev/v2/search", json={"query": "dental"})
                assert response.status_code == 429 and response.headers["Retry-After"] == "2"
                assert "set-cookie" not in response.headers and "authorization" not in response.headers
    asyncio.run(replay())


def test_incomplete_model_plan_still_issues_x_search(journey_db, monkeypatch):
    from app.core.local_mode import is_local
    from app.gtm import journey
    fixture_models(monkeypatch)
    original = journey._discovery_model
    async def model(payload, stage, context):
        if stage == "plan":
            return {"queries": [{"query": "site:linkedin.com/in dental Owner Austin", "intent": "person"},
                {"query": "site:linkedin.com/in dental front desk Boston", "intent": "person"}]}
        return await original(payload, stage, context)
    monkeypatch.setattr(journey, "_discovery_model", model)
    searches, pages = providers(monkeypatch)
    monkeypatch.setenv("FIRECRAWL_API_KEY", "synthetic")
    journey.update_icp(journey_db["wid"], journey_db["vid"], "Independent dental practices in the US")
    _work(journey_db, journey.prepare(journey_db["wid"], journey_db["vid"]))
    view = journey.status(journey_db["wid"], journey_db["vid"])
    assert "business websites" in view["discovery_summary"]
    if is_local():
        assert any("dental" in query.lower() and "contact" in query.lower() for query, _ in searches)
        assert not any('site:x.com intitle:"/ X"' in query for query, _ in searches)
    else:
        assert any(query == "site:linkedin.com/in dental Owner" for query, _ in searches)
        assert any('site:x.com intitle:"/ X"' in query for query, _ in searches)
    assert any('site:reddit.com' in item["query"] for item in journey._fallback_plan("Bangalore parents", "swimming lessons", []))


def test_realistic_firecrawl_x_profiles_accept_aliases_not_posts(journey_db, monkeypatch):
    from app.gtm import journey, reachable
    from app.signals.research import _sources_from_response
    from tests.unit.gtm.test_journey_signals import _leased_payload
    fixture_models(monkeypatch)
    rows = [{"url": url, "title": "Sam Rao (@FrontDesk) / X", "description": "Sam Rao: I run dental reception and need help reducing patient no-shows."}
        for url in ("https://x.com/FrontDesk?ref=search", "https://twitter.com/frontdesk", "https://x.com/frontdesk/status/123", "https://x.com/i/123", "https://x.com/search", "https://twitter.com/hashtag/dental")]
    hits = [row | {"kind": "person"} for row in _sources_from_response("firecrawl", {"success": True, "data": {"web": rows}})]
    original = journey._discovery_model
    async def model(payload, stage, context):
        answer = await original(payload, stage, context)
        if stage == "reachjudge":
            for item in answer["items"]:
                item.update(name="Sam Rao", value_exchange="Runs dental reception and asks for help reducing no-shows.")
        return answer
    monkeypatch.setattr(journey, "_discovery_model", model)
    context = {"mode": "B2C", "audience": "Dental reception staff", "problem": "patient no-shows", "competitors": [], "offer": ""}
    journey.update_icp(journey_db["wid"], journey_db["vid"], context["audience"])
    leads = asyncio.run(reachable.discover(None, hits, [], context, _leased_payload(journey_db)))
    assert len(leads) == 1 and leads[0]["routes"][0]["url"] == "https://x.com/frontdesk"
    assert leads[0]["handle"] == "@frontdesk"


def test_personal_brand_forms_and_explicit_obfuscation_keep_published_evidence(journey_db):
    from app.gtm import enrich
    page = "https://designer.example/contact"
    raw = "Please complete the form below.\nName\nEmail(required)\nMessage(required)\nhello [at] designer [dot] example"
    facts = enrich.extract("designer.example", [(page, raw)], published_mailboxes=True)
    assert facts["contact_form"]["url"] == page
    contact = facts["contacts"][0]
    assert contact["email"] == "hello@designer.example"
    assert contact["published_as"] == "hello [at] designer [dot] example" and contact["published_as"] in contact["quote"]
    assert not enrich.extract("designer.example", [(page, "Subscribe to my newsletter\nName\nEmail\nSubmit")])["contact_form"]
    alias = enrich.extract("hart-studio.preview.example", [(page.replace("designer.example", "hart-studio.preview.example"), "[Email](mailto:info@hart-studio.example)")], published_mailboxes=True)
    assert not alias["contacts"]
    assert enrich.classify("https://behance.net/hire/designers", "Designer") == "directory"


def test_coffee_forum_author_header_belongs_to_its_own_post(journey_db):
    from app.gtm.reachable import author_routes
    source = "https://coffeeforums.com/threads/beans.123/"
    text = "#### [fixturebean90](https://coffeeforums.com/members/fixturebean90.1/)\n\nNew member\n\nI need fresh espresso beans.\n\n#### [other](https://coffeeforums.com/members/other.2/)\n\nI need a grinder."
    assert author_routes(source, text, "I need fresh espresso beans.")["author"] == "fixturebean90"
    assert author_routes(source, text, "I need a grinder.")["author"] == "other"


def test_published_workflow_role_needs_no_date_or_exact_case_and_draft_failure_keeps_route(journey_db, monkeypatch):
    from app.gtm import journey, reachable
    from tests.unit.gtm.test_journey_signals import _leased_payload
    async def model(payload, stage, context):
        if stage in {"marketrole", "passages"}:
            return quality_response(stage, context)
        if stage == "reachdraft":
            raise ValueError("invalid draft")
        row = context["candidates"][0]
        return {"items": [{"id": row["id"], "kind": "person", "quote": row["evidence_pages"][0]["text"], "affiliation": "", "name": "Sorrel Finspark", "url": row["url"],
            "role": "Swimming Coach", "company": "Invented academy", "location": "Bangalore", "fit": 85,
            "why": "Local coach serves parents.", "value_exchange": "Teaches children in Bangalore; parent progress feedback is a relevant partnership discussion."}]}
    monkeypatch.setattr(journey, "_discovery_model", model)
    journey.update_icp(journey_db["wid"], journey_db["vid"], "Bangalore parents")
    journey.prepare(journey_db["wid"], journey_db["vid"], retry=True, recruitment_opt_in=True)
    context = {"mode": "B2C", "audience": "Bangalore parents", "problem": "swim progress", "competitors": [], "offer": ""}
    hits = [{"kind": "person", "url": "https://in.linkedin.com/in/fixture-swim-workflow", "title": "Sorrel Finspark - swimming coach", "snippet": "I teach children in bangalore."}]
    leads = asyncio.run(reachable.discover(None, hits, [], context, _leased_payload(journey_db)))
    assert len(leads) == 1 and leads[0]["role"] == "swimming coach" and leads[0]["location"] == "bangalore"
    assert leads[0]["company"] == "" and leads[0]["draft_source"] == "template"
    assert "15-minute" in leads[0]["draft"] and leads[0]["routes"][0]["kind"] == "linkedin"


def test_synthetic_failed_recordings_preserve_forms_and_valid_judgement_rows(journey_db):
    from pathlib import Path
    from app.gtm import enrich
    from app.gtm.discovery import Judgements, parse_response
    recorded = json.loads((Path(__file__).parent / "fixtures/reachable_live_failures.json").read_text())
    for page in recorded["contact_pages"]:
        facts = enrich.extract(enrich._host(page["url"]), [(page["url"], page["text"])], published_mailboxes=True)
        assert facts["contacts"] or facts["contact_form"], page["url"]
        for contact in facts["contacts"]:
            assert contact["email"] in contact["quote"]
    discarded = []
    judged = parse_response(Judgements, json.dumps(recorded["brew_judgement"]), rejected=lambda row, issues: discarded.append(row["id"]))
    assert len(judged["items"]) == 8 and len(discarded) == 2
    assert recorded["empty_judgements"] == [{"items": []}, {"items": []}]


@pytest.mark.parametrize("audience,problem,mode,expected", [
    ("Commercial greenhouse operators", "equipment maintenance delays", "B2B", False),
    ("Families arranging weekend activities", "tracking pottery practice", "B2C", True),
])
def test_topic_people_remain_candidates_only_without_an_explicit_customer_role(journey_db, audience, problem, mode, expected):
    from app.gtm.reachable import candidate_hits

    hit = {"url": "https://linkedin.com/in/jordan-ellis", "title": "Jordan Ellis",
        "snippet": f"Jordan Ellis teaches workshops about {problem}.", "kind": "person"}
    context = {"audience": audience, "problem": problem, "mode": mode}
    assert bool(candidate_hits([hit], context)) is expected
    for constraint in ({"target_type": "person", "requested_role": audience},
                       {"industry": audience}, {"requested_role": audience}):
        assert not candidate_hits([hit], context | constraint)


def test_synthetic_swim_profiles_keep_four_grounded_people_without_invented_case(journey_db, monkeypatch):
    from pathlib import Path
    from app.gtm import journey, reachable
    from tests.unit.gtm.test_journey_signals import _leased_payload
    recorded = json.loads((Path(__file__).parent / "fixtures/reachable_live_failures.json").read_text())
    def linkedin(row):
        return row | {"url": row["url"].replace("profiles.test", "www.linkedin.com")}
    async def model(payload, stage, context):
        if stage in {"marketrole", "passages"}:
            return quality_response(stage, context)
        return {"items": [linkedin(row) for row in recorded["splash_judgement"]["items"]]} if stage == "reachjudge" else {"items": []}
    monkeypatch.setattr(journey, "_discovery_model", model)
    journey.update_icp(journey_db["wid"], journey_db["vid"], "Toronto parents", recruitment_opt_in=True)
    context = {"mode": "B2C", "audience": "Toronto parents", "problem": "children's swimming progress", "competitors": [], "offer": ""}
    hits = [linkedin(row) | {"kind": "person"} for row in recorded["splash_profiles"]]
    leads = asyncio.run(reachable.discover(None, hits, [], context, _leased_payload(journey_db)))
    assert {lead["name"] for lead in leads if lead["judging_status"] == "judged"} == {"Aster Tumblefin", "Pip.Pondstar Pip.Pondstar", "ORBIT SPOONWAVE", "Moss Bubbletail"}
    assert [lead["name"] for lead in leads if lead["judging_status"] == "pending"] == ["PEBBLE TEACUP"]
    assert all(lead["quote"] and lead["value_exchange"] and lead["draft_source"] == "template" for lead in leads)
    for lead in leads:
        row = next(row for row in hits if reachable.reach_url(row["url"]) == lead["source_url"])
        assert all(not lead[field] or journey._verbatim(lead[field], row["title"] + "\n" + row["snippet"]) for field in ("name", "role", "company", "location"))


def test_draft_cannot_address_another_recipient_after_lead_order_changes(journey_db, monkeypatch):
    from app.gtm import journey, reachable
    from tests.unit.gtm.test_journey_signals import _leased_payload
    events = []
    async def model(payload, stage, context):
        if stage in {"marketrole", "passages"}:
            return quality_response(stage, context)
        if stage == "reachjudge":
            return {"items": [{"id": 0, "kind": "person", "name": "Alice Jones", "affiliation": "", "role": "Office Manager", "company": "", "location": "",
                "url": context["candidates"][0]["url"], "quote": "Office Manager coordinating patient scheduling", "fit": 85,
                "why": "Responsible for scheduling", "value_exchange": "Manages patient scheduling; could advise on rebooking."}]}
        return {"items": [{"id": 0, "body": "Hi Meera, would you be open to a 15-minute call? Reply with a time.",
            "connection_note": "Hi Meera, open to a 15-minute call? Reply with a time."}]}
    monkeypatch.setattr(journey, "_discovery_model", model)
    monkeypatch.setattr(reachable, "trace", lambda payload, url, stage, reason="", **facts: events.append((stage, reason)))
    journey.update_icp(journey_db["wid"], journey_db["vid"], "dental office managers")
    context = {"mode": "B2B", "audience": "dental office managers", "problem": "patient no-shows", "competitors": [], "offer": ""}
    hits = [{"kind": "person", "url": "https://linkedin.com/in/alice-jones", "title": "Alice Jones - Office Manager",
        "snippet": "Office Manager coordinating patient scheduling"}]
    leads = asyncio.run(reachable.discover(None, hits, [], context, _leased_payload(journey_db)))
    assert len(leads) == 1 and leads[0]["draft_source"] == "template"
    assert "Hi Alice," in leads[0]["draft"] and "Meera" not in leads[0]["draft"]
    assert ("draft_rejected", "wrong_recipient") in events


def test_recorded_blank_dental_plan_falls_back_to_five_practices_and_two_people_searches(journey_db):
    from pathlib import Path
    from pydantic import ValidationError
    from app.core.local_mode import is_local
    from app.gtm import journey
    from app.gtm.discovery import Plan
    fixture = json.loads((Path(__file__).parent / "fixtures/reachable_second_run.json").read_text())
    with pytest.raises(ValidationError):
        Plan.model_validate(fixture["recall"]["plan"])
    plan = Plan.model_validate({"queries": journey._fallback_plan("Independent dental clinics in the US", "dental no-show rebooking", [])})
    assert len(plan.queries) == 8
    assert [row.intent for row in plan.queries] == ["business"] * 3 + ["person"] * 2 + ["customer_voice", "need_hiring", "prior_complaints"]
    assert all("contact" in row.query for row in plan.queries[:3])
    cities = ("New York", "Los Angeles", "Chicago")
    if is_local():
        assert all("US" in row.query and not any(city in row.query for city in cities) for row in plan.queries[:3])
    else:
        assert all(city in row.query for city, row in zip(cities, plan.queries[:3]))
    assert "linkedin.com/in" in plan.queries[3].query and "site:x.com" in plan.queries[4].query
    designers = journey._fallback_plan("UK freelance graphic designers", "late invoice reminders", [])
    assert [row["intent"] for row in designers[:3]] == ["business"] * 3
    assert not any(city in str(designers) for city in ("London", "Birmingham"))
    unknown_locale = journey._fallback_plan("Independent dental clinics", "dental no-shows", [])
    assert not any(city in str(unknown_locale) for city in ("Austin", "Boston", "London"))
    espresso = Plan.model_validate({"queries": journey._fallback_plan("Home espresso enthusiasts US Canada", "fresh beans", [])})
    assert [row.intent for row in espresso.queries[:2]] == ["community"] * 2
    assert all(row.intent == "customer_voice" for row in espresso.queries[2:])


def test_recorded_x_vendors_and_unsubstantiated_brand_accounts_fail_even_with_high_fit(journey_db, monkeypatch):
    from pathlib import Path
    from app.gtm import journey, reachable
    from tests.unit.gtm.test_journey_signals import _leased_payload
    fixture = json.loads((Path(__file__).parent / "fixtures/reachable_second_run.json").read_text())
    hits = [row | {"url": row["url"].replace("short-posts.test", "x.com"), "kind": "person"} for row in fixture["freelance_design"]["profiles"]]
    def answer(context):
        return {"items": [{"id": row["id"], "kind": "person", "name": row["title"].split(" (")[0].split(" - ")[0],
            "affiliation": "", "url": row["url"], "quote": row["evidence_pages"][0]["text"], "fit": 95, "why": "Claims fit.",
            "value_exchange": "Could advise on invoice follow-ups."} for row in context["candidates"]]}
    async def model(payload, stage, context):
        if stage in {"marketrole", "passages"}:
            return quality_response(stage, context)
        return answer(context) if stage == "reachjudge" else {"items": []}
    monkeypatch.setattr(journey, "_discovery_model", model)
    journey.update_icp(journey_db["wid"], journey_db["vid"], "UK freelance graphic designers")
    context = {"mode": "B2B", "audience": "UK freelance graphic designers", "problem": "invoice payment reminders", "offer": "invoice payment reminders", "competitors": []}
    leads = asyncio.run(reachable.discover(None, hits, [], context, _leased_payload(journey_db)))
    assert {lead["profile_url"] for lead in leads} == {"https://x.com/fixturebranding"}
    assert leads[0]["role"] == "Freelance Graphic Designer"


def test_brand_partner_requires_verbatim_program_evidence(journey_db, monkeypatch):
    from app.gtm import journey, reachable
    from tests.unit.gtm.test_journey_signals import _leased_payload
    quote = "Home espresso brewing classes for Canadian enthusiasts."
    async def model(payload, stage, context):
        if stage in {"marketrole", "passages"}:
            return quality_response(stage, context)
        return {"items": [{"id": 0, "kind": "person", "name": "Brew School", "affiliation": "", "url": context["candidates"][0]["url"],
            "quote": quote, "fit": 85, "why": "Teaches target customers", "value_exchange": "Could share bean-freshness lessons with home brewers.",
            "partner_quote": quote}]} if stage == "reachjudge" else {"items": []}
    monkeypatch.setattr(journey, "_discovery_model", model)
    journey.update_icp(journey_db["wid"], journey_db["vid"], "Home espresso enthusiasts US Canada", recruitment_opt_in=True)
    context = {"mode": "B2C", "audience": "Home espresso enthusiasts US Canada", "problem": "fresh beans", "offer": "", "competitors": []}
    leads = asyncio.run(reachable.discover(None, [{"kind": "person", "url": "https://x.com/brewschool", "title": "Brew School", "snippet": quote}], [], context, _leased_payload(journey_db)))
    assert len(leads) == 1 and leads[0]["value_exchange"]


def test_html_label_does_not_create_an_unpublished_email(journey_db):
    from app.gtm import enrich
    raw = '<p>Email<a href="mailto:alex.drew@accounting-studio.example">alex.drew@accounting-studio.example</a></p>'
    contacts = enrich.extract("accounting-studio.example", [("https://accounting-studio.example/contact", raw)], published_mailboxes=True)["contacts"]
    assert [contact["email"] for contact in contacts] == ["alex.drew@accounting-studio.example"]


def test_raw_html_contacts_decode_only_published_tokens_with_exact_provenance(journey_db):
    from app.gtm import enrich
    email = "office@clinic.example"
    key = 93
    encoded = f"{key:02x}" + bytes(value ^ key for value in email.encode()).hex()
    raw = (f'<span data-cfemail="{encoded}">protected</span>'
        '<a href="mailto:Front.Desk%40clinic.example?subject=hi">Email</a>'
        '<script type="application/ld+json">{"@type":"Dentist","contactPoint":{"email":"hello@clinic.example","telephone":"+1 202 555 0100"}}</script>'
        '<p>jane [at] clinic [dot] example</p> office&#64;clinic.example'
        '<script>const tracker="billing@clinic.example";</script><img src="logo@2x.example">'
        '<form><input type="email" name="email"><textarea name="msg"></textarea></form>')
    facts = enrich.extract("clinic.example", [("https://clinic.example/kontakt", raw)], published_mailboxes=True)
    assert {contact["email"] for contact in facts["contacts"]} == {email, "front.desk@clinic.example", "hello@clinic.example", "jane@clinic.example"}
    assert facts["contact_form"] and facts["phone"]["value"] == "+1 202 555 0100"
    for contact in facts["contacts"]:
        assert contact["quote"] in raw
        assert contact.get("published_as", contact["email"]).lower() in contact["quote"].lower()
        assert contact["source_url"] == "https://clinic.example/kontakt"
    invalid = enrich.extract("clinic.example", [("https://clinic.example/", '<span data-cfemail="a">broken</span><script>const email="hello@clinic.example";</script>')], published_mailboxes=True)
    assert not invalid["contacts"]


def test_multilingual_anchor_scoring_and_newsletter_is_not_contact_form(journey_db):
    from app.gtm import enrich
    raw = '<a href="/legal">Impressum</a><a href="/get-help">お問い合わせ</a><a href="/about">About</a><a href="https://other.example/contact">Contact</a>'
    assert enrich._links(raw, "https://clinic.example/") == ["https://clinic.example/legal", "https://clinic.example/get-help", "https://clinic.example/about"]
    assert not enrich.extract("clinic.example", [("https://clinic.example/", '<form><input type="email">Subscribe</form>')])["contact_form"]
    blocked = enrich.extract("clinic.example", [("https://clinic.example/", 'hello@clinic.example No solicitation')])
    assert blocked["no_solicitation"] and not blocked["contacts"]


def test_xray_queries_titles_and_outreach_permission_hints(journey_db):
    from app.gtm import journey
    from app.gtm.discovery import Query
    from app.gtm.reachable import profile_facts, outreach_hint
    query = 'site:linkedin.com/in intitle:"practice owner" dentist "Austin, Texas" -jobs -hiring -recruiter'
    assert Query(query=query, intent="person").query == query.split(" -jobs")[0]
    parsed = profile_facts("Jonas Kelven - Dental Practice Owner - Examplevale Dental | LinkedIn", query)
    assert parsed["name"] == "Jonas Kelven" and parsed["role"] == "Dental Practice Owner" and parsed["company"] == "Examplevale Dental"
    assert profile_facts("Pella Playvale (@pellaplayvale) / X", "Freelance designer")["handle"] == "@pellaplayvale"
    assert "ask permission before sending marketing email" in outreach_hint("GB")["note"]
    assert outreach_hint("GB", {"url": "https://registry.example/company/123"})["subscriber"] == "corporate"
    assert journey._business_cities("Independent dentists in Raleigh, NC and Portland, OR in the US") == [("Raleigh", "NC"), ("Portland", "OR")]
    assert journey._business_cities("UK freelance graphic designers in Oxford") == [("Oxford", "")]


def test_judge_id_repair_requires_unique_exact_source_and_quote(journey_db, monkeypatch):
    from app.gtm import journey, reachable
    from tests.unit.gtm.test_journey_signals import _leased_payload
    hits = [{"kind": "person", "url": "https://linkedin.com/in/alice", "title": "Alice Jones - Office Manager", "snippet": "Office Manager coordinates patient scheduling"},
        {"kind": "person", "url": "https://linkedin.com/in/bob", "title": "Bob Smith - Office Manager", "snippet": "Office Manager coordinates dental appointments"}]
    async def model(payload, stage, context):
        if stage in {"marketrole", "passages"}:
            return quality_response(stage, context)
        if stage == "reachjudge":
            return {"items": [{"id": 0, "kind": "person", "name": "Bob Smith", "affiliation": "", "quote": hits[1]["snippet"], "url": hits[1]["url"],
                "fit": 90, "why": "Decides scheduling", "value_exchange": "Coordinates dental appointments; can discuss rebooking."}]}
        return {"items": []}
    monkeypatch.setattr(journey, "_discovery_model", model)
    journey.update_icp(journey_db["wid"], journey_db["vid"], "Dental office managers")
    context = {"mode": "B2B", "audience": "Dental office managers", "problem": "No-shows", "offer": "", "competitors": []}
    leads = asyncio.run(reachable.discover(None, hits, [], context, _leased_payload(journey_db)))
    assert len(leads) == 1 and leads[0]["name"] == "Bob Smith" and leads[0]["source_url"] == reachable.reach_url(hits[1]["url"])


def test_registry_is_candidate_only_and_duplicate_slug_is_hidden(journey_db, monkeypatch):
    from app.gtm import enrich, journey, reachable
    from tests.unit.gtm.test_journey_signals import _leased_payload
    from app.signals.research import HttpResearchProvider
    source = "https://npiregistry.cms.hhs.gov/provider-view/1234567890"
    home = "https://austindental.example/"
    text = "Austin Dental LLC is an independent practice coordinating patient appointments in Austin, US."
    hits = [{"kind": "business", "provider": "firecrawl", "url": home, "registry_url": source, "title": "Austin Dental LLC", "snippet": text, "phone": "2025550100"},
        {"kind": "person", "url": "https://linkedin.com/in/alice", "title": "Alice Jones - Office Manager", "snippet": "Office Manager coordinates patient scheduling"},
        {"kind": "person", "url": "https://us.linkedin.com/in/ALICE/?trk=public_profile#about", "title": "Alice Jones - Office Manager", "snippet": "Office Manager coordinates patient scheduling"}]
    async def model(payload, stage, context):
        if stage in {"marketrole", "passages"}:
            return quality_response(stage, context)
        return {"items": [{"id": row["id"], "kind": row["kind"], "name": "Austin Dental LLC" if row["kind"] == "business" else "Alice Jones", "affiliation": "", "quote": row["evidence_pages"][0]["text"], "url": row["url"],
            "fit": 90, "why": "Runs a dental practice", "value_exchange": "Can discuss practice scheduling."} for row in context["candidates"]]} if stage == "reachjudge" else {"items": []}
    async def enrich_site(*args, **kwargs):
        facts = enrich.extract("austindental.example", [(home, text)])
        facts.update(company_text=text, evidence_pages=[{"url": home, "text": text, "relevance": 0.9}],
            market_role=quality_response("marketrole", {"candidates": [{"text": text}]}),
            phone={"value": "2025550100", "source_url": source, "quote": "Austin Dental LLC Dentist LOCATION phone 2025550100"},
            country="US", profile_urls=[], careers_url=None)
        return facts
    monkeypatch.setattr(journey, "_discovery_model", model)
    monkeypatch.setattr(enrich, "enrich_site", enrich_site)
    monkeypatch.setenv("FIRECRAWL_API_KEY", "synthetic")
    monkeypatch.setenv("GTM_ENRICH_ENABLED", "1")
    journey.update_icp(journey_db["wid"], journey_db["vid"], "Dental office managers")
    context = {"mode": "B2B", "audience": "Dental office managers", "problem": "No-shows", "offer": "", "competitors": []}
    leads = asyncio.run(reachable.discover(HttpResearchProvider("firecrawl", "synthetic"), hits, [], context, _leased_payload(journey_db)))
    assert len(leads) == 1
    assert leads[0]["name"] == "Alice Jones"
    assert all(route["source_url"] != source for lead in leads for route in lead["routes"])
    assert not any(lead["name"] == "Austin Dental LLC" for lead in leads)


def test_person_business_dedupe_uses_only_published_profile_and_preserves_suppression(journey_db, monkeypatch):
    from app.gtm import journey, reachable
    from app.signals.research import HttpResearchProvider
    from app.db.models import GtmSuppression
    from core.clock import utcnow
    from tests.unit.gtm.test_journey_signals import _leased_payload
    profile = "https://uk.linkedin.com/in/alice-jones"
    async def scrape(provider, url, **kwargs):
        return f"Alice Jones Design Studio\nFreelance graphic designer in London\n[LinkedIn]({profile})\nhello@alicestudio.example"
    async def model(payload, stage, context):
        if stage in {"marketrole", "passages"}:
            return quality_response(stage, context)
        return {"items": [{"id": row["id"], "kind": row["kind"], "affiliation": "", "name": "Alice Jones Design Studio" if row["kind"] == "business" else "Alice Jones",
            "url": row["evidence_pages"][0]["url"], "quote": "Freelance graphic designer in London", "fit": 90,
            "why": "Published design practice fits invoice workflow", "value_exchange": "Decides her design practice's client workflow."} for row in context["candidates"]]} if stage == "reachjudge" else {"items": []}
    monkeypatch.setenv("FIRECRAWL_API_KEY", "synthetic")
    monkeypatch.setenv("GTM_ENRICH_ENABLED", "1")
    monkeypatch.setattr(HttpResearchProvider, "scrape", scrape)
    monkeypatch.setattr(journey, "_discovery_model", model)
    journey.update_icp(journey_db["wid"], journey_db["vid"], "UK freelance graphic designers")
    context = {"mode": "B2B", "audience": "UK freelance graphic designers", "problem": "invoice follow-ups", "offer": "", "competitors": []}
    hits = [{"kind": "business", "url": "https://alicestudio.example/", "title": "Alice Jones Design Studio", "snippet": "Freelance graphic designer in London"},
        {"kind": "person", "url": profile, "title": "Alice Jones", "snippet": "Freelance graphic designer in London"}]
    leads = asyncio.run(reachable.discover(HttpResearchProvider("firecrawl", "synthetic"), hits, [], context, _leased_payload(journey_db)))
    assert len(leads) == 1 and leads[0]["person"] == "Alice Jones"
    assert {route["kind"] for route in leads[0]["routes"]} == {"email", "linkedin"}
    with journey_db["factory"]() as session:
        session.add(GtmSuppression(id="profile-stop", workspace_id=journey_db["wid"], linkedin_url="https://www.linkedin.com/in/alice-jones/", reason="opt_out", created_at=utcnow()))
        session.commit()
    assert reachable.visible(leads, journey_db["wid"]) == []


def test_testimonial_name_is_not_a_business_owner_or_contact(journey_db, monkeypatch):
    from app.gtm import journey, reachable
    from app.signals.research import HttpResearchProvider
    from tests.unit.gtm.test_journey_signals import _leased_payload
    async def scrape(provider, url, **kwargs):
        return "Alice Design Studio\nFreelance graphic designer\nhello@alicestudio.example\nTestimonials\nJovana Wren, Head of Communications"
    async def model(payload, stage, context):
        if stage in {"marketrole", "passages"}:
            return quality_response(stage, context)
        return {"items": [{"id": 0, "kind": "business", "affiliation": "", "name": "Alice Design Studio", "quote": "Freelance graphic designer",
            "url": context["candidates"][0]["evidence_pages"][0]["url"], "fit": 90, "why": "Design practice fits invoice workflow"}]} if stage == "reachjudge" else {"items": []}
    monkeypatch.setenv("FIRECRAWL_API_KEY", "synthetic")
    monkeypatch.setenv("GTM_ENRICH_ENABLED", "1")
    monkeypatch.setattr(HttpResearchProvider, "scrape", scrape)
    monkeypatch.setattr(journey, "_discovery_model", model)
    journey.update_icp(journey_db["wid"], journey_db["vid"], "UK freelance graphic designers")
    hits = [{"kind": "business", "url": "https://alicestudio.example/", "title": "Alice Design Studio", "snippet": "Freelance graphic designer"}]
    context = {"mode": "B2B", "audience": "UK freelance graphic designers", "problem": "invoices", "offer": "", "competitors": []}
    leads = asyncio.run(reachable.discover(HttpResearchProvider("firecrawl", "synthetic"), hits, [], context, _leased_payload(journey_db)))
    assert len(leads) == 1 and leads[0]["person"] is None
    assert leads[0]["name"] == "Alice Design Studio"


def test_organization_never_inherits_model_person_role(journey_db, monkeypatch):
    from app.gtm import journey, reachable
    from app.signals.research import HttpResearchProvider
    from tests.unit.gtm.test_journey_signals import _leased_payload
    async def model(payload, stage, context):
        if stage in {"marketrole", "passages"}:
            return quality_response(stage, context)
        if stage == "reachjudge":
            row = context["candidates"][0]
            return {"items": [{"id": 0, "kind": "business", "name": "Example Meadows Dental Care", "role": "Office Manager", "company": "Example Meadows Dental Care",
                "url": row["evidence_pages"][0]["url"], "quote": "Example Meadows Dental Care", "affiliation": "", "fit": 90, "why": "Dental practice managing patient bookings."}]}
        return {"items": []}
    async def scrape(*args, **kwargs):
        return '<h1>Example Meadows Dental Care</h1><p>Office Manager handles appointments.</p><a href="mailto:office@meadows.example">Contact</a>'
    monkeypatch.setattr(journey, "_discovery_model", model)
    monkeypatch.setattr(HttpResearchProvider, "scrape", scrape)
    monkeypatch.setenv("GTM_ENRICH_ENABLED", "1")
    context = {"mode": "B2B", "audience": "Independent dental practices", "problem": "patient no-shows", "competitors": [], "offer": ""}
    hits = [{"kind": "business", "url": "https://meadows.example/", "title": "Example Meadows Dental Care", "snippet": "Independent dental practice"}]
    journey.update_icp(journey_db["wid"], journey_db["vid"], context["audience"])
    monkeypatch.setenv("FIRECRAWL_API_KEY", "synthetic")
    leads = asyncio.run(reachable.discover(HttpResearchProvider("firecrawl", "synthetic"), hits, [], context, _leased_payload(journey_db)))
    assert len(leads) == 1 and leads[0]["role"] == "" and leads[0]["person"] is None and leads[0]["person_role"] is None


@pytest.mark.parametrize("identity", ["owner", "unrelated", "testimonial"])
def test_people_portfolios_attach_only_verified_published_contacts_with_six_person_cap(journey_db, monkeypatch, identity):
    from app.gtm import journey, reachable
    from app.signals.research import HttpResearchProvider
    from tests.unit.gtm.test_journey_signals import _leased_payload
    names = ["Alice Jones", "Bob Smith", "Clara Green", "David White", "Emma Brown", "Frank Grey", "Grace Black"]
    searches, pages = [], []
    async def model(payload, stage, context):
        if stage in {"marketrole", "passages"}:
            return quality_response(stage, context)
        if stage == "reachjudge":
            return {"items": [{"id": row["id"], "kind": "person", "name": row["title"], "role": "Freelance designer", "company": "", "affiliation": "", "fit": 85,
                "why": "Runs a freelance design business.", "value_exchange": "Freelance designer serving clients; decides their own invoice follow-up workflow.",
                "url": row["url"], "quote": row["evidence_pages"][0]["text"]} for row in context["candidates"]]}
        return {"items": []}
    async def search(provider, query, **kwargs):
        name = next(name for name in names if name in query)
        searches.append((query, kwargs))
        assert not kwargs.get("include_content") and "linkedin.com" in kwargs["exclude_domains"]
        return [{"url": "https://portfolio" + str(names.index(name)) + ".example/", "title": name + " freelance design portfolio", "snippet": name + " contact"}]
    async def scrape(provider, url, **kwargs):
        assert not any(social in url for social in ("linkedin.com", "x.com", "twitter.com"))
        pages.append(url)
        index = int(url.split("portfolio")[1].split(".")[0])
        name = names[index] if identity == "owner" else "Unrelated Person"
        testimonial = f'<blockquote>{names[index]} says great work!</blockquote>' if identity == "testimonial" else ""
        return f'<h1>{name}</h1><p>Freelance designer portfolio</p>{testimonial}<a href="mailto:hello@portfolio{index}.example">Contact</a><form><input type="email"><textarea></textarea></form>'
    monkeypatch.setattr(journey, "_discovery_model", model)
    monkeypatch.setattr(HttpResearchProvider, "search", search)
    monkeypatch.setattr(HttpResearchProvider, "scrape", scrape)
    monkeypatch.setenv("GTM_ENRICH_ENABLED", "1")
    hits = [{"kind": "person", "url": "https://www.linkedin.com/in/" + name.split()[0].lower(), "title": name, "snippet": name + " Freelance designer serving UK clients"} for name in names]
    context = {"mode": "B2B", "audience": "UK freelance designers", "problem": "invoice follow-ups", "competitors": [], "offer": ""}
    journey.update_icp(journey_db["wid"], journey_db["vid"], context["audience"])
    monkeypatch.setenv("FIRECRAWL_API_KEY", "synthetic")
    payload = _leased_payload(journey_db)
    leads = asyncio.run(reachable.discover(HttpResearchProvider("firecrawl", "synthetic"), hits, [], context, payload))
    assert len(leads) == 7 and len(searches) == 6 and len(pages) <= 18
    emails = [route for lead in leads for route in lead["routes"] if route["kind"] == "email"]
    assert len(emails) == (6 if identity == "owner" else 0)
    for route in emails:
        assert route["email"] in route["quote"] and route["source_url"] in pages
    if identity == "owner":
        assert sum(any(route["kind"] == "contact_form" for route in lead["routes"]) for lead in leads) == 6
    assert journey._journal(payload).document(payload["journey_id"])["_runtime"]["calls_used"]["person_site_search"] == 6

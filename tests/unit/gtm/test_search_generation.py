"""Search again isolates new calls without rewriting unsettled receipts."""
import asyncio
import json
import os
import traceback
from copy import deepcopy
from types import SimpleNamespace

import pytest

from tests.idea_fixtures import install_provider
from tests.unit.gtm.test_journey import journey_db as journey_db, _work, _expire_search_replay
from tests.integration.test_postgres_auth_security import pg_database as pg_database


@pytest.fixture()
def people_search_db(request, tmp_path, monkeypatch):
    if os.environ.get("TEST_POSTGRES_URL"):
        from tests.integration.test_postgres_journey_jobs import journey_db as pg_journey_db
        fields = request.node.callspec.params
        seed = {"audience": fields["audience"], "problem": fields["problem"],
            "offer": "A research interview tool", "customer_type": "B2C" if fields["consumer"] else "B2B"}
        for data in pg_journey_db.__wrapped__(request.getfixturevalue("pg_database"), tmp_path, monkeypatch, SimpleNamespace(param=seed)):
            from app.tenancy import bind
            with bind(workspace_id=data["wid"], venture_id=data["vid"], actor=data["actor"]):
                yield data
    else:
        yield request.getfixturevalue("journey_db")


def _completion(answer):
    return SimpleNamespace(model="DeepSeek-V4-Pro",
        choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(answer)))],
        usage=SimpleNamespace(prompt_tokens=300, completion_tokens=100, total_tokens=400))


@pytest.mark.parametrize("audience", ["commercial greenhouse operators", "amateur ceramic painters"])
@pytest.mark.parametrize("reference,output", [
    ("idea", "save their contacts with public source URLs"),
    ("project", "export the list"),
])
def test_saved_audience_requests_do_not_search_for_output_instructions(journey_db, monkeypatch, audience, reference, output):
    from app.gtm import journey
    from app.gtm.discovery import Plan, request_audience

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    request = f"Find three potential customers for my current {reference} and {output}"
    assert request_audience(request) == ""
    assert journey.search_audience(audience, request) == audience
    proposed = {"target_type": "person", "audience": audience, "requested_count": 3,
        "queries": [{"query": audience + " public profiles", "intent": "person"},
            {"query": audience + " community", "intent": "community"}]}
    context = {"audience": audience, "founder_note": request}
    plan = Plan.model_validate(proposed, context=context)
    assert plan.target_type is None and plan.requested_count == 3 and plan.audience == audience
    assert Plan.model_validate(proposed, context=context | {"target_type": "person"}).target_type == "person"
    named = context | {"founder_note": f"Find three {audience} for my current {reference}"}
    assert Plan.model_validate(proposed, context=named).target_type == "person"
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    assert Plan.model_validate(proposed, context=context).target_type == "person"


@pytest.mark.parametrize("audience,problem,role,place,mode", [
    ("warehouse operators", "last minute unloading conflicts", "Warehouse logistics consultant", "Porto", "B2B"),
    ("adult watercolor painters", "practice stops when work runs late", "Art instructor", "Oslo", "B2C"),
    ("adult trail runners", "training clashes with rotating shifts", "Running coach", "Dublin", "B2C"),
])
def test_expert_queries_find_the_profession_not_the_consumers_personal_situation(journey_db, monkeypatch, audience, problem, role, place, mode):
    from app.gtm import journey
    from app.gtm.discovery import Plan
    from tests.company_os_harness import configure_local_gtm
    from tests.unit.gtm.test_leads_to_drafts import _leased_payload

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    configure_local_gtm(monkeypatch)
    proposal = {"queries": [{"query": f"{role} {problem} {place} contact", "intent": "person", "expert_role": role},
        {"query": f"{audience} community {place}", "intent": "community"}]}
    context = {"audience": audience, "saved_audience": audience, "problem": problem, "location": place, "mode": mode}
    plan = Plan.model_validate(proposal, context=context)
    assert plan.queries[0].expert_role == role and place in plan.queries[0].query
    if mode == "B2C":
        assert plan.queries[0].query == f'{role} {place} contact'
        assert problem not in plan.queries[0].query
        assert all(item["query"].startswith('"' + item["expert_role"] + '"')
            and item["query"].endswith("contact -jobs -directory") for item in journey._expert_queries(audience, problem))
    else:
        assert role in plan.queries[0].query and audience in plan.queries[0].query
    assert audience in plan.queries[1].query
    explicit = Plan.model_validate(proposal, context=context | {"target_type": "person", "requested_role": role})
    assert role in explicit.queries[0].query and audience in explicit.queries[0].query
    contexts = []

    async def capture(payload, stage, targeting):
        assert stage == "plan"
        contexts.append(targeting)
        raise RuntimeError("plan_context_captured")

    monkeypatch.setattr(journey, "_discovery_model", capture)
    data = journey_db
    journey.update_icp(data["wid"], data["vid"], audience, problem=problem, recruitment_opt_in=True)
    payload = _leased_payload(data)
    journal = journey._journal(payload)
    icp = journal.document(payload["journey_id"])["icp"]
    defaults = journey._search_queries(audience, problem, [], context=icp)
    edited = [f"{role} {place} published website"]
    for hints, expected in ((defaults, []), (edited, edited)):
        journal.mutate(payload["journey_id"], payload, lambda row, doc, session: doc.update(discovery_plan=None))
        with pytest.raises(RuntimeError, match="plan_context_captured"):
            asyncio.run(journey._discover(audience, hints, problem=problem,
                note="Find potential customers for my saved idea", payload=payload))
        assert contexts[-1]["founder_query_hints"] == expected


@pytest.mark.parametrize("audience,role,industry,abbreviation,domain,consumer", [
    ("Horticulture scheduling coordinators at commercial greenhouse growers", "Horticulture scheduling coordinator",
     "commercial greenhouse growers", "HSC", "glassgarden.example", False),
    ("Pottery class coordinators at amateur ceramic painting circles", "Pottery class coordinator",
     "amateur ceramic painting circles", "PCC", "clayweekend.example", True),
])
def test_people_queries_and_results_require_saved_customer_industry(journey_db, audience, role, industry, abbreviation, domain, consumer):
    from app.gtm.discovery import Plan
    from app.gtm.reachable import candidate_hits

    context = {"target_type": "person", "mode": "B2C" if consumer else "B2B", "audience": audience,
        "saved_audience": audience + f" ({abbreviation})", "requested_role": role, "industry": industry,
        "problem": "Arrange interviews", "offer": "", "competitors": []}
    plan = Plan.model_validate({"requested_role": role, "organisation_audience": industry,
        "queries": [{"query": f"{abbreviation} leadership", "intent": "person"},
            {"query": f"{abbreviation} public profiles", "intent": "person"},
            {"query": f"{role} leadership", "intent": "person"}]}, context=context | {"requested_role": "", "industry": ""})
    assert all(industry in query.query for query in plan.queries)
    hits = [{"url": f"https://{domain}/team", "title": "Jordan Ellis", "snippet": f"Jordan Ellis, {role}, at {industry}.", "kind": "person"},
        {"url": "https://trailcycling.example/team", "title": f"{abbreviation} cycling instructor", "snippet": "Jordan Ellis leads cycling trips.", "kind": "person"},
        {"url": "https://civicarchives.example/leadership", "title": f"{abbreviation} scheduling coordinator", "snippet": "Jordan Ellis maintains municipal archives.", "kind": "person"},
        {"url": "https://shortlabels.example/team", "title": abbreviation.lower(), "snippet": abbreviation.lower(), "kind": "person"}]
    assert [hit["url"] for hit in candidate_hits(hits, context)] == [hits[0]["url"]]


@pytest.mark.parametrize("audience,domain,consumer", [
    ("Kiln technicians at ceramics manufacturers", "ridgeglass.example", False),
    ("Weekend stargazers in astronomy clubs", "sproutwalk.example", True),
])
def test_people_shortfall_copy_gives_a_next_step_without_organisations(journey_db, audience, domain, consumer):
    from app.gtm.discovery import people_search_message

    target = {"target_type": "person", "audience": audience, "requested_count": 5,
        "mode": "B2C" if consumer else "B2B"}
    organisations = [{"name": audience, "url": f"https://{domain}/"}]
    for count in (0, 2):
        message = people_search_message(count, target, [])
        assert f"You found {count} of 5 people" in message
        assert "Try a broader role or place" in message
        assert "Nothing is sent." in message
        suggested = people_search_message(count, target, organisations)
        assert f"You found {count} of 5 people" in suggested
        assert organisations[0]["name"] in suggested and organisations[0]["url"] in suggested
        assert "Ask to look for people" in suggested


@pytest.mark.parametrize("audience,domain,consumer", [
    ("Kiln technicians at ceramics manufacturers", "ridgeglass.example", False),
    ("Weekend stargazers in astronomy clubs", "sproutwalk.example", True),
])
def test_people_search_failure_reports_results_only_after_search_stops(journey_db, audience, domain, consumer):
    from app.chat.customers import _search_failure

    view = {"search_target": {"target_type": "person", "audience": audience, "requested_count": 5,
        "mode": "B2C" if consumer else "B2B"},
        "organisations_found": [{"name": audience, "url": f"https://{domain}/"}]}
    for count in (0, 2):
        view["reachable_leads"] = [{"id": "public-person"}] if count else []
        view["industry_experts"] = [{"id": "public-expert"}] if count else []
        for state in ("idle", "queued", "running"):
            reply = _search_failure(view | {"state": state}, "public_sources_unavailable")
            assert "You found" not in reply["body_md"]
            assert reply["actions"]
        for state in ("ready", "needs_input", "failed"):
            reply = _search_failure(view | {"state": state}, "public_sources_unavailable")
            assert f"You found {count} of 5 people" in reply["body_md"]
            assert f"https://{domain}/" in reply["body_md"]
            assert reply["actions"]


@pytest.mark.parametrize("audience,domain,consumer", [
    ("Kiln technicians at ceramics manufacturers", "ridgeglass.example", False),
    ("Weekend stargazers in astronomy clubs", "sproutwalk.example", True),
])
def test_people_search_outage_keeps_counts_and_organisations_without_generic_broadening(journey_db, audience, domain, consumer):
    from app.chat.customers import _search_failure

    view = {"state": "needs_input", "search_target": {"target_type": "person", "audience": audience,
        "requested_count": 5, "mode": "B2C" if consumer else "B2B"}}
    for organisations in ([], [{"name": audience, "url": f"https://{domain}/"}]):
        view["organisations_found"] = organisations
        for code in ("public_search_unavailable", "public_sources_unavailable"):
            reply = _search_failure(view, code)
            assert "You found 0 of 5 people" in reply["body_md"]
            assert "Try a broader" not in reply["body_md"]
            assert reply["actions"]
            if organisations:
                assert f"https://{domain}/" in reply["body_md"]


@pytest.mark.parametrize("audience,role,organisation,domain,consumer", [
    ("Process safety engineers at regional water-treatment operators", "Process safety engineer", "Regional water-treatment operators", "lumenworks.example", False),
    ("Adult literacy tutors at independent reading clubs", "Adult literacy tutor", "Independent reading clubs", "hearthreading.example", True),
])
def test_people_plan_searches_organisations_before_named_sources(journey_db, monkeypatch, audience, role, organisation, domain, consumer):
    from app.core.local_mode import is_local
    from app.gtm import journey
    from app.gtm.discovery import Plan
    from app.signals.research import HttpResearchProvider

    from tests.unit.gtm.test_journey_signals import _leased_payload

    data = journey_db
    problem = "Arrange research conversations"
    journey.update_icp(data["wid"], data["vid"], audience, problem=problem, recruitment_opt_in=True)
    payload = _leased_payload(data)
    journal = journey._journal(payload)
    plan = Plan.model_validate({"target_type": "person", "audience": audience, "requested_count": 5,
        "requested_role": role, "organisation_audience": organisation,
        "queries": [{"query": f"{organisation} official websites", "intent": "business"},
            {"query": f"{role} team leadership", "intent": "person"},
            {"query": f"{role} conference speakers", "intent": "person"},
            {"query": f"{role} press release interview", "intent": "person"}]}).model_dump()
    journal.mutate(payload["journey_id"], payload, lambda row, doc, session: doc.update(
        icp=doc["icp"] | {"mode": "consumer" if consumer else "company"},
        discovery_plan=plan["queries"], search_target={key: value for key, value in plan.items() if key != "queries"},
        force_discovery=True))
    searches, reads = [], []
    monkeypatch.setattr("app.signals.research._self_hosted_firecrawl", lambda: ("https://crawl.example", "offline-token"))
    monkeypatch.setattr("app.lead_library.service.match", lambda *args, **kwargs: {"items": []})

    async def search(provider, query, **options):
        searches.append(query)
        assert options["include_content"] is False
        if query == plan["queries"][0]["query"]:
            return [{"url": f"https://{domain}/", "title": "Cedar Collective", "snippet": organisation}]
        return [{"url": f"https://{domain}/team", "title": "Our team", "snippet": "Meet our team"},
            {"url": f"https://{domain}/careers", "title": "Careers", "snippet": "Open roles"}]

    async def scrape(provider, url, **options):
        reads.append(url)
        return "Cedar Collective. Meet our team. No individual is named here."

    async def model(payload, stage, context):
        if stage == "reformulate":
            assert context["organisations"][0]["url"] == f"https://{domain}/"
            return {"queries": [{"query": f"site:{domain} leadership", "intent": "person"},
                {"query": f"site:{domain} interviews", "intent": "person"}]}
        return {"items": [{"id": candidate["id"], "reject_reason": "No published individual"}
            for candidate in context["candidates"]]}

    monkeypatch.setattr(HttpResearchProvider, "search", search)
    monkeypatch.setattr(HttpResearchProvider, "scrape", scrape)
    monkeypatch.setattr(journey, "_discovery_model", model)
    before = deepcopy(journal.document(payload["journey_id"])["_runtime"]["ceilings"])
    asyncio.run(journey._discover(audience, problem=problem, payload=payload))
    saved = journal.document(payload["journey_id"])
    assert searches[0] == plan["queries"][0]["query"]
    assert any(f"site:{domain}" in query and "team" in query for query in searches[1:])
    if is_local():
        assert any(role.lower() in query.lower() for query in searches[1:])
    else:
        assert any("speaker" in query for query in searches)
        assert any("interview" in query or "press" in query for query in searches)
    expected_reads = [f"https://{domain}/team"] + ([f"https://{domain}/"] if is_local() else [])
    assert reads == expected_reads
    assert saved["reachable_leads"] == []
    assert saved["organisations_found"] == [{"name": "Cedar Collective", "url": f"https://{domain}/", "quote": organisation}]
    assert saved["_runtime"]["ceilings"] == before
    assert saved["_runtime"]["calls_used"]["public_page"] <= 45
    assert len(saved["_runtime"]["reachable_candidates_quality_v1"]) <= 80
    from app.gtm.discovery import people_search_message
    message = people_search_message(0, saved["search_target"], saved["organisations_found"])
    assert "0 of 5" in message and domain in message and "you" in message.lower()
    assert "1 of 5" in people_search_message(1, saved["search_target"], saved["organisations_found"])
    from app.chat.customers import _search_failure
    for code in ("no_public_candidates", "public_sources_unavailable"):
        reply = _search_failure({"state": "needs_input", "search_target": saved["search_target"],
            "organisations_found": saved["organisations_found"]}, code)
        assert "0 of 5" in reply["body_md"] and domain in reply["body_md"]
    saved_plan = deepcopy(saved["discovery_plan"])
    asyncio.run(journey._discover(audience, problem=problem, payload=payload))
    assert journal.document(payload["journey_id"])["discovery_plan"] == saved_plan
    assert reads == expected_reads


@pytest.mark.parametrize("domain,consumer", [("lumenworks.example", False), ("hearthreading.example", True)])
def test_people_page_classes_are_skipped_before_fetch(domain, consumer):
    from app.gtm.reachable import candidate_hits

    allowed = [f"https://{domain}/{path}" for path in ("team", "leadership", "news/person-appointed", "interviews/avery", "conference/speakers")]
    blocked = [f"https://{domain}/{path}" for path in ("careers", "en/careers/123", "jobs/engineer", "vacancies", "doi/10.1234/article", "papers/search", "phd/programmes")]
    blocked += [f"https://careers.{domain}/", "https://www.indeed.com/viewjob?jk=123", "https://uk.jooble.org/jobs", "https://www.findaphd.com/phds", "https://www.mdpi.com/123", "https://topic.einnews.com/news"]
    hits = [{"url": url, "title": "Public source", "kind": "person"} for url in allowed + blocked]
    context = {"target_type": "person", "mode": "B2C" if consumer else "B2B", "audience": "Practitioners", "problem": "Research", "offer": "", "competitors": []}
    assert [hit["url"] for hit in candidate_hits(hits, context)] == allowed


@pytest.mark.parametrize("domain,role,senior,consumer", [
    ("lumenworks.example", "Process safety engineer", "Head of Operations", False),
    ("hearthreading.example", "Adult literacy tutor", "Programme Director", True),
])
@pytest.mark.parametrize("evidence", ["exact", "senior", "junior", "invented", "unlinked"])
def test_people_fallback_keeps_only_grounded_senior_roles(journey_db, monkeypatch, domain, role, senior, consumer, evidence):
    from app.gtm import journey, reachable

    from tests.unit.gtm.test_journey_signals import _leased_payload

    data = journey_db
    journey.update_icp(data["wid"], data["vid"], f"{role} at local organisations", problem="Research conversations", recruitment_opt_in=True)
    payload = _leased_payload(data)
    url = f"https://{domain}/team"
    actual_role = role if evidence == "exact" else "Assistant" if evidence == "junior" else senior
    source = f"Avery Morgan, {actual_role} at Cedar Collective. You can ask about their published work."
    if evidence == "unlinked":
        source = f"Avery Morgan attends our event. Rowan Ellis, {senior} at Cedar Collective."
    quote = "Avery Morgan attends our event." if evidence == "unlinked" else source

    async def scrape(page, **options):
        return source

    async def model(payload, stage, context):
        candidate = context["candidates"][0]
        return {"items": [{"id": candidate["id"], "kind": "person", "name": "Invented Person" if evidence == "invented" else "Avery Morgan",
            "role": actual_role, "company": "Cedar Collective", "affiliation": "", "url": url, "quote": quote,
            "fit": 90, "why": "You can learn about their work", "value_exchange": "You can ask about their published experience"}]}

    monkeypatch.setattr(journey, "_discovery_model", model)
    context = {"mode": "B2C" if consumer else "B2B", "target_type": "person", "audience": f"{role} at local organisations",
        "requested_role": role, "organisation_audience": "Local organisations", "requested_count": 5,
        "problem": "Research conversations", "offer": "", "competitors": []}
    leads = asyncio.run(reachable.discover(SimpleNamespace(scrape=scrape),
        [{"url": url, "kind": "person", "title": "Team"}], [], context, payload))
    if evidence in {"junior", "invented", "unlinked"}:
        assert leads == []
    else:
        assert len(leads) == 1 and leads[0]["role"] == actual_role
        assert leads[0]["source_url"] == url and leads[0]["quote"] == source
        assert ("Closest senior role" in leads[0]["why"]) == (evidence == "senior")
        if evidence == "senior":
            alternate = leads[0] | {"id": "alternate", "fit": 80}
            assert reachable.prefer_exact_roles(leads + [alternate]) == leads
            exact = leads[0] | {"id": "exact", "role_match": "exact", "role": role}
            assert reachable.prefer_exact_roles(leads + [exact]) == [exact]
            elsewhere = exact | {"company": "Another Collective"}
            assert reachable.prefer_exact_roles(leads + [elsewhere]) == leads + [elsewhere]


@pytest.mark.parametrize("audience,role,organisation", [
    ("Process safety engineers at regional water-treatment operators", "Process safety engineer", "Regional water-treatment operators"),
    ("Adult literacy tutors at independent reading clubs", "Adult literacy tutor", "Independent reading clubs"),
])
def test_people_generation_prompt_uses_named_source_shapes(journey_db, monkeypatch, audience, role, organisation):
    from app.gtm import journey

    from tests.unit.gtm.test_journey_signals import _leased_payload

    captured = []
    answer = {"target_type": "person", "audience": audience, "requested_count": 5,
        "requested_role": role, "organisation_audience": organisation,
        "queries": [{"query": f"{organisation} official websites", "intent": "business"},
            {"query": f"{role} conference speakers", "intent": "person"}]}

    def synthesize(*args, **kwargs):
        captured.extend(kwargs["messages"])
        return kwargs["parse"](json.dumps(answer))

    monkeypatch.setattr("app.research.metered.synthesize", synthesize)
    journey.update_icp(journey_db["wid"], journey_db["vid"], audience, problem="Research conversations", recruitment_opt_in=True)
    result = asyncio.run(journey._discovery_model(_leased_payload(journey_db), "plan",
        {"target_type": "person", "audience": audience, "founder_note": f"Find 5 {audience}"}))
    instruction = captured[0]["content"]
    assert "organisation_audience" in instruction and "requested_role" in instruction
    assert all(word in instruction.lower() for word in ("leadership", "speaker", "press release", "interview", "public profile"))
    assert "ALL queries must have intent=person" not in instruction
    assert result["requested_role"] == role


@pytest.mark.parametrize("audience,problem,consumer", [
    ("Industrial maintenance planners", "Coordinate inspection rounds", False),
    ("Weekend gardeners", "Plan seasonal planting", True),
])
@pytest.mark.parametrize("first_unavailable", [False, True])
@pytest.mark.parametrize("page_budget", [3, 4])
@pytest.mark.parametrize("limit_candidates", [False, True])
@pytest.mark.hosted_only
def test_people_search_uses_remaining_budget_after_thin_or_failed_pages(journey_db, monkeypatch, audience, problem, consumer, first_unavailable, page_budget, limit_candidates):
    import httpx
    from app.db.models import CreditReservation
    from app.gtm import journey
    from app.signals.research import HttpResearchProvider
    from sqlalchemy import select

    from tests.unit.gtm.test_journey_signals import _leased_payload

    data = journey_db
    if limit_candidates:
        monkeypatch.setattr(journey, "MAX_CANDIDATES", 1)
    monkeypatch.setattr("app.signals.research._self_hosted_firecrawl", lambda: ("https://crawl.example", "offline-token"))
    monkeypatch.setattr("app.lead_library.service.match", lambda *args, **kwargs: {"items": []})
    journey.update_icp(data["wid"], data["vid"], audience, problem=problem, recruitment_opt_in=True)
    payload = _leased_payload(data)
    journal = journey._journal(payload)
    initial = [f"{audience} biography", f"{audience} team"]
    further = [f"{audience} speakers", f"{audience} practitioners"]
    journal.mutate(payload["journey_id"], payload, lambda row, doc, session: doc.update(
        icp=doc["icp"] | {"mode": "consumer" if consumer else "company"},
        discovery_plan=[{"query": query, "intent": "person"} for query in initial],
        search_target={"target_type": "person", "audience": audience, "requested_count": 2}, force_discovery=True))
    journal.mutate(payload["journey_id"], payload, lambda row, doc, session:
        doc["_runtime"]["ceilings"].update(public_page=page_budget))
    searches, inspected, batches = [], [], []
    names = ["Morgan Vale", "Skyler Reed", "Jordan Finch", "Taylor Moss"]
    role = "Hobby gardener" if consumer else "Maintenance planner"

    async def search(provider, query, **kwargs):
        searches.append(query)
        return [{"url": f"https://biography.example/person/{len(searches)}", "title": audience, "snippet": audience}]

    async def model(payload, stage, context):
        if stage == "reformulate":
            return {"queries": [{"query": query, "intent": "person"} for query in further]}
        assert stage == "reachjudge"
        batches.append(context["_batch"])
        prefix = journey._op_prefix(journal.document(payload["journey_id"])["_runtime"])
        journal.mutate(payload["journey_id"], payload, lambda row, doc, session: doc["_runtime"]["calls_used"].update(
            {prefix + "-reachjudge": doc["_runtime"]["calls_used"].get(prefix + "-reachjudge", 0) + 1}))
        return {"items": [{"id": candidate["id"], "kind": "person", "name": candidate["text"].split(",")[0],
            "role": role, "affiliation": "", "url": candidate["url"], "quote": candidate["evidence_pages"][0]["text"], "fit": 91,
            "why": "Your audience's published practitioner", "value_exchange": "You can learn about their published work"}
            for candidate in context["candidates"]]}

    async def scrape(provider, url, **kwargs):
        inspected.append(url)
        index = int(url.rsplit("/", 1)[-1])
        if index <= 2 and (first_unavailable or index == 2):
            response = httpx.Response(500, request=httpx.Request("POST", "https://crawl.example/v2/scrape"),
                extensions={"firecrawl_self_hosted": True})
            response.raise_for_status()
        return f"{names[index - 1]}, {role}. {audience}. You can contact this person on their published page."

    monkeypatch.setattr(HttpResearchProvider, "search", search)
    monkeypatch.setattr(HttpResearchProvider, "scrape", scrape)
    monkeypatch.setattr(journey, "_discovery_model", model)
    before = deepcopy(journal.document(payload["journey_id"])["_runtime"]["ceilings"])
    asyncio.run(journey._discover(audience, problem=problem, payload=payload))
    doc = journal.document(payload["journey_id"])
    expected = 1 if limit_candidates else min(2, page_budget - 2) if first_unavailable else 2
    extra_queries = expected if first_unavailable else 0 if limit_candidates else 1
    assert len(doc["reachable_leads"]) == expected
    assert searches == initial + further[:extra_queries]
    assert doc["_runtime"]["ceilings"] == before
    assert doc["_runtime"]["calls_used"].get("broaden_search", 0) == extra_queries
    assert len(inspected) == (1 if limit_candidates else 2) + extra_queries
    assert len(inspected) <= page_budget
    assert batches == list(range(expected))
    with data["factory"]() as session:
        holds = list(session.scalars(select(CreditReservation)))
        assert holds and all(hold.status in {"settled", "released"} for hold in holds)


@pytest.mark.parametrize("audience", ["Industrial maintenance planners", "Home gardening tutors"])
def test_started_search_updates_saved_state_before_progress_is_read(journey_db, audience):
    from app.gtm import journey
    from app.gtm.progress import compute_progress

    from tests.unit.gtm.test_journey_signals import _leased_payload

    data = journey_db
    journey.update_icp(data["wid"], data["vid"], audience)
    payload = _leased_payload(data)
    journey._progress(payload, "searching", ["public-search"])
    saved = journey._journal(payload).document(payload["journey_id"])
    progress = compute_progress(saved)
    assert progress["state"] == "running"
    from app.core.local_mode import is_local
    assert progress["step_label"] == ("Searching..." if is_local() else "Searching public sources")
    assert "has not started" not in progress["step_label"]


@pytest.mark.parametrize("audience,problem,requested,consumer,all_blocked", [
    ("shipping coordinators at freight firms in Canada", "Dispatch handovers lose delivery updates", 4, False, False),
    ("adult swimming coaches in Ireland", "Adults struggle to choose suitable swimming lessons", 1, True, False),
    ("shipping coordinators at freight firms in Canada", "Dispatch handovers lose delivery updates", 4, False, True),
    ("adult swimming coaches in Ireland", "Adults struggle to choose suitable swimming lessons", 1, True, True),
])
def test_people_request_preserves_role_count_and_public_people(people_search_db, monkeypatch, audience, problem, requested, consumer, all_blocked):
    from app.chat import customers
    from app.db.models import CreditAccount, GtmTouch, Venture
    from app.credits.accounts import ensure_account
    from app.credits.migration import migrate_wallets
    from app.credits.policy import wallet_phase
    from app.chat.execute import start_requested_work
    from app.ventures import startup_review
    from app.gtm import journey, playbooks, reachable
    from app.signals.research import HttpResearchProvider
    from sqlalchemy import select

    data = people_search_db
    note = f"Find me {requested} {audience} to talk to."
    role = "Swimming coach" if consumer else "Shipping coordinator"
    place = "Ireland" if consumer else "Canada"
    practice = "They teach swimming lessons for adults." if consumer else "They coordinate freight dispatch handovers."
    pages = {f"https://practitioners.example/people/{slug}": f"{name}, {role}, at Harbour Collective in {place}. {practice}"
        for slug, name in (("avery-morgan", "Avery Morgan"), ("rowan-ellis", "Rowan Ellis"))}
    pages["https://practitioners.example/contact"] = f"Harbour Collective in {place}. Contact our team."
    blocked_page = "https://practitioners.example/people/blocked-page"
    pages[blocked_page] = f"A {role.lower()} at our organisation in {place}. {practice}"
    queries, reads, library_calls, stages, events = [], [], [], [], []
    original_trace = reachable.trace
    def trace(payload, url, stage, reason="", **facts):
        events.append((url, stage, reason))
        original_trace(payload, url, stage, reason, **facts)
    monkeypatch.setattr(reachable, "trace", trace)
    monkeypatch.setenv("FIRECRAWL_API_KEY", "offline-search")
    monkeypatch.setenv("FIRECRAWL_BASE_URL", "http://127.0.0.1:3002")
    monkeypatch.setenv("GTM_ENRICH_ENABLED", "1")
    monkeypatch.setenv("SUPYMEM_OUTBOUND_EXECUTION_ENABLED", "false")
    monkeypatch.setattr("app.signals.research._self_hosted_firecrawl", lambda: True)
    monkeypatch.setattr("app.gtm.email_learning.cached", lambda key: None)
    monkeypatch.setattr("app.gtm.email_learning.cache_save", lambda key, value: None)
    monkeypatch.setattr(journey, "_icp_seed", lambda vid: {"idea": {"audience": audience, "problem": problem,
        "customer_type": "B2C" if consumer else "B2B", "confirmed": True}})
    monkeypatch.setattr("app.research.prior_art.adapters.search_hn", lambda *args, **kwargs: pytest.fail("People search must not call another provider"))
    monkeypatch.setattr(journey.outreach, "_send_one", lambda *args, **kwargs: pytest.fail("Search must never send"))
    monkeypatch.setattr(playbooks, "select", lambda *args, **kwargs: pytest.fail("People research needs no sales playbook call"))

    def library(vid, **options):
        library_calls.append(options)
        return {"total": 1, "items": [{"id": "library-person", "name": "Avery Morgan", "role": role,
            "company": "Harbour Collective", "why": "Relevant practitioner", "channels": ["LinkedIn"]}]}

    async def search(self, query, **options):
        assert "practitioners.example" not in options.get("exclude_domains", [])
        queries.append(query)
        if all_blocked:
            text = f"{audience}. Public staff profiles in {place}. {practice}"
            return [{"url": url, "title": text, "snippet": text} for url in pages]
        return [{"url": url, "title": text, "snippet": text} for url, text in pages.items()]

    async def scrape(self, url, **options):
        reads.append(url)
        if all_blocked or url == blocked_page:
            raise RuntimeError("403 from a public page")
        return pages.get(url, "")

    async def model(payload, stage, context):
        stages.append(stage)
        if stage == "plan":
            assert context["audience"] == audience
            assert context["who"] == audience
            assert context["location"] == context["geography"] == place
            return {"target_type": "person", "audience": audience, "requested_count": requested,
                "queries": [{"query": f'{audience} public biography', "intent": "person"},
                    {"query": f'site:linkedin.com/in {audience}', "intent": "person"}]}
        if stage == "reachjudge":
            return {"items": [{"id": candidate["id"], "kind": "person", "name": candidate["text"].split(",")[0],
                "role": role, "company": "Harbour Collective", "location": place, "affiliation": "",
                "url": candidate["url"], "quote": candidate["evidence_pages"][0]["text"], "fit": 90,
                "why": "Your audience's published practitioner", "value_exchange": "You can learn about their published work"}
                if "/people/" in candidate["url"] else {"id": candidate["id"], "reject_reason": "No named practitioner or role is published"}
                for candidate in context["candidates"]]}
        if stage == "passages":
            return {"items": [{"id": passage["id"], "relevance": 0.9} for passage in context["candidates"]]}
        return {"items": []}

    monkeypatch.setattr("app.lead_library.service.match", library)
    monkeypatch.setattr(HttpResearchProvider, "search", search)
    monkeypatch.setattr(HttpResearchProvider, "scrape", scrape)
    monkeypatch.setattr(journey, "_discovery_model", model)
    from app.core.local_mode import is_local
    if not is_local():
        with data["factory"]() as session:
            migrate_wallets(session)
            assert wallet_phase(session) == "person"
            session.commit()
    account = None if is_local() else ensure_account(data["vid"], workspace_id=data["wid"], owner_person_id=data["actor"])
    with data["factory"]() as session:
        session.get(Venture, data["vid"]).raw_idea = problem
        if account is None:
            assert session.scalar(select(CreditAccount.id)) is None
        else:
            session.get(CreditAccount, account.id).granted_micro = 10_000_000
        startup_review._document(session, data["vid"], "internal_new_user_flow", {"enabled": True})
        session.commit()
    saved_audience = "adults learning to swim in Ireland" if consumer else "freight firms in Canada"
    assert journey.search_audience(saved_audience, f"Find me {requested} customers in {place}") == saved_audience
    journey.update_icp(data["wid"], data["vid"], saved_audience, problem=problem, start=False)
    async def start():
        reply = await customers.find_reply(data["vid"], note)
        return await start_requested_work(data["vid"], reply, data["actor"], actions={"gtm.find"})
    started = asyncio.run(start())
    assert started.get("kind") != "confirmation" and "credit" not in started["body_md"].lower()
    with data["factory"]() as session:
        row = journey._row(session, data["vid"])
        document = journey._json(row.provenance_json)
        document["seen_lead_urls"] = ["https://practitioners.example/people/previous-person"]
        row.provenance_json = json.dumps(document)
        session.commit()
    _work(data, journey.status(data["wid"], data["vid"]))
    view = journey.status(data["wid"], data["vid"])
    assert view["icp"]["who"] == saved_audience
    assert view["drafts"] == []
    leads = view["reachable_leads"] + view["industry_experts"]
    if all_blocked:
        # Every page failing reports the outage with a retry, never a plain "try a broader role" empty result.
        assert view["state"] == "needs_input" and not leads
        assert view["error_code"] == ("public_sources_unavailable" if is_local() else "public_search_unavailable")
        failed = customers.render(data["vid"], view)
        assert f"0 of {requested} people" in failed["body_md"] and "broader role" not in failed["body_md"]
        assert "403" not in failed["body_md"] and failed["actions"]
        return
    assert not view.get("error_code") and blocked_page in reads
    expected = min(2, requested)
    assert len(leads) == expected, (view.get("error_code"), sorted({reason for url, stage, reason in events if stage == "dropped"}))
    assert all(lead["kind"] == "person" and lead["role"] == role and "/people/" in lead["source_url"] for lead in leads)
    assert {playbooks.real_person(lead) for lead in leads} <= {"Avery Morgan", "Rowan Ellis"}
    assert all(reachable.recipient_name(lead) in lead["draft"] and "Collective team" not in lead["draft"] for lead in leads)
    assert view["search_target"]["requested_count"] == requested
    assert journey._consumer(view["icp"]) is consumer
    assert note in view["discovery_note"]
    with data["factory"]() as session:
        document = journey._json(journey._row(session, data["vid"]).provenance_json)
    assert all(item["intent"] == "person" for item in document["discovery_plan"])
    assert queries and all(place.casefold() in query.casefold() for query in queries)
    assert any(role.casefold() in query.casefold() for query in queries)
    assert library_calls and library_calls[0]["icp"]["who"] == view["search_target"]["audience"]
    assert any("Avery Morgan" in query for query in queries)
    assert customers._search_audience(view) == view["search_target"]["audience"]
    assert reads and not any("linkedin.com" in url for url in reads)
    reply = customers.render(data["vid"], view)["body_md"]
    assert f"{expected} of {requested} people" in reply and role in reply and "https://practitioners.example/people/" in reply
    assert "Collective team" not in reply and "credit" not in reply.lower()
    assert "\u2014" not in reply
    assert view["search_timing"]["duration_ms"] >= 0
    assert journey._keep_limit(8, 1) == 1
    assert all("no public sign" not in lead["why"] for lead in leads)
    for saved_drafts in ([], [{"doc_id": "old-company-draft", "state": "draft", "body": "Hi Harbour Collective team"}]):
        failed = customers.render(data["vid"], view | {"state": "needs_input", "reachable_leads": [], "industry_experts": [],
            "drafts": saved_drafts, "prospects": [{"company": "Harbour Collective"}] if saved_drafts else [],
            "playbooks": [{"title": "Sell to companies", "why": "Old search"}],
            "error_code": "public_sources_unavailable", "source_access": "unavailable"})
        assert f"0 of {requested} people" in failed["body_md"] and "could not" in failed["body_md"].lower()
        assert failed["actions"] and "Hi Harbour Collective team" not in failed["body_md"]
        assert "Sell to companies" not in failed["body_md"]
    with data["factory"]() as session:
        assert not session.scalar(select(GtmTouch.id))


@pytest.mark.parametrize("audience,problem,requested,consumer", [
    ("shipping coordinators at freight firms in Canada", "Dispatch handovers lose delivery updates", 10, False),
    ("adult swimming coaches in Ireland", "Adults struggle to choose suitable swimming lessons", 6, True),
])
def test_changed_people_request_starts_new_search_without_rewriting_customer(people_search_db, monkeypatch, audience, problem, requested, consumer):
    from app.chat import customers
    from app.chat.execute import start_requested_work
    from app.credits.migration import migrate_wallets
    from app.credits.policy import wallet_phase
    from app.db.models import GtmTouch, Job
    from app.gtm import journey
    from sqlalchemy import select

    data = people_search_db
    with data["factory"]() as session:
        migrate_wallets(session)
        assert wallet_phase(session) == "person"
        session.commit()
    journey.update_icp(data["wid"], data["vid"], audience, problem=problem, start=False)

    async def empty_search(*args, **kwargs):
        return []

    monkeypatch.setattr(journey, "_discover", empty_search)
    monkeypatch.setattr(journey.outreach, "_send_one", lambda *args, **kwargs: pytest.fail("Search must never send"))
    old_note = f"Find me 20 {audience} to talk to."
    first = journey.prepare(data["wid"], data["vid"], note=old_note, recruitment_opt_in=True)
    _work(data, first)
    saved_customer = deepcopy(journey.status(data["wid"], data["vid"])["icp"])
    note = f"Find me {requested} {audience} to talk to."

    async def start():
        reply = await customers.find_reply(data["vid"], note)
        return await start_requested_work(data["vid"], reply, data["actor"], actions={"gtm.find"})

    reply = asyncio.run(start())
    current = journey.status(data["wid"], data["vid"])
    assert current["job_id"] != first["job_id"]
    assert current["state"] in {"queued", "running"}
    assert current["discovery_note"].endswith(note)
    assert current["icp"] == saved_customer
    assert reply["kind"] != "confirmation" and "credit" not in reply["body_md"].lower()
    with data["factory"]() as session:
        assert session.query(Job).filter(Job.venture_id == data["vid"], Job.kind == "gtm.journey.prepare").count() == 2
        assert not session.scalar(select(GtmTouch.id).where(GtmTouch.venture_id == data["vid"]))


@pytest.mark.parametrize("uncertain", [True, False], ids=["pending", "settled"])
def test_search_again_plan_uses_new_generation_only_with_pending_receipts(journey_db, monkeypatch, uncertain):
    from app.gtm import journey
    from app.gtm.progress import compute_progress
    data = journey_db
    plans = []
    def respond(messages, options):
        if options["purpose"] != "gtm.discovery.plan":
            return _completion({"items": []})  # passage scoring etc. are separate operations
        if "attempted_queries" not in json.loads(messages[-1]["content"]):
            plans.append(options)
            if uncertain and len(plans) == 1:
                raise TimeoutError()
        return _completion({"queries": [{"query": "bakery phone orders forum", "intent": "customer_voice"},
            {"query": "bakery orders complaints", "intent": "prior_complaints"}]})
    calls = install_provider(monkeypatch, respond)
    _work(data, journey.update_icp(data["wid"], data["vid"], "Independent bakery owners"))
    with data["factory"]() as session:
        runtime = journey._json(journey._row(session, data["vid"]).provenance_json)["_runtime"]
    original = deepcopy(runtime["operations"]["discovery-plan"])
    assert original["state"] == ("uncertain" if uncertain else "completed")
    assert runtime["generation"] == 0 and len(plans) == 1
    for search in (1, 2):
        _expire_search_replay(data, journey.status(data["wid"], data["vid"])["job_id"])
        view = journey.prepare(data["wid"], data["vid"], retry=True)
        generation = search if uncertain else 0
        key = f"discovery@{generation}-plan" if generation else "discovery-plan"
        with data["factory"]() as session:
            queued = journey._json(journey._row(session, data["vid"]).provenance_json)["_runtime"]
        assert queued["generation"] == generation
        assert queued["progress_plan"] == {"planning": [key]}
        assert compute_progress({"_runtime": queued, "state": "queued"})["steps"]["planning"]["completed"] == 0
        _work(data, view)
        with data["factory"]() as session:
            document = journey._json(journey._row(session, data["vid"]).provenance_json)
        runtime = document["_runtime"]
        assert len(plans) == search + 1
        assert runtime["operations"][key]["state"] == "completed"
        assert runtime["operations"][key]["usage"]["settled"]
        if uncertain:
            assert runtime["operations"]["discovery-plan"] == original
            assert "tailored search plan could not be confirmed" not in document.get("discovery_notice", "")
        else:
            assert all(not name.startswith("discovery@") for name in runtime["operations"])


def test_search_again_reachdraft_personalises_without_redialling_uncertain_receipt(journey_db, monkeypatch):
    from app.gtm import journey, reachable
    data = journey_db
    original_model = journey._discovery_model
    async def model(payload, stage, context):
        if stage != "reachjudge":
            return await original_model(payload, stage, context)
        return {"items": [{"id": candidate["id"], "kind": "business", "quote": "Physiotherapy clinic in Bangalore",
            "affiliation": "", "name": "Sample Physical Therapy", "url": candidate["url"], "fit": 90,
            "why": "Published physiotherapy clinic managing appointments."} for candidate in context["candidates"]]}
    monkeypatch.setattr(journey, "_discovery_model", model)
    def respond(messages, options):
        if options["purpose"] == "venture.chat.assistant":  # playbook pick; stage fallback covers it
            raise TimeoutError()
        assert options["purpose"] == "gtm.discovery.draft"
        if len(drafts()) == 1:
            raise TimeoutError()
        candidates = json.loads(messages[-1]["content"])["candidates"]
        return _completion({"items": [{"id": candidate["id"],
            "body": "Hi Sample Physical Therapy team, your published clinic work in Bangalore caught my attention. How do you handle appointment follow-ups? Open to a 15-minute call? Reply with a time.",
            "connection_note": "Hi Sample Physical Therapy team, could we discuss appointment follow-ups? Reply with a time."} for candidate in candidates]})
    calls = install_provider(monkeypatch, respond)
    drafts = lambda: [call for call in calls if call[1]["purpose"] == "gtm.discovery.draft"]
    async def discover(who, search_queries=None, **kwargs):
        payload = kwargs["payload"]
        journal = journey._journal(payload)
        generation = journal.document(payload["journey_id"])["_runtime"]["generation"]
        url = f"https://quantum{generation}.example/"
        candidate = {"id": 0, "kind": "business", "url": url, "title": "Sample Physical Therapy",
            "text": "Sample Physical Therapy\nPhysiotherapy clinic in Bangalore", "quality_selected": True,
            "pages": [{"url": url, "text": "Physiotherapy clinic in Bangalore", "relevance": 0.9}], "people": [], "country": "IN",
            "routes": [{"kind": "email", "email": f"hello@quantum{generation}.example", "source_url": url, "quote": f"hello@quantum{generation}.example"}]}
        journal.mutate(payload["journey_id"], payload, lambda row, doc, session:
            doc["_runtime"].update(reachable_candidates_quality_v1=[candidate]))
        await reachable.discover(None, [], [], {"mode": "B2B", "audience": who, "problem": kwargs["problem"], "competitors": [], "offer": ""}, payload)
        return []
    monkeypatch.setattr(journey, "_discover", discover)
    _work(data, journey.update_icp(data["wid"], data["vid"], "Physiotherapy clinics in Bangalore", problem="A tool for appointment reminders"))
    assert journey.status(data["wid"], data["vid"])["reachable_leads"][0]["draft_source"] == "template"
    with data["factory"]() as session:
        original = deepcopy(journey._json(journey._row(session, data["vid"]).provenance_json)["_runtime"]["operations"]["discovery-reachdraft-0"])
    assert original["state"] == "uncertain" and len(drafts()) == 1
    _expire_search_replay(data, journey.status(data["wid"], data["vid"])["job_id"])
    _work(data, journey.prepare(data["wid"], data["vid"], retry=True))
    view = journey.status(data["wid"], data["vid"])
    assert any(lead["draft_source"] == "model" and lead["draft_status"] == "personalised" for lead in view["reachable_leads"])
    assert len(drafts()) == 2
    with data["factory"]() as session:
        runtime = journey._json(journey._row(session, data["vid"]).provenance_json)["_runtime"]
    assert runtime["operations"]["discovery-reachdraft-0"] == original
    assert runtime["operations"]["discovery@1-reachdraft-0"]["state"] == "completed"
    assert runtime["progress_plan"]["drafting"] == ["discovery@1-reachdraft-0"]


@pytest.mark.parametrize("stage,ceiling", [("judge", 4), ("reachjudge", 5), ("marketrole", 30), ("passages", 60)])
def test_model_keys_and_ceilings_are_generation_scoped(journey_db, monkeypatch, stage, ceiling):
    from app.gtm import journey
    from tests.unit.gtm.test_journey_signals import _leased_payload, quality_response
    data = journey_db
    journey.update_icp(data["wid"], data["vid"], "Independent bakery owners")
    payload = _leased_payload(data)
    journal = journey._journal(payload)
    old_prefix = "discovery-" + stage
    pending = {f"{old_prefix}-{index}": {"state": "uncertain", "attempt": 1} for index in range(ceiling)}
    def exhausted(row, doc, session):
        doc["_runtime"].update(generation=2, operations=pending,
            calls_used={key: 1 for key in pending} | {old_prefix: ceiling})
    journal.mutate(payload["journey_id"], payload, exhausted)
    def respond(messages, options):
        context = json.loads(messages[-1]["content"])
        return _completion(quality_response(stage, context) if stage in {"marketrole", "passages"} else {"items": []})
    calls = install_provider(monkeypatch, respond)
    context = {"candidates": [{"id": 0, "kind": "business", "url": "https://bakery.example/", "text": "Independent bakery handling orders.",
        "evidence_pages": [{"url": "https://bakery.example/", "text": "Independent bakery handling orders."}]}]}
    if stage in {"judge", "reachjudge"}:
        asyncio.run(journey._judge_batch(payload, stage, context | {"_batch": 0}))
        assert len(calls) == 2
        operations = journal.document(payload["journey_id"])["_runtime"]["operations"]
        assert set(operations) - set(pending) == {f"discovery@2-{stage}-0", f"discovery@2-{stage}-0-omitted"}
        assert journal.document(payload["journey_id"])["_runtime"]["calls_used"][f"discovery@2-{stage}"] == 2
    else:
        asyncio.run(journey._discovery_model(payload, stage, context))
        asyncio.run(journey._discovery_model(payload, stage, context))
        operations = journal.document(payload["journey_id"])["_runtime"]["operations"]
        keys = set(operations) - set(pending)
        assert len(calls) == len(keys) == 1 and next(iter(keys)).startswith(f"discovery@2-{stage}-")
    operations = journal.document(payload["journey_id"])["_runtime"]["operations"]
    assert {key: operations[key] for key in pending} == pending


@pytest.mark.parametrize("interruption", ["timeout", "settlement", "missing-attempt"])
@pytest.mark.hosted_only
def test_consumer_search_again_isolates_pending_firecrawl_holds(journey_db, monkeypatch, interruption):
    from app.core.jobs import FatalError
    from app.db.models import CreditAccount, CreditReservation
    from app.credits import gate
    from app.gtm import journey
    from app.research.prior_art import adapters
    from app.signals.research import HttpResearchProvider
    from tests.unit.gtm.test_journey_signals import _leased_payload, fake_discovery_models

    data = journey_db
    fake_discovery_models(monkeypatch)
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fixture")
    searches, keyerrors = [], []
    outage = True
    original_settle = gate._settle_receipt
    def settle(usage, counts):
        if outage and interruption == "settlement":
            raise RuntimeError("Interrupted Firecrawl settlement")
        return original_settle(usage, counts)
    async def search(self, query, **kwargs):
        searches.append(query)
        if outage and interruption != "settlement":
            raise TimeoutError("Firecrawl response lost")
        return [{"url": "https://forum.example/parents", "title": "Parents community forum",
            "content": "Parents community rules: no promotion. Parents gather to discuss baby sleep.", "metadata": {}}]
    async def hn(*args, **kwargs):
        return {"items": []}
    original_warning = journey.log.warning
    def record_failure(message, *args, **kwargs):
        if kwargs.get("extra", {}).get("exception_type") == "KeyError":
            keyerrors.append(traceback.format_exc())
        original_warning(message, *args, **kwargs)
    monkeypatch.setattr(HttpResearchProvider, "search", search)
    monkeypatch.setattr(gate, "_settle_receipt", settle)
    monkeypatch.setattr(adapters, "search_hn", hn)
    monkeypatch.setattr(journey.log, "warning", record_failure)
    _work(data, journey.update_icp(data["wid"], data["vid"], "Parents", problem="baby sleep", recruitment_opt_in=True))
    with data["factory"]() as session:
        row = journey._row(session, data["vid"])
        document = journey._json(row.provenance_json)
        runtime = document["_runtime"]
        pending = {key: deepcopy(operation) for key, operation in runtime["operations"].items() if key.startswith("signal_search-")}
        assert len(pending) == len(searches) == 8 and runtime["generation"] == 0
        assert all(operation["state"] == ("responded" if interruption == "settlement" else "uncertain") for operation in pending.values())
        if interruption == "missing-attempt":
            first = next(iter(pending))
            runtime["operations"][first].update(state="responded", response=[])
            runtime["operations"][first].pop("attempt")
            pending[first] = deepcopy(runtime["operations"][first])
            row.provenance_json = json.dumps(document)
            session.commit()
        held = session.get(CreditAccount, data["account"]).reserved_micro
        assert held > 0
        assert all(session.get(CreditReservation, operation["usage"]["reservation_id"]).status == "reconciling" for operation in pending.values())

    outage = False
    _expire_search_replay(data, journey.status(data["wid"], data["vid"])["job_id"])
    journey.prepare(data["wid"], data["vid"], retry=True)
    payload = _leased_payload(data)
    journal = journey._journal(payload)
    assert journal.document(payload["journey_id"])["_runtime"]["operations"] == pending
    asyncio.run(journey.run_prepare(payload))
    assert not keyerrors, "\n".join(keyerrors)
    assert searches[8:] == searches[:8]
    assert journey.status(data["wid"], data["vid"])["channels"]
    runtime = journal.document(payload["journey_id"])["_runtime"]
    assert {key: runtime["operations"][key] for key in pending} == pending
    assert runtime["calls_used"]["signal_search"] == 8
    fresh = {"discovery@1-" + key: runtime["operations"]["discovery@1-" + key] for key in pending}
    assert all(operation["state"] == "completed" and operation["usage"]["settled"] for operation in fresh.values())
    assert set(fresh) <= set(runtime["progress_plan"]["searching"])
    assert not set(pending) & set(runtime["progress_plan"]["searching"])
    with data["factory"]() as session:
        account = session.get(CreditAccount, data["account"])
        spent = account.spent_micro
        assert spent == sum(operation["usage"]["credits_micro"] for operation in fresh.values()) > 0
        assert account.reserved_micro == held
        assert all(session.get(CreditReservation, operation["usage"]["reservation_id"]).status == "reconciling" for operation in pending.values())
        assert not {operation["usage"]["reservation_id"] for operation in pending.values()} & {operation["usage"]["reservation_id"] for operation in fresh.values()}
    async def forbidden():
        pytest.fail("Same-key replay must not redial")
    async def replay():
        for query in searches[:8]:
            assert await journey._signal_call(payload, "signal_search", query, forbidden, meter="firecrawl_search")
        if interruption == "missing-attempt":
            with pytest.raises(FatalError, match="gtm_outcome_uncertain"):
                journal.recover(payload["journey_id"], first, payload)
    asyncio.run(replay())
    assert journal.document(payload["journey_id"])["_runtime"] == runtime
    with data["factory"]() as session:
        account = session.get(CreditAccount, data["account"])
        assert account.spent_micro == spent and account.reserved_micro == held


def test_all_signal_keys_isolate_older_generations(journey_db):
    from app.gtm import journey
    from tests.unit.gtm.test_journey_signals import _leased_payload
    data = journey_db
    journey.update_icp(data["wid"], data["vid"], "Independent bakery owners")
    payload = _leased_payload(data)
    journal = journey._journal(payload)
    buckets = ["signal_search", "signal_scrape", "public_search", "public_page", "contact:bakery.example", "domain_search", "hn"]
    pending = {bucket + "-" + journey.hashlib.sha256(b"same").hexdigest()[:24]: {"state": "uncertain", "usage": {}} for bucket in buckets}
    journal.mutate(payload["journey_id"], payload, lambda row, doc, session:
        doc["_runtime"].update(generation=2, operations=deepcopy(pending)))
    calls = []
    async def provider():
        calls.append(1)
        return ["fresh"]
    async def run():
        for bucket in buckets:
            assert await journey._signal_call(payload, bucket, "same", provider) == ["fresh"]
            assert await journey._signal_call(payload, bucket, "same", provider) == ["fresh"]
    asyncio.run(run())
    operations = journal.document(payload["journey_id"])["_runtime"]["operations"]
    assert len(calls) == len(buckets)
    assert {key: operations[key] for key in pending} == pending
    assert set(operations) - set(pending) == {"discovery@2-" + key for key in pending}


@pytest.mark.parametrize("audience", ["Independent bakery owners", "Weekend trail walkers"])
def test_incomplete_signal_receipts_remain_unsettled_without_redial(journey_db, audience):
    from app.core.jobs import FatalError
    from app.gtm import journey
    from tests.unit.gtm.test_journey_signals import _leased_payload
    data = journey_db
    journey.update_icp(data["wid"], data["vid"], audience)
    payload = _leased_payload(data)
    journal = journey._journal(payload)
    async def forbidden():
        pytest.fail("An incomplete receipt cannot authorize another call")
    async def run():
        for state in ("calling", "uncertain", "provider_responded", "responded", "release_pending", "completed"):
            for field in ("attempt", "usage", "response"):
                identity = state + ":" + field
                key = "signal_search-" + journey.hashlib.sha256(identity.encode()).hexdigest()[:24]
                operation = {"state": state, "attempt": 1, "usage": {"reservation_id": "existing_hold"}, "response": []}
                operation.pop(field)
                journal.save_operation(payload["journey_id"], key, operation)
                assert await journey._signal_call(payload, "signal_search", identity, forbidden, meter="firecrawl_search") is None
                with pytest.raises(FatalError, match="gtm_outcome_uncertain"):
                    journal.recover(payload["journey_id"], key, payload)
                assert journal.operation(payload["journey_id"], key) == operation
    asyncio.run(run())
    assert journal.document(payload["journey_id"])["_runtime"]["calls_used"] == {}


def test_a_person_found_by_fit_is_shown_as_fit_not_proof_of_pain():
    from app.chat.customers import _lead_block

    lead = {"kind": "person", "name": "Avery Stone", "role": "Head of Fraud Operations", "company": "Harbor Bank",
            "why": "Runs fraud operations at a mid-size bank.", "source_url": "https://bank.example.test/team", "evidence_label": "fit_only"}
    assert "nothing public shows they have this problem" in _lead_block(1, lead)
    assert "Fit only" not in _lead_block(1, {**lead, "evidence_label": "public_signal"})


@pytest.mark.parametrize("audience,problem,consumer,role,place,stated,negated", [
    ("inventory coordinators in Canada", "Duplicate inventory entries", False, "Inventory coordinator", "Canada",
     "I struggle with duplicate inventory entries.", "I do not struggle with duplicate inventory entries."),
    ("adult hikers in Ireland", "Choosing safe hiking routes", True, "Hiker", "Ireland",
     "I need help choosing safe hiking routes.", "I do not need help choosing safe hiking routes."),
])
@pytest.mark.parametrize("evidence", ["stated", "role_only", "negated", "unrelated", "fabricated"])
def test_person_evidence_reports_only_the_grounded_source(people_search_db, monkeypatch, audience, problem,
        consumer, role, place, stated, negated, evidence):
    from app.chat.customers import _lead_block
    from app.credits.migration import migrate_wallets
    from app.credits.policy import wallet_phase
    from app.gtm import journey, reachable
    from tests.unit.gtm.test_journey_signals import _leased_payload

    data = people_search_db
    with data["factory"]() as session:
        migrate_wallets(session)
        assert wallet_phase(session) == "person"
        session.commit()
    journey.update_icp(data["wid"], data["vid"], audience, problem=problem, recruitment_opt_in=consumer)
    payload = _leased_payload(data)
    url = f"https://practitioners.example/people/{data['vid']}/{evidence}/avery-morgan"
    source = (f"Avery Morgan, {role} in {place}. " + {
        "stated": stated, "role_only": "", "negated": negated,
        "unrelated": "I need help organising my book collection.", "fabricated": "",
    }[evidence]).strip()
    if not consumer:
        source += " Public professional contact: avery@practitioners.example."
    invented = "They need your product and have agreed to buy it."
    calls, reads = [], []

    async def scrape(page, **kwargs):
        reads.append(page)
        assert page == url
        return source

    async def model(payload, stage, context):
        calls.append(stage)
        assert stage == "reachjudge"
        candidate = context["candidates"][0]
        return {"items": [{"id": candidate["id"], "kind": "person", "name": "Avery Morgan", "role": role,
            "company": "", "location": place, "affiliation": "", "url": url,
            "quote": invented if evidence == "fabricated" else source, "fit": 90,
            "why": invented, "value_exchange": invented}]}

    monkeypatch.setattr(journey, "_discovery_model", model)
    context = {"mode": "B2C" if consumer else "B2B", "target_type": "person", "audience": audience,
        "problem": problem, "offer": "", "competitors": [], "requested_count": 1}
    leads = asyncio.run(reachable.discover(SimpleNamespace(scrape=scrape),
        [{"kind": "person", "url": url, "title": "Avery Morgan"}], [], context, payload))
    assert calls == ["reachjudge"] and reads == [url]
    if evidence == "fabricated":
        assert leads == []
        return
    assert len(leads) == 1
    lead = leads[0]
    assert lead["evidence_label"] == "Public source"
    assert lead["quote"] == source and lead["source_url"] == url
    assert source in lead["why"]
    assert invented not in lead["why"] + lead["value_exchange"]
    rendered = _lead_block(1, lead, include_notes=False)
    assert "nothing public shows" not in rendered and "no public sign" not in rendered
    assert "You can ask" in rendered
    if not consumer:
        assert reachable.contact_seed(lead)["evidence_label"] == "fit_only"
    assert journey.status(data["wid"], data["vid"])["drafts"] == []


@pytest.mark.parametrize("url,raw,email", [
    ("https://sample-metalworks.test/leadership/jordan-sample", "Jordan Sample\nPlant Manager\nEmail: jordan.sample@sample-metalworks.test", "jordan.sample@sample-metalworks.test"),
    ("https://sample-swim-school.test/team/taylor-sample", "Taylor Sample, head coach. Write to aoife@sample-swim-school.test to talk.", "aoife@sample-swim-school.test"),
])
def test_person_page_keeps_its_published_address_so_the_person_can_be_emailed(url, raw, email):
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    from app.gtm.reachable import contact_seed, person_page_routes, published_route

    routes = person_page_routes(url, raw, raw)
    assert published_route({"routes": routes})["email"] == email
    assert routes[-1] == {"kind": "website", "url": url, "source_url": url, "quote": raw}
    seed = contact_seed({"routes": routes, "judging_status": "judged", "source_url": url, "name": "Named person"})
    assert seed["email"] == email and seed["contact_source_url"] == url
    roster = raw + f"\nSomeone Else\nEmail: someone.else@{email.split('@')[1]}"
    assert published_route({"routes": person_page_routes(url, roster, roster)}) is None
    assert published_route({"routes": person_page_routes(url, "Jordan Sample, Plant Manager", "Jordan Sample, Plant Manager")}) is None


@pytest.mark.parametrize("role", ["Chief Commercial Officer", "Chief Medical Information Officer",
    "Head of Clinical Software Operations", "Head of Customer Experience"])
def test_company_people_recognises_generic_chief_and_head_roles(role):
    from tests.company_os_harness import load_company_os_app
    load_company_os_app()
    from app.gtm import enrich

    for text in (f"Morgan Blake, {role}", f"Morgan Blake\n{role}"):
        people = enrich.extract("employer.example", [("https://employer.example/team", text)])["people"]
        assert len(people) == 1 and people[0]["role"] == role and people[0]["staff_verified"]


@pytest.mark.parametrize("audience,problem,company,domain,role,consumer,pattern,outcome", [
    ("space hardware operators in Canada", "Flight component inspections take too long", "Orbit Components", "orbitcomponents.example",
     "Chief Commercial Officer", False, False, "generated"),
    ("hospital software directors in Canada", "Clinical handovers lose patient updates", "Ward Systems", "wardsystems.example",
     "Head of Clinical Software Operations", False, True, "generated"),
    ("families learning musical instruments in Canada", "Daily practice is hard to maintain", "Practice Club", "practiceclub.example",
     "Head of Customer Experience", True, False, "consumer"),
    ("space hardware operators in Canada", "Flight component inspections take too long", "Orbit Components", "orbitcomponents.example",
     "Chief Commercial Officer", False, False, "unrelated"),
    ("hospital software directors in Canada", "Clinical handovers lose patient updates", "Ward Systems", "wardsystems.example",
     "Head of Clinical Software Operations", False, True, "testimonial"),
    ("families learning musical instruments in Canada", "Daily practice is hard to maintain", "Practice Club", "practiceclub.example",
     "Head of Customer Experience", True, False, "published"),
    ("hospital software directors in Canada", "Clinical handovers lose patient updates", "Ward Systems", "wardsystems.example",
     "Head of Clinical Software Operations", False, True, "employer_search"),
    ("space hardware operators in Germany", "Flight component inspections take too long", "Orbit Components", "orbitcomponents.example",
     "Chief Commercial Officer", False, False, "germany"),
    ("hospital software directors in Austria", "Clinical handovers lose patient updates", "Ward Systems", "wardsystems.example",
     "Head of Clinical Software Operations", False, True, "austria"),
])
def test_people_search_reads_employer_before_return_without_guessing_for_consumers(
        journey_db, monkeypatch, audience, problem, company, domain, role, consumer, pattern, outcome):
    from app.config import settings
    from app.gtm import enrich, journey, reachable
    from app.gtm.relationships.email_compliance import source_line
    from tests.unit.gtm.test_journey_signals import _leased_payload

    data = journey_db
    monkeypatch.setattr(journey, "_icp_seed", lambda vid: {"idea": {"audience": audience, "problem": problem,
        "customer_type": "B2C" if consumer else "B2B", "confirmed": True}})
    journey.update_icp(data["wid"], data["vid"], audience, problem=problem, recruitment_opt_in=consumer)
    payload = _leased_payload(data)
    assert journey._consumer(journey._journal(payload).document(payload["journey_id"])["icp"]) is consumer
    home = f"https://{domain}/"
    source = "https://www.linkedin.com/in/morgan-blake" if outcome == "employer_search" else home + ("team/morgan-blake" if pattern else "about/morgan-blake")
    contact = home + "contact"
    team = home + "team"
    location = {"germany": "Germany", "austria": "Austria"}.get(outcome, "Canada")
    text = f"Morgan Blake, {role}, at {company} in {location}."
    pages = {source: f"<h1>Morgan Blake</h1><p>{text}</p>",
        home: f'<h1>{company}</h1><a href="/contact">Contact</a><a href="/team">Team</a><footer>Copyright 2026 {company}.</footer>',
        contact: '<form><input type="email"><textarea name="message"></textarea></form>',
        team: f"<p>{text}</p><p>Alex Rivers, Chief Medical Information Officer, alex.rivers@{domain}</p>" if pattern else "Our team handles customer enquiries."}
    if outcome == "unrelated":
        pages[home] = pages[home].replace(company, "Other Employer")
        pages[home] = pages[home].split("<footer>")[0]
        pages[home] += f"<h1>People who use {company}</h1>"
        pages[source] += f"<footer>Copyright 2026 {company}.</footer>"
    elif outcome == "testimonial":
        pages[source] = f"<h1>Testimonials</h1><p>{text}</p>"
        pages[team] = "<h1>Testimonials</h1>" + pages[team]
    elif outcome == "published":
        pages[source] += f"<footer>Contact morgan.blake@{domain}</footer>"
    elif outcome in {"germany", "austria"}:
        pages[home] += "<p>USA</p>"
    reads, mx_checks = [], []
    monkeypatch.setenv("GTM_ENRICH_ENABLED", "1")
    monkeypatch.setattr("app.signals.research._self_hosted_firecrawl", lambda: True)
    monkeypatch.setattr(settings, "guessed_emails", "on")
    monkeypatch.setattr("app.gtm.email_learning.cached", lambda key: None)
    monkeypatch.setattr("app.gtm.email_learning.cache_save", lambda *args: None)
    def mx(host):
        assert not consumer and outcome not in {"germany", "austria"}
        mx_checks.append(host)
        return "mx"
    monkeypatch.setattr(enrich, "mx_status", mx)

    class Provider:
        name = "firecrawl"
        async def scrape(self, url, *, main_only=True):
            reads.append((url, main_only))
            raw = pages.get(url, "")
            return raw.split("<footer>")[0] if main_only else raw
        async def search(self, query, **kwargs):
            assert outcome == "employer_search" and "portfolio" not in query
            return [{"url": home, "title": company, "snippet": text}]

    async def judge(payload, stage, context):
        return [{"id": row["id"], "url": row["url"], "kind": "person", "name": "Morgan Blake", "role": role,
            "company": company, "affiliation": company, "location": location, "fit": 90, "quote": text,
            "why": "Works in the requested field", "value_exchange": "You can ask about their day-to-day work"}
            for row in context["candidates"]]
    monkeypatch.setattr(journey, "_judge_batch", judge)
    context = {"target_type": "person", "mode": "B2C" if consumer else "B2B", "audience": audience,
        "problem": problem, "offer": "An interview tool", "competitors": [], "requested_count": 1}
    leads = asyncio.run(reachable.discover(Provider(), [{"url": source, "title": text, "kind": "person"}], [], context, payload))
    assert len(leads) == 1
    assert leads[0]["country"] == {"Germany": "DE", "Austria": "AT", "Canada": "CA"}[location]
    routes = leads[0]["routes"]
    guesses = [route for route in routes if route.get("route_type") in {"guess", "pattern"}]
    from app.core.local_mode import is_local
    if is_local() or outcome not in {"generated", "employer_search"}:
        assert not guesses and not mx_checks
        if is_local() and outcome in {"generated", "employer_search"}:
            assert reachable.published_route(leads[0]) is None
            assert not (reachable.contact_seed(leads[0]) or {}).get("email")
        if outcome == "published":
            route = reachable.published_route(leads[0])
            assert route["email"] == f"morgan.blake@{domain}" and route["confidence"] == 100
            assert reads == [(source, False)]
            return
        if outcome in {"unrelated", "testimonial"}:
            assert "business_url" not in leads[0]
    else:
        assert len(guesses) == 1
        route = guesses[0]
        assert route["email"] == f"morgan.blake@{domain}"
        assert route["confidence"] == (80 if pattern else 40)
        assert route["route_type"] == ("pattern" if pattern else "guess")
        assert route["confidence_label"] == ("Derived from a published company pattern" if pattern else "Guessed business address")
        assert reachable.published_route(leads[0]) is None
        seed = reachable.contact_seed(leads[0])
        assert seed["email_route"] == route and "email_confidence" not in seed
        assert enrich.generated_route_allowed(seed, route["email"], "CA")
        assert not enrich.generated_route_allowed(seed, route["email"], "CA", for_send=True)
        assert source_line(SimpleNamespace(enrichment_json=json.dumps(seed))) == ""
    assert (home, False) in reads and (contact, False) in reads and (team, False) in reads
    if outcome == "employer_search":
        assert len(reads) <= 3 and not any("linkedin.com" in url for url, _ in reads)
    else:
        assert reads[0] == (source, False) and len(reads) - 1 <= 3

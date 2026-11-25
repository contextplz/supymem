import asyncio
import json
import pytest

from tests.unit.gtm.test_journey import journey_db
from tests.unit.gtm.test_journey_signals import _leased_payload
from tests.unit.gtm.test_reviewed_ports import safety_runtime as safety_runtime


@pytest.mark.parametrize("audience", ["Scheduling for dental practices", "Meal planning for home cooks"])
def test_customer_search_wait_explains_backoff_instead_of_worker_availability(monkeypatch, audience):
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from tests.company_os_harness import load_company_os_app
    load_company_os_app()
    from app.core import jobs
    from app.gtm.progress import compute_progress

    clock = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(jobs, "utcnow", lambda: clock)
    data = {"state": "running", "icp": {"who": audience}, "_runtime": {
        "started_at": clock.isoformat(), "progress_percent": 45, "progress_step": "searching"}}
    row = SimpleNamespace(status="retry_wait", cancel_requested=False, last_error="provider_retryable:private",
                          run_after=clock + timedelta(minutes=3), scheduled_for=clock)
    progress = compute_progress(data, row)
    assert progress["percent"] == 45 and progress["started_at"] == clock.isoformat()
    assert progress["state"] == "queued"
    assert progress["step_label"] == jobs.job_wait(row)["label"]
    assert "temporary problem" in progress["step_label"]
    row.status = "scheduled"
    assert "scheduled for later" in compute_progress(data, row)["step_label"]
    row.run_after = clock
    from app.core.local_mode import is_local
    assert ("Your customer search is queued." if is_local() else "has not started yet") in compute_progress(data, row)["step_label"]
    row.status = "leased"
    data["state"] = "queued"
    assert compute_progress(data, row)["step_label"] == ("Searching..." if is_local() else "Searching public sources")
    assert compute_progress(data, row)["state"] == "running"
    assert "worker" not in str(progress) and "private" not in str(progress) and audience not in str(progress)


def test_fit_progress_reserves_later_work_when_new_checks_extend_the_plan():
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    from app.gtm.progress import compute_progress

    for audience in ("Equipment rental dispatchers", "Families planning camping trips"):
        data = {"state": "running", "icp": {"who": audience}, "_runtime": {
            "progress_step": "judging", "progress_percent": 99,
            "progress_plan": {"planning": ["plan"], "searching": ["search"],
                "judging": ["fit_first", "fit_last"]},
            "operations": {"plan": {"state": "completed"}, "search": {"state": "completed"},
                "fit_first": {"state": "completed"}, "fit_last": {"state": "running"}}}}
        progress = compute_progress(data)
        assert progress["step_label"] == "Checking fit"
        assert progress["percent"] <= 75
        assert progress["steps"]["judging"] == {"planned": 2, "completed": 1}
        data["_runtime"]["operations"]["fit_last"]["state"] = "completed"
        assert compute_progress(data)["percent"] <= 75
        assert compute_progress(data | {"state": "ready"})["percent"] == 100


@pytest.mark.parametrize("audience", ["Scheduling for dental practices", "Meal planning for home cooks"])
def test_progress_is_weighted_monotonic_when_discovered_steps_expand(journey_db, audience):
    from app.db.models import Job
    from app.gtm import journey
    from app.gtm.progress import compute_progress
    journey.update_icp(journey_db["wid"], journey_db["vid"], audience)
    payload = _leased_payload(journey_db)
    journal = journey._journal(payload)
    def searched(row, doc, session):
        doc["_runtime"].update(progress_plan={"planning": ["plan"], "searching": ["search"]},
            operations={"plan": {"state": "completed"}, "search": {"state": "completed"}}, progress_step="searching")
    journal.mutate(payload["journey_id"], payload, searched)
    first = compute_progress(journal.document(payload["journey_id"]))
    assert first["percent"] == 55
    journey._progress(payload, "searching", ["extra-search"])
    second = compute_progress(journal.document(payload["journey_id"]))
    assert second["percent"] == first["percent"]
    assert second["steps"]["searching"] == {"planned": 2, "completed": 1}
    def drafted(row, doc, session):
        doc["_runtime"]["progress_percent"] = 99
        doc["_runtime"]["progress_plan"]["drafting"] = ["draft1", "draft2"]
        doc["_runtime"]["progress_step"] = "drafting"
    journal.mutate(payload["journey_id"], payload, drafted)
    data = journal.document(payload["journey_id"])
    assert compute_progress(data)["percent"] == 99
    with journey_db["factory"]() as session:
        expected = compute_progress(data, session.get(Job, payload["_job_id"]))
    assert journey.status(journey_db["wid"], journey_db["vid"])["research_progress"] == expected
    assert compute_progress(data | {"state": "ready"})["percent"] == 100


@pytest.mark.parametrize("audience,problem,query_subject", [
    ("Structural engineering consultancies in India", "Review structural engineering drawings", "engineering consultancies"),
    ("Specialty coffee roasters in Norway", "Track coffee roasting batches", "coffee roasters"),
])
def test_business_plan_repairs_signal_only_plan_and_empty_search_is_actionable(journey_db, monkeypatch, audience, problem, query_subject):
    from app.gtm import journey
    from app.signals.research import HttpResearchProvider
    from app.research.prior_art import adapters

    monkeypatch.setenv("FIRECRAWL_API_KEY", "fixture")
    searches = []
    first_wave = asyncio.Event()
    active = maximum = 0
    async def search(self, query, **kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        searches.append(query)
        if active == journey.enrich.CONCURRENCY:
            asyncio.get_running_loop().call_soon(first_wave.set)
        try:
            await asyncio.wait_for(first_wave.wait(), timeout=5)
        finally:
            active -= 1
        return []
    async def model(payload, stage, context):
        assert stage == "plan"
        return {"queries": [{"query": f"{problem} forum", "intent": "community"},
            {"query": f"{problem} complaints", "intent": "customer_voice"}]}
    async def hn(*args, **kwargs):
        return {"items": []}
    monkeypatch.setattr(HttpResearchProvider, "search", search)
    monkeypatch.setattr(journey, "_discovery_model", model)
    monkeypatch.setattr(adapters, "search_hn", hn)
    journey.update_icp(journey_db["wid"], journey_db["vid"], audience, problem=problem)
    payload = _leased_payload(journey_db)
    asyncio.run(journey.run_prepare(payload))
    view = journey.status(journey_db["wid"], journey_db["vid"])
    saved = journey._journal(payload).document(payload["journey_id"])
    assert [item["intent"] for item in saved["discovery_plan"][:3]] == ["business"] * 3
    assert all(query_subject in item["query"] for item in saved["discovery_plan"][:3])
    assert all("Mumbai" not in query for query in searches)
    from app.core.local_mode import is_local
    assert maximum == journey.enrich.CONCURRENCY
    assert len(searches) == journey.BUSINESS_SEARCH_QUERIES + (0 if is_local() else journey.CALL_CEILINGS["broaden_search"])
    assert view["error_code"] == "no_public_candidates"
    assert "add someone you know" in view["message"]
    if is_local():
        assert view["discovery_notice"] is None
    else:
        assert "public web sources" in view["discovery_notice"]
    assert set(view["research_progress"]["steps"]) == {"planning", "searching", "judging", "contacting", "drafting"}


@pytest.mark.parametrize("audience,problem", [
    ("Structural engineering consultancies in India", "Review structural engineering drawings"),
    ("Home cooks in Canada", "Plan weekday dinners"),
])
@pytest.mark.parametrize("failure,status,reason", [
    ("message", 429, "runtimeerror_429"),
    ("status", 503, "runtimeerror_503"),
    ("status_code", 402, "runtimeerror_402"),
    ("response", 402, "httpstatuserror_402"),
    ("timeout", None, "timeouterror"),
])
def test_failed_search_traces_status_and_warns_without_query(journey_db, monkeypatch, caplog, failure, status, reason, audience, problem):
    import httpx
    from app.gtm import journey
    from app.signals.research import HttpResearchProvider
    from app.research.prior_art import adapters

    monkeypatch.setenv("FIRECRAWL_API_KEY", "private-test-key")
    searches = []
    error = RuntimeError("Firecrawl search failed: 429 Too Many Requests")
    if failure in {"status", "status_code"}:
        setattr(error, failure, status)
    elif failure == "response":
        error = httpx.HTTPStatusError("private-test-key", request=httpx.Request("POST", "https://private.example/search"),
            response=httpx.Response(status))
    elif failure == "timeout":
        error = TimeoutError("private-test-key https://private.example/search")
    async def search(self, query, **kwargs):
        searches.append(query)
        raise error
    async def model(payload, stage, context):
        return {"queries": [{"query": f"{problem} forum", "intent": "community"},
            {"query": f"{problem} complaints", "intent": "customer_voice"}]}
    async def hn(*args, **kwargs):
        return {"items": []}
    monkeypatch.setattr(HttpResearchProvider, "search", search)
    monkeypatch.setattr(journey, "_discovery_model", model)
    monkeypatch.setattr(adapters, "search_hn", hn)
    journey.update_icp(journey_db["wid"], journey_db["vid"], audience, problem=problem)
    payload = _leased_payload(journey_db)
    caplog.set_level("DEBUG", logger=journey.log.name)
    with pytest.raises((RuntimeError, journey.PublicSearchBlocked)):
        asyncio.run(journey._discover(audience, problem=problem, payload=payload))
    saved = journey._journal(payload).document(payload["journey_id"])
    planned = saved["discovery_plan"] + saved.get("expert_discovery_plan", []) + saved.get("broadened_discovery_plan", [])
    assert sorted(searches) == sorted(item["query"] for item in planned)
    assert payload["_trace_counts"][f"seed:search_failed:{reason}"] == len(searches)
    traces = [record.lead_trace for record in caplog.records if record.message == "gtm_lead_trace" and record.lead_trace["stage"] == "search_failed"]
    warnings = [record for record in caplog.records if record.message == "gtm_discovery_search_failed"]
    assert len(traces) == len(warnings) == len(searches)
    for trace, warning in zip(traces, warnings):
        assert trace == {"url": "", "stage": "search_failed", "reason": reason, "intent": warning.intent}
        assert warning.levelname == "WARNING" and warning.exception_type == type(error).__name__
        assert warning.status == status and warning.provider == "firecrawl" and warning.exc_info is None
        assert not any(query in str(warning.__dict__) for query in searches)
        assert "private-test-key" not in str(warning.__dict__) and "private.example" not in str(warning.__dict__)


def test_public_fallback_verifies_company_route_without_paid_search_replay(journey_db, monkeypatch):
    from app.core.local_mode import is_local
    from app.gtm import journey
    from app.signals.research import DuckDuckGoProvider, HttpResearchProvider
    from app.research.prior_art import adapters

    monkeypatch.setenv("FIRECRAWL_API_KEY", "fixture")
    monkeypatch.setenv("GTM_ENRICH_ENABLED", "1")
    counts = {"paid": 0, "public": 0}
    async def search(self, query, **kwargs):
        if is_local():
            return await public(self, query)
        counts["paid"] += 1
        return []
    async def public(self, query):
        counts["public"] += 1
        return [{"url": "https://consultancy.example/", "title": "Design Consultancy", "snippet": "Structural engineering consultancy in Mumbai"}]
    def fetch(url, *, allow_missing=False):
        if url.endswith("/robots.txt"):
            assert allow_missing
            return "User-agent: *\nAllow: /"
        return "Design Consultancy\nStructural engineering consultancy in Mumbai, India.\nContact hello@consultancy.example"
    async def model(payload, stage, context):
        if stage == "plan":
            return {"queries": [{"query": "structural engineer firms", "intent": "business"}, {"query": "engineering complaints", "intent": "customer_voice"}]}
        if stage in {"marketrole", "passages"}:
            from tests.unit.gtm.test_journey_signals import quality_response
            return quality_response(stage, context)
        if stage == "reachjudge":
            return {"items": [{"id": row["id"], "kind": "business", "quote": "Structural engineering consultancy in Mumbai, India.",
                "affiliation": "", "name": "Design Consultancy", "url": row["url"], "fit": 90, "why": "Reviews engineering drawings."} for row in context["candidates"]]}
        return {"items": []}
    async def hn(*args, **kwargs):
        return {"items": []}
    monkeypatch.setattr(HttpResearchProvider, "search", search)
    monkeypatch.setattr(DuckDuckGoProvider, "search", public)
    monkeypatch.setattr("app.gtm.scrapy_leads._fetch", fetch)
    if is_local():
        import socket
        from app.signals.research import ScrapedText
        async def scrape(self, url, **kwargs):
            return ScrapedText(fetch(url), metadata={"sourceURL": url}, usage={})
        monkeypatch.setattr(HttpResearchProvider, "scrape", scrape)
        monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port))])
    monkeypatch.setattr(journey, "_discovery_model", model)
    monkeypatch.setattr(adapters, "search_hn", hn)
    journey.update_icp(journey_db["wid"], journey_db["vid"], "Structural engineering consultancies in India", problem="Review structural drawings")
    payload = _leased_payload(journey_db)
    async def run():
        for _ in range(2):
            await journey._discover("Structural engineering consultancies in India", problem="Review structural drawings", payload=payload)
    asyncio.run(run())
    doc = journey._journal(payload).document(payload["journey_id"])
    if is_local():
        assert counts == {"paid": 0, "public": journey.BUSINESS_SEARCH_QUERIES + journey.CALL_CEILINGS["broaden_search"]}
        assert doc.get("discovery_notice") is None
    else:
        assert counts == {"paid": journey.BUSINESS_SEARCH_QUERIES + journey.CALL_CEILINGS["broaden_search"],
            "public": journey.CALL_CEILINGS["public_search"]}
        assert "public web sources" in doc["discovery_notice"]
    assert doc["reachable_leads"][0]["routes"][0]["email"] == "hello@consultancy.example"


@pytest.mark.parametrize("audience,problem,work", [
    ("Structural engineering firm owners", "Review drawings", "structural engineering drawing review"),
    ("Independent veterinary practice owners", "Schedule appointments", "veterinary appointment scheduling"),
])
def test_first_judged_batch_is_visible_while_later_judge_is_running(journey_db, monkeypatch, audience, problem, work):
    from app.gtm import journey, reachable
    journey.update_icp(journey_db["wid"], journey_db["vid"], audience, problem=problem)
    payload = _leased_payload(journey_db)
    context = {"mode": "B2B", "audience": audience, "problem": problem, "competitors": [], "offer": ""}
    hits = [{"kind": "person", "url": f"https://www.linkedin.com/in/owner{index}", "title": f"Owner {index}",
        "snippet": f"Owner {index} manages {work}."} for index in range(16)]
    async def run():
        later_started, release_later = asyncio.Event(), asyncio.Event()
        async def judge(payload, stage, supplied):
            if supplied["_batch"] == 1:
                later_started.set()
                await release_later.wait()
            else:
                await later_started.wait()
            return [{"id": row["id"], "kind": "person", "quote": row["evidence_pages"][0]["text"], "affiliation": "", "name": row["title"],
                "url": row["url"], "fit": 90, "why": f"Owns {work}.", "value_exchange": f"Decides how to run {work}."}
                for row in supplied["candidates"]]
        async def model(*args):
            if args[1] in {"marketrole", "passages"}:
                from tests.unit.gtm.test_journey_signals import quality_response
                return quality_response(args[1], args[2])
            return {"items": []}
        monkeypatch.setattr(journey, "_judge_batch", judge)
        monkeypatch.setattr(journey, "_discovery_model", model)
        task = asyncio.create_task(reachable.discover(None, hits, [], context, payload))
        await later_started.wait()
        for _ in range(100):
            doc = journey._journal(payload).document(payload["journey_id"])
            if sum(lead.get("judging_status") == "judged" for lead in doc.get("reachable_leads", [])) == 8:
                break
            await asyncio.sleep(0.01)
        assert len(doc["reachable_leads"]) == 16 and not task.done()
        assert sum(lead["judging_status"] == "judged" for lead in doc["reachable_leads"]) == 8
        assert all(lead["draft_source"] == "template" for lead in doc["reachable_leads"])
        release_later.set()
        assert len(await task) == 10
        assert len(journey._journal(payload).document(payload["journey_id"])["reachable_leads"]) == 10
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["uncertain", "timeout"])
def test_unconfirmed_plan_uses_saved_icp_without_replaying_call(journey_db, monkeypatch, failure):
    from app.core.jobs import FatalError
    from app.gtm import journey
    from app.signals.research import HttpResearchProvider
    from app.research.prior_art import adapters
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fixture")
    calls, queries = [], []
    async def model(payload, stage, context):
        calls.append(stage)
        raise FatalError("discovery_outcome_uncertain") if failure == "uncertain" else TimeoutError()
    async def search(self, query, **kwargs):
        queries.append(query)
        return []
    async def hn(*args, **kwargs):
        return {"items": []}
    monkeypatch.setattr(journey, "_discovery_model", model)
    monkeypatch.setattr(HttpResearchProvider, "search", search)
    monkeypatch.setattr(adapters, "search_hn", hn)
    journey.update_icp(journey_db["wid"], journey_db["vid"], "Structural engineering consultancies in India", problem="Review drawings")
    payload = _leased_payload(journey_db)
    async def run():
        for _ in range(2):
            try:
                await journey._discover("Structural engineering consultancies in India", problem="Review drawings", payload=payload)
            except RuntimeError:
                pass
    asyncio.run(run())
    saved = journey._journal(payload).document(payload["journey_id"])
    from app.core.local_mode import is_local
    assert calls == (["plan", "reformulate"] if is_local() else ["plan"])
    assert len(queries) == journey.BUSINESS_SEARCH_QUERIES + (0 if is_local() else journey.CALL_CEILINGS["broaden_search"])
    assert saved["discovery_plan"] and any("practitioner author" in row["query"] for row in saved["discovery_plan"])
    assert "saved audience" in saved["discovery_notice"] and ("public sources" if is_local() else "public web sources") in saved["discovery_notice"]


def test_timed_out_judge_keeps_checked_and_unchecked_call_routes(journey_db, monkeypatch):
    from app.gtm import journey, reachable
    journey.update_icp(journey_db["wid"], journey_db["vid"], "Structural engineering practitioners", problem="Review drawings")
    payload = _leased_payload(journey_db)
    context = {"mode": "B2B", "audience": "Structural engineering practitioners", "problem": "Review drawings", "competitors": [], "offer": ""}
    hits = [{"kind": "person", "url": f"https://www.linkedin.com/in/engineer{index}", "title": f"Engineer {index}",
        "snippet": f"Engineer {index} reviews structural engineering drawings in India."} for index in range(9)]
    async def judge(payload, stage, supplied):
        if supplied["_batch"] == 1:
            raise TimeoutError("uncertain response")
        return [{"id": row["id"], "kind": "person", "quote": row["evidence_pages"][0]["text"], "affiliation": "", "name": row["title"], "url": row["url"],
            "fit": 85, "why": "Published drawing review experience.", "value_exchange": "Can explain drawing review and receive the founder's findings.", "lead_type": "insight"}
            for row in supplied["candidates"]]
    async def model(*args):
        if args[1] in {"marketrole", "passages"}:
            from tests.unit.gtm.test_journey_signals import quality_response
            return quality_response(args[1], args[2])
        return {"items": []}
    monkeypatch.setattr(journey, "_judge_batch", judge)
    monkeypatch.setattr(journey, "_discovery_model", model)
    leads = asyncio.run(reachable.discover(None, hits, [], context, payload))
    assert len(leads) == 9
    assert sum(lead["lead_type"] == "insight" for lead in leads) == 8
    assert leads[-1]["judging_status"] == "pending" and leads[-1]["fit"] == 0
    assert all(lead["source_url"] and lead["quote"] and lead["routes"][0]["source_url"] for lead in leads)
    assert all("15-minute call" in lead["draft"] and "Review drawings" in lead["draft"] for lead in leads)
    assert "could not be checked" in journey._journal(payload).document(payload["journey_id"])["discovery_notice"]


def test_public_company_reads_overlap_and_stop_at_first_call_route(journey_db, monkeypatch):
    from app.gtm import journey, reachable
    monkeypatch.setattr(journey.enrich, "CONCURRENCY", 2)
    journey.update_icp(journey_db["wid"], journey_db["vid"], "Engineering consultancies", problem="Review drawings")
    payload = _leased_payload(journey_db)
    context = {"mode": "B2B", "audience": "Engineering consultancies", "problem": "Review drawings", "competitors": [], "offer": ""}
    hits = [{"kind": "business", "url": f"https://firm{index}.example/", "title": f"Firm {index}", "snippet": "Structural engineering consultancy"} for index in range(5)]
    active = maximum = 0
    pages = []
    async def scrape(self, url, **kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        pages.append(url)
        await asyncio.sleep(0.01)
        active -= 1
        host = url.split("/")[2]
        return f"Firm {host.split('.')[0][4:]}\nStructural engineering consultancy. Contact hello@{host}"
    async def model(payload, stage, supplied):
        if stage in {"marketrole", "passages"}:
            from tests.unit.gtm.test_journey_signals import quality_response
            return quality_response(stage, supplied)
        if stage == "reachjudge":
            return {"items": [{"id": row["id"], "kind": "business", "quote": "Structural engineering consultancy.", "affiliation": "", "name": row["title"],
                "url": row["url"], "fit": 85, "why": "Engineering drawing review experience."} for row in supplied["candidates"]]}
        return {"items": []}
    monkeypatch.setattr(reachable.PublicPages, "scrape", scrape)
    monkeypatch.setattr(journey, "_discovery_model", model)
    leads = asyncio.run(reachable.discover(reachable.PublicPages(), hits, [], context, payload, fallback=True))
    assert maximum == 2 and len(pages) == 5 and len(leads) == 5
    assert all(lead["routes"][0]["kind"] == "email" for lead in leads)
    runtime = journey._journal(payload).document(payload["journey_id"])["_runtime"]
    assert runtime["calls_used"]["public_page"] == 5 and not any(key.startswith("contact:") for key in runtime["calls_used"])


@pytest.mark.parametrize("stage,timeout", [("plan", 120), ("reachjudge", 240), ("reachdraft", 240)])
def test_discovery_deadlines_follow_output_budget(journey_db, monkeypatch, stage, timeout):
    from app.gtm import journey
    journey.update_icp(journey_db["wid"], journey_db["vid"], "Engineering consultancies", problem="Review drawings")
    payload = _leased_payload(journey_db)
    calls = []
    def synthesize(*args, **kwargs):
        calls.append(kwargs)
        return {"items": []}
    monkeypatch.setattr("app.research.metered.synthesize", synthesize)
    asyncio.run(journey._discovery_model(payload, stage, {"candidates": [], "_batch": 0} if stage != "plan" else {}))
    assert calls[0]["provider_timeout_seconds"] == timeout
    assert calls[0]["pin_provider"] and calls[0]["journal"].prefix == "gtm"


@pytest.mark.parametrize("audience,problem", [
    ("Engineering consultancies in India", "Review drawings"),
    ("First-time cat owners", "Plan daily pet care"),
])
def test_uncertain_plan_receipt_survives_fallback_without_another_paid_dial(journey_db, monkeypatch, audience, problem):
    from app.gtm import journey
    from app.research import metered
    from app.db.models import CreditReservation
    from app.signals.research import HttpResearchProvider
    from app.research.prior_art import adapters
    from tests.idea_fixtures import install_provider
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fixture")
    install_provider(monkeypatch)
    dials = []
    async def timeout(transmit, kwargs, seconds, *args):
        context = json.loads(kwargs["messages"][-1]["content"])
        dials.append((kwargs["purpose"], "attempted_queries" in context))
        raise TimeoutError()
    async def search(*args, **kwargs):
        return []
    async def hn(*args, **kwargs):
        return {"items": []}
    monkeypatch.setattr(metered, "_transmit_with_deadline", timeout)
    monkeypatch.setattr(HttpResearchProvider, "search", search)
    monkeypatch.setattr(adapters, "search_hn", hn)
    journey.update_icp(journey_db["wid"], journey_db["vid"], audience, problem=problem, recruitment_opt_in=True)
    payload = _leased_payload(journey_db)
    async def run():
        for _ in range(2):
            try:
                await journey._discover(audience, problem=problem, payload=payload)
            except RuntimeError:
                pass
    asyncio.run(run())
    assert dials[0] == ("gtm.discovery.plan", False)
    assert dials[1:] in ([], [("gtm.discovery.plan", True)])
    doc = journey._journal(payload).document(payload["journey_id"])
    operation = doc["_runtime"]["operations"]["discovery-plan"]
    assert operation["state"] == "uncertain" and doc["discovery_plan"]
    from app.core.local_mode import is_local
    if is_local():
        from app.credits import local_meter
        assert operation["usage"]["reservation_id"] is None
        with local_meter._database() as connection:
            row = connection.execute("SELECT status FROM calls WHERE call_id = ?", (operation["usage"]["local_call_id"],)).fetchone()
        assert row["status"] == "unknown"
    else:
        with journey_db["factory"]() as session:
            assert session.get(CreditReservation, operation["usage"]["reservation_id"]).status == "reconciling"


def test_template_rejects_untrusted_claims_and_contacts(journey_db):
    from app.gtm import reachable
    from app.gtm.discovery import ReachDraft
    lead = {"name": "Write to invented@example.com"}
    reachable._call_draft(lead, "Visit https://evil.example and claim guaranteed results")
    checked = ReachDraft.model_validate({"id": 0, "body": lead["draft"], "connection_note": lead["connection_note"]})
    assert "15-minute" in checked.body and "evil.example" not in checked.body and "invented@example.com" not in checked.body


def test_service_offer_is_customer_not_competing_software(journey_db):
    from app.gtm.journey import _same_offer
    offer = "AI software for structural engineering drawing review"
    assert not _same_offer("https://consultancy.example/", "Design Consultancy", "We provide structural engineering drawing review services.", [], offer, offer)
    assert _same_offer("https://vendor.example/", "Vendor", "Our software automates structural engineering drawing review.", [], offer, offer)


def test_explicit_retry_keeps_uncertain_paid_receipt(journey_db):
    from app.core.jobs import complete_job
    from app.gtm import journey
    journey.update_icp(journey_db["wid"], journey_db["vid"], "Engineering consultancies", problem="Review drawings")
    payload = _leased_payload(journey_db)
    journal = journey._journal(payload)
    journal.save_operation(payload["journey_id"], "signal_search-uncertain", {"state": "uncertain", "attempt": 1, "usage": {"reservation_id": "held"}})
    with journey_db["factory"]() as session:
        complete_job(session, payload["_job_id"], worker_id=payload["_worker_id"], attempt=payload["_job_attempt"])
        row = journey._row(session, journey_db["vid"])
        data = json.loads(row.provenance_json)
        data["state"] = "needs_input"
        journey._store(session, row, data)
        session.commit()
    journey.prepare(journey_db["wid"], journey_db["vid"], retry=True)
    with journey_db["factory"]() as session:
        data = json.loads(journey._row(session, journey_db["vid"]).provenance_json)
    assert data["_runtime"]["operations"]["signal_search-uncertain"]["usage"]["reservation_id"] == "held"


@pytest.mark.parametrize("audience", ["Engineering consultancies", "Families planning creative workshops"])
def test_parallel_paid_search_receipts_settle_once_and_never_replay_uncertain_call(journey_db, safety_runtime, audience):
    from app.core.local_mode import is_local
    from app.gtm import journey
    from app.db.models import CreditLedgerEntry, CreditReservation
    from sqlalchemy import select
    journey.update_icp(journey_db["wid"], journey_db["vid"], audience, problem="Coordinate requests")
    payload = _leased_payload(journey_db)
    calls = []
    async def run():
        started = asyncio.Event()
        async def provider(index):
            calls.append(index)
            if len(calls) == 3:
                started.set()
            await started.wait()
            if index == 1:
                raise TimeoutError("outcome unknown")
            return [{"url": f"https://firm{index}.example/"}]
        async def search(index):
            try:
                return await journey._signal_call(payload, "signal_search", f"query {index}", lambda: provider(index), meter="firecrawl_search")
            except TimeoutError:
                return None
        first = await asyncio.wait_for(journey._parallel([lambda index=index: search(index) for index in range(3)]), 5)
        second = await journey._parallel([lambda index=index: search(index) for index in range(3)])
        assert first == second and (
            first == [[{"url": f"https://firm{index}.example/"}] for index in (0, 2)]
            if is_local() else first[1] is None)
    asyncio.run(run())
    assert calls == ([0, 1, 2, 1] if is_local() else [0, 1, 2])
    operations = journey._journal(payload).document(payload["journey_id"])["_runtime"]["operations"]
    assert sorted(op["state"] for op in operations.values()) == ["completed", "completed", "uncertain"]
    if is_local():
        uncertain = next(op for op in operations.values() if op["state"] == "uncertain")
        assert len(uncertain["interrupted_reads"]) == 1
        return
    reservations = [op["usage"]["reservation_id"] for op in operations.values()]
    assert len(set(reservations)) == 3
    with journey_db["factory"]() as session:
        receipts = list(session.scalars(select(CreditReservation).where(CreditReservation.id.in_(reservations))))
        assert sorted(receipt.status for receipt in receipts) == ["reconciling", "settled", "settled"]
        ledger = list(session.scalars(select(CreditLedgerEntry).where(CreditLedgerEntry.reservation_id.in_(reservations), CreditLedgerEntry.kind == "spend")))
        assert len(ledger) == len({entry.reservation_id for entry in ledger}) == 2

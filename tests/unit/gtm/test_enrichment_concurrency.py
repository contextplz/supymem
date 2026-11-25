"""Bounded business enrichment with real charging and durable operation receipts."""
from __future__ import annotations

import asyncio
from collections import Counter
import json
from datetime import timedelta

import pytest

from tests.unit.gtm.test_journey import journey_db as journey_db
from tests.unit.gtm.test_journey_signals import _leased_payload


DOMAINS = [
    ("audit", "Independent security audit firms", "Collecting audit evidence"),
    ("music", "Music schools for adult beginners", "Scheduling music lessons"),
]


@pytest.mark.parametrize("domain,audience,problem", DOMAINS)
@pytest.mark.hosted_only
def test_seed_lanes_preserve_pages_charges_checkpoint_order_and_restart(journey_db, monkeypatch, domain, audience, problem):
    from app.credits import metered
    from app.db.models import CreditAccount
    from app.gtm import email_learning, enrich, journey
    from app.signals import research

    data = journey_db
    assert enrich.CONCURRENCY == 6
    hosts = [f"{domain}{index}.example" for index in range(enrich.CONCURRENCY + 2)]
    seeds = [{"website": f"https://{host}/", "audience": audience} for host in hosts]
    monkeypatch.setenv("GTM_ENRICH_ENABLED", "1")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "synthetic")
    monkeypatch.setattr(email_learning, "cached", lambda key: None)
    monkeypatch.setattr(email_learning, "cache_save", lambda *args: None)
    monkeypatch.setattr(enrich, "guessed_routes", lambda *args, **kwargs: [])
    monkeypatch.setattr(journey, "_icp", lambda vid: {"who": audience})
    records, charges, snapshots = [], [], []
    state = {"interrupt": False}
    real_call = metered.call

    async def charged(callback, **kwargs):
        charges.append(kwargs)
        return await real_call(callback, **kwargs)

    class Provider:
        def __init__(self, *args, **kwargs):
            pass

        async def scrape(self, url, *, main_only=True):
            assert main_only is False
            host = enrich._host(url)
            started = asyncio.get_running_loop().time()
            record = [url, started, None]
            records.append(record)
            if state["interrupt"] and host != hosts[0]:
                await asyncio.Event().wait()
            await asyncio.sleep(0.003 * (3 - hosts.index(host) % 3))
            record[2] = asyncio.get_running_loop().time()
            return (f"<h1>{host}</h1><p>{audience}. {problem}.</p>"
                '<a href="/contact">Contact</a><a href="/about">About</a><a href="/team">Team</a>')

    monkeypatch.setattr(metered, "call", charged)
    monkeypatch.setattr(research, "HttpResearchProvider", Provider)

    def spent():
        with data["factory"]() as session:
            account = session.get(CreditAccount, data["account"])
            return account.spent_micro, account.reserved_micro

    baseline = None
    for lanes in (1, enrich.CONCURRENCY):
        monkeypatch.setattr(enrich, "CONCURRENCY", lanes)
        records.clear()
        charges.clear()
        snapshots.clear()
        progress = {}
        before, reserved = spent()
        result = asyncio.run(enrich.enrich_seeds(seeds + [seeds[0]], data["wid"], data["vid"], progress=progress,
            checkpoint=lambda: snapshots.append(json.loads(json.dumps(progress)))))
        after, pending = spent()
        assert pending == reserved == 0
        assert list(result) == hosts
        assert all(list(snapshot["done"]) == [host for host in hosts if host in snapshot["done"]] for snapshot in snapshots)
        expected = {host: [f"https://{host}/{path}" for path in ("", "contact", "about", "team")] for host in hosts}
        assert {host: [record[0] for record in records if enrich._host(record[0]) == host] for host in hosts} == expected
        assert len(charges) == len(records) == len(hosts) * enrich.MAX_PAGES
        assert after - before == len(records) * metered.price_micro("firecrawl_scrape")
        peak = max(sum(start <= record[1] < end for _, start, end in records) for record in records)
        assert peak <= lanes and (lanes == 1 or peak > 1)  # concurrent when allowed, never above the lane limit (exact overlap depends on runner timing)
        for host in hosts:
            pages = [record for record in records if enrich._host(record[0]) == host]
            assert all(previous[2] <= following[1] for previous, following in zip(pages, pages[1:]))
        current = (result, expected, len(charges), after - before)
        if baseline is None:
            baseline = current
        else:
            assert current == baseline

    records.clear()
    progress = {}
    state["interrupt"] = True

    async def restart():
        completed = asyncio.Event()
        def checkpoint():
            snapshots.append(json.loads(json.dumps(progress)))
            if hosts[0] in progress.get("done", {}):
                completed.set()
        task = asyncio.create_task(enrich.enrich_seeds(seeds, data["wid"], data["vid"], progress=progress, checkpoint=checkpoint))
        await asyncio.wait_for(completed.wait(), 30)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        saved = json.loads(json.dumps(progress))
        assert saved == snapshots[-1]
        assert hosts[0] in saved["done"]
        completed_reads = list(records)
        state["interrupt"] = False
        result = await enrich.enrich_seeds(seeds, data["wid"], data["vid"], progress=saved)
        assert result[hosts[0]] == saved["done"][hosts[0]]
        assert list(result) == [host for host in hosts if host not in saved["failed"]]
        assert not any(enrich._host(record[0]) == hosts[0] for record in records[len(completed_reads):])
        assert all(count == 1 for count in Counter(record[0] for record in records).values())

    asyncio.run(restart())


@pytest.mark.parametrize("domain,audience,problem", DOMAINS)
@pytest.mark.parametrize("lanes", [1, 6])
@pytest.mark.parametrize("scenario", ["complete", "budget", "uncertain"])
@pytest.mark.hosted_only
def test_journey_lanes_preserve_contact_budgets_charges_receipts_and_order(journey_db, monkeypatch, domain, audience, problem, lanes, scenario):
    from app.credits.metered import price_micro
    from app.db.models import CreditAccount
    from app.gtm import email_learning, enrich, journey, quality, reachable
    from app.research.prior_art import adapters
    from app.signals import research
    from core.clock import utcnow

    data = journey_db
    hosts = [f"{domain}{index}.example" for index in range(4)]
    now = utcnow()
    quote = f"We need {problem.lower()} without manual spreadsheets."
    hits = [{"url": f"https://discussion.example/{domain}/{index}", "title": problem, "provider": "firecrawl",
        "content": f"I work at [Practice {index}](https://{host}/).\n{quote}"} for index, host in enumerate(hosts)]
    reads = []

    class Provider:
        def __init__(self, *args, **kwargs):
            self.name = "firecrawl"

        async def search(self, query, **kwargs):
            return hits

        async def scrape(self, url, *, main_only=True):
            host = enrich._host(url)
            record = [url, main_only, asyncio.get_running_loop().time(), None]
            reads.append(record)
            if scenario == "uncertain" and host == hosts[1] and main_only:
                raise TimeoutError("Provider outcome unknown")
            await asyncio.sleep(0.003 * (3 - hosts.index(host) % 3))
            record[3] = asyncio.get_running_loop().time()
            return (f"<h1>Practice {hosts.index(host)}</h1><p>{audience}. {problem}.</p>"
                '<a href="/contact">Contact</a><a href="/about">About</a><a href="/team">Team</a>'
                f"<p>hello@{host}</p>")

    async def model(payload, stage, context):
        if stage == "plan":
            return {"queries": [{"query": f"{domain} workflow discussion", "intent": "customer_voice"}]}
        if stage == "judge":
            return {"items": [{"id": row["id"], "kind": "customer_voice", "quote": quote,
                "affiliation": row["text"].splitlines()[0], "name": "", "url": ""} for row in context["candidates"]]}
        return {"items": []}

    async def signal_page(provider, hit, payload, **kwargs):
        return hit, hit["content"], {"publishedTime": (now - timedelta(days=1)).isoformat()}

    async def passages(pages, *args, **kwargs):
        return [page | {"relevance": 0.9, "evidence_label": "need_passage"} for page in pages]

    async def classify(*args, **kwargs):
        return {"role_in_market": "buyer", "icp_fit": 0.9} if args[1] else None

    async def hn(*args, **kwargs):
        return {"items": []}

    monkeypatch.setenv("GTM_ENRICH_ENABLED", "1")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "synthetic")
    monkeypatch.setattr(email_learning, "cached", lambda key: None)
    monkeypatch.setattr(email_learning, "cache_save", lambda *args: None)
    monkeypatch.setattr(enrich, "guessed_routes", lambda *args, **kwargs: [])
    monkeypatch.setattr(research, "HttpResearchProvider", Provider)
    monkeypatch.setattr(adapters, "search_hn", hn)
    monkeypatch.setattr(journey, "_discovery_model", model)
    monkeypatch.setattr(journey, "utcnow", lambda: now)
    monkeypatch.setattr(journey, "_signal_page", signal_page)
    monkeypatch.setattr(quality, "select_passages", passages)
    monkeypatch.setattr(quality, "classify_home", classify)
    monkeypatch.setattr(reachable, "candidate_hits", lambda *args: [])
    if lanes == 1:
        async def serial(calls):
            return [await callback() for callback in calls]
        monkeypatch.setattr(journey, "_parallel", serial)
    journey.update_icp(data["wid"], data["vid"], audience, problem=problem)
    journey.prepare(data["wid"], data["vid"], recruitment_opt_in=True)
    payload = _leased_payload(data)
    journal = journey._journal(payload)
    def ceilings(row, doc, session):
        doc["_runtime"]["ceilings"].update(companies=2 if scenario == "budget" else len(hosts),
            contact_pages=3 if scenario == "budget" else enrich.MAX_PAGES + 1)
    journal.mutate(payload["journey_id"], payload, ceilings)

    result = asyncio.run(journey._discover(audience, problem=problem, payload=payload))
    expected_hosts = hosts[:2] if scenario == "budget" else [host for host in hosts if scenario != "uncertain" or host != hosts[1]]
    assert [enrich._host(row["website"]) for row in result] == expected_hosts
    runtime = journal.document(payload["journey_id"])["_runtime"]
    counts = {bucket: used for bucket, used in runtime["calls_used"].items() if bucket.startswith("contact:")}
    expected_counts = {"contact:" + host: 3 if scenario == "budget" else 1 if scenario == "uncertain" and host == hosts[1] else 5
        for host in (hosts[:2] if scenario == "budget" else hosts)}
    assert counts == expected_counts
    assert len(reads) == sum(expected_counts.values())
    successful = [record for record in reads if record[3] is not None]
    receipts = [receipt for key, receipt in runtime["operations"].items() if key.startswith("contact:")]
    assert sum(receipt["usage"].get("credits_micro", 0) for receipt in receipts) == len(successful) * price_micro("firecrawl_scrape")
    with data["factory"]() as session:
        account = session.get(CreditAccount, data["account"])
        assert account.reserved_micro == (price_micro("firecrawl_scrape") if scenario == "uncertain" else 0)
    if scenario == "complete":
        peak = max(sum(start <= record[2] < end for _, _, start, end in successful) for record in successful)
        assert peak <= lanes and (lanes == 1 or peak > 1)  # concurrent when allowed, never above the lane limit (exact overlap depends on runner timing)
    for host in expected_hosts:
        pages = [record for record in successful if enrich._host(record[0]) == host]
        expected_pages = [(f"https://{host}/{path}", main)
            for path, main in [("", True), ("", False), ("contact", False), ("about", False), ("team", False)]]
        assert [(record[0], record[1]) for record in pages] == expected_pages[:expected_counts["contact:" + host]]
        assert all(previous[3] <= following[2] for previous, following in zip(pages, pages[1:]))
    before = list(reads)
    from app.core.jobs import RetryableError, fail_job
    with data["factory"]() as session:
        fail_job(session, payload["_job_id"], RetryableError("worker_interrupted", retry_after_s=1),
            now=now - timedelta(minutes=1), worker_id=payload["_worker_id"], attempt=payload["_job_attempt"])
        session.commit()
    payload = _leased_payload(data, worker="restart-worker")
    journal = journey._journal(payload)
    assert payload["_job_attempt"] == 2
    assert asyncio.run(journey._discover(audience, problem=problem, payload=payload)) == result
    assert reads == before
    assert journal.document(payload["journey_id"])["_runtime"]["calls_used"] == runtime["calls_used"]


@pytest.mark.parametrize("domain,audience,problem", DOMAINS)
def test_six_public_lanes_keep_first_route_and_replay_without_more_reads(journey_db, monkeypatch, domain, audience, problem):
    from app.gtm import email_learning, enrich, journey, reachable
    from tests.unit.gtm.test_journey_signals import quality_response

    assert enrich.CONCURRENCY == 6
    journey.update_icp(journey_db["wid"], journey_db["vid"], audience, problem=problem)
    payload = _leased_payload(journey_db)
    context = {"mode": "B2B", "audience": audience, "problem": problem, "competitors": [], "offer": ""}
    hosts = [f"{domain}{index}.example" for index in range(enrich.CONCURRENCY + 2)]
    hits = [{"kind": "business", "url": f"https://{host}/", "title": host, "snippet": audience} for host in hosts]
    reads = []
    active = peak = 0

    async def scrape(self, url, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        reads.append(url)
        await asyncio.sleep(0.01)
        active -= 1
        host = enrich._host(url)
        return (f"<h1>{host}</h1><p>{audience}. We handle {problem}.</p>"
            '<a href="/contact">Contact</a><a href="/about">About</a><a href="/team">Team</a>'
            f"<p>Contact hello@{host}</p>")

    async def model(payload, stage, supplied):
        if stage in {"marketrole", "passages"}:
            return quality_response(stage, supplied)
        if stage == "reachjudge":
            return {"items": [{"id": row["id"], "kind": "business", "name": row["title"], "affiliation": "",
                "url": row["url"], "quote": f"{audience}.", "fit": 85, "why": f"Handles {problem}."}
                for row in supplied["candidates"]]}
        return {"items": []}

    monkeypatch.setattr(email_learning, "cached", lambda key: None)
    monkeypatch.setattr(email_learning, "cache_save", lambda *args: None)
    monkeypatch.setattr(reachable.PublicPages, "scrape", scrape)
    monkeypatch.setattr(journey, "_discovery_model", model)
    leads = asyncio.run(reachable.discover(reachable.PublicPages(), hits, [], context, payload, fallback=True))
    assert peak == enrich.CONCURRENCY
    assert reads == [hit["url"] for hit in hits]
    assert len(leads) == len(hosts)
    assert {lead["routes"][0]["email"] for lead in leads} == {f"hello@{host}" for host in hosts}
    journal = journey._journal(payload)
    runtime = journal.document(payload["journey_id"])["_runtime"]
    assert runtime["calls_used"]["public_page"] == len(hosts)
    assert not any(key.startswith("contact:") for key in runtime["calls_used"])
    assert asyncio.run(reachable.discover(reachable.PublicPages(), hits, [], context, payload, fallback=True)) == leads
    assert reads == [hit["url"] for hit in hits]
    assert journal.document(payload["journey_id"])["_runtime"]["calls_used"] == runtime["calls_used"]

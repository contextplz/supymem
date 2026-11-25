import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select

from tests.unit.gtm.test_search_lifecycle import search_domain as search_domain
from tests.unit.gtm.test_journey import journey_db as journey_db
from tests.unit.gtm.test_journey_signals import _leased_payload
from tests.unit.gtm.test_reviewed_ports import safety_runtime as safety_runtime


@pytest.fixture(autouse=True)
def local_search_mode(request, monkeypatch):
    if "local_search_domain" in request.fixturenames:
        monkeypatch.setenv("SUPYMEM_MODE", "local")


@pytest.fixture()
def local_search_domain(search_domain, monkeypatch):
    from tests.company_os_harness import configure_local_gtm
    from app.core.local_secrets import update_settings

    configure_local_gtm(monkeypatch)
    update_settings("search", {"provider": "builtin", "firecrawl_url": ""}, {})
    return search_domain


def test_local_default_customer_plan_reaches_search_without_a_social_profile(local_search_domain, monkeypatch):
    from types import SimpleNamespace
    from app.gtm import discovery, journey

    data = local_search_domain
    searches = []
    consumer = data["business_model"] == "consumer"
    plan = [{"query": data["audience"] + " " + suffix, "intent": "community" if consumer else "business"}
        for suffix in ("contact", "team contact")]

    async def model(payload, stage, context):
        return {"queries": plan} if stage in {"plan", "reformulate"} else {"items": []}

    async def search(query, **kwargs):
        searches.append(query)
        return []

    monkeypatch.setattr(journey, "_discovery_model", model)
    monkeypatch.setattr(discovery, "local_provider", lambda **kwargs: SimpleNamespace(name="duckduckgo", _limit=kwargs.get("limit", 10), search=search))
    journey.update_icp(data["wid"], data["vid"], data["audience"], problem=data["problem"], recruitment_opt_in=True)
    asyncio.run(journey._discover(data["audience"], [], problem=data["problem"], payload=_leased_payload(data)))
    assert searches


def test_local_research_off_refuses_customer_admission(local_search_domain):
    from app.core.local_capabilities import blocked_message
    from app.core.local_secrets import update_settings
    from app.db.models import Job
    from app.gtm import journey

    data = local_search_domain
    update_settings("capabilities", {"research": False}, {})
    with data["factory"]() as session:
        before = list(session.scalars(select(Job.id)))
    view = journey.prepare(data["wid"], data["vid"], recruitment_opt_in=True)
    assert view["error_code"] == "public_search_blocked"
    assert blocked_message("research") in view["message"] and "Research" in view["message"]
    with data["factory"]() as session:
        assert list(session.scalars(select(Job.id))) == before


def test_local_source_budget_completes_without_cancelling_paid_reads(local_search_domain, monkeypatch, caplog):
    from collections import Counter
    from app.gtm import enrich, journey

    data = local_search_domain
    journey.update_icp(data["wid"], data["vid"], data["audience"], problem=data["problem"], recruitment_opt_in=True)
    payload = _leased_payload(data)
    journal = journey._journal(payload)
    monkeypatch.setattr(enrich, "CONCURRENCY", 1)
    monkeypatch.setattr(enrich, "TOTAL_TIMEOUT", 0.005)
    calls, contacts = Counter(), {}
    sources = [f"https://source{index}.example/" for index in range(3)]

    async def source(url):
        if url in contacts:
            return
        async def read():
            calls[url] += 1
            await asyncio.sleep(0.025)
            return data["audience"] + ". " + data["problem"]
        text = await journey._signal_call(payload, "public_page", url, read)
        async def extract():
            await asyncio.sleep(0.005)
            return {"source_url": url, "quote": text}
        contacts[url] = await journey._signal_call(payload, "contact:extraction", url, extract)

    async def run():
        await journey._parallel([lambda url=url: source(url) for url in sources])
        assert len(contacts) == 3
        assert all(saved["state"] == "completed" for saved in journal.document(payload["journey_id"])["_runtime"]["operations"].values())
        monkeypatch.setattr(enrich, "TOTAL_TIMEOUT", 1)
        await journey._parallel([lambda url=url: source(url) for url in sources])

    asyncio.run(run())
    assert set(contacts) == set(sources)
    assert calls == {url: 1 for url in sources}
    assert not any(getattr(record, "error_code", None) == "gtm_enrichment_deferred"
        for record in caplog.records)


def test_local_cancelled_reader_releases_receipt(local_search_domain, monkeypatch):
    from app.gtm import journey

    data = local_search_domain
    journey.update_icp(data["wid"], data["vid"], data["audience"], problem=data["problem"], recruitment_opt_in=True)
    payload = _leased_payload(data)
    journal = journey._journal(payload)
    monkeypatch.setattr(journey, "_journal", lambda _: journal)
    url = "https://source.example/"

    async def run():
        started = asyncio.Event()
        async def read():
            started.set()
            await asyncio.Event().wait()
        task = asyncio.create_task(journey._signal_call(payload, "public_page", url, read))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        saved = journal.document(payload["journey_id"])["_runtime"]["operations"]
        assert {operation["state"] for operation in saved.values()} == {"retryable"}
        async def retry():
            return data["audience"]
        assert await journey._signal_call(payload, "public_page", url, retry) == data["audience"]
    asyncio.run(run())


def test_cancelled_model_finishes_and_checkpoints_before_retry(local_search_domain, monkeypatch):
    import threading
    from app.gtm import journey
    from app.research import metered
    from app.credits import gate

    data = local_search_domain
    journey.update_icp(data["wid"], data["vid"], data["audience"], problem=data["problem"], recruitment_opt_in=True)
    payload = _leased_payload(data)
    journal = journey._journal(payload)
    started, finish = threading.Event(), threading.Event()
    calls = []
    response = {"role_in_market": "buyer", "icp_fit": 0.9, "anti_icp": False}

    def synthesize(did, delivery, *, key, journal, **kwargs):
        calls.append(key)
        token = journal.prepare(did, key, delivery)
        journal.begin(did, key, delivery, {"source": "byok", "purpose": "gtm.discovery.marketrole"}, token)
        started.set()
        assert finish.wait(5)
        journal.save_operation(did, key, {"state": "completed", "response": response, "usage": {}})
        return response

    monkeypatch.setattr(metered, "synthesize", synthesize)
    monkeypatch.setattr(gate, "reconcile_receipt", lambda *args: 0)
    supplied = {"url": "https://source.example/", "audience": data["audience"], "problem": data["problem"]}

    async def run():
        task = asyncio.create_task(journey._discovery_model(payload, "marketrole", supplied))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await journey._discovery_model(payload, "marketrole", supplied) == response
    asyncio.run(run())
    assert len(calls) == 1
    assert {operation["state"] for operation in journal.document(payload["journey_id"])["_runtime"]["operations"].values()} == {"completed"}


def test_local_enrichment_budget_resumes_all_readable_sources(local_search_domain, monkeypatch):
    from collections import Counter
    from app.core.jobs import RetryableError
    from app.gtm import enrich, quality
    from app.research import local_providers

    data = local_search_domain
    hosts = [f"source{index}.example" for index in range(3)]
    reads, progress = Counter(), {}

    async def read(url, *, main_only=True):
        host = enrich._host(url)
        reads[host] += 1
        await asyncio.sleep(0.025)
        return f"<h1>{data['audience']}</h1><p>{data['problem']}. hello@{host}</p>"

    async def classify(*args):
        await asyncio.sleep(0.005)
        return {"role_in_market": "buyer", "icp_fit": 0.9, "anti_icp": False}

    async def passages(pages, *args, **kwargs):
        return pages

    monkeypatch.setattr(local_providers, "local_read", read)
    monkeypatch.setattr(quality, "classify_home", classify)
    monkeypatch.setattr(quality, "select_passages", passages)
    monkeypatch.setattr(enrich, "CONCURRENCY", 1)
    monkeypatch.setattr(enrich, "TOTAL_TIMEOUT", 0.005)
    context = {"audience": data["audience"], "problem": data["problem"], "mode": "B2C" if data["business_model"] == "consumer" else "B2B"}
    seeds = [{"website": f"https://{host}/"} for host in hosts]

    async def run():
        await enrich.enrich_seeds(seeds, data["wid"], data["vid"], progress=progress, context=context, payload={})
        assert len(progress["done"]) == 1
        assert not progress["failed"]
        monkeypatch.setattr(enrich, "TOTAL_TIMEOUT", 1)
        return await enrich.enrich_seeds(seeds, data["wid"], data["vid"], progress=progress, context=context, payload={})
    result = asyncio.run(run())
    assert set(result) == set(hosts)
    assert reads == {host: 1 for host in hosts}
    assert all(facts["contacts"] for facts in result.values())


@pytest.mark.parametrize("state", ["reserving", "calling", "uncertain"])
def test_local_read_reclaims_only_its_safe_receipts(local_search_domain, state):
    from app.core.jobs import FatalError
    from app.gtm import journey

    data = local_search_domain
    journey.update_icp(data["wid"], data["vid"], data["audience"], problem=data["problem"], recruitment_opt_in=True)
    payload = _leased_payload(data)
    journal = journey._journal(payload)
    did, key = payload["journey_id"], "discovery-passages-source"
    usage = {"source": "byok", "purpose": "gtm.discovery.passages", "task_invocation": "unknown-read"}
    saved = {"state": state, "attempt": payload["_job_attempt"], "worker_id": payload["_worker_id"], "usage": usage}
    journal.save_operation(did, key, saved)
    assert journal.recover(did, key, payload) is None
    token = journal.prepare(did, key, payload)
    journal.begin(did, key, payload, {}, token)
    if state != "reserving":
        assert journal.operation(did, key)["interrupted_reads"][0]["usage"] == usage
    for outward_usage in ({"source": "platform", "purpose": "gtm.discovery.passages", "reservation_id": "hold"},
                           {"source": "byok", "purpose": "email.send"}):
        journal.save_operation(did, "outward", saved | {"state": "calling", "usage": outward_usage})
        with pytest.raises(FatalError, match="gtm_operation_in_progress"):
            journal.recover(did, "outward", payload)
    journal.retry_reads = False
    journal.save_operation(did, "strict", saved | {"state": "reserving"})
    with pytest.raises(FatalError, match="gtm_operation_in_progress"):
        journal.prepare(did, "strict", payload)
    journal.retry_reads = True
    journal.save_operation(did, "strict", saved | {"state": "reserving", "worker_id": "other-worker"})
    with pytest.raises(FatalError, match="gtm_operation_in_progress"):
        journal.prepare(did, "strict", payload)


def test_local_reader_timeout_keeps_other_sources(local_search_domain, monkeypatch):
    from app.gtm import enrich, quality
    from app.research import local_providers

    data = local_search_domain
    progress = {}
    hosts = [f"source{index}.example" for index in range(3)]

    async def read(url, *, main_only=True):
        host = enrich._host(url)
        if host == hosts[0]:
            await asyncio.Event().wait()
        return f"<h1>{data['audience']}</h1><p>{data['problem']}. hello@{host}</p>"

    async def classify(*args):
        return {"role_in_market": "buyer", "icp_fit": 0.9, "anti_icp": False}

    async def passages(pages, *args, **kwargs):
        return pages

    monkeypatch.setattr(local_providers, "local_read", read)
    monkeypatch.setattr(quality, "classify_home", classify)
    monkeypatch.setattr(quality, "select_passages", passages)
    monkeypatch.setattr(enrich, "COMPANY_TIMEOUT", 0.02)

    async def run():
        return await asyncio.wait_for(enrich.enrich_seeds([{"website": f"https://{host}/"} for host in hosts],
            data["wid"], data["vid"], progress=progress, payload={}), 1)
    assert set(asyncio.run(run())) == set(hosts[1:])
    assert progress["failed"] == [hosts[0]]
    failure, = progress["read_failures"]
    assert (failure["url"], failure["reason"]) == (f"https://{hosts[0]}/", "timed out")


@pytest.mark.parametrize("all_failed", [False, True])
def test_local_source_failures_keep_readable_contacts(local_search_domain, monkeypatch, all_failed):
    from collections import Counter
    from copy import deepcopy
    from app.core.jobs import RetryableError
    from app.gtm import enrich, journey, quality
    from app.research import local_providers
    from app.tools.reader import ReaderError

    data = local_search_domain
    hosts = [f"source{index}.example" for index in range(6)]
    calls = Counter()
    snapshots = []
    progress = {}

    async def read(url, *, main_only=True):
        host = enrich._host(url)
        calls[host] += 1
        if all_failed or host in hosts[:2]:
            raise ReaderError("robots_disallowed" if host == hosts[0] else "reader_timeout")
        if host == hosts[2] and calls[host] == 1:
            raise RetryableError("page_temporarily_busy", retry_after_s=0)
        return f"<h1>Source {hosts.index(host)}</h1><p>{data['audience']}. {data['problem']}.</p><p>hello@{host}</p>"

    async def classify(*args):
        return {"role_in_market": "buyer", "icp_fit": 0.9, "anti_icp": False}

    async def passages(pages, *args, **kwargs):
        return pages

    monkeypatch.setattr(local_providers, "local_read", read)
    monkeypatch.setattr(quality, "classify_home", classify)
    monkeypatch.setattr(quality, "select_passages", passages)
    context = {"audience": data["audience"], "problem": data["problem"], "mode": "B2C" if data["business_model"] == "consumer" else "B2B"}
    result = asyncio.run(enrich.enrich_seeds([{"website": f"https://{host}/"} for host in hosts],
        data["wid"], data["vid"], progress=progress, checkpoint=lambda: snapshots.append(deepcopy(progress["done"])), context=context, payload={}))
    readable = [facts for facts in result.values() if facts.get("contacts")]
    assert len(result) == (0 if all_failed else 4)
    assert len(readable) == (0 if all_failed else 4)
    assert all(contact["source_url"] == f"https://{host}/" for host, facts in result.items() for contact in facts.get("contacts", []))
    assert len(progress["read_failures"]) == (6 if all_failed else 2)
    summary = enrich.source_failure_summary(progress["read_failures"])
    assert f"{6 if all_failed else 2} sources" in summary and "permissions" in summary and "timed out" in summary
    assert "Retry" in summary and "\u2014" not in summary
    assert any(0 < len(snapshot) < len(result) for snapshot in snapshots) if result else True
    if not all_failed:
        assert any(facts.get("contacts") for snapshot in snapshots if len(snapshot) < len(result) for facts in snapshot.values())
    assert calls[hosts[2]] == (1 if all_failed else 2)


def test_local_source_retry_releases_its_operation(local_search_domain, monkeypatch):
    from app.core.jobs import RetryableError
    from app.gtm import enrich, journey

    data = local_search_domain
    journey.update_icp(data["wid"], data["vid"], data["audience"], problem=data["problem"], recruitment_opt_in=True)
    payload = _leased_payload(data)
    journal = journey._journal(payload)
    original_begin = journal.begin
    attempts = []

    def begin(*args):
        attempts.append(1)
        if len(attempts) == 1:
            raise RetryableError("page_temporarily_busy", retry_after_s=0)
        return original_begin(*args)

    monkeypatch.setattr(journal, "begin", begin)
    monkeypatch.setattr(journey, "_journal", lambda _: journal)

    async def read():
        return "Public source text"

    result = asyncio.run(enrich.read_source(lambda: journey._signal_call(payload, "public_page", "https://source.example/", read)))
    assert result == "Public source text" and len(attempts) == 2
    assert all(operation["state"] == "completed" for operation in journal.document(payload["journey_id"])["_runtime"]["operations"].values())


def test_local_failed_source_is_skipped_on_job_retry(local_search_domain):
    from app.gtm import journey
    from app.tools.reader import ReaderError

    data = local_search_domain
    journey.update_icp(data["wid"], data["vid"], data["audience"], problem=data["problem"], recruitment_opt_in=True)
    payload = _leased_payload(data)
    attempts = []

    async def read():
        attempts.append(1)
        raise ReaderError("robots_disallowed")

    with pytest.raises(ReaderError):
        asyncio.run(journey._signal_call(payload, "public_page", "https://blocked.example/", read))
    assert asyncio.run(journey._signal_call(payload, "public_page", "https://blocked.example/", read)) == ""
    assert len(attempts) == 1


def test_local_search_progress_names_current_work(local_search_domain):
    from app.gtm.progress import compute_progress

    data = {"state": "queued", "_runtime": {"progress_step": "searching"}}
    assert "has not started" not in compute_progress(data)["step_label"]
    data["state"] = "running"
    assert compute_progress(data)["step_label"] == "Searching..."
    data["_runtime"]["source_reads"] = {"https://source0.example/": {"state": "failed"},
        "https://source1.example/": {"state": "read"}, "https://source2.example/": {"state": "reading"}}
    assert compute_progress(data)["step_label"] == "Reading 3 of 3 sources..."


def test_builtin_search_backoff_is_bounded(local_search_domain, monkeypatch):
    from ddgs.engines import ENGINES
    from ddgs.exceptions import RatelimitException
    from app.research import local_providers
    from app.signals import research
    from types import SimpleNamespace

    attempts, delays, clock = [], [], [0.0]

    def limited(*args, **kwargs):
        attempts.append(1)
        raise RatelimitException("rate limited")

    def sleep(delay):
        delays.append(delay)
        clock[0] += delay

    monkeypatch.setattr(research, "_ddgs_text", limited)
    monkeypatch.setattr(local_providers, "_builtin_cooldowns", {})
    monkeypatch.setattr(local_providers, "_builtin_next_at", 0.0)
    monkeypatch.setattr(local_providers, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
    provider = local_providers.LocalHttpResearchProvider("duckduckgo", "")
    for query in (local_search_domain["audience"], local_search_domain["audience"] + " alternatives"):
        with pytest.raises(local_providers.SearchEngineLimited, match="Try again in 30 minutes.*Your search and page reader"):
            asyncio.run(provider._search(query))
    engine_count = len(ENGINES["text"])
    assert len(attempts) == engine_count and delays == [1] * engine_count


@pytest.mark.parametrize("all_failed,short_budget", [(False, False), (True, False), (False, True)])
def test_local_customer_job_keeps_sources_and_reports_failed_reads(local_search_domain, monkeypatch, caplog, all_failed, short_budget):
    from collections import Counter
    from types import SimpleNamespace
    from app.chat import customers
    from app.core.jobs import RetryableError, complete_job
    from app.db.models import GtmContact, Job
    from app.gtm import enrich, journey, quality, reachable
    from app.gtm.discovery import LocalDiscoveryProvider
    from app.gtm.progress import compute_progress
    from app.research import local_providers
    from app.tools.reader import ReaderError

    data = local_search_domain
    consumer = data["business_model"] == "consumer"
    names = ["Ada Miller", "Ben Clarke", "Cara Evans", "Dan Wright", "Ella Brown", "Finn Green"]
    urls = [f"https://source{index}.example/" for index in range(6)]
    calls, progress = Counter(), []
    hits = [{"url": url, "title": name, "kind": "person" if consumer else "business", "snippet": data["audience"]}
        for url, name in zip(urls, names)]
    context = {"audience": data["audience"], "problem": data["problem"], "mode": "B2C" if consumer else "B2B",
        "competitors": [], "offer": "", "seen_lead_urls": []}
    if consumer:
        context.update(target_type="person", requested_role="caregiver")

    async def read(url, *, main_only=True):
        calls[url] += 1
        if all_failed or url in urls[:2]:
            raise ReaderError("robots_disallowed" if url == urls[0] else "reader_timeout")
        if url == urls[2] and calls[url] == 1:
            raise RetryableError("page_temporarily_busy", retry_after_s=0)
        if short_budget and url == urls[2]:
            await asyncio.sleep(7)
        index = urls.index(url)
        return f"<h1>{names[index]}</h1><p>{data['audience']}. {data['problem']}.</p><p>hello@source{index}.example</p>"

    async def classify(*args):
        return {"role_in_market": "buyer", "icp_fit": 0.9, "anti_icp": False}

    async def passages(pages, *args, **kwargs):
        return [page | {"relevance": 0.9} for page in pages]

    async def model(payload, stage, supplied):
        if stage == "reachdraft":
            return {"items": []}
        assert stage == "reachjudge"
        return {"items": [{"id": candidate["id"], "kind": candidate["kind"], "name": candidate["title"],
            "url": candidate["url"], "quote": data["audience"] + ". " + data["problem"] + ".", "affiliation": "",
            "fit": 90, "why": "You can ask about this published experience.", "value_exchange": "You can share what you learn.",
            "role": "caregiver" if consumer else "", "location": "Australia" if consumer else "Ireland"} for candidate in supplied["candidates"]]}

    async def discover(*args, payload, **kwargs):
        await reachable.discover(LocalDiscoveryProvider(SimpleNamespace(name="builtin", _limit=10)), hits, [], context, payload)
        return []

    monkeypatch.setattr(local_providers, "local_read", read)
    monkeypatch.setattr(quality, "classify_home", classify)
    monkeypatch.setattr(quality, "select_passages", passages)
    monkeypatch.setattr(journey, "_discovery_model", model)
    monkeypatch.setattr(journey, "_discover", discover)
    monkeypatch.setattr(journey, "_draft_ready", lambda *args: None)
    monkeypatch.setattr(journey, "_post_results", lambda *args: True)
    journey.update_icp(data["wid"], data["vid"], data["audience"], problem=data["problem"], recruitment_opt_in=True)
    payload = _leased_payload(data)
    journal = journey._journal(payload)
    original_mutate = journal.mutate

    def mutate(*args, **kwargs):
        result = original_mutate(*args, **kwargs)
        progress.append(compute_progress(journal.document(payload["journey_id"]))["step_label"])
        return result

    monkeypatch.setattr(journal, "mutate", mutate)
    monkeypatch.setattr(journey, "_journal", lambda _: journal)
    if short_budget:
        monkeypatch.setattr(enrich, "CONCURRENCY", 1)
        monkeypatch.setattr(enrich, "TOTAL_TIMEOUT", 1)
        asyncio.run(journey.run_prepare(payload))
        saved = journal.document(payload["journey_id"])
        reads = saved["_runtime"]["source_reads"]
        assert sum(source["state"] == "read" for source in reads.values()) == 4
        assert not any(operation["state"] in {"reserving", "calling"} for operation in saved["_runtime"]["operations"].values())
        attempted = calls.copy()
        monkeypatch.setattr(enrich, "TOTAL_TIMEOUT", 120)
    asyncio.run(journey.run_prepare(payload))
    if short_budget:
        assert calls == attempted
    saved = journal.document(payload["journey_id"])
    with data["factory"]() as session:
        complete_job(session, payload["_job_id"], worker_id=payload["_worker_id"], attempt=payload["_job_attempt"])
        session.commit()
        assert session.get(Job, payload["_job_id"]).status == "succeeded"
        contacts = list(session.scalars(select(GtmContact)))
        expected = 0 if all_failed else 4
        assert len(contacts) == expected, (calls, payload.get("_trace_counts"), saved.get("error_code"))
        assert all(journey._json(contact.enrichment_json)["contact_source_url"] in urls[2:] for contact in contacts)
    view = journey.status(data["wid"], data["vid"], authorize=False)
    reply = customers.render(data["vid"], view)
    diagnostics = saved["_runtime"]["source_reads"][urls[0]]["diagnostics"]
    assert diagnostics == {"error_class": "ReaderError", "message": "robots_disallowed",
        "http_status": None, "content_type": None, "byte_length": None}
    assert "robots_disallowed" not in str(view) + str(reply)
    assert any(record.getMessage().startswith("gtm_source_read_failed")
        and getattr(record, "diagnostics", None) == diagnostics for record in caplog.records)
    assert not any(record.getMessage() == "founder_action_failed" and getattr(record, "action", "").startswith("gtm.reachable.")
        and getattr(record, "error_type", "") == "ReaderError" for record in caplog.records)
    assert len(view["reachable_leads"]) == expected
    assert not any(detail in reply["body_md"] for detail in ("could not be read:", "permissions (automated reads blocked)", "timed out"))
    assert "\u2014" not in reply["body_md"]
    assert ("stopped" in reply["body_md"]) == all_failed
    assert "sources could not be read" not in view["message"]
    assert any(label.startswith("Reading ") and "of 6 sources" in label for label in progress)
    assert not any(operation["state"] in {"reserving", "calling"} for operation in saved["_runtime"]["operations"].values())


def _recovery_actions(reply):
    if any(action.get("action") == "openworker_search" for action in reply["actions"]):
        actions = [action for action in reply["actions"] if not action["action"].startswith("gtm.library_")]
        assert {action["action"] for action in actions} == {"openworker_search", "contacts.preview"}
        assert all(action["payload"]["search_context"] for action in actions)
        assert "\u2014" not in reply["body_md"]
        return
    assert any(action.get("action") == "recovery.alternative" and action["payload"]["step"] == "gtm.journey.prepare"
               for action in reply["actions"])
    assert any(action.get("action") in {"gtm.status", "gtm.find"}
               for action in reply["actions"])
    assert "\u2014" not in reply["body_md"]
    assert "something went wrong" not in reply["body_md"].lower().replace("something went wrong on our side", "")


@pytest.mark.parametrize("blocked", ["first", "all", "omitted"])
def test_blocked_judge_drops_batch_saves_remaining_and_never_replays(search_domain, monkeypatch, blocked):
    import json
    from collections import Counter
    from types import SimpleNamespace
    from app.chat import customers
    from app.db.models import BoundaryLedgerEntry, CreditLedgerEntry, Job
    from app.core.jobs import complete_job
    from app.gtm import journey
    from app.gtm.reachable import profile_key
    from app.signals.research import HttpResearchProvider
    from app.tasks.contracts import blocker
    from tests.idea_fixtures import install_provider
    from tests.unit.gtm.test_journey_signals import quality_response

    data = search_domain
    monkeypatch.setattr(journey.enrich, "CONCURRENCY", 1)
    monkeypatch.setattr("app.signals.research._self_hosted_firecrawl", lambda: ("https://crawl.example", "fixture"))
    monkeypatch.setattr("app.lead_library.service.match", lambda *args, **kwargs: {"items": []})
    journey.update_icp(data["wid"], data["vid"], data["audience"], problem=data["problem"], recruitment_opt_in=True)
    payload = _leased_payload(data)
    journal = journey._journal(payload)
    names = ["Morgan " + surname for surname in (
        "Cedar", "Birch", "Maple", "Ash", "Elm", "Pine", "Oak", "Willow",
        "Hazel", "Alder", "Rowan", "Aspen", "Walnut", "Cherry", "Beech", "Laurel")]
    candidates = [{"kind": "person", "url": f"https://www.linkedin.com/in/member{index}", "title": name,
        "text": f"{name}. Community coordinator. {data['audience']}. {data['problem']}.",
        "evidence_label": "Public source", "quality_selected": True, "contact_pages": [],
        "routes": [{"kind": "linkedin", "url": f"https://www.linkedin.com/in/member{index}"}],
        "pages": [{"url": f"https://www.linkedin.com/in/member{index}",
            "text": f"{name}. Community coordinator. {data['audience']}. {data['problem']}."}]}
        for index, name in enumerate(names)]
    if blocked != "omitted":
        for candidate in candidates[:8] if blocked == "first" else candidates:
            candidate["text"] += " password=BoundaryFixtureCredentialValue"
    journal.mutate(payload["journey_id"], payload, lambda row, doc, session: (
        doc.update(discovery_plan=[{"query": f"{data['audience']} biography {index}", "intent": "person"} for index in range(2)],
            search_target={"target_type": "person", "audience": data["audience"], "requested_count": 16}, force_discovery=True),
        doc["_runtime"].update(reachable_candidates_quality_v1=candidates)))
    searches, model_calls = Counter(), Counter()
    async def search(provider, query, **kwargs):
        searches[query] += 1
        return [{"url": candidate["url"], "title": candidate["title"], "snippet": candidate["text"]} for candidate in candidates]
    original = journey._discovery_model
    async def model(payload, stage, context):
        if stage in {"marketrole", "passages"}:
            return quality_response(stage, context)
        assert stage == "reachjudge"
        model_calls[(context["_batch"], bool(context.get("_omitted")))] += 1
        if blocked == "omitted" and context.get("_omitted"):
            context = context | {"candidates": [row | {"text": row["text"] + " password=BoundaryFixtureCredentialValue"}
                for row in context["candidates"]]}
        return await original(payload, stage, context)
    def completion(messages, kwargs):
        supplied = json.loads(messages[-1]["content"])["candidates"]
        if blocked == "omitted" and supplied[0]["id"] == 0:
            supplied = supplied[:1]
        items = [{"id": row["id"], "kind": "person", "name": names[row["id"]], "role": "Community coordinator",
            "quote": candidates[row["id"]]["pages"][0]["text"], "url": candidates[row["id"]]["url"],
            "affiliation": "", "fit": 90, "why": data["problem"], "value_exchange": data["problem"]} for row in supplied]
        return SimpleNamespace(model="DeepSeek-V4-Pro", service_tier="default",
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps({"items": items})))],
            usage=SimpleNamespace(prompt_tokens=300, completion_tokens=100, total_tokens=400))
    transmitted = install_provider(monkeypatch, completion)
    monkeypatch.setattr(HttpResearchProvider, "search", search)
    monkeypatch.setattr(HttpResearchProvider, "scrape", lambda *args, **kwargs: pytest.fail("Unexpected page read"))
    monkeypatch.setattr(journey, "_discovery_model", model)
    monkeypatch.setattr(journey, "_post_results", lambda *args: True)
    asyncio.run(journey.run_prepare(payload))
    expected = {"first": 8, "all": 0, "omitted": 9}[blocked]
    view = journey.status(data["wid"], data["vid"], authorize=False)
    reply = customers.render(data["vid"], view)
    assert view["state"] == ("ready" if expected else "needs_input")
    assert view["error_code"] == (None if expected else "discovery_boundary_blocked")
    assert view["source_access"] == "partial"
    assert len(view["reachable_leads"]) == expected
    assert f"You found {expected} of 16 people" in reply["body_md"]
    assert all(lead["name"] in names[8:] or blocked == "omitted" and lead["name"] == names[0]
        for lead in view["reachable_leads"])
    assert any(action.get("action") == "gtm.find" and action["label"] == "Search again" for action in reply["actions"])
    assert "provider's rules" not in reply["body_md"] and "boundary" not in reply["body_md"].lower()
    if not expected:
        assert "could not finish" in reply["body_md"]
        assert blocker("discovery_boundary_blocked")["class"] == "needs_input"
        assert blocker("research_boundary_blocked")["class"] == "policy"
    saved = journal.document(payload["journey_id"])
    failures = [operation for operation in saved["_runtime"]["operations"].values()
        if operation.get("error_code") == "discovery_boundary_blocked"]
    assert len(failures) == (2 if blocked == "all" else 1)
    assert all(operation["state"] == "failed" and operation["failure"]["category"] == "boundary"
        and operation["usage"]["settled"] and operation["usage"]["credits_micro"] == 0 for operation in failures)
    with data["factory"]() as session:
        ledger = list(session.scalars(select(BoundaryLedgerEntry).where(BoundaryLedgerEntry.blocked.is_(True))))
        assert len(ledger) == len(failures) and all("assignment" in row.block_reason for row in ledger)
        spends = len(list(session.scalars(select(CreditLedgerEntry).where(CreditLedgerEntry.kind == "spend"))))
    before = len(transmitted)
    journal.mutate(payload["journey_id"], payload, lambda row, doc, session: doc.update(force_discovery=True))
    payload = payload | {"_job_attempt": payload["_job_attempt"] + 1}
    with data["factory"]() as session:
        session.get(Job, payload["_job_id"]).attempts = payload["_job_attempt"]
        session.commit()
    asyncio.run(journey.run_prepare(payload))
    assert len(transmitted) == before and all(count == 1 for count in searches.values())
    assert model_calls[(0, blocked == "omitted")] == 1
    if blocked == "all":
        assert model_calls[(1, False)] == 1
    assert len(journey.status(data["wid"], data["vid"], authorize=False)["reachable_leads"]) == expected
    with data["factory"]() as session:
        assert len(list(session.scalars(select(BoundaryLedgerEntry).where(BoundaryLedgerEntry.blocked.is_(True))))) == len(failures)
        assert len(list(session.scalars(select(CreditLedgerEntry).where(CreditLedgerEntry.kind == "spend")))) == spends
        complete_job(session, payload["_job_id"], worker_id=payload["_worker_id"], attempt=payload["_job_attempt"])
        session.commit()
    journey.prepare(data["wid"], data["vid"], retry=True)
    retry_payload = _leased_payload(data)
    assert {profile_key(url) for url in saved["_runtime"]["blocked_judge_urls"]} <= set(journey._journal(retry_payload).document(payload["journey_id"])["seen_lead_urls"])


@pytest.mark.parametrize("outcome", ["retry", "partial", "empty", "all_failed"])
def test_query_failure_retries_once_without_stopping_successful_searches(search_domain, monkeypatch, outcome):
    from collections import Counter
    from app.chat import customers
    from app.db.models import CreditLedgerEntry, CreditReservation
    from app.gtm import journey, reachable
    from app.signals.research import HttpResearchProvider

    data = search_domain
    monkeypatch.setattr("app.signals.research._self_hosted_firecrawl", lambda: ("https://crawl.example", "fixture"))
    monkeypatch.setattr("app.lead_library.service.match", lambda *args, **kwargs: {"items": []})
    journey.update_icp(data["wid"], data["vid"], data["audience"], problem=data["problem"], recruitment_opt_in=True)
    payload = _leased_payload(data)
    journal = journey._journal(payload)
    queries = [f'{data["audience"]} biography {index}' for index in range(3)]
    journal.mutate(payload["journey_id"], payload, lambda row, doc, session: (
        doc.update(discovery_plan=[{"query": query, "intent": "person"} for query in queries],
            search_target={"target_type": "person", "audience": data["audience"], "requested_count": 10},
            force_discovery=True),
        doc["_runtime"]["ceilings"].update(signal_search=len(queries))))
    calls = Counter()
    leads = [{"id": f"lead-{index}", "name": f"Person {index}", "kind": "person", "fit": 90,
        "source_url": f"https://biography{index}.example/person", "quote": data["audience"],
        "value_exchange": data["problem"], "routes": [{"kind": "website", "url": f"https://biography{index}.example/person"}]}
        for index in range(4)]

    async def search(provider, query, **kwargs):
        assert provider.name == "firecrawl"
        calls[query] += 1
        if outcome == "all_failed" or query == queries[0] and (outcome != "retry" or calls[query] == 1):
            if outcome == "retry":
                import httpx
                response = httpx.Response(429, request=httpx.Request("POST", "https://crawl.example/search"))
                raise httpx.HTTPStatusError("Rate limited", request=response.request, response=response)
            raise TimeoutError("private provider detail")
        if outcome == "empty" or query != queries[1]:
            return []
        return [{"url": lead["source_url"], "title": lead["name"], "snippet": data["audience"]} for lead in leads]

    async def discover(provider, hits, signals, context, payload, **kwargs):
        assert hits
        journal.mutate(payload["journey_id"], payload, lambda row, doc, session: doc.update(reachable_leads=leads))
        return leads

    async def model(payload, stage, context):
        assert stage == "reformulate"
        return {"queries": [{"query": query, "intent": "person"} for query in queries]}

    monkeypatch.setattr(HttpResearchProvider, "search", search)
    monkeypatch.setattr(reachable, "discover", discover)
    monkeypatch.setattr(journey, "_discovery_model", model)
    monkeypatch.setattr(journey, "_post_results", lambda *args: True)
    asyncio.run(journey.run_prepare(payload))
    assert calls == Counter({queries[0]: 2 if outcome == "retry" else 1, queries[1]: 1, queries[2]: 1})
    view = journey.status(data["wid"], data["vid"], authorize=False)
    reply = customers.render(data["vid"], view)
    from app.core.local_mode import is_local
    if outcome == "all_failed":
        assert view["error_code"] == "public_search_unavailable"
        assert ("Keys and connections" if is_local() else "Your customer search stopped.") in reply["body_md"]
        _recovery_actions(reply)
    else:
        assert view["state"] == ("needs_input" if outcome == "empty" else "ready")
        assert view["error_code"] == ("no_public_candidates" if outcome == "empty" else None)
        assert f'You found {0 if outcome == "empty" else 4} of 10 people' in reply["body_md"]
        assert "stopped" not in reply["body_md"].lower()
        if outcome == "empty":
            assert reply["actions"]
        else:
            assert any(action.get("action") == "gtm.find" and action["label"] == "Search again" for action in reply["actions"])
    operations = journal.document(payload["journey_id"])["_runtime"]["operations"]
    with data["factory"]() as session:
        reservations = list(session.scalars(select(CreditReservation)))
        spent = list(session.scalars(select(CreditLedgerEntry).where(CreditLedgerEntry.kind == "spend")))
        if is_local():
            assert not reservations and not spent
        else:
            assert len(reservations) == sum(calls.values())
            assert sum(reservation.status == "reconciling" for reservation in reservations) == (3 if outcome == "all_failed" else 0 if outcome == "retry" else 1)
            assert len(spent) == (0 if outcome == "all_failed" else 3 if outcome == "retry" else 2)
    assert all(operation.get("state") != "calling" for operation in operations.values())


@pytest.mark.parametrize("interruption", ["timeout", "uncertain-replay", "settlement", "missing-attempt", "unbilled"])
def test_search_retries_unbilled_or_local_read_attempts(search_domain, monkeypatch, interruption, safety_runtime):
    from collections import Counter
    import httpx
    from app.credits import gate
    from app.db.models import CreditLedgerEntry, CreditReservation
    from app.gtm import journey
    from app.signals.research import HttpResearchProvider
    from app.core.local_mode import is_local

    data = search_domain
    monkeypatch.setattr("app.signals.research._self_hosted_firecrawl", lambda: ("https://crawl.example", "fixture"))
    monkeypatch.setattr("app.lead_library.service.match", lambda *args, **kwargs: {"items": []})
    journey.update_icp(data["wid"], data["vid"], data["audience"], problem=data["problem"], recruitment_opt_in=True)
    payload = _leased_payload(data)
    journal = journey._journal(payload)
    queries = [f'{data["audience"]} biography {index}' for index in range(2)]
    journal.mutate(payload["journey_id"], payload, lambda row, doc, session: (
        doc.update(discovery_plan=[{"query": query, "intent": "person"} for query in queries],
            broadened_discovery_plan=[],
            search_target={"target_type": "person", "audience": data["audience"], "requested_count": 2},
            force_discovery=True),
        doc["_runtime"]["ceilings"].update(signal_search=len(queries))))
    key = journey._signal_key(journal.document(payload["journey_id"])["_runtime"], "signal_search", queries[0])
    calls = Counter()
    original_settle = gate._settle_receipt

    def settle(usage, counts):
        if interruption == "settlement" and usage["request_id"].startswith(f'{payload["journey_id"]}:{key}:'):
            raise RuntimeError("Settlement unavailable")
        return original_settle(usage, counts)

    async def search(provider, query, **kwargs):
        calls[query] += 1
        if query == queries[0]:
            if interruption in {"timeout", "uncertain-replay", "missing-attempt"}:
                raise TimeoutError("Response unavailable")
            if interruption == "unbilled" and calls[query] == 1:
                response = httpx.Response(429, request=httpx.Request("POST", "https://crawl.example/search"))
                raise httpx.HTTPStatusError("Rate limited", request=response.request, response=response)
        return []

    monkeypatch.setattr(HttpResearchProvider, "search", search)
    monkeypatch.setattr(gate, "_settle_receipt", settle)
    if is_local() and interruption == "settlement":
        save_operation = type(journal).save_operation
        def fail_receipt(self, document_id, operation_key, operation):
            if operation_key == key and operation.get("state") == "completed":
                raise RuntimeError("Local response receipt unavailable")
            return save_operation(self, document_id, operation_key, operation)
        monkeypatch.setattr(type(journal), "save_operation", fail_receipt)
    monkeypatch.setattr(journey, "_post_results", lambda *args: True)
    if interruption in {"uncertain-replay", "missing-attempt"}:
        with pytest.raises(TimeoutError):
            asyncio.run(journey._signal_call(payload, "signal_search", queries[0], lambda: search(None, queries[0]), meter="firecrawl_search"))
        if interruption == "missing-attempt":
            journal.mutate(payload["journey_id"], payload, lambda row, doc, session: (
                doc["_runtime"]["operations"][key].update(state="responded", response=[]),
                doc["_runtime"]["operations"][key].pop("attempt")))
    asyncio.run(journey.run_prepare(payload))
    retry_read = is_local() and interruption == "uncertain-replay"
    assert calls == Counter({queries[0]: 2 if interruption == "unbilled" or retry_read else 1, queries[1]: 1}), calls
    operations = journal.document(payload["journey_id"])["_runtime"]["operations"]
    operation = operations[key]
    if interruption == "unbilled":
        assert operation["state"] == "retryable"
        assert operation["usage"]["definitive_unspent"] and operation["usage"]["settled"]
        assert operation["usage"]["credits_micro"] == 0
    else:
        expected = "uncertain" if interruption in {"timeout", "uncertain-replay"} or is_local() and interruption == "settlement" else "responded"
        assert operation["state"] == expected
        assert not any(name.startswith("signal_search_retry-") for name in operations)
    if is_local():
        return
    with data["factory"]() as session:
        reservations = list(session.scalars(select(CreditReservation)))
        assert len(reservations) == sum(calls.values())
        assert sum(reservation.status == "reconciling" for reservation in reservations) == (interruption != "unbilled")
        spent = list(session.scalars(select(CreditLedgerEntry).where(CreditLedgerEntry.kind == "spend")))
        assert len(spent) == (2 if interruption == "unbilled" else 1)


def test_search_failure_sentence_and_actions_are_posted_once(search_domain, monkeypatch):
    import json
    from app.chat import milestones
    from app.gtm import journey
    from app.gtm.progress import failure_message, normalize_search_reply

    data = search_domain
    view = {"state": "ready", "icp": {"who": data["audience"]}, "can_prepare": True,
        "error_code": "public_search_unavailable", "search_target": {"target_type": "person", "requested_count": 10},
        "reachable_leads": [{"id": "partial", "name": "Published person", "kind": "person", "fit": 90,
            "source_url": "https://biography.example/person", "quote": data["audience"], "routes": []}],
        "outreach_next_step": {"actions": [{"label": "Review your leads", "action": "gtm.status"}]}}
    posted = []
    monkeypatch.setattr(journey, "status", lambda *args, **kwargs: view)
    monkeypatch.setattr(milestones, "post_milestone", lambda *args, **kwargs: posted.append(kwargs) or True)
    assert journey._post_results(data["wid"], data["vid"], "failed-search")
    reply = posted[-1]
    assert reply["body_md"].count(failure_message("public_search_unavailable")) == 1
    identities = [(action.get("action"), json.dumps(action.get("payload"), sort_keys=True)) for action in reply["actions"]]
    assert len(identities) == len(set(identities))
    assert [action["label"] for action in reply["actions"]] == ["Review your leads", "Retry", "Do it another way"]
    failure = failure_message("public_search_unavailable")
    body = f"Charlie: {failure}\n\n{data['audience']}\n\n{failure}"
    distinct = {"label": "Refine your search", "action": "gtm.find", "payload": {"note": data["audience"]}}
    direct = {"body_md": body, "content": body, "parts": [{"type": "text", "text": body}],
        "actions": reply["actions"] + [distinct]}
    normalized = normalize_search_reply(direct)
    assert normalized["body_md"].count(failure) == normalized["content"].count(failure) == 1
    assert normalized["parts"][0]["text"].count(failure) == 1
    assert normalized["actions"][-1] == distinct and normalize_search_reply(normalized) == normalized
    assert direct["body_md"] == body


def test_person_page_server_error_is_only_a_safe_warning(search_domain, monkeypatch, caplog):
    import httpx
    from app.gtm import journey, reachable

    data = search_domain
    journey.update_icp(data["wid"], data["vid"], data["audience"], problem=data["problem"], recruitment_opt_in=True)
    payload = _leased_payload(data)

    class Provider:
        async def scrape(self, url, *, main_only=True):
            raise httpx.HTTPStatusError("private provider detail", request=httpx.Request("GET", url), response=httpx.Response(500))

    async def call(payload, bucket, identity, callback, **kwargs):
        return await callback()

    monkeypatch.setattr(journey, "_signal_call", call)
    context = {"mode": "B2B" if data["business_model"] == "b2b" else "B2C", "audience": data["audience"],
        "problem": data["problem"], "target_type": "person", "competitors": [], "offer": ""}
    from app.core.local_mode import is_local
    from app.signals.local_reader import SourcesUnreadable
    error_type, code = (SourcesUnreadable, "public_sources_unavailable") if is_local() else (RuntimeError, "people_page_unavailable")
    with caplog.at_level("WARNING"), pytest.raises(error_type, match=code):
        asyncio.run(reachable.discover(Provider(), [{"kind": "person", "url": "https://biography.example/person?session=private-value#private-fragment",
            "title": "Published person", "snippet": data["audience"]}], [], context, payload))
    warnings = [record for record in caplog.records if record.message == ("gtm_source_read_failed" if is_local() else "gtm_person_page_unavailable")]
    assert len(warnings) == 1 and warnings[0].levelname == "WARNING"
    if is_local():
        assert warnings[0].diagnostics["error_class"] == "HTTPStatusError" and warnings[0].diagnostics["http_status"] == 500
        assert warnings[0].source_host == "biography.example"
    else:
        assert warnings[0].error_type == "HTTPStatusError" and warnings[0].status == 500
        assert warnings[0].source_url == "https://biography.example/person"
    assert warnings[0].exc_info is None
    assert not any(record.message == "founder_action_failed" for record in caplog.records)
    assert "private provider detail" not in caplog.text
    assert "private-value" not in caplog.text and "private-fragment" not in caplog.text


@pytest.mark.parametrize("audience,role,company,domain,consumer", [
    ("Horticulture scheduling coordinators at commercial greenhouse growers", "Horticulture scheduling coordinator",
     "Glassgarden", "glassgarden.example", False),
    ("Pottery class coordinators at amateur ceramic painting circles", "Pottery class coordinator",
     "", "clayweekend.example", True),
])
@pytest.mark.parametrize("failure", ["server_error", "antibot"])
def test_blocked_person_page_extracts_only_published_search_identity(journey_db, monkeypatch, caplog,
        audience, role, company, domain, consumer, failure):
    import httpx
    from app.gtm import enrich, journey, reachable

    data = journey_db
    journey.update_icp(data["wid"], data["vid"], audience, problem="Arrange interviews", recruitment_opt_in=True)
    payload = _leased_payload(data)
    url = f"https://{domain}/team"
    snippet = f"Jordan Ellis, {role}" + (f", at {company}" if company else f". {audience}") + f". Contact jordan@{domain}."
    calls = []

    class Provider:
        name = "firecrawl"
        async def scrape(self, page, *, main_only=True):
            calls.append(page)
            if failure == "server_error":
                raise httpx.HTTPStatusError("document_antibot", request=httpx.Request("GET", page), response=httpx.Response(500))
            return "Access denied. Verify you are human."

    async def call(payload, bucket, identity, callback, **kwargs):
        return await callback()

    async def model(payload, stage, context):
        assert stage == "reachjudge"
        assert all(row["search_result_only"] for row in context["candidates"])
        return {"items": [{"id": row["id"], "kind": "person", "name": "Jordan Ellis" if row["url"] == url else "Invented Person",
            "role": role, "company": company, "affiliation": "", "url": row["url"], "quote": snippet,
            "fit": 90, "why": "You can learn about their work", "value_exchange": "You can ask about their published experience"}
            for row in context["candidates"]]}

    monkeypatch.setattr(journey, "_signal_call", call)
    monkeypatch.setattr(journey, "_discovery_model", model)
    monkeypatch.setenv("GTM_ENRICH_ENABLED", "1")
    monkeypatch.setattr(enrich, "guessed_routes", lambda *args, **kwargs: pytest.fail("Search-only people must not get guessed addresses"))
    hits = [{"kind": "person", "url": source, "title": company or role, "snippet": snippet} for source in (url, f"https://{domain}/about")]
    context = {"mode": "B2C" if consumer else "B2B", "target_type": "person", "audience": audience,
        "problem": "Arrange interviews", "offer": "", "competitors": [], "requested_role": role,
        "organisations": [{"url": f"https://{domain}/", "quote": audience}] if company else []}
    from app.core.local_mode import is_local
    if is_local():
        from app.signals.local_reader import SourcesUnreadable

        monkeypatch.setattr(journey, "_discovery_model", lambda *_args: pytest.fail("Do not judge a source you could not read"))
        with pytest.raises(SourcesUnreadable, match="public_sources_unavailable"):
            asyncio.run(reachable.discover(Provider(), hits, [], context, payload))
        saved = journey._journal(payload).document(payload["journey_id"])
        reads = saved["_runtime"]["source_reads"]
        assert set(reads) == set(calls) == {hit["url"] for hit in hits}
        assert all(item["state"] == "failed" for item in reads.values())
        assert not saved.get("reachable_leads")
        return
    with caplog.at_level("WARNING"):
        leads = asyncio.run(reachable.discover(Provider(), hits, [], context, payload))
    assert len(leads) == 1
    assert leads[0]["name"] == "Jordan Ellis" and leads[0]["role"] == role and leads[0]["company"] == company
    assert leads[0]["source_url"] == url and leads[0]["quote"] in snippet
    assert reachable.published_route(leads[0]) is None
    assert reachable.contact_seed(leads[0]) is None
    assert not any(route.get("email") for route in leads[0]["routes"])
    assert calls == [hit["url"] for hit in hits]
    assert any(record.message == "gtm_person_page_unavailable" for record in caplog.records)


@pytest.mark.parametrize("status", [404, 500, None])
def test_single_page_enrichment_http_error_is_only_a_safe_warning(search_domain, monkeypatch, caplog, status):
    import httpx
    from types import SimpleNamespace
    from app.core.local_mode import is_local
    from app.gtm import enrich, journey, reachable

    data = search_domain
    journey.update_icp(data["wid"], data["vid"], data["audience"], problem=data["problem"], recruitment_opt_in=True)
    payload = _leased_payload(data)
    calls = []

    async def unavailable(provider, url, *args, **kwargs):
        calls.append(url)
        if status is None:
            raise ValueError("private provider detail")
        raise httpx.HTTPStatusError("private provider detail", request=httpx.Request("GET", url + "?token=private"),
            response=httpx.Response(status))

    monkeypatch.setattr(enrich, "enrich_site", unavailable)
    monkeypatch.setattr(enrich, "enabled", lambda: True)
    monkeypatch.setattr(journey, "_same_offer", lambda *args, **kwargs: False)
    context = {"mode": "B2B" if data["business_model"] == "b2b" else "B2C", "audience": data["audience"],
        "problem": data["problem"], "competitors": [], "offer": ""}
    with caplog.at_level("WARNING"):
        leads = asyncio.run(reachable.discover(SimpleNamespace(), [{"kind": "business", "url": "https://workshop.example",
            "title": data["audience"], "expert_role": data["audience"]}], [], context, payload))
    assert calls == ["https://workshop.example"] and leads == []
    warning = "gtm_source_read_failed" if is_local() else "gtm_enrichment_page_unavailable"
    warnings = [record for record in caplog.records if record.message == warning]
    failures = [record for record in caplog.records if record.message == "founder_action_failed"]
    if status is None:
        assert not warnings and len(failures) == 1
        assert failures[0].action == "gtm.reachable.enrich" and failures[0].levelname == "ERROR"
    else:
        assert len(warnings) == 1 and warnings[0].levelname == "WARNING"
        if is_local():
            diagnostics = warnings[0].diagnostics
            assert diagnostics["error_class"] == "HTTPStatusError" and diagnostics["http_status"] == status
            assert diagnostics["message"] == f"HTTP {status}"
            runtime = journey._journal(payload).document(payload["journey_id"])["_runtime"]
            source = runtime["source_reads"][calls[0]]
            assert source["state"] == "failed" and source["diagnostics"] == diagnostics
        else:
            assert warnings[0].error_type == "HTTPStatusError" and warnings[0].status == status
        assert warnings[0].exc_info is None and not failures
    assert "private provider detail" not in caplog.text and "token=private" not in caplog.text


def test_empty_search_offers_public_routes_and_known_contact_without_same_search(search_domain, monkeypatch):
    from app.chat import customers
    from app.gtm import journey

    data = search_domain
    monkeypatch.setattr(customers, "_library_section", lambda *args, **kwargs: ("", []))
    view = {"state": "needs_input", "error_code": "no_public_candidates", "can_prepare": True,
        "icp": {"who": data["audience"], "mode": "consumer" if data["business_model"] == "consumer" else "company"}}
    reply = customers.render(data["vid"], view)
    assert [action["action"] for action in reply["actions"]] == ["openworker_search", "openworker_search", "contacts.preview"]
    assert "forums" in reply["actions"][0]["payload"]["query"]
    assert "site:linkedin.com/in/" in reply["actions"][1]["payload"]["query"]
    assert all(data["audience"] in action["payload"]["search_context"] for action in reply["actions"])
    assert "email address or usable contact page" in reply["body_md"]
    assert "Broaden your audience" in reply["body_md"] and "existing customers" in reply["body_md"]
    assert "add someone you know" in reply["body_md"]
    assert journey._next_step(data["vid"], view)["actions"] == reply["actions"]
    assert "\u2014" not in str(reply)


def test_local_changed_search_shows_only_the_latest_request(local_search_domain):
    from app.chat import customers

    audience = local_search_domain["audience"]
    prior = f"Find {audience} near Chicago for an interview."
    latest = f"Find {audience} near Boston for an interview."
    reply = customers.empty_search_reply({"icp": {"who": audience}, "discovery_note": prior,
                                          "search_request": f"{prior}\nLatest request: {latest}"})
    assert "I haven't searched your updated request yet" in reply["body_md"]
    assert "Nothing was posted or sent" in reply["body_md"]
    assert latest in reply["body_md"] and prior not in reply["body_md"]
    assert "Boston" in reply["actions"][0]["payload"]["query"]
    assert "Chicago" not in str(reply["actions"])


def test_local_expert_judge_requires_published_workflow_not_the_consumers_pain(local_search_domain, monkeypatch):
    import json
    from app.gtm import journey
    from app.research import metered

    data = local_search_domain
    role = "Family care educator" if data["business_model"] == "consumer" else "Bicycle mechanic"
    quote = f"Avery Morgan is a {role}. Practical training and advice are available."
    prompts = []

    def synthesize(*args, **kwargs):
        prompts.append(kwargs["messages"][0]["content"])
        return kwargs["parse"](json.dumps({"items": [{"id": 0, "kind": "person", "name": "Avery Morgan",
            "role": role, "affiliation": "", "url": "https://practitioner.example/", "quote": quote,
            "fit": 80, "why": "Published practical experience of the relevant workflow.", "lead_type": "insight",
            "value_exchange": "Compare practical experience and share research findings."}]}))

    monkeypatch.setattr(metered, "synthesize", synthesize)
    journey.update_icp(data["wid"], data["vid"], data["audience"], problem=data["problem"], recruitment_opt_in=True)
    payload = _leased_payload(data)
    result = asyncio.run(journey._discovery_model(payload, "reachjudge", {
        "mode": "B2C" if data["business_model"] == "consumer" else "B2B", "audience": data["audience"],
        "problem": data["problem"], "target_type": None, "candidates": [{"id": 0, "kind": "business",
            "expert_role": role, "text": quote, "url": "https://practitioner.example/"}]}))
    assert result["items"][0]["lead_type"] == "insight"
    assert "Do not require the audience's personal circumstances" in prompts[0]
    assert "return the literal published role" in prompts[0]
    assert "Reject conflicting published geography or service scope" in prompts[0]


def test_local_public_contacts_broaden_after_unusable_sources_within_the_saved_budget(local_search_domain, monkeypatch):
    from app.gtm import discovery, journey, reachable
    from app.research.prior_art import adapters

    data = local_search_domain
    consumer = data["business_model"] == "consumer"
    role = "Family care educator" if consumer else "Bicycle mechanic"
    initial = {"query": role + " initial public contact", "intent": "person", "expert_role": role}
    variants = [{"query": role + " " + suffix, "intent": "person", "expert_role": role}
        for suffix in ("practice contact", "consultation booking")]
    searches, reformulations = [], []

    async def search(self, query, **kwargs):
        searches.append(query)
        planned = [initial, *variants]
        if query not in [item["query"] for item in planned]:
            return [{"url": "https://reference.example/", "title": "General reference articles",
                "snippet": data["problem"], "provider": "builtin"}]
        index = next(index for index, item in enumerate(planned) if item["query"] == query)
        return [{"url": f"https://practitioner{index}.example/", "title": role, "snippet": data["problem"], "provider": "builtin"}]

    async def model(payload, stage, context):
        if stage == "reformulate":
            reformulations.append(context)
            return {"queries": variants}
        return {"items": []}

    async def discovered(provider, hits, candidates, context, payload, **kwargs):
        assert all(hit.get("expert_role") == role for hit in hits)
        leads = [{"id": hit["url"], "kind": "person", "judging_status": "judged", "role": role,
            "name": "Published practitioner", "source_url": hit["url"], "expert_role": role,
            "routes": [{"kind": "website", "url": hit["url"], "source_url": hit["url"]}]} for hit in hits]
        field = "industry_experts" if consumer else "reachable_leads"
        journey._journal(payload).mutate(payload["journey_id"], payload,
            lambda row, doc, session: doc.update({field: doc.get(field, []) + leads}))
        return leads

    async def hn(*args, **kwargs):
        return {"items": []}

    monkeypatch.setattr(discovery.LocalDiscoveryProvider, "search", search)
    monkeypatch.setattr(journey, "_discovery_model", model)
    monkeypatch.setattr(reachable, "discover", discovered)
    monkeypatch.setattr(adapters, "search_hn", hn)
    journey.update_icp(data["wid"], data["vid"], data["audience"], problem=data["problem"], recruitment_opt_in=True)
    payload = _leased_payload(data)
    journal = journey._journal(payload)
    journal.mutate(payload["journey_id"], payload, lambda row, doc, session: doc.update(
        discovery_plan=[] if consumer else [initial], expert_discovery_plan=[initial] if consumer else [],
        search_target={"target_type": None if consumer else "person", "audience": data["audience"], "requested_count": 3}))
    asyncio.run(journey._discover(data["audience"], problem=data["problem"], payload=payload))
    saved = journal.document(payload["journey_id"])
    assert len(saved["industry_experts" if consumer else "reachable_leads"]) == 3
    assert len(reformulations) == 1 and saved["broadened_discovery_plan"] == variants
    assert all(searches.count(item["query"]) == 1 for item in variants)
    assert saved["_runtime"]["calls_used"]["broaden_search"] == journey.CALL_CEILINGS["broaden_search"]
    assert saved["_runtime"]["calls_used"]["signal_search"] <= journey.CALL_CEILINGS["signal_search"]


@pytest.mark.parametrize("has_results", [True, False])
def test_empty_self_hosted_discovery_reformulates_with_model_before_giving_up(search_domain, monkeypatch, has_results):
    import json
    from app.gtm import journey, reachable
    from app.research import metered
    from app.research.prior_art import adapters
    from app.signals import research

    data = search_domain
    monkeypatch.setenv("FIRECRAWL_SELF_HOSTED_URL", "https://crawl.example")
    monkeypatch.setenv("FIRECRAWL_SELF_HOSTED_TOKEN", "self-key")
    monkeypatch.setattr(research.DuckDuckGoProvider, "search", lambda *args, **kwargs: pytest.fail("empty search replayed through keyless adapter"))
    searches, prompts, seeds = [], [], []
    variants = (["bicycle mechanics Ireland published contact", "cycle repair workshops Ireland published contact"]
        if data["business_model"] != "consumer" else ["caregiver support coach Australia contact", "family care educator Australia contact"])
    intent = "business" if data["business_model"] != "consumer" else "person"

    async def search(self, query, **kwargs):
        searches.append(query)
        if has_results and query in variants:
            return [{"url": "https://source.example/", "title": data["audience"], "snippet": data["problem"], "provider": "firecrawl"}]
        if has_results and query == "initial audience search":
            return [{"url": "https://unreadable.example/", "title": data["audience"], "snippet": data["problem"], "provider": "firecrawl"},
                {"url": "https://directory.example/list", "title": "Unrelated directory", "snippet": "General listings", "provider": "firecrawl"}]
        return []

    def synthesize(*args, **kwargs):
        prompts.append(kwargs)
        return kwargs["parse"](json.dumps({"queries": [{"query": query, "intent": intent} for query in variants]}))

    async def discovered(provider, hits, candidates, context, payload, **kwargs):
        seeds.extend(hits)
        return []

    async def hn(*args, **kwargs):
        return {"items": []}

    monkeypatch.setattr(research.HttpResearchProvider, "search", search)
    monkeypatch.setattr(metered, "synthesize", synthesize)
    monkeypatch.setattr(reachable, "discover", discovered)
    monkeypatch.setattr(journey, "_signal_page", lambda *args, **kwargs: pytest.fail("empty search must not read unrelated pages"))
    monkeypatch.setattr(adapters, "search_hn", hn)
    journey.update_icp(data["wid"], data["vid"], data["audience"], problem=data["problem"], recruitment_opt_in=True)
    payload = _leased_payload(data)
    initial = [{"query": "initial audience search", "intent": intent}, {"query": "initial workflow search", "intent": "customer_voice"}]
    journal = journey._journal(payload)
    journal.mutate(payload["journey_id"], payload, lambda row, doc, session: doc.update(discovery_plan=initial, expert_discovery_plan=[]))
    asyncio.run(journey._discover(data["audience"], problem=data["problem"], payload=payload))
    assert len(prompts) == 1 and prompts[0]["purpose"] == "gtm.discovery.plan"
    prompt = prompts[0]["messages"][0]["content"]
    assert "synonyms" in prompt and "adjacent roles" in prompt and "2" in prompt
    context = json.loads(prompts[0]["messages"][1]["content"])
    assert context["audience"] == data["audience"] and context["problem"] == data["problem"]
    assert {item["query"] for item in context["attempted_queries"]} == set(searches) - set(variants)
    assert all(item in context["attempted_queries"] for item in initial)
    assert all(searches.count(query) == 1 for query in variants)
    assert journal.document(payload["journey_id"])["broadened_discovery_plan"] == [{"query": query, "intent": intent} for query in variants]
    assert bool(seeds) is has_results


@pytest.mark.parametrize("audience", ["Maintenance scheduling for factories", "Weekend cooking classes for families"])
def test_posted_results_preserve_refinements_and_distinct_retries_without_duplicate_actions(monkeypatch, audience):
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    from app.chat import customers, milestones
    from app.gtm import journey

    retry = {"label": "Try the same search again", "action": "gtm.find", "payload": {"audience": audience}}
    refine = {"label": "Refine your audience", "action": "chat.reply", "text": f"Refine {audience}"}
    reply = {"kind": "text", "body_md": f"Your search for {audience} is ready.", "actions": [refine, retry],
             "citations": [{"source": "saved_search", "title": audience}]}
    snapshot = {"outreach_next_step": {"actions": [retry.copy()]}}
    posted = []
    monkeypatch.setattr(journey, "status", lambda *args, **kwargs: snapshot)
    monkeypatch.setattr(customers, "render", lambda *args, **kwargs: reply)
    monkeypatch.setattr(milestones, "post_milestone", lambda *args, **kwargs: posted.append(kwargs) or True)

    assert journey._post_results("workspace_saved", "venture_saved", "run_saved")
    assert posted[-1]["actions"] == [refine, retry]
    assert all(posted[-1][key] == reply[key] for key in ("body_md", "kind", "citations"))
    distinct = {**retry, "payload": {"audience": f"{audience} in one region"}}
    snapshot["outreach_next_step"]["actions"].append(distinct)
    assert journey._post_results("workspace_saved", "venture_saved", "run_next")
    assert posted[-1]["actions"] == [distinct, refine, retry]
    assert reply["actions"] == [refine, retry]


def test_reformulated_community_queries_over_sixteen_words_reach_broadened_search(search_domain, monkeypatch):
    import json
    from app.gtm import journey, reachable
    from app.gtm.discovery import Query
    from app.research import metered
    from app.research.prior_art import adapters
    from app.signals import research

    data = search_domain
    variants = ([
        "bicycle repair workshop owners in Ireland sharing practical scheduling challenges and advice with peers in public communities",
        "independent cycling mechanics in Ireland discussing appointment coordination problems and exchanging workflow tips with colleagues on public forums",
    ] if data["business_model"] != "consumer" else [
        "family caregivers in Australia sharing practical care coordination challenges and advice with peers in welcoming public communities",
        "relatives supporting adults in Australia discussing shared care planning problems and exchanging daily routines in welcoming public forums",
    ])
    assert [len(query.split()) for query in variants] == [17, 18]
    with pytest.raises(ValueError):
        Query(query=" ".join(["community"] * 25), intent="community")
    with pytest.raises(ValueError):
        Query(query="community OR forum", intent="community")
    monkeypatch.setenv("FIRECRAWL_SELF_HOSTED_URL", "https://crawl.example")
    monkeypatch.setenv("FIRECRAWL_SELF_HOSTED_TOKEN", "self-key")
    searches, prompts = [], []

    async def search(self, query, **kwargs):
        searches.append(query)
        return []

    def synthesize(*args, **kwargs):
        prompts.append(kwargs)
        return kwargs["parse"](json.dumps({"queries": [{"query": query, "intent": "community"} for query in variants]}))

    async def discovered(*args, **kwargs):
        return []

    async def hn(*args, **kwargs):
        return {"items": []}

    monkeypatch.setattr(research.HttpResearchProvider, "search", search)
    monkeypatch.setattr(research.DuckDuckGoProvider, "search", lambda *args, **kwargs: pytest.fail("No fallback provider"))
    monkeypatch.setattr(metered, "synthesize", synthesize)
    monkeypatch.setattr(reachable, "discover", discovered)
    monkeypatch.setattr(adapters, "search_hn", hn)
    journey.update_icp(data["wid"], data["vid"], data["audience"], problem=data["problem"], recruitment_opt_in=True)
    payload = _leased_payload(data)
    journal = journey._journal(payload)
    initial = [{"query": "initial audience search", "intent": "community"}, {"query": "initial workflow search", "intent": "customer_voice"}]
    journal.mutate(payload["journey_id"], payload, lambda row, doc, session: doc.update(discovery_plan=initial, expert_discovery_plan=[]))
    asyncio.run(journey._discover(data["audience"], problem=data["problem"], payload=payload))
    assert len(prompts) == 1
    assert all(searches.count(query) == 1 for query in variants)
    assert journal.document(payload["journey_id"])["broadened_discovery_plan"] == [
        {"query": query, "intent": "community"} for query in variants]


@pytest.mark.parametrize("key_field", ["discovered_for", "discovery_key"])
def test_older_research_completion_has_a_next_step_at_each_email_gate(search_domain, monkeypatch, key_field):
    import json
    from app.chat import customers
    from app.db.models import ChatMessage
    from app.gtm import journey

    data = search_domain
    icp = journey._icp(data["vid"])
    with data["factory"]() as session:
        row = journey._row(session, data["vid"], create=True)
        original = json.dumps({"state": "ready", "icp": icp, key_field: "older-saved-audience",
            "run_id": "older-run", "message": "Research finished. Nothing was sent."})
        row.provenance_json = original
        session.commit()
    view = journey.status(data["wid"], data["vid"], authorize=False)
    assert view["outreach_next_step"]["reason"] == "stale_journey"
    assert view["outreach_next_step"]["actions"][0]["payload"]["retry"] is True
    with data["factory"]() as session:
        assert journey._row(session, data["vid"]).provenance_json == original

    base = view | {"state": "ready", "error_code": None, "can_prepare": True, "processing_blocked_reason": None,
        "send_blocked_reason": None, "gmail": {"connected": True, "sender": "you@sender.example"},
        "daily_cap": 15, "sends_left_today": 15, "prospects": [], "reachable_leads": [], "industry_experts": [],
        "channels": [], "need_signals": [], "drafts": [], "batches": []}
    draft = {"doc_id": "saved-email", "contact_id": "saved-person", "state": "draft", "channel": "email", "review_state": "draft"}
    batch = {"batch_id": "saved-batch", "approved_count": 1, "cap": 5, "draft_ids": [draft["doc_id"]], "approval_digest": "saved-digest"}
    cases = [
        (base, "no_leads"),
        (base | {"drafts": [draft], "gmail": {"connected": False}, "send_blocked_reason": "gtm_gmail_required"}, "gmail_required"),
        (base | {"drafts": [draft]}, "approval_pending"),
        (base | {"drafts": [draft | {"review_state": "approved"}], "batches": [batch], "sends_left_today": 0}, "cap_reached"),
        (base | {"drafts": [draft | {"review_state": "approved"}], "batches": [batch]}, "approved_drafts"),
        (base | {"prospects": [{"id": "saved-person", "eligible": True, "email": "person@public.example"}]}, "draft_needed"),
        (base | {"prospects": [{"id": "saved-person", "blocked_reason": "casl_consent_required"}]}, "permission_required"),
        (base | {"drafts": [draft | {"state": "delivery_pending"}]}, "delivery_unconfirmed"),
        (base | {"drafts": [draft | {"review_state": "approved", "truth_state": "queued"}]}, "queued"),
        (base | {"drafts": [draft | {"review_state": "approved", "truth_state": "queued"}], "gmail": {"connected": False}}, "gmail_required"),
        (base | {"drafts": [draft], "send_blocked_reason": "gtm_sender_address_required"}, "gtm_sender_address_required"),
        (base | {"drafts": [draft | {"review_state": "approved", "truth_state": "queued"}], "send_blocked_reason": "gtm_sender_address_required"}, "gtm_sender_address_required"),
        (base | {"drafts": [draft | {"review_state": "approved", "truth_state": "queued"}], "batches": [batch], "send_blocked_reason": "send_paused"}, "send_paused"),
    ]
    for snapshot, reason in cases:
        step = journey._next_step(data["vid"], snapshot)
        assert step["reason"] == reason and step["actions"]
        assert "\u2014" not in step["body_md"]
        if reason == "cap_reached":
            card = step["actions"][0]["confirmation"]
            assert card["action"] == "autopilot.send_cap" and card["resulting_state"] == {"send_max": 30}
            assert "15 a day is recommended" in step["body_md"]
        if reason == "approved_drafts":
            assert step["actions"][0]["action"] == "gtm.status"
            assert not any(action.get("action") == "gtm.send" or action.get("confirmation") for action in step["actions"])
    monkeypatch.setattr(customers, "_library_section", lambda *args, **kwargs: ("", []))
    assert journey._post_results(data["wid"], data["vid"], "older-run")
    assert not journey._post_results(data["wid"], data["vid"], "older-run")
    with data["factory"]() as session:
        message = session.scalar(select(ChatMessage).where(ChatMessage.venture_id == data["vid"]))
        assert "Research finished" in message.body_md
        assert json.loads(message.actions_json)[0]["action"] == "gtm.find"
        assert not json.loads(message.actions_json)[0].get("confirmation")


def test_customer_search_read_and_start_failures_have_working_recovery(search_domain, monkeypatch):
    from app.chat import customers
    from app.gtm import journey

    data = search_domain
    from app.core.local_mode import is_local
    if is_local():
        from app.core.local_secrets import update_settings
        from secrets import token_urlsafe
        update_settings("smtp", {"host": "smtp.example.test", "port": 587, "user": "owner@example.test",
            "sender_name": "Your sender", "postal_address": "10 Test Street, Sample City"}, {"password": token_urlsafe(32)})
        update_settings("capabilities", {"send_email": True, "background_work": True}, {})
    view = journey.status(data["wid"], data["vid"], authorize=False)
    with monkeypatch.context() as patch:
        patch.setattr(customers, "_view", lambda vid: (_ for _ in ()).throw(RuntimeError("private database detail")))
        for reply in (asyncio.run(customers.find_reply(data["vid"])),
                      asyncio.run(customers.execute(data["vid"], "gtm.status", {}, data["actor"])),
                      asyncio.run(customers.execute(data["vid"], "gtm.status", {"contact_ids": ["saved-contact"]}, data["actor"])),
                      asyncio.run(customers.execute(data["vid"], "gtm.status", {"contact_id": "saved-contact", "linkedin_note": True}, data["actor"])),
                      asyncio.run(customers.execute(data["vid"], "gtm.automate", {}, data["actor"])),
                      *(asyncio.run(customers.run_command(data["vid"], name, None, "", data["actor"]))
                        for name in ("refine", "more", "search_again", "auto_search", "resume_search"))):
            _recovery_actions(reply)
            assert "could not be read" in reply["body_md"] and "private database detail" not in reply["body_md"]
            assert "search stopped" not in reply["body_md"]
            assert reply["actions"][0]["action"] == "gtm.status"
    with monkeypatch.context() as patch:
        patch.setattr(journey, "update_settings", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("private database detail")))
        reply = asyncio.run(customers.execute(data["vid"], "gtm.auto_search", {"auto_search": {"enabled": True}}, data["actor"]))
        _recovery_actions(reply)
        assert "could not be read" in reply["body_md"] and "private database detail" not in reply["body_md"]
        patch.setattr(customers, "_view", lambda vid: view | {"gmail": {"connected": True}})
        reply = asyncio.run(customers.execute(data["vid"], "gtm.automate", {"auto_search": {"enabled": True}}, data["actor"]))
        _recovery_actions(reply)
        assert "could not be read" in reply["body_md"] and "private database detail" not in reply["body_md"]
        patch.setattr(journey, "update_settings", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("gtm_invalid_auto_search")))
        reply = asyncio.run(customers.execute(data["vid"], "gtm.automate", {"auto_search": {}}, data["actor"]))
        _recovery_actions(reply)
        assert "positive contact target" in reply["body_md"]
    monkeypatch.setattr(journey, "prepare", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("gtm_invalid_input")))
    reply = asyncio.run(customers.execute(data["vid"], "gtm.find", {}, data["actor"]))
    _recovery_actions(reply)
    assert "Check your audience and problem" in reply["body_md"]
    card = reply["actions"][0]
    assert not card.get("confirmation") and card["payload"]["retry"] is True
    assert card["payload"].get("recruitment_opt_in", False) == (view["icp"]["mode"] == "consumer")
    monkeypatch.setattr(journey, "prepare", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("gtm_preparation_in_progress")))
    reply = asyncio.run(customers.execute(data["vid"], "gtm.find", {}, data["actor"]))
    _recovery_actions(reply)
    assert "earlier search is still running" in reply["body_md"] and reply["actions"][0]["action"] == "gtm.status"
    assert "search stopped" not in reply["body_md"]


def test_saved_customer_results_keep_recovery_and_requested_order(search_domain, monkeypatch):
    from app.chat import customers
    from app.gtm import journey
    from tests.unit.gtm.test_leads_to_drafts import _lead

    data = search_domain
    view = journey.status(data["wid"], data["vid"], authorize=False)
    saved = [_lead("saved-first", "First saved match", "first-saved.example"),
        _lead("saved-second", "Second saved match", "second-saved.example")]
    public = _lead("public-match", "Unrelated public match", "public-match.example")
    for state in ("queued", "failed"):
        monkeypatch.setattr(customers, "_view", lambda vid: view | {"state": state, "prospects": saved,
            "reachable_leads": [public], "error_code": "gtm_search_timed_out" if state == "failed" else None})
        reply = asyncio.run(customers.execute(data["vid"], "gtm.status",
            {"contact_ids": ["saved-second", "saved-first"]}, data["actor"]))
        assert "Your saved leads" in reply["body_md"] and "Unrelated public match" not in reply["body_md"]
        assert reply["body_md"].index("Second saved match") < reply["body_md"].index("First saved match")
        if state == "failed":
            _recovery_actions(reply)


def test_missing_customer_search_returns_starter_not_read_failure(search_domain, monkeypatch):
    from app.chat import customers
    from app.db.models import Job
    from app.gtm import journey

    data = search_domain
    with data["factory"]() as session:
        assert journey._row(session, data["vid"]) is None
        jobs_before = session.query(Job).count()
    view = customers._view(data["vid"])
    assert view["state"] == "idle" and not view["discovery_started"] and not view["error_code"]
    monkeypatch.setattr(customers, "_view", lambda vid: (_ for _ in ()).throw(ValueError("gtm_not_found")))
    reply = asyncio.run(customers.find_reply(data["vid"]))
    assert "book a first call" in reply["body_md"] and "could not be read" not in reply["body_md"]
    assert reply["kind"] == "text"
    assert reply["actions"] == [{"label": "Find customers", "action": "gtm.status", "style": "primary"}]
    with data["factory"]() as session:
        assert journey._row(session, data["vid"]) is None
        assert session.query(Job).count() == jobs_before


@pytest.mark.parametrize("has_library", [True, False])
def test_merged_library_results_keep_customer_search_recovery(search_domain, monkeypatch, has_library):
    from app.chat import customers
    from app.gtm import journey

    data = search_domain
    view = journey.status(data["wid"], data["vid"], authorize=False)
    consumer = journey._consumer(view["icp"])
    library = "Saved library matches for " + data["audience"]
    library_actions = [{"label": "Add these to my leads", "action": "gtm.library_claim", "payload": {"entry_ids": ["fixture-entry"]}},
        {"label": "Show more", "action": "gtm.library_more", "payload": {"offset": 5}}]
    monkeypatch.setattr(customers, "_library_section", lambda *args: (library, library_actions) if has_library else ("", []))
    for code, can_prepare in (("no_public_candidates", False), ("public_sources_unavailable", True),
                              ("public_sources_unavailable", False), (None, True), (None, False)):
        search_view = view | {"state": "ready" if code else "idle", "error_code": code,
            "can_prepare": can_prepare, "discovery_started": bool(code)}
        reply = customers.render(data["vid"], search_view)
        from app.core.local_mode import is_local
        if is_local() or consumer or not has_library:
            assert library not in reply["body_md"]
            assert not any(action["action"].startswith("gtm.library_") for action in reply["actions"])
        else:
            assert library in reply["body_md"]
            assert reply["actions"][:2] == library_actions
        if code:
            recovery = customers.empty_search_reply(search_view)
            assert reply["body_md"].endswith(recovery["body_md"])
            assert reply["actions"][-len(recovery["actions"]):] == recovery["actions"]
            if is_local() or consumer or not has_library:
                assert reply == recovery
        if code or ((consumer or not has_library) and not can_prepare):
            _recovery_actions(reply)
        elif can_prepare:
            assert any(action.get("action") == "gtm.find" and not action.get("confirmation") for action in reply["actions"])


def test_finished_search_explains_empty_and_inaccessible_sources(search_domain, monkeypatch):
    from app.chat import customers
    from app.gtm import journey, reachable
    from tests.unit.gtm.test_leads_to_drafts import _lead

    data = search_domain
    monkeypatch.setattr(journey, "_draft_ready", lambda *args: 0)
    for inaccessible, partial in ((False, False), (True, False), (True, True)):
        async def discover(*args, **kwargs):
            if inaccessible:
                payload = kwargs["payload"]
                journal = journey._journal(payload)
                consumer = journey._consumer(journal.document(payload["journey_id"])["icp"])
                journal.mutate(payload["journey_id"], payload,
                    lambda row, doc, session: doc.update(source_access="unavailable",
                        channels=[{"name": "Verified discussion", "url": "https://community.example/rules", "why_fits": data["audience"]}] if partial and consumer else []))
                if partial and not consumer:
                    reachable._publish(journal, payload, [_lead("verified-result", "Verified discussion", "verified.example")
                        | {"country": None, "quote": data["problem"]}])
            return []

        monkeypatch.setattr(journey, "_discover", discover)
        journey.update_icp(data["wid"], data["vid"], data["audience"], problem=data["problem"], recruitment_opt_in=True)
        payload = _leased_payload(data)
        from app.db.models import Job
        with data["factory"]() as session:
            session.get(Job, payload["_job_id"]).last_error = "gtm_enrichment_deferred"
            session.commit()
        asyncio.run(journey.run_prepare(payload))
        view = journey.status(data["wid"], data["vid"], authorize=False)
        reply = customers.render(data["vid"], view)
        if partial:
            assert view["state"] == "ready" and not view["error_code"]
            assert "Verified discussion" in reply["body_md"] and "search stopped" not in reply["body_md"]
        else:
            _recovery_actions(reply)
            assert view["error_code"] == ("public_sources_unavailable" if inaccessible else "no_public_candidates")
            assert view["state"] == "needs_input"
            assert ("could not be opened" if inaccessible else "nothing usable") in reply["body_md"]
        from app.core.jobs import complete_job
        with data["factory"]() as session:
            complete_job(session, payload["_job_id"], worker_id=payload["_worker_id"], attempt=payload["_job_attempt"])
            session.commit()


def test_provider_failure_keeps_partial_results_and_offers_recovery(search_domain, monkeypatch):
    from app.chat import customers
    from app.gtm import journey

    data = search_domain

    async def unavailable(*args, **kwargs):
        raise RuntimeError("provider token must stay private")

    monkeypatch.setattr(journey, "_discover", unavailable)
    journey.update_icp(data["wid"], data["vid"], data["audience"], problem=data["problem"], recruitment_opt_in=True)
    payload = _leased_payload(data)
    asyncio.run(journey.run_prepare(payload))
    view = journey.status(data["wid"], data["vid"], authorize=False)
    assert view["state"] == "needs_input" and view["error_code"] == "public_search_unavailable"
    view["channels"] = [{"name": "Saved local discussion", "url": "https://community.example/rules", "why_fits": data["audience"]}]
    view["icp"]["mode"] = "consumer"
    monkeypatch.setattr(journey, "_consumer", lambda *args: True)
    reply = customers.render(data["vid"], view)
    _recovery_actions(reply)
    from app.core.local_mode import is_local
    if is_local():
        assert "could not finish" in reply["body_md"] and "Keys and connections" in reply["body_md"]
    else:
        assert "could not respond" in reply["body_md"]
    assert "Saved local discussion" in reply["body_md"] and "provider token" not in reply["body_md"]


@pytest.mark.parametrize("job_state", ["pending", "leased", "retry_wait", "succeeded", "missing", "finished_during_expiry"])
def test_stale_customer_search_fails_durably_and_fences_late_workers(search_domain, monkeypatch, job_state):
    from app.chat import customers
    from app.core.jobs import FatalError
    from app.db.models import ChatMessage, Job
    from app.gtm import journey
    from core.clock import utcnow

    data = search_domain
    journey.update_icp(data["wid"], data["vid"], data["audience"], problem=data["problem"], recruitment_opt_in=True)
    payload = _leased_payload(data)
    with data["factory"]() as session:
        row = journey._row(session, data["vid"])
        saved = journey._json(row.provenance_json)
        saved.update(state="failed" if job_state == "leased" else "running")
        saved["_runtime"].update(started_at=(utcnow() - timedelta(minutes=31)).isoformat(),
                                 operations={"held": {"state": "uncertain", "usage": {"reservation_id": "keep-receipt"}}})
        job = session.get(Job, payload["_job_id"])
        if job_state == "missing":
            session.delete(job)
        else:
            job.status = "leased" if job_state == "finished_during_expiry" else job_state
        journey._store(session, row, saved)
        session.commit()
    if job_state == "finished_during_expiry":
        from app.credits import _txn
        from app.db import session as db_session
        original_serialize = _txn.serialize_writes
        finished = False
        def finish_before_lock(session, **kwargs):
            nonlocal finished
            if not finished:
                with data["factory"]() as worker:
                    worker.get(Job, payload["_job_id"]).status = "succeeded"
                    worker.commit()
                finished = True
            return original_serialize(session, **kwargs)
        monkeypatch.setattr(_txn, "serialize_writes", finish_before_lock)
        monkeypatch.setattr(db_session, "serialize_writes", finish_before_lock)
    view = journey.status(data["wid"], data["vid"], authorize=False)
    assert view["state"] == "failed" and view["research_progress"]["state"] == "failed"
    assert view["error_code"] == ("gtm_search_worker_missing" if job_state == "missing" else
                                  "gtm_search_result_missing" if job_state in {"succeeded", "finished_during_expiry"} else "gtm_search_timed_out")
    _recovery_actions(customers.render(data["vid"], view))
    with data["factory"]() as session:
        saved = journey._json(journey._row(session, data["vid"]).provenance_json)
        assert saved["_runtime"]["operations"]["held"]["usage"]["reservation_id"] == "keep-receipt"
        assert saved["state"] == "failed"
        if job_state == "finished_during_expiry":
            assert session.get(Job, payload["_job_id"]).status == "succeeded"
        assert len(list(session.scalars(select(ChatMessage).where(ChatMessage.kind == "gtm_search_failed")))) == 1
        with pytest.raises(FatalError):
            journey._prepare_fence(session, payload)
    journey.status(data["wid"], data["vid"], authorize=False)
    retry = journey.prepare(data["wid"], data["vid"], retry=True, recruitment_opt_in=True)
    assert retry["state"] == "queued" and retry["job_id"] != payload["_job_id"]
    current = _leased_payload(data)
    with data["factory"]() as session:
        current_data = journey._json(journey._row(session, data["vid"]).provenance_json)
    monkeypatch.setattr(journey, "_prepare_fence", lambda session, payload: (None, current_data))
    original_timeout = asyncio.timeout
    monkeypatch.setattr(asyncio, "timeout", lambda seconds: original_timeout(0.01))

    async def stuck(payload):
        await asyncio.Event().wait()

    monkeypatch.setattr(journey, "_run_prepare", stuck)
    with pytest.raises(FatalError, match="gtm_search_timed_out"):
        asyncio.run(asyncio.wait_for(journey.run_prepare(current), 30))
    async def provider_timeout(payload):
        raise TimeoutError("provider did not respond")

    monkeypatch.setattr(journey, "_run_prepare", provider_timeout)
    with pytest.raises(TimeoutError, match="provider did not respond"):
        asyncio.run(journey.run_prepare(current))


def test_enrichment_credit_failure_is_not_a_silent_empty_result(search_domain, monkeypatch):
    from app.core.jobs import FatalError
    from app.credits.errors import InsufficientCredits
    from app.gtm import enrich, journey

    data = search_domain
    original_prepare = journey._run_prepare
    monkeypatch.setenv("GTM_ENRICH_ENABLED", "1")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "synthetic")
    monkeypatch.setattr(enrich, "CONCURRENCY", 1)
    progress = {"done": {"saved.example": {"name": "Saved evidence"}}, "pages": {"saved.example": [["https://saved.example", "evidence"]]}}

    async def no_credits(*args, **kwargs):
        raise InsufficientCredits(data["account"], 0, 100)

    monkeypatch.setattr(enrich, "enrich_site", no_credits)
    with pytest.raises(InsufficientCredits):
        asyncio.run(enrich.enrich_seeds([{"website": "https://unread.example", "audience": data["audience"]}],
            data["wid"], data["vid"], progress=progress, context={"audience": data["audience"], "problem": data["problem"]}))
    assert progress["done"]["saved.example"]["name"] == "Saved evidence"
    assert not progress["failed"]
    journey.update_icp(data["wid"], data["vid"], data["audience"], problem=data["problem"], recruitment_opt_in=True)
    payload = _leased_payload(data)
    monkeypatch.setattr(journey, "_run_prepare", no_credits)
    with pytest.raises(FatalError, match="insufficient_credits"):
        asyncio.run(journey.run_prepare(payload))
    monkeypatch.setattr(journey, "_run_prepare", original_prepare)
    monkeypatch.setattr(journey, "_discover", no_credits)
    monkeypatch.setattr(journey, "_draft_ready", lambda *args: pytest.fail("No paid drafting after a credit stop"))
    asyncio.run(journey.run_prepare(payload))
    view = journey.status(data["wid"], data["vid"], authorize=False)
    assert view["state"] == "needs_input" and view["error_code"] == "insufficient_credits"
    from app.chat import customers
    _recovery_actions(customers.render(data["vid"], view))


@pytest.mark.parametrize("known", ["neither", "audience", "problem", "manual", "declined"])
def test_no_website_intake_asks_only_missing_fields_then_starts(search_domain, monkeypatch, known):
    from app.chat import customers
    from app.db.models import Job, Venture
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.gtm import journey
    from app.state import MEM

    data = search_domain
    with data["factory"]() as session:
        idea = session.get(IdeaSession, "search_idea")
        idea.state, idea.current_revision_id, idea.confirmed_revision_id = "abandoned", None, None
        session.get(Venture, data["vid"]).raw_idea = data["problem"] if known == "problem" else ""
        session.commit()
    base = journey._icp(data["vid"]) | {"who": data["audience"] if known == "audience" else "",
        "problem": data["problem"] if known == "problem" else "", "business_model": data["business_model"],
        "mode": "consumer" if data["business_model"] == "consumer" else "company", "needs_input": True, "confirmed": False}
    monkeypatch.setattr(journey, "_icp", lambda vid: dict(base))
    monkeypatch.setattr(customers, "icp_reply", lambda *args, **kwargs: pytest.fail("No suggestions or extra questions before missing input"))
    MEM[customers._SUGGESTED.format(data["vid"])] = {data["audience"].lower(): data["problem"]}
    request = "Find 7 customers." if known == "neither" else ""
    first = (customers._describe_reply(data["vid"]) if known == "manual" else
             asyncio.run(customers.find_reply(data["vid"], request)))
    assert "website" not in first["body_md"].lower()
    assert "credit" not in first["body_md"].lower()
    assert MEM[customers._ASKED.format(data["vid"])]["start_search"] == (known != "manual")
    if known == "declined":
        asyncio.run(customers.find_reply(data["vid"], "not now"))
        assert not MEM.get(customers._ASKED.format(data["vid"]))
        assert customers.command(data["vid"], f"My customers are {data['audience']}.") is None
    if known == "audience":
        assert "What problem do they have" in first["body_md"] and "who are you trying to reach?" not in first["body_md"].lower()
        assert not first["actions"]
        last = asyncio.run(customers.run_command(data["vid"], "audience", None, data["problem"], data["actor"]))
    else:
        assert "Who are you trying to reach?" in first["body_md"] and "What problem" not in first["body_md"]
        last = asyncio.run(customers.run_command(data["vid"], "audience", None, data["audience"], data["actor"]))
        if known in {"neither", "manual", "declined"}:
            assert "What problem do they have" in last["body_md"] and "who are you trying to reach?" not in last["body_md"].lower()
            if known in {"manual", "declined"}:
                assert "ready to start" in last["body_md"] and "credit" not in last["body_md"].lower()
            assert MEM[customers._ASKED.format(data["vid"])]["field"] == "problem"
            with data["factory"]() as session:
                assert not list(session.scalars(select(Job)))
            last = asyncio.run(customers.run_command(data["vid"], "audience", None, data["problem"], data["actor"]))
    view = journey.status(data["wid"], data["vid"], authorize=False)
    if known in {"manual", "declined"}:
        assert view["state"] == "idle" and last["kind"] == "text"
        assert last["actions"][0]["action"] == "gtm.find" and not last["actions"][0].get("confirmation")
        with data["factory"]() as session:
            assert not list(session.scalars(select(Job)))
    else:
        assert view["state"] == "queued" and last["kind"] == "gtm_search_progress"
        assert view["discovery_note"] == request
    assert view["icp"]["who"] == data["audience"] and view["icp"]["problem"] == data["problem"]
    assert view["recruitment_opt_in"] == (data["business_model"] == "consumer" and known not in {"manual", "declined"})
    assert "updates here automatically" not in last["body_md"]


def test_automatic_search_empty_failure_and_credit_pause_offer_another_way(search_domain, monkeypatch):
    from types import SimpleNamespace
    from app.chat import customers, milestones
    from app.db.models import CreditAccount
    from app.gtm import journey
    from app.core.local_mode import is_local
    if is_local():
        from app.core.local_secrets import update_settings
        update_settings("capabilities", {"background_work": True}, {})

    data = search_domain
    for reply in (asyncio.run(customers.run_command(data["vid"], "auto_search", SimpleNamespace(group=lambda name: "0"), "", data["actor"])),
                  asyncio.run(customers.execute(data["vid"], "gtm.auto_search", {}, data["actor"]))):
        _recovery_actions(reply)
        assert "positive contact target" in reply["body_md"]
        assert "credit" not in reply["body_md"].lower()
    posted = []
    monkeypatch.setattr(milestones, "post_milestone", lambda vid, **kwargs: posted.append(kwargs) or True)
    journey.update_icp(data["wid"], data["vid"], data["audience"], problem=data["problem"], start=False)
    with data["factory"]() as session:
        row = journey._row(session, data["vid"])
        original = journey._json(row.provenance_json)
        for successful, code, source_access in ((False, "public_search_unavailable", None), (False, None, "unavailable"), (True, None, None)):
            saved = original | {"run_id": "automatic-test", "error_code": code, "source_access": source_access,
                "auto_search": {"enabled": False, "cycle_id": "automatic-cycle"},
                "_runtime": {"auto_run": {"seen_lead_urls": [], "draft_ids": [], "requests": {}}}}
            journey._auto_search_results(session, row, saved, [], successful=successful)
            _recovery_actions(posted[-1])
            if is_local() and code == "public_search_unavailable":
                assert "could not finish" in posted[-1]["body_md"] and "Keys and connections" in posted[-1]["body_md"]
            else:
                assert ("nothing usable" if successful else "could not be opened" if source_access else "could not respond") in posted[-1]["body_md"]
            assert "prepared 0 email drafts" in posted[-1]["body_md"] and "queued" not in posted[-1]["body_md"]
            assert "credit" not in posted[-1]["body_md"].lower()
        for reason in ("credits_low", "empty_runs", "credits_unavailable", "insufficient_credits", "gtm_auto_search_credit_ceiling"):
            saved = original | {"auto_search": {"enabled": True, "cycle_id": "paused-cycle", "runs": 2}}
            journey._stop_auto_search(session, row, saved, 0, reason)
            assert any(action.get("action") == "recovery.alternative" for action in posted[-1]["actions"])
            action = posted[-1]["actions"][0]
            if is_local():
                assert action["action"] == "gtm.auto_search" and not action.get("confirmation")
            else:
                assert action["confirmation"]["action"] == "gtm.auto_search"
            assert saved["auto_search"]["enabled"] is False
            assert "credit" not in posted[-1]["body_md"].lower()
        if data["account"] is None:
            assert session.scalar(select(CreditAccount.id)) is None
        else:
            session.get(CreditAccount, data["account"]).status = "paused"
            saved = original | {"auto_search": {"enabled": True, "cycle_id": "account-cycle"}}
            assert journey._auto_search_due(session, row, saved, data["wid"], data["vid"]) is False
            assert saved["auto_search"]["enabled"] is False and saved["auto_search"]["stop_reason"] == "credits_unavailable"
            assert any(action.get("action") == "recovery.alternative" for action in posted[-1]["actions"])


def test_unreadable_search_completion_posts_recovery_instead_of_silence(search_domain, monkeypatch):
    from app.chat import customers, milestones

    data = search_domain
    posted = []
    monkeypatch.setattr(customers, "_view", lambda vid: (_ for _ in ()).throw(RuntimeError("private storage detail")))
    monkeypatch.setattr(milestones, "post_milestone", lambda vid, **kwargs: posted.append(kwargs) or True)
    assert customers.post_results(data["vid"], "finished-run") is False
    assert len(posted) == 1
    _recovery_actions(posted[0])
    assert "could not be read" in posted[0]["body_md"] and "private storage detail" not in posted[0]["body_md"]


def test_reachable_batches_do_not_block_search_cancellation(search_domain):
    from app.gtm import reachable

    async def run():
        started = asyncio.Event()

        async def blocked():
            assert search_domain["audience"]
            started.set()
            await asyncio.Event().wait()

        work = asyncio.create_task(blocked())

        async def consume():
            async for batch in reachable._finished([work]):
                pytest.fail("A blocked source cannot publish a batch")

        consumer = asyncio.create_task(consume())
        await started.wait()
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(consumer, 30)
        assert work.cancelled()

    asyncio.run(run())


def test_connecting_mail_does_not_promise_unconditional_first_drafts(search_domain):
    from app.chat import customers
    from app.db.models import CreditAccount
    from app.gtm import journey

    data = search_domain
    view = journey.status(data["wid"], data["vid"], authorize=False)
    view["reachable_leads"] = [{"id": "published-contact", "name": "Public contact", "company": "Public contact",
        "source_url": "https://contact.example", "kind": "business", "judging_status": "judged",
        "why": data["audience"], "draft": f"Hi, could we talk about your experience with {data['problem']}?",
        "connection_note": "Could we talk about your experience?",
        "routes": [{"kind": "email", "email": "hello@contact.example", "source_url": "https://contact.example"}]}]
    reply = customers.render(data["vid"], view)
    from app.core.local_mode import is_local
    assert ("Settings > Keys and connections > Your mail account" if is_local() else "Connect your Gmail to send any drafts you approve") in reply["body_md"]
    if is_local():
        assert any(action.get("href") == "/settings/local#smtp-title" for action in reply["actions"])
        assert "Gmail" not in reply["body_md"]
    assert "Drafts still depend on fit and permission" in reply["body_md"]
    assert "credit" not in reply["body_md"].lower()
    assert "I will write each one" not in reply["body_md"]
    with data["factory"]() as session:
        account = session.get(CreditAccount, data["account"])
        if data["account"] is None:
            assert session.scalar(select(CreditAccount.id)) is None
        else:
            assert account.spent_micro == account.reserved_micro == 0


def test_unavailable_customer_suggestions_offer_retry_and_manual_help(search_domain):
    from app.chat import customers

    reply = asyncio.run(customers.icp_reply(search_domain["vid"]))
    assert "I couldn't prepare customer group suggestions." in reply["body_md"]
    assert reply["actions"][0]["label"] == "Retry" and reply["actions"][0]["action"] == "chat.reply"
    assert any(action.get("action") == "recovery.alternative" for action in reply["actions"])


def test_local_free_search_replays_without_hosted_wallet(local_search_domain):
    from app.db.models import CreditAccount, CreditReservation
    from app.gtm import journey

    data = local_search_domain
    journey.update_icp(data["wid"], data["vid"], data["audience"], problem=data["problem"], recruitment_opt_in=True)
    payload = _leased_payload(data)
    calls = []
    expected = [{"url": "https://contact.example", "title": data["audience"]}]

    async def search():
        calls.append(data["audience"])
        return expected

    async def run():
        for _ in range(2):
            assert await journey._signal_call(payload, "signal_search", data["audience"], search,
                meter="firecrawl_search") == expected

    asyncio.run(run())
    assert calls == [data["audience"]]
    journal = journey._journal(payload)
    key = journey._signal_key(journal.document(payload["journey_id"])["_runtime"], "signal_search", data["audience"])
    receipt = journal.operation(payload["journey_id"], key)
    assert receipt["state"] == "completed" and receipt["response"] == expected and receipt["usage"] == {}
    with data["factory"]() as session:
        assert session.scalar(select(CreditAccount.id)) is None
        assert session.scalar(select(CreditReservation.id)) is None


def test_local_receipt_writers_do_not_overwrite_each_other(local_search_domain):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from app.gtm import journey

    data = local_search_domain
    journey.update_icp(data["wid"], data["vid"], data["audience"], problem=data["problem"], recruitment_opt_in=True)
    payload = _leased_payload(data)
    journal = journey._journal(payload)
    first_entered, second_entered, release = Event(), Event(), Event()

    def write(key, entered):
        def update(row, doc, session):
            entered.set()
            if key == "first":
                assert release.wait(5)
            doc["_runtime"]["operations"][key] = {"state": "completed", "response": data["audience"],
                "attempt": 1, "usage": {}}
        journal.mutate(payload["journey_id"], payload, update)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(write, "first", first_entered)
        try:
            assert first_entered.wait(5)
            second = executor.submit(write, "second", second_entered)
            assert not second_entered.wait(0.1)
        finally:
            release.set()
        first.result(timeout=5)
        second.result(timeout=5)
    saved = journal.document(payload["journey_id"])["_runtime"]["operations"]
    assert {"first", "second"} <= saved.keys()


def test_generic_customer_request_keeps_your_saved_audience(local_search_domain):
    from app.chat.customers import _search_audience
    from app.gtm.discovery import request_audience, search_note

    for qualifier in ("potential", "prospective", "suitable"):
        request = f"Find {qualifier} customers"
        assert search_note(request, local_search_domain["audience"]) == ""
        assert request_audience(request) == ""
    assert local_search_domain["audience"] in request_audience("Find " + local_search_domain["audience"])
    assert _search_audience({"icp": {"who": local_search_domain["audience"]},
        "search_request": f"Find customers for {local_search_domain['problem']}"}) == local_search_domain["audience"]


def test_local_judged_non_email_contacts_are_saved_once(local_search_domain):
    from app.db.models import GtmContact
    from app.gtm import journey, leads, reachable
    from tests.unit.gtm.test_leads_to_drafts import _lead

    data = local_search_domain
    journey.update_icp(data["wid"], data["vid"], data["audience"], problem=data["problem"], recruitment_opt_in=True)
    payload = _leased_payload(data)
    journal = journey._journal(payload)
    consumer = data["business_model"] == "consumer"
    results = []
    for index, name in enumerate(("Alex Stone", "Sam Clarke", "Taylor Reed")):
        result = _lead(str(index), name, f"source{index}.example")
        result.update(kind="person" if consumer else "business", company="" if consumer else name,
            country="AU" if consumer else "IE", quote=data["audience"], role="Caregiver" if consumer else "", signals=[])
        result["routes"] = [{"kind": "website", "url": result["source_url"],
            "source_url": result["source_url"], "quote": data["audience"]}]
        results.append(result)
    reachable._publish(journal, payload, results, final=True)
    reachable._publish(journal, payload, results, final=True)
    with data["factory"]() as session:
        contacts = list(session.scalars(select(GtmContact).where(GtmContact.workspace_id == data["wid"])))
        assert len(contacts) == 3 and all(contact.email is None and contact.lawful_basis == "none" for contact in contacts)
    saved = leads.saved_leads(data["wid"], data["vid"])["items"]
    contacts = [item for item in saved if item["contact_id"]]
    assert len(contacts) == 3 and all(item["source_url"] and not item["can_draft"] for item in contacts)


def test_local_parallel_budget_does_not_discard_queued_sources(local_search_domain, monkeypatch):
    from app.gtm import enrich, journey

    monkeypatch.setattr(enrich, "CONCURRENCY", 1)
    monkeypatch.setattr(enrich, "TOTAL_TIMEOUT", 0.001)
    found = []

    async def source():
        await asyncio.sleep(0.02)
        found.append(local_search_domain["audience"])
        return local_search_domain["audience"]

    assert asyncio.run(journey._parallel([source, source, source])) == [local_search_domain["audience"]] * 3
    assert found == [local_search_domain["audience"]] * 3


@pytest.mark.parametrize("stage", ["parallel", "enrichment"])
@pytest.mark.parametrize("error_code", ["discovery_invalid_output", "gtm_call_budget_exhausted", "discovery_boundary_blocked"])
def test_local_invalid_source_keeps_other_results_but_not_boundary_failures(local_search_domain, monkeypatch, stage, error_code):
    from app.core.jobs import FatalError
    from app.gtm import enrich, journey

    data = local_search_domain
    source = "https://source.example/"
    quote = data["audience"] + ". " + data["problem"]
    contact = {"source_url": source, "quote": quote, "email": "hello@source.example"}

    async def valid():
        return {"contacts": [contact]}

    async def invalid():
        raise FatalError(error_code)

    async def read(provider, website, *args, **kwargs):
        return await (valid() if website == source else invalid())

    monkeypatch.setattr(enrich, "enrich_site", read)
    monkeypatch.setattr(enrich, "guessed_routes", lambda *args, **kwargs: [])
    progress = {}
    callback = journey._parallel([valid, invalid]) if stage == "parallel" else enrich.enrich_seeds(
        [{"website": source}, {"website": "https://invalid.example/"}], data["wid"], data["vid"], progress=progress)
    if error_code == "discovery_boundary_blocked":
        with pytest.raises(FatalError, match=error_code):
            asyncio.run(callback)
        return
    result = asyncio.run(callback)
    rows = result if stage == "parallel" else list(result.values())
    assert len(rows) == 1 and rows[0]["contacts"] == [contact]
    if stage == "enrichment":
        assert progress["failed"] == ["invalid.example"]


def test_local_source_budget_keeps_previously_read_contact_evidence(local_search_domain, monkeypatch):
    from app.core.jobs import FatalError
    from app.gtm import enrich

    data = local_search_domain
    pages = [("https://source.example/", f'<h1>{data["audience"]}</h1><p>{data["problem"]}</p>'
        '<p>Contact hello@source.example</p><a href="/contact">Contact</a>')]

    async def limited(url):
        raise FatalError("gtm_call_budget_exhausted")

    result = asyncio.run(enrich.enrich_site(None, pages[0][0], data["wid"], data["vid"], pages=pages, scrape=limited))
    assert result["contacts"][0]["email"] == "hello@source.example"
    failure, = result["read_failures"]
    assert (failure["url"], failure["reason"]) == ("https://source.example/contact", "search limit reached")


def test_local_search_failure_names_the_next_step(local_search_domain):
    from app.chat.customers import _search_failure

    view = {"icp": {"who": local_search_domain["audience"]}}
    reply = _search_failure(view, "public_search_unavailable")
    assert "choose Tavily or Exa, and save your Search API key" in reply["body_md"]
    assert "Settings > Keys and connections > Your search and page reader" in reply["body_md"]
    assert "AI service" not in reply["body_md"]
    reply = _search_failure(view, "public_sources_unavailable")
    assert "Retry in Find customers" in reply["body_md"] and "Firecrawl" not in reply["body_md"]


def test_local_saved_audience_does_not_need_model_confirmation(local_search_domain):
    from app.chat.customers import _unchecked_audience

    view = {"can_prepare": True, "icp": {"who": local_search_domain["audience"], "chat_confirmed": False,
        "known_checks": {"audience": {"status": "known", "source": "Your clarification"}}}}
    assert not _unchecked_audience(local_search_domain["vid"], view)
    view["icp"]["known_checks"]["audience"]["status"] = "missing"
    assert _unchecked_audience(local_search_domain["vid"], view)


def test_local_chat_starts_search_after_plain_audience_answer(local_search_domain):
    from app.chat.execute import find_customers_reply
    from app.chat.router import handle_message
    from app.chat.store import append_message
    from app.db.models import Venture
    from app.db.supymem_models import IdeaSession
    from app.gtm import journey

    data = local_search_domain
    with data["factory"]() as session:
        session.get(Venture, data["vid"]).raw_idea = data["problem"]
        idea = session.get(IdeaSession, "search_idea")
        idea.state, idea.current_revision_id, idea.confirmed_revision_id = "abandoned", None, None
        session.commit()
    first = asyncio.run(find_customers_reply(data["vid"], "Find customers"))
    assert "Who are you trying to reach?" in first["body_md"]
    append_message(data["vid"], role="assistant", **first)
    append_message(data["vid"], role="assistant", kind="next_moves", body_md="Review your next step.")
    answer = asyncio.run(handle_message(data["vid"], data["audience"], actor_subject=data["actor"]))
    view = journey.status(data["wid"], data["vid"], authorize=False)
    assert answer["kind"] == "gtm_search_progress", answer["body_md"]
    assert view["state"] == "queued" and view["job_id"]
    assert view["icp"]["who"] == data["audience"] and view["icp"]["chat_confirmed"]


@pytest.mark.parametrize("choose_suggestion", [True, False])
def test_local_chat_starts_requested_search_after_audience_suggestions(local_search_domain, monkeypatch, choose_suggestion):
    from app.chat import customers
    from app.chat.router import handle_message
    from app.core import local_mode
    from app.db.models import Job, Venture
    from app.db.supymem_models import IdeaSession
    from app.gtm import journey

    data = local_search_domain
    consumer = data["business_model"] == "consumer"
    audience = "Families managing shared care calendars in Australia" if consumer else data["audience"]
    with data["factory"]() as session:
        venture = session.get(Venture, data["vid"])
        venture.name = data["problem"]
        venture.raw_idea = data["problem"]
        idea = session.get(IdeaSession, "search_idea")
        idea.state, idea.current_revision_id, idea.confirmed_revision_id = "abandoned", None, None
        session.commit()

    async def suggest(*args):
        return False, [dict(who=group, problem=data["problem"], why="They have this problem", where="Public groups", reach="Easy")
                       for group in (audience, "Local organizers", "Volunteer coordinators")]

    monkeypatch.setattr(local_mode, "is_offline", lambda: False)
    monkeypatch.setattr(customers, "_suggest", suggest)

    def chat(text):
        return asyncio.run(handle_message(data["vid"], text, actor_subject=data["actor"]))

    question = chat(f"Find customers for {data['problem']}")
    unsure = next(action for action in question["actions"] if action["label"] == "Not sure, suggest some")
    suggestions = chat(unsure["text"])
    assert len(suggestions["actions"]) == 4
    assert not journey.status(data["wid"], data["vid"], authorize=False)["job_id"]
    if choose_suggestion:
        reply = chat(suggestions["actions"][0]["text"])
    else:
        chat(suggestions["actions"][-1]["text"])
        reply = chat(audience)

    view = journey.status(data["wid"], data["vid"], authorize=False)
    assert reply["kind"] == "gtm_search_progress", reply["body_md"]
    assert reply["gtm_search_progress"]["job_id"] == view["job_id"]
    assert "next_moves" not in reply and "I recommend:" not in reply["body_md"]
    assert view["state"] == "queued" and view["icp"]["who"] == audience
    assert view["icp"]["mode"] == ("consumer" if consumer else "company")
    assert view["recruitment_opt_in"] == consumer
    with data["factory"]() as session:
        assert list(session.scalars(select(Job.id))) == [view["job_id"]]


@pytest.mark.parametrize("needs_choice", [False, True])
def test_local_chat_prefills_audience_from_confirmed_idea(local_search_domain, monkeypatch, needs_choice):
    from app.chat import customers
    from app.chat.router import handle_message
    from app.db.models import Venture
    from app.gtm import journey
    from app.ventures import known

    data = local_search_domain
    with data["factory"]() as session:
        venture = session.get(Venture, data["vid"])
        venture.name = "Spoke Calendar" if data["business_model"] == "b2b" else "Care Calendar"
        venture.raw_idea = f"{data['problem']}. Especially relevant to {data['audience']}."
        request = f"Find customers for {venture.name}"
        session.commit()
    if needs_choice:
        original = known.known

        def inferred(venture, field, **kwargs):
            result = original(venture, field, **kwargs)
            return {**result, "status": "confirm"} if field == "audience" and result["status"] == "known" else result

        monkeypatch.setattr(known, "known", inferred)
    monkeypatch.setattr(customers, "icp_reply", lambda *args, **kwargs: pytest.fail("The confirmed idea already has an audience"))
    reply = asyncio.run(handle_message(data["vid"], request, actor_subject=data["actor"]))
    if needs_choice:
        assert "Who are you trying to reach" not in reply["body_md"]
        assert not journey.status(data["wid"], data["vid"], authorize=False)["job_id"]
        action, = reply["actions"]
        assert action["action"] == "chat.reply" and action["label"] == f"Use {data['audience']}"
        reply = asyncio.run(handle_message(data["vid"], action["text"], actor_subject=data["actor"]))
    view = journey.status(data["wid"], data["vid"], authorize=False)
    assert reply["kind"] == "gtm_search_progress", reply["body_md"]
    assert view["state"] == "queued" and view["icp"]["who"] == data["audience"]
    assert not view["discovery_note"]


def test_local_customer_search_prefers_the_stated_buyer(local_search_domain):
    import json
    from app.chat.router import handle_message
    from app.db.models import Venture
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.gtm import journey

    data = local_search_domain
    product_target = "hydraulic pumps" if data["business_model"] == "b2b" else "music lessons"
    with data["factory"]() as session:
        venture = session.get(Venture, data["vid"])
        venture.raw_idea = (f"An app for {product_target}, sold as a subscription to {data['audience']}. "
            f"Problem: {data['problem']}.")
        request = f"Find customers for {venture.name}"
        revision = session.get(IdeaRevision, "search_revision")
        session.add(IdeaRevision(id="inferred_search_revision", session_id=revision.session_id,
            workspace_id=revision.workspace_id, venture_id=revision.venture_id, rev=revision.rev + 1,
            source="agent", summary=venture.raw_idea,
            structured=json.dumps(json.loads(revision.structured) | {"customer": product_target}),
            created_at=revision.created_at))
        session.flush()
        idea = session.get(IdeaSession, revision.session_id)
        idea.current_revision_id = idea.confirmed_revision_id = "inferred_search_revision"
        session.commit()
    reply = asyncio.run(handle_message(data["vid"], request, actor_subject=data["actor"]))
    view = journey.status(data["wid"], data["vid"], authorize=False)
    assert view["icp"]["who"] == data["audience"]
    assert reply["kind"] == "gtm_search_progress" and view["state"] == "queued"


def test_local_search_card_does_not_invent_person_only_instructions(local_search_domain):
    from app.chat.customers import _search_card

    data = local_search_domain
    view = {"icp": {"who": data["audience"], "mode": "consumer" if data["business_model"] == "consumer" else "company"}}
    assert "note" not in _search_card(view)["payload"]
    view["discovery_note"] = f'Find {data["audience"]} to talk to.'
    assert "note" not in _search_card(view, retry=True)["payload"]
    requested = f'Find 5 {data["audience"]}'
    view["search_request"] = requested
    assert _search_card(view)["payload"]["note"] == requested


def test_local_expert_plan_preserves_the_model_role(local_search_domain, monkeypatch):
    import json
    from app.gtm.discovery import Plan
    from app.gtm import journey
    from app.research import metered

    data = local_search_domain
    role = data["audience"] + " advisor"
    plan = Plan.model_validate({"queries": [
        {"query": role + " contact", "intent": "person", "expert_role": role},
        {"query": data["audience"] + " community", "intent": "community"},
    ]}).model_dump()
    assert plan["queries"][0]["expert_role"] == role
    assert "expert_role" not in plan["queries"][1]
    journey.update_icp(data["wid"], data["vid"], data["audience"], problem=data["problem"], recruitment_opt_in=True)
    payload = _leased_payload(data)

    def synthesize(*args, **kwargs):
        assert "expert_role" in kwargs["messages"][0]["content"]
        return kwargs["parse"](json.dumps(plan))

    monkeypatch.setattr(metered, "synthesize", synthesize)
    result = asyncio.run(journey._discovery_model(payload, "plan", {"audience": data["audience"], "problem": data["problem"]}))
    assert result["queries"][0]["expert_role"] == role


def test_local_expert_reader_keeps_your_scope_and_role(local_search_domain, monkeypatch):
    from types import SimpleNamespace
    from app.gtm import enrich, journey, reachable

    data = local_search_domain
    journey.update_icp(data["wid"], data["vid"], data["audience"], problem=data["problem"], recruitment_opt_in=True)
    payload = _leased_payload(data)
    captured = []
    role = data["audience"] + " advisor"

    async def enrich_site(*args, **kwargs):
        captured.append(kwargs["context"])
        return {"quality_rejected": True}

    monkeypatch.setattr(enrich, "enrich_site", enrich_site)
    context = {"mode": "B2B" if data["business_model"] == "b2b" else "B2C", "audience": data["audience"],
        "problem": data["problem"], "competitors": [], "offer": ""}
    asyncio.run(reachable.discover(SimpleNamespace(), [{"kind": "business", "url": "https://advisory.example/",
        "title": role, "expert_role": role}], [], context, payload))
    assert len(captured) == 1
    assert captured[0]["audience"] == context["audience"] and captured[0]["mode"] == context["mode"]
    assert captured[0]["expert_role"] == role
    assert captured[0]["problem"] == data["problem"]


def test_local_homepage_qualification_keeps_published_contact_links(local_search_domain):
    from types import SimpleNamespace
    from app.gtm import enrich, quality
    from app.gtm.discovery import LocalDiscoveryProvider

    data = local_search_domain
    source = "https://advisory.example/"
    page = f'<main><h1>{data["audience"]}</h1><p>{data["problem"]}</p></main>'
    page += '<footer><a href="/contact">Contact</a></footer>'

    async def main_scrape(url):
        return quality.main_text(page)

    async def scrape(url):
        return page if url == source else "Your published contact is hello@advisory.example."

    result = asyncio.run(enrich.enrich_site(LocalDiscoveryProvider(SimpleNamespace(name="builtin", _limit=6)), source, data["wid"], data["vid"],
        scrape=scrape, main_scrape=main_scrape, published_mailboxes=True,
        context={"audience": data["audience"], "expert_role": data["audience"] + " advisor"}, payload={}))
    assert result["contacts"][0]["email"] == "hello@advisory.example"
    assert result["contacts"][0]["source_url"] == source + "contact"


def test_local_expert_reads_keep_source_validation_not_buyer_qualification(local_search_domain, monkeypatch):
    from app.gtm import enrich, journey, quality

    data = local_search_domain
    journey.update_icp(data["wid"], data["vid"], data["audience"], problem=data["problem"], recruitment_opt_in=True)
    payload = _leased_payload(data)
    context = {"audience": data["audience"], "expert_role": data["audience"] + " advisor", "problem": data["problem"]}
    monkeypatch.setattr(quality, "classify_home", lambda *args: pytest.fail("An insight expert is not required to be a buyer"))

    async def read(url):
        return "<h1>Alex Rivera</h1><p>" + data["audience"] + ". " + data["problem"] + ".</p><p>hello@advisory.example</p>"

    result = asyncio.run(enrich.enrich_site(None, "https://advisory.example/", data["wid"], data["vid"],
        scrape=read, main_scrape=read, context=context, payload=payload, published_mailboxes=True))
    assert result["contacts"][0]["email"] == "hello@advisory.example"

    async def blocked(url):
        return "Access denied. Verify you are human. hello@advisory.example"

    result = asyncio.run(enrich.enrich_site(None, "https://advisory.example/", data["wid"], data["vid"],
        scrape=blocked, main_scrape=blocked, context=context, payload=payload, published_mailboxes=True))
    assert result["quality_rejected"] and not result.get("contacts")


def test_local_insight_experts_still_exclude_named_competitors(local_search_domain):
    from app.gtm import journey

    problem = local_search_domain["problem"]
    text = "We provide " + problem + "."
    args = ("https://advisory.example/", "Alex Rivera", text, [], problem, problem)
    assert journey._same_offer(*args)
    assert not journey._same_offer(*args, expert_role=local_search_domain["audience"] + " advisor")
    assert journey._same_offer(*args[:3], ["advisory.example"], *args[4:], expert_role="advisor")


@pytest.mark.parametrize("published_identity", [True, False])
def test_local_named_expert_on_a_professional_site_keeps_its_person_identity(local_search_domain, monkeypatch, published_identity):
    from types import SimpleNamespace
    from app.gtm import enrich, journey, reachable

    data = local_search_domain
    journey.update_icp(data["wid"], data["vid"], data["audience"], problem=data["problem"], recruitment_opt_in=True)
    payload = _leased_payload(data)
    source = "https://advisory.example/"
    role = data["audience"] + " advisor"
    quote = f"Alex Rivera is a {role}. {data['problem']}."

    async def enrich_site(*args, **kwargs):
        return {"contacts": [], "booking": None, "contact_form": None, "phone": None, "home": source,
            "pages": {source: quote}, "company_text": quote, "evidence_pages": [{"url": source, "text": quote}],
            "people": [], "profile_urls": [], "country": "", "careers_url": None, "name": "Advisory practice"}

    async def model(payload, stage, context):
        if stage != "reachjudge":
            return {"items": []}
        return {"items": [{"id": candidate["id"], "kind": "person", "name": "Alex Rivera" if published_identity else "Invented Person",
            "role": role, "company": "", "affiliation": "", "url": source, "quote": quote, "fit": 90,
            "why": "You can learn from this published experience.", "value_exchange": "You can share research findings.",
            "lead_type": "insight"} for candidate in context["candidates"]]}

    monkeypatch.setattr(enrich, "enrich_site", enrich_site)
    monkeypatch.setattr(journey, "_discovery_model", model)
    context = {"mode": "B2B" if data["business_model"] == "b2b" else "B2C", "audience": data["audience"],
        "problem": data["problem"], "competitors": [], "offer": ""}
    found = asyncio.run(reachable.discover(SimpleNamespace(), [{"kind": "person", "url": source,
        "title": "Advisory practice", "expert_role": role}], [], context, payload))
    assert len(found) == int(published_identity)
    if published_identity:
        assert found[0]["kind"] == "person" and found[0]["name"] == "Alex Rivera" and found[0]["lead_type"] == "insight"
        assert found[0]["quote"] == quote and found[0]["source_url"] == source


def test_local_search_never_invents_city_constraints(local_search_domain):
    from app.gtm import journey

    audience = local_search_domain["audience"].split(" in ")[0] + " in the United States"
    assert journey._business_cities(audience, context={"location": "United States"}) == []
    assert journey._business_cities(audience, context={"location": "Chicago"}) == [("Chicago", "IL")]
    planned = {"query": audience + " official website contact", "intent": "business"}
    assert journey._business_queries(audience, local_search_domain["problem"], [], {}, [planned])[0] == planned


def test_search_failure_diagnostics_keep_the_real_cause_redacted(local_search_domain, caplog):
    from app.core.jobs import RetryableError
    from app.gtm.journey import _log_search_failure

    secret = "sk-proj-" + "z" * 40
    try:
        raise ValueError("source unavailable " + secret)
    except ValueError as cause:
        error = RetryableError("gtm_settlement_pending")
        error.__cause__ = cause
    _log_search_failure(error, local_search_domain["vid"], "gtm.discovery.marketrole")
    record = caplog.records[-1]
    assert record.cause_type == "ValueError" and record.error_code == "gtm_settlement_pending"
    assert secret not in str(vars(record)) and not hasattr(record, "cause_message")
    _log_search_failure(ValueError("source unavailable " + secret), local_search_domain["vid"], "gtm.discovery")
    record = caplog.records[-1]
    assert record.error_type == "ValueError" and record.error_code == "gtm_search_failed"
    assert secret not in str(vars(record)) and not hasattr(record, "exception_message")


def test_search_diagnostics_redact_saved_keys_before_truncation(local_search_domain, monkeypatch, caplog):
    from core import logging as core_logging
    from app.core.jobs import RetryableError
    from app.gtm.journey import _log_search_failure

    secret = bytes(range(32)).hex()
    monkeypatch.setattr(core_logging, "_LOCAL_SECRETS", {secret})
    message = "source unavailable " + "x" * 460 + secret
    error = RetryableError("gtm_settlement_pending")
    error.__cause__ = ValueError(message)
    _log_search_failure(error, local_search_domain["vid"], "gtm.discovery")
    assert secret[:16] not in str(vars(caplog.records[-1]))
    _log_search_failure(ValueError(message), local_search_domain["vid"], "gtm.discovery")
    assert secret[:16] not in str(vars(caplog.records[-1]))


def test_local_new_search_clears_the_previous_expert_plan(local_search_domain):
    from app.core.jobs import FatalError, fail_job
    from app.gtm import journey

    data = local_search_domain
    journey.update_icp(data["wid"], data["vid"], data["audience"], problem=data["problem"], recruitment_opt_in=True)
    payload = _leased_payload(data)
    journal = journey._journal(payload)

    def interrupted(row, doc, session):
        doc["expert_discovery_plan"] = [{"query": "old role contact", "intent": "person", "expert_role": "old role"}]
        doc["_runtime"]["operations"]["old-read"] = {"state": "uncertain", "attempt": 1, "usage": {}}

    journal.mutate(payload["journey_id"], payload, interrupted)
    with data["factory"]() as session:
        fail_job(session, payload["_job_id"], FatalError("gtm_search_failed"), worker_id=payload["_worker_id"], attempt=payload["_job_attempt"])
        session.commit()
    journey.prepare(data["wid"], data["vid"], retry=True, recruitment_opt_in=True)
    with data["factory"]() as session:
        assert "expert_discovery_plan" not in journey._json(journey._row(session, data["vid"]).provenance_json)


def test_local_invalid_judge_batch_does_not_discard_other_sources(local_search_domain, monkeypatch):
    from app.core.jobs import FatalError
    from app.gtm import journey

    data = local_search_domain
    journey.update_icp(data["wid"], data["vid"], data["audience"], problem=data["problem"], recruitment_opt_in=True)
    payload = _leased_payload(data)

    async def invalid(*args, **kwargs):
        raise FatalError("discovery_invalid_output")

    monkeypatch.setattr(journey, "_discovery_model", invalid)
    assert asyncio.run(journey._judge_batch(payload, "reachjudge", {"candidates": [{"id": 0, "url": "https://source.example/"}]})) == []


def test_local_business_fallback_queries_use_public_contact_shapes_without_duplicate_variants(local_search_domain):
    from app.gtm import journey
    from app.gtm.discovery import contact_queries

    data = local_search_domain
    context = {"mode": "B2B" if data["business_model"] == "b2b" else "B2C", "audience": data["audience"],
        "problem": data["problem"]}
    planned = [{"query": "matching organizations published contacts", "intent": "business"}]
    queries = journey._business_queries(data["audience"], data["problem"], [], context, planned)
    assert queries[0] == planned[0]
    fallbacks = queries[1:]
    assert fallbacks and len(fallbacks) <= 6
    assert len({item["query"].casefold() for item in queries}) == len(queries)
    shapes = ("contact", "leadership", "team", "directory", "association members")
    assert all(any(shape in item["query"] for item in fallbacks) for shape in shapes)
    with_role = contact_queries(data["audience"], "coordinator")
    assert all(query.startswith("coordinator " + data["audience"]) for query in with_role)
    assert all(any(query.endswith(shape) for query in with_role) for shape in shapes)


@pytest.fixture()
def builtin_http_transport(local_search_domain, monkeypatch):
    from httpx import MockTransport
    from app.research import local_providers

    monkeypatch.setattr(local_providers, "local_read", local_providers.fetch_page)
    return MockTransport


@pytest.mark.parametrize("vault_available", [True, False], ids=["serp", "unavailable-vault"])
def test_local_customer_search_reads_serp_and_preserves_saved_audience(local_search_domain, builtin_http_transport, monkeypatch, caplog, vault_available):
    import base64
    import json
    from html import escape
    from types import SimpleNamespace

    import httpcore
    import httpx
    from ddgs.engines import ENGINES
    from ddgs.engines.bing import Bing
    from ddgs.http_client import HttpClient
    from app.chat import customers
    from app.chat.execute import find_customers_reply
    from app.config import data_root
    from app.core.jobs import lease
    from app.db.models import Job, Venture, VentureDocument
    from app.gtm import journey, reachable
    from app.gtm.email_learning import opaque
    from app.research import local_providers
    from app.signals import research
    from app.worker.handlers import install_default_handlers
    from app.worker.main import _run_one
    from core.logging import JsonFormatter
    from core.research_web import guard

    data = local_search_domain
    audience = data["audience"]
    consumer = data["business_model"] == "consumer"
    venture_name = "Care Calendar" if consumer else "Workshop Scheduler"
    hosts = [f"{data['business_model']}-source-{index}.example" for index in range(5)]
    names = ["Ada Miller", "Ben Clarke", "Cara Evans", "Dan Wright", "Ella Brown"]
    queries, opened, judged, checkpoints = [], set(), [], []
    pinned_ip = "93.184.216.34"
    urls = [f"{'http' if index == 0 else 'https'}://{host}/" for index, host in enumerate(hosts)]
    wrapped = "https://www.bing.com/ck/a?u=a1" + base64.urlsafe_b64encode(urls[0].encode()).decode().rstrip("=")
    serp = "".join(f'<li class="b_algo"><h2><a href="{url}">{name}</a></h2>'
        f"<p>{escape(audience)}</p></li>" for url, name in zip([wrapped, *urls[1:]], names))
    private = "private-person@example.test"

    def respond(request):
        if request.url.host == "empty.example":
            return httpx.Response(200, text="<main>Nothing found</main>")
        assert request.url.host == "www.bing.com"
        queries.append(request.url.params["q"])
        return httpx.Response(200, text=serp)

    def ddgs_text(query, *, limit, backend, **kwargs):
        if backend != "bing":
            return []
        queries.append(query)
        return [{"href": url, "title": name, "body": audience} for url, name in zip(urls, names)][:limit]

    class SharedIPStream(httpcore.NetworkStream):
        hostname = None
        reply = b""

        def start_tls(self, ssl_context, server_hostname=None, timeout=None):
            if server_hostname == hosts[0]:
                raise httpcore.ConnectError("HTTP-only redirect origin")
            self.hostname = server_hostname
            return self

        def write(self, buffer, timeout=None):
            if not buffer:
                return
            target = buffer.split(b"\r\n", 1)[0].split()[1].decode()
            host = next(line.split(b": ", 1)[1].decode() for line in buffer.split(b"\r\n") if line.lower().startswith(b"host: "))
            apex = host.removeprefix("www.").removeprefix("policy.")
            assert apex in hosts
            status, location, body = "404 Not Found", "", ""
            if target == "/robots.txt" and host == self.hostname and not host.startswith("policy."):
                status, location = "301 Moved", f"Location: https://policy.{apex}/robots.txt\r\n"
            elif target != "/robots.txt" and (host == self.hostname or host == hosts[0] and self.hostname is None):
                if host == apex:
                    status, location = "301 Moved", f"Location: https://www.{host}/profiles\r\n"
                else:
                    opened.add(apex)
                    if apex == hosts[3]:
                        status, body = "200 OK", '<main><h1>Please verify you are a human</h1><div class="g-recaptcha"></div></main>'
                    elif apex != hosts[4]:
                        status, body = "200 OK", (f"<main><h1>{names[hosts.index(apex)]}</h1>"
                            f"<p>{escape(audience)}. {escape(data['problem'])}.</p>"
                            f'<p>Contact {private} or <a href="mailto:info@{apex}">info@{apex}</a></p></main>')
            content = body.encode()
            self.reply = f"HTTP/1.1 {status}\r\n{location}Content-Type: text/html\r\nContent-Length: {len(content)}\r\n\r\n".encode() + content

        def read(self, max_bytes, timeout=None):
            chunk, self.reply = self.reply[:max_bytes], self.reply[max_bytes:]
            return chunk

        def close(self):
            pass

    class SharedIPBackend(httpcore.NetworkBackend):
        def connect_tcp(self, host, port, **kwargs):
            assert host == pinned_ip and port in {80, 443}
            if not checkpoints:
                checkpoints.append(job_runtime())
            return SharedIPStream()

    class EmptyEngine(Bing):
        search_url = "https://empty.example/search"

    def job_runtime():
        with data["factory"]() as session:
            return json.loads(session.get(Job, job.id).payload_json).get("_runtime", {})

    async def model(payload, stage, context):
        if stage == "plan":
            intent = "person" if consumer else "business"
            return {"audience": f"customers for {venture_name}", "target_type": intent, "queries": [
                {"query": f"{audience} {suffix}", "intent": intent} for suffix in ("profiles", "directory")]}
        if stage in {"marketrole", "reachjudge"}:
            judged.append((context["candidates"], job_runtime()))
        if stage == "marketrole":
            return {"role_in_market": "buyer", "icp_fit": 0.4, "anti_icp": False, "homepage_quote": audience}
        return {"items": []}

    client = httpx.Client
    def page_client(**options):
        transport = httpx.HTTPTransport(limits=options.pop("limits", httpx.Limits(max_keepalive_connections=20)))
        transport._pool._network_backend = SharedIPBackend()
        return client(transport=transport, **options)

    with client(transport=builtin_http_transport(respond)) as engine:
        monkeypatch.setattr(HttpClient, "request", lambda _, method, url, **options: engine.request(method, url, **options))
        monkeypatch.setattr(httpx, "Client", page_client)
        monkeypatch.setattr(guard.socket, "getaddrinfo", lambda *args, **kwargs: [(0, 0, 0, "", (pinned_ip, 0))])
        monkeypatch.setitem(ENGINES, "text", {"bing": Bing, "mojeek": EmptyEngine})
        monkeypatch.setattr(local_providers, "time", SimpleNamespace(monotonic=lambda: 0, sleep=lambda _: None))
        monkeypatch.setattr(journey, "_discovery_model", model)
        with data["factory"]() as session:
            session.get(Venture, data["vid"]).name = venture_name
            session.commit()
        journey.update_icp(data["wid"], data["vid"], audience, problem=data["problem"], start=False)
        started = asyncio.run(find_customers_reply(data["vid"], f"Find customers for {venture_name}"))
        assert audience in started["body_md"]
        with data["factory"]() as session:
            job, = lease(session, "review-worker", kinds=["gtm.journey.prepare"], n=1)
            session.commit()
        if not vault_available:
            opaque("initialize diagnostic key")
            (data_root() / "vault/master.key").write_bytes(b"invalid encrypted connector vault key")
            monkeypatch.setattr(research, "_ddgs_text", ddgs_text)
        install_default_handlers()
        with caplog.at_level("INFO", logger=reachable.log.name):
            asyncio.run(_run_one(data["factory"], job, {(job.id, job.attempts)}))

    assert queries and all(audience.split()[1].casefold() in query.casefold() for query in queries), queries
    with data["factory"]() as session:
        assert session.get(Job, job.id).status == "succeeded"
    runtime = job_runtime()
    assert runtime["search_queries"], runtime
    assert opened == set(hosts)
    with data["factory"]() as session:
        journal = json.loads(session.get(VentureDocument, job.payload["journey_id"]).provenance_json)
    assert any(key.startswith("signal_search-") and operation["state"] == "completed" and operation["response"]
        for key, operation in journal["_runtime"]["operations"].items())
    assert checkpoints[0]["search_queries"]
    assert {query["results"] for query in runtime["search_queries"].values()} == {5}
    assert {query["reason"] for query in runtime["search_queries"].values()} == {""}
    sources = runtime["source_yield"]
    outcomes = [sources[opaque(url, "source-yield")] for url in urls]
    assert [source["read_state"] for source in outcomes] == ["read"] * 3 + ["failed"] * 2
    assert [source["read_reason"] for source in outcomes] == [None] * 3 + ["blocked access", "page not found"]
    assert outcomes[-1]["http_status"] == 404
    assert any(data["problem"] in candidate["text"] for candidates, _ in judged for candidate in candidates)
    assert any(source["read_state"] == "read" for _, checkpoint in judged for source in checkpoint["source_yield"].values())
    view = journey.status(data["wid"], data["vid"], authorize=False)
    assert view["job_id"] == job.id and view.get("error_code") != "public_sources_unavailable"
    assert view["source_access"] != "unavailable"
    assert customers._search_audience(view) == audience
    [event] = [record for record in caplog.records if record.getMessage().startswith("gtm_customer_search_diagnostics:")]
    line = JsonFormatter().format(event)
    summary = json.loads(json.loads(line)["msg"].partition(": ")[2])
    assert summary["job_id"] == job.id
    assert {query[0] for query in summary["queries"].values()} == {5}
    assert {outcome: len(sources) for outcome, sources in summary["sources"].items()} == {
        "read": 3, "failed blocked access 200": 1, "failed page not found 404": 1}
    assert len(line.encode()) < 8192 and private not in line + caplog.text
    if not vault_available:
        with pytest.raises(ValueError, match="Restore your encrypted vault backup"):
            opaque(private)


def test_local_worker_json_logs_reach_desktop_stderr(local_search_domain, monkeypatch, capsys):
    import json
    import logging
    from app.worker import main as worker
    from core import env_contract, logging as service_logging

    def check_stream(**kwargs):
        logging.getLogger("app.gtm.reachable").info("gtm_customer_search_diagnostics: stream check")
        raise RuntimeError("Stop after checking the stream")

    monkeypatch.setenv("APP_ROLE", "worker")
    monkeypatch.setenv("SERVICE_NAME", "company_os.worker")
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.setattr(logging.getLogger(), "handlers", [])
    monkeypatch.setattr(service_logging, "_CONFIGURED", set())
    monkeypatch.setattr(env_contract, "ensure_boot_env", check_stream)
    with pytest.raises(RuntimeError, match="Stop after checking the stream"):
        worker.main([])
    event = json.loads(capsys.readouterr().err)
    assert event["level"] == "INFO" and event["msg"].endswith("stream check")


@pytest.mark.parametrize("finishes", [True, False])
def test_local_customer_job_exposes_only_redacted_search_diagnostics(local_search_domain, monkeypatch, caplog, finishes):
    import json
    from types import SimpleNamespace
    from ddgs.exceptions import RatelimitException
    from fastapi import HTTPException
    from app.api import local_health
    from app.chat import customers
    from app.core.jobs import complete_job
    from app.db.models import Job
    from app.gtm import journey, reachable
    from app.research import local_providers
    from app.signals import research

    data = local_search_domain
    venture = data["vid"]
    private = "private-person@example.test"
    source = f"https://site.example/contact?address={private}"
    provider = local_providers.LocalHttpResearchProvider("duckduckgo", "")
    def limited(*args, **kwargs):
        raise RatelimitException("fixture refusal")

    monkeypatch.setattr(research, "_ddgs_text", limited)
    monkeypatch.setattr(local_providers, "time", SimpleNamespace(monotonic=lambda: 0, sleep=lambda _: None))

    async def discover(who, *args, payload, **kwargs):
        reachable.observe_source(payload, source, f"<p>Contact {private}</p>")
        with pytest.raises(local_providers.SearchEngineLimited):
            await provider._search(who + " " + private)
        return []

    monkeypatch.setattr(journey, "_discover", discover)
    journey.update_icp(data["wid"], venture, data["audience"], problem=data["problem"], recruitment_opt_in=True)
    payload = _leased_payload(data)
    with caplog.at_level("INFO", logger=reachable.log.name):
        if finishes:
            asyncio.run(journey.run_prepare(payload))
        else:
            def fail_after_discovery(*args, **kwargs):
                raise RuntimeError("interrupted after discovery")

            with monkeypatch.context() as failed, pytest.raises(RuntimeError, match="interrupted after discovery"):
                failed.setattr(journey, "_prospects", fail_after_discovery)
                asyncio.run(journey.run_prepare(payload))
    with data["factory"]() as session:
        if finishes:
            complete_job(session, payload["_job_id"], worker_id=payload["_worker_id"], attempt=payload["_job_attempt"])
            session.commit()
        runtime = json.loads(session.get(Job, payload["_job_id"]).payload_json)["_runtime"]
    assert len(runtime["source_yield"]) == 1
    [query] = runtime["search_queries"].values()
    assert query["results"] == 0 and query["reason"] == "search_engine_limited"
    [event] = [record.getMessage() for record in caplog.records
        if record.getMessage().startswith("gtm_customer_search_diagnostics:")]
    assert json.loads(event.partition(": ")[2])["queries"] == {
        key[:12]: [0, "search_engine_limited"] for key in runtime["search_queries"]}
    assert private not in str(runtime) + caplog.text
    if finishes:
        view = journey.status(data["wid"], venture, authorize=False)
        assert local_providers.SEARCH_LIMITED in customers.render(venture, view)["body_md"]
    monkeypatch.delenv("SUPYMEM_DEV_DIAGNOSTICS", raising=False)
    with pytest.raises(HTTPException, match="404"):
        local_health.local_search_diagnostics(None)
    monkeypatch.setenv("SUPYMEM_DEV_DIAGNOSTICS", "1")
    monkeypatch.setattr("app.core.local_owner.request_owner", lambda request: SimpleNamespace(id=data["actor"]))
    response = local_health.local_search_diagnostics(None)
    assert json.loads(response.body)["diagnostics"] == runtime
    assert response.headers["cache-control"] == "no-store"
    monkeypatch.setattr("app.core.local_owner.request_owner", lambda request: SimpleNamespace(id="per_other"))
    assert json.loads(local_health.local_search_diagnostics(None).body)["diagnostics"] == {}


def test_search_limit_notice_gives_the_next_step_in_the_same_sentence(local_search_domain):
    from app.gtm import enrich

    skipped, prefix = (1, "1 check was skipped") if local_search_domain["business_model"] == "b2b" else (3, "3 checks were skipped")
    notice = enrich.source_failure_summary([], searches_skipped=skipped)
    assert notice.startswith(f"{prefix} after your search reached its limit, so narrow your audience")
    assert notice.endswith("Keys and connections > Search.")
    assert notice.count(".") == 1


@pytest.mark.parametrize("audience, domain", [
    ("Independent optical labs", "lenskiln.example"),
    ("Apartment gardeners", "porchplanter.example"),
])
def test_queued_search_does_not_promise_an_inline_card(journey_db, monkeypatch, audience, domain):
    from app.chat import customers
    from app.chat.store import append_message

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    venture = journey_db["vid"]
    reply = customers.render(venture, {"state": "queued", "job_id": f"search:{domain}", "icp": {"who": audience}})
    saved = append_message(venture, role="assistant", **reply)
    assert "Your customer search is queued" in saved["body_md"] and audience in saved["body_md"]
    assert "below" not in saved["body_md"]
    assert "I will post here when your search finishes or stops." in saved["body_md"]
    assert saved["gtm_search_progress"]["job_id"] == f"search:{domain}"


@pytest.mark.parametrize("domain, mode, found, drafts, next_action", [
    ("dockcaliper.example", "company", 2, 1, "Open Search settings"),
    ("quiltbench.example", "consumer", 3, 0, "Search again"),
])
def test_finished_search_posts_counts_and_one_next_step(journey_db, monkeypatch, domain, mode, found, drafts, next_action):
    from app.chat import customers
    from app.chat.store import list_messages
    from app.core.local_secrets import update_settings
    from app.gtm import journey
    from tests.company_os_harness import configure_local_gtm

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    configure_local_gtm(monkeypatch)
    update_settings("search", {"provider": "builtin"}, {})
    data = journey_db
    view = {"state": "ready", "discovery_started": True, "can_prepare": True,
        "icp": {"who": domain, "mode": mode}, "search_target": {"requested_count": 5},
        "reachable_leads": [{"id": f"lead-{index}", "source_url": f"https://{domain}/{index}"} for index in range(found)],
        "drafts": [{"state": "draft", "channel": "email"}] * drafts,
        "outreach_next_step": {"actions": []}}
    monkeypatch.setattr(journey, "status", lambda *args, **kwargs: view)
    monkeypatch.setattr(customers, "render", lambda *args, **kwargs: {"kind": "text", "actions": [],
        "body_md": "1 source could not be read: blocked access. 21 checks were skipped."})

    assert journey._post_results(data["wid"], data["vid"], "findcopy:" + domain)
    [result] = list_messages(data["vid"])
    assert f"You found {found} contacts. You have {drafts} email draft" in result["body_md"]
    assert not any(detail in result["body_md"] for detail in ("blocked access", "source", "skipped", "below"))
    assert [action["label"] for action in result["actions"]] == [next_action]


def test_local_customer_search_budget_separates_skipped_searches_from_unreadable_pages(local_search_domain, monkeypatch):
    from types import SimpleNamespace
    from app.chat import customers
    from app.db.models import Job
    from app.gtm import journey
    from app.research import local_providers
    from app.signals import research

    data = local_search_domain
    provider = local_providers.LocalHttpResearchProvider("duckduckgo", "", limit=1)
    calls = []

    def fake_ddgs(query, *, limit, backend, **kwargs):
        calls.append((query, backend))
        return [{"href": "https://source.example/", "title": "Public source", "body": query}]

    async def discover(who, *args, payload, **kwargs):
        queries = [f"{who} {index}" for index in range(20)]
        for query in queries:
            assert await provider._search(query)
        with pytest.raises(ValueError, match="Keys and connections"):
            await provider._search(who + " company lookup")
        assert await provider._search(queries[0])
        assert calls == [(query, "bing") for query in queries]
        journey._journal(payload).mutate(payload["journey_id"], payload, lambda row, doc, session:
            doc["_runtime"].update(source_reads={
                "https://source.example/": {"state": "read"},
                "https://blocked.example/": {"state": "failed", "reason": "blocked access"},
                "https://unopened.example/": {"state": "failed", "reason": "search limit reached"},
            }))
        return []

    monkeypatch.setattr(research, "_ddgs_text", fake_ddgs)
    monkeypatch.setattr(local_providers, "time", SimpleNamespace(monotonic=lambda: 0, sleep=lambda _: None))
    monkeypatch.setattr(journey, "_discover", discover)
    journey.update_icp(data["wid"], data["vid"], data["audience"], problem=data["problem"], recruitment_opt_in=True)
    payload = _leased_payload(data)
    asyncio.run(journey.run_prepare(payload))
    assert len(calls) == 20
    with data["factory"]() as session:
        runtime = journey._json(session.get(Job, payload["_job_id"]).payload_json)["_runtime"]
    assert sum(query["cached"] for query in runtime["search_queries"].values()) == 1
    with local_providers.collect_search_diagnostics(customer_run_id=payload["prepare_id"]):
        with pytest.raises(local_providers.SearchBudgetExhausted):
            asyncio.run(provider._search(data["audience"] + " after worker restart"))
    view = journey.status(data["wid"], data["vid"], authorize=False)
    reply = customers.render(data["vid"], view)["body_md"]
    assert sum(query["reason"] == "search_budget_exhausted" for query in runtime["search_queries"].values()) == 1
    assert {source["read_reason"] for source in runtime["source_yield"].values()} >= {"blocked access", "search limit reached"}
    assert not any(detail in reply for detail in ("skipped", "could not be read", "blocked access"))
    assert "Keys and connections > Search" in reply


def test_local_customer_search_spends_on_audience_before_top_organisation_lookups(local_search_domain, monkeypatch):
    from types import SimpleNamespace
    from app.gtm import journey, reachable
    from app.research import local_providers
    from app.signals import research

    data = local_search_domain
    audience = data["audience"]
    organisation_query, audience_query = [f"{audience} {suffix}" for suffix in ("official websites", "public profiles")]
    hosts = ("first.example", "second.example", "third.example")
    calls = []

    def fake_ddgs(query, *, limit, backend, **kwargs):
        calls.append(query)
        if backend != "bing":
            return []
        if query.startswith("site:"):
            host, topic = query.removeprefix("site:").split(" ", 1)
            return [{"href": f"https://{host}/{topic}", "title": topic}]
        return [{"href": f"https://{host}/", "title": f"{audience} {index}", "body": audience}
            for index, host in enumerate(hosts if query == organisation_query else hosts[:1])]

    async def no_pages(*args, **kwargs):
        return []

    async def no_reformulation(*args, **kwargs):
        return {"queries": []}

    monkeypatch.setattr(research, "_ddgs_text", fake_ddgs)
    monkeypatch.setattr(local_providers, "time", SimpleNamespace(monotonic=lambda: 0, sleep=lambda _: None))
    monkeypatch.setattr(local_providers.LocalHttpResearchProvider, "search", local_providers.LocalHttpResearchProvider._search)
    monkeypatch.setattr(reachable, "discover", no_pages)
    monkeypatch.setattr(journey, "_discovery_model", no_reformulation)
    journey.update_icp(data["wid"], data["vid"], audience, problem=data["problem"], recruitment_opt_in=True)
    payload = _leased_payload(data)
    journal = journey._journal(payload)
    journal.mutate(payload["journey_id"], payload, lambda row, doc, session: doc.update(
        search_target={"target_type": "person", "audience": audience, "organisation_audience": audience},
        discovery_plan=[{"query": organisation_query, "intent": "business"}, {"query": audience_query, "intent": "person"}]))
    asyncio.run(journey.run_prepare(payload))
    ordered = list(dict.fromkeys(calls))
    assert ordered == [organisation_query, audience_query] + [f"site:{host} {suffix}"
        for host in hosts[:2] for suffix in ("team", "leadership")]


def test_local_business_snippets_wait_for_the_actual_page_check(local_search_domain, monkeypatch):
    from types import SimpleNamespace
    from app.gtm import discovery, enrich, journey, reachable

    data = local_search_domain
    business = data["business_model"] == "b2b"
    context = {"mode": "B2B" if business else "B2C", "audience": data["audience"],
        "problem": data["problem"], "offer": "", "competitors": []}
    hit = {"kind": "business", "url": "https://practice.example/", "title": "Public practice",
        "snippet": "We offer maintenance and scheduling."}
    assert discovery.hit_target_fit(hit, context) == business
    assert not discovery.hit_target_fit(hit, context | {"target_type": "person"})
    journey.update_icp(data["wid"], data["vid"], data["audience"], problem=data["problem"], recruitment_opt_in=True)
    payload = _leased_payload(data)
    checked = []

    async def rejected(*args, **kwargs):
        checked.append(args[1])
        return {"quality_rejected": True}

    monkeypatch.setattr(enrich, "enrich_site", rejected)
    found = asyncio.run(reachable.discover(SimpleNamespace(), [hit], [], context, payload))
    assert not found and checked == ([hit["url"]] if business else [])


@pytest.mark.parametrize("expert", [False, True])
def test_local_insight_judge_keeps_unknown_location_unknown(local_search_domain, monkeypatch, expert):
    import json
    from app.gtm import journey
    from app.research import metered

    data = local_search_domain
    journey.update_icp(data["wid"], data["vid"], data["audience"], problem=data["problem"], recruitment_opt_in=True)
    payload = _leased_payload(data)

    def synthesize(*args, **kwargs):
        instruction = kwargs["messages"][0]["content"]
        assert ("Unknown expert location alone" in instruction) == expert
        if expert:
            assert "Do not invent a location or claim service coverage" in instruction
        return kwargs["parse"](json.dumps({"items": [{"id": 0, "reject_reason": "No published identity."}]}))

    monkeypatch.setattr(metered, "synthesize", synthesize)
    candidate = {"id": 0, "url": "https://advisory.example/", "text": data["problem"]}
    if expert:
        candidate["expert_role"] = data["audience"] + " advisor"
    asyncio.run(journey._discovery_model(payload, "reachjudge", {"audience": data["audience"],
        "problem": data["problem"], "candidates": [candidate]}))


@pytest.mark.parametrize("provider", ["builtin", "tavily"])
def test_local_search_key_is_optional_and_missing_upgrade_names_its_setting(local_search_domain, provider):
    from app.chat import customers
    from app.core.local_capabilities import blocked_message
    from app.core.local_secrets import update_settings

    update_settings("search", {"provider": provider}, {})
    reply = customers.empty_search_reply({"icp": {"who": local_search_domain["audience"]},
        "error_code": "public_search_blocked"})
    if provider == "builtin":
        assert "Settings > Keys and connections" not in reply["body_md"]
    else:
        assert blocked_message("find_customers") in reply["body_md"]
        assert "API key" in reply["body_md"] and "Settings > Keys and connections" in reply["body_md"]
    assert "sample" not in reply["body_md"].casefold()
    _recovery_actions(reply)


def test_hosted_customer_search_without_a_key_keeps_public_recovery(search_domain, monkeypatch):
    from app.chat import customers
    from app.research import local_providers

    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    monkeypatch.setattr(local_providers, "search_provider", lambda **kwargs: None)
    audience = search_domain["audience"]
    reply = customers.empty_search_reply({"icp": {"who": audience}, "error_code": "public_search_blocked"})
    assert reply["body_md"] == (
        f"Public sources could not be opened, so I could not verify reachable contacts for {audience}. "
        "You can try public community or LinkedIn search, or add someone you know. Nothing is posted or sent.")
    context = f"Your audience: {audience}."
    assert reply["actions"] == [
        {"label": f"Find communities for {audience}", "action": "openworker_search", "style": "primary",
         "payload": {"query": f"{audience} public subreddits forums groups", "search_context": context}},
        {"label": f"Search public LinkedIn for {audience}", "action": "openworker_search", "style": "secondary",
         "payload": {"query": f"site:linkedin.com/in/ {audience}", "search_context": context}},
        {"label": "Add someone you know", "action": "contacts.preview", "style": "secondary",
         "payload": {"search_context": context}},
    ]


def test_local_source_yield_counts_rejection_reasons_without_contact_data(local_search_domain):
    import json
    from types import SimpleNamespace
    from app.gtm import reachable
    from app.gtm.email_learning import opaque

    data = local_search_domain
    url = "https://source.example/contact?ref=private@example.test"
    payload = {}
    reachable.observe_source(payload, url, "<p>Contact Sam Ellis at private@example.test</p>")
    reason = "consumer_non_consumer_source" if data["business_model"] == "consumer" else "not_grounded_senior_role"
    for code in (reason, "low_fit", reason):
        reachable.trace(payload, url, "dropped", code)
    job = SimpleNamespace(id="search", payload_json="{}")
    reachable.save_job_search_diagnostics(job, {}, payload)
    source = json.loads(job.payload_json)["_runtime"]["source_yield"][opaque("https://source.example/contact", "source-yield")]
    assert source["rejection_reasons"] == {reason: 2, "low_fit": 1}
    assert source["rejections"] == 3
    assert all(value not in job.payload_json for value in ("Sam Ellis", "private@example.test", "source.example"))


def test_local_role_search_keeps_public_staff_and_department_routes(local_search_domain, monkeypatch):
    from types import SimpleNamespace
    from app.db.models import GtmTouch
    from app.gtm import journey, reachable

    data = local_search_domain
    consumer = data["business_model"] == "consumer"
    first, second, role, organisation, mailbox = (("Juniper Swim School", "Linden Swim School", "Swim Coach", "Swim schools", "lessons")
        if consumer else ("Juniper Medical Center", "Linden Medical Center", "Director of Pharmacy", "Independent acute-care hospitals", "pharmacy"))
    audience = f"{role} at {organisation}"
    journey.update_icp(data["wid"], data["vid"], audience, problem=data["problem"], recruitment_opt_in=True)
    payload = _leased_payload(data)
    target = {"target_type": "person", "audience": audience, "requested_role": role, "organisation_audience": organisation}
    journey._journal(payload).mutate(payload["journey_id"], payload, lambda row, doc, session: doc.update(search_target=target))
    profile = "https://juniper.example/staff/alex-rivera"
    department = "https://linden.example/department/contact"
    email = "alex.rivera@juniper.example"
    team_email = f"{mailbox}@linden.example"
    quotes = {profile: f"Alex Rivera, {role} at {first}.", department: f"Contact the {mailbox} team at {second}."}
    pages = {
        profile: f'<h1>{first}</h1>\n<p>Alex Rivera, {role}</p>\n<p>{quotes[profile]}</p>\n'
                 f'<a href="mailto:{email}">{email}</a>\n<a href="tel:+12025550123">Call</a>'
                 '\n<p>Personal email: alex.rivera@personal.example</p>',
        department: f'<h1>{second}</h1>\n<p>{quotes[department]}</p>\n<a href="mailto:{team_email}">{team_email}</a>',
    }

    async def scrape(url, **kwargs):
        return pages[url]

    async def model(payload, stage, context):
        if stage != "reachjudge":
            return {"items": []}
        return {"items": [{"id": candidate["id"], "kind": "person" if candidate["url"] == profile else "business",
            "name": "Alex Rivera" if candidate["url"] == profile else second, "role": role if candidate["url"] == profile else "",
            "company": first if candidate["url"] == profile else second, "affiliation": "", "url": candidate["url"],
            "quote": quotes[candidate["url"]], "fit": 90, "why": "You can learn about this work.",
            "value_exchange": "You can share what you learn." if consumer else ""} for candidate in context["candidates"]]}

    monkeypatch.setattr(journey, "_discovery_model", model)
    context = target | {"mode": "B2C" if consumer else "B2B", "problem": data["problem"], "competitors": [], "offer": ""}
    hits = [{"kind": "person", "url": url, "title": title} for url, title in ((profile, first), (department, second))]
    leads = asyncio.run(reachable.discover(SimpleNamespace(name="builtin", scrape=scrape), hits, [], context, payload))
    assert {lead["source_url"] for lead in leads} == ({profile} if consumer else {profile, department})
    view = journey.status(data["wid"], data["vid"], authorize=False)
    assert {lead["source_url"] for lead in view["reachable_leads"]} == {lead["source_url"] for lead in leads}
    assert all(route.get("email") != "alex.rivera@personal.example" for lead in leads for route in lead["routes"])
    if not consumer:
        by_source = {lead["source_url"]: lead for lead in leads}
        assert {"email", "phone", "website"} <= {route["kind"] for route in by_source[profile]["routes"]}
        assert reachable.published_route(by_source[profile])["email"] == email
        assert by_source[department]["company_contact_fallback"]
        assert reachable.published_route(by_source[department])["email"] == team_email
        assert reachable.published_route(by_source[department])["source_url"] == department
    with data["factory"]() as session:
        assert session.scalar(select(GtmTouch.id).where(GtmTouch.direction == "out", GtmTouch.sent_at.is_not(None))) is None


def test_local_customer_result_invalidates_open_chat_after_commit(local_search_domain, monkeypatch):
    from types import SimpleNamespace
    from app.api.activity import stream_activity
    from app.chat import customers
    from app.chat.store import append_message, list_messages
    from app.gtm import journey

    data = local_search_domain
    venture = data["vid"]
    result = f"Your search for {data['audience']} is finished. Review your leads."
    append_message(venture, role="assistant", body_md="Your customer search is queued.")
    monkeypatch.setattr(journey, "status", lambda *args, **kwargs: {"outreach_next_step": {"actions": []}})
    monkeypatch.setattr(customers, "render", lambda *args: {"body_md": result, "actions": [], "kind": "text"})

    async def exercise():
        async def connected():
            return False

        stream = (await stream_activity(venture, SimpleNamespace(headers={"x-subject": data["actor"]}, is_disconnected=connected))).body_iterator
        try:
            assert await anext(stream) == ": heartbeat\n\n"
            assert not any(result in item["body_md"] for item in list_messages(venture))
            assert journey._post_results(data["wid"], venture, "search-run")
            assert not journey._post_results(data["wid"], venture, "search-run")
            assert "event: activity\n" in await anext(stream)
            assert sum(result in item["body_md"] for item in list_messages(venture)) == 1
            assert await anext(stream) == ": heartbeat\n\n"
        finally:
            await stream.aclose()

    asyncio.run(exercise())

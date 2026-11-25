"""Top search hits are fetched as full pages; failures keep the excerpt-only result."""
import json
import threading
from types import SimpleNamespace

import pytest

from tests.security.test_connector_oauth_boundary import client as client  # noqa: F401
from tests.security.test_research_boundary import confirmed, dossier, job
from tests.security.test_durable_journey_boundary import run_queued
from tests.research_fixtures import install_research
from tests.company_os_harness import load_company_os_app

UNVERIFIED_GAP = "Search excerpts have not been verified against full source pages."


def install_provider(monkeypatch, *, fail=False, hosts=16):
    from app.research import deep
    scraped = []

    class Provider:
        name = "firecrawl"

        async def search(self, query):
            import hashlib
            offset = int(hashlib.sha256(query.encode()).hexdigest()[:8], 16) % hosts
            return [{"url": f"https://vendor{i}.example.com/pricing", "title": "Bakery order software",
                     "snippet": "Bakery order management for small shops."} for i in [(offset + index) % hosts for index in range(hosts)]]

        async def scrape(self, url):
            scraped.append(url)
            if fail:
                class Rejected(Exception):
                    status = 404  # Synthetic definitive rejection, not a transport timeout.
                raise Rejected()
            return "# Bakery order software\n\nPlans start at $12 per month for each shop."

    monkeypatch.setattr(deep, "_web_provider", lambda: Provider())
    return scraped


def test_scraped_pages_become_verified_sources_and_support_an_area(client, monkeypatch):
    vid, _, _, jid = confirmed(client, monkeypatch)
    install_research(monkeypatch)
    scraped = install_provider(monkeypatch)
    run_queued("research.deep")
    assert job(jid).status == "succeeded", job(jid).last_error
    result = dossier(client, vid)
    verified = [f for f in result["findings"]["source"] if f["page_verified"]]
    assert len(scraped) == len(verified) == 12
    assert len({url.split("/")[2] for url in scraped}) == 12
    pricing = result["summary"]["evidence_dimensions"]["pricing"]
    assert pricing["status"] == "supporting_leads" and pricing["source_strength"] == "page_verified"
    assert result["summary"]["evidence_dimensions"]["willingness_to_pay"]["status"] == "unknown"
    gaps = result["summary"]["coverage_gaps"]
    assert UNVERIFIED_GAP not in gaps and not any("verified against full pages" in g for g in gaps)
    from app.research import execution as ex
    saved = json.loads(ex.read(ex.identifier(jid)).summary)
    assert any(item["kind"] == "system_verification" and "verified against full pages" in item["text"] for item in saved["gap_items"])
    assert result["calls_used"] <= result["budget_calls"]


def test_scrape_failure_falls_back_to_excerpts(client, monkeypatch):
    vid, _, _, jid = confirmed(client, monkeypatch)
    def empty_review(messages, options):
        body = {"overview": "Excerpts only.", "observations": [], "assessments": [], "service_checks": [],
                "coverage_gaps": ["No verified pages."], "proposed_tests": []}
        from openai.types import CompletionUsage
        return SimpleNamespace(model="DeepSeek-V4-Pro", service_tier="default",
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(body)))],
            usage=CompletionUsage(prompt_tokens=500, completion_tokens=100, total_tokens=600,
                prompt_tokens_details={"cached_tokens": 0}, completion_tokens_details={"reasoning_tokens": 0}))
    install_research(monkeypatch, callback=empty_review)
    scraped = install_provider(monkeypatch, fail=True)
    run_queued("research.deep")
    assert job(jid).status == "succeeded", job(jid).last_error
    result = dossier(client, vid)
    assert len(scraped) == 12
    assert not any(f["page_verified"] for f in result["findings"]["source"])
    assert UNVERIFIED_GAP not in result["summary"]["coverage_gaps"]
    assert all("verified pages" not in gap.lower() for gap in result["summary"]["coverage_gaps"])
    assert result["summary"]["evidence_dimensions"]["pricing"]["status"] == "unknown"
    assert result["summary"]["review"] is not None


def test_provider_without_scrape_is_unchanged(client, monkeypatch):
    vid, _, _, jid = confirmed(client, monkeypatch)
    install_research(monkeypatch)  # its provider has no scrape method
    run_queued("research.deep")
    assert job(jid).status == "succeeded"
    assert UNVERIFIED_GAP not in dossier(client, vid)["summary"]["coverage_gaps"]


@pytest.mark.parametrize("status,retry_after,delay", [(429, "4", 4), (429, "900", 60), (503, "12", 12)])
def test_firecrawl_search_rejection_waits_before_founder_research_completes(client, monkeypatch, status, retry_after, delay):
    from datetime import timedelta

    import httpx
    from core import clock
    from app.research import deep, execution as ex
    from app.signals import research

    vid, _, _, jid = confirmed(client, monkeypatch)
    calls = install_research(monkeypatch)
    now = [clock.utcnow()]
    monkeypatch.setattr(clock, "_clock", lambda: now[0])
    monkeypatch.setattr(research, "_pace_firecrawl", lambda key: None)
    requests = []

    def respond(request):
        if request.url.path == "/v2/scrape":
            return httpx.Response(200, json={"success": True, "data": {
                "markdown": "Bakery order management. Plans cost $12 monthly."}})
        assert request.url.path == "/v2/search"
        requests.append(json.loads(request.content)["query"])
        if len(requests) == 2:
            return httpx.Response(status, headers={"Retry-After": retry_after})
        return httpx.Response(200, json={"success": True, "data": {"web": [{
            "url": "https://example.com/bakery", "title": "Bakery order software",
            "description": "Bakery order management. Plans cost $12 monthly."}]}})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    monkeypatch.setattr(deep, "_web_provider", lambda: research.HttpResearchProvider("firecrawl", "offline-key"))
    run_queued("research.deep", through_job_id=jid)
    waiting = job(jid)
    assert waiting.status == "retry_wait" and waiting.attempts == 1
    retry_at = clock.as_utc(waiting.run_after)
    assert retry_at == now[0] + timedelta(seconds=delay)
    result = dossier(client, vid)
    assert result["state"] == "running" and result["completed_at"] is None
    assert result["summary"]["review"] is None and len(result["findings"]["source"]) == 1
    assert calls == []
    operations = json.loads(ex.read(result["id"]).summary)["_runtime"]["operations"]
    paid_key, paid = next((key, receipt) for key, receipt in operations.items() if receipt["state"] == "completed")
    rejected = next(receipt for receipt in operations.values() if receipt["state"] == "retryable")
    assert rejected["usage"]["definitive_unspent"] and rejected["usage"]["settled"]
    assert rejected["usage"]["credits_micro"] == 0
    assert rejected["failure"]["http_status"] == status

    now[0] = retry_at + timedelta(seconds=1)
    run_queued("research.deep", through_job_id=jid)
    assert job(jid).status == "succeeded" and job(jid).attempts == 2, job(jid).last_error
    result = dossier(client, vid)
    assert result["state"] in {"complete", "partial"} and result["summary"]["review"] is not None
    assert requests.count(requests[0]) == 1 and requests[1] == requests[2]
    assert json.loads(ex.read(result["id"]).summary)["_runtime"]["operations"][paid_key] == paid


@pytest.mark.parametrize("kind", ["search", "scrape", "scrape_http", "github", "408", "429", "500", "connection", "content"])
def test_source_failure_never_replays_and_paid_uncertainty_stops_run(client, monkeypatch, caplog, kind):
    from app.research import deep, execution as ex, github_search

    vid, _, _, jid = confirmed(client, monkeypatch)
    calls = install_research(monkeypatch)
    provider = deep._web_provider()
    timed_out = []
    if kind == "search":
        search = provider.search
        async def one_timeout(query):
            if not timed_out:
                timed_out.append(query)
                raise TimeoutError("private upstream details")
            return await search(query)
        provider.search = one_timeout
    elif kind in {"scrape", "scrape_http", "408", "429", "500", "connection", "content"}:
        async def scrape(url):
            import httpx
            timed_out.append(url)
            if kind.isdigit():
                raise httpx.HTTPStatusError("private upstream details", request=httpx.Request("GET", url),
                    response=httpx.Response(int(kind)))
            if kind == "connection":
                raise httpx.ConnectError("private upstream details")
            if kind == "content":
                return None
            if kind == "scrape_http":
                raise httpx.ReadTimeout("private upstream details")
            raise TimeoutError("private upstream details")
        provider.scrape = scrape
    else:
        async def github(*args, **kwargs):
            timed_out.append(True)
            raise TimeoutError("private upstream details")
        monkeypatch.setattr(github_search, "search_public_repositories", github)
    monkeypatch.setattr(deep, "_web_provider", lambda: provider)
    run_queued("research.deep")
    uncertain_paid = kind not in {"github", "429"}
    assert job(jid).status == ("failed_dead" if uncertain_paid else "succeeded"), job(jid).last_error
    result = dossier(client, vid)
    assert (result["summary"]["review"] is None) is uncertain_paid
    if uncertain_paid:
        assert job(jid).last_error == "research_outcome_uncertain"
        assert not any(call[0] == "review" for call in calls)
        assert len(timed_out) <= (1 if kind == "search" else 4)
        if kind == "search":
            assert calls == [] and len(timed_out) == 1
    assert result["summary"]["usage"]["has_unsettled"] is (kind != "429")
    saved = json.loads(ex.read(result["id"]).summary)
    key, receipt = next((key, receipt) for key, receipt in saved["_runtime"]["operations"].items()
                        if receipt["state"] == ("retryable" if kind == "429" else "uncertain"))
    category = "http" if kind.isdigit() else "connection" if kind == "connection" else "internal" if kind == "content" else "timeout"
    assert receipt["failure"]["category"] == category
    assert bool(receipt["usage"].get("definitive_unspent")) is (kind == "429")
    assert any(getattr(record, "failure", {}).get("category") == category for record in caplog.records)
    assert "private upstream details" not in caplog.text
    before = list(calls), list(timed_out), saved["_runtime"]["operations"]
    payload = {"workspace_id": job(jid).workspace_id, "venture_id": vid, "_job_id": jid, "_job_attempt": 1,
               "_worker": "worker"}
    if kind != "429":
        with pytest.raises(deep.FatalError, match="research_outcome_uncertain"):
            deep._retrieve(result["id"], key, payload, purpose="research.fetch", provider="firecrawl", estimate=1,
                           callback=lambda: pytest.fail("uncertain retrieval was replayed"))
    else:
        replay = deep._retrieve(result["id"], key, payload, purpose="research.fetch", provider="firecrawl", estimate=1,
                                callback=lambda: pytest.fail("rejected retrieval was replayed"))
        assert replay["text"] == ""
    if uncertain_paid:
        with pytest.raises(deep.FatalError, match="research_outcome_uncertain"):
            ex._validate_usage(SimpleNamespace(kind="deep"), saved, None,
                               {"request_id": "new-operation", "source": "platform"})
    assert (calls, timed_out, json.loads(ex.read(result["id"]).summary)["_runtime"]["operations"]) == before


def test_review_timeout_completes_sources_only_and_keeps_rerun_available(client, monkeypatch):
    import asyncio
    from app.core.jobs import lease
    from app.db.models import CreditReservation, Job
    from app.research import execution as ex
    from app.state import db
    from app.worker.main import _run_one
    from tests.security.test_durable_journey_boundary import request

    vid, _, _, jid = confirmed(client, monkeypatch)
    def timeout(messages, options):
        assert options["max_tokens"] == 8192
        assert options["config"].timeout_seconds == 240
        raise TimeoutError("private model error")
    calls = install_research(monkeypatch, callback=timeout)
    run_queued("research.deep")
    assert job(jid).status == "failed_dead" and job(jid).last_error == "research_outcome_uncertain"
    result = dossier(client, vid)
    assert result["summary"]["review_kind"] == "retrieved_sources_only"
    assert result["summary"]["review"] is None and result["completed_at"]
    assert result["summary"]["usage"]["has_unsettled"] is True
    assert result["summary"]["stopped_early"] is True
    assert len([call for call in calls if call[0] == "review"]) == 1
    receipt = json.loads(ex.read(result["id"]).summary)["_runtime"]["operations"]["review"]
    assert receipt["state"] == "uncertain" and not receipt["usage"].get("definitive_unspent")
    with db()() as session:
        hold = session.get(CreditReservation, receipt["usage"]["reservation_id"])
        assert hold.status in {"active", "open", "reconciling"}
        row = session.get(Job, jid)
        row.status, row.run_after, row.scheduled_for = "retry_wait", None, None
        session.commit()
        repeated = lease(session, "replacement-worker", kinds=["research.deep"], n=1)[0]
        session.commit()
    before = list(calls)
    asyncio.run(_run_one(db(), repeated, {jid}))
    assert job(jid).status == "failed_dead" and job(jid).last_error == "research_outcome_uncertain" and calls == before
    rendered = request(client, "GET", f"/ventures/{vid}/research/dossier").json()
    assert rendered["available"] is True
    assert "private model error" not in json.dumps(rendered)
    new_run = request(client, "POST", f"/ventures/{vid}/research/dossiers/{result['id']}/rerun",
                      json={"phase": "all", "request_key": "new-research-run"})
    assert new_run.status_code == 200, new_run.text
    assert new_run.json()["job_id"] != jid


def test_deep_dossier_is_available_in_legacy_download_routes(client, monkeypatch):
    from tests.security.test_durable_journey_boundary import request
    vid, _, _, jid = confirmed(client, monkeypatch)
    install_research(monkeypatch)
    run_queued("research.deep")
    result = dossier(client, vid)
    response = request(client, "GET", f"/ventures/{vid}/research/dossier")
    assert response.status_code == 200, response.text
    meta = response.json()
    assert meta["available"] is True and meta["depth"] == "deep"
    assert meta["scan"]["deep_research"]["ok"] is True
    assert result["id"] in meta["markdown"]
    for suffix in (".md", ".html", ".pdf"):
        response = request(client, "GET", f"/ventures/{vid}/research/dossier{suffix}")
        assert response.status_code == 200, response.text
        if suffix != ".pdf":
            assert result["id"] in response.text


def test_legacy_report_does_not_claim_running_failed_or_outdated_deep_dossier(client, monkeypatch):
    from app.db.supymem_models import ResearchDossier
    from app.research import execution as ex
    from app.state import db
    from tests.security.test_durable_journey_boundary import request

    vid, _, _, jid = confirmed(client, monkeypatch)
    install_research(monkeypatch)
    run_queued("research.deep")
    result = dossier(client, vid)
    for state, completed_at in (("running", None), ("failed", ex.read(result["id"]).completed_at)):
        with db()() as session:
            row = session.get(ResearchDossier, result["id"])
            row.state, row.completed_at = state, completed_at
            session.commit()
        assert request(client, "GET", f"/ventures/{vid}/research/dossier").json()["available"] is False
    with db()() as session:
        row = session.get(ResearchDossier, result["id"])
        row.state, row.completed_at, row.revision_id = "complete", completed_at, None
        session.commit()
    assert request(client, "GET", f"/ventures/{vid}/research/dossier").json()["available"] is False


def test_scrape_targets_prefer_matching_pages_and_dedupe_domains(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    load_company_os_app()
    from app.research.deep import scrape_targets

    def src(i, url, intents=("community",), **evidence):
        return {"id": i, "url": url, "title": "t", "summary": "s",
                "evidence": {"evidence_kind": "search_excerpt", "search_intents": list(intents), **evidence}}

    rows = [src("a", "https://one.example/blog"), src("b", "https://two.example/pricing", ("direct",)),
            src("c", "https://two.example/other", ("direct",)), src("d", "https://three.example/", ("alternatives",)),
            src("e", "https://four.example/", ("direct",), page_verified=True),
            {**src("f", "https://five.example/"), "evidence": {"evidence_kind": "repository_metadata"}}]
    assert [s["id"] for s in scrape_targets(rows, limit=3)] == ["b", "d", "a"]


def test_boundary_retry_filters_titles_urls_and_verified_excerpts(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    load_company_os_app()
    from app.research.deep import _boundary_safe_sources
    marker = "sk-live-AbCdEfGhIjKlMnOpQrStUvWxYz123456"
    safe = {"id": "safe", "title": "Bakery software", "url": "https://example.com", "summary": "Bakery order intake.", "evidence": {}}
    page = {**safe, "id": "page", "evidence": {"additional_excerpts": [{"text": marker, "verified_page": True}]}}
    title = {**safe, "id": "title", "title": marker}
    url = {**safe, "id": "url", "url": "https://example.com/" + marker}
    cleaned = _boundary_safe_sources([safe, page, title, url], "workspace")
    assert [source["id"] for source in cleaned] == ["safe"]
    assert marker not in json.dumps(cleaned)


def test_verification_uses_bounded_pool_and_frozen_receipts(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    load_company_os_app()
    from app.research import deep
    rows = [{"id": f"source_{index}", "url": f"https://vendor{index}.example/pricing", "evidence": {"evidence_kind": "search_excerpt"}}
            for index in range(16)]
    rows[0]["evidence"]["evidence_kind"] = "web"
    runtime = {"_runtime": {}}
    monkeypatch.setattr(deep.ex, "read", lambda did: SimpleNamespace(summary=json.dumps(runtime), budget_calls=60, calls_used=0))
    monkeypatch.setattr(deep, "sources_for", lambda did: rows)
    monkeypatch.setattr(deep.ex, "mutate", lambda did, payload, callback: callback(None, runtime, None))
    barrier = threading.Barrier(4)
    fetched, saved = [], []
    def scrape(did, payload, provider, source):
        fetched.append(source["id"])
        barrier.wait(timeout=5)
        return {"text": "Public product page."}
    monkeypatch.setattr(deep, "_scrape", scrape)
    monkeypatch.setattr(deep, "_store_page", lambda did, payload, sid, result: saved.append(sid))
    deep.verify_top_sources("dossier", {}, SimpleNamespace(scrape=lambda: None))
    assert len(fetched) == len(saved) == 12
    assert set(fetched) == set(runtime["_runtime"]["scrape_targets"])


@pytest.mark.parametrize("code", ["research_outcome_uncertain", "research_call_budget_exhausted"])
def test_verification_stops_on_uncertainty_or_budget(tmp_path, monkeypatch, code):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    load_company_os_app()
    from app.research import deep
    rows = [{"id": f"source_{index}", "url": f"https://vendor{index}.example/pricing", "evidence": {"evidence_kind": "search_excerpt"}}
            for index in range(12)]
    monkeypatch.setattr(deep, "sources_for", lambda did: rows)
    monkeypatch.setattr(deep.ex, "read", lambda did: SimpleNamespace(summary='{"_runtime": {}}', budget_calls=60, calls_used=0))
    monkeypatch.setattr(deep.ex, "mutate", lambda did, payload, callback: callback(None, {"_runtime": {}}, None))
    fetched = []
    def scrape(did, payload, provider, source):
        fetched.append(source["id"])
        raise deep.FatalError(code)
    monkeypatch.setattr(deep, "_scrape", scrape)
    if code == "research_outcome_uncertain":
        with pytest.raises(deep.FatalError, match=code):
            deep.verify_top_sources("dossier", {}, SimpleNamespace(scrape=lambda: None))
    else:
        deep.verify_top_sources("dossier", {}, SimpleNamespace(scrape=lambda: None))
    assert len(fetched) == 4


def test_verification_deadline_prevents_late_paid_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    load_company_os_app()
    from app.research import deep
    row = {"id": "source", "url": "https://vendor.example/pricing", "evidence": {"evidence_kind": "search_excerpt"}}
    monkeypatch.setattr(deep, "sources_for", lambda did: [row])
    monkeypatch.setattr(deep.ex, "mutate", lambda did, payload, callback: callback(None, {"_runtime": {}}, None))
    ticks = iter([0, 70])
    monkeypatch.setattr(deep.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(deep, "_scrape", lambda *args: pytest.fail("late paid fetch"))
    deep.verify_top_sources("dossier", {}, SimpleNamespace(scrape=lambda: None))


def test_verification_reserves_last_raw_call_for_assessment(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    load_company_os_app()
    from app.research import deep
    rows = [{"id": f"source_{index}", "url": f"https://vendor{index}.example/pricing", "evidence": {"evidence_kind": "web"}}
            for index in range(12)]
    runtime = {"_runtime": {"cost_ceiling_micro": 2000000, "raw_calls_reserved": 57}}
    monkeypatch.setattr(deep, "sources_for", lambda did: rows)
    monkeypatch.setattr(deep.ex, "read", lambda did: SimpleNamespace(summary=json.dumps(runtime), budget_calls=60, calls_used=20))
    monkeypatch.setattr(deep.ex, "mutate", lambda did, payload, callback: callback(None, runtime, None))
    fetched = []
    def scrape(did, payload, provider, source):
        fetched.append(source["id"])
        runtime["_runtime"]["raw_calls_reserved"] += 1
        return {"text": "Public page."}
    monkeypatch.setattr(deep, "_scrape", scrape)
    monkeypatch.setattr(deep, "_store_page", lambda *args: None)
    deep.verify_top_sources("dossier", {}, SimpleNamespace(scrape=lambda: None))
    assert len(fetched) == 2 and runtime["_runtime"]["raw_calls_reserved"] == 59


def test_same_host_sources_fill_remaining_verification_slots(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    load_company_os_app()
    from app.research.deep import scrape_targets
    rows = [{"id": f"source_{index}", "url": f"https://vendor.example/page{index}", "evidence": {"evidence_kind": "search_excerpt"}}
            for index in range(16)]
    assert len(scrape_targets(rows)) == 12


def test_assessment_trims_large_material_without_modifying_saved_sources(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    load_company_os_app()
    from app.research import deep
    rows = [{"id": f"source_{index}", "title": "Public software", "url": f"https://vendor{index}.example/",
             "summary": "Public bakery order software. " * 60,
             "evidence": {"page_verified": True, "additional_excerpts": [
                 {"text": f"Public bakery product passage {number}. " * 140, "verified_page": number == 0} for number in range(5)]}}
            for index in range(50)]
    original = json.dumps(rows)
    monkeypatch.setattr(deep.ex, "read", lambda did: SimpleNamespace(summary='{"_runtime": {}}', workspace_id="workspace"))
    def synthesize(did, payload, **kwargs):
        content = kwargs["messages"][-1]["content"]
        assert len(content) <= 180000
        sources = json.loads(content)["sources"]
        assert len(sources) == 50 and all(source["excerpts"] for source in sources)
        assert all(any("passage 0" in text for text in source["excerpts"]) for source in sources)
        return {"overview": "Bounded review."}
    monkeypatch.setattr(deep, "synthesize", synthesize)
    assert deep._review_sources("dossier", {}, {"problem": "Bakery order intake"}, rows)["overview"] == "Bounded review."
    assert json.dumps(rows) == original
    marker = "sk-live-AbCdEfGhIjKlMnOpQrStUvWxYz123456"
    unsafe = {**rows[0], "summary": "Public bakery. " * 1000 + marker, "evidence": {}}
    assert deep._boundary_safe_sources([unsafe], "workspace", text_budget=100) == []


def test_repository_disclaimer_stays_on_item_not_gap(client, monkeypatch):
    vid, _, _, jid = confirmed(client, monkeypatch)
    install_research(monkeypatch)
    run_queued("research.deep")
    assert job(jid).status == "succeeded"
    result = dossier(client, vid)
    assert not any("Repository license metadata" in gap for gap in result["summary"]["coverage_gaps"])
    from app.research.deep import sources_for
    repository = next(source for source in sources_for(result["id"]) if source["evidence"].get("repository"))
    assert "dependency licenses" in repository["evidence"]["license_review"]


def test_assessment_bounds_quote_choices_for_long_words(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    load_company_os_app()
    from app.research.deep import _boundary_safe_sources
    from app.research.selection import quote_options
    row = {"id": "source", "summary": "Public bakery software. " * 60,
           "evidence": {"additional_excerpts": [
               {"text": " ".join(chr(97 + number) * 501 + str(index) for index in range(11))}
               for number in range(5)]}}
    cleaned = _boundary_safe_sources([row], "workspace")
    assert len(quote_options(cleaned)["source"]) <= 24


def test_blocked_page_retains_local_evidence_without_founder_gap(client, monkeypatch):
    vid, _, _, jid = confirmed(client, monkeypatch)
    def review(messages, options):
        sources = json.loads(messages[-1]["content"])["sources"]
        assert len(sources) == 1
        body = {"overview": "Safe repository evidence.", "observations": [{"source_id": sources[0]["id"], "quote_index": 0}],
                "assessments": [], "service_checks": [], "competitor_checks": [], "coverage_gaps": [], "proposed_tests": []}
        from openai.types import CompletionUsage
        return SimpleNamespace(model="DeepSeek-V4-Pro", service_tier="default",
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(body)))],
            usage=CompletionUsage(prompt_tokens=500, completion_tokens=100, total_tokens=600,
                prompt_tokens_details={"cached_tokens": 0}, completion_tokens_details={"reasoning_tokens": 0}))
    install_research(monkeypatch, callback=review)
    from app.research import deep
    provider = deep._web_provider()
    marker = "sk-live-AbCdEfGhIjKlMnOpQrStUvWxYz123456"
    async def scrape(url):
        return "Public bakery software. " + marker
    provider.name, provider.scrape = "firecrawl", scrape
    monkeypatch.setattr(deep, "_web_provider", lambda: provider)
    run_queued("research.deep")
    assert job(jid).status == "succeeded", job(jid).last_error
    result = dossier(client, vid)
    assert result["summary"]["review"] is not None
    assert result["summary"]["review"]["observations"][0]["source_id"] == result["findings"]["oss_repo"][0]["id"]
    assert marker in result["findings"]["source"][0]["additional_excerpts"][0]["text"]
    assert not any("sent for assessment" in gap or "kept out" in gap for gap in result["summary"]["coverage_gaps"])
    assert result["summary"]["usage"]["has_unsettled"] is False


def test_empty_queries_create_one_plain_gap_without_internal_names(client, monkeypatch):
    vid, _, _, jid = confirmed(client, monkeypatch)
    install_research(monkeypatch, empty=True)
    run_queued("research.deep")
    assert job(jid).status == "succeeded", job(jid).last_error
    gaps = dossier(client, vid)["summary"]["coverage_gaps"]
    grouped = [gap for gap in gaps if "searches found no pages specific to your idea" in gap]
    assert not grouped
    assert not any("No sources returned for" in gap or "No sources qualified as evidence for" in gap for gap in gaps)
    assert not any("job_tool" in gap or "customer_voice" in gap for gap in gaps)

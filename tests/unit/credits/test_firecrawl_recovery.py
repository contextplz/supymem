"""Real worker, durable search receipts and SQLite ledger; provider HTTP is offline."""
import asyncio
import json
from datetime import timedelta

import httpx
import pytest
from core.clock import as_utc, utcnow
from sqlalchemy import select


@pytest.mark.parametrize("url,page_text", [
    ("https://freight.example/contact", "Our dispatch team handles freight bookings."),
    ("https://garden.example/community", "We share seasonal planting advice."),
])
@pytest.mark.parametrize("status", [500, 503])
@pytest.mark.parametrize("mode", ["hosted", "local"])
def test_failed_self_hosted_scrape_releases_without_charging(account, monkeypatch, caplog, url, page_text, status, mode):
    monkeypatch.setattr("core.ssrf.socket.getaddrinfo", lambda *args, **kwargs: [
        (0, 0, 0, "", ("93.184.216.34", 0))])
    from app.credits import metered
    from app.credits.accounts import get_account_by_id
    from app.credits.ledger import verify_chain
    from app.db.models import CreditReservation
    from app.signals import research
    from app.state import db

    monkeypatch.setenv("FIRECRAWL_SELF_HOSTED_URL", "https://crawl.example")
    monkeypatch.setenv("FIRECRAWL_SELF_HOSTED_TOKEN", "offline-token")
    monkeypatch.setenv("SUPYMEM_MODE", mode)
    if mode == "local":
        from app.core.local_secrets import update_settings
        update_settings("search", {"provider": "firecrawl", "firecrawl_url": "https://crawl.example"},
                        {"firecrawl_key": "offline-token"})
    monkeypatch.setattr(research, "_openai_web_search", lambda: None)
    original, original_sync = httpx.AsyncClient, httpx.Client
    requests = []

    def respond(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, headers={"content-type": "text/plain"}, stream=httpx.ByteStream(
                ("User-agent: *\nAllow: /" if request.url.path == "/robots.txt" else page_text).encode()))
        return httpx.Response(status)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original_sync(transport=httpx.MockTransport(respond), **kwargs))
    provider = research.HttpResearchProvider("firecrawl", "")
    before = get_account_by_id(account.id)
    call = metered.call(lambda: provider.scrape(url + "?session=private-value"),
        purpose="gtm.discovery.public_page", estimate_micro=117, account_id=account.id,
        provider="firecrawl", request_id=url)
    with pytest.raises(httpx.HTTPStatusError) as failure:
        asyncio.run(call)
    assert failure.value.response.status_code == status
    after = get_account_by_id(account.id)
    assert after.spent_micro == before.spent_micro
    assert after.reserved_micro == before.reserved_micro
    with db()() as session:
        holds = list(session.scalars(select(CreditReservation).where(CreditReservation.account_id == account.id)))
        if mode == "local":
            assert holds == []
        else:
            assert len(holds) == 1 and holds[0].status == "released"
        assert verify_chain(account.id, transaction=session) == (True, None)
    assert len(requests) == 1 and requests[0].url.host == "crawl.example"
    failures = [record for record in caplog.records if record.message.startswith("firecrawl_self_hosted_error")]
    assert len(failures) == 1 and failures[0].source_url == url
    assert "private-value" not in caplog.text
    assert "credits.outcome_reconciliation_required" not in caplog.text


@pytest.mark.parametrize("domain", ["warehouse_dispatch", "family_gardening"])
def test_local_unspent_model_receipt_needs_no_token_usage_and_keeps_scope(account, monkeypatch, domain):
    from app.credits import gate, local_meter
    from app.credits.errors import CreditsUnavailable

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    with gate.charge_sync(workspace_id="workspace", venture_id=domain, purpose="firstlook.copy",
            provider="azure_foundry", model="DeepSeek-V4-Pro", estimate_micro=100,
            request_id=domain, payer="founder") as meter:
        meter.protect_outcome()
        meter.confirm_unspent()
        usage = gate.pending_model_receipt(meter) | {"definitive_unspent": True}
    assert gate.reconcile_receipt(usage, {"workspace_id": "workspace", "venture_id": domain}) == 0
    assert local_meter.snapshot()["month_cost_usd_micro"] == 0
    with pytest.raises(CreditsUnavailable):
        gate.reconcile_receipt(usage, {"workspace_id": "other", "venture_id": domain})
    with pytest.raises(CreditsUnavailable):
        gate.reconcile_receipt(usage | {"purpose": "content.writer"})
    with gate.charge_sync(workspace_id="workspace", venture_id=domain, purpose="firstlook.copy",
            provider="azure_foundry", model="DeepSeek-V4-Pro", estimate_micro=100,
            request_id=domain + ":unknown", payer="founder") as meter:
        meter.protect_outcome()
        unknown = gate.pending_model_receipt(meter) | {"definitive_unspent": True}
    with pytest.raises(CreditsUnavailable):
        gate.reconcile_receipt(unknown)


@pytest.mark.parametrize("status,retry_after,delay", [(429, "4", 4), (429, "900", 60), (503, "12", 12)])
def test_searches_then_rejection_release_and_resume_without_recharging(account, monkeypatch, caplog, status, retry_after, delay):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    from core import clock

    from app.core.jobs import enqueue, lease
    from app.credits.accounts import get_account_by_id
    from app.credits.ledger import entries_for_account, verify_chain
    from app.db.models import CreditReservation, Job
    from app.db.supymem_models import ResearchDossier, ResearchFinding
    from app.research import deep, execution
    from app.signals import research
    from app.state import db
    from app.worker import main as worker

    now = [utcnow()]
    monkeypatch.setattr(clock, "_clock", lambda: now[0])
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("COMPANY_OS_ENV", "local")
    monkeypatch.setattr(research, "_pace_firecrawl", lambda key: None)
    requests = []
    def respond(request):
        requests.append(json.loads(request.content)["query"])
        if len(requests) == 13:
            return httpx.Response(status, headers={"Retry-After": retry_after})
        return httpx.Response(200, json={"success": True, "data": {"web": [{
            "url": f"https://example.org/bakery/{len(requests)}", "title": "Bakery order management software",
            "description": "Bakery order management software. Plans cost $12 monthly."}]}})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    provider = research.HttpResearchProvider("firecrawl", "offline-key")
    venture_id = account.attribution_venture_id
    workspace_id = "ws_wallet:" + venture_id
    factory = db()
    with factory() as session:
        job_id = enqueue(session, "research.deep", {"workspace_id": workspace_id, "venture_id": venture_id},
            workspace_id=workspace_id, venture_id=venture_id, max_attempts=3)
        dossier_id = execution.identifier(job_id)
        session.add(ResearchDossier(id=dossier_id, workspace_id=workspace_id, venture_id=venture_id,
            kind="deep", state="running", budget_calls=20, calls_used=0,
            summary=json.dumps({"_runtime": {"job_id": job_id, "operations": {}}}), created_at=utcnow()))
        session.commit()

    async def dispatch(kind, payload):
        assert kind == "research.deep"
        for index in range(13):
            query = f"bakery order management {index}"
            result = await asyncio.to_thread(deep._search, dossier_id, payload, provider,
                f"search_{index}", query, "bakery order management")
            deep._store_sources(dossier_id, payload, result, query, "direct")
        execution.mutate(dossier_id, payload, lambda row, doc, session: setattr(row, "state", "completed"))
    monkeypatch.setattr(worker, "dispatch", dispatch)

    def run_attempt(moment):
        now[0] = moment
        with factory() as session:
            claimed = lease(session, "firecrawl-worker", kinds=["research.deep"], n=1)
            session.commit()
        assert len(claimed) == 1
        asyncio.run(worker._run_one(factory, claimed[0], set()))

    run_attempt(utcnow())
    operations = execution.journal.document(dossier_id)["_runtime"]["operations"]
    rejected = operations["search_12"]
    assert rejected["state"] == "retryable" and rejected["usage"]["definitive_unspent"] is True
    assert rejected["usage"]["credits_micro"] == 0 and rejected["usage"]["settled"] is True
    assert rejected["failure"]["http_status"] == status
    paid = sum(operations[f"search_{index}"]["usage"]["credits_micro"] for index in range(12))
    balance = get_account_by_id(account.id)
    assert balance.spent_micro == account.spent_micro + paid and balance.reserved_micro == 0
    spends = [entry.id for entry in entries_for_account(account.id)["items"] if entry.kind == "spend"]
    assert len(spends) == 12 and len(requests) == 13
    with factory() as session:
        row = session.get(Job, job_id)
        assert row.status == "retry_wait" and row.attempts == 1
        retry_at = as_utc(row.run_after)
        assert retry_at - as_utc(row.finished_at or row.created_at) >= timedelta(seconds=delay)
        assert retry_at - utcnow() <= timedelta(seconds=delay)
        assert session.get(CreditReservation, rejected["usage"]["reservation_id"]).status == "released"
        dossier = session.get(ResearchDossier, dossier_id)
        assert dossier.state == "running" and dossier.completed_at is None
        assert len(session.scalars(select(ResearchFinding).where(ResearchFinding.dossier_id == dossier_id)).all()) == 12

    run_attempt(retry_at + timedelta(seconds=1))
    assert requests == [f"bakery order management {index}" for index in range(13)] + ["bakery order management 12"]
    resumed = execution.journal.document(dossier_id)["_runtime"]["operations"]
    for index in range(12):
        assert resumed[f"search_{index}"] == operations[f"search_{index}"]
    assert resumed["search_12"]["state"] == "completed" and resumed["search_12"]["attempt"] == 2
    balance = get_account_by_id(account.id)
    assert balance.spent_micro == account.spent_micro + paid + resumed["search_12"]["usage"]["credits_micro"]
    assert balance.reserved_micro == 0
    final_spends = [entry.id for entry in entries_for_account(account.id)["items"] if entry.kind == "spend"]
    assert len(final_spends) == 13 and set(spends) <= set(final_spends)
    assert verify_chain(account.id) == (True, None)
    with factory() as session:
        assert session.get(Job, job_id).status in {"succeeded", "done"}
        dossier = session.get(ResearchDossier, dossier_id)
        assert dossier.state == "completed" and dossier.calls_used == 14
        assert len(session.scalars(select(ResearchFinding).where(ResearchFinding.dossier_id == dossier_id)).all()) == 13
    assert "credits.outcome_reconciliation_required" not in caplog.text

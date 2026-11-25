"""Actual signed app + jobs + credit ledger + persisted research evidence."""
from openai.types import CompletionUsage
import asyncio
import json

import pytest

from sqlalchemy import select

from tests.security.test_connector_oauth_boundary import client as client
from tests.security.test_durable_journey_boundary import confirm_idea, request, run_queued, venture
from tests.idea_fixtures import install_provider
from tests.research_fixtures import install_research


def confirmed(client, monkeypatch):
    # Legacy research fixtures exercise their original receipt sequence. The
    # dedicated prior-art founder-flow test enables and records its adapters.
    monkeypatch.setenv("RESEARCH_PRIOR_ART_ENABLED", "0")
    install_provider(monkeypatch)
    vid, wid, sid = venture(client)
    result = confirm_idea(client, vid, sid)
    return vid, wid, sid, result["dossier_job_id"]


def job(jid):
    from app.db.models import Job
    from app.state import db
    with db()() as session:
        row = session.get(Job, jid)
        session.expunge(row)
        return row


def dossier(client, vid):
    items = request(client, "GET", f"/ventures/{vid}/research/dossiers").json()["items"]
    assert len(items) == 1
    return request(client, "GET", f"/ventures/{vid}/research/dossiers/{items[0]['id']}").json()


def saved_gap_items(result):
    from app.db.supymem_models import ResearchDossier
    from app.state import db
    with db()() as session:
        return json.loads(session.get(ResearchDossier, result["id"]).summary)["gap_items"]


def test_confirmed_research_persists_real_sources_and_cited_assessments(client, monkeypatch):
    from app.db.supymem_models import ExecutionPlan, ThirdPartyService
    from app.state import db
    vid, wid, sid, jid = confirmed(client, monkeypatch)
    calls = install_research(monkeypatch)
    run_queued("research.deep")
    assert job(jid).status == "succeeded", job(jid).last_error
    result = dossier(client, vid)
    assert result["kind"] == "deep" and result["state"] == "partial" and result["calls_used"] == 12
    assert len(calls) == 12 and calls[-1][1]["workspace_id"] == wid
    sources = [f for group in result["findings"].values() for f in group]
    assert len(sources) == 2 and all(f["observed_at"] for f in sources)
    assert result["findings"]["source"][0]["search_intents"]
    assert result["summary"]["review"]["observations"][0]["source_id"] in {f["id"] for f in sources}
    assert result["summary"]["usage"]["credits_micro"] > 140000
    assert result["summary"]["evidence_dimensions"]["competition"]["status"] == "supporting_leads"
    assert result["summary"]["evidence_dimensions"]["willingness_to_pay"]["status"] == "unknown"
    assert result["summary"]["usage"]["has_unsettled"] is False
    repo = result["findings"]["oss_repo"][0]
    assert repo["license"] == "MIT" and repo["license_ok"] is None
    assert "_runtime" not in json.dumps(result) and "reservation_id" not in json.dumps(result)
    with db()() as session:
        assert not session.scalars(select(ThirdPartyService)).all()
        assert not session.scalars(select(ExecutionPlan)).all()  # Never auto-compile.
    export = request(client, "GET", f"/ventures/{vid}/research/dossiers/{result['id']}/export")
    assert export.status_code == 200 and "example.com/bakery" in export.text
    assert request(client, "GET", f"/ventures/{vid}/research/dossiers/{result['id']}", "usr_bob").status_code == 404


def test_research_review_uses_the_founders_venture_identity(client, monkeypatch):
    from app.db.supymem_models import ResearchDossier
    from app.research.selection import messages_for
    from app.state import db

    vid, _, _, jid = confirmed(client, monkeypatch)
    install_research(monkeypatch)
    run_queued("research.deep")
    assert job(jid).status == "succeeded"
    result = dossier(client, vid)
    with db()() as session:
        saved = session.get(ResearchDossier, result["id"])
        snapshot = json.loads(saved.summary)["_runtime"]["snapshot"]

    assert snapshot["venture_name"] == "Bakery"
    messages = messages_for(snapshot, [{"id": "source_1", "summary": "Bakery order software.", "evidence": {}}])
    assert json.loads(messages[-1]["content"])["snapshot"]["venture_name"] == "Bakery"
    assert "Supymem is the platform" in messages[0]["content"]
    assert "_runtime" not in json.dumps(result)


def test_deep_confirmation_and_nested_finding_ownership(client, monkeypatch):
    install_provider(monkeypatch)
    vid, _, sid = venture(client)
    before = request(client, "POST", f"/ventures/{vid}/research/dossier/regenerate")
    # Unconfirmed idea: a needs-you card offering an explicit, confirmable jump; no research is queued.
    assert before.status_code == 200 and before.json()["kind"] == "needs_you"
    assert "idea.review" in [a.get("action") for a in before.json()["actions"]]  # chat-first: the review opens in chat
    from app.db.models import Job
    from app.state import db
    with db()() as session:
        assert not session.scalars(select(Job).where(Job.kind == "research.deep", Job.venture_id == vid)).all()
    confirm_idea(client, vid, sid)
    install_research(monkeypatch)
    run_queued("research.deep")
    result = dossier(client, vid)
    other, _, _ = venture(client)  # Same founder, sibling venture is also denied.
    fid = result["findings"]["source"][0]["id"]
    assert request(client, "PATCH", f"/ventures/{other}/research/findings/{fid}", json={"founder_flag": "exclude"}).status_code == 404
    assert request(client, "GET", f"/ventures/{other}/research/dossiers/{result['id']}/export").status_code == 404
    assert request(client, "POST", f"/ventures/{other}/research/dossiers/{result['id']}/rerun", json={}).status_code == 404
    assert dossier(client, vid)["findings"]["source"][0]["founder_flag"] is None
    from app.research.service import export_markdown, set_finding_flag

    with pytest.raises(KeyError):
        set_finding_flag(other, fid, "exclude")
    with pytest.raises(KeyError):
        export_markdown(other, result["id"])
    assert request(client, "PATCH", f"/ventures/{vid}/research/findings/{fid}", json={"founder_flag": "pin"}).status_code == 200
    rerun = request(client, "POST", f"/ventures/{vid}/research/dossiers/{result['id']}/rerun", json={"phase": "all"})
    assert rerun.status_code == 200 and job(rerun.json()["job_id"]).status == "scheduled"
    repeated = request(client, "POST", f"/ventures/{vid}/research/dossiers/{result['id']}/rerun", json={})
    assert repeated.json()["job_id"] == rerun.json()["job_id"]
    for phase in ("competitors", "pricing", "demand"):
        focused = request(client, "POST", f"/ventures/{vid}/research/dossiers/{result['id']}/rerun",
                          json={"phase": phase})
        assert focused.status_code == 200
        assert job(focused.json()["job_id"]).status == "scheduled"
        assert json.loads(job(focused.json()["job_id"]).payload_json)["phase"] == phase
    assert dossier(client, vid)["findings"]["source"][0]["founder_flag"] == "pin"


def test_no_results_never_fabricates_competitors_or_spends_model_tokens(client, monkeypatch):
    vid, _, _, jid = confirmed(client, monkeypatch)
    calls = install_research(monkeypatch, empty=True)
    run_queued("research.deep")
    assert job(jid).status == "succeeded"
    result = dossier(client, vid)
    assert result["findings"] == {} and result["summary"]["review"] is None
    assert len(calls) == 12 and "No usable public source"  # includes the one shorter zero-source fallback search in " ".join(result["summary"]["coverage_gaps"])


def test_keyless_search_rate_limits_become_coverage_gaps(client, monkeypatch):
    from ddgs.exceptions import RatelimitException
    from app.research import deep, github_search

    vid, _, _, jid = confirmed(client, monkeypatch)
    calls = []

    class KeylessProvider:
        name = "duckduckgo"

        async def search(self, query):
            calls.append(query)
            raise RatelimitException("public engine refused the query")

    async def empty_github(query, workspace_id, limit=5):
        return {"items": [], "provider": "github", "credential_source": "anonymous", "incomplete": False}

    monkeypatch.setattr(deep, "_web_provider", lambda: KeylessProvider())
    monkeypatch.setattr(github_search, "search_public_repositories", empty_github)
    run_queued("research.deep")
    assert job(jid).status == "succeeded", job(jid).last_error
    result = dossier(client, vid)
    assert any(item["kind"] == "system_request_limit" and "Keyless web search was rate limited" in item["text"]
               for item in saved_gap_items(result))
    assert "Keyless web search was rate limited" not in json.dumps(result)
    assert len(calls) == 10 and result["findings"] == {}


def test_research_queries_use_the_problem_and_customer_not_long_offer_prose():
    from app.research.deep import queries_for

    snapshot = {
        "offer": "A manual concierge service: " + "generated workflow prose " * 30,
        "problem": "Small agencies lose time chasing overdue invoices without damaging client relationships.",
        "customer": "Owners of design and software agencies with 2 to 20 employees.",
    }
    queries = [query for _, query in queries_for(snapshot, "all")]
    assert len(queries) == 10
    assert all("manual concierge service" not in query for query in queries)
    assert all(len(query) < 240 for query in queries)
    assert all("agenc" in query for query in queries)
    assert sum("overdue invoices" in query for query in queries) == 10  # category and customer-voice discovery also retains the job to investigate


def test_targeted_research_phases_have_distinct_bounded_search_intentions():
    from app.research.deep import queries_for

    snapshot = {"problem": "Security teams review AI agent permissions", "customer": "enterprise security leads"}
    pricing = queries_for(snapshot, "pricing")
    demand = queries_for(snapshot, "demand")
    assert len(pricing) == len(demand) == 3
    assert {kind for kind, _ in pricing} == {"pricing", "buyer_budget", "customer_voice"}
    assert {kind for kind, _ in demand} == {"customer_request", "workaround", "customer_voice"}
    assert all("agent permissions" in query for _, query in pricing + demand)


def test_unknown_citation_retains_source_only_partial_report_and_single_spend(client, monkeypatch):
    from app.research.service import review_quote
    vid, _, _, jid = confirmed(client, monkeypatch)
    calls = install_research(monkeypatch, invalid=True)
    run_queued("research.deep")
    assert job(jid).status == "succeeded", job(jid).last_error
    result = dossier(client, vid)
    assert result["state"] == "partial" and result["findings"]
    assert result["summary"]["review"] is None and result["summary"]["usage"]["credits_micro"] > 140000
    assert result["summary"]["review_kind"] == "retrieved_sources_only"
    assert any(op["key"] == "review" and op["error_code"] == "research_invalid_output"
               for op in result["summary"]["operations"])
    assert any(item["kind"] == "system_assessment" and "model assessment failed evidence validation" in item["text"].lower()
               for item in saved_gap_items(result))
    assert "model assessment failed evidence validation" not in json.dumps(result).lower()
    assert "rsf_foreign" not in json.dumps(result) and len(calls) == 12  # captured paid requests are never auto-repaired
    exported = request(client, "GET", f"/ventures/{vid}/research/dossiers/{result['id']}/export")
    assert exported.status_code == 200
    assert "No completed assessment is available" not in exported.text
    assert "What we found" in exported.text and "Source:" in exported.text
    assert "rsf_foreign" not in exported.text
    # The founder can explicitly review the saved evidence without repeating
    # retrieval; the invalid assessment never becomes a cited claim.
    quote = review_quote(result["id"])
    install_research(monkeypatch)
    rerun = request(client, "POST", f"/ventures/{vid}/research/dossiers/{result['id']}/rerun",
                    json={"phase": "review", "source_digest": quote["source_digest"]})
    assert rerun.status_code == 200, rerun.text
    run_queued("research.deep")
    from app.research.execution import identifier
    reviewed = request(client, "GET", f"/ventures/{vid}/research/dossiers/{identifier(rerun.json()['job_id'])}").json()
    assert reviewed["id"] != result["id"] and reviewed["summary"]["review"] is not None


def test_blocked_verified_page_is_dropped_once_without_loosening_gateway(client, monkeypatch):
    from types import SimpleNamespace
    from app.db.models import BoundaryLedgerEntry
    from app.research import deep
    from app.state import db

    vid, wid, _, jid = confirmed(client, monkeypatch)
    marker = "sk-live-AbCdEfGhIjKlMnOpQrStUvWxYz123456"
    accepted = []
    def completion(messages, options):
        assert marker not in json.dumps(messages)
        sources = json.loads(messages[-1]["content"])["sources"]
        assert len(sources) == 1
        accepted.append(sources[0]["id"])
        body = {"overview": "One safe repository source remained.",
                "observations": [{"source_id": sources[0]["id"], "quote_index": 0}],
                "assessments": [], "service_checks": [], "competitor_checks": [],
                "coverage_gaps": [], "proposed_tests": []}
        return SimpleNamespace(model="DeepSeek-V4-Pro", service_tier="default", choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(body)))],
            usage=CompletionUsage(prompt_tokens=500, completion_tokens=100, total_tokens=600,
            prompt_tokens_details={"cached_tokens": 0}, completion_tokens_details={"reasoning_tokens": 0}))
    calls = install_research(monkeypatch, callback=completion)
    provider = deep._web_provider()
    async def scrape(url):
        return "Bakery order software. " + marker
    provider.scrape = scrape
    monkeypatch.setattr(deep, "_web_provider", lambda: provider)
    run_queued("research.deep")
    assert job(jid).status == "succeeded", job(jid).last_error
    result = dossier(client, vid)
    assert result["state"] == "partial" and result["summary"]["usage"]["has_unsettled"] is False
    assert len(accepted) == 1 and sum(kind == "review" for kind, _ in calls) == 1
    assert marker in result["findings"]["source"][0]["additional_excerpts"][0]["text"]  # remains local evidence
    observation = result["summary"]["review"]["observations"][0]
    assert observation["source_id"] == result["findings"]["oss_repo"][0]["id"]
    operations = {op["key"]: op for op in result["summary"]["operations"]}
    assert operations["review"]["error_code"] == "research_boundary_blocked"
    assert operations["review-boundary-retry"]["state"] == "completed"
    assert any(item["kind"] == "system_unsent_material" and "kept out" in item["text"]
               for item in saved_gap_items(result))
    assert "kept out" not in json.dumps(result)
    with db()() as session:
        rows = session.scalars(select(BoundaryLedgerEntry).where(
            BoundaryLedgerEntry.workspace_id == wid, BoundaryLedgerEntry.purpose == "research.deep")).all()
        assert len(rows) == 2 and [row.blocked for row in rows] == [True, False]
        assert rows[1].trace_id.endswith(":review-boundary-retry")
        assert marker not in json.dumps([row.block_reason for row in rows])


def test_second_boundary_refusal_keeps_partial_success_and_replay_never_spends(client, monkeypatch):
    from app.core.jobs import lease
    from app.db.models import BoundaryLedgerEntry, Job
    from app.research import selection
    from app.state import db
    from app.worker.main import _run_one

    vid, wid, _, jid = confirmed(client, monkeypatch)
    calls = install_research(monkeypatch)
    original = selection.messages_for
    marker = "sk-live-AbCdEfGhIjKlMnOpQrStUvWxYz123456"
    monkeypatch.setattr(selection, "messages_for", lambda snapshot, sources: original({**snapshot, "untrusted_note": marker}, sources))
    run_queued("research.deep")
    assert job(jid).status == "succeeded" and job(jid).last_error is None
    result = dossier(client, vid)
    assert result["state"] == "partial" and result["summary"]["review"] is None
    assert result["summary"]["review_kind"] == "retrieved_sources_only"
    assert result["findings"] and result["summary"]["usage"]["has_unsettled"] is False
    assert not any(kind == "review" for kind, _ in calls)
    operations = {op["key"]: op for op in result["summary"]["operations"]}
    assert operations["review"]["error_code"] == operations["review-boundary-retry"]["error_code"] == "research_boundary_blocked"
    with db()() as session:
        rows = session.scalars(select(BoundaryLedgerEntry).where(
            BoundaryLedgerEntry.workspace_id == wid, BoundaryLedgerEntry.purpose == "research.deep")).all()
        assert len(rows) == 2 and all(row.blocked for row in rows)
        row = session.get(Job, jid)
        row.status, row.run_after, row.scheduled_for = "retry_wait", None, None
        session.commit()
        replay = lease(session, "replacement-worker", kinds=["research.deep"], n=1)[0]
        session.commit()
    asyncio.run(_run_one(db(), replay, {jid}))
    assert job(jid).status == "succeeded"
    assert dossier(client, vid)["summary"]["usage"] == result["summary"]["usage"]
    assert not any(kind == "review" for kind, _ in calls)


def test_pending_failed_and_outdated_dossier_reads_show_status_without_verdict(client, monkeypatch):
    from app.core.jobs import RetryableError
    from app.db.models import Job
    from app.db.supymem_models import IdeaSession
    from app.research import deep
    from app.state import db

    vid, _, sid, jid = confirmed(client, monkeypatch)
    install_research(monkeypatch)
    def busy_provider(*args, **kwargs):
        raise RetryableError("research_provider_rate_limited")
    monkeypatch.setattr(deep, "_search", busy_provider)
    run_queued("research.deep")
    pending = dossier(client, vid)
    assert pending["state"] == "running" and pending["summary"]["verdict"]["verdict"] == "in_progress"
    assert pending["summary"]["verdict"]["why_not"] == [] and pending["summary"]["verdict"]["next_step"] == ""
    with db()() as session:
        session.get(Job, jid).status = "failed_dead"
        session.commit()
    failed = dossier(client, vid)
    assert failed["state"] == "failed" and failed["summary"]["verdict"]["verdict"] == "failed"
    with db()() as session:
        session.get(IdeaSession, sid).current_revision_id = "changed-revision"
        session.commit()
    outdated = dossier(client, vid)
    assert outdated["state"] == "outdated" and outdated["summary"]["verdict"]["verdict"] == "outdated"
    assert outdated["summary"]["verdict"]["why_not"] == []


def test_dossier_history_reuses_idea_and_credit_receipts_without_changing_public_output(client, monkeypatch):
    from sqlalchemy import event
    from app.db.models import CreditReservation, Job
    from app.db.supymem_models import ResearchDossier
    from app.research.service import list_dossiers
    from app.state import db
    from core.clock import utcnow
    vid, wid, _, _ = confirmed(client, monkeypatch)
    statements = []
    with db()() as session:
        job = session.query(Job).filter(Job.venture_id == vid, Job.kind == "research.deep").first()
        reservation = session.query(CreditReservation).filter(CreditReservation.workspace_id == wid).first()
        runtime = {"job_id": job.id, "operations": {"review": {"state": "calling", "usage": {"reservation_id": reservation.id}}}}
        for index in range(4):
            session.add(ResearchDossier(id=f"history_{index}", workspace_id=wid, venture_id=vid, kind="deep", state="running",
                                       summary=json.dumps({"_runtime": runtime}), created_at=utcnow()))
        session.commit()
        engine = session.get_bind()
    def record(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)
    event.listen(engine, "before_cursor_execute", record)
    try:
        reports = list_dossiers(vid)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert len(reports) == 4
    assert all(report["summary"]["usage"]["has_unsettled"] for report in reports)
    assert len(statements) <= 5


def test_replayed_job_uses_receipts_without_second_provider_spend(client, monkeypatch):
    from app.core.jobs import lease
    from app.db.models import Job
    from app.state import db
    from app.worker.main import _run_one
    vid, _, _, jid = confirmed(client, monkeypatch)
    calls = install_research(monkeypatch)
    run_queued("research.deep")
    before = dossier(client, vid)["summary"]["usage"]
    with db()() as session:
        row = session.get(Job, jid)
        row.status, row.run_after, row.scheduled_for = "retry_wait", None, None
        session.commit()
        repeated = lease(session, "replacement-worker", kinds=["research.deep"], n=1)[0]
        session.commit()
    asyncio.run(_run_one(db(), repeated, {jid}))
    assert job(jid).status == "succeeded" and len(calls) == 12
    assert dossier(client, vid)["summary"]["usage"] == before


def test_uncited_assessments_preserve_cited_partial_report_and_single_spend(client, monkeypatch):
    from types import SimpleNamespace
    from app.core.jobs import lease
    from app.db.models import Job
    from app.state import db
    from app.worker.main import _run_one
    vid, _, _, jid = confirmed(client, monkeypatch)

    def completion(messages, options):
        source = json.loads(messages[-1]["content"])["sources"][0]["id"]
        body = {"overview": "Unsupported market claim.", "observations": [],
            "assessments": [
                {"kind": "inference", "category": "competitor_alternative", "citations": [{"source_id": source, "quote_index": 0}], "assessment": "A possible existing alternative to compare."},
                {"kind": "inference", "category": "competitor_funded", "citations": [], "assessment": "Unsupported market claim."},
                {"kind": "inference", "category": "competitor_accelerator", "citations": [], "assessment": "Unsupported market claim."}],
            "service_checks": [], "coverage_gaps": [], "proposed_tests": []}
        return SimpleNamespace(model="DeepSeek-V4-Pro", service_tier="default", choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(body)))],
            usage=CompletionUsage(prompt_tokens=500, completion_tokens=250, total_tokens=750,
            prompt_tokens_details={"cached_tokens": 0}, completion_tokens_details={"reasoning_tokens": 0}))

    calls = install_research(monkeypatch, callback=completion)
    run_queued("research.deep")
    assert job(jid).status == "succeeded", job(jid).last_error
    before = dossier(client, vid)
    assert before["state"] == "partial" and len(calls) == 12
    review = before["summary"]["review"]
    assert len(review["assessments"]) == 1 and len(review["observations"]) == 1
    assert "Unsupported market claim" not in json.dumps(before)
    assert any(item["kind"] == "system_assessment" and "2 research assessments lacked source citations" in item["text"]
               for item in saved_gap_items(before))
    assert "2 research assessments lacked source citations" not in json.dumps(before)
    assert not any("competitor_funded" in gap for gap in before["summary"]["coverage_gaps"])
    assert before["summary"]["usage"]["has_unsettled"] is False
    exported = request(client, "GET", f"/ventures/{vid}/research/dossiers/{before['id']}/export")
    assert "2 research assessments lacked source citations" not in exported.text and "Unsupported market claim" not in exported.text
    with db()() as session:
        row = session.get(Job, jid)
        row.status, row.run_after, row.scheduled_for = "retry_wait", None, None
        session.commit()
        repeated = lease(session, "replacement-worker", kinds=["research.deep"], n=1)[0]
        session.commit()
    asyncio.run(_run_one(db(), repeated, {jid}))
    assert job(jid).status == "succeeded" and len(calls) == 12
    assert dossier(client, vid)["summary"]["usage"] == before["summary"]["usage"]


def test_credit_denial_blocks_search_before_network(client, monkeypatch):
    from app.credits.accounts import get_account
    from app.db.models import CreditAccount
    from app.state import db
    vid, _, _, jid = confirmed(client, monkeypatch)
    calls = install_research(monkeypatch)
    with db()() as session:
        account = session.get(CreditAccount, get_account(vid).id)
        account.spent_micro = account.granted_micro
        session.commit()
    run_queued("research.deep")
    assert job(jid).last_error == "research_insufficient_credits" and calls == []


def test_product_followup_is_metered_and_replay_does_not_repeat_search(client, monkeypatch):
    from app.core.jobs import lease
    from app.db.models import Job
    from app.research import deep
    from app.state import db
    from app.worker.main import _run_one

    vid, _, _, jid = confirmed(client, monkeypatch)
    calls = install_research(monkeypatch)
    provider = deep._web_provider()
    original = provider.search

    async def discovery_then_price(query):
        rows = await original(query)
        if not query.startswith("site:example.com "):
            rows[0]["snippet"] = "Bakery order management software. See product plans for details."
        return rows

    provider.search = discovery_then_price
    monkeypatch.setattr(deep, "_web_provider", lambda: provider)
    run_queued("research.deep")
    assert job(jid).status == "succeeded", job(jid).last_error
    searches = [value for kind, value in calls if kind == "search"]
    assert searches[-1] == "site:example.com software product features pricing plans"
    assert len(calls) == 13
    before = dossier(client, vid)
    assert before["calls_used"] == 13
    assert before["summary"]["usage"]["has_unsettled"] is False
    additions = before["findings"]["source"][0]["additional_excerpts"]
    assert len(additions) == 1 and "$12" in additions[0]["text"]
    assert additions[0]["observed_at"] and additions[0]["query"].startswith("site:")
    exported = request(client, "GET", f"/ventures/{vid}/research/dossiers/{before['id']}/export")
    assert "$12" in exported.text
    with db()() as session:
        row = session.get(Job, jid)
        row.status, row.run_after, row.scheduled_for = "retry_wait", None, None
        session.commit()
        repeated = lease(session, "replacement-worker", kinds=["research.deep"], n=1)[0]
        session.commit()
    asyncio.run(_run_one(db(), repeated, {jid}))
    assert job(jid).status == "succeeded" and len(calls) == 13
    assert dossier(client, vid)["summary"]["usage"] == before["summary"]["usage"]
    assert dossier(client, vid)["findings"]["source"][0]["additional_excerpts"] == additions


def test_each_search_rechecks_founder_authority(client, monkeypatch):
    from app.db.models import WorkspaceMember
    from app.research import deep
    from app.state import db
    vid, wid, _, jid = confirmed(client, monkeypatch)
    calls = install_research(monkeypatch)
    provider = deep._web_provider()
    original = provider.search
    async def revoke(query):
        rows = await original(query)
        with db()() as session:
            member = session.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == wid, WorkspaceMember.person_id == "usr_alice"))
            member.role = "viewer"
            session.commit()
        return rows
    provider.search = revoke
    monkeypatch.setattr(deep, "_web_provider", lambda: provider)
    run_queued("research.deep")
    assert job(jid).last_error == "job_authority_revoked"
    assert len(calls) == 1  # A response receipt may settle; a second call cannot start.


def test_reviewed_regenerate_rejects_changed_revision_before_adopting_current_job(client, monkeypatch):
    from app.db.models import CreditReservation, Job
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.state import db
    from core.clock import utcnow
    from core.ids import new_id
    vid, _, sid, old_job = confirmed(client, monkeypatch)
    with db()() as session:
        idea = session.get(IdeaSession, sid)
        reviewed = idea.confirmed_revision_id
        changed = new_id("idv")
        revision_number = max(r.rev for r in session.scalars(select(IdeaRevision).where(IdeaRevision.session_id == sid))) + 1
        session.add(IdeaRevision(id=changed, session_id=sid, workspace_id=idea.workspace_id, venture_id=vid,
            rev=revision_number, source="founder", structured=json.dumps({"offer": "Changed scope"}), created_at=utcnow()))
        idea.current_revision_id = idea.confirmed_revision_id = changed
        session.commit()
    # Legacy/Godmode calls remain accepted; the current job is now a different scope.
    current = request(client, "POST", f"/ventures/{vid}/research/dossier/regenerate")
    assert current.status_code == 200 and current.json()["job_id"] != old_job
    with db()() as session:
        before_jobs = len(session.scalars(select(Job)).all())
        before_holds = len(session.scalars(select(CreditReservation)).all())
    stale = request(client, "POST", f"/ventures/{vid}/research/dossier/regenerate",
                    json={"expected_revision_id": reviewed})
    assert stale.status_code == 409 and "research_inputs_changed" in stale.text
    adopted = request(client, "POST", f"/ventures/{vid}/research/dossier/regenerate",
                      json={"expected_revision_id": changed})
    assert adopted.status_code == 200 and adopted.json()["job_id"] == current.json()["job_id"]
    with db()() as session:
        assert len(session.scalars(select(Job)).all()) == before_jobs
        assert len(session.scalars(select(CreditReservation)).all()) == before_holds

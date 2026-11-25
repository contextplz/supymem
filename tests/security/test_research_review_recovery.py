"""Signed native-compatible recovery reuses sources and charges only a review."""
from openai.types import CompletionUsage
import json
from types import SimpleNamespace

from tests.security.test_connector_oauth_boundary import client as client
from tests.security.test_durable_journey_boundary import request, run_queued, venture
from tests.security.test_research_boundary import confirmed, dossier, job
from tests.research_fixtures import install_research


def test_truncated_review_recovers_saved_sources_without_searches(client, monkeypatch):
    vid, _, _, jid = confirmed(client, monkeypatch)
    def truncated(messages, options):
        body = {"overview": "Valid JSON but generation stopped early", "observations": [], "assessments": [],
                "service_checks": [], "coverage_gaps": ["Incomplete"], "proposed_tests": []}
        return SimpleNamespace(model="DeepSeek-V4-Pro", service_tier="default", choices=[SimpleNamespace(finish_reason="length", message=SimpleNamespace(content=json.dumps(body)))],
                               usage=CompletionUsage(prompt_tokens=500, completion_tokens=8192, total_tokens=8692,
            prompt_tokens_details={"cached_tokens": 0}, completion_tokens_details={"reasoning_tokens": 0}))
    install_research(monkeypatch, callback=truncated)
    run_queued("research.deep")
    # A truncated review finishes sources-only (97ccc3425); the founder can buy just the review.
    assert job(jid).status == "succeeded" and job(jid).last_error is None
    old = dossier(client, vid)
    assert old["summary"]["review"] is None and not old["summary"]["usage"]["has_unsettled"]
    base = f"/ventures/{vid}/research/dossiers/{old['id']}"
    quote = request(client, "GET", base + "/review-quote")
    assert quote.status_code == 200
    quoted = quote.json()
    assert quoted["source_count"] == 2 and quoted["max_output_tokens"] == 8192 and quoted["ceiling_micro"] > 0
    calls = install_research(monkeypatch)
    body = {"phase": "review", "source_digest": quoted["source_digest"]}
    started = request(client, "POST", base + "/rerun", json=body)
    assert started.status_code == 200
    again = request(client, "POST", base + "/rerun", json=body)
    assert again.json()["job_id"] == started.json()["job_id"]
    run_queued("research.deep")
    assert job(started.json()["job_id"]).status == "succeeded"
    assert len(calls) == 1 and calls[0][0] == "review"
    reports = request(client, "GET", f"/ventures/{vid}/research/dossiers").json()["items"]
    current = request(client, "GET", f"/ventures/{vid}/research/dossiers/{reports[0]['id']}").json()
    assert current["id"] != old["id"] and current["calls_used"] == 1
    assert {f["url"] for values in current["findings"].values() for f in values} == {f["url"] for values in old["findings"].values() for f in values}
    source_ids = {f["id"] for values in current["findings"].values() for f in values}
    assert all(o["source_id"] in source_ids for o in current["summary"]["review"]["observations"])
    assert 0 < current["summary"]["usage"]["credits_micro"] < old["summary"]["usage"]["credits_micro"]


def test_review_quote_and_dispatch_require_owned_unchanged_sources(client, monkeypatch):
    vid, _, _, _ = confirmed(client, monkeypatch)
    install_research(monkeypatch)
    run_queued("research.deep")
    old = dossier(client, vid)
    base = f"/ventures/{vid}/research/dossiers/{old['id']}"
    assert request(client, "GET", base + "/review-quote", "usr_bob").status_code == 404
    other, _, _ = venture(client)
    assert request(client, "GET", f"/ventures/{other}/research/dossiers/{old['id']}/review-quote").status_code == 404
    quote = request(client, "GET", base + "/review-quote").json()
    fid = old["findings"]["oss_repo"][0]["id"]
    assert request(client, "PATCH", f"/ventures/{vid}/research/findings/{fid}", json={"founder_flag": "exclude"}).status_code == 200
    assert request(client, "POST", base + "/rerun", json={"phase": "review", "source_digest": quote["source_digest"]}).status_code == 409
    refreshed = request(client, "GET", base + "/review-quote").json()
    assert refreshed["source_count"] == 1 and refreshed["source_digest"] != quote["source_digest"]


def test_unknown_provider_outcome_blocks_new_saved_source_review(client, monkeypatch):
    from app.db.supymem_models import ResearchDossier
    from app.state import db
    vid, _, _, _ = confirmed(client, monkeypatch)
    install_research(monkeypatch)
    run_queued("research.deep")
    old = dossier(client, vid)
    with db()() as session:
        row = session.get(ResearchDossier, old["id"])
        document = json.loads(row.summary)
        document["_runtime"]["operations"]["review"]["state"] = "uncertain"
        row.state, row.summary = "failed", json.dumps(document)
        session.commit()
    result = request(client, "GET", f"/ventures/{vid}/research/dossiers/{old['id']}/review-quote")
    assert result.status_code == 409 and "research_outcome_uncertain" in result.text

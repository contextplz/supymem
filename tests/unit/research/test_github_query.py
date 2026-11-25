"""Prod job_sq2fn9dyw7dt: founder prose with a newline (or an empty offer after vague
clarification answers) reached GitHub's query guard, which raised before any request; the
free lookup was journaled as an uncertain outcome and failed the whole deep-research run."""
from __future__ import annotations

import asyncio
import json

import pytest

from tests.company_os_harness import load_company_os_app
from tests.security.test_connector_oauth_boundary import client as client  # noqa: F401
from tests.security.test_durable_journey_boundary import run_queued
from tests.security.test_research_boundary import confirmed, dossier, job
from tests.research_fixtures import install_research

load_company_os_app()

from app.research import github_search  # noqa: E402
from app.research.deep import github_query  # noqa: E402


@pytest.mark.parametrize("snapshot,topic", [
    ({"offer": "AI agent security\nfor startups", "customer": "CTOs\r\n\tat seed stage"}, ""),
    ({"offer": "", "customer": ""}, "AI agent security"),           # vague answers left fields empty
    ({"offer": "scan agents\x00\x07 is:private repos", "customer": "-visibility:public"}, ""),
    ({"offer": "x" * 500}, ""),
])
def test_github_query_always_passes_the_real_public_query_guard(monkeypatch, snapshot, topic):
    query = github_query(snapshot, topic)
    assert query and len(query) <= 180 and "\n" not in query
    seen = {}

    async def reply(method, url, *, headers, params, timeout):
        seen.update(params)
        return github_search.Reply(status=200, headers={}, body={"items": [], "incomplete_results": False})

    monkeypatch.setattr(github_search, "_credentials", lambda workspace_id: (None, "anonymous"))
    monkeypatch.setattr(github_search, "request_json", reply)
    result = asyncio.run(github_search.search_public_repositories(query, "ws_test"))
    assert result["items"] == [] and seen["q"].endswith(" is:public")


def test_github_query_is_empty_only_when_nothing_is_known():
    assert github_query({"offer": " \n ", "customer": None}, "") == ""


def test_github_query_refusal_keeps_partial_research_success(client, monkeypatch):
    from app.research import execution as ex, github_search

    vid, _, _, jid = confirmed(client, monkeypatch)
    install_research(monkeypatch)

    async def refused(*args, **kwargs):
        raise ValueError("public query refused before transport")

    monkeypatch.setattr(github_search, "search_public_repositories", refused)
    run_queued("research.deep", through_job_id=jid)
    assert job(jid).status == "succeeded" and job(jid).last_error is None
    result = dossier(client, vid)
    assert result["state"] == "partial" and result["summary"]["stopped_early"] is False
    assert not any("GitHub repository search could not run" in gap for gap in result["summary"]["coverage_gaps"])
    assert any(item["kind"] == "system_adapter" for item in json.loads(ex.read(ex.identifier(jid)).summary)["gap_items"])
    assert json.loads(ex.read(ex.identifier(jid)).summary)["_runtime"]["operations"]["github"]["state"] == "uncertain"


@pytest.mark.parametrize("status", [429, 403])
def test_github_rate_limit_keeps_research_and_model_review_running(client, monkeypatch, status):
    """The real GitHub adapter and metering convert rate limits into optional gaps."""
    from app.research import execution as ex, github_search

    vid, _, _, jid = confirmed(client, monkeypatch)
    real_search = github_search.search_public_repositories
    calls = install_research(monkeypatch)
    monkeypatch.setattr(github_search, "search_public_repositories", real_search)
    requests = []

    async def limited(method, url, *, headers, params, timeout):
        requests.append(params["q"])
        return github_search.Reply(status=status, headers={"x-ratelimit-remaining": "0"},
                                   body={"message": "API rate limit exceeded"})

    monkeypatch.setattr(github_search, "_credentials", lambda _wid: (None, "anonymous"))
    monkeypatch.setattr(github_search, "request_json", limited)
    run_queued("research.deep", through_job_id=jid)

    saved = job(jid)
    assert saved.status == "succeeded", saved.last_error
    assert saved.attempts == 1 and len(requests) == 1
    result = dossier(client, vid)
    assert result["state"] == "partial" and result["summary"]["review"] is not None
    assert result["summary"]["stopped_early"] is False
    assert [kind for kind, _ in calls].count("review") == 1
    assert not any("GitHub repository search could not run" in gap
               for gap in result["summary"]["coverage_gaps"])
    operation = json.loads(ex.read(ex.identifier(jid)).summary)["_runtime"]["operations"]["github"]
    assert operation["state"] == "retryable" and operation["error_code"] == "research_provider_rate_limited"
    assert operation["usage"]["estimate_micro"] == 0 and operation["usage"]["reservation_id"] is None

"""Offline API fixtures only: no live providers, paid usage or real people."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest
from connectors.http import Reply

from tests.company_os_harness import load_company_os_app


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.delenv("RESEARCH_YC_OSS_RIGHTS_APPROVED", raising=False)
    load_company_os_app()
    from app.research.prior_art import adapters

    calls = []
    replies = []

    async def request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(adapters, "request_json", request)
    monkeypatch.setattr(adapters, "data_root", lambda: tmp_path)
    monkeypatch.setattr(adapters.gh, "_credentials", lambda workspace: (None, "anonymous"))
    return adapters, calls, replies, tmp_path


def hn_hit(**overrides):
    return {"objectID": "101", "title": "Show HN: Example inventory", "url": "https://example.com/",
            "story_text": "<p>Track roasting inventory.</p>", "created_at_i": 1700000000,
            "points": 42, "num_comments": 8, "author": "example-maker", "_tags": ["story", "show_hn"], **overrides}


def yc_row(**overrides):
    return {"name": "Example", "website": "https://example.com", "url": "https://www.ycombinator.com/companies/example",
            "one_liner": "Coffee roaster inventory", "long_description": "Track green beans", "tags": ["Coffee", "B2B"],
            "industry": "Manufacturing", "batch": "S20", "status": "Inactive", "team_size": 4, "launched_at": 1600000000, **overrides}


def repository(**overrides):
    return {"full_name": "example/inventory", "html_url": "https://github.com/example/inventory", "private": False, "visibility": "public",
            "description": "Coffee inventory tracking", "stargazers_count": 50, "pushed_at": "2026-09-01T12:00:00Z",
            "archived": True, "homepage": "https://example.com", **overrides}


def issue(**overrides):
    return {"number": 7, "html_url": "https://github.com/example/inventory/issues/7", "repository_url": "https://api.github.com/repos/example/inventory",
            "title": "Inventory sync fails", "body": "The inventory sync keeps failing.", "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-02-01T00:00:00Z", "reactions": {"total_count": 4}, "user": {"login": "example-user"}, **overrides}


@pytest.mark.asyncio
async def test_hn_adapter_parses_fixture_and_numeric_filters(adapter, caplog):
    mod, calls, replies, _ = adapter
    replies.append(Reply(200, {"hits": [hn_hit()]}))
    result = await mod.search_hn("coffee inventory", created_after=1600000000, min_points=5)
    assert result["calls"] == len(calls) == 1
    assert result["incomplete"] is False
    assert calls[0]["params"]["numericFilters"] == "created_at_i>1600000000,points>5"
    assert calls[0]["params"]["tags"] == "(show_hn,launch_hn)"
    assert calls[0]["timeout"] == 10
    item = result["items"][0]
    assert item["source_kind"] == "hn_item"
    assert item["url"] == "https://news.ycombinator.com/item?id=101"
    assert item["snippet"] == "Track roasting inventory."
    assert item["prior_art"]["company_url"] == "https://example.com/"
    assert item["prior_art"]["date"] == "2023-11-14T22:13:20+00:00"
    assert item["prior_art"]["date_source"] == "created_at_i"
    assert item["prior_art"]["date_raw"] == 1700000000
    assert item["prior_art"]["points"] == 42
    assert item["prior_art"]["author_url"] == "https://news.ycombinator.com/user?id=example-maker"
    assert "roasting" not in caplog.text


@pytest.mark.asyncio
async def test_hn_untrusted_metadata_is_not_a_company_fact(adapter):
    mod, _, replies, _ = adapter
    replies.append(Reply(200, {"hits": [hn_hit(url="http://127.0.0.1/pricing", author="test@example.com", created_at_i=True, points=True), hn_hit(objectID="102", deleted=True)]}))
    result = await mod.search_hn("inventory")
    assert len(result["items"]) == 1
    meta = result["items"][0]["prior_art"]
    assert meta["company_url"] is meta["company_name"] is meta["date"] is meta["points"] is meta["author_url"] is None
    assert meta["status"] == "unknown"


@pytest.mark.asyncio
async def test_hn_recent_signal_comments_use_official_date_endpoint(adapter):
    mod, calls, replies, _ = adapter
    passage = '<p>I work at <a href="https://acme.example/">Acme</a>. We need order help.</p>'
    replies.append(Reply(200, {"hits": [{"objectID": "202", "comment_text": passage,
        "created_at_i": 1790000000, "author": "public-founder", "_tags": ["comment"]}]}))
    result = await mod.search_hn("order help", tags="(story,comment,ask_hn)", created_after=1780000000,
        max_calls=1, limit=5, by_date=True)
    assert calls[0]["url"] == "https://hn.algolia.com/api/v1/search_by_date"
    assert calls[0]["params"]["numericFilters"] == "created_at_i>1780000000"
    assert calls[0]["params"]["tags"] == "(story,comment,ask_hn)" and calls[0]["timeout"] == 10
    assert result["items"][0]["content"] == passage
    assert result["items"][0]["url"] == "https://news.ycombinator.com/item?id=202"
    assert result["items"][0]["prior_art"]["date_source"] == "created_at_i"


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [{"tags": "user_secret"}, {"created_after": True}, {"min_points": -1}, {"max_calls": -1}, {"limit": 0}])
async def test_hn_invalid_inputs_have_no_http_call(adapter, kwargs):
    mod, calls, _, _ = adapter
    with pytest.raises(ValueError):
        await mod.search_hn("inventory", **kwargs)
    assert calls == []


@pytest.mark.asyncio
async def test_hn_comments_parent_identity_and_child_call_cap(adapter):
    mod, calls, replies, _ = adapter
    replies.extend([
        Reply(200, {"id": 101, "type": "story", "title": "Example inventory", "url": "https://example.com", "kids": [201, 202, 203, 204]}),
        Reply(200, {"id": 201, "parent": 101, "type": "comment", "text": "<p>The sync fails.</p>", "by": "public-user", "time": 1700000000}),
        httpx.ReadTimeout("fixture transport detail must not be returned"),
    ])
    result = await mod.hn_comments(101, parent_company_url="https://example.com", parent_company_name="Example", max_calls=3)
    assert result["calls"] == len(calls) == 3
    assert result["incomplete"] is True
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["source_kind"] == "hn_comment"
    assert item["snippet"] == "The sync fails."
    assert item["prior_art"]["company_url"] == "https://example.com"
    assert item["prior_art"]["company_name"] == "Example"
    assert item["prior_art"]["author"] == "public-user"
    assert "fixture transport" not in json.dumps(result)


@pytest.mark.asyncio
async def test_hn_comments_omit_deleted_foreign_parent_and_cycles(adapter):
    mod, calls, replies, _ = adapter
    replies.extend([
        Reply(200, {"id": 101, "type": "story", "title": "Ask HN", "url": "https://news.ycombinator.com/item?id=101", "kids": [201, 202, 203]}),
        Reply(200, {"id": 201, "parent": 101, "type": "comment", "deleted": True, "text": "gone"}),
        Reply(200, {"id": 202, "parent": 999, "type": "comment", "text": "foreign"}),
        Reply(200, {"id": 203, "parent": 101, "type": "comment", "text": "A useful question", "kids": [203, 204]}),
        Reply(200, {"id": 204, "parent": 203, "type": "comment", "text": "A nested reply"}),
    ])
    result = await mod.hn_comments(101)
    assert result["calls"] == len(calls) == 5
    assert [item["snippet"] for item in result["items"]] == ["A useful question", "A nested reply"]
    assert all(item["prior_art"]["company_url"] is None for item in result["items"])


@pytest.mark.asyncio
async def test_hn_comments_company_association_requires_observed_story_url(adapter):
    mod, _, replies, _ = adapter
    replies.extend([
        Reply(200, {"id": 101, "type": "story", "title": "Other product", "url": "https://other.example", "kids": [201]}),
        Reply(200, {"id": 201, "parent": 101, "type": "comment", "text": "This product is difficult to use."}),
    ])
    result = await mod.hn_comments(101, parent_company_url="https://example.com", parent_company_name="Example")
    assert result["items"][0]["prior_art"]["company_url"] == "https://other.example"
    assert result["items"][0]["prior_art"]["company_name"] is None
    assert result["incomplete"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cap", [0, 1, 3, 999])
async def test_hn_comment_cap_counts_actual_child_requests(adapter, cap):
    mod, calls, replies, _ = adapter
    replies.append(Reply(200, {"id": 101, "type": "story", "kids": list(range(201, 230))}))
    replies.extend(Reply(200, {"id": child, "parent": 101, "type": "comment", "text": "A public reaction"}) for child in range(201, 230))
    result = await mod.hn_comments(101, max_calls=cap, limit=10)
    assert result["calls"] == len(calls) == min(cap, mod.HN_CALL_CAP)
    assert result["incomplete"] is (cap == 0)
    assert result["gaps"] == (["This source reached its request limit."] if cap == 0 else [])


@pytest.mark.asyncio
async def test_hn_discussion_keeps_ranked_comments_within_call_and_text_limits(adapter):
    mod, calls, replies, _ = adapter
    replies.extend([
        Reply(200, {"id": 101, "type": "story", "kids": [205, 201, 202, 203]}),
        Reply(200, {"id": 205, "parent": 101, "type": "comment", "text": "Highest ranked useful public reaction. " * 200}),
        Reply(200, {"id": 201, "parent": 101, "type": "comment", "text": "Second ranked public reaction."}),
    ])
    result = await mod.hn_comments(101, limit=5, max_calls=3)
    assert len(calls) == result["calls"] == 3
    assert [item["prior_art"]["hn_id"] for item in result["items"]] == [205, 201]
    assert all(len(item["snippet"]) <= 2000 for item in result["items"])
    assert result["gaps"] == [] and result["incomplete"] is False


@pytest.mark.asyncio
async def test_hn_numeric_boolean_story_id_is_rejected(adapter):
    mod, _, replies, _ = adapter
    replies.append(Reply(200, {"id": True, "type": "story", "url": "https://example.com", "kids": [201]}))
    result = await mod.hn_comments(1)
    assert result["items"] == [] and result["incomplete"] and result["calls"] == 1


@pytest.mark.asyncio
async def test_exhausted_or_invalid_hn_responses_are_partial(adapter):
    mod, calls, replies, _ = adapter
    result = await mod.search_hn("inventory", max_calls=0)
    assert result["incomplete"] and result["calls"] == 0 and calls == []
    replies.append(Reply(429, {"message": "do not leak response text"}))
    result = await mod.search_hn("inventory")
    assert result["incomplete"] and result["calls"] == 1
    assert "leak" not in json.dumps(result)


@pytest.mark.asyncio
async def test_yc_rights_gate_does_not_fetch_or_read_cache(adapter):
    mod, calls, _, path = adapter
    cache = path / "research" / "yc-oss.json"
    cache.parent.mkdir()
    cache.write_text(json.dumps({"fetched_at": datetime.now(timezone.utc).timestamp(), "rows": [yc_row()]}))
    result = await mod.search_yc(["coffee", "inventory"])
    assert result["calls"] == 0 and result["items"] == [] and calls == []
    assert result["gaps"] == [] and result["incomplete"] is False


@pytest.mark.asyncio
async def test_yc_daily_cache_and_typed_status(adapter, monkeypatch):
    mod, calls, replies, _ = adapter
    monkeypatch.setenv("RESEARCH_YC_OSS_RIGHTS_APPROVED", "1")
    replies.append(Reply(200, [yc_row(), yc_row(name="Other", one_liner="Gym memberships", long_description="Gym exercise", tags=["Fitness"])]))
    first = await mod.search_yc(["coffee", "inventory"])
    second = await mod.search_yc("coffee inventory")
    assert first["calls"] == len(calls) == 1
    assert second["calls"] == 0
    assert first["items"] == second["items"]
    item = first["items"][0]
    assert item["title"] == "Example" and item["source_kind"] == "yc_company"
    assert item["prior_art"]["yc_status"] == "Inactive"
    assert item["prior_art"]["status"] == "inactive"
    assert item["prior_art"]["date_source"] == "launched_at"
    assert item["prior_art"]["batch"] == "S20"


@pytest.mark.asyncio
async def test_yc_stale_fallback_does_not_assert_current_status(adapter, monkeypatch):
    mod, calls, replies, path = adapter
    monkeypatch.setenv("RESEARCH_YC_OSS_RIGHTS_APPROVED", "1")
    cache = path / "research" / "yc-oss.json"
    cache.parent.mkdir()
    cache.write_text(json.dumps({"fetched_at": datetime.now(timezone.utc).timestamp() - 2 * 86400, "rows": [yc_row()]}))
    replies.append(httpx.ConnectError("offline fixture"))
    result = await mod.search_yc("coffee inventory")
    assert result["calls"] == len(calls) == 1
    assert result["incomplete"] is True
    assert result["items"][0]["prior_art"]["cache_stale"] is True
    assert result["items"][0]["prior_art"]["status"] == "unknown"
    assert any("older cached" in gap for gap in result["gaps"])


@pytest.mark.asyncio
async def test_yc_old_or_untrusted_cache_is_not_used(adapter, monkeypatch):
    mod, _, replies, path = adapter
    monkeypatch.setenv("RESEARCH_YC_OSS_RIGHTS_APPROVED", "1")
    cache = path / "research" / "yc-oss.json"
    cache.parent.mkdir()
    cache.write_text(json.dumps({"fetched_at": datetime.now(timezone.utc).timestamp() - 8 * 86400, "rows": [yc_row()]}))
    replies.append(Reply(200, {"unexpected": "shape"}))
    result = await mod.search_yc("coffee inventory")
    assert result["items"] == [] and result["incomplete"]


@pytest.mark.asyncio
async def test_yc_untrusted_optional_fields_do_not_fabricate_status(adapter, monkeypatch):
    mod, _, replies, _ = adapter
    monkeypatch.setenv("RESEARCH_YC_OSS_RIGHTS_APPROVED", "1")
    replies.append(Reply(200, [yc_row(status=["Inactive"], team_size=True, launched_at="yesterday", website="https://localhost/private")]))
    result = await mod.search_yc("coffee inventory")
    meta = result["items"][0]["prior_art"]
    assert meta["status"] == "unknown"
    assert meta["yc_status"] is meta["date"] is meta["team_size"] is meta["company_url"] is None


@pytest.mark.asyncio
async def test_github_public_repository_and_issue_metadata(adapter):
    mod, calls, replies, _ = adapter
    replies.extend([Reply(200, {"items": [repository(), repository(private=True)]}), Reply(200, {"items": [issue(), issue(number=8, html_url="https://evil.example/8"), issue(number=9, pull_request={})]})])
    result = await mod.search_github("coffee inventory in:name,description,readme", "workspace-a")
    assert result["calls"] == len(calls) == 2
    assert calls[0]["params"]["sort"] == "stars"
    assert calls[1]["params"]["sort"] == "reactions"
    assert calls[1]["params"]["q"] == "repo:example/inventory is:issue is:public"
    repo, complaint = result["items"]
    assert repo["source_kind"] == "repository_metadata"
    assert repo["prior_art"]["homepage"] == "https://example.com"
    assert repo["prior_art"]["archived"] is True
    assert repo["prior_art"]["status"] == "unknown"
    assert complaint["source_kind"] == "github_issue"
    assert complaint["prior_art"]["company_url"] == repo["url"]
    assert complaint["prior_art"]["reactions"] == 4
    assert complaint["prior_art"]["author_url"] == "https://github.com/example-user"


@pytest.mark.asyncio
async def test_github_budget_counts_child_calls_and_keeps_partial(adapter):
    mod, calls, replies, _ = adapter
    replies.extend([Reply(200, {"items": [repository(), repository(full_name="other/inventory", html_url="https://github.com/other/inventory")]}), httpx.ReadTimeout("fixture")])
    result = await mod.search_github("coffee inventory", "workspace-a", max_calls=2)
    assert result["calls"] == len(calls) == 2
    assert len(result["items"]) == 2
    assert result["incomplete"]


@pytest.mark.asyncio
async def test_github_selected_connection_failure_never_switches_identity(adapter, monkeypatch):
    mod, calls, _, _ = adapter

    def unavailable(workspace):
        raise mod.gh.ConnectorError("private credential error")

    monkeypatch.setattr(mod.gh, "_credentials", unavailable)
    result = await mod.search_github("inventory", "workspace-a")
    assert result["items"] == [] and result["incomplete"]
    assert result["calls"] == 0 and calls == []
    assert "private credential" not in json.dumps(result)

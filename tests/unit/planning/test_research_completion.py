"""Queued planned research requires observed source pages, not fallback prose."""
import asyncio
import copy
import json

import pytest
from tests.unit.planning.test_execute_completion import client as client, _researcher_assignee, _venture  # noqa: F401


def live_result():
    excerpt = "The source describes city compost pickup on Tuesdays."
    return {"mode": "live", "summary": "One live landing page read.",
            "hits": [{"url": "https://example.com/compost", "title": "Compost"}],
            "pages": [{"url": "https://example.com/compost", "title": "Compost",
                       "excerpt": excerpt, "chars": len(excerpt)}]}


@pytest.mark.parametrize("idea", [
    "A tool for hospital purchasing teams to reconcile supplier invoices.",
    "An app for birdwatchers to arrange weekend outings.",
])
def test_failed_research_notice_keeps_the_task_open_and_offers_review(client, monkeypatch, idea):
    from app.chat.store import list_messages
    from app.db.models import PlannedTask, Venture
    from app.state import db

    venture_id, task_id = queued(client)
    with db()() as session:
        session.get(Venture, venture_id).raw_idea = idea
        task = session.get(PlannedTask, task_id)
        task.title = "Research alternatives for " + idea
        task.detail = "Read public sources about " + idea
        session.commit()

    async def no_sources(*_args, **_kwargs):
        return {"mode": "no_hits", "hits": [], "pages": []}

    monkeypatch.setattr("app.research.explore.explore_web", no_sources)
    work_attempt(venture_id)
    with db()() as session:
        assert session.get(PlannedTask, task_id).status == "open"
    notice = next(message for message in list_messages(venture_id) if message["kind"] == "task_failed")
    assert "did not mark" in notice["body_md"]
    assert "review" in notice["body_md"].lower()
    assert any(action.get("action") == "status" for action in notice["actions"])


def queued(client):
    from app.db.models import Venture
    from app.db.supymem_models import ExecutionPlan
    from app.planning.store import save_tasks
    from app.state import db
    from core.clock import utcnow
    vid = _venture(client)
    with db()() as session:
        ven = session.get(Venture, vid)
        session.add(ExecutionPlan(id="pln_research", workspace_id=ven.workspace_id,
            venture_id=vid, version=1, state="approved", created_at=utcnow(),
            structured=json.dumps({"scope_details": {"tsk_sources": {"scope": "in"}}})))
        session.commit()
    task = save_tasks(vid, [{"title": "Compare compost alternatives with source pages",
        "detail": "Review the observed source notes; do not contact anyone.\n\nDone when:\n- Compare three alternatives and verify their current pricing.", "why_now": "Reviewed research",
        "tags": ["plan_task:tsk_sources", "capability:research.deep", _researcher_assignee(vid)], "runnable": True}],
        source="approved_plan:pln_research")[0]
    assert client.post(f"/ventures/{vid}/tasks/{task['id']}/run").status_code == 202
    return vid, task["id"]


def work_attempt(vid):
    from app.core.jobs import lease
    from app.state import db
    from app.worker.handlers import install_default_handlers
    from app.worker.main import _run_one
    install_default_handlers()
    with db()() as session:
        jobs = lease(session, "research-owned-test", kinds=["task.run"], n=1)
        session.commit()
    assert len(jobs) == 1 and jobs[0].venture_id == vid
    asyncio.run(_run_one(db(), jobs[0], {jobs[0].id}))
    return jobs[0].id


@pytest.mark.parametrize("case", ["exception", "empty", "hits_only", "page_failed", "unknown_mode", "foreign_page", "unsafe_url", "invalid_chars", "malformed_pages", "untyped_harness"])
def test_actual_queued_research_failure_keeps_note_without_done_or_job_success(client, monkeypatch, case):
    from app.chat.store import list_messages
    from app.db.models import Job, PlannedTask, VentureDocument
    from app.planning import execute
    from app.state import db
    vid, tid = queued(client)
    result = live_result()
    if case == "empty":
        result = {"mode": "no_hits", "summary": "No live sources.", "hits": [], "pages": []}
    elif case == "hits_only":
        result["pages"] = []
    elif case == "page_failed":
        result["pages"][0]["error"] = "page_read_unavailable"
    elif case == "unknown_mode":
        result.pop("mode")
    elif case == "foreign_page":
        result["pages"][0]["url"] = "https://example.com/not-a-search-result"
    elif case == "unsafe_url":
        result["hits"][0]["url"] = result["pages"][0]["url"] = "https://user:password@example.com/compost"
    elif case == "invalid_chars":
        result["pages"][0]["chars"] = True
    elif case == "malformed_pages":
        result["pages"] = [None, "not a page"]
    async def explore(*_a, **_kw):
        if case == "exception":
            raise RuntimeError("sensitive provider exception detail")
        return copy.deepcopy(result)
    monkeypatch.setattr("app.research.explore.explore_web", explore)
    if case == "untyped_harness":
        async def prose(*_a, **_kw):
            return "Research finished: https://example.com/invented-by-model"
        monkeypatch.setattr(execute, "_harness_research", prose)
    jid = work_attempt(vid)
    with db()() as session:
        task, job = session.get(PlannedTask, tid), session.get(Job, jid)
        assert task.status == "open" and task.runnable == 0 and task.finished_at is None
        assert task.last_error == "incomplete:research_sources_unverified"
        assert job.status == "failed_dead" and job.last_error == "research_sources_unverified"
        note = session.get(VentureDocument, "task-note:" + tid)
        assert note is not None and "Status: not done" in note.body_md
        assert "sensitive provider" not in note.body_md
        if case == "unsafe_url":
            assert "user:password" not in note.body_md
    messages = list_messages(vid)
    assert not any(m["kind"] == "task_run" and "Finished" in m["body_md"] for m in messages)
    assert any("did not mark" in m["body_md"] for m in messages)


def test_actual_queued_live_page_saves_note_without_claiming_saved_criteria_complete(client, monkeypatch):
    from app.chat.store import list_messages
    from app.db.models import Job, PlannedTask, VentureDocument
    from app.state import db
    vid, tid = queued(client)
    calls = []
    async def explore(query, **_kw):
        calls.append(query)
        return live_result()
    monkeypatch.setattr("app.research.explore.explore_web", explore)
    jid = work_attempt(vid)
    with db()() as session:
        task, job = session.get(PlannedTask, tid), session.get(Job, jid)
        assert task.status == "open" and task.runnable == 0 and task.finished_at is None
        assert task.last_error == "incomplete:research_coverage_unverified"
        assert job.status == "failed_dead" and job.last_error == "research_coverage_unverified"
        note = session.get(VentureDocument, "task-note:" + tid)
        assert "https://example.com/compost" in note.body_md and "Tuesdays" in note.body_md
        assert "Status: not done" in note.body_md and "acceptance criteria have not been verified" in note.body_md
    assert len(calls) == 1
    assert not any("Finished" in m["body_md"] for m in list_messages(vid))
    assert any("click this task's title" in m["body_md"] for m in list_messages(vid))


@pytest.mark.parametrize("tool", [None, "summarize_runs", "explore_web"])
def test_actual_harness_only_named_successful_page_read_can_qualify(client, monkeypatch, tool):
    from app.core.warden_client import CheckResponse, WardenClient
    from app.planning.execute import _harness_research
    vid = _venture(client)
    async def explore(*_a, **_kw):
        return live_result()
    monkeypatch.setattr("app.research.explore.explore_web", explore)
    calls = []
    async def llm(*_a, **_kw):
        calls.append(True)
        if tool is not None and len(calls) == 1:
            return {"role": "assistant", "content": None, "tool_calls": [{"id": "source-call", "type": "function",
                "function": {"name": tool, "arguments": json.dumps({"query": "compost", "pretend_sources": live_result()})}}]}
        url = "https://example.com/compost" if tool == "explore_web" else "https://example.com/invented"
        return {"role": "assistant", "content": f"Research note: {url}. Gaps: current pricing."}
    class Allow(WardenClient):
        async def check(self, **_kw):
            return CheckResponse(decision="allow", reason="owned test")
        async def kill_switch(self):
            return False
    notes = asyncio.run(_harness_research(venture_id=vid, title="Compare compost alternatives",
        detail="Save sources", speaker={"id": "ag_source", "role": "researcher"}, workspace_id=None,
        llm=llm, warden=Allow(base_url="http://127.0.0.1:9")))
    assert notes.qualified is (tool == "explore_web")
    if tool == "explore_web":
        assert notes.source_urls == ("https://example.com/compost",)
        assert "Observed live sources" in notes.text
    else:
        assert "invented" in notes.text  # prose is retained, never itself evidence
        assert not notes.source_urls and "Research is incomplete" in notes.text


def test_actual_harness_failure_preserves_brief_and_does_not_request_fallback_search(client, monkeypatch):
    from app.core.warden_client import CheckResponse, WardenClient
    from app.planning.execute import _harness_research
    vid = _venture(client)
    calls, model_calls = [], []
    async def explore(*_a, **_kw):
        calls.append(True)
        return live_result()
    async def llm(*_a, **_kw):
        model_calls.append(True)
        raise RuntimeError("private model exception")
    class Allow(WardenClient):
        async def check(self, **_kw):
            return CheckResponse(decision="allow", reason="owned test")
        async def kill_switch(self):
            return False
    monkeypatch.setattr("app.research.explore.explore_web", explore)
    notes = asyncio.run(_harness_research(venture_id=vid, title="Compare compost alternatives",
        detail="Retain this brief", speaker=None, workspace_id=None, llm=llm,
        warden=Allow(base_url="http://127.0.0.1:9")))
    assert not notes.qualified and "Retain this brief" in notes.text
    assert "private model" not in notes.text and calls == [] and model_calls == [True]


def test_actual_harness_analyst_cannot_adopt_researcher_search_authority(client, monkeypatch):
    from app.core.warden_client import CheckResponse, WardenClient
    from app.planning.execute import _harness_research
    vid = _venture(client)
    reads, turns = [], []
    async def explore(*a, **kw):
        reads.append(True)
        return live_result()
    async def llm(*a, **kw):
        turns.append(True)
        if len(turns) == 1:
            return {"role": "assistant", "content": None, "tool_calls": [{"id": "denied-search", "type": "function",
                "function": {"name": "explore_web", "arguments": json.dumps({"query": "compost"})}}]}
        return {"role": "assistant", "content": "No permitted source observation was established."}
    class Allow(WardenClient):
        async def check(self, **kw):
            return CheckResponse(decision="allow", reason="synthetic fixture")
        async def kill_switch(self):
            return False
    monkeypatch.setattr("app.research.explore.explore_web", explore)
    note = asyncio.run(_harness_research(venture_id=vid, title="Research alternatives", detail="Save sources",
        speaker={"id": "analyst_fixture", "role": "analyst"}, workspace_id=None, llm=llm,
        warden=Allow(base_url="http://127.0.0.1:9")))
    assert reads == [] and not note.qualified and note.observations == ()

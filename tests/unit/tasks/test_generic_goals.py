"""Actual generic executor, synthetic tool pages, and code-owned receipts."""
import asyncio
from contextlib import nullcontext
import json

import pytest
from sqlalchemy import select

from tests.unit.tasks.test_task_lifecycle import lifecycle as lifecycle


@pytest.fixture()
def generic(lifecycle, tmp_path, monkeypatch):
    from app.chat import assistant, recovery
    from app.db.models import PlannedTask, TaskFingerprint, VentureDocument
    from app.munder import ledger, mailbox
    from app.org import characters, summary
    from app.paperclip import goals
    from app.planning import execute, store
    from app.research import explore
    from app.tasks import skills
    d = lifecycle
    for model in (PlannedTask, TaskFingerprint, VentureDocument):
        model.__table__.create(d["factory"].kw["bind"], checkfirst=True)
    monkeypatch.setattr(execute, "db", lambda: d["factory"])
    monkeypatch.setattr(store, "db", lambda: d["factory"])
    monkeypatch.setattr(execute, "_mark", lambda *a, **kw: None)
    monkeypatch.setattr(execute, "_chat", lambda *a, **kw: None)
    monkeypatch.setattr(execute, "_venture_repo", lambda vid: tmp_path / "ventures" / vid / "repo")
    monkeypatch.setattr(recovery, "post_failure", lambda *a, **kw: None)
    monkeypatch.setattr(goals, "ancestry_markdown", lambda *a, **kw: "")
    monkeypatch.setattr(summary, "resolve_org", lambda *a: {"agents": []})
    monkeypatch.setattr(characters, "owner_for_work", lambda *a: {"id": "synthetic_researcher",
        "name": "Synthetic researcher", "role": "researcher", "title": "Research"})
    monkeypatch.setattr(mailbox, "drain_for_stop", lambda *a: {})
    monkeypatch.setattr(mailbox, "send", lambda *a, **kw: {})
    monkeypatch.setattr(ledger, "patch_task", lambda *a, **kw: None)
    monkeypatch.setattr(ledger, "sync_planned_runnable", lambda *a: None)
    from app.ventures import state_brief
    monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda *a: {"idea": {
        "name": "Synthetic idea", "raw_idea": "Bakery order scheduling", "audience": "bakery owners"}})
    async def composed(*a, **kw):
        return "Subject: A question about Synthetic idea\n\nHow do you handle bakery order scheduling today?"
    monkeypatch.setattr(assistant, "_complete", composed)
    async def no_harness(**kwargs):
        return None
    d["real_harness"] = execute._harness_research
    monkeypatch.setattr(execute, "_harness_research", no_harness)
    async def observed(query):
        urls = ["https://source-one.example/scheduling", "https://source-two.example/scheduling"]
        return {"mode": "live", "summary": "Synthetic observed pages, not live research evidence",
            "hits": [{"url": url} for url in urls],
            "pages": [{"url": url, "excerpt": "Synthetic source observation", "chars": 28} for url in urls]}
    monkeypatch.setattr(explore, "explore_web", observed)
    d.update(execute=execute, store=store, skills=skills, explore=explore)
    return d


def _planned(d, goal):
    from app.db.models import PlannedTask
    with d["factory"]() as session:
        session.add(PlannedTask(id="planned_generic", workspace_id="ws_lifecycle", venture_id="ven_lifecycle",
            title=goal, detail=goal, why_now="Synthetic", source="synthetic", status="running",
            created_at=d["clock"][0], updated_at=d["clock"][0]))
        session.commit()
    return "planned_generic"


def _linked(d, goal, check):
    from app.db.task_models import TaskRun, TaskStep
    from app.tasks.contracts import Contract
    pid = _planned(d, goal)
    contract = Contract(title=goal, goal=goal, done_test=check,
        steps=[{"title": goal, "skill": "task.run", "inputs": {"task_id": pid}}])
    with d["factory"]() as session:
        task = d["service"].create(session, workspace_id="ws_lifecycle", venture_id="ven_lifecycle",
            owner_person_id="usr_lifecycle", contract=contract)
        session.flush()
        run = session.scalar(select(TaskRun).where(TaskRun.task_id == task.id))
        step = session.scalar(select(TaskStep).where(TaskStep.run_id == run.id))
        with d["service"].step_context(run.id, step.id):
            jid = d["jobs"].enqueue(session, "task.run", {"task_id": pid, "_actor_subject": "usr_lifecycle"},
                workspace_id="ws_lifecycle", venture_id="ven_lifecycle", dedupe_key="synthetic-generic-work")
        session.commit()
        return pid, task.id, run.id, step.id, jid


def _execute(d, pid, jid=None):
    with (d["usage"].job_context(jid) if jid else nullcontext()):
        return asyncio.run(d["execute"].execute_planned_task("ven_lifecycle", pid, already_claimed=True))


def _verify(d, pid, jid):
    with d["factory"]() as session:
        return d["skills"].verify_result(session, "task.run", {"_job_id": jid, "task_id": pid},
            workspace_id="ws_lifecycle", venture_id="ven_lifecycle")


@pytest.mark.parametrize("goal", ["Book a flight", "Buy a laptop", "Delete my inbox email", "Send a note to Maya",
    "Book a flight and draft an email to Maya", "Buy ads and draft an email to Maya",
    "Delete production data and draft an email to Maya", "Draft an email and send it to Maya",
    "Can you book a flight and draft an email to Maya", "I want to buy ads and draft an email to Maya",
    "Draft an email after booking a flight"])
def test_unsupported_action_cannot_complete_from_local_draft_or_note(generic, goal):
    from app.db.models import PlannedTask
    d = generic
    pid = _planned(d, goal)
    try:
        result = _execute(d, pid)
    except d["execute"].TaskRunError:
        result = {"status": "blocked"}
    with d["factory"]() as session:
        assert session.get(PlannedTask, pid).status != "done"
    assert result["status"] != "done"


def test_exact_linked_source_count_completes_from_observed_pages(generic):
    from app.db.models import PlannedTask, VentureDocument
    from app.db.task_models import TaskEvent, TaskRun
    d = generic
    check = {"type": "deterministic", "spec": {"predicate": "at_least", "metric": "source_count", "count": 2}}
    pid, _, run_id, _, jid = _linked(d, "Research two source pages about bakery scheduling", check)
    assert _execute(d, pid, jid)["status"] == "done"
    receipt = _verify(d, pid, jid)
    assert len(receipt["evidence"][0]["source_hashes"]) == 2
    assert receipt["verified"] and receipt["evidence"][0]["source_count"] == 2
    with d["factory"]() as session:
        assert session.get(TaskRun, run_id).tool_calls == 1
        assert len(session.scalars(select(TaskEvent).where(TaskEvent.run_id == run_id, TaskEvent.kind == "tool_observed")).all()) == 1
        assert session.get(PlannedTask, pid).status == "done"
        provenance = json.loads(session.get(VentureDocument, "task-note:" + pid).provenance_json)
        source = provenance["research_receipt"]
        assert source["capture"] == "explore_web.pages_v1" and source["source_count"] == 2
        assert len(source["observed_sources"]) == 2 and source["job_id"] == jid


def test_model_prose_urls_never_satisfy_source_count(generic, monkeypatch):
    from app.db.models import PlannedTask
    d = generic
    async def invented(query):
        return {"mode": "live", "summary": "Here are twenty model-written citations: https://invented.example",
            "hits": [{"url": "https://lead.example"}], "pages": []}
    monkeypatch.setattr(d["explore"], "explore_web", invented)
    check = {"type": "deterministic", "spec": {"predicate": "at_least", "metric": "source_count", "count": 2}}
    pid, _, _, _, jid = _linked(d, "Research two source pages about bakery scheduling", check)
    assert _execute(d, pid, jid)["status"] != "done" and not _verify(d, pid, jid)["verified"]
    with d["factory"]() as session:
        assert session.get(PlannedTask, pid).finished_at is None


def test_duplicate_page_observations_do_not_inflate_source_count(generic, monkeypatch):
    d = generic
    async def duplicates(query):
        url = "https://source.example/scheduling"
        return {"mode": "live", "summary": "Duplicate synthetic source",
            "hits": [{"url": url}], "pages": [{"url": url, "excerpt": "Synthetic page", "chars": 14}] * 3}
    monkeypatch.setattr(d["explore"], "explore_web", duplicates)
    check = {"type": "deterministic", "spec": {"predicate": "at_least", "metric": "source_count", "count": 2}}
    pid, _, _, _, jid = _linked(d, "Research two source pages about bakery scheduling", check)
    assert _execute(d, pid, jid)["status"] != "done" and not _verify(d, pid, jid)["verified"]


@pytest.mark.parametrize("mutation", ["body", "goal", "job", "scope", "observation"])
def test_source_count_receipt_rejects_changed_body_goal_scope_or_source(generic, mutation):
    from app.db.models import PlannedTask, VentureDocument
    d = generic
    check = {"type": "deterministic", "spec": {"predicate": "at_least", "metric": "source_count", "count": 2}}
    pid, _, _, _, jid = _linked(d, "Research two source pages about bakery scheduling", check)
    _execute(d, pid, jid)
    with d["factory"]() as session:
        row = session.get(VentureDocument, "task-note:" + pid)
        data = json.loads(row.provenance_json)
        if mutation == "body":
            row.body_md += "Changed content"
        elif mutation == "goal":
            session.get(PlannedTask, pid).detail = "A different research request"
        elif mutation == "job":
            data["research_receipt"]["job_id"] = "foreign"
        elif mutation == "scope":
            data["research_receipt"]["workspace_id"] = "foreign"
        else:
            data["research_receipt"]["observed_sources"][0]["excerpt_sha256"] = "f" * 64
        row.provenance_json = json.dumps(data)
        session.commit()
    assert not _verify(d, pid, jid)["verified"]


def test_whole_rubric_keeps_legacy_open_until_named_finalization_seam(generic):
    from app.db.models import PlannedTask
    from app.db.task_models import TaskRun, TaskStep
    d = generic
    check = {"type": "rubric", "spec": {"predicate": "rubric", "criteria": ["The sources answer the founder's research question"]}}
    pid, _, run_id, step_id, jid = _linked(d, "Research bakery scheduling alternatives", check)
    assert _execute(d, pid, jid)["status"] == "prepared"
    receipt = _verify(d, pid, jid)
    assert receipt["verified"] and receipt["evidence"][0]["source_count"] == 2
    with d["factory"]() as session:
        assert session.get(PlannedTask, pid).status == "open"
        assert d["execute"].finalize_research_for_run(session, run_id) == 0
        # Synthetic parent-verifier acceptance, not a live rubric/model verdict.
        session.get(TaskRun, run_id).state = "completed"
        session.get(TaskStep, step_id).state = "completed"
        assert d["execute"].finalize_research_for_run(session, run_id) == 1
        assert d["execute"].finalize_research_for_run(session, run_id) == 0
        session.commit()
        assert session.get(PlannedTask, pid).status == "done"


def test_legacy_open_ended_research_stays_parked_even_with_observed_pages(generic):
    from app.db.models import PlannedTask
    d = generic
    pid = _planned(d, "Research bakery scheduling alternatives")
    assert _execute(d, pid)["status"] != "done"
    with d["factory"]() as session:
        assert session.get(PlannedTask, pid).status == "open" and session.get(PlannedTask, pid).finished_at is None


@pytest.mark.parametrize("goal", ["Book a flight", "Buy ads and draft an email", "Send the saved email to Maya", "Delete my inbox email",
    "Can you book a flight and draft an email to Maya", "I want to buy ads and draft an email to Maya",
    "Draft an email after booking a flight"])
def test_old_completed_local_note_cannot_certify_reviewed_action(generic, goal):
    from app.db.models import Job, PlannedTask, VentureDocument
    d = generic
    pid = _planned(d, goal)
    with d["factory"]() as session:
        planned = session.get(PlannedTask, pid)
        planned.status, planned.finished_at = "done", d["clock"][0]
        session.add(VentureDocument(id="task-note:" + pid, venture_id="ven_lifecycle", kind="task_note", title=goal,
            body_md="Status: completed\n\nA generic saved note does not prove the requested effect.",
            provenance_json=json.dumps({"task_id": pid, "source": "task.run"}),
            created_at=d["clock"][0], updated_at=d["clock"][0]))
        session.add(Job(id="job_old_note", kind="task.run", workspace_id="ws_lifecycle", venture_id="ven_lifecycle",
            payload_json=json.dumps({"task_id": pid}), status="succeeded", created_at=d["clock"][0]))
        session.commit()
    receipt = _verify(d, pid, "job_old_note")
    assert not receipt["verified"] and receipt["blocker"]["class"] == "needs_approval"


@pytest.mark.parametrize("goal", ["Draft an email asking Maya to book the room", "Research how to book a flight", "Draft a message discussing buying a laptop"])
def test_ordinary_drafts_and_read_only_research_keep_their_existing_authority(generic, goal):
    from app.tasks.contracts import requires_reviewed_action
    assert requires_reviewed_action(goal) is False


@pytest.mark.parametrize("check_kind", ["count", "rubric"])
def test_committed_research_receipt_recovers_without_model_or_tool_replay(generic, monkeypatch, check_kind):
    from app.db.models import VentureDocument
    d = generic
    check = ({"type": "rubric", "spec": {"predicate": "rubric", "criteria": ["The sources answer the research goal"]}}
        if check_kind == "rubric" else {"type": "deterministic", "spec": {"predicate": "at_least", "metric": "source_count", "count": 2}})
    pid, _, _, _, jid = _linked(d, "Research source pages about bakery scheduling", check)
    close_done = d["execute"]._close_done
    if check_kind == "count":
        def crash(*args, **kwargs):
            raise RuntimeError("Synthetic crash after durable artifact, before legacy close")
        monkeypatch.setattr(d["execute"], "_close_done", crash)
        with pytest.raises(RuntimeError, match="Synthetic crash"):
            _execute(d, pid, jid)
        monkeypatch.setattr(d["execute"], "_close_done", close_done)
    else:
        assert _execute(d, pid, jid)["status"] == "prepared"
    with d["factory"]() as session:
        note = session.get(VentureDocument, "task-note:" + pid)
        durable = (note.body_md, note.provenance_json)
    async def forbidden(*args, **kwargs):
        raise AssertionError("The committed exact receipt must not trigger another paid or source call")
    monkeypatch.setattr(d["execute"], "_harness_research", forbidden)
    monkeypatch.setattr(d["execute"], "_research_notes", forbidden)
    monkeypatch.setattr(d["explore"], "explore_web", forbidden)
    assert _execute(d, pid, jid)["status"] == ("prepared" if check_kind == "rubric" else "done")
    assert _verify(d, pid, jid)["verified"]
    with d["factory"]() as session:
        note = session.get(VentureDocument, "task-note:" + pid)
        assert (note.body_md, note.provenance_json) == durable


@pytest.mark.parametrize("mutation", ["body", "provenance", "goal"])
def test_changed_crash_receipt_requires_review_without_a_fresh_call(generic, monkeypatch, mutation):
    from app.db.models import PlannedTask, VentureDocument
    from app.db.task_models import Task
    d = generic
    check = {"type": "deterministic", "spec": {"predicate": "at_least", "metric": "source_count", "count": 2}}
    pid, task_id, _, _, jid = _linked(d, "Research source pages about bakery scheduling", check)
    close_done = d["execute"]._close_done
    def crash(*args, **kwargs):
        raise RuntimeError("Synthetic crash")
    monkeypatch.setattr(d["execute"], "_close_done", crash)
    with pytest.raises(RuntimeError, match="Synthetic crash"):
        _execute(d, pid, jid)
    monkeypatch.setattr(d["execute"], "_close_done", close_done)
    with d["factory"]() as session:
        note = session.get(VentureDocument, "task-note:" + pid)
        if mutation == "body":
            note.body_md += "Changed after crash"
        elif mutation == "provenance":
            note.provenance_json = "not valid JSON"
        else:
            session.get(Task, task_id).goal = "A different research goal"
        session.commit()
    async def forbidden(*args, **kwargs):
        raise AssertionError("A changed saved receipt cannot silently authorize another paid call")
    monkeypatch.setattr(d["execute"], "_harness_research", forbidden)
    monkeypatch.setattr(d["execute"], "_research_notes", forbidden)
    assert _execute(d, pid, jid)["status"] == "blocked"
    with d["factory"]() as session:
        assert session.get(PlannedTask, pid).status != "done"
        assert json.loads(session.get(Task, task_id).blocker_json)["class"] == "needs_input"


def test_analyst_fallback_cannot_adopt_researcher_tool_authority(generic, monkeypatch):
    from app.db.models import PlannedTask, VentureDocument
    from app.db.task_models import Task
    from app.org import characters
    d = generic
    calls = []
    async def forbidden(query):
        calls.append(query)
        raise AssertionError("Denied role cannot call the research fallback")
    monkeypatch.setattr(d["explore"], "explore_web", forbidden)
    monkeypatch.setattr(characters, "owner_for_work", lambda *args: {"id": "analyst", "role": "analyst"})
    check = {"type": "deterministic", "spec": {"predicate": "at_least", "metric": "source_count", "count": 2}}
    pid, task_id, _, _, jid = _linked(d, "Research source pages about bakery scheduling", check)
    assert _execute(d, pid, jid)["status"] == "blocked"
    assert calls == []
    with d["factory"]() as session:
        assert session.get(Task, task_id).state == "rejected"
        planned = session.get(PlannedTask, pid)
        assert planned.status == "open" and planned.runnable == 0 and planned.finished_at is None
        assert "Status: not done" in session.get(VentureDocument, "task-note:" + pid).body_md


def test_direct_fallback_cannot_exceed_actual_tool_budget(generic, monkeypatch):
    from app.db.task_models import Task, TaskRun
    d = generic
    calls = []
    async def forbidden(query):
        calls.append(query)
        raise AssertionError("Tool ceiling must be checked before calling the fallback")
    monkeypatch.setattr(d["explore"], "explore_web", forbidden)
    check = {"type": "deterministic", "spec": {"predicate": "at_least", "metric": "source_count", "count": 2}}
    pid, task_id, run_id, _, jid = _linked(d, "Research source pages about bakery scheduling", check)
    with d["factory"]() as session:
        session.get(Task, task_id).budget_json = json.dumps({"tool_calls": 1})
        session.get(TaskRun, run_id).tool_calls = 1
        session.commit()
    assert _execute(d, pid, jid)["status"] != "done" and calls == []
    with d["factory"]() as session:
        assert json.loads(session.get(Task, task_id).blocker_json)["class"] == "budget_exhausted"


def test_direct_fallback_unavailable_result_stops_without_model_reflection(generic, monkeypatch):
    from app.db.task_models import Task, TaskEvent
    d = generic
    calls = []
    async def unavailable(query):
        calls.append(query)
        return {"mode": "error", "error_code": "search_unavailable", "hits": [], "pages": []}
    monkeypatch.setattr(d["explore"], "explore_web", unavailable)
    check = {"type": "deterministic", "spec": {"predicate": "at_least", "metric": "source_count", "count": 2}}
    pid, task_id, run_id, _, jid = _linked(d, "Research source pages about bakery scheduling", check)
    assert _execute(d, pid, jid)["status"] != "done" and len(calls) == 1
    with d["factory"]() as session:
        assert json.loads(session.get(Task, task_id).blocker_json)["class"] == "needs_input"
        assert not session.scalar(select(TaskEvent.id).where(TaskEvent.run_id == run_id, TaskEvent.kind == "strategy_reflection_calling"))


def test_outreach_draft_quoting_a_later_gmail_send_still_runs_as_a_draft():
    from tests.company_os_harness import load_company_os_app
    load_company_os_app()
    from app.planning.execute import _requires_task_approval
    detail = ("Founder request: Draft one outreach email to info@sample-strength.test for me to review, then I'll send it from my Gmail.\n"
              "Write one outreach email for review only. Do not send, post or schedule anything.")
    assert _requires_task_approval("Draft one outreach email for me to review", detail, ["chat"]) is False

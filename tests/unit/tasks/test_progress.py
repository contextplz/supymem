"""Actual strategy journal and research boundary with synthetic model/tool results."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tests.unit.tasks.test_task_lifecycle import lifecycle as lifecycle
from tests.unit.tasks.test_generic_goals import generic as generic
from tests.task_billing_fixtures import checkpoint, install_recovery, synthetic_receipt


@pytest.fixture()
def progress_db(lifecycle, monkeypatch):
    from app.db.models import Job
    from app.db.task_models import Task, TaskRun, TaskStep
    from app.planning import execute
    from app.tasks import progress
    from app.tasks.contracts import Contract
    d = lifecycle
    monkeypatch.setattr(progress, "db", lambda: d["factory"])
    monkeypatch.setattr(execute, "db", lambda: d["factory"])
    with d["factory"]() as session:
        task = d["service"].create(session, workspace_id="ws_lifecycle", venture_id="ven_lifecycle",
            owner_person_id="usr_lifecycle", contract=Contract(title="Research bakery scheduling",
                goal="Research three public sources about bakery scheduling",
                steps=[{"title": "Gather sources", "skill": "task.run", "inputs": {"task_id": "synthetic_planned"}}]))
        session.flush()
        run = session.scalar(select(TaskRun).where(TaskRun.task_id == task.id))
        step = session.scalar(select(TaskStep).where(TaskStep.run_id == run.id))
        with d["service"].step_context(run.id, step.id):
            jid = d["jobs"].enqueue(session, "task.run", {"task_id": "synthetic_planned"},
                workspace_id=task.workspace_id, venture_id=task.venture_id, dedupe_key="synthetic-progress")
        task.state = run.state = step.state = "working"
        session.get(Job, jid).status = "running"
        session.commit()
        d.update(task_id=task.id, run_id=run.id, step_id=step.id, jid=jid)
    calls, mode = [], {"value": "ok", "query": "bakery order scheduling independent comparison", "receipt": None,
        "after_checkpoint": None, "billing_source": "platform"}
    recovery = install_recovery(monkeypatch)
    from app.credits import metered
    async def llm(messages, **kwargs):
        estimate, actual = (0, 0) if mode["billing_source"] == "free" else (20, 7)
        invocation = d["usage"].before_call(estimate, kwargs["max_tokens"])
        calls.append((messages, {key: value for key, value in kwargs.items() if key != "before_meter_exit"}))
        if mode["value"] == "timeout":
            d["usage"].abort_call(invocation, definitive=False)
            raise TimeoutError("Private provider body must never enter a task event")
        if mode["value"] == "calling_crash":
            raise KeyboardInterrupt()
        if mode["value"] in {"goal_drift", "paused"}:
            with d["factory"]() as session:
                if mode["value"] == "goal_drift":
                    session.get(Task, d["task_id"]).goal = "A different request"
                else:
                    session.get(Task, d["task_id"]).state = "paused"
                session.commit()
        if mode["value"] == "concurrent":
            await asyncio.sleep(0)
        response = {"rationale": "Different source angle", "newquery": mode["query"]}
        if mode["value"] == "invented_tool":
            response["tools"] = ["send_email"]
        result = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(response)),
            finish_reason="length" if mode["value"] == "truncated" else "stop")])
        mode["receipt"] = synthetic_receipt(kwargs, invocation, estimate_micro=estimate, actual_micro=actual,
            prompt=5, completion=5, source=mode["billing_source"])
        if mode["billing_source"] == "byok":
            mode["receipt"]["actual_micro"] = 0
        checkpoint(result, kwargs, mode["receipt"])
        if mode["after_checkpoint"]:
            mode["after_checkpoint"]()
        if mode["value"] == "meter_exit_error":
            from app.credits.errors import CreditsUnavailable
            raise CreditsUnavailable("Synthetic metering exit did not confirm settlement")
        recovery.confirm(mode["receipt"])
        d["usage"].after_call(invocation, actual, 10)
        return result
    monkeypatch.setattr(metered, "llm", llm)
    d.update(progress=progress, calls=calls, mode=mode, billing=recovery,
        scope={"workspace_id": "ws_lifecycle", "venture_id": "ven_lifecycle"})
    return d


def _record(d, query="initial public query", count=0, response=None, tool="explore_web", error=None):
    with d["usage"].job_context(d["jid"]):
        d["progress"].record_tool(tool, {"query": query}, response or {"ok": True, "result": {"mode": "live", "pages": []}},
            source_count=count, error=error, **d["scope"])


def _reflect(d):
    with d["usage"].job_context(d["jid"]):
        return asyncio.run(d["progress"].reflect_if_due(**d["scope"]))


def _events(d, kind=None):
    from app.db.task_models import TaskEvent
    with d["factory"]() as session:
        query = select(TaskEvent).where(TaskEvent.run_id == d["run_id"])
        if kind:
            query = query.where(TaskEvent.kind == kind)
        return session.scalars(query).all()


def test_reflection_and_private_invoice_checkpoint_precede_meter_exit(progress_db):
    from app.db.task_models import TaskRun, TaskStep
    d, observed = progress_db, []
    _record(d)
    _record(d)
    def before_exit():
        with d["factory"]() as session:
            journal = json.loads(session.get(TaskStep, d["step_id"]).evidence_json)[d["progress"].KEY]
            assert journal["state"] == "responded" and journal["response"]["newquery"] == d["mode"]["query"]
            assert journal["billing"]["format"] == "task_billing_v1" and not d["billing"].proofs
            run = session.get(TaskRun, d["run_id"])
            assert run.reserved_cost_micro == 20 and run.cost_micro == 0
        assert not _events(d, "strategy_switched")
        observed.append(True)
    d["mode"]["after_checkpoint"] = before_exit
    assert _reflect(d) == d["mode"]["query"] and observed == [True]
    assert "before_meter_exit" not in d["calls"][0][1]


def test_failed_meter_exit_keeps_reflection_until_exact_invoice_recovery(progress_db):
    from app.db.task_models import Task, TaskRun, TaskStep
    d = progress_db
    _record(d)
    _record(d)
    d["mode"]["value"] = "meter_exit_error"
    with pytest.raises(d["progress"].ResearchStopped):
        _reflect(d)
    with d["factory"]() as session:
        journal = json.loads(session.get(TaskStep, d["step_id"]).evidence_json)[d["progress"].KEY]
        assert journal["state"] == "responded" and journal["valid"] is True
        assert session.get(TaskRun, d["run_id"]).reserved_cost_micro == 20
    with pytest.raises(d["progress"].ResearchStopped):
        _reflect(d)
    assert len(d["calls"]) == 1 and not _events(d, "strategy_switched")
    d["billing"].confirm(d["mode"]["receipt"])
    with d["factory"]() as session:
        session.get(Task, d["task_id"]).state = session.get(TaskRun, d["run_id"]).state = "working"
        session.commit()
    assert _reflect(d) == d["mode"]["query"] and len(d["calls"]) == 1
    with d["factory"]() as session:
        run = session.get(TaskRun, d["run_id"])
        assert (run.reserved_cost_micro, run.reserved_tokens, run.cost_micro, run.tokens_used) == (0, 0, 7, 10)


@pytest.mark.parametrize("mutation", ["missing_billing", "invoice", "response"])
def test_saved_reflection_cannot_apply_from_tampered_or_missing_invoice(progress_db, mutation):
    from app.db.task_models import TaskStep
    d = progress_db
    _record(d)
    _record(d)
    def tamper():
        with d["factory"]() as session:
            step = session.get(TaskStep, d["step_id"])
            data = json.loads(step.evidence_json)
            if mutation == "missing_billing":
                data[d["progress"].KEY].pop("billing")
            elif mutation == "invoice":
                data[d["progress"].KEY]["billing"]["sha256"] = "different invoice"
            else:
                data[d["progress"].KEY]["response"]["newquery"] = "different unreviewed search query"
            step.evidence_json = json.dumps(data)
            session.commit()
    d["mode"]["after_checkpoint"] = tamper
    with pytest.raises(d["progress"].ResearchStopped):
        _reflect(d)
    with pytest.raises(d["progress"].ResearchStopped):
        _reflect(d)
    assert len(d["calls"]) == 1 and not _events(d, "strategy_switched")


@pytest.mark.parametrize("source", ["byok", "free"])
def test_zero_wallet_reflection_still_requires_scoped_task_invocation(progress_db, source):
    from app.db.task_models import TaskRun
    d = progress_db
    _record(d)
    _record(d)
    d["mode"]["billing_source"] = source
    assert _reflect(d) == d["mode"]["query"]
    assert _reflect(d) == d["mode"]["query"] and len(d["calls"]) == 1
    with d["factory"]() as session:
        run = session.get(TaskRun, d["run_id"])
        assert run.cost_micro == (7 if source == "byok" else 0) and run.tokens_used == 10


@pytest.mark.parametrize("mutation", ["newquery", "valid", "response_digest", "response"])
def test_switched_cache_rechecks_normalized_response_before_returning_query(progress_db, mutation):
    from app.db.task_models import TaskStep
    d = progress_db
    _record(d)
    _record(d)
    assert _reflect(d) == d["mode"]["query"]
    with d["factory"]() as session:
        step = session.get(TaskStep, d["step_id"])
        data = json.loads(step.evidence_json)
        journal = data[d["progress"].KEY]
        if mutation == "response":
            journal["response"]["newquery"] = "a different query"
        else:
            journal[mutation] = False if mutation == "valid" else "a changed cached value"
        step.evidence_json = json.dumps(data)
        session.commit()
    with pytest.raises(d["progress"].ResearchStopped):
        _reflect(d)
    assert len(d["calls"]) == 1 and len(_events(d, "strategy_switched")) == 1


def test_actual_metric_stall_reflects_once_and_binds_scope_without_tool_content(progress_db):
    from app.db.task_models import TaskStep
    d = progress_db
    _record(d, response={"ok": True, "result": {"mode": "live", "pages": [], "private": "Raw tool text withheld"}})
    _record(d, query="a different failed discovery query")
    assert len(_events(d, "strategy_failed")) == 1
    assert _reflect(d) == d["mode"]["query"] and len(d["calls"]) == 1
    assert _reflect(d) == d["mode"]["query"] and len(d["calls"]) == 1
    messages, kwargs = d["calls"][0]
    assert kwargs["purpose"] == "task.reflect" and kwargs["tier"] == "flash" and kwargs["max_tokens"] == 512
    assert kwargs["response_schema"]["additionalProperties"] is False and "tools" not in kwargs
    payload = json.loads(messages[1]["content"])
    assert payload["trusted_goal"] == "Research three public sources about bakery scheduling"
    assert [row["metric"] for row in payload["diagnostics"]] == [0, 0]
    assert "Raw tool text" not in json.dumps(payload) and "failed discovery" not in json.dumps(payload)
    for event in _events(d, "tool_observed"):
        assert (event.task_id, event.run_id, event.step_id) == (d["task_id"], d["run_id"], d["step_id"])
        assert len(json.loads(event.data_json)["action"]) == 64
    assert "Raw tool text" not in " ".join(event.data_json + event.message for event in _events(d))
    assert "Different source angle" not in " ".join(event.data_json for event in _events(d))
    with d["factory"]() as session:
        assert session.get(TaskStep, d["step_id"]).strategy_id == "research:2"


@pytest.mark.parametrize("pattern", ["four_repeats", "ping_pong"])
def test_exact_action_result_patterns_are_journaled_from_real_boundary(progress_db, pattern):
    d = progress_db
    count = {"four_repeats": 4, "ping_pong": 12}[pattern]
    for i in range(count):
        _record(d, query=str(i % 2) if pattern == "ping_pong" else "one action", tool="summarize_runs",
            response={"ok": True, "result": "synthetic " + str(i % 2 if pattern == "ping_pong" else 0)})
        assert len(_events(d, "strategy_failed")) == int(i == count - 1)


def test_actual_source_metric_improvement_does_not_reflect(progress_db):
    d = progress_db
    _record(d, count=1)
    _record(d, query="second source", count=2)
    assert not _events(d, "strategy_failed") and _reflect(d) is None and d["calls"] == []


def test_same_error_detector_cannot_authorize_reflection_of_unknown_error(progress_db):
    from app.tasks.contracts import stuck
    d = progress_db
    assert stuck([{"action": "a", "result": str(i), "error": "unknown"} for i in range(3)])
    with pytest.raises(d["progress"].ResearchStopped):
        _record(d, response={"ok": False, "error": "unknown_source_error"})
    assert len(_events(d, "tool_observed")) == 1 and d["calls"] == []
    with pytest.raises(d["progress"].ResearchStopped):
        _reflect(d)


def test_applied_query_is_not_forced_again_after_actual_observation(progress_db):
    d = progress_db
    _record(d)
    _record(d)
    query = _reflect(d)
    _record(d, query=query, count=1)
    assert _reflect(d) is None and len(d["calls"]) == 1


def test_zero_cost_uncertain_invocation_still_blocks_strategy(progress_db):
    from app.db.task_models import Task, TaskRun
    d = progress_db
    _record(d)
    _record(d)
    with d["usage"].job_context(d["jid"]):
        invocation = d["usage"].before_call(0, 0)
        d["usage"].abort_call(invocation, definitive=False)
    with d["factory"]() as session:
        # A zero reservation cannot erase an unknown transport outcome.
        session.get(Task, d["task_id"]).state = session.get(TaskRun, d["run_id"]).state = "working"
        session.commit()
    with pytest.raises(d["progress"].ResearchStopped):
        _reflect(d)
    assert d["calls"] == []


@pytest.mark.parametrize("code", ["search_unavailable", "page_read_unavailable", "platform_lookup_unavailable"])
def test_actual_explore_unavailable_codes_stop_without_guessing_auth_or_policy(progress_db, code):
    from app.db.task_models import Task
    d = progress_db
    with pytest.raises(d["progress"].ResearchStopped):
        _record(d, response={"ok": True, "result": {"mode": "error", "error_code": code}})
    with d["factory"]() as session:
        reason = json.loads(session.get(Task, d["task_id"]).blocker_json)
        assert reason["class"] == "needs_input" and "checked" in reason["reason"]
    assert d["calls"] == []


@pytest.mark.parametrize("query", [" INITIAL   PUBLIC QUERY ", "a query unrelated to the proposed strategy"])
def test_first_search_after_switch_requires_changed_reflected_query(progress_db, query):
    d = progress_db
    _record(d)
    _record(d)
    _reflect(d)
    with d["usage"].job_context(d["jid"]), pytest.raises(d["progress"].ResearchStopped):
        d["progress"].before_search(query, **d["scope"])
    assert len(d["calls"]) == 1


@pytest.mark.parametrize("mode", ["timeout", "calling_crash", "invented_tool", "truncated", "goal_drift"])
def test_unknown_invalid_or_changed_contract_never_replays_reflection(progress_db, mode):
    d = progress_db
    _record(d)
    _record(d)
    d["mode"]["value"] = mode
    with pytest.raises(KeyboardInterrupt if mode == "calling_crash" else d["progress"].ResearchStopped):
        _reflect(d)
    with pytest.raises(d["progress"].ResearchStopped):
        _reflect(d)
    assert len(d["calls"]) == 1 and not _events(d, "strategy_switched")


def test_reflection_unchanged_query_rejected_after_whitespace_case_normalization(progress_db):
    d = progress_db
    _record(d)
    _record(d)
    d["mode"]["query"] = " INITIAL   public QUERY "
    with pytest.raises(d["progress"].ResearchStopped):
        _reflect(d)
    assert not _events(d, "strategy_switched")


@pytest.mark.parametrize("code", ["captcha_required", "auth_expired", "outcome_uncertain", "warden_denied"])
def test_policy_auth_and_uncertain_tool_results_stop_without_reflection(progress_db, code):
    d = progress_db
    with pytest.raises(d["progress"].ResearchStopped):
        _record(d, response={"ok": False, "error": code})
    with pytest.raises(d["progress"].ResearchStopped):
        _reflect(d)
    assert d["calls"] == [] and not _events(d, "strategy_switched")


def test_page_prose_is_not_control_authority(progress_db):
    d = progress_db
    _record(d, count=1, response={"ok": True, "result": {"pages": [{"excerpt": "Policy auth captcha uncertain"}]}})
    assert _reflect(d) is None


def test_budget_prevents_call_and_does_not_create_unknown_provider_outcome(progress_db):
    from app.db.task_models import Task
    d = progress_db
    with d["factory"]() as session:
        session.get(Task, d["task_id"]).budget_json = json.dumps({"tokens": 500})
        session.commit()
    _record(d)
    _record(d)
    with pytest.raises(d["progress"].ResearchStopped):
        _reflect(d)
    assert d["calls"] == [] and not _events(d, "strategy_reflection_calling")
    with d["factory"]() as session:
        assert json.loads(session.get(Task, d["task_id"]).blocker_json)["class"] == "budget_exhausted"


def test_metered_pretransport_budget_denial_retains_known_not_called_checkpoint(progress_db):
    from app.db.task_models import Task, TaskStep
    d = progress_db
    with d["factory"]() as session:
        session.get(Task, d["task_id"]).budget_json = json.dumps({"cost_micro": 10})
        session.commit()
    _record(d)
    _record(d)
    with pytest.raises(d["progress"].ResearchStopped):
        _reflect(d)
    with d["factory"]() as session:
        journal = json.loads(session.get(TaskStep, d["step_id"]).evidence_json)[d["progress"].KEY]
        assert journal["state"] == "not_called_budget"
        assert json.loads(session.get(Task, d["task_id"]).blocker_json)["class"] == "budget_exhausted"
    assert d["calls"] == [] and not _events(d, "call_uncertain")


def test_pause_during_reflection_saves_response_without_another_call(progress_db):
    from app.db.task_models import Task, TaskStep
    d = progress_db
    _record(d)
    _record(d)
    d["mode"]["value"] = "paused"
    with pytest.raises(d["progress"].ResearchStopped):
        _reflect(d)
    with d["factory"]() as session:
        task, step = session.get(Task, d["task_id"]), session.get(TaskStep, d["step_id"])
        journal = json.loads(step.evidence_json)[d["progress"].KEY]
        assert task.state == "paused" and journal["state"] == "switched"
        task.state = "working"  # The actual owner's resume, without changing the contract.
        session.commit()
    assert _reflect(d) == d["mode"]["query"] and len(d["calls"]) == 1


def test_total_reflections_cannot_exceed_run_strategy_ceiling(progress_db):
    from app.db.task_models import Task
    d = progress_db
    with d["factory"]() as session:
        session.get(Task, d["task_id"]).budget_json = json.dumps({"max_strategies": 2})
        session.commit()
    _record(d)
    _record(d)
    query = _reflect(d)
    with d["usage"].job_context(d["jid"]):
        d["progress"].before_search(query, **d["scope"])
    _record(d, query=query)
    _record(d, query="a third failed discovery query")
    with pytest.raises(d["progress"].ResearchStopped):
        _reflect(d)
    assert len(d["calls"]) == 1


@pytest.mark.parametrize("change", ["wrong_scope", "owner_drift", "canceled", "paused", "wall_limit"])
def test_scope_and_lifecycle_guard_precede_reflection(progress_db, change):
    from datetime import timedelta
    from app.db.models import Workspace
    from app.db.task_models import Task
    d = progress_db
    _record(d)
    _record(d)
    with d["factory"]() as session:
        if change == "wrong_scope":
            d["scope"]["workspace_id"] = "another_workspace"
        elif change == "owner_drift":
            session.get(Workspace, "ws_lifecycle").owner_id = "another_founder"
        elif change in {"canceled", "paused"}:
            session.get(Task, d["task_id"]).state = change
        else:
            d["clock"][0] += timedelta(seconds=1800)
        session.commit()
    with pytest.raises(d["progress"].ResearchStopped):
        _reflect(d)
    assert d["calls"] == []


def test_concurrent_reflection_claim_has_one_paid_intent(progress_db):
    d = progress_db
    _record(d)
    _record(d)
    d["mode"]["value"] = "concurrent"
    async def concurrent():
        return await asyncio.gather(d["progress"].reflect_if_due(**d["scope"]),
            d["progress"].reflect_if_due(**d["scope"]), return_exceptions=True)
    with d["usage"].job_context(d["jid"]):
        results = asyncio.run(concurrent())
    assert len(d["calls"]) == 1 and len(_events(d, "strategy_reflection_calling")) == 1
    assert all(isinstance(result, d["progress"].ResearchStopped) for result in results)


def test_actual_harness_turn_reflects_from_real_pages_then_requires_new_query(progress_db, monkeypatch):
    from app.core.warden_client import CheckResponse, WardenClient
    from app.planning.execute import _harness_research
    d = progress_db
    reads, requests = [], []
    async def explore(query):
        reads.append(query)
        urls = ["https://observed.example/one"]
        if len(reads) == 3:
            urls.append("https://observed.example/two")
        return {"mode": "live", "hits": [{"url": url} for url in urls],
            "pages": [{"url": url, "excerpt": "Observed synthetic page", "chars": 23} for url in urls]}
    async def model(messages, **kwargs):
        requests.append((messages, kwargs))
        if len(requests) <= 3:
            query = "initial public query" if len(requests) < 3 else d["mode"]["query"]
            return {"role": "assistant", "content": None, "tool_calls": [{"id": "search_" + str(len(requests)),
                "type": "function", "function": {"name": "explore_web", "arguments": json.dumps({"query": query})}}]}
        return {"role": "assistant", "content": "Synthetic source brief. Gaps: current pricing."}
    class Allow(WardenClient):
        async def check(self, **kwargs):
            return CheckResponse(decision="allow", reason="synthetic fixture")
        async def kill_switch(self):
            return False
    monkeypatch.setattr("app.research.explore.explore_web", explore)
    with d["usage"].job_context(d["jid"]):
        note = asyncio.run(_harness_research(venture_id="ven_lifecycle", title="Research bakery scheduling",
            detail="Observe source pages", speaker={"id": "synthetic_researcher", "role": "researcher"},
            workspace_id="ws_lifecycle", llm=model, warden=Allow(base_url="http://127.0.0.1:9")))
    assert reads == ["initial public query", "initial public query", d["mode"]["query"]]
    assert len(d["calls"]) == 1 and len(note.observations) == 2
    assert [json.loads(row.data_json)["metric"] for row in _events(d, "tool_observed")] == [1, 1, 2]
    assert d["mode"]["query"] in requests[2][0][-1]["content"]
    assert requests[0][1]["tools"] == requests[2][1]["tools"]


def test_actual_executor_unavailable_tool_stops_with_no_reflection_or_fallback_call(generic, monkeypatch):
    from app.core.warden_client import CheckResponse, WardenClient
    from app.db.task_models import Task, TaskEvent, TaskStep
    from app.tasks import progress
    from tests.unit.tasks.test_generic_goals import _execute, _linked
    d = generic
    reads, calls, reflections = [], [], []
    monkeypatch.setattr(progress, "db", lambda: d["factory"])
    from app.credits import metered
    async def forbidden_reflection(*args, **kwargs):
        reflections.append(True)
        raise AssertionError("Unavailable outcomes cannot authorize reflection")
    monkeypatch.setattr(metered, "llm", forbidden_reflection)
    async def model(messages, **kwargs):
        invocation = d["usage"].before_call(7, 10)
        calls.append(True)
        d["usage"].after_call(invocation, 7, 10)
        return {"role": "assistant", "content": None, "tool_calls": [{"id": "source_unavailable",
            "type": "function", "function": {"name": "explore_web", "arguments": '{"query":"public bakery scheduling"}'}}]}
    async def unavailable(query):
        reads.append(query)
        return {"mode": "error", "error_code": "search_unavailable", "hits": [], "pages": []}
    async def forbidden_fallback(*args, **kwargs):
        raise AssertionError("Stopped research cannot request a new fallback search")
    class Allow(WardenClient):
        async def check(self, **kwargs):
            return CheckResponse(decision="allow", reason="synthetic fixture")
        async def kill_switch(self):
            return False
    async def harness(**kwargs):
        return await d["real_harness"](**kwargs, llm=model, warden=Allow(base_url="http://127.0.0.1:9"))
    monkeypatch.setattr(d["explore"], "explore_web", unavailable)
    monkeypatch.setattr(d["execute"], "_research_notes", forbidden_fallback)
    monkeypatch.setattr(d["execute"], "_harness_research", harness)
    check = {"type": "deterministic", "spec": {"predicate": "at_least", "metric": "source_count", "count": 1}}
    pid, task_id, _, step_id, jid = _linked(d, "Research source pages about bakery scheduling", check)
    try:
        outcome = _execute(d, pid, jid)
    except d["execute"].TaskRunError:
        outcome = {"status": "incomplete"}
    assert outcome["status"] != "done" and reads == ["public bakery scheduling"] and calls == [True] and reflections == []
    with d["factory"]() as session:
        task = session.get(Task, task_id)
        step = session.get(TaskStep, step_id)
        saved = step.evidence_json
        assert task.state == "input_required" and json.loads(task.blocker_json)["class"] == "needs_input"
        d["service"].after_job(session, jid)
        session.commit()
        assert step.evidence_json == saved and task.state == "input_required"
        assert not session.scalar(select(TaskEvent.id).where(TaskEvent.task_id == task_id, TaskEvent.kind == "strategy_reflection_calling"))


def test_actual_twenty_source_goal_uses_bounded_remaining_tool_budget(progress_db, monkeypatch):
    from app.core.warden_client import CheckResponse, WardenClient
    from app.db.task_models import Task, TaskStep
    from app.planning.execute import _harness_research
    d = progress_db
    with d["factory"]() as session:
        task = session.get(Task, d["task_id"])
        task.goal = "Research twenty public source pages about bakery scheduling"
        task.done_test_json = json.dumps({"type": "deterministic", "spec": {"predicate": "at_least", "metric": "source_count", "count": 20}})
        session.get(TaskStep, d["step_id"]).done_test_json = task.done_test_json
        session.commit()
    reads, requests = [], []
    async def explore(query):
        reads.append(query)
        urls = [f"https://observed.example/{len(reads)}/{n}" for n in range(3)]
        return {"mode": "live", "hits": [{"url": url} for url in urls],
            "pages": [{"url": url, "excerpt": "Observed synthetic page", "chars": 23} for url in urls]}
    async def model(messages, **kwargs):
        requests.append(True)
        if len(requests) <= 7:
            return {"role": "assistant", "content": None, "tool_calls": [{"id": "search_" + str(len(requests)),
                "type": "function", "function": {"name": "explore_web", "arguments": json.dumps({"query": "public bakery sources " + str(len(requests))})}}]}
        return {"role": "assistant", "content": "Synthetic twenty-source brief. Gaps: pricing remains unverified."}
    class Allow(WardenClient):
        async def check(self, **kwargs):
            return CheckResponse(decision="allow", reason="synthetic fixture")
        async def kill_switch(self):
            return False
    monkeypatch.setattr("app.research.explore.explore_web", explore)
    with d["usage"].job_context(d["jid"]):
        note = asyncio.run(_harness_research(venture_id="ven_lifecycle", title=task.goal,
            detail="Observe source pages", speaker={"id": "synthetic_researcher", "role": "researcher"},
            workspace_id="ws_lifecycle", llm=model, warden=Allow(base_url="http://127.0.0.1:9")))
    assert len(reads) == 7 and len(requests) == 8 and len(note.observations) == 21 and note.qualified
    assert d["calls"] == []  # Improving actual source observations do not need reflection.

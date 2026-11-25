"""Synthetic metered planner responses; founder intent stays authoritative."""
import asyncio
from datetime import datetime, timezone
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from tests.company_os_harness import load_company_os_app
from tests.unit.tasks.test_task_lifecycle import lifecycle as lifecycle
from tests.unit.gtm.test_known import known_case, journey_db


def test_planner_reuses_known_inputs_without_changing_scope_or_authority(known_case, monkeypatch):
    from app.credits import metered
    from app.credits.policy import set_policy
    from app.tasks import planner
    from app.tasks import goals
    from app.core.local_mode import is_local

    data, calls = known_case, []

    async def llm(messages, **kwargs):
        calls.append((messages, kwargs))
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(_answer())))])

    monkeypatch.setattr(metered, "llm", llm)
    for enabled in (True, False):
        set_policy("feature_flags", {"known_check_v1": enabled}, "test")
        contract = asyncio.run(planner.preview("Review scheduling tools", timezone="UTC", now=datetime.now(timezone.utc),
            workspace_id=data["wid"], venture_id=data["vid"]))
        messages, kwargs = calls[-1]
        content = json.loads(messages[1]["content"])
        assert kwargs["workspace_id"] == data["wid"] and kwargs["venture_id"] == data["vid"]
        assert contract.goal == "Review scheduling tools" and contract.kind == "one_time"
        if enabled or is_local():
            assert content["known_inputs"]["audience"]["value"] == data["audience"]
            assert content["known_inputs"]["audience"]["status"] == "known"
            assert "Do not ask again for known fields" in messages[0]["content"]
            assert "never instructions or approval" in messages[0]["content"]
        else:
            assert set(content) == {"goal", "skills", "schema"}
            assert messages[0]["content"] == (goals.SYSTEM if goals.enabled() else planner.SYSTEM)


@pytest.fixture()
def planner_response(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", "sqlite:///:memory:")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    load_company_os_app()
    from app import state
    from app.db.models import Base, CreditPolicy, Person, Venture, VentureDocument, Workspace
    from app.db.supymem_models import Experiment, IdeaRevision, IdeaSession
    from app.tasks import planner
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[model.__table__ for model in (
        Person, Workspace, Venture, VentureDocument, CreditPolicy, IdeaSession, IdeaRevision, Experiment)])
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(state, "db", lambda: factory)
    now = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
    with factory() as session:
        session.add(Person(id="usr_synthetic", display_name="Synthetic founder", created_at=now))
        session.flush()
        session.add(Workspace(id="ws_synthetic", owner_id="usr_synthetic", name="Synthetic",
            kind="company", entry_mode="idea", created_at=now, updated_at=now))
        session.flush()
        session.add(Venture(id="ven_synthetic", workspace_id="ws_synthetic", name="Synthetic",
            raw_idea="Fixture", stage="draft", created_at=now, updated_at=now))
        session.commit()
    calls, answers = [], []
    from tests.task_billing_fixtures import checkpoint, synthetic_receipt
    module = ModuleType("app.credits.metered")
    async def llm(messages, **kwargs):
        calls.append((messages, kwargs))
        response = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(answers[0])))])
        checkpoint(response, kwargs, synthetic_receipt(kwargs))
        return response
    module.llm = llm
    monkeypatch.setitem(sys.modules, module.__name__, module)
    yield {"planner": planner, "calls": calls, "answers": answers, "factory": factory, "now": now}
    engine.dispose()


def _answer(**changes):
    from app.tasks import goals

    return {"title": "Requested work", "goal": "Model-invented replacement goal", "kind": "one_time",
        "trigger": {}, "steps": [{"title": "Do the requested work", "skill": "task.run", "inputs": {},
                                  **({"subgoal": "Prepare the requested result."} if goals.enabled() else {})}],
        **changes}


def _preview(d, goal, body=None):
    d["answers"][:] = [body or _answer()]
    return asyncio.run(d["planner"].preview(goal, timezone="Asia/Kolkata", now=d["now"],
        workspace_id="ws_synthetic", venture_id="ven_synthetic"))


def test_planner_uses_one_metered_structured_call_with_exact_scope(planner_response):
    d = planner_response
    contract = _preview(d, "Draft a short welcome note")
    assert len(d["calls"]) == 1
    messages, options = d["calls"][0]
    assert options["purpose"] == "task.plan" and options["max_tokens"] == 3000
    assert options["workspace_id"] == "ws_synthetic" and options["venture_id"] == "ven_synthetic"
    assert options["response_schema"]["type"] == "object" and "schema" not in options
    assert json.loads(messages[1]["content"])["goal"] == contract.goal == "Draft a short welcome note"


def test_planner_checkpoints_the_normalized_contract_before_meter_exit(planner_response):
    d, saved = planner_response, []
    d["answers"][:] = [_answer()]
    contract = asyncio.run(d["planner"].preview("Draft a short welcome note", timezone="UTC", now=d["now"],
        workspace_id="ws_synthetic", venture_id="ven_synthetic", request_id="synthetic-plan-checkpoint",
        before_meter_exit=lambda normalized, receipt: saved.append((normalized.model_dump(), receipt))))
    assert len(saved) == 1 and saved[0][0] == contract.model_dump()
    assert saved[0][1]["request_id"] == "synthetic-plan-checkpoint" and saved[0][1]["task_invocation"] is None


def test_model_cannot_raise_a_hidden_task_allowance_or_weaken_the_default_rubric(planner_response):
    from app.tasks.contracts import Budget
    from app.tasks import goals
    contract = _preview(planner_response, "Find developers worth following", _answer(
        budget={"tokens": 2_000_000, "cost_micro": 50_000_000, "wall_seconds": 86400,
                "attempts": 100, "tool_calls": 500, "max_strategies": 3}, tolerance=0.01,
        done_test={"type": "rubric", "spec": {"predicate": "rubric", "criteria": ["Each developer has current public work relevant to AI agents"]}}))
    assert contract.budget == (Budget(wall_seconds=86400) if goals.enabled() else Budget()) and contract.tolerance == 0.8


def test_a_smaller_requested_allowance_stays_small(planner_response):
    contract = _preview(planner_response, "Draft a short welcome note", _answer(budget={"tokens": 1000, "cost_micro": 10_000}))
    assert contract.budget.tokens == 1000 and contract.budget.cost_micro == 10_000


def test_multiple_generic_steps_cannot_redo_the_whole_founder_goal(planner_response):
    from app.tasks import goals
    with pytest.raises(ValueError, match="specific result" if goals.enabled() else "Repeated whole-goal steps"):
        _preview(planner_response, "Compare scheduling tools", _answer(steps=[
            {"title": "Research the tools", "skill": "task.run"},
            {"title": "Write the comparison", "skill": "task.run"}]))


def test_model_cannot_replace_goal_or_invent_recurrence(planner_response):
    contract = _preview(planner_response, "Draft a short welcome note", _answer(kind="scheduled",
        trigger={"rrule": "FREQ=DAILY;BYHOUR=1", "tz": "UTC"}))
    assert contract.goal == "Draft a short welcome note" and contract.kind == "one_time" and contract.trigger == {}


def test_founder_frequency_is_resolved_in_code_in_founder_timezone(planner_response):
    contract = _preview(planner_response, "Every morning at 8, draft a short welcome note", _answer(kind="event",
        trigger={"event_type": "unrelated.event"}))
    assert contract.kind == "scheduled" and contract.trigger["tz"] == "Asia/Kolkata"
    assert contract.trigger["rrule"] == "FREQ=DAILY;BYHOUR=8;BYMINUTE=0;BYSECOND=0"


@pytest.mark.parametrize("domain", ["Warehouse dispatch teams", "Families planning nature walks"])
def test_recurring_existing_skill_keeps_trigger_without_one_time_goal_authority(planner_response, monkeypatch, domain):
    from app.tasks.contracts import Budget
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    goal = "Every Friday at 8, draft a welcome note for " + domain
    contract = _preview(planner_response, goal)
    assert contract.kind == "scheduled" and contract.trigger["tz"] == "Asia/Kolkata"
    assert contract.goal == goal and contract.steps[0].skill == "task.run"
    assert contract.steps[0].subgoal is None and not contract.steps[0].output_bindings
    assert contract.budget == Budget()


def test_founder_event_is_resolved_in_code(planner_response):
    contract = _preview(planner_response, "When someone replies, draft a follow-up for my approval")
    assert contract.kind == "event" and contract.trigger == {"event_type": "outreach.reply", "filter": {}}


def test_ambiguous_schedule_asks_before_metered_call(planner_response):
    with pytest.raises(ValueError, match="frequency or event"):
        _preview(planner_response, "Whenever it seems useful, draft a welcome note")
    assert planner_response["calls"] == []


def test_invented_server_skill_is_rejected(planner_response):
    with pytest.raises(ValueError):
        _preview(planner_response, "Draft a note", _answer(steps=[{"title": "Execute code", "skill": "server.eval",
            "inputs": {"code": "arbitrary server code"}}]))


@pytest.mark.parametrize("domain", ["warehouse dispatch teams", "weekend hiking groups"])
@pytest.mark.parametrize("invalid", ["unknown_skill", "invalid_input"])
def test_local_generated_preparation_is_repaired_without_a_second_paid_call(planner_response, monkeypatch, domain, invalid):
    from app.core import local_capabilities

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(local_capabilities, "enabled", lambda name: name == "background_work")
    goal = "Draft a welcome note for " + domain
    step = {"title": "Draft your welcome note", "skill": "legacy.prepare" if invalid == "unknown_skill" else "task.run",
        "subgoal": goal, "inputs": {} if invalid == "unknown_skill" else {"task_id": 42}}
    saved = []
    data = planner_response
    data["answers"][:] = [_answer(steps=[step])]
    contract = asyncio.run(data["planner"].preview(goal, timezone="UTC", now=data["now"],
        workspace_id="ws_synthetic", venture_id="ven_synthetic", request_id="local-plan",
        before_meter_exit=lambda normalized, receipt: saved.append(normalized.model_dump())))
    assert contract.goal == goal and contract.steps[0].subgoal == goal
    assert contract.steps[0].skill == "task.run" and contract.steps[0].inputs == {}
    assert len(data["calls"]) == 1 and saved == [contract.model_dump()]


@pytest.mark.parametrize("domain", ["warehouse dispatch teams", "weekend hiking groups"])
@pytest.mark.parametrize("skill,inputs", [("legacy.prepare", {"code": "execute anything"}),
    ("task.run", {"workspace_id": "foreign"}), ("pagebuilder.publish", {"build_run_id": 42})])
def test_local_planner_does_not_repair_tool_authority_or_consequential_inputs(planner_response, monkeypatch, domain, skill, inputs):
    from app.core import local_capabilities

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(local_capabilities, "enabled", lambda name: False)
    with pytest.raises(ValueError):
        _preview(planner_response, "Draft a welcome note for " + domain,
            _answer(steps=[{"title": "Requested work", "skill": skill, "inputs": inputs}]))


@pytest.mark.parametrize("domain", ["warehouse dispatch teams", "weekend hiking groups"])
@pytest.mark.parametrize("skill,inputs", [("legacy.prepare", {}), ("task.run", {"task_id": 42})])
def test_invalid_supplied_local_contract_keeps_validation_and_names_recovery(planner_response, monkeypatch, domain, skill, inputs):
    from app.tasks.contracts import Contract

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    goal = "Draft a welcome note for " + domain
    contract = Contract(title=goal, goal=goal, steps=[{"title": goal, "skill": skill, "inputs": inputs}])
    with pytest.raises(ValueError, match="Tasks") as error:
        planner_response["planner"].validate_contract(contract, goal=goal)
    assert "not available yet" not in str(error.value)
    if skill == "legacy.prepare":
        assert goal in str(error.value)


@pytest.mark.parametrize("domain", ["warehouse dispatch teams", "weekend hiking groups"])
@pytest.mark.parametrize("skill,name", [("outreach.draft", "content"), ("gtm.find", "find_customers")])
def test_local_unregistered_capability_step_keeps_its_exact_setup_action(planner_response, monkeypatch, domain, skill, name):
    from app.core import local_capabilities
    from app.tasks.contracts import Contract

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(local_capabilities, "enabled", lambda capability: capability == "background_work")
    message = "In Settings > Keys and connections > Your AI provider, save your Chat model, then run your requested work again."
    monkeypatch.setattr(local_capabilities, "blocked_message", lambda capability: message if capability == name else "Unexpected capability")
    goal = "Prepare customer conversations for " + domain
    contract = Contract(title=goal, goal=goal, steps=[{"title": goal, "skill": skill, "subgoal": goal}])
    with pytest.raises(ValueError) as error:
        planner_response["planner"].validate_contract(contract, goal=goal)
    assert str(error.value) == message


@pytest.mark.parametrize("domain", ["warehouse dispatch teams", "weekend hiking groups"])
@pytest.mark.parametrize("unsafe", [False, True])
def test_local_saved_unknown_step_replans_only_preparation(lifecycle, monkeypatch, domain, unsafe):
    from app.core import local_capabilities
    from app.db.models import PlannedTask
    from app.db.task_models import Task, TaskRun, TaskStep
    from app.planning import execute as planned, store
    from app.tasks.contracts import Contract

    data, calls = lifecycle, []
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(local_capabilities, "enabled", lambda name: False)
    goal = "Draft a welcome note for " + domain
    PlannedTask.__table__.create(data["factory"].kw["bind"], checkfirst=True)
    with data["factory"]() as session:
        session.add(PlannedTask(id="planned_local", workspace_id="ws_lifecycle", venture_id="ven_lifecycle",
            title=goal, detail=goal, why_now="You requested this work.", status="open",
            created_at=data["clock"][0], updated_at=data["clock"][0]))
        task = data["service"].create(session, workspace_id="ws_lifecycle", venture_id="ven_lifecycle",
            owner_person_id="usr_lifecycle", contract=Contract(title=goal, goal=goal,
                steps=[{"title": goal, "skill": "task.run"}]))
        run = session.scalar(select(TaskRun).where(TaskRun.task_id == task.id))
        step = session.scalar(select(TaskStep).where(TaskStep.run_id == run.id))
        task_id, run_id, step_id = task.id, run.id, step.id
        scope = json.loads(task.scope_json)
        scope["steps"][0].update(skill="legacy.prepare", inputs={"code": "arbitrary execution"} if unsafe else {})
        task.scope_json = json.dumps(scope)
        step.skill = "legacy.prepare"
        session.commit()
    monkeypatch.setattr(store, "enqueue_task", lambda *args, **kwargs: {"id": "planned_local"})
    monkeypatch.setattr(planned, "start_planned_task", lambda *args: calls.append(args) or {})
    asyncio.run(data["execution"].execute({"task_run_id": run_id, "workspace_id": "ws_lifecycle", "venture_id": "ven_lifecycle"}))
    with data["factory"]() as session:
        task, step = session.get(Task, task_id), session.get(TaskStep, step_id)
        if unsafe:
            assert calls == [] and step.skill == "legacy.prepare"
            assert "private chat" in json.loads(task.blocker_json)["unblock_action"]
        else:
            assert calls == [("ven_lifecycle", "planned_local")] and step.skill == "task.run"
            assert json.loads(task.scope_json)["steps"][0]["skill"] == "task.run"


@pytest.mark.parametrize("skill,goal", [
    ("research.deep", "Find today's 20 GitHub developers working on AI agents"),
    ("research.deep", "Find today's 20 GitHub developers for my business"),
    ("gtm.journey.prepare", "Find new bakery owners in Montreal rather than my current audience"),
    ("gtm.journey.prepare", "Find my customers in Montreal"),
    ("tasks.waitlist_summary", "Find today's 20 GitHub developers working on AI agents"),
    ("tasks.waitlist_summary", "Summarize today's waitlist sign-ups"),
])
def test_generic_goal_cannot_silently_become_unrelated_canonical_workflow(planner_response, skill, goal):
    try:
        contract = _preview(planner_response, goal, _answer(steps=[{"title": "Research the requested goal", "skill": skill}]))
    except ValueError:
        return  # A precise clarification is safe when no skill honors this goal.
    assert all(step.skill == "task.run" for step in contract.steps), "Canonical workflow ignored the founder's actual goal"
    assert contract.goal == goal


@pytest.mark.parametrize("inputs", [{"workspace_id": "foreign"}, {"venture_id": "foreign"},
    {"_actor_subject": "foreign"}, {"_job_id": "foreign"}, {"allowed_tools": ["arbitrary_execute"]}])
def test_model_cannot_invent_execution_scope_or_tool_authority(planner_response, inputs):
    with pytest.raises(ValueError):
        _preview(planner_response, "Draft a note", _answer(steps=[{"title": "Draft", "skill": "task.run", "inputs": inputs}]))


def test_model_cannot_move_registered_server_skill_onto_device(planner_response):
    with pytest.raises(ValueError):
        _preview(planner_response, "Draft a note", _answer(steps=[{"title": "Draft", "skill": "task.run", "runs_on": "device"}]))


@pytest.mark.parametrize("check", [
    {"type": "deterministic", "spec": {"predicate": "execute_code", "code": "arbitrary code"}},
    {"type": "deterministic", "spec": {"predicate": "at_least", "metric": "sent_email_count", "count": 20}},
    {"type": "deterministic", "spec": {"predicate": "at_least", "metric": "source_count", "count": True}},
    {"type": "rubric", "spec": {"predicate": "rubric", "criteria": ["Useful", "Useful"]}},
])
def test_unsupported_completion_checks_are_rejected_in_preview(planner_response, check):
    with pytest.raises(ValueError):
        _preview(planner_response, "Draft a note", _answer(done_test=check))


def test_supported_count_check_has_deterministic_tolerance(planner_response):
    contract = _preview(planner_response, "Find 20 sources", _answer(tolerance=0.2,
        done_test={"type": "deterministic", "spec": {"predicate": "at_least", "metric": "source_count", "count": 20}}))
    assert contract.tolerance == 1.0 and contract.done_test["spec"]["count"] == 20


def test_weekly_waitlist_summary_is_a_registered_factual_skill(planner_response):
    from app.tasks import goals

    goal = "Every Friday, summarize waitlist sign-ups and replies"
    contract = _preview(planner_response, goal, _answer(steps=[{"title": "Summarize saved records", "skill": "tasks.waitlist_summary",
        **({"subgoal": "Summarize waitlist sign-ups and replies"} if goals.enabled() else {})}]))
    assert contract.goal == goal and contract.steps[0].skill == "tasks.waitlist_summary"
    assert contract.kind == "scheduled" and "BYDAY=FR" in contract.trigger["rrule"]


@pytest.mark.parametrize("matching", [False, True])
def test_saved_planned_task_must_match_actual_founder_goal_before_admission(lifecycle, monkeypatch, matching):
    from app.db.models import Job, PlannedTask
    from app.db.task_models import Task, TaskRun
    from app.planning import execute as planned
    from app.tasks.contracts import Contract
    d, goal, calls = lifecycle, "Draft one welcome note for bakery owners", []
    engine = d["factory"].kw["bind"]
    PlannedTask.__table__.create(engine, checkfirst=True)
    with d["factory"]() as session:
        session.add(PlannedTask(id="planned_synthetic", workspace_id="ws_lifecycle", venture_id="ven_lifecycle",
            title=goal if matching else "Unrelated venture research", detail=goal if matching else "A different goal",
            why_now="Synthetic", status="open", created_at=d["clock"][0], updated_at=d["clock"][0]))
        session.commit()
    contract = Contract(title="Requested draft", goal=goal,
        steps=[{"title": "Draft the note", "skill": "task.run", "inputs": {"task_id": "planned_synthetic"}}])
    try:
        with d["factory"]() as session:
            task = d["service"].create(session, workspace_id="ws_lifecycle", venture_id="ven_lifecycle",
                owner_person_id="usr_lifecycle", contract=contract)
            session.commit()
            task_id = task.id
    except ValueError:
        assert not matching
        return
    def start(venture_id, task_id):
        calls.append(task_id)
        with d["factory"]() as session:
            jid = d["jobs"].enqueue(session, "task.run", {"task_id": task_id, "_actor_subject": "usr_lifecycle"},
                workspace_id="ws_lifecycle", venture_id=venture_id, dedupe_key="synthetic-planned-admission")
            session.commit()
        return {"job_id": jid}
    monkeypatch.setattr(planned, "start_planned_task", start)
    with d["factory"]() as session:
        run = session.scalar(select(TaskRun).where(TaskRun.task_id == task_id))
        run_id = run.id
    asyncio.run(d["execution"].execute({"task_run_id": run_id,
        "workspace_id": "ws_lifecycle", "venture_id": "ven_lifecycle"}))
    with d["factory"]() as session:
        task = session.get(Task, task_id)
        if matching:
            assert calls == ["planned_synthetic"] and task.goal == goal
        else:
            assert not calls and task.state == "input_required"
            assert session.scalar(select(Job.id).where(Job.kind == "task.run")) is None

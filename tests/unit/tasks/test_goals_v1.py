"""Serial goal contracts and exact approvals, across unrelated venture domains."""
import asyncio
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tests.unit.tasks.test_task_lifecycle import lifecycle as lifecycle
from tests.unit.tasks.test_chat import chat_db as chat_db, call as chat_call, source as chat_source
from tests.company_os_harness import configure_local_test_model


@pytest.fixture(params=["Industrial pump suppliers", "Home baking enthusiasts"])
def goal_env(lifecycle, monkeypatch, request):
    from app.credits import policy
    from app.chat import execute, confirmations, store
    from app.db.models import ChatConversation, ChatMessage, ContentOAuthState, GtmContact, GtmTouch, Venture, VentureDocument
    from app.db.supymem_models import Experiment
    from app.tasks import goals, planner
    configure_local_test_model()
    original = policy.get_policy
    flags = {"goals_v1": True}
    monkeypatch.setattr(policy, "get_policy", lambda key, default=None: flags if key == "feature_flags" else original(key, default))
    for module in (execute, confirmations, store):
        monkeypatch.setattr(module, "db", lambda: lifecycle["factory"])
    engine = lifecycle["factory"].kw["bind"]
    for model in (ChatConversation, ChatMessage, ContentOAuthState, GtmContact, GtmTouch, VentureDocument, Experiment):
        model.__table__.create(engine, checkfirst=True)
    with lifecycle["factory"]() as session:
        venture = session.get(Venture, "ven_lifecycle")
        venture.name, venture.raw_idea = request.param, "Help " + request.param.lower()
        session.commit()
    scope = SimpleNamespace(id="conv_goals", visibility="private", workspace_id="ws_lifecycle", actor_id="usr_lifecycle")
    monkeypatch.setattr(confirmations, "resolve", lambda *args: scope)
    def save(venture_id, **kwargs):
        with lifecycle["factory"]() as session:
            row = ChatMessage(id="message_" + kwargs["idempotency_key"], workspace_id="ws_lifecycle",
                venture_id=venture_id, conversation_id=scope.id, role="assistant", kind=kwargs["kind"],
                body_md=kwargs["body_md"], actions_json=json.dumps(kwargs["actions"]),
                created_at=lifecycle["clock"][0].replace(tzinfo=None))
            session.add(row)
            session.commit()
            return {"id": row.id, "actions": kwargs["actions"]}
    monkeypatch.setattr(store, "append_message", save)
    return {**lifecycle, "goals": goals, "planner": planner, "execute_action": execute,
        "confirmations": confirmations, "flags": flags, "domain": request.param}


def disable_goals(env):
    from app.core.local_mode import is_local
    from app.core.local_secrets import update_settings

    env["flags"].clear()
    if is_local():
        update_settings("capabilities", {"background_work": False}, {})


def contract(env, steps=None, **changes):
    from app.tasks.contracts import Contract
    return Contract(title="Your goal", goal="Prepare customer conversations for " + env["domain"], steps=steps or [
        {"title": "Read your booking link", "skill": "tasks.booking_link", "subgoal": "Read your saved booking link."}], **changes)


def create(env, selected=None):
    from app.db.task_models import TaskRun, TaskStep
    with env["factory"]() as session:
        task = env["service"].create(session, workspace_id="ws_lifecycle", venture_id="ven_lifecycle",
            owner_person_id="usr_lifecycle", contract=selected or contract(env))
        run = session.scalar(select(TaskRun).where(TaskRun.task_id == task.id))
        steps = session.scalars(select(TaskStep).where(TaskStep.run_id == run.id).order_by(TaskStep.position)).all()
        identifiers = task.id, run.id, [step.id for step in steps]
        session.commit()
        return identifiers


def search_lead(env, email):
    return {"name": env["domain"], "source_url": "https://example.org/contact", "judging_status": "judged",
        "routes": [{"kind": "email", "email": email, "source_url": "https://example.org/contact", "name": "Contact", "quote": email}]}


def test_local_goal_execution_uses_background_work_not_routine_or_policy_gate(goal_env, monkeypatch):
    from app.core import local_capabilities
    from app.db.task_models import Task

    env = goal_env
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    env["flags"].clear()
    ready = [True]
    monkeypatch.setattr(local_capabilities, "enabled", lambda name: name == "background_work" and ready[0])
    message = "In Settings > Capabilities, switch on Background work, then run your saved task again."
    monkeypatch.setattr(local_capabilities, "blocked_message", lambda name: message)
    assert env["goals"].enabled()
    with env["factory"]() as session:
        task = env["service"].create(session, workspace_id="ws_lifecycle", venture_id="ven_lifecycle",
            owner_person_id="usr_lifecycle", contract=contract(env), start=False)
        identifier = task.id
        session.commit()
        ready[0] = False
        assert env["service"].start_run(session, session.get(Task, identifier), "local-start") is None
        assert json.loads(task.blocker_json)["reason"] == message
        assert json.loads(task.blocker_json)["unblock_action"] == message
        ready[0] = True
        run = env["service"].start_run(session, task, "local-retry")
        assert run.state == "submitted" and task.state == "working"


def test_flag_off_keeps_whole_goal_validation_and_rejects_new_authority(goal_env):
    env = goal_env
    disable_goals(env)
    selected = contract(env, steps=[{"title": "Draft", "skill": "research.deep"}])
    assert "subgoal" not in selected.model_dump()["steps"][0]
    assert "output_bindings" not in selected.model_dump()["steps"][0]
    assert env["planner"].validate_contract(selected, goal=selected.goal).steps[0].skill == "task.run"
    with pytest.raises(ValueError):
        env["planner"].validate_contract(contract(env), goal="Draft a note")
    env["flags"]["goals_v1"] = 1
    assert not env["goals"].enabled()


def test_generic_steps_keep_distinct_subgoals_without_whole_goal_collapse(goal_env):
    selected = contract(goal_env, steps=[
        {"title": "Audience research", "skill": "research.deep", "subgoal": "Research complaints from " + goal_env["domain"]},
        {"title": "Invitation", "skill": "task.run", "subgoal": "Draft a conversation invitation for " + goal_env["domain"]}])
    normalized = goal_env["planner"].validate_contract(selected, goal=selected.goal)
    assert [step.skill for step in normalized.steps] == ["task.run", "task.run"]
    assert len({step.subgoal for step in normalized.steps}) == 2


@pytest.mark.parametrize("bad", ["future", "duplicate", "authority", "wrong_type", "empty_subgoal", "unsupported_effect", "thirteen", "four_replans"])
def test_contract_rejects_unknown_bindings_authority_and_hard_cap_overrides(goal_env, bad):
    from app.tasks.contracts import Budget
    steps = [{"title": "Scan", "skill": "experiment.prepare", "subgoal": "Prepare a public scan.",
              "inputs": {"experiment_id": "scan_fixture"}, "output_bindings": {"experiment_id": "scan"}},
             {"title": "Run", "skill": "experiment.run", "subgoal": "Run that public scan.", "inputs": {
                 "experiment_id": {"binding": "scan"}, "fingerprint": "digest", "ceiling_micro": 0}}]
    if bad == "future":
        steps[0]["inputs"]["experiment_id"] = {"binding": "scan"}
    elif bad == "duplicate":
        steps[1]["output_bindings"] = {"experiment_id": "scan"}
    elif bad == "authority":
        steps[1]["inputs"]["approved"] = True
    elif bad == "wrong_type":
        steps[1]["inputs"]["ceiling_micro"] = True
    elif bad == "empty_subgoal":
        steps[0]["subgoal"] = " "
    elif bad == "unsupported_effect":
        steps[1] = {"title": "Publish", "skill": "pagebuilder.publish", "subgoal": "Publish your website."}
    with pytest.raises(ValueError):
        if bad == "four_replans":
            Budget(max_strategies=4)
        else:
            selected = contract(goal_env, steps=steps * 7 if bad == "thirteen" else steps)
            goal_env["planner"].validate_contract(selected, goal=selected.goal)


def test_verified_binding_requires_owned_earlier_receipt_not_saved_completion_flag(goal_env, monkeypatch):
    from app.db.task_models import Task, TaskRun, TaskStep
    from app.experiments import outreach_tests
    env = goal_env
    link = ["https://example.invalid/book"]
    monkeypatch.setattr(outreach_tests, "booking_link", lambda *args, **kwargs: link[0])
    selected = contract(env, steps=[
        {"title": "Read", "skill": "tasks.booking_link", "subgoal": "Read your booking link.", "output_bindings": {"booking_link": "link"}},
        {"title": "Find", "skill": "gtm.find", "subgoal": "Find your audience.", "inputs": {"query": {"binding": "link"}}}])
    task_id, run_id, step_ids = create(env, selected)
    with env["factory"]() as session:
        task, run = session.get(Task, task_id), session.get(TaskRun, run_id)
        first, second = [session.get(TaskStep, identifier) for identifier in step_ids]
        first.state = "completed"
        first.evidence_json = json.dumps([{"verified": True, "outputs": {"booking_link": link[0]}}])
        with pytest.raises(ValueError):
            env["goals"].resolve_inputs(session, task, run, second)
        env["service"].event(session, task, "goal_receipt", "Saved", run_id=run_id, step_id=first.id, data={"booking_link": link[0]})
        session.flush()
        assert env["goals"].resolve_inputs(session, task, run, second) == {"query": link[0]}
        link[0] = "https://example.invalid/changed"
        with pytest.raises(ValueError):
            env["goals"].resolve_inputs(session, task, run, second)
        first.run_id = "foreign_run"
        session.flush()
        with pytest.raises(ValueError):
            env["goals"].resolve_inputs(session, task, run, second)


def test_serial_receipt_finishes_only_one_step_and_bookings_do_not_count_drafts(goal_env, monkeypatch):
    from app.db.task_models import TaskRun, TaskStep
    from app.experiments import outreach_tests
    env = goal_env
    monkeypatch.setattr(outreach_tests, "booking_link", lambda *args, **kwargs: "https://example.invalid/book")
    selected = contract(env, steps=[
        {"title": "Read", "skill": "tasks.booking_link", "subgoal": "Read your booking link."},
        {"title": "Verify calls", "skill": "tasks.customer_calls", "subgoal": "Verify two confirmed calls.", "inputs": {"target_count": 2}}])
    _, run_id, step_ids = create(env, selected)
    payload = {"task_run_id": run_id, "workspace_id": "ws_lifecycle", "venture_id": "ven_lifecycle"}
    asyncio.run(env["execution"].execute(payload))
    with env["factory"]() as session:
        assert [session.get(TaskStep, identifier).state for identifier in step_ids] == ["completed", "submitted"]
    asyncio.run(env["execution"].execute(payload))
    with env["factory"]() as session:
        assert session.get(TaskStep, step_ids[1]).state == "verifying"
        assert session.get(TaskRun, run_id).state != "completed"


@pytest.mark.parametrize("limit", ["credit", "credit_plus_plan", "deadline", "stop", "repeat"])
def test_goal_limits_cannot_be_extended_or_replayed(goal_env, limit):
    from app.db.task_models import Task, TaskRun, TaskStep
    env = goal_env
    task_id, run_id, step_ids = create(env)
    with env["factory"]() as session:
        task, run, step = session.get(Task, task_id), session.get(TaskRun, run_id), session.get(TaskStep, step_ids[0])
        if limit == "credit":
            run.cost_micro = 500_001
        elif limit == "credit_plus_plan":
            run.cost_micro = 495_001
            task.scope_json = json.dumps({**json.loads(task.scope_json), "_planning": {"billing": {"usage": {"actual_micro": 5000}}}})
        elif limit == "deadline":
            run.deadline_at = env["clock"][0].replace(tzinfo=None) - timedelta(seconds=1)
        elif limit == "stop":
            env["service"].cancel(session, task)
        else:
            env["goals"].record_attempt(session, task, run, step, "tasks.booking_link", {})
            session.flush()
            with pytest.raises(ValueError, match="same inputs"):
                env["goals"].record_attempt(session, task, run, step, "tasks.booking_link", {})
            return
        assert not env["service"].guard(session, task, run)


@pytest.mark.parametrize("tamper", [None, "card", "payload", "actor", "stop", "off", "unaccepted"])
def test_exact_approval_restores_original_context_attaches_job_and_wakes_only_same_run(goal_env, monkeypatch, tamper):
    from app.db.models import ChatMessage, GtmContact, Job, VentureDocument
    from app.db.task_models import Task, TaskRun, TaskStep
    from app.tasks import usage
    env, calls = goal_env, []
    selected = contract(env, steps=[{"title": "Find", "skill": "gtm.find", "subgoal": "Find " + env["domain"], "inputs": {"query": env["domain"]}}])
    task_id, run_id, step_ids = create(env, selected)
    step_id = step_ids[0]
    card = {"action": "gtm.find", "label": "Start search", "confirmation": {"action": "gtm.find", "resulting_state": {"retry": False}}}
    asyncio.run(env["goals"].park(step_id, {"body_md": "Search your saved audience without sending anything."}, card))
    with env["factory"]() as session:
        step = session.get(TaskStep, step_id)
        approval = json.loads(step.inputs_json)["_goal_approval"]
        payload = approval["payload"]
        assert session.get(TaskRun, run_id).state == "approval_required" and step.job_id is None
    if tamper != "unaccepted":
        env["confirmations"].mark("ven_lifecycle", approval["message_id"], "gtm.find", "starting")
    if tamper == "card":
        with env["factory"]() as session:
            row = session.get(ChatMessage, approval["message_id"])
            actions = json.loads(row.actions_json)
            actions[0]["confirmation"]["resulting_state"]["retry"] = True
            row.actions_json = json.dumps(actions)
            session.commit()
    elif tamper == "payload":
        payload = {**payload, "approved": True}
    elif tamper == "stop":
        with env["factory"]() as session:
            env["service"].cancel(session, session.get(Task, task_id))
            session.commit()
    elif tamper == "off":
        disable_goals(env)
    async def admitted(venture_id, action, **kwargs):
        calls.append((env["service"]._RUN.get(), usage.context(), kwargs["payload"]))
        with env["factory"]() as session:
            jid = env["jobs"].enqueue(session, "gtm.journey.prepare", {"journey_id": "journey_goal", "prepare_id": "prepare_goal"},
                workspace_id="ws_lifecycle", venture_id=venture_id, dedupe_key="goal_search_fixture")
            session.commit()
        return {"run_id": jid, "action_started": True, "body_md": "Your search is running."}
    monkeypatch.setattr(env["execute_action"], "_execute_action", admitted)
    call = env["execute_action"]._execute_confirmed_action("ven_lifecycle", "gtm.find", payload,
        "usr_other" if tamper == "actor" else "usr_lifecycle")
    if tamper:
        from app.core.local_mode import is_local
        if tamper == "off" and is_local():
            from app.core.local_capabilities import blocked_message
            reply = asyncio.run(call)
            assert reply["kind"] == "text" and reply["body_md"] == blocked_message("background_work")
            with env["factory"]() as session:
                step = session.get(TaskStep, step_id)
                assert step.job_id is None and step.state == "approval_required"
                assert session.get(TaskRun, run_id).state == "approval_required"
        else:
            with pytest.raises((ValueError, PermissionError)):
                asyncio.run(call)
        assert not calls
        return
    reply = asyncio.run(call)
    assert calls == [((run_id, step_id), (run_id, step_id), {"retry": False})]
    with env["factory"]() as session:
        step = session.get(TaskStep, step_id)
        assert step.job_id == reply["run_id"] and step.state == "verifying"
        job = session.get(Job, step.job_id)
        job.status = "succeeded"
        now = env["clock"][0].replace(tzinfo=None)
        session.add(VentureDocument(id="journey_goal", venture_id="ven_lifecycle", kind="gtm_journey", title="Your audience",
            body_md=env["domain"], provenance_json=json.dumps({"state": "ready", "job_id": job.id, "run_id": "prepare_goal",
                "reachable_leads": [search_lead(env, "found@example.org")]}), created_at=now, updated_at=now))
        session.add(GtmContact(id="contact_goal", workspace_id="ws_lifecycle", email="found@example.org",
            source="venture:ven_lifecycle:search", created_at=now, stage_changed_at=now))
        session.flush()
        env["service"].after_job(session, job.id)
        assert step.state == "completed" and session.get(TaskRun, run_id).state == "completed"
        session.commit()
    with pytest.raises(PermissionError):
        asyncio.run(env["execute_action"]._execute_confirmed_action("ven_lifecycle", "gtm.find", payload, "usr_lifecycle"))


def test_first_template_is_bounded_and_cannot_certify_calls_from_preparation(goal_env):
    env = goal_env
    template = env["goals"].customer_calls_template("book 3 customer calls this week", timezone="UTC", now=env["clock"][0])
    normalized = env["planner"].validate_contract(template, goal=template.goal)
    assert len(normalized.steps) == 4 and normalized.steps[-1].inputs == {"target_count": 3}
    assert normalized.steps[-1].skill == "tasks.customer_calls"
    assert not any("send" in step.skill or "book" == step.skill for step in normalized.steps)
    assert normalized.budget.max_strategies <= 3 and normalized.budget.cost_micro <= 500_000
    assert normalized.budget.wall_seconds == 86400


def test_preview_uses_typed_schema_and_checkpoints_each_literal_subgoal(goal_env, monkeypatch):
    from app.credits import metered
    env, calls, saved = goal_env, [], []
    steps = [{"title": "Complaints", "skill": "task.run", "subgoal": "Research complaints from " + env["domain"]},
             {"title": "Invitation", "skill": "task.run", "subgoal": "Draft an invitation for " + env["domain"]}]
    answer = contract(env, steps=steps).model_dump()
    async def llm(messages, **kwargs):
        calls.append((messages, kwargs))
        response = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(answer)))])
        kwargs["before_meter_exit"](response, {"receipt": "fixture"})
        return response
    monkeypatch.setattr(metered, "llm", llm)
    normalized = asyncio.run(env["planner"].preview(answer["goal"], timezone="UTC", now=env["clock"][0],
        workspace_id="ws_lifecycle", venture_id="ven_lifecycle", before_meter_exit=lambda selected, receipt: saved.append((selected, receipt))))
    assert len(calls) == len(saved) == 1
    assert [step.subgoal for step in normalized.steps] == [step["subgoal"] for step in steps]
    schema = calls[0][1]["response_schema"]["$defs"]["StepContract"]
    assert "subgoal" in schema["required"] and "anyOf" in schema["properties"]["inputs"]
    assert "pagebuilder.publish" not in schema["properties"]["skill"]["enum"]


def test_public_scan_subset_rejects_contact_runners_and_checks_owned_receipts(goal_env, monkeypatch):
    from app.experiments import autonomous
    env = goal_env
    specs = [{"id": "scan_fixture", "runner": "scan", "scan": "demand", "fingerprint": "current", "ceiling_micro": 5000},
             {"id": "outreach_fixture", "runner": "outreach_split", "scan": "demand"}]
    monkeypatch.setattr(autonomous, "available", lambda venture_id: specs if venture_id == "ven_lifecycle" else [])
    assert env["goals"].scan_spec("ven_lifecycle", "scan_fixture")["runner"] == "scan"
    with pytest.raises(ValueError):
        env["goals"].scan_spec("ven_lifecycle", "outreach_fixture")
    with pytest.raises(ValueError):
        env["goals"].scan_spec("foreign_venture", "scan_fixture")


def test_booking_receipts_count_distinct_real_contacts_not_drafts_foreign_or_simulated_replies(goal_env):
    from app.db.models import GtmContact, GtmTouch
    from app.db.task_models import Task, TaskRun, TaskStep
    env = goal_env
    selected = contract(env, steps=[{"title": "Verify", "skill": "tasks.customer_calls", "subgoal": "Verify two calls.", "inputs": {"target_count": 2}}])
    task_id, run_id, step_ids = create(env, selected)
    with env["factory"]() as session:
        now = env["clock"][0].replace(tzinfo=None)
        for index in range(4):
            session.add(GtmContact(id=f"booking_contact_{index}", workspace_id="ws_lifecycle", source="venture:ven_lifecycle:fixture", created_at=now, stage_changed_at=now))
            session.add(GtmTouch(id=f"booking_sent_{index}", workspace_id="ws_lifecycle", venture_id="ven_lifecycle",
                contact_id=f"booking_contact_{index}", direction="out", simulated=0, body_text="An approved invitation.", sent_at=now - timedelta(hours=1), created_at=now - timedelta(hours=1)))
        for index, contact, venture, simulated in [(0, 0, "ven_lifecycle", 0), (1, 0, "ven_lifecycle", 0),
                (2, 1, "foreign_venture", 0), (3, 2, "ven_lifecycle", 1), (4, 3, "ven_lifecycle", 0)]:
            session.add(GtmTouch(id=f"booking_reply_{index}", workspace_id="ws_lifecycle", venture_id=venture,
                contact_id=f"booking_contact_{contact}", direction="in", simulated=simulated,
                classification="interested", body_text="I booked a call.", created_at=now))
        session.flush()
        task, step = session.get(Task, task_id), session.get(TaskStep, step_ids[0])
        receipt = env["goals"].customer_calls_receipt(session, task, step)
        assert receipt["verified"] and receipt["outputs"] == {"booking_count": 2}
        env["goals"].finish(session, task, session.get(TaskRun, run_id), step, receipt)
        assert session.get(TaskRun, run_id).state == "completed"


def test_conditional_hypothetical_and_negated_replies_cannot_complete_a_booking_goal(goal_env):
    from app.db.models import GtmContact, GtmTouch
    from app.db.task_models import Task, TaskRun, TaskStep
    env = goal_env
    task_id, run_id, step_ids = create(env, contract(env, steps=[
        {"title": "Verify", "skill": "tasks.customer_calls", "subgoal": "Verify three calls.", "inputs": {"target_count": 3}}]))
    with env["factory"]() as session:
        now = env["clock"][0].replace(tzinfo=None)
        replies = []
        for index in range(3):
            contact_id = f"conditional_contact_{index}"
            session.add(GtmContact(id=contact_id, workspace_id="ws_lifecycle", source="venture:ven_lifecycle:fixture",
                created_at=now, stage_changed_at=now))
            session.add(GtmTouch(id=f"conditional_sent_{index}", workspace_id="ws_lifecycle", venture_id="ven_lifecycle",
                contact_id=contact_id, direction="out", simulated=0, body_text="An approved invitation.",
                sent_at=now - timedelta(hours=1), created_at=now - timedelta(hours=1)))
            reply = GtmTouch(id=f"conditional_reply_{index}", workspace_id="ws_lifecycle", venture_id="ven_lifecycle",
                contact_id=contact_id, direction="in", simulated=0, classification="interested", created_at=now)
            session.add(reply)
            replies.append(reply)
        task, run, step = session.get(Task, task_id), session.get(TaskRun, run_id), session.get(TaskStep, step_ids[0])
        for body in ("If I booked a call, a reminder would be useful.", "Imagine I booked a call. That would save time.",
                "I have not booked a call."):
            for reply in replies:
                reply.body_text = body
            session.flush()
            receipt = env["goals"].customer_calls_receipt(session, task, step)
            assert not receipt["verified"] and receipt["outputs"] == {"booking_count": 0}
            env["goals"].finish(session, task, run, step, receipt)
            assert task.state != "completed" and run.state != "completed" and step.state == "verifying"
        for reply in replies:
            reply.body_text = "I booked a call.\n\n> If I booked a call, a reminder would be useful."
        session.flush()
        receipt = env["goals"].customer_calls_receipt(session, task, step)
        assert receipt["verified"] and receipt["outputs"] == {"booking_count": 3}
        env["goals"].finish(session, task, run, step, receipt)
        assert task.state == run.state == step.state == "completed"


@pytest.mark.parametrize("domain", ["Industrial pump suppliers", "Home baking enthusiasts"])
def test_customer_call_goal_starts_only_preparation_then_waits_for_step_reviews(chat_db, monkeypatch, domain):
    configure_local_test_model()
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    from app.credits import policy
    from app.db.models import Venture
    from app.db.task_models import Task, TaskRun, TaskStep
    original = policy.get_policy
    monkeypatch.setattr(policy, "get_policy", lambda key, default=None: {"goals_v1": True} if key == "feature_flags" else original(key, default))
    with chat_db["factory"]() as session:
        venture = session.get(Venture, "ven_chat")
        venture.name, venture.raw_idea = domain, "Help " + domain.lower()
        session.commit()
    text = "book 3 customer calls this week"
    chat_source(chat_db, text)
    reply = chat_call(chat_db, text)
    with chat_db["factory"]() as session:
        task = session.scalar(select(Task))
        run = session.scalar(select(TaskRun).where(TaskRun.task_id == task.id))
        steps = session.scalars(select(TaskStep).where(TaskStep.run_id == run.id).order_by(TaskStep.position)).all()
        assert task.state == "working" and run.state == "submitted"
        assert [step.skill for step in steps] == ["tasks.booking_link", "gtm.find", "outreach.draft", "tasks.customer_calls"]
        assert not any(step.job_id for step in steps)
    assert "Nothing will be sent" in reply["body_md"] and len(chat_db["calls"]) == 1


def test_goal_binding_cannot_borrow_a_newer_unrelated_search_receipt(goal_env):
    from app.db.models import GtmContact, Job, VentureDocument
    from app.db.task_models import TaskStep
    env = goal_env
    _, run_id, step_ids = create(env, contract(env, steps=[
        {"title": "Find", "skill": "gtm.find", "subgoal": "Find " + env["domain"]}]))
    with env["service"].step_context(run_id, step_ids[0]), env["factory"]() as session:
        job_id = env["jobs"].enqueue(session, "gtm.journey.prepare", {"journey_id": "owned_journey", "prepare_id": "owned_prepare"},
            workspace_id="ws_lifecycle", venture_id="ven_lifecycle", dedupe_key="owned_search_fixture")
        job = session.get(Job, job_id)
        job.status = "succeeded"
        now = env["clock"][0].replace(tzinfo=None)
        doc = VentureDocument(id="owned_journey", venture_id="ven_lifecycle", kind="gtm_journey", title="Your audience",
            body_md=env["domain"], provenance_json=json.dumps({"state": "ready", "job_id": job_id, "run_id": "owned_prepare",
                "reachable_leads": [search_lead(env, "found@example.org")]}), created_at=now, updated_at=now)
        session.add(doc)
        session.add(GtmContact(id="owned_contact", workspace_id="ws_lifecycle", email="found@example.org",
            source="venture:ven_lifecycle:search", stage_changed_at=now, created_at=now))
        session.flush()
        step = session.get(TaskStep, step_ids[0])
        payload = {**json.loads(step.inputs_json), "_job_id": job_id}
        assert env["goals"].verify_job(session, "gtm.find", payload, workspace_id="ws_lifecycle", venture_id="ven_lifecycle")["verified"]
        doc.provenance_json = json.dumps({"state": "ready", "job_id": "newer_unrelated_search", "run_id": "newer_prepare"})
        session.flush()
        assert not env["goals"].verify_job(session, "gtm.find", payload, workspace_id="ws_lifecycle", venture_id="ven_lifecycle")["verified"]


def test_experiment_receipt_requires_its_own_finished_zero_contact_scan(goal_env):
    from app.db.models import Job
    from app.db.supymem_models import Experiment
    from app.experiments import ledger
    env = goal_env
    _, run_id, step_ids = create(env, contract(env, steps=[{"title": "Scan", "skill": "experiment.run", "subgoal": "Scan public complaints for " + env["domain"],
        "inputs": {"experiment_id": "receipt_scan", "fingerprint": "fixture", "ceiling_micro": 0}}]))
    with env["service"].step_context(run_id, step_ids[0]), env["factory"]() as session:
        job_id = env["jobs"].enqueue(session, "experiment.run", {"experiment_id": "receipt_scan"},
            workspace_id="ws_lifecycle", venture_id="ven_lifecycle", dedupe_key="receipt_scan_fixture")
        session.get(Job, job_id).status = "succeeded"
        context = {"job_id": job_id, "spec": {"runner": "scan", "scan": "demand"}, "result": {"insight": env["domain"]}}
        row = Experiment(id="receipt_scan", workspace_id="ws_lifecycle", venture_id="ven_lifecycle", kind=ledger.KIND,
            state="running", hypothesis=env["domain"], context=json.dumps(context))
        session.add(row)
        session.flush()
        verify = lambda: env["goals"].verify_job(session, "experiment.run", {"_job_id": job_id}, workspace_id="ws_lifecycle", venture_id="ven_lifecycle")
        assert not verify()["verified"]
        row.state, row.ended_at = "concluded", env["clock"][0].replace(tzinfo=None)
        session.flush()
        assert verify()["verified"]
        context["spec"]["runner"] = "outreach_split"
        row.context = json.dumps(context)
        session.flush()
        assert not verify()["verified"]


def test_goal_worker_retry_stops_before_repeating_the_same_action(goal_env):
    from app.db.task_models import Task, TaskStep
    env = goal_env
    task_id, run_id, step_ids = create(env, contract(env, steps=[{"title": "Find", "skill": "gtm.find", "subgoal": "Find " + env["domain"]}]))
    with env["service"].step_context(run_id, step_ids[0]), env["factory"]() as session:
        job_id = env["jobs"].enqueue(session, "gtm.journey.prepare", {"journey_id": "retry_journey", "prepare_id": "retry_prepare"},
            workspace_id="ws_lifecycle", venture_id="ven_lifecycle", dedupe_key="goal_retry_fixture")
        session.commit()
        assert env["service"].before_job(session, job_id, 1)
        assert not env["service"].before_job(session, job_id, 2)
        assert session.get(Task, task_id).state == "input_required"
        assert session.get(TaskStep, step_ids[0]).attempt == 1


def test_finished_goal_cannot_start_another_twelve_step_run(goal_env):
    from app.db.task_models import Task, TaskRun
    env = goal_env
    task_id, run_id, _ = create(env)
    with env["factory"]() as session:
        task, run = session.get(Task, task_id), session.get(TaskRun, run_id)
        task.state = run.state = "completed"
        session.flush()
        assert env["service"].start_run(session, task, "again") is None
        assert session.scalars(select(TaskRun).where(TaskRun.task_id == task_id)).all() == [run]


def test_delayed_goal_start_cannot_move_this_week_deadline_to_a_new_week(goal_env):
    env = goal_env
    selected = env["goals"].customer_calls_template("book 3 customer calls this week", timezone="UTC", now=env["clock"][0])
    with env["factory"]() as session:
        task = env["service"].create(session, workspace_id="ws_lifecycle", venture_id="ven_lifecycle",
            owner_person_id="usr_lifecycle", contract=selected, start=False)
        deadline = task.deadline_at
        session.commit()
        env["clock"][0] += timedelta(days=7)
        run = env["service"].start_run(session, task, "late")
        assert run.deadline_at == deadline and not env["service"].guard(session, task, run)


def test_goal_customer_search_cannot_enter_linkedin_automation(goal_env, monkeypatch):
    env, calls = goal_env, []
    selected = contract(env, steps=[{"title": "Find", "skill": "gtm.find", "subgoal": "Find " + env["domain"], "inputs": {"query": "Find LinkedIn profiles"}}])
    task_id, run_id, step_ids = create(env, selected)
    async def forbidden(*args, **kwargs):
        calls.append(args)
        raise AssertionError("No LinkedIn workflow may run for a goal.")
    monkeypatch.setattr(env["execute_action"], "find_customers_reply", forbidden)
    with pytest.raises(ValueError, match="LinkedIn"):
        asyncio.run(env["goals"].dispatch(task_id, run_id, step_ids[0], {"query": "Find LinkedIn profiles"}))
    assert not calls


def test_verified_output_binding_cannot_expose_credentials_to_a_search(goal_env, monkeypatch):
    from app.db.task_models import Task, TaskRun, TaskStep
    from app.experiments import outreach_tests
    env = goal_env
    link = "https://example.invalid/book?token=ghp_" + "A" * 36
    monkeypatch.setattr(outreach_tests, "booking_link", lambda *args, **kwargs: link)
    selected = contract(env, steps=[
        {"title": "Read", "skill": "tasks.booking_link", "subgoal": "Read your booking link.", "output_bindings": {"booking_link": "link"}},
        {"title": "Find", "skill": "gtm.find", "subgoal": "Find " + env["domain"], "inputs": {"query": {"binding": "link"}}}])
    task_id, run_id, step_ids = create(env, selected)
    with env["factory"]() as session:
        task, run = session.get(Task, task_id), session.get(TaskRun, run_id)
        first, second = [session.get(TaskStep, identifier) for identifier in step_ids]
        first.state = "completed"
        env["service"].event(session, task, "goal_receipt", "Saved", run_id=run_id, step_id=first.id, data={"booking_link": link})
        session.flush()
        with pytest.raises(ValueError, match="credentials"):
            env["goals"].resolve_inputs(session, task, run, second)


def test_turning_goals_off_keeps_queued_scan_custody_and_blocks_new_provider_work(goal_env):
    from app.db.task_models import TaskStep
    from app.tasks import usage
    env = goal_env
    _, run_id, step_ids = create(env, contract(env, steps=[{"title": "Scan", "skill": "experiment.run", "subgoal": "Scan public complaints for " + env["domain"],
        "inputs": {"experiment_id": "off_scan", "fingerprint": "fixture", "ceiling_micro": 0}}]))
    disable_goals(env)
    with env["service"].step_context(run_id, step_ids[0]), env["factory"]() as session:
        job_id = env["jobs"].enqueue(session, "experiment.run", {"experiment_id": "off_scan"},
            workspace_id="ws_lifecycle", venture_id="ven_lifecycle", dedupe_key="off_scan_fixture")
        session.commit()
        assert session.get(TaskStep, step_ids[0]).job_id == job_id
        assert not env["service"].before_job(session, job_id, 1)
        session.commit()
    with usage.job_context(job_id, kind="experiment.run"):
        assert usage.context() == (run_id, step_ids[0])


def test_canonical_skills_also_stop_same_action_args_under_different_subgoals(goal_env, monkeypatch):
    from app.research import service as research
    from app.db.task_models import Task, TaskStep
    env, calls = goal_env, []
    selected = contract(env, steps=[
        {"title": "Research", "skill": "research.deep", "subgoal": "Research the current venture"},
        {"title": "Review", "skill": "research.deep", "subgoal": "Review the current venture"}])
    task_id, run_id, step_ids = create(env, selected)
    def admitted(venture_id, **kwargs):
        calls.append(venture_id)
        with env["factory"]() as session:
            job_id = env["jobs"].enqueue(session, "research.deep", {"session_id": "repeat_session", "revision_id": "repeat_revision"},
                workspace_id="ws_lifecycle", venture_id=venture_id, dedupe_key="canonical_repeat_fixture")
            session.commit()
        return {"job_id": job_id}
    monkeypatch.setattr(research, "start", admitted)
    payload = {"task_run_id": run_id, "workspace_id": "ws_lifecycle", "venture_id": "ven_lifecycle"}
    asyncio.run(env["execution"].execute(payload))
    with env["factory"]() as session:
        first = session.get(TaskStep, step_ids[0])
        assert first.job_id and calls == ["ven_lifecycle"]
        first.state = "completed"
        session.commit()
    asyncio.run(env["execution"].execute(payload))
    with env["factory"]() as session:
        assert session.get(Task, task_id).state == "input_required" and len(calls) == 1


def test_legacy_contracts_and_receipts_do_not_require_a_new_flag_policy_read(goal_env, monkeypatch):
    from app.credits import policy
    from app.tasks import reasoning, skills
    env = goal_env
    # The independent reasoning budget gate must not mask a goals_v1 policy read.
    monkeypatch.setattr(reasoning, "enabled", lambda: False)
    original = policy.get_policy
    def forbidden(key, default=None):
        if key == "feature_flags":
            raise AssertionError("Legacy tasks must not need the new flag policy.")
        return original(key, default)
    monkeypatch.setattr(policy, "get_policy", forbidden)
    selected = contract(env, steps=[{"title": "Draft", "skill": "task.run"}])
    assert selected.budget.cost_micro == 500_000
    with env["factory"]() as session:
        task = env["service"].create(session, workspace_id="ws_lifecycle", venture_id="ven_lifecycle",
            owner_person_id="usr_lifecycle", contract=selected, start=False)
        assert task.goal == selected.goal
        assert skills.get_skill("unknown.skill") is None
        receipt = skills.verify_result(session, "task.run", {"_job_id": "missing_job"}, workspace_id="ws_lifecycle", venture_id="ven_lifecycle")
        assert not receipt["verified"]


def test_missing_search_output_parks_without_losing_the_verified_job_receipt(goal_env):
    from app.db.models import VentureDocument
    from app.db.task_models import Task, TaskStep
    env = goal_env
    task_id, run_id, step_ids = create(env, contract(env, steps=[
        {"title": "Find", "skill": "gtm.find", "subgoal": "Find " + env["domain"], "output_bindings": {"contact_id": "customer_contact"}},
        {"title": "Draft", "skill": "outreach.draft", "subgoal": "Prepare your invitation.", "inputs": {"contact_id": {"binding": "customer_contact"}}}]))
    with env["service"].step_context(run_id, step_ids[0]), env["factory"]() as session:
        job_id = env["jobs"].enqueue(session, "gtm.journey.prepare", {"journey_id": "channel_journey", "prepare_id": "channel_prepare"},
            workspace_id="ws_lifecycle", venture_id="ven_lifecycle", dedupe_key="channel_search_fixture")
        job = session.get(env["jobs"].Job, job_id)
        job.status = "succeeded"
        now = env["clock"][0].replace(tzinfo=None)
        session.add(VentureDocument(id="channel_journey", venture_id="ven_lifecycle", kind="gtm_journey", title="Your audience",
            body_md=env["domain"], provenance_json=json.dumps({"state": "ready", "job_id": job_id, "run_id": "channel_prepare",
                "channels": [{"name": env["domain"]}]}), created_at=now, updated_at=now))
        session.commit()
        env["service"].after_job(session, job_id)
        step = session.get(TaskStep, step_ids[0])
        assert session.get(Task, task_id).state == step.state == "input_required"
        assert step.job_id == job_id and json.loads(step.evidence_json)
        assert session.get(TaskStep, step_ids[1]).job_id is None
        session.commit()


@pytest.mark.parametrize("case", ["returned", "empty", "missing_record", "foreign_workspace", "foreign_venture"])
def test_search_contact_binding_uses_only_its_owned_receipt_and_stops_without_one(goal_env, case):
    from app.db.models import GtmContact, Job, VentureDocument
    from app.db.task_models import Task, TaskStep
    env = goal_env
    task_id, run_id, step_ids = create(env, contract(env, steps=[
        {"title": "Find", "skill": "gtm.find", "subgoal": "Find " + env["domain"], "output_bindings": {"contact_id": "customer_contact"}},
        {"title": "Draft", "skill": "outreach.draft", "subgoal": "Prepare your invitation.", "inputs": {"contact_id": {"binding": "customer_contact"}}}]))
    with env["service"].step_context(run_id, step_ids[0]), env["factory"]() as session:
        job_id = env["jobs"].enqueue(session, "gtm.journey.prepare", {"journey_id": "receipt_journey", "prepare_id": "receipt_prepare"},
            workspace_id="ws_lifecycle", venture_id="ven_lifecycle", dedupe_key="receipt_search_fixture")
        session.get(Job, job_id).status = "succeeded"
        now = env["clock"][0].replace(tzinfo=None)
        session.add_all([GtmContact(id="a_older_unrelated", workspace_id="ws_lifecycle", email="older@example.org",
            source="venture:ven_lifecycle:search", created_at=now - timedelta(days=3), stage_changed_at=now),
            GtmContact(id="b_concurrent_unrelated", workspace_id="ws_lifecycle", email="concurrent@example.org",
                source="venture:ven_lifecycle:search", created_at=now, stage_changed_at=now)])
        if case not in {"empty", "missing_record"}:
            session.add(GtmContact(id="z_returned_contact", workspace_id="ws_other" if case == "foreign_workspace" else "ws_lifecycle",
                email="found@example.org", source="venture:ven_other:search" if case == "foreign_venture" else "venture:ven_lifecycle:search",
                created_at=now - timedelta(days=1), stage_changed_at=now))
        session.add(VentureDocument(id="receipt_journey", venture_id="ven_lifecycle", kind="gtm_journey", title="Your audience",
            body_md=env["domain"], provenance_json=json.dumps({"state": "ready", "job_id": job_id, "run_id": "receipt_prepare",
                "reachable_leads": [] if case == "empty" else [search_lead(env, "found@example.org")]}), created_at=now, updated_at=now))
        session.commit()
        task, run, step = env["service"].job_step(session, job_id)
        receipt = env["goals"].verify_job(session, "gtm.find", {"_job_id": job_id}, workspace_id="ws_lifecycle", venture_id="ven_lifecycle")
        assert receipt["verified"] is (case == "returned")
        assert receipt["outputs"].get("contact_id") == ("z_returned_contact" if case == "returned" else None)
        assert receipt["evidence"][0]["prospect_count"] == (1 if case == "returned" else 0)
        env["service"].after_job(session, job_id)
        following = session.get(TaskStep, step_ids[1])
        if case == "returned":
            assert env["goals"].resolve_inputs(session, task, run, following) == {"contact_id": "z_returned_contact"}
        else:
            assert session.get(Task, task_id).state == step.state == "input_required"
            with pytest.raises(ValueError, match="earlier step must finish"):
                env["goals"].resolve_inputs(session, task, run, following)
        assert following.job_id is None
        session.commit()

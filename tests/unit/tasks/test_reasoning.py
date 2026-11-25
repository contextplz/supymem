"""Deep answers share one durable allowance across unrelated founders."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tests.credit_fixtures import ensure_test_wallet
from tests.unit.credits.conftest import credits_env as credits_env


@pytest.fixture(params=["industrial valve procurement", "family meal planning"])
def deep_env(credits_env, monkeypatch, request):
    from app.credits import policy
    from app.db.models import Venture
    from app.state import db
    from app.tasks import reasoning
    flags = {"reasoning_v1": True}
    original = policy.get_policy
    monkeypatch.setattr(policy, "get_policy", lambda key, default=None: flags if key == "feature_flags" else original(key, default))
    wallets = [ensure_test_wallet("ven_reasoning_business"), ensure_test_wallet("ven_reasoning_consumer")]
    domains = ["industrial valve procurement", "family meal planning"]
    with db()() as session:
        for wallet, domain in zip(wallets, domains):
            session.get(Venture, wallet.attribution_venture_id).raw_idea = domain
        session.commit()
    index = domains.index(request.param)
    wallet = wallets[index]
    return SimpleNamespace(reasoning=reasoning, flags=flags, wallet=wallet, other=wallets[1-index],
        vid=wallet.attribution_venture_id, wid="ws_wallet:" + wallet.attribution_venture_id,
        other_vid=wallets[1-index].attribution_venture_id, domain=request.param, db=db)


def prepare(env, request_id="saved-question"):
    question = "Analyze the tradeoffs in depth for " + env.domain
    messages = [{"role": "system", "content": "You are Charlie. Do not invent facts."},
        {"role": "user", "content": question}]
    return env.reasoning._prepare(env.wid, env.vid, question, messages, request_id, None)


def meter(env, request_id, cost=100_000):
    from app.credits.gate import Meter
    value = Meter(hold=None, passthrough=True, purpose="venture.chat.web_search", account_id=env.wallet.id,
        workspace_id=env.wid, venture_id=env.vid, request_id=request_id, estimate_micro=cost)
    value.record_micro(cost)
    return value


def approve(env, task_id, request_id="approval"):
    from app.tenancy import bind
    with bind(workspace_id=env.wid, venture_id=env.vid, actor="per_wallet:" + env.vid):
        return env.reasoning._approve(task_id, env.wid, env.vid, request_id)


def test_local_deep_answers_use_background_capability_and_its_setup_message(deep_env, monkeypatch):
    from app.core import local_capabilities
    from app.credits.errors import CreditsUnavailable

    env = deep_env
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    env.flags.clear()
    ready = [True]
    monkeypatch.setattr(local_capabilities, "enabled", lambda name: name == "background_work" and ready[0])
    message = "In Settings > Capabilities, switch on Background work, then ask your question again."
    monkeypatch.setattr(local_capabilities, "blocked_message", lambda name: message)
    assert env.reasoning.enabled()
    ready[0] = False
    assert not env.reasoning.enabled()
    admission = env.reasoning.Admission("unstarted", env.wid, env.vid)
    with pytest.raises(CreditsUnavailable, match="Settings > Capabilities") as error:
        admission(env.wallet.id, "local-question", 1)
    assert str(error.value) == message
    with pytest.raises(CreditsUnavailable) as error:
        env.reasoning._prepare(env.wid, env.vid, "Review " + env.domain, [], "local-question", None)
    assert str(error.value) == message
    with pytest.raises(CreditsUnavailable) as error:
        asyncio.run(env.reasoning.approve("unstarted", workspace_id=env.wid, venture_id=env.vid, request_id="local-question"))
    assert str(error.value) == message


def test_local_background_toggle_does_not_raise_general_task_spending_cap(deep_env, monkeypatch):
    from app.core import local_capabilities
    from app.tasks.contracts import Budget

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(local_capabilities, "enabled", lambda name: name == "background_work")
    assert Budget().cost_micro == Budget.model_fields["cost_micro"].default
    assert Budget(cost_micro=deep_env.reasoning.DEFAULT_COST_MICRO).cost_micro == deep_env.reasoning.DEFAULT_COST_MICRO


def test_spoken_continuation_cannot_raise_owner_allowance_or_enqueue(deep_env):
    from fastapi import HTTPException
    from app.chat import assistant
    from app.credits.errors import CreditsUnavailable
    from app.db.models import Job
    from app.db.task_models import TaskEvent
    from app.intake.evidence import INPUT_KIND
    from app.tenancy import bind
    env = deep_env
    view = prepare(env)
    admission = env.reasoning.Admission(view["run_id"], env.wid, env.vid)
    admission(env.wallet.id, "spent", 5_000_000)(meter(env, "spent", 5_000_000))
    with pytest.raises(CreditsUnavailable):
        admission(env.wallet.id, "over-cap", 1)
    text = f"Continue my deep answer [reasoning:{view['task_id']}] for about $5?"
    with env.db()() as session:
        before = list(session.scalars(select(Job.id)))
    token = INPUT_KIND.set("voice")
    try:
        with bind(workspace_id=env.wid, venture_id=env.vid, actor="per_wallet:" + env.vid):
            with pytest.raises(HTTPException) as refused:
                asyncio.run(assistant.answer_question(env.vid, text, workspace_id=env.wid, request_id="spoken"))
        assert refused.value.detail["error"]["code"] == "voice_cannot_confirm"
    finally:
        INPUT_KIND.reset(token)
    assert env.reasoning.snapshot(view["run_id"], env.wid, env.vid)["cap_micro"] == 5_000_000
    with env.db()() as session:
        assert list(session.scalars(select(Job.id))) == before
        assert not session.scalar(select(TaskEvent.id).where(TaskEvent.kind == "reasoning_approved"))


def test_deep_worker_uses_ventures_selected_manager(deep_env, monkeypatch):
    from app.chat import assistant
    from app.db.models import Job
    from app.db.task_models import TaskRun
    from app.org.manager import manager_name, set_manager_name
    from llm_gateway import sole
    from llm_gateway.provider_foundry import ProviderConfig
    env = deep_env
    selected = "Zoe" if env.domain.startswith("industrial") else "Amira"
    previous = manager_name(env.vid)
    question = "Analyze in depth for " + env.domain
    messages = assistant.build_messages(question=question, brief={"idea": {"name": env.domain}}, history=[])
    messages[0]["content"] = f"You are {previous}. Follow the saved task's policies."
    view = env.reasoning._prepare(env.wid, env.vid, question, messages, "persona", None)
    set_manager_name(env.vid, selected)
    seen = []
    async def complete(messages, **kwargs):
        seen.extend(message["content"] for message in messages if message["role"] == "system")
        return ""
    monkeypatch.setattr(assistant, "_complete", complete)
    monkeypatch.setattr(env.reasoning, "_post", lambda *args: None)
    monkeypatch.setattr(sole, "resolve_override", lambda wid: ProviderConfig())
    with env.db()() as session:
        run = session.get(TaskRun, view["run_id"])
        job = session.get(Job, run.control_job_id)
        job.status = "running"
        payload = json.loads(job.payload_json) | {"_job_id": job.id}
        session.commit()
    asyncio.run(env.reasoning.run_job(payload))
    assert len(seen) == 1
    assert seen[0].index("You are " + previous + ".") < seen[0].index("You are " + selected + ".")


def test_browse_jobs_share_parent_allowance_and_stop_with_parent(deep_env, monkeypatch):
    from app.credits import gate, metered
    from app.credits.errors import CreditsUnavailable
    from app.db.models import Job, WorkspaceMember
    from app.db.task_models import Task, TaskRun
    from app.tenancy import bind
    from app.tools import browse, ToolContext
    env = deep_env
    view = prepare(env)
    with env.db()() as session:
        session.add(WorkspaceMember(id="member_browse_" + env.vid, workspace_id=env.wid,
            person_id="per_wallet:" + env.vid, role="owner", created_at=env.reasoning.naive_utc()))
        session.commit()
    url = "https://" + ("procurement.example" if env.domain.startswith("industrial") else "recipes.example")
    ctx = ToolContext(env.vid, env.wid, budget_micro=5_000_000, founder_urls=[url], reasoning_run_id=view["run_id"])
    monkeypatch.setattr(browse, "enabled", lambda: True)
    monkeypatch.setattr(browse, "publish_wake", lambda: None)
    monkeypatch.setattr(browse.reader, "_target", lambda url: None)
    dispatched, jobs = [], []
    for index in range(3):
        with bind(workspace_id=env.wid, venture_id=env.vid, actor="per_wallet:" + env.vid):
            result = browse.start(url, env.domain, ctx)
        job_id = json.loads(result.split("<<<\n")[1].rsplit("\n>>>", 1)[0])["job_id"]
        with env.db()() as session:
            job = session.get(Job, job_id)
            job.status, job.locked_by, job.attempts = "leased", "worker", 1
            document = json.loads(job.payload_json)
            document["_runtime"]["deadline"] = time.time() + 300
            job.payload_json = json.dumps(document)
            payload = document | {"_job_id": job.id, "_worker_id": "worker", "_job_attempt": 1}
            session.commit()
        jobs.append(document)
        async def paid():
            dispatched.append(index)
            current = env.reasoning.snapshot(view["run_id"], env.wid, env.vid)
            assert current["reserved_micro"] == 2_000_000
            return "public evidence"
        async def execute():
            with gate.bounded_calls(env.wid, env.vid, lambda *args: browse._admit(payload, *args)):
                return await metered.call(paid, purpose="browse.web.summary", workspace_id=env.wid,
                    venture_id=env.vid, provider="fixture", estimate_micro=2_000_000,
                    actual_micro=lambda result: 2_000_000, request_id=f"browse-paid:{index}")
        if index < 2:
            assert asyncio.run(execute()) == "public evidence"
        else:
            with pytest.raises(CreditsUnavailable, match="allowance"):
                asyncio.run(execute())
        with env.db()() as session:
            session.get(Job, job_id).status = "succeeded"
            session.commit()
    assert dispatched == [0, 1]
    assert env.reasoning.snapshot(view["run_id"], env.wid, env.vid)["spend_micro"] == 4_000_000
    with env.db()() as session:
        run = session.get(TaskRun, view["run_id"])
        task = session.get(Task, view["task_id"])
        task.state = run.state = "working"
        journal = env.reasoning._journal(run)
        journal["state"] = "thinking"
        run.evaluation_json = json.dumps(journal)
        session.get(Job, run.control_job_id).status = "failed"
        session.commit()
    with pytest.raises(browse.BrowseStop, match="parent_stopped"):
        browse._check_parent(jobs[0])
    with env.db()() as session:
        run = session.get(TaskRun, view["run_id"])
        session.get(Job, run.control_job_id).status = "pending"
        run.deadline_at = env.reasoning.naive_utc()
        session.commit()
    with pytest.raises(browse.BrowseStop, match="parent_stopped"):
        browse._check_parent(jobs[0])
    with env.db()() as session:
        session.get(Task, view["task_id"]).state = "paused"
        session.commit()
    for document in jobs:
        with pytest.raises(browse.BrowseStop, match="parent_stopped"):
            browse._check_parent(document)


def test_flag_off_preserves_budget_and_charlie_prompt_bytes(deep_env):
    from app.chat.assistant import build_messages, SYSTEM_PROMPT
    from app.tasks.contracts import Budget
    env = deep_env
    env.flags.clear()
    arguments = dict(question="What do you know?", brief={"idea": {"name": env.domain}},
        history=[{"role": "assistant", "body_md": "I have your saved idea."}])
    before = build_messages(**arguments)
    assert before[0]["content"] == SYSTEM_PROMPT
    assert "Assistant: I have your saved idea." in before[1]["content"]
    assert Budget().cost_micro == 500_000
    env.flags["reasoning_v1"] = True
    after = build_messages(**arguments)
    assert "You are the venture's selected manager" in after[0]["content"]
    assert "Manager: I have your saved idea." in after[1]["content"]
    assert Budget().cost_micro == 5_000_000
    assert Budget(cost_micro=10_000).cost_micro == 10_000
    env.flags["reasoning_v1"] = False
    assert build_messages(**arguments) == before


def test_unavailable_policy_keeps_legacy_budget_and_prompt_bytes(deep_env, monkeypatch):
    from sqlalchemy.exc import OperationalError
    from app.credits import policy
    from app.tasks.contracts import Budget
    from app.chat.assistant import build_messages, SYSTEM_PROMPT
    env = deep_env
    def unavailable(*args):
        raise OperationalError("offline policy lookup", {}, Exception("policy storage unavailable"))
    monkeypatch.setattr(policy, "get_policy", unavailable)
    assert env.reasoning.enabled() is False and Budget().cost_micro == 500_000
    assert build_messages(question=env.domain, brief={"idea": {"name": env.domain}}, history=[])[0]["content"] == SYSTEM_PROMPT


def test_planner_uses_reasoning_default_without_raising_an_explicit_smaller_cap(deep_env):
    from app.tasks.contracts import Contract
    from app.tasks.planner import validate_contract
    env = deep_env
    for budget, expected in [({}, 5_000_000), ({"cost_micro": 10_000}, 10_000), ({"cost_micro": 50_000_000}, 5_000_000)]:
        contract = Contract(title="Your requested work", goal=env.domain, budget=budget,
            steps=[{"title": "Your requested work", "skill": "task.run"}])
        assert validate_contract(contract, goal=env.domain).budget.cost_micro == expected


def test_prepare_is_durable_idempotent_and_scoped_to_two_founders(deep_env):
    from app.db.task_models import Task, TaskRun
    from app.credits.errors import CreditsUnavailable
    env = deep_env
    first = prepare(env)
    assert prepare(env) == first
    other_wid = "ws_wallet:" + env.other_vid
    second = env.reasoning._prepare(other_wid, env.other_vid, "Analyze in depth " + env.other_vid,
        [{"role": "user", "content": "Unrelated question"}], "saved-question", None)
    assert first["task_id"] != second["task_id"]
    with env.db()() as session:
        assert len(list(session.scalars(select(Task)))) == 2
        assert len(list(session.scalars(select(TaskRun)))) == 2
    with pytest.raises(PermissionError):
        env.reasoning.snapshot(first["run_id"], other_wid, env.other_vid)
    env.flags.clear()
    with pytest.raises(CreditsUnavailable):
        prepare(env, "off-request")


def test_every_metered_retry_and_paid_tool_reserves_before_dispatch_and_stops_at_cap(deep_env):
    from app.credits import metered
    from app.credits.errors import CreditsUnavailable
    env = deep_env
    view = prepare(env)
    admission = env.reasoning.Admission(view["run_id"], env.wid, env.vid)
    observed = []

    async def execute():
        async def dispatch():
            observed.append(env.reasoning.snapshot(view["run_id"], env.wid, env.vid))
            return "Evidence"
        with metered.bounded_calls(env.wid, env.vid, admission):
            for request_id, amount in [("model:attempt:0", 2_000_000), ("model:attempt:1", 2_900_000), ("paid-tool", 100_000)]:
                await metered.call(dispatch, workspace_id=env.wid, venture_id=env.vid,
                    purpose="venture.chat.web_search", provider="fixture", request_id=request_id,
                    estimate_micro=amount, actual_micro=lambda result, amount=amount: amount)
            with pytest.raises(CreditsUnavailable):
                await metered.call(dispatch, workspace_id=env.wid, venture_id=env.vid,
                    purpose="venture.chat.web_search", provider="fixture", request_id="over-cap",
                    estimate_micro=1, actual_micro=lambda result: 1)
    asyncio.run(execute())
    assert len(observed) == 3
    assert [item["reserved_micro"] for item in observed] == [2_000_000, 2_900_000, 100_000]
    final = env.reasoning.snapshot(view["run_id"], env.wid, env.vid)
    assert final["state"] == "partial" and final["spend_micro"] == 5_000_000
    assert final["remaining_micro"] == 0 and final["reserved_micro"] == 0
    assert env.reasoning.reply(final)["actions"][-1]["label"] == "Continue for about $5?"


def test_unknown_outcome_keeps_allowance_protected_and_refuses_replay_or_extension(deep_env):
    from app.credits.errors import CreditsUnavailable
    env = deep_env
    view = prepare(env)
    admission = env.reasoning.Admission(view["run_id"], env.wid, env.vid)
    settle = admission(env.wallet.id, "uncertain-call", 4_999_999)
    value = meter(env, "uncertain-call")
    value.recorded = False
    value.outcome_protected = True
    settle(value)
    final = env.reasoning.snapshot(view["run_id"], env.wid, env.vid)
    assert final["state"] == "uncertain" and final["reserved_micro"] == 4_999_999
    with pytest.raises(CreditsUnavailable):
        admission(env.wallet.id, "uncertain-call", 4_999_999)
    with pytest.raises(ValueError, match="earlier calls"):
        approve(env, view["task_id"])
    assert all("Continue" not in action["label"] for action in env.reasoning.reply(final)["actions"])


def test_scope_change_blocks_new_calls_but_does_not_lose_confirmed_usage(deep_env):
    from app.db.models import Workspace
    env = deep_env
    view = prepare(env)
    admission = env.reasoning.Admission(view["run_id"], env.wid, env.vid)
    settle = admission(env.wallet.id, "started-call", 100_000)
    with env.db()() as session:
        session.get(Workspace, env.wid).owner_id = "per_wallet:" + env.other_vid
        session.commit()
    with pytest.raises(PermissionError):
        admission(env.wallet.id, "new-call", 100_000)
    settle(meter(env, "started-call", 40_000))
    from app.db.task_models import TaskRun
    with env.db()() as session:
        run = session.get(TaskRun, view["run_id"])
        assert run.cost_micro == 40_000 and run.reserved_cost_micro == 0


def test_chat_stop_prevents_any_new_dispatch_without_losing_partial_work(deep_env):
    from app.db.models import Job
    from app.db.task_models import TaskRun
    from app.credits.errors import CreditsUnavailable
    env = deep_env
    view = prepare(env)
    with env.db()() as session:
        run = session.get(TaskRun, view["run_id"])
        session.get(Job, run.control_job_id).cancel_requested = True
        session.commit()
    admission = env.reasoning.Admission(view["run_id"], env.wid, env.vid)
    with pytest.raises(CreditsUnavailable, match="stopped"):
        admission(env.wallet.id, "new-call", 10_000)
    final = env.reasoning.snapshot(view["run_id"], env.wid, env.vid)
    assert final["state"] == "stopped" and final["spend_micro"] == 0 and final["reserved_micro"] == 0
    assert all("Continue" not in action["label"] for action in env.reasoning.reply(final)["actions"])


def test_non_budget_partial_does_not_request_more_money(deep_env):
    env = deep_env
    view = prepare(env)
    journal = {"format": "reasoning_v1", "state": "partial", "last_body": "Your partial answer for " + env.domain}
    final = env.reasoning._save(view["run_id"], env.wid, env.vid, journal)
    assert final["remaining_micro"] == 5_000_000
    assert all("Continue" not in action["label"] for action in env.reasoning.reply(final)["actions"])


def test_founder_approval_extends_same_task_once_to_ten_dollars_without_resetting_spend(deep_env):
    from app.db.task_models import TaskEvent
    env = deep_env
    view = prepare(env)
    admission = env.reasoning.Admission(view["run_id"], env.wid, env.vid)
    admission(env.wallet.id, "first-call", 5_000_000)(meter(env, "first-call", 5_000_000))
    journal = {"format": "reasoning_v1", "state": "partial", "last_body": "Here is useful partial work for " + env.domain,
        "messages": [], "phase": "synthesis", "round": 1, "effort": "low"}
    env.reasoning._save(view["run_id"], env.wid, env.vid, journal)
    with pytest.raises(PermissionError):
        env.reasoning._approve(view["task_id"], "ws_wallet:" + env.other_vid, env.other_vid, "foreign-approval")
    from app.tenancy import bind
    with bind(workspace_id=env.wid, venture_id=env.vid, actor="per_wallet:" + env.other_vid):
        with pytest.raises(PermissionError, match="owner"):
            env.reasoning._approve(view["task_id"], env.wid, env.vid, "member-approval")
    extended = approve(env, view["task_id"])
    assert extended["cap_micro"] == 10_000_000 and extended["spend_micro"] == 5_000_000
    assert extended["run_id"] == view["run_id"]
    assert approve(env, view["task_id"], "approval-again") == extended
    with env.db()() as session:
        assert len(list(session.scalars(select(TaskEvent).where(TaskEvent.kind == "reasoning_approved")))) == 1
    admission(env.wallet.id, "continued-call", 5_000_000)(meter(env, "continued-call", 5_000_000))
    final = env.reasoning.snapshot(view["run_id"], env.wid, env.vid)
    assert final["spend_micro"] == 10_000_000
    from app.chat.intent import classify_intent
    text = f"Continue my deep answer [reasoning:{view['task_id']}] for about $5?"
    assert env.reasoning.continuation_task(text) == view["task_id"]
    assert classify_intent(text) == "assistant"
    assert env.reasoning.continuation_task(f"Continue my deep answer [reasoning:{view['task_id']}] for about $20?") is None


@pytest.mark.parametrize("empty_result", [False, True])
def test_worker_escalates_next_call_only_keeps_tools_low_and_does_not_block_event_loop(deep_env, monkeypatch, empty_result):
    from app.chat import assistant
    from app.credits import metered
    from app.db.models import Job
    from app.tools import __name__ as tool_module_name
    from llm_gateway import sole
    from llm_gateway.provider_foundry import ProviderConfig
    import importlib
    env = deep_env
    view = prepare(env)
    config = ProviderConfig(provider="azure_foundry", base_url="https://offline.invalid/openai/v1", api_key="fixture",
        deployment_pro="gpt-6.1-sol", deployment_flash="gpt-6.1-sol", source="platform_default")
    monkeypatch.setattr(sole, "resolve_override", lambda wid: config)
    posts, calls = [], []
    monkeypatch.setattr(env.reasoning, "_post", lambda view, conversation: posts.append(view))
    script = [('{"effort":"low"}', None), ("", "request_reasoning_effort"),
        ("A partial plan for " + env.domain, None), ("", "web_search"),
        ("The collected evidence", None), ("", "request_reasoning_effort"), ("Your deep answer for " + env.domain, None)]

    async def complete(messages, **kwargs):
        time.sleep(0.02)
        calls.append((messages, kwargs))
        text, name = script[len(calls)-1]
        arguments = {"effort": "max"} if name == "request_reasoning_effort" else {"query": env.domain}
        tool_calls = [SimpleNamespace(id="call-" + str(len(calls)), type="function",
            function=SimpleNamespace(name=name, arguments=json.dumps(arguments)))] if name else None
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text, tool_calls=tool_calls))])
        if empty_result:
            response.choices = []
        return await metered.call(lambda: asyncio.sleep(0, result=response), workspace_id=env.wid, venture_id=env.vid,
            purpose="venture.chat.web_search", request_id=kwargs["request_id"], provider="fixture",
            estimate_micro=20_000, actual_micro=lambda result: 5000)
    async def execute_tool(name, arguments, ctx):
        assert ctx.venture_id == env.vid
        return await metered.call(lambda: asyncio.sleep(0, result="Untrusted source data for " + env.domain),
            workspace_id=env.wid, venture_id=env.vid, purpose="venture.chat.web_search", provider="fixture",
            request_id="tool-call", estimate_micro=20_000, actual_micro=lambda result: 5000)
    monkeypatch.setattr(assistant, "_complete", complete)
    monkeypatch.setattr(importlib.import_module(tool_module_name), "execute_tool", execute_tool)
    with env.db()() as session:
        run = session.get(__import__("app.db.task_models", fromlist=["TaskRun"]).TaskRun, view["run_id"])
        job = session.get(Job, run.control_job_id)
        job.status = "running"
        payload = json.loads(job.payload_json) | {"_job_id": job.id}
        session.commit()
    ticks = []
    async def execute():
        running = asyncio.create_task(env.reasoning.run_job(payload))
        while not running.done():
            ticks.append(time.monotonic())
            await asyncio.sleep(0.005)
        await running
    asyncio.run(execute())
    if empty_result:
        assert len(calls) == 1 and len(ticks) > 2
        assert posts[-1]["state"] == "partial" and posts[-1]["spend_micro"] == 5000
        assert all("Continue" not in action["label"] for action in env.reasoning.reply(posts[-1])["actions"])
        return
    assert len(calls) == 7 and len(ticks) > 10
    assert [item[1]["reasoning_effort"] for item in calls] == ["low", "low", "medium", "low", "low", "medium", "high"]
    assert [item[1]["output_cap"] for item in calls] == [2048, 128_000, 128_000, 4096, 4096, 128_000, 128_000]
    assert all("Remaining allowance for this task: $" in messages[0]["content"] for messages, _ in calls)
    assert posts[-1]["state"] == "complete" and posts[-1]["spend_micro"] == 40_000
    assert all(env.other_vid not in json.dumps(messages) for messages, _ in calls)

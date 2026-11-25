"""Deep chat reaches the registered worker, saved history and real credit ledger."""
import asyncio
from asyncio import sleep as async_sleep
import json
from datetime import datetime, timedelta, timezone
from itertools import count
from types import SimpleNamespace

import pytest
from openai import AsyncOpenAI, _base_client
from sqlalchemy import select

from tests.unit.tasks.test_reasoning import deep_env as deep_env, credits_env as credits_env
from tests.unit.credits.test_chat_provider_captures import chat_capture as chat_capture


@pytest.fixture
def account(deep_env):
    return deep_env.wallet


@pytest.mark.parametrize("flag_on", [False, True])
@pytest.mark.parametrize("local", [False, True])
def test_deep_question_runs_registered_job_posts_chat_and_settles_only_its_founder(deep_env, monkeypatch, flag_on, local, request):
    from app.api import chat_api
    from app.chat import assistant
    from app.chat.conversations import bind_conversation, create
    from app.chat.store import list_messages
    from app.core.jobs import lease
    from app.db.models import CreditAccount, CreditReservation, Job
    from app.db.task_models import Task, TaskRun
    from app.org.characters import parse_speaker
    from app.tenancy import bind
    from app.worker import handlers, main
    from core import clock
    from llm_gateway import sole

    env = deep_env
    if not local:
        request.getfixturevalue("chat_capture")
    env.flags["reasoning_v1"] = flag_on
    monkeypatch.setenv("SUPYMEM_MODE", "local" if local else "hosted")
    monkeypatch.setenv("LLM_CASSETTE_MODE", "off")
    if local:
        from app.core.local_secrets import update_settings
        from app.core.providers_local import save_settings
        save_settings(provider="azure_openai", base_url="https://offline.invalid/openai/v1",
            api_key="synthetic-reasoning-key", models={"chat": "supymem-gpt-6-1-sol"})
        update_settings("capabilities", {"background_work": flag_on}, {})
        monkeypatch.setenv("LLM_OFFLINE", "0")
        monkeypatch.setattr(sole, "_primary_chat", None)
        monkeypatch.setattr(sole, "_fallback_chat", None)
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setattr(asyncio, "sleep", async_sleep)
    ticks = count()
    monkeypatch.setattr(clock, "_clock", lambda: datetime(2026, 10, 9, 12, 34, 56, tzinfo=timezone.utc)
        + timedelta(microseconds=next(ticks)))
    monkeypatch.setattr(handlers, "HANDLERS", {})
    handlers.install_default_handlers()
    assert "tasks.execute" in handlers.HANDLERS
    monkeypatch.setattr(assistant, "get_venture_state_brief", lambda *args, **kwargs: {
        "workspace_id": env.wid, "idea": {"name": env.domain}})
    monkeypatch.setattr(assistant, "diagram_messages", lambda messages, **kwargs: messages)
    httpx = getattr(_base_client, "httpx", None) or _base_client.httpx2
    seen = []
    final_text = "Your analysis for " + env.domain

    async def respond(request):
        body = json.loads(request.content)
        seen.append(body)
        assert env.domain in json.dumps(body)
        with env.db()() as session:
            holds = list(session.scalars(select(CreditReservation).where(CreditReservation.account_id == env.wallet.id)))
            if local:
                assert holds == []
            else:
                assert sum(hold.status == "reconciling" for hold in holds) == 1
            if flag_on:
                run = session.scalar(select(TaskRun).join(Task).where(Task.venture_id == env.vid))
                assert run.reserved_cost_micro > 0
        text = '{"effort":"medium"}' if flag_on and len(seen) == 1 else final_text
        return httpx.Response(200, json={"id": "resp_reasoning_e2e", "object": "response",
            "status": "completed", "model": "supymem-gpt-6-1-sol", "service_tier": "default",
            "output": [{"type": "message", "role": "assistant", "status": "completed", "content": [
                {"type": "output_text", "text": text, "annotations": []}]}],
            "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
                "input_tokens_details": {"cached_tokens": 80}, "output_tokens_details": {"reasoning_tokens": 7}}})

    def client(config):
        return AsyncOpenAI(api_key="offline", base_url="https://offline.invalid/openai/v1", max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond), trust_env=False)), config
    monkeypatch.setattr(sole, "create_async_client_from_config", client)
    with env.db()() as session:
        before = session.get(CreditAccount, env.wallet.id).spent_micro
        other_before = session.get(CreditAccount, env.other.id).spent_micro

    question = "What are the tradeoffs in depth for " + env.domain + "?"
    with bind(workspace_id=env.wid, venture_id=env.vid, actor="per_wallet:" + env.vid):
        conversation = create(env.vid, idempotency_key="same-founder-conversation")
        with bind_conversation(env.vid, conversation["id"]):
            answer = asyncio.run(chat_api._generic_reply(env.vid,
                SimpleNamespace(reply_to_question=False, question_id=None, job_id=None),
                question, "per_wallet:" + env.vid, env.wid, request_id="same-founder-request"))
    with env.db()() as session:
        jobs = list(session.scalars(select(Job).where(Job.kind == "tasks.execute")))
        if flag_on:
            assert answer["kind"] == "deep_answer_pending" and len(seen) == 0 and len(jobs) == 1
            claimed = lease(session, "reasoning-e2e", kinds=["tasks.execute"], job_classes=["bulk"])
            assert len(claimed) == 1 and claimed[0].id == jobs[0].id
            job = claimed[0]
            session.commit()
        else:
            assert answer["kind"] == "text" and parse_speaker(answer["body_md"])[1] == final_text
            assert jobs == [] and len(seen) == 1
            assert session.scalar(select(Task.id)) is None

    if flag_on:
        payload = {**job.payload, "_job_id": job.id, "_worker_id": job.worker_id, "_job_attempt": job.attempts}
        assert job.payload["_chat_conversation"]["conversation_id"] == conversation["id"]
        asyncio.run(main._run_one(env.db(), job, set()))
        view = env.reasoning.snapshot(payload["reasoning_run_id"], env.wid, env.vid)
        assert view["state"] == "complete" and view["body"] == final_text
        assert view["reserved_micro"] == 0 and len(seen) == 4
        with pytest.raises(PermissionError):
            env.reasoning.snapshot(payload["reasoning_run_id"], "ws_wallet:" + env.other_vid, env.other_vid)

    with env.db()() as session:
        holds = list(session.scalars(select(CreditReservation).where(CreditReservation.account_id == env.wallet.id)))
        spent = sum(hold.settled_micro for hold in holds)
        wallet = session.get(CreditAccount, env.wallet.id)
        if local:
            from app.credits import local_meter
            usage = local_meter.snapshot(workspace_id=env.wid, venture_id=env.vid)
            spent = usage["month_cost_usd_micro"]
            assert spent > 0 and sum(task["prompt_tokens"] for task in usage["tasks"]) == 100 * len(seen)
            assert sum(task["completion_tokens"] for task in usage["tasks"]) == 20 * len(seen)
            assert all(task["unconfirmed_calls"] == 0 for task in usage["tasks"])
            assert holds == [] and wallet.spent_micro == before and wallet.reserved_micro == 0
            assert local_meter.snapshot(venture_id=env.other_vid)["month_cost_usd_micro"] == 0
        else:
            assert len(holds) == len(seen) and all(hold.status == "settled" for hold in holds)
            assert spent > 0 and wallet.spent_micro - before == spent and wallet.reserved_micro == 0
        assert session.get(CreditAccount, env.other.id).spent_micro == other_before
        assert session.scalar(select(CreditReservation.id).where(CreditReservation.account_id == env.other.id)) is None
        if flag_on:
            assert view["spend_micro"] == spent and session.get(Job, job.id).status == "succeeded"
    with bind(workspace_id=env.wid, venture_id=env.vid, actor="per_wallet:" + env.vid):
        history = list_messages(env.vid, conversation_id=conversation["id"])
        assert final_text in history[-1]["body_md"]
        if flag_on:
            assert history[-1]["kind"] == "deep_answer"
            assert history[-1]["actions"][0]["payload"]["reasoning"]["spend_micro"] == spent
    with bind(workspace_id="ws_wallet:" + env.other_vid, venture_id=env.other_vid, actor="per_wallet:" + env.other_vid):
        assert list_messages(env.other_vid) == []

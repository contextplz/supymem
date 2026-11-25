"""Short chat stays on its existing path; deep questions are durably queued."""
import asyncio
import json

from tests.unit.tasks.test_reasoning import deep_env as deep_env, credits_env as credits_env


def test_short_chat_stays_fast_and_deep_questions_queue_without_calling_the_model(deep_env, monkeypatch):
    from app.chat import assistant
    from app.tasks import reasoning
    from app.db.models import Job
    from sqlalchemy import select
    env = deep_env
    calls = []
    monkeypatch.setattr(assistant, "get_venture_state_brief", lambda *args, **kwargs: {
        "workspace_id": env.wid, "idea": {"name": env.domain}})
    async def messages(venture_id, question, **kwargs):
        return [{"role": "system", "content": "You are Charlie."}, {"role": "user", "content": question}]
    async def complete(messages, **kwargs):
        calls.append(kwargs)
        return "Your saved context for " + env.domain
    monkeypatch.setattr(assistant, "question_messages", messages)
    monkeypatch.setattr(assistant, "_complete", complete)
    monkeypatch.setattr(assistant, "diagram_messages", lambda messages, **kwargs: messages)
    short = asyncio.run(assistant.answer_question(env.vid, "How are you?", workspace_id=env.wid, request_id="short"))
    assert short["kind"] == "text" and len(calls) == 1
    deep = asyncio.run(assistant.answer_question(env.vid, "Analyze tradeoffs in depth for " + env.domain,
        workspace_id=env.wid, request_id="deep"))
    assert deep["kind"] == "deep_answer_pending" and "Thinking…" in deep["body_md"]
    assert len(calls) == 1
    with env.db()() as session:
        queued = list(session.scalars(select(Job).where(Job.kind == "tasks.execute")))
        assert len(queued) == 1 and json.loads(queued[0].payload_json)["reasoning_v1"] is True
        assert queued[0].workspace_id == env.wid and queued[0].venture_id == env.vid
    assert not reasoning.wants_depth("How are you?")


def test_flag_off_deep_question_uses_exact_existing_chat_path(deep_env, monkeypatch):
    from app.chat import assistant
    env = deep_env
    env.flags.clear()
    calls = []
    monkeypatch.setattr(assistant, "get_venture_state_brief", lambda *args, **kwargs: {
        "workspace_id": env.wid, "idea": {"name": env.domain}})
    async def messages(*args, **kwargs):
        return [{"role": "system", "content": "Legacy prompt"}, {"role": "user", "content": env.domain}]
    async def complete(messages, **kwargs):
        calls.append((messages, kwargs))
        return "Legacy answer for " + env.domain
    monkeypatch.setattr(assistant, "question_messages", messages)
    monkeypatch.setattr(assistant, "_complete", complete)
    monkeypatch.setattr(assistant, "diagram_messages", lambda messages, **kwargs: messages)
    result = asyncio.run(assistant.answer_question(env.vid, "Analyze tradeoffs in depth for " + env.domain,
        workspace_id=env.wid, request_id="legacy"))
    assert result["kind"] == "text" and result["body_md"] == "Legacy answer for " + env.domain
    assert calls[0][1] == {"workspace_id": env.wid, "request_id": "legacy:answer"}


def test_deep_preparation_does_not_call_the_paid_rephraser_before_task_admission(deep_env, monkeypatch):
    import threading
    from app.chat import assistant, compaction, rephrase
    env = deep_env
    main_thread = threading.get_ident()
    def brief(*args, **kwargs):
        assert threading.get_ident() != main_thread
        return {"workspace_id": env.wid, "idea": {"name": env.domain}}
    monkeypatch.setattr(assistant, "get_venture_state_brief", brief)
    monkeypatch.setattr(assistant, "list_messages", lambda *args, **kwargs: [])
    monkeypatch.setattr(compaction, "context_for_question", lambda *args: None)
    async def prohibited(*args, **kwargs):
        raise AssertionError("Deep preparation cannot dispatch an unbudgeted model call")
    monkeypatch.setattr(rephrase, "prepare", prohibited)
    result = asyncio.run(assistant.answer_question(env.vid, "Analyze it in depth for " + env.domain,
        workspace_id=env.wid, request_id="deep-preparation"))
    assert result["kind"] == "deep_answer_pending"
    saved = env.reasoning.snapshot(result["actions"][0]["payload"]["reasoning"]["run_id"], env.wid, env.vid)
    assert saved["spend_micro"] == saved["reserved_micro"] == 0


def test_deep_preparation_uses_bounded_history_without_python_ranking(deep_env, monkeypatch):
    from app.chat import assistant, compaction
    env = deep_env
    limits = []
    def history(venture_id, *, limit):
        assert venture_id == env.vid
        limits.append(limit)
        return [{"role": "founder", "body_md": env.domain}]
    def unbounded(*args, **kwargs):
        raise AssertionError("Deep answers cannot rank unbounded history")
    monkeypatch.setattr(assistant, "list_messages", history)
    monkeypatch.setattr(compaction, "context_for_question", unbounded)
    messages = asyncio.run(assistant.question_messages(env.vid, "Analyze in depth for " + env.domain,
        brief={"idea": {"name": env.domain}}, workspace_id=env.wid, request_id="bounded-context", deep_answer=True))
    assert limits == [assistant.HISTORY_TURNS * 2]
    assert env.domain in messages[1]["content"]

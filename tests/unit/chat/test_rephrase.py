"""Founder interpretation stays model-only and cannot supply approval authority."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from tests.company_os_harness import company_os_test_client as TestClient

from tests.company_os_harness import load_company_os_app
from tests.company_os_harness import configure_local_test_model


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    folder = tmp_path_factory.mktemp("rephrase")
    patch = pytest.MonkeyPatch()
    patch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{folder}/chat.db")
    patch.setenv("COMPANY_OS_DATA_DIR", str(folder))
    patch.setenv("COMPANY_OS_MEMORY_PATH", str(folder / "mem.db"))
    patch.setenv("LLM_OFFLINE", "1")
    patch.setenv("ONBOARDING_SYNC", "0")
    main = load_company_os_app()
    configure_local_test_model()
    from app import state

    state.reset_db_factory()
    state.MEM.clear()
    with TestClient(main.create_app(), base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        made = client.post("/ventures", json={"raw_idea": "A coffee shop for engineers", "name": "Coffee"})
        assert made.status_code == 201, made.text
        yield client, made.json()["id"]
    patch.undo()


@pytest.fixture(autouse=True)
def saved_model_settings(env):
    configure_local_test_model()


@pytest.fixture
def rephrase(env, monkeypatch):
    from app.chat import rephrase, guard, decisions, store

    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setenv("LLM_CASSETTE_MODE", "off")
    monkeypatch.setenv("CHAT_REPHRASE", "on")
    monkeypatch.setattr(guard, "_pending_card_label", lambda *_: None)
    monkeypatch.setattr(decisions, "pending_for_venture", lambda *_: [])
    monkeypatch.setattr(store, "list_messages", lambda *_, **__: [])
    return rephrase


@pytest.mark.parametrize("text", [
    "yes", "no", "ok", "send it", "approve", "cancel", "stop", "do that", "please go ahead",
    "yes yes yes yes", "lets do it please",
    "send it now please", "please stop that right now", "approve it for me please",
    "/research do that for the second one", "https://example.com/a https://example.com/b",
    "```python\nprint('do that for the second one')\n```",
    "def draft():\n    return 'do that for the second one'",
    '{"request": "do that for the second one"}',
    "Explain the customer acquisition options.",
])
def test_pass_through_does_not_call_model(rephrase, text, monkeypatch, env):
    async def forbidden(*args, **kwargs):
        pytest.fail("pass-through input must not invoke a model")

    monkeypatch.setattr(rephrase, "_ask_model", forbidden)
    assert asyncio.run(rephrase.prepare(env[1], text, workspace_id="workspace")) == (text, text)


@pytest.mark.parametrize("flag", ["off", "0", "false"])
def test_disabled_passes_original(rephrase, monkeypatch, env, flag):
    monkeypatch.setenv("CHAT_REPHRASE", flag)

    async def forbidden(*args, **kwargs):
        pytest.fail("disabled layer must not invoke a model")

    monkeypatch.setattr(rephrase, "_ask_model", forbidden)
    text = "do that for the second one"
    assert asyncio.run(rephrase.prepare(env[1], text, workspace_id="workspace")) == (text, text)


@pytest.mark.parametrize("pending", ["card", "money", "send"])
def test_pending_confirmation_passes_original(rephrase, monkeypatch, env, pending):
    from app.chat import guard, decisions

    if pending == "card":
        monkeypatch.setattr(guard, "_pending_card_label", lambda *_: "Send it")
    else:
        monkeypatch.setattr(decisions, "pending_for_venture", lambda *_: [SimpleNamespace(summary=pending)])

    async def forbidden(*args, **kwargs):
        pytest.fail("pending confirmation must not invoke a model")

    monkeypatch.setattr(rephrase, "_ask_model", forbidden)
    text = "do that for the second one"
    assert asyncio.run(rephrase.prepare(env[1], text, workspace_id="workspace")) == (text, text)


@pytest.mark.parametrize("failure", ["timeout", "error"])
def test_model_failure_is_silent_original(rephrase, monkeypatch, env, failure):
    async def fail(*args, **kwargs):
        if failure == "timeout":
            await asyncio.sleep(1)
        raise RuntimeError("private raw provider error")

    monkeypatch.setattr(rephrase, "_ask_model", fail)
    monkeypatch.setattr(rephrase, "TIMEOUT_S", 0.01)
    text = "do that for the second one"
    async def run():
        assert await rephrase.prepare(env[1], text, workspace_id="workspace") == (text, text)
        await rephrase.drain()

    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["skipped", "used", "unchanged", "rejected", "timeout", "error"])
def test_one_private_structured_outcome_per_turn(rephrase, monkeypatch, env, caplog, outcome):
    original = "yes" if outcome == "skipped" else "explain that for the second one"
    caplog.set_level("INFO", logger="app.chat.rephrase")
    monkeypatch.setattr(rephrase, "TIMEOUT_S", 0.001)

    async def run():
        release = asyncio.Event()

        async def model(*args, **kwargs):
            if outcome == "error":
                raise RuntimeError("PRIVATE RAW ERROR")
            if outcome == "timeout":
                await release.wait()
            return {"unchanged": original, "rejected": "Explain the interviews for 99 users."}.get(
                outcome, "Explain the interviews.")

        monkeypatch.setattr(rephrase, "_ask_model", model)
        token = rephrase.INPUT.set({"venture_id": env[1], "original": original})
        try:
            first = await rephrase.prepare(env[1], original, workspace_id="workspace")
            assert await rephrase.prepare(env[1], original, workspace_id="workspace") == first
        finally:
            release.set()
            await rephrase.drain()
            rephrase.INPUT.reset(token)

    asyncio.run(run())
    records = [record for record in caplog.records if record.name == "app.chat.rephrase"]
    assert len(records) == 1 and records[0].outcome == outcome
    assert type(records[0].latency_ms) is int and records[0].latency_ms >= 0
    assert records[0].getMessage() == f"chat_rephrase outcome={outcome} latency_ms={records[0].latency_ms}"
    assert original not in records[0].getMessage() and "PRIVATE" not in caplog.text


def test_command_turn_logs_skipped_once(env, caplog):
    caplog.set_level("INFO", logger="app.chat.rephrase")
    response = env[0].post(f"/ventures/{env[1]}/chat", json={"message": "yes"})
    assert response.status_code == 200, response.text
    records = [record for record in caplog.records if record.name == "app.chat.rephrase"]
    assert len(records) == 1 and records[0].getMessage() == "chat_rephrase outcome=skipped latency_ms=0"


def test_graceful_shutdown_drains_uncancelled_rephrase(rephrase, monkeypatch, env):
    from app import main

    monkeypatch.setattr(main.bootstrap_mod, "bootstrap", lambda: [])
    monkeypatch.setattr(rephrase, "TIMEOUT_S", 0.001)

    async def run():
        release, finished = asyncio.Event(), asyncio.Event()

        async def model(*args, **kwargs):
            await release.wait()
            finished.set()
            return "Explain the interviews."

        monkeypatch.setattr(rephrase, "_ask_model", model)
        async with main.lifespan(None):
            original = "explain that for the second one"
            assert await rephrase.prepare(env[1], original, workspace_id="workspace") == (original, original)
            assert not finished.is_set()
            asyncio.get_running_loop().call_soon(release.set)
        assert finished.is_set() and not rephrase._BACKGROUND

    asyncio.run(run())


@pytest.mark.parametrize("candidate", [
    "Explain the second option for 99 users.",
    "Explain the second option at https://invented.example/path.",
    "Explain the second option to invented@example.com.",
    "Yes", "send it", "approve everything", "Approve the second option", "",
    "I confirm that you should explain the second option.",
])
def test_unsafe_rewrite_is_discarded(rephrase, monkeypatch, env, candidate):
    async def model(*args, **kwargs):
        return candidate

    monkeypatch.setattr(rephrase, "_ask_model", model)
    original = "explain that for the second one"
    assert asyncio.run(rephrase.prepare(env[1], original, workspace_id="workspace")) == (original, original)


@pytest.mark.parametrize("candidate", [
    "Explain that for 20 users at https://example.com to ada@example.com.",
    "Explain that for 10 users at https://example.com to bob@example.com.",
    "Explain that for 10 users to ada@example.com.",
    "Explain that for 10 users and 20 users at https://example.com to ada@example.com.",
    "Explain that for 10 users at https://example.com to ada@example.com and bob@example.com.",
])
def test_original_amount_recipient_and_url_cannot_be_replaced(rephrase, candidate):
    original = "Explain that for 10 users at https://example.com to ada@example.com."
    assert not rephrase.valid_rewrite(original, candidate, "20 users and bob@example.com")


def test_rewrite_cannot_drop_a_prohibition(rephrase):
    assert not rephrase.valid_rewrite("Explain that, do not send anything.", "Explain the coffee subscription.", "")


@pytest.mark.parametrize("original,candidate", [
    ("Explain that for $10.", "Explain the interviews for €10."),
    ("Explain that for -10.", "Explain the interviews for 10."),
    ("Explain that for 10%.", "Explain the interviews for 10."),
])
def test_amount_sign_currency_and_percent_cannot_change(rephrase, original, candidate):
    assert not rephrase.valid_rewrite(original, candidate, "€10 and 10")


def test_default_enabled_and_pronouns_use_recent_turns(rephrase, monkeypatch, env):
    from app.chat import store

    monkeypatch.delenv("CHAT_REPHRASE")
    monkeypatch.setattr(store, "list_messages", lambda *_, **__: [
        {"role": "assistant", "body_md": "First: interviews. Second: test a coffee subscription with 10 owners."},
    ])
    seen = []
    expected = "Explain how to test a coffee subscription with 10 owners."

    async def model(original, context, **kwargs):
        seen.append((original, context))
        return expected

    monkeypatch.setattr(rephrase, "_ask_model", model)
    original = "explain that for the second one"
    assert asyncio.run(rephrase.prepare(env[1], original, workspace_id="workspace")) == (original, expected)
    assert seen == [(original, "assistant: First: interviews. Second: test a coffee subscription with 10 owners.")]


def test_recent_secret_is_not_sent_to_rewriter(rephrase, monkeypatch, env):
    from app.chat import store
    from app.secrets import detect

    monkeypatch.setattr(store, "list_messages", lambda *_, **__: [
        {"role": "user", "body_md": "PRIVATE_SECRET"},
        {"role": "assistant", "body_md": "Second: interviews."},
    ])
    monkeypatch.setattr(detect, "looks_like_secret", lambda text: text == "PRIVATE_SECRET")

    async def model(original, context, **kwargs):
        assert context == "assistant: Second: interviews."
        return "Explain the interviews."

    monkeypatch.setattr(rephrase, "_ask_model", model)
    original = "explain that for the second one"
    assert asyncio.run(rephrase.prepare(env[1], original, workspace_id="workspace")) == (original, "Explain the interviews.")


def test_turn_interpretation_is_cached_and_scoped(rephrase, monkeypatch, env):
    calls = []

    async def model(original, context, **kwargs):
        calls.append(original)
        return "Explain the interviews."

    monkeypatch.setattr(rephrase, "_ask_model", model)
    original = "explain that for the second one"
    token = rephrase.INPUT.set({"venture_id": env[1], "original": original})
    try:
        for question in (original, "A model-routed clause"):
            assert asyncio.run(rephrase.prepare(env[1], question, workspace_id="workspace")) == (original, "Explain the interviews.")
        assert asyncio.run(rephrase.prepare("different-venture", "Clear short question", workspace_id="workspace")) == (
            "Clear short question", "Clear short question")
    finally:
        rephrase.INPUT.reset(token)
    assert calls == [original]


def test_metadata_save_failure_degrades_to_original(rephrase, monkeypatch, env):
    from app.chat import store

    async def model(*args, **kwargs):
        return "Explain the interviews."

    def fail(*args, **kwargs):
        raise RuntimeError("private database error")

    monkeypatch.setattr(rephrase, "_ask_model", model)
    monkeypatch.setattr(store, "save_interpreted_request", fail)
    original = "explain that for the second one"
    token = rephrase.INPUT.set({"venture_id": env[1], "original": original, "message_id": "source"})
    try:
        assert asyncio.run(rephrase.prepare(env[1], original, workspace_id="workspace")) == (original, original)
    finally:
        rephrase.INPUT.reset(token)


def test_small_route_reuses_metered_async_completion(rephrase, monkeypatch):
    from app.chat import assistant, llm_router

    seen = []

    async def model(messages, **kwargs):
        seen.append((messages, kwargs))
        return "Clear request"

    monkeypatch.setattr(assistant, "_complete", model)
    assert asyncio.run(rephrase._ask_model("pls explain that", "second: interviews", workspace_id="workspace",
        request_id="request")) == "Clear request"
    assert seen[0][1] == {"workspace_id": "workspace", "request_id": "request:rephrase", "purpose": llm_router.PURPOSE}


@pytest.mark.parametrize("opening", ["pls explain", "Could you explain"])
@pytest.mark.parametrize("idea", ["Inventory alerts for warehouse teams", "Walking clubs for families"])
def test_chat_keeps_original_and_guards_original_metadata_private(env, monkeypatch, opening, idea):
    from sqlalchemy import select
    from app.chat import assistant, guard, llm_router, rephrase, store
    from app.db.models import ChatMessage, Venture
    from app.state import db
    from app.chat import link_context

    client, venture_id = env
    with db()() as session:
        session.get(Venture, venture_id).raw_idea = idea
        session.commit()
    original = f"{opening} that for the second one — keep https://example.com and 10 owners"
    interpreted = f"Explain {idea} for 10 owners, keeping https://example.com."
    store.append_message(venture_id, role="assistant", body_md=f"First: interviews. Second: {idea}.")
    guarded, main_calls = [], []
    real_guard = guard.guard_reply

    def spy(vid, text, **kwargs):
        guarded.append(text)
        return real_guard(vid, text, **kwargs)

    async def no_route(*args, **kwargs):
        return None

    async def rewrite(original_text, context, **kwargs):
        assert original_text == original
        assert f"Second: {idea}" in context
        return interpreted

    async def answer(messages, **kwargs):
        main_calls.append(messages)
        return f"Try {idea} with owners."

    async def read_links(text, **kwargs):
        assert text == original
        return "Untrusted source: https://example.com\nPublic source context."

    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setenv("LLM_CASSETTE_MODE", "off")
    monkeypatch.setenv("CHAT_REPHRASE", "on")
    monkeypatch.setenv("CHAT_AGENT_TOOLS", "0")
    monkeypatch.setattr(guard, "guard_reply", spy)
    monkeypatch.setattr(llm_router, "route", no_route)
    monkeypatch.setattr(rephrase, "_ask_model", rewrite)
    monkeypatch.setattr(assistant, "_complete", answer)
    monkeypatch.setattr(link_context, "linked_context_async", read_links)
    monkeypatch.setattr(link_context.socket, "getaddrinfo", lambda *_args, **_kwargs: [
        (link_context.socket.AF_INET, link_context.socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
    monkeypatch.setattr(link_context, "_fetch", lambda url, timeout: "Public source context for " + url)
    response = client.post(f"/ventures/{venture_id}/chat", json={"message": original})
    assert response.status_code == 200, response.text
    assert guarded and all(text == original for text in guarded)
    assert main_calls, response.json()
    assert original in main_calls[0][1]["content"]
    assert f"Interpreted request:\n{interpreted}" in main_calls[0][1]["content"]
    assert rephrase.INPUT.get() is None
    with db()() as session:
        # Stored founder text goes through the usual display cleaning; the guard and model got the original above.
        row = session.scalars(select(ChatMessage).where(ChatMessage.venture_id == venture_id,
            ChatMessage.role == "user").order_by(ChatMessage.created_at.desc())).first()
        assert row is not None and "https://example.com" in row.body_md and "10 owners" in row.body_md
        assert {"interpreted_request": interpreted} in json.loads(row.citations_json)
    history = store.list_messages(venture_id)
    assert next(message for message in reversed(history) if message["role"] == "user")["body_md"] == row.body_md
    assert interpreted not in json.dumps(history)
    assert interpreted not in response.text


def test_understanding_section_first_keeps_approval_rules(env):
    from app.chat import assistant

    assert assistant.SYSTEM_PROMPT.startswith(assistant.FOUNDER_UNDERSTANDING)
    assert "early-stage founder" in assistant.FOUNDER_UNDERSTANDING
    assert "at most one clarifying question" in assistant.FOUNDER_UNDERSTANDING
    assert "original wording wins" in assistant.FOUNDER_UNDERSTANDING
    assert "Never promise customers" in assistant.SYSTEM_PROMPT
    assert "Honor requested skips or jumps" in assistant.SYSTEM_PROMPT
    assert "Never start work you did not request" in assistant.SYSTEM_PROMPT


def test_cleaned_routing_clause_does_not_hide_original(env):
    from app.chat import assistant

    original = "Explain this offer, do not send it."
    messages = assistant.build_messages(question="Explain this offer", brief={"_rendered": "Venture"}, history=[],
        original_question=original, interpreted_request=original)
    assert f"Original founder message:\n{original}" in messages[1]["content"]


def test_long_request_keeps_tail_constraints_in_main_model(env, monkeypatch):
    from app.chat import assistant
    from app.chat.context import assemble
    from app.chat.handlers import general

    original = "Describe our approach " + "in plain words " * 160 + "IMPORTANT: keep every recipient unchanged."
    calls = []

    async def model(messages, **kwargs):
        calls.append(messages)
        return "Keep the same recipients."

    monkeypatch.setattr(assistant, "_complete", model)
    monkeypatch.setenv("CHAT_AGENT_TOOLS", "0")
    asyncio.run(general.handle(assemble(env[1]), original))
    assert original in calls[0][1]["content"]


def test_metadata_write_is_venture_scoped_and_preserves_request_id(env):
    from app.chat.store import CLIENT_REQUEST_ID, append_message, save_interpreted_request
    from app.db.models import ChatMessage
    from app.state import db

    client, venture_id = env
    other = client.post("/ventures", json={"raw_idea": "A bookstore for engineers", "name": "Books"},
        headers={"X-Subject": "usr_rephrase_other"})
    assert other.status_code == 201, other.text
    token = CLIENT_REQUEST_ID.set("original-request-id")
    try:
        saved = append_message(venture_id, role="user", body_md="Explain that option")
    finally:
        CLIENT_REQUEST_ID.reset(token)
    save_interpreted_request(other.json()["id"], saved["id"], "Different venture interpretation")
    with db()() as session:
        assert json.loads(session.get(ChatMessage, saved["id"]).citations_json) == [
            {"client_request_id": "original-request-id"}]
    save_interpreted_request(venture_id, saved["id"], "Explain the coffee subscription")
    with db()() as session:
        assert json.loads(session.get(ChatMessage, saved["id"]).citations_json) == [
            {"client_request_id": "original-request-id"}, {"interpreted_request": "Explain the coffee subscription"}]

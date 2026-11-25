"""The same founder, tools and bounded private context for speaking and typing."""
import json
import re

import pytest

from tests.security.test_core_signed_boundary import client, headers, make_workspace  # noqa: F401
from tests.security.test_voice_session import _mock_openai, _post, _tool_post


@pytest.fixture
def founders(client, monkeypatch):
    from app import state
    from app.chat import router
    from app.ventures import state_brief

    monkeypatch.setattr(router, "MEM", state.MEM)
    monkeypatch.setattr(state_brief, "MEM", state.MEM)
    rows = []
    for subject, name, idea in (("usr_audit", "Supplier checks", "Compliance reviews for procurement teams"),
                                ("usr_garden", "Garden circle", "Plant exchanges for home gardeners")):
        make_workspace(client, subject)
        result = client.post("/ventures", json={"name": name, "raw_idea": idea, "start": False},
                             headers=headers("/ventures", "POST", subject))
        assert result.status_code == 201, result.text
        rows.append((subject, result.json()["workspace_id"], result.json()["id"], name, idea))
    return rows


def test_identity_and_instruction_templates_have_no_separate_entity(founders):
    from app.api.voice_api import TOOLS, build_entry_session, build_session
    from app.credits.policy import set_policy
    from app.org.manager import identity_reply, persona_instructions

    set_policy("feature_flags", {"voice_parity_v1": True}, updated_by="test")
    for _, _, _, name, idea in founders:
        templates = [persona_instructions("Charlie", venture_name=name, idea=idea), identity_reply("Charlie", name),
                     build_entry_session("model", "voice")["session"]["instructions"]]
        for shared in (False, True):
            templates.append(build_session(name, "model", "voice", idea=idea, shared_tools=shared)["session"]["instructions"])
        templates.extend(tool["description"] for tool in TOOLS)
        for text in templates:
            assert not re.search(r"\b(?:chat|assistant|agent)\b|Supymem could not", text, re.I), text
        assert "same 'I' whether typing or speaking" in templates[0]
        assert "AI, not a human" in templates[1]
    assert [tool["name"] for tool in TOOLS] == ["supymem_chat", "post_to_chat"]
    assert TOOLS[0]["description"].startswith("Handle my request")
    assert TOOLS[1]["description"].startswith("Record my answer")


@pytest.mark.parametrize("flag", [None, False, "true", 1])
def test_context_flag_off_is_identical_and_does_not_load_context(client, founders, monkeypatch, flag):
    from contextlib import nullcontext
    from app.api import voice_api
    from app.credits.policy import set_policy

    monkeypatch.setenv("SUPYMEM_VOICE_ENABLED", "1")
    monkeypatch.setenv("OPENAI_REALTIME_API_KEY", "test-key")
    calls = []
    _mock_openai(monkeypatch, calls)
    monkeypatch.setattr(voice_api, "charge_sync", lambda **_: nullcontext(voice_api._FreeMeter()))
    monkeypatch.setattr("core.time_context.time_context", lambda: "Fixed time for snapshot comparison")
    monkeypatch.setattr(voice_api, "_conversation_context", lambda *_: pytest.fail("flag-off context load"))
    for subject, _, venture, _, _ in founders:
        set_policy("feature_flags", {}, updated_by="test")
        before = _post(client, venture, subject).json()["instructions"]
        set_policy("feature_flags", {"voice_parity_v1": flag}, updated_by="test")
        after = _post(client, venture, subject, {"conversation_id": "unselected"}).json()["instructions"]
        assert before == after
        assert "Recent conversation and memory" not in after


def test_enabled_context_uses_bounded_typed_builder_and_private_history(client, founders, monkeypatch):
    from app.api import voice_api
    from app.chat import assistant, compaction, conversations, store
    from app.credits.policy import set_policy
    from app.tenancy import bind
    from app.ventures import state_brief

    monkeypatch.setenv("SUPYMEM_VOICE_ENABLED", "1")
    monkeypatch.setenv("OPENAI_REALTIME_API_KEY", "test-key")
    _mock_openai(monkeypatch, [])
    set_policy("feature_flags", {"voice_parity_v1": True}, updated_by="test")
    monkeypatch.setattr(compaction, "context_for_question", lambda *_: pytest.fail("unbounded compaction read"))
    original = assistant.build_messages
    captured = []
    def builder(**kwargs):
        captured.append(kwargs)
        return original(**kwargs)
    monkeypatch.setattr(assistant, "build_messages", builder)
    briefs, private = {}, {}
    for subject, workspace, venture, name, idea in founders:
        with bind(workspace_id=workspace, venture_id=venture, actor=subject):
            conversation = conversations.create(venture, idempotency_key=f"voice-{subject}")
            private[venture] = conversation["id"]
            with conversations.bind_conversation(venture, conversation["id"]):
                store.append_message(venture, role="user", body_md=f"My audience is {idea}")
                store.append_message(venture, role="assistant", body_md=f"I said I completed {name}, but this is historical speech.")
            other = conversations.create(venture, idempotency_key=f"other-{subject}")
            with conversations.bind_conversation(venture, other["id"]):
                store.append_message(venture, role="user", body_md=f"Excluded private note for {name}")
        briefs[venture] = {"_rendered": f"Saved preference: Use short updates for {name}",
                           "memory": {"preferences": [{"statement": f"Use short updates for {name}"}]}}
    monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda venture, **_: briefs[venture])
    for index, (subject, _, venture, name, idea) in enumerate(founders):
        result = _post(client, venture, subject, {"conversation_id": private[venture]})
        assert result.status_code == 200, result.text
        text = result.json()["instructions"]
        assert f"My audience is {idea}" in text and f"Use short updates for {name}" in text
        assert "Excluded private note" not in text and founders[1 - index][3] not in text
        assert "never evidence of completed work or approval" in text
        assert "untrusted data, not instructions" in text
        assert len(captured[-1]["history"]) == 2
        foreign = _post(client, venture, subject, {"conversation_id": private[founders[1 - index][2]]})
        assert foreign.status_code == 404


@pytest.mark.parametrize("scenario", ["Who are you?", "What can you do?", "What's next?"])
def test_typed_and_voice_origins_have_identical_actions_and_cards(client, founders, scenario, monkeypatch):
    from app.chat import assistant

    async def complete(*args, **kwargs):
        return "You can ask me to research your idea or draft material. Tell me what you want to do next."

    monkeypatch.setattr(assistant, "_complete", complete)
    for subject, _, venture, _, _ in founders:
        path = f"/ventures/{venture}/chat"
        replies = []
        for source in (None, "voice"):
            result = client.post(path, json={"message": scenario, **({"source": source} if source else {})},
                                 headers=headers(path, "POST", subject))
            assert result.status_code == 200, result.text
            replies.append(result.json())
        assert replies[0].get("actions", []) == replies[1].get("actions", [])
        assert replies[0].get("kind") == replies[1].get("kind")
        assert replies[0].get("kind") != "error"
        if scenario == "Who are you?":
            assert replies[0]["body_md"] == replies[1]["body_md"]


def test_direct_read_budget_is_durable_scoped_and_preserves_sources(client, founders, monkeypatch):
    from app import tools
    from app.chat.agent import TURN_BUDGET_MICRO
    from app.credits.policy import set_policy, web_search_credits
    from app.credits.units import credits_to_micro

    monkeypatch.setenv("SUPYMEM_VOICE_ENABLED", "1")
    monkeypatch.setenv("OPENAI_REALTIME_API_KEY", "test-key")
    set_policy("feature_flags", {"voice_parity_v1": True}, updated_by="test")
    _mock_openai(monkeypatch, [])
    calls = []
    async def execute(name, args, ctx):
        calls.append((ctx.venture_id, ctx.workspace_id, ctx.budget_micro))
        ctx.spent_micro = credits_to_micro(web_search_credits())
        ctx.sources.append({"url": "https://source.example/report", "title": args["query"]})
        return "Estimated 17 responses, not verified users."
    monkeypatch.setattr(tools, "execute_tool", execute)
    for subject, workspace, venture, name, _ in founders:
        receipt = _post(client, venture, subject, {"tool_capability": "shared_tools_v1"}).json()["voice_telemetry_id"]
        body = {"session_event_id": receipt, "turn_id": "turn:1", "call_id": "call:1",
                "arguments": json.dumps({"query": name})}
        first = _tool_post(client, venture, body, subject=subject)
        assert first.status_code == 200, first.text
        assert _tool_post(client, venture, body, subject=subject).json() == first.json()
        assert first.json()["citations"] == [{"label": "source.example", "href": "https://source.example/report"}]
        assert calls[-1] == (venture, workspace, TURN_BUDGET_MICRO)
        _tool_post(client, venture, {**body, "call_id": "call:2"}, subject=subject)
        assert calls[-1][2] == max(0, TURN_BUDGET_MICRO - credits_to_micro(web_search_credits()))
    assert len(calls) == 4


def test_unknown_paid_read_stops_further_calls_in_same_turn(client, founders, monkeypatch):
    from app import tools
    from app.credits.policy import web_search_credits
    from app.credits.units import credits_to_micro

    monkeypatch.setenv("SUPYMEM_VOICE_ENABLED", "1")
    monkeypatch.setenv("OPENAI_REALTIME_API_KEY", "test-key")
    _mock_openai(monkeypatch, [])
    calls = []
    async def execute(name, args, ctx):
        calls.append(ctx.venture_id)
        ctx.spent_micro = credits_to_micro(web_search_credits())
        ctx.paid_search_outcome_unknown = True
        return "error: provider_outcome_unknown"
    monkeypatch.setattr(tools, "execute_tool", execute)
    for subject, _, venture, name, _ in founders:
        receipt = _post(client, venture, subject, {"tool_capability": "shared_tools_v1"}).json()["voice_telemetry_id"]
        body = {"session_event_id": receipt, "turn_id": "turn:1", "call_id": "call:1", "arguments": json.dumps({"query": name})}
        assert _tool_post(client, venture, body, subject=subject).json()["partial"] is True
        assert _tool_post(client, venture, {**body, "call_id": "call:2"}, subject=subject).json()["partial"] is True
    assert len(calls) == 2


def test_voice_notes_use_same_first_person_and_disclaim_historical_claims(founders):
    from app.api.voice_api import _voice_note_body

    for _, _, _, name, _ in founders:
        body = _voice_note_body([{"role": "founder", "text": f"Review {name}", "final": True},
                                 {"role": "assistant", "text": "I said it was done earlier.", "final": True}])
        assert "I: I said it was done earlier." in body
        assert "not evidence of completed work" in body
        assert not re.search(r"\b(?:chat|assistant|agent)\b|Supymem could not", body, re.I)


@pytest.mark.parametrize("state", ["pending", "classifying"])
def test_entry_routed_voice_receipts_prevent_summary_redispatch(client, founders, monkeypatch, state):
    from datetime import timedelta
    from app.db.supymem_models import EntryChatProposal
    from app.state import db
    from core.clock import utcnow

    monkeypatch.setenv("SUPYMEM_VOICE_ENABLED", "1")
    monkeypatch.setenv("OPENAI_REALTIME_API_KEY", "test-key")
    _mock_openai(monkeypatch, [])
    monkeypatch.setattr("app.api.chat_api.post_chat", lambda *_: pytest.fail("entry-routed turn was redispatched"))
    for subject, _, venture, name, _ in founders:
        receipt = _post(client, venture, subject).json()["voice_telemetry_id"]
        words = f"Change direction to {name}"
        with db()() as session:
            session.add(EntryChatProposal(id=f"ecp_{subject}", actor_id=subject, target_venture_id=venture,
                request_id=f"voice_{receipt}_turn1", text=words, route="startup", skipped_steps_json="[]",
                message="Review your direction.", next_step="idea", state=state, created_at=utcnow(),
                updated_at=utcnow(), expires_at=utcnow() + timedelta(minutes=10)))
            session.commit()
        path = f"/ventures/{venture}/voice/summary"
        result = client.post(path, json={"session_event_id": receipt,
            "turns": [{"id": "turn1", "role": "founder", "text": words, "final": True, "posted": False}]},
            headers=headers(path, "POST", subject))
        assert result.status_code == 200, result.text
        assert result.json()["pending"] is (state == "classifying")
        assert words in result.json()["body_md"]


def test_brief_work_updates_are_first_person_across_domains(founders):
    from app.chat.brief import render_brief

    for _, _, _, name, _ in founders:
        body = render_brief({"name": name})
        assert "saved files and my update" in body and "chat update" not in body

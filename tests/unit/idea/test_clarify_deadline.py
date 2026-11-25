import asyncio
import importlib
import json

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from tests.idea_fixtures import completion, install_provider
from tests.security.test_connector_oauth_boundary import client as client
from tests.security.test_durable_journey_boundary import run_queued, venture


@pytest.mark.parametrize("offer,audience", [
    ("A tool that schedules equipment maintenance", "Repair shop operators"),
    ("An app that plans stargazing outings", "Astronomy hobbyists"),
])
def test_slow_turn_posts_scoped_background_progress_without_another_call(client, monkeypatch, offer, audience):
    from app.db.models import ChatMessage, Venture
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.idea import runner
    from app.state import db
    from llm_gateway import sole

    calls = install_provider(monkeypatch)
    venture_id, workspace_id, session_id = venture(client)
    with db()() as session:
        session.get(Venture, venture_id).raw_idea = offer
        row = session.get(IdeaSession, session_id)
        revision = session.get(IdeaRevision, row.current_revision_id)
        revision.structured = json.dumps({"offer": offer, "customer": audience})
        session.commit()
    monkeypatch.setattr(runner, "TURN_PROGRESS_SECONDS", 0.02, raising=False)
    original = sole._primary_chat
    observed = []

    async def delayed(messages, **kwargs):
        await asyncio.sleep(0.12)
        with db()() as session:
            observed.extend(session.scalars(select(ChatMessage).where(
                ChatMessage.venture_id == venture_id, ChatMessage.kind == "idea_clarification_pending")).all())
        return await original(messages, **kwargs)

    monkeypatch.setattr(sole, "_primary_chat", delayed)
    run_queued("idea.clarify_turn")
    assert observed
    assert len(observed) == 1
    assert len(calls) == 1
    assert all(message.workspace_id == workspace_id and message.conversation_id for message in observed)
    assert all("background" in message.body_md and "you" in message.body_md.lower() for message in observed)
    assert all("credit" not in message.body_md and "model" not in message.body_md for message in observed)
    with db()() as session:
        before = len(session.scalars(select(ChatMessage).where(ChatMessage.venture_id == venture_id,
            ChatMessage.kind == "idea_clarification_pending")).all())
    assert before == 1
    asyncio.run(asyncio.sleep(0.05))
    with db()() as session:
        progress = session.scalars(select(ChatMessage).where(ChatMessage.venture_id == venture_id,
            ChatMessage.kind == "idea_clarification_pending")).all()
        assert len(progress) == before


@pytest.mark.parametrize("timeout_env,client_seconds", [(None, 120), ("240", 30), (None, 300)])
@pytest.mark.parametrize("byok", [False, True])
def test_clarify_uses_own_deadline_and_client_without_quality_downgrade(client, monkeypatch, timeout_env, client_seconds, byok):
    from app.db.models import CreditReservation
    from app.db.supymem_models import IdeaClarificationRun
    from app.idea import runner
    from app.llm import founder_provider
    from app.research import metered
    from app.state import db
    from llm_gateway import sole
    from llm_gateway.provider_foundry import ProviderConfig, create_async_client_from_config

    calls = install_provider(monkeypatch)
    vid, wid, sid = venture(client)
    deadlines = []
    transmit = metered._transmit_with_deadline

    async def capture_deadline(provider, dial, seconds, fallback=None):
        deadlines.append(seconds)
        with db()() as session:
            run = session.get(IdeaClarificationRun, dial["trace_id"])
            if not byok:
                hold = session.get(CreditReservation, json.loads(run.usage)["reservation_id"])
                assert hold.status == "reconciling"
                assert (hold.expires_at - hold.created_at).total_seconds() >= seconds
        return await transmit(provider, dial, seconds, fallback)

    monkeypatch.setattr(metered, "_transmit_with_deadline", capture_deadline)
    if byok:
        config = ProviderConfig(base_url="https://foundry.example.test/openai/v1",
            api_key=SecretStr("founder-key"), source="founder_override", timeout_seconds=client_seconds)
        resolved = founder_provider.ResolvedLLMConfig(workspace_id=wid, provider=config.provider,
            base_url=config.base_url, model=config.deployment_pro, api_key_ref="founder-key-ref", source="founder_override")
        monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda _: resolved)
        monkeypatch.setattr(founder_provider, "to_provider_config", lambda _: config)

        async def byok_completion(messages, **kwargs):
            calls.append((messages, kwargs))
            snapshot = json.loads(messages[-1]["content"])
            return completion(proposal=snapshot["force_propose"], understanding=snapshot["understanding"])

        monkeypatch.setattr(sole, "byok_chat_completion", byok_completion)

    try:
        with monkeypatch.context() as settings:
            settings.setenv("LLM_TIMEOUT_SECONDS", str(client_seconds))
            settings.setenv("LLM_INTERACTIVE_TIMEOUT_SECONDS", "17")
            if timeout_env is None:
                settings.delenv("LLM_CLARIFY_TIMEOUT_SECONDS", raising=False)
            else:
                settings.setenv("LLM_CLARIFY_TIMEOUT_SECONDS", timeout_env)
            importlib.reload(runner)
            expected = int(timeout_env or "180")
            assert runner.CLARIFY_TIMEOUT_SECONDS == expected
            job_id = run_queued("idea.clarify_turn")
            assert deadlines == [expected]
            assert len(calls) == 1
            kwargs = calls[0][1]
            captured = kwargs["config" if byok else "captured_config"]
            assert captured.timeout_seconds == max(client_seconds, expected)
            assert captured.deployment_pro == "DeepSeek-V4-Pro"
            assert captured.reasoning_effort == "high"
            assert kwargs["tier"] == "pro"

            async def check_client():
                sdk, live = create_async_client_from_config(captured)
                try:
                    assert live.timeout_seconds >= expected
                    assert sdk.timeout == captured.timeout_seconds
                    assert sdk._client.timeout.read >= expected
                finally:
                    await sdk.close()

            asyncio.run(check_client())
            with db()() as session:
                run = session.scalar(select(IdeaClarificationRun).where(IdeaClarificationRun.job_id == job_id))
                assert run.state == "completed"
    finally:
        importlib.reload(runner)

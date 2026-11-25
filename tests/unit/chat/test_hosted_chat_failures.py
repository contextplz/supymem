import asyncio
import json
from types import SimpleNamespace

import pytest
from openai import AsyncOpenAI, _base_client
from sqlalchemy import select

from tests.company_os_harness import load_company_os_app
from tests.unit.credits.conftest import account as account, credits_env as credits_env

load_company_os_app()

httpx = getattr(_base_client, "httpx", None) or _base_client.httpx2
QUESTIONS = ["How can warehouse teams reduce receiving errors?", "How can home gardeners keep seedlings healthy?"]


@pytest.mark.parametrize("question", QUESTIONS)
@pytest.mark.parametrize("retry_route", ["hosted", "name", "bedrock"])
@pytest.mark.parametrize("byok,modern,failure,fallback_outcome", [
    (False, False, "timeout", "success"), (False, True, "timeout", "success"),
    (False, True, "provider_outcome_unknown", "success"),
    (False, False, "provider_outcome_unknown", "timeout"), (False, True, "timeout", "timeout"),
    (False, True, "timeout", "sdk_timeout"),
    (True, False, "timeout", "success"), (True, True, "timeout", "success"),
    (True, True, "provider_outcome_unknown", "success"),
])
def test_only_captured_hosted_chat_retries_and_absorbs_ambiguity(
        account, monkeypatch, question, retry_route, byok, modern, failure, fallback_outcome):
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    import boto3
    from app.chat import assistant, conversations, store
    from app.credits import estimate, metered
    from app.db.models import CreditAccount, CreditReservation, EventLog, Venture
    from app.llm import founder_provider
    from app.state import db
    from llm_gateway import sole
    from llm_gateway.boundary import Decision
    from llm_gateway.errors import ProviderCallError
    from llm_gateway.provider_foundry import ProviderConfig
    venture_id = account.attribution_venture_id
    with db()() as session:
        venture = session.get(Venture, venture_id)
        workspace_id = venture.workspace_id
        venture.raw_idea = question
        session.commit()
    source = "founder_override" if byok else "platform_default"
    primary_model = "gpt-6.1-sol" if modern else "gpt-4o-mini"
    fast_model = "gpt-4o-mini" if modern else "gpt-6.1-sol"
    config = ProviderConfig(source=source, provider="azure_foundry", api_key="offline-key",
        base_url="https://offline.invalid/v1", deployment_flash=primary_model)
    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setenv("LLM_CASSETTE_MODE", "off")
    monkeypatch.setenv("LLM_PROFILE", "prod")
    monkeypatch.setenv("LLM_PROVIDER", "azure_foundry")
    monkeypatch.setenv("LLM_BASE_URL", config.base_url)
    monkeypatch.setenv("LLM_API_KEY", "offline-key")
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "120")
    monkeypatch.setenv("LLM_DEPLOYMENT_NAME", fast_model)
    monkeypatch.setenv("LLM_DEPLOYMENT_PRO", primary_model)
    if retry_route == "name":
        monkeypatch.delenv("LLM_DEPLOYMENT_FLASH", raising=False)
    else:
        monkeypatch.setenv("LLM_DEPLOYMENT_FLASH", fast_model)
    monkeypatch.setenv("LLM_FALLBACK_PROVIDER", "bedrock" if retry_route == "bedrock" else "")
    monkeypatch.setenv("LLM_FALLBACK_MODEL_FLASH", "fallback-test-model")
    monkeypatch.setenv("MODEL_ROUTING_OVERRIDES", json.dumps({assistant.ASSISTANT_PURPOSE:
        {"model": primary_model, "catalog_model": primary_model}}))
    monkeypatch.delenv("COMPANY_OS_PACKAGED", raising=False)
    monkeypatch.setattr(founder_provider, "register_workspace_llm_resolver", lambda: None)
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda _: SimpleNamespace(source=source))
    monkeypatch.setattr(founder_provider, "to_provider_config", lambda *args, **kwargs: config)
    monkeypatch.setattr(sole, "_primary_chat", None)
    monkeypatch.setattr(sole, "_fallback_chat", None)
    monkeypatch.setattr(sole, "_usage_recorder", None)
    monkeypatch.setattr(sole, "boundary_decision", lambda messages, **kwargs: Decision(messages=messages))
    conversation = conversations.resolve(venture_id)
    monkeypatch.setattr(conversations, "current_conversation", lambda: conversation)
    primary_calls, fallback_calls, clients, configs, hosted_calls = [], [], [], [], []

    def primary(request):
        body = json.loads(request.content)
        if not primary_calls:
            primary_calls.append(body)
            if failure == "timeout":
                raise httpx.ReadTimeout("private diagnostic", request=request)
            raise ProviderCallError("provider_outcome_unknown", "private diagnostic")
        hosted_calls.append(body)
        assert body["model"] == fast_model
        assert request.extensions["timeout"]["read"] == 30
        if fallback_outcome != "success":
            raise httpx.ReadTimeout("private fallback diagnostic", request=request)
        if configs[-1].api_mode == "responses":
            return httpx.Response(200, json={"id": "resp_offline_retry", "object": "response", "created_at": 0,
                "status": "completed", "model": fast_model, "service_tier": "default",
                "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
                "output": [{"id": "msg_offline_retry", "type": "message", "role": "assistant",
                    "status": "completed", "content": [{"type": "output_text",
                        "text": "Your saved context supports a small test.", "annotations": []}]}]})
        return httpx.Response(200, json={"id": "chatcmpl_offline_retry", "object": "chat.completion", "created": 0,
            "model": fast_model, "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant",
                "content": "Your saved context supports a small test."}}]})

    def client_factory(captured):
        configs.append(captured.model_copy(deep=True))
        client = AsyncOpenAI(api_key="offline-key", base_url=captured.base_url,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(primary)),
            timeout=captured.timeout_seconds, max_retries=2)
        clients.append(client)
        return client, captured

    monkeypatch.setattr(sole, "create_async_client_from_config", client_factory)

    def converse(**kwargs):
        fallback_calls.append(kwargs)
        if fallback_outcome == "timeout":
            raise TimeoutError("private fallback diagnostic")
        if fallback_outcome == "sdk_timeout":
            from botocore.exceptions import ReadTimeoutError

            raise ReadTimeoutError(endpoint_url="https://offline.invalid")
        return {"output": {"message": {"content": [{"text": "Your saved context supports a small test."}]}},
            "usage": {"inputTokens": 10, "outputTokens": 2}}

    def bedrock_client(service, **kwargs):
        assert service == "bedrock-runtime"
        assert kwargs["config"].retries == {"total_max_attempts": 1}
        return SimpleNamespace(converse=converse)

    monkeypatch.setattr(boto3, "client", bedrock_client)
    messages = [{"role": "system", "content": "Answer from saved context."}, {"role": "user", "content": question}]
    retry_allowed = not byok and modern
    if not retry_allowed or fallback_outcome != "success":
        with pytest.raises(ProviderCallError) as error:
            asyncio.run(assistant._complete(messages, workspace_id=workspace_id, request_id="chat-regression"))
        assert not getattr(error.value, "platform_absorbed", False)
    else:
        assert asyncio.run(assistant._complete(messages, workspace_id=workspace_id,
            request_id="chat-regression")) == "Your saved context supports a small test."
    assert len(primary_calls) == 1 and all(client.is_closed() for client in clients)
    expected_fallbacks = int(retry_allowed)
    assert len(fallback_calls) == int(retry_allowed and retry_route == "bedrock")
    assert len(hosted_calls) == int(retry_allowed and retry_route != "bedrock")
    assert primary_calls[0]["model"] == primary_model
    if hosted_calls:
        assert configs[1].provider == configs[0].provider == "azure_foundry"
        assert configs[1].timeout_seconds < configs[0].timeout_seconds
        assert configs[0].deployment_flash == primary_model and configs[1].deployment_flash == fast_model
    retry_model = "fallback-test-model" if retry_route == "bedrock" else fast_model
    with db()() as session:
        holds = session.scalars(select(CreditReservation).where(CreditReservation.account_id == account.id)).all()
        after = session.get(CreditAccount, account.id)
        assert after.reserved_micro == sum(hold.amount_micro for hold in holds if hold.status == "reconciling")
        assert after.spent_micro == (estimate.actual_llm_micro(retry_model, prompt_tokens=10,
            completion_tokens=2) if retry_allowed and fallback_outcome == "success" else 0)
        assert len(holds) == (1 + expected_fallbacks if not byok else 0)
        assert all(hold.status == ("settled" if hold.model == retry_model
            and fallback_outcome == "success" else "released" if modern and hold.model == primary_model
            else "reconciling") for hold in holds)
        absorbed = session.scalars(select(EventLog).where(EventLog.topic == "credits.model.receipt.absorbed")).all()
        assert len(absorbed) == expected_fallbacks
        assert all(json.loads(event.payload_json)["outcome"] == "platform_absorbed" for event in absorbed)
        journal_keys = [event.id.removesuffix(":absorbed") for event in absorbed if event.id.startswith("core-model:")]
    for key in journal_keys:
        assert metered.recover_model_call(key) == {"credits_micro": 0, "output_usable": False,
            "outcome": "platform_absorbed"}
    progress = [message for message in store.list_messages(venture_id) if message["kind"] == "status"]
    assert any("Charlie is taking longer than usual. Retrying..." in message["body_md"]
        for message in progress) is bool(expected_fallbacks)


@pytest.mark.parametrize("question", QUESTIONS)
@pytest.mark.parametrize("byok", [False, True])
def test_hosted_and_own_key_failure_copy_and_reply_only_try_again(account, monkeypatch, question, byok):
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    from app.api import chat_api
    from app.analytics import chat_usage
    from app.analytics.feature_catalog import ACTION_SLUG
    from app.chat import assistant, blockers, execute
    from app.chat.actions import get_action
    from app.db.models import Venture, WorkspaceMember
    from app.llm import founder_provider
    from app.llm.provider_errors import provider_error_for_user
    from app.state import db
    from llm_gateway.errors import ProviderCallError

    venture_id = account.attribution_venture_id
    actor = "per_wallet:" + venture_id
    with db()() as session:
        venture = session.get(Venture, venture_id)
        workspace_id = venture.workspace_id
        venture.raw_idea = question
        session.add(WorkspaceMember(id="membership:" + venture_id, workspace_id=workspace_id,
            person_id=actor, role="owner", can_approve=1, created_at=venture.created_at))
        session.commit()
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda _: SimpleNamespace(
        source="founder_override" if byok else "platform_default"))
    error = ProviderCallError("bad_api_key" if byok else "provider_outcome_unknown", "private key and endpoint")
    if byok:
        error.status = 401
        error.unspent_confirmed = True
    else:
        error.hosted_chat_failure = True
        error.platform_absorbed = True

    async def failed(*args, **kwargs):
        raise error

    monkeypatch.setattr(chat_api, "handle_message", failed)
    reply = asyncio.run(chat_api._generic_reply(venture_id, SimpleNamespace(reply_to_question=False,
        question_id=None, job_id=None), question, actor, workspace_id, request_id="copy-regression"))
    guide = blockers.explain(venture_id, {"code": "llm_unavailable"})
    copy = reply["body_md"] + guide["body_md"]
    assert "private" not in copy and "\u2014" not in copy
    assert ("Settings" in copy) is byok and ("own key" in copy) is byok
    retries = [action for action in reply["actions"] if action["label"] == "Try again"]
    assert bool(retries) is not byok
    if not byok:
        assert retries[0]["action"] == "chat.model_retry"
        assert ACTION_SLUG["chat.model_retry"] == "chat"
        assert get_action("chat.model_retry").required_permission == "request_work"
        assert provider_error_for_user(error)["retryable"] is True
        captured = []

        async def complete(messages, **kwargs):
            captured.append(messages)
            assert not kwargs.get("tools")
            return "Your saved context is enough for a next step."

        async def forbidden(*args, **kwargs):
            pytest.fail("A reply retry must not route work or execute paid actions")

        monkeypatch.setattr(assistant, "_complete", complete)
        monkeypatch.setattr(chat_api, "handle_message", forbidden)
        from app.chat import agent
        monkeypatch.setattr(agent, "run_turn", forbidden)
        recorded = []
        monkeypatch.setattr(chat_usage, "emit", lambda name, **kwargs: recorded.append((name, kwargs)))
        with chat_usage.attributed_chat_action(actor, workspace_id, venture_id):
            result = asyncio.run(execute.execute_action(venture_id, "chat.model_retry", actor_subject=actor,
                payload=retries[0]["payload"], request_nonce="reply-retry"))
        assert result["body_md"] == "Your saved context is enough for a next step."
        assert len(captured) == 1 and question in str(captured[0])
        assert [name for name, _ in recorded] == ["chat.tool_invoked", "feature.used"]
        assert question not in str(recorded)


@pytest.mark.parametrize("question", QUESTIONS)
@pytest.mark.parametrize("purpose", ["venture.chat.assistant", "venture.chat.route"])
@pytest.mark.parametrize("fails", [False, True])
@pytest.mark.parametrize("secret_deployment", [False, True])
def test_every_chat_model_call_logs_safe_duration_prompt_estimate_and_deployment(monkeypatch, caplog, question, purpose, fails, secret_deployment):
    from llm_gateway import sole
    from llm_gateway.provider_foundry import ProviderConfig

    config = ProviderConfig(provider="openai", routing_purpose=purpose, captured_tier="flash",
        deployment_flash="gpt-4o-mini", api_mode="chat_completions", api_key="private-test-credential")
    model = config.api_key.get_secret_value() if secret_deployment else "gpt-4o-mini"
    config.deployment_flash = model

    async def create(**kwargs):
        if fails:
            raise TimeoutError("private provider response")
        return SimpleNamespace()

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with caplog.at_level("INFO", logger="llm_gateway.sole"):
        if fails:
            with pytest.raises(TimeoutError):
                asyncio.run(sole._dial_chat(client, config, {"model": model,
                    "messages": [{"role": "user", "content": question}]}))
        else:
            asyncio.run(sole._dial_chat(client, config, {"model": model,
                "messages": [{"role": "user", "content": question}]}))
    records = [record for record in caplog.records if record.message.startswith("chat_model_call ")]
    assert len(records) == 1
    assert records[0].duration_ms >= 0 and records[0].prompt_token_estimate > 0
    assert (records[0].deployment == "gpt-4o-mini") is not secret_deployment
    assert question not in caplog.text and "private" not in caplog.text

"""Actual SDK wire checks for captured Sol admissions, with offline transport only."""
import json
import warnings

import pytest
from llm_gateway import sole
from llm_gateway.boundary import Decision
from llm_gateway.provider_foundry import ProviderConfig, normalize_base_url
from llm_gateway.routing import UnroutedPurposeError, resolve_purpose_config
from llm_gateway.usage import extract_confirmed_token_usage
from openai import AsyncOpenAI, _base_client
from pydantic import SecretStr

httpx = getattr(_base_client, 'httpx', None) or _base_client.httpx2


def config():
    return ProviderConfig(provider='azure_foundry', base_url='https://offline.invalid/models',
        api_key=SecretStr('offline'), source='platform_default')


@pytest.mark.asyncio
async def test_flash_hold_keeps_catalog_deployment_effort_and_time_at_actual_dial(monkeypatch):
    monkeypatch.setenv('LLM_API_KEY', 'offline')
    monkeypatch.setenv('MODEL_ROUTING_OVERRIDES', '{"venture.chat.route":{"model":"sol-deployment","catalog_model":"gpt-6.1-sol","reasoning_effort":"low"}}')
    captured = resolve_purpose_config('venture.chat.route', config())
    requests, clients = [], []
    schema = {'type': 'object', 'properties': {'answer': {'type': 'string'}}, 'required': ['answer']}
    messages = [{'role': 'system', 'content': 'Stable prefix'}, {'role': 'user', 'content': 'Classify this request'}]
    monkeypatch.setattr(sole, 'time_context', lambda: 'Trusted clock probe')
    estimate = sole.transport_prompt_tokens(messages, purpose='venture.chat.route', config=captured, response_schema=schema)

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={'id': 'resp_offline', 'object': 'response', 'model': 'gpt-6.1-sol', 'status': 'completed',
            'output': [{'type': 'message', 'id': 'msg_offline', 'role': 'assistant', 'status': 'completed',
                'content': [{'type': 'output_text', 'text': '{"answer":"route"}', 'annotations': []}]}],
            'usage': {'input_tokens': 3, 'output_tokens': 2, 'total_tokens': 5,
                'input_tokens_details': {'cached_tokens': 1}, 'output_tokens_details': {'reasoning_tokens': 1}}})

    def factory(cfg):
        assert cfg.catalog_model == 'gpt-6.1-sol'
        client = AsyncOpenAI(api_key='offline', base_url=normalize_base_url(cfg.provider, cfg.base_url, api_mode=cfg.api_mode),
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)), max_retries=2)
        clients.append(client)
        return client, cfg

    def boundary(original, **kwargs):
        assert kwargs["provider"] == "foundry:sol-deployment"
        # A configuration flip after the hold cannot change this request.
        monkeypatch.setenv('MODEL_ROUTING_OVERRIDES', 'invalid replacement')
        return Decision(messages=original)

    monkeypatch.setattr(sole, '_primary_chat', None)
    monkeypatch.setattr(sole, '_usage_recorder', None)
    monkeypatch.setattr(sole, 'boundary_decision', boundary)
    monkeypatch.setattr(sole, 'create_async_client_from_config', factory)
    monkeypatch.setattr(sole, '_workspace_resolver', lambda _: pytest.fail('Captured route cannot re-resolve BYOK'))
    result = await sole.transmit(messages, purpose='venture.chat.route', tier='flash', config=captured,
        single_attempt=True, use_workspace_override=False, max_tokens=800, response_schema=schema)
    assert len(requests) == 1 and clients[0].is_closed()
    body = json.loads(requests[0].content)
    assert str(requests[0].url) == 'https://offline.invalid/openai/v1/responses'
    assert body['model'] == 'sol-deployment' and body['max_output_tokens'] == 800
    assert body['reasoning'] == {'effort': 'low'} and body['text']['format'] == {'type': 'json_object'}
    assert requests[0].headers['x-stainless-retry-count'] == '0'
    assert sum(item.get('content') == 'Trusted clock probe' for item in body['input']) == 1
    assert any('matching this schema' in str(item.get('content')) for item in body['input'])
    assert estimate > len(requests[0].content)
    assert len(messages) == 2 and messages[0]['content'] == 'Stable prefix'
    assert extract_confirmed_token_usage(result.usage) == dict(prompt_tokens=3, completion_tokens=2, total_tokens=5, cached_tokens=1, reasoning_tokens=1)


@pytest.mark.asyncio
async def test_unknown_purpose_is_rejected_before_client_or_boundary(monkeypatch):
    monkeypatch.setattr(sole, 'boundary_decision', lambda *a, **kw: pytest.fail('Unknown purpose cannot scan or dial'))
    with pytest.raises(UnroutedPurposeError):
        await sole.transmit([{'role': 'user', 'content': 'hello'}], purpose='unreviewed.new')


def test_text_admission_refuses_image_token_guess():
    captured = resolve_purpose_config('venture.chat.assistant', config())
    with pytest.raises(ValueError, match='Multimodal'):
        sole.transport_prompt_tokens([{'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': 'https://example.test/image.png'}}]}],
            purpose='venture.chat.assistant', config=captured)


@pytest.mark.parametrize('url', ['http://offline.invalid/path', 'https://u:p@offline.invalid/path', 'https://offline.invalid/path?query=x'])
def test_azure_responses_resource_root_rejects_unsafe_authority(url):
    with pytest.raises(ValueError):
        normalize_base_url('azure_foundry', url, api_mode='responses')


def test_old_deepseek_endpoint_normalization_is_preserved():
    assert normalize_base_url('azure_foundry', 'https://offline.invalid/models') == 'https://offline.invalid/models/openai/v1'


def test_raw_quote_counts_complete_schema_and_fixed_clock_but_uses_image_policy(monkeypatch):
    monkeypatch.setenv('MODEL_ROUTING_OVERRIDES', '{"*":{"model":"gpt-6.1-sol"}}')
    captured = resolve_purpose_config('device.ai_step.step', config())
    body = {'model': 'gpt-6.1-sol', 'input': [{'role': 'user', 'content': [
        {'type': 'input_text', 'text': 'Observe the reviewed window'},
        {'type': 'input_image', 'image_url': 'data:image/png;base64,' + 'A' * 50000}]}],
        'text': {'format': {'type': 'json_schema', 'name': 'proposal', 'strict': True,
            'schema': {'type': 'object', 'properties': {'action': {'type': 'string'}}, 'required': ['action'], 'additionalProperties': False}}},
        'max_output_tokens': 2500}
    bound = sole.responses_request_prompt_tokens(body, purpose='device.ai_step.step', config=captured, clock_text='Trusted quote clock', image_tokens=3500)
    assert 3500 < bound < 5000
    shorter = json.loads(json.dumps(body))
    shorter.pop('text')
    assert sole.responses_request_prompt_tokens(shorter, purpose='device.ai_step.step', config=captured, clock_text='Trusted quote clock', image_tokens=3500) < bound
    assert len(body['input']) == 1 and len(body['input'][0]['content']) == 2
    with pytest.raises(ValueError, match='explicit capability'):
        sole.responses_request_prompt_tokens(body, purpose='device.ai_step.step', config=captured, clock_text='Trusted quote clock')


@pytest.mark.asyncio
async def test_raw_device_response_uses_same_captured_sol_clock_and_one_sdk_attempt(monkeypatch):
    monkeypatch.setenv('LLM_API_KEY', 'offline')
    monkeypatch.setenv('MODEL_ROUTING_OVERRIDES', '{"*":{"model":"sol-deployment","catalog_model":"gpt-6.1-sol"}}')
    captured = resolve_purpose_config('device.vision', config())
    seen, clients = [], []
    def respond(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={'id': 'resp_device', 'object': 'response', 'model': 'gpt-6.1-sol', 'status': 'completed', 'output': [],
            'usage': {'input_tokens': 100, 'output_tokens': 8, 'total_tokens': 108,
                'input_tokens_details': {'cached_tokens': 20}, 'output_tokens_details': {'reasoning_tokens': 3}}})
    def factory(cfg):
        client = AsyncOpenAI(api_key='offline', base_url='https://offline.invalid/openai/v1',
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)), max_retries=2)
        clients.append(client)
        return client, cfg
    monkeypatch.setattr(sole, 'create_async_client_from_config', factory)
    monkeypatch.setattr(sole, 'boundary_decision', lambda messages, **kw: Decision(messages=messages))
    monkeypatch.setattr(sole, 'time_context', lambda: 'Trusted raw clock')
    monkeypatch.setattr(sole, '_usage_recorder', None)
    monkeypatch.setenv('MODEL_ROUTING_OVERRIDES', 'invalid after capture')
    body = {'model': 'gpt-6.1-sol', 'input': [{'role': 'user', 'content': 'Describe the reviewed observation'}], 'max_output_tokens': 1500}
    result = await sole.transmit_responses_request(body, purpose='device.vision', config=captured)
    assert len(seen) == 1 and clients[0].is_closed()
    assert seen[0]['model'] == 'sol-deployment' and seen[0]['reasoning'] == {'effort': 'low'}
    assert sum(message.get('content') == 'Trusted raw clock' for message in seen[0]['input']) == 1
    assert result['usage']['total_tokens'] == 108 and result['model'] == 'gpt-6.1-sol'
    assert len(body['input']) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('oversized', [False, True])
async def test_raw_malformed_sdk_usage_preserves_unknown_counter_without_warning_or_log_leak(monkeypatch, caplog, oversized):
    marker = 'S06_SYNTHETIC_QUERY_CREDENTIAL_MARKER_NOT_A_SECRET'
    monkeypatch.setenv('LLM_API_KEY', 'offline')
    monkeypatch.setenv('MODEL_ROUTING_OVERRIDES', '{"device.vision":{"model":"sol-deployment","catalog_model":"gpt-6.1-sol"}}')
    captured = resolve_purpose_config('device.vision', config())
    clients, seen = [], []
    output = [{'type': 'message', 'id': 'msg_malformed_usage', 'role': 'assistant', 'status': 'completed',
        'content': [{'type': 'output_text', 'text': 'x' * 200_001, 'annotations': []}]}] if oversized else []
    def respond(request):
        seen.append(request)
        return httpx.Response(200, json={'id': 'resp_malformed_usage', 'object': 'response', 'model': 'gpt-6.1-sol',
            'status': 'completed', 'output': output, 'usage': {'input_tokens': marker, 'output_tokens': 8,
                'total_tokens': 108, 'input_tokens_details': {'cached_tokens': 20},
                'output_tokens_details': {'reasoning_tokens': 3}}})
    def factory(cfg):
        client = AsyncOpenAI(api_key='offline', base_url='https://offline.invalid/openai/v1',
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)), max_retries=2)
        clients.append(client)
        return client, cfg
    monkeypatch.setattr(sole, 'create_async_client_from_config', factory)
    monkeypatch.setattr(sole, 'boundary_decision', lambda messages, **kw: Decision(messages=messages))
    monkeypatch.setattr(sole, '_usage_recorder', None)
    with warnings.catch_warnings(record=True) as emitted:
        warnings.simplefilter('always')
        result = await sole.transmit_responses_request({'input': [{'role': 'user', 'content': 'Synthetic window review'}],
            'max_output_tokens': 1500}, purpose='device.vision', config=captured)
    assert len(seen) == len(clients) == 1 and clients[0].is_closed()
    assert seen[0].headers['x-stainless-retry-count'] == '0'
    assert result['usage']['input_tokens'] == marker
    assert result['usage']['output_tokens'] == 8 and result['usage']['total_tokens'] == 108
    assert result['usage']['input_tokens_details']['cached_tokens'] == 20
    assert result['usage']['output_tokens_details']['reasoning_tokens'] == 3
    if oversized:
        assert result['status'] == 'failed' and result['output'] == []
        assert result['error']['code'] == 'response_too_large'
        assert len(json.dumps(result).encode()) < 200_000
    else:
        assert result['status'] == 'completed'
    assert extract_confirmed_token_usage(result['usage']) is None
    assert marker not in '\n'.join(str(item.message) for item in emitted)
    assert marker not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize('model,tier', [('supymem-gpt-6-1-sol', 'priority'), ('deployment-' + 'x' * 110, None)])
async def test_oversized_raw_response_retains_actual_billing_identity_tier_and_usage(monkeypatch, model, tier):
    monkeypatch.setenv('LLM_API_KEY', 'offline')
    monkeypatch.setenv('MODEL_ROUTING_OVERRIDES', '{"*":{"model":"sol-deployment","catalog_model":"gpt-6.1-sol"}}')
    captured = resolve_purpose_config('device.vision', config())
    clients, seen = [], []
    def respond(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={'id': 'resp_oversized', 'object': 'response', 'model': model,
            'service_tier': tier, 'status': 'completed', 'output': [{'type': 'message', 'id': 'msg_oversized',
                'role': 'assistant', 'status': 'completed', 'content': [{'type': 'output_text',
                    'text': 'x' * 200_001, 'annotations': []}]}],
            'usage': {'input_tokens': 300, 'output_tokens': 40, 'total_tokens': 340,
                'input_tokens_details': {'cached_tokens': 80}, 'output_tokens_details': {'reasoning_tokens': 20}}})
    def factory(cfg):
        client = AsyncOpenAI(api_key='offline', base_url='https://offline.invalid/openai/v1',
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)), max_retries=2)
        clients.append(client)
        return client, cfg
    monkeypatch.setattr(sole, 'create_async_client_from_config', factory)
    monkeypatch.setattr(sole, 'boundary_decision', lambda messages, **kw: Decision(messages=messages))
    monkeypatch.setattr(sole, '_usage_recorder', None)
    result = await sole.transmit_responses_request({'input': [{'role': 'user', 'content': 'Review'}],
        'max_output_tokens': 1500}, purpose='device.vision', config=captured)
    assert len(seen) == 1 and clients[0].is_closed()
    assert result['status'] == 'failed' and result['output'] == []
    assert result['error']['code'] == 'response_too_large'
    assert result['model'] == model and result['service_tier'] == tier
    assert result['usage']['total_tokens'] == 340
    assert result['usage']['input_tokens_details']['cached_tokens'] == 80
    assert result['usage']['output_tokens_details']['reasoning_tokens'] == 20


@pytest.mark.parametrize('tier,expected', [('flash', 'deepseek-flash'), ('pro', 'deepseek-pro')])
@pytest.mark.asyncio
async def test_legacy_environment_facade_keeps_its_explicit_quoted_tier(monkeypatch, tier, expected):
    from types import SimpleNamespace
    cfg = ProviderConfig(provider='azure_foundry', base_url='https://offline.invalid',
        api_key=SecretStr('offline'), source='platform_default', deployment_pro='deepseek-pro', deployment_flash='deepseek-flash')
    sent = []
    class Client:
        def __init__(self):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        def with_options(self, **kwargs):
            assert kwargs == {'max_retries': 0}
            return self
        async def create(self, **kwargs):
            sent.append(kwargs)
            return SimpleNamespace(choices=[], usage=None)
        async def close(self):
            pass
    monkeypatch.delenv('MODEL_ROUTING_OVERRIDES', raising=False)
    monkeypatch.setattr(sole, '_primary_chat', None)
    monkeypatch.setattr(sole, '_usage_recorder', None)
    monkeypatch.setattr(sole, 'resolve_override', lambda *a, **kw: None)
    monkeypatch.setattr(sole, 'boundary_decision', lambda messages, **kw: Decision(messages=messages))
    monkeypatch.setattr(sole, 'open_env_client', lambda _: (Client(), cfg))
    monkeypatch.setattr(sole, 'create_async_client_from_config', lambda captured: (Client(), captured))
    await sole.env_chat_completion([{'role': 'user', 'content': 'Synthetic legacy copy'}], tier=tier)
    assert len(sent) == 1 and sent[0]['model'] == expected

"""Actual budgeted facade/SDK wire with offline transport and a synthetic boundary.

No provider network, billing database or live inference is used.
"""
import json
from copy import deepcopy

import pytest
from llm_gateway import budget, sole
from llm_gateway.boundary import Decision
from llm_gateway.client import LLMClient
from llm_gateway.provider_foundry import ProviderConfig
from openai import AsyncOpenAI, _base_client
from pydantic import SecretStr

httpx = getattr(_base_client, "httpx", None) or _base_client.httpx2

CALLS = [{"type": "function_call", "id": f"fc_{i}", "call_id": f"call_{i}",
          "name": "read_file", "arguments": json.dumps({"path": f"file-{i}"}),
          "status": "completed"} for i in range(2)]
OUTPUT = [{"type": "reasoning", "id": "rs_offline", "summary": [],
           "encrypted_content": "opaque-offline-reasoning"}, *CALLS,
          {"type": "message", "id": "msg_commentary", "role": "assistant",
           "phase": "commentary", "status": "completed",
           "content": [{"type": "output_text", "text": "Checking the two files.", "annotations": []}]}]
TOOLS = [{"type": "function", "function": {"name": "read_file", "parameters": {
    "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}]


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "offline-budget-test")
    monkeypatch.setenv("MODEL_ROUTING_OVERRIDES", '{"*":{"model":"sol-deploy","catalog_model":"gpt-6.1-sol"}}')
    monkeypatch.setattr(sole, "resolve_override", lambda *args, **kwargs: None)
    monkeypatch.setattr(sole, "_primary_chat", None)
    monkeypatch.setattr(sole, "_usage_recorder", None)
    monkeypatch.setattr(sole, "boundary_decision", lambda messages, **kwargs: Decision(messages=messages))
    monkeypatch.setattr(sole, "time_context", lambda: "Trusted budget test clock")
    return ProviderConfig(provider="azure_foundry", source="platform_default",
        base_url="https://offline.invalid", api_key=SecretStr("offline-budget-test"), max_output_tokens=512)


def tool_round():
    return [{"role": "assistant", "content": "Checking the files.",
             "responses_output_items": deepcopy(OUTPUT)},
            {"role": "tool", "tool_call_id": "call_1", "content": "Second file result."},
            {"role": "tool", "tool_call_id": "call_0", "content": "First file result."}]


@pytest.mark.asyncio
async def test_real_facade_budget_crossing_preserves_encrypted_output_phase_and_complete_wire_round(config, monkeypatch):
    monkeypatch.setattr(budget, "prompt_budget", lambda purpose: 1000)
    requests, clients = [], []
    def respond(request):
        requests.append(json.loads(request.content))
        output = OUTPUT if len(requests) == 1 else [{"type": "message", "id": "msg_final",
            "role": "assistant", "phase": "final_answer", "status": "completed",
            "content": [{"type": "output_text", "text": "Complete.", "annotations": []}]}]
        return httpx.Response(200, json={"id": f"resp_{len(requests)}", "object": "response",
            "model": "gpt-6.1-sol", "status": "completed", "output": output,
            "usage": {"input_tokens": 150, "output_tokens": 10, "total_tokens": 160,
                "input_tokens_details": {"cached_tokens": 20}, "output_tokens_details": {"reasoning_tokens": 2}}})
    def factory(captured):
        client = AsyncOpenAI(api_key="offline-budget-test", base_url="https://offline.invalid/openai/v1",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)), max_retries=2)
        clients.append(client)
        return client, captured
    monkeypatch.setattr(sole, "create_async_client_from_config", factory)
    client = LLMClient(config=config, offline=False)
    initial = [{"role": "system", "content": "Follow the reviewed task."},
               {"role": "user", "content": "Read both project files."}]
    first = await client.chat(initial, purpose="harness.turn", tools=TOOLS)
    messages = [initial[0], {"role": "user", "content": "Old disposable text. " * 2000},
        initial[1], first.assistant_message,
        {"role": "tool", "tool_call_id": "call_1", "content": "Second file result."},
        {"role": "tool", "tool_call_id": "call_0", "content": "First file result."},
        {"role": "user", "content": "Finish from these exact saved results."}]
    before = deepcopy(messages)
    assert budget.messages_token_estimate(messages) > 1000
    final = await client.chat(messages, purpose="harness.turn", tools=TOOLS)
    assert final.content == "Complete." and len(requests) == len(clients) == 2
    assert all(client.is_closed() for client in clients)
    wire = requests[1]["input"]
    assert not any("Old disposable text" in str(item.get("content")) for item in wire)
    assert next(item for item in wire if item.get("type") == "reasoning") == OUTPUT[0]
    assert next(item for item in wire if item.get("id") == "msg_commentary")["phase"] == "commentary"
    calls = [item for item in wire if item.get("type") == "function_call"]
    results = [item for item in wire if item.get("type") == "function_call_output"]
    assert calls == CALLS
    assert results == [{"type": "function_call_output", "call_id": "call_1", "output": "Second file result."},
                       {"type": "function_call_output", "call_id": "call_0", "output": "First file result."}]
    assert {item["call_id"] for item in calls} == {item["call_id"] for item in results}
    assert wire[-1] == {"role": "user", "content": "Finish from these exact saved results."}
    assert messages == before


@pytest.mark.asyncio
@pytest.mark.parametrize("large", ["arguments", "result", "reasoning"])
async def test_impossible_protected_round_fails_before_client_open(config, monkeypatch, large):
    monkeypatch.setattr(budget, "prompt_budget", lambda purpose: 100)
    monkeypatch.setattr(sole, "create_async_client_from_config", lambda *args: pytest.fail("over-budget round opened a provider client"))
    messages = [{"role": "system", "content": "Task"}, *tool_round()]
    if large == "arguments":
        messages[1]["responses_output_items"][1]["arguments"] = json.dumps({"path": "x" * 10000})
    elif large == "result":
        messages[2]["content"] = "Exact output line.\n" * 1000
    else:
        messages[1]["responses_output_items"][0]["encrypted_content"] = "opaque" * 2000
    before = deepcopy(messages)
    with pytest.raises(ValueError, match="prompt_budget_exceeded"):
        await LLMClient(config=config, offline=False).chat(messages, purpose="harness.turn", tools=TOOLS)
    assert messages == before


@pytest.mark.asyncio
async def test_current_instruction_cannot_be_dropped_when_a_tool_result_is_last(config, monkeypatch):
    monkeypatch.setattr(budget, "prompt_budget", lambda purpose: 1000)
    monkeypatch.setattr(sole, "create_async_client_from_config", lambda *args: pytest.fail("current instruction was discarded before a provider call"))
    messages = [{"role": "system", "content": "Reviewed policy"},
                {"role": "user", "content": "The exact current goal. " * 1000}, *tool_round()]
    before = deepcopy(messages)
    with pytest.raises(ValueError, match="prompt_budget_exceeded"):
        await LLMClient(config=config, offline=False).chat(messages, purpose="harness.turn", tools=TOOLS)
    assert messages == before


@pytest.mark.asyncio
async def test_current_plain_commentary_cannot_be_removed_to_fit_a_complete_tool_exchange(config, monkeypatch):
    monkeypatch.setattr(budget, "prompt_budget", lambda purpose: 1000)
    opened = []
    def forbidden_client(*args):
        opened.append(True)
        pytest.fail("compression changed the current exchange before provider I/O")
    monkeypatch.setattr(sole, "create_async_client_from_config", forbidden_client)
    messages = [{"role": "system", "content": "Reviewed policy"},
                {"role": "user", "content": "Read both files under the current scope."},
                {"role": "assistant", "content": "Current plain commentary. " * 200}, *tool_round()]
    before = deepcopy(messages)
    # Dropping only this unmarked commentary would otherwise make the round fit.
    assert budget.messages_token_estimate(messages) > 1000
    assert budget.messages_token_estimate(messages[:2] + messages[3:]) < 1000
    with pytest.raises(ValueError, match="prompt_budget_exceeded"):
        await LLMClient(config=config, offline=False).chat(messages, purpose="harness.turn", tools=TOOLS)
    assert not opened and messages == before


@pytest.mark.asyncio
@pytest.mark.parametrize("messages", [
    [{"role": "tool", "tool_call_id": "orphan", "content": "Result"}],
    tool_round()[:-1],
    [*tool_round(), {"role": "tool", "tool_call_id": "call_0", "content": "Repeated result"}],
    [tool_round()[0], {"role": "user", "content": "Interrupt incomplete results"}, *tool_round()[1:]],
    [{"role": "assistant", "tool_calls": [{"id": "same"}, {"id": "same"}], "content": ""}],
    [{"role": "assistant", "responses_output_items": [{"type": "function_call_output", "call_id": "orphan", "output": "Bad"}]}],
])
async def test_invalid_tool_history_is_rejected_before_client_open(config, monkeypatch, messages):
    monkeypatch.setattr(sole, "create_async_client_from_config", lambda *args: pytest.fail("invalid round opened a provider client"))
    with pytest.raises(ValueError, match="tool_round_|tool_result_without_call"):
        await LLMClient(config=config, offline=False).chat(messages, purpose="harness.turn", tools=TOOLS)


def test_plain_history_compression_keeps_current_instruction_without_mutating_input():
    messages = [{"role": "system", "content": "Always follow this policy."},
                {"role": "user", "content": "Obsolete context. " * 1000},
                {"role": "assistant", "content": "An older plain answer."},
                {"role": "user", "content": "Keep the current instruction exactly."}]
    before = deepcopy(messages)
    compressed, log = budget.compress_messages(messages, max_tokens=50)
    assert log.steps and budget.messages_token_estimate(compressed) <= 50
    assert compressed[0] == messages[0] and compressed[-1] == messages[-1]
    assert messages == before

"""reasoning_content must survive multi-turn tool conversations."""

from __future__ import annotations

from types import SimpleNamespace

from llm_gateway.messages import assistant_message_preserving_reasoning, ensure_reasoning_roundtrip


def _fake_response(*, content: str, reasoning: str, tool_calls=None):
    fn = SimpleNamespace(name="lookup", arguments='{"q":"x"}')
    tcs = None
    if tool_calls:
        tcs = [
            SimpleNamespace(id="call_1", type="function", function=fn),
        ]
    msg = SimpleNamespace(content=content, tool_calls=tcs, reasoning_content=reasoning)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def test_assistant_message_preserves_reasoning_content() -> None:
    resp = _fake_response(content="hi", reasoning="think step by step", tool_calls=True)
    msg = assistant_message_preserving_reasoning(resp)
    assert msg["role"] == "assistant"
    assert msg["content"] == "hi"
    assert msg["reasoning_content"] == "think step by step"
    assert msg["tool_calls"][0]["id"] == "call_1"


def test_multi_turn_roundtrip_keeps_reasoning_on_every_assistant() -> None:
    turns = []
    for i in range(3):
        resp = _fake_response(
            content=f"step-{i}",
            reasoning=f"reason-{i}",
            tool_calls=True,
        )
        turns.append(assistant_message_preserving_reasoning(resp))
        turns.append(
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": '{"ok":true}',
            }
        )
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}, *turns]
    roundtripped = ensure_reasoning_roundtrip(messages)
    assistants = [m for m in roundtripped if m.get("role") == "assistant"]
    assert len(assistants) == 3
    for i, m in enumerate(assistants):
        assert m.get("reasoning_content") == f"reason-{i}"
        assert "tool_calls" in m


def test_dropping_reasoning_would_be_detectable() -> None:
    resp = _fake_response(content="x", reasoning="secret-chain-of-thought")
    msg = assistant_message_preserving_reasoning(resp)
    assert "reasoning_content" in msg
    # Simulate the bug: strip field → next request would 400 on Foundry
    buggy = {k: v for k, v in msg.items() if k != "reasoning_content"}
    assert "reasoning_content" not in buggy

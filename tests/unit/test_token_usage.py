from types import SimpleNamespace

from llm_gateway.usage import extract_token_usage


def test_extract_openai_prompt_and_completion():
    usage = SimpleNamespace(
        prompt_tokens=120,
        completion_tokens=40,
        total_tokens=160,
        prompt_tokens_details=SimpleNamespace(cached_tokens=30),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=8),
    )
    counts = extract_token_usage(usage)
    assert counts["prompt_tokens"] == 120
    assert counts["completion_tokens"] == 40
    assert counts["total_tokens"] == 160
    assert counts["cached_tokens"] == 30
    assert counts["reasoning_tokens"] == 8


def test_extract_anthropic_adds_cache_split_to_input():
    usage = {
        "input_tokens": 50,
        "output_tokens": 20,
        "cache_read_input_tokens": 100,
        "cache_creation_input_tokens": 10,
    }
    counts = extract_token_usage(usage)
    assert counts["prompt_tokens"] == 160
    assert counts["completion_tokens"] == 20
    assert counts["cached_tokens"] == 100
    assert counts["total_tokens"] == 180


def test_extract_missing_usage_is_zero_not_invented():
    counts = extract_token_usage(None)
    assert counts == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cached_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
    }

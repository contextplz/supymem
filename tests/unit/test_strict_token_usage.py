"""Complete billing receipts are distinct from permissive usage telemetry."""
from types import SimpleNamespace

import pytest
from openai.types import CompletionUsage

from llm_gateway.usage import extract_confirmed_token_usage, extract_token_usage


def test_real_openai_sdk_complete_receipt_and_unset_optional_fields():
    receipt = CompletionUsage(prompt_tokens=120, completion_tokens=40, total_tokens=160)
    assert extract_confirmed_token_usage(receipt) == dict(
        prompt_tokens=120, completion_tokens=40, total_tokens=160, cached_tokens=0, reasoning_tokens=0)


@pytest.mark.parametrize("receipt", [
    {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    SimpleNamespace(prompt_tokens=0, completion_tokens=0, total_tokens=0),
    {"input_tokens": 0, "output_tokens": 0},
])
def test_legitimate_zero_is_confirmed_not_missing(receipt):
    assert extract_confirmed_token_usage(receipt) == dict(
        prompt_tokens=0, completion_tokens=0, total_tokens=0, cached_tokens=0, reasoning_tokens=0)


def test_openai_details_are_subsets_not_additional_billable_counts():
    receipt = CompletionUsage(prompt_tokens=120, completion_tokens=40, total_tokens=160,
        prompt_tokens_details={"cached_tokens": 30}, completion_tokens_details={"reasoning_tokens": 8})
    assert extract_confirmed_token_usage(receipt) == dict(
        prompt_tokens=120, completion_tokens=40, total_tokens=160, cached_tokens=30, reasoning_tokens=8)


def test_anthropic_cache_splits_add_to_uncached_input():
    receipt = {"input_tokens": 50, "output_tokens": 20,
               "cache_read_input_tokens": 100, "cache_creation_input_tokens": 10}
    assert extract_confirmed_token_usage(receipt) == dict(
        prompt_tokens=160, completion_tokens=20, total_tokens=180, cached_tokens=100, reasoning_tokens=0)
    assert extract_confirmed_token_usage({**receipt, "total_tokens": 180}) == extract_confirmed_token_usage(receipt)
    assert extract_confirmed_token_usage({**receipt, "total_tokens": 179}) is None


@pytest.mark.parametrize("receipt", [None, {}, [], 1, "10", {"total_tokens": 0},
    {"prompt_tokens": 10}, {"completion_tokens": 10}, {"input_tokens": 10}, {"output_tokens": 10},
    {"prompt_tokens": 10, "completion_tokens": 1},
    {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 12},
    {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11, "input_tokens": 10, "output_tokens": 1},
])
def test_missing_malformed_or_mixed_receipt_remains_unknown(receipt):
    assert extract_confirmed_token_usage(receipt) is None


@pytest.mark.parametrize("field", ["prompt_tokens", "completion_tokens", "total_tokens"])
@pytest.mark.parametrize("invalid", [True, False, -1, None, "1", 1.0])
def test_openai_required_counts_are_exact_nonnegative_integers(field, invalid):
    receipt = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, field: invalid}
    assert extract_confirmed_token_usage(receipt) is None


@pytest.mark.parametrize("field", ["input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"])
@pytest.mark.parametrize("invalid", [True, -1, None, "1", 1.0])
def test_anthropic_reported_counts_are_exact_nonnegative_integers(field, invalid):
    receipt = {"input_tokens": 1, "output_tokens": 1, field: invalid}
    assert extract_confirmed_token_usage(receipt) is None


@pytest.mark.parametrize("details,counter,invalid", [
    ("prompt_tokens_details", "cached_tokens", True),
    ("prompt_tokens_details", "cached_tokens", -1),
    ("prompt_tokens_details", "cached_tokens", 11),
    ("completion_tokens_details", "reasoning_tokens", 2),
    ("completion_tokens_details", "reasoning_tokens", "1"),
])
def test_reported_detail_counts_are_valid_subsets(details, counter, invalid):
    assert extract_confirmed_token_usage({"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11,
                                         details: {counter: invalid}}) is None


def test_logging_normalizer_contract_remains_unchanged():
    assert extract_token_usage(None) == dict(prompt_tokens=0, completion_tokens=0,
        cached_tokens=0, reasoning_tokens=0, total_tokens=0)
    assert extract_token_usage({"prompt_tokens": "12", "completion_tokens": "4"})["total_tokens"] == 16
    assert extract_confirmed_token_usage({"prompt_tokens": "12", "completion_tokens": "4"}) is None


@pytest.mark.parametrize("details", [{}, {"prompt_tokens_details": None, "completion_tokens_details": None}])
def test_unreported_deepseek_breakdown_keeps_valid_billable_counts(details):
    assert extract_confirmed_token_usage({"prompt_tokens": 6, "completion_tokens": 2, "total_tokens": 8,
        **details}) == {"prompt_tokens": 6, "completion_tokens": 2, "total_tokens": 8, "cached_tokens": 0, "reasoning_tokens": 0}


@pytest.mark.parametrize("receipt_type", [dict, SimpleNamespace, CompletionUsage])
@pytest.mark.parametrize("details,cached,reasoning", [
    ({"prompt_tokens_details": {"cached_tokens": 30}}, 30, 0),
    ({"completion_tokens_details": {"reasoning_tokens": 8}}, 0, 8),
])
def test_omitted_optional_container_preserves_the_reported_subset(receipt_type, details, cached, reasoning):
    receipt = receipt_type(prompt_tokens=120, completion_tokens=40, total_tokens=160, **details)
    assert extract_confirmed_token_usage(receipt) == dict(prompt_tokens=120, completion_tokens=40,
        total_tokens=160, cached_tokens=cached, reasoning_tokens=reasoning)


@pytest.mark.parametrize("details", [
    {"prompt_tokens_details": None, "completion_tokens_details": {"reasoning_tokens": 0}},
    {"prompt_tokens_details": {"cached_tokens": 0}, "completion_tokens_details": None},
    {"prompt_tokens_details": {}},
    {"completion_tokens_details": {"reasoning_tokens": None}},
    {"prompt_tokens_details": {}, "completion_tokens_details": {}},
    {"prompt_tokens_details": True, "completion_tokens_details": []},
    {"prompt_tokens_details": "invalid", "completion_tokens_details": {"reasoning_tokens": 0}},
    {"prompt_tokens_details": {"cached_tokens": 0}, "completion_tokens_details": {"reasoning_tokens": None}},
])
def test_partial_or_malformed_breakdown_is_unknown(details):
    assert extract_confirmed_token_usage({"prompt_tokens": 6, "completion_tokens": 2, "total_tokens": 8, **details}) is None

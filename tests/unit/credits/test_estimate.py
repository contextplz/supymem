"""Estimate bounds: max_tokens ceiling, unknown model, floor, no binary floats."""

from __future__ import annotations

import ast
import inspect
from decimal import Decimal

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def pricing_app(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/est.db")
    load_company_os_app()


def test_max_tokens_ceiling_is_priced(pricing_app) -> None:
    from app.credits.estimate import estimate_llm_micro

    small = estimate_llm_micro("deepseek-v4-flash", prompt_tokens=100, max_tokens=1_000)
    large = estimate_llm_micro(
        "deepseek-v4-flash", prompt_tokens=100, max_tokens=100_000
    )
    assert large > small
    # Output tokens dominate; 100× tokens must move the estimate proportionally.
    assert large >= small * 50


def test_deepseek_unreported_breakdown_prices_all_input_and_completion(pricing_app):
    from app.credits.estimate import actual_llm_micro, estimate_llm_micro
    from app.credits.provider_receipts import capture_pricing_snapshot
    from llm_gateway.usage import extract_confirmed_token_usage
    snapshot = capture_pricing_snapshot("DeepSeek-V4-Pro")
    row = snapshot["models"]["DeepSeek-V4-Pro"]
    assert Decimal(row["input_per_mtok"]) > 0 and Decimal(row["output_per_mtok"]) > 0
    counts = extract_confirmed_token_usage({"prompt_tokens": 6, "completion_tokens": 2, "total_tokens": 8,
        "prompt_tokens_details": None, "completion_tokens_details": None})
    assert actual_llm_micro("DeepSeek-V4-Pro", prompt_tokens=counts["prompt_tokens"],
        completion_tokens=counts["completion_tokens"], cached_tokens=counts["cached_tokens"]) == 4
    assert estimate_llm_micro("DeepSeek-V4-Pro", prompt_tokens=6, max_tokens=2) >= 4


def test_unknown_model_is_not_free_and_uses_max_rate(pricing_app) -> None:
    from app.credits.estimate import estimate_llm_micro
    from app.credits.units import MIN_RESERVE_MICRO, usd_to_micro
    from app.llm.pricing import pricing_table

    max_in = Decimal("0")
    max_out = Decimal("0")
    for row in pricing_table().values():
        if not isinstance(row, dict):
            continue
        max_in = max(max_in, Decimal(str(row.get("input_per_mtok") or 0)))
        max_out = max(max_out, Decimal(str(row.get("output_per_mtok") or 0)))

    prompt_tokens = 1_000
    max_tokens = 8_000
    usd = max_in * Decimal(prompt_tokens) / Decimal(1_000_000) + max_out * Decimal(
        max_tokens
    ) / Decimal(1_000_000)
    expected = max(usd_to_micro(usd), MIN_RESERVE_MICRO)
    got = estimate_llm_micro(
        "totally-unknown-model-xyz",
        prompt_tokens=prompt_tokens,
        max_tokens=max_tokens,
    )
    assert got == expected
    assert got > 0
    cheap = estimate_llm_micro(
        "deepseek-v4-flash", prompt_tokens=prompt_tokens, max_tokens=max_tokens
    )
    assert got > cheap


def test_min_reserve_micro_floor_applies(pricing_app) -> None:
    from app.credits.estimate import estimate_llm_micro
    from app.credits.units import MIN_RESERVE_MICRO

    tiny = estimate_llm_micro("deepseek-v4-flash", prompt_tokens=1, max_tokens=1)
    assert tiny == MIN_RESERVE_MICRO


def test_no_float_in_estimate_module(pricing_app) -> None:
    from app.credits import estimate as estimate_mod

    tree = ast.parse(inspect.getsource(estimate_mod))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id != "float"
        if isinstance(node, ast.Constant):
            assert type(node.value) is not float


def test_confirmed_cache_arithmetic_and_missing_price_fallback(pricing_app, monkeypatch):
    from app.credits import estimate
    row = {"input_per_mtok": "2", "output_per_mtok": "10", "cached_input_per_mtok": "0.1"}
    monkeypatch.setattr(estimate, "pricing_table", lambda: {"synthetic": row})
    assert estimate.actual_llm_micro("synthetic", prompt_tokens=100, completion_tokens=20, cached_tokens=80) == 248
    assert estimate.actual_llm_micro("synthetic", prompt_tokens=100, completion_tokens=20) == 400
    row.pop("cached_input_per_mtok")
    assert estimate.actual_llm_micro("synthetic", prompt_tokens=100, completion_tokens=20, cached_tokens=80) == 400


@pytest.mark.parametrize("cached,exception", [(True, TypeError), ("1", TypeError), (Decimal("1"), TypeError),
                                             (-1, ValueError), (101, ValueError)])
def test_confirmed_cache_count_validation(pricing_app, cached, exception):
    from app.credits.estimate import actual_llm_micro
    with pytest.raises(exception):
        actual_llm_micro("synthetic", prompt_tokens=100, completion_tokens=20, cached_tokens=cached)


def test_long_context_threshold_and_reservation(pricing_app, monkeypatch):
    from app.credits import estimate
    row = {"input_per_mtok": "2", "output_per_mtok": "10", "cached_input_per_mtok": "0.1",
           "long_context_threshold_tokens": 1000, "long_context_input_per_mtok": "4",
           "long_context_output_per_mtok": "15", "long_context_cached_input_per_mtok": "0.2"}
    monkeypatch.setattr(estimate, "pricing_table", lambda: {"synthetic": row})
    assert estimate.actual_llm_micro("synthetic", prompt_tokens=1000, completion_tokens=100, cached_tokens=500) == 2050
    assert estimate.actual_llm_micro("synthetic", prompt_tokens=1001, completion_tokens=100, cached_tokens=500) == 3604
    assert estimate.estimate_llm_micro("synthetic", prompt_tokens=1000, max_tokens=100) == 3000
    assert estimate.estimate_llm_micro("synthetic", prompt_tokens=1001, max_tokens=100) == 5504
    row.pop("long_context_cached_input_per_mtok")
    assert estimate.actual_llm_micro("synthetic", prompt_tokens=1001, completion_tokens=100, cached_tokens=500) == 5504


def test_long_context_missing_rate_does_not_invent_discount(pricing_app, monkeypatch):
    from app.credits import estimate
    row = {"input_per_mtok": "2", "output_per_mtok": "10", "cached_input_per_mtok": "0.1",
           "long_context_threshold_tokens": 1000}
    monkeypatch.setattr(estimate, "pricing_table", lambda: {"synthetic": row})
    assert estimate.actual_llm_micro("synthetic", prompt_tokens=1001, completion_tokens=100, cached_tokens=500) == 3002


def test_cache_price_priority_multiplier(pricing_app, monkeypatch):
    from app.credits import estimate
    row = {"input_per_mtok": "2", "output_per_mtok": "10", "cached_input_per_mtok": "0.1"}
    monkeypatch.setattr(estimate, "pricing_table", lambda: {"synthetic": row})
    monkeypatch.setenv("SUPYMEM_CU_PRIORITY_MULTIPLIER", "2")
    assert estimate.actual_llm_micro("synthetic", prompt_tokens=100, completion_tokens=20, cached_tokens=80, priority=True) == 496

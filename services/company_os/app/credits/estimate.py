"""Upper-bound LLM cost estimates. Decimal arithmetic only."""

from __future__ import annotations

import os

import logging
from decimal import Decimal, ROUND_CEILING
from typing import Any

from app.credits.units import MIN_RESERVE_MICRO, usd_to_micro
from app.llm.pricing import load_pricing, pricing_table

log = logging.getLogger("app.credits.estimate")

_MTOK = Decimal("1000000")
_CHARS_PER_TOKEN = Decimal("3.5")


def _decimal_rate(value: object, *, name: str) -> Decimal:
    """Coerce a yaml rate to Decimal without binary floating-point arithmetic."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool) or value is None:
        raise TypeError(f"{name} is missing or not a number")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        return Decimal(value)
    # PyYAML may give a binary floating value; str() round-trips pinned list prices.
    return Decimal(str(value))


def _row_rates(row: dict[str, Any]) -> tuple[Decimal, Decimal]:
    return (
        _decimal_rate(row.get("input_per_mtok"), name="input_per_mtok"),
        _decimal_rate(row.get("output_per_mtok"), name="output_per_mtok"),
    )


def _max_configured_rates() -> tuple[Decimal, Decimal]:
    """Most expensive input and output rates in the table. Never zero."""
    max_in = Decimal("0")
    max_out = Decimal("0")
    for raw in pricing_table().values():
        if not isinstance(raw, dict):
            continue
        try:
            inp, out = _row_rates(raw)
        except TypeError:
            continue
        if inp > max_in:
            max_in = inp
        if out > max_out:
            max_out = out
    if max_in == 0 and max_out == 0:
        log.warning("llm pricing table empty; using conservative fallback rates")
        return Decimal("5"), Decimal("30")
    return max_in, max_out


def _lookup_model_row(model: str | None) -> dict[str, Any] | None:
    models = pricing_table()
    key = (model or "").strip()
    if not key:
        return None
    if key in models:
        row = models[key]
        if isinstance(row, dict):
            return dict(row)
    aliases = load_pricing().get("aliases") or {}
    mapped = aliases.get(key) or aliases.get(key.lower())
    if mapped and mapped in models:
        row = models[mapped]
        if isinstance(row, dict):
            return dict(row)
    lower = {str(k).lower(): v for k, v in models.items()}
    found = lower.get(key.lower())
    if isinstance(found, dict):
        return dict(found)
    return None


def _context_row(row: dict[str, Any], prompt_tokens: int) -> dict[str, Any]:
    threshold = row.get("long_context_threshold_tokens")
    if threshold is None:
        return row
    if type(threshold) is not int or threshold < 0:
        raise ValueError("long_context_threshold_tokens must be a non-negative int")
    if prompt_tokens <= threshold:
        return row
    effective = dict(row)
    for field in ("input_per_mtok", "output_per_mtok"):
        effective[field] = row.get("long_context_" + field, row.get(field))
    # A short-context cache discount does not establish a long-context rate.
    effective["cached_input_per_mtok"] = row.get(
        "long_context_cached_input_per_mtok", effective["input_per_mtok"])
    return effective


def _rates_for_model(model: str | None, *, prompt_tokens: int = 0) -> tuple[Decimal, Decimal]:
    row = _lookup_model_row(model)
    if row is not None:
        return _row_rates(_context_row(row, prompt_tokens))
    max_in, max_out = _max_configured_rates()
    if model is not None:
        log.warning("unknown llm model %r; pricing at max configured rates %s/%s per mtok", model, max_in, max_out)
    return max_in, max_out


def _usd_cost(prompt_rate: Decimal, output_rate: Decimal, prompt_tokens: int, output_tokens: int) -> Decimal:
    prompt = Decimal(prompt_tokens) * prompt_rate / _MTOK
    output = Decimal(output_tokens) * output_rate / _MTOK
    return prompt + output


def priority_multiplier() -> Decimal:
    """Price multiple for OpenAI priority processing (service_tier=priority). Env-tunable; never below 1."""
    try:
        value = Decimal(os.environ.get("SUPYMEM_CU_PRIORITY_MULTIPLIER", "2"))
    except Exception:
        value = Decimal(2)
    return max(Decimal(1), min(value, Decimal(10)))


def estimate_llm_micro(
    model: str | None, *, prompt_tokens: int, max_tokens: int, priority: bool = False
) -> int:
    """Upper-bound micro-credits: prompt at prompt rate, ``max_tokens`` at output rate.

    Pricing ``max_tokens`` at the ceiling is what makes estimate-gaming (attack #9)
    structurally impossible. Unknown models and model-agnostic ceilings (``None``)
    use the most expensive configured rate. ``MIN_RESERVE_MICRO`` is a floor.
    """
    if type(prompt_tokens) is not int or type(max_tokens) is not int:
        raise TypeError("token counts must be int")
    if prompt_tokens < 0 or max_tokens < 0:
        raise ValueError("token counts must be non-negative")
    prompt_rate, output_rate = _rates_for_model(model, prompt_tokens=prompt_tokens)
    usd = _usd_cost(prompt_rate, output_rate, prompt_tokens, max_tokens) * (priority_multiplier() if priority else 1)
    micro = usd_to_micro(usd)
    return max(micro, MIN_RESERVE_MICRO)


def estimate_llm_purpose_micro(purpose: str, *, prompt_tokens: int, max_tokens: int) -> int:
    from app.core.local_mode import is_local

    model = None
    if is_local():
        from app.core.providers_local import LocalProviderError, load_local_config
        from app.llm.founder_provider import keyless_local_config
        from llm_gateway.provider_foundry import resolve_live_model
        from llm_gateway.routing import resolve_purpose_config, sol_output_cap

        try:
            config = resolve_purpose_config(purpose, load_local_config())
        except LocalProviderError:
            pass
        else:
            if keyless_local_config(config):
                return 0
            model = resolve_live_model(config, tier=config.captured_tier)
            max_tokens = sol_output_cap(config, max_tokens)
    return estimate_llm_micro(model, prompt_tokens=prompt_tokens, max_tokens=max_tokens)


def actual_llm_micro(
    model: str, *, prompt_tokens: int, completion_tokens: int, priority: bool = False,
    cached_tokens: int = 0,
) -> int:
    """Actual (not ceiling) cost from provider token counts. No reserve floor."""
    if type(prompt_tokens) is not int or type(completion_tokens) is not int or type(cached_tokens) is not int:
        raise TypeError("token counts must be int")
    if prompt_tokens < 0 or completion_tokens < 0:
        raise ValueError("token counts must be non-negative")
    if not 0 <= cached_tokens <= prompt_tokens:
        raise ValueError("cached tokens must be between zero and prompt tokens")
    row = _lookup_model_row(model)
    if row is None:
        prompt_rate, output_rate = _rates_for_model(model, prompt_tokens=prompt_tokens)
        cached_rate = prompt_rate
    else:
        row = _context_row(row, prompt_tokens)
        prompt_rate, output_rate = _row_rates(row)
        cached_rate = _decimal_rate(row.get("cached_input_per_mtok", prompt_rate), name="cached_input_per_mtok")
    usd = (_usd_cost(prompt_rate, output_rate, prompt_tokens - cached_tokens, completion_tokens)
           + Decimal(cached_tokens) * cached_rate / _MTOK) * (priority_multiplier() if priority else 1)
    return usd_to_micro(usd)


def count_prompt_tokens(messages: list[dict[str, Any]]) -> int:
    """Conservative character estimate: chars/3.5, rounded up.

    We cannot tokenize every provider; over-estimating is the safe direction.
    """
    chars = 0
    for msg in messages:
        role = msg.get("role")
        if isinstance(role, str):
            chars += len(role)
        content = msg.get("content")
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, str):
                    chars += len(part)
                elif isinstance(part, dict):
                    text = part.get("text")
                    if isinstance(text, str):
                        chars += len(text)
    if chars <= 0:
        return 0
    tokens = (Decimal(chars) / _CHARS_PER_TOKEN).to_integral_value(
        rounding=ROUND_CEILING
    )
    return int(tokens)

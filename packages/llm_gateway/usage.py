"""Normalize provider usage objects into prompt/completion counts.

OpenAI-compatible APIs return ``prompt_tokens`` / ``completion_tokens``.
Anthropic returns ``input_tokens`` / ``output_tokens``, and when prompt
caching is on those input fields are split across cache read/write.
We do not invent counts: missing usage is recorded as zero.
"""

from __future__ import annotations

from typing import Any


def _get(obj: Any, *names: str) -> Any:
    for name in names:
        if obj is None:
            return None
        if isinstance(obj, dict):
            if name in obj and obj[name] is not None:
                return obj[name]
        else:
            value = getattr(obj, name, None)
            if value is not None:
                return value
    return None


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def extract_token_usage(usage: Any) -> dict[str, int]:
    """Return billed-ish token counts from an OpenAI or Anthropic usage object."""
    if usage is None:
        return {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cached_tokens": 0,
            "reasoning_tokens": 0,
            "total_tokens": 0,
        }

    openai_prompt = _get(usage, "prompt_tokens")
    openai_completion = _get(usage, "completion_tokens")
    anthropic_input = _get(usage, "input_tokens")
    anthropic_output = _get(usage, "output_tokens")

    cache_read = _int(_get(usage, "cache_read_input_tokens"))
    cache_write = _int(_get(usage, "cache_creation_input_tokens"))

    details_in = _get(usage, "prompt_tokens_details") or _get(usage, "input_tokens_details")
    details_out = _get(usage, "completion_tokens_details") or _get(usage, "output_tokens_details")
    cached_from_details = _int(_get(details_in, "cached_tokens"))
    reasoning = _int(_get(details_out, "reasoning_tokens"))

    if openai_prompt is not None:
        prompt = _int(openai_prompt)
        cached = cached_from_details or cache_read
    elif _get(usage, "input_tokens_details") is not None:
        # Responses input already includes cached input, unlike Anthropic's split.
        prompt = _int(anthropic_input)
        cached = cached_from_details
    else:
        # Anthropic: input_tokens is the uncached tail; add cache read/write
        # so "input" means tokens the model actually processed.
        prompt = _int(anthropic_input) + cache_read + cache_write
        cached = cache_read or cached_from_details

    if openai_completion is not None:
        completion = _int(openai_completion)
    else:
        completion = _int(anthropic_output)

    total = _int(_get(usage, "total_tokens")) or (prompt + completion)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cached_tokens": cached,
        "reasoning_tokens": reasoning,
        "total_tokens": total,
    }


_MISSING = object()


def _receipt_field(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, _MISSING)
    return getattr(obj, name, _MISSING)


def _receipt_count(obj: Any, name: str, *, optional: bool = False) -> int | None:
    value = _receipt_field(obj, name)
    if value is _MISSING and optional:
        return 0
    return value if type(value) is int and value >= 0 else None


def extract_confirmed_token_usage(usage: Any) -> dict[str, int] | None:
    """Return complete billing counts, or None when the receipt is unknown.

    Unlike the permissive logging normalizer, required counters are never
    invented or coerced. OpenAI requires prompt/completion/total; Anthropic
    requires input/output and adds any reported cache input splits. Optional
    SDK fields that were not supplied by the provider stay absent.
    """
    if usage is None:
        return None
    try:
        dump = getattr(usage, "model_dump", None)
        if callable(dump):
            usage = dump(exclude_unset=True)
        openai = any(_receipt_field(usage, name) is not _MISSING
                     for name in ("prompt_tokens", "completion_tokens"))
        anthropic = any(_receipt_field(usage, name) is not _MISSING
                        for name in ("input_tokens", "output_tokens"))
        if openai == anthropic:
            return None
        responses = anthropic and any(_receipt_field(usage, name) is not _MISSING
            for name in ("input_tokens_details", "output_tokens_details"))
        cached = reasoning = 0
        if openai or responses:
            prompt = _receipt_count(usage, "prompt_tokens" if openai else "input_tokens")
            completion = _receipt_count(usage, "completion_tokens" if openai else "output_tokens")
            total = _receipt_count(usage, "total_tokens")
            if prompt is None or completion is None or total != prompt + completion:
                return None
            input_details = "prompt_tokens_details" if openai else "input_tokens_details"
            output_details = "completion_tokens_details" if openai else "output_tokens_details"
            unreported = all(_receipt_field(usage, key) is _MISSING or _receipt_field(usage, key) is None
                for key in (input_details, output_details))
            for key, counter, bound in (
                (input_details, "cached_tokens", prompt),
                (output_details, "reasoning_tokens", completion),
            ):
                details = _receipt_field(usage, key)
                if unreported or details is _MISSING:
                    # ponytail: no reported breakdown charges uncached input; use provider subsets when available.
                    count = 0
                else:
                    count = _receipt_count(details, counter)
                    if count is None or count > bound:
                        return None
                if counter == "cached_tokens":
                    cached = count
                else:
                    reasoning = count
        else:
            uncached = _receipt_count(usage, "input_tokens")
            completion = _receipt_count(usage, "output_tokens")
            cached = _receipt_count(usage, "cache_read_input_tokens", optional=True)
            cache_write = _receipt_count(usage, "cache_creation_input_tokens", optional=True)
            if any(value is None for value in (uncached, completion, cached, cache_write)):
                return None
            prompt = uncached + cached + cache_write
            total = prompt + completion
            if (_receipt_field(usage, "total_tokens") is not _MISSING
                    and _receipt_count(usage, "total_tokens") != total):
                return None
        return {"prompt_tokens": prompt, "completion_tokens": completion,
                "total_tokens": total, "cached_tokens": cached, "reasoning_tokens": reasoning}
    except (AttributeError, TypeError, ValueError):
        return None

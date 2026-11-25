"""One shared clip-then-revalidate step for model output parsed with pydantic.

Only when every validation error is a length overflow is each offending string cut to its
max_length at a word boundary (and each over-long list of plain strings truncated) and the
output revalidated; anything else still rejects. Lists of objects are never truncated: dropping
items could drop evidence or claims.
"""
from __future__ import annotations

import copy
import json
import logging
from functools import lru_cache
from typing import Any

from pydantic import TypeAdapter, ValidationError

_log = logging.getLogger(__name__)


def clip_text(text: str, limit: int) -> str:
    cut = text[: limit - 1].rstrip()
    head = cut.rpartition(" ")[0]
    return (head if len(head) > limit // 2 else cut).rstrip() + "…"


@lru_cache(maxsize=64)
def _adapter(model: Any) -> TypeAdapter:
    return TypeAdapter(model)


def _prepared(target: Any, raw: Any):
    adapter = target if isinstance(target, TypeAdapter) else _adapter(target)
    parse = adapter.validate_json if isinstance(raw, (str, bytes)) else adapter.validate_python
    return adapter, parse


def _clipped(raw: Any, exc: ValidationError, preserve_paths: tuple = ()) -> Any | None:
    """`raw` as python data with over-long strings clipped, or None if any other error exists."""
    errors = exc.errors()
    if not errors or not all(e["type"] in ("string_too_long", "too_long") for e in errors):
        return None
    # Never shorten complete executable/discovery instructions. A protected
    # prefix may contain '*' for one list index; any overflow rejects the whole
    # original response so downstream bounded repair can ask for complete text.
    if any(len(e["loc"]) >= len(path) and all(wanted == "*" or wanted == actual
           for wanted, actual in zip(path, e["loc"])) for e in errors for path in preserve_paths):
        return None
    data = json.loads(raw) if isinstance(raw, (str, bytes)) else copy.deepcopy(raw)
    try:
        # strings first so list indices stay valid, then truncate lists
        for e in sorted(errors, key=lambda e: e["type"] == "too_long"):
            parent = data
            for key in e["loc"][:-1]:
                parent = parent[key]
            key, limit = e["loc"][-1], e["ctx"]["max_length"]
            if e["type"] == "too_long":
                if not all(isinstance(i, str) for i in parent[key]):
                    return None
                del parent[key][limit:]
            else:
                parent[key] = clip_text(parent[key].strip(), limit)
    except (KeyError, IndexError, TypeError, AttributeError):
        return None
    _log.warning("model_output_clipped", extra={"clipped_strings": len(errors)})
    return data


def validate_clipping(target: Any, raw: Any, *, preserve_paths: tuple = ()) -> Any:
    """Validate `raw` (JSON str or python data) against a model class or TypeAdapter."""
    adapter, parse = _prepared(target, raw)
    data = clip_overlong(target, raw, preserve_paths=preserve_paths)
    return parse(raw) if data is raw else adapter.validate_python(data)


def clip_overlong(target: Any, raw: Any, *, preserve_paths: tuple = ()) -> Any:
    """Data ready for a stricter downstream validator: clipped if that is the only problem, else unchanged.

    Pydantic reports a list's own `too_long` only once its items are valid, so clip in a few passes.
    """
    adapter, parse = _prepared(target, raw)
    data = raw
    for _ in range(3):
        try:
            parse(data)
            return data
        except ValidationError as exc:
            clipped = _clipped(data, exc, preserve_paths)
            if clipped is None:
                return raw
            data, parse = clipped, adapter.validate_python
    return raw

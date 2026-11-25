"""JSONPath / dotted-path lookups used by Huginn Trigger and Peak agents."""

from __future__ import annotations

import json
import re
from typing import Any


def value_at(payload: Any, path: str | None) -> Any:
    """Huginn Utils.value_at — dotted path, optional [index], `$` root."""
    if not path or path in {".", "$"}:
        return payload
    cur: Any = payload
    for part in _tokens(path):
        if cur is None:
            return None
        if part.isdigit() and isinstance(cur, list):
            idx = int(part)
            cur = cur[idx] if 0 <= idx < len(cur) else None
            continue
        if isinstance(cur, dict):
            cur = cur.get(part)
            continue
        return None
    return cur


def values_at(payload: Any, path: str | None) -> list[Any]:
    """Collect matches; `*` walks a list (WebsiteAgent JSON extract)."""
    if not path or "*" not in path:
        found = value_at(payload, path)
        if found is None:
            return []
        return found if isinstance(found, list) else [found]
    return _walk(payload, _tokens(path))


def same_json(left: Any, right: Any) -> bool:
    return json.dumps(left, sort_keys=True, default=str) == json.dumps(right, sort_keys=True, default=str)


def _tokens(path: str) -> list[str]:
    cleaned = path.lstrip("$").lstrip(".")
    parts: list[str] = []
    for chunk in cleaned.split("."):
        if not chunk:
            continue
        for m in re.finditer(r"([^\[\]]+)|\[(\d+|\*)\]", chunk):
            parts.append(m.group(1) or m.group(2))
    return parts


def _walk(node: Any, tokens: list[str]) -> list[Any]:
    if not tokens:
        return [node]
    head, rest = tokens[0], tokens[1:]
    if head == "*":
        if not isinstance(node, list):
            return []
        out: list[Any] = []
        for item in node:
            out.extend(_walk(item, rest))
        return out
    if head.isdigit() and isinstance(node, list):
        idx = int(head)
        if 0 <= idx < len(node):
            return _walk(node[idx], rest)
        return []
    if isinstance(node, dict) and head in node:
        return _walk(node[head], rest)
    return []

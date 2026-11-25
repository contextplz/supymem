"""Prompt budgeting — never silently truncate system or current instruction."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from llm_gateway.routing import prompt_budget


def estimate_tokens(text: str) -> int:
    # Cheap heuristic: ~4 chars/token. Good enough for budgets without tiktoken dep.
    return max(1, len(text) // 4)


def messages_token_estimate(messages: list[dict[str, Any]]) -> int:
    total = 0
    for m in messages:
        if _protected_message(m) and m.get("role") not in {"system", "developer"}:
            # Include arguments, result IDs, encrypted reasoning and output phase.
            total += estimate_tokens(json.dumps(m, ensure_ascii=False, allow_nan=False))
            continue
        content = m.get("content") or ""
        if isinstance(content, list):
            content = "".join(
                str(p.get("text", "")) if isinstance(p, dict) else str(p) for p in content
            )
        total += estimate_tokens(str(content))
        if "tool_calls" in m:
            total += estimate_tokens(str(m["tool_calls"]))
    return total


def _protected_message(message: dict[str, Any]) -> bool:
    return (message.get("role") in {"system", "developer", "tool"}
            or bool(message.get("tool_calls")) or "responses_output_items" in message
            or "reasoning_content" in message)


def _validate_tool_rounds(messages: list[dict[str, Any]]) -> None:
    """A follow-up must contain every requested result once, in either result order."""
    pending, seen = set(), set()
    for message in messages:
        role = message.get("role")
        events = []
        if role == "tool":
            events = [("result", message.get("tool_call_id"))]
        else:
            if pending:
                raise ValueError("tool_round_incomplete")
            if role == "assistant":
                output = message.get("responses_output_items")
                if output:
                    if not isinstance(output, list) or any(not isinstance(item, dict) for item in output):
                        raise ValueError("tool_round_invalid_output")
                    events = [("call" if item["type"] == "function_call" else "result", item.get("call_id"))
                              for item in output if item.get("type") in {"function_call", "function_call_output"}]
                else:
                    calls = message.get("tool_calls") or []
                    if not isinstance(calls, list) or any(not isinstance(call, dict) for call in calls):
                        raise ValueError("tool_round_invalid_calls")
                    events = [("call", call.get("id")) for call in calls]
        for kind, call_id in events:
            if not isinstance(call_id, str) or not call_id.strip():
                raise ValueError("tool_round_invalid_id")
            if kind == "call":
                if call_id in seen:
                    raise ValueError("tool_round_duplicate_call")
                seen.add(call_id)
                pending.add(call_id)
            elif call_id not in pending:
                raise ValueError("tool_result_without_call")
            else:
                pending.remove(call_id)
    if pending:
        raise ValueError("tool_round_incomplete")


@dataclass
class CompressionLog:
    steps: list[str]


def compress_messages(
    messages: list[dict[str, Any]],
    *,
    max_tokens: int,
) -> tuple[list[dict[str, Any]], CompressionLog]:
    """Compress plain history while preserving complete tool/reasoning messages.

    Never truncates the system prompt or any item in the current user exchange.
    """
    log = CompressionLog(steps=[])
    msgs = [dict(m) for m in messages]
    current_start = next((i for i in range(len(msgs) - 1, -1, -1)
                          if msgs[i].get("role") == "user"), max(0, len(msgs) - 1))
    current_exchange = {id(m) for m in msgs[current_start:]}
    _validate_tool_rounds(msgs)
    if messages_token_estimate(msgs) <= max_tokens:
        return msgs, log

    # ponytail: retain all tool/reasoning history; compact complete older rounds
    # only when longer traces need it. An impossible remainder fails before I/O.
    # 1. Drop oldest plain messages outside the current exchange.
    while messages_token_estimate(msgs) > max_tokens and len(msgs) > 2:
        # Find first droppable (not system, not last)
        drop_idx = None
        for i, m in enumerate(msgs[:-1]):
            if not _protected_message(m) and id(m) not in current_exchange:
                drop_idx = i
                break
        if drop_idx is None:
            break
        msgs.pop(drop_idx)
        log.steps.append(f"dropped_history_index_{drop_idx}")

    # 2. Truncate largest non-system/non-last content with marker
    if messages_token_estimate(msgs) > max_tokens:
        candidates = [
            (i, m)
            for i, m in enumerate(msgs[:-1])
            if not _protected_message(m) and id(m) not in current_exchange and isinstance(m.get("content"), str)
        ]
        if candidates:
            i, m = max(candidates, key=lambda t: len(str(t[1].get("content") or "")))
            content = str(m["content"])
            keep = max(200, len(content) // 3)
            lines = content.splitlines()
            kept = "\n".join(lines[: max(5, keep // 40)])
            dropped = max(0, len(lines) - kept.count("\n") - 1)
            msgs[i] = {
                **m,
                "content": kept + f"\n[truncated: {dropped} lines]",
            }
            log.steps.append(f"truncated_message_{i}")

    if messages_token_estimate(msgs) > max_tokens:
        raise ValueError("prompt_budget_exceeded")
    return msgs, log


def apply_purpose_budget(
    purpose: str, messages: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], CompressionLog]:
    budget = prompt_budget(purpose) or 6000
    return compress_messages(messages, max_tokens=budget)

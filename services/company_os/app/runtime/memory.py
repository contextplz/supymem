"""Durable memory assembly under a 6000-token ceiling."""

from __future__ import annotations

from typing import Any


TOKEN_CEILING = 6000


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def assemble_context(
    *,
    system_prompt: str,
    artifacts: list[dict[str, Any]],
    ceiling: int = TOKEN_CEILING,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    used = estimate_tokens(system_prompt)
    for art in artifacts:
        chunk = str(art.get("content") or "")
        cost = estimate_tokens(chunk)
        if used + cost > ceiling:
            break
        messages.append({"role": "user", "content": f"[{art.get('kind','artifact')}]\n{chunk}"})
        used += cost
    return messages

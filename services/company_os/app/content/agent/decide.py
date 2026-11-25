"""Decide what the agent should do next given sensed signals + brief."""

from __future__ import annotations

import logging
from typing import Any

from app.content.agent.policy import policy_for

log = logging.getLogger(__name__)


def decide(*, workspace_id: str, signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a list of intended actions.

    Uses the LLM when available; falls back to deterministic heuristics so the
    agent keeps moving offline.
    """
    from app.content import brief as brief_store

    brief = brief_store.active(workspace_id)
    product = (brief or {}).get("product") if isinstance(brief, dict) else {}
    one_liner = ""
    if isinstance(product, dict):
        one_liner = str(product.get("one_liner") or product.get("name") or "our product")

    actions: list[dict[str, Any]] = []
    # Prefer LLM batch when configured.
    llm_actions = _llm_decide(signals=signals, one_liner=one_liner, brief=brief)
    if llm_actions is not None:
        return _filter_by_policy(llm_actions)

    for sig in signals:
        action = _heuristic(sig, one_liner=one_liner)
        if action:
            actions.append(action)
    return _filter_by_policy(actions)


def _filter_by_policy(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for a in actions:
        pol = policy_for(str(a.get("platform") or ""))
        kind = str(a.get("kind") or "")
        if kind == "reply" and not pol.can_reply:
            continue
        if kind == "post" and not pol.can_post:
            continue
        out.append(a)
    return out


def _heuristic(sig: dict[str, Any], *, one_liner: str) -> dict[str, Any] | None:
    kind = sig.get("kind")
    platform = sig.get("platform")
    summary = str(sig.get("summary") or "")
    if kind == "comment" and sig.get("reply_target"):
        # Don't reply to ourselves / empty.
        author = str(sig.get("author") or "").lower()
        if author in {"[deleted]", "automoderator"}:
            return None
        body = (
            f"Appreciate you raising this. {one_liner} "
            f"is aimed at exactly that kind of friction, happy to compare notes."
        )[:480]
        return {
            "kind": "reply",
            "platform": platform,
            "target": sig["reply_target"],
            "body": body,
            "signal_external_id": sig.get("external_id"),
            "reason": "reply_to_comment",
            "parent_post_id": sig.get("parent_post_id"),
        }
    if kind == "sector_post" and platform == "reddit" and sig.get("reply_target"):
        # Only engage when the title looks like a question / pain.
        lower = summary.lower()
        if not any(k in lower for k in ("?", "how", "help", "struggling", "advice", "tool")):
            return {
                "kind": "observe",
                "platform": platform,
                "signal_external_id": sig.get("external_id"),
                "reason": "sector_noise",
            }
        body = (
            f"We've been deep in this. {one_liner} "
            f", curious what you've already tried."
        )[:480]
        return {
            "kind": "reply",
            "platform": platform,
            "target": sig["reply_target"],
            "body": body,
            "signal_external_id": sig.get("external_id"),
            "reason": "sector_engage",
        }
    if kind in {"reactions", "post_snapshot"}:
        return {
            "kind": "observe",
            "platform": platform,
            "signal_external_id": sig.get("external_id"),
            "reason": "learn_engagement",
            "payload": {"summary": summary[:200]},
        }
    return None


def _llm_decide(
    *,
    signals: list[dict[str, Any]],
    one_liner: str,
    brief: dict[str, Any] | None,
) -> list[dict[str, Any]] | None:
    # Heuristics are the reliable default; LLM optional later.
    return None

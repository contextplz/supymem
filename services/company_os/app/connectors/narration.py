"""Founder-facing copy for connections, resolved from the narration deck.

Copy lives in ``config/narration.d/connections.yaml``; scope-specific wording
lives in the provider manifests. No founder-facing string is written in Python
(invariant I6). When a fake adapter served the call, the line carries the
``(simulated)`` suffix so nobody believes a real pull request was opened
(27 §5, 39 §7).
"""

from __future__ import annotations

from typing import Any

from app.onboarding.narrator import interpolate, load_deck

SIMULATED_KEY = "connections.simulated_suffix"


def _suffix() -> str:
    entry = load_deck().get(SIMULATED_KEY) or ""
    return entry if isinstance(entry, str) else str(entry.get("done", ""))


def stamp_simulated(text: str) -> str:
    suffix = _suffix()
    if not text or not suffix or text.endswith(suffix):
        return text
    return f"{text} {suffix}"


def line(
    key: str,
    *,
    status: str = "done",
    simulated: bool = False,
    tokens: dict[str, Any] | None = None,
) -> str:
    deck = load_deck()
    entry = deck.get(key)
    if entry is None:
        return ""
    if isinstance(entry, str):
        text = entry
    else:
        order = [f"{status}_simulated"] if simulated else []
        order += [status, "done", "running", "blocked"]
        text = next((entry[name] for name in order if name in entry), "")
    rendered = interpolate(text, tokens or {})
    return stamp_simulated(rendered) if simulated else rendered

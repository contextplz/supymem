"""Proposals API for the Suggestions cards (and anything else that wants "what should happen next").

    propose_next_actions(venture_id, limit)  -> list[ProposedAction]   (read-only, starts nothing)
    execute_proposed_action(venture_id, action, actor) -> dict          (Go ahead: same guarded path as God mode)

The planner is the one God mode's tick uses (autopilot/lifecycle.py), so a card and an autonomous step never disagree.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from app.autopilot import lifecycle


@dataclass(frozen=True)
class ProposedAction:
    id: str  # stable for the same step + payload, so a dismissed card stays dismissed
    title: str
    why: str
    will_do: str  # what Supymem will do if the founder says go ahead
    estimated_credits: float  # 0 when the step quotes its own price before it spends anything
    kind: str  # chat action or driver name; "human" when only a person can do it
    payload: dict[str, Any] = field(default_factory=dict)
    requires_mac: bool = False
    reversible: bool = True
    human_only: bool = False  # Go ahead would do nothing: show the link instead
    href: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _to_proposal(mv: lifecycle.Move) -> ProposedAction:
    kind = mv.action or "human"
    return ProposedAction(
        id=lifecycle.move_id(mv.key, mv.action, mv.payload), title=mv.title, why=mv.why, will_do=mv.will_do,
        estimated_credits=lifecycle.estimate_credits(mv.action, mv.payload), kind=kind, payload=dict(mv.payload),
        requires_mac=kind in lifecycle.MAC_KINDS, reversible=kind not in lifecycle.IRREVERSIBLE,
        human_only=mv.human, href=mv.human_href)


def propose_next_actions(venture_id: str, limit: int = 3) -> list[ProposedAction]:
    """The best next moves, most important first. Waiting-only moves (work already running) are left out."""
    try:
        moves = lifecycle.all_moves(venture_id)
    except Exception:  # noqa: BLE001 - a card feed never breaks the screen
        return []
    return [_to_proposal(m) for m in moves if not m.wait][:max(0, limit)]


async def execute_proposed_action(venture_id: str, action: ProposedAction | dict[str, Any], actor: str | None,
                                  *, approved_ceiling_micro: int | None = None) -> dict[str, Any]:
    """Run a proposal the founder approved. Floors still apply (credentials, sends, allow-list).

    `done` contains independently completed actions, not queued work. The
    explicit completion flag is false whenever the chain stopped or is pending.
    Never raises for a refused or failed step."""
    from app.autopilot import service

    kind = action["kind"] if isinstance(action, dict) else action.kind
    payload = dict(action["payload"] if isinstance(action, dict) else action.payload)
    title = action["title"] if isinstance(action, dict) else action.title
    if kind == "human":
        return {"ok": False, "reason": "needs_you", "done": [], "completion_verified": False}
    done: list[str] = []
    step = {"key": f"suggestion:{kind}", "title": title, "chat_action": kind, "payload": payload}
    from app.suggestions.proposals import BLOCKED_KINDS  # one tap never publishes or reaches people, even as the next card in a chain

    effects = {"jobs": set(), "documents": [], "records": [], "links": [], "message": ""}
    token = service._effects.set(effects)
    fence_token = service._forbidden.set(service._forbidden.get() | BLOCKED_KINDS)
    try:
        stop = await service._run_step(venture_id, step, actor, done, human=True,
                                      approved_ceiling_micro=approved_ceiling_micro)
    finally:
        service._forbidden.reset(fence_token)
        service._effects.reset(token)
    effects["jobs"] = sorted(effects["jobs"])
    if stop == "waiting":
        return {"ok": True, "reason": "noop", "done": done, "completion_verified": False, "effects": effects}
    ran = bool(done) or stop == "queued"  # queued = admitted and running in the background
    return {"ok": ran and stop in (None, "queued"), "reason": stop or ("" if ran else "nothing_to_do"),
            "done": done, "completion_verified": bool(done) and stop is None, "effects": effects}

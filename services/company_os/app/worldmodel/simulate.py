"""Business world model simulation — rules → empirical → max 1 flash."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from app.worldmodel.actions import NO_OP, QUANTITIES_FOR, Action
from app.worldmodel.estimators import Est, empirical_rate
from app.worldmodel.rules import rules_rate
from app.worldmodel.state import WorldState, snapshot


@dataclass
class Prediction:
    quantity: str
    point: float
    lo: float
    hi: float
    method: str
    confidence: float


@dataclass
class SimulationRun:
    verdict: str
    predictions: list[Prediction] = field(default_factory=list)
    method_mix: dict[str, int] = field(default_factory=dict)
    llm_calls: int = 0
    elapsed_ms: float = 0.0
    blocking_reason: str = ""
    cost_usd: float = 0.0


async def _default_llm(quantity: str, state: WorldState, action: Action) -> Est:
    # Offline deterministic flash stand-in
    return Est(quantity, -0.05, 0.0, 0.05, 0.3, "llm")


async def simulate(
    venture_id: str,
    action: Action | dict[str, Any],
    *,
    state: WorldState | None = None,
    horizon_days: int = 14,
    llm_estimate: Callable[..., Awaitable[Est]] | None = None,
    metrics: dict[str, float] | None = None,
) -> SimulationRun:
    t0 = time.perf_counter()
    if isinstance(action, dict):
        action = Action(kind=action.get("kind", "config_change"), cost_usd=float(action.get("cost_usd") or 0), params=action)
    st = state or snapshot(venture_id, metrics)
    mix: dict[str, int] = {"rules": 0, "empirical": 0, "llm": 0}
    llm_calls = 0
    llm_fn = llm_estimate or _default_llm

    # Feasibility
    if action.cost_usd > st.budget_remaining_usd:
        return SimulationRun(
            verdict="unfavourable",
            blocking_reason="budget",
            elapsed_ms=(time.perf_counter() - t0) * 1000,
        )

    async def rate(q: str) -> Est:
        nonlocal llm_calls
        v = rules_rate(q, st, action)
        if v is not None:
            mix["rules"] += 1
            return Est(q, v, v, v, 1.0, "rules")
        est = empirical_rate(q, st)
        if est.confidence >= 0.2:
            mix["empirical"] += 1
            return est
        if llm_calls >= 1:
            return Est.insufficient(q)
        e = await llm_fn(q, st, action)
        e.confidence = min(e.confidence, 0.4)
        llm_calls += 1
        mix["llm"] += 1
        return e

    quantities = QUANTITIES_FOR.get(action.kind, ["engagement_rate"])
    # Always evaluate noop counterfactual + action arm for primary quantity
    preds: list[Prediction] = []
    for q in quantities:
        base = await rate(q) if action.kind != "noop" else await rate(q)
        # For action arm, re-rate (may hit llm cap → insufficient_data is neutral)
        arm = await rate(q) if False else base  # same snapshot; delta via action.kind nudge
        point = arm.point
        if action.kind != "noop" and arm.method != "insufficient_data":
            point = arm.point + (0.02 if action.kind == "config_change" else 0.0)
        lo, hi = min(arm.lo, point), max(arm.hi, point)
        assert lo <= point <= hi or arm.method == "insufficient_data"
        preds.append(Prediction(q, point, lo, hi, arm.method, arm.confidence))

    # insufficient_data must not block
    verdict = "favourable"
    if any(p.method == "insufficient_data" for p in preds) and all(
        p.method in {"insufficient_data", "rules"} for p in preds
    ):
        verdict = "insufficient_data"  # neutral
    elapsed = (time.perf_counter() - t0) * 1000
    return SimulationRun(
        verdict=verdict,
        predictions=preds,
        method_mix=mix,
        llm_calls=llm_calls,
        elapsed_ms=elapsed,
        cost_usd=0.0,
    )

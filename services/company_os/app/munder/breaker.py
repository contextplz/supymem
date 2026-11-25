"""Circuit breaker — steer → constrain → stop. Policy only, no side effects.

Ported from Munder Difflin ``src/main/breaker.ts``. Company OS maps one
venture to one "agent" (we do not run a floor of CLI processes).

MIT: Chaitanya Giri. Do not run vendor/munder-difflin in-process.
"""

from __future__ import annotations

import json
import os
from typing import Any, Literal

from app.state import MEM

BreakerLevel = Literal["healthy", "steering", "constrained", "stopped"]
BreakerAction = Literal["none", "steer", "constrain", "stop"]

LEVELS: list[BreakerLevel] = ["healthy", "steering", "constrained", "stopped"]
DEFAULTS = {
    "enabled": True,
    "hardStop": False,
    "repeatedToolLimit": 8,
    "errorStormLimit": 5,
    "tokenVelocityPerMin": 60_000,
}
COMPACT_GRACE_MS = 5 * 60_000
POST_COMPACT_GRACE_MS = 90_000
PROGRESS_TOOL_WINDOW_MS = 300_000
NO_PROGRESS_BEATS = 2


def _rank(level: str) -> int:
    try:
        return LEVELS.index(level)  # type: ignore[arg-type]
    except ValueError:
        return 0


def _action_for(level: str) -> BreakerAction:
    if level == "steering":
        return "steer"
    if level == "constrained":
        return "constrain"
    if level == "stopped":
        return "stop"
    return "none"


def _tokens_of(sample: dict[str, Any] | None) -> int:
    if not sample:
        return 0
    return int(
        (sample.get("input") or 0)
        + (sample.get("output") or 0)
        + (sample.get("cacheRead") or 0)
        + (sample.get("cacheCreation") or 0)
    )


def _blank_state() -> dict[str, Any]:
    return {
        "level": "healthy",
        "reason": "",
        "lastSample": None,
        "repeatKey": None,
        "repeatCount": 0,
        "errorCount": 0,
        "compactingUntil": 0,
        "lastDistinctToolAt": 0,
        "noProgressBeats": 0,
    }


def _agents() -> dict[str, Any]:
    data = MEM.get("munder_breaker_agents")
    if not isinstance(data, dict):
        data = {}
        MEM["munder_breaker_agents"] = data
    return data


def _put_agents(data: dict[str, Any]) -> None:
    MEM["munder_breaker_agents"] = data


def _get(agent_id: str) -> dict[str, Any]:
    agents = _agents()
    state = agents.get(agent_id)
    if not isinstance(state, dict):
        state = _blank_state()
        agents[agent_id] = state
        _put_agents(agents)
    return state


def _set(agent_id: str, state: dict[str, Any]) -> None:
    agents = _agents()
    agents[agent_id] = state
    _put_agents(agents)


def forget(agent_id: str) -> None:
    agents = _agents()
    agents.pop(agent_id, None)
    _put_agents(agents)


def level_for(agent_id: str) -> BreakerLevel:
    state = _agents().get(agent_id)
    if not isinstance(state, dict):
        return "healthy"
    level = state.get("level") or "healthy"
    return level if level in LEVELS else "healthy"


def _cfg() -> dict[str, Any]:
    hard = (os.environ.get("COMPANY_OS_BREAKER_HARD_STOP") or "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    enabled = (os.environ.get("COMPANY_OS_BREAKER_OFF") or "").strip().lower() not in {
        "1",
        "true",
        "yes",
    }
    cap_usd = os.environ.get("COMPANY_OS_LLM_BUDGET_USD_MONTH") or "0"
    try:
        cost_cap = float(cap_usd)
    except ValueError:
        cost_cap = 0.0
    return {
        **DEFAULTS,
        "enabled": enabled,
        "hardStop": hard,
        "costCapUsd": cost_cap if cost_cap > 0 else None,
        "costCapTokens": None,
        "agentTokenCaps": {},
    }


def _tool_key(tool_name: str | None, tool_input: Any) -> str:
    try:
        inp = json.dumps(
            tool_input,
            default=lambda v: (v[:250] if isinstance(v, str) and len(v) > 250 else v),
        )
    except TypeError:
        inp = str(tool_input)
    return f"{tool_name or '?'}:{inp[:200]}"


def record_tool_use(
    agent_id: str,
    tool_name: str | None,
    tool_input: Any,
    now: int | None = None,
) -> None:
    state = _get(agent_id)
    key = _tool_key(tool_name, tool_input)
    if key == state.get("repeatKey"):
        state["repeatCount"] = int(state.get("repeatCount") or 0) + 1
    else:
        state["repeatKey"] = key
        state["repeatCount"] = 1
        state["errorCount"] = 0
        state["lastDistinctToolAt"] = int(now) if now is not None else _now_ms()
    _set(agent_id, state)


def record_error(agent_id: str) -> None:
    state = _get(agent_id)
    state["errorCount"] = int(state.get("errorCount") or 0) + 1
    _set(agent_id, state)


def record_compact_start(agent_id: str, now: int | None = None) -> None:
    state = _get(agent_id)
    state["compactingUntil"] = int(now or _now_ms()) + COMPACT_GRACE_MS
    _set(agent_id, state)


def record_compact_end(agent_id: str, now: int | None = None) -> None:
    state = _get(agent_id)
    now_ms = int(now or _now_ms())
    if int(state.get("compactingUntil") or 0) > now_ms:
        state["compactingUntil"] = now_ms + POST_COMPACT_GRACE_MS
        _set(agent_id, state)


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


def _evaluate(
    agent_id: str,
    sample: dict[str, Any] | None,
    progressing: bool,
    state: dict[str, Any],
    cfg: dict[str, Any],
    now_ms: int,
    is_top_spender: bool,
    is_top_token: bool,
) -> tuple[bool, str]:
    if int(state.get("repeatCount") or 0) >= int(cfg["repeatedToolLimit"]):
        tool = str(state.get("repeatKey") or "?").split(":", 1)[0]
        return True, f"looping: {state['repeatCount']}× identical tool call ({tool})"
    if int(state.get("errorCount") or 0) >= int(cfg["errorStormLimit"]):
        return True, f"error storm: {state['errorCount']} consecutive api errors/retries"
    caps = cfg.get("agentTokenCaps") or {}
    per_cap = caps.get(agent_id)
    if isinstance(per_cap, (int, float)) and per_cap > 0 and _tokens_of(sample) > per_cap:
        return True, f"token limit: {_tokens_of(sample)} over the agent cap of {int(per_cap)}"
    cost_cap = cfg.get("costCapUsd")
    if is_top_spender and isinstance(cost_cap, (int, float)):
        usd = float((sample or {}).get("usd") or 0)
        return True, f"cost cap: floor total over ${cost_cap} (top spender ${usd:.2f})"
    token_cap = cfg.get("costCapTokens")
    if is_top_token and isinstance(token_cap, (int, float)):
        return True, (
            f"token cap: floor total over {int(token_cap)} tokens "
            f"(top spender {_tokens_of(sample)})"
        )
    last = state.get("lastSample")
    if sample and isinstance(last, dict) and now_ms >= int(state.get("compactingUntil") or 0):
        d_out = int(sample.get("output") or 0) - int(last.get("output") or 0)
        d_min = (int(sample.get("ts") or 0) - int(last.get("ts") or 0)) / 60_000
        if d_out > 0 and d_min > 0:
            velocity = d_out / d_min
            if velocity > cfg["tokenVelocityPerMin"]:
                return True, (
                    f"token velocity {round(velocity)}/min > {cfg['tokenVelocityPerMin']}/min"
                )
            tool_active = now_ms - int(state.get("lastDistinctToolAt") or 0) < PROGRESS_TOOL_WINDOW_MS
            if not progressing and not tool_active:
                state["noProgressBeats"] = int(state.get("noProgressBeats") or 0) + 1
                if state["noProgressBeats"] >= NO_PROGRESS_BEATS:
                    return True, "no-progress: generating tokens without coordinating (stale log/files)"
            else:
                state["noProgressBeats"] = 0
    return False, ""


def tick(
    inputs: list[dict[str, Any]],
    now_ms: int,
    *,
    config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Evaluate every agent for this beat. Copy of ``CircuitBreaker.tick``."""
    cfg = {**_cfg(), **(config or {})}
    decisions: list[dict[str, Any]] = []
    if not cfg.get("enabled", True):
        for item in inputs:
            agent_id = str(item["agentId"])
            state = _get(agent_id)
            changed = state.get("level") != "healthy"
            state["level"] = "healthy"
            state["reason"] = ""
            _set(agent_id, state)
            decisions.append(
                {
                    "state": {"agentId": agent_id, "level": "healthy", "reason": "", "ts": now_ms},
                    "action": "none",
                    "changed": changed,
                }
            )
        return decisions

    top_spender = None
    cost_cap = cfg.get("costCapUsd")
    if isinstance(cost_cap, (int, float)) and cost_cap > 0:
        total = 0.0
        max_usd = -1.0
        for item in inputs:
            usd = float((item.get("sample") or {}).get("usd") or 0)
            total += usd
            if usd > max_usd:
                max_usd = usd
                top_spender = item["agentId"]
        if total <= cost_cap:
            top_spender = None

    top_token = None
    token_cap = cfg.get("costCapTokens")
    if isinstance(token_cap, (int, float)) and token_cap > 0:
        total_t = 0
        max_t = -1
        for item in inputs:
            tok = _tokens_of(item.get("sample"))
            total_t += tok
            if tok > max_t:
                max_t = tok
                top_token = item["agentId"]
        if total_t <= token_cap:
            top_token = None

    ceiling: BreakerLevel = "stopped" if cfg.get("hardStop") else "constrained"
    for item in inputs:
        agent_id = str(item["agentId"])
        state = _get(agent_id)
        sample = item.get("sample")
        tripping, reason = _evaluate(
            agent_id,
            sample,
            bool(item.get("progressing")),
            state,
            cfg,
            now_ms,
            agent_id == top_spender,
            agent_id == top_token,
        )
        if sample:
            state["lastSample"] = sample
        target_rank = min(_rank(state.get("level") or "healthy") + 1, _rank(ceiling)) if tripping else max(
            _rank(state.get("level") or "healthy") - 1, 0
        )
        target: BreakerLevel = LEVELS[target_rank]
        changed = target != state.get("level")
        escalated = _rank(target) > _rank(str(state.get("level") or "healthy"))
        state["level"] = target
        state["reason"] = reason if tripping else ("recovering — signals cleared" if changed else state.get("reason") or "")
        _set(agent_id, state)
        decisions.append(
            {
                "state": {
                    "agentId": agent_id,
                    "level": target,
                    "reason": state["reason"],
                    "ts": now_ms,
                },
                "action": _action_for(target) if escalated else "none",
                "changed": changed,
            }
        )
    return decisions


def tick_venture(
    venture_id: str,
    sample: dict[str, Any] | None,
    progressing: bool,
    now_ms: int | None = None,
    *,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = int(now_ms if now_ms is not None else _now_ms())
    rows = tick(
        [{"agentId": venture_id, "sample": sample, "progressing": progressing}],
        now,
        config=config,
    )
    return rows[0]


def block_for_venture(venture_id: str) -> dict[str, Any] | None:
    """If the ladder has reached constrain/stop, skip new work this beat."""
    from app.actuation.social import workspace_id_for
    from app.llm.pricing import global_usage_summary, workspace_usage_summary

    sample = None
    workspace_id = workspace_id_for(venture_id)
    try:
        if workspace_id:
            summary = workspace_usage_summary(workspace_id)
            sample = {
                "agentId": venture_id,
                "sessionId": workspace_id,
                "ts": _now_ms(),
                "input": int(summary.get("prompt_tokens_month") or 0),
                "output": int(summary.get("completion_tokens_month") or 0),
                "cacheRead": 0,
                "cacheCreation": 0,
                "model": "default",
                "usd": float(summary.get("estimate_usd_month") or 0),
            }
        else:
            summary = global_usage_summary()
            month = summary.get("this_month") or {}
            sample = {
                "agentId": venture_id,
                "sessionId": "platform",
                "ts": _now_ms(),
                "input": int(month.get("prompt_tokens") or 0),
                "output": int(month.get("completion_tokens") or 0),
                "cacheRead": 0,
                "cacheCreation": 0,
                "model": "default",
                "usd": float(month.get("estimate_usd") or 0),
            }
    except Exception:  # noqa: BLE001
        sample = None

    progressing = bool(MEM.get(f"munder_progress:{venture_id}"))
    decision = tick_venture(venture_id, sample, progressing)
    level = decision["state"]["level"]
    if level in {"constrained", "stopped"}:
        return {
            "level": level,
            "reason": decision["state"].get("reason") or "hive_breaker",
            "action": decision["action"],
        }
    if decision["action"] == "steer":
        from app.munder.control import control_for

        control_for(venture_id).steer(
            venture_id,
            f"Circuit breaker: steer — {decision['state'].get('reason')}. "
            "Stop repeating, summarize what you tried, and change approach.",
        )
    return None

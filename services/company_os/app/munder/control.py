"""Operator control: pause, gate tools, steer, halt.

Ported from Munder Difflin ``src/main/control.ts`` ``ControlRegistry``.
Company OS applies this per venture (one execution lock), not per PTY.

MIT: Chaitanya Giri. Do not run vendor/munder-difflin in-process.
"""

from __future__ import annotations

from typing import Any

from app.state import MEM


class ControlRegistry:
    def __init__(
        self,
        store: dict[str, Any] | None = None,
        persist: Any | None = None,
    ) -> None:
        self._map = store if store is not None else {}
        self._persist = persist

    def _touch(self) -> None:
        if self._persist:
            self._persist(self._map)

    def _ensure(self, agent_id: str) -> dict[str, Any]:
        row = self._map.get(agent_id)
        if not isinstance(row, dict):
            row = {
                "paused": False,
                "halted": False,
                "autoDeliveryPaused": False,
                "gatedTools": [],
                "steerQueue": [],
            }
            self._map[agent_id] = row
        row.setdefault("gatedTools", [])
        row.setdefault("steerQueue", [])
        return row

    def pause(self, agent_id: str, on: bool) -> None:
        self._ensure(agent_id)["paused"] = bool(on)
        self._touch()

    def halt(self, agent_id: str) -> None:
        self._ensure(agent_id)["halted"] = True
        self._touch()

    def resume(self, agent_id: str) -> None:
        row = self._ensure(agent_id)
        row["paused"] = False
        row["halted"] = False
        self._touch()

    def gate_tool(self, agent_id: str, tool: str, on: bool) -> None:
        row = self._ensure(agent_id)
        gated = [t for t in (row.get("gatedTools") or []) if t != tool]
        if on:
            gated.append(tool)
        row["gatedTools"] = gated
        self._touch()

    def steer(self, agent_id: str, text: str) -> None:
        note = (text or "").strip()[:10_000]
        if not note:
            return
        row = self._ensure(agent_id)
        queue = list(row.get("steerQueue") or [])
        queue.append(note)
        row["steerQueue"] = queue
        self._touch()

    def clear_steers(self, agent_id: str) -> None:
        row = self._map.get(agent_id)
        if isinstance(row, dict):
            row["steerQueue"] = []
            self._touch()

    def should_halt(self, agent_id: str) -> bool:
        row = self._map.get(agent_id)
        return bool(isinstance(row, dict) and row.get("halted"))

    def tool_decision(self, agent_id: str, tool: str) -> dict[str, Any]:
        row = self._map.get(agent_id)
        if not isinstance(row, dict):
            return {"deny": False}
        if row.get("paused"):
            return {
                "deny": True,
                "reason": "Paused by operator — resume from the floor to continue.",
            }
        if tool and tool in (row.get("gatedTools") or []):
            return {"deny": True, "reason": f"Tool {tool} is gated by the operator."}
        return {"deny": False}

    def take_steer(self, agent_id: str) -> str | None:
        row = self._map.get(agent_id)
        if not isinstance(row, dict):
            return None
        queue = list(row.get("steerQueue") or [])
        if not queue:
            return None
        note = queue.pop(0)
        row["steerQueue"] = queue
        self._touch()
        return str(note)

    def snapshot(self, agent_id: str) -> dict[str, Any]:
        row = self._map.get(agent_id)
        if not isinstance(row, dict):
            return {
                "paused": False,
                "halted": False,
                "autoDeliveryPaused": False,
                "gatedTools": [],
                "pendingSteers": 0,
            }
        return {
            "paused": bool(row.get("paused")),
            "halted": bool(row.get("halted")),
            "autoDeliveryPaused": bool(row.get("autoDeliveryPaused")),
            "gatedTools": list(row.get("gatedTools") or []),
            "pendingSteers": len(row.get("steerQueue") or []),
        }


def _store() -> dict[str, Any]:
    data = MEM.get("munder_control")
    if not isinstance(data, dict):
        data = {}
        MEM["munder_control"] = data
    return data


def _put(data: dict[str, Any]) -> None:
    MEM["munder_control"] = data


def control_for(venture_id: str) -> ControlRegistry:
    store = _store()
    registry = ControlRegistry(store, persist=_put)
    registry._ensure(venture_id)
    _put(store)
    return registry


def should_halt(venture_id: str) -> bool:
    return ControlRegistry(_store()).should_halt(venture_id)


def take_steer(venture_id: str) -> str | None:
    return ControlRegistry(_store(), persist=_put).take_steer(venture_id)


def tool_decision(venture_id: str, tool: str) -> dict[str, Any]:
    return ControlRegistry(_store()).tool_decision(venture_id, tool)


def snapshot(venture_id: str) -> dict[str, Any]:
    return ControlRegistry(_store()).snapshot(venture_id)

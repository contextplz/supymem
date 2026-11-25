"""Quarantine: new agents start restricted until N healthy cycles."""

from __future__ import annotations

from app.org.registry import registry


def advance_quarantine(agent_id: str, *, cycles_required: int = 2) -> str:
    inst = registry.get_instance(agent_id)
    if inst is None:
        raise KeyError(agent_id)
    if inst.status != "quarantined":
        return inst.status
    inst.quarantine_cycles_passed += 1
    if inst.quarantine_cycles_passed >= cycles_required:
        inst.status = "active"
    return inst.status

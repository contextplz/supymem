"""AgentSpec registry — YAML + in-memory/SQLite, versioned, checksummed."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from core.canonical import digest
from core.clock import utcnow
from core.ids import new_id


@dataclass
class AgentSpecRecord:
    id: str
    version: str
    venture_id: str
    role: str
    name: str
    charge: str
    system_prompt: str
    allowed_tools: list[str]
    model_tier: str
    fitness_metric: str
    retire_when: str
    max_steps: int = 12
    parent_version: str | None = None
    spec_checksum: str = ""
    created_by: str = "composer"
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AgentInstanceRecord:
    id: str
    venture_id: str
    spec_id: str
    spec_version: str
    status: str
    created_at: str
    quarantine_cycles_passed: int = 0


class SpecRegistry:
    def __init__(self) -> None:
        self._specs: dict[tuple[str, str], AgentSpecRecord] = {}
        self._instances: dict[str, AgentInstanceRecord] = {}
        self._archive: list[dict[str, Any]] = []

    def register_spec(self, spec: AgentSpecRecord) -> AgentSpecRecord:
        payload = {k: v for k, v in spec.to_dict().items() if k != "spec_checksum"}
        spec.spec_checksum = digest(payload)
        if not spec.created_at:
            spec.created_at = utcnow().isoformat()
        self._specs[(spec.id, spec.version)] = spec
        return spec

    def create_agent_from_spec(
        self,
        *,
        venture_id: str,
        role: str,
        name: str,
        charge: str,
        system_prompt: str,
        allowed_tools: list[str],
        model_tier: str,
        fitness_metric: str,
        retire_when: str,
        status: str = "active",
    ) -> AgentInstanceRecord:
        """Create spec + instance in under 1s (no import reload)."""
        t0 = time.perf_counter()
        spec_id = new_id("as")
        spec = AgentSpecRecord(
            id=spec_id,
            version="1.0.0",
            venture_id=venture_id,
            role=role,
            name=name,
            charge=charge,
            system_prompt=system_prompt,
            allowed_tools=allowed_tools,
            model_tier=model_tier,
            fitness_metric=fitness_metric,
            retire_when=retire_when,
        )
        self.register_spec(spec)
        inst = AgentInstanceRecord(
            id=new_id("ag"),
            venture_id=venture_id,
            spec_id=spec_id,
            spec_version=spec.version,
            status=status,
            created_at=utcnow().isoformat(),
        )
        self._instances[inst.id] = inst
        elapsed = time.perf_counter() - t0
        if elapsed > 1.0:
            raise RuntimeError(f"agent creation took {elapsed:.3f}s (>1s)")
        return inst

    def get_instance(self, agent_id: str) -> AgentInstanceRecord | None:
        return self._instances.get(agent_id)

    def get_spec(self, spec_id: str, version: str) -> AgentSpecRecord | None:
        return self._specs.get((spec_id, version))

    def list_agents(self, venture_id: str) -> list[AgentInstanceRecord]:
        return [a for a in self._instances.values() if a.venture_id == venture_id]

    def archive(self, spec: AgentSpecRecord, reason: str) -> None:
        self._archive.append({"spec": spec.to_dict(), "reason": reason, "at": utcnow().isoformat()})


# Process-global registry (local-first)
registry = SpecRegistry()

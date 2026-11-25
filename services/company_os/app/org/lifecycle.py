"""Agent lifecycle — create/activate/pause/retire, atomic replace."""

from __future__ import annotations

from typing import Any

from app.org.composer import OrgPlanResult, compose
from app.org.registry import registry
from app.org.summary import pack_agents


def materialize_org(
    *,
    venture_id: str,
    charter: dict[str, Any],
    brief: dict[str, Any] | None = None,
    stage: str = "org_planning",
) -> dict[str, Any]:
    from app.org.characters import canonical_role

    plan: OrgPlanResult = compose(charter, brief, stage=stage)
    pack = pack_agents(venture_id)
    seen = {canonical_role(str(a.get("role") or "")) for a in pack}
    agents = [dict(a) for a in pack]
    for role in plan.roles:
        if canonical_role(role.role) in seen:
            continue
        # Quarantine non-orchestrator initially optional; activate orchestrator
        status = "active" if role.role == "orchestrator" else "quarantined"
        if stage in {"building", "live", "demand_testing"} and role.role != "researcher":
            status = "active"
        inst = registry.create_agent_from_spec(
            venture_id=venture_id,
            role=role.role,
            name=role.name,
            charge=role.charge,
            system_prompt=role.system_prompt,
            allowed_tools=role.allowed_tools,
            model_tier=role.model_tier,
            fitness_metric=role.fitness_metric,
            retire_when=role.retire_when,
            status=status,
        )
        agents.append(
            {
                "id": inst.id,
                "role": role.role,
                "status": inst.status,
                "name": role.name,
                "charge": role.charge,
                "title": role.role.replace("_", " ").title(),
            }
        )
        seen.add(canonical_role(role.role))
    source = "composed+pack" if pack else "composed"
    return {
        "agent_count": len(agents),
        "rationale": plan.rationale,
        "rejected": plan.rejected,
        "deferred": plan.deferred,
        "agents": agents,
        "source": source,
    }


def atomic_replace(*, retire_id: str, new_spec_kwargs: dict[str, Any]) -> dict[str, Any]:
    """Retire+replace atomically — never neither, never both active mid-swap."""
    old = registry.get_instance(retire_id)
    if old is None:
        raise KeyError(retire_id)
    # Create new first as quarantined, then flip
    new = registry.create_agent_from_spec(**new_spec_kwargs, status="quarantined")
    old.status = "retired"
    new.status = "active"
    return {"retired": old.id, "activated": new.id}

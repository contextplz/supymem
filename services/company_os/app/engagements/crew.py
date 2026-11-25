"""Who does the work on an engagement (36 §6).

No org composition — that is venture-scale ceremony. Borrow the workspace's own
agents where their capabilities match, otherwise spawn at most two ephemeral
ones and retire them at delivery. Ephemeral agents are deliberately kept out of
``agent_instances`` and out of ``metrics``: one engagement is not enough signal
to evolve on, and letting it in would let a single odd request rewrite the
company.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import select

from app.db.models import AgentInstance, AgentSpec
from app.state import MEM, db
from core.ids import new_id

MAX_EPHEMERAL = 2

# Capability -> the catalog role that covers it, cheapest competent first.
ROLE_FOR_CAPABILITY: dict[str, str] = {
    "answer": "researcher",
    "diagnose": "researcher",
    "code": "builder",
    "config": "builder",
    "review": "reviewer",
    "write": "builder",
}

CHARGE: dict[str, str] = {
    "researcher": "find the answer and cite where it came from",
    "builder": "make the smallest change that satisfies the done-definition",
    "reviewer": "check the work against the done-definition before hand-over",
}


@dataclass(frozen=True)
class CrewMember:
    id: str
    role: str
    charge: str
    ephemeral: bool
    borrowed_from: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Crew:
    engagement_id: str
    members: list[CrewMember] = field(default_factory=list)
    executor: str = "runner"

    def to_dict(self) -> dict[str, Any]:
        return {
            "engagement_id": self.engagement_id,
            "executor": self.executor,
            "members": [m.to_dict() for m in self.members],
        }

    @property
    def ids(self) -> list[str]:
        return [m.id for m in self.members]


def _mem_key(engagement_id: str) -> str:
    return f"engagement:crew:{engagement_id}"


def capabilities_for(size: str, *, sensitive: bool, is_question: bool) -> list[str]:
    if is_question:
        return ["answer"]
    caps = ["code" if size != "errand" else "config"]
    if size != "errand" or sensitive:
        caps.append("review")
    return caps


def borrow(workspace_id: str, capabilities: list[str]) -> list[CrewMember]:
    """Borrow active agents whose role covers a needed capability.

    An engagement never permanently changes a workspace's org, so nothing here
    writes: it only reads which agents already exist.
    """
    from app.workspaces.resolve import venture_for_workspace

    venture = venture_for_workspace(workspace_id)
    if venture is None:
        return []

    wanted = {ROLE_FOR_CAPABILITY.get(c, c) for c in capabilities}
    found: dict[str, CrewMember] = {}
    factory = db()
    with factory() as session:
        rows = session.scalars(
            select(AgentInstance).where(
                AgentInstance.venture_id == venture.id,
                AgentInstance.status == "active",
            )
        ).all()
        for row in rows:
            spec = session.get(AgentSpec, row.spec_id)
            role = spec.role if spec is not None else ""
            if role in wanted and role not in found:
                found[role] = CrewMember(
                    id=row.id,
                    role=role,
                    charge=CHARGE.get(role, "help with this engagement"),
                    ephemeral=False,
                    borrowed_from=venture.id,
                )
    return [found[r] for r in sorted(found)]


def assemble(
    engagement_id: str,
    *,
    workspace_id: str,
    capabilities: list[str],
    byo_coding_agent: bool = False,
) -> Crew:
    """Borrow first, spawn at most two ephemeral agents for what is missing."""
    members = borrow(workspace_id, capabilities)
    covered = {m.role for m in members}
    for capability in capabilities:
        role = ROLE_FOR_CAPABILITY.get(capability, capability)
        if role in covered:
            continue
        if len([m for m in members if m.ephemeral]) >= MAX_EPHEMERAL:
            break
        members.append(
            CrewMember(
                id=new_id("eph"),
                role=role,
                charge=CHARGE.get(role, "help with this engagement"),
                ephemeral=True,
            )
        )
        covered.add(role)

    crew = Crew(
        engagement_id=engagement_id,
        members=members,
        # A connected coding agent runs on the founder's own subscription (36 §6.3).
        executor="byo_coding_agent" if byo_coding_agent else "runner",
    )
    MEM[_mem_key(engagement_id)] = crew.to_dict()
    return crew


def crew_for(engagement_id: str) -> Crew | None:
    crew = MEM.get(_mem_key(engagement_id))
    if isinstance(crew, Crew):  # Existing local cache compatibility.
        return crew
    if (not isinstance(crew, dict) or crew.get("engagement_id") != engagement_id
            or crew.get("executor") not in {"runner", "byo_coding_agent"}
            or not isinstance(crew.get("members"), list)):
        return None
    try:
        members = [CrewMember(**row) for row in crew["members"]]
        return Crew(engagement_id=engagement_id, executor=crew["executor"], members=members)
    except (TypeError, ValueError):
        return None


def retire(engagement_id: str) -> list[str]:
    """Retire the ephemeral half of the crew. Borrowed agents are left alone."""
    crew = crew_for(engagement_id)
    if crew is None:
        return []
    retired = [m.id for m in crew.members if m.ephemeral]
    MEM.pop(_mem_key(engagement_id), None)
    return retired

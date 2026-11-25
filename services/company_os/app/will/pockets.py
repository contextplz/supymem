"""Activity pockets and deterministic 1m → 5m rollups (doc 79 §7)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.state import MEM
from core.ids import new_id


class AgentBrief(BaseModel):
    id: str
    name: str
    current_task: str


class Pocket(BaseModel):
    id: str = Field(default_factory=lambda: new_id("pkt"))
    venture_id: str
    period: Literal["1m", "5m", "1h", "night"]
    window_start: datetime
    window_end: datetime
    headline: str = Field(max_length=90)
    detail: list[str] = Field(default_factory=list, max_length=5)
    agents_active: list[AgentBrief] = Field(default_factory=list)
    actuations: int = 0
    money_delta_minor: int = 0
    signals_in: int = 0
    insights_out: int = 0
    needs_founder: list[str] = Field(default_factory=list)
    notable: str | None = None
    parent_pocket_id: str | None = None

    def is_empty(self) -> bool:
        return not (
            self.detail
            or self.agents_active
            or self.actuations
            or self.money_delta_minor
            or self.signals_in
            or self.insights_out
            or self.needs_founder
            or self.notable
        )


def write_pocket(pocket: Pocket) -> Pocket | None:
    """Persist a material pocket; idle windows intentionally produce no row."""
    if pocket.is_empty():
        return None
    payload = pocket.model_dump(mode="json")
    MEM[f"pocket:{pocket.id}"] = payload
    index_key = f"pockets:{pocket.venture_id}"
    ids = list(MEM.get(index_key) or [])
    if pocket.id not in ids:
        ids.append(pocket.id)
    MEM[index_key] = ids
    return pocket


def rollup_1m_to_5m(children: list[Pocket]) -> Pocket | None:
    """Roll up material one-minute pockets into a single five-minute pocket."""
    children = [child for child in children if child.period == "1m" and not child.is_empty()]
    if not children:
        return None
    first, last = min(children, key=lambda p: p.window_start), max(children, key=lambda p: p.window_end)
    detail = [line for child in children for line in child.detail][:5]
    pocket = Pocket(
        venture_id=first.venture_id,
        period="5m",
        window_start=first.window_start,
        window_end=last.window_end,
        headline=first.notable or first.headline,
        detail=detail,
        agents_active=[agent for child in children for agent in child.agents_active],
        actuations=sum(child.actuations for child in children),
        money_delta_minor=sum(child.money_delta_minor for child in children),
        signals_in=sum(child.signals_in for child in children),
        insights_out=sum(child.insights_out for child in children),
        needs_founder=[need for child in children for need in child.needs_founder],
        notable=next((child.notable for child in children if child.notable), None),
        parent_pocket_id=None,
    )
    return write_pocket(pocket)

"""Genome models and validated seed-file loading."""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from app.config import REPO_ROOT


class AgentSpecRef(BaseModel):
    role: str
    name: str | None = None
    capability_ids: list[str] = Field(default_factory=list)


class WorkflowRef(BaseModel):
    id: str
    description: str = ""


class PhaseTemplate(BaseModel):
    id: str
    proves: str
    estimated_days: int = Field(ge=1)


class GenomeFitness(BaseModel):
    ventures: int = Field(default=0, ge=0)
    held: int = Field(default=0, ge=0)
    regressed: int = Field(default=0, ge=0)


class Genome(BaseModel):
    id: str = Field(pattern=r"^gen_[a-z0-9_]+$")
    version: str
    lineage: list[str] = Field(default_factory=list)
    archetype: str
    agents: list[AgentSpecRef] = Field(default_factory=list)
    workflows: list[WorkflowRef] = Field(default_factory=list)
    capability_bindings: dict[str, list[str]] = Field(default_factory=dict)
    default_channels: list[str] = Field(default_factory=list)
    default_phases: list[PhaseTemplate] = Field(default_factory=list)
    prompt_overrides: dict[str, str] = Field(default_factory=dict)
    thresholds: dict[str, float] = Field(default_factory=dict)
    playbook_overrides: dict = Field(default_factory=dict)
    fitness: GenomeFitness = Field(default_factory=GenomeFitness)
    provenance: str = "seed"


def genome_path(genome_id: str) -> Path:
    filename = genome_id.removeprefix("gen_") + ".yaml"
    return REPO_ROOT / "config" / "genomes" / filename


def load_genome(genome_id: str) -> Genome:
    path = genome_path(genome_id)
    if not path.exists():
        raise KeyError(f"unknown genome: {genome_id}")
    with path.open() as handle:
        return Genome.model_validate(yaml.safe_load(handle) or {})

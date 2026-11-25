"""Fork-on-write genome materialisation for a venture."""
from __future__ import annotations

from app import state
from app.genome.model import Genome, load_genome


def clone_genome_to_venture(venture_id: str, genome_id: str) -> Genome:
    """Persist an independent, deterministic seed snapshot for one venture."""
    if not venture_id:
        raise ValueError("venture_id is required")
    genome = load_genome(genome_id)
    clone = genome.model_copy(deep=True)
    # Store data, never the model instance, so a later mutation cannot affect
    # the seed or another venture.
    state.MEM[f"genome:{venture_id}"] = clone.model_dump(mode="json")
    return clone

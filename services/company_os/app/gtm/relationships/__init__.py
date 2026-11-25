"""GTM relationship engine — Loop 1 customer-acquisition spine (docs 115–117)."""

from app.gtm.relationships.icp import score_signal, load_icp
from app.gtm.relationships.state_machine import (
    RELATIONSHIP_STAGES,
    can_transition,
    transition,
)
from app.gtm.relationships.tone import lint_draft

__all__ = [
    "RELATIONSHIP_STAGES",
    "can_transition",
    "transition",
    "lint_draft",
    "score_signal",
    "load_icp",
]

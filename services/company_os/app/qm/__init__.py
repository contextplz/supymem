"""Company OS → QM agent runtime client (docs 51–58)."""

from app.qm.client import TurnResult, run_turn, signal_run
from app.qm.scope import ScopeRef, workspace_to_scope

__all__ = [
    "ScopeRef",
    "TurnResult",
    "run_turn",
    "signal_run",
    "workspace_to_scope",
]

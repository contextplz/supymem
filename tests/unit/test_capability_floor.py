from __future__ import annotations

import pytest
from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.evolution.propose import Proposal
from app.evolution.promote import promote
from core.ids import new_id


def test_untrusted_evidence_needs_founder_even_at_l4():
    p = Proposal(
        id=new_id("prop"),
        tier="config",
        hypothesis="do it",
        evidence=["hostile comment"],
        predicted_metric="engagement_rate",
        predicted_delta=0.1,
        rollback={"action": "undo"},
        authored_by_agent_id="a1",
        trust_of_evidence="untrusted",
    )
    with pytest.raises(PermissionError, match="needs_founder"):
        promote(p, target_kind="config", target_id="x", metrics_used=["engagement_rate", "stage_velocity_days"])

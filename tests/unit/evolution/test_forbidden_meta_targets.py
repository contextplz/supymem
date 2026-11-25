from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.evolution.propose import Proposal
from app.evolution.validate import validate


@pytest.mark.asyncio
async def test_memory_port_target_is_rejected_and_logged():
    proposal = Proposal(
        id="prop_forbidden",
        tier="code",
        hypothesis="update memory access",
        evidence=["ins_1"],
        predicted_metric="stage_velocity_days",
        predicted_delta=0.1,
        rollback={"action": "revert"},
        authored_by_agent_id="author",
        target_id="app/memory/port.py",
        venture_id="ven_1",
    )

    result = await validate(proposal, reviewer_id="reviewer")

    assert result.passed is False
    assert result.summary == "forbidden_meta_target"

from __future__ import annotations

import pytest
from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.evolution.propose import generate_proposal
from app.evolution.validate import STAGES, validate


@pytest.mark.asyncio
async def test_each_stage_can_fail():
    prop = generate_proposal({"kind": "config_change"}, authored_by="a1", evidence=["e"])
    for stage in STAGES:
        v = await validate(prop, reviewer_id="a2", fail_at=stage)
        assert v.passed is False
        assert v.failed_stage == stage

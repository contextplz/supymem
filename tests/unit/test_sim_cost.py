from __future__ import annotations

import pytest
from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.worldmodel.actions import Action
from app.worldmodel.simulate import simulate


@pytest.mark.asyncio
async def test_sim_under_200ms_and_one_flash():
    sim = await simulate("ven", Action(kind="org_replace"))
    assert sim.elapsed_ms < 200
    assert sim.llm_calls <= 1

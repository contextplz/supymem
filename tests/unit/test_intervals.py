from __future__ import annotations

import pytest
from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.worldmodel.actions import Action
from app.worldmodel.simulate import simulate
from app.worldmodel.state import snapshot


@pytest.mark.asyncio
async def test_every_prediction_has_interval_and_method():
    st = snapshot("ven", {"engagement_rate": 0.1})
    sim = await simulate("ven", Action(kind="config_change"), state=st)
    for p in sim.predictions:
        assert p.lo <= p.point <= p.hi
        assert p.method

from __future__ import annotations

import pytest
from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.gtm.scheduler import schedule_post


@pytest.mark.asyncio
async def test_dry_run_default_no_live_write(monkeypatch):
    monkeypatch.setenv("CHANNELS_DRY_RUN", "true")
    res = await schedule_post("landing", {"html": "<h1>hi</h1>"}, armed=True)
    assert res["ok"] and res["dry_run"] is True
    live = await schedule_post("x", {"text": "hi"}, armed=True, dry_run=False, warden_allow=False)
    assert live["ok"] is False

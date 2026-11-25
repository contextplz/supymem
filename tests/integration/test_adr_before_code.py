from __future__ import annotations

import pytest
from pathlib import Path
from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.build.adr import clear_adrs, create_adr, lock_adr
from app.build.dispatcher import dispatch
from app.build.workorder import WorkOrder
from coding_agents.consent import clear_consents, record_consent
from coding_agents.inhouse import InhouseAdapter


@pytest.mark.hosted_only
@pytest.mark.asyncio
async def test_dispatch_without_adr_refused(tmp_path):
    clear_adrs()
    clear_consents()
    record_consent("inhouse")
    wo = WorkOrder(id="wo1", venture_id="ven_noadr", title="x", components=["a"], files_expected=["inhouse_change.txt"])
    res = await dispatch(wo, workdir=tmp_path, adapter=InhouseAdapter())
    assert res.ok is False
    assert res.reason == "adr_not_locked"

    adr = create_adr("ven_noadr", title="t", stack=["fastapi"], protected_routes=["/api/me"])
    lock_adr(adr.id)
    res2 = await dispatch(wo, workdir=tmp_path, adapter=InhouseAdapter())
    assert res2.ok is True

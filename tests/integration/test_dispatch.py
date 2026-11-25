from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.build.dispatcher import dispatch, dispatch_with_timeout_kill, files_outside
from app.build.adr import clear_adrs, create_adr, lock_adr
from app.build.workorder import WorkOrder
from coding_agents.consent import clear_consents, record_consent
from coding_agents.inhouse import InhouseAdapter
from coding_agents.protocol import CodingResult


def test_files_outside_expected():
    assert files_outside(["a.py", "b.py"], ["a.py"]) == ["b.py"]


@pytest.mark.hosted_only
@pytest.mark.asyncio
async def test_outside_files_fail(tmp_path):
    clear_adrs(); clear_consents(); record_consent("inhouse")
    adr = create_adr("ven_d", title="t", stack=["py"], protected_routes=["/api/me"])
    lock_adr(adr.id)
    wo = WorkOrder(id="wo", venture_id="ven_d", title="t", components=["a"], files_expected=["ok.py"])
    fake = CodingResult(ok=True, changed_files=["evil.py"], write_flag_used=True)
    res = await dispatch(wo, workdir=tmp_path, adapter=InhouseAdapter(), fake_result=fake)
    assert res.ok is False
    assert "files_outside_expected" in res.reason


@pytest.mark.asyncio
async def test_timeout_kills_process_group(tmp_path):
    script = tmp_path / "sleep.py"
    script.write_text("import time\ntime.sleep(30)\n")
    res = await dispatch_with_timeout_kill([sys.executable, str(script)], cwd=tmp_path, timeout_s=0.3)
    assert res["killed"] is True

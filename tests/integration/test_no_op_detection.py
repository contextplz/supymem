from __future__ import annotations

import pytest
from pathlib import Path
from coding_agents.cursor import CursorAdapter
from coding_agents.protocol import CodingRequest


@pytest.mark.asyncio
async def test_missing_write_flag_caught(tmp_path):
    ad = CursorAdapter(recorded=[{"type": "finished"}])
    # Force allow_write but strip flag by monkeypatching build_argv
    req = CodingRequest(prompt="x", workdir=tmp_path, allow_write=True)
    orig = ad.build_argv
    ad.build_argv = lambda r: [x for x in orig(r) if x != "--force"]
    res = await ad.run(req)
    assert res.ok is False
    assert res.error == "missing_write_flag"

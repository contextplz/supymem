from __future__ import annotations

import pytest

from coding_agents.codex_cloud import CodexCloudAdapter
from coding_agents.claude_routine import ClaudeRoutineAdapter
from coding_agents.protocol import CodingRequest


@pytest.mark.asyncio
async def test_codex_cloud_dry_run(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_API_KEY", "sk-test")
    monkeypatch.setenv("CODEX_CLOUD_DRY_RUN", "1")
    ad = CodexCloudAdapter()
    res = await ad.run(CodingRequest(prompt="hi", workdir=tmp_path))
    assert res.ok
    assert res.session_id == "codex_cloud_dry"


@pytest.mark.asyncio
async def test_claude_routine_dry_run(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_ROUTINE_TOKEN", "tok")
    monkeypatch.setenv("CLAUDE_ROUTINE_ID", "rtn_1")
    monkeypatch.setenv("CLAUDE_ROUTINE_DRY_RUN", "1")
    ad = ClaudeRoutineAdapter()
    res = await ad.run(CodingRequest(prompt="hi", workdir=tmp_path))
    assert res.ok

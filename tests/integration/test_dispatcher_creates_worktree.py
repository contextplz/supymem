from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coding_agents.dispatcher import create_worktree, dispatch


def test_create_worktree_makes_dir(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    wt = create_worktree(repo, "wo/test1")
    assert wt.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", ["supplier scheduling", "home cooking lessons"])
async def test_local_dispatch_cannot_fall_back_to_stub(tmp_path: Path, monkeypatch, domain):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setenv("CODING_AGENT_LIVE", "0")
    monkeypatch.setattr("coding_agents.dispatcher.detect", lambda: [])
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    result = await dispatch(
        prompt=domain,
        repo=repo,
        branch="wo/inhouse",
        prefs=["inhouse"],
        mode="local",
    )
    assert not result.ok
    assert not result.changed_files
    assert "Settings > Keys and connections > Coding tools" in result.error
    assert not (repo.parent / "worktrees").exists()
    assert not (repo / "inhouse_change.txt").exists()

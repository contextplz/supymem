"""First Look site commits must never touch an enclosing git repository."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from tests.company_os_harness import load_company_os_app

load_company_os_app()


def _git(args: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged = {**os.environ, **(env or {})}
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=merged,
        check=True,
        capture_output=True,
        text=True,
    )


def _outer_snapshot(outer: Path) -> tuple[str, str, str]:
    head = _git(["rev-parse", "HEAD"], cwd=outer).stdout.strip()
    cached = _git(["diff", "--cached"], cwd=outer).stdout
    worktree = _git(["diff", "HEAD"], cwd=outer).stdout
    return head, cached, worktree


def _write_incomplete_git(repo: Path) -> None:
    """Reproduce the production stub: `.git` exists but is not a repository."""
    git_dir = repo / ".git"
    git_dir.mkdir(parents=True)
    (git_dir / "description").write_text("Unnamed repository\n", encoding="utf-8")
    (git_dir / "info").mkdir()
    (git_dir / "info" / "exclude").write_text("", encoding="utf-8")


def test_commit_site_does_not_touch_enclosing_repo(tmp_path: Path, monkeypatch) -> None:
    from app.firstlook.serve import write_site

    outer = tmp_path / "parent-repo"
    outer.mkdir()
    _git(["init"], cwd=outer)
    _git(["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "--allow-empty", "-m", "base"], cwd=outer)
    (outer / "unrelated.txt").write_text("do not commit me\n", encoding="utf-8")
    before = _outer_snapshot(outer)

    site_repo = outer / "workspaces" / "wsp_iso" / "site"
    _write_incomplete_git(site_repo)

    monkeypatch.setenv("FIRSTLOOK_DATA_DIR", str(outer))
    monkeypatch.setenv("GIT_DIR", str(outer / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(outer))

    path, _url = write_site("<html><body>look</body></html>", workspace_id="wsp_iso", version=1)
    assert path.is_file()

    after = _outer_snapshot(outer)
    assert after == before
    porcelain = _git(["status", "--porcelain"], cwd=outer).stdout
    assert "?? unrelated.txt" in porcelain
    log = _git(["log", "--oneline"], cwd=outer).stdout
    assert "first look" not in log

    isolated = site_repo.resolve()
    iso_env = {
        "GIT_DIR": str(isolated / ".git"),
        "GIT_WORK_TREE": str(isolated),
    }
    toplevel = _git(["rev-parse", "--show-toplevel"], cwd=isolated, env=iso_env).stdout.strip()
    assert Path(toplevel).resolve() == isolated
    iso_log = _git(["log", "-1", "--pretty=%s"], cwd=isolated, env=iso_env).stdout.strip()
    assert iso_log == "first look v1"
    tracked = _git(["ls-files"], cwd=isolated, env=iso_env).stdout
    assert "v1/index.html" in tracked
    assert "unrelated.txt" not in tracked

"""Read-only inventory of one reviewed venture workspace.

This helper lists names already on disk. It does not open Cursor, send a
prompt, or write venture files.
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

from desktop_runtime.paths import data_root, venture_workspace_root

_MAX_FILES = 2000
_SAMPLE = 30


def cursor_installed(*, applications: Path | None = None) -> bool:
    if shutil.which("cursor"):
        return True
    applications = applications or Path("/Applications")
    return (applications / "Cursor.app").is_dir()


class OwnedInspect:
    def __init__(self, *, venture_id: str, workspace_slug: str, root: Path | None = None):
        self.venture_id = venture_id
        self.workspace_slug = workspace_slug
        self.root = root or venture_workspace_root(workspace_slug, Path(data_root()))

    def _workspace(self) -> Path:
        root = self.root.resolve()
        workspace = (root / self.workspace_slug).resolve()
        if workspace.parent != root or not workspace.is_dir():
            raise ValueError("inspect_workspace_missing")
        marker = workspace / ".supymem-venture"
        try:
            owner = marker.read_text().strip() if marker.is_file() else ""
        except OSError as exc:
            raise ValueError("inspect_workspace_owner_mismatch") from exc
        if owner != self.venture_id:
            raise ValueError("inspect_workspace_owner_mismatch")
        return workspace

    @staticmethod
    def _git_status(workspace: Path) -> str | None:
        if not (workspace / ".git").is_dir():
            return None
        result = subprocess.run(
            ["/usr/bin/git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=workspace,
            env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home()), "GIT_CONFIG_NOSYSTEM": "1",
                 "GIT_TERMINAL_PROMPT": "0"},
            stdin=subprocess.DEVNULL, capture_output=True, timeout=15, check=False,
        )
        if result.returncode != 0 or len(result.stdout) > 65536:
            raise ValueError("inspect_git_status_failed")
        return hashlib.sha256(result.stdout).hexdigest()

    def inventory(self) -> dict:
        workspace = self._workspace()
        before = self._git_status(workspace)
        paths: list[str] = []
        truncated = False
        for path in workspace.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(workspace)
            if ".git" in relative.parts or relative.as_posix() == ".supymem-venture":
                continue
            if len(paths) >= _MAX_FILES:
                truncated = True
                break
            paths.append(relative.as_posix())
        paths.sort()
        after = self._git_status(workspace)
        if before != after:
            raise ValueError("inspect_workspace_changed")
        if not cursor_installed():
            raise ValueError("coding_provider_not_installed")
        digest = hashlib.sha256("\n".join(paths).encode()).hexdigest()
        return {
            "operation": "workspace_inspect_v1",
            "provider": "cursor",
            "cursor_edit_session": False,
            "cursor_present": True,
            "file_count": len(paths),
            "inventory_truncated": truncated,
            "inventory_sha256": digest,
            "sample_paths": paths[:_SAMPLE],
            "git_present": before is not None,
            "git_unchanged": True,
            "workspace_marker_verified": True,
            "writes_performed": False,
        }

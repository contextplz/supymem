"""One fixed terminal operation in a reviewed Supymem venture workspace."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import subprocess
import time
from pathlib import Path

import psutil
from core.artifact_policy import _open_at, _parent, read_file, replace_file, safe_name

from desktop_runtime.paths import data_root, venture_workspace_root


def workspace_file_scope(workspace: Path, name: str) -> tuple[Path, str]:
    if name.startswith(".git/"):
        workspace, name = workspace / ".git", name[5:]
    return workspace, safe_name(name)


def read_workspace_file(workspace: Path, name: str, limit: int, *, missing=None) -> bytes:
    root, name = workspace_file_scope(workspace, name)
    with _parent(root, name) as (parent, leaf):
        try:
            fd = _open_at(parent, leaf, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        except FileNotFoundError:
            if missing is not None:
                return missing
            raise
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise ValueError("coding_changed_path_invalid")
            if before.st_size > limit:
                raise ValueError("coding_workspace_state_bounds")
            blob = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
            if (len(blob) != before.st_size or after.st_nlink != 1
                    or (before.st_mtime_ns, before.st_ctime_ns) != (after.st_mtime_ns, after.st_ctime_ns)):
                raise ValueError("coding_changed_path_invalid")
            return blob


def restore_workspace_file(workspace: Path, name: str, blob: bytes) -> None:
    root, name = workspace_file_scope(workspace, name)
    with _parent(root, name, create=True) as (parent, leaf):
        temporary = ".restore-" + secrets.token_hex(12)
        fd = _open_at(parent, temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(blob)
            if os.name == "nt":
                os.rename(parent / temporary, parent / leaf)
            else:
                try:
                    os.stat(leaf, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    os.rename(temporary, leaf, src_dir_fd=parent, dst_dir_fd=parent)
                else:
                    raise ValueError("coding_changed_path_invalid")
        finally:
            try:
                if os.name == "nt":
                    os.unlink(parent / temporary)
                else:
                    os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass


class OwnedTerminal:
    def __init__(self, *, venture_id: str, workspace_slug: str, tracker, authorize):
        self.venture_id = venture_id
        self.workspace_slug = workspace_slug
        self.tracker = tracker
        self.authorize = authorize

    def workspace(self) -> Path:
        if not self.venture_id or any(char in self.venture_id for char in "/\\"):
            raise ValueError("terminal_workspace_owner_unconfirmed")
        root = venture_workspace_root(self.workspace_slug, Path(data_root()))
        root.mkdir(parents=True, exist_ok=True)
        root = root.resolve()
        selected = root / self.workspace_slug
        if selected.is_symlink():
            raise ValueError("terminal_workspace_scope_invalid")
        workspace = selected.resolve()
        if workspace.parent != root:
            raise ValueError("terminal_workspace_scope_invalid")
        existed = workspace.exists()
        workspace.mkdir(mode=0o700, exist_ok=True)
        marker = workspace / ".supymem-venture"
        if marker.is_symlink():
            raise ValueError("terminal_workspace_owner_unconfirmed")
        if marker.exists():
            if read_file(workspace, marker.name).decode().strip() != self.venture_id:
                raise ValueError("terminal_workspace_owner_mismatch")
        else:
            if existed and any(workspace.iterdir()):
                raise ValueError("terminal_workspace_owner_unconfirmed")
            replace_file(workspace, marker.name, (self.venture_id + "\n").encode())
        replace_file(data_root(create=True), f"ventures/{self.venture_id}/workspace.json",
            json.dumps({"path": str(workspace), "venture_id": self.venture_id}).encode())
        return workspace

    # Kept for callers compiled against the first terminal-only runtime.
    def _workspace(self) -> Path:
        return self.workspace()

    def _run(self, argv: list[str], workspace: Path, deadline: float):
        if not self.authorize() or time.monotonic() >= deadline:
            raise ValueError("terminal_authority_unconfirmed")
        env = {
            "HOME": str(Path.home()),
            "PATH": "/usr/bin:/bin",
            "LANG": "C",
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
        }
        process = psutil.Popen(argv, cwd=str(workspace), env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True)
        self.tracker.register(process)
        timeout = max(0.1, min(30.0, deadline - time.monotonic()))
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except (psutil.TimeoutExpired, subprocess.TimeoutExpired):
            process.terminate()
            try:
                process.wait(timeout=3)
            except (psutil.TimeoutExpired, subprocess.TimeoutExpired):
                process.kill()
                process.wait(timeout=3)
            raise ValueError("terminal_process_timeout") from None
        if len(stdout) > 65536 or len(stderr) > 65536:
            raise ValueError("terminal_output_bounds")
        return process.returncode, stdout, stderr

    def git_status(self, deadline: float) -> dict:
        workspace = self.workspace()
        if not (workspace / ".git").is_dir():
            code, _stdout, _stderr = self._run(
                ["/usr/bin/git", "init", "--quiet", "--initial-branch=main"], workspace, deadline)
            if code != 0:
                raise ValueError("terminal_git_init_failed")
        code, stdout, _stderr = self._run(
            ["/usr/bin/git", "status", "--short", "--branch", "--untracked-files=no"],
            workspace, deadline)
        if code != 0 or not stdout.startswith(b"## "):
            raise ValueError("terminal_git_status_failed")
        marker = read_file(workspace, ".supymem-venture").decode().strip() == self.venture_id
        return {
            "operation": "git_status_v1",
            "exit_code": code,
            "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
            "stdout_bytes": len(stdout),
            "workspace_marker_verified": marker,
        }

"""Caller-owned bounded local failure diagnostics, never founder-facing prose."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4


def save_failure(venture_id: str, action: str, error: Exception) -> Path | None:
    root = os.environ.get("COMPANY_OS_DATA_DIR")
    if not root:
        return None
    try:
        import fcntl

        folder = Path(root) / "autopilot-private" / hashlib.sha256(venture_id.encode()).hexdigest()[:32]
        folder.mkdir(parents=True, mode=0o700, exist_ok=True)
        if folder.is_symlink() or folder.stat().st_mode & 0o077:
            return None
        exclusive = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        lock_flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        lock_fd = os.open(folder / ".lock", lock_flags, 0o600)
        with os.fdopen(lock_fd, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if len(list(folder.glob("*.json"))) >= 64:
                return None
            path = folder / f"{uuid4().hex}.json"
            # Exclusive creation prevents overwriting/following an existing file.
            fd = os.open(path, exclusive, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump({"v": 1, "action": action[:80], "reason": str(error)[:200]}, stream)
            return path
    except (ImportError, OSError, ValueError, TypeError):
        return None

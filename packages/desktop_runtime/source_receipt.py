"""Keep an immutable, local transport receipt for a stopped coding order.

This is source custody, not an accepted build, QA result, or hosting authority.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from contextlib import ExitStack
from pathlib import Path

from core.artifact_policy import DEFAULT_LIMITS
from core.artifact_snapshot import SKIP_DIRS, freeze_source, manifest_digest
from desktop_runtime.paths import data_root, venture_workspace_root

if os.name != "nt":
    import fcntl

_ID = re.compile(r"[a-z][a-z0-9_]{2,79}\Z")
_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,79}\Z")


def _write_atomic(path: Path, content: bytes) -> None:
    if os.name == "nt":
        from app.core.local_windows import atomic_write

        atomic_write(path, content)
        return
    descriptor, temporary = tempfile.mkstemp(prefix=".source-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def capture(*, venture_id: str, workspace_id: str, actor_id: str,
            device_id: str, plan_task_id: str, workspace_slug: str,
            order_id: str, execution_id: str, provider: str) -> dict:
    """Freeze exactly the reviewed venture workspace once per stopped order.

    Repeated calls return the original receipt. A changed workspace never
    rewrites the archived version or changes the order's source identity.
    """
    if not all(isinstance(value, str) and _ID.fullmatch(value) for value in
               (venture_id, workspace_id, device_id, plan_task_id, order_id, execution_id)):
        raise ValueError("source_receipt_identity_invalid")
    if not isinstance(actor_id, str) or not 1 <= len(actor_id) <= 256:
        raise ValueError("source_receipt_actor_invalid")
    if not isinstance(workspace_slug, str) or not _SLUG.fullmatch(workspace_slug):
        raise ValueError("source_receipt_workspace_invalid")
    if provider not in {"codex", "claude", "cursor", "opencode"}:
        raise ValueError("source_receipt_provider_unsupported")
    root = venture_workspace_root(workspace_slug, Path(data_root())).resolve()
    workspace_path = root / workspace_slug
    if workspace_path.is_symlink() or not workspace_path.is_dir():
        raise ValueError("source_receipt_workspace_invalid")
    workspace = workspace_path.resolve()
    if workspace.parent != root or (workspace / ".supymem-venture").read_text().strip() != venture_id:
        raise ValueError("source_receipt_workspace_owner_mismatch")

    directory = Path(data_root(create=True)) / "ventures" / venture_id / "coding_sources" / order_id
    if os.name == "nt":
        from app.core.local_process import check_private, file_lock, open_private
        from app.core.local_windows import private_directory

        root = Path(data_root(create=True))
        private_directory(root)
        current = root
        for part in directory.relative_to(root).parts:
            current = current / part
            private_directory(current)
    else:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = directory / ".lock"
    with ExitStack() as locks:
        descriptor = (open_private(lock_path, os.O_CREAT | os.O_RDWR) if os.name == "nt" else
                      os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600))
        lock = locks.enter_context(os.fdopen(descriptor, "rb"))
        if os.name == "nt":
            check_private(lock)
            locks.enter_context(file_lock(lock))
        else:
            os.fchmod(lock.fileno(), 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
        receipt_path = directory / "receipt.json"
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text())
            archive_path = directory / receipt["archive_name"]
            if (receipt.get("order_id") != order_id or receipt.get("venture_id") != venture_id
                    or receipt.get("workspace_id") != workspace_id
                    or receipt.get("actor_sha256") != hashlib.sha256(actor_id.encode()).hexdigest()
                    or receipt.get("device_id") != device_id
                    or receipt.get("plan_task_id") != plan_task_id
                    or receipt.get("execution_id") != execution_id or receipt.get("provider") != provider
                    or not re.fullmatch(r"[a-f0-9]{64}\.zip", receipt.get("archive_name", ""))
                    or not archive_path.is_file()
                    or hashlib.sha256(archive_path.read_bytes()).hexdigest() != receipt.get("sha256")):
                raise ValueError("source_receipt_reconciliation_required")
            return receipt

        # Generated transport archives and old local-review bundles are not
        # application source. The source manifest still includes public assets.
        manifest, archive = freeze_source(workspace, skip_dirs=SKIP_DIRS | {"artifacts", ".reference"})
        sha256 = hashlib.sha256(archive).hexdigest()
        archive_name = f"{sha256}.zip"
        receipt = {"version": 1, "venture_id": venture_id, "workspace_id": workspace_id,
                   "actor_sha256": hashlib.sha256(actor_id.encode()).hexdigest(),
                   "device_id": device_id, "plan_task_id": plan_task_id, "order_id": order_id,
                   "execution_id": execution_id, "provider": provider,
                   "workspace_slug": workspace_slug, "sha256": sha256,
                   "manifest_sha256": manifest_digest(manifest),
                   "file_count": len(manifest), "size_bytes": len(archive),
                   "archive_name": archive_name, "qa_verified": False}
        _write_atomic(directory / archive_name, archive)
        _write_atomic(receipt_path, json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode())
        return receipt


def load_archive(receipt: dict) -> bytes:
    """Read only the native-owned immutable archive named by a captured receipt."""
    venture_id, order_id = receipt.get("venture_id"), receipt.get("order_id")
    sha, size = receipt.get("sha256"), receipt.get("size_bytes")
    if (not all(isinstance(value, str) and _ID.fullmatch(value) for value in (venture_id, order_id))
            or not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{64}", sha)
            or type(size) is not int or not 0 < size <= DEFAULT_LIMITS.max_archive_bytes
            or receipt.get("archive_name") != sha + ".zip"):
        raise ValueError("source_receipt_identity_invalid")
    directory = Path(data_root(create=False)) / "ventures" / venture_id / "coding_sources" / order_id
    saved = directory / "receipt.json"
    archive = directory / receipt["archive_name"]
    if directory.is_symlink() or saved.is_symlink() or archive.is_symlink():
        raise ValueError("source_receipt_reconciliation_required")
    if json.loads(saved.read_text()) != receipt or archive.stat().st_size != size:
        raise ValueError("source_receipt_reconciliation_required")
    blob = archive.read_bytes()
    if hashlib.sha256(blob).hexdigest() != sha:
        raise ValueError("source_receipt_reconciliation_required")
    return blob

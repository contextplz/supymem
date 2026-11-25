"""Private immutable page artifacts, with receipts bound to their owner and bytes."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re

from app.builder.model_proxy import BuildModelError
from app.config import data_root
from app.core.local_process import check_private, open_private
from app.core.local_secrets import _atomic_write
from core.artifact_policy import DEFAULT_LIMITS


class LocalArtifactStore:
    @classmethod
    def configured(cls):
        return cls()

    def _path(self, scope, kind, object_digest):
        if (kind != "raw" or not isinstance(scope, dict)
                or set(scope) != {"workspace_id", "venture_id", "app_id", "import_id"}
                or any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value)
                       for value in scope.values())
                or not isinstance(object_digest, str) or not re.fullmatch(r"[a-f0-9]{64}", object_digest)):
            raise BuildModelError("pagebuilder_artifact_scope_invalid")
        root = data_root()
        directory = root
        for part in ("pagebuilder", "artifacts", scope["workspace_id"], scope["venture_id"], scope["app_id"], scope["import_id"]):
            directory = directory / part
            directory.mkdir(mode=0o700, exist_ok=True)
            if directory.is_symlink() or not directory.resolve().is_relative_to(root):
                raise BuildModelError("pagebuilder_artifact_scope_invalid")
        return directory / (object_digest + ".zip")

    def persist_import(self, blob, scope, *, kind, object_digest):
        if (not isinstance(blob, bytes) or not 0 < len(blob) <= DEFAULT_LIMITS.max_archive_bytes
                or hashlib.sha256(blob).hexdigest() != object_digest):
            raise BuildModelError("pagebuilder_artifact_invalid")
        try:
            path = self._path(scope, kind, object_digest)
            receipt = {"storage": "local", "kind": kind, "object_digest": object_digest,
                       "sha256": object_digest, "version_id": object_digest, "size_bytes": len(blob), "scope": dict(scope)}
            if path.exists() or path.is_symlink():
                if self.read_import(receipt, scope, kind=kind, object_digest=object_digest) != blob:
                    raise BuildModelError("pagebuilder_artifact_changed")
            else:
                _atomic_write(path, blob)
            return receipt
        except OSError as exc:
            raise BuildModelError("pagebuilder_artifact_write_failed") from exc

    def read_import(self, receipt, scope, *, kind, object_digest):
        if (not isinstance(receipt, dict) or receipt.get("storage") != "local"
                or receipt.get("kind") != kind or receipt.get("object_digest") != object_digest
                or receipt.get("version_id") != object_digest
                or receipt.get("sha256") != object_digest or receipt.get("scope") != scope
                or type(receipt.get("size_bytes")) is not int
                or not 0 < receipt["size_bytes"] <= DEFAULT_LIMITS.max_archive_bytes):
            raise BuildModelError("pagebuilder_artifact_receipt_invalid")
        try:
            path = self._path(scope, kind, object_digest)
            descriptor = open_private(path, os.O_RDONLY)
            with os.fdopen(descriptor, "rb") as source:
                check_private(source)
                blob = source.read(receipt["size_bytes"] + 1)
        except (OSError, ValueError) as exc:
            raise BuildModelError("pagebuilder_artifact_read_failed") from exc
        if len(blob) != receipt["size_bytes"] or hashlib.sha256(blob).hexdigest() != object_digest:
            raise BuildModelError("pagebuilder_artifact_changed")
        return blob

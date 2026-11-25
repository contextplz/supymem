"""Deterministic source ZIPs using the existing phase2 artifact policy.

A valid snapshot proves transport integrity only, never code correctness or QA.
"""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import re
import zipfile

from .artifact_policy import DEFAULT_LIMITS, ArtifactLimits, ArtifactRejected, extract_zip, inspect_blob, read_file, safe_name, tree_manifest

SKIP_DIRS = frozenset({".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache", ".next", "dist", "build", ".tmux", ".tmp"})


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def manifest_digest(manifest: list[dict]) -> str:
    return hashlib.sha256(canonical(manifest)).hexdigest()


def validate_manifest(value: object) -> list[dict]:
    limits = DEFAULT_LIMITS
    if not isinstance(value, list) or not 1 <= len(value) <= limits.max_files:
        raise ArtifactRejected("artifact_manifest_invalid")
    previous, total, components = "", 0, {}
    from .artifact_policy import _record_components
    for item in value:
        if not isinstance(item, dict) or set(item) != {"path", "size_bytes", "sha256"}:
            raise ArtifactRejected("artifact_manifest_invalid")
        name = safe_name(item["path"])
        _record_components(name, components)
        if name <= previous or type(item["size_bytes"]) is not int or not 0 <= item["size_bytes"] <= limits.max_file_bytes:
            raise ArtifactRejected("artifact_manifest_invalid")
        if not isinstance(item["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"]):
            raise ArtifactRejected("artifact_manifest_invalid")
        previous, total = name, total + item["size_bytes"]
    if total > limits.max_total_bytes:
        raise ArtifactRejected("artifact_total_too_large")
    return value


def freeze_source(root: Path, *, exact_secrets: tuple[str, ...] = (),
                  skip_dirs: frozenset[str] = SKIP_DIRS,
                  omit_credential_paths: bool = False) -> tuple[list[dict], bytes]:
    manifest = sorted(tree_manifest(root, skip_dirs=skip_dirs, exact_secrets=exact_secrets,
                                    omit_credential_paths=omit_credential_paths), key=lambda x: x["path"])
    validate_manifest(manifest)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for item in manifest:
            blob = read_file(root, item["path"], exact_secrets=exact_secrets)
            if len(blob) != item["size_bytes"] or hashlib.sha256(blob).hexdigest() != item["sha256"]:
                raise ArtifactRejected("artifact_file_changed")
            info = zipfile.ZipInfo(item["path"], date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system, info.external_attr = 3, 0o100600 << 16
            archive.writestr(info, blob)
            if output.tell() > DEFAULT_LIMITS.max_archive_bytes:
                raise ArtifactRejected("artifact_archive_too_large")
    raw = output.getvalue()
    if len(raw) > DEFAULT_LIMITS.max_archive_bytes:
        raise ArtifactRejected("artifact_archive_too_large")
    after = sorted(tree_manifest(root, skip_dirs=skip_dirs, exact_secrets=exact_secrets,
                                 omit_credential_paths=omit_credential_paths), key=lambda x: x["path"])
    if after != manifest:
        raise ArtifactRejected("artifact_snapshot_changed")
    return manifest, raw


def import_source(blob: bytes, destination: Path, *, manifest: list[dict], sha256: str, size_bytes: int,
                  exact_secrets: tuple[str, ...] = ()) -> list[dict]:
    validate_manifest(manifest)
    if (type(size_bytes) is not int or size_bytes != len(blob) or size_bytes > DEFAULT_LIMITS.max_archive_bytes
            or hashlib.sha256(blob).hexdigest() != sha256):
        raise ArtifactRejected("artifact_archive_mismatch")
    # ZIP comments/trailing bytes are stored too: scan the complete envelope,
    # not only the extracted members, before persisting any remote bytes.
    inspect_blob(blob, limits=ArtifactLimits(max_file_bytes=DEFAULT_LIMITS.max_archive_bytes), exact_secrets=exact_secrets)
    actual = extract_zip(blob, destination, exact_secrets=exact_secrets)
    if sorted(actual, key=lambda x: x["path"]) != manifest:
        import shutil
        shutil.rmtree(destination)
        raise ArtifactRejected("artifact_manifest_mismatch")
    return actual

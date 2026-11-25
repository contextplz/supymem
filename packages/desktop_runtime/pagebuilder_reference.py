"""Pinned pagebuilder bytes are review data, never execution or source acceptance."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tempfile
import zipfile
from dataclasses import replace
from pathlib import Path

from core.artifact_policy import DEFAULT_LIMITS, ArtifactRejected, directory_fd, extract_zip, read_file, tree_manifest
from core.artifact_snapshot import canonical, import_source, manifest_digest, validate_manifest

_IDENTITIES = {"job_id", "revision_id", "workspace_id", "venture_id", "app_id"}
_DIGESTS = {"source_sha256", "source_manifest_digest", "spec_sha256", "reference_sha256"}
_SIZES = {"source_size_bytes", "reference_size_bytes"}
_FIELDS = _IDENTITIES | _DIGESTS | _SIZES | {"schema_version", "variant"}
_MEMBERS = {"source.zip", "manifest.json", "SPEC.md", "REFERENCE.json"}
_OUTER_LIMITS = replace(DEFAULT_LIMITS, max_files=4, max_file_bytes=DEFAULT_LIMITS.max_archive_bytes)


def validate_binding(value: object, *, venture_id: str, workspace_id: str | None = None) -> dict:
    if (not isinstance(value, dict) or set(value) != _FIELDS
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or type(value["variant"]) is not int or not 0 <= value["variant"] <= 2
            or any(not isinstance(value[key], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value[key]) for key in _IDENTITIES)
            or any(not isinstance(value[key], str) or not re.fullmatch(r"[a-f0-9]{64}", value[key]) for key in _DIGESTS)
            or any(type(value[key]) is not int or not 1 <= value[key] <= DEFAULT_LIMITS.max_archive_bytes for key in _SIZES)
            or value["venture_id"] != venture_id
            or (workspace_id is not None and value["workspace_id"] != workspace_id)):
        raise ArtifactRejected("pagebuilder_binding_invalid")
    return dict(value)


def verify_staged(workspace: Path, relative: str, expected: list[dict]) -> None:
    if sorted(tree_manifest(workspace / relative), key=lambda item: item["path"]) != expected:
        raise ArtifactRejected("pagebuilder_reference_changed")


def stage_reference(workspace: Path, binding: dict, blob: bytes) -> tuple[str, list[dict]]:
    binding = validate_binding(binding, venture_id=binding.get("venture_id"))
    if (len(blob) != binding["reference_size_bytes"]
            or hashlib.sha256(blob).hexdigest() != binding["reference_sha256"]):
        raise ArtifactRejected("pagebuilder_reference_mismatch")
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            names = [info.filename for info in archive.infolist()]
            if len(names) != 4 or set(names) != _MEMBERS:
                raise ArtifactRejected("pagebuilder_reference_members_invalid")
    except zipfile.BadZipFile:
        raise ArtifactRejected("artifact_archive_invalid") from None

    relative = f".reference/pagebuilder/{binding['job_id']}/variant-{binding['variant']}"
    parent = workspace
    # Existing artifact policy opens each component without following links.
    for part in relative.split("/")[:-1]:
        with directory_fd(parent) as fd:
            try:
                os.mkdir(part, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
        parent = parent / part
    with directory_fd(parent), tempfile.TemporaryDirectory(prefix=".reference-", dir=parent) as temporary:
        staged = Path(temporary) / "reference"
        extract_zip(blob, staged, limits=_OUTER_LIMITS)
        expected_identity = {key: value for key, value in binding.items() if key not in {"reference_sha256", "reference_size_bytes"}}
        reference_raw = read_file(staged, "REFERENCE.json")
        if reference_raw != canonical(expected_identity):
            raise ArtifactRejected("pagebuilder_reference_identity_changed")
        manifest_raw = read_file(staged, "manifest.json")
        if len(manifest_raw) > 1024 * 1024:
            raise ArtifactRejected("pagebuilder_manifest_bounds")
        try:
            manifest = validate_manifest(json.loads(manifest_raw))
        except (ValueError, UnicodeDecodeError):
            raise ArtifactRejected("pagebuilder_manifest_invalid") from None
        if manifest_raw != canonical(manifest) or manifest_digest(manifest) != binding["source_manifest_digest"]:
            raise ArtifactRejected("pagebuilder_manifest_mismatch")
        # Do not materialize provider rule/settings files as hidden instructions.
        rules = {"agents.md", "claude.md", "claude.local.md", ".claude", ".codex", ".mcp.json"}
        if any(any(part.casefold() in rules for part in item["path"].split("/")) for item in manifest):
            raise ArtifactRejected("pagebuilder_reference_rules_denied")
        spec = read_file(staged, "SPEC.md")
        if not 0 < len(spec) <= 65536 or hashlib.sha256(spec).hexdigest() != binding["spec_sha256"]:
            raise ArtifactRejected("pagebuilder_spec_mismatch")
        try:
            spec.decode("utf-8", "strict")
        except UnicodeDecodeError:
            raise ArtifactRejected("pagebuilder_spec_invalid") from None
        source = read_file(staged, "source.zip", limits=_OUTER_LIMITS)
        import_source(source, staged / "source", manifest=manifest,
                      sha256=binding["source_sha256"], size_bytes=binding["source_size_bytes"])
        (staged / "source.zip").unlink()  # Avoid duplicating the archive in genuine source intake.
        expected = sorted(tree_manifest(staged), key=lambda item: item["path"])
        destination = workspace / relative
        if destination.exists() or destination.is_symlink():
            verify_staged(workspace, relative, expected)
        else:
            with directory_fd(parent) as fd, directory_fd(staged.parent) as source_fd:
                os.rename(staged.name, destination.name, src_dir_fd=source_fd, dst_dir_fd=fd)
        verify_staged(workspace, relative, expected)
    return relative, expected

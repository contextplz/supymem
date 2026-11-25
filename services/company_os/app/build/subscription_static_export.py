"""Deterministic public static projection; source custody remains unchanged."""
from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from core.artifact_policy import ArtifactRejected, extract_zip, read_file
from core.artifact_snapshot import freeze_source, manifest_digest
from core.static_qa_contracts import STATIC_MEDIA

EXPORT_POLICY = "subscription_static_public_projection_v2"
PRIVATE_DOCUMENTS = {"README.md", "DEPLOY.md", "AGENTS.md", "LICENSE", "LICENSE.md", ".gitignore", ".supymem-venture", "CODING_CARD.md"}


def private_path(name: str) -> bool:
    return (name in PRIVATE_DOCUMENTS or name.startswith("review/")
            or name.startswith("docs/") and name.endswith(".md"))


def export(blob: bytes, *, source_sha256: str, source_manifest_digest: str, venture_id: str):
    if hashlib.sha256(blob).hexdigest() != source_sha256:
        raise ArtifactRejected("subscription_static_source_archive_mismatch")
    with TemporaryDirectory(prefix="supymem-static-source-") as directory:
        base = Path(directory).resolve()
        source, public = base / "source", base / "public"
        manifest = sorted(extract_zip(blob, source), key=lambda item: item["path"])
        if manifest_digest(manifest) != source_manifest_digest:
            raise ArtifactRejected("subscription_static_source_manifest_mismatch")
        public.mkdir()
        omitted = []
        for item in manifest:
            name = item["path"]
            if private_path(name):
                if name == ".supymem-venture" and read_file(source, name).decode().strip() != venture_id:
                    raise ArtifactRejected("subscription_static_source_owner_mismatch")
                omitted.append(name)
                continue
            if PurePosixPath(name).suffix.lower() not in STATIC_MEDIA:
                raise ArtifactRejected("subscription_static_non_public_file")
            target = public / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(read_file(source, name))
        if not (public / "index.html").is_file():
            raise ArtifactRejected("subscription_static_index_missing")
        public_manifest, archive = freeze_source(public)
        projection = {"export_policy": EXPORT_POLICY, "original_source_sha256": source_sha256,
                      "original_manifest_digest": source_manifest_digest, "omitted_paths": omitted,
                      "manifest_digest": manifest_digest(public_manifest),
                      "archive_sha256": hashlib.sha256(archive).hexdigest(), "size_bytes": len(archive),
                      "manifest": public_manifest}
        return archive, projection

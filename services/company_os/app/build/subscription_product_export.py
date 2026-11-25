"""Disabled product-source projection; no execution, QA or hosting authority.

Only the reviewed Python product directory may reach the platform runtime.
Source-supplied scaffold, dependency and build configuration cannot be copied.
"""
from __future__ import annotations

from core.deployment import infrastructure
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory

from core.application_source import validate_product_manifest
from core.artifact_policy import ArtifactRejected, extract_zip, read_file
from core.artifact_snapshot import freeze_source, manifest_digest

EXPORT_POLICY = "subscription_product_source_projection_v1"
PRIVATE_DOCUMENTS = {"README.md", "DEPLOY.md", "AGENTS.md", "LICENSE", "LICENSE.md", ".gitignore", ".supymem-venture"}


def export(blob: bytes, *, source_sha256: str, source_manifest_digest: str, venture_id: str):
    """Revalidate immutable custody bytes and return a product-only archive.

    The caller must authorize the original custody receipt and approved task.
    A structurally valid projection is still unverified executable source.
    """
    if hashlib.sha256(blob).hexdigest() != source_sha256:
        raise ArtifactRejected("subscription_product_source_archive_mismatch")
    with TemporaryDirectory(prefix=infrastructure('supymem-product-source-')) as directory:
        base = Path(directory).resolve()
        source, product = base / "source", base / "projection"
        manifest = sorted(extract_zip(blob, source), key=lambda item: item["path"])
        if manifest_digest(manifest) != source_manifest_digest:
            raise ArtifactRejected("subscription_product_source_manifest_mismatch")
        product.mkdir()
        omitted = []
        projected = []
        for item in manifest:
            name = item["path"]
            if name in PRIVATE_DOCUMENTS or name.startswith("docs/") and name.endswith(".md"):
                if name == ".supymem-venture" and read_file(source, name).decode().strip() != venture_id:
                    raise ArtifactRejected("subscription_product_source_owner_mismatch")
                omitted.append(name)
                continue
            projected.append(item)
        validate_product_manifest(projected)
        for item in projected:
            target = product / item["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(read_file(source, item["path"]))
        product_manifest, archive = freeze_source(product)
        projection = {"export_policy": EXPORT_POLICY, "artifact_profile": "founder_product_v1",
                      "original_source_sha256": source_sha256,
                      "original_manifest_digest": source_manifest_digest, "omitted_paths": omitted,
                      "manifest_digest": manifest_digest(product_manifest),
                      "archive_sha256": hashlib.sha256(archive).hexdigest(), "size_bytes": len(archive),
                      "manifest": product_manifest,
                      "runtime_qa_verified": False, "hosting_authorized": False}
        return archive, projection

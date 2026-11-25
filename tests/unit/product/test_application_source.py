"""Actual source archive import cannot overwrite the platform image context."""
import hashlib

import pytest

from core.application_source import import_product
from core.artifact_policy import ArtifactRejected
from core.artifact_snapshot import freeze_source


def test_product_transport_preserves_bytes_and_rejects_platform_override(tmp_path):
    source = tmp_path / "source"
    (source / "product/static").mkdir(parents=True)
    (source / "product/__init__.py").write_text("")
    code = b"raise RuntimeError('assembly must not execute generated source')\n"
    (source / "product/routes.py").write_bytes(code)
    (source / "product/static/index.html").write_text("<h1>Labelled QA asset</h1>")
    manifest, blob = freeze_source(source)
    args = dict(manifest=manifest, sha256=hashlib.sha256(blob).hexdigest(), size_bytes=len(blob))
    target = tmp_path / "context"
    import_product(blob, target, **args)
    assert (target / "product/routes.py").read_bytes() == code
    assert not (target / "app").exists()
    with pytest.raises(ArtifactRejected, match="archive_mismatch"):
        import_product(blob + b"changed", tmp_path / "tampered", **args)
    assert not (tmp_path / "tampered").exists()
    (source / "Dockerfile").write_text("FROM unapproved\nRUN unapproved\n")
    manifest, blob = freeze_source(source)
    with pytest.raises(ArtifactRejected, match="platform_override"):
        import_product(blob, tmp_path / "override", manifest=manifest,
                       sha256=hashlib.sha256(blob).hexdigest(), size_bytes=len(blob))
    assert not (tmp_path / "override").exists()


def test_product_transport_rejects_a_runtime_control_secret(tmp_path):
    source = tmp_path / "source"
    (source / "product/static").mkdir(parents=True)
    (source / "product/__init__.py").write_text("")
    (source / "product/routes.py").write_text('TOKEN = "runtime-control-secret"\n')
    (source / "product/static/index.html").write_text("<h1>Product</h1>")
    manifest, blob = freeze_source(source)
    with pytest.raises(ArtifactRejected):
        import_product(blob, tmp_path / "destination", manifest=manifest,
                       sha256=hashlib.sha256(blob).hexdigest(), size_bytes=len(blob),
                       exact_secrets=("runtime-control-secret",))


def test_product_transport_accepts_bundled_javascript_modules(tmp_path):
    source = tmp_path / "source"
    (source / "product/static").mkdir(parents=True)
    (source / "product/__init__.py").write_text("")
    (source / "product/routes.py").write_text("from fastapi import APIRouter\nrouter = APIRouter()\n")
    (source / "product/static/index.html").write_text('<script type="module" src="/app.mjs"></script>')
    (source / "product/static/app.mjs").write_text("export const ready = true;\n")
    manifest, blob = freeze_source(source)
    target = tmp_path / "context"
    import_product(blob, target, manifest=manifest,
                   sha256=hashlib.sha256(blob).hexdigest(), size_bytes=len(blob))
    assert (target / "product/static/app.mjs").read_text() == "export const ready = true;\n"

"""Closed source-origin wire and deterministic public bytes, no provider calls."""
import hashlib
import io
from types import SimpleNamespace
import zipfile

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def modules(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/boundary.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(tmp_path / "memory.db"))
    load_company_os_app()


def test_server_origin_cannot_borrow_native_or_untyped_origin(modules):
    from app.pagebuilder_admission import owns
    spec = {"lifecycle_version": 3, "source_origin": "pagebuilder_candidate_v1"}
    assert owns(SimpleNamespace(trigger="pagebuilder_source_qa"), spec)
    assert not owns(SimpleNamespace(trigger="coding_source_import"), spec)
    assert not owns(SimpleNamespace(trigger="pagebuilder_source_qa"), {"lifecycle_version": 3})
    assert not owns(SimpleNamespace(trigger="pagebuilder_source_qa"), spec | {"lifecycle_version": 2})


@pytest.mark.parametrize("value", [False, 0, 1, "true", None])
def test_qa_approval_requires_actual_click_boolean(modules, value):
    from app.pagebuilder_admission.router import Admission, Review, Publish
    for model, fields in [(Admission, {"job_id": "job_one", "variant": 0, "plan_id": "plan_one", "plan_task_id": "task_one",
        "quote_id": "q_one", "quote_digest": "a" * 64, "approve_qa_ceiling_micro": 10}),
        (Review, {"source_artifact_id": "artifact_one", "report_digest": "a" * 64, "decision": "accept"}), (Publish, {})]:
        with pytest.raises(ValueError):
            model.model_validate(fields | {"confirmed": value})


def test_public_metadata_is_bound_to_real_bytes_before_qa_and_body_script_is_unchanged(modules, tmp_path, monkeypatch):
    from app.pagebuilder_admission.projection import project
    from core.artifact_snapshot import freeze_source, manifest_digest
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    root = tmp_path / "source"
    root.mkdir()
    assets = root / "assets"
    assets.mkdir()
    (assets / "social-preview.png").write_bytes(b"bounded-fixture-image")
    body = '<main><h1>A &amp; B</h1></main><script data-supymem-analytics="v1">const text = "A & B";</script>'
    (root / "index.html").write_text('<!doctype html><html><head><title>A &amp; B</title><link rel="canonical" href="https://wrong.invalid">'
        '<meta property="og:image" content="assets/old.png"></head><body>' + body + '</body></html>')
    (root / "AGENTS.md").write_text("private reference instructions must never publish")
    manifest, archive = freeze_source(root)
    identity = {"scope": {"venture_id": "ven_one"}, "source": {"sha256": hashlib.sha256(archive).hexdigest(), "manifest_digest": manifest_digest(manifest)}}
    output, projection = project(archive, identity, {"hostname": "reviewed.supymem.com"})
    assert projection["original_source_sha256"] == identity["source"]["sha256"]
    assert projection["archive_sha256"] == hashlib.sha256(output).hexdigest()
    with zipfile.ZipFile(io.BytesIO(output)) as opened:
        assert "AGENTS.md" not in opened.namelist()
        page = opened.read("index.html").decode()
    assert body in page and page.count('rel="canonical"') == 1
    assert 'https://wrong.invalid' not in page and 'assets/old.png' not in page
    assert 'https://reviewed.supymem.com/assets/social-preview.png' in page
    assert project(archive, identity, {"hostname": "reviewed.supymem.com"})[0] == output


@pytest.mark.parametrize("name", ["Fleet inspection scheduling", "Family trail planning"])
def test_local_public_metadata_checks_without_rewriting(modules, tmp_path, monkeypatch, name):
    from app.pagebuilder_admission.projection import project
    from core.artifact_snapshot import freeze_source, manifest_digest

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    root = tmp_path / "tool"
    root.mkdir()
    body = (f'<!doctype html><html><head><title>{name}</title>'
        '<link rel="canonical" href="https://your-site.test/">'
        '<meta property="og:url" content="https://your-site.test/"></head>'
        '<body><script>const label = "A & B";</script></body></html>')
    (root / "index.html").write_text(body)
    manifest, archive = freeze_source(root)
    identity = {"scope": {"venture_id": "venture_one"}, "source": {
        "sha256": hashlib.sha256(archive).hexdigest(), "manifest_digest": manifest_digest(manifest)}}
    output, _ = project(archive, identity, {"hostname": "your-site.test"})
    with zipfile.ZipFile(io.BytesIO(output)) as opened:
        assert opened.read("index.html") == body.encode()
    (root / "index.html").write_text(body.replace("your-site.test", "wrong.test"))
    manifest, archive = freeze_source(root)
    identity["source"] = {"sha256": hashlib.sha256(archive).hexdigest(), "manifest_digest": manifest_digest(manifest)}
    with pytest.raises(ValueError, match="Coding tools"):
        project(archive, identity, {"hostname": "your-site.test"})
    (root / "index.html").write_text(body.replace("</head>", '<meta property="og:image" content="https://wrong.test/private.png"></head>'))
    manifest, archive = freeze_source(root)
    identity["source"] = {"sha256": hashlib.sha256(archive).hexdigest(), "manifest_digest": manifest_digest(manifest)}
    with pytest.raises(ValueError, match="Coding tools"):
        project(archive, identity, {"hostname": "your-site.test"})

"""Static QA gets an exact public projection without executing saved code."""
import hashlib
import io
import zipfile
import pytest
from core.artifact_snapshot import freeze_source, manifest_digest
from tests.company_os_harness import load_company_os_app


def _export(tmp_path, files):
    load_company_os_app()
    from app.build.subscription_static_export import export
    root = tmp_path / 'source'
    root.mkdir()
    for name, value in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
    manifest, blob = freeze_source(root)
    return export(blob, source_sha256=hashlib.sha256(blob).hexdigest(),
                  source_manifest_digest=manifest_digest(manifest), venture_id='v_test')


def test_projection_preserves_public_bytes_and_custody_identity(tmp_path):
    html = '<!doctype html><title>Landing</title><h1>Exact Landing</h1>'
    blob, receipt = _export(tmp_path, {'index.html': html, 'README.md': 'Local deployment instructions', 'DEPLOY.md': 'Reviewed hosting dashboard steps',
                                      '.supymem-venture': 'v_test', 'assets/style.css': 'body{color:black}'})
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        assert archive.read('index.html').decode() == html
        assert set(archive.namelist()) == {'index.html', 'assets/style.css'}
    assert receipt['omitted_paths'] == ['.supymem-venture', 'DEPLOY.md', 'README.md']
    assert receipt['original_source_sha256'] != receipt['archive_sha256']
    assert receipt['size_bytes'] == len(blob)
    assert receipt['manifest_digest'] == manifest_digest(receipt['manifest'])


def test_projection_rejects_server_source_instead_of_hiding_it(tmp_path):
    with pytest.raises(Exception, match='^subscription_static_non_public_file$'):
        _export(tmp_path, {'index.html': '<h1>Landing</h1>', 'product/server.py':
                          'raise RuntimeError("must never execute")'})


def test_projection_rejects_foreign_workspace_marker(tmp_path):
    with pytest.raises(Exception, match='^subscription_static_source_owner_mismatch$'):
        _export(tmp_path, {'index.html': '<h1>Landing</h1>', '.supymem-venture': 'v_foreign'})


@pytest.mark.parametrize('captured_review', [False, True], ids=['actual-eight-file-shape', 'actual-sixteen-file-shape'])
def test_actual_native_manifest_shapes_omit_all_private_cards_checks_and_captures(tmp_path, captured_review):
    # Exact paths from the two real Groomer custody manifests. Content is
    # synthetic; scripts/logs are private evidence, never executed or served.
    index = '<!doctype html><h1>Exact reviewed landing bytes</h1>'
    files = {'.supymem-venture': 'v_test', 'CODING_CARD.md': 'Private original approval card',
        'index.html': index, 'review/MAC_CODING_TASK.md': 'Private task metadata',
        'review/browser-contract.json': '{"private":"contract"}',
        'review/check_local.py': 'raise RuntimeError("must never execute")',
        'review/check_source.cjs': 'throw new Error("must never execute")',
        'review/source-checks.json': '{"private":"checks"}'}
    if captured_review:
        prefix = 'review/capture-20261002T022841Z/'
        files.update({prefix + 'browser-check.log': 'Private browser evidence',
            prefix + 'index.html': index, prefix + 'manifest.json': '{"private":"manifest"}',
            prefix + 'review/browser-contract.json': files['review/browser-contract.json'],
            prefix + 'review/check_local.py': files['review/check_local.py'],
            prefix + 'review/check_source.cjs': files['review/check_source.cjs'],
            prefix + 'review/source-checks.json': files['review/source-checks.json'],
            prefix + 'source-check.log': 'Private check evidence'})
    assert len(files) == (16 if captured_review else 8)
    public, receipt = _export(tmp_path, files)
    original_manifest, original_blob = freeze_source(tmp_path / 'source')
    from app.build.coding_source_preflight import inspect_contract
    preflight = inspect_contract(original_blob, source_sha256=receipt['original_source_sha256'],
        manifest_sha256=receipt['original_manifest_digest'], order_id='dwo_test', artifact_profile='static_landing_v1')
    assert preflight['status'] == 'requires_runtime_qa' and preflight['findings'] == []
    assert receipt['original_source_sha256'] == hashlib.sha256(original_blob).hexdigest()
    assert receipt['original_manifest_digest'] == manifest_digest(original_manifest)
    assert receipt['omitted_paths'] == sorted(set(files) - {'index.html'})
    assert receipt['export_policy'] == 'subscription_static_public_projection_v2'
    with zipfile.ZipFile(io.BytesIO(public)) as archive:
        assert archive.namelist() == ['index.html']
        assert archive.read('index.html') == index.encode()


@pytest.mark.parametrize('path', ['server.py', 'check_local.py', 'browser-contract.json', 'source-check.log'])
def test_unknown_nonpublic_root_files_still_refused(tmp_path, path):
    with pytest.raises(Exception, match='^subscription_static_non_public_file$'):
        _export(tmp_path, {'index.html': '<h1>Landing</h1>', path: 'Do not execute or serve'})
    from app.build.coding_source_preflight import inspect_contract
    manifest, blob = freeze_source(tmp_path / 'source')
    report = inspect_contract(blob, source_sha256=hashlib.sha256(blob).hexdigest(),
        manifest_sha256=manifest_digest(manifest), order_id='dwo_test', artifact_profile='static_landing_v1')
    assert report['status'] == 'blocked_runtime_contract'
    assert report['findings'] == ['static_landing_non_public_file']

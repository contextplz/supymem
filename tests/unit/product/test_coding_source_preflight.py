"""A source contract check never implies that runtime QA ran."""
import hashlib

from core.artifact_snapshot import freeze_source, manifest_digest

from tests.company_os_harness import load_company_os_app


def _report(tmp_path, files):
    load_company_os_app()
    from app.build.coding_source_preflight import inspect_contract
    root = tmp_path / "source"
    root.mkdir()
    for name, body in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(body)
    manifest, blob = freeze_source(root)
    return inspect_contract(blob, source_sha256=hashlib.sha256(blob).hexdigest(),
                            manifest_sha256=manifest_digest(manifest), order_id="dwo_test")


def test_missing_start_blocks_source_runtime(tmp_path):
    report = _report(tmp_path, {"package.json": '{"scripts":{"test":"node --test"}}'})
    assert report["status"] == "blocked_runtime_contract"
    assert report["findings"] == ["hosted_product_artifact_missing", "start_script_missing"]
    assert report["runtime_qa_verified"] is False
    assert report["hosting_authorized"] is False


def test_node_start_alone_is_not_a_hosted_product(tmp_path):
    report = _report(tmp_path, {"package.json": '{"scripts":{"start":"node server.js"}}'})
    assert report["status"] == "blocked_runtime_contract"
    assert report["runtime"] == "unsupported_node_review_shell"
    assert report["findings"] == ["hosted_product_artifact_missing"]
    assert report["runtime_qa_verified"] is False
    assert report["hosting_authorized"] is False


def test_declared_dependencies_require_lock(tmp_path):
    report = _report(tmp_path, {"package.json": '{"scripts":{"start":"node server.js"},"dependencies":{"express":"1.0.0"}}'})
    assert report["status"] == "blocked_runtime_contract"
    assert report["findings"] == ["dependency_lock_missing", "hosted_product_artifact_missing"]


def test_python_product_artifact_needs_real_runtime_qa(tmp_path):
    files = {"product/__init__.py": "", "product/routes.py":
             "from fastapi import APIRouter\nrouter = APIRouter()\n",
             "product/static/index.html": "<!doctype html><title>Product</title>"}
    report = _report(tmp_path, files)
    assert report["schema_version"] == 2
    assert report["runtime"] == "python_product_v1"
    assert report["status"] == "requires_runtime_qa"
    assert report["findings"] == []
    assert report["runtime_qa_verified"] is False


def test_python_product_with_review_shell_ignores_node_start(tmp_path):
    files = {"product/__init__.py": "", "product/routes.py":
             "from fastapi import APIRouter\nrouter = APIRouter()\n",
             "product/static/index.html": "<!doctype html><title>Product</title>",
             "package.json": '{"scripts":{"test":"node --test"}}'}
    assert _report(tmp_path, files)["status"] == "requires_runtime_qa"


def test_python_product_syntax_error_blocks_runtime_check(tmp_path):
    files = {"product/__init__.py": "", "product/routes.py": "def broken(:\n",
             "product/static/index.html": "<!doctype html><title>Product</title>"}
    report = _report(tmp_path, files)
    assert report["status"] == "blocked_runtime_contract"
    assert report["findings"] == ["product_routes_syntax_invalid"]


def test_static_profile_is_explicit_and_never_claims_browser_qa(tmp_path):
    import io
    import zipfile
    load_company_os_app()
    from app.build.coding_source_preflight import inspect_contract
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr('index.html', '<!doctype html><title>Landing</title>')
    blob = stream.getvalue()
    report = inspect_contract(blob, source_sha256='a' * 64, manifest_sha256='b' * 64,
                              order_id='dwo_test', artifact_profile='static_landing_v1')
    assert report['runtime'] == 'static_landing_v1'
    assert report['status'] == 'requires_runtime_qa'
    assert report['findings'] == []
    assert report['runtime_qa_verified'] is False
    assert report['hosting_authorized'] is False
    product = inspect_contract(blob, source_sha256='a' * 64, manifest_sha256='b' * 64,
                               order_id='dwo_test')
    assert product['status'] == 'blocked_runtime_contract'


def test_static_profile_rejects_product_server_code():
    import io
    import zipfile
    load_company_os_app()
    from app.build.coding_source_preflight import inspect_contract
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr('index.html', '<!doctype html><title>Landing</title>')
        archive.writestr('product/routes.py', 'raise RuntimeError("not executed")')
    report = inspect_contract(stream.getvalue(), source_sha256='a' * 64,
                              manifest_sha256='b' * 64, order_id='dwo_test',
                              artifact_profile='static_landing_v1')
    assert report['findings'] == ['static_landing_server_code_present']
    assert report['status'] == 'blocked_runtime_contract'

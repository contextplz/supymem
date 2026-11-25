"""Offline custody tests: candidate bytes never substitute for real CLI work."""
import asyncio
import concurrent.futures
import hashlib
import io
import stat
import threading
import time
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from core.artifact_policy import ArtifactRejected
from core.artifact_snapshot import canonical, manifest_digest
from desktop_runtime.bridge import create_bridge_app
from desktop_runtime.cancellable_http import ModelRequestStopped
from desktop_runtime.device_gateway import WorkOrderGateway
from desktop_runtime.owned_coding import OwnedCoding
from desktop_runtime.owned_processes import OwnedProcesses
from desktop_runtime.pagebuilder_reference import stage_reference, validate_binding, verify_staged


@pytest.fixture(autouse=True)
def hosted_reference_transport(monkeypatch):
    from desktop_runtime.agent_status import start_probes, stop_probes

    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    start_probes()
    yield
    stop_probes()


def archive(files, *, modes=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as zipped:
        for name, blob in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system, info.external_attr = 3, (modes or {}).get(name, 0o100600) << 16
            zipped.writestr(info, blob)
    return output.getvalue()


def candidate(files=None, *, modes=None, outer_extra=None, venture_id="ven_one"):
    files = files or {"index.html": b"<h1>Candidate reference</h1>"}
    source = archive(files, modes=modes)
    manifest = [{"path": name, "size_bytes": len(blob), "sha256": hashlib.sha256(blob).hexdigest()}
                for name, blob in sorted(files.items())]
    spec = b"# Reviewed candidate design\nData only."
    identity = {"schema_version": 1, "job_id": "job_one", "variant": 0, "revision_id": "rev_one",
        "workspace_id": "w_one", "venture_id": venture_id, "app_id": "app_one",
        "source_sha256": hashlib.sha256(source).hexdigest(), "source_manifest_digest": manifest_digest(manifest),
        "source_size_bytes": len(source), "spec_sha256": hashlib.sha256(spec).hexdigest()}
    blob = archive({"source.zip": source, "manifest.json": canonical(manifest), "SPEC.md": spec,
                    "REFERENCE.json": canonical(identity), **(outer_extra or {})})
    return {**identity, "reference_sha256": hashlib.sha256(blob).hexdigest(), "reference_size_bytes": len(blob)}, blob


def test_reference_only_staging_idempotency_and_tamper(tmp_path):
    tmp_path = tmp_path.resolve()
    binding, blob = candidate()
    root_source = tmp_path / "index.html"
    root_source.write_text("Founder source")
    relative, expected = stage_reference(tmp_path, binding, blob)
    assert root_source.read_text() == "Founder source"
    assert (tmp_path / relative / "source/index.html").read_bytes() == b"<h1>Candidate reference</h1>"
    assert not (tmp_path / relative / "source.zip").exists()
    assert stage_reference(tmp_path, binding, blob) == (relative, expected)
    (tmp_path / relative / "SPEC.md").write_text("mutated")
    with pytest.raises(ArtifactRejected, match="reference_changed"):
        verify_staged(tmp_path, relative, expected)
    with pytest.raises(ArtifactRejected, match="reference_changed"):
        stage_reference(tmp_path, binding, blob)


@pytest.mark.parametrize("member", ["source.zip", "SPEC.md", "manifest.json", "REFERENCE.json"])
def test_resealed_transport_cannot_change_inner_pins(tmp_path, member):
    binding, blob = candidate()
    with zipfile.ZipFile(io.BytesIO(blob)) as zipped:
        files = {name: zipped.read(name) for name in zipped.namelist()}
    files[member] = ({"source.zip": archive({"index.html": b"Changed source"}),
                      "SPEC.md": b"Changed spec", "manifest.json": canonical([]),
                      "REFERENCE.json": canonical({"venture_id": "ven_foreign"})})[member]
    changed = archive(files)
    binding.update(reference_sha256=hashlib.sha256(changed).hexdigest(), reference_size_bytes=len(changed))
    with pytest.raises(ArtifactRejected):
        stage_reference(tmp_path.resolve(), binding, changed)
    assert not (tmp_path / ".reference/pagebuilder/job_one/variant-0").exists()


@pytest.mark.parametrize("attack", ["traversal", "symlink", "rules", "outer_extra", "outer_tamper", "foreign", "parent_link"])
def test_reference_rejects_unsafe_or_foreign_bytes_before_root_effects(tmp_path, attack):
    tmp_path = tmp_path.resolve()
    binding, blob = candidate()
    if attack == "traversal":
        binding, blob = candidate({"../escape.html": b"escape"})
    elif attack == "symlink":
        binding, blob = candidate({"index.html": b"outside"}, modes={"index.html": stat.S_IFLNK | 0o777})
    elif attack == "rules":
        binding, blob = candidate({"CLAUDE.md": b"Hidden instructions", "index.html": b"page"})
    elif attack == "outer_extra":
        binding, blob = candidate(outer_extra={"run.sh": b"do something"})
    elif attack == "outer_tamper":
        blob += b"modified"
    elif attack == "foreign":
        binding["venture_id"] = "ven_foreign"
    elif attack == "parent_link":
        outside = tmp_path / "outside"
        outside.mkdir()
        (tmp_path / ".reference").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ArtifactRejected):
        validated = validate_binding(binding, venture_id="ven_one", workspace_id="w_one")
        stage_reference(tmp_path, validated, blob)
    assert not (tmp_path / "index.html").exists()
    assert not (tmp_path / "escape.html").exists()
    if attack == "parent_link":
        assert list(outside.iterdir()) == []


def test_gateway_requires_claim_exact_binding_and_current_authority_around_download():
    binding, blob = candidate()
    saved = {"id": "order", "venture_id": "ven_one", "workspace_id": "w_one", "device_id": "device",
             "state": "claimed", "execution_id": "exec", "review_digest": "a" * 64,
             "manifest": {"coding_verification": {"pagebuilder_candidate": binding}}}
    calls = []
    def request(method, path, body, **kwargs):
        calls.append(path)
        return {"current": True} if path.endswith("/authority") else saved
    def download(path, **kwargs):
        assert path == "/api/ventures/ven_one/device-work-orders/order/pagebuilder-reference"
        assert kwargs["max_bytes"] == len(blob)
        calls.append(path)
        return blob
    gateway = WorkOrderGateway(venture_id="ven_one", order_id="order", device_id="device", request=request,
                               cancellable_request=request, download=download)
    with pytest.raises(PermissionError, match="claim_required"):
        gateway.pagebuilder_reference(binding, stop=threading.Event(), deadline=time.monotonic() + 5)
    gateway.claimed, gateway.execution_id = True, "exec"
    assert gateway.pagebuilder_reference(binding, stop=threading.Event(), deadline=time.monotonic() + 5) == blob
    assert [path.rsplit("/", 1)[-1] for path in calls] == ["order", "authority", "pagebuilder-reference", "order", "authority"]
    def changed_download(*args, **kwargs):
        saved["manifest"]["coding_verification"]["pagebuilder_candidate"] = {**binding, "variant": 1}
        return blob
    gateway.download = changed_download
    with pytest.raises(PermissionError, match="binding_changed"):
        gateway.pagebuilder_reference(binding, stop=threading.Event(), deadline=time.monotonic() + 5)


def test_download_failure_after_genuine_claim_settles_without_provider_or_source(monkeypatch, tmp_path):
    from desktop_runtime.device_worker import DeviceWorker
    monkeypatch.setenv("HOME", str(tmp_path.resolve()))
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / "data"))
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\necho must-not-run > provider-ran\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    binding, _blob = candidate()
    calls = []
    class Gateway:
        order_id, venture_id, execution_id, claim_attempted = "dwo_one", "ven_one", None, False
        def claim(self):
            self.claim_attempted, self.execution_id = True, "dex_one"
            calls.append("claim")
        def pagebuilder_reference(self, *args, **kwargs):
            assert self.claim_attempted is True
            calls.append("download")
            raise ValueError("pagebuilder_reference_mismatch")
        def stop_receipt(self, digest, stopped, *, task_verified=False):
            assert stopped is True and task_verified is False
            calls.append("settle")
            return {"state": "stopped"}
    saved = {"state": "approved", "workspace_id": "w_one", "task_snapshot": {
        "task_id": "tsk_one", "artifact_profile": "static_landing_v1", "landing_expectations": {
            "contract": {"profile": "static_landing_v1", "product_name": "Reviewed page",
                         "required_text": ["Reviewed page"], "actions": []}}}}
    manifest = {"applications": ["com.companyos.app"], "origins": [],
        "actions": ["editor.edit", "terminal.execute"], "duration_seconds": 60,
        "instruction": "Implement approved candidate", "coding_verification": {
            "operation": "workspace_edit_v1", "provider": "codex", "workspace_slug": "one",
            "read_scope": "device_filesystem", "write_scope": "venture_workspace", "command_network": "disabled",
            "offline_profile": "static_html_v1", "pagebuilder_candidate": binding}}
    worker = DeviceWorker(Gateway(), lambda receipt: None)
    worker.run_coding(saved, manifest)
    assert calls == ["claim", "download", "settle"]
    assert worker.last["credits_reconciled"] is True and worker.last["task_verified"] is False
    assert worker.last["failure_code"] == "pagebuilder_reference_mismatch"
    assert worker.last["evidence"]["coding"]["failure_stage"] == "claiming"
    assert worker.last["evidence"]["coding"]["failure_code"] == "pagebuilder_reference_mismatch"
    assert worker.last["evidence"]["coding"]["exception_type"] == "ValueError"
    assert worker.last["evidence"]["inventory"]["processes"] == []
    assert "source_receipt" not in worker.last
    assert not (tmp_path / "provider-ran").exists()


@pytest.mark.parametrize("mode", ["success", "oversize", "redirect", "stop", "epoch", "renewal", "logout"])
def test_real_bridge_fixed_binary_stream_custody(monkeypatch, tmp_path, mode):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BRIDGE_TOKEN", "synthetic-local-test-token")
    monkeypatch.setenv("GATEWAY_URL", "https://example.invalid")
    monkeypatch.delenv("COMPANY_OS_PUBLIC_URL", raising=False)
    monkeypatch.delenv("BRIDGE_AUTH_OWNER", raising=False)
    native_secret = "synthetic-native-secret-for-stream-custody"
    if mode in {"renewal", "logout"}:
        monkeypatch.setenv("BRIDGE_AUTH_OWNER", "native")
        monkeypatch.setenv("BRIDGE_NATIVE_TOKEN", native_secret)
    entered, cleaned = threading.Event(), threading.Event()
    content = b"a" * 300000  # Exceeds the JSON gateway's cap.
    requests = []
    async def binary_request(request):
        requests.append(request)
        assert request.url.host == "example.invalid"
        assert request.url.path == "/api/ventures/ven_one/device-work-orders/order/pagebuilder-reference"
        assert request.headers["Authorization"] == "Bearer synthetic-account-token"
        entered.set()
        if mode in {"stop", "epoch", "renewal", "logout"}:
            try:
                if mode == "stop":
                    await asyncio.Event().wait()
                while not cleaned.is_set():
                    await asyncio.sleep(.01)
            finally:
                cleaned.set()
        if mode == "redirect":
            return httpx.Response(302, headers={"Location": "https://foreign.invalid/secret"})
        return httpx.Response(200, content=content, headers={"Content-Type": "application/zip"})
    def handler(request):
        if request.url.path == "/auth/session":
            return httpx.Response(200, json={"authenticated": True, "subject": "usr_one"})
        return binary_request(request)
    app = create_bridge_app(transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        session = app.state.bridge_session
        session.accept_tokens({"subject": "usr_one", "device_id": "device", "access_token": "synthetic-account-token", "expires_in": 900})
        retired = threading.Event()
        session.activity_retired = retired.set
        gateway = app.state.native_order_gateway("ven_one", "order")
        stop = threading.Event()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(gateway.download, gateway.path + "/pagebuilder-reference", stop=stop,
                                     deadline=time.monotonic() + 5, max_bytes=299999 if mode == "oversize" else len(content))
            assert entered.wait(1)
            if mode == "stop":
                stop.set()
            elif mode == "epoch":
                with session.lock:
                    session.clear()
                    session.accept_tokens({"subject": "usr_other", "device_id": "other", "access_token": "other", "expires_in": 900})
                    session.clear()
                    session.accept_tokens({"subject": "usr_one", "device_id": "device", "access_token": "synthetic-account-token", "expires_in": 900})
                cleaned.set()
            elif mode in {"renewal", "logout"}:
                headers = {"x-native-token": native_secret}
                if mode == "logout":
                    assert client.post("/native/clear", headers=headers).status_code == 200
                assert client.post("/native/session", headers=headers, json={"account_id": "usr_one", "device_id": "device",
                    "access_token": "renewed", "expires_at": time.time() + 900}).status_code == 200
                assert retired.is_set() is (mode == "logout")
                cleaned.set()
            error = {"oversize": RuntimeError, "redirect": PermissionError, "stop": ModelRequestStopped,
                     "epoch": PermissionError, "logout": PermissionError}.get(mode)
            if error:
                with pytest.raises(error):
                    future.result(timeout=2)
            else:
                assert future.result(timeout=2) == content
        assert len(requests) == 1
        with pytest.raises(PermissionError, match="scope_invalid"):
            gateway.download("/api/other/pagebuilder-reference", stop=stop, deadline=time.monotonic() + 1, max_bytes=100)


@pytest.mark.parametrize("transition", ["refresh", "clear", "switch", "invalid"])
def test_verified_same_device_renewal_preserves_custody_but_reauthentication_retires_it(monkeypatch, tmp_path, transition):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BRIDGE_TOKEN", "synthetic-local-test-token")
    monkeypatch.setenv("GATEWAY_URL", "https://example.invalid")
    monkeypatch.setenv("BRIDGE_AUTH_OWNER", "native")
    secret = "synthetic-native-secret-for-custody-test"
    monkeypatch.setenv("BRIDGE_NATIVE_TOKEN", secret)
    monkeypatch.delenv("COMPANY_OS_PUBLIC_URL", raising=False)
    def handler(request):
        bearer = request.headers.get("Authorization")
        if request.url.path == "/auth/session":
            return httpx.Response(200, json={"authenticated": bearer != "Bearer invalid",
                "subject": "usr_other" if bearer == "Bearer other" else "usr_one"})
        return httpx.Response(200, json={"id": "order", "venture_id": "ven_one", "device_id": "device",
            "review_digest": "a" * 64, "state": "approved"})
    app = create_bridge_app(transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        headers = {"x-native-token": secret}
        body = {"account_id": "usr_one", "device_id": "device", "access_token": "original", "expires_at": time.time() + 900}
        assert client.post("/native/session", headers=headers, json=body).status_code == 200
        retired = threading.Event()
        app.state.bridge_session.activity_retired = retired.set
        gateway = app.state.native_order_gateway("ven_one", "order")
        if transition == "clear":
            assert client.post("/native/clear", headers=headers).status_code == 200
        elif transition == "switch":
            assert client.post("/native/session", headers=headers, json={**body, "account_id": "usr_other",
                "device_id": "other", "access_token": "other"}).status_code == 200
        elif transition == "invalid":
            assert client.post("/native/session", headers=headers, json={**body, "access_token": "invalid"}).status_code == 401
        assert client.post("/native/session", headers=headers, json={**body, "access_token": "renewed"}).status_code == 200
        if transition == "refresh":
            assert not retired.is_set()
            assert gateway.read()["state"] == "approved"
        else:
            assert retired.is_set()
            with pytest.raises(PermissionError, match="account_changed"):
                gateway.read()


@pytest.mark.parametrize("change", ["root", "review_only", "artifacts_only", "reference_tamper", "none"])
def test_real_cli_fixture_has_to_modify_root_source_after_reference_staging(monkeypatch, tmp_path, change):
    monkeypatch.setenv("HOME", str(tmp_path.resolve()))
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / "data"))
    binary = tmp_path / "claude"
    edits = {"root": "echo '<h1>Implemented by CLI</h1>' > index.html; mkdir -p assets; echo 'h1 { color: green; }' > assets/style.css",
        "review_only": "mkdir -p review; echo notes > review/notes.md",
        "artifacts_only": "mkdir -p artifacts; echo notes > artifacts/notes.html",
        "reference_tamper": "echo modified > .reference/pagebuilder/job_one/variant-0/SPEC.md; echo page > index.html", "none": ":"}
    binary.write_text('#!/bin/sh\nif [ "$1" = "--version" ]; then echo "2.1.284 (Claude Code)"; exit 0; fi\n'
                      + edits[change] + '\necho \'{"type":"result","subtype":"success","is_error":false}\'\n')
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    tracker = OwnedProcesses()
    coding = OwnedCoding(venture_id="ven_one", workspace_slug="one", provider="claude", tracker=tracker, authorize=lambda: True)
    if change in {"review_only", "artifacts_only"}:
        (coding._workspace() / "index.html").write_text("Pre-existing founder source")
    binding, blob = candidate()
    coding.stage_pagebuilder_reference(binding, blob, time.monotonic() + 10)
    if change not in {"review_only", "artifacts_only"}:
        assert not (coding._workspace() / "index.html").exists()
    try:
        if change == "root":
            receipt = coding.run("Build approved source from the reviewed candidate.", time.monotonic() + 10)
            assert [item["path"] for item in receipt["changed_files"]] == ["assets/style.css", "index.html"]
            assert receipt["task_acceptance_verified"] is False
            tracker.stop_all()
            assert tracker.stopped()
            from desktop_runtime import source_receipt
            frozen = source_receipt.capture(venture_id="ven_one", workspace_id="wsp_one", actor_id="person_one",
                device_id="dev_one", plan_task_id="tsk_one", workspace_slug="one", order_id="dwo_one",
                execution_id="dex_one", provider="claude")
            with zipfile.ZipFile(io.BytesIO(source_receipt.load_archive(frozen))) as source:
                assert {"index.html", "assets/style.css"} <= set(source.namelist())
                assert not any(name.startswith(("artifacts/", ".reference/")) for name in source.namelist())
                assert source.read("index.html") == b"<h1>Implemented by CLI</h1>\n"
        else:
            with pytest.raises(ValueError, match="root_source_unchanged|reference_changed|change_set_invalid"):
                coding.run("Build approved source from the reviewed candidate.", time.monotonic() + 10)
    finally:
        tracker.stop_all()
    assert tracker.stopped()

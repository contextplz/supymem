"""The native protocol binds source bytes to one stopped reviewed order."""
import pytest

from desktop_runtime.device_gateway import WorkOrderGateway


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_native_source_upload_is_bound_and_idempotent(provider):
    calls = []
    receipt = dict(order_id="dwo_one", venture_id="ven_one", workspace_id="wsp_one",
                   device_id="dev_one", plan_task_id="tsk_one", execution_id="dex_one",
                   provider=provider, qa_verified=False, sha256="a" * 64,
                   manifest_sha256="b" * 64, size_bytes=3)
    def request(method, path, body):
        calls.append((method, path, body))
        if method == "GET":
            return dict(id="dwo_one", venture_id="ven_one", workspace_id="wsp_one",
                        device_id="dev_one", state="stopped", execution_id="dex_one",
                        task_snapshot={"task_id": "tsk_one"}, review_digest="c" * 64,
                        manifest={"coding_verification": {"provider": provider}})
        return {**receipt, "state": "awaiting_upload"}
    def upload(path, blob):
        calls.append(("PUT", path, blob))
        return {**receipt, "state": "stored"}
    gateway = WorkOrderGateway(venture_id="ven_one", order_id="dwo_one",
                               device_id="dev_one", request=request, upload=upload)
    assert gateway.store_coding_source(receipt, b"zip")["state"] == "stored"
    assert calls[-1] == ("PUT", gateway.path + "/source-archive", b"zip")
    with pytest.raises(PermissionError, match="scope_changed"):
        gateway.store_coding_source({**receipt, "device_id": "dev_other"}, b"zip")
    with pytest.raises(PermissionError, match="scope_changed"):
        gateway.store_coding_source({**receipt, "provider": "claude" if provider == "codex" else "codex"}, b"zip")

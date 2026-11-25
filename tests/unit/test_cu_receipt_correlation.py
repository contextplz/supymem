from types import SimpleNamespace

from desktop_runtime.device_worker import DeviceWorker


def worker():
    gateway = SimpleNamespace(order_id="dwo_owned", execution_id="dex_owned", device_id="dev_owned", venture_id="ven_owned")
    return DeviceWorker(gateway, lambda *a, **k: None)


def test_step_context_cannot_be_spoofed_by_values():
    w = worker()
    w._record_step(_owned_context=True, kind="ai_step", order_id="dwo_fake", execution_id="dex_fake", device_id="dev_fake",
                   worker_instance_id="fake", worker_pid=-1, observed_at_ns=0, token="private")
    row = w.last["steps"][0]
    assert row["order_id"] == "dwo_owned" and row["execution_id"] == "dex_owned"
    assert row["device_id"] == "dev_owned" and row["worker_instance_id"] == w._receipt_instance_id
    assert row["worker_pid"] > 0 and row["observed_at_ns"] > 0
    assert "token" not in row


def test_ai_log_uses_owned_invocation_not_proposal_identifier_and_drops_payload():
    w = worker()
    receipt = w._ai_log_receipt({"step":1,"action":"click","order_id":"fake", "execution_id":"fake",
        "invocation_id":"ai_" + "b"*32, "text":"private", "png":"private", "secret":"private",
        "ms":{"observe":100,"model":200,"act":50,"raw":"private","token":"private"}},
        invocation_id="ai_" + "a"*32)
    assert receipt["invocation_id"] == "ai_" + "a"*32
    assert receipt["execution_id"] == "dex_owned"
    assert receipt["ms"] == {"observe":100,"model":200,"act":50}
    assert not any(k in receipt for k in ("text","png","secret"))
    assert "invocation_id" not in w._ai_log_receipt({"invocation_id":"fake"})


def test_instances_distinct_and_timing_fields_are_real():
    a, b = worker(), worker()
    assert a._receipt_instance_id != b._receipt_instance_id
    first = a._receipt_context()
    second = a._receipt_context()
    assert second["monotonic_ns"] >= first["monotonic_ns"]
    assert first["worker_pid"] == second["worker_pid"]

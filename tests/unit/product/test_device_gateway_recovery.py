"""Old cancelled orders can be read for reconciliation, never re-executed."""
from __future__ import annotations

import pytest

from desktop_runtime.device_gateway import WorkOrderGateway


def _gateway(state: str, *, claimed: bool = True) -> WorkOrderGateway:
    def request(method, path, body):
        assert (method, path, body) == ("GET", "/api/ventures/ven_1/device-work-orders/dwo_1", None)
        return {"id": "dwo_1", "venture_id": "ven_1", "device_id": "dev_old",
                "state": state, "claimed_at": "2026-09-24T00:00:00Z" if claimed else None,
                "execution_id": "dex_old" if claimed else None,
                "review_digest": "a" * 64}
    return WorkOrderGateway(venture_id="ven_1", order_id="dwo_1",
                            device_id="dev_new", request=request)


@pytest.mark.parametrize("state", ["cancelled", "stopped"])
def test_old_claimed_terminal_order_is_readable(state: str) -> None:
    assert _gateway(state).read()["device_id"] == "dev_old"


@pytest.mark.parametrize("state,claimed", [("approved", True), ("claimed", True),
                                             ("cancelled", False), ("stopped", False)])
def test_old_nonterminal_or_unclaimed_order_is_rejected(state: str, claimed: bool) -> None:
    with pytest.raises(PermissionError, match="native_order_identity_changed"):
        _gateway(state, claimed=claimed).read()


@pytest.mark.parametrize("domain", ["Equipment servicing", "Weekend garden planner"])
@pytest.mark.parametrize("claim_attempted", [False, True])
@pytest.mark.parametrize("reply_lost", [False, True])
def test_failed_preflight_closes_only_a_definitely_unstarted_build(monkeypatch, domain, claim_attempted, reply_lost):
    import threading
    from desktop_runtime.device_worker import DeviceWorker

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    requests = []
    def request(method, path, body):
        requests.append((method, path, body))
        if method == "GET":
            return {"id": "dwo_1", "venture_id": "ven_1", "device_id": "dev_1", "state": "approved",
                    "review_digest": "a" * 64, "manifest": {"title": domain}}
        assert path.endswith("/fail-before-start") and body["failure_code"] == "coding_checks_unavailable"
        if reply_lost:
            raise OSError("failure receipt unavailable")
        return {"id": "dwo_1", "state": "failed", "failure_code": body["failure_code"]}
    gateway = WorkOrderGateway(venture_id="ven_1", order_id="dwo_1", device_id="dev_1", request=request)
    gateway.claim_attempted = claim_attempted
    worker = DeviceWorker.__new__(DeviceWorker)
    worker.gateway, worker.cancelled = gateway, threading.Event()
    worker.last = {"state": "checking", "failure_code": "coding_checks_unavailable", "task_verified": False}
    worker.progress = lambda state, **values: worker.last.update(state=state, **values)
    worker._notify_terminal = lambda *_args, **_kwargs: None
    manifest = {"title": domain, "coding_loop_verification": {"phase": "implement"}}
    worker._finish_product_execution(manifest, None, False, None, "execution_requires_reconciliation", None)
    if claim_attempted:
        assert not requests and worker.last["state"] == "reconciliation_required"
    elif reply_lost:
        assert requests[-1][0] == "POST" and worker.last["failure_finalized"] is False
        assert "Build activity" in worker.last["message"] and "Cancel" in worker.last["message"]
    else:
        assert requests[-1][0] == "POST" and worker.last["failure_finalized"] is True
        assert worker.last["state"] == "not_started"

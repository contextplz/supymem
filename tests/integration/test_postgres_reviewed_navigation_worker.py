"""Real signed Core/PG -> isolated Chrome typed navigation, with no provider I/O.

This read-only public HTTPS check proves the source protocol and bounded current
load witness. It does not prove pixels, an installed app, or production authority.
"""
import json
import hashlib
import os
import re
from pathlib import Path
from datetime import datetime, timezone

import pytest

from tests.integration.test_postgres_auth_security import pg_database as pg_database
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db
from tests.integration.test_postgres_device_orders import prepare


LOCATIONS = [
    "https://audit-qa.supymem.com/",
    "https://gateway.supymem.com/public/waitlists/ven_0sf17pcf8dt6",
]


def test_signed_core_worker_current_reviewed_urls_without_provider(journey_db, monkeypatch):
    pytest.importorskip("playwright.sync_api")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.assertion import sign_assertion
    from app.api.device_orders_api import router
    from app.build import device_orders as orders, device_jev
    from desktop_runtime import device_worker as worker_module
    from desktop_runtime.device_gateway import WorkOrderGateway
    from desktop_runtime.owned_processes import OwnedProcesses

    d = journey_db
    monkeypatch.setenv("DEVICE_BROWSER_EXECUTION_ENABLED", "1")
    # Successful reviewed URL navigation has no dependency on a Jev credential.
    monkeypatch.delenv("JEV_DEVICE_DECISIONS_ENABLED", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(device_jev, "_provider_call", lambda *_: pytest.fail("unexpected provider I/O"))
    monkeypatch.setattr(worker_module, "report_error", lambda *_args, **_kwargs: None)
    original = orders.prepare

    def typed(venture_id, caller, manifest, **kwargs):
        manifest.applications = ["com.google.Chrome"]
        manifest.origins = ["https://audit-qa.supymem.com", "https://gateway.supymem.com"]
        manifest.instruction = (
            "Confirm the reviewed site visibly loads, follow its exact reviewed "
            "waitlist link, and confirm that reviewed page visibly loads. Do not "
            "enter data, submit forms, sign in, change settings, publish, or download."
        )
        manifest.browser_verification = orders.BrowserVerification(expected_locations=LOCATIONS)
        manifest.duration_seconds = 90
        return original(venture_id, caller, manifest, **kwargs)

    with monkeypatch.context() as local:
        local.setattr(orders, "prepare", typed)
        service, caller, order = prepare(d, actions=["browser.read", "browser.interact"])
    args = (d["vid"], caller, order["id"])
    service.transition(*args, order["review_digest"], "approve")

    secret = "reviewed-navigation-worker-test-secret-more-than-32-bytes"
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", secret)
    app = FastAPI()
    app.include_router(router)
    requests = []
    with TestClient(app) as client:
        def signed_request(method, path, body):
            # The desktop bridge uses /api; the Core router is mounted directly.
            target = path.removeprefix("/api")
            token = sign_assertion(secret, sub=d["actor"], email="", role="user",
                path=target, method=method, device_id=caller.device_id)
            response = client.request(method, target, json=body,
                headers={"X-COS-Assertion": token})
            requests.append({"method": method, "path": target,
                "status": response.status_code})
            assert response.status_code == 200, (method, target, response.status_code)
            return response.json()

        gateway = WorkOrderGateway(venture_id=d["vid"], order_id=order["id"],
            device_id=caller.device_id, request=signed_request)
        proposal_errors = []
        actual_proposal = gateway.navigation_proposal
        def captured_proposal(*args, **kwargs):
            try:
                return actual_proposal(*args, **kwargs)
            except Exception as error:
                value = str(error)
                proposal_errors.append({"type": type(error).__name__,
                    "code": value if re.fullmatch(r"[a-z0-9_]{1,100}", value) else "not_static"})
                raise
        gateway.navigation_proposal = captured_proposal
        worker = worker_module.DeviceWorker(gateway, lambda _value: None)
        worker._notify_terminal = lambda *_args, **_kwargs: None
        worker._notify_needs_you = lambda *_args, **_kwargs: None
        worker.run()

    evidence = worker.last.get("evidence", {})
    diagnostic = {
        "outcome": evidence.get("outcome"), "failure_code": worker.last.get("failure_code"),
        "requests": [{"method": r["method"], "route": r["path"].rsplit("/", 1)[-1],
            "status": r["status"]} for r in requests],
        "loads": [{"status": (p.get("response") or {}).get("status"),
            "qualified": p.get("qualified"), "sequence": p.get("sequence")}
            for p in evidence.get("verified_documents", [])], "proposal_errors": proposal_errors}
    saved = service.retrieve(*args)
    root = Path(__file__).resolve().parents[2]
    source_paths = [
        'packages/coding_agents/owned_browser.py',
        'packages/desktop_runtime/device_gateway.py',
        'packages/desktop_runtime/device_worker.py',
        'services/company_os/app/build/device_browser_navigation.py',
        'services/company_os/app/build/device_invocations.py',
        'services/company_os/app/api/device_orders_api.py',
        'tests/integration/test_postgres_reviewed_navigation_worker.py',
    ]
    receipt_path = os.environ.get('SUPYMEM_REVIEWED_WORKER_PROOF')
    if receipt_path:
        value = {'schema_version': 1, 'completed_utc': datetime.now(timezone.utc).isoformat(),
            'source_files': {p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in source_paths},
            'requests': requests, 'diagnostic': diagnostic,
            'worker_evidence': evidence, 'worker_closed': worker.closed,
            'process_inventory_stopped': OwnedProcesses.receipt_stopped(evidence.get('inventory')),
            'task_verified': worker.last.get('task_verified') is True,
            'gateway_typed_capability': gateway.typed_navigation_proposal_v1,
            'saved_state': saved['state'], 'invocations': saved.get('invocations', {}),
            'installed_app': False, 'production_authority': False,
            'human_or_pixel_visibility': False}
        fd = os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as target:
            json.dump(value, target, sort_keys=True, indent=2)
            target.write('\n')
        Path(receipt_path).chmod(0o400)
    if worker.last.get("task_verified") is not True:
        pytest.fail(json.dumps(diagnostic, sort_keys=True))
    assert evidence.get("outcome") == "verified"
    assert worker.closed and evidence.get("close_returned") is True
    assert evidence.get("processes_stopped") is True
    assert OwnedProcesses.receipt_stopped(evidence.get("inventory"))
    proofs = evidence["verified_documents"]
    assert [worker.strip_check_marker(p["location"]) for p in proofs] == LOCATIONS
    assert all(p["qualified"] is True and len(p["page_id"]) == 32 for p in proofs)
    assert all(p["response"]["complete"] is True and 200 <= p["response"]["status"] < 300
        for p in proofs)
    assert gateway.typed_navigation_proposal_v1 is True
    assert sum(r["path"].endswith("/browser-navigation-proposal") for r in requests) == 1
    assert not any(r["path"].endswith("/browser-decision") for r in requests)
    assert sum(r["path"].endswith("/effects/admit") for r in requests) == 1
    assert sum(r["path"].endswith("/effects/receipt") for r in requests) == 1
    assert saved["state"] == "stopped"
    invocations = saved["invocations"]
    assert len(invocations) == 1
    invocation = next(iter(invocations.values()))
    assert invocation["state"] == "completed" and invocation["no_provider_io"] is True
    assert invocation["source_kind"] == "typed_reviewed_url_v1"
    assert invocation["quote_micro"] == invocation["actual_micro"] == invocation["provider_actual_micro"] == 0
    assert "usage" not in invocation and "model" not in invocation
    assert invocation["effect"]["state"] == "returned"

"""Signed migrated Core -> actual worker/headless browser, with local model stub.

The private HTTPS fixture has a temporary synthetic certificate. Only this test
context ignores its certificate error and admits loopback; production browser
origin/TLS policy, provider compatibility, pixels and installed UI are unproved.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone
import hashlib
import json
import os
import secrets
from pathlib import Path
import ssl
import subprocess
from threading import Thread
from urllib.parse import urlsplit

import pytest

from tests.integration.test_postgres_auth_security import pg_database as pg_database  # noqa: F401
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db  # noqa: F401
from tests.integration.test_postgres_device_orders import prepare
from tests.unit.product.test_device_browser_ai import configure, response


@pytest.fixture
def owned_https(tmp_path):
    cert, key = tmp_path / "fixture-cert.pem", tmp_path / "fixture-key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=127.0.0.1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            path = urlsplit(self.path).path
            seen.append(path)
            content = (
                "<h1>First fixture page</h1><p>First source value: 23</p>"
                '<a href="/two">Next fixture page</a>'
                if path == "/one"
                else "<h1>Second fixture page</h1><p>Second source value: 41</p>"
            )
            raw = content.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://127.0.0.1:{server.server_address[1]}", seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("cancel_during_actor", [False, True])
def test_signed_core_worker_browser_cuai_one_use_chain_and_cancel(
    journey_db, owned_https, monkeypatch, cancel_during_actor
):
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import BrowserType
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.assertion import sign_assertion
    from app.api.device_orders_api import router
    from app.build import device_orders as orders, device_browser_ai as ai, device_jev
    from app.db.models import CreditAccount
    from coding_agents.owned_browser import OwnedBrowser
    from desktop_runtime import device_worker as worker_module
    from desktop_runtime.device_gateway import WorkOrderGateway, UpstreamRejected
    from desktop_runtime.owned_processes import OwnedProcesses

    d, (origin, seen) = journey_db, owned_https
    profile = configure(monkeypatch)
    monkeypatch.delenv("JEV_DEVICE_DECISIONS_ENABLED", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("DEVICE_DESKTOP_EXECUTION_ENABLED", raising=False)
    monkeypatch.setattr(
        device_jev, "_provider_call", lambda *_: pytest.fail("Jev fallback")
    )
    monkeypatch.setattr(worker_module, "report_error", lambda *_args, **_kwargs: None)
    original_prepare = orders.prepare

    def prepared(vid, caller, manifest, **kwargs):
        value = manifest.model_dump(exclude_none=True)
        value.update(
            applications=["com.google.Chrome"],
            origins=[origin],
            instruction="Read both fixture source values by following Next fixture page, then report 23 and 41. Do not submit forms or send anything.",
            credit_ceiling_micro=500000,
            duration_seconds=90,
            general_browser_verification={
                "operation": "general_goal_v1",
                "start_url": origin + "/one",
                "max_steps": 8,
                "allow_type": False,
                "fill_values": {},
                "execution_profile": profile.model_dump(),
            },
        )
        return original_prepare(
            vid, caller, orders.Manifest.model_validate(value), **kwargs
        )

    with monkeypatch.context() as local:
        local.setattr(orders, "prepare", prepared)
        service, caller, order = prepare(
            d, actions=["browser.read", "browser.interact"]
        )
    with d["factory"]() as session:
        account = (
            session.query(CreditAccount)
            .filter_by(venture_id=d["vid"], scope="venture")
            .one()
        )
        account.granted_micro = 1_000_000
        session.commit()
    args = (d["vid"], caller, order["id"])
    service.transition(*args, order["review_digest"], "approve")

    browsers, provider_attempts, requests, observations = [], [], [], []
    launch = BrowserType.launch_persistent_context

    def local_tls(self, *positional, **kwargs):
        kwargs["ignore_https_errors"] = True
        return launch(self, *positional, **kwargs)

    class LocalBrowser(OwnedBrowser):
        def __init__(self, **kwargs):
            super().__init__(**kwargs, allow_loopback_for_tests=True)
            browsers.append(self)

        def observe(self):
            observation = super().observe()
            load = self.last_load_evidence or {}
            observations.append(
                {
                    "qualified": load.get("qualified"),
                    "status": (load.get("response") or {}).get("status"),
                    "ready": load.get("ready_state"),
                    "complete": (load.get("response") or {}).get("complete"),
                }
            )
            return observation

    monkeypatch.setattr(BrowserType, "launch_persistent_context", local_tls)
    monkeypatch.setattr(worker_module, "OwnedBrowser", LocalBrowser)
    # This test pins the Playwright launch (self-signed fixture TLS via ignore_https_errors); the persistent
    # Supymem browser (ChromeFactory over CDP) is covered by test_cu_supymem_browser_wiring.
    monkeypatch.setattr(worker_module.DeviceWorker, "chrome_factory", lambda self: None)
    secret = secrets.token_hex(32)
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", secret)
    app = FastAPI()
    app.include_router(router)

    with TestClient(app) as client:

        def signed_request(method, path, body):
            target = path.removeprefix("/api")
            assertion = sign_assertion(
                secret,
                sub=d["actor"],
                email="",
                role="user",
                path=target,
                method=method,
                device_id=caller.device_id,
            )
            result = client.request(
                method, target, json=body, headers={"X-COS-Assertion": assertion}
            )
            requests.append(
                {
                    "method": method,
                    "route": target.rsplit("/", 1)[-1],
                    "status": result.status_code,
                }
            )
            if result.status_code != 200:
                detail = result.json().get("detail", {})
                raise UpstreamRejected(result.status_code, detail.get("code", ""))
            return result.json()

        def provider(body):
            name = body["text"]["format"]["name"]
            phase = (
                "plan" if "plan" in name else "verify" if "verify" in name else "actor"
            )
            provider_attempts.append(phase)
            if phase == "plan":
                return response(
                    '{"subgoals":["Read first source","Follow link and read second source"]}'
                )
            if phase == "verify":
                return response(
                    '{"passed":true,"reason":"Both observed source values match"}'
                )
            if cancel_during_actor:
                service.transition(*args, order["review_digest"], "cancel")
            browser = browsers[0]
            if urlsplit(browser._page.url).path == "/one":
                target = next(
                    c.id
                    for c in browser._observation.controls
                    if c.label == "Next fixture page"
                )
                action = {"op": "click", "element_id": target}
            else:
                action = {
                    "op": "done",
                    "summary": "First source value: 23; second source value: 41.",
                }
            return response(
                json.dumps({"actions": [action], "memory": "", "subgoal_done": False})
            )

        monkeypatch.setattr(ai, "_provider_call", provider)
        gateway = WorkOrderGateway(
            venture_id=d["vid"],
            order_id=order["id"],
            device_id=caller.device_id,
            request=signed_request,
            account_id=d["actor"],
            cancellable_request=lambda method, path, body, **_kwargs: signed_request(
                method, path, body
            ),
        )
        worker = worker_module.DeviceWorker(gateway, lambda _value: None)
        worker._notify_terminal = lambda *_args, **_kwargs: None
        worker._notify_needs_you = lambda *_args, **_kwargs: None
        root = Path(__file__).resolve().parents[2]
        proof_sources = [
            "services/company_os/app/build/device_orders.py",
            "services/company_os/app/api/device_orders_api.py",
            "services/company_os/app/build/device_browser_ai.py",
            "services/company_os/app/build/device_invocations.py",
            "services/company_os/app/build/device_effects.py",
            "config/llm_pricing.yaml",
            "packages/coding_agents/owned_browser.py",
            "packages/desktop_runtime/cu_owned_browser_adapter.py",
            "packages/desktop_runtime/device_gateway.py",
            "packages/desktop_runtime/device_worker.py",
            "tests/integration/test_postgres_browser_cuai_runtime.py",
        ]
        source_hashes = {
            p: hashlib.sha256((root / p).read_bytes()).hexdigest()
            for p in proof_sources
        }
        worker.run()

    saved = service.retrieve(*args)
    evidence = worker.last.get("evidence", {})
    assert worker.closed and evidence.get("close_returned") is True
    assert evidence.get("processes_stopped") is True
    assert OwnedProcesses.receipt_stopped(evidence.get("inventory"))
    assert worker.last.get("task_verified") is False
    assert gateway.browser_cuai_profile_digest == ai.profile_digest(profile)
    assert not any(r["route"] in {"browser-decision", "ai-step"} for r in requests)
    assert "invocations" in saved, json.dumps(
        {
            "outcome": evidence.get("outcome"),
            "failure_code": worker.last.get("failure_code"),
            "phases": provider_attempts,
            "requests": requests,
            "observations": observations,
        },
        sort_keys=True,
    )
    invocations = saved["invocations"]
    parents = [
        v for v in invocations.values() if v.get("source_kind") == ai.SOURCE_KIND
    ]
    children = [
        v
        for v in invocations.values()
        if v.get("source_kind") == "browser_cuai_actor_effect_v1"
    ]
    assert all(v["state"] == "completed" and v["actual_micro"] == 350 for v in parents)
    assert saved.get("settled_micro") == sum(v["actual_micro"] for v in parents)
    with d["factory"]() as session:
        account = (
            session.query(CreditAccount)
            .filter_by(venture_id=d["vid"], scope="venture")
            .one()
        )
        assert (
            account.reserved_micro == 0
            and account.spent_micro == saved["settled_micro"]
        )
    if cancel_during_actor:
        assert provider_attempts == ["plan", "actor"] and not children
        assert seen == ["/one"] and saved["state"] == "stopped"
        assert (
            saved.get("cancelled_at")
            and saved["native_stop"]["processes_stopped"] is True
        )
        assert not any(r["route"] in {"admit", "receipt"} for r in requests)
        assert worker.last.get("semantic_satisfied") is not True
    else:
        assert provider_attempts == ["plan", "actor", "actor", "verify"]
        assert seen == ["/one", "/two"] and saved["state"] == "stopped"
        assert len(children) == 1 and children[0]["effect"]["state"] == "returned"
        assert children[0]["quote_micro"] == children[0]["actual_micro"] == 0
        assert evidence["outcome"] == "outcome_review_required"
        assert worker.last["semantic_satisfied"] is True
        assert sum(r["route"] == "admit" for r in requests) == 1
        assert sum(r["route"] == "receipt" for r in requests) == 1
        assert len(browsers[0]._cuai_inputs) == 0  # close retires source buffers
    assert source_hashes == {
        p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in proof_sources
    }
    proof_dir = os.environ.get("SUPYMEM_BROWSER_CUAI_JOINT_PROOF_DIR")
    if proof_dir:
        value = {
            "schema_version": 1,
            "completed_utc": datetime.now(timezone.utc).isoformat(),
            "scenario": "cancel_during_actor"
            if cancel_during_actor
            else "returned_input_then_semantic_review",
            "source_files": [
                {"sha256": sha, "hash_kind": "source_file", "path": p}
                for p, sha in source_hashes.items()
            ],
            "requests": [
                {
                    **r,
                    "route": "order_read"
                    if r["route"].startswith("dwo_")
                    else r["route"],
                }
                for r in requests
            ],
            "observations": observations,
            "source_https_get_paths": seen,
            "provider_stub_phases": provider_attempts,
            "external_provider_calls": 0,
            "provider_invoice_verified": False,
            "model_reliability_verified": False,
            "synthetic_known_usage_parent_calls": len(parents),
            "source_effect_children": len(children),
            "provider_unknown_or_inflight": sum(
                v["state"] != "completed" for v in parents
            ),
            "returned_effects": sum(
                v.get("effect", {}).get("state") == "returned" for v in children
            ),
            "settled_synthetic_usage_micro": saved["settled_micro"],
            "account_hold_released": account.reserved_micro == 0,
            "saved_state": saved["state"],
            "cancel_recorded": bool(saved.get("cancelled_at")),
            "worker_closed": worker.closed,
            "owned_processes_stopped": OwnedProcesses.receipt_stopped(
                evidence.get("inventory")
            ),
            "semantic_satisfied": worker.last.get("semantic_satisfied") is True,
            "task_verified": False,
            "installed_app": False,
            "production_authority": False,
            "production_tls_policy_tested": False,
            "physical_background_or_pixel_visibility": False,
        }
        path = Path(proof_dir) / (
            "cancel.json" if cancel_during_actor else "success.json"
        )
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as target:
            json.dump(value, target, sort_keys=True, indent=2)
            target.write("\n")
        path.chmod(0o400)

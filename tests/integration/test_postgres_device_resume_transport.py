"""Temporary combined-source review: real Core custody and native async bridge."""

import asyncio
import json
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from tests.integration.test_postgres_device_resume import claimed
from tests.integration.test_postgres_auth_security import pg_database as pg_database
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db


@contextmanager
def owned(d, monkeypatch, tmp_path, hook=None):
    from core.assertion import sign_assertion
    from app.api import device_orders_api as api
    from desktop_runtime.bridge import create_bridge_app
    from desktop_runtime.device_worker import DeviceWorker
    from desktop_runtime.device_resume import ResumeMonitor

    svc, caller, original = claimed(d)
    saved = svc.retrieve(d["vid"], caller, original["id"])
    secret = "combined-resume-review-test-secret-longer-than32bytes"
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", secret)
    monkeypatch.setenv("DEVICE_BROWSER_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BRIDGE_TOKEN", "synthetic-local-review-token")
    monkeypatch.setenv("GATEWAY_URL", "https://example.invalid")
    monkeypatch.delenv("COMPANY_OS_PUBLIC_URL", raising=False)
    monkeypatch.delenv("BRIDGE_AUTH_OWNER", raising=False)

    async def handler(network):
        if hook is not None:
            await hook(network)
        path = network.url.path.removeprefix("/api")
        body = json.loads(network.content) if network.content else None
        token = sign_assertion(
            secret,
            sub=d["actor"],
            email="",
            role="user",
            path=path,
            method=network.method,
            device_id=caller.device_id,
        )
        request = Request(
            {
                "type": "http",
                "method": network.method,
                "path": path,
                "headers": [(b"x-cos-assertion", token.encode())],
                "query_string": b"",
                "server": ("test", 80),
                "scheme": "http",
            }
        )
        args = (d["vid"], original["id"])
        try:
            if path.endswith("/authority"):
                result = api.current_authority(*args, request)
            elif path.endswith("/human-pause"):
                result = api.register_human_pause(
                    *args, api.HumanPauseBody(**body), request
                )
            elif path.endswith("/human-resume"):
                result = api.pending_human_resume(*args, request)
            elif path.endswith("/human-resume/ack"):
                result = api.acknowledge_human_resume(
                    *args, api.HumanResumeAck(**body), request
                )
            elif path.endswith("/human-pause/clear"):
                result = api.clear_human_pause(
                    *args, api.HumanPauseClear(**body), request
                )
            else:
                result = api.retrieve(*args, request)
        except HTTPException as exc:
            return httpx.Response(exc.status_code, json={"detail": exc.detail})
        return httpx.Response(200, json=result)

    app = create_bridge_app(transport=httpx.MockTransport(handler))
    with TestClient(app):
        session = app.state.bridge_session
        session.account_id, session.device_id = d["actor"], caller.device_id
        session.access_token, session.expires_at, session.connected = (
            "synthetic-access-token",
            time.time() + 900,
            True,
        )
        gateway = app.state.native_order_gateway(d["vid"], original["id"])
        gateway.claimed = True
        gateway.execution_id, gateway.review_digest = (
            saved["execution_id"],
            saved["review_digest"],
        )
        worker = DeviceWorker(gateway, lambda receipt: None)
        worker.deadline = time.monotonic() + saved["manifest"]["duration_seconds"]
        worker.progress("needs_you", pause_kind="captcha")
        worker._record_step(kind="pause", pause_kind="captcha")
        monitor = ResumeMonitor(worker)
        monitor.tick()
        yield svc, caller, original, saved, worker, monitor, app


def test_real_bridge_signed_continue_reaches_same_worker_and_expiry_revokes(
    journey_db, monkeypatch, tmp_path
):
    from app.needs_you import service
    from app.credits.accounts import get_account

    with owned(journey_db, monkeypatch, tmp_path) as (
        svc,
        caller,
        order,
        saved,
        w,
        m,
        app,
    ):
        before = get_account(journey_db["vid"])
        service.resolve(
            m.request_id, venture_id=journey_db["vid"], actor=journey_db["actor"]
        )
        deadline = datetime.fromisoformat(saved["claimed_at"]) + timedelta(
            seconds=saved["manifest"]["duration_seconds"]
        )
        with monkeypatch.context() as clock:
            clock.setattr(svc, "utcnow", lambda: deadline)
            with pytest.raises(PermissionError):
                m.tick()
        assert not w.continue_event.is_set() and not m.accepted
        m.tick()
        assert w.continue_event.is_set() and m.accepted
        assert (
            svc.retrieve(journey_db["vid"], caller, order["id"])["state"] == "claimed"
        )
        assert not w.last["task_verified"]
        after = get_account(journey_db["vid"])
        assert (after.spent_micro, after.reserved_micro) == (
            before.spent_micro,
            before.reserved_micro,
        )


def test_stop_cancels_blocked_resume_poll_without_delivery_or_replay(
    journey_db, monkeypatch, tmp_path
):
    from app.needs_you import service
    from app.build import device_resume
    from desktop_runtime.cancellable_http import ModelRequestStopped

    entered, cleaned = threading.Event(), threading.Event()
    block = False

    async def hook(request):
        if block and request.url.path.endswith("/human-resume"):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

    with owned(journey_db, monkeypatch, tmp_path, hook) as (
        svc,
        caller,
        order,
        saved,
        w,
        m,
        app,
    ):
        service.resolve(
            m.request_id, venture_id=journey_db["vid"], actor=journey_db["actor"]
        )
        block = True
        errors = []

        def poll():
            try:
                m.tick()
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=poll)
        thread.start()
        assert entered.wait(1)
        w.cancel()
        thread.join(1)
        assert not thread.is_alive() and isinstance(errors[0], ModelRequestStopped)
        assert cleaned.wait(1) and not m.accepted
        m.tick()
        assert (
            device_resume.pending(journey_db["vid"], caller, order["id"])["signal"]
            is not None
        )
        assert app.state.model_http.pending == 0 and not app.state.model_http.tasks
        assert not w.last["task_verified"]


def test_lost_ack_then_pause_exit_clears_truthfully_without_redelivery(
    journey_db, monkeypatch, tmp_path
):
    from app.needs_you import service
    from app.build import device_resume

    fail_ack = True

    async def hook(request):
        if fail_ack and request.url.path.endswith("/human-resume/ack"):
            raise httpx.ReadError("simulated lost ack transport")

    with owned(journey_db, monkeypatch, tmp_path, hook) as (
        svc,
        caller,
        order,
        saved,
        w,
        m,
        app,
    ):
        service.resolve(
            m.request_id, venture_id=journey_db["vid"], actor=journey_db["actor"]
        )
        delivered = []
        original = w.continue_founder_handoff

        def deliver():
            delivered.append(id(w))
            return original()

        w.continue_founder_handoff = deliver
        with pytest.raises(RuntimeError):
            m.tick()
        assert m.accepted and w.continue_event.is_set()
        assert (
            device_resume.pending(journey_db["vid"], caller, order["id"])["signal"]
            is not None
        )
        w.progress("acting", resumed_after="needs_you")
        fail_ack = False
        m.tick()
        m.tick()
        assert m.cleared and delivered == [id(w)]
        assert (
            device_resume.pending(journey_db["vid"], caller, order["id"])["signal"]
            is None
        )
        assert (
            svc.retrieve(journey_db["vid"], caller, order["id"])["state"] == "claimed"
        )
        assert not w.last["task_verified"]

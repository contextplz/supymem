"""Actual claimed-order PostgreSQL handoff: durable intent, exact owner, no replay."""
from dataclasses import replace
import pytest
from tests.integration.test_postgres_auth_security import concurrent, pg_database as pg_database
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db
from tests.integration.test_postgres_device_orders import prepare


def claimed(d):
    svc, caller, order = prepare(d)
    args = (d["vid"], caller, order["id"], order["review_digest"])
    svc.transition(*args, "approve")
    svc.transition(*args, "claim")
    return svc, caller, order


def test_continue_atomically_queues_one_owned_signal_and_acknowledges_only_delivery(journey_db):
    from app.build import device_resume as resume
    from app.needs_you import service
    from app.credits.accounts import get_account
    d = journey_db
    _, caller, order = claimed(d)
    token = "a" * 32
    pause = resume.register(d["vid"], caller, order["id"], pause_token=token, pause_kind="captcha")
    before = get_account(d["vid"])
    outcomes = concurrent(lambda: service.resolve(pause["request_id"], venture_id=d["vid"], actor=d["actor"]), count=2)
    assert sum(item is not None for item in outcomes) == 1
    signal = resume.pending(d["vid"], caller, order["id"])["signal"]
    assert signal["pause_token"] == token and signal["device_id"] == caller.device_id
    assert service.list_open(d["vid"]) == []
    with pytest.raises(KeyError):
        resume.pending(d["vid"], replace(caller, device_id="dev_other"), order["id"])
    with pytest.raises(ValueError):
        resume.acknowledge(d["vid"], caller, order["id"], pause_token="b" * 32,
            request_id=pause["request_id"], execution_id=signal["execution_id"])
    result = resume.acknowledge(d["vid"], caller, order["id"], pause_token=token,
        request_id=pause["request_id"], execution_id=signal["execution_id"])
    assert result["state"] == "signal_accepted"
    assert resume.pending(d["vid"], caller, order["id"])["signal"] is None
    assert resume.acknowledge(d["vid"], caller, order["id"], pause_token=token,
        request_id=pause["request_id"], execution_id=signal["execution_id"]) == result
    after = get_account(d["vid"])
    assert (after.spent_micro, after.reserved_micro) == (before.spent_micro, before.reserved_micro)


def test_new_pause_revokes_old_continue_and_cancel_revokes_delivery(journey_db):
    from app.build import device_resume as resume
    from app.needs_you import service
    d = journey_db
    svc, caller, order = claimed(d)
    first = resume.register(d["vid"], caller, order["id"], pause_token="c" * 32, pause_kind="login")
    service.resolve(first["request_id"], venture_id=d["vid"], actor=d["actor"])
    second = resume.register(d["vid"], caller, order["id"], pause_token="d" * 32, pause_kind="captcha")
    assert first["request_id"] != second["request_id"]
    assert resume.pending(d["vid"], caller, order["id"])["signal"] is None
    with pytest.raises(ValueError, match="superseded"):
        resume.register(d["vid"], caller, order["id"], pause_token="c" * 32, pause_kind="login")
    with pytest.raises(resume.ResumeUnavailable):
        service.resolve(second["request_id"], venture_id=d["vid"], actor="someone_else")
    assert len(service.list_open(d["vid"])) == 1
    service.resolve(second["request_id"], venture_id=d["vid"], actor=d["actor"])
    svc.transition(d["vid"], caller, order["id"], order["review_digest"], "cancel")
    with pytest.raises(ValueError):
        resume.pending(d["vid"], caller, order["id"])


def test_lost_resolution_commit_rolls_back_request_and_signal(journey_db, monkeypatch):
    from app.build import device_resume as resume
    from app.needs_you import service
    d = journey_db
    _, caller, order = claimed(d)
    pause = resume.register(d["vid"], caller, order["id"], pause_token="e" * 32, pause_kind="captcha")
    original = resume.queue
    def crash(*args):
        original(*args)
        raise RuntimeError("simulated precommit crash")
    monkeypatch.setattr(resume, "queue", crash)
    with pytest.raises(RuntimeError):
        service.resolve(pause["request_id"], venture_id=d["vid"], actor=d["actor"])
    assert [r["id"] for r in service.list_open(d["vid"])] == [pause["request_id"]]
    assert resume.pending(d["vid"], caller, order["id"])["signal"] is None
    monkeypatch.setattr(resume, "queue", original)
    assert service.resolve(pause["request_id"], venture_id=d["vid"], actor=d["actor"])["resume_state"] == "queued"


def test_signed_api_signal_reaches_actual_native_worker_event_without_effects(journey_db, monkeypatch):
    from starlette.requests import Request
    from fastapi import HTTPException
    from core.assertion import sign_assertion
    from app.api import device_orders_api as api
    from app.needs_you import service
    from app.credits.accounts import get_account
    from desktop_runtime.device_gateway import WorkOrderGateway
    from desktop_runtime.device_worker import DeviceWorker
    from desktop_runtime.device_resume import ResumeMonitor
    d = journey_db
    svc, caller, order = claimed(d)
    saved = svc.retrieve(d["vid"], caller, order["id"])
    monkeypatch.setenv("DEVICE_BROWSER_EXECUTION_ENABLED", "1")
    secret = "owned-resume-test-secret-longer-than-thirty-two-bytes"
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", secret)
    core_path = f"/ventures/{d['vid']}/device-work-orders/{order['id']}"
    def signed(method, path, device=caller.device_id):
        token = sign_assertion(secret, sub=d["actor"], email="", role="user", path=path, method=method, device_id=device)
        return Request({"type": "http", "method": method, "path": path,
            "headers": [(b"x-cos-assertion", token.encode())], "query_string": b"", "server": ("test", 80), "scheme": "http"})
    def request(method, path, body):
        path = path.removeprefix("/api")
        bound = signed(method, path)
        args = (d["vid"], order["id"])
        if path == core_path:
            return api.retrieve(*args, bound)
        if path.endswith("/authority"):
            return api.current_authority(*args, bound)
        if path.endswith("/human-pause"):
            return api.register_human_pause(*args, api.HumanPauseBody(**body), bound)
        if path.endswith("/human-resume"):
            return api.pending_human_resume(*args, bound)
        if path.endswith("/human-resume/ack"):
            return api.acknowledge_human_resume(*args, api.HumanResumeAck(**body), bound)
        raise AssertionError("unexpected endpoint")
    body = api.HumanPauseBody(pause_token="f" * 32, pause_kind="captcha")
    with pytest.raises(HTTPException) as denied:
        api.register_human_pause(d["vid"], order["id"], body, signed("POST", core_path + "/human-pause", "dev_other"))
    assert denied.value.status_code == 404
    unsigned = Request({"type": "http", "method": "POST", "path": core_path + "/human-pause",
        "headers": [], "query_string": b"", "server": ("test", 80), "scheme": "http"})
    with pytest.raises(HTTPException) as no_identity:
        api.register_human_pause(d["vid"], order["id"], body, unsigned)
    assert no_identity.value.status_code == 401
    with pytest.raises(HTTPException) as internal_error:
        api.register_human_pause(d["vid"], order["id"], api.HumanPauseBody(pause_token="f" * 32, pause_kind="provider_timeout"),
            signed("POST", core_path + "/human-pause"))
    assert internal_error.value.status_code == 409
    assert service.list_open(d["vid"]) == []
    gateway = WorkOrderGateway(venture_id=d["vid"], order_id=order["id"], device_id=caller.device_id, request=request)
    gateway.claimed = True
    gateway.review_digest, gateway.execution_id = saved["review_digest"], saved["execution_id"]
    worker = DeviceWorker(gateway, lambda receipt: None)
    worker.progress("needs_you", pause_kind="captcha")
    worker._record_step(kind="pause", pause_kind="captcha")
    monitor = ResumeMonitor(worker)
    before = get_account(d["vid"])
    monitor.tick()
    assert not worker.continue_event.is_set()
    assert service.resolve(monitor.request_id, venture_id=d["vid"], actor=d["actor"])["resume_state"] == "queued"
    assert not worker.continue_event.is_set()
    monitor.tick()
    assert worker.continue_event.is_set()
    assert api.pending_human_resume(d["vid"], order["id"], signed("GET", core_path + "/human-resume"))["signal"] is None
    assert svc.retrieve(d["vid"], caller, order["id"])["state"] == "claimed"
    after = get_account(d["vid"])
    assert (after.spent_micro, after.reserved_micro) == (before.spent_micro, before.reserved_micro)


def test_native_registration_reuses_same_pause_report_without_duplicate_alert(journey_db, monkeypatch):
    from app.build import device_resume as resume
    from app.needs_you import service
    from app.db.supymem_models import NeedsYouRequest
    d = journey_db
    _, caller, order = claimed(d)
    rid = service.request_human(d["vid"], reason_code="captcha", title="Verification", what_we_need="Complete verification",
        how_to_resume="Continue", blocking_ref=f"device:{order['id']}", chat_card=False)
    deliveries = []
    monkeypatch.setattr(service, "_deliver", lambda *args: deliveries.append(args))
    registered = resume.register(d["vid"], caller, order["id"], pause_token="1" * 32, pause_kind="captcha")
    assert registered["request_id"] == rid and deliveries == []
    with d["factory"]() as session:
        assert session.query(NeedsYouRequest).filter(NeedsYouRequest.venture_id == d["vid"]).count() == 1

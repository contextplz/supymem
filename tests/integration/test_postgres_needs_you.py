"""Needs-you custody on migrated PostgreSQL, including concurrent Continue calls."""
from tests.integration.test_postgres_auth_security import concurrent, pg_database as pg_database
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db


def test_concurrent_continue_runs_resume_callback_once(journey_db, monkeypatch):
    from app.needs_you import service
    seen = []
    monkeypatch.setitem(service.RESUME, "probe", seen.append)
    vid = journey_db["vid"]
    rid = service.request_human(vid, reason_code="captcha", title="Verification",
                               what_we_need="Complete verification", how_to_resume="Return to the task",
                               blocking_ref="probe:one", chat_card=False)
    assert rid
    outcomes = concurrent(lambda: service.resolve(rid, venture_id=vid), count=2)
    assert sum(item is not None for item in outcomes) == 1
    assert len(seen) == 1


def test_failed_desktop_delivery_remains_retryable(journey_db, monkeypatch):
    from app import desktop_notices
    from app.needs_you import service
    from app.db.supymem_models import NeedsYouRequest
    def unavailable(**kwargs):
        raise RuntimeError("notification transport unavailable")
    monkeypatch.setattr(desktop_notices, "enqueue", unavailable)
    vid = journey_db["vid"]
    rid = service.request_human(vid, reason_code="captcha", title="Verification",
                               what_we_need="Complete verification", how_to_resume="Return to the task",
                               blocking_ref="probe:retry", severity="blocking", chat_card=False)
    with journey_db["factory"]() as session:
        assert session.get(NeedsYouRequest, rid).notified_at is None
    monkeypatch.setattr(desktop_notices, "enqueue", lambda **kwargs: "notice")
    from datetime import timedelta
    from core.clock import utcnow
    now = utcnow()
    noon = now.replace(hour=12, minute=0, second=0, microsecond=0)
    # Retry outside quiet hours; the reminder schedule holds notices overnight.
    assert service.remind_due(now=noon if noon > now else noon + timedelta(days=1))["digests"] == 1
    with journey_db["factory"]() as session:
        assert session.get(NeedsYouRequest, rid).notified_at is not None


def test_godmode_resolution_commits_durable_wakeup(journey_db):
    from app.needs_you import service
    from app.db.models import Job
    from sqlalchemy import select
    vid = journey_db["vid"]
    rid = service.request_human(vid, reason_code="consent_send", title="Consent",
                               what_we_need="Review sending", how_to_resume="Continue",
                               blocking_ref="godmode:outreach", chat_card=False)
    assert service.resolve(rid, venture_id=vid)
    with journey_db["factory"]() as session:
        job = session.scalar(select(Job).where(Job.dedupe_key == f"needs_you_resume:{rid}"))
        assert job and job.kind == "autopilot.tick" and job.venture_id == vid


def test_device_report_requires_signed_active_actor_and_device(journey_db, monkeypatch):
    import pytest
    from datetime import datetime, timedelta
    from starlette.requests import Request
    from fastapi import HTTPException
    from app.api.needs_you_api import DeviceBody, device_needs_you
    from core.assertion import sign_assertion
    from app.needs_you import service
    from tests.integration.test_postgres_device_orders import prepare

    secret = "needs-you-test-secret-over-thirty-two-characters"
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", secret)
    vid, actor = journey_db["vid"], journey_db["actor"]
    svc, caller, order = prepare(journey_db)
    args = (vid, caller, order["id"], order["review_digest"])
    svc.transition(*args, "approve")
    claimed = svc.transition(*args, "claim")
    assert claimed["manifest"]["duration_seconds"] == 60
    assert "limits" not in claimed["manifest"]
    path = f"/ventures/{vid}/needs-you/device"
    def request(device):
        token = sign_assertion(secret, sub=actor, email="", role="user", path=path, method="POST", device_id=device)
        return Request({"type": "http", "method": "POST", "path": path, "headers": [(b"x-cos-assertion", token.encode())],
                        "query_string": b"", "server": ("testserver", 80), "scheme": "http"})
    body = DeviceBody(order_id=order["id"], pause_kind="captcha")
    with pytest.raises(HTTPException) as wrong:
        device_needs_you(vid, body, request("dev_other"))
    assert wrong.value.status_code == 404 and service.list_open(vid) == []
    assert device_needs_you(vid, body, request("dev_one"))["id"]
    # The real invocation deadline, with no invented grace period, revokes reporting.
    with monkeypatch.context() as clock:
        deadline = datetime.fromisoformat(claimed["claimed_at"]) + timedelta(seconds=60)
        clock.setattr(svc, "utcnow", lambda: deadline)
        with pytest.raises(HTTPException) as expired:
            device_needs_you(vid, body, request("dev_one"))
        assert expired.value.status_code == 409
    svc.transition(*args, "cancel")
    with pytest.raises(HTTPException) as cancelled:
        device_needs_you(vid, body, request("dev_one"))
    assert cancelled.value.status_code == 409
    assert len(service.list_open(vid)) == 1

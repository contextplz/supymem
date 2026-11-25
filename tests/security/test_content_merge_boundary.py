"""Incoming content identity cannot weaken the canonical founder boundary."""

import pytest

from tests.security.test_core_signed_boundary import client as client, headers


def test_alternate_consumer_identity_and_hosted_llm_are_not_mounted(client):
    cases = [
        ("POST", "/api/auth/v2/cognito/email-otp/start"),
        ("POST", "/api/auth/v2/open-app/consume"),
        ("GET", "/api/v1/account"),
        ("GET", "/api/v1/founder/users"),
        ("POST", "/api/v1/reports/crash"),
        ("POST", "/desktop/crash-queue/flush"),
        ("GET", "/desktop/bug-report/prefs"),
    ]
    for method, path in cases:
        response = client.request(method, path, headers=headers(path, method))
        assert response.status_code == 404, (path, response.text)


def test_bug_report_uses_signed_identity_and_explicit_account_opt_in(client):
    from app.db.models import DesktopBugReport
    from app.state import db

    path = "/desktop/bug-report"
    body = {"title": "A real issue", "body": "The panel closed unexpectedly."}
    assert client.post(path, json=body).status_code == 401
    anonymous = client.post(path, json=body, headers=headers(path, "POST"))
    assert anonymous.status_code == 201, anonymous.text
    assert anonymous.json()["status"] == "received"
    linked = client.post(path, json={**body, "include_account_id": True}, headers=headers(path, "POST"))
    assert linked.status_code == 201, linked.text
    with db()() as session:
        first = session.get(DesktopBugReport, anonymous.json()["id"])
        second = session.get(DesktopBugReport, linked.json()["id"])
        assert first.account_id is None and first.anonymous is True
        assert second.account_id == "usr_alice" and second.anonymous is False
        assert first.logs_text is None and second.logs_text is None
    forged = client.post(path, json={**body, "account_id": "usr_bob"}, headers=headers(path, "POST"))
    assert forged.status_code == 422


def test_bug_report_disabled_does_not_claim_success(client, monkeypatch):
    monkeypatch.setenv("BUG_REPORTS_INGEST_ENABLED", "0")
    path = "/desktop/bug-report"
    response = client.post(path, json={"title": "Issue", "body": "Details"}, headers=headers(path, "POST"))
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "ingest_disabled"


def test_legacy_consumer_session_cannot_be_issued_in_hosted_mode(client, monkeypatch):
    from app.consumer.cognito import CognitoIdentity
    from app.consumer.session import SessionError, issue_session

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ACCOUNT_SESSION_SECRET", "strong-but-not-canonical-session-secret")
    with pytest.raises(SessionError) as failure:
        issue_session(user_id="csu_legacy", identity=CognitoIdentity("sub", "founder@example.test"))
    assert failure.value.code == "legacy_account_auth_disabled"

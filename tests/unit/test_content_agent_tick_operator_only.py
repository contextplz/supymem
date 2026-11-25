"""SEC1: the operator 'Run agent cycle' route is gated server-side, not just hidden in the founder UI."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.company_os_harness import load_company_os_app


def test_account_session_gets_403_and_operator_does_not(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/tick.db")
    monkeypatch.setenv("SANDBOX_MODE", "true")
    monkeypatch.setenv("ACCOUNT_SESSION_SECRET", "tick-account-session-secret-32b!!")
    main = load_company_os_app()
    from core.clock import utcnow

    from app import state as state_mod
    from app.consumer.cognito import CognitoIdentity
    from app.consumer.session import issue_session
    from app.db.models import ConsumerUser, Person
    from app.state import db

    state_mod.reset_db_factory()
    now = utcnow()
    with db()() as session:
        session.add(Person(id="per_founder000000", display_name="F", created_at=now))
        session.add(ConsumerUser(id="csu_founder000000", cognito_sub="f@example.com", email="f@example.com",
                                 person_id="per_founder000000", plan="trial", plan_started_at=now,
                                 plan_expires_at=now, status="active", created_at=now, updated_at=now))
        session.commit()
    token = issue_session(user_id="csu_founder000000",
                          identity=CognitoIdentity(sub="f@example.com", email="f@example.com"))
    with TestClient(main.create_app()) as client:
        denied = client.post("/api/v1/content/agent/tick", headers={"Authorization": f"Bearer {token}"})
        assert denied.status_code == 403 and denied.json()["detail"]["code"] == "operator_only"
        assert client.post("/api/v1/content/agent/tick").status_code != 403

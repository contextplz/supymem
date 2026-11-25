"""OT-33: hosted session without membership is 404, never 403."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def setup(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ot33.db")
    monkeypatch.setenv("SANDBOX_MODE", "true")
    monkeypatch.setenv("OPS_API_KEY", "ot33-ops-key")
    monkeypatch.setenv("ACCOUNT_SESSION_SECRET", "ot33-account-session-secret-32b!!")
    main = load_company_os_app()
    from app import state as state_mod
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk
    from app.workspaces.service import create_workspace

    state_mod.reset_db_factory()
    ensure_desk(LOCAL_PERSON_ID)
    hosted = create_workspace(
        kind="company",
        name="HostedCognito",
        entry_mode="existing_company",
        owner_id=LOCAL_PERSON_ID,
        tenancy="managed",
    )
    return {"main": main, "hosted": hosted.id}


def test_token_without_membership_is_404(setup):
    from core.clock import utcnow
    from core.ids import new_id

    from app.consumer.cognito import CognitoIdentity
    from app.consumer.session import issue_session
    from app.db.models import ConsumerUser, Person
    from app.state import db

    now = utcnow()
    with db()() as session:
        session.add(Person(id="per_stranger00000", display_name="Stranger", created_at=now))
        session.add(
            ConsumerUser(
                id="csu_stranger00000",
                cognito_sub="fake-stranger@example.com",
                email="stranger@example.com",
                person_id="per_stranger00000",
                plan="trial",
                plan_started_at=now,
                plan_expires_at=now,
                status="active",
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()

    token = issue_session(
        user_id="csu_stranger00000",
        identity=CognitoIdentity(sub="fake-stranger@example.com", email="stranger@example.com"),
    )
    with TestClient(setup["main"].create_app()) as client:
        r = client.get(
            "/api/v1/outreach/prospects",
            headers={
                "X-Workspace-Id": setup["hosted"],
                "X-API-Key": "ot33-ops-key",
                "Authorization": f"Bearer {token}",
            },
        )
        assert r.status_code == 404
        assert r.status_code != 403

        own = client.get(
            "/api/v1/outreach/prospects",
            headers={
                "X-Workspace-Id": setup["hosted"],
                "X-API-Key": "ot33-ops-key",
            },
        )
        assert own.status_code == 200

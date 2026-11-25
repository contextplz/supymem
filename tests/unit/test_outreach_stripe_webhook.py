"""OT-07: Stripe webhook ignores caller workspace; missing secret is 503."""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ot07.db")
    monkeypatch.setenv("SANDBOX_MODE", "true")
    monkeypatch.delenv("OPS_API_KEY", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    main = load_company_os_app()
    from app import state as state_mod

    state_mod.reset_db_factory()
    return main


def test_missing_secret_returns_503(env):
    with TestClient(env.create_app()) as client:
        r = client.post(
            "/api/v1/finance/stripe/webhook?workspace_id=wsp_B",
            content=b'{"id":"evt_x","type":"invoice.paid"}',
            headers={"X-Workspace-Id": "wsp_B"},
        )
        assert r.status_code == 503


def test_forged_event_does_not_write_caller_workspace(env, monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    from app.db.models import OpsStripeEvent, Person, Workspace, WorkspaceMember
    from app.state import db
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk
    from core.clock import utcnow
    from core.ids import new_id

    ensure_desk(LOCAL_PERSON_ID)
    now = utcnow()
    with db()() as session:
        session.add(Person(id="per_b000000000", display_name="B", created_at=now))
        session.add(
            Workspace(
                id="wsp_B",
                kind="company",
                name="TenantB",
                entry_mode="existing_company",
                owner_id="per_b000000000",
                tenancy="local",
                autonomy_level="L1",
                observe_level="O0",
                settings_json="{}",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            WorkspaceMember(
                id=new_id("wsm"),
                workspace_id="wsp_B",
                person_id="per_b000000000",
                role="owner",
                can_approve=1,
                created_at=now,
            )
        )
        session.commit()

    forged = json.dumps(
        {
            "id": "evt_forged",
            "type": "invoice.paid",
            "data": {"object": {"metadata": {"workspace_id": "wsp_A_not_real"}}},
        }
    ).encode()
    with TestClient(env.create_app()) as client:
        r = client.post(
            "/api/v1/finance/stripe/webhook?workspace_id=wsp_B",
            content=forged,
            headers={
                "X-Workspace-Id": "wsp_B",
                "Stripe-Signature": "t=1,v1=deadbeef",
            },
        )
        assert r.status_code in {400, 503}

    with db()() as session:
        rows = list(
            session.scalars(
                select(OpsStripeEvent).where(OpsStripeEvent.workspace_id == "wsp_B")
            )
        )
        assert rows == []

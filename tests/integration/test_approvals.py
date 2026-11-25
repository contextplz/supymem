"""Approval routing, expiry, escalation and batching (42 §9, 46 T10.65-T10.67)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/apr.db")
    main = load_company_os_app()
    from app import state as state_mod

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    with TestClient(main.create_app()) as c:
        yield c


@pytest.fixture()
def workspace(client: TestClient) -> str:
    return client.post("/workspaces", json={"kind": "venture", "name": "Acme"}).json()["id"]


def _request(client: TestClient, ws: str, **kwargs) -> dict:
    body = {
        "kind": "engagement_brief",
        "subject_ref": "eng_test00000001",
        "summary": "Ship the changelog update",
        "requested_by": "agt_growth00001",
        "risk": "low",
    }
    body.update(kwargs)
    r = client.post(f"/workspaces/{ws}/approvals", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_request_routes_to_the_owner_and_notifies_once(client, workspace) -> None:
    view = _request(client, workspace)
    assert view["status"] == "pending"
    assert view["assigned_to"] == "per_local000000"
    assert view["notified_count"] == 1
    assert view["expires_at"]


def test_owner_can_approve(client, workspace) -> None:
    view = _request(client, workspace)
    r = client.post(
        f"/approvals/{view['id']}/decide",
        json={"decision": "approved", "decided_by": "per_local000000"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"
    assert r.json()["decided_by"] == "per_local000000"


def test_nobody_can_approve_their_own_request(client, workspace) -> None:
    view = _request(client, workspace, requested_by="per_local000000")
    r = client.post(
        f"/approvals/{view['id']}/decide",
        json={"decision": "approved", "decided_by": "per_local000000"},
    )
    assert r.status_code == 403
    assert r.json()["detail"]["error"]["code"] == "self_approval"


def test_a_contributor_cannot_approve(client, workspace) -> None:
    invite = client.post(
        f"/workspaces/{workspace}/invites",
        json={"email": "sam@example.com", "role": "contributor"},
    ).json()
    token = invite["link"].rsplit("/", 1)[-1]
    joined = client.post(f"/invites/{token}/accept", json={}).json()

    view = _request(client, workspace)
    r = client.post(
        f"/approvals/{view['id']}/decide",
        json={"decision": "approved", "decided_by": joined["person_id"]},
    )
    assert r.status_code == 403
    assert r.json()["detail"]["error"]["code"] == "cannot_approve"


def test_a_maintainer_can_approve(client, workspace) -> None:
    invite = client.post(
        f"/workspaces/{workspace}/invites",
        json={"email": "mo@example.com", "role": "maintainer"},
    ).json()
    token = invite["link"].rsplit("/", 1)[-1]
    joined = client.post(f"/invites/{token}/accept", json={}).json()

    view = _request(client, workspace)
    r = client.post(
        f"/approvals/{view['id']}/decide",
        json={"decision": "approved", "decided_by": joined["person_id"]},
    )
    assert r.status_code == 200, r.text


def test_deciding_twice_is_refused(client, workspace) -> None:
    view = _request(client, workspace)
    body = {"decision": "approved", "decided_by": "per_local000000"}
    assert client.post(f"/approvals/{view['id']}/decide", json=body).status_code == 200
    second = client.post(f"/approvals/{view['id']}/decide", json=body)
    assert second.status_code == 409
    assert second.json()["detail"]["error"]["code"] == "already_decided"


def test_expiry_blocks_the_work_and_never_proceeds(client, workspace) -> None:
    from app.approvals import service
    from app.db.models import Engagement
    from app.state import db
    from core.clock import utcnow
    from core.ids import new_id

    eid = new_id("eng")
    now = utcnow()
    factory = db()
    with factory() as session:
        session.add(
            Engagement(
                id=eid,
                workspace_id=workspace,
                requested_by="per_local000000",
                size="task",
                title="Ship it",
                request_verbatim="ship it",
                definition_of_done="shipped",
                status="awaiting_goahead",
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()

    view = _request(client, workspace, subject_ref=eid)
    expired = service.expire_due(now=now + timedelta(hours=73))
    assert view["id"] in expired

    assert client.get(f"/approvals/{view['id']}").json()["status"] == "expired"
    with factory() as session:
        row = session.get(Engagement, eid)
        assert row is not None
        assert row.status == "blocked"
        assert row.blocked_reason == "nobody decided in 72h"


def test_expired_approval_cannot_be_decided(client, workspace) -> None:
    from app.approvals import service
    from core.clock import utcnow

    view = _request(client, workspace)
    service.expire_due(now=utcnow() + timedelta(hours=73))
    r = client.post(
        f"/approvals/{view['id']}/decide",
        json={"decision": "approved", "decided_by": "per_local000000"},
    )
    assert r.status_code == 409


def test_exactly_two_notifications_then_silence(client, workspace) -> None:
    from app.approvals import service
    from core.clock import utcnow

    view = _request(client, workspace)
    assert view["notified_count"] == 1

    halfway = utcnow() + timedelta(hours=37)
    first_sweep = service.sweep_notifications(now=halfway)
    assert any(i == view["id"] for i, _ in first_sweep)
    assert client.get(f"/approvals/{view['id']}").json()["notified_count"] == 2

    second_sweep = service.sweep_notifications(now=utcnow() + timedelta(hours=71))
    assert all(i != view["id"] for i, _ in second_sweep)
    assert client.get(f"/approvals/{view['id']}").json()["notified_count"] == 2


def test_batch_refuses_mixed_kinds(client, workspace) -> None:
    a = _request(client, workspace, kind="engagement_brief")
    b = _request(client, workspace, kind="proposal", subject_ref="prp_1")
    r = client.post(
        "/approvals/batch-decide",
        json={
            "ids": [a["id"], b["id"]],
            "decision": "approved",
            "decided_by": "per_local000000",
        },
    )
    assert r.status_code == 422
    assert r.json()["detail"]["error"]["code"] == "mixed_kind"


def test_batch_refuses_mixed_risk(client, workspace) -> None:
    a = _request(client, workspace, risk="low")
    b = _request(client, workspace, risk="high", subject_ref="eng_other0000001")
    r = client.post(
        "/approvals/batch-decide",
        json={
            "ids": [a["id"], b["id"]],
            "decision": "approved",
            "decided_by": "per_local000000",
        },
    )
    assert r.status_code == 422
    assert r.json()["detail"]["error"]["code"] == "mixed_risk"


def test_batch_refuses_high_risk_even_when_uniform(client, workspace) -> None:
    a = _request(client, workspace, risk="high")
    b = _request(client, workspace, risk="high", subject_ref="eng_other0000002")
    r = client.post(
        "/approvals/batch-decide",
        json={
            "ids": [a["id"], b["id"]],
            "decision": "approved",
            "decided_by": "per_local000000",
        },
    )
    assert r.status_code == 422
    assert r.json()["detail"]["error"]["code"] == "risk_too_high"


def test_batch_is_capped_at_ten(client, workspace) -> None:
    ids = [
        _request(client, workspace, subject_ref=f"eng_bulk{i:08d}")["id"]
        for i in range(11)
    ]
    r = client.post(
        "/approvals/batch-decide",
        json={"ids": ids, "decision": "approved", "decided_by": "per_local000000"},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["error"]["code"] == "batch_too_large"


def test_batch_writes_one_audit_row_per_item(client, workspace) -> None:
    from sqlalchemy import select

    from app.db.models import EventLog
    from app.state import db

    ids = [
        _request(client, workspace, subject_ref=f"eng_ok{i:010d}")["id"] for i in range(3)
    ]
    r = client.post(
        "/approvals/batch-decide",
        json={"ids": ids, "decision": "approved", "decided_by": "per_local000000"},
    )
    assert r.status_code == 200, r.text

    factory = db()
    with factory() as session:
        rows = session.scalars(
            select(EventLog).where(EventLog.topic == "approval.approved")
        ).all()
        assert len(rows) == 3
        assert {r.approval_id for r in rows} == set(ids)


def test_seven_floor_actions_need_a_human_at_l4() -> None:
    from app.approvals.floor import FLOOR_ACTIONS, requires_human

    assert len(FLOOR_ACTIONS) == 7
    for action in FLOOR_ACTIONS:
        assert requires_human(action, autonomy_level="L4") is not None

    # An ordinary action at L4 does not.
    assert requires_human("read_a_file", autonomy_level="L4") is None
    # …unless the warden flagged it high risk without confidence.
    assert (
        requires_human("read_a_file", autonomy_level="L4", risk="high", warden_confidence=0.5)
        is not None
    )
    assert (
        requires_human("read_a_file", autonomy_level="L4", risk="high", warden_confidence=0.95)
        is None
    )

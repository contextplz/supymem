"""trace() walks a deliverable back to a human intent in one query (42 §5, T10.68)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from tests.company_os_harness import load_company_os_app

DELIVERABLE = "repo/CHANGELOG.md"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/trace.db")
    main = load_company_os_app()
    from app import state as state_mod

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    with TestClient(main.create_app()) as c:
        yield c


@pytest.fixture()
def seeded(client: TestClient) -> dict[str, str]:
    """A delivered engagement with an approval, an agent run and a model call."""
    from app.db.models import (
        AgentInstance,
        AgentRun,
        ConnectionEvent,
        Engagement,
        TokenLedger,
        Venture,
    )
    from app.state import db
    from core.clock import utcnow
    from core.ids import new_id

    ws = client.post("/workspaces", json={"kind": "company", "name": "Acme"}).json()["id"]
    eid = new_id("eng")
    run_id = new_id("run")
    agent_id = "agt_writer00001"
    venture_id = new_id("ven")
    now = utcnow()
    factory = db()
    with factory() as session:
        # agent_runs.agent_id is a real foreign key, and agent_instances needs a
        # venture, so the whole chain has to exist for the trace to be honest.
        session.add(
            Venture(
                id=venture_id,
                workspace_id=ws,
                name="Acme",
                raw_idea="an existing company we were invited into",
                stage="draft",
                autonomy_level="L1",
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        session.add(
            AgentInstance(
                id=agent_id,
                venture_id=venture_id,
                spec_id="spc_writer00001",
                spec_version="1.0.3",
                status="active",
                created_at=now,
            )
        )
        session.flush()
        session.add(
            Engagement(
                id=eid,
                workspace_id=ws,
                requested_by="per_local000000",
                size="task",
                title="Update the changelog",
                request_verbatim="please update the changelog for the 2.1 release",
                definition_of_done="CHANGELOG.md lists 2.1",
                status="delivered",
                deliverables_json=json.dumps([{"path": DELIVERABLE, "kind": "file"}]),
                created_at=now,
                updated_at=now,
                delivered_at=now,
            )
        )
        session.add(
            AgentRun(
                id=run_id,
                venture_id=ws,
                agent_id=agent_id,
                spec_version="1.0.3",
                trigger="engagement",
                goal="update the changelog",
                status="done",
                cost_usd=0.004,
                started_at=now,
            )
        )
        session.add(
            TokenLedger(
                id=new_id("tkl"),
                venture_id=ws,
                run_id=run_id,
                purpose="engagement.work",
                tier="balanced",
                tokens_in=800,
                tokens_out=200,
                cost_usd=0.004,
                created_at=now,
            )
        )
        session.add(
            ConnectionEvent(
                id=new_id("cev"),
                connection_id="con_github000001",
                workspace_id=ws,
                at=now,
                direction="write",
                operation="create_pull_request",
                target="acme/repo#12",
                actor=agent_id,
                engagement_id=eid,
                outcome="ok",
            )
        )
        session.commit()

    approval = client.post(
        f"/workspaces/{ws}/approvals",
        json={
            "kind": "engagement_brief",
            "subject_ref": eid,
            "summary": "Update the changelog",
            "requested_by": agent_id,
            "risk": "low",
        },
    ).json()
    client.post(
        f"/approvals/{approval['id']}/decide",
        json={"decision": "approved", "decided_by": "per_local000000"},
    )
    return {"workspace_id": ws, "engagement_id": eid, "approval_id": approval["id"]}


def test_trace_from_a_deliverable_path(client, seeded) -> None:
    body = client.get("/trace", params={"ref": DELIVERABLE}).json()
    assert body["found"] is True
    assert body["engagement"]["id"] == seeded["engagement_id"]
    # The whole point: the original human words, not a paraphrase.
    assert (
        body["engagement"]["request_verbatim"]
        == "please update the changelog for the 2.1 release"
    )
    assert body["requested_by"]["id"] == "per_local000000"


def test_trace_includes_the_approval_and_who_decided(client, seeded) -> None:
    body = client.get("/trace", params={"ref": seeded["engagement_id"]}).json()
    assert len(body["approvals"]) == 1
    approval = body["approvals"][0]
    assert approval["id"] == seeded["approval_id"]
    assert approval["status"] == "approved"
    assert approval["decided_by"] == "per_local000000"


def test_trace_includes_model_cost_and_what_we_touched(client, seeded) -> None:
    body = client.get("/trace", params={"ref": seeded["engagement_id"]}).json()
    assert body["model_calls"]
    assert body["cost_usd"] == pytest.approx(0.004)
    assert body["agent_runs"][0]["spec_version"] == "1.0.3"

    events = body["connection_events"]
    assert len(events) == 1
    assert events[0]["operation"] == "create_pull_request"
    assert events[0]["target"] == "acme/repo#12"


def test_a_superseded_agent_version_still_resolves(client, seeded) -> None:
    """History stays honest after Evolution promotes a newer version."""
    body = client.get("/trace", params={"ref": seeded["engagement_id"]}).json()
    versions = {r["spec_version"] for r in body["agent_runs"]}
    assert versions == {"1.0.3"}


def test_unknown_ref_reports_not_found_rather_than_empty(client, seeded) -> None:
    body = client.get("/trace", params={"ref": "repo/nope.md"}).json()
    assert body["found"] is False
    assert body["engagement"] is None


def test_audit_list_is_readable_and_filterable(client, seeded) -> None:
    ws = seeded["workspace_id"]
    body = client.get(f"/workspaces/{ws}/audit").json()
    assert body["items"]
    for item in body["items"]:
        assert item["line"]
        assert item["actor_id"]
        assert item["actor_kind"]

    approved = client.get(
        f"/workspaces/{ws}/audit", params={"topic": "approval.approved"}
    ).json()
    assert len(approved["items"]) == 1
    assert "approved a request" in approved["items"][0]["line"]


def test_audit_pagination_is_stable(client, seeded) -> None:
    ws = seeded["workspace_id"]
    page = client.get(f"/workspaces/{ws}/audit", params={"limit": 1}).json()
    assert len(page["items"]) == 1
    if page["has_more"]:
        nxt = client.get(
            f"/workspaces/{ws}/audit",
            params={"limit": 1, "before_seq": page["next_before_seq"]},
        ).json()
        assert nxt["items"][0]["seq"] < page["items"][0]["seq"]

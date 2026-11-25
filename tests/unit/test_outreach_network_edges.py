"""OT-40: network edges cannot mix workspaces or use LinkedIn as source."""

from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture(params=["warehouse.example", "gardening.example"])
def two_ws(tmp_path, monkeypatch, request):
    monkeypatch.setenv("COS_TEST_APP_REUSE", "1")
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ot40.db")
    monkeypatch.setenv("SANDBOX_MODE", "true")
    load_company_os_app()
    from app import state as state_mod
    from app.gtm.relationships.ingest import ingest_paste
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk
    from app.workspaces.service import create_workspace

    state_mod.reset_db_factory()
    desk_a = ensure_desk(LOCAL_PERSON_ID)
    ws_b = create_workspace(
        kind="company",
        name=request.param,
        entry_mode="existing_company",
        owner_id=LOCAL_PERSON_ID,
    )
    a = ingest_paste(
        desk_a.id,
        source="profile_paste",
        raw_text=f'Ada A\nada@{request.param}\nFounder\n"we still run this workflow in a spreadsheet"',
        enrich=False,
    )
    a2 = ingest_paste(
        desk_a.id,
        source="profile_paste",
        raw_text=f'Bea A\nbea@{request.param}\nFounder\n"we still run this workflow in a spreadsheet"',
        enrich=False,
    )
    b = ingest_paste(
        ws_b.id,
        source="profile_paste",
        raw_text=f'Bob B\nbob@{request.param}\nFounder\n"we still run this workflow in a spreadsheet"',
        enrich=False,
    )
    return desk_a.id, ws_b.id, a["contact_id"], a2["contact_id"], b["contact_id"]


def test_same_workspace_edge_ok(two_ws):
    from app.gtm.relationships.edges import record_edge

    a, _b, c1, c2, _cb = two_ws
    out = record_edge(a, from_contact_id=c1, to_contact_id=c2, source="tenant_gmail")
    assert out["source"] == "tenant_gmail"


def test_mixed_workspace_insert_fails(two_ws):
    from app.gtm.relationships.edges import NetworkEdgeError, record_edge

    a, _b, c1, _c2, cb = two_ws
    with pytest.raises(NetworkEdgeError) as exc:
        record_edge(a, from_contact_id=c1, to_contact_id=cb, source="tenant_slack")
    assert exc.value.code == "mixed_workspace_edge"


def test_linkedin_source_forbidden(two_ws):
    from app.gtm.relationships.edges import NetworkEdgeError, record_edge

    a, _b, c1, c2, _cb = two_ws
    with pytest.raises(NetworkEdgeError) as exc:
        record_edge(a, from_contact_id=c1, to_contact_id=c2, source="linkedin")
    assert exc.value.code == "linkedin_source_forbidden"


def test_orm_insert_mixed_workspace_fails(two_ws):
    from core.clock import utcnow
    from core.ids import new_id

    from app.db.models import GtmNetworkEdge
    from app.gtm.relationships.edges import NetworkEdgeError  # registers listener
    from app.state import db

    a, _b, c1, _c2, cb = two_ws
    with pytest.raises(NetworkEdgeError):
        with db()() as session:
            session.add(
                GtmNetworkEdge(
                    id=new_id("gtmed"),
                    workspace_id=a,
                    from_contact_id=c1,
                    to_contact_id=cb,
                    source="tenant_asserted",
                    created_at=utcnow(),
                )
            )
            session.commit()

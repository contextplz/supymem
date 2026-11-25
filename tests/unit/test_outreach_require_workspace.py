"""OT-05: require_workspace returns 404 (not 403) for a non-member header."""

from __future__ import annotations

import pytest
from tests.company_os_harness import company_os_test_client as TestClient

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ot05.db")
    monkeypatch.setenv("SANDBOX_MODE", "true")
    monkeypatch.delenv("OPS_API_KEY", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)
    main = load_company_os_app()
    from app import state as state_mod

    state_mod.reset_db_factory()
    with TestClient(main.create_app()) as c:
        yield c


def test_foreign_workspace_header_is_404_not_403(client):
    from app.db.models import Person, Workspace, WorkspaceMember
    from app.state import db
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk
    from core.clock import utcnow
    from core.ids import new_id

    desk = ensure_desk(LOCAL_PERSON_ID)
    now = utcnow()
    with db()() as session:
        session.add(
            Person(id="per_other000000", display_name="Other", created_at=now)
        )
        session.add(
            Workspace(
                id="wsp_other",
                kind="company",
                name="OtherCo",
                entry_mode="existing_company",
                owner_id="per_other000000",
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
                workspace_id="wsp_other",
                person_id="per_other000000",
                role="owner",
                can_approve=1,
                created_at=now,
            )
        )
        session.commit()

    r = client.get(
        "/api/v1/outreach/prospects",
        headers={"X-Workspace-Id": "wsp_other"},
    )
    assert r.status_code == 404
    assert r.status_code != 403
    own = client.get(
        "/api/v1/outreach/prospects",
        headers={"X-Workspace-Id": desk.id},
    )
    assert own.status_code == 200

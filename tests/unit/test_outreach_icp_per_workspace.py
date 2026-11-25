"""OT-25: second workspace does not inherit ICP rows."""

from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def two_ws(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ot25b.db")
    monkeypatch.setenv("SANDBOX_MODE", "true")
    load_company_os_app()
    from app import state as state_mod
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk
    from app.workspaces.service import create_workspace

    state_mod.reset_db_factory()
    a = ensure_desk(LOCAL_PERSON_ID)
    b = create_workspace(
        kind="company",
        name="OtherCo",
        entry_mode="existing_company",
        owner_id=LOCAL_PERSON_ID,
    )
    return a.id, b.id


def test_icp_profiles_are_workspace_scoped(two_ws):
    from sqlalchemy import select

    from app.db.models import GtmMarketProfile
    from app.gtm.relationships.icp_seed import seed_icp_template
    from app.state import db

    a, b = two_ws
    seed_icp_template(a)
    seed_icp_template(b)
    with db()() as session:
        ids_a = {
            row.id
            for row in session.scalars(
                select(GtmMarketProfile).where(GtmMarketProfile.workspace_id == a)
            )
        }
        ids_b = {
            row.id
            for row in session.scalars(
                select(GtmMarketProfile).where(GtmMarketProfile.workspace_id == b)
            )
        }
    assert ids_a
    assert ids_b
    assert ids_a.isdisjoint(ids_b)

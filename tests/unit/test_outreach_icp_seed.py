"""OT-25: ICP seed is per-workspace template copy, not a shared graph."""

from __future__ import annotations

import json

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def two_ws(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ot25.db")
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


def test_template_keys_present(two_ws):
    from app.gtm.relationships.icp_seed import get_segment, seed_icp_template

    a, _ = two_ws
    seed_icp_template(a)
    seg = get_segment(a)
    assert "roles" in seg
    assert "template" in seg


def test_second_workspace_does_not_inherit(two_ws):
    from app.gtm.relationships.icp_seed import get_segment, save_segment, seed_icp_template

    a, b = two_ws
    seed_icp_template(a)
    seed_icp_template(b)
    save_segment(a, {**get_segment(a), "who": "only workspace A sentence here"})
    assert "only workspace A" in json.dumps(get_segment(a))
    assert "only workspace A" not in json.dumps(get_segment(b))

"""OT-36: hosted send_enabled refused until DPA click-wrap."""

from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def hosted(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ot36.db")
    monkeypatch.setenv("SANDBOX_MODE", "true")
    load_company_os_app()
    from app import state as state_mod
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk
    from app.workspaces.service import create_workspace

    state_mod.reset_db_factory()
    ensure_desk(LOCAL_PERSON_ID)
    ws = create_workspace(
        kind="company",
        name="HostedDPA",
        entry_mode="existing_company",
        owner_id=LOCAL_PERSON_ID,
        tenancy="managed",
    )
    return ws.id


def test_send_enabled_refused_until_dpa(hosted):
    from app.gtm.relationships.hosted import accept_dpa, set_send_enabled
    from app.gtm.relationships.service import OutreachError
    from app.gtm.supymem.factory import set_run

    with pytest.raises(OutreachError) as exc:
        set_send_enabled(hosted, True)
    assert exc.value.status == 403
    assert exc.value.code == "dpa_required"

    with pytest.raises(OutreachError):
        set_run(hosted, send=True)

    accept_dpa(hosted)
    out = set_send_enabled(hosted, True)
    assert out["send_enabled"] is True

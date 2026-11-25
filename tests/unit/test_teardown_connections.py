"""Provisioning must not tear down Connection-backed targets (R14)."""

from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.mark.asyncio
async def test_teardown_refuses_connected_workspace(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/td.db")
    monkeypatch.setenv("CONNECTORS_MODE", "local")
    monkeypatch.setenv("PROVISIONING_MODE", "local")
    load_company_os_app()
    from app.connectors import service as cons
    from app.provisioning.teardown import ConnectionBackedTeardown, teardown_venture
    from app.workspaces.service import create_workspace
    from app.db.models import Venture
    from app.state import db
    from core.clock import utcnow
    from core.ids import new_id

    ws = create_workspace(kind="venture", name="ConnectedCo")
    now = utcnow()
    ven = Venture(
        id=new_id("ven"),
        workspace_id=ws.id,
        name="ConnectedCo",
        raw_idea="connected",
        stage="draft",
        autonomy_level="L1",
        created_at=now,
        updated_at=now,
    )
    factory = db()
    with factory() as session:
        session.add(ven)
        session.commit()
        vid = ven.id

    await cons.create_connection(
        workspace_id=ws.id,
        provider="linear",
        auth_kind="api_key",
    )
    with pytest.raises(ConnectionBackedTeardown):
        await teardown_venture(vid)

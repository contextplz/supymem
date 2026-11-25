from __future__ import annotations

from datetime import timedelta

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def org_env(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/org.db")
    load_company_os_app()
    from app import state
    from app.db.models import AgentInstance, Venture
    from core.clock import utcnow

    state.reset_db_factory()
    now = utcnow()
    with state.db()() as session:
        session.add(Venture(id="ven_org", workspace_id=None, name="Org", raw_idea="Org test venture", stage="draft", autonomy_level="L1", created_at=now, updated_at=now))
        session.commit()
        session.add(AgentInstance(id="ag_live", venture_id="ven_org", spec_id="as_1", spec_version="1", status="active", created_at=now))
        session.commit()


def test_dead_worker_is_reported_blocked(org_env) -> None:
    from app import state
    from app.db.models import AgentLiveStatus
    from app.org.heartbeat import HEARTBEAT_TIMEOUT_SECONDS, list_live_status, mark_working
    from core.clock import utcnow

    mark_working("ag_live", "ven_org", "Writing the launch post")
    with state.db()() as session:
        row = session.get(AgentLiveStatus, "ag_live")
        row.last_heartbeat = utcnow() - timedelta(seconds=HEARTBEAT_TIMEOUT_SECONDS + 1)
        session.commit()
    status = list_live_status("ven_org")[0]
    assert status["state"] == "blocked"
    assert status["blocked_reason"] == "no heartbeat"

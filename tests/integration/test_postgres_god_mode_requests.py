"""God mode early-access requests on real PostgreSQL: migration chain, one active request per person per venture,
email capture, approval allowlist and the beta gate."""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from tests.integration.test_postgres_auth_security import pg_database as pg_database, service_modules


@pytest.fixture()
def world(pg_database, tmp_path, monkeypatch):
    url, engine = pg_database
    for key, value in {"APP_ENV": "local", "COMPANY_OS_DB_URL": url, "COMPANY_OS_DATA_DIR": str(tmp_path),
                       "LLM_OFFLINE": "1", "SUPYMEM_BETA_SCOPE": "1"}.items():
        monkeypatch.setenv(key, value)
    with service_modules("company_os"):
        from app.db.models import Person, Venture
        from app.state import set_factory
        from app.workspaces.service import create_workspace

        factory = sessionmaker(engine, expire_on_commit=False)
        set_factory(factory)
        owner = "usr_gm_" + uuid.uuid4().hex[:10]
        now = datetime.now(timezone.utc)
        with factory() as s:
            s.add(Person(id=owner, display_name="Founder", email="Founder@Example.test", created_at=now))
            s.commit()
        ws = create_workspace(kind="company", name="GM pg", owner_id=owner)
        vid = "ven_" + uuid.uuid4().hex[:12]
        with factory() as s:
            s.add(Venture(id=vid, workspace_id=ws.id, name="GM", raw_idea="x", stage="draft", autonomy_level="L1",
                          created_at=now, updated_at=now))
            s.commit()
        yield factory, owner, ws.id, vid


def test_concurrent_requests_make_one_row_with_email_and_approval_opens_gate(world):
    factory, owner, ws, vid = world
    from app.autopilot import god_requests
    from app.db.supymem_models import GodModeRequest

    results = []
    threads = [threading.Thread(target=lambda: results.append(god_requests.request_access(vid, ws, owner, "outreach")))
               for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert [r["created"] for r in results].count(True) == 1 and {r["status"] for r in results} == {"requested"}
    with factory() as s:
        rows = s.scalars(select(GodModeRequest).where(GodModeRequest.venture_id == vid)).all()
    assert len(rows) == 1 and rows[0].email == "Founder@Example.test"

    assert not god_requests.beta_gate_open(vid)
    assert god_requests.approve(rows[0].id, "adm_1")["status"] == "approved"
    assert god_requests.approve(rows[0].id, "adm_2")["decided_by"] == "adm_1"
    assert god_requests.beta_gate_open(vid)
    assert god_requests.request_access(vid, ws, owner)["status"] == "approved"  # still one active row
    listing = god_requests.admin_list()
    assert listing["counts"]["approved"] >= 1 and any(i["email"] == "Founder@Example.test" for i in listing["items"])


def test_migration_head_and_declined_can_be_re_requested(world):
    factory, owner, ws, vid = world
    from app.autopilot import god_requests
    from app.db.supymem_models import GodModeRequest

    first = god_requests.request_access(vid, ws, owner)
    assert first["created"]
    with factory() as s:
        row = s.scalars(select(GodModeRequest).where(GodModeRequest.venture_id == vid)).one()
        row.status = "declined"
        s.commit()
    assert god_requests.request_access(vid, ws, owner)["created"] is True

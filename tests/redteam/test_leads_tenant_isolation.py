"""Cross-tenant lead reads must 404; waitlist collector uses workspace_id."""

from __future__ import annotations

import sys
import types
from datetime import datetime, timezone

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def cos(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/company_os.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_EPHEMERAL_VAULT", "1")
    if "argon2" not in sys.modules:
        argon2 = types.ModuleType("argon2")
        exceptions = types.ModuleType("argon2.exceptions")

        class PasswordHasher:
            pass

        class VerifyMismatchError(Exception):
            pass

        argon2.PasswordHasher = PasswordHasher  # type: ignore[attr-defined]
        exceptions.VerifyMismatchError = VerifyMismatchError  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "argon2", argon2)
        monkeypatch.setitem(sys.modules, "argon2.exceptions", exceptions)
    load_company_os_app()
    from app import state
    from app.connectors.secrets import reset_store

    state.reset_db_factory()
    reset_store()
    return True


def test_foreign_subject_cannot_read_leads(cos):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.workspaces import service as workspace_service
    from app.workspaces.resolve import LOCAL_PERSON_ID
    from app.db.models import Venture
    from app.state import db
    from app.firstlook import interest as interest_mod
    from core.clock import utcnow
    from core.enums import VentureStage
    from core.ids import new_id

    ws = workspace_service.create_workspace(
        kind="venture", name="A", entry_mode="idea", owner_id=LOCAL_PERSON_ID
    )
    now = utcnow()
    ven_id = new_id("ven")
    with db()() as session:
        session.add(
            Venture(
                id=ven_id,
                workspace_id=ws.id,
                name="A",
                raw_idea="test idea for isolation",
                stage=VentureStage.DRAFT.value,
                autonomy_level="L1",
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()

    # Legacy workspace-level interest is deliberately not attributed to a venture;
    # leads are the venture's own consented managed-waitlist entries.
    interest_mod.record(workspace_id=ws.id, site_version=1, email="legacy@example.com", note="Lead")
    from hashlib import sha256

    from app.conversion import waitlist

    waitlist.configure(ven_id, ws.id, enabled=True, actor="founder")
    waitlist.capture(ven_id, {"email": "lead@sample-corporation.example", "consent": True,
                              "consent_version": waitlist.VERSION,
                              "consent_digest": sha256(waitlist.consent_text("A").encode()).hexdigest()})

    client = TestClient(create_app())
    ok = client.get(f"/ventures/{ven_id}/leads", headers={"X-Subject": "founder"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["total"] == 1
    assert [lead["email"] for lead in ok.json()["leads"]] == ["lead@sample-corporation.example"]

    denied = client.get(
        f"/ventures/{ven_id}/leads", headers={"X-Subject": "usr_attacker"}
    )
    assert denied.status_code == 404
    assert "lead@sample-corporation.example" not in denied.text


@pytest.mark.asyncio
async def test_interest_waitlist_collector_uses_workspace_not_venture_id(cos):
    from app.db.models import Venture
    from app.firstlook import interest as interest_mod
    from app.signals.collectors.interest_waitlist import InterestWaitlistCollector
    from app.state import db
    from app.workspaces import service as workspace_service
    from app.workspaces.resolve import LOCAL_PERSON_ID
    from core.clock import utcnow
    from core.enums import VentureStage
    from core.ids import new_id

    ws = workspace_service.create_workspace(
        kind="venture", name="B", entry_mode="idea", owner_id=LOCAL_PERSON_ID
    )
    now = utcnow()
    ven_id = new_id("ven")
    with db()() as session:
        session.add(
            Venture(
                id=ven_id,
                workspace_id=ws.id,
                name="B",
                raw_idea="collector fix idea",
                stage=VentureStage.DRAFT.value,
                autonomy_level="L1",
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()

    interest_mod.record(
        workspace_id=ws.id,
        site_version=1,
        email="w@example.com",
        note="W",
    )

    collector = InterestWaitlistCollector()
    since = datetime(2000, 1, 1, tzinfo=timezone.utc)
    # Old bug: filtered InterestSignal.workspace_id == venture_id (always empty).
    rows = await collector.collect(ven_id, since)
    assert len(rows) >= 1

"""OT-47: 90-day paste PII null with a frozen clock."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ot47.db")
    monkeypatch.setenv("SANDBOX_MODE", "true")
    load_company_os_app()
    from app import state as state_mod
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk

    state_mod.reset_db_factory()
    return ensure_desk(LOCAL_PERSON_ID).id


def test_nulls_paste_after_90_days(workspace, monkeypatch):
    from core.clock import set_clock, utcnow

    from app.db.models import GtmPasteBody
    from app.gtm.relationships.ingest import ingest_paste
    from app.gtm.relationships.retention import null_stale_paste_pii
    from app.state import db

    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr("app.gtm.relationships.ingest.utcnow", lambda: t0)
    ingest_paste(
        workspace,
        source="job_post_paste",
        raw_text='Company: Acme\n"we still run this workflow in a spreadsheet" secret@acme.test',
        company_name="Acme",
        enrich=False,
    )
    later = t0 + timedelta(days=91)
    set_clock(lambda: later)
    try:
        out = null_stale_paste_pii(workspace, now=utcnow(), retention_days=90)
    finally:
        set_clock(None)
    assert out["nulled"] == 1
    with db()() as session:
        row = session.scalar(select(GtmPasteBody).where(GtmPasteBody.workspace_id == workspace))
        assert row is not None
        assert row.raw_text is None
        assert row.raw_text_sha256
        assert row.nulled_at is not None

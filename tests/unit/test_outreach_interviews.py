"""OT-42: interview capture flags Mom-Test hypotheticals."""

from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ot42.db")
    monkeypatch.setenv("SANDBOX_MODE", "true")
    load_company_os_app()
    from app import state as state_mod
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk

    state_mod.reset_db_factory()
    return ensure_desk(LOCAL_PERSON_ID).id


def test_extracts_hypotheticals(workspace):
    from app.gtm.relationships.ingest import ingest_paste
    from app.gtm.relationships.interviews import capture_interview, extract_mom_test_flags

    flags = extract_mom_test_flags("Would you ever buy this if we built it?")
    assert "would you" in flags["hypotheticals_flagged"]
    contact = ingest_paste(
        workspace,
        source="profile_paste",
        raw_text='Sam\nsam@sample-tools.test\nFounder\n"we still run this workflow in a spreadsheet"',
        enrich=False,
    )
    out = capture_interview(
        workspace,
        contact_id=contact["contact_id"],
        notes_md='Last time this happened was Tuesday. Would you use a dashboard?',
    )
    assert "would you" in out["hypotheticals_flagged"]
    assert out["mom_test_ok"] is False

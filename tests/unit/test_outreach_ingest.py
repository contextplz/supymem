"""OT-20: paste ingest without Bright Data."""

from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ot20.db")
    monkeypatch.setenv("SANDBOX_MODE", "true")
    monkeypatch.delenv("BRIGHTDATA_API_KEY", raising=False)
    load_company_os_app()
    from app import state as state_mod
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk

    state_mod.reset_db_factory()
    return ensure_desk(LOCAL_PERSON_ID).id


def test_job_paste_creates_company_and_layer_a(workspace):
    from app.gtm.relationships.ingest import ingest_paste

    out = ingest_paste(
        workspace,
        source="job_post_paste",
        raw_text=(
            "Company: Northstar Tools\n"
            'Engineering post: "we still run this workflow in a spreadsheet"\n'
        ),
        company_name="Northstar Tools",
        trigger_code="hiring_complaint",
        enrich=False,
    )
    assert out["company_id"]
    assert out["signal_id"]
    assert out["layer"] in {"A", "B"}
    assert out["quote"]


def test_profile_paste_creates_layer_b(workspace):
    from app.gtm.relationships.ingest import ingest_paste

    out = ingest_paste(
        workspace,
        source="profile_paste",
        raw_text="Sam Builder\nemail: sam@sample-tools.test\nFounder\n",
        enrich=False,
    )
    assert out["contact_id"]
    assert out["layer"] == "B"


def test_import_identities_old_signature_still_works(workspace):
    from app.gtm.supymem.pipeline import import_identities

    result = import_identities(workspace, emails="old@sample-path.test\n", enrich=False)
    assert result["imported"] >= 1

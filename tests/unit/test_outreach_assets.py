"""OT-41: monthly SKU factory stops at in_review; attach needs approved + reviewed."""

from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ot41.db")
    monkeypatch.setenv("SANDBOX_MODE", "true")
    load_company_os_app()
    from app import state as state_mod
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk

    state_mod.reset_db_factory()
    return ensure_desk(LOCAL_PERSON_ID).id


def test_cannot_attach_in_review_sku(workspace):
    from app.gtm.relationships.assets import mint_monthly_sku
    from app.gtm.relationships.ingest import ingest_paste
    from app.gtm.relationships.skus import SkuAttachError, attach_sku

    minted = mint_monthly_sku(
        workspace,
        period="2026-09",
        title="September benchmark",
        body_md="One-pager from public numbers only.",
    )
    assert minted["status"] == "in_review"
    contact = ingest_paste(
        workspace,
        source="profile_paste",
        raw_text='Sam\nsam@sample-tools.test\nFounder\n"we still run this workflow in a spreadsheet"',
        enrich=False,
    )
    with pytest.raises(SkuAttachError) as exc:
        attach_sku(workspace, contact["contact_id"], minted["sku_key"], reviewed=True)
    assert exc.value.code == "sku_not_approved"


def test_factory_honesty_blocks_fake_interview_counts(workspace):
    from app.gtm.relationships.assets import AssetFactoryError, mint_monthly_sku

    with pytest.raises(AssetFactoryError) as exc:
        mint_monthly_sku(
            workspace,
            period="2026-09",
            title="Lies",
            body_md="We interviewed 10 companies last month.",
        )
    assert exc.value.code == "catalogue_honesty_failed"


def test_approve_then_attach(workspace):
    from app.gtm.relationships.assets import approve_sku, mint_monthly_sku
    from app.gtm.relationships.ingest import ingest_paste
    from app.gtm.relationships.skus import attach_sku

    minted = mint_monthly_sku(
        workspace,
        period="2026-09",
        title="September benchmark",
        body_md="Public teardown of a workflow.",
        sku_key="2026-09-bench-01",
    )
    with pytest.raises(Exception):
        approve_sku(workspace, minted["sku_key"], reviewed=False)
    approve_sku(workspace, minted["sku_key"], reviewed=True)
    contact = ingest_paste(
        workspace,
        source="profile_paste",
        raw_text='Sam\nsam@sample-tools.test\nFounder\n"we still run this workflow in a spreadsheet"',
        enrich=False,
    )
    out = attach_sku(workspace, contact["contact_id"], minted["sku_key"], reviewed=True)
    assert out["ok"] is True

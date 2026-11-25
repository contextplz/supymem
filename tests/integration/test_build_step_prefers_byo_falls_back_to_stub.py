from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from coding_agents.consent import clear_consents, record_consent


@pytest.mark.asyncio
async def test_byo_falls_back_to_stub_files(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CODING_AGENT_CONSENT_DB", str(tmp_path / "consent.sqlite3"))
    monkeypatch.setenv("BUILD_PROVIDER", "byo")
    monkeypatch.setenv("CODING_AGENT_LIVE", "0")
    clear_consents()
    record_consent("inhouse")

    from app.onboarding.steps import build as build_mod

    venture = SimpleNamespace(id="ven_test", raw_idea="A test idea about widgets.")
    ctx = SimpleNamespace(
        venture=venture,
        company="WidgetCo",
        out={},
        narrator=None,
        set_flag=lambda *a, **k: None,
    )
    # Ensure repo path under COMPANY_OS_DATA_DIR
    result = await build_mod.landing_page(ctx)
    assert result.output is not None
    assert result.output.get("provider") == "byo"
    files = result.output.get("files") or []
    assert "index.html" in files
    repo = tmp_path / "ventures" / "ven_test" / "repo"
    assert (repo / "index.html").is_file()

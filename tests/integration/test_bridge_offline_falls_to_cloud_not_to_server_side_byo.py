from __future__ import annotations

import pytest

from coding_agents.consent import clear_consents, record_consent
from coding_agents.dispatcher import dispatch_request
from coding_agents.protocol import CodingRequest


@pytest.mark.asyncio
async def test_bridge_offline_uses_cloud_dry_run_not_local_byo(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_ENV", "local")
    monkeypatch.setenv("CODING_AGENT_CONSENT_DB", str(tmp_path / "c.sqlite3"))
    monkeypatch.setenv("CURSOR_API_KEY", "cursor_test_key")
    monkeypatch.setenv("CODING_AGENT_LIVE", "0")
    # Force cloud adapter dry path via missing SDK → binary_missing → fall to inhouse
    clear_consents()
    record_consent("cursor_cloud")
    record_consent("inhouse")

    req = CodingRequest(prompt="x", workdir=tmp_path, allow_write=True)
    result = await dispatch_request(
        req,
        prefs=["cursor", "inhouse"],
        mode="auto",
        bridge_connected=False,
        allow_server_side_byo=False,
    )
    # Must not run local cursor-agent argv when bridge is offline.
    assert "cursor-agent" not in (result.argv or [])
    assert result.ok or result.error in {"binary_missing", "auth_failure", "consent_required"} or (
        result.argv and result.argv[0] in {"inhouse-coder", "cursor-sdk-cloud"}
    )

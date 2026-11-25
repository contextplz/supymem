"""Guard: process-local MEM dict must not return (doc 72 A72-19)."""

from __future__ import annotations

from collections.abc import MutableMapping
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.company_os_harness import company_os_client, load_company_os_app


@pytest.mark.parametrize("idea", [
    "Inventory reconciliation for warehouse supervisors",
    "A walking journal for families exploring local trails",
], ids=["b2b", "consumer"])
@pytest.mark.parametrize("lowercase_headers", [False, True])
def test_local_unit_defaults_isolate_data_and_preserve_remote_denial(tmp_path, monkeypatch, idea, lowercase_headers):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    assert Path(os.environ.get("COMPANY_OS_DATA_DIR", ".")).resolve().is_relative_to(tmp_path)
    main = load_company_os_app()
    from app.workspaces.resolve import LOCAL_PERSON_ID

    with company_os_client(main.create_app()) as client:
        created = client.post("/ventures", json={"raw_idea": idea, "start": False})
        assert created.status_code == 201, created.text
        assert client.get("/auth/session").json()["subject"] == LOCAL_PERSON_ID
        credential_header = "x-local-proxy-secret" if lowercase_headers else "X-Local-Proxy-Secret"
        origin_header = "origin" if lowercase_headers else "Origin"
        with company_os_client(client.app, headers={credential_header: "wrong-owner"}) as wrong:
            denied = wrong.get("/auth/session")
            assert denied.status_code == 403
            assert denied.json()["detail"]["code"] == "local_owner_required"
        with company_os_client(client.app, headers={origin_header: "https://foreign.example"}) as foreign:
            denied = foreign.get("/auth/session")
            assert denied.status_code == 403
            assert denied.json()["detail"]["code"] == "local_origin_rejected"
        with TestClient(client.app, base_url="http://localhost", client=("127.0.0.1", 50000)) as missing:
            denied = missing.get("/auth/session")
            assert denied.status_code == 403
            assert denied.json()["detail"]["code"] == "local_owner_required"
        with TestClient(client.app, client=("192.0.2.1", 50000)) as remote:
            denied = remote.get("/health")
            assert denied.status_code == 403
            assert denied.json()["detail"]["code"] == "local_only"
        monkeypatch.setenv("SUPYMEM_MODE", "hosted")
        with company_os_client(client.app) as hosted:
            assert hosted.base_url.host == "testserver"


def test_mem_is_working_memory_not_plain_dict(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/cos.db")
    monkeypatch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(tmp_path / "wm.sqlite3"))
    load_company_os_app()
    from app import state as state_mod

    # Survives as MutableMapping but is not a bare dict.
    assert isinstance(state_mod.MEM, MutableMapping)
    assert type(state_mod.MEM).__name__ == "WorkingMemory"
    assert type(state_mod.MEM) is not dict


@pytest.mark.parametrize("domain", ["warehouse.example", "gardening.example"])
def test_local_model_fixture_has_saved_readiness_and_catalogued_pricing(tmp_path, monkeypatch, domain):
    from tests.company_os_harness import configure_local_test_model

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / domain))
    load_company_os_app()
    configure_local_test_model()
    from app.core.local_capabilities import capability
    from app.core.providers_local import load_local_config
    from app.llm.pricing import pricing_table

    assert capability("content")["state"] == "ready"
    assert load_local_config().deployment_flash in pricing_table()

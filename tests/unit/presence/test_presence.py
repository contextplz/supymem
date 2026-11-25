"""Workspace presence heartbeats."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.company_os_harness import load_company_os_app


import pytest

pytestmark = pytest.mark.hosted_only


def test_presence_beat_and_list(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/pre.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    main = load_company_os_app()
    from app import state

    state.reset_db_factory()
    with TestClient(main.create_app()) as client:
        ws = client.post("/workspaces", json={"kind": "venture", "name": "Crew"}).json()["id"]
        beat = client.post(f"/workspaces/{ws}/presence", json={"surface": "people"})
        assert beat.status_code == 200, beat.text
        items = beat.json()["items"]
        assert items
        listed = client.get(f"/workspaces/{ws}/presence")
        assert listed.status_code == 200
        assert listed.json()["items"]

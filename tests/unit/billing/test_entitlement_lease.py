"""Signed entitlement lease verifies locally and rejects tampering."""

from __future__ import annotations


def test_sign_and_verify_lease(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/lease.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    from app import state

    state.reset_db_factory()
    from app.billing.entitlements import current_plan
    from app.billing.lease import sign_lease, verify_lease

    plan = current_plan(workspace_id="wsp_lease", person_id="per_local000000")
    lease = sign_lease(plan, subject="founder")
    assert verify_lease(lease) is True
    lease["plan"] = "team"
    assert verify_lease(lease) is False


def test_entitlement_endpoint_returns_lease(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ent.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    from tests.company_os_harness import company_os_test_client as TestClient

    from tests.company_os_harness import load_company_os_app

    main = load_company_os_app()
    from app import state

    state.reset_db_factory()
    with TestClient(main.create_app()) as client:
        ws = client.post("/workspaces", json={"kind": "venture", "name": "Paid"}).json()
        r = client.get(f"/entitlement?workspace_id={ws['id']}")
        assert r.status_code == 200, r.text
        body = r.json()
        from app.core.local_mode import is_local

        if is_local():
            assert body["mode"] == "local" and body["status"] == "active"
            assert body["payments_accepting"] is False
            assert "harness_research" in body["features"]
            assert not {"plan", "lease", "public_key"} & body.keys()
            return
        assert body["plan"] in {"free", "founder", "pro", "team"}
        assert "harness_research" in body["features"] or body["plan"] == "free"
        assert body["lease"]["signature"]

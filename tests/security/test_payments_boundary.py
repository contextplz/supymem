"""Payments tenant boundary, no micro-credits on the wire, unsigned webhook 401."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.company_os_harness import load_company_os_app


def _walk_keys(value: object) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, inner in value.items():
            found.append(str(key))
            found.extend(_walk_keys(inner))
    elif isinstance(value, list):
        for inner in value:
            found.extend(_walk_keys(inner))
    return found


def _assert_no_micro(payload: object) -> None:
    offenders = [key for key in _walk_keys(payload) if key.endswith("_micro")]
    assert not offenders, offenders


def test_foreign_venture_orders_are_404(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/pay.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PAYMENTS_ACCEPTING", "1")
    main = load_company_os_app()
    with TestClient(main.create_app()) as client:
        alice = client.post(
            "/ventures",
            json={
                "raw_idea": "a private architecture studio for independent planners",
                "start": False,
            },
            headers={"X-Subject": "usr_alice"},
        )
        assert alice.status_code == 201, alice.text
        vid = alice.json()["id"]
        own = client.get(
            f"/ventures/{vid}/credits/orders",
            headers={"X-Subject": "usr_alice"},
        )
        assert own.status_code == 200, own.text
        _assert_no_micro(own.json())
        denied = client.get(
            f"/ventures/{vid}/credits/orders",
            headers={"X-Subject": "usr_bob"},
        )
        assert denied.status_code == 404
        packs = client.get(
            f"/ventures/{vid}/credits/packs",
            headers={"X-Subject": "usr_alice"},
        )
        assert packs.status_code == 200
        _assert_no_micro(packs.json())
        from app.credits.policy import set_policy
        set_policy("topup_provider_by_currency", {"*": "stub"}, updated_by="test")
        topup = client.post(
            f"/ventures/{vid}/credits/topup",
            json={"sku": "cr_500", "currency": "USD"},
            headers={"X-Subject": "usr_alice"},
        )
        assert topup.status_code == 200, topup.text
        _assert_no_micro(topup.json())


def test_unsigned_webhook_is_401_without_session(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/pay.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    main = load_company_os_app()
    with TestClient(main.create_app()) as client:
        response = client.post(
            "/webhooks/payments/stub",
            content=b'{"id":"evt_unsigned","type":"payment.succeeded"}',
        )
        assert response.status_code == 401
        assert response.json() == {}
        _assert_no_micro(response.json())


def test_complete_stub_is_refused_while_payments_are_closed(tmp_path, monkeypatch) -> None:
    import types

    import pytest
    from fastapi import HTTPException

    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/stub.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("PAYMENTS_ACCEPTING", raising=False)
    load_company_os_app()
    from app.api.consumer_api import StubCompleteBody, account_complete_stub

    with pytest.raises(HTTPException) as exc:
        account_complete_stub(StubCompleteBody(provider_order_id="po_x"), types.SimpleNamespace(user_id="u"))
    assert exc.value.status_code == 403 and exc.value.detail["error"]["code"] == "payments_closed"

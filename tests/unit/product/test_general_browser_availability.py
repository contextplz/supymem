"""General browser beta admission refuses before cards, holds or execution."""
import pytest

from tests.unit.product.test_device_orders import orders as orders  # noqa: PLC0414


def general(svc, manifest):
    return svc.Manifest.model_validate({**manifest.model_dump(),
        "applications": ["com.google.Chrome"], "actions": ["browser.read", "browser.interact"],
        "general_browser_verification": {"operation": "general_goal_v1", "start_url": "https://example.test/"}})


@pytest.mark.parametrize("value", [None, "", "0", "true", "TRUE"])
def test_default_off_refuses_before_card_and_hold(orders, monkeypatch, value):
    svc, caller, manifest = orders
    if value is None:
        monkeypatch.delenv(svc.GENERAL_BROWSER_EXECUTION_ENV, raising=False)
    else:
        monkeypatch.setenv(svc.GENERAL_BROWSER_EXECUTION_ENV, value)
    from app.db.models import CreditAccount, VentureDocument
    with svc.db()() as session:
        before = session.query(VentureDocument).count()
        account = session.query(CreditAccount).filter_by(id="cra_device_test").one()
        balance = (account.reserved_micro, account.spent_micro)
    for run in (lambda: svc.prepare("v_one", caller, general(svc, manifest)),
                lambda: svc.prepare_general_browser_from_chat("v_one", caller, "Go to example.test and prepare a signup form")):
        with pytest.raises(ValueError, match="^device_general_browser_execution_unavailable$"):
            run()
    with svc.db()() as session:
        assert session.query(VentureDocument).count() == before
        account = session.query(CreditAccount).filter_by(id="cra_device_test").one()
        assert (account.reserved_micro, account.spent_micro) == balance


def test_exact_experimental_opt_in_and_typed_default_are_separate(orders, monkeypatch):
    svc, caller, manifest = orders
    monkeypatch.delenv(svc.GENERAL_BROWSER_EXECUTION_ENV, raising=False)
    order = svc.prepare("v_one", caller, manifest)
    assert order["state"] == "pending"
    svc.transition("v_one", caller, order["id"], order["review_digest"], "cancel")
    monkeypatch.setenv(svc.GENERAL_BROWSER_EXECUTION_ENV, "1")
    assert svc.prepare("v_one", caller, general(svc, manifest))["state"] == "pending"
    for profile in ({"browser_verification": {}}, {"coding_verification": {}}, {"terminal_verification": {}}, {}):
        assert not svc.general_browser_manifest(profile)
    assert svc.general_browser_manifest({"general_browser_verification": {}})


def test_signed_http_default_off_and_saved_capabilities_are_honest(orders, monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.device_orders_api import router
    from tests.unit.product.test_device_order_api import SECRET, headers
    svc, caller, manifest = orders
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", SECRET)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    monkeypatch.delenv(svc.GENERAL_BROWSER_EXECUTION_ENV, raising=False)
    base = "/ventures/v_one/device-work-orders"
    for path, payload in ((base, general(svc, manifest).model_dump()), (base + "/from-chat", {
            "instruction": "Go to example.test and prepare the signup form without submitting"})):
        response = client.post(path, json=payload, headers=headers(path))
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "device_general_browser_execution_unavailable"
        assert "unavailable in this beta" in response.json()["detail"]["message"]
    monkeypatch.setenv(svc.GENERAL_BROWSER_EXECUTION_ENV, "1")
    order = svc.prepare("v_one", caller, general(svc, manifest))
    monkeypatch.delenv(svc.GENERAL_BROWSER_EXECUTION_ENV)
    path = base + "/" + order["id"] + "/capabilities"
    response = client.get(path, headers=headers(path, "GET"))
    assert response.status_code == 200
    assert response.json()["available"] is False
    assert response.json()["general_browser_execution_v1"] is False
    assert response.json()["code"] == svc.GENERAL_BROWSER_EXECUTION_UNAVAILABLE

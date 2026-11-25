"""Per-founder enablement of general browser goals: default off, allowlist opens named founders only."""
import pytest

from tests.company_os_harness import load_company_os_app

GENERAL = {"general_browser_verification": {}}


@pytest.fixture
def orders(monkeypatch):
    load_company_os_app()
    from app.build import device_orders
    monkeypatch.delenv(device_orders.GENERAL_BROWSER_EXECUTION_ENV, raising=False)
    monkeypatch.delenv(device_orders.GENERAL_BROWSER_ALLOWLIST_ENV, raising=False)
    return device_orders


def test_default_off_for_everyone(orders):
    assert not orders.general_browser_execution_enabled("usr_a")
    with pytest.raises(ValueError, match=orders.GENERAL_BROWSER_EXECUTION_UNAVAILABLE):
        orders.assert_general_browser_available(GENERAL, "usr_a")


def test_allowlist_opens_only_listed_founders(orders, monkeypatch):
    monkeypatch.setenv(orders.GENERAL_BROWSER_ALLOWLIST_ENV, " usr_a, ,usr_b ")
    assert orders.general_browser_execution_enabled("usr_a") and orders.general_browser_execution_enabled("usr_b")
    for other in ("usr_c", "", None, "usr_a,usr_b"):
        assert not orders.general_browser_execution_enabled(other)
    orders.assert_general_browser_available(GENERAL, "usr_a")
    with pytest.raises(ValueError):
        orders.assert_general_browser_available(GENERAL, "usr_c")
    with pytest.raises(ValueError):
        orders.assert_general_browser_available(GENERAL)   # no actor: never allowlisted


def test_global_flag_keeps_its_meaning(orders, monkeypatch):
    monkeypatch.setenv(orders.GENERAL_BROWSER_EXECUTION_ENV, "1")
    assert orders.general_browser_execution_enabled(None) and orders.general_browser_execution_enabled("usr_z")

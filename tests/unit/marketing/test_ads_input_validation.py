"""Blueprint admission rejects invalid inputs before storage or provider access."""
import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture(scope="module")
def service():
    load_company_os_app()
    from app.marketing.ads import service
    return service


def payload():
    return {"platform": "google", "idea": "Scheduling software for independent fitness studios",
            "landing_url": "https://studio.invalid/demo", "audience": "Studio owners", "daily_budget_usd": 10}


@pytest.fixture
def no_side_effects(service, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid proposal reached persistence, audit or provider")
    monkeypatch.setattr(service, "db", forbidden)
    monkeypatch.setattr(service, "audit", forbidden)
    monkeypatch.setattr(service.probes, "create_paused_ad", forbidden)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), "nan", "NaN", " NAN ",
    "+nan", "-NaN", "inf", "Infinity", "+INFINITY", "-inf", "1e999", "-1e999", 10 ** 1000])
def test_nonfinite_budget_is_rejected_before_side_effects(service, no_side_effects, value):
    with pytest.raises(service.AdsError) as raised:
        service.create_proposal("private-workspace", payload() | {"daily_budget_usd": value})
    assert raised.value.code == "budget_invalid" and raised.value.status == 422


@pytest.mark.parametrize("value,expected", [(1, 1.0), ("10.125", 10.12), (25, 25.0), (" 0.01 ", .01)])
def test_finite_budgets_keep_existing_rounding_and_limits(service, value, expected):
    assert service._budget(value) == expected


@pytest.mark.parametrize("value", [0, -.01, 25.01, "100"])
def test_finite_outside_guardrail_remains_rejected(service, value):
    with pytest.raises(service.AdsError) as raised:
        service._budget(value)
    assert raised.value.code == "budget_outside_guardrail"


@pytest.mark.parametrize("url", ["https://name:password@studio.invalid/demo", "https://name@studio.invalid",
    "https://@studio.invalid", "https://:password@studio.invalid", "https://name%40mail:password@studio.invalid",
    "\nhttps://studio.invalid", "https://studio.invalid\t/demo", "https://studio.invalid/demo\r\n",
    "https://studio.invalid/\x00demo", "https://studio.invalid/\x7fdemo", "https://studio.invalid/\x85demo",
    "https://[invalid"])
def test_credential_or_control_landing_urls_fail_before_side_effects(service, no_side_effects, url):
    with pytest.raises(service.AdsError) as raised:
        service.create_proposal("private-workspace", payload() | {"landing_url": url})
    assert raised.value.code == "landing_url_invalid" and raised.value.status == 422
    assert "password" not in str(raised.value)


@pytest.mark.parametrize("url", ["http://studio.invalid/demo", "https://studio.invalid/demo?campaign=pilot#offer"])
def test_private_blueprint_url_validation_does_not_require_network_or_provider(service, no_side_effects, url):
    blueprint = service._blueprint(payload() | {"landing_url": "  " + url + "  "})
    assert blueprint["landing_url"] == url
    assert blueprint["status_on_create"] == "PAUSED"

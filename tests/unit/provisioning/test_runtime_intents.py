import pytest

from tests.integration.test_postgres_auth_security import service_modules


def test_product_hostname_is_stable_bounded_and_separate_from_landing():
    with service_modules("company_os"):
        from app.provisioning.runtime_intents import RuntimeIntentError, product_hostname

        hostname = product_hostname("app_abcdefgh", "labhire")
        assert hostname.startswith("labhire-app-") and hostname.endswith(".supymem.com")
        assert hostname != "labhire.supymem.com"
        assert len(hostname.removesuffix(".supymem.com")) <= 48
        assert product_hostname("app_abcdefgh", "labhire") == hostname
        with pytest.raises(RuntimeIntentError):
            product_hostname("other", "labhire")

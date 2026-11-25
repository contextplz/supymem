"""Fail-closed APP_ENV contract: one shot, no secret values, local warns."""

from __future__ import annotations

import logging

import pytest

from core.env_contract import (
    CLASSIFICATION,
    EnvContractError,
    Problem,
    Requirement,
    coerce_bool,
    collect_violations,
    ensure_boot_env,
    format_violations,
    parse_app_env,
    reset_env_contract_state,
)

# Distinctive values used only in these tests so a leak is obvious.
_PROD_PASSPHRASE = "unit-test-passphrase-not-changeme"
_PROD_SESSION = "unit-test-session-secret-32bytes!!"
_PROD_ASSERTION = "unit-test-assertion-secret-32byte"
_PROD_VAULT = "unit-test-vault-secret-at-least-32b"

PRODUCTION_ENV = {
    "APP_ENV": "production",
    "APP_ROLE": "api",
    "CONSOLE_PASSPHRASE": _PROD_PASSPHRASE,
    "SESSION_SECRET": _PROD_SESSION,
    "COS_INTERNAL_ASSERTION_SECRET": _PROD_ASSERTION,
    "COMPANY_OS_REQUIRE_SIGNED_CALLER": "1",
    "COMPANY_OS_VAULT_SECRET": _PROD_VAULT,
    "ADMIN_REQUIRE_TOTP": "1",
    "ADMIN_BOOTSTRAP_EMAILS": "ops@example.com",
    "COMPANY_OS_DB_URL": "postgresql+psycopg://cos:cos@db:5432/companyos",
    "WARDEN_DB_URL": "postgresql+psycopg://cos:cos@db:5432/warden",
    "GATEWAY_USERS_DB": "postgresql://cos:cos@db:5432/gateway",
    "LLM_CREDITS_REQUIRE_GATE": "1",
}

_SECRETS = (_PROD_PASSPHRASE, _PROD_SESSION, _PROD_ASSERTION, _PROD_VAULT, "changeme")


def _assert_no_secret(text: str) -> None:
    for secret in _SECRETS:
        assert secret not in text, f"secret leaked into: {text}"


def test_classification_covers_fail_open_defaults() -> None:
    outside = Requirement.OUTSIDE_LOCAL
    assert CLASSIFICATION["CONSOLE_PASSPHRASE"] is outside
    assert CLASSIFICATION["SESSION_SECRET"] is outside
    assert CLASSIFICATION["COMPANY_OS_REQUIRE_SIGNED_CALLER"] is outside
    assert CLASSIFICATION["COS_INTERNAL_ASSERTION_SECRET"] is outside
    assert CLASSIFICATION["COMPANY_OS_DB_URL"] is outside
    assert CLASSIFICATION["APP_ENV"] is Requirement.ALL
    assert CLASSIFICATION["COMPANY_OS_ENV"] is Requirement.OPTIONAL


def test_boot_succeeds_with_production_like_env() -> None:
    reset_env_contract_state()
    assert parse_app_env(PRODUCTION_ENV) == "production"
    assert ensure_boot_env(PRODUCTION_ENV) == []
    assert collect_violations(PRODUCTION_ENV) == []


@pytest.mark.parametrize("service,required", [
    ("core", {"COS_INTERNAL_ASSERTION_SECRET", "COMPANY_OS_REQUIRE_SIGNED_CALLER", "COMPANY_OS_VAULT_SECRET",
              "ADMIN_REQUIRE_TOTP", "ADMIN_BOOTSTRAP_EMAILS", "COMPANY_OS_DB_URL", "LLM_CREDITS_REQUIRE_GATE"}),
    ("gateway", {"CONSOLE_PASSPHRASE", "SESSION_SECRET", "COS_INTERNAL_ASSERTION_SECRET",
                 "COMPANY_OS_REQUIRE_SIGNED_CALLER", "ADMIN_REQUIRE_TOTP", "ADMIN_BOOTSTRAP_EMAILS", "GATEWAY_USERS_DB"}),
    ("warden", {"WARDEN_DB_URL"}),
])
def test_services_need_only_their_own_credentials(service, required):
    env = {key: value for key, value in PRODUCTION_ENV.items() if key in required | {"APP_ENV", "APP_ROLE"}}
    if service == "gateway":
        # Actual key parsing is additionally enforced by gateway create_app.
        env["JWT_SIGNING_KEY_ES256"] = "test-key-presence-only"
    assert ensure_boot_env(env, service=service) == []
    for key in required - {"ADMIN_REQUIRE_TOTP"}:
        invalid = {name: value for name, value in env.items() if name != key}
        # Vault intentionally supports SESSION_SECRET fallback; gateway receives no extra vault key.
        with pytest.raises(EnvContractError):
            ensure_boot_env(invalid, service=service)


def test_native_gateway_cannot_boot_without_signing_key():
    with pytest.raises(EnvContractError) as error:
        ensure_boot_env(PRODUCTION_ENV, service="gateway")
    assert {item.name for item in error.value.violations} == {"JWT_SIGNING_KEY_ES256"}


def test_unknown_service_cannot_weaken_environment_validation():
    with pytest.raises(ValueError, match="Unknown service"):
        ensure_boot_env(PRODUCTION_ENV, service="typo")


def test_staging_changeme_fails_with_multi_variable_message() -> None:
    reset_env_contract_state()
    env = {"APP_ENV": "staging", "CONSOLE_PASSPHRASE": "changeme"}
    with pytest.raises(EnvContractError) as excinfo:
        ensure_boot_env(env)
    err = excinfo.value
    msg = str(err)
    _assert_no_secret(msg)
    assert err.app_env == "staging"
    names = {v.name for v in err.violations}
    assert "CONSOLE_PASSPHRASE" in names
    assert "SESSION_SECRET" in names
    assert "COS_INTERNAL_ASSERTION_SECRET" in names
    assert "COMPANY_OS_REQUIRE_SIGNED_CALLER" in names
    assert "COMPANY_OS_DB_URL" in names
    passphrase = next(v for v in err.violations if v.name == "CONSOLE_PASSPHRASE")
    assert passphrase.problem is Problem.STILL_AT_DEFAULT
    assert msg.startswith("Refusing to boot with APP_ENV=staging")
    assert msg.count("\n  - ") == len(err.violations) >= 5


def test_local_warns_rather_than_fails(caplog: pytest.LogCaptureFixture) -> None:
    reset_env_contract_state()
    env = {
        "APP_ENV": "local",
        "CONSOLE_PASSPHRASE": "changeme",
        "SESSION_SECRET": _PROD_SESSION,
    }
    with caplog.at_level(logging.WARNING, logger="core.env_contract"):
        violations = ensure_boot_env(env)
    assert violations
    names = {v.name for v in violations}
    assert "CONSOLE_PASSPHRASE" in names
    assert "would refuse to boot in staging/production" in caplog.text
    _assert_no_secret(caplog.text)
    _assert_no_secret(format_violations("local", violations, fatal=False))


def test_local_owner_boot_omits_hosted_requirements_without_weakening_hosted_validation(caplog):
    for domain in ("supplier-audit", "cooking-classes"):
        reset_env_contract_state()
        caplog.clear()
        env = {"APP_ENV": "local", "SUPYMEM_MODE": "local", "COMPANY_OS_DB_URL": f"sqlite:///{domain}.db"}
        with caplog.at_level(logging.WARNING, logger="core.env_contract"):
            assert ensure_boot_env(env, service="core") == []
        assert not caplog.text
        with pytest.raises(EnvContractError):
            ensure_boot_env({**env, "APP_ENV": "production"}, service="core")
        with pytest.raises(EnvContractError):
            ensure_boot_env({**env, "APP_ROLE": "invalid"}, service="core")


def test_invalid_app_env_always_fatal() -> None:
    reset_env_contract_state()
    with pytest.raises(EnvContractError) as excinfo:
        ensure_boot_env({**PRODUCTION_ENV, "APP_ENV": "prod"})
    assert excinfo.value.app_env == "invalid"
    assert any(v.name == "APP_ENV" for v in excinfo.value.violations)
    _assert_no_secret(str(excinfo.value))


def test_session_secret_too_short_does_not_echo_value() -> None:
    tiny = "n0t32"
    env = {
        **PRODUCTION_ENV,
        "SESSION_SECRET": tiny,
        "COMPANY_OS_VAULT_SECRET": _PROD_VAULT,
    }
    violations = collect_violations(env)
    short = [v for v in violations if v.name == "SESSION_SECRET"]
    assert short and short[0].problem is Problem.TOO_SHORT
    msg = format_violations("staging", violations, fatal=True)
    assert tiny not in msg
    _assert_no_secret(msg)


def test_core_create_app_refuses_missing_staging_identity_and_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wiring: create_app must run the contract before serving."""
    from tests.company_os_harness import load_company_os_app

    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("CONSOLE_PASSPHRASE", "changeme")
    for key in (
        "SESSION_SECRET",
        "COS_INTERNAL_ASSERTION_SECRET",
        "COMPANY_OS_REQUIRE_SIGNED_CALLER",
        "COMPANY_OS_VAULT_SECRET",
        "ADMIN_BOOTSTRAP_EMAILS",
        "COMPANY_OS_DB_URL",
        "DATABASE_URL",
        "DB_URL",
        "WARDEN_DB_URL",
        "GATEWAY_USERS_DB",
        "LLM_CREDITS_REQUIRE_GATE",
        "ADMIN_REQUIRE_TOTP",
    ):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(EnvContractError) as excinfo:
        main = load_company_os_app()
        main.create_app()  # Reused test modules still need to exercise service boot.
    msg = str(excinfo.value)
    assert "COS_INTERNAL_ASSERTION_SECRET" in msg
    assert "COMPANY_OS_DB_URL" in msg
    assert "CONSOLE_PASSPHRASE" not in msg
    assert "GATEWAY_USERS_DB" not in msg
    _assert_no_secret(msg)


def test_coerce_bool_empty_uses_default() -> None:
    assert coerce_bool("") is False
    assert coerce_bool("  ") is False
    assert coerce_bool(None) is False
    assert coerce_bool("", default=True) is True
    assert coerce_bool("1") is True
    assert coerce_bool("0") is False
    assert coerce_bool("true") is True
    assert coerce_bool("false") is False
    assert coerce_bool("on") is True
    assert coerce_bool("nope") is False
    assert coerce_bool(True) is True
    assert coerce_bool(False) is False


def test_empty_admin_require_totp_does_not_crash_settings(
    tmp_path, monkeypatch
) -> None:
    """An older local .env can contain a blank ADMIN_REQUIRE_TOTP setting."""
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("ADMIN_REQUIRE_TOTP", "")
    monkeypatch.setenv("COMPANY_OS_REQUIRE_SIGNED_CALLER", "")
    monkeypatch.setenv("LLM_OFFLINE", "")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_CREDITS_REQUIRE_GATE", "0")
    from tests.company_os_harness import load_company_os_app

    main = load_company_os_app()
    from app.config import Settings

    s = Settings()
    assert s.admin_require_totp is False
    assert s.company_os_require_signed_caller is False
    assert s.llm_offline is True
    assert main is not None

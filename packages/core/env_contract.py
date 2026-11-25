"""Fail-closed environment contract for Company OS services.

``APP_ENV`` is the deployment environment: ``local`` | ``staging`` | ``production``.
It is *not* ``COMPANY_OS_ENV``. That older variable is ``local`` | ``cloud`` and
selects the coding-agent adapter set (see ``packages/coding_agents/__init__.py``).
Cookie ``Secure`` flags follow ``APP_ENV`` (see ``cookies_secure``). Mixing the
two would either break BYO-agent exclusion in cloud builds or silently treat
``staging`` as ``cloud``. Keep them separate; never alias one to the other.

Classification of each variable (see ``CLASSIFICATION``):

- ``required_all`` — must be valid in every environment (closed enum, defaulted).
- ``required_outside_local`` — unset, weak, or still at a development default
  refuses boot when ``APP_ENV`` is ``staging`` or ``production``.
- ``optional_default`` — documented, has a safe default, never a boot failure.

Messages name the variable and the *nature* of the problem (missing / too short
/ still at default / not enabled / sqlite / invalid). They never include the
value, so a secret cannot leak into logs or the exception text.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from core.assertion import MIN_SECRET_BYTES

log = logging.getLogger("core.env_contract")

APP_ENV_VALUES = frozenset({"local", "staging", "production"})
APP_ROLE_VALUES = frozenset({"api", "worker"})
DEFAULT_APP_ENV = "local"
DEFAULT_APP_ROLE = "api"

# Public, well-known local defaults. Compared against, never interpolated into
# operator-facing messages.
_DEV_PASSPHRASE = "changeme"
_DEV_SESSION_SECRET = "dev-session-secret-change-me-32b!!"
_DEV_VAULT_SECRET = "company-os-local-vault"

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSEY = frozenset({"0", "false", "no", "off"})

_WARNED = False


class Requirement(str, Enum):
    ALL = "required_all"
    OUTSIDE_LOCAL = "required_outside_local"
    OPTIONAL = "optional_default"


class Problem(str, Enum):
    MISSING = "missing"
    TOO_SHORT = "too short"
    STILL_AT_DEFAULT = "still at default"
    NOT_ENABLED = "not enabled"
    SQLITE = "sqlite is not allowed outside local"
    INVALID = "invalid value"


@dataclass(frozen=True)
class Violation:
    name: str
    problem: Problem
    requirement: Requirement

    def line(self) -> str:
        return f"{self.name}: {self.problem.value}"


# Canonical classification table. Optional vars are documented in `.env.example`
# and are not boot-fatal; they still appear here so operators have one list.
CLASSIFICATION: dict[str, Requirement] = {
    "APP_ENV": Requirement.ALL,
    "APP_ROLE": Requirement.ALL,
    "COMPANY_OS_ENV": Requirement.OPTIONAL,  # local|cloud — adapter selection, not APP_ENV
    "CONSOLE_PASSPHRASE": Requirement.OUTSIDE_LOCAL,
    "SESSION_SECRET": Requirement.OUTSIDE_LOCAL,
    "COS_INTERNAL_ASSERTION_SECRET": Requirement.OUTSIDE_LOCAL,
    "COMPANY_OS_REQUIRE_SIGNED_CALLER": Requirement.OUTSIDE_LOCAL,
    "COMPANY_OS_VAULT_SECRET": Requirement.OUTSIDE_LOCAL,
    "ADMIN_REQUIRE_TOTP": Requirement.OUTSIDE_LOCAL,
    "ADMIN_BOOTSTRAP_EMAILS": Requirement.OUTSIDE_LOCAL,
    "COMPANY_OS_DB_URL": Requirement.OUTSIDE_LOCAL,
    "DATABASE_URL": Requirement.OUTSIDE_LOCAL,
    "DB_URL": Requirement.OUTSIDE_LOCAL,
    "WARDEN_DB_URL": Requirement.OUTSIDE_LOCAL,
    "GATEWAY_USERS_DB": Requirement.OUTSIDE_LOCAL,
    "LLM_CREDITS_REQUIRE_GATE": Requirement.OUTSIDE_LOCAL,
    "COMPANY_OS_DATA_DIR": Requirement.OPTIONAL,
    "REDIS_URL": Requirement.OPTIONAL,
    "LLM_OFFLINE": Requirement.OPTIONAL,
    "LLM_API_KEY": Requirement.OPTIONAL,
    "CODING_AGENT": Requirement.OPTIONAL,
    "BUILDER_SERVICE_URL": Requirement.OPTIONAL,
    "BUILDER_SERVICE_TOKEN": Requirement.OPTIONAL,
    "BUILDER_SANDBOX": Requirement.OPTIONAL,
    "LLM_API_VERSION": Requirement.OPTIONAL,
    "ONBOARDING_SYNC": Requirement.OPTIONAL,
    "CHANNELS_DRY_RUN": Requirement.OPTIONAL,
    "COMPANY_OS_LLM_BUDGET_USD_MONTH": Requirement.OPTIONAL,
    "COMPANY_OS_LLM_BUDGET_WARN_PERCENT": Requirement.OPTIONAL,
    "ZERNIO_API_KEY": Requirement.OPTIONAL,
    "ZERNIO_API_URL": Requirement.OPTIONAL,
    "OUTSTAND_API_KEY": Requirement.OPTIONAL,
    "FIRSTLOOK_COPY_MODE": Requirement.OPTIONAL,
    "FIRSTLOOK_DATA_DIR": Requirement.OPTIONAL,
    "FIRSTLOOK_ENGINE": Requirement.OPTIONAL,
    "NEXT_PUBLIC_GATEWAY_URL": Requirement.OPTIONAL,
    "CREDITS_ENFORCEMENT_ENABLED": Requirement.OPTIONAL,  # unused; enforcement is a DB policy
    "CREDITS_DEFAULT_VENTURE": Requirement.OPTIONAL,
}


class EnvContractError(RuntimeError):
    """Boot refused because ``APP_ENV`` is not local and the contract is violated."""

    def __init__(self, app_env: str, violations: list[Violation]) -> None:
        self.app_env = app_env
        self.violations = list(violations)
        super().__init__(format_violations(app_env, self.violations, fatal=True))


def format_violations(
    app_env: str,
    violations: list[Violation],
    *,
    fatal: bool,
) -> str:
    n = len(violations)
    noun = "error" if n == 1 else "errors"
    if fatal:
        head = f"Refusing to boot with APP_ENV={app_env} ({n} configuration {noun}):"
    else:
        head = (
            f"APP_ENV={app_env} would refuse to boot in staging/production "
            f"({n} configuration {noun}):"
        )
    body = "\n".join(f"  - {v.line()}" for v in violations)
    return f"{head}\n{body}"


def _get(env: Mapping[str, str], name: str) -> str:
    return (env.get(name) or "").strip()


def parse_app_env(env: Mapping[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    raw = _get(source, "APP_ENV").lower()
    if not raw and _running_in_ecs(source):
        raise EnvContractError(
            "missing", [Violation("APP_ENV", Problem.MISSING, Requirement.ALL)]
        )
    raw = raw or DEFAULT_APP_ENV
    if raw not in APP_ENV_VALUES:
        raise EnvContractError(
            "invalid",
            [Violation("APP_ENV", Problem.INVALID, Requirement.ALL)],
        )
    return raw


def _running_in_ecs(env: Mapping[str, str]) -> bool:
    return bool(_get(env, "ECS_CONTAINER_METADATA_URI") or _get(env, "ECS_CONTAINER_METADATA_URI_V4"))


def cookies_secure(env: Mapping[str, str] | None = None) -> bool:
    """Session cookies must be Secure outside local HTTP."""
    source = os.environ if env is None else env
    try:
        return parse_app_env(source) != "local"
    except EnvContractError:
        return True


def coerce_bool(value: object, *, default: bool = False) -> bool:
    """Parse a flag from env. Empty / unknown values use ``default``; never raise.

    Pydantic ``bool`` fields reject ``ADMIN_REQUIRE_TOTP=`` (empty). Settings
    should call this from a ``before`` validator so ``.env.example`` can leave
    optional flags blank.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raw = str(value).strip().lower()
    if not raw:
        return default
    if raw in _TRUTHY:
        return True
    if raw in _FALSEY:
        return False
    return default


def _is_truthy(raw: str) -> bool:
    return raw.lower() in _TRUTHY


def _is_falsey(raw: str) -> bool:
    return raw.lower() in _FALSEY


def _is_sqlite(value: str) -> bool:
    v = value.strip().lower()
    if not v:
        return True
    if "sqlite" in v:
        return True
    return v.endswith((".db", ".sqlite", ".sqlite3"))


def _byte_len(value: str) -> int:
    return len(value.encode("utf-8"))


def _chosen_db_url(env: Mapping[str, str]) -> tuple[str, str]:
    """Return (variable_name, url) for the URL company_os would actually use."""
    for name in ("COMPANY_OS_DB_URL", "DATABASE_URL", "DB_URL"):
        raw = _get(env, name)
        if raw:
            return name, raw
    return "COMPANY_OS_DB_URL", ""


_SERVICE_REQUIREMENTS = {
    "core": {"COS_INTERNAL_ASSERTION_SECRET", "COMPANY_OS_REQUIRE_SIGNED_CALLER", "COMPANY_OS_VAULT_SECRET",
             "ADMIN_REQUIRE_TOTP", "ADMIN_BOOTSTRAP_EMAILS", "COMPANY_OS_DB_URL", "DATABASE_URL", "DB_URL",
             "LLM_CREDITS_REQUIRE_GATE"},
    "gateway": {"CONSOLE_PASSPHRASE", "SESSION_SECRET", "COS_INTERNAL_ASSERTION_SECRET",
                "COMPANY_OS_REQUIRE_SIGNED_CALLER", "COMPANY_OS_VAULT_SECRET", "ADMIN_REQUIRE_TOTP",
                "ADMIN_BOOTSTRAP_EMAILS", "GATEWAY_USERS_DB", "JWT_SIGNING_KEY_ES256"},
    "warden": {"WARDEN_DB_URL"},
}


def collect_violations(env: Mapping[str, str] | None = None, *, service: str | None = None) -> list[Violation]:
    """Return every contract violation. Never raises; never includes secret values."""
    source = os.environ if env is None else env
    if service is not None and service not in _SERVICE_REQUIREMENTS:
        raise ValueError("Unknown service environment contract")
    found: list[Violation] = []
    outside = Requirement.OUTSIDE_LOCAL

    app_env_raw = _get(source, "APP_ENV").lower()
    if not app_env_raw and _running_in_ecs(source):
        found.append(Violation("APP_ENV", Problem.MISSING, Requirement.ALL))
    if app_env_raw and app_env_raw not in APP_ENV_VALUES:
        found.append(Violation("APP_ENV", Problem.INVALID, Requirement.ALL))

    app_role_raw = _get(source, "APP_ROLE").lower()
    if app_role_raw and app_role_raw not in APP_ROLE_VALUES:
        found.append(Violation("APP_ROLE", Problem.INVALID, Requirement.ALL))

    passphrase = _get(source, "CONSOLE_PASSPHRASE")
    if not passphrase or passphrase == _DEV_PASSPHRASE:
        found.append(Violation("CONSOLE_PASSPHRASE", Problem.STILL_AT_DEFAULT, outside))

    session = _get(source, "SESSION_SECRET")
    if not session or session == _DEV_SESSION_SECRET:
        found.append(Violation("SESSION_SECRET", Problem.STILL_AT_DEFAULT, outside))
    elif _byte_len(session) < MIN_SECRET_BYTES:
        found.append(Violation("SESSION_SECRET", Problem.TOO_SHORT, outside))

    assertion = _get(source, "COS_INTERNAL_ASSERTION_SECRET")
    if not assertion:
        found.append(Violation("COS_INTERNAL_ASSERTION_SECRET", Problem.MISSING, outside))
    elif _byte_len(assertion) < MIN_SECRET_BYTES:
        found.append(Violation("COS_INTERNAL_ASSERTION_SECRET", Problem.TOO_SHORT, outside))

    if not _is_truthy(_get(source, "COMPANY_OS_REQUIRE_SIGNED_CALLER")):
        found.append(
            Violation("COMPANY_OS_REQUIRE_SIGNED_CALLER", Problem.NOT_ENABLED, outside)
        )

    vault = (
        _get(source, "COMPANY_OS_VAULT_SECRET")
        or _get(source, "SESSION_SECRET")
        or _get(source, "COMPANY_OS_SECRET")
    )
    if not vault or vault == _DEV_VAULT_SECRET or vault == _DEV_SESSION_SECRET:
        found.append(Violation("COMPANY_OS_VAULT_SECRET", Problem.STILL_AT_DEFAULT, outside))
    elif _byte_len(vault) < MIN_SECRET_BYTES:
        found.append(Violation("COMPANY_OS_VAULT_SECRET", Problem.TOO_SHORT, outside))

    totp_raw = _get(source, "ADMIN_REQUIRE_TOTP")
    if _is_falsey(totp_raw):
        found.append(Violation("ADMIN_REQUIRE_TOTP", Problem.NOT_ENABLED, outside))

    if not _get(source, "ADMIN_BOOTSTRAP_EMAILS"):
        found.append(Violation("ADMIN_BOOTSTRAP_EMAILS", Problem.MISSING, outside))

    db_name, db_url = _chosen_db_url(source)
    if _is_sqlite(db_url):
        found.append(Violation(db_name, Problem.SQLITE, outside))

    if _is_sqlite(_get(source, "WARDEN_DB_URL")):
        found.append(Violation("WARDEN_DB_URL", Problem.SQLITE, outside))

    if _is_sqlite(_get(source, "GATEWAY_USERS_DB")):
        found.append(Violation("GATEWAY_USERS_DB", Problem.SQLITE, outside))

    if not _is_truthy(_get(source, "LLM_CREDITS_REQUIRE_GATE")):
        found.append(Violation("LLM_CREDITS_REQUIRE_GATE", Problem.NOT_ENABLED, outside))

    if service == "gateway" and not _get(source, "JWT_SIGNING_KEY_ES256"):
        found.append(Violation("JWT_SIGNING_KEY_ES256", Problem.MISSING, outside))
    if service is not None:
        required = _SERVICE_REQUIREMENTS[service] | {"APP_ENV", "APP_ROLE"}
        found = [violation for violation in found if violation.name in required]
    if parse_app_env(source) == "local" and _get(source, "SUPYMEM_MODE").lower() == "local":
        found = [violation for violation in found if violation.requirement is not outside]
    return found


def ensure_boot_env(environ: Mapping[str, str] | None = None, *, service: str | None = None) -> list[Violation]:
    """Run the contract. Raises ``EnvContractError`` outside local; warns locally.

    Call this from each service's ``create_app`` *before* the FastAPI instance
    is constructed so a violation cannot serve traffic.
    """
    global _WARNED
    source = os.environ if environ is None else environ
    app_env_raw = parse_app_env(source)
    role_raw = _get(source, "APP_ROLE").lower()
    violations = collect_violations(source, service=service)
    identity_bad = app_env_raw not in APP_ENV_VALUES or (
        bool(role_raw) and role_raw not in APP_ROLE_VALUES
    )
    # An unrecognised APP_ENV is always fatal: we cannot tell local from hosted.
    if identity_bad:
        raise EnvContractError("invalid", violations)
    if not violations:
        return []
    if app_env_raw == "local":
        if not _WARNED:
            log.warning(format_violations(app_env_raw, violations, fatal=False))
            _WARNED = True
        return violations
    raise EnvContractError(app_env_raw, violations)


def reset_env_contract_state() -> None:
    """Test helper — allow the local warning to fire again."""
    global _WARNED
    _WARNED = False

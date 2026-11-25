"""Optional legacy lease verification is local; it never authorizes hosted billing.

The merged Mac product uses gateway PKCE and the server ledger. These tests cover
only the retained signed-lease parser for migration/compatibility; no consumer JWT
transport is installed into sole.py.
"""
from datetime import datetime, timedelta, timezone

import pytest
from llm_gateway.credit_lease import enforce_local_hosted_lease, generate_keypair, issue_lease, write_lease
from llm_gateway.hosted import HostedAuthRequired, HostedCreditsRequired, HostedPlanExpired


@pytest.fixture()
def lease_keys(tmp_path, monkeypatch):
    priv, pub = generate_keypair()
    monkeypatch.setenv("CREDIT_LEASE_PRIVATE_KEY", priv)
    monkeypatch.setenv("CREDIT_LEASE_PUBLIC_KEY", pub)
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_PACKAGED", "1")


def lease(remaining, expires):
    now = datetime.now(timezone.utc)
    return issue_lease(user_id="csu_test", email="founder@example.test", cognito_sub="sub-test",
                       remaining_micro=remaining, remaining_credits=str(remaining / 10000),
                       plan="trial", plan_expires_at=now + expires, now=now)


def test_zero_credit_lease_is_rejected(lease_keys):
    write_lease(lease(0, timedelta(days=10)))
    with pytest.raises(HostedCreditsRequired):
        enforce_local_hosted_lease()


def test_expired_plan_is_rejected(lease_keys):
    write_lease(lease(50000, timedelta(days=-1)))
    with pytest.raises(HostedPlanExpired):
        enforce_local_hosted_lease()


def test_explicit_byok_does_not_depend_on_legacy_platform_lease(lease_keys):
    write_lease(lease(0, timedelta(days=10)))
    assert enforce_local_hosted_lease(byok=True) is None


def test_missing_lease_is_not_authorization(lease_keys):
    with pytest.raises(HostedAuthRequired):
        enforce_local_hosted_lease()

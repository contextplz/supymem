"""Offline synthetic founder-credential reads never borrow platform credentials."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def founders(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/credentials.db")
    monkeypatch.setenv("COMPANY_OS_EPHEMERAL_VAULT", "1")
    load_company_os_app()
    from app.db.models import Venture
    from app.state import db
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk
    from app.workspaces.service import create_workspace
    from core.clock import utcnow

    a = ensure_desk(LOCAL_PERSON_ID).id
    b = create_workspace(kind="company", name="Synthetic B", entry_mode="existing_company", owner_id=LOCAL_PERSON_ID).id
    now = utcnow()
    with db()() as session:
        session.add_all([
            Venture(id="synthetic-a", workspace_id=a, name="Synthetic A", raw_idea="synthetic",
                    stage="draft", created_at=now, updated_at=now),
            Venture(id="synthetic-b", workspace_id=b, name="Synthetic B", raw_idea="synthetic",
                    stage="draft", created_at=now, updated_at=now),
        ])
        session.commit()
    return a, b


@pytest.mark.parametrize("file_store", [False, True])
def test_vault_lookup_requires_exact_binding_and_uses_latest_rotation(founders, tmp_path, monkeypatch, file_store):
    from app.connectors import secrets

    vault = secrets.FileSecretStore(tmp_path / "synthetic-vault.bin") if file_store else secrets.LocalSecretStore()
    moment = datetime(2026, 10, 8, tzinfo=timezone.utc)
    monkeypatch.setattr(secrets, "utcnow", lambda: moment)
    a, b = founders
    vault.put(workspace_id=a, provider="stripe", key="STRIPE_SECRET_KEY", value="synthetic-a-stripe")
    vault.put(workspace_id=b, provider="stripe", key="STRIPE_SECRET_KEY", value="synthetic-b-stripe")
    vault.put(workspace_id=a, provider="zernio", key="ZERNIO_API_KEY", value="synthetic-a-social")
    def find(workspace_id, provider, key):
        return vault.find(workspace_id=workspace_id, provider=provider, key=key, requester="test")
    assert find(a, "stripe", "STRIPE_SECRET_KEY") == "synthetic-a-stripe"
    assert find(b, "stripe", "STRIPE_SECRET_KEY") == "synthetic-b-stripe"
    assert find(a, "stripe", "ZERNIO_API_KEY") is None
    assert find("", "stripe", "STRIPE_SECRET_KEY") is None
    moment += timedelta(seconds=1)
    vault.put(workspace_id=a, provider="stripe", key="STRIPE_SECRET_KEY", value="synthetic-a-rotated")
    if file_store:
        vault = secrets.FileSecretStore(tmp_path / "synthetic-vault.bin")
    assert find(a, "stripe", "STRIPE_SECRET_KEY") == "synthetic-a-rotated"


def test_ambiguous_legacy_refs_never_choose_a_random_key(founders):
    from app.connectors.secrets import LocalSecretStore

    vault = LocalSecretStore()
    a, _ = founders
    for value in ("synthetic-old-a", "synthetic-old-b"):
        ref = vault.put(workspace_id=a, provider="stripe", key="STRIPE_SECRET_KEY", value=value)
        vault._meta[ref].pop("created_at")
    assert vault.find(workspace_id=a, provider="stripe", key="STRIPE_SECRET_KEY", requester="test") is None


@pytest.mark.parametrize("provider,key", [("zernio", "ZERNIO_API_KEY"), ("outstand", "OUTSTAND_API_KEY")])
def test_hosted_social_uses_own_vault_and_refuses_platform_key_for_other_founder(founders, monkeypatch, provider, key):
    from app.actuation.social import founder_social_connected, resolve_social_key
    from app.connectors.secrets import store

    a, _ = founders
    store().put(workspace_id=a, provider=provider, key=key, value="synthetic-founder-social")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ZERNIO_API_KEY", "synthetic-platform-social")
    monkeypatch.setenv("OUTSTAND_API_KEY", "synthetic-platform-outstand")
    assert resolve_social_key("synthetic-a") == (provider, "synthetic-founder-social")
    assert resolve_social_key("synthetic-b") is None
    assert founder_social_connected("synthetic-b") is False


def test_hosted_social_reads_the_real_catalog_binding(founders, monkeypatch):
    from app.actuation.social import resolve_social_key
    from app.api.local_tools_api import _social_health
    from app.connectors.secrets import store
    from app.secrets.catalog import get_slot

    spec = get_slot("zernio.api_key")
    assert spec.provider == "social"
    assert spec.env_keys == ("ZERNIO_API_KEY",)
    a, _ = founders
    store().put(workspace_id=a, provider=spec.provider, key=spec.env_keys[0], value="synthetic-catalog-social")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ZERNIO_API_KEY", "synthetic-platform-social")
    assert resolve_social_key("synthetic-a") == ("zernio", "synthetic-catalog-social")
    assert resolve_social_key("synthetic-b") is None
    assert _social_health("synthetic-a")["configured"] is True


def test_legacy_social_vault_namespace_is_bound_to_the_target_venture(founders, monkeypatch):
    from app.actuation.social import resolve_social_key
    from app.connectors.secrets import store

    store().put(workspace_id="synthetic-a", provider="social", key="vault:social", value="synthetic-legacy-a")
    monkeypatch.setenv("APP_ENV", "production")
    assert resolve_social_key("synthetic-a") == ("zernio", "synthetic-legacy-a")
    assert resolve_social_key("synthetic-b") is None


def test_hosted_flags_do_not_claim_a_platform_key_or_unusable_connection(founders, monkeypatch):
    from app.actuation.social import founder_social_connected
    from app.api.local_tools_api import _social_health
    from app.connectors.secrets import store
    from app.state import MEM

    a, _ = founders
    store().put(workspace_id=a, provider="zernio", key="ZERNIO_API_KEY", value="synthetic-a-key")
    MEM["founder_channels:synthetic-b"] = {"social": {"connected": "1"}}
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ZERNIO_API_KEY", "synthetic-platform-key")
    assert _social_health()["configured"] is False
    assert _social_health("synthetic-a")["configured"] is True
    assert _social_health("synthetic-b")["configured"] is False
    assert founder_social_connected("synthetic-b") is False


def test_local_social_env_compatibility_remains(founders, monkeypatch):
    from app.actuation.social import resolve_social_key

    monkeypatch.setenv("ZERNIO_API_KEY", "synthetic-local-key")
    assert resolve_social_key("synthetic-b") == ("zernio", "synthetic-local-key")


@pytest.mark.parametrize("env", ["staging", "production"])
def test_venture_stripe_uses_per_request_own_key_without_global_sdk_mutation(founders, monkeypatch, env):
    from app.connectors.secrets import store
    from app.money.stripe_client import StripeClient

    a, b = founders
    store().put(workspace_id=a, provider="stripe", key="STRIPE_SECRET_KEY", value="synthetic-founder-a-stripe")
    store().put(workspace_id=b, provider="stripe", key="STRIPE_SECRET_KEY", value="synthetic-founder-b-stripe")
    monkeypatch.setenv("APP_ENV", env)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "synthetic-platform-stripe")
    sdk = ModuleType("stripe")
    sdk.api_key = "synthetic-untouched-global"
    create = Mock(return_value=SimpleNamespace(id="synthetic-checkout", url="https://checkout.example/synthetic"))
    sdk.checkout = SimpleNamespace(Session=SimpleNamespace(create=create))
    monkeypatch.setitem(sys.modules, "stripe", sdk)
    client = StripeClient()
    for venture, expected in (("synthetic-a", "synthetic-founder-a-stripe"), ("synthetic-b", "synthetic-founder-b-stripe")):
        result = client.create_checkout_session(venture_id=venture, price_minor=100, product_name="Synthetic", currency="USD")
        assert result.mode == "live"
        assert create.call_args.kwargs["api_key"] == expected
        assert sdk.api_key == "synthetic-untouched-global"


def test_hosted_venture_without_stripe_key_never_calls_platform(founders, monkeypatch):
    from app.money.stripe_client import StripeClient

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "synthetic-platform-stripe")
    sdk = ModuleType("stripe")
    create = Mock(side_effect=AssertionError("no Stripe call allowed"))
    sdk.checkout = SimpleNamespace(Session=SimpleNamespace(create=create))
    monkeypatch.setitem(sys.modules, "stripe", sdk)
    result = StripeClient().create_checkout_session(venture_id="synthetic-b", price_minor=100, product_name="Synthetic", currency="USD")
    assert result.reason == "no_credential"
    create.assert_not_called()


def test_vault_backend_that_cannot_release_credentials_never_falls_back_on_hosted(founders, monkeypatch):
    from app.actuation.social import resolve_social_key
    from app.connectors import secrets
    from app.money.stripe_client import StripeClient

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "synthetic-platform-stripe")
    monkeypatch.setenv("ZERNIO_API_KEY", "synthetic-platform-social")
    monkeypatch.setattr(secrets, "_STORE", secrets.GatewayVaultStore("https://synthetic-vault.example"))
    assert resolve_social_key("synthetic-a") is None
    result = StripeClient().create_checkout_session(venture_id="synthetic-a", price_minor=100, product_name="Synthetic", currency="USD")
    assert result.reason == "no_credential"

"""Synthetic offline request/save journeys bind secrets to verified workspaces."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from tests.security import test_founder_credential_readers as credential_readers

founders = credential_readers.founders


@pytest.fixture()
def requests_env(founders, tmp_path, monkeypatch):
    from app.secrets import service

    monkeypatch.delenv("ZERNIO_API_KEY", raising=False)
    # Hosted paths must never reach the process writer; local operator tests
    # still isolate their intended local write from the developer's checkout.
    def local_writer(*args, **kwargs):
        from core.env_contract import parse_app_env
        assert parse_app_env() == "local", "hosted credential save reached process env writer"
        return tmp_path / "unused-env"
    monkeypatch.setattr(service, "upsert_env", Mock(side_effect=local_writer))
    service.reset_secret_slots()
    return founders


def test_hosted_save_fulfills_only_own_workspace_and_never_platform(requests_env, monkeypatch):
    from app.connectors.secrets import store
    from app.secrets import service
    from app.secrets.catalog import get_slot
    from app.tenancy import bind

    a, b = requests_env
    spec = get_slot("zernio.api_key")
    platform = service.request_secret(spec.id)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ZERNIO_API_KEY", "synthetic-platform-key")
    with bind(workspace_id=a, venture_id="synthetic-a", actor="synthetic-founder-a"):
        request_a = service.request_secret(spec.id, venture_id="synthetic-a")
    with bind(workspace_id=b, venture_id="synthetic-b", actor="synthetic-founder-b"):
        request_b = service.request_secret(spec.id, venture_id="synthetic-b")
    with bind(workspace_id=a, venture_id="synthetic-a", actor="synthetic-founder-a"):
        service.save_secret(spec.id, {spec.env_keys[0]: "synthetic-owned-a"}, workspace_id=a)
        assert service.pending_requests() == []
    rows = service._load()
    assert rows[request_a["request_id"]]["status"] == "fulfilled"
    assert rows[request_b["request_id"]]["status"] == "pending"
    assert rows[platform["request_id"]]["status"] == "pending"
    assert store().find(workspace_id=a, provider=spec.provider, key=spec.env_keys[0], requester="test") == "synthetic-owned-a"
    assert store().find(workspace_id=b, provider=spec.provider, key=spec.env_keys[0], requester="test") is None
    assert service.env_is_set(spec.env_keys[0]) is True  # Platform availability did not arm either founder request.


def test_venture_only_request_resolves_db_binding_and_rejects_explicit_mismatch(requests_env, monkeypatch):
    from app.secrets import service
    from app.tenancy import bind

    a, b = requests_env
    monkeypatch.setenv("APP_ENV", "production")
    with bind(workspace_id=a, venture_id="synthetic-a", actor="synthetic-founder-a"):
        first = service.request_secret("zernio.api_key", venture_id="synthetic-a")
        same = service.request_secret("zernio.api_key", venture_id="synthetic-a", workspace_id=a)
        assert first["request_id"] == same["request_id"]
        assert service._load()[first["request_id"]]["workspace_id"] == a
        with pytest.raises(service.SecretSlotError, match="another workspace"):
            service.request_secret("zernio.api_key", venture_id="synthetic-a", workspace_id=b)
        with pytest.raises(service.SecretSlotError, match="does not belong"):
            service.request_secret("zernio.api_key", venture_id="synthetic-b", workspace_id=a)


def test_workspace_only_requests_dedupe_within_the_verified_workspace(requests_env, monkeypatch):
    from app.secrets import service
    from app.tenancy import bind

    a, b = requests_env
    platform = service.request_secret("zernio.api_key")
    monkeypatch.setenv("APP_ENV", "production")
    with bind(workspace_id=a, venture_id="synthetic-a", actor="synthetic-founder-a"):
        first = service.request_secret("zernio.api_key", workspace_id=a)
        repeat = service.request_secret("zernio.api_key")  # Trusted tenant supplies the omitted workspace.
        assert first["request_id"] == repeat["request_id"]
    with bind(workspace_id=b, venture_id="synthetic-b", actor="synthetic-founder-b"):
        other = service.request_secret("zernio.api_key", workspace_id=b)
    assert len({platform["request_id"], first["request_id"], other["request_id"]}) == 3


def test_legacy_venture_only_rows_fulfill_only_their_db_owned_workspace(requests_env, monkeypatch):
    from app.secrets import service
    from app.tenancy import bind

    a, _ = requests_env
    old_a = service.request_secret("zernio.api_key", venture_id="synthetic-a")
    old_b = service.request_secret("zernio.api_key", venture_id="synthetic-b")
    platform = service.request_secret("zernio.api_key")
    monkeypatch.setenv("APP_ENV", "production")
    with bind(workspace_id=a, venture_id="synthetic-a", actor="synthetic-founder-a"):
        service.save_secret("zernio.api_key", {"ZERNIO_API_KEY": "synthetic-owned-a"}, workspace_id=a)
    rows = service._load()
    assert rows[old_a["request_id"]]["status"] == "fulfilled"
    assert rows[old_b["request_id"]]["status"] == "pending"
    assert rows[platform["request_id"]]["status"] == "pending"


@pytest.mark.parametrize("explicit_workspace", [False, True])
def test_hosted_private_request_without_verified_tenant_refuses(requests_env, monkeypatch, explicit_workspace):
    from app.secrets import service

    a, _ = requests_env
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(service.SecretSlotError, match="Choose a workspace"):
        service.request_secret("zernio.api_key", workspace_id=a if explicit_workspace else None)
    with pytest.raises(service.SecretSlotError, match="Choose a workspace"):
        service.save_secret("zernio.api_key", {"ZERNIO_API_KEY": "synthetic-owned-a"}, workspace_id=a)
    assert service._load() == {}
    service.upsert_env.assert_not_called()


def test_hosted_pending_list_and_status_reject_hostile_workspace(requests_env, monkeypatch):
    from app.secrets import service
    from app.tenancy import bind

    a, b = requests_env
    req_a = service.request_secret("zernio.api_key", venture_id="synthetic-a", workspace_id=a)
    service.request_secret("zernio.api_key", venture_id="synthetic-b", workspace_id=b)
    service.request_secret("zernio.api_key")
    monkeypatch.setenv("APP_ENV", "production")
    with bind(workspace_id=a, venture_id="synthetic-a", actor="synthetic-founder-a"):
        pending = service.pending_requests()
        assert [item["id"] for item in pending] == [req_a["request_id"]]
        with pytest.raises(service.SecretSlotError):
            service.pending_requests(workspace_id=b)
        with pytest.raises(service.SecretSlotError):
            service._status_for(service._spec_or_raise("zernio.api_key"), workspace_id=b)


def test_local_operator_can_still_fulfill_machine_slot_requests(requests_env):
    from app.secrets import service

    first = service.request_secret("zernio.api_key", venture_id="synthetic-a")
    second = service.request_secret("zernio.api_key", venture_id="synthetic-b")
    service.save_secret("zernio.api_key", {"ZERNIO_API_KEY": "synthetic-local-operator"})
    rows = service._load()
    assert rows[first["request_id"]]["status"] == "fulfilled"
    assert rows[second["request_id"]]["status"] == "fulfilled"

"""Founders must never overwrite or adopt the platform's process-wide secrets on hosted Core."""

from __future__ import annotations

import asyncio
import os

import pytest
from fastapi.testclient import TestClient

from tests.company_os_harness import load_company_os_app

_PLATFORM = "sk_test_" + "P" * 24
_FOUNDER = "sk_test_" + "F" * 24


@pytest.fixture()
def hosted(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/plat.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_SECRETS_ENV", str(tmp_path / ".env"))
    monkeypatch.setenv("COMPANY_OS_MEMORY_PATH", str(tmp_path / "mem.sqlite3"))
    monkeypatch.setenv("COMPANY_OS_BRAIN_GRAPH_PATH", str(tmp_path / "graph.sqlite3"))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    monkeypatch.setenv("CODING_AGENT", "inhouse_stub")
    monkeypatch.setenv("STRIPE_SECRET_KEY", _PLATFORM)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "P" * 36)
    main = load_company_os_app()
    from app import state as state_mod

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    with TestClient(main.create_app()) as client:
        created = client.post("/ventures", json={"raw_idea": "a marketplace for vintage synthesizers"})
        assert created.status_code == 201, created.text
        # Flip to hosted after setup so the request path sees production.
        monkeypatch.setenv("APP_ENV", "production")
        yield client, created.json()["id"], tmp_path


def test_hosted_connect_stripe_does_not_touch_process_env(hosted) -> None:
    client, vid, tmp_path = hosted
    client.get(f"/ventures/{vid}/connect/stripe")  # opens the human task when needed
    res = client.post(f"/ventures/{vid}/connect/stripe", json={"fields": {"token": _FOUNDER}})
    assert res.status_code in {200, 409}, res.text
    assert os.environ["STRIPE_SECRET_KEY"] == _PLATFORM
    env_file = tmp_path / ".env"
    assert not env_file.exists() or _FOUNDER not in env_file.read_text()


def test_upsert_env_refuses_outside_local(hosted) -> None:
    _client, _vid, tmp_path = hosted
    from app.secrets.dotenv_file import upsert_env

    with pytest.raises(PermissionError):
        upsert_env("STRIPE_SECRET_KEY", _FOUNDER, path=tmp_path / ".env")
    assert os.environ["STRIPE_SECRET_KEY"] == _PLATFORM


def test_save_secret_hosted_needs_workspace_and_skips_env(hosted) -> None:
    from app.secrets.service import SecretSlotError, save_secret

    with pytest.raises(SecretSlotError) as err:
        save_secret("stripe.secret_key", {"STRIPE_SECRET_KEY": _FOUNDER})
    assert err.value.code == "workspace_scope_required"
    with pytest.raises(SecretSlotError) as unverified:
        save_secret("stripe.secret_key", {"STRIPE_SECRET_KEY": _FOUNDER}, workspace_id="ws_unit")
    assert unverified.value.code == "workspace_scope_required"
    from app.db.models import Venture
    from app.state import db
    from app.tenancy import bind
    _client, vid, _tmp = hosted
    with db()() as session:
        wid = session.get(Venture, vid).workspace_id
    with bind(workspace_id=wid, venture_id=vid, actor="synthetic-verified-founder"):
        out = save_secret("stripe.secret_key", {"STRIPE_SECRET_KEY": _FOUNDER}, workspace_id=wid)
    assert out["status"] == "stored"
    assert os.environ["STRIPE_SECRET_KEY"] == _PLATFORM


def test_hosted_connection_never_adopts_platform_token(hosted) -> None:
    _client, vid, _tmp = hosted
    from app.connectors.secrets import store
    from app.connectors.service import create_connection
    from app.db.models import Venture
    from app.state import db

    with db()() as session:
        wid = session.get(Venture, vid).workspace_id
    assert wid
    try:
        asyncio.run(
            create_connection(workspace_id=wid, provider="github", token=None, external_account="acme")
        )
    except Exception:  # noqa: BLE001 — refusing is fine; adopting the platform token is not
        pass
    assert os.environ["GITHUB_TOKEN"] not in store().values(wid)

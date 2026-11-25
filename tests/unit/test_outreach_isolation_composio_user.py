"""OT-19: Composio Gmail identity is workspace-bound; foreign client identities are rejected."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def setup(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ot19.db")
    monkeypatch.setenv("SANDBOX_MODE", "true")
    monkeypatch.setenv("SUPYMEM_DEFAULT_USER_ID", "local-user")
    monkeypatch.delenv("OPS_API_KEY", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)
    main = load_company_os_app()
    from app import state as state_mod
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk
    from app.workspaces.service import create_workspace

    state_mod.reset_db_factory()
    desk_a = ensure_desk(LOCAL_PERSON_ID)
    ws_b = create_workspace(
        kind="company",
        name="TenantB-Gmail",
        entry_mode="existing_company",
        owner_id=LOCAL_PERSON_ID,
    )
    return {"main": main, "desk_a": desk_a.id, "ws_b": ws_b.id}


def test_default_user_id_is_namespaced(setup, monkeypatch):
    from app.gtm.supymem import composio_gmail

    a = composio_gmail.default_user_id(setup["desk_a"])
    b = composio_gmail.default_user_id(setup["ws_b"])
    assert a == f"gmail:{setup['desk_a']}"
    assert b == f"gmail:{setup['ws_b']}"
    assert a != b
    assert "local-user" not in {a, b, composio_gmail.default_user_id()}
    monkeypatch.setenv("SUPYMEM_DEFAULT_USER_ID", "local-user")
    assert composio_gmail.default_user_id(setup["desk_a"]) == a


def test_connections_hub_gmail_connect_binds_workspace_and_callback(setup, monkeypatch):
    from app.gtm.supymem import composio_gmail
    from app.db.models import ContentOAuthState, Venture
    from app.state import db
    from sqlalchemy import select
    from core.clock import utcnow

    seen: list[tuple[str | None, str | None]] = []

    def fake_create(app_user_id=None, *, workspace_id=None, callback=None):
        seen.append((app_user_id, callback))
        return {
            "app_user_id": app_user_id,
            "connection_id": "ca_hub_workspace_b",
            "auth_config_id": "ac_hub_gmail",
            "redirect_url": "https://connect.composio.dev/link/ln_workspace_b",
        }

    monkeypatch.setattr(composio_gmail, "configured", lambda: True)
    monkeypatch.setattr(composio_gmail, "create_gmail_connection", fake_create)
    now = utcnow()
    with db()() as session:
        session.add(Venture(id="ven_hub_b", workspace_id=setup["ws_b"], name="Tenant B",
                            raw_idea="A useful product", stage="idea", autonomy_level="L1",
                            created_at=now, updated_at=now))
        session.commit()
    with TestClient(setup["main"].create_app()) as client:
        response = client.post(
            "/api/v1/connections/gmail/connect",
            json={"return_to": f"/connections?workspace={setup['ws_b']}&venture=ven_hub_b"},
            headers={"X-Workspace-Id": setup["ws_b"]},
        )
    assert response.status_code == 200, response.text
    assert response.json()["redirect_url"] == "https://connect.composio.dev/link/ln_workspace_b"
    assert len(seen) == 1
    assert seen[0][0] == composio_gmail.user_id_for(setup["ws_b"])
    assert "supymem_state=" in (seen[0][1] or "")
    with db()() as session:
        attempt = session.execute(select(ContentOAuthState)).scalar_one()
        assert attempt.workspace_id == setup["ws_b"]
        assert attempt.provider_user_id == composio_gmail.user_id_for(setup["ws_b"])
        assert attempt.connected_account_id == "ca_hub_workspace_b"
        assert attempt.return_to == (
            f"http://127.0.0.1:3100/connections?workspace={setup['ws_b']}&venture=ven_hub_b"
        )


def test_connections_hub_rejects_foreign_return_scope(setup, monkeypatch):
    from app.content.oauth.flow import _safe_return_to
    from app.content.oauth.attempts import OAuthError

    with pytest.raises(OAuthError, match="Return to a Supymem"):
        _safe_return_to(f"/connections?workspace={setup['desk_a']}", workspace_id=setup["ws_b"])
    with pytest.raises(OAuthError, match="Return to a Supymem"):
        _safe_return_to(f"/connections?workspace={setup['ws_b']}&venture=ven_foreign", workspace_id=setup["ws_b"])
    with pytest.raises(OAuthError, match="Return to a Supymem"):
        _safe_return_to(f"/connections?workspace={setup['ws_b']}&workspace={setup['ws_b']}", workspace_id=setup["ws_b"])


def test_gmail_callback_returns_to_signed_company_and_venture(setup, monkeypatch):
    from app.content.oauth import callbacks
    from app.db.models import Venture
    from app.state import db
    from core.clock import utcnow

    now = utcnow()
    with db()() as session:
        session.add(Venture(id="ven_callback_b", workspace_id=setup["ws_b"], name="Tenant B",
                            raw_idea="A useful product", stage="idea", autonomy_level="L1",
                            created_at=now, updated_at=now))
        session.commit()
    target = f"http://127.0.0.1:3100/connections?workspace={setup['ws_b']}&venture=ven_callback_b"
    monkeypatch.setattr(callbacks.attempts, "consume", lambda *_: {
        "id": "attempt_digest", "workspace_id": setup["ws_b"],
        "actor_subject": "local", "auth_method": "composio", "return_to": target,
    })
    monkeypatch.setattr(callbacks, "_complete_managed", lambda *_: None)
    monkeypatch.setattr(callbacks.attempts, "record_outcome", lambda *_: None)
    with TestClient(setup["main"].create_app()) as client:
        response = client.get("/api/v1/ai-gtm/gmail/callback?supymem_state=test", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == (
        target + "&provider=gmail&oauth=connected"
    )


@pytest.mark.parametrize("domain", ["warehouse teams", "home gardeners"])
def test_connect_rejects_client_user_id_and_header_swap_cannot_use_a(setup, monkeypatch, domain):
    from app.authz.workspace import ensure_subject_person
    from app.gtm.supymem import composio_gmail
    from core.clock import utcnow
    from core.ids import new_id
    from app.db.models import GtmEmailAccount
    from app.state import db
    from app.workspaces.service import create_workspace

    other_owner = ensure_subject_person("usr_other_founder")
    workspace_b = create_workspace(kind="company", name=domain,
                                   entry_mode="existing_company", owner_id=other_owner)

    captured: list[dict] = []

    def fake_create(app_user_id=None, *, workspace_id=None, callback=None):
        uid = composio_gmail.default_user_id(workspace_id)
        captured.append(
            {
                "workspace_id": workspace_id,
                "user_id": uid,
                "client_user_id": app_user_id,
            }
        )
        return {
            "app_user_id": uid,
            "connection_id": f"ca_for_{uid}",
            "redirect_url": "http://example.invalid/oauth",
        }

    monkeypatch.setattr(
        "app.gtm.supymem.composio_gmail.create_gmail_connection", fake_create
    )

    ca_a = "ca_tenant_a_secret"
    now = utcnow()
    with db()() as session:
        session.add(
            GtmEmailAccount(
                id=new_id("gtmba"),
                workspace_id=setup["desk_a"],
                app_user_id=composio_gmail.default_user_id(setup["desk_a"]),
                provider="gmail",
                email="ada@sample-analytics.test",
                composio_user_id=composio_gmail.default_user_id(setup["desk_a"]),
                connected_account_id=ca_a,
                daily_send_limit=25,
                sent_today=0,
                status="active",
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()

    sent_calls: list[tuple[str, str]] = []

    def fake_send(*, composio_user_id, connected_account_id, **_k):
        sent_calls.append((composio_user_id, connected_account_id))
        raise AssertionError("must not send using another tenant mailbox")

    monkeypatch.setattr(
        "app.gtm.supymem.composio_gmail.send_gmail_email", fake_send
    )

    with TestClient(setup["main"].create_app()) as client:
        stolen_connect = client.post(
            "/api/v1/ai-gtm/gmail/connect",
            json={"user_id": composio_gmail.default_user_id(setup["desk_a"])},
            headers={"X-Workspace-Id": workspace_b.id, "X-Subject": other_owner},
        )
        assert stolen_connect.status_code == 403, stolen_connect.text
        assert stolen_connect.json()["detail"]["code"] == "provider_identity_mismatch"
        assert captured == []

        from app.gtm.supymem.pipeline import send_test_email, sync_gmail_accounts

        def fake_list(app_user_id=None, *, workspace_id=None):
            uid = composio_gmail.default_user_id(workspace_id) if workspace_id else app_user_id
            if uid == composio_gmail.default_user_id(setup["desk_a"]) or uid == "local-user":
                return [{"id": ca_a, "status": "ACTIVE", "user_id": uid}]
            return []

        monkeypatch.setattr(
            "app.gtm.supymem.composio_gmail.list_gmail_connections", fake_list
        )
        with pytest.raises(ValueError, match="identity must belong"):
            sync_gmail_accounts(workspace_b.id, app_user_id="local-user")
        synced_b = sync_gmail_accounts(workspace_b.id)
        accounts_b = synced_b.get("accounts") or []
        assert ca_a not in {a.get("connected_account_id") for a in accounts_b}

        send_test_email(
            workspace_b.id,
            to="other@example.invalid",
            subject="hi",
            body="hello",
            confirm=True,
        )
        assert sent_calls == []
        assert ca_a not in {cid for _uid, cid in sent_calls}


def test_managed_connect_preserves_server_identity_and_workspace_contract(setup, monkeypatch):
    from app.gtm.supymem import composio_gmail

    calls = []
    monkeypatch.setattr(composio_gmail, "ensure_auth_config", lambda: "ac_test")
    def request(method, path, *, json):
        calls.append(json)
        return {"connected_account_id": "ca_test", "redirect_url": "https://example.invalid/oauth"}
    monkeypatch.setattr(composio_gmail, "_request", request)
    uid = composio_gmail.user_id_for(setup["ws_b"])
    callback = "https://example.invalid/callback?supymem_state=test"
    result = composio_gmail.create_gmail_connection(uid, callback=callback)
    assert result["app_user_id"] == uid
    assert calls[-1]["user_id"] == uid
    assert calls[-1]["callback_url"] == callback
    composio_gmail.create_gmail_connection(workspace_id=setup["ws_b"])
    assert calls[-1]["user_id"] == uid
    with pytest.raises(ValueError, match="identity must belong"):
        composio_gmail.create_gmail_connection(composio_gmail.user_id_for(setup["desk_a"]), workspace_id=setup["ws_b"])
    assert len(calls) == 2

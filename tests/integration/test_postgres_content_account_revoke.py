"""Signed HTTP plus migrated PostgreSQL; vault/provider deletion is isolated."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tests.integration.test_postgres_auth_security import pg_database as pg_database, concurrent, SECRET
from tests.integration.test_postgres_oauth_state import oauth_db as oauth_db


@pytest.fixture()
def revocation(oauth_db, monkeypatch):
    from app.authz.workspace import ensure_subject_person
    from app.content import composio_social
    from app.content.oauth import flow
    from app.db.models import ContentAccount, WorkspaceMember
    from app.state import db
    from app.workspaces.service import create_workspace
    from core.assertion import reset_nonce_store, sign_assertion
    from core.clock import utcnow
    from core.ids import new_id
    _, _, wid, actor, _ = oauth_db
    for key, value in {"COS_INTERNAL_ASSERTION_SECRET": SECRET, "ADMIN_REQUIRE_TOTP": "1",
        "ADMIN_BOOTSTRAP_EMAILS": "test-admin@example.test", "LLM_OFFLINE": "1", "WILL_SCHEDULER": "0",
        "CONTENT_ENGINE_ENABLED": "0", "CONTENT_AGENT_ENABLED": "0", "SUPYMEM_OUTBOUND_EXECUTION_ENABLED": "0"}.items():
        monkeypatch.setenv(key, value)
    reset_nonce_store()
    other, contributor = new_id("usr"), new_id("usr")
    ensure_subject_person(other)
    ensure_subject_person(contributor)
    foreign = create_workspace(kind="company", name="Other company", owner_id=other)
    aid, bid, now = new_id("cac"), new_id("cac"), utcnow()
    with db()() as session:
        session.add(WorkspaceMember(id=new_id("wm"), workspace_id=wid, person_id=contributor,
            role="contributor", can_approve=0, created_at=now))
        for account_id, workspace in [(aid, wid), (bid, foreign.id)]:
            session.add(ContentAccount(id=account_id, workspace_id=workspace, platform="x", external_account=account_id,
                status="active", access_secret_ref="vault:test-access:" + account_id,
                refresh_secret_ref="vault:test-refresh:" + account_id,
                extra_json='{"auth_via":"composio","connected_account_id":"fixture-connection"}',
                created_at=now, updated_at=now))
        session.commit()
    forgotten, deleted = [], []
    monkeypatch.setattr(flow, "secret_store", lambda: SimpleNamespace(forget=forgotten.append))
    monkeypatch.setattr(composio_social, "delete_connection", lambda account_id, **kw: deleted.append(account_id))
    # No lifespan, provider account connection, secret retrieval or live revoke.
    from app.main import create_app
    client = TestClient(create_app())
    def send(account_id, *, subject=actor):
        path = f"/api/v1/content/accounts/{account_id}/revoke"
        assertion = sign_assertion(SECRET, sub=subject, email="", role="user", method="POST", path=path)
        return client.post(path, params={"workspace_id": wid}, json={}, headers={"X-COS-Assertion": assertion})
    yield {"send": send, "wid": wid, "actor": actor, "contributor": contributor, "own": aid,
        "foreign": bid, "forgotten": forgotten, "deleted": deleted}
    client.close()


def test_signed_foreign_account_and_missing_id_are_identical_and_have_no_side_effects(revocation):
    from app.db.models import ContentAccount
    from app.state import db
    d = revocation
    foreign, missing = d["send"](d["foreign"]), d["send"]("cac_missing")
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json() == {"detail": {"code": "no_account", "message": "That publishing account is not connected."}}
    assert not d["forgotten"] and not d["deleted"]
    with db()() as session:
        assert session.get(ContentAccount, d["foreign"]).status == "active"
        assert session.get(ContentAccount, d["own"]).status == "active"


def test_service_and_http_require_scoped_administrator_before_credential_mutation(revocation):
    from app.content.oauth import flow
    from app.tenancy import bind
    d = revocation
    response = d["send"](d["own"], subject=d["contributor"])
    assert response.status_code == 403 and response.json()["detail"]["code"] == "workspace_admin_required"
    with bind(workspace_id=d["wid"], venture_id="", actor=d["contributor"]):
        for call in [lambda: flow.revoke_account(d["own"], workspace_id=d["wid"]), lambda: flow.forget_all(d["wid"])]:
            with pytest.raises(flow.OAuthError) as error:
                call()
            assert error.value.code == "workspace_admin_required"
    with bind(workspace_id="wrong-workspace", venture_id="", actor=d["actor"]):
        with pytest.raises(flow.OAuthError) as error:
            flow.revoke_account(d["own"], workspace_id=d["wid"])
        assert error.value.code == "actor_required"
    assert not d["forgotten"] and not d["deleted"]


def test_same_account_concurrent_and_repeated_revocation_is_idempotent(revocation):
    from app.db.models import ContentAccount
    from app.state import db
    d = revocation
    responses = concurrent(lambda: d["send"](d["own"]))
    assert all(r.status_code == 200 and r.json()["status"] == "revoked" for r in responses)
    again = d["send"](d["own"])
    assert again.status_code == 200 and again.json() == responses[0].json()
    assert sorted(d["forgotten"]) == sorted(["vault:test-access:" + d["own"], "vault:test-refresh:" + d["own"]])
    assert d["deleted"] == ["fixture-connection"]
    assert "vault:" not in again.text and "fixture-connection" not in again.text
    with db()() as session:
        row = session.get(ContentAccount, d["own"])
        assert row.status == "revoked" and row.access_secret_ref is None and row.refresh_secret_ref is None
        assert session.get(ContentAccount, d["foreign"]).status == "active"

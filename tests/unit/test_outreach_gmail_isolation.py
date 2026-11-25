"""Gmail is shared only between workspaces owned by the same person."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.company_os_harness import load_company_os_app


@pytest.mark.parametrize("domain", ["supplier-invoices", "trail-packing"])
def test_connections_status_allows_discord_to_be_unshipped(setup, monkeypatch, domain):
    from fastapi import BackgroundTasks
    from app.api import ops_api
    from app.connections import hub

    status = {"connections": {"gmail": {"label": domain}}}
    monkeypatch.setattr(ops_api, "_auth_workspace", lambda *args: "ws_fixture")
    monkeypatch.setattr(hub, "hub_status", lambda workspace_id: status)
    background = BackgroundTasks()
    assert ops_api.connections_hub_status(background, workspace_id="ws_fixture",
        x_workspace_id=None, x_api_key=None) == status
    assert background.tasks == []


@pytest.fixture()
def setup(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ot37.db")
    monkeypatch.setenv("SANDBOX_MODE", "true")
    monkeypatch.delenv("OPS_API_KEY", raising=False)
    main = load_company_os_app()
    from core.clock import utcnow
    from core.ids import new_id

    from app import state as state_mod
    from app.authz.workspace import ensure_subject_person
    from app.db.models import GtmEmailAccount, WorkspaceMember
    from app.gtm.supymem.composio_gmail import default_user_id
    from app.state import db
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk
    from app.workspaces.service import create_workspace

    state_mod.reset_db_factory()
    desk_a = ensure_desk(LOCAL_PERSON_ID)
    other_owner = ensure_subject_person("usr_other_founder")
    ws_b = create_workspace(
        kind="company",
        name="TenantBGmail",
        entry_mode="existing_company",
        owner_id=other_owner,
    )
    now = utcnow()
    with db()() as session:
        session.add(WorkspaceMember(id="member_not_owner", workspace_id=ws_b.id, person_id=LOCAL_PERSON_ID,
            role="viewer", created_at=now))
        session.add(
            GtmEmailAccount(
                id=new_id("gtmba"),
                workspace_id=desk_a.id,
                app_user_id=default_user_id(desk_a.id),
                provider="gmail",
                email="a@tenant-a.dev",
                composio_user_id=default_user_id(desk_a.id),
                connected_account_id="ca_tenant_a",
                status="active",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            GtmEmailAccount(
                id=new_id("gtmba"),
                workspace_id=ws_b.id,
                app_user_id=default_user_id(ws_b.id),
                provider="gmail",
                email="b@tenant-b.dev",
                composio_user_id=default_user_id(ws_b.id),
                connected_account_id="ca_tenant_b",
                status="active",
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    return {"main": main, "a": desk_a.id, "b": ws_b.id, "other_owner": other_owner}


def test_connections_status_reads_saved_mailboxes_without_recounting_send_slots(setup, monkeypatch):
    import asyncio
    import json
    from contextlib import contextmanager
    from datetime import timedelta
    from time import perf_counter
    from types import SimpleNamespace

    from app.api import ops_api
    from app.community import composio_discord
    from app.connections import hub
    from app.credits import metered
    from app.db.models import ContentAccount, EventLog, GtmEmailAccount, Venture
    from app.gtm.supymem import composio_gmail
    from app.state import db
    from app.tenancy import bind
    from app.workspaces.resolve import LOCAL_PERSON_ID
    from app.workspaces.service import create_workspace
    from core.clock import utcnow
    from fastapi import BackgroundTasks
    from sqlalchemy import event

    monkeypatch.delenv("COMPOSIO_PROJECT", raising=False)
    monkeypatch.setenv("COMPOSIO_API_KEY", "offline-connection-status")
    monkeypatch.setattr(composio_gmail, "configured", lambda: True)
    monkeypatch.setattr("app.content.composio_social.configured", lambda: False)
    provider_reads = []

    def discord_read(method, path, **kwargs):
        provider_reads.append((method, path))
        return {"items": []}

    monkeypatch.setattr(composio_discord, "configured", lambda: True)
    monkeypatch.setattr(composio_discord, "_request", discord_read)
    charged = []

    @contextmanager
    def charge(**kwargs):
        charged.append(kwargs["purpose"])
        yield SimpleNamespace(record_micro=lambda amount: None)

    monkeypatch.setattr(metered, "charge_sync", charge)

    def unexpected_provider(*args, **kwargs):
        pytest.fail("Saved connection status must not call the provider")

    monkeypatch.setattr(composio_gmail, "execute_tool", unexpected_provider)
    monkeypatch.setattr(composio_gmail, "list_gmail_connections", unexpected_provider)
    now = utcnow()
    for domain, owner, workspace_id in (
        ("inventory coordinators", LOCAL_PERSON_ID, setup["a"]),
        ("weekend cyclists", setup["other_owner"], setup["b"]),
    ):
        provider_reads.clear()
        for index in range(20):
            workspace = create_workspace(kind="company", name=f"{domain} {index}", owner_id=owner)
            with db()() as session:
                session.add(Venture(id=f"venture_{workspace.id}", workspace_id=workspace.id,
                    name=domain, raw_idea=domain, stage="draft", created_at=now, updated_at=now))
                for connection in range(2):
                    session.add(GtmEmailAccount(id=f"mail_{workspace.id}_{connection}", workspace_id=workspace.id,
                        app_user_id=f"gmail:{workspace.id}", composio_user_id=f"gmail:{workspace.id}",
                        provider="gmail", email=f"reader{connection}@{index}.example",
                        connected_account_id=f"account_{workspace.id}_{connection}", status="active",
                        sent_today=3, sent_count_date=(now - timedelta(days=connection)).date().isoformat(),
                        created_at=now, updated_at=now))
                    session.add(ContentAccount(id=f"social_{workspace.id}_{connection}", workspace_id=workspace_id,
                        platform="linkedin", external_account=f"profile_{index}_{connection}", status="active",
                        created_at=now, updated_at=now))
                session.commit()
        statements = []

        def record_sql(connection, cursor, statement, parameters, context, executemany, statements=statements):
            statements.append(statement)

        engine = db().kw["bind"]
        event.listen(engine, "before_cursor_execute", record_sql)
        started = perf_counter()
        try:
            status = hub.hub_status(workspace_id)
        finally:
            event.remove(engine, "before_cursor_execute", record_sql)
        print(f"{domain}: {len(statements)} SQL statements in {perf_counter() - started:.3f}s")
        accounts = status["connections"]["gmail"]["accounts"]
        assert len(accounts) == 41
        assert status["connections"]["linkedin"]["active"] == 40
        assert ("b@tenant-b.dev" if workspace_id == setup["a"] else "a@tenant-a.dev") not in {
            account["email"] for account in accounts}
        assert not any("gtm_touches" in statement or "gtm_email_events" in statement for statement in statements)
        assert len(statements) < 100
        assert provider_reads == []
        assert next(account for account in accounts if account["email"] == "reader0@0.example")["sent_today"] == 3
        assert next(account for account in accounts if account["email"] == "reader1@0.example")["sent_today"] == 0
        with db()() as session:
            session.add(EventLog(id=f"saved_discord_{workspace_id}", topic="connections.discord.status",
                workspace_id=workspace_id, actor=owner, ts=now - timedelta(minutes=10),
                payload_json=json.dumps({"connected": True, "connection_id": f"discord_{workspace_id}"})))
            session.commit()
        background = BackgroundTasks()
        with bind(workspace_id=workspace_id, venture_id="", actor=owner):
            saved = ops_api.connections_hub_status(background, workspace_id=workspace_id,
                x_workspace_id=None, x_api_key=None)
            assert saved["connections"]["discord"]["connection_id"] == f"discord_{workspace_id}"
            assert saved["connections"]["discord"]["connected"] is True
            assert provider_reads == [] and len(background.tasks) == 1
            asyncio.run(background())
            assert len(provider_reads) == 4
            again = BackgroundTasks()
            refreshed = ops_api.connections_hub_status(again, workspace_id=workspace_id,
                x_workspace_id=None, x_api_key=None)
            assert refreshed["connections"]["discord"]["connected"] is False
            assert again.tasks == [] and len(provider_reads) == 4
            with db()() as session:
                session.add(EventLog(id=f"pending_discord_{workspace_id}", topic="connections.discord.status",
                    workspace_id=workspace_id, actor=owner, ts=now,
                    payload_json=json.dumps({"connected": False, "connection_id": f"discord_{workspace_id}",
                                             "status": "pending"})))
                session.commit()
            pending = BackgroundTasks()
            ops_api.connections_hub_status(pending, workspace_id=workspace_id, x_workspace_id=None, x_api_key=None)
            assert len(pending.tasks) == 1 and len(provider_reads) == 4
        assert charged[-1] == "connections.status"


def test_workspaces_cannot_list_each_others_gmail(setup, monkeypatch):
    from app.gtm.supymem import pipeline
    from app.gtm.supymem.composio_gmail import default_user_id
    from app.gtm.supymem.pipeline import list_mailboxes

    monkeypatch.setattr(
        pipeline,
        "sync_gmail_accounts",
        lambda wid, app_user_id=None: {"accounts": list_mailboxes(wid), "synced": 0},
    )
    uid_a = default_user_id(setup["a"])
    uid_b = default_user_id(setup["b"])
    assert uid_a != uid_b
    assert "local-user" not in {uid_a, uid_b}

    with TestClient(setup["main"].create_app()) as client:
        b = client.get(
            "/api/v1/ai-gtm/gmail/accounts",
            headers={"X-Workspace-Id": setup["b"], "X-Subject": setup["other_owner"]},
        )
        assert b.status_code == 200
        emails = {row.get("email") for row in b.json().get("accounts") or []}
        assert "a@tenant-a.dev" not in emails
        assert "b@tenant-b.dev" in emails
        a = client.get(
            "/api/v1/ai-gtm/gmail/accounts",
            headers={"X-Workspace-Id": setup["a"]},
        )
        emails_a = {row.get("email") for row in a.json().get("accounts") or []}
        assert "b@tenant-b.dev" not in emails_a


def test_one_founder_shares_gmail_and_cap_across_unrelated_ventures(setup, monkeypatch):
    import json
    from datetime import timedelta
    from sqlalchemy import select
    from app.autopilot import outreach
    from app.chat import connect
    from app.connections import hub
    from app.content.oauth import callbacks
    from app.db.models import ContentOAuthState, GtmContact, GtmEmailAccount, GtmEmailEvent, GtmOutreachSettings, GtmTouch, Venture
    from app.gtm.relationships.email_compliance import complete_body
    from app.gtm.supymem import composio_gmail, pipeline, workers
    from app.state import db
    from app.ventures.state_brief import _connections_section
    from app.workspaces.resolve import LOCAL_PERSON_ID
    from app.workspaces.service import create_workspace
    from core.clock import utcnow

    sibling = create_workspace(kind="company", name="Music lessons", owner_id=LOCAL_PERSON_ID)
    now = utcnow()
    bodies = {}
    with db()() as session:
        for index, (workspace_id, idea) in enumerate(((setup["a"], "Warehouse audit software for logistics teams"),
                                                     (sibling.id, "Music lessons for hobbyists"))):
            venture_id = f"ven_shared_{index}"
            session.add(Venture(id=venture_id, workspace_id=workspace_id, name=idea, raw_idea=idea, stage="idea", created_at=now, updated_at=now))
            session.add(GtmOutreachSettings(workspace_id=workspace_id, send_enabled=True,
                sender_postal_address=f"{index + 1} Fixture Road, CA, US", created_at=now, updated_at=now))
            contact = GtmContact(id=f"contact_shared_{index}", workspace_id=workspace_id, email=f"reader{index}@recipient.example",
                recipient_geo="US", lawful_basis="consent", lawful_basis_at=now, lawful_basis_note="Requested email updates",
                enrichment_json=json.dumps({"founder_import": True}), stage_changed_at=now, created_at=now)
            session.add(contact)
            session.flush()
            bodies[workspace_id] = complete_body(session, workspace_id, contact, "Would you like the details?", "a@tenant-a.dev")
        session.commit()

    monkeypatch.setattr(composio_gmail, "configured", lambda: True)
    with db()() as session:
        account = session.scalar(select(GtmEmailAccount).where(GtmEmailAccount.connected_account_id == "ca_tenant_a"))
        account.sent_today, account.sent_count_date = 1, now.date().isoformat()
        session.add(GtmTouch(id="legacy_outreach_shared", workspace_id=setup["a"], venture_id="ven_shared_0",
            contact_id="contact_shared_0", direction="out", channel="email", simulated=0, send_mode="founder_gmail",
            logged_by="outreach:ven_shared_0", sent_at=now, message_id="legacy_message", created_at=now))
        session.commit()
    for workspace_id in (setup["a"], sibling.id):
        assert pipeline.mail_readiness(workspace_id)["account_email"] == "a@tenant-a.dev"
        assert hub.hub_status(workspace_id)["connections"]["gmail"]["connected"]
        with db()() as session:
            assert _connections_section(session, GtmEmailAccount, workspace_id)["gmail"]["connected"]
    assert "already connected" in connect.handle("ven_shared_1", "connect gmail")["body_md"]
    assert outreach._mailbox(sibling.id)["user"] == composio_gmail.user_id_for(setup["a"])
    assert {row["email"] for row in pipeline.list_mailboxes(setup["b"])} == {"b@tenant-b.dev"}
    with db()() as session:
        other_account = session.scalar(select(GtmEmailAccount).where(GtmEmailAccount.workspace_id == setup["b"]))
        other_account.status = "expired"
        session.commit()
    assert not pipeline.mail_readiness(setup["b"])["connected"]
    assert not connect._is_connected(setup["b"], "gmail")

    proofs = []
    monkeypatch.setattr(composio_gmail, "verify_gmail_profile", lambda user, account: proofs.append((user, account)) or {
        "successful": True, "email": "a@tenant-a.dev", "log_id": "verified_shared"})
    assert pipeline.verify_gmail_connection(sibling.id)["ok"]
    assert proofs == [(composio_gmail.user_id_for(setup["a"]), "ca_tenant_a")]

    sent = []
    monkeypatch.setattr(composio_gmail, "_send_gmail_email", lambda **kwargs: sent.append(kwargs) or {
        "provider_message_id": f"message_{len(sent)}", "thread_id": "thread_shared"})
    def send(workspace_id, index, *, followup=False):
        return composio_gmail.send_gmail_email(composio_user_id=composio_gmail.user_id_for(setup["a"]),
            connected_account_id="ca_tenant_a", workspace_id=workspace_id, to=f"reader{index}@recipient.example",
            subject="Requested details", body=bodies.get(workspace_id, bodies[setup["a"]]), thread_id="thread_shared" if followup else None)

    with pytest.raises(composio_gmail.DefiniteFailure, match="gtm_gmail_required"):
        send(setup["b"], 0)
    assert not sent
    for index in range(13):
        send(setup["a"] if index % 2 == 0 else sibling.id, index % 2, followup=index > 1)
    assert len(sent) == 13
    for workspace_id in (setup["a"], sibling.id):
        with pytest.raises(composio_gmail.DefiniteFailure, match="daily cap"):
            send(workspace_id, 0 if workspace_id == setup["a"] else 1, followup=True)
        assert pipeline.list_mailboxes(workspace_id)[0]["sent_today"] == 15
    assert outreach.send_slots_used_today("ven_shared_0") == outreach.send_slots_used_today("ven_shared_1") == 15
    with db()() as session:
        account = session.scalar(select(GtmEmailAccount).where(GtmEmailAccount.connected_account_id == "ca_tenant_a"))
        for index, workspace_id in enumerate((setup["a"], sibling.id)):
            session.add(GtmTouch(id=f"tracked_shared_{index}", workspace_id=workspace_id, venture_id=f"ven_shared_{index}",
                contact_id=f"contact_shared_{index}", direction="out", channel="email", simulated=0, send_mode="founder_gmail",
                sent_at=now, message_id=f"message_{index + 1}", provider_thread_id=f"tracked_thread_{index}", created_at=now))
            session.add(GtmEmailEvent(id=f"checkpoint_shared_{index}", workspace_id=workspace_id, email_account_id=account.id,
                event_type="gmail_sync_checkpoint", metadata_json=json.dumps({"history_id": f"history_{index}"}),
                created_at=now + timedelta(seconds=index)))
        session.commit()
    histories = []
    monkeypatch.setattr(composio_gmail, "fetch_history", lambda **kwargs: histories.append(kwargs["start_history_id"]) or {
        "history": [], "historyId": "next_history"})
    workers.tick_replies(setup["a"])
    workers.tick_replies(sibling.id)
    assert histories == ["history_0", "history_1"]

    sync_users = []
    known_connections = callbacks.known_connections
    monkeypatch.setattr(composio_gmail, "list_gmail_connections", lambda user: sync_users.append(user) or [{
        "id": "ca_tenant_a", "user_id": user, "status": "REVOKED"}])
    monkeypatch.setattr("app.content.oauth.callbacks.known_connections", lambda *args, **kwargs: [])
    pipeline.sync_gmail_accounts(sibling.id)
    assert sync_users == [composio_gmail.user_id_for(setup["a"])]
    assert not pipeline.mail_readiness(setup["a"])["connected"]
    assert not pipeline.mail_readiness(sibling.id)["connected"]
    assert pipeline.mail_readiness(setup["b"])["account_email"] == "b@tenant-b.dev"
    with db()() as session:
        account = session.scalar(select(GtmEmailAccount).where(GtmEmailAccount.connected_account_id == "ca_tenant_a"))
        assert account.status == "needs_reconnect"
        assert len(list(session.scalars(select(GtmEmailEvent).where(GtmEmailEvent.event_type == "gmail_reserved")))) == 13
        account.status = "active"
        session.add(ContentOAuthState(id="receipt_shared", workspace_id=setup["a"], platform="gmail", actor_subject=LOCAL_PERSON_ID,
            auth_method="composio", provider_user_id=composio_gmail.user_id_for(setup["a"]), auth_config_id="auth_shared",
            connected_account_id="ca_tenant_a", outcome="connected", redirect_uri="https://callback.example/gmail",
            expires_at=now + timedelta(minutes=10), created_at=now, consumed_at=now))
        session.commit()
    monkeypatch.setattr(callbacks, "known_connections", known_connections)
    monkeypatch.setattr(composio_gmail, "list_gmail_connections", lambda user: [])
    def deleted_account(*args, **kwargs):
        raise composio_gmail.DefiniteFailure("Account deleted", status_code=404)
    monkeypatch.setattr(composio_gmail, "_request", deleted_account)
    pipeline.sync_gmail_accounts(sibling.id)
    assert not pipeline.mail_readiness(setup["a"])["connected"]
    assert not pipeline.mail_readiness(sibling.id)["connected"]

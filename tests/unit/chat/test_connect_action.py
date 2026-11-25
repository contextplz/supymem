"""'connect <service>' through chat: a real link plus exact steps, then watched until connected."""
from __future__ import annotations

import asyncio

import pytest
from tests.company_os_harness import company_os_test_client, load_company_os_app

load_company_os_app()
from app.chat import connect  # noqa: E402

FOUNDER_MESSAGE = "Hey I want to connect my Gmail can you help me do that"


@pytest.mark.parametrize("audience", ["Industrial procurement teams", "Family cycling groups"])
def test_local_unconfigured_shipped_apps_are_recognized_for_setup(monkeypatch, audience):
    from app.connections import toolkits

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(toolkits, "configured_toolkits", lambda: [])
    names = [(slug, entry["name"]) for slug, entry in toolkits.TOOLKITS.items()]
    for slug, name in [*names, ("x", connect.SERVICES["x"][0])]:
        message = f"For {audience}, connect my {name}"
        assert connect.service_from(message) == slug and connect.wants_connect(message)


@pytest.mark.parametrize("audience", ["Industrial procurement teams", "Family cycling groups"])
@pytest.mark.parametrize("ready", [False, True])
def test_local_mail_readiness_uses_saved_smtp_without_mailbox_writes(monkeypatch, audience, ready):
    from app.connections import hub

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(hub, "_smtp_card", lambda: {"ready": ready})
    monkeypatch.setattr("app.gtm.supymem.pipeline.mail_readiness", lambda *args: pytest.fail("Local SMTP readiness must not load or create mailbox records"))
    assert connect._is_connected(audience, "gmail") is ready


@pytest.mark.parametrize("audience", ["Industrial procurement teams", "Family cycling groups"])
def test_conn_regression_disconnect_uses_current_server_snapshot(monkeypatch, audience):
    from app.connections import hub, mailboxes

    calls = []
    monkeypatch.setattr(connect, "_scope", lambda venture_id: ("workspace", "owner@example.org"))
    monkeypatch.setattr(mailboxes, "enabled", lambda: False)
    monkeypatch.setattr(hub, "disconnect_accounts", lambda workspace_id, slug: {"owned": "current"})
    monkeypatch.setattr(hub, "disconnect_provider", lambda workspace_id, slug, **kwargs: calls.append((workspace_id, slug, kwargs)))
    monkeypatch.setattr("app.ventures.state_brief.invalidate_venture_state_brief", lambda *args: None)
    reply = connect.disconnect(audience, {"query": "Disconnect Gmail", "accounts": {"foreign": "stale"}})
    assert calls == [("workspace", "gmail", {"accounts": {"owned": "current"}})]
    assert reply["action_started"] and reply["kind"] != "confirmation"
    assert not any(action.get("confirmation") for action in reply["actions"])


@pytest.mark.parametrize("audience", ["Industrial procurement teams", "Family cycling groups"])
def test_conn_regression_gmail_explanation_never_routes_to_social(env, monkeypatch, audience):
    from app.chat.intent import classify_intent, split_speech_acts
    from app.chat.guard import _outreach_requests
    from app.connections import hub

    monkeypatch.setattr(connect, "_scope", lambda venture_id: ("workspace", "owner@example.org"))
    monkeypatch.setattr(hub, "connect_provider", lambda *args, **kwargs: pytest.fail("Explanation must not start OAuth"))
    question = f"Please explain how to connect Gmail for {audience}. I do not want to send anything."
    assert classify_intent(question) == "connections.connect"
    assert split_speech_acts(question) == [("connections.connect", question)]
    assert not _outreach_requests(question)
    assert _outreach_requests(question + " But send messages to everyone now.")
    reply = connect.handle("venture", question)
    assert "Gmail" in reply["body_md"] and "Connections" in reply["body_md"]
    assert "LinkedIn" not in reply["body_md"] and "Reddit" not in reply["body_md"]
    calls = []
    monkeypatch.setattr(connect, "_is_connected", lambda *args: False)
    monkeypatch.setattr(connect, "_save_watch", lambda *args, **kwargs: None)
    monkeypatch.setattr(hub, "connect_provider", lambda workspace_id, provider, **kwargs:
                        calls.append((workspace_id, provider)) or {"redirect_url": "https://example.org/authorize"})
    requested = connect.handle("venture", f"Connect Gmail. Explain the next step for {audience}.")
    assert calls == [("workspace", "gmail")]
    assert any(action.get("href") == "https://example.org/authorize" for action in requested["actions"])
    from app.chat import llm_router
    monkeypatch.setattr(llm_router, "route", lambda *args, **kwargs: pytest.fail("Service help keeps its deterministic route"))
    monkeypatch.setattr(hub, "connect_provider", lambda *args, **kwargs: pytest.fail("Explanation must not start OAuth"))
    with company_os_test_client(load_company_os_app().create_app()) as client:
        response = client.post(f"/ventures/{env}/chat", json={"message": question})
    assert response.status_code == 200, response.text
    assert "Gmail" in response.json()["body_md"] and "LinkedIn" not in response.json()["body_md"]


@pytest.mark.parametrize("text,slug", [
    (FOUNDER_MESSAGE, "gmail"), ("connect my gmial", "gmail"), ("gmail connect karo", "gmail"),
    ("mera gmail jodo", "gmail"), ("hook up my linkedin", "linkedin"), ("link linkdin please", "linkedin"),
    ("sign in to claude code", "claude"), ("connect discord", "discord"),
    ("set up hunter", "hunter"), ("connect my google ads", "google_ads"), ("log into codex", "codex"),
    ("conect reddit", "reddit"),
])
def test_connect_requests_are_recognised(text, slug):
    from app.chat.intent import classify_intent

    assert connect.wants_connect(text) and connect.service_from(text) == slug
    assert classify_intent(text) == "connections.connect"


@pytest.mark.parametrize("text", ["is gmail connected?", "connections status", "don't connect gmail",
                                  "gmail connect mat karo", "I connected my gmail yesterday", "send the emails"])
def test_status_questions_and_refusals_are_not_connect_requests(text):
    from app.chat.intent import classify_intent

    assert not connect.wants_connect(text)
    assert classify_intent(text) != "connections.connect"


@pytest.mark.parametrize("noun", ["task link", "receipt link", "report links"])
def test_saved_mac_receipt_links_do_not_connect_an_account(noun):
    from app.chat.intent import classify_intent

    question = ("For the latest LabHire Mac terminal task, was Git status independently verified, "
                f"how many credits were used, and what remains unproven? Give the {noun}.")
    assert classify_intent(question) == "assistant"
    assert classify_intent("How many credits are left?") == "credits.balance"


@pytest.mark.parametrize("command", ["Connect Codex", "Can you connect my Gmail", "Link Claude Code", "Sign in to Cursor"])
def test_explicit_connection_still_routes_to_connect_with_saved_mac_context(command):
    from app.chat.intent import classify_intent

    assert classify_intent(f"{command} and show the latest Mac task status. Give the task link.") == "connections.connect"


def test_founder_message_routes_to_connect_not_the_assistant():
    from app.chat.intent import classify_intent

    assert classify_intent(FOUNDER_MESSAGE) == "connections.connect"


@pytest.mark.parametrize("domain", ["warehouse forecasting for distributors", "meal planning for home cooks"])
@pytest.mark.parametrize("slug,name", [("cal", "Cal.com"), ("calendly", "Calendly")])
def test_booking_connection_actions_use_the_existing_oauth_flow(monkeypatch, domain, slug, name):
    from app.chat.intent import classify_intent
    from app.chat.navigation_map import in_chat
    from app.connections import hub

    calls = []
    monkeypatch.setattr(connect, "_scope", lambda venture_id: ("wsp_booking", "you@example.org"))
    monkeypatch.setattr(connect, "_is_connected", lambda workspace_id, provider: False)
    monkeypatch.setattr(connect, "_save_watch", lambda *args: None)
    monkeypatch.setattr(hub, "connect_provider", lambda workspace_id, provider: calls.append((workspace_id, provider)) or {
        "redirect_url": "https://example.org/authorize"})
    query = "Connect " + name
    assert connect.service_from(query) == slug and classify_intent(f"For {domain}, {query}") == "connections.connect"
    action = {"label": query, "action": "connections.connect", "payload": {"query": query}}
    assert in_chat([action]) == [action]
    reply = connect.handle("ven_booking", action["payload"]["query"])
    assert calls == [("wsp_booking", slug)] and reply["watch"] == slug
    assert any(item.get("href") == "https://example.org/authorize" and item["label"] == query for item in reply["actions"])


@pytest.fixture()
def env(tmp_path, monkeypatch, request):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/c.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_MEMORY_PATH", str(tmp_path / "mem.sqlite3"))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    monkeypatch.setenv("CONNECT_POLL_SECONDS", "0")
    main = load_company_os_app()
    from app import state as state_mod

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    with company_os_test_client(main.create_app()) as client:
        from app.core.local_mode import is_local
        if is_local():
            selected = client.post("/local/providers/sample", json={})
            assert selected.status_code == 200, selected.text
        made = client.post("/ventures", json={"raw_idea": getattr(request, "param", "a marketplace for restoring vintage synthesizers")})
        assert made.status_code == 201, made.text
        yield made.json()["id"]


@pytest.mark.parametrize("domain", ["warehouse forecasting for distributors", "meal planning for home cooks"])
def test_connection_intents_do_not_confuse_account_changes_with_status(domain):
    from app.chat import connect
    from app.chat.intent import classify_intent, split_speech_acts

    for text, expected in [
        ("Disconnect my Gmail.", "connections.disconnect"), ("remove gmail", "connections.disconnect"),
        ("unlink my LinkedIn", "connections.disconnect"), ("revoke my Reddit account", "connections.disconnect"),
        ("disconnect Google Calendar", "connections.disconnect"), ("connect my Gmail", "connections.connect"),
        ("reconnect my Gmail", "connections.connect"), ("reconnect google calendar", "connections.connect"),
        ("show my connections", "connections.status"), ("what is connected", "connections.status"),
        ("show connections", "connections.status"), ("list my integrations", "connections.status"),
    ]:
        for message in (text, f"For {domain}, {text}"):
            assert classify_intent(message) == expected, message
            assert split_speech_acts(message)[0][0] == expected, message
    for text in ("don't disconnect Gmail", "do not remove Reddit", "I disconnected Gmail yesterday"):
        assert not connect.wants_disconnect(text)
    from app.chat import llm_router

    decision = llm_router._validate({"acts": [{"intent": "connections.connect", "text": "connect gmail"}]}, "Disconnect my Gmail.")
    assert decision.acts == [("connections.disconnect", "Disconnect my Gmail.")]


@pytest.mark.parametrize("env", ["warehouse forecasting for distributors", "meal planning for home cooks"], indirect=True)
@pytest.mark.parametrize("provider", ["gmail", "google_calendar", "linkedin", "reddit"])
def test_requested_disconnect_revokes_once_and_preserves_other_tenants(env, provider, monkeypatch):
    import json
    from app import main
    from app.chat.execute import execute_action
    from app.content import composio_social
    from app.db.models import ContentAccount, GtmEmailAccount, Venture, Workspace
    from app.state import db
    from core.clock import utcnow

    now = utcnow()
    with db()() as session:
        workspace_id = session.get(Venture, env).workspace_id
        model = GtmEmailAccount if provider == "gmail" else ContentAccount
        fields = {"provider": "gmail", "email": "owner@fixture.example", "composio_user_id": f"gmail:{workspace_id}",
                  "connected_account_id": "ca_owned"} if provider == "gmail" else {
            "platform": provider, "external_account": "ca_owned", "handle": "owner@fixture.example",
            "extra_json": json.dumps({"auth_via": "composio", "connected_account_id": "ca_owned"})}
        session.add(model(id="account_owned", workspace_id=workspace_id, status="active", created_at=now, updated_at=now, **fields))
        foreign_fields = {**fields, "connected_account_id": "ca_foreign", "email": "other@fixture.example"} if provider == "gmail" else {
            **fields, "external_account": "ca_foreign", "extra_json": json.dumps({"auth_via": "composio", "connected_account_id": "ca_foreign"})}
        session.add(model(id="account_foreign", workspace_id="wsp_foreign", status="active", created_at=now, updated_at=now, **foreign_fields))
        if provider == "gmail":
            workspace = session.get(Workspace, workspace_id)
            session.add(Workspace(id="wsp_sibling", kind="venture", name="Another project", entry_mode="idea",
                owner_id=workspace.owner_id, created_at=now, updated_at=now))
            session.add(GtmEmailAccount(id="account_sibling", workspace_id="wsp_sibling", status="active",
                created_at=now, updated_at=now, **{**fields, "connected_account_id": "ca_sibling",
                    "composio_user_id": "gmail:wsp_sibling"}))
            session.add(GtmEmailAccount(id="account_smtp", workspace_id=workspace_id, provider="smtp", status="active",
                composio_user_id=f"smtp:{workspace_id}", connected_account_id=f"smtp:{workspace_id}", created_at=now, updated_at=now))
        session.commit()
    calls = []
    monkeypatch.setattr(composio_social, "_request", lambda method, path, **kwargs: calls.append((method, path, kwargs)) or {})
    client = company_os_test_client(main.create_app())
    response = client.post(f"/ventures/{env}/chat", json={"message": f"disconnect my {connect.SERVICES[provider][0]}"})
    assert response.status_code == 200, response.text
    prepared = response.json()
    assert prepared["kind"] == "text" and "disconnected" in prepared["body_md"]
    assert not any(action.get("confirmation") for action in prepared["actions"])
    payload = {"provider": provider, "accounts": {"account_owned": "ca_owned"}}
    body = {"action": "connections.disconnect", "payload": payload}
    for _ in range(2):
        result = client.post(f"/ventures/{env}/chat/confirm", json=body)
        assert result.status_code == 200, result.text
    assert calls == [("DELETE", "/connected_accounts/ca_owned", {"params": {"revoke_on_delete": "true"}})]
    from app.connections import hub

    with pytest.raises(ValueError):
        hub.disconnect_provider(workspace_id, provider, accounts={"account_foreign": "ca_foreign"})
    hub.disconnect_provider(workspace_id, provider, accounts=payload["accounts"])
    assert calls == [("DELETE", "/connected_accounts/ca_owned", {"params": {"revoke_on_delete": "true"}})]
    with db()() as session:
        assert session.get(model, "account_owned").status == "revoked"
        assert session.get(model, "account_foreign").status == "active"
        if provider == "gmail":
            assert session.get(GtmEmailAccount, "account_sibling").status == "active"
            assert session.get(GtmEmailAccount, "account_smtp").status == "active"
    if provider == "gmail":
        from app.gtm.supymem import composio_gmail, pipeline
        from app.core.local_mode import is_local

        if is_local():
            monkeypatch.setattr("app.gtm.local_mail.ensure_account", lambda session, workspace_id: session.get(GtmEmailAccount, "account_smtp"))
        monkeypatch.setattr(composio_gmail, "list_gmail_connections", lambda user_id: [{
            "id": "ca_owned", "user_id": f"gmail:{workspace_id}", "status": "ACTIVE"}])
        monkeypatch.setattr("app.content.oauth.callbacks.known_connections", lambda *args, **kwargs: [])
        pipeline.sync_gmail_accounts(workspace_id)
        with db()() as session:
            assert session.get(model, "account_owned").status == "revoked"
            expected_updated_at = session.get(model, "account_owned").updated_at.isoformat()
        from app.connections.mailboxes import MailboxUnavailable
        from app.credits.policy import get_policy, set_policy

        set_policy("feature_flags", {**(get_policy("feature_flags") or {}), "multi_gmail_v1": False}, "test")
        if is_local():
            hub.disconnect_mailbox(workspace_id, "account_owned", confirmed=True, expected_updated_at=expected_updated_at)
            with pytest.raises(ValueError, match="connection_confirmation_required"):
                hub.disconnect_mailbox(workspace_id, "account_owned", confirmed=False, expected_updated_at=expected_updated_at)
            with pytest.raises(ValueError, match="connection_changed_review_again"):
                hub.disconnect_mailbox(workspace_id, "account_owned", confirmed=True, expected_updated_at="stale")
            with pytest.raises(MailboxUnavailable, match="mailbox_unavailable"):
                hub.disconnect_mailbox(workspace_id, "account_smtp", confirmed=True, expected_updated_at=expected_updated_at)
            assert calls == [("DELETE", "/connected_accounts/ca_owned", {"params": {"revoke_on_delete": "true"}})]
        else:
            with pytest.raises(MailboxUnavailable, match="multi_gmail_disabled"):
                hub.disconnect_mailbox(workspace_id, "account_owned", confirmed=True, expected_updated_at=expected_updated_at)


@pytest.mark.parametrize("env", ["warehouse forecasting for distributors", "meal planning for home cooks"], indirect=True)
@pytest.mark.parametrize("provider", ["gmail", "linkedin"])
def test_revoke_failure_keeps_account_credentials_for_retry(env, provider, monkeypatch):
    import json
    from app.connections import hub
    from app.content import composio_social
    from app.db.models import ContentAccount, GtmEmailAccount, Venture
    from app.state import db
    from core.clock import utcnow

    now = utcnow()
    with db()() as session:
        workspace_id = session.get(Venture, env).workspace_id
        model = GtmEmailAccount if provider == "gmail" else ContentAccount
        fields = {"provider": "gmail", "composio_user_id": f"gmail:{workspace_id}", "connected_account_id": "ca_retry"} if provider == "gmail" else {
            "platform": provider, "extra_json": json.dumps({"auth_via": "composio", "connected_account_id": "ca_retry"})}
        session.add(model(id="account_retry", workspace_id=workspace_id, status="active", created_at=now, updated_at=now, **fields))
        session.commit()
    def fail(*args, **kwargs):
        raise RuntimeError("provider unavailable")
    monkeypatch.setattr(composio_social, "delete_connection", fail)
    with pytest.raises(RuntimeError, match="provider unavailable"):
        hub.disconnect_provider(workspace_id, provider, accounts={"account_retry": "ca_retry"})
    with db()() as session:
        account = session.get(model, "account_retry")
        assert account.status == "active"
        assert (account.connected_account_id if provider == "gmail" else json.loads(account.extra_json)["connected_account_id"]) == "ca_retry"
        if provider == "gmail":
            account.connected_account_id = "ca_reconnected"
        else:
            account.extra_json = json.dumps({"auth_via": "composio", "connected_account_id": "ca_reconnected"})
        session.commit()
    with pytest.raises(ValueError, match="connection_changed_review_again"):
        hub.disconnect_provider(workspace_id, provider, accounts={"account_retry": "ca_retry"})


@pytest.mark.parametrize("env", ["warehouse forecasting for distributors", "meal planning for home cooks"], indirect=True)
@pytest.mark.parametrize("provider", ["gmail", "google_calendar"])
def test_reconnect_starts_authorization_even_when_old_account_is_connected(env, provider, monkeypatch):
    from app.chat import connect
    from app.connections import hub

    calls = []
    monkeypatch.setattr(connect, "_is_connected", lambda *args: True)
    monkeypatch.setattr(hub, "connect_provider", lambda workspace, service: calls.append(service) or {
        "ok": True, "redirect_url": "https://connect.composio.dev/fixture"})
    assert "already connected" in connect.handle(env, f"connect {connect.SERVICES[provider][0]}")["body_md"]
    assert calls == []
    assert connect.handle(env, f"reconnect {connect.SERVICES[provider][0]}")["watch"] == provider
    assert calls == [provider]


@pytest.mark.parametrize("domain,address", [("warehouse teams", "dispatch@distribution.example"), ("home cooks", "recipes@kitchen.example")])
def test_connections_card_reads_hub_status_and_offers_matching_actions(domain, address, monkeypatch):
    from app.chat import connect, execute
    from app.connections import hub

    seen = []
    workspace_id = "wsp_" + address.split("@")[0]
    monkeypatch.setattr(connect, "_scope", lambda venture_id: (workspace_id, address))
    def status(workspace):
        seen.append(workspace)
        return {"order": ["gmail", "google_calendar", "linkedin", "reddit"], "connections": {"gmail": {"connected": True, "account_email": address, "ready": False},
            "google_calendar": {"connected": False}, "linkedin": {"connected": True, "needs_reconnect": True},
            "reddit": {"connected": True, "ready": True, "handle": address}}}
    monkeypatch.setattr(hub, "hub_status", status)
    card = execute._connections_status(domain)
    assert seen == [workspace_id]
    assert f"**Gmail**: Connected as {address}" in card["body_md"]
    assert "**Google Calendar**: Not connected" in card["body_md"]
    assert "**LinkedIn**: Needs reconnect" in card["body_md"]
    assert f"**Reddit**: Connected as {address}" in card["body_md"]
    assert [(action["label"], action["action"]) for action in card["actions"]] == [
        ("Disconnect Gmail", "connections.disconnect"), ("Connect Google Calendar", "connections.connect"),
        ("Reconnect LinkedIn", "connections.connect"), ("Disconnect Reddit", "connections.disconnect")]


@pytest.mark.parametrize("env", ["warehouse forecasting for distributors", "meal planning for home cooks"], indirect=True)
def test_calendar_uses_existing_managed_oauth_and_hub_status(env, monkeypatch):
    from app.connections import hub
    from app.content import composio_social
    from app.content.oauth import callbacks
    from app.db.models import Venture
    from app.state import db

    monkeypatch.setenv("COMPOSIO_API_KEY", "fixture")
    monkeypatch.setenv("COMPOSIO_GOOGLECALENDAR_AUTH_CONFIG_ID", "ac_calendar_fixture")
    monkeypatch.delenv("COMPOSIO_GOOGLECALENDAR_TOOLKIT_VERSION", raising=False)
    from app.core.local_mode import is_local
    if is_local():
        from app.core.local_secrets import update_settings
        update_settings("composio", {"auth_configs": {"googlecalendar": "ac_calendar_fixture"}}, {"api_key": "fixture"})
        update_settings("capabilities", {"connected_apps": True}, {})
    with db()() as session:
        workspace_id = session.get(Venture, env).workspace_id
    requests = []
    def request(method, path, **kwargs):
        requests.append((method, path, kwargs))
        return {"redirect_url": "https://connect.composio.dev/fixture", "connected_account_id": "ca_calendar_fixture"}
    monkeypatch.setattr(composio_social, "_request", request)
    started = hub.connect_provider(workspace_id, "google_calendar")
    assert started["ok"], started
    assert requests[0][2]["json_body"]["auth_config_id"] == "ac_calendar_fixture"
    assert "google_calendar" in callbacks.PLATFORMS
    composio_social.sync_workspace(workspace_id, platform="google_calendar", connections=[{
        "id": "ca_calendar_fixture", "platform": "google_calendar", "toolkit": "googlecalendar"}])
    calendar = hub.hub_status(workspace_id)["connections"]["google_calendar"]
    assert calendar["connected"] and calendar["ready"]
    from app.chat import connect

    assert "**Google Calendar**: Connected" in connect.status(env)["body_md"]
    assert "ca_calendar_fixture" not in connect.status(env)["body_md"]


def test_gmail_card_has_link_numbered_steps_and_real_navigation(env, monkeypatch):
    from app.chat.execute import execute_action
    from app.connections import hub

    link = "https://connect.composio.dev/link/lk_test"
    monkeypatch.setattr(connect, "_is_connected", lambda wid, slug: False)
    monkeypatch.setattr(hub, "connect_provider", lambda wid, slug, **kw: {"ok": True, "redirect_url": link})
    card = asyncio.run(execute_action(env, "connections.connect", payload={"query": FOUNDER_MESSAGE}))
    assert card["actions"][0] == {"label": "Connect Gmail", "href": link, "style": "primary"}
    assert card["actions"][1]["href"] == f"/connections?venture={env}"
    body = card["body_md"]
    for n in ("1. Click **Connect Gmail**", "2. Pick the Google account", "3. Click Allow", "4. Come back here"):
        assert n in body
    assert "Open Gmail authorization" in body and "afterwards click Refresh." in body and "Connections" in body
    assert "Integrations" not in body


@pytest.mark.hosted_only
def test_watch_posts_connected_once_and_clears_the_blocker(env, monkeypatch):
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    from app.chat.execute import execute_action
    from app.chat.store import list_messages
    from app.connections import hub
    from app.needs_you import service

    state = {"ok": False}
    monkeypatch.setattr(connect, "_is_connected", lambda wid, slug: state["ok"])
    monkeypatch.setattr(hub, "connect_provider", lambda wid, slug, **kw: {"ok": True, "redirect_url": "https://connect.composio.dev/link/x"})
    monkeypatch.setattr(hub, "sync_provider", lambda wid, slug: {"ok": True})
    asyncio.run(execute_action(env, "connections.connect", payload={"query": "connect gmail"}))
    assert connect.check(env, "gmail") == "waiting"
    monkeypatch.setenv("CONNECT_ESCALATE_MINUTES", "0")
    assert connect.check(env, "gmail") == "waiting"
    open_ = service.list_open(env)
    assert len(open_) == 1 and open_[0]["reason_code"] == "gmail_connect" and open_[0]["severity"] == "blocking"
    assert open_[0]["how_to_resume"].startswith("1. ")
    state["ok"] = True
    assert connect.check(env, "gmail") == "connected"
    assert connect.check(env, "gmail") == "gone"
    assert service.list_open(env) == []  # the blocker clears itself, reminders stop
    done = [m for m in list_messages(env, limit=30) if "Gmail connected" in m["body_md"]]
    assert len(done) == 1


@pytest.mark.parametrize("audience", ["Industrial procurement teams", "Family cycling groups"])
@pytest.mark.parametrize("provider_message", ["Hunter is not set up yet.", "Connections are unavailable for Hunter."])
def test_non_composio_service_gets_exact_navigation(env, monkeypatch, audience, provider_message):
    from app.chat.execute import execute_action
    from app.connections import hub
    from app.core.local_capabilities import blocked_message
    from app.core.local_mode import is_local

    monkeypatch.setattr(hub, "connect_provider", lambda wid, slug, **kw: {"ok": False, "needs_you": True, "message": provider_message})
    card = asyncio.run(execute_action(env, "connections.connect", payload={"query": f"set up hunter for {audience}"}))
    if is_local():
        # 77be6a460 distinguishes unsupported hosted connections from local Composio setup.
        assert blocked_message("hosted_connected_apps") in card["body_md"]
        assert not card["actions"]
    else:
        assert "Hunter could not be connected right now" in card["body_md"]
        assert "try again from Connections" in card["body_md"]
        assert card["actions"][0]["href"] == f"/connections?venture={env}"
    assert "Click Connect or Authorize" not in card["body_md"]
    coding = asyncio.run(execute_action(env, "connections.connect", payload={"query": "sign in to claude code"}))
    assert "Coding tools on this Mac" in coding["body_md"] and "Open Claude Code app" in coding["body_md"]
    assert "sign in inside that app or its CLI yourself" in coding["body_md"]
    assert "Supymem never types your password" in coding["body_md"]

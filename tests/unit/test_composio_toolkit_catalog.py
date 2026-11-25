"""Configured toolkit connections remain founder-driven and workspace-bound."""
from __future__ import annotations

from contextlib import contextmanager
from urllib.parse import parse_qs, urlencode, urlparse

import pytest
from starlette.requests import Request

from tests.company_os_harness import load_company_os_app

DOMAINS = ("Inventory planning for wholesalers", "Singing lessons for families")


@pytest.fixture()
def scopes(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/toolkits.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_MEMORY_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("SANDBOX_MODE", "true")
    monkeypatch.setenv("COMPOSIO_API_KEY", "test-only")
    monkeypatch.setenv("CONTENT_OAUTH_BASE_URL", "https://api.example.test")
    monkeypatch.setenv("CONSOLE_URL", "https://console.example.test")
    monkeypatch.delenv("COMPOSIO_CONTENT_USER_ID", raising=False)
    load_company_os_app()
    from app.connections import toolkits
    from app.db.models import Person, Venture, Workspace, WorkspaceMember
    from app.state import db, reset_db_factory
    from core.clock import utcnow

    reset_db_factory()
    for entry in toolkits.TOOLKITS.values():
        monkeypatch.delenv(entry["auth_config_env"], raising=False)
    monkeypatch.setattr("app.gtm.supymem.pipeline.mail_readiness", lambda _wid: {"connected": False, "ready": False})
    monkeypatch.setattr("app.community.service.discord_status", lambda _wid: {"connected": False, "ready": False})
    monkeypatch.setattr("app.marketing.media.probes.google_ads_available", lambda: {"ok": False, "missing": []})
    monkeypatch.setattr("app.marketing.media.probes.meta_ads_available", lambda: {"ok": False, "missing": []})

    def no_network(*args, **kwargs):
        raise AssertionError("Unexpected live provider request")

    monkeypatch.setattr("httpx.Client.request", no_network)
    result, now = [], utcnow()
    with db()() as session:
        for index, domain in enumerate(DOMAINS):
            owner, workspace, venture = f"person_{index}", f"workspace_{index}", f"venture_{index}"
            session.add(Person(id=owner, email=f"you{index}@example.test", display_name="You", created_at=now))
            session.add(Workspace(id=workspace, owner_id=owner, name=domain, kind="company", entry_mode="idea", created_at=now, updated_at=now))
            session.add(WorkspaceMember(id=f"member_{index}", workspace_id=workspace, person_id=owner, role="owner", can_approve=1, created_at=now))
            session.add(Venture(id=venture, workspace_id=workspace, name=domain, raw_idea=domain, stage="draft", created_at=now, updated_at=now))
            result.append({"owner": owner, "workspace": workspace, "venture": venture, "domain": domain})
        session.commit()
    yield result
    reset_db_factory()


@pytest.fixture()
def local_connections(scopes, monkeypatch):
    from app.core.local_secrets import update_settings
    from app.connections import toolkits

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    update_settings("composio", {"auth_configs": {
        **{slug: "ac_" + slug for slug in toolkits.TOOLKITS},
        "googlesheets": "ac_sheets", "googledocs": "ac_docs", "googlecalendar": "ac_calendar",
        "notion": "ac_notion", "linkedin": "ac_linkedin", "twitter": "ac_twitter",
    }}, {"api_key": "saved-test-key"})
    update_settings("capabilities", {"connected_apps": True}, {})
    return scopes


@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.parametrize("slug", ["GMAIL_FETCH_EMAILS", "GOOGLESHEETS_GET_SPREADSHEET",
    "GOOGLEDOCS_GET_DOCUMENT_BY_ID", "GOOGLECALENDAR_FIND_EVENT", "NOTION_FETCH_PAGE",
    "GOOGLEDRIVE_GET_FILE", "CAL_GET_BOOKINGS", "CALENDLY_LIST_EVENTS", "HUBSPOT_RETRIEVE_CONTACTS",
    "TYPEFORM_GET_FORM_RESPONSES", "STRIPE_LIST_CHARGES", "GOOGLE_ANALYTICS_RUN_REPORT",
    "GOOGLESEARCHCONSOLE_QUERY_SEARCH_ANALYTICS", "SLACK_FETCH_CONVERSATION_HISTORY", "GITHUB_LIST_REPOSITORY_ISSUES"])
def test_local_tool_routing_uses_saved_composio_key_not_mail_for_other_apps(local_connections, monkeypatch, domain, slug):
    import httpx
    from app.gtm import local_mail
    from app.gtm.supymem import composio_gmail

    scope = next(scope for scope in local_connections if scope["domain"] == domain)
    calls, mail = [], []
    arguments = {"query": domain}
    monkeypatch.setattr(local_mail, "check_binding", lambda *args: mail.append(args))
    monkeypatch.setattr(local_mail, "read_tool", lambda *args: {"items": [domain]})

    def request(client, method, url, **kwargs):
        calls.append((method, url, kwargs))
        return httpx.Response(200, json={"successful": True, "data": {"items": [domain]}})

    monkeypatch.setattr(httpx.Client, "request", request)
    reply = composio_gmail.execute_tool(slug, user_id=scope["owner"],
        connected_account_id="ca_owned", arguments=arguments)
    assert reply == {"successful": True, "data": {"items": [domain]}}
    if slug.startswith("GMAIL_"):
        assert mail == [(scope["owner"], "ca_owned")] and not calls
    else:
        assert not mail and len(calls) == 1
        assert calls[0][0:2] == ("POST", composio_gmail.COMPOSIO_BASE + "/tools/execute/" + slug)
        assert calls[0][2]["headers"]["x-api-key"] == "saved-test-key"
        assert calls[0][2]["json"]["arguments"] == arguments
        assert calls[0][2]["json"]["user_id"] == scope["owner"]


@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.parametrize("failure", ["transport", "http", "tool"])
def test_local_provider_failures_never_expose_your_key_and_give_the_next_step(local_connections, monkeypatch, domain, failure):
    import httpx
    from app.gtm.supymem import composio_gmail

    def request(client, method, url, **kwargs):
        if failure == "transport":
            raise httpx.ConnectError("saved-test-key")
        return httpx.Response(500 if failure == "http" else 200,
            json={"successful": False, "error": "saved-test-key"})

    monkeypatch.setattr(httpx.Client, "request", request)
    with pytest.raises(RuntimeError) as error:
        composio_gmail.execute_tool("GOOGLESHEETS_GET_SPREADSHEET", user_id="usr_" + domain,
            connected_account_id="ca_owned", arguments={})
    assert "saved-test-key" not in str(error.value)
    assert "Google Sheets" in str(error.value) and "Keys and connections" in str(error.value)


@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.parametrize("setting", ["api_key", "auth_configs", "connected_apps"])
def test_local_connection_blockers_name_the_app_field_and_settings_screen(local_connections, domain, setting):
    from app.api.local_settings import FIELD_INFO
    from app.connections import toolkits
    from app.core.local_capabilities import blocked_message
    from app.core.local_secrets import update_settings
    from app.gtm.supymem import composio_gmail

    if setting == "api_key":
        update_settings("composio", {}, {"api_key": ""})
    elif setting == "auth_configs":
        update_settings("composio", {"auth_configs": {}}, {})
    else:
        update_settings("capabilities", {"connected_apps": False}, {})
    with pytest.raises(RuntimeError) as error:
        composio_gmail.execute_tool("GOOGLESHEETS_GET_SPREADSHEET", user_id="usr_" + domain,
            connected_account_id="ca_owned", arguments={})
    message = str(error.value)
    name = toolkits.TOOLKITS["googlesheets"]["name"]
    assert name in message
    if setting == "connected_apps":
        assert message == f"For {name}: {blocked_message('connected_apps')}"
    else:
        assert "Settings > Keys and connections > Your connected apps" in message
        if setting == "api_key":
            assert FIELD_INFO["composio"]["api_key"][0] in message
        else:
            assert f"{name} authorization ID" in message and FIELD_INFO["composio"]["auth_configs"][0] in message
            assert "Connections" in message and "reconnect" in message
    assert "composio." not in message and "auth_configs" not in message
    assert "test-only" not in message and "saved-test-key" not in message


@pytest.mark.parametrize("domain", DOMAINS)
def test_local_nonmail_reconnect_updates_the_toolkit_receipt_not_your_mailbox(local_connections, monkeypatch, domain):
    import httpx
    from app.connections import composio_migration, toolkits
    from app.db.models import ContentOAuthState
    from app.gtm.supymem import composio_gmail
    from app.state import db
    from core.clock import utcnow

    scope = next(scope for scope in local_connections if scope["domain"] == domain)
    now = utcnow()
    with db()() as session:
        session.add(ContentOAuthState(id="notion_receipt", workspace_id=scope["workspace"], platform="notion",
            actor_subject=scope["owner"], auth_method="composio", provider_user_id=scope["owner"],
            auth_config_id="ac_notion", connected_account_id="ca_notion", outcome="connected",
            redirect_uri="http://127.0.0.1:3300/callback", consumed_at=now, expires_at=now, created_at=now))
        session.commit()

    def no_mail(connection_id):
        raise AssertionError("A document connection must not pause your mailbox")

    monkeypatch.setattr(composio_migration, "mark_gmail_reconnect", no_mail)
    monkeypatch.setattr(httpx.Client, "request", lambda *args, **kwargs:
        httpx.Response(404, json={"error": "Connected account not found"}))
    with pytest.raises(composio_gmail.ReconnectRequired, match="Notion.*Connections"):
        composio_gmail.execute_tool("NOTION_FETCH_PAGE", user_id=scope["owner"],
            connected_account_id="ca_notion", arguments={"page_id": "selected_page"})
    assert toolkits.accounts(scope["workspace"], "notion")[0]["outcome"] == "needs_reconnect"


@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.parametrize("failure", ["transport", "http", "authorization_http", "authorization_tool"])
def test_local_social_provider_failures_preserve_delivery_and_authentication_state(local_connections, monkeypatch, domain, failure):
    import httpx
    from app.content import composio_social

    monkeypatch.setattr(composio_social, "dry_run_enabled", lambda: False)

    def request(client, method, url, **kwargs):
        if failure == "transport":
            raise httpx.ConnectError("saved-test-key")
        if failure.startswith("authorization"):
            return httpx.Response(400 if failure.endswith("http") else 200,
                json={"successful": False, "error": "invalid_grant: Token has expired saved-test-key"})
        return httpx.Response(503, json={"error": "saved-test-key"})

    monkeypatch.setattr(httpx.Client, "request", request)
    reply = composio_social.publish(platform="linkedin", body=domain,
        connected_account_id="ca_owned", composio_user_id="usr_" + domain, author_urn="urn:li:person:test")
    assert not reply.ok and reply.fatal is failure.startswith("authorization")
    assert "saved-test-key" not in reply.error and "connections" in reply.error.lower()


@pytest.mark.parametrize("domain", DOMAINS)
def test_local_social_uses_saved_key_and_toolkit_auth_config_without_env_pin(local_connections, monkeypatch, domain):
    import httpx
    from app.content import composio_social
    from app.connections import toolkits

    scope = next(scope for scope in local_connections if scope["domain"] == domain)
    for platform in ("google_calendar", "linkedin", "x"):
        monkeypatch.delenv(composio_social.AUTH_CONFIG_ENV[platform], raising=False)
        monkeypatch.delenv(f"COMPOSIO_{composio_social.toolkit_for(platform).upper()}_TOOLKIT_VERSION", raising=False)
        assert composio_social.connectable(platform)
    assert composio_social.ensure_auth_config("x") == "ac_twitter"
    assert composio_social.ensure_auth_config("google_calendar") == "ac_calendar"
    assert toolkits.auth_config_id("googlesheets") == "ac_sheets"
    calls = []

    def request(client, method, url, **kwargs):
        calls.append(kwargs)
        return httpx.Response(200, json={"successful": True, "data": {"id": "post_owned"}})

    monkeypatch.setattr(httpx.Client, "request", request)
    composio_social.execute_tool("LINKEDIN_CREATE_LINKED_IN_POST", user_id="content:" + scope["workspace"],
        connected_account_id="ca_owned", arguments={"text": domain})
    assert len(calls) == 1 and calls[0]["headers"]["x-api-key"] == "saved-test-key"
    assert calls[0]["json"]["arguments"] == {"text": domain}


@pytest.mark.parametrize("domain", DOMAINS)
def test_local_connections_page_oauth_returns_to_loopback_with_bound_owner_and_saved_auth(local_connections, monkeypatch, domain):
    import httpx
    from app.connections import hub, toolkits
    from app.content.oauth import callbacks
    from app.credits import policy
    from app.tenancy import bind

    scope = next(scope for scope in local_connections if scope["domain"] == domain)
    monkeypatch.setenv("CONTENT_OAUTH_BASE_URL", "http://127.0.0.1:3300")
    monkeypatch.setenv("CONSOLE_URL", "http://127.0.0.1:3100")
    monkeypatch.delenv("SUPYMEM_PUBLIC_URL", raising=False)
    original_policy = policy.get_policy
    monkeypatch.setattr(policy, "get_policy", lambda name, default=None:
        {} if name == "feature_flags" else original_policy(name, default))
    calls = []

    def request(client, method, url, **kwargs):
        assert kwargs["headers"]["x-api-key"] == "saved-test-key"
        calls.append((method, url, kwargs))
        if method == "POST":
            payload = kwargs["json"]
            assert payload["auth_config_id"] == "ac_notion" and payload["user_id"] == scope["owner"]
            assert payload["callback_url"].startswith("http://127.0.0.1:3300/api/v1/content/oauth/notion/callback?")
            return httpx.Response(200, json={"connected_account_id": "ca_notion",
                "redirect_url": "https://connect.composio.dev/test"})
        return httpx.Response(200, json={"id": "ca_notion", "status": "ACTIVE", "toolkit": {"slug": "notion"},
            "auth_config": {"id": "ac_notion"}, "user_id": scope["owner"]})

    monkeypatch.setattr(httpx.Client, "request", request)
    with bind(workspace_id=scope["workspace"], venture_id=scope["venture"], actor=scope["owner"]):
        started = hub.connect_provider(scope["workspace"], "notion", return_to="/connections")
        assert started["ok"] is True, started
        callback = urlparse(calls[0][2]["json"]["callback_url"])
        response = callbacks.callback_response("notion", Request({"type": "http", "method": "GET",
            "path": callback.path, "query_string": callback.query.encode(), "headers": []}))
        assert response.status_code == 200
        assert b'href="http://127.0.0.1:3100/connections?provider=notion&amp;oauth=connected"' in response.body
        accounts = toolkits.accounts(scope["workspace"], "notion")
        assert len(accounts) == 1 and accounts[0]["provider_user_id"] == scope["owner"]
    assert len(calls) == 2 and all("/tools/execute/" not in url for _, url, _ in calls)


@pytest.mark.parametrize("domain", DOMAINS)
def test_managed_oauth_uses_link_and_preserves_callback_identity_and_metering(scopes, monkeypatch, domain):
    from app.content import composio_social
    from app.content.oauth import attempts, callbacks
    from app.credits import metered
    from app.db.models import ContentOAuthState
    from app.gtm.supymem import composio_gmail
    from app.state import db
    from app.tenancy import bind

    scope = next(scope for scope in scopes if scope["domain"] == domain)
    calls, charges = [], []
    charge_sync = metered.charge_sync

    @contextmanager
    def charge(**kwargs):
        charges.append(kwargs)
        with charge_sync(**kwargs) as meter:
            yield meter

    def request(method, path, **kwargs):
        assert (method, path) == ("POST", "/connected_accounts/link")
        payload = kwargs.get("json_body") or kwargs["json"]
        calls.append(payload)
        return {"data": {"connected_account_id": f"ca_{len(calls)}", "redirect_url": "https://connect.composio.dev/test"}}

    monkeypatch.setattr(metered, "charge_sync", charge)
    monkeypatch.setattr(composio_social, "_request", request)
    monkeypatch.setattr(composio_gmail, "_request", request)
    monkeypatch.setenv("COMPOSIO_GMAIL_AUTH_CONFIG_ID", "ac_test_gmail")
    for platform in ("google_calendar", "linkedin", "reddit"):
        monkeypatch.setenv(composio_social.AUTH_CONFIG_ENV[platform], f"ac_test_{platform}")
        monkeypatch.setenv(f"COMPOSIO_{composio_social.toolkit_for(platform).upper()}_TOOLKIT_VERSION", "20260922_00")
    with bind(workspace_id=scope["workspace"], venture_id=scope["venture"], actor=scope["owner"]):
        for platform in ("gmail", "google_calendar", "linkedin", "reddit"):
            return_to = f"/connections?workspace={scope['workspace']}&venture={scope['venture']}"
            started = callbacks.start_managed(workspace_id=scope["workspace"], platform=platform, return_to=return_to)
            payload = calls[-1]
            expected_user = f"{'gmail' if platform == 'gmail' else 'content'}:{scope['workspace']}"
            assert payload["user_id"] == expected_user and "@" not in payload["user_id"]
            assert payload["auth_config_id"] == f"ac_test_{platform}"
            assert payload["allow_multiple"] is (platform == "gmail")
            callback = urlparse(payload["callback_url"])
            assert parse_qs(callback.query) == {"supymem_state": [started["state"]]}
            assert callback.path == ("/api/v1/ai-gtm/gmail/callback" if platform == "gmail" else f"/api/v1/content/oauth/{platform}/callback")
            with db()() as session:
                receipt = session.get(ContentOAuthState, attempts.digest(started["state"]))
                assert receipt.provider_user_id == expected_user
                assert receipt.connected_account_id == f"ca_{len(calls)}"
                assert receipt.return_to == "https://console.example.test" + return_to
    assert len(charges) == len(calls) == 4
    assert all(charge["workspace_id"] == scope["workspace"] and charge["purpose"] == "connections.connect"
               and charge["provider"] == "composio" and charge["estimate_micro"] == 0 for charge in charges)


@pytest.mark.parametrize("domain", DOMAINS)
def test_catalog_is_opt_in_and_connections_complete_sync_and_disconnect_without_tool_execution(scopes, monkeypatch, domain):
    from app.connections import hub, toolkits
    from app.content import composio_social
    from app.content.oauth import attempts, callbacks, flow
    from app.db.models import ContentAccount
    from app.state import db
    from app.tenancy import bind
    from sqlalchemy import select

    scope = next(scope for scope in scopes if scope["domain"] == domain)
    foreign = next(other for other in scopes if other != scope)
    calls, remote, fail_delete = [], {}, False
    monkeypatch.setenv("COMPOSIO_CONTENT_USER_ID", "legacy-local-identity@example.test")

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if (method, path) == ("POST", "/connected_accounts/link"):
            payload = kwargs["json_body"]
            slug = payload["auth_config_id"].removeprefix("ac_test_")
            account_id = f"ca_{slug}_{scope['workspace']}"
            assert payload["user_id"] == f"content:{scope['workspace']}"
            remote[account_id] = {"id": account_id, "status": "ACTIVE", "toolkit": {"slug": slug},
                                  "auth_config": {"id": payload["auth_config_id"]}, "experimental": {"account_type": "PRIVATE"}}
            return {"connected_account_id": account_id, "redirect_url": "https://connect.composio.dev/test"}
        account_id = path.removeprefix("/connected_accounts/")
        assert account_id in remote
        if method == "GET":
            return remote[account_id]
        assert method == "DELETE" and kwargs["params"] == {"revoke_on_delete": "true"}
        if fail_delete:
            raise composio_social.ComposioSocialError("http_error", "Provider temporarily unavailable", status_code=503)
        return {}

    monkeypatch.setattr(composio_social, "_request", request)
    initial = hub.hub_status(scope["workspace"])
    assert not toolkits.configured_toolkits()
    assert not set(toolkits.TOOLKITS).intersection(initial["order"])
    assert hub.connect_provider(scope["workspace"], "notion")["ok"] is False
    assert calls == []
    assert toolkits.TOOLKITS["stripe"]["read_only"] is True

    with bind(workspace_id=scope["workspace"], venture_id=scope["venture"], actor=scope["owner"]):
        for slug, entry in toolkits.TOOLKITS.items():
            assert entry["slug"] == slug and entry["goals"] and "you" in entry["why"]
            assert "—" not in entry["why"]
            monkeypatch.setenv(entry["auth_config_env"], "  ")
            assert not composio_social.connectable(slug)
            monkeypatch.setenv(entry["auth_config_env"], f"ac_test_{slug}")
            configured = hub.hub_status(scope["workspace"])
            assert configured["order"] == initial["order"] + [slug]
            assert configured["connections"][slug]["connected"] is False
            assert calls == [] or calls[-1][0] == "DELETE"
            started = hub.connect_provider(scope["workspace"], slug, return_to=f"/connections?workspace={scope['workspace']}&venture={scope['venture']}")
            assert started["ok"] is True
            payload = calls[-1][2]["json_body"]
            state = parse_qs(urlparse(payload["callback_url"]).query)["supymem_state"][0]
            response = callbacks.callback_response(slug, Request({"type": "http", "query_string": urlencode({"supymem_state": state}).encode()}))
            assert response.status_code == 303
            target = urlparse(response.headers["location"])
            assert parse_qs(target.query) == {"workspace": [scope["workspace"]], "venture": [scope["venture"]], "provider": [slug], "oauth": ["connected"]}
            receipt_id = attempts.digest(state)
            count = len(calls)
            assert callbacks.known_connections(scope["workspace"]) == []
            card = hub.hub_status(scope["workspace"])["connections"][slug]
            assert len(calls) == count
            assert card["connected"] and card["ready"]
            assert card["note"] == entry["why"] and card["experiment_goals"] == entry["goals"]
            assert card["accounts"] == [{"id": receipt_id}]
            assert hub.hub_status(foreign["workspace"])["connections"][slug]["connected"] is False
            assert hub.sync_provider(scope["workspace"], slug)["ok"] is True
            account_id = payload["auth_config_id"].replace("ac_test_", "ca_") + "_" + scope["workspace"]
            remote[account_id]["user_id"] = f"content:{foreign['workspace']}"
            assert hub.sync_provider(scope["workspace"], slug)["ok"] is True
            unverified = hub.hub_status(scope["workspace"])["connections"][slug]
            assert unverified["needs_reconnect"] and not unverified["ready"]
            remote[account_id].pop("user_id")
            assert hub.sync_provider(scope["workspace"], slug)["ok"] is True
            reconnected = hub.connect_provider(scope["workspace"], slug)
            assert reconnected["ok"] is True
            state = parse_qs(urlparse(calls[-1][2]["json_body"]["callback_url"]).query)["supymem_state"][0]
            callbacks.callback_response(slug, Request({"type": "http", "query_string": urlencode({"supymem_state": state}).encode()}))
            receipt_id = attempts.digest(state)
            assert hub.hub_status(scope["workspace"])["connections"][slug]["accounts"] == [{"id": receipt_id}]
            with bind(workspace_id=foreign["workspace"], venture_id=foreign["venture"], actor=foreign["owner"]):
                with pytest.raises(attempts.OAuthError, match="not connected"):
                    flow.revoke_account(receipt_id, workspace_id=foreign["workspace"])
            fail_delete = True
            with pytest.raises(composio_social.ComposioSocialError, match="temporarily unavailable"):
                flow.revoke_account(receipt_id, workspace_id=scope["workspace"])
            assert hub.hub_status(scope["workspace"])["connections"][slug]["connected"] is True
            fail_delete = False
            assert flow.revoke_account(receipt_id, workspace_id=scope["workspace"])["status"] == "revoked"
            count = len(calls)
            assert flow.revoke_account(receipt_id, workspace_id=scope["workspace"])["status"] == "revoked"
            assert len(calls) == count
            assert hub.hub_status(scope["workspace"])["connections"][slug]["connected"] is False
            monkeypatch.delenv(entry["auth_config_env"])
            assert slug not in hub.hub_status(scope["workspace"])["connections"]
    with db()() as session:
        assert session.scalar(select(ContentAccount.id)) is None
    assert all(path.startswith("/connected_accounts/") for _, path, _ in calls)


@pytest.mark.parametrize("domain", DOMAINS)
def test_local_hub_requires_your_connection_settings_and_defaults_to_smtp(scopes, monkeypatch, domain):
    from app.community import composio_discord
    from app.connections import hub, toolkits
    from app.content import composio_social
    from app.gtm.supymem import composio_gmail
    from app.core.local_secrets import update_settings
    from app.connections import local_config

    scope = next(scope for scope in scopes if scope["domain"] == domain)
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(local_config, "enabled", lambda name: name in {"connected_apps", "send_email"})
    providers = (*composio_social.AUTH_CONFIG_ENV, "discord")
    auth_names = (composio_gmail.AUTH_CONFIG_ENV, *composio_social.AUTH_CONFIG_ENV.values(),
                  *composio_discord.AUTH_CONFIG_ENV_ALIASES)
    for name in auth_names:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("app.content.oauth.flow.list_accounts", lambda _wid: [
        {"platform": platform, "status": "active"} for platform in composio_social.AUTH_CONFIG_ENV
    ])
    monkeypatch.setattr(hub, "_saved_discord_status", lambda _wid: {"connected": True})
    monkeypatch.setattr("app.gtm.supymem.pipeline.mail_readiness", lambda _wid: {"connected": True, "ready": True})
    update_settings("smtp", {"host": "mail.example.test", "user": "you@example.test", "sender_name": domain, "postal_address": "Your office address"}, {"password": "test-mail-password"})

    mail = hub.connect_provider(scope["workspace"], "gmail")
    assert mail["ok"] and mail["provider"] == "smtp"
    assert not mail.get("authorize_url") and not mail.get("redirect_url")
    for has_key in (False, True):
        update_settings("composio", {}, {"api_key": "test-only" if has_key else ""})
        status = hub.hub_status(scope["workspace"])
        assert status["order"][0] == status["email_provider"] == "smtp"
        assert status["connections"]["smtp"]["configured"] is True
        for provider in providers:
            card = status["connections"][provider]
            assert not card["ready"] and not card["connectable"]
            assert card["missing"] and "Keys and connections" in card["hint"]
            assert provider in status["order"]
            for operation in (hub.connect_provider, hub.sync_provider):
                result = operation(scope["workspace"], provider)
                assert not result["ok"] and result["needs_you"] and result["missing"]
                assert "Keys and connections" in result["message"] and "COMPOSIO_" not in result["message"]
        assert "test-only" not in str(status)
        assert "mail.example.test" not in str(status) and "you@example.test" not in str(status)
        refresh = hub.refresh_discord_status(scope["workspace"])
        assert not refresh["ok"] and not refresh["connected"] and refresh["needs_you"] and refresh["missing"]
    monkeypatch.setenv(toolkits.TOOLKITS["stripe"]["auth_config_env"], "test-auth")
    update_settings("composio", {}, {"api_key": ""})
    assert toolkits.auth_config_id("stripe") == ""
    assert "stripe" not in toolkits.configured_toolkits()
    assert toolkits.accounts(scope["workspace"], "stripe") == []


@pytest.mark.parametrize("domain", DOMAINS)
def test_local_configured_connections_keep_managed_authorization(scopes, monkeypatch, domain):
    from app.community import composio_discord
    from app.connections import hub, toolkits
    from app.content import composio_social
    from app.content.oauth import callbacks
    from app.gtm.supymem import composio_gmail
    from app.core.local_secrets import update_settings
    from app.connections import local_config

    scope = next(scope for scope in scopes if scope["domain"] == domain)
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(local_config, "enabled", lambda name: name == "connected_apps")
    monkeypatch.setenv("SUPYMEM_PUBLIC_URL", "https://your-app.example.test")
    calls = []

    def start_managed(**kwargs):
        calls.append(kwargs)
        return {"redirect_url": "https://connect.example.test/authorize"}

    monkeypatch.setattr(callbacks, "start_managed", start_managed)
    monkeypatch.setattr(composio_social, "connectable", lambda _platform: True)
    configs = {}
    for provider, env_name in (*composio_social.AUTH_CONFIG_ENV.items(),
                               ("notion", toolkits.TOOLKITS["notion"]["auth_config_env"])):
        configs[local_config.toolkit_slug(provider)] = "your-auth-config"
        update_settings("composio", {"auth_configs": configs}, {"api_key": "test-only"})
        result = hub.connect_provider(scope["workspace"], provider, return_to="/connections")
        assert result["ok"] is True and result["auth_via"] == "composio"
        assert calls[-1]["workspace_id"] == scope["workspace"] and calls[-1]["platform"] == provider
    configs[composio_discord.TOOLKIT_SLUG] = "your-discord-config"
    update_settings("composio", {"auth_configs": configs}, {})
    monkeypatch.setattr(composio_discord, "start_connect", lambda _wid, **kwargs:
        calls.append(kwargs) or {"redirect_url": "https://connect.example.test/authorize"})
    assert hub.connect_provider(scope["workspace"], "discord")["ok"] is True
    assert calls[-1]["callback"] == "https://your-app.example.test/connections?connected=discord"


@pytest.mark.parametrize("domain", DOMAINS)
def test_local_bulk_sync_only_checks_apps_you_configured(scopes, monkeypatch, domain):
    from app.connections import hub
    from app.content import composio_social
    from app.tenancy import bind
    from app.core.local_secrets import update_settings
    from app.connections import local_config

    scope = next(scope for scope in scopes if scope["domain"] == domain)
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(local_config, "enabled", lambda name: name == "connected_apps")
    for env_name in composio_social.AUTH_CONFIG_ENV.values():
        monkeypatch.delenv(env_name, raising=False)
    platform = next(iter(composio_social.AUTH_CONFIG_ENV))
    update_settings("composio", {"auth_configs": {local_config.toolkit_slug(platform): "your-auth-config"}}, {"api_key": "test-only"})
    checked = []
    monkeypatch.setattr(composio_social, "sync_workspace", lambda _wid, **kwargs:
                        checked.append(kwargs["platform"]) or [])
    with bind(workspace_id=scope["workspace"], venture_id=scope["venture"], actor=scope["owner"]):
        assert hub.sync_provider(scope["workspace"], "content")["ok"] is True
    assert checked == [platform]


@pytest.mark.parametrize("domain", DOMAINS)
def test_local_discord_auth_config_uses_saved_settings_without_an_env_pin(local_connections, monkeypatch, domain):
    from app.community import composio_discord
    from app.connections import local_config
    from app.core.local_secrets import update_settings

    scope = next(scope for scope in local_connections if scope["domain"] == domain)
    auth_config = "ac_discord_" + scope["owner"]
    for name in composio_discord.AUTH_CONFIG_ENV_ALIASES:
        monkeypatch.delenv(name, raising=False)
    update_settings("composio", {"auth_configs": {local_config.toolkit_slug("discord"): auth_config}}, {})
    assert composio_discord.ensure_auth_config() == auth_config
    update_settings("composio", {"auth_configs": {}}, {})
    with pytest.raises(composio_discord.DiscordBotError, match="Keys and connections"):
        composio_discord.ensure_auth_config()


@pytest.mark.parametrize("domain", DOMAINS)
def test_local_provider_settings_and_managed_origins_use_your_configuration(scopes, monkeypatch, domain):
    from functools import partial

    from app.community import composio_discord
    from app.content import composio_social
    from app.content.oauth import callbacks, flow
    from app.content.oauth.attempts import OAuthError
    from app.gtm.supymem import composio_gmail
    from app.core.local_secrets import update_settings
    from app.connections import local_config

    scope = next(scope for scope in scopes if scope["domain"] == domain)
    for env_name in composio_discord.AUTH_CONFIG_ENV_ALIASES:
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(local_config, "enabled", lambda name: name == "connected_apps")
    monkeypatch.setenv("SUPYMEM_PUBLIC_URL", "https://your-app.example.test")
    assert flow.public_base_url() == flow.console_origin() == "https://your-app.example.test"
    resolvers = [("gmail", composio_gmail.ensure_auth_config),
                 ("discord", composio_discord.ensure_auth_config)]
    resolvers.extend((platform, partial(composio_social.ensure_auth_config, platform))
                     for platform in composio_social.AUTH_CONFIG_ENV)
    for platform, resolve in resolvers:
        update_settings("composio", {"auth_configs": {local_config.toolkit_slug(platform): "your-auth-config"}}, {"api_key": "test-only"})
        assert resolve() == "your-auth-config"
        if platform == "discord":
            assert composio_discord.api_key() == "test-only"
            assert composio_discord.auth_config_id_from_env() == "your-auth-config"
            assert composio_discord.configured()
            assert composio_discord._headers()["x-api-key"] == "test-only"
            assert composio_discord.callback_url() == "https://your-app.example.test/community"
        update_settings("composio", {}, {"api_key": ""})
        with pytest.raises(RuntimeError, match="Keys and connections"):
            resolve()
        if platform == "discord":
            with pytest.raises(RuntimeError, match="Keys and connections"):
                composio_discord.start_connect(scope["workspace"])
        update_settings("composio", {"auth_configs": {}}, {"api_key": "test-only"})
        with pytest.raises(RuntimeError, match="Keys and connections"):
            resolve()
    with pytest.raises(OAuthError, match="Keys and connections"):
        callbacks.start_managed(workspace_id=scope["workspace"], platform="gmail")
    monkeypatch.setenv("SUPYMEM_PUBLIC_URL", "https://you:secret@example.test")
    for origin in (flow.public_base_url, flow.console_origin):
        with pytest.raises(OAuthError, match="local settings") as error:
            origin()
        assert "secret" not in str(error.value)


@pytest.mark.parametrize("domain", DOMAINS)
def test_local_connection_hub_and_posts_use_saved_composio_settings(scopes, monkeypatch, domain):
    from app.chat import posts
    from app.connections import hub, local_config
    from app.core.local_secrets import update_settings

    scope = next(scope for scope in scopes if scope["domain"] == domain)
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(local_config, "enabled", lambda name: name == "connected_apps")
    monkeypatch.delenv("COMPOSIO_API_KEY", raising=False)
    monkeypatch.delenv("COMPOSIO_TWITTER_AUTH_CONFIG_ID", raising=False)
    monkeypatch.delenv("COMPOSIO_X_AUTH_CONFIG_ID", raising=False)
    update_settings("composio", {"auth_configs": {"twitter": "your-config"}}, {"api_key": "test-only"})
    card = hub.hub_status(scope["workspace"])["connections"]["composio"]
    assert card["ready"] and not card["missing"]
    assert hub.connect_provider(scope["workspace"], "composio")["ok"]
    assert posts._x_available()
    update_settings("composio", {"auth_configs": {}}, {"api_key": ""})
    card = hub.hub_status(scope["workspace"])["connections"]["composio"]
    assert not card["ready"] and card["missing"] == ["composio.api_key"]
    assert "Keys and connections" in card["blocker"]["message"]
    assert not posts._x_available()


@pytest.mark.parametrize("domain", DOMAINS)
def test_local_discord_provider_failures_keep_your_saved_key_private(local_connections, monkeypatch, domain):
    import httpx

    from app.community import composio_discord
    from app.core.local_secrets import update_settings

    scope = next(scope for scope in local_connections if scope["domain"] == domain)
    update_settings("composio", {"auth_configs": {"discordbot": "your-config"}}, {})
    monkeypatch.setattr(httpx.Client, "request", lambda *_args, **_kwargs: httpx.Response(401,
        json={"error": {"message": "Provider rejected saved-test-key"}}))
    with pytest.raises(composio_discord.DiscordBotError) as failure:
        composio_discord.list_connections(scope["workspace"])
    assert failure.value.code == "http_error"
    assert "saved-test-key" not in failure.value.message
    assert "Keys and connections" in failure.value.message and "Connections screen" in failure.value.message


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_local_oauth_preserves_existing_origins_and_validates_them(scopes, monkeypatch, domain):
    from app.content.oauth import flow
    from app.content.oauth.attempts import OAuthError

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.delenv("SUPYMEM_PUBLIC_URL", raising=False)
    monkeypatch.setenv("CONTENT_OAUTH_BASE_URL", f"https://api.{domain}")
    monkeypatch.setenv("CONSOLE_URL", f"https://{domain}")
    assert flow.public_base_url() == f"https://api.{domain}"
    assert flow.console_origin() == f"https://{domain}"
    for setting, origin in (("CONTENT_OAUTH_BASE_URL", flow.public_base_url), ("CONSOLE_URL", flow.console_origin)):
        monkeypatch.setenv(setting, f"https://you:private@{domain}")
        with pytest.raises(OAuthError, match="local settings") as error:
            origin()
        assert "private" not in str(error.value)

"""Founder-facing connection hub copy must not expose server env key names."""

from __future__ import annotations

from tests.company_os_harness import load_company_os_app
import pytest


@pytest.fixture(autouse=True)
def hosted_connection_copy(monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)


@pytest.mark.parametrize("domain", ["factory supply teams", "family holiday planners"])
def test_local_smtp_connection_obeys_saved_send_capability(monkeypatch, tmp_path, domain):
    load_company_os_app()
    from app.connections import hub, local_config
    from app.core import local_capabilities
    from app.core.local_secrets import update_settings

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    update_settings("smtp", {"host": "smtp.example.test", "user": "you@example.test",
        "sender_name": domain, "postal_address": "Your mailing address"}, {"password": "test-only-password"})
    ready = [False]
    monkeypatch.setattr(local_capabilities, "enabled", lambda name: name == "send_email" and ready[0])
    monkeypatch.setattr(local_config, "enabled", local_capabilities.enabled)
    message = "In Settings > Capabilities, switch on Send email, then refresh Connections."
    monkeypatch.setattr(local_capabilities, "blocked_message", lambda name: message)
    monkeypatch.setattr(local_config, "blocked_message", local_capabilities.blocked_message)
    assert local_config.missing("smtp")
    assert local_config.setup_message("smtp") == message
    result = hub.connect_provider("wsp_" + domain.split()[0], "smtp")
    assert not result["ok"] and result["message"] == message
    ready[0] = True
    assert not local_config.missing("smtp")
    assert hub.connect_provider("wsp_" + domain.split()[0], "smtp")["ok"]


@pytest.mark.parametrize("domain", ["factory supply teams", "family holiday planners"])
def test_local_social_rejection_names_saved_authorization_and_reconnection(monkeypatch, domain):
    load_company_os_app()
    from app.connections import hub, local_config

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(local_config, "missing", lambda provider: [])
    monkeypatch.setattr(local_config, "enabled", lambda name: name == "connected_apps")
    monkeypatch.setattr(local_config, "auth_config_id", lambda provider: "test-authorization")
    monkeypatch.setattr(hub, "_hub_callback", lambda provider: "http://127.0.0.1:3300/connections")
    monkeypatch.setattr("app.content.composio_social.connectable", lambda provider: False)
    monkeypatch.setattr("app.content.composio_social.configured", lambda: False)
    result = hub.connect_provider("wsp_" + domain.split()[0], "linkedin")
    assert not result["ok"] and result["needs_you"]
    assert "Settings > Keys and connections" in result["message"] and "reconnect" in result["message"]
    assert "contact support" not in result["message"] and "operator_detail" not in result


@pytest.mark.parametrize("domain", ["factory supply teams", "family holiday planners"])
def test_local_social_connection_switch_changed_during_connect_keeps_capability_next_step(monkeypatch, domain):
    load_company_os_app()
    from app.connections import hub, local_config

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(local_config, "missing", lambda provider: [])
    monkeypatch.setattr(local_config, "enabled", lambda name: False)
    monkeypatch.setattr(local_config, "auth_config_id", lambda provider: "test-authorization")
    message = "In Settings > Capabilities, switch on Connected apps, then reconnect your account."
    monkeypatch.setattr(local_config, "blocked_message", lambda name: message)
    monkeypatch.setattr(hub, "_hub_callback", lambda provider: "http://127.0.0.1:3300/connections")
    monkeypatch.setattr("app.content.composio_social.connectable", lambda provider: False)
    result = hub.connect_provider("wsp_" + domain.split()[0], "linkedin")
    assert not result["ok"] and result["message"] == f"For LinkedIn: {message}"


@pytest.mark.parametrize("domain", ["factory supply teams", "family holiday planners"])
def test_local_missing_connection_events_offer_accounts_recovery(monkeypatch, domain):
    from fastapi import HTTPException

    load_company_os_app()
    from app.api import connections_api

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(connections_api.service, "get_connection", lambda identifier: None)
    with pytest.raises(HTTPException) as error:
        connections_api.connection_events("connection_" + domain.split()[0])
    assert error.value.status_code == 404
    assert error.value.detail["error"]["message"] == "Open Connections, refresh your accounts, then select an available connection."


@pytest.mark.parametrize("domain", ["factory supply teams", "family holiday planners"])
@pytest.mark.parametrize("missing", ["key", "authorization", "switch"])
def test_local_connection_setup_uses_saved_settings_and_plain_capability_guidance(monkeypatch, tmp_path, domain, missing):
    load_company_os_app()
    from app.connections import local_config
    from app.core.local_secrets import update_settings

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    update_settings("composio", {"auth_configs": {} if missing == "authorization" else {"linkedin": "test-authorization"}},
        {"api_key": "" if missing == "key" else "test-only-key"})
    monkeypatch.setattr(local_config, "enabled", lambda name: missing == "authorization")
    message = "In Settings > Keys and connections > Your app connections, save your Composio key." if missing == "key" else "In Settings > Capabilities, switch on Connected apps."
    monkeypatch.setattr(local_config, "blocked_message", lambda name: message)
    result = local_config.setup_message("linkedin")
    assert "LinkedIn" in result
    assert "composio." not in result and "auth_configs" not in result
    if missing == "authorization":
        assert "LinkedIn authorization ID" in result and "App authorization IDs" in result and "Keys and connections" in result
    else:
        assert message in result


@pytest.mark.parametrize("domain", ["factory supply teams", "family holiday planners"])
def test_unavailable_social_connections_keep_setup_details_out_of_founder_copy(monkeypatch, domain):
    load_company_os_app()
    from app.connections import hub

    monkeypatch.setattr("app.content.composio_social.connectable", lambda provider: False)
    monkeypatch.setattr("app.content.composio_social.configured", lambda: False)
    for provider in ("google_calendar", "linkedin", "reddit"):
        result = hub.connect_provider("wsp_" + domain.split()[0], provider)
        assert result["ok"] is False and result["needs_you"] is True
        assert provider not in result["message"]
        assert "COMPOSIO_API_KEY" not in result["message"]
        assert "COMPOSIO_API_KEY" in result["operator_detail"]
        assert "keep working" in result["message"]


def test_gmail_chat_status_uses_plain_owned_address_and_approval_copy(monkeypatch):
    load_company_os_app()
    from app.chat import assistant, connect, execute
    from app.gtm.supymem import pipeline
    from app.connections import hub
    for domain, address in [("warehouse teams", "dispatch@example.com"), ("pet owners", "pets@example.com")]:
        brief = {"workspace_id": "wsp_" + address.split("@")[0], "idea": {"name": domain},
                 "connections": {"gmail": {"connected": True, "allowed": ["draft outreach (no send)"]}}}
        seen = []
        mail = {"connected": True, "account_email": address, "ready": True, "send_enabled": False}
        def readiness(workspace_id):
            seen.append(workspace_id)
            return mail
        monkeypatch.setattr(pipeline, "mail_readiness", readiness)
        monkeypatch.setattr(execute, "_brief_or_empty", lambda venture_id: brief)
        monkeypatch.setattr(connect, "_scope", lambda venture_id: (brief["workspace_id"], address))
        monkeypatch.setattr(hub, "hub_status", lambda workspace_id: {"connections": {"gmail": mail}, "order": ["gmail"]})
        for connected, sending in [(True, False), (True, True), (False, False)]:
            mail.update(connected=connected, send_enabled=sending)
            text = connect.gmail_status(brief)
            card = execute._connections_status("ven_" + address.split("@")[0])
            assert f"**Gmail**: {'Connected as ' + address if connected else 'Not connected'}" in card["body_md"]
            assert card["actions"][0]["label"] == ("Disconnect Gmail" if connected else "Connect Gmail")
            assert assistant._fallback_answer("Is Gmail connected?", brief) == text
            assert (address in text) == connected
            assert "Nothing is sent without your approval." in text
            assert ("15 emails per day" in text) == sending
            assert "no send" not in text and "read connection status" not in text
        mail.update(connected=True, account_email=None)
        assert "Your email address is not confirmed yet" in connect.gmail_status(brief)
        assert set(seen) == {brief["workspace_id"]}


def test_hub_connect_founder_messages_hide_env_keys(monkeypatch):
    load_company_os_app()
    from app.connections import hub

    monkeypatch.delenv("UNIPILE_API_KEY", raising=False)
    monkeypatch.delenv("UNIPILE_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("HUNTER_API_KEY", raising=False)
    unipile = hub.connect_provider("wsp_test", "unipile")
    hunter = hub.connect_provider("wsp_test", "hunter")
    assert unipile["needs_you"] is True
    assert "UNIPILE" not in (unipile.get("message") or "")
    assert "UNIPILE" in (unipile.get("operator_detail") or "")
    assert "HUNTER" not in (hunter.get("message") or "")
    assert "HUNTER" in (hunter.get("operator_detail") or "")


def test_hub_status_env_cards_founder_messages_hide_env_keys(monkeypatch):
    load_company_os_app()
    from app.connections import hub

    monkeypatch.delenv("UNIPILE_API_KEY", raising=False)
    monkeypatch.delenv("UNIPILE_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("HUNTER_API_KEY", raising=False)

    def fake_list_accounts(_workspace_id):
        return []

    monkeypatch.setattr("app.content.oauth.flow.list_accounts", fake_list_accounts)
    monkeypatch.setattr(
        "app.marketing.media.probes.google_ads_available",
        lambda: {"ok": False, "missing": ["GOOGLE_ADS_DEVELOPER_TOKEN"]},
    )
    monkeypatch.setattr(
        "app.marketing.media.probes.meta_ads_available",
        lambda: {"ok": False, "missing": ["META_ADS_ACCESS_TOKEN"]},
    )
    # Skip Gmail / Composio / Discord branches that need live connectors.
    monkeypatch.setattr(
        "app.gtm.supymem.pipeline.mail_readiness",
        lambda _ws: {"ok": True, "ready": True, "account_email": "a@example.com"},
    )
    monkeypatch.setattr(
        "app.content.composio_social.configured",
        lambda: False,
    )

    status = hub.hub_status("wsp_test")
    unipile = status["connections"]["unipile"]
    hunter = status["connections"]["hunter"]
    google = status["connections"]["google_ads"]
    assert unipile["blocker"]["code"] == "unipile_missing"
    assert "UNIPILE" not in unipile["blocker"]["message"]
    assert "UNIPILE" not in (unipile.get("note") or "")
    assert "UNIPILE" in (
        unipile.get("operator_note") or unipile["blocker"].get("operator_detail") or ""
    )
    assert hunter["blocker"]["code"] == "hunter_missing"
    assert "HUNTER" not in hunter["blocker"]["message"]
    assert "HUNTER" in (hunter["blocker"].get("operator_detail") or "")
    assert "GOOGLE_ADS" not in (google.get("note") or "")
    assert "GOOGLE_ADS" not in google["blocker"]["message"]
    assert "GOOGLE_ADS" in (google["blocker"].get("operator_detail") or "")


@pytest.mark.parametrize("domain", ["factory supply teams", "family holiday planners"])
def test_local_callback_origin_ignores_hosted_settings_and_validates_public_url(monkeypatch, tmp_path, domain):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    load_company_os_app()
    from app.connections import hub
    from app.core.local_secrets import update_settings

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    update_settings("composio", {"auth_configs": {"googlesheets": "test-authorization"}}, {"api_key": "test-only"})
    update_settings("capabilities", {"connected_apps": True}, {})
    monkeypatch.setenv("COMPOSIO_API_KEY", "test-only")
    monkeypatch.setenv("CONSOLE_URL", "https://hosted.example.test")
    monkeypatch.delenv("SUPYMEM_PUBLIC_URL", raising=False)
    assert hub._hub_callback("discord") == "http://127.0.0.1:3100/connections?connected=discord"
    from app.connections import local_config

    assert local_config.public_url() == "http://127.0.0.1:3300"
    monkeypatch.setenv("SUPYMEM_PUBLIC_URL", "https://your-app.example.test/")
    assert hub._hub_callback("discord") == "https://your-app.example.test/connections?connected=discord"
    assert local_config.public_url() == "https://your-app.example.test"
    for valid in ("http://localhost:3300", "http://[::1]:3300"):
        monkeypatch.setenv("SUPYMEM_PUBLIC_URL", valid)
        assert local_config.public_url() == valid
    for invalid in ("http://remote.example.test", "https://you:secret@example.test", "https://@example.test", "https://example.test/path",
                    "https://example.test?key=secret", "https://example.test#secret", "https://example.test:bad",
                    "https://example.test\\other", "https://example.test\n"):
        monkeypatch.setenv("SUPYMEM_PUBLIC_URL", invalid)
        with pytest.raises(ValueError, match="local settings") as error:
            local_config.public_url()
        assert "secret" not in str(error.value)
        result = hub.connect_provider("wsp_" + domain.split()[0], "composio")
        assert not result["ok"] and result["needs_you"] and "local settings" in result["message"]
    monkeypatch.delenv("SUPYMEM_MODE")
    assert hub._hub_callback("discord") == "https://hosted.example.test/connections?connected=discord"

import importlib
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from tests.coding_tool_fixtures import installed_test_tool
from tests.company_os_harness import load_company_os_app, local_chat as local_chat


@pytest.fixture(params=["fleet-planning.invalid", "home-gardening.invalid"])
def owner(request, monkeypatch, tmp_path, isolate_app_imports):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / request.param))
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/capabilities.db")
    main = load_company_os_app(reset=False)
    from app.core import local_capabilities, local_secrets, providers_local

    return SimpleNamespace(caps=local_capabilities, store=local_secrets, providers=providers_local,
                           domain=request.param, main=main)


def model(owner):
    owner.providers.save_settings(provider="openai_compatible", base_url="http://127.0.0.1:11434/v1",
                                  models={"chat": "local-chat"}, api_key="")


def mailbox(owner):
    owner.store.update_settings("smtp", {"host": "smtp." + owner.domain, "port": 587,
        "user": "you@" + owner.domain, "sender_name": "Your sender", "postal_address": "Your mailing address",
        "imap_host": "imap." + owner.domain}, {"password": "private-mail-password"})


def switch(owner, name, on):
    owner.store.update_settings("capabilities", {name: on}, {})


@pytest.mark.parametrize("local", [True, False])
def test_unavailable_surfaces_have_one_local_only_switch(owner, monkeypatch, local):
    surfaces = {
        "computer_use": ("actions", "mac.task.prepare"),
        "voice": ("paths", "/voice"),
        "social_posting": ("actions", "social_post"),
        "ads": ("paths", "/ads"),
        "lead_library": ("actions", "gtm.library_claim"),
        "admin": ("paths", "/admin"),
        "billing": ("paths", "/credits"),
        "hosted_connected_apps": ("connections", "unipile"),
    }
    for name in surfaces:
        switch(owner, name, True)
    monkeypatch.setenv("SUPYMEM_MODE", "local" if local else "hosted")
    for name, (surface, value) in surfaces.items():
        item = owner.caps.capability(name)
        assert item["state"] == ("not_in_this_version" if local else "ready")
        assert owner.caps.surface_visible(surface, value) is not local
        if surface == "paths":
            assert owner.caps.surface_visible(surface, f"/v/{owner.domain}{value}/details?tab=setup") is not local
        if local:
            assert "not in this version yet" in item["next_step"]
            assert item["next_step"].count(".") == 1
            if surface == "actions":
                from app.chat.actions import beta_blocked

                assert beta_blocked(value, owner.domain)["body_md"] == item["next_step"]
    assert owner.caps.surface_visible("actions", "build.tool.start")
    assert owner.caps.surface_visible("connections", "googlesheets")
    assert owner.caps.surface_visible("settings", "composio")
    for text in ("Start voice chat", "Post on LinkedIn"):
        assert bool(owner.caps.hidden_request_message(text, "assistant")) is local
    assert owner.caps.hidden_request_message("Build a voice app", "build.tool.start") is None


@pytest.mark.parametrize("request_text", ["Start voice chat", "Post on LinkedIn"])
def test_charlie_hidden_feature_requests_reply_without_starting_work(owner, request_text):
    import asyncio
    from app.chat import router

    reply = asyncio.run(router._dispatch_intent(owner.domain, SimpleNamespace(), "assistant", request_text, None))
    assert reply["kind"] == "text"
    assert reply["actions"] == []
    assert "not in this version yet" in reply["body_md"]
    assert reply["body_md"].count(".") == 1
    assert "instead" in reply["body_md"]


def test_hidden_connection_requests_never_start_authorization_or_a_watch(owner, monkeypatch):
    from app.chat import connect
    from app.connections import hub, local_config

    monkeypatch.setattr(local_config, "missing", lambda provider: [])
    monkeypatch.setattr(hub, "_hub_callback", lambda *args: pytest.fail("Started hidden account authorization"))
    monkeypatch.setattr(connect, "_scope", lambda *args: pytest.fail("Started a hidden connection watch"))
    for provider in ("unipile", "hunter", "google_ads", "meta_ads"):
        result = hub.connect_provider(owner.domain, provider)
        assert result["ok"] is False
        assert "not in this version yet" in result["message"]
    reply = connect.handle(owner.domain, "Connect Unipile")
    assert reply["kind"] == "text" and reply["actions"] == []
    assert "not in this version yet" in reply["body_md"]


def test_shipped_connections_require_saved_settings_before_authorization(owner, monkeypatch):
    from app.connections import hub

    monkeypatch.setattr(hub, "_hub_callback", lambda *args: pytest.fail("Started authorization without your settings"))
    for provider in ("linkedin", "discord"):
        assert owner.caps.surface_visible("connections", provider)
        result = hub.connect_provider(owner.domain, provider)
        assert result["ok"] is False and result["needs_you"] and result["missing"]
        assert "Settings > Keys and connections" in result["message"]


@pytest.mark.parametrize("local", [True, False])
def test_connection_status_hides_unshipped_services_and_counts_only_visible_services(owner, monkeypatch, local):
    from app.connections import hub, local_config, toolkits

    monkeypatch.setenv("SUPYMEM_MODE", "local" if local else "hosted")
    monkeypatch.setattr(local_config, "api_key", lambda: None)
    monkeypatch.setattr(local_config, "missing", lambda provider: ["setup"])
    monkeypatch.setattr(toolkits, "accounts", lambda *args: [])
    monkeypatch.setattr(hub, "_saved_discord_status", lambda *args: {})
    monkeypatch.setattr("app.content.oauth.flow.list_accounts", lambda *args: [])
    monkeypatch.setattr("app.content.composio_social.configured", lambda: False)
    monkeypatch.setattr("app.gtm.supymem.composio_gmail.configured", lambda: False)
    monkeypatch.setattr("app.gtm.supymem.pipeline.mail_readiness", lambda *args: {"ok": False})
    monkeypatch.setattr("app.marketing.media.probes.google_ads_available", lambda: {"ok": False, "missing": []})
    monkeypatch.setattr("app.marketing.media.probes.meta_ads_available", lambda: {"ok": False, "missing": []})
    snapshot = hub.hub_status(owner.domain)
    for name in ("unipile", "hunter", "google_ads", "meta_ads"):
        assert (name in snapshot["connections"]) is not local
        assert (name in snapshot["order"]) is not local
    for name in ("linkedin", "discord"):
        assert name in snapshot["connections"] and name in snapshot["order"]
        assert snapshot["connections"][name]["ready"] is False
    assert snapshot["total_count"] == len(snapshot["connections"])
    assert snapshot["ready_count"] == sum(bool(card.get("ready") or card.get("ok")) for card in snapshot["connections"].values())


@pytest.mark.parametrize("local", [True, False])
def test_charlie_prompt_does_not_offer_features_outside_your_version(owner, monkeypatch, local):
    from app.chat import assistant

    monkeypatch.setenv("SUPYMEM_MODE", "local" if local else "hosted")
    messages = assistant.build_messages(question="What can you help me do?", brief={"_rendered": owner.domain},
                                        history=[], supplied_evidence_only=True)
    system = messages[0]["content"]
    for name in owner.caps.UNAVAILABLE:
        assert (owner.caps.capability(name)["next_step"] in system) is local


def test_background_work_is_opt_in_and_lists_every_local_job(owner):
    from app.core.local_worker import background_jobs, tick_kinds
    from app.tasks.scheduler import INTERVALS

    model(owner)
    mailbox(owner)
    for name in ("read_replies", "background_work", "run_for_me_experiments", "routines"):
        item = owner.caps.capability(name)
        assert item["state"] == "off", (owner.domain, name)
        assert item["background_jobs"] == background_jobs(name)
        assert item["background_jobs"]
        assert "Settings > Capabilities" in item["next_step"]
    assert tick_kinds("daily") == ["retention.purge"]
    assert not any(tick_kinds(tick) for tick in INTERVALS if tick != "daily")
    switch(owner, "background_work", True)
    item = owner.caps.capability("background_work")
    assert item["state"] == "ready"
    assert {job["kind"] for job in item["background_jobs"]} == {
        "inbox.briefing.tick", "inbox.cos_brief.tick", "suggestions.generate", "gtm.journey.maintain"}
    assert "Settings > Keys and connections > Your monthly spend cap" in item["next_step"]


def test_default_switches_and_model_readiness(owner):
    states = {item["name"]: item for item in owner.caps.list_capabilities()}
    assert set(states) == {"research", "find_customers", "send_email", "read_replies", "landing_pages",
        "publish_pages", "page_measurement", "background_work", "run_for_me_experiments", "routines",
        "connected_apps", "content", "build_with_my_tool", *owner.caps.UNAVAILABLE}
    for name in ("send_email", "publish_pages", "connected_apps", "read_replies", "background_work",
                 "run_for_me_experiments", "routines"):
        assert states[name]["state"] == "off"
        assert "Settings > Capabilities" in states[name]["next_step"]
    for name in ("research", "page_measurement"):
        assert states[name]["state"] == "ready"
    for name in ("find_customers", "landing_pages", "content"):
        assert states[name]["state"] == "needs_setup"
        assert states[name]["missing"]
        assert "Keys and connections" in states[name]["next_step"]
    model(owner)
    switch(owner, "run_for_me_experiments", True)
    for name in ("find_customers", "content", "run_for_me_experiments"):
        assert owner.caps.enabled(name)
    assert not owner.caps.enabled("landing_pages")
    assert "Settings > Keys and connections > Coding tools" in owner.caps.blocked_message("landing_pages")


@pytest.mark.parametrize("tool_on", [True, False])
def test_landing_page_connection_step_is_not_repeated(owner, monkeypatch, tool_on):
    from app import coding_agents_consent
    from desktop_runtime import agent_status

    monkeypatch.setattr(agent_status, "statuses", lambda: [])
    monkeypatch.setattr(coding_agents_consent, "consents", lambda subject: {})
    switch(owner, "build_with_my_tool", tool_on)
    item = owner.caps.capability("landing_pages")
    assert item["state"] == "needs_setup", owner.domain
    assert item["next_step"] == owner.caps.blocked_message("build_with_my_tool")
    assert item["next_step"].count("Settings >") == 1
    assert item["missing"]
    if not tool_on:
        assert "Settings > Capabilities" in item["next_step"]
    else:
        assert "Settings > Keys and connections > Coding tools" in item["next_step"]


@pytest.mark.parametrize("name", ["build_with_my_tool", "landing_pages"])
def test_connection_step_names_all_supported_coding_tools(owner, monkeypatch, name):
    from app import coding_agents_consent
    from desktop_runtime import agent_status

    monkeypatch.setattr(agent_status, "statuses", lambda: [])
    monkeypatch.setattr(coding_agents_consent, "consents", lambda subject: {})
    message = owner.caps.blocked_message(name)
    assert "Claude Code, Codex, Cursor or OpenCode" in message, owner.domain
    assert "sign in with your own subscription" in message
    assert "Allow Charlie" in message and "check the connection again" in message


@pytest.mark.parametrize("provider", ["builtin", "tavily", "exa", "openai", "firecrawl"])
def test_selected_search_requirements_and_optional_reader(owner, provider):
    owner.store.update_settings("search", {"provider": provider}, {})
    assert owner.caps.enabled("research") is (provider == "builtin")
    if provider in {"tavily", "exa"}:
        owner.store.update_settings("search", {}, {"api_key": "private-search-key"})
    elif provider == "openai":
        owner.providers.save_settings(provider="openai", base_url="https://api.openai.com/v1",
            models={"chat": "saved-model"}, api_key="private-model-key")
    elif provider == "firecrawl":
        owner.store.update_settings("search", {"firecrawl_url": "http://127.0.0.1:3002"}, {})
    assert owner.caps.enabled("research")
    switch(owner, "research", False)
    assert not owner.caps.enabled("research")
    assert "Settings > Capabilities" in owner.caps.blocked_message("research")


def test_user_keys_do_not_come_from_environment_or_plain_values(owner, monkeypatch):
    monkeypatch.setenv("SUPYMEM_DESKTOP_INSTANCE_ID", "local-capabilities-test")
    monkeypatch.setenv("TAVILY_API_KEY", "ignored-private-key")
    monkeypatch.setenv("SUPYMEM_SMTP_PASSWORD", "ignored-private-key")
    owner.store.update_settings("search", {"provider": "tavily", "api_key": "wrong-store"}, {})
    assert not owner.caps.enabled("research")
    switch(owner, "send_email", True)
    owner.store.update_settings("smtp", {"host": "smtp." + owner.domain, "user": "you@" + owner.domain,
        "sender_name": "Your sender", "postal_address": "Your mailing address"}, {})
    assert not owner.caps.enabled("send_email")
    assert "Mail app password" in owner.caps.capability("send_email")["missing"]
    assert "wrong-store" not in str(owner.caps.list_capabilities())
    assert "ignored-private-key" not in str(owner.caps.list_capabilities())


def test_mail_readiness_requires_saved_credentials_and_compliance_fields(owner):
    switch(owner, "send_email", True)
    switch(owner, "read_replies", True)
    mailbox(owner)
    assert owner.caps.enabled("send_email") and owner.caps.enabled("read_replies")
    owner.store.update_settings("smtp", {"postal_address": ""}, {"password": owner.store.get_secret("smtp", "password")})
    result = owner.caps.capability("send_email")
    assert result["state"] == "needs_setup" and "Postal address" in result["missing"]
    assert "Your mail account" in result["next_step"]
    assert owner.caps.enabled("read_replies")
    owner.store.update_settings("smtp", {"imap_host": ""}, {"password": owner.store.get_secret("smtp", "password")})
    assert not owner.caps.enabled("read_replies")


@pytest.mark.parametrize("provider", ["vercel", "netlify", "s3"])
def test_publishing_readiness_and_saved_switch(owner, provider):
    switch(owner, "publish_pages", True)
    owner.store.update_settings("publishing", {"provider": provider}, {})
    assert not owner.caps.enabled("publish_pages")
    secrets = {"token": "private-publish-token", "access_key_id": "private-id",
        "secret_access_key": "private-secret"}
    owner.store.update_settings("publishing", {"project": owner.domain, "bucket": owner.domain,
        "region": "us-east-1", "prefix": "your-pages", "site_url": "https://" + owner.domain}, secrets)
    assert owner.caps.enabled("publish_pages")
    if provider == "s3":
        for field, label, value in (("prefix", "S3 page folder", "your-pages"),
                                    ("site_url", "Public site URL", "https://" + owner.domain)):
            owner.store.update_settings("publishing", {field: ""}, secrets)
            assert label in owner.caps.capability("publish_pages")["missing"]
            owner.store.update_settings("publishing", {field: value}, secrets)
    switch(owner, "publish_pages", False)
    assert owner.caps.capability("publish_pages")["state"] == "off"
    assert owner.store.read_settings("capabilities")["values"]["publish_pages"] is False


def test_connections_require_secret_not_admin_policy(owner):
    switch(owner, "connected_apps", True)
    assert not owner.caps.enabled("connected_apps")
    owner.store.update_settings("composio", {}, {"api_key": "private-connection-key"})
    assert not owner.caps.enabled("connected_apps")
    assert "App authorization IDs" in owner.caps.capability("connected_apps")["missing"]
    owner.store.update_settings("composio", {"auth_configs": {"gmail": "owner-authorization"}}, {})
    assert owner.caps.enabled("connected_apps")


@pytest.mark.parametrize("module_name,function,cap_name", [
    ("app.pagebuilder.api", "enabled", "landing_pages"),
    ("app.approvals.external", "execution_enabled", "send_email"),
    ("app.tasks.routines", "enabled", "routines"),
    ("app.autopilot.god_requests", "beta_gate_open", "background_work"),
    ("app.simulation.service", "enabled", "run_for_me_experiments"),
    ("app.tools.browse", "enabled", "research"),
])
def test_local_gates_obey_owner_not_flags_or_policies(owner, monkeypatch, tmp_path, module_name, function, cap_name):
    from app.credits import policy

    module = importlib.import_module(module_name)
    monkeypatch.setenv("SUPYMEM_PAGEBUILDER_ENABLED", "1")
    monkeypatch.setenv("SUPYMEM_OUTBOUND_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("SUPYMEM_SIMULATION_ENABLED", "true")
    monkeypatch.setenv("SUPYMEM_SIMULATION_WORKSPACES", "another-workspace")
    model(owner)
    mailbox(owner)
    if cap_name == "landing_pages":
        from app.coding_agents_consent import set_consent
        from app.workspaces.resolve import local_person
        from desktop_runtime import agent_status

        assert owner.caps.capability(cap_name)["state"] == "needs_setup"
        monkeypatch.setattr(agent_status, "statuses", lambda: [{"name": "codex", "installed": True,
            "signed_in": True, "subscription_login": True, "available": True}])
        binary = tmp_path / "codex"
        binary.touch(mode=0o700)
        with installed_test_tool(monkeypatch, "codex", binary):
            set_consent(local_person().id, "codex", True)
    switch(owner, cap_name, True)
    monkeypatch.setattr(policy, "get_policy", lambda *args: pytest.fail("Local gate read a hosted policy"))
    if hasattr(module, "beta_scope_enforced"):
        monkeypatch.setattr(module, "beta_scope_enforced", lambda: pytest.fail("Local gate checked admin approval"))
    args = (owner.domain,) if function == "beta_gate_open" or module_name == "app.simulation.service" else ()
    assert getattr(module, function)(*args)
    if hasattr(module, "require_enabled"):
        module.require_enabled()
    switch(owner, cap_name, False)
    assert not getattr(module, function)(*args)
    if hasattr(module, "require_enabled"):
        with pytest.raises(HTTPException) as error:
            module.require_enabled()
        assert "Settings > Capabilities" in str(error.value.detail)
    if module_name == "app.simulation.service":
        with pytest.raises(HTTPException) as error:
            module.authority(None, owner.domain, owner.domain, "you")
        assert "Settings > Capabilities" in str(error.value.detail)
    if module_name == "app.tools.browse":
        assert "Settings > Capabilities" in module.start("https://" + owner.domain, "Read the site", None)
    if module_name == "app.approvals.external":
        with pytest.raises(module.OutboundExecutionError, match="Settings > Capabilities"):
            module.begin_metered_external_execution(workspace_id=owner.domain, approval_id=None,
                action="gtm_email_send", provider="smtp", target={})


def test_external_gate_binds_non_email_actions_to_connections(owner, monkeypatch):
    from app.approvals import external

    mailbox(owner)
    switch(owner, "send_email", True)
    assert external.execution_enabled()
    assert not external.execution_enabled("content_publish")
    with pytest.raises(external.OutboundExecutionError, match="Settings > Capabilities"):
        external.begin_metered_external_execution(workspace_id=owner.domain, approval_id=None,
            action="content_publish", provider="composio", target={})
    owner.store.update_settings("composio", {}, {"api_key": "private-connection-key"})
    assert not external.execution_enabled("content_publish")
    owner.store.update_settings("composio", {"auth_configs": {"gmail": "owner-authorization"}}, {})
    switch(owner, "connected_apps", True)
    assert external.execution_enabled("content_publish")
    monkeypatch.setattr(external, "outbound_rate", lambda *args: (_ for _ in ()).throw(
        external.OutboundExecutionError("rate_check_kept")))
    with pytest.raises(external.OutboundExecutionError, match="rate check kept"):
        external.begin_metered_external_execution(workspace_id=owner.domain, approval_id=None,
            action="content_publish", provider="composio", target={})


def test_capability_api_validation_persistence_and_secret_free_response(owner):
    from app.api.local_capabilities_api import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    mailbox(owner)
    response = client.get("/local/capabilities")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert len(response.json()["capabilities"]) == len(owner.caps.REGISTRY) + len(owner.caps.UNAVAILABLE)
    assert "private-mail-password" not in response.text
    response = client.put("/local/capabilities/send_email", json={"on": True})
    assert response.status_code == 200 and response.json()["state"] == "ready"
    assert owner.caps.enabled("send_email")
    assert client.put("/local/capabilities/send_email", json={"on": "true"}).status_code == 422
    assert client.put("/local/capabilities/send_email", json={"on": True, "extra": 1}).status_code == 422
    assert client.put("/local/capabilities/unknown", json={"on": True}).status_code == 404


@pytest.mark.parametrize("client_host,url,origin", [
    ("192.0.2.10", "http://127.0.0.1", None),
    ("127.0.0.1", "http://evil.invalid", None),
    ("127.0.0.1", "http://127.0.0.1", "https://evil.invalid"),
])
def test_api_rejects_non_loopback_requests(owner, client_host, url, origin):
    from app.api.local_capabilities_api import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, base_url=url, client=(client_host, 50000))
    assert client.get("/local/capabilities", headers={"origin": origin} if origin else {}).status_code == 403


def test_router_is_only_mounted_locally(owner, monkeypatch):
    from app.api import mount_routers

    app = FastAPI()
    mount_routers(app)
    assert TestClient(app).get("/local/capabilities").status_code == 200
    monkeypatch.delenv("SUPYMEM_MODE")
    app = FastAPI()
    mount_routers(app)
    assert TestClient(app).get("/local/capabilities").status_code == 404


def test_local_background_requests_never_create_admin_requests_or_start_work(owner, monkeypatch):
    from app.autopilot import god_requests

    switch(owner, "background_work", True)
    monkeypatch.setattr(god_requests, "db", lambda: pytest.fail("Local activation used the admin request database"))
    monkeypatch.setattr(god_requests, "on_god_mode_requested", lambda *args: pytest.fail("Sent an admin email"))
    assert god_requests.venture_approved(owner.domain)
    assert god_requests.my_status(owner.domain, "you", owner.domain)["status"] == "approved"
    assert not god_requests.request_access(owner.domain, owner.domain, "you")["created"]
    switch(owner, "background_work", False)
    assert not god_requests.venture_approved(owner.domain)
    response = god_requests.request_access(owner.domain, owner.domain, "you")
    assert response["status"] == "none" and not response["created"]
    assert "Settings > Capabilities" in response["next_step"]
    reply = god_requests.explain_reply(owner.domain)
    assert "Settings > Capabilities" in reply["body_md"]
    assert reply["actions"][0]["href"] == "/settings/local/capabilities"
    assert not owner.caps.enabled("background_work")


def test_invalid_mail_port_and_removed_provider_turn_readiness_off(owner):
    model(owner)
    mailbox(owner)
    switch(owner, "send_email", True)
    for port in (0, 65536, True, "587"):
        owner.store.update_settings("smtp", {"port": port}, {"password": owner.store.get_secret("smtp", "password")})
        assert not owner.caps.enabled("send_email")
        assert "Outgoing mail port" in owner.caps.capability("send_email")["missing"]
    owner.providers.clear_settings()
    assert not owner.caps.enabled("content")


def test_api_rejects_corrupt_settings_without_disclosure_or_overwrite(owner, tmp_path, monkeypatch):
    from app.api.local_capabilities_api import router

    root = tmp_path / "corrupt"
    root.mkdir()
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(root))
    path = root / "local-settings.json"
    path.write_text("private-invalid-settings")
    path.chmod(0o600)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    for response in (client.get("/local/capabilities"),
                     client.put("/local/capabilities/research", json={"on": False})):
        assert response.status_code == 503
        assert "private-invalid-settings" not in response.text
        assert "data folder permissions" in response.json()["detail"]
    assert path.read_text() == "private-invalid-settings"


def test_local_browse_parent_gate_keeps_intent_and_parent_validation(owner, monkeypatch):
    from app.tasks import reasoning
    from app.tools import browse

    monkeypatch.setattr(reasoning, "enabled", lambda: pytest.fail("Read a hosted parent policy"))
    with pytest.raises(browse.BrowseStop, match="intent_unknown"):
        browse._check_parent({})
    browse._check_parent({"intent_checked": True, "reasoning_run_id": None})
    switch(owner, "background_work", False)
    with pytest.raises(browse.BrowseStop, match="Settings > Capabilities"):
        browse._check_parent({"intent_checked": True, "reasoning_run_id": "your-request"})


def test_mounted_api_works_with_the_real_local_owner_boundary(owner):
    from core.local_security import _proxy_secret

    with TestClient(owner.main.create_app(), base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        client.headers["X-Local-Proxy-Secret"] = _proxy_secret().decode("ascii")
        assert client.get("/local/capabilities").status_code == 200
        response = client.put("/local/capabilities/research", json={"on": False})
        assert response.status_code == 200 and response.json()["state"] == "off"
    assert not owner.caps.enabled("research")


def test_local_routine_review_keeps_digest_validation_without_a_hosted_flag(owner, monkeypatch):
    from app.approvals import standing
    from app.tasks import routines

    switch(owner, "routines", True)
    monkeypatch.setattr(standing, "enabled", lambda: pytest.fail("Read a hosted approval feature flag"))
    monkeypatch.setattr(routines, "db", lambda: lambda: nullcontext(None))
    monkeypatch.setattr(routines, "owned", lambda *args: object())
    monkeypatch.setattr(routines, "approval_digest", lambda task: "current-digest")
    with pytest.raises(ValueError, match="routine changed"):
        routines.approve(owner.domain, "your-routine", "old-digest")


def test_malformed_saved_field_types_are_missing_not_ready(owner):
    switch(owner, "send_email", True)
    mailbox(owner)
    owner.store.update_settings("smtp", {"host": {"unexpected": owner.domain}},
        {"password": owner.store.get_secret("smtp", "password")})
    assert "Outgoing mail host" in owner.caps.capability("send_email")["missing"]
    owner.store.update_settings("search", {"provider": []}, {})
    assert "Search service" in owner.caps.capability("research")["missing"]
    switch(owner, "publish_pages", True)
    owner.store.update_settings("publishing", {"provider": {}}, {})
    assert "Publishing service" in owner.caps.capability("publish_pages")["missing"]


def test_model_readiness_names_only_missing_fields_and_the_selected_search_provider(owner, monkeypatch):
    monkeypatch.setattr(owner.providers, "configured_providers", lambda: {"provider": "openai_compatible",
        "providers": {"openai_compatible": {"configured": False, "api_key_set": False,
            "base_url": "http://127.0.0.1:11434/v1", "models": {}}}})
    assert owner.caps.capability("content")["missing"] == ["Chat model"]
    owner.store.update_settings("search", {"provider": "openai"}, {})
    monkeypatch.setattr(owner.providers, "configured_providers", lambda: {"provider": "anthropic",
        "providers": {"anthropic": {"configured": True, "api_key_set": True,
            "base_url": "https://api.anthropic.com/v1", "models": {"chat": "saved-chat"}}}})
    assert owner.caps.capability("research")["missing"] == ["Model service (OpenAI or Azure OpenAI)"]

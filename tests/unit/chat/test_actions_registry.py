from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from tests.company_os_harness import load_company_os_app, local_chat as local_chat

load_company_os_app()

from app.chat.actions import all_actions, get_action
from app.chat.confirm import ConfirmationCard


@pytest.mark.parametrize("venture_id", ["ven_invoicing", "ven_gardening"])
@pytest.mark.parametrize("ready", [False, True])
def test_local_copy_social_and_background_requests_use_owner_capabilities(monkeypatch, venture_id, ready):
    from app.chat.actions import beta_blocked, disabled_reason, social_post_enabled

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.delenv("SUPYMEM_SOCIAL_POST_ENABLED", raising=False)
    called = []
    def enabled(name):
        called.append(name)
        return ready
    next_step = "Open Settings > Keys and connections and save your Composio API key."
    monkeypatch.setitem(sys.modules, "app.core.local_capabilities", SimpleNamespace(
        enabled=enabled, blocked_message=lambda name: next_step))
    for action, capability_name in (("social_post", "social_posting"), ("social_connect", "social_posting"),
                                    ("social_status", "social_posting"), ("social_guide", "social_posting"),
                                    ("experiment.tools.link", "connected_apps"),
                                    ("god_mode.request", "background_work")):
        called.clear()
        reply = beta_blocked(action, venture_id)
        assert reply is None if ready else reply["body_md"] == next_step
        assert disabled_reason(action) == (None if ready else next_step)
        assert capability_name in called
    assert social_post_enabled() is ready


@pytest.mark.parametrize("venture_id", ["ven_invoicing", "ven_gardening"])
@pytest.mark.parametrize("ready", [False, True])
def test_local_copy_background_request_dispatches_without_hosted_approval(monkeypatch, venture_id, ready):
    import asyncio
    from app.chat import router
    from app.chat import guard, other_venture
    from app.org import manager
    from app.autopilot import god_requests

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    next_step = "Open Settings > Your capabilities and turn on Background work."
    monkeypatch.setitem(sys.modules, "app.core.local_capabilities", SimpleNamespace(
        enabled=lambda name: ready, blocked_message=lambda name: next_step))
    monkeypatch.setattr(god_requests, "beta_gate_open", lambda *args: pytest.fail("No hosted approval"))
    monkeypatch.setattr(guard, "guard_reply", lambda *args, **kwargs: None)
    monkeypatch.setattr(other_venture, "other_venture_reply", lambda *args, **kwargs: None)
    monkeypatch.setattr(manager, "manager_name", lambda *args: "Your assistant")
    calls = []
    async def execute(vid, action, **kwargs):
        calls.append((vid, action, kwargs))
        return {"kind": "text", "body_md": "Your requested background work is on.", "actions": [], "citations": []}
    monkeypatch.setattr(router, "execute_action", execute)
    ctx = SimpleNamespace(venture={"name": venture_id, "workspace_id": "workspace"})
    reply = asyncio.run(router._prepare_intent(venture_id, ctx, "god_on", "Turn on background work", None, actor_subject="owner"))
    assert reply["kind"] == "text" and "confirmation" not in str(reply)
    assert len(calls) == int(ready)
    if ready:
        assert calls == [(venture_id, "autopilot.god_on", {"actor_subject": "owner", "founder_initiated": True})]
    else:
        assert reply["body_md"] == next_step
    refusal = {"kind": "refusal", "body_md": "Switch to your other venture before requesting its work.", "actions": [], "citations": []}
    monkeypatch.setattr(guard, "guard_reply", lambda *args, **kwargs: refusal)
    monkeypatch.setattr(router, "_note_guard", lambda *args: None)
    reply = asyncio.run(router._prepare_intent(venture_id, ctx, "god_on", "Turn on background work for another venture", None, actor_subject="owner"))
    assert reply == refusal and len(calls) == int(ready)


@pytest.mark.parametrize("venture_id", ["ven_invoicing", "ven_gardening"])
def test_local_copy_dormant_actions_offer_exact_existing_chat_commands(monkeypatch, venture_id):
    from app.chat.actions import disabled_reason

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    for action in ("memory.remember", "memory.correct", "memory.forget", "memory.list"):
        assert disabled_reason(action) is None
    for action, instruction in (("idea.answer", "your venture home"), ("idea.explore", "your idea")):
        reason = disabled_reason(action)
        assert instruction in reason
        assert "yet" not in reason and "Ask me for your next step" not in reason
    assert "new chat message" in disabled_reason("not.a.real.action")


def test_registry_is_closed_and_actions_are_named():
    names = {action.name for action in all_actions()}
    assert {
        "status",
        "why",
        "money",
        "leads",
        "research",
        "agents",
        "whats_next",
        "refine_site",
        "openworker_search",
        "agent_reach",
        "slate_override",
        "pause",
        "cancel",
        "approve",
        "office_hours",
        "ceo_review",
        "brain_brief",
        "social_post",
        "social_guide",
        "social_status",
        "social_connect",
    } <= names
    assert get_action("status").governed_capability == "cap.read.venture_status"
    assert get_action("leads").endpoint.startswith("GET")


def test_consequential_action_builds_confirmation_card():
    card = ConfirmationCard.for_action(
        get_action("slate_override"),
        interpretation="Drop item 3.",
        consequences=["One task will be deferred."],
    )
    assert card.as_action()["confirmation"]["capability"] == "cap.will.override"


@pytest.mark.parametrize("venture_id", ["ven_procurement", "ven_weekend_cooking"])
@pytest.mark.parametrize("capability_name,action_name", [
    ("research", "research.start"), ("find_customers", "gtm.find"),
    ("send_email", "gtm.send"), ("read_replies", "inbox.briefing.consent"),
    ("landing_pages", "pagebuilder.build"), ("publish_pages", "site.publish"),
    ("landing_pages", "experiment.page.configure"), ("page_measurement", "experiment.page.readout"),
    ("background_work", "autopilot.god_on"),
    ("computer_use", "mac.task.prepare"),
    ("build_with_my_tool", "build.tool.start"),
    ("run_for_me_experiments", "experiment.beliefs.approve"), ("routines", "routines.create"),
    ("social_posting", "social_connect"), ("content", "content.drafts"),
])
def test_local_registry_uses_owner_capabilities(monkeypatch, venture_id, capability_name, action_name):
    from app.chat.actions import beta_blocked, disabled_reason

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    ready = True
    next_step = f"Open Settings > Your capabilities and turn on {capability_name}."
    def enabled(name):
        assert name == capability_name
        return ready
    monkeypatch.setitem(sys.modules, "app.core.local_capabilities", SimpleNamespace(
        enabled=enabled, blocked_message=lambda name: next_step))
    assert get_action(action_name)
    assert beta_blocked(action_name, venture_id) is None
    assert disabled_reason(action_name) is None
    ready = False
    assert beta_blocked(action_name, venture_id)["body_md"] == next_step
    assert disabled_reason(action_name) == next_step


@pytest.mark.parametrize("venture_id", ["ven_procurement", "ven_weekend_cooking"])
@pytest.mark.parametrize("action_name", [
    "build.start", "build.prepare", "build.app.create", "build.accept", "app.debug", "app.iterate",
    "import.start", "scope.request", "mac.task.prepare",
    "gtm.library_claim", "gtm.library_more", "gtm.library_notes",
])
def test_local_unshipped_actions_give_the_same_next_step_on_every_gate(monkeypatch, venture_id, action_name):
    from app.chat.actions import beta_blocked, disabled_reason, get_action

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("SUPYMEM_BETA_SCOPE", "0")
    from app.core.local_capabilities import UNAVAILABLE
    monkeypatch.setitem(sys.modules, "app.core.local_capabilities", SimpleNamespace(
        enabled=lambda name: name not in UNAVAILABLE, blocked_message=lambda name: UNAVAILABLE[name]["next_step"]))
    reply = beta_blocked(action_name, venture_id)
    if action_name not in {"gtm.library_claim", "gtm.library_more", "gtm.library_notes", "mac.task.prepare"}:
        assert reply is None
        assert get_action(action_name).endpoint and get_action(action_name).required_permission
        return
    assert reply and reply["actions"] == []
    assert reply["body_md"] == disabled_reason(action_name)
    assert "not in this version yet; " in reply["body_md"]
    assert "coming soon" not in reply["body_md"] and "\u2014" not in reply["body_md"]
    assert "ask me" in reply["body_md"].lower()


@pytest.mark.parametrize("venture_id", ["ven_procurement", "ven_weekend_cooking"])
def test_local_hosting_guides_use_saved_publishing_without_coding_or_login_orders(monkeypatch, venture_id):
    from app.chat import hosting
    from app.core import local_secrets

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(hosting, "db", lambda: pytest.fail("Local guidance must not mutate hosted plans or queue work"))
    monkeypatch.setattr(local_secrets, "read_settings", lambda section: {"values": {"provider": "netlify"}})
    ready = False
    next_step = "Open Settings > Keys and connections and save your Netlify token and project."
    monkeypatch.setitem(sys.modules, "app.core.local_capabilities", SimpleNamespace(
        enabled=lambda name: ready, blocked_message=lambda name: next_step))
    for ready in (False, True):
        for reply in (hosting.start(venture_id), hosting.plan(venture_id), hosting.choose(venture_id, {"category": "hosting", "provider": "netlify"})):
            assert "pagebuilder.build" in {action.get("action") for action in reply["actions"]}
            assert ("site.publish" in {action.get("action") for action in reply["actions"]}) == ready
            assert "export" in reply["body_md"].lower()
            assert "mac.task.prepare" not in str(reply) and "sign in" not in str(reply).lower()
            if not ready:
                assert next_step in reply["body_md"]
    assert "Nothing was saved" in hosting.choose(venture_id, {"category": "hosting", "provider": "netlify", "token": "not-a-real-secret"})["body_md"]


@pytest.mark.parametrize("venture_id", ["ven_procurement", "ven_weekend_cooking"])
def test_local_capability_fallback_reads_saved_settings_and_keeps_stop_controls_available(monkeypatch, venture_id):
    from app.chat.actions import beta_blocked, disabled_reason, social_post_enabled
    from app.core import local_secrets

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("SUPYMEM_SOCIAL_POST_ENABLED", "1")
    monkeypatch.setitem(sys.modules, "app.core.local_capabilities", None)
    values, secrets = {}, {}
    monkeypatch.setattr(local_secrets, "read_settings", lambda section: {"values": values})
    monkeypatch.setattr(local_secrets, "get_secret", lambda section, name: secrets.get(name))
    assert beta_blocked("research.start", venture_id) is None
    for action in ("gtm.send", "inbox.briefing.consent", "site.publish"):
        assert "Settings > Keys and connections" in beta_blocked(action, venture_id)["body_md"]
    values.update(host="mail.example", user="you@example.test", sender_name="Your team", postal_address="12 Lake Rd", imap_host="mail.example", provider="netlify", project="your-site")
    secrets.update(password="placeholder", token="placeholder", api_key="placeholder")
    for action in ("gtm.send", "inbox.briefing.consent", "site.publish"):
        assert beta_blocked(action, venture_id) is None
    with monkeypatch.context() as off:
        off.setitem(sys.modules, "app.core.local_capabilities", SimpleNamespace(
            enabled=lambda name: False, blocked_message=lambda name: "Open Settings > Your capabilities."))
        for action in ("pause", "cancel", "build.cancel", "autopilot.send_off", "routines.pause", "routines.delete", "connections.disconnect", "site.unpublish"):
            assert beta_blocked(action, venture_id) is None and disabled_reason(action) is None
    assert not social_post_enabled()
    assert "not in this version yet" in disabled_reason("social_post")


@pytest.mark.parametrize("venture_id", ["ven_procurement", "ven_weekend_cooking"])
def test_local_source_review_starts_no_qa_without_a_receipt(monkeypatch, venture_id):
    import asyncio
    from app.chat.actions import beta_blocked
    from app.chat.router import _prepare_intent
    from app.chat.context import ChatContext

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    looked_up = []
    def brief(vid, **kwargs):
        looked_up.append(vid)
        return {}
    monkeypatch.setattr("app.ventures.state_brief.get_venture_state_brief", brief)
    reply = asyncio.run(_prepare_intent(venture_id, ChatContext(venture={"id": venture_id, "name": venture_id}), "source_qa", "Review my stored coding source", None))
    assert beta_blocked("build.accept", venture_id) is None and looked_up == [venture_id]
    assert "no stored coding-source receipt" in reply["body_md"]
    assert "No QA job was started and no product was deployed" in reply["body_md"] and not reply["actions"]


@pytest.mark.parametrize("venture_id", ["ven_procurement", "ven_weekend_cooking"])
def test_local_routines_respect_owner_switch_before_dispatch(monkeypatch, venture_id):
    import asyncio
    from app.chat.execute import execute_action
    from app.tasks import routines

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    next_step = "Open Settings > Your capabilities and turn on routines."
    monkeypatch.setitem(sys.modules, "app.core.local_capabilities", SimpleNamespace(
        enabled=lambda name: False, blocked_message=lambda name: next_step))
    monkeypatch.setattr(routines, "action", lambda *args: pytest.fail("Switched-off routines must not start"))
    assert asyncio.run(execute_action(venture_id, "routines.create"))["body_md"] == next_step

"""Every console route and label chat may mention exists in apps/console/src (chat never invents UI)."""
from __future__ import annotations

import json
import re
from pathlib import Path
from html import unescape
from types import SimpleNamespace

import pytest

from tests.company_os_harness import load_company_os_app

load_company_os_app()
from app.chat import navigation_map as nav  # noqa: E402

SRC = Path(__file__).resolve().parents[3] / "apps" / "console" / "src"


@pytest.mark.parametrize("venture_id", ["ven_dispatch", "ven_gardens"])
def test_local_open_connections_link_survives_chat_normalization(monkeypatch, venture_id):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    status = {"label": "Show connections here", "action": "connections.status"}
    for href in (nav.path("connections", venture_id), f"/v/{venture_id}/connections"):
        link = {"label": "Open Connections", "href": href}
        assert nav.in_chat([status, link], check_readiness=False) == [status, link]
        assert nav.hidden_href(href) is None


@pytest.mark.parametrize("audience", ["Industrial procurement teams", "Family cycling groups"])
def test_local_chat_failure_copy_keeps_builds_and_replaces_internal_reports(monkeypatch, audience):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    from app.chat import actions
    monkeypatch.setattr(actions, "disabled_reason", lambda _: None)
    body = (f"Your build for {audience} stopped. Open Builds to review your saved result. "
            "Report a problem in Account settings and include code device_terminal_task_unavailable.")
    reply = nav.in_chat_reply({"kind": "needs_you", "body_md": body, "content": body,
                               "parts": [{"type": "text", "text": body}], "actions": []})
    for text in (reply["body_md"], reply["content"], reply["parts"][0]["text"]):
        assert "Open Builds" in text
        assert not re.search(r"Report a problem|include code|device_terminal_task_unavailable", text)
        assert "Show status here" in text
    assert any(action.get("action") == "status" for action in reply["actions"])
    assert nav.in_chat_reply(reply) == reply

_ALL = "\n".join(p.read_text(errors="ignore") for p in SRC.rglob("*.tsx"))
_ALL += "\n".join(p.read_text(errors="ignore") for p in SRC.rglob("*.ts"))
_ALL = unescape(_ALL)


@pytest.mark.parametrize("venture_id", ["ven_invoicing", "ven_gardening"])
@pytest.mark.parametrize("cap", [None, 0, "24.50"])
def test_local_copy_purchase_help_reads_your_spending_settings(monkeypatch, venture_id, cap):
    from app.core import local_secrets
    from app.payments import flags

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    read_settings = local_secrets.read_settings
    monkeypatch.setattr(local_secrets, "read_settings", lambda section: {"values": {"monthly_spend_cap": cap}}
                        if section == "spend" else read_settings(section))
    monkeypatch.setattr(flags, "payments_accepting", lambda: pytest.fail("Local help must not read hosted purchases"))
    reply = nav.purchase_help(venture_id, "How do I request credits?")
    assert "Settings > Keys and connections" in reply["body_md"]
    assert "provider account" in reply["body_md"]
    assert ("$24.50" if cap == "24.50" else "$0.00" if cap == 0 else "no monthly spending limit") in reply["body_md"]
    assert not re.search(r"credits|checkout|manual review|upgrade|team|supymem\.com", reply["body_md"], re.I)
    assert reply["actions"][0]["href"] == "/settings/local"
    from app.chat.assistant import _nav_hint

    assert "built-in search and page reading unless you connect another search service" in _nav_hint()
    assert "Buy more" not in _nav_hint()


@pytest.mark.parametrize("audience", ["Industrial procurement teams", "Family cycling groups"])
def test_conn_regression_settings_help_is_grounded_in_real_controls(monkeypatch, audience):
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    load_company_os_app()
    from app.chat.assistant import build_messages
    from app.payments import flags
    from app.chat.store import _clean

    for accepting in (False, True):
        monkeypatch.setattr(flags, "payments_accepting", lambda: accepting)
        messages = build_messages(question=f"How do I download my data and buy more for {audience}?", brief={}, history=[])
        system = messages[0]["content"]
        context = "\n".join(message["content"] for message in messages)
        assert "Download my venture" in context and "I'm stuck, download report" in context
        assert "conversations" in context and "document text" in context and "not bundled" in context
        assert ("Purchases are open" if accepting else "Purchases are closed") in context
        assert "manual review" in context and "plan upgrade" in context
        assert "Exact button labels in the navigation context are allowed" in system
        assert ("Buy more" if accepting else "Request credits") in context
        help_copy = _clean(nav.billing_hint(), assistant=True)
        assert ("Buy more" if accepting else "Request credits") in help_copy
        assert "manual review" in help_copy and "plan upgrade" in help_copy
        for quote in ("Request 250 credits.", "Request credits: 250.", "Request credits, 250 available."):
            assert "credit" not in _clean(f"Your work for {audience} is saved. {quote}", assistant=True).lower()
        assert _clean("You serve credit unions with 250 locations.", assistant=True) == "You serve credit unions with 250 locations."
        reply = nav.purchase_help("venture", f"How do I buy more, and do I need to upgrade my plan for {audience}?")
        assert _clean(reply["body_md"], assistant=True) == help_copy
        assert reply["actions"][0]["href"] == "/v/venture/settings"
        assert nav.purchase_help("venture", f"Should I buy more bicycles for {audience}?") is None
        assert nav.purchase_help("venture", f"Should I upgrade my plan for serving {audience}?") is None
        assert nav.purchase_help("venture", f"Write sales scripts for {audience} with the question Buy more?") is None
        assert nav.purchase_help("venture", f"Connect Gmail for {audience}. How do I buy more?") is None


@pytest.mark.parametrize("screen", sorted(nav.SCREENS))
def test_screen_page_and_sidebar_label_exist(screen):
    label, route, page = nav.SCREENS[screen]
    assert (SRC / "app" / page).is_file(), f"{route} has no console page {page}"
    assert f'label: "{label}"' in _ALL or f"<h2>{label}" in _ALL or f'title="{label}"' in _ALL, label


@pytest.mark.parametrize("key", sorted(nav.LABELS))
def test_every_ui_label_exists_in_console_source(key):
    for part in re.split(r"\{\w+\}", nav.LABELS[key]):
        if part.strip():
            assert part.strip() in _ALL, f"{key}: {part!r} not found in apps/console/src"


def test_hint_never_mentions_an_integrations_area_as_real():
    assert "no 'Integrations area'" in nav.HINT
    assert nav.path("connections", "ven_1") == "/connections?venture=ven_1"


@pytest.mark.parametrize("venture_id", ["ven_stockroom", "ven_hiking"])
def test_local_work_screen_links_are_not_replaced_with_chat_actions(monkeypatch, venture_id):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    links = [{"label": label, "href": f"/v/{venture_id}/{path}"} for label, path in (
        ("Open builds", "builds"), ("Open report", "research?dossier=saved-report"), ("Open experiments", "experiments"))]
    body = "\n".join(f"[{link['label']}]({link['href']})" for link in links)
    assert nav.in_chat_reply({"body_md": body, "actions": links}) == {"body_md": body, "actions": links}


@pytest.mark.parametrize("venture_id,audience", [
    ("ven_warehouse", "Wholesale stock checks for warehouse managers"),
    ("ven_sleep", "Sleep routines for new parents"),
])
def test_work_and_outreach_aliases_normalize_buttons_and_body_links(venture_id, audience):
    for screen, expected_action in (("work", "status"), ("outreach", "gtm.status")):
        href = f"/v/{venture_id}/{screen}?x=1#review"
        reply = nav.in_chat_reply({
            "body_md": f"{audience}: [Review]({href}).",
            "actions": [{"label": "Review", "href": href}],
        })
        assert reply["body_md"] == f"{audience}: Review."
        assert len(reply["actions"]) == 1
        assert reply["actions"][0]["action"] == expected_action
        assert "href" not in reply["actions"][0]
        assert nav.in_chat_reply(reply) == reply


@pytest.mark.parametrize("venture_id,audience", [
    ("ven_fleet", "Fleet maintenance for depot managers"),
    ("ven_plants", "Plant watering reminders for apartment gardeners"),
])
def test_settings_recovery_destinations_survive_assistant_serialization(venture_id, audience):
    from app.chat.blockers import explain
    from app.chat.store import _ser

    for code in ("insufficient_credits", "computer_use_permissions"):
        reply = explain(venture_id, {"code": code})
        button = reply["actions"][0]
        assert button["kind"] == "recovery_link"
        href = button["href"]
        actions = [*reply["actions"],
                   {**button, "href": href.replace("settings", "settings?recovery=1", 1)},
                   {**button, "href": "https://app.supymem.com" + href}]
        body = f"{audience}\n\n{reply['body_md']}\n\n[{button['label']}]({href})"
        direct = {**reply, "body_md": body, "content": body,
                  "parts": [{"type": "text", "text": body}], "actions": actions}
        row = SimpleNamespace(id="message-recovery", conversation_id=None, role="assistant", kind="text",
                              venture_id=venture_id, workspace_id="workspace", body_md=body, citations_json=None,
                              actions_json=json.dumps(actions), created_at=None)

        for served, normalized in ((nav.in_chat_reply(direct), False), (_ser(row), True)):
            if normalized and code == "insufficient_credits":
                assert served["actions"] == reply["actions"]
                assert served["body_md"] == served["content"] == reply["body_md"]
                assert served["parts"] == [{"type": "text", "text": reply["body_md"]}]
                assert nav.in_chat_reply(served) == served
                continue
            assert served["actions"] == actions
            assert f"]({href})" in served["body_md"]
            assert f"]({href})" in served["content"]
            assert f"]({href})" in served["parts"][0]["text"]
            assert nav.in_chat_reply(served) == served
        assert direct["actions"] == actions and row.actions_json == json.dumps(actions)
        plain_actions = [{"label": button["label"], "href": href}]
        legacy = SimpleNamespace(**{**vars(row), "kind": "navigation", "actions_json": json.dumps(plain_actions)})
        for served, normalized in ((nav.in_chat_reply({**direct, "actions": plain_actions}), False), (_ser(legacy), True)):
            if normalized and code == "insufficient_credits":
                assert served["actions"] == reply["actions"] and served["body_md"] == reply["body_md"]
                assert nav.in_chat_reply(served) == served
                continue
            assert served["actions"] == [{**action, "kind": "recovery_link"} for action in plain_actions]
            assert f"]({href})" in served["body_md"]
            assert nav.in_chat_reply(served) == served
        assert legacy.actions_json == json.dumps(plain_actions)

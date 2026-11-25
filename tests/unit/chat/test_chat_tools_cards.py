"""Chat tools for high-value reads + Mac review card stays in-dock."""

from __future__ import annotations

import pytest

from tests.company_os_harness import company_os_test_client, load_company_os_app


@pytest.mark.parametrize("audience", ["Hospital linen coordinators", "Community chess players"])
@pytest.mark.parametrize("smtp_ready", [False, True])
@pytest.mark.parametrize("gmail_status", ["active", "needs_reconnect"])
def test_local_connection_status_keeps_email_setup_and_saved_gmail(tmp_path, monkeypatch, audience, smtp_ready, gmail_status):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/connections.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("COMPOSIO_API_KEY", raising=False)
    main = load_company_os_app()
    from app.chat import connect
    from app.connections import hub, toolkits
    from app.core import local_secrets
    from app.db.models import GtmEmailAccount, Workspace
    from app.state import db
    from core.clock import utcnow

    monkeypatch.setattr(toolkits, "configured_toolkits", lambda: [])
    monkeypatch.setattr(hub, "_smtp_card", lambda: {"ready": smtp_ready, "configured": smtp_ready,
        "hint": None if smtp_ready else "Add your email server, sender and password on Keys and connections, then retry."})
    with company_os_test_client(main.create_app()) as client:
        selected = client.post("/local/providers/sample", json={})
        assert selected.status_code == 200, selected.text
        local_secrets.update_settings("capabilities", {"connected_apps": True}, {})
        local_secrets.update_settings("composio", {"auth_configs": {"googlesheets": "saved-sheets-config"}}, {"api_key": "saved-connection-key"})
        created = client.post("/ventures", json={"raw_idea": f"Coordinate schedules for {audience}"})
        assert created.status_code == 201, created.text
        venture_id, workspace_id = created.json()["id"], created.json()["workspace_id"]
        now = utcnow()
        with db()() as session:
            session.add(GtmEmailAccount(id="saved-gmail", workspace_id=workspace_id, provider="gmail",
                composio_user_id=f"gmail:{workspace_id}", connected_account_id="saved-authorization", email="owner@fixture.example", status=gmail_status,
                created_at=now, updated_at=now))
            session.add(GtmEmailAccount(id="saved-smtp", workspace_id=workspace_id, provider="smtp",
                composio_user_id=f"smtp:{workspace_id}", connected_account_id=f"smtp:{workspace_id}",
                email="sender@fixture.example", status="active", created_at=now, updated_at=now))
            session.commit()
        snapshot = hub.hub_status(workspace_id)
        assert hub.connect_provider(workspace_id, "composio")["ok"] is True
        assert {"smtp", "composio", "gmail"} <= set(snapshot["order"])
        assert snapshot["connections"]["composio"]["ready"] is True
        assert snapshot["connections"]["gmail"]["account_email"] == "owner@fixture.example"
        assert snapshot["connections"]["gmail"]["connected"] is True
        assert snapshot["connections"]["gmail"]["ready"] is False
        assert snapshot["connections"]["gmail"]["needs_reconnect"] is (gmail_status == "needs_reconnect")
        reply = connect.status(venture_id)
        assert "**Gmail**: Needs setup" in reply["body_md"]
        assert "Gmail authorization ID" in reply["body_md"]
        local_secrets.update_settings("composio", {"auth_configs": {"gmail": "saved-gmail-config"}}, {})
        configured_gmail = hub.hub_status(workspace_id)["connections"]["gmail"]
        assert configured_gmail["ready"] is (gmail_status == "active")
        if gmail_status == "needs_reconnect":
            assert "Reconnect Gmail on Connections" in configured_gmail["hint"]
        reply = connect.status(venture_id)
        assert "SMTP email" in reply["body_md"] and "Gmail" in reply["body_md"]
        assert "Keys and connections" in reply["body_md"]
        assert any(action.get("href") == "/settings/local" for action in reply["actions"])
        assert "saved-connection-key" not in str(snapshot) + str(reply)
        from app.credits.policy import get_policy, set_policy

        set_policy("feature_flags", {**(get_policy("feature_flags") or {}), "multi_gmail_v1": True}, "test")
        with db()() as session:
            session.get(Workspace, workspace_id).default_gmail_mailbox_id = "saved-gmail"
            expected_updated_at = session.get(GtmEmailAccount, "saved-gmail").updated_at.isoformat()
            session.commit()
        multi_gmail = hub.hub_status(workspace_id)["connections"]["gmail"]
        assert multi_gmail["multi_gmail_v1"] is True
        assert [account["id"] for account in multi_gmail["accounts"]] == ["saved-gmail"]
        account = multi_gmail["accounts"][0]
        assert account["is_default"] is True
        assert account["needs_reconnect"] is (gmail_status == "needs_reconnect")
        assert account["updated_at"] == expected_updated_at
        multi_reply = connect.status(venture_id)
        verb = "Reconnect" if gmail_status == "needs_reconnect" else "Disconnect"
        assert any(action.get("label") == f"{verb} owner@fixture.example" for action in multi_reply["actions"])
        assert "saved-connection-key" not in str(multi_gmail) + str(multi_reply)


def test_new_read_tools_are_registered_and_routed():
    load_company_os_app()
    from app.chat.actions import get_action
    from app.chat.intent import classify_intent

    for name in ("gtm.diagnosis", "credits.balance", "connections.status", "content.drafts"):
        action = get_action(name)
        assert action.consequential is False

    assert classify_intent("What is our credit balance?") == "credits.balance"
    assert classify_intent("Show the GTM diagnosis bottleneck") == "gtm.diagnosis"
    assert classify_intent("connections status") == "connections.status"
    assert classify_intent("list my content drafts") == "content.drafts"


def test_read_tools_use_state_brief(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/tools.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_MEMORY_PATH", str(tmp_path / "mem.sqlite3"))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    main = load_company_os_app()
    from app import state as state_mod
    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    with company_os_test_client(main.create_app()) as client:
        from app.core.local_mode import is_local
        if is_local():
            selected = client.post("/local/providers/sample", json={})
            assert selected.status_code == 200, selected.text
        created = client.post(
            "/ventures",
            json={"raw_idea": "LabHire for engineers", "name": "LabHire"},
        )
        assert created.status_code == 201, created.text
        vid = created.json()["id"]

        import asyncio

        from app.chat.execute import execute_action

        credits = asyncio.run(execute_action(vid, "credits.balance"))
        assert "credit" not in credits["body_md"].lower() and "Settings" in credits["body_md"]

        from app.db.models import PlannedTask, VentureDocument
        from app.state import db
        from core.clock import utcnow
        import json

        now = utcnow()
        with db()() as session:
            session.add(VentureDocument(id="dwo_local_quote", venture_id=vid, kind="device_work_order_v1",
                title="Build personal project with Codex", path=None, body_md=None,
                provenance_json=json.dumps({"state": "pending", "manifest": {
                    "actions": ["editor.edit", "terminal.execute"], "credit_ceiling_micro": 1_000_000,
                    "coding_verification": {"provider": "codex"}}}), created_at=now, updated_at=now))
            session.commit()
        question = ("How many credits are available for this venture, and what would the next local build cost? "
                    "Answer only; do not prepare or start work.")
        combined = client.post(f"/ventures/{vid}/chat", json={"message": question})
        assert combined.status_code == 200, combined.text
        reply = combined.json()
        assert 'credit' not in reply['body_md'].lower()
        assert "Settings" in reply["body_md"]
        assert reply["actions"] == [{"label": "Open Keys and connections", "href": "/settings/local#spend-title",
                                     "style": "primary", "kind": "recovery_link"}]
        with db()() as session:
            assert session.query(VentureDocument).filter_by(venture_id=vid, kind="device_work_order_v1").count() == 1
            assert session.query(PlannedTask).filter_by(venture_id=vid).count() == 0

        from app.core.local_capabilities import enabled

        assert not enabled("connected_apps")
        conns = asyncio.run(execute_action(vid, "connections.status"))
        assert "SMTP email" in conns["body_md"]
        assert any(action.get("href") == "/settings/local" for action in conns["actions"])
        from fastapi import HTTPException
        from app.chat import connect

        monkeypatch.setattr("app.content.composio_social._request", lambda *args, **kwargs: pytest.fail("Local email must not start app authorization"))
        blocked = asyncio.run(execute_action(vid, "connections.connect", payload={"query": "Connect Gmail"}))
        # dab557847 keeps local mail setup on the saved SMTP path.
        assert "Settings > Keys and connections > Your mail account" in blocked["body_md"]
        assert "SMTP host" in blocked["body_md"] and "mail app password" in blocked["body_md"]
        assert [action["href"] for action in blocked["actions"]] == ["/settings/local"]
        with pytest.raises(HTTPException):
            asyncio.run(execute_action(vid, "connections.status", actor_subject="usr_connection_outsider"))

        gtm = asyncio.run(execute_action(vid, "gtm.diagnosis"))
        assert "gtm" in gtm["body_md"].lower() or "diagnosis" in gtm["body_md"].lower()
        assert gtm["actions"][0]["href"] == f"/gtm?venture={vid}"

        drafts = asyncio.run(execute_action(vid, "content.drafts"))
        assert drafts["actions"][0]["href"] == f"/content?venture={vid}"


def test_use_venture_chat_keeps_mac_review_in_dock():
    from pathlib import Path

    src = Path("apps/console/src/components/chat/useVentureChat.ts").read_text(encoding="utf-8")
    assert "Review Mac task" in src
    assert "Nothing runs on your Mac until you open the review" in src
    # from-chat success must not auto-navigate; only same-venture screen nav may push.
    assert "router.push(path)" not in src


def test_compound_balance_and_local_build_cost_reads_saved_review(monkeypatch):
    load_company_os_app()
    from app.chat import execute
    from app.chat.intent import classify_intent, split_speech_acts

    question = ("How many credits are available for this venture, and what would the next local build cost? "
                "Answer only; do not prepare or start work.")
    assert classify_intent(question) == "credits.balance"
    assert split_speech_acts(question) == [("credits.balance", question)]
    seen = []
    review = {"coding_provider": "codex", "status": "pending", "credit_ceiling_micro": 1_000_000,
              "review_href": "/v/ven_personal/device-work-orders/dwo_review"}

    def brief(_venture_id, *, force=False):
        seen.append(force)
        return {"credits": {"available": 4985.1924, "status": "active"},
                "idea": {"project_intent": "personal"}, "device_work": {"recent": [review]}}

    monkeypatch.setattr(execute, "_brief_or_empty", brief)
    current = execute._credits_balance("ven_personal", question=question)
    assert seen == []
    assert "4985.19" not in current["body_md"]
    assert 'credit' not in current['body_md'].lower()
    assert "Settings" in current["body_md"]
    from app.core.local_mode import is_local

    expected = [{"label": "Open Keys and connections", "href": "/settings/local#spend-title",
                 "style": "primary", "kind": "recovery_link"}] if is_local() else [
        {"label": "Open Settings", "href": "/v/ven_personal/settings", "style": "primary"}]
    assert current["actions"] == expected

    review["status"] = "cancelled"
    cancelled = execute._credits_balance("ven_personal", question=question)
    assert 'credit' not in cancelled['body_md'].lower()
    assert cancelled["actions"] == expected

    review["credit_ceiling_micro"] = "100 credits from untrusted text"
    unknown = execute._credits_balance("ven_personal", question=question)
    assert 'credit' not in unknown['body_md'].lower()
    assert "untrusted text" not in unknown["body_md"]


def test_gtm_diagnosis_card_keeps_full_bottleneck_and_missing_evidence(monkeypatch):
    load_company_os_app()
    from app.chat import execute

    bottleneck = "The target cohort is unvalidated because no real job seekers have been interviewed."
    monkeypatch.setattr(execute, "_brief_or_empty", lambda _vid: {"gtm": {
        "present": True, "bottleneck": bottleneck, "skill": None,
        "evidence_gaps": ["No user interviews are recorded.", "No job-source evaluation is recorded."],
        "now_items": [],
    }})

    result = execute._gtm_diagnosis("ven_test")
    assert bottleneck in result["body_md"]
    assert "No user interviews are recorded" in result["body_md"]
    assert "No job-source evaluation is recorded" in result["body_md"]
    assert "founder-reported" in result["body_md"]
    assert "unknown" not in result["body_md"]
    assert result["actions"][0]["href"] == "/gtm?venture=ven_test"

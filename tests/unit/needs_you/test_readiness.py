"""Setup readiness: each missing step is ONE card, never repeated, and goes away when satisfied."""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from tests.company_os_harness import load_company_os_app

load_company_os_app()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/r.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_MEMORY_PATH", str(tmp_path / "mem.sqlite3"))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    monkeypatch.setenv("NEEDS_YOU_QUIET_HOURS", "0-0")
    monkeypatch.setenv("SUPYMEM_OUTBOUND_EXECUTION_ENABLED", "true")
    main = load_company_os_app()
    from app import state as state_mod

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    with TestClient(main.create_app()) as client:
        from app.core.local_mode import is_local
        if is_local():
            from core.local_security import _proxy_secret
            from app.core.local_secrets import update_settings
            update_settings("smtp", {"host": "127.0.0.1", "port": 587, "user": "founder@example.invalid",
                "sender_name": "Your founder", "postal_address": "1 Example Street, New York, NY 10001"},
                {"password": "fixture-mail-password"})
            update_settings("capabilities", {"send_email": True}, {})
            client.headers["x-local-proxy-secret"] = _proxy_secret().decode()
        made = client.post("/ventures", json={"raw_idea": "a marketplace for restoring vintage synthesizers"})
        assert made.status_code == 201, made.text
        yield made.json()["id"]


@pytest.fixture
def local_env(monkeypatch, request):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    return request.getfixturevalue("env")


def _drafts(vid, n=3):
    from app.autopilot import outreach

    from tests.unit.autopilot.test_outreach import _contacts

    for contact_id in _contacts(vid, n):
        contact = next(contact for contact in outreach.contacts(vid) if contact.id == contact_id)
        outreach._save_draft(vid, {"contact_id": contact_id, "to": contact.email, "subject": "Hi", "body": "Hello", "state": "draft"})


def _cards(vid, needle):
    from app.chat.store import list_messages

    return [m for m in list_messages(vid, limit=50) if needle in m["body_md"]]


def test_nothing_waiting_means_no_cards(env):
    from app.needs_you import readiness

    assert readiness.missing(env) == [] and readiness.sync(env) == {"opened": 0, "resolved": 0}


@pytest.mark.parametrize("opening,offer,audience", [
    ("I'm unsure about a monitoring system for warehouse operators.",
     "A tool for warehouse operators that flags missing inventory.", "warehouse operators"),
    ("I'm unsure about a soil sensor for hobby gardeners.",
     "An app for hobby gardeners that tracks watering schedules.", "hobby gardeners"),
])
def test_saved_discovery_prefill_does_not_recurse(env, opening, offer, audience, caplog):
    from datetime import timedelta

    from app.db.models import Job, Venture
    from app.db.supymem_models import IdeaSession
    from app.intake.smart import (
        _describes_offer,
        _idea_candidate,
        _prefill,
        discovery_idea_text,
    )
    from app.needs_you import readiness
    from app.state import db
    from app.ventures import startup_review
    from app.ventures.current_idea import current_revision
    from app.ventures.state_brief import get_venture_state_brief
    from core.clock import utcnow

    now = utcnow()
    with db()() as session:
        venture = session.get(Venture, env)
        venture.raw_idea = opening + "\nClarification: " + offer
        venture.created_at = now - timedelta(days=7)
        revision = current_revision(venture, session)
        if revision:
            session.get(IdeaSession, revision.session_id).state = "abandoned"
            session.flush()
        assert current_revision(venture, session) is None
        startup_review._document(session, env, "intake_decision",
            {"prefill_answers": {"offer": offer, "customer": audience}})
        session.add(Job(id="failed_discovery_research", workspace_id=venture.workspace_id, venture_id=env,
            kind="research.deep", status="failed", payload_json="{}", created_at=now - timedelta(days=6),
            finished_at=now - timedelta(days=6), last_error="provider_down"))
        session.commit()
    with db()() as session:
        venture = session.get(Venture, env)
        assert _prefill(venture.raw_idea) == _prefill(offer)
        assert _idea_candidate(venture.raw_idea) == offer
        assert discovery_idea_text(venture.raw_idea) == venture.raw_idea
        assert _describes_offer(opening)
        evidence = startup_review.evidence_for(session, venture)
        assert any(item["facts"].get("customer") == audience for item in evidence)
    brief = get_venture_state_brief(env)
    assert brief["idea"]["confirmed"] is False
    assert brief["idea"]["offer"] == offer
    assert readiness.sync(env) == {"opened": 0, "resolved": 0}
    assert not any(record.getMessage() == "readiness_sync_failed" for record in caplog.records)


@pytest.mark.parametrize("idea", ["Invoice reconciliation for freight brokers", "Meal planning for amateur cyclists"])
@pytest.mark.parametrize("failure_kind", ["saved_json", "next_moves_cycle", "json_walker"])
def test_readiness_sync_with_failed_work_and_saved_next_moves(env, monkeypatch, caplog, idea, failure_kind):
    import json
    import logging
    import sys
    from datetime import timedelta

    from app.chat import connect, next_moves, proactive
    from app.chat.store import list_messages
    from app.core.local_mode import is_local
    from app.db.models import ChatMessage, Job, Venture, VentureDocument
    from app.db.supymem_models import Experiment
    from app.experiments import ledger
    from app.needs_you import readiness, service
    from app.state import db
    from core.clock import utcnow
    from core.logging import JsonFormatter
    from sqlalchemy import select

    from tests.unit.test_autonomous_experiments import confirm_saved_idea

    now = utcnow()
    with db()() as session:
        venture = session.get(Venture, env)
        venture.raw_idea = idea
        venture.created_at = now - timedelta(days=7)
        session.commit()
    confirm_saved_idea(env)
    if is_local():
        from app.ventures import startup_review

        current = startup_review.get(env)
        while current["question"]:
            current = startup_review.skip_question(env, current["revision"], current["question"]["field"])
        startup_review._mutate(env, current["revision"], "priorities", value="Review my saved test results")
    with db()() as session:
        workspace_id = session.get(Venture, env).workspace_id
        session.add(Job(id="failed_research", workspace_id=workspace_id, venture_id=env,
            kind="research.deep", status="failed", payload_json="{}", created_at=now - timedelta(days=6),
            finished_at=now - timedelta(days=6), last_error="provider_down"))
        session.add(Experiment(id="failed_test", workspace_id=workspace_id, venture_id=env, kind=ledger.KIND,
            state="concluded", hypothesis=idea, outcome="refuted", ended_at=now - timedelta(days=5),
            context=json.dumps({"direction": ledger.current_direction(session, env)})))
        session.commit()
    recommendation = next_moves.experiment_learning(env)["recommendation"]
    with db()() as session:
        session.add_all([
            ChatMessage(id="saved_next_moves", workspace_id=workspace_id, venture_id=env, role="assistant",
                kind="next_moves", body_md="Your test result is saved. Review it to choose your next step.",
                citations_json=json.dumps([{"next_moves": {"recommendation": recommendation, "actions": []}}]),
                created_at=now - timedelta(days=4)),
            ChatMessage(id="saved_nudge", workspace_id=workspace_id, venture_id=env, role="assistant",
                kind=proactive.NUDGE, body_md="Your research did not finish. Retry it using your saved idea.",
                actions_json=json.dumps([{"label": "Retry research", "action": "research.prepare", "retry": True}]),
                created_at=now - timedelta(days=3)),
        ])
        session.commit()
    monkeypatch.setattr(connect, "_is_connected", lambda wid, slug: False)
    _drafts(env, 1)
    with caplog.at_level(logging.ERROR):
        assert readiness.sync(env)["opened"] == 2
        assert readiness.sync(env) == {"opened": 0, "resolved": 0}
        card = next_moves.compose(env)
        if is_local():
            assert isinstance(card["recommendation"], dict) and card["recommendation"]["id"]
        else:
            assert "recommendation" not in card
        assert {"saved_next_moves", "saved_nudge"} <= {message["id"] for message in list_messages(env)}
    assert not caplog.records
    assert {request["blocking_ref"] for request in service.list_open(env)} == {"setup:gmail", "setup:send"}
    with db()() as session:
        saved_count = len(list(session.scalars(select(ChatMessage.id).where(ChatMessage.venture_id == env))))

    private_value = {"idea": idea, "recommendation": recommendation, "detail": "private recursion detail"}
    caplog.clear()
    with monkeypatch.context() as recursion:
        if failure_kind == "saved_json":
            depth = sys.getrecursionlimit() * 10
            nested = "[" * depth + json.dumps(private_value) + "]" * depth
            with pytest.raises(RecursionError):
                json.loads(nested)
            with db()() as session:
                document = session.scalar(select(VentureDocument).where(VentureDocument.venture_id == env,
                    VentureDocument.kind == "outreach_draft"))
                document_id, saved_json = document.id, document.provenance_json
                document.provenance_json = saved_json[:-1] + ', "saved_context": ' + nested + "}"
                session.commit()
            with db()() as session:
                assert session.get(VentureDocument, document_id).provenance_json.endswith(nested + "}")
            expected_functions = []
        elif failure_kind == "next_moves_cycle":
            def ready_drafts(venture_id):
                return next_moves.compose(venture_id, requested=bool(private_value))

            def snapshot(venture_id):
                return readiness.missing(venture_id)

            recursion.setattr(readiness, "_ready_drafts", ready_drafts)
            recursion.setattr(next_moves, "_snapshot", snapshot)
            expected_functions = ["missing", "ready_drafts", "compose", "_compose", "snapshot"]
        else:
            from core.redact import redact

            private_value["next_moves"] = private_value
            recursion.setattr(readiness, "_ready_drafts", lambda venture_id: redact(private_value))
            expected_functions = ["redact"]
        with pytest.raises(RecursionError):
            readiness.missing(env)
        with caplog.at_level(logging.WARNING, logger=readiness.__name__):
            for _ in range(2):
                assert readiness.sync(env) == {"opened": 0, "resolved": 0}
        assert len(caplog.records) == 2
        for record in caplog.records:
            assert record.getMessage() == "readiness_sync_failed"
            assert record.error_type == "RecursionError" and record.exc_info is None
            assert record.repeating_functions[:len(expected_functions)] == expected_functions
            if failure_kind == "saved_json":
                assert record.repeating_functions == []
            assert len(record.repeating_functions) == len(set(record.repeating_functions))
            assert not record.stack_info
            diagnostic = JsonFormatter().format(record)
            assert json.loads(diagnostic)["extra"] == {"venture_id": env, "error_type": "RecursionError",
                "repeating_functions": record.repeating_functions}
            assert idea not in diagnostic and "private recursion detail" not in diagnostic
            assert json.dumps(recommendation) not in diagnostic
        if failure_kind == "saved_json":
            with db()() as session:
                document = session.get(VentureDocument, document_id)
                assert document.provenance_json.endswith(nested + "}")
                document.provenance_json = saved_json
                session.commit()
    private_value["next_moves"] = private_value
    with pytest.raises(ValueError, match="Circular reference detected"):
        json.dumps(private_value)

    def unavailable(venture_id):
        raise RuntimeError("private provider detail")

    caplog.clear()
    with monkeypatch.context() as tick:
        tick.setattr(readiness, "_server_off_note", unavailable)
        for _ in range(2):
            assert readiness.sync(env) == {"opened": 0, "resolved": 0}
    assert len(caplog.records) == 2
    assert all(record.getMessage() == "readiness_sync_failed" and record.error_type == "RuntimeError"
               and record.exc_info is None and not hasattr(record, "repeating_functions") for record in caplog.records)
    assert "private provider detail" not in caplog.text
    with db()() as session:
        assert len(list(session.scalars(select(ChatMessage.id).where(ChatMessage.venture_id == env)))) == saved_count
    monkeypatch.setattr(connect, "_is_connected", lambda wid, slug: True)
    assert readiness.sync(env) == {"opened": 0, "resolved": 1}
    assert [request["blocking_ref"] for request in service.list_open(env)] == ["setup:send"]


def test_gmail_and_send_consent_each_get_exactly_one_card(env, monkeypatch):
    from app.chat import connect
    from app.needs_you import readiness, service

    monkeypatch.setattr(connect, "_is_connected", lambda wid, slug: False)
    _drafts(env)
    assert readiness.sync(env)["opened"] == 2
    assert readiness.sync(env)["opened"] == 0  # no spam on the next tick
    assert len(service.list_open(env)) == 2
    gmail = _cards(env, "Gmail is not connected")
    assert len(gmail) == 1 and "3 emails ready" in gmail[0]["body_md"]
    assert gmail[0]["actions"][0] == {"label": "Connect Gmail", "action": "chat.reply", "text": "connect my gmail", "style": "primary"}
    send = _cards(env, "up to 15/day")
    assert len(send) == 1 and send[0]["actions"][0]["confirmation"]["action"] == "autopilot.allow_send"
    review = next(a for a in send[0]["actions"] if a["label"] == "Review drafts first")
    assert review["action"] == "gtm.status" and review["payload"] == {"show_saved": True}
    assert "Gmail?." not in send[0]["body_md"]
    from app.chat.execute import execute_action
    out = asyncio.run(execute_action(env, review["action"], payload=review["payload"]))
    assert "**Email drafts**" in out["body_md"]


def test_cards_disappear_when_satisfied(env, monkeypatch):
    from app.autopilot import service as autopilot
    from app.chat import connect
    from app.needs_you import readiness, service

    state = {"gmail": False}
    monkeypatch.setattr(connect, "_is_connected", lambda wid, slug: state["gmail"])
    _drafts(env)
    readiness.sync(env)
    state["gmail"] = True
    autopilot.update_settings(env, allow_send=True, send_max_per_day=20, confirmed_send_max=True,
                             actor_subject=autopilot._owner(env))
    assert readiness.sync(env)["resolved"] == 2
    assert service.list_open(env) == []


def test_allow_sending_tap_sets_capped_permission_and_tells_the_truth_about_the_server(env, monkeypatch):
    from app.autopilot import guard
    from app.chat.execute import execute_action

    _drafts(env, 1)
    out = asyncio.run(execute_action(env, "autopilot.allow_send"))
    assert "up to 15 emails a day" in out["body_md"] and "Stop sending" in out["body_md"]
    assert "Settings" not in out["body_md"]
    assert out["actions"] == [{"label": "Stop sending", "action": "autopilot.send_off", "style": "ghost"}]
    assert guard.get_settings(env)["allow_send"] and guard.get_settings(env)["send_max_per_day"] == 15
    monkeypatch.delenv("SUPYMEM_OUTBOUND_EXECUTION_ENABLED")
    assert "up to 15 emails a day" in asyncio.run(execute_action(env, "autopilot.allow_send"))["body_md"]


@pytest.mark.parametrize("audience", ["Freight dispatch teams", "Family gardening groups"])
def test_owner_can_reenable_sending_from_the_saved_capability(local_env, monkeypatch, audience):
    from app.core.local_capabilities import blocked_message
    from app.chat import connect
    from app.core.local_secrets import update_settings
    from app.db.models import Venture
    from app.needs_you import readiness, service
    from app.state import db

    env = local_env
    with db()() as session:
        session.get(Venture, env).raw_idea = audience
        session.commit()
    update_settings("capabilities", {"send_email": False}, {})
    monkeypatch.delenv("SUPYMEM_OUTBOUND_EXECUTION_ENABLED")
    monkeypatch.setattr(connect, "_is_connected", lambda wid, slug: True)
    _drafts(env, 2)
    readiness.sync(env)
    readiness.sync(env)
    assert len(_cards(env, blocked_message("send_email"))) == 1
    assert service.list_open(env) == []
    update_settings("capabilities", {"send_email": True}, {})
    assert readiness.sync(env)["opened"] == 1


def test_continue_without_fixing_is_not_re_asked_immediately(env, monkeypatch):
    from app.chat import connect
    from app.needs_you import readiness, service

    monkeypatch.setattr(connect, "_is_connected", lambda wid, slug: False)
    _drafts(env, 1)
    readiness.sync(env)
    for r in service.list_open(env):
        service.resolve(r["id"], venture_id=env)
    assert readiness.sync(env)["opened"] == 0  # inside the cool-down


def test_connected_gmail_clears_historical_refs_and_the_saved_card_without_affecting_other_ventures(env, monkeypatch):
    from app.chat import connect
    from app.chat.store import list_messages
    from app.db.models import Venture
    from app.needs_you import readiness, service
    from app.state import db

    with db()() as s:
        workspace_id = s.get(Venture, env).workspace_id
    service.request_human(env, reason_code="gmail_connect", title="Connect Gmail", blocking_ref="legacy:gmail", what_we_need="Connect this company's mailbox.", how_to_resume="Connect Gmail in Connections.")
    service.request_human(env, reason_code="os_permission", title="Mac permissions", blocking_ref="device:permissions", what_we_need="Grant Mac permissions.", how_to_resume="Open System Settings.")
    # A Gmail connected to another company cannot clear this company's request.
    monkeypatch.setattr(connect, "_is_connected", lambda wid, slug: wid != workspace_id)
    assert readiness.sync(env)["resolved"] == 0
    assert any(r["reason_code"] == "gmail_connect" for r in service.list_open(env))
    monkeypatch.setattr(connect, "_is_connected", lambda wid, slug: wid == workspace_id)
    assert readiness.sync(env)["resolved"] == 1
    assert [r["reason_code"] for r in service.list_open(env)] == ["os_permission"]
    cleared = [m for m in list_messages(env) if m["kind"] == "needs_you_resolved"]
    assert len(cleared) == 1 and "Resolved: Connect Gmail" in cleared[0]["body_md"]
    assert cleared[0]["actions"] == []


def test_worker_resolves_connection_and_readiness_before_reminding(monkeypatch):
    from app import tenancy
    from app.autopilot import service as autopilot
    from app.chat import connect
    from app.needs_you import readiness, service
    from app.worker import handlers

    calls = []
    monkeypatch.setattr(tenancy, "current_or_none", lambda: None)
    monkeypatch.setattr(connect, "check_pending", lambda **kwargs: calls.append("connections"))
    monkeypatch.setattr(readiness, "sync_all", lambda **kwargs: calls.append("readiness"))
    monkeypatch.setattr(service, "remind_due", lambda **kwargs: calls.append("reminders"))
    async def tick_all(founder_id=None):
        calls.append("tick")
    monkeypatch.setattr(autopilot, "tick_all", tick_all)
    asyncio.run(handlers._autopilot_tick({"founder_id": "selected-owner"}))
    assert calls == ["connections", "readiness", "reminders", "tick"]


def test_send_consent_card_is_skipped_once_chat_offered_automate_all_emails(env, monkeypatch):
    import json

    from app.chat import connect
    from app.db.models import ChatMessage, Venture
    from app.needs_you import readiness
    from app.state import db
    from core.clock import utcnow

    monkeypatch.setattr(connect, "_is_connected", lambda wid, slug: True)
    _drafts(env)
    with db()() as s:
        s.add(ChatMessage(id="msg_automate", venture_id=env, workspace_id=s.get(Venture, env).workspace_id, role="assistant",
                          kind="confirmation", body_md="Automate?", created_at=utcnow(),
                          actions_json=json.dumps([{"label": "Automate all emails", "confirmation": {"action": "gtm.automate"}}])))
        s.commit()
    assert not any(item.key == "send" for item in readiness.missing(env))

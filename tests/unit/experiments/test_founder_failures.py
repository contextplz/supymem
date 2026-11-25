"""Founder failures stay honest, actionable and safe to log."""
import asyncio
import logging

import pytest
from pydantic import BaseModel
from tests.company_os_harness import local_owner_clients

from tests.unit.test_autonomous_experiments import credit_meter, market, run_jobs, venture


def test_schema_failure_logs_location_without_raw_idea_or_key(venture, monkeypatch, caplog):
    from app.experiments import ledger

    class ValidationPacket(BaseModel):
        proposed_tests: list[str]

    raw_idea = "private founder idea with sk-fixture-secret-token"

    def invalid_schema():
        ValidationPacket.model_validate({"proposed_tests": raw_idea})

    monkeypatch.setattr(ledger, "list_for_venture", lambda _venture_id: invalid_schema())
    with caplog.at_level(logging.ERROR, logger="app.chat.recovery"):
        assert ledger.summary_lines(venture[1]) == []
    record = next(record for record in caplog.records if getattr(record, "action", "") == "experiment.ledger.summary")
    assert record.venture_id == venture[1] and record.error_type == "ValidationError"
    assert record.validation_errors == [{"loc": ("proposed_tests",), "type": "list_type"}]
    assert record.exc_info is None or (record.exc_info[0] is Exception and record.exc_info[2] is None)
    assert "Traceback" not in caplog.text and "ValidationError" in caplog.text
    assert raw_idea not in caplog.text and "sk-fixture-secret-token" not in caplog.text
    assert raw_idea not in repr(record.__dict__)


def test_optional_flag_failure_stays_off_and_logs_safely(venture, monkeypatch, caplog):
    from app.core.local_mode import is_local
    from app.experiments import tools

    error = RuntimeError("sk-fixture-private-policy")

    def unavailable(_key):
        raise error

    monkeypatch.setattr(tools, "get_policy", unavailable)
    with caplog.at_level(logging.ERROR, logger="app.chat.recovery"):
        assert tools.enabled() is False
    if is_local():
        assert not caplog.records
    else:
        assert any(getattr(record, "action", "") == "experiment.tools.enabled" for record in caplog.records)
    assert "sk-fixture-private-policy" not in caplog.text


def test_activity_failure_logs_safely_and_rethrows_same_exception(venture, monkeypatch, caplog):
    from app.experiments import activity

    recorded = []
    monkeypatch.setattr(activity, "record_safely", lambda *args, **kwargs: recorded.append(kwargs))
    error = RuntimeError("private founder idea and sk-fixture-secret")
    context = {"venture_id": venture[1], "option_id": "option-fixture"}
    with caplog.at_level(logging.ERROR, logger="app.chat.recovery"):
        with pytest.raises(RuntimeError) as failure:
            with activity.track(context, action="search", tool="fixture", evidence_level="desk_research"):
                raise error
    assert failure.value is error and len(recorded) == 1
    assert recorded[0]["outcome"] == "failed"
    assert any(getattr(record, "venture_id", "") == venture[1] for record in caplog.records)
    assert str(error) not in caplog.text and "sk-fixture-secret" not in caplog.text


def test_invitation_pre_dispatch_failure_is_definite_without_guessed_cause(venture, monkeypatch, caplog):
    from app.approvals import external
    from app.experiments import page_tests
    from app.gtm.supymem.composio_gmail import DefiniteFailure

    error = RuntimeError("private founder idea and sk-fixture-secret")

    def unavailable(*args):
        raise error

    monkeypatch.setattr(external, "outbound_rate", unavailable)
    with caplog.at_level(logging.ERROR, logger="app.chat.recovery"):
        with pytest.raises(DefiniteFailure) as failure:
            page_tests.send_one(venture[1], venture[2], {}, None)
    assert failure.value.__cause__ is error
    assert "Nothing was sent" in str(failure.value) and "Retry" in str(failure.value)
    assert not any(word in str(failure.value).lower() for word in ("credits", "model", "usage limit", "—"))
    assert str(error) not in caplog.text and "sk-fixture-secret" not in caplog.text


@pytest.mark.parametrize("started", [False, True], ids=["before-call", "uncertain-call"])
def test_worker_failure_replay_never_duplicates_work_or_guesses_cause(market, credit_meter, monkeypatch, caplog, started):
    from app.core.local_mode import is_local
    from app.chat.store import list_messages
    from app.credits import metered
    from app.experiments import autonomous

    venture_id = market[1]
    spec = autonomous.available(venture_id)[0]
    error = RuntimeError("private founder idea and sk-fixture-secret")

    async def unavailable(*args, **kwargs):
        raise error

    if is_local() and not started:
        def unavailable_provider():
            raise error
    else:
        monkeypatch.setattr(autonomous if started else metered, "_scan" if started else "call", unavailable)
    queued = asyncio.run(autonomous.run(venture_id, {"experiment_id": spec["id"],
        "fingerprint": spec["fingerprint"], "ceiling_micro": spec["ceiling_micro"]}))
    assert queued["run_id"] and not credit_meter
    if is_local() and not started:
        monkeypatch.setattr(autonomous, "_provider", unavailable_provider)
    with caplog.at_level(logging.ERROR, logger="app.chat.recovery"):
        run_jobs()
    terminal = [message for message in list_messages(venture_id) if message["kind"] == "experiment_blocked"]
    assert len(terminal) == 1 and len(credit_meter) == (0 if is_local() else int(started))
    reply = terminal[0]
    assert "Choose Retry to check your saved work" in reply["body_md"]
    assert not any(word in reply["body_md"].lower() for word in ("credits", "model", "check your account", "—"))
    if started:
        assert all(action.get("action") != "experiment.prepare" for action in reply["actions"])
        assert any(action.get("href") == (f"/v/{venture_id}/experiments" if is_local() else "/account")
            or is_local() and action.get("action") == "experiment.list" for action in reply["actions"])
        assert any(action["label"] == "Retry" and (action.get("action") == "experiment.run_review"
            if is_local() else action.get("confirmation")) for action in reply["actions"])
    else:
        assert any(action["label"] == "Retry" and action["action"] == "experiment.prepare"
            and action["payload"] == {"experiment_id": spec["id"]} for action in reply["actions"])
    monkeypatch.setattr(autonomous, "_provider", lambda: market[5])
    autonomous.prepare(venture_id, spec["id"])
    assert len(credit_meter) == (0 if is_local() else int(started))
    assert "sk-fixture-secret" not in caplog.text and "private founder idea" not in caplog.text


@pytest.mark.parametrize("reply", [True, False], ids=["reply-retry", "consequential-recovery"])
@pytest.mark.parametrize("kind", ["unexpected", "policy", "credits", "provider"])
def test_api_failure_retry_keeps_payload_and_never_replays_consequential_action(venture, monkeypatch, caplog, reply, kind):
    from app.analytics import error_events, events
    from app.api import chat_api
    from app.chat import recovery
    from app.credits.errors import CreditsUnavailable, InsufficientCredits
    from llm_gateway.errors import ProviderCallError

    saved = {}

    def post_failure(_venture_id, **fields):
        saved.update(fields)

    monkeypatch.setattr(recovery, "post_failure", post_failure)
    monkeypatch.setattr(chat_api, "idempotent_message", lambda *args: saved)
    monkeypatch.setattr(error_events, "log_error_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(events, "emit", lambda *args, **kwargs: None)
    args = {"text": "private founder idea with sk-fixture-secret", "question_id": "question-fixture"}
    error = {"unexpected": RuntimeError("private failure with sk-fixture-secret"),
        "policy": CreditsUnavailable("private policy with sk-fixture-secret"),
        "credits": InsufficientCredits("private-account", 0, 1),
        "provider": ProviderCallError("model_not_configured", "private provider with sk-fixture-secret")}[kind]
    action = "chat.reply" if reply else "experiment.run"
    with caplog.at_level(logging.ERROR, logger="app.chat.recovery"):
        result = chat_api._action_failed_reply(venture[1], action, args, error, None, reply=reply)
    if kind == "credits":  # founder decision: at the limit, only the buy-more notice
        assert "buy more" in result["body_md"].lower()
        return
    retry = next(button for button in result["actions"] if button["label"] == "Retry")
    if reply:
        assert retry["action"] == "chat.reply" and retry["payload"] == args
    else:
        assert retry["action"] == recovery.ALTERNATIVE and all(button.get("action") != action for button in result["actions"])
    if kind in {"unexpected", "policy"}:
        assert "Choose Retry to check your saved work" in result["body_md"]
        assert not any(word in result["body_md"].lower() for word in ("model", "credits"))
    elif kind == "credits":
        assert "not enough credits" in result["body_md"].lower() and "model" not in result["body_md"].lower()
    else:
        assert "model" in result["body_md"].lower() and "credits" not in result["body_md"].lower()
    assert "—" not in result["body_md"]
    assert "sk-fixture-secret" not in caplog.text and "private founder idea" not in caplog.text
    records = [record for record in caplog.records if getattr(record, "venture_id", "") == venture[1]]
    assert len(records) == 1 and records[0].error_type == type(error).__name__


def test_unknown_gmail_status_keeps_blocker_and_retry_without_false_disconnect(venture, monkeypatch, caplog):
    from app.autopilot import guard
    from app.chat import connect
    from app.credits import accounts
    from app.needs_you import readiness, service

    error = RuntimeError("private connection with sk-fixture-secret")

    def unknown(*args):
        raise connect.ConnectionStatusUnavailable(error)

    monkeypatch.setattr(connect, "_is_connected", unknown)
    monkeypatch.setattr(guard, "get_settings", lambda _venture_id: {"god_mode": False, "allow_send": True})
    monkeypatch.setattr(readiness, "_ready_drafts", lambda _venture_id: 1)
    monkeypatch.setattr(readiness, "_server_off_note", lambda _venture_id: None)
    monkeypatch.setattr(accounts, "account_for_workspace", lambda _workspace_id: None)
    with caplog.at_level(logging.ERROR, logger="app.chat.recovery"):
        items = readiness.missing(venture[1])
    gmail = next(item for item in items if item.key == "gmail")
    assert "could not check" in gmail.what and "not connected" not in gmail.what
    assert any(action["label"] == "Retry" and action["action"] == "connections.status" and action["retry"] is True for action in gmail.actions)
    assert "sk-fixture-secret" not in caplog.text
    resolved = []
    monkeypatch.setattr(service, "resolve", lambda *args, **kwargs: resolved.append(args))
    monkeypatch.setattr(service, "resolve_ref", lambda *args, **kwargs: resolved.append(args))
    opened = readiness.sync(venture[1])
    assert opened["opened"] == 1 and not resolved
    monkeypatch.setattr(readiness, "_ready_drafts", lambda _venture_id: 0)
    assert readiness.sync(venture[1])["resolved"] == 0 and not resolved


@pytest.mark.parametrize("code", ["pagebuilder_provider_or_credits_unavailable", "pagebuilder_pricing_unavailable",
                                 "pagebuilder_credit_reservation_unavailable"])
def test_saved_ambiguous_page_failure_never_guesses_credit_or_model_cause(venture, code):
    from app.tasks.contracts import blocker

    result = blocker(code)
    assert result["class"] == "needs_auth"
    assert not any(word in result["reason"].lower() for word in ("model", "credits", "account", "—"))
    assert "retry" in result["unblock_action"].lower()


def test_active_belief_plan_pending_intake_only_reads_existing_work(venture, monkeypatch):
    from unittest.mock import AsyncMock, Mock

    from app.experiments import autonomous, belief_autopilot, belief_planner, beliefs, service
    from app.ventures import current_idea
    from core.clock import utcnow

    venture_id, workspace_id = venture[1:]
    now = utcnow()
    context = {"beliefs": [], "plan": {"state": "active", "tests": [
        {"experiment_id": "approved-running", "state": "running"},
        {"experiment_id": "queued-next", "state": "queued"},
    ]}}
    evaluate = Mock()
    read_results = Mock(return_value=context)
    pending = Mock(return_value=True)
    narrow = AsyncMock()
    run = AsyncMock()
    available = Mock(side_effect=AssertionError("Pending intake must not plan new tests"))
    note = Mock()
    monkeypatch.setattr(autonomous, "_snapshot", lambda _venture_id: {"workspace_id": workspace_id})
    monkeypatch.setattr(belief_autopilot, "enabled", lambda: True)
    monkeypatch.setattr(beliefs, "load", lambda _venture_id: context)
    monkeypatch.setattr(service, "run_evaluate", evaluate)
    monkeypatch.setattr(belief_autopilot, "_read_results", read_results)
    monkeypatch.setattr(current_idea, "intake_pending", pending)
    monkeypatch.setattr(belief_autopilot, "_narrow", narrow)
    monkeypatch.setattr(belief_planner, "available", available)
    monkeypatch.setattr(autonomous, "run", run)
    monkeypatch.setattr(belief_autopilot, "daily_note", note)

    asyncio.run(belief_autopilot.tick(venture_id, now=now))

    evaluate.assert_called_once()
    assert evaluate.call_args.args[0]["experiment_ids"] == ["approved-running"]
    read_results.assert_called_once_with(venture_id, now)
    pending.assert_called_once_with(venture_id)
    narrow.assert_not_awaited()
    available.assert_not_called()
    run.assert_not_awaited()
    note.assert_called_once_with(venture_id, now=now)

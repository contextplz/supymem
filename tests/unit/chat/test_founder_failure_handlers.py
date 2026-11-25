import asyncio
import json
import logging
import os
import sys
import time
from types import SimpleNamespace

import pytest
from tests.unit.chat.test_round3_matrix import venture


@pytest.mark.parametrize("private", ["Private warehouse supplier terms", "Private family trip plans"])
def test_local_failure_writes_only_scrubbed_diagnostics(chat, monkeypatch, tmp_path, private):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    try:
        try:
            raise ValueError(private)
        except ValueError as cause:
            raise KeyError(private) from cause
    except KeyError as error:
        reply = chat.founder_failure(error, private, "your reply", {"action": "status", "payload": {}})
    path = tmp_path / "logs/errors.jsonl"
    assert path.is_file()
    content = path.read_text()
    entry, = [json.loads(line) for line in content.splitlines()]
    assert set(entry) == {"time", "error_type", "frames", "code"}
    assert entry["error_type"] == "KeyError" and entry["code"] == "chat_action_failed"
    assert len(entry["frames"]) >= 2 and all(frame.startswith(__name__ + ":") for frame in entry["frames"])
    assert private not in content and str(tmp_path) not in content
    assert path.stat().st_mode & 0o077 == 0
    assert reply["body_md"] == "I couldn't prepare your reply. Choose Retry to check your saved work."


def test_local_diagnostics_rejects_symlink_without_breaking_failure_reply(chat, monkeypatch, tmp_path):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "data/logs"
    root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_text("Keep this data")
    (root / "errors.jsonl").symlink_to(outside)
    reply = chat.founder_failure(RuntimeError("private"), "venture", "your reply", {"action": "status"})
    assert outside.read_text() == "Keep this data"
    assert "Choose Retry to check your saved work." in reply["body_md"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX named pipes only")
def test_local_diagnostics_never_blocks_on_a_named_pipe(chat, monkeypatch, tmp_path):
    from app.core import local_process
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    directory = tmp_path / "logs"
    directory.mkdir()
    os.mkfifo(directory / "errors.jsonl", 0o600)
    original = local_process.open_private
    observed = []

    def private_open(path, flags):
        observed.append(flags)
        assert flags & os.O_NONBLOCK
        return original(path, flags)

    monkeypatch.setattr(local_process, "open_private", private_open)
    reply = chat.founder_failure(RuntimeError("private"), "venture", "your reply", {"action": "status"})
    assert observed and all(flags & os.O_NONBLOCK for flags in observed)
    assert "Choose Retry to check your saved work." in reply["body_md"]


@pytest.mark.parametrize("private", ["Private factory procurement terms", "Private family travel plans"])
@pytest.mark.parametrize("raised", [True, False], ids=["raised", "returned"])
def test_failure_diagnostics_name_safe_exception_origin(chat, caplog, private, raised):
    from core.logging import JsonFormatter

    error = RuntimeError(private)
    if raised:
        try:
            raise error
        except RuntimeError as caught:
            origin = f"{__name__}:{caught.__traceback__.tb_lineno}"
            chat.log_founder_exception(caught, "venture", "chat.reply")
    else:
        origin = f"{__name__}:{sys._getframe().f_lineno + 1}"
        chat.log_founder_exception(error, "venture", "chat.reply")
    record, = caplog.records
    assert record.levelno == logging.ERROR
    assert record.error_location == origin
    assert record.diagnostic_code == "chat_action_failed"
    assert "RuntimeError" in caplog.text and origin in caplog.text
    assert private not in caplog.text and private not in JsonFormatter().format(record)


@pytest.mark.parametrize("private", ["Private supplier inventory", "Private family meal plans"])
@pytest.mark.parametrize("reason", ["timeout", "cancelled", "empty_reply", "chat_reply_missing", "lease_lost"])
def test_reply_failure_diagnostics_name_the_reason_without_private_text(chat, caplog, private, reason):
    from app.core.jobs import FatalError
    from core.logging import JsonFormatter

    error = {"timeout": lambda: TimeoutError(private),
             "cancelled": lambda: asyncio.CancelledError(private),
             "empty_reply": lambda: RuntimeError("empty_reply"),
             "chat_reply_missing": lambda: ValueError("chat_reply_missing"),
             "lease_lost": lambda: FatalError("recovery_lease_lost")}[reason]()
    chat.log_founder_exception(error, "venture", "chat.reply")
    record, = caplog.records
    assert record.reason_code == {"timeout": "chat_reply_timeout", "cancelled": "chat_reply_cancelled",
                                 "empty_reply": "chat_reply_empty", "chat_reply_missing": "chat_reply_empty",
                                 "lease_lost": "chat_reply_lease_lost"}[reason]
    assert record.levelno == logging.ERROR
    assert private not in JsonFormatter().format(record)


@pytest.mark.parametrize("private", ["Private supplier inventory", "Private family meal plans"])
def test_reply_timeout_checks_saved_work_instead_of_repeating_the_request(chat, monkeypatch, private):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    reply = chat.founder_failure(TimeoutError(private), "venture", "your reply",
                                {"action": "chat.reply", "payload": {"text": private}})
    assert reply["actions"][0]["action"] == "status"
    assert "saved" in reply["body_md"] and "Something went wrong" not in reply["body_md"]
    assert private not in str(reply)


@pytest.mark.parametrize("audience", ["Procurement teams comparing supplier terms", "Families choosing weekend dance lessons"])
def test_invitation_limits_failure_keeps_its_safe_next_step(chat, monkeypatch, caplog, audience):
    from app.autopilot import outreach

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    with pytest.raises(ValueError) as failed:
        outreach._template("a new service", audience, "", request=(
            f"Interview invitations for {audience} about their experience. Test: a private interview. "
            "Plan: Invite participants to a private 20-minute interview. Never collect passwords. "
            "Keep only anonymous notes with consent. Delete notes after review."), word_budget=5)
    reply = chat.founder_failure(failed.value, "venture", "your reply",
                                {"action": "chat.reply", "payload": {"text": "Prepare interview invitations"}})
    assert "Something went wrong" not in reply["body_md"]
    assert "shorten" in reply["body_md"].lower() and "keeping your limits" in reply["body_md"]
    assert not reply["actions"]
    record = next(record for record in caplog.records if record.getMessage() == "founder_action_failed")
    assert record.reason_code == "outreach_invitation_limits"
    assert audience not in caplog.text


@pytest.mark.parametrize("private", ["Private factory procurement terms", "Private family travel plans"])
@pytest.mark.parametrize("cause_kind", ["timeout", "provider", "storage"])
def test_retryable_diagnostics_classify_causes_without_private_messages(chat, caplog, private, cause_kind):
    from app.core.jobs import RetryableError
    from core.logging import JsonFormatter
    from llm_gateway.errors import ProviderCallError

    cause = {"timeout": lambda: TimeoutError(private),
             "provider": lambda: ProviderCallError("rate_limited", private),
             "storage": lambda: OSError(private)}[cause_kind]()
    error = RetryableError("gtm_enrichment_deferred")
    error.__cause__ = cause
    chat.log_founder_exception(error, "venture", "gtm.reachable.enrich")
    record, = caplog.records
    assert record.error_code == "gtm_enrichment_deferred"
    assert record.cause_type == type(cause).__name__
    assert record.cause_code == ("rate_limited" if cause_kind == "provider" else None)
    assert private not in caplog.text and private not in repr(record.__dict__)
    assert private not in JsonFormatter().format(record)


@pytest.mark.parametrize("private", ["Private factory procurement terms", "Private family travel plans"])
def test_local_model_recovery_names_the_key_and_screen(chat, monkeypatch, caplog, private):
    from app.llm.founder_provider import FounderLLMConfigError

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    reply = chat.founder_failure(FounderLLMConfigError(private), "venture", "your reply",
                                {"action": "chat.reply", "payload": {"text": "Explain my notes"}})
    assert "Settings > Keys and connections > Your AI provider" in reply["body_md"]
    assert "choose your provider and chat model" in reply["body_md"]
    assert "save your API key if required" in reply["body_md"]
    assert private not in str(reply) and private not in caplog.text


@pytest.mark.parametrize("private", ["Private dental servicing offer", "Private pottery membership offer"])
@pytest.mark.parametrize("extra_field", [False, True], ids=["nested-values", "unknown-field"])
def test_validation_failure_logs_only_field_locations_and_types(chat, caplog, private, extra_field):
    from core.logging import JsonFormatter
    from pydantic import BaseModel, ConfigDict, ValidationError

    class Packet(BaseModel):
        model_config = ConfigDict(extra="forbid")
        prices: list[int]

    secret = private + " credential-fixture-abc123"
    with pytest.raises(ValidationError) as failure:
        Packet.model_validate({"prices": [], secret: secret} if extra_field else {"prices": [secret, secret]})
    with caplog.at_level(logging.ERROR, logger="app.chat.recovery"):
        chat.log_founder_exception(failure.value, "venture", "experiment.start")
    record, = [record for record in caplog.records if record.getMessage().startswith("founder_action_failed")]
    assert record.validation_errors == ([{"loc": ("<extra>",), "type": "extra_forbidden"}] if extra_field else [
        {"loc": ("prices", 0), "type": "int_parsing"},
        {"loc": ("prices", 1), "type": "int_parsing"},
    ])
    assert secret not in caplog.text and secret not in repr(record.__dict__)
    assert secret not in JsonFormatter().format(record)
    assert record.exc_info is None or record.exc_info[2] is None


@pytest.mark.parametrize("private", ["Private manufacturing price", "Private hiking membership price"])
def test_validation_failure_plain_message_names_field_without_values(chat, caplog, private):
    from app.idea.validation_agent import ProposedTest
    from pydantic import ValidationError

    secret = private + " credential-fixture-xyz789"
    with pytest.raises(ValidationError) as failure:
        ProposedTest.model_validate({"hypothesis": "You can learn about willingness to pay.",
            "action": secret * 100, "success_measure": "Pass with a dated commitment."})
    with caplog.at_level(logging.ERROR, logger="app.chat.recovery"):
        chat.log_founder_exception(failure.value, "venture", "experiment.start")
    record, = caplog.records
    message = record.getMessage()
    assert '"loc": ["action"]' in message and '"type": "string_too_long"' in message
    assert secret not in message and private not in caplog.text


@pytest.mark.parametrize("idea", ["Invoice matching for manufacturers", "Holiday planning for families"])
def test_failed_review_recovery_keeps_task_and_real_next_step(chat, venture, monkeypatch, idea):
    import asyncio
    from app.chat import blockers, recovery
    payload = {"step": "idea.board", "code": "job_failed", "error_code": "chat_reply_failed"}

    async def unavailable(*args, **kwargs):
        raise AssertionError("A setup failure must not dispatch another model")

    monkeypatch.setattr(recovery, "_llm", unavailable)
    from app.api import chat_api
    failures = []
    monkeypatch.setattr("app.analytics.error_events.log_error_event", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.analytics.events.emit", lambda *args, **kwargs: None)
    monkeypatch.setattr(recovery, "post_failure", lambda *args, **kwargs: failures.append(kwargs))
    original_message = chat_api.idempotent_message
    monkeypatch.setattr(chat_api, "idempotent_message", lambda *args, **kwargs: {"body_md": "Your review failed. Report a problem."})
    chat_api._action_failed_reply("ven_fixture", "chat.reply", {"text": "Pressure-test my idea"}, RuntimeError(idea), None, reply=True)
    assert failures[0]["step"] == "idea.board"
    monkeypatch.setattr(chat_api, "idempotent_message", original_message)
    client, venture_id, _workspace_id = venture
    from app.state import db
    from app.db.models import Venture
    with db()() as session:
        session.get(Venture, venture_id).raw_idea = idea
        session.commit()
    for reply in [blockers.explain("ven_fixture", payload),
                  asyncio.run(recovery.alternative("ven_fixture", payload, workspace_id="w_fixture"))]:
        assert "review" in reply["body_md"].lower()
        assert any(action.get("href") == "/account" for action in reply["actions"])
        assert "model settings" not in reply["body_md"].lower()
    for action in ("blocker.explain", "recovery.alternative"):
        response = client.post(f"/ventures/{venture_id}/chat/confirm", json={"action": action, "payload": payload})
        assert response.status_code == 200
        assert any(item.get("href") == "/account" for item in response.json()["actions"]), response.json()

from tests.company_os_harness import load_company_os_app


PRIVATE = "private founder idea sk-secret-credential"


@pytest.fixture
def chat():
    load_company_os_app()
    from app.chat import recovery

    return recovery


def failure(kind):
    from app.credits.errors import CreditsUnavailable, InsufficientCredits
    from app.llm.founder_provider import FounderLLMConfigError
    from llm_gateway.errors import ProviderCallError
    from llm_gateway.hosted import HostedCreditsRequired

    return {
        "runtime": lambda: RuntimeError(PRIVATE),
        "value": lambda: ValueError(PRIVATE),
        "timeout": lambda: TimeoutError(PRIVATE),
        "metering": lambda: CreditsUnavailable(PRIVATE),
        "credits": lambda: InsufficientCredits(PRIVATE, 0, 1000),
        "hosted_credits": lambda: HostedCreditsRequired(PRIVATE),
        "provider": lambda: ProviderCallError("provider_down", PRIVATE),
        "provider_unknown": lambda: ProviderCallError("provider_outcome_unknown", PRIVATE),
        "configuration": lambda: FounderLLMConfigError(PRIVATE),
        "validation": validation_failure,
    }[kind]()


def validation_failure():
    from pydantic import ValidationError, create_model

    model = create_model("PrivateInput", count=(int, ...))
    with pytest.raises(ValidationError) as caught:
        model.model_validate({"count": PRIVATE})
    return caught.value


@pytest.mark.parametrize("code", ["provider_down", PRIVATE, [PRIVATE]])
def test_logs_keep_only_known_error_codes(chat, caplog, code):
    from llm_gateway.errors import ProviderCallError

    error = ProviderCallError(code, PRIVATE)
    chat.founder_failure(error, "venture", "your request", {"action": "status", "payload": {}})
    record = next(record for record in caplog.records if record.getMessage() == "founder_action_failed")
    assert record.error_code == ("provider_down" if code == "provider_down" else None)
    assert PRIVATE not in caplog.text


@pytest.mark.parametrize("detail", ["Private dispatch pricing: $38 per team", "Private family appointment: next Tuesday"])
def test_logs_withhold_traceback_source_and_chained_private_messages(chat, caplog, detail):
    try:
        try:
            raise ValueError(detail)
        except ValueError as cause:
            raise RuntimeError("PRIVATE " + detail) from cause
    except RuntimeError as error:
        chat.log_founder_exception(error, "venture", "status")
    record = next(record for record in caplog.records if record.getMessage() == "founder_action_failed")
    assert record.error_type == "RuntimeError" and record.error_code is None
    assert record.exc_info is None or record.exc_info[2] is None
    assert detail not in caplog.text and "PRIVATE" not in caplog.text and "Traceback" not in caplog.text


@pytest.mark.parametrize("idea", ["A maintenance scheduler for repair shops", "A stargazing planner for families"])
@pytest.mark.parametrize("code", ["timeout", "provider_outcome_unknown"])
def test_uncertain_failure_retry_only_checks_saved_work(chat, caplog, idea, code):
    from app.core.local_mode import is_local
    from llm_gateway.errors import ProviderCallError

    error = TimeoutError(PRIVATE) if code == "timeout" else ProviderCallError(code, PRIVATE)
    reply = chat.founder_failure(error, "venture", idea,
                                {"action": "pagebuilder.build", "payload": {"request": idea}})
    assert reply["actions"] == [{"action": "status", "payload": {}, "label": "Retry", "retry": True}]
    if is_local() and code == "provider_outcome_unknown":
        assert "Settings > Keys and connections > Your AI provider" in reply["body_md"]
        assert "Check your saved work before trying again. Nothing was retried." in reply["body_md"]
    else:
        assert reply["body_md"] == "I couldn't confirm what finished. Choose Retry to check your saved work."
    assert PRIVATE not in reply["body_md"] and PRIVATE not in caplog.text


@pytest.mark.parametrize("kind, mentions", [
    ("runtime", ""), ("value", ""), ("validation", ""), ("timeout", ""), ("metering", ""),
    ("credits", ""), ("hosted_credits", ""),
    ("provider", "model"), ("configuration", "model"),
])
def test_saved_audience_failure_classification_and_retry_payload(chat, monkeypatch, caplog, kind, mentions):
    from app.chat import conversations, customers, experiments, market_beliefs
    from app.experiments import autonomous
    from app.gtm import journey
    from app.state import MEM

    options = {"day_one": True, "max_emails_per_day": 0, "weekly_budget_micro": 1000}
    asked = {"at": time.time(), "field": "who", "experiment_request": {
        "action": "experiment.beliefs.prepare", "payload": options,
        "direction": "current", "conversation_id": None}}
    monkeypatch.setattr(conversations, "current_conversation", lambda: None)
    monkeypatch.setattr(autonomous, "_snapshot", lambda _: {
        "workspace_id": "workspace", "direction": "current", "journey": {"icp": {}}})
    saved = []
    monkeypatch.setattr(journey, "update_icp", lambda *args, **kwargs: saved.append((args, kwargs)))
    monkeypatch.setitem(MEM, customers._ASKED.format("venture"), asked)

    async def unavailable(*args, **kwargs):
        raise failure(kind)

    monkeypatch.setattr(market_beliefs, "prepare", unavailable)
    with caplog.at_level(logging.ERROR):
        reply = asyncio.run(experiments.answer_audience("venture", "Teachers", asked))
    assert saved[0][1]["start"] is False
    assert customers._ASKED.format("venture") not in MEM
    if kind in {"credits", "hosted_credits"}:
        from app.credits.errors import LIMIT_MESSAGE
        assert reply["body_md"] == LIMIT_MESSAGE
        assert reply["actions"] == [{"label": "Buy more", "href": "/v/venture/settings#settings-credits", "style": "primary", "kind": "recovery_link"}]
    else:
        assert "Your answer was saved" in reply["body_md"] and "Check Experiments for any started work" in reply["body_md"]
        assert "Nothing was launched" not in reply["body_md"]
        assert reply["actions"] == [{"label": "Retry", "action": "status" if kind == "timeout" else "experiment.beliefs.prepare",
                                     "payload": {} if kind == "timeout" else options, "retry": True}]
        for word in ("credits", "model"):
            assert (word in reply["body_md"].lower()) is (word == mentions)
    assert PRIVATE not in reply["body_md"] and PRIVATE not in caplog.text
    assert "founder_action_failed" in caplog.text
    assert "\u2014" not in reply["body_md"]


@pytest.mark.parametrize("kind", ["runtime", "provider", "credits", "provider_unknown"])
@pytest.mark.parametrize("idea", ["Inventory alerts for warehouse operators", "Walking groups for neighborhood families"])
@pytest.mark.parametrize("mode", ["hosted", "local"])
def test_failed_rewrite_retries_status_not_another_write(chat, monkeypatch, kind, idea, mode, tmp_path):
    import hashlib
    from app.chat import site_actions
    from app.firstlook import service, versions

    monkeypatch.setenv("SUPYMEM_MODE", mode)
    monkeypatch.setattr(site_actions, "_venture", lambda _: ("workspace", idea, "Venture"))
    monkeypatch.setattr(site_actions, "_saved_page_builds", lambda *args: [])
    monkeypatch.setattr(site_actions, "_local_page_setup", lambda actor: None)
    path = tmp_path / "index.html"
    path.write_text("<html><body>Your saved page</body></html>")
    monkeypatch.setattr(versions, "get_latest", lambda *args, **kwargs: SimpleNamespace(
        id="saved-version", workspace_id="workspace", venture_id="venture", sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        copy={"headline": "Saved headline"}, archetype="saas", stage="concept", path=str(path)))

    calls = []
    def unavailable(*args, **kwargs):
        calls.append(mode)
        raise failure(kind)

    if mode == "local":
        monkeypatch.setattr(service, "prepare_local_page", unavailable)
    else:
        async def hosted_unavailable(*args, **kwargs):
            return unavailable(*args, **kwargs)
        monkeypatch.setattr(service, "generate_first_look", hosted_unavailable)
    reply = asyncio.run(site_actions._rewrite("venture", {"instruction": f"Improve the introduction for {idea}"}, "founder"))
    assert calls == [mode]
    if kind == "provider_unknown":
        assert reply["actions"] == []
        if mode == "local":
            assert "did not return a confirmed result" in reply["body_md"]
            assert "Settings > Keys and connections > Your AI provider" in reply["body_md"]
        else:
            assert "the provider outcome is not confirmed" in reply["body_md"]
            assert "nothing is automatically replayed" in reply["body_md"]
    elif kind == "credits":
        from app.credits.errors import LIMIT_MESSAGE
        assert reply["body_md"] == LIMIT_MESSAGE
        assert reply["actions"] == [{"label": "Buy more", "href": "/v/venture/settings#settings-credits", "style": "primary", "kind": "recovery_link"}]
    else:
        assert reply["actions"] == [{"label": "Retry", "action": "site.preview", "payload": {}, "retry": True}]
    assert reply["kind"] == "needs_you" and "unchanged" not in reply["body_md"]
    if kind != "credits":
        assert "Check the saved draft" in reply["body_md"] and "Nothing was published" in reply["body_md"]
    assert PRIVATE not in reply["body_md"]


def test_optional_publish_status_failure_does_not_offer_another_build(chat, monkeypatch, caplog):
    from app.chat import site_actions

    def unavailable(*args, **kwargs):
        raise RuntimeError(PRIVATE)

    monkeypatch.setattr(site_actions, "publish_preflight", unavailable)
    with caplog.at_level(logging.ERROR):
        body, actions = site_actions.after_waitlist("venture")
    assert "could not check" in body
    assert actions == [{"label": "Retry", "action": "site.preview", "payload": {}, "retry": True}]
    assert "nothing to publish" not in body.lower()
    assert PRIVATE not in caplog.text


def test_browser_lost_ack_retries_saved_status_without_repeating_mutation(chat, monkeypatch):
    from app.build import device_orders
    from app.chat import browser_prepare, link_context, store
    from app.tools import ToolContext

    caller = SimpleNamespace(legacy=False, role="user", device_id="device", sub="founder")
    conversation = SimpleNamespace(id="conversation", visibility="private", venture_id="venture",
                                   workspace_id="workspace", actor_id="person")
    monkeypatch.setattr(browser_prepare, "subject_person_id", lambda _: "person")
    monkeypatch.setattr(browser_prepare, "current_conversation", lambda: conversation)
    monkeypatch.setattr(link_context, "_safe_host", lambda _: True)
    prepared = []

    def prepare(*args, **kwargs):
        prepared.append(args)
        return {"id": "saved_review"}

    def lost_ack(*args, **kwargs):
        raise OSError(PRIVATE)

    monkeypatch.setattr(device_orders, "prepare_browser_task", prepare)
    monkeypatch.setattr(store, "append_message", lost_ack)
    context = ToolContext("venture", "workspace")
    with browser_prepare.bind_caller(caller):
        body = browser_prepare.prepare_founder_review("Check https://example.com/pricing", context)
    assert len(prepared) == 1
    assert "Nothing was approved or started" in body and "Check saved Mac tasks" in body
    assert context.actions[0] == {"label": "Retry", "action": "chat.reply", "text": "What happened in the latest Mac browser task?", "retry": True}
    assert all(action.get("action") != "mac.task.prepare" for action in context.actions)
    assert PRIVATE not in body


@pytest.mark.parametrize("history", [False, True])
def test_saved_fact_failure_preserves_recall_or_history_query(chat, monkeypatch, history):
    import app.state
    from app.chat import memory_commands

    def unavailable():
        raise RuntimeError(PRIVATE)

    monkeypatch.setattr(app.state, "db", unavailable)
    reply = memory_commands.fact_reply("venture", "idea.audience", history=history)
    action = reply["actions"][0]
    assert action["label"] == "Retry" and action["action"] == "chat.reply"
    command = memory_commands.parse(action["text"])
    assert command.action == ("memory.history" if history else "memory.recall")
    assert command.target == "idea.audience"
    assert PRIVATE not in reply["body_md"]


def test_optional_rephrase_error_keeps_original_and_logs_safely(chat, monkeypatch, caplog):
    from app.chat import decisions, guard, rephrase, store

    monkeypatch.setenv("CHAT_REPHRASE", "on")
    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setenv("LLM_CASSETTE_MODE", "off")
    monkeypatch.setattr(guard, "_pending_card_label", lambda _: None)
    monkeypatch.setattr(decisions, "pending_for_venture", lambda _: [])
    monkeypatch.setattr(store, "list_messages", lambda *args, **kwargs: [])

    async def unavailable(*args, **kwargs):
        raise RuntimeError(PRIVATE)

    monkeypatch.setattr(rephrase, "_ask_model", unavailable)
    text = "do that for the second one"
    with caplog.at_level(logging.ERROR):
        assert asyncio.run(rephrase.prepare("venture", text, workspace_id="workspace")) == (text, text)
    assert "founder_action_failed" in caplog.text and PRIVATE not in caplog.text


def test_unknown_recording_error_is_not_blame_for_an_existing_result(chat, monkeypatch):
    from app.chat import experiments
    from app.experiments import ledger

    monkeypatch.setattr(ledger, "autopilot_enabled", lambda: False)

    def unavailable(*args, **kwargs):
        raise RuntimeError(PRIVATE)

    monkeypatch.setattr(ledger, "record_result", unavailable)
    reply = asyncio.run(experiments.record_run("venture", {"experiment_id": "experiment", "outcome": "supported",
        "result_text": "I ran the test. Three people signed up."}))
    assert "already has a recorded result" not in reply["body_md"]
    assert "Check the saved result" in reply["body_md"]
    assert reply["actions"] == [{"label": "Retry", "action": "experiment.list", "payload": {"experiment_id": "experiment"}, "retry": True}]


def test_optional_mac_lookup_failure_is_not_no_task(chat, monkeypatch):
    from app.chat import stop_work
    from app.ventures import state_brief

    def unavailable(*args, **kwargs):
        raise RuntimeError(PRIVATE)

    monkeypatch.setattr(state_brief, "get_venture_state_brief", unavailable)
    reply = stop_work.route("venture", "Cancel my Mac task")
    assert "No Mac task" not in reply["body_md"]
    assert reply["actions"][0]["href"] == "/v/venture/settings#settings-computer-use"


def test_host_check_failure_does_not_show_internal_exception_names(chat, monkeypatch, caplog):
    from app.chat import hosting

    def unavailable(*args, **kwargs):
        raise RuntimeError(PRIVATE)

    monkeypatch.setattr(hosting, "_get", unavailable)
    with caplog.at_level(logging.ERROR):
        result = hosting.check("https://example.com/")
    assert result["ok"] is False
    assert "RuntimeError" not in result["reason"] and "try again" in result["reason"]
    assert PRIVATE not in caplog.text


def test_gmail_lookup_failure_abstains_from_stale_connection_claim_and_offers_retry(chat, monkeypatch):
    from app.chat import connect
    from app.gtm.supymem import pipeline

    def unavailable(*args, **kwargs):
        raise RuntimeError(PRIVATE)

    monkeypatch.setattr(pipeline, "mail_readiness", unavailable)
    brief = {"workspace_id": "workspace", "connections": {"gmail": {"connected": True}}}
    with pytest.raises(connect.ConnectionStatusUnavailable):
        connect.gmail_status(brief)
    reply = connect.gmail_status_reply("venture", brief)
    assert "could not check your Gmail connection" in reply["body_md"]
    assert "Gmail is not connected" not in reply["body_md"]
    assert "Gmail is connected" not in reply["body_md"]
    assert reply["actions"] == [{"label": "Retry", "action": "connections.status", "payload": {}, "retry": True}]


def test_unknown_connection_lookup_does_not_start_authorization(chat, monkeypatch):
    from app.chat import connect
    from app.connections import hub, mailboxes
    from app.gtm.supymem import pipeline

    def unavailable(*args, **kwargs):
        raise RuntimeError(PRIVATE)

    def forbidden(*args, **kwargs):
        pytest.fail("Unknown connection status must not start another authorization")

    monkeypatch.setattr(connect, "_scope", lambda _: ("workspace", "founder@example.com"))
    monkeypatch.setattr(mailboxes, "enabled", lambda: False)
    monkeypatch.setattr(pipeline, "mail_readiness", unavailable)
    monkeypatch.setattr(hub, "_smtp_card", unavailable)
    monkeypatch.setattr(hub, "connect_provider", forbidden)
    with pytest.raises(connect.ConnectionStatusUnavailable):
        connect._is_connected("workspace", "gmail")
    reply = connect.handle("venture", "connect gmail")
    assert reply["actions"] == [{"label": "Retry", "action": "connections.status", "payload": {}, "retry": True}]
    assert "not connected" not in reply["body_md"]


def test_unknown_watched_connection_keeps_polling_without_false_escalation(chat, monkeypatch):
    from app.chat import connect
    from app.connections import hub, mailboxes
    from app.gtm.supymem import pipeline

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, *args):
            return SimpleNamespace(body_md=json.dumps({"workspace_id": "workspace",
                "started": "2000-01-01T00:00:00+00:00", "escalated": False}))

    def unavailable(*args, **kwargs):
        raise RuntimeError(PRIVATE)

    def forbidden(*args, **kwargs):
        pytest.fail("Unknown connection status must not claim a reconnect blocker")

    monkeypatch.setattr(connect, "db", lambda: Session)
    monkeypatch.setattr(connect, "_escalate", forbidden)
    monkeypatch.setattr(mailboxes, "enabled", lambda: False)
    monkeypatch.setattr(pipeline, "mail_readiness", unavailable)
    monkeypatch.setattr(hub, "_smtp_card", unavailable)
    assert connect.check("venture", "gmail") == "waiting"


def test_connection_status_failure_offers_read_only_retry(chat, monkeypatch):
    from app.chat import connect
    from app.connections import hub

    def unavailable(*args, **kwargs):
        raise RuntimeError(PRIVATE)

    monkeypatch.setattr(connect, "_scope", lambda _: ("workspace", "founder@example.com"))
    monkeypatch.setattr(hub, "hub_status", unavailable)
    reply = connect.status("venture")
    assert reply["actions"] == [{"label": "Retry", "action": "connections.status", "payload": {}, "retry": True}]
    assert "Not connected" not in reply["body_md"] and "Nothing was changed" in reply["body_md"]


def test_partial_brief_does_not_claim_unchecked_activity_or_pause_state(chat):
    from app.chat.brief import render_brief

    body = render_brief({"name": "Venture", "paused": None, "live_status_unconfirmed": True})
    assert "Nobody is mid-task" not in body and "autopilot on" not in body
    assert "Nothing needs you" not in body
    assert "Live teammate activity could not be checked" in body

"""Core chat failures remain private, actionable and honest about uncertain effects."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tests.unit.gtm.test_journey import _add, _mail, journey_db as journey_db


@pytest.fixture
def core(journey_db):
    from app.chat import assistant, customers, execute, llm_router

    return SimpleNamespace(assistant=assistant, customers=customers, execute=execute, router=llm_router,
                           data=journey_db)


def _fail(error):
    raise error


def _assert_retry(reply, action, payload=None):
    from app.chat.actions import disabled_reason

    retry = next(button for button in reply["actions"] if button["label"] == "Retry")
    assert retry["action"] == action and retry["payload"] == (payload or {})
    assert disabled_reason(retry["action"]) is None
    body = reply["body_md"].lower()
    assert "credit" not in body and "model" not in body and "private" not in body
    assert "nothing was" not in body and "unchanged" not in body
    assert reply.get("action_started") is not False


def _assert_private_log(caplog, venture_id, error_type):
    record = next(record for record in reversed(caplog.records) if record.msg == "founder_action_failed")
    assert record.venture_id == venture_id and record.action
    assert record.error_type == error_type
    assert record.exc_info is None or (record.exc_info[0] is Exception and record.exc_info[2] is None)
    assert "Traceback" not in (record.exc_text or "")
    assert "PRIVATE" not in caplog.text and "PRIVATE" not in repr(record.__dict__)


@pytest.mark.parametrize("error_type", [ValueError, TypeError, RuntimeError])
def test_customer_preparation_failure_does_not_guess_credit_or_no_work(core, monkeypatch, caplog, error_type):
    from app.gtm import journey

    error = error_type("gtm_insufficient_credits PRIVATE idea and credential")
    monkeypatch.setattr(journey, "prepare", lambda *args, **kwargs: _fail(error))
    reply = asyncio.run(core.customers.execute(core.data["vid"], "gtm.find", {}, core.data["actor"]))
    _assert_retry(reply, "gtm.status")
    assert "could not confirm whether the search started" in reply["body_md"]
    _assert_private_log(caplog, core.data["vid"], error_type.__name__)


@pytest.mark.parametrize("error_type", [ValueError, RuntimeError])
def test_draft_failure_after_save_does_not_claim_unchanged(core, monkeypatch, caplog, error_type):
    from app.autopilot import outreach
    from app.gtm import journey

    data = core.data
    _add(data, monkeypatch)
    _mail(data)
    before = core.customers._pending(core.customers._view(data["vid"]))[0]
    patch = journey.patch_draft
    error = error_type("PRIVATE post-save failure")

    def save_then_fail(*args, **kwargs):
        patch(*args, **kwargs)
        _fail(error)

    sent = []
    monkeypatch.setattr(outreach, "_send_one", lambda *args, **kwargs: sent.append(args))
    monkeypatch.setattr(journey, "patch_draft", save_then_fail)
    reply = asyncio.run(core.customers.execute(data["vid"], "gtm.draft_edit",
        {"doc_id": before["doc_id"], "edit": "first_line", "line": "Hello team,"}, data["actor"]))
    _assert_retry(reply, "gtm.status", {"show_saved": True})
    current = next(draft for draft in core.customers._view(data["vid"])["drafts"] if draft["doc_id"] == before["doc_id"])
    assert current["body"].startswith("Hello team,") and current["revision"] != before["revision"]
    assert current["review_state"] == "draft" and not sent
    _assert_private_log(caplog, data["vid"], error_type.__name__)


def test_send_readback_failure_leaves_review_not_another_send(core, monkeypatch, caplog):
    from app.gtm import journey

    _mail(core.data)
    attempts = []
    error = TypeError("PRIVATE failed after dispatch")

    async def send(*args, **kwargs):
        attempts.append(args)
        _fail(error)

    monkeypatch.setattr(journey, "send", send)
    reply = asyncio.run(core.customers.execute(core.data["vid"], "gtm.send", {"batch_id": "batch"}, core.data["actor"]))
    _assert_retry(reply, "gtm.status")
    assert len(attempts) == 1 and "could not confirm which emails were sent" in reply["body_md"]
    _assert_private_log(caplog, core.data["vid"], "TypeError")


def test_customer_status_failure_does_not_claim_saved_results_or_repeat_invalid_payload(core, monkeypatch, caplog):
    error = ValueError("PRIVATE unreadable records")
    monkeypatch.setattr(core.customers, "_view", lambda *args: _fail(error))
    reply = asyncio.run(core.customers.execute(core.data["vid"], "gtm.status", {"offset": "invalid"}, core.data["actor"]))
    _assert_retry(reply, "gtm.status")
    assert "saved search could not be read" in reply["body_md"].lower()
    assert "saved work is kept" not in reply["body_md"].lower()
    _assert_private_log(caplog, core.data["vid"], "ValueError")


def test_optional_library_failure_keeps_web_fallback(core, monkeypatch, caplog):
    from app.lead_library import service

    error = RuntimeError("PRIVATE library failure")
    monkeypatch.setattr(service, "match", lambda *args, **kwargs: _fail(error))
    assert core.customers._library_section(core.data["vid"], {"who": "Clinic owners"}) == ("", [])
    _assert_private_log(caplog, core.data["vid"], "RuntimeError")


@pytest.mark.parametrize("gate", [False, True])
def test_suggestion_failure_keeps_optional_gate_or_retry_with_audience_question(core, monkeypatch, caplog, gate):
    error = ValueError("PRIVATE audience suggestions")
    saved = {"kind": "text", "body_md": "Review the saved audience", "actions": []}
    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setenv("LLM_CASSETTE_MODE", "off")
    monkeypatch.setattr(core.customers, "render", lambda *args: saved)

    async def suggest(*args):
        _fail(error)

    monkeypatch.setattr(core.customers, "_suggest", suggest)
    reply = asyncio.run(core.customers.icp_reply(core.data["vid"], {"icp": {"who": "Clinic owners"}}, gate=gate))
    if gate:
        assert reply is saved
    else:
        retry = next(action for action in reply["actions"] if action["label"] == "Retry")
        _assert_retry(reply, "chat.reply", retry["payload"])
        assert "Who are you trying to reach?" in reply["body_md"]
        assert "Suggest three specific groups" in retry["payload"]["text"]
    _assert_private_log(caplog, core.data["vid"], "ValueError")


def test_research_failure_after_queue_keeps_uncertainty_and_status_retry(core, monkeypatch, caplog):
    from app.autopilot import receipts
    from app.research import service
    from app.ventures import build_prerequisites, journey_overrides, state_brief

    queued = []
    error = RuntimeError("research_insufficient_credits PRIVATE unrelated readback")
    monkeypatch.setattr(build_prerequisites, "answers_pending", lambda *args: False)
    monkeypatch.setattr(journey_overrides, "read", lambda *args, **kwargs: {"skipped": ["validation"]})
    monkeypatch.setattr(service, "start", lambda *args, **kwargs: {"job_id": "queued-job"})
    monkeypatch.setattr(receipts, "queued_job", queued.append)
    monkeypatch.setattr(state_brief, "invalidate_venture_state_brief", lambda *args: _fail(error))
    reply = core.execute._research_start(core.data["vid"], core.data["actor"])
    _assert_retry(reply, "status")
    assert queued == ["queued-job"] and "could not confirm whether research started" in reply["body_md"]
    _assert_private_log(caplog, core.data["vid"], "RuntimeError")


def test_optional_brief_failure_keeps_empty_fallback(core, monkeypatch, caplog):
    from app.ventures import state_brief

    error = TypeError("PRIVATE state brief failure")
    monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda *args, **kwargs: _fail(error))
    assert core.execute._brief_or_empty(core.data["vid"]) == {}
    _assert_private_log(caplog, core.data["vid"], "TypeError")
    missing = asyncio.run(core.assistant.narrate_action_result(core.data["vid"], question="What happened?", action_result={}))
    assert missing["actions"] == [{"label": "Retry", "action": "status", "payload": {}, "retry": True}]
    assert "No result was recorded" in missing["body_md"]


def test_optional_search_summary_failure_keeps_plain_results(core, monkeypatch, caplog):
    from app.credits import metered

    error = ValueError("PRIVATE search summary failure")

    async def complete(*args, **kwargs):
        _fail(error)

    monkeypatch.setattr(metered, "llm", complete)
    assert asyncio.run(core.execute._answer_from_search("Explain this source", {"hits": []}, venture_id=core.data["vid"])) == ""
    _assert_private_log(caplog, core.data["vid"], "ValueError")


def test_execute_rethrows_original_failure_without_logging_unregistered_action_text(core, monkeypatch, caplog):
    from app.analytics import chat_usage

    error = ValueError("PRIVATE invalid action")

    async def execute(*args, **kwargs):
        _fail(error)

    monkeypatch.setattr(core.execute, "_execute_action", execute)
    monkeypatch.setattr(chat_usage, "record_chat_action", lambda *args: None)
    with pytest.raises(ValueError) as caught:
        asyncio.run(core.execute.execute_action(core.data["vid"], "PRIVATE-action"))
    assert caught.value is error
    _assert_private_log(caplog, core.data["vid"], "ValueError")


@pytest.mark.hosted
@pytest.mark.parametrize("audience", ["Industrial procurement teams", "Family cycling groups"])
def test_posting_status_failure_does_not_guess_bad_key(core, monkeypatch, caplog, audience):
    from app.chat import posts

    error = TypeError("PRIVATE account response for " + audience)
    monkeypatch.setattr(posts, "_accounts", lambda *args: _fail(error))
    reply = asyncio.run(core.execute._social_status(core.data["vid"]))
    _assert_retry(reply, "social_status")
    assert "key may" not in reply["body_md"] and "could not check" in reply["body_md"]
    assert "Settings > Keys and connections > Your connected apps" in reply["body_md"]
    _assert_private_log(caplog, core.data["vid"], "TypeError")


def test_model_custody_readback_failure_remains_unknown(core, monkeypatch, caplog):
    from app.credits import metered

    error = TypeError("PRIVATE custody readback failure")
    monkeypatch.setattr(metered, "recover_model_call", lambda *args: _fail(error))
    meter = SimpleNamespace(billing_output_error=True, model_journal_key="original")
    result = core.assistant._captured_model_error(meter, venture_id=core.data["vid"])
    assert result.code == "provider_outcome_unknown"
    _assert_private_log(caplog, core.data["vid"], "TypeError")


@pytest.mark.parametrize("provider_unknown", [False, True])
def test_retry_reply_local_failure_offers_status_without_replaying_actions(core, monkeypatch, caplog, provider_unknown):
    from app.llm import founder_provider
    from llm_gateway.errors import ProviderCallError

    error = ProviderCallError("provider_outcome_unknown", "PRIVATE reply failure") if provider_unknown else ValueError("PRIVATE reply failure")
    monkeypatch.setattr(core.assistant, "get_venture_state_brief", lambda *args: {"workspace_id": core.data["wid"]})
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda *args: SimpleNamespace(source="platform_default"))

    async def messages(*args, **kwargs):
        return []

    async def complete(*args, **kwargs):
        _fail(error)

    monkeypatch.setattr(core.assistant, "question_messages", messages)
    monkeypatch.setattr(core.assistant, "_complete", complete)
    reply = asyncio.run(core.assistant.retry_reply(core.data["vid"], {"text": "Explain my saved progress"}))
    if provider_unknown:
        retry = next(action for action in reply["actions"] if action["label"] == "Retry")
        assert retry["action"] == "status" and retry["payload"] == {}
        assert "PRIVATE" not in reply["body_md"]
    else:
        _assert_retry(reply, "status")
    _assert_private_log(caplog, core.data["vid"], type(error).__name__)


def test_optional_router_failure_keeps_regex_and_original_error_log(core, monkeypatch, caplog):
    error = ValueError("PRIVATE malformed routing state")
    calls = []
    monkeypatch.setattr(core.router, "mode", lambda: "live")
    monkeypatch.setattr(core.router, "needs_model", lambda *args: True)

    async def ask(*args, **kwargs):
        calls.append(args)
        _fail(error)

    monkeypatch.setattr(core.router, "_ask_model", ask)
    assert asyncio.run(core.router.route(core.data["vid"], "Explain and review", [("assistant", "Explain and review")], None)) is None
    assert len(calls) == 1
    _assert_private_log(caplog, core.data["vid"], "ValueError")


def test_known_confirmation_blockers_keep_validation_and_landing_page_cards(core, monkeypatch):
    from app.db.supymem_models import IdeaSession
    from app.idea import service
    from tests.unit.chat.test_build_path import seed

    seed(core.data["vid"], core.data["wid"])
    with core.data["factory"]() as session:
        session.get(IdeaSession, "idea_build_path").state = "reviewing"
        session.commit()
    validation = {"kind": "needs_you", "body_md": "Review your idea", "actions": []}
    page = {"kind": "confirmation", "body_md": "Review the page idea", "actions": []}
    monkeypatch.setattr(core.execute, "_validation_first", lambda *args: validation)
    monkeypatch.setattr(core.execute, "_pagebuilder_idea_card", lambda *args: page)
    binding = {"idea_session_id": "session", "idea_revision_id": "revision"}
    for code in core.execute._IDEA_CONFIRMATION_BLOCKERS:
        error = RuntimeError(code)
        monkeypatch.setattr(service, "confirm", lambda *args, **kwargs: _fail(error))
        for build_page in (False, True):
            payload = {"session_id": "session", "revision_id": "revision", "build_landing_page": build_page}
            reply = core.execute._confirm_idea(core.data["vid"], payload, core.data["actor"])
            assert reply is (page if build_page else validation)
        assert core.execute._confirm_idea_for_research(core.data["vid"], core.data["actor"],
            expected_advance_binding=binding) is validation
        assert core.execute._confirm_idea_for_research(core.data["vid"], core.data["actor"]) is validation


def test_unknown_confirmation_failure_does_not_guess_validation(core, monkeypatch, caplog):
    from app.db.supymem_models import IdeaSession
    from app.idea import service
    from tests.unit.chat.test_build_path import seed

    seed(core.data["vid"], core.data["wid"])
    with core.data["factory"]() as session:
        session.get(IdeaSession, "idea_build_path").state = "reviewing"
        session.commit()
    error = RuntimeError("PRIVATE confirmation storage failure")
    monkeypatch.setattr(service, "confirm", lambda *args, **kwargs: _fail(error))
    monkeypatch.setattr(core.execute, "_validation_first", lambda *args: pytest.fail("Unknown errors must not invent validation blockers"))
    monkeypatch.setattr(core.execute, "_pagebuilder_idea_card", lambda *args: pytest.fail("Unknown errors must not invent a missing idea"))
    reply = core.execute._confirm_idea(core.data["vid"],
        {"session_id": "session", "revision_id": "revision", "build_landing_page": True}, core.data["actor"])
    _assert_retry(reply, "idea.review")
    reply = core.execute._confirm_idea_for_research(core.data["vid"], core.data["actor"])
    _assert_retry(reply, "idea.review")
    reply = core.execute._confirm_idea_for_research(core.data["vid"], core.data["actor"],
        expected_advance_binding={"idea_session_id": "session", "idea_revision_id": "revision"})
    _assert_retry(reply, "idea.review")
    _assert_private_log(caplog, core.data["vid"], "RuntimeError")


def test_connection_failure_is_plain_and_does_not_guess_disconnected_accounts(core, monkeypatch, caplog):
    from app.connections import hub

    error = TypeError("PRIVATE connection response failure")
    monkeypatch.setattr(hub, "hub_status", lambda *args: _fail(error))
    reply = core.assistant._channel_readiness_answer(core.data["vid"], {"workspace_id": core.data["wid"]})
    _assert_retry(reply, "connections.status")
    assert "I could not check your connections" in reply["body_md"]
    assert "unverified" not in reply["body_md"] and "No connected account" not in reply["body_md"]
    _assert_private_log(caplog, core.data["vid"], "TypeError")


@pytest.mark.parametrize("gmail_card", [None, {"connected": False, "blocker": {"code": "gmail_error", "message": "PRIVATE"}}])
def test_channel_readiness_keeps_gmail_failure_retry(core, monkeypatch, caplog, gmail_card):
    from app.connections import hub
    from app.gtm.supymem import pipeline

    error = TypeError("PRIVATE mailbox readiness response")
    monkeypatch.setattr(hub, "hub_status", lambda *args: {"connections": {
        "linkedin": {"connected": True, "ready": True}, "gmail": gmail_card}})
    monkeypatch.setattr(pipeline, "mail_readiness", lambda *args: _fail(error))
    reply = core.assistant._channel_readiness_answer(core.data["vid"], {"workspace_id": core.data["wid"]})
    retry = next(action for action in reply["actions"] if action["label"] == "Retry")
    assert retry["action"] == "connections.status" and retry["payload"] == {}
    assert "LinkedIn" in reply["body_md"] and "I could not check your Gmail connection" in reply["body_md"]
    assert "Gmail is not" not in reply["body_md"] and "PRIVATE" not in reply["body_md"]
    assert "does not verify publication approval or a delivered post" in reply["body_md"]
    _assert_private_log(caplog, core.data["vid"], "TypeError")
    body = core.assistant._fallback_answer("Is Gmail connected?", {"workspace_id": core.data["wid"], "venture_id": core.data["vid"]})
    assert "I could not check your Gmail connection" in body and "Gmail is not" not in body and "PRIVATE" not in body
    assert "Gmail is connected" not in body


@pytest.mark.hosted_only
@pytest.mark.parametrize("reason", ["unavailable", "suspended", "insufficient"])
def test_reply_uses_actual_credit_cause_not_provider_wrapper(core, monkeypatch, caplog, reason):
    from app.credits.errors import AccountSuspended, CreditError, CreditsUnavailable, InsufficientCredits, LIMIT_MESSAGE
    from app.llm import founder_provider
    from llm_gateway.errors import ProviderCallError

    source = {"unavailable": CreditsUnavailable("PRIVATE policy or receipt failure"),
        "suspended": AccountSuspended("PRIVATE"), "insufficient": InsufficientCredits("PRIVATE", 0, 100)}[reason]
    monkeypatch.setattr(core.assistant, "get_venture_state_brief", lambda *args: {"workspace_id": core.data["wid"]})
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda *args: SimpleNamespace(source="platform_default"))

    async def messages(*args, **kwargs):
        return []

    async def complete(*args, **kwargs):
        try:
            _fail(source)
        except CreditError as exc:
            raise ProviderCallError(exc.code, "This reply could not be prepared.") from exc

    monkeypatch.setattr(core.assistant, "question_messages", messages)
    monkeypatch.setattr(core.assistant, "_complete", complete)
    reply = asyncio.run(core.assistant.retry_reply(core.data["vid"], {"text": "Explain my saved progress"}))
    if reason == "insufficient":
        assert reply["body_md"] == LIMIT_MESSAGE and "model" not in reply["body_md"].lower()
        assert [action["label"] for action in reply["actions"]] == ["Buy more"]
    else:
        _assert_retry(reply, "chat.model_retry", {"text": "Explain my saved progress"})
    _assert_private_log(caplog, core.data["vid"], type(source).__name__)

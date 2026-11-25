"""A current Mac-task question must use the saved order, not model recall."""

from __future__ import annotations

import asyncio

from tests.company_os_harness import load_company_os_app


def test_latest_mac_task_answer_uses_fresh_saved_order(monkeypatch):
    load_company_os_app()
    from app.chat import assistant

    seen = {}

    def brief(venture_id, **kwargs):
        seen.update(venture_id=venture_id, **kwargs)
        browser = {"id": "dwo_new", "title": "Browser goal on httpbin.org", "status": "stopped",
             "recorded_clicks": 0,
             "review_href": "/v/ven_test/device-work-orders/dwo_new"}
        return {"device_work": {"latest_browser": browser, "recent": [
            {"id": "dwo_terminal", "title": "Verify venture workspace in terminal", "status": "stopped",
             "recorded_clicks": 0,
             "review_href": "/v/ven_test/device-work-orders/dwo_terminal"},
            browser,
            {"id": "dwo_old", "title": "Older browser task", "status": "stopped",
             "recorded_clicks": 1,
             "review_href": "/v/ven_test/device-work-orders/dwo_old"},
        ]}}

    monkeypatch.setattr(assistant, "get_venture_state_brief", brief)
    monkeypatch.setattr(assistant, "list_messages", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("latest task answer must not rely on chat history")))
    result = asyncio.run(assistant.answer_question(
        "ven_test", "What happened in the latest Mac browser task? Give me the review link."))

    assert seen == {"venture_id": "ven_test", "force": True, "mark_visit": True}
    assert "dwo_new" in result["body_md"]
    assert "dwo_old" not in result["body_md"]
    assert "unverified" in result["body_md"]
    assert "submitted" not in result["body_md"]
    assert result["actions"][0]["href"] == "/v/ven_test/device-work-orders/dwo_new"


def test_latest_mac_task_gate_leaves_ordinary_questions_to_chat():
    load_company_os_app()
    from app.chat.assistant import _asks_about_application_build_run, _asks_about_latest_mac_task

    assert _asks_about_latest_mac_task(
        "For the latest Clock Mac task, state each attempted action and its saved visible checkpoint, "
        "verification state, and reconciled credits. Do not start another task."
    )

    assert _asks_about_latest_mac_task("What happened in the latest Mac browser task?")
    assert _asks_about_latest_mac_task(
        "What was the outcome of the latest approved Supymem Mac task? Did it open Builds, "
        "was that verified, and how many credits were settled? Do not start another task."
    )
    assert not _asks_about_latest_mac_task("What is the latest market research?")
    assert not _asks_about_latest_mac_task("Open the browser to check the latest waitlist")
    assert not _asks_about_latest_mac_task(
        "What happened in the latest Mac task, and what is our GTM plan?")
    assert not _asks_about_latest_mac_task("Quote the latest market research and its credits.")
    assert not _asks_about_latest_mac_task("Quote the latest Mac task and our GTM plan.")
    assert not _asks_about_latest_mac_task(
        "What is the latest LabHire application build run status, not a Mac task? "
        "Include the recorded failure reason and whether it will retry automatically. Do not start anything."
    )
    for question in (
        "What is latest LabHire application build run status? I mean product build, not a Mac task. "
        "Include recorded failure reason and whether retries automatically. Do not start any task.",
        "What is the latest LabHire application build run status? I mean product build, not a Mac task. "
        "Include recorded failure reason and whether retries automatically. Do not start any task.",
        "What happened with the latest product build? Not a Mac task. Does it retry automatically?",
        "Tell me the last managed builder run status and failure, not the last Mac task.",
        "What is the latest build status? I mean the product build, not a Mac task.",
    ):
        assert _asks_about_application_build_run(question), question
        assert not _asks_about_latest_mac_task(question), question
    mac_question = "What happened in the latest Mac task? I mean the Mac task, not the product build."
    assert not _asks_about_application_build_run(mac_question)
    assert _asks_about_latest_mac_task(mac_question)


def test_explicit_application_build_run_question_uses_build_record_not_mac_task(monkeypatch):
    load_company_os_app()
    from app.chat import assistant
    from app.chat.intent import classify_intent

    question = ("What is the latest LabHire application build run status? I mean product build, "
                "not a Mac task. Include recorded failure reason and whether retries automatically. "
                "Do not start any task.")
    assert classify_intent(question) == "assistant"
    seen = {}

    def brief(venture_id, **kwargs):
        seen.update(venture_id=venture_id, **kwargs)
        return {"builds": {"last": {"state": "failed", "error_code": "build_provider_provider_down",
                                    "retry_policy": "manual"}},
                "device_work": {"recent": [{"title": "TextEdit Cancel", "status": "stopped",
                                            "review_href": "/v/ven_test/device-work-orders/dwo_textedit"}]}}

    monkeypatch.setattr(assistant, "get_venture_state_brief", brief)
    monkeypatch.setattr(assistant, "list_messages", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("build status must use fresh saved build record, not old chat")))
    monkeypatch.setattr(assistant, "_complete", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("explicit build status needs no model inference")))
    answer = asyncio.run(assistant.answer_question("ven_test", question))

    assert seen == {"venture_id": "ven_test", "force": True, "mark_visit": True}
    assert "application build run is failed" in answer["body_md"]
    assert "build_provider_provider_down" in answer["body_md"]
    assert "will not retry automatically" in answer["body_md"]
    assert "No new build was started" in answer["body_md"]
    assert "TextEdit" not in answer["body_md"]
    assert answer["actions"] == []


def test_approved_mac_outcome_question_reads_saved_order_without_starting_one(monkeypatch):
    load_company_os_app()
    from app.chat import assistant
    from app.chat.intent import classify_intent

    question = ("What was the outcome of the latest approved Supymem Mac task? Did it open Builds, "
                "was that verified, and how many credits were settled? Do not start another task.")
    assert classify_intent(question) == "assistant"
    brief = {"device_work": {"recent": [{
        "title": "Approved but unexecuted duplicate review", "status": "cancelled", "approved": True, "executed": False,
        "review_href": "/v/ven_test/device-work-orders/dwo_cancelled",
    }, {
        "title": "Open Builds in Supymem", "status": "stopped", "task_verified": True,
        "approved": True, "executed": True,
        "verification_kind": "open_venture_screen_v1", "settled_micro": 72,
        "review_href": "/v/ven_test/device-work-orders/dwo_builds",
    }]}}
    monkeypatch.setattr(assistant, "get_venture_state_brief", lambda *_args, **_kw: brief)
    monkeypatch.setattr(assistant, "list_messages", lambda *_args, **_kw: (_ for _ in ()).throw(
        AssertionError("saved receipt answer must not rely on chat history")))
    answer = asyncio.run(assistant.answer_question("ven_test", question))
    assert "dwo_builds" in answer["body_md"]
    assert "dwo_cancelled" not in answer["body_md"]
    assert "independently verified" in answer["body_md"]
    assert 'credit' not in answer['body_md'].lower()
    assert answer["actions"][0]["href"] == "/v/ven_test/device-work-orders/dwo_builds"


def test_approved_mac_outcome_uses_executed_receipt_beyond_recent_window():
    load_company_os_app()
    from app.chat.assistant import _latest_mac_task_answer

    executed = {"title": "Open Builds", "status": "stopped", "approved": True,
                "executed": True, "task_verified": True, "settled_micro": 72,
                "review_href": "/v/ven_test/device-work-orders/dwo_executed"}
    reviews = [{"title": f"Pending review {index}", "status": "pending",
                "approved": False, "executed": False,
                "review_href": f"/v/ven_test/device-work-orders/dwo_review_{index}"}
               for index in range(6)]
    answer = _latest_mac_task_answer(
        {"device_work": {"recent": reviews, "latest_executed": executed}},
        "What was the outcome of the latest approved Mac task?")
    assert answer is not None
    assert "dwo_executed" in answer["body_md"]
    assert 'credit' not in answer['body_md'].lower()


def test_latest_mac_receipt_quote_uses_saved_task_not_generic_status(monkeypatch):
    load_company_os_app()
    from app.chat import assistant
    from app.chat.intent import classify_intent

    question = ("For the latest Calculator Mac task, quote the saved visible expression, "
                "verification state, and reconciled credits. Do not start a new task.")
    assert classify_intent(question) == "assistant"
    assert assistant._asks_about_latest_mac_task(question)
    latest = {"title": "Calculator expression", "status": "stopped", "task_verified": False,
              "settled_micro": 810,
              "review_href": "/v/ven_test/device-work-orders/dwo_calculator"}
    older = {"title": "Older Mac task", "status": "stopped", "task_verified": True,
             "review_href": "/v/ven_test/device-work-orders/dwo_older"}
    seen = {}

    def brief(venture_id, **kwargs):
        seen.update(venture_id=venture_id, **kwargs)
        return {"device_work": {"recent": [latest, older]}}

    monkeypatch.setattr(assistant, "get_venture_state_brief", brief)
    monkeypatch.setattr(assistant, "list_messages", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("latest Mac receipt must not use generic chat history")))
    answer = asyncio.run(assistant.answer_question("ven_test", question))

    assert seen == {"venture_id": "ven_test", "force": True, "mark_visit": True}
    assert "dwo_calculator" in answer["body_md"]
    assert "dwo_older" not in answer["body_md"]
    assert "outcome remains unverified" in answer["body_md"]
    assert 'credit' not in answer['body_md'].lower()
    assert "contains no final screenshot facts" in answer["body_md"]
    assert answer["actions"] == []


def test_latest_browser_receipt_question_keeps_same_task_in_focus(monkeypatch):
    load_company_os_app()
    from app.chat import assistant

    question = ("For the latest LabHire Mac browser task, what exactly was verified and how many browser clicks "
                "were recorded? Give the task link. Do not claim the product application is live.")
    assert assistant._asks_about_latest_mac_task(question)
    browser = {"title": "Verify live site and waitlist", "status": "stopped",
               "task_verified": True, "browser_task": True, "recorded_clicks": 1,
               "review_href": "/v/ven_test/device-work-orders/dwo_browser"}
    coding = {"title": "Build with Codex", "status": "stopped", "coding_provider": "codex",
              "review_href": "/v/ven_test/device-work-orders/dwo_coding"}
    brief = {"device_work": {"latest_browser": browser, "recent": [browser, coding]}}
    monkeypatch.setattr(assistant, "get_venture_state_brief", lambda *_args, **_kwargs: brief)
    monkeypatch.setattr(assistant, "list_messages", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("browser receipt answer must use saved task evidence")))
    answer = asyncio.run(assistant.answer_question("ven_test", question))
    assert "1 completed browser click" in answer["body_md"]
    assert "independently verified" in answer["body_md"]
    assert "dwo_browser" in answer["body_md"]
    assert "dwo_coding" not in answer["body_md"]


def test_terminal_usage_question_uses_task_receipt_not_venture_balance(monkeypatch):
    load_company_os_app()
    from app.chat import assistant
    from app.chat.intent import classify_intent

    question = ("For the latest LabHire Mac terminal task, was Git status independently verified, "
                "how many credits were used, and what remains unproven? Give the task link.")
    assert classify_intent(question) == "assistant"
    assert classify_intent("How many credits are left?") == "credits.balance"
    browser = {"title": "Browser task", "status": "stopped", "browser_task": True,
               "review_href": "/v/ven_test/device-work-orders/dwo_browser"}
    terminal = {"title": "Verify venture workspace in terminal", "status": "stopped",
                "terminal_task": True, "task_verified": True, "settled_micro": 0,
                "review_href": "/v/ven_test/device-work-orders/dwo_terminal"}
    brief = {"device_work": {"recent": [browser, terminal]}}
    monkeypatch.setattr(assistant, "get_venture_state_brief", lambda *_args, **_kwargs: brief)
    monkeypatch.setattr(assistant, "list_messages", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("terminal receipt answer must use saved task evidence")))
    answer = asyncio.run(assistant.answer_question("ven_test", question))
    assert "dwo_terminal" in answer["body_md"]
    assert "dwo_browser" not in answer["body_md"]
    assert "independently verified" in answer["body_md"]
    assert 'credit' not in answer['body_md'].lower()
    assert "not general terminal control" in answer["body_md"]


def test_latest_mac_answer_reports_native_verified_outcome():
    load_company_os_app()
    from app.chat.assistant import _latest_mac_task_answer

    brief = {"device_work": {"recent": [{
        "title": "Verify venture workspace in terminal", "status": "stopped",
        "task_verified": True, "verification_kind": "git_status_v1", "recorded_clicks": 0,
        "review_href": "/v/ven_test/device-work-orders/dwo_verified",
    }]}}
    answer = _latest_mac_task_answer(brief, "What happened in the latest Mac task?")
    assert answer is not None
    assert "independently verified" in answer["body_md"]
    assert "form submission" not in answer["body_md"]


def test_latest_clock_answer_reports_saved_effects_and_settlement_without_inventing_visual_facts():
    load_company_os_app()
    from app.chat.assistant import _latest_mac_task_answer

    brief = {"device_work": {"recent": [{
        "title": "Clock task", "status": "stopped", "task_verified": False,
        "recorded_effects": 2, "settled_micro": 810,
        "review_href": "/v/ven_test/device-work-orders/dwo_clock",
    }]}}
    answer = _latest_mac_task_answer(brief,
        "What happened in the latest Clock Mac task? What was observed and were credits reconciled?")
    assert answer is not None
    assert "2 native action receipts" in answer["body_md"]
    assert "outcome remains unverified" in answer["body_md"]
    assert 'credit' not in answer['body_md'].lower()
    assert "contains no final screenshot facts" in answer["body_md"]
    assert "cannot establish whether a visual assessment was saved" in answer["body_md"]
    assert "the saved Mac review has the visual assessment" not in answer["body_md"]
    assert "World Clock" not in answer["body_md"]


def test_latest_mac_answer_separates_zero_server_receipts_from_local_decisions():
    load_company_os_app()
    from app.chat.assistant import _latest_mac_task_answer

    brief = {"device_work": {"recent": [{
        "title": "Safari research task", "status": "stopped", "task_verified": False,
        "recorded_effects": 0, "settled_micro": 84,
        "review_href": "/v/ven_test/device-work-orders/dwo_safari",
    }]}}
    answer = _latest_mac_task_answer(brief, "What happened in the latest Mac task?")
    assert answer is not None
    assert "server recorded 0 native action receipts" in answer["body_md"]
    assert "local decisions stopped before action dispatch" in answer["body_md"]
    assert "cannot establish whether a visual assessment was saved" in answer["body_md"]
    assert 'credit' not in answer['body_md'].lower()


def test_latest_clock_answer_never_calls_unsettled_usage_reconciled():
    load_company_os_app()
    from app.chat.assistant import _latest_mac_task_answer

    brief = {"device_work": {"recent": [{
        "title": "Clock task", "status": "stopped", "task_verified": False,
        "recorded_effects": 2, "settled_micro": None,
        "review_href": "/v/ven_test/device-work-orders/dwo_clock",
    }]}}
    answer = _latest_mac_task_answer(brief, "What happened in the latest Clock Mac task?")
    assert answer is not None
    assert "outcome remains unverified" in answer["body_md"]
    assert 'credit' not in answer['body_md'].lower()
    assert "reservation was reconciled" not in answer["body_md"]


def test_latest_coding_answer_also_reports_saved_credit_settlement():
    load_company_os_app()
    from app.chat.assistant import _latest_mac_task_answer

    brief = {"device_work": {"recent": [{
        "title": "Build with Codex", "status": "stopped", "task_verified": False,
        "coding_provider": "codex", "settled_micro": 1200,
        "review_href": "/v/ven_test/device-work-orders/dwo_coding",
    }]}}
    answer = _latest_mac_task_answer(brief, "What did the latest Codex Mac task produce?")
    assert answer is not None
    assert 'credit' not in answer['body_md'].lower()
    assert "did not independently verify" in answer["body_md"]


def test_latest_codex_question_uses_order_receipt_without_inventing_product_or_retry(monkeypatch):
    load_company_os_app()
    from app.chat import assistant

    question = ("What did the latest approved Codex Mac task actually produce? "
                "Is the LabHire product live and verified? Give the exact review link and next step.")
    assert assistant._asks_about_latest_mac_task(question)
    brief = {"website": {"live": True, "url": "https://labhire.supymem.com/"},
             "device_work": {"recent": [
                 {"title": "Other Mac task", "status": "stopped",
                  "review_href": "/v/ven_test/device-work-orders/dwo_other"},
                 {"title": "Build approved task with Codex", "status": "stopped",
                  "coding_provider": "codex", "task_verified": False, "approved": True, "executed": True,
                  "review_href": "/v/ven_test/device-work-orders/dwo_coding"},
             ]}}
    monkeypatch.setattr(assistant, "get_venture_state_brief", lambda *_args, **_kwargs: brief)
    monkeypatch.setattr(assistant, "list_messages", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("the saved order answer must not rely on chat history")))
    answer = asyncio.run(assistant.answer_question("ven_test", question))
    assert answer is not None
    assert "cannot attest which local files changed" in answer["body_md"]
    assert "cannot confirm a live product application" in answer["body_md"]
    assert "https://labhire.supymem.com/" in answer["body_md"]
    assert "dwo_coding" in answer["body_md"]
    assert "dwo_other" not in answer["body_md"]
    assert "click" not in answer["body_md"]
    assert "retry" not in answer["body_md"].lower()
    assert answer["actions"][0]["href"] == "/v/ven_test/device-work-orders/dwo_coding"


def test_product_readiness_uses_stored_source_and_matching_contract_receipt(monkeypatch):
    load_company_os_app()
    from app.chat import assistant

    source = "a" * 64
    report = "b" * 64
    brief = {"device_work": {"recent": [{
        "title": "Build approved task with Codex", "status": "stopped",
        "coding_provider": "codex", "task_verified": False,
        "source_state": "stored", "source_sha256": source,
        "source_contract_status": "requires_runtime_qa",
        "source_contract_runtime": "python_product_v1",
        "source_contract_report_sha256": report,
        "review_href": "/v/ven_test/device-work-orders/dwo_coding",
    }]}}
    monkeypatch.setattr(assistant, "get_venture_state_brief", lambda *_args, **_kwargs: brief)
    answer = asyncio.run(assistant.answer_question(
        "ven_test", "What is the latest stored source SHA and contract check? Is the product live?"))
    assert source in answer["body_md"]
    assert report in answer["body_md"]
    assert "Python product files parse" in answer["body_md"]
    assert "not independent QA" in answer["body_md"]
    assert "hosted product" in answer["body_md"]
    assert "cannot start one yet for that product runtime" in answer["body_md"]

    latest = assistant._latest_mac_task_answer(brief, "What did the latest Codex Mac task produce?")
    assert latest is not None
    assert source in latest["body_md"]
    assert "no accepted source" not in latest["body_md"]
    assert "cannot start one yet for that product runtime" in latest["body_md"]
    assert "cannot attest which local files changed" not in latest["body_md"]


def test_source_evidence_does_not_claim_mismatched_or_missing_contract():
    load_company_os_app()
    from app.chat.assistant import _coding_source_evidence

    result = _coding_source_evidence({"source_sha256": "c" * 64})
    assert "No matching source-contract report" in result
    assert "Basic source contract present" not in result


def test_browser_qa_question_uses_exact_coding_source_instead_of_old_safari():
    load_company_os_app()
    from app.chat.assistant import _latest_mac_task_answer
    coding = {"id": "dwo_coding", "title": "Codex landing", "status": "stopped",
        "coding_provider": "codex", "approved": True, "executed": True,
        "source_state": "stored", "source_sha256": "a" * 64,
        "source_qa": {"qa_status": "passed", "qa_passed": 49, "qa_failed": 0, "qa_unsupported": 0,
            "qa_report_digest": "b" * 64, "current_scope": True, "source_accepted": False,
            "source_published": False, "plan_task_state": "waiting_for_founder"},
        "review_href": "/v/ven_test/device-work-orders/dwo_coding"}
    browser = {"title": "Old Safari", "status": "stopped", "browser_task": True,
        "approved": True, "executed": True, "review_href": "/v/ven_test/device-work-orders/dwo_safari"}
    brief = {"device_work": {"recent": [browser, coding], "latest_browser": browser,
        "latest_coding_source": coding, "latest_executed_by_kind": {"codex": coding, "browser": browser}}}
    for question in ("What is the status of my latest approved Codex task and its browser QA?",
                     "What is the status of the latest coding task and browser QA?"):
        reply = _latest_mac_task_answer(brief, question)
        assert "49 passed, 0 failed, 0 unsupported" in reply["body_md"]
        assert "dwo_coding" in reply["body_md"] and "dwo_safari" not in reply["body_md"]
        assert "Source acceptance is not verified" in reply["body_md"]
    reply = _latest_mac_task_answer(brief, "What happened in the latest approved browser task?")
    assert "dwo_safari" in reply["body_md"] and "dwo_coding" not in reply["body_md"]


def test_exact_source_qa_facts_survive_brief_budget_before_older_activity():
    load_company_os_app()
    from app.ventures.state_brief import BRIEF_CHAR_BUDGET, render_brief_for_llm
    coding = {"id": "dwo_current", "coding_provider": "codex", "source_sha256": "a" * 64,
        "source_qa": {"qa_status": "passed", "qa_passed": 49, "qa_failed": 0, "qa_unsupported": 0,
            "qa_report_digest": "b" * 64, "current_scope": True}}
    brief = {"idea": {"name": "Current venture"}, "device_work": {"latest_coding_source": coding},
             "research": {"present": True, "summary": "older unrelated notes " * 600}}
    text = render_brief_for_llm(brief)
    assert len(text) <= BRIEF_CHAR_BUDGET
    assert "49 passed, 0 failed, 0 unsupported" in text
    assert "Publication of this exact source is not verified" in text
    assert "dwo_current" in text

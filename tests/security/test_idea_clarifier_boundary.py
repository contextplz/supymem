"""Assembled signed API + real credit ledger, jobs, parser and persisted revisions."""
from __future__ import annotations

import asyncio
import json


from sqlalchemy import select

from tests.security.test_connector_oauth_boundary import client as client
from tests.security.test_durable_journey_boundary import IDEA, request, venture, run_queued
from tests.idea_fixtures import completion, install_provider


def submit(client, base, text):
    result = request(client, "POST", base + "/messages", json={"text": text})
    assert result.status_code == 200, result.text
    return result.json()["job_id"]


def job_row(jid):
    from app.db.models import Job
    from app.state import db
    with db()() as session:
        row = session.get(Job, jid)
        session.expunge(row)
        return row


def receipt(jid):
    from app.db.supymem_models import IdeaClarificationRun
    from app.state import db
    with db()() as session:
        row = session.scalars(select(IdeaClarificationRun).where(IdeaClarificationRun.job_id == jid)).one()
        session.expunge(row)
        return row


def test_case_a_model_questions_and_case_b_exploration_are_persisted(client, monkeypatch):
    calls = install_provider(monkeypatch, lambda messages, kw: completion(proposal=False, case="explore", understanding={
        "summary": "Explore bakery ordering problems", "problem": "Missing cake orders",
        "proposed_options": [{"title": "Order inbox", "problem": "Missing cake orders", "customer": "Bakers", "offer": "Order board", "first_test": "Interview one bakery owner"}],
        "assumptions": ["An order board might help; demand is unvalidated"], "open_questions": ["Can you reach bakery owners?"]}))
    vid, wid, sid = venture(client)
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    assert request(client, "POST", base + "/case", json={"case": "explore"}).status_code == 200
    jid = submit(client, base, "I know bakery owners but I do not know what to build")
    assert job_row(jid).status == "scheduled"
    assert request(client, "POST", base + "/confirm", json={}).status_code == 409
    run_queued("idea.clarify_turn")
    assert job_row(jid).status == "succeeded", job_row(jid).last_error
    state = request(client, "GET", base).json()
    assert state["clarifier_mode"] == "ai_clarifier" and state["case"] == "explore"
    assert state["state"] == "clarifying" and state["questions_asked"] == 1
    assert state["current_revision"]["source"] == "agent"
    assert len(state["current_revision"]["structured"]["proposed_options"]) == 1
    assert state["turns"][-1]["options"]["allow_free_text"] is True
    assert state["turns"][-1]["llm_usage"]["source"] == "platform"
    assert state["turns"][-1]["llm_usage"]["credits_micro"] > 0
    assert len(calls) == 1 and calls[0][1]["workspace_id"] == wid
    saved = receipt(jid)
    assert saved.state == "completed" and json.loads(saved.usage)["settled"]
    assert request(client, "GET", base, "usr_bob").status_code == 404


def test_manual_and_conversational_edits_evolve_without_heuristic_invention(client, monkeypatch):
    calls = install_provider(monkeypatch)
    vid, _, sid = venture(client)
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    result = request(client, "PATCH", base + "/revision", json={"patch": IDEA})
    assert result.status_code == 200
    jid = submit(client, base, "Move payments before onboarding and use Stripe")
    # Input persists immediately; no ungrounded heuristic rewrite of problem.
    pre = request(client, "GET", base).json()["current_revision"]["structured"]
    assert pre["problem"] == IDEA["problem"] and pre["first_user_path"] == IDEA["first_user_path"]
    run_queued("idea.clarify_turn")
    assert job_row(jid).status == "succeeded"
    snapshot = json.loads(calls[0][0][-1]["content"])
    assert snapshot["conversation"][-1]["text"] == "Move payments before onboarding and use Stripe"
    assert snapshot["understanding"]["workflow"] == IDEA["workflow"]
    assert request(client, "PATCH", base + "/revision", json={"patch": {"admin": True}}).status_code == 422
    assert request(client, "PATCH", base + "/revision", json={"patch": {"offer": ["not a string"]}}).status_code == 422


def test_saved_company_idea_reaches_first_mode_chip_and_later_edits(client, monkeypatch):
    calls = install_provider(monkeypatch)
    vid, _, sid = venture(client)
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    jid = submit(client, base, "I know what I want")
    run_queued("idea.clarify_turn")
    assert job_row(jid).status == "succeeded", job_row(jid).last_error
    first = json.loads(calls[0][0][-1]["content"])
    assert first["original_idea"] == IDEA["problem"]
    assert first["explicit_case"] == "know"
    assert first["conversation"][-1]["text"] == "I know what I want"
    assert json.loads(receipt(jid).snapshot)["original_idea"] == IDEA["problem"]
    turns_before_edit = request(client, "GET", base).json()["turns"]
    correction = request(client, "PATCH", base + "/revision", json={"patch": {"offer": "A manual intake service"}})
    assert correction.status_code == 200
    corrected = request(client, "GET", base).json()
    assert corrected["current_revision"]["structured"]["offer"] == "A manual intake service"
    assert corrected["turns"] == turns_before_edit, "Correction must not silently rewrite saved conversation history"
    later = submit(client, base, "Use the manual service before building software")
    run_queued("idea.clarify_turn")
    assert job_row(later).status == "succeeded", job_row(later).last_error
    next_snapshot = json.loads(calls[-1][0][-1]["content"])
    assert "Latest founder statement" in next_snapshot["original_idea"]
    assert "Use the manual service before building software" in next_snapshot["original_idea"]
    assert IDEA["problem"] in next_snapshot["original_idea"]
    assert "A manual intake service" in next_snapshot["original_idea"]
    assert next_snapshot["understanding"]["offer"] == "A manual intake service"
    assert next_snapshot["conversation"][-1]["text"] == "Use the manual service before building software"


def test_confirm_requires_the_reviewed_revision_and_never_rewrites_manual_constraints(client, monkeypatch):
    calls = install_provider(monkeypatch)
    vid, _, sid = venture(client)
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    submit(client, base, "just propose")
    run_queued("idea.clarify_turn")
    original = request(client, "GET", base).json()["current_revision"]["id"]
    constraints = ["Do not contact anyone during acceptance", "No payment processing"]
    edited = request(client, "PATCH", base + "/revision", json={"patch": {"constraints": constraints}}).json()
    assert request(client, "GET", base).json()["state"] == "reviewing"
    stale = request(client, "POST", base + "/confirm", json={"revision_id": original})
    assert stale.status_code == 409 and stale.json()["detail"]["error"]["code"] == "idea_review_conflict"
    confirmed = request(client, "POST", base + "/confirm", json={"revision_id": edited["id"]})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["session"]["current_revision"]["structured"]["constraints"] == constraints
    reopened = request(client, "POST", f"/ventures/{vid}/idea/sessions")
    assert reopened.status_code == 201
    assert reopened.json()["id"] == sid
    assert reopened.json()["state"] == "confirmed"
    assert len(calls) == 1


def test_correction_after_validation_has_explicit_revalidation_path(client, monkeypatch):
    from tests.security.test_idea_validation_boundary import validation_provider
    validation_provider(monkeypatch)
    vid, _, sid = venture(client)
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    assert request(client, "PATCH", base + "/revision", json={"patch": IDEA}).status_code == 200
    assert request(client, "POST", base + "/validate", json={}).status_code == 200
    run_queued("idea.validate")
    assert request(client, "GET", base).json()["validation_runs"]
    prior = request(client, "GET", base).json()["current_revision"]["id"]
    edited = request(client, "PATCH", base + "/revision", json={"patch": {"offer": "Manual service before software"}})
    assert edited.status_code == 200, edited.text
    current = edited.json()["id"]
    assert current != prior
    queued = request(client, "POST", base + "/validate", json={"revision_id": current})
    assert queued.status_code == 200 and queued.json()["job_id"]
    assert queued.json()["session"]["state"] == "validating"


def test_credit_rejection_never_calls_provider(client, monkeypatch):
    from app.credits.accounts import get_account
    from app.db.models import CreditAccount
    from app.state import db
    calls = install_provider(monkeypatch)
    vid, _, sid = venture(client)
    with db()() as session:
        row = session.get(CreditAccount, get_account(vid).id)
        row.spent_micro = row.granted_micro
        session.commit()
    jid = submit(client, f"/ventures/{vid}/idea/sessions/{sid}", "An order board for bakers")
    run_queued("idea.clarify_turn")
    assert job_row(jid).last_error == "clarification_insufficient_credits"
    assert not calls and receipt(jid).state == "failed"


def test_invalid_clarifier_output_needs_explicit_retry_with_a_fresh_receipt(client, monkeypatch):
    attempts = []
    def flaky(*args):
        attempts.append(1)
        response = completion()
        if len(attempts) == 1:
            response.choices[0].message.content = '{"action":"execute_shell"}'
        return response
    calls = install_provider(monkeypatch, flaky)
    vid, _, sid = venture(client)
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    jid = submit(client, base, "An order board for bakers")
    run_queued("idea.clarify_turn")
    assert job_row(jid).last_error == "clarification_invalid_output" and len(calls) == 1
    saved = receipt(jid)
    assert saved.error_code == "clarification_invalid_output" and saved.response is None
    usage = json.loads(saved.usage)
    assert usage["prompt_tokens"] == 200 and usage["completion_tokens"] == 100 and usage["credits_micro"] > 0
    retry = request(client, "POST", base + "/retry-clarification")
    assert retry.status_code == 200, retry.text
    second = retry.json()["job_id"]
    assert second != jid
    run_queued("idea.clarify_turn")
    repaired = receipt(second)
    assert job_row(second).status == "succeeded" and len(calls) == 2
    assert repaired.error_code is None and repaired.response
    assert json.loads(repaired.usage)["reservation_id"] != usage["reservation_id"]
    assert json.loads(repaired.usage)["prompt_tokens"] == 200


def test_invalid_paid_output_is_charged_once_and_not_applied(client, monkeypatch, caplog):
    def invalid(*args):
        response = completion()
        response.choices[0].message.content = '{"action":"execute_shell","secret":"do not expose"}'
        return response
    calls = install_provider(monkeypatch, invalid)
    vid, _, sid = venture(client)
    jid = submit(client, f"/ventures/{vid}/idea/sessions/{sid}", "An order board for bakers")
    run_queued("idea.clarify_turn")
    saved = receipt(jid)
    assert job_row(jid).last_error == "clarification_invalid_output"
    assert saved.response is None and saved.state == "failed"
    usage = json.loads(saved.usage)
    assert usage["credits_micro"] > 0
    assert usage["output"]["validation_kind"] == "schema"
    assert usage["output"]["finish_reason"] == "stop"
    assert usage["output"]["schema_issue_count"] > 0
    public_job = request(client, "GET", f"/jobs/{jid}").json()
    assert public_job["revision_id"] == saved.revision_id
    assert public_job["error_code"] == "clarification_invalid_output"
    assert "do not expose" not in str(saved.__dict__) and len(calls) == 1
    assert "do not expose" not in caplog.text
    assert any(record.message == "clarification_model_output_rejected" for record in caplog.records)


def test_explicit_case_mismatch_keeps_valid_paid_output(client, monkeypatch):
    calls = install_provider(monkeypatch, lambda *_: completion(case="know", proposal=False))
    vid, _, sid = venture(client)
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    assert request(client, "POST", base + "/case", json={"case": "explore"}).status_code == 200
    jid = submit(client, base, "Help me explore a bakery workflow")
    run_queued("idea.clarify_turn")
    saved = request(client, "GET", base).json()
    assert job_row(jid).status == "succeeded"
    assert saved["case"] == "explore" and saved["turns"][-1]["kind"] == "question"
    assert len(calls) == 1


def test_forced_proposal_uses_valid_understanding_from_question_response(client, monkeypatch):
    install_provider(monkeypatch, lambda *_: completion(proposal=False))
    vid, _, sid = venture(client)
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    jid = submit(client, base, "just propose")
    run_queued("idea.clarify_turn")
    saved = request(client, "GET", base).json()
    assert job_row(jid).status == "succeeded"
    assert saved["turns"][-1]["kind"] == "proposal"
    assert saved["current_revision"]["structured"]["offer"] == "Order intake board"


def test_proposal_without_explicit_null_question_is_accepted(client, monkeypatch):
    def provider(*args):
        response = completion(proposal=True)
        body = json.loads(response.choices[0].message.content)
        body.pop("question")
        response.choices[0].message.content = json.dumps(body)
        return response
    install_provider(monkeypatch, provider)
    vid, _, sid = venture(client)
    jid = submit(client, f"/ventures/{vid}/idea/sessions/{sid}", "A bakery order board")
    run_queued("idea.clarify_turn")
    assert job_row(jid).status == "succeeded"


def test_founder_can_explicitly_retry_rejected_output_without_duplicate_input(client, monkeypatch):
    attempts = 0
    def provider(*args):
        nonlocal attempts
        attempts += 1
        if attempts == 1:  # one rejected call before the founder's explicit retry
            response = completion()
            response.choices[0].message.content = '{"action":"execute_shell","secret":"do not expose"}'
            return response
        return completion(proposal=False)
    calls = install_provider(monkeypatch, provider)
    vid, _, sid = venture(client)
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    first = submit(client, base, "An order board for bakers")
    run_queued("idea.clarify_turn")
    before = request(client, "GET", base).json()
    retry = request(client, "POST", base + "/retry-clarification")
    assert retry.status_code == 200, retry.text
    second = retry.json()["job_id"]
    assert second != first
    assert request(client, "POST", base + "/retry-clarification").status_code == 409
    run_queued("idea.clarify_turn")
    after = request(client, "GET", base).json()
    assert job_row(second).status == "succeeded" and len(calls) == 2
    assert len([turn for turn in after["turns"] if turn["role"] == "founder"]) == len(
        [turn for turn in before["turns"] if turn["role"] == "founder"]
    )
    assert request(client, "POST", base + "/retry-clarification").status_code == 409
    other_vid, _, _ = venture(client)
    assert request(client, "POST", f"/ventures/{other_vid}/idea/sessions/{sid}/retry-clarification").status_code == 404


def test_two_malformed_attempts_exhaust_retry_then_manual_review_uses_saved_answer(client, monkeypatch):
    def invalid(*args):
        response = completion()
        response.choices[0].message.content = '{"action":"execute_shell"}'
        return response
    calls = install_provider(monkeypatch, invalid)
    vid, _, sid = venture(client)
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    original = submit(client, base, "An order board for bakers")
    run_queued("idea.clarify_turn")
    first_state = request(client, "GET", base).json()
    assert first_state["clarification_retries_remaining"] == 1
    assert first_state["clarification_invalid_output_seen"] is True
    first_reload = request(client, "POST", f"/ventures/{vid}/idea/sessions").json()
    assert first_reload["manual_review_available"] is True
    assert first_reload["clarification_retry_available"] is True
    assert first_reload["clarification_invalid_output_seen"] is True
    assert first_reload["clarification_retries_remaining"] == first_state["clarification_retries_remaining"]
    assert first_reload["turns"] == first_state["turns"], "Reload must not reseed the evolving idea as a new answer"
    assert first_reload["initial_job_id"] is None
    retry = request(client, "POST", base + "/retry-clarification")
    assert retry.status_code == 200, retry.text
    repeated = retry.json()["job_id"]
    assert repeated != original
    pending_manual = request(client, "POST", base + "/manual-review", json={
        "revision_id": first_state["current_revision"]["id"],
        "patch": {"problem": IDEA["problem"], "customer": IDEA["customer"], "offer": IDEA["offer"]},
    })
    assert pending_manual.status_code == 409
    run_queued("idea.clarify_turn")
    exhausted = request(client, "GET", base).json()
    assert exhausted["clarification_retries_remaining"] == 0
    assert exhausted["clarification_invalid_output_seen"] is True
    exhausted_reload = request(client, "POST", f"/ventures/{vid}/idea/sessions").json()
    assert exhausted_reload["manual_review_available"] is True
    assert exhausted_reload["clarification_retry_available"] is False
    assert request(client, "POST", base + "/retry-clarification").status_code == 409
    assert len(calls) == 2
    patch = {"problem": "Bakers lose custom orders in email", "customer": "Neighbourhood bakeries",
             "offer": "A manually managed order board"}
    body = {"revision_id": exhausted["current_revision"]["id"], "patch": patch}
    assert request(client, "POST", base + "/manual-review", "usr_bob", json=body).status_code in {403, 404}
    assert request(client, "POST", base + "/manual-review", json={**body, "patch": {"offer": ""}}).status_code == 422
    reviewed = request(client, "POST", base + "/manual-review", json=body)
    assert reviewed.status_code == 200, reviewed.text
    saved = reviewed.json()
    assert saved["state"] == "reviewing"
    assert saved["current_revision"]["structured"]["_manual_recovery"] is True
    assert request(client, "POST", f"/ventures/{vid}/idea/sessions").json()["manual_review_available"] is False
    assert all(saved["current_revision"]["structured"][field] == value for field, value in patch.items())
    assert len([turn for turn in saved["turns"] if turn["role"] == "founder"]) == len(
        [turn for turn in exhausted["turns"] if turn["role"] == "founder"])
    assert len(calls) == 2
    assert request(client, "POST", base + "/manual-review", json=body).status_code == 409
    confirmed = request(client, "POST", base + "/confirm", json={"revision_id": saved["current_revision"]["id"]})
    assert confirmed.status_code == 200, confirmed.text


def test_manual_review_available_after_first_malformed_attempt(client, monkeypatch):
    def invalid(*args):
        response = completion()
        response.choices[0].message.content = "not JSON"
        return response
    calls = install_provider(monkeypatch, invalid)
    vid, _, sid = venture(client)
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    submit(client, base, "An order board for bakers")
    run_queued("idea.clarify_turn")
    saved = request(client, "GET", base).json()
    # A later validation failure is the latest venture job, so a client that
    # restores only that job will miss the older malformed clarifier failure.
    started = request(client, "POST", base + "/validate",
        json={"revision_id": saved["current_revision"]["id"]})
    assert started.status_code == 200, started.text
    run_queued("idea.validate")
    reopened = request(client, "POST", f"/ventures/{vid}/idea/sessions").json()
    assert reopened["manual_review_available"] is True
    assert reopened["clarification_retry_available"] is True
    assert reopened["initial_job_id"] is None
    assert reopened["clarification_invalid_output_seen"] is True
    assert reopened["clarification_retries_remaining"] == saved["clarification_retries_remaining"]
    assert reopened["turns"] == saved["turns"]
    result = request(client, "POST", base + "/manual-review", json={"revision_id": saved["current_revision"]["id"],
        "patch": {"problem": IDEA["problem"], "customer": IDEA["customer"], "offer": IDEA["offer"]}})
    assert result.status_code == 200, result.text
    assert result.json()["state"] == "reviewing" and len(calls) == 4  # clarify, validate + two repairs


def test_manual_review_survives_different_failure_on_explicit_retry(client, monkeypatch):
    from llm_gateway.errors import ProviderCallError
    attempts = 0
    def provider(*args):
        nonlocal attempts
        attempts += 1
        if attempts == 1:  # one rejected call before the founder's explicit retry
            response = completion()
            response.choices[0].message.content = "not JSON"
            return response
        raise ProviderCallError("bad_api_key", "provider refused request", status=401, retryable=False)
    install_provider(monkeypatch, provider)
    vid, _, sid = venture(client)
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    submit(client, base, "An order board for bakers")
    run_queued("idea.clarify_turn")
    assert request(client, "POST", base + "/retry-clarification").status_code == 200
    run_queued("idea.clarify_turn")
    saved = request(client, "GET", base).json()
    assert saved["clarification_invalid_output_seen"] is True
    result = request(client, "POST", base + "/manual-review", json={"revision_id": saved["current_revision"]["id"],
        "patch": {"problem": IDEA["problem"], "customer": IDEA["customer"], "offer": IDEA["offer"]}})
    assert result.status_code == 200, result.text
    assert result.json()["state"] == "reviewing" and attempts == 2


def test_exploratory_manual_review_can_validate_accept_and_confirm(client, monkeypatch):
    from tests.security.test_idea_validation_boundary import validation_provider
    def invalid(*args):
        response = completion(case="explore")
        response.choices[0].message.content = "not JSON"
        return response
    install_provider(monkeypatch, invalid)
    vid, _, sid = venture(client)
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    assert request(client, "POST", base + "/case", json={"case": "explore"}).status_code == 200
    submit(client, base, "Explore bakery order intake")
    run_queued("idea.clarify_turn")
    saved = request(client, "GET", base).json()
    reviewed = request(client, "POST", base + "/manual-review", json={
        "revision_id": saved["current_revision"]["id"],
        "patch": {"problem": IDEA["problem"], "customer": IDEA["customer"], "offer": IDEA["offer"]},
    })
    assert reviewed.status_code == 200 and reviewed.json()["state"] == "reviewing"
    validation_provider(monkeypatch)
    revision = reviewed.json()["current_revision"]["id"]
    started = request(client, "POST", base + "/validate", json={"revision_id": revision})
    assert started.status_code == 200, started.text
    run_queued("idea.validate")
    runs = request(client, "GET", base).json()["validation_runs"]
    assert len(runs) == 3 and all(run["state"] == "complete" for run in runs)
    for run in runs:
        assert request(client, "POST", base + f"/validations/{run['id']}/decide",
            json={"decision": "accept"}).status_code == 200
    confirmed = request(client, "POST", base + "/confirm", json={"revision_id": revision})
    assert confirmed.status_code == 200, confirmed.text


def test_rejected_output_can_retry_after_only_case_toggle(client, monkeypatch):
    attempts = 0
    def provider(*args):
        nonlocal attempts
        attempts += 1
        if attempts == 1:  # one rejected call before the founder's explicit retry
            response = completion()
            response.choices[0].message.content = '{"action":"execute_shell"}'
            return response
        return completion(case="explore", proposal=False)
    install_provider(monkeypatch, provider)
    vid, _, sid = venture(client)
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    submit(client, base, "An order board for bakers")
    run_queued("idea.clarify_turn")
    assert request(client, "POST", base + "/case", json={"case": "explore"}).status_code == 200
    retried = request(client, "POST", base + "/retry-clarification")
    assert retried.status_code == 200, retried.text
    run_queued("idea.clarify_turn")
    assert request(client, "GET", base).json()["case"] == "explore"
    assert attempts == 2


def test_case_toggle_is_bound_to_venture(client):
    first_vid, _, sid = venture(client)
    other_vid, _, _ = venture(client)
    response = request(client, "POST", f"/ventures/{other_vid}/idea/sessions/{sid}/case", json={"case": "explore"})
    assert response.status_code == 404
    assert request(client, "GET", f"/ventures/{first_vid}/idea/sessions/{sid}").json()["case"] == "know"


def test_prior_choice_correction_survives_reload_and_second_edit(client, monkeypatch):
    install_provider(monkeypatch, lambda *_: completion(proposal=False))
    vid, _, sid = venture(client)
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    submit(client, base, "A bakery order board")
    run_queued("idea.clarify_turn")
    question = request(client, "GET", base).json()["turns"][-1]
    assert question["kind"] == "question" and question["options"]["field"] == "customer"
    submit(client, base, "Bakery owner")
    run_queued("idea.clarify_turn")
    for answer in ("Local bakers", "Business consultants / managers"):
        patched = request(client, "PATCH", base + "/revision", json={
            "patch": {"customer": answer},
            "answer_correction": {"question_id": question["id"], "text": answer},
        })
        assert patched.status_code == 200, patched.text
        reloaded = request(client, "GET", base).json()
        assert reloaded["current_revision"]["structured"]["customer"] == answer
        assert reloaded["current_revision"]["structured"]["_answer_corrections"][question["id"]] == answer
        assert next(turn for turn in reloaded["turns"] if turn["id"] == question["id"])["options"]["options"] == ["Bakery owner", "Not sure"]
        assert next(turn for turn in reloaded["turns"] if turn["role"] == "founder" and turn["turn"] == question["turn"] + 1)["text"] == "Bakery owner"


def test_exploration_choice_correction_survives_following_model_revision(client, monkeypatch):
    def provider(*args):
        response = completion(proposal=False, case="explore")
        body = json.loads(response.choices[0].message.content)
        body["question"].update({"field": "proposed_options", "text": "Which direction fits?", "options": ["Order board", "Manual intake"]})
        response.choices[0].message.content = json.dumps(body)
        return response
    install_provider(monkeypatch, provider)
    vid, _, sid = venture(client)
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    request(client, "POST", base + "/case", json={"case": "explore"})
    submit(client, base, "Explore bakery order intake")
    run_queued("idea.clarify_turn")
    question = request(client, "GET", base).json()["turns"][-1]
    submit(client, base, "Order board")
    run_queued("idea.clarify_turn")
    correction = request(client, "POST", base + "/messages", json={
        "text": "Manual intake", "correction_question_id": question["id"],
    })
    assert correction.status_code == 200, correction.text
    run_queued("idea.clarify_turn")
    reloaded = request(client, "GET", base).json()
    assert reloaded["current_revision"]["structured"]["_answer_corrections"][question["id"]] == "Manual intake"
    assert next(turn for turn in reloaded["turns"] if turn["role"] == "founder" and turn["kind"] == "answer_correction")["text"] == "Manual intake"


def test_repeated_question_field_becomes_proposal_instead_of_paid_loop(client, monkeypatch):
    calls = install_provider(monkeypatch, lambda *_: completion(
        proposal=False,
        understanding={
            "summary": "Order intake",
            "offer": "A manual order board",
        },
        text="Which bakery owner would use this first?",
    ))
    vid, _, sid = venture(client)
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    first = submit(client, base, "An order board")
    run_queued("idea.clarify_turn")
    assert job_row(first).status == "succeeded"
    first_state = request(client, "GET", base).json()
    assert first_state["questions_asked"] == 1
    assert first_state["turns"][-1]["kind"] == "question"

    second = submit(client, base, "A neighbourhood bakery owner")
    run_queued("idea.clarify_turn")
    assert job_row(second).status == "succeeded"
    second_state = request(client, "GET", base).json()
    assert second_state["state"] == "reviewing"  # waits for the founder's Looks right
    assert second_state["questions_asked"] == 1
    assert second_state["turns"][-1]["kind"] == "proposal"
    assert json.loads(receipt(second).snapshot)["question_fields_asked"] == ["customer"]
    assert len(calls) == 2


def test_repeated_question_guard_keeps_fields_older_than_prompt_window(client, monkeypatch):
    calls = install_provider(monkeypatch, lambda *_: completion(
        proposal=False,
        understanding={
            "summary": "Order intake",
            "offer": "A manual order board",
        },
        text="Which bakery owner would use this first?",
    ))
    vid, wid, sid = venture(client)
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    first = submit(client, base, "An order board")
    run_queued("idea.clarify_turn")
    assert job_row(first).status == "succeeded"

    first_state = request(client, "GET", base).json()
    assert first_state["questions_asked"] == 1
    assert first_state["turns"][-1]["kind"] == "question"

    # Validation/research can add enough conversation entries to push the
    # original structured question outside the bounded provider prompt.
    from app.db.supymem_models import ClarifierTurn
    from app.state import db
    from core.clock import utcnow
    from core.ids import new_id
    with db()() as session:
        for turn in range(100, 130):
            session.add(ClarifierTurn(
                id=new_id("ict"), session_id=sid, workspace_id=wid,
                venture_id=vid, turn=turn, role="founder", kind="message",
                text=f"Saved research note {turn}", created_at=utcnow(),
            ))
        session.commit()

    second = submit(client, base, "A neighbourhood bakery owner")
    run_queued("idea.clarify_turn")
    assert job_row(second).status == "succeeded"
    state = request(client, "GET", base).json()
    assert state["state"] == "reviewing"
    assert state["turns"][-1]["kind"] == "proposal"
    snapshot = json.loads(receipt(second).snapshot)
    assert snapshot["question_fields_asked"] == ["customer"]
    assert all(turn["text"] != "Which bakery owner would use this first?" for turn in snapshot["conversation"])
    assert len(calls) == 2


def test_stated_customer_becomes_proposal_with_one_paid_call(client, monkeypatch):
    calls = install_provider(monkeypatch, lambda *_: completion(proposal=False,
        understanding={"summary": "Order intake for neighbourhood bakeries", "customer": "Neighbourhood bakery owners", "offer": "A manual order board"},
        text="Which bakery owner would use this first?"))
    vid, _, sid = venture(client)
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    job = submit(client, base, "An order board for neighbourhood bakery owners")
    run_queued("idea.clarify_turn")
    assert job_row(job).status == "succeeded"
    state = request(client, "GET", base).json()
    assert state["state"] == "reviewing" and state["questions_asked"] == 0
    assert state["turns"][-1]["kind"] == "proposal"
    assert not any(turn["kind"] == "question" for turn in state["turns"])
    assert json.loads(receipt(job).snapshot)["question_fields_asked"] == []
    assert len(calls) == 1


def test_explicit_byok_never_spends_platform_credits(client, monkeypatch):
    from app.connectors.secrets import reset_store
    from app.credits.accounts import get_account
    from app.llm import founder_provider
    from llm_gateway import sole
    install_provider(monkeypatch)
    # This assertion is about spend, not FileSecretStore durability. Pin the
    # founder key in the process vault so a stale tmp_path file cannot look
    # like a missing BYOK secret.
    monkeypatch.setenv("COMPANY_OS_EPHEMERAL_VAULT", "1")
    reset_store()
    vid, wid, sid = venture(client)
    founder_provider.set_founder_config(workspace_id=wid, provider="azure_foundry", model="founder-deployment", base_url="https://byok.example.test/openai/v1", api_key="founder-key")
    assert founder_provider.to_provider_config(wid) is not None
    calls = []
    async def byok(messages, **kwargs):
        calls.append(kwargs)
        return completion(proposal=False)
    monkeypatch.setattr(sole, "byok_chat_completion", byok)
    before = get_account(vid).spent_micro
    jid = submit(client, f"/ventures/{vid}/idea/sessions/{sid}", "An order board for bakers")
    run_queued("idea.clarify_turn")
    assert job_row(jid).status == "succeeded", job_row(jid).last_error
    assert get_account(vid).spent_micro == before
    assert json.loads(receipt(jid).usage)["source"] == "byok"
    assert calls[0]["single_attempt"] is True and calls[0]["response_schema"]


def test_newer_manual_edit_fences_inflight_provider_result(client, monkeypatch):
    from app.idea.service import patch_revision
    vid, _, sid = venture(client)
    def edited(*args):
        patch_revision(sid, {"offer": "Founder edited this while the model was working"})
        return completion(proposal=False)
    calls = install_provider(monkeypatch, edited)
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    jid = submit(client, base, "An order board for bakers")
    run_queued("idea.clarify_turn")
    assert receipt(jid).state == "superseded" and len(calls) == 1
    assert request(client, "GET", base).json()["current_revision"]["structured"]["offer"] == "Founder edited this while the model was working"


def test_provider_timeout_receipt_blocks_automatic_paid_retry(client, monkeypatch):
    from app.core.jobs import lease
    from app.db.models import Job
    from app.state import db
    from app.worker.main import _run_one
    from llm_gateway.errors import ProviderCallError
    def timeout(*args):
        raise ProviderCallError("provider_down", "provider request secret", retryable=True)
    calls = install_provider(monkeypatch, timeout)
    vid, _, sid = venture(client)
    jid = submit(client, f"/ventures/{vid}/idea/sessions/{sid}", "An order board for bakers")
    run_queued("idea.clarify_turn")
    assert receipt(jid).state == "uncertain"
    from datetime import timedelta
    from core.clock import utcnow
    from app.credits.accounts import get_account
    from app.credits.reserve import sweep_expired
    held = get_account(vid).reserved_micro
    assert held > 0
    sweep_expired(utcnow() + timedelta(days=2))
    assert get_account(vid).reserved_micro == held
    with db()() as session:
        row = session.get(Job, jid)
        row.status, row.run_after, row.scheduled_for = "retry_wait", None, None
        session.commit()
        repeated = lease(session, "replacement-worker", kinds=["idea.clarify_turn"], n=1)[0]
        session.commit()
    asyncio.run(_run_one(db(), repeated, {jid}))
    assert len(calls) == 1 and job_row(jid).last_error == "clarification_outcome_uncertain"  # no redial after an ambiguous call
    assert "secret" not in job_row(jid).last_error


def test_missing_byok_secret_never_falls_back_to_platform(client, monkeypatch):
    from app.llm import founder_provider
    calls = install_provider(monkeypatch)
    vid, wid, sid = venture(client)
    founder_provider.set_founder_config(workspace_id=wid, provider="azure_foundry", model="founder-model", base_url="https://byok.example.test/v1", api_key="key")
    monkeypatch.setattr(founder_provider, "to_provider_config", lambda _: None)
    jid = submit(client, f"/ventures/{vid}/idea/sessions/{sid}", "An order board for bakers")
    run_queued("idea.clarify_turn")
    assert not calls and job_row(jid).last_error == "clarification_byok_unavailable"


def test_provider_rejection_retries_only_after_new_job_lease(client, monkeypatch):
    from app.db.models import Job
    from app.state import db
    from llm_gateway.errors import ProviderCallError
    attempts = []
    def rejected_once(*args):
        attempts.append(1)
        if len(attempts) == 1:
            raise ProviderCallError("rate_limited", "retry after", status=429, retryable=True)
        return completion(proposal=False)
    calls = install_provider(monkeypatch, rejected_once)
    vid, _, sid = venture(client)
    jid = submit(client, f"/ventures/{vid}/idea/sessions/{sid}", "An order board for bakers")
    run_queued("idea.clarify_turn")
    assert job_row(jid).status == "retry_wait" and len(calls) == 1
    assert receipt(jid).state == "retryable"
    with db()() as session:
        job = session.get(Job, jid)
        job.run_after, job.scheduled_for = None, None
        session.commit()
    run_queued("idea.clarify_turn")
    assert job_row(jid).status == "succeeded", job_row(jid).last_error
    assert len(calls) == 2 and receipt(jid).attempt == 2


def test_signed_credit_summary_exposes_actual_clarifier_usage_and_denies_foreign(client, monkeypatch):
    install_provider(monkeypatch)
    vid, wid, sid = venture(client)
    jid = submit(client, f"/ventures/{vid}/idea/sessions/{sid}", "An order board for bakers")
    run_queued("idea.clarify_turn")
    assert job_row(jid).status == "succeeded"
    path = f"/workspaces/{wid}/credits/summary"
    data = request(client, "GET", path).json()
    assert data["status"] == "available" and float(data["balance"]) > 0
    assert float(data["burn_7d"]["idea.clarify_turn"]) > 0
    assert float(data["spent"]) > 0 and float(data["reserved"]) == 0
    assert data["available"] == data["balance"]
    assert request(client, "GET", path, "usr_bob").status_code == 404
    assert client.get(path).status_code == 401
    usage = f"/workspaces/{wid}/credits/usage"
    assert request(client, "GET", usage, params={"group_by": "app"}).status_code == 422


def test_operator_offline_setting_prevents_provider_call(client, monkeypatch):
    from app.credits.accounts import get_account
    calls = install_provider(monkeypatch)
    monkeypatch.setenv("LLM_OFFLINE", "1")
    vid, _, sid = venture(client)
    before = get_account(vid)
    usage = (before.spent_micro, before.reserved_micro)
    jid = submit(client, f"/ventures/{vid}/idea/sessions/{sid}", "An order board for bakers")
    run_queued("idea.clarify_turn")
    assert not calls
    assert job_row(jid).status == "succeeded" and job_row(jid).last_error is None
    after = get_account(vid)
    assert (after.spent_micro, after.reserved_micro) == usage

"""A founder's first minutes: answers advance, supplied facts stay answered, nothing dead-ends."""
from __future__ import annotations

import json
import re
from contextlib import contextmanager

import pytest

from tests.integration.test_entry_chat_api import client as base_client
from tests.integration.test_postgres_auth_security import pg_database as pg_database

CASES = {
    "b2b": {
        "vague": "Perhaps something for freight dispatch teams. I'm not sure what yet.",
        "answers": ["Drivers wait hours because loads get double booked", "A shared board showing each truck's next load"],
        "idea": "A tool for freight dispatch teams that shows each truck's next load",
        "described": "I'm making a load board for regional freight dispatchers. They struggle because loads get double booked. "
                     "You can assign a load and see each truck's next stop.",
        "mechanism": "You can assign a load",
    },
    "consumer": {
        "vague": "Maybe something for people who bake at home. I don't know what yet.",
        "answers": ["Loaves fail because starters are forgotten", "A reminder that follows each starter's feeding cycle"],
        "idea": "An app for home sourdough bakers that tracks starter feedings",
        "described": "I'm building a starter journal for home sourdough bakers. They struggle because feedings are forgotten. "
                     "You can log a feeding and get the next reminder.",
        "mechanism": "You can log a feeding",
    },
}
cases = pytest.mark.parametrize("case", CASES.values(), ids=CASES.keys())

audit_domains = pytest.mark.parametrize("domain,idea,background", [
    ("rivetworks.invalid", "A tool for sheet-metal shops to estimate material waste", "I have worked as a sheet metal estimator"),
    ("mossmornings.invalid", "An app for apartment dwellers to remember watering their ferns", "I maintain a collection of indoor ferns"),
], ids=["b2b", "consumer"])


@audit_domains
def test_audit_rejected_credential_never_enters_entry_history(client, domain, idea, background):
    from sqlalchemy import select
    from app.db.supymem_models import EntryChatProposal
    from app.state import db

    parent = preview(client, f"I'm thinking about {background.removeprefix('I ')}.")
    proposal = parent.get("proposal", parent)
    value = f"fixture-value-for-{domain}"
    responses = [client.post("/entry/chat/preview", json={"text": f"{idea}\npassword = {value}", **extra})
                 for extra in ({}, {"previous_proposal_id": proposal["id"]})]
    with db()() as session:
        rows = session.scalars(select(EntryChatProposal)).all()
        assert all(value not in row.text and value not in (row.receipt_json or "") for row in rows)
        legacy = session.get(EntryChatProposal, proposal["id"])
        legacy.text += f"\npassword = {value}"
        session.commit()
    assert all(response.status_code == 422 and "Settings > Keys and connections" in response.text for response in responses)
    assert value not in client.get("/entry/chat/history").text
    assert value not in client.get(f"/entry/chat/proposals/{proposal['id']}").text


@audit_domains
def test_audit_entry_website_consent_and_sample_mode(tmp_path, monkeypatch, domain, idea, background):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    with contextmanager(base_client.__wrapped__)(tmp_path, monkeypatch) as client:
        from app.core import providers_local
        from app.intake import application, assets

        calls = []
        sample = False
        monkeypatch.setattr(providers_local, "_sample_selected", lambda: sample)
        monkeypatch.setattr(application, "_page", lambda url, **kwargs: calls.append(url) or {
            "url": url, "status": "ok", "content": f"{idea}\nGet started", "website_answers": {"offer": idea}})
        monkeypatch.setattr(assets, "import_site", lambda venture, url: calls.append(url) or {"site": {"url": url}})
        url = f"https://{domain}"
        for sample, instruction, should_read in [(False, f"Do not open {url}.", False),
                                                  (True, f"My website is {url}", False),
                                                  (False, f"My website is {url}", True)]:
            calls.clear()
            response = preview(client, f"{idea}. {instruction}")
            proposal = response.get("proposal", response)
            assert bool(calls) == should_read
            calls.clear()
            confirmed = client.post("/entry/chat/confirm", json={"proposal_id": proposal["id"],
                "name": "Test venture", "assets": {"website": url}})
            assert bool(calls) == should_read
            assert confirmed.status_code == 200, confirmed.text
            if not should_read:
                assert "paste the relevant page text into Add Venture" in confirmed.json()["message"]


@audit_domains
def test_audit_expired_delete_clears_only_deleted_entry_and_chat(tmp_path, monkeypatch, domain, idea, background):
    from datetime import timedelta

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    with contextmanager(base_client.__wrapped__)(tmp_path, monkeypatch) as client:
        from sqlalchemy import select
        from app.core.local_worker import tick_kinds
        from app.db.models import ChatMessage
        from app.db.supymem_models import EntryChatProposal
        from app.state import db
        from app.ventures import lifecycle
        from app.worker.handlers import _retention_purge

        def turn(text, previous=None, request_id=None):
            response = client.post("/entry/chat/preview", json={"text": text,
                "request_id": request_id,
                **({"previous_proposal_id": previous} if previous else {})})
            assert response.status_code == 200, response.text
            return response.json()["proposal"]["id"]

        def add(proposal_id, name):
            response = client.post("/entry/chat/confirm", json={"proposal_id": proposal_id, "name": name})
            assert response.status_code == 200, response.text
            return response.json()["venture_id"]

        def history_ids():
            response = client.get("/entry/chat/history")
            assert response.status_code == 200, response.text
            return {item["proposal"]["id"] for item in response.json()["items"]}

        opening = f"{idea}. Do not open https://{domain}."
        first = turn(opening, request_id="retention-opening")
        middle = turn(background, first)
        final = turn("There is no working product yet. I want to start with a prototype.", middle)
        removed = add(final, "First notebook")
        removed_turns = {first, middle, final}
        other = turn("A tool for survey crews to tag broken equipment")
        retained = add(other, "Second notebook")
        draft = turn(opening)
        from app.workspaces.resolve import LOCAL_PERSON_ID

        assert removed_turns | {other, draft} <= history_ids()
        lifecycle.soft_delete(removed, confirmation="DELETE", actor_id=LOCAL_PERSON_ID)
        hidden = removed_turns.isdisjoint(history_ids())
        hidden_detail = client.get(f"/entry/chat/proposals/{first}").status_code
        hidden_replay = client.post("/entry/chat/preview", json={"text": opening,
            "request_id": "retention-opening"}).status_code
        lifecycle.restore(removed, actor_id=LOCAL_PERSON_ID)
        assert removed_turns <= history_ids()
        lifecycle.soft_delete(removed, confirmation="DELETE", actor_id=LOCAL_PERSON_ID)
        with db()() as session:
            for proposal_id in (first, middle):
                session.get(EntryChatProposal, proposal_id).target_venture_id = None
            session.commit()
        expires = lifecycle.utcnow() + timedelta(days=8)
        monkeypatch.setattr(lifecycle, "utcnow", lambda: expires)
        assert "retention.purge" in tick_kinds("daily")
        _retention_purge({})
        assert removed_turns.isdisjoint(history_ids())
        assert {other, draft} <= history_ids()
        with db()() as session:
            assert not session.scalars(select(EntryChatProposal).where(EntryChatProposal.id.in_(removed_turns))).first()
            assert not session.scalars(select(ChatMessage).filter_by(venture_id=removed)).first()
            assert session.scalars(select(ChatMessage).filter_by(venture_id=retained)).first()
        assert hidden and hidden_detail == hidden_replay == 404


@audit_domains
def test_audit_preventure_requests_use_saved_model(tmp_path, monkeypatch, domain, idea, background):
    from types import SimpleNamespace
    from tests.company_os_harness import configure_local_test_model

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    with contextmanager(base_client.__wrapped__)(tmp_path, monkeypatch) as client:
        from app.credits import metered
        from app.workspaces.resolve import LOCAL_DESK_ID

        configure_local_test_model()
        calls = []

        async def transmit(messages, *, purpose, config, workspace_id, **kwargs):
            calls.append((purpose, config.provider, workspace_id))
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({
                "answer": "Start by asking one person about their current routine."})))],
                usage={"prompt_tokens": 25, "completion_tokens": 12, "total_tokens": 37})

        monkeypatch.setattr(metered, "_transmit", transmit)
        response = preview(client, f"I don't have an idea yet. {background}.")
        proposal = response.get("proposal", response)
        question = client.post("/entry/chat/preview", json={"text": "What is customer discovery?",
            "previous_proposal_id": proposal["id"]})
        assert question.status_code == 200, question.text
        assert "asking one person" in question.json()["body_md"]
        assert [purpose for purpose, _, _ in calls] == ["intake.classify", "venture.chat.assistant"]
        assert all(workspace == LOCAL_DESK_ID for _, _, workspace in calls)


@audit_domains
def test_audit_confirm_retries_initial_question_in_same_venture(client, monkeypatch, domain, idea, background):
    from app.idea import service

    original = service.post_message
    calls = []

    def post_message(session_id, *args, **kwargs):
        calls.append(session_id)
        if len(calls) == 1:
            raise RuntimeError("simulated queue outage")
        return original(session_id, *args, **kwargs)

    monkeypatch.setattr(service, "post_message", post_message)
    proposal = preview(client, idea)["proposal"]
    payload = {"proposal_id": proposal["id"], "name": "Question retry"}
    first = client.post("/entry/chat/confirm", json=payload)
    assert first.status_code == 503 and "initial_clarification_unavailable" in first.text
    second = client.post("/entry/chat/confirm", json=payload)
    assert second.status_code == 200, second.text
    assert len(client.get("/ventures").json()["items"]) == 1
    assert len(calls) == 2 and calls[0] == calls[1]


@audit_domains
def test_audit_site_facts_survive_clarification(client, monkeypatch, domain, idea, background):
    from app.intake import application, assets

    url = f"https://{domain}"
    calls = []

    def page(target, **kwargs):
        calls.append(target)
        if len(calls) == 1:
            return {"url": target, "status": "ok", "content": idea, "website_answers": {"offer": idea}}
        return {"url": target, "status": "failed", "content": ""}

    monkeypatch.setattr(application, "_page", page)
    monkeypatch.setattr(assets, "import_site", lambda venture, site: {"site": {"url": site}})
    first = preview(client, f"My website is {url}")
    proposal = first.get("proposal", first)
    assert proposal["website_answers"].get("offer") == idea
    response = client.post("/entry/chat/preview", json={"text": f"My website is {url}\nClarification: I already have a live product with paying customers.",
        "previous_proposal_id": proposal["id"]})
    assert response.status_code == 200, response.text
    second = response.json().get("proposal", response.json())
    assert second["website_answers"].get("offer") == idea
    receipt = client.post("/entry/chat/confirm", json={"proposal_id": second["id"], "name": "Website notes"})
    assert receipt.status_code == 200, receipt.text
    assert receipt.json()["intake"]["website_answers"]["offer"] == idea


@audit_domains
def test_audit_discovery_skip_does_not_loop_after_reload(client, domain, idea, background):
    first = preview(client, f"I don't have an idea yet. {background}.")
    seen = set()
    for _ in range(5):
        proposal = first.get("proposal", first)
        question = proposal.get("onboarding_question") or proposal.get("question")
        assert question and question not in seen
        seen.add(question)
        saved = client.get(f"/entry/chat/proposals/{proposal['id']}").json()["proposal"]
        assert saved.get("onboarding_question", saved["message"]) == question
        if not proposal.get("onboarding_field"):
            assert len(seen) >= 2
            break
        response = client.post("/entry/chat/preview", json={"text": "Skip this question",
            "previous_proposal_id": proposal["id"]})
        assert response.status_code == 200, response.text
        first = response.json()


@audit_domains
def test_audit_rename_limit_does_not_truncate(client, domain, idea, background):
    proposal = preview(client, idea)["proposal"]
    receipt = client.post("/entry/chat/confirm", json={"proposal_id": proposal["id"], "name": "Original notebook"})
    assert receipt.status_code == 200, receipt.text
    path = f"/ventures/{receipt.json()['venture_id']}"
    name = (domain.split('.')[0] + " workspace for planning and notes").ljust(48, "x")[:48]
    assert client.patch(path, json={"name": name}).json()["name"] == name
    rejected = client.patch(path, json={"name": name + "s"})
    assert rejected.status_code == 422 and "Manage Ventures" in rejected.text
    assert client.get(path).json()["name"] == name


@pytest.mark.parametrize("idea,name", [
    ("Equipment monitoring for delivery vehicles so depot managers spot faults early", "DepotSignal"),
    ("Activity reminders for rescue dogs so new pet owners establish a routine", "PawRhythm"),
], ids=["b2b", "consumer"])
def test_concrete_idea_names_first_and_keeps_only_missing_questions(client, idea, name):
    from app.db.supymem_models import EntryChatProposal
    from app.state import db

    result = preview(client, idea)
    assert result["status"] == "proposal", result
    proposal = result["proposal"]
    assert not proposal.get("onboarding_question"), proposal
    assert proposal["message"] == "What should we call it?"
    with db()() as session:
        row = session.get(EntryChatProposal, proposal["id"])
        saved = json.loads(row.receipt_json)
        assert saved["onboarding"].get("field") != "customer"
        saved["onboarding"].update(field="context", question="Which problem would you solve first?", choices=[])
        row.receipt_json = json.dumps(saved)
        session.commit()
    resumed = client.get(f"/entry/chat/proposals/{proposal['id']}").json()["proposal"]
    assert not resumed.get("onboarding_question") and resumed["message"] == "What should we call it?"
    response = client.post("/entry/chat/confirm", json={"proposal_id": proposal["id"], "name": name})
    assert response.status_code == 200, response.text
    assert client.get(f"/entry/chat/proposals/{proposal['id']}").json()["proposal"]["venture_name"] == name


@pytest.mark.parametrize("opening,name", [
    ("A tool for depot managers", "DepotSignal"),
    ("An app for pet owners", "PawRhythm"),
], ids=["b2b", "consumer"])
def test_name_like_reply_cannot_be_saved_as_a_discovery_answer(client, monkeypatch, opening, name):
    from app.db.supymem_models import EntryChatProposal
    from app.state import db
    from app.idea.agent import name_like_reply

    with monkeypatch.context() as boundary:
        boundary.setattr("app.secrets.detect.looks_like_secret", lambda value: value == name)
        assert not name_like_reply(name, "offer")

    proposal = preview(client, opening).get("proposal")
    assert proposal
    with db()() as session:
        parent = session.get(EntryChatProposal, proposal["id"])
        before = (parent.text, parent.receipt_json, session.query(EntryChatProposal).count())
    for text in (name, name + ".", f'"{name}"', opening + "\nClarification: " + name):
        response = client.post("/entry/chat/preview", json={"text": text, "previous_proposal_id": proposal["id"]})
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "reply"
        assert "name" in response.json()["body_md"] and "describe" in response.json()["body_md"]
    with db()() as session:
        parent = session.get(EntryChatProposal, proposal["id"])
        assert (parent.text, parent.receipt_json, session.query(EntryChatProposal).count()) == before
    from app.idea.service import get_session, post_message
    from app.chat.router import _answer_idea
    from app.chat.store import append_message
    from app.idea.runner import _question_reply
    import asyncio
    from tests.unit.chat.test_founder_intake_progress import _pending_questions

    cancelled = client.post(f"/entry/chat/proposals/{proposal['id']}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    venture_id, session_id, question_id = _pending_questions(client, opening, "", opening, field="offer")
    before = get_session(session_id)
    accepted = post_message(session_id, name, expected_question_id=question_id, reply_in_chat=True)
    assert accepted["job_id"] is None
    assert "name" in accepted["note"] and "describe" in accepted["note"]
    assert get_session(session_id) == before
    question = before["turns"][-1]
    body, actions = _question_reply(question["text"], question["options"], question_id)
    append_message(venture_id, role="assistant", kind="idea_clarification_ready", body_md=body, actions=actions)
    reply = asyncio.run(_answer_idea(venture_id, name, (session_id, question_id)))
    assert "Did you mean" in reply["body_md"] and "Rename this venture" in reply["body_md"]
    assert get_session(session_id) == before


@cases
def test_name_step_copy_fits_an_already_answered_discovery(client, case):
    from app.intake.smart import classify_fallback

    assert classify_fallback(case["described"]).founder_line == "You can continue with your idea, and I'll ask only for missing details."
    result = preview(client, case["described"] + "\nClarification: " + case["mechanism"])
    assert result["status"] == "proposal", result
    assert result["proposal"]["message"] == "What should we call it?"
    assert not result["proposal"].get("onboarding_question")


@pytest.mark.parametrize("audience", ["music teachers", "home gardeners"], ids=["b2b", "consumer"])
def test_partial_idea_echo_preserves_uncertainty_words(client, audience):
    from app.entry.chat_start import _onboarding_progress

    text = f"Something for {audience}, I'm not sure if this would help."
    intake = {"path": "startup", "extracted": {"idea": text, "audience": audience},
              "prefill_answers": {"customer": audience},
              "next_step": {"action": "ask", "field": "customer", "situation": "clear_idea",
                            "question": "Who would use it?", "examples": []}}
    progress = _onboarding_progress(intake, text)
    assert "I'm not sure if this would help." in progress["question"]
    assert "I'm if" not in progress["question"]


@pytest.mark.parametrize("product,beneficiary,mechanism,customer", [
    ("routing platform", "freight depots", "matches truck slots to driver shifts",
     "dispatch managers at regional transport firms"),
    ("trail planning app", "hiking clubs", "matches routes to daylight hours",
     "parents hiking with young children"),
], ids=["b2b", "consumer"])
def test_first_message_keeps_stated_customer_despite_demand_uncertainty(client, product, beneficiary, mechanism, customer):
    from app.entry.chat_start import _onboarding_progress
    from app.intake.smart import classify_fallback

    opening = f"We are building a {product} for {beneficiary} that {mechanism}, for {customer}."
    text = opening + " Nobody has tried it. I don't know whether anyone would pay."
    intake = classify_fallback(text).to_dict()
    assert intake["prefill_answers"]["customer"] == customer
    progress = _onboarding_progress(intake, text)
    assert "customer" in progress["completed"]
    assert not progress.get("question") and not progress["assumptions"]


@cases
def test_saved_entry_context_is_subject_bound(client, case):
    from app.authz.workspace import ensure_subject_person
    from app.entry import chat_start

    saved = preview(client, case["idea"])
    identifier = saved.get("proposal", saved)["id"]
    assert chat_start.get_text(identifier, ensure_subject_person(None)) == case["idea"]
    with pytest.raises(chat_start.ChatStartError) as denied:
        chat_start.get_text(identifier, "another-founder")
    assert denied.value.status == 404


@pytest.mark.parametrize("opening,answer", [
    ("A scanner that checks linen deliveries", "Commercial laundry teams"),
    ("An app that records paddling trips", "Recreational canoeists"),
], ids=["b2b", "consumer"])
def test_setup_document_request_keeps_the_question_and_real_answer(client, monkeypatch, opening, answer):
    opening = f"FirstLight: {opening}"
    from app.api import entry_api
    from app.db.supymem_models import EntryChatProposal
    from app.intake import application, smart
    from app.state import db

    async def interpret(text, **kwargs):
        return smart.classify_fallback(text).to_dict()

    calls = []

    async def write(text, actor_id, request_id, *, context=""):
        calls.append(text)
        assert opening in context
        return "Your summary is ready. You can choose your first audience next."

    monkeypatch.setattr(application, "interpret", interpret)
    monkeypatch.setattr(entry_api, "_entry_question_answer", write)
    first = preview(client, opening)
    proposal = first.get("proposal", first)
    identifier = proposal["id"]
    with db()() as session:
        parent = session.get(EntryChatProposal, identifier)
        saved = (parent.text, parent.receipt_json, parent.state)
    for request in ("Please draft a concise overview of this concept.",
                    "Could you prepare a document describing the assumptions?"):
        response = client.post("/entry/chat/preview", json={"text": request, "previous_proposal_id": identifier})
        assert response.status_code == 200, response.text
        reply = response.json()
        assert reply["status"] == "reply"
        assert calls[-1] == request
        assert reply["body_md"].endswith(proposal["onboarding_question"])
        with db()() as session:
            parent = session.get(EntryChatProposal, identifier)
            assert (parent.text, parent.receipt_json, parent.state) == saved
    response = client.post("/entry/chat/preview", json={"text": answer, "previous_proposal_id": identifier})
    assert response.status_code == 200, response.text
    continued = response.json()
    with db()() as session:
        parent = session.get(EntryChatProposal, continued.get("proposal", continued)["id"])
        assert json.loads(parent.receipt_json)["intake"]["prefill_answers"]["customer"] == answer
        assert all(request not in parent.text for request in calls)


@pytest.mark.hosted_only
@pytest.mark.parametrize("opening,audience,review", [
    ("A scanner that checks linen deliveries", "Commercial laundry teams",
     "Please review the concept I've described."),
    ("An app that records paddling trips", "Recreational canoeists",
     "Review the idea from my answers."),
], ids=["b2b", "consumer"])
@pytest.mark.parametrize("pending", [True, False], ids=["pending", "ready"])
def test_setup_review_keeps_the_saved_answers(client, monkeypatch, opening, audience, review, pending):
    opening = f"FirstLight: {opening}"
    from app.api import entry_api
    from app.chat.idea_bridge import informational_request
    from app.db.models import Job, Venture, VentureDocument
    from app.db.supymem_models import EntryChatProposal
    from app.intake import application, smart
    from app.state import db

    async def interpret(text, **kwargs):
        return smart.classify_fallback(text).to_dict()

    monkeypatch.setattr(application, "interpret", interpret)
    first = preview(client, opening)
    proposal = first.get("proposal", first)
    assert proposal.get("onboarding_question")
    if not pending:
        response = client.post("/entry/chat/preview", json={"text": audience, "previous_proposal_id": proposal["id"]})
        assert response.status_code == 200, response.text
        continued = response.json()
        proposal = continued.get("proposal", continued)
        assert not proposal.get("onboarding_question"), continued
    identifier = proposal["id"]

    def snapshot():
        with db()() as session:
            saved = session.get(EntryChatProposal, identifier)
            return ((saved.text, saved.receipt_json, saved.state),
                    [session.query(model).count() for model in (EntryChatProposal, VentureDocument, Venture, Job)])

    before = snapshot()
    assert opening in before[0][0]
    if not pending:
        assert json.loads(before[0][1])["intake"]["prefill_answers"]["customer"] == audience
    calls = []
    summary = f"Your current idea: {opening}. You can correct the summary or continue with your idea."

    async def answer(text, actor_id, request_id, *, context=""):
        calls.append(text)
        assert context == before[0][0]
        return summary

    monkeypatch.setattr(entry_api, "_entry_question_answer", answer)
    response = client.post("/entry/chat/preview", json={"text": review, "previous_proposal_id": identifier})
    assert response.status_code == 200, response.text
    reply = response.json()
    assert reply["status"] == "reply", reply
    assert informational_request(review)
    assert not informational_request("You can review each delivery and record its condition.")
    assert calls == [review]
    assert reply["body_md"] == summary + (f"\n\nBack to your idea: {proposal['onboarding_question']}" if pending else "")
    assert reply["actions"] == []
    assert snapshot() == before
    denied = client.post("/entry/chat/preview", headers={"X-Subject": "another-person"},
                         json={"text": review, "previous_proposal_id": identifier})
    assert denied.status_code == 422, denied.text
    assert calls == [review]
    assert snapshot() == before


@pytest.mark.parametrize("audience,context", [
    ("Couriers", "Sort delivery manifests before loading."),
    ("Swimmers", "Record pool sessions after each visit."),
], ids=["b2b", "consumer"])
def test_inferred_name_cannot_cross_a_setup_line(client, audience, context):
    from app.entry.chat_start import _stated_name

    assert _stated_name(f"{audience}\nContext: {context}") is None
    assert _stated_name(f"Harbor Notebook: A tracking tool\nAudience: {audience}\nContext: {context}") == "Harbor Notebook"
    assert _stated_name(f"Audience: {audience}\nThe venture name is Harbor Notebook.") == "Harbor Notebook"


@pytest.mark.hosted_only
@pytest.mark.parametrize("case,side_question,side_answer", [
    (CASES["b2b"], "Why do copper pipes turn green?",
     "You are seeing surface corrosion. You can check whether the pipe is leaking next."),
    (CASES["consumer"], "Could you explain how rainbows form?",
     "You see sunlight refracted and reflected in water droplets. You can look opposite the sun after rain."),
], ids=["b2b", "consumer"])
def test_ready_setup_side_question_preserves_the_known_direction(client, monkeypatch, case, side_question, side_answer):
    from app.api import entry_api
    from app.db.models import Job, Venture, VentureDocument
    from app.db.supymem_models import EntryChatProposal
    from app.state import db

    first = preview(client, case["described"])
    proposal = first["proposal"]
    assert first["status"] == "proposal" and not proposal.get("onboarding_question")
    identifier = proposal["id"]

    def snapshot():
        with db()() as session:
            parent = session.get(EntryChatProposal, identifier)
            return ((parent.text, parent.receipt_json, parent.state),
                    [session.query(model).count() for model in (EntryChatProposal, VentureDocument, Venture, Job)])

    before = snapshot()
    calls = []

    async def answer(text, actor_id, request_id, *, context=""):
        calls.append(text)
        assert case["described"] in context
        return side_answer

    monkeypatch.setattr(entry_api, "_entry_question_answer", answer)
    response = client.post("/entry/chat/preview", json={"text": side_question, "previous_proposal_id": identifier})
    assert response.status_code == 200, response.text
    reply = response.json()
    assert reply == {"status": "reply", "body_md": side_answer, "actions": []}
    assert calls == [side_question]
    assert snapshot() == before
    denied = client.post("/entry/chat/preview", headers={"X-Subject": "another-person"},
                         json={"text": side_question, "previous_proposal_id": identifier})
    assert denied.status_code == 422, denied.text
    assert calls == [side_question]
    assert snapshot() == before


@pytest.mark.hosted_only
@pytest.mark.parametrize("opening,side_question,side_answer,setup_answer", [
    ("I want to build a product for commercial laundry operators",
     "Quick question: what is the difference between a comet and an asteroid?",
     "You can distinguish them by composition: comets contain ice, while asteroids are mostly rock or metal.",
     "Staff can scan each delivery and see which linens have not returned."),
    ("I want to build an app for recreational canoeists",
     "How can I remove candle wax from cotton?",
     "You can let the wax harden, gently scrape it off, then wash according to the fabric's care label.",
     "You can record a trip and see which waterways you have paddled."),
], ids=["b2b", "consumer"])
def test_setup_side_question_answers_without_consuming_progress(
    client, monkeypatch, opening, side_question, side_answer, setup_answer,
):
    opening = f"FirstLight: {opening}"
    from types import SimpleNamespace

    from app.credits import metered
    from app.db.models import Job, Venture, VentureDocument
    from app.db.supymem_models import EntryChatProposal
    from app.intake import application, model_capture, smart
    from app.state import db
    from llm_gateway import provider_foundry
    from sqlalchemy import select

    calls = []

    async def answer(callback, **kwargs):
        assert kwargs["actor_id"]
        assert kwargs["request_id"].startswith("entry-question:")
        calls.append(callback.args[0][-1]["content"])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"answer": side_answer})))])

    async def classify(text, **kwargs):
        return smart.classify_fallback(text).to_dict()

    first = preview(client, opening)
    proposal_id = first.get("id") or first["proposal"]["id"]
    with db()() as session:
        parent = session.get(EntryChatProposal, proposal_id)
        saved = (parent.text, parent.state, parent.receipt_json)
        counts = [session.query(model).count() for model in (EntryChatProposal, VentureDocument, Venture, Job)]
    with monkeypatch.context() as model:
        model.delenv("LLM_OFFLINE")
        model.delenv("SANDBOX_MODE", raising=False)
        model.setattr(application, "interpret", classify)
        model.setattr(metered, "call", answer)
        model.setattr(smart, "_consumer_account", lambda actor_id: SimpleNamespace(id="account"))
        model.setattr(provider_foundry, "load_provider_config_from_env",
                      lambda: SimpleNamespace(provider="azure", captured_tier="flash"))
        model.setattr(model_capture, "capture_request", lambda messages, **kwargs: (
            kwargs["config"], messages, kwargs["response_schema"], {
                "model_binding": {"deployment": "test", "max_output_tokens": 512},
                "estimate_micro": 1, "token_ceiling": 1024, "request_digest": "test",
            }))
        response = client.post("/entry/chat/preview", json={
            "text": side_question, "previous_proposal_id": proposal_id,
            "request_id": "side-question",
        })
    assert response.status_code == 200, response.text
    reply = response.json()
    assert reply["status"] == "reply"
    assert calls == [side_question]
    assert reply["body_md"].startswith(side_answer)
    question = first.get("onboarding_question") or first.get("question") or first["proposal"]["onboarding_question"]
    assert question
    assert reply["body_md"].endswith(question)
    assert "proposal" not in reply and "id" not in reply
    assert not re.search(r"confirmation|credits|\u2014", reply["body_md"], re.IGNORECASE)
    with db()() as session:
        parent = session.get(EntryChatProposal, proposal_id)
        assert (parent.text, parent.state, parent.receipt_json) == saved
        assert [session.query(model).count() for model in (EntryChatProposal, VentureDocument, Venture, Job)] == counts
    denied = client.post("/entry/chat/preview", headers={"X-Subject": "another-person"}, json={
        "text": side_question, "previous_proposal_id": proposal_id,
    })
    assert denied.status_code == 422, denied.text
    assert calls == [side_question]
    continued = client.post("/entry/chat/preview", json={
        "text": setup_answer, "previous_proposal_id": proposal_id,
    })
    assert continued.status_code == 200, continued.text
    assert continued.json()["status"] != "reply"
    assert calls == [side_question]
    with db()() as session:
        texts = session.scalars(select(EntryChatProposal.text)).all()
        assert any(setup_answer in text for text in texts)
        assert all(side_question not in text for text in texts)


@cases
def test_hedged_offer_answers_and_skips_advance(client, monkeypatch, case):
    from app.intake import application, smart

    async def interpret(text, **kwargs):
        result = smart.classify_fallback(text).to_dict()
        result["next_step"] = {"action": "ask", "situation": "vague_idea", "field": "offer",
            "question": "What task would it help you complete?",
            "examples": ["Track a missed deadline", "Organize a shared handoff"]}
        return result

    monkeypatch.setattr(application, "interpret", interpret)
    opening = case["vague"]
    first = preview(client, opening)
    previous = first.get("proposal", first)
    response = client.post("/entry/chat/preview", json={"text": case["idea"] + " I am unsure whether anyone would pay.",
        "previous_proposal_id": previous["id"]})
    assert response.status_code == 200, response.text
    answered = response.json()
    proposal = answered.get("proposal", answered)
    assert proposal.get("onboarding_field") != "offer", answered
    response = client.post("/entry/chat/preview", json={"text": "Skip that question for now.",
        "previous_proposal_id": proposal["id"]})
    assert response.status_code == 200, response.text
    skipped = response.json()
    assert skipped["status"] == "proposal", skipped
    assert not skipped["proposal"].get("onboarding_question"), skipped


@pytest.mark.parametrize("words", [
    ("Meters", "Leaks", "Pipes", "Plumbers", "Threshold", "Pressure"),
    ("Journal", "Forgotten", "Feedings", "Bakers", "Time", "Interval"),
], ids=["b2b", "consumer"])
def test_one_word_setup_keeps_completed_answers_across_skips(client, monkeypatch, words):
    from app.db.models import Venture
    from app.db.supymem_models import EntryChatProposal
    from app.intake import application, smart
    from app.state import db

    async def interpret(text, **kwargs):
        result = smart.classify_fallback(text).to_dict()
        return {**result, "path": "startup", "confidence": .95, "next_step": None}

    monkeypatch.setattr(application, "interpret", interpret)
    previous_id = None
    completed = set()
    for word in (*words, "Skip", "Skip"):
        payload = {"text": word}
        if previous_id:
            payload["previous_proposal_id"] = previous_id
            if word != "Skip" and not smart.audience_phrase(word):
                payload["text"] = f"My answer is {word}"
        response = client.post("/entry/chat/preview", json=payload)
        assert response.status_code == 200, response.text
        result = response.json()
        proposal = result.get("proposal", result)
        previous_id = proposal["id"]
        with db()() as session:
            saved = session.get(EntryChatProposal, previous_id)
            progress = json.loads(saved.receipt_json)["onboarding"]
            assert completed <= set(progress.get("completed", [])), result
            completed.update(progress.get("completed", []))
    assert result["status"] == "proposal", result
    assert not proposal.get("onboarding_question"), result
    assert completed == {"offer", "customer"}
    with db()() as session:
        saved = session.get(EntryChatProposal, previous_id)
        assert all(word in saved.text for word in words)
        assert json.loads(saved.receipt_json)["intake"]["prefill_answers"]["customer"] == words[3]
    response = client.post("/entry/chat/confirm", json={"proposal_id": previous_id, "name": "Working name"})
    assert response.status_code == 200, response.text
    with db()() as session:
        raw_idea = session.get(Venture, response.json()["venture_id"]).raw_idea
        assert all(word in raw_idea for word in words)


@cases
def test_supplied_name_survives_source_and_later_changes(client, monkeypatch, case):
    from app.db.models import Venture
    from app.entry.chat_start import _stated_name
    from app.intake import application, smart
    from app.state import db

    original_name, chosen_name = "Cedar Ledger", "Willow Beacon"
    introduction = "I have selected a direction: " + original_name + " is a tool for " + smart._prefill(case["idea"])["customer"] + "."
    assert _stated_name(introduction) == original_name
    source = (case["described"] + " The name is " + original_name + ".\n"
        + "More planning details.\n" * 150 + "For the working plan: the venture name is " + chosen_name + ".")
    uploaded = client.post("/entry/evidence/files", files=[("files", ("pitch.txt", source.encode(), "text/plain"))])
    assert uploaded.status_code == 200, uploaded.text
    evidence_id = uploaded.json()["files"][0]["evidence_id"]
    async def interpret(text, **kwargs):
        result = smart.classify_fallback(text, page_context={"original_input": kwargs.get("original_input") or ""}).to_dict()
        result["extracted"]["venture_name"] = ""
        return result

    monkeypatch.setattr(application, "interpret", interpret)
    response = client.post("/entry/chat/preview", json={"text": case["described"], "evidence_ids": [evidence_id]})
    assert response.status_code == 200, response.text
    result = response.json()
    proposal = result["proposal"]
    assert proposal["venture_name"] == chosen_name
    chosen_name = "Birch Window"
    response = client.post("/entry/chat/preview", json={"text": "Please keep the venture name " + chosen_name + " and this direction, not " + original_name + ".\nSkip",
        "previous_proposal_id": proposal["id"]})
    assert response.status_code == 200, response.text
    proposal = response.json()["proposal"]
    assert proposal["venture_name"] == chosen_name
    response = client.post("/entry/chat/confirm", json={"proposal_id": proposal["id"]})
    assert response.status_code == 200, response.text
    with db()() as session:
        assert session.get(Venture, response.json()["venture_id"]).name == chosen_name


@cases
@pytest.mark.parametrize("invalid_saved_decision", [False, True], ids=["review", "recover"])
def test_review_idea_validates_before_creation_and_recovers(client, monkeypatch, case, invalid_saved_decision):
    from sqlalchemy import select
    from app.db.models import Job, Venture
    from app.db.supymem_models import EntryChatProposal, IdeaSession
    from app.intake import smart
    from app.state import db

    monkeypatch.setattr(client._transport, "raise_server_exceptions", False)
    opening = ("I have no working product yet. " + case["described"]
               + " My product helps with " + case["answers"][1].lower() + ".")
    assert smart.classify_fallback(opening).path == "gtm"
    proposal = preview(client, opening)["proposal"]
    assert proposal["route"] == proposal["intake_path"] == "startup"
    assert proposal["button_label"] == "Review your idea"
    name = "Your working name"
    if invalid_saved_decision:
        with db()() as session:
            saved = session.get(EntryChatProposal, proposal["id"])
            metadata = json.loads(saved.receipt_json)
            metadata["intake"]["founder_line"] += " Send your idea again."
            saved.receipt_json = json.dumps(metadata)
            session.commit()
        rejected = client.post("/entry/chat/confirm", json={"proposal_id": proposal["id"], "name": name})
        assert rejected.status_code == 422, rejected.text
        message = rejected.json()["detail"]["error"]["message"]
        assert "Send your idea again" in message
        assert not re.search(r"validation|schema|intake|proposal|credits|\u2014", message, re.I)
        with db()() as session:
            assert session.query(Venture).count() == session.query(IdeaSession).count() == session.query(Job).count() == 0
            assert session.get(EntryChatProposal, proposal["id"]).state == "pending"
        proposal = preview(client, opening)["proposal"]

    payload = {"proposal_id": proposal["id"], "name": name}
    response = client.post("/entry/chat/confirm", json=payload)
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt["next_step"] == "idea"
    with db()() as session:
        ventures = session.scalars(select(Venture)).all()
        assert len(ventures) == 1
        assert ventures[0].id == receipt["venture_id"] and ventures[0].name == name
        assert ventures[0].raw_idea == opening
        assert session.scalar(select(IdeaSession).where(IdeaSession.venture_id == receipt["venture_id"]))
        saved = session.get(EntryChatProposal, proposal["id"])
        assert smart.IntakeResult.model_validate(json.loads(saved.receipt_json)["intake"]).path == "startup"
    retried = client.post("/entry/chat/confirm", json=payload)
    assert retried.status_code == 200 and retried.json() == receipt
    with db()() as session:
        assert session.query(Venture).count() == session.query(IdeaSession).count() == 1


@cases
@pytest.mark.hosted_only
def test_entry_continuation_rejects_another_founder_even_when_cached(client, monkeypatch, case):
    from app.intake import application, smart
    from core.ids import new_id

    interpreted = []

    async def interpret(text, **kwargs):
        interpreted.append(text)
        return smart.classify_fallback(text).to_dict()

    monkeypatch.setattr(application, "interpret", interpret)
    previous_id = None
    for statement in [case["vague"], case["answers"][0], case["idea"]]:
        payload = {"text": statement, "request_id": new_id("ecr")}
        if previous_id:
            payload["previous_proposal_id"] = previous_id
        response = client.post("/entry/chat/preview", json=payload)
        assert response.status_code == 200, response.text
        details = response.json().get("proposal") or response.json()
        previous_id = details["id"]
        if payload.get("previous_proposal_id"):
            before = len(interpreted)
            rejected = client.post("/entry/chat/preview", json=payload, headers={"X-Subject": "usr_other_intake"})
            assert rejected.status_code == 422
            assert len(interpreted) == before


@cases
def test_standalone_entry_answers_reach_the_active_idea(client, monkeypatch, case, pg_database):
    import json
    from sqlalchemy import select
    from app import state
    from app.credits.migration import migrate_wallets
    from app.credits.policy import wallet_phase
    from app.db.models import ChatMessage, Venture
    from app.db.supymem_models import EntryChatProposal, IdeaRevision, IdeaSession
    from app.idea.agent import Clarification, Question, Understanding
    from app.idea.service import get_session
    from app.intake import application, evidence, smart
    from tests.idea_fixtures import completion, install_provider
    from tests.security.test_durable_journey_boundary import run_queued
    from core.ids import new_id

    monkeypatch.setenv("COMPANY_OS_DB_URL", pg_database[0])
    state.reset_db_factory()
    with state.db()() as session:
        migrate_wallets(session)
        session.commit()
        assert wallet_phase(session) == "person"
    interpreted = []
    async def interpret(text, **kwargs):
        interpreted.append(text)
        return smart.classify_fallback(text).to_dict()
    monkeypatch.setattr(application, "interpret", interpret)
    statements = [case["vague"], case["answers"][0], case["idea"]]
    previous_id = None
    identifiers = []
    for statement in statements:
        payload = {"text": statement, "request_id": new_id("ecr")}
        if previous_id:
            payload["previous_proposal_id"] = previous_id
        response = client.post("/entry/chat/preview", json=payload)
        assert response.status_code == 200, response.text
        result = response.json()
        details = result.get("proposal") or result
        previous_id = details["id"]
        assert all(answer in interpreted[-1] for answer in statements[:len(interpreted)])
        with state.db()() as session:
            proposal = session.get(EntryChatProposal, previous_id)
            saved = json.loads(proposal.receipt_json)["evidence_ids"]
            assert set(identifiers) < set(saved)
            identifiers = saved
        replay = client.post("/entry/chat/preview", json=payload)
        assert replay.status_code == 200 and replay.json() == result
        assert len(interpreted) == len(identifiers)
    assert result["status"] == "proposal"
    response = client.post("/entry/chat/confirm", json={"proposal_id": previous_id, "name": "Your startup"})
    assert response.status_code == 200, response.text
    venture_id = response.json()["venture_id"]
    assert {row["text"] for row in evidence.venture_evidence(venture_id)} == set(statements)
    understanding = Understanding(offer=case["idea"], customer=smart._prefill(case["idea"])["customer"], summary=case["idea"])
    answers = [("workflow", case["answers"][1], []),
        ("pricing_hypothesis", "I would charge $12 per month after a trial.", ["A recurring fee", "Pay for each use"])]
    responses = []
    for field, answer, options in answers:
        response = completion()
        response.choices[0].message.content = Clarification(case="know", action="question", understanding=understanding,
            message="Your idea is saved. Answer the next question to continue.",
            question=Question(text="What task should the first version help with?" if field == "workflow" else "How would you charge for it?",
                field=field, why="Describe your plan.", options=options)).model_dump_json()
        responses.append(response)
    responses.append(completion(understanding=understanding.model_dump()))
    response_sequence = iter(responses)
    calls = install_provider(monkeypatch, lambda *_: next(response_sequence))
    run_queued("idea.clarify_turn")
    with state.db()() as session:
        venture = session.get(Venture, venture_id)
        assert all(answer in venture.raw_idea for answer in statements)
        idea = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == venture_id))
        revision = session.get(IdeaRevision, idea.current_revision_id)
        assert json.loads(revision.structured)["offer"] == case["idea"]
        openings = session.scalars(select(ChatMessage.body_md).where(ChatMessage.venture_id == venture_id,
            ChatMessage.role == "user")).all()
        assert set(statements).issubset(openings)
        session_id = idea.id
    for field, answer, options in answers:
        saved = get_session(session_id)
        question = saved["turns"][-1]
        assert question["kind"] == "question"
        assert question["options"]["field"] == field
        assert bool(question["options"]["options"]) == bool(options)
        previous_revision = saved["current_revision"]
        payload = {"message": answer}
        if options:
            payload.update(reply_to_question=True, question_id=question["id"])
        response = client.post(f"/ventures/{venture_id}/chat", json=payload)
        assert response.status_code == 200, response.text
        run_queued("idea.clarify_turn")
        structured = get_session(session_id)["current_revision"]["structured"]
        assert structured["offer"] == case["idea"]
        assert structured[field] == answer
        with state.db()() as session:
            original = session.get(IdeaRevision, previous_revision["id"])
            assert json.loads(original.structured)[field] == previous_revision["structured"][field]
    assert len(calls) == 3


@cases
@pytest.mark.asyncio
async def test_interpretation_leaves_time_for_the_http_reply(client, monkeypatch, case):
    from app.intake import application, smart

    deadlines = []
    async def read_pages(*args, **kwargs):
        return {"pages": []}
    async def classify(text, **kwargs):
        deadlines.append(kwargs["budget_seconds"])
        return smart.classify_fallback(text)
    monkeypatch.setattr(application, "read_page_context", read_pages)
    monkeypatch.setattr(smart, "classify", classify)
    result = await application.interpret(case["described"], actor_id="person_budget")
    assert deadlines and 0 < deadlines[0] <= 16
    assert result["extracted"]["idea"]

DISCOVERY_DOMAINS = [
    ("identity governance for large engineering teams", "access security"),
    ("home care for adults with chronic illness", "personal health monitoring"),
]
DISCOVERY_OPENINGS = [
    "I work in {background}.",
    "My background is {background}.",
    "I'm interested in {interest}.",
    "Hey! I work in {background}.",
    "I work in {background}, but I don't know what to build.",
    "I don't have an idea yet. My background is {background}.",
    "I don't know. I'm interested in {interest}.",
    "How are you? My background is {background}.",
    "I have experience in {background}.",
    "We have worked in {background}.",
    "Our background is {background}.",
    "Good morning. I'm interested in {interest}.",
    "Hello. I don't know. I work in {background}.",
    "I have no startup idea yet. I'm interested in {interest}.",
    "Nothing in mind yet. My background is {background}.",
]
DISCOVERY_CASES = [
    (*DISCOVERY_DOMAINS[0], "Security teams at software companies",
     ["Monitor permission changes", "Review unused access", "Track access approval delays"], "Gate Lantern"),
    (*DISCOVERY_DOMAINS[1], "Adults managing chronic conditions",
     ["Track daily symptoms", "Organize medication reminders", "Prepare appointment notes"], "Care Compass"),
]


def preview(client, text):
    response = client.post("/entry/chat/preview", json={"text": text})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("domain,search", [
    ("b2b", "Find me ten regional freight dispatch teams I can interview"),
    ("consumer", "Find me ten home sourdough baking groups I can interview"),
])
def test_entry_starts_the_requested_customer_search_after_naming(tmp_path, monkeypatch, domain, search):
    from tests.company_os_harness import configure_local_test_model

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    with contextmanager(base_client.__wrapped__)(tmp_path, monkeypatch) as client:
        from app.chat import customers, execute

        configure_local_test_model()
        calls = []

        async def find(venture_id, query):
            calls.append((venture_id, query))
            return {"body_md": "Ready to look for customers.", "actions": [{"action": "gtm.find"}], "citations": []}

        async def start(venture_id, reply, actor_id, *, actions):
            assert actions == {"gtm.find"}
            calls.append((venture_id, actor_id))
            return {**reply, "body_md": "Your search started.", "actions": []}

        monkeypatch.setattr(customers, "find_reply", find)
        monkeypatch.setattr(execute, "start_requested_work", start)
        opening = CASES[domain]["idea"] + ". Nothing is built yet."
        first = preview(client, opening)
        parent = first.get("proposal") or first
        continuation = client.post("/entry/chat/preview", json={"text": search, "previous_proposal_id": parent["id"]})
        assert continuation.status_code == 200, continuation.text
        proposal = continuation.json()["proposal"]
        assert proposal["id"] != parent["id"] and not proposal.get("onboarding_question")
        assert "?" not in proposal["message"] and not calls
        payload = {"proposal_id": proposal["id"], "name": "Working name"}
        saved = client.post("/entry/chat/confirm", json=payload)
        assert saved.status_code == 200, saved.text
        result = saved.json()
        assert result["message"] == "Your search started."
        assert calls[0] == (result["venture_id"], search)
        assert client.post("/entry/chat/confirm", json=payload).json() == result
        assert len(calls) == 2


@pytest.mark.parametrize("offer,audience,stage", [
    ("A vibration sensor that tracks machinery faults for human review", "factory maintenance teams", "We have eight paid factory pilots generating $18,000 monthly revenue."),
    ("A wearable that records heart rhythms for clinical review", "adults recovering after surgery", "We have two supervised pilots, not paying customers."),
])
def test_read_attachment_prefills_entry_and_survives_continuation(client, monkeypatch, offer, audience, stage):
    from app import state
    from app.db.models import Venture
    from app.db.supymem_models import EntryChatProposal
    from app.intake import application, smart

    document = f"We build {offer.lower()} for {audience}.\n" + "More background.\n" * 500 + stage
    uploaded = client.post("/entry/evidence/files", files=[("files", ("pitch.txt", document.encode(), "text/plain"))])
    assert uploaded.status_code == 200
    identifier = uploaded.json()["files"][0]["evidence_id"]
    seen = []
    async def interpret(text, **kwargs):
        seen.append((text, kwargs.get("original_input")))
        return smart.classify_fallback(text, page_context={"original_input": kwargs.get("original_input") or ""}).to_dict()
    monkeypatch.setattr(application, "interpret", interpret)
    instruction = "Read my attached pitch and help me choose the next step."
    response = client.post("/entry/chat/preview", json={"text": instruction, "evidence_ids": [identifier]})
    assert response.status_code == 200, response.text
    details = response.json().get("proposal") or response.json()
    assert seen[-1] == (instruction, document)
    with state.db()() as session:
        saved = session.get(EntryChatProposal, details["id"])
        metadata = json.loads(saved.receipt_json)
        assert saved.text == instruction and metadata["original_source"] == document
        assert metadata["intake"]["prefill_answers"]["customer"] == audience
        assert metadata["intake"]["extracted"]["has_customers"] is (True if "revenue" in stage else None)
        assert not metadata["onboarding"].get("question")
    continued = client.post("/entry/chat/preview", json={"text": "Skip this question", "previous_proposal_id": details["id"]})
    assert continued.status_code == 200, continued.text
    assert seen[-1][1] == document
    assert identifier in continued.json()["evidence_ids"]
    venture_id = create(client, continued.json().get("proposal") or continued.json())
    with state.db()() as session:
        assert session.get(Venture, venture_id).raw_idea == document


@cases
@pytest.mark.parametrize("status", [
    "We have no customers.",
    "We have zero users.",
    "We have no sales.",
    "We are not getting users.",
    "I do not want to find customers.",
    "I don't need more sales.",
    "We are not trying to get users.",
    "I don't want marketing.",
])
def test_customer_absence_or_negation_keeps_idea_setup(client, monkeypatch, case, status):
    from app.db.supymem_models import IdeaSession
    from app.entry.chat_start import _classify
    from app.intake import application, smart
    from app.state import db
    from sqlalchemy import select

    opening = (case["described"] + " Our prototype needs several rounds of design changes before anyone can try it. "
               + status + " Please draft a page describing the idea, without contacting anyone.")
    classified = _classify(opening, None)
    assert classified and classified[0] == "startup" and classified[1] == []

    async def interpret(text, **kwargs):
        intake = smart.classify_fallback(text).to_dict()
        return {**intake, "path": "gtm", "confidence": 0.95,
                "founder_line": "Let's find your next customers.", "button_label": "Get customers"}

    monkeypatch.setattr(application, "interpret", interpret)
    proposal = preview(client, opening)["proposal"]
    assert proposal["route"] == proposal["intake_path"] == "startup"
    assert proposal["button_label"] == "Continue"
    assert proposal["skipped_steps"] == []
    venture_id = create(client, proposal)
    with db()() as session:
        assert session.scalar(select(IdeaSession).where(IdeaSession.venture_id == venture_id))

    live_opening = case["described"] + " The product is live. " + status
    live_proposal = preview(client, live_opening)["proposal"]
    live_venture_id = create(client, live_proposal)
    with db()() as session:
        assert session.scalar(select(IdeaSession).where(IdeaSession.venture_id == live_venture_id))

    established = case["described"] + " The product is live. We have no customers. Please find customers for it."
    assert _classify(established, None)[0] == "gtm"
    assert preview(client, established)["proposal"]["route"] == "gtm"


@cases
def test_pre_product_demand_test_does_not_skip_idea_review(client, case):
    from app.entry.chat_start import _classify

    opening = (case["described"] + " I have a clickable prototype, no working product, no customers and no revenue. "
               "Help me test whether people would pay before building or selling. Do not send anything.")
    classified = _classify(opening, None)
    assert classified and classified[0] == "startup" and classified[1] == []
    result = preview(client, opening)
    assert result["proposal"]["intake_path"] == "startup"
    established = case["described"] + " The product is live. I have no product issues. Help me get more customers."
    assert _classify(established, None)[0] == "gtm"


@cases
@pytest.mark.parametrize("stage", ["no product and no customers", "no product yet", "no working product and no customers"])
def test_no_product_discovery_cannot_become_customer_finding(client, monkeypatch, case, stage):
    from app.db.models import Job
    from app.db.supymem_models import EntryChatProposal
    from app.entry.chat_start import _classify
    from app.intake import application, smart
    from app.state import db

    opening = case["vague"] + " I have " + stage + "."
    assert _classify(opening, None)[0] == "startup"

    async def interpret(text, **kwargs):
        intake = smart.classify_fallback(text).to_dict()
        return {**intake, "path": "gtm", "confidence": 0.95,
                "founder_line": "Let's find your next customers.", "button_label": "Get customers",
                "prefill_answers": {"problem": opening}}

    monkeypatch.setattr(application, "interpret", interpret)
    text = opening
    for answer in (None, case["answers"][0], "Do not find customers for this idea."):
        if answer:
            text += "\nContext: " + answer
        result = preview(client, text)
        details = result["proposal"]
        assert details["intake_path"] == details["route"] == "startup", result
        assert details.get("onboarding_field") != "context"
        assert details["founder_stage"]["stage"] == "idea"
        assert opening not in (details.get("onboarding_question") or "")
        assert client.get("/ventures").json()["items"] == []
        with db()() as session:
            assert session.query(Job).count() == 0
            intake = json.loads(session.get(EntryChatProposal, details["id"]).receipt_json)["intake"]
            assert intake["prefill_answers"].get("problem") != opening
            assert intake["extracted"]["idea"]

    picked = preview(client, text + "\nContext: " + case["idea"])["proposal"]
    assert picked["route"] == picked["intake_path"] == "startup"
    assert picked["next_step"] == "idea" and picked["skipped_steps"] == []
    assert picked["founder_stage"]["stage"] == "idea"
    assert "customers" not in picked["message"]
    assert not picked.get("onboarding_question")


def create(client, proposal):
    response = client.post("/entry/chat/confirm", json={"proposal_id": proposal["id"], "name": "Working name"})
    assert response.status_code == 200, response.text
    return response.json()["venture_id"]


@pytest.mark.parametrize("background,interest", DISCOVERY_DOMAINS, ids=["b2b", "consumer"])
@pytest.mark.parametrize("opening", DISCOVERY_OPENINGS)
def test_background_only_openings_start_discovery(client, background, interest, opening):
    from app.db.supymem_models import EntryChatProposal
    from app.state import db

    text = opening.format(background=background, interest=interest)
    result = preview(client, text)
    assert result["status"] == "clarify", result
    assert result["onboarding_field"] == "context", result
    assert result["onboarding_question"] and result["onboarding_choices"]
    assert not result.get("venture_name")
    assert client.get("/ventures").json()["items"] == []
    with db()() as session:
        assert session.get(EntryChatProposal, result["id"]).text == text


@pytest.mark.parametrize("background,interest", DISCOVERY_DOMAINS, ids=["b2b", "consumer"])
def test_small_talk_uncertainty_does_not_start_model_work(client, monkeypatch, background, interest):
    from app.intake import smart

    calls = []

    async def model(source, *_args, **_kwargs):
        calls.append(source)
        return smart.classify_fallback(source).to_dict()

    monkeypatch.setattr(smart, "_call_model", model)
    monkeypatch.setenv("LLM_OFFLINE", "0")
    text = "Hello\nContext: I don't know"
    result = preview(client, text)
    assert result["onboarding_question"] == smart.DISCOVERY_QUESTION
    assert calls == []
    text += f"\nContext: I work in {background}. I'm interested in {interest}."
    preview(client, text)
    assert calls == [text]


@pytest.mark.parametrize("background,interest", DISCOVERY_DOMAINS, ids=["b2b", "consumer"])
def test_supplied_background_is_not_asked_again(client, monkeypatch, background, interest):
    from app.intake import smart

    text = f"I work in {background}, but I don't know what to build."
    first = preview(client, text)
    assert first["onboarding_question"] == smart.DISCOVERY_FOLLOWUP
    assert first["onboarding_choices"] == smart.DISCOVERY_OFFER_SHAPES

    async def repeat(*_args, **_kwargs):
        return _no_idea_step(smart.DISCOVERY_QUESTION)

    monkeypatch.setattr(smart, "_call_model", repeat)
    repeated = preview(client, text)
    assert repeated["onboarding_question"] == smart.DISCOVERY_FOLLOWUP
    assert repeated["founder_stage"]["stage"] == "exploring"
    text += "\nContext: I'm interested in " + interest
    next_step = preview(client, text)
    assert next_step["onboarding_question"] != repeated["onboarding_question"]
    uncertain = preview(client, text + "\nContext: I don't know")
    assert uncertain["onboarding_question"] != next_step["onboarding_question"]
    assert len(uncertain["onboarding_choices"]) == 3
    assert client.get("/ventures").json()["items"] == []


@pytest.mark.parametrize("background,interest", DISCOVERY_DOMAINS, ids=["b2b", "consumer"])
def test_greeting_keeps_the_welcome_and_offers_a_real_next_step(client, background, interest):
    welcome = preview(client, "Hello")
    assert welcome["onboarding_question"] == welcome["question"], welcome
    assert len(welcome["onboarding_choices"]) == 3
    assert welcome["choices"] == []
    background_reply = preview(client, "Hello\nContext: I work in " + background + ". I'm interested in " + interest + ".")
    assert background_reply["status"] == "clarify" and background_reply["onboarding_field"] == "context"
    assert background_reply["founder_stage"]["stage"] == "exploring"
    assert client.get("/ventures").json()["items"] == []


@pytest.mark.parametrize("background,interest,customer,offers,name", DISCOVERY_CASES, ids=["b2b", "consumer"])
def test_greeting_can_use_the_first_idea_choice(client, background, interest, customer, offers, name):
    first = preview(client, "Hello")
    assert first["status"] == "clarify"
    chosen = "Assumed: " + offers[0] + " for " + customer
    text = "Hello\nContext: " + chosen
    result = preview(client, text)
    assert result["status"] == "proposal", result
    assert not result["proposal"].get("onboarding_question")
    assert result["proposal"]["founder_stage"]["stage"] == "idea"


@pytest.mark.parametrize("background,interest,customer,offers,name", DISCOVERY_CASES, ids=["b2b", "consumer"])
def test_discovery_needs_a_chosen_idea_not_more_background(client, monkeypatch, background, interest, customer, offers, name,
                                                       *, opening="I have no idea yet."):
    from app.db.models import Job, Venture
    from app.intake import smart
    from app.state import db

    text = opening + "\nContext: I work in " + background + "."
    background_step = preview(client, text)
    assert background_step["status"] == "clarify" and background_step["onboarding_field"] == "context"
    text += "\nContext: I'm interested in " + interest + "."
    result = preview(client, text)
    assert result["status"] == "clarify", result
    assert result["onboarding_field"] == "context", result
    assert result["founder_stage"]["stage"] == "exploring"
    assert result["onboarding_question"] != background_step["onboarding_question"]
    text += "\nContext: I don't know"
    uncertain = preview(client, text)
    assert uncertain["status"] == "clarify", uncertain
    assert uncertain["onboarding_question"] != result["onboarding_question"]
    with db()() as session:
        assert session.query(Job).count() == session.query(Venture).count() == 0

    async def decide(source, *_args, **_kwargs):
        decision = smart.classify_fallback(source).to_dict()
        if source.endswith("Assumed: " + offers[0] + " for " + customer):
            decision["next_step"] = {"situation": "clear_idea", "action": "continue"}
        else:
            decision["next_step"] = {"situation": "no_idea", "action": "discover", "ideas": [
                {"title": offer, "offer": offer, "customer": customer,
                 "problem": "An untested possibility", "first_test": "Ask a potential user about the problem"}
                for offer in offers]}
        return decision

    monkeypatch.setattr(smart, "_call_model", decide)
    text += "\nContext: " + uncertain["onboarding_choices"][0]
    options = preview(client, text)
    assert "untested" in options["onboarding_question"]
    chosen = "Assumed: " + options["onboarding_choices"][0]
    picked = preview(client, text + "\nContext: " + chosen)
    assert picked["status"] == "proposal", picked
    assert picked["proposal"]["founder_stage"]["stage"] == "idea"
    assert not picked["proposal"].get("onboarding_question")
    created = client.post("/entry/chat/confirm", json={"proposal_id": picked["proposal"]["id"], "name": name})
    assert created.status_code == 200, created.text
    with db()() as session:
        venture = session.get(Venture, created.json()["venture_id"])
        assert venture.name == name and chosen in venture.raw_idea
        assert venture.raw_idea == text + "\nContext: " + chosen
        kinds = [row.kind for row in session.query(Job).filter(Job.venture_id == venture.id)]
        assert sorted(kinds) in [["idea.clarify_turn"], ["idea.clarify_turn", "idea.page_refresh"]]
    history = client.get("/entry/chat/history").json()["items"]
    assert any(background in item["text"] and chosen in item["text"] for item in history)


@pytest.mark.parametrize("background,interest,customer,offers,name", DISCOVERY_CASES, ids=["b2b", "consumer"])
def test_discovery_background_cannot_replace_the_chosen_audience(client, background, interest, customer, offers, name):
    from app.clarify.dilemmas import venture_context
    from app.idea.agent import ASSUMED, answer_metadata
    from app.entry.chat_start import _onboarding_progress
    from app.intake.smart import _prefill, classify_fallback
    from app.ventures.founder_facts import literal_facts

    chosen = ASSUMED + offers[0] + " for " + customer
    text = "Hello\nContext: I work in " + background + ".\nContext: I'm interested in " + interest + ".\nContext: " + chosen
    assert _prefill(text) == {}
    metadata = answer_metadata(text)
    assert metadata["suggested_text"] == chosen.removeprefix(ASSUMED)
    assert background in metadata["founder_text"] and chosen not in metadata["founder_text"]
    facts = literal_facts(text)
    assert facts == {}, facts
    assert venture_context({"original_idea": chosen, "understanding": {"summary": chosen}, "founder_facts": facts})[0] == customer
    assert venture_context({"original_idea": offers[0], "founder_idea": text, "founder_facts": facts})[0] == customer
    progress = _onboarding_progress(classify_fallback(text).to_dict(), text)
    assert progress["completed"] == ["offer", "customer"]
    assert progress["assumptions"] == [chosen]


@pytest.mark.parametrize("background,interest,customer,offers,name", DISCOVERY_CASES, ids=["b2b", "consumer"])
def test_small_talk_then_no_idea_keeps_discovery_and_the_chosen_direction(client, monkeypatch, background, interest, customer, offers, name):
    from app.intake.smart import DISCOVERY_QUESTION

    preview(client, "Hello, how are you?")
    uncertain = preview(client, "Hello, how are you?\nContext: I don't know")
    assert uncertain["status"] == "clarify" and uncertain["onboarding_field"] == "context"
    assert uncertain["onboarding_question"] == DISCOVERY_QUESTION
    test_discovery_needs_a_chosen_idea_not_more_background(client, monkeypatch, background, interest, customer, offers, name,
        opening="Hello, how are you?\nContext: I don't know")


@pytest.mark.parametrize("background,interest", DISCOVERY_DOMAINS, ids=["b2b", "consumer"])
def test_default_discovery_examples_stay_in_startup_scope(client, background, interest):
    scope = re.compile(r"\b(?:enterprise|healthcare|health|fintech|hardware|deep tech|space|security|developer|climate|product|technology)\b", re.I)
    for text in ("Hello", f"I have no idea yet. My background is {background}. I'm interested in {interest}.",
                 f"I have no idea yet.\nContext: I work in {background}."):
        result = preview(client, text)
        assert result["status"] == "clarify", result
        assert len(result["onboarding_choices"]) == 3
        assert all(scope.search(choice) for choice in result["onboarding_choices"]), result


@cases
def test_discovery_answers_advance_to_a_venture(client, case):
    from app.intake.smart import _DISCOVERY_STEPS

    first = preview(client, case["vague"])["proposal"]
    assert first.get("onboarding_question") not in _DISCOVERY_STEPS
    assert first.get("onboarding_field") != "context"
    text = case["vague"] + "\nContext: " + case["answers"][0]
    second = preview(client, text)["proposal"]
    assert second.get("onboarding_field") != "context"
    assert second.get("onboarding_question") not in _DISCOVERY_STEPS
    third = preview(client, text + "\nContext: " + case["answers"][1])["proposal"]
    assert third["founder_stage"]["stage"] == "idea"
    chosen = preview(client, text + "\nContext: " + case["answers"][1] + "\nContext: " + case["idea"])
    assert chosen["status"] == "proposal" and not chosen["proposal"].get("onboarding_question")
    assert case["answers"][1] in create_idea(client, chosen["proposal"])


def create_idea(client, proposal):
    from app.db.models import Venture
    from app.state import db

    with db()() as session:
        return session.get(Venture, create(client, proposal)).raw_idea


@cases
def test_repeating_a_problem_keeps_the_stated_idea(client, case):
    text = case["vague"] + ("\nContext: " + case["answers"][0]) * 2
    result = preview(client, text)
    assert result["status"] == "proposal" and result["proposal"]["founder_stage"]["stage"] == "idea"
    assert result["proposal"].get("onboarding_field") != "context"
    assert client.get("/ventures").json()["items"] == []
    assert preview(client, text + "\nContext: " + case["idea"])["status"] == "proposal"


@cases
def test_continue_with_what_i_shared_continues(client, case):
    text = case["vague"] + "\nContext: " + case["answers"][0] + "\nContext: Skip this question"
    result = preview(client, text)
    assert result["status"] == "proposal" and result["proposal"].get("onboarding_field") != "context"
    chosen = preview(client, text + "\nContext: " + case["idea"])
    assert chosen["status"] == "proposal"
    result = preview(client, case["vague"] + "\nContext: Skip this question")
    assert result["status"] == "proposal" and result["proposal"].get("onboarding_field") != "context"


def test_skipping_with_nothing_shared_asks_something_else(client):
    from app.intake.smart import DISCOVERY_QUESTION

    result = preview(client, "I have no idea yet\nContext: Skip this question")
    assert result["status"] == "clarify" and result["onboarding_question"] != DISCOVERY_QUESTION
    assert result["onboarding_choices"]


def _no_idea_step(question):
    return {"path": "clarify", "confidence": 0.9, "extracted": {"idea": "", "language": "en"}, "founder_line": question,
            "button_label": "Continue", "next_step": {"situation": "no_idea", "action": "ask", "question": question,
                "field": "context", "examples": ["First grounded example", "Second grounded example"]}}


@cases
def test_the_model_decides_the_next_discovery_question(client, case):
    from app.entry.chat_start import _onboarding_progress

    question = "Which part of that have you seen most often?"
    text = case["vague"] + "\nContext: " + case["answers"][0]
    progress = _onboarding_progress(_no_idea_step(question), text)
    assert progress["question"] == question and progress["choices"][0] == "First grounded example"


@cases
@pytest.mark.parametrize("answers,question", [(1, None), (2, "Tell me more about that?")])
def test_a_model_cannot_hold_a_founder_in_discovery(client, case, answers, question):
    from app.intake import smart

    question = question or smart.DISCOVERY_QUESTION
    text = case["vague"] + "".join("\nContext: " + answer for answer in case["answers"][:answers])
    if answers == 2:
        text += "\nContext: " + case["idea"]
    fallback = smart.classify_fallback(text)
    result = smart._validated_result(_no_idea_step(question), text, smart.PageContext(), fallback)
    assert result.founder_line == fallback.founder_line and result.next_step is None


@cases
def test_a_described_opening_needs_no_more_questions(client, case):
    from app.intake.naming import title_from_idea
    from app.intake.smart import _prefill, classify_fallback

    assert title_from_idea(case["described"])
    assert classify_fallback(case["described"]).extracted.idea
    assert case["mechanism"] in _prefill(case["described"])["offer"]
    assert "you" not in _prefill(case["described"])["customer"].casefold().split()
    result = preview(client, case["described"])
    assert result["status"] == "proposal" and not result["proposal"].get("onboarding_question")
    assert "doc" not in result["proposal"]["message"]


@pytest.mark.parametrize("opening", ["A dispatch board", "A recipe journal"])
def test_a_skip_marker_is_never_the_idea(client, opening):
    from app.intake.smart import classify_fallback

    assert classify_fallback(opening + "\nClarification: Skip this question").extracted.idea == opening
    assert "Skip" not in classify_fallback(opening + " for regional hauliers that plans each route\nClarification: Skip this question").extracted.idea


@cases
def test_a_vague_later_clarification_does_not_reopen_a_described_offer(client, case):
    from app.intake.smart import _prefill

    text = case["described"] + "\nClarification: A simple tool"
    assert case["mechanism"] in _prefill(text)["offer"]


@cases
def test_a_stale_question_click_gives_a_next_step(client, case):
    venture_id = create(client, preview(client, case["described"])["proposal"])
    reply = client.post(f"/ventures/{venture_id}/chat", json={
        "message": "An old choice", "reply_to_question": True, "question_id": "ict_old"})
    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert "already answered" in body["body_md"]
    assert [action["action"] for action in body["actions"]][:1] in (["whats_next"], ["idea.review"], ["chat.reply"])


@cases
def test_a_skip_shows_what_the_founder_chose(client, case):
    from app.chat.store import list_messages

    proposal = preview(client, case["described"] + "\nAudience: Skip this question")["proposal"]
    messages = [message["body_md"] for message in list_messages(create(client, proposal)) if message["role"] == "user"]
    assert "I'm not sure, continue with what I shared" in messages
    assert not any("Skip this question" in message for message in messages)


CONCRETE_ANSWERS = {
    "fintech": ("I ran treasury operations at a regional lender and I don't have an idea yet.",
                "Reconciling cash positions across banks takes community lenders all morning.",
                "I want to build a web app for treasury analysts at community banks. It would match daily balances across accounts and flag gaps before noon.",
                "treasury analysts"),
    "hardware": ("I spent a decade servicing wind farms and I don't know what to build.",
                 "Technicians climb towers just to find out whether a bearing is failing.",
                 "The idea is a handheld vibration sensor for wind turbine technicians that flags a failing bearing before anyone climbs.",
                 "wind turbine technicians"),
}
concrete = pytest.mark.parametrize("opening,problem,offer,who", CONCRETE_ANSWERS.values(), ids=CONCRETE_ANSWERS.keys())


@concrete
def test_a_concrete_discovery_answer_moves_on_to_naming(client, opening, problem, offer, who):
    text = opening + "\nContext: " + problem
    problem_step = preview(client, text)["proposal"]
    assert problem_step["intake_path"] == "startup" and problem_step.get("onboarding_field") != "context"
    result = preview(client, text + "\nContext: " + offer)
    assert result["status"] == "proposal", result
    assert not result["proposal"].get("onboarding_question"), result
    assert result["proposal"]["intake_path"] == preview(client, offer)["proposal"]["intake_path"]
    assert who in create_idea(client, result["proposal"])


@concrete
@pytest.mark.parametrize("decision", [{"situation": "clear_idea", "action": "continue"},
                                      {"situation": "no_idea", "action": "ask", "question": "Which part matters most?",
                                       "field": "context", "examples": ["First example", "Second example"]},
                                      {"situation": "no_idea", "action": "discover", "ideas": [
                                          {"title": title, "offer": title, "customer": "Operations teams",
                                           "problem": "An untested possibility", "first_test": "Ask a potential user"}
                                          for title in ("First option", "Second option", "Third option")]}], ids=["continue", "ask", "discover"])
def test_the_model_cannot_reopen_discovery_after_a_concrete_answer(client, monkeypatch, opening, problem, offer, who, decision):
    from app.intake import smart

    async def model(source, *_args, **_kwargs):
        return {**smart.classify_fallback(source).to_dict(), "next_step": decision}

    monkeypatch.setattr(smart, "_call_model", model)
    monkeypatch.setenv("LLM_OFFLINE", "0")
    result = preview(client, opening + "\nContext: " + problem + "\nContext: " + offer)
    assert result["status"] == "proposal" and not result["proposal"].get("onboarding_question"), result


@concrete
def test_no_discovery_question_is_asked_twice(client, opening, problem, offer, who):
    text, asked = opening, []
    for answer in ("My background is " + who, "I'm interested in " + who, "I'm not sure yet"):
        result = preview(client, text)
        assert result["status"] == "clarify", result
        asked.append(result["onboarding_question"])
        text += "\nContext: " + answer
    assert len(set(asked)) == len(asked), asked
    assert preview(client, text)["status"] == "clarify"
    assert preview(client, text + "\nContext: " + offer)["status"] == "proposal"


@concrete
@pytest.mark.parametrize("chip", ["Help me choose a problem", "I'll describe a problem", "Suggest startup ideas"])
def test_picking_a_discovery_choice_never_repeats_the_question(client, opening, problem, offer, who, chip):
    text = opening + "\nContext: My background is " + who + "\nContext: I'm interested in " + who
    question = preview(client, text)["onboarding_question"]
    result = preview(client, text + "\nContext: " + chip)
    assert result["status"] == "clarify" and result["onboarding_question"] != question, result
    assert client.get("/ventures").json()["items"] == []


@pytest.mark.parametrize("answer", [
    "There isn't a tool for treasury analysts that handles this today.",
    "I ran the product for wind turbine technicians at my last job.",
    "A product for a problem you have experienced",
])
def test_describing_the_past_or_a_gap_is_not_choosing_an_idea(client, answer):
    assert preview(client, "I have no idea yet.\nContext: " + answer)["status"] == "clarify"

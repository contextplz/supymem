"""The saved classifier decision drives the existing entry transaction end to end."""
from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import func, select

from tests.integration.test_entry_chat_api import _dashboard_next_step, client as client  # noqa: F401


def decision(path="gtm", *, url="https://bookflow.example.com", prefill=None):
    from app.intake.smart import IntakeResult
    return IntakeResult(path=path, confidence=0.95,
        extracted={"idea": "Booking software for rehearsal studios", "audience": "Studio managers",
                   "stage": "live" if url else "idea", "existing_site_url": url},
        prefill_answers=prefill or {}, founder_line="Let's find your next customers." if path == "gtm" else "I've read your brief.",
        button_label="Get customers" if path == "gtm" else "Continue").to_dict()


def stub(monkeypatch, result):
    calls = []
    async def interpret(text, **kwargs):
        calls.append((text, kwargs))
        return result
    monkeypatch.setattr("app.intake.application.interpret", interpret)
    monkeypatch.setattr("app.intake.assets._read_site", lambda url: {"status": "verified", "audit": {
        "headline": "Synthetic booking product", "cta": "present", "contact": "present", "analytics": "unverified",
        "fetch_ms": 1, "mobile": "unverified", "lighthouse": "unverified", "recommendations": []}})
    return calls


def preview(client, text="Our live booking software is at https://bookflow.example.com", **kwargs):
    response = client.post("/entry/chat/preview", json={"text": text, **kwargs})
    assert response.status_code == 200, response.text
    return response.json()


def confirm(client, proposal):
    response = client.post("/entry/chat/confirm", json={"proposal_id": proposal["id"]})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("text", [
    "A tool for structural engineers to catch errors in construction drawings before tender",
    "I want to build a tool for structural engineers to catch errors in construction drawings before tender",
    "Software for accountants to reconcile bank statements",
])
def test_explicit_offer_and_audience_preview_has_a_continue_action(client, monkeypatch, text):
    from app.intake.smart import classify_fallback
    stub(monkeypatch, classify_fallback(text).to_dict())
    result = preview(client, text)
    assert result["status"] == "proposal"
    assert not result["proposal"].get("onboarding_question")
    assert result["proposal"]["button_label"] == "Continue"
    assert result["proposal"]["next_step"]


@pytest.mark.parametrize("audience,task", [
    ("Independent bakery owners", "organize delivery shifts"),
    ("Weekend hikers", "plan unfamiliar routes"),
])
def test_customer_capitalisation_keeps_answered_audience(client, monkeypatch, audience, task):
    from app.intake.smart import classify_fallback

    text = f"Software to {task}.\nCustomer: {audience}."
    intake = classify_fallback(text)
    assert intake.prefill_answers["customer"] == audience
    assert not intake.extracted.has_customers
    stub(monkeypatch, intake.to_dict())
    result = preview(client, text)
    assert result["status"] == "proposal"
    assert not result["proposal"].get("onboarding_question")
    assert result["proposal"]["next_step"]


def test_website_default_corrections_sources_and_missing_question(client, monkeypatch):
    from tests.unit.intake.test_website import HTML, URL
    from app.intake.smart import classify_fallback
    from app.db.models import Venture
    from app.db.supymem_models import IdeaSession, IdeaRevision
    from app.state import db

    intake = classify_fallback(URL, page_context={"pages": [{"url": URL, "content": HTML}]}).to_dict()
    stub(monkeypatch, intake)
    proposal = preview(client, URL)["proposal"]
    assert proposal["venture_name"] == "Slotwise"
    from app.entry.chat_start import _stated_name
    assert _stated_name(f"Website: {URL}") == proposal["venture_name"]
    assert proposal["website_answers"]["customer"] == "independent tutors"
    result = client.post("/entry/chat/confirm", json={"proposal_id": proposal["id"],
        "website_answers": {"customer": "studio managers"}})
    assert result.status_code == 200, result.text
    vid = result.json()["venture_id"]
    with db()() as session:
        assert session.get(Venture, vid).name == "Slotwise"
        idea = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == vid))
        revision = session.get(IdeaRevision, idea.current_revision_id)
        structured = json.loads(revision.structured)
        assert structured["customer"] == "studio managers"
        assert "customer" not in structured["_website_answers"]
        assert structured["_onboarding_completed"] == ["offer", "customer"]
        assert structured["_website_answers"]["offer"] == {
            "answer": "Lesson bookings for independent tutors.", "source": "From your website", "status": "answered"}
        assert structured["pricing_hypothesis"] == intake["website_answers"]["pricing_hypothesis"]
    saved = client.get(f"/ventures/{vid}/idea/sessions/{idea.id}").json()
    assert saved["current_revision"]["structured"]["offer"] == "Lesson bookings for independent tutors."
    from app.idea.service import start_direction_correction
    corrected = start_direction_correction(idea.id, "know", venture_id=vid, revision_id=saved["current_revision"]["id"])
    assert "_website_answers" not in corrected["current_revision"]["structured"]
    assert corrected["current_revision"]["structured"]["offer"] == ""
    assert client.get(f"/ventures/{vid}/idea/sessions/{idea.id}").json()["current_revision"]["structured"]["offer"] == "Lesson bookings for independent tutors."


@pytest.mark.parametrize("model", [False, True])
def test_website_prefilled_clarifier_only_asks_missing_field(client, monkeypatch, model):
    from tests.unit.intake.test_website import HTML, URL
    from app.intake.smart import classify_fallback
    from app.idea.service import open_session
    from app.db.models import Venture
    from app.state import db
    from tests.security.test_durable_journey_boundary import run_queued

    intake = classify_fallback(URL, page_context={"pages": [{"url": URL, "content": HTML}]}).to_dict()
    response = client.post("/ventures", json={"name": "Slotwise", "start": False})
    assert response.status_code == 201, response.text
    with db()() as session:
        venture = session.get(Venture, response.json()["id"])
        session.expunge(venture)
    monkeypatch.setattr("app.idea.service._chat_entry_deferred", lambda *args: True)
    if model:
        from tests.idea_fixtures import completion, install_provider
        def reply(messages, kwargs):
            snapshot = json.loads(messages[-1]["content"])
            understanding = snapshot["understanding"]
            if any("studio managers" in turn["text"] for turn in snapshot["conversation"]):
                understanding["customer"] = "studio managers"
            response = completion(proposal=False, understanding=understanding)
            body = json.loads(response.choices[0].message.content)
            body["question"]["field"] = "customer"
            body["question"]["text"] = "Who is it for?"
            response.choices[0].message.content = json.dumps(body)
            return response
        install_provider(monkeypatch, reply)
    opened = open_session(venture=venture, prefill_answers=intake["prefill_answers"], website_answers=intake["website_answers"],
        intake_idea=intake["extracted"]["idea"], intake_text="Lesson bookings from my website")
    run_queued("idea.clarify_turn")
    saved = client.get(f"/ventures/{venture.id}/idea/sessions/{opened['id']}").json()
    assert "Who is it for?" not in saved["turns"][-1]["text"]
    assert saved["current_revision"]["structured"]["customer"] == "independent tutors"
    assert saved["current_revision"]["structured"]["pricing_hypothesis"] == intake["website_answers"]["pricing_hypothesis"]
    if not model:
        assert saved["state"] == "reviewing" and saved["turns"][-1]["kind"] == "proposal"
    if model:
        from app.idea.service import post_message
        post_message(opened["id"], "Actually our audience is studio managers")
        run_queued("idea.clarify_turn")
        corrected = client.get(f"/ventures/{venture.id}/idea/sessions/{opened['id']}").json()
        assert corrected["current_revision"]["structured"]["customer"] == "studio managers"
        assert "customer" not in corrected["current_revision"]["structured"]["_website_answers"]
        post_message(opened["id"], "I want to pivot to a new idea for dentists")
        pivoted = client.get(f"/ventures/{venture.id}/idea/sessions/{opened['id']}").json()
        assert "_website_answers" not in pivoted["current_revision"]["structured"]
        assert "_onboarding_completed" not in pivoted["current_revision"]["structured"]
        assert pivoted["current_revision"]["structured"]["offer"] == ""


def test_website_correction_boundary_rejects_new_fields(client, monkeypatch):
    from tests.unit.intake.test_website import HTML, URL
    from app.intake.smart import classify_fallback
    stub(monkeypatch, classify_fallback(URL, page_context={"pages": [{"url": URL, "content": HTML}]}).to_dict())
    proposal = preview(client, URL)["proposal"]
    response = client.post("/entry/chat/confirm", json={"proposal_id": proposal["id"],
        "website_answers": {"traction": "1000 customers"}})
    assert response.status_code == 422
    assert client.get(f"/entry/chat/proposals/{proposal['id']}").json()["proposal"]["state"] == "pending"


@pytest.mark.parametrize("path,key", [("startup", "research.start"), ("gtm", "research.start"), ("research", "research.start")])
def test_dashboard_calls_founder_guidance_for_canonical_intake_path(client, monkeypatch, path, key):
    from app.db.models import Job
    from app.db.supymem_models import IdeaSession
    from app.state import db
    stub(monkeypatch, decision(path, url="https://bookflow.example.com" if path == "gtm" else None,
        prefill={"customer": "Studio managers", "offer": "Booking software"}))
    text = "Our live booking product is at https://bookflow.example.com" if path == "gtm" else (
        "Only research booking software for rehearsal studios" if path == "research" else
        "Build a startup offering booking software for rehearsal studios")
    receipt = confirm(client, preview(client, text)["proposal"])
    with db()() as session:
        idea = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == receipt["venture_id"]))
        if idea:
            idea.state, idea.confirmed_revision_id = "confirmed", idea.current_revision_id
        for job in session.scalars(select(Job).where(Job.venture_id == receipt["venture_id"])):
            job.status = "succeeded"
        session.commit()
    response = client.get(f"/ventures/{receipt['venture_id']}/dashboard")
    assert response.status_code == 200, response.text
    assert response.json()["next_step"]["key"] == key


@pytest.mark.parametrize("path", ["gtm", "landing_gtm"])
def test_existing_site_is_recorded_and_never_offers_build(client, monkeypatch, path):
    from app.chat.recovery import external_site
    from app.db.models import Venture
    from app.state import db
    from app.workspaces.service import get_workspace
    calls = stub(monkeypatch, decision(path))
    proposal = preview(client, request_id="existing-site")["proposal"]
    assert proposal["route"] == "gtm" and proposal["intake_path"] == path
    assert proposal["receipt"] is None
    assert preview(client, request_id="existing-site")["proposal"]["id"] == proposal["id"]
    assert len(calls) == 1
    receipt = confirm(client, proposal)
    vid = receipt["venture_id"]
    assert external_site(vid)["url"] == "https://bookflow.example.com"
    assert not external_site(vid).get("verified", False)
    assert _dashboard_next_step(client, vid)["key"] == "research.running"
    from app.intake.assets import get_site
    assert get_site(vid)["site"]["status"] == "verified"
    assert get_workspace(receipt["workspace_id"]).settings["intake"]["path"] == path
    assert confirm(client, proposal) == receipt
    with db()() as session:
        assert session.scalar(select(func.count()).select_from(Venture)) == 1


def test_research_to_startup_reroute_uses_current_direction_and_forks_review(client, monkeypatch):
    from app.db.models import Job, Venture
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.state import db
    answers = {"customer": "Studio managers", "offer": "Booking software"}
    stub(monkeypatch, decision("research", url=None, prefill=answers))
    first = confirm(client, preview(client, "Only research booking software for rehearsal studios")["proposal"])
    with db()() as session:
        old = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == first["venture_id"]))
        old_id, confirmed_revision = old.id, old.confirmed_revision_id
    stub(monkeypatch, decision("startup", url=None, prefill=answers))
    direction = "Change path to a startup and build this business after research"
    changed = preview(client, direction, target_venture_id=first["venture_id"], use_original=True)["proposal"]
    receipt = confirm(client, changed)
    with db()() as session:
        old = session.get(IdeaSession, old_id)
        assert old.state == "confirmed" and old.current_revision_id == confirmed_revision
        current = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == receipt["venture_id"], IdeaSession.id != old_id))
        assert current.state == "clarifying"
        assert session.scalar(select(ClarifierTurn.text).where(ClarifierTurn.session_id == current.id,
            ClarifierTurn.role == "founder")) == direction
        assert sorted(session.scalars(select(Job.kind))) == ["idea.clarify_turn", "research.deep"]
    with db()() as session:
        assert session.scalar(select(func.count()).select_from(Venture)) == 1


def test_full_doc_and_prefill_are_saved_before_bounded_first_question(client, monkeypatch):
    from app.db.models import Job, Venture, VentureDocument
    from app.db.supymem_models import IdeaClarificationRun, IdeaRevision, IdeaSession
    from app.state import db
    prefill = {"problem": "Bookings get lost in messages", "customer": "Independent studio managers",
               "offer": "Shared booking board", "workflow": "Collect requests and approve slots"}
    original = "# Product brief\n" + "Historical booking observations.\n" * 5000 + "\nCustomer: Independent studio managers"
    stub(monkeypatch, decision("startup", url=None, prefill=prefill))
    proposal = preview(client, original, request_id="full-doc")["proposal"]
    receipt = confirm(client, proposal)
    with db()() as session:
        venture = session.get(Venture, receipt["venture_id"])
        assert venture.raw_idea == original
        assert session.get(VentureDocument, f"intake_source:{proposal['id']}").body_md == original
        idea = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == venture.id))
        revision = session.get(IdeaRevision, idea.current_revision_id)
        structured = json.loads(revision.structured)
        assert all(structured[k] == v for k, v in prefill.items())
        job = session.scalar(select(Job).where(Job.venture_id == venture.id, Job.kind == "idea.clarify_turn"))
        payload = json.loads(job.payload_json)
        assert payload["revision_id"] == revision.id
        assert len(payload["text"]) <= 12000
    # Reload/open does not enqueue another first question from the full source.
    client.post(f"/ventures/{receipt['venture_id']}/idea/sessions")
    with db()() as session:
        assert session.scalar(select(func.count()).select_from(Job).where(Job.venture_id == receipt["venture_id"])) == 1
    from tests.security.test_durable_journey_boundary import run_queued
    job_id = run_queued("idea.clarify_turn")
    with db()() as session:
        job = session.get(Job, job_id)
        assert job.status == "succeeded", job.last_error
        run = session.scalar(select(IdeaClarificationRun).where(IdeaClarificationRun.job_id == job_id))
        assert run.state == "completed" and run.error_code is None
        assert len(json.loads(run.snapshot)["original_idea"]) <= 12000
        assert session.get(Venture, receipt["venture_id"]).raw_idea == original


def test_research_button_confirms_saved_brief_and_queues_only_research(client, monkeypatch):
    from app.db.models import Job
    from app.db.supymem_models import IdeaSession, ValidationRun
    from app.state import db
    stub(monkeypatch, decision("research", url=None, prefill={"customer": "Studio managers", "offer": "Booking software"}))
    proposal = preview(client, "Only research a booking startup for rehearsal studio managers")["proposal"]
    with db()() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 0
    receipt = confirm(client, proposal)
    with db()() as session:
        idea = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == receipt["venture_id"]))
        assert idea.state == "confirmed"
        assert session.scalar(select(func.count()).select_from(ValidationRun)) == 0
        assert list(session.scalars(select(Job.kind).where(Job.venture_id == receipt["venture_id"]))) == ["research.deep"]
    assert confirm(client, proposal) == receipt
    reloaded = client.post(f"/ventures/{receipt['venture_id']}/idea/sessions")
    assert reloaded.status_code == 201, reloaded.text
    assert reloaded.json()["initial_job_id"] is None and reloaded.json()["initialization_error"] is None


@pytest.mark.parametrize("path", ["clarify", "multiple_ideas", "refuse"])
def test_uncertain_or_refused_inputs_never_create_a_venture(client, monkeypatch, path):
    from app.db.models import Venture
    from app.state import db
    result = decision(path, url=None)
    result["confidence"] = 0.5
    result["choices"] = [] if path == "refuse" else ["New idea", "Existing product"]
    stub(monkeypatch, result)
    result = preview(client, "Please help me pick the right path")
    assert result["status"] == "clarify"
    assert client.post("/entry/chat/confirm", json={"proposal_id": result["id"]}).status_code == 409
    with db()() as session:
        assert session.scalar(select(func.count()).select_from(Venture)) == 0


def test_owned_reroute_reuses_whole_source_without_another_venture(client, monkeypatch):
    from app.db.models import Venture, VentureDocument
    from app.state import db
    original = "I want to build a booking startup for independent rehearsal studios"
    stub(monkeypatch, decision("startup", url=None))
    first = preview(client, original)["proposal"]
    receipt = confirm(client, first)
    calls = stub(monkeypatch, decision("gtm", url=None))
    changed = preview(client, "Change path to GTM", target_venture_id=receipt["venture_id"], use_original=True)["proposal"]
    assert calls[0][1]["original_input"] == original
    rerouted = confirm(client, changed)
    assert rerouted["venture_id"] == receipt["venture_id"]
    with db()() as session:
        assert session.scalar(select(func.count()).select_from(Venture)) == 1
        assert session.get(Venture, receipt["venture_id"]).raw_idea == original
        assert session.get(VentureDocument, f"intake_source:{changed['id']}").body_md == original


def test_simultaneous_same_request_classifies_once_and_replays(client, monkeypatch):
    from app.authz.workspace import ensure_subject_person
    from app.entry.chat_start import preview_async
    calls = []
    async def slow(text, **kwargs):
        calls.append(text)
        await asyncio.sleep(0.01)
        return decision()
    monkeypatch.setattr("app.intake.application.interpret", slow)
    actor = ensure_subject_person(None)
    async def run():
        kwargs = {"text": "Our live booking software is at https://bookflow.example.com", "actor_id": actor, "request_id": "simultaneous"}
        return await asyncio.gather(preview_async(**kwargs), preview_async(**kwargs))
    responses = asyncio.run(run())
    assert len(calls) == 1 and responses[0] == responses[1]


def test_database_claim_blocks_another_worker_before_paid_interpretation(client, monkeypatch):
    from app.authz.workspace import ensure_subject_person
    from app.entry.chat_start import ChatStartError, _preview_async
    calls = []
    async def slow(text, **kwargs):
        calls.append(text)
        await asyncio.sleep(0.02)
        return decision()
    monkeypatch.setattr("app.intake.application.interpret", slow)
    actor = ensure_subject_person(None)
    kwargs = {"text": "Our live booking software is at https://bookflow.example.com", "actor_id": actor,
              "request_id": "two-workers", "target_venture_id": None, "use_original": False}
    async def run():
        results = await asyncio.gather(_preview_async(**kwargs), _preview_async(**kwargs), return_exceptions=True)
        return results, await _preview_async(**kwargs)
    results, resumed = asyncio.run(run())
    busy = next(r for r in results if isinstance(r, ChatStartError))
    success = next(r for r in results if isinstance(r, dict))
    assert busy.code == "proposal_busy" and len(calls) == 1
    assert resumed == success


def test_stale_classifier_claim_recovers_without_another_paid_call(client, monkeypatch):
    from datetime import timedelta
    from app.authz.workspace import ensure_subject_person
    from app.db.supymem_models import EntryChatProposal
    from app.state import db
    from core.clock import utcnow
    actor = ensure_subject_person(None)
    now = utcnow() - timedelta(seconds=30)
    text = "I want to build a startup for independent rehearsal studios"
    with db()() as session:
        session.add(EntryChatProposal(id="ecp_stale_intake", actor_id=actor, request_id="stale-intake", text=text,
            route="clarify", target_venture_id=None, skipped_steps_json="[]", message="Reading your request.",
            next_step="clarify", state="classifying", receipt_json="{}", created_at=now,
            expires_at=now + timedelta(hours=24), updated_at=now))
        session.commit()
    async def forbidden(*args, **kwargs):
        pytest.fail("Stale paid classification must not be redialled")
    monkeypatch.setattr("app.intake.application.interpret", forbidden)
    result = preview(client, text, request_id="stale-intake")
    assert result["proposal"]["id"] == "ecp_stale_intake"
    assert result["proposal"]["state"] == "pending"


def test_stale_targeted_classifier_claim_keeps_venture_when_retry_omits_target(client, monkeypatch):
    from datetime import timedelta
    from app.authz.workspace import ensure_subject_person
    from app.db.models import Venture
    from app.db.supymem_models import EntryChatProposal
    from app.state import db
    from core.clock import utcnow

    stub(monkeypatch, decision())
    first = confirm(client, preview(client)["proposal"])
    second = confirm(client, preview(client)["proposal"])
    now = utcnow() - timedelta(seconds=30)
    text = "Change path to GTM"
    actor = ensure_subject_person(None)
    with db()() as session:
        session.add(EntryChatProposal(id="ecp_stale_targeted", actor_id=actor, request_id="stale-targeted", text=text,
            route="clarify", target_venture_id=first["venture_id"], skipped_steps_json="[]", message="Reading your request.",
            next_step="clarify", state="classifying", receipt_json=json.dumps({
                "original_source": "Our live booking software is at https://bookflow.example.com",
                "created_new": False}), created_at=now, expires_at=now + timedelta(hours=24), updated_at=now))
        session.commit()

    async def forbidden(*args, **kwargs):
        pytest.fail("Stale targeted classification must not spend again")
    monkeypatch.setattr("app.intake.application.interpret", forbidden)
    changed = client.post("/entry/chat/preview", json={"text": text, "request_id": "stale-targeted",
        "target_venture_id": second["venture_id"]})
    assert changed.status_code == 409
    assert changed.json()["detail"]["error"]["code"] == "request_id_conflict"
    proposal = preview(client, text, request_id="stale-targeted")["proposal"]
    assert proposal["id"] == "ecp_stale_targeted" and proposal["target_venture_id"] == first["venture_id"]
    resumed = confirm(client, proposal)
    assert resumed["venture_id"] == first["venture_id"] and resumed["workspace_id"] == first["workspace_id"]
    assert confirm(client, proposal) == resumed
    with db()() as session:
        assert session.scalar(select(func.count()).select_from(Venture)) == 2
        assert json.loads(session.get(EntryChatProposal, proposal["id"]).receipt_json)["venture_id"] == first["venture_id"]


@pytest.mark.parametrize("unavailable", ["archived", "foreign"])
def test_stale_targeted_classifier_claim_rechecks_access_before_recovery(client, monkeypatch, unavailable):
    from datetime import timedelta
    from sqlalchemy import delete
    from app.authz.workspace import ensure_subject_person
    from app.db.models import Venture, Workspace, WorkspaceMember
    from app.db.supymem_models import EntryChatProposal
    from app.state import db
    from core.clock import utcnow

    stub(monkeypatch, decision())
    first = confirm(client, preview(client)["proposal"])
    actor = ensure_subject_person(None)
    foreign = ensure_subject_person("usr_foreign_stale")
    now = utcnow() - timedelta(seconds=30)
    text = "Change path to GTM"
    with db()() as session:
        session.add(EntryChatProposal(id="ecp_unavailable_target", actor_id=actor, request_id="unavailable-target", text=text,
            route="clarify", target_venture_id=first["venture_id"], skipped_steps_json="[]", message="Reading your request.",
            next_step="clarify", state="classifying", receipt_json=json.dumps({"created_new": False}),
            created_at=now, expires_at=now + timedelta(hours=24), updated_at=now))
        if unavailable == "archived":
            session.get(Venture, first["venture_id"]).archived_at = utcnow()
        else:
            session.get(Workspace, first["workspace_id"]).owner_id = foreign
            session.execute(delete(WorkspaceMember).where(WorkspaceMember.workspace_id == first["workspace_id"],
                WorkspaceMember.person_id == actor))
        session.commit()

    async def forbidden(*args, **kwargs):
        pytest.fail("An inaccessible stale target must not call interpretation")
    monkeypatch.setattr("app.intake.application.interpret", forbidden)
    response = client.post("/entry/chat/preview", json={"text": text, "request_id": "unavailable-target"})
    assert response.status_code == 404
    with db()() as session:
        row = session.get(EntryChatProposal, "ecp_unavailable_target")
        assert row.state == "classifying" and row.target_venture_id == first["venture_id"]
        assert session.scalar(select(func.count()).select_from(Venture)) == 1


def test_slow_free_reader_does_not_extend_preview_deadline(client, monkeypatch):
    import threading
    from app.intake.application import read_page_context
    monkeypatch.setattr("app.chat.link_context._safe_host", lambda _host: True)
    started, released, finished = threading.Event(), threading.Event(), threading.Event()
    def slow(*args, **kwargs):
        started.set()
        released.wait(timeout=2)
        finished.set()
        return []
    monkeypatch.setattr("app.intake.crawl.crawl_site", slow)
    try:
        result = asyncio.run(read_page_context("https://bookflow.example.com", budget_seconds=0.02))
        assert started.is_set() and not finished.is_set()
        assert result["pages"][0]["status"] == "failed"
    finally:
        released.set()


def test_user_chosen_name_is_saved_and_repeated_confirm_cannot_rename(client, monkeypatch):
    from app.db.models import Venture
    from app.state import db
    stub(monkeypatch, decision("startup", url=None))
    proposal = preview(client, "I want to build a startup for independent rehearsal studios")["proposal"]
    response = client.post("/entry/chat/confirm", json={"proposal_id": proposal["id"], "name": "Studio Board"})
    assert response.status_code == 200, response.text
    receipt = response.json()
    with db()() as session:
        assert session.get(Venture, receipt["venture_id"]).name == "Studio Board"
    assert client.post("/entry/chat/confirm", json={"proposal_id": proposal["id"], "name": "Wrong second name"}).json() == receipt


def test_opt_in_name_suggestion_is_cached_without_repeated_spend(client, monkeypatch):
    calls = []
    stub(monkeypatch, decision("startup", url=None))
    async def suggest(idea, audience, **kwargs):
        calls.append((idea, audience))
        return "Studio Board"
    monkeypatch.setattr("app.intake.venture_name.suggest_name", suggest)
    monkeypatch.setattr("app.intake.smart._consumer_account", lambda actor_id: object())
    proposal = preview(client, "I want to build a startup for independent rehearsal studios")["proposal"]
    assert calls == []
    path = f"/entry/chat/proposals/{proposal['id']}/name"
    for _ in range(2):
        response = client.post(path, json={"request_id": "name-once"})
        assert response.status_code == 200 and response.json() == {"name": "Studio Board", "source": "model"}
    assert calls == [("Booking software for rehearsal studios", "Studio managers")]
    saved = client.get(f"/entry/chat/proposals/{proposal['id']}").json()["proposal"]
    assert saved["venture_name"] == "Studio Board" and saved["receipt"] is None
    history = client.get("/entry/chat/history").json()["items"]
    assert history[-1]["proposal"]["venture_name"] == "Studio Board"


@pytest.mark.parametrize("domain", ["gyms", "clinics"])
def test_grounded_names_keep_founder_value_when_extraction_drops_it(client, monkeypatch, domain):
    intake = decision("startup", url=None)
    intake["extracted"].update(idea=f"app for {domain}", audience=domain)
    stub(monkeypatch, intake)
    calls = []
    async def model(*args, **kwargs):
        calls.append(kwargs["request_id"])
        return {"name": "AppNest"}
    monkeypatch.setattr("app.intake.venture_name._metered_json", model)
    proposal = preview(client, f"app for {domain}\nAudience: help {domain} get more members")["proposal"]
    path = f"/entry/chat/proposals/{proposal['id']}/name"
    stem = domain[:-1].capitalize()
    for index, suffix in enumerate(("Grow", "Magnet", "Funnel")):
        request = {"request_id": f"{domain}-name-{index}"}
        response = client.post(path, json=request)
        assert response.status_code == 200, response.text
        assert response.json() == {"name": stem + suffix, "source": "idea_words"}
        assert client.post(path, json=request).json() == response.json()
    assert len(calls) == 3
    saved = client.get(f"/entry/chat/proposals/{proposal['id']}").json()["proposal"]
    assert saved["venture_name"] == stem + "Funnel" and saved["receipt"] is None
    response = client.post("/entry/chat/confirm", json={"proposal_id": proposal["id"], "name": "Founder Choice"})
    assert response.status_code == 200, response.text


def test_unknown_name_outcome_does_not_replay_and_manual_name_still_works(client, monkeypatch):
    calls = []
    stub(monkeypatch, decision("startup", url=None))
    async def unavailable(*args, **kwargs):
        calls.append(True)
        return None
    monkeypatch.setattr("app.intake.venture_name.suggest_name", unavailable)
    monkeypatch.setattr("app.intake.smart._consumer_account", lambda actor_id: object())
    monkeypatch.setattr("app.intake.venture_name._idea_words_name", lambda idea: None)
    proposal = preview(client, "I want to build a startup for independent rehearsal studios")["proposal"]
    path = f"/entry/chat/proposals/{proposal['id']}/name"
    first = client.post(path, json={"request_id": "unknown-name"})
    assert first.status_code == 200 and first.json()["reason"] == "name_unavailable"
    assert first.json()["source"] == "free_text" and first.json()["name"] is None
    assert client.post(path, json={"request_id": "unknown-name"}).json() == first.json()
    assert len(calls) == 1
    response = client.post("/entry/chat/confirm", json={"proposal_id": proposal["id"], "name": "My Studio Tool"})
    assert response.status_code == 200, response.text


def test_any_account_attempts_metered_name_then_gets_free_fallback_when_unavailable(client, monkeypatch):
    calls = []
    stub(monkeypatch, decision("startup", url=None))
    async def model(*args, **kwargs):
        calls.append(True)
        return None
    monkeypatch.setattr("app.intake.venture_name.suggest_name", model)
    monkeypatch.setattr("app.intake.smart._consumer_account", lambda actor_id: None)
    proposal = preview(client, "I want to build a startup for independent rehearsal studios")["proposal"]
    response = client.post(f"/entry/chat/proposals/{proposal['id']}/name", json={"request_id": "free-name"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "idea_words" and body["name"] and body["name"].split()[0].lower() not in {"hi", "hello", "hey", "i"}
    assert calls == [True]


@pytest.mark.parametrize("name", ["  ", "n" * 49])
def test_blank_name_rejected_before_creating_any_venture(client, monkeypatch, name):
    from app.db.models import Venture
    from app.state import db
    stub(monkeypatch, decision("startup", url=None))
    proposal = preview(client, "I want to build a startup for independent rehearsal studios")["proposal"]
    response = client.post("/entry/chat/confirm", json={"proposal_id": proposal["id"], "name": name})
    assert response.status_code == 422
    with db()() as session:
        assert session.scalar(select(func.count()).select_from(Venture)) == 0


@pytest.mark.parametrize("path", ["startup", "gtm"])
@pytest.mark.parametrize("name", ["X", "Test", "ven_custom"])
def test_explicit_short_or_reserved_name_is_preserved(client, monkeypatch, path, name):
    from app.db.models import Venture
    from app.intake import application
    from app.intake.naming import needs_heal
    from app.state import db
    from app.ventures import lifecycle
    stub(monkeypatch, decision(path, url=None if path == "startup" else "https://bookflow.example.com"))
    apply_intake = application.apply
    def observe_before_receipt(venture_id, workspace_id, **kwargs):
        visible = {venture["id"]: venture for venture in lifecycle.list_active(workspace_ids={workspace_id})}
        assert visible[venture_id]["name"] == name
        return apply_intake(venture_id, workspace_id, **kwargs)
    monkeypatch.setattr(application, "apply", observe_before_receipt)
    text = "I want to build a startup for rehearsal studios" if path == "startup" else "Our live booking app is at https://bookflow.example.com and we want customers"
    proposal = preview(client, text)["proposal"]
    response = client.post("/entry/chat/confirm", json={"proposal_id": proposal["id"], "name": name})
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert client.get(f"/ventures/{receipt['venture_id']}").json()["name"] == name
    listed = client.get("/ventures").json()["items"]
    assert next(venture for venture in listed if venture["id"] == receipt["venture_id"])["name"] == name
    with db()() as session:
        assert session.get(Venture, receipt["venture_id"]).name == name
    assert receipt["name"] == name
    assert client.get(f"/entry/chat/proposals/{proposal['id']}").json()["proposal"]["venture_name"] == name
    assert confirm(client, proposal) == receipt
    # A matching venture ID alone does not protect an unrelated generated junk name.
    with db()() as session:
        session.get(Venture, receipt["venture_id"]).name = "ven_unreviewed_junk"
        session.commit()
    healed = client.get(f"/ventures/{receipt['venture_id']}")
    assert healed.status_code == 200 and not needs_heal(healed.json()["name"])


@pytest.mark.parametrize("path", ["startup", "gtm"])
@pytest.mark.parametrize("name", ["Studio Board", "ven_custom"])
def test_duplicate_chosen_name_receipt_uses_saved_unique_name(client, monkeypatch, path, name):
    from app.db.models import Venture
    from app.intake import application
    from app.state import db
    from app.ventures import lifecycle
    stub(monkeypatch, decision(path, url=None if path == "startup" else "https://bookflow.example.com"))
    apply_intake = application.apply
    def observe_before_receipt(venture_id, workspace_id, **kwargs):
        visible = {venture["id"]: venture for venture in lifecycle.list_active(workspace_ids={workspace_id})}
        assert visible[venture_id]["name"] == expected
        return apply_intake(venture_id, workspace_id, **kwargs)
    monkeypatch.setattr(application, "apply", observe_before_receipt)
    text = "I want to build a startup for rehearsal studios" if path == "startup" else "Our live booking app is at https://bookflow.example.com and we want customers"
    for expected in [name, f"{name} 2"]:
        proposal = preview(client, text)["proposal"]
        response = client.post("/entry/chat/confirm", json={"proposal_id": proposal["id"], "name": name})
        assert response.status_code == 200, response.text
        receipt = response.json()
        assert receipt["name"] == expected
        with db()() as session:
            assert session.get(Venture, receipt["venture_id"]).name == expected
        assert client.get(f"/ventures/{receipt['venture_id']}").json()["name"] == expected
        assert confirm(client, proposal) == receipt


def test_scoped_intake_does_not_change_sibling_venture_intent(client, monkeypatch):
    from app.db.models import Venture
    from app.idea.service import _project_intent
    from app.intake.application import apply
    from app.state import db
    from core.clock import utcnow
    stub(monkeypatch, decision("gtm"))
    receipt = confirm(client, preview(client)["proposal"])
    with db()() as session:
        session.add(Venture(id="ven_sibling_intake", workspace_id=receipt["workspace_id"], name="Internal board",
            raw_idea="Internal booking board for my team", stage="draft", autonomy_level="L1", created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    apply("ven_sibling_intake", receipt["workspace_id"], original="Internal booking board for my team",
          decision=decision("personal_build", url=None), proposal_id="ecp_sibling_intake", new_venture=False)
    assert _dashboard_next_step(client, receipt["venture_id"])["key"] == "research.running"
    with db()() as session:
        assert _project_intent(session, receipt["workspace_id"], receipt["venture_id"]) == "startup"
        assert _project_intent(session, receipt["workspace_id"], "ven_sibling_intake") == "personal"


def test_asset_questions_confirm_stage_and_save_the_audit_in_normal_entry(client, monkeypatch):
    from app.db.models import VentureDocument
    from app.intake.assets import get_site
    from app.state import db
    stub(monkeypatch, decision("startup", url=None))
    proposal = preview(client, "Booking software for rehearsal studios")['proposal']
    assert len(proposal["asset_questions"]) == 4
    assert proposal["founder_stage"]["stage"] == "idea"
    response = client.post("/entry/chat/confirm", json={"proposal_id": proposal["id"],
        "assets": {"website": "https://synthetic.example/product", "contacts": "20 interviews", "built": "Working prototype", "tried": "3 demos"},
        "founder_stage": {"stage": "page_live", "confirmed": True}})
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt["founder_stage"]["stage"] == "page_live"
    assert receipt["founder_stage"]["confirmed_by_founder"] is True
    assert get_site(receipt["venture_id"])["site"]["status"] == "verified"
    with db()() as session:
        supplied = session.get(VentureDocument, f"founder_assets:{receipt['venture_id']}")
        assert json.loads(supplied.body_md)["tried"] == "3 demos"
    assert client.post("/entry/chat/confirm", json={"proposal_id": proposal["id"]}).json() == receipt


def test_bad_asset_url_can_be_corrected_before_any_venture_is_created(client, monkeypatch):
    stub(monkeypatch, decision("startup", url=None))
    proposal = preview(client, "Booking software for rehearsal studios")["proposal"]
    bad = client.post("/entry/chat/confirm", json={"proposal_id": proposal["id"], "assets": {"website": "http://169.254.169.254"}})
    assert bad.status_code == 422
    assert client.get("/ventures").json()["items"] == []
    assert client.get(f"/entry/chat/proposals/{proposal['id']}").json()["proposal"]["state"] == "pending"
    assert client.post("/entry/chat/confirm", json={"proposal_id": proposal["id"], "assets": {"website": "https://synthetic.example"}}).status_code == 200


def test_implicit_private_site_is_rejected_before_creation_and_can_be_corrected(client, monkeypatch):
    from app.db.models import VentureDocument
    from app.state import db
    stub(monkeypatch, decision("gtm", url="https://bookflow.example.com?access_token=synthetic"))
    proposal = preview(client)["proposal"]
    rejected = client.post("/entry/chat/confirm", json={"proposal_id": proposal["id"]})
    assert rejected.status_code == 422
    assert client.get("/ventures").json()["items"] == []
    assert client.get(f"/entry/chat/proposals/{proposal['id']}").json()["proposal"]["state"] == "pending"
    corrected = client.post("/entry/chat/confirm", json={"proposal_id": proposal["id"], "assets": {"website": "https://synthetic.example"}})
    assert corrected.status_code == 200, corrected.text
    receipt = corrected.json()
    assert receipt["site"]["url"] == "https://synthetic.example"
    with db()() as session:
        saved = session.get(VentureDocument, f"intake_decision:{receipt['venture_id']}")
        assert json.loads(saved.body_md)["extracted"]["existing_site_url"] is None


def test_reentry_preserves_existing_audit_assets_and_subject_bound_preview(client, monkeypatch):
    from app.db.models import VentureDocument
    from app.intake.application import apply
    from app.state import db
    stub(monkeypatch, decision("gtm"))
    proposal = preview(client, request_id="existing-assets-snapshot")["proposal"]
    assert preview(client, request_id="existing-assets-snapshot")["proposal"] == proposal
    response = client.post("/entry/chat/confirm", json={"proposal_id": proposal["id"],
        "assets": {"contacts": "20 interviews", "built": "Working prototype", "tried": "3 demos"}})
    assert response.status_code == 200, response.text
    receipt = response.json()
    with db()() as session:
        site_before = session.get(VentureDocument, f"external_site:{receipt['venture_id']}").body_md
    apply(receipt["venture_id"], receipt["workspace_id"], original="Continue this booking product",
          decision=decision("gtm"), proposal_id="ecp_asset_reentry", new_venture=False,
          assets={"website": "", "contacts": "", "built": "", "tried": ""})
    with db()() as session:
        assert session.get(VentureDocument, f"external_site:{receipt['venture_id']}").body_md == site_before
        stored = json.loads(session.get(VentureDocument, f"founder_assets:{receipt['venture_id']}").body_md)
        assert stored["contacts"] == "20 interviews" and stored["tried"] == "3 demos"
    stub(monkeypatch, decision("gtm", url=None))
    resumed = preview(client, "Continue GTM for my booking product", target_venture_id=receipt["venture_id"], request_id="target-assets")['proposal']
    assert resumed["asset_prefill"]["website"] == "https://bookflow.example.com"
    assert resumed["asset_prefill"]["contacts"] == "20 interviews"
    assert client.get(f"/entry/chat/proposals/{resumed['id']}").json()["proposal"]["asset_prefill"] == resumed["asset_prefill"]


@pytest.mark.parametrize("path", ["gtm", "landing_gtm", "repo_build"])
def test_existing_assets_confirm_a_research_brief_without_a_validation_verdict(client, monkeypatch, path):
    from app.db.models import Job
    from app.db.supymem_models import IdeaSession, ValidationRun
    from app.state import db
    value = decision(path, url=None if path == "repo_build" else "https://bookflow.example.com")
    if path == "repo_build":
        value["extracted"].update(stage="code", repo_url="https://github.com/acme/booking")
    stub(monkeypatch, value)
    proposal = preview(client, "Our booking software for studios already exists")['proposal']
    assert proposal["route"] == ("startup" if path == "repo_build" else "gtm")
    receipt = confirm(client, proposal)
    with db()() as session:
        idea = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == receipt["venture_id"]))
        assert idea.state == "confirmed" and idea.confirmed_by_person_id
        assert session.scalar(select(func.count()).select_from(ValidationRun)) == 0
        assert list(session.scalars(select(Job.kind).where(Job.venture_id == receipt["venture_id"]))) == ["research.deep"]


def test_unsure_interested_founder_gets_three_directions_without_a_venture(client):
    from app.intake.smart import DISCOVERY_QUESTION
    for interest in ("independent tutoring", "manufacturing quality"):
        result = preview(client, f"I'm not sure what to build. I'm interested in {interest}.")
        assert result["status"] == "clarify" and len(result["onboarding_choices"]) == 3
        assert result["onboarding_question"] != DISCOVERY_QUESTION  # they named an area, so it isn't asked for again
        assert result["founder_stage"]["stage"] == "exploring"
    assert client.get("/ventures").json()["items"] == []


@pytest.mark.parametrize("sibling_path", ["startup", "gtm"])
@pytest.mark.parametrize("provider_online", [True, False])
def test_personal_clarification_worker_keeps_intent_after_sibling_intake(client, monkeypatch, sibling_path, provider_online):
    from app.db.models import ChatMessage, Job, Venture, Workspace
    from app.db.supymem_models import IdeaClarificationRun, IdeaRevision, IdeaSession
    from app.idea.service import fallback_first_question
    from app.state import db
    from app.ventures.run_status import snapshot
    from core.clock import utcnow
    from tests.idea_fixtures import completion, install_provider
    from tests.security.test_durable_journey_boundary import run_queued

    stub(monkeypatch, decision("personal_build", url=None))
    personal = confirm(client, preview(client, "Build a private booking board just for my own use")["proposal"])
    vid, wid = personal["venture_id"], personal["workspace_id"]
    with db()() as session:
        idea = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == vid))
        sid = idea.id
        job_id = session.scalar(select(Job.id).where(Job.venture_id == vid, Job.kind == "idea.clarify_turn"))
        session.add(Venture(id="ven_sibling_startup", workspace_id=wid, name="Booking business",
            raw_idea="Booking software for rehearsal studios", stage="draft", autonomy_level="L1",
            created_at=utcnow(), updated_at=utcnow()))
        session.commit()

    # Confirming the sibling through the entry API overwrites the shared
    # workspace defaults while this personal venture's real worker is pending.
    stub(monkeypatch, decision(sibling_path, url=None))
    sibling = confirm(client, preview(client, "Build a startup offering booking software for rehearsal studios",
        target_venture_id="ven_sibling_startup")["proposal"])
    assert sibling["workspace_id"] == wid
    with db()() as session:
        settings = json.loads(session.get(Workspace, wid).settings_json)
        assert settings["intake_venture_id"] == sibling["venture_id"]
        assert settings["project_intent"] == "startup"
        if sibling_path == "gtm":
            # A sibling's legacy entry can also disable this shared flag;
            # the personal venture's saved intake still owns its chat route.
            settings["chat_entry_defer_clarification"] = False
            session.get(Workspace, wid).settings_json = json.dumps(settings)
            session.commit()

    calls = install_provider(monkeypatch, lambda *args: completion()) if provider_online else []
    assert run_queued("idea.clarify_turn", through_job_id=job_id) == job_id
    with db()() as session:
        idea = session.get(IdeaSession, sid)
        assert session.get(Job, job_id).status == "succeeded"
        assert json.loads(session.get(IdeaRevision, idea.current_revision_id).structured)["project_intent"] == "personal"
        assert not list(session.scalars(select(Job).where(Job.venture_id == vid, Job.kind == "idea.validate")))
        run = session.scalar(select(IdeaClarificationRun).where(IdeaClarificationRun.job_id == job_id))
        assert json.loads(run.snapshot)["project_intent"] == "personal"
        assert idea.state == ("reviewing" if provider_online else "clarifying")
        replies = list(session.scalars(select(ChatMessage).where(
            ChatMessage.venture_id == vid, ChatMessage.workspace_id == wid,
            ChatMessage.kind == "idea_clarification_ready")))
        assert len(replies) == 1

    listed = client.get(f"/ventures/{vid}/idea/sessions")
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"][0]["project_intent"] == "personal"
    if provider_online:
        from app.chat.execute import _confirm_idea_for_research

        assert len(calls) == 1
        assert snapshot(vid, wid)["stage"] == "Project review"

        def unexpected_startup_confirmation(*args, **kwargs):
            pytest.fail("A personal project's research request must not auto-confirm a startup proposal")

        monkeypatch.setattr("app.idea.service.confirm", unexpected_startup_confirmation)
        assert _confirm_idea_for_research(vid, None) is None
    else:
        from app.chat.idea_bridge import pending_question

        saved = client.get(f"/ventures/{vid}/idea/sessions/{sid}").json()
        assert saved["turns"][-1]["text"] == fallback_first_question("personal")
        question_id = saved["turns"][-1]["id"]
        answer = "I track my private studio bookings in a paper notebook"
        assert pending_question(vid, answer) == (sid, question_id)
        sent = client.post(f"/ventures/{vid}/chat", json={"message": answer,
            "reply_to_question": True, "question_id": question_id})
        assert sent.status_code == 200, sent.text
        assert sent.json()["kind"] == "idea_clarification_pending"
        with db()() as session:
            assert session.scalar(select(func.count()).select_from(Job).where(
                Job.venture_id == vid, Job.kind == "idea.clarify_turn")) == 2
            assert not list(session.scalars(select(Job).where(Job.venture_id == vid, Job.kind == "idea.validate")))


def test_reroute_updates_prefill_intent_and_preserves_confirmed_history(client, monkeypatch):
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.state import db
    stub(monkeypatch, decision("research", url=None, prefill={"customer": "Studio managers", "offer": "Booking software"}))
    original = "Only research booking software for rehearsal studio managers"
    first = confirm(client, preview(client, original)["proposal"])
    with db()() as session:
        original_session = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == first["venture_id"]))
        old_id, old_confirmed = original_session.id, original_session.confirmed_revision_id
        old_structured = session.get(IdeaRevision, old_confirmed).structured
    changed_decision = decision("personal_build", url=None, prefill={"customer": "My team", "offer": "Internal booking board"})
    changed_decision["extracted"]["idea"] = "Internal booking board"
    stub(monkeypatch, changed_decision)
    changed = preview(client, "Change path to an internal build for my team", target_venture_id=first["venture_id"], use_original=True)["proposal"]
    rerouted = confirm(client, changed)
    assert rerouted["venture_id"] == first["venture_id"]
    with db()() as session:
        old = session.get(IdeaSession, old_id)
        assert old.state == "confirmed" and old.current_revision_id == old_confirmed
        assert session.get(IdeaRevision, old_confirmed).structured == old_structured
        current = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == first["venture_id"], IdeaSession.id != old_id))
        structured = json.loads(session.get(IdeaRevision, current.current_revision_id).structured)
        assert structured["project_intent"] == "personal" and structured["customer"] == "My team"
        assert structured["summary"] == "Internal booking board"


def test_existing_target_continuation_lease_blocks_duplicates_and_recovers_stale(client, monkeypatch):
    from datetime import timedelta
    from app.db.models import Venture
    from app.db.supymem_models import EntryChatProposal
    from app.state import db
    from core.clock import utcnow
    stub(monkeypatch, decision("gtm"))
    first = confirm(client, preview(client)["proposal"])
    changed = preview(client, "Change path to GTM", target_venture_id=first["venture_id"], use_original=True)["proposal"]
    with db()() as session:
        row = session.get(EntryChatProposal, changed["id"])
        metadata = json.loads(row.receipt_json)
        metadata["commit_owner"] = "another-active-request"
        row.receipt_json = json.dumps(metadata)
        row.state, row.updated_at = "committing", utcnow()
        session.commit()
    assert client.post("/entry/chat/confirm", json={"proposal_id": changed["id"]}).status_code == 409
    with db()() as session:
        row = session.get(EntryChatProposal, changed["id"])
        assert json.loads(row.receipt_json)["commit_owner"] == "another-active-request"
        assert row.updated_at > utcnow().replace(tzinfo=None) - timedelta(seconds=30)
        row.updated_at = utcnow() - timedelta(minutes=3)
        session.commit()
    resumed = confirm(client, changed)
    assert resumed["venture_id"] == first["venture_id"]
    assert confirm(client, changed) == resumed
    with db()() as session:
        assert session.scalar(select(func.count()).select_from(Venture)) == 1


def test_full_account_replays_and_resolves_owned_targets_before_new_quota(client, monkeypatch):
    from app.db.models import Venture
    from app.state import db
    calls = stub(monkeypatch, decision("startup", url=None))
    first = preview(client, "I want to build a startup for rehearsal studios", request_id="full-account-first")["proposal"]
    receipt = confirm(client, first)
    second = preview(client, "I want to build a startup for independent bookshops")["proposal"]
    confirm(client, second)
    assert preview(client, "I want to build a startup for rehearsal studios", request_id="full-account-first")["proposal"]["id"] == first["id"]
    assert len(calls) == 2
    with db()() as session:
        session.get(Venture, receipt["venture_id"]).name = "StudioBoard"
        session.commit()
    existing = preview(client, "GTM for StudioBoard")["proposal"]
    assert existing["target_venture_id"] == receipt["venture_id"]
    assert confirm(client, existing)["venture_id"] == receipt["venture_id"]
    with db()() as session:
        for venture in session.scalars(select(Venture)):
            venture.name = "StudioBoard"
        session.commit()
    ambiguous = preview(client, "GTM for StudioBoard")
    assert ambiguous["status"] == "clarify" and len(calls) == 3


def test_venture_api_scopes_intake_path_and_gtm_goal_to_the_sibling(client, monkeypatch):
    from app.db.models import Venture, VentureDocument
    from app.state import db
    from app.workspaces.service import merge_settings
    from core.clock import utcnow

    stub(monkeypatch, decision("gtm"))
    receipt = confirm(client, preview(client)["proposal"])
    sibling_id = "ven_sibling_api_intake"
    document_id = f"intake_decision:{sibling_id}"
    with db()() as session:
        now = utcnow()
        session.add(Venture(id=sibling_id, workspace_id=receipt["workspace_id"], name="Internal board",
            raw_idea="Internal booking board for my team", stage="draft", autonomy_level="L1", created_at=now, updated_at=now))
        session.add(VentureDocument(id=document_id, venture_id=sibling_id, kind="intake_decision",
            title="Saved intake", body_md=json.dumps(decision("personal_build", url=None)), created_at=now, updated_at=now))
        session.commit()

    owner = client.get(f"/ventures/{receipt['venture_id']}")
    sibling = client.get(f"/ventures/{sibling_id}")
    assert owner.status_code == sibling.status_code == 200
    assert owner.json()["entry_goal"] == "gtm" and owner.json()["intake_path"] == "gtm"
    assert sibling.json()["entry_goal"] is None and sibling.json()["intake_path"] == "personal_build"

    for invalid in ("not JSON", '["research"]', '{"path": ["gtm"]}'):
        with db()() as session:
            session.get(VentureDocument, document_id).body_md = invalid
            session.commit()
        response = client.get(f"/ventures/{sibling_id}")
        assert response.status_code == 200 and response.json()["intake_path"] is None
        assert response.json()["entry_goal"] is None

    # Legacy workspaces without intake ownership keep their existing goal.
    merge_settings(receipt["workspace_id"], {"intake_venture_id": None})
    assert client.get(f"/ventures/{sibling_id}").json()["entry_goal"] == "gtm"

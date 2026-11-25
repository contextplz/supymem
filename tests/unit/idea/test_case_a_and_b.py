"""Idea journeys execute queued real workflows; providers are isolated fixtures."""
import json

import pytest
from sqlalchemy import select

from tests.security.test_connector_oauth_boundary import client as client
from tests.security.test_durable_journey_boundary import IDEA, request, run_queued, venture
from tests.idea_fixtures import completion, install_provider
from tests.research_fixtures import install_research
from tests.security.test_idea_validation_boundary import validation_provider


def test_u10_review_summary_has_labelled_lines(client, monkeypatch):
    from app.db.supymem_models import ValidationRun
    from app.idea.validation import post_outcome_to_chat
    from app.state import db
    from app.chat import milestones
    from core.clock import utcnow
    vid, wid, sid = venture(client)
    revision = request(client, "PATCH", f"/ventures/{vid}/idea/sessions/{sid}/revision", json={"patch": IDEA}).json()["id"]
    captured = []
    monkeypatch.setattr(milestones, "post_milestone", lambda *args, **kwargs: captured.append(kwargs["body_md"]))
    with db()() as session:
        session.add(ValidationRun(id="summary-test", session_id=sid, revision_id=revision, workspace_id=wid, venture_id=vid,
            method="gstack_office_hours", state="complete", verdict="needs_evidence", created_at=utcnow(),
            report=json.dumps({"strengths": ["Specific audience"], "biggest_risk": "No conversations", "proposed_tests": [{"hypothesis": "Parents will take a call"}], "gaps": ["Willingness to pay", "No scoped GBrain memory was provided; no memory-based synthesis was performed."]})))
        session.flush()
        post_outcome_to_chat(session, "summary-job", sid, revision, ok=True)
    assert len(captured) == 1
    for label in ("What we found", "Your opening", "What I'd do next"):
        assert f"**{label}**" in captured[0]
    assert "Willingness to pay" not in captured[0] and "Biggest risk" not in captured[0]
    assert not any(word in captured[0].lower() for word in ("gbrain", "gstack", "scoped memory", "synthesis"))


@pytest.mark.parametrize("domain,brand,offer,audience,problem", [
    ("contextkeep.example", "ContextKeep", "Persistent agent memory for AI developers.", "AI developers", "Agents lose context between sessions"),
    ("weekendpages.example", "BookNest", "Reading lists for book club members.", "book club members", "Reading lists get lost in messages"),
])
def test_d13_first_website_message_prefills_product_and_brand(client, monkeypatch, domain, brand, offer, audience, problem):
    import asyncio
    from app.chat import link_context
    from app.intake import application, smart
    from app.ventures.startup_review import problem_prefill

    text = f"We already have a website: https://{domain}. Help me find our first customers."
    fetched = []
    def fetch(url, timeout):
        fetched.append(url)
        return f'<meta property="og:site_name" content="{brand}"><meta name="description" content="{offer}"><h1>{offer}</h1><p>Problem: {problem}</p><a>Get started</a>'
    def crawl(url, **kwargs):
        from app.intake.website import extract_website
        website = extract_website(fetch(url, 3), url)
        return [{"url": url, "text": website["content"], "venture_name": website["name"], "website_answers": website["answers"]}]
    async def classify(text, **kwargs):
        assert fetched and set(fetched) == {f"https://{domain}"}
        context = smart.PageContext.model_validate(kwargs["page_context"])
        fallback = smart.classify_fallback(text, page_context=context)
        model = fallback.to_dict()
        model["extracted"]["idea"] = text
        model["prefill_answers"]["customer"] = "Not sure"
        return smart._validated_result(model, text, context, fallback)
    monkeypatch.setattr(link_context.socket, "getaddrinfo", lambda *args: [(0, 0, 0, "", ("93.184.216.34", 0))])
    monkeypatch.setattr("app.intake.crawl.crawl_site", crawl)
    monkeypatch.setattr(smart, "classify", classify)
    preview = request(client, "POST", "/entry/chat/preview", json={"text": text, "request_id": "website-first"})
    assert preview.status_code == 200, preview.text
    assert preview.json()["status"] == "proposal", preview.text
    proposal = preview.json()["proposal"]
    assert proposal["route"] == "gtm"
    assert "Who is it for" not in proposal["message"]
    assert proposal["website_answers"] == {"offer": offer, "customer": audience, "problem": problem}
    named = request(client, "POST", f"/entry/chat/proposals/{proposal['id']}/name", json={"request_id": "site-brand"})
    assert named.status_code == 200, named.text
    assert named.json() == {"name": brand, "source": "website"}
    intake = asyncio.run(application.interpret(text + "\nAudience: Not sure", actor_id="usr_alice", budget_seconds=0.1))
    assert intake["prefill_answers"]["customer"] == audience
    assert intake["extracted"]["idea"] == offer
    from app.db.models import Venture
    from app.idea.service import open_session
    from app.state import db
    from app.ventures import startup_review
    vid, _, _ = venture(client)
    with db()() as session:
        saved_venture = session.get(Venture, vid)
        saved_venture.raw_idea = text
        session.commit()
        session.refresh(saved_venture)
        session.expunge(saved_venture)
    opened = open_session(venture=saved_venture, intake_text=text, intake_idea=intake["extracted"]["idea"],
        prefill_answers=intake["prefill_answers"], website_answers=intake["website_answers"], research_only=True)
    structured = opened["current_revision"]["structured"]
    assert structured["problem"] == problem
    assert structured["customer"] == audience
    with db()() as session:
        review = startup_review.review(session, session.get(Venture, vid))
        assert review["checklist"]["problem"]["value"] == problem
        assert not review["question"] or review["question"]["field"] not in {"making", "audience", "problem"}
    assert problem_prefill(text) == problem_prefill("Help me find our first customers.", stated=True) == ""
    assert problem_prefill(f"Help me find customers. {problem}.") == problem + "."
    assert smart._canonical_url("https://app.supymem.com") is None
    assert smart._canonical_url("https://supymem.com") is None
    assert smart._canonical_url("https://supymem.com/auth/consume?token=secret") is None
    assert not link_context._safe_host("supymem.com")
    monkeypatch.setattr(link_context.socket, "getaddrinfo", lambda *args: [(0, 0, 0, "", ("10.0.0.1", 0))])
    assert not link_context._safe_host(domain)


@pytest.mark.parametrize("via_chat", [False, True])
def test_validated_idea_can_confirm_without_research_or_charge(client, via_chat):
    from core.clock import utcnow
    from app.db.models import CreditAccount, CreditLedgerEntry, CreditReservation, Job
    from app.db.supymem_models import IdeaSession, ValidationRun
    from app.idea.validation import METHODS, post_outcome_to_chat
    from app.state import db

    vid, wid, sid = venture(client)
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    revision = request(client, "PATCH", base + "/revision", json={"patch": IDEA}).json()["id"]
    with db()() as session:
        idea = session.get(IdeaSession, sid)
        idea.state, idea.case = "validating", "explore"
        for method in METHODS:
            session.add(ValidationRun(id=f"{sid}:{method}", session_id=sid, revision_id=revision,
                workspace_id=wid, venture_id=vid, method=method, state="complete",
                verdict="needs_evidence", report="{}", created_at=utcnow()))
        session.commit()
        before_jobs = list(session.scalars(select(Job.id)).all())
        before_ledger = list(session.scalars(select(CreditLedgerEntry.id)).all())
        before_reservations = list(session.scalars(select(CreditReservation.id)).all())
        before_balance = [(account.id, account.spent_micro, account.reserved_micro)
                          for account in session.scalars(select(CreditAccount))]
        post_outcome_to_chat(session, "validated-job", sid, revision, ok=True)
        session.commit()
    confirmation = {"revision_id": revision, "start_research": False, "accept_validation": True}
    assert request(client, "POST", base + "/confirm", json={**confirmation, "revision_id": "stale"}).status_code in {404, 409}
    assert request(client, "POST", base + "/confirm", json={**confirmation, "start_research": "false"}).status_code == 422
    other_vid, _, _ = venture(client)
    assert request(client, "POST", f"/ventures/{other_vid}/idea/sessions/{sid}/confirm", json=confirmation).status_code == 404
    with db()() as session:
        session.get(ValidationRun, f"{sid}:{METHODS[0]}").state = "pending"
        session.commit()
    incomplete = request(client, "POST", base + "/confirm", json=confirmation)
    assert incomplete.status_code == 409 and incomplete.json()["detail"]["error"]["code"] == "validation_required"
    with db()() as session:
        assert session.get(IdeaSession, sid).state == "validating"
        assert all(run.founder_decision is None for run in session.scalars(
            select(ValidationRun).where(ValidationRun.session_id == sid)))
        session.get(ValidationRun, f"{sid}:{METHODS[0]}").state = "complete"
        session.commit()
        before_jobs = list(session.scalars(select(Job.id)).all())
        before_balance = [(account.id, account.spent_micro, account.reserved_micro)
                          for account in session.scalars(select(CreditAccount))]
    if via_chat:
        from app.chat.store import list_messages
        cards = list_messages(vid)
        card = next(item for item in cards if item.get("kind") == "idea_validation_ready")
        action = next(item for item in card["actions"] if item["label"] == "Build my landing page now")
        assert action["style"] == "secondary"
        assert card["actions"][0]["label"] == "Continue to research"
        result = request(client, "POST", f"/ventures/{vid}/chat/confirm", json={
            "action": action["action"], "payload": action["payload"]})
        assert result.status_code == 200, result.text
        # Chat-first: Website is a hidden page, so the next step is an in-chat build action, not a link there.
        follow = result.json()["actions"][0]
        assert "href" not in follow and follow["label"] == "Build my landing page now"
        assert follow["action"] in {"pagebuilder.build", "build.prepare"}
    else:
        result = request(client, "POST", base + "/confirm", json=confirmation)
        assert result.status_code == 200, result.text
        assert result.json()["dossier_job_id"] is None
    with db()() as session:
        idea = session.get(IdeaSession, sid)
        assert idea.state == "confirmed" and idea.confirmed_revision_id == revision
        assert all(run.founder_decision == "accept" for run in session.scalars(
            select(ValidationRun).where(ValidationRun.session_id == sid)))
        assert list(session.scalars(select(Job.id)).all()) == before_jobs
        assert list(session.scalars(select(CreditLedgerEntry.id)).all()) == before_ledger
        assert list(session.scalars(select(CreditReservation.id)).all()) == before_reservations
        assert [(account.id, account.spent_micro, account.reserved_micro)
                for account in session.scalars(select(CreditAccount))] == before_balance


def test_empty_idea_creates_venture(client):
    result = request(client, "POST", "/ventures", json={"start": False})
    assert result.status_code == 201, result.text
    short = request(client, "POST", "/ventures", json={"raw_idea": "too short", "start": False})
    assert short.status_code == 422


@pytest.mark.parametrize("via_chat", [False, True])
def test_regular_clarification_preserves_assumed_pick(client, monkeypatch, via_chat):
    install_provider(monkeypatch)
    vid, wid, sid = venture(client)
    if via_chat:
        from app.db.models import Workspace
        from app.state import db
        with db()() as session:
            workspace = session.get(Workspace, wid)
            workspace.settings_json = json.dumps({**json.loads(workspace.settings_json or "{}"),
                                                  "chat_entry_defer_clarification": True, "entry_goal": "startup"})
            session.commit()
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    request(client, "POST", base + "/messages", json={"text": "An order intake board"})
    run_queued("idea.clarify_turn")
    question = request(client, "GET", base).json()["turns"][-1]
    assert question["kind"] == "question"
    answer = "Assumed: Bakery owner\nI have not spoken to one yet"
    if via_chat:
        assert request(client, "POST", f"/ventures/{vid}/chat/confirm", json={"action": "idea.review"}).status_code == 200
        result = request(client, "POST", f"/ventures/{vid}/chat", json={
            "message": answer, "question_id": question["id"], "reply_to_question": True})
    else:
        result = request(client, "POST", base + "/messages", json={"text": answer})
    assert result.status_code == 200, result.text
    assert request(client, "GET", base).json()["turns"][-1]["text"] == answer


def test_case_a_ask_propose_edit_confirm(client, monkeypatch):
    calls = install_provider(monkeypatch)
    vid, _, sid = venture(client)
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    request(client, "PATCH", base + "/revision", json={"patch": IDEA})
    request(client, "POST", base + "/propose")
    run_queued("idea.clarify_turn")
    patched = request(client, "PATCH", base + "/revision", json={"patch": {"pricing_hypothesis": "₹499 per month after the waitlist"}})
    assert patched.status_code == 200 and "499" in patched.json()["structured"]["pricing_hypothesis"]
    assert request(client, "POST", base + "/confirm", json={}).status_code == 409
    confirmed = request(client, "POST", base + "/confirm", json={"revision_id": patched.json()["id"]})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["session"]["state"] == "confirmed"
    assert len(calls) == 1, "Manual edits must not require a second paid model rewrite"
    install_research(monkeypatch)
    run_queued("research.deep")
    assert request(client, "GET", f"/ventures/{vid}/research/dossiers").json()["items"]


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_fresh_personal_confirmation_does_not_enqueue_market_research(client, monkeypatch, provider):
    from app.db.models import Job, Workspace, CreditReservation
    from app.db.supymem_models import FounderApp, IdeaRevision, IdeaSession
    from app.state import db

    vid, wid, sid = venture(client)
    with db()() as session:
        workspace = session.get(Workspace, wid)
        workspace.settings_json = json.dumps({**json.loads(workspace.settings_json or "{}"),
                                              "project_intent": "personal"})
        session.commit()
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    install_provider(monkeypatch)
    assert request(client, "PATCH", base + "/revision", json={"patch": IDEA}).status_code == 200
    assert request(client, "POST", base + "/propose").status_code == 200
    run_queued("idea.clarify_turn")
    revised = request(client, "PATCH", base + "/revision", json={"patch": {
        **IDEA, "pricing_hypothesis": "none; private use"}})
    assert revised.status_code == 200, revised.text
    # This fixture creates a startup revision before switching its workspace;
    # fresh personal onboarding records the intent in both places.
    with db()() as session:
        revision = session.get(IdeaRevision, revised.json()["id"])
        revision.structured = json.dumps({**json.loads(revision.structured), "project_intent": "personal"})
        session.commit()
    confirmed = request(client, "POST", base + "/confirm", json={"revision_id": revised.json()["id"]})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["session"]["state"] == "confirmed"
    assert confirmed.json()["dossier_job_id"] is None
    with db()() as session:
        assert session.scalar(select(Job.id).where(Job.venture_id == vid, Job.kind == "research.deep")) is None
        from app.build.device_orders import _personal_build_snapshot
        snapshot = _personal_build_snapshot(session, wid, vid)
        assert snapshot["revision_id"] == revised.json()["id"]
        assert snapshot["kind"] == "personal_build"
    from app.admin.authn import CallerContext
    from app.build import device_orders
    monkeypatch.setattr(device_orders, "_scope", lambda *_args: (wid, "usr_alice", "dev_personal"))
    monkeypatch.setattr(device_orders, "authorize", lambda *_args, **_kwargs: None)
    caller = CallerContext("usr_alice", "", "user", "/test", "POST", 9999999999,
                           "test", device_id="dev_personal")
    # Confirmation remains useful; an untyped personal idea does not prove an
    # offline framework/runtime profile or authorize a misleading build card.
    with pytest.raises(ValueError, match="^device_coding_offline_profile_unavailable$"):
        device_orders.prepare_coding_task_from_chat(
            vid, caller, f"Use {provider.title()} to build my private bookmark organizer")
    from app.db.models import VentureDocument
    with db()() as session:
        assert not session.scalar(select(FounderApp.id).where(FounderApp.venture_id == vid))
        assert not session.scalar(select(CreditReservation.id).where(
            CreditReservation.workspace_id == wid, CreditReservation.purpose == "device.work_order"))
        assert not session.scalar(select(VentureDocument.id).where(
            VentureDocument.venture_id == vid, VentureDocument.kind == device_orders.KIND))
        idea = session.get(IdeaSession, sid)
        assert idea.state == "confirmed" and idea.confirmed_revision_id == revised.json()["id"]
        assert device_orders._personal_build_snapshot(session, wid, vid)["revision_id"] == revised.json()["id"]
        idea.state = "reviewing"
        session.commit()
    with db()() as session, pytest.raises(ValueError, match="device_personal_build_unavailable"):
        device_orders._personal_build_snapshot(session, wid, vid)


def test_case_b_candidates_validate_accept(client, monkeypatch):
    options = [{"title": "Bakery order board", "problem": "Missing orders", "customer": "Bakers", "offer": "Order intake", "first_test": "Interview one baker"}]
    calls = install_provider(monkeypatch, lambda *args: completion(case="explore", understanding={"proposed_options": options}))
    vid, _, sid = venture(client)
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    request(client, "POST", base + "/case", json={"case": "explore"})
    candidate = request(client, "POST", base + "/messages", json={"text": "Suggest up to three tailored, unvalidated product directions for me to review."})
    assert candidate.status_code == 200 and candidate.json()["job_id"]
    run_queued("idea.clarify_turn")
    state = request(client, "GET", base).json()
    assert len(calls) == 1 and state["current_revision"]["structured"]["proposed_options"] == options
    validation_provider(monkeypatch)
    validated = request(client, "POST", base + "/validate", json={})
    assert validated.status_code == 200 and validated.json()["job_id"]
    run_queued("idea.validate")  # the GStack question stage: the test provider cannot word them, so the standard ones are asked
    assert request(client, "POST", base + "/messages", json={"text": "Skip — run the review with what you have"}).json()["job_id"]
    run_queued("idea.validate")
    runs = request(client, "GET", base).json()["validation_runs"]
    assert runs and all(run["verdict"] == "needs_evidence" for run in runs)
    decided = request(client, "POST", base + f"/validations/{runs[0]['id']}/decide", json={"decision": "accept"})
    assert decided.status_code == 200 and decided.json()["state"] == "reviewing"

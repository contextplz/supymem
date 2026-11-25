"""Scripted founder journeys: source-backed slots, not a transcript guessing game."""
import json
import os

import pytest

from tests.company_os_harness import append_idea_revision
from tests.unit.gtm.test_journey import journey_db
from tests.unit.gtm.test_known import known_case
from tests.integration.test_postgres_auth_security import pg_database
from tests.integration.test_postgres_journey_jobs import journey_db as postgres_journey_db


@pytest.mark.parametrize("audience,role,offer", [
    ("purchasing managers at independent hospitals", "operator", "A tool that reconciles purchase orders"),
    ("parents planning meals for children with allergies", "parent", "An app that builds family shopping lists"),
])
@pytest.mark.parametrize("extraction", ["literal", "model"])
def test_corrected_customer_survives_incidental_role_and_idea_confirmation(request, audience, role, offer, extraction):
    data = request.getfixturevalue("postgres_journey_db" if os.environ.get("TEST_POSTGRES_URL") else "journey_db")
    from app.credits.migration import migrate_wallets
    from app.credits.policy import wallet_phase
    from app.db.models import Venture
    from app.db.session import serialize_writes
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.idea import agent, runner, service
    from app.tenancy import bind
    from app.ventures import founder_facts as facts, startup_review
    from core.clock import utcnow
    from core.ids import new_id

    data = {**data, "sid": new_id("ids")}
    with data["factory"]() as session:
        migrate_wallets(session)
        session.commit()
    assert wallet_phase() == "person"
    answer = f"The product imports the records and tells the {role} what to review before approving a recommendation."
    with bind(workspace_id=data["wid"], venture_id=data["vid"], actor=data["actor"]):
        with data["factory"]() as session:
            serialize_writes(session, key=f"startup_review:{data['vid']}")
            venture = session.get(Venture, data["vid"])
            venture.raw_idea = offer
            startup_review._document(session, venture.id, "internal_new_user_flow", {"enabled": True})
            now, revision_id = utcnow(), new_id("idv")
            idea = IdeaSession(id=data["sid"], workspace_id=data["wid"], venture_id=data["vid"], case="know",
                state="reviewing", current_revision_id=revision_id, created_at=now, updated_at=now)
            session.add(idea)
            session.flush()
            session.add(IdeaRevision(id=revision_id, session_id=idea.id, workspace_id=data["wid"], venture_id=data["vid"],
                rev=1, source="founder", structured=json.dumps({"offer": offer, "customer": audience}), created_at=now))
            session.flush()
            facts.capture(session, venture, "Audience: " + audience, source="clarification", field="customer")
            assert facts.read(session, venture)["audience"]["value"] == audience
            facts.capture(session, venture, answer, source="clarification", field="workflow")
            if extraction == "model":
                raw = dict.fromkeys(facts.FIELDS)
                raw["audience"] = {**facts._fact("the " + role, "clarification"), "quote": answer}
                facts.save(session, venture, facts.validated_extraction(raw, answer, "clarification", "workflow"))
            snapshot = {"original_idea": offer, "project_intent": "startup",
                "understanding": {"offer": offer, "customer": audience},
                "founder_facts": facts.read(session, venture),
                "conversation": [{"role": "founder", "field": "workflow", "text": answer}]}
            result = runner._core_clarification(snapshot, agent.Clarification(case="know", action="proposal",
                message="Review your idea.", understanding=agent.Understanding(offer=offer, customer=audience)))
            revision = service._append_revision(session, idea, result.understanding.model_dump(), source="agent")
            revision_id = revision.id
            session.commit()
        service.confirm(data["sid"], revision_id=revision_id, person_id=data["actor"], start_research=False)
        saved = service.get_session(data["sid"])
        assert saved["state"] == "confirmed"
        assert saved["current_revision"]["structured"]["customer"] == audience
        with data["factory"]() as session:
            idea = session.get(IdeaSession, data["sid"])
            assert idea.confirmed_revision_id == revision_id
            assert json.loads(session.get(IdeaRevision, revision_id).structured)["customer"] == audience
        assert facts.literal_facts("Audience: the " + role, field="customer")["audience"]["value"] == "the " + role


@pytest.mark.parametrize("audience,job", [
    ("Freight coordinators", "reconcile shipment charges"),
    ("Parents planning dinners", "plan weekly family menus"),
])
@pytest.mark.parametrize("source", ["review", "founder message", "website"])
def test_latest_saved_answers_replace_older_facts_but_not_later_review(journey_db, audience, job, source):
    from datetime import timedelta
    from app.db.models import Venture, VentureDocument
    from app.db.session import serialize_writes
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.gstack.office_hours import saved_snapshot
    from app.ventures import founder_facts as facts, startup_review
    from core.clock import utcnow

    now = utcnow()
    with journey_db["factory"]() as session:
        serialize_writes(session, key=f"startup_review:{journey_db['vid']}")
        venture = session.get(Venture, journey_db["vid"])
        venture.raw_idea = "A tool for museum curators to catalogue artifacts"
        startup_review._document(session, venture.id, startup_review.INTERNAL_KIND,
            {"founder_facts": {"audience": facts._fact("Museum curators", source),
                               "problem": facts._fact("catalogue artifacts", source)}})
        session.add(IdeaSession(id="newest_session", workspace_id=venture.workspace_id, venture_id=venture.id,
            case="know", state="clarifying", current_revision_id="newest_revision", created_at=now, updated_at=now))
        session.flush()
        session.add(IdeaRevision(id="older_answer_revision", session_id="newest_session", workspace_id=venture.workspace_id,
            venture_id=venture.id, rev=1, source="founder", created_at=now - timedelta(seconds=3),
            structured=json.dumps({"_answered_fields": {"problem": "catalogue artifacts"}})))
        revision = IdeaRevision(id="newest_revision", session_id="newest_session", workspace_id=venture.workspace_id,
            venture_id=venture.id, rev=2, source="founder", created_at=now - timedelta(seconds=1),
            structured=json.dumps({"customer": audience, "workflow": job,
                "_answered_fields": {"customer": audience, "workflow": job, "problem": "catalogue artifacts"}}))
        session.add(revision)
        session.flush()
        document = session.get(VentureDocument, f"{startup_review.INTERNAL_KIND}:{venture.id}")
        document.updated_at = now + timedelta(seconds=2)
        observed = facts.read(session, venture)
        assert observed["audience"]["value"] == audience
        assert observed["audience"]["source"] == "clarification"
        snapshot = saved_snapshot(session, venture.id, revision.id)
        assert "Museum curators" not in snapshot["idea"]
        from app.clarify.dilemmas import venture_context
        assert venture_context(snapshot) == (audience, job)
        imported = facts._fact("catalogue artifacts", "website", 1)
        imported["quote"] = "The page says they catalogue artifacts."
        facts.save(session, venture, {"problem": imported})
        assert facts.read(session, venture)["problem"]["source"] == "clarification"
        assert venture_context(saved_snapshot(session, venture.id, revision.id)) == (audience, job)
        copied = IdeaRevision(id="copied_revision", session_id="newest_session", workspace_id=venture.workspace_id,
            venture_id=venture.id, rev=3, source="agent", created_at=now + timedelta(hours=1), structured=revision.structured)
        session.add(copied)
        session.get(IdeaSession, "newest_session").current_revision_id = copied.id
        session.flush()
        facts.save(session, venture, {"audience": facts._fact("Museum visitors", "review")})
        assert facts.read(session, venture)["audience"]["value"] == "Museum visitors"
        facts.save(session, venture, {"problem": facts._fact("finding accessible exhibition routes", "review")})
        assert venture_context(saved_snapshot(session, venture.id, revision.id)) == (
            "Museum visitors", "finding accessible exhibition routes")
        facts.save(session, venture, {"audience": None, "problem": None})
        assert venture_context(saved_snapshot(session, venture.id, revision.id)) == ("the people you want to help", job)


def test_journey_questions_share_known_policy_without_approving_outreach(known_case, monkeypatch):
    from app.core.local_mode import is_local
    from app.connections import context_docs
    from app.credits.policy import set_policy
    from app.db.models import Venture
    from app.gtm import journey
    from app.ventures import founder_facts, startup_review

    data = known_case
    view = journey._icp(data["vid"])
    assert view["known_checks"]["audience"]["status"] == "known" and view["who"] == data["audience"]
    assert not view.get("chat_confirmed")
    with data["factory"]() as session:
        startup_review._document(session, data["vid"], startup_review.INTERNAL_KIND,
            {"founder_facts": {"audience": founder_facts._fact(data["audience"], "website", .8)}})
        session.commit()
    assert journey._icp(data["vid"])["known_checks"]["audience"]["status"] == "confirm"
    if is_local():
        from secrets import token_urlsafe
        from app.core.local_secrets import update_settings
        update_settings("composio", {}, {"api_key": token_urlsafe(24)})
        update_settings("capabilities", {"connected_apps": True}, {})
    monkeypatch.setattr(context_docs, "research_context", lambda *_args: {"connected_context": {"documents": [
        {"title": "Your selected plan", "excerpt": "Audience: Museum visitors"}]}})
    view = journey._icp(data["vid"])
    assert view["known_checks"]["audience"]["status"] == "conflict" and not view["who"]
    journey.update_icp(data["wid"], data["vid"], data["audience"], problem="Your customers struggle to organize visits.", start=False)
    assert journey._icp(data["vid"])["known_checks"]["audience"]["status"] == "known"
    set_policy("feature_flags", {"known_check_v1": False}, "test")
    view = journey._icp(data["vid"])
    if is_local():
        assert view["known_checks"]["audience"]["status"] == "known"
        assert not view.get("chat_confirmed")
    else:
        assert "known_checks" not in view


@pytest.mark.parametrize("audience,workaround", [
    ("Small warehouse teams", "Bin cards and emailed spreadsheets"),
    ("Shift-working households", "Recipe clippings and grocery notes"),
    ("Independent mobile appliance technicians in Canada", "Appointment sheets and social inboxes"),
])
def test_option_filters_distinguish_assertions_from_goals_conditions_and_scoped_gaps(journey_db, audience, workaround):
    from app.ventures import founder_facts as facts

    saved = {"audience": {"value": audience, "source": "founder message", "confidence": .95},
             "current_workaround": {"value": workaround}, "progress": {"value": "Zero paying customers"}}
    kept = [f"When no current tools coordinate {workaround.lower()}.",
            "Nothing exists for this exact workflow.", "Nothing exists to automate the remaining step.",
            "No alternatives can keep all records consistent.", "No competitors serve this exact need.",
            "3 customers would try it", "Reach 10 paying customers", "Get 10 paying customers", "We plan to start earning revenue",
            "We will have some revenue", "If we have paying customers", "Revenue tier", "No paying customers yet",
            "Zero paying customers", "0 paying customers", "Not a broad audience",
            "Do not assume nothing exists."]
    rejected = ["Nothing exists", "Nothing exists - people manage without help.",
                "Nothing exists \u2014 people manage without help.", "No tools: the issue stays unresolved.",
                "Nothing exists Check the workflow Confirm the gap", "No workaround Check the workflow",
                "No tools exist today", "No alternatives exist at all",
                "No workaround", "We have no tools.", "10 paying customers",
                "We have paying customers", "Some revenue", "We are earning revenue", "A broad category"]
    assert facts.filter_options(kept + rejected, saved) == kept
    assert not any(facts.contradicts(option, saved) for option in kept)
    assert all(facts.contradicts(option, saved) for option in rejected)


@pytest.mark.parametrize("audience,problem", [
    ("Freight coordinators", "Shipment charges require duplicate entry"),
    ("Parents planning dinners", "Weekly menus take hours to plan"),
])
def test_one_verified_quote_can_support_multiple_fields(journey_db, audience, problem):
    from app.ventures import founder_facts as facts
    text = f"{audience}: {problem}."
    raw = {field: None for field in facts.FIELDS}
    raw["audience"] = {"value": audience, "quote": text, "source": "founder message", "confidence": .95}
    raw["problem"] = {"value": problem, "quote": text, "source": "founder message", "confidence": .95}
    assert facts.validated_extraction(raw, text, "founder message") == {key: value for key, value in raw.items() if value}
    assert facts.validated_extraction(raw, "A different source", "founder message") == {}
    assert facts.validated_extraction(raw, text, "website") == {}


@pytest.mark.parametrize("name,audience", [("Dispatch Ledger", "freight coordinators"), ("Garden Lessons", "families learning gardening")])
def test_audience_quotes_cannot_replace_your_saved_product_identity(journey_db, name, audience):
    from app.db.models import Venture
    from app.ventures import founder_facts as facts

    text = f"Our audience is {audience}."
    raw = {field: None for field in facts.FIELDS}
    raw["product_name"] = {"value": audience, "quote": text, "source": "founder message", "confidence": .99}
    raw["audience"] = {"value": audience, "quote": text, "source": "founder message", "confidence": .99}
    with journey_db["factory"]() as session:
        venture = session.get(Venture, journey_db["vid"])
        facts.save(session, venture, {"product_name": facts._fact(name, "review")})
        updates = facts.validated_extraction(raw, text, "founder message")
        facts.save(session, venture, updates)
        session.commit()
        saved = facts.read(session, venture)
        assert saved["product_name"]["value"] == name
        assert saved["audience"]["value"] == audience
    for statement, field in [(f"Product name: {name}", None), (f"Our product is called {name}.", None), (name, "product_name")]:
        raw["product_name"].update(value=name, quote=statement)
        assert facts.validated_extraction(raw, statement, "founder message", field)["product_name"]["value"] == name


@pytest.mark.parametrize("audience,problem", [
    ("Freight coordinators", "Shipment charges require duplicate entry"),
    ("Parents planning dinners", "Weekly menus take hours to plan"),
])
def test_fact_reads_reconcile_new_evidence_without_promoting_websites_or_undoing_clears(journey_db, audience, problem):
    from app.db.models import Venture
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.ventures import founder_facts as facts, startup_review
    from core.clock import utcnow
    with journey_db["factory"]() as session:
        venture = session.get(Venture, journey_db["vid"])
        venture.raw_idea = "A planning tool"
        facts.save(session, venture, {"problem": {"value": problem, "quote": problem, "source": "clarification", "confidence": .95}})
        website = {"value": audience, "quote": "A planning tool for " + audience, "source": "website", "confidence": .95}
        startup_review._document(session, venture.id, "intake_decision", {
            "prefill_answers": {"customer": audience, "pricing_hypothesis": "$8/mo"},
            "website_answers": {"customer": audience, "pricing_hypothesis": "$10/mo"},
            "prefill_facts": {"customer": website}})
        session.flush()
        observed = facts.read(session, venture)
        assert observed["audience"] == website
        assert facts.policy(observed, "customer") == "confirm"
        assert observed["problem"]["value"] == problem
        assert observed["progress"]["value"] is None
        assert observed["pricing"]["value"] == "$8/mo"
        checklist = startup_review.fill_checklist(startup_review.evidence_for(session, venture))
        assert any(conflict["field"] == "business_model" for conflict in startup_review.conflicts(checklist))
        now = utcnow()
        row = IdeaSession(id="observed_session", workspace_id=venture.workspace_id, venture_id=venture.id,
            case="know", state="clarifying", created_at=now, updated_at=now)
        session.add(row)
        session.flush()
        revision = IdeaRevision(id="observed_revision", session_id=row.id, workspace_id=venture.workspace_id,
            venture_id=venture.id, rev=1, source="agent", created_at=now,
            structured=json.dumps({"customer": "Independent " + audience,
                "_website_answers": {"customer": audience, "pricing_hypothesis": "$10/mo"}}))
        session.add(revision)
        row.current_revision_id = revision.id
        session.flush()
        observed = facts.read(session, venture)
        assert observed["audience"] == website and facts.policy(observed, "customer") == "confirm"
        venture.raw_idea = f"A planning tool\nAudience: {audience}"
        assert facts.policy(facts.read(session, venture), "customer") == "skip"
        facts.save(session, venture, {"audience": None})
        assert facts.policy(facts.read(session, venture), "customer") == "ask"
        revision = append_idea_revision(session, revision.id,
            structured=json.dumps({"_answered_fields": {"customer": ""}}))
        facts.save(session, venture, {"audience": {**website, "source": "clarification"}})
        confirmed = facts.read(session, venture)
        assert confirmed["audience"]["value"] == audience and facts.policy(confirmed, "customer") == "skip"


@pytest.mark.parametrize("audience", ["Freight coordinators", "Parents planning dinners"])
def test_pressure_test_skips_only_confirmed_audiences(journey_db, monkeypatch, audience):
    from app.core.local_mode import is_local
    from app.db.models import Venture
    from app.db.supymem_models import IdeaSession
    from core.clock import utcnow
    from core.copy import no_dashes
    from app.gstack import office_hours
    from app.ventures import founder_facts as facts
    fact = {"value": audience, "quote": audience, "source": "clarification", "confidence": .95}
    snapshot = {"founder_facts": {"audience": fact}}
    monkeypatch.setattr(office_hours, "saved_snapshot", lambda *args: snapshot)
    monkeypatch.setattr(office_hours, "answers_for", lambda *args: [])
    picks = [pick for pick in office_hours.default_picks({"audience": audience}) if pick["gstack_id"] == "Q3"]
    with journey_db["factory"]() as session:
        venture = session.get(Venture, journey_db["vid"])
        venture.raw_idea = "A planning tool"
        for index, (source, confidence) in enumerate((("clarification", .95), ("website", .95), ("clarification", .7))):
            fact.update(source=source, confidence=confidence)
            facts.save(session, venture, {"audience": dict(fact)})
            row = IdeaSession(id=f"clarification_{index}", venture_id=venture.id,
                workspace_id=venture.workspace_id, case="know", state="clarifying",
                created_at=utcnow(), updated_at=utcnow())
            session.add(row)
            session.flush()
            questions = office_hours.unanswered_picks(session, row, picks)
            if index == 0:
                assert questions == []
            else:
                assert len(questions) == 1
                if is_local():
                    assert questions[0]["known_check"]["status"] == "confirm"
                    assert questions[0]["known_check"]["value"] == audience
                else:
                    assert questions[0]["text"] == no_dashes(facts.confirmation(snapshot["founder_facts"], "audience"))


@pytest.mark.parametrize("audience,task,object_name", [
    ("freight coordinators in Kenya", "manage delayed manifests", "manifests"),
    ("night-shift residents in the Netherlands", "plan weekly laundry", "laundry"),
])
def test_audience_extraction_and_saves_reject_task_objects(journey_db, audience, task, object_name):
    from app.db.models import Venture
    from app.clarify.dilemmas import venture_context
    from app.intake.smart import _prefill, classify_fallback
    from app.ventures import founder_facts as facts

    with journey_db["factory"]() as session:
        venture = session.get(Venture, journey_db["vid"])
        for verb in ("reminds", "nudges", "enables", "helps"):
            idea = f"An app that {verb} {audience} to {task}, like a checklist for {object_name}."
            venture.raw_idea = idea
            assert _prefill(idea)["customer"] == audience
            assert _prefill(idea + f"\nAudience: {object_name}")["customer"] == audience
            decision = classify_fallback(idea)
            assert decision.extracted.audience == audience
            assert facts.literal_facts(idea)["audience"]["value"] == audience
            assert venture_context({"idea": idea})[0] == audience
        for idea in (f"An app that helps {audience} {task}.", f"An app for {audience} to {task}.",
                     f"A tracker for {object_name} for {audience} to {task}."):
            assert _prefill(idea)["customer"] == audience
        person = facts._fact(audience, "clarification")
        facts.save(session, venture, {"audience": person})
        for candidate in (object_name, "plants", "dogs' food", "invoices", "orders", "data"):
            assert not _prefill(f"A tracker for {candidate}.").get("customer")
            assert not _prefill(f"Audience: {candidate}").get("customer")
            saved = facts.save(session, venture, {"audience": facts._fact(candidate, "clarification")})
            assert saved["audience"] == person
        assert not _prefill(f"Audience: {object_name} for {audience}").get("customer", "").startswith(object_name)
        facts.save(session, venture, {"audience": None})
        assert facts.save(session, venture, {"audience": facts._fact(object_name)})["audience"]["value"] is None


@pytest.mark.parametrize("idea,problem,stories", [
    ("A dispatch tool for freight coordinators in Kenya.", "Freight coordinators struggle to manage delayed deliveries.",
     ("My client missed the departure yesterday and lost a contract.",
      "I spent three hours fixing a shipment last Tuesday.", "She forgot the shipment twice.",
      "My colleague ignores the schedule and his truck left without the cargo.",
      "Sam missed the departure and lost the delivery slot.")),
    ("A laundry planner for night-shift residents in the Netherlands.", "Night-shift residents struggle to schedule laundry.",
     ("My sister missed her laundry slot yesterday and lost her booking.",
      "I spent an evening fixing a duplicate laundry booking.", "He forgot his booking twice.",
      "My neighbor ignores the calendar and her clothes were still wet.",
      "Nadia missed the laundry slot and lost the booking.")),
])
def test_instance_details_never_overwrite_problem(journey_db, idea, problem, stories):
    from app.db.models import Venture
    from app.idea.agent import answered_field, answer_metadata
    from app.ventures import founder_facts as facts, startup_review

    with journey_db["factory"]() as session:
        venture = session.get(Venture, journey_db["vid"])
        venture.raw_idea = idea
        startup_review._document(session, venture.id, "internal_new_user_flow", {"enabled": True})
        original = facts._fact(problem, "clarification")
        facts.save(session, venture, {"problem": original})
        questions = ("Tell me about one specific example.", "Why would they need this?",
                     "What evidence have you collected?", "How much time does the current workaround take?",
                     "What is their current workflow?")
        for story in stories:
            detail = answer_metadata("Assumed: Some evidence to describe\n" + story)["founder_text"]
            for question in questions:
                field = answered_field(question, "problem")
                facts.capture(session, venture, detail, source="clarification", field=field)
                assert facts.read(session, venture)["problem"] == original
                raw = dict.fromkeys(facts.FIELDS)
                raw["problem"] = facts._fact(detail, "clarification")
                updates = facts.validated_extraction(raw, detail, "clarification", field)
                assert facts.save(session, venture, updates)["problem"] == original
            fact = facts._fact(detail, "clarification")
            saved = facts.save(session, venture, {"problem": fact})
            assert saved["problem"] == original
            assert saved["evidence"] == fact
            excerpt = {**fact, "value": detail.split(" ", 1)[1]}
            assert facts.save(session, venture, {"problem": excerpt})["problem"] == original
        for general_pain in ("Customers cannot coordinate changes without extra effort.",
                             "I need a reliable workflow to coordinate changes."):
            saved = facts.save(session, venture, {"problem": facts._fact(general_pain, "clarification")})
            assert saved["problem"]["value"] == general_pain
            field = answered_field("What evidence have you collected?", "problem")
            assert "problem" not in facts.literal_facts(general_pain, source="clarification", field=field)
            raw = dict.fromkeys(facts.FIELDS)
            raw["problem"] = facts._fact(general_pain, "clarification")
            assert "problem" not in facts.validated_extraction(raw, general_pain, "clarification", field)


@pytest.mark.parametrize("audience,idea,answer,alternative", [
    ("Independent towing operators in Ohio",
     "A dispatch assistant for independent towing operators in Ohio. Independent towing operators in Ohio lose urgent jobs when assignments get mixed up.",
     "Nearly all of them use a wall map and radio calls; fixing incorrect assignments takes another round of calls.",
     "A wall map and radio calls"),
    ("University students in Kenya",
     "A meal planner for university students in Kenya. University students in Kenya struggle with wasted groceries.",
     "We use phone notes and sticky labels; fixing duplicate grocery purchases means comparing receipts after dinner.",
     "Phone notes and sticky labels"),
])
@pytest.mark.parametrize("as_fragment", [False, True])
def test_current_process_answers_never_replace_the_problem(journey_db, monkeypatch, audience, idea, answer, alternative, as_fragment):
    from app.authz import workspace
    from app.db.models import Venture
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.idea.agent import answered_field
    from app.ventures import founder_facts as facts, startup_review
    from core.clock import utcnow

    words = alternative if as_fragment else answer
    questions = [f"When {audience} have an unexpected change, what does their current process for handling it look like?",
                 f"How does the existing workflow work for {audience}?",
                 f"What is the current alternative for {audience}?"]
    for question in questions:
        assert answered_field(question, "problem") == "current_workaround"
    assert answered_field(questions[0], "problem", ["I have interviewed users", "Not sure"]) == "current_workaround"
    assert answered_field("What evidence have you collected about their current workaround?", "problem") == "evidence"
    assert answered_field("What percentage of attempts to use the current workaround fail?", "problem") == "metric"
    for field in ("current_workaround", "current_solution", "competitors", "gstack_Q2"):
        updates = facts.literal_facts(words, source="clarification", field=field)
        assert "problem" not in updates
        assert updates["current_workaround"] == facts._fact(words, "clarification")
        raw = dict.fromkeys(facts.FIELDS)
        raw["problem"] = facts._fact(words, "clarification")
        assert "problem" not in facts.validated_extraction(raw, words, "clarification", field)
        raw = dict.fromkeys(facts.FIELDS)
        raw["current_workaround"] = facts._fact(words, "clarification")
        assert facts.validated_extraction(raw, words, "clarification", field)["current_workaround"] == raw["current_workaround"]
    now = utcnow()
    with journey_db["factory"]() as session:
        venture = session.get(Venture, journey_db["vid"])
        venture.raw_idea = idea
        startup_review._document(session, venture.id, "internal_new_user_flow", {"enabled": True})
        saved = facts.save(session, venture, {"problem": facts._fact(idea.split(". ", 1)[1])})
        problem = saved["problem"]
        facts.capture(session, venture, words, source="clarification", field=answered_field(questions[0], "problem"))
        saved = facts.read(session, venture)
        assert saved["problem"] == problem
        assert saved["current_workaround"] == facts._fact(words, "clarification")
        session.add(IdeaSession(id="process_session", venture_id=venture.id, workspace_id=venture.workspace_id,
            case="know", state="clarifying", created_at=now, updated_at=now))
        session.flush()
        session.add_all([
            ClarifierTurn(id="process_question", session_id="process_session", venture_id=venture.id,
                workspace_id=venture.workspace_id, turn=1, role="agent", kind="question", text=questions[0],
                options=json.dumps({"field": "problem"}), created_at=now),
            ClarifierTurn(id="process_answer", session_id="process_session", venture_id=venture.id,
                workspace_id=venture.workspace_id, turn=2, role="founder", kind="answer", text=words, created_at=now),
        ])
        session.commit()
    with monkeypatch.context() as patch:
        patch.setattr(workspace, "db", lambda: pytest.fail("Fact capture must check access in its write transaction"))
        captured = facts._capture_turn(journey_db["vid"], words, journey_db["actor"], "founder message", None)
    assert captured[3:] == ("clarification", "current_workaround")
    assert captured[2]["problem"] == problem
    assert captured[2]["current_workaround"] == facts._fact(words, "clarification")


@pytest.mark.parametrize("idea,answer,tools", [
    ("A service that reconciles supplier invoices",
     "Not sure, maybe factory finance teams could use it first because they handle disputed invoices?",
     "Shared spreadsheets and email"),
    ("An app that helps families plan accessible holidays",
     "I don't know, maybe parents planning holidays could use it first because they need accessible activities?",
     "Printed guides and phone notes"),
])
@pytest.mark.parametrize("as_answer", [False, True])
def test_workaround_prefills_require_matching_answers_or_explicit_statements(journey_db, monkeypatch, idea, answer, tools, as_answer):
    import asyncio
    from app.db.models import Venture
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.ventures import founder_facts as facts, startup_review
    from core.clock import utcnow
    from tests.idea_fixtures import completion, install_provider

    data = journey_db
    source = "clarification" if as_answer else "founder message"
    for unrelated in (answer, f"Can people use {tools.lower()}?", f"Review how they use {tools.lower()}."):
        raw = dict.fromkeys(facts.FIELDS)
        raw["current_workaround"] = facts._fact(unrelated, source)
        assert "current_workaround" not in facts.literal_facts(unrelated, source=source, field="first_user_path")
        assert "current_workaround" not in facts.validated_extraction(raw, unrelated, source, "first_user_path")

    def extract_reply(messages, kwargs):
        text = json.loads(messages[-1]["content"])["turn"]
        raw = dict.fromkeys(facts.FIELDS)
        raw["current_workaround"] = facts._fact(text, source if text == answer else "founder message")
        response = completion()
        response.choices[0].message.content = json.dumps(raw)
        return response

    calls = install_provider(monkeypatch, extract_reply)
    monkeypatch.setenv("LLM_CASSETTE_MODE", "off")
    monkeypatch.setenv("LLM_OFFLINE", "0")
    with data["factory"]() as session:
        venture = session.get(Venture, data["vid"])
        venture.raw_idea = idea
        startup_review._document(session, venture.id, "internal_new_user_flow", {"enabled": True})
        startup_review._document(session, venture.id, "journey_overrides", {"startup_context": {"editing_field": "competitors"}})
        if as_answer:
            now = utcnow()
            session.add(IdeaSession(id="fact_question_session", workspace_id=data["wid"], venture_id=venture.id,
                case="know", state="clarifying", created_at=now, updated_at=now))
            session.flush()
            session.add_all([
                ClarifierTurn(id="fact_question", session_id="fact_question_session", workspace_id=data["wid"],
                    venture_id=venture.id, turn=1, role="agent", kind="question", text="Who would try this first and why?",
                    options=json.dumps({"field": "first_user_path"}), created_at=now),
                ClarifierTurn(id="fact_answer", session_id="fact_question_session", workspace_id=data["wid"],
                    venture_id=venture.id, turn=2, role="founder", kind="answer", text=answer, created_at=now),
            ])
        session.commit()
    asyncio.run(facts.extract(data["vid"], answer, data["actor"]))
    assert json.loads(calls[0][0][-1]["content"])["answered_field"] == ("first_user_path" if as_answer else None)
    with data["factory"]() as session:
        venture = session.get(Venture, data["vid"])
        assert facts.read(session, venture)["current_workaround"]["value"] is None
        state = startup_review.review(session, venture)
        assert state["checklist"]["competitors"]["value"] is None
        assert state["question"]["field"] == "competitors" and state["question"]["prefill"] == ""
    raw = dict.fromkeys(facts.FIELDS)
    raw["current_workaround"] = facts._fact(tools, "clarification")
    for field in ("current_workaround", "current_solution", "competitors", "gstack_Q2"):
        assert facts.literal_facts(tools, source="clarification", field=field)["current_workaround"]["value"] == tools
        assert facts.validated_extraction(raw, tools, "clarification", field)["current_workaround"]["value"] == tools
    statement = f"They currently use {tools.lower()}."
    raw["current_workaround"] = facts._fact(answer, source)
    assert "current_workaround" not in facts.validated_extraction(raw, statement + " " + answer, source, "first_user_path")
    assert facts.literal_facts(statement, field="first_user_path")["current_workaround"]["value"] == statement
    asyncio.run(facts.extract(data["vid"], statement, data["actor"]))
    with data["factory"]() as session:
        state = startup_review.review(session, session.get(Venture, data["vid"]))
        assert state["checklist"]["competitors"]["value"] == statement
        assert state["question"]["prefill"] == statement


def test_paper_waitlist_workaround_drives_saved_question_and_chat_recommendations(journey_db):
    from app.db.models import Venture
    from app.db.supymem_models import IdeaSession
    from app.gstack import office_hours
    from app.idea.service import serialize_turn
    from app.ventures import founder_facts as facts, startup_review
    from core.clock import utcnow

    data = journey_db
    words = "The receptionist phones people on a paper waitlist one by one."
    with data["factory"]() as session:
        from app.db.session import serialize_writes
        serialize_writes(session, key=f"startup_review:{data['vid']}")
        venture = session.get(Venture, data["vid"])
        startup_review._document(session, venture.id, "internal_new_user_flow", {"enabled": True})
        session.flush()
        facts.capture(session, venture, words, source="clarification", field="workflow")
        saved = facts.read(session, venture)
        assert saved["current_workaround"]["value"] == words
        row = IdeaSession(id="waitlist_session", venture_id=venture.id, workspace_id=venture.workspace_id,
                          case="know", state="validating", created_at=utcnow(), updated_at=utcnow())
        session.add(row)
        session.flush()
        pick = next(pick for pick in office_hours.default_picks() if pick["gstack_id"] == "Q2")
        turn = office_hours.add_question_turn(session, row, pick, [], 1, 1, utcnow())
        snapshot = office_hours.saved_snapshot(session, venture.id, None)
        rendered = serialize_turn(turn, snapshot)["options"]
        assert rendered["prefill"] == words
        assert rendered["recommended_option"] == words
        assert not any(facts.contradicts(option, saved) for option in rendered["options"])
        _, actions = office_hours._reply(turn, snapshot)
        recommended = next(action for action in actions if action["payload"].get("recommended"))
        assert recommended["label"] == words
        assert recommended["payload"]["prefill"] == words


@pytest.mark.parametrize("question_text", [
    "When an exhausted parent doesn't know when the next nap should be, what do they typically do?",
    "What do they do today when this problem comes up? Describe their current workaround.",
])
def test_parent_workaround_clarification_is_reused_in_pressure_test(journey_db, question_text):
    from app.db.models import Venture
    from app.db.supymem_models import IdeaSession
    from app.gstack import office_hours
    from app.idea.agent import answered_field
    from app.idea.service import serialize_turn
    from app.ventures import founder_facts as facts, startup_review
    from core.clock import utcnow

    words = "Search Google or ask in a parenting group/forum"
    field = answered_field(question_text, "workflow")
    assert field == "current_workaround"
    with journey_db["factory"]() as session:
        from app.db.session import serialize_writes
        serialize_writes(session, key=f"startup_review:{journey_db['vid']}")
        venture = session.get(Venture, journey_db["vid"])
        venture.raw_idea = "An app that helps new parents track their baby's naps and feeds and suggests a daily nap schedule."
        startup_review._document(session, venture.id, "internal_new_user_flow", {"enabled": True})
        session.flush()
        facts.capture(session, venture, words, source="clarification", field=field)
        saved = facts.read(session, venture)
        assert saved["current_workaround"] == {"value": words, "source": "clarification", "quote": words, "confidence": .95}
        row = IdeaSession(id="parent_session", venture_id=venture.id, workspace_id=venture.workspace_id,
            case="know", state="validating", created_at=utcnow(), updated_at=utcnow())
        session.add(row)
        session.flush()
        picks = office_hours.default_picks({"audience": "new parents"})
        remaining = office_hours.unanswered_picks(session, row, picks)
        assert [pick["gstack_id"] for pick in remaining] == ["Q1"]
        answer = next(answer for answer in office_hours.answers_for(session, row.id) if answer["id"] == "Q2")
        assert answer["answer"] == words and not answer["assumed"] and not answer["skipped"]
        assert [pick["gstack_id"] for pick in office_hours.unanswered_picks(session, row, picks)] == ["Q1"]
        pick = next(pick for pick in picks if pick["gstack_id"] == "Q2")
        turn = office_hours.add_question_turn(session, row, pick, [], 1, 1, utcnow())
        assert turn.kind == "proposal"
        row = IdeaSession(id="parent_render_session", venture_id=venture.id, workspace_id=venture.workspace_id,
            case="know", state="validating", created_at=utcnow(), updated_at=utcnow())
        session.add(row)
        session.flush()
        turn = office_hours.add_question_turn(session, row, pick, [], 1, 1, utcnow())
        snapshot = office_hours.saved_snapshot(session, venture.id, None)
        rendered = serialize_turn(turn, snapshot)["options"]
        assert rendered["prefill"] == words
        assert rendered["recommended_option"] == words
        _, actions = office_hours._reply(turn, snapshot)
        recommended = next(action for action in actions if action["payload"].get("recommended"))
        assert recommended["label"] == words
        assert recommended["payload"]["prefill"] == words
        assert not any(facts.contradicts(option, saved) for option in rendered["options"])


@pytest.mark.parametrize("audience,problem,limit", [
    ("commercial refrigeration maintenance teams", "Maintenance teams cannot reconcile service histories across depots.",
     "We cannot overwrite technicians' service records without a supervisor's approval."),
    ("people sharing a household pantry", "Households can't track groceries before they expire.",
     "We can't publish household shopping habits or enable purchases without permission."),
])
def test_constraint_answers_preserve_customer_problem_and_saved_limits(request, audience, problem, limit):
    data = request.getfixturevalue("journey_db")
    from app.db.models import Venture
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.idea import agent, runner, service
    from app.ventures import founder_facts as facts
    from app.ventures import startup_review
    from core.clock import utcnow

    for source in ("founder message", "clarification"):
        assert "problem" not in facts.literal_facts(limit, source=source, field="constraints")
        assert "problem" not in facts.literal_facts("Problem: " + limit, source=source, field="constraints")
        raw = dict.fromkeys(facts.FIELDS)
        raw["problem"] = facts._fact(limit, source)
        assert "problem" not in facts.validated_extraction(raw, limit, source, "constraints")
        assert facts.literal_facts(problem, source=source, field="problem")["problem"] == facts._fact(problem, source)
        assert facts.validated_extraction(raw, limit, source, "problem")["problem"] == raw["problem"]
    now = utcnow()
    with data["factory"]() as session:
        venture = session.get(Venture, data["vid"])
        venture.raw_idea = f"Audience: {audience}\nProblem: {problem}"
        startup_review._document(session, venture.id, "internal_new_user_flow", {"enabled": True})
        facts.capture(session, venture, problem, source="clarification", field="problem")
        facts.capture(session, venture, limit, source="clarification", field="constraints")
        idea = IdeaSession(id="limits_session", venture_id=venture.id, workspace_id=venture.workspace_id,
            case="know", state="clarifying", created_at=now, updated_at=now)
        session.add(idea)
        session.flush()
        original = service._append_revision(session, idea, {"customer": audience, "problem": problem}, source="founder")
        original_body = original.structured
        session.add_all([
            ClarifierTurn(id="limits_question", session_id=idea.id, venture_id=venture.id,
                workspace_id=venture.workspace_id, turn=1, role="agent", kind="question",
                text="What must your first version avoid?", options=json.dumps({"field": "constraints"}), created_at=now),
            ClarifierTurn(id="limits_answer", session_id=idea.id, venture_id=venture.id,
                workspace_id=venture.workspace_id, turn=2, role="founder", kind="answer", text=limit,
                options=json.dumps({"field": "constraints"}), revision_id=original.id, created_at=now),
        ])
        structured = {"customer": audience, "problem": problem, "constraints": [limit], "_answered_fields": {"constraints": [limit]}}
        revision = service._append_revision(session, idea, structured, source="founder")
        revision_id = revision.id
        session.commit()
        assert original.structured == original_body
    with data["factory"]() as session:
        venture = session.get(Venture, data["vid"])
        saved = facts.read(session, venture)
        assert saved["problem"] == facts._fact(problem, "clarification")
        stored = service.get_session("limits_session")["current_revision"]
        assert stored["id"] == revision_id
        assert stored["structured"]["constraints"] == [limit]
        snapshot = {"original_idea": venture.raw_idea, "understanding": stored["structured"], "founder_facts": saved,
            "answered_fields": stored["structured"]["_answered_fields"],
            "conversation": [{"role": "founder", "kind": "answer", "text": limit}]}
        result = runner._core_clarification(snapshot, agent.Clarification(case="know", action="proposal",
            message="Review your idea and tell me what to change.", understanding=agent.Understanding(customer=audience)))
        assert result.understanding.problem == problem
        assert result.understanding.constraints == [limit]


@pytest.mark.parametrize("audience,problem,words", [
    ("Clinic owners", "Clinics lose appointment revenue when cancelled slots stay empty.",
     "I don't know the exact rate yet. Two clinic owners I spoke to said roughly 1 in 10 appointments are missed."),
    ("Shared pantry households", "Households cannot track groceries before they expire.",
     "I don't know the exact rate yet. Three housemates I spoke to said roughly 1 in 8 groceries expires unused."),
])
def test_metric_clarification_cannot_replace_problem_and_gtm_prefers_structured_problem(journey_db, audience, problem, words):
    from app.core.local_mode import is_local
    from app.db.models import Venture
    from app.db.supymem_models import ClarifierTurn, IdeaRevision, IdeaSession
    from app.gtm import journey
    from app.idea.agent import answered_field
    from app.ventures import founder_facts as facts, startup_review
    from core.clock import utcnow

    data = journey_db
    field = answered_field("What is the no-show rate?", "problem")
    assert field == "metric"
    assert "problem" not in facts.literal_facts(words, source="clarification", field=field)
    assert "problem" not in facts.literal_facts("Problem: I cannot measure the rate yet.", source="clarification", field=field)
    raw = {name: None for name in facts.FIELDS}
    raw["problem"] = {"value": words, "quote": words, "source": "clarification", "confidence": .95}
    assert "problem" not in facts.validated_extraction(raw, words, "clarification", field)
    now = utcnow()
    with data["factory"]() as session:
        from app.db.session import serialize_writes
        serialize_writes(session, key=f"startup_review:{data['vid']}")
        venture = session.get(Venture, data["vid"])
        startup_review._document(session, venture.id, "internal_new_user_flow", {"enabled": True})
        row = IdeaSession(id="metric_session", venture_id=venture.id, workspace_id=venture.workspace_id, case="know",
                          state="confirmed", current_revision_id="metric_revision", confirmed_revision_id="metric_revision", created_at=now, updated_at=now)
        session.add(row)
        session.flush()
        session.add(IdeaRevision(id="metric_revision", session_id=row.id, venture_id=venture.id, workspace_id=venture.workspace_id,
                                rev=1, source="founder", structured=json.dumps({"problem": problem, "customer": audience}), created_at=now))
        session.add_all([ClarifierTurn(id="metric_question", session_id=row.id, venture_id=venture.id, workspace_id=venture.workspace_id,
            turn=1, role="agent", kind="question", text="What is the no-show rate?", options=json.dumps({"field": "problem"}), created_at=now),
            ClarifierTurn(id="metric_answer", session_id=row.id, venture_id=venture.id, workspace_id=venture.workspace_id,
            turn=2, role="founder", kind="answer", text=words, created_at=now)])
        session.flush()
        assert facts.read(session, venture)["problem"]["value"] == problem
        facts.save(session, venture, {"problem": raw["problem"]})
        session.commit()
    prefill = journey._icp(data["vid"])
    assert prefill["problem"] == problem
    assert prefill["problem_basis"] == "From your saved idea."
    if is_local():
        assert prefill["known_checks"]["problem"]["source"] == "Your clarification"
    refreshed = journey._journey_icp(data["vid"], {**prefill, "problem": words,
        "problem_basis": "From your confirmed founder facts.", "confirmed": True}, base=prefill)
    assert refreshed["problem"] == problem


def test_revision_clarification_overrides_initial_problem_with_provenance(journey_db):
    from app.db.models import Venture
    from app.db.session import serialize_writes
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.ventures import founder_facts as facts
    from core.clock import utcnow

    data = journey_db
    problem = "Restaurant GMs struggle to coordinate shift swaps."
    now = utcnow()
    with data["factory"]() as session:
        serialize_writes(session, key=f"startup_review:{data['vid']}")
        venture = session.get(Venture, data["vid"])
        venture.raw_idea = "Problem: ShiftSwap helps managers stop spending Sunday nights fixing the rota over text."
        session.add(IdeaSession(id="clarification_session", venture_id=venture.id, workspace_id=venture.workspace_id,
            case="know", state="confirmed", current_revision_id="clarification_revision", created_at=now, updated_at=now))
        session.flush()
        session.add(IdeaRevision(id="clarification_revision", session_id="clarification_session", venture_id=venture.id,
            workspace_id=venture.workspace_id, rev=1, source="founder", structured=json.dumps({"problem": problem}), created_at=now))
        session.flush()
        expected = {"value": problem, "source": "clarification", "quote": problem, "confidence": .95}
        assert facts.read(session, venture)["problem"] == expected
        facts.save(session, venture, {})
        session.commit()
        assert facts.read(session, venture)["problem"] == expected


@pytest.mark.parametrize("name,audience,problem,testimony,workaround", [
    ("ShiftSwap", "independent restaurant owners in the US", "Managers are spending Sunday nights fixing the rota over text",
     "Two GMs I worked with said they lose 2-3 hours every Sunday on the rota. Nobody has paid us yet.",
     "A GM pays $40/month for 7shifts but still swaps by text"),
    ("PupPath", "first-time puppy owners in US apartments", "Puppy owners cannot fit toilet training around their workday",
     "Three puppy owners told me they missed toilet-training sessions. Zero paying customers.",
     "They are using Dogo and phone reminders"),
])
def test_scripted_transcript_no_reasks_contradictions_or_motivations(journey_db, name, audience, problem, testimony, workaround):
    from app.db.models import Venture
    from app.db.supymem_models import IdeaSession
    from app.entry.chat_start import _onboarding_progress
    from app.gstack import office_hours
    from app.gtm import journey
    from app.ventures import founder_facts as facts, startup_review

    data = journey_db
    original = f"Product name: {name}\nWe sell to {audience}.\nProblem: {problem}\nOffer: a shift scheduling app"
    assert _onboarding_progress({"path": "startup", "extracted": {"idea": original}}, original).get("field") != "customer"
    with data["factory"]() as session:
        from app.db.session import serialize_writes
        serialize_writes(session, key=f"startup_review:{data['vid']}")
        venture = session.get(Venture, data["vid"])
        venture.name, venture.raw_idea = name, original
        startup_review._document(session, venture.id, "internal_new_user_flow", {"enabled": True})
        session.flush()
        facts.capture(session, venture, original)
        from core.clock import utcnow
        row = IdeaSession(id="transcript", venture_id=venture.id, workspace_id=venture.workspace_id, state="validating",
            case="know", created_at=utcnow(), updated_at=utcnow())
        session.add(row)
        session.flush()
        from core.clock import utcnow
        picks = office_hours.unanswered_picks(session, row, office_hours.default_picks({"audience": audience}))
        assert [pick["gstack_id"] for pick in picks] == ["Q1", "Q2"]
        question = office_hours.add_question_turn(session, row, picks[0], picks[1:], 1, len(picks), utcnow())
        reply = office_hours.record_answer(session, row, question, json.loads(question.options), testimony + " " + workaround, utcnow())
        from app.db.supymem_models import ClarifierTurn
        from sqlalchemy import select
        next_question = session.scalar(select(ClarifierTurn).where(ClarifierTurn.session_id == row.id,
            ClarifierTurn.role == "agent").order_by(ClarifierTurn.turn.desc()))
        assert json.loads(next_question.options)["gstack_id"] != "Q3"
        assert reply["gstack_turn_id"] is None
        assert not facts.contradicts(json.loads(next_question.options)["recommended_option"], facts.read(session, venture))
        facts.capture(session, venture, "Promoted for low labour cost and full shifts; fired for understaffed Friday nights.", source="gstack", field="gstack_Q3")
        facts.capture(session, venture, "Book our first customer call", field="priorities")
        session.commit()
        saved = facts.read(session, venture)
        assert saved["audience"]["value"] == audience
        assert saved["problem"]["value"] == problem
        assert "paid us" in saved["progress"]["value"] or "Zero paying" in saved["progress"]["value"]
        assert "promoted" not in json.dumps(saved).lower()
        assert all(facts.policy(saved, field) == "skip" for field in ("audience", "problem", "progress", "priorities"))
        candidates = ["Nothing exists — people just live with the pain", "No workaround", "10 paying customers", "Some revenue", "A broad category", "Describe the existing workaround"]
        assert facts.filter_options(candidates, saved) == ["Describe the existing workaround"]
        assert facts.contradicts("Nothing — they live with it", saved)
        from app.idea.agent import Clarification, Understanding, Question
        from app.idea.runner import _core_clarification
        result = Clarification(case="know", action="question", message="Saved", understanding=Understanding(),
            question=Question(text="Who is it for?", field="customer", why="Audience", options=[]))
        snapshot = {"founder_facts": saved, "active_core_field": "customer", "question_fields_asked": ["offer"]}
        from app.core.local_mode import is_local
        clarified = _core_clarification(snapshot, result)
        if is_local():
            assert clarified.action == "question" and clarified.question.field == "pricing_hypothesis"
            assert saved["pricing"]["value"] is None
        else:
            assert clarified.question is None
        picks = office_hours.unanswered_picks(session, row, office_hours.default_picks({"audience": audience}))
        assert picks == []
        review = startup_review.review(session, venture)
        assert review["question"] is None
        assert review["priority_text"] == "Book our first customer call"
        assert review["checklist"]["problem"]["value"] == problem
        from app.chat.store import append_message
        body = "<!--employee:Charlie:Manager:manager-->\nNothing exists: people just live with the pain.\n\nThis describes a rejected suggestion, not a recommendation."
        allowed = {"label": "Describe the existing workaround", "href": "/context"}
        actions = [{"label": "Nothing exists", "href": "/context"}, allowed]
        reply = append_message(venture.id, role="assistant", body_md=body, actions=actions, idempotency_key="contradiction")
        replay = append_message(venture.id, role="assistant", body_md=body, actions=actions, idempotency_key="contradiction")
        assert reply["id"] == replay["id"]
        assert reply["body_md"] == replay["body_md"] == body
        assert reply["actions"] == replay["actions"] == [allowed]
        assert reply["citations"] == []
        from app.idea.validation_agent import parse_result, ValidationPolicyError
        revision = {"id": "founder", "text": json.dumps({"offer": "An app", "founder_facts": saved})}
        report = {"verdict": "needs_evidence", "summary": "Test the pain", "gaps": ["Unverified demand"],
            "findings": [{"method": "gstack_ceo_review", "assessment": "Founder testimony", "source_ids": ["founder"]}],
            "proposed_tests": [{"hypothesis": "Nothing exists", "action": "Describe the pain", "success_measure": "One interview"}],
            "questions": []}
        with pytest.raises(ValidationPolicyError, match="contradicts"):
            parse_result(json.dumps(report), revision, [], ["gstack_ceo_review"])
    icp = journey._icp(data["vid"])
    assert icp["who"] == audience and icp["problem"] == problem


def test_strict_extraction_and_confidence_policy(journey_db):
    from app.ventures import founder_facts as facts
    from pydantic import ValidationError
    text = "We sell to restaurant owners. Nobody has paid us yet."
    raw = {field: None for field in facts.FIELDS}
    raw["audience"] = {"value": "restaurant owners", "quote": "restaurant owners", "source": "founder message", "confidence": .95}
    raw["progress"] = {"value": "Nobody has paid us yet", "quote": "Nobody has paid us yet", "source": "founder message", "confidence": .95}
    saved = facts.validated_extraction(raw, text, "founder message")
    assert facts.policy(saved, "audience") == "skip" and facts.policy(saved, "pricing") == "ask"
    saved["audience"]["confidence"] = .7
    assert facts.policy(saved, "audience") == "confirm"
    assert facts.confirmation(saved, "audience") == "I understood restaurant owners — correct? Edit it if needed."
    from app.idea.agent import Clarification, Understanding, Question
    from app.idea.runner import _core_clarification
    result = Clarification(case="know", action="question", message="Saved", understanding=Understanding(),
        question=Question(text="Who is it for?", field="customer", why="Audience", options=[]))
    updated = _core_clarification({"founder_facts": saved, "active_core_field": "customer"}, result)
    assert updated.question.text == facts.confirmation(saved, "audience")
    raw["problem"] = {**raw["audience"], "quote": "invented quote"}
    assert "problem" not in facts.validated_extraction(raw, text, "founder message")
    raw["problem"]["extra"] = "no"
    with pytest.raises(ValidationError):
        facts.validated_extraction(raw, text, "founder message")


@pytest.mark.parametrize("product", ["Freight audit product", "Family recipe product"])
def test_model_extraction_persists_nullable_slots_and_replays_without_spend(journey_db, monkeypatch, product):
    import asyncio
    from tests.idea_fixtures import completion, install_provider
    from app.db.models import Venture, VentureDocument
    from app.ventures import founder_facts as facts, startup_review
    from llm_gateway import sole

    text = f"{product} costs twenty dollars each month."
    raw = {field: None for field in facts.FIELDS}
    raw["pricing"] = {"value": "twenty dollars each month", "quote": text, "source": "founder message", "confidence": .95}
    response = completion()
    response.choices[0].message.content = json.dumps(raw)
    calls = install_provider(monkeypatch, lambda messages, kwargs: response)
    monkeypatch.setenv("LLM_CASSETTE_MODE", "off")
    monkeypatch.setenv("LLM_OFFLINE", "0")
    original = sole.transmit
    schemas = []
    async def transmit(**kwargs):
        assert kwargs["tier"] == "flash"
        assert kwargs["max_tokens"] <= 1024
        assert kwargs["single_attempt"] is True
        schemas.append(kwargs["response_schema"])
        return await original(**kwargs)
    monkeypatch.setattr(sole, "transmit", transmit)
    data = journey_db
    with data["factory"]() as session:
        startup_review._document(session, data["vid"], "internal_new_user_flow", {"enabled": True})
        session.commit()
    asyncio.run(facts.extract(data["vid"], text, data["actor"]))
    with data["factory"]() as session:
        venture = session.get(Venture, data["vid"])
        saved = facts.read(session, venture)
        assert saved["pricing"] == raw["pricing"]
        assert saved["priorities"]["value"] is None
        row = session.get(VentureDocument, f"internal_startup_checklist:{data['vid']}")
        assert len(json.loads(row.body_md)["extracted_turns"]) == 1
        from app.db.session import serialize_writes
        session.rollback()
        serialize_writes(session, key=f"startup_review:{data['vid']}")
        facts.capture(session, venture, "Thirty dollars each month", source="clarification", field="pricing")
        session.commit()
    asyncio.run(facts.extract(data["vid"], text, data["actor"], source="clarification", field="pricing"))
    assert len(calls) == 1
    with data["factory"]() as session:
        assert facts.read(session, session.get(Venture, data["vid"]))["pricing"]["value"] == "Thirty dollars each month"
    assert "additionalProperties" in calls[0][0][0]["content"]
    assert schemas[0]["additionalProperties"] is False
    assert set(schemas[0]["required"]) == set(facts.FIELDS)


def test_failed_extraction_is_not_retried_or_logged_with_provider_details(journey_db, monkeypatch, caplog):
    import asyncio
    from tests.idea_fixtures import install_provider
    from app.db.models import VentureDocument
    from app.ventures import founder_facts as facts, startup_review

    def fail(messages, kwargs):
        raise TimeoutError("sensitive founder and provider details")

    calls = install_provider(monkeypatch, fail)
    monkeypatch.setenv("LLM_CASSETTE_MODE", "off")
    monkeypatch.setenv("LLM_OFFLINE", "0")
    data = journey_db
    with data["factory"]() as session:
        startup_review._document(session, data["vid"], "internal_new_user_flow", {"enabled": True})
        session.commit()
    text = "We need help finding a first customer."
    asyncio.run(facts.extract(data["vid"], text, data["actor"]))
    asyncio.run(facts.extract(data["vid"], text, data["actor"], source="clarification", field="priorities"))
    assert len(calls) == 1
    with data["factory"]() as session:
        record = json.loads(session.get(VentureDocument, f"internal_startup_checklist:{data['vid']}").body_md)
        assert len(record["extraction_attempts"]) == 1
    assert [record.message for record in caplog.records if record.name == facts.__name__] == ["founder_fact_extraction_unavailable"]


def test_secret_private_and_suggested_answers_never_become_shared_facts(journey_db):
    from app.chat.conversations import ConversationScope, _CURRENT
    from app.db.models import Venture
    from app.ventures import founder_facts as facts, startup_review
    data = journey_db
    with data["factory"]() as session:
        from app.db.session import serialize_writes
        serialize_writes(session, key=f"startup_review:{data['vid']}")
        venture = session.get(Venture, data["vid"])
        startup_review._document(session, data["vid"], "internal_new_user_flow", {"enabled": True})
        session.flush()
        facts.capture(session, venture, "Priorities: sk-abcdefghijklmnopqrstuvwx")
        token = _CURRENT.set(ConversationScope("private", data["vid"], data["wid"], data["actor"], "private"))
        try:
            facts.capture(session, venture, "Audience: private medical records")
        finally:
            _CURRENT.reset(token)
        facts.capture(session, venture, "Assumed: Audience: everyone")
        saved = facts.read(session, venture)
        assert saved["priorities"]["value"] is None and saved["audience"]["value"] is None


def test_founder_corrections_clears_and_medium_icp_confirmations(journey_db, monkeypatch):
    from app.core.local_mode import is_local
    from app.db.models import Venture
    from app.db.supymem_models import IdeaSession
    from app.db.session import serialize_writes
    from app.idea.service import patch_locked
    from app.gtm import journey
    from app.ventures import founder_facts as facts, startup_review
    from core.clock import utcnow
    data = journey_db
    with data["factory"]() as session:
        serialize_writes(session, key=f"startup_review:{data['vid']}")
        venture = session.get(Venture, data["vid"])
        startup_review._document(session, venture.id, "internal_new_user_flow", {"enabled": True})
        session.flush()
        audience = {"value": "Apartment puppy owners", "source": "website", "quote": "Apartment puppy owners", "confidence": .7}
        facts.save(session, venture, {"audience": audience})
        row = IdeaSession(id="correction", venture_id=venture.id, workspace_id=venture.workspace_id, case="know", state="clarifying",
            created_at=utcnow(), updated_at=utcnow())
        session.add(row)
        session.commit()
    icp = journey._icp(data["vid"])
    assert icp["who"] == audience["value"]
    if is_local():
        assert icp["known_checks"]["audience"]["status"] == "confirm"
        assert icp["known_checks"]["audience"]["value"] == audience["value"]
    else:
        assert icp["fact_confirmations"]["audience"] == facts.confirmation({"audience": audience}, "audience")
    with data["factory"]() as session:
        serialize_writes(session, key=f"startup_review:{data['vid']}")
        row = session.get(IdeaSession, "correction")
        patch_locked(session, row, {"customer": "Dog walkers", "problem": "Missed walks"})
        session.commit()
        assert facts.read(session, session.get(Venture, data["vid"]))["audience"]["value"] == "Dog walkers"
        patch_locked(session, row, {"customer": ""})
        session.commit()
        assert facts.policy(facts.read(session, session.get(Venture, data["vid"])), "audience") == "ask"
    monkeypatch.setattr(journey, "prepare", lambda *args, **kwargs: {})
    journey.update_icp(data["wid"], data["vid"], "Apartment puppy owners", problem="Missed toilet training")
    with data["factory"]() as session:
        saved = facts.read(session, session.get(Venture, data["vid"]))
        assert saved["audience"]["value"] == "Apartment puppy owners"
        assert saved["problem"]["value"] == "Missed toilet training"

"""Saved evidence, private classification and reversible founder priorities."""
import asyncio
import json

import pytest

from tests.unit.gtm.test_journey import journey_db as journey_db
from tests.company_os_harness import load_company_os_app

load_company_os_app()


class _CurrentReview:
    """Resolve startup_review at call time: other suites reload app modules
    (load_company_os_app), so a module bound at import goes stale."""

    __slots__ = ()  # Delegate __dict__ too: pytest 9.2 records attribute undo from it.

    @staticmethod
    def _module():
        import importlib
        return importlib.import_module("app.ventures.startup_review")

    def __getattr__(self, name):
        return getattr(self._module(), name)

    def __setattr__(self, name, value):  # monkeypatch.setattr(review, ...) patches the live module
        setattr(self._module(), name, value)

    def __delattr__(self, name):
        delattr(self._module(), name)


review = _CurrentReview()


def test_review_proxy_monkeypatch_restores_live_module_attribute():
    proxy = _CurrentReview()
    module = proxy._module()
    original = module._mutate
    assert vars(proxy) is vars(module)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(proxy, "_mutate", object())
        patch.setattr(proxy, "_mutate", original)
    assert module._mutate is original


def checklist(**facts):
    return review.fill_checklist([{"facts": facts, "source": "Founder", "confidence": .95}])


def _shift_swap(data, *, clarification=""):
    from app.db.models import Venture
    from app.db.supymem_models import ClarifierTurn, IdeaRevision, IdeaSession
    from core.clock import utcnow
    now = utcnow()
    audience = "independent restaurant owners in the US"
    problem = "Managers are spending Sunday nights fixing the rota over text."
    with data["factory"]() as session:
        venture = session.get(Venture, data["vid"])
        venture.raw_idea = f"ShiftSwap helps managers stop spending Sunday nights fixing the rota over text. We sell to {audience}."
        review._document(session, venture.id, "internal_new_user_flow", {"enabled": True})
        session.add(IdeaSession(id="shift_session", workspace_id=data["wid"], venture_id=venture.id, case="know", state="confirmed",
            current_revision_id="shift_revision", confirmed_revision_id="shift_revision", created_at=now, updated_at=now))
        session.flush()
        session.add(IdeaRevision(id="shift_revision", session_id="shift_session", workspace_id=data["wid"], venture_id=venture.id,
            rev=1, source="founder", structured=json.dumps({"offer": "Restaurant rota scheduling software", "customer": audience, **({"problem": clarification} if clarification else {})}), created_at=now))
        for number, (qid, answer) in enumerate((("Q1", "Two GMs I worked with said they lose 2-3 hours every Sunday on the rota. Nobody has paid us yet."),
                ("Q3", audience + ". Promoted for low labour cost and full shifts; fired for understaffed Friday nights."))):
            session.add(ClarifierTurn(id=f"shift_{qid}", session_id="shift_session", workspace_id=data["wid"], venture_id=venture.id,
                turn=number * 2 + 1, role="agent", kind="question", text="What have you learned?",
                options=json.dumps({"phase": "gstack", "gstack_id": qid, "gstack_name": "Demand evidence" if qid == "Q1" else "Customer motivations"}), created_at=now))
            session.add(ClarifierTurn(id=f"shift_{qid}_answer", session_id="shift_session", workspace_id=data["wid"], venture_id=venture.id,
                turn=number * 2 + 2, role="founder", kind="answer", text=answer, created_at=now))
        session.commit()
    return audience, problem


@pytest.mark.parametrize("clarification", ["", "Restaurant GMs struggle to coordinate shift swaps."])
def test_u69_problem_uses_clarification_then_initial_pain_not_demand_evidence(journey_db, monkeypatch, clarification):
    from app.gtm import journey
    monkeypatch.setattr(journey, "_icp_seed", lambda vid: {})
    _shift_swap(journey_db, clarification=clarification)
    with journey_db["factory"]() as session:
        from app.db.models import Venture
        state = review.review(session, session.get(Venture, journey_db["vid"]))
        problem = state["checklist"]["problem"]["value"] or next(item["prefill"] for item in [state["question"]] if item and item["field"] == "problem")
    assert "Two GMs" not in problem and "lose 2-3" not in problem
    if clarification:
        assert problem == clarification
    else:
        assert "fixing the rota over text" in problem
    assert "Two GMs" not in journey._icp(journey_db["vid"])["problem"]


def test_u71_progress_reuses_no_payment_without_claiming_customer_revenue(journey_db):
    _shift_swap(journey_db)
    with journey_db["factory"]() as session:
        from app.db.models import Venture
        checklist = review.fill_checklist(review.evidence_for(session, session.get(Venture, journey_db["vid"])))
    assert checklist["progress"]["value"] == "Nobody has paid us yet."
    assert review.classify(checklist) == "Early"


def test_u72_search_uses_confirmed_audience_without_gstack_motivations(journey_db, monkeypatch):
    from app.gtm import journey
    monkeypatch.setattr(journey, "_icp_seed", lambda vid: {})
    audience, _ = _shift_swap(journey_db)
    assert journey._icp(journey_db["vid"])["who"] == audience


def test_checklist_reuses_known_answers_and_retains_sources_and_confidence():
    data = review.fill_checklist([
        {"facts": {"offer": "Order board", "customer": "Bakeries", "problem": "Lost orders", "need": "12 interviews",
                   "competitors": ["Paper"], "different": "One shared inbox", "pricing": "$12/mo", "launched": False,
                   "users": 0, "team": "Two founders", "time_worked": "6 months", "funding": "Bootstrapped"},
         "source": "Founder intake", "confidence": .95},
        {"facts": {"pricing": "$12/mo", "customer": "Bakeries", "problem": "Assumed: lost orders"},
         "source": "Website", "confidence": .8},
    ])
    assert not review.conflicts(data)
    assert data["business_model"]["value"] == "$12/mo"
    assert data["business_model"]["source"] == "Founder intake"
    assert data["business_model"]["confidence"] == .95
    assert len(data["business_model"]["evidence"]) == 2
    assert all(data[field]["value"] is not None for field in review.FIELDS if field != "product_name")


def test_suggested_choices_never_become_evidence_but_typed_context_does():
    data = checklist(problem="Assumed: New parents experience anxiety\nMy interviews found parents lose sleep logs",
                     need="Assumed: I interviewed ten parents")
    assert data["problem"]["value"] == "My interviews found parents lose sleep logs"
    assert data["need"]["value"] is None


@pytest.mark.parametrize("answer,expected", [
    ("Assumed: Parents experience anxiety\nParents lose track of naps", "Parents lose track of naps"),
    ("Assumed: Parents experience anxiety", None),
    ("Parents experience anxiety", None),
])
def test_saved_problem_clarification_uses_only_founder_text(journey_db, answer, expected):
    from app.db.models import Venture
    from app.db.supymem_models import ClarifierTurn, IdeaRevision, IdeaSession
    from core.clock import utcnow
    data, now = journey_db, utcnow()
    with data["factory"]() as session:
        session.add(IdeaSession(id="problem_session", workspace_id=data["wid"], venture_id=data["vid"],
            case="know", state="reviewing", current_revision_id="problem_revision", created_at=now, updated_at=now))
        session.flush()
        session.add(IdeaRevision(id="problem_revision", session_id="problem_session", workspace_id=data["wid"],
            venture_id=data["vid"], rev=1, source="agent", structured="{}", summary="Sleep tracker", created_at=now))
        session.add(ClarifierTurn(id="problem_question", session_id="problem_session", workspace_id=data["wid"],
            venture_id=data["vid"], turn=1, role="agent", kind="question", text="What problem?",
            options=json.dumps({"field": "problem", "options": ["Parents experience anxiety"]}), created_at=now))
        session.add(ClarifierTurn(id="problem_answer", session_id="problem_session", workspace_id=data["wid"],
            venture_id=data["vid"], turn=2, role="founder", kind="answer", text=answer, created_at=now))
        session.commit()
        known = review.fill_checklist(review.evidence_for(session, session.get(Venture, data["vid"])))
    assert known["problem"]["value"] == expected


def test_missing_problem_prefills_saved_idea_and_known_problem_can_be_edited(journey_db):
    from app.db.models import Venture, VentureDocument
    data = journey_db
    _seed(data)
    with data["factory"]() as session:
        row = session.get(VentureDocument, f"intake_decision:{data['vid']}")
        intake = json.loads(row.body_md)
        intake["prefill_answers"].pop("problem")
        intake["website_answers"] = {}
        row.body_md = json.dumps(intake)
        session.commit()
    current = review.get(data["vid"])
    assert current["question"]["field"] == "problem"
    assert current["question"]["prefill"] == "A shared order board for bakeries"
    saved = review.save_answer(data["vid"], current["revision"], "problem", "Parents lose sleep logs")
    assert saved["question"] is None
    edited = review.go_back(data["vid"], saved["revision"], "problem")
    assert edited["question"]["field"] == "problem"
    assert edited["question"]["prefill"] == "Parents lose sleep logs"
    with data["factory"]() as session:
        fields = review.effective_fields(session, session.get(Venture, data["vid"]), {})
        assert "problem" not in fields


def test_u29_problem_prefill_is_shared_and_never_contains_internal_clarification(journey_db, monkeypatch):
    from app.db.models import Venture, VentureDocument
    from app.gtm import journey
    data = journey_db
    monkeypatch.setattr(journey, "_icp_seed", data["stored_seed"])
    _seed(data)
    with data["factory"]() as session:
        venture = session.get(Venture, data["vid"])
        venture.raw_idea = "TinyPlate QA: meal-planning app for busy US parents of picky toddlers, $5/month.\nClarification: Not sure"
        row = session.get(VentureDocument, f"intake_decision:{data['vid']}")
        intake = json.loads(row.body_md)
        intake["prefill_answers"].pop("problem")
        intake["prefill_answers"]["offer"] = "Meal-planning app for busy US parents of picky toddlers"
        intake["website_answers"] = {}
        row.body_md = json.dumps(intake)
        session.commit()
    problem = "Busy US parents of picky toddlers need help with meal planning."
    assert review.get(data["vid"])["question"]["prefill"] == problem
    assert journey._icp(data["vid"])["problem"] == problem
    assert review.problem_prefill("Parents struggle to plan meals. We charge $5/month.") == "Parents struggle to plan meals."
    assert journey._journey_icp(data["vid"], {"who": "Busy US parents", "problem": "TinyPlate QA: meal-planning app for busy US parents of picky toddlers.\nClarification: Not sure",
        "confirmed": True, "problem_basis": "Your saved idea"})["problem"] == problem


def test_skips_are_reversible_and_never_record_evidence(journey_db):
    from app.db.models import Venture, VentureDocument
    data = journey_db
    _seed(data)
    with data["factory"]() as session:
        row = session.get(VentureDocument, f"intake_decision:{data['vid']}")
        intake = json.loads(row.body_md)
        intake["prefill_answers"].pop("problem")
        intake["extracted"] = {}
        row.body_md = json.dumps(intake)
        session.commit()
    current = review.get(data["vid"])
    skipped = []
    while current["question"]:
        field = current["question"]["field"]
        skipped.append(field)
        current = review.skip_question(data["vid"], current["revision"], field)
    assert set(skipped) == {"problem", "progress", "business_model"}
    with data["factory"]() as session:
        venture = session.get(Venture, data["vid"])
        fields = review.effective_fields(session, venture, {})
        assert not fields
        known = review.review(session, venture)["checklist"]
        assert all(known[field]["source"] == "Skipped" and known[field]["confidence"] == 0 for field in skipped)
    done = asyncio.run(review.save_priorities(data["vid"], current["revision"], "", data["actor"]))
    assert done["complete"]
    back = review.go_back(data["vid"], done["revision"], "problem")
    assert back["question"]["field"] == "problem" and not back["complete"]


def test_conflicts_show_each_value_and_its_origin_not_cosmetic_duplicates():
    data = review.fill_checklist([
        {"facts": {"name": "Order Board", "pricing": "$12/mo"}, "source": "Website"},
        {"facts": {"name": "Cake Desk", "pricing": "$20/mo"}, "source": "Founder"},
        {"facts": {"name": "order-board", "pricing": "$12/mo"}, "source": "PDF"},
    ])
    result = review.conflicts(data)
    assert [item["field"] for item in result] == ["product_name", "business_model"]
    assert {entry["source"] for entry in result[1]["values"]} == {"Website", "Founder"}


@pytest.mark.parametrize("facts,stage", [
    ({}, "Early"), ({"launched": False, "users": 0}, "Early"),
    ({"progress": "Not launched; no users or revenue"}, "Early"),
    ({"progress": "We haven't launched; no users"}, "Early"),
    ({"progress": "Planning to launch; target $20k MRR", "funding": "Seeking Series A"}, "Early"),
    ({"launched": True}, "Mid"), ({"users": 2}, "Mid"), ({"pilots": 1}, "Mid"),
    ({"revenue": 1, "funding": "Seed"}, "Mid"), ({"progress": "Launched; 12 active users"}, "Mid"),
    ({"mrr": 9999}, "Mid"), ({"mrr": 10000}, "Advanced"), ({"arr": 120000}, "Advanced"),
    ({"progress": "$20k MRR"}, "Advanced"), ({"progress": "MRR: $20k"}, "Advanced"),
    ({"progress": "$120k ARR"}, "Advanced"), ({"funding": "Raised Series A"}, "Advanced"),
    ({"progress": "$12,000 MRR"}, "Advanced"), ({"funding": {"round": "Series B"}}, "Advanced"),
    ({"progress": "Not launched. We have 12 users"}, "Mid"), ({"progress": "Pilot with two bakeries"}, "Mid"),
    ({"progress": "Some revenue"}, "Mid"), ({"progress": "Paying customers"}, "Mid"),
    ({"team": {"employees": 1}}, "Advanced"), ({"team": "Two founders; 3 employees"}, "Advanced"),
])
def test_rules_are_deterministic_and_ignore_plans_and_negation(facts, stage):
    assert review.classify(checklist(**facts)) == stage


@pytest.mark.parametrize("text,expected", [
    ("", ["outreach_leads"]), ("not sure", ["outreach_leads"]), ("I don't know", ["outreach_leads"]),
    ("Not sure about fundraising or research", ["outreach_leads"]),
    ("Find customers then research competitors", ["outreach_leads", "market_insights"]),
    ("Market research then fundraising", ["market_insights", "fundraising"]),
    ("Fundraising then improve onboarding", ["fundraising", "other"]),
    ("Improve onboarding", ["other"]),
])
def test_priority_mapping_and_unsure_default(text, expected):
    assert review.map_priorities(text) == expected
    assert review.map_priorities("not sure", ["fundraising"]) == ["outreach_leads"]
    assert review.map_priorities("Locate angels", ["fundraising"]) == ["fundraising"]
    assert review.map_priorities("Find customers", [{"malicious": "category"}]) == ["outreach_leads"]


@pytest.mark.parametrize("audience,problem", [
    ("regional freight dispatchers", "double-booked loads"),
    ("home sourdough bakers", "forgotten starter feedings"),
])
def test_pre_product_market_priority_does_not_insert_unrequested_outreach(journey_db, audience, problem):
    data = journey_db
    _seed(data, f"A small tool for {audience}", audience, problem)
    current = review.get(data["vid"])
    current = review.save_answer(data["vid"], current["revision"], "business_model", "$25/mo")
    text = f"Design a small market test for {audience} about {problem} before I build anything."
    saved = asyncio.run(review.save_priorities(data["vid"], current["revision"], text, data["actor"]))
    assert saved["complete"] and saved["priorities"] == ["market_insights"]
    assert saved["next_href"] == f"/v/{data['vid']}/research"
    assert review.map_priorities(text, ["market_insights"]) == ["market_insights"]


def _seed(data, offer="A shared order board for bakeries", audience="bakeries", problem="Lost orders"):
    from app.db.models import Venture, VentureDocument
    from core.clock import utcnow
    now = utcnow()
    with data["factory"]() as session:
        venture = session.get(Venture, data["vid"])
        review._document(session, venture.id, "internal_new_user_flow", {"enabled": True})
        venture.raw_idea = offer
        session.add(VentureDocument(id=f"intake_decision:{data['vid']}", venture_id=data["vid"], kind="intake_decision",
            title="Intake", body_md=json.dumps({"path": "startup", "prefill_answers": {
                "offer": venture.raw_idea, "customer": audience, "problem": problem, "pricing_hypothesis": "$12/mo"},
                "website_answers": {"pricing_hypothesis": "$20/mo"}, "extracted": {"stage_evidence": "Not launched; no users"}}),
            created_at=now, updated_at=now))
        session.commit()


@pytest.mark.parametrize("offer,audience,problem,priority", [
    ("Reconcile warehouse inventory", "Distribution operations teams", "Mismatched counts", "Improve stock reconciliation"),
    ("Plan accessible family trips", "Parents travelling with children", "Unsuitable itineraries", "Simplify itinerary sharing"),
])
def test_next_up_uses_saved_priority_text_for_new_and_legacy_tasks(journey_db, monkeypatch, offer, audience, problem, priority):
    from app.chat import execute
    from app.db.models import PlannedTask
    from app.planning import store
    from app.ventures import state_brief

    data = journey_db
    _seed(data, offer, audience, problem)
    current = review.get(data["vid"])
    current = review.save_answer(data["vid"], current["revision"], "business_model", "$25/mo")
    assert current["question"] is None
    saved = asyncio.run(review.save_priorities(data["vid"], current["revision"], priority, data["actor"]))
    assert saved["complete"] and "other" in saved["priorities"]
    with data["factory"]() as session:
        task = session.query(PlannedTask).filter_by(venture_id=data["vid"], source="startup_priorities", status="open").one()
        assert task.title == task.detail == priority
        assert task.why_now == "Requested by you"
        task.title = "Founder priority"
        session.commit()
    assert store.list_open_tasks(data["vid"])[0]["title"] == priority
    monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda *args, **kwargs: {
        "idea": {"confirmed": True}, "research": {"plan_ready": True}})
    reply = execute._whats_next(data["vid"])
    assert f"1. {priority}" in reply["body_md"]
    assert "Founder priority" not in reply["body_md"]
    assert "—" not in reply["body_md"]


def test_conflict_back_and_updated_evidence_recompute_later_steps(journey_db):
    from app.db.models import Venture, VentureDocument
    from app.ventures.current_idea import current_idea
    data = journey_db
    _seed(data)
    result = review.get(data["vid"])
    assert result["question"]["field"] == "business_model"
    assert len(result["question"]["values"]) == 2
    saved = review.save_answer(data["vid"], result["revision"], "business_model", "$25/mo")
    assert saved["question"] is None
    with pytest.raises(ValueError, match="changed"):
        review.save_answer(data["vid"], result["revision"], "business_model", "$30/mo")
    done = asyncio.run(review.save_priorities(data["vid"], saved["revision"], "not sure", data["actor"]))
    assert done["complete"] and done["priorities"] == ["outreach_leads"]
    assert done["next_href"].startswith("/gtm?")
    with data["factory"]() as session:
        fields = review.effective_fields(session, session.get(Venture, data["vid"]), {})
        assert fields["pricing_hypothesis"] == "$25/mo"
        assert "$25/mo" in current_idea(session.get(Venture, data["vid"]), session=session)
    back = review.go_back(data["vid"], done["revision"], "business_model")
    assert not back["complete"] and back["question"]["field"] == "business_model"
    saved = review.save_answer(data["vid"], back["revision"], "business_model", "$30/mo")
    done = asyncio.run(review.save_priorities(data["vid"], saved["revision"], "Research", data["actor"]))
    with data["factory"]() as session:
        row = session.get(VentureDocument, f"intake_decision:{data['vid']}")
        intake = json.loads(row.body_md)
        intake["website_answers"]["pricing_hypothesis"] = "$40/mo"
        row.body_md = json.dumps(intake)
        session.commit()
    changed = review.get(data["vid"])
    assert not changed["complete"] and changed["question"]["field"] == "business_model"
    assert "$40/mo" in [item["value"] for item in changed["question"]["values"]]


def test_saved_plan_items_are_nonrunnable_and_internal_stage_never_leaks(journey_db):
    from sqlalchemy import select
    from app.db.models import PlannedTask, VentureDocument
    from app.research.presentation import founder_payload, founder_report
    from app.ventures.founder_control import export_pack
    from app.chat.context import assemble
    data = journey_db
    _seed(data)
    current = review.get(data["vid"])
    current = review.save_answer(data["vid"], current["revision"], "business_model", "$12/mo")
    result = asyncio.run(review.save_priorities(data["vid"], current["revision"], "Fundraising then improve onboarding", data["actor"]))
    assert result["acknowledgement"] and result["next_href"] == f"/v/{data['vid']}/tasks"
    assert result["priorities"] == ["fundraising", "other"]
    with data["factory"]() as session:
        internal = json.loads(session.get(VentureDocument, f"{review.INTERNAL_KIND}:{data['vid']}").body_md)
        assert internal["internal_classification"] == "Early"
        tasks = list(session.scalars(select(PlannedTask).where(PlannedTask.source == "startup_priorities")))
        assert tasks and all(not task.runnable for task in tasks)
    public = founder_payload({"nested": internal, "internal_classification": "Advanced"})
    assert "internal_classification" not in json.dumps(public) and "Advanced" not in json.dumps(public)
    report = founder_report({}, {"internal_classification": {"value": "Early"}})
    assert "internal_classification" not in json.dumps(report)
    for payload in (result, export_pack(data["vid"]), assemble(data["vid"]).documents):
        assert "internal_startup" not in json.dumps(payload) and "internal_classification" not in json.dumps(payload)


def test_next_step_requires_answers_review_even_when_idea_questions_were_skipped(journey_db):
    from app.ventures.next_step import _founder_resolve
    from tests.unit.product.test_next_step import FACTS
    pending = {"complete": False, "question": None, "priorities": []}
    facts = {**FACTS, "startup_review": pending, "idea_confirmed": True}
    assert _founder_resolve("v1", "/v/v1", facts)["key"] == "venture.context"
    ready = {**facts, "startup_review": {"complete": True, "question": None, "priorities": ["outreach_leads"]}}
    assert _founder_resolve("v1", "/v/v1", ready)["title"] == "Book a first call"
    assert _founder_resolve("v1", "/v/v1", {**facts, "idea_confirmed": False})["chat_action"] == "startup.review"
    assert _founder_resolve("v1", "/v/v1", {**facts, "personal": True})["key"] != "venture.context"


def test_validation_completion_links_review_before_confirmation_and_preserves_legacy(journey_db):
    from app.ventures.next_step import _founder_resolve
    from tests.unit.product.test_next_step import FACTS
    facts = {**FACTS, "idea_state": "reviewing", "validation_done": True}
    assert _founder_resolve("v1", "/v/v1", facts)["title"] == "Review your idea validation"
    step = _founder_resolve("v1", "/v/v1", {**facts, "startup_review": {
        "complete": False, "question": None, "priorities": []}})
    assert step["key"] == "venture.context" and step["href"] == "/v/v1/context"


@pytest.mark.parametrize("clarification", [False, True])
def test_review_uses_saved_spreadsheet_trials_before_asking_progress(journey_db, clarification):
    from app.db.models import Venture, VentureDocument
    from app.db.supymem_models import IdeaSession, IdeaRevision, ClarifierTurn
    from core.clock import utcnow
    data = journey_db
    _seed(data)
    testimony = "About 3 clinics have tried a spreadsheet version"
    now = utcnow()
    with data["factory"]() as session:
        venture = session.get(Venture, data["vid"])
        venture.raw_idea = "Booking reminders for physiotherapy clinics in India."
        intake = session.get(VentureDocument, f"intake_decision:{data['vid']}")
        payload = json.loads(intake.body_md)
        payload["extracted"].pop("stage_evidence")
        intake.body_md = json.dumps(payload)
        if clarification:
            session.add(IdeaSession(id="idea_trials", workspace_id=data["wid"], venture_id=data["vid"],
                state="reviewing", case="know", current_revision_id="rev_trials", created_at=now, updated_at=now))
            session.flush()
            session.add(IdeaRevision(id="rev_trials", session_id="idea_trials", workspace_id=data["wid"],
                venture_id=data["vid"], rev=1, source="agent", structured="{}", summary="Booking reminders", created_at=now))
            session.add(ClarifierTurn(id="answer_trials", session_id="idea_trials", workspace_id=data["wid"],
                venture_id=data["vid"], turn=1, role="founder", kind="answer", text=testimony, created_at=now))
        else:
            venture.raw_idea += " " + testimony
        session.commit()
    with data["factory"]() as session:
        checklist = review.fill_checklist(review.evidence_for(session, session.get(Venture, data["vid"])))
        assert testimony in checklist["progress"]["value"]
        assert checklist["progress"]["source"] == ("Your clarification answers" if clarification else "Your original idea")


def test_optional_priority_plan_items_do_not_become_required_banner_actions(journey_db):
    from app.db.models import PlannedTask
    from app.ventures.run_status import _project_recent_task
    from core.clock import utcnow
    result = {"state": "idle", "stage": "Saved venture", "changed_at": None}
    task = PlannedTask(source="startup_priorities", status="open", title="Founder priority", runnable=0, updated_at=utcnow())
    _project_recent_task(result, "v1", task)
    assert result == {"state": "idle", "stage": "Saved venture", "changed_at": None}


def test_both_banners_follow_priorities_and_back_actions(journey_db):
    from app.db.supymem_models import IdeaSession
    from app.ventures import next_step, run_status
    from core.clock import utcnow
    data = journey_db
    _seed(data)
    with data["factory"]() as session:
        session.add(IdeaSession(id="idea_ready", workspace_id=data["wid"], venture_id=data["vid"],
            state="confirmed", case="know", created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    current = review.get(data["vid"])
    current = review.save_answer(data["vid"], current["revision"], "business_model", "$12/mo")
    saved = asyncio.run(review.save_priorities(data["vid"], current["revision"], "Fundraising and improve onboarding", data["actor"]))
    assert next_step._facts(data["vid"])["startup_review"]["complete"]
    assert next_step.founder_next_step(data["vid"])["title"] == "Your priorities are saved as plan items"
    banner = run_status.snapshot(data["vid"], data["wid"])
    assert banner["activity"] == "Your priorities are saved as plan items"
    assert banner["action"]["href"] == saved["next_href"]
    review.go_back(data["vid"], saved["revision"], "priorities")
    assert next_step.founder_next_step(data["vid"])["href"] == f"/v/{data['vid']}/context"
    assert run_status.snapshot(data["vid"], data["wid"])["action"]["href"] == f"/v/{data['vid']}/context"


def test_saved_priorities_survive_checklist_signature_changes(journey_db):
    from app.db.models import Venture
    from app.db.supymem_models import IdeaSession
    from app.ventures import next_step, run_status
    from core.clock import utcnow
    data = journey_db
    _seed(data)
    with data["factory"]() as session:
        session.add(IdeaSession(id="ids_priorities", workspace_id=data["wid"], venture_id=data["vid"], case="know", state="confirmed", created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    current = review.get(data["vid"])
    current = review.save_answer(data["vid"], current["revision"], "business_model", "$12/mo")
    saved = asyncio.run(review.save_priorities(data["vid"], current["revision"], "Fundraising and improve onboarding", data["actor"]))
    assert saved["complete"] and saved["acknowledgement"]
    with data["factory"]() as session:
        venture = session.get(Venture, data["vid"])
        before = review.review(session, venture)["signature"]
        venture.name = "Updated bakery product"
        session.commit()
        after = review.review(session, venture)
        assert after["signature"] != before and after["question"] is None
    reloaded = review.get(data["vid"])
    assert reloaded["complete"] and reloaded["priorities"] == saved["priorities"]
    assert next_step.founder_next_step(data["vid"])["href"] == saved["next_href"]
    assert run_status.snapshot(data["vid"], data["wid"])["action"]["href"] == saved["next_href"]


def test_first_call_hook_only_adds_goal_and_preserves_saved_icp(journey_db):
    from app.gtm import journey
    data = journey_db
    legacy = journey._icp(data["vid"])
    assert "goal" not in legacy
    _seed(data)
    current = review.get(data["vid"])
    current = review.save_answer(data["vid"], current["revision"], "business_model", "$12/mo")
    research = asyncio.run(review.save_priorities(data["vid"], current["revision"], "Research", data["actor"]))
    assert "goal" not in journey._icp(data["vid"])
    current = review.go_back(data["vid"], research["revision"], "priorities")
    asyncio.run(review.save_priorities(data["vid"], current["revision"], "Find customers", data["actor"]))
    fresh = journey._icp(data["vid"])
    assert fresh["goal"] == "book a first call"
    saved = {**legacy, "who": "Founder's edited audience", "problem": "Edited problem", "problem_basis": "Founder supplied", "confirmed": True}
    result = journey._journey_icp(data["vid"], saved, base=fresh)
    assert result["goal"] == "book a first call"
    assert all(result[key] == saved[key] for key in ("who", "problem", "confirmed"))


def test_privacy_filters_keep_preexisting_stage_and_checklist_fields():
    from app.research.presentation import founder_payload
    legacy = {"stage": "BUILDING", "startup_stage": "Existing label", "checklist": ["Existing feature"], "internal_legacy": {"title": "Saved note"}}
    assert json.dumps(founder_payload(legacy)) == json.dumps(legacy)


def test_uncertain_model_mapping_is_never_replayed_and_falls_back(journey_db, monkeypatch):
    from app.credits import metered
    data = journey_db
    _seed(data)
    current = review.get(data["vid"])
    current = review.save_answer(data["vid"], current["revision"], "business_model", "$12/mo")
    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.delenv("SANDBOX_MODE", raising=False)
    calls = []
    async def uncertain(*args, **kwargs):
        calls.append(kwargs)
        raise RuntimeError("provider outcome uncertain")
    monkeypatch.setattr(metered, "llm", uncertain)
    mutate = review._mutate
    def unavailable(*args, **kwargs):
        raise RuntimeError("save unavailable")
    monkeypatch.setattr(review, "_mutate", unavailable)
    with pytest.raises(RuntimeError, match="save unavailable"):
        asyncio.run(review.save_priorities(data["vid"], current["revision"], "Find customers", data["actor"]))
    monkeypatch.setattr(review, "_mutate", mutate)
    saved = asyncio.run(review.save_priorities(data["vid"], current["revision"], "Find customers", data["actor"]))
    assert saved["complete"] and saved["priorities"] == ["outreach_leads"]
    assert len(calls) == 1 and calls[0]["request_id"].startswith("startup_priorities:")


def test_model_mapping_is_ordered_and_unsure_uses_no_provider(journey_db, monkeypatch):
    from types import SimpleNamespace
    from app.credits import metered
    data = journey_db
    _seed(data)
    current = review.get(data["vid"])
    current = review.save_answer(data["vid"], current["revision"], "business_model", "$12/mo")
    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.delenv("SANDBOX_MODE", raising=False)
    calls = []
    async def model(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"categories": ["fundraising", "market_insights"]}'))])
    monkeypatch.setattr(metered, "llm", model)
    saved = asyncio.run(review.save_priorities(data["vid"], current["revision"], "Locate angels and analyze rivals", data["actor"]))
    assert saved["priorities"] == ["fundraising", "market_insights"]
    back = review.go_back(data["vid"], saved["revision"], "priorities")
    unsure = asyncio.run(review.save_priorities(data["vid"], back["revision"], "not sure", data["actor"]))
    assert unsure["priorities"] == ["outreach_leads"] and len(calls) == 1


def test_a_founder_edit_supersedes_their_earlier_answer_but_not_the_website():
    data = review.fill_checklist([
        {"facts": {"pricing": "$10/mo"}, "source": "Founder intake", "origin": "founder", "confidence": 1},
        {"facts": {"pricing": "$20/mo"}, "source": "Website", "origin": "website", "confidence": .8},
        {"facts": {"pricing": "$30/mo"}, "source": "Founder correction", "origin": "founder", "confidence": .95},
    ])
    assert data["business_model"]["value"] == "$30/mo"
    assert [entry["value"] for entry in review.conflicts(data)[0]["values"]] == ["$20/mo", "$30/mo"]
    currencies = review.fill_checklist([{"facts": {"pricing": price}, "source": price} for price in ("$12/mo", "€12/mo")])
    assert review.conflicts(currencies)[0]["field"] == "business_model"


def test_structured_progress_choice_keeps_its_types_for_classification(journey_db):
    from app.db.models import VentureDocument
    data = journey_db
    _seed(data)
    with data["factory"]() as session:
        row = session.get(VentureDocument, f"intake_decision:{data['vid']}")
        intake = json.loads(row.body_md)
        intake["extracted"]["progress"] = {"launched": False, "users": 0}
        intake["website_answers"]["progress"] = {"launched": True, "users": 5}
        row.body_md = json.dumps(intake)
        session.commit()
    current = review.get(data["vid"])
    current = review.save_answer(data["vid"], current["revision"], "business_model", "$12/mo")
    assert current["question"]["field"] == "progress"
    value = review._words({"launched": False, "users": 0})
    current = review.save_answer(data["vid"], current["revision"], "progress", value)
    with data["factory"]() as session:
        internal = json.loads(session.get(VentureDocument, f"{review.INTERNAL_KIND}:{data['vid']}").body_md)
    assert internal["internal_classification"] == "Early"
    assert internal["checklist"]["progress"]["value"] == {"launched": False, "users": 0}

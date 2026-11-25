import json
import pytest

from tests.unit.idea.test_core_onboarding import core as core, assert_moves_past


@pytest.mark.parametrize("audience,mechanism", [
    ("Maintenance teams at factories", "Maintenance teams review safety logs"),
    ("Households with young children", "Young households share bedtime routines"),
])
def test_audience_answer_to_offer_question_is_not_lost(core, audience, mechanism):
    from app.idea.agent import answer_prefill
    from app.idea.runner import _known_fields
    from app.ventures.founder_facts import literal_facts

    parsed = answer_prefill(audience, "offer")
    assert parsed.get("customer") == audience
    assert not parsed.get("offer")
    assert literal_facts(audience, source="clarification", field="offer")["audience"]["value"] == audience
    known = _known_fields({"understanding": {}, "conversation": [
        {"role": "founder", "kind": "answer", "field": "offer", "text": audience}]})
    assert known["customer"] == audience
    assert not answer_prefill(mechanism, "offer").get("customer")


@pytest.mark.parametrize("offer,customer,inferred_customer,summary", [
    ("A tool that compares delivery manifests for depot operators.", "depot operators",
     "people who reconcile deliveries", "Delivery discrepancy alerts"),
    ("An app that plans weekend cycling routes for families.", "families",
     "people who plan bike rides", "Weekend cycling route planner"),
])
def test_saved_customer_reconciliation_keeps_the_new_idea_summary(core, offer, customer, inferred_customer, summary):
    from app.idea.runner import _core_clarification, _understanding_summary

    snapshot = {"original_idea": f"Customer: {customer}\nOffer: {offer}\nSummary: {offer}",
        "founder_idea": offer, "project_intent": "startup",
        "understanding": {"offer": offer, "customer": customer, "summary": offer},
        "answered_fields": {"offer": offer, "customer": customer}}
    result = _core_clarification(snapshot, core.Clarification(case="know", action="proposal",
        message="Check your idea, then tell me what to change.",
        understanding=core.Understanding(summary=summary, offer=summary, customer=inferred_customer)))

    assert result.understanding.customer == customer
    assert result.understanding.summary == summary
    assert summary in _understanding_summary(result.understanding)

    previous_offer = f"An earlier alternative to {summary}"
    snapshot["understanding"] = {"offer": previous_offer, "customer": customer, "summary": previous_offer}
    corrected = _core_clarification(snapshot, core.Clarification(case="know", action="proposal",
        message="Check your updated idea, then tell me what to change.",
        understanding=core.Understanding(summary=previous_offer, offer=previous_offer, customer=customer)))
    assert corrected.understanding.offer == offer
    assert corrected.understanding.summary == offer


MARKETPLACE = (
    "I want to build an AI agent marketplace where people can publish skill files or AI agents "
    "and make them using my own marketplace. Anyone on the internet can use those agents on a "
    "pay-as-you-go usage basis. The user pays for my platform, whatever compute he's using, "
    "and whatever API credits he's using. He also pays a small commission to the person who "
    "created that thing, that agent, and also a small commission to me. You can probably just "
    "describe your issue and it will give you the most relevant one created by the user. "
    "You can probably upvote the agent if you like it and the best-ranking ones are usually "
    "suggested first. Marketplace for AI agents where people can both create and use AI agents"
)
MEALS = (
    "A meal-planning app for busy parents. Parents enter allergies and choose dinners, "
    "then the app builds a shopping list. Families pay $5 per month."
)


@pytest.mark.parametrize("opening,audience,problem", [
    ("A dashboard that reconciles purchase orders for hotel procurement managers.", "hotel procurement managers",
     "Missing deliveries are discovered late because the alerts were designed for retail clerks."),
    ("An app that records trail conditions for weekend hikers.", "weekend hikers",
     "Closed paths remain on the map since updates are prepared for commuting cyclists."),
], ids=["b2b", "consumer"])
def test_problem_beneficiary_does_not_replace_the_stated_customer(core, opening, audience, problem):
    from app.idea.runner import _core_clarification, _known_fields
    from app.ventures.founder_facts import literal_facts

    snapshot = {"original_idea": opening, "project_intent": "startup",
        "understanding": {"offer": opening, "customer": audience},
        "answered_fields": {"offer": opening, "customer": audience, "problem": problem},
        "conversation": [{"role": "founder", "field": "problem", "text": problem}],
        "answered_questions": [{"field": "problem", "founder_text": problem}]}
    assert not core.answer_prefill(problem, "problem").get("customer")
    assert "audience" not in literal_facts(problem, source="clarification", field="problem")
    known = _known_fields(snapshot)
    assert known["customer"] == audience and known["problem"] == problem
    result = _core_clarification(snapshot, core.Clarification(case="know", action="proposal",
        message="Check your idea, then tell me what to change.",
        understanding=core.Understanding(customer=audience, problem=problem)))
    assert result.understanding.customer == audience and result.understanding.problem == problem
    assert snapshot["conversation"][0]["text"] == problem


@pytest.mark.parametrize("offer,audience,problem", [
    ("a permit calendar", "film location coordinators", "Missed permit deadlines cancel paid shoots."),
    ("a sky forecast", "amateur stargazers", "Cloudy viewing nights waste long journeys."),
])
def test_customer_embedded_in_an_answer_is_saved_as_a_clean_field(core, monkeypatch, offer, audience, problem):
    from types import SimpleNamespace

    from app.experiments import coach
    from app.idea import runner
    from app.idea.runner import _core_clarification, _known_fields
    from app.ventures.founder_facts import literal_facts

    answer = f"We are building {offer} for {audience}. {problem}"
    assert core.answer_prefill(answer, "problem")["customer"] == audience
    assert literal_facts(answer, source="clarification", field="problem")["audience"]["value"] == audience
    raw_customer = f"{audience} are our customers. We are building {offer} for {audience}."
    snapshot = {"original_idea": offer, "project_intent": "startup",
        "answered_fields": {"offer": offer, "customer": raw_customer, "problem": answer},
        "conversation": [{"role": "founder", "field": "problem", "text": answer}]}
    assert core.founder_audience(raw_customer) == audience
    assert core.founder_audience(audience + " for local clubs") == audience + " for local clubs"
    script = coach.interview_script(raw_customer, problem)
    assert audience in script and raw_customer not in script and "are our customers" not in script
    assert coach.interview_script(None, problem) is None
    assert _known_fields(snapshot)["customer"] == audience
    result = _core_clarification(snapshot, core.Clarification(case="know", action="proposal",
        message="Review your idea.", understanding=core.Understanding(customer=raw_customer, problem=problem)))
    assert result.understanding.customer == audience
    assert snapshot["conversation"][0]["text"] == answer
    saved = []
    monkeypatch.setattr(runner, "_receipt", lambda _: SimpleNamespace(snapshot=json.dumps(snapshot)))
    monkeypatch.setattr(runner, "_save", lambda _, **changes: saved.append(changes))
    runner._perform("clean-customer", {})
    assert core.Clarification.model_validate_json(saved[-1]["response"]).understanding.customer == audience
INVOICES = (
    "An invoice-chasing tool for creative agencies. Agencies upload unpaid invoices, "
    "then the tool sends reminders and stops when a client pays. Agencies pay $20 per month."
)


@pytest.mark.parametrize("opening,previous,audience,problem", [
    ("Software warns clinic owners about refrigeration outages.", "clinic owners",
     "facilities managers at clinics with fewer than 30 staff",
     "A refrigeration outage spoils stored vaccines, and today they discover it after stock is lost."),
    ("An app alerts families about missed school pickups.", "families",
     "parents with children under 12",
     "A forgotten pickup leaves a child waiting, and today they discover it after school calls."),
])
@pytest.mark.parametrize("statement", [
    "The customers would be {audience}.", "Our customers are {audience}.",
    "The buyer is {audience}.", "My target users will be {audience}.",
    "Our primary audience should be {audience}.", "The first customers would be {audience}.",
])
def test_customer_sentence_in_problem_updates_fields_without_becoming_the_problem(core, opening, previous, audience, problem, statement):
    from app.chat.idea_corrections import _also_answers
    from app.clarify.dilemmas import ground_question, venture_context
    from app.idea.runner import _core_clarification, _known_fields
    from app.ventures.founder_facts import literal_facts

    answer = problem + " " + statement.format(audience=audience)
    snapshot = {"original_idea": opening, "project_intent": "startup",
        "understanding": {"offer": opening, "customer": previous},
        "answered_fields": {"customer": previous, "problem": answer},
        "conversation": [{"role": "founder", "field": "problem", "text": answer}],
        "answered_questions": [{"field": "problem", "founder_text": answer}]}
    parsed = core.answer_prefill(answer, "problem")
    assert parsed["customer"] == audience
    assert parsed["problem"] == problem
    known = _known_fields(snapshot)
    assert known["customer"] == audience and known["problem"] == problem
    facts = literal_facts(answer, source="clarification", field="problem")
    assert facts["audience"]["value"] == audience
    assert facts["problem"]["value"] == problem
    assert venture_context(snapshot)[0] == audience
    question = ground_question({"field": "requirements", "text": "What must it do?", "options": []}, snapshot)
    assert audience in question["text"]
    result = _core_clarification(snapshot, core.Clarification(case="know", action="proposal",
        message="Review your idea.", understanding=core.Understanding(customer=previous, problem=answer)))
    assert result.understanding.customer == audience and result.understanding.problem == problem
    assert snapshot["conversation"][0]["text"] == answer
    assert _also_answers("Thanks very much. " + statement.format(audience=audience), {"customer": audience}, "problem") == ""
    uncertain = "I do not know whether this is frequent or expensive enough to solve."
    assert _also_answers(uncertain + " " + statement.format(audience=audience), {"customer": audience}, "problem") == uncertain


@pytest.mark.parametrize("offer,audience", [
    ("A tool that checks delivery records", "compliance coordinators at regional warehouses"),
    ("An app that plans accessible rides", "retired cyclists with limited mobility"),
])
@pytest.mark.parametrize("statement", [
    "Actually, the buyer is {audience}, not a different group.",
    "The customers would be {audience}.",
    "Audience: {audience}",
    "Customer: For {audience}, you can review the results.",
    "{audience}",
])
def test_customer_statements_use_canonical_audience_in_saved_fields_and_questions(core, offer, audience, statement):
    from app.clarify.dilemmas import ground_question, venture_context
    from app.idea.runner import _core_clarification
    from app.intake.smart import _prefill

    answer = statement.format(audience=audience)
    assert core.founder_audience(answer) == audience
    assert core.answer_prefill(answer, "customer")["customer"] == audience
    assert _prefill("Audience: " + answer, explicit_only=True)["customer"] == audience
    snapshot = {"original_idea": offer, "project_intent": "startup",
        "answered_fields": {"offer": offer, "customer": answer},
        "conversation": [{"role": "founder", "field": "customer", "text": answer}]}
    result = _core_clarification(snapshot, core.Clarification(case="know", action="proposal",
        message="Review your idea.", understanding=core.Understanding(offer=offer, customer=answer)))
    assert result.understanding.customer == audience
    assert snapshot["conversation"][0]["text"] == answer
    assert venture_context(snapshot, clip_audience=False)[0] == audience
    question = ground_question({"field": "requirements", "text": "What must it do?", "options": []}, snapshot)
    assert audience in question["text"]
    if answer != audience:
        assert answer not in question["text"]


@pytest.mark.parametrize("offer,audience,workflow", [
    ("A tool that reconciles hospital access permissions", "Hospital IT teams with fewer than 20 staff",
     "review access requests and preserve approval records"),
    ("An app that tracks medication schedules", "Families managing prescriptions for older relatives",
     "record doses and check refill dates"),
])
def test_run_on_customer_answer_keeps_only_the_audience(core, offer, audience, workflow):
    from app.idea.runner import _core_clarification

    answer = (f"For {audience}, you can {workflow}, and I want a paid subscription "
              "after a trial, and anyone can read the public help page, and I have not interviewed users yet")
    snapshot = {"original_idea": offer, "project_intent": "startup",
        "answered_fields": {"offer": offer, "customer": answer},
        "conversation": [{"role": "founder", "field": "customer", "text": answer}]}
    result = _core_clarification(snapshot, core.Clarification(case="know", action="proposal",
        message="Review your idea.", understanding=core.Understanding(offer=offer, customer=audience)))
    assert result.understanding.customer == audience
    assert result.question is None or result.question.field != "customer"
    snapshot["conversation"].append({"role": "founder", "field": "workflow", "text": workflow})
    snapshot["understanding"] = result.understanding.model_dump()
    continued = _core_clarification(snapshot, core.Clarification(case="know", action="proposal",
        message="Review your idea.", understanding=core.Understanding(customer=answer)))
    assert continued.understanding.customer == audience
    assert core.founder_audience(audience + ", IT teams") == audience + ", IT teams"
    assert core.founder_audience("Everyone, including " + audience) == "Everyone, including " + audience


@pytest.mark.parametrize("offer,previous,audience", [
    ("A tool that reconciles hospital access permissions", "Hospital IT teams", "Hospital IT teams with fewer than 20 staff"),
    ("An app that tracks medication schedules", "Families", "Families managing prescriptions for older relatives"),
])
def test_explicit_run_on_audience_correction_is_not_lost_on_another_question(core, offer, previous, audience):
    from app.idea.runner import _core_clarification

    answer = (f"Audience: For {audience}, you can preserve the original records, and anyone can read the help page\n"
              "Workflow: You preserve the original records.")
    snapshot = {"original_idea": offer, "project_intent": "startup",
        "understanding": {"offer": offer, "customer": previous},
        "answered_fields": {"offer": offer, "customer": previous},
        "conversation": [{"role": "founder", "field": "workflow", "text": answer}]}
    result = _core_clarification(snapshot, core.Clarification(case="know", action="proposal",
        message="Review your idea.", understanding=core.Understanding(offer=offer, customer=previous)))
    assert result.understanding.customer == audience
    assert result.understanding.workflow == "You preserve the original records."
    assert snapshot["answered_fields"]["customer"] == previous
    snapshot["conversation"][-1]["reused"] = True
    reused = _core_clarification(snapshot, core.Clarification(case="know", action="proposal",
        message="Review your idea.", understanding=core.Understanding(offer=offer, customer=previous)))
    assert reused.understanding.customer == previous


@pytest.mark.parametrize("offer,previous,audience", [
    ("A tool that reconciles hospital access permissions", "Hospital IT teams", "Hospital IT teams with fewer than 20 staff"),
    ("An app that tracks medication schedules", "Families", "Families managing prescriptions for older relatives"),
])
def test_newer_saved_audience_survives_later_answers(core, offer, previous, audience):
    from app.idea.runner import _core_clarification

    snapshot = {"original_idea": offer, "project_intent": "startup",
        "understanding": {"offer": offer, "customer": previous},
        "answered_fields": {"offer": offer, "customer": previous},
        "founder_facts": {"audience": {"value": audience, "quote": audience, "source": "gstack", "confidence": .95}},
        "founder_fact_updated_at": {"audience": "2026-10-31T09:01:00+00:00"},
        "answered_field_updated_at": {"customer": "2026-10-31T09:00:00+00:00"},
        "conversation": [{"role": "founder", "field": "workflow", "text": "You preserve the original records."}]}
    def clarify():
        return _core_clarification(snapshot, core.Clarification(case="know", action="proposal",
            message="Review your idea.", understanding=core.Understanding(offer=offer, customer=previous)))
    assert clarify().understanding.customer == audience
    assert snapshot["answered_fields"]["customer"] == previous
    snapshot["answered_field_updated_at"] = {"audience": "2026-10-31T09:00:00+00:00"}
    assert clarify().understanding.customer == audience
    snapshot["founder_fact_updated_at"]["audience"] = "2026-10-31T04:01:00-05:00"
    assert clarify().understanding.customer == audience
    snapshot["founder_fact_updated_at"]["audience"] = "2026-10-31T08:59:00+00:00"
    assert clarify().understanding.customer == previous
    snapshot["founder_fact_updated_at"]["audience"] = "invalid"
    assert clarify().understanding.customer == previous
    snapshot["founder_fact_updated_at"]["audience"] = "2026-10-31T09:01:00+00:00"
    snapshot["founder_facts"]["audience"]["source"] = "website"
    assert clarify().understanding.customer == previous


@pytest.mark.parametrize("offer,audience,workflow", [
    ("A tool that reconciles hospital access permissions", "Hospital IT teams", "review permission changes"),
    ("An app that tracks medication schedules", "Families managing prescriptions", "record doses and check refill dates"),
])
def test_grounded_summary_survives_unrelated_model_field_corrections(core, offer, audience, workflow):
    from app.idea.runner import _core_clarification

    snapshot = {"original_idea": f"Offer: {offer}\nAudience: {audience}", "project_intent": "startup",
        "understanding": {"offer": offer, "customer": audience},
        "answered_fields": {"offer": offer, "customer": audience, "workflow": workflow},
        "conversation": [{"role": "founder", "field": "workflow", "text": workflow}]}
    wrong_offer, wrong_audience = "A different product for other buyers", "Unrelated retail managers"
    summary = f"{offer}. You {workflow}."
    for candidate, expected in ((summary, summary), (wrong_offer + ".", offer),
                                (summary + " Your users are " + wrong_audience + ".", offer)):
        result = _core_clarification(snapshot, core.Clarification(case="know", action="proposal",
            message="Review your idea.", understanding=core.Understanding(summary=candidate,
                offer=wrong_offer, customer=wrong_audience)))
        assert result.understanding.summary == expected
        assert result.understanding.offer == offer
        assert result.understanding.customer == audience
        assert result.understanding.workflow == workflow


@pytest.mark.parametrize("offer,audience,question,broad_question", [
    ("A shipment invoice matching tool", "Independent freight coordinators", "Which dispatch office would try this first?",
     "Which kind of office would try this first?"),
    ("A neighborhood walking app", "Families exploring nearby trails", "Which family could test this first?",
     "Which type of family could test this first?"),
])
def test_first_trial_question_survives_an_already_known_audience(core, offer, audience, question, broad_question):
    from app.idea.agent import answered_field
    from app.idea.runner import _core_clarification

    snapshot = {"original_idea": f"Offer: {offer}\nAudience: {audience}",
        "understanding": {"offer": offer, "customer": audience},
        "answered_fields": {"offer": offer, "customer": audience}}
    result = core.Clarification(case="know", action="question", message="Choose your first trial.",
        understanding=core.Understanding(offer=offer, customer=audience),
        question=core.Question(text=question, field="customer", why="Choose your first trial", options=[]))
    assert answered_field(question, "customer") == "first_user_path"
    assert answered_field(broad_question, "customer") == "customer"
    result = _core_clarification(snapshot, result)
    assert result.question is not None and result.question.text == question
    assert result.understanding.customer == audience


@pytest.mark.parametrize("offer,audience,contradiction,price,workflow", [
    ("A tool to reconcile shipment bills", "Independent freight coordinators", "Global retail procurement teams",
     "$24 per team per month", "Import delivery records and match invoices."),
    ("An app to organize neighborhood walks", "Families exploring nearby trails", "Professional expedition guides",
     "$6 per household per month", "Choose a nearby route and share it with family."),
])
def test_founder_audience_overrides_model_on_every_later_answer(core, offer, audience, contradiction, price, workflow):
    from app.idea.runner import _core_clarification

    snapshot = {"original_idea": f"Offer: {offer}\nAudience: {audience}", "project_intent": "startup",
        "understanding": {"offer": offer, "customer": audience, "summary": "An earlier model summary."},
        "answered_fields": {"customer": audience, "offer": offer},
        "conversation": [{"role": "founder", "field": "customer", "text": audience}]}
    for field, answer in (("pricing_hypothesis", price), ("workflow", workflow)):
        snapshot["answered_fields"][field] = answer
        snapshot["conversation"].append({"role": "founder", "field": field, "text": answer})
        summary = f"Your updated plan: {offer}. {answer}"
        result = core.Clarification(case="know", action="proposal", message="Review your idea.",
            understanding=core.Understanding(summary=summary, offer="A different service for " + contradiction, customer=contradiction))
        result = _core_clarification(snapshot, result)
        assert result.understanding.customer == audience
        assert result.understanding.offer == offer
        assert getattr(result.understanding, field) == answer
        assert result.understanding.summary == summary
        snapshot["understanding"] = result.understanding.model_dump()


@pytest.mark.parametrize("description,task", [
    ("I want a personal project for myself to track my garden watering, not a startup.", "track my garden watering, not a startup"),
    ("I want an internal tool for my team to reconcile supplier invoices.", "reconcile supplier invoices"),
])
def test_offline_personal_review_reuses_the_explicit_task(core, monkeypatch, description, task):
    from types import SimpleNamespace
    from app.idea import runner

    snapshot = {"project_intent": "personal", "original_idea": description, "understanding": {},
                "question_fields_asked": [], "answered_fields": {}}
    assert runner._known_fields(snapshot)["workflow"] == task
    saved = []
    monkeypatch.setattr(runner, "_receipt", lambda _: SimpleNamespace(snapshot=json.dumps(snapshot)))
    monkeypatch.setattr(runner, "_save", lambda _, **changes: saved.append(changes))
    runner._perform("personal-task", {})
    result = core.Clarification.model_validate_json(saved[-1]["response"])
    assert result.action == "proposal" and result.question is None
    assert result.understanding.workflow == task


@pytest.mark.parametrize("offer,workflow", [
    ("A private invoice board for my work", "I import invoices and mark them paid."),
    ("A private garden planner for my family", "I choose plants and schedule watering."),
])
@pytest.mark.parametrize("answered", [False, True])
@pytest.mark.parametrize("force_propose", [False, True])
def test_offline_personal_review_waits_for_a_known_task(core, monkeypatch, offer, workflow, answered, force_propose):
    from types import SimpleNamespace
    from app.idea import runner
    from app.idea.service import fallback_first_question

    understanding = {"offer": offer, **({"workflow": workflow} if answered else {})}
    snapshot = {"project_intent": "personal", "original_idea": offer,
        "understanding": understanding, "question_fields_asked": ["offer"],
        "answered_fields": understanding, "force_propose": force_propose}
    saved = []
    monkeypatch.setattr(runner, "_receipt", lambda _: SimpleNamespace(snapshot=json.dumps(snapshot)))
    monkeypatch.setattr(runner, "_save", lambda _, **changes: saved.append(changes))
    runner._perform("personal-attempt", {})
    result = core.Clarification.model_validate_json(saved[-1]["response"])
    assert saved[-1]["state"] == "responded"
    assert result.understanding.offer == offer
    if answered or force_propose:
        assert result.action == "proposal" and result.question is None
        assert result.understanding.workflow == (workflow if answered else "")
    else:
        assert result.action == "question" and result.question.field == "workflow"
        assert result.question.text == fallback_first_question("personal")


@pytest.mark.parametrize("background", ["I manage supplier invoices", "I enjoy hiking with my family"])
def test_no_idea_discovery_overrides_a_product_followup(core, background):
    from app.entry.chat_start import _onboarding_progress
    from app.intake.smart import BACKGROUND_QUESTION as DISCOVERY_QUESTION

    text = "I do not have an idea yet. " + background
    intake = {"path": "startup", "extracted": {}, "next_step": {"action": "ask", "field": "offer",
        "question": core.core_followup("offer", 0), "examples": []}}
    progress = _onboarding_progress(intake, text)
    assert progress["field"] == "context" and progress["question"] == DISCOVERY_QUESTION
    assert progress["completed"] == [] and progress["assumptions"] == []


@pytest.mark.parametrize("text", [
    "A library where teachers make lesson packs for their classrooms.",
    "A marketplace where illustrators create printable posters.",
])
def test_mechanism_is_not_limited_to_task_keywords(core, text):
    assert core.core_answer_status(text, 0, field="offer") == (True, False)


@pytest.mark.parametrize("text", [MARKETPLACE, MEALS, INVOICES])
def test_detailed_description_answers_offer_before_any_followup(core, text):
    from app.entry.chat_start import _onboarding_progress
    from app.intake.smart import classify_fallback

    assert core.core_answer_status(text, 0, field="offer") == (True, False)
    intake = classify_fallback(text).to_dict()
    progress = _onboarding_progress(intake, text)
    assert "offer" in progress["completed"]
    assert progress.get("field") != "offer"
    assert "What would it actually do?" not in progress.get("question", "")


@pytest.mark.parametrize("text", [MEALS, INVOICES])
def test_first_description_also_answers_customer(core, text):
    from app.entry.chat_start import _onboarding_progress
    from app.intake.smart import classify_fallback

    progress = _onboarding_progress(classify_fallback(text).to_dict(), text)
    assert progress["completed"] == ["offer", "customer"]
    assert not progress.get("question")


@pytest.mark.parametrize("text,field", [
    ("an AI app for everyone", "offer"), ("stuff", "context"), ("An app where people do things", "offer"),
])
def test_vague_description_still_needs_a_followup(core, text, field):
    from app.entry.chat_start import _onboarding_progress
    from app.intake.smart import classify_fallback

    assert core.core_answer_status(text, 0, field="offer", confidence=.99) == (False, False)
    progress = _onboarding_progress(classify_fallback(text).to_dict(), text)
    assert progress["field"] == field
    assert progress["completed"] == []
    assert progress["question"] == (core.core_followup("offer", 0) if field == "offer" else
                                    "Happy to help, what would you like to work on?")


@pytest.mark.parametrize("text", [MARKETPLACE, MEALS, INVOICES])
@pytest.mark.parametrize("field", ["offer", "workflow", "pricing_hypothesis"])
def test_question_selection_reads_earlier_founder_answers(core, text, field):
    from app.idea.runner import _core_clarification, _known_fields

    snapshot = {"original_idea": "A new product", "project_intent": "startup",
        "question_fields_asked": ["offer", "customer"],
        "conversation": [{"role": "founder", "text": text},
            {"role": "agent", "text": "What else matters?"},
            {"role": "founder", "text": "The first version should work on mobile."}]}
    known = _known_fields(snapshot)
    assert field in known
    assert known[field] in text
    result = core.Clarification(case="know", action="question", message="Saved",
        understanding=core.Understanding(), question=core.Question(
            text="Tell me about this part?", field=field, why="Understand this part", options=[]))
    result = _core_clarification(snapshot, result)
    assert not result.question or result.question.field != field
    assert getattr(result.understanding, field) == known[field]


def test_model_extraction_is_used_without_readiness_confidence(core):
    from app.idea.runner import _core_clarification, _known_fields

    text = "A seed exchange for community gardeners. Gardeners swap seeds with neighbours."
    result = core.Clarification(case="know", action="question", message="Saved", confidence=.4,
        understanding=core.Understanding(offer=text, customer="community gardeners"),
        question=core.Question(text=core.core_followup("offer", 0), field="offer", why="Task", options=[]))
    result = _core_clarification({"original_idea": text, "project_intent": "startup"}, result)
    assert not result.question or result.question.field != "offer"
    assert result.understanding.offer == text
    assert _known_fields({"answered_fields": {"offer": text}})["offer"] == text


def test_model_cannot_invent_a_missing_offer(core):
    from app.idea.runner import _core_clarification

    result = core.Clarification(case="know", action="proposal", message="Saved", confidence=.99,
        understanding=core.Understanding(offer="A marketplace where gardeners exchange seeds"))
    result = _core_clarification({"original_idea": "an AI app for everyone"}, result)
    assert result.question.field == "offer"
    assert result.understanding.offer == ""


def test_grounded_prefill_assessment_does_not_need_task_keywords(core):
    from app.idea.runner import _core_clarification

    offer = "A seed exchange for community gardeners"
    text = "Offer: " + offer + "\nAudience: Community gardeners"
    result = core.Clarification(case="know", action="question", message="Saved", confidence=.4,
        understanding=core.Understanding(), question=core.Question(
            text=core.core_followup("offer", 0), field="offer", why="Task", options=[]))
    result = _core_clarification({"original_idea": text, "prefill_facts": {"offer": {
        "value": offer, "quote": offer, "source": "founder message", "confidence": .95}}}, result)
    assert not result.question or result.question.field != "offer"
    assert result.understanding.offer == offer


def test_grounded_customer_assessment_does_not_need_a_role_keyword(core):
    from app.entry.chat_start import _onboarding_progress
    from app.intake.smart import classify_fallback

    text = "An app that books appointments. The first customer is legal aid."
    intake = classify_fallback(text).to_dict()
    intake["prefill_answers"]["customer"] = "legal aid"
    progress = _onboarding_progress(intake, text)
    assert progress["completed"] == ["offer", "customer"]
    assert not progress.get("question")


@pytest.mark.parametrize("field", ["customer", "workflow", "pricing_hypothesis"])
def test_explicit_correction_overrides_earlier_message(core, field):
    from app.idea.runner import _known_fields

    snapshot = {"original_idea": MEALS, "answered_fields": {field: ""},
        "conversation": [{"role": "founder", "text": MEALS}]}
    assert field not in _known_fields(snapshot)


@pytest.mark.parametrize("field", ["requirements", "constraints"])
@pytest.mark.parametrize("idea", ["Software reconciles purchase orders for retail buyers.", MEALS], ids=["b2b", "consumer"])
def test_grounded_list_answers_are_saved_and_not_reasked(core, field, idea):
    from app.idea.runner import _core_clarification, _known_fields, _understanding_summary

    answer = "Do not store health data." if field == "constraints" else "Export the shopping list."
    snapshot = {"original_idea": idea + " " + answer, "project_intent": "startup"}
    result = core.Clarification(case="know", action="question", message="Saved",
        understanding=core.Understanding(**{field: [answer]}), question=core.Question(
            text="Tell me about this part?", field=field, why="Understand this part", options=[]))
    result = _core_clarification(snapshot, result)
    assert_moves_past(result, field)
    assert getattr(result.understanding, field) == [answer]
    assert answer in _understanding_summary(result.understanding)
    assert _known_fields({**snapshot, "answered_fields": {field: [answer]}})[field] == [answer]


def test_fully_answered_fallback_offers_review_instead_of_repeating(core, monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from app.idea import runner
    from app.idea.service import REQUIRED
    from app.db.models import Venture, VentureDocument
    from app.db.supymem_models import ClarifierTurn, IdeaRevision, IdeaSession

    fields = dict(offer=MEALS, customer="busy parents", problem="Parents spend hours choosing meals.",
        workflow="Parents choose dinners, then make a shopping list.", pricing_hypothesis="Families pay $5 per month.",
        first_user_path="Invite parents from a school group.", desired_outcome="Plan meals for a week.",
        requirements=["Export the shopping list."], constraints=["Do not store health data."])
    text = "\n".join([*(f"{key.replace('_', ' ')}: {fields[key]}" for key in REQUIRED),
        *fields["requirements"], *fields["constraints"]])
    row = SimpleNamespace(id="ids_complete", workspace_id="wsp_complete", venture_id="ven_complete",
        state="clarifying", current_revision_id="rev_complete", questions_asked=0)
    revision = SimpleNamespace(id="rev_complete", rev=1, structured=json.dumps(fields))
    rows = {IdeaSession: row, IdeaRevision: revision, Venture: SimpleNamespace(raw_idea=text), VentureDocument: None}
    saved = []
    session = SimpleNamespace(get=lambda model, *args, **kwargs: rows[model],
        scalars=lambda *_: SimpleNamespace(all=lambda: []), add=saved.append, flush=lambda: None, commit=lambda: None)
    monkeypatch.setattr(runner, "db", lambda: lambda: nullcontext(session))
    monkeypatch.setattr(runner, "unresolved_clarification", lambda *args: False)
    monkeypatch.setattr(runner, "_speaker", lambda *args: {})
    monkeypatch.setattr(runner, "_manager_body", lambda speaker, body: body)
    monkeypatch.setattr(runner, "_reply_conversation_id", lambda *args: "cnv_complete")
    monkeypatch.setattr("app.idea.service._next_turn_number", lambda *args: 1)
    monkeypatch.setattr("app.idea.service._project_intent", lambda *args: "startup")
    monkeypatch.setattr("app.ventures.idea_page.schedule", lambda *args: None)
    monkeypatch.setattr("app.core.fsm.advance", lambda session, row, state: setattr(row, "state", state))
    assert runner.post_fallback_question({"session_id": row.id, "workspace_id": row.workspace_id,
        "venture_id": row.venture_id, "revision_id": row.current_revision_id})
    turn = next(item for item in saved if isinstance(item, ClarifierTurn))
    assert turn.kind == "proposal" and turn.options is None
    assert row.state == "reviewing" and row.questions_asked == 0


@pytest.mark.parametrize("text", [MEALS, INVOICES])
@pytest.mark.parametrize("field", ["offer", "customer"])
def test_model_next_step_cannot_repeat_a_known_field(core, text, field):
    from app.entry.chat_start import _onboarding_progress
    from app.intake.smart import PageContext, _validated_result, classify_fallback

    fallback = classify_fallback(text)
    intake = fallback.to_dict()
    intake["next_step"] = {"situation": "clear_idea", "action": "ask", "field": field,
        "question": "Tell me about this part?", "examples": ["One", "Two"]}
    progress = _onboarding_progress(intake, text)
    assert progress["completed"] == ["offer", "customer"]
    assert not progress.get("question")
    intake["path"] = "clarify"
    admitted = _validated_result(intake, text, PageContext(), fallback).to_dict()
    assert admitted["path"] == "startup"
    assert admitted["next_step"] is None
    assert admitted["choices"] == []
    assert _onboarding_progress(admitted, text)["completed"] == ["offer", "customer"]


@pytest.mark.parametrize("field", ["offer", "workflow", "pricing_hypothesis"])
def test_suggested_or_competitor_answers_are_not_founder_answers(core, field):
    from app.idea.runner import _known_fields

    snapshot = {"conversation": [
        {"role": "founder", "kind": "chip", "text": MEALS},
        {"role": "founder", "text": MEALS, "provenance": "suggested_choice",
            "suggested_text": MEALS, "founder_text": ""},
        {"role": "agent", "text": INVOICES}]}
    assert field not in _known_fields(snapshot)
    assert field not in _known_fields({"original_idea": "Competitor: " + MEALS})


def test_explicit_pivot_does_not_reuse_old_answers(core):
    from app.idea.runner import _known_fields

    known = _known_fields({"original_idea": MEALS, "conversation": [
        {"role": "founder", "text": "I want to change my idea. A new product for creative agencies."}]})
    assert "pricing_hypothesis" not in known
    assert "workflow" not in known


def test_rendered_model_fields_are_not_founder_testimony(core):
    from app.idea.runner import _core_clarification, _founder_messages

    rendered = "Offer: A marketplace where gardeners exchange seeds"
    snapshot = {"original_idea": rendered, "founder_idea": "an AI app for everyone"}
    assert _founder_messages(snapshot) == ["an AI app for everyone"]
    result = core.Clarification(case="know", action="proposal", message="Saved",
        understanding=core.Understanding(offer=rendered))
    result = _core_clarification(snapshot, result)
    assert result.question.field == "offer"
    assert result.understanding.offer == ""


@pytest.mark.parametrize("latest", [
    "It can reconcile shipment charges for freight coordinators.",
    "It can plan weekly family menus for parents planning dinners.",
])
@pytest.mark.parametrize("uncertain", [False, True])
@pytest.mark.parametrize("source", ["turn", "field"])
def test_durable_offer_answer_does_not_recover_the_raw_creation_offer(core, latest, uncertain, source):
    from app.idea.runner import _founder_messages, _known_fields
    previous = "It can catalogue artifacts for museum curators."
    latest = "not sure" if uncertain else latest
    snapshot = {"founder_idea": previous, "conversation": [{"role": "founder", "kind": "answer", "text": previous}],
        "question_fields_asked": ["offer"], "answered_questions": [
            {"field": "offer", "founder_text": latest, "answered_at": "2026-01-01T00:00:00+00:00"}]}
    if source == "field":
        snapshot.update(answered_questions=[], answered_fields={"offer": latest})
    assert _founder_messages(snapshot) == [previous, latest]
    if uncertain:
        assert "offer" not in _known_fields(snapshot)
        from app.idea.agent import Clarification, Understanding
        from app.idea.runner import _core_clarification
        snapshot["understanding"] = {"offer": previous}
        result = _core_clarification(snapshot, Clarification(case="know", action="proposal", message="Saved",
            understanding=Understanding(offer=previous)))
        assert result.understanding.offer == ""
    else:
        assert _known_fields(snapshot)["offer"] == latest


def test_discovery_request_is_not_a_product_answer(core):
    from app.idea.runner import _core_clarification, _known_fields

    request = "Suggest up to three tailored, unvalidated product directions for me to review."
    snapshot = {"original_idea": INVOICES, "conversation": [{"role": "founder", "text": request}]}
    assert _known_fields(snapshot)["offer"] == INVOICES
    assert "offer" not in _known_fields({"original_idea": request}, extracted={"offer": request})
    result = core.Clarification(case="explore", action="proposal", message="Saved", understanding=core.Understanding())
    result = _core_clarification(snapshot, result)
    assert result.action == "proposal"
    assert result.understanding.offer == INVOICES
    combined = request + "\n" + INVOICES
    assert INVOICES in _known_fields({"original_idea": combined})["offer"]


@pytest.mark.parametrize("text", [
    "A tool for small freight firms. People arranging deliveries at small freight firms need this",
    "A tool for small freight firms. Software engineering managers at small freight firms need this",
    "An app for home gardeners. App reviewing hobbyists need this",
])
def test_audience_description_does_not_invent_a_workflow(core, text):
    from app.intake.smart import _prefill

    assert "workflow" not in _prefill(text)


@pytest.mark.parametrize("payment", ["Families pay $100 for groceries.", "Can I charge $5 per month?"])
def test_other_payments_and_questions_are_not_platform_pricing(core, payment):
    from app.intake.smart import _prefill

    assert "pricing_hypothesis" not in _prefill("An app that plans weekly meals. " + payment)

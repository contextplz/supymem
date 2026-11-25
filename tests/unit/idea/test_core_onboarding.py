"""Core questions refine the saved answer, with bounded uncertainty."""
import json
from types import SimpleNamespace

import pytest

from tests.integration.test_postgres_auth_security import service_modules


@pytest.fixture(scope="module")
def core(tmp_path_factory):
    with pytest.MonkeyPatch.context() as setup, service_modules("company_os"):
        setup.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path_factory.mktemp('core-onboarding')}/core.db")
        setup.setenv("LLM_OFFLINE", "1")
        from app.idea import agent
        yield agent


def assert_moves_past(result, field):
    from app.core.local_mode import is_local
    from app.idea.question_history import question_field

    if is_local() and result.question:
        assert result.action == "question"
        assert question_field(result.question.model_dump()) != question_field({"field": field})
    else:
        assert result.action == "proposal" and result.question is None


@pytest.mark.parametrize("offer,buyer", [
    ("An inspection scanner for bridge cables that finds corrosion, sold to engineering firms to prevent failures.", "engineering firms"),
    ("A cleaning kit for vinyl records that removes dust, sold as a bundle to music collectors to protect each album.", "music collectors"),
])
def test_local_intake_prefills_the_named_buyer_not_the_product(core, monkeypatch, offer, buyer):
    from app.intake.smart import _prefill

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    assert _prefill(offer)["customer"] == buyer
    assert _prefill(f"Audience: Parents\nOffer: {offer}")["customer"] == "Parents"


@pytest.mark.parametrize("idea", [
    "An invoice tracking tool for independent cinemas",
    "A medication tracking tool for families",
])
def test_completed_offer_is_retained_without_model_inference(core, idea):
    from app.idea.runner import _core_clarification

    result = _core_clarification({"original_idea": idea, "project_intent": "startup",
        "question_fields_asked": ["offer", "customer"]},
        core.Clarification(case="know", action="proposal", message="Review your idea.", understanding=core.Understanding()))
    assert result.understanding.offer == idea
    assert result.action == "proposal" and result.question is None
    missing = _core_clarification({"original_idea": "A startup for " + idea.rsplit(" for ", 1)[1],
        "project_intent": "startup", "question_fields_asked": ["customer"]},
        core.Clarification(case="know", action="proposal", message="Review your idea.", understanding=core.Understanding()))
    assert missing.action == "question" and missing.question.field == "offer"
    vague = _core_clarification({"original_idea": "I want to launch an AI startup for " + idea.rsplit(" for ", 1)[1],
        "project_intent": "startup", "question_fields_asked": ["customer"]},
        core.Clarification(case="know", action="proposal", message="Review your idea.", understanding=core.Understanding()))
    assert vague.action == "question" and vague.question.field == "offer"


@pytest.mark.parametrize("offer", [
    "A tool for freight dispatch teams that shows each truck's next load",
    "An app for adults managing chronic conditions that records every symptom",
])
def test_offer_mechanism_accepts_quantified_objects_without_losing_uncertainty(core, offer):
    assert core.core_answer_status(offer, 0, field="offer") == (True, False)
    assert core.core_answer_status(offer + ". I'm not sure yet", 0, field="offer") == (True, True)


@pytest.mark.parametrize("idea,audience,qualified", [
    ("Software for municipal crews detecting water leaks from pressure readings, sold per depot per year.",
     "municipal crews", "inspecting water meters"),
    ("An app for balcony growers predicting frost damage from local forecasts.",
     "balcony growers", "maintaining seedlings"),
])
def test_product_shorthand_prefills_both_task_and_customer(core, idea, audience, qualified):
    from app.entry.chat_start import _onboarding_progress
    from app.idea.runner import _core_clarification
    from app.intake.smart import classify_fallback

    intake = classify_fallback(idea).to_dict()
    assert _onboarding_progress(intake, idea)["completed"] == ["offer", "customer"]
    assert intake["prefill_answers"]["offer"] == idea
    assert intake["prefill_answers"]["customer"] == audience
    phrased = "I want to build " + idea[0].lower() + idea[1:]
    phrased_intake = classify_fallback(phrased).to_dict()
    assert _onboarding_progress(phrased_intake, phrased)["completed"] == ["offer", "customer"]
    assert phrased_intake["prefill_answers"]["customer"] == audience
    detailed = f"An app for {audience} {qualified} that detects changes in daily readings."
    assert classify_fallback(detailed).prefill_answers["customer"] == f"{audience} {qualified}"
    result = _core_clarification({"original_idea": idea, "project_intent": "startup",
        "conversation": [{"role": "founder", "kind": "answer", "field": "offer", "text": "Skip"}]},
        core.Clarification(case="know", action="proposal", message="Review your idea.", understanding=core.Understanding()))
    assert result.understanding.customer == audience
    assert not result.question or result.question.field not in {"offer", "customer"}


@pytest.mark.parametrize("offer,audience", [
    ("A tool that reconciles shipment invoices", "Freight dispatch teams"),
    ("An app that plans weekend walking routes", "Families exploring local trails"),
])
def test_smalltalk_unsure_discovers_before_asking_customer(core, offer, audience):
    from app.entry.chat_start import _onboarding_progress
    from app.intake.smart import classify_fallback

    text = "hello\nClarification: Not sure"
    progress = _onboarding_progress(classify_fallback(text).to_dict(), text)
    assert progress["field"] == "context"
    assert len(progress["choices"]) == 3
    text += "\nClarification: " + offer
    progress = _onboarding_progress(classify_fallback(text).to_dict(), text)
    assert progress["field"] == "customer"
    text += "\nAudience: " + audience
    assert not _onboarding_progress(classify_fallback(text).to_dict(), text).get("question")
    discovery = "I don't have an idea yet"
    assert _onboarding_progress(classify_fallback(discovery).to_dict(), discovery)["field"] == "context"


@pytest.mark.parametrize("offer,audience", [
    ("A tool that tracks welding equipment", "Independent fabrication shop owners"),
    ("An app that tracks knitting patterns", "People knitting gifts at home"),
])
def test_uncertain_audience_after_smalltalk_keeps_discovery_open(core, offer, audience):
    from app.entry.chat_start import _onboarding_progress
    from app.intake.smart import classify_fallback

    text = f"hello\nClarification: {offer}\nAudience: I do not know yet"
    progress = _onboarding_progress(classify_fallback(text).to_dict(), text)
    assert progress["completed"] == ["offer"] and progress["field"] == "customer"
    assert progress["question"] and progress["choices"] == [] and progress["assumptions"] == []
    text += f"\nAudience: {audience}"
    assert not _onboarding_progress(classify_fallback(text).to_dict(), text).get("question")
    text = f"hello\nClarification: {offer}\nAudience: everyone\nAudience: people"
    progress = _onboarding_progress(classify_fallback(text).to_dict(), text)
    assert progress["field"] == "customer" and progress["completed"] == ["offer"]


@pytest.mark.parametrize("audience,task,customer_request", [
    ("repair shops", "compare damaged bicycle parts", "find me repair shops in Bristol I can email"),
    ("families", "choose local swimming lessons", "find me parents in Wellington I can email"),
])
def test_help_task_clauses_do_not_finish_audience_onboarding(core, monkeypatch, audience, task, customer_request):
    from app.entry import chat_start
    from app.intake.smart import _prefill, classify_fallback

    monkeypatch.setattr(chat_start, "_assert_can_start", lambda *_: None)
    monkeypatch.setattr(chat_start, "record_preview", lambda *_: None)
    text = f"I want to build a startup that helps {audience} {task}"
    assert _prefill(text).get("customer") != f"{audience} {task}"
    progress = chat_start._onboarding_progress(classify_fallback(text).to_dict(), text)
    assert progress["field"] == "customer" and progress["question"]
    text += f"\nClarification: {customer_request}"
    reply = chat_start.preview(text=text, actor_id="founder", intake=classify_fallback(text).to_dict())
    proposal = reply.get("proposal") or reply
    assert proposal["route"] == proposal["next_step"] == "gtm" and not proposal.get("onboarding_question")


@pytest.mark.parametrize("offer,audience", [
    ("A tool to reconcile shipment bills", "Freight coordinators"),
    ("An app to plan weekly meals", "Parents planning dinners"),
])
def test_readiness_uses_known_fields_and_preserves_corrections_and_uncertainty(core, offer, audience):
    from app.idea.runner import _core_clarification, _enough_answers, _known_fields
    snapshot = {"project_intent": "startup", "questions_asked": 0,
        "original_idea": f"Offer: {offer}\nAudience: {audience}"}
    assert _enough_answers(snapshot)
    assert not _enough_answers({"project_intent": "startup", "questions_asked": 20})
    correction = "An internal tool to review plans for my own team"
    snapshot["understanding"] = {"offer": correction}
    assert _known_fields(snapshot)["offer"] == correction
    snapshot["answered_fields"] = {"customer": ""}
    snapshot["website_answers"] = {"customer": {"answer": audience, "source": "From your website", "status": "answered"}}
    assert not _enough_answers(snapshot)
    snapshot["question_fields_asked"] = ["offer", "customer"]
    snapshot["founder_facts"] = {"audience": {"value": audience, "quote": audience, "source": "clarification", "confidence": .95}}
    result = core.Clarification(case="know", action="question", message="Saved", understanding=core.Understanding(),
        question=core.Question(text="Who is it for?", field="customer", why="Audience", options=[]))
    assert _core_clarification(snapshot, result).question is not None
    snapshot = {"project_intent": "startup", "questions_asked": 20,
        "understanding": {"offer": offer, "customer": audience, "assumptions": ["Assumed: " + audience]}}
    assert not _enough_answers(snapshot)
    snapshot["understanding"]["assumptions"] = []
    snapshot["prefill_facts"] = {"customer": {"value": audience, "quote": audience, "source": "website", "confidence": .95}}
    assert not _enough_answers(snapshot)
    snapshot["understanding"]["customer"] = audience + " in a nearby town"
    assert not _enough_answers(snapshot)
    snapshot["answered_fields"] = {"offer": offer, "customer": audience, "workflow": "Create a plan, review it, then save it"}
    result = core.Clarification(case="know", action="question", message="Saved", understanding=core.Understanding(),
        question=core.Question(text="What is the workflow?", field="workflow", why="Workflow", options=[]))
    updated = _core_clarification(snapshot, result)
    assert _enough_answers(snapshot)
    assert_moves_past(updated, "workflow")
    assert updated.understanding.workflow == snapshot["answered_fields"]["workflow"]


@pytest.mark.parametrize("offer,audience", [
    ("A tool to schedule equipment maintenance", "Repair shop operators"),
    ("An app to plan stargazing outings", "Astronomy hobbyists"),
])
def test_rendered_prefills_do_not_turn_assumptions_into_known_answers(core, offer, audience):
    from app.idea.runner import _core_clarification, _enough_answers, _known_fields

    for field, value in (("customer", audience), ("offer", offer)):
        snapshot = {"project_intent": "startup", "questions_asked": 0,
            "original_idea": f"Customer: {audience}\nOffer: {offer}",
            "understanding": {"offer": offer, "customer": audience, "assumptions": ["Assumed: " + value]}}
        assert field not in _known_fields(snapshot)
        assert not _enough_answers(snapshot)
        result = core.Clarification(case="know", action="question", message="Clarify this part",
            understanding=core.Understanding.model_validate(snapshot["understanding"]),
            question=core.Question(text="Who is it for?" if field == "customer" else "What are you building?",
                field=field, why="Clarify the idea", options=[]))
        updated = _core_clarification(snapshot, result)
        assert updated.question is not None and updated.question.field == field
        snapshot["answered_fields"] = {field: value}
        assert _known_fields(snapshot)[field] == value
        assert _enough_answers(snapshot)


@pytest.mark.parametrize("offer,audience", [
    ("A tool to plan maintenance schedules", "Repair shop operators"),
    ("An app to plan shopping lists", "Home cooks"),
])
def test_offer_labels_do_not_supply_a_buyer_or_override_an_empty_answer(core, offer, audience):
    from app.idea.audience import stated_buyer
    from app.idea.runner import _known_fields

    source = f"Offer: {offer}\nAudience: {audience}"
    assert not stated_buyer(source)
    assert _known_fields({"original_idea": source})["customer"] == audience
    assert "customer" not in _known_fields({"original_idea": source, "answered_fields": {"customer": ""}})


@pytest.mark.parametrize("workaround,problem,audience,offer", [
    ("Dispatchers use a shared spreadsheet", "Shipment charges require duplicate entry", "Freight coordinators", "A tool to reconcile shipment bills"),
    ("Families use a paper meal calendar", "Weekly menus take hours to plan", "Parents planning dinners", "An app to plan weekly meals"),
])
def test_known_facts_match_question_intent_without_confusing_evidence_or_product_workflow(core, workaround, problem, audience, offer):
    from app.idea.runner import _core_clarification, _enough_answers
    fact = {"value": workaround, "quote": workaround, "source": "clarification", "confidence": .95}
    snapshot = {"question_fields_asked": ["offer", "customer"], "founder_facts": {"current_workaround": fact}}
    result = core.Clarification(case="know", action="question", message="Saved", understanding=core.Understanding(),
        question=core.Question(text="What do they use today when this comes up?", field="workflow", why="Current workaround", options=[]))
    updated = _core_clarification(snapshot, result)
    assert updated.question is None and updated.understanding.workflow == ""
    snapshot["founder_facts"] = {"problem": {**fact, "value": problem, "quote": problem}}
    result = core.Clarification(case="know", action="question", message="Saved", understanding=core.Understanding(),
        question=core.Question(text="What evidence of demand do you have?", field="problem", why="Evidence", options=[]))
    assert _core_clarification(snapshot, result).question is not None
    snapshot["founder_facts"] = {}
    snapshot["prefill_facts"] = {"workflow": {**fact, "source": "website"}}
    snapshot["original_idea"] = "Workflow: " + workaround
    result = core.Clarification(case="know", action="question", message="Saved", understanding=core.Understanding(),
        question=core.Question(text="How does your product work?", field="workflow", why="Workflow", options=[]))
    updated = _core_clarification(snapshot, result)
    assert updated.question is not None and workaround in updated.question.text
    assert updated.understanding.workflow == ""
    snapshot.update(project_intent="startup", understanding={"offer": offer, "customer": audience})
    assert _enough_answers(snapshot)
    assert not _enough_answers(snapshot, updated.question)


@pytest.mark.parametrize("field,answer,followups,expected", [
    ("offer", "A tool to organize meal plans", 0, (True, False)),
    ("offer", "A tool for structural engineers to catch errors in construction drawings before tender", 0, (True, False)),
    ("offer", "Software for accountants to reconcile bank statements", 0, (True, False)),
    ("customer", "Independent tutors", 0, (True, False)),
    ("offer", "an AI app", 0, (False, False)),
    ("offer", "I want to build a startup for independent bookshops", 0, (False, False)),
    ("offer", "A tool for structural engineers", 0, (False, False)),
    ("offer", "A tool for engineers to use", 0, (False, False)),
    ("customer", "everyone", 0, (False, False)),
    ("offer", "not sure", 0, (True, True)),
    ("customer", "idk", 0, (True, True)),
    ("customer", "skip", 0, (True, True)),
    ("customer", "Skip this question", 0, (True, True)),
    ("offer", "something", 1, (False, False)),
    ("offer", "something", 2, (True, True)),
    ("customer", "people", 2, (True, True)),
])
def test_advance_rule(core, field, answer, followups, expected):
    assert core.core_answer_status(answer, followups, field=field) == expected


def test_model_clarity_and_uncertainty(core):
    assert core.is_unsure_answer("idk\nNot sure")
    assert not core.is_unsure_answer("Not sure\nTwo gym owners asked me for it")
    assert core.core_answer_status("short", 0, field="offer", confidence=0.9) == (True, False)
    assert core.core_answer_status("A tool to organize meal plans", 0, field="offer", confidence=0.4) == (True, False)
    assert core.core_answer_status("not sure", 0, field="offer", confidence=0.9) == (True, True)


@pytest.mark.parametrize("idea,options", [
    ("Software for warehouse operators", ["Reconcile missing inventory", "Plan loading bays", "Assign picking routes"]),
    ("An app for home gardeners", ["Plan balcony planting", "Diagnose leaf damage", "Schedule seasonal watering"]),
])
def test_onboarding_options_use_the_existing_metered_intake_call(core, monkeypatch, idea, options):
    import asyncio
    from app.credits import metered
    from app.entry.chat_start import _onboarding_progress
    from app.intake import smart
    from app.clarify.dilemmas import dilemmas_for_idea, suggested_answers
    from app.tenancy import current
    from app.workspaces.resolve import LOCAL_DESK_ID

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    calls = []
    data = smart.classify_fallback(idea).to_dict()
    data["onboarding_options"] = {"offer": options}

    async def model(messages, **kwargs):
        scope = current()
        assert (scope.workspace_id, scope.venture_id, scope.actor) == (LOCAL_DESK_ID, "", "founder")
        calls.append((messages, kwargs))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(data)))])

    monkeypatch.setattr(metered, "llm", model)
    result = asyncio.run(smart.classify(idea, actor_id="founder", workspace_id=LOCAL_DESK_ID, request_id="choice-request"))
    assert len(calls) == 1 and calls[0][1]["purpose"] == "intake.classify"
    assert calls[0][1]["max_tokens"] == 900
    assert calls[0][1]["workspace_id"] == LOCAL_DESK_ID and calls[0][1]["request_id"] == "choice-request"
    assert idea in calls[0][0][1]["content"]
    assert _onboarding_progress(result.to_dict(), idea)["choices"] == options
    assert all(idea.casefold() not in option.casefold() for option in suggested_answers(idea, "offer"))
    assert all(idea.casefold() not in option.casefold() for option in suggested_answers(idea, "offer", ["Consider: " + idea]))
    questions = dilemmas_for_idea(idea)
    labels = [option["label"] for question in questions[:2] for option in question["options"]]
    assert all(idea.casefold() not in label.casefold() for label in labels)


@pytest.mark.parametrize("idea,audience,problem", [
    ("A tool to reconcile shipment invoices", "Transport operators", "Missing payment records"),
    ("An app to plan weekend meals", "Home cooks", "Repeated shopping trips"),
])
def test_known_text_and_website_answers_are_not_asked_again(core, idea, audience, problem):
    from app.idea.runner import _core_clarification
    for source in ("text", "website", "history", "saved"):
        known = {"offer": idea, "customer": audience, "problem": problem}
        snapshot = {"original_idea": idea + f"\nAudience: {audience}\nProblem: {problem}" if source == "text" else "",
                    "understanding": known if source == "saved" else {}, "website_answers": known if source == "website" else {},
                    "question_fields_asked": ["problem"] if source == "history" else [],
                    "founder_facts": {"problem": {"value": problem, "source": "research", "confidence": .7}} if source == "history" else {}}
        result = core.Clarification(case="know", action="question", message="Let's clarify this.",
            understanding=core.Understanding(), question=core.Question(
                field="problem", text="What problem should this solve?", why="Understand the problem", options=[]))
        refined = _core_clarification(snapshot, result)
        assert_moves_past(refined, "problem")
        if source != "history":
            assert refined.understanding.customer == audience
    incomplete = f"Software for {audience}"
    result = core.Clarification(case="know", action="proposal", message="Let's review this.", understanding=core.Understanding())
    refined = _core_clarification({"original_idea": incomplete, "understanding": {"summary": incomplete, "customer": audience},
        "question_fields_asked": []}, result)
    assert refined.question.field == "offer" and refined.understanding.customer == audience


@pytest.mark.parametrize("name,description", [
    ("CargoLedger", "a tool for transport operators to reconcile shipment invoices"),
    ("GardenNest", "an app for home gardeners to plan balcony planting"),
])
def test_greeting_build_declaration_prefills_name_without_confirming(core, name, description):
    from app.entry import chat_start
    from app.intake.smart import classify_fallback
    for prefix in ("Hi, i am building ", "Hello, I'm building ", "We are building "):
        text = prefix + name + ", " + description
        assert chat_start._stated_name(text) == name
        assert chat_start._stated_name(prefix + name + " and I need help with " + description) == name
        preview = chat_start.preview(text=text, actor_id="founder", intake=classify_fallback(text).to_dict())
        proposal = preview.get("proposal") or chat_start.get(preview["id"], "founder")["proposal"]
        assert proposal["venture_name"] == name and proposal["state"] in {"pending", "clarify"}
        assert proposal["receipt"] is None
        assert chat_start.get(proposal["id"], "founder")["proposal"]["venture_name"] == name
    assert chat_start._stated_name("Hi, I am building a tool for " + description) is None


@pytest.mark.parametrize("prefix,generic,description,name", [
    ("We're creating ", "platform", "that reconciles purchase orders for hotel procurement managers", "OrderCove"),
    ("I'm launching ", "service", "that records trail conditions for weekend hikers", "TrailPocket"),
], ids=["b2b", "consumer"])
def test_generic_product_description_does_not_suggest_a_venture_name(core, prefix, generic, description, name):
    from app.entry import chat_start
    from app.intake import naming
    from app.intake.smart import classify_fallback

    opening = prefix + generic + " " + description + "."
    assert chat_start._stated_name(opening) is None
    assert chat_start._stated_name(generic.title() + ": " + description) is None
    assert not naming.is_valid_name(generic)
    preview = chat_start.preview(text=opening, actor_id="founder", intake=classify_fallback(opening).to_dict())
    proposal = preview.get("proposal") or chat_start.get(preview["id"], "founder")["proposal"]
    assert not proposal["venture_name"]
    assert chat_start._stated_name(prefix + name + ", " + description + ".") == name


@pytest.mark.parametrize("domain,name,description", [
    ("invoice-harbor.io", "Invoice Harbor", "a tool to reconcile supplier invoices"),
    ("pantry-bloom.app", "Pantry Bloom", "an app to save recipes and plan dinners"),
])
def test_owned_domain_prefills_name_after_skipping_a_question(core, domain, name, description):
    from app.entry import chat_start
    from app.intake.smart import classify_fallback

    text = f"I run {domain}, {description}."
    assert chat_start._stated_name(text) == name
    assert chat_start._stated_name(f"I run {name}, {description} at {domain}.") == name
    for suffix in ("", "\nClarification: Skip this question"):
        source = text + suffix
        result = chat_start.preview(text=source, actor_id="founder", intake=classify_fallback(source).to_dict())
        proposal = result.get("proposal") or chat_start.get(result["id"], "founder")["proposal"]
        assert proposal["venture_name"] == name
        assert chat_start.get(proposal["id"], "founder")["proposal"]["venture_name"] == name
        if result["status"] == "clarify":
            assert result["venture_name"] == name


def test_concrete_task_answer_does_not_get_another_example_question(core):
    from app.entry.chat_start import _onboarding_progress
    from app.idea.runner import _core_clarification
    from app.intake.smart import classify_fallback

    for idea, task in [
        ("A tool for fleet operators", "It compares fuel receipts against vehicle mileage."),
        ("An app for home gardeners", "It converts balcony measurements into planting layouts."),
    ]:
        initial = _onboarding_progress(classify_fallback(idea).to_dict(), idea)
        assert initial["field"] == "offer"
        text = idea + "\nClarification: " + task
        progress = _onboarding_progress(classify_fallback(text).to_dict(), text)
        assert "offer" in progress["completed"] and progress.get("field") != "offer"
        result = core.Clarification(case="know", action="question", confidence=0.2,
            understanding=core.Understanding(), message="Let's clarify your idea.",
            question=core.Question(text=core.core_followup("offer", 1), field="offer", why="Clarify the task", options=[]))
        updated = _core_clarification({"original_idea": idea, "active_core_field": "offer",
            "question_fields_asked": ["offer"], "core_question_counts": {"offer": 1},
            "conversation": [{"role": "founder", "text": task}]}, result)
        assert updated.question is None or updated.question.field != "offer"
        assert updated.understanding.offer == task
    assert core.core_answer_status("It does something useful", 1, field="offer") == (False, False)


@pytest.mark.parametrize("audience,task,contact,excluded", [
    ("repair shop managers", "coordinate repair estimates", "Lena Ortiz at Willow Repairs", "home mechanics"),
    ("parents planning weekend trips", "arrange accessible outings", "Nolan Reed at Cedar Walking Club", "tour operators"),
])
def test_offer_answer_promotes_explicit_customer_without_losing_context(core, audience, task, contact, excluded):
    from app.entry.chat_start import _onboarding_progress

    intake = {"path": "startup", "extracted": {"idea": "A tool", "language": "en"}}
    assert _onboarding_progress(intake, "A tool")["field"] == "offer"
    answer = (f"You would help {audience} {task} without making decisions for them. "
              f"{contact} is the kind of customer: {audience}, not {excluded}.")
    accepted = {}
    progress = _onboarding_progress(intake, "A tool\nClarification: " + answer, accepted_answers=accepted)
    assert progress == {"completed": ["offer", "customer"], "assumptions": []}
    assert accepted == {"offer": answer, "customer": audience}
    unrelated = f"It compares dates against available time slots. You saw {contact} at a conference."
    progress = _onboarding_progress(intake, "A tool\nClarification: " + unrelated)
    assert progress["field"] == "customer" and progress["completed"] == ["offer"]
    progress = _onboarding_progress(intake, "A tool\nClarification: Assumed: " + answer)
    assert "customer" not in progress["completed"]


def test_explicit_continue_advances_each_core_step_with_or_without_a_draft(core):
    from app.entry import chat_start
    from app.intake.smart import classify_fallback

    for idea in ["A dispatch dashboard", "An outdoor hobby app"]:
        for draft in ["", "I still need to work out the details"]:
            answer = "\n".join(filter(None, [draft, "Skip this question"]))
            text = idea + "\nClarification: " + answer
            progress = chat_start._onboarding_progress(classify_fallback(text).to_dict(), text)
            assert progress["completed"] == ["offer"] and progress["field"] == "customer"
            assert core.core_answer_status(answer, 0, field="offer") == (True, True)
            text += "\nAudience: " + answer
            intake = classify_fallback(text).to_dict()
            progress = chat_start._onboarding_progress(intake, text)
            assert progress["completed"] == ["offer", "customer"]
            assert not progress.get("question")
            saved = chat_start.preview(text=text, actor_id="founder", intake=intake)
            assert saved["status"] == "proposal" and not saved["proposal"].get("onboarding_question")
            assert not chat_start.get(saved["proposal"]["id"], "founder")["proposal"].get("onboarding_question")


def test_generated_choice_shortens_only_at_word_boundaries(core):
    from app.clarify.dilemmas import _noun_phrase, suggested_answers

    for text in [
        "Software for independent logistics operators reconciling international freight invoices every week",
        "An app for neighbourhood gardeners exchanging seasonal planting notes and sharing balcony designs",
    ]:
        phrase = _noun_phrase(text)
        assert len(phrase) <= 80 and phrase.endswith("...")
        original = text.removeprefix("An ")
        prefix = phrase.removesuffix("...")
        assert original.startswith(prefix + " ")
        assert all(phrase not in option for option in suggested_answers(text, "offer"))


def test_greeting_first_audience_clarification_is_bounded_and_explicit_skip_advances(core):
    from app.entry.chat_start import _onboarding_progress
    from app.intake.smart import classify_fallback

    for idea in ["A tool that reconciles payroll deductions", "An app that plans hiking routes"]:
        text = "Good morning\nClarification: " + idea
        unsure = text + "\nAudience: Not sure"
        progress = _onboarding_progress(classify_fallback(unsure).to_dict(), unsure)
        assert progress["completed"] == ["offer"] and progress["field"] == "customer"
        assert progress["choices"] == []
        for advanced in [unsure + "\nAudience: Not sure", text + "\nAudience: Skip this question",
                         idea + "\nAudience: Not sure"]:
            progress = _onboarding_progress(classify_fallback(advanced).to_dict(), advanced)
            assert progress["completed"] == ["offer", "customer"]
            assert not progress.get("question") and progress["assumptions"]


def test_greeting_choices_stay_available_without_duplicating_onboarding_options(core):
    from app.entry import chat_start
    from app.intake.smart import classify_fallback

    for text in ["Good morning", "Good morning\nClarification: how are you",
                 "Good morning\nClarification: New idea", "A tool that reconciles payroll deductions",
                 "An app that plans hiking routes"]:
        intake = classify_fallback(text).to_dict()
        intake.update(path="clarify", confidence=0.4)
        result = chat_start.preview(text=text, actor_id="founder", intake=intake)
        assert result["status"] == "clarify"
        assert not (result["choices"] and result["onboarding_choices"])
        if intake["extracted"].get("idea"):
            assert result["choices"] == result["onboarding_choices"] == []
            assert "choices" not in chat_start.get(result["id"], "founder")["proposal"]
        else:
            assert result["choices"] == []
            assert result["onboarding_choices"] == intake["choices"] and len(result["onboarding_choices"]) == 3
        assert chat_start.cancel(result["id"], "founder")["status"] == "cancelled"


def test_only_unbound_questions_without_context_keep_the_legacy_free_text_reply(core):
    from app.idea.runner import _question_reply

    for question, moment in [("How would payroll teams use this?", "When do payroll teams feel most frustrated?"),
                             ("How would hikers use this?", "When do hikers feel most frustrated?")]:
        assert _question_reply(question, {"options": []}) == (question + "\n\nReply here with your answer.", None)
        options = {"options": []}
        body, actions = _question_reply(question, options, "saved-question")
        assert "Type your own answer" in body
        assert len(actions) == 1 and actions[0]["payload"] == {"text": "Skip the questions", "question_id": "saved-question"}
        assert options["options"] == [] and options["free_text_only"]
        body, actions = _question_reply(moment, {"options": []})
        assert "Type your own answer" in body and actions is None


@pytest.mark.parametrize("text,clear", [
    ("TinyPlate QA: a meal-planning app for busy US parents of picky toddlers that suggests weekly dinners their kids will actually eat, $5/month", True),
    ("An app for parents that recommends toddler recipes", True),
    ("An app for parents that plans weekly meals", True),
    ("I want to build a startup for busy US parents", False),
    ("An app that suggests things", False),
])
def test_u22_entry_skips_task_question_only_for_a_concrete_task(core, text, clear):
    from app.entry.chat_start import _onboarding_progress
    from app.intake.smart import classify_fallback
    progress = _onboarding_progress(classify_fallback(text).to_dict(), text)
    assert ("offer" in progress["completed"]) is clear
    if clear:
        assert progress.get("field") != "offer"
        assert core.core_answer_status(text, 0, field="offer") == (True, False)
    else:
        assert progress["question"] == core.core_followup("offer", 0)


@pytest.mark.parametrize("text,audience", [
    ("A tool for structural engineers to catch errors in construction drawings before tender", "structural engineers"),
    ("Software for accountants to reconcile bank statements", "accountants"),
    ("I want to build a tool for structural engineers to catch errors in construction drawings before tender", "structural engineers"),
])
def test_clear_offer_and_audience_skip_core_questions(core, text, audience):
    from app.entry.chat_start import _onboarding_progress
    from app.intake.smart import classify_fallback
    intake = classify_fallback(text)
    assert intake.path == "startup"
    assert "offer" not in intake.prefill_answers
    assert intake.prefill_answers["customer"] == audience
    assert _onboarding_progress(intake.to_dict(), text) == {"completed": ["offer", "customer"], "assumptions": []}


def test_u64_sell_to_audience_is_reused_by_intake_and_runner(core):
    from app.entry.chat_start import _onboarding_progress
    from app.intake.smart import classify_fallback, _prefill
    from app.idea.runner import _core_clarification
    text = "ShiftSwap helps managers stop spending Sunday nights fixing the rota over text. We sell to independent restaurant owners in the US."
    audience = "independent restaurant owners in the US"
    assert _prefill(text)["customer"] == audience
    assert "customer" in _onboarding_progress(classify_fallback(text).to_dict(), text)["completed"]
    result = core.Clarification(case="know", action="question", message="Saved", understanding=core.Understanding(),
        question=core.Question(text="Who is it for?", field="customer", why="Audience", options=[]))
    saved = _core_clarification({"original_idea": text, "question_fields_asked": ["offer"]}, result)
    assert_moves_past(saved, "customer")
    assert saved.understanding.customer == audience


@pytest.mark.parametrize("offer,completed,website,field", [
    ("AI app", [], {}, "offer"),
    ("AI app", [], {"customer": "Structural engineers"}, "offer"),
    ("A tool to review drawings", [], {}, "customer"),
    ("A tool to review drawings", ["offer"], {}, "customer"),
    ("A tool to review drawings", ["offer", "customer"], {}, "pricing_hypothesis"),
])
def test_rejected_turn_fallback_asks_the_missing_core_slot(core, monkeypatch, offer, completed, website, field):
    import json
    from contextlib import nullcontext
    from types import SimpleNamespace
    from app.idea import runner
    from app.db.models import Venture, VentureDocument
    from app.db.supymem_models import ClarifierTurn, IdeaRevision, IdeaSession
    saved = []
    row = SimpleNamespace(id="ids_fallback", workspace_id="wsp_fallback", venture_id="ven_fallback",
        state="clarifying", current_revision_id="rev_fallback", questions_asked=0)
    revision = SimpleNamespace(id="rev_fallback", rev=1, structured=json.dumps({"_onboarding_completed": completed, "_website_answers": website}))
    rows = {IdeaSession: row, IdeaRevision: revision, Venture: SimpleNamespace(raw_idea=offer), VentureDocument: None}
    session = SimpleNamespace(get=lambda model, *args, **kwargs: rows[model],
        scalars=lambda *_: SimpleNamespace(all=lambda: []),
        add=saved.append, flush=lambda: None, commit=lambda: None)
    monkeypatch.setattr(runner, "db", lambda: lambda: nullcontext(session))
    monkeypatch.setattr(runner, "unresolved_clarification", lambda *args: False)
    monkeypatch.setattr(runner, "_speaker", lambda *args: {})
    monkeypatch.setattr(runner, "_manager_body", lambda speaker, body: body)
    monkeypatch.setattr(runner, "_reply_conversation_id", lambda *args: "cnv_fallback")
    monkeypatch.setattr("app.idea.service._next_turn_number", lambda *args: 1)
    monkeypatch.setattr("app.idea.service._project_intent", lambda *args: "startup")
    monkeypatch.setattr("app.ventures.idea_page.schedule", lambda *args: None)
    assert runner.post_fallback_question({"session_id": row.id, "workspace_id": row.workspace_id,
        "venture_id": row.venture_id, "revision_id": row.current_revision_id})
    turn = next(item for item in saved if isinstance(item, ClarifierTurn))
    assert json.loads(turn.options)["field"] == field
    assert json.loads(turn.options)["options"] == [] and json.loads(turn.options)["free_text_only"]
    expected = "How would you charge for it?" if field == "pricing_hypothesis" else core.core_followup(field, 0)
    assert turn.text == expected
    assert turn.text in saved[-1].body_md
    assert "Type your own answer" in saved[-1].body_md
    actions = json.loads(saved[-1].actions_json)
    assert len(actions) == 1 and actions[0]["payload"] == {"text": "Skip the questions", "question_id": turn.id}


def test_entry_refines_each_question_and_persists_assumptions(core, monkeypatch):
    from app.entry import chat_start
    from app.intake.smart import classify_fallback
    monkeypatch.setattr(chat_start, "_assert_can_start", lambda *_: None)
    monkeypatch.setattr(chat_start, "record_preview", lambda *_: None)
    at = chat_start.utcnow()
    monkeypatch.setattr(chat_start, "utcnow", lambda: at)
    texts = ["AI app", "AI app\nClarification: something", "AI app\nClarification: something\nClarification: anything",
             "AI app\nClarification: something\nClarification: anything\nAudience: everyone",
             "AI app\nClarification: something\nClarification: anything\nAudience: everyone\nAudience: people",
             "AI app\nClarification: something\nClarification: anything\nAudience: everyone\nAudience: people\nAudience: idk"]
    for index, text in enumerate(texts):
        result = chat_start.preview(text=text, actor_id="founder", request_id=str(index), intake=classify_fallback(text).to_dict())
        saved = chat_start.get(result.get("id") or result["proposal"]["id"], "founder")["proposal"]
        expected = "offer" if index < 2 else "customer" if index < 5 else None
        assert saved.get("onboarding_field") == expected
        assert chat_start.history("founder")["items"][-1]["proposal"] == saved
    from app.db.supymem_models import EntryChatProposal
    from app.state import db
    import json
    with db()() as session:
        metadata = json.loads(session.get(EntryChatProposal, saved["id"]).receipt_json)
    assert metadata["onboarding"]["completed"] == ["offer", "customer"]
    assert len(metadata["onboarding"]["assumptions"]) == 2
    assert not saved["onboarding_choices"] and not saved["onboarding_question"]


@pytest.mark.parametrize("answer,count,action", [("everyone", 1, "question"), ("everyone", 2, "question"),
    ("everyone", 3, "proposal"), ("not sure", 1, "proposal"), ("Independent tutors", 1, "proposal")])
def test_runner_does_not_jump_to_another_field(core, answer, count, action):
    from app.idea.runner import _core_clarification
    result = core.Clarification(case="know", action="proposal", message="Saved", understanding=core.Understanding())
    snapshot = {"active_core_field": "customer", "core_question_counts": {"customer": count},
                "question_fields_asked": ["customer"], "conversation": [{"role": "founder", "text": answer}]}
    result = _core_clarification(snapshot, result)
    assert result.action == action
    if action == "question":
        assert result.question.field == "customer"
    elif answer in {"everyone", "not sure"}:
        assert result.understanding.customer == ""
        assert answer in result.understanding.assumptions[-1]


@pytest.mark.parametrize("count,expected", [(0, "offer"), (1, "offer"), (2, "customer")])
def test_initial_idea_has_only_two_followups(core, count, expected):
    from app.idea.runner import _core_clarification
    result = core.Clarification(case="know", action="proposal", message="Saved", understanding=core.Understanding())
    snapshot = {"original_idea": "AI app", "active_core_field": "offer" if count else None,
                "core_question_counts": {"offer": count}, "question_fields_asked": ["offer"] if count else [],
                "conversation": [{"role": "founder", "text": "AI app"}]}
    assert _core_clarification(snapshot, result).question.field == expected


def test_other_fields_still_cannot_repeat(core):
    from app.idea.runner import _core_clarification
    result = core.Clarification(case="know", action="question", message="Saved", understanding=core.Understanding(),
        question=core.Question(text="What price?", field="pricing_hypothesis", why="Pricing", options=[]))
    assert _core_clarification({"active_core_field": "pricing_hypothesis", "question_fields_asked": ["pricing_hypothesis"]}, result).action == "proposal"


def test_runner_reuses_audience_in_original_idea(core):
    from app.idea.runner import _core_clarification
    result = core.Clarification(case="know", action="question", message="Saved", understanding=core.Understanding(),
        question=core.Question(text="Who is it for?", field="customer", why="Audience", options=[]))
    snapshot = {"active_core_field": "offer", "original_idea": "Booking reminders for physiotherapy clinics in India.",
        "conversation": [{"role": "founder", "text": "Booking reminders for physiotherapy clinics in India."}]}
    updated = _core_clarification(snapshot, result)
    assert_moves_past(updated, "customer")
    assert updated.understanding.customer == "physiotherapy clinics in India"


@pytest.mark.parametrize("customer,expected", [("", "Independent tutors"), ("studio managers", "studio managers")])
@pytest.mark.parametrize("understanding", [{}, {"customer": "Independent tutors"}])
@pytest.mark.parametrize("website_customer", ["Independent tutors", {"answer": "Independent tutors", "source": "From your website", "status": "answered"}])
def test_runner_prefers_website_audience_over_idea_and_keeps_founder_corrections(core, customer, expected, understanding, website_customer):
    from app.idea.runner import _core_clarification
    result = core.Clarification(case="know", action="question", message="Saved",
        understanding=core.Understanding(customer=customer),
        question=core.Question(text="Who is it for?", field="customer", why="Audience", options=[]))
    snapshot = {"active_core_field": "workflow", "original_idea": "Booking reminders for dentists",
        "understanding": understanding,
        "website_answers": {"offer": "Booking reminders", "customer": website_customer}}
    updated = _core_clarification(snapshot, result)
    assert updated.understanding.customer == expected


@pytest.mark.parametrize("website,expected", [({"offer": "Lesson bookings"}, "customer"),
    ({"customer": "Independent tutors"}, "offer"),
    ({"offer": "Lesson bookings", "customer": "everyone"}, None)])
def test_website_core_answers_skip_followups(core, website, expected):
    from app.entry.chat_start import _onboarding_progress
    from app.idea.runner import _core_clarification
    intake = {"path": "startup", "extracted": {}, "prefill_answers": website, "website_answers": website}
    progress = _onboarding_progress(intake, "https://slotwise.example")
    assert progress.get("field") == expected
    assert progress["assumptions"] == []
    result = core.Clarification(case="know", action="question", message="Saved", understanding=core.Understanding(**website),
        question=core.Question(text="Describe the idea again", field="offer", why="Idea", options=[]))
    snapshot = {"website_answers": website, "understanding": website, "active_core_field": next(iter(website)),
        "original_idea": "https://slotwise.example", "question_fields_asked": list(website)}
    result = _core_clarification(snapshot, result)
    assert (result.question.field if result.question else None) == expected
    if expected == "customer":
        snapshot.update(active_core_field="customer", core_question_counts={"customer": 1},
            conversation=[{"role": "founder", "text": "everyone"}])
        assert _core_clarification(snapshot, result).question.field == "customer"
        snapshot["core_question_counts"]["customer"] = 3
        advanced = _core_clarification(snapshot, result)
        assert advanced.action == "proposal"
        assert "everyone" in advanced.understanding.assumptions[-1]


@pytest.mark.parametrize("website,field", [({"offer": "Lesson bookings"}, "customer"),
    ({"customer": "Independent tutors"}, "offer")])
def test_offline_website_runner_asks_only_missing_core(core, monkeypatch, website, field):
    import json
    from types import SimpleNamespace
    from app.idea import runner
    saved = []
    monkeypatch.setattr(runner, "_receipt", lambda _: SimpleNamespace(snapshot=json.dumps({
        "website_answers": website, "understanding": website, "question_fields_asked": list(website),
        "original_idea": "https://slotwise.example", "case_hint": "know"})))
    monkeypatch.setattr(runner, "_save", lambda _, **changes: saved.append(changes))
    runner._perform("saved-website-attempt", {})
    result = core.Clarification.model_validate_json(saved[-1]["response"])
    assert result.question.field == field


def test_vague_prefilled_audience_counts_toward_followup_budget(core):
    from app.entry.chat_start import _onboarding_progress
    from app.intake.smart import classify_fallback
    text = "An app to organize meal plans for everyone"
    assert _onboarding_progress(classify_fallback(text).to_dict(), text)["field"] == "customer"
    text += "\nAudience: people"
    assert _onboarding_progress(classify_fallback(text).to_dict(), text)["field"] == "customer"
    text += "\nAudience: anyone"
    progress = _onboarding_progress(classify_fallback(text).to_dict(), text)
    assert "question" not in progress
    assert progress["assumptions"]


def test_offline_runner_reuses_the_saved_understanding(core, monkeypatch):
    import json
    from types import SimpleNamespace
    from app.idea import runner
    saved = []
    monkeypatch.setattr(runner, "_receipt", lambda _: SimpleNamespace(snapshot=json.dumps({
        "active_core_field": "customer", "core_question_counts": {"customer": 1},
        "question_fields_asked": ["customer"], "case_hint": "know",
        "understanding": {"offer": "An invoice reminder tool"},
        "conversation": [{"role": "founder", "text": "everyone"}]})))
    monkeypatch.setattr(runner, "_save", lambda _, **changes: saved.append(changes))
    runner._perform("saved-attempt", {})
    result = core.Clarification.model_validate_json(saved[-1]["response"])
    assert result.question.field == "customer"
    assert result.understanding.offer == "An invoice reminder tool"
    assert saved[-1]["state"] == "responded"


def test_hedged_audience_in_the_idea_skips_who_is_it_for(core):
    from app.entry.chat_start import _onboarding_progress
    from app.intake.smart import classify_fallback
    text = "something for my mom's bakery orders on whatsapp idk"
    intake = classify_fallback(text)
    assert intake.prefill_answers["customer"] == "my mom's bakery customers who order on whatsapp"
    progress = _onboarding_progress(intake.to_dict(), text)
    assert "customer" in progress["completed"] and progress.get("question") != "Who is it for?"


@pytest.mark.parametrize("text,audience,acknowledgement", [
    ("something for gym owners idk, members keep leaving", "gym owners", "Gym owners, and members keep leaving."),
    ("i think something for home bakers idk, orders get buried in WhatsApp", "home bakers", "Home bakers, and orders get buried in WhatsApp."),
    ("something for home bakers, i think orders get buried in WhatsApp idk", "home bakers", "Home bakers, and orders get buried in WhatsApp."),
    ("something for gym owners IDK", "gym owners", "You mentioned gym owners."),
    ("i think something for home bakers idk", "home bakers", "You mentioned home bakers."),
])
def test_partial_idea_acknowledgement_uses_founder_words_across_domains(core, text, audience, acknowledgement):
    from app.entry.chat_start import _onboarding_progress
    from app.intake.smart import classify_fallback
    intake = classify_fallback(text).to_dict()
    assert intake["prefill_answers"]["customer"] == audience
    progress = _onboarding_progress(intake, text)
    assert progress["question"] == acknowledgement + "\n\nWhich group should we focus on first?"
    assert progress["field"] == "customer"
    assert progress["choices"] == []


@pytest.mark.parametrize("text", ["A tool to track delivery routes", "An app to organize meal plans"])
@pytest.mark.parametrize("fragment", ["10", "5,000", "10 to 50 person", "1-3 locations", "under 20 employees", "5,000 users", "aged 18 to 25", "small", "in", "in Canada"])
def test_audience_fragments_are_not_acknowledged_or_offered_as_chips(core, text, fragment):
    from app.entry.chat_start import _onboarding_progress
    from app.intake.smart import IntakeResult, _prefill

    assert not _prefill(text + "\nAudience: " + fragment).get("customer")
    for source in ("extracted", "prefill_answers", "website_answers"):
        intake = {"path": "startup", "confidence": .95, "founder_line": "Let's clarify your idea.",
                  "button_label": "Continue", "extracted": {"idea": text}, source: {"audience" if source == "extracted" else "customer": fragment}}
        progress = _onboarding_progress(intake, text)
        assert progress["question"] == "Who is it for?" and progress["choices"] == []
        assert fragment not in progress["choices"]
        validated = IntakeResult.model_validate(intake)
        assert not validated.extracted.audience
        assert not validated.prefill_answers.get("customer") and not validated.website_answers.get("customer")
    progress = _onboarding_progress({"path": "startup", "extracted": {"idea": text}}, text + "\nAudience: " + fragment)
    assert progress["question"] == "Who is it for?" and progress["choices"] == []
    assert fragment not in progress["choices"]


@pytest.mark.parametrize("answer", ["everyone who likes pickles i guess, mostly people in india",
    "anyone who enjoys audio lessons", "all people who want to learn languages",
    "people who like language podcasts"])
def test_broad_audience_cannot_be_accepted_on_words_or_model_confidence(core, answer):
    assert core.core_answer_status(answer, 0, field="customer") == (False, False)
    assert core.core_answer_status(answer, 0, field="customer", confidence=.99) == (False, False)
    assert core.core_answer_status(answer, 2, field="customer", confidence=.99) == (True, True)


@pytest.mark.parametrize("answer,segments", [
    ("everyone who likes pickles i guess, mostly people in india",
     ["Expats looking for regional pickles", "Specialty food shop owners", "Regional cuisine supper-club hosts"]),
    ("people who like audio lessons",
     ["Night-shift nurses learning a language", "New immigrants preparing for language exams", "Tour guides learning conversational phrases"]),
])
def test_broad_audience_refinement_keeps_three_options_and_founder_words(core, answer, segments):
    from app.idea.runner import _core_clarification
    from app.ventures.founder_facts import policy

    facts = {"audience": {"value": answer, "source": "founder message", "confidence": .99}}
    assert policy(facts, "customer") == "ask"
    result = core.Clarification(case="know", action="question", message="Saved", confidence=.99,
        understanding=core.Understanding(), question=core.Question(text="Who should you start with?", field="customer",
            why="Start with a reachable group", options=segments))
    snapshot = {"active_core_field": "customer", "core_question_counts": {"customer": 1},
        "question_fields_asked": ["offer", "customer"], "founder_facts": facts,
        "conversation": [{"role": "founder", "text": answer}]}
    refined = _core_clarification(snapshot, result)
    assert refined.question.field == "customer" and refined.question.options == segments
    assert "too broad to reach" in refined.question.text and "keep your own words" in refined.question.text
    assert snapshot["conversation"][-1]["text"] == answer
    snapshot["conversation"].append({"role": "founder", "text": "Not sure"})
    kept = _core_clarification(snapshot, refined)
    assert kept.action == "proposal" and kept.understanding.customer == ""


@pytest.mark.parametrize("path", ["startup", "clarify", "multiple_ideas"])
@pytest.mark.parametrize("answer", ["everyone", "Not sure"])
def test_entry_chip_contract_separates_onboarding_from_plain_clarification(core, monkeypatch, path, answer):
    from app.entry import chat_start
    from app.intake.smart import classify_fallback

    text = "FirstLight: A tool to organize audio lessons\nAudience: " + answer
    intake = classify_fallback(text).to_dict()
    intake.update(path=path, confidence=.95, choices=["Night-shift nurses learning a language",
        "New immigrants preparing for language exams", "Tour guides learning conversational phrases"])
    intake["onboarding_options"] = {"customer": intake["choices"]}
    monkeypatch.setattr(chat_start, "_assert_can_start", lambda *_: None)
    monkeypatch.setattr(chat_start, "record_preview", lambda *_: None)
    at = chat_start.utcnow()
    monkeypatch.setattr(chat_start, "utcnow", lambda: at)
    result = chat_start.preview(text=text, actor_id="founder", intake=intake)
    proposal = result["proposal"] if result["status"] == "proposal" else result
    saved = chat_start.get(proposal["id"], "founder")["proposal"]
    assert chat_start.history("founder")["items"][-1] == {"text": text, "proposal": saved}
    if path == "multiple_ideas":
        assert result["status"] == "clarify" and not saved.get("onboarding_question")
        assert proposal["choices"] == saved["choices"] == intake["choices"]
    elif answer == "Not sure":
        progress = chat_start._onboarding_progress(intake, text)
        assert "customer" in progress["completed"] and progress["assumptions"]
        assert not saved["onboarding_question"] and not saved["onboarding_choices"]
    else:
        progress = chat_start._onboarding_progress(intake, text)
        assert proposal["onboarding_choices"] == saved["onboarding_choices"] == progress["choices"]
        assert len(saved["onboarding_choices"]) == 3 and "choices" not in saved
        assert "customer" not in progress["completed"]
        if result["status"] == "clarify":
            assert result["choices"] == []
